"""动量策略示例：每月初买入上月涨幅最高的 N 只股票，等权配置。"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from utils.logger import get_logger

logger = get_logger(__name__)


class MomentumStrategy(BaseStrategy):
    """月度动量策略。

    每月第一个交易日收盘后，计算上月收益率，买入前 N 名，等权持有至下月。
    """

    def __init__(self, top_n: int = 10, lookback: int = 20) -> None:
        super().__init__()
        self.top_n = top_n
        self.lookback = lookback

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        # 演示用 universe
        self.set_universe(
            ["000001.SZ", "000002.SZ", "000063.SZ", "000100.SZ", "000333.SZ",
             "600000.SH", "600009.SH", "600016.SH", "600028.SH", "600030.SH",
             "600036.SH", "600276.SH", "600519.SH", "600887.SH", "601318.SH",
             "601398.SH", "601888.SH", "603288.SH", "000858.SZ", "002415.SZ"]
        )
        context.user_data["last_rebalance"] = None
        context.user_data["holdings"] = []

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        current = context.current_date
        # 简化：每月1号调仓（实际应判断是否为交易日）
        if not current.endswith("01"):
            return

        last_rebalance = context.user_data.get("last_rebalance")
        if last_rebalance == current:
            return

        # 1. 计算过去 lookback 日收益率
        returns = {}
        for code in self._universe:
            hist = context.get_price(code, count=self.lookback + 5)
            if len(hist) >= self.lookback:
                old_close = hist.iloc[-self.lookback]["close"]
                new_close = hist.iloc[-1]["close"]
                if old_close > 0:
                    returns[code] = (new_close - old_close) / old_close

        if not returns:
            return

        # 2. 排序取前 N
        sorted_codes = sorted(returns, key=returns.get, reverse=True)  # type: ignore
        targets = sorted_codes[: self.top_n]

        # 3. 清仓非目标持仓
        for code in context.user_data.get("holdings", []):
            if code not in targets:
                pos = context.portfolio.get_position(code)
                if pos and pos.sellable_qty > 0:
                    context.order(code, -pos.sellable_qty)

        # 4. 等权买入目标
        cash_per_stock = context.portfolio.available_cash / max(len(targets), 1)
        for code in targets:
            bar = data.get(code)
            price = bar["close"] if bar is not None else 0.0
            if price > 0:
                qty = int((cash_per_stock / price) // 100) * 100
                if qty > 0:
                    context.order(code, qty)

        context.user_data["last_rebalance"] = current
        context.user_data["holdings"] = targets
        logger.info(f"{current} 动量调仓: {targets}")
