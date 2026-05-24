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
from analytics.gcs_archive import apply_gcs_archive_uri, archive_backtest_output
from analytics.daily_diagnostics import write_daily_diagnostics
from data_layer.bigquery_source import BigQueryDataSource
from engine.backtest import BacktestEngine
from engine.backtest import DailyRecord
from engine.trade_engine import OrderSide, TradeEngine
from strategy.base_strategy import BaseStrategy
from utils.code import normalize_code as _normalize_code
from utils.code import parse_universe as _parse_universe
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


def build_bigquery_data_source(cfg: dict) -> BigQueryDataSource:
    """根据 config + secrets + 环境变量构造 BigQuery 数据源。

    凭据优先级：环境变量 GOOGLE_APPLICATION_CREDENTIALS > secrets.yaml。
    """
    secrets = load_secrets()
    data_cfg = cfg.get("data", {})
    bq_cfg = data_cfg.get("bigquery", {})
    bq_secrets = secrets.get("bigquery", {})
    account_cfg = cfg.get("account", {}) or {}

    credentials_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS") or bq_secrets.get("credentials_path", "")

    cache_cfg = data_cfg.get("cache", {})
    return BigQueryDataSource(
        project_id=bq_cfg.get("project_id", ""),
        dataset=bq_cfg.get("dataset", "ashare_core"),
        location=bq_cfg.get("location", "asia-east2"),
        credentials_path=credentials_path,
        cache_retention_days=cache_cfg.get("retention_days", 7),
        cache_max_size_gb=cache_cfg.get("max_size_gb", 1.0),
        tables=bq_cfg.get("tables", {}),
        trading_permissions=account_cfg.get("trading_permissions", {}),
    )


def resolve_strategy(strategy_path: str):
    """将 'strategy.double_ma' 解析为类对象。"""
    module_name, class_name = strategy_path.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, class_name)


def load_strategy_preset(preset_name: str) -> dict:
    """加载 strategy/{name}/config.yaml，作为该策略实验包的默认配置。

    返回 dict 结构示例:
        {
            "name": "double_ma",
            "class": "strategy.double_ma.DoubleMAStrategy",
            "params": {"short_window": 5, "long_window": 20, "universe": [...]},
            "backtest": {"start_date": "20160101", "frequency": "15min", ...},
        }
    """
    path = Path("strategy") / preset_name / "config.yaml"
    if not path.exists():
        raise FileNotFoundError(
            f"策略 preset 不存在: {path}\n"
            f"可用 preset 位于 strategy/<name>/config.yaml；当前可用列表请看 strategy/ 目录。"
        )
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve_universe(cli_value: str, default: str = "510300.SH") -> list:
    """确定本次回测的标的列表：

      - CLI 提供了 --universe → 直接用
      - 否则交互式提示用户输入（按回车使用默认值）
    """
    if cli_value:
        codes = _parse_universe(cli_value)
        if not codes:
            raise ValueError("--universe 解析后为空")
        return codes

    print("\n" + "=" * 50)
    print("请输入本次回测的标的（多个用逗号或空格分隔）")
    print(f"示例: 510300.SH    或   510300.SH,510500.SH")
    print(f"直接回车使用默认: {default}")
    print("=" * 50)
    try:
        raw = input("标的代码: ").strip()
    except EOFError:
        raw = ""

    if not raw:
        codes = _parse_universe(default)
    else:
        codes = _parse_universe(raw)

    print(f"→ 本次回测标的: {codes}\n")
    return codes


def main() -> int:
    parser = argparse.ArgumentParser(description="A股模拟量化交易回测")
    parser.add_argument(
        "--preset",
        default=None,
        help="策略实验包名称（对应 strategy/<name>/config.yaml）。"
        "指定后会加载该 preset 作为默认值；其他 CLI 参数仍可覆盖。",
    )
    parser.add_argument(
        "--strategy",
        default=None,
        help="策略类路径，如 strategy.double_ma.DoubleMAStrategy。"
        "如使用 --preset 则可省略（会从 preset 中读取）。",
    )
    parser.add_argument("--start", default=None, help="回测起始日期 YYYYMMDD（默认从 preset 或 20210101）")
    parser.add_argument("--end", default=None, help="回测结束日期 YYYYMMDD（默认从 preset 或 20231231）")
    parser.add_argument("--capital", type=float, default=None, help="初始资金（默认从 preset 或 1,000,000）")
    parser.add_argument("--config", default="config/backtest.yaml", help="全局配置文件路径")
    parser.add_argument(
        "--output",
        default=None,
        help="报告输出根目录。默认逻辑：--preset 模式 → strategy/<name>/runs/，否则 → output/",
    )
    parser.add_argument(
        "--run-name",
        default=None,
        help="可选的运行标签，加在时间戳后作为子目录后缀，例如 20260519_181500_doublema",
    )
    parser.add_argument(
        "--no-gcs-archive",
        action="store_true",
        help="跳过本次回测产物 GCS 归档（用于本地调试）。",
    )
    parser.add_argument(
        "--gcs-archive-uri",
        default=None,
        help="覆盖本次回测产物归档位置，例如 gs://data-aquarium/a-share/backtest_runs_tmp。",
    )
    parser.add_argument("--frequency", default=None, help="回测频率：daily / 1min / 5min / 15min / 30min / 60min")
    parser.add_argument(
        "--daily-diagnostics",
        action="store_true",
        help="启用逐日结构化日志与 daily_log/daily_positions/daily_candidates CSV 输出。",
    )
    parser.add_argument(
        "--daily-candidate-top-n",
        type=int,
        default=10,
        help="逐日诊断中每日候选股 Top N 数量。",
    )
    parser.add_argument(
        "--universe",
        default="",
        help="回测标的代码（多个用逗号分隔，如 510300.SH,510500.SH）；"
        "留空时优先用 preset 里的 universe，否则进入交互式询问",
    )
    args = parser.parse_args()

    # 加载全局配置
    cfg = load_config(args.config)
    if args.no_gcs_archive:
        cfg.setdefault("output", {}).setdefault("gcs_archive", {})["enabled"] = False
    if args.gcs_archive_uri:
        try:
            apply_gcs_archive_uri(cfg, args.gcs_archive_uri)
        except ValueError as e:
            print(f"--gcs-archive-uri 解析失败: {e}")
            return 1
    setup_logging(level=cfg.get("logging", {}).get("level", "INFO"))

    # ── 加载 preset（若指定）──
    preset_cfg: dict = {}
    if args.preset:
        try:
            preset_cfg = load_strategy_preset(args.preset)
            print(f"已加载 preset: {args.preset} ({preset_cfg.get('description', '')})")
        except FileNotFoundError as e:
            print(str(e))
            return 1

    # ── 确定策略类路径（CLI --strategy > preset.class）──
    strategy_path = args.strategy or preset_cfg.get("class")
    if not strategy_path:
        print("错误：必须指定 --strategy 或 --preset 之一")
        return 1

    # 初始化数据源（当前默认使用 BigQuery）
    try:
        data_source = build_bigquery_data_source(cfg)
    except RuntimeError as e:
        print(str(e))
        return 1

    # 解析策略类
    try:
        strategy_cls = resolve_strategy(strategy_path)
    except Exception as e:
        print(f"策略加载失败: {e}")
        return 1

    if not issubclass(strategy_cls, BaseStrategy):
        print(f"{strategy_path} 不是 BaseStrategy 的子类")
        return 1

    # ── 确定 universe（CLI > preset.params.universe > 类默认 / 交互式）──
    preset_params = preset_cfg.get("params", {}) or {}
    preset_universe = preset_params.get("universe", [])
    if args.universe:
        try:
            universe = _parse_universe(args.universe)
        except ValueError as e:
            print(f"--universe 解析失败: {e}")
            return 1
    elif preset_universe:
        try:
            universe = [_normalize_code(c) for c in preset_universe]
        except ValueError as e:
            print(f"preset 中 universe 解析失败: {e}")
            return 1
        print(f"使用 preset 标的: {universe}")
    elif getattr(strategy_cls, "DYNAMIC_UNIVERSE", False):
        universe = []
        print("策略使用动态 universe，将由策略 initialize 阶段从数据源加载标的。")
    else:
        default_universe = ",".join(getattr(strategy_cls, "DEFAULT_UNIVERSE", ["510300.SH"]))
        try:
            universe = resolve_universe("", default=default_universe)
        except ValueError as e:
            print(f"标的解析失败: {e}")
            return 1

    # ── 确定回测参数（CLI > preset.backtest > 全局 cfg.backtest > 内置默认）──
    preset_bt = preset_cfg.get("backtest", {}) or {}
    global_bt = cfg.get("backtest", {}) or {}
    start_date = args.start or preset_bt.get("start_date") or global_bt.get("start_date", "20210101")
    end_date = args.end or preset_bt.get("end_date") or global_bt.get("end_date", "20231231")
    capital = args.capital or preset_bt.get("initial_capital") or global_bt.get("initial_capital", 1_000_000)
    frequency = (
        args.frequency
        or preset_bt.get("frequency")
        or global_bt.get("frequency", "daily")
    )
    benchmark = preset_bt.get("benchmark") or global_bt.get("benchmark", "000300.SH")
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

    # 策略构造参数：preset.params 里除 universe 之外的字段透传给策略构造函数
    strategy_kwargs = {k: v for k, v in preset_params.items() if k != "universe"}
    strategy_kwargs["universe"] = universe

    # 运行回测
    engine = BacktestEngine(
        strategy_cls=strategy_cls,
        data_source=data_source,
        start_date=start_date,
        end_date=end_date,
        initial_capital=capital,
        benchmark=benchmark,
        trade_engine=trade_engine,
        frequency=frequency,
        stop_loss_enabled=stop_loss_cfg.get("enabled", False),
        stop_loss_threshold=stop_loss_cfg.get("threshold", 0.05),
        strategy_kwargs=strategy_kwargs,
        daily_log_enabled=args.daily_diagnostics,
        daily_candidate_top_n=args.daily_candidate_top_n,
        comparison_benchmarks={
            "hs300": "000300.SH",
            "sz50": "000016.SH",
            "zz500": "000905.SH",
            "zz1000": "000852.SH",
            "chinext": "399006.SZ",
        },
    )
    nav_df = engine.run()

    if nav_df.empty:
        print("回测结果为空")
        return 1

    # 提取全部成交流水（先于 metrics，因为 metrics 需要 fills 计算交易统计）
    trade_rows = []
    all_fills = []  # 喂给 metrics 用于 FIFO 配对的胜率/盈亏比
    for record in engine.records:
        for fill in record.fills:
            all_fills.append(fill)
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

    # 绩效分析（带 fills 才能算出胜率与盈亏比）
    metrics = calculate_metrics(
        nav_df, engine.benchmark_df, fills=all_fills, frequency=frequency
    )
    print(f"\n{'='*40}")
    print(f"累计收益率: {metrics.total_return:.2%}")
    print(f"年化收益率: {metrics.annual_return:.2%}")
    print(f"最大回撤:   {metrics.max_drawdown:.2%}")
    print(f"夏普比率:   {metrics.sharpe_ratio:.2f}")
    print(f"{'='*40}\n")

    # ── 输出目录：每次运行创建独立子目录，不覆盖历史 ──
    # 优先级：--output > strategy/<preset>/runs/（preset 模式）> output/（兜底）
    if args.output:
        output_root = Path(args.output)
    elif args.preset:
        output_root = Path("strategy") / args.preset / "runs"
    else:
        output_root = Path("output")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_label = timestamp if not args.run_name else f"{timestamp}_{args.run_name}"
    out = output_root / run_label
    out.mkdir(parents=True, exist_ok=True)

    # 可视化
    plotter = Plotter()
    plotter.plot_cumulative_returns(nav_df, engine.benchmark_df, save_path=out / "cum_returns.png")
    plotter.plot_drawdown(nav_df, save_path=out / "drawdown.png")
    plotter.plot_monthly_returns(nav_df, save_path=out / "monthly_returns.png")

    # 准备策略元信息（HTML 与 Markdown 报告共用）
    strategy_inst = engine.strategy_instance
    universe_for_report = strategy_inst.get_universe() if strategy_inst else []
    strategy_doc_for_report = (
        (strategy_inst.__class__.__doc__ or "") if strategy_inst else ""
    )
    buy_count = sum(
        1 for r in engine.records for f in r.fills if f.side == OrderSide.BUY
    )
    sell_count = sum(
        1 for r in engine.records for f in r.fills if f.side == OrderSide.SELL
    )

    # HTML 报告（完整元信息 + 图表 + 交易明细）
    report_path = generate_html_report(
        metrics,
        output_dir=str(out),
        strategy_class_path=strategy_path,
        strategy_doc=strategy_doc_for_report,
        universe=universe_for_report,
        start_date=start_date,
        end_date=end_date,
        initial_capital=capital,
        frequency=frequency,
        benchmark=benchmark,
        config=cfg,
        nav_records_count=len(nav_df),
        fills_buy_count=buy_count,
        fills_sell_count=sell_count,
        data_source_name="Google Cloud BigQuery",
        trade_rows=trade_rows,
    )

    # 保存完整成交流水 CSV
    if trade_rows:
        import pandas as _pd
        trades_df = _pd.DataFrame(trade_rows)
        trades_df.to_csv(out / "trades.csv", index=False, encoding="utf-8-sig")

    nav_df.to_csv(out / "nav.csv", index=True, index_label="date", encoding="utf-8-sig")
    if args.daily_diagnostics:
        diag_paths = write_daily_diagnostics(out, engine.records)
        for name, path in diag_paths.items():
            print(f"  - {name}: {path}")

    benchmark_loaded = (
        engine.benchmark_df is not None and not engine.benchmark_df.empty
    )

    # Markdown 说明文件（策略 / 数据 / 参数 / 绩效）
    summary_path = generate_markdown_summary(
        output_dir=out,
        metrics=metrics,
        strategy_class_path=strategy_path,
        strategy_doc=strategy_doc_for_report,
        universe=universe_for_report,
        start_date=start_date,
        end_date=end_date,
        initial_capital=capital,
        frequency=frequency,
        benchmark=benchmark,
        config=cfg,
        nav_records_count=len(nav_df),
        fills_buy_count=buy_count,
        fills_sell_count=sell_count,
        data_source_name="Google Cloud BigQuery",
        trade_rows=trade_rows,
        benchmark_loaded=benchmark_loaded,
    )

    print(f"输出目录: {out}")
    print(f"  - HTML 报告: {report_path}")
    print(f"  - Markdown 说明: {summary_path}")
    archive_cfg = ((cfg.get("output", {}) or {}).get("gcs_archive", {}) or {})
    try:
        strategy_key = args.preset or strategy_path.rsplit(".", 1)[-1]
        archive_result = archive_backtest_output(
            out,
            cfg,
            strategy_key=strategy_key,
            strategy_class_path=strategy_path,
            run_label=run_label,
            start_date=start_date,
            end_date=end_date,
            initial_capital=float(capital),
            frequency=frequency,
            benchmark=benchmark,
        )
    except Exception as e:
        print(f"GCS 归档失败: {e}")
        if archive_cfg.get("fail_on_error", True):
            return 1
    else:
        if archive_result:
            print(f"  - GCS 归档: {archive_result.uri} ({len(archive_result.uploaded_files)} files)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
