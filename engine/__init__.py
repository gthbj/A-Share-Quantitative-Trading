"""引擎模块：回测引擎、交易撮合引擎与虚拟盘。"""

from .trade_engine import Fill, Order, OrderSide, OrderType, TradeEngine

__all__ = [
    "TradeEngine",
    "Order",
    "OrderType",
    "OrderSide",
    "Fill",
    "BacktestEngine",
    "PaperTrader",
]


def __getattr__(name: str):
    """Lazy-import heavier submodules to avoid circular imports."""
    if name == "BacktestEngine":
        from .backtest import BacktestEngine
        return BacktestEngine
    if name == "PaperTrader":
        from .paper_trader import PaperTrader
        return PaperTrader
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
