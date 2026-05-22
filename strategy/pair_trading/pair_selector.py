"""配对筛选：相关性检验、协整检验、半衰期计算。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

# statsmodels 用于 ADF 检验和 OLS
try:
    from statsmodels.tsa.stattools import adfuller
    from statsmodels.regression.linear_model import OLS
    from statsmodels.tools import add_constant

    HAS_STATSMODELS = True
except ImportError:
    HAS_STATSMODELS = False


@dataclass(frozen=True)
class Pair:
    """配对结果。"""

    code_x: str
    code_y: str
    beta: float
    alpha: float
    adf_pvalue: float
    half_life: float
    correlation: float


def _ensure_statsmodels():
    if not HAS_STATSMODELS:
        raise ImportError(
            "pair_selector 需要 statsmodels，请先执行 `pip install statsmodels`"
        )


def compute_halflife(spread: np.ndarray) -> float:
    """估计价差序列的 OU 过程半衰期。

    模型:  d(spread_t) = λ * spread_{t-1} * dt + ε
    半衰期 = -ln(2) / λ

    Returns:
        半衰期（交易日数）。 np.inf 表示无均值回归。
    """
    spread = np.asarray(spread, dtype=float)
    spread = spread[~np.isnan(spread)]
    if len(spread) < 10:
        return np.inf

    # 一阶差分对滞后项回归
    y = np.diff(spread)
    x = spread[:-1]
    # 加常数项
    x_with_const = add_constant(x)
    model = OLS(y, x_with_const).fit()
    lam = model.params[1]  # x 的系数

    if lam >= 0:
        return np.inf  # 无均值回归

    half_life = -np.log(2) / lam
    return half_life


def cointegration_test(
    prices_x: pd.Series,
    prices_y: pd.Series,
) -> Tuple[float, float, float]:
    """Engle-Granger 两步法协整检验。

    Returns:
        (beta, alpha, adf_pvalue)
    """
    _ensure_statsmodels()

    # 对齐
    df = pd.concat([prices_x, prices_y], axis=1).dropna()
    if len(df) < 30:
        return 0.0, 0.0, 1.0

    x = df.iloc[:, 0].values
    y = df.iloc[:, 1].values

    # OLS: y = beta * x + alpha
    x_const = add_constant(x)
    model = OLS(y, x_const).fit()
    alpha, beta = model.params

    # 残差
    residual = y - (beta * x + alpha)

    # ADF 检验
    adf_result = adfuller(residual, maxlag=1, regression="ct")
    adf_pvalue = adf_result[1]

    return beta, alpha, adf_pvalue


def select_pairs(
    price_df: pd.DataFrame,
    corr_threshold: float = 0.8,
    adf_pvalue_threshold: float = 0.05,
    half_life_range: Tuple[float, float] = (3.0, 20.0),
) -> List[Pair]:
    """从价格矩阵中筛选协整配对。

    Args:
        price_df: 价格矩阵 (T, n)，列为股票代码。
        corr_threshold: Pearson 相关系数阈值。
        adf_pvalue_threshold: ADF 检验 p-value 阈值。
        half_life_range: 半衰期有效范围（交易日）。

    Returns:
        协整配对列表。
    """
    _ensure_statsmodels()

    codes = list(price_df.columns)
    n = len(codes)
    if n < 2:
        return []

    # 计算收益率相关性
    returns = price_df.pct_change().dropna()
    corr_matrix = returns.corr()

    pairs: List[Pair] = []
    for i in range(n):
        for j in range(i + 1, n):
            code_x, code_y = codes[i], codes[j]
            corr = corr_matrix.iloc[i, j]
            if corr < corr_threshold:
                continue

            beta, alpha, adf_p = cointegration_test(
                price_df.iloc[:, i], price_df.iloc[:, j]
            )
            if adf_p > adf_pvalue_threshold:
                continue

            # 计算半衰期
            spread = price_df.iloc[:, j].values - (beta * price_df.iloc[:, i].values + alpha)
            hl = compute_halflife(spread)
            if not (half_life_range[0] <= hl <= half_life_range[1]):
                continue

            pairs.append(
                Pair(
                    code_x=code_x,
                    code_y=code_y,
                    beta=beta,
                    alpha=alpha,
                    adf_pvalue=adf_p,
                    half_life=hl,
                    correlation=corr,
                )
            )

    # 按半衰期排序，优先半衰期适中的对
    pairs.sort(key=lambda p: abs(p.half_life - 10))
    return pairs
