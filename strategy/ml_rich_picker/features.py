"""ml_rich_picker 富特征定义（PRD_20260525_03）。

复用 ml_multi_horizon_picker 的 17 维日线技术面 + 5 维 sell-side 风险特征，
在其上扩展：

- 8 维基本面（来自 dws_equity_fundamental_features）
- 5 维资金流/事件（来自 dws_equity_event_money_flow_features_1d）

总特征维度：
    Buy 模型: 17 (daily) + 8 (fundamental) + 5 (event) = 30
    Sell 模型: 30 + 5 (sell-side risk) + 4 (position state) = 39
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


# 4 维持仓状态特征。sell 模型是“当前持仓是否应在下一开盘卖出”的
# 决策模型，必须看到成本、持仓年龄、持仓高点回撤等状态。
POSITION_STATE_FEATURE_COLUMNS: List[str] = [
    "holding_days",
    "position_return",
    "drawdown_from_position_peak",
    "days_to_expected_horizon",
]


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
    """模型缺失时的兜底 sell prob 评分函数（rich 版本）。

    在 v1 deterministic_sell_score 上加：
        - 资产负债率高 → 风险加分
        - 净利率快速下降 → 风险加分（暂未实现，需要历史 diff）
        - 龙虎榜净卖出 → 风险加分
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
