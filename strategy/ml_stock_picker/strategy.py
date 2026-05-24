"""ML 选股策略：基于预训练 LightGBM / XGBoost 模型的日线中频选股。

策略逻辑：
  1. 加载预训练模型（本地或 GCS）。
  2. 每 rebalance_freq 个交易日，优先读取 BigQuery DWS 增强特征快照。
  3. 模型预测每只股票的上涨概率（或收益排序得分）。
  4. 模型缺失时使用确定性 fallback score。
  5. 取 Top-K 等权持仓，卖出不在 Top-K 中的已有持仓。

面向中频（持仓几天到几周），默认 label_horizon=5、rebalance_freq=5（周频调仓）。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from strategy.ml_stock_picker.features import FeatureEngineer
from strategy.ml_stock_picker.model_storage import load_model
from utils.logger import get_logger

logger = get_logger(__name__)


class MLStockPickerStrategy(BaseStrategy):
    """机器学习选股策略。

    Args:
        model_path: 预训练模型路径（本地或 gs://）。
        model_type: 模型类型，lightgbm 或 xgboost。
        universe: 股票池代码列表。
        feature_window: 特征回看窗口（天）。
        label_horizon: 模型训练时的预测 horizon（天），仅用于日志/校验。
        top_k: 持仓数量。
        rebalance_freq: 调仓频率（交易日数）。
        position_pct: 资金使用比例（0~1）。
        feature_source: 特征来源，auto/dws/local。
        feature_set: 特征集合，technical/enhanced。
        use_deterministic_fallback: 模型不可用时是否使用确定性 fallback score。
    """

    DEFAULT_UNIVERSE = [
        "000001.SZ", "000002.SZ", "000063.SZ", "000100.SZ", "000333.SZ",
        "000568.SZ", "000651.SZ", "000725.SZ", "000768.SZ", "000858.SZ",
        "600000.SH", "600009.SH", "600016.SH", "600028.SH", "600030.SH",
        "600031.SH", "600036.SH", "600048.SH", "600276.SH", "600309.SH",
        "600406.SH", "600436.SH", "600519.SH", "600585.SH", "600690.SH",
        "600745.SH", "600809.SH", "600887.SH", "601012.SH", "601066.SH",
        "601088.SH", "601138.SH", "601166.SH", "601318.SH", "601398.SH",
        "601888.SH", "603288.SH", "603501.SH", "603986.SH", "688981.SH",
    ]

    def __init__(
        self,
        model_path: str = "",
        model_type: str = "lightgbm",
        universe: Optional[List[str]] = None,
        feature_window: int = 20,
        label_horizon: int = 5,
        top_k: int = 8,
        rebalance_freq: int = 5,
        position_pct: float = 0.95,
        feature_source: str = "auto",
        feature_set: str = "enhanced",
        use_deterministic_fallback: bool = True,
    ) -> None:
        super().__init__()
        self.model_path = model_path
        self.model_type = model_type
        self._init_universe = list(universe) if universe else list(self.DEFAULT_UNIVERSE)
        self.feature_window = feature_window
        self.label_horizon = label_horizon
        self.top_k = top_k
        self.rebalance_freq = rebalance_freq
        self.position_pct = position_pct
        if feature_source not in {"auto", "dws", "local"}:
            raise ValueError("feature_source must be 'auto', 'dws' or 'local'")
        if feature_set not in {"technical", "enhanced"}:
            raise ValueError("feature_set must be 'technical' or 'enhanced'")
        self.feature_source = feature_source
        self.feature_set = feature_set
        self.use_deterministic_fallback = use_deterministic_fallback
        # ML 训练需要较长历史 warm-up，回测引擎预加载只覆盖回测区间，
        # 模型已在训练阶段完成学习，回测时只需 model_path 加载即可。
        self.lookback_days = feature_window + 5

        self._model: Optional[Any] = None
        self._feature_engineer = FeatureEngineer(
            feature_window=feature_window, label_horizon=label_horizon
        )
        self._bar_count: int = 0  # 调仓计数器

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        self.set_universe(self._init_universe)
        if self.model_path:
            self._model = load_model(self.model_path)
            if self._model is None:
                logger.warning(
                    f"模型加载失败: {self.model_path}，策略将使用确定性增强打分 fallback。"
                )
        else:
            logger.warning("未指定 model_path，策略将使用确定性增强打分 fallback。")
        logger.info(
            f"MLStockPicker 初始化完成: universe={len(self._init_universe)}, "
            f"top_k={self.top_k}, rebalance_freq={self.rebalance_freq}, "
            f"model_type={self.model_type}, feature_source={self.feature_source}, "
            f"feature_set={self.feature_set}"
        )

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        # ── 调仓日判断 ──
        self._bar_count += 1
        if (self._bar_count - 1) % self.rebalance_freq != 0:
            return

        current_date = context.current_date
        logger.info(f"{current_date} 调仓日 (bar_count={self._bar_count})")

        feature_df = self._load_feature_snapshot(context)

        if feature_df.empty:
            logger.warning(f"{current_date} 无有效特征，跳过调仓")
            return

        codes = feature_df["code"].astype(str).tolist()
        feature_cols = FeatureEngineer.feature_columns(self.feature_set)
        X = feature_df[feature_cols].values

        # ── 模型预测 ──
        if self._model is not None:
            try:
                if self.model_type == "lightgbm":
                    scores = self._model.predict(X)
                elif self.model_type == "xgboost":
                    import xgboost as xgb
                    scores = self._model.predict(xgb.DMatrix(X))
                else:
                    raise ValueError(f"不支持的 model_type: {self.model_type}")
            except Exception as e:
                if not self.use_deterministic_fallback:
                    logger.warning(f"模型预测失败且禁用 fallback，跳过调仓: {e}")
                    return
                logger.warning(f"模型预测失败，降级为确定性增强打分: {e}")
                scores = FeatureEngineer.deterministic_score(feature_df).values
        else:
            if not self.use_deterministic_fallback:
                logger.warning("模型不可用且禁用 fallback，跳过调仓")
                return
            scores = FeatureEngineer.deterministic_score(feature_df).values

        # ── 排序选 Top-K ──
        score_df = pd.DataFrame({"code": codes, "score": scores})
        score_df = score_df.sort_values("score", ascending=False).reset_index(drop=True)
        top_codes = score_df.head(self.top_k)["code"].tolist()
        logger.info(f"{current_date} 选中 Top-{len(top_codes)}: {top_codes}")

        # ── 调仓执行 ──
        portfolio = context.portfolio
        current_positions = {
            code: pos.total_qty for code, pos in portfolio.positions.items() if pos.total_qty > 0
        }

        # 1) 卖出不在 Top-K 中的持仓
        for code, qty in list(current_positions.items()):
            if code not in top_codes and qty > 0:
                pos = portfolio.get_position(code)
                if pos and pos.sellable_qty > 0:
                    context.order(code, -pos.sellable_qty)
                    logger.info(f"{current_date} 卖出 {code} {pos.sellable_qty}股")

        # 2) 计算每只目标股票的等权目标市值
        total_value = portfolio.total_value(
            {code: float(data[code]["close"]) for code in top_codes if code in data}
        )
        if not top_codes:
            return
        target_value_per_stock = total_value * self.position_pct / len(top_codes)

        # 3) 买入 Top-K 中尚无持仓或持仓不足的股票
        for code in top_codes:
            if code not in data:
                continue
            price = float(data[code]["close"])
            if price <= 0:
                continue
            pos = portfolio.positions.get(code)
            current_qty = pos.total_qty if pos is not None else 0
            target_qty = int((target_value_per_stock / price) // 100) * 100
            delta = target_qty - current_qty
            if delta > 0:
                # 检查可用资金
                required = delta * price
                if portfolio.available_cash >= required:
                    context.order(code, delta)
                    logger.info(f"{current_date} 买入 {code} {delta}股")
                else:
                    # 资金不足时按可用资金买入
                    affordable = int((portfolio.available_cash / price) // 100) * 100
                    if affordable > 0:
                        context.order(code, affordable)
                        logger.info(
                            f"{current_date} 买入 {code} {affordable}股 (资金不足)"
                        )

    def _load_feature_snapshot(self, context: Context) -> pd.DataFrame:
        """调仓日加载当前截面特征，优先 BigQuery DWS，失败时回退本地计算。"""
        feature_df = pd.DataFrame()
        if self.feature_source in {"auto", "dws"}:
            feature_df = self._load_dws_feature_snapshot(context)
            if not feature_df.empty:
                return feature_df
            if self.feature_source == "dws":
                return feature_df
        return self._load_local_feature_snapshot(context)

    def _load_dws_feature_snapshot(self, context: Context) -> pd.DataFrame:
        loader = getattr(context.data_source, "get_equity_feature_snapshot", None)
        if loader is None:
            return pd.DataFrame()
        try:
            raw = loader(
                list(self._universe),
                context.current_date[:8],
                feature_set=self.feature_set,
            )
        except Exception as exc:
            logger.warning(f"{context.current_date} DWS 特征快照读取失败，回退本地特征: {exc}")
            return pd.DataFrame()
        if raw.empty:
            return raw
        if "code" not in raw.columns and "equity_code" in raw.columns:
            raw = raw.rename(columns={"equity_code": "code"})
        raw["code"] = raw["code"].astype(str)
        prepared = self._feature_engineer.prepare_model_frame(
            raw, feature_set=self.feature_set, require_technical=True
        )
        return prepared

    def _load_local_feature_snapshot(self, context: Context) -> pd.DataFrame:
        rows = []
        feature_cols = FeatureEngineer.feature_columns("technical")
        for code in self._universe:
            hist = context.get_price(code, count=self.feature_window + 5)
            if len(hist) < self.feature_window:
                continue
            feat_df = self._feature_engineer.compute_features(hist)
            if feat_df.empty:
                continue
            latest = feat_df.iloc[-1:].copy()
            if any(c not in latest.columns for c in feature_cols):
                continue
            latest["code"] = code
            rows.append(latest)
        if not rows:
            return pd.DataFrame()
        raw = pd.concat(rows, ignore_index=True)
        return self._feature_engineer.prepare_model_frame(
            raw, feature_set=self.feature_set, require_technical=True
        )
