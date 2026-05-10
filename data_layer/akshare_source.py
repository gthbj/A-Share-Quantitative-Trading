"""AKShare 数据源实现。

利用 AKShare 免费接口获取 A 股行情，并自动缓存到本地 LocalStorage。
"""

from __future__ import annotations

from typing import List, Optional

import pandas as pd

from .base_data_source import BaseDataSource
from .local_storage import LocalStorage


class AKShareDataSource(BaseDataSource):
    """基于 AKShare 的 A 股数据源。

    首次请求时会调用 AKShare API 拉取数据并写入本地缓存；
    后续请求优先读取本地缓存，缺失部分再增量拉取。
    """

    def __init__(
        self,
        storage: Optional[LocalStorage] = None,
        use_cache: bool = True,
    ) -> None:
        self.storage = storage or LocalStorage()
        self.use_cache = use_cache
        self._ak = None  # 懒加载

    def _get_ak(self):
        if self._ak is None:
            import akshare as ak

            self._ak = ak
        return self._ak

    def _normalize_code(self, code: str) -> str:
        """统一代码格式，去除后缀用于 AKShare 查询。"""
        return code.split(".")[0]

    def get_daily_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        ak = self._get_ak()
        norm_code = self._normalize_code(code)

        # 1. 尝试读本地缓存
        if self.use_cache:
            cached = self.storage.load_daily(norm_code, start_date, end_date)
            if not cached.empty:
                return cached

        # 2. 调用 AKShare 拉取
        try:
            df = ak.stock_zh_a_hist(
                symbol=norm_code,
                period="daily",
                start_date=start_date,
                end_date=end_date,
                adjust=adjust,
            )
        except Exception:
            return pd.DataFrame()

        if df is None or df.empty:
            return pd.DataFrame()

        # 列名标准化
        rename_map = {
            "日期": "date",
            "开盘": "open",
            "最高": "high",
            "最低": "low",
            "收盘": "close",
            "成交量": "volume",
            "成交额": "amount",
        }
        df = df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns})
        df["code"] = norm_code
        df["date"] = df["date"].astype(str).str.replace("-", "")
        df = df[["code", "date", "open", "high", "low", "close", "volume", "amount"]]

        # 3. 写缓存
        if self.use_cache:
            self.storage.save_daily(norm_code, df)

        return df

    def get_stock_list(self) -> pd.DataFrame:
        cached = self.storage.load_stock_list()
        if not cached.empty:
            return cached

        ak = self._get_ak()
        df = ak.stock_zh_a_spot_em()
        # 取关键列
        cols = ["代码", "名称", "所属行业"]
        df = df[[c for c in cols if c in df.columns]].copy()
        df.columns = ["code", "name", "industry"]
        self.storage.save_stock_list(df)
        return df

    def get_index_constituents(self, index_code: str) -> List[str]:
        """获取指数成分股，目前仅支持部分常见指数。"""
        ak = self._get_ak()
        norm = index_code.split(".")[0]

        try:
            if norm == "000300":
                df = ak.index_stock_cons_weight_csindex(symbol="000300")
            elif norm == "000905":
                df = ak.index_stock_cons_weight_csindex(symbol="000905")
            elif norm == "000001":
                df = ak.index_stock_cons_weight_csindex(symbol="000001")
            else:
                return []
        except Exception:
            return []

        if "成分券代码" in df.columns:
            return df["成分券代码"].astype(str).tolist()
        if "code" in df.columns:
            return df["code"].astype(str).tolist()
        return []
