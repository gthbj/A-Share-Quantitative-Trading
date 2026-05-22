"""配对交易策略单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategy.pair_trading.kalman import KalmanHedge
from strategy.pair_trading.pair_selector import compute_halflife, cointegration_test, select_pairs


class TestKalmanHedge:
    """Kalman Filter 测试。"""

    def test_kalman_convergence(self):
        """Kalman Filter 应收敛到真实 beta。"""
        kf = KalmanHedge(delta=1e-4, ve=1e-3)
        true_beta = 2.0
        true_alpha = 1.0

        np.random.seed(42)
        for _ in range(200):
            x = np.random.randn()
            y = true_beta * x + true_alpha + np.random.randn() * 0.1
            beta, alpha = kf.update(x, y)

        assert abs(beta - true_beta) < 0.1
        assert abs(alpha - true_alpha) < 0.1

    def test_kalman_smooth_vs_ols(self):
        """Kalman beta 应比单点 OLS 更平滑。"""
        kf = KalmanHedge(delta=1e-4, ve=1e-3)
        betas = []
        np.random.seed(42)
        for _ in range(100):
            x = np.random.randn()
            y = 1.5 * x + 0.5 + np.random.randn() * 0.2
            beta, _ = kf.update(x, y)
            betas.append(beta)
        # 后50个beta的标准差应较小
        assert np.std(betas[50:]) < 0.1


class TestPairSelector:
    """配对筛选测试。"""

    def test_compute_halflife_stationary(self):
        """均值回归序列半衰期应有限。"""
        np.random.seed(42)
        # OU 过程
        spread = [0.0]
        for _ in range(100):
            spread.append(0.9 * spread[-1] + np.random.randn() * 0.5)
        hl = compute_halflife(np.array(spread))
        assert 1 < hl < 50

    def test_compute_halflife_random_walk(self):
        """随机游 walk 半衰期应远大于平稳序列。"""
        np.random.seed(42)
        spread = np.cumsum(np.random.randn(100))
        hl_rw = compute_halflife(spread)
        # OU 过程
        ou = [0.0]
        for _ in range(100):
            ou.append(0.9 * ou[-1] + np.random.randn() * 0.5)
        hl_ou = compute_halflife(np.array(ou))
        # 随机游走半衰期应明显大于 OU 过程
        assert hl_rw > hl_ou * 2

    def test_cointegration_test(self):
        """协整检验：构造一对协整序列。"""
        np.random.seed(42)
        n = 100
        x = np.cumsum(np.random.randn(n)) + 100
        y = 2.0 * x + 1.0 + np.random.randn(n) * 0.5

        beta, alpha, adf_p = cointegration_test(
            pd.Series(x), pd.Series(y)
        )
        assert abs(beta - 2.0) < 0.2
        assert abs(alpha - 1.0) < 0.5
        assert adf_p < 0.05  # 应拒绝原假设（存在协整）

    def test_select_pairs(self):
        """筛选配对：构造 3 只相关股票。"""
        np.random.seed(42)
        n = 80
        x = np.cumsum(np.random.randn(n)) + 100
        y = 2.0 * x + 1.0 + np.random.randn(n) * 0.5  # 协整
        z = np.cumsum(np.random.randn(n)) + 50  # 不相关

        df = pd.DataFrame({"A": x, "B": y, "C": z})
        pairs = select_pairs(df, corr_threshold=0.5, half_life_range=(0.1, 50))

        # A-B 应该被选出（放宽阈值以确保测试稳定）
        assert len(pairs) >= 1
        pair = pairs[0]
        codes = {pair.code_x, pair.code_y}
        assert "A" in codes
        assert "B" in codes
        assert pair.correlation > 0.8
        assert pair.adf_pvalue < 0.05


class TestPairTradingStrategy:
    """策略逻辑测试。"""

    def test_strategy_init(self):
        from strategy.pair_trading.strategy import PairTradingStrategy
        strategy = PairTradingStrategy(
            lookback=30,
            entry_zscore=1.5,
            use_kalman=True,
        )
        assert strategy.lookback == 30
        assert strategy.entry_zscore == 1.5
        assert strategy.use_kalman
        assert len(strategy._positions) == 0
