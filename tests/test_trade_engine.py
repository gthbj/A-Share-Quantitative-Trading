"""TradeEngine 单元测试。"""

import pandas as pd
import pytest

from account.portfolio import Portfolio
from engine.trade_engine import (
    Fill,
    Order,
    OrderSide,
    OrderType,
    TradeEngine,
)

# ── 辅助 ──────────────────────────────────────────────────────────────────────

def _make_portfolio(cash: float = 1_000_000.0) -> Portfolio:
    """创建一个指定初始资金的空账户（不依赖 conftest fixture，方便参数化）。"""
    return Portfolio(initial_capital=cash)


class TestFeeCalc:
    def test_commission_waives_min_floor(self, trade_engine: TradeEngine):
        # 万一免五：1000 元 × 0.0001 = 0.1 元，不再触发最低 5 元
        assert trade_engine.calc_commission(1000) == pytest.approx(0.1)

    def test_commission_above_min(self, trade_engine: TradeEngine):
        # 100_000 元 × 0.0001 = 10 元
        assert trade_engine.calc_commission(100_000) == pytest.approx(10.0)

    def test_stamp_duty(self, trade_engine: TradeEngine):
        # 10_000 元 × 0.0005 = 5.0 元
        assert trade_engine.calc_stamp_duty(10_000) == 5.0

    def test_transfer_fee(self, trade_engine: TradeEngine):
        assert trade_engine.calc_transfer_fee(10_000) == pytest.approx(0.1)


class TestSlippage:
    def test_buy_adds_percent(self, trade_engine: TradeEngine):
        result = trade_engine._apply_slippage(10.0, OrderSide.BUY)
        # 10 * (1 + 0.001) = 10.01
        assert result == pytest.approx(10.01)

    def test_sell_subtracts(self, trade_engine: TradeEngine):
        result = trade_engine._apply_slippage(10.0, OrderSide.SELL)
        # 10 - 0.01 = 9.99
        assert result == pytest.approx(9.99)


class TestMarketBuy:
    def test_basic(self, trade_engine, portfolio: Portfolio, bar):
        portfolio.reserve_cash(2000)
        order = Order(code="510300.SH", side=OrderSide.BUY, qty=100)
        fills = trade_engine.execute_orders(
            [order], portfolio, {"510300.SH": bar}, "20240101"
        )
        assert len(fills) == 1
        # next_open 模式：以 open=10.0 + 滑点 0.001 → 10.01
        assert fills[0].price == pytest.approx(10.01)

    def test_t_plus_one_blocks(self, trade_engine, portfolio: Portfolio, bar):
        # 当日买入后立刻卖出
        portfolio.reserve_cash(2000)
        portfolio.apply_buy_fill("510300.SH", 100, 10.0, "20240101")
        sell = Order(code="510300.SH", side=OrderSide.SELL, qty=100)
        fills = trade_engine.execute_orders(
            [sell], portfolio, {"510300.SH": bar}, "20240101"
        )
        assert fills == []  # T+1 阻断


class TestLimitsAndStops:
    def test_up_limit_blocks_buy(self, trade_engine, portfolio: Portfolio, bar_factory):
        # prev_close=10，bar.open=11.1 已达涨停（+10%）
        b = bar_factory(open_=11.1, high=11.1, low=11.0, close=11.1, prev_close=10.0)
        portfolio.reserve_cash(5000)
        order = Order(code="510300.SH", side=OrderSide.BUY, qty=100)
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": b}, "20240101")
        assert fills == []

    def test_down_limit_blocks_sell(self, trade_engine, portfolio: Portfolio, bar_factory):
        # 持仓充足，但 bar.open=9.0 跌停
        b = bar_factory(open_=9.0, high=9.0, low=9.0, close=9.0, prev_close=10.0)
        portfolio.apply_buy_fill("510300.SH", 100, 10.0, "20231231")
        portfolio.positions["510300.SH"].update_sellable("20240101")
        order = Order(code="510300.SH", side=OrderSide.SELL, qty=100)
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": b}, "20240101")
        assert fills == []

    def test_volume_limit_truncates(self, trade_engine, portfolio: Portfolio, bar_factory):
        # 当日成交量 1000 股，volume_limit=0.1 → 上限 100 股
        b = bar_factory(volume=1000)
        portfolio.reserve_cash(50_000)
        order = Order(code="510300.SH", side=OrderSide.BUY, qty=500)
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": b}, "20240101")
        assert len(fills) == 1
        assert fills[0].qty == 100  # 截断到 volume_limit


class TestBoardSpecificPriceLimit:
    """板块涨跌停规则集成测试（PRD_20260520_08）。"""

    def test_star_market_allows_15_percent(self, trade_engine, portfolio: Portfolio, bar_factory):
        # 科创板 ±20%：prev_close=100，open=115（+15%）应放行
        b = bar_factory(open_=115.0, high=115.0, low=115.0, close=115.0,
                        prev_close=100.0, volume=1_000_000)
        portfolio.reserve_cash(50_000)
        order = Order(code="688981.SH", side=OrderSide.BUY, qty=100)
        fills = trade_engine.execute_orders([order], portfolio, {"688981.SH": b}, "20240101")
        assert len(fills) == 1

    def test_star_market_blocks_at_20_percent(self, trade_engine, portfolio: Portfolio, bar_factory):
        # 科创板涨停：prev_close=100，open=120（+20%）应拦截
        b = bar_factory(open_=120.0, high=120.0, low=120.0, close=120.0,
                        prev_close=100.0, volume=1_000_000)
        portfolio.reserve_cash(50_000)
        order = Order(code="688981.SH", side=OrderSide.BUY, qty=100)
        fills = trade_engine.execute_orders([order], portfolio, {"688981.SH": b}, "20240101")
        assert fills == []

    def test_chinext_after_reform_allows_15_percent(self, trade_engine, portfolio: Portfolio, bar_factory):
        # 创业板 2020-08-24 后 ±20%：+15% 应放行
        b = bar_factory(open_=115.0, high=115.0, low=115.0, close=115.0,
                        prev_close=100.0, volume=1_000_000)
        portfolio.reserve_cash(50_000)
        order = Order(code="300750.SZ", side=OrderSide.BUY, qty=100)
        fills = trade_engine.execute_orders([order], portfolio, {"300750.SZ": b}, "20240101")
        assert len(fills) == 1

    def test_chinext_before_reform_blocks_at_12_percent(self, trade_engine, portfolio: Portfolio, bar_factory):
        # 创业板 2020-08-23 前仍是 ±10%：+12% 应拦截
        b = bar_factory(open_=112.0, high=112.0, low=112.0, close=112.0,
                        prev_close=100.0, volume=1_000_000)
        portfolio.reserve_cash(50_000)
        order = Order(code="300750.SZ", side=OrderSide.BUY, qty=100)
        fills = trade_engine.execute_orders([order], portfolio, {"300750.SZ": b}, "20200823")
        assert fills == []

    def test_chinext_on_reform_day_allows_15_percent(self, trade_engine, portfolio: Portfolio, bar_factory):
        # 创业板 2020-08-24 当天起 ±20%：+15% 应放行
        b = bar_factory(open_=115.0, high=115.0, low=115.0, close=115.0,
                        prev_close=100.0, volume=1_000_000)
        portfolio.reserve_cash(50_000)
        order = Order(code="300750.SZ", side=OrderSide.BUY, qty=100)
        fills = trade_engine.execute_orders([order], portfolio, {"300750.SZ": b}, "20200824")
        assert len(fills) == 1

    def test_main_board_blocks_at_12_percent(self, trade_engine, portfolio: Portfolio, bar_factory):
        # 主板始终 ±10%：+12% 应拦截
        b = bar_factory(open_=112.0, high=112.0, low=112.0, close=112.0,
                        prev_close=100.0, volume=1_000_000)
        portfolio.reserve_cash(50_000)
        order = Order(code="600000.SH", side=OrderSide.BUY, qty=100)
        fills = trade_engine.execute_orders([order], portfolio, {"600000.SH": b}, "20240101")
        assert fills == []


class TestLimitOrder:
    def test_limit_buy_triggers(self, trade_engine, portfolio: Portfolio, bar_factory):
        b = bar_factory(open_=10.5, high=11.0, low=9.5, close=10.8, prev_close=10.5)
        portfolio.reserve_cash(2000)
        order = Order(code="510300.SH", side=OrderSide.BUY, qty=100,
                      order_type=OrderType.LIMIT, price=10.0)
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": b}, "20240101")
        assert len(fills) == 1
        # 限价单不加滑点，按 order.price 成交
        assert fills[0].price == pytest.approx(10.0)

    def test_limit_buy_skips(self, trade_engine, portfolio: Portfolio, bar_factory):
        # bar.low=9.5 > price=9.0，不应触发
        b = bar_factory(open_=10.5, high=11.0, low=9.5, close=10.8, prev_close=10.5)
        portfolio.reserve_cash(2000)
        order = Order(code="510300.SH", side=OrderSide.BUY, qty=100,
                      order_type=OrderType.LIMIT, price=9.0)
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": b}, "20240101")
        assert fills == []

    def test_limit_sell_triggers(self, trade_engine, portfolio: Portfolio, bar_factory):
        # 持仓充足
        portfolio.apply_buy_fill("510300.SH", 100, 10.0, "20231231")
        portfolio.positions["510300.SH"].update_sellable("20240101")
        # bar.high=11.0 ≥ price=10.8，触发
        b = bar_factory(open_=10.5, high=11.0, low=9.5, close=10.8, prev_close=10.5)
        order = Order(code="510300.SH", side=OrderSide.SELL, qty=100,
                      order_type=OrderType.LIMIT, price=10.8)
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": b}, "20240101")
        assert len(fills) == 1
        assert fills[0].price == pytest.approx(10.8)


class TestStopOrder:
    def test_stop_sell_at_worse_price(self, trade_engine, portfolio: Portfolio, bar_factory):
        # 持仓充足
        portfolio.apply_buy_fill("510300.SH", 100, 10.0, "20231231")
        portfolio.positions["510300.SH"].update_sellable("20240101")
        # bar.low=9.0 ≤ stop=9.5 触发
        # min(open=9.8, stop=9.5) = 9.5（卖出更不利价）
        b = bar_factory(open_=9.8, high=10.0, low=9.0, close=9.2, prev_close=10.0)
        order = Order(code="510300.SH", side=OrderSide.SELL, qty=100,
                      order_type=OrderType.STOP, stop_price=9.5)
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": b}, "20240101")
        assert len(fills) == 1
        assert fills[0].price == pytest.approx(9.5)


class TestStampDutyOnSellOnly:
    def test_buy_no_stamp(self, trade_engine, portfolio: Portfolio, bar):
        portfolio.reserve_cash(2000)
        order = Order(code="510300.SH", side=OrderSide.BUY, qty=100)
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": bar}, "20240101")
        assert fills[0].stamp_duty == 0.0

    def test_sell_has_stamp(self, trade_engine, portfolio: Portfolio, bar):
        portfolio.apply_buy_fill("510300.SH", 100, 10.0, "20231231")
        portfolio.positions["510300.SH"].update_sellable("20240101")
        order = Order(code="510300.SH", side=OrderSide.SELL, qty=100)
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": bar}, "20240101")
        assert fills[0].stamp_duty > 0


class TestOrderQtyWarning:
    def test_truncates_with_warning(self, caplog):
        import logging

        # Order 不足 100 股时应截断为 0 并 WARNING
        with caplog.at_level(logging.WARNING):
            o = Order(code="510300.SH", side=OrderSide.BUY, qty=50)

        assert o.qty == 0
        assert any("不是 100 的倍数" in r.message for r in caplog.records)


class TestListingDates:
    """新股首日 + listing_dates 注入（PRD_20260520_10）。"""

    def _make_engine(self, **kwargs):
        return TradeEngine(
            commission_rate=0.00025,
            min_commission=5.0,
            stamp_duty_rate=0.0005,
            transfer_fee_rate=0.00001,
            slippage_type="percent",
            slippage_value=0.0,
            volume_limit=1.0,
            price_type="next_open",
            **kwargs,
        )

    def _make_bar(self, open_=10.0, close=10.0, prev_close=10.0) -> pd.Series:
        """构造一个 prev_close = 5.0 的 bar，用于验证新股涨跌停放行。"""
        return pd.Series(
            {
                "open": open_,
                "high": open_ * 2,
                "low": open_,
                "close": close,
                "volume": 10_000_000,
                "prev_close": prev_close,
            }
        )

    def test_set_listing_dates_updates_dict(self):
        engine = self._make_engine()
        engine.set_listing_dates({"688999.SH": "20240105", "832000.BJ": "20240108"})
        assert engine.listing_dates["688999.SH"] == "20240105"
        assert engine.listing_dates["832000.BJ"] == "20240108"

    def test_new_listing_order_passes_via_engine_dict(self):
        """engine.listing_dates 注入后，新股首日订单按涨跌幅 ±100% 放行（价格不被拦截）。

        prev_close=5, open=8：
          - 正常 ±20% 上限 = 5*1.2 = 6 → open=8 > 6，会被拦截
          - 新股首日 ±100% 上限 = 5*2.0 = 10 → open=8 < 10，不会被拦截
        """
        engine = self._make_engine()
        engine.set_listing_dates({"688999.SH": "20240105"})
        portfolio = Portfolio(1_000_000.0)
        portfolio.reserve_cash(500_000)

        bar = self._make_bar(open_=8.0, prev_close=5.0)
        order = Order(code="688999.SH", side=OrderSide.BUY, qty=100)
        fills = engine.execute_orders([order], portfolio, {"688999.SH": bar}, "20240105")
        assert len(fills) == 1, "新股首日订单应成功成交"

    def test_order_list_date_overrides_engine_dict(self):
        """order.list_date 优先级高于 engine.listing_dates。"""
        engine = self._make_engine()
        # engine 字典给了一个更早的 list_date（已过首 5 日窗口）
        engine.set_listing_dates({"688999.SH": "20231201"})
        portfolio = Portfolio(1_000_000.0)
        portfolio.reserve_cash(500_000)

        # prev_close=5, open=8 → 正常 ±20% 上限=6，会被拦截；新股 ±100% 上限=10，不拦截
        bar = self._make_bar(open_=8.0, prev_close=5.0)
        # order 自带正确的 list_date = 当天 → 应以 order.list_date 为准，新股首日放行
        order = Order(code="688999.SH", side=OrderSide.BUY, qty=100, list_date="20240105")
        fills = engine.execute_orders([order], portfolio, {"688999.SH": bar}, "20240105")
        assert len(fills) == 1, "order.list_date 应覆盖 engine 字典，新股首日放行"

    def test_normal_order_blocked_at_up_limit(self):
        """不传 list_date 时科创板 ±20% 正常拦截。"""
        engine = self._make_engine()
        portfolio = Portfolio(1_000_000.0)
        portfolio.reserve_cash(500_000)

        bar = self._make_bar(open_=12.0, prev_close=5.0)
        order = Order(code="688999.SH", side=OrderSide.BUY, qty=100)
        fills = engine.execute_orders([order], portfolio, {"688999.SH": bar}, "20240105")
        assert len(fills) == 0, "科创板 ±20% 拦截：open=12 > prev_close*1.2=6"

    def test_order_list_date_to_dict_roundtrip(self):
        """Order.list_date 经 to_dict / from_dict 往返不丢失。"""
        order = Order(
            code="688999.SH",
            side=OrderSide.BUY,
            qty=100,
            list_date="20240105",
        )
        restored = Order.from_dict(order.to_dict())
        assert restored.list_date == "20240105"

    def test_order_without_list_date_roundtrip(self):
        """无 list_date 的旧 Order 向后兼容。"""
        old_dict = {
            "code": "510300.SH",
            "side": "buy",
            "qty": 100,
            "order_type": "market",
            "price": None,
            "stop_price": None,
            # 无 list_date 字段
        }
        order = Order.from_dict(old_dict)
        assert order.list_date is None


# ═════════════════════════════════════════════════════════════════════════════
# PRD_20260520_10  限价/止损单挂单池、过期与取消机制
# 验收标准 AC-9.1 ~ AC-9.7（含若干边界用例）
# ═════════════════════════════════════════════════════════════════════════════

class TestPendingOrderPool:
    """挂单池：LIMIT/STOP 单生命周期——入池、跨 Bar 触发、过期、取消。"""

    # ── AC-9.1: execute_orders 未触发 → 入池 ────────────────────────────────

    def test_ac91_limit_buy_enters_pending(self, trade_engine, portfolio, bar_factory):
        """LIMIT 买单 bar.low > limit：未触发，进入挂单池。"""
        # bar.low=9.5 > limit=9.0，不触发
        b = bar_factory(open_=10.0, high=10.5, low=9.5, close=10.2, volume=1_000_000)
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
        )
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": b}, "20240101")
        assert fills == []
        assert order.order_id in trade_engine.pending_orders

    def test_ac91_stop_sell_enters_pending(self, trade_engine, portfolio, bar_factory):
        """STOP 卖单 bar.low > stop_price：未触发，进入挂单池。"""
        portfolio.apply_buy_fill("510300.SH", 100, 10.0, "20231231")
        portfolio.positions["510300.SH"].update_sellable("20240101")
        # bar.low=9.5 > stop_price=9.0，不触发
        b = bar_factory(open_=10.0, high=10.5, low=9.5, close=10.2, volume=1_000_000)
        order = Order(
            code="510300.SH", side=OrderSide.SELL, qty=100,
            order_type=OrderType.STOP, stop_price=9.0,
        )
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": b}, "20240101")
        assert fills == []
        assert order.order_id in trade_engine.pending_orders

    # ── AC-9.2: sweep_pending 下一根 bar 触发 ────────────────────────────────

    def test_ac92_limit_sell_fills_next_bar(self, trade_engine, portfolio, bar_factory):
        """LIMIT 卖单在第二根 bar bar.high ≥ limit 时由 sweep_pending 成交。"""
        portfolio.apply_buy_fill("510300.SH", 100, 10.0, "20231231")
        portfolio.positions["510300.SH"].update_sellable("20240101")

        # Bar-1: bar.high=10.3 < limit=10.5 → 不触发，进池
        b1 = bar_factory(open_=10.0, high=10.3, low=9.5, close=10.2, volume=1_000_000)
        order = Order(
            code="510300.SH", side=OrderSide.SELL, qty=100,
            order_type=OrderType.LIMIT, price=10.5,
        )
        fills1 = trade_engine.execute_orders([order], portfolio, {"510300.SH": b1}, "20240101")
        assert fills1 == []
        assert order.order_id in trade_engine.pending_orders

        # Bar-2: bar.high=11.0 ≥ limit=10.5 → 触发成交
        b2 = bar_factory(open_=10.6, high=11.0, low=10.4, close=10.8, volume=1_000_000)
        fills2 = trade_engine.sweep_pending(
            portfolio, {"510300.SH": b2}, "20240102", is_last_bar_of_day=False
        )
        assert len(fills2) == 1
        assert fills2[0].price == pytest.approx(10.5)
        assert order.order_id not in trade_engine.pending_orders  # 成交后移除

    def test_ac92_limit_buy_fills_next_bar(self, trade_engine, portfolio, bar_factory):
        """LIMIT 买单在第二根 bar bar.low ≤ limit 时由 sweep_pending 成交。"""
        # Bar-1: bar.low=9.5 > limit=9.0 → 不触发
        b1 = bar_factory(open_=10.0, high=10.5, low=9.5, close=10.2, volume=1_000_000)
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
        )
        trade_engine.execute_orders([order], portfolio, {"510300.SH": b1}, "20240101")
        assert order.order_id in trade_engine.pending_orders

        # Bar-2: bar.low=8.8 ≤ limit=9.0 → 触发，按 limit=9.0 成交
        b2 = bar_factory(open_=9.2, high=9.4, low=8.8, close=9.0, volume=1_000_000)
        fills2 = trade_engine.sweep_pending(
            portfolio, {"510300.SH": b2}, "20240102", is_last_bar_of_day=False
        )
        assert len(fills2) == 1
        assert fills2[0].price == pytest.approx(9.0)
        assert order.order_id not in trade_engine.pending_orders

    # ── AC-9.3: DAY 单在当日最后 bar 过期 ───────────────────────────────────

    def test_ac93_day_order_expires_at_eod(self, trade_engine, portfolio, bar_factory):
        """DAY 单 is_last_bar_of_day=True 且未触发时，从挂单池中移除。"""
        b = bar_factory(open_=10.0, high=10.5, low=9.5, close=10.2, volume=1_000_000)
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
            time_in_force="DAY",
        )
        trade_engine.add_pending(order)
        assert order.order_id in trade_engine.pending_orders

        # is_last_bar_of_day=True → DAY 单应在此 bar 结束时过期
        trade_engine.sweep_pending(
            portfolio, {"510300.SH": b}, "20240101", is_last_bar_of_day=True
        )
        assert order.order_id not in trade_engine.pending_orders

    def test_ac93_day_order_not_expired_mid_day(self, trade_engine, portfolio, bar_factory):
        """DAY 单 is_last_bar_of_day=False 时不过期。"""
        b = bar_factory(open_=10.0, high=10.5, low=9.5, close=10.2, volume=1_000_000)
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
            time_in_force="DAY",
        )
        trade_engine.add_pending(order)

        # 非最后一根 bar → 不过期
        trade_engine.sweep_pending(
            portfolio, {"510300.SH": b}, "20240101", is_last_bar_of_day=False
        )
        assert order.order_id in trade_engine.pending_orders

    # ── AC-9.4: GTC 单跨日存活，直到触发 ────────────────────────────────────

    def test_ac94_gtc_order_survives_across_days_and_fills(
        self, trade_engine, portfolio, bar_factory
    ):
        """GTC 单（无 expire_date）跨越多个 is_last_bar_of_day=True，保留至触发。"""
        b_miss = bar_factory(open_=10.0, high=10.5, low=9.5, close=10.2, volume=1_000_000)
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
            time_in_force="GTC",
        )
        trade_engine.add_pending(order)

        # Day-1 收盘：GTC 不过期
        trade_engine.sweep_pending(
            portfolio, {"510300.SH": b_miss}, "20240101", is_last_bar_of_day=True
        )
        assert order.order_id in trade_engine.pending_orders

        # Day-2 收盘：仍不过期
        trade_engine.sweep_pending(
            portfolio, {"510300.SH": b_miss}, "20240102", is_last_bar_of_day=True
        )
        assert order.order_id in trade_engine.pending_orders

        # Day-3：bar.low=8.5 ≤ limit=9.0 → 触发成交
        b_hit = bar_factory(open_=9.2, high=9.4, low=8.5, close=8.8, volume=1_000_000)
        fills = trade_engine.sweep_pending(
            portfolio, {"510300.SH": b_hit}, "20240103", is_last_bar_of_day=False
        )
        assert len(fills) == 1
        assert fills[0].price == pytest.approx(9.0)
        assert order.order_id not in trade_engine.pending_orders

    # ── AC-9.5: GTC 单 expire_date 过期 ─────────────────────────────────────

    def test_ac95_gtc_order_not_expired_on_expire_date(
        self, trade_engine, portfolio, bar_factory
    ):
        """GTC 单 current_date == expire_date：尚未过期，保留在池中。"""
        b = bar_factory(open_=10.0, high=10.5, low=9.5, close=10.2, volume=1_000_000)
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
            time_in_force="GTC", expire_date="20240103",
        )
        trade_engine.add_pending(order)

        # current_date == expire_date → "20240103" > "20240103" is False → 不过期
        trade_engine.sweep_pending(
            portfolio, {"510300.SH": b}, "20240103", is_last_bar_of_day=True
        )
        assert order.order_id in trade_engine.pending_orders

    def test_ac95_gtc_order_expired_after_expire_date(
        self, trade_engine, portfolio, bar_factory
    ):
        """GTC 单 current_date > expire_date：过期，从池中移除。"""
        b = bar_factory(open_=10.0, high=10.5, low=9.5, close=10.2, volume=1_000_000)
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
            time_in_force="GTC", expire_date="20240103",
        )
        trade_engine.add_pending(order)

        # current_date > expire_date → "20240104" > "20240103" is True → 过期
        trade_engine.sweep_pending(
            portfolio, {"510300.SH": b}, "20240104", is_last_bar_of_day=False
        )
        assert order.order_id not in trade_engine.pending_orders

    def test_ac95_gtc_no_expire_date_never_expires(self, trade_engine, portfolio, bar_factory):
        """GTC 单 expire_date=None：无论跨多少日，永不因时间而过期。"""
        b = bar_factory(open_=10.0, high=10.5, low=9.5, close=10.2, volume=1_000_000)
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
            time_in_force="GTC", expire_date=None,
        )
        trade_engine.add_pending(order)

        for date in ["20240101", "20240201", "20241231", "20250101"]:
            trade_engine.sweep_pending(
                portfolio, {"510300.SH": b}, date, is_last_bar_of_day=True
            )
        # 从未触发也从未过期
        assert order.order_id in trade_engine.pending_orders

    # ── AC-9.6: cancel() 取消挂单 ────────────────────────────────────────────

    def test_ac96_cancel_removes_from_pool(self, trade_engine):
        """cancel(order_id) 成功返回 True 并从池中移除。"""
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
        )
        trade_engine.add_pending(order)
        assert order.order_id in trade_engine.pending_orders

        result = trade_engine.cancel(order.order_id)
        assert result is True
        assert order.order_id not in trade_engine.pending_orders

    def test_ac96_cancel_unknown_id_returns_false(self, trade_engine):
        """cancel() 对不存在的 order_id 返回 False，不抛错。"""
        assert trade_engine.cancel("nonexistent_id_xyz") is False

    def test_ac96_cancel_twice_returns_false_second_time(self, trade_engine):
        """同一 order_id cancel 两次：第一次 True，第二次 False。"""
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
        )
        trade_engine.add_pending(order)
        assert trade_engine.cancel(order.order_id) is True
        assert trade_engine.cancel(order.order_id) is False

    # ── AC-9.7: 回归——市价单行为不变 ─────────────────────────────────────────

    def test_ac97_market_order_fills_immediately(self, trade_engine, portfolio, bar):
        """（回归）市价单立即成交，不进入挂单池。"""
        portfolio.reserve_cash(2000)
        order = Order(code="510300.SH", side=OrderSide.BUY, qty=100)
        fills = trade_engine.execute_orders([order], portfolio, {"510300.SH": bar}, "20240101")
        assert len(fills) == 1
        assert fills[0].price == pytest.approx(10.01)  # next_open + 0.1% slippage
        assert len(trade_engine.pending_orders) == 0

    # ── 额外边界用例 ──────────────────────────────────────────────────────────

    def test_order_id_auto_generated_and_unique(self):
        """Order.order_id 在构造时自动生成，且不同 Order 的 ID 彼此不同。"""
        o1 = Order(code="510300.SH", side=OrderSide.BUY, qty=100)
        o2 = Order(code="510300.SH", side=OrderSide.BUY, qty=100)
        assert o1.order_id  # 非空
        assert o2.order_id  # 非空
        assert o1.order_id != o2.order_id

    def test_market_order_cannot_enter_pending(self, trade_engine):
        """add_pending 拒绝 MARKET 单，抛 ValueError。"""
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.MARKET,
        )
        with pytest.raises(ValueError, match="MARKET"):
            trade_engine.add_pending(order)

    def test_invalid_tif_raises(self):
        """time_in_force 非 DAY/GTC 时，Order 构造抛 ValueError。"""
        with pytest.raises(ValueError, match="time_in_force"):
            Order(
                code="510300.SH", side=OrderSide.BUY, qty=100,
                order_type=OrderType.LIMIT, price=10.0,
                time_in_force="FOK",
            )

    def test_day_order_with_expire_date_raises(self):
        """DAY 单指定 expire_date 时，Order 构造抛 ValueError。"""
        with pytest.raises(ValueError):
            Order(
                code="510300.SH", side=OrderSide.BUY, qty=100,
                order_type=OrderType.LIMIT, price=10.0,
                time_in_force="DAY", expire_date="20240201",
            )

    def test_sweep_skips_fill_on_insufficient_cash(self, trade_engine, bar_factory):
        """买单资金不足时 sweep_pending 跳过撮合，但保留挂单（GTC 不过期）。"""
        tiny_portfolio = _make_portfolio(cash=10.0)  # 远不足 100股×9.0=900元
        b = bar_factory(open_=9.2, high=9.4, low=8.5, close=8.8, volume=1_000_000)
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
            time_in_force="GTC",
        )
        trade_engine.add_pending(order)
        fills = trade_engine.sweep_pending(
            tiny_portfolio, {"510300.SH": b}, "20240101", is_last_bar_of_day=False
        )
        assert fills == []
        assert order.order_id in trade_engine.pending_orders  # 资金不足但单子保留

    def test_sweep_no_bar_data_order_retained(self, trade_engine, portfolio):
        """挂单对应代码在当前 bar 无行情时，单子保留（GTC 不过期）。"""
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
            time_in_force="GTC",
        )
        trade_engine.add_pending(order)
        # bar_data 里没有该代码
        fills = trade_engine.sweep_pending(
            portfolio, {}, "20240101", is_last_bar_of_day=False
        )
        assert fills == []
        assert order.order_id in trade_engine.pending_orders

    def test_sweep_no_bar_day_order_expires(self, trade_engine, portfolio):
        """无行情时 DAY 单在 is_last_bar_of_day=True 依然过期。"""
        order = Order(
            code="510300.SH", side=OrderSide.BUY, qty=100,
            order_type=OrderType.LIMIT, price=9.0,
            time_in_force="DAY",
        )
        trade_engine.add_pending(order)
        # bar_data 空，但 is_last_bar_of_day=True → DAY 单应过期
        trade_engine.sweep_pending(
            portfolio, {}, "20240101", is_last_bar_of_day=True
        )
        assert order.order_id not in trade_engine.pending_orders
