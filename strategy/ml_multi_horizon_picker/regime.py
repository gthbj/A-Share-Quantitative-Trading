"""市场状态（Regime）检测：bull / neutral / bear。

设计简单可调试：
- close > MA200 ：趋势向上前提
- 60 日实际波动率 < 历史 80% 分位：波动率低（稳态）

组合：
- bull    = close > MA200 且 60d vol < 80% 分位
- neutral = close > MA200 且 60d vol >= 80% 分位
- bear    = close <= MA200

不用 HMM，避免训练收敛和样本依赖问题。
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd


class Regime(str, Enum):
    BULL = "bull"
    NEUTRAL = "neutral"
    BEAR = "bear"


def detect_regime(
    index_close: pd.Series,
    ma_window: int = 200,
    vol_window: int = 60,
    vol_percentile: float = 0.80,
) -> pd.Series:
    """对基准指数收盘价序列计算逐日 regime。

    Args:
        index_close: 基准指数收盘价时间序列（按日，至少 `ma_window + vol_window` 天）
        ma_window: 趋势均线窗口
        vol_window: 波动率窗口
        vol_percentile: 高波动阈值（历史百分位）

    Returns:
        与 `index_close` 等长的 Series，值为 `Regime` 枚举字符串。
        前 ma_window 行因数据不足返回 NEUTRAL（保守）。
    """
    if index_close.empty:
        return pd.Series([], dtype=object)

    close = index_close.astype(float)

    ma = close.rolling(ma_window, min_periods=1).mean()
    log_ret = np.log(close).diff()
    realized_vol = log_ret.rolling(vol_window, min_periods=2).std()

    # 历史百分位（用扩张窗口避免 lookahead）
    vol_threshold = realized_vol.expanding(min_periods=vol_window).quantile(vol_percentile)

    above_ma = close > ma
    high_vol = realized_vol >= vol_threshold

    regimes = []
    for i in range(len(close)):
        if i < ma_window:
            regimes.append(Regime.NEUTRAL.value)
            continue
        if not above_ma.iloc[i]:
            regimes.append(Regime.BEAR.value)
        elif bool(high_vol.iloc[i]):
            regimes.append(Regime.NEUTRAL.value)
        else:
            regimes.append(Regime.BULL.value)
    return pd.Series(regimes, index=close.index, dtype=object)


def latest_regime(index_close: pd.Series, **kwargs) -> str:
    """便捷封装：返回序列最后一天的 regime。"""
    series = detect_regime(index_close, **kwargs)
    if series.empty:
        return Regime.NEUTRAL.value
    return str(series.iloc[-1])


def regime_position_multiplier(regime: str) -> float:
    """Regime → 目标仓位比例乘数。"""
    return {
        Regime.BULL.value: 1.0,
        Regime.NEUTRAL.value: 0.7,
        Regime.BEAR.value: 0.3,
    }.get(regime, 0.7)


def regime_stop_loss(regime: str, bull_pct: float = 0.05, bear_pct: float = 0.03) -> float:
    """Regime → 止损百分比。bear 收紧。"""
    if regime == Regime.BEAR.value:
        return bear_pct
    return bull_pct
