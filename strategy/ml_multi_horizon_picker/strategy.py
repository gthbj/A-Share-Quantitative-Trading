"""多 Horizon 机器学习择时择股策略。

设计要点（详见 PRD_20260524_12）：
- 4 个独立 LightGBM 买入模型（horizon 1/5/10/20 天）
- 1 个独立 LightGBM 卖出风险模型
- 卖出触发（任一命中即卖）：
    a. 硬止损（regime 调制）
    b. 追踪止盈（从持仓期高点回撤）
    c. 最大持仓交易日兜底
    d. 排名迟滞（连续 N 天不在 Top-2K）
    e. prob_up 兜底（低于 floor 阈值）
    f. 卖出持仓决策模型（prob_sell > threshold）
- Regime 三态调制目标持仓数与止损宽度

回测路径：日频 handle_data，符合 BacktestEngine T 信号 → T+1 开盘成交规范。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from strategy.base_strategy import BaseStrategy, Context
from strategy.ml_stock_picker.features import FeatureEngineer as BaseFeatureEngineer
from strategy.ml_multi_horizon_picker.features import (
    BUY_FEATURE_COLUMNS,
    SELL_FEATURE_COLUMNS,
    compute_sell_risk_features,
    deterministic_score,
    deterministic_sell_score,
)
from strategy.ml_multi_horizon_picker.model_registry import ModelRegistry
from strategy.ml_multi_horizon_picker.model_storage import (
    BUY_HORIZONS,
    SELL_MODEL_NAME,
    load_bundle,
)
from strategy.ml_multi_horizon_picker.regime import (
    Regime,
    combine_regime_with_market_breadth,
    is_crisis_regime,
    latest_regime,
    market_breadth_metrics,
    regime_stop_loss,
)
from strategy.ml_multi_horizon_picker.tradable import filter_codes
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PositionState:
    """每个持仓的额外状态簿记（除 Portfolio.Position 之外）。"""

    entered_date: str
    expected_horizon: int  # 建仓时记录的 buy horizon，仅作为持仓状态特征
    peak_price: float       # 持仓期间最高收盘价
    rank_dropout_streak: int = 0  # 连续不在 Top-2K 的天数


class MLMultiHorizonStrategy(BaseStrategy):
    """多 Horizon ML 择时择股策略。"""

    DYNAMIC_UNIVERSE = True
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
    BENCHMARK_CODE = "000300.SH"

    def __init__(
        self,
        model_dir: str = "models/ml_multi_horizon",
        model_registry_path: Optional[str] = None,    # 走步模式：模型注册表 JSON 路径
        universe: Optional[List[str]] = None,
        universe_source: str = "static",
        liquidity_top_n: int = 500,
        liquidity_lookback_days: int = 60,
        trading_permissions: Optional[Dict[str, bool]] = None,  # 可交易过滤（PRD_20260524_13）
        target_position_count: int = 5,
        min_buy_prob: float = 0.50,
        min_prob_floor: float = 0.30,
        sell_threshold: float = 0.70,
        stop_loss_pct_bull: float = 0.05,
        stop_loss_pct_bear: float = 0.03,
        trailing_stop_pct: float = 0.03,
        min_hold_days: int = 3,
        max_hold_days: Optional[int] = 20,
        dropout_persistence_days: int = 2,
        rank_buffer_multiplier: int = 2,
        use_deterministic_fallback: bool = True,
        position_pct: float = 0.95,
        regime_target_position_counts: Optional[Dict[str, int]] = None,
        regime_position_pcts: Optional[Dict[str, float]] = None,
        bear_clear_existing: bool = True,
        crisis_enabled: bool = False,
        crisis_drawdown_threshold: float = -0.10,
        crisis_fast_drop_threshold: float = -0.06,
        crisis_breadth_threshold: float = 0.20,
        crisis_selloff_share_threshold: float = 0.75,
        breadth_bear_threshold: float = 0.35,
        breadth_recovery_threshold: float = 0.55,
        feature_window: int = 20,    # 与 BaseFeatureEngineer 默认一致；sell-side 60d 特征用 min_periods=1 降级
    ) -> None:
        super().__init__()
        self.model_dir = model_dir
        self.model_registry_path = model_registry_path
        self.trading_permissions = trading_permissions
        self.universe_source = str(universe_source or "static")
        self.liquidity_top_n = int(liquidity_top_n)
        self.liquidity_lookback_days = int(liquidity_lookback_days)
        self._explicit_universe = universe is not None and len(universe) > 0
        if self._explicit_universe:
            self._init_universe = list(universe or [])
        elif self.universe_source == "liquidity_top":
            self._init_universe = []
        else:
            self._init_universe = list(self.DEFAULT_UNIVERSE)
        # 用 trading_permissions 过滤 universe（PRD_20260524_13）
        if trading_permissions is not None:
            filtered = filter_codes(self._init_universe, trading_permissions)
            if len(filtered) < len(self._init_universe):
                blocked = set(self._init_universe) - set(filtered)
                logger.info(
                    f"trading_permissions 过滤掉 {len(blocked)} 只: {sorted(blocked)[:10]}…"
                )
            self._init_universe = filtered
        self.target_position_count = target_position_count
        self.min_buy_prob = min_buy_prob
        self.min_prob_floor = min_prob_floor
        self.sell_threshold = sell_threshold
        self.stop_loss_pct_bull = stop_loss_pct_bull
        self.stop_loss_pct_bear = stop_loss_pct_bear
        self.trailing_stop_pct = trailing_stop_pct
        self.min_hold_days = min_hold_days
        self.max_hold_days = (
            int(max_hold_days)
            if max_hold_days is not None and int(max_hold_days) > 0
            else None
        )
        self.dropout_persistence_days = dropout_persistence_days
        self.rank_buffer_multiplier = rank_buffer_multiplier
        self.use_deterministic_fallback = use_deterministic_fallback
        self.position_pct = position_pct
        self.regime_target_position_counts = self._normalize_regime_counts(
            regime_target_position_counts
        )
        self.regime_position_pcts = self._normalize_regime_position_pcts(
            regime_position_pcts
        )
        self.bear_clear_existing = bool(bear_clear_existing)
        self.crisis_enabled = bool(crisis_enabled)
        self.crisis_drawdown_threshold = float(crisis_drawdown_threshold)
        self.crisis_fast_drop_threshold = float(crisis_fast_drop_threshold)
        self.crisis_breadth_threshold = float(crisis_breadth_threshold)
        self.crisis_selloff_share_threshold = float(crisis_selloff_share_threshold)
        self.breadth_bear_threshold = float(breadth_bear_threshold)
        self.breadth_recovery_threshold = float(breadth_recovery_threshold)
        self.feature_window = feature_window
        # 回测时引擎会预加载 feature_window 天的 warmup
        self.lookback_days = feature_window + 5

        self._base_feature_engineer = BaseFeatureEngineer(
            feature_window=20, label_horizon=5
        )
        self._models: Dict[str, Optional[Any]] = {}
        self._position_state: Dict[str, PositionState] = {}
        self._trading_date_index: Dict[str, int] = {}
        # 走步模式专用
        self._model_registry: Optional[ModelRegistry] = None
        self._current_model_dir: Optional[str] = None
        # 回测诊断输出专用：不参与交易决策，仅供 BacktestEngine 记录日志/CSV。
        self._last_score_df: Optional[pd.DataFrame] = None
        self._last_regime: str = ""
        self._last_target_n: int = 0
        self._last_stop_loss_pct: float = 0.0
        self._last_position_pct: float = 0.0

    def _normalize_regime_counts(
        self,
        configured: Optional[Dict[str, int]],
    ) -> Dict[str, int]:
        """标准化 regime → 目标持仓数配置。"""
        max_count = max(1, int(self.target_position_count))
        counts = {
            Regime.BULL.value: max_count,
            Regime.NEUTRAL.value: min(3, max_count),
            Regime.BEAR.value: 0,
            Regime.CRISIS.value: 0,
        }
        if configured:
            for key, value in configured.items():
                if key in counts:
                    counts[key] = max(0, min(max_count, int(value)))
        return counts

    def _normalize_regime_position_pcts(
        self,
        configured: Optional[Dict[str, float]],
    ) -> Dict[str, float]:
        """标准化 regime → 最大资金使用比例配置。"""
        base = max(0.0, min(1.0, float(self.position_pct)))
        pcts = {
            Regime.BULL.value: min(base, 0.85),
            Regime.NEUTRAL.value: min(base, 0.45),
            Regime.BEAR.value: 0.0,
            Regime.CRISIS.value: 0.0,
        }
        if configured:
            for key, value in configured.items():
                if key in pcts:
                    pcts[key] = max(0.0, min(1.0, float(value)))
        return pcts

    def _target_count_for_regime(self, regime: str) -> int:
        """返回当前 regime 的目标持仓数。"""
        return int(
            self.regime_target_position_counts.get(
                regime,
                self.regime_target_position_counts[Regime.NEUTRAL.value],
            )
        )

    def _position_pct_for_regime(self, regime: str) -> float:
        """返回当前 regime 的资金预算上限。"""
        return float(
            self.regime_position_pcts.get(
                regime,
                self.regime_position_pcts[Regime.NEUTRAL.value],
            )
        )

    # ──────────────────────────────────────────────────────────────
    # 初始化
    # ──────────────────────────────────────────────────────────────

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        if not self._init_universe and self.universe_source == "liquidity_top":
            self._init_universe = self._load_liquidity_universe(context)
        self.set_universe(self._init_universe)
        self.set_benchmark(self.BENCHMARK_CODE)

        # ── 走步模式：加载注册表，模型按日动态切换 ──
        if self.model_registry_path:
            self._model_registry = ModelRegistry.from_json(self.model_registry_path)
            logger.info(
                f"走步模式启用，注册表含 {len(self._model_registry)} 个时点"
                f"（{self.model_registry_path}）"
            )
            # 不在 initialize 时预加载，等 handle_data 第一次按当日切换
            return

        # ── 传统模式：一次性加载单组模型 ──
        self._models = load_bundle(self.model_dir, horizons=BUY_HORIZONS)
        self._current_model_dir = self.model_dir
        all_missing = all(v is None for v in self._models.values())
        if all_missing:
            if self.use_deterministic_fallback:
                logger.warning(
                    f"全部模型缺失 ({self.model_dir})；启用 deterministic fallback。"
                )
            else:
                logger.error(
                    f"全部模型缺失 ({self.model_dir})；且禁用 fallback，"
                    f"策略将无任何信号产生。"
                )
        logger.info(
            f"MLMultiHorizon 初始化完成: universe={len(self._init_universe)}, "
            f"target_n={self.target_position_count}, model_dir={self.model_dir}"
        )

    def _load_liquidity_universe(self, context: Context) -> List[str]:
        """从数据源按回测首日前历史成交额初始化股票池。"""
        loader = getattr(context.data_source, "get_liquidity_top_equities", None)
        if not callable(loader):
            raise RuntimeError(
                "universe_source=liquidity_top 需要数据源实现 "
                "get_liquidity_top_equities(as_of_date, top_n, lookback_days)"
            )
        codes = loader(
            context.current_date,
            top_n=self.liquidity_top_n,
            lookback_days=self.liquidity_lookback_days,
            adjust="qfq",
        )
        if self.trading_permissions is not None:
            codes = filter_codes(codes, self.trading_permissions)
        if not codes:
            raise RuntimeError(
                f"{context.current_date} 未能初始化流动性股票池："
                f"top_n={self.liquidity_top_n}, lookback_days={self.liquidity_lookback_days}"
            )
        logger.info(
            f"流动性 universe 已初始化: {len(codes)} 只 "
            f"(top_n={self.liquidity_top_n}, lookback_days={self.liquidity_lookback_days})"
        )
        return codes

    def _maybe_switch_model(self, current_date: str) -> bool:
        """走步模式下检查并切换模型。返回 False 表示当前日无可用模型，调用方应跳过。"""
        if self._model_registry is None:
            return True  # 传统模式直接放行
        target_dir = self._model_registry.find_for_date(current_date)
        if target_dir is None:
            return False  # 当前日早于注册表第一个时点
        if target_dir != self._current_model_dir:
            self._models = load_bundle(target_dir, horizons=BUY_HORIZONS)
            self._current_model_dir = target_dir
            logger.info(f"{current_date} 切换到模型 {target_dir}")
        return True

    # ──────────────────────────────────────────────────────────────
    # 主回调
    # ──────────────────────────────────────────────────────────────

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        current_date = context.current_date
        portfolio = context.portfolio
        self._last_score_df = None
        self._last_regime = ""
        self._last_target_n = 0
        self._last_stop_loss_pct = 0.0
        self._last_position_pct = 0.0

        # 0. 走步模式：按当前日期切换模型（首次进入也走这条）
        if not self._maybe_switch_model(current_date):
            logger.warning(
                f"{current_date} 早于走步注册表起点，无可用模型，跳过"
            )
            return

        # 1. 全宇宙打分（buy 4 horizons + sell）。Regime 广度修正也依赖当日截面特征。
        score_df = self._score_universe(context)
        if score_df is None or score_df.empty:
            logger.warning(f"{current_date} 无有效特征，跳过本日")
            return
        self._last_score_df = score_df.copy()

        # 2. Regime 检测 + 市场广度修正，并转成可执行风险预算
        regime = self._detect_regime(context, score_df)
        target_n = self._target_count_for_regime(regime)
        position_pct = self._position_pct_for_regime(regime)
        stop_loss_pct = regime_stop_loss(
            regime, self.stop_loss_pct_bull, self.stop_loss_pct_bear
        )
        self._last_regime = regime
        self._last_target_n = target_n
        self._last_stop_loss_pct = stop_loss_pct
        self._last_position_pct = position_pct

        # 3. 更新所有持仓的 peak_price / state
        self._update_position_states(context, score_df, data)

        # 4. 计算 Top-N 与 Top-2N 名单。target_n=0 时不产生候选买入名单。
        score_sorted = score_df.sort_values("score", ascending=False).reset_index(drop=True)
        top_n_codes = score_sorted.head(max(target_n, 0))["code"].tolist()
        top_2n_codes = set(
            score_sorted.head(max(target_n, 1) * self.rank_buffer_multiplier)["code"].tolist()
        )

        # 5. 遍历现有持仓，检查 sell trigger
        sells = self._check_sell_triggers(
            context=context,
            score_df=score_df,
            top_2n_codes=top_2n_codes,
            stop_loss_pct=stop_loss_pct,
            data=data,
        )
        risk_budget_sells = self._risk_budget_sells(
            context=context,
            score_df=score_df,
            regime=regime,
            target_n=target_n,
            position_pct=position_pct,
            data=data,
            existing_sells=set(sells),
        )
        sells.update(risk_budget_sells)

        for code, reason in sells.items():
            pos = portfolio.get_position(code)
            if pos and pos.sellable_qty > 0:
                context.order(code, -pos.sellable_qty)
                logger.info(
                    f"{current_date} 卖出 {code} {pos.sellable_qty}股 ({reason})"
                )
                self._position_state.pop(code, None)

        if risk_budget_sells:
            logger.info(
                f"{current_date} regime={regime} 触发风险预算降仓，"
                f"target_n={target_n}, position_pct={position_pct:.0%}，当天不补仓"
            )
            return

        # 6. 零风险预算时不开新仓；bear 若配置了非零预算则允许补仓。
        if target_n <= 0 or position_pct <= 0:
            logger.info(
                f"{current_date} regime={regime}，零风险预算不开新仓 "
                f"(target_n={target_n}, position_pct={position_pct:.0%})"
            )
            return

        # 7. 补仓到 target_n
        held_codes = {c for c, p in portfolio.positions.items() if p.total_qty > 0}
        # 排除刚下单卖出的（虽然撮合在 T+1，但本日逻辑不再买入它们）
        held_codes -= set(sells.keys())
        slots = target_n - len(held_codes)
        if slots <= 0:
            return

        # 候选：未持仓 + prob_up 达标的 Top-N
        candidates = [
            c for c in top_n_codes
            if c not in held_codes
        ]
        # prob_up 达标过滤
        prob_lookup = score_df.set_index("code")
        candidates = [
            c for c in candidates
            if c in prob_lookup.index
            and float(prob_lookup.loc[c, "prob_up_h5"]) >= self.min_buy_prob
        ]
        to_buy = candidates[:slots]

        if not to_buy:
            return

        # 等权分配
        total_value = portfolio.total_value(
            {c: float(data[c]["close"]) for c in held_codes if c in data}
        )
        if target_n <= 0 or position_pct <= 0:
            return
        target_value_per_stock = total_value * position_pct / target_n

        for code in to_buy:
            if code not in data:
                continue
            price = float(data[code]["close"])
            if price <= 0:
                continue
            target_qty = int((target_value_per_stock / price) // 100) * 100
            if target_qty <= 0:
                continue
            required = target_qty * price
            if portfolio.available_cash < required:
                affordable = int((portfolio.available_cash / price) // 100) * 100
                if affordable <= 0:
                    continue
                target_qty = affordable
            context.order(code, target_qty)
            logger.info(
                f"{current_date} 买入 {code} {target_qty}股 "
                f"(prob_h5={float(prob_lookup.loc[code, 'prob_up_h5']):.3f})"
            )
            # 记录建仓状态
            expected_horizon = self._argmax_horizon(prob_lookup.loc[code])
            self._position_state[code] = PositionState(
                entered_date=current_date,
                expected_horizon=expected_horizon,
                peak_price=price,
                rank_dropout_streak=0,
            )

    # ──────────────────────────────────────────────────────────────
    # Helpers: regime
    # ──────────────────────────────────────────────────────────────

    def _detect_regime(
        self,
        context: Context,
        score_df: Optional[pd.DataFrame] = None,
    ) -> str:
        try:
            hist = context.get_price(self.BENCHMARK_CODE, count=260)
            if hist is None or hist.empty or "close" not in hist.columns:
                return Regime.NEUTRAL.value
            close = pd.to_numeric(hist["close"], errors="coerce").astype(float)
            base_regime = latest_regime(close)
            if score_df is None or score_df.empty:
                return base_regime
            regime = combine_regime_with_market_breadth(
                base_regime,
                score_df,
                breadth_bear_threshold=self.breadth_bear_threshold,
                breadth_recovery_threshold=self.breadth_recovery_threshold,
            )
            if self.crisis_enabled and is_crisis_regime(
                close,
                score_df,
                drawdown_threshold=self.crisis_drawdown_threshold,
                fast_drop_threshold=self.crisis_fast_drop_threshold,
                breadth_threshold=self.crisis_breadth_threshold,
                selloff_share_threshold=self.crisis_selloff_share_threshold,
            ):
                if regime != Regime.CRISIS.value:
                    metrics = market_breadth_metrics(score_df)
                    logger.info(
                        f"{context.current_date} regime 由 {regime} 修正为 crisis "
                        f"(crisis_metrics={metrics})"
                    )
                return Regime.CRISIS.value
            if regime != base_regime:
                metrics = market_breadth_metrics(score_df)
                logger.info(
                    f"{context.current_date} regime 由 {base_regime} 修正为 {regime} "
                    f"(breadth={metrics})"
                )
            return regime
        except Exception as exc:
            logger.warning(f"regime 检测失败，默认 neutral: {exc}")
            return Regime.NEUTRAL.value

    # ──────────────────────────────────────────────────────────────
    # Helpers: scoring
    # ──────────────────────────────────────────────────────────────

    def _score_universe(self, context: Context) -> Optional[pd.DataFrame]:
        """对宇宙内每只股票计算 4 个 buy prob + 1 个 sell prob。

        返回 DataFrame：[code, prob_up_h1, prob_up_h5, prob_up_h10, prob_up_h20,
                         prob_sell, score]
        score = max(prob_up_h5, prob_up_h10, prob_up_h20)
        """
        buy_rows: List[Dict[str, Any]] = []
        sell_rows: List[Dict[str, Any]] = []

        # 多取一些以容纳 sell-side 60d 窗口（rolling 内部 min_periods=1 降级）
        request_count = max(self.feature_window + 10, 70)
        for code in self._universe:
            try:
                hist = context.get_price(code, count=request_count)
            except Exception:
                continue
            # 至少 feature_window 天才能算出 17 维买入特征
            if hist is None or len(hist) < self.feature_window:
                continue

            # BigQuery NUMERIC → decimal.Decimal，np.log 不支持；强转 float
            hist = hist.copy()
            for c in ("open", "high", "low", "close", "volume", "amount"):
                if c in hist.columns:
                    hist[c] = pd.to_numeric(hist[c], errors="coerce").astype(float)

            # 17 维基础特征
            feat = self._base_feature_engineer.compute_features(hist)
            if feat.empty or any(c not in feat.columns for c in BUY_FEATURE_COLUMNS):
                continue
            latest_buy = feat.iloc[-1:][BUY_FEATURE_COLUMNS]
            if latest_buy.isnull().any().any():
                continue

            # 5 维 sell-side 风险特征
            full = compute_sell_risk_features(feat)
            if any(c not in full.columns for c in SELL_FEATURE_COLUMNS):
                continue
            latest_sell = full.iloc[-1:][SELL_FEATURE_COLUMNS].fillna(0)

            buy_rows.append({"code": code, **latest_buy.iloc[0].to_dict()})
            sell_rows.append({"code": code, **latest_sell.iloc[0].to_dict()})

        if not buy_rows:
            return None

        buy_df = pd.DataFrame(buy_rows)
        sell_df = pd.DataFrame(sell_rows)

        # 预测 4 buy horizon
        for h in BUY_HORIZONS:
            col = f"prob_up_h{h}"
            model = self._models.get(f"buy_h{h}")
            if model is not None:
                try:
                    X = buy_df[BUY_FEATURE_COLUMNS].values
                    pred = model.predict(X)
                    buy_df[col] = self._squash_to_prob(pred)
                except Exception as exc:
                    logger.warning(f"buy_h{h} 预测失败，降级 fallback: {exc}")
                    buy_df[col] = self._squash_to_prob(
                        deterministic_score(buy_df[BUY_FEATURE_COLUMNS])
                    )
            elif self.use_deterministic_fallback:
                buy_df[col] = self._squash_to_prob(
                    deterministic_score(buy_df[BUY_FEATURE_COLUMNS])
                )
            else:
                buy_df[col] = 0.0

        # 预测 sell model
        sell_model = self._models.get(SELL_MODEL_NAME)
        if sell_model is not None:
            try:
                X = sell_df[SELL_FEATURE_COLUMNS].values
                buy_df["prob_sell"] = self._squash_to_prob(sell_model.predict(X))
            except Exception as exc:
                logger.warning(f"sell 预测失败，降级 fallback: {exc}")
                buy_df["prob_sell"] = deterministic_sell_score(
                    sell_df[SELL_FEATURE_COLUMNS]
                )
        else:
            buy_df["prob_sell"] = deterministic_sell_score(
                sell_df[SELL_FEATURE_COLUMNS]
            )

        # score = max(h5, h10, h20)：跳 h1 噪音太大
        buy_df["score"] = buy_df[["prob_up_h5", "prob_up_h10", "prob_up_h20"]].max(axis=1)
        return buy_df

    @staticmethod
    def _squash_to_prob(arr) -> np.ndarray:
        """把任意预测值挤压到 [0, 1] 概率空间。

        - 若所有值已在 [0, 1]，直接返回（LightGBM binary 输出）
        - 否则用 sigmoid
        """
        arr = np.asarray(arr, dtype=float)
        if np.all((arr >= 0) & (arr <= 1)):
            return arr
        return 1 / (1 + np.exp(-arr))

    @staticmethod
    def _argmax_horizon(row: pd.Series) -> int:
        """返回 argmax_h h × prob_up_h（h ∈ {5, 10, 20}）。"""
        scores = {
            5: 5 * float(row.get("prob_up_h5", 0)),
            10: 10 * float(row.get("prob_up_h10", 0)),
            20: 20 * float(row.get("prob_up_h20", 0)),
        }
        return max(scores, key=scores.get)

    # ──────────────────────────────────────────────────────────────
    # Helpers: position state
    # ──────────────────────────────────────────────────────────────

    @staticmethod
    def _normalize_trade_date(value: Any) -> str:
        digits = "".join(ch for ch in str(value)[:10] if ch.isdigit())
        return digits[:8] if len(digits) >= 8 else ""

    @classmethod
    def _build_trading_date_index(cls, values: Any) -> Dict[str, int]:
        dates = sorted({d for d in (cls._normalize_trade_date(v) for v in values) if d})
        return {date: idx for idx, date in enumerate(dates)}

    def _ensure_trading_date_index(self, context: Optional[Context] = None) -> Dict[str, int]:
        if self._trading_date_index:
            return self._trading_date_index

        all_bars = getattr(context, "all_bars", None) if context is not None else None
        if all_bars:
            dates: List[Any] = []
            for frame in all_bars.values():
                if frame is not None and not frame.empty and "date" in frame.columns:
                    dates.extend(frame["date"].tolist())
            self._trading_date_index = self._build_trading_date_index(dates)
        return self._trading_date_index

    def _trading_day_diff(
        self,
        start: str,
        end: str,
        context: Optional[Context] = None,
    ) -> int:
        """返回交易日差：买入成交日到当前交易日经过了几个交易日。"""
        start_key = self._normalize_trade_date(start)
        end_key = self._normalize_trade_date(end)
        if not start_key or not end_key or end_key <= start_key:
            return 0

        index = self._ensure_trading_date_index(context)
        if start_key in index and end_key in index:
            return max(0, index[end_key] - index[start_key])
        dates = sorted(index)
        if dates:
            return sum(1 for date in dates if start_key < date <= end_key)

        return max(0, self._date_diff(start_key, end_key))

    def _actual_position_entry_date(self, pos: Any) -> str:
        records = getattr(pos, "_buy_records", {}) or {}
        dates = [
            self._normalize_trade_date(date)
            for date, qty in records.items()
            if qty and qty > 0
        ]
        dates = [date for date in dates if date]
        return min(dates) if dates else ""

    def _holding_trade_days(
        self,
        context: Context,
        state: Optional[PositionState],
        pos: Any,
    ) -> int:
        entry_date = self._actual_position_entry_date(pos)
        if not entry_date and state is not None:
            entry_date = self._normalize_trade_date(getattr(state, "entered_date", ""))
        return self._trading_day_diff(entry_date, context.current_date, context)

    def _update_position_states(
        self,
        context: Context,
        score_df: pd.DataFrame,
        data: Dict[str, pd.Series],
    ) -> None:
        portfolio = context.portfolio
        held_codes = {c for c, p in portfolio.positions.items() if p.total_qty > 0}

        # 清理已不在持仓的状态
        for code in list(self._position_state.keys()):
            if code not in held_codes:
                self._position_state.pop(code, None)

        # 更新 peak_price
        for code in held_codes:
            if code not in self._position_state:
                # 历史上某种原因 state 缺失（如断点续跑）。补一个保守 state
                pos = portfolio.get_position(code)
                price = float(data[code]["close"]) if code in data else (pos.cost_price if pos else 0)
                self._position_state[code] = PositionState(
                    entered_date=context.current_date,
                    expected_horizon=5,
                    peak_price=price,
                    rank_dropout_streak=0,
                )
                continue
            if code in data:
                price = float(data[code]["close"])
                state = self._position_state[code]
                if price > state.peak_price:
                    state.peak_price = price

        # 订单在 T 日信号生成、T+1 开盘成交。state 初始写入的是信号日，
        # 这里用 Portfolio 真实成交记录修正为买入成交日，持仓天数才是交易日口径。
        for code, state in list(self._position_state.items()):
            pos = portfolio.get_position(code)
            if pos is None or pos.total_qty <= 0:
                continue
            actual_entry = self._actual_position_entry_date(pos)
            if not actual_entry:
                continue
            if self._normalize_trade_date(state.entered_date) == actual_entry:
                continue
            state.entered_date = actual_entry
            if code in data:
                price = float(data[code]["close"])
                cost = float(getattr(pos, "cost_price", 0.0) or 0.0)
                state.peak_price = max(cost, price)

    def _check_sell_triggers(
        self,
        context: Context,
        score_df: pd.DataFrame,
        top_2n_codes: set,
        stop_loss_pct: float,
        data: Dict[str, pd.Series],
    ) -> Dict[str, str]:
        """逐持仓检查 sell trigger，返回 {code: reason}。"""
        portfolio = context.portfolio
        prob_lookup = score_df.set_index("code")

        sells: Dict[str, str] = {}
        for code, pos in portfolio.positions.items():
            if pos.total_qty <= 0:
                continue
            if pos.sellable_qty <= 0:
                # T+1 当日买入不可卖
                continue

            state = self._position_state.get(code)
            if code not in data:
                continue
            price = float(data[code]["close"])
            cost = float(pos.cost_price)

            # a. 硬止损
            if cost > 0 and price < cost * (1 - stop_loss_pct):
                sells[code] = f"stop_loss(-{stop_loss_pct:.0%})"
                continue

            # b. 追踪止盈
            if state is not None and state.peak_price > 0:
                if price < state.peak_price * (1 - self.trailing_stop_pct) and price > cost:
                    sells[code] = (
                        f"trailing_stop(peak={state.peak_price:.2f},"
                        f"now={price:.2f})"
                    )
                    continue

            holding_days = self._holding_trade_days(context, state, pos)

            # c. 最大持仓交易日兜底。decision_horizon/h5 不再作为硬卖出条件。
            if self.max_hold_days is not None and holding_days >= self.max_hold_days:
                sells[code] = f"max_hold_expired({holding_days}d>={self.max_hold_days}d)"
                continue

            # d. 排名迟滞
            if state is not None:
                if code not in top_2n_codes:
                    state.rank_dropout_streak += 1
                else:
                    state.rank_dropout_streak = 0
                if (
                    holding_days >= self.min_hold_days
                    and state.rank_dropout_streak >= self.dropout_persistence_days
                ):
                    sells[code] = (
                        f"rank_dropout(streak={state.rank_dropout_streak},"
                        f"held={holding_days}d)"
                    )
                    continue

            # e. prob_up 兜底
            if code in prob_lookup.index:
                prob_h5 = float(prob_lookup.loc[code, "prob_up_h5"])
                if state is not None:
                    if (
                        prob_h5 < self.min_prob_floor
                        and holding_days >= self.min_hold_days
                    ):
                        sells[code] = f"prob_floor(prob_h5={prob_h5:.3f})"
                        continue

                # f. 卖出模型
                prob_sell = float(prob_lookup.loc[code, "prob_sell"])
                if holding_days >= self.min_hold_days and prob_sell > self.sell_threshold:
                    sells[code] = f"sell_model(prob_sell={prob_sell:.3f})"
                    continue

        return sells

    def _risk_budget_sells(
        self,
        context: Context,
        score_df: pd.DataFrame,
        regime: str,
        target_n: int,
        position_pct: float,
        data: Dict[str, pd.Series],
        existing_sells: set,
    ) -> Dict[str, str]:
        """根据 regime 风险预算生成额外降仓卖单。

        风险预算不绕过 T+1：只卖 `sellable_qty > 0` 的持仓。
        """
        portfolio = context.portfolio
        score_lookup = score_df.set_index("code")["score"] if "score" in score_df else pd.Series(dtype=float)

        def score_for(code: str) -> float:
            try:
                return float(score_lookup.loc[code])
            except Exception:
                return float("-inf")

        sells: Dict[str, str] = {}
        held_codes = [
            code
            for code, pos in portfolio.positions.items()
            if pos.total_qty > 0 and code not in existing_sells
        ]

        if regime == Regime.BEAR.value and self.bear_clear_existing:
            for code in held_codes:
                pos = portfolio.get_position(code)
                if pos and pos.sellable_qty > 0:
                    sells[code] = "bear_risk_budget_clear"
            return sells

        remaining = [code for code in held_codes if code not in sells]
        if target_n <= 0:
            for code in remaining:
                pos = portfolio.get_position(code)
                if pos and pos.sellable_qty > 0:
                    sells[code] = f"risk_budget_target_zero(regime={regime})"
            return sells

        if len(remaining) > target_n:
            excess = len(remaining) - target_n
            for code in sorted(remaining, key=score_for)[:excess]:
                pos = portfolio.get_position(code)
                if pos and pos.sellable_qty > 0:
                    sells[code] = f"risk_budget_count(regime={regime},target_n={target_n})"

        price_map = {
            code: float(row["close"])
            for code, row in data.items()
            if code in portfolio.positions and "close" in row
        }
        total_value = portfolio.total_value(price_map)
        allowed_value = total_value * max(0.0, min(1.0, position_pct))
        remaining_value = 0.0
        for code in remaining:
            if code in sells or code not in price_map:
                continue
            pos = portfolio.get_position(code)
            if pos:
                remaining_value += pos.total_qty * price_map[code]

        if remaining_value > allowed_value:
            for code in sorted(remaining, key=score_for):
                if code in sells or code not in price_map:
                    continue
                pos = portfolio.get_position(code)
                if not pos or pos.sellable_qty <= 0:
                    continue
                sells[code] = (
                    f"risk_budget_value(regime={regime},budget={position_pct:.0%})"
                )
                remaining_value -= pos.total_qty * price_map[code]
                if remaining_value <= allowed_value:
                    break

        return sells

    @staticmethod
    def _date_diff(start: str, end: str) -> int:
        """日期字符串差（天数）。容错 YYYYMMDD 或 YYYY-MM-DD。"""
        for fmt in ("%Y%m%d", "%Y-%m-%d"):
            try:
                s = pd.to_datetime(start, format=fmt)
                e = pd.to_datetime(end, format=fmt)
                return int((e - s).days)
            except (ValueError, TypeError):
                continue
        # 终极兜底
        try:
            return int((pd.to_datetime(end) - pd.to_datetime(start)).days)
        except Exception:
            return 0
