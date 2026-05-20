"""TradeEngine 单元测试。"""

import pytest

from account.portfolio import Portfolio
from engine.trade_engine import (
    Fill,
    Order,
    OrderSide,
    OrderType,
    TradeEngine,
)


class TestFeeCalc:
    def test_commission_min_floor(self, trade_engine: TradeEngine):
        # 1000 元 × 0.00025 = 0.25 元 → 触发最低 5 元
        assert trade_engine.calc_commission(1000) == 5.0

    def test_commission_above_min(self, trade_engine: TradeEngine):
        # 100_000 元 × 0.00025 = 25 元
        assert trade_engine.calc_commission(100_000) == 25.0

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
