"""GCS 模型加载降级路径测试（PRD_20260524_15）。"""

from __future__ import annotations

import pickle
import subprocess

from strategy.ml_stock_picker import model_storage


class _Completed:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_load_from_gcs_via_gsutil_success(monkeypatch):
    expected = {"model": "ok"}
    monkeypatch.setattr(model_storage, "_find_gsutil", lambda: "/usr/bin/gsutil")

    def fake_run(cmd, check, capture_output, timeout):
        assert cmd == ["/usr/bin/gsutil", "cat", "gs://bucket/model.pkl"]
        assert check is False
        assert capture_output is True
        assert timeout == 120
        return _Completed(0, stdout=pickle.dumps(expected))

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert model_storage._load_from_gcs_via_gsutil(  # noqa: SLF001
        "gs://bucket/model.pkl", RuntimeError("sdk failed")
    ) == expected


def test_load_from_gcs_via_gsutil_missing_returns_none(monkeypatch):
    monkeypatch.setattr(model_storage, "_find_gsutil", lambda: "/usr/bin/gsutil")

    def fake_run(cmd, check, capture_output, timeout):
        return _Completed(1, stderr=b"CommandException: No URLs matched")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert model_storage._load_from_gcs_via_gsutil(  # noqa: SLF001
        "gs://bucket/missing.pkl", RuntimeError("sdk failed")
    ) is None
