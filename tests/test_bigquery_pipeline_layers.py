from __future__ import annotations

from bigquery_pipeline.ads import build_event_money_flow_signal_sql
from bigquery_pipeline.dwd import (
    build_adjust_factor_dwd_sql,
    build_dragon_tiger_dwd_sql,
    build_kline_dwd_sql,
    build_index_component_dwd_sql,
    build_kpl_board_dwd_sql,
    build_money_flow_dwd_sql,
)
from bigquery_pipeline.dws import build_event_money_flow_features_sql


CONFIG = {
    "project_id": "data-aquarium",
    "dataset": "ashare",
    "table_prefixes": {"dwd": "dwd_", "dws": "dws_", "ads": "ads_"},
    "defaults": {"ads": {"event_money_flow_top_n": 100}},
}


def test_new_raw_dwd_sql_standardizes_equity_event_tables():
    kpl_sql = build_kpl_board_dwd_sql(
        "data-aquarium.ashare.ods_fact_kpl_board_1d",
        "data-aquarium.ashare.dwd_fact_kpl_board_1d",
        ["日期", "代码", "名称", "初次涨停时间", "最后涨停时间", "炸板时间", "跌停时间", "涨停原因", "标签", "板块", "主力净额(元)", "N连板"],
    )
    assert "equity_code" in kpl_sql
    assert "limit_up_reason" in kpl_sql
    assert "limit_up_streak" in kpl_sql

    dragon_sql = build_dragon_tiger_dwd_sql(
        "data-aquarium.ashare.ods_fact_dragon_tiger_seat_1d",
        "data-aquarium.ashare.dwd_fact_dragon_tiger_seat_1d",
        ["日期", "代码", "股票名称", "买卖类型", "营业部名称", "买入额(元)", "卖出额(元)", "净成交额(元)", "上榜理由"],
    )
    assert "department_name" in dragon_sql
    assert "net_amount" in dragon_sql

    money_sql = build_money_flow_dwd_sql(
        "data-aquarium.ashare.ods_fact_money_flow_1d",
        "data-aquarium.ashare.dwd_fact_money_flow_1d",
        ["日期", "代码", "名称", "净流入量(手)", "净流入额(万元)", "主力净流入额(万元)"],
    )
    assert "net_inflow_amount" in money_sql
    assert "main_net_inflow_amount" in money_sql


def test_new_raw_dwd_sql_keeps_index_component_two_codes():
    sql = build_index_component_dwd_sql(
        "data-aquarium.ashare.ods_fact_index_component_1d",
        "data-aquarium.ashare.dwd_fact_index_component_1d",
        ["指数代码", "成分股票代码", "交易日期", "权重"],
    )

    assert "index_code" in sql
    assert "equity_code" in sql
    assert "weight" in sql


def test_event_money_flow_dws_and_ads_sql_are_registered():
    dws_sql = build_event_money_flow_features_sql(CONFIG)
    ads_sql = build_event_money_flow_signal_sql(CONFIG)

    assert "dws_equity_event_money_flow_features_1d" in dws_sql
    assert "dwd_fact_money_flow_1d" in dws_sql
    assert "dwd_fact_dragon_tiger_seat_1d" in dws_sql
    assert "dwd_fact_kpl_board_1d" in dws_sql
    assert "ads_signal_event_money_flow_1d" in ads_sql
    assert "score_proxy" in ads_sql
    assert "is_selected" in ads_sql


def test_equity_kline_dwd_infers_adjust_type_from_source_file():
    sql = build_kline_dwd_sql(
        "fact_equity_kline_1d",
        "data-aquarium.ashare.ods_fact_equity_kline_1d",
        "data-aquarium.ashare.dwd_fact_equity_kline_1d",
        [
            "__",
            "____",
            "___",
            "_____",
            "______",
            "_______",
            "________",
            "_________",
            "date",
            "security_code",
            "source_file",
            "source_entry",
            "partition_month",
        ],
    )

    assert "AS adjust_type" in sql
    assert "daily_qfq" in sql
    assert "daily_hfq" in sql
    assert "ELSE 'none'" in sql
    assert "PARTITION BY equity_code, date, adjust_type" in sql


def test_adjust_factor_dwd_keeps_qfq_and_hfq_and_reads_placeholder_factor():
    sql = build_adjust_factor_dwd_sql(
        "data-aquarium.ashare.ods_fact_adjust_factor",
        "data-aquarium.ashare.dwd_fact_adjust_factor",
        [
            "____",
            "_____",
            "______",
            "date",
            "security_code",
            "source_file",
            "source_entry",
            "partition_month",
        ],
    )

    assert "AS adjust_type" in sql
    assert "SAFE_CAST(NULLIF(TRIM(CAST(t.`______` AS STRING)), '') AS NUMERIC) AS adjust_factor" in sql
    assert "CLUSTER BY equity_code, adjust_type" in sql
    assert "PARTITION BY equity_code, date, adjust_type" in sql
