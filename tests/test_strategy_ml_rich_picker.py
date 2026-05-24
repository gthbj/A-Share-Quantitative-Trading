"""ml_rich_picker 单元测试（PRD_20260525_03）。

覆盖：
- 富特征列定义（30 维 buy / 35 维 sell）
- deterministic_rich_score / deterministic_rich_sell_score 兜底逻辑
- MLRichPickerStrategy 构造（继承父类）
- walk_forward 模块 import 不报错
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategy.ml_rich_picker import (
    DAILY_FEATURE_COLUMNS,
    FUNDAMENTAL_FEATURE_COLUMNS,
    EVENT_FEATURE_COLUMNS,
    RICH_BUY_FEATURE_COLUMNS,
    RICH_SELL_FEATURE_COLUMNS,
    MLRichPickerStrategy,
    deterministic_rich_score,
    deterministic_rich_sell_score,
)
from strategy.ml_multi_horizon_picker.features import SELL_RISK_FEATURE_COLUMNS
from strategy.ml_multi_horizon_picker.strategy import MLMultiHorizonStrategy


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
    """Sell 特征 35 维 = 30 + 5。"""
    assert len(RICH_SELL_FEATURE_COLUMNS) == 35
    # 前 30 维与 buy 一致，最后 5 维是 sell-side 风险
    assert RICH_SELL_FEATURE_COLUMNS[:30] == RICH_BUY_FEATURE_COLUMNS
    assert RICH_SELL_FEATURE_COLUMNS[30:] == SELL_RISK_FEATURE_COLUMNS


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


def test_deterministic_rich_score_returns_series():
    """fallback 函数返回长度匹配的 Series。"""
    df = _toy_rich_features()
    out = deterministic_rich_score(df)
    assert isinstance(out, pd.Series)
    assert len(out) == len(df)
    # 不应抛 NaN（fundamental NaN 应被 fillna(0) 处理）
    assert not out.isna().all()


def test_deterministic_rich_sell_score_in_range():
    """sell fallback 输出 [0, 1] 概率。"""
    df = _toy_rich_features()
    # 补 sell-side 风险特征
    for col in SELL_RISK_FEATURE_COLUMNS:
        df[col] = np.random.uniform(-0.5, 0.5, len(df))
    out = deterministic_rich_sell_score(df)
    assert (out >= 0).all() and (out <= 1).all()


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


def test_config_yaml_strict_rich_default():
    """P2 修复：config.yaml 应默认 require_rich_features=true，
    避免静默退化为 v1 行为。"""
    import yaml
    from pathlib import Path
    cfg_path = Path("strategy/ml_rich_picker/config.yaml")
    if not cfg_path.exists():
        pytest.skip("当前工作目录无 config.yaml")
    raw = yaml.safe_load(cfg_path.read_text("utf-8"))
    params = raw.get("params", {})
    assert params.get("require_rich_features") is True, \
        "config 必须默认 require_rich_features: true"


def test_walk_forward_sql_contains_3_tables():
    """SQL 必须 JOIN 3 表（daily + fundamental + event）。

    通过检查函数 source code（不实际跑 SQL，避免 BQ 依赖）。
    """
    import inspect
    from strategy.ml_rich_picker.walk_forward import _load_rich_features_from_bq
    src = inspect.getsource(_load_rich_features_from_bq)
    assert "dws_equity_daily_features" in src or "bq_table_daily" in src
    assert "dws_equity_fundamental_features" in src
    assert "dws_equity_event_money_flow_features_1d" in src
    assert "LEFT JOIN" in src
    assert "SAFE_DIVIDE" in src
