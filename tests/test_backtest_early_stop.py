"""BacktestEngine 相对沪深300超额亏损熔断测试。"""

from __future__ import annotations

from typing import List

import pandas as pd

from data_layer.base_data_source import BaseDataSource
from engine.backtest import BacktestEngine
from strategy.base_strategy import BaseStrategy


DATES = ["20240102", "20240103", "20240104", "20240105"]


class _EarlyStopDataSource(BaseDataSource):
    def get_multi_bars(self, codes, start_date, end_date, period="daily", adjust="qfq"):
        return {str(code): _make_stock_frame(str(code), DATES) for code in codes}

    def get_bars(self, code, start_date, end_date, period="daily", adjust="qfq"):
        if str(code) == "000300.SH":
            return _make_benchmark_frame(str(code), DATES, [100.0, 105.0, 111.0, 112.0])
        return _make_stock_frame(str(code), DATES)

    def get_stock_list(self):
        return pd.DataFrame()

    def get_index_constituents(self, index_code):
        return []


def _make_stock_frame(code: str, dates: List[str]) -> pd.DataFrame:
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


def _make_benchmark_frame(code: str, dates: List[str], closes: List[float]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "code": code,
                "date": date,
                "open": close,
                "high": close,
                "low": close,
                "close": close,
                "volume": 1_000_000,
                "amount": close * 1_000_000,
            }
            for date, close in zip(dates, closes)
        ]
    )


class _NoTradeStrategy(BaseStrategy):
    def initialize(self, context):
        super().initialize(context)
        self.set_universe(["600000.SH"])

    def handle_data(self, context, data):
        pass


def _make_engine(threshold):
    return BacktestEngine(
        strategy_cls=_NoTradeStrategy,
        data_source=_EarlyStopDataSource(),
        start_date="20240102",
        end_date="20240105",
        initial_capital=100_000,
        benchmark="000300.SH",
        frequency="daily",
        daily_log_enabled=True,
        comparison_benchmarks={"hs300": "000300.SH"},
        early_stop_excess_vs_hs300=threshold,
    )


def test_backtest_stops_when_excess_loss_reaches_threshold():
    engine = _make_engine(-0.10)

    result = engine.run()

    assert engine.early_stop_triggered is True
    assert engine.early_stop_date == "20240104"
    assert engine.early_stop_value is not None
    assert engine.early_stop_value <= -0.10
    assert result.index.strftime("%Y%m%d").tolist() == [
        "20240102",
        "20240103",
        "20240104",
    ]


def test_backtest_does_not_stop_when_threshold_disabled():
    engine = _make_engine(None)

    result = engine.run()

    assert engine.early_stop_triggered is False
    assert engine.early_stop_date == ""
    assert result.index.strftime("%Y%m%d").tolist() == DATES
