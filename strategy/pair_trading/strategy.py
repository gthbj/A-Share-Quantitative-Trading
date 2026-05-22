"""配对交易策略：同行业统计套利，Kalman Filter 动态对冲。"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from strategy.pair_trading.kalman import KalmanHedge
from strategy.pair_trading.pair_selector import Pair, select_pairs
from utils.logger import get_logger

logger = get_logger(__name__)


class PairTradingStrategy(BaseStrategy):
    """配对交易策略。

    Args:
        board_code: 行业板块代码（如 ``BK0421.DC`` 白酒行业）。
        lookback: 协整估计窗口（交易日）。
        entry_zscore: 开仓 z-score 阈值。
        exit_zscore: 平仓 z-score 阈值。
        stop_zscore: 止损 z-score 阈值。
        max_holding_days: 最大持仓天数。
        use_kalman: 是否使用 Kalman Filter 动态估计对冲比率。
        capital_per_pair: 每对资金占比。
    """

    def __init__(
        self,
        board_code: Optional[str] = None,
        lookback: int = 60,
        entry_zscore: float = 2.0,
        exit_zscore: float = 0.5,
        stop_zscore: float = 3.5,
        max_holding_days: int = 15,
        use_kalman: bool = True,
        capital_per_pair: float = 0.1,
        universe: Optional[List[str]] = None,
    ) -> None:
        super().__init__()
        self.board_code = board_code
        self.lookback = lookback
        self.entry_zscore = entry_zscore
        self.exit_zscore = exit_zscore
        self.stop_zscore = stop_zscore
        self.max_holding_days = max_holding_days
        self.use_kalman = use_kalman
        self.capital_per_pair = capital_per_pair
        self._init_universe = list(universe) if universe else []

        # 运行时状态
        self._pairs: List[Pair] = []
        self._kalman_filters: Dict[str, KalmanHedge] = {}
        self._positions: Dict[str, dict] = {}  # pair_key -> {code_x, code_y, side, entry_z, entry_date}
        self._pair_prices: Dict[str, pd.DataFrame] = {}

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        if self._init_universe:
            self.set_universe(self._init_universe)
        logger.info(
            f"PairTradingStrategy 初始化: board_code={self.board_code}, "
            f"lookback={self.lookback}, use_kalman={self.use_kalman}"
        )

    def before_trading_start(self, context: Context, data: Dict[str, pd.Series]) -> None:
        """每日开盘前：更新 Kalman Filter 和价差。"""
        if not self._pairs:
            self._select_pairs(context)

        for pair in self._pairs:
            key = f"{pair.code_x}_{pair.code_y}"
            # 获取最新价格
            px_x = self._get_latest_price(context, pair.code_x)
            px_y = self._get_latest_price(context, pair.code_y)
            if px_x is None or px_y is None:
                continue

            if self.use_kalman and key in self._kalman_filters:
                kf = self._kalman_filters[key]
                beta, alpha = kf.update(px_x, px_y)
            else:
                beta, alpha = pair.beta, pair.alpha

            # 计算价差和 z-score
            spread = px_y - (beta * px_x + alpha)
            # 用历史价差的标准差
            hist_spread = self._get_hist_spread(context, pair, beta, alpha)
            if hist_spread is not None and len(hist_spread) > 5:
                zscore = (spread - hist_spread.mean()) / (hist_spread.std() + 1e-8)
            else:
                zscore = 0.0

            self._pair_prices[key] = pd.Series(
                {"px_x": px_x, "px_y": px_y, "beta": beta, "alpha": alpha, "spread": spread, "zscore": zscore}
            )

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        for pair in self._pairs:
            key = f"{pair.code_x}_{pair.code_y}"
            if key not in self._pair_prices:
                continue

            info = self._pair_prices[key]
            zscore = info["zscore"]

            has_pos = key in self._positions

            if has_pos:
                pos = self._positions[key]
                holding_days = self._day_count_since(context, pos["entry_date"])

                # 平仓条件
                if abs(zscore) < self.exit_zscore:
                    self._close_pair(context, key, "zscore回归")
                elif abs(zscore) > self.stop_zscore:
                    self._close_pair(context, key, "止损")
                elif holding_days >= self.max_holding_days:
                    self._close_pair(context, key, "超时平仓")
            else:
                # 开仓条件
                if zscore > self.entry_zscore:
                    # 价差过高，预期回归：做空 y，做多 x
                    self._open_pair(context, key, pair, "short_spread")
                elif zscore < -self.entry_zscore:
                    # 价差过低，预期回归：做多 y，做空 x
                    self._open_pair(context, key, pair, "long_spread")

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    def _select_pairs(self, context: Context) -> None:
        """筛选协整配对。"""
        if len(self._universe) < 2:
            logger.warning("universe 不足 2 只，无法筛选配对")
            return

        prices = {}
        for code in self._universe:
            hist = context.get_price(code, count=self.lookback)
            if hist is not None and not hist.empty and "close" in hist.columns:
                prices[code] = hist["close"]

        if len(prices) < 2:
            logger.warning("有效价格数据不足，无法筛选配对")
            return

        price_df = pd.concat(prices, axis=1).dropna()
        if price_df.shape[1] < 2:
            return

        try:
            self._pairs = select_pairs(
                price_df,
                corr_threshold=0.8,
                adf_pvalue_threshold=0.05,
                half_life_range=(3.0, 20.0),
            )
        except Exception as exc:
            logger.warning(f"配对筛选失败: {exc}")
            return

        logger.info(f"筛选出 {len(self._pairs)} 对协整配对")

        # 初始化 Kalman Filter
        if self.use_kalman:
            for pair in self._pairs:
                key = f"{pair.code_x}_{pair.code_y}"
                kf = KalmanHedge()
                # 用历史数据 warm up
                px_x = price_df[pair.code_x].values
                px_y = price_df[pair.code_y].values
                for x, y in zip(px_x, px_y):
                    kf.update(x, y)
                self._kalman_filters[key] = kf

    def _get_latest_price(self, context: Context, code: str) -> Optional[float]:
        hist = context.get_price(code, count=1)
        if hist is not None and not hist.empty and "close" in hist.columns:
            return float(hist["close"].iloc[-1])
        return None

    def _get_hist_spread(
        self, context: Context, pair: Pair, beta: float, alpha: float
    ) -> Optional[pd.Series]:
        hist_x = context.get_price(pair.code_x, count=self.lookback)
        hist_y = context.get_price(pair.code_y, count=self.lookback)
        if hist_x is None or hist_y is None:
            return None
        spread = hist_y["close"] - (beta * hist_x["close"] + alpha)
        return spread

    def _open_pair(
        self, context: Context, key: str, pair: Pair, side: str
    ) -> None:
        """开仓。"""
        capital = context.portfolio.total_value * self.capital_per_pair
        if capital <= 0:
            return

        px_x = self._get_latest_price(context, pair.code_x)
        px_y = self._get_latest_price(context, pair.code_y)
        if px_x is None or px_y is None or px_x <= 0 or px_y <= 0:
            return

        if side == "short_spread":
            # 做空 y，做多 x
            qty_x = int((capital / 2 / px_x) // 100) * 100
            qty_y = int((capital / 2 / px_y) // 100) * 100
            if qty_x > 0 and qty_y > 0:
                context.order(pair.code_x, qty_x)
                context.order(pair.code_y, -qty_y)
                self._positions[key] = {
                    "code_x": pair.code_x,
                    "code_y": pair.code_y,
                    "side": side,
                    "entry_date": context.current_date,
                    "qty_x": qty_x,
                    "qty_y": qty_y,
                }
                logger.info(
                    f"{context.current_date} 开仓 {key} 做空价差: 买{pair.code_x} {qty_x}, 卖{pair.code_y} {qty_y}"
                )
        else:
            # 做多 y，做空 x
            qty_x = int((capital / 2 / px_x) // 100) * 100
            qty_y = int((capital / 2 / px_y) // 100) * 100
            if qty_x > 0 and qty_y > 0:
                context.order(pair.code_x, -qty_x)
                context.order(pair.code_y, qty_y)
                self._positions[key] = {
                    "code_x": pair.code_x,
                    "code_y": pair.code_y,
                    "side": side,
                    "entry_date": context.current_date,
                    "qty_x": qty_x,
                    "qty_y": qty_y,
                }
                logger.info(
                    f"{context.current_date} 开仓 {key} 做多价差: 卖{pair.code_x} {qty_x}, 买{pair.code_y} {qty_y}"
                )

    def _close_pair(self, context: Context, key: str, reason: str) -> None:
        """平仓。"""
        if key not in self._positions:
            return
        pos = self._positions[key]
        # 反向平仓
        context.order(pos["code_x"], -pos["qty_x"])
        context.order(pos["code_y"], -pos["qty_y"])
        logger.info(
            f"{context.current_date} 平仓 {key} ({reason}): "
            f"{pos['code_x']} {-pos['qty_x']}, {pos['code_y']} {-pos['qty_y']}"
        )
        del self._positions[key]

    def _day_count_since(self, context: Context, entry_date: str) -> int:
        """估算持仓天数（简化版）。"""
        try:
            from datetime import datetime
            d1 = datetime.strptime(str(entry_date), "%Y%m%d")
            d2 = datetime.strptime(str(context.current_date), "%Y%m%d")
            return (d2 - d1).days
        except Exception:
            return 0
