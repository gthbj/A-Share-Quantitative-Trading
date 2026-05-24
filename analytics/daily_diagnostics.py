"""逐日回测诊断 CSV 输出。

CSV 第一行是中文表头，第二行是英文字段名，第三行开始为数据。
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Iterable, Mapping


DAILY_SUMMARY_HEADERS = [
    ("日期", "date"),
    ("交易日序号", "day_index"),
    ("总交易日数", "total_days"),
    ("完成进度", "progress_pct"),
    ("已运行秒数", "elapsed_seconds"),
    ("预计剩余秒数", "eta_seconds"),
    ("总资产", "nav"),
    ("初始资金", "initial_capital"),
    ("当日收益率", "daily_return_pct"),
    ("累计收益率", "total_return_pct"),
    ("最大回撤", "max_drawdown_pct"),
    ("现金", "cash"),
    ("持仓市值", "position_value"),
    ("现金占比", "cash_ratio_pct"),
    ("持仓数量", "position_count"),
    ("今日买入笔数", "buy_count"),
    ("今日卖出笔数", "sell_count"),
    ("今日费用", "fee_total"),
    ("模型训练截止日", "model_train_end"),
    ("模型路径", "model_dir"),
    ("市场状态", "regime"),
    ("目标持仓数", "target_position_count"),
    ("基准代码", "benchmark_code"),
    ("沪深300收益率", "hs300_return_pct"),
    ("沪深300最大回撤", "hs300_max_drawdown_pct"),
    ("上证50收益率", "sz50_return_pct"),
    ("上证50最大回撤", "sz50_max_drawdown_pct"),
    ("中证500收益率", "zz500_return_pct"),
    ("中证500最大回撤", "zz500_max_drawdown_pct"),
    ("中证1000收益率", "zz1000_return_pct"),
    ("中证1000最大回撤", "zz1000_max_drawdown_pct"),
    ("创业板指收益率", "chinext_return_pct"),
    ("创业板指最大回撤", "chinext_max_drawdown_pct"),
    ("相对沪深300超额", "excess_vs_hs300_pct"),
]


DAILY_POSITION_HEADERS = [
    ("日期", "date"),
    ("代码", "code"),
    ("持仓数量", "qty"),
    ("可卖数量", "sellable_qty"),
    ("成本价", "cost_price"),
    ("收盘价", "close_price"),
    ("市值", "market_value"),
    ("浮动盈亏", "unrealized_pnl"),
    ("浮动盈亏率", "unrealized_pnl_pct"),
    ("持有天数", "holding_days"),
    ("持仓期最高价", "peak_price"),
    ("相对最高价回撤", "drawdown_from_peak_pct"),
    ("是否可卖", "is_sellable"),
    ("模型训练截止日", "model_train_end"),
    ("市场状态", "regime"),
]


DAILY_CANDIDATE_HEADERS = [
    ("日期", "date"),
    ("排名", "rank"),
    ("代码", "code"),
    ("综合分", "score"),
    ("1日上涨概率", "prob_up_h1"),
    ("5日上涨概率", "prob_up_h5"),
    ("10日上涨概率", "prob_up_h10"),
    ("20日上涨概率", "prob_up_h20"),
    ("卖出风险概率", "prob_sell"),
    ("是否已持仓", "is_held"),
    ("是否目标Top内", "in_top_target"),
    ("模型训练截止日", "model_train_end"),
    ("市场状态", "regime"),
]


def write_two_header_csv(
    path: str | Path,
    headers: list[tuple[str, str]],
    rows: Iterable[Mapping[str, Any]],
) -> Path:
    """写两行表头 CSV。"""
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    fields = [field for _, field in headers]
    with out.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([zh for zh, _ in headers])
        writer.writerow(fields)
        for row in rows:
            writer.writerow([row.get(field, "") for field in fields])
    return out


def write_daily_diagnostics(output_dir: str | Path, records) -> dict[str, Path]:
    """从 BacktestEngine.records 写出逐日诊断 CSV。"""
    out = Path(output_dir)
    summary_rows = [r.summary for r in records if getattr(r, "summary", None)]
    position_rows = [
        row
        for r in records
        for row in getattr(r, "position_details", [])
    ]
    candidate_rows = [
        row
        for r in records
        for row in getattr(r, "candidate_details", [])
    ]
    return {
        "daily_log": write_two_header_csv(
            out / "daily_log.csv",
            DAILY_SUMMARY_HEADERS,
            summary_rows,
        ),
        "daily_positions": write_two_header_csv(
            out / "daily_positions.csv",
            DAILY_POSITION_HEADERS,
            position_rows,
        ),
        "daily_candidates": write_two_header_csv(
            out / "daily_candidates.csv",
            DAILY_CANDIDATE_HEADERS,
            candidate_rows,
        ),
    }
