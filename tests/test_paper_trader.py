"""PaperTrader 单元测试（PRD_20260520_09）。

覆盖：
- 首次启动 / 状态加载与保存 / 老 state.json 向后兼容
- 订单延迟成交（next_open 语义）
- user_data 跨日续接
- 重复运行拦截
- 止损队列持久化
- user_data 不可序列化 → TypeError
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List

import pandas as pd
import pytest

from data_layer.base_data_source import BaseDataSource
from engine.paper_trader import PaperTrader
from engine.trade_engine import Order, OrderSide, OrderType, TradeEngine
from strategy.base_strategy import BaseStrategy, Context


# ============== 测试用 Stub 数据源 ==============


class StubDataSource(BaseDataSource):
    """内存数据源：按 code 返回固定 DataFrame。"""

    def __init__(self, frames: Dict[str, pd.DataFrame]) -> None:
        self._frames = frames

    def get_bars(self, code, start_date, end_date, period="daily", adjust="qfq"):
        df = self._frames.get(code, pd.DataFrame())
        if df.empty:
            return df
        mask = (df["date"].astype(str) >= str(start_date)) & (
            df["date"].astype(str) <= str(end_date)
        )
        return df.loc[mask].reset_index(drop=True)

    def get_stock_list(self):
        return pd.DataFrame()

    def get_index_constituents(self, index_code):
        return []


def _make_daily_frame(code: str, dates: List[str], close: float = 10.0) -> pd.DataFrame:
    """构造 N 天日线，open=low=high=close=同一价格，方便确定性撮合。"""
    return pd.DataFrame(
        [
            {
                "date": d,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 10_000_000,
                "amount": 10_000_000 * close,
            }
            for d in dates
        ]
    )


# ============== 测试用策略 ==============


class _RecordingStrategy(BaseStrategy):
    """每次 handle_data 都下一笔买入单，并在 user_data 累加计数。"""

    DEFAULT_UNIVERSE = ["510300.SH"]
    lookback_days = 5

    def __init__(self, universe=None, buy_qty: int = 100):
        super().__init__()
        self._init_universe = list(universe) if universe else list(self.DEFAULT_UNIVERSE)
        self.buy_qty = buy_qty

    def initialize(self, context):
        super().initialize(context)
        self.set_universe(self._init_universe)

    def handle_data(self, context, data):
        context.user_data["call_count"] = context.user_data.get("call_count", 0) + 1
        # 只有还没持仓时买入，避免反复堆积
        for code in self._universe:
            if not context.portfolio.has_position(code):
                context.order(code, self.buy_qty)


class _NoopStrategy(BaseStrategy):
    """什么都不做，用于测纯状态加载/保存。"""

    DEFAULT_UNIVERSE = ["510300.SH"]
    lookback_days = 5

    def __init__(self, universe=None):
        super().__init__()
        self._init_universe = list(universe) if universe else list(self.DEFAULT_UNIVERSE)

    def initialize(self, context):
        super().initialize(context)
        self.set_universe(self._init_universe)

    def handle_data(self, context, data):
        pass


# ============== Fixtures ==============


@pytest.fixture
def tmp_state(tmp_path: Path) -> Path:
    return tmp_path / "paper_state.json"


@pytest.fixture
def data_source() -> StubDataSource:
    dates = [f"2024010{d}" for d in range(1, 10)]  # 20240101 .. 20240109
    return StubDataSource(
        {"510300.SH": _make_daily_frame("510300.SH", dates, close=10.0)}
    )


@pytest.fixture
def trade_engine_noslip() -> TradeEngine:
    """关闭滑点的撮合引擎，方便断言成交价格。"""
    return TradeEngine(
        commission_rate=0.00025,
        min_commission=5.0,
        stamp_duty_rate=0.0005,
        transfer_fee_rate=0.00001,
        slippage_type="percent",
        slippage_value=0.0,
        volume_limit=0.10,
        price_type="next_open",
    )


# ============== 测试用例 ==============


class TestFirstRun:
    def test_requires_strategy_cls_on_first_run(self, tmp_state, data_source):
        pt = PaperTrader(data_source=data_source, state_file=str(tmp_state))
        with pytest.raises(ValueError, match="必须传入 strategy_cls"):
            pt.load_state()

    def test_initial_state_fresh(self, tmp_state, data_source):
        pt = PaperTrader(
            data_source=data_source,
            strategy_cls=_NoopStrategy,
            strategy_kwargs={"universe": ["510300.SH"]},
            state_file=str(tmp_state),
            initial_capital=500_000.0,
        )
        pt.load_state()
        assert pt.portfolio is not None
        assert pt.portfolio.initial_capital == 500_000.0
        assert pt.portfolio.available_cash == 500_000.0
        assert pt.user_data == {}
        assert pt.pending_orders == []


class TestStatePersistence:
    def test_save_then_load_roundtrip(self, tmp_state, data_source, trade_engine_noslip):
        # 跑一次产生订单
        pt1 = PaperTrader(
            data_source=data_source,
            strategy_cls=_RecordingStrategy,
            strategy_kwargs={"universe": ["510300.SH"], "buy_qty": 100},
            trade_engine=trade_engine_noslip,
            state_file=str(tmp_state),
            initial_capital=100_000.0,
        )
        pt1.run_once(date="20240105")

        assert tmp_state.exists()
        assert len(pt1.pending_orders) == 1  # handle_data 留下了买单
        assert pt1.user_data["call_count"] == 1
        assert pt1.last_run_date == "20240105"

        # 用 fresh 实例（不传 strategy_cls）重新加载
        pt2 = PaperTrader(data_source=data_source, state_file=str(tmp_state))
        pt2.load_state()
        # 策略类应从 state 反射出来
        assert pt2.strategy_cls is _RecordingStrategy
        assert pt2.strategy_kwargs == {"universe": ["510300.SH"], "buy_qty": 100}
        assert len(pt2.pending_orders) == 1
        assert pt2.pending_orders[0].code == "510300.SH"
        assert pt2.pending_orders[0].side == OrderSide.BUY
        assert pt2.pending_orders[0].qty == 100
        assert pt2.user_data["call_count"] == 1
        assert pt2.last_run_date == "20240105"

    def test_legacy_state_backward_compat(self, tmp_state, data_source):
        """老 state.json（缺新字段）能正确加载。"""
        legacy = {
            "initial_capital": 200_000,
            "available_cash": 180_000,
            "current_date": "20240105",
            "positions": {},
        }
        tmp_state.write_text(json.dumps(legacy), encoding="utf-8")

        pt = PaperTrader(
            data_source=data_source,
            strategy_cls=_NoopStrategy,
            state_file=str(tmp_state),
        )
        pt.load_state()
        assert pt.portfolio.initial_capital == 200_000
        assert pt.portfolio.available_cash == 180_000
        assert pt.user_data == {}
        assert pt.pending_orders == []
        assert pt.pending_stop_loss == {}
        assert pt.last_run_date is None


class TestNextOpenSemantics:
    def test_order_executes_on_next_run(self, tmp_state, data_source, trade_engine_noslip):
        """T 日 handle_data 产生的订单，T+1 才成交。"""
        pt = PaperTrader(
            data_source=data_source,
            strategy_cls=_RecordingStrategy,
            strategy_kwargs={"universe": ["510300.SH"], "buy_qty": 100},
            trade_engine=trade_engine_noslip,
            state_file=str(tmp_state),
            initial_capital=10_000.0,
        )
        # T = 20240103
        pt.run_once(date="20240103")
        # T 日订单尚未成交：仓位还是 0
        assert pt.portfolio.get_position("510300.SH") is None
        assert len(pt.pending_orders) == 1

        # T+1 = 20240104：上次的订单按开盘价 10.0 成交
        pt.run_once(date="20240104")
        pos = pt.portfolio.get_position("510300.SH")
        assert pos is not None
        assert pos.total_qty == 100
        # 又留下一笔新订单（_RecordingStrategy 每次都尝试买入，但已持仓所以不下单）
        # 但 user_data["call_count"] 应增加
        assert pt.user_data["call_count"] == 2


class TestUserDataPersistence:
    def test_user_data_carries_across_runs(self, tmp_state, data_source, trade_engine_noslip):
        pt = PaperTrader(
            data_source=data_source,
            strategy_cls=_RecordingStrategy,
            strategy_kwargs={"universe": ["510300.SH"]},
            trade_engine=trade_engine_noslip,
            state_file=str(tmp_state),
            initial_capital=10_000.0,
        )
        pt.run_once(date="20240103")
        pt.run_once(date="20240104")
        pt.run_once(date="20240105")
        # call_count 累加到 3
        assert pt.user_data["call_count"] == 3

        # 重新加载后值还在
        pt2 = PaperTrader(data_source=data_source, state_file=str(tmp_state))
        pt2.load_state()
        assert pt2.user_data["call_count"] == 3


class TestDuplicateRunGuard:
    def test_running_same_day_twice_is_noop(
        self, tmp_state, data_source, trade_engine_noslip, caplog
    ):
        pt = PaperTrader(
            data_source=data_source,
            strategy_cls=_RecordingStrategy,
            strategy_kwargs={"universe": ["510300.SH"]},
            trade_engine=trade_engine_noslip,
            state_file=str(tmp_state),
            initial_capital=10_000.0,
        )
        pt.run_once(date="20240103")
        cnt1 = pt.user_data["call_count"]

        # 同一天再跑：被拦截
        import logging
        with caplog.at_level(logging.WARNING):
            pt.run_once(date="20240103")
        assert any("已跑过" in r.message for r in caplog.records)
        # call_count 不变
        assert pt.user_data["call_count"] == cnt1


class TestStopLossPersistence:
    def test_stop_loss_queue_persists(
        self, tmp_path, trade_engine_noslip
    ):
        """触发止损时，pending_stop_loss 持久化到 state，下次 open 成交。"""
        state_file = tmp_path / "paper_state.json"
        # 构造一个先涨后跌的数据：先在 day1 买入、day2 暴跌触发止损
        dates = ["20240101", "20240102", "20240103", "20240104"]
        prices = [10.0, 10.0, 7.0, 7.0]  # day3 跌 30%
        df = pd.DataFrame(
            [
                {
                    "date": d,
                    "open": p,
                    "high": p,
                    "low": p,
                    "close": p,
                    "volume": 10_000_000,
                    "amount": 10_000_000 * p,
                }
                for d, p in zip(dates, prices)
            ]
        )
        ds = StubDataSource({"510300.SH": df})

        pt = PaperTrader(
            data_source=ds,
            strategy_cls=_RecordingStrategy,
            strategy_kwargs={"universe": ["510300.SH"], "buy_qty": 100},
            trade_engine=trade_engine_noslip,
            state_file=str(state_file),
            initial_capital=10_000.0,
            stop_loss_enabled=True,
            stop_loss_threshold=0.05,
        )
        # day1: handle_data 下买单（不成交）
        pt.run_once(date="20240101")
        assert len(pt.pending_orders) == 1
        # day2: 上次的买单按 open=10 成交，持仓 100 股
        pt.run_once(date="20240102")
        assert pt.portfolio.get_position("510300.SH").total_qty == 100
        # day3: 价格跌到 7（-30%），收盘后止损检查触发
        pt.run_once(date="20240103")
        assert "510300.SH" in pt.pending_stop_loss
        # day4: 止损单按开盘价 7 成交
        pt.run_once(date="20240104")
        assert pt.portfolio.get_position("510300.SH") is None


class TestUserDataSerializationGuard:
    def test_unserializable_user_data_raises(self, tmp_state, data_source):
        pt = PaperTrader(
            data_source=data_source,
            strategy_cls=_NoopStrategy,
            state_file=str(tmp_state),
            initial_capital=10_000.0,
        )
        pt.load_state()
        # 塞一个不可 JSON 序列化的对象
        pt.user_data["bad"] = object()
        with pytest.raises(TypeError):
            pt.save_state()
