"""特征工程：从日K线 DataFrame 构建机器学习特征。

面向中频策略（持仓几天到几周），特征以日线级别技术指标为主，
包含收益率、成交量、波动率、技术指标和价格位置五类特征。
"""

from __future__ import annotations

import numpy as np
import pandas as pd


TECHNICAL_FEATURE_COLUMNS = [
    "return_1d", "return_5d", "return_10d", "return_20d",
    "volume_ma5_ratio", "volume_ma20_ratio", "amount_ma5_ratio",
    "std_5d", "std_20d", "std_ratio",
    "rsi_14",
    "macd_diff", "macd_signal", "macd_hist",
    "close_to_high_20d", "close_to_ma5", "close_to_ma20",
]

FUNDAMENTAL_FEATURE_COLUMNS = [
    "pe_basic", "pb", "roe",
    "gross_margin", "net_margin", "debt_to_assets",
    "current_ratio", "asset_turnover", "market_cap_log",
]

EVENT_MONEY_FLOW_FEATURE_COLUMNS = [
    "net_inflow_to_amount",
    "main_net_inflow_to_amount",
    "dragon_tiger_net_to_amount",
    "dragon_tiger_department_count",
    "limit_up_streak",
    "is_kpl_event",
]


class FeatureEngineer:
    """特征工程器。

    Args:
        feature_window: 特征回看窗口（天），默认 20。
        label_horizon: 标签预测 horizon（天），默认 5。
    """

    def __init__(self, feature_window: int = 20, label_horizon: int = 5) -> None:
        self.feature_window = feature_window
        self.label_horizon = label_horizon

    def compute_features(self, df: pd.DataFrame) -> pd.DataFrame:
        """为单只股票计算全部特征，返回与输入等长的 DataFrame。

        输入列至少包含 [date, open, high, low, close, volume, amount]。
        输出包含原始列 + 所有特征列（前 feature_window 行为 NaN）。
        """
        if df.empty or len(df) < self.feature_window:
            return df.copy()

        out = df.copy().sort_values("date").reset_index(drop=True)
        close = out["close"]
        volume = out["volume"]
        amount = out["amount"]

        # ── 收益率动量 ──
        log_close = np.log(close)
        out["return_1d"] = log_close.diff(1)
        out["return_5d"] = log_close.diff(5)
        out["return_10d"] = log_close.diff(10)
        out["return_20d"] = log_close.diff(20)

        # ── 成交量特征 ──
        vol_ma5 = volume.rolling(5, min_periods=1).mean()
        vol_ma20 = volume.rolling(20, min_periods=1).mean()
        out["volume_ma5_ratio"] = volume / vol_ma5.replace(0, np.nan)
        out["volume_ma20_ratio"] = volume / vol_ma20.replace(0, np.nan)

        amt_ma5 = amount.rolling(5, min_periods=1).mean()
        out["amount_ma5_ratio"] = amount / amt_ma5.replace(0, np.nan)

        # ── 波动率特征 ──
        std5 = close.rolling(5, min_periods=1).std()
        std20 = close.rolling(20, min_periods=1).std()
        out["std_5d"] = std5
        out["std_20d"] = std20
        out["std_ratio"] = std5 / std20.replace(0, np.nan)

        # ── 技术指标：RSI ──
        delta = close.diff()
        gain = delta.clip(lower=0)
        loss = (-delta).clip(lower=0)
        avg_gain = gain.rolling(14, min_periods=1).mean()
        avg_loss = loss.rolling(14, min_periods=1).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        out["rsi_14"] = 100 - 100 / (1 + rs)

        # ── 技术指标：MACD ──
        ema12 = close.ewm(span=12, adjust=False).mean()
        ema26 = close.ewm(span=26, adjust=False).mean()
        macd_diff = ema12 - ema26
        macd_signal = macd_diff.ewm(span=9, adjust=False).mean()
        out["macd_diff"] = macd_diff
        out["macd_signal"] = macd_signal
        out["macd_hist"] = macd_diff - macd_signal

        # ── 价格位置 ──
        high20 = out["high"].rolling(20, min_periods=1).max()
        low20 = out["low"].rolling(20, min_periods=1).min()
        range20 = (high20 - low20).replace(0, np.nan)
        out["close_to_high_20d"] = (close - low20) / range20

        ma5 = close.rolling(5, min_periods=1).mean()
        ma20 = close.rolling(20, min_periods=1).mean()
        out["close_to_ma5"] = close / ma5.replace(0, np.nan) - 1
        out["close_to_ma20"] = close / ma20.replace(0, np.nan) - 1

        return out

    def compute_label(self, df: pd.DataFrame) -> pd.DataFrame:
        """计算未来 horizon 天对数收益率作为标签。

        输出列：
          - `label_return`：未来 horizon 天对数收益（回归标签）
          - `label_class`：1=top30%, 0=bottom30%, NaN=中间40%（分类标签）
        """
        if df.empty or "close" not in df.columns:
            return df.copy()

        out = df.copy().sort_values("date").reset_index(drop=True)
        log_close = np.log(out["close"])
        out["label_return"] = log_close.shift(-self.label_horizon) - log_close

        # 分类标签：按日截面分位数，top30% 为 1，bottom30% 为 0，中间丢弃
        # 注意：这里暂时不计算，因为分类标签需要截面信息（所有股票同一天对比）
        # 在训练时统一计算
        out["label_class"] = np.nan
        return out

    @staticmethod
    def feature_columns(feature_set: str = "technical") -> list[str]:
        """返回特征列名列表（用于训练和预测时筛选 X）。

        Args:
            feature_set: ``technical`` 保持旧 17 维技术特征；``enhanced`` 增加
                BigQuery DWS 中已生成的估值/基本面和事件/资金流特征。
        """
        if feature_set == "technical":
            return list(TECHNICAL_FEATURE_COLUMNS)
        if feature_set == "enhanced":
            return (
                list(TECHNICAL_FEATURE_COLUMNS)
                + list(FUNDAMENTAL_FEATURE_COLUMNS)
                + list(EVENT_MONEY_FLOW_FEATURE_COLUMNS)
            )
        raise ValueError("feature_set must be 'technical' or 'enhanced'")

    @staticmethod
    def optional_feature_columns(feature_set: str = "technical") -> list[str]:
        """返回可缺失但会按中性值处理的增强特征列。"""
        if feature_set == "technical":
            return []
        if feature_set == "enhanced":
            return list(FUNDAMENTAL_FEATURE_COLUMNS) + list(EVENT_MONEY_FLOW_FEATURE_COLUMNS)
        raise ValueError("feature_set must be 'technical' or 'enhanced'")

    def prepare_model_frame(
        self,
        df: pd.DataFrame,
        feature_set: str = "technical",
        require_technical: bool = True,
    ) -> pd.DataFrame:
        """补齐并清理模型输入特征。

        技术特征缺失通常表示该股票窗口不足，默认会被剔除；增强特征来自
        财报或事件表，天然稀疏，缺失时保留为 NaN 供模型处理，fallback score
        会按中性值处理。
        """
        out = df.copy()
        feature_cols = self.feature_columns(feature_set)
        for col in feature_cols:
            if col not in out.columns:
                out[col] = np.nan
            out[col] = pd.to_numeric(out[col], errors="coerce")
        out[feature_cols] = out[feature_cols].replace([np.inf, -np.inf], np.nan)

        if require_technical:
            out = out.dropna(subset=TECHNICAL_FEATURE_COLUMNS)
        return out.reset_index(drop=True)

    @staticmethod
    def deterministic_score(df: pd.DataFrame) -> pd.Series:
        """在模型不可用时生成可解释、可复现的截面 fallback score。

        该 score 只使用当前行可见特征，不使用未来收益或 label 字段。
        """
        if df.empty:
            return pd.Series(dtype=float)

        frame = df.copy()

        def rank_score(col: str, higher_is_better: bool = True) -> pd.Series:
            if col not in frame.columns:
                return pd.Series(0.5, index=frame.index, dtype=float)
            values = pd.to_numeric(frame[col], errors="coerce")
            if values.notna().sum() <= 1:
                return pd.Series(0.5, index=frame.index, dtype=float)
            ranked = values.rank(pct=True, ascending=higher_is_better)
            return ranked.fillna(0.5).astype(float)

        score = (
            0.18 * rank_score("return_20d")
            + 0.12 * rank_score("return_5d")
            + 0.10 * rank_score("macd_hist")
            + 0.08 * rank_score("volume_ma20_ratio")
            + 0.08 * rank_score("roe")
            + 0.06 * rank_score("gross_margin")
            + 0.05 * rank_score("net_margin")
            + 0.07 * rank_score("net_inflow_to_amount")
            + 0.05 * rank_score("main_net_inflow_to_amount")
            + 0.04 * rank_score("dragon_tiger_net_to_amount")
            + 0.03 * rank_score("limit_up_streak")
            + 0.02 * rank_score("is_kpl_event")
            + 0.06 * rank_score("std_ratio", higher_is_better=False)
            + 0.04 * rank_score("pb", higher_is_better=False)
            + 0.02 * rank_score("debt_to_assets", higher_is_better=False)
        )
        return score.astype(float)
