"""HMM 市场状态识别策略。

识别市场隐藏状态（Bull/Bear/Sideways），在不同状态下调整仓位。
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from utils.logger import get_logger

logger = get_logger(__name__)

# hmmlearn 为可选依赖
try:
    from hmmlearn.hmm import GaussianHMM

    HAS_HMMLEARN = True
except ImportError:
    HAS_HMMLEARN = False


def _ensure_hmmlearn():
    if not HAS_HMMLEARN:
        raise ImportError(
            "HMMRegimeStrategy 需要 hmmlearn，请先执行 `pip install hmmlearn`"
        )


class HMMRegimeStrategy(BaseStrategy):
    """HMM 市场状态识别 + 仓位管理。

    Args:
        index_code: 基准指数代码（用于识别市场整体状态）。
        n_states: HMM 隐状态数（默认 3：Bull/Bear/Sideways）。
        lookback: HMM 训练窗口。
        observation_window: 观测变量计算窗口。
        position_pcts: 各状态对应的目标仓位比例，列表长度必须等于 n_states。
    """

    def __init__(
        self,
        index_code: str = "000300.SH",
        n_states: int = 3,
        lookback: int = 252,
        observation_window: int = 20,
        position_pcts: Optional[List[float]] = None,
    ) -> None:
        super().__init__()
        self.index_code = index_code
        self.n_states = n_states
        self.lookback = lookback
        self.observation_window = observation_window
        self.position_pcts = position_pcts or [1.0, 0.0, 0.5]
        if len(self.position_pcts) != n_states:
            raise ValueError("position_pcts 长度必须等于 n_states")

        self._hmm: Optional[GaussianHMM] = None
        self._current_state: int = 0
        self._state_names: List[str] = []
        self._trained: bool = False

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        _ensure_hmmlearn()
        self.set_universe([self.index_code])
        logger.info(
            f"HMMRegimeStrategy 初始化: index={self.index_code}, "
            f"n_states={self.n_states}"
        )

    def before_trading_start(self, context: Context, data: Dict[str, pd.Series]) -> None:
        if not self._trained:
            self._train_hmm(context)
            self._trained = True

        # 更新当前状态
        state = self._predict_state(context)
        self._current_state = state
        logger.info(
            f"{context.current_date} HMM 状态: {self._state_name(state)} "
            f"(仓位={self.position_pcts[state]:.0%})"
        )

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        # HMM 策略本身不下单，只通过 user_data 暴露状态
        # 其他策略可读取 context.user_data['hmm_state'] 调整仓位
        context.user_data["hmm_state"] = self._current_state
        context.user_data["hmm_state_name"] = self._state_name(self._current_state)
        context.user_data["hmm_position_pct"] = self.position_pcts[self._current_state]

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    def _train_hmm(self, context: Context) -> None:
        """用历史数据训练 HMM。"""
        hist = context.get_price(self.index_code, count=self.lookback)
        if hist is None or len(hist) < self.lookback // 2:
            logger.warning("历史数据不足，HMM 训练失败")
            return

        obs = self._build_observations(hist)
        if len(obs) < 50:
            logger.warning("观测样本不足，HMM 训练失败")
            return

        self._hmm = GaussianHMM(
            n_components=self.n_states,
            covariance_type="diag",
            n_iter=100,
            random_state=42,
        )
        self._hmm.fit(obs)

        # 按均值排序状态：高收益低波动 -> Bull，低收益高波动 -> Bear，其余 -> Sideways
        means = self._hmm.means_.flatten()
        sorted_states = np.argsort(means)[::-1]  # 从大到小
        self._state_names = ["unknown"] * self.n_states
        name_map = ["Bull", "Sideways", "Bear"]
        for rank, state_idx in enumerate(sorted_states):
            if rank < len(name_map):
                self._state_names[state_idx] = name_map[rank]
            else:
                self._state_names[state_idx] = f"State{rank}"

        logger.info(f"HMM 训练完成，状态映射: {dict(enumerate(self._state_names))}")

    def _predict_state(self, context: Context) -> int:
        """预测当前市场状态。"""
        if self._hmm is None:
            return 0

        hist = context.get_price(self.index_code, count=self.observation_window + 5)
        if hist is None or len(hist) < self.observation_window:
            return 0

        obs = self._build_observations(hist)
        if len(obs) == 0:
            return 0

        state = self._hmm.predict(obs)[-1]
        return int(state)

    def _build_observations(self, hist: pd.DataFrame) -> np.ndarray:
        """构建观测变量矩阵 (T, 3)。

        观测变量：
        1. 动量：过去 N 日收益率均值
        2. 波动率：过去 N 日收益率标准差
        3. 量能比：当日成交额 / N 日成交额均值
        """
        if "close" not in hist.columns:
            return np.empty((0, 3))

        close = hist["close"].astype(float)
        ret = close.pct_change().dropna()
        if len(ret) < self.observation_window:
            return np.empty((0, 3))

        obs = []
        for i in range(self.observation_window, len(ret) + 1):
            window = ret.iloc[i - self.observation_window : i]
            momentum = window.mean()
            volatility = window.std()

            # 量能比
            if "amount" in hist.columns:
                amt = hist["amount"].astype(float).iloc[i - self.observation_window : i]
                volume_ratio = amt.iloc[-1] / (amt.mean() + 1e-8)
            else:
                volume_ratio = 1.0

            obs.append([momentum, volatility, volume_ratio])

        return np.array(obs)

    def _state_name(self, state: int) -> str:
        if 0 <= state < len(self._state_names):
            return self._state_names[state]
        return f"State{state}"
