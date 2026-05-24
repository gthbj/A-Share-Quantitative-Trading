"""BigQuery ML ADS signal strategy with real execution constraints.

The strategy reads precomputed candidates from
``ashare.ads_signal_ml_stock_picker_bqml_1d`` and lets the existing
BacktestEngine/TradeEngine handle next-open execution, slippage, fees,
volume limits, price limits, and T+1 sellability.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List, Optional

import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class SignalCandidate:
    code: str
    score_rank: int
    prob_up: float


class BQMLSignalPickerStrategy(BaseStrategy):
    """BQML 候选信号真实撮合策略。

    策略本身只负责根据 ADS 信号产生买卖订单；真实成交约束由回测引擎处理。
    """

    DEFAULT_UNIVERSE: List[str] = []
    DYNAMIC_UNIVERSE = True

    def __init__(
        self,
        signal_table: str = "ads_signal_ml_stock_picker_bqml_1d",
        signal_start_date: str = "",
        signal_end_date: str = "",
        candidate_pool_size: int = 10,
        max_positions: int = 3,
        min_positions: int = 1,
        holding_period: int = 5,
        position_pct: float = 0.95,
        min_cash_reserve: float = 1000.0,
        universe: Optional[List[str]] = None,
    ) -> None:
        super().__init__()
        self.signal_table = signal_table
        self.signal_start_date = signal_start_date
        self.signal_end_date = signal_end_date
        self.candidate_pool_size = max(int(candidate_pool_size), 1)
        self.max_positions = max(int(max_positions), 1)
        self.min_positions = max(int(min_positions), 1)
        if self.min_positions > self.max_positions:
            raise ValueError("min_positions must be <= max_positions")
        self.holding_period = max(int(holding_period), 1)
        self.position_pct = min(max(float(position_pct), 0.0), 1.0)
        self.min_cash_reserve = max(float(min_cash_reserve), 0.0)
        self._configured_universe = list(universe) if universe else []
        self.lookback_days = 1

        self._signals_by_date: Dict[str, List[SignalCandidate]] = {}
        self._position_ages: Dict[str, int] = {}
        self._pending_entry_dates: Dict[str, str] = {}

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        signals = self._load_signals(context)
        if signals.empty:
            if not self._configured_universe:
                raise RuntimeError("BQML signal table returned no candidates and no universe fallback is configured")
            self.set_universe(self._configured_universe)
            logger.warning("BQML 信号为空，使用配置 universe 作为 fallback: %s", self._configured_universe)
            return

        self._signals_by_date = self._build_signal_map(signals)
        signal_universe = sorted(signals["equity_code"].astype(str).unique().tolist())
        if self._configured_universe:
            configured = set(self._configured_universe)
            signal_universe = [code for code in signal_universe if code in configured]
        self.set_universe(signal_universe)
        logger.info(
            "BQMLSignalPicker 初始化完成: signal_days=%s, universe=%s, max_positions=%s, holding_period=%s",
            len(self._signals_by_date),
            len(signal_universe),
            self.max_positions,
            self.holding_period,
        )

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        current_date = context.current_date[:8]
        self._sync_position_state(context)
        self._submit_due_sells(context, current_date)

        open_slots = self.max_positions - self._active_position_count(context)
        if open_slots <= 0:
            return

        candidates = self._signals_by_date.get(current_date, [])
        if not candidates:
            return

        portfolio = context.portfolio
        price_map = {code: float(bar["close"]) for code, bar in data.items() if float(bar.get("close", 0.0)) > 0}
        total_value = portfolio.total_value(price_map)
        target_positions = min(self.max_positions, max(self.min_positions, len(portfolio.positions) + open_slots))
        target_value_per_stock = total_value * self.position_pct / max(target_positions, 1)
        available_budget = max(portfolio.available_cash - self.min_cash_reserve, 0.0)

        bought = 0
        for candidate in candidates:
            if bought >= open_slots or available_budget <= 0:
                break
            code = candidate.code
            if context.portfolio.has_position(code) or code in self._pending_entry_dates:
                continue
            bar = data.get(code)
            if bar is None:
                continue
            price = float(bar.get("close", 0.0))
            if price <= 0:
                continue
            order_budget = min(target_value_per_stock, available_budget)
            qty = int((order_budget / price) // 100) * 100
            if qty <= 0:
                continue
            context.order(code, qty)
            self._pending_entry_dates[code] = current_date
            available_budget -= qty * price
            bought += 1
            logger.info(
                "%s BQML 买入候选 %s qty=%s rank=%s prob=%.6f",
                current_date,
                code,
                qty,
                candidate.score_rank,
                candidate.prob_up,
            )

    def _load_signals(self, context: Context) -> pd.DataFrame:
        loader = getattr(context.data_source, "get_bqml_signal_candidates", None)
        if loader is None:
            raise RuntimeError("data_source does not support get_bqml_signal_candidates")
        signals = loader(
            start_date=self.signal_start_date,
            end_date=self.signal_end_date,
            table_name=self.signal_table,
            candidate_pool_size=self.candidate_pool_size,
        )
        if signals.empty:
            return signals
        signals = signals.copy()
        signals["date"] = signals["date"].astype(str)
        signals["equity_code"] = signals["equity_code"].astype(str)
        signals["score_rank"] = pd.to_numeric(signals["score_rank"], errors="coerce").fillna(999999).astype(int)
        signals["prob_up"] = pd.to_numeric(signals["prob_up"], errors="coerce").fillna(0.0)
        return signals.sort_values(["date", "score_rank", "equity_code"]).reset_index(drop=True)

    @staticmethod
    def _build_signal_map(signals: pd.DataFrame) -> Dict[str, List[SignalCandidate]]:
        grouped: Dict[str, List[SignalCandidate]] = defaultdict(list)
        for row in signals.itertuples(index=False):
            grouped[str(row.date)].append(
                SignalCandidate(
                    code=str(row.equity_code),
                    score_rank=int(row.score_rank),
                    prob_up=float(row.prob_up),
                )
            )
        return dict(grouped)

    def _sync_position_state(self, context: Context) -> None:
        active_codes = {code for code, pos in context.portfolio.positions.items() if pos.total_qty > 0}
        for code in list(self._position_ages):
            if code not in active_codes:
                self._position_ages.pop(code, None)
        for code in active_codes:
            self._position_ages[code] = self._position_ages.get(code, 0) + 1
            self._pending_entry_dates.pop(code, None)
        for code in list(self._pending_entry_dates):
            if code not in active_codes and self._pending_entry_dates[code] != context.current_date[:8]:
                self._pending_entry_dates.pop(code, None)

    def _submit_due_sells(self, context: Context, current_date: str) -> None:
        for code, age in list(self._position_ages.items()):
            if age < self.holding_period:
                continue
            pos = context.portfolio.get_position(code)
            if pos is None or pos.sellable_qty <= 0:
                continue
            context.order(code, -pos.sellable_qty)
            logger.info("%s BQML 持有期到期卖出 %s qty=%s age=%s", current_date, code, pos.sellable_qty, age)

    @staticmethod
    def _active_position_count(context: Context) -> int:
        return sum(1 for pos in context.portfolio.positions.values() if pos.total_qty > 0)
