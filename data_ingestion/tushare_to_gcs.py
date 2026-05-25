from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

import pandas as pd
import yaml

from data_ingestion.tushare_client import create_tushare_pro


DEFAULT_CONFIG_PATH = Path("config/tushare_to_gcs.yaml")
REQUIREMENTS_INSTALL_HINT = "Install dependencies with: python -m pip install -r requirements.txt"
DEFAULT_PRIORITY_ORDER = ("p0", "p1", "p2")


@dataclass(frozen=True)
class EndpointConfig:
    key: str
    api_name: str
    target_table: str
    phase: int
    mode: str
    params: dict
    priority: str = "p1"
    fields: str | None = None
    date_param: str | None = None
    partition: str = "auto"
    enabled: bool = True
    start_date: str | None = None
    end_date: str | None = None
    row_limit: int | None = None
    row_limit_action: str = "fail"
    paginate: bool = False
    page_size: int | None = None
    max_pages: int | None = None


@dataclass(frozen=True)
class IngestJob:
    endpoint_key: str
    api_name: str
    target_table: str
    params: dict
    phase: int
    mode: str
    logical_date: str
    partition_month: str
    chunk_key: str
    fields: str | None = None
    priority: str = "p1"
    row_limit: int | None = None
    row_limit_action: str = "fail"
    paginate: bool = False
    page_size: int | None = None
    max_pages: int | None = None


@dataclass(frozen=True)
class UploadResult:
    gcs_uri: str
    size_bytes: int
    rows: int


@dataclass(frozen=True)
class ManifestRecord:
    run_id: str
    endpoint_key: str
    api_name: str
    target_table: str
    priority: str
    params: dict
    logical_date: str
    partition_month: str
    rows: int
    status: str
    raw_uri: str | None
    standardized_uri: str | None
    ingested_at: str
    error: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def default_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def today_yyyymmdd() -> str:
    return date.today().strftime("%Y%m%d")


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def normalize_yyyymmdd(value: str | int | None, *, default: str | None = None) -> str:
    if value is None:
        if default is None:
            raise ValueError("Date value is required")
        value = default
    text = str(value).strip().replace("-", "")
    if len(text) != 8 or not text.isdigit():
        raise ValueError(f"Expected YYYYMMDD date, got: {value!r}")
    datetime.strptime(text, "%Y%m%d")
    return text


def yyyymmdd_to_iso(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().replace("-", "")
    if len(text) == 8 and text.isdigit():
        return f"{text[:4]}-{text[4:6]}-{text[6:8]}"
    return str(value)


def iter_calendar_dates(start_date: str, end_date: str) -> Iterable[str]:
    current = datetime.strptime(start_date, "%Y%m%d").date()
    end = datetime.strptime(end_date, "%Y%m%d").date()
    while current <= end:
        yield current.strftime("%Y%m%d")
        current += timedelta(days=1)


def iter_quarter_periods(start_date: str, end_date: str) -> Iterable[str]:
    start = datetime.strptime(start_date, "%Y%m%d").date()
    end = datetime.strptime(end_date, "%Y%m%d").date()
    for year in range(start.year, end.year + 1):
        for month, day in ((3, 31), (6, 30), (9, 30), (12, 31)):
            period = date(year, month, day)
            if start <= period <= end:
                yield period.strftime("%Y%m%d")


def stable_hash(payload: object, length: int = 12) -> str:
    data = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str).encode("utf-8")
    return hashlib.sha256(data).hexdigest()[:length]


def normalize_priority(value: str | None) -> str:
    priority = (value or "p1").strip().lower()
    if not priority:
        raise ValueError("Priority cannot be empty.")
    return priority


def priority_order(config: dict) -> list[str]:
    configured = config.get("priority_order") or DEFAULT_PRIORITY_ORDER
    order: list[str] = []
    for raw in configured:
        priority = normalize_priority(str(raw))
        if priority not in order:
            order.append(priority)

    for raw in (config.get("endpoints") or {}).values():
        if not raw:
            continue
        phase = int(raw.get("phase", 1))
        priority = normalize_priority(raw.get("priority") or priority_from_phase(phase))
        if priority not in order:
            order.append(priority)
    return order


def priority_rank(config: dict) -> dict[str, int]:
    return {name: rank for rank, name in enumerate(priority_order(config))}


def priority_from_phase(phase: int) -> str:
    if phase <= 1:
        return "p1"
    if phase == 2:
        return "p2"
    return "p2"


def endpoint_configs(config: dict) -> list[EndpointConfig]:
    endpoints = []
    for key, raw in (config.get("endpoints") or {}).items():
        if raw is None:
            continue
        phase = int(raw.get("phase", 1))
        endpoints.append(
            EndpointConfig(
                key=key,
                api_name=raw.get("api_name", key),
                target_table=raw["target_table"],
                phase=phase,
                mode=raw.get("mode", "snapshot"),
                params=dict(raw.get("params") or {}),
                priority=normalize_priority(raw.get("priority") or priority_from_phase(phase)),
                fields=raw.get("fields"),
                date_param=raw.get("date_param"),
                partition=raw.get("partition", "auto"),
                enabled=bool(raw.get("enabled", True)),
                start_date=raw.get("start_date"),
                end_date=raw.get("end_date"),
                row_limit=int(raw["row_limit"]) if raw.get("row_limit") is not None else None,
                row_limit_action=raw.get("row_limit_action", "fail"),
                paginate=bool(raw.get("paginate", False)),
                page_size=int(raw["page_size"]) if raw.get("page_size") is not None else None,
                max_pages=int(raw["max_pages"]) if raw.get("max_pages") is not None else None,
            )
        )
    return endpoints


def sort_endpoints_by_priority(config: dict, endpoints: Sequence[EndpointConfig]) -> list[EndpointConfig]:
    rank = priority_rank(config)
    return [
        endpoint
        for _, endpoint in sorted(
            enumerate(endpoints),
            key=lambda item: (rank.get(item[1].priority, len(rank)), item[0]),
        )
    ]


def select_endpoints(
    config: dict,
    *,
    phase: int | None = None,
    priority: str | None = None,
    priority_through: str | None = None,
    endpoint_keys: Sequence[str] | None = None,
) -> list[EndpointConfig]:
    selected = [ep for ep in endpoint_configs(config) if ep.enabled]
    rank = priority_rank(config)
    if priority and priority_through:
        raise ValueError("Use either priority or priority_through, not both.")
    if endpoint_keys:
        keys = set(endpoint_keys)
        selected = [ep for ep in selected if ep.key in keys]
        missing = keys - {ep.key for ep in selected}
        if missing:
            raise ValueError(f"Unknown or disabled endpoint(s): {', '.join(sorted(missing))}")
        return sort_endpoints_by_priority(config, selected)
    if priority:
        selected_priority = normalize_priority(priority)
        selected = [ep for ep in selected if ep.priority == selected_priority]
    if priority_through:
        selected_priority = normalize_priority(priority_through)
        if selected_priority not in rank:
            raise ValueError(
                f"Unknown priority_through={priority_through!r}. "
                f"Known priorities from config: {', '.join(priority_order(config))}"
            )
        max_rank = rank[selected_priority]
        selected = [ep for ep in selected if rank.get(ep.priority, len(rank)) <= max_rank]
    if phase is not None:
        selected = [ep for ep in selected if ep.phase <= phase]
    return sort_endpoints_by_priority(config, selected)


def require_storage():
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency: google-cloud-storage. {REQUIREMENTS_INSTALL_HINT}"
        ) from exc
    return storage


def gcloud_access_token(timeout_seconds: int) -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token", "--quiet"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout_seconds,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("gcloud did not return an access token")
    return token


def gcloud_credentials(config: dict):
    try:
        from google.auth.credentials import Credentials
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency: google-auth. {REQUIREMENTS_INSTALL_HINT}") from exc

    class GcloudAccessTokenCredentials(Credentials):
        def __init__(self, credential_config: dict) -> None:
            super().__init__()
            self._credential_config = credential_config
            self.refresh(None)

        def refresh(self, request) -> None:
            timeout = int(self._credential_config.get("auth", {}).get("gcloud_token_timeout_seconds", 30))
            self.token = gcloud_access_token(timeout)
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=50)

    return GcloudAccessTokenCredentials(config)


def use_gcloud_access_token(config: dict) -> bool:
    env_value = os.environ.get("ASHARE_USE_GCLOUD_ACCESS_TOKEN", "").strip().lower()
    if env_value in {"1", "true", "yes", "on"}:
        return True
    return bool(config.get("auth", {}).get("use_gcloud_access_token", False))


def storage_client(config: dict):
    storage = require_storage()
    kwargs = {"project": config["project_id"]}
    if use_gcloud_access_token(config):
        kwargs["credentials"] = gcloud_credentials(config)
    return storage.Client(**kwargs)


def tushare_client(config: dict):
    return create_tushare_pro(config)


class RateLimiter:
    def __init__(self, max_calls_per_minute: int) -> None:
        self.min_interval = 60.0 / max_calls_per_minute if max_calls_per_minute > 0 else 0.0
        self._last_call = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        elapsed = time.monotonic() - self._last_call
        sleep_for = self.min_interval - elapsed
        if sleep_for > 0:
            time.sleep(sleep_for)
        self._last_call = time.monotonic()


class TushareGCSIngestor:
    def __init__(self, config: dict, *, run_id: str | None = None) -> None:
        self.config = config
        self.run_id = run_id or default_run_id()
        self.prefix = config["gcs"]["prefix"].strip("/")
        self.bucket_name = config["gcs"]["bucket"]
        self._storage_client = None
        self._bucket = None
        self._pro = None
        max_calls = int(config.get("tushare", {}).get("max_calls_per_minute", 120))
        self.rate_limiter = RateLimiter(max_calls)
        self._trade_dates: dict[tuple[str, str], list[str]] = {}

    @property
    def pro(self):
        if self._pro is None:
            self._pro = tushare_client(self.config)
        return self._pro

    @property
    def bucket(self):
        if self._bucket is None:
            self._storage_client = storage_client(self.config)
            self._bucket = self._storage_client.bucket(self.bucket_name)
        return self._bucket

    def call_tushare(self, api_name: str, params: dict, fields: str | None = None) -> pd.DataFrame:
        attempts = int(self.config.get("tushare", {}).get("retry", {}).get("attempts", 3))
        initial_sleep = float(self.config.get("tushare", {}).get("retry", {}).get("initial_sleep_seconds", 5))
        backoff = float(self.config.get("tushare", {}).get("retry", {}).get("backoff_multiplier", 2))
        clean_params = {k: v for k, v in params.items() if v is not None}
        if fields:
            clean_params["fields"] = fields

        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            self.rate_limiter.wait()
            try:
                func = getattr(self.pro, api_name, None)
                if callable(func):
                    df = func(**clean_params)
                else:
                    df = self.pro.query(api_name, **clean_params)
                if df is None:
                    return pd.DataFrame()
                return df
            except Exception as exc:  # pragma: no cover - exercised in Cloud Run, not unit tests
                last_error = exc
                if attempt >= attempts:
                    break
                time.sleep(initial_sleep * (backoff ** (attempt - 1)))
        raise RuntimeError(f"Tushare API call failed: {api_name} params={clean_params}") from last_error

    def trade_dates(self, start_date: str, end_date: str) -> list[str]:
        key = (start_date, end_date)
        if key in self._trade_dates:
            return self._trade_dates[key]
        df = self.call_tushare(
            "trade_cal",
            {"exchange": self.config.get("tushare", {}).get("calendar_exchange", "SSE"),
             "start_date": start_date,
             "end_date": end_date,
             "is_open": "1"},
        )
        if df.empty or "cal_date" not in df.columns:
            raise RuntimeError("trade_cal returned no open calendar dates")
        dates = sorted(df["cal_date"].astype(str).tolist())
        self._trade_dates[key] = dates
        return dates

    def build_jobs(self, endpoint: EndpointConfig, start_date: str, end_date: str) -> list[IngestJob]:
        ep_start = normalize_yyyymmdd(endpoint.start_date, default=start_date) if endpoint.start_date else start_date
        ep_end = normalize_yyyymmdd(endpoint.end_date, default=end_date) if endpoint.end_date else end_date
        if endpoint.mode == "snapshot":
            return [self._job(endpoint, dict(endpoint.params), today_yyyymmdd())]
        if endpoint.mode == "date_range":
            params = {**endpoint.params, "start_date": ep_start, "end_date": ep_end}
            return [self._job(endpoint, params, ep_end)]
        if endpoint.mode == "by_trade_date":
            date_param = endpoint.date_param or "trade_date"
            return [
                self._job(endpoint, {**endpoint.params, date_param: trade_date}, trade_date)
                for trade_date in self.trade_dates(ep_start, ep_end)
            ]
        if endpoint.mode == "by_calendar_date":
            date_param = endpoint.date_param or "trade_date"
            return [
                self._job(endpoint, {**endpoint.params, date_param: calendar_date}, calendar_date)
                for calendar_date in iter_calendar_dates(ep_start, ep_end)
            ]
        if endpoint.mode == "by_period":
            date_param = endpoint.date_param or "period"
            return [
                self._job(endpoint, {**endpoint.params, date_param: period}, period)
                for period in iter_quarter_periods(ep_start, ep_end)
            ]
        raise ValueError(f"Unsupported endpoint mode for {endpoint.key}: {endpoint.mode}")

    def _job(self, endpoint: EndpointConfig, params: dict, logical_date: str) -> IngestJob:
        partition_month = partition_month_for(endpoint.partition, logical_date)
        chunk_key = stable_hash(
            {
                "endpoint": endpoint.key,
                "api": endpoint.api_name,
                "target_table": endpoint.target_table,
                "params": params,
                "logical_date": logical_date,
            }
        )
        return IngestJob(
            endpoint_key=endpoint.key,
            api_name=endpoint.api_name,
            target_table=endpoint.target_table,
            params=params,
            phase=endpoint.phase,
            mode=endpoint.mode,
            logical_date=logical_date,
            partition_month=partition_month,
            chunk_key=chunk_key,
            fields=endpoint.fields,
            priority=endpoint.priority,
            row_limit=endpoint.row_limit,
            row_limit_action=endpoint.row_limit_action,
            paginate=endpoint.paginate,
            page_size=endpoint.page_size,
            max_pages=endpoint.max_pages,
        )

    def checkpoint_enabled(self) -> bool:
        return bool(self.config.get("checkpoint", {}).get("enabled", True))

    def checkpoint_skip_statuses(self) -> set[str]:
        return set(self.config.get("checkpoint", {}).get("skip_statuses", ["uploaded"]))

    def checkpoint_object_name(self, job: IngestJob) -> str:
        checkpoint_prefix = self.config.get("checkpoint", {}).get("prefix", "_checkpoints").strip("/")
        return (
            f"{self.prefix}/{checkpoint_prefix}/endpoint={job.endpoint_key}/"
            f"logical_date={job.logical_date}.json"
        )

    def read_checkpoint(self, job: IngestJob) -> dict | None:
        if not self.checkpoint_enabled():
            return None
        blob = self.bucket.blob(self.checkpoint_object_name(job))
        if not blob.exists():
            return None
        payload = blob.download_as_bytes()
        return json.loads(payload.decode("utf-8"))

    def should_skip_completed(self, job: IngestJob) -> dict | None:
        checkpoint = self.read_checkpoint(job)
        if not checkpoint:
            return None
        if checkpoint.get("status") in self.checkpoint_skip_statuses():
            return checkpoint
        return None

    def upload_checkpoint(self, record: ManifestRecord) -> None:
        if not self.checkpoint_enabled() or record.status not in {"uploaded", "empty"}:
            return
        job = IngestJob(
            endpoint_key=record.endpoint_key,
            api_name=record.api_name,
            target_table=record.target_table,
            params=record.params,
            phase=0,
            mode="checkpoint",
            logical_date=record.logical_date,
            partition_month=record.partition_month,
            chunk_key="checkpoint",
            priority=record.priority,
        )
        payload = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True).encode("utf-8")
        self.bucket.blob(self.checkpoint_object_name(job)).upload_from_string(
            payload,
            content_type="application/json",
        )

    def fetch_job_dataframe(self, job: IngestJob) -> pd.DataFrame:
        if job.paginate:
            page_size = job.page_size or min(job.row_limit or 5000, 5000)
            max_pages = job.max_pages or 100
            pages: list[pd.DataFrame] = []
            offset = 0
            for page_number in range(1, max_pages + 1):
                page_params = {**job.params, "limit": page_size, "offset": offset}
                page = self.call_tushare(job.api_name, page_params, fields=job.fields)
                if page.empty:
                    break
                pages.append(page)
                if len(page) < page_size:
                    break
                offset += page_size
            else:
                raise RuntimeError(
                    f"{job.endpoint_key} reached max_pages={max_pages}; "
                    "increase max_pages or split the request more finely."
                )
            if not pages:
                return pd.DataFrame()
            return pd.concat(pages, ignore_index=True)

        df = self.call_tushare(job.api_name, job.params, fields=job.fields)
        self.validate_row_limit(job, df)
        return df

    def validate_row_limit(self, job: IngestJob, df: pd.DataFrame) -> None:
        if job.row_limit is None or df.empty:
            return
        rows = len(df)
        if rows < job.row_limit:
            return
        message = (
            f"{job.endpoint_key} returned {rows} rows, meeting configured row_limit={job.row_limit}. "
            "This may mean Tushare truncated the response; enable pagination or split the request."
        )
        if job.row_limit_action == "warn":
            print(f"WARNING: {message}")
            return
        raise RuntimeError(message)

    def run_jobs(self, jobs: Sequence[IngestJob], *, max_calls: int | None = None) -> list[ManifestRecord]:
        records: list[ManifestRecord] = []
        write_raw = bool(self.config.get("outputs", {}).get("raw", {}).get("enabled", True))
        write_standardized = bool(self.config.get("outputs", {}).get("standardized", {}).get("enabled", False))
        if not write_raw and not write_standardized:
            raise ValueError("At least one output must be enabled: outputs.raw or outputs.standardized")

        for index, job in enumerate(jobs, start=1):
            if max_calls is not None and index > max_calls:
                break
            ingested_at = utc_now()
            try:
                checkpoint = self.should_skip_completed(job)
                if checkpoint:
                    record = ManifestRecord(
                        run_id=self.run_id,
                        endpoint_key=job.endpoint_key,
                        api_name=job.api_name,
                        target_table=job.target_table,
                        priority=job.priority,
                        params=job.params,
                        logical_date=job.logical_date,
                        partition_month=job.partition_month,
                        rows=int(checkpoint.get("rows") or 0),
                        status="skipped",
                        raw_uri=checkpoint.get("raw_uri"),
                        standardized_uri=checkpoint.get("standardized_uri"),
                        ingested_at=ingested_at,
                    )
                    self.upload_manifest_record(record, index)
                    records.append(record)
                    print(f"[{index}/{len(jobs)}] skipped {job.endpoint_key} {job.logical_date}")
                    continue

                df = self.fetch_job_dataframe(job)
                if df.empty:
                    record = ManifestRecord(
                        run_id=self.run_id,
                        endpoint_key=job.endpoint_key,
                        api_name=job.api_name,
                        target_table=job.target_table,
                        priority=job.priority,
                        params=job.params,
                        logical_date=job.logical_date,
                        partition_month=job.partition_month,
                        rows=0,
                        status="empty",
                        raw_uri=None,
                        standardized_uri=None,
                        ingested_at=ingested_at,
                    )
                    self.upload_manifest_record(record, index)
                    self.upload_checkpoint(record)
                    records.append(record)
                    print(f"[{index}/{len(jobs)}] empty {job.endpoint_key} {job.logical_date}")
                    continue

                raw_df = add_ingestion_metadata(df, job, self.run_id, ingested_at)
                std_df = standardize_dataframe(raw_df) if write_standardized else None
                raw_result = self.upload_dataframe(raw_df, self.raw_object_name(job)) if write_raw else None
                std_result = (
                    self.upload_dataframe(std_df, self.standardized_object_name(job))
                    if write_standardized and std_df is not None
                    else None
                )
                record = ManifestRecord(
                    run_id=self.run_id,
                    endpoint_key=job.endpoint_key,
                    api_name=job.api_name,
                    target_table=job.target_table,
                    priority=job.priority,
                    params=job.params,
                    logical_date=job.logical_date,
                    partition_month=job.partition_month,
                    rows=len(df),
                    status="uploaded",
                    raw_uri=raw_result.gcs_uri if raw_result else None,
                    standardized_uri=std_result.gcs_uri if std_result else None,
                    ingested_at=ingested_at,
                )
                self.upload_manifest_record(record, index)
                self.upload_checkpoint(record)
                records.append(record)
                uploaded_targets = [
                    uri for uri in (record.raw_uri, record.standardized_uri)
                    if uri is not None
                ]
                print(
                    f"[{index}/{len(jobs)}] uploaded {job.endpoint_key} {job.logical_date} "
                    f"rows={len(df)} targets={','.join(uploaded_targets)}"
                )
            except Exception as exc:
                record = ManifestRecord(
                    run_id=self.run_id,
                    endpoint_key=job.endpoint_key,
                    api_name=job.api_name,
                    target_table=job.target_table,
                    priority=job.priority,
                    params=job.params,
                    logical_date=job.logical_date,
                    partition_month=job.partition_month,
                    rows=0,
                    status="failed",
                    raw_uri=None,
                    standardized_uri=None,
                    ingested_at=ingested_at,
                    error=str(exc),
                )
                self.upload_manifest_record(record, index)
                records.append(record)
                raise
        self.upload_run_summary(records)
        return records

    def raw_object_name(self, job: IngestJob) -> str:
        return (
            f"{self.prefix}/raw/api={job.api_name}/endpoint={job.endpoint_key}/"
            f"partition_date={date_partition(job.logical_date)}/run_id={self.run_id}/"
            f"{job.endpoint_key}_{job.logical_date}_{job.chunk_key}.parquet"
        )

    def standardized_object_name(self, job: IngestJob) -> str:
        return (
            f"{self.prefix}/standardized_parquet/{job.target_table}/"
            f"partition_month={job.partition_month}/run_id={self.run_id}/"
            f"{job.endpoint_key}_{job.logical_date}_{job.chunk_key}.parquet"
        )

    def manifest_object_name(self, sequence: int, record: ManifestRecord) -> str:
        return (
            f"{self.prefix}/_manifests/run_id={self.run_id}/records/"
            f"{sequence:06d}_{record.endpoint_key}_{record.logical_date}.json"
        )

    def summary_object_name(self) -> str:
        return f"{self.prefix}/_manifests/run_id={self.run_id}/summary.json"

    def upload_dataframe(self, df: pd.DataFrame, object_name: str) -> UploadResult:
        payload = dataframe_to_parquet_bytes(df)
        blob = self.bucket.blob(object_name)
        blob.metadata = {
            "run_id": self.run_id,
            "source": "tushare",
        }
        blob.upload_from_string(payload, content_type="application/octet-stream")
        return UploadResult(
            gcs_uri=f"gs://{self.bucket_name}/{object_name}",
            size_bytes=len(payload),
            rows=len(df),
        )

    def upload_manifest_record(self, record: ManifestRecord, sequence: int) -> None:
        payload = json.dumps(asdict(record), ensure_ascii=False, sort_keys=True).encode("utf-8")
        blob = self.bucket.blob(self.manifest_object_name(sequence, record))
        blob.upload_from_string(payload, content_type="application/json")

    def upload_run_summary(self, records: Sequence[ManifestRecord]) -> None:
        summary = {
            "run_id": self.run_id,
            "created_at": utc_now(),
            "bucket": self.bucket_name,
            "prefix": self.prefix,
            "records": len(records),
            "uploaded_records": sum(1 for r in records if r.status == "uploaded"),
            "empty_records": sum(1 for r in records if r.status == "empty"),
            "failed_records": sum(1 for r in records if r.status == "failed"),
            "rows": sum(r.rows for r in records),
        }
        payload = json.dumps(summary, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
        self.bucket.blob(self.summary_object_name()).upload_from_string(payload, content_type="application/json")


def date_partition(logical_date: str) -> str:
    if logical_date == "snapshot":
        return today_yyyymmdd()
    if len(logical_date) >= 8 and logical_date[:8].isdigit():
        return logical_date[:8]
    return "snapshot"


def partition_month_for(partition: str, logical_date: str) -> str:
    if partition == "all":
        return "all"
    if len(logical_date) >= 6 and logical_date[:6].isdigit():
        return logical_date[:6]
    return "all"


def add_ingestion_metadata(df: pd.DataFrame, job: IngestJob, run_id: str, ingested_at: str) -> pd.DataFrame:
    out = df.copy()
    out["_source"] = "tushare"
    out["_tushare_api"] = job.api_name
    out["_endpoint_key"] = job.endpoint_key
    out["_target_table"] = job.target_table
    out["_run_id"] = run_id
    out["_ingested_at"] = ingested_at
    out["_logical_date"] = job.logical_date
    out["_request_params_json"] = json.dumps(job.params, ensure_ascii=False, sort_keys=True)
    return out


def standardize_dataframe(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "ts_code" in out.columns and "security_code" not in out.columns:
        out["security_code"] = out["ts_code"].astype(str)
    if "con_code" in out.columns and "component_security_code" not in out.columns:
        out["component_security_code"] = out["con_code"].astype(str)
    if "trade_date" in out.columns and "date" not in out.columns:
        out["date"] = out["trade_date"].map(yyyymmdd_to_iso)
    elif "cal_date" in out.columns and "date" not in out.columns:
        out["date"] = out["cal_date"].map(yyyymmdd_to_iso)
    elif "suspend_date" in out.columns and "date" not in out.columns:
        out["date"] = out["suspend_date"].map(yyyymmdd_to_iso)
    elif "ex_date" in out.columns and "date" not in out.columns:
        out["date"] = out["ex_date"].map(yyyymmdd_to_iso)
    if "ann_date" in out.columns and "announcement_date" not in out.columns:
        out["announcement_date"] = out["ann_date"].map(yyyymmdd_to_iso)
    if "f_ann_date" in out.columns and "actual_announcement_date" not in out.columns:
        out["actual_announcement_date"] = out["f_ann_date"].map(yyyymmdd_to_iso)
    if "end_date" in out.columns and "report_period" not in out.columns:
        out["report_period"] = out["end_date"].map(yyyymmdd_to_iso)
    if "period" in out.columns and "report_period" not in out.columns:
        out["report_period"] = out["period"].map(yyyymmdd_to_iso)
    return out


def dataframe_to_parquet_bytes(df: pd.DataFrame) -> bytes:
    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False, engine="auto")
    return buffer.getvalue()


def build_all_jobs(
    ingestor: TushareGCSIngestor,
    endpoints: Sequence[EndpointConfig],
    *,
    start_date: str,
    end_date: str,
) -> list[IngestJob]:
    jobs: list[IngestJob] = []
    for endpoint in endpoints:
        jobs.extend(ingestor.build_jobs(endpoint, start_date, end_date))
    return jobs


def print_plan(config: dict, endpoints: Sequence[EndpointConfig], start_date: str, end_date: str) -> None:
    print(f"Configured range: {start_date} -> {end_date}")
    print(f"Priority order: {', '.join(priority_order(config))}")
    for endpoint in endpoints:
        print(
            f"- {endpoint.priority} {endpoint.key}: api={endpoint.api_name}, phase={endpoint.phase}, "
            f"mode={endpoint.mode}, target={endpoint.target_table}"
        )
    print("Exact by_trade_date call counts are resolved at run time via Tushare trade_cal.")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingest Tushare data directly to GCS as Parquet snapshots.")
    parser.add_argument("command", choices=("plan", "run"))
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--phase", type=int, help="Deprecated: run endpoints with phase <= this value.")
    parser.add_argument("--priority", help="Run exactly one priority bucket from config, e.g. p0.")
    parser.add_argument("--priority-through", help="Run priority buckets in config order up to this bucket, e.g. p2.")
    parser.add_argument("--endpoint", action="append", dest="endpoints", help="Run one endpoint key. Repeatable.")
    parser.add_argument("--start-date", default=None, help="YYYYMMDD, defaults to config.defaults.start_date.")
    parser.add_argument("--end-date", default=None, help="YYYYMMDD, defaults to today.")
    parser.add_argument("--run-id", default=None, help="Stable run id. Defaults to current UTC timestamp.")
    parser.add_argument("--max-calls", type=int, default=None, help="Stop after N endpoint calls; useful for smoke tests.")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    config = load_config(Path(args.config))
    defaults = config.get("defaults", {})
    start_date = normalize_yyyymmdd(args.start_date, default=defaults.get("start_date", "20190101"))
    end_date = normalize_yyyymmdd(args.end_date, default=defaults.get("end_date") or today_yyyymmdd())
    endpoints = select_endpoints(
        config,
        phase=args.phase,
        priority=args.priority,
        priority_through=args.priority_through,
        endpoint_keys=args.endpoints,
    )

    if args.command == "plan":
        print_plan(config, endpoints, start_date, end_date)
        return 0

    ingestor = TushareGCSIngestor(config, run_id=args.run_id)
    jobs = build_all_jobs(ingestor, endpoints, start_date=start_date, end_date=end_date)
    if not jobs:
        print("No jobs selected.")
        return 0
    ingestor.run_jobs(jobs, max_calls=args.max_calls)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
