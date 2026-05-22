from __future__ import annotations

import argparse
import hashlib
import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

import yaml
from google.cloud import storage
from google.oauth2 import service_account


@dataclass(frozen=True)
class ManifestRecord:
    source_path: str
    relative_path: str
    gcs_uri: str
    size: int
    mtime_ns: int
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


def file_fingerprint(path: Path, size: int, mtime_ns: int) -> str:
    payload = f"{path}|{size}|{mtime_ns}".encode("utf-8", errors="surrogatepass")
    return hashlib.sha256(payload).hexdigest()


def iter_source_files(config: dict) -> Iterable[Path]:
    source_root = norm_path(config["source_root"])
    ignore_dirs = [norm_path(p) for p in config.get("ignore_dirs", [])]
    include_extensions = {e.lower() for e in config.get("upload", {}).get("include_extensions", [])}
    exclude_extensions = {e.lower() for e in config.get("upload", {}).get("exclude_extensions", [])}

    for root, dirs, files in os.walk(source_root):
        root_path = Path(root).resolve()
        dirs[:] = [
            d for d in dirs
            if not any(is_relative_to((root_path / d).resolve(), ignored) for ignored in ignore_dirs)
        ]
        for file_name in files:
            path = (root_path / file_name).resolve()
            suffix = path.suffix.lower()
            if include_extensions and suffix not in include_extensions:
                continue
            if suffix in exclude_extensions:
                continue
            yield path


def gcs_object_name(config: dict, source_path: Path) -> tuple[str, str]:
    source_root = norm_path(config["source_root"])
    relative_path = source_path.relative_to(source_root).as_posix()
    prefix = config["gcs"]["prefix"].strip("/")
    return relative_path, f"{prefix}/{relative_path}"


def build_manifest(config: dict) -> list[ManifestRecord]:
    bucket = config["gcs"]["bucket"]
    records: list[ManifestRecord] = []
    for path in iter_source_files(config):
        stat = path.stat()
        relative_path, object_name = gcs_object_name(config, path)
        records.append(
            ManifestRecord(
                source_path=str(path),
                relative_path=relative_path,
                gcs_uri=f"gs://{bucket}/{object_name}",
                size=stat.st_size,
                mtime_ns=stat.st_mtime_ns,
                fingerprint=file_fingerprint(path, stat.st_size, stat.st_mtime_ns),
            )
        )
    return records


def write_manifest(path: Path, records: Iterable[ManifestRecord]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def read_manifest(path: Path) -> list[ManifestRecord]:
    records = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                records.append(ManifestRecord(**json.loads(line)))
    return records


def load_client(config: dict) -> storage.Client:
    credentials_cfg = config.get("credentials", {})
    mode = credentials_cfg.get("mode", "service_account_json")
    if mode == "service_account_json":
        service_account_path = Path(credentials_cfg["service_account_json"])
        if not service_account_path.exists():
            raise FileNotFoundError(
                f"Service account JSON not found: {service_account_path}. "
                "An API key is not enough to upload to a private GCS bucket."
            )
        credentials = service_account.Credentials.from_service_account_file(str(service_account_path))
        return storage.Client(credentials=credentials, project=credentials.project_id)
    if mode == "application_default_credentials":
        project_id = config.get("gcs", {}).get("project_id")
        return storage.Client(project=project_id)
    raise ValueError(f"Unsupported credential mode: {mode}")


def verify_access(config: dict) -> None:
    client = load_client(config)
    bucket_name = config["gcs"]["bucket"]
    bucket = client.bucket(bucket_name)
    if not bucket.exists(client):
        raise RuntimeError(f"Bucket does not exist or is not accessible: gs://{bucket_name}")

    probe_name = f"{config['gcs']['prefix'].strip('/')}/_transfer_checks/permission_probe.txt"
    blob = bucket.blob(probe_name)
    payload = f"permission check {utc_now()}\n".encode("utf-8")
    blob.upload_from_string(payload, content_type="text/plain")
    blob.reload()
    if int(blob.size or 0) != len(payload):
        raise RuntimeError("Permission probe uploaded but size verification failed")
    blob.delete()
    print(f"Verified read/write/delete access to gs://{bucket_name}/{config['gcs']['prefix'].strip('/')}/")


def upload(config: dict, dry_run: bool) -> None:
    manifest_path = Path(config["manifest_path"])
    records = read_manifest(manifest_path) if manifest_path.exists() else build_manifest(config)
    if dry_run:
        total_size = sum(r.size for r in records)
        print(f"Dry run: {len(records)} files, {total_size} bytes")
        for record in records[:20]:
            print(f"{record.source_path} -> {record.gcs_uri}")
        if len(records) > 20:
            print(f"... {len(records) - 20} more")
        return

    client = load_client(config)
    bucket = client.bucket(config["gcs"]["bucket"])
    skip_size_match = config.get("upload", {}).get("skip_if_remote_size_matches", True)
    verify_after = config.get("upload", {}).get("verify_after_upload", True)
    updated: list[ManifestRecord] = []

    for idx, record in enumerate(records, start=1):
        source = Path(record.source_path)
        _, object_name = gcs_object_name(config, source)
        blob = bucket.blob(object_name)
        try:
            if skip_size_match and blob.exists(client):
                blob.reload()
                if int(blob.size or -1) == record.size:
                    updated.append(ManifestRecord(**{**asdict(record), "status": "skipped", "uploaded_at": utc_now()}))
                    print(f"[{idx}/{len(records)}] skipped {record.relative_path}")
                    continue

            blob.upload_from_filename(str(source))
            if verify_after:
                blob.reload()
                if int(blob.size or -1) != record.size:
                    raise RuntimeError(f"Remote size mismatch: {blob.size} != {record.size}")
            updated.append(ManifestRecord(**{**asdict(record), "status": "uploaded", "uploaded_at": utc_now(), "error": None}))
            print(f"[{idx}/{len(records)}] uploaded {record.relative_path}")
        except Exception as exc:
            updated.append(ManifestRecord(**{**asdict(record), "status": "failed", "error": str(exc)}))
            print(f"[{idx}/{len(records)}] failed {record.relative_path}: {exc}")
            write_manifest(manifest_path, updated + records[idx:])
            raise

    write_manifest(manifest_path, updated)


def progress(config: dict) -> None:
    manifest_path = Path(config["manifest_path"])
    records = read_manifest(manifest_path) if manifest_path.exists() else build_manifest(config)
    total_files = len(records)
    total_bytes = sum(r.size for r in records)

    client = load_client(config)
    bucket_name = config["gcs"]["bucket"]
    prefix = config["gcs"]["prefix"].strip("/") + "/"
    uploaded_files = 0
    uploaded_bytes = 0
    for blob in client.list_blobs(bucket_name, prefix=prefix):
        if "/_transfer_checks/" in blob.name:
            continue
        uploaded_files += 1
        uploaded_bytes += int(blob.size or 0)

    pct_files = (uploaded_files / total_files * 100) if total_files else 0
    pct_bytes = (uploaded_bytes / total_bytes * 100) if total_bytes else 0
    print(f"Remote prefix: gs://{bucket_name}/{prefix}")
    print(f"Files: {uploaded_files}/{total_files} ({pct_files:.2f}%)")
    print(f"Bytes: {uploaded_bytes}/{total_bytes} ({pct_bytes:.2f}%)")
    print(f"GiB: {uploaded_bytes / 1024**3:.3f}/{total_bytes / 1024**3:.3f}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Transfer local A-share raw files to GCS.")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("manifest", "verify", "upload", "progress"):
        p = sub.add_parser(name)
        p.add_argument("--config", default="data_transfer/config.yaml")
    sub.choices["upload"].add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    config = load_config(Path(args.config))

    if args.command == "manifest":
        records = build_manifest(config)
        write_manifest(Path(config["manifest_path"]), records)
        print(f"Wrote manifest: {config['manifest_path']}")
        print(f"Files: {len(records)}")
        print(f"Bytes: {sum(r.size for r in records)}")
        return 0
    if args.command == "verify":
        verify_access(config)
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
