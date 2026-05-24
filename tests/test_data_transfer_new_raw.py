from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path.cwd() / "data_transfer"))

from prepare_parquet_to_gcs import date_from_source_path, raw_local_path_for_blob, raw_sync_include_specs, raw_sync_include_suffixes
from prepare_standardized_to_gcs import classify


def test_new_raw_directories_are_classified_to_fact_tables():
    root = Path("/mnt/localssd/raw_incremental/source_snapshot=20260523")

    assert classify(root / "开盘啦榜单" / "20260522.csv") == "fact_kpl_board_1d"
    assert classify(root / "龙虎榜席位" / "20160614.csv") == "fact_dragon_tiger_seat_1d"
    assert classify(root / "资金流向" / "20130411.csv") == "fact_money_flow_1d"


def test_index_raw_directories_are_classified_without_duplicating_existing_index_tables():
    root = Path("/mnt/localssd/raw_incremental/source_snapshot=20260523")

    assert classify(root / "指数数据" / "指数日线行情.zip") == "unmapped"
    assert classify(root / "指数数据" / "增量数据" / "指数日线行情" / "2026-05" / "20260522_指数日线行情.csv") == "unmapped"
    assert classify(root / "指数数据" / "指数基本信息_中证指数.csv") == "dim_index_profile"
    assert classify(root / "指数数据" / "中信行业日线行情.zip") == "fact_citic_industry_kline_1d"
    assert classify(root / "指数数据" / "申万行业日线行情.zip") == "fact_sw_industry_kline_1d"
    assert classify(root / "指数数据" / "大盘指数每日指标" / "上证综指.csv") == "fact_index_market_indicator_1d"
    assert classify(root / "指数数据" / "上交所指数成分" / "上交所指数成分_20260430.zip") == "fact_index_component_1d"
    assert classify(root / "指数数据" / "申万行业成分_每日更新" / "2026-05" / "申万行业成分_20260522.csv") == "fact_sw_industry_component_1d"
    assert classify(root / "指数数据" / "申万行业分类" / "申万行业分类_L1_SW2021.csv") == "dim_sw_industry"
    assert classify(root / "指数数据" / "中信行业分类" / "中信行业分类_行业层级图.csv") == "dim_citic_industry"
    assert classify(root / "指数数据" / "中信行业分类" / "中信行业分类_成分股_全部_CITIC.csv") == "fact_citic_industry_component_history"


def test_raw_gcs_blob_maps_under_configured_source_root():
    config = {
        "source_root": "/mnt/localssd/raw_incremental/source_snapshot=20260523",
        "raw_gcs": {
            "source_prefix": "a-share/raw/source_snapshot=20260523",
        },
    }

    path = raw_local_path_for_blob(
        config,
        "a-share/raw/source_snapshot=20260523/开盘啦榜单/20260522.csv",
    )

    assert path == Path("/mnt/localssd/raw_incremental/source_snapshot=20260523/开盘啦榜单/20260522.csv")


def test_raw_gcs_sync_defaults_to_csv_only():
    config = {"raw_gcs": {}}

    assert raw_sync_include_suffixes(config) == (".csv",)


def test_raw_gcs_sync_supports_per_prefix_suffixes():
    config = {
        "raw_gcs": {
            "include_items": [
                {"prefix": "开盘啦榜单", "suffixes": ["csv"]},
                {"prefix": "指数数据/中信行业日线行情.zip", "suffixes": [".zip"]},
            ],
        },
    }

    assert raw_sync_include_specs(config) == [
        ("开盘啦榜单", (".csv",)),
        ("指数数据/中信行业日线行情.zip", (".zip",)),
    ]


def test_source_file_date_can_drive_daily_snapshot_tables():
    source = Path("/mnt/localssd/raw_incremental/source_snapshot=20260523/指数数据/申万行业成分_每日更新/2026-05/申万行业成分_20260522.csv")

    assert date_from_source_path(source) == "2026-05-22"
