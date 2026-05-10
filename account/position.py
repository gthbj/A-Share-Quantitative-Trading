"""单只股票持仓模型，支持 A 股 T+1 规则。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class Position:
    """单只股票持仓。

    Attributes:
        code: 股票代码。
        total_qty: 总持仓数量（含当日买入）。
        sellable_qty: 可卖数量（T+1：当日买入部分不可卖）。
        cost_price: 当前成本价（加权平均）。
        buy_dates: 记录每笔买入的数量与日期，用于 T+1 判定。
    """

    code: str
    total_qty: int = 0
    sellable_qty: int = 0
    cost_price: float = 0.0
    # date -> qty 的映射，记录各交易日买入数量
    _buy_records: Dict[str, int] = field(default_factory=dict, repr=False)

    def apply_buy(self, qty: int, price: float, date: str) -> None:
        """执行买入，更新持仓与成本。"""
        if qty <= 0:
            return
        # 加权平均更新成本价
        total_cost = self.cost_price * self.total_qty + price * qty
        self.total_qty += qty
        self.cost_price = total_cost / self.total_qty if self.total_qty > 0 else 0.0
        # 当日买入不可卖
        self._buy_records[date] = self._buy_records.get(date, 0) + qty

    def apply_sell(self, qty: int, price: float) -> None:
        """执行卖出，更新持仓。"""
        if qty <= 0:
            return
        qty = min(qty, self.total_qty)
        self.total_qty -= qty
        self.sellable_qty -= qty
        # 成本价不变（加权平均法），若清仓则归零
        if self.total_qty == 0:
            self.cost_price = 0.0
            self._buy_records.clear()

    def update_sellable(self, current_date: str) -> None:
        """每日开盘后更新可卖数量。

        将前一日及更早的买入记录加入可卖数量，当日买入仍冻结。
        """
        sellable = 0
        for date, qty in self._buy_records.items():
            if date != current_date:
                sellable += qty
        self.sellable_qty = sellable

    def market_value(self, current_price: float = 0.0) -> float:
        """按当前价格计算持仓市值。"""
        return self.total_qty * current_price

    def profit_ratio(self, current_price: float = 0.0) -> float:
        """当前盈亏比例。"""
        if self.cost_price == 0:
            return 0.0
        return (current_price - self.cost_price) / self.cost_price
