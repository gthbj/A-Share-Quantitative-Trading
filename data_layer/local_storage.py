"""本地数据存储与读取。

支持 Parquet / CSV 格式，提供按日期范围、股票代码的快速索引。
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd


BAR_NUMERIC_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]


def _normalize_bar_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "date" in out.columns:
        out["date"] = out["date"].astype(str)
    for col in BAR_NUMERIC_COLUMNS:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out


class LocalStorage:
    """本地行情数据存储器。

    目录结构：
        data/raw/
            ├── daily/
            │     ├── 000001.parquet
            │     ├── 000002.parquet
            │     └── ...
            └── info/
                  └── stock_list.parquet
    """

    def __init__(self, root_dir: str = "data/raw") -> None:
        self.root = Path(root_dir)
        self.daily_dir = self.root / "daily"
        self.info_dir = self.root / "info"
        self.daily_dir.mkdir(parents=True, exist_ok=True)
        self.info_dir.mkdir(parents=True, exist_ok=True)

    def _bar_path(self, code: str, period: str = "daily", fmt: str = "parquet") -> Path:
        if period == "daily":
            return self.daily_dir / f"{code}.{fmt}"
        return self.daily_dir / f"{code}_{period}.{fmt}"

    def save_bars(self, code: str, df: pd.DataFrame, period: str = "daily", fmt: str = "parquet") -> None:
        """保存单只股票K线数据。"""
        path = self._bar_path(code, period, fmt)
        if fmt == "parquet":
            df.to_parquet(path, index=False)
        else:
            df.to_csv(path, index=False)

    def load_bars_raw(
        self,
        code: str,
        period: str = "daily",
        fmt: str = "parquet",
    ) -> pd.DataFrame:
        """读取单只股票K线数据的完整缓存（不做日期过滤）。

        用于缓存覆盖范围检查，配合 load_bars 使用。
        """
        path = self._bar_path(code, period, fmt)
        if not path.exists():
            return pd.DataFrame()

        if fmt == "parquet":
            df = pd.read_parquet(path)
        else:
            df = pd.read_csv(path)
        return _normalize_bar_frame(df).reset_index(drop=True)

    def load_bars(
        self,
        code: str,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
        period: str = "daily",
        fmt: str = "parquet",
    ) -> pd.DataFrame:
        """读取单只股票K线数据，支持日期过滤。"""
        path = self._bar_path(code, period, fmt)
        if not path.exists():
            return pd.DataFrame()

        if fmt == "parquet":
            df = pd.read_parquet(path)
        else:
            df = pd.read_csv(path)

        df = _normalize_bar_frame(df)

        if "date" not in df.columns:
            return df

        if start_date:
            df = df[df["date"] >= start_date]
        if end_date:
            df = df[df["date"] <= end_date + "9999"]
        return df.reset_index(drop=True)

    def save_stock_list(self, df: pd.DataFrame, fmt: str = "parquet") -> None:
        """保存股票基础信息表。"""
        path = self.info_dir / f"stock_list.{fmt}"
        if fmt == "parquet":
            df.to_parquet(path, index=False)
        else:
            df.to_csv(path, index=False)

    def load_stock_list(self, fmt: str = "parquet") -> pd.DataFrame:
        """读取股票基础信息表。"""
        path = self.info_dir / f"stock_list.{fmt}"
        if not path.exists():
            return pd.DataFrame()
        if fmt == "parquet":
            return pd.read_parquet(path)
        return pd.read_csv(path)
