"""ml_multi_horizon_picker 特征工程。

复用 ml_stock_picker.FeatureEngineer 提供的 17 维基础特征，在其上扩展
5 维 sell-side 风险特征，专门给"独立卖出模型"用。

买入模型仍只用 17 维基础特征（与 DWS 表对齐，零额外计算）。
卖出模型用 17 + 5 = 22 维。
"""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

from strategy.ml_stock_picker.features import FeatureEngineer as BaseFeatureEngineer


# 17 维买入特征（直接引用，保持与 DWS 表 dws_equity_daily_features 列名对齐）
BUY_FEATURE_COLUMNS: List[str] = BaseFeatureEngineer.feature_columns()


# 5 维卖出风险特征（本模块新增）
SELL_RISK_FEATURE_COLUMNS: List[str] = [
    "drawdown_from_high_20d",
    "drawdown_from_high_60d",
    "vol_expansion",
    "rsi_overbought_streak",
    "dist_to_ma60",
]


# 卖出模型总特征 = 17 + 5
SELL_FEATURE_COLUMNS: List[str] = BUY_FEATURE_COLUMNS + SELL_RISK_FEATURE_COLUMNS


def compute_sell_risk_features(df: pd.DataFrame) -> pd.DataFrame:
    """为单只股票计算 5 维 sell-side 风险特征。

    输入要求：含 ['date', 'close', 'high', 'rsi_14']。若已有
    `ma_60` 列优先使用（DWS 表里就有），否则现算。

    输出：在输入基础上追加 5 个新列，索引保持。
    """
    if df.empty:
        return df.copy()

    out = df.sort_values("date").reset_index(drop=True).copy()
    close = out["close"].astype(float)
    high = out["high"].astype(float)

    # 1) 20 日内最高价回撤
    high20 = high.rolling(20, min_periods=1).max()
    out["drawdown_from_high_20d"] = (close - high20) / high20.replace(0, np.nan)

    # 2) 60 日内最高价回撤
    high60 = high.rolling(60, min_periods=1).max()
    out["drawdown_from_high_60d"] = (close - high60) / high60.replace(0, np.nan)

    # 3) 波动率扩张：5d std / 60d std
    std5 = close.rolling(5, min_periods=1).std()
    std60 = close.rolling(60, min_periods=1).std()
    out["vol_expansion"] = std5 / std60.replace(0, np.nan)

    # 4) RSI 连续超买天数（RSI > 70 的当前连续天数）
    if "rsi_14" in out.columns:
        rsi = out["rsi_14"].astype(float)
        is_ob = (rsi > 70).astype(int)
        # 连续 streak：reset 到 0 每次 is_ob=0
        streak = np.zeros(len(out), dtype=float)
        for i in range(len(out)):
            if is_ob.iloc[i] == 1:
                streak[i] = streak[i - 1] + 1 if i > 0 else 1
        out["rsi_overbought_streak"] = streak
    else:
        out["rsi_overbought_streak"] = np.nan

    # 5) 距 60 日均线的偏离
    if "ma_60" in out.columns:
        ma60 = out["ma_60"].astype(float)
    else:
        ma60 = close.rolling(60, min_periods=1).mean()
    out["dist_to_ma60"] = (close - ma60) / ma60.replace(0, np.nan)

    return out


def deterministic_score(feature_df: pd.DataFrame) -> pd.Series:
    """模型缺失时的兜底打分函数。

    简单组合：动量 + RSI 中性 + 距 MA20 正偏离 - 高波动惩罚
    返回 score，越高越好。

    注：此函数仅用于训练前测试管道、CI 检查；正式回测必须有训练好的模型。
    """
    df = feature_df.copy()
    score = (
        df.get("return_5d", 0).fillna(0).astype(float) * 1.0
        + df.get("return_20d", 0).fillna(0).astype(float) * 0.5
        + (50 - (df.get("rsi_14", 50).fillna(50).astype(float) - 50).abs()) / 100
        + df.get("close_to_ma20", 0).fillna(0).astype(float) * 0.3
        - df.get("std_20d", 0).fillna(0).astype(float) * 0.01
    )
    return score


def deterministic_sell_score(feature_df: pd.DataFrame) -> pd.Series:
    """模型缺失时的卖出概率兜底打分。

    回撤越大、波动越扩张、连续超买越多 → sell prob 越高。
    返回 [0, 1] 区间值（粗略，仅用于管道测试）。
    """
    df = feature_df.copy()
    raw = (
        -df.get("drawdown_from_high_20d", 0).fillna(0).astype(float) * 3
        + (df.get("vol_expansion", 1).fillna(1).astype(float) - 1).clip(0, None) * 2
        + (df.get("rsi_overbought_streak", 0).fillna(0).astype(float) > 5).astype(float) * 0.3
        + (df.get("return_5d", 0).fillna(0).astype(float) < -0.05).astype(float) * 0.3
    )
    # 简单挤压到 [0, 1]
    return 1 / (1 + np.exp(-raw * 3))
