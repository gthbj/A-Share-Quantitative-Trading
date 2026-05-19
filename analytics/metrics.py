"""绩效指标计算：收益、风险、交易统计。"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import List, Optional

import math
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


def _pair_fifo(fills: List) -> List[float]:
    """按 FIFO 顺序配对买卖成交，返回每对的"净盈亏（含费用）"列表。

    每只股票独立配对：
      - 维护一个 FIFO 队列存储未平仓的买入（qty, price, fee_per_share）
      - 收到卖出 fill 时，按队首买入逐步配对，累计盈亏
      - 配对的盈亏 = (卖出价 - 买入价) * 配对数量 - 该数量对应的买卖费用
      - 最后未配对的买入忽略（仍持仓）

    Args:
        fills: 时间顺序排列的 Fill 列表。

    Returns:
        每个完整配对的净盈亏列表，正数为盈利，负数为亏损。
    """
    from engine.trade_engine import OrderSide

    profits: List[float] = []
    # code -> 未平仓买入队列：deque[(qty, price, fee_per_share)]
    open_buys: dict = defaultdict(deque)

    for fill in fills:
        qty = int(fill.qty)
        price = float(fill.price)
        total_fee = float(fill.total_cost)
        fee_per_share = total_fee / qty if qty > 0 else 0.0

        if fill.side == OrderSide.BUY:
            open_buys[fill.code].append([qty, price, fee_per_share])
            continue

        # 卖出：FIFO 配对
        remaining = qty
        sell_fee_per_share = fee_per_share
        queue = open_buys[fill.code]
        while remaining > 0 and queue:
            buy_qty, buy_price, buy_fee_pps = queue[0]
            match_qty = min(remaining, buy_qty)
            gross = (price - buy_price) * match_qty
            fee_for_pair = (buy_fee_pps + sell_fee_per_share) * match_qty
            profits.append(gross - fee_for_pair)
            remaining -= match_qty
            buy_qty -= match_qty
            if buy_qty == 0:
                queue.popleft()
            else:
                queue[0][0] = buy_qty

    return profits


def _annual_factor(frequency: str) -> int:
    """根据回测频率返回年化系数。"""
    factors = {
        "daily": 252,
        "1min": 252 * 240,
        "5min": 252 * 48,
        "15min": 252 * 16,
        "30min": 252 * 8,
        "60min": 252 * 4,
    }
    return factors.get(frequency, 252)


def calculate_metrics(
    nav_df: pd.DataFrame,
    benchmark_df: Optional[pd.DataFrame] = None,
    risk_free_rate: float = 0.02,
    fills: Optional[List] = None,
    frequency: str = "daily",
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
    ann_factor = _annual_factor(frequency)

    result.total_return = (nav.iloc[-1] / nav.iloc[0]) - 1 if nav.iloc[0] != 0 else 0.0
    n_years = len(returns) / ann_factor
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
        result.volatility = returns.std() * np.sqrt(ann_factor)
        excess_period = returns - risk_free_rate / ann_factor
        if result.volatility > 0:
            result.sharpe_ratio = (excess_period.mean() * ann_factor) / result.volatility

        downside = returns[returns < 0]
        if len(downside) > 0 and downside.std() > 0:
            result.sortino_ratio = (returns.mean() * ann_factor) / (downside.std() * np.sqrt(ann_factor))

    # ---------- Beta / Alpha / IR ----------
    # 基准固定为日线（见 PRD_20260520_03），策略 returns 在分钟级时需重采样到日线对齐
    if benchmark_df is not None and not benchmark_df.empty and "close" in benchmark_df.columns:
        if frequency != "daily":
            # 把分钟级 nav 按日重采样取最后一根 bar 的净值 → 日收益
            try:
                strategy_daily_nav = nav.resample("D").last().dropna()
            except (ValueError, TypeError):
                strategy_daily_nav = nav
            strategy_daily_returns = strategy_daily_nav.pct_change().dropna()
            daily_ann = 252  # 已对齐到日线，统一用日线年化系数
        else:
            strategy_daily_returns = returns
            daily_ann = ann_factor

        bench_returns = benchmark_df["close"].pct_change().dropna()
        aligned = pd.concat([strategy_daily_returns, bench_returns], axis=1).dropna()
        # 至少 20 个对齐日线点才计算 Beta，否则结果不可信
        if len(aligned) >= 20:
            cov = aligned.cov().iloc[0, 1]
            bench_var = aligned.iloc[:, 1].var()
            if bench_var > 0:
                result.beta = cov / bench_var
                result.alpha = result.annual_return - (
                    risk_free_rate + result.beta * (result.benchmark_return - risk_free_rate)
                )

            tracking_error = (aligned.iloc[:, 0] - aligned.iloc[:, 1]).std() * np.sqrt(daily_ann)
            if tracking_error > 0:
                result.information_ratio = result.excess_return / tracking_error

    # ---------- 交易统计（FIFO 盈亏配对）----------
    if fills:
        profits = _pair_fifo(fills)
        result.total_trades = len(profits)
        if profits:
            winners = [p for p in profits if p > 0]
            losers = [p for p in profits if p < 0]
            result.win_rate = len(winners) / len(profits)
            total_win = sum(winners)
            total_loss = abs(sum(losers))
            if total_loss > 0:
                result.profit_loss_ratio = total_win / total_loss
            else:
                # 无亏损：用 inf 而非 0，避免与"无成交"混淆
                result.profit_loss_ratio = float("inf") if total_win > 0 else 0.0

    return result
