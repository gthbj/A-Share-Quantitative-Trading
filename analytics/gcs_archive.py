"""Archive backtest output directories to Google Cloud Storage."""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable


TRUTHY = {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class GCSArchiveResult:
    bucket: str
    prefix: str
    uri: str
    uploaded_files: list[str]
    manifest_path: Path


def parse_gcs_uri(uri: str) -> tuple[str, str]:
    if not uri.startswith("gs://"):
        raise ValueError(f"GCS URI must start with gs://: {uri}")
    without_scheme = uri[5:]
    bucket, _, prefix = without_scheme.partition("/")
    if not bucket:
        raise ValueError(f"GCS URI must include a bucket: {uri}")
    return bucket, prefix.strip("/")


def sanitize_path_part(value: str) -> str:
    sanitized = re.sub(r"[^A-Za-z0-9._=-]+", "_", value.strip())
    sanitized = sanitized.strip("._")
    return sanitized or "unknown"


def archive_enabled(config: dict) -> bool:
    archive_cfg = gcs_archive_config(config)
    return bool(archive_cfg.get("enabled", False))


def gcs_archive_config(config: dict) -> dict:
    return ((config or {}).get("output", {}) or {}).get("gcs_archive", {}) or {}


def apply_gcs_archive_uri(config: dict, uri: str) -> None:
    bucket, prefix = parse_gcs_uri(uri)
    archive_cfg = config.setdefault("output", {}).setdefault("gcs_archive", {})
    archive_cfg["enabled"] = True
    archive_cfg["bucket"] = bucket
    archive_cfg["prefix"] = prefix


def _use_gcloud_access_token(config: dict) -> bool:
    env_value = os.environ.get("ASHARE_USE_GCLOUD_ACCESS_TOKEN", "").strip().lower()
    if env_value in TRUTHY:
        return True
    return bool((config.get("auth", {}) or {}).get("use_gcloud_access_token", False))


def _gcloud_access_token(config: dict) -> str:
    timeout = int((config.get("auth", {}) or {}).get("gcloud_token_timeout_seconds", 30))
    result = subprocess.run(
        ["gcloud", "auth", "print-access-token", "--quiet"],
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    token = result.stdout.strip()
    if not token:
        raise RuntimeError("gcloud did not return an access token")
    return token


def _gcloud_credentials(config: dict):
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
            self.token = _gcloud_access_token(self._credential_config)
            self.expiry = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(minutes=50)

    return GcloudAccessTokenCredentials(config)


def storage_client(config: dict):
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise RuntimeError("Missing dependency: google-cloud-storage") from exc

    archive_cfg = gcs_archive_config(config)
    project_id = archive_cfg.get("project_id") or (config.get("data", {}) or {}).get("bigquery", {}).get("project_id")
    kwargs = {"project": project_id} if project_id else {}
    if _use_gcloud_access_token(config):
        kwargs["credentials"] = _gcloud_credentials(config)
    return storage.Client(**kwargs)


def iter_output_files(output_dir: Path) -> Iterable[Path]:
    for path in sorted(output_dir.rglob("*")):
        if path.is_file() and path.name != ".DS_Store":
            yield path


def build_destination_prefix(config: dict, strategy_key: str, run_label: str) -> tuple[str, str]:
    archive_cfg = gcs_archive_config(config)
    if "uri" in archive_cfg and archive_cfg["uri"]:
        bucket, base_prefix = parse_gcs_uri(str(archive_cfg["uri"]))
    else:
        bucket = str(archive_cfg.get("bucket") or "")
        base_prefix = str(archive_cfg.get("prefix") or "").strip("/")
    if not bucket:
        raise ValueError("Missing output.gcs_archive.bucket")
    parts = [base_prefix, sanitize_path_part(strategy_key), sanitize_path_part(run_label)]
    prefix = "/".join(part.strip("/") for part in parts if part)
    return bucket, prefix


def write_archive_manifest(
    output_dir: Path,
    *,
    destination_uri: str,
    uploaded_relative_paths: list[str],
    strategy_key: str,
    strategy_class_path: str,
    run_label: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
    frequency: str,
    benchmark: str,
) -> Path:
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "local_output_dir": str(output_dir.resolve()),
        "destination_uri": destination_uri,
        "strategy_key": strategy_key,
        "strategy_class_path": strategy_class_path,
        "run_label": run_label,
        "start_date": start_date,
        "end_date": end_date,
        "initial_capital": initial_capital,
        "frequency": frequency,
        "benchmark": benchmark,
        "files": uploaded_relative_paths,
    }
    manifest_path = output_dir / "gcs_archive_manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest_path


def archive_backtest_output(
    output_dir: Path,
    config: dict,
    *,
    strategy_key: str,
    strategy_class_path: str,
    run_label: str,
    start_date: str,
    end_date: str,
    initial_capital: float,
    frequency: str,
    benchmark: str,
    client_factory: Callable[[dict], object] | None = None,
) -> GCSArchiveResult | None:
    if not archive_enabled(config):
        return None

    output_dir = Path(output_dir)
    bucket_name, object_prefix = build_destination_prefix(config, strategy_key, run_label)
    destination_uri = f"gs://{bucket_name}/{object_prefix}/"

    relative_paths = [path.relative_to(output_dir).as_posix() for path in iter_output_files(output_dir)]
    manifest_path = write_archive_manifest(
        output_dir,
        destination_uri=destination_uri,
        uploaded_relative_paths=relative_paths + ["gcs_archive_manifest.json"],
        strategy_key=strategy_key,
        strategy_class_path=strategy_class_path,
        run_label=run_label,
        start_date=start_date,
        end_date=end_date,
        initial_capital=initial_capital,
        frequency=frequency,
        benchmark=benchmark,
    )

    client = (client_factory or storage_client)(config)
    bucket = client.bucket(bucket_name)
    uploaded_files: list[str] = []
    for local_path in iter_output_files(output_dir):
        rel = local_path.relative_to(output_dir).as_posix()
        object_name = f"{object_prefix}/{rel}"
        blob = bucket.blob(object_name)
        blob.upload_from_filename(str(local_path))
        uploaded_files.append(rel)

    return GCSArchiveResult(
        bucket=bucket_name,
        prefix=object_prefix,
        uri=destination_uri,
        uploaded_files=uploaded_files,
        manifest_path=manifest_path,
    )
