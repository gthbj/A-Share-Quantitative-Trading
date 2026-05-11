"""Tushare Pro 数据源实现。

作为 AKShare 的备用数据源，当东财接口不可用时切换使用。
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from .base_data_source import BaseDataSource
from .local_storage import LocalStorage
from utils.logger import get_logger

logger = get_logger(__name__)


class TushareDataSource(BaseDataSource):
    """基于 Tushare Pro 的数据源。

    支持个股、ETF、指数的日K与分钟K数据获取。
    列名标准化、日期格式处理、本地缓存逻辑与 AKShareDataSource 完全一致。
    """

    def __init__(
        self,
        token: str,
        storage: Optional[LocalStorage] = None,
        use_cache: bool = True,
    ) -> None:
        if not token:
            raise ValueError("Tushare Token 不能为空，请在 config/backtest.yaml 中配置 data.tushare_token")

        self.token = token
        self.storage = storage or LocalStorage()
        self.use_cache = use_cache
        self._ts = None  # 懒加载

    def _get_ts(self):
        if self._ts is None:
            import tushare as ts
            ts.set_token(self.token)
            self._ts = ts.pro_api()
        return self._ts

    def _detect_asset_type(self, code: str) -> str:
        """根据代码判断资产类型：stock / etf / index。"""
        norm = code.split(".")[0]
        if norm.startswith(("15", "16", "51", "56", "58", "59")):
            return "etf"
        if norm.startswith(("000", "399", "88")):
            return "index"
        return "stock"

    def _standardize_df(self, df: pd.DataFrame, norm_code: str, period: str) -> pd.DataFrame:
        """将 Tushare 返回的 DataFrame 标准化为统一格式。"""
        if df is None or df.empty:
            return pd.DataFrame()

        # Tushare 列名映射
        rename_map = {
            "trade_date": "date",
            "open": "open",
            "high": "high",
            "low": "low",
            "close": "close",
            "vol": "volume",
            "amount": "amount",
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
        df["code"] = norm_code

        # Tushare 的 trade_date 格式为 YYYYMMDD
        if "date" in df.columns:
            df["date"] = df["date"].astype(str)
            if period != "daily":
                # 分钟级数据时间格式处理
                df["date"] = df["date"].astype(str).str.replace("-", "").str.replace(":", "").str.replace(" ", "")
                df["date"] = df["date"].str.slice(0, 12)

        cols = ["code", "date", "open", "high", "low", "close", "volume", "amount"]
        available_cols = [c for c in cols if c in df.columns]
        return df[available_cols].copy()

    def get_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        period: str = "daily",
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        ts = self._get_ts()
        norm_code = code  # Tushare 使用 代码.交易所 格式，与框架一致
        asset_type = self._detect_asset_type(code)

        # 1. 尝试读本地缓存
        if self.use_cache:
            cached = self.storage.load_bars(norm_code, start_date, end_date, period=period)
            if not cached.empty:
                return cached

        # 2. Tushare 参数映射
        if asset_type == "etf":
            asset = "FD"
        elif asset_type == "index":
            asset = "I"
        else:
            asset = "E"

        freq = "D" if period == "daily" else period.replace("min", "")
        adj_map = {"qfq": "qfq", "hfq": "hfq", "": None}
        adj = adj_map.get(adjust)

        # 3. 调用 Tushare 拉取
        try:
            df = ts.pro_bar(
                ts_code=norm_code,
                asset=asset,
                freq=freq,
                start_date=start_date,
                end_date=end_date,
                adj=adj,
            )
        except Exception as e:
            logger.warning(f"Tushare 数据获取失败 ({code}, {period}): {e}")
            return pd.DataFrame()

        df = self._standardize_df(df, norm_code, period)
        if df.empty:
            return pd.DataFrame()

        # 4. 写缓存
        if self.use_cache:
            self.storage.save_bars(norm_code, df, period=period)

        return df

    def get_stock_list(self) -> pd.DataFrame:
        """获取股票基础信息列表（Tushare 实现）。"""
        ts = self._get_ts()
        try:
            df = ts.stock_basic(exchange="", list_status="L")
            if df is not None and not df.empty:
                df = df[["ts_code", "name", "industry"]].copy()
                df.columns = ["code", "name", "industry"]
                return df
        except Exception as e:
            logger.warning(f"Tushare 获取股票列表失败: {e}")
        return pd.DataFrame()

    def get_index_constituents(self, index_code: str) -> List[str]:
        """获取指数成分股列表（Tushare 实现）。"""
        ts = self._get_ts()
        norm = index_code.split(".")[0]
        try:
            df = ts.index_weight(index_code=norm)
            if df is not None and not df.empty:
                return df["con_code"].astype(str).tolist()
        except Exception as e:
            logger.warning(f"Tushare 获取指数成分股失败 ({index_code}): {e}")
        return []
