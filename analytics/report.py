"""HTML 回测报告生成器。"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from analytics.metrics import MetricsResult


HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <title>A股模拟量化交易回测报告</title>
    <style>
        body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; margin: 40px; background: #f5f5f5; }
        .container { max-width: 960px; margin: 0 auto; background: #fff; padding: 32px; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); }
        h1 { font-size: 24px; margin-bottom: 8px; }
        .subtitle { color: #888; font-size: 14px; margin-bottom: 24px; }
        .metrics { display: grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap: 16px; margin-bottom: 32px; }
        .card { background: #fafafa; border: 1px solid #eee; border-radius: 6px; padding: 16px; }
        .card .label { font-size: 12px; color: #666; margin-bottom: 4px; }
        .card .value { font-size: 20px; font-weight: 600; color: #222; }
        .positive { color: #d93025; }
        .negative { color: #1e8e3e; }
        .section { margin-bottom: 32px; }
        .section h2 { font-size: 18px; border-bottom: 1px solid #eee; padding-bottom: 8px; margin-bottom: 16px; }
        img { max-width: 100%; border: 1px solid #eee; border-radius: 4px; margin-bottom: 16px; }
        footer { text-align: center; color: #aaa; font-size: 12px; margin-top: 40px; }
    </style>
</head>
<body>
    <div class="container">
        <h1>📈 A股模拟量化交易回测报告</h1>
        <div class="subtitle">生成时间：{generated_at}</div>

        <div class="section">
            <h2>核心绩效指标</h2>
            <div class="metrics">
                <div class="card">
                    <div class="label">累计收益率</div>
                    <div class="value {total_return_cls}">{total_return:.2%}</div>
                </div>
                <div class="card">
                    <div class="label">年化收益率</div>
                    <div class="value {annual_return_cls}">{annual_return:.2%}</div>
                </div>
                <div class="card">
                    <div class="label">基准收益率</div>
                    <div class="value">{benchmark_return:.2%}</div>
                </div>
                <div class="card">
                    <div class="label">超额收益</div>
                    <div class="value {excess_return_cls}">{excess_return:.2%}</div>
                </div>
                <div class="card">
                    <div class="label">最大回撤</div>
                    <div class="value negative">{max_drawdown:.2%}</div>
                </div>
                <div class="card">
                    <div class="label">年化波动率</div>
                    <div class="value">{volatility:.2%}</div>
                </div>
                <div class="card">
                    <div class="label">夏普比率</div>
                    <div class="value">{sharpe_ratio:.2f}</div>
                </div>
                <div class="card">
                    <div class="label">信息比率</div>
                    <div class="value">{information_ratio:.2f}</div>
                </div>
            </div>
        </div>

        <div class="section">
            <h2>收益曲线</h2>
            <img src="cum_returns.png" alt="累计收益">
        </div>

        <div class="section">
            <h2>回撤曲线</h2>
            <img src="drawdown.png" alt="回撤">
        </div>

        <div class="section">
            <h2>月度收益热力图</h2>
            <img src="monthly_returns.png" alt="月度收益">
        </div>

        <footer>由 A-Share Quantitative Trading 框架自动生成 · 纯模拟交易，不构成投资建议</footer>
    </div>
</body>
</html>
"""


def generate_html_report(
    metrics: MetricsResult,
    output_dir: str = "output/report",
    images_dir: Optional[str] = None,
) -> str:
    """生成 HTML 回测报告并保存到本地。

    Returns:
        生成的 HTML 文件路径。
    """
    from datetime import datetime

    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)

    def cls(v: float) -> str:
        if v > 0:
            return "positive"
        if v < 0:
            return "negative"
        return ""

    html = HTML_TEMPLATE.format(
        generated_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        total_return=metrics.total_return,
        total_return_cls=cls(metrics.total_return),
        annual_return=metrics.annual_return,
        annual_return_cls=cls(metrics.annual_return),
        benchmark_return=metrics.benchmark_return,
        excess_return=metrics.excess_return,
        excess_return_cls=cls(metrics.excess_return),
        max_drawdown=metrics.max_drawdown,
        volatility=metrics.volatility,
        sharpe_ratio=metrics.sharpe_ratio,
        information_ratio=metrics.information_ratio,
    )

    path = out / "report.html"
    path.write_text(html, encoding="utf-8")
    return str(path)
