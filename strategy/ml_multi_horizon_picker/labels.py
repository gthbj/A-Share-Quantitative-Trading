"""多 Horizon + 卖出标签生成。

为 `ml_multi_horizon_picker` 策略提供训练标签：
- 4 个买入 horizon (1/5/10/20 天) 的截面分位 0/1 分类标签
- 1 个卖出标签（未来 5 天最大回撤超过阈值）

设计：
- 标签生成需要"未来 N 天"窗口，因此**仅用于训练**，不在回测路径
- 截面分类需要全市场截面同步，因此函数签名要求 wide-form
- Lookahead Bias 防护：调用方必须保证训练区间 end_date 截止后仍有 buffer 数据
"""

from __future__ import annotations

from typing import List, Optional

import numpy as np
import pandas as pd


def compute_horizon_return(close: pd.Series, horizon: int) -> pd.Series:
    """单只股票未来 horizon 天对数收益。

    返回 Series 长度与输入相同，最后 horizon 行为 NaN。
    """
    log_close = np.log(close.astype(float))
    return log_close.shift(-horizon) - log_close


def compute_max_drawdown(close: pd.Series, lookforward: int) -> pd.Series:
    """单只股票未来 lookforward 天内"最低价相对当前"的最大跌幅。

    返回值为负数（如 -0.06 表示未来 N 天最大跌 6%）。
    """
    closes = close.astype(float).values
    n = len(closes)
    out = np.full(n, np.nan, dtype=float)
    for i in range(n - 1):
        end = min(n, i + 1 + lookforward)
        window = closes[i + 1 : end]
        if len(window) == 0:
            continue
        min_future = window.min()
        out[i] = (min_future - closes[i]) / closes[i]
    return pd.Series(out, index=close.index)


def build_buy_labels_cross_section(
    df: pd.DataFrame,
    horizons: List[int] = (1, 5, 10, 20),
    top_q: float = 0.30,
    bottom_q: float = 0.30,
) -> pd.DataFrame:
    """跨股票截面 top/bottom 分位 0/1 标签。

    Args:
        df: 必须含列 ['date', 'equity_code', 'close']，多股票多日 long-form
        horizons: 标签 horizon 列表
        top_q: 顶部分位（默认 30%）
        bottom_q: 底部分位（默认 30%）

    Returns:
        df + 新增列 'label_h{horizon}'，取值 {0, 1, NaN}
            1 = 当日截面收益 top top_q
            0 = 当日截面收益 bottom bottom_q
            NaN = 中间或样本不足
    """
    if df.empty:
        return df.copy()

    required = {"date", "equity_code", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"df 必须包含 {required}，实际 {set(df.columns)}")

    out = df.sort_values(["equity_code", "date"]).copy()

    # 先按股票算未来收益
    for h in horizons:
        col = f"future_ret_h{h}"
        out[col] = out.groupby("equity_code")["close"].transform(
            lambda x: compute_horizon_return(x, h)
        )

    # 再按日期做截面分位
    for h in horizons:
        ret_col = f"future_ret_h{h}"
        label_col = f"label_h{h}"
        out[label_col] = np.nan

        def _label_section(group: pd.DataFrame) -> pd.Series:
            valid = group.dropna(subset=[ret_col])
            if len(valid) < 10:  # 截面样本太少时整天丢弃
                return pd.Series(np.nan, index=group.index)
            top_th = valid[ret_col].quantile(1 - top_q)
            bot_th = valid[ret_col].quantile(bottom_q)
            labels = pd.Series(np.nan, index=group.index)
            labels[group[ret_col] >= top_th] = 1.0
            labels[group[ret_col] <= bot_th] = 0.0
            return labels

        out[label_col] = (
            out.groupby("date", group_keys=False).apply(_label_section).astype(float)
        )

    return out


def build_sell_label(
    df: pd.DataFrame,
    lookforward: int = 5,
    drawdown_threshold: float = -0.05,
) -> pd.DataFrame:
    """卖出标签：未来 lookforward 天内最大回撤 < drawdown_threshold 则标 1。

    Args:
        df: 含 ['date', 'equity_code', 'close']
        lookforward: 未来观测窗口天数
        drawdown_threshold: 触发阈值（负数）

    Returns:
        df + ['future_max_dd', 'label_sell']
            label_sell ∈ {0, 1}，最后 lookforward 天为 NaN
    """
    if df.empty:
        return df.copy()

    required = {"date", "equity_code", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"df 必须包含 {required}，实际 {set(df.columns)}")

    out = df.sort_values(["equity_code", "date"]).copy()
    out["future_max_dd"] = out.groupby("equity_code")["close"].transform(
        lambda x: compute_max_drawdown(x, lookforward)
    )

    out["label_sell"] = np.nan
    valid_mask = out["future_max_dd"].notna()
    out.loc[valid_mask, "label_sell"] = (
        out.loc[valid_mask, "future_max_dd"] <= drawdown_threshold
    ).astype(float)
    return out
