"""Cloud Run 走步训练相关单元测试（PRD_20260524_14）。

覆盖：
- shard_retrain_dates：striding 分片正确性
- build_registry：扫描本地目录生成 registry
- model_registry：gs:// 路径 mock（避免实际网络）
"""

from __future__ import annotations

import json
import pickle
from pathlib import Path
from typing import List

import pytest

from strategy.ml_multi_horizon_picker.build_registry import (
    _scan_local,
    build_registry_from_disk,
)
from strategy.ml_multi_horizon_picker.model_registry import (
    ModelRegistry,
    RegistryEntry,
    build_registry,
)
from strategy.ml_multi_horizon_picker.walk_forward import shard_retrain_dates


# ────────────────────── shard_retrain_dates ──────────────────────


def test_shard_strides_evenly():
    """PRD §8 用例 1：8 个时点 × 4 并发 = 每个 2 个，按 stride 分。"""
    dates = [
        "20191231", "20200131", "20200229", "20200331",
        "20200430", "20200531", "20200630", "20200731",
    ]
    assert shard_retrain_dates(dates, 0, 4) == ["20191231", "20200430"]
    assert shard_retrain_dates(dates, 1, 4) == ["20200131", "20200531"]
    assert shard_retrain_dates(dates, 2, 4) == ["20200229", "20200630"]
    assert shard_retrain_dates(dates, 3, 4) == ["20200331", "20200731"]


def test_shard_no_env_returns_all():
    """PRD §8 用例 2：未设置时返回全部。"""
    dates = ["20191231", "20200131", "20200229"]
    assert shard_retrain_dates(dates, None, None) == dates
    assert shard_retrain_dates(dates, None, 4) == dates  # 缺一个也算未设置
    assert shard_retrain_dates(dates, 0, None) == dates


def test_shard_count_one_returns_all():
    dates = ["20191231", "20200131"]
    assert shard_retrain_dates(dates, 0, 1) == dates


def test_shard_uneven_remainder():
    """7 个时点 × 3 并发 → 3,2,2 个分布。"""
    dates = ["d1", "d2", "d3", "d4", "d5", "d6", "d7"]
    s0 = shard_retrain_dates(dates, 0, 3)
    s1 = shard_retrain_dates(dates, 1, 3)
    s2 = shard_retrain_dates(dates, 2, 3)
    # 合起来覆盖全部，互不重复
    all_collected = set(s0) | set(s1) | set(s2)
    assert all_collected == set(dates)
    assert len(s0) + len(s1) + len(s2) == 7


def test_shard_more_tasks_than_dates():
    """8 并发 × 3 个时点 → 前 3 个 task 各 1，后 5 个空。"""
    dates = ["d1", "d2", "d3"]
    assert shard_retrain_dates(dates, 0, 8) == ["d1"]
    assert shard_retrain_dates(dates, 1, 8) == ["d2"]
    assert shard_retrain_dates(dates, 2, 8) == ["d3"]
    assert shard_retrain_dates(dates, 3, 8) == []
    assert shard_retrain_dates(dates, 7, 8) == []


# ────────────────────── build_registry 本地扫描 ──────────────────────


def _make_fake_model_dir(root: Path, dates: List[str], with_pkl: bool = True) -> None:
    """造假目录结构：root/{date}/buy_h5.pkl"""
    for d in dates:
        sub = root / d
        sub.mkdir(parents=True, exist_ok=True)
        if with_pkl:
            # 真 pickle 一个空 dict，让 glob *.pkl 能匹配
            (sub / "buy_h5.pkl").write_bytes(pickle.dumps({"hi": 1}))
            (sub / "metadata.json").write_text("{}", encoding="utf-8")


def test_scan_local_returns_valid_dirs(tmp_path: Path):
    """PRD §8 用例 3：扫描本地目录得到所有 YYYYMMDD 子目录。"""
    _make_fake_model_dir(tmp_path, ["20191231", "20200131", "20200229"])
    result = _scan_local(str(tmp_path))
    dates = [d for d, _ in result]
    assert dates == ["20191231", "20200131", "20200229"]


def test_scan_local_skips_non_date_dirs(tmp_path: Path):
    """非 YYYYMMDD 命名的子目录应被跳过。"""
    _make_fake_model_dir(tmp_path, ["20191231"])
    (tmp_path / "ad_hoc").mkdir()
    (tmp_path / "ad_hoc" / "buy_h5.pkl").write_bytes(b"x")
    result = _scan_local(str(tmp_path))
    assert [d for d, _ in result] == ["20191231"]


def test_scan_local_skips_empty_dirs(tmp_path: Path):
    """无 .pkl 文件的子目录应被跳过。"""
    _make_fake_model_dir(tmp_path, ["20191231"], with_pkl=True)
    _make_fake_model_dir(tmp_path, ["20200131"], with_pkl=False)  # 空目录
    result = _scan_local(str(tmp_path))
    assert [d for d, _ in result] == ["20191231"]


def test_build_registry_from_disk(tmp_path: Path):
    """完整流程：扫描 → ModelRegistry。"""
    _make_fake_model_dir(tmp_path, ["20191231", "20200131"])
    reg = build_registry_from_disk(str(tmp_path))
    assert len(reg) == 2
    assert reg.find_for_date("20200115") == str(tmp_path / "20191231")
    assert reg.find_for_date("20200131") == str(tmp_path / "20191231")
    assert reg.find_for_date("20200201") == str(tmp_path / "20200131")


def test_build_registry_to_json_roundtrip(tmp_path: Path):
    """扫描 + 序列化 + 反序列化。"""
    _make_fake_model_dir(tmp_path, ["20191231", "20200131", "20200229"])
    reg = build_registry_from_disk(str(tmp_path))
    json_path = tmp_path / "registry.json"
    reg.to_json(json_path)
    data = json.loads(json_path.read_text("utf-8"))
    assert data["model_root"].rstrip("/") == str(tmp_path)
    assert len(data["entries"]) == 3

    reloaded = ModelRegistry.from_json(json_path)
    assert len(reloaded) == 3
    assert reloaded.find_for_date("20200201") == str(tmp_path / "20200131")


# ────────────────────── GCS 路径辅助函数 ──────────────────────


def test_join_path_local():
    """import 内部 helper 测试。"""
    from strategy.ml_multi_horizon_picker.walk_forward import _join_path

    assert _join_path("models/wf", "20191231") == str(
        Path("models/wf") / "20191231"
    )


def test_join_path_gcs():
    from strategy.ml_multi_horizon_picker.walk_forward import _join_path

    assert (
        _join_path("gs://bucket/wf", "20191231")
        == "gs://bucket/wf/20191231"
    )
    # 多段
    assert (
        _join_path("gs://bucket/wf", "20191231", "metadata.json")
        == "gs://bucket/wf/20191231/metadata.json"
    )
    # 末尾斜杠正常处理
    assert (
        _join_path("gs://bucket/wf/", "20191231")
        == "gs://bucket/wf/20191231"
    )


def test_is_gcs_path():
    from strategy.ml_multi_horizon_picker.walk_forward import _is_gcs_path

    assert _is_gcs_path("gs://bucket/x") is True
    assert _is_gcs_path("/local/path") is False
    assert _is_gcs_path("relative/path") is False
