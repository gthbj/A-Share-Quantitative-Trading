"""GARCH 波动率预测 + 仓位管理策略。

预测基准指数波动率，在预测波动率高时降低仓位。
可作为 wrapper 叠加到其他选股策略。
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from utils.logger import get_logger

logger = get_logger(__name__)

# arch 为可选依赖
try:
    from arch import arch_model

    HAS_ARCH = True
except ImportError:
    HAS_ARCH = False


def _ensure_arch():
    if not HAS_ARCH:
        raise ImportError(
            "GARCHVolTimingStrategy 需要 arch，请先执行 `pip install arch`"
        )


class GARCHVolTimingStrategy(BaseStrategy):
    """GARCH 波动率择时。

    Args:
        index_code: 基准指数代码。
        lookback: GARCH 模型训练窗口。
        vol_window: 历史波动率分位数计算窗口。
        high_volile: 高波动阈值分位数（0~1）。
        low_volile: 低波动阈值分位数（0~1）。
        high_vol_position: 高波动时目标仓位比例。
        low_vol_position: 低波动时目标仓位比例。
    """

    def __init__(
        self,
        index_code: str = "000300.SH",
        lookback: int = 252,
        vol_window: int = 60,
        high_volile: float = 0.80,
        low_volile: float = 0.20,
        high_vol_position: float = 0.5,
        low_vol_position: float = 1.0,
    ) -> None:
        super().__init__()
        self.index_code = index_code
        self.lookback = lookback
        self.vol_window = vol_window
        self.high_volile = high_volile
        self.low_volile = low_volile
        self.high_vol_position = high_vol_position
        self.low_vol_position = low_vol_position

        self._current_position_scale: float = 1.0

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        _ensure_arch()
        self.set_universe([self.index_code])
        logger.info(
            f"GARCHVolTimingStrategy 初始化: index={self.index_code}, "
            f"high_vol_position={self.high_vol_position}, "
            f"low_vol_position={self.low_vol_position}"
        )

    def before_trading_start(self, context: Context, data: Dict[str, pd.Series]) -> None:
        scale = self._compute_position_scale(context)
        self._current_position_scale = scale
        logger.info(
            f"{context.current_date} GARCH 仓位缩放: {scale:.2%}"
        )

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        # 通过 user_data 暴露仓位缩放比例
        context.user_data["garch_position_scale"] = self._current_position_scale

    # ------------------------------------------------------------------ #
    # 内部方法
    # ------------------------------------------------------------------ #

    def _compute_position_scale(self, context: Context) -> float:
        """计算当前仓位缩放比例。"""
        hist = context.get_price(self.index_code, count=self.lookback)
        if hist is None or len(hist) < self.lookback // 2:
            return 1.0

        close = hist["close"].astype(float)
        ret = close.pct_change().dropna()
        if len(ret) < 30:
            return 1.0

        ret = ret * 100  # GARCH 对尺度敏感，放大 100 倍

        try:
            # 拟合 GARCH(1,1)
            model = arch_model(ret, vol="Garch", p=1, q=1, rescale=False)
            res = model.fit(disp="off", show_warning=False)
            forecast = res.forecast(horizon=1)
            next_vol = np.sqrt(forecast.variance.values[-1, 0])
        except Exception as exc:
            logger.debug(f"GARCH 拟合失败: {exc}")
            return 1.0

        # 历史波动率分位数
        hist_vol = ret.rolling(window=5).std().dropna()
        if len(hist_vol) < self.vol_window:
            return 1.0

        recent_vol = hist_vol.tail(self.vol_window)
        high_threshold = float(np.percentile(recent_vol, self.high_volile * 100))
        low_threshold = float(np.percentile(recent_vol, self.low_volile * 100))

        if next_vol > high_threshold:
            return self.high_vol_position
        elif next_vol < low_threshold:
            return self.low_vol_position
        else:
            # 线性插值
            if high_threshold == low_threshold:
                return self.low_vol_position
            t = (next_vol - low_threshold) / (high_threshold - low_threshold)
            scale = self.low_vol_position + t * (
                self.high_vol_position - self.low_vol_position
            )
            return float(np.clip(scale, self.high_vol_position, self.low_vol_position))
