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
        require_rich_features: bool = True,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.bq_project = bq_project
        self.bq_dataset = bq_dataset
        self.bq_location = bq_location
        self.adjust_type = adjust_type
        # ── 强制 rich 模式（默认）──
        # True（默认）：预加载或推理时 rich features 不可用 → 直接 raise，
        #              避免悄无声息退回 v1 17 维路径让用户拿到误以为是 v2 的回测结果
        # False：允许退化（仅用于调试/CI；正式回测不要打开）
        self.require_rich_features = bool(require_rich_features)
        # 预加载的富特征宽表，indexed by (date, equity_code)
        self._rich_features: Optional[pd.DataFrame] = None
        self._rich_features_index: Optional[pd.MultiIndex] = None
        # before_trading_start 的"第一次"标记（懒加载）
        self._rich_preloaded: bool = False

    # ──────────────────────────────────────────────────────────────
    # 初始化：注意 initialize 阶段不能取到 all_bars / engine.end_date
    # （PRD §10.x 修订：BacktestEngine 是先 initialize 再 _preload_bars
    # 再 context.all_bars=all_bars。所以预加载推迟到 before_trading_start。）
    # ──────────────────────────────────────────────────────────────

    def initialize(self, context: Context) -> None:
        """父类初始化。Rich features 预加载推迟到 before_trading_start。"""
        super().initialize(context)
        # 不在这里调 _preload_rich_features —— 此时 context.all_bars 未注入

    def before_trading_start(self, context: Context, data: Dict[str, pd.Series]) -> None:
        """在第一个交易日调用一次预加载（懒加载到 all_bars 已就绪）。"""
        super().before_trading_start(context, data)
        if not self._rich_preloaded:
            self._rich_preloaded = True   # 无论成败都置 True，避免每日重试
            try:
                self._preload_rich_features(context)
            except Exception as exc:
                msg = f"预加载 rich features 失败: {exc}"
                logger.error(msg, exc_info=True)
                if self.require_rich_features:
                    # 严格模式：直接抛错，让回测立即停掉而不是退化跑 v1
                    raise RuntimeError(
                        f"{msg}（require_rich_features=True；如确认要降级 v1 行为，"
                        f"显式设 require_rich_features=False）"
                    ) from exc
                # 降级模式：保持 _rich_features=None，handle_data 时退到父类逻辑
                self._rich_features = None

    def _preload_rich_features(self, context: Context) -> None:
        """从 BigQuery 拉取整个回测期 + 60 天 warmup 的 rich features。

        要求 context.all_bars 已经被 BacktestEngine 注入（即在 before_trading_start
        及之后），通过 all_bars 推导真实回测起止日。
        """
        from datetime import timedelta as _td

        # ── 1) 从 all_bars 推导真实回测起止日 ──
        all_bars = getattr(context, "all_bars", None)
        start_date: Optional[str] = None
        end_date: Optional[str] = None
        if all_bars:
            dates: List[str] = []
            for df in all_bars.values():
                if df is None or df.empty or "date" not in df.columns:
                    continue
                dates.append(str(df["date"].iloc[0]))
                dates.append(str(df["date"].iloc[-1]))
            if dates:
                start_date = min(dates)[:8]
                end_date = max(dates)[:8]

        # 兜底（极少触发，主要给单测用）：current_date ± 60 天 ~ 5 年
        if not start_date or not end_date:
            cur = pd.to_datetime(context.current_date[:8])
            start_date = (cur - _td(days=60)).strftime("%Y%m%d")
            end_date = (cur + _td(days=365 * 5)).strftime("%Y%m%d")
            logger.warning(
                f"all_bars 未提供日期边界，rich 预加载兜底范围 {start_date}~{end_date}"
            )

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

        如果 _rich_features 未加载：
          - require_rich_features=True（默认）：抛错（已在 before_trading_start
            预加载阶段抛过；这里是双重保险）
          - require_rich_features=False（显式降级）：退到父类 17/22 维路径，并
            每次都打 WARNING 强提醒
        """
        if self._rich_features is None or self._rich_features.empty:
            if self.require_rich_features:
                # 理论上 before_trading_start 已经 raise；走到这里通常意味
                # 手动注入或单测路径，明确抛错
                raise RuntimeError(
                    f"{context.current_date} rich features 未加载，且 "
                    f"require_rich_features=True；策略拒绝退化为 v1 行为"
                )
            logger.warning(
                f"{context.current_date} rich features 未加载 → 退化为父类 17/22 维路径 "
                f"（require_rich_features=False；这不是真正的 rich 策略结果！）"
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
