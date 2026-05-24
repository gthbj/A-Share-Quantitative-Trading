"""模型存储与加载。

支持本地文件系统和 GCS（Cloud Storage）。
策略回测时从指定路径加载预训练模型；训练脚本将模型保存到指定路径。
"""

from __future__ import annotations

import pickle
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from utils.logger import get_logger

logger = get_logger(__name__)
_GCS_STORAGE_SDK_DISABLED = False


def save_model(model: Any, path: str) -> None:
    """保存模型到本地或 GCS 路径。

    Args:
        model: 已训练的模型对象。
        path: 目标路径。本地路径如 ``models/lgbm_v1.pkl``；
            GCS 路径如 ``gs://bucket/models/lgbm_v1.pkl``。
    """
    if path.startswith("gs://"):
        _save_to_gcs(model, path)
    else:
        _save_to_local(model, path)


def load_model(path: str) -> Optional[Any]:
    """从本地或 GCS 路径加载模型。

    Args:
        path: 模型路径。本地路径或 ``gs://...``。

    Returns:
        模型对象，加载失败返回 None。
    """
    if path.startswith("gs://"):
        return _load_from_gcs(path)
    return _load_from_local(path)


def _save_to_local(model: Any, path: str) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "wb") as f:
        pickle.dump(model, f)
    logger.info(f"模型已保存到本地: {path}")


def _load_from_local(path: str) -> Optional[Any]:
    p = Path(path)
    if not p.exists():
        logger.warning(f"本地模型文件不存在: {path}")
        return None
    with open(p, "rb") as f:
        model = pickle.load(f)
    logger.info(f"模型已从本地加载: {path}")
    return model


def _save_to_gcs(model: Any, path: str) -> None:
    try:
        from google.cloud import storage
    except ImportError as exc:
        raise ImportError(
            "未安装 google-cloud-storage，无法保存到 GCS。"
        ) from exc
    # gs://bucket/blob_path
    parts = path.replace("gs://", "").split("/", 1)
    bucket_name = parts[0]
    blob_name = parts[1] if len(parts) > 1 else "model.pkl"
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)
    blob.upload_from_string(pickle.dumps(model))
    logger.info(f"模型已保存到 GCS: {path}")


def _load_from_gcs(path: str) -> Optional[Any]:
    global _GCS_STORAGE_SDK_DISABLED
    if _GCS_STORAGE_SDK_DISABLED:
        return _load_from_gcs_via_gsutil(
            path, RuntimeError("storage SDK disabled after previous failure")
        )
    try:
        from google.cloud import storage
        parts = path.replace("gs://", "").split("/", 1)
        bucket_name = parts[0]
        blob_name = parts[1] if len(parts) > 1 else "model.pkl"
        client = storage.Client()
        bucket = client.bucket(bucket_name)
        blob = bucket.blob(blob_name)
        if not blob.exists():
            logger.warning(f"GCS 模型对象不存在: {path}")
            return None
        data = blob.download_as_bytes()
        model = pickle.loads(data)
        logger.info(f"模型已从 GCS 加载: {path}")
        return model
    except Exception as exc:
        _GCS_STORAGE_SDK_DISABLED = True
        logger.warning(
            f"storage SDK 加载 GCS 模型失败，尝试 gsutil 降级读取: {path} ({exc})"
        )
        return _load_from_gcs_via_gsutil(path, exc)


def _find_gsutil() -> Optional[str]:
    """定位 gsutil，兼容本机 Google Cloud SDK 未加入 PATH 的情况。"""
    found = shutil.which("gsutil")
    if found:
        return found
    bundled = Path("/Users/luna/.local/google-cloud-sdk/bin/gsutil")
    if bundled.exists():
        return str(bundled)
    return None


def _load_from_gcs_via_gsutil(path: str, reason: Exception) -> Optional[Any]:
    gsutil = _find_gsutil()
    if not gsutil:
        raise RuntimeError(
            f"读取 {path} 失败：storage SDK 不可用，且 gsutil 不在 PATH"
        ) from reason

    result = subprocess.run(
        [gsutil, "cat", path],
        check=False,
        capture_output=True,
        timeout=120,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace")
        if "No URLs matched" in stderr or "NotFound" in stderr:
            logger.warning(f"GCS 模型对象不存在: {path}")
            return None
        raise RuntimeError(f"gsutil cat 读取模型失败: {path}: {stderr}") from reason

    model = pickle.loads(result.stdout)
    logger.info(f"模型已通过 gsutil 从 GCS 加载: {path}")
    return model
