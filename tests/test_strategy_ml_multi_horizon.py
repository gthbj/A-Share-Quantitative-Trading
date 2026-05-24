"""ml_multi_horizon_picker 单元测试。

覆盖：
- labels.py：多 horizon 标签 / sell 标签
- features.py：5 维 sell-side 风险特征
- regime.py：bull/neutral/bear 三态
- strategy.py：6 个 sell trigger 各自命中（独立场景）
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Dict, List, Optional

import numpy as np
import pandas as pd
import pytest

from account.portfolio import Portfolio
from strategy.ml_multi_horizon_picker.features import (
    SELL_FEATURE_COLUMNS,
    compute_sell_risk_features,
)
from strategy.ml_multi_horizon_picker.labels import (
    build_buy_labels_cross_section,
    build_sell_label,
    compute_horizon_return,
    compute_max_drawdown,
)
from strategy.ml_multi_horizon_picker.regime import (
    Regime,
    combine_regime_with_market_breadth,
    detect_regime,
    regime_position_multiplier,
    regime_stop_loss,
)
from strategy.ml_multi_horizon_picker.strategy import (
    MLMultiHorizonStrategy,
    PositionState,
)


# ─────────────────────────────────────────────────────────
# labels.py
# ─────────────────────────────────────────────────────────


def test_horizon_return_basic():
    close = pd.Series([10, 11, 12, 13, 14], dtype=float)
    ret = compute_horizon_return(close, horizon=1)
    # log(11)-log(10), log(12)-log(11), ...
    assert ret.iloc[0] == pytest.approx(np.log(11) - np.log(10))
    assert ret.iloc[3] == pytest.approx(np.log(14) - np.log(13))
    assert pd.isna(ret.iloc[-1])


def test_max_drawdown_negative_only():
    close = pd.Series([10, 9, 8, 7, 6], dtype=float)
    dd = compute_max_drawdown(close, lookforward=3)
    # 从 10 起：下 1/2/3 天最低 7（i=0..3），(7-10)/10 = -0.3
    assert dd.iloc[0] == pytest.approx(-0.3)
    assert pd.isna(dd.iloc[-1])


def test_max_drawdown_uptrend_zero_or_positive():
    close = pd.Series([10, 11, 12, 13], dtype=float)
    dd = compute_max_drawdown(close, lookforward=2)
    # 一直上涨，未来最低 11，比 10 高，所以是 +0.1
    assert dd.iloc[0] == pytest.approx(0.1)


def test_build_buy_labels_cross_section_basic():
    # 构造 3 天连续行情：t0, t1, t2。h=1 的标签可在 t0、t1 计算
    rows = []
    for i in range(12):
        # t0 close = 10+i, t1 close = (10+i) × (1 + 0.001*i)
        # i 越大未来涨越多
        rows.append({"date": "2024-01-01", "equity_code": f"S{i:02d}", "close": 10 + i})
        rows.append({
            "date": "2024-01-02", "equity_code": f"S{i:02d}",
            "close": (10 + i) * (1 + 0.001 * i),
        })
        rows.append({
            "date": "2024-01-03", "equity_code": f"S{i:02d}",
            "close": (10 + i) * (1 + 0.002 * i),
        })
    df = pd.DataFrame(rows)

    labeled = build_buy_labels_cross_section(df, horizons=[1], top_q=0.3, bottom_q=0.3)
    assert "label_h1" in labeled.columns
    # 2024-01-01 这一天有截面标签
    d1 = labeled[labeled["date"] == "2024-01-01"]
    valid = d1["label_h1"].dropna()
    assert len(valid) > 0, f"d1 全部 NaN，原始 future_ret={d1.get('future_ret_h1')}"
    assert set(valid.unique()) <= {0.0, 1.0}
    assert (valid == 1.0).sum() >= 1
    assert (valid == 0.0).sum() >= 1


def test_build_sell_label_basic():
    # 一只股票，未来 5 天最大跌 10%
    rows = [
        {"date": f"2024-01-{i+1:02d}", "equity_code": "S00", "close": 10 - 0.5 * i}
        for i in range(6)
    ]
    df = pd.DataFrame(rows)
    labeled = build_sell_label(df, lookforward=5, drawdown_threshold=-0.05)
    # 第 0 天：未来 5 天 close [9.5, 9.0, 8.5, 8.0, 7.5] 最低 7.5
    # (7.5-10)/10 = -0.25 < -0.05 → label_sell=1
    assert labeled["label_sell"].iloc[0] == 1.0


# ─────────────────────────────────────────────────────────
# features.py
# ─────────────────────────────────────────────────────────


def _toy_kline(n: int = 80) -> pd.DataFrame:
    """构造 n 天 K 线（高+1.0, 低-1.0, close 与 high 相同的简化序列）。"""
    np.random.seed(42)
    base = np.linspace(10, 15, n) + np.random.normal(0, 0.3, n)
    return pd.DataFrame(
        {
            "date": pd.date_range("2024-01-01", periods=n, freq="D").strftime("%Y-%m-%d"),
            "open": base,
            "high": base + 0.5,
            "low": base - 0.5,
            "close": base,
            "volume": np.random.randint(1_000_000, 5_000_000, n),
            "amount": np.random.uniform(1e7, 5e7, n),
            "rsi_14": np.random.uniform(30, 80, n),
            "ma_60": pd.Series(base).rolling(60, min_periods=1).mean().values,
        }
    )


def test_compute_sell_risk_features_columns_present():
    df = _toy_kline()
    out = compute_sell_risk_features(df)
    for col in [
        "drawdown_from_high_20d", "drawdown_from_high_60d",
        "vol_expansion", "rsi_overbought_streak", "dist_to_ma60",
    ]:
        assert col in out.columns


def test_drawdown_negative_when_below_high():
    df = pd.DataFrame({
        "date": ["2024-01-01", "2024-01-02", "2024-01-03"],
        "close": [10, 12, 11],
        "high":  [10, 12, 12],
        "rsi_14": [50, 50, 50],
    })
    out = compute_sell_risk_features(df)
    # 第 3 天 close=11，最近 20 日最高 12 → (11-12)/12 ≈ -0.0833
    assert out["drawdown_from_high_20d"].iloc[-1] == pytest.approx(-1/12, rel=1e-6)


def test_rsi_overbought_streak_counts_consecutive():
    df = pd.DataFrame({
        "date": [f"2024-01-{i+1:02d}" for i in range(6)],
        "close": [10] * 6,
        "high":  [10] * 6,
        "rsi_14": [50, 75, 75, 80, 60, 75],   # 索引 1/2/3 是连续超买
    })
    out = compute_sell_risk_features(df)
    # 索引 0 → 0, 1 → 1, 2 → 2, 3 → 3, 4 → 0, 5 → 1
    assert list(out["rsi_overbought_streak"]) == [0, 1, 2, 3, 0, 1]


# ─────────────────────────────────────────────────────────
# regime.py
# ─────────────────────────────────────────────────────────


def test_regime_bear_when_below_ma200():
    # 250 天数据，最近持续低于 MA200
    n = 250
    close = pd.Series(
        # 前 100 天上涨到 100，后 150 天慢慢回到 50
        np.concatenate([np.linspace(50, 100, 100), np.linspace(100, 50, 150)])
    )
    out = detect_regime(close, ma_window=200, vol_window=60)
    assert out.iloc[-1] == Regime.BEAR.value


def test_regime_bull_when_above_ma_low_vol():
    # 持续平稳上涨
    n = 260
    close = pd.Series(np.linspace(50, 100, n) + np.random.normal(0, 0.01, n))
    out = detect_regime(close, ma_window=200, vol_window=60)
    # 末尾应为 bull 或 neutral
    assert out.iloc[-1] in {Regime.BULL.value, Regime.NEUTRAL.value}


def test_regime_fast_selloff_turns_bear_before_long_ma_break():
    """快速下跌应作为风险预算开关提前进入 bear。"""
    close = pd.Series(
        np.concatenate([
            np.linspace(80, 120, 240),
            np.array([121, 120, 118, 115, 112, 108], dtype=float),
        ])
    )
    out = detect_regime(close, ma_window=200, vol_window=60)
    assert out.iloc[-1] == Regime.BEAR.value


def test_market_breadth_can_downgrade_bull_to_bear():
    """指数基础状态健康但股票池广度恶化时，应降为 bear。"""
    frame = pd.DataFrame({
        "close_to_ma20": [-0.02, -0.01, -0.03, 0.01],
        "return_20d": [-0.08, -0.04, -0.06, -0.01],
        "return_5d": [-0.04, -0.05, -0.02, -0.01],
    })
    assert (
        combine_regime_with_market_breadth(Regime.BULL.value, frame)
        == Regime.BEAR.value
    )


def test_regime_helpers():
    assert regime_position_multiplier(Regime.BULL.value) == 1.0
    assert regime_position_multiplier(Regime.NEUTRAL.value) == 0.5
    assert regime_position_multiplier(Regime.BEAR.value) == 0.0
    assert regime_stop_loss(Regime.BEAR.value, 0.05, 0.03) == 0.03
    assert regime_stop_loss(Regime.BULL.value, 0.05, 0.03) == 0.05


# ─────────────────────────────────────────────────────────
# strategy.py — 6 sell trigger 独立场景
# ─────────────────────────────────────────────────────────


class _StubContext:
    """最小可用 Context stub，仅满足 strategy 内部使用的方法/属性。"""

    def __init__(self, portfolio: Portfolio, current_date: str):
        self.portfolio = portfolio
        self.current_date = current_date
        self.orders: List[tuple] = []

    def order(self, code: str, amount: int) -> None:
        self.orders.append((code, amount))


def _build_strategy_with_held(
    code: str,
    cost: float,
    qty: int,
    entered_date: str,
    expected_horizon: int = 5,
    peak_price: Optional[float] = None,
    rank_dropout_streak: int = 0,
) -> tuple:
    """构造一个 strategy + portfolio，并预置一个持仓 + state。"""
    strat = MLMultiHorizonStrategy(
        target_position_count=10,
        use_deterministic_fallback=True,
    )
    portfolio = Portfolio(initial_capital=1_000_000)
    # 直接 mock 一个持仓
    from account.position import Position
    pos = Position(code=code)
    pos.total_qty = qty
    pos.sellable_qty = qty
    pos.cost_price = cost
    portfolio.positions[code] = pos
    portfolio.available_cash -= cost * qty

    strat._position_state[code] = PositionState(
        entered_date=entered_date,
        expected_horizon=expected_horizon,
        peak_price=peak_price if peak_price is not None else cost,
        rank_dropout_streak=rank_dropout_streak,
    )
    return strat, portfolio


def _score_df_for(code: str, prob_h5: float, prob_sell: float = 0.1) -> pd.DataFrame:
    return pd.DataFrame([{
        "code": code,
        "prob_up_h1": prob_h5,
        "prob_up_h5": prob_h5,
        "prob_up_h10": prob_h5,
        "prob_up_h20": prob_h5,
        "prob_sell": prob_sell,
        "score": prob_h5,
    }])


def test_sell_trigger_stop_loss():
    """用例 1: 硬止损"""
    strat, portfolio = _build_strategy_with_held(
        code="600000.SH", cost=10.0, qty=1000, entered_date="20240101"
    )
    ctx = _StubContext(portfolio, "20240105")
    data = {"600000.SH": pd.Series({"close": 9.40})}  # -6%
    score_df = _score_df_for("600000.SH", prob_h5=0.6)
    sells = strat._check_sell_triggers(
        context=ctx, score_df=score_df, top_2n_codes={"600000.SH"},
        stop_loss_pct=0.05, data=data,
    )
    assert "600000.SH" in sells
    assert "stop_loss" in sells["600000.SH"]


def test_sell_trigger_trailing_stop():
    """用例 2: 追踪止盈"""
    strat, portfolio = _build_strategy_with_held(
        code="600000.SH", cost=10.0, qty=1000, entered_date="20240101",
        peak_price=12.0,
    )
    ctx = _StubContext(portfolio, "20240110")
    data = {"600000.SH": pd.Series({"close": 11.60})}  # 12 → 11.60 ≈ -3.3%
    score_df = _score_df_for("600000.SH", prob_h5=0.6)
    sells = strat._check_sell_triggers(
        context=ctx, score_df=score_df, top_2n_codes={"600000.SH"},
        stop_loss_pct=0.05, data=data,
    )
    assert "600000.SH" in sells
    assert "trailing_stop" in sells["600000.SH"]


def test_sell_trigger_horizon_expired():
    """用例 3: Horizon 到期"""
    strat, portfolio = _build_strategy_with_held(
        code="600000.SH", cost=10.0, qty=1000,
        entered_date="20240101", expected_horizon=5,
    )
    ctx = _StubContext(portfolio, "20240108")  # 7 天后，超过 5 天 horizon
    data = {"600000.SH": pd.Series({"close": 10.20})}
    score_df = _score_df_for("600000.SH", prob_h5=0.6)
    sells = strat._check_sell_triggers(
        context=ctx, score_df=score_df, top_2n_codes={"600000.SH"},
        stop_loss_pct=0.05, data=data,
    )
    assert "600000.SH" in sells
    assert "horizon_expired" in sells["600000.SH"]


def test_sell_trigger_rank_dropout():
    """用例 4: 排名迟滞触发"""
    strat, portfolio = _build_strategy_with_held(
        code="600000.SH", cost=10.0, qty=1000,
        entered_date="20240101",
        expected_horizon=20,                # 避免 horizon 优先触发
        rank_dropout_streak=1,              # 之前已累积 1 天
    )
    ctx = _StubContext(portfolio, "20240108")  # 持有 7 天 > min_hold_days=3
    data = {"600000.SH": pd.Series({"close": 10.20})}
    # 候选股票中没有 600000.SH，即不在 top_2n_codes
    score_df = _score_df_for("600000.SH", prob_h5=0.4)
    sells = strat._check_sell_triggers(
        context=ctx, score_df=score_df,
        top_2n_codes=set(),  # 持仓不在 top-2N
        stop_loss_pct=0.05, data=data,
    )
    assert "600000.SH" in sells
    assert "rank_dropout" in sells["600000.SH"]


def test_sell_trigger_prob_floor():
    """用例 5: prob_up 兜底触发"""
    strat, portfolio = _build_strategy_with_held(
        code="600000.SH", cost=10.0, qty=1000, entered_date="20240101",
        expected_horizon=20,
    )
    ctx = _StubContext(portfolio, "20240108")
    data = {"600000.SH": pd.Series({"close": 10.20})}
    score_df = _score_df_for("600000.SH", prob_h5=0.20)  # < min_prob_floor=0.30
    sells = strat._check_sell_triggers(
        context=ctx, score_df=score_df,
        top_2n_codes={"600000.SH"},   # 仍在排名内
        stop_loss_pct=0.05, data=data,
    )
    assert "600000.SH" in sells
    assert "prob_floor" in sells["600000.SH"]


def test_sell_trigger_sell_model():
    """用例 6: 卖出模型触发"""
    strat, portfolio = _build_strategy_with_held(
        code="600000.SH", cost=10.0, qty=1000, entered_date="20240101",
        expected_horizon=20,
    )
    ctx = _StubContext(portfolio, "20240108")
    data = {"600000.SH": pd.Series({"close": 10.20})}
    score_df = _score_df_for("600000.SH", prob_h5=0.65, prob_sell=0.85)  # > 0.70
    sells = strat._check_sell_triggers(
        context=ctx, score_df=score_df,
        top_2n_codes={"600000.SH"},
        stop_loss_pct=0.05, data=data,
    )
    assert "600000.SH" in sells
    assert "sell_model" in sells["600000.SH"]


def test_no_sell_when_all_conditions_clean():
    """所有触发条件都不命中时不卖。"""
    strat, portfolio = _build_strategy_with_held(
        code="600000.SH", cost=10.0, qty=1000, entered_date="20240101",
        expected_horizon=20, peak_price=10.5,
    )
    ctx = _StubContext(portfolio, "20240105")  # 持有 4 天，未到 20d horizon
    data = {"600000.SH": pd.Series({"close": 10.30})}  # 在 cost 和 peak 之间
    score_df = _score_df_for("600000.SH", prob_h5=0.60, prob_sell=0.20)
    sells = strat._check_sell_triggers(
        context=ctx, score_df=score_df,
        top_2n_codes={"600000.SH"},
        stop_loss_pct=0.05, data=data,
    )
    assert "600000.SH" not in sells


def test_default_regime_risk_budget_counts():
    """默认风险预算适合 10 万资金：bull 5 / neutral 3 / bear 0。"""
    strat = MLMultiHorizonStrategy()
    assert strat.regime_target_position_counts == {
        Regime.BULL.value: 5,
        Regime.NEUTRAL.value: 3,
        Regime.BEAR.value: 0,
    }
    assert strat.regime_position_pcts == {
        Regime.BULL.value: 0.85,
        Regime.NEUTRAL.value: 0.45,
        Regime.BEAR.value: 0.0,
    }


def test_bear_risk_budget_clears_sellable_positions():
    """bear 是风险预算约束：所有可卖持仓都应清掉。"""
    strat, portfolio = _build_strategy_with_held(
        code="600000.SH", cost=10.0, qty=1000, entered_date="20240101"
    )
    from account.position import Position
    pos2 = Position(code="000001.SZ")
    pos2.total_qty = 500
    pos2.sellable_qty = 500
    pos2.cost_price = 20.0
    portfolio.positions["000001.SZ"] = pos2

    ctx = _StubContext(portfolio, "20240105")
    score_df = pd.DataFrame([
        {"code": "600000.SH", "score": 0.8},
        {"code": "000001.SZ", "score": 0.7},
    ])
    data = {
        "600000.SH": pd.Series({"close": 10.2}),
        "000001.SZ": pd.Series({"close": 19.8}),
    }
    sells = strat._risk_budget_sells(
        context=ctx,
        score_df=score_df,
        regime=Regime.BEAR.value,
        target_n=0,
        position_pct=0.0,
        data=data,
        existing_sells=set(),
    )
    assert sells == {
        "600000.SH": "bear_risk_budget_clear",
        "000001.SZ": "bear_risk_budget_clear",
    }


def test_neutral_risk_budget_sells_lowest_score_when_too_many_positions():
    """neutral 风险预算最多 3 只；超过预算时卖掉最低评分。"""
    strat, portfolio = _build_strategy_with_held(
        code="600000.SH", cost=10.0, qty=1000, entered_date="20240101"
    )
    from account.position import Position
    for code, cost in [
        ("000001.SZ", 20.0),
        ("000002.SZ", 30.0),
        ("000003.SZ", 40.0),
    ]:
        pos = Position(code=code)
        pos.total_qty = 500
        pos.sellable_qty = 500
        pos.cost_price = cost
        portfolio.positions[code] = pos

    ctx = _StubContext(portfolio, "20240105")
    score_df = pd.DataFrame([
        {"code": "600000.SH", "score": 0.8},
        {"code": "000001.SZ", "score": 0.1},
        {"code": "000002.SZ", "score": 0.7},
        {"code": "000003.SZ", "score": 0.6},
    ])
    data = {
        "600000.SH": pd.Series({"close": 10.2}),
        "000001.SZ": pd.Series({"close": 20.0}),
        "000002.SZ": pd.Series({"close": 30.0}),
        "000003.SZ": pd.Series({"close": 40.0}),
    }
    sells = strat._risk_budget_sells(
        context=ctx,
        score_df=score_df,
        regime=Regime.NEUTRAL.value,
        target_n=3,
        position_pct=0.90,
        data=data,
        existing_sells=set(),
    )
    assert sells == {
        "000001.SZ": "risk_budget_count(regime=neutral,target_n=3)",
    }


def test_argmax_horizon():
    """动态 horizon 选择：max(h × prob_h)"""
    row = pd.Series({
        "prob_up_h5": 0.6,    # 5 × 0.6 = 3.0
        "prob_up_h10": 0.5,   # 10 × 0.5 = 5.0
        "prob_up_h20": 0.4,   # 20 × 0.4 = 8.0
    })
    assert MLMultiHorizonStrategy._argmax_horizon(row) == 20

    row2 = pd.Series({"prob_up_h5": 0.9, "prob_up_h10": 0.3, "prob_up_h20": 0.1})
    # 5 × 0.9 = 4.5; 10 × 0.3 = 3.0; 20 × 0.1 = 2.0
    assert MLMultiHorizonStrategy._argmax_horizon(row2) == 5
