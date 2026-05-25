"""MLRichPickerStrategy 主策略（PRD_20260525_03）。

继承 ml_multi_horizon_picker.MLMultiHorizonStrategy，覆盖：

- 特征加载：initialize 时预拉取整个回测期 + warmup 的 rich features 宽表，
  按 (date, equity_code) 建索引；handle_data 时 O(1) 查表
- 推理特征列：30 维 buy / 35 维 sell（回归 ``optimal_remaining_days``，不含
  持仓状态）

其余逻辑（regime / 走步切换 / trading_permissions 过滤）继承自父类；
sell trigger 使用当前持仓决策口径：不做 h5 到期硬卖，仅用更长
max_hold_days 作为异常兜底。

Sell 模型语义说明（修法 3）：
- 模型输出 ``predicted_remaining_days`` ∈ [0, sell_lookforward]，表示
  「从今天起还应该持有多少个交易日（风险可控的最优卖出窗口）」
- 父类 sell trigger 仍然用 ``prob_sell > sell_threshold`` 判断，rich 这里
  把 ``predicted_remaining_days`` 通过 sigmoid 桥接到 ``prob_sell``，
  保留父类逻辑无需改动
"""

from __future__ import annotations

from typing import Dict, List, Optional

import numpy as np
import pandas as pd

from strategy.base_strategy import Context
from strategy.ml_multi_horizon_picker.strategy import MLMultiHorizonStrategy
from strategy.ml_multi_horizon_picker.model_registry import ModelRegistry
from strategy.ml_multi_horizon_picker.model_storage import BUY_HORIZONS
from strategy.ml_multi_horizon_picker.tradable import filter_codes

from strategy.ml_rich_picker.features import (
    DAILY_FEATURE_COLUMNS,
    DEFAULT_MAX_REMAINING_DAYS,
    EVENT_FEATURE_COLUMNS,
    FUNDAMENTAL_FEATURE_COLUMNS,
    POSITION_STATE_FEATURE_COLUMNS,
    RICH_BUY_FEATURE_COLUMNS,
    RICH_SELL_FEATURE_COLUMNS,
    SELL_REGRESSION_FEATURE_COLUMNS,
    deterministic_optimal_remaining_days,
    deterministic_rich_score,
    remaining_days_to_prob_sell,
)
from strategy.ml_rich_picker.model_storage import (
    SELL_REMAINING_DAYS_MODEL_NAME,
    load_rich_bundle,
)
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

    3. **Sell 模型语义**：从父类的 binary ``prob_sell`` 改为回归
       ``predicted_remaining_days``——预测未来若干天内风险可控的最优
       卖出剩余天数。父类 sell trigger 继续用 ``prob_sell > threshold``，
       rich 这里把 ``predicted_remaining_days`` 通过 sigmoid 桥接到
       ``prob_sell``，无需改父类逻辑。持仓状态特征不进入 sell 模型，
       只在策略层 trigger 时作为辅助判断。

    其余（regime / 走步切换）复用父类；sell trigger 使用当前持仓决策口径，
    不再按 h5/expected_horizon 到期硬卖。
    """

    # 类常量
    RICH_BUY_FEATURE_COLUMNS = RICH_BUY_FEATURE_COLUMNS
    RICH_SELL_FEATURE_COLUMNS = RICH_SELL_FEATURE_COLUMNS
    SELL_REGRESSION_FEATURE_COLUMNS = SELL_REGRESSION_FEATURE_COLUMNS

    def __init__(
        self,
        *args,
        bq_project: str = "data-aquarium",
        bq_dataset: str = "ashare",
        bq_location: str = "asia-east2",
        adjust_type: str = "qfq",
        require_rich_features: bool = True,
        decision_horizon: int = 5,
        max_hold_days: Optional[int] = 20,
        sell_remaining_days_threshold: float = 1.0,
        sell_remaining_days_sharpness: float = 1.5,
        sell_max_remaining_days: float = DEFAULT_MAX_REMAINING_DAYS,
        # Position-aware sell trigger 参数（A 路线后半段）：
        # sell 回归模型只看市场未来，浮盈兑现 / 浮亏割肉这两个核心持仓决策
        # 由策略层 trigger 用 position_return + holding_days 真实合成。
        # 注意：父类 stop_loss 已经在浮亏 5%(bull)/3%(bear) 时硬止损；
        # stale_loss 必须用**比 stop_loss 浅**的阈值，专门捕"温水煮青蛙"的
        # 小幅浮亏长拖场景——否则会被父类 stop_loss 完全盖死、成为死代码。
        profit_take_return_threshold: float = 0.20,   # 浮盈 ≥ 20%
        profit_take_prob_ceiling: float = 0.45,        # 且 prob_up_h5 < 0.45 → 止盈
        stale_loss_min_days: int = 8,                  # 持仓 ≥ 8 个交易日
        stale_loss_return_threshold: float = -0.02,    # 且浮亏在 (-2%, stop_loss) → 拖久无起色，认输
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
        self.decision_horizon = int(decision_horizon)
        if self.decision_horizon not in BUY_HORIZONS:
            raise ValueError(f"decision_horizon 必须在 {BUY_HORIZONS} 内")
        self.max_hold_days = (
            int(max_hold_days)
            if max_hold_days is not None and int(max_hold_days) > 0
            else None
        )
        # Sell 回归模型 → prob_sell 桥接参数
        # predicted_remaining_days <= sell_remaining_days_threshold 视为应卖
        # sharpness 越大越接近硬阈值（默认 1.5 适中平滑）
        self.sell_remaining_days_threshold = float(sell_remaining_days_threshold)
        self.sell_remaining_days_sharpness = float(sell_remaining_days_sharpness)
        self.sell_max_remaining_days = float(sell_max_remaining_days)
        # Position-aware trigger 阈值（参考 v1 的 stop_loss / trailing_stop，
        # 但这里专门针对"浮盈兑现"和"长期被套"两个 v1 触发器未覆盖的场景）
        self.profit_take_return_threshold = float(profit_take_return_threshold)
        self.profit_take_prob_ceiling = float(profit_take_prob_ceiling)
        self.stale_loss_min_days = int(stale_loss_min_days)
        self.stale_loss_return_threshold = float(stale_loss_return_threshold)
        # 预加载的富特征宽表，indexed by (date, equity_code)
        self._rich_features: Optional[pd.DataFrame] = None
        self._rich_features_index: Optional[pd.MultiIndex] = None
        self._rich_amount_features: Optional[pd.DataFrame] = None
        self._trading_date_index: Dict[str, int] = {}
        # before_trading_start 的"第一次"标记（懒加载）
        self._rich_preloaded: bool = False

    # ──────────────────────────────────────────────────────────────
    # 初始化：注意 initialize 阶段不能取到 all_bars / engine.end_date
    # （PRD §10.x 修订：BacktestEngine 是先 initialize 再 _preload_bars
    # 再 context.all_bars=all_bars。所以预加载推迟到 before_trading_start。）
    # ──────────────────────────────────────────────────────────────

    def initialize(self, context: Context) -> None:
        """父类初始化。Rich features 预加载推迟到 before_trading_start。

        覆盖父类的传统模型加载：用 ``load_rich_bundle`` 拿新的 sell 回归模型
        （文件名 ``sell_remaining_days_v1.pkl``），而不是父类的 binary
        ``sell_v1.pkl``。
        """
        super().initialize(context)
        # 父类 initialize 在 _model_registry_path 为 None 时已经走 load_bundle 加载
        # 了一组父类的 binary sell_v1.pkl。这对 rich 不适用——rich 用回归模型，
        # 重新覆盖加载结果。
        if self._model_registry is None and self.model_dir:
            try:
                self._models = load_rich_bundle(self.model_dir, horizons=BUY_HORIZONS)
                self._current_model_dir = self.model_dir
                logger.info(f"rich 模型加载完成: {self.model_dir}")
            except Exception as exc:
                logger.warning(f"rich 模型加载失败: {exc}（继续初始化，运行时再判断）")

        if self.universe_source == "liquidity_top" and not self._explicit_universe:
            broad_universe = self._load_broad_trading_universe(context)
            if broad_universe:
                self._init_universe = broad_universe
                self.set_universe(broad_universe)
                logger.info(
                    f"rich 动态 universe 启用：预加载可交易全市场 {len(broad_universe)} 只，"
                    f"每日按近 {self.liquidity_lookback_days} 日成交额取 Top{self.liquidity_top_n}"
                )
        # 不在这里调 _preload_rich_features —— 此时 context.all_bars 未注入

    def _maybe_switch_model(self, current_date: str) -> bool:
        """覆盖父类：走步模式下用 ``load_rich_bundle`` 加载 rich 模型。"""
        if self._model_registry is None:
            return True  # 传统模式（已在 initialize 加载）直接放行
        target_dir = self._model_registry.find_for_date(current_date)
        if target_dir is None:
            return False
        if target_dir != self._current_model_dir:
            self._models = load_rich_bundle(target_dir, horizons=BUY_HORIZONS)
            self._current_model_dir = target_dir
            logger.info(f"{current_date} 切换到 rich 模型 {target_dir}")
        return True

    def _load_broad_trading_universe(self, context: Context) -> List[str]:
        """返回预加载用宽股票池；实际交易池在每日打分时动态收敛。"""
        loader = getattr(context.data_source, "get_stock_list", None)
        if not callable(loader):
            return []
        try:
            try:
                stock_df = loader(include_inactive=True)
            except TypeError:
                stock_df = loader()
        except Exception as exc:
            logger.warning(f"加载全市场股票池失败，保留父类初始 universe: {exc}")
            return []
        if stock_df is None or stock_df.empty:
            return []
        code_col = "code" if "code" in stock_df.columns else "equity_code"
        if code_col not in stock_df.columns:
            return []
        codes = stock_df[code_col].dropna().astype(str).unique().tolist()
        if self.trading_permissions is not None:
            codes = filter_codes(codes, self.trading_permissions)
        return sorted(codes)

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
            msg = "rich features 加载为空"
            logger.warning(msg)
            self._rich_features = None
            if self.require_rich_features:
                raise RuntimeError(msg)
            return

        # 计算 sell-side 5 维风险特征
        from strategy.ml_multi_horizon_picker.walk_forward import _enrich_sell_features_grouped
        df = _enrich_sell_features_grouped(df)

        # 建索引
        df = df.sort_values(["date", "equity_code"]).reset_index(drop=True)
        df = df.set_index(["date", "equity_code"])
        self._rich_features = df
        self._rich_amount_features = (
            df[["amount"]].copy()
            if "amount" in df.columns
            else None
        )
        self._trading_date_index = self._build_trading_date_index(
            df.index.get_level_values("date")
        )
        logger.info(f"rich features 预加载完成: {len(df):,} 行")

    def _ensure_trading_date_index(self, context: Optional[Context] = None) -> Dict[str, int]:
        if self._trading_date_index:
            return self._trading_date_index

        if self._rich_features is not None and not self._rich_features.empty:
            if isinstance(self._rich_features.index, pd.MultiIndex) and "date" in self._rich_features.index.names:
                self._trading_date_index = self._build_trading_date_index(
                    self._rich_features.index.get_level_values("date")
                )
                return self._trading_date_index
            if "date" in self._rich_features.columns:
                self._trading_date_index = self._build_trading_date_index(
                    self._rich_features["date"]
                )
                return self._trading_date_index

        return super()._ensure_trading_date_index(context)

    def _rich_amount_history(self) -> Optional[pd.DataFrame]:
        if self._rich_amount_features is not None and not self._rich_amount_features.empty:
            return self._rich_amount_features
        if (
            self._rich_features is None
            or self._rich_features.empty
            or "amount" not in self._rich_features.columns
        ):
            return None
        if not isinstance(self._rich_features.index, pd.MultiIndex):
            return None
        self._rich_amount_features = self._rich_features[["amount"]].copy()
        return self._rich_amount_features

    def _held_codes(self, context: Optional[Context]) -> List[str]:
        portfolio = getattr(context, "portfolio", None)
        if portfolio is None:
            return []
        return [
            str(code)
            for code, pos in getattr(portfolio, "positions", {}).items()
            if getattr(pos, "total_qty", 0) > 0
        ]

    def _dynamic_universe_for_date(
        self,
        today_df: pd.DataFrame,
        current_date: str,
        context: Optional[Context] = None,
    ) -> List[str]:
        """按当前信号日可见数据动态选近 N 日成交额 Top 股票池。"""
        if self._explicit_universe or self.universe_source != "liquidity_top":
            return list(self._universe)
        if self._rich_features is None or self._rich_features.empty:
            return list(self._universe)

        lookback_start = (
            pd.to_datetime(current_date) - pd.Timedelta(days=self.liquidity_lookback_days * 2)
        ).strftime("%Y%m%d")
        amount_hist = self._rich_amount_history()
        recent = pd.DataFrame()
        if amount_hist is not None and isinstance(amount_hist.index, pd.MultiIndex):
            try:
                recent = amount_hist.loc[
                    pd.IndexSlice[lookback_start:current_date, :],
                    ["amount"],
                ]
            except (KeyError, TypeError, pd.errors.UnsortedIndexError):
                date_idx = amount_hist.index.get_level_values("date")
                recent = amount_hist[
                    (date_idx >= lookback_start) & (date_idx <= current_date)
                ]
            if not recent.empty:
                today_codes = set(today_df["equity_code"].astype(str))
                code_idx = recent.index.get_level_values("equity_code").astype(str)
                recent = recent[code_idx.isin(today_codes)]
        if recent.empty or "amount" not in recent.columns:
            codes = today_df["equity_code"].head(self.liquidity_top_n).astype(str).tolist()
        else:
            codes = (
                recent.groupby(level="equity_code")["amount"]
                .mean()
                .sort_values(ascending=False)
                .head(self.liquidity_top_n)
                .index.astype(str)
                .tolist()
            )
        if self.trading_permissions is not None:
            codes = filter_codes(codes, self.trading_permissions)
        held_codes = self._held_codes(context)
        if held_codes:
            held_set = set(held_codes)
            today_set = set(today_df["equity_code"].astype(str))
            codes = list(dict.fromkeys(codes + [c for c in held_codes if c in today_set]))
            if self.trading_permissions is not None:
                blocked = held_set - set(codes)
                if blocked:
                    logger.warning(
                        f"{current_date} 持仓含当前权限外股票 {sorted(blocked)}；"
                        "保留行情用于卖出风控，不作为新买候选"
                    )
        return codes

    def _position_state_features(
        self,
        context: Context,
        today_df: pd.DataFrame,
    ) -> pd.DataFrame:
        """为 sell 模型补齐当前真实持仓状态；未持仓股票使用中性状态。"""
        out = pd.DataFrame(index=today_df.index)
        out["holding_days"] = 0.0
        out["position_return"] = 0.0
        out["drawdown_from_position_peak"] = 0.0
        out["days_to_expected_horizon"] = float(self.decision_horizon)

        portfolio = getattr(context, "portfolio", None)
        if portfolio is None:
            return out
        close_by_code = today_df.set_index("equity_code")["close"].astype(float)
        held_codes = self._held_codes(context)
        candidate_codes = list(dict.fromkeys(list(self._position_state.keys()) + held_codes))
        for code in candidate_codes:
            if code not in close_by_code.index:
                continue
            pos = portfolio.get_position(code) if hasattr(portfolio, "get_position") else None
            if pos is None or getattr(pos, "total_qty", 0) <= 0:
                continue
            state = self._position_state.get(code)
            matching_idx = today_df.index[today_df["equity_code"] == code]
            if len(matching_idx) == 0:
                continue
            idx = matching_idx[0]
            close = float(close_by_code.loc[code])
            entry_date = self._actual_position_entry_date(pos)
            if not entry_date:
                entry_date = self._normalize_trade_date(
                    getattr(state, "entered_date", "") if state is not None else ""
                )
            entry_price = self._rich_entry_open(code, entry_date)
            peak = self._rich_position_peak(code, entry_date, context.current_date, close)
            holding_days = float(self._holding_trade_days(context, state, pos))
            expected_horizon = (
                float(state.expected_horizon)
                if state is not None
                else float(self.decision_horizon)
            )
            out.loc[idx, "holding_days"] = holding_days
            out.loc[idx, "position_return"] = (close / entry_price - 1.0) if entry_price > 0 else 0.0
            out.loc[idx, "drawdown_from_position_peak"] = (close / peak - 1.0) if peak > 0 else 0.0
            out.loc[idx, "days_to_expected_horizon"] = expected_horizon - holding_days
        return out

    def _rich_entry_open(self, code: str, entry_date: str) -> float:
        """取实际成交日的 qfq open，供 sell 模型持仓收益特征使用。"""
        if not entry_date or self._rich_features is None or self._rich_features.empty:
            return 0.0
        try:
            row = self._rich_features.loc[(entry_date, code)]
        except KeyError:
            return 0.0
        if isinstance(row, pd.DataFrame):
            row = row.iloc[-1]
        value = row.get("open", 0.0)
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    def _rich_position_peak(
        self,
        code: str,
        entry_date: str,
        current_date: str,
        fallback_close: float,
    ) -> float:
        """持仓期 qfq high 峰值，与训练样本的 drawdown_from_position_peak 对齐。"""
        if not entry_date or self._rich_features is None or self._rich_features.empty:
            return float(fallback_close)
        current_key = self._normalize_trade_date(current_date)
        if not current_key:
            return float(fallback_close)
        try:
            code_hist = self._rich_features.xs(code, level="equity_code")
        except KeyError:
            return float(fallback_close)
        if code_hist.empty or "high" not in code_hist.columns:
            return float(fallback_close)
        window = code_hist.loc[
            (code_hist.index >= entry_date) & (code_hist.index <= current_key)
        ]
        if window.empty:
            return float(fallback_close)
        high = pd.to_numeric(window["high"], errors="coerce").max()
        if pd.isna(high) or float(high) <= 0:
            return float(fallback_close)
        return max(float(high), float(fallback_close))

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

        current_date = self._normalize_trade_date(context.current_date)
        if not current_date:
            if self.require_rich_features:
                raise RuntimeError(f"无法规范化当前交易日: {context.current_date}")
            logger.warning(f"无法规范化当前交易日: {context.current_date}")
            return None
        try:
            today_df = self._rich_features.xs(current_date, level="date").copy()
        except KeyError:
            msg = f"{current_date} 在预加载特征表中无记录"
            if self.require_rich_features:
                raise RuntimeError(msg)
            logger.warning(msg)
            return None

        if today_df.empty:
            if self.require_rich_features:
                raise RuntimeError(f"{current_date} rich features 当日截面为空")
            return None

        today_df = today_df.reset_index()
        dynamic_universe = self._dynamic_universe_for_date(today_df, current_date, context)
        today_df = today_df[today_df["equity_code"].isin(dynamic_universe)]
        if today_df.empty:
            if self.require_rich_features:
                raise RuntimeError(f"{current_date} rich features 与 universe 无交集")
            return None

        # 缺失任何 daily 特征的股票直接丢弃（基本面 / event NaN 允许）
        before = len(today_df)
        today_df = today_df.dropna(subset=DAILY_FEATURE_COLUMNS)
        if len(today_df) < before:
            logger.debug(f"{current_date} daily 特征不全丢弃 {before - len(today_df)} 股")
        if today_df.empty:
            if self.require_rich_features:
                raise RuntimeError(f"{current_date} daily 特征完整的 rich 截面为空")
            return None
        today_df = today_df.reset_index(drop=True)
        # 持仓状态特征只用于诊断 / 策略层 trigger 合成，不再喂给 sell 模型。
        # 但保留写入 today_df / score_df 方便 _last_score_df 输出 / 单测验证。
        position_features = self._position_state_features(context, today_df)
        for col in POSITION_STATE_FEATURE_COLUMNS:
            today_df[col] = position_features[col].astype(float).to_numpy()

        X_buy = today_df[RICH_BUY_FEATURE_COLUMNS].values
        X_sell_reg = today_df[SELL_REGRESSION_FEATURE_COLUMNS].values

        # 保留当日可见特征列，供父类 regime 市场广度/危机判断复用。
        score_df = today_df[["equity_code"] + RICH_BUY_FEATURE_COLUMNS].rename(
            columns={"equity_code": "code"}
        ).copy()
        for h in BUY_HORIZONS:
            col = f"prob_up_h{h}"
            model = self._models.get(f"buy_h{h}")
            if model is not None:
                try:
                    pred = model.predict(X_buy)
                    score_df[col] = self._squash_to_prob(pred)
                except Exception as exc:
                    if not self.use_deterministic_fallback:
                        raise RuntimeError(
                            f"buy_h{h} 模型预测失败，且 use_deterministic_fallback=False"
                        ) from exc
                    logger.warning(f"buy_h{h} 预测失败，降级 fallback: {exc}")
                    score_df[col] = self._squash_to_prob(
                        deterministic_rich_score(
                            today_df[RICH_BUY_FEATURE_COLUMNS]
                        ).to_numpy()
                    )
            elif self.use_deterministic_fallback:
                score_df[col] = self._squash_to_prob(
                    deterministic_rich_score(today_df[RICH_BUY_FEATURE_COLUMNS]).to_numpy()
                )
            elif h != self.decision_horizon:
                score_df[col] = np.nan
            else:
                raise RuntimeError(
                    f"buy_h{h} 模型缺失，且 use_deterministic_fallback=False"
                )

        # ── 预测 sell（回归 optimal_remaining_days）──
        # 输出列：
        #   predicted_remaining_days  原始回归预测，clip 到 [0, sell_max_remaining_days]
        #   prob_sell                父类 sell trigger 用，sigmoid 桥接
        sell_model = self._models.get(SELL_REMAINING_DAYS_MODEL_NAME)
        remaining_days_series: pd.Series
        if sell_model is not None:
            try:
                raw_pred = np.asarray(sell_model.predict(X_sell_reg), dtype=float)
                remaining_days_series = pd.Series(raw_pred, index=today_df.index)
            except Exception as exc:
                if not self.use_deterministic_fallback:
                    raise RuntimeError(
                        "sell 回归模型预测失败，且 use_deterministic_fallback=False"
                    ) from exc
                logger.warning(f"sell 回归预测失败，降级 fallback: {exc}")
                remaining_days_series = deterministic_optimal_remaining_days(
                    today_df[SELL_REGRESSION_FEATURE_COLUMNS],
                    max_remaining_days=self.sell_max_remaining_days,
                )
        elif self.use_deterministic_fallback:
            remaining_days_series = deterministic_optimal_remaining_days(
                today_df[SELL_REGRESSION_FEATURE_COLUMNS],
                max_remaining_days=self.sell_max_remaining_days,
            )
        else:
            raise RuntimeError(
                f"{SELL_REMAINING_DAYS_MODEL_NAME} 模型缺失，"
                "且 use_deterministic_fallback=False"
            )

        # clip 到 [0, sell_max_remaining_days]，防止极端预测搞乱 trigger
        remaining_days_series = remaining_days_series.clip(
            lower=0.0, upper=self.sell_max_remaining_days
        )
        score_df["predicted_remaining_days"] = remaining_days_series.to_numpy()
        # 桥接到父类 prob_sell（>= sell_threshold 触发卖出）
        score_df["prob_sell"] = remaining_days_to_prob_sell(
            remaining_days_series,
            threshold=self.sell_remaining_days_threshold,
            sharpness=self.sell_remaining_days_sharpness,
        ).to_numpy()

        # 不跨 horizon 比较原始概率；交易排序固定用一个决策 horizon。
        score_df["score"] = score_df[f"prob_up_h{self.decision_horizon}"]
        return score_df

    def _argmax_horizon(self, row: pd.Series) -> int:
        """rich 版避免跨 horizon 概率比较，持仓状态特征记录决策 horizon。"""
        return self.decision_horizon

    # ──────────────────────────────────────────────────────────────
    # Sell trigger：在父类基础上追加 position-aware 触发器
    # ──────────────────────────────────────────────────────────────

    def _check_sell_triggers(
        self,
        context: Context,
        score_df: pd.DataFrame,
        top_2n_codes: set,
        stop_loss_pct: float,
        data: Dict[str, pd.Series],
    ) -> Dict[str, str]:
        """覆盖父类：在原 6 个触发器（stop_loss / trailing / max_hold /
        rank_dropout / prob_floor / sell_model）之外追加两个 position-aware
        触发器，用 position_return 和 holding_days 真正合成业务决策——
        因为新 sell 回归模型已经把持仓状态从特征里拿掉，这些信号必须在
        策略层显式合成，否则"浮盈大该止盈 / 浮亏久该割肉"会丢失。
        """
        sells = super()._check_sell_triggers(
            context, score_df, top_2n_codes, stop_loss_pct, data,
        )
        extra = self._check_position_aware_sell_triggers(
            context, score_df, data, existing=sells,
        )
        sells.update(extra)
        return sells

    def _check_position_aware_sell_triggers(
        self,
        context: Context,
        score_df: pd.DataFrame,
        data: Dict[str, pd.Series],
        existing: Dict[str, str],
    ) -> Dict[str, str]:
        """逐持仓检查两个 position-aware 触发器：

        - **profit_take（止盈）**：浮盈 ≥ ``profit_take_return_threshold``
          *且* ``prob_up_h{decision_horizon}`` < ``profit_take_prob_ceiling``
          → 兑现。语义："已经赚很多 + 模型不再看好" → 别等回吐。
          覆盖父类没有的场景："价仍在创新高所以 trailing_stop 没触发，但
          模型已经看不到上行了，应该兑现"。
        - **stale_loss（温水割肉）**：``holding_days >= stale_loss_min_days``
          *且* 浮亏 ≤ ``stale_loss_return_threshold``（**比父类 stop_loss
          浅一档**，默认 -2% vs 父类 -5%/-3%）→ 认输。
          覆盖父类没有的场景："亏得不够深所以 stop_loss 没触发，但已经
          拖了 8+ 个交易日还在水下"——典型温水煮青蛙。

        与父类触发器的关系（rich 在父类之后追加，已被父类卖出的 code 跳过）：

        ::

            父类:  a. stop_loss     | 浮亏 > stop_loss_pct（5%/3%）→ 卖
                   b. trailing_stop | 从持仓高点回撤 > trailing_stop_pct → 卖
                   ...
                   f. sell_model    | prob_sell > sell_threshold → 卖
            rich:  g. profit_take   | 浮盈 ≥ 20% 且模型转弱 → 卖
                   h. stale_loss    | 持仓久 + 浅幅亏（避开 a） → 卖

        触发依据用真实持仓的 ``pos.cost_price``（execution-priced，none 复权）
        计算 ``position_return``，跟回测撮合口径一致；不依赖 _position_state
        的快照（避免冷启动时缺失状态导致漏 trigger）。
        """
        portfolio = context.portfolio
        prob_lookup = score_df.set_index("code")
        out: Dict[str, str] = {}
        prob_h_col = f"prob_up_h{self.decision_horizon}"

        for code, pos in portfolio.positions.items():
            if code in existing or getattr(pos, "total_qty", 0) <= 0:
                continue
            if getattr(pos, "sellable_qty", 0) <= 0:
                continue
            if code not in data:
                continue
            cost = float(getattr(pos, "cost_price", 0.0) or 0.0)
            if cost <= 0:
                continue
            try:
                price = float(data[code]["close"])
            except (KeyError, TypeError, ValueError):
                continue
            if price <= 0:
                continue
            position_return = price / cost - 1.0
            state = self._position_state.get(code)
            holding_days = self._holding_trade_days(context, state, pos)

            # 止盈：浮盈大 + 市场已经透支
            if position_return >= self.profit_take_return_threshold:
                prob_h = None
                if code in prob_lookup.index and prob_h_col in prob_lookup.columns:
                    try:
                        prob_h = float(prob_lookup.loc[code, prob_h_col])
                    except (TypeError, ValueError):
                        prob_h = None
                if prob_h is not None and prob_h < self.profit_take_prob_ceiling:
                    out[code] = (
                        f"profit_take(ret={position_return:.2%},"
                        f"prob_h{self.decision_horizon}={prob_h:.3f})"
                    )
                    continue

            # 割肉：持仓拖太久 + 仍浮亏
            if (holding_days >= self.stale_loss_min_days
                    and position_return <= self.stale_loss_return_threshold):
                out[code] = (
                    f"stale_loss(ret={position_return:.2%},"
                    f"held={holding_days}d)"
                )
                continue

        return out
