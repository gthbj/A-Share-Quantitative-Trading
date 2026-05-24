"""多 Horizon 机器学习择时择股策略。

详见 `PRD/PRD_20260524_12_*`（核心策略，原 _05）和 `PRD/PRD_20260524_13_*`
（走步重训 + 可交易过滤，原 _06）以及本目录 `README.md`。
"""

from strategy.ml_multi_horizon_picker.model_registry import ModelRegistry, RegistryEntry, build_registry
from strategy.ml_multi_horizon_picker.strategy import MLMultiHorizonStrategy
from strategy.ml_multi_horizon_picker.tradable import (
    DEFAULT_TRADING_PERMISSIONS,
    classify_board,
    filter_codes,
    is_tradable_code,
    merge_permissions,
)

__all__ = [
    "MLMultiHorizonStrategy",
    "ModelRegistry",
    "RegistryEntry",
    "build_registry",
    "DEFAULT_TRADING_PERMISSIONS",
    "merge_permissions",
    "is_tradable_code",
    "filter_codes",
    "classify_board",
]
