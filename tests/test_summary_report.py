from __future__ import annotations

from analytics.metrics import MetricsResult
from analytics.summary import generate_markdown_summary


def _config() -> dict:
    return {
        "trading": {
            "commission_rate": 0.00025,
            "min_commission": 5.0,
            "stamp_duty_rate": 0.0005,
            "transfer_fee_rate": 0.00001,
        },
        "slippage": {"type": "percent", "value": 0.003},
        "execution": {"price_type": "next_open", "volume_limit": 0.10, "allow_short": False},
        "stop_loss": {"enabled": True, "threshold": 0.05},
        "data": {
            "bigquery": {
                "project_id": "data-aquarium",
                "tables": {
                    "kline_1d_equity": "dwd_fact_equity_kline_1d",
                    "kline_1d_fund": "dwd_fact_fund_kline_1d",
                    "kline_1d_index": "dwd_fact_index_kline_1d",
                },
            }
        },
    }


def test_summary_does_not_claim_outperformance_when_no_trades(tmp_path):
    path = generate_markdown_summary(
        output_dir=tmp_path,
        metrics=MetricsResult(benchmark_return=-0.1, excess_return=0.1),
        strategy_class_path="strategy.double_ma.DoubleMAStrategy",
        strategy_doc="双均线策略。",
        universe=["510300.SH"],
        start_date="20231201",
        end_date="20231229",
        initial_capital=1_000_000,
        frequency="daily",
        benchmark="000300.SH",
        config=_config(),
        nav_records_count=21,
        fills_buy_count=0,
        fills_sell_count=0,
        trade_rows=[],
        benchmark_loaded=True,
    )

    text = path.read_text(encoding="utf-8")
    assert "回测区间内没有成交，绩效没有策略含义" in text
    assert "策略年化跑赢基准累计收益" not in text
    assert "trades.csv" not in text
    assert "nav.csv" in text
    assert "equity: dwd_fact_equity_kline_1d" in text
