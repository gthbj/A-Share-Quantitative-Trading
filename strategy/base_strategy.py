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
    # 回测频率：daily / 1min / 5min / 15min / 30min / 60min
    frequency: str = "daily"
    # 引擎预加载的全量行情（注入后 get_price 直接查内存，不再查数据源）
    all_bars: Dict[str, pd.DataFrame] = field(default_factory=dict, repr=False)
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
        """调仓至目标仓位（占总资产的百分比）。

        当前版本**未实现**，立即抛错以避免策略静默不下单。请用：

            target_value = portfolio.total_value(...) * percent
            target_qty = int((target_value / price) // 100) * 100
            context.order(code, target_qty - current_qty)
        """
        raise NotImplementedError(
            "order_target_percent 尚未实现：请手动计算目标数量后调用 context.order()。"
            "原因：自动计算会引入对最新价格的隐式依赖，易触发 Lookahead Bias。"
        )

    def order_target_value(self, code: str, value: float) -> None:
        """调仓至目标市值。当前未实现，理由同 ``order_target_percent``。"""
        raise NotImplementedError(
            "order_target_value 尚未实现：请手动计算目标数量后调用 context.order()。"
        )

    def limit_order(self, code: str, amount: int, price: float) -> None:
        """挂限价单。

        Args:
            code: 股票代码。
            amount: 数量，正数为买入、负数为卖出，自动对齐 100 整数倍。
            price: 限价。买入时 bar.low ≤ price 才成交；卖出时 bar.high ≥ price 才成交。

        Raises:
            ValueError: price ≤ 0 时抛出。
        """
        side = OrderSide.BUY if amount > 0 else OrderSide.SELL
        qty = abs(amount)
        if qty == 0:
            return
        if price is None or price <= 0:
            raise ValueError(f"限价必须为正数，传入 {price}")
        self._orders.append(
            Order(code=code, side=side, qty=qty, order_type=OrderType.LIMIT, price=price)
        )

    def stop_order(self, code: str, amount: int, stop_price: float) -> None:
        """挂止损单。

        Args:
            code: 股票代码。
            amount: 数量，正数为买入触发、负数为卖出触发，自动对齐 100 整数倍。
            stop_price: 触发价。卖出时 bar.low ≤ stop_price 触发；买入时 bar.high ≥ stop_price 触发。
                触发后按"更不利"价成交（卖出取 min(open, stop_price)，买入取 max）。

        Raises:
            ValueError: stop_price ≤ 0 时抛出。
        """
        side = OrderSide.BUY if amount > 0 else OrderSide.SELL
        qty = abs(amount)
        if qty == 0:
            return
        if stop_price is None or stop_price <= 0:
            raise ValueError(f"止损价必须为正数，传入 {stop_price}")
        self._orders.append(
            Order(
                code=code, side=side, qty=qty,
                order_type=OrderType.STOP, stop_price=stop_price,
            )
        )

    # ---------- 数据查询接口 ----------

    def get_price(self, code: str, count: int = 20) -> pd.DataFrame:
        """获取最近 N 根 Bar 的历史行情（截至 current_date，含当前 Bar）。

        优先使用引擎预加载的 all_bars（内存查询，无额外 IO）。
        若 all_bars 未注入（如单元测试），则降级为直接查询数据源。
        """
        # 优先路径：引擎预加载数据（日线与分钟级回测均可用）
        if self.all_bars and code in self.all_bars:
            df = self.all_bars[code]
            # current_date 格式：YYYYMMDD（日线）或 YYYYMMDDHHMM（分钟级）
            # 两种格式均支持字符串字典序截断，因为短字符串在同前缀下字典序更小
            mask = df["date"].astype(str) <= str(self.current_date)
            hist = df.loc[mask]
            return hist.tail(count).reset_index(drop=True)

        # 降级路径：直接查询数据源（all_bars 未注入时的兜底，主要用于单元测试）
        from datetime import datetime, timedelta
        import math

        # current_date 可能是 YYYYMMDD（日线）或 YYYYMMDDHHMM（分钟级）
        date_str = self.current_date[:8]
        end = datetime.strptime(date_str, "%Y%m%d")

        # 按频率估算所需自然日跨度，避免分钟级把日数估成 30 天导致拉超大数据
        # 240 = A 股一日有效分钟数（9:30-11:30 + 13:00-15:00）
        bars_per_day = {
            "daily": 1,
            "1min": 240,
            "5min": 48,
            "15min": 16,
            "30min": 8,
            "60min": 4,
        }.get(self.frequency, 1)
        if bars_per_day <= 1:
            # 日线：考虑节假日，1.6 倍系数足够覆盖
            days_back = max(int(count * 1.6), 2)
        else:
            # 分钟级：count / bars_per_day 是交易日数，再补 2 天兜底
            days_back = max(math.ceil(count / bars_per_day) + 2, 2)

        start = end - timedelta(days=days_back)
        df = self.data_source.get_bars(
            code, start.strftime("%Y%m%d"), date_str, period=self.frequency
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
