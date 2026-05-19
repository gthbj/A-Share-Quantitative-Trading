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


# 上交所代码前缀（沪市主板 / 科创板 / ETF / LOF / 转债）
_SH_PREFIXES = ("60", "68", "51", "56", "58", "11")
# 深交所代码前缀（深市主板 / 创业板 / ETF / LOF）
_SZ_PREFIXES = ("00", "30", "15", "16")


def _normalize_code(raw: str) -> str:
    """把用户输入归一化为框架代码格式 'XXXXXX.SH' / 'XXXXXX.SZ'。

    支持的输入：
      - '510300.SH' / '510300.sh'  → '510300.SH'
      - '510300'                   → '510300.SH'（按前缀推断）
      - '000001'                   → '000001.SZ'
    """
    s = raw.strip().upper()
    if not s:
        raise ValueError("代码为空")
    if "." in s:
        bare, _, suffix = s.partition(".")
        if suffix not in ("SH", "SZ"):
            raise ValueError(f"未知交易所后缀: {raw}（应为 .SH 或 .SZ）")
        if not bare.isdigit() or len(bare) != 6:
            raise ValueError(f"代码格式错误: {raw}（应为 6 位数字）")
        return f"{bare}.{suffix}"
    # 裸 6 位代码：按前缀推断
    if not s.isdigit() or len(s) != 6:
        raise ValueError(f"代码格式错误: {raw}（应为 6 位数字 + 可选 .SH/.SZ 后缀）")
    if s.startswith(_SH_PREFIXES):
        return f"{s}.SH"
    if s.startswith(_SZ_PREFIXES):
        return f"{s}.SZ"
    raise ValueError(f"无法识别代码所属交易所: {raw}（请显式写明 .SH 或 .SZ）")


def _parse_universe(raw: str) -> list:
    """把逗号/空格分隔的字符串解析为代码列表，并归一化。"""
    if not raw:
        return []
    parts = [p for p in raw.replace(",", " ").split() if p]
    return [_normalize_code(p) for p in parts]


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
    parser.add_argument("--frequency", default=None, help="回测频率：daily / 1min / 5min / 15min / 30min / 60min")
    parser.add_argument(
        "--universe",
        default="",
        help="回测标的代码（多个用逗号分隔，如 510300.SH,510500.SH）；"
        "留空时优先用 preset 里的 universe，否则进入交互式询问",
    )
    args = parser.parse_args()

    # 加载全局配置
    cfg = load_config(args.config)
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

    # 初始化数据源（当前固定使用 MaxCompute）
    try:
        data_source = build_maxcompute_data_source(cfg)
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
        strategy_class_path=strategy_path,
        strategy_doc=(strategy_inst.__class__.__doc__ or "") if strategy_inst else "",
        universe=strategy_inst.get_universe() if strategy_inst else [],
        start_date=start_date,
        end_date=end_date,
        initial_capital=capital,
        frequency=frequency,
        benchmark=benchmark,
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
