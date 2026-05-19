"""FIFO 配对盈亏计算单元测试（依赖 PRD_20260520_02）。"""

import math

import pandas as pd
import pytest

from analytics.metrics import _pair_fifo, calculate_metrics
from engine.trade_engine import Fill, OrderSide


def _fill(side, qty, price, comm=5.0, stamp=0.0, transfer=0.0):
    return Fill(
        code="510300.SH",
        side=side,
        qty=qty,
        price=price,
        commission=comm,
        stamp_duty=stamp,
        transfer_fee=transfer,
    )


class TestPairFifo:
    def test_all_winning(self):
        fills = [
            _fill(OrderSide.BUY, 100, 10.0),
            _fill(OrderSide.SELL, 100, 12.0, stamp=0.6),
        ]
        profits = _pair_fifo(fills)
        assert len(profits) == 1
        # 毛利 200，扣两侧费用（commission 各 5 + stamp 0.6 = 10.6）
        assert profits[0] == pytest.approx(200 - 10.6)

    def test_all_losing(self):
        fills = [
            _fill(OrderSide.BUY, 100, 10.0),
            _fill(OrderSide.SELL, 100, 8.0, stamp=0.4),
        ]
        profits = _pair_fifo(fills)
        assert len(profits) == 1
        assert profits[0] < 0

    def test_mixed(self):
        fills = [
            _fill(OrderSide.BUY, 100, 10.0),
            _fill(OrderSide.SELL, 100, 12.0, stamp=0.6),  # 盈
            _fill(OrderSide.BUY, 100, 12.0),
            _fill(OrderSide.SELL, 100, 11.0, stamp=0.5),  # 亏
        ]
        profits = _pair_fifo(fills)
        assert len(profits) == 2
        assert profits[0] > 0
        assert profits[1] < 0

    def test_partial_unmatched_buy_ignored(self):
        # 买 200，卖 100：第二个买入未平仓，不进入 profits
        fills = [
            _fill(OrderSide.BUY, 200, 10.0),
            _fill(OrderSide.SELL, 100, 12.0, stamp=0.6),
        ]
        profits = _pair_fifo(fills)
        assert len(profits) == 1

    def test_fifo_order(self):
        # 两笔买入价格不同，FIFO 第一笔应配对第一笔卖出
        fills = [
            _fill(OrderSide.BUY, 100, 10.0),   # 应被先卖出
            _fill(OrderSide.BUY, 100, 12.0),
            _fill(OrderSide.SELL, 100, 11.0, stamp=0.55),  # 配对 10.0 买入 → 盈利
        ]
        profits = _pair_fifo(fills)
        assert len(profits) == 1
        # 毛利 (11-10)*100 = 100，扣费用
        assert profits[0] > 0


class TestCalculateMetrics:
    def _make_nav(self, values, freq="D"):
        # 构造带 DatetimeIndex 的 nav DataFrame
        idx = pd.date_range("2024-01-01", periods=len(values), freq=freq)
        return pd.DataFrame({"nav": values}, index=idx)

    def test_win_rate_all_winners(self):
        nav = self._make_nav([1_000_000, 1_010_000, 1_020_000])
        fills = [
            _fill(OrderSide.BUY, 100, 10.0),
            _fill(OrderSide.SELL, 100, 12.0, stamp=0.6),
        ]
        result = calculate_metrics(nav, fills=fills, frequency="daily")
        assert result.total_trades == 1
        assert result.win_rate == pytest.approx(1.0)
        # 无亏损 → pl_ratio = inf
        assert math.isinf(result.profit_loss_ratio)

    def test_win_rate_mixed(self):
        nav = self._make_nav([1_000_000, 1_005_000, 1_002_000, 1_004_000])
        fills = [
            _fill(OrderSide.BUY, 100, 10.0),
            _fill(OrderSide.SELL, 100, 12.0, stamp=0.6),  # 盈
            _fill(OrderSide.BUY, 100, 12.0),
            _fill(OrderSide.SELL, 100, 11.0, stamp=0.55),  # 亏
        ]
        result = calculate_metrics(nav, fills=fills, frequency="daily")
        assert result.total_trades == 2
        assert result.win_rate == pytest.approx(0.5)
        # 盈利 ~ 190+，亏损 ~ 100+，pl_ratio > 1
        assert result.profit_loss_ratio > 0
        assert not math.isinf(result.profit_loss_ratio)

    def test_no_fills(self):
        nav = self._make_nav([1_000_000, 1_005_000, 1_010_000])
        result = calculate_metrics(nav, fills=[], frequency="daily")
        assert result.total_trades == 0
        assert result.win_rate == 0.0
        assert result.profit_loss_ratio == 0.0
