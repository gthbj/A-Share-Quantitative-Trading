"""虚拟账户资产组合：资金、持仓、市值的统一管理。"""

from __future__ import annotations

from typing import Dict, List, Optional

from .position import Position


class Portfolio:
    """虚拟账户。

    管理可用现金、冻结资金、持仓字典，以及总资产计算。
    """

    def __init__(self, initial_capital: float = 1_000_000.0) -> None:
        self.initial_capital = initial_capital
        self.available_cash = initial_capital
        self.frozen_cash = 0.0
        self.positions: Dict[str, Position] = {}
        self.current_date: Optional[str] = None

    # ---------- 查询接口 ----------

    @property
    def total_value(self, price_map: Optional[Dict[str, float]] = None) -> float:
        """总资产 = 可用现金 + 冻结资金 + 持仓市值。"""
        pos_value = 0.0
        if price_map:
            for code, pos in self.positions.items():
                price = price_map.get(code, 0.0)
                pos_value += pos.market_value(price)
        return self.available_cash + self.frozen_cash + pos_value

    @property
    def total_position_value(self, price_map: Dict[str, float]) -> float:
        """持仓总市值。"""
        return sum(
            pos.market_value(price_map.get(code, 0.0))
            for code, pos in self.positions.items()
        )

    def get_position(self, code: str) -> Optional[Position]:
        return self.positions.get(code)

    def has_position(self, code: str) -> bool:
        pos = self.positions.get(code)
        return pos is not None and pos.total_qty > 0

    # ---------- 每日更新 ----------

    def before_trading(self, date: str) -> None:
        """每日开盘前调用：解冻 T+1 持仓。"""
        self.current_date = date
        self.frozen_cash = 0.0
        for pos in self.positions.values():
            pos.update_sellable(date)

    # ---------- 交易接口 ----------

    def reserve_cash(self, amount: float) -> bool:
        """冻结下单所需资金。"""
        if amount > self.available_cash:
            return False
        self.available_cash -= amount
        self.frozen_cash += amount
        return True

    def release_cash(self, amount: float) -> None:
        """释放未成交的冻结资金。"""
        release = min(amount, self.frozen_cash)
        self.frozen_cash -= release
        self.available_cash += release

    def apply_buy_fill(self, code: str, qty: int, price: float, date: str) -> None:
        """买入成交后更新账户。"""
        cost = qty * price
        # 从冻结资金中扣减（reserve_cash 时已冻结）
        self.frozen_cash -= cost
        if self.frozen_cash < 0:
            self.available_cash += self.frozen_cash
            self.frozen_cash = 0.0

        pos = self.positions.get(code)
        if pos is None:
            pos = Position(code=code)
            self.positions[code] = pos
        pos.apply_buy(qty, price, date)

    def apply_sell_fill(self, code: str, qty: int, price: float) -> None:
        """卖出成交后更新账户。"""
        proceed = qty * price
        self.available_cash += proceed
        pos = self.positions.get(code)
        if pos is not None:
            pos.apply_sell(qty, price)
            if pos.total_qty == 0:
                del self.positions[code]

    # ---------- 工具 ----------

    def holding_codes(self) -> List[str]:
        return [code for code, pos in self.positions.items() if pos.total_qty > 0]
