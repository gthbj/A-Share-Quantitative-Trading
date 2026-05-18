"""阿里云 MaxCompute (ODPS) 数据源实现。

本模块通过 pyodps 连接阿里云 MaxCompute，从用户的 `a_share_historical_data`
项目中拉取 A 股历史行情。首次查询后写入本地 Parquet 缓存（复用 LocalStorage），
后续命中本地缓存即跳过 MaxCompute 查询，节省 SQL 计费成本。

⚠️ 当前状态：连接与缓存框架已就绪，但 SQL 查询逻辑待补充。
   项目 a_share_historical_data 的具体表结构尚未确认，下列方法在被调用时会抛出
   NotImplementedError，并提示需要补充的字段。表结构确认后，按 _build_*_sql 中的
   TODO 注释填充 SQL 即可。

待补充的表结构信息（参见 PRD/ARCHITECTURE.md 或 commit 记录）：
  1. 日K表
     - 表名（config: data.maxcompute.tables.daily）
     - 字段：股票代码、交易日期、开/高/低/收、成交量、成交额
     - 代码格式：带交易所后缀（000001.SZ）还是裸代码（000001）
     - 日期类型：STRING(YYYYMMDD) / DATE / DATETIME
     - 分区方式：是否按 ds / pt 分区，分区字段名
     - 复权：独立表（daily_qfq / daily_hfq）还是单字段区分
  2. 分钟K表（可选）
     - 表名、period 字段、时间戳格式
  3. 股票基础信息表
     - 表名、字段（code/name/list_date/industry）
  4. 指数成分股表
     - 表名、字段（index_code/code）
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


class MaxComputeDataSource(BaseDataSource):
    """基于阿里云 MaxCompute 的 A 股数据源。

    Args:
        access_id: 阿里云 AccessKey ID。
        access_key: 阿里云 AccessKey Secret。
        project: MaxCompute 项目名，如 "a_share_historical_data"。
        endpoint: MaxCompute Endpoint URL（按区域不同），北京区为
            "http://service.cn-beijing.maxcompute.aliyun.com/api"。
        storage: 本地缓存存储器，默认新建 LocalStorage。
        use_cache: 是否启用本地缓存。
        cache_retention_days: 缓存保留天数，超过自动清理。
        cache_max_size_gb: 缓存目录最大体积（GB）。
        tables: 表名映射，键为 daily / minute / stock_info / index_constituent。
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
        self._odps = None  # 懒加载

        if self.use_cache:
            self._cleanup_cache()

    # ------------------------------------------------------------------ #
    # 连接与缓存管理
    # ------------------------------------------------------------------ #

    def _get_odps(self):
        """懒加载 ODPS 连接对象。"""
        if self._odps is None:
            try:
                from odps import ODPS
            except ImportError as e:
                raise ImportError(
                    "未安装 pyodps，请先执行 `pip install pyodps`。"
                ) from e
            self._odps = ODPS(
                access_id=self._access_id,
                secret_access_key=self._access_key,
                project=self.project,
                endpoint=self.endpoint,
            )
            logger.info(f"MaxCompute 连接已建立: project={self.project}, endpoint={self.endpoint}")
        return self._odps

    def _cache_root(self) -> Path:
        """缓存根目录（与 LocalStorage 一致）。"""
        # LocalStorage 默认目录是 data/raw，此处通过其属性获取以保持一致
        for attr in ("cache_dir", "root", "base_dir"):
            if hasattr(self.storage, attr):
                return Path(getattr(self.storage, attr))
        return Path("data/raw")

    def _cleanup_cache(self) -> None:
        """根据保留策略清理本地缓存。

        策略：
          1. 按修改时间清理超过 cache_retention_days 的文件。
          2. 若清理后总大小仍超过 cache_max_size_gb，按 mtime 升序继续删除最旧文件
             直至总大小回到阈值内。
        """
        cache_dir = self._cache_root()
        if not cache_dir.exists():
            return

        now = time.time()
        retention_seconds = self.cache_retention_days * 86400
        max_size_bytes = int(self.cache_max_size_gb * (1024 ** 3))

        # 收集缓存文件
        entries = []
        for f in cache_dir.rglob("*"):
            if not f.is_file():
                continue
            try:
                st = f.stat()
                entries.append((f, st.st_mtime, st.st_size))
            except OSError:
                continue

        # 1) 按 retention 清理过期文件
        kept = []
        expired_count = 0
        for f, mtime, size in entries:
            if now - mtime > retention_seconds:
                try:
                    f.unlink()
                    expired_count += 1
                except OSError:
                    kept.append((f, mtime, size))
            else:
                kept.append((f, mtime, size))
        if expired_count:
            logger.info(f"缓存清理：删除 {expired_count} 个超期文件（>{self.cache_retention_days}天）")

        # 2) 按容量清理（升序，先删最旧）
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
                f"MaxCompute 表名未配置：config/backtest.yaml 中 data.maxcompute.tables.{key} 为空。\n"
                f"请补充 {self.project} 项目实际的表名与字段结构后再运行。"
            )
        return name

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
        """获取K线数据。

        流程：
          1. 优先读本地缓存；命中且完全覆盖 [start_date, end_date] 即直接返回。
          2. 未命中则向 MaxCompute 发送 SQL 查询。
          3. 查询结果标准化为框架统一列 [code, date, open, high, low, close, volume, amount]
             并写入本地缓存。

        ⚠️ 第 2 步的 SQL 构造逻辑待补充，详见模块顶部 TODO。
        """
        norm_code = code.split(".")[0]

        # 1) 本地缓存优先
        if self.use_cache:
            cached_full = self.storage.load_bars_raw(norm_code, period=period)
            if not cached_full.empty and "date" in cached_full.columns:
                cmin = str(cached_full["date"].min())
                cmax = str(cached_full["date"].max())
                if cmin <= str(start_date) and cmax >= str(end_date):
                    return self.storage.load_bars(norm_code, start_date, end_date, period=period)

        # 2) 走 MaxCompute
        if period == "daily":
            df = self._fetch_daily_bars(code, start_date, end_date, adjust)
        else:
            df = self._fetch_minute_bars(code, start_date, end_date, period, adjust)

        if df.empty:
            return df

        # 3) 写缓存（合并已有缓存，去重）
        if self.use_cache:
            existing = self.storage.load_bars_raw(norm_code, period=period)
            if not existing.empty:
                combined = pd.concat([existing, df], ignore_index=True)
                combined = combined.drop_duplicates(subset=["date"], keep="last")
                combined = combined.sort_values("date").reset_index(drop=True)
                self.storage.save_bars(norm_code, combined, period=period)
            else:
                self.storage.save_bars(norm_code, df, period=period)

        mask = (df["date"] >= str(start_date)) & (df["date"] <= str(end_date))
        return df.loc[mask].copy()

    def get_stock_list(self) -> pd.DataFrame:
        """获取股票基础信息列表。

        ⚠️ SQL 待补充：需要 stock_info 表名与字段（code/name/list_date/industry）。
        """
        cached = self.storage.load_stock_list()
        if not cached.empty:
            return cached

        table = self._require_table("stock_info")
        raise NotImplementedError(
            f"MaxCompute get_stock_list 待补充实现。\n"
            f"已配置表名：{table}\n"
            f"请补充该表字段映射（code / name / list_date / industry）后填充 SQL。"
        )

    def get_index_constituents(self, index_code: str) -> List[str]:
        """获取指数成分股列表。

        ⚠️ SQL 待补充：需要 index_constituent 表名与字段（index_code / code）。
        """
        table = self._require_table("index_constituent")
        raise NotImplementedError(
            f"MaxCompute get_index_constituents 待补充实现。\n"
            f"已配置表名：{table}\n"
            f"请补充字段映射（index_code / code）后填充 SQL。"
        )

    # ------------------------------------------------------------------ #
    # SQL 构造与执行（待表结构确认后补充）
    # ------------------------------------------------------------------ #

    def _fetch_daily_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str,
    ) -> pd.DataFrame:
        """从 MaxCompute 拉取日K数据。

        TODO 待表结构确认后实现：
          - 根据 adjust 选择对应表（如 daily_qfq / daily_hfq）或加 WHERE 条件
          - 处理代码格式（带后缀 / 裸代码）
          - 处理分区字段（如 ds）以利用分区裁剪
          - 字段映射到框架标准列：[code, date, open, high, low, close, volume, amount]

        预期 SQL 模板（示例，需根据实际字段名调整）：
            SELECT
                <code_col>   AS code,
                <date_col>   AS date,
                <open_col>   AS open,
                <high_col>   AS high,
                <low_col>    AS low,
                <close_col>  AS close,
                <volume_col> AS volume,
                <amount_col> AS amount
            FROM <daily_table>
            WHERE <code_col> = '<code>'
              AND <date_col> BETWEEN '<start>' AND '<end>'
              AND <adjust_filter>
            ORDER BY <date_col>
        """
        table = self._require_table("daily")
        raise NotImplementedError(
            f"MaxCompute 日K查询待补充实现。\n"
            f"参数：code={code}, [{start_date}, {end_date}], adjust={adjust}\n"
            f"已配置表名：{table}\n"
            f"请补充以下信息后填充 SQL：\n"
            f"  1) 表 {table} 的字段名（股票代码列、日期列、开高低收、成交量、成交额）\n"
            f"  2) 股票代码格式（{code} 应转换为何种形式）\n"
            f"  3) 日期列类型（STRING/DATE/DATETIME）\n"
            f"  4) 分区字段（如有，需在 WHERE 中加入以触发分区裁剪）\n"
            f"  5) 复权方式（独立表 / 字段过滤 / 单表无复权）"
        )

    def _fetch_minute_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        period: str,
        adjust: str,
    ) -> pd.DataFrame:
        """从 MaxCompute 拉取分钟K数据。

        TODO 待分钟K表结构确认后实现。若项目暂无分钟K表，可由调用方处理 fallback。
        """
        table = self._require_table("minute")
        raise NotImplementedError(
            f"MaxCompute 分钟K查询待补充实现。\n"
            f"参数：code={code}, [{start_date}, {end_date}], period={period}, adjust={adjust}\n"
            f"已配置表名：{table}\n"
            f"请补充字段映射（period 字段、时间戳列、OHLCV 列）后填充 SQL。"
        )

    def _execute_sql(self, sql: str) -> pd.DataFrame:
        """执行 SQL 并返回 DataFrame。

        pyodps 提供 `execute_sql().open_reader().to_pandas()`，超大结果集
        建议使用 `read_sql` 流式读取。当前框架按单只股票查询规模较小，
        直接 to_pandas 即可。
        """
        odps = self._get_odps()
        logger.debug(f"MaxCompute SQL: {sql}")
        with odps.execute_sql(sql).open_reader(tunnel=True) as reader:
            return reader.to_pandas()
