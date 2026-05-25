"""ml_rich_picker 富特征定义（PRD_20260525_03）。

复用 ml_multi_horizon_picker 的 17 维日线技术面 + 5 维 sell-side 风险特征，
在其上扩展：

- 8 维基本面（来自 dws_equity_fundamental_features）
- 5 维资金流/事件（来自 dws_equity_event_money_flow_features_1d）

总特征维度：
    Buy 模型: 17 (daily) + 8 (fundamental) + 5 (event) = 30
    Sell 模型（回归 ``optimal_remaining_days``）: 30 + 5 (sell-side risk) = 35

注：sell 回归模型预测的是「未来若干天内风险可控的最优卖出剩余天数」，
属于纯市场预测，不依赖具体持仓状态。持仓状态（holding_days /
position_return 等）由策略层在 sell trigger 时单独合成判断，
不再喂给 sell 模型。POSITION_STATE_FEATURE_COLUMNS 保留供
诊断 / 旧 sell_v1 二分类模型回退使用。
"""

from __future__ import annotations

from typing import List

import numpy as np
import pandas as pd

# 复用 v1 的 17 维 + 5 维风险特征常量
from strategy.ml_multi_horizon_picker.features import (
    BUY_FEATURE_COLUMNS as DAILY_FEATURE_COLUMNS,
    SELL_RISK_FEATURE_COLUMNS,
    compute_sell_risk_features,
)


# 8 维基本面特征（dws_equity_fundamental_features 直接选）
FUNDAMENTAL_FEATURE_COLUMNS: List[str] = [
    "pe_basic",
    "pb",
    "roe",
    "gross_margin",
    "net_margin",
    "debt_to_assets",
    "log_market_cap",      # 派生：LN(market_cap + 1)
    "asset_turnover",
]


# 5 维资金流/事件特征（dws_equity_event_money_flow_features_1d 派生）
EVENT_FEATURE_COLUMNS: List[str] = [
    "net_inflow_pct",          # net_inflow_amount / amount
    "main_net_inflow_pct",     # main_net_inflow_amount / amount
    "dragon_tiger_net_pct",    # dragon_tiger_net_amount / amount
    "limit_up_streak",         # 连续涨停天数
    "is_kpl_event_int",        # 开盘啦事件 0/1
]


# 4 维持仓状态特征。仅供策略层 sell trigger 合成 / 诊断 / 兼容旧
# sell_v1 二分类模型回退使用；不再喂给新的 sell 回归模型。
POSITION_STATE_FEATURE_COLUMNS: List[str] = [
    "holding_days",
    "position_return",
    "drawdown_from_position_peak",
    "days_to_expected_horizon",
]


# sell 回归模型默认输出上限（天）：模型预测值会被 clip 到 [0, MAX_REMAINING_DAYS]
# 区间。和 walk_forward_config.yaml 里的 sell_lookforward 保持一致。
DEFAULT_MAX_REMAINING_DAYS: float = 20.0


def _numeric_feature(
    df: pd.DataFrame,
    column: str,
    default: float = 0.0,
) -> pd.Series:
    """取数值特征列；列缺失时返回与 df 等长的默认 Series。"""
    if column in df.columns:
        values = df[column]
    else:
        values = pd.Series(default, index=df.index)
    return pd.to_numeric(values, errors="coerce").fillna(default).astype(float)


# 完整的 buy / sell 特征列定义
RICH_BUY_FEATURE_COLUMNS: List[str] = (
    DAILY_FEATURE_COLUMNS         # 17 维
    + FUNDAMENTAL_FEATURE_COLUMNS  # 8 维
    + EVENT_FEATURE_COLUMNS        # 5 维
)  # = 30

# Sell 回归模型（``optimal_remaining_days``）的输入特征：30 buy + 5 sell-side
# 风险共 35 维。label 是市场未来最优卖出窗口，不依赖具体何时进场，所以
# position state 不参与训练；持仓状态由策略层在 sell trigger 时合成。
SELL_REGRESSION_FEATURE_COLUMNS: List[str] = (
    RICH_BUY_FEATURE_COLUMNS       # 30 维
    + SELL_RISK_FEATURE_COLUMNS    # 5 维
)  # = 35


# 旧的 39 维 sell schema（30 buy + 5 sell-side + 4 position state）。
# 仅保留用于：
# 1. 向后兼容 v1 二分类 sell_v1.pkl 模型路径（策略可选择降级）
# 2. 诊断 / 单元测试
# 新的回归 sell 模型一律走 SELL_REGRESSION_FEATURE_COLUMNS。
RICH_SELL_FEATURE_COLUMNS: List[str] = (
    RICH_BUY_FEATURE_COLUMNS       # 30 维
    + SELL_RISK_FEATURE_COLUMNS    # 5 维
    + POSITION_STATE_FEATURE_COLUMNS  # 4 维
)  # = 39


def deterministic_rich_score(feature_df: pd.DataFrame) -> pd.Series:
    """模型缺失时的兜底 buy 评分函数（rich 版本）。

    在 v1 deterministic_score 基础上加入：
        - 低 PE / 低 PB 加分
        - 高 ROE 加分
        - 主力净流入正向加分
        - 龙虎榜净买入加分

    注意：此函数仅用于管道测试 / CI。生产回测需训练好的模型。
    """
    df = feature_df.copy()

    # 技术面 base score（与 v1 一致）
    return_5d = _numeric_feature(df, "return_5d")
    return_20d = _numeric_feature(df, "return_20d")
    rsi_14 = _numeric_feature(df, "rsi_14", 50.0)
    close_to_ma20 = _numeric_feature(df, "close_to_ma20")
    std_20d = _numeric_feature(df, "std_20d")
    tech = (
        return_5d * 1.0
        + return_20d * 0.5
        + (50 - (rsi_14 - 50).abs()) / 100
        + close_to_ma20 * 0.3
        - std_20d * 0.01
    )

    # 基本面加分（低 PE/PB + 高 ROE）
    pe = _numeric_feature(df, "pe_basic", np.nan)
    pe_score = np.where(
        (pe > 0) & (pe < 30),
        1.0 / (pe.clip(lower=1) + 1),   # 越低 PE 分越高
        0.0,
    )
    pb = _numeric_feature(df, "pb", np.nan)
    pb_score = np.where(
        (pb > 0) & (pb < 5),
        1.0 / (pb.clip(lower=0.5) + 1),
        0.0,
    )
    roe_score = _numeric_feature(df, "roe").clip(-30, 30) / 100

    fundamental = pe_score * 0.5 + pb_score * 0.3 + roe_score * 0.5

    # 资金流加分
    main_flow = _numeric_feature(df, "main_net_inflow_pct").clip(-1, 1)
    dragon = _numeric_feature(df, "dragon_tiger_net_pct").clip(-1, 1)
    flow = main_flow * 0.5 + dragon * 0.3

    return tech + fundamental + flow


def deterministic_rich_sell_score(feature_df: pd.DataFrame) -> pd.Series:
    """[兼容] 旧 binary sell 模型的 deterministic fallback：返回 prob ∈ [0,1]。

    新策略默认使用 ``deterministic_optimal_remaining_days`` 配套
    新回归模型；本函数仅保留给单测 / 旧 sell_v1 binary fallback 用。
    """
    df = feature_df.copy()
    drawdown_high = _numeric_feature(df, "drawdown_from_high_20d")
    vol_expansion = _numeric_feature(df, "vol_expansion", 1.0)
    overbought = _numeric_feature(df, "rsi_overbought_streak")
    return_5d = _numeric_feature(df, "return_5d")
    position_return = _numeric_feature(df, "position_return")
    position_drawdown = _numeric_feature(df, "drawdown_from_position_peak")
    days_to_horizon = _numeric_feature(df, "days_to_expected_horizon", 5.0)
    debt_to_assets = _numeric_feature(df, "debt_to_assets")
    dragon = _numeric_feature(df, "dragon_tiger_net_pct")
    raw = (
        # 与 v1 一致的风险因子
        -drawdown_high * 3
        + (vol_expansion - 1).clip(0, None) * 2
        + (overbought > 5).astype(float) * 0.3
        + (return_5d < -0.05).astype(float) * 0.3
        + (position_return < -0.03).astype(float) * 0.4
        - position_drawdown.clip(-1, 0) * 0.6
        + (days_to_horizon <= 0).astype(float) * 0.2
        # 富特征新增风险因子
        + debt_to_assets.clip(0, 1) * 0.3
        - dragon.clip(-1, 0) * 0.5  # 净卖出（负值）
    )
    return 1 / (1 + np.exp(-raw * 3))


def deterministic_optimal_remaining_days(
    feature_df: pd.DataFrame,
    max_remaining_days: float = DEFAULT_MAX_REMAINING_DAYS,
) -> pd.Series:
    """新 sell 回归模型缺失时的兜底：返回估算的 ``optimal_remaining_days``。

    设计思路：把"风险信号"映射成"该多快卖出"——风险越大、剩余天数越少。
    输出 clip 到 [0, max_remaining_days]，与训练 label 同口径，方便策略层
    用统一的 ``remaining_days <= threshold`` 触发卖出。

    特征来源仅限于 ``SELL_REGRESSION_FEATURE_COLUMNS``（30 buy + 5 sell-side
    risk）。任何持仓状态由策略层另行合成，本函数不接收。
    """
    df = feature_df.copy()
    drawdown_high_20d = _numeric_feature(df, "drawdown_from_high_20d")
    drawdown_high_60d = _numeric_feature(df, "drawdown_from_high_60d")
    vol_expansion = _numeric_feature(df, "vol_expansion", 1.0)
    overbought_streak = _numeric_feature(df, "rsi_overbought_streak")
    dist_to_ma60 = _numeric_feature(df, "dist_to_ma60")
    return_5d = _numeric_feature(df, "return_5d")
    return_20d = _numeric_feature(df, "return_20d")
    debt_to_assets = _numeric_feature(df, "debt_to_assets")
    dragon = _numeric_feature(df, "dragon_tiger_net_pct")
    main_flow = _numeric_feature(df, "main_net_inflow_pct")
    rsi = _numeric_feature(df, "rsi_14", 50.0)

    # 风险信号（越大越紧急卖）
    risk = (
        -drawdown_high_20d.clip(-1, 0) * 4          # 近 20 日深度回撤
        + -drawdown_high_60d.clip(-1, 0) * 2        # 近 60 日深度回撤
        + (vol_expansion - 1).clip(0, None) * 1.5   # 波动率扩张
        + (overbought_streak > 5).astype(float)     # 连续超买（RSI > 70）
        + (return_5d < -0.05).astype(float) * 0.5   # 近 5 日大跌
        + debt_to_assets.clip(0, 1) * 0.3           # 资产负债率高
        - dragon.clip(-1, 0) * 1.5                  # 龙虎榜净卖出（负值翻正）
        + (rsi > 80).astype(float) * 0.4            # 极端超买
    )

    # 持有动机（越大越值得继续持有）
    hold = (
        return_20d.clip(-0.5, 0.5)                  # 近 20 日趋势
        + main_flow.clip(-1, 1) * 0.5               # 主力净流入
        + dist_to_ma60.clip(-1, 1) * 0.3            # 强势远离 60 日线
    )

    # 把 (risk - hold) 映射到 [0, max_remaining_days]
    # risk 大 → remaining_days 接近 0；hold 大 → remaining_days 接近上限
    signal = risk - hold
    # sigmoid 让映射平滑，阈值 0 对应 max/2
    urgency = 1.0 / (1.0 + np.exp(-signal))   # ∈ (0, 1)，越大越紧急
    remaining = (1.0 - urgency) * max_remaining_days
    return pd.Series(remaining, index=df.index).clip(0.0, max_remaining_days)


def remaining_days_to_prob_sell(
    remaining_days: pd.Series,
    threshold: float = 1.0,
    sharpness: float = 1.5,
) -> pd.Series:
    """把回归输出的 ``optimal_remaining_days`` 转成父类 sell trigger 用的
    ``prob_sell`` ∈ [0, 1]。

    ``prob_sell`` 仅作为父类二分阈值接口的桥接，不再代表概率：
    - ``remaining_days <= threshold`` → prob 显著 > 0.5 → 触发卖出
    - ``remaining_days >> threshold`` → prob 接近 0 → 不卖

    Args:
        remaining_days: 模型预测的剩余天数（>= 0 的浮点）
        threshold: 卖出阈值。剩余天数小于等于该值则触发
        sharpness: sigmoid 陡峭度。值越大越接近硬阈值
    """
    rd = pd.to_numeric(remaining_days, errors="coerce").astype(float)
    return 1.0 / (1.0 + np.exp(sharpness * (rd - threshold)))
