"""MLRichPickerStrategy 主策略（PRD_20260525_03）。

继承 ml_multi_horizon_picker.MLMultiHorizonStrategy，覆盖：

- 特征加载：initialize 时预拉取整个回测期 + warmup 的 rich features 宽表，
  按 (date, equity_code) 建索引；handle_data 时 O(1) 查表
- 推理特征列：30 维 buy / 35 维 sell（含基本面 + 资金流）

其余逻辑（6 个 sell trigger / regime / 走步切换 / trading_permissions 过滤）
继承自父类，零修改。
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from strategy.base_strategy import Context
from strategy.ml_multi_horizon_picker.strategy import MLMultiHorizonStrategy
from strategy.ml_multi_horizon_picker.model_storage import BUY_HORIZONS, SELL_MODEL_NAME

from strategy.ml_rich_picker.features import (
    DAILY_FEATURE_COLUMNS,
    FUNDAMENTAL_FEATURE_COLUMNS,
    EVENT_FEATURE_COLUMNS,
    RICH_BUY_FEATURE_COLUMNS,
    RICH_SELL_FEATURE_COLUMNS,
    deterministic_rich_score,
    deterministic_rich_sell_score,
)
from strategy.ml_multi_horizon_picker.features import compute_sell_risk_features
from utils.logger import get_logger

logger = get_logger(__name__)


class MLRichPickerStrategy(MLMultiHorizonStrategy):
    """富特征 ML 择时择股策略。

    与父类 MLMultiHorizonStrategy 的差异：

    1. **特征加载**：initialize 时一次性从 BigQuery 拉取整个回测期 + warmup
       的 rich features 宽表（3-表 JOIN），存为 (date, code) → row dict。
       handle_data 时 O(1) 查表，无需每只股票循环。

    2. **特征维度**：buy 30 维（17 daily + 8 fundamental + 5 event），
       sell 35 维（30 + 5 sell-side 风险特征）。

    其余（6 个 sell trigger / regime / 走步切换）完全复用父类。
    """

    # 类常量
    RICH_BUY_FEATURE_COLUMNS = RICH_BUY_FEATURE_COLUMNS
    RICH_SELL_FEATURE_COLUMNS = RICH_SELL_FEATURE_COLUMNS

    def __init__(
        self,
        *args,
        bq_project: str = "data-aquarium",
        bq_dataset: str = "ashare",
        bq_location: str = "asia-east2",
        adjust_type: str = "qfq",
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.bq_project = bq_project
        self.bq_dataset = bq_dataset
        self.bq_location = bq_location
        self.adjust_type = adjust_type
        # 预加载的富特征宽表，indexed by (date, equity_code)
        self._rich_features: Optional[pd.DataFrame] = None
        self._rich_features_index: Optional[pd.MultiIndex] = None

    # ──────────────────────────────────────────────────────────────
    # 初始化：预加载富特征
    # ──────────────────────────────────────────────────────────────

    def initialize(self, context: Context) -> None:
        """父类初始化 + 一次性拉富特征宽表。"""
        super().initialize(context)
        try:
            self._preload_rich_features(context)
        except Exception as exc:
            logger.error(
                f"预加载 rich features 失败，策略将不可推理: {exc}", exc_info=True
            )
            self._rich_features = None

    def _preload_rich_features(self, context: Context) -> None:
        """从 BigQuery 拉取整个回测期 + 60 天 warmup 的 rich features。

        本方法在 initialize 中调用一次，把数据缓存到 self._rich_features，
        后续 handle_data 直接查表。
        """
        # 估算回测时段
        # BaseStrategy 没有直接暴露 start/end，但引擎 attach 后会有 all_bars
        # 简化处理：使用配置里的 backtest 时段（如果有）或从 universe / current_date 推算
        # 实际上回测时段在 _engine.start_date / end_date，可通过 context.engine 拿到
        from datetime import timedelta as _td

        if hasattr(context, "engine") and context.engine is not None:
            start_date = getattr(context.engine, "start_date", None)
            end_date = getattr(context.engine, "end_date", None)
        else:
            start_date = None
            end_date = None

        # 兜底：用 current_date - 1 年到 current_date + 5 年作为最大范围
        if not start_date or not end_date:
            cur = pd.to_datetime(context.current_date[:8])
            start_date = (cur - _td(days=60)).strftime("%Y%m%d")
            end_date = (cur + _td(days=365 * 5)).strftime("%Y%m%d")

        # warmup 60 天供 feature_window
        load_start = (
            pd.to_datetime(start_date) - _td(days=self.feature_window + 60)
        ).strftime("%Y%m%d")

        logger.info(
            f"预加载 rich features: {load_start} ~ {end_date} "
            f"(universe={len(self._init_universe)} 股, ~{(pd.to_datetime(end_date)-pd.to_datetime(load_start)).days} 天)"
        )

        # 直接复用 walk_forward 的 SQL（一致性保证）
        from strategy.ml_multi_horizon_picker.walk_forward import WalkForwardConfig
        from strategy.ml_rich_picker.walk_forward import _load_rich_features_from_bq

        # 构造一个最小化的 WalkForwardConfig 用于 SQL 调用
        cfg = WalkForwardConfig(
            initial_train_start="", initial_train_end="", final_retrain_date="",
            retrain_freq="month_end", rolling_window_years=3,
            model_root="", trading_permissions={}, liquidity_top_n=0,
            liquidity_lookback_days=60, fixed_codes=[],
            buy_horizons=[1, 5, 10, 20],
            buy_top_quantile=0.3, buy_bottom_quantile=0.3,
            sell_lookforward=5, sell_drawdown_threshold=-0.05,
            lightgbm_params={},
            bq_project=self.bq_project,
            bq_dataset=self.bq_dataset,
            bq_location=self.bq_location,
            bq_table_daily="dws_equity_daily_features",
            bq_table_kline="dwd_fact_equity_kline_1d",
            bq_table_dim="dwd_dim_security",
            adjust_type=self.adjust_type,
            valid_days=60,
        )

        df = _load_rich_features_from_bq(
            cfg, load_start, end_date, code_filter=self._init_universe
        )
        if df.empty:
            logger.warning("rich features 加载为空")
            self._rich_features = None
            return

        # 计算 sell-side 5 维风险特征
        from strategy.ml_multi_horizon_picker.walk_forward import _enrich_sell_features_grouped
        df = _enrich_sell_features_grouped(df)

        # 建索引
        df = df.sort_values(["date", "equity_code"]).reset_index(drop=True)
        df = df.set_index(["date", "equity_code"])
        self._rich_features = df
        logger.info(f"rich features 预加载完成: {len(df):,} 行")

    # ──────────────────────────────────────────────────────────────
    # 重写 _score_universe：从预加载表查特征
    # ──────────────────────────────────────────────────────────────

    def _score_universe(self, context: Context) -> Optional[pd.DataFrame]:
        """对 universe 内每只股票计算 buy/sell prob。

        覆盖父类：从 self._rich_features 查表（O(1)），不再逐股 get_price 计算。
        """
        if self._rich_features is None or self._rich_features.empty:
            logger.warning(
                f"{context.current_date} rich features 未加载，回退父类逻辑"
            )
            return super()._score_universe(context)

        current_date = context.current_date[:8]
        try:
            today_df = self._rich_features.xs(current_date, level="date").copy()
        except KeyError:
            logger.warning(f"{current_date} 在预加载特征表中无记录")
            return None

        if today_df.empty:
            return None

        today_df = today_df.reset_index()
        # 限制到 universe
        today_df = today_df[today_df["equity_code"].isin(self._universe)]
        if today_df.empty:
            return None

        # 缺失任何 daily 特征的股票直接丢弃（基本面 / event NaN 允许）
        before = len(today_df)
        today_df = today_df.dropna(subset=DAILY_FEATURE_COLUMNS)
        if len(today_df) < before:
            logger.debug(f"{current_date} daily 特征不全丢弃 {before - len(today_df)} 股")
        if today_df.empty:
            return None

        codes = today_df["equity_code"].tolist()
        X_buy = today_df[RICH_BUY_FEATURE_COLUMNS].values
        X_sell = today_df[RICH_SELL_FEATURE_COLUMNS].values

        # 预测 4 个 buy horizon
        score_df = pd.DataFrame({"code": codes})
        for h in BUY_HORIZONS:
            col = f"prob_up_h{h}"
            model = self._models.get(f"buy_h{h}")
            if model is not None:
                try:
                    pred = model.predict(X_buy)
                    score_df[col] = self._squash_to_prob(pred)
                except Exception as exc:
                    logger.warning(f"buy_h{h} 预测失败，降级 fallback: {exc}")
                    score_df[col] = self._squash_to_prob(
                        deterministic_rich_score(today_df[RICH_BUY_FEATURE_COLUMNS])
                    )
            elif self.use_deterministic_fallback:
                score_df[col] = self._squash_to_prob(
                    deterministic_rich_score(today_df[RICH_BUY_FEATURE_COLUMNS])
                )
            else:
                score_df[col] = 0.0

        # 预测 sell
        sell_model = self._models.get(SELL_MODEL_NAME)
        if sell_model is not None:
            try:
                score_df["prob_sell"] = self._squash_to_prob(sell_model.predict(X_sell))
            except Exception as exc:
                logger.warning(f"sell 预测失败，降级 fallback: {exc}")
                score_df["prob_sell"] = deterministic_rich_sell_score(
                    today_df[RICH_SELL_FEATURE_COLUMNS]
                )
        else:
            score_df["prob_sell"] = deterministic_rich_sell_score(
                today_df[RICH_SELL_FEATURE_COLUMNS]
            )

        # score = max(h5, h10, h20)（与 v1 一致）
        score_df["score"] = score_df[["prob_up_h5", "prob_up_h10", "prob_up_h20"]].max(axis=1)
        return score_df
