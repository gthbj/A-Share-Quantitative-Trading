"""RL 组合权重管理策略。

加载预训练 PPO 模型，在回测中生成仓位调整信号。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from strategy.rl_portfolio.env import SimpleTradingEnv
from utils.logger import get_logger

logger = get_logger(__name__)

try:
    from stable_baselines3 import PPO

    HAS_SB3 = True
except ImportError:
    HAS_SB3 = False


class RLPortfolioStrategy(BaseStrategy):
    """RL 组合权重管理策略。

    Args:
        model_path: 预训练模型路径（.zip）。
        index_code: 基准指数，用于构造状态。
        observation_window: 状态观测窗口。
        rebalance_freq: 调仓频率。
    """

    def __init__(
        self,
        model_path: str,
        index_code: str = "000300.SH",
        observation_window: int = 5,
        rebalance_freq: int = 1,
    ) -> None:
        super().__init__()
        self.model_path = model_path
        self.index_code = index_code
        self.observation_window = observation_window
        self.rebalance_freq = rebalance_freq
        self._model = None
        self._returns_buffer: List[float] = []
        self._position_pct = 0.5
        self._day_count = 0

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        if not HAS_SB3:
            raise ImportError(
                "RLPortfolioStrategy 需要 stable-baselines3，"
                "请先执行 `pip install stable-baselines3`"
            )
        if not Path(self.model_path).exists():
            raise FileNotFoundError(f"模型文件不存在: {self.model_path}")
        self._model = PPO.load(self.model_path)
        self.set_universe([self.index_code])
        logger.info(f"RLPortfolioStrategy 初始化，加载模型: {self.model_path}")

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        self._day_count += 1
        if self._day_count % self.rebalance_freq != 0:
            return

        hist = context.get_price(self.index_code, count=self.observation_window + 1)
        if hist is None or len(hist) < 2:
            return

        close = hist["close"].astype(float)
        ret = float(close.pct_change().dropna().iloc[-1])
        self._returns_buffer.append(ret)

        if len(self._returns_buffer) < self.observation_window:
            return

        # 构造观测
        obs = self._build_obs()
        if obs is None:
            return

        action, _ = self._model.predict(obs, deterministic=True)
        self._position_pct = float(action) / 4.0

        context.user_data["rl_position_pct"] = self._position_pct
        logger.info(
            f"{context.current_date} RL 仓位: {self._position_pct:.2%}"
        )

    def _build_obs(self) -> Optional[np.ndarray]:
        if len(self._returns_buffer) < self.observation_window:
            return None
        hist = np.array(self._returns_buffer[-self.observation_window:])
        return np.array([
            float(hist[-1]),
            float(np.mean(hist)),
            float(np.std(hist)),
            self._position_pct,
        ], dtype=np.float32)
