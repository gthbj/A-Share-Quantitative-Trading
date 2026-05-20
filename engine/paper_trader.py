"""虚拟盘（Paper Trading）：每日收盘后运行，模拟策略最新调仓。

状态持久化到本地 JSON，支持断点续跑（含策略类、构造参数、user_data、待执行订单）。

撮合时序（PRD_20260520_09）：与回测引擎 ``_run_daily`` 对齐的 next_open 语义。
``run_once(date=T)`` 处理流程：

1. load_state() — 含上次跑剩下的 pending_orders / pending_stop_loss / user_data
2. 实例化策略（用 state 里的 strategy_class + strategy_kwargs）
3. 预加载 ``[T - lookback_days, T]`` 历史行情，注入 ``context.all_bars``
4. ``portfolio.before_trading(T)`` — 解冻 T-1 买入的股票
5. 用 T 日开盘价撮合 pending_orders + pending_stop_loss
6. ``strategy.before_trading_start(T)``
7. ``strategy.handle_data(T)`` — 新订单进 ``context._orders``
8. ``strategy.after_trading_end(T)``
9. 止损检查（基于 T 日收盘价 → 加入 pending_stop_loss，下次 run_once 开盘执行）
10. save_state() — 持久化新的待执行订单 + 止损队列 + user_data
"""

from __future__ import annotations

import importlib
import json
from datetime import date as dt_date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Type

import pandas as pd

from account.portfolio import Portfolio
from data_layer.base_data_source import BaseDataSource
from engine.trade_engine import (
    Fill,
    Order,
    OrderSide,
    OrderType,
    TradeEngine,
)
from strategy.base_strategy import BaseStrategy, Context
from utils.calendar import TradingCalendar
from utils.logger import get_logger

logger = get_logger(__name__)


class PaperTrader:
    """虚拟盘。

    与回测引擎复用同一套 TradeEngine 与策略生命周期，状态持久化到本地 JSON，
    支持每日增量运行与断点续跑。
    """

    STATE_FILE = "data/paper_state.json"

    def __init__(
        self,
        data_source: BaseDataSource,
        strategy_cls: Optional[Type[BaseStrategy]] = None,
        strategy_kwargs: Optional[Dict[str, Any]] = None,
        trade_engine: Optional[TradeEngine] = None,
        state_file: Optional[str] = None,
        initial_capital: float = 1_000_000.0,
        stop_loss_enabled: bool = False,
        stop_loss_threshold: float = 0.05,
        frequency: str = "daily",
    ) -> None:
        """
        Args:
            data_source: 行情数据源。
            strategy_cls: 策略类。首次启动（无 state.json）时必填；
                          后续从 state 恢复时，传入则覆盖 state 里的，不传则沿用。
            strategy_kwargs: 策略构造参数。语义同 strategy_cls。
            trade_engine: 撮合引擎，默认 TradeEngine()。
            state_file: 状态文件路径，默认 data/paper_state.json。
            initial_capital: 首次启动时的初始资金。
            stop_loss_enabled / stop_loss_threshold: 止损配置。
            frequency: 行情频率，默认 ``daily``。若日线表不可用，会自动降级到
                       该频率拉数据；用户数据源若仅有分钟线，可显式传 ``5min`` 等。
        """
        self.data_source = data_source
        self.strategy_cls = strategy_cls
        self.strategy_kwargs = strategy_kwargs
        self.trade_engine = trade_engine or TradeEngine()
        self.state_file = Path(state_file or self.STATE_FILE)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.initial_capital = initial_capital
        self.stop_loss_enabled = stop_loss_enabled
        self.stop_loss_threshold = stop_loss_threshold
        self.frequency = frequency

        # 运行时状态
        self.portfolio: Optional[Portfolio] = None
        self.current_date: Optional[str] = None
        self.last_run_date: Optional[str] = None
        self.user_data: Dict[str, Any] = {}
        self.pending_orders: List[Order] = []
        self.pending_stop_loss: Dict[str, int] = {}

    # ---------- 状态序列化 ----------

    def load_state(self) -> None:
        """从本地加载虚拟盘状态。"""
        if not self.state_file.exists():
            if self.strategy_cls is None:
                raise ValueError(
                    "首次启动 PaperTrader（无 state.json）必须传入 strategy_cls。"
                )
            logger.info("未找到历史状态，初始化新账户")
            self.portfolio = Portfolio(initial_capital=self.initial_capital)
            self.user_data = {}
            self.pending_orders = []
            self.pending_stop_loss = {}
            return

        with open(self.state_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        # Portfolio 复原
        self.portfolio = Portfolio(
            initial_capital=state.get("initial_capital", self.initial_capital)
        )
        self.portfolio.available_cash = state.get(
            "available_cash", self.portfolio.initial_capital
        )
        self.current_date = state.get("current_date")
        self.last_run_date = state.get("last_run_date")

        # Positions 复原
        for code, pos_state in state.get("positions", {}).items():
            from account.position import Position
            pos = Position(
                code=code,
                total_qty=pos_state["total_qty"],
                sellable_qty=pos_state["sellable_qty"],
                cost_price=pos_state["cost_price"],
            )
            pos._buy_records = pos_state.get("buy_records", {})
            self.portfolio.positions[code] = pos

        # 策略元信息复原（外部传入优先）
        if self.strategy_cls is None:
            class_path = state.get("strategy_class")
            if class_path:
                self.strategy_cls = _import_class(class_path)
        if self.strategy_kwargs is None:
            self.strategy_kwargs = state.get("strategy_kwargs") or {}

        # 跨日状态
        self.user_data = state.get("user_data", {}) or {}
        self.pending_orders = [
            Order.from_dict(d) for d in state.get("pending_orders", []) or []
        ]
        self.pending_stop_loss = dict(state.get("pending_stop_loss", {}) or {})

        logger.info(
            f"虚拟盘状态加载成功: 日期={self.current_date}, "
            f"现金={self.portfolio.available_cash}, "
            f"待执行订单={len(self.pending_orders)}笔, "
            f"待止损={len(self.pending_stop_loss)}只"
        )

    def save_state(self) -> None:
        """保存虚拟盘状态到本地。

        策略元信息（class + kwargs）也一并保存，下次启动时无需再传。
        """
        if self.portfolio is None:
            return

        strategy_class_path: Optional[str] = None
        if self.strategy_cls is not None:
            strategy_class_path = (
                f"{self.strategy_cls.__module__}.{self.strategy_cls.__name__}"
            )

        state = {
            "initial_capital": self.portfolio.initial_capital,
            "available_cash": self.portfolio.available_cash,
            "current_date": self.current_date,
            "last_run_date": self.last_run_date,
            "positions": {},
            "strategy_class": strategy_class_path,
            "strategy_kwargs": self.strategy_kwargs or {},
            "user_data": self.user_data,
            "pending_orders": [o.to_dict() for o in self.pending_orders],
            "pending_stop_loss": dict(self.pending_stop_loss),
        }
        for code, pos in self.portfolio.positions.items():
            state["positions"][code] = {
                "total_qty": pos.total_qty,
                "sellable_qty": pos.sellable_qty,
                "cost_price": pos.cost_price,
                "buy_records": pos._buy_records,
            }
        # JSON 兼容性预检：user_data 若含不可序列化对象，这里会抛 TypeError
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        logger.info(f"虚拟盘状态已保存: {self.state_file}")

    # ---------- 主循环 ----------

    def run_once(self, date: Optional[str] = None) -> None:
        """运行单日虚拟盘。

        Args:
            date: 指定日期（YYYYMMDD），默认取今天或最近一个交易日。
        """
        # 1. 加载状态
        if self.portfolio is None:
            self.load_state()

        # 2. 解析日期
        calendar = TradingCalendar()
        if date is None:
            today = dt_date.today()
            if calendar.is_trading_day(today):
                date = today.strftime("%Y%m%d")
            else:
                date = calendar.prev_trading_day(today).strftime("%Y%m%d")

        # 3. 重复运行拦截
        if self.last_run_date and self.last_run_date >= date:
            logger.warning(
                f"虚拟盘已跑过 {self.last_run_date}，本次请求 {date} 跳过"
            )
            return

        self.current_date = date
        logger.info(f"虚拟盘开始: date={date}")

        # 4. 实例化策略
        if self.strategy_cls is None:
            raise ValueError("strategy_cls 未指定且 state 中也无记录，无法实例化策略")
        strategy = self.strategy_cls(**(self.strategy_kwargs or {}))
        universe = self._init_strategy_get_universe(strategy)

        # 5. 预加载历史 + 当日行情
        all_bars = self._preload_history(universe, date, strategy.lookback_days)

        # 6. 构造 Context
        context = Context(
            portfolio=self.portfolio,
            data_source=self.data_source,
            current_date=date,
            frequency="daily",
            all_bars=all_bars,
            user_data=self.user_data,
        )
        strategy.context = context

        # 7. 当日 bar 字典（撮合用）
        today_bars = self._today_bars(universe, all_bars, date)
        if not today_bars:
            logger.warning(f"虚拟盘 {date}: 无任何当日行情，跳过")
            self.last_run_date = date
            self.save_state()
            return

        # 8. 注入 prev_close（涨跌停判定）
        self._inject_prev_close(today_bars, all_bars, date)

        # 9. before_trading（T+1 解冻）
        self.portfolio.before_trading(date)

        # 10. 执行上次留存的订单 + 止损单（next_open 语义）
        orders_to_execute = list(self.pending_orders)
        orders_to_execute.extend(self._build_stop_loss_orders())
        for order in orders_to_execute:
            if order.side == OrderSide.BUY:
                price = today_bars.get(order.code, {}).get("open", 0.0)
                if price > 0:
                    est_amount = min(order.qty * price, self.portfolio.available_cash)
                    if not self.portfolio.reserve_cash(est_amount):
                        order.qty = 0
        orders_to_execute = [o for o in orders_to_execute if o.qty > 0]

        fills = self.trade_engine.execute_orders(
            orders_to_execute, self.portfolio, today_bars, date
        )
        for fill in fills:
            logger.info(
                f"{date} 成交: {fill.code} {fill.side.value} "
                f"qty={fill.qty} price={fill.price:.2f} fee={fill.total_cost:.2f}"
            )

        # 11. 策略生命周期
        strategy.before_trading_start(context, today_bars)
        strategy.handle_data(context, today_bars)
        strategy.after_trading_end(context, today_bars)

        # 12. 止损检查（基于 T 日收盘价 → 下次开盘执行）
        new_stop_loss = self._check_stop_loss(today_bars, date)

        # 13. 收集新产生的订单 + 止损 → 持久化
        self.pending_orders = context.pop_orders()
        self.pending_stop_loss = new_stop_loss
        self.user_data = context.user_data
        self.last_run_date = date

        # 14. 估值日志
        price_map = {c: b["close"] for c, b in today_bars.items()}
        nav = self.portfolio.total_value(price_map)
        logger.info(
            f"虚拟盘 {date} 结束: 持仓={len(self.portfolio.positions)}只, "
            f"总资产≈{nav:,.2f}, 待执行订单={len(self.pending_orders)}笔, "
            f"待止损={len(self.pending_stop_loss)}只"
        )

        # 15. 保存
        self.save_state()

    # ---------- 内部辅助 ----------

    def _init_strategy_get_universe(self, strategy: BaseStrategy) -> List[str]:
        """调用 strategy.initialize 获取 universe（不依赖 Context.portfolio 的瞬时状态）。"""
        # 临时构造一个最小 Context 让 initialize 跑起来；之后 run_once 会用真正的 Context
        tmp_ctx = Context(
            portfolio=self.portfolio,
            data_source=self.data_source,
            current_date=self.current_date or "",
            frequency="daily",
        )
        strategy.initialize(tmp_ctx)
        return strategy.get_universe()

    def _preload_history(
        self, universe: List[str], date: str, lookback_days: int
    ) -> Dict[str, pd.DataFrame]:
        """预加载 [date - lookback_days, date] 区间内 universe 行情。

        lookback_days 是策略需要的"交易日"数；这里按自然日 × 1.6 估算
        （A 股每周约 5 个交易日 / 7 个自然日 ≈ 0.71，反推 1.4；含节假日补 1.6）。

        频率：优先用 ``self.frequency``（默认 daily）。若日线表未配置抛
        NotImplementedError，降级到该频率重试一次（与 BacktestEngine._load_benchmark
        相同的兜底策略）。
        """
        end_dt = dt_date(int(date[:4]), int(date[4:6]), int(date[6:8]))
        start_dt = end_dt - timedelta(days=max(int(lookback_days * 1.6), 5))
        start = start_dt.strftime("%Y%m%d")
        logger.info(
            f"预加载历史行情: {start} ~ {date}, "
            f"lookback_days={lookback_days}, frequency={self.frequency}"
        )
        try:
            return self.data_source.get_multi_bars(
                universe, start, date, period=self.frequency
            )
        except NotImplementedError as e:
            if self.frequency == "daily":
                logger.warning(
                    f"日线表未配置：{e}。请通过 frequency 参数指定可用频率（如 '15min'）。"
                )
            raise

    def _today_bars(
        self,
        universe: List[str],
        all_bars: Dict[str, pd.DataFrame],
        date: str,
    ) -> Dict[str, pd.Series]:
        """从预加载的全量行情中切出当日 bar。

        - daily 频率：``date`` 长度为 8（YYYYMMDD），精确匹配
        - 分钟级（frequency != "daily"）：``df["date"]`` 是 ``YYYYMMDDHHMM``，
          取所有以 ``date`` 开头的 bar 中**第一根（open）和最后一根（close）合并**
          作为当日 OHLCV——open 取第一根、high/low 取全天极值、close 取最后一根
        """
        today_bars: Dict[str, pd.Series] = {}
        for code in universe:
            df = all_bars.get(code)
            if df is None or df.empty:
                continue
            date_col = df["date"].astype(str)
            if self.frequency == "daily":
                row = df[date_col == date]
                if not row.empty:
                    today_bars[code] = row.iloc[0].copy()
            else:
                day_df = df[date_col.str.startswith(date)].sort_values("date")
                if not day_df.empty:
                    first = day_df.iloc[0]
                    last = day_df.iloc[-1]
                    today_bars[code] = pd.Series({
                        "open": first["open"],
                        "high": day_df["high"].max(),
                        "low": day_df["low"].min(),
                        "close": last["close"],
                        "volume": day_df["volume"].sum(),
                    })
        return today_bars

    def _inject_prev_close(
        self,
        today_bars: Dict[str, pd.Series],
        all_bars: Dict[str, pd.DataFrame],
        date: str,
    ) -> None:
        """给 today_bars 注入 prev_close 字段（用于涨跌停判定）。

        分钟级数据：取 date 之前最后一根 bar 的 close（即昨日最后一根分钟收盘）。
        """
        for code, bar in today_bars.items():
            df = all_bars.get(code)
            if df is None:
                continue
            date_col = df["date"].astype(str)
            # 取 YYYYMMDD 前缀严格小于 date 的 bar 中最后一根
            prev = df[date_col.str[:8] < date].tail(1)
            if not prev.empty:
                bar["prev_close"] = prev.iloc[0]["close"]

    def _build_stop_loss_orders(self) -> List[Order]:
        """把 pending_stop_loss 队列转成 MARKET 卖单，并清空队列。"""
        if not self.pending_stop_loss:
            return []
        orders: List[Order] = []
        for code, qty in list(self.pending_stop_loss.items()):
            pos = self.portfolio.get_position(code) if self.portfolio else None
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
                logger.info(f"开盘前生成止损单: {code} 卖出 {sell_qty}股")
        self.pending_stop_loss = {}
        return orders

    def _check_stop_loss(
        self, today_bars: Dict[str, pd.Series], date: str
    ) -> Dict[str, int]:
        """收盘后检查持仓浮亏，返回新一批待执行止损队列。"""
        if not self.stop_loss_enabled or self.portfolio is None:
            return {}
        new_queue: Dict[str, int] = {}
        for code, pos in self.portfolio.positions.items():
            if pos.sellable_qty <= 0:
                continue
            bar = today_bars.get(code)
            if bar is None:
                continue
            close_price = bar.get("close", 0.0)
            if close_price <= 0:
                continue
            profit_ratio = pos.profit_ratio(close_price)
            if profit_ratio < -self.stop_loss_threshold:
                new_queue[code] = pos.sellable_qty
                logger.info(
                    f"{date} 止损触发: {code} 浮亏={profit_ratio:.2%} "
                    f"阈值={self.stop_loss_threshold:.2%} 计划卖出={pos.sellable_qty}股"
                )
        return new_queue


def _import_class(class_path: str) -> Type[BaseStrategy]:
    """通过模块路径字符串反射导入策略类。

    例：``strategy.double_ma.strategy.DoubleMAStrategy``。
    """
    module_path, _, class_name = class_path.rpartition(".")
    if not module_path:
        raise ValueError(f"非法的 class_path: {class_path}")
    module = importlib.import_module(module_path)
    return getattr(module, class_name)
