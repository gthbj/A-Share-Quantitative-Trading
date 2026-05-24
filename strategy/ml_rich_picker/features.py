"""ml_rich_picker 富特征定义（PRD_20260525_03）。

复用 ml_multi_horizon_picker 的 17 维日线技术面 + 5 维 sell-side 风险特征，
在其上扩展：

- 8 维基本面（来自 dws_equity_fundamental_features）
- 5 维资金流/事件（来自 dws_equity_event_money_flow_features_1d）

总特征维度：
    Buy 模型: 17 (daily) + 8 (fundamental) + 5 (event) = 30
    Sell 模型: 30 + 5 (sell-side risk) = 35
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


# 完整的 buy / sell 特征列定义
RICH_BUY_FEATURE_COLUMNS: List[str] = (
    DAILY_FEATURE_COLUMNS         # 17 维
    + FUNDAMENTAL_FEATURE_COLUMNS  # 8 维
    + EVENT_FEATURE_COLUMNS        # 5 维
)  # = 30

RICH_SELL_FEATURE_COLUMNS: List[str] = (
    RICH_BUY_FEATURE_COLUMNS       # 30 维
    + SELL_RISK_FEATURE_COLUMNS    # 5 维
)  # = 35


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
    tech = (
        df.get("return_5d", 0).fillna(0).astype(float) * 1.0
        + df.get("return_20d", 0).fillna(0).astype(float) * 0.5
        + (50 - (df.get("rsi_14", 50).fillna(50).astype(float) - 50).abs()) / 100
        + df.get("close_to_ma20", 0).fillna(0).astype(float) * 0.3
        - df.get("std_20d", 0).fillna(0).astype(float) * 0.01
    )

    # 基本面加分（低 PE/PB + 高 ROE）
    pe = df.get("pe_basic", np.nan).astype(float)
    pe_score = np.where(
        (pe > 0) & (pe < 30),
        1.0 / (pe.clip(lower=1) + 1),   # 越低 PE 分越高
        0.0,
    )
    pb_score = np.where(
        (df.get("pb", np.nan).astype(float) > 0)
        & (df.get("pb", np.nan).astype(float) < 5),
        1.0 / (df.get("pb", 1).astype(float).clip(lower=0.5) + 1),
        0.0,
    )
    roe_score = df.get("roe", 0).fillna(0).astype(float).clip(-30, 30) / 100

    fundamental = pe_score * 0.5 + pb_score * 0.3 + roe_score * 0.5

    # 资金流加分
    main_flow = df.get("main_net_inflow_pct", 0).fillna(0).astype(float).clip(-1, 1)
    dragon = df.get("dragon_tiger_net_pct", 0).fillna(0).astype(float).clip(-1, 1)
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
    raw = (
        # 与 v1 一致的风险因子
        -df.get("drawdown_from_high_20d", 0).fillna(0).astype(float) * 3
        + (df.get("vol_expansion", 1).fillna(1).astype(float) - 1).clip(0, None) * 2
        + (df.get("rsi_overbought_streak", 0).fillna(0).astype(float) > 5).astype(float) * 0.3
        + (df.get("return_5d", 0).fillna(0).astype(float) < -0.05).astype(float) * 0.3
        # 富特征新增风险因子
        + df.get("debt_to_assets", 0).fillna(0).astype(float).clip(0, 1) * 0.3
        - df.get("dragon_tiger_net_pct", 0).fillna(0).astype(float).clip(-1, 0) * 0.5  # 净卖出（负值）
    )
    return 1 / (1 + np.exp(-raw * 3))
