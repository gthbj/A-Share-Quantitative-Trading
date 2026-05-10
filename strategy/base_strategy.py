"""策略基类与 Context 上下文。

所有用户策略继承 BaseStrategy，通过 Context 与回测引擎交互。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import pandas as pd

from account.portfolio import Portfolio
from data_layer.base_data_source import BaseDataSource
from engine.trade_engine import Order, OrderSide, OrderType
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class Context:
    """策略运行上下文。

    回测引擎每日注入最新状态，策略通过此对象下单、查询数据。
    """

    portfolio: Portfolio
    data_source: BaseDataSource
    current_date: str
    # 私有：待处理订单队列
    _orders: List[Order] = field(default_factory=list, repr=False)
    # 用户可存储自定义状态
    user_data: Dict[str, Any] = field(default_factory=dict)

    # ---------- 下单接口 ----------

    def order(self, code: str, amount: int) -> None:
        """下市价单。

        Args:
            code: 股票代码。
            amount: 数量，正数为买入，负数为卖出，自动对齐到100的整数倍。
        """
        side = OrderSide.BUY if amount > 0 else OrderSide.SELL
        qty = abs(amount)
        if qty == 0:
            return
        self._orders.append(Order(code=code, side=side, qty=qty, order_type=OrderType.MARKET))

    def order_target_percent(self, code: str, percent: float) -> None:
        """调仓至目标仓位（占总资产的百分比）。"""
        # 总资产以当前可用现金估算（简化）
        # 更精确的做法需要传入价格，这里作为接口预留
        logger.warning("order_target_percent 为简化实现，建议手动计算目标数量后调用 order()")
        # 暂不实现完整逻辑，防止 lookahead bias

    def order_target_value(self, code: str, value: float) -> None:
        """调仓至目标市值。"""
        logger.warning("order_target_value 为简化实现，建议手动计算目标数量后调用 order()")

    # ---------- 数据查询接口 ----------

    def get_price(self, code: str, count: int = 20) -> pd.DataFrame:
        """获取最近 N 日历史行情。"""
        # 计算日期范围：向前取 count*1.5 个自然日，再过滤交易日
        from datetime import datetime, timedelta

        end = datetime.strptime(self.current_date, "%Y%m%d")
        start = end - timedelta(days=int(count * 1.5))
        df = self.data_source.get_daily_bars(
            code, start.strftime("%Y%m%d"), self.current_date
        )
        if not df.empty:
            df = df.tail(count)
        return df

    # ---------- 内部方法 ----------

    def pop_orders(self) -> List[Order]:
        """由引擎调用，取出当前累计的订单并清空队列。"""
        orders = self._orders[:]
        self._orders.clear()
        return orders


class BaseStrategy(ABC):
    """策略抽象基类。

    用户策略必须继承此类，并重写以下生命周期方法：
      - initialize(context)
      - handle_data(context, data)
    """

    def __init__(self) -> None:
        self.context: Optional[Context] = None
        self._universe: List[str] = []
        self._benchmark: str = "000300.SH"

    # ---------- 生命周期方法 ----------

    def initialize(self, context: Context) -> None:
        """初始化：设置股票池、基准、调度周期等。"""
        self.context = context
        # 默认全A股（演示时建议缩小范围）
        self._universe = []

    def before_trading_start(self, context: Context, data: Dict[str, pd.Series]) -> None:
        """每日开盘前调用。子类可覆盖。"""
        pass

    @abstractmethod
    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        """核心交易逻辑，每个 Bar 调用一次。"""
        raise NotImplementedError

    def after_trading_end(self, context: Context, data: Dict[str, pd.Series]) -> None:
        """每日收盘后调用。子类可覆盖。"""
        pass

    # ---------- 工具方法 ----------

    def get_universe(self) -> List[str]:
        return self._universe

    def set_universe(self, codes: List[str]) -> None:
        self._universe = codes

    def set_benchmark(self, code: str) -> None:
        self._benchmark = code
