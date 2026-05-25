from __future__ import annotations

import json
import sys
import types
from pathlib import Path

import pandas as pd
import pytest

from data_ingestion.tushare_client import create_tushare_pro
from data_ingestion.tushare_to_gcs import (
    EndpointConfig,
    IngestJob,
    TushareGCSIngestor,
    add_ingestion_metadata,
    dataframe_to_parquet_bytes,
    iter_quarter_periods,
    iter_year_ranges,
    is_retryable_tushare_error,
    load_config,
    normalize_yyyymmdd,
    partition_month_for,
    priority_order,
    select_endpoints,
    snapshot_date_for_run_id,
    standardize_dataframe,
)


class FakeTusharePro:
    def __init__(self, rows: int) -> None:
        self.rows = rows
        self.calls: list[dict] = []

    def daily(self, **params):
        self.calls.append(params)
        limit = int(params.get("limit", self.rows))
        offset = int(params.get("offset", 0))
        end = min(offset + limit, self.rows)
        if offset >= self.rows:
            return pd.DataFrame()
        return pd.DataFrame({"row_id": list(range(offset, end))})


class FakeIgnoredPaginationPro:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def daily(self, **params):
        self.calls.append(params)
        return pd.DataFrame({"row_id": [1, 2, 3]})


class FakeNoneTusharePro:
    def daily(self, **params):
        return None


class FakeRunTusharePro:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def daily(self, **params):
        self.calls.append(params)
        if params.get("trade_date") == "20260524":
            raise RuntimeError("temporary timeout")
        return pd.DataFrame(
            [
                {
                    "ts_code": "000001.SZ",
                    "trade_date": params.get("trade_date"),
                    "close": 10.0,
                }
            ]
        )


class FakeBlob:
    def __init__(self, store: dict[str, bytes], name: str) -> None:
        self.store = store
        self.name = name

    def exists(self) -> bool:
        return self.name in self.store

    def download_as_bytes(self) -> bytes:
        return self.store[self.name]

    def upload_from_string(self, payload, content_type=None) -> None:
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.store[self.name] = payload


class FakeBucket:
    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(self.store, name)


class FakeTushareModule(types.SimpleNamespace):
    def __init__(self) -> None:
        super().__init__()
        self.token = None
        self.pro = types.SimpleNamespace()

    def set_token(self, token):
        self.token = token

    def pro_api(self, token):
        self.token = token
        return self.pro


def test_normalize_yyyymmdd_accepts_dashless_and_dashed_dates():
    assert normalize_yyyymmdd("2026-05-25") == "20260525"
    assert normalize_yyyymmdd(20260525) == "20260525"


def test_iter_quarter_periods_limits_to_range():
    assert list(iter_quarter_periods("20190101", "20191231")) == [
        "20190331",
        "20190630",
        "20190930",
        "20191231",
    ]
    assert list(iter_quarter_periods("20190401", "20190930")) == [
        "20190630",
        "20190930",
    ]


def test_iter_year_ranges_limits_to_requested_dates():
    assert list(iter_year_ranges("20191115", "20210203")) == [
        ("20191115", "20191231"),
        ("20200101", "20201231"),
        ("20210101", "20210203"),
    ]


def test_select_phase_includes_lower_phases():
    config = {
        "endpoints": {
            "daily": {"phase": 1, "target_table": "fact_equity_kline_1d"},
            "moneyflow": {"phase": 2, "target_table": "fact_money_flow_1d"},
        }
    }

    assert [ep.key for ep in select_endpoints(config, phase=1)] == ["daily"]
    assert [ep.key for ep in select_endpoints(config, phase=2)] == ["daily", "moneyflow"]


def test_select_priority_uses_configurable_order():
    config = {
        "priority_order": ["pit", "core", "events"],
        "endpoints": {
            "daily": {"priority": "pit", "target_table": "fact_equity_kline_1d"},
            "custom_news": {"priority": "events", "target_table": "fact_news"},
            "daily_basic": {"priority": "core", "target_table": "fact_equity_daily_basic"},
        },
    }

    assert priority_order(config) == ["pit", "core", "events"]
    assert [ep.key for ep in select_endpoints(config, priority="core")] == ["daily_basic"]
    assert [ep.key for ep in select_endpoints(config, priority_through="events")] == [
        "daily",
        "daily_basic",
        "custom_news",
    ]


def test_config_uses_vip_apis_for_p2_financial_endpoints():
    config = load_config(Path("config/tushare_to_gcs.yaml"))
    endpoints = config["endpoints"]

    assert endpoints["income"]["api_name"] == "income_vip"
    assert endpoints["balancesheet"]["api_name"] == "balancesheet_vip"
    assert endpoints["cashflow"]["api_name"] == "cashflow_vip"
    assert endpoints["fina_indicator"]["api_name"] == "fina_indicator_vip"
    assert endpoints["forecast"]["api_name"] == "forecast_vip"
    assert endpoints["express"]["api_name"] == "express_vip"


def test_config_uses_trade_date_for_suspend_d():
    config = load_config(Path("config/tushare_to_gcs.yaml"))
    suspend_d = config["endpoints"]["suspend_d"]

    assert suspend_d["api_name"] == "suspend_d"
    assert suspend_d["mode"] == "by_trade_date"
    assert suspend_d["date_param"] == "trade_date"


def test_config_uses_safe_modes_for_calendar_and_snapshot_endpoints():
    config = load_config(Path("config/tushare_to_gcs.yaml"))
    endpoints = config["endpoints"]

    assert endpoints["dividend"]["mode"] == "by_trade_date"
    assert endpoints["dividend"]["date_param"] == "ex_date"
    assert endpoints["dividend"]["row_limit"] == 5000
    assert endpoints["suspend_d"]["row_limit"] == 5000
    assert "paginate" not in endpoints["stock_basic_listed"]
    assert "paginate" not in endpoints["stock_basic_delisted"]
    assert "paginate" not in endpoints["stock_basic_pending"]
    assert endpoints["namechange"]["mode"] == "by_year_range"
    assert endpoints["namechange"]["row_limit"] == 5000
    assert "paginate" not in endpoints["namechange"]


def test_config_paginates_near_full_market_endpoints():
    config = load_config(Path("config/tushare_to_gcs.yaml"))
    endpoints = config["endpoints"]

    for key in ("stk_limit", "moneyflow", "margin_detail"):
        assert endpoints[key]["paginate"] is True
        assert endpoints[key]["page_size"] == 5000
        assert endpoints[key]["max_pages"] == 3


def test_create_tushare_pro_sets_private_http_url(monkeypatch):
    fake_tushare = FakeTushareModule()
    monkeypatch.setitem(sys.modules, "tushare", fake_tushare)
    monkeypatch.setenv("TUSHARE_TOKEN", "test-token")
    monkeypatch.setenv("TUSHARE_HTTP_URL", "http://127.0.0.1:8010/")

    pro = create_tushare_pro(
        {"tushare": {"token_env": "TUSHARE_TOKEN", "request_timeout_seconds": 7}}
    )

    assert fake_tushare.token == "test-token"
    assert pro._DataApi__http_url == "http://127.0.0.1:8010/"
    assert pro._DataApi__timeout == 7


def test_snapshot_date_is_frozen_from_run_id():
    assert snapshot_date_for_run_id("20260525T010203Z") == "20260525"
    fallback = snapshot_date_for_run_id("manual-run")
    assert len(fallback) == 8
    assert fallback.isdigit()


def test_build_jobs_uses_trade_dates_for_daily_mode():
    config = {
        "project_id": "data-aquarium",
        "gcs": {"bucket": "data-aquarium", "prefix": "a-share/tushare"},
        "tushare": {"max_calls_per_minute": 0},
    }
    ingestor = TushareGCSIngestor(config, run_id="run1")
    ingestor._trade_dates[("20260501", "20260506")] = ["20260504", "20260505", "20260506"]
    endpoint = EndpointConfig(
        key="daily",
        api_name="daily",
        target_table="fact_equity_kline_1d",
        phase=1,
        mode="by_trade_date",
        params={},
        date_param="trade_date",
    )

    jobs = ingestor.build_jobs(endpoint, "20260501", "20260506")

    assert [job.params["trade_date"] for job in jobs] == ["20260504", "20260505", "20260506"]
    assert jobs[0].partition_month == "202605"
    assert ingestor.standardized_object_name(jobs[0]).startswith(
        "a-share/tushare/standardized_parquet/fact_equity_kline_1d/partition_month=202605/run_id=run1/"
    )


def test_build_jobs_uses_year_ranges_for_namechange():
    config = load_config(Path("config/tushare_to_gcs.yaml"))
    ingestor = TushareGCSIngestor(config, run_id="20260525T010203Z")
    endpoint = select_endpoints(config, endpoint_keys=["namechange"])[0]

    jobs = ingestor.build_jobs(endpoint, "20251115", "20260525")

    assert len(jobs) == 2
    assert jobs[0].params["start_date"] == "20251115"
    assert jobs[0].params["end_date"] == "20251231"
    assert jobs[0].logical_date == "20251231"
    assert jobs[1].params["start_date"] == "20260101"
    assert jobs[1].params["end_date"] == "20260525"
    assert jobs[1].logical_date == "20260525"


def test_fetch_job_dataframe_fails_when_non_paginated_call_hits_row_limit():
    config = {
        "project_id": "data-aquarium",
        "gcs": {"bucket": "data-aquarium", "prefix": "a-share/tushare"},
        "tushare": {"max_calls_per_minute": 0},
    }
    ingestor = TushareGCSIngestor(config, run_id="run1")
    ingestor._pro = FakeTusharePro(rows=6000)
    job = IngestJob(
        endpoint_key="daily",
        api_name="daily",
        target_table="fact_equity_kline_1d",
        params={"trade_date": "20260525"},
        phase=1,
        mode="by_trade_date",
        logical_date="20260525",
        partition_month="202605",
        chunk_key="abc123",
        row_limit=6000,
    )

    with pytest.raises(RuntimeError, match="may mean Tushare truncated"):
        ingestor.fetch_job_dataframe(job)


def test_fetch_job_dataframe_paginates_until_short_page():
    config = {
        "project_id": "data-aquarium",
        "gcs": {"bucket": "data-aquarium", "prefix": "a-share/tushare"},
        "tushare": {"max_calls_per_minute": 0},
    }
    ingestor = TushareGCSIngestor(config, run_id="run1")
    fake_pro = FakeTusharePro(rows=12001)
    ingestor._pro = fake_pro
    job = IngestJob(
        endpoint_key="top10_holders",
        api_name="daily",
        target_table="fact_top10_shareholders",
        params={"period": "20251231"},
        phase=2,
        mode="by_period",
        logical_date="20251231",
        partition_month="202512",
        chunk_key="abc123",
        paginate=True,
        page_size=5000,
        max_pages=5,
    )

    df = ingestor.fetch_job_dataframe(job)

    assert len(df) == 12001
    assert [call["offset"] for call in fake_pro.calls] == [0, 5000, 10000]
    assert [call["limit"] for call in fake_pro.calls] == [5000, 5000, 5000]


def test_fetch_job_dataframe_allows_exact_max_pages_after_empty_probe():
    config = {
        "project_id": "data-aquarium",
        "gcs": {"bucket": "data-aquarium", "prefix": "a-share/tushare"},
        "tushare": {"max_calls_per_minute": 0},
    }
    ingestor = TushareGCSIngestor(config, run_id="run1")
    fake_pro = FakeTusharePro(rows=10000)
    ingestor._pro = fake_pro
    job = IngestJob(
        endpoint_key="top10_holders",
        api_name="daily",
        target_table="fact_top10_shareholders",
        params={"period": "20251231"},
        phase=2,
        mode="by_period",
        logical_date="20251231",
        partition_month="202512",
        chunk_key="abc123",
        paginate=True,
        page_size=5000,
        max_pages=2,
    )

    df = ingestor.fetch_job_dataframe(job)

    assert len(df) == 10000
    assert [call["offset"] for call in fake_pro.calls] == [0, 5000, 10000]


def test_fetch_job_dataframe_detects_ignored_limit_offset_pagination():
    config = {
        "project_id": "data-aquarium",
        "gcs": {"bucket": "data-aquarium", "prefix": "a-share/tushare"},
        "tushare": {"max_calls_per_minute": 0},
    }
    ingestor = TushareGCSIngestor(config, run_id="run1")
    ingestor._pro = FakeIgnoredPaginationPro()
    job = IngestJob(
        endpoint_key="stock_basic_listed",
        api_name="daily",
        target_table="dim_security",
        params={"list_status": "L"},
        phase=1,
        mode="snapshot",
        logical_date="20260525",
        partition_month="all",
        chunk_key="abc123",
        paginate=True,
        page_size=3,
        max_pages=2,
    )

    with pytest.raises(RuntimeError, match="duplicate pagination page"):
        ingestor.fetch_job_dataframe(job)


def test_call_tushare_treats_none_response_as_failure():
    config = {
        "project_id": "data-aquarium",
        "gcs": {"bucket": "data-aquarium", "prefix": "a-share/tushare"},
        "tushare": {"max_calls_per_minute": 0, "retry": {"attempts": 1}},
    }
    ingestor = TushareGCSIngestor(config, run_id="run1")
    ingestor._pro = FakeNoneTusharePro()

    with pytest.raises(RuntimeError, match="Tushare API call failed"):
        ingestor.call_tushare("daily", {"trade_date": "20260525"})


def test_retryable_error_detection_uses_precise_token_markers():
    assert not is_retryable_tushare_error(RuntimeError("token is invalid"))
    assert not is_retryable_tushare_error(RuntimeError("请输入参数 ts_code"))
    assert is_retryable_tushare_error(RuntimeError("connection reset while reading token"))
    assert is_retryable_tushare_error(RuntimeError("tushare token API timed out"))


def test_run_jobs_continues_after_failure_writes_summary_then_fails_run():
    config = {
        "project_id": "data-aquarium",
        "gcs": {"bucket": "data-aquarium", "prefix": "a-share/tushare"},
        "tushare": {"max_calls_per_minute": 0, "retry": {"attempts": 1}},
        "outputs": {"raw": {"enabled": True}, "standardized": {"enabled": False}},
        "checkpoint": {"enabled": True, "prefix": "_checkpoints", "skip_statuses": ["uploaded"]},
    }
    ingestor = TushareGCSIngestor(config, run_id="run1")
    fake_bucket = FakeBucket()
    ingestor._bucket = fake_bucket
    ingestor._pro = FakeRunTusharePro()
    failed_job = IngestJob(
        endpoint_key="daily",
        api_name="daily",
        target_table="fact_equity_kline_1d",
        params={"trade_date": "20260524"},
        phase=1,
        mode="by_trade_date",
        logical_date="20260524",
        partition_month="202605",
        chunk_key="failed",
        priority="p0",
    )
    uploaded_job = IngestJob(
        endpoint_key="daily",
        api_name="daily",
        target_table="fact_equity_kline_1d",
        params={"trade_date": "20260525"},
        phase=1,
        mode="by_trade_date",
        logical_date="20260525",
        partition_month="202605",
        chunk_key="uploaded",
        priority="p0",
    )

    with pytest.raises(RuntimeError, match="1 Tushare ingestion job"):
        ingestor.run_jobs([failed_job, uploaded_job])

    summary_name = "a-share/tushare/_manifests/run_id=run1/summary.json"
    summary = json.loads(fake_bucket.store[summary_name].decode("utf-8"))
    assert summary["failed_records"] == 1
    assert summary["uploaded_records"] == 1
    assert ingestor.checkpoint_object_name(failed_job) not in fake_bucket.store
    assert ingestor.checkpoint_object_name(uploaded_job) in fake_bucket.store


def test_checkpoint_can_skip_completed_job():
    config = {
        "project_id": "data-aquarium",
        "gcs": {"bucket": "data-aquarium", "prefix": "a-share/tushare"},
        "tushare": {"max_calls_per_minute": 0},
        "checkpoint": {"enabled": True, "prefix": "_checkpoints", "skip_statuses": ["uploaded"]},
    }
    ingestor = TushareGCSIngestor(config, run_id="run1")
    ingestor._bucket = FakeBucket()
    record = {
        "run_id": "previous",
        "endpoint_key": "daily",
        "api_name": "daily",
        "target_table": "fact_equity_kline_1d",
        "priority": "p0",
        "params": {"trade_date": "20260525"},
        "logical_date": "20260525",
        "partition_month": "202605",
        "rows": 5000,
        "status": "uploaded",
        "raw_uri": "gs://data-aquarium/a-share/tushare/raw/example.parquet",
        "standardized_uri": None,
        "ingested_at": "2026-05-25T00:00:00+00:00",
        "error": None,
    }
    job = IngestJob(
        endpoint_key="daily",
        api_name="daily",
        target_table="fact_equity_kline_1d",
        params={"trade_date": "20260525"},
        phase=1,
        mode="by_trade_date",
        logical_date="20260525",
        partition_month="202605",
        chunk_key="abc123",
        priority="p0",
    )
    ingestor.bucket.blob(ingestor.checkpoint_object_name(job)).upload_from_string(json.dumps(record))

    checkpoint = ingestor.should_skip_completed(job)

    assert checkpoint is not None
    assert checkpoint["rows"] == 5000
    assert checkpoint["status"] == "uploaded"


def test_snapshot_endpoint_partitions_to_all():
    config = {
        "project_id": "data-aquarium",
        "gcs": {"bucket": "data-aquarium", "prefix": "a-share/tushare"},
        "tushare": {"max_calls_per_minute": 0},
    }
    ingestor = TushareGCSIngestor(config, run_id="run1")
    endpoint = EndpointConfig(
        key="stock_basic_listed",
        api_name="stock_basic",
        target_table="dim_security",
        phase=1,
        mode="snapshot",
        params={"list_status": "L"},
        partition="all",
    )

    jobs = ingestor.build_jobs(endpoint, "20190101", "20260525")

    assert len(jobs) == 1
    assert jobs[0].partition_month == "all"
    assert jobs[0].logical_date == ingestor.snapshot_date


def test_default_rate_limit_is_120_calls_per_minute():
    config = {
        "project_id": "data-aquarium",
        "gcs": {"bucket": "data-aquarium", "prefix": "a-share/tushare"},
    }

    ingestor = TushareGCSIngestor(config, run_id="run1")

    assert ingestor.rate_limiter.min_interval == 0.5


def test_partition_month_for_falls_back_to_all_for_snapshot_values():
    assert partition_month_for("auto", "20260525") == "202605"
    assert partition_month_for("all", "20260525") == "all"
    assert partition_month_for("auto", "snapshot") == "all"


def test_add_ingestion_metadata_preserves_request_context():
    job = IngestJob(
        endpoint_key="daily",
        api_name="daily",
        target_table="fact_equity_kline_1d",
        params={"trade_date": "20260525"},
        phase=1,
        mode="by_trade_date",
        logical_date="20260525",
        partition_month="202605",
        chunk_key="abc123",
    )
    df = pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20260525", "close": 10.5}])

    out = add_ingestion_metadata(df, job, "run1", "2026-05-25T00:00:00+00:00")

    assert out["_source"].iloc[0] == "tushare"
    assert out["_run_id"].iloc[0] == "run1"
    assert json.loads(out["_request_params_json"].iloc[0]) == {"trade_date": "20260525"}


def test_standardize_dataframe_adds_common_feature_columns():
    df = pd.DataFrame(
        [
            {
                "ts_code": "000001.SZ",
                "trade_date": "20260525",
                "ann_date": "20260430",
                "end_date": "20260331",
            }
        ]
    )

    out = standardize_dataframe(df)

    assert out["security_code"].iloc[0] == "000001.SZ"
    assert out["date"].iloc[0] == "2026-05-25"
    assert out["announcement_date"].iloc[0] == "2026-04-30"
    assert out["report_period"].iloc[0] == "2026-03-31"


def test_standardize_dataframe_uses_dividend_ex_date_as_date():
    df = pd.DataFrame([{"ts_code": "000001.SZ", "ex_date": "20260525"}])

    out = standardize_dataframe(df)

    assert out["date"].iloc[0] == "2026-05-25"


def test_dataframe_to_parquet_bytes_round_trips():
    df = pd.DataFrame([{"ts_code": "000001.SZ", "trade_date": "20260525", "close": 10.5}])

    payload = dataframe_to_parquet_bytes(df)
    restored = pd.read_parquet(__import__("io").BytesIO(payload))

    assert restored.to_dict("records") == df.to_dict("records")
