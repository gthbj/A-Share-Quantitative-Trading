"""ml_rich_picker — 富特征 ML 择时择股策略（PRD_20260525_03）。

在 ml_multi_horizon_picker (v1) 的 17 维日线技术面基础上扩展：
- 8 维基本面（PE/PB/ROE/毛利率/资产负债 等）
- 5 维资金流/事件（龙虎榜/主力净流入/涨停连板/开盘啦）

共 30 维 buy / 39 维 sell。

详见 PRD_20260525_03 与 README.md。
"""

from strategy.ml_rich_picker.features import (
    DAILY_FEATURE_COLUMNS,
    DEFAULT_MAX_REMAINING_DAYS,
    EVENT_FEATURE_COLUMNS,
    FUNDAMENTAL_FEATURE_COLUMNS,
    POSITION_STATE_FEATURE_COLUMNS,
    RICH_BUY_FEATURE_COLUMNS,
    RICH_SELL_FEATURE_COLUMNS,
    SELL_REGRESSION_FEATURE_COLUMNS,
    deterministic_optimal_remaining_days,
    deterministic_rich_score,
    deterministic_rich_sell_score,
    remaining_days_to_prob_sell,
)
from strategy.ml_rich_picker.model_storage import (
    SELL_REMAINING_DAYS_MODEL_NAME,
    load_rich_bundle,
    save_rich_bundle,
)
from strategy.ml_rich_picker.strategy import MLRichPickerStrategy

__all__ = [
    "MLRichPickerStrategy",
    "DAILY_FEATURE_COLUMNS",
    "FUNDAMENTAL_FEATURE_COLUMNS",
    "EVENT_FEATURE_COLUMNS",
    "POSITION_STATE_FEATURE_COLUMNS",
    "RICH_BUY_FEATURE_COLUMNS",
    "RICH_SELL_FEATURE_COLUMNS",
    "SELL_REGRESSION_FEATURE_COLUMNS",
    "DEFAULT_MAX_REMAINING_DAYS",
    "SELL_REMAINING_DAYS_MODEL_NAME",
    "deterministic_rich_score",
    "deterministic_rich_sell_score",
    "deterministic_optimal_remaining_days",
    "remaining_days_to_prob_sell",
    "load_rich_bundle",
    "save_rich_bundle",
]
