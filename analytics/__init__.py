"""绩效分析模块：收益、风险指标计算与可视化。"""

from .metrics import calculate_metrics, MetricsResult
from .plotter import Plotter
from .report import generate_html_report
from .gcs_archive import archive_backtest_output

__all__ = [
    "calculate_metrics",
    "MetricsResult",
    "Plotter",
    "generate_html_report",
    "archive_backtest_output",
]
