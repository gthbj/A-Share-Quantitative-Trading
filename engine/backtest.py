"""回测主引擎：驱动策略运行、调度交易日、记录净值。"""

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
    """单日回测记录。"""

    date: str
    nav: float  # 净值
    cash: float
    positions: Dict[str, int]  # code -> qty
    fills: List[Fill] = field(default_factory=list)


class BacktestEngine:
    """回测引擎。

    按交易日历逐日推进，调用策略生命周期方法，撮合订单并记录净值曲线。
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
    ) -> None:
        self.strategy_cls = strategy_cls
        self.data_source = data_source
        self.start_date = start_date
        self.end_date = end_date
        self.initial_capital = initial_capital
        self.benchmark = benchmark
        self.trade_engine = trade_engine or TradeEngine()

        self.calendar = TradingCalendar()
        self.records: List[DailyRecord] = []
        self.benchmark_df: Optional[pd.DataFrame] = None

    def run(self) -> pd.DataFrame:
        """运行回测，返回每日净值 DataFrame。"""
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
        logger.info(f"回测启动: universe={len(universe)} 只股票, 区间={self.start_date}~{self.end_date}")

        # 预加载所有行情数据（简化版：全量加载；大数据量时可改为逐日懒加载）
        all_bars = self._preload_bars(universe, trading_days)
        self.benchmark_df = self._load_benchmark(trading_days)

        # 主循环
        for i, date_obj in enumerate(trading_days):
            date_str = date_obj.strftime("%Y%m%d")
            context.current_date = date_str

            # 1. 构建当日 bar 字典 {code: Series}
            today_bars = {}
            for code in universe:
                df = all_bars.get(code)
                if df is not None and not df.empty:
                    row = df[df["date"] == date_str]
                    if not row.empty:
                        today_bars[code] = row.iloc[0]

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

            # 4. 盘中处理（日线回测：每日一次）
            strategy.handle_data(context, today_bars)

            # 5. 获取订单并撮合
            orders = context.pop_orders()
            # 买入订单先冻结资金
            for order in orders:
                if order.side == OrderSide.BUY:
                    # 粗略估计所需资金（以昨日收盘价或当前open）
                    price = today_bars.get(order.code, {}).get("open", 0.0)
                    if price > 0:
                        est_amount = order.qty * price
                        ok = portfolio.reserve_cash(est_amount)
                        if not ok:
                            order.qty = 0  # 资金不足，标记为无效
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

        logger.info("回测结束")
        return self._build_result_df()

    def _preload_bars(
        self, codes: List[str], trading_days: List[Any]
    ) -> Dict[str, pd.DataFrame]:
        """预加载回测区间内的全部日K数据。"""
        start = trading_days[0].strftime("%Y%m%d")
        end = trading_days[-1].strftime("%Y%m%d")
        logger.info(f"预加载行情数据: {start} ~ {end}")
        return self.data_source.get_multi_daily_bars(codes, start, end)

    def _load_benchmark(self, trading_days: List[Any]) -> pd.DataFrame:
        """加载基准指数行情。"""
        start = trading_days[0].strftime("%Y%m%d")
        end = trading_days[-1].strftime("%Y%m%d")
        try:
            df = self.data_source.get_daily_bars(
                self.benchmark, start, end, adjust=None
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
        df["date"] = pd.to_datetime(df["date"])
        df.set_index("date", inplace=True)
        return df
