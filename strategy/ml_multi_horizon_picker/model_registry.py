"""走步重训模型注册表：date → model_dir 映射。

配套 PRD_20260524_13（基础）和 PRD_20260524_14（GCS 支持）。

格式（JSON）::

    {
      "model_root": "models/walk_forward",
      "entries": [
        {"train_end_date": "20191231", "model_dir": "models/walk_forward/20191231"},
        {"train_end_date": "20200131", "model_dir": "models/walk_forward/20200131"},
        ...
      ]
    }

查询语义：给定 current_date，返回 train_end_date <= current_date 中最大那个对应的
model_dir。这是防止 lookahead bias 的关键——只能用今天前已经训练完成的模型。

支持本地路径和 ``gs://...`` 路径（PRD_20260524_14 引入）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


def _read_text_local_or_gcs(path) -> str:
    """读本地或 gs:// 路径文本。

    gs:// 优先 google.cloud.storage（需 ADC），失败降级到 gsutil cat
    （依赖 gcloud SDK，无需 ADC）。
    """
    p = str(path)
    if not p.startswith("gs://"):
        return Path(p).read_text(encoding="utf-8")

    try:
        from google.cloud import storage  # type: ignore
        parts = p[5:].split("/", 1)
        bucket_name = parts[0]
        blob_name = parts[1] if len(parts) > 1 else ""
        client = storage.Client()
        return client.bucket(bucket_name).blob(blob_name).download_as_text()
    except Exception:
        import subprocess
        try:
            return subprocess.run(
                ["gsutil", "cat", p],
                check=True, capture_output=True, text=True, timeout=60,
            ).stdout
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"读取 {p} 失败：google.cloud.storage 不可用且 gsutil 不在 PATH"
            ) from exc


def _write_text_local_or_gcs(path, content: str) -> None:
    """写本地或 gs:// 路径。

    gs:// 优先 google.cloud.storage，失败降级到 tempfile + gsutil cp。
    """
    p = str(path)
    if not p.startswith("gs://"):
        Path(p).parent.mkdir(parents=True, exist_ok=True)
        Path(p).write_text(content, encoding="utf-8")
        return

    try:
        from google.cloud import storage  # type: ignore
        parts = p[5:].split("/", 1)
        bucket_name = parts[0]
        blob_name = parts[1] if len(parts) > 1 else ""
        client = storage.Client()
        client.bucket(bucket_name).blob(blob_name).upload_from_string(
            content, content_type="application/json"
        )
    except Exception:
        import subprocess
        import tempfile
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            f.write(content)
            tmp_path = f.name
        try:
            subprocess.run(
                ["gsutil", "-q", "cp", tmp_path, p],
                check=True, capture_output=True, text=True, timeout=60,
            )
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"写入 {p} 失败：google.cloud.storage 不可用且 gsutil 不在 PATH"
            ) from exc
        finally:
            Path(tmp_path).unlink(missing_ok=True)


@dataclass(frozen=True)
class RegistryEntry:
    """单条注册表项：训练截止日 + 模型目录。"""

    train_end_date: str   # YYYYMMDD
    model_dir: str


class ModelRegistry:
    """模型注册表（按训练截止日排序的列表）。"""

    def __init__(self, entries: List[RegistryEntry], model_root: str = ""):
        # 按 train_end_date 升序排序，便于二分查找
        self._entries: List[RegistryEntry] = sorted(
            entries, key=lambda e: e.train_end_date
        )
        self.model_root = model_root

    # ── 构造 / 持久化 ───────────────────────────────────────────────

    @classmethod
    def from_json(cls, path: str | Path) -> "ModelRegistry":
        """从 JSON 文件加载（支持本地路径或 ``gs://...``）。"""
        text = _read_text_local_or_gcs(path)
        data = json.loads(text)
        entries = [
            RegistryEntry(
                train_end_date=str(e["train_end_date"]),
                model_dir=str(e["model_dir"]),
            )
            for e in data.get("entries", [])
        ]
        return cls(entries=entries, model_root=str(data.get("model_root", "")))

    def to_json(self, path: str | Path) -> None:
        """保存到 JSON 文件（支持本地路径或 ``gs://...``）。"""
        data = {
            "model_root": self.model_root,
            "entries": [
                {"train_end_date": e.train_end_date, "model_dir": e.model_dir}
                for e in self._entries
            ],
        }
        _write_text_local_or_gcs(
            path, json.dumps(data, indent=2, ensure_ascii=False)
        )

    # ── 查询 ───────────────────────────────────────────────────────

    def find_for_date(self, current_date: str) -> Optional[str]:
        """返回 train_end_date <= current_date 的最大项的 model_dir。

        Args:
            current_date: ``YYYYMMDD`` 或 ``YYYY-MM-DD``

        Returns:
            ``model_dir`` 路径字符串；如果没有可用条目（当前日早于所有训练终点）
            则返回 None
        """
        normalized = self._normalize_date(current_date)
        best: Optional[RegistryEntry] = None
        for e in self._entries:
            if e.train_end_date <= normalized:
                best = e
            else:
                break
        return best.model_dir if best is not None else None

    def list_entries(self) -> List[RegistryEntry]:
        """返回不可变的条目列表副本。"""
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    # ── 辅助 ───────────────────────────────────────────────────────

    @staticmethod
    def _normalize_date(d: str) -> str:
        """容错把 ``YYYY-MM-DD`` / ``YYYYMMDD`` / ``YYYYMMDDHHMM`` 归一为 ``YYYYMMDD``。"""
        s = str(d).replace("-", "").replace("/", "")
        return s[:8]


def build_registry(
    model_root: str,
    train_end_dates: List[str],
) -> ModelRegistry:
    """便捷构造：把一组训练截止日转成注册表。

    每个 train_end_date 对应一个 ``{model_root}/{YYYYMMDD}/`` 子目录。
    """
    root = model_root.rstrip("/")
    entries = [
        RegistryEntry(
            train_end_date=str(d).replace("-", ""),
            model_dir=f"{root}/{str(d).replace('-', '')}",
        )
        for d in train_end_dates
    ]
    return ModelRegistry(entries=entries, model_root=root)
