from __future__ import annotations

import pandas as pd
import pytest

from bigquery_pipeline import cli
from bigquery_pipeline.financial import build_equity_valuation_features_sql, normalize_financial_frame
from bigquery_pipeline.sql import coerce_date_value, normalize_security_code


def test_normalize_security_code_for_equity_codes():
    assert normalize_security_code("000001") == "000001.SZ"
    assert normalize_security_code("600519") == "600519.SH"
    assert normalize_security_code("430139") == "430139.BJ"
    assert normalize_security_code("SH600000") == "600000.SH"


def test_coerce_date_value_accepts_compact_date():
    assert coerce_date_value("20240328").isoformat() == "2024-03-28"
    assert coerce_date_value(20231231).isoformat() == "2023-12-31"
    assert coerce_date_value("") is None


def test_normalize_financial_frame_maps_core_fields():
    source = pd.DataFrame(
        {
            "股票代码": ["000001"],
            "公告日期": ["20240328"],
            "报告期": [20231231],
            "基本每股收益": ["1.23"],
            "每股净资产": ["10.5"],
            "净资产收益率": ["12.8"],
            "source_entry": ["part-000"],
        }
    )

    result = normalize_financial_frame(source, "gs://bucket/fact_financial_indicator/part.parquet")

    assert result.loc[0, "equity_code"] == "000001.SZ"
    assert result.loc[0, "announcement_date"].isoformat() == "2024-03-28"
    assert result.loc[0, "report_period"] == "20231231"
    assert result.loc[0, "partition_month"] == 202403
    assert result.loc[0, "eps_basic"] == 1.23
    assert result.loc[0, "bps"] == 10.5
    assert result.loc[0, "roe"] == 12.8
    assert result.loc[0, "source_file"] == "gs://bucket/fact_financial_indicator/part.parquet"
    assert result.loc[0, "source_hash"]


def test_valuation_sql_uses_unadjusted_price_for_valuation():
    config = {"project_id": "data-aquarium", "dataset": "ashare", "table_prefixes": {"dwd": "dwd_", "dws": "dws_"}}

    sql = build_equity_valuation_features_sql(config)

    assert "AND adjust_type = 'none'" in sql
    assert "AND adjust_type = 'qfq'" not in sql


def test_bigquery_pipeline_cli_exposes_internal_commands(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli.py", "--help"])

    with pytest.raises(SystemExit):
        cli.main()

    captured = capsys.readouterr()
    assert "repair-financial-indicator" in captured.out
    assert "transform-dwd" in captured.out
    assert "transform-dws" in captured.out
    assert "transform-ads" in captured.out
