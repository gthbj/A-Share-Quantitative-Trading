"""CLI 入口：一键运行回测。

用法示例：
    python run_backtest.py --strategy strategy.double_ma --start 20210101 --end 20231231
"""

from __future__ import annotations

import argparse
import importlib
import sys
from pathlib import Path

import yaml

from analytics.metrics import calculate_metrics
from analytics.plotter import Plotter
from analytics.report import generate_html_report
from data_layer.akshare_source import AKShareDataSource
from data_layer.tushare_source import TushareDataSource
from engine.backtest import BacktestEngine
from strategy.base_strategy import BaseStrategy
from utils.logger import setup_logging


def load_config(config_path: str = "config/backtest.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def resolve_strategy(strategy_path: str):
    """将 'strategy.double_ma' 解析为类对象。"""
    module_name, class_name = strategy_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def main() -> int:
    parser = argparse.ArgumentParser(description="A股模拟量化交易回测")
    parser.add_argument("--strategy", required=True, help="策略类路径，如 strategy.double_ma.DoubleMAStrategy")
    parser.add_argument("--start", default="20210101", help="回测起始日期 YYYYMMDD")
    parser.add_argument("--end", default="20231231", help="回测结束日期 YYYYMMDD")
    parser.add_argument("--capital", type=float, default=1_000_000, help="初始资金")
    parser.add_argument("--config", default="config/backtest.yaml", help="配置文件路径")
    parser.add_argument("--output", default="output/report", help="报告输出目录")
    parser.add_argument("--frequency", default=None, help="回测频率：daily / 1min / 5min / 15min / 30min / 60min")
    args = parser.parse_args()

    # 加载配置
    cfg = load_config(args.config)
    setup_logging(level=cfg.get("logging", {}).get("level", "INFO"))

    # 初始化数据源
    data_cfg = cfg.get("data", {})
    source_type = data_cfg.get("source", "akshare")
    if source_type == "tushare":
        data_source = TushareDataSource(token=data_cfg.get("tushare_token", ""))
    else:
        data_source = AKShareDataSource()

    # 解析策略类
    try:
        strategy_cls = resolve_strategy(args.strategy)
    except Exception as e:
        print(f"策略加载失败: {e}")
        return 1

    if not issubclass(strategy_cls, BaseStrategy):
        print(f"{args.strategy} 不是 BaseStrategy 的子类")
        return 1

    # 确定频率与止损配置
    frequency = args.frequency or cfg.get("backtest", {}).get("frequency", "daily")
    stop_loss_cfg = cfg.get("stop_loss", {})

    # 运行回测
    engine = BacktestEngine(
        strategy_cls=strategy_cls,
        data_source=data_source,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        benchmark=cfg.get("backtest", {}).get("benchmark", "000300.SH"),
        frequency=frequency,
        stop_loss_enabled=stop_loss_cfg.get("enabled", False),
        stop_loss_threshold=stop_loss_cfg.get("threshold", 0.05),
    )
    nav_df = engine.run()

    if nav_df.empty:
        print("回测结果为空")
        return 1

    # 绩效分析
    metrics = calculate_metrics(nav_df, engine.benchmark_df, frequency=frequency)
    print(f"\n{'='*40}")
    print(f"累计收益率: {metrics.total_return:.2%}")
    print(f"年化收益率: {metrics.annual_return:.2%}")
    print(f"最大回撤:   {metrics.max_drawdown:.2%}")
    print(f"夏普比率:   {metrics.sharpe_ratio:.2f}")
    print(f"{'='*40}\n")

    # 可视化
    plotter = Plotter()
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)

    plotter.plot_cumulative_returns(nav_df, engine.benchmark_df, save_path=out / "cum_returns.png")
    plotter.plot_drawdown(nav_df, save_path=out / "drawdown.png")
    plotter.plot_monthly_returns(nav_df, save_path=out / "monthly_returns.png")

    # 生成报告
    report_path = generate_html_report(metrics, output_dir=str(out))
    print(f"报告已生成: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
