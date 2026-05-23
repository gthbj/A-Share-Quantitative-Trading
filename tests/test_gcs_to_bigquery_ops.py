from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

import gcs_to_bigquery.pipeline as pipeline


def test_load_config_sets_persistent_manifest_default(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text('project_id: "data-aquarium"\n', encoding="utf-8")

    config = pipeline.load_config(config_path)

    assert config["manifest_path"] == pipeline.DEFAULT_MANIFEST_PATH


def test_manifest_path_uses_home(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("HOME", str(tmp_path))

    result = pipeline.manifest_path({"manifest_path": pipeline.DEFAULT_MANIFEST_PATH})

    assert result == tmp_path / ".local" / "state" / "ashare" / "ods_pipeline_manifest.jsonl"


def test_manifest_path_uses_userprofile_when_home_missing(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    result = pipeline.manifest_path({"manifest_path": pipeline.DEFAULT_MANIFEST_PATH})

    assert result == tmp_path / "AppData" / "Local" / "ashare" / "ods_pipeline_manifest.jsonl"


def test_manifest_path_rewrites_custom_home_when_home_missing(monkeypatch, tmp_path: Path):
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    result = pipeline.manifest_path({"manifest_path": "${HOME}/custom/manifest.jsonl"})

    assert result == tmp_path / "custom" / "manifest.jsonl"


def test_ensure_manifest_parent_creates_directory(tmp_path: Path):
    target = tmp_path / "state" / "ashare" / "manifest.jsonl"

    pipeline.ensure_manifest_parent({"manifest_path": str(target)})

    assert target.parent.is_dir()


def test_allow_unconfigured_tables_defaults_false():
    assert pipeline.allow_unconfigured_tables({}) is False


@dataclass
class FakeBlob:
    name: str
    size: int = 10
    generation: str = "1"


def test_parse_object_rejects_unconfigured_table_by_default():
    config = {
        "gcs": {"bucket": "data-aquarium", "prefix": "a-share/standardized_parquet"},
        "tables": {},
        "defaults": {},
    }
    blob = FakeBlob("a-share/standardized_parquet/fact_unknown/partition_month=202401/part.parquet")

    record = pipeline.parse_object(config, blob, "batch")

    assert record.status == "invalid"
    assert record.error_message == "Target table is not configured: fact_unknown"


def test_gcloud_access_token_uses_configured_timeout(monkeypatch):
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args[0], 0, stdout="token\n", stderr="")

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)

    token = pipeline.gcloud_access_token({"auth": {"gcloud_token_timeout_seconds": 7}})

    assert token == "token"
    assert calls[0][1]["timeout"] == 7


def test_gcloud_access_token_rejects_empty_token(monkeypatch):
    def fake_run(*args, **kwargs):
        return subprocess.CompletedProcess(args[0], 0, stdout="\n", stderr="")

    monkeypatch.setattr(pipeline.subprocess, "run", fake_run)

    with pytest.raises(RuntimeError, match="did not return"):
        pipeline.gcloud_access_token({})


def test_gcloud_credentials_refreshes_token(monkeypatch):
    pytest.importorskip("google.auth.credentials")
    tokens = iter(["first-token", "second-token"])
    monkeypatch.setattr(pipeline, "gcloud_access_token", lambda config: next(tokens))

    credentials = pipeline.gcloud_credentials({"auth": {"gcloud_token_timeout_seconds": 1}})
    assert credentials.token == "first-token"

    credentials.refresh(None)
    assert credentials.token == "second-token"


def test_bq_client_uses_adc_by_default(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeBigQuery:
        Client = FakeClient

    monkeypatch.setattr(pipeline, "require_bigquery", lambda: FakeBigQuery)

    client = pipeline.bq_client({"project_id": "data-aquarium", "location": "asia-east2"})

    assert client.kwargs == {"project": "data-aquarium", "location": "asia-east2"}


def test_bq_client_uses_gcloud_fallback_when_enabled(monkeypatch):
    class FakeClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    class FakeBigQuery:
        Client = FakeClient

    monkeypatch.setattr(pipeline, "require_bigquery", lambda: FakeBigQuery)
    monkeypatch.setattr(pipeline, "gcloud_credentials", lambda config: "credentials")

    client = pipeline.bq_client({
        "project_id": "data-aquarium",
        "location": "asia-east2",
        "auth": {"use_gcloud_access_token": True},
    })

    assert client.kwargs["credentials"] == "credentials"


def test_load_does_not_retry_failed_by_default(tmp_path: Path, capsys):
    manifest = tmp_path / "manifest.jsonl"
    records = [
        pipeline.LoadRecord("b", "gs://bucket/pending.parquet", "table_a", 202401, 1, "1", "PARQUET", "staging_only"),
        pipeline.LoadRecord("b", "gs://bucket/failed.parquet", "table_a", 202401, 1, "2", "PARQUET", "staging_only", status="failed"),
    ]
    pipeline.write_manifest(manifest, records)

    pipeline.load({"manifest_path": str(manifest), "defaults": {}}, dry_run=True)

    output = capsys.readouterr().out
    assert "Records: 1" in output
    assert "pending.parquet" in output
    assert "failed.parquet" not in output


def test_load_retries_failed_when_cli_flag_sets_retry(tmp_path: Path, capsys):
    manifest = tmp_path / "manifest.jsonl"
    records = [
        pipeline.LoadRecord("b", "gs://bucket/pending.parquet", "table_a", 202401, 1, "1", "PARQUET", "staging_only"),
        pipeline.LoadRecord("b", "gs://bucket/failed.parquet", "table_a", 202401, 1, "2", "PARQUET", "staging_only", status="failed"),
    ]
    pipeline.write_manifest(manifest, records)

    pipeline.load({"manifest_path": str(manifest), "defaults": {}}, dry_run=True, retry_failed=True)

    output = capsys.readouterr().out
    assert "Records: 2" in output
    assert "pending.parquet" in output
    assert "failed.parquet" in output


def test_single_file_load_retries_failed_when_cli_flag_sets_retry(monkeypatch, tmp_path: Path):
    manifest = tmp_path / "manifest.jsonl"
    records = [
        pipeline.LoadRecord("b", "gs://bucket/failed.parquet", "table_a", 202401, 1, "2", "PARQUET", "staging_only", status="failed"),
    ]
    pipeline.write_manifest(manifest, records)
    monkeypatch.setattr(pipeline, "bq_client", lambda config: object())

    def fake_load_to_staging(config, client, record):
        return pipeline.LoadRecord(
            **{
                **record.__dict__,
                "status": "loaded",
                "bq_job_id": "job",
                "started_at": "2026-05-24T00:00:00+00:00",
                "finished_at": "2026-05-24T00:00:01+00:00",
                "error_message": None,
            }
        )

    monkeypatch.setattr(pipeline, "load_to_staging", fake_load_to_staging)

    pipeline.load(
        {"manifest_path": str(manifest), "defaults": {"load_strategy": "single_file"}},
        dry_run=False,
        retry_failed=True,
    )

    [record] = pipeline.read_manifest(manifest)
    assert record.status == "loaded"
    assert record.bq_job_id == "job"
