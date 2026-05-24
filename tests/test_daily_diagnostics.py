from types import SimpleNamespace

from analytics.daily_diagnostics import (
    DAILY_SUMMARY_HEADERS,
    write_daily_diagnostics,
    write_two_header_csv,
)
from scripts.sync_cloud_run_daily_logs import _parse_kv


def test_write_two_header_csv(tmp_path):
    out = tmp_path / "daily_log.csv"
    write_two_header_csv(out, DAILY_SUMMARY_HEADERS[:3], [
        {"date": "20200102", "day_index": 1, "total_days": 2}
    ])

    lines = out.read_text("utf-8-sig").splitlines()
    assert lines[0] == "日期,交易日序号,总交易日数"
    assert lines[1] == "date,day_index,total_days"
    assert lines[2] == "20200102,1,2"


def test_write_daily_diagnostics_outputs_three_files(tmp_path):
    records = [
        SimpleNamespace(
            summary={"date": "20200102", "day_index": 1},
            position_details=[{"date": "20200102", "code": "600000.SH", "qty": 100}],
            candidate_details=[{"date": "20200102", "rank": 1, "code": "600000.SH"}],
        )
    ]

    paths = write_daily_diagnostics(tmp_path, records)

    assert paths["daily_log"].exists()
    assert paths["daily_positions"].exists()
    assert paths["daily_candidates"].exists()
    assert "daily_log.csv" in str(paths["daily_log"])


def test_parse_day_summary_kv_payload():
    row = _parse_kv(
        "2026-05-24 INFO DAY_SUMMARY date=20200102 nav=100000.5 "
        "is_sellable=true model_dir=gs://bucket/path/20191231",
        "DAY_SUMMARY",
    )

    assert row == {
        "date": 20200102,
        "nav": 100000.5,
        "is_sellable": True,
        "model_dir": "gs://bucket/path/20191231",
    }
