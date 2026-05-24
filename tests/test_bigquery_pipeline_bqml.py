from __future__ import annotations

import pytest

from bigquery_pipeline import cli
from bigquery_pipeline.bqml import (
    build_predict_ml_stock_picker_bqml_sql,
    build_train_ml_stock_picker_bqml_sql,
)


CONFIG = {
    "project_id": "data-aquarium",
    "dataset": "ashare",
    "location": "asia-east2",
    "table_prefixes": {"dwd": "dwd_", "dws": "dws_", "ads": "ads_"},
    "bqml": {
        "ml_stock_picker": {
            "model_name": "bqml_ml_stock_picker_baseline",
            "prediction_table": "ads_signal_ml_stock_picker_bqml_1d",
            "train_start_date": "20180101",
            "train_end_date": "20241231",
            "eval_start_date": "20250101",
            "eval_end_date": "20251231",
            "prediction_start_date": "20250101",
            "prediction_end_date": "20260522",
            "label_horizon": 5,
            "top_pct": 0.30,
            "bottom_pct": 0.30,
            "top_n": 50,
            "max_iterations": 30,
            "learn_rate": 0.05,
            "max_tree_depth": 6,
            "subsample": 0.8,
        }
    },
}


def test_bqml_train_sql_uses_boosted_tree_and_no_identifier_features():
    sql = build_train_ml_stock_picker_bqml_sql(CONFIG)

    assert "CREATE OR REPLACE MODEL `data-aquarium.ashare.bqml_ml_stock_picker_baseline`" in sql
    assert "MODEL_TYPE = 'BOOSTED_TREE_CLASSIFIER'" in sql
    assert "INPUT_LABEL_COLS = ['label_class']" in sql
    assert "DATA_SPLIT_METHOD = 'CUSTOM'" in sql
    assert "LOG(LEAD(close, 5)" in sql
    assert "dws_equity_daily_features" in sql
    assert "dws_equity_fundamental_features" in sql
    assert "dws_equity_event_money_flow_features_1d" in sql
    assert "SELECT\n  label_class,\n  is_eval," in sql
    assert "equity_code,\n  date," not in sql.split("SELECT\n  label_class,\n  is_eval,")[-1]


def test_bqml_predict_sql_writes_ads_signal_table_and_extracts_up_probability():
    sql = build_predict_ml_stock_picker_bqml_sql(CONFIG)

    assert "CREATE OR REPLACE TABLE `data-aquarium.ashare.ads_signal_ml_stock_picker_bqml_1d`" in sql
    assert "ML.PREDICT" in sql
    assert "predicted_label_class_probs" in sql
    assert "CAST(label AS STRING) = '1'" in sql
    assert "score_rank <= 50 AS is_selected" in sql
    assert "PARTITION BY RANGE_BUCKET(partition_month" in sql


def test_bigquery_pipeline_cli_exposes_bqml_commands(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli.py", "--help"])

    with pytest.raises(SystemExit):
        cli.main()

    captured = capsys.readouterr()
    assert "train-bqml-ml-stock-picker" in captured.out
    assert "predict-bqml-ml-stock-picker" in captured.out
    assert "audit-bqml-ml-stock-picker" in captured.out
