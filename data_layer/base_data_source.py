"""数据源抽象基类与通用数据模型。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Dict, List, Optional

import pandas as pd


@dataclass(frozen=True)
class Bar:
    """单根K线数据。"""

    code: str
    date: str  # YYYYMMDD
    open: float
    high: float
    low: float
    close: float
    volume: float
    amount: float


class BaseDataSource(ABC):
    """数据源抽象基类。

    所有具体数据源（AKShare、Tushare、本地文件等）均继承此类，
    保证上层模块对数据源无感知。
    """

    @abstractmethod
    def get_daily_bars(
        self,
        code: str,
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
    ) -> pd.DataFrame:
        """获取日K线数据。

        Args:
            code: 股票代码，如 "000001.SZ"。
            start_date: 起始日期，YYYYMMDD。
            end_date: 结束日期，YYYYMMDD。
            adjust: 复权方式，"qfq" 前复权 / "hfq" 后复权 / None 不复权。

        Returns:
            DataFrame，列至少包含 [date, open, high, low, close, volume, amount]。
        """
        raise NotImplementedError

    @abstractmethod
    def get_stock_list(self) -> pd.DataFrame:
        """获取股票基础信息列表。

        Returns:
            DataFrame，列至少包含 [code, name, list_date, industry]。
        """
        raise NotImplementedError

    @abstractmethod
    def get_index_constituents(self, index_code: str) -> List[str]:
        """获取指数成分股列表。

        Args:
            index_code: 指数代码，如 "000300.SH"。

        Returns:
            成分股代码列表。
        """
        raise NotImplementedError

    def get_multi_daily_bars(
        self,
        codes: List[str],
        start_date: str,
        end_date: str,
        adjust: str = "qfq",
    ) -> Dict[str, pd.DataFrame]:
        """批量获取多只股票日K线。

        默认串行获取，子类可覆盖为并行加速。
        """
        result: Dict[str, pd.DataFrame] = {}
        for code in codes:
            df = self.get_daily_bars(code, start_date, end_date, adjust)
            if not df.empty:
                result[code] = df
        return result
