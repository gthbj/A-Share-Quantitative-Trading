"""虚拟盘（Paper Trading）：每日收盘后运行，模拟策略最新调仓。

状态持久化到本地 JSON，支持断点续跑。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Type

from account.portfolio import Portfolio
from data_layer.base_data_source import BaseDataSource
from engine.backtest import BacktestEngine
from engine.trade_engine import TradeEngine
from strategy.base_strategy import BaseStrategy
from utils.calendar import TradingCalendar
from utils.logger import get_logger

logger = get_logger(__name__)


class PaperTrader:
    """虚拟盘。

    与回测引擎复用同一套 TradeEngine，但状态持久化到本地，
    支持每日增量运行。
    """

    STATE_FILE = "data/paper_state.json"

    def __init__(
        self,
        strategy_cls: Type[BaseStrategy],
        data_source: BaseDataSource,
        trade_engine: Optional[TradeEngine] = None,
        state_file: Optional[str] = None,
    ) -> None:
        self.strategy_cls = strategy_cls
        self.data_source = data_source
        self.trade_engine = trade_engine or TradeEngine()
        self.state_file = Path(state_file or self.STATE_FILE)
        self.state_file.parent.mkdir(parents=True, exist_ok=True)

        self.portfolio: Optional[Portfolio] = None
        self.current_date: Optional[str] = None

    def load_state(self) -> None:
        """从本地加载虚拟盘状态。"""
        if not self.state_file.exists():
            logger.info("未找到历史状态，初始化新账户")
            self.portfolio = Portfolio()
            return

        with open(self.state_file, "r", encoding="utf-8") as f:
            state = json.load(f)

        self.portfolio = Portfolio(initial_capital=state.get("initial_capital", 1_000_000))
        self.portfolio.available_cash = state.get("available_cash", self.portfolio.initial_capital)
        self.current_date = state.get("current_date")

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

        logger.info(f"虚拟盘状态加载成功: 日期={self.current_date}, 现金={self.portfolio.available_cash}")

    def save_state(self) -> None:
        """保存虚拟盘状态到本地。"""
        if self.portfolio is None:
            return
        state = {
            "initial_capital": self.portfolio.initial_capital,
            "available_cash": self.portfolio.available_cash,
            "current_date": self.current_date,
            "positions": {},
        }
        for code, pos in self.portfolio.positions.items():
            state["positions"][code] = {
                "total_qty": pos.total_qty,
                "sellable_qty": pos.sellable_qty,
                "cost_price": pos.cost_price,
                "buy_records": pos._buy_records,
            }
        with open(self.state_file, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False, indent=2)
        logger.info(f"虚拟盘状态已保存: {self.state_file}")

    def run_once(self, date: Optional[str] = None) -> None:
        """运行单日虚拟盘。

        Args:
            date: 指定日期，默认取最近一个交易日。
        """
        if self.portfolio is None:
            self.load_state()

        calendar = TradingCalendar()
        if date is None:
            # 取今天或最近交易日
            from datetime import date as dt_date
            today = dt_date.today()
            if calendar.is_trading_day(today):
                date = today.strftime("%Y%m%d")
            else:
                date = calendar.prev_trading_day(today).strftime("%Y%m%d")

        self.current_date = date
        self.portfolio.before_trading(date)

        # 构造当日 bar 数据（虚拟盘通常只关注持仓股的收盘价）
        codes = list(self.portfolio.positions.keys())
        bars = {}
        for code in codes:
            try:
                df = self.data_source.get_bars(code, date, date)
                if not df.empty:
                    bars[code] = df.iloc[0]
            except Exception:
                continue

        # 目前仅做持仓估值与状态更新，复杂信号可后续扩展
        logger.info(
            f"虚拟盘 {date}: 持仓={len(self.portfolio.positions)} 只, "
            f"总资产≈{self.portfolio.total_value({c: b['close'] for c, b in bars.items()}):,.2f}"
        )

        self.save_state()
