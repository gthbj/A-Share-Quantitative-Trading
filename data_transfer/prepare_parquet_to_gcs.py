from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


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


def source_bytes(source: Path, entry: str | None) -> bytes | Path:
    if entry:
        with zipfile.ZipFile(source) as zf:
            return zf.read(entry)
    return source


def read_csv_chunks(source: Path, entry: str | None, chunksize: int):
    payload = source_bytes(source, entry)
    encodings = ("utf-8-sig", "utf-8", "gb18030")
    last_error: Exception | None = None
    for encoding in encodings:
        try:
            handle = io.BytesIO(payload) if isinstance(payload, bytes) else payload
            yield from pd.read_csv(
                handle,
                dtype=str,
                chunksize=chunksize,
                encoding=encoding,
                on_bad_lines="skip",
                keep_default_na=False,
            )
            return
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error


def read_csv_header(source: Path, entry: str | None) -> list[str]:
    payload = source_bytes(source, entry)
    last_error: Exception | None = None
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            handle = io.BytesIO(payload) if isinstance(payload, bytes) else payload
            frame = pd.read_csv(handle, dtype=str, nrows=0, encoding=encoding, on_bad_lines="skip")
            return [str(c).strip().lstrip("\ufeff") for c in frame.columns]
        except UnicodeDecodeError as exc:
            last_error = exc
            continue
    if last_error:
        raise last_error
    return []


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
    date_col = detect_date_column(list(frame.columns), table)
    if date_col:
        frame["date"] = frame[date_col].map(normalize_date)
        frame = frame[frame["date"].notna() & (frame["date"] != "")]
    elif table.startswith(FACT_TABLES_WITH_REQUIRED_DATE_PREFIXES):
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
    def __init__(self, path: Path, schema: pa.Schema):
        self.path = path
        self.temp_path = path.with_suffix(".parquet.tmp")
        self.rows = 0
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.writer = pq.ParquetWriter(self.temp_path, schema=schema, compression="zstd")

    def write(self, frame: pd.DataFrame, schema: pa.Schema) -> None:
        table = pa.Table.from_pandas(frame, schema=schema, preserve_index=False)
        self.writer.write_table(table)
        self.rows += len(frame)

    def close(self) -> None:
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
    for column in columns:
        if column not in frame.columns:
            frame[column] = pd.NA
    frame = frame[columns]
    for column in frame.columns:
        frame[column] = frame[column].astype("string")
    return frame


def build(config: dict, limit: int | None = None) -> list[ParquetRecord]:
    records: list[ParquetRecord] = []
    chunksize = int(config.get("build", {}).get("chunksize", 50000))
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
    if parquet_root.exists():
        import shutil

        shutil.rmtree(parquet_root)

    for table, sources in sorted(grouped_sources.items()):
        columns = schema_columns_for_sources(sources)
        schema = pa.schema([pa.field(column, pa.string()) for column in columns])
        writers: dict[str, PartitionWriter] = {}
        source_count_by_month: dict[str, int] = {}
        print(f"building {table}: sources={len(sources)} columns={len(columns)}", flush=True)

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
                        part_path = parquet_root / table / f"partition_month={partition_month}" / f"part-{source_hash[:8]}-{len(writers):05d}.parquet"
                        writers[partition_month] = PartitionWriter(part_path, schema)
                    aligned = align_to_schema(month_frame, columns)
                    writers[partition_month].write(aligned, schema)
                    source_months.add(partition_month)
            if table.startswith("fact_") and source_missing_date and not source_has_usable_rows:
                skipped_no_date[table] = skipped_no_date.get(table, 0) + 1
            for partition_month in source_months:
                source_count_by_month[partition_month] = source_count_by_month.get(partition_month, 0) + 1
            if source_idx % 1000 == 0:
                print(f"  {table}: processed_sources={source_idx}/{len(sources)} open_partitions={len(writers)}", flush=True)

        for partition_month, writer in sorted(writers.items()):
            writer.close()
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

    if skipped_no_date:
        detail = ", ".join(f"{table}:{count}" for table, count in sorted(skipped_no_date.items()))
        raise RuntimeError(f"Fact CSV chunks without a usable date column: {detail}")
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

    gcs_client = storage.Client(project=config["gcs"].get("project_id"))
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
        gcs_client = storage.Client(project=config["gcs"].get("project_id"))
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
    gcs_client = storage.Client(project=config["gcs"].get("project_id"))
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare month-partitioned Parquet files and upload them to GCS.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "upload", "audit", "progress"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="data_transfer/parquet_config.yaml")
    sub.choices["build"].add_argument("--limit", type=int)
    sub.choices["upload"].add_argument("--dry-run", action="store_true")
    sub.choices["audit"].add_argument("--remote", action="store_true")
    args = parser.parse_args()
    config = load_config(Path(args.config))
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
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
