"""扫描 model_root 下所有时点子目录，生成 registry.json。

配套 PRD_20260524_14：Cloud Run 并行训练后用此工具合并所有 task 产物。

用法::

    # 本地
    python -m strategy.ml_multi_horizon_picker.build_registry \\
        --model-root models/walk_forward

    # GCS
    python -m strategy.ml_multi_horizon_picker.build_registry \\
        --model-root gs://data-aquarium/models/walk_forward

逻辑：
    1. 列出 model_root 下所有形如 ``YYYYMMDD/`` 的子目录
    2. 验证每个子目录至少有 1 个 ``.pkl`` 模型文件
    3. 按 train_end_date 升序，写 ``model_root/registry.json``
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Tuple

from strategy.ml_multi_horizon_picker.model_registry import (
    ModelRegistry,
    RegistryEntry,
    _write_text_local_or_gcs,
)
from utils.logger import get_logger

logger = get_logger(__name__)


_DATE_DIR_RE = re.compile(r"^\d{8}$")


def _scan_local(model_root: str) -> List[Tuple[str, str]]:
    """扫描本地目录，返回 [(YYYYMMDD, model_dir), ...]。"""
    root = Path(model_root)
    if not root.exists():
        return []
    result: List[Tuple[str, str]] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        if not _DATE_DIR_RE.match(sub.name):
            continue
        # 至少有一个 .pkl
        if not any(sub.glob("*.pkl")):
            logger.warning(f"跳过 {sub}：无 .pkl 文件")
            continue
        result.append((sub.name, str(sub)))
    return result


def _scan_gcs_via_storage_api(model_root: str) -> List[Tuple[str, str]]:
    """用 google.cloud.storage SDK 扫描（需 ADC）。"""
    from google.cloud import storage  # type: ignore

    parts = model_root[5:].split("/", 1)
    bucket_name = parts[0]
    prefix = parts[1].rstrip("/") + "/" if len(parts) > 1 and parts[1] else ""

    client = storage.Client()
    bucket = client.bucket(bucket_name)

    iterator = bucket.list_blobs(prefix=prefix, delimiter="/")
    _ = list(iterator)
    prefixes = list(iterator.prefixes)

    result: List[Tuple[str, str]] = []
    for full_prefix in sorted(prefixes):
        leaf = full_prefix[len(prefix):].rstrip("/")
        if not _DATE_DIR_RE.match(leaf):
            continue
        sub_blobs = list(bucket.list_blobs(prefix=full_prefix, max_results=10))
        if not any(b.name.endswith(".pkl") for b in sub_blobs):
            logger.warning(f"跳过 gs://{bucket_name}/{full_prefix}：无 .pkl 文件")
            continue
        result.append((leaf, f"gs://{bucket_name}/{full_prefix.rstrip('/')}"))
    return result


def _scan_gcs_via_gsutil(model_root: str) -> List[Tuple[str, str]]:
    """用 gsutil 子进程扫描（不需要 ADC，依赖 gcloud SDK on PATH）。"""
    import subprocess
    root = model_root.rstrip("/")
    try:
        out = subprocess.run(
            ["gsutil", "ls", f"{root}/"],
            check=True, capture_output=True, text=True, timeout=60,
        ).stdout
    except FileNotFoundError as exc:
        raise RuntimeError(
            "gsutil 不在 PATH 且 ADC 不可用。安装 Google Cloud SDK 或运行 "
            "`gcloud auth application-default login` 配置 ADC。"
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"gsutil ls 失败: {exc.stderr}") from exc

    result: List[Tuple[str, str]] = []
    for line in sorted(out.strip().splitlines()):
        line = line.strip().rstrip("/")
        if not line.startswith("gs://"):
            continue
        leaf = line.split("/")[-1]
        if not _DATE_DIR_RE.match(leaf):
            continue
        try:
            sub_out = subprocess.run(
                ["gsutil", "ls", f"{line}/"],
                check=True, capture_output=True, text=True, timeout=30,
            ).stdout
        except subprocess.CalledProcessError:
            sub_out = ""
        if ".pkl" not in sub_out:
            logger.warning(f"跳过 {line}：无 .pkl 文件")
            continue
        result.append((leaf, line))
    return result


def _scan_gcs(model_root: str) -> List[Tuple[str, str]]:
    """扫描 gs:// 路径，返回 [(YYYYMMDD, gs://.../{date}), ...]。

    优先 google.cloud.storage（需 ADC）；ADC 不可用时降级到 gsutil 子进程
    （仅需 gcloud SDK 安装，无需配置 ADC）。
    """
    if not model_root.startswith("gs://"):
        raise ValueError(f"非 gs:// 路径: {model_root}")
    try:
        return _scan_gcs_via_storage_api(model_root)
    except Exception as exc:
        logger.info(
            f"storage SDK 不可用（{exc.__class__.__name__}: {exc}），降级到 gsutil"
        )
        return _scan_gcs_via_gsutil(model_root)


def scan_model_root(model_root: str) -> List[Tuple[str, str]]:
    """扫描本地或 GCS 路径，返回 [(YYYYMMDD, model_dir), ...] 升序。"""
    if model_root.startswith("gs://"):
        return _scan_gcs(model_root)
    return _scan_local(model_root)


def build_registry_from_disk(model_root: str) -> ModelRegistry:
    """从已存在的 model_root 扫描生成 ModelRegistry。"""
    entries_raw = scan_model_root(model_root)
    entries = [
        RegistryEntry(train_end_date=d, model_dir=path)
        for d, path in entries_raw
    ]
    return ModelRegistry(entries=entries, model_root=model_root.rstrip("/"))


def _upload_via_gsutil(local_path: str, gcs_path: str) -> None:
    """用 gsutil cp 把本地文件上传到 gs://，规避 ADC 依赖。"""
    import subprocess
    try:
        subprocess.run(
            ["gsutil", "-q", "cp", local_path, gcs_path],
            check=True, capture_output=True, text=True, timeout=60,
        )
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"gsutil cp 失败: {exc.stderr}") from exc


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="build_registry")
    parser.add_argument(
        "--model-root",
        required=True,
        help="模型根目录（本地路径或 gs://...）",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="registry.json 输出路径，默认 {model_root}/registry.json",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印扫描结果，不写文件",
    )
    args = parser.parse_args(argv)

    registry = build_registry_from_disk(args.model_root)
    logger.info(f"扫描完成，共 {len(registry)} 个有效时点")
    for entry in registry.list_entries():
        logger.info(f"  {entry.train_end_date}: {entry.model_dir}")

    if args.dry_run:
        return 0

    output = args.output or (args.model_root.rstrip("/") + "/registry.json")

    if output.startswith("gs://"):
        # 走本地写 + gsutil cp 上传，避免依赖 ADC
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            registry.to_json(Path(f.name))  # 这里走本地路径
            tmp_path = f.name
        try:
            _upload_via_gsutil(tmp_path, output)
        finally:
            Path(tmp_path).unlink(missing_ok=True)
    else:
        registry.to_json(output)
    logger.info(f"registry.json 已写入 {output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
