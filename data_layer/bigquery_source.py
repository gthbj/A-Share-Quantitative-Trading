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
import os
import subprocess
from datetime import datetime, timedelta, timezone
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

# 账户专项权限默认值：false 表示当前账户没有该权限。
_DEFAULT_TRADING_PERMISSIONS = {
    "allow_bse": False,
    "allow_star_market": False,
    "allow_chinext": False,
    "allow_hk_stock_connect": False,
    "allow_neeq": False,
    "allow_risk_warning": False,
    "allow_delisting": False,
    "allow_margin_trading": False,
    "allow_stock_options": False,
    "allow_convertible_bonds": False,
    "allow_cdr": False,
    "allow_unknown_security": False,
}


def _use_gcloud_access_token() -> bool:
    return os.environ.get("ASHARE_USE_GCLOUD_ACCESS_TOKEN", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _gcloud_credentials():
    from google.auth.credentials import Credentials

    class GcloudAccessTokenCredentials(Credentials):
        def refresh(self, request) -> None:
            result = subprocess.run(
                ["gcloud", "auth", "print-access-token", "--quiet"],
                check=True,
                capture_output=True,
                text=True,
                timeout=30,
            )
            token = result.stdout.strip()
            if not token:
                raise RuntimeError("gcloud did not return an access token")
            self.token = token
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=50)

    credentials = GcloudAccessTokenCredentials()
    credentials.refresh(None)
    return credentials


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
        trading_permissions: 账户专项交易权限。false 表示当前账户没有对应权限，
            数据源会过滤对应板块或证券状态的买入候选。
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
        trading_permissions: Optional[Dict[str, bool]] = None,
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
        merged_permissions = dict(_DEFAULT_TRADING_PERMISSIONS)
        if trading_permissions:
            merged_permissions.update(
                {key: bool(value) for key, value in trading_permissions.items()}
            )
        self.trading_permissions = merged_permissions
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
            elif _use_gcloud_access_token():
                kwargs["credentials"] = _gcloud_credentials()
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

    def _optional_table(self, key: str, default: str) -> str:
        """读取可选表名配置；未配置时使用当前 BigQuery 分层默认表名。"""
        return self.tables.get(key, "").strip() or default

    def get_multi_bars(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        period: str = "daily",
        adjust: str = "qfq",
    ) -> Dict[str, pd.DataFrame]:
        """批量获取 K 线。

        日线股票回测使用单次 BigQuery IN 查询，避免候选池较大时逐股查询。
        其他资产/周期保留基类串行路径。
        """
        normalized = [str(code) for code in codes]
        if period == "daily" and normalized and all(
            not self._is_fund_code(code) and not self._is_index_code(code)
            for code in normalized
        ):
            normalized = self._filter_codes_by_permissions(normalized)
            if not normalized:
                return {}
            return self._fetch_daily_equity_bars_batch(normalized, start_date, end_date, adjust or "none")
        return super().get_multi_bars(codes, start_date, end_date, period, adjust)

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
        suffix = suffix.upper()
        if bare.startswith(("000", "930", "932", "950")) and suffix in ("SH", "CSI"):
            return True
        if bare.startswith("399") and suffix == "SZ":
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
            return "kline_1d_equity", "equity_code", "equity"

        if period in ("1min", "5min", "15min", "30min", "60min"):
            # 分钟级暂时统一用 equity 前缀，未来可按资产类型拆分
            key = f"kline_{period}_equity"
            return key, "equity_code", "equity"

        raise ValueError(f"不支持的 period: {period}")

    def _permission_enabled(self, key: str) -> bool:
        return bool(self.trading_permissions.get(key, False))

    @staticmethod
    def _bare_code(code: str) -> str:
        return str(code).split(".")[0].strip()

    def _is_code_allowed_by_permissions(self, code: str) -> bool:
        """基于代码前缀做本地快速过滤。

        该方法只覆盖能从代码本身判断的交易权限；ST/退市等依赖
        ``dim_security`` 的状态字段，在 BigQuery SQL 过滤中处理。
        """
        bare = self._bare_code(code)
        suffix = str(code).split(".")[-1].upper() if "." in str(code) else ""

        if not self._permission_enabled("allow_bse") and (
            suffix == "BJ" or bare.startswith(("43", "83", "87", "88", "920"))
        ):
            return False
        if not self._permission_enabled("allow_star_market") and bare.startswith(("688", "689")):
            return False
        if not self._permission_enabled("allow_chinext") and bare.startswith(("300", "301")):
            return False
        return True

    def _filter_codes_by_permissions(self, codes: List[str]) -> List[str]:
        allowed = [code for code in codes if self._is_code_allowed_by_permissions(code)]
        blocked_count = len(codes) - len(allowed)
        if blocked_count:
            logger.info("账户权限过滤：跳过 %s 个不可交易标的", blocked_count)
        return allowed

    @staticmethod
    def _cache_code_key(code: str, adjust: Optional[str]) -> str:
        """本地 K 线缓存 key：完整代码 + 复权口径，避免 qfq/none/hfq 串缓存。"""
        safe_code = str(code).replace("/", "_").replace(".", "_")
        adjust_type = adjust if adjust in ("qfq", "hfq") else "none"
        return f"{safe_code}_{adjust_type}"

    def _security_permission_filter_sql(
        self,
        alias: str = "s",
        use_current_name_lifecycle_filters: bool = True,
    ) -> str:
        """生成基于 dim_security 的账户权限过滤 SQL。"""
        conditions: List[str] = []

        if not self._permission_enabled("allow_unknown_security"):
            conditions.append(f"{alias}.security_code IS NOT NULL")
        if not self._permission_enabled("allow_bse"):
            conditions.extend(
                [
                    f"COALESCE({alias}.exchange_code, '') != 'BSE'",
                    f"COALESCE({alias}.market_type, '') NOT LIKE '%北交%'",
                    f"NOT REGEXP_CONTAINS(COALESCE({alias}.security_code, ''), r'^(43|83|87|88|920)')",
                ]
            )
        if not self._permission_enabled("allow_star_market"):
            conditions.extend(
                [
                    f"COALESCE({alias}.market_type, '') NOT LIKE '%科创%'",
                    f"NOT REGEXP_CONTAINS(COALESCE({alias}.security_code, ''), r'^(688|689)')",
                ]
            )
        if not self._permission_enabled("allow_chinext"):
            conditions.extend(
                [
                    f"COALESCE({alias}.market_type, '') NOT LIKE '%创业%'",
                    f"NOT REGEXP_CONTAINS(COALESCE({alias}.security_code, ''), r'^(300|301)')",
                ]
            )
        if not self._permission_enabled("allow_hk_stock_connect"):
            conditions.append(f"COALESCE({alias}.market_type, '') NOT LIKE '%港股通%'")
        if not self._permission_enabled("allow_neeq"):
            conditions.extend(
                [
                    f"COALESCE({alias}.exchange_code, '') != 'NEEQ'",
                    f"COALESCE({alias}.market_type, '') NOT LIKE '%新三板%'",
                    f"COALESCE({alias}.market_type, '') NOT LIKE '%全国股转%'",
                ]
            )
        if (
            use_current_name_lifecycle_filters
            and not self._permission_enabled("allow_risk_warning")
        ):
            conditions.append(
                f"NOT REGEXP_CONTAINS(UPPER(COALESCE({alias}.security_name, '')), r'\\*?ST')"
            )
        if (
            use_current_name_lifecycle_filters
            and not self._permission_enabled("allow_delisting")
        ):
            conditions.extend(
                [
                    f"COALESCE({alias}.security_name, '') NOT LIKE '%退%'",
                    f"COALESCE({alias}.market_type, '') NOT LIKE '%退市%'",
                ]
            )
        if not self._permission_enabled("allow_cdr"):
            conditions.extend(
                [
                    f"COALESCE({alias}.security_type, '') != 'cdr'",
                    f"COALESCE({alias}.security_name, '') NOT LIKE '%存托%'",
                ]
            )

        return " AND ".join(conditions) if conditions else "TRUE"

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
        if (
            period == "daily"
            and not self._is_fund_code(code)
            and not self._is_index_code(code)
            and not self._is_code_allowed_by_permissions(code)
        ):
            logger.info("账户权限过滤：跳过不可交易标的 %s", code)
            return pd.DataFrame()

        cache_code = self._cache_code_key(code, adjust)

        # 1) 本地缓存优先
        if self.use_cache:
            cached_full = self.storage.load_bars_raw(cache_code, period=period)
            if not cached_full.empty and "date" in cached_full.columns:
                cmin = str(cached_full["date"].min())
                cmax = str(cached_full["date"].max())
                if cmin <= str(start_date) and cmax >= str(end_date):
                    df = self.storage.load_bars(
                        cache_code, start_date, end_date, period=period
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
            existing = self.storage.load_bars_raw(cache_code, period=period)
            if not existing.empty:
                combined = pd.concat([existing, df], ignore_index=True)
                combined = combined.drop_duplicates(subset=["date"], keep="last")
                combined = combined.sort_values("date").reset_index(drop=True)
                self.storage.save_bars(cache_code, combined, period=period)
            else:
                self.storage.save_bars(cache_code, df, period=period)

        # 4) 按请求区间过滤并返回
        if period == "daily":
            mask = (df["date"] >= str(start_date)) & (df["date"] <= str(end_date))
        else:
            mask = (df["date"] >= str(start_date)) & (
                df["date"] <= str(end_date) + "9999"
            )
        return df.loc[mask].copy().reset_index(drop=True)

    def get_stock_list(
        self,
        as_of_date: Optional[str] = None,
        include_inactive: bool = False,
    ) -> pd.DataFrame:
        """从 dim_security 获取股票列表。

        返回列：[code, name, list_date, industry]
        as_of_date 指定时按该日期判断上市/退市；include_inactive=True 时返回全历史股票池。
        权限配置会过滤当前账户不可交易的专项板块股票。
        """
        use_current_name_filters = not include_inactive and not as_of_date
        permission_sql = self._security_permission_filter_sql(
            "s",
            use_current_name_lifecycle_filters=use_current_name_filters,
        )
        use_stock_list_cache = (
            self.use_cache
            and permission_sql == "TRUE"
            and not include_inactive
            and not as_of_date
        )
        if use_stock_list_cache:
            cached = self.storage.load_stock_list()
            if not cached.empty:
                return cached

        table = self._require_table("dim_security")
        where = [
            "s.security_type = 'stock'",
            permission_sql,
        ]
        if as_of_date:
            as_of = self._to_date_literal(as_of_date)
            where.extend(
                [
                    f"(s.list_date IS NULL OR s.list_date <= DATE '{as_of}')",
                    f"(s.delist_date IS NULL OR s.delist_date > DATE '{as_of}')",
                ]
            )
        elif not include_inactive:
            where.append("s.is_active = TRUE")
        where_sql = " AND ".join(where)
        sql = (
            f"SELECT security_code, security_name, list_date\n"
            f"FROM `{self.project_id}.{self.dataset}.{table}` AS s\n"
            f"WHERE {where_sql}\n"
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

        if use_stock_list_cache:
            self.storage.save_stock_list(df)
        return df

    def get_liquidity_top_equities(
        self,
        as_of_date: str,
        top_n: int = 500,
        lookback_days: int = 60,
        adjust: str = "qfq",
    ) -> List[str]:
        """按回测时点之前的近 N 日平均成交额选股票池。

        该 helper 只使用 ``date < as_of_date`` 的历史行情，避免在回测首日
        用到当天收盘后才知道的成交额。账户专项权限过滤与 ``get_stock_list`` 保持一致。
        """
        top_n = int(top_n)
        lookback_days = int(lookback_days)
        if top_n <= 0:
            return []
        if lookback_days <= 0:
            raise ValueError("lookback_days 必须为正数")

        table = self._require_table("kline_1d_equity")
        dim_table = self._require_table("dim_security")
        as_of = datetime.strptime(str(as_of_date)[:8], "%Y%m%d")
        end = as_of - timedelta(days=1)
        start = as_of - timedelta(days=max(lookback_days * 2, lookback_days + 7))
        partition_months = self._partition_months_in_range(
            start.strftime("%Y%m%d"), end.strftime("%Y%m%d")
        )
        if not partition_months:
            return []

        pm_list = ", ".join(str(pm) for pm in partition_months)
        permission_sql = self._security_permission_filter_sql(
            "s",
            use_current_name_lifecycle_filters=False,
        )
        adjust_type = adjust if adjust in ("qfq", "hfq") else "none"
        min_observations = min(5, lookback_days)
        sql = f"""
WITH liquidity AS (
  SELECT
    k.equity_code,
    AVG(COALESCE(k.amount, 0)) AS avg_amount,
    COUNT(*) AS observation_count
  FROM `{self.project_id}.{self.dataset}.{table}` AS k
  JOIN `{self.project_id}.{self.dataset}.{dim_table}` AS s
    ON k.equity_code = s.security_code
  WHERE k.adjust_type = '{adjust_type}'
    AND k.partition_month IN ({pm_list})
    AND k.date >= DATE '{start.strftime("%Y-%m-%d")}'
    AND k.date < DATE '{as_of.strftime("%Y-%m-%d")}'
    AND s.security_type = 'stock'
    AND (s.list_date IS NULL OR s.list_date <= DATE '{end.strftime("%Y-%m-%d")}')
    AND (s.delist_date IS NULL OR s.delist_date > DATE '{as_of.strftime("%Y-%m-%d")}')
    AND {permission_sql}
  GROUP BY k.equity_code
  HAVING observation_count >= {min_observations}
)
SELECT equity_code
FROM liquidity
ORDER BY avg_amount DESC, equity_code
LIMIT {top_n}
"""
        df = self._execute_sql(sql)
        if df.empty or "equity_code" not in df.columns:
            logger.warning(
                f"get_liquidity_top_equities: {as_of_date} 未返回可交易股票池"
            )
            return []
        codes = df["equity_code"].astype(str).tolist()
        logger.info(
            f"流动性股票池初始化: as_of={as_of_date}, top_n={top_n}, "
            f"lookback_days={lookback_days}, selected={len(codes)}"
        )
        return codes

    def get_index_constituents(self, index_code: str) -> List[str]:
        """从 fact_board_component_1d 获取指数/板块成分股。

        返回成分股代码列表（标准格式）。
        """
        table = self._require_table("board_component")
        # 取该指数最新日期的成分股
        sql = (
            f"SELECT equity_code\n"
            f"FROM `{self.project_id}.{self.dataset}.{table}`\n"
            f"WHERE board_code = '{index_code}'\n"
            f"  AND date = (\n"
            f"    SELECT MAX(date)\n"
            f"    FROM `{self.project_id}.{self.dataset}.{table}`\n"
            f"    WHERE board_code = '{index_code}'\n"
            f"  )\n"
            f"ORDER BY equity_code"
        )
        df = self._execute_sql(sql)
        if df.empty:
            logger.warning(
                f"get_index_constituents: BigQuery 返回空结果（index_code={index_code}）"
            )
            return []
        return df["equity_code"].astype(str).tolist()

    # ------------------------------------------------------------------ #
    # DWS 特征查询（策略可选使用，不改变 BaseDataSource 抽象接口）
    # ------------------------------------------------------------------ #

    @staticmethod
    def _to_yyyymmdd(date_value: str) -> str:
        digits = "".join(ch for ch in str(date_value) if ch.isdigit())
        if len(digits) < 8:
            raise ValueError(f"日期格式应包含 YYYYMMDD: {date_value}")
        return digits[:8]

    @staticmethod
    def _to_date_literal(date_value: str) -> str:
        yyyymmdd = BigQueryDataSource._to_yyyymmdd(date_value)
        return f"{yyyymmdd[:4]}-{yyyymmdd[4:6]}-{yyyymmdd[6:8]}"

    @staticmethod
    def _sql_string_list(values: List[str]) -> str:
        return ", ".join("'" + str(v).replace("'", "''") + "'" for v in values)

    def _equity_feature_join_sql(self, feature_set: str, where_sql: str) -> str:
        daily = self._optional_table("dws_equity_daily_features", "dws_equity_daily_features")
        fundamental = self._optional_table(
            "dws_equity_fundamental_features", "dws_equity_fundamental_features"
        )
        event = self._optional_table(
            "dws_equity_event_money_flow_features_1d",
            "dws_equity_event_money_flow_features_1d",
        )

        base_cols = """
  b.equity_code,
  b.equity_code AS code,
  FORMAT_DATE('%Y%m%d', b.date) AS date,
  b.partition_month,
  SAFE_CAST(b.close AS FLOAT64) AS close,
  SAFE_CAST(b.return_1d AS FLOAT64) AS return_1d,
  SAFE_CAST(b.return_5d AS FLOAT64) AS return_5d,
  SAFE_CAST(b.return_10d AS FLOAT64) AS return_10d,
  SAFE_CAST(b.return_20d AS FLOAT64) AS return_20d,
  SAFE_CAST(b.volume_ma5_ratio AS FLOAT64) AS volume_ma5_ratio,
  SAFE_CAST(b.volume_ma20_ratio AS FLOAT64) AS volume_ma20_ratio,
  SAFE_CAST(b.amount_ma5_ratio AS FLOAT64) AS amount_ma5_ratio,
  SAFE_CAST(b.std_5d AS FLOAT64) AS std_5d,
  SAFE_CAST(b.std_20d AS FLOAT64) AS std_20d,
  SAFE_CAST(b.std_ratio AS FLOAT64) AS std_ratio,
  SAFE_CAST(b.rsi_14 AS FLOAT64) AS rsi_14,
  SAFE_CAST(b.macd_diff AS FLOAT64) AS macd_diff,
  SAFE_CAST(b.macd_signal AS FLOAT64) AS macd_signal,
  SAFE_CAST(b.macd_hist AS FLOAT64) AS macd_hist,
  SAFE_CAST(b.close_to_high_20d AS FLOAT64) AS close_to_high_20d,
  SAFE_CAST(b.close_to_ma5 AS FLOAT64) AS close_to_ma5,
  SAFE_CAST(b.close_to_ma20 AS FLOAT64) AS close_to_ma20"""

        joins = ""
        enhanced_cols = ""
        if feature_set == "enhanced":
            enhanced_cols = """,
  SAFE_CAST(f.pe_basic AS FLOAT64) AS pe_basic,
  SAFE_CAST(f.pb AS FLOAT64) AS pb,
  SAFE_CAST(f.roe AS FLOAT64) AS roe,
  COALESCE(SAFE_CAST(f.gross_margin_from_income AS FLOAT64), SAFE_CAST(f.gross_margin AS FLOAT64)) AS gross_margin,
  COALESCE(SAFE_CAST(f.net_margin_from_income AS FLOAT64), SAFE_CAST(f.net_margin AS FLOAT64)) AS net_margin,
  COALESCE(SAFE_CAST(f.debt_to_assets_from_balance AS FLOAT64), SAFE_CAST(f.debt_to_assets AS FLOAT64)) AS debt_to_assets,
  COALESCE(SAFE_CAST(f.current_ratio_from_balance AS FLOAT64), SAFE_CAST(f.current_ratio AS FLOAT64)) AS current_ratio,
  COALESCE(SAFE_CAST(f.asset_turnover_from_income_balance AS FLOAT64), SAFE_CAST(f.asset_turnover AS FLOAT64)) AS asset_turnover,
  LOG(GREATEST(COALESCE(SAFE_CAST(f.market_cap AS FLOAT64), 0), 1)) AS market_cap_log,
  SAFE_DIVIDE(SAFE_CAST(e.net_inflow_amount AS FLOAT64), NULLIF(SAFE_CAST(b.amount AS FLOAT64), 0)) AS net_inflow_to_amount,
  SAFE_DIVIDE(SAFE_CAST(e.main_net_inflow_amount AS FLOAT64), NULLIF(SAFE_CAST(b.amount AS FLOAT64), 0)) AS main_net_inflow_to_amount,
  SAFE_DIVIDE(SAFE_CAST(e.dragon_tiger_net_amount AS FLOAT64), NULLIF(SAFE_CAST(b.amount AS FLOAT64), 0)) AS dragon_tiger_net_to_amount,
  SAFE_CAST(e.dragon_tiger_department_count AS FLOAT64) AS dragon_tiger_department_count,
  COALESCE(SAFE_CAST(e.limit_up_streak AS FLOAT64), 0) AS limit_up_streak,
  CASE WHEN e.is_kpl_event THEN 1.0 ELSE 0.0 END AS is_kpl_event"""
            joins = f"""
LEFT JOIN `{self.project_id}.{self.dataset}.{fundamental}` AS f
  ON b.equity_code = f.equity_code AND b.date = f.date
LEFT JOIN `{self.project_id}.{self.dataset}.{event}` AS e
  ON b.equity_code = e.equity_code AND b.date = e.date"""

        return f"""SELECT
{base_cols}{enhanced_cols}
FROM `{self.project_id}.{self.dataset}.{daily}` AS b
{joins}
WHERE {where_sql}
"""

    def get_equity_feature_snapshot(
        self,
        codes: List[str],
        date: str,
        feature_set: str = "enhanced",
    ) -> pd.DataFrame:
        """读取某个交易日的股票 DWS 特征快照。

        该方法供策略层可选调用，不属于 ``BaseDataSource`` 抽象接口。
        """
        if feature_set not in {"technical", "enhanced"}:
            raise ValueError("feature_set must be 'technical' or 'enhanced'")
        if not codes:
            return pd.DataFrame()
        date_literal = self._to_date_literal(date)
        yyyymmdd = self._to_yyyymmdd(date)
        code_list = self._sql_string_list(codes)
        where_sql = (
            f"b.partition_month = {yyyymmdd[:6]} "
            f"AND b.date = DATE '{date_literal}' "
            f"AND b.equity_code IN ({code_list})"
        )
        return self._execute_sql(self._equity_feature_join_sql(feature_set, where_sql))

    def get_equity_feature_history(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        feature_set: str = "enhanced",
        label_horizon: int = 5,
    ) -> pd.DataFrame:
        """读取股票 DWS 训练特征，并生成未来 horizon 日收益标签。

        标签仅供训练使用；回测策略不得读取 ``label_return``。
        """
        if feature_set not in {"technical", "enhanced"}:
            raise ValueError("feature_set must be 'technical' or 'enhanced'")
        if not codes:
            return pd.DataFrame()
        start = self._to_yyyymmdd(start_date)
        end = self._to_yyyymmdd(end_date)
        start_literal = self._to_date_literal(start)
        end_literal = self._to_date_literal(end)
        extended_end = (
            datetime.strptime(end, "%Y%m%d")
            + timedelta(days=max(label_horizon * 4 + 20, 30))
        ).strftime("%Y%m%d")
        extended_literal = self._to_date_literal(extended_end)
        partition_months = self._partition_months_in_range(start, extended_end)
        pm_list = ", ".join(str(pm) for pm in partition_months)
        code_list = self._sql_string_list(codes)
        where_sql = (
            f"b.partition_month IN ({pm_list}) "
            f"AND b.date >= DATE '{start_literal}' "
            f"AND b.date <= DATE '{extended_literal}' "
            f"AND b.equity_code IN ({code_list})"
        )
        feature_sql = self._equity_feature_join_sql(feature_set, where_sql)
        sql = f"""WITH features AS (
{feature_sql}
),
labeled AS (
  SELECT
    *,
    LOG(LEAD(close, {int(label_horizon)}) OVER (
      PARTITION BY equity_code ORDER BY date
    )) - LOG(close) AS label_return
  FROM features
  WHERE close IS NOT NULL AND close > 0
)
SELECT *
FROM labeled
WHERE date >= '{start}' AND date <= '{end}'
        """
        return self._execute_sql(sql)

    def get_bqml_signal_candidates(
        self,
        start_date: str = "",
        end_date: str = "",
        table_name: str = "ads_signal_ml_stock_picker_bqml_1d",
        candidate_pool_size: int = 10,
    ) -> pd.DataFrame:
        """读取 BQML ADS 候选信号，供真实撮合回测策略使用。

        该方法只返回信号本身，不返回未来收益或标签。
        """
        candidate_pool_size = max(int(candidate_pool_size), 1)
        table = table_name.strip() or self._optional_table(
            "ads_signal_ml_stock_picker_bqml_1d",
            "ads_signal_ml_stock_picker_bqml_1d",
        )
        dim_table = self._require_table("dim_security")
        where = [
            "a.is_selected",
            f"a.score_rank <= {candidate_pool_size}",
        ]
        if start_date:
            where.append(f"a.date >= DATE '{self._to_date_literal(start_date)}'")
        if end_date:
            where.append(f"a.date <= DATE '{self._to_date_literal(end_date)}'")
        where.append(self._security_permission_filter_sql("s"))
        where_sql = " AND ".join(where)
        sql = f"""
SELECT
  FORMAT_DATE('%Y%m%d', a.date) AS date,
  a.equity_code,
  SAFE_CAST(a.prob_up AS FLOAT64) AS prob_up,
  SAFE_CAST(a.score_rank AS INT64) AS score_rank
FROM `{self.project_id}.{self.dataset}.{table}` AS a
LEFT JOIN `{self.project_id}.{self.dataset}.{dim_table}` AS s
  ON a.equity_code = s.security_code
WHERE {where_sql}
ORDER BY a.date, a.score_rank, a.equity_code
"""
        return self._execute_sql(sql)

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
        # ── 历史 bug 修复（由 PRD_20260524_12 / commit a5eecdd 引入；
        #    与其他 agent 在 PRD_20260524_11 同位置的修复合并）──
        # BigQuery 表中 OHLC 等数值列在 BQ 端为 NUMERIC 类型，
        # google-cloud-bigquery 反序列化为 Python `decimal.Decimal`。
        # 而下游所有 numpy / pandas 数值计算（如 np.log、Series.diff、rolling.mean）
        # 在遇到 Decimal 时会抛 TypeError 或回退到 object dtype，
        # 导致任何 BQ-backed 的日频回测 / ML 策略都跑不通。
        # 统一在数据源出口把这几列强转 float，避免每个策略各自处理。
        # 关联：strategy/ml_multi_horizon_picker/, strategy/ml_stock_picker/ 同样受益。
        for col in ("open", "high", "low", "close", "volume", "amount"):
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
        return df[["code", "date", "open", "high", "low", "close", "volume", "amount"]].copy()

    def _fetch_daily_equity_bars_batch(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        adjust: str,
    ) -> Dict[str, pd.DataFrame]:
        table = self._require_table("kline_1d_equity")
        partition_months = self._partition_months_in_range(start_date, end_date)
        if not partition_months:
            return {}
        code_values = sorted(set(self._filter_codes_by_permissions(codes)))
        if not code_values:
            return {}

        pm_list = ", ".join(str(pm) for pm in partition_months)
        code_list = self._sql_string_list(code_values)
        sd = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
        ed = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"
        adjust_type = adjust if adjust in ("qfq", "hfq") else "none"
        sql = f"""
SELECT equity_code AS code, date, open, high, low, close, volume, amount
FROM `{self.project_id}.{self.dataset}.{table}`
WHERE equity_code IN ({code_list})
  AND adjust_type = '{adjust_type}'
  AND partition_month IN ({pm_list})
  AND date >= DATE '{sd}'
  AND date <= DATE '{ed}'
ORDER BY equity_code, date
"""
        df = self._execute_sql(sql)
        if df.empty:
            return {}
        df["date"] = pd.to_datetime(df["date"], errors="coerce").dt.strftime("%Y%m%d")
        df = df.dropna(subset=["date"])
        for col in ["open", "high", "low", "close", "volume", "amount"]:
            if col in df.columns:
                df[col] = pd.to_numeric(df[col], errors="coerce")
        result: Dict[str, pd.DataFrame] = {}
        for code, group in df.groupby("code", sort=False):
            result[str(code)] = group[
                ["code", "date", "open", "high", "low", "close", "volume", "amount"]
            ].reset_index(drop=True)
        return result

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
