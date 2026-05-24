"""BacktestEngine warmup 预加载测试（PRD_20260524_15）。"""

from __future__ import annotations

from typing import Dict, List

import pandas as pd

from data_layer.base_data_source import BaseDataSource
from engine.backtest import BacktestEngine
from strategy.base_strategy import BaseStrategy


class _RecordingDataSource(BaseDataSource):
    def __init__(self) -> None:
        self.multi_calls = []

    def get_multi_bars(self, codes, start_date, end_date, period="daily", adjust="qfq"):
        self.multi_calls.append((codes, start_date, end_date, period, adjust))
        return {
            str(code): _make_frame(str(code), ["20240110", "20240111", "20240112"])
            for code in codes
        }

    def get_bars(self, code, start_date, end_date, period="daily", adjust="qfq"):
        return _make_frame(str(code), ["20240110", "20240111", "20240112"])

    def get_stock_list(self):
        return pd.DataFrame()

    def get_index_constituents(self, index_code):
        return []


def _make_frame(code: str, dates: List[str]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": code,
                "date": date,
                "open": 10.0,
                "high": 10.0,
                "low": 10.0,
                "close": 10.0,
                "volume": 1_000_000,
                "amount": 10_000_000,
            }
            for date in dates
        ]
    )


class _WarmupStrategy(BaseStrategy):
    lookback_days = 20

    def initialize(self, context):
        super().initialize(context)
        self.set_universe(["600000.SH"])

    def handle_data(self, context, data):
        pass


def test_backtest_preloads_before_start_date():
    data_source = _RecordingDataSource()
    engine = BacktestEngine(
        strategy_cls=_WarmupStrategy,
        data_source=data_source,
        start_date="20240110",
        end_date="20240112",
        initial_capital=100_000,
        benchmark="000300.SH",
        frequency="daily",
    )

    result = engine.run()

    assert not result.empty
    assert data_source.multi_calls
    _, start_date, end_date, period, _ = data_source.multi_calls[0]
    assert start_date < "20240110"
    assert end_date == "20240112"
    assert period == "daily"
