"""RL 交易环境（Gymnasium 接口）。

简化版：
- 状态 = [动量, 波动率, 当前仓位]
- 动作 = 0~4（大幅减仓/小幅减仓/不变/小幅加仓/大幅加仓）
- 奖励 = 日收益率 - 换手惩罚
"""

from __future__ import annotations

from typing import Optional

import gymnasium as gym
import numpy as np


class SimpleTradingEnv(gym.Env):
    """简化交易环境。

    Args:
        returns: 日收益率序列 (T,)。
        initial_position: 初始仓位（0~1）。
    """

    def __init__(
        self,
        returns: np.ndarray,
        initial_position: float = 0.5,
        transaction_cost: float = 0.001,
    ) -> None:
        super().__init__()
        self.returns = np.asarray(returns, dtype=float)
        self.initial_position = initial_position
        self.transaction_cost = transaction_cost
        self.n_steps = len(returns)

        # 状态: [当前收益率, 过去5日平均收益率, 过去5日波动率, 当前仓位]
        self.observation_space = gym.spaces.Box(
            low=-np.inf, high=np.inf, shape=(4,), dtype=np.float32
        )
        # 动作: 0=空仓, 1=25%, 2=50%, 3=75%, 4=满仓
        self.action_space = gym.spaces.Discrete(5)

        self._current_step = 0
        self._current_position = initial_position

    def reset(self, seed: Optional[int] = None, options=None):
        super().reset(seed=seed)
        self._current_step = 0
        self._current_position = self.initial_position
        return self._get_obs(), {}

    def step(self, action: int):
        target_position = action / 4.0  # 0, 0.25, 0.5, 0.75, 1.0
        turnover = abs(target_position - self._current_position)
        tc = turnover * self.transaction_cost

        # 日收益 = 仓位 * 市场收益 - 交易成本
        market_ret = self.returns[self._current_step]
        portfolio_ret = target_position * market_ret - tc

        # 奖励 = 收益率 - 回撤惩罚（简化）
        reward = portfolio_ret * 100  # 放大奖励信号

        self._current_position = target_position
        self._current_step += 1
        terminated = self._current_step >= self.n_steps
        truncated = False

        return self._get_obs(), float(reward), terminated, truncated, {}

    def _get_obs(self) -> np.ndarray:
        idx = self._current_step
        ret = self.returns[idx] if idx < self.n_steps else 0.0

        start = max(0, idx - 5)
        hist = self.returns[start:idx]
        momentum = float(np.mean(hist)) if len(hist) > 0 else 0.0
        volatility = float(np.std(hist)) if len(hist) > 1 else 0.0

        return np.array([ret, momentum, volatility, self._current_position], dtype=np.float32)
