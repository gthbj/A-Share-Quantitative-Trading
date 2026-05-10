"""日内双均线策略：基于分钟级 K 线的 MA5/MA15 交叉，收盘前强制平仓。

这是一个典型的分钟级策略，完全依赖分钟粒度数据，日线回测无法有效验证。
"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from utils.logger import get_logger

logger = get_logger(__name__)


class IntradayMAStrategy(BaseStrategy):
    """日内双均线突破策略。

    交易逻辑：
    1. 计算最近 N 根分钟 K 线的短期均线（默认 5）与长期均线（默认 15）。
    2. 在交易时段中段（默认 09:45 ~ 14:50）监听金叉/死叉：
       - 金叉（MA5 上穿 MA15）→ 买入
       - 死叉（MA5 下穿 MA15）→ 卖出
    3. 每日 14:50 后若仍有持仓，强制平仓（避免隔夜风险）。
    4. 每日 15:00 最后一个 Bar 作为兜底，再次强制平仓。

    参数：
        short_window: 短期均线周期（默认 5）
        long_window: 长期均线周期（默认 15）
        trade_start: 允许交易的起始时间 HHMM（默认 "0945"）
        trade_end: 强制平仓时间 HHMM（默认 "1450"）
    """

    def __init__(
        self,
        short_window: int = 5,
        long_window: int = 15,
        trade_start: str = "0945",
        trade_end: str = "1450",
    ) -> None:
        super().__init__()
        self.short_window = short_window
        self.long_window = long_window
        self.trade_start = trade_start
        self.trade_end = trade_end

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        self.set_universe(["000001.SZ"])  # 平安银行，流动性好
        context.user_data["holding"] = False

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        current = context.current_date  # 分钟级格式：YYYYMMDDHHMM
        time_str = current[-4:]        # HHMM
        code = self._universe[0]

        # 获取历史分钟 K 线
        hist = context.get_price(code, count=self.long_window + 5)
        if len(hist) < self.long_window:
            return

        hist["ma_short"] = hist["close"].rolling(self.short_window).mean()
        hist["ma_long"] = hist["close"].rolling(self.long_window).mean()

        if len(hist) < 2:
            return

        prev = hist.iloc[-2]
        curr = hist.iloc[-1]
        is_holding = context.user_data["holding"]

        # 强制平仓时段
        if time_str >= self.trade_end and is_holding:
            pos = context.portfolio.get_position(code)
            if pos and pos.sellable_qty > 0:
                context.order(code, -pos.sellable_qty)
                context.user_data["holding"] = False
                logger.info(f"{current} 强制平仓 {code} {pos.sellable_qty}股")
            return

        # 非交易时段（开盘前、收盘前）不生成新信号
        if time_str < self.trade_start or time_str >= self.trade_end:
            return

        # 金叉买入
        if prev["ma_short"] <= prev["ma_long"] and curr["ma_short"] > curr["ma_long"]:
            if not is_holding:
                budget = context.portfolio.available_cash * 0.95
                price = curr["close"]
                if price > 0:
                    qty = int((budget / price) // 100) * 100
                    if qty > 0:
                        context.order(code, qty)
                        context.user_data["holding"] = True
                        logger.info(f"{current} 金叉买入 {code} {qty}股")

        # 死叉卖出
        elif prev["ma_short"] >= prev["ma_long"] and curr["ma_short"] < curr["ma_long"]:
            if is_holding:
                pos = context.portfolio.get_position(code)
                if pos and pos.sellable_qty > 0:
                    context.order(code, -pos.sellable_qty)
                    context.user_data["holding"] = False
                    logger.info(f"{current} 死叉卖出 {code} {pos.sellable_qty}股")
