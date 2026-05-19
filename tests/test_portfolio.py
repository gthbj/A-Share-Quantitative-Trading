"""Portfolio 单元测试。"""

import pytest

from account.portfolio import Portfolio


class TestReserveCash:
    def test_success(self, portfolio: Portfolio):
        ok = portfolio.reserve_cash(100_000)
        assert ok is True
        assert portfolio.available_cash == 900_000
        assert portfolio.frozen_cash == 100_000

    def test_insufficient(self, portfolio: Portfolio):
        ok = portfolio.reserve_cash(2_000_000)
        assert ok is False
        assert portfolio.available_cash == 1_000_000
        assert portfolio.frozen_cash == 0


class TestReleaseCash:
    def test_release_partial(self, portfolio: Portfolio):
        portfolio.reserve_cash(100_000)
        portfolio.release_cash(40_000)
        assert portfolio.available_cash == 940_000
        assert portfolio.frozen_cash == 60_000

    def test_release_caps_at_frozen(self, portfolio: Portfolio):
        portfolio.reserve_cash(100_000)
        # 释放超过 frozen 的金额，按 frozen 上限处理
        portfolio.release_cash(200_000)
        assert portfolio.frozen_cash == 0
        assert portfolio.available_cash == 1_000_000


class TestBeforeTrading:
    def test_releases_residual_frozen(self, portfolio: Portfolio):
        portfolio.reserve_cash(100_000)
        # 当日有部分未用冻结，开盘前应全部释放
        portfolio.before_trading("20240102")
        assert portfolio.frozen_cash == 0
        assert portfolio.available_cash == 1_000_000


class TestApplyFills:
    def test_buy_fill_extracts_from_frozen(self, portfolio: Portfolio):
        portfolio.reserve_cash(100_000)
        portfolio.apply_buy_fill("510300.SH", 100, 10.0, "20240101")
        # 成交 1000 元，冻结池减少 1000
        assert portfolio.frozen_cash == 99_000
        assert "510300.SH" in portfolio.positions
        assert portfolio.positions["510300.SH"].total_qty == 100

    def test_sell_fill_increases_cash(self, portfolio: Portfolio):
        portfolio.reserve_cash(100_000)
        portfolio.apply_buy_fill("510300.SH", 100, 10.0, "20240101")
        portfolio.positions["510300.SH"].update_sellable("20240102")
        portfolio.before_trading("20240102")  # 释放残留 frozen
        cash_before = portfolio.available_cash
        portfolio.apply_sell_fill("510300.SH", 100, 11.0)
        # 卖出回笼 1100 元
        assert portfolio.available_cash == pytest.approx(cash_before + 1100)
        # 清仓后从 positions 移除
        assert "510300.SH" not in portfolio.positions


class TestTotalValue:
    def test_with_holdings(self, portfolio: Portfolio):
        portfolio.reserve_cash(100_000)
        portfolio.apply_buy_fill("510300.SH", 100, 10.0, "20240101")
        # available=900_000, frozen=99_000, 持仓 1000 元市值
        total = portfolio.total_value({"510300.SH": 11.0})
        # 900_000 + 99_000 + 100 * 11 = 1_000_100
        assert total == pytest.approx(1_000_100)

    def test_empty_account(self, portfolio: Portfolio):
        assert portfolio.total_value({}) == 1_000_000.0
