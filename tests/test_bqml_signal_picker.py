from __future__ import annotations

import pandas as pd

from account.portfolio import Portfolio
from data_layer.bigquery_source import BigQueryDataSource
from strategy.base_strategy import Context
from strategy.bqml_signal_picker import BQMLSignalPickerStrategy


class FakeSignalDataSource:
    def get_bqml_signal_candidates(self, **kwargs):
        return pd.DataFrame(
            [
                {"date": "20250102", "equity_code": "000001.SZ", "prob_up": 0.6, "score_rank": 1},
                {"date": "20250102", "equity_code": "600000.SH", "prob_up": 0.5, "score_rank": 2},
                {"date": "20250103", "equity_code": "000001.SZ", "prob_up": 0.7, "score_rank": 1},
            ]
        )

    def get_bars(self, *args, **kwargs):  # pragma: no cover - not used by this unit test
        return pd.DataFrame()

    def get_stock_list(self):  # pragma: no cover
        return pd.DataFrame()

    def get_index_constituents(self, index_code):  # pragma: no cover
        return []


def test_bqml_signal_picker_sets_universe_from_ads_signals():
    strategy = BQMLSignalPickerStrategy(max_positions=2, candidate_pool_size=2)
    context = Context(
        portfolio=Portfolio(100_000),
        data_source=FakeSignalDataSource(),
        current_date="20250102",
    )

    strategy.initialize(context)

    assert strategy.get_universe() == ["000001.SZ", "600000.SH"]


def test_bqml_signal_picker_buys_top_candidates_with_cash_limit():
    strategy = BQMLSignalPickerStrategy(max_positions=2, candidate_pool_size=2, position_pct=0.95)
    context = Context(
        portfolio=Portfolio(100_000),
        data_source=FakeSignalDataSource(),
        current_date="20250102",
    )
    strategy.initialize(context)

    strategy.handle_data(
        context,
        {
            "000001.SZ": pd.Series({"close": 10.0}),
            "600000.SH": pd.Series({"close": 20.0}),
        },
    )
    orders = context.pop_orders()

    assert [order.code for order in orders] == ["000001.SZ", "600000.SH"]
    assert all(order.qty % 100 == 0 for order in orders)


def test_bigquery_source_does_not_route_000_sz_stock_as_index():
    assert not BigQueryDataSource._is_index_code("000001.SZ")
    assert BigQueryDataSource._is_index_code("000300.SH")
    assert BigQueryDataSource._is_index_code("399001.SZ")


def test_bigquery_source_filters_special_board_codes_by_default():
    source = BigQueryDataSource(
        project_id="data-aquarium",
        dataset="ashare",
        use_cache=False,
    )

    assert not source._is_code_allowed_by_permissions("920123.SH")
    assert not source._is_code_allowed_by_permissions("688001.SH")
    assert not source._is_code_allowed_by_permissions("300001.SZ")
    assert source._is_code_allowed_by_permissions("000001.SZ")
    assert source._filter_codes_by_permissions(
        ["920123.SH", "688001.SH", "300001.SZ", "000001.SZ"]
    ) == ["000001.SZ"]


def test_bigquery_source_allows_special_board_codes_when_permission_enabled():
    source = BigQueryDataSource(
        project_id="data-aquarium",
        dataset="ashare",
        use_cache=False,
        trading_permissions={
            "allow_bse": True,
            "allow_star_market": True,
            "allow_chinext": True,
        },
    )

    assert source._is_code_allowed_by_permissions("920123.SH")
    assert source._is_code_allowed_by_permissions("688001.SH")
    assert source._is_code_allowed_by_permissions("300001.SZ")


def test_bqml_signal_query_applies_security_permission_filter(monkeypatch):
    source = BigQueryDataSource(
        project_id="data-aquarium",
        dataset="ashare",
        use_cache=False,
        tables={"dim_security": "dwd_dim_security"},
    )
    captured = {}

    def fake_execute(sql: str, max_retries: int = 3):
        captured["sql"] = sql
        return pd.DataFrame()

    monkeypatch.setattr(source, "_execute_sql", fake_execute)

    source.get_bqml_signal_candidates(
        start_date="20250301",
        end_date="20250331",
        candidate_pool_size=3,
    )

    sql = captured["sql"]
    assert "LEFT JOIN `data-aquarium.ashare.dwd_dim_security` AS s" in sql
    assert "a.is_selected" in sql
    assert "a.score_rank <= 3" in sql
    assert "s.security_code IS NOT NULL" in sql
    assert "COALESCE(s.exchange_code, '') != 'BSE'" in sql
    assert "r'^(688|689)'" in sql
    assert "r'^(300|301)'" in sql
    assert "r'\\*?ST'" in sql
