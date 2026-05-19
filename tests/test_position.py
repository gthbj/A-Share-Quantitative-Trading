"""Position 单元测试。"""

import pytest

from account.position import Position


class TestApplyBuy:
    def test_first_buy_sets_cost(self):
        p = Position(code="510300.SH")
        p.apply_buy(100, 10.0, "20240101")
        assert p.total_qty == 100
        assert p.cost_price == 10.0

    def test_second_buy_weighted_average(self):
        p = Position(code="510300.SH")
        p.apply_buy(100, 10.0, "20240101")
        p.apply_buy(100, 12.0, "20240102")
        assert p.total_qty == 200
        # 加权平均：(100*10 + 100*12) / 200 = 11.0
        assert p.cost_price == pytest.approx(11.0)

    def test_zero_qty_noop(self):
        p = Position(code="510300.SH")
        p.apply_buy(0, 10.0, "20240101")
        assert p.total_qty == 0
        assert p.cost_price == 0.0


class TestApplySell:
    def test_partial_sell(self):
        p = Position(code="510300.SH")
        p.apply_buy(200, 10.0, "20240101")
        p.update_sellable("20240102")
        p.apply_sell(100, 11.0)
        assert p.total_qty == 100
        # 加权平均成本不变（清仓后才归零）
        assert p.cost_price == pytest.approx(10.0)

    def test_full_sell_clears(self):
        p = Position(code="510300.SH")
        p.apply_buy(100, 10.0, "20240101")
        p.update_sellable("20240102")
        p.apply_sell(100, 11.0)
        assert p.total_qty == 0
        assert p.cost_price == 0.0
        assert p._buy_records == {}

    def test_fifo_updates_buy_records(self):
        """关键防回归：apply_sell 必须 FIFO 减少 _buy_records，
        否则下一交易日 update_sellable 会把已卖份额重新算入 sellable_qty。"""
        p = Position(code="510300.SH")
        p.apply_buy(100, 10.0, "20240101")
        p.apply_buy(100, 12.0, "20240102")
        # 解冻第一笔买入
        p.update_sellable("20240102")
        assert p.sellable_qty == 100  # 仅 20240101 的 100 股
        # 卖出全部可卖（100 股）
        p.apply_sell(100, 11.0)
        # 20240101 应已从 _buy_records 移除，仅剩 20240102 的 100
        assert "20240101" not in p._buy_records
        assert p._buy_records.get("20240102") == 100
        # 第三天解冻：20240102 的 100 股应可卖
        p.update_sellable("20240103")
        assert p.sellable_qty == 100  # 不应该是 200（防虚高 bug）


class TestUpdateSellable:
    def test_t_plus_one(self):
        p = Position(code="510300.SH")
        p.apply_buy(100, 10.0, "20240101")
        # 当日买入不可卖
        p.update_sellable("20240101")
        assert p.sellable_qty == 0
        # 次日解冻
        p.update_sellable("20240102")
        assert p.sellable_qty == 100


class TestProfitRatio:
    def test_zero_cost_returns_zero(self):
        p = Position(code="510300.SH")
        assert p.profit_ratio(10.0) == 0.0

    def test_normal(self):
        p = Position(code="510300.SH")
        p.apply_buy(100, 10.0, "20240101")
        assert p.profit_ratio(11.0) == pytest.approx(0.10)
        assert p.profit_ratio(9.0) == pytest.approx(-0.10)
