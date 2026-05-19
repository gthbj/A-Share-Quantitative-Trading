"""CLI 入口：一键运行回测。

用法示例：
    python run_backtest.py --strategy strategy.double_ma --start 20210101 --end 20231231
"""

from __future__ import annotations

import argparse
import importlib
import os
import sys
from datetime import datetime
from pathlib import Path

import yaml

from analytics.metrics import calculate_metrics
from analytics.plotter import Plotter
from analytics.report import generate_html_report
from analytics.summary import generate_markdown_summary
from data_layer.maxcompute_source import MaxComputeDataSource
from engine.backtest import BacktestEngine
from engine.backtest import DailyRecord
from engine.trade_engine import OrderSide, TradeEngine
from strategy.base_strategy import BaseStrategy
from utils.logger import setup_logging


def load_config(config_path: str = "config/backtest.yaml") -> dict:
    with open(config_path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_secrets(secrets_path: str = "config/secrets.yaml") -> dict:
    """加载敏感配置（API key 等）。文件不存在则返回空 dict，由上层退化为环境变量。"""
    p = Path(secrets_path)
    if not p.exists():
        return {}
    with open(p, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_maxcompute_data_source(cfg: dict) -> MaxComputeDataSource:
    """根据 config + secrets + 环境变量构造 MaxCompute 数据源。

    凭据优先级：环境变量 > secrets.yaml。
    """
    secrets = load_secrets()
    data_cfg = cfg.get("data", {})
    mc_cfg = data_cfg.get("maxcompute", {})
    mc_secrets = secrets.get("maxcompute", {})

    access_id = os.environ.get("MAXCOMPUTE_ACCESS_ID") or mc_secrets.get("access_id", "")
    access_key = os.environ.get("MAXCOMPUTE_ACCESS_KEY") or mc_secrets.get("access_key", "")

    if not access_id or not access_key:
        raise RuntimeError(
            "缺少 MaxCompute 凭据：请在 config/secrets.yaml 中配置 access_id / access_key，"
            "或设置环境变量 MAXCOMPUTE_ACCESS_ID / MAXCOMPUTE_ACCESS_KEY。"
        )

    cache_cfg = data_cfg.get("cache", {})
    return MaxComputeDataSource(
        access_id=access_id,
        access_key=access_key,
        project=mc_cfg.get("project", ""),
        endpoint=mc_cfg.get("endpoint", ""),
        cache_retention_days=cache_cfg.get("retention_days", 7),
        cache_max_size_gb=cache_cfg.get("max_size_gb", 1.0),
        tables=mc_cfg.get("tables", {}),
    )


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
    parser.add_argument(
        "--output",
        default="output",
        help="报告输出根目录；每次运行会在其下创建带时间戳的子目录 YYYYMMDD_HHMMSS/",
    )
    parser.add_argument(
        "--run-name",
        default=None,
        help="可选的运行标签，加在时间戳后作为子目录后缀，例如 20260519_181500_doublema",
    )
    parser.add_argument("--frequency", default=None, help="回测频率：daily / 1min / 5min / 15min / 30min / 60min")
    args = parser.parse_args()

    # 加载配置
    cfg = load_config(args.config)
    setup_logging(level=cfg.get("logging", {}).get("level", "INFO"))

    # 初始化数据源（当前固定使用 MaxCompute）
    try:
        data_source = build_maxcompute_data_source(cfg)
    except RuntimeError as e:
        print(str(e))
        return 1

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

    # 用配置文件构造撮合引擎（佣金/印花税/滑点/撮合规则等）
    # 否则会退化到 TradeEngine 的默认值（滑点 0.001 等），与 backtest.yaml 不一致。
    trading_cfg = cfg.get("trading", {})
    slip_cfg = cfg.get("slippage", {})
    exec_cfg = cfg.get("execution", {})
    trade_engine = TradeEngine(
        commission_rate=trading_cfg.get("commission_rate", 0.00025),
        min_commission=trading_cfg.get("min_commission", 5.0),
        stamp_duty_rate=trading_cfg.get("stamp_duty_rate", 0.0005),
        transfer_fee_rate=trading_cfg.get("transfer_fee_rate", 0.00001),
        slippage_type=slip_cfg.get("type", "percent"),
        slippage_value=slip_cfg.get("value", 0.001),
        volume_limit=exec_cfg.get("volume_limit", 0.10),
        price_type=exec_cfg.get("price_type", "next_open"),
    )

    # 运行回测
    engine = BacktestEngine(
        strategy_cls=strategy_cls,
        data_source=data_source,
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        benchmark=cfg.get("backtest", {}).get("benchmark", "000300.SH"),
        trade_engine=trade_engine,
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

    # 提取全部成交流水（用于报告中的交易明细和费用汇总）
    trade_rows = []
    for record in engine.records:
        for fill in record.fills:
            trade_rows.append({
                "date": record.date,
                "side": "买入" if fill.side == OrderSide.BUY else "卖出",
                "code": fill.code,
                "qty": fill.qty,
                "price": round(fill.price, 4),
                "amount": round(fill.qty * fill.price, 2),
                "commission": round(fill.commission, 2),
                "stamp_duty": round(fill.stamp_duty, 2),
                "transfer_fee": round(fill.transfer_fee, 2),
                "total_fee": round(fill.total_cost, 2),
            })

    # ── 输出目录：每次运行创建独立子目录，不覆盖历史 ──
    # 子目录命名：YYYYMMDD_HHMMSS[_run-name]
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_label = timestamp if not args.run_name else f"{timestamp}_{args.run_name}"
    out = Path(args.output) / run_label
    out.mkdir(parents=True, exist_ok=True)

    # 可视化
    plotter = Plotter()
    plotter.plot_cumulative_returns(nav_df, engine.benchmark_df, save_path=out / "cum_returns.png")
    plotter.plot_drawdown(nav_df, save_path=out / "drawdown.png")
    plotter.plot_monthly_returns(nav_df, save_path=out / "monthly_returns.png")

    # HTML 报告
    report_path = generate_html_report(metrics, output_dir=str(out))

    # 保存完整成交流水 CSV
    if trade_rows:
        import pandas as _pd
        trades_df = _pd.DataFrame(trade_rows)
        trades_df.to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")

    # Markdown 说明文件（策略 / 数据 / 参数 / 绩效）
    buy_count = sum(
        1 for r in engine.records for f in r.fills if f.side == OrderSide.BUY
    )
    sell_count = sum(
        1 for r in engine.records for f in r.fills if f.side == OrderSide.SELL
    )
    strategy_inst = engine.strategy_instance
    summary_path = generate_markdown_summary(
        output_dir=out,
        metrics=metrics,
        strategy_class_path=args.strategy,
        strategy_doc=(strategy_inst.__class__.__doc__ or "") if strategy_inst else "",
        universe=strategy_inst.get_universe() if strategy_inst else [],
        start_date=args.start,
        end_date=args.end,
        initial_capital=args.capital,
        frequency=frequency,
        benchmark=cfg.get("backtest", {}).get("benchmark", "000300.SH"),
        config=cfg,
        nav_records_count=len(nav_df),
        fills_buy_count=buy_count,
        fills_sell_count=sell_count,
        data_source_name="阿里云 MaxCompute",
        trade_rows=trade_rows,
    )

    print(f"输出目录: {out}")
    print(f"  - HTML 报告: {report_path}")
    print(f"  - Markdown 说明: {summary_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
