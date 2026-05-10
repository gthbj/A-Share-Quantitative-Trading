"""回测主引擎：驱动策略运行、调度交易日/Bar、记录净值。

支持日线（daily）与分钟级（1min/5min/15min/30min/60min）双频回测。
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import pandas as pd

from account.portfolio import Portfolio
from data_layer.base_data_source import BaseDataSource
from engine.trade_engine import Fill, Order, OrderSide, TradeEngine
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
    ) -> None:
        self.strategy_cls = strategy_cls
        self.data_source = data_source
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.benchmark = benchmark
        self.trade_engine = trade_engine or TradeEngine()
        self.frequency = frequency

        self.calendar = TradingCalendar()
        self.records: List[DailyRecord] = []
        self.benchmark_df: Optional[pd.DataFrame] = None

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
        )
        strategy = self.strategy_cls()
        strategy.initialize(context)

        # 获取Universe列表
        universe = strategy.get_universe()
        logger.info(
            f"回测启动: universe={len(universe)} 只股票, "
            f"区间={self.start_date}~{self.end_date}, 频率={self.frequency}"
        )

        # 预加载所有行情数据
        all_bars = self._preload_bars(universe, trading_days)
        self.benchmark_df = self._load_benchmark(trading_days)

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
        """日线回测主循环（与修改前逻辑完全一致）。"""
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

            # 3. 开盘前
            portfolio.before_trading(date_str)
            strategy.before_trading_start(context, today_bars)

            # 4. 盘中处理
            strategy.handle_data(context, today_bars)

            # 5. 获取订单并撮合
            orders = context.pop_orders()
            for order in orders:
                if order.side == OrderSide.BUY:
                    price = today_bars.get(order.code, {}).get("open", 0.0)
                    if price > 0:
                        est_amount = order.qty * price
                        ok = portfolio.reserve_cash(est_amount)
                        if not ok:
                            order.qty = 0
            orders = [o for o in orders if o.qty > 0]

            fills = self.trade_engine.execute_orders(
                orders, portfolio, today_bars, date_str
            )

            # 6. 收盘后
            strategy.after_trading_end(context, today_bars)

            # 7. 记录净值
            price_map = {code: bar["close"] for code, bar in today_bars.items()}
            nav = portfolio.total_value(price_map)
            self.records.append(
                DailyRecord(
                    date=date_str,
                    nav=nav,
                    cash=portfolio.available_cash + portfolio.frozen_cash,
                    positions={
                        code: pos.total_qty for code, pos in portfolio.positions.items()
                    },
                    fills=fills,
                )
            )

    def _run_intraday(
        self,
        trading_days: List[Any],
        strategy: BaseStrategy,
        context: Context,
        portfolio: Portfolio,
        all_bars: Dict[str, pd.DataFrame],
    ) -> None:
        """分钟级回测主循环：逐交易日加载分钟 Bar，逐条推进。"""
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
                    strategy.before_trading_start(context, current_bars)

                # 盘中处理
                strategy.handle_data(context, current_bars)

                # 获取订单并撮合
                orders = context.pop_orders()
                for order in orders:
                    if order.side == OrderSide.BUY:
                        price = current_bars.get(order.code, {}).get("open", 0.0)
                        if price > 0:
                            est_amount = order.qty * price
                            ok = portfolio.reserve_cash(est_amount)
                            if not ok:
                                order.qty = 0
                orders = [o for o in orders if o.qty > 0]

                fills = self.trade_engine.execute_orders(
                    orders, portfolio, current_bars, time_str
                )

                # 收盘后（仅每天最后一个 Bar）
                if is_last_bar_of_day:
                    strategy.after_trading_end(context, current_bars)

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
        self, codes: List[str], trading_days: List[Any]
    ) -> Dict[str, pd.DataFrame]:
        """预加载回测区间内的全部行情数据。"""
        start = trading_days[0].strftime("%Y%m%d")
        end = trading_days[-1].strftime("%Y%m%d")
        logger.info(f"预加载行情数据: {start} ~ {end}, period={self.frequency}")
        return self.data_source.get_multi_bars(codes, start, end, period=self.frequency)

    def _load_benchmark(self, trading_days: List[Any]) -> pd.DataFrame:
        """加载基准指数行情。"""
        start = trading_days[0].strftime("%Y%m%d")
        end = trading_days[-1].strftime("%Y%m%d")
        try:
            df = self.data_source.get_bars(
                self.benchmark, start, end, period=self.frequency, adjust=None
            )
            return df
        except Exception:
            logger.warning("基准指数数据加载失败")
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
        # 分钟级时间戳可能为 YYYYMMDDHHMM，根据长度区分解析
        raw_dates = df["date"].astype(str)
        df["date"] = pd.to_datetime(raw_dates, format="%Y%m%d%H%M", errors="coerce")
        # 对解析失败的（日线 YYYYMMDD 或长度不足），fallback 到 YYYYMMDD 格式
        mask = df["date"].isna()
        if mask.any():
            df.loc[mask, "date"] = pd.to_datetime(
                raw_dates[mask].str.slice(0, 8), format="%Y%m%d", errors="coerce"
            )
        df.set_index("date", inplace=True)
        return df
