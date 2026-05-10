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
        self.set_universe(["000001.SZ", "000002.SZ", "600000.SH"])
        context.user_data["holding"] = set()

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        holding = context.user_data["holding"]

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

            # 金叉
            if prev["ma_short"] <= prev["ma_long"] and curr["ma_short"] > curr["ma_long"]:
                if code not in holding:
                    # 买入：每只股票分配 1/3 资金
                    budget = context.portfolio.available_cash / max(len(self._universe) - len(holding), 1)
                    price = curr["close"]
                    if price > 0:
                        qty = int((budget / price) // 100) * 100
                        if qty > 0:
                            context.order(code, qty)
                            holding.add(code)
                            logger.info(f"{context.current_date} 金叉买入 {code} {qty}股")

            # 死叉
            elif prev["ma_short"] >= prev["ma_long"] and curr["ma_short"] < curr["ma_long"]:
                if code in holding:
                    pos = context.portfolio.get_position(code)
                    if pos and pos.sellable_qty > 0:
                        context.order(code, -pos.sellable_qty)
                        holding.discard(code)
                        logger.info(f"{context.current_date} 死叉卖出 {code} {pos.sellable_qty}股")
