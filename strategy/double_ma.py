"""双均线策略示例：5日线上穿20日线买入，下穿卖出。"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from utils.logger import get_logger

logger = get_logger(__name__)


class DoubleMAStrategy(BaseStrategy):
    """双均线交叉策略（MA5 / MA20）。

    买入信号：MA5 上穿 MA20（金叉）
    卖出信号：MA5 下穿 MA20（死叉）
    """

    def __init__(self, short_window: int = 5, long_window: int = 20) -> None:
        super().__init__()
        self.short_window = short_window
        self.long_window = long_window

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        # 演示：使用少量股票作为 universe
        self.set_universe(["510300.SH"])

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        for code in self._universe:
            hist = context.get_price(code, count=self.long_window + 5)
            if len(hist) < self.long_window:
                continue

            hist["ma_short"] = hist["close"].rolling(self.short_window).mean()
            hist["ma_long"] = hist["close"].rolling(self.long_window).mean()

            if len(hist) < 2:
                continue

            prev = hist.iloc[-2]
            curr = hist.iloc[-1]

            # 用实际持仓状态作为 holding 判断依据，避免止损平仓后 holding 集合
            # 与真实仓位不一致而导致策略永远不再买入的 bug。
            has_pos = context.portfolio.has_position(code)

            # 金叉
            if prev["ma_short"] <= prev["ma_long"] and curr["ma_short"] > curr["ma_long"]:
                if not has_pos:
                    # 买入：把全部可用资金押注在单一品种
                    budget = context.portfolio.available_cash
                    price = curr["close"]
                    if price > 0:
                        qty = int((budget / price) // 100) * 100
                        if qty > 0:
                            context.order(code, qty)
                            logger.info(f"{context.current_date} 金叉买入 {code} {qty}股")

            # 死叉
            elif prev["ma_short"] >= prev["ma_long"] and curr["ma_short"] < curr["ma_long"]:
                if has_pos:
                    pos = context.portfolio.get_position(code)
                    if pos and pos.sellable_qty > 0:
                        context.order(code, -pos.sellable_qty)
                        logger.info(f"{context.current_date} 死叉卖出 {code} {pos.sellable_qty}股")
