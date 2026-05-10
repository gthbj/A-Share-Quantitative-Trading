"""绩效指标计算：收益、风险、交易统计。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

import numpy as np
import pandas as pd


@dataclass
class MetricsResult:
    """绩效指标结果容器。"""

    total_return: float = 0.0
    annual_return: float = 0.0
    benchmark_return: float = 0.0
    excess_return: float = 0.0
    max_drawdown: float = 0.0
    max_drawdown_duration: int = 0
    volatility: float = 0.0
    sharpe_ratio: float = 0.0
    sortino_ratio: float = 0.0
    beta: float = 0.0
    alpha: float = 0.0
    information_ratio: float = 0.0
    total_trades: int = 0
    win_rate: float = 0.0
    profit_loss_ratio: float = 0.0
    turnover: float = 0.0


def calculate_metrics(
    nav_df: pd.DataFrame,
    benchmark_df: Optional[pd.DataFrame] = None,
    risk_free_rate: float = 0.02,
    fills: Optional[List] = None,
) -> MetricsResult:
    """计算回测绩效指标。

    Args:
        nav_df: 策略净值 DataFrame，必须包含 'nav' 列，index 为日期。
        benchmark_df: 基准净值 DataFrame，可选。
        risk_free_rate: 无风险利率（年化），默认 2%。
        fills: 成交记录列表，用于统计交易次数等。

    Returns:
        MetricsResult 绩效结果。
    """
    if nav_df.empty or "nav" not in nav_df.columns:
        return MetricsResult()

    nav = nav_df["nav"]
    returns = nav.pct_change().dropna()

    result = MetricsResult()

    # ---------- 收益指标 ----------
    result.total_return = (nav.iloc[-1] / nav.iloc[0]) - 1 if nav.iloc[0] != 0 else 0.0
    n_years = len(returns) / 252.0
    if n_years > 0:
        result.annual_return = (1 + result.total_return) ** (1 / n_years) - 1

    # ---------- 基准与超额 ----------
    if benchmark_df is not None and not benchmark_df.empty and "close" in benchmark_df.columns:
        bench = benchmark_df["close"]
        result.benchmark_return = (bench.iloc[-1] / bench.iloc[0]) - 1 if bench.iloc[0] != 0 else 0.0
    result.excess_return = result.annual_return - result.benchmark_return

    # ---------- 回撤 ----------
    cummax = nav.cummax()
    drawdown = (nav - cummax) / cummax
    result.max_drawdown = drawdown.min()
    # 回撤持续期（简化：最大回撤谷底到恢复的天数）
    max_dd_end = drawdown.idxmin()
    peak_before = nav.loc[:max_dd_end].idxmax()
    try:
        recovery = nav.loc[max_dd_end:][nav.loc[max_dd_end:] >= nav.loc[peak_before]].index[0]
        result.max_drawdown_duration = (recovery - peak_before).days
    except IndexError:
        result.max_drawdown_duration = (nav.index[-1] - peak_before).days

    # ---------- 风险指标 ----------
    if len(returns) > 1:
        result.volatility = returns.std() * np.sqrt(252)
        excess_daily = returns - risk_free_rate / 252
        if result.volatility > 0:
            result.sharpe_ratio = (excess_daily.mean() * 252) / result.volatility

        downside = returns[returns < 0]
        if len(downside) > 0 and downside.std() > 0:
            result.sortino_ratio = (returns.mean() * 252) / (downside.std() * np.sqrt(252))

    # ---------- Beta / Alpha / IR ----------
    if benchmark_df is not None and not benchmark_df.empty and "close" in benchmark_df.columns:
        bench_returns = benchmark_df["close"].pct_change().dropna()
        aligned = pd.concat([returns, bench_returns], axis=1).dropna()
        if len(aligned) > 1:
            cov = aligned.cov().iloc[0, 1]
            bench_var = aligned.iloc[:, 1].var()
            if bench_var > 0:
                result.beta = cov / bench_var
                result.alpha = result.annual_return - (risk_free_rate + result.beta * (result.benchmark_return - risk_free_rate))

            tracking_error = (aligned.iloc[:, 0] - aligned.iloc[:, 1]).std() * np.sqrt(252)
            if tracking_error > 0:
                result.information_ratio = result.excess_return / tracking_error

    # ---------- 交易统计 ----------
    if fills is not None:
        result.total_trades = len(fills)
        profits = []
        # 简化：无法精确配对每笔盈亏，仅统计买入/卖出次数
        # 实际项目可维护逐笔盈亏映射
        result.win_rate = 0.0
        result.profit_loss_ratio = 0.0

    return result
