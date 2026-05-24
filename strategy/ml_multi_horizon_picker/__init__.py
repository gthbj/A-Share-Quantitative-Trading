"""多 Horizon 机器学习择时择股策略。

详见 `PRD/PRD_20260524_05_*` 和本目录 `README.md`。
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
