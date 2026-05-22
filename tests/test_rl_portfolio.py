"""RL 组合权重管理策略单元测试。"""

from __future__ import annotations

import numpy as np
import pytest

from strategy.rl_portfolio.env import SimpleTradingEnv


class TestSimpleTradingEnv:
    """RL 环境测试。"""

    def test_env_reset(self):
        np.random.seed(42)
        returns = np.random.randn(100) * 0.01
        env = SimpleTradingEnv(returns)
        obs, info = env.reset()
        assert obs.shape == (4,)
        assert -1 <= obs[-1] <= 1  # 仓位

    def test_env_step(self):
        np.random.seed(42)
        returns = np.random.randn(100) * 0.01
        env = SimpleTradingEnv(returns)
        env.reset()
        obs, reward, terminated, truncated, info = env.step(2)  # 50% 仓位
        assert obs.shape == (4,)
        assert isinstance(reward, float)
        assert not terminated

    def test_env_terminated(self):
        np.random.seed(42)
        returns = np.random.randn(5) * 0.01
        env = SimpleTradingEnv(returns)
        env.reset()
        for _ in range(4):
            _, _, terminated, _, _ = env.step(2)
            assert not terminated
        _, _, terminated, _, _ = env.step(2)
        assert terminated


class TestRLPortfolioStrategy:
    """策略接口测试。"""

    def test_strategy_init(self):
        from strategy.rl_portfolio.strategy import RLPortfolioStrategy
        strategy = RLPortfolioStrategy(
            model_path="strategy/rl_portfolio/models/ppo_v1.zip",
            index_code="000300.SH",
            observation_window=5,
        )
        assert strategy.index_code == "000300.SH"
        assert strategy.observation_window == 5
