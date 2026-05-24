from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import zipfile
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import yaml
from google.cloud import storage

from prepare_standardized_to_gcs import classify, is_relative_to, norm_path, slug


DATE_COLUMNS = [
    "交易日期",
    "日期",
    "停复牌日期",
    "公告日期",
    "实际公告日期",
    "预约披露日期",
    "报告期",
    "截止日期",
    "开始日期",
    "终止日期",
    "分红年度",
]

SECURITY_COLUMNS = [
    "股票代码",
    "基金代码",
    "基金交易代码",
    "代码",
    "指数代码",
    "板块代码",
    "成分股票代码",
]

FACT_TABLES_WITH_REQUIRED_DATE_PREFIXES = ("fact_",)
SOURCE_DATE_FACT_TABLES = {"fact_sw_industry_component_1d"}


@dataclass(frozen=True)
class ParquetRecord:
    target_table: str
    partition_month: str
    local_path: str
    gcs_uri: str
    size: int
    rows: int
    source_count: int
    fingerprint: str
    status: str = "pending"
    uploaded_at: str | None = None
    error: str | None = None


@dataclass(frozen=True)
class RawSyncRecord:
    source_gcs_uri: str
    local_path: str
    size: int
    updated_at: str | None
    status: str = "pending"
    synced_at: str | None = None
    error: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def gcloud_token_timeout_seconds(config: dict | None = None) -> int:
    return int((config or {}).get("auth", {}).get("gcloud_token_timeout_seconds", 30))


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
        raise RuntimeError("Missing dependency: google-auth") from exc

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


def storage_client(config: dict, project_id: str | None = None) -> storage.Client:
    kwargs = {"project": project_id or config["gcs"].get("project_id")}
    if use_gcloud_access_token(config):
        kwargs["credentials"] = gcloud_credentials(config)
    return storage.Client(**kwargs)


def fingerprint(*parts: object) -> str:
    payload = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()


def iter_sources(config: dict):
    source_root = norm_path(config["source_root"])
    ignore_dirs = [norm_path(p) for p in config.get("ignore_dirs", [])]
    for root, dirs, files in os.walk(source_root):
        root_path = Path(root).resolve()
        dirs[:] = [
            d
            for d in dirs
            if not any(is_relative_to((root_path / d).resolve(), ignored) for ignored in ignore_dirs)
        ]
        for file_name in files:
            path = (root_path / file_name).resolve()
            suffix = path.suffix.lower()
            if suffix == ".csv":
                yield path, None
            elif suffix == ".zip":
                try:
                    with zipfile.ZipFile(path) as zf:
                        for info in zf.infolist():
                            if not info.is_dir() and info.filename.lower().endswith(".csv"):
                                yield path, info.filename
                except zipfile.BadZipFile:
                    continue


@lru_cache(maxsize=256)
def cached_zip(path: str) -> zipfile.ZipFile:
    return zipfile.ZipFile(path)


def source_bytes(source: Path, entry: str | None) -> bytes | Path:
    if entry:
        return cached_zip(str(source)).read(entry)
    return source


def read_csv_chunks(source: Path, entry: str | None, chunksize: int):
    encodings = ("utf-8-sig", "utf-8", "gb18030")
    engines = ("c", "python")
    last_error: Exception | None = None
    for encoding in encodings:
        for engine in engines:
            try:
                if entry:
                    with cached_zip(str(source)).open(entry) as handle:
                        yield from pd.read_csv(
                            handle,
                            dtype=str,
                            chunksize=chunksize,
                            encoding=encoding,
                            engine=engine,
                            on_bad_lines="skip",
                            keep_default_na=False,
                        )
                else:
                    yield from pd.read_csv(
                        source,
                        dtype=str,
                        chunksize=chunksize,
                        encoding=encoding,
                        engine=engine,
                        on_bad_lines="skip",
                        keep_default_na=False,
                    )
                return
            except (UnicodeDecodeError, pd.errors.ParserError, MemoryError) as exc:
                last_error = exc
                continue
    if last_error:
        raise last_error


def read_csv_header(source: Path, entry: str | None) -> list[str]:
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            if entry:
                with cached_zip(str(source)).open(entry) as handle:
                    frame = pd.read_csv(handle, dtype=str, nrows=0, encoding=encoding, on_bad_lines="skip")
            else:
                frame = pd.read_csv(source, dtype=str, nrows=0, encoding=encoding, on_bad_lines="skip")
            return [str(c).strip().lstrip("\ufeff") for c in frame.columns]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
        except (pd.errors.ParserError, MemoryError) as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return []


def raw_sync_manifest_path(config: dict) -> Path:
    raw_config = config.get("raw_gcs", {})
    if raw_config.get("manifest_path"):
        return norm_path(raw_config["manifest_path"])
    work_dir = norm_path(config.get("work_dir", norm_path(config["manifest_path"]).parent))
    return work_dir / "raw_sync_manifest.jsonl"


def raw_sync_source_prefix(config: dict) -> str:
    return str(config.get("raw_gcs", {}).get("source_prefix", "")).strip("/")


def raw_sync_bucket_name(config: dict) -> str:
    raw_config = config.get("raw_gcs", {})
    return str(raw_config.get("bucket") or config["gcs"]["bucket"])


def raw_sync_project_id(config: dict) -> str | None:
    raw_config = config.get("raw_gcs", {})
    return raw_config.get("project_id") or config["gcs"].get("project_id")


def raw_sync_include_suffixes(config: dict) -> tuple[str, ...]:
    suffixes = config.get("raw_gcs", {}).get("include_suffixes", [".csv"])
    normalized = []
    for suffix in suffixes:
        text = str(suffix).strip().lower()
        if text and not text.startswith("."):
            text = f".{text}"
        normalized.append(text)
    return tuple(normalized)


def raw_sync_include_specs(config: dict) -> list[tuple[str, tuple[str, ...]]]:
    raw_config = config.get("raw_gcs", {})
    default_suffixes = raw_sync_include_suffixes(config)
    include_items = raw_config.get("include_items")
    if include_items:
        specs: list[tuple[str, tuple[str, ...]]] = []
        for item in include_items:
            if isinstance(item, str):
                specs.append((item.strip("/"), default_suffixes))
                continue
            prefix = str(item["prefix"]).strip("/")
            suffixes = item.get("suffixes", default_suffixes)
            normalized_suffixes = []
            for suffix in suffixes:
                text = str(suffix).strip().lower()
                if text and not text.startswith("."):
                    text = f".{text}"
                normalized_suffixes.append(text)
            specs.append((prefix, tuple(normalized_suffixes)))
        return specs
    return [(str(prefix).strip("/"), default_suffixes) for prefix in raw_config.get("include_prefixes") or [""]]


def iter_raw_gcs_blobs(config: dict):
    raw_config = config.get("raw_gcs")
    if not raw_config:
        raise RuntimeError("Missing raw_gcs config. Add source_prefix and include_prefixes first.")
    source_prefix = raw_sync_source_prefix(config)
    if not source_prefix:
        raise RuntimeError("raw_gcs.source_prefix is required")
    include_specs = raw_sync_include_specs(config)
    gcs_client = storage_client(config, project_id=raw_sync_project_id(config))
    bucket = gcs_client.bucket(raw_sync_bucket_name(config))
    seen: set[str] = set()
    for include_prefix, suffixes in include_specs:
        blob_prefix = "/".join(part.strip("/") for part in (source_prefix, str(include_prefix)) if part).strip("/")
        for blob in gcs_client.list_blobs(bucket, prefix=blob_prefix):
            if blob.name in seen:
                continue
            seen.add(blob.name)
            if blob.name.endswith("/"):
                continue
            if suffixes and not blob.name.lower().endswith(suffixes):
                continue
            yield blob


def raw_local_path_for_blob(config: dict, blob_name: str) -> Path:
    source_prefix = raw_sync_source_prefix(config)
    prefix = source_prefix + "/"
    if not blob_name.startswith(prefix):
        raise RuntimeError(f"Raw object is outside source_prefix: {blob_name}")
    relative = blob_name.removeprefix(prefix)
    if not relative or relative.startswith("../") or "/../" in relative:
        raise RuntimeError(f"Unsafe raw object path: {blob_name}")
    return norm_path(config["source_root"]) / relative


def write_raw_sync_manifest(path: Path, records: list[RawSyncRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    os.replace(temp_path, path)


def sync_raw(config: dict, dry_run: bool = False, limit: int | None = None) -> list[RawSyncRecord]:
    records: list[RawSyncRecord] = []
    skip_size_match = bool(config.get("raw_gcs", {}).get("skip_if_local_size_matches", True))
    manifest_path = raw_sync_manifest_path(config)
    for idx, blob in enumerate(iter_raw_gcs_blobs(config), start=1):
        if limit and idx > limit:
            break
        local_path = raw_local_path_for_blob(config, blob.name)
        size = int(blob.size or 0)
        source_uri = f"gs://{blob.bucket.name}/{blob.name}"
        updated_at = blob.updated.isoformat() if blob.updated else None
        record = RawSyncRecord(source_uri, str(local_path), size, updated_at)
        if dry_run:
            records.append(record)
            continue
        try:
            if skip_size_match and local_path.exists() and local_path.stat().st_size == size:
                records.append(RawSyncRecord(**{**asdict(record), "status": "skipped", "synced_at": utc_now()}))
            else:
                local_path.parent.mkdir(parents=True, exist_ok=True)
                blob.download_to_filename(local_path)
                if local_path.stat().st_size != size:
                    raise RuntimeError(f"Local size mismatch: {local_path.stat().st_size} != {size}")
                records.append(RawSyncRecord(**{**asdict(record), "status": "synced", "synced_at": utc_now()}))
            if idx % 100 == 0:
                write_raw_sync_manifest(manifest_path, records)
                print(f"[{idx}] raw synced/skipped: {source_uri}", flush=True)
        except Exception as exc:
            records.append(RawSyncRecord(**{**asdict(record), "status": "failed", "error": str(exc)}))
            write_raw_sync_manifest(manifest_path, records)
            raise
    total_bytes = sum(record.size for record in records)
    print(f"Raw objects: {len(records)}")
    print(f"Raw GiB: {total_bytes / 1024**3:.3f}")
    if dry_run:
        for record in records[:20]:
            print(f"{record.source_gcs_uri} -> {record.local_path}")
    else:
        write_raw_sync_manifest(manifest_path, records)
        print(f"Raw manifest: {manifest_path}")
    return records

def detect_date_column(columns: list[str], table: str) -> str | None:
    for column in DATE_COLUMNS:
        if column in columns:
            return column
    for column in columns:
        if "日期" in column or column.endswith("日") or "报告期" in column or "年度" in column:
            return column
    return None


def normalize_date(value: object) -> str | None:
    text = "" if value is None else str(value).strip()
    if not text or text.lower() in {"nan", "none", "nat"}:
        return None
    text = re.sub(r"\.0$", "", text)
    digits = re.sub(r"\D", "", text)
    if len(digits) >= 8:
        year, month, day = digits[:4], digits[4:6], digits[6:8]
    elif len(digits) == 6:
        year, month, day = digits[:4], digits[4:6], "01"
    elif len(digits) == 4:
        year, month, day = digits[:4], "12", "31"
    else:
        parsed = pd.to_datetime(text, errors="coerce")
        if pd.isna(parsed):
            return None
        return parsed.strftime("%Y-%m-%d")
    try:
        parsed = pd.Timestamp(year=int(year), month=int(month), day=int(day))
    except ValueError:
        return None
    return parsed.strftime("%Y-%m-%d")


def date_from_source_path(source: Path, entry: str | None = None) -> str | None:
    text = f"{source.as_posix()}/{entry or ''}"
    latest_in_path = None
    for match in re.finditer(r"(?<!\d)((?:19|20)\d{2})(0[1-9]|1[0-2])([0-3]\d)(?!\d)", text):
        value = f"{match.group(1)}{match.group(2)}{match.group(3)}"
        normalized = normalize_date(value)
        if normalized:
            latest_in_path = normalized
    return latest_in_path


def normalize_security_code(value: object) -> str | None:
    text = "" if value is None else str(value).strip().upper()
    if not text:
        return None
    text = text.replace("_", ".")
    if re.fullmatch(r"\d{6}", text):
        if text.startswith(("5", "6", "9")):
            return f"{text}.SH"
        return f"{text}.SZ"
    if re.fullmatch(r"(SH|SZ|BJ)\d{6}", text):
        return f"{text[2:]}.{text[:2]}"
    return text


def enrich_frame(frame: pd.DataFrame, table: str, source: Path, entry: str | None) -> tuple[pd.DataFrame, str | None]:
    frame = frame.copy()
    frame.columns = [str(c).strip().lstrip("\ufeff") for c in frame.columns]
    if table in SOURCE_DATE_FACT_TABLES:
        source_date = date_from_source_path(source, entry)
        if not source_date:
            return frame.iloc[0:0], None
        date_col = "__source_file_date__"
        frame["date"] = source_date
    else:
        date_col = detect_date_column(list(frame.columns), table)
    if date_col and date_col != "__source_file_date__":
        frame["date"] = frame[date_col].map(normalize_date)
        frame = frame[frame["date"].notna() & (frame["date"] != "")]
    elif not date_col and table.startswith(FACT_TABLES_WITH_REQUIRED_DATE_PREFIXES):
        return frame.iloc[0:0], None

    security_col = next((c for c in SECURITY_COLUMNS if c in frame.columns), None)
    if security_col:
        frame["security_code"] = frame[security_col].map(normalize_security_code)

    frame["source_file"] = source.as_posix()
    frame["source_entry"] = entry or ""
    frame["target_table"] = table
    for column in frame.columns:
        frame[column] = frame[column].astype("string")
    return frame, date_col


def partition_months(frame: pd.DataFrame, table: str) -> pd.Series:
    if "date" in frame.columns:
        return frame["date"].str.slice(0, 7).str.replace("-", "", regex=False)
    return pd.Series(["all"] * len(frame), index=frame.index, dtype="string")


def parquet_path(config: dict, table: str, partition_month: str, source: Path, entry: str | None, seq: int) -> Path:
    source_root = norm_path(config["source_root"])
    relative = source.relative_to(source_root).as_posix()
    unique = fingerprint(relative, entry, seq)[:16]
    name = slug(entry or source.name)
    if name.lower().endswith(".csv"):
        name = name[:-4]
    if name.lower().endswith(".zip"):
        name = name[:-4]
    file_name = f"part-{unique}_{name}.parquet"
    return norm_path(config["parquet_root"]) / table / f"partition_month={partition_month}" / file_name


class PartitionWriter:
    def __init__(self, path: Path, schema: pa.Schema, flush_rows: int):
        self.path = path
        self.temp_path = path.with_suffix(".parquet.tmp")
        self.rows = 0
        self.flush_rows = flush_rows
        self.buffer_rows = 0
        self.buffers: list[pd.DataFrame] = []
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.writer = pq.ParquetWriter(self.temp_path, schema=schema, compression="zstd")

    def write(self, frame: pd.DataFrame, schema: pa.Schema) -> None:
        self.buffers.append(frame)
        self.buffer_rows += len(frame)
        if self.buffer_rows >= self.flush_rows:
            self.flush(schema)

    def flush(self, schema: pa.Schema) -> None:
        if not self.buffers:
            return
        frame = pd.concat(self.buffers, ignore_index=True)
        table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
        self.writer.write_table(table)
        self.rows += len(frame)
        self.buffers.clear()
        self.buffer_rows = 0

    def close(self, schema: pa.Schema) -> None:
        self.flush(schema)
        self.writer.close()
        os.replace(self.temp_path, self.path)


def gcs_uri(config: dict, local_path: Path) -> str:
    rel = local_path.relative_to(norm_path(config["parquet_root"])).as_posix()
    return f"gs://{config['gcs']['bucket']}/{config['gcs']['prefix'].strip('/')}/{rel}"


def schema_columns_for_sources(sources: list[tuple[Path, str | None]]) -> list[str]:
    columns: list[str] = []
    seen: set[str] = set()
    for source, entry in sources:
        for column in read_csv_header(source, entry):
            if column and column not in seen:
                columns.append(column)
                seen.add(column)
    for column in ("date", "security_code", "source_file", "source_entry", "target_table"):
        if column not in seen:
            columns.append(column)
            seen.add(column)
    return columns


def align_to_schema(frame: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        frame = pd.concat(
            [frame, pd.DataFrame({column: pd.NA for column in missing}, index=frame.index)],
            axis=1,
        )
    frame = frame[columns]
    for column in frame.columns:
        frame[column] = frame[column].astype("string")
    return frame


def table_build_int(config: dict, table: str, key: str, default: int) -> int:
    build_config = config.get("build", {})
    per_table = build_config.get(f"table_{key}", {})
    if isinstance(per_table, dict) and table in per_table:
        return int(per_table[table])
    return int(build_config.get(key, default))


def table_parallel_workers(config: dict, table: str) -> int:
    workers = config.get("build", {}).get("table_parallel_workers", {})
    if not isinstance(workers, dict):
        return 1
    return max(1, int(workers.get(table, 1)))


def split_round_robin(items: list[tuple[Path, str | None]], parts: int) -> list[list[tuple[Path, str | None]]]:
    shards: list[list[tuple[Path, str | None]]] = [[] for _ in range(parts)]
    for idx, item in enumerate(items):
        shards[idx % parts].append(item)
    return [shard for shard in shards if shard]


def build_table(
    config: dict,
    table: str,
    sources: list[tuple[Path, str | None]],
    columns: list[str] | None = None,
    cleanup_outputs: bool = True,
    write_checkpoint: bool = True,
    file_prefix: str | None = None,
    progress_label: str | None = None,
) -> tuple[list[ParquetRecord], dict[str, int]]:
    cached_zip.cache_clear()
    records: list[ParquetRecord] = []
    chunksize = table_build_int(config, table, "chunksize", 50000)
    flush_rows = table_build_int(config, table, "flush_rows", 50000)
    progress_interval = table_build_int(config, table, "progress_interval", 1000)
    skipped_no_date: dict[str, int] = {}
    parquet_root = norm_path(config["parquet_root"])
    if cleanup_outputs and checkpoint_enabled(config):
        cleanup_table_outputs(config, table)

    columns = columns or schema_columns_for_sources(sources)
    schema = pa.schema([pa.field(column, pa.string()) for column in columns])
    writers: dict[str, PartitionWriter] = {}
    source_count_by_month: dict[str, int] = {}
    label = progress_label or table
    print(f"building {label}: sources={len(sources)} columns={len(columns)}", flush=True)

    for source_idx, (source, entry) in enumerate(sources, start=1):
        source_months: set[str] = set()
        source_has_usable_rows = False
        source_missing_date = False
        source_hash = fingerprint(source.relative_to(norm_path(config["source_root"])).as_posix(), entry)[:16]
        for chunk in read_csv_chunks(source, entry, chunksize):
            frame, date_col = enrich_frame(chunk, table, source, entry)
            if frame.empty:
                if table.startswith("fact_") and not date_col:
                    source_missing_date = True
                continue
            source_has_usable_rows = True
            months = partition_months(frame, table)
            for partition_month, month_frame in frame.groupby(months, dropna=True):
                partition_month = str(partition_month)
                if table.startswith("fact_") and not re.fullmatch(r"\d{6}", partition_month):
                    raise RuntimeError(f"Invalid fact partition {partition_month}: {source} :: {entry}")
                if not re.fullmatch(r"\d{6}|all", partition_month):
                    raise RuntimeError(f"Invalid partition_month={partition_month}: {source} :: {entry}")
                if partition_month not in writers:
                    prefix = f"{file_prefix}-" if file_prefix else ""
                    part_path = parquet_root / table / f"partition_month={partition_month}" / f"part-{prefix}{source_hash[:8]}-{len(writers):05d}.parquet"
                    writers[partition_month] = PartitionWriter(part_path, schema, flush_rows)
                aligned = align_to_schema(month_frame, columns)
                writers[partition_month].write(aligned, schema)
                source_months.add(partition_month)
        if table.startswith("fact_") and source_missing_date and not source_has_usable_rows:
            skipped_no_date[table] = skipped_no_date.get(table, 0) + 1
        for partition_month in source_months:
            source_count_by_month[partition_month] = source_count_by_month.get(partition_month, 0) + 1
        if source_idx % progress_interval == 0:
            print(f"  {label}: processed_sources={source_idx}/{len(sources)} open_partitions={len(writers)}", flush=True)

    for partition_month, writer in sorted(writers.items()):
        writer.close(schema)
        stat = writer.path.stat()
        records.append(
            ParquetRecord(
                target_table=table,
                partition_month=partition_month,
                local_path=str(writer.path),
                gcs_uri=gcs_uri(config, writer.path),
                size=stat.st_size,
                rows=writer.rows,
                source_count=source_count_by_month.get(partition_month, 0),
                fingerprint=fingerprint(writer.path, stat.st_size, stat.st_mtime_ns),
            )
        )
    if write_checkpoint and checkpoint_enabled(config) and not skipped_no_date:
        write_table_checkpoint(config, table, records)
    return records, skipped_no_date


def build_table_parallel(config: dict, table: str, sources: list[tuple[Path, str | None]], workers: int) -> tuple[list[ParquetRecord], dict[str, int]]:
    if checkpoint_enabled(config):
        cleanup_table_outputs(config, table)

    columns = schema_columns_for_sources(sources)
    cached_zip.cache_clear()
    shards = split_round_robin(sources, min(workers, len(sources)))
    print(
        f"parallel table build enabled: table={table} workers={len(shards)} sources={len(sources)} columns={len(columns)}",
        flush=True,
    )
    records: list[ParquetRecord] = []
    skipped_no_date: dict[str, int] = {}
    with ProcessPoolExecutor(max_workers=len(shards)) as executor:
        futures = {
            executor.submit(
                build_table,
                config,
                table,
                shard,
                columns,
                False,
                False,
                f"shard-{shard_idx:03d}",
                f"{table} shard={shard_idx + 1}/{len(shards)}",
            ): shard_idx
            for shard_idx, shard in enumerate(shards)
        }
        for future in as_completed(futures):
            shard_idx = futures[future]
            shard_records, shard_skipped = future.result()
            records.extend(shard_records)
            for key, value in shard_skipped.items():
                skipped_no_date[key] = skipped_no_date.get(key, 0) + value
            print(f"finished {table} shard={shard_idx + 1}/{len(shards)}: parquet_files={len(shard_records)}", flush=True)

    records.sort(key=lambda r: (r.target_table, r.partition_month, r.local_path))
    if checkpoint_enabled(config) and not skipped_no_date:
        write_table_checkpoint(config, table, records)
    return records, skipped_no_date


def checkpoint_enabled(config: dict) -> bool:
    return bool(config.get("build", {}).get("resume_checkpoint", False))


def table_checkpoint_dir(config: dict) -> Path:
    work_dir = norm_path(config.get("work_dir", norm_path(config["manifest_path"]).parent))
    return work_dir / "table_manifests"


def table_checkpoint_path(config: dict, table: str) -> Path:
    return table_checkpoint_dir(config) / f"{table}.jsonl.done"


def write_table_checkpoint(config: dict, table: str, records: list[ParquetRecord]) -> None:
    write_manifest(table_checkpoint_path(config, table), records)


def read_table_checkpoint(config: dict, table: str) -> list[ParquetRecord]:
    return read_manifest(table_checkpoint_path(config, table))


def cleanup_table_outputs(config: dict, table: str) -> None:
    table_root = norm_path(config["parquet_root"]) / table
    if table_root.exists():
        shutil.rmtree(table_root)


def checkpoint_is_valid(config: dict, table: str) -> tuple[bool, list[ParquetRecord]]:
    path = table_checkpoint_path(config, table)
    if not path.exists():
        return False, []
    records = read_manifest(path)
    if not records:
        return False, []
    try:
        validate_records(config, records, local=True)
    except RuntimeError as exc:
        print(f"checkpoint invalid for {table}: {exc}", flush=True)
        return False, []
    return True, records


def build(config: dict, limit: int | None = None) -> list[ParquetRecord]:
    records: list[ParquetRecord] = []
    skipped_no_date: dict[str, int] = {}
    grouped_sources: dict[str, list[tuple[Path, str | None]]] = {}

    for idx, (source, entry) in enumerate(iter_sources(config), start=1):
        table = classify(source, entry)
        if table == "unmapped":
            continue
        grouped_sources.setdefault(table, []).append((source, entry))
        if limit and idx >= limit:
            break

    parquet_root = norm_path(config["parquet_root"])
    resume = checkpoint_enabled(config) and not limit
    if parquet_root.exists() and not resume:
        shutil.rmtree(parquet_root)

    max_workers = int(config.get("build", {}).get("max_workers", 1))
    items = []
    for table, sources in sorted(grouped_sources.items()):
        if resume:
            ok, checkpoint_records = checkpoint_is_valid(config, table)
            if ok:
                records.extend(checkpoint_records)
                print(f"resume skip {table}: parquet_files={len(checkpoint_records)}", flush=True)
                continue
        items.append((table, sources))
    if limit or max_workers <= 1 or len(items) <= 1:
        for table, sources in items:
            table_workers = 1 if limit else table_parallel_workers(config, table)
            if table_workers > 1 and len(sources) > 1:
                table_records, table_skipped = build_table_parallel(config, table, sources, table_workers)
            else:
                table_records, table_skipped = build_table(config, table, sources)
            records.extend(table_records)
            for key, value in table_skipped.items():
                skipped_no_date[key] = skipped_no_date.get(key, 0) + value
    else:
        table_parallel_items = [(table, sources) for table, sources in items if table_parallel_workers(config, table) > 1 and len(sources) > 1]
        regular_items = [(table, sources) for table, sources in items if (table, sources) not in table_parallel_items]
        if regular_items:
            print(f"parallel build enabled: max_workers={max_workers} tables={len(regular_items)}", flush=True)
            with ProcessPoolExecutor(max_workers=max_workers) as executor:
                futures = {executor.submit(build_table, config, table, sources): table for table, sources in regular_items}
                for future in as_completed(futures):
                    table = futures[future]
                    table_records, table_skipped = future.result()
                    records.extend(table_records)
                    for key, value in table_skipped.items():
                        skipped_no_date[key] = skipped_no_date.get(key, 0) + value
                    print(f"finished {table}: parquet_files={len(table_records)}", flush=True)
        for table, sources in table_parallel_items:
            table_records, table_skipped = build_table_parallel(config, table, sources, table_parallel_workers(config, table))
            records.extend(table_records)
            for key, value in table_skipped.items():
                skipped_no_date[key] = skipped_no_date.get(key, 0) + value
            print(f"finished {table}: parquet_files={len(table_records)}", flush=True)

    if skipped_no_date:
        detail = ", ".join(f"{table}:{count}" for table, count in sorted(skipped_no_date.items()))
        raise RuntimeError(f"Fact CSV chunks without a usable date column: {detail}")
    records.sort(key=lambda r: (r.target_table, r.partition_month, r.local_path))
    write_manifest(norm_path(config["manifest_path"]), records)
    validate_records(config, records, local=True)
    return records

def write_manifest(path: Path, records: list[ParquetRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    os.replace(temp_path, path)


def read_manifest(path: Path) -> list[ParquetRecord]:
    if not path.exists():
        return []
    records: list[ParquetRecord] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                records.append(ParquetRecord(**json.loads(line)))
    return records


def validate_records(config: dict, records: list[ParquetRecord], local: bool = False) -> None:
    bucket = config["gcs"]["bucket"]
    prefix = config["gcs"]["prefix"].strip("/")
    parquet_root = norm_path(config["parquet_root"])
    errors: list[str] = []
    seen: set[str] = set()
    for record in records:
        uri_prefix = f"gs://{bucket}/{prefix}/"
        if not record.gcs_uri.startswith(uri_prefix):
            errors.append(f"wrong GCS prefix: {record.gcs_uri}")
        rel = record.gcs_uri.removeprefix(uri_prefix)
        parts = rel.split("/")
        if len(parts) < 3:
            errors.append(f"path too shallow: {record.gcs_uri}")
        if any(part.lower().endswith(".zip") for part in parts[:-1]):
            errors.append(f"zip-like directory segment: {record.gcs_uri}")
        if not parts[-1].endswith(".parquet"):
            errors.append(f"not parquet: {record.gcs_uri}")
        if record.target_table.startswith("fact_") and not re.fullmatch(r"\d{6}", record.partition_month):
            errors.append(f"fact table missing month partition: {record.gcs_uri}")
        if f"partition_month={record.partition_month}" not in parts:
            errors.append(f"partition mismatch: {record.gcs_uri}")
        if record.gcs_uri in seen:
            errors.append(f"duplicate object: {record.gcs_uri}")
        seen.add(record.gcs_uri)
        if local:
            local_path = Path(record.local_path).resolve()
            if not is_relative_to(local_path, parquet_root):
                errors.append(f"local path outside parquet_root: {local_path}")
            if not local_path.exists():
                errors.append(f"missing local file: {local_path}")
            elif local_path.stat().st_size != record.size:
                errors.append(f"local size mismatch: {local_path}")
    if errors:
        sample = "\n".join(f"- {err}" for err in errors[:30])
        extra = "" if len(errors) <= 30 else f"\n... {len(errors) - 30} more"
        raise RuntimeError(f"Parquet validation failed with {len(errors)} error(s):\n{sample}{extra}")


def manifest_summary(records: list[ParquetRecord]) -> None:
    total_size = sum(r.size for r in records)
    total_rows = sum(r.rows for r in records)
    tables: dict[str, int] = {}
    for r in records:
        tables[r.target_table] = tables.get(r.target_table, 0) + 1
    print(f"Parquet files: {len(records)}")
    print(f"Rows: {total_rows}")
    print(f"Bytes: {total_size}")
    for table, count in sorted(tables.items()):
        print(f"{table}: {count}")


def upload(config: dict, dry_run: bool = False) -> None:
    records = read_manifest(norm_path(config["manifest_path"]))
    if not records:
        raise RuntimeError("Manifest is empty. Run build first.")
    validate_records(config, records, local=True)
    if dry_run:
        manifest_summary(records)
        for record in records[:20]:
            print(record.gcs_uri)
        return

    gcs_client = storage_client(config)
    bucket = gcs_client.bucket(config["gcs"]["bucket"])
    skip_size_match = config.get("upload", {}).get("skip_if_remote_size_matches", True)
    verify_after_upload = config.get("upload", {}).get("verify_after_upload", True)
    updated: list[ParquetRecord] = []
    prefix = config["gcs"]["prefix"].strip("/")
    root = norm_path(config["parquet_root"])
    for idx, record in enumerate(records, start=1):
        try:
            local_path = Path(record.local_path)
            object_name = f"{prefix}/{local_path.relative_to(root).as_posix()}"
            blob = bucket.blob(object_name)
            if skip_size_match and blob.exists(gcs_client):
                blob.reload()
                if int(blob.size or -1) == local_path.stat().st_size:
                    updated.append(ParquetRecord(**{**asdict(record), "status": "skipped", "uploaded_at": utc_now(), "error": None}))
                    continue
            blob.upload_from_filename(str(local_path), content_type="application/octet-stream")
            if verify_after_upload:
                blob.reload()
                if int(blob.size or -1) != local_path.stat().st_size:
                    raise RuntimeError(f"Remote size mismatch: {blob.size} != {local_path.stat().st_size}")
            updated.append(ParquetRecord(**{**asdict(record), "status": "uploaded", "uploaded_at": utc_now(), "error": None}))
            print(f"[{idx}/{len(records)}] uploaded {record.target_table}/{local_path.name}", flush=True)
        except Exception as exc:
            updated.append(ParquetRecord(**{**asdict(record), "status": "failed", "error": str(exc)}))
            write_manifest(norm_path(config["manifest_path"]), updated + records[idx:])
            raise
        if idx % 100 == 0:
            write_manifest(norm_path(config["manifest_path"]), updated + records[idx:])
    write_manifest(norm_path(config["manifest_path"]), updated)


def audit(config: dict, remote: bool = False) -> None:
    records = read_manifest(norm_path(config["manifest_path"]))
    validate_records(config, records, local=True)
    sample_count = int(config.get("upload", {}).get("audit_sample_files", 30))
    for record in records[:sample_count]:
        metadata = pq.read_metadata(record.local_path)
        if metadata.num_rows != record.rows:
            raise RuntimeError(f"Parquet row count mismatch: {record.local_path}")
    if remote:
        gcs_client = storage_client(config)
        bucket_name = config["gcs"]["bucket"]
        prefix = config["gcs"]["prefix"].strip("/") + "/"
        remote_count = 0
        remote_bytes = 0
        bad_remote: list[str] = []
        for blob in gcs_client.list_blobs(bucket_name, prefix=prefix):
            remote_count += 1
            remote_bytes += int(blob.size or 0)
            if not blob.name.endswith(".parquet"):
                bad_remote.append(blob.name)
            if "/partition_month=unknown/" in blob.name or ".zip/" in blob.name:
                bad_remote.append(blob.name)
        if bad_remote:
            raise RuntimeError("Bad remote paths:\n" + "\n".join(bad_remote[:20]))
        print(f"Remote files: {remote_count}/{len(records)}")
        print(f"Remote GiB: {remote_bytes / 1024**3:.3f}/{sum(r.size for r in records) / 1024**3:.3f}")
    print("Audit passed")


def progress(config: dict) -> None:
    records = read_manifest(norm_path(config["manifest_path"]))
    gcs_client = storage_client(config)
    prefix = config["gcs"]["prefix"].strip("/") + "/"
    uploaded_files = 0
    uploaded_bytes = 0
    for blob in gcs_client.list_blobs(config["gcs"]["bucket"], prefix=prefix):
        uploaded_files += 1
        uploaded_bytes += int(blob.size or 0)
    total_bytes = sum(r.size for r in records)
    print(f"Remote prefix: gs://{config['gcs']['bucket']}/{prefix}")
    print(f"Remote files: {uploaded_files}/{len(records)} ({(uploaded_files / len(records) * 100) if records else 0:.2f}%)")
    print(f"Remote GiB: {uploaded_bytes / 1024**3:.3f}/{total_bytes / 1024**3:.3f}")


def checkpoint_existing(config: dict) -> None:
    parquet_root = norm_path(config["parquet_root"])
    table_count = 0
    record_count = 0
    for table_root in sorted(p for p in parquet_root.iterdir() if p.is_dir()):
        table = table_root.name
        records: list[ParquetRecord] = []
        for parquet_file in sorted(table_root.glob("partition_month=*/*.parquet")):
            partition_name = parquet_file.parent.name
            if not partition_name.startswith("partition_month="):
                continue
            partition_month = partition_name.removeprefix("partition_month=")
            stat = parquet_file.stat()
            try:
                rows = pq.read_metadata(parquet_file).num_rows
            except Exception:
                continue
            records.append(
                ParquetRecord(
                    target_table=table,
                    partition_month=partition_month,
                    local_path=str(parquet_file),
                    gcs_uri=gcs_uri(config, parquet_file),
                    size=stat.st_size,
                    rows=rows,
                    source_count=0,
                    fingerprint=fingerprint(parquet_file, stat.st_size, stat.st_mtime_ns),
                )
            )
        if records:
            validate_records(config, records, local=True)
            write_table_checkpoint(config, table, records)
            table_count += 1
            record_count += len(records)
    print(f"checkpointed tables={table_count} parquet_files={record_count}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare month-partitioned Parquet files and upload them to GCS.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("sync-raw", "build", "upload", "audit", "progress", "checkpoint-existing"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="data_transfer/parquet_config.yaml")
    sub.choices["sync-raw"].add_argument("--dry-run", action="store_true")
    sub.choices["sync-raw"].add_argument("--limit", type=int)
    sub.choices["build"].add_argument("--limit", type=int)
    sub.choices["upload"].add_argument("--dry-run", action="store_true")
    sub.choices["audit"].add_argument("--remote", action="store_true")
    args = parser.parse_args()
    config = load_config(Path(args.config))
    if args.command == "sync-raw":
        sync_raw(config, dry_run=args.dry_run, limit=args.limit)
        return 0
    if args.command == "build":
        records = build(config, limit=args.limit)
        manifest_summary(records)
        return 0
    if args.command == "upload":
        upload(config, dry_run=args.dry_run)
        return 0
    if args.command == "audit":
        audit(config, remote=args.remote)
        return 0
    if args.command == "progress":
        progress(config)
        return 0
    if args.command == "checkpoint-existing":
        checkpoint_existing(config)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
