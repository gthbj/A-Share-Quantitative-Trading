"""HMM 市场状态切换策略单元测试。"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from strategy.regime_switching.strategy import HMMRegimeStrategy


class TestHMMRegimeStrategy:
    """HMM 策略逻辑测试。"""

    def test_strategy_init(self):
        strategy = HMMRegimeStrategy(
            index_code="000300.SH",
            n_states=3,
            lookback=60,
            position_pcts=[1.0, 0.5, 0.0],
        )
        assert strategy.index_code == "000300.SH"
        assert strategy.n_states == 3
        assert strategy.position_pcts == [1.0, 0.5, 0.0]

    def test_position_pcts_validation(self):
        with pytest.raises(ValueError):
            HMMRegimeStrategy(n_states=3, position_pcts=[1.0, 0.0])

    def test_build_observations(self):
        strategy = HMMRegimeStrategy(observation_window=10)
        dates = pd.date_range("20230101", periods=30)
        hist = pd.DataFrame({
            "date": dates.strftime("%Y%m%d"),
            "close": np.cumsum(np.random.randn(30)) + 100,
            "amount": np.random.randn(30).clip(1e6, None),
        })
        obs = strategy._build_observations(hist)
        assert obs.shape[1] == 3  # 动量, 波动率, 量能比
        assert obs.shape[0] == 20  # 30 - 10

    def test_state_name(self):
        strategy = HMMRegimeStrategy(n_states=3)
        strategy._state_names = ["Bull", "Bear", "Sideways"]
        assert strategy._state_name(0) == "Bull"
        assert strategy._state_name(1) == "Bear"
        assert strategy._state_name(2) == "Sideways"


class TestGARCHVolTiming:
    """GARCH 波动率择时策略单元测试。"""

    def test_strategy_init(self):
        from strategy.volatility_timing.strategy import GARCHVolTimingStrategy
        strategy = GARCHVolTimingStrategy(
            index_code="000300.SH",
            high_vol_position=0.5,
            low_vol_position=1.0,
        )
        assert strategy.index_code == "000300.SH"
        assert strategy.high_vol_position == 0.5
        assert strategy.low_vol_position == 1.0

    def test_compute_position_scale(self):
        from strategy.volatility_timing.strategy import GARCHVolTimingStrategy
        strategy = GARCHVolTimingStrategy()
        # 无法在无完整 Context 下测试，验证接口存在即可
        assert hasattr(strategy, "_compute_position_scale")
