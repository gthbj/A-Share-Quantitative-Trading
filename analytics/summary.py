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
    if v != v:  # NaN
        return "n/a"
    return f"{v:+.2%}" if sign else f"{v:.2%}"


def _fmt_float(v: float, digits: int = 2) -> str:
    if v != v:
        return "n/a"
    return f"{v:.{digits}f}"


def _fmt_int(v: float) -> str:
    return f"{int(v):,}"


def _classify_excess(actual_annual: float, bench_total: float) -> str:
    """根据策略年化 vs 基准累计给出一句话评价。"""
    if actual_annual > bench_total:
        return "策略年化跑赢基准累计收益"
    if actual_annual > 0:
        return "策略整体盈利，但未能跑赢基准买入持有"
    return "策略整体亏损，**显著跑输基准买入持有**"


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
    data_source_name: str = "MaxCompute",
) -> Path:
    """生成 summary.md 文件，返回路径。"""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    trading_cfg = config.get("trading", {})
    slip_cfg = config.get("slippage", {})
    exec_cfg = config.get("execution", {})
    stop_cfg = config.get("stop_loss", {})
    data_cfg = config.get("data", {})
    mc_cfg = data_cfg.get("maxcompute", {})
    tables_cfg = mc_cfg.get("tables", {})

    # 推断本次回测真正用到的表（按 frequency）
    table_hints = {
        "daily": tables_cfg.get("daily", ""),
        "1min": tables_cfg.get("kline_1min", ""),
        "5min": tables_cfg.get("kline_5min", ""),
        "15min": tables_cfg.get("kline_etf_15min") or tables_cfg.get("kline_15min", ""),
        "30min": tables_cfg.get("kline_30min", ""),
        "60min": tables_cfg.get("kline_60min", ""),
    }
    used_table = table_hints.get(frequency, "")

    universe_str = ", ".join(universe) if universe else "(空)"
    doc_first_line = (strategy_doc or "").strip().split("\n")[0] if strategy_doc else "(无描述)"

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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
| 项目 (project) | `{mc_cfg.get('project', '')}` |
| 主表 (period={frequency}) | `{used_table or '(未配置)'}` |
| 复权方式 | 暂未接入（使用原始价） |

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
| 基准累计收益 | {_fmt_pct(metrics.benchmark_return)} |
| 年化超额收益 | {_fmt_pct(metrics.excess_return)} |
| 最大回撤 | {_fmt_pct(metrics.max_drawdown, sign=False)} |
| 最大回撤持续天数 | {metrics.max_drawdown_duration} 天 |
| 年化波动率 | {_fmt_pct(metrics.volatility, sign=False)} |
| 夏普比率 | {_fmt_float(metrics.sharpe_ratio)} |
| 索提诺比率 | {_fmt_float(metrics.sortino_ratio)} |
| Beta | {_fmt_float(metrics.beta)} |
| Alpha | {_fmt_pct(metrics.alpha)} |
| 信息比率 | {_fmt_float(metrics.information_ratio)} |

### 一句话评价

{_classify_excess(metrics.annual_return, metrics.benchmark_return)}。

## 六、交易统计

| 字段 | 值 |
|---|---|
| 买入成交笔数 | {fills_buy_count:,} |
| 卖出成交笔数 | {fills_sell_count:,} |
| 合计成交笔数 | {fills_buy_count + fills_sell_count:,} |

## 七、产物清单

- `summary.md` — 本说明文件
- `report.html` — HTML 可视化报告
- `cum_returns.png` — 策略 vs 基准累计收益曲线
- `drawdown.png` — 回撤曲线
- `monthly_returns.png` — 月度收益热力图

---

> ⚠️ 本回测为纯模拟，不构成投资建议。
> 由 A-Share Quantitative Trading 框架自动生成于 {now_str}。
"""

    path = output_dir / "summary.md"
    path.write_text(md, encoding="utf-8")
    return path
