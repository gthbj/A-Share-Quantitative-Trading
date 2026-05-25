"""单元测试公共 fixture。"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

# 让 tests/ 之外的源代码可被 import
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from account.portfolio import Portfolio
from engine.trade_engine import TradeEngine


@pytest.fixture
def portfolio() -> Portfolio:
    """初始资金 100 万的空账户。"""
    return Portfolio(initial_capital=1_000_000.0)


@pytest.fixture
def trade_engine() -> TradeEngine:
    """默认参数的撮合引擎（与 config/backtest.yaml 默认一致）。"""
    return TradeEngine(
        commission_rate=0.0001,
        min_commission=0.0,
        stamp_duty_rate=0.0005,
        transfer_fee_rate=0.00001,
        slippage_type="percent",
        slippage_value=0.001,
        volume_limit=0.10,
        price_type="next_open",
    )


def make_bar(
    *,
    open_: float = 10.0,
    high: float = 10.5,
    low: float = 9.5,
    close: float = 10.2,
    volume: int = 1_000_000,
    prev_close: float = 10.0,
) -> pd.Series:
    """构造一根 K 线 bar。"""
    return pd.Series(
        {
            "open": open_,
            "high": high,
            "low": low,
            "close": close,
            "volume": volume,
            "prev_close": prev_close,
        }
    )


@pytest.fixture
def bar() -> pd.Series:
    """常用 bar（涨跌停区间内、量足）。"""
    return make_bar()


@pytest.fixture
def bar_factory():
    """允许测试用例按需自定义 bar 字段。"""
    return make_bar
