"""阿里云 MaxCompute (ODPS) 数据源实现。

本模块通过 pyodps 连接阿里云 MaxCompute，从 `a_share_historical_data` 项目
拉取 A 股历史行情。首次查询后写入本地 Parquet 缓存（复用 LocalStorage），
后续命中本地缓存即跳过 MaxCompute 查询，节省 SQL 计费成本。

当前接入进度（详见 ARCHITECTURE.md §4.7 / §4.8 / §4.9 与 PRD_20260519_02/03）：
  - ✅ 5min K 线：cn_stock_kline_5min 表已接入
  - ✅ 15min ETF K 线：cn_etf_kline_15min 表已接入（510300.SH 等沪深300 ETF）
  - ✅ 股票列表：从 5min 表 DISTINCT code, name 派生
  - ⚠️  指数成分股：暂返回空列表（warning），待建表后接入
  - ⚠️  复权：接口预留（adjust=qfq/hfq 时打 warning，返回原始价），待复权因子表接入
  - ❌ 日K / 1min / 30min / 60min：对应表尚未建立，调用时抛 NotImplementedError
  - ❌ 普通股票 15min K（kline_15min）：表尚未建立

代码格式约定：
  - 框架对外：`XXXXXX.SH` / `XXXXXX.SZ`（上交所 / 深交所）
  - 表内存储：`shXXXXXX` / `szXXXXXX`（小写前缀 + 裸代码）
  - 两者由 _to_exchange_code / _to_framework_code 双向映射，对上层透明

分区裁剪：
  - cn_stock_kline_5min 按 year_month STRING(YYYYMM) 分区
  - 查询时根据 [start_date, end_date] 计算覆盖的 year_month 列表，
    SQL 中用 `year_month IN (...)` 触发分区裁剪，避免全表扫描
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd

from .base_data_source import BaseDataSource
from .local_storage import LocalStorage
from utils.logger import get_logger

logger = get_logger(__name__)


# 上交所代码前缀（沪市主板 / 科创板 / ETF / LOF / 转债）
_SH_PREFIXES = ("60", "68", "51", "56", "58", "11")
# 深交所代码前缀（深市主板 / 创业板 / ETF / LOF）
_SZ_PREFIXES = ("00", "30", "15", "16")


class MaxComputeDataSource(BaseDataSource):
    """基于阿里云 MaxCompute 的 A 股数据源。

    Args:
        access_id: 阿里云 AccessKey ID。
        access_key: 阿里云 AccessKey Secret。
        project: MaxCompute 项目名，如 "a_share_historical_data"。
        endpoint: MaxCompute Endpoint URL（按区域不同）。
        storage: 本地缓存存储器。
        use_cache: 是否启用本地缓存。
        cache_retention_days: 缓存保留天数。
        cache_max_size_gb: 缓存目录最大体积（GB）。
        tables: 表名映射，键含 kline_5min / kline_etf_15min / adjust_factor /
            daily / kline_1min / kline_15min / kline_30min / kline_60min /
            stock_info / index_constituent。
    """

    def __init__(
        self,
        access_id: str,
        access_key: str,
        project: str,
        endpoint: str,
        storage: Optional[LocalStorage] = None,
        use_cache: bool = True,
        cache_retention_days: int = 7,
        cache_max_size_gb: float = 1.0,
        tables: Optional[Dict[str, str]] = None,
    ) -> None:
        self._access_id = access_id
        self._access_key = access_key
        self.project = project
        self.endpoint = endpoint
        self.storage = storage or LocalStorage()
        self.use_cache = use_cache
        self.cache_retention_days = cache_retention_days
        self.cache_max_size_gb = cache_max_size_gb
        self.tables = tables or {}
        self._odps = None

        if self.use_cache:
            self._cleanup_cache()

    # ------------------------------------------------------------------ #
    # 连接与缓存
    # ------------------------------------------------------------------ #

    def _get_odps(self):
        if self._odps is None:
            try:
                from odps import ODPS
            except ImportError as e:
                raise ImportError("未安装 pyodps，请先执行 `pip install pyodps`。") from e
            self._odps = ODPS(
                access_id=self._access_id,
                secret_access_key=self._access_key,
                project=self.project,
                endpoint=self.endpoint,
            )
            logger.info(f"MaxCompute 连接已建立: project={self.project}, endpoint={self.endpoint}")
        return self._odps

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
            logger.info(f"缓存清理：删除 {expired} 个超期文件（>{self.cache_retention_days}天）")

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
                    f"缓存清理：删除 {removed} 个最旧文件以满足 {self.cache_max_size_gb}GB 上限"
                )

    def _require_table(self, key: str) -> str:
        """读取并校验表名配置，未配置则抛出明确错误。"""
        name = self.tables.get(key, "").strip()
        if not name:
            raise NotImplementedError(
                f"对应表尚未建立或未配置：config/backtest.yaml 中 "
                f"data.maxcompute.tables.{key} 为空。\n"
                f"如该表已存在请填入表名；如尚未建立请等待数据准备完成后再使用。"
            )
        return name

    # ------------------------------------------------------------------ #
    # 代码格式映射 / 分区列表 / 复权占位
    # ------------------------------------------------------------------ #

    def _to_exchange_code(self, framework_code: str) -> str:
        """框架代码 → 表代码：`600000.SH` → `sh600000`、`000001.SZ` → `sz000001`。

        - 已是表格式（小写前缀）保持原样（幂等）。
        - 裸代码按前缀推断；无法识别则默认 `sh` + WARNING。
        """
        if not framework_code:
            raise ValueError("股票代码为空")
        code = framework_code.strip()
        lower = code.lower()

        if lower.startswith(("sh", "sz")):
            return lower

        if "." in code:
            bare, _, suffix = code.partition(".")
            suffix = suffix.upper()
            if suffix == "SH":
                return f"sh{bare}"
            if suffix == "SZ":
                return f"sz{bare}"
            logger.warning(f"未知交易所后缀 {suffix}（code={framework_code}），默认按 sh 处理")
            return f"sh{bare}"

        bare = code
        if bare.startswith(_SH_PREFIXES):
            logger.info(f"裸代码 {bare} 按前缀推断为沪市 → sh{bare}")
            return f"sh{bare}"
        if bare.startswith(_SZ_PREFIXES):
            logger.info(f"裸代码 {bare} 按前缀推断为深市 → sz{bare}")
            return f"sz{bare}"

        logger.warning(f"无法识别股票代码 {framework_code} 的交易所，默认按 sh 处理")
        return f"sh{bare}"

    def _to_framework_code(self, exchange_code: str) -> str:
        """表代码 → 框架代码：`sh600000` → `600000.SH`、`sz000001` → `000001.SZ`。"""
        if not exchange_code:
            return exchange_code
        code = exchange_code.strip().lower()
        if code.startswith("sh"):
            return f"{code[2:]}.SH"
        if code.startswith("sz"):
            return f"{code[2:]}.SZ"
        logger.warning(f"表中存在异常前缀代码 {exchange_code!r}，原样返回")
        return exchange_code

    def _year_months_in_range(self, start_date: str, end_date: str) -> List[str]:
        """根据 [YYYYMMDD, YYYYMMDD] 返回覆盖的 year_month 列表（YYYYMM 字符串）。

        示例：("20000609", "20020815") → ["200006", "200007", ..., "200208"]
        """
        if len(start_date) < 6 or len(end_date) < 6:
            raise ValueError(f"日期格式应为 YYYYMMDD：start={start_date}, end={end_date}")
        y, m = int(start_date[:4]), int(start_date[4:6])
        end_y, end_m = int(end_date[:4]), int(end_date[4:6])
        if (y, m) > (end_y, end_m):
            return []
        result: List[str] = []
        while (y, m) <= (end_y, end_m):
            result.append(f"{y:04d}{m:02d}")
            m += 1
            if m > 12:
                y += 1
                m = 1
        return result

    # ------------------------------------------------------------------ #
    # 复权支持
    # ------------------------------------------------------------------ #

    def _adj_cache_path(self, norm_code: str) -> Path:
        """复权因子本地缓存路径：data/raw/daily/{code}_adj.parquet。"""
        return self._cache_root() / "daily" / f"{norm_code}_adj.parquet"

    def _fetch_adjust_factors(
        self, code: str, start_date: str, end_date: str
    ) -> pd.DataFrame:
        """从 cn_etf_adj_factor 拉取复权因子，返回 DataFrame[date(YYYYMMDD), adj_factor]。

        表结构假设（基于阿里云 MaxCompute ETF 复权因子表惯例）：
          code STRING, trade_date STRING, adj_factor DOUBLE, year_month STRING(partition)
        adj_factor 为累计后复权因子（上市日=1.0，随分红/拆股增大）。
        """
        norm_code = code.split(".")[0]

        # ── 本地缓存优先 ──
        if self.use_cache:
            adj_path = self._adj_cache_path(norm_code)
            if adj_path.exists():
                try:
                    cached = pd.read_parquet(adj_path)
                    if not cached.empty and "date" in cached.columns:
                        cmin = str(cached["date"].min())
                        cmax = str(cached["date"].max())
                        if cmin <= str(start_date) and cmax >= str(end_date):
                            mask = (cached["date"] >= str(start_date)) & (
                                cached["date"] <= str(end_date)
                            )
                            return cached.loc[mask].copy().reset_index(drop=True)
                except Exception as e:
                    logger.warning(f"复权因子缓存读取失败: {e}")

        # ── MaxCompute 查询 ──
        table = self._require_table("adjust_factor")
        year_months = self._year_months_in_range(start_date, end_date)
        if not year_months:
            return pd.DataFrame()

        ym_list = ", ".join(f"'{ym}'" for ym in year_months)
        sd = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
        ed = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"
        sql = (
            f"SELECT code, trade_date, adj_factor\n"
            f"FROM {table}\n"
            f"WHERE code = '{code}'\n"
            f"  AND year_month IN ({ym_list})\n"
            f"  AND trade_date >= '{sd}'\n"
            f"  AND trade_date <= '{ed}'\n"
            f"ORDER BY trade_date"
        )
        df = self._execute_sql(sql)
        if df.empty:
            return df

        df["date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")
        df["adj_factor"] = df["adj_factor"].astype(float)
        result = df[["date", "adj_factor"]].copy().sort_values("date").reset_index(drop=True)

        # ── 写缓存 ──
        if self.use_cache:
            try:
                adj_path = self._adj_cache_path(norm_code)
                adj_path.parent.mkdir(parents=True, exist_ok=True)
                if adj_path.exists():
                    existing = pd.read_parquet(adj_path)
                    merged = pd.concat([existing, result], ignore_index=True)
                    merged = merged.drop_duplicates(subset=["date"], keep="last")
                    merged = merged.sort_values("date").reset_index(drop=True)
                    merged.to_parquet(adj_path, index=False)
                else:
                    result.to_parquet(adj_path, index=False)
            except Exception as e:
                logger.warning(f"复权因子缓存写入失败: {e}")

        return result

    def _apply_adjust(
        self, df: pd.DataFrame, code: str, adjust: Optional[str]
    ) -> pd.DataFrame:
        """前/后复权：从 cn_etf_adj_factor 获取因子并调整 OHLC 列。

        前复权（qfq）：调整历史价格，使最新收盘价与原始价一致。
            adj_price = raw_price * adj_factor_on_date / adj_factor_latest

        后复权（hfq）：以上市日价格为基准，历史价向上调整。
            adj_price = raw_price * adj_factor_on_date
        """
        if adjust not in ("qfq", "hfq") or df.empty:
            return df

        raw_dates = df["date"].astype(str)
        first_date = raw_dates.iloc[0]
        is_minute = len(first_date) == 12  # YYYYMMDDHHMM vs YYYYMMDD

        if is_minute:
            day_series = raw_dates.str[:8]
        else:
            day_series = raw_dates

        start_day = day_series.min()
        end_day = day_series.max()

        try:
            adj_df = self._fetch_adjust_factors(code, start_day, end_day)
        except NotImplementedError:
            logger.warning(
                f"复权因子表未配置（adjust_factor 为空），code={code} adjust={adjust}，"
                f"返回原始价"
            )
            return df
        except Exception as e:
            logger.warning(f"复权因子获取失败，使用原始价: code={code}, error={e}")
            return df

        if adj_df.empty:
            logger.warning(f"未获取到复权因子数据，使用原始价: code={code} adjust={adjust}")
            return df

        adj_df = adj_df.sort_values("date").reset_index(drop=True)
        latest_factor = float(adj_df["adj_factor"].iloc[-1])

        temp = df.copy()
        temp["_day"] = day_series.values
        temp = temp.merge(
            adj_df.rename(columns={"date": "_day"}),
            on="_day",
            how="left",
        )
        # 节假日可能无因子：前向填充后后向填充
        temp["adj_factor"] = temp["adj_factor"].ffill().bfill().astype(float)

        if adjust == "qfq":
            ratio = temp["adj_factor"] / latest_factor
        else:  # hfq
            ratio = temp["adj_factor"]

        for col in ("open", "high", "low", "close"):
            if col in temp.columns:
                temp[col] = (temp[col] * ratio).round(4)

        logger.info(
            f"复权完成: code={code} adjust={adjust} bars={len(temp)} "
            f"latest_factor={latest_factor:.6f}"
        )
        return temp.drop(columns=["_day", "adj_factor"]).reset_index(drop=True)

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

        # 1) 本地缓存优先（缓存存储原始价，复权在读取后即时计算）
        if self.use_cache:
            cached_full = self.storage.load_bars_raw(norm_code, period=period)
            if not cached_full.empty and "date" in cached_full.columns:
                cmin = str(cached_full["date"].min())
                cmax = str(cached_full["date"].max())
                if cmin <= str(start_date) and cmax >= str(end_date):
                    df = self.storage.load_bars(
                        norm_code, start_date, end_date, period=period
                    )
                    return self._apply_adjust(df, code, adjust)

        # 2) 路由（拉取原始价，不传 adjust）
        if period == "daily":
            df = self._fetch_daily_bars(code, start_date, end_date, adjust)
        elif period in ("1min", "5min", "15min", "30min", "60min"):
            df = self._fetch_minute_bars(code, start_date, end_date, period, adjust)
        else:
            raise ValueError(f"不支持的 period: {period}")

        if df.empty:
            return df

        # 3) 写缓存（存原始价，复权因子另存）
        if self.use_cache:
            existing = self.storage.load_bars_raw(norm_code, period=period)
            if not existing.empty:
                combined = pd.concat([existing, df], ignore_index=True)
                combined = combined.drop_duplicates(subset=["date"], keep="last")
                combined = combined.sort_values("date").reset_index(drop=True)
                self.storage.save_bars(norm_code, combined, period=period)
            else:
                self.storage.save_bars(norm_code, df, period=period)

        # 4) 按请求区间过滤，施加复权，返回
        mask = (df["date"] >= str(start_date)) & (df["date"] <= str(end_date) + "9999")
        df_filtered = df.loc[mask].copy()
        return self._apply_adjust(df_filtered, code, adjust)

    def get_stock_list(self) -> pd.DataFrame:
        """从 5min 表派生股票列表（SELECT DISTINCT code, name）。

        返回列：[code, name, list_date, industry]
        其中 list_date / industry 字段为空字符串（5min 表未提供）。
        """
        cached = self.storage.load_stock_list()
        if not cached.empty:
            return cached

        table = self._require_table("kline_5min")
        sql = f"SELECT DISTINCT code, name FROM {table}"
        df = self._execute_sql(sql)

        if df.empty:
            logger.warning("get_stock_list: MaxCompute 返回空结果")
            return df

        df["code"] = df["code"].apply(self._to_framework_code)
        df["list_date"] = ""
        df["industry"] = ""
        df = df[["code", "name", "list_date", "industry"]].copy()

        self.storage.save_stock_list(df)
        return df

    def get_index_constituents(self, index_code: str) -> List[str]:
        """指数成分股暂未接入。返回空列表 + WARNING。"""
        logger.warning(
            f"MaxCompute 数据源暂未接入指数成分股表（tables.index_constituent 待补充），"
            f"返回空列表（请求 index_code={index_code}）"
        )
        return []

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
        """日K查询。当前日K表尚未建立。"""
        # _require_table 会抛 NotImplementedError 并附带提示
        self._require_table("daily")
        # 上一行如未抛错（即配置了表名），落到这里仍未实现 SQL 拼接
        raise NotImplementedError(
            "日K表 (tables.daily) 已配置但 SQL 实现尚未补充。"
        )  # pragma: no cover

    def _fetch_minute_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        period: str,
        adjust: str,
    ) -> pd.DataFrame:
        """分钟K查询。当前支持：5min（普通股票）、15min（ETF）。

        代码格式差异：
          - cn_stock_kline_5min：表内格式 shXXXXXX，需 _to_exchange_code / _to_framework_code 双向映射
          - cn_etf_kline_15min：表内格式已是框架格式（XXXXXX.SH），无需映射
        """
        if period == "5min":
            return self._fetch_5min_bars(code, start_date, end_date)
        elif period == "15min":
            return self._fetch_etf_15min_bars(code, start_date, end_date)
        else:
            tables_key = f"kline_{period}"
            raise NotImplementedError(
                f"MaxCompute 当前仅接入 5min（普通股票）与 15min（ETF）K 线。\n"
                f"period={period} 对应的表 (data.maxcompute.tables.{tables_key}) 尚未建立。\n"
                f"请等待对应表建立后再使用。"
            )

    def _fetch_5min_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """从 cn_stock_kline_5min 拉取 5min K 线。

        代码格式：表内 shXXXXXX/szXXXXXX，需双向映射。
        """
        table = self._require_table("kline_5min")
        exchange_code = self._to_exchange_code(code)
        year_months = self._year_months_in_range(start_date, end_date)
        if not year_months:
            return pd.DataFrame()

        ym_list = ", ".join(f"'{ym}'" for ym in year_months)
        sql = (
            f"SELECT code, trade_time, open, high, low, close, volume, amount\n"
            f"FROM {table}\n"
            f"WHERE code = '{exchange_code}'\n"
            f"  AND year_month IN ({ym_list})\n"
            f"  AND trade_time >= '{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]} 00:00:00'\n"
            f"  AND trade_time <= '{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]} 23:59:59'\n"
            f"ORDER BY trade_time"
        )

        df = self._execute_sql(sql)
        if df.empty:
            return df

        df["code"] = df["code"].apply(self._to_framework_code)
        df["date"] = pd.to_datetime(df["trade_time"]).dt.strftime("%Y%m%d%H%M")
        return df[["code", "date", "open", "high", "low", "close", "volume", "amount"]].copy()

    def _fetch_etf_15min_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
    ) -> pd.DataFrame:
        """从 cn_etf_kline_15min 拉取 ETF 15min K 线。

        代码格式：表内已是框架格式（XXXXXX.SH / XXXXXX.SZ），无需映射。
        全表扫描被禁止，必须携带 year_month 分区条件。
        """
        table = self._require_table("kline_etf_15min")
        year_months = self._year_months_in_range(start_date, end_date)
        if not year_months:
            return pd.DataFrame()

        # 表内代码已是框架格式，直接使用
        ym_list = ", ".join(f"'{ym}'" for ym in year_months)
        sql = (
            f"SELECT code, trade_time, open, high, low, close, volume, amount\n"
            f"FROM {table}\n"
            f"WHERE code = '{code}'\n"
            f"  AND year_month IN ({ym_list})\n"
            f"  AND trade_time >= '{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]} 00:00:00'\n"
            f"  AND trade_time <= '{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]} 23:59:59'\n"
            f"ORDER BY trade_time"
        )

        df = self._execute_sql(sql)
        if df.empty:
            return df

        # 代码列原样保留（已是框架格式）
        df["date"] = pd.to_datetime(df["trade_time"]).dt.strftime("%Y%m%d%H%M")
        return df[["code", "date", "open", "high", "low", "close", "volume", "amount"]].copy()

    def _execute_sql(self, sql: str, max_retries: int = 3) -> pd.DataFrame:
        """执行 SQL 并返回 DataFrame，自动指数退避重试网络故障。"""
        odps = self._get_odps()
        logger.debug(f"MaxCompute SQL:\n{sql}")
        last_exc: Exception = RuntimeError("未知错误")
        for attempt in range(max_retries):
            try:
                with odps.execute_sql(sql).open_reader(tunnel=True) as reader:
                    return reader.to_pandas()
            except Exception as e:
                last_exc = e
                if attempt < max_retries - 1:
                    wait = 2 ** attempt  # 1s → 2s → 4s
                    logger.warning(
                        f"MaxCompute SQL 执行失败（第 {attempt + 1} 次），{wait}s 后重试: {e}"
                    )
                    time.sleep(wait)
        raise last_exc
