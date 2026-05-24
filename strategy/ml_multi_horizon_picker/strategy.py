"""多 Horizon 机器学习择时择股策略。

设计要点（详见 PRD_20260524_05）：
- 4 个独立 LightGBM 买入模型（horizon 1/5/10/20 天）
- 1 个独立 LightGBM 卖出风险模型
- 6 个卖出触发（任一命中即卖）：
    a. 硬止损（regime 调制）
    b. 追踪止盈（从持仓期高点回撤）
    c. Horizon 到期（建仓时锁定的预期持有期）
    d. 排名迟滞（连续 N 天不在 Top-2K）
    e. prob_up 兜底（低于 floor 阈值）
    f. 卖出模型（prob_sell > threshold）
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
from strategy.ml_multi_horizon_picker.model_storage import (
    BUY_HORIZONS,
    SELL_MODEL_NAME,
    load_bundle,
)
from strategy.ml_multi_horizon_picker.regime import (
    Regime,
    latest_regime,
    regime_position_multiplier,
    regime_stop_loss,
)
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PositionState:
    """每个持仓的额外状态簿记（除 Portfolio.Position 之外）。"""

    entered_date: str
    expected_horizon: int  # 建仓时锁定的 horizon 天数
    peak_price: float       # 持仓期间最高收盘价
    rank_dropout_streak: int = 0  # 连续不在 Top-2K 的天数


class MLMultiHorizonStrategy(BaseStrategy):
    """多 Horizon ML 择时择股策略。"""

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
        universe: Optional[List[str]] = None,
        target_position_count: int = 10,
        min_buy_prob: float = 0.50,
        min_prob_floor: float = 0.30,
        sell_threshold: float = 0.70,
        stop_loss_pct_bull: float = 0.05,
        stop_loss_pct_bear: float = 0.03,
        trailing_stop_pct: float = 0.03,
        min_hold_days: int = 3,
        dropout_persistence_days: int = 2,
        rank_buffer_multiplier: int = 2,
        use_deterministic_fallback: bool = True,
        position_pct: float = 0.95,
        feature_window: int = 20,    # 与 BaseFeatureEngineer 默认一致；sell-side 60d 特征用 min_periods=1 降级
    ) -> None:
        super().__init__()
        self.model_dir = model_dir
        self._init_universe = list(universe) if universe else list(self.DEFAULT_UNIVERSE)
        self.target_position_count = target_position_count
        self.min_buy_prob = min_buy_prob
        self.min_prob_floor = min_prob_floor
        self.sell_threshold = sell_threshold
        self.stop_loss_pct_bull = stop_loss_pct_bull
        self.stop_loss_pct_bear = stop_loss_pct_bear
        self.trailing_stop_pct = trailing_stop_pct
        self.min_hold_days = min_hold_days
        self.dropout_persistence_days = dropout_persistence_days
        self.rank_buffer_multiplier = rank_buffer_multiplier
        self.use_deterministic_fallback = use_deterministic_fallback
        self.position_pct = position_pct
        self.feature_window = feature_window
        # 回测时引擎会预加载 feature_window 天的 warmup
        self.lookback_days = feature_window + 5

        self._base_feature_engineer = BaseFeatureEngineer(
            feature_window=20, label_horizon=5
        )
        self._models: Dict[str, Optional[Any]] = {}
        self._position_state: Dict[str, PositionState] = {}

    # ──────────────────────────────────────────────────────────────
    # 初始化
    # ──────────────────────────────────────────────────────────────

    def initialize(self, context: Context) -> None:
        super().initialize(context)
        self.set_universe(self._init_universe)
        self.set_benchmark(self.BENCHMARK_CODE)

        self._models = load_bundle(self.model_dir, horizons=BUY_HORIZONS)
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

    # ──────────────────────────────────────────────────────────────
    # 主回调
    # ──────────────────────────────────────────────────────────────

    def handle_data(self, context: Context, data: Dict[str, pd.Series]) -> None:
        current_date = context.current_date
        portfolio = context.portfolio

        # 1. Regime 检测
        regime = self._detect_regime(context)
        target_n = max(
            1, int(round(self.target_position_count * regime_position_multiplier(regime)))
        )
        stop_loss_pct = regime_stop_loss(
            regime, self.stop_loss_pct_bull, self.stop_loss_pct_bear
        )

        # 2. 全宇宙打分（buy 4 horizons + sell）
        score_df = self._score_universe(context)
        if score_df is None or score_df.empty:
            logger.warning(f"{current_date} 无有效特征，跳过本日")
            return

        # 3. 更新所有持仓的 peak_price / state
        self._update_position_states(context, score_df, data)

        # 4. 计算 Top-N 与 Top-2N 名单
        score_sorted = score_df.sort_values("score", ascending=False).reset_index(drop=True)
        top_n_codes = score_sorted.head(target_n)["code"].tolist()
        top_2n_codes = set(
            score_sorted.head(target_n * self.rank_buffer_multiplier)["code"].tolist()
        )

        # 5. 遍历现有持仓，检查 6 个 sell trigger
        sells = self._check_sell_triggers(
            context=context,
            score_df=score_df,
            top_2n_codes=top_2n_codes,
            stop_loss_pct=stop_loss_pct,
            data=data,
        )

        for code, reason in sells.items():
            pos = portfolio.get_position(code)
            if pos and pos.sellable_qty > 0:
                context.order(code, -pos.sellable_qty)
                logger.info(
                    f"{current_date} 卖出 {code} {pos.sellable_qty}股 ({reason})"
                )
                self._position_state.pop(code, None)

        # 6. Bear regime 不开新仓
        if regime == Regime.BEAR.value:
            logger.info(f"{current_date} regime=bear，不开新仓 (target_n={target_n})")
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
        target_value_per_stock = total_value * self.position_pct / target_n

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

    def _detect_regime(self, context: Context) -> str:
        try:
            hist = context.get_price(self.BENCHMARK_CODE, count=260)
            if hist is None or hist.empty or "close" not in hist.columns:
                return Regime.NEUTRAL.value
            close = pd.to_numeric(hist["close"], errors="coerce").astype(float)
            return latest_regime(close)
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

    def _check_sell_triggers(
        self,
        context: Context,
        score_df: pd.DataFrame,
        top_2n_codes: set,
        stop_loss_pct: float,
        data: Dict[str, pd.Series],
    ) -> Dict[str, str]:
        """逐持仓检查 6 个 sell trigger，返回 {code: reason}。"""
        portfolio = context.portfolio
        current_date = context.current_date
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

            # c. Horizon 到期
            if state is not None:
                holding_days = self._date_diff(state.entered_date, current_date)
                if holding_days >= state.expected_horizon:
                    sells[code] = f"horizon_expired({holding_days}d>={state.expected_horizon}d)"
                    continue

            # d. 排名迟滞
            if state is not None:
                holding_days = self._date_diff(state.entered_date, current_date)
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
                    holding_days = self._date_diff(state.entered_date, current_date)
                    if (
                        prob_h5 < self.min_prob_floor
                        and holding_days >= self.min_hold_days
                    ):
                        sells[code] = f"prob_floor(prob_h5={prob_h5:.3f})"
                        continue

                # f. 卖出模型
                prob_sell = float(prob_lookup.loc[code, "prob_sell"])
                if prob_sell > self.sell_threshold:
                    sells[code] = f"sell_model(prob_sell={prob_sell:.3f})"
                    continue

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
