"""市场状态（Regime）检测：bull / neutral / bear。

Regime 在本策略中不是行情标签，而是风险预算开关。判定只使用当前日
及历史数据，避免未来函数：

- 指数趋势：MA60 / MA120 / MA200 和 MA60 斜率
- 指数动量：20 日 / 60 日收益
- 指数风险：20 日 / 60 日回撤、5 日快速下跌
- 指数波动：20 日波动率相对历史分位和 60 日波动率
- 市场广度：可选，用股票池当日特征修正基础 regime

不用 HMM，避免训练收敛和样本依赖问题。
"""

from __future__ import annotations

from enum import Enum
from typing import Dict

import numpy as np
import pandas as pd


class Regime(str, Enum):
    BULL = "bull"
    NEUTRAL = "neutral"
    BEAR = "bear"


def detect_regime(
    index_close: pd.Series,
    ma_window: int = 200,
    short_ma_window: int = 60,
    medium_ma_window: int = 120,
    vol_window: int = 60,
    vol_percentile: float = 0.80,
    momentum_window: int = 20,
    long_momentum_window: int = 60,
    fast_drop_window: int = 5,
    severe_drawdown_threshold: float = -0.08,
    fast_drop_threshold: float = -0.05,
    bull_score_threshold: float = 70.0,
    bear_score_threshold: float = 40.0,
) -> pd.Series:
    """对基准指数收盘价序列计算逐日 regime。

    Args:
        index_close: 基准指数收盘价时间序列（按日，至少 `ma_window + vol_window` 天）
        ma_window: 长趋势均线窗口，默认 MA200
        short_ma_window: 短趋势均线窗口，默认 MA60
        medium_ma_window: 中期趋势均线窗口，默认 MA120
        vol_window: 波动率窗口
        vol_percentile: 高波动阈值（历史百分位）
        momentum_window: 中短期动量窗口
        long_momentum_window: 中期动量窗口
        fast_drop_window: 快速下跌窗口
        severe_drawdown_threshold: 20 日回撤低于该值时直接 bear
        fast_drop_threshold: fast_drop_window 收益低于该值时直接 bear
        bull_score_threshold: 风险评分达到该值进入 bull
        bear_score_threshold: 风险评分低于该值进入 bear

    Returns:
        与 `index_close` 等长的 Series，值为 `Regime` 枚举字符串。
        数据不足时返回 NEUTRAL（保守）。
    """
    if index_close.empty:
        return pd.Series([], dtype=object)

    close = index_close.astype(float)
    ma_short = close.rolling(short_ma_window, min_periods=1).mean()
    ma_medium = close.rolling(medium_ma_window, min_periods=1).mean()
    ma_long = close.rolling(ma_window, min_periods=1).mean()
    ma_short_slope = ma_short / ma_short.shift(momentum_window) - 1
    log_ret = np.log(close).diff()
    ret_fast = close / close.shift(fast_drop_window) - 1
    ret_mid = close / close.shift(momentum_window) - 1
    ret_long = close / close.shift(long_momentum_window) - 1
    drawdown_20d = close / close.rolling(momentum_window, min_periods=1).max() - 1
    drawdown_60d = close / close.rolling(long_momentum_window, min_periods=1).max() - 1
    realized_vol_20d = log_ret.rolling(momentum_window, min_periods=2).std()
    realized_vol = log_ret.rolling(vol_window, min_periods=2).std()

    # 历史百分位（用扩张窗口避免 lookahead）
    vol_threshold = realized_vol_20d.expanding(min_periods=vol_window).quantile(
        vol_percentile
    )

    regimes = []
    min_history = max(ma_window, vol_window, long_momentum_window)
    for i in range(len(close)):
        if i < min_history:
            regimes.append(Regime.NEUTRAL.value)
            continue

        score = 0.0
        current = close.iloc[i]

        # 趋势层：35 分
        if current > ma_short.iloc[i]:
            score += 8
        if current > ma_medium.iloc[i]:
            score += 10
        if current > ma_long.iloc[i]:
            score += 12
        if ma_short_slope.iloc[i] > 0:
            score += 5

        # 动量层：20 分
        if ret_mid.iloc[i] > 0:
            score += 10
        if ret_long.iloc[i] > 0:
            score += 10

        # 回撤层：25 分
        if drawdown_20d.iloc[i] > -0.05:
            score += 10
        if drawdown_60d.iloc[i] > -0.10:
            score += 10
        if ret_fast.iloc[i] > -0.03:
            score += 5

        # 波动层：20 分
        if realized_vol_20d.iloc[i] <= vol_threshold.iloc[i]:
            score += 12
        if realized_vol_20d.iloc[i] <= realized_vol.iloc[i] * 1.2:
            score += 8

        severe_selloff = (
            drawdown_20d.iloc[i] <= severe_drawdown_threshold
            or ret_fast.iloc[i] <= fast_drop_threshold
        )
        long_trend_break = (
            current <= ma_long.iloc[i]
            and (ma_short_slope.iloc[i] <= 0 or ret_mid.iloc[i] <= 0)
        )

        if severe_selloff or long_trend_break or score < bear_score_threshold:
            regimes.append(Regime.BEAR.value)
        elif score >= bull_score_threshold:
            regimes.append(Regime.BULL.value)
        else:
            regimes.append(Regime.NEUTRAL.value)
    return pd.Series(regimes, index=close.index, dtype=object)


def latest_regime(index_close: pd.Series, **kwargs) -> str:
    """便捷封装：返回序列最后一天的 regime。"""
    series = detect_regime(index_close, **kwargs)
    if series.empty:
        return Regime.NEUTRAL.value
    return str(series.iloc[-1])


def market_breadth_metrics(feature_frame: pd.DataFrame) -> Dict[str, float]:
    """从股票池当日特征计算市场广度指标。

    输入通常是策略当日 `score_df`，其中已经包含买入模型使用的技术特征。
    该函数只读当前日可见的截面特征，不使用未来收益。
    """
    if feature_frame.empty:
        return {}

    metrics: Dict[str, float] = {}

    if "close_to_ma20" in feature_frame.columns:
        close_to_ma20 = pd.to_numeric(feature_frame["close_to_ma20"], errors="coerce")
        valid = close_to_ma20.dropna()
        if not valid.empty:
            metrics["above_ma20_share"] = float((valid > 0).mean())

    if "return_20d" in feature_frame.columns:
        ret20 = pd.to_numeric(feature_frame["return_20d"], errors="coerce")
        valid = ret20.dropna()
        if not valid.empty:
            metrics["positive_return_20d_share"] = float((valid > 0).mean())
            metrics["median_return_20d"] = float(valid.median())

    if "return_5d" in feature_frame.columns:
        ret5 = pd.to_numeric(feature_frame["return_5d"], errors="coerce")
        valid = ret5.dropna()
        if not valid.empty:
            metrics["selloff_5d_share"] = float((valid < -0.03).mean())

    return metrics


def combine_regime_with_market_breadth(
    base_regime: str,
    feature_frame: pd.DataFrame,
    breadth_bear_threshold: float = 0.35,
    breadth_recovery_threshold: float = 0.55,
    median_return_bear_threshold: float = -0.05,
    selloff_share_bear_threshold: float = 0.60,
) -> str:
    """用股票池广度修正指数基础 regime。

    广度恶化时可以把 `bull/neutral` 降为 `bear`；广度恢复时，只允许把
    `bear` 修正到 `neutral`，避免刚反弹就立刻满风险。
    """
    metrics = market_breadth_metrics(feature_frame)
    if not metrics:
        return base_regime

    above_ma20_share = metrics.get("above_ma20_share")
    positive_return_20d_share = metrics.get("positive_return_20d_share")
    median_return_20d = metrics.get("median_return_20d")
    selloff_5d_share = metrics.get("selloff_5d_share")

    breadth_bear = any(
        [
            above_ma20_share is not None and above_ma20_share < breadth_bear_threshold,
            positive_return_20d_share is not None
            and positive_return_20d_share < breadth_bear_threshold,
            median_return_20d is not None
            and median_return_20d <= median_return_bear_threshold,
            selloff_5d_share is not None
            and selloff_5d_share >= selloff_share_bear_threshold,
        ]
    )
    if breadth_bear:
        return Regime.BEAR.value

    breadth_recovered = all(
        value is not None and value >= breadth_recovery_threshold
        for value in (above_ma20_share, positive_return_20d_share)
    )
    if (
        base_regime == Regime.BEAR.value
        and breadth_recovered
        and (median_return_20d is None or median_return_20d > 0)
    ):
        return Regime.NEUTRAL.value

    return base_regime


def regime_position_multiplier(regime: str) -> float:
    """Regime → 目标仓位比例乘数。"""
    return {
        Regime.BULL.value: 1.0,
        Regime.NEUTRAL.value: 0.5,
        Regime.BEAR.value: 0.0,
    }.get(regime, 0.7)


def regime_stop_loss(regime: str, bull_pct: float = 0.05, bear_pct: float = 0.03) -> float:
    """Regime → 止损百分比。bear 收紧。"""
    if regime == Regime.BEAR.value:
        return bear_pct
    return bull_pct
