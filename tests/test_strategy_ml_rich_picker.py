"""ml_rich_picker 单元测试（PRD_20260525_03）。

覆盖：
- 富特征列定义（30 维 buy / 39 维 sell）
- deterministic_rich_score / deterministic_rich_sell_score 兜底逻辑
- MLRichPickerStrategy 构造（继承父类）
- walk_forward 模块 import 不报错
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from account.portfolio import Portfolio
from account.position import Position
from strategy.ml_rich_picker import (
    DAILY_FEATURE_COLUMNS,
    DEFAULT_MAX_REMAINING_DAYS,
    EVENT_FEATURE_COLUMNS,
    FUNDAMENTAL_FEATURE_COLUMNS,
    POSITION_STATE_FEATURE_COLUMNS,
    RICH_BUY_FEATURE_COLUMNS,
    RICH_SELL_FEATURE_COLUMNS,
    SELL_REGRESSION_FEATURE_COLUMNS,
    SELL_REMAINING_DAYS_MODEL_NAME,
    MLRichPickerStrategy,
    deterministic_optimal_remaining_days,
    deterministic_rich_score,
    deterministic_rich_sell_score,
    remaining_days_to_prob_sell,
)
from strategy.ml_multi_horizon_picker.features import SELL_RISK_FEATURE_COLUMNS
from strategy.ml_multi_horizon_picker.model_storage import BUY_HORIZONS
from strategy.ml_multi_horizon_picker.strategy import MLMultiHorizonStrategy, PositionState


# ──────────────────────────────────────────────────────────────
# 特征列定义
# ──────────────────────────────────────────────────────────────


def test_daily_feature_count():
    """日线特征 17 维（与 v1 完全一致）。"""
    assert len(DAILY_FEATURE_COLUMNS) == 17


def test_fundamental_feature_count():
    """基本面 8 维。"""
    assert len(FUNDAMENTAL_FEATURE_COLUMNS) == 8


def test_event_feature_count():
    """事件/资金流 5 维。"""
    assert len(EVENT_FEATURE_COLUMNS) == 5


def test_rich_buy_feature_count():
    """PRD §9 用例 1：Buy 特征 30 维 = 17 + 8 + 5。"""
    assert len(RICH_BUY_FEATURE_COLUMNS) == 30
    # 顺序：daily → fundamental → event
    assert RICH_BUY_FEATURE_COLUMNS[:17] == DAILY_FEATURE_COLUMNS
    assert RICH_BUY_FEATURE_COLUMNS[17:25] == FUNDAMENTAL_FEATURE_COLUMNS
    assert RICH_BUY_FEATURE_COLUMNS[25:30] == EVENT_FEATURE_COLUMNS


def test_rich_sell_feature_count():
    """[兼容] 旧 39 维 sell schema 保留供 sell_v1 binary 模型 fallback。"""
    assert len(RICH_SELL_FEATURE_COLUMNS) == 39
    # 前 30 维与 buy 一致，然后 5 维 sell-side 风险，最后 4 维持仓状态
    assert RICH_SELL_FEATURE_COLUMNS[:30] == RICH_BUY_FEATURE_COLUMNS
    assert RICH_SELL_FEATURE_COLUMNS[30:35] == SELL_RISK_FEATURE_COLUMNS
    assert RICH_SELL_FEATURE_COLUMNS[35:] == POSITION_STATE_FEATURE_COLUMNS


def test_sell_regression_feature_count():
    """Sell 回归模型 35 维 = 30 buy + 5 sell-side risk。"""
    assert len(SELL_REGRESSION_FEATURE_COLUMNS) == 35
    assert SELL_REGRESSION_FEATURE_COLUMNS[:30] == RICH_BUY_FEATURE_COLUMNS
    assert SELL_REGRESSION_FEATURE_COLUMNS[30:] == SELL_RISK_FEATURE_COLUMNS
    # 关键约束：sell 回归模型不能含 position state
    for col in POSITION_STATE_FEATURE_COLUMNS:
        assert col not in SELL_REGRESSION_FEATURE_COLUMNS, (
            f"sell 回归模型不应包含 position state 列 {col}"
        )


def test_no_duplicate_features():
    """30 维 buy 特征不能有重复列名。"""
    assert len(set(RICH_BUY_FEATURE_COLUMNS)) == len(RICH_BUY_FEATURE_COLUMNS)


def test_fundamental_feature_keys():
    """基本面 8 维必含的关键列。"""
    must_have = ["pe_basic", "pb", "roe", "log_market_cap"]
    for col in must_have:
        assert col in FUNDAMENTAL_FEATURE_COLUMNS, f"缺少 {col}"


def test_event_feature_keys():
    """事件/资金流 5 维必含的关键列。"""
    must_have = ["dragon_tiger_net_pct", "main_net_inflow_pct", "limit_up_streak"]
    for col in must_have:
        assert col in EVENT_FEATURE_COLUMNS, f"缺少 {col}"


# ──────────────────────────────────────────────────────────────
# Deterministic fallback
# ──────────────────────────────────────────────────────────────


def _toy_rich_features(n: int = 5) -> pd.DataFrame:
    """构造 5 只股票的 rich 特征 DataFrame（用于 fallback 测试）。"""
    np.random.seed(7)
    df = pd.DataFrame({col: np.random.randn(n) for col in DAILY_FEATURE_COLUMNS})
    df["rsi_14"] = np.random.uniform(30, 70, n)
    # 基本面：5 只里有 1 只是 NaN（模拟新股缺基本面）
    for col in FUNDAMENTAL_FEATURE_COLUMNS:
        df[col] = np.concatenate([np.random.uniform(0.5, 30, n - 1), [np.nan]])
    # 事件：随机
    for col in EVENT_FEATURE_COLUMNS:
        df[col] = np.random.uniform(-0.1, 0.1, n)
    return df


def _indexed_rich_features(codes: list[str] | None = None) -> pd.DataFrame:
    """构造 _score_universe 可直接查表的 indexed rich features。"""
    codes = codes or ["000001.SZ", "600000.SH"]
    rows = []
    for i, code in enumerate(codes):
        row = {
            "date": "20240102",
            "equity_code": code,
            "open": 9.8 + i,
            "close": 10.0 + i,
            "high": 10.5 + i,
            "low": 9.5 + i,
            "ma_60": 9.5 + i,
            "amount": 1_000_000.0,
        }
        for col in RICH_SELL_FEATURE_COLUMNS:
            row[col] = 0.1
        row.update(
            {
                "rsi_14": 50.0,
                "pe_basic": 10.0,
                "pb": 1.5,
                "roe": 0.15,
                "debt_to_assets": 0.35,
                "dragon_tiger_net_pct": 0.0,
                "drawdown_from_high_20d": 0.0,
                "vol_expansion": 1.0,
                "rsi_overbought_streak": 0.0,
                "return_5d": 0.02,
            }
        )
        rows.append(row)
    return pd.DataFrame(rows).set_index(["date", "equity_code"])


class _ConstantModel:
    def __init__(self, value: float = 0.6):
        self.value = value

    def predict(self, X):
        return np.full(len(X), self.value)


class _FailingModel:
    def predict(self, X):
        raise ValueError("dimension mismatch")


class _PicklableTaggedModel:
    """Module-level fake model（local class 无法 pickle，故移到模块顶层）。"""

    def __init__(self, tag: str = "anonymous"):
        self.tag = tag

    def predict(self, X):
        return np.zeros(len(X), dtype=float)


def _rich_strategy_with_features(use_fallback: bool = False) -> MLRichPickerStrategy:
    codes = ["000001.SZ", "600000.SH"]
    strat = MLRichPickerStrategy(
        universe=codes,
        require_rich_features=True,
        use_deterministic_fallback=use_fallback,
    )
    strat._universe = codes
    strat._rich_features = _indexed_rich_features(codes)
    return strat


def test_deterministic_rich_score_returns_series():
    """fallback 函数返回长度匹配的 Series。"""
    df = _toy_rich_features()
    out = deterministic_rich_score(df)
    assert isinstance(out, pd.Series)
    assert len(out) == len(df)
    # 不应抛 NaN（fundamental NaN 应被 fillna(0) 处理）
    assert not out.isna().all()


def test_deterministic_rich_fallback_handles_missing_columns():
    """fallback 单独调用时，缺少 rich 列也不应因为标量默认值崩溃。"""
    df = pd.DataFrame({"return_5d": [0.05, -0.02]}, index=["a", "b"])

    buy = deterministic_rich_score(df)
    sell = deterministic_rich_sell_score(pd.DataFrame(index=df.index))

    assert len(buy) == len(df)
    assert len(sell) == len(df)
    assert np.isfinite(buy.to_numpy()).all()
    assert ((sell >= 0) & (sell <= 1)).all()


def test_deterministic_rich_sell_score_in_range():
    """sell fallback 输出 [0, 1] 概率。"""
    df = _toy_rich_features()
    # 补 sell-side 风险特征
    for col in SELL_RISK_FEATURE_COLUMNS:
        df[col] = np.random.uniform(-0.5, 0.5, len(df))
    out = deterministic_rich_sell_score(df)
    assert (out >= 0).all() and (out <= 1).all()


def test_score_universe_allows_optional_buy_models_missing_without_fallback():
    """正式 rich 模式：非 decision_horizon 的 buy 模型缺失不应阻塞交易。"""
    strat = _rich_strategy_with_features(use_fallback=False)
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS}
    strat._models.pop("buy_h10")
    strat._models[SELL_REMAINING_DAYS_MODEL_NAME] = _ConstantModel()

    class _FakeCtx:
        current_date = "20240102"

    out = strat._score_universe(_FakeCtx())

    assert out is not None
    assert out["prob_up_h10"].isna().all()
    assert out["score"].notna().all()


def test_score_universe_raises_when_decision_buy_model_missing_without_fallback():
    """正式 rich 模式：decision_horizon 的 buy 模型缺失必须失败。"""
    strat = _rich_strategy_with_features(use_fallback=False)
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS if h != 5}
    strat._models[SELL_REMAINING_DAYS_MODEL_NAME] = _ConstantModel()

    class _FakeCtx:
        current_date = "20240102"

    with pytest.raises(RuntimeError, match="buy_h5 模型缺失"):
        strat._score_universe(_FakeCtx())


def test_score_universe_raises_when_sell_model_missing_without_fallback():
    """正式 rich 模式：sell 模型缺失也应失败，不静默用 deterministic sell。"""
    strat = _rich_strategy_with_features(use_fallback=False)
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS}

    class _FakeCtx:
        current_date = "20240102"

    with pytest.raises(RuntimeError, match=f"{SELL_REMAINING_DAYS_MODEL_NAME} 模型缺失"):
        strat._score_universe(_FakeCtx())


def test_score_universe_raises_when_prediction_fails_without_fallback():
    """正式 rich 模式：模型维度不匹配 / predict 失败应直接暴露。"""
    strat = _rich_strategy_with_features(use_fallback=False)
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS}
    strat._models["buy_h5"] = _FailingModel()
    strat._models[SELL_REMAINING_DAYS_MODEL_NAME] = _ConstantModel()

    class _FakeCtx:
        current_date = "20240102"

    with pytest.raises(RuntimeError, match="buy_h5 模型预测失败"):
        strat._score_universe(_FakeCtx())


def test_score_universe_allows_missing_models_only_when_fallback_enabled():
    """显式 fallback 调试模式仍可用 deterministic rich 分数。"""
    strat = _rich_strategy_with_features(use_fallback=True)
    strat._models = {}

    class _FakeCtx:
        current_date = "20240102"

    out = strat._score_universe(_FakeCtx())
    assert out is not None
    assert set(["prob_up_h1", "prob_up_h5", "prob_up_h10", "prob_up_h20", "prob_sell"]).issubset(out.columns)
    assert len(out) == 2


def test_score_universe_retains_features_for_parent_regime_breadth():
    """rich score_df 必须保留父类 regime 广度判断所需的可见特征。"""
    strat = _rich_strategy_with_features(use_fallback=False)
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS}
    strat._models[SELL_REMAINING_DAYS_MODEL_NAME] = _ConstantModel()

    class _FakeCtx:
        current_date = "20240102"

    out = strat._score_universe(_FakeCtx())

    assert out is not None
    assert {"return_5d", "return_20d", "close_to_ma20"}.issubset(out.columns)
    assert {"pe_basic", "main_net_inflow_pct"}.issubset(out.columns)


def test_score_universe_uses_fixed_decision_horizon_score():
    """不同 horizon 不再取 max，交易排序固定使用 decision_horizon。"""
    strat = _rich_strategy_with_features(use_fallback=False)
    strat._models = {
        "buy_h1": _ConstantModel(0.9),
        "buy_h5": _ConstantModel(0.4),
        "buy_h10": _ConstantModel(0.8),
        "buy_h20": _ConstantModel(0.7),
        SELL_REMAINING_DAYS_MODEL_NAME: _ConstantModel(0.2),
    }

    class _FakeCtx:
        current_date = "20240102"

    out = strat._score_universe(_FakeCtx())

    assert out is not None
    assert (out["score"] == out["prob_up_h5"]).all()


def test_position_state_features_use_rich_adjusted_prices_not_execution_cost():
    """sell 持仓状态特征用 qfq rich 价格，不能把 qfq close 除以 none 成本价。"""
    strat = MLRichPickerStrategy(require_rich_features=True)
    rich = pd.DataFrame(
        [
            {
                "date": "20240103",
                "equity_code": "000001.SZ",
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 102.0,
                "ma_60": 90.0,
                "amount": 1_000_000.0,
            },
            {
                "date": "20240104",
                "equity_code": "000001.SZ",
                "open": 110.0,
                "high": 130.0,
                "low": 109.0,
                "close": 120.0,
                "ma_60": 90.0,
                "amount": 1_000_000.0,
            },
        ]
    )
    for col in RICH_SELL_FEATURE_COLUMNS:
        if col not in rich.columns:
            rich[col] = 0.1
    strat._rich_features = rich.set_index(["date", "equity_code"])
    strat._trading_date_index = {
        "20240103": 0,
        "20240104": 1,
    }
    strat._position_state["000001.SZ"] = PositionState(
        entered_date="20240102",
        expected_horizon=5,
        peak_price=10.0,
    )
    portfolio = Portfolio(initial_capital=100_000)
    portfolio.apply_buy_fill("000001.SZ", 100, 10.0, "20240103")

    ctx = SimpleNamespace(
        current_date="20240104",
        portfolio=portfolio,
        all_bars={},
    )

    today_df = rich[rich["date"] == "20240104"].copy()
    out = strat._position_state_features(ctx, today_df)

    assert out.loc[today_df.index[0], "position_return"] == pytest.approx(0.20)
    assert out.loc[today_df.index[0], "drawdown_from_position_peak"] == pytest.approx(
        120.0 / 130.0 - 1.0
    )


def test_position_state_features_use_live_position_when_state_missing():
    """断点续跑 / 冷启动时，已有持仓没有 _position_state 也应给 sell 模型真实状态。"""
    strat = MLRichPickerStrategy(require_rich_features=True)
    rich = pd.DataFrame(
        [
            {
                "date": "20240102",
                "equity_code": "000001.SZ",
                "open": 10.0,
                "close": 10.0,
                "high": 10.5,
            },
            {
                "date": "20240103",
                "equity_code": "000001.SZ",
                "open": 11.0,
                "close": 12.0,
                "high": 12.5,
            },
        ]
    )
    strat._rich_features = rich.set_index(["date", "equity_code"])
    strat._trading_date_index = {"20240102": 0, "20240103": 1}
    portfolio = Portfolio(100_000)
    pos = Position(code="000001.SZ", total_qty=100, sellable_qty=100, cost_price=10.0)
    pos._buy_records["20240102"] = 100
    portfolio.positions["000001.SZ"] = pos
    ctx = SimpleNamespace(
        current_date="20240103",
        portfolio=portfolio,
        all_bars={},
    )

    today_df = rich[rich["date"] == "20240103"].copy()
    out = strat._position_state_features(ctx, today_df)

    assert out.loc[today_df.index[0], "holding_days"] == pytest.approx(1.0)
    assert out.loc[today_df.index[0], "position_return"] == pytest.approx(0.20)
    assert out.loc[today_df.index[0], "days_to_expected_horizon"] == pytest.approx(4.0)


def test_score_universe_dynamic_liquidity_universe():
    """回测交易 universe 每天按近 N 日成交额动态收敛，而不是首日静态。"""
    codes = ["000001.SZ", "600000.SH"]
    rows = []
    for date, amounts in [
        ("20240101", [1_000.0, 10_000.0]),
        ("20240102", [1_000.0, 20_000.0]),
    ]:
        frame = _indexed_rich_features(codes).reset_index()
        frame["date"] = date
        frame["amount"] = amounts
        rows.append(frame)
    features = pd.concat(rows, ignore_index=True).set_index(["date", "equity_code"])
    strat = MLRichPickerStrategy(
        require_rich_features=True,
        use_deterministic_fallback=False,
        universe_source="liquidity_top",
        liquidity_top_n=1,
    )
    strat._explicit_universe = False
    strat._universe = codes
    strat._rich_features = features
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS}
    strat._models[SELL_REMAINING_DAYS_MODEL_NAME] = _ConstantModel()

    class _FakeCtx:
        current_date = "20240102"

    out = strat._score_universe(_FakeCtx())

    assert out is not None
    assert out["code"].tolist() == ["600000.SH"]
    assert strat.get_universe() == codes


def test_score_universe_scores_held_position_outside_dynamic_liquidity_top():
    """持仓股即使掉出当日 Top-N，也要进入 score_df 供 sell_model / prob_floor 使用。"""
    codes = ["000001.SZ", "600000.SH", "000002.SZ"]
    rows = []
    for date, amounts in [
        ("20240101", [1_000.0, 10_000.0, 500.0]),
        ("20240102", [1_000.0, 20_000.0, 500.0]),
    ]:
        frame = _indexed_rich_features(codes).reset_index()
        frame["date"] = date
        frame["amount"] = amounts
        rows.append(frame)
    features = pd.concat(rows, ignore_index=True).set_index(["date", "equity_code"])
    strat = MLRichPickerStrategy(
        require_rich_features=True,
        use_deterministic_fallback=False,
        universe_source="liquidity_top",
        liquidity_top_n=1,
    )
    strat._explicit_universe = False
    strat._universe = codes
    strat._rich_features = features
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS}
    strat._models[SELL_REMAINING_DAYS_MODEL_NAME] = _ConstantModel()
    portfolio = Portfolio(100_000)
    pos = Position(code="000001.SZ", total_qty=100, sellable_qty=100, cost_price=10.0)
    pos._buy_records["20240101"] = 100
    portfolio.positions["000001.SZ"] = pos

    ctx = SimpleNamespace(current_date="20240102", portfolio=portfolio, all_bars={})

    out = strat._score_universe(ctx)

    assert out is not None
    assert out["code"].tolist() == ["000001.SZ", "600000.SH"]
    assert strat.get_universe() == codes


def test_score_universe_normalizes_hyphenated_current_date():
    strat = _rich_strategy_with_features(use_fallback=False)
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS}
    strat._models[SELL_REMAINING_DAYS_MODEL_NAME] = _ConstantModel()

    out = strat._score_universe(SimpleNamespace(current_date="2024-01-02"))

    assert out is not None
    assert set(out["code"]) == {"000001.SZ", "600000.SH"}


# ──────────────────────────────────────────────────────────────
# Strategy 继承关系
# ──────────────────────────────────────────────────────────────


def test_strategy_subclass_of_v1():
    """MLRichPickerStrategy 继承自 MLMultiHorizonStrategy（共享 sell trigger 逻辑）。"""
    assert issubclass(MLRichPickerStrategy, MLMultiHorizonStrategy)


def test_strategy_construct_minimal():
    """最小构造不应报错。"""
    strat = MLRichPickerStrategy(
        target_position_count=5,
        use_deterministic_fallback=True,
    )
    assert strat.target_position_count == 5
    # 默认 BQ 配置
    assert strat.bq_project == "data-aquarium"
    assert strat.bq_dataset == "ashare"
    assert strat.bq_location == "asia-east2"
    # 默认严格 rich 模式
    assert strat.require_rich_features is True
    assert strat.max_hold_days == 20
    # 富特征常量挂在类上
    assert strat.RICH_BUY_FEATURE_COLUMNS == RICH_BUY_FEATURE_COLUMNS
    assert strat.RICH_SELL_FEATURE_COLUMNS == RICH_SELL_FEATURE_COLUMNS


def test_strategy_require_rich_features_explicit_false():
    """显式 False 时允许降级。"""
    strat = MLRichPickerStrategy(require_rich_features=False)
    assert strat.require_rich_features is False


def test_score_universe_raises_when_rich_features_missing_and_strict():
    """P2 修复：require_rich_features=True 时 rich 缺失应抛错，不静默退化。"""
    strat = MLRichPickerStrategy(require_rich_features=True)
    # 模拟 _rich_features 未加载
    strat._rich_features = None

    class _FakeCtx:
        current_date = "20240101"

    with pytest.raises(RuntimeError, match="rich features 未加载"):
        strat._score_universe(_FakeCtx())


def test_score_universe_raises_when_current_date_missing_and_strict():
    """严格 rich 模式：某交易日缺 rich 截面不能静默跳过。"""
    strat = _rich_strategy_with_features(use_fallback=True)

    class _FakeCtx:
        current_date = "20240103"

    with pytest.raises(RuntimeError, match="预加载特征表中无记录"):
        strat._score_universe(_FakeCtx())


def test_score_universe_fallback_when_explicit_off(caplog):
    """require_rich_features=False 时允许退化，但会打 WARNING。"""
    strat = MLRichPickerStrategy(require_rich_features=False)
    strat._rich_features = None

    class _FakeCtx:
        current_date = "20240101"

    # 父类 _score_universe 会试图调 context.get_price 拉数据；这里只验证 WARNING 路径
    # 不实际跑成功（吞掉父类抛的 AttributeError 即可）
    try:
        strat._score_universe(_FakeCtx())
    except Exception:
        pass  # 父类需要 portfolio 等更多 context，单测里跑不通

    # 但 WARNING 必须出现，证明走的是"显式降级"路径
    assert any(
        "退化为父类" in r.getMessage() or "退化" in r.getMessage() or "rich features 未加载" in r.getMessage()
        for r in caplog.records
    )


def test_before_trading_start_lazy_loads_only_once():
    """P1.1 修复：rich 预加载在 before_trading_start 懒加载，且只触发一次。"""
    strat = MLRichPickerStrategy(require_rich_features=False)
    calls = []

    def fake_preload(ctx):
        calls.append(ctx.current_date)

    strat._preload_rich_features = fake_preload  # type: ignore

    class _FakeCtx:
        current_date = "20200102"
    ctx = _FakeCtx()

    # 调三次 before_trading_start，应只触发 1 次 preload
    strat.before_trading_start(ctx, {})
    strat.before_trading_start(ctx, {})
    strat.before_trading_start(ctx, {})
    assert len(calls) == 1
    assert strat._rich_preloaded is True


def test_preload_failure_raises_when_strict():
    """P2 修复：严格模式下预加载失败 → before_trading_start 抛 RuntimeError。"""
    strat = MLRichPickerStrategy(require_rich_features=True)

    def fake_preload(ctx):
        raise ValueError("BQ unavailable")
    strat._preload_rich_features = fake_preload  # type: ignore

    class _FakeCtx:
        current_date = "20200102"

    with pytest.raises(RuntimeError, match="预加载 rich features 失败"):
        strat.before_trading_start(_FakeCtx(), {})


def test_preload_failure_swallowed_when_lenient():
    """非严格模式：预加载失败不抛错，_rich_features 保持 None。"""
    strat = MLRichPickerStrategy(require_rich_features=False)

    def fake_preload(ctx):
        raise ValueError("BQ unavailable")
    strat._preload_rich_features = fake_preload  # type: ignore

    class _FakeCtx:
        current_date = "20200102"

    # 不应抛
    strat.before_trading_start(_FakeCtx(), {})
    assert strat._rich_features is None
    assert strat._rich_preloaded is True   # 仍标记完成，避免每日重试


def test_strategy_bq_overrides():
    strat = MLRichPickerStrategy(
        bq_project="my-proj",
        bq_dataset="my-ds",
        bq_location="us-east1",
        adjust_type="hfq",
    )
    assert strat.bq_project == "my-proj"
    assert strat.bq_dataset == "my-ds"
    assert strat.bq_location == "us-east1"
    assert strat.adjust_type == "hfq"


def test_strategy_inherits_trading_permissions_filter():
    """继承父类的 trading_permissions universe 过滤。"""
    universe = ["600000.SH", "688001.SH", "300001.SZ", "000001.SZ", "430001.BJ"]
    strat = MLRichPickerStrategy(
        universe=universe,
        trading_permissions={"allow_star_market": False, "allow_chinext": False, "allow_bse": False},
    )
    # 主板应保留，其他被过滤
    assert set(strat._init_universe) == {"600000.SH", "000001.SZ"}


def test_rich_position_state_uses_trading_day_holding_days():
    """持仓状态特征按交易日计数，并使用 Portfolio 的真实成交日。"""
    strat = MLRichPickerStrategy(require_rich_features=True)
    strat._trading_date_index = strat._build_trading_date_index(
        ["20240105", "20240108", "20240109", "20240110"]
    )
    portfolio = Portfolio(initial_capital=1_000_000)
    pos = Position(code="600000.SH")
    pos.total_qty = 1000
    pos.sellable_qty = 1000
    pos.cost_price = 10.0
    pos._buy_records["20240105"] = 1000
    portfolio.positions["600000.SH"] = pos
    strat._position_state["600000.SH"] = PositionState(
        entered_date="20240104",  # 模拟信号日；真实成交日应来自 _buy_records
        expected_horizon=5,
        peak_price=10.5,
    )

    class _FakeCtx:
        pass
    ctx = _FakeCtx()
    ctx.current_date = "20240108"
    ctx.all_bars = {}
    ctx.portfolio = portfolio

    today_df = pd.DataFrame([{"equity_code": "600000.SH", "close": 10.2}])
    out = strat._position_state_features(ctx, today_df)

    assert out.loc[0, "holding_days"] == 1.0
    assert out.loc[0, "days_to_expected_horizon"] == 4.0


# ──────────────────────────────────────────────────────────────
# walk_forward 模块
# ──────────────────────────────────────────────────────────────


def test_walk_forward_module_importable():
    """walk_forward 模块 import 不应报错。"""
    from strategy.ml_rich_picker import walk_forward
    # 关键函数存在
    assert hasattr(walk_forward, "main")
    assert hasattr(walk_forward, "_load_rich_features_from_bq")
    assert hasattr(walk_forward, "_train_one_retrain_point_rich")
    assert hasattr(walk_forward, "_missing_rich_model_components")


def test_config_yaml_has_universe_top500():
    """P1.2 修复：config.yaml 必须设 universe_source=liquidity_top + top_n=500，
    否则父类默认 static 会回退到 40 只 DEFAULT_UNIVERSE，与训练 universe 错配。"""
    import yaml
    from pathlib import Path
    cfg_path = Path("strategy/ml_rich_picker/config.yaml")
    if not cfg_path.exists():
        pytest.skip("当前工作目录无 config.yaml")
    raw = yaml.safe_load(cfg_path.read_text("utf-8"))
    params = raw.get("params", {})
    assert params.get("universe_source") == "liquidity_top", \
        "config 必须明示 universe_source: liquidity_top"
    assert params.get("liquidity_top_n") == 500, \
        f"config liquidity_top_n 应为 500，实际 {params.get('liquidity_top_n')}"
    assert params.get("liquidity_lookback_days") == 60, \
        f"config liquidity_lookback_days 应为 60，实际 {params.get('liquidity_lookback_days')}"
    assert params.get("max_hold_days") == 20, \
        f"config max_hold_days 应为 20，实际 {params.get('max_hold_days')}"


def test_config_yaml_strict_rich_default():
    """P2 修复：config.yaml 应默认 require_rich_features=true，
    且正式回测不应启用 deterministic fallback。"""
    import yaml
    from pathlib import Path
    cfg_path = Path("strategy/ml_rich_picker/config.yaml")
    if not cfg_path.exists():
        pytest.skip("当前工作目录无 config.yaml")
    raw = yaml.safe_load(cfg_path.read_text("utf-8"))
    params = raw.get("params", {})
    assert params.get("require_rich_features") is True, \
        "config 必须默认 require_rich_features: true"
    assert params.get("use_deterministic_fallback") is False, \
        "正式 rich preset 必须默认 use_deterministic_fallback: false"


def test_walk_forward_sql_contains_3_tables():
    """SQL 必须 JOIN 3 表（daily + fundamental + event）。

    通过检查函数 source code（不实际跑 SQL，避免 BQ 依赖）。
    """
    import inspect
    from strategy.ml_rich_picker.walk_forward import _load_rich_features_from_bq
    src = inspect.getsource(_load_rich_features_from_bq)
    assert "dws_equity_daily_features" in src or "bq_table_daily" in src
    assert "dws_equity_fundamental_features" in src
    assert "dwd_fact_money_flow_1d" in src
    assert "dwd_fact_dragon_tiger_seat_1d" in src
    assert "dwd_fact_kpl_board_1d" in src
    assert "LEFT JOIN" in src
    assert "SAFE_DIVIDE" in src
    assert src.count("partition_month IN UNNEST(@partition_months)") >= 4
    assert 'ArrayQueryParameter("partition_months", "INT64"' in src
    assert "financial_announcement_date < date" in src
    assert "available_signal_date" in src
    assert "kpl_mapped AS" in src
    assert "GROUP BY equity_code, available_signal_date" in src


def test_walk_forward_partition_months_cover_full_range():
    from strategy.ml_rich_picker.walk_forward import _partition_months_in_range

    assert _partition_months_in_range("20231229", "20240201") == [
        202312,
        202401,
        202402,
    ]


def test_walk_forward_requires_decision_horizon_and_sell_model_bundle():
    from strategy.ml_rich_picker.walk_forward import _missing_rich_model_components

    complete = {h: object() for h in BUY_HORIZONS}
    assert _missing_rich_model_components(complete, object()) == []

    partial = {1: object(), 5: object()}
    missing = _missing_rich_model_components(partial, object())
    assert missing == []
    assert _missing_rich_model_components({1: object()}, object()) == ["buy_h5"]
    assert _missing_rich_model_components(
        {1: object(), 10: object()},
        object(),
        required_horizons=[10],
    ) == []
    assert _missing_rich_model_components(complete, None) == [SELL_REMAINING_DAYS_MODEL_NAME]


def test_buy_label_uses_next_open_execution_return():
    from strategy.ml_rich_picker.walk_forward import _compute_execution_horizon_return

    group = pd.DataFrame(
        {
            "date": ["20240101", "20240102", "20240103"],
            "open": [10.0, 20.0, 30.0],
            "close": [100.0, 100.0, 100.0],
        }
    )

    ret = _compute_execution_horizon_return(group, 1)

    assert ret.iloc[0] == pytest.approx(np.log(30.0) - np.log(20.0))


def test_sell_training_frame_contains_position_state_features():
    from strategy.ml_rich_picker.walk_forward import _build_held_position_sell_training_frame

    rows = []
    for i in range(30):
        row = {
            "date": f"202401{i + 1:02d}",
            "equity_code": "000001.SZ",
            "open": 10.0 + i * 0.1,
            "high": 10.2 + i * 0.1,
            "low": 9.8 + i * 0.1,
            "close": 10.1 + i * 0.1,
        }
        for col in RICH_BUY_FEATURE_COLUMNS:
            row[col] = 0.1
        rows.append(row)
    frame = pd.DataFrame(rows)

    out = _build_held_position_sell_training_frame(
        frame,
        lookforward=5,
        drawdown_threshold=-0.05,
        holding_day_samples=[1, 5, 10],
    )

    assert set(POSITION_STATE_FEATURE_COLUMNS).issubset(out.columns)
    assert "label_sell" in out.columns
    assert set(out["holding_days"].dropna().unique()) == {1.0, 5.0, 10.0}
    assert out.loc[out["holding_days"] == 5.0, "days_to_expected_horizon"].dropna().eq(0.0).all()
    assert out.loc[out["holding_days"] == 10.0, "days_to_expected_horizon"].dropna().eq(-5.0).all()


def test_sell_training_frame_default_samples_cover_first_holding_frame():
    from strategy.ml_rich_picker.walk_forward import _build_held_position_sell_training_frame

    rows = []
    for i in range(30):
        row = {
            "date": f"202401{i + 1:02d}",
            "equity_code": "000001.SZ",
            "open": 10.0 + i * 0.1,
            "high": 10.2 + i * 0.1,
            "low": 9.8 + i * 0.1,
            "close": 10.1 + i * 0.1,
        }
        for col in RICH_BUY_FEATURE_COLUMNS:
            row[col] = 0.1
        rows.append(row)

    out = _build_held_position_sell_training_frame(
        pd.DataFrame(rows),
        lookforward=5,
        drawdown_threshold=-0.05,
    )

    assert set(out["holding_days"].dropna().unique()) == {0.0, 3.0, 10.0, 20.0}
    assert out.loc[out["holding_days"] == 0.0, "days_to_expected_horizon"].dropna().eq(5.0).all()


def test_binary_auc_helper():
    from strategy.ml_rich_picker.walk_forward import _binary_auc

    assert _binary_auc(np.array([0, 0, 1, 1]), np.array([0.1, 0.2, 0.8, 0.9])) == pytest.approx(1.0)
    assert _binary_auc(np.array([1, 1]), np.array([0.1, 0.2])) is None


# ──────────────────────────────────────────────────────────────
# 新 sell 回归模型：optimal_remaining_days
# ──────────────────────────────────────────────────────────────


def test_optimal_remaining_days_k_zero_means_immediate_sell():
    """关键契约：k=0 表示"立刻卖出 T+1 开盘"、baseline 收益 0。

    任何 k>=1 的收益都是相对 T+1 开盘价的对数收益，所以下行行情下
    所有 k>=1 都 < 0 < best_return(k=0) → best_k=0；上行行情下
    存在 k>=1 使收益为正，才会偏离 k=0。
    """
    from strategy.ml_rich_picker.walk_forward import (
        _compute_optimal_remaining_days_per_group,
    )

    # 平盘：所有未来 open 等于 T+1 开盘价 → 所有 k 收益都是 0 → best_k 维持初始 0
    flat = pd.DataFrame({
        "date": [f"202401{i + 1:02d}" for i in range(8)],
        "open": [10.0] * 8,
        "low":  [9.95] * 8,
    })
    labels = _compute_optimal_remaining_days_per_group(
        flat, lookforward=5, drawdown_threshold=-0.10,
    )
    # 所有非末尾行都该是 0（平盘无收益，立刻卖最优）
    assert labels.iloc[0] == pytest.approx(0.0)
    assert labels.iloc[2] == pytest.approx(0.0)


def test_optimal_remaining_days_uphill_picks_last_day():
    """单调上涨行情：最佳卖出剩余天数应该接近 lookforward 上限。"""
    from strategy.ml_rich_picker.walk_forward import (
        _compute_optimal_remaining_days_per_group,
    )

    group = pd.DataFrame(
        {
            "date": [f"202401{i + 1:02d}" for i in range(15)],
            "open": [10.0 + i * 0.5 for i in range(15)],   # 单调上涨
            "low": [10.0 + i * 0.5 - 0.1 for i in range(15)],
        }
    )

    labels = _compute_optimal_remaining_days_per_group(
        group, lookforward=5, drawdown_threshold=-0.10,
    )

    # i=0: entry_idx=1, baseline=10.5; k=5: exit=opens[6]=13.0, return=log(13/10.5)
    # 是 k 取值范围里最大者 → best_k=5
    assert labels.iloc[0] == pytest.approx(5.0)
    # i=5: 同样单调涨，best_k=5
    assert labels.iloc[5] == pytest.approx(5.0)


def test_optimal_remaining_days_downhill_picks_zero():
    """单调下跌：立刻卖（k* = 0），所有 k>=1 收益均为负。"""
    from strategy.ml_rich_picker.walk_forward import (
        _compute_optimal_remaining_days_per_group,
    )

    group = pd.DataFrame(
        {
            "date": [f"202401{i + 1:02d}" for i in range(15)],
            "open": [20.0 - i * 0.5 for i in range(15)],   # 单调下跌
            "low": [20.0 - i * 0.5 - 0.1 for i in range(15)],
        }
    )

    labels = _compute_optimal_remaining_days_per_group(
        group, lookforward=5, drawdown_threshold=-0.10,
    )

    # 任何起点都该立刻卖：未来都是亏的，best_return 维持 0、best_k=0
    assert labels.iloc[0] == pytest.approx(0.0)
    assert labels.iloc[5] == pytest.approx(0.0)


def test_optimal_remaining_days_drawdown_truncates_window():
    """中段触发风控阈值：搜索窗被截断，不会贪婪等下一个高点。

    数据（i=0 行的视角，entry_idx=1，baseline=10.0）::

        i:     0     1     2     3     4    5     6
        open:  9.5  10.0  11.0  12.0   8.0  13.0  14.0   ← baseline=opens[1]=10.0
        low:   9.0   9.8  10.5  11.5   7.8  12.8  13.7

        k=1: day=1, low=9.8,  dd=-2%, exit=11, ret=log(11/10)=0.095
        k=2: day=2, low=10.5, dd=-2%, exit=12, ret=log(12/10)=0.182  ← best
        k=3: day=3, low=11.5, dd=-2%, exit=8,  ret=log(8/10)=-0.223  不更新
        k=4: day=4, low=7.8,  dd=-22% → break

    所以 best_k=2（在风控截断前的最高收益）。
    """
    from strategy.ml_rich_picker.walk_forward import (
        _compute_optimal_remaining_days_per_group,
    )

    group = pd.DataFrame(
        {
            "date": ["20240101", "20240102", "20240103", "20240104",
                     "20240105", "20240106", "20240107"],
            "open": [9.5, 10.0, 11.0, 12.0, 8.0, 13.0, 14.0],
            "low":  [9.0, 9.8, 10.5, 11.5, 7.8, 12.8, 13.7],
        }
    )

    labels = _compute_optimal_remaining_days_per_group(
        group, lookforward=5, drawdown_threshold=-0.10,
    )

    assert labels.iloc[0] == pytest.approx(2.0)


def test_optimal_remaining_days_label_does_not_depend_on_holding_state():
    """同一行的 label 不应被人为复制 4 份；每个 (date, code) 只一行样本。"""
    from strategy.ml_rich_picker.walk_forward import (
        _build_optimal_remaining_days_training_frame,
    )

    rows = []
    for i in range(20):
        rows.append({
            "date": f"202401{i + 1:02d}",
            "equity_code": "000001.SZ",
            "open": 10.0 + i * 0.2,
            "close": 10.1 + i * 0.2,
            "high": 10.3 + i * 0.2,
            "low": 9.9 + i * 0.2,
        })

    out = _build_optimal_remaining_days_training_frame(
        pd.DataFrame(rows),
        lookforward=5,
        drawdown_threshold=-0.05,
    )

    # 每个 (date, code) 只产生一行，不再做 holding_day 样本复制
    assert len(out) == len(rows)
    assert "label_optimal_remaining_days" in out.columns
    # label 是浮点天数，不再是 0/1 binary
    valid = out["label_optimal_remaining_days"].dropna()
    assert len(valid) > 0
    assert (valid >= 0).all()
    assert (valid <= 5).all()


def test_deterministic_optimal_remaining_days_high_risk_means_few_days():
    """deterministic fallback：高风险信号 → remaining_days 接近 0。"""
    # 构造一个明显的"该卖"行：深度回撤 + 波动率扩张 + 龙虎榜净卖出
    df = pd.DataFrame([{
        "drawdown_from_high_20d": -0.20,
        "vol_expansion": 3.0,
        "rsi_overbought_streak": 10.0,
        "return_5d": -0.10,
        "debt_to_assets": 0.9,
        "dragon_tiger_net_pct": -0.8,
        "rsi_14": 85.0,
        "return_20d": -0.15,
        "main_net_inflow_pct": -0.5,
        "dist_to_ma60": -0.20,
    }])
    out = deterministic_optimal_remaining_days(df, max_remaining_days=20.0)
    assert out.iloc[0] < 5.0   # 风险大 → 强烈建议尽快卖


def test_deterministic_optimal_remaining_days_low_risk_means_many_days():
    """deterministic fallback：低风险 + 强势 → remaining_days 接近上限。"""
    df = pd.DataFrame([{
        "drawdown_from_high_20d": -0.01,
        "vol_expansion": 0.8,
        "rsi_overbought_streak": 0.0,
        "return_5d": 0.05,
        "debt_to_assets": 0.2,
        "dragon_tiger_net_pct": 0.5,
        "rsi_14": 55.0,
        "return_20d": 0.20,
        "main_net_inflow_pct": 0.6,
        "dist_to_ma60": 0.15,
    }])
    out = deterministic_optimal_remaining_days(df, max_remaining_days=20.0)
    assert out.iloc[0] > 12.0   # 良性环境 → 继续持有


def test_deterministic_optimal_remaining_days_clipped_to_range():
    """输出 clip 到 [0, max_remaining_days]。"""
    df = pd.DataFrame([{}, {}, {}])   # 全空列
    out = deterministic_optimal_remaining_days(df, max_remaining_days=10.0)
    assert (out >= 0).all()
    assert (out <= 10.0).all()


def test_config_sell_max_remaining_days_matches_walk_forward_lookforward():
    """关键契约：策略 sell_max_remaining_days 必须与训练 sell_lookforward 对齐。

    训练 label optimal_remaining_days ∈ [0, sell_lookforward]；推理 clip 上限
    若大于训练范围，模型实际永远预测不到大值，sigmoid 桥接会误判分布。
    """
    import yaml
    from pathlib import Path

    backtest_cfg_path = Path("strategy/ml_rich_picker/config.yaml")
    train_cfg_path = Path("strategy/ml_rich_picker/walk_forward_config.yaml")
    if not (backtest_cfg_path.exists() and train_cfg_path.exists()):
        pytest.skip("找不到配置文件（非项目工作目录）")

    bt_cfg = yaml.safe_load(backtest_cfg_path.read_text("utf-8")) or {}
    tr_cfg = yaml.safe_load(train_cfg_path.read_text("utf-8")) or {}

    inference_max = bt_cfg["params"]["sell_max_remaining_days"]
    train_lookforward = tr_cfg["labels"]["sell_lookforward"]

    assert inference_max == pytest.approx(float(train_lookforward)), (
        f"sell_max_remaining_days={inference_max} 必须 == "
        f"walk_forward sell_lookforward={train_lookforward}"
    )


def test_remaining_days_to_prob_sell_threshold_boundary():
    """剩余天数 = threshold 时桥接的 prob_sell 应为 0.5。"""
    rd = pd.Series([0.0, 1.0, 2.0, 5.0, 20.0])
    prob = remaining_days_to_prob_sell(rd, threshold=1.0, sharpness=1.5)

    # threshold 处恰好 0.5
    assert prob.iloc[1] == pytest.approx(0.5, abs=1e-6)
    # 小于 threshold（应该卖）→ prob 大
    assert prob.iloc[0] > 0.5
    # 大于 threshold（不卖）→ prob 小
    assert prob.iloc[2] < 0.5
    assert prob.iloc[3] < 0.2
    assert prob.iloc[4] < 0.01


def test_remaining_days_to_prob_sell_sharpness_effect():
    """sharpness 越大越接近硬阈值。"""
    rd = pd.Series([0.5, 1.5])   # threshold 两侧各 0.5
    soft = remaining_days_to_prob_sell(rd, threshold=1.0, sharpness=0.5)
    sharp = remaining_days_to_prob_sell(rd, threshold=1.0, sharpness=5.0)
    # sharp 版的 0/1 分离更明显
    assert (sharp.iloc[0] - sharp.iloc[1]) > (soft.iloc[0] - soft.iloc[1])


def test_score_universe_outputs_predicted_remaining_days_column():
    """新 sell 回归模型的输出列必须出现在 score_df 上。"""
    strat = _rich_strategy_with_features(use_fallback=False)
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS}
    # 回归模型常数输出 3.0（"再持有 3 天"）
    strat._models[SELL_REMAINING_DAYS_MODEL_NAME] = _ConstantModel(3.0)

    class _FakeCtx:
        current_date = "20240102"

    out = strat._score_universe(_FakeCtx())
    assert out is not None
    assert "predicted_remaining_days" in out.columns
    assert "prob_sell" in out.columns
    # 预测被 clip 到 [0, sell_max_remaining_days]，恒 3.0 不变
    assert out["predicted_remaining_days"].between(0.0, 20.0).all()
    assert np.allclose(out["predicted_remaining_days"].to_numpy(), 3.0)
    # 桥接 prob_sell：3 > threshold(1) 显著大于 0 → prob_sell 偏低（不卖）
    assert (out["prob_sell"] < 0.5).all()


def test_score_universe_remaining_days_clipped_when_model_overshoots():
    """模型预测超出 [0, max] 时也要被 clip 到合法范围。"""
    strat = _rich_strategy_with_features(use_fallback=False)
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS}
    # 异常大的输出 100.0（远超 max=5）
    strat._models[SELL_REMAINING_DAYS_MODEL_NAME] = _ConstantModel(100.0)

    class _FakeCtx:
        current_date = "20240102"

    out = strat._score_universe(_FakeCtx())
    assert out is not None
    # 默认 sell_max_remaining_days=5（与 walk_forward sell_lookforward 对齐）
    assert np.allclose(
        out["predicted_remaining_days"].to_numpy(),
        strat.sell_max_remaining_days,
    )
    assert strat.sell_max_remaining_days == pytest.approx(5.0)


def test_score_universe_prob_sell_triggers_when_remaining_days_zero():
    """remaining_days=0 → prob_sell 显著 > 0.5 → 父类 trigger 会卖。"""
    strat = _rich_strategy_with_features(use_fallback=False)
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS}
    strat._models[SELL_REMAINING_DAYS_MODEL_NAME] = _ConstantModel(0.0)

    class _FakeCtx:
        current_date = "20240102"

    out = strat._score_universe(_FakeCtx())
    assert out is not None
    assert (out["prob_sell"] > 0.5).all()


def test_score_universe_uses_sell_regression_features_not_position_state():
    """sell 回归模型应该用 SELL_REGRESSION_FEATURE_COLUMNS (35 维)，
    不应被传入 4 维 position state。"""
    strat = _rich_strategy_with_features(use_fallback=False)
    strat._models = {f"buy_h{h}": _ConstantModel() for h in BUY_HORIZONS}

    seen_x_shapes = []

    class _DimSpyModel:
        def predict(self, X):
            seen_x_shapes.append(X.shape)
            return np.full(len(X), 3.0)

    strat._models[SELL_REMAINING_DAYS_MODEL_NAME] = _DimSpyModel()

    class _FakeCtx:
        current_date = "20240102"

    strat._score_universe(_FakeCtx())
    # 应该是 (n_codes, 35)
    assert len(seen_x_shapes) == 1
    n_rows, n_cols = seen_x_shapes[0]
    assert n_cols == 35, f"sell 模型应该收到 35 维特征，实际 {n_cols}"


def test_train_lgbm_with_quality_regression_uses_rank_ic_gate():
    """sell 回归模型走 Spearman rank IC 闸门，buy 走 AUC 闸门。"""
    from strategy.ml_rich_picker.walk_forward import _train_lgbm_with_quality

    # 构造一个易学的回归：y = X[:, 0] * 2 + noise
    rng = np.random.RandomState(7)
    n = 600
    X = rng.randn(n, 5)
    y_reg = X[:, 0] * 2.0 + rng.randn(n) * 0.1
    cfg = SimpleNamespace(
        lightgbm_params={
            "num_boost_round": 30,
            "early_stopping_rounds": 5,
            "verbose": -1,
        },
        min_buy_auc=0.55,
        min_sell_rank_ic=0.10,
    )
    model, metrics = _train_lgbm_with_quality(
        X[:400], y_reg[:400], X[400:], y_reg[400:], cfg,
        SELL_REMAINING_DAYS_MODEL_NAME,
    )
    assert model is not None
    assert metrics["quality_pass"] is True
    assert "valid_rank_ic" in metrics
    assert "valid_mae" in metrics
    assert metrics["objective"] == "regression_l1"


def test_train_lgbm_force_overrides_yaml_binary_objective_for_sell_regression():
    """关键契约：项目 yaml lightgbm.objective='binary'（为 buy 服务）
    必须被 sell 回归模型强制覆盖成 regression_l1，不能 setdefault。

    回归之前的 bug：用 setdefault 时，cfg.lightgbm_params 已经从 yaml 加载到
    objective='binary'，sell 模型会被错按 binary 训练，输出全部 ∈ [0, 1]，
    桥接 sigmoid 后几乎所有持仓都触发卖出。
    """
    from strategy.ml_rich_picker.walk_forward import _train_lgbm_with_quality

    rng = np.random.RandomState(11)
    n = 400
    X = rng.randn(n, 5)
    y = X[:, 0] * 5.0 + rng.randn(n) * 0.5   # 显然超出 [0,1]
    cfg = SimpleNamespace(
        lightgbm_params={
            # 模拟真实 walk_forward_config.yaml：lightgbm.objective=binary
            "objective": "binary",
            "metric": ["binary_logloss", "auc"],
            "num_boost_round": 20,
            "early_stopping_rounds": 5,
            "verbose": -1,
        },
        min_buy_auc=0.55,
        min_sell_rank_ic=0.0,   # 放低门槛只测 objective 行为
    )

    # Sell 回归路径：必须强制覆盖成 regression_l1
    model, metrics = _train_lgbm_with_quality(
        X[:300], y[:300], X[300:], y[300:], cfg,
        SELL_REMAINING_DAYS_MODEL_NAME,
    )
    assert metrics["objective"] == "regression_l1", (
        f"yaml 配置 binary 必须被 sell 回归覆盖，实际 {metrics['objective']}"
    )
    # cfg.lightgbm_params 原始字典不能被改坏（避免污染后续 buy 训练）
    assert cfg.lightgbm_params["objective"] == "binary"

    # Buy 路径：保留 yaml 的 binary
    rng2 = np.random.RandomState(13)
    y_bin = (rng2.rand(n) < 0.4).astype(int)
    cfg.min_buy_auc = 0.0   # 放低门槛只测 objective
    buy_model, _ = _train_lgbm_with_quality(
        X[:300], y_bin[:300], X[300:], y_bin[300:], cfg, "buy_h5",
    )
    # buy 不该被改成 regression
    pred = np.asarray(buy_model.predict(X[300:310]), dtype=float)
    # binary 模型输出 ∈ [0, 1]
    assert (pred >= 0).all() and (pred <= 1).all()


def test_spearman_corr_helper():
    """无 scipy 的 Spearman 相关系数：单调关系 → 1.0。"""
    from strategy.ml_rich_picker.walk_forward import _spearman_corr

    rho = _spearman_corr(
        np.array([1.0, 2.0, 3.0, 4.0, 5.0]),
        np.array([10.0, 20.0, 30.0, 40.0, 50.0]),
    )
    assert rho == pytest.approx(1.0)

    rho_inv = _spearman_corr(
        np.array([1.0, 2.0, 3.0, 4.0, 5.0]),
        np.array([50.0, 40.0, 30.0, 20.0, 10.0]),
    )
    assert rho_inv == pytest.approx(-1.0)

    # 极小样本返回 None
    assert _spearman_corr(np.array([1.0, 2.0]), np.array([1.0, 2.0])) is None


def _make_strat_with_position(
    *,
    cost: float,
    qty: int = 1000,
    entry_date: str,
    current_date: str,
    peak_price: float,
    trading_days: "list[str]",
    profit_take_return_threshold: float = 0.20,
    profit_take_prob_ceiling: float = 0.45,
    stale_loss_min_days: int = 8,
    stale_loss_return_threshold: float = -0.02,
    **strat_kwargs,
):
    """组装一个带 1 只持仓的 rich 策略 + ctx，方便共享给多个 sell trigger 测试。"""
    strat = MLRichPickerStrategy(
        require_rich_features=False,
        profit_take_return_threshold=profit_take_return_threshold,
        profit_take_prob_ceiling=profit_take_prob_ceiling,
        stale_loss_min_days=stale_loss_min_days,
        stale_loss_return_threshold=stale_loss_return_threshold,
        **strat_kwargs,
    )
    portfolio = Portfolio(1_000_000)
    pos = Position(
        code="600000.SH",
        total_qty=qty,
        sellable_qty=qty,
        cost_price=cost,
    )
    pos._buy_records[entry_date] = qty
    portfolio.positions["600000.SH"] = pos
    strat._trading_date_index = strat._build_trading_date_index(trading_days)
    strat._position_state["600000.SH"] = PositionState(
        entered_date=entry_date,
        expected_horizon=5,
        peak_price=peak_price,
    )
    ctx = SimpleNamespace(
        current_date=current_date,
        portfolio=portfolio,
        all_bars={},
    )
    return strat, ctx


def test_profit_take_helper_fires_when_no_trailing_risk():
    """helper-level：当 peak_price 不超过当前价（无 trailing 回撤）时触发 profit_take。

    与 reviewer 反馈对应：之前测试用 peak=14, price=13，在完整调用链里 trailing_stop
    会先抢走。这里改成 peak == 当前价（新高保持中），保证父类 trailing 不命中、
    profit_take 是真正可达的。
    """
    strat, ctx = _make_strat_with_position(
        cost=10.0,
        entry_date="20240108",
        current_date="20240115",
        peak_price=13.0,                # 与当前价持平：无 trailing 回撤
        trading_days=["20240108", "20240109", "20240110", "20240111",
                       "20240112", "20240115"],
    )

    score_df = pd.DataFrame([
        {"code": "600000.SH", "prob_up_h5": 0.30, "prob_sell": 0.10,
         "score": 0.30, "predicted_remaining_days": 4.0},
    ])
    data = {"600000.SH": pd.Series({"close": 13.0})}

    out = strat._check_position_aware_sell_triggers(
        ctx, score_df, data, existing={}, stop_loss_pct=0.05,
    )
    assert "600000.SH" in out
    assert "profit_take" in out["600000.SH"]


def test_profit_take_helper_not_triggered_when_market_still_bullish():
    """浮盈大但 prob_up_h5 仍然看多（>= ceiling）→ 不止盈，让模型继续看着。"""
    strat, ctx = _make_strat_with_position(
        cost=10.0,
        entry_date="20240108",
        current_date="20240115",
        peak_price=14.0,
        trading_days=["20240108", "20240109", "20240110", "20240111",
                       "20240112", "20240115"],
    )

    score_df = pd.DataFrame([
        {"code": "600000.SH", "prob_up_h5": 0.70, "prob_sell": 0.10,
         "score": 0.70, "predicted_remaining_days": 4.0},
    ])
    data = {"600000.SH": pd.Series({"close": 14.0})}

    out = strat._check_position_aware_sell_triggers(
        ctx, score_df, data, existing={}, stop_loss_pct=0.05,
    )
    assert "600000.SH" not in out


def test_stale_loss_helper_fires_in_stop_loss_safe_zone():
    """helper-level：浮亏在 (-2%, stop_loss_pct) 区间（即父类 stop_loss 抓不到
    但比 stale_loss 阈值更负）+ 持仓 ≥ 8 天 → 触发 stale_loss。

    与 reviewer 反馈对应：之前测试用浮亏 -10%（父类 stop_loss 已经 -5% 触发），
    现在改成 -3%，刚好在父类 5% 安全区内，stale_loss 才有意义。
    """
    dates = ["20240101", "20240102", "20240103", "20240104", "20240105",
             "20240108", "20240109", "20240110", "20240111", "20240112",
             "20240115"]
    strat, ctx = _make_strat_with_position(
        cost=10.0,
        entry_date="20240101",
        current_date="20240115",
        peak_price=10.5,
        trading_days=dates,
    )

    # 当前 9.7 → 浮亏 -3%，落在 (-2%, -5%) 区间——stop_loss 抓不到、stale_loss 应该捕
    score_df = pd.DataFrame([
        {"code": "600000.SH", "prob_up_h5": 0.50, "prob_sell": 0.20,
         "score": 0.50, "predicted_remaining_days": 3.0},
    ])
    data = {"600000.SH": pd.Series({"close": 9.7})}

    out = strat._check_position_aware_sell_triggers(
        ctx, score_df, data, existing={}, stop_loss_pct=0.05,
    )
    assert "600000.SH" in out
    assert "stale_loss" in out["600000.SH"]


def test_stale_loss_helper_not_triggered_when_held_short():
    """持仓不足 stale_loss_min_days → 不触发（让父类 stop_loss 处理更深的亏）。"""
    strat, ctx = _make_strat_with_position(
        cost=10.0,
        entry_date="20240110",
        current_date="20240115",
        peak_price=10.0,
        trading_days=["20240110", "20240111", "20240112", "20240115"],
    )

    score_df = pd.DataFrame([
        {"code": "600000.SH", "prob_up_h5": 0.50, "prob_sell": 0.20,
         "score": 0.50, "predicted_remaining_days": 3.0},
    ])
    data = {"600000.SH": pd.Series({"close": 9.7})}

    out = strat._check_position_aware_sell_triggers(
        ctx, score_df, data, existing={}, stop_loss_pct=0.05,
    )
    # 持仓只有 3 个交易日 < 8 → 不触发 stale_loss
    assert "600000.SH" not in out


def test_position_aware_triggers_skip_existing_sells():
    """已经被父类触发器决定卖出的 code，position-aware trigger 不重复处理。"""
    strat, ctx = _make_strat_with_position(
        cost=10.0,
        entry_date="20240108",
        current_date="20240115",
        peak_price=13.0,
        trading_days=["20240108", "20240109", "20240110", "20240111",
                       "20240112", "20240115"],
    )

    score_df = pd.DataFrame([
        {"code": "600000.SH", "prob_up_h5": 0.30, "prob_sell": 0.10,
         "score": 0.30, "predicted_remaining_days": 4.0},
    ])
    data = {"600000.SH": pd.Series({"close": 13.0})}

    # 已经在 existing 里 → 跳过
    out = strat._check_position_aware_sell_triggers(
        ctx, score_df, data,
        existing={"600000.SH": "stop_loss"},
        stop_loss_pct=0.05,
    )
    assert "600000.SH" not in out


# ──────────────────────────────────────────────────────────────
# Full _check_sell_triggers 链路测试：rich trigger 在真实调用链里必须可达
# ──────────────────────────────────────────────────────────────
#
# Reviewer 反馈：helper-level 测试绕过了父类 6 个 trigger 的顺序判定，无法证明
# rich 触发器在真实调用链里是可达的。下面这些测试调 ``_check_sell_triggers``
# 主入口，覆盖 stop_loss / trailing_stop / max_hold / rank_dropout / prob_floor
# / sell_model 都不命中的真实场景，确认 rich 的 g/h 触发器才是命中者。


def _full_chain_call(strat, ctx, *, score_df, data, top_2n_codes=None,
                     stop_loss_pct=None):
    """统一封装一下 full chain 调用：默认让 top_2n 包含本持仓避开 rank_dropout，
    stop_loss_pct 默认走 bull 0.05 (rich config 默认值)。"""
    top = top_2n_codes if top_2n_codes is not None else set(
        score_df["code"].tolist()
    )
    pct = stop_loss_pct if stop_loss_pct is not None else strat.stop_loss_pct_bull
    return strat._check_sell_triggers(ctx, score_df, top, pct, data)


def test_full_chain_profit_take_reaches_rich_trigger():
    """完整调用链：浮盈 30%、peak == current price（无 trailing 回撤）、
    prob_up_h5 < ceiling、prob_sell 低 → 父类 6 个 trigger 全部不命中，
    rich profit_take 才是真正命中者。
    """
    strat, ctx = _make_strat_with_position(
        cost=10.0,
        entry_date="20240108",
        current_date="20240115",
        peak_price=13.0,                # 与当前价持平：无 trailing 回撤
        trading_days=["20240108", "20240109", "20240110", "20240111",
                       "20240112", "20240115"],
        min_hold_days=3,
        max_hold_days=20,
        min_prob_floor=0.30,
        sell_threshold=0.50,
    )

    score_df = pd.DataFrame([
        # prob_h5=0.35 ≥ min_prob_floor(0.30) → 不触发 prob_floor
        # prob_h5=0.35 < profit_take_prob_ceiling(0.45) → 触发 profit_take
        # prob_sell=0.10 ≤ sell_threshold(0.50) → 不触发 sell_model
        {"code": "600000.SH", "prob_up_h5": 0.35, "prob_sell": 0.10,
         "score": 0.35, "predicted_remaining_days": 5.0},
    ])
    data = {"600000.SH": pd.Series({"close": 13.0})}

    sells = _full_chain_call(strat, ctx, score_df=score_df, data=data)

    assert "600000.SH" in sells, "rich profit_take 在父类不命中时应可达"
    assert sells["600000.SH"].startswith("profit_take"), (
        f"应是 rich profit_take 而非父类 trigger，实际：{sells['600000.SH']}"
    )


def test_full_chain_stale_loss_reaches_rich_trigger():
    """完整调用链：浮亏 -3%（父类 5% stop_loss 抓不到）、持仓 10 个交易日、
    peak=cost（无 trailing 回撤候选）、prob_h5 在 floor 之上、prob_sell 低
    → 父类 6 个 trigger 全部不命中，rich stale_loss 才是真正命中者。
    """
    dates = ["20240101", "20240102", "20240103", "20240104", "20240105",
             "20240108", "20240109", "20240110", "20240111", "20240112",
             "20240115"]
    strat, ctx = _make_strat_with_position(
        cost=10.0,
        entry_date="20240101",
        current_date="20240115",
        peak_price=10.0,                # = cost → trailing_stop 不触发（要求 price > cost）
        trading_days=dates,
        min_hold_days=3,
        max_hold_days=20,
        min_prob_floor=0.30,
        sell_threshold=0.50,
    )

    score_df = pd.DataFrame([
        {"code": "600000.SH", "prob_up_h5": 0.50, "prob_sell": 0.20,
         "score": 0.50, "predicted_remaining_days": 3.0},
    ])
    # 当前 9.7 → 浮亏 -3%，父类 stop_loss 5% 抓不到
    data = {"600000.SH": pd.Series({"close": 9.7})}

    sells = _full_chain_call(strat, ctx, score_df=score_df, data=data)

    assert "600000.SH" in sells, "rich stale_loss 在父类不命中时应可达"
    assert sells["600000.SH"].startswith("stale_loss"), (
        f"应是 rich stale_loss 而非父类 trigger，实际：{sells['600000.SH']}"
    )


def test_full_chain_stop_loss_takes_precedence_over_stale_loss():
    """完整调用链：浮亏 -8%（父类 5% stop_loss 命中）+ 持仓 10 天
    → 必须是父类 stop_loss 卖出，不是 rich stale_loss。

    这是 reviewer 反馈的关键场景：旧 stale_loss=-5% 时几乎所有"应该 stop_loss"
    的场景在 helper-level 里也会判定为 stale_loss；full chain 下父类先到。
    现在 stale_loss 阈值改成 -2%（比 stop_loss 浅）后，stop_loss 自然优先，
    stale_loss 只覆盖"温水"区间。
    """
    dates = ["20240101", "20240102", "20240103", "20240104", "20240105",
             "20240108", "20240109", "20240110", "20240111", "20240112",
             "20240115"]
    strat, ctx = _make_strat_with_position(
        cost=10.0,
        entry_date="20240101",
        current_date="20240115",
        peak_price=10.0,
        trading_days=dates,
    )

    score_df = pd.DataFrame([
        {"code": "600000.SH", "prob_up_h5": 0.50, "prob_sell": 0.20,
         "score": 0.50, "predicted_remaining_days": 3.0},
    ])
    # 浮亏 -8%（明显超过父类 bull stop_loss 5%）
    data = {"600000.SH": pd.Series({"close": 9.2})}

    sells = _full_chain_call(strat, ctx, score_df=score_df, data=data)

    assert "600000.SH" in sells
    assert sells["600000.SH"].startswith("stop_loss"), (
        f"父类 stop_loss 应优先于 rich stale_loss，实际：{sells['600000.SH']}"
    )


def test_stale_loss_uses_stop_loss_pct_as_floor_not_static_threshold():
    """关键契约：stale_loss 区间下界**显式**用 stop_loss_pct，而不是依赖
    配置约定。

    场景：把 stop_loss_pct 临时调到 1.5%（比 stale_loss_return_threshold=-2%
    更浅），那 -2% 的浮亏已经穿过 stop_loss 区，stale_loss 必须**不触发**
    （父类 stop_loss 应该早就卖掉了）。

    这是把"abs(stale_loss_threshold) < stop_loss_pct" 这个隐含参数契约
    硬编码进 stale_loss 自身逻辑的代码约束测试。
    """
    dates = ["20240101", "20240102", "20240103", "20240104", "20240105",
             "20240108", "20240109", "20240110", "20240111", "20240112",
             "20240115"]
    strat, ctx = _make_strat_with_position(
        cost=10.0,
        entry_date="20240101",
        current_date="20240115",
        peak_price=10.0,
        trading_days=dates,
        stale_loss_min_days=8,
        stale_loss_return_threshold=-0.02,
    )

    score_df = pd.DataFrame([
        {"code": "600000.SH", "prob_up_h5": 0.50, "prob_sell": 0.20,
         "score": 0.50, "predicted_remaining_days": 3.0},
    ])
    # 浮亏 -2.5% → 在默认 stop_loss=5% 时落在 stale_loss 区间
    data = {"600000.SH": pd.Series({"close": 9.75})}

    # 用默认 bull stop_loss 0.05 → stale_loss 应该触发
    out_bull = strat._check_position_aware_sell_triggers(
        ctx, score_df, data, existing={}, stop_loss_pct=0.05,
    )
    assert "600000.SH" in out_bull
    assert "stale_loss" in out_bull["600000.SH"]

    # 同样浮亏，但 stop_loss 调到 1.5%（比 stale_loss_threshold 浅）→
    # -2.5% 已经穿过 stop_loss 区，stale_loss 必须不触发
    out_tight = strat._check_position_aware_sell_triggers(
        ctx, score_df, data, existing={}, stop_loss_pct=0.015,
    )
    assert "600000.SH" not in out_tight, (
        "stop_loss_pct=1.5% 时 -2.5% 已经穿过 stop_loss 区，"
        "stale_loss 不应抢父类 stop_loss 的活"
    )


def test_stale_loss_floor_inclusive_when_return_equals_stop_loss_boundary():
    """边界：浮亏正好 = -stop_loss_pct 时不触发 stale_loss
    （因为父类 stop_loss 用的是严格 ``<``，理论上不会卖；但 rich 这边为了
    避免和父类边界重叠，仍然用严格 ``>`` 来排除等号——
    宁可少卖一次也不让两个 trigger 在同一个浮亏点都判定为卖出）。
    """
    dates = ["20240101", "20240102", "20240103", "20240104", "20240105",
             "20240108", "20240109", "20240110", "20240111", "20240112",
             "20240115"]
    strat, ctx = _make_strat_with_position(
        cost=10.0,
        entry_date="20240101",
        current_date="20240115",
        peak_price=10.0,
        trading_days=dates,
        stale_loss_min_days=8,
        stale_loss_return_threshold=-0.02,
    )

    score_df = pd.DataFrame([
        {"code": "600000.SH", "prob_up_h5": 0.50, "prob_sell": 0.20,
         "score": 0.50, "predicted_remaining_days": 3.0},
    ])
    # 浮亏正好 -5%（= stop_loss_pct_bull）：边界值
    data = {"600000.SH": pd.Series({"close": 9.5})}

    out = strat._check_position_aware_sell_triggers(
        ctx, score_df, data, existing={}, stop_loss_pct=0.05,
    )
    # 浮亏 == -5%：return == stop_loss_return，严格 ``>`` 排除等号 → 不触发
    assert "600000.SH" not in out


def test_full_chain_trailing_stop_takes_precedence_over_profit_take():
    """完整调用链：浮盈 30% 但 peak=15、当前 13（自高点回撤 -13% > 3%）
    → 父类 trailing_stop 命中，不是 rich profit_take。

    虽然 rich profit_take 条件也满足（浮盈≥20% 且 prob 不再看好），但父类
    trailing_stop 先到。helper-level 测试容易遗漏这种"父类先抢走"的真实顺序。
    """
    strat, ctx = _make_strat_with_position(
        cost=10.0,
        entry_date="20240108",
        current_date="20240115",
        peak_price=15.0,                # 高点 15、当前 13 → 回撤 -13% > 3%
        trading_days=["20240108", "20240109", "20240110", "20240111",
                       "20240112", "20240115"],
    )

    score_df = pd.DataFrame([
        {"code": "600000.SH", "prob_up_h5": 0.35, "prob_sell": 0.10,
         "score": 0.35, "predicted_remaining_days": 4.0},
    ])
    data = {"600000.SH": pd.Series({"close": 13.0})}

    sells = _full_chain_call(strat, ctx, score_df=score_df, data=data)

    assert "600000.SH" in sells
    assert sells["600000.SH"].startswith("trailing_stop"), (
        f"父类 trailing_stop 应优先于 rich profit_take，实际：{sells['600000.SH']}"
    )


def test_rich_bundle_save_load_round_trip(tmp_path):
    """save_rich_bundle / load_rich_bundle 圆环：sell 走新名字 sell_remaining_days_v1。"""
    from strategy.ml_rich_picker.model_storage import (
        save_rich_bundle,
        load_rich_bundle,
        sell_remaining_days_path,
    )

    buy_models = {h: _PicklableTaggedModel(f"buy_h{h}") for h in BUY_HORIZONS}
    sell_model = _PicklableTaggedModel("sell_regression")

    save_rich_bundle(str(tmp_path), buy_models, sell_model)

    # sell_remaining_days_v1.pkl 必须存在
    expected = sell_remaining_days_path(str(tmp_path))
    assert Path(expected).exists()

    loaded = load_rich_bundle(str(tmp_path), horizons=BUY_HORIZONS)
    assert loaded[SELL_REMAINING_DAYS_MODEL_NAME] is not None
    assert loaded[SELL_REMAINING_DAYS_MODEL_NAME].tag == "sell_regression"
    for h in BUY_HORIZONS:
        assert loaded[f"buy_h{h}"] is not None
        assert loaded[f"buy_h{h}"].tag == f"buy_h{h}"
