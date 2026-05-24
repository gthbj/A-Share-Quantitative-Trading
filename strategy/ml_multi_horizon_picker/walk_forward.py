"""Walk-Forward 重训驱动。

配套 PRD_20260524_06。

用法::

    python -m strategy.ml_multi_horizon_picker.walk_forward \\
        --config strategy/ml_multi_horizon_picker/walk_forward_config.yaml

执行流程：
    1. 列出所有重训点（默认每月最后一个交易日）
    2. 一次性从 BigQuery 拉取覆盖所有重训点训练窗的数据
    3. 用 trading_permissions 过滤可交易股票
    4. 按近 N 日成交额做 liquidity 过滤（可选）
    5. 计算 sell-side 5 维风险特征
    6. 对每个重训点：
       - 切训练窗（rolling）
       - 截面分位算 4 个 buy label + 1 个 sell label
       - 训 5 个 LightGBM
       - 保存到 model_root/YYYYMMDD/
    7. 写注册表 registry.json
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

from strategy.ml_multi_horizon_picker.features import (
    BUY_FEATURE_COLUMNS,
    SELL_FEATURE_COLUMNS,
    compute_sell_risk_features,
)
from strategy.ml_multi_horizon_picker.labels import (
    build_buy_labels_cross_section,
    build_sell_label,
)
from strategy.ml_multi_horizon_picker.model_registry import (
    ModelRegistry,
    RegistryEntry,
    build_registry,
)
from strategy.ml_multi_horizon_picker.model_storage import (
    BUY_HORIZONS,
    SELL_MODEL_NAME,
    save_bundle,
)
from strategy.ml_multi_horizon_picker.tradable import (
    filter_codes,
    merge_permissions,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────
# 配置加载
# ──────────────────────────────────────────────────────────────────────

@dataclass
class WalkForwardConfig:
    initial_train_start: str
    initial_train_end: str
    final_retrain_date: str
    retrain_freq: str
    rolling_window_years: int
    model_root: str
    trading_permissions: Dict[str, bool]
    liquidity_top_n: int
    liquidity_lookback_days: int
    fixed_codes: List[str]
    buy_horizons: List[int]
    buy_top_quantile: float
    buy_bottom_quantile: float
    sell_lookforward: int
    sell_drawdown_threshold: float
    lightgbm_params: Dict[str, Any]
    bq_project: str
    bq_dataset: str
    bq_location: str
    bq_table_daily: str
    bq_table_kline: str
    bq_table_dim: str
    adjust_type: str
    valid_days: int


def load_walk_forward_config(path: str | Path) -> WalkForwardConfig:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    wf = raw["walk_forward"]
    perms = merge_permissions(raw.get("trading_permissions", {}))
    univ = raw.get("universe", {})
    labels = raw.get("labels", {})
    bq = raw.get("data", {}).get("bigquery", {})
    valid = raw.get("validation", {})

    return WalkForwardConfig(
        initial_train_start=str(wf["initial_train_start"]),
        initial_train_end=str(wf["initial_train_end"]),
        final_retrain_date=str(wf["final_retrain_date"]),
        retrain_freq=str(wf.get("retrain_freq", "month_end")),
        rolling_window_years=int(wf.get("rolling_window_years", 3)),
        model_root=str(wf.get("model_root", "models/walk_forward")),
        trading_permissions=perms,
        liquidity_top_n=int(univ.get("liquidity_top_n", 500)),
        liquidity_lookback_days=int(univ.get("liquidity_lookback_days", 60)),
        fixed_codes=list(univ.get("fixed_codes") or []),
        buy_horizons=list(labels.get("buy_horizons", [1, 5, 10, 20])),
        buy_top_quantile=float(labels.get("buy_top_quantile", 0.30)),
        buy_bottom_quantile=float(labels.get("buy_bottom_quantile", 0.30)),
        sell_lookforward=int(labels.get("sell_lookforward", 5)),
        sell_drawdown_threshold=float(labels.get("sell_drawdown_threshold", -0.05)),
        lightgbm_params=raw.get("lightgbm", {}),
        bq_project=str(bq.get("project_id", "data-aquarium")),
        bq_dataset=str(bq.get("dataset", "ashare")),
        bq_location=str(bq.get("location", "asia-east2")),
        bq_table_daily=str(bq.get("table_daily", "dws_equity_daily_features")),
        bq_table_kline=str(bq.get("table_kline", "dwd_fact_equity_kline_1d")),
        bq_table_dim=str(bq.get("table_dim", "dwd_dim_security")),
        adjust_type=str(raw.get("data", {}).get("adjust_type", "qfq")),
        valid_days=int(valid.get("valid_days", 60)),
    )


# ──────────────────────────────────────────────────────────────────────
# 重训日列表
# ──────────────────────────────────────────────────────────────────────

def list_month_end_dates(start: str, end: str) -> List[str]:
    """返回 [start, end] 区间内所有月末的 YYYYMMDD 字符串。

    使用日历月末（不是交易日历），因为标签生成会自动避开周末/节日。
    """
    start_d = pd.to_datetime(start)
    end_d = pd.to_datetime(end)
    months = pd.date_range(start_d, end_d, freq="M")  # month-end
    return [d.strftime("%Y%m%d") for d in months]


# ──────────────────────────────────────────────────────────────────────
# 数据加载
# ──────────────────────────────────────────────────────────────────────

def _make_bq_client(cfg: WalkForwardConfig):
    """构造 BigQuery 客户端，支持环境变量 ASHARE_USE_GCLOUD_ACCESS_TOKEN=1
    走 gcloud token 路径（与 bigquery_pipeline.client / data_layer.bigquery_source 一致）。"""
    try:
        from google.cloud import bigquery  # type: ignore
    except ImportError as exc:
        raise ImportError("google-cloud-bigquery 未安装") from exc
    kwargs = {"project": cfg.bq_project, "location": cfg.bq_location}
    import os
    if os.environ.get("ASHARE_USE_GCLOUD_ACCESS_TOKEN", "").strip().lower() in {"1", "true", "yes", "on"}:
        from bigquery_pipeline.client import gcloud_credentials
        kwargs["credentials"] = gcloud_credentials({})
    return bigquery.Client(**kwargs)


def _load_features_from_bq(cfg: WalkForwardConfig, start_date: str, end_date: str) -> pd.DataFrame:
    """从 dws_equity_daily_features 拉取宽表数据。

    返回字段：equity_code, date, close, high, ma_60, + 17 维 BUY_FEATURE_COLUMNS
    """
    try:
        from google.cloud import bigquery  # type: ignore
    except ImportError as exc:
        raise ImportError("google-cloud-bigquery 未安装") from exc

    fq = f"`{cfg.bq_project}.{cfg.bq_dataset}.{cfg.bq_table_daily}`"
    # ma_60 用于 sell-side 风险特征
    select_cols = ", ".join(
        ["equity_code", "date", "close", "high", "ma_60"] + BUY_FEATURE_COLUMNS
    )
    sql = f"""
        SELECT {select_cols}
        FROM {fq}
        WHERE date BETWEEN DATE(@start_date) AND DATE(@end_date)
          AND adjust_type = @adjust
    """
    client = _make_bq_client(cfg)
    # date 列在 BQ 端是 DATE 类型，传入需带连字符
    sd = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    ed = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "STRING", sd),
            bigquery.ScalarQueryParameter("end_date", "STRING", ed),
            bigquery.ScalarQueryParameter("adjust", "STRING", cfg.adjust_type),
        ]
    )
    logger.info(
        f"从 BigQuery 读取 {cfg.bq_table_daily} ({start_date} ~ {end_date}, "
        f"adjust={cfg.adjust_type})"
    )
    df = client.query(sql, job_config=job_config, location=cfg.bq_location).result().to_dataframe()
    logger.info(f"读取完成: {len(df):,} 行 × {len(df.columns)} 列")

    # 强转数值类型避免 Decimal 干扰
    for col in ["close", "high", "ma_60"] + BUY_FEATURE_COLUMNS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    # BigQuery DATE 列被读为 datetime.date / DateArray，统一转 YYYYMMDD 字符串
    # 便于 string 比较（与现有 features 等模块对齐）
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
    return df


def _load_volume_from_bq(cfg: WalkForwardConfig, start_date: str, end_date: str) -> pd.DataFrame:
    """加载日成交额用于 liquidity 排名。"""
    try:
        from google.cloud import bigquery  # type: ignore
    except ImportError:
        raise

    fq = f"`{cfg.bq_project}.{cfg.bq_dataset}.{cfg.bq_table_kline}`"
    sql = f"""
        SELECT equity_code, date, amount
        FROM {fq}
        WHERE date BETWEEN DATE(@start_date) AND DATE(@end_date)
          AND adjust_type = @adjust
    """
    client = _make_bq_client(cfg)
    # date 列在 BQ 端是 DATE 类型，传入需带连字符
    sd = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    ed = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "STRING", sd),
            bigquery.ScalarQueryParameter("end_date", "STRING", ed),
            bigquery.ScalarQueryParameter("adjust", "STRING", cfg.adjust_type),
        ]
    )
    df = client.query(sql, job_config=job_config, location=cfg.bq_location).result().to_dataframe()
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").astype(float)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
    return df


# ──────────────────────────────────────────────────────────────────────
# Universe 过滤
# ──────────────────────────────────────────────────────────────────────

def _select_universe_at_date(
    features_df: pd.DataFrame,
    volume_df: pd.DataFrame,
    cfg: WalkForwardConfig,
    as_of_date: str,
) -> List[str]:
    """在 as_of_date 时点选 universe。

    1. trading_permissions 过滤
    2. liquidity_top_n 排名（近 N 天平均成交额）

    Returns:
        股票代码列表
    """
    if cfg.fixed_codes:
        # 用户给死了
        return filter_codes(cfg.fixed_codes, cfg.trading_permissions)

    # 候选：features_df 里 as_of_date 当天有数据的所有股票
    snap = features_df[features_df["date"] == as_of_date]
    candidates = snap["equity_code"].unique().tolist()
    candidates = filter_codes(candidates, cfg.trading_permissions)

    # liquidity 排名
    lookback_start = (
        pd.to_datetime(as_of_date) - timedelta(days=cfg.liquidity_lookback_days * 2)
    ).strftime("%Y%m%d")
    recent_vol = volume_df[
        (volume_df["date"] >= lookback_start)
        & (volume_df["date"] <= as_of_date)
        & (volume_df["equity_code"].isin(candidates))
    ]
    avg_amount = (
        recent_vol.groupby("equity_code")["amount"].mean().sort_values(ascending=False)
    )
    top_n = avg_amount.head(cfg.liquidity_top_n).index.tolist()
    return top_n


# ──────────────────────────────────────────────────────────────────────
# 特征增强
# ──────────────────────────────────────────────────────────────────────

def _enrich_sell_features_grouped(df: pd.DataFrame) -> pd.DataFrame:
    """对每只股票计算 5 维 sell-side 风险特征。"""
    enriched_groups = []
    for code, group in df.groupby("equity_code"):
        out = compute_sell_risk_features(group)
        enriched_groups.append(out)
    return pd.concat(enriched_groups, ignore_index=True)


# ──────────────────────────────────────────────────────────────────────
# 模型训练
# ──────────────────────────────────────────────────────────────────────

def _train_lgbm(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_valid: np.ndarray,
    y_valid: np.ndarray,
    params: Dict[str, Any],
) -> Any:
    """训单个二分类 LightGBM。"""
    try:
        import lightgbm as lgb
    except ImportError as exc:
        raise ImportError("lightgbm 未安装") from exc

    train_set = lgb.Dataset(X_train, label=y_train)
    valid_set = lgb.Dataset(X_valid, label=y_valid, reference=train_set)
    lgb_params = {
        "objective": params.get("objective", "binary"),
        "metric": params.get("metric", ["binary_logloss"]),
        "num_leaves": params.get("num_leaves", 63),
        "learning_rate": params.get("learning_rate", 0.05),
        "feature_fraction": params.get("feature_fraction", 0.85),
        "bagging_fraction": params.get("bagging_fraction", 0.85),
        "bagging_freq": params.get("bagging_freq", 5),
        "min_child_samples": params.get("min_child_samples", 30),
        "random_state": params.get("random_state", 42),
        "verbose": params.get("verbose", -1),
    }
    return lgb.train(
        lgb_params,
        train_set,
        num_boost_round=params.get("num_boost_round", 200),
        valid_sets=[train_set, valid_set],
        callbacks=[
            lgb.early_stopping(params.get("early_stopping_rounds", 25), verbose=False),
            lgb.log_evaluation(period=0),
        ],
    )


def _train_one_retrain_point(
    enriched_df: pd.DataFrame,
    universe: List[str],
    retrain_end: str,
    cfg: WalkForwardConfig,
) -> Tuple[Dict[int, Any], Optional[Any], Dict[str, Any]]:
    """对单个重训点训 4 buy + 1 sell 模型。

    Returns:
        (buy_models {h: model}, sell_model 或 None, metadata)
    """
    train_start = (
        pd.to_datetime(retrain_end) - timedelta(days=365 * cfg.rolling_window_years)
    ).strftime("%Y%m%d")
    valid_start = (
        pd.to_datetime(retrain_end) - timedelta(days=cfg.valid_days)
    ).strftime("%Y%m%d")

    window = enriched_df[
        (enriched_df["date"] >= train_start)
        & (enriched_df["date"] <= retrain_end)
        & (enriched_df["equity_code"].isin(universe))
    ].copy()

    metadata: Dict[str, Any] = {
        "retrain_end": retrain_end,
        "train_start": train_start,
        "valid_start": valid_start,
        "universe_size": len(universe),
        "rows_in_window": int(len(window)),
    }
    # 提示型阈值；具体训不训交给下方每个模型自己的 train/valid 检查
    if len(window) < 500:
        logger.warning(
            f"[{retrain_end}] 训练窗口样本极少 ({len(window)} 行)，可能所有模型都跳过"
        )

    # 标签（截面分位算 buy + 单股算 sell）
    window = build_buy_labels_cross_section(
        window,
        horizons=cfg.buy_horizons,
        top_q=cfg.buy_top_quantile,
        bottom_q=cfg.buy_bottom_quantile,
    )
    window = build_sell_label(
        window,
        lookforward=cfg.sell_lookforward,
        drawdown_threshold=cfg.sell_drawdown_threshold,
    )

    # 训 4 buy
    buy_models: Dict[int, Any] = {}
    for h in cfg.buy_horizons:
        label_col = f"label_h{h}"
        cols_needed = BUY_FEATURE_COLUMNS + [label_col, "date"]
        sub = window.dropna(subset=cols_needed)
        tr = sub[sub["date"] < valid_start]
        va = sub[(sub["date"] >= valid_start) & (sub["date"] <= retrain_end)]
        if len(tr) < 200 or len(va) < 30:
            logger.warning(
                f"[{retrain_end}] buy_h{h} 样本不足 train={len(tr)} valid={len(va)}，跳过"
            )
            continue
        Xtr, ytr = tr[BUY_FEATURE_COLUMNS].values, tr[label_col].astype(int).values
        Xva, yva = va[BUY_FEATURE_COLUMNS].values, va[label_col].astype(int).values
        model = _train_lgbm(Xtr, ytr, Xva, yva, cfg.lightgbm_params)
        buy_models[h] = model
        metadata[f"buy_h{h}_train_rows"] = int(len(tr))
        metadata[f"buy_h{h}_valid_rows"] = int(len(va))

    # 训 sell
    sell_model = None
    cols_needed = SELL_FEATURE_COLUMNS + ["label_sell", "date"]
    sub = window.dropna(subset=cols_needed)
    tr = sub[sub["date"] < valid_start]
    va = sub[(sub["date"] >= valid_start) & (sub["date"] <= retrain_end)]
    if len(tr) >= 200 and len(va) >= 30:
        Xtr, ytr = tr[SELL_FEATURE_COLUMNS].values, tr["label_sell"].astype(int).values
        Xva, yva = va[SELL_FEATURE_COLUMNS].values, va["label_sell"].astype(int).values
        sell_model = _train_lgbm(Xtr, ytr, Xva, yva, cfg.lightgbm_params)
        metadata["sell_train_rows"] = int(len(tr))
        metadata["sell_valid_rows"] = int(len(va))
        metadata["sell_pos_ratio"] = float(np.mean(ytr))
    else:
        logger.warning(
            f"[{retrain_end}] sell 样本不足 train={len(tr)} valid={len(va)}"
        )

    return buy_models, sell_model, metadata


# ──────────────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="walk_forward")
    parser.add_argument("--config", required=True, help="walk_forward_config.yaml 路径")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只列重训点和数据范围，不实际训练",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="只跑前 N 个重训点（调试用，0 表示全部）",
    )
    args = parser.parse_args(argv)

    cfg = load_walk_forward_config(args.config)

    # 1. 列重训日
    retrain_dates = list_month_end_dates(
        cfg.initial_train_end, cfg.final_retrain_date
    )
    if args.limit > 0:
        retrain_dates = retrain_dates[: args.limit]
    logger.info(f"共 {len(retrain_dates)} 个重训点: {retrain_dates[0]} ~ {retrain_dates[-1]}")

    # 2. 数据范围 = max 训练窗口
    max_train_start = (
        pd.to_datetime(retrain_dates[0])
        - timedelta(days=365 * cfg.rolling_window_years + 30)
    ).strftime("%Y%m%d")
    # 训练终点 + sell_lookforward 留 buffer（label 计算需要未来 N 天）
    data_end = (
        pd.to_datetime(retrain_dates[-1])
        + timedelta(days=cfg.sell_lookforward + 30)
    ).strftime("%Y%m%d")

    if args.dry_run:
        print(f"将训练 {len(retrain_dates)} 个时点")
        print(f"数据范围: {max_train_start} ~ {data_end}")
        print(f"trading_permissions: {cfg.trading_permissions}")
        return 0

    # 3. 一次性拉数据
    logger.info(f"数据范围: {max_train_start} ~ {data_end}")
    features_df = _load_features_from_bq(cfg, max_train_start, data_end)
    volume_df = _load_volume_from_bq(cfg, max_train_start, data_end)

    # 4. 算 sell-side 风险特征（一次性算完整段，避免每次重训重算）
    logger.info("计算 sell-side 5 维风险特征…")
    features_df = _enrich_sell_features_grouped(features_df)
    logger.info(f"sell-side 特征完成，最终 {len(features_df):,} 行")

    # 5. 对每个重训点：选 universe + 训模型 + 保存
    model_root = Path(cfg.model_root)
    model_root.mkdir(parents=True, exist_ok=True)
    successful_dates: List[str] = []
    all_metadata: Dict[str, Dict[str, Any]] = {}

    for retrain_date in retrain_dates:
        logger.info(f"━━━━━━━━━━ 重训 @ {retrain_date} ━━━━━━━━━━")
        universe = _select_universe_at_date(features_df, volume_df, cfg, retrain_date)
        if not universe:
            logger.warning(f"[{retrain_date}] universe 为空，跳过")
            continue
        logger.info(f"[{retrain_date}] universe 大小 = {len(universe)}")

        buy_models, sell_model, meta = _train_one_retrain_point(
            features_df, universe, retrain_date, cfg
        )
        if not buy_models and sell_model is None:
            logger.warning(f"[{retrain_date}] 全部模型训练失败，跳过保存")
            continue

        # 保存
        target_dir = str(model_root / retrain_date)
        save_bundle(target_dir, buy_models, sell_model)
        # 单独写当时点 metadata
        meta_path = Path(target_dir) / "metadata.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(
            json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        all_metadata[retrain_date] = meta
        successful_dates.append(retrain_date)

    # 6. 写注册表
    registry = build_registry(str(model_root), successful_dates)
    registry_path = model_root / "registry.json"
    registry.to_json(registry_path)
    logger.info(
        f"注册表已写入 {registry_path}，共 {len(successful_dates)}/{len(retrain_dates)} 个成功时点"
    )

    # 7. 写汇总 metadata
    summary_path = model_root / "metadata_summary.json"
    summary_path.write_text(
        json.dumps(all_metadata, indent=2, ensure_ascii=False), encoding="utf-8"
    )

    logger.info("✅ 走步训练完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
