from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from google.cloud import storage


@dataclass(frozen=True)
class StandardizedRecord:
    source_path: str
    source_entry: str | None
    target_table: str
    partition_month: str
    local_path: str
    gcs_uri: str
    size: int
    fingerprint: str
    status: str = "pending"
    uploaded_at: str | None = None
    error: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_config(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def norm_path(value: str) -> Path:
    return Path(value.replace("/", os.sep)).resolve()


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def slug(value: str, max_len: int = 160) -> str:
    value = value.replace("\\", "/")
    value = re.sub(r"[^0-9A-Za-z._=\-]+", "_", value)
    value = re.sub(r"_+", "_", value).strip("._")
    return (value or "item")[:max_len]


def fingerprint(*parts: object) -> str:
    payload = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(payload.encode("utf-8", errors="surrogatepass")).hexdigest()


def month_from_text(*parts: str | None) -> str:
    text = " ".join(p or "" for p in parts)
    match = re.search(r"(19|20)\d{2}[-_/]?(0[1-9]|1[0-2])[-_/]?\d{0,2}", text)
    if match:
        digits = re.sub(r"\D", "", match.group(0))
        return digits[:6]
    return "unknown"


def classify(path: Path, entry: str | None = None) -> str:
    text = f"{path.as_posix()}/{entry or ''}"
    name = path.name

    if "daily" in text or "日线" in text or "日_kline" in text or "日k" in text or "_日" in text:
        tf = "1d"
    elif "weekly" in text or "周线" in text or "周_kline" in text or "周k" in text or "_周" in text:
        tf = "1w"
    elif "monthly" in text or "月线" in text or "月_kline" in text or "月k" in text or "_月" in text:
        tf = "1mo"
    else:
        tf = None

    if "A股数据_zip" in text and "指数" not in text and tf:
        return f"fact_equity_kline_{tf}"
    if "基金数据" in text and tf:
        return f"fact_fund_kline_{tf}"
    if "指数" in text and tf:
        return f"fact_index_kline_{tf}"
    if "行业概念板块" in text and ("行情" in text or "历史行情" in text or "kline" in text or "日k" in text or "周k" in text or "月k" in text):
        return f"fact_board_kline_{tf or '1d'}"
    if "复权因子" in text:
        return "fact_adjust_factor"
    if "涨跌停价格" in text:
        return "fact_limit_price_1d"
    if "停复牌" in text:
        return "fact_suspend_1d"
    if "ST股票列表" in text:
        return "fact_st_status_1d"
    if "板块成分" in text or "概念板块成分" in text:
        return "fact_board_component_1d"
    if "资产负债表" in text:
        return "fact_balance_sheet"
    if "利润表" in text:
        return "fact_income_statement"
    if "现金流量表" in text:
        return "fact_cash_flow_statement"
    if "财务指标" in text or "上市公司财务信息_季报_CSV" in text:
        return "fact_financial_indicator"
    if "业绩预告" in text:
        return "fact_earnings_forecast"
    if "业绩快报" in text:
        return "fact_earnings_express"
    if "分红送股" in text:
        return "fact_dividend"
    if "配股" in text:
        return "fact_rights_issue"
    if "股东人数" in text:
        return "fact_shareholder_count"
    if "前十大流通股东" in text:
        return "fact_top10_float_shareholders"
    if "前十大股东" in text:
        return "fact_top10_shareholders"
    if "主营业务构成" in text:
        return "fact_business_composition"
    if "财务审计意见" in text:
        return "fact_audit_opinion"
    if "财报披露计划" in text:
        return "fact_disclosure_schedule"
    if name == "交易日历.csv":
        return "dim_trade_calendar"
    if "股票曾用名" in text:
        return "dim_security_name_history"
    if name in {"股票列表.csv", "退市股票列表.csv", "退市股票列表.csv"} or "退市股票列表" in text:
        return "dim_security"
    if "指数列表" in text or "中证指数列表" in text:
        return "dim_index"
    if "板块信息" in text or "概念板块_东财" in text:
        return "dim_board"
    return "unmapped"


def iter_sources(config: dict):
    source_root = norm_path(config["source_root"])
    ignore_dirs = [norm_path(p) for p in config.get("ignore_dirs", [])]
    for root, dirs, files in os.walk(source_root):
        root_path = Path(root).resolve()
        dirs[:] = [
            d for d in dirs
            if not any(is_relative_to((root_path / d).resolve(), ignored) for ignored in ignore_dirs)
        ]
        for file_name in files:
            path = (root_path / file_name).resolve()
            suffix = path.suffix.lower()
            if suffix == ".csv":
                yield path, None, path.stat().st_size
            elif suffix == ".zip":
                try:
                    with zipfile.ZipFile(path) as zf:
                        for info in zf.infolist():
                            if not info.is_dir() and info.filename.lower().endswith(".csv"):
                                yield path, info.filename, info.file_size
                except zipfile.BadZipFile:
                    yield path, "__BAD_ZIP__", 0


def local_output_path(config: dict, source: Path, entry: str | None, table: str, partition_month: str) -> Path:
    source_root = norm_path(config["source_root"])
    relative = source.relative_to(source_root).as_posix()
    if entry:
        out_name = slug(entry)
    else:
        out_name = slug(source.name)
    if not out_name.lower().endswith(".csv"):
        out_name += ".csv"
    unique = fingerprint(relative, entry)[:12]
    return norm_path(config["standardized_root"]) / table / f"partition_month={partition_month}" / f"{unique}_{out_name}"


def validate_records(config: dict, records: list[StandardizedRecord]) -> None:
    bucket = config["gcs"]["bucket"]
    prefix = config["gcs"]["prefix"].strip("/")
    source_root = norm_path(config["source_root"])
    standardized_root = norm_path(config["standardized_root"])
    ignore_dirs = [norm_path(p) for p in config.get("ignore_dirs", [])]
    seen_gcs: set[str] = set()
    errors: list[str] = []

    for record in records:
        source = Path(record.source_path).resolve()
        local_path = Path(record.local_path).resolve()
        expected_prefix = f"gs://{bucket}/{prefix}/"

        if not is_relative_to(source, source_root):
            errors.append(f"source outside source_root: {source}")
        if any(is_relative_to(source, ignored) for ignored in ignore_dirs):
            errors.append(f"ignored source included: {source}")
        if not is_relative_to(local_path, standardized_root):
            errors.append(f"local output outside standardized_root: {local_path}")
        if not record.gcs_uri.startswith(expected_prefix):
            errors.append(f"wrong GCS prefix: {record.gcs_uri}")

        rel_gcs = record.gcs_uri.removeprefix(expected_prefix)
        parts = [p for p in rel_gcs.split("/") if p]
        if any(p.lower().endswith(".zip") for p in parts[:-1]):
            errors.append(f"zip-like directory segment in GCS path: {record.gcs_uri}")
        if not parts or not parts[-1].lower().endswith(".csv"):
            errors.append(f"non-CSV object name: {record.gcs_uri}")
        if record.gcs_uri in seen_gcs:
            errors.append(f"duplicate GCS object: {record.gcs_uri}")
        seen_gcs.add(record.gcs_uri)
        if record.target_table.startswith("fact_") and record.partition_month == "unknown":
            errors.append(f"fact table is not month-partitioned: {record.gcs_uri}")

    if errors:
        sample = "\n".join(f"- {err}" for err in errors[:30])
        extra = "" if len(errors) <= 30 else f"\n... {len(errors) - 30} more"
        raise RuntimeError(f"Manifest validation failed with {len(errors)} error(s):\n{sample}{extra}")


def build_manifest(config: dict) -> list[StandardizedRecord]:
    bucket = config["gcs"]["bucket"]
    prefix = config["gcs"]["prefix"].strip("/")
    records: list[StandardizedRecord] = []
    for source, entry, size in iter_sources(config):
        if entry == "__BAD_ZIP__":
            table = "unmapped_bad_zip"
        else:
            table = classify(source, entry)
        partition_month = month_from_text(str(source), entry)
        local_path = local_output_path(config, source, entry, table, partition_month)
        rel_local = local_path.relative_to(norm_path(config["standardized_root"])).as_posix()
        gcs_uri = f"gs://{bucket}/{prefix}/{rel_local}"
        stat = source.stat()
        records.append(
            StandardizedRecord(
                source_path=str(source),
                source_entry=entry,
                target_table=table,
                partition_month=partition_month,
                local_path=str(local_path),
                gcs_uri=gcs_uri,
                size=size,
                fingerprint=fingerprint(source, entry, stat.st_size, stat.st_mtime_ns, size),
            )
        )
    return records


def write_manifest(path: Path, records: list[StandardizedRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f"{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
    os.replace(temp_path, path)


def read_manifest(path: Path) -> list[StandardizedRecord]:
    records = []
    if not path.exists():
        return records
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                records.append(StandardizedRecord(**json.loads(line)))
    return records


def ensure_local_file(record: StandardizedRecord) -> None:
    source = Path(record.source_path)
    target = Path(record.local_path)
    if target.exists() and target.stat().st_size == record.size:
        return
    target.parent.mkdir(parents=True, exist_ok=True)
    if record.source_entry:
        with zipfile.ZipFile(source) as zf:
            with zf.open(record.source_entry) as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst, length=1024 * 1024)
    else:
        shutil.copy2(source, target)


def client(config: dict) -> storage.Client:
    return storage.Client(project=config["gcs"].get("project_id"))


def gcs_object_name(config: dict, local_path: Path) -> str:
    rel = local_path.relative_to(norm_path(config["standardized_root"])).as_posix()
    return f"{config['gcs']['prefix'].strip('/')}/{rel}"


def manifest_summary(records: list[StandardizedRecord]) -> None:
    total = sum(r.size for r in records)
    tables: dict[str, int] = {}
    for r in records:
        tables[r.target_table] = tables.get(r.target_table, 0) + 1
    print(f"Records: {len(records)}")
    print(f"Bytes: {total}")
    for table, count in sorted(tables.items()):
        print(f"{table}: {count}")


def upload(config: dict, dry_run: bool = False) -> None:
    manifest_path = norm_path(config["manifest_path"])
    records = read_manifest(manifest_path)
    if not records:
        records = build_manifest(config)
        write_manifest(manifest_path, records)
    validate_records(config, records)

    if dry_run:
        manifest_summary(records)
        for record in records[:20]:
            print(f"{record.source_path} :: {record.source_entry or ''} -> {record.gcs_uri}")
        return

    gcs_client = client(config)
    bucket = gcs_client.bucket(config["gcs"]["bucket"])
    skip_size_match = config.get("upload", {}).get("skip_if_remote_size_matches", True)
    verify_after_upload = config.get("upload", {}).get("verify_after_upload", True)
    keep_local_files = config.get("upload", {}).get("keep_local_files", True)

    updated: list[StandardizedRecord] = []
    for idx, record in enumerate(records, start=1):
        try:
            local_path = Path(record.local_path)
            ensure_local_file(record)
            object_name = gcs_object_name(config, local_path)
            blob = bucket.blob(object_name)

            if skip_size_match and blob.exists(gcs_client):
                blob.reload()
                if int(blob.size or -1) == local_path.stat().st_size:
                    updated.append(StandardizedRecord(**{**asdict(record), "status": "skipped", "uploaded_at": utc_now(), "error": None}))
                    print(f"[{idx}/{len(records)}] skipped {record.target_table}/{local_path.name}", flush=True)
                    continue

            blob.upload_from_filename(str(local_path), content_type="text/csv")
            if verify_after_upload:
                blob.reload()
                if int(blob.size or -1) != local_path.stat().st_size:
                    raise RuntimeError(f"Remote size mismatch: {blob.size} != {local_path.stat().st_size}")

            if not keep_local_files:
                local_path.unlink(missing_ok=True)

            updated.append(StandardizedRecord(**{**asdict(record), "status": "uploaded", "uploaded_at": utc_now(), "error": None}))
            print(f"[{idx}/{len(records)}] uploaded {record.target_table}/{local_path.name}", flush=True)
        except Exception as exc:
            updated.append(StandardizedRecord(**{**asdict(record), "status": "failed", "error": str(exc)}))
            write_manifest(manifest_path, updated + records[idx:])
            print(f"[{idx}/{len(records)}] failed {record.source_path} :: {record.source_entry}: {exc}", flush=True)
            raise

        if idx % 100 == 0:
            write_manifest(manifest_path, updated + records[idx:])
    write_manifest(manifest_path, updated)


def progress(config: dict) -> None:
    manifest_path = norm_path(config["manifest_path"])
    records = read_manifest(manifest_path)
    if not records:
        records = build_manifest(config)
    total_files = len(records)
    total_bytes = sum(r.size for r in records)

    gcs_client = client(config)
    bucket_name = config["gcs"]["bucket"]
    prefix = config["gcs"]["prefix"].strip("/") + "/"
    uploaded_files = 0
    uploaded_bytes = 0
    for blob in gcs_client.list_blobs(bucket_name, prefix=prefix):
        uploaded_files += 1
        uploaded_bytes += int(blob.size or 0)

    local_files = sum(1 for r in records if Path(r.local_path).exists())
    local_bytes = sum(Path(r.local_path).stat().st_size for r in records if Path(r.local_path).exists())
    print(f"Remote prefix: gs://{bucket_name}/{prefix}")
    print(f"Remote files: {uploaded_files}/{total_files} ({(uploaded_files / total_files * 100) if total_files else 0:.2f}%)")
    print(f"Remote GiB: {uploaded_bytes / 1024**3:.3f}/{total_bytes / 1024**3:.3f}")
    print(f"Local standardized files: {local_files}/{total_files}")
    print(f"Local standardized GiB: {local_bytes / 1024**3:.3f}/{total_bytes / 1024**3:.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare standardized CSV files and upload them to GCS.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("manifest", "upload", "progress"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="data_transfer/standardized_config.yaml")
    sub.choices["upload"].add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = load_config(Path(args.config))

    if args.command == "manifest":
        records = build_manifest(config)
        validate_records(config, records)
        write_manifest(norm_path(config["manifest_path"]), records)
        manifest_summary(records)
        return 0
    if args.command == "upload":
        upload(config, dry_run=args.dry_run)
        return 0
    if args.command == "progress":
        progress(config)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
