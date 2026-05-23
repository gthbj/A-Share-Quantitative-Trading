"""Google Cloud BigQuery 数据源实现。

本模块通过 google-cloud-bigquery 连接 BigQuery，从 `ashare` dataset
拉取 A 股历史行情。首次查询后写入本地 Parquet 缓存（复用 LocalStorage），
后续命中本地缓存即跳过 BigQuery 查询，节省查询成本。

当前接入进度：
  - ✅ 日K线：fact_equity_kline_1d / fact_fund_kline_1d / fact_index_kline_1d 已接入
    （adjust_type 字段内置 none/qfq/hfq，无需 Python 侧即时复权）
  - ✅ 股票列表：dim_security 表已接入
  - ✅ 指数成分股：fact_board_component_1d 表已接入
  - ⚠️  分钟K线：对应表尚未建立，调用时抛 NotImplementedError

代码格式约定：
  - 框架对外与表内存储统一使用标准格式 ``XXXXXX.SH`` / ``XXXXXX.SZ``，
    无需像 MaxCompute 那样做 shXXXXXX 双向映射。

分区裁剪：
  - 所有表按 partition_month INT64（格式 YYYYMM）分区
  - 查询时根据 [start_date, end_date] 计算覆盖的 partition_month 列表，
    SQL 中用 `partition_month IN (...)` 触发分区裁剪，避免全表扫描
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from .base_data_source import BaseDataSource
from .local_storage import LocalStorage
from utils.logger import get_logger

logger = get_logger(__name__)

# 基金/ETF/LOF 代码前缀（用于路由到 fact_fund_kline_1d）
_FUND_PREFIXES_SH = ("51", "56", "58", "11")
_FUND_PREFIXES_SZ = ("15", "16")

# 指数代码前缀（用于路由到 fact_index_kline_1d）
_INDEX_PREFIXES = ("000", "399", "930", "950", "932")


class BigQueryDataSource(BaseDataSource):
    """基于 Google Cloud BigQuery 的 A 股数据源。

    Args:
        project_id: GCP 项目 ID，如 ``data-aquarium``。
        dataset: BigQuery dataset 名，如 ``ashare``。
        location: BigQuery 位置，如 ``asia-east2``。
        credentials_path: 服务账号 JSON 文件路径（可选）。
            若未提供，则依赖 ``GOOGLE_APPLICATION_CREDENTIALS`` 环境变量。
        storage: 本地缓存存储器。
        use_cache: 是否启用本地缓存。
        cache_retention_days: 缓存保留天数。
        cache_max_size_gb: 缓存目录最大体积（GB）。
        tables: 表名映射，键含 kline_1d_equity / kline_1d_fund / kline_1d_index /
            adjust_factor / dim_security / board_component / kline_5min / kline_1min /
            kline_15min / kline_30min / kline_60min 等。
    """

    def __init__(
        self,
        project_id: str,
        dataset: str = "ashare",
        location: str = "asia-east2",
        credentials_path: Optional[str] = None,
        storage: Optional[LocalStorage] = None,
        use_cache: bool = True,
        cache_retention_days: int = 7,
        cache_max_size_gb: float = 1.0,
        tables: Optional[Dict[str, str]] = None,
    ) -> None:
        self.project_id = project_id
        self.dataset = dataset
        self.location = location
        self.credentials_path = credentials_path
        self.storage = storage or LocalStorage()
        self.use_cache = use_cache
        self.cache_retention_days = cache_retention_days
        self.cache_max_size_gb = cache_max_size_gb
        self.tables = tables or {}
        self._client = None

        if self.use_cache:
            self._cleanup_cache()

    # ------------------------------------------------------------------ #
    # 连接与缓存
    # ------------------------------------------------------------------ #

    def _get_client(self):
        if self._client is None:
            try:
                from google.cloud import bigquery
            except ImportError as e:
                raise ImportError(
                    "未安装 google-cloud-bigquery，请先执行 "
                    "`pip install google-cloud-bigquery`。"
                ) from e
            kwargs = {"project": self.project_id, "location": self.location}
            if self.credentials_path:
                from google.oauth2 import service_account
                credentials = service_account.Credentials.from_service_account_file(
                    self.credentials_path
                )
                kwargs["credentials"] = credentials
            self._client = bigquery.Client(**kwargs)
            logger.info(
                f"BigQuery 连接已建立: project={self.project_id}, "
                f"dataset={self.dataset}, location={self.location}"
            )
        return self._client

    def _cache_root(self) -> Path:
        for attr in ("cache_dir", "root", "base_dir"):
            if hasattr(self.storage, attr):
                return Path(getattr(self.storage, attr))
        return Path("data/raw")

    def _cleanup_cache(self) -> None:
        """按 retention_days + max_size_gb 清理本地缓存。"""
        cache_dir = self._cache_root()
        if not cache_dir.exists():
            return

        now = time.time()
        retention_seconds = self.cache_retention_days * 86400
        max_size_bytes = int(self.cache_max_size_gb * (1024 ** 3))

        entries = []
        for f in cache_dir.rglob("*"):
            if not f.is_file():
                continue
            try:
                st = f.stat()
                entries.append((f, st.st_mtime, st.st_size))
            except OSError:
                continue

        kept = []
        expired = 0
        for f, mtime, size in entries:
            if now - mtime > retention_seconds:
                try:
                    f.unlink()
                    expired += 1
                except OSError:
                    kept.append((f, mtime, size))
            else:
                kept.append((f, mtime, size))
        if expired:
            logger.info(
                f"缓存清理：删除 {expired} 个超期文件（>{self.cache_retention_days}天）"
            )

        total = sum(s for _, _, s in kept)
        if total > max_size_bytes:
            kept.sort(key=lambda x: x[1])
            removed = 0
            for f, _, size in kept:
                if total <= max_size_bytes:
                    break
                try:
                    f.unlink()
                    total -= size
                    removed += 1
                except OSError:
                    continue
            if removed:
                logger.info(
                    f"缓存清理：删除 {removed} 个最旧文件以满足 "
                    f"{self.cache_max_size_gb}GB 上限"
                )

    def _require_table(self, key: str) -> str:
        """读取并校验表名配置，未配置则抛出明确错误。"""
        name = self.tables.get(key, "").strip()
        if not name:
            raise NotImplementedError(
                f"对应表尚未建立或未配置：config/backtest.yaml 中 "
                f"data.bigquery.tables.{key} 为空。\n"
                f"如该表已存在请填入表名；如尚未建立请等待数据准备完成后再使用。"
            )
        return name

    # ------------------------------------------------------------------ #
    # 代码路由 / 分区列表
    # ------------------------------------------------------------------ #

    @staticmethod
    def _is_fund_code(code: str) -> bool:
        """判断代码是否为基金（ETF/LOF/可转债）。"""
        if "." not in code:
            return False
        bare, suffix = code.split(".")
        if suffix.upper() == "SH" and bare.startswith(_FUND_PREFIXES_SH):
            return True
        if suffix.upper() == "SZ" and bare.startswith(_FUND_PREFIXES_SZ):
            return True
        return False

    @staticmethod
    def _is_index_code(code: str) -> bool:
        """判断代码是否为指数。"""
        if "." not in code:
            return False
        bare, suffix = code.split(".")
        if bare.startswith(_INDEX_PREFIXES) and suffix.upper() in ("SH", "SZ", "CSI"):
            return True
        return False

    def _resolve_kline_table(self, code: str, period: str) -> Tuple[str, str, str]:
        """根据代码和周期解析目标表。

        Returns:
            (table_key, code_column, asset_type)
            asset_type: equity | fund | index
        """
        if period == "daily":
            if self._is_index_code(code):
                return "kline_1d_index", "index_code", "index"
            if self._is_fund_code(code):
                return "kline_1d_fund", "fund_code", "fund"
            return "kline_1d_equity", "security_code", "equity"

        if period in ("1min", "5min", "15min", "30min", "60min"):
            # 分钟级暂时统一用 equity 前缀，未来可按资产类型拆分
            key = f"kline_{period}_equity"
            return key, "security_code", "equity"

        raise ValueError(f"不支持的 period: {period}")

    @staticmethod
    def _partition_months_in_range(start_date: str, end_date: str) -> List[int]:
        """根据 [YYYYMMDD, YYYYMMDD] 返回覆盖的 partition_month 列表（INT，YYYYMM）。

        示例：(\"20200101\", \"20200315\") → [202001, 202002, 202003]
        """
        if len(start_date) < 6 or len(end_date) < 6:
            raise ValueError(
                f"日期格式应为 YYYYMMDD：start={start_date}, end={end_date}"
            )
        y, m = int(start_date[:4]), int(start_date[4:6])
        end_y, end_m = int(end_date[:4]), int(end_date[4:6])
        if (y, m) > (end_y, end_m):
            return []
        result: List[int] = []
        while (y, m) <= (end_y, end_m):
            result.append(y * 100 + m)
            m += 1
            if m > 12:
                y += 1
                m = 1
        return result

    # ------------------------------------------------------------------ #
    # BaseDataSource 接口实现
    # ------------------------------------------------------------------ #

    def get_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        period: str = "daily",
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        norm_code = code.split(".")[0]

        # 1) 本地缓存优先
        if self.use_cache:
            cached_full = self.storage.load_bars_raw(norm_code, period=period)
            if not cached_full.empty and "date" in cached_full.columns:
                cmin = str(cached_full["date"].min())
                cmax = str(cached_full["date"].max())
                if cmin <= str(start_date) and cmax >= str(end_date):
                    df = self.storage.load_bars(
                        norm_code, start_date, end_date, period=period
                    )
                    return df

        # 2) 按资产类型路由拉取
        if period == "daily":
            df = self._fetch_daily_bars(code, start_date, end_date, adjust)
        elif period in ("1min", "5min", "15min", "30min", "60min"):
            df = self._fetch_minute_bars(code, start_date, end_date, period, adjust)
        else:
            raise ValueError(f"不支持的 period: {period}")

        if df.empty:
            return df

        # 3) 写缓存
        if self.use_cache:
            existing = self.storage.load_bars_raw(norm_code, period=period)
            if not existing.empty:
                combined = pd.concat([existing, df], ignore_index=True)
                combined = combined.drop_duplicates(subset=["date"], keep="last")
                combined = combined.sort_values("date").reset_index(drop=True)
                self.storage.save_bars(norm_code, combined, period=period)
            else:
                self.storage.save_bars(norm_code, df, period=period)

        # 4) 按请求区间过滤并返回
        if period == "daily":
            mask = (df["date"] >= str(start_date)) & (df["date"] <= str(end_date))
        else:
            mask = (df["date"] >= str(start_date)) & (
                df["date"] <= str(end_date) + "9999"
            )
        return df.loc[mask].copy().reset_index(drop=True)

    def get_stock_list(self) -> pd.DataFrame:
        """从 dim_security 获取股票列表。

        返回列：[code, name, list_date, industry]
        其中 industry 字段当前为空字符串（dim_security 未提供该字段）。
        """
        cached = self.storage.load_stock_list()
        if not cached.empty:
            return cached

        table = self._require_table("dim_security")
        sql = (
            f"SELECT security_code, security_name, list_date\n"
            f"FROM `{self.project_id}.{self.dataset}.{table}`\n"
            f"WHERE security_type = 'stock'\n"
            f"  AND is_active = TRUE\n"
            f"ORDER BY security_code"
        )
        df = self._execute_sql(sql)

        if df.empty:
            logger.warning("get_stock_list: BigQuery 返回空结果")
            return df

        df = df.rename(
            columns={
                "security_code": "code",
                "security_name": "name",
                "list_date": "list_date",
            }
        )
        df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce").dt.strftime(
            "%Y%m%d"
        )
        df["list_date"] = df["list_date"].fillna("")
        df["industry"] = ""
        df = df[["code", "name", "list_date", "industry"]].copy()

        self.storage.save_stock_list(df)
        return df

    def get_index_constituents(self, index_code: str) -> List[str]:
        """从 fact_board_component_1d 获取指数/板块成分股。

        返回成分股代码列表（标准格式）。
        """
        table = self._require_table("board_component")
        # 取该指数最新日期的成分股
        sql = (
            f"SELECT security_code\n"
            f"FROM `{self.project_id}.{self.dataset}.{table}`\n"
            f"WHERE board_code = '{index_code}'\n"
            f"  AND date = (\n"
            f"    SELECT MAX(date)\n"
            f"    FROM `{self.project_id}.{self.dataset}.{table}`\n"
            f"    WHERE board_code = '{index_code}'\n"
            f"  )\n"
            f"ORDER BY security_code"
        )
        df = self._execute_sql(sql)
        if df.empty:
            logger.warning(
                f"get_index_constituents: BigQuery 返回空结果（index_code={index_code}）"
            )
            return []
        return df["security_code"].astype(str).tolist()

    # ------------------------------------------------------------------ #
    # SQL 拼接与执行
    # ------------------------------------------------------------------ #

    def _fetch_daily_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str,
    ) -> pd.DataFrame:
        """日K查询。根据代码类型自动路由到 equity / fund / index 表。"""
        table_key, code_col, asset_type = self._resolve_kline_table(code, "daily")
        table = self._require_table(table_key)
        partition_months = self._partition_months_in_range(start_date, end_date)
        if not partition_months:
            return pd.DataFrame()

        pm_list = ", ".join(str(pm) for pm in partition_months)
        sd = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
        ed = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

        # 日K表内置 adjust_type（none/qfq/hfq），直接透传
        adjust_type = adjust if adjust in ("qfq", "hfq") else "none"

        if asset_type == "index":
            # 指数表无 adjust_type
            sql = (
                f"SELECT {code_col}, date, open, high, low, close, volume, amount\n"
                f"FROM `{self.project_id}.{self.dataset}.{table}`\n"
                f"WHERE {code_col} = '{code}'\n"
                f"  AND partition_month IN ({pm_list})\n"
                f"  AND date >= '{sd}'\n"
                f"  AND date <= '{ed}'\n"
                f"ORDER BY date"
            )
        else:
            sql = (
                f"SELECT {code_col}, date, open, high, low, close, volume, amount\n"
                f"FROM `{self.project_id}.{self.dataset}.{table}`\n"
                f"WHERE {code_col} = '{code}'\n"
                f"  AND adjust_type = '{adjust_type}'\n"
                f"  AND partition_month IN ({pm_list})\n"
                f"  AND date >= '{sd}'\n"
                f"  AND date <= '{ed}'\n"
                f"ORDER BY date"
            )

        df = self._execute_sql(sql)
        if df.empty:
            return df

        # 统一列名：code_col → code，date → YYYYMMDD 字符串
        df = df.rename(columns={code_col: "code"})
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y%m%d")
        df = df.dropna(subset=["date"])
        return df[["code", "date", "open", "high", "low", "close", "volume", "amount"]].copy()

    def _fetch_minute_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        period: str,
        adjust: str,
    ) -> pd.DataFrame:
        """分钟K查询。当前分钟级表尚未建立。"""
        table_key, code_col, _ = self._resolve_kline_table(code, period)
        self._require_table(table_key)
        raise NotImplementedError(
            f"BigQuery 当前分钟级 K 线表（data.bigquery.tables.{table_key}）尚未建立。\n"
            f"请先完成数据迁移后再使用 {period} 周期回测。"
        )

    def _execute_sql(self, sql: str, max_retries: int = 3) -> pd.DataFrame:
        """执行 SQL 并返回 DataFrame，自动指数退避重试网络故障。"""
        client = self._get_client()
        logger.debug(f"BigQuery SQL:\n{sql}")
        last_exc: Exception = RuntimeError("未知错误")
        for attempt in range(max_retries):
            try:
                query_job = client.query(sql)
                return query_job.to_dataframe()
            except Exception as e:
                last_exc = e
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # 1s → 2s → 4s
                    logger.warning(
                        f"BigQuery SQL 执行失败（第 {attempt + 1} 次），{wait}s 后重试: {e}"
                    )
                    time.sleep(wait)
        raise last_exc
