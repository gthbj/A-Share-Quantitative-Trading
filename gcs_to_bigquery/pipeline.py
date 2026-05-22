from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml


VALID_STATUSES_TO_SKIP = {"loaded", "merged", "skipped"}


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
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def norm_path(value: str) -> Path:
    return Path(value.replace("/", os.sep)).resolve()


def require_bigquery():
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: google-cloud-bigquery. "
            "Install it with: python -m pip install -r gcs_to_bigquery\\requirements.txt"
        ) from exc
    return bigquery


def require_storage():
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: google-cloud-storage. "
            "Install it with: python -m pip install -r gcs_to_bigquery\\requirements.txt"
        ) from exc
    return storage


def bq_client(config: dict):
    bigquery = require_bigquery()
    return bigquery.Client(project=config["project_id"], location=config.get("location"))


def storage_client(config: dict):
    storage = require_storage()
    return storage.Client(project=config["project_id"])


def dataset_id(config: dict, key: str) -> str:
    return f"{config['project_id']}.{config['datasets'][key]}"


def table_id(config: dict, dataset_key: str, table_name: str) -> str:
    return f"{dataset_id(config, dataset_key)}.{table_name}"


def staging_table_name(target_table: str) -> str:
    return f"stg_{target_table}"


def parse_object(config: dict, blob, current_batch_id: str) -> LoadRecord:
    prefix = config["gcs"]["prefix"].strip("/") + "/"
    relative = blob.name.removeprefix(prefix)
    parts = [part for part in relative.split("/") if part]
    target_table = parts[0] if parts else "unmapped"
    partition_month = None
    status = "pending"
    error_message = None

    match = re.search(r"partition_month=(\d{6})", relative)
    if match:
        partition_month = int(match.group(1))
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

    if target_table not in config.get("tables", {}):
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
    client.create_table(table, exists_ok=True)
    print(f"Ensured table: {table.full_table_id.replace(':', '.')}")


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


def load_job_config(config: dict, record: LoadRecord):
    bigquery = require_bigquery()
    if record.source_format == "CSV":
        csv_cfg = config.get("defaults", {}).get("csv", {})
        return bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.CSV,
            skip_leading_rows=csv_cfg.get("skip_leading_rows", 1),
            autodetect=csv_cfg.get("autodetect", True),
            allow_quoted_newlines=csv_cfg.get("allow_quoted_newlines", True),
            encoding=csv_cfg.get("encoding", "UTF-8"),
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        )
    if record.source_format == "PARQUET":
        return bigquery.LoadJobConfig(
            source_format=bigquery.SourceFormat.PARQUET,
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
            create_disposition=bigquery.CreateDisposition.CREATE_IF_NEEDED,
        )
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


def load(config: dict, dry_run: bool) -> None:
    manifest_path = norm_path(config["manifest_path"])
    records = read_manifest(manifest_path)
    if not records:
        records = list(iter_gcs_records(config))
        write_manifest(manifest_path, records)

    pending = [
        r for r in records
        if r.status == "pending" and not (config.get("defaults", {}).get("skip_loaded", True) and r.status in VALID_STATUSES_TO_SKIP)
    ]

    if dry_run:
        summarize(pending)
        for record in pending[:20]:
            print(f"{record.gcs_uri} -> ashare_raw.{staging_table_name(record.target_table)}")
        if len(pending) > 20:
            print(f"... {len(pending) - 20} more")
        return

    client = bq_client(config)
    updated: list[LoadRecord] = []
    for idx, record in enumerate(records, start=1):
        if record.status != "pending":
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
            write_manifest(manifest_path, updated + records[idx:])
            print(f"[{idx}/{len(records)}] failed {record.gcs_uri}: {exc}")
            raise

        if idx % 25 == 0:
            write_manifest(manifest_path, updated + records[idx:])
    write_manifest(manifest_path, updated)


def field_names(schema: list) -> list[str]:
    return [field.name for field in schema]


def require_table(client, full_table_id: str):
    try:
        from google.api_core.exceptions import NotFound
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: google-api-core. "
            "Install it with: python -m pip install -r gcs_to_bigquery\\requirements.txt"
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
    records = read_manifest(norm_path(config["manifest_path"]))
    summarize(records)


def build_manifest(config: dict) -> None:
    records = list(iter_gcs_records(config))
    write_manifest(norm_path(config["manifest_path"]), records)
    summarize(records)


def main() -> int:
    parser = argparse.ArgumentParser(description="Load standardized A-share GCS objects into BigQuery.")
    sub = parser.add_subparsers(dest="command", required=True)

    for command in ("init", "manifest", "load", "progress"):
        p = sub.add_parser(command)
        p.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    sub.choices["load"].add_argument("--dry-run", action="store_true")

    merge_parser = sub.add_parser("merge")
    merge_parser.add_argument("--config", default="gcs_to_bigquery/config.yaml")
    merge_parser.add_argument("--table", required=True)

    args = parser.parse_args()
    config = load_config(Path(args.config))

    if args.command == "init":
        init(config)
        return 0
    if args.command == "manifest":
        build_manifest(config)
        return 0
    if args.command == "load":
        load(config, dry_run=args.dry_run)
        return 0
    if args.command == "merge":
        merge_table(config, args.table)
        return 0
    if args.command == "progress":
        progress(config)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
