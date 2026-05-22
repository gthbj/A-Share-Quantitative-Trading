"""Kalman Filter 动态对冲比率估计。"""

from __future__ import annotations

from typing import Tuple

import numpy as np


class KalmanHedge:
    """一维 Kalman Filter，用于动态估计配对交易的 beta 和 alpha。

    状态: [beta, alpha]^T（假设缓慢漂移的随机游走）
    观测: y_t = beta_t * x_t + alpha_t + v_t

    Args:
        delta: 转移协方差系数，控制 beta/alpha 的漂移速度。
            delta 越小，beta 越稳定；delta 越大，beta 跟踪越灵敏。
        ve: 观测噪声方差。
    """

    def __init__(self, delta: float = 1e-4, ve: float = 1e-3) -> None:
        self.delta = delta
        self.ve = ve

        # 状态: [beta, alpha]
        self.theta = np.array([[0.0], [0.0]])
        # 协方差
        self.cov = np.eye(2)

        # 转移协方差矩阵
        self.W = np.eye(2) * delta

    def update(self, x: float, y: float) -> Tuple[float, float]:
        """更新 Kalman Filter，返回最新的 beta, alpha。

        Args:
            x: 解释变量（如配对中股票 X 的价格）。
            y: 被解释变量（如配对中股票 Y 的价格）。

        Returns:
            (beta, alpha)
        """
        # 观测矩阵
        F = np.array([[x, 1.0]])  # shape (1, 2)
        F_T = F.T  # shape (2, 1)

        # 预测
        theta_pred = self.theta
        cov_pred = self.cov + self.W

        # 观测预测
        y_pred = F @ theta_pred

        # 残差
        residual = y - y_pred[0, 0]

        # 卡尔曼增益
        S = F @ cov_pred @ F_T + self.ve
        K = cov_pred @ F_T / S

        # 更新
        self.theta = theta_pred + K * residual
        self.cov = (np.eye(2) - K @ F) @ cov_pred

        beta = float(self.theta[0, 0])
        alpha = float(self.theta[1, 0])
        return beta, alpha

    def reset(self) -> None:
        """重置滤波器状态。"""
        self.theta = np.array([[0.0], [0.0]])
        self.cov = np.eye(2)
