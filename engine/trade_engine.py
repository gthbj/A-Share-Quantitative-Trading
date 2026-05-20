"""交易撮合引擎：订单管理、A股规则撮合、费用计算。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional, Tuple

import pandas as pd

from account.portfolio import Portfolio
from utils.code import price_limit_pct
from utils.logger import get_logger

logger = get_logger(__name__)


class OrderType(Enum):
    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"


class OrderSide(Enum):
    BUY = "buy"
    SELL = "sell"


@dataclass
class Order:
    """订单。"""

    code: str
    side: OrderSide
    qty: int
    order_type: OrderType = OrderType.MARKET
    price: Optional[float] = None  # 限价单/止损单有效
    stop_price: Optional[float] = None  # 止损单触发价

    def __post_init__(self):
        # A股最小交易单位为100股
        if self.qty % 100 != 0:
            original = self.qty
            self.qty = (self.qty // 100) * 100
            logger.warning(
                f"订单数量 {original} 不是 100 的倍数，已截断为 {self.qty}（A股最小交易单位）。"
                f"qty=0 时该订单将不会被撮合。code={self.code} side={self.side.value}"
            )


@dataclass
class Fill:
    """成交记录。"""

    code: str
    side: OrderSide
    qty: int
    price: float
    commission: float = 0.0
    stamp_duty: float = 0.0
    transfer_fee: float = 0.0

    @property
    def total_cost(self) -> float:
        return self.commission + self.stamp_duty + self.transfer_fee


class TradeEngine:
    """A股交易撮合引擎。

    职责：
    1. 验证订单合法性（资金、T+1、涨跌停、成交量限制）。
    2. 模拟撮合，计算成交价。
    3. 计算并扣除交易费用（佣金、印花税、过户费）。
    4. 更新 Portfolio。
    """

    def __init__(
        self,
        commission_rate: float = 0.00025,
        min_commission: float = 5.0,
        stamp_duty_rate: float = 0.0005,
        transfer_fee_rate: float = 0.00001,
        slippage_type: str = "percent",
        slippage_value: float = 0.001,
        volume_limit: float = 0.10,
        price_type: str = "next_open",
    ) -> None:
        self.commission_rate = commission_rate
        self.min_commission = min_commission
        self.stamp_duty_rate = stamp_duty_rate
        self.transfer_fee_rate = transfer_fee_rate
        self.slippage_type = slippage_type
        self.slippage_value = slippage_value
        self.volume_limit = volume_limit
        self.price_type = price_type

    # ---------- 费用计算 ----------

    def calc_commission(self, amount: float) -> float:
        comm = amount * self.commission_rate
        return max(comm, self.min_commission)

    def calc_stamp_duty(self, amount: float) -> float:
        return amount * self.stamp_duty_rate

    def calc_transfer_fee(self, amount: float) -> float:
        return amount * self.transfer_fee_rate

    # ---------- 撮合核心 ----------

    def execute_orders(
        self,
        orders: List[Order],
        portfolio: Portfolio,
        bar_data: Dict[str, pd.Series],
        current_date: str,
    ) -> List[Fill]:
        """批量执行订单，返回成交列表。"""
        fills: List[Fill] = []
        for order in orders:
            fill = self._try_fill(order, portfolio, bar_data, current_date)
            if fill:
                fills.append(fill)
                self._apply_fill(fill, portfolio, current_date)
        return fills

    def _resolve_trigger_and_price(
        self,
        order: Order,
        bar: pd.Series,
    ) -> Optional[float]:
        """根据 order_type 与 bar 解析"是否触发 + 成交基准价（未叠滑点）"。

        返回 None 表示未触发。返回的价格还未叠加滑点。
        - MARKET: 按 self.price_type 选 open 或 close
        - LIMIT: 价格区间满足时按 order.price 成交
        - STOP: 触发后按"更不利"价（min/max(open, stop_price)）
        """
        bar_open = float(bar.get("open", 0.0))
        bar_high = float(bar.get("high", bar_open))
        bar_low = float(bar.get("low", bar_open))
        bar_close = float(bar.get("close", bar_open))

        if order.order_type == OrderType.MARKET:
            ref = bar_open if self.price_type == "next_open" else bar_close
            return ref if ref > 0 else None

        if order.order_type == OrderType.LIMIT:
            if order.price is None or order.price <= 0:
                return None
            if order.side == OrderSide.BUY:
                # 买入限价：bar.low 触及限价才成交
                return order.price if bar_low <= order.price else None
            else:
                return order.price if bar_high >= order.price else None

        if order.order_type == OrderType.STOP:
            if order.stop_price is None or order.stop_price <= 0:
                return None
            if order.side == OrderSide.SELL:
                # 卖出止损：bar.low 触及止损价才触发，取更不利价（更低）
                if bar_low <= order.stop_price:
                    return min(bar_open, order.stop_price) if bar_open > 0 else order.stop_price
                return None
            else:
                # 买入止损（突破）：bar.high 触及触发价
                if bar_high >= order.stop_price:
                    return max(bar_open, order.stop_price) if bar_open > 0 else order.stop_price
                return None

        return None

    def _try_fill(
        self,
        order: Order,
        portfolio: Portfolio,
        bar_data: Dict[str, pd.Series],
        current_date: str,
    ) -> Optional[Fill]:
        """尝试对单笔订单进行撮合。"""
        bar = bar_data.get(order.code)
        if bar is None or bar.empty:
            return None

        # 价格解析（含 LIMIT / STOP 触发判断）
        price = self._resolve_trigger_and_price(order, bar)
        if price is None or price <= 0:
            return None

        # 涨跌停限制（对所有订单类型均生效）
        # 板块细分：主板 ±10%、科创板/创业板 ±20%、ETF/LOF/可转债 ±10%
        # 创业板按 current_date 切换（2020-08-24 起 ±20%）
        # ST ±5% / 北交所 ±30% / 新股首日：本期不支持，见 TODO
        prev_close = bar.get("prev_close", price)
        if prev_close and prev_close > 0:
            limit_pct = price_limit_pct(order.code, current_date)
            up_limit = prev_close * (1.0 + limit_pct)
            down_limit = prev_close * (1.0 - limit_pct)
            if order.side == OrderSide.BUY and price >= up_limit:
                return None  # 涨停无法买入
            if order.side == OrderSide.SELL and price <= down_limit:
                return None  # 跌停无法卖出

        # 成交量限制
        daily_volume = bar.get("volume", 0)
        max_vol = daily_volume * self.volume_limit
        if order.qty > max_vol:
            order.qty = int((max_vol // 100) * 100)
        if order.qty <= 0:
            return None

        # T+1 检查：卖出时不能超过可卖数量
        if order.side == OrderSide.SELL:
            pos = portfolio.get_position(order.code)
            if pos is None or pos.sellable_qty <= 0:
                return None
            order.qty = min(order.qty, pos.sellable_qty)
            if order.qty <= 0:
                return None

        # 资金检查：买入时 reserve_cash 已由上层完成，这里仅作防御
        if order.side == OrderSide.BUY:
            required = order.qty * price
            if portfolio.available_cash + portfolio.frozen_cash < required:
                return None

        # 滑点：仅 MARKET 单叠加滑点；LIMIT / STOP 自带价格约束不再额外加滑点
        if order.order_type == OrderType.MARKET:
            fill_price = self._apply_slippage(price, order.side)
        else:
            fill_price = price

        # 计算费用
        amount = order.qty * fill_price
        commission = self.calc_commission(amount)
        stamp = self.calc_stamp_duty(amount) if order.side == OrderSide.SELL else 0.0
        transfer = self.calc_transfer_fee(amount)

        return Fill(
            code=order.code,
            side=order.side,
            qty=order.qty,
            price=fill_price,
            commission=commission,
            stamp_duty=stamp,
            transfer_fee=transfer,
        )

    def _apply_slippage(self, price: float, side: OrderSide) -> float:
        """对价格施加滑点。"""
        if self.slippage_type == "percent":
            delta = price * self.slippage_value
        else:
            delta = self.slippage_value
        if side == OrderSide.BUY:
            return price + delta
        else:
            return price - delta

    def _apply_fill(
        self, fill: Fill, portfolio: Portfolio, current_date: str
    ) -> None:
        """将成交结果写入 Portfolio。"""
        if fill.side == OrderSide.BUY:
            portfolio.apply_buy_fill(fill.code, fill.qty, fill.price, current_date)
        else:
            portfolio.apply_sell_fill(fill.code, fill.qty, fill.price)

        # 费用从可用资金扣除（买入时已冻结部分资金，apply_buy_fill 会解冻差额）
        # 佣金、印花税、过户费统一从 available_cash 扣除
        total_fee = fill.total_cost
        if fill.side == OrderSide.BUY:
            # 买入时佣金和过户费从 available 扣
            portfolio.available_cash -= total_fee
        else:
            # 卖出时佣金、印花税、过户费从 available 扣
            portfolio.available_cash -= total_fee
