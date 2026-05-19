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
        """执行卖出，更新持仓。

        关键：必须按 FIFO 顺序同步减少 ``_buy_records``，否则下一交易日
        ``update_sellable`` 会从陈旧的 ``_buy_records`` 把已卖份额重新计入
        ``sellable_qty``，导致可卖数量虚高（凭空创造份额，进而虚增成交金额）。
        """
        if qty <= 0:
            return
        qty = min(qty, self.total_qty)
        self.total_qty -= qty
        self.sellable_qty -= qty
        # 保护：sellable_qty 不应为负（防御性）
        if self.sellable_qty < 0:
            self.sellable_qty = 0

        # FIFO 同步消减 _buy_records，按日期升序优先卖最早买入
        remaining = qty
        for date in sorted(self._buy_records.keys()):
            if remaining <= 0:
                break
            record_qty = self._buy_records[date]
            if record_qty > remaining:
                self._buy_records[date] = record_qty - remaining
                remaining = 0
            else:
                remaining -= record_qty
                del self._buy_records[date]

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
