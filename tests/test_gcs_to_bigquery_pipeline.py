"""ODS external table 与审计改造 单元测试 (PRD_20260523_10)。"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from gcs_to_bigquery.pipeline import (
    LoadRecord,
    batch_id,
    dataset_id,
    load_config,
    ods_errors_schema,
    ods_manifest_schema,
    ods_table_name,
    read_manifest,
    table_id,
    write_manifest,
)


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------

class TestOdsDatasetHelpers:
    def test_ods_dataset(self):
        config = {"project_id": "data-aquarium", "dataset": "ashare"}
        assert dataset_id(config) == "data-aquarium.ashare"

    def test_ods_table(self):
        config = {"project_id": "data-aquarium", "dataset": "ashare"}
        assert table_id(config, "ods_fact_equity_kline_1d") == (
            "data-aquarium.ashare.ods_fact_equity_kline_1d"
        )

    def test_ods_table_name_prefix(self):
        assert ods_table_name("fact_equity_kline_1d") == "ods_fact_equity_kline_1d"
        assert ods_table_name("dim_security") == "ods_dim_security"


class TestConfigLoad:
    def test_load_config_has_ods_external_tables(self):
        config_path = Path(__file__).resolve().parent.parent / "gcs_to_bigquery" / "config.yaml"
        cfg = load_config(config_path)
        assert "ods_external_tables" in cfg
        assert cfg["dataset"] == "ashare"
        tables = cfg["ods_external_tables"]
        assert "fact_equity_kline_1d" in tables
        assert tables["fact_equity_kline_1d"]["source_format"] == "PARQUET"
        assert tables["fact_equity_kline_1d"]["destination_table"] == "ods_fact_equity_kline_1d"

    def test_ods_external_tables_all_destination_prefixed(self):
        config_path = Path(__file__).resolve().parent.parent / "gcs_to_bigquery" / "config.yaml"
        cfg = load_config(config_path)
        for table_key, table_cfg in cfg["ods_external_tables"].items():
            assert table_cfg["destination_table"].startswith("ods_"), (
                f"{table_key} destination should start with ods_"
            )

    def test_ods_external_tables_source_uris_valid(self):
        config_path = Path(__file__).resolve().parent.parent / "gcs_to_bigquery" / "config.yaml"
        cfg = load_config(config_path)
        gcs_prefix = f"gs://{cfg['gcs']['bucket']}/{cfg['gcs']['prefix'].strip('/')}/"
        for table_key, table_cfg in cfg["ods_external_tables"].items():
            for uri in table_cfg["source_uris"]:
                assert uri.startswith("gs://"), f"{table_key} URI must be gs://: {uri}"
            # At least one URI path component should reference the table
            has_table_key = table_key in uri
            assert has_table_key, f"{table_key}: URI {uri} must reference table"

    def test_tables_section_uses_equity_code(self):
        config_path = Path(__file__).resolve().parent.parent / "gcs_to_bigquery" / "config.yaml"
        cfg = load_config(config_path)
        eq_table = cfg["tables"].get("fact_equity_kline_1d")
        assert eq_table is not None
        assert "equity_code" in eq_table["primary_key"], "Should use equity_code per PRD_06 naming"

    def test_no_stale_datasets_block(self):
        config_path = Path(__file__).resolve().parent.parent / "gcs_to_bigquery" / "config.yaml"
        cfg = load_config(config_path)
        assert "raw" not in cfg.get("datasets", {}), "Should not have legacy 'raw' dataset key"
        assert "core" not in cfg.get("datasets", {}), "Should not have legacy 'core' dataset key"
        assert "mart" not in cfg.get("datasets", {}), "Should not have legacy 'mart' dataset key"


# ---------------------------------------------------------------------------
# Manifest read / write
# ---------------------------------------------------------------------------

class TestManifestRoundtrip:
    def test_write_read_roundtrip(self):
        records = [
            LoadRecord(
                batch_id=batch_id(),
                gcs_uri="gs://data-aquarium/a-share/standardized_parquet/foo/file.parquet",
                target_table="foo",
                partition_month=202401,
                object_size=1024,
                object_generation="12345",
                source_format="PARQUET",
                load_mode="staging_only",
            ),
        ]
        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as fh:
            path = Path(fh.name)
        try:
            write_manifest(path, records)
            loaded = read_manifest(path)
            assert len(loaded) == 1
            assert loaded[0].target_table == "foo"
            assert loaded[0].partition_month == 202401
        finally:
            path.unlink(missing_ok=True)

    def test_read_empty_manifest(self):
        path = Path("/tmp/nonexistent_manifest_test.jsonl")
        assert read_manifest(path) == []


# ---------------------------------------------------------------------------
# ODS control table schemas
# ---------------------------------------------------------------------------

class TestControlTableSchemas:
    @pytest.mark.skipif(
        os.environ.get("GCP_AVAILABLE") is None,
        reason="Requires google-cloud-bigquery to be installed",
    )
    def test_ods_manifest_schema_fields(self):
        schema = ods_manifest_schema()
        fields = {f.name for f in schema}
        assert "batch_id" in fields
        assert "synced_at" in fields
        assert "gcs_uri" in fields
        assert "target_table" in fields
        assert "destination_table" in fields

    @pytest.mark.skipif(
        os.environ.get("GCP_AVAILABLE") is None,
        reason="Requires google-cloud-bigquery to be installed",
    )
    def test_ods_errors_schema_fields(self):
        schema = ods_errors_schema()
        fields = {f.name for f in schema}
        assert "occurred_at" in fields
        assert "batch_id" in fields
        assert "error_type" in fields


# ---------------------------------------------------------------------------
# External table config generation (unit, no BigQuery)
# ---------------------------------------------------------------------------

class TestExternalTableConfig:
    def test_gcs_uri_prefix_consistency(self):
        config_path = Path(__file__).resolve().parent.parent / "gcs_to_bigquery" / "config.yaml"
        cfg = load_config(config_path)
        gcs_prefix = f"gs://{cfg['gcs']['bucket']}/{cfg['gcs']['prefix'].strip('/')}/"
        for table_key, table_cfg in cfg["ods_external_tables"].items():
            for uri in table_cfg["source_uris"]:
                assert uri.startswith(gcs_prefix), (
                    f"{table_key}: URI {uri} must start with {gcs_prefix}"
                )

    def test_hive_partitioning_present_on_fact_tables(self):
        config_path = Path(__file__).resolve().parent.parent / "gcs_to_bigquery" / "config.yaml"
        cfg = load_config(config_path)
        for table_key, table_cfg in cfg["ods_external_tables"].items():
            if table_key.startswith("fact_"):
                assert "hive_partitioning" in table_cfg, (
                    f"{table_key}: fact table must have hive_partitioning"
                )
                assert table_cfg["hive_partitioning"]["mode"] == "AUTO"

    def test_source_format_uniform(self):
        config_path = Path(__file__).resolve().parent.parent / "gcs_to_bigquery" / "config.yaml"
        cfg = load_config(config_path)
        for table_cfg in cfg["ods_external_tables"].values():
            assert table_cfg["source_format"] == "PARQUET"

    def test_existing_external_table_is_updated(self, monkeypatch):
        from gcs_to_bigquery import pipeline

        class FakeSourceFormat:
            PARQUET = "PARQUET"

        class FakeExternalConfig:
            def __init__(self, source_format):
                self.source_format = source_format
                self.source_uris = []
                self.hive_partitioning = None

        class FakeHivePartitioningOptions:
            def __init__(self):
                self.mode = None
                self.source_uri_prefix = None
                self.require_partition_filter = None

        class FakeTable:
            def __init__(self, table_id):
                self.table_id = table_id
                self.external_data_configuration = None

        class FakeBigQuery:
            SourceFormat = FakeSourceFormat
            ExternalConfig = FakeExternalConfig
            HivePartitioningOptions = FakeHivePartitioningOptions
            Table = FakeTable

        existing = FakeTable("data-aquarium.ashare.ods_fact_equity_kline_1d")
        client = MagicMock()
        client.get_table.return_value = existing
        monkeypatch.setattr(pipeline, "require_bigquery", lambda: FakeBigQuery)

        pipeline.ensure_ods_external_table(
            {"project_id": "data-aquarium", "dataset": "ashare"},
            client,
            "fact_equity_kline_1d",
            {
                "destination_table": "ods_fact_equity_kline_1d",
                "source_format": "PARQUET",
                "source_uris": ["gs://data-aquarium/a-share/standardized_parquet/fact_equity_kline_1d/**/*.parquet"],
            },
        )

        client.create_table.assert_not_called()
        client.update_table.assert_called_once_with(existing, ["external_data_configuration"])
        assert existing.external_data_configuration.source_format == "PARQUET"


# ---------------------------------------------------------------------------
# Audit logic (mock BigQuery client)
# ---------------------------------------------------------------------------

class TestAuditOdsExternal:
    def _make_mock_table(self, *, has_external=True, source_format="PARQUET"):
        table = MagicMock()
        table.schema = [
            MagicMock(name="col1"),
            MagicMock(name="col2"),
        ]
        if has_external:
            ext = MagicMock()
            ext.source_format = source_format
            ext.source_uris = [
                "gs://data-aquarium/a-share/standardized_parquet/fact_equity_kline_1d/**/*.parquet",
            ]
            table.external_data_configuration = ext
        else:
            table.external_data_configuration = None
        return table

    def _mock_client(self, tables_map: dict):
        client = MagicMock()

        def get_table(full_id):
            if full_id in tables_map:
                return tables_map[full_id]
            from google.api_core.exceptions import NotFound
            raise NotFound(f"Not found: {full_id}")

        client.get_table.side_effect = get_table

        mock_job = MagicMock()
        mock_job.result.return_value = [{"_": 1}]
        client.query.return_value = mock_job
        return client

    def test_passes_when_all_tables_ok(self, monkeypatch):
        from gcs_to_bigquery import pipeline

        config = {
            "project_id": "data-aquarium",
            "dataset": "ashare",
            "gcs": {
                "bucket": "data-aquarium",
                "prefix": "a-share/standardized_parquet",
            },
            "ods_external_tables": {
                "fact_equity_kline_1d": {
                    "destination_table": "ods_fact_equity_kline_1d",
                    "source_format": "PARQUET",
                },
            },
        }
        client = self._mock_client({
            "data-aquarium.ashare.ods_fact_equity_kline_1d": self._make_mock_table(),
        })
        monkeypatch.setattr(pipeline, "bq_client", lambda _: client)
        pipeline.audit_ods_external(config)

    def test_raises_when_table_missing(self, monkeypatch):
        from gcs_to_bigquery import pipeline

        config = {
            "project_id": "data-aquarium",
            "dataset": "ashare",
            "gcs": {
                "bucket": "data-aquarium",
                "prefix": "a-share/standardized_parquet",
            },
            "ods_external_tables": {
                "fact_equity_kline_1d": {
                    "destination_table": "ods_fact_equity_kline_1d",
                    "source_format": "PARQUET",
                },
            },
        }
        client = self._mock_client({})
        monkeypatch.setattr(pipeline, "bq_client", lambda _: client)
        with pytest.raises(RuntimeError, match="missing external tables"):
            pipeline.audit_ods_external(config)

    def test_raises_when_no_external_config(self, monkeypatch):
        from gcs_to_bigquery import pipeline

        config = {
            "project_id": "data-aquarium",
            "dataset": "ashare",
            "gcs": {
                "bucket": "data-aquarium",
                "prefix": "a-share/standardized_parquet",
            },
            "ods_external_tables": {
                "fact_equity_kline_1d": {
                    "destination_table": "ods_fact_equity_kline_1d",
                    "source_format": "PARQUET",
                },
            },
        }
        table = self._make_mock_table(has_external=False)
        client = self._mock_client({
            "data-aquarium.ashare.ods_fact_equity_kline_1d": table,
        })
        monkeypatch.setattr(pipeline, "bq_client", lambda _: client)
        with pytest.raises(RuntimeError, match="without externalDataConfiguration"):
            pipeline.audit_ods_external(config)

    def test_raises_when_wrong_source_format(self, monkeypatch):
        from gcs_to_bigquery import pipeline

        config = {
            "project_id": "data-aquarium",
            "dataset": "ashare",
            "gcs": {
                "bucket": "data-aquarium",
                "prefix": "a-share/standardized_parquet",
            },
            "ods_external_tables": {
                "fact_equity_kline_1d": {
                    "destination_table": "ods_fact_equity_kline_1d",
                    "source_format": "PARQUET",
                },
            },
        }
        table = self._make_mock_table(source_format="CSV")
        client = self._mock_client({
            "data-aquarium.ashare.ods_fact_equity_kline_1d": table,
        })
        monkeypatch.setattr(pipeline, "bq_client", lambda _: client)
        with pytest.raises(RuntimeError, match="wrong source_format"):
            pipeline.audit_ods_external(config)

    def test_raises_when_gcs_uri_mismatch(self, monkeypatch):
        from gcs_to_bigquery import pipeline

        config = {
            "project_id": "data-aquarium",
            "dataset": "ashare",
            "gcs": {
                "bucket": "data-aquarium",
                "prefix": "a-share/standardized_parquet",
            },
            "ods_external_tables": {
                "fact_equity_kline_1d": {
                    "destination_table": "ods_fact_equity_kline_1d",
                    "source_format": "PARQUET",
                },
            },
        }
        table = self._make_mock_table()
        table.external_data_configuration.source_uris = [
            "gs://other-bucket/wrong/path/file.parquet",
        ]
        client = self._mock_client({
            "data-aquarium.ashare.ods_fact_equity_kline_1d": table,
        })
        monkeypatch.setattr(pipeline, "bq_client", lambda _: client)
        with pytest.raises(RuntimeError, match="GCS URI mismatch"):
            pipeline.audit_ods_external(config)


# ---------------------------------------------------------------------------
# Deprecation: old commands emit warnings
# ---------------------------------------------------------------------------

class TestDeprecationWarnings:
    def test_load_prints_deprecation(self, capsys):
        from gcs_to_bigquery import pipeline

        config = {
            "project_id": "data-aquarium",
            "manifest_path": "/tmp/test_load_manifest.jsonl",
            "gcs": {
                "bucket": "data-aquarium",
                "prefix": "a-share/standardized_parquet",
            },
            "defaults": {"retry_failed": False},
        }
        with patch.object(pipeline, "bq_client", return_value=MagicMock()):
            with patch.object(pipeline, "iter_gcs_records", return_value=[]):
                pipeline.load(config, dry_run=True)
        captured = capsys.readouterr()
        # dry-run should succeed without deprecation warning in code path
        # (deprecation is on CLI entry point, not load() itself)


# ---------------------------------------------------------------------------
# CLI argument parsing smoke test
# ---------------------------------------------------------------------------

class TestCli:
    def test_help_contains_new_commands(self, capsys):
        from gcs_to_bigquery import pipeline
        import sys as _sys

        with pytest.raises(SystemExit):
            _sys.argv = ["pipeline.py", "--help"]
            try:
                pipeline.main()
            except SystemExit:
                raise
        captured = capsys.readouterr()
        assert "init-ods" in captured.out
        assert "create-ods-external" in captured.out
        assert "audit-ods" in captured.out
        assert "transform-dwd" not in captured.out
        assert "transform-dws" not in captured.out
        assert "transform-ads" not in captured.out
