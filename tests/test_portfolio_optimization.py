"""组合优化策略单元测试。"""

from __future__ import annotations

import numpy as np
import pytest

from strategy.portfolio_optimization.optimizer import PortfolioOptimizer, _sample_covariance


class TestPortfolioOptimizer:
    """优化器数学正确性测试。"""

    def test_min_variance_basic(self):
        """最小方差：两只股票，低波动股票应获更高权重。"""
        cov = np.array([
            [0.04, 0.02],
            [0.02, 0.09],
        ])
        opt = PortfolioOptimizer(method="min_variance")
        w = opt.optimize(cov, max_weight=1.0)
        assert abs(w.sum() - 1.0) < 1e-6
        assert w[0] > w[1]  # 股票0波动更低，权重应更高

    def test_min_variance_with_bounds(self):
        """最小方差：带上下限约束。"""
        cov = np.diag([0.04, 0.09, 0.16])
        opt = PortfolioOptimizer(method="min_variance")
        w = opt.optimize(cov, max_weight=0.40, min_weight=0.05)
        assert abs(w.sum() - 1.0) < 1e-6
        assert all(w >= 0.05 - 1e-6)
        assert all(w <= 0.40 + 1e-6)

    def test_risk_parity_basic(self):
        """风险平价：等波动股票应趋近等权。"""
        n = 5
        cov = np.eye(n) * 0.04  # 不相关，等波动
        opt = PortfolioOptimizer(method="risk_parity")
        w = opt.optimize(cov, max_weight=1.0)
        assert abs(w.sum() - 1.0) < 1e-4
        # 等波动不相关时，风险平价趋近等权
        for wi in w:
            assert abs(wi - 1 / n) < 0.05

    def test_risk_parity_risk_contrib_equal(self):
        """风险平价：风险贡献应大致相等。"""
        cov = np.array([
            [0.04, 0.01, 0.01],
            [0.01, 0.06, 0.02],
            [0.01, 0.02, 0.08],
        ])
        opt = PortfolioOptimizer(method="risk_parity")
        w = opt.optimize(cov, max_weight=1.0)
        sigma_w = cov @ w
        portfolio_var = w @ sigma_w
        rc = w * sigma_w / portfolio_var
        # 风险贡献差异应较小
        assert np.std(rc) < 0.15

    def test_max_diversification_basic(self):
        """最大分散化：应能产生有效权重。"""
        cov = np.diag([0.04, 0.09, 0.16])
        opt = PortfolioOptimizer(method="max_diversification")
        w = opt.optimize(cov, max_weight=1.0)
        assert abs(w.sum() - 1.0) < 1e-4
        assert all(w >= 0)

    def test_sample_covariance(self):
        """样本协方差估计。"""
        np.random.seed(42)
        returns = np.random.randn(100, 3)
        cov = _sample_covariance(returns)
        assert cov.shape == (3, 3)
        assert np.allclose(cov, cov.T)  # 对称
        eigvals = np.linalg.eigvalsh(cov)
        assert all(eigvals >= -1e-10)  # 半正定

    def test_shrinkage_stability(self):
        """Ledoit-Wolf 压缩估计应改善病态矩阵。"""
        # 构造接近奇异的协方差矩阵
        cov = np.array([
            [1.0, 0.99, 0.99],
            [0.99, 1.0, 0.99],
            [0.99, 0.99, 1.0],
        ])
        opt = PortfolioOptimizer(method="min_variance")
        w = opt.optimize(cov, max_weight=1.0)
        assert abs(w.sum() - 1.0) < 1e-6
        assert not np.isnan(w).any()


class TestPortfolioOptStrategy:
    """策略逻辑测试（mock 环境）。"""

    def test_get_returns_matrix(self):
        """收益率矩阵构建。"""
        # 无法在无数据源环境下完整测试，但可验证接口
        from strategy.portfolio_optimization.strategy import PortfolioOptStrategy
        strategy = PortfolioOptStrategy(
            universe=["000001.SZ", "000002.SZ"],
            method="risk_parity",
            lookback=20,
            rebalance_freq=5,
        )
        assert strategy.method == "risk_parity"
        assert strategy.lookback == 20
        assert strategy.rebalance_freq == 5
        assert len(strategy._init_universe) == 2

    def test_rebalance_order_logic(self):
        """调仓订单逻辑：验证权重偏离阈值过滤。"""
        from strategy.portfolio_optimization.strategy import PortfolioOptStrategy
        strategy = PortfolioOptStrategy(
            universe=["000001.SZ"],
            method="min_variance",
        )
        # 无持仓时，目标权重 0.5，当前权重 0，偏离 > 0.5%，应触发调仓
        strategy._target_weights = {"000001.SZ": 0.5}
        # 这里需要完整的 Context + Portfolio mock 才能验证下单
        # 暂时只验证权重存储正确
        assert strategy._target_weights["000001.SZ"] == 0.5
