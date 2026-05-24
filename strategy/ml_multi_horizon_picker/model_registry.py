"""走步重训模型注册表：date → model_dir 映射。

配套 PRD_20260524_06。

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
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional


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
        """从 JSON 文件加载。"""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        entries = [
            RegistryEntry(
                train_end_date=str(e["train_end_date"]),
                model_dir=str(e["model_dir"]),
            )
            for e in data.get("entries", [])
        ]
        return cls(entries=entries, model_root=str(data.get("model_root", "")))

    def to_json(self, path: str | Path) -> None:
        """保存到 JSON 文件。"""
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "model_root": self.model_root,
            "entries": [
                {"train_end_date": e.train_end_date, "model_dir": e.model_dir}
                for e in self._entries
            ],
        }
        p.write_text(
            json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
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
