from __future__ import annotations

import pandas as pd
import pytest

from bigquery_pipeline import cli
from bigquery_pipeline.fundamental import (
    build_equity_fundamental_features_sql,
    normalize_balance_frame,
    normalize_income_frame,
)


def test_normalize_income_frame_maps_core_fields():
    source = pd.DataFrame(
        {
            "股票代码": ["000001"],
            "公告日期": ["20240328"],
            "报告期": [20231231],
            "营业总收入": ["1000.5"],
            "净利润(不含少数股东损益)": ["100.25"],
            "研发费用": ["8.5"],
        }
    )

    result = normalize_income_frame(source, "gs://bucket/fact_income_statement/part.parquet")

    assert result.loc[0, "equity_code"] == "000001.SZ"
    assert result.loc[0, "announcement_date"].isoformat() == "2024-03-28"
    assert result.loc[0, "report_period"] == "20231231"
    assert result.loc[0, "total_revenue"] == 1000.5
    assert result.loc[0, "net_profit_parent"] == 100.25
    assert result.loc[0, "rd_expense"] == 8.5


def test_normalize_balance_frame_maps_core_fields():
    source = pd.DataFrame(
        {
            "股票代码": ["600519"],
            "实际公告日期": ["20240403"],
            "报告期": [20231231],
            "资产总计": ["5000"],
            "负债合计": ["3000"],
            "股东权益合计(不含少数股东权益)": ["1800"],
            "期末总股本": ["1256197800"],
        }
    )

    result = normalize_balance_frame(source, "gs://bucket/fact_balance_sheet/part.parquet")

    assert result.loc[0, "equity_code"] == "600519.SH"
    assert result.loc[0, "announcement_date"].isoformat() == "2024-04-03"
    assert result.loc[0, "report_period"] == "20231231"
    assert result.loc[0, "total_assets"] == 5000
    assert result.loc[0, "total_liabilities"] == 3000
    assert result.loc[0, "total_equity_parent"] == 1800
    assert result.loc[0, "total_share"] == 1256197800


def test_fundamental_sql_uses_announcement_date_intervals():
    config = {"project_id": "data-aquarium", "dataset": "ashare", "table_prefixes": {"dwd": "dwd_", "dws": "dws_"}}

    sql = build_equity_fundamental_features_sql(config)

    assert "dws_equity_fundamental_features" in sql
    assert "p.date >= f.announcement_date" in sql
    assert "p.date >= i.announcement_date" in sql
    assert "p.date >= b.announcement_date" in sql
    assert "dwd_fact_income_statement_core" in sql
    assert "dwd_fact_balance_sheet_core" in sql


def test_fundamental_sql_uses_unadjusted_price_for_valuation():
    config = {"project_id": "data-aquarium", "dataset": "ashare", "table_prefixes": {"dwd": "dwd_", "dws": "dws_"}}

    sql = build_equity_fundamental_features_sql(config)

    assert "AND adjust_type = 'none'" in sql
    assert "AND adjust_type = 'qfq'" not in sql


def test_bigquery_pipeline_cli_exposes_fundamental_commands(capsys, monkeypatch):
    monkeypatch.setattr("sys.argv", ["cli.py", "--help"])

    with pytest.raises(SystemExit):
        cli.main()

    captured = capsys.readouterr()
    assert "repair-fundamental-inputs" in captured.out
    assert "transform-equity-fundamental-features" in captured.out
    assert "audit-equity-fundamental-features" in captured.out
