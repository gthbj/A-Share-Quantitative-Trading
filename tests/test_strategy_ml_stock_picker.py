from __future__ import annotations

import pandas as pd

from account.portfolio import Portfolio
from data_layer.bigquery_source import BigQueryDataSource
from data_layer.local_storage import LocalStorage
from strategy.base_strategy import Context
from strategy.ml_stock_picker.features import FeatureEngineer
from strategy.ml_stock_picker.strategy import MLStockPickerStrategy


def test_enhanced_feature_columns_include_current_dws_data():
    cols = FeatureEngineer.feature_columns("enhanced")

    assert "return_20d" in cols
    assert "pe_basic" in cols
    assert "pb" in cols
    assert "roe" in cols
    assert "net_inflow_to_amount" in cols
    assert "dragon_tiger_net_to_amount" in cols


def test_deterministic_score_is_reproducible_and_cross_sectional():
    frame = pd.DataFrame(
        {
            "code": ["000001.SZ", "000002.SZ"],
            "return_20d": [0.10, -0.02],
            "return_5d": [0.03, -0.01],
            "macd_hist": [0.2, -0.1],
            "volume_ma20_ratio": [1.4, 0.8],
            "std_ratio": [0.7, 1.8],
            "pb": [1.2, 6.0],
            "roe": [0.15, 0.03],
            "gross_margin": [0.45, 0.12],
            "net_margin": [0.18, 0.02],
            "net_inflow_to_amount": [0.05, -0.03],
            "main_net_inflow_to_amount": [0.04, -0.02],
            "dragon_tiger_net_to_amount": [0.02, -0.01],
            "limit_up_streak": [2, 0],
            "is_kpl_event": [1, 0],
            "debt_to_assets": [0.35, 0.8],
        }
    )

    first = FeatureEngineer.deterministic_score(frame)
    second = FeatureEngineer.deterministic_score(frame)

    assert first.equals(second)
    assert first.iloc[0] > first.iloc[1]


def test_bigquery_feature_snapshot_uses_dws_joins(monkeypatch):
    source = BigQueryDataSource(
        project_id="data-aquarium",
        dataset="ashare",
        use_cache=False,
    )
    captured = {}

    def fake_execute(sql: str, max_retries: int = 3):
        captured["sql"] = sql
        return pd.DataFrame()

    monkeypatch.setattr(source, "_execute_sql", fake_execute)

    source.get_equity_feature_snapshot(
        ["000001.SZ"],
        "20240131",
        feature_set="enhanced",
    )

    sql = captured["sql"]
    assert "dws_equity_daily_features" in sql
    assert "dws_equity_fundamental_features" in sql
    assert "dws_equity_event_money_flow_features_1d" in sql
    assert "net_inflow_to_amount" in sql
    assert "b.date = DATE '2024-01-31'" in sql
    assert "b.equity_code IN ('000001.SZ')" in sql


def test_strategy_uses_deterministic_fallback_when_model_missing():
    strategy = MLStockPickerStrategy(
        model_path="",
        universe=["000001.SZ", "000002.SZ"],
        top_k=1,
        position_pct=0.9,
        feature_source="local",
        feature_set="enhanced",
    )
    context = Context(
        portfolio=Portfolio(initial_capital=100_000),
        data_source=object(),  # type: ignore[arg-type]
        current_date="20240131",
    )
    strategy.initialize(context)

    feature_frame = pd.DataFrame(
        {
            "code": ["000001.SZ", "000002.SZ"],
            "return_1d": [0.01, -0.01],
            "return_5d": [0.03, -0.02],
            "return_10d": [0.08, -0.03],
            "return_20d": [0.12, -0.06],
            "volume_ma5_ratio": [1.2, 0.9],
            "volume_ma20_ratio": [1.3, 0.8],
            "amount_ma5_ratio": [1.2, 0.9],
            "std_5d": [0.02, 0.04],
            "std_20d": [0.03, 0.05],
            "std_ratio": [0.6, 1.8],
            "rsi_14": [60, 35],
            "macd_diff": [0.2, -0.2],
            "macd_signal": [0.1, -0.1],
            "macd_hist": [0.1, -0.1],
            "close_to_high_20d": [0.8, 0.2],
            "close_to_ma5": [0.02, -0.03],
            "close_to_ma20": [0.05, -0.08],
            "roe": [0.15, 0.03],
            "gross_margin": [0.4, 0.1],
            "net_margin": [0.18, 0.02],
            "pb": [1.5, 5.0],
            "debt_to_assets": [0.4, 0.8],
            "net_inflow_to_amount": [0.04, -0.02],
            "main_net_inflow_to_amount": [0.03, -0.01],
            "dragon_tiger_net_to_amount": [0.02, -0.01],
            "limit_up_streak": [1, 0],
            "is_kpl_event": [1, 0],
        }
    )
    prepared = strategy._feature_engineer.prepare_model_frame(
        feature_frame, feature_set="enhanced", require_technical=True
    )
    strategy._load_feature_snapshot = lambda _: prepared  # type: ignore[method-assign]

    strategy.handle_data(
        context,
        {
            "000001.SZ": pd.Series({"close": 10.0}),
            "000002.SZ": pd.Series({"close": 10.0}),
        },
    )

    orders = context.pop_orders()
    assert len(orders) == 1
    assert orders[0].code == "000001.SZ"


def test_local_storage_normalizes_cached_bar_numeric_columns(tmp_path):
    storage = LocalStorage(root_dir=str(tmp_path))
    storage.save_bars(
        "000001",
        pd.DataFrame(
            {
                "date": ["20240131"],
                "open": ["10.1"],
                "high": ["10.5"],
                "low": ["9.9"],
                "close": ["10.2"],
                "volume": ["1000"],
                "amount": ["10200"],
            }
        ),
        fmt="csv",
    )

    loaded = storage.load_bars("000001", "20240101", "20241231", fmt="csv")

    assert loaded.loc[0, "date"] == "20240131"
    assert isinstance(float(loaded.loc[0, "close"]), float)
    assert loaded["close"].dtype.kind in {"f", "i"}
