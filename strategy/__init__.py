"""策略模块：策略基类与示例策略。"""

from .base_strategy import BaseStrategy, Context
from .double_ma import DoubleMAStrategy
from .momentum import MomentumStrategy
from .multi_factor import MultiFactorStrategy

__all__ = [
    "BaseStrategy",
    "Context",
    "DoubleMAStrategy",
    "MomentumStrategy",
    "MultiFactorStrategy",
]
