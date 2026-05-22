"""HTML 回测报告生成器。

报告内容与 ``analytics/summary.py`` 生成的 markdown 同源对齐：
  策略元信息 / 数据来源 / 回测参数 / 交易规则 / 核心绩效 / 收益图表 /
  交易统计 / 费用汇总 / 完整交易明细（折叠展示）
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from analytics.metrics import MetricsResult


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>A股模拟量化交易回测报告</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei", "Segoe UI", Roboto, sans-serif; margin: 40px; background: #f5f5f5; color: #333; }
        .container { max-width: 1080px; margin: 0 auto; background: #fff; padding: 32px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
        h1 { font-size: 24px; margin-bottom: 8px; }
        h2 { font-size: 18px; border-bottom: 1px solid #eee; padding-bottom: 8px; margin: 32px 0 16px; }
        .subtitle { color: #888; font-size: 14px; margin-bottom: 24px; }
        .metrics { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; margin-bottom: 16px; }
        .card { background: #fafafa; border: 1px solid #eee; border-radius: 6px; padding: 14px 16px; }
        .card .label { font-size: 12px; color: #666; margin-bottom: 4px; }
        .card .value { font-size: 20px; font-weight: 600; color: #222; }
        .positive { color: #d93025; }
        .negative { color: #1e8e3e; }
        table.meta { width: 100%; border-collapse: collapse; }
        table.meta td { padding: 8px 12px; border-bottom: 1px solid #f0f0f0; vertical-align: top; }
        table.meta td.k { width: 200px; color: #666; }
        .chip { display: inline-block; padding: 2px 10px; margin: 2px 4px 2px 0; border-radius: 12px; background: #e8f0fe; color: #1a73e8; font-size: 12px; }
        img { max-width: 100%; border: 1px solid #eee; border-radius: 4px; margin-bottom: 16px; }
        details { margin: 16px 0; }
        details summary { cursor: pointer; padding: 8px; background: #fafafa; border: 1px solid #eee; border-radius: 4px; font-weight: 600; }
        table.trades { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 12px; }
        table.trades th, table.trades td { padding: 6px 8px; border: 1px solid #eee; text-align: right; }
        table.trades th { background: #f5f5f5; color: #555; font-weight: 600; }
        table.trades td.time, table.trades td.side, table.trades td.code { text-align: left; }
        table.trades tr.buy { background: #fff8f8; }
        table.trades tr.sell { background: #f5fffa; }
        footer { text-align: center; color: #aaa; font-size: 12px; margin-top: 40px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📈 A股模拟量化交易回测报告</h1>
        <div class="subtitle">生成时间：__GENERATED_AT__</div>

        <h2>一、策略与回测概要</h2>
        <table class="meta">
            <tr><td class="k">策略类</td><td><code>__STRATEGY_CLASS__</code></td></tr>
            <tr><td class="k">描述</td><td>__STRATEGY_DOC__</td></tr>
            <tr><td class="k">品种池 (universe)</td><td>__UNIVERSE_CHIPS__</td></tr>
            <tr><td class="k">时间区间</td><td>__START_DATE__ ~ __END_DATE__</td></tr>
            <tr><td class="k">频率</td><td>__FREQUENCY__</td></tr>
            <tr><td class="k">初始资金</td><td>__INITIAL_CAPITAL__ 元</td></tr>
            <tr><td class="k">基准</td><td>__BENCHMARK__</td></tr>
            <tr><td class="k">Bar 总条数</td><td>__NAV_COUNT__</td></tr>
        </table>

        <h2>二、数据来源</h2>
        <table class="meta">
            <tr><td class="k">数据源</td><td>__DATA_SOURCE__</td></tr>
            <tr><td class="k">项目 (project)</td><td><code>__MC_PROJECT__</code></td></tr>
            <tr><td class="k">主表 (period=__FREQUENCY__)</td><td><code>__USED_TABLE__</code></td></tr>
            <tr><td class="k">复权方式</td><td>前复权（qfq），BigQuery 日K表内置 <code>adjust_type</code> 直接查询</td></tr>
        </table>

        <h2>三、核心绩效指标</h2>
        <div class="metrics">
            <div class="card"><div class="label">累计收益率</div><div class="value __CLS_TOTAL__">__TOTAL_RETURN__</div></div>
            <div class="card"><div class="label">年化收益率</div><div class="value __CLS_ANNUAL__">__ANNUAL_RETURN__</div></div>
            <div class="card"><div class="label">基准累计收益</div><div class="value">__BENCH_RETURN__</div></div>
            <div class="card"><div class="label">年化超额收益</div><div class="value __CLS_EXCESS__">__EXCESS_RETURN__</div></div>
            <div class="card"><div class="label">最大回撤</div><div class="value negative">__MAX_DRAWDOWN__</div></div>
            <div class="card"><div class="label">最大回撤持续</div><div class="value">__MDD_DAYS__ 天</div></div>
            <div class="card"><div class="label">年化波动率</div><div class="value">__VOLATILITY__</div></div>
            <div class="card"><div class="label">夏普比率</div><div class="value">__SHARPE__</div></div>
            <div class="card"><div class="label">索提诺比率</div><div class="value">__SORTINO__</div></div>
            <div class="card"><div class="label">Beta</div><div class="value">__BETA__</div></div>
            <div class="card"><div class="label">Alpha</div><div class="value __CLS_ALPHA__">__ALPHA__</div></div>
            <div class="card"><div class="label">信息比率</div><div class="value">__IR__</div></div>
        </div>

        <h2>四、交易规则</h2>
        <table class="meta">
            <tr><td class="k">佣金</td><td>__COMMISSION__</td></tr>
            <tr><td class="k">印花税</td><td>__STAMP_DUTY__（卖出时收）</td></tr>
            <tr><td class="k">过户费</td><td>__TRANSFER_FEE__</td></tr>
            <tr><td class="k">滑点</td><td>__SLIPPAGE__</td></tr>
            <tr><td class="k">撮合价格</td><td><code>__PRICE_TYPE__</code></td></tr>
            <tr><td class="k">成交量限制</td><td>单笔 ≤ 当根 bar 成交量的 __VOLUME_LIMIT__</td></tr>
            <tr><td class="k">T+1 规则</td><td>✓ 当日买入次日才可卖</td></tr>
            <tr><td class="k">止损</td><td>__STOP_LOSS__</td></tr>
        </table>

        <h2>五、收益曲线</h2>
        <img src="cum_returns.png" alt="累计收益">

        <h2>六、回撤曲线</h2>
        <img src="drawdown.png" alt="回撤">

        <h2>七、月度收益热力图</h2>
        <img src="monthly_returns.png" alt="月度收益">

        <h2>八、交易统计与费用汇总</h2>
        <div class="metrics">
            <div class="card"><div class="label">买入笔数</div><div class="value">__BUY_COUNT__</div></div>
            <div class="card"><div class="label">卖出笔数</div><div class="value">__SELL_COUNT__</div></div>
            <div class="card"><div class="label">配对交易数</div><div class="value">__TOTAL_TRADES__</div></div>
            <div class="card"><div class="label">胜率</div><div class="value">__WIN_RATE__</div></div>
            <div class="card"><div class="label">盈亏比</div><div class="value">__PL_RATIO__</div></div>
            <div class="card"><div class="label">佣金合计</div><div class="value">__FEE_COMM__</div></div>
            <div class="card"><div class="label">印花税合计</div><div class="value">__FEE_STAMP__</div></div>
            <div class="card"><div class="label">过户费合计</div><div class="value">__FEE_TRANSFER__</div></div>
            <div class="card"><div class="label">总费用</div><div class="value">__FEE_TOTAL__</div></div>
            <div class="card"><div class="label">综合费率</div><div class="value">__FEE_RATE__</div></div>
        </div>

        <h2>九、完整交易明细</h2>
        <details>
            <summary>展开/收起交易明细（共 __TRADE_COUNT__ 条）</summary>
            __TRADE_TABLE__
        </details>

        <footer>由 A-Share Quantitative Trading 框架自动生成 · 纯模拟交易，不构成投资建议</footer>
    </div>
</body>
</html>
"""


def _fmt_pct(v: float, sign: bool = True) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v))):
        return "n/a"
    return f"{v:+.2%}" if sign else f"{v:.2%}"


def _fmt_float(v: float, digits: int = 2) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return "n/a"
    if math.isinf(v):
        return "∞" if v > 0 else "-∞"
    return f"{v:.{digits}f}"


def _fmt_int(v: float) -> str:
    return f"{int(v):,}"


def _fmt_date(date_str: str) -> str:
    s = str(date_str)
    if len(s) == 12:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]} {s[8:10]}:{s[10:12]}"
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return s


def _cls(v: float) -> str:
    if v is None or (isinstance(v, float) and math.isnan(v)):
        return ""
    if v > 0:
        return "positive"
    if v < 0:
        return "negative"
    return ""


def _build_trade_table(trade_rows: Optional[List[Dict[str, Any]]]) -> str:
    if not trade_rows:
        return "<p>暂无成交记录。</p>"
    rows_html = [
        "<table class='trades'>",
        "<thead><tr>"
        "<th>时间</th><th>方向</th><th>代码</th><th>数量</th>"
        "<th>成交价</th><th>成交金额</th><th>佣金</th>"
        "<th>印花税</th><th>过户费</th><th>合计费用</th>"
        "</tr></thead><tbody>",
    ]
    for r in trade_rows:
        cls = "buy" if r["side"] == "买入" else "sell"
        rows_html.append(
            f"<tr class='{cls}'>"
            f"<td class='time'>{_fmt_date(r['date'])}</td>"
            f"<td class='side'>{r['side']}</td>"
            f"<td class='code'>{r['code']}</td>"
            f"<td>{int(r['qty']):,}</td>"
            f"<td>{r['price']:.4f}</td>"
            f"<td>{r['amount']:,.2f}</td>"
            f"<td>{r['commission']:.2f}</td>"
            f"<td>{r['stamp_duty']:.2f}</td>"
            f"<td>{r['transfer_fee']:.2f}</td>"
            f"<td>{r['total_fee']:.2f}</td>"
            f"</tr>"
        )
    rows_html.append("</tbody></table>")
    return "".join(rows_html)


def _fee_summary(trade_rows: Optional[List[Dict[str, Any]]]) -> Dict[str, str]:
    if not trade_rows:
        return {
            "comm": "0.00", "stamp": "0.00", "transfer": "0.00",
            "total": "0.00", "rate": "0.00%",
        }
    total_comm = sum(r["commission"] for r in trade_rows)
    total_stamp = sum(r["stamp_duty"] for r in trade_rows)
    total_transfer = sum(r["transfer_fee"] for r in trade_rows)
    total_fee = total_comm + total_stamp + total_transfer
    total_amount = sum(r["amount"] for r in trade_rows)
    fee_rate = total_fee / total_amount if total_amount > 0 else 0.0
    return {
        "comm": f"{total_comm:,.2f}",
        "stamp": f"{total_stamp:,.2f}",
        "transfer": f"{total_transfer:,.2f}",
        "total": f"{total_fee:,.2f}",
        "rate": f"{fee_rate:.4%}",
    }


def generate_html_report(
    metrics: MetricsResult,
    output_dir: str = "output/report",
    images_dir: Optional[str] = None,
    *,
    strategy_class_path: str = "",
    strategy_doc: str = "",
    universe: Optional[List[str]] = None,
    start_date: str = "",
    end_date: str = "",
    initial_capital: float = 0.0,
    frequency: str = "daily",
    benchmark: str = "",
    config: Optional[Dict[str, Any]] = None,
    nav_records_count: int = 0,
    fills_buy_count: int = 0,
    fills_sell_count: int = 0,
    data_source_name: str = "BigQuery",
    trade_rows: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """生成 HTML 回测报告并保存到本地。

    Returns:
        生成的 HTML 文件路径。
    """
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    config = config or {}
    universe = universe or []
    trading_cfg = config.get("trading", {})
    slip_cfg = config.get("slippage", {})
    exec_cfg = config.get("execution", {})
    stop_cfg = config.get("stop_loss", {})
    bq_cfg = config.get("data", {}).get("bigquery", {})
    tables_cfg = bq_cfg.get("tables", {})

    # 推断本次回测真正用到的表
    if frequency == "daily":
        used_table = (
            f"equity: {tables_cfg.get('kline_1d_equity', '')}, "
            f"fund: {tables_cfg.get('kline_1d_fund', '')}, "
            f"index: {tables_cfg.get('kline_1d_index', '')}"
        )
    else:
        used_table = tables_cfg.get(f"kline_{frequency}_equity", "")

    universe_chips = "".join(
        f"<span class='chip'>{code}</span>" for code in universe
    ) if universe else "<span class='chip'>(空)</span>"

    doc_first_line = (strategy_doc or "").strip().split("\n")[0] if strategy_doc else "(无描述)"

    commission_str = (
        f"{trading_cfg.get('commission_rate', 0) * 10000:.2f}/万 "
        f"(最低 {trading_cfg.get('min_commission', 0):.0f} 元)"
    )
    stamp_duty_str = f"{trading_cfg.get('stamp_duty_rate', 0) * 1000:.2f}‰"
    transfer_fee_str = f"{trading_cfg.get('transfer_fee_rate', 0) * 10000:.2f}/万"
    slippage_str = f"{slip_cfg.get('value', 0) * 100:.2f}% ({slip_cfg.get('type', '')})"
    price_type_str = exec_cfg.get('price_type', '')
    volume_limit_str = f"{exec_cfg.get('volume_limit', 0) * 100:.0f}%"
    if stop_cfg.get("enabled", False):
        stop_loss_str = f"已启用，阈值 {stop_cfg.get('threshold', 0) * 100:.1f}%"
    else:
        stop_loss_str = "未启用"

    fees = _fee_summary(trade_rows)
    trade_table_html = _build_trade_table(trade_rows)

    # 字段替换映射
    placeholders = {
        "__GENERATED_AT__": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "__STRATEGY_CLASS__": strategy_class_path or "(未指定)",
        "__STRATEGY_DOC__": doc_first_line or "(无描述)",
        "__UNIVERSE_CHIPS__": universe_chips,
        "__START_DATE__": start_date,
        "__END_DATE__": end_date,
        "__FREQUENCY__": frequency,
        "__INITIAL_CAPITAL__": _fmt_int(initial_capital),
        "__BENCHMARK__": benchmark,
        "__NAV_COUNT__": f"{nav_records_count:,}",
        "__DATA_SOURCE__": data_source_name,
        "__MC_PROJECT__": bq_cfg.get('project_id', ''),
        "__USED_TABLE__": used_table or "(未配置)",
        "__TOTAL_RETURN__": _fmt_pct(metrics.total_return),
        "__CLS_TOTAL__": _cls(metrics.total_return),
        "__ANNUAL_RETURN__": _fmt_pct(metrics.annual_return),
        "__CLS_ANNUAL__": _cls(metrics.annual_return),
        "__BENCH_RETURN__": _fmt_pct(metrics.benchmark_return),
        "__EXCESS_RETURN__": _fmt_pct(metrics.excess_return),
        "__CLS_EXCESS__": _cls(metrics.excess_return),
        "__MAX_DRAWDOWN__": _fmt_pct(metrics.max_drawdown, sign=False),
        "__MDD_DAYS__": str(metrics.max_drawdown_duration),
        "__VOLATILITY__": _fmt_pct(metrics.volatility, sign=False),
        "__SHARPE__": _fmt_float(metrics.sharpe_ratio),
        "__SORTINO__": _fmt_float(metrics.sortino_ratio),
        "__BETA__": _fmt_float(metrics.beta),
        "__ALPHA__": _fmt_pct(metrics.alpha),
        "__CLS_ALPHA__": _cls(metrics.alpha),
        "__IR__": _fmt_float(metrics.information_ratio),
        "__COMMISSION__": commission_str,
        "__STAMP_DUTY__": stamp_duty_str,
        "__TRANSFER_FEE__": transfer_fee_str,
        "__SLIPPAGE__": slippage_str,
        "__PRICE_TYPE__": price_type_str,
        "__VOLUME_LIMIT__": volume_limit_str,
        "__STOP_LOSS__": stop_loss_str,
        "__BUY_COUNT__": f"{fills_buy_count:,}",
        "__SELL_COUNT__": f"{fills_sell_count:,}",
        "__TOTAL_TRADES__": f"{metrics.total_trades:,}",
        "__WIN_RATE__": _fmt_pct(metrics.win_rate, sign=False) if metrics.total_trades else "n/a",
        "__PL_RATIO__": _fmt_float(metrics.profit_loss_ratio) if metrics.total_trades else "n/a",
        "__FEE_COMM__": fees["comm"],
        "__FEE_STAMP__": fees["stamp"],
        "__FEE_TRANSFER__": fees["transfer"],
        "__FEE_TOTAL__": fees["total"],
        "__FEE_RATE__": fees["rate"],
        "__TRADE_COUNT__": f"{len(trade_rows) if trade_rows else 0}",
        "__TRADE_TABLE__": trade_table_html,
    }

    html = HTML_TEMPLATE
    for k, v in placeholders.items():
        html = html.replace(k, str(v))

    path = out / "report.html"
    path.write_text(html, encoding="utf-8")
    return str(path)
