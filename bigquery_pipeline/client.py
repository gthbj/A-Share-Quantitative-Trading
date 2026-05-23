from __future__ import annotations

import os
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

import yaml


REQUIREMENTS_INSTALL_HINT = "Install dependencies with: python -m pip install -r requirements.txt"
DEFAULT_CONFIG_PATH = Path("bigquery_pipeline/config.yaml")


def load_config(path: Path | str = DEFAULT_CONFIG_PATH) -> dict:
    config_path = Path(path)
    config = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    config.setdefault("project_id", "data-aquarium")
    config.setdefault("location", "asia-east2")
    config.setdefault("dataset", "ashare")
    config.setdefault("auth", {})
    config.setdefault("gcs", {})
    config.setdefault("table_prefixes", {"ods": "ods_", "dwd": "dwd_", "dws": "dws_", "ads": "ads_"})
    config.setdefault("financial_indicator", {})
    return config


def require_bigquery():
    try:
        from google.cloud import bigquery
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency: google-cloud-bigquery. {REQUIREMENTS_INSTALL_HINT}") from exc
    return bigquery


def require_storage():
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise RuntimeError(f"Missing dependency: google-cloud-storage. {REQUIREMENTS_INSTALL_HINT}") from exc
    return storage


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
        raise RuntimeError(f"Missing dependency: google-auth. {REQUIREMENTS_INSTALL_HINT}") from exc

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


def table_prefix(config: dict, layer: str) -> str:
    return config.get("table_prefixes", {}).get(layer, f"{layer}_")


def dwd_table_name(config: dict, table_key: str) -> str:
    return table_key if table_key.startswith(table_prefix(config, "dwd")) else f"{table_prefix(config, 'dwd')}{table_key}"


def dws_table_name(config: dict, table_key: str) -> str:
    return table_key if table_key.startswith(table_prefix(config, "dws")) else f"{table_prefix(config, 'dws')}{table_key}"


def ads_table_name(config: dict, table_key: str) -> str:
    return table_key if table_key.startswith(table_prefix(config, "ads")) else f"{table_prefix(config, 'ads')}{table_key}"

