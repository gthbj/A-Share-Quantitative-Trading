"""组合优化策略：用优化方法替代等权或市值加权。

支持：
  - min_variance：最小方差
  - risk_parity：风险平价
  - max_diversification：最大分散化

回测流程：
  1. 按 rebalance_freq 个交易日调仓
  2. 用过去 lookback 日收益率估计协方差矩阵
  3. 优化求解目标权重
  4. 按 next_open 执行（T+1）
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from strategy.portfolio_optimization.optimizer import PortfolioOptimizer, _sample_covariance
from utils.logger import get_logger

logger = get_logger(__name__)


class PortfolioOptStrategy(BaseStrategy):
    """组合优化权重分配策略。

    Args:
        universe: 股票池，默认沪深300成分股（需用户传入）。
        method: 优化方法，``min_variance`` / ``risk_parity`` / ``max_diversification``
        lookback: 协方差估计窗口（交易日）。
        rebalance_freq: 调仓频率（交易日）。
        max_weight: 单只股票权重上限。
        min_weight: 单只股票权重下限。
    """

    def __init__(
        self,
        universe: Optional[List[str]] = None,
        method: str = "risk_parity",
        lookback: int = 60,
        rebalance_freq: int = 5,
        max_weight: float = 0.20,
        min_weight: float = 0.0,
    ) -> None:
        super().__init__()
        self._init_universe = list(universe) if universe else []
        self.method = method
        self.lookback = lookback
        self.rebalance_freq = rebalance_freq
        self.max_weight = max_weight
        self.min_weight = min_weight
        self.optimizer = PortfolioOptimizer(method=method)
        self._day_count = 0
        self._target_weights: Dict[str, float] = {}

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        if self._init_universe:
            self.set_universe(self._init_universe)
        else:
            # 默认使用宽基 ETF 作为演示
            self.set_universe(["510300.SH"])
        logger.info(
            f"PortfolioOptStrategy 初始化: method={self.method}, "
            f"universe={len(self._universe)}只, lookback={self.lookback}, "
            f"rebalance_freq={self.rebalance_freq}"
        )

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        self._day_count += 1

        # 只在调仓日执行
        if self._day_count % self.rebalance_freq != 0:
            return

        if len(self._universe) < 2:
            logger.warning("股票池不足 2 只，无法优化，跳过调仓")
            return

        # 1. 获取历史收益率
        returns_df = self._get_returns_matrix(context)
        if returns_df is None or returns_df.shape[1] < 2:
            logger.warning("收益率矩阵无效，跳过调仓")
            return

        codes = list(returns_df.columns)
        returns = returns_df.values

        # 2. 估计协方差矩阵
        try:
            cov = _sample_covariance(returns)
        except Exception as exc:
            logger.warning(f"协方差估计失败: {exc}")
            return

        # 3. 优化权重
        try:
            weights = self.optimizer.optimize(
                cov_matrix=cov,
                max_weight=self.max_weight,
                min_weight=self.min_weight,
            )
        except Exception as exc:
            logger.warning(f"优化求解失败: {exc}")
            return

        # 4. 映射到股票代码
        self._target_weights = dict(zip(codes, weights))

        # 5. 生成调仓订单
        self._rebalance(context)

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    def _get_returns_matrix(self, context: Context) -> Optional[pd.DataFrame]:
        """获取各标的在 lookback 窗口内的日收益率，返回 DataFrame (T, n)。"""
        price_list = []
        valid_codes = []
        for code in self._universe:
            hist = context.get_price(code, count=self.lookback + 1)
            if hist is None or hist.empty or len(hist) < self.lookback // 2:
                continue
            if "close" not in hist.columns:
                continue
            closes = hist["close"].astype(float)
            # 计算日收益率
            ret = closes.pct_change().dropna()
            if len(ret) < 5:
                continue
            price_list.append(ret)
            valid_codes.append(code)

        if len(price_list) < 2:
            return None

        # 对齐日期索引
        df = pd.concat(price_list, axis=1)
        df.columns = valid_codes
        df = df.dropna(how="all")
        return df

    def _rebalance(self, context: Context) -> None:
        """根据目标权重生成买卖订单。"""
        total_value = context.portfolio.total_value
        if total_value <= 0:
            return

        # 当前持仓市值
        current_values = {}
        for code in self._universe:
            pos = context.portfolio.get_position(code)
            if pos:
                # 用当前 bar 的 close 估算市值
                hist = context.get_price(code, count=1)
                if hist is not None and not hist.empty:
                    price = float(hist["close"].iloc[-1])
                    current_values[code] = pos.total_qty * price
                else:
                    current_values[code] = 0.0
            else:
                current_values[code] = 0.0

        for code, target_w in self._target_weights.items():
            target_value = total_value * target_w
            current_value = current_values.get(code, 0.0)
            delta_value = target_value - current_value

            if abs(delta_value) < total_value * 0.005:
                # 偏离 < 0.5% 忽略，减少换手
                continue

            hist = context.get_price(code, count=1)
            if hist is None or hist.empty:
                continue
            price = float(hist["close"].iloc[-1])
            if price <= 0:
                continue

            delta_qty = int(delta_value / price)
            # 对齐到 100 股整数倍
            delta_qty = (delta_qty // 100) * 100

            if delta_qty == 0:
                continue

            # 买入时检查可用资金
            if delta_qty > 0:
                needed = delta_qty * price
                if needed > context.portfolio.available_cash:
                    # 资金不足，按可用资金调整
                    max_qty = int(context.portfolio.available_cash / price)
                    delta_qty = (max_qty // 100) * 100
                    if delta_qty <= 0:
                        continue

            context.order(code, delta_qty)
            action = "买入" if delta_qty > 0 else "卖出"
            logger.info(
                f"{context.current_date} {action} {code} {abs(delta_qty)}股 "
                f"目标权重={target_w:.2%}"
            )
