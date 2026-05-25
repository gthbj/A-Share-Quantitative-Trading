"""回测主引擎：驱动策略运行、调度交易日/Bar、记录净值。

支持日线（daily）与分钟级（1min/5min/15min/30min/60min）双频回测。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import pandas as pd

from account.portfolio import Portfolio
from data_layer.base_data_source import BaseDataSource
from engine.trade_engine import Fill, Order, OrderSide, OrderType, TradeEngine
from strategy.base_strategy import BaseStrategy, Context
from utils.calendar import TradingCalendar
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class DailyRecord:
    """单根 Bar 回测记录。"""

    date: str
    nav: float  # 净值
    cash: float
    positions: Dict[str, int]  # code -> qty
    fills: List[Fill] = field(default_factory=list)
    summary: Dict[str, Any] = field(default_factory=dict)
    position_details: List[Dict[str, Any]] = field(default_factory=list)
    candidate_details: List[Dict[str, Any]] = field(default_factory=list)


class BacktestEngine:
    """回测引擎。

    按交易日历逐日/逐 Bar 推进，调用策略生命周期方法，撮合订单并记录净值曲线。
    """

    def __init__(
        self,
        strategy_cls: Type[BaseStrategy],
        data_source: BaseDataSource,
        start_date: str,
        end_date: str,
        initial_capital: float = 1_000_000.0,
        benchmark: str = "000300.SH",
        trade_engine: Optional[TradeEngine] = None,
        frequency: str = "daily",
        stop_loss_enabled: bool = False,
        stop_loss_threshold: float = 0.05,
        strategy_kwargs: Optional[Dict[str, Any]] = None,
        daily_log_enabled: bool = False,
        comparison_benchmarks: Optional[Dict[str, str]] = None,
        daily_candidate_top_n: int = 10,
        early_stop_excess_vs_hs300: Optional[float] = None,
        bar_adjust: Optional[str] = "none",
    ) -> None:
        self.strategy_cls = strategy_cls
        self.data_source = data_source
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.benchmark = benchmark
        self.trade_engine = trade_engine or TradeEngine()
        self.frequency = frequency
        self.stop_loss_enabled = stop_loss_enabled
        self.stop_loss_threshold = stop_loss_threshold
        # 策略构造函数参数（如 universe / short_window / long_window 等）
        self.strategy_kwargs: Dict[str, Any] = strategy_kwargs or {}
        self.daily_log_enabled = daily_log_enabled
        self.comparison_benchmarks: Dict[str, str] = comparison_benchmarks or {}
        self.daily_candidate_top_n = max(int(daily_candidate_top_n), 0)
        self.early_stop_excess_vs_hs300 = early_stop_excess_vs_hs300
        self.bar_adjust = bar_adjust if bar_adjust in ("qfq", "hfq") else "none"

        # 校验止损阈值
        if self.stop_loss_enabled and self.stop_loss_threshold <= 0:
            raise ValueError("stop_loss_threshold 必须为正数")

        self.calendar = TradingCalendar()
        self.records: List[DailyRecord] = []
        self.early_stop_triggered = False
        self.early_stop_reason = ""
        self.early_stop_date = ""
        self.early_stop_value: Optional[float] = None
        self.benchmark_df: Optional[pd.DataFrame] = None
        self.comparison_benchmark_dfs: Dict[str, pd.DataFrame] = {}
        # 待执行的止损队列：code -> qty，由前一日收盘后检查写入
        self._stop_loss_pending: Dict[str, int] = {}
        # 暴露给外层报告/总结使用：策略实例（含 universe、docstring）
        self.strategy_instance: Optional[BaseStrategy] = None
        self._run_started_at: float = 0.0

    def _is_intraday(self) -> bool:
        """判断当前是否为分钟级回测。"""
        return self.frequency != "daily"

    def run(self) -> pd.DataFrame:
        """运行回测，返回每日/每 Bar 净值 DataFrame。"""
        trading_days = self.calendar.get_trading_days(self.start_date, self.end_date)
        if not trading_days:
            logger.warning("回测区间内无交易日")
            return pd.DataFrame()

        # 初始化策略与上下文
        portfolio = Portfolio(initial_capital=self.initial_capital)
        context = Context(
            portfolio=portfolio,
            data_source=self.data_source,
            current_date=trading_days[0].strftime("%Y%m%d"),
            frequency=self.frequency,
            _cancel_callback=self.trade_engine.cancel,
        )
        strategy = self.strategy_cls(**self.strategy_kwargs)
        strategy.initialize(context)
        self.strategy_instance = strategy  # 暴露给外层（universe / docstring 用）

        # 获取Universe列表
        universe = strategy.get_universe()
        logger.info(
            f"回测启动: universe={len(universe)} 只股票, "
            f"区间={self.start_date}~{self.end_date}, 频率={self.frequency}"
        )

        # 预加载所有行情数据，并注入到 Context（get_price 优先查内存）
        all_bars = self._preload_bars(
            universe, trading_days, lookback_days=getattr(strategy, "lookback_days", 0)
        )
        context.all_bars = all_bars
        self.benchmark_df = self._load_benchmark(trading_days)
        if self.daily_log_enabled:
            self.comparison_benchmark_dfs = self._load_comparison_benchmarks(
                trading_days
            )

        self._run_started_at = time.monotonic()
        if self._is_intraday():
            self._run_intraday(trading_days, strategy, context, portfolio, all_bars)
        else:
            self._run_daily(trading_days, strategy, context, portfolio, all_bars)

        logger.info("回测结束")
        return self._build_result_df()

    def _run_daily(
        self,
        trading_days: List[Any],
        strategy: BaseStrategy,
        context: Context,
        portfolio: Portfolio,
        all_bars: Dict[str, pd.DataFrame],
    ) -> None:
        """日线回测主循环。

        订单执行顺序遵循 next_open 语义：
          - 策略在 day T 的 close 数据上产生信号（handle_data）
          - 订单在 day T+1 的 open 成交（下一轮循环 pop_orders）
        止损订单同理：day T 收盘后触发 → day T+1 开盘前生成 → day T+1 开盘成交。
        """
        for i, date_obj in enumerate(trading_days):
            date_str = date_obj.strftime("%Y%m%d")
            context.current_date = date_str

            # 1. 构建当日 bar 字典 {code: Series}
            today_bars = {}
            for code in strategy.get_universe():
                df = all_bars.get(code)
                if df is not None and not df.empty:
                    row = df[df["date"] == date_str]
                    if not row.empty:
                        today_bars[code] = row.iloc[0].copy()

            # 2. 更新前一日收盘价（用于涨跌停判定）
            if i > 0:
                prev_date = trading_days[i - 1].strftime("%Y%m%d")
                for code in today_bars:
                    df = all_bars.get(code)
                    if df is not None:
                        prow = df[df["date"] == prev_date]
                        if not prow.empty:
                            today_bars[code]["prev_close"] = prow.iloc[0]["close"]

            # 3. 开盘前（含上一日止损订单生成）
            portfolio.before_trading(date_str)
            stop_loss_orders = self._generate_stop_loss_orders(portfolio)
            strategy.before_trading_start(context, today_bars)

            # 3.5 扫描前期挂单（LIMIT/STOP；PRD_20260520_10）
            # 日线回测每日只有一根 bar，is_last_bar_of_day 恒为 True，DAY 单当日过期
            sweep_fills = self.trade_engine.sweep_pending(
                portfolio, today_bars, date_str, is_last_bar_of_day=True
            )

            # 4. 执行上一交易日 handle_data 产生的订单（next_open 语义）
            #    以及上一日收盘后触发的止损订单，均在今日开盘价成交。
            orders = context.pop_orders()
            orders.extend(stop_loss_orders)
            for order in orders:
                if order.side == OrderSide.BUY:
                    price = today_bars.get(order.code, {}).get("open", 0.0)
                    if price > 0:
                        est_amount = min(order.qty * price, portfolio.available_cash)
                        ok = portfolio.reserve_cash(est_amount)
                        if not ok:
                            order.qty = 0
            orders = [o for o in orders if o.qty > 0]

            fills = self.trade_engine.execute_orders(
                orders, portfolio, today_bars, date_str
            )
            fills = sweep_fills + fills  # 合并挂单成交与新单成交

            # 5. 盘中处理：策略基于今日 close 产生信号，订单留存至明日开盘执行
            strategy.handle_data(context, today_bars)

            # 6. 收盘后
            strategy.after_trading_end(context, today_bars)

            # 7. 收盘后止损检查
            self._check_stop_loss(portfolio, today_bars, date_str)

            # 8. 记录净值
            price_map = {code: bar["close"] for code, bar in today_bars.items()}
            nav = portfolio.total_value(price_map)
            record = self._build_daily_record(
                date_str=date_str,
                day_index=i + 1,
                total_days=len(trading_days),
                nav=nav,
                portfolio=portfolio,
                price_map=price_map,
                fills=fills,
                strategy=strategy,
            )
            self.records.append(record)
            if self.daily_log_enabled:
                self._log_daily_record(record)
            if self._maybe_stop_after_daily_record(record):
                break

    def _build_daily_record(
        self,
        date_str: str,
        day_index: int,
        total_days: int,
        nav: float,
        portfolio: Portfolio,
        price_map: Dict[str, float],
        fills: List[Fill],
        strategy: BaseStrategy,
    ) -> DailyRecord:
        """构造每日记录，并在启用诊断时附带 summary/position/candidate 明细。"""
        cash = portfolio.available_cash + portfolio.frozen_cash
        positions = {
            code: pos.total_qty for code, pos in portfolio.positions.items()
        }
        record = DailyRecord(
            date=date_str,
            nav=nav,
            cash=cash,
            positions=positions,
            fills=fills,
        )
        if not self.daily_log_enabled:
            return record

        summary = self._build_daily_summary(
            date_str=date_str,
            day_index=day_index,
            total_days=total_days,
            nav=nav,
            portfolio=portfolio,
            price_map=price_map,
            fills=fills,
            strategy=strategy,
        )
        record.summary = summary
        record.position_details = self._build_daily_position_details(
            date_str=date_str,
            portfolio=portfolio,
            price_map=price_map,
            strategy=strategy,
            summary=summary,
        )
        record.candidate_details = self._build_daily_candidate_details(
            date_str=date_str,
            portfolio=portfolio,
            strategy=strategy,
            summary=summary,
        )
        return record

    def _build_daily_summary(
        self,
        date_str: str,
        day_index: int,
        total_days: int,
        nav: float,
        portfolio: Portfolio,
        price_map: Dict[str, float],
        fills: List[Fill],
        strategy: BaseStrategy,
    ) -> Dict[str, Any]:
        """构造单日诊断摘要。数值字段用原始小数，CSV/日志再格式化。"""
        prev_nav = self.records[-1].nav if self.records else self.initial_capital
        daily_return = nav / prev_nav - 1 if prev_nav else 0.0
        total_return = nav / self.initial_capital - 1 if self.initial_capital else 0.0
        navs = [r.nav for r in self.records] + [nav]
        peak = navs[0] if navs else nav
        max_drawdown = 0.0
        for v in navs:
            if v > peak:
                peak = v
            if peak:
                max_drawdown = min(max_drawdown, v / peak - 1)

        buy_count = sum(1 for f in fills if f.side == OrderSide.BUY)
        sell_count = sum(1 for f in fills if f.side == OrderSide.SELL)
        fee_total = sum(float(f.total_cost) for f in fills)
        position_value = portfolio.total_position_value(price_map)
        cash = portfolio.available_cash + portfolio.frozen_cash
        elapsed_seconds = max(time.monotonic() - self._run_started_at, 0.0)
        eta_seconds = (
            elapsed_seconds / day_index * max(total_days - day_index, 0)
            if day_index > 0
            else 0.0
        )
        model_dir = str(getattr(strategy, "_current_model_dir", "") or "")
        model_train_end = model_dir.rstrip("/").rsplit("/", 1)[-1] if model_dir else ""
        regime = str(getattr(strategy, "_last_regime", "") or "")
        target_position_count = getattr(strategy, "_last_target_n", "")

        summary: Dict[str, Any] = {
            "date": date_str,
            "day_index": day_index,
            "total_days": total_days,
            "progress_pct": day_index / total_days if total_days else 0.0,
            "elapsed_seconds": elapsed_seconds,
            "eta_seconds": eta_seconds,
            "nav": nav,
            "initial_capital": self.initial_capital,
            "daily_return_pct": daily_return,
            "total_return_pct": total_return,
            "max_drawdown_pct": max_drawdown,
            "cash": cash,
            "position_value": position_value,
            "cash_ratio_pct": cash / nav if nav else 0.0,
            "position_count": sum(1 for p in portfolio.positions.values() if p.total_qty > 0),
            "buy_count": buy_count,
            "sell_count": sell_count,
            "fee_total": fee_total,
            "model_train_end": model_train_end,
            "model_dir": model_dir,
            "regime": regime,
            "target_position_count": target_position_count,
            "benchmark_code": self.benchmark,
        }

        for key in self.comparison_benchmarks:
            stats = self._benchmark_stats_for_date(key, date_str)
            summary[f"{key}_return_pct"] = stats.get("return_pct", "")
            summary[f"{key}_max_drawdown_pct"] = stats.get("max_drawdown_pct", "")
        hs300_ret = summary.get("hs300_return_pct")
        summary["excess_vs_hs300_pct"] = (
            total_return - float(hs300_ret)
            if isinstance(hs300_ret, (int, float))
            else ""
        )
        return summary

    def _build_daily_position_details(
        self,
        date_str: str,
        portfolio: Portfolio,
        price_map: Dict[str, float],
        strategy: BaseStrategy,
        summary: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        rows: List[Dict[str, Any]] = []
        states = getattr(strategy, "_position_state", {}) or {}
        date_diff = getattr(strategy, "_date_diff", None)
        for code, pos in sorted(portfolio.positions.items()):
            if pos.total_qty <= 0:
                continue
            close_price = float(price_map.get(code, 0.0) or 0.0)
            market_value = pos.market_value(close_price)
            unrealized_pnl = (close_price - pos.cost_price) * pos.total_qty
            unrealized_pnl_pct = pos.profit_ratio(close_price) if close_price else 0.0
            state = states.get(code)
            entered_date = getattr(state, "entered_date", "")
            holding_days = (
                date_diff(entered_date, date_str)
                if callable(date_diff) and entered_date
                else ""
            )
            peak_price = float(getattr(state, "peak_price", 0.0) or 0.0)
            drawdown_from_peak = (
                close_price / peak_price - 1 if close_price > 0 and peak_price > 0 else ""
            )
            rows.append({
                "date": date_str,
                "code": code,
                "qty": pos.total_qty,
                "sellable_qty": pos.sellable_qty,
                "cost_price": pos.cost_price,
                "close_price": close_price,
                "market_value": market_value,
                "unrealized_pnl": unrealized_pnl,
                "unrealized_pnl_pct": unrealized_pnl_pct,
                "holding_days": holding_days,
                "peak_price": peak_price,
                "drawdown_from_peak_pct": drawdown_from_peak,
                "is_sellable": pos.sellable_qty > 0,
                "model_train_end": summary.get("model_train_end", ""),
                "regime": summary.get("regime", ""),
            })
        return rows

    def _build_daily_candidate_details(
        self,
        date_str: str,
        portfolio: Portfolio,
        strategy: BaseStrategy,
        summary: Dict[str, Any],
    ) -> List[Dict[str, Any]]:
        score_df = getattr(strategy, "_last_score_df", None)
        if score_df is None or getattr(score_df, "empty", True):
            return []
        try:
            sorted_df = score_df.sort_values("score", ascending=False).head(
                self.daily_candidate_top_n
            )
        except Exception:
            return []

        held_codes = {
            code for code, pos in portfolio.positions.items() if pos.total_qty > 0
        }
        target_n = summary.get("target_position_count") or 0
        rows: List[Dict[str, Any]] = []
        for rank, (_, row) in enumerate(sorted_df.iterrows(), start=1):
            code = str(row.get("code", ""))
            rows.append({
                "date": date_str,
                "rank": rank,
                "code": code,
                "score": float(row.get("score", 0.0) or 0.0),
                "prob_up_h1": float(row.get("prob_up_h1", 0.0) or 0.0),
                "prob_up_h5": float(row.get("prob_up_h5", 0.0) or 0.0),
                "prob_up_h10": float(row.get("prob_up_h10", 0.0) or 0.0),
                "prob_up_h20": float(row.get("prob_up_h20", 0.0) or 0.0),
                "prob_sell": float(row.get("prob_sell", 0.0) or 0.0),
                "is_held": code in held_codes,
                "in_top_target": rank <= int(target_n or 0),
                "model_train_end": summary.get("model_train_end", ""),
                "regime": summary.get("regime", ""),
            })
        return rows

    def _log_daily_record(self, record: DailyRecord) -> None:
        summary = record.summary
        if not summary:
            return
        logger.info("DAY_SUMMARY " + self._as_kv(summary))

        if record.position_details:
            for row in record.position_details:
                logger.info("DAY_POSITION " + self._as_kv(row))

        if record.candidate_details:
            for row in record.candidate_details:
                logger.info("DAY_CANDIDATE " + self._as_kv(row))

    def _maybe_stop_after_daily_record(self, record: DailyRecord) -> bool:
        """按逐日诊断中的相对沪深300超额收益执行可选回测熔断。"""
        threshold = self.early_stop_excess_vs_hs300
        if threshold is None:
            return False
        raw_value = record.summary.get("excess_vs_hs300_pct") if record.summary else ""
        if raw_value == "":
            return False
        try:
            excess = float(raw_value)
        except (TypeError, ValueError):
            return False
        if excess > threshold:
            return False

        self.early_stop_triggered = True
        self.early_stop_date = record.date
        self.early_stop_value = excess
        self.early_stop_reason = (
            f"excess_vs_hs300_pct={excess:.6f} <= threshold={threshold:.6f}"
        )
        logger.warning(
            "EARLY_STOP "
            f"date={record.date} reason=excess_vs_hs300 "
            f"value={excess:.10g} threshold={threshold:.10g}"
        )
        return True

    @staticmethod
    def _as_kv(row: Dict[str, Any]) -> str:
        parts = []
        for key, value in row.items():
            if value is None:
                rendered = ""
            elif isinstance(value, bool):
                rendered = "true" if value else "false"
            elif isinstance(value, float):
                rendered = f"{value:.10g}"
            else:
                rendered = str(value)
            rendered = rendered.replace(" ", "_")
            parts.append(f"{key}={rendered}")
        return " ".join(parts)

    def _run_intraday(
        self,
        trading_days: List[Any],
        strategy: BaseStrategy,
        context: Context,
        portfolio: Portfolio,
        all_bars: Dict[str, pd.DataFrame],
    ) -> None:
        """分钟级回测主循环：逐交易日加载分钟 Bar，逐条推进。

        订单执行顺序遵循 next_open 语义：
          - 策略在 bar T 的 close 数据上产生信号（handle_data）
          - 订单在 bar T+1 的 open 成交（下一根 bar 开始时 pop_orders）
        止损订单：day T 最后一根 bar 收盘后检查触发 → day T+1 第一根 bar 开盘前生成
                  → day T+1 第一根 bar 开盘价成交（与策略延迟订单一同执行）。
        """
        universe = strategy.get_universe()

        for i, date_obj in enumerate(trading_days):
            date_str = date_obj.strftime("%Y%m%d")

            # 按股票组织当日分钟 Bar 列表
            day_minute_bars: Dict[str, pd.DataFrame] = {}
            for code in universe:
                df = all_bars.get(code)
                if df is not None and not df.empty:
                    # 分钟数据 date 格式为 YYYYMMDDHHMM，取前8位匹配交易日
                    day_df = df[df["date"].astype(str).str.startswith(date_str)]
                    if not day_df.empty:
                        day_df = day_df.sort_values("date").reset_index(drop=True)
                        day_minute_bars[code] = day_df

            if not day_minute_bars:
                continue

            # 获取所有分钟时间戳的并集，按时间排序
            all_times = set()
            for code, df in day_minute_bars.items():
                all_times.update(df["date"].astype(str).tolist())
            sorted_times = sorted(all_times)

            if not sorted_times:
                continue

            for t_idx, time_str in enumerate(sorted_times):
                context.current_date = time_str
                is_first_bar_of_day = t_idx == 0
                is_last_bar_of_day = t_idx == len(sorted_times) - 1

                # 构建当前分钟 bar 字典 {code: Series}
                current_bars = {}
                for code in universe:
                    df = day_minute_bars.get(code)
                    if df is not None:
                        row = df[df["date"].astype(str) == time_str]
                        if not row.empty:
                            current_bars[code] = row.iloc[0].copy()

                # 更新前一根 Bar 的收盘价（用于涨跌停判定）
                if t_idx > 0:
                    prev_time = sorted_times[t_idx - 1]
                    for code in current_bars:
                        df = day_minute_bars.get(code)
                        if df is not None:
                            prow = df[df["date"].astype(str) == prev_time]
                            if not prow.empty:
                                current_bars[code]["prev_close"] = prow.iloc[0]["close"]

                # 开盘前（仅每天第一个 Bar）
                if is_first_bar_of_day:
                    portfolio.before_trading(date_str)
                    # 上一日收盘后产生的止损订单在今日第一根 bar 开盘时执行
                    stop_loss_orders = self._generate_stop_loss_orders(portfolio)
                    strategy.before_trading_start(context, current_bars)
                else:
                    stop_loss_orders = []

                # 扫描前期挂单（LIMIT/STOP；PRD_20260520_10）
                # is_last_bar_of_day 由循环顶部已计算，DAY 单在该 bar 后过期
                sweep_fills = self.trade_engine.sweep_pending(
                    portfolio, current_bars, time_str, is_last_bar_of_day
                )

                # ── 执行上一根 bar 产生的挂单（next_open 语义）──
                # pop_orders() 取出的是上一次 handle_data 留存的订单
                orders = context.pop_orders()
                orders.extend(stop_loss_orders)
                for order in orders:
                    if order.side == OrderSide.BUY:
                        price = current_bars.get(order.code, {}).get("open", 0.0)
                        if price > 0:
                            # 预留金额不超过可用现金（策略用 close 估量，open 可能略高）
                            est_amount = min(
                                order.qty * price, portfolio.available_cash
                            )
                            ok = portfolio.reserve_cash(est_amount)
                            if not ok:
                                order.qty = 0
                orders = [o for o in orders if o.qty > 0]

                fills = self.trade_engine.execute_orders(
                    orders, portfolio, current_bars, time_str
                )
                fills = sweep_fills + fills  # 合并挂单成交与新单成交

                # ── 盘中策略处理：基于当前 bar 的 close 产生信号 ──
                # 新订单留存在 context._orders，下一根 bar 开盘时才执行
                strategy.handle_data(context, current_bars)

                # 收盘后（仅每天最后一个 Bar）
                if is_last_bar_of_day:
                    strategy.after_trading_end(context, current_bars)
                    self._check_stop_loss(portfolio, current_bars, date_str)

                # 记录净值
                price_map = {
                    code: bar["close"] for code, bar in current_bars.items()
                }
                nav = portfolio.total_value(price_map)
                self.records.append(
                    DailyRecord(
                        date=time_str,
                        nav=nav,
                        cash=portfolio.available_cash + portfolio.frozen_cash,
                        positions={
                            code: pos.total_qty
                            for code, pos in portfolio.positions.items()
                        },
                        fills=fills,
                    )
                )

    def _preload_bars(
        self,
        codes: List[str],
        trading_days: List[Any],
        lookback_days: int = 0,
    ) -> Dict[str, pd.DataFrame]:
        """预加载回测区间内的全部行情数据。"""
        start_date = trading_days[0]
        if lookback_days and lookback_days > 0:
            calendar_days = max(int(lookback_days * 1.6), lookback_days + 5)
            start_date = start_date - timedelta(days=calendar_days)
        start = start_date.strftime("%Y%m%d")
        end = trading_days[-1].strftime("%Y%m%d")
        logger.info(
            f"预加载行情数据: {start} ~ {end}, period={self.frequency}, "
            f"lookback_days={lookback_days}, adjust={self.bar_adjust}"
        )
        return self.data_source.get_multi_bars(
            codes,
            start,
            end,
            period=self.frequency,
            adjust=self.bar_adjust,
        )

    def _load_comparison_benchmarks(
        self,
        trading_days: List[Any],
    ) -> Dict[str, pd.DataFrame]:
        """加载逐日诊断使用的指数对比行情。"""
        start = trading_days[0].strftime("%Y%m%d")
        end = trading_days[-1].strftime("%Y%m%d")
        result: Dict[str, pd.DataFrame] = {}
        for key, code in self.comparison_benchmarks.items():
            try:
                df = self.data_source.get_bars(
                    code, start, end, period="daily", adjust=None
                )
            except Exception as exc:
                logger.warning(f"诊断基准 {key}({code}) 加载失败: {exc}")
                continue
            prepared = self._prepare_benchmark_df(df)
            if prepared.empty:
                logger.warning(f"诊断基准 {key}({code}) 无可用日线数据")
                continue
            result[key] = prepared
        return result

    @staticmethod
    def _prepare_benchmark_df(df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty or "date" not in df.columns or "close" not in df.columns:
            return pd.DataFrame()
        out = df[["date", "close"]].copy()
        out["date_key"] = out["date"].astype(str).str.replace("-", "", regex=False).str[:8]
        out["close"] = pd.to_numeric(out["close"], errors="coerce").astype(float)
        out = out.dropna(subset=["close"])
        out = out[out["close"] > 0].sort_values("date_key").reset_index(drop=True)
        return out[["date_key", "close"]]

    def _benchmark_stats_for_date(self, key: str, date_str: str) -> Dict[str, float]:
        df = self.comparison_benchmark_dfs.get(key)
        if df is None or df.empty:
            return {}
        upto = df[df["date_key"] <= date_str]
        if upto.empty:
            return {}
        first_close = float(df.iloc[0]["close"])
        current_close = float(upto.iloc[-1]["close"])
        if first_close <= 0:
            return {}
        closes = upto["close"].astype(float)
        peaks = closes.cummax()
        drawdowns = closes / peaks - 1
        return {
            "return_pct": current_close / first_close - 1,
            "max_drawdown_pct": float(drawdowns.min()) if not drawdowns.empty else 0.0,
        }

    def _load_benchmark(self, trading_days: List[Any]) -> pd.DataFrame:
        """加载基准指数行情。

        优先按**日线**获取（基准只用于收益对比与 Beta/Alpha，日线精度足够，
        且分钟级指数表当前未建）。日线表未配置时降级到回测频率作 fallback。
        """
        start = trading_days[0].strftime("%Y%m%d")
        end = trading_days[-1].strftime("%Y%m%d")

        # 优先：日线
        try:
            df = self.data_source.get_bars(
                self.benchmark, start, end, period="daily", adjust=None
            )
            if not df.empty:
                return df
            logger.info("基准日线返回空，尝试降级到回测频率")
        except NotImplementedError:
            logger.info(
                f"基准日线表未配置，降级到 period={self.frequency} 加载基准"
            )
        except Exception as e:
            logger.warning(f"基准指数日线加载失败: {e}")

        # 降级：回测频率
        try:
            df = self.data_source.get_bars(
                self.benchmark, start, end, period=self.frequency, adjust=None
            )
            if df.empty:
                logger.warning(
                    f"基准 {self.benchmark} 在 daily 与 {self.frequency} 数据源中均无数据。"
                    f"benchmark_return / Beta / Alpha 等指标将为 0。"
                    f"建议：将 preset/config 中 benchmark 改为有数据的代码（如 510300.SH）。"
                )
            return df
        except Exception as e:
            logger.warning(
                f"基准 {self.benchmark} {self.frequency} 加载也失败: {e}。"
                f"benchmark_return / Beta / Alpha 等指标将为 0。"
            )
            return pd.DataFrame()

    def _build_result_df(self) -> pd.DataFrame:
        """将回测记录整理为 DataFrame。"""
        if not self.records:
            return pd.DataFrame()
        df = pd.DataFrame(
            [
                {
                    "date": r.date,
                    "nav": r.nav,
                    "cash": r.cash,
                }
                for r in self.records
            ]
        )
        df["returns"] = df["nav"].pct_change().fillna(0)
        df["cumulative_returns"] = (1 + df["returns"]).cumprod() - 1
        # 按字符串长度显式路由日期解析，避免 format + errors='coerce' 的未定义行为
        raw_dates = df["date"].astype(str)
        lengths = raw_dates.str.len()

        # 初始化结果列为 NaT
        df["date"] = pd.NaT

        # 8 位：日线 YYYYMMDD
        mask8 = lengths == 8
        if mask8.any():
            df.loc[mask8, "date"] = pd.to_datetime(
                raw_dates[mask8], format="%Y%m%d", errors="coerce"
            )

        # 12 位：分钟线 YYYYMMDDHHMM
        mask12 = lengths == 12
        if mask12.any():
            df.loc[mask12, "date"] = pd.to_datetime(
                raw_dates[mask12], format="%Y%m%d%H%M", errors="coerce"
            )

        # 异常长度：报错
        invalid = ~(mask8 | mask12)
        if invalid.any():
            bad = raw_dates[invalid].iloc[0]
            raise ValueError(f"无法识别的日期格式（长度既非8也非12）: {bad}")

        df.set_index("date", inplace=True)
        return df

    # ---------- 止损机制 ----------

    def _check_stop_loss(
        self,
        portfolio: Portfolio,
        bar_data: Dict[str, pd.Series],
        date: str,
    ) -> None:
        """收盘后检查持仓浮亏，触发止损条件的记入待执行队列。

        检查规则：
        - 仅对 sellable_qty > 0 的持仓检查（T+1 当日买入不止损）
        - 以当前收盘价计算 profit_ratio，若 < -threshold 则触发
        - 触发后记录到 _stop_loss_pending，下一交易日开盘前生成订单
        """
        if not self.stop_loss_enabled:
            return

        for code, pos in portfolio.positions.items():
            if pos.sellable_qty <= 0:
                continue
            bar = bar_data.get(code)
            if bar is None:
                continue
            close_price = bar.get("close", 0.0)
            if close_price <= 0:
                continue
            profit_ratio = pos.profit_ratio(close_price)
            if profit_ratio < -self.stop_loss_threshold:
                self._stop_loss_pending[code] = pos.sellable_qty
                logger.info(
                    f"{date} 止损触发: {code} "
                    f"成本价={pos.cost_price:.2f} 收盘价={close_price:.2f} "
                    f"浮亏={profit_ratio:.2%} 阈值={self.stop_loss_threshold:.2%} "
                    f"计划卖出={pos.sellable_qty}股"
                )

    def _generate_stop_loss_orders(self, portfolio: Portfolio) -> List[Order]:
        """根据待执行止损队列生成市价卖出订单，并清空队列。

        在下一交易日开盘前调用，将前一日的止损计划转化为实际订单。
        """
        if not self.stop_loss_enabled or not self._stop_loss_pending:
            return []

        orders: List[Order] = []
        for code, qty in list(self._stop_loss_pending.items()):
            pos = portfolio.get_position(code)
            if pos is not None and pos.sellable_qty > 0:
                sell_qty = min(qty, pos.sellable_qty)
                orders.append(
                    Order(
                        code=code,
                        side=OrderSide.SELL,
                        qty=sell_qty,
                        order_type=OrderType.MARKET,
                    )
                )
                logger.info(
                    f"开盘前生成止损单: {code} 卖出 {sell_qty}股"
                )
            del self._stop_loss_pending[code]
        return orders
