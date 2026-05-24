"""Markdown 形式的回测说明文件生成器。

每次回测在 output/{timestamp}/ 下生成 summary.md，汇总：
  - 策略信息（类路径、docstring、universe）
  - 数据源与数据表
  - 回测参数（区间、频率、初始资金、基准）
  - 交易规则（佣金、印花税、滑点、止损、成交量限制、T+1）
  - 核心绩效指标
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from analytics.metrics import MetricsResult


# ---------- 工具函数 ----------


def _fmt_pct(v: float, sign: bool = True) -> str:
    if v is None or v != v:  # NaN
        return "n/a"
    return f"{v:+.2%}" if sign else f"{v:.2%}"


def _fmt_float(v: float, digits: int = 2) -> str:
    import math
    if v is None or v != v:
        return "n/a"
    if isinstance(v, float) and math.isinf(v):
        return "∞" if v > 0 else "-∞"
    return f"{v:.{digits}f}"


def _fmt_int(v: float) -> str:
    return f"{int(v):,}"


def _fmt_benchmark_return(v: float, benchmark_loaded: bool) -> str:
    if not benchmark_loaded:
        return "n/a（基准数据未加载）"
    return _fmt_pct(v)


def _fmt_date(date_str: str) -> str:
    """YYYYMMDDHHMM 或 YYYYMMDD → 可读日期字符串。"""
    s = str(date_str)
    if len(s) == 12:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _classify_excess(
    actual_annual: float,
    bench_total: float,
    benchmark_loaded: bool = True,
    trade_count: int = 0,
) -> str:
    """根据策略年化 vs 基准累计给出一句话评价。"""
    if trade_count == 0:
        return "回测区间内没有成交，绩效没有策略含义"
    if not benchmark_loaded:
        # 基准缺失时不做相对评价
        if actual_annual > 0:
            return "策略整体盈利（基准数据未加载，无法对比）"
        return "策略整体亏损（基准数据未加载，无法对比）"
    if actual_annual > bench_total:
        return "策略年化跑赢基准累计收益"
    if actual_annual > 0:
        return "策略整体盈利，但未能跑赢基准买入持有"
    return "策略整体亏损，**显著跑输基准买入持有**"


# ---------- 章节辅助函数 ----------


def _fee_summary_section(trade_rows: Optional[List[Dict]]) -> str:
    """生成费用汇总章节（Markdown 字符串）。"""
    if not trade_rows:
        return "## 七、费用汇总\n\n> 暂无成交记录。\n"
    total_comm = sum(r["commission"] for r in trade_rows)
    total_stamp = sum(r["stamp_duty"] for r in trade_rows)
    total_transfer = sum(r["transfer_fee"] for r in trade_rows)
    total_fee = total_comm + total_stamp + total_transfer
    total_amount = sum(r["amount"] for r in trade_rows)
    fee_rate = total_fee / total_amount if total_amount > 0 else 0.0
    return f"""## 七、费用汇总

| 费用类型 | 金额（元） |
|---|---|
| 佣金合计 | {total_comm:,.2f} |
| 印花税合计 | {total_stamp:,.2f} |
| 过户费合计 | {total_transfer:,.2f} |
| **总交易费用** | **{total_fee:,.2f}** |
| 成交金额合计 | {total_amount:,.2f} |
| 综合费率 | {fee_rate:.4%} |"""


def _trade_log_section(trade_rows: Optional[List[Dict]]) -> str:
    """生成完整交易明细章节（Markdown 字符串）。"""
    if not trade_rows:
        return "## 八、交易明细\n\n> 暂无成交记录。\n"

    header = (
        "## 八、交易明细\n\n"
        "| 时间 | 方向 | 代码 | 数量（股） | 成交价 | 成交金额 | 佣金 | 印花税 | 过户费 | 合计费用 |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
    )
    lines = [header]
    for r in trade_rows:
        lines.append(
            f"| {_fmt_date(r['date'])} "
            f"| {r['side']} "
            f"| {r['code']} "
            f"| {r['qty']:,} "
            f"| {r['price']:.4f} "
            f"| {r['amount']:,.2f} "
            f"| {r['commission']:.2f} "
            f"| {r['stamp_duty']:.2f} "
            f"| {r['transfer_fee']:.2f} "
            f"| {r['total_fee']:.2f} |\n"
        )
    return "".join(lines)


# ---------- 主入口 ----------


def generate_markdown_summary(
    *,
    output_dir: Path,
    metrics: MetricsResult,
    strategy_class_path: str,
    strategy_doc: str,
    universe: List[str],
    start_date: str,
    end_date: str,
    initial_capital: float,
    frequency: str,
    benchmark: str,
    config: Dict[str, Any],
    nav_records_count: int,
    fills_buy_count: int,
    fills_sell_count: int,
    data_source_name: str = "BigQuery",
    trade_rows: Optional[List[Dict[str, Any]]] = None,
    benchmark_loaded: bool = True,
) -> Path:
    """生成 summary.md 文件，返回路径。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    trading_cfg = config.get("trading", {})
    slip_cfg = config.get("slippage", {})
    exec_cfg = config.get("execution", {})
    stop_cfg = config.get("stop_loss", {})
    data_cfg = config.get("data", {})
    bq_cfg = data_cfg.get("bigquery", {})
    tables_cfg = bq_cfg.get("tables", {})

    # 推断本次回测真正用到的表（按 frequency）
    if frequency == "daily":
        used_table = (
            f"equity: {tables_cfg.get('kline_1d_equity', '')}, "
            f"fund: {tables_cfg.get('kline_1d_fund', '')}, "
            f"index: {tables_cfg.get('kline_1d_index', '')}"
        )
    else:
        used_table = tables_cfg.get(f"kline_{frequency}_equity", "")

    universe_str = ", ".join(universe) if universe else "(空)"
    doc_first_line = (strategy_doc or "").strip().split("\n")[0] if strategy_doc else "(无描述)"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    assessment = _classify_excess(
        metrics.annual_return,
        metrics.benchmark_return,
        benchmark_loaded,
        fills_buy_count + fills_sell_count,
    )
    artifact_lines = [
        "- `summary.md` — 本说明文件（含交易明细）",
        "- `nav.csv` — 净值曲线明细（CSV 格式）",
        "- `report.html` — HTML 可视化报告",
        "- `cum_returns.png` — 策略 vs 基准累计收益曲线",
        "- `drawdown.png` — 回撤曲线",
        "- `monthly_returns.png` — 月度收益热力图",
    ]
    if trade_rows:
        artifact_lines.insert(1, "- `trades.csv` — 完整成交流水（CSV 格式）")
    artifact_list = "\n".join(artifact_lines)

    md = f"""# 回测报告 - {now_str}

## 一、策略

| 字段 | 值 |
|---|---|
| 策略类 | `{strategy_class_path}` |
| 描述 | {doc_first_line} |
| 品种池 (universe) | {universe_str} |

## 二、数据

| 字段 | 值 |
|---|---|
| 数据源 | {data_source_name} |
| 项目 (project) | `{bq_cfg.get('project_id', '')}` |
| 主表 (period={frequency}) | `{used_table or '(未配置)'}` |
| 复权方式 | 前复权（qfq），BigQuery 日K表内置 adjust_type 直接查询 |

## 三、回测参数

| 字段 | 值 |
|---|---|
| 时间区间 | {start_date} ~ {end_date} |
| 频率 | {frequency} |
| 初始资金 | {_fmt_int(initial_capital)} 元 |
| 基准 | {benchmark} |
| Bar 总条数 | {nav_records_count:,} |

## 四、交易规则

| 字段 | 值 |
|---|---|
| 佣金 | {trading_cfg.get('commission_rate', 0) * 10000:.2f}/万（最低 {trading_cfg.get('min_commission', 0):.0f} 元） |
| 印花税 | {trading_cfg.get('stamp_duty_rate', 0) * 1000:.2f}‰（卖出时收） |
| 过户费 | {trading_cfg.get('transfer_fee_rate', 0) * 10000:.2f}/万 |
| 滑点 | {slip_cfg.get('value', 0) * 100:.2f}% ({slip_cfg.get('type', '')}) |
| 撮合价格 | `{exec_cfg.get('price_type', '')}`（bar T 信号 → bar T+1 open 成交） |
| 成交量限制 | 单笔 ≤ 当根 bar 成交量的 {exec_cfg.get('volume_limit', 0) * 100:.0f}% |
| 允许做空 | {'是' if exec_cfg.get('allow_short', False) else '否'} |
| T+1 规则 | ✓ 当日买入次日才可卖 |
| 止损 | {'已启用，阈值 ' + f"{stop_cfg.get('threshold', 0) * 100:.1f}%" + '（基于加权平均成本价）' if stop_cfg.get('enabled', False) else '未启用'} |

## 五、核心绩效

| 指标 | 值 |
|---|---|
| **累计收益率** | {_fmt_pct(metrics.total_return)} |
| **年化收益率** | {_fmt_pct(metrics.annual_return)} |
| 基准累计收益 | {_fmt_benchmark_return(metrics.benchmark_return, benchmark_loaded)} |
| 年化超额收益 | {_fmt_benchmark_return(metrics.excess_return, benchmark_loaded)} |
| 最大回撤 | {_fmt_pct(metrics.max_drawdown, sign=False)} |
| 最大回撤持续天数 | {metrics.max_drawdown_duration} 天 |
| 年化波动率 | {_fmt_pct(metrics.volatility, sign=False)} |
| 夏普比率 | {_fmt_float(metrics.sharpe_ratio)} |
| 索提诺比率 | {_fmt_float(metrics.sortino_ratio)} |
| Beta | {_fmt_float(metrics.beta) if benchmark_loaded else "n/a（基准未加载）"} |
| Alpha | {_fmt_pct(metrics.alpha) if benchmark_loaded else "n/a（基准未加载）"} |
| 信息比率 | {_fmt_float(metrics.information_ratio) if benchmark_loaded else "n/a（基准未加载）"} |

### 一句话评价

{assessment}。

## 六、交易统计

| 字段 | 值 |
|---|---|
| 买入成交笔数 | {fills_buy_count:,} |
| 卖出成交笔数 | {fills_sell_count:,} |
| 合计成交笔数 | {fills_buy_count + fills_sell_count:,} |
| 配对交易数（FIFO） | {metrics.total_trades:,} |
| 胜率 | {_fmt_pct(metrics.win_rate, sign=False) if metrics.total_trades else "n/a"} |
| 盈亏比 | {_fmt_float(metrics.profit_loss_ratio) if metrics.total_trades else "n/a"} |

{_fee_summary_section(trade_rows)}

{_trade_log_section(trade_rows)}

## 九、产物清单

{artifact_list}

---

> ⚠️ 本回测为纯模拟，不构成投资建议。
> 由 A-Share Quantitative Trading 框架自动生成于 {now_str}。
"""

    path = output_dir / "summary.md"
    path.write_text(md, encoding="utf-8")
    return path
