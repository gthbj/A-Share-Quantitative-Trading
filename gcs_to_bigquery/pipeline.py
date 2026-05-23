from __future__ import annotations

import argparse
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


def dataset_id(config: dict, key: str) -> str:
    return f"{config['project_id']}.{config['datasets'][key]}"


def table_id(config: dict, dataset_key: str, table_name: str) -> str:
    return f"{dataset_id(config, dataset_key)}.{table_name}"


def staging_table_name(target_table: str) -> str:
    return f"stg_{target_table}"


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
    for key in ("raw", "core", "mart"):
        ensure_dataset(client, dataset_id(config, key), location)

    manifest_table = bigquery.Table(table_id(config, "raw", "gcs_load_manifest"), schema=control_manifest_schema())
    manifest_table.time_partitioning = bigquery.TimePartitioning(field="started_at")
    ensure_table(client, manifest_table)

    errors_table = bigquery.Table(table_id(config, "raw", "gcs_load_errors"), schema=control_errors_schema())
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
        if column_name_character_map and hasattr(job_config, "column_name_character_map"):
            job_config.column_name_character_map = column_name_character_map
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
        if column_name_character_map and hasattr(job_config, "column_name_character_map"):
            job_config.column_name_character_map = column_name_character_map
        return job_config
    raise ValueError(f"Unsupported source format: {record.source_format}")


def load_to_staging(config: dict, client, record: LoadRecord) -> LoadRecord:
    started_at = utc_now()
    destination = table_id(config, "raw", staging_table_name(record.target_table))
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
    destination = table_id(config, "raw", staging_table_name(target_table))
    max_source_uris = int(config.get("defaults", {}).get("max_source_uris_per_job", 9000))
    replace_staging = bool(config.get("defaults", {}).get("replace_staging_tables", True))
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
            print(f"{record.gcs_uri} -> ashare_raw.{staging_table_name(record.target_table)}")
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
    staging_id = table_id(config, "raw", staging_table_name(target_table))
    core_id = table_id(config, "core", target_table)
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
    destination = table_id(config, "raw", "gcs_load_manifest")
    job_config = bigquery.LoadJobConfig(
        source_format=bigquery.SourceFormat.NEWLINE_DELIMITED_JSON,
        schema=control_manifest_schema(),
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
    )
    with manifest_file.open("rb") as fh:
        job = client.load_table_from_file(fh, destination, job_config=job_config)
    job.result()
    print(f"Synced manifest rows={len(records)} to {destination}; job_id={job.job_id}")


def audit_staging(config: dict) -> None:
    records = read_manifest(manifest_path(config))
    loaded_tables = sorted({record.target_table for record in records if record.status == "loaded"})
    if not loaded_tables:
        raise RuntimeError("No loaded tables found in manifest.")

    client = bq_client(config)
    missing: list[str] = []
    zero_rows: list[str] = []
    for target_table in loaded_tables:
        destination = table_id(config, "raw", staging_table_name(target_table))
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
    records = list(iter_gcs_records(config))
    write_manifest(manifest_path(config), records)
    summarize(records)


def main() -> int:
    parser = argparse.ArgumentParser(description="Load standardized A-share GCS objects into BigQuery.")
    sub = parser.add_subparsers(dest="command", required=True)

    for command in ("init", "manifest", "load", "progress", "sync-manifest", "audit-staging"):
        p = sub.add_parser(command)
        p.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    sub.choices["load"].add_argument("--dry-run", action="store_true")
    sub.choices["load"].add_argument("--retry-failed", action="store_true")

    merge_parser = sub.add_parser("merge")
    merge_parser.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    merge_parser.add_argument("--table", required=True)

    args = parser.parse_args()
    config = load_config(Path(args.config))
    ensure_manifest_parent(config)

    if args.command == "init":
        init(config)
        return 0
    if args.command == "manifest":
        build_manifest(config)
        return 0
    if args.command == "load":
        print(
            "WARNING: 'load' is deprecated for the ODS flow; use create-ods-external "
            "after PRD_10 external table support is implemented.",
            file=sys.stderr,
        )
        load(config, dry_run=args.dry_run, retry_failed=args.retry_failed)
        return 0
    if args.command == "merge":
        merge_table(config, args.table)
        return 0
    if args.command == "progress":
        progress(config)
        return 0
    if args.command == "sync-manifest":
        sync_manifest(config)
        return 0
    if args.command == "audit-staging":
        audit_staging(config)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
