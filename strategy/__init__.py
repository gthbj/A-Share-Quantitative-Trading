"""策略模块：策略基类与示例策略。"""

__all__ = [
    "BaseStrategy",
    "Context",
    "DoubleMAStrategy",
    "MomentumStrategy",
    "MultiFactorStrategy",
    "IntradayMAStrategy",
]


def __getattr__(name: str):
    """Lazy-import strategy classes to avoid circular imports."""
    if name == "BaseStrategy":
        from .base_strategy import BaseStrategy
        return BaseStrategy
    if name == "Context":
        from .base_strategy import Context
        return Context
    if name == "DoubleMAStrategy":
        from .double_ma import DoubleMAStrategy
        return DoubleMAStrategy
    if name == "MomentumStrategy":
        from .momentum import MomentumStrategy
        return MomentumStrategy
    if name == "MultiFactorStrategy":
        from .multi_factor import MultiFactorStrategy
        return MultiFactorStrategy
    if name == "IntradayMAStrategy":
        from .intraday_ma import IntradayMAStrategy
        return IntradayMAStrategy
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
