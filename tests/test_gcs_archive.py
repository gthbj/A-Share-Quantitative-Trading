from __future__ import annotations

import json

from analytics.gcs_archive import (
    archive_backtest_output,
    build_destination_prefix,
    parse_gcs_uri,
    sanitize_path_part,
)


class FakeBlob:
    def __init__(self, name: str, uploads: list[tuple[str, str]]) -> None:
        self.name = name
        self._uploads = uploads

    def upload_from_filename(self, filename: str) -> None:
        self._uploads.append((self.name, filename))


class FakeBucket:
    def __init__(self, uploads: list[tuple[str, str]]) -> None:
        self._uploads = uploads

    def blob(self, name: str) -> FakeBlob:
        return FakeBlob(name, self._uploads)


class FakeClient:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str]] = []

    def bucket(self, name: str) -> FakeBucket:
        self.bucket_name = name
        return FakeBucket(self.uploads)


def test_parse_gcs_uri():
    assert parse_gcs_uri("gs://data-aquarium/a-share/backtest_runs") == (
        "data-aquarium",
        "a-share/backtest_runs",
    )


def test_sanitize_path_part_keeps_stable_path_characters():
    assert sanitize_path_part("strategy.foo.Bar") == "strategy.foo.Bar"
    assert sanitize_path_part("测试 run/name") == "run_name"


def test_build_destination_prefix_from_config():
    config = {
        "output": {
            "gcs_archive": {
                "bucket": "data-aquarium",
                "prefix": "a-share/backtest_runs",
            }
        }
    }

    assert build_destination_prefix(config, "ml_stock_picker", "20260524_150000") == (
        "data-aquarium",
        "a-share/backtest_runs/ml_stock_picker/20260524_150000",
    )


def test_archive_backtest_output_uploads_files_and_manifest(tmp_path):
    out = tmp_path / "run"
    out.mkdir()
    (out / "summary.md").write_text("# summary\n", encoding="utf-8")
    (out / "charts").mkdir()
    (out / "charts" / "cum_returns.png").write_bytes(b"png")

    fake_client = FakeClient()
    config = {
        "output": {
            "gcs_archive": {
                "enabled": True,
                "bucket": "data-aquarium",
                "prefix": "a-share/backtest_runs",
            }
        }
    }

    result = archive_backtest_output(
        out,
        config,
        strategy_key="ml_stock_picker",
        strategy_class_path="strategy.ml_stock_picker.MLStockPickerStrategy",
        run_label="20260524_150000",
        start_date="20250101",
        end_date="20251231",
        initial_capital=1_000_000,
        frequency="daily",
        benchmark="000300.SH",
        client_factory=lambda _: fake_client,
    )

    assert result is not None
    assert result.uri == "gs://data-aquarium/a-share/backtest_runs/ml_stock_picker/20260524_150000/"
    uploaded_names = {name for name, _ in fake_client.uploads}
    assert "a-share/backtest_runs/ml_stock_picker/20260524_150000/summary.md" in uploaded_names
    assert "a-share/backtest_runs/ml_stock_picker/20260524_150000/charts/cum_returns.png" in uploaded_names
    assert "a-share/backtest_runs/ml_stock_picker/20260524_150000/gcs_archive_manifest.json" in uploaded_names

    manifest = json.loads((out / "gcs_archive_manifest.json").read_text(encoding="utf-8"))
    assert manifest["strategy_key"] == "ml_stock_picker"
    assert manifest["destination_uri"] == result.uri
    assert "summary.md" in manifest["files"]
    assert "gcs_archive_manifest.json" in manifest["files"]
