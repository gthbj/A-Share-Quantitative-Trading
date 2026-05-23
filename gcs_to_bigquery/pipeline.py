from __future__ import annotations

import argparse
import copy
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence

import yaml


VALID_STATUSES_TO_SKIP = {"loaded", "merged", "skipped"}
LOADABLE_STATUSES = {"pending"}
DEFAULT_MANIFEST_PATH = "${HOME}/.local/state/ashare/ods_pipeline_manifest.jsonl"
WINDOWS_MANIFEST_RELATIVE_PATH = "AppData/Local/ashare/ods_pipeline_manifest.jsonl"
REQUIREMENTS_INSTALL_HINT = "Install it with: python -m pip install -r gcs_to_bigquery/requirements.txt"


@dataclass(frozen=True)
class LoadRecord:
    batch_id: str
    gcs_uri: str
    target_table: str
    partition_month: int | None
    object_size: int
    object_generation: str
    source_format: str
    load_mode: str
    status: str = "pending"
    bq_job_id: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    error_message: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def batch_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def load_config(path: Path) -> dict:
    config = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    config.setdefault("manifest_path", DEFAULT_MANIFEST_PATH)
    return config


def default_manifest_path() -> Path:
    home = os.environ.get("HOME")
    if home:
        return Path(home) / ".local" / "state" / "ashare" / "ods_pipeline_manifest.jsonl"

    userprofile = os.environ.get("USERPROFILE")
    if userprofile:
        return Path(userprofile) / WINDOWS_MANIFEST_RELATIVE_PATH

    return Path.home() / ".local" / "state" / "ashare" / "ods_pipeline_manifest.jsonl"


def norm_path(value: str | None) -> Path:
    if not value:
        return default_manifest_path().resolve()

    if "${HOME}" in value and not os.environ.get("HOME") and os.environ.get("USERPROFILE"):
        if value == DEFAULT_MANIFEST_PATH:
            value = str(Path(os.environ["USERPROFILE"]) / WINDOWS_MANIFEST_RELATIVE_PATH)
        else:
            value = value.replace("${HOME}", os.environ["USERPROFILE"])

    return Path(os.path.expandvars(os.path.expanduser(value))).resolve()


def manifest_path(config: dict) -> Path:
    return norm_path(config.get("manifest_path"))


def ensure_manifest_parent(config: dict) -> None:
    manifest_path(config).parent.mkdir(parents=True, exist_ok=True)


def require_bigquery():
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency: google-cloud-bigquery. {REQUIREMENTS_INSTALL_HINT}"
        ) from exc
    return bigquery


def require_storage():
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency: google-cloud-storage. {REQUIREMENTS_INSTALL_HINT}"
        ) from exc
    return storage


def gcloud_token_timeout_seconds(config: dict | None = None) -> int:
    auth_config = (config or {}).get("auth", {})
    return int(auth_config.get("gcloud_token_timeout_seconds", 30))


def gcloud_access_token(config: dict | None = None) -> str:
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token", "--quiet"],
        check=True,
        capture_output=True,
        text=True,
        timeout=gcloud_token_timeout_seconds(config),
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("gcloud did not return an access token")
    return token


def gcloud_credentials(config: dict):
    try:
        from google.auth.credentials import Credentials
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency: google-auth. {REQUIREMENTS_INSTALL_HINT}"
        ) from exc

    class GcloudAccessTokenCredentials(Credentials):
        def __init__(self, credential_config: dict) -> None:
            super().__init__()
            self._credential_config = credential_config
            self.refresh(None)

        def refresh(self, request) -> None:
            self.token = gcloud_access_token(self._credential_config)
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=50)

    return GcloudAccessTokenCredentials(config)


def use_gcloud_access_token(config: dict) -> bool:
    env_value = os.environ.get("ASHARE_USE_GCLOUD_ACCESS_TOKEN", "").strip().lower()
    if env_value in {"1", "true", "yes", "on"}:
        return True
    return bool(config.get("auth", {}).get("use_gcloud_access_token", False))


def bq_client(config: dict):
    bigquery = require_bigquery()
    kwargs = {"project": config["project_id"], "location": config.get("location")}
    if use_gcloud_access_token(config):
        kwargs["credentials"] = gcloud_credentials(config)
    return bigquery.Client(**kwargs)


def storage_client(config: dict):
    storage = require_storage()
    kwargs = {"project": config["project_id"]}
    if use_gcloud_access_token(config):
        kwargs["credentials"] = gcloud_credentials(config)
    return storage.Client(**kwargs)


def dataset_id(config: dict) -> str:
    return f"{config['project_id']}.{config['dataset']}"


def table_id(config: dict, table_name: str) -> str:
    return f"{dataset_id(config)}.{table_name}"


def table_prefix(config: dict | None, layer: str) -> str:
    if config:
        return config.get("table_prefixes", {}).get(layer, f"{layer}_")
    return f"{layer}_"


def ods_table_name(target_table: str, config: dict | None = None) -> str:
    return f"{table_prefix(config, 'ods')}{target_table}"


def dwd_table_name(target_table: str, config: dict | None = None) -> str:
    return f"{table_prefix(config, 'dwd')}{target_table}"


def dws_table_name(target_table: str, config: dict | None = None) -> str:
    return f"{table_prefix(config, 'dws')}{target_table}"


def ads_table_name(target_table: str, config: dict | None = None) -> str:
    return f"{table_prefix(config, 'ads')}{target_table}"


def table_is_configured(config: dict, target_table: str) -> bool:
    return target_table in config.get("tables", {})


def allow_unconfigured_tables(config: dict) -> bool:
    return bool(config.get("defaults", {}).get("allow_unconfigured_tables", False))


def source_uri_prefix(config: dict, target_table: str) -> str:
    prefix = config["gcs"]["prefix"].strip("/")
    return f"gs://{config['gcs']['bucket']}/{prefix}/{target_table}/"


def parse_object(config: dict, blob, current_batch_id: str) -> LoadRecord:
    prefix = config["gcs"]["prefix"].strip("/") + "/"
    relative = blob.name.removeprefix(prefix)
    parts = [part for part in relative.split("/") if part]
    target_table = parts[0] if parts else "unmapped"
    partition_month = None
    status = "pending"
    error_message = None

    match = re.search(r"partition_month=(\d{6}|all)", relative)
    if match:
        partition_value = match.group(1)
        partition_month = int(partition_value) if partition_value != "all" else None
    else:
        status = "invalid"
        error_message = "Cannot parse partition_month from object path"

    suffix = Path(blob.name).suffix.lower()
    if suffix == ".csv":
        source_format = "CSV"
    elif suffix == ".parquet":
        source_format = "PARQUET"
    else:
        source_format = "UNKNOWN"
        status = "invalid"
        error_message = f"Unsupported source format: {suffix or '<none>'}"

    if target_table.startswith("fact_") and partition_month is None:
        status = "invalid"
        error_message = f"Fact table has non-numeric partition_month: {target_table}"

    if not table_is_configured(config, target_table) and not allow_unconfigured_tables(config):
        status = "invalid"
        error_message = f"Target table is not configured: {target_table}"

    load_mode = config.get("tables", {}).get(target_table, {}).get(
        "load_mode",
        config.get("defaults", {}).get("load_mode", "staging_only"),
    )

    return LoadRecord(
        batch_id=current_batch_id,
        gcs_uri=f"gs://{config['gcs']['bucket']}/{blob.name}",
        target_table=target_table,
        partition_month=partition_month,
        object_size=int(blob.size or 0),
        object_generation=str(blob.generation or ""),
        source_format=source_format,
        load_mode=load_mode,
        status=status,
        error_message=error_message,
    )


def iter_gcs_records(config: dict) -> Iterable[LoadRecord]:
    client = storage_client(config)
    bucket_name = config["gcs"]["bucket"]
    prefix = config["gcs"]["prefix"].strip("/") + "/"
    current_batch_id = batch_id()
    for blob in client.list_blobs(bucket_name, prefix=prefix):
        if blob.name.endswith("/"):
            continue
        yield parse_object(config, blob, current_batch_id)


def write_manifest(path: Path, records: Iterable[LoadRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def read_manifest(path: Path) -> list[LoadRecord]:
    if not path.exists():
        return []
    records: list[LoadRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                records.append(LoadRecord(**json.loads(line)))
    return records


def summarize(records: Iterable[LoadRecord]) -> None:
    rows = list(records)
    total_bytes = sum(r.object_size for r in rows)
    status_counts: dict[str, int] = {}
    table_counts: dict[str, int] = {}
    for record in rows:
        status_counts[record.status] = status_counts.get(record.status, 0) + 1
        table_counts[record.target_table] = table_counts.get(record.target_table, 0) + 1
    print(f"Records: {len(rows)}")
    print(f"Bytes: {total_bytes}")
    print(f"GiB: {total_bytes / 1024**3:.3f}")
    for status, count in sorted(status_counts.items()):
        print(f"status.{status}: {count}")
    for table, count in sorted(table_counts.items()):
        print(f"table.{table}: {count}")


def ensure_dataset(client, full_dataset_id: str, location: str) -> None:
    bigquery = require_bigquery()
    dataset = bigquery.Dataset(full_dataset_id)
    dataset.location = location
    client.create_dataset(dataset, exists_ok=True)
    print(f"Ensured dataset: {full_dataset_id}")


def ensure_table(client, table) -> None:
    created = client.create_table(table, exists_ok=True)
    full_table_id = getattr(created, "full_table_id", None) or getattr(table, "full_table_id", None)
    if full_table_id:
        full_table_id = full_table_id.replace(":", ".")
    else:
        full_table_id = str(table.reference)
    print(f"Ensured table: {full_table_id}")


def ods_manifest_schema() -> list:
    bigquery = require_bigquery()
    return [
        bigquery.SchemaField("batch_id", "STRING"),
        bigquery.SchemaField("synced_at", "TIMESTAMP"),
        bigquery.SchemaField("gcs_uri", "STRING"),
        bigquery.SchemaField("target_table", "STRING"),
        bigquery.SchemaField("destination_table", "STRING"),
        bigquery.SchemaField("partition_month", "INT64"),
        bigquery.SchemaField("object_size", "INT64"),
        bigquery.SchemaField("object_generation", "STRING"),
        bigquery.SchemaField("source_format", "STRING"),
        bigquery.SchemaField("status", "STRING"),
        bigquery.SchemaField("error_message", "STRING"),
    ]


def ods_errors_schema() -> list:
    bigquery = require_bigquery()
    return [
        bigquery.SchemaField("occurred_at", "TIMESTAMP"),
        bigquery.SchemaField("batch_id", "STRING"),
        bigquery.SchemaField("target_table", "STRING"),
        bigquery.SchemaField("destination_table", "STRING"),
        bigquery.SchemaField("error_type", "STRING"),
        bigquery.SchemaField("error_message", "STRING"),
    ]


def control_manifest_schema() -> list:
    bigquery = require_bigquery()
    return [
        bigquery.SchemaField("batch_id", "STRING"),
        bigquery.SchemaField("gcs_uri", "STRING"),
        bigquery.SchemaField("target_table", "STRING"),
        bigquery.SchemaField("partition_month", "INT64"),
        bigquery.SchemaField("object_size", "INT64"),
        bigquery.SchemaField("object_generation", "STRING"),
        bigquery.SchemaField("source_format", "STRING"),
        bigquery.SchemaField("load_mode", "STRING"),
        bigquery.SchemaField("status", "STRING"),
        bigquery.SchemaField("bq_job_id", "STRING"),
        bigquery.SchemaField("started_at", "TIMESTAMP"),
        bigquery.SchemaField("finished_at", "TIMESTAMP"),
        bigquery.SchemaField("error_message", "STRING"),
    ]


def control_errors_schema() -> list:
    bigquery = require_bigquery()
    return [
        bigquery.SchemaField("occurred_at", "TIMESTAMP"),
        bigquery.SchemaField("batch_id", "STRING"),
        bigquery.SchemaField("gcs_uri", "STRING"),
        bigquery.SchemaField("target_table", "STRING"),
        bigquery.SchemaField("error_type", "STRING"),
        bigquery.SchemaField("error_message", "STRING"),
    ]


def init(config: dict) -> None:
    bigquery = require_bigquery()
    client = bq_client(config)
    location = config["location"]
    ensure_dataset(client, dataset_id(config), location)

    manifest_table = bigquery.Table(table_id(config, "ods_gcs_load_manifest"), schema=control_manifest_schema())
    manifest_table.time_partitioning = bigquery.TimePartitioning(field="started_at")
    ensure_table(client, manifest_table)

    errors_table = bigquery.Table(table_id(config, "ods_gcs_load_errors"), schema=control_errors_schema())
    errors_table.time_partitioning = bigquery.TimePartitioning(field="occurred_at")
    ensure_table(client, errors_table)


def init_ods(config: dict) -> None:
    bigquery = require_bigquery()
    client = bq_client(config)
    location = config["location"]
    ensure_dataset(client, dataset_id(config), location)

    manifest_table = bigquery.Table(
        table_id(config, "ods_external_manifest"),
        schema=ods_manifest_schema(),
    )
    manifest_table.time_partitioning = bigquery.TimePartitioning(field="synced_at")
    ensure_table(client, manifest_table)

    errors_table = bigquery.Table(
        table_id(config, "ods_external_errors"),
        schema=ods_errors_schema(),
    )
    errors_table.time_partitioning = bigquery.TimePartitioning(field="occurred_at")
    ensure_table(client, errors_table)


def load_job_config(config: dict, record: LoadRecord, write_disposition: str | None = None):
    bigquery = require_bigquery()
    write_disposition = write_disposition or bigquery.WriteDisposition.WRITE_APPEND
    column_name_character_map = config.get("defaults", {}).get("column_name_character_map")
    if record.source_format == "CSV":
        csv_cfg = config.get("defaults", {}).get("csv", {})
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.CSV,
            skip_leading_rows=csv_cfg.get("skip_leading_rows", 1),
            autodetect=csv_cfg.get("autodetect", True),
            allow_quoted_newlines=csv_cfg.get("allow_quoted_newlines", True),
            encoding=csv_cfg.get("encoding", "UTF-8"),
            write_disposition=write_disposition,
            create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        )
        if column_name_character_map:
            apply_column_name_character_map(config, job_config)
        return job_config
    if record.source_format == "PARQUET":
        job_config = bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            write_disposition=write_disposition,
            create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        )
        hive = bigquery.HivePartitioningOptions()
        hive.mode = "AUTO"
        hive.source_uri_prefix = source_uri_prefix(config, record.target_table)
        job_config.hive_partitioning = hive
        if column_name_character_map:
            apply_column_name_character_map(config, job_config)
        return job_config
    raise ValueError(f"Unsupported source format: {record.source_format}")


def load_to_staging(config: dict, client, record: LoadRecord) -> LoadRecord:
    started_at = utc_now()
    destination = table_id(config, ods_table_name(record.target_table, config))
    job = client.load_table_from_uri(record.gcs_uri, destination, job_config=load_job_config(config, record))
    job.result()
    return LoadRecord(
        **{
            **asdict(record),
            "status": "loaded",
            "bq_job_id": job.job_id,
            "started_at": started_at,
            "finished_at": utc_now(),
            "error_message": None,
        }
    )


def chunked(values: Sequence[LoadRecord], size: int) -> Iterable[list[LoadRecord]]:
    for idx in range(0, len(values), size):
        yield list(values[idx:idx + size])


def load_table_batch(config: dict, client, target_table: str, records: list[LoadRecord]) -> list[LoadRecord]:
    bigquery = require_bigquery()
    destination = table_id(config, ods_table_name(target_table, config))
    max_source_uris = int(config.get("defaults", {}).get("max_source_uris_per_job", 9000))
    replace_staging = bool(config.get("defaults", {}).get("replace_staging_tables", False))
    loaded: list[LoadRecord] = []
    chunks = list(chunked(records, max_source_uris))
    for chunk_idx, chunk in enumerate(chunks, start=1):
        started_at = utc_now()
        write_disposition = (
            bigquery.WriteDisposition.WRITE_TRUNCATE
            if replace_staging and chunk_idx == 1
            else bigquery.WriteDisposition.WRITE_APPEND
        )
        uris = [record.gcs_uri for record in chunk]
        job = client.load_table_from_uri(
            uris,
            destination,
            job_config=load_job_config(config, chunk[0], write_disposition=write_disposition),
        )
        job.result()
        finished_at = utc_now()
        for record in chunk:
            loaded.append(
                LoadRecord(
                    **{
                        **asdict(record),
                        "status": "loaded",
                        "bq_job_id": job.job_id,
                        "started_at": started_at,
                        "finished_at": finished_at,
                        "error_message": None,
                    }
                )
            )
        print(
            f"loaded table={target_table} chunk={chunk_idx}/{len(chunks)} files={len(chunk)} job_id={job.job_id}",
            flush=True,
        )
    return loaded


def load(config: dict, dry_run: bool, retry_failed: bool = False) -> None:
    manifest_file = manifest_path(config)
    records = read_manifest(manifest_file)
    if not records:
        records = list(iter_gcs_records(config))
        write_manifest(manifest_file, records)

    loadable_statuses = set(LOADABLE_STATUSES)
    if retry_failed or config.get("defaults", {}).get("retry_failed", False):
        loadable_statuses.add("failed")
    pending = [
        r for r in records
        if r.status in loadable_statuses and not (config.get("defaults", {}).get("skip_loaded", True) and r.status in VALID_STATUSES_TO_SKIP)
    ]

    if dry_run:
        summarize(pending)
        for record in pending[:20]:
            print(f"{record.gcs_uri} -> {config.get('dataset', 'ashare')}.{ods_table_name(record.target_table, config)}")
        if len(pending) > 20:
            print(f"... {len(pending) - 20} more")
        return

    use_table_batch = config.get("defaults", {}).get("load_strategy", "table_batch") == "table_batch"
    client = bq_client(config)
    if use_table_batch:
        updated_by_key = {(r.gcs_uri, r.object_generation): r for r in records}
        pending_by_table: dict[str, list[LoadRecord]] = defaultdict(list)
        for record in pending:
            pending_by_table[record.target_table].append(record)
        for target_table, table_records in sorted(pending_by_table.items()):
            try:
                for loaded in load_table_batch(config, client, target_table, table_records):
                    updated_by_key[(loaded.gcs_uri, loaded.object_generation)] = loaded
                write_manifest(manifest_file, [updated_by_key[(r.gcs_uri, r.object_generation)] for r in records])
            except Exception as exc:
                failed_at = utc_now()
                for record in table_records:
                    updated_by_key[(record.gcs_uri, record.object_generation)] = LoadRecord(
                        **{
                            **asdict(record),
                            "status": "failed",
                            "started_at": record.started_at or failed_at,
                            "finished_at": failed_at,
                            "error_message": str(exc),
                        }
                    )
                write_manifest(manifest_file, [updated_by_key[(r.gcs_uri, r.object_generation)] for r in records])
                print(f"failed table={target_table}: {exc}", flush=True)
                raise
        return

    updated: list[LoadRecord] = []
    for idx, record in enumerate(records, start=1):
        if record.status not in loadable_statuses:
            updated.append(record)
            continue
        try:
            loaded = load_to_staging(config, client, record)
            updated.append(loaded)
            print(f"[{idx}/{len(records)}] loaded {record.gcs_uri}")
        except Exception as exc:
            failed = LoadRecord(
                **{
                    **asdict(record),
                    "status": "failed",
                    "started_at": record.started_at or utc_now(),
                    "finished_at": utc_now(),
                    "error_message": str(exc),
                }
            )
            updated.append(failed)
            write_manifest(manifest_file, updated + records[idx:])
            print(f"[{idx}/{len(records)}] failed {record.gcs_uri}: {exc}")
            raise

        if idx % 25 == 0:
            write_manifest(manifest_file, updated + records[idx:])
    write_manifest(manifest_file, updated)


def normalize_security_code(value: object) -> str | None:
    text = "" if value is None else str(value).strip().upper()
    if not text:
        return None
    text = text.replace("_", ".")
    if re.fullmatch(r"\d{6}", text):
        if text.startswith(("43", "83", "87", "88", "92")):
            return f"{text}.BJ"
        if text.startswith(("5", "6", "9")):
            return f"{text}.SH"
        return f"{text}.SZ"
    if re.fullmatch(r"(SH|SZ|BJ)\d{6}", text):
        return f"{text[2:]}.{text[:2]}"
    return text


def normalize_index_code(value: object) -> str | None:
    text = "" if value is None else str(value).strip().upper()
    if not text:
        return None
    text = text.replace("_", ".")
    prefix_value = re.fullmatch(r"(SH|SZ|BJ)(\d{6})", text)
    if prefix_value:
        text = f"{prefix_value.group(2)}.{prefix_value.group(1)}"
    dotted_value = re.fullmatch(r"(\d{6})\.(SH|SZ|BJ)", text)
    if dotted_value:
        code = dotted_value.group(1)
        if re.fullmatch(r"399\d{3}", code):
            return f"{code}.SZ"
        if re.fullmatch(r"(000|930|932|950)\d{3}", code):
            return f"{code}.SH"
        return f"{code}.{dotted_value.group(2)}"
    if re.fullmatch(r"399\d{3}", text):
        return f"{text}.SZ"
    if re.fullmatch(r"(000|930|932|950)\d{3}", text):
        return f"{text}.SH"
    if re.fullmatch(r"\d{6}", text):
        return f"{text}.SH"
    return text


def resolve_source_column(available_columns: list[str], candidates: list[str]) -> str | None:
    for candidate in candidates:
        if candidate in available_columns:
            return candidate
    return None


def get_code_column_config(config: dict, target_table: str) -> dict | None:
    return config.get("field_mappings", {}).get("per_table", {}).get(target_table)


def _add_rename(rename_map: dict[str, str], available: list[str], source: str | None, target: str) -> None:
    if not source or source == target:
        return
    if target in available:
        return
    if target in rename_map.values():
        return
    rename_map[source] = target


def apply_field_mappings(config: dict, target_table: str, df: "pd.DataFrame") -> "pd.DataFrame":
    common_mappings = config.get("field_mappings", {}).get("common", {})
    table_cfg = get_code_column_config(config, target_table)
    available = list(df.columns)
    rename_map: dict[str, str] = {}

    for source_col in available:
        target_col = common_mappings.get(source_col)
        if target_col:
            _add_rename(rename_map, available, source_col, target_col)

    if table_cfg:
        if "code_column" in table_cfg:
            target_code = table_cfg["code_column"]
            source = resolve_source_column(available, table_cfg.get("source_candidates", []))
            _add_rename(rename_map, available, source, target_code)
        elif "code_columns" in table_cfg:
            board_target, equity_target = table_cfg["code_columns"]
            board_source = resolve_source_column(available, table_cfg.get("board_source_candidates", []))
            equity_source = resolve_source_column(available, table_cfg.get("equity_source_candidates", []))
            _add_rename(rename_map, available, board_source, board_target)
            _add_rename(rename_map, available, equity_source, equity_target)

    if rename_map:
        df = df.rename(columns=rename_map)

    normalizable_code_columns = {"equity_code", "fund_code", "index_code", "security_code"}
    if table_cfg:
        configured_columns = [table_cfg["code_column"]] if "code_column" in table_cfg else table_cfg.get("code_columns", [])
        for code_column in configured_columns:
            if code_column in normalizable_code_columns and code_column in df.columns:
                normalizer = normalize_index_code if code_column == "index_code" else normalize_security_code
                df[code_column] = df[code_column].map(normalizer)

    return df


def validate_financial_date_policy(config: dict, df: "pd.DataFrame", target_table: str) -> "pd.DataFrame":
    policy = config.get("financial_date_policy", {})
    if not policy.get("report_period_is_not_visible_date", False):
        return df
    if "report_period_raw" in df.columns and "announcement_date_raw" not in df.columns:
        df["announcement_date_raw"] = None
    return df


def _source_format_value(bigquery, source_format_name: str):
    source_format = getattr(bigquery.SourceFormat, source_format_name, None)
    if source_format is None:
        available = [
            name for name in dir(bigquery.SourceFormat)
            if name.isupper() and not name.startswith("_")
        ]
        raise ValueError(
            f"Unsupported source_format '{source_format_name}'. Available: {available}"
        )
    return source_format


def apply_column_name_character_map(config: dict, api_object) -> None:
    value = config.get("defaults", {}).get("column_name_character_map")
    if not value:
        return
    if hasattr(api_object, "column_name_character_map"):
        api_object.column_name_character_map = value
        return
    if hasattr(api_object, "_properties"):
        api_object._properties["columnNameCharacterMap"] = value


def _apply_hive_partitioning(bigquery, external_config, table_cfg: dict) -> None:
    hive_cfg = table_cfg.get("hive_partitioning")
    if not hive_cfg:
        return

    hive_opts = bigquery.HivePartitioningOptions()
    hive_opts.mode = hive_cfg.get("mode", "AUTO")
    if "source_uri_prefix" in hive_cfg:
        hive_opts.source_uri_prefix = hive_cfg["source_uri_prefix"]
    if "require_hive_partition_filter" in table_cfg and hasattr(hive_opts, "require_partition_filter"):
        hive_opts.require_partition_filter = bool(table_cfg["require_hive_partition_filter"])
    external_config.hive_partitioning = hive_opts


def _manifest_discovery_config(config: dict) -> dict:
    discovery_config = copy.deepcopy(config)
    discovery_config.setdefault("defaults", {})["allow_unconfigured_tables"] = True
    return discovery_config


def _load_or_discover_manifest_records(config: dict) -> list[LoadRecord]:
    records = read_manifest(manifest_path(config))
    if records:
        return records
    return list(iter_gcs_records(_manifest_discovery_config(config)))


def _generated_ods_external_table_config(config: dict, table_key: str, source_uris: list[str]) -> dict:
    gcs_prefix = f"gs://{config['gcs']['bucket']}/{config['gcs']['prefix'].strip('/')}/{table_key}/"
    table_cfg: dict = {
        "destination_table": ods_table_name(table_key, config),
        "source_uris": source_uris,
        "source_format": "PARQUET",
        "require_hive_partition_filter": False,
    }
    if table_key.startswith("fact_"):
        table_cfg["hive_partitioning"] = {
            "mode": "AUTO",
            "source_uri_prefix": gcs_prefix,
        }
    return table_cfg


def resolve_ods_external_tables(config: dict) -> dict:
    configured = copy.deepcopy(config.get("ods_external_tables", {}))
    ods_defaults = config.get("defaults", {}).get("ods", {})
    if not ods_defaults.get("auto_discover_tables", False):
        return configured

    records = _load_or_discover_manifest_records(config)
    by_table: dict[str, set[str]] = defaultdict(set)
    for record in records:
        if record.source_format != "PARQUET":
            continue
        if record.target_table in {"", "unmapped"}:
            continue
        by_table[record.target_table].add(record.gcs_uri)

    resolved: dict[str, dict] = {}
    for table_key, uris in sorted(by_table.items()):
        generated = _generated_ods_external_table_config(config, table_key, sorted(uris))
        merged = {**generated, **configured.get(table_key, {})}
        # Exact URIs from the manifest avoid wildcard compatibility ambiguity and
        # keep each external table bound to the current GCS snapshot.
        merged["source_uris"] = sorted(uris)
        resolved[table_key] = merged

    for table_key, table_cfg in configured.items():
        if table_key not in resolved:
            resolved[table_key] = table_cfg
    return resolved


def ensure_ods_external_table(config: dict, client, table_key: str, table_cfg: dict) -> None:
    bigquery = require_bigquery()
    full_dataset_id = dataset_id(config)
    destination = table_cfg.get("destination_table") or ods_table_name(table_key, config)
    full_table_id = f"{full_dataset_id}.{destination}"
    source_format_name = table_cfg.get("source_format", "PARQUET")

    external_config = bigquery.ExternalConfig(_source_format_value(bigquery, source_format_name))
    external_config.source_uris = table_cfg["source_uris"]
    apply_column_name_character_map(config, external_config)
    _apply_hive_partitioning(bigquery, external_config, table_cfg)

    if "require_hive_partition_filter" in table_cfg and hasattr(external_config, "require_hive_partition_filter"):
        external_config.require_hive_partition_filter = bool(table_cfg["require_hive_partition_filter"])

    table = bigquery.Table(full_table_id)
    table.external_data_configuration = external_config

    def apply_safe_schema(target_table) -> None:
        # Some source Parquet files contain columns like "3日涨幅%" that BigQuery
        # maps to names starting with a digit. External tables cannot expose those
        # names directly, so fall back to position-based raw_col_* names. DWD keeps
        # the raw payload JSON and standardizes known P0 tables separately.
        target_table.schema = [
            bigquery.SchemaField(f"raw_col_{idx:03d}", "STRING")
            for idx in range(1, 301)
        ]

    try:
        existing = client.get_table(full_table_id)
    except Exception as exc:
        if exc.__class__.__name__ != "NotFound":
            raise
        try:
            client.create_table(table, exists_ok=False)
        except Exception as create_exc:
            if "Invalid field name" not in str(create_exc):
                raise
            apply_safe_schema(table)
            client.create_table(table, exists_ok=False)
            print(
                f"Created external table with safe raw schema: {full_table_id} "
                f"({len(external_config.source_uris)} URI(s))"
            )
            return
        print(f"Created external table: {full_table_id} ({len(external_config.source_uris)} URI(s))")
        return

    existing.external_data_configuration = external_config
    try:
        client.update_table(existing, ["external_data_configuration"])
    except Exception as update_exc:
        if "Invalid field name" not in str(update_exc):
            raise
        apply_safe_schema(existing)
        client.update_table(existing, ["schema", "external_data_configuration"])
        print(
            f"Updated external table with safe raw schema: {full_table_id} "
            f"({len(external_config.source_uris)} URI(s))"
        )
        return
    print(f"Updated external table: {full_table_id} ({len(external_config.source_uris)} URI(s))")


def create_ods_external(config: dict) -> None:
    client = bq_client(config)
    ods_tables = resolve_ods_external_tables(config)
    if not ods_tables:
        raise RuntimeError("No ods_external_tables configured.")

    for table_key, table_cfg in sorted(ods_tables.items()):
        ensure_ods_external_table(config, client, table_key, table_cfg)

    print(f"ODS external tables ensured: {len(ods_tables)}")


def audit_ods_external(config: dict) -> None:
    client = bq_client(config)
    full_dataset_id = dataset_id(config)
    ods_tables = resolve_ods_external_tables(config)
    if not ods_tables:
        raise RuntimeError("No ods_external_tables configured.")

    gcs_prefix = f"gs://{config['gcs']['bucket']}/{config['gcs']['prefix'].strip('/')}/"
    missing: list[str] = []
    no_external_config: list[str] = []
    wrong_format: list[str] = []
    wrong_uri_prefix: list[str] = []
    sample_failures: list[str] = []

    for table_key, table_cfg in sorted(ods_tables.items()):
        destination = table_cfg.get("destination_table") or ods_table_name(table_key, config)
        full_table_id = f"{full_dataset_id}.{destination}"
        expected_format = table_cfg.get("source_format", "PARQUET")

        try:
            table = client.get_table(full_table_id)
        except Exception as exc:
            missing.append(f"{full_table_id}: {exc}")
            continue

        ext = table.external_data_configuration
        if ext is None:
            no_external_config.append(full_table_id)
            continue

        if ext.source_format != expected_format:
            wrong_format.append(
                f"{full_table_id}: expected={expected_format} actual={ext.source_format}"
            )

        for uri in ext.source_uris:
            if not uri.startswith(gcs_prefix):
                wrong_uri_prefix.append(f"{full_table_id}: {uri}")

        try:
            rows = list(client.query(f"SELECT * FROM `{full_table_id}` LIMIT 1").result())
            if not rows:
                sample_failures.append(f"{full_table_id}: sample query returned 0 rows")
            else:
                print(
                    f"{full_table_id}: OK "
                    f"(fields={len(table.schema)}, source_format={ext.source_format})"
                )
        except Exception as exc:
            sample_failures.append(f"{full_table_id}: {exc}")

    errors: list[str] = []
    if missing:
        errors.append(f"missing external tables ({len(missing)}):\n" + "\n".join(missing[:10]))
    if no_external_config:
        errors.append(
            f"tables without externalDataConfiguration ({len(no_external_config)}):\n"
            + "\n".join(no_external_config)
        )
    if wrong_format:
        errors.append(f"wrong source_format ({len(wrong_format)}):\n" + "\n".join(wrong_format[:5]))
    if wrong_uri_prefix:
        errors.append(f"GCS URI mismatch ({len(wrong_uri_prefix)}):\n" + "\n".join(wrong_uri_prefix[:5]))
    if sample_failures:
        errors.append(f"sample query failures ({len(sample_failures)}):\n" + "\n".join(sample_failures[:5]))

    if errors:
        raise RuntimeError("ODS external table audit failed:\n" + "\n".join(errors))

    print(f"ODS external table audit passed: {len(ods_tables)} tables OK")


def field_names(schema: list) -> list[str]:
    return [field.name for field in schema]


def require_table(client, full_table_id: str):
    try:
        from google.api_core.exceptions import NotFound
    except ImportError as exc:
        raise RuntimeError(
            f"Missing dependency: google-api-core. {REQUIREMENTS_INSTALL_HINT}"
        ) from exc
    try:
        return client.get_table(full_table_id)
    except NotFound as exc:
        raise RuntimeError(f"BigQuery table not found: {full_table_id}") from exc


def merge_table(config: dict, target_table: str) -> None:
    table_cfg = config.get("tables", {}).get(target_table)
    if not table_cfg:
        raise ValueError(f"Table is not configured: {target_table}")
    keys = table_cfg.get("primary_key") or []
    if not keys:
        raise ValueError(f"Table has no primary_key configured: {target_table}")

    client = bq_client(config)
    staging_id = table_id(config, ods_table_name(target_table, config))
    core_id = table_id(config, dwd_table_name(target_table, config))
    staging = require_table(client, staging_id)
    core = require_table(client, core_id)
    staging_fields = set(field_names(staging.schema))
    core_fields = field_names(core.schema)

    missing_keys = [key for key in keys if key not in staging_fields or key not in core_fields]
    if missing_keys:
        raise RuntimeError(f"Primary key fields missing from staging/core schema: {missing_keys}")

    merge_fields = [field for field in core_fields if field in staging_fields]
    if not merge_fields:
        raise RuntimeError(f"No shared fields between {staging_id} and {core_id}")

    update_fields = [field for field in merge_fields if field not in keys]
    on_clause = " AND ".join(f"T.`{key}` = S.`{key}`" for key in keys)
    update_clause = ", ".join(f"`{field}` = S.`{field}`" for field in update_fields)
    insert_columns = ", ".join(f"`{field}`" for field in merge_fields)
    insert_values = ", ".join(f"S.`{field}`" for field in merge_fields)

    sql = f"""
MERGE `{core_id}` T
USING `{staging_id}` S
ON {on_clause}
WHEN MATCHED THEN
  UPDATE SET {update_clause}
WHEN NOT MATCHED THEN
  INSERT ({insert_columns})
  VALUES ({insert_values})
"""
    job = client.query(sql)
    job.result()
    print(f"Merged {staging_id} into {core_id}; job_id={job.job_id}")


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def quote_table(full_table_id: str) -> str:
    return "`" + full_table_id.replace("`", "``") + "`"


def table_columns(table) -> list[str]:
    return [field.name for field in table.schema]


def source_column_expr(columns: set[str], candidates: Sequence[str]) -> str | None:
    for candidate in candidates:
        if candidate in columns:
            return f"t.{quote_ident(candidate)}"
    return None


def nullable_string_sql(expr: str | None) -> str:
    if expr is None:
        return "CAST(NULL AS STRING)"
    return f"NULLIF(TRIM(CAST({expr} AS STRING)), '')"


def numeric_sql(expr: str | None) -> str:
    if expr is None:
        return "CAST(NULL AS NUMERIC)"
    return f"SAFE_CAST(NULLIF(TRIM(CAST({expr} AS STRING)), '') AS NUMERIC)"


def date_sql(expr: str | None) -> str:
    if expr is None:
        return "CAST(NULL AS DATE)"
    text = nullable_string_sql(expr)
    return (
        f"COALESCE(SAFE_CAST({expr} AS DATE), "
        f"SAFE.PARSE_DATE('%Y-%m-%d', {text}), "
        f"SAFE.PARSE_DATE('%Y%m%d', {text}))"
    )


def normalize_code_sql(expr: str | None) -> str:
    if expr is None:
        return "CAST(NULL AS STRING)"
    text = f"UPPER(REPLACE(TRIM(CAST({expr} AS STRING)), '_', '.'))"
    return f"""CASE
    WHEN {expr} IS NULL OR TRIM(CAST({expr} AS STRING)) = '' THEN NULL
    WHEN REGEXP_CONTAINS({text}, r'^\\d{{6}}$') THEN
      CASE
        WHEN STARTS_WITH({text}, '43') OR STARTS_WITH({text}, '83')
          OR STARTS_WITH({text}, '87') OR STARTS_WITH({text}, '88')
          OR STARTS_WITH({text}, '92') THEN CONCAT({text}, '.BJ')
        WHEN STARTS_WITH({text}, '5') OR STARTS_WITH({text}, '6')
          OR STARTS_WITH({text}, '9') THEN CONCAT({text}, '.SH')
        ELSE CONCAT({text}, '.SZ')
      END
    WHEN REGEXP_CONTAINS({text}, r'^(SH|SZ|BJ)\\d{{6}}$') THEN CONCAT(SUBSTR({text}, 3), '.', SUBSTR({text}, 1, 2))
    ELSE {text}
  END"""


def normalize_index_code_sql(expr: str | None) -> str:
    if expr is None:
        return "CAST(NULL AS STRING)"
    text = f"UPPER(REPLACE(TRIM(CAST({expr} AS STRING)), '_', '.'))"
    return f"""CASE
    WHEN {expr} IS NULL OR TRIM(CAST({expr} AS STRING)) = '' THEN NULL
    WHEN REGEXP_CONTAINS({text}, r'^399\\d{{3}}\\.(SH|SZ|BJ)$') THEN CONCAT(SUBSTR({text}, 1, 6), '.SZ')
    WHEN REGEXP_CONTAINS({text}, r'^(000|930|932|950)\\d{{3}}\\.(SH|SZ|BJ)$') THEN CONCAT(SUBSTR({text}, 1, 6), '.SH')
    WHEN REGEXP_CONTAINS({text}, r'^399\\d{{3}}$') THEN CONCAT({text}, '.SZ')
    WHEN REGEXP_CONTAINS({text}, r'^(000|930|932|950)\\d{{3}}$') THEN CONCAT({text}, '.SH')
    WHEN REGEXP_CONTAINS({text}, r'^\\d{{6}}$') THEN
      CASE
        WHEN STARTS_WITH({text}, '39') THEN CONCAT({text}, '.SZ')
        ELSE CONCAT({text}, '.SH')
      END
    WHEN REGEXP_CONTAINS({text}, r'^(SH|SZ|BJ)\\d{{6}}$') THEN CONCAT(SUBSTR({text}, 3), '.', SUBSTR({text}, 1, 2))
    ELSE {text}
  END"""


def partition_month_sql(columns: set[str], parsed_date_expr: str | None = None) -> str:
    if "partition_month" in columns:
        return "SAFE_CAST(t.`partition_month` AS INT64)"
    if parsed_date_expr:
        return f"SAFE_CAST(FORMAT_DATE('%Y%m', {parsed_date_expr}) AS INT64)"
    return "CAST(NULL AS INT64)"


def month_range_boundaries(start_year: int = 1990, end_year: int = 2100) -> str:
    return f"GENERATE_ARRAY({start_year}01, {end_year}01, 100)"


def source_payload_sql() -> str:
    return "TO_JSON_STRING(t)"


def source_hash_sql() -> str:
    return f"TO_HEX(SHA256({source_payload_sql()}))"


def lineage_select_items(columns: set[str]) -> list[str]:
    return [
        f"{nullable_string_sql(source_column_expr(columns, ['source_file']))} AS source_file",
        f"{nullable_string_sql(source_column_expr(columns, ['source_entry']))} AS source_entry",
        f"{nullable_string_sql(source_column_expr(columns, ['target_table']))} AS target_table",
        f"{source_hash_sql()} AS source_hash",
        "CURRENT_TIMESTAMP() AS ingested_at",
        f"{source_payload_sql()} AS source_payload_json",
    ]


def create_table_prefix(
    table_id_to_write: str,
    partition: bool,
    cluster_by: Sequence[str],
    partition_field: str | None = None,
) -> str:
    sql = f"CREATE OR REPLACE TABLE {quote_table(table_id_to_write)}"
    if partition:
        if partition_field == "date":
            sql += "\nPARTITION BY date"
        elif partition_field == "partition_month":
            sql += f"\nPARTITION BY RANGE_BUCKET(partition_month, {month_range_boundaries()})"
    if cluster_by:
        sql += "\nCLUSTER BY " + ", ".join(cluster_by)
    return sql + "\nAS"


def build_kline_dwd_sql(config: dict, table_key: str, source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    ordered_columns = list(columns)

    def by_ordinal(idx: int) -> str | None:
        if idx >= len(ordered_columns):
            return None
        return f"t.{quote_ident(ordered_columns[idx])}"

    if "equity" in table_key:
        code_column = "equity_code"
        code_candidates = ["equity_code", "security_code", "股票代码", "证券代码", "代码"]
        cluster = ["equity_code"]
        ordinal_map = {"open": 2, "close": 3, "high": 4, "low": 5, "volume": 6, "amount": 7}
    elif "fund" in table_key:
        code_column = "fund_code"
        code_candidates = ["fund_code", "security_code", "基金代码", "基金交易代码", "代码"]
        cluster = ["fund_code"]
        ordinal_map = {"open": 2, "close": 3, "high": 4, "low": 5, "volume": 6, "amount": 7}
    elif "index" in table_key:
        code_column = "index_code"
        code_candidates = ["index_code", "security_code", "指数代码", "代码"]
        cluster = ["index_code"]
        ordinal_map = {"open": 3, "close": 4, "high": 5, "low": 6, "volume": 7, "amount": 8}
    else:
        code_column = "board_code"
        code_candidates = ["board_code", "security_code", "板块代码", "指数代码", "代码"]
        cluster = ["board_code"]
        ordinal_map = {"open": 3, "close": 4, "high": 5, "low": 6, "volume": 7, "amount": 8}

    raw_code = source_column_expr(columns, code_candidates)
    parsed_date = date_sql(source_column_expr(columns, ["date", "日期", "交易日期"]))
    period = table_key.rsplit("_", 1)[-1]
    has_adjust = code_column in {"equity_code", "fund_code"}
    adjust_items = ["'qfq' AS adjust_type"] if has_adjust else []
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{(normalize_index_code_sql if code_column == 'index_code' else normalize_code_sql)(raw_code)} AS {code_column}",
        *adjust_items,
        f"'{period}' AS period",
        f"{numeric_sql(source_column_expr(columns, ['open', 'open_raw', '开盘']) or by_ordinal(ordinal_map['open']))} AS open",
        f"{numeric_sql(source_column_expr(columns, ['high', 'high_raw', '最高']) or by_ordinal(ordinal_map['high']))} AS high",
        f"{numeric_sql(source_column_expr(columns, ['low', 'low_raw', '最低']) or by_ordinal(ordinal_map['low']))} AS low",
        f"{numeric_sql(source_column_expr(columns, ['close', 'close_raw', '收盘']) or by_ordinal(ordinal_map['close']))} AS close",
        f"{numeric_sql(source_column_expr(columns, ['volume', 'volume_raw', '成交量']) or by_ordinal(ordinal_map['volume']))} AS volume",
        f"{numeric_sql(source_column_expr(columns, ['amount', 'amount_raw', '成交额']) or by_ordinal(ordinal_map['amount']))} AS amount",
        *lineage_select_items(columns),
    ]
    key_columns = [code_column, "date"] + (["adjust_type"] if has_adjust else [])
    partition_by = ", ".join(key_columns)
    order_by = "source_file DESC, source_entry DESC, source_hash DESC"
    prefix = create_table_prefix(
        destination_id,
        partition=True,
        partition_field="partition_month",
        cluster_by=cluster + (["adjust_type"] if has_adjust else []),
    )
    where_not_null = f"date IS NOT NULL AND {code_column} IS NOT NULL"
    return f"""{prefix}
WITH normalized AS (
  SELECT
    {",\n    ".join(select_items)}
  FROM {quote_table(source_id)} AS t
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY {partition_by} ORDER BY {order_by}) AS rn
  FROM normalized
  WHERE {where_not_null}
)
SELECT * EXCEPT(rn)
FROM ranked
WHERE rn = 1
"""


def build_board_component_dwd_sql(config: dict, source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    ordered_columns = list(columns)

    def by_ordinal(idx: int) -> str | None:
        if idx >= len(ordered_columns):
            return None
        return f"t.{quote_ident(ordered_columns[idx])}"

    board_expr = source_column_expr(columns, ["board_code", "板块代码", "指数代码", "security_code"]) or by_ordinal(6)
    equity_expr = source_column_expr(columns, ["equity_code", "成分股票代码", "股票代码"]) or by_ordinal(7)
    parsed_date = date_sql(source_column_expr(columns, ["date", "日期", "交易日期"]))
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{normalize_code_sql(board_expr)} AS board_code",
        f"{normalize_code_sql(equity_expr)} AS equity_code",
        f"{normalize_code_sql(equity_expr)} AS security_code",
        f"{nullable_string_sql(source_column_expr(columns, ['指数名称', '板块名称']))} AS board_name",
        f"{nullable_string_sql(source_column_expr(columns, ['成分股票名称', '股票名称']) or by_ordinal(8))} AS equity_name",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(
        destination_id,
        partition=True,
        partition_field="partition_month",
        cluster_by=["board_code", "equity_code"],
    )
    return f"""{prefix}
WITH normalized AS (
  SELECT
    {",\n    ".join(select_items)}
  FROM {quote_table(source_id)} AS t
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY date, board_code, equity_code
      ORDER BY source_file DESC, source_entry DESC, source_hash DESC
    ) AS rn
  FROM normalized
  WHERE date IS NOT NULL AND board_code IS NOT NULL AND equity_code IS NOT NULL
)
SELECT * EXCEPT(rn)
FROM ranked
WHERE rn = 1
"""


def build_dim_security_dwd_sql(config: dict, source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    ordered_columns = list(columns)

    def by_ordinal(idx: int) -> str | None:
        if idx >= len(ordered_columns):
            return None
        return f"t.{quote_ident(ordered_columns[idx])}"

    code_expr = source_column_expr(columns, ["security_code", "TS代码", "股票代码", "证券代码", "代码"])
    list_date = date_sql(source_column_expr(columns, ["上市日期", "list_date", "date"]) or by_ordinal(12))
    delist_date = date_sql(source_column_expr(columns, ["退市日期", "delist_date"]) or by_ordinal(13))
    status_expr = nullable_string_sql(source_column_expr(columns, ["上市状态", "status"]) or by_ordinal(11))
    select_items = [
        f"{normalize_code_sql(code_expr)} AS security_code",
        f"{nullable_string_sql(source_column_expr(columns, ['股票名称', '证券名称', '名称', 'security_name']) or by_ordinal(2))} AS security_name",
        "'stock' AS security_type",
        f"{nullable_string_sql(source_column_expr(columns, ['所属行业', 'industry']) or by_ordinal(4))} AS industry",
        f"{nullable_string_sql(source_column_expr(columns, ['市场类型', 'market_type']) or by_ordinal(8))} AS market_type",
        f"{nullable_string_sql(source_column_expr(columns, ['交易所代码', 'exchange_code']) or by_ordinal(9))} AS exchange_code",
        f"{list_date} AS list_date",
        f"{delist_date} AS delist_date",
        f"({status_expr} IS NULL OR {status_expr} != '退市') AS is_active",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=False, cluster_by=["security_code", "security_type"])
    return f"""{prefix}
WITH normalized AS (
  SELECT
    {",\n    ".join(select_items)}
  FROM {quote_table(source_id)} AS t
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY security_code
      ORDER BY is_active DESC, list_date DESC, source_file DESC, source_hash DESC
    ) AS rn
  FROM normalized
  WHERE security_code IS NOT NULL
)
SELECT * EXCEPT(rn)
FROM ranked
WHERE rn = 1
"""


def build_adjust_factor_dwd_sql(config: dict, source_id: str, destination_id: str, columns: set[str]) -> str:
    parsed_date = date_sql(source_column_expr(columns, ["date", "日期", "交易日期"]))
    code_expr = source_column_expr(columns, ["equity_code", "security_code", "股票代码", "证券代码", "代码"])
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{normalize_code_sql(code_expr)} AS equity_code",
        f"{numeric_sql(source_column_expr(columns, ['adjust_factor', '复权因子']))} AS adjust_factor",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(
        destination_id,
        partition=True,
        partition_field="partition_month",
        cluster_by=["equity_code"],
    )
    return f"""{prefix}
WITH normalized AS (
  SELECT
    {",\n    ".join(select_items)}
  FROM {quote_table(source_id)} AS t
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY equity_code, date
      ORDER BY source_file DESC, source_entry DESC, source_hash DESC
    ) AS rn
  FROM normalized
  WHERE equity_code IS NOT NULL AND date IS NOT NULL
)
SELECT * EXCEPT(rn)
FROM ranked
WHERE rn = 1
"""


def generic_code_candidates(table_key: str) -> tuple[str | None, list[str]]:
    if "board" in table_key:
        return "board_code", ["board_code", "板块代码", "指数代码", "security_code"]
    if "fund" in table_key:
        return "fund_code", ["fund_code", "基金代码", "基金交易代码", "security_code", "代码"]
    if "index" in table_key:
        return "index_code", ["index_code", "指数代码", "security_code", "代码"]
    if table_key.startswith("fact_"):
        return "equity_code", ["equity_code", "security_code", "股票代码", "证券代码", "代码"]
    if table_key == "dim_index":
        return "index_code", ["index_code", "指数代码", "security_code", "代码"]
    if table_key == "dim_board":
        return "board_code", ["board_code", "板块代码", "security_code", "代码"]
    return None, []


def build_generic_dwd_sql(config: dict, table_key: str, source_id: str, destination_id: str, columns: set[str]) -> str:
    date_expr = source_column_expr(columns, ["date", "日期", "交易日期", "公告日期", "实际公告日期"])
    parsed_date = date_sql(date_expr)
    code_column, code_candidates = generic_code_candidates(table_key)
    select_items = []
    if date_expr is not None:
        select_items.append(f"{parsed_date} AS date")
        select_items.append(f"{partition_month_sql(columns, parsed_date)} AS partition_month")
    elif "partition_month" in columns:
        select_items.append(f"{partition_month_sql(columns)} AS partition_month")
    if code_column:
        normalizer = normalize_index_code_sql if code_column == "index_code" else normalize_code_sql
        select_items.append(f"{normalizer(source_column_expr(columns, code_candidates))} AS {code_column}")
    if source_column_expr(columns, ["报告期", "report_period", "report_period_raw"]):
        select_items.append(
            f"{nullable_string_sql(source_column_expr(columns, ['报告期', 'report_period', 'report_period_raw']))} AS report_period"
        )
    if source_column_expr(columns, ["公告日期", "实际公告日期", "announcement_date_raw", "actual_announcement_date_raw"]):
        select_items.append(
            f"{date_sql(source_column_expr(columns, ['实际公告日期', '公告日期', 'actual_announcement_date_raw', 'announcement_date_raw']))} AS announcement_date"
        )
    select_items.extend(lineage_select_items(columns))
    cluster = [code_column] if code_column else []
    prefix = create_table_prefix(destination_id, partition=False, cluster_by=cluster)
    return f"""{prefix}
SELECT
  {",\n  ".join(select_items)}
FROM {quote_table(source_id)} AS t
"""


def build_dwd_transform_sql(config: dict, table_key: str, source_id: str, destination_id: str, columns: set[str]) -> str:
    if re.fullmatch(r"fact_(equity|fund|index)_kline_(1d|1w|1mo)", table_key):
        return build_kline_dwd_sql(config, table_key, source_id, destination_id, columns)
    if table_key == "fact_board_component_1d":
        return build_board_component_dwd_sql(config, source_id, destination_id, columns)
    if table_key == "dim_security":
        return build_dim_security_dwd_sql(config, source_id, destination_id, columns)
    if table_key == "fact_adjust_factor":
        return build_adjust_factor_dwd_sql(config, source_id, destination_id, columns)
    return build_generic_dwd_sql(config, table_key, source_id, destination_id, columns)


def resolve_dwd_source_tables(config: dict, client) -> dict[str, str]:
    full_dataset_id = dataset_id(config)
    result: dict[str, str] = {}
    excluded_ods_tables = {"ods_external_manifest", "ods_external_errors", "ods_gcs_load_manifest", "ods_gcs_load_errors"}
    for table in client.list_tables(full_dataset_id):
        table_name = table.table_id
        prefix = table_prefix(config, "ods")
        if table_name in excluded_ods_tables:
            continue
        if table_name.startswith(prefix):
            result[table_name.removeprefix(prefix)] = table_name
    return dict(sorted(result.items()))


def transform_dwd(config: dict, mode: str = "full", target_table: str | None = None, sample_limit: int | None = None) -> None:
    if mode not in {"full", "sample"}:
        raise ValueError("mode must be 'full' or 'sample'")

    client = bq_client(config)
    source_tables = resolve_dwd_source_tables(config, client)
    if target_table:
        source_tables = {target_table: source_tables[target_table]} if target_table in source_tables else {}
    if not source_tables:
        raise RuntimeError("No ODS source tables found for DWD transform.")

    for table_key, ods_name in source_tables.items():
        source_id = table_id(config, ods_name)
        destination_name = dwd_table_name(table_key, config)
        destination_id = table_id(config, destination_name)
        source_table = client.get_table(source_id)
        columns = table_columns(source_table)
        sql = build_dwd_transform_sql(config, table_key, source_id, destination_id, columns)
        if mode == "sample":
            # The sample mode validates SQL generation without marking production
            # completion. It writes to a temporary sample table to avoid clobbering
            # full DWD output.
            sample_destination = table_id(config, f"_sample_{destination_name}")
            sql = sql.replace(quote_table(destination_id), quote_table(sample_destination), 1)
        job = client.query(sql)
        job.result()
        written = sample_destination if mode == "sample" else destination_id
        print(f"Transformed {source_id} -> {written}; job_id={job.job_id}")


def dwd_coverage_report_schema() -> list:
    bigquery = require_bigquery()
    return [
        bigquery.SchemaField("audited_at", "TIMESTAMP"),
        bigquery.SchemaField("source_table", "STRING"),
        bigquery.SchemaField("dwd_table", "STRING"),
        bigquery.SchemaField("row_count", "INT64"),
        bigquery.SchemaField("status", "STRING"),
        bigquery.SchemaField("message", "STRING"),
    ]


def write_dwd_coverage_report(config: dict, rows: list[dict]) -> None:
    bigquery = require_bigquery()
    client = bq_client(config)
    destination = table_id(config, "dwd_coverage_report")
    table = bigquery.Table(destination, schema=dwd_coverage_report_schema())
    table.time_partitioning = bigquery.TimePartitioning(field="audited_at")
    client.create_table(table, exists_ok=True)
    if rows:
        errors = client.insert_rows_json(destination, rows)
        if errors:
            raise RuntimeError(f"Failed to write dwd_coverage_report: {errors}")


def audit_dwd(config: dict, scope: str = "full", target_table: str | None = None) -> None:
    client = bq_client(config)
    source_tables = resolve_dwd_source_tables(config, client)
    if target_table:
        source_tables = {target_table: source_tables[target_table]} if target_table in source_tables else {}
    if not source_tables:
        raise RuntimeError("No ODS source tables found for DWD audit.")

    missing: list[str] = []
    zero_rows: list[str] = []
    schema_errors: list[str] = []
    report_rows: list[dict] = []
    audited_at = utc_now()

    required_fields = {
        "dwd_fact_equity_kline_1d": {"equity_code", "date", "partition_month", "adjust_type", "open", "high", "low", "close", "volume", "amount"},
        "dwd_fact_fund_kline_1d": {"fund_code", "date", "partition_month", "adjust_type", "open", "high", "low", "close", "volume", "amount"},
        "dwd_fact_index_kline_1d": {"index_code", "date", "partition_month", "open", "high", "low", "close", "volume", "amount"},
        "dwd_fact_board_component_1d": {"board_code", "equity_code", "date", "partition_month"},
        "dwd_dim_security": {"security_code", "security_name", "security_type", "list_date", "is_active"},
    }

    for table_key in sorted(source_tables):
        dwd_name = dwd_table_name(table_key, config)
        dwd_id = table_id(config, dwd_name)
        try:
            table = client.get_table(dwd_id)
        except Exception as exc:
            missing.append(f"{dwd_id}: {exc}")
            report_rows.append({
                "audited_at": audited_at,
                "source_table": ods_table_name(table_key, config),
                "dwd_table": dwd_name,
                "row_count": None,
                "status": "missing",
                "message": str(exc),
            })
            continue

        row_count = int(table.num_rows or 0)
        fields = set(table_columns(table))
        missing_fields = required_fields.get(dwd_name, set()) - fields
        if missing_fields:
            schema_errors.append(f"{dwd_id}: missing fields {sorted(missing_fields)}")
        if row_count == 0:
            zero_rows.append(dwd_id)
        status = "pass" if row_count > 0 and not missing_fields else "failed"
        report_rows.append({
            "audited_at": audited_at,
            "source_table": ods_table_name(table_key, config),
            "dwd_table": dwd_name,
            "row_count": row_count,
            "status": status,
            "message": "" if status == "pass" else "zero rows or schema mismatch",
        })
        print(f"{dwd_id}: rows={row_count} fields={len(fields)} status={status}")

    write_dwd_coverage_report(config, report_rows)

    errors: list[str] = []
    if missing:
        errors.append(f"missing DWD tables ({len(missing)}):\n" + "\n".join(missing[:20]))
    if zero_rows:
        errors.append(f"zero-row DWD tables ({len(zero_rows)}):\n" + "\n".join(zero_rows[:20]))
    if schema_errors:
        errors.append(f"DWD schema errors ({len(schema_errors)}):\n" + "\n".join(schema_errors[:20]))
    if errors:
        raise RuntimeError("DWD audit failed:\n" + "\n".join(errors))

    print(f"DWD audit passed: {len(source_tables)} source tables covered")


def smoke_query(config: dict) -> None:
    client = bq_client(config)
    checks = [
        ("dwd_dim_security", "SELECT security_code FROM `{table}` WHERE security_type = 'stock' AND is_active = TRUE LIMIT 1"),
        ("dwd_fact_equity_kline_1d", "SELECT equity_code, date, close FROM `{table}` WHERE partition_month IS NOT NULL LIMIT 1"),
        ("dwd_fact_fund_kline_1d", "SELECT fund_code, date, close FROM `{table}` WHERE partition_month IS NOT NULL LIMIT 1"),
        ("dwd_fact_index_kline_1d", "SELECT index_code, date, close FROM `{table}` WHERE partition_month IS NOT NULL LIMIT 1"),
    ]
    failures: list[str] = []
    for table_name, template in checks:
        full_id = table_id(config, table_name)
        sql = template.format(table=full_id)
        try:
            rows = list(client.query(sql).result())
        except Exception as exc:
            failures.append(f"{full_id}: {exc}")
            continue
        if not rows:
            failures.append(f"{full_id}: returned 0 rows")
        else:
            print(f"{full_id}: smoke OK")
    if failures:
        raise RuntimeError("smoke-query failed:\n" + "\n".join(failures))


def sql_string_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_string_list(values: Sequence[str]) -> str:
    return ", ".join(sql_string_literal(value) for value in values)


def build_daily_feature_sql(
    source_id: str,
    destination_id: str,
    code_column: str,
    include_adjust_type: bool,
) -> str:
    adjust_filter = "AND adjust_type = 'qfq'" if include_adjust_type else ""
    adjust_select = "adjust_type," if include_adjust_type else ""
    prefix = create_table_prefix(
        destination_id,
        partition=True,
        partition_field="partition_month",
        cluster_by=[code_column],
    )
    return f"""{prefix}
WITH base AS (
  SELECT
    {code_column},
    date,
    partition_month,
    {adjust_select}
    open,
    high,
    low,
    close,
    volume,
    amount
  FROM {quote_table(source_id)}
  WHERE date IS NOT NULL
    AND {code_column} IS NOT NULL
    AND close IS NOT NULL
    AND SAFE_CAST(close AS FLOAT64) > 0
    {adjust_filter}
),
ordered AS (
  SELECT
    *,
    LAG(close, 1) OVER code_date AS lag_close_1,
    LAG(close, 5) OVER code_date AS lag_close_5,
    LAG(close, 10) OVER code_date AS lag_close_10,
    LAG(close, 20) OVER code_date AS lag_close_20
  FROM base
  WINDOW code_date AS (PARTITION BY {code_column} ORDER BY date)
),
derived AS (
  SELECT
    *,
    SAFE_CAST(close AS FLOAT64) - SAFE_CAST(lag_close_1 AS FLOAT64) AS close_delta,
    LOG(SAFE_CAST(close AS FLOAT64)) - LOG(SAFE_CAST(lag_close_1 AS FLOAT64)) AS return_1d,
    LOG(SAFE_CAST(close AS FLOAT64)) - LOG(SAFE_CAST(lag_close_5 AS FLOAT64)) AS return_5d,
    LOG(SAFE_CAST(close AS FLOAT64)) - LOG(SAFE_CAST(lag_close_10 AS FLOAT64)) AS return_10d,
    LOG(SAFE_CAST(close AS FLOAT64)) - LOG(SAFE_CAST(lag_close_20 AS FLOAT64)) AS return_20d
  FROM ordered
),
rolling_raw AS (
  SELECT
    *,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w5 AS ma_5,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w10 AS ma_10,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w12 AS ma_12,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w20 AS ma_20,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w26 AS ma_26,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w60 AS ma_60,
    AVG(SAFE_CAST(volume AS FLOAT64)) OVER w5 AS volume_ma_5,
    AVG(SAFE_CAST(volume AS FLOAT64)) OVER w20 AS volume_ma_20,
    AVG(SAFE_CAST(amount AS FLOAT64)) OVER w5 AS amount_ma_5,
    AVG(SAFE_CAST(amount AS FLOAT64)) OVER w20 AS amount_ma_20,
    STDDEV_SAMP(SAFE_CAST(close AS FLOAT64)) OVER w5 AS std_5d,
    STDDEV_SAMP(SAFE_CAST(close AS FLOAT64)) OVER w20 AS std_20d,
    STDDEV_SAMP(return_1d) OVER w20 AS volatility_20,
    MAX(SAFE_CAST(high AS FLOAT64)) OVER w20 AS high_20d,
    MIN(SAFE_CAST(low AS FLOAT64)) OVER w20 AS low_20d,
    AVG(GREATEST(close_delta, 0)) OVER w14 AS avg_gain_14,
    AVG(ABS(LEAST(close_delta, 0))) OVER w14 AS avg_loss_14
  FROM derived
  WINDOW
    w5 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
    w10 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
    w12 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 11 PRECEDING AND CURRENT ROW),
    w14 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW),
    w20 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
    w26 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 25 PRECEDING AND CURRENT ROW),
    w60 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
),
with_macd AS (
  SELECT
    *,
    ma_12 - ma_26 AS macd_diff,
    AVG(ma_12 - ma_26) OVER (
      PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 8 PRECEDING AND CURRENT ROW
    ) AS macd_signal
  FROM rolling_raw
)
SELECT
  {code_column},
  date,
  partition_month,
  {adjust_select}
  open,
  high,
  low,
  close,
  volume,
  amount,
  return_1d,
  return_5d,
  return_10d,
  return_20d,
  ma_5,
  ma_10,
  ma_20,
  ma_60,
  SAFE_DIVIDE(SAFE_CAST(volume AS FLOAT64), NULLIF(volume_ma_5, 0)) AS volume_ma5_ratio,
  SAFE_DIVIDE(SAFE_CAST(volume AS FLOAT64), NULLIF(volume_ma_20, 0)) AS volume_ma20_ratio,
  SAFE_DIVIDE(SAFE_CAST(amount AS FLOAT64), NULLIF(amount_ma_5, 0)) AS amount_ma5_ratio,
  SAFE_DIVIDE(SAFE_CAST(amount AS FLOAT64), NULLIF(amount_ma_20, 0)) AS amount_ma20_ratio,
  std_5d,
  std_20d,
  SAFE_DIVIDE(std_5d, NULLIF(std_20d, 0)) AS std_ratio,
  volatility_20,
  100 - SAFE_DIVIDE(100, 1 + SAFE_DIVIDE(avg_gain_14, NULLIF(avg_loss_14, 0))) AS rsi_14,
  macd_diff,
  macd_signal,
  macd_diff - macd_signal AS macd_hist,
  SAFE_DIVIDE(SAFE_CAST(close AS FLOAT64) - low_20d, NULLIF(high_20d - low_20d, 0)) AS close_to_high_20d,
  SAFE_DIVIDE(SAFE_CAST(close AS FLOAT64), NULLIF(ma_5, 0)) - 1 AS close_to_ma5,
  SAFE_DIVIDE(SAFE_CAST(close AS FLOAT64), NULLIF(ma_20, 0)) - 1 AS close_to_ma20,
  CURRENT_TIMESTAMP() AS feature_generated_at
FROM with_macd
"""


def build_equity_daily_features_sql(config: dict) -> str:
    return build_daily_feature_sql(
        table_id(config, "dwd_fact_equity_kline_1d"),
        table_id(config, "dws_equity_daily_features"),
        "equity_code",
        include_adjust_type=True,
    )


def build_fund_daily_features_sql(config: dict) -> str:
    return build_daily_feature_sql(
        table_id(config, "dwd_fact_fund_kline_1d"),
        table_id(config, "dws_fund_daily_features"),
        "fund_code",
        include_adjust_type=True,
    )


def build_index_daily_features_sql(config: dict) -> str:
    return build_daily_feature_sql(
        table_id(config, "dwd_fact_index_kline_1d"),
        table_id(config, "dws_index_daily_features"),
        "index_code",
        include_adjust_type=False,
    )


def build_portfolio_asset_returns_sql(config: dict) -> str:
    destination_id = table_id(config, "dws_portfolio_asset_returns_1d")
    prefix = create_table_prefix(
        destination_id,
        partition=True,
        partition_field="partition_month",
        cluster_by=["asset_type", "asset_code"],
    )
    return f"""{prefix}
SELECT
  'equity' AS asset_type,
  equity_code AS asset_code,
  date,
  partition_month,
  close,
  return_1d,
  return_5d,
  return_20d,
  volatility_20,
  CURRENT_TIMESTAMP() AS feature_generated_at
FROM {quote_table(table_id(config, "dws_equity_daily_features"))}
UNION ALL
SELECT
  'fund' AS asset_type,
  fund_code AS asset_code,
  date,
  partition_month,
  close,
  return_1d,
  return_5d,
  return_20d,
  volatility_20,
  CURRENT_TIMESTAMP() AS feature_generated_at
FROM {quote_table(table_id(config, "dws_fund_daily_features"))}
UNION ALL
SELECT
  'index' AS asset_type,
  index_code AS asset_code,
  date,
  partition_month,
  close,
  return_1d,
  return_5d,
  return_20d,
  volatility_20,
  CURRENT_TIMESTAMP() AS feature_generated_at
FROM {quote_table(table_id(config, "dws_index_daily_features"))}
"""


def build_board_component_latest_sql(config: dict) -> str:
    destination_id = table_id(config, "dws_board_component_latest")
    prefix = create_table_prefix(destination_id, partition=False, cluster_by=["board_code", "equity_code"])
    return f"""{prefix}
SELECT
  board_code,
  equity_code,
  security_code,
  board_name,
  equity_name,
  date AS latest_date,
  CURRENT_TIMESTAMP() AS feature_generated_at
FROM {quote_table(table_id(config, "dwd_fact_board_component_1d"))}
WHERE board_code IS NOT NULL
  AND equity_code IS NOT NULL
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY board_code, equity_code
  ORDER BY date DESC, source_file DESC, source_hash DESC
) = 1
"""


def ads_config(config: dict) -> dict:
    return config.get("defaults", {}).get("ads", {})


def pair_candidate_universe(config: dict) -> list[str]:
    configured = ads_config(config).get("pair_candidate_universe") or []
    return [str(code).strip().upper() for code in configured if str(code).strip()]


def build_pair_candidate_stats_sql(config: dict) -> str:
    destination_id = table_id(config, "dws_pair_candidate_stats")
    universe = pair_candidate_universe(config)
    if not universe:
        raise RuntimeError("defaults.ads.pair_candidate_universe must not be empty")
    prefix = create_table_prefix(destination_id, partition=False, cluster_by=["code_x", "code_y"])
    return f"""{prefix}
WITH max_date AS (
  SELECT MAX(date) AS end_date
  FROM {quote_table(table_id(config, "dws_equity_daily_features"))}
  WHERE equity_code IN ({sql_string_list(universe)})
),
base AS (
  SELECT
    equity_code,
    date,
    SAFE_CAST(close AS FLOAT64) AS close,
    return_1d
  FROM {quote_table(table_id(config, "dws_equity_daily_features"))}, max_date
  WHERE equity_code IN ({sql_string_list(universe)})
    AND date >= DATE_SUB(end_date, INTERVAL 756 DAY)
    AND close IS NOT NULL
    AND return_1d IS NOT NULL
),
pairs AS (
  SELECT
    x.equity_code AS code_x,
    y.equity_code AS code_y,
    COUNT(*) AS observation_count,
    CORR(x.return_1d, y.return_1d) AS return_corr,
    SAFE_DIVIDE(COVAR_SAMP(x.return_1d, y.return_1d), NULLIF(VAR_SAMP(y.return_1d), 0)) AS beta_xy,
    AVG(LOG(x.close) - LOG(y.close)) AS avg_log_spread,
    STDDEV_SAMP(LOG(x.close) - LOG(y.close)) AS std_log_spread,
    MAX(x.date) AS latest_date
  FROM base AS x
  JOIN base AS y
    ON x.date = y.date
   AND x.equity_code < y.equity_code
  GROUP BY code_x, code_y
)
SELECT
  *,
  CURRENT_TIMESTAMP() AS feature_generated_at
FROM pairs
WHERE observation_count >= 120
"""


def build_double_ma_signal_sql(config: dict) -> str:
    destination_id = table_id(config, "ads_signal_double_ma_1d")
    prefix = create_table_prefix(
        destination_id,
        partition=True,
        partition_field="partition_month",
        cluster_by=["fund_code"],
    )
    return f"""{prefix}
SELECT
  fund_code,
  date,
  partition_month,
  close,
  ma_5,
  ma_20,
  CASE WHEN ma_5 > ma_20 THEN 1 ELSE 0 END AS signal,
  CASE WHEN ma_5 > ma_20 THEN 'long' ELSE 'flat' END AS signal_label,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM {quote_table(table_id(config, "dws_fund_daily_features"))}
WHERE ma_5 IS NOT NULL
  AND ma_20 IS NOT NULL
"""


def build_ml_stock_picker_signal_sql(config: dict) -> str:
    top_n = int(ads_config(config).get("ml_stock_picker_top_n", 50))
    destination_id = table_id(config, "ads_signal_ml_stock_picker_1d")
    prefix = create_table_prefix(
        destination_id,
        partition=True,
        partition_field="partition_month",
        cluster_by=["date", "equity_code"],
    )
    return f"""{prefix}
WITH scored AS (
  SELECT
    equity_code,
    date,
    partition_month,
    close,
    0.35 * COALESCE(return_20d, 0)
      + 0.20 * COALESCE(return_5d, 0)
      + 0.15 * COALESCE(volume_ma20_ratio - 1, 0)
      - 0.20 * COALESCE(std_ratio, 0)
      + 0.10 * COALESCE(close_to_ma20, 0) AS score_proxy,
    return_1d,
    return_5d,
    return_20d,
    volume_ma20_ratio,
    std_ratio,
    rsi_14,
    macd_hist,
    close_to_ma20
  FROM {quote_table(table_id(config, "dws_equity_daily_features"))}
  WHERE return_20d IS NOT NULL
    AND volume_ma20_ratio IS NOT NULL
    AND std_ratio IS NOT NULL
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY date ORDER BY score_proxy DESC, equity_code) AS score_rank
  FROM scored
)
SELECT
  equity_code,
  date,
  partition_month,
  close,
  score_proxy,
  score_rank,
  {top_n} AS top_n,
  score_rank <= {top_n} AS is_selected,
  CASE WHEN score_rank <= {top_n} THEN 1 ELSE 0 END AS signal,
  return_1d,
  return_5d,
  return_20d,
  volume_ma20_ratio,
  std_ratio,
  rsi_14,
  macd_hist,
  close_to_ma20,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM ranked
"""


def build_volatility_timing_signal_sql(config: dict) -> str:
    destination_id = table_id(config, "ads_signal_volatility_timing_1d")
    prefix = create_table_prefix(
        destination_id,
        partition=True,
        partition_field="partition_month",
        cluster_by=["index_code"],
    )
    return f"""{prefix}
WITH base AS (
  SELECT
    index_code,
    date,
    partition_month,
    close,
    return_20d,
    volatility_20,
    AVG(volatility_20) OVER (
      PARTITION BY index_code ORDER BY date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW
    ) AS volatility_252_avg
  FROM {quote_table(table_id(config, "dws_index_daily_features"))}
  WHERE return_20d IS NOT NULL
    AND volatility_20 IS NOT NULL
)
SELECT
  index_code,
  date,
  partition_month,
  close,
  return_20d,
  volatility_20,
  volatility_252_avg,
  CASE
    WHEN volatility_20 > volatility_252_avg * 1.25 THEN 0.5
    WHEN return_20d > 0 THEN 1.0
    ELSE 0.3
  END AS position_scale,
  CASE
    WHEN volatility_20 > volatility_252_avg * 1.25 THEN 'high_volatility'
    WHEN return_20d > 0 THEN 'risk_on'
    ELSE 'risk_off'
  END AS signal_label,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM base
WHERE volatility_252_avg IS NOT NULL
"""


def build_regime_switching_signal_sql(config: dict) -> str:
    destination_id = table_id(config, "ads_signal_regime_switching_1d")
    prefix = create_table_prefix(
        destination_id,
        partition=True,
        partition_field="partition_month",
        cluster_by=["index_code", "regime_label"],
    )
    return f"""{prefix}
WITH base AS (
  SELECT
    index_code,
    date,
    partition_month,
    close,
    return_20d,
    volatility_20,
    amount_ma20_ratio,
    AVG(volatility_20) OVER (
      PARTITION BY index_code ORDER BY date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW
    ) AS volatility_252_avg
  FROM {quote_table(table_id(config, "dws_index_daily_features"))}
  WHERE return_20d IS NOT NULL
    AND volatility_20 IS NOT NULL
)
SELECT
  index_code,
  date,
  partition_month,
  close,
  return_20d,
  volatility_20,
  amount_ma20_ratio,
  CASE
    WHEN return_20d > 0 AND volatility_20 <= volatility_252_avg THEN 'bull'
    WHEN return_20d < 0 AND volatility_20 > volatility_252_avg THEN 'bear'
    ELSE 'sideways'
  END AS regime_label,
  CASE
    WHEN return_20d > 0 AND volatility_20 <= volatility_252_avg THEN 1.0
    WHEN return_20d < 0 AND volatility_20 > volatility_252_avg THEN 0.0
    ELSE 0.5
  END AS position_pct,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM base
WHERE volatility_252_avg IS NOT NULL
"""


def build_portfolio_risk_snapshot_sql(config: dict) -> str:
    destination_id = table_id(config, "ads_portfolio_risk_snapshot_1d")
    prefix = create_table_prefix(
        destination_id,
        partition=True,
        partition_field="partition_month",
        cluster_by=["asset_type"],
    )
    return f"""{prefix}
SELECT
  date,
  partition_month,
  asset_type,
  COUNT(DISTINCT asset_code) AS asset_count,
  AVG(return_1d) AS avg_return_1d,
  AVG(return_20d) AS avg_return_20d,
  AVG(volatility_20) AS avg_volatility_20,
  COUNTIF(volatility_20 > 0.05) AS high_vol_asset_count,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM {quote_table(table_id(config, "dws_portfolio_asset_returns_1d"))}
WHERE return_1d IS NOT NULL
GROUP BY date, partition_month, asset_type
"""


DWS_TABLE_BUILDERS = {
    "equity_daily_features": build_equity_daily_features_sql,
    "fund_daily_features": build_fund_daily_features_sql,
    "index_daily_features": build_index_daily_features_sql,
    "portfolio_asset_returns_1d": build_portfolio_asset_returns_sql,
    "board_component_latest": build_board_component_latest_sql,
    "pair_candidate_stats": build_pair_candidate_stats_sql,
}


ADS_TABLE_BUILDERS = {
    "signal_double_ma_1d": build_double_ma_signal_sql,
    "signal_ml_stock_picker_1d": build_ml_stock_picker_signal_sql,
    "signal_volatility_timing_1d": build_volatility_timing_signal_sql,
    "signal_regime_switching_1d": build_regime_switching_signal_sql,
    "portfolio_risk_snapshot_1d": build_portfolio_risk_snapshot_sql,
}


def transform_layer(config: dict, layer: str, target_table: str | None = None) -> None:
    if layer == "dws":
        builders = DWS_TABLE_BUILDERS
        name_fn = dws_table_name
    elif layer == "ads":
        builders = ADS_TABLE_BUILDERS
        name_fn = ads_table_name
    else:
        raise ValueError("layer must be 'dws' or 'ads'")

    if target_table:
        key = target_table.removeprefix(table_prefix(config, layer))
        if key not in builders:
            raise RuntimeError(f"Unknown {layer.upper()} target table: {target_table}")
        selected = {key: builders[key]}
    else:
        selected = builders

    client = bq_client(config)
    for key, builder in selected.items():
        sql = builder(config)
        job = client.query(sql)
        job.result()
        print(f"Transformed {layer.upper()} table {table_id(config, name_fn(key, config))}; job_id={job.job_id}")


def transform_dws(config: dict, target_table: str | None = None) -> None:
    transform_layer(config, "dws", target_table)


def transform_ads(config: dict, target_table: str | None = None) -> None:
    transform_layer(config, "ads", target_table)


def audit_table_specs(config: dict, layer: str) -> dict[str, set[str]]:
    if layer == "dws":
        prefix = table_prefix(config, "dws")
        return {
            f"{prefix}equity_daily_features": {"equity_code", "date", "partition_month", "return_20d", "rsi_14", "macd_hist"},
            f"{prefix}fund_daily_features": {"fund_code", "date", "partition_month", "ma_5", "ma_20", "return_20d"},
            f"{prefix}index_daily_features": {"index_code", "date", "partition_month", "return_20d", "volatility_20"},
            f"{prefix}portfolio_asset_returns_1d": {"asset_type", "asset_code", "date", "return_1d", "volatility_20"},
            f"{prefix}board_component_latest": {"board_code", "equity_code", "latest_date"},
            f"{prefix}pair_candidate_stats": {"code_x", "code_y", "observation_count", "return_corr", "beta_xy"},
        }
    if layer == "ads":
        prefix = table_prefix(config, "ads")
        return {
            f"{prefix}signal_double_ma_1d": {"fund_code", "date", "signal", "ma_5", "ma_20"},
            f"{prefix}signal_ml_stock_picker_1d": {"equity_code", "date", "score_proxy", "score_rank", "signal"},
            f"{prefix}signal_volatility_timing_1d": {"index_code", "date", "position_scale", "signal_label"},
            f"{prefix}signal_regime_switching_1d": {"index_code", "date", "regime_label", "position_pct"},
            f"{prefix}portfolio_risk_snapshot_1d": {"date", "asset_type", "asset_count", "avg_volatility_20"},
        }
    raise ValueError("layer must be 'dws' or 'ads'")


def audit_layer(config: dict, layer: str, target_table: str | None = None) -> None:
    client = bq_client(config)
    specs = audit_table_specs(config, layer)
    if target_table:
        table_name = target_table if target_table.startswith(table_prefix(config, layer)) else f"{table_prefix(config, layer)}{target_table}"
        specs = {table_name: specs[table_name]} if table_name in specs else {}
    if not specs:
        raise RuntimeError(f"No {layer.upper()} table specs found for audit.")

    missing: list[str] = []
    zero_rows: list[str] = []
    schema_errors: list[str] = []
    for table_name, required_fields in specs.items():
        full_id = table_id(config, table_name)
        try:
            table = client.get_table(full_id)
        except Exception as exc:
            missing.append(f"{full_id}: {exc}")
            continue
        row_count = int(table.num_rows or 0)
        fields = set(table_columns(table))
        absent = required_fields - fields
        if row_count == 0:
            zero_rows.append(full_id)
        if absent:
            schema_errors.append(f"{full_id}: missing fields {sorted(absent)}")
        status = "pass" if row_count > 0 and not absent else "failed"
        print(f"{full_id}: rows={row_count} fields={len(fields)} status={status}")

    errors: list[str] = []
    if missing:
        errors.append(f"missing {layer.upper()} tables ({len(missing)}):\n" + "\n".join(missing[:20]))
    if zero_rows:
        errors.append(f"zero-row {layer.upper()} tables ({len(zero_rows)}):\n" + "\n".join(zero_rows[:20]))
    if schema_errors:
        errors.append(f"{layer.upper()} schema errors ({len(schema_errors)}):\n" + "\n".join(schema_errors[:20]))
    if errors:
        raise RuntimeError(f"{layer.upper()} audit failed:\n" + "\n".join(errors))
    print(f"{layer.upper()} audit passed: {len(specs)} tables covered")


def audit_dws(config: dict, target_table: str | None = None) -> None:
    audit_layer(config, "dws", target_table)


def audit_ads(config: dict, target_table: str | None = None) -> None:
    audit_layer(config, "ads", target_table)


def progress(config: dict) -> None:
    records = read_manifest(manifest_path(config))
    summarize(records)


def sync_manifest(config: dict) -> None:
    bigquery = require_bigquery()
    manifest_file = manifest_path(config)
    records = read_manifest(manifest_file)
    if not records:
        raise RuntimeError("Manifest is empty. Run manifest or load first.")
    client = bq_client(config)
    destination = table_id(config, "ods_external_manifest")
    batch_ids = sorted({record.batch_id for record in records if record.batch_id})
    current_batch = batch_ids[0] if len(batch_ids) == 1 else batch_id()
    synced_at = utc_now()
    payload_path = manifest_file.with_suffix(".ods_external_manifest.ndjson")

    with payload_path.open("w", encoding="utf-8") as fh:
        for record in records:
            payload = {
                "batch_id": current_batch,
                "synced_at": synced_at,
                "gcs_uri": record.gcs_uri,
                "target_table": record.target_table,
                "destination_table": ods_table_name(record.target_table, config),
                "partition_month": record.partition_month,
                "object_size": record.object_size,
                "object_generation": record.object_generation,
                "source_format": record.source_format,
                "status": record.status,
                "error_message": record.error_message,
            }
            fh.write(json.dumps(payload, ensure_ascii=False) + "\n")

    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=ods_manifest_schema(),
        write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
    )
    safe_batch = re.sub(r"[^A-Za-z0-9_]+", "_", current_batch).strip("_")
    job_id = f"ods_external_manifest_{safe_batch}"
    with payload_path.open("rb") as fh:
        job = client.load_table_from_file(fh, destination, job_config=job_config, job_id=job_id)
    job.result()
    print(
        f"Synced manifest rows={len(records)} batch_id={current_batch} "
        f"to {destination}; job_id={job.job_id}"
    )


def audit_staging(config: dict) -> None:
    records = read_manifest(manifest_path(config))
    loaded_tables = sorted({record.target_table for record in records if record.status == "loaded"})
    if not loaded_tables:
        raise RuntimeError("No loaded tables found in manifest.")

    client = bq_client(config)
    missing: list[str] = []
    zero_rows: list[str] = []
    for target_table in loaded_tables:
        destination = table_id(config, ods_table_name(target_table, config))
        try:
            table = client.get_table(destination)
        except Exception as exc:
            missing.append(f"{destination}: {exc}")
            continue
        print(f"{destination}: rows={table.num_rows} fields={len(table.schema)}")
        if table.num_rows == 0:
            zero_rows.append(destination)

    if missing or zero_rows:
        details = []
        if missing:
            details.append("missing tables:\n" + "\n".join(missing[:20]))
        if zero_rows:
            details.append("zero-row tables:\n" + "\n".join(zero_rows[:20]))
        raise RuntimeError("Staging audit failed:\n" + "\n".join(details))
    print(f"Staging audit passed: tables={len(loaded_tables)}")


def build_manifest(config: dict) -> None:
    records = list(iter_gcs_records(_manifest_discovery_config(config)))
    write_manifest(manifest_path(config), records)
    summarize(records)


def main() -> int:
    parser = argparse.ArgumentParser(description="Load standardized A-share GCS objects into BigQuery.")
    sub = parser.add_subparsers(dest="command", required=True)

    for command in ("init-ods", "create-ods-external", "audit-ods"):
        p = sub.add_parser(command)
        p.add_argument("--config", default="gcs_to_bigquery/config.yaml")

    for command in ("init", "manifest", "load", "progress", "sync-manifest", "audit-staging"):
        p = sub.add_parser(command)
        p.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    sub.choices["load"].add_argument("--dry-run", action="store_true")
    sub.choices["load"].add_argument("--retry-failed", action="store_true")

    merge_parser = sub.add_parser("merge")
    merge_parser.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    merge_parser.add_argument("--table", required=True)

    transform_dwd_parser = sub.add_parser("transform-dwd")
    transform_dwd_parser.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    transform_dwd_parser.add_argument("--mode", choices=("full", "sample"), default="full")
    transform_dwd_parser.add_argument("--table")
    transform_dwd_parser.add_argument("--sample-limit", type=int)

    audit_dwd_parser = sub.add_parser("audit-dwd")
    audit_dwd_parser.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    audit_dwd_parser.add_argument("--scope", choices=("full", "sample"), default="full")
    audit_dwd_parser.add_argument("--table")

    smoke_parser = sub.add_parser("smoke-query")
    smoke_parser.add_argument("--config", default="gcs_to_bigquery/config.yaml")

    transform_dws_parser = sub.add_parser("transform-dws")
    transform_dws_parser.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    transform_dws_parser.add_argument("--table")

    audit_dws_parser = sub.add_parser("audit-dws")
    audit_dws_parser.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    audit_dws_parser.add_argument("--table")

    transform_ads_parser = sub.add_parser("transform-ads")
    transform_ads_parser.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    transform_ads_parser.add_argument("--table")

    audit_ads_parser = sub.add_parser("audit-ads")
    audit_ads_parser.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    audit_ads_parser.add_argument("--table")

    args = parser.parse_args()
    config = load_config(Path(args.config))
    ensure_manifest_parent(config)

    if args.command == "init-ods":
        init_ods(config)
        return 0
    if args.command == "create-ods-external":
        create_ods_external(config)
        return 0
    if args.command == "audit-ods":
        audit_ods_external(config)
        return 0
    if args.command == "init":
        print("WARNING: 'init' is deprecated; use init-ods instead.", file=sys.stderr)
        init(config)
        return 0
    if args.command == "manifest":
        build_manifest(config)
        return 0
    if args.command == "load":
        print(
            "WARNING: 'load' is deprecated for the ODS flow; use create-ods-external.",
            file=sys.stderr,
        )
        load(config, dry_run=args.dry_run, retry_failed=args.retry_failed)
        return 0
    if args.command == "merge":
        merge_table(config, args.table)
        return 0
    if args.command == "transform-dwd":
        transform_dwd(config, mode=args.mode, target_table=args.table, sample_limit=args.sample_limit)
        return 0
    if args.command == "audit-dwd":
        audit_dwd(config, scope=args.scope, target_table=args.table)
        return 0
    if args.command == "smoke-query":
        smoke_query(config)
        return 0
    if args.command == "transform-dws":
        transform_dws(config, target_table=args.table)
        return 0
    if args.command == "audit-dws":
        audit_dws(config, target_table=args.table)
        return 0
    if args.command == "transform-ads":
        transform_ads(config, target_table=args.table)
        return 0
    if args.command == "audit-ads":
        audit_ads(config, target_table=args.table)
        return 0
    if args.command == "progress":
        progress(config)
        return 0
    if args.command == "sync-manifest":
        sync_manifest(config)
        return 0
    if args.command == "audit-staging":
        print("WARNING: 'audit-staging' is deprecated; use audit-ods instead.", file=sys.stderr)
        audit_staging(config)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
