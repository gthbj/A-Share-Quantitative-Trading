"""多因子选股策略示例：基于 PE、PB、ROE 综合评分。"""

from __future__ import annotations

from typing import Dict

import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from utils.logger import get_logger

logger = get_logger(__name__)


class MultiFactorStrategy(BaseStrategy):
    """多因子选股策略。

    每月初根据 PE（低）、PB（低）、ROE（高）综合评分，
    选取得分最高的 N 只股票等权持有。

    注意：此示例为演示框架接口，实际因子数据需接入财务数据库。
    """

    def __init__(self, top_n: int = 5) -> None:
        super().__init__()
        self.top_n = top_n

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        self.set_universe(
            ["000001.SZ", "000002.SZ", "000333.SZ", "000858.SZ",
             "600000.SH", "600036.SH", "600519.SH", "601318.SH"]
        )
        context.user_data["last_rebalance"] = None
        context.user_data["holdings"] = []

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        current = context.current_date
        if not current.endswith("01"):
            return

        if context.user_data.get("last_rebalance") == current:
            return

        # 模拟因子得分：使用价格动量作为代理（真实场景应接入财务数据）
        scores = {}
        for code in self._universe:
            hist = context.get_price(code, count=60)
            if len(hist) >= 20:
                # 用20日涨幅作为模拟因子（越高越好，与PE/PB反向不同，仅演示框架）
                ret = (hist.iloc[-1]["close"] - hist.iloc[-20]["close"]) / hist.iloc[-20]["close"]
                scores[code] = ret

        if not scores:
            return

        sorted_codes = sorted(scores, key=scores.get, reverse=True)  # type: ignore
        targets = sorted_codes[: self.top_n]

        # 清仓
        for code in context.user_data.get("holdings", []):
            if code not in targets:
                pos = context.portfolio.get_position(code)
                if pos and pos.sellable_qty > 0:
                    context.order(code, -pos.sellable_qty)

        # 等权买入
        cash_each = context.portfolio.available_cash / max(len(targets), 1)
        for code in targets:
            bar = data.get(code)
            price = bar["close"] if bar is not None else 0.0
            if price > 0:
                qty = int((cash_each / price) // 100) * 100
                if qty > 0:
                    context.order(code, qty)

        context.user_data["last_rebalance"] = current
        context.user_data["holdings"] = targets
        logger.info(f"{current} 多因子调仓: {targets}")
