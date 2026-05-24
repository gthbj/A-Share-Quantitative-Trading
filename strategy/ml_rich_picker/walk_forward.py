"""ml_rich_picker 走步训练驱动（PRD_20260525_03）。

与 ml_multi_horizon_picker.walk_forward 框架一致，差异：

- 数据加载：3 表 JOIN（daily / fundamental / event）替代单表
- 特征列：30 维 buy / 35 维 sell（含基本面 + 资金流）
- 模型输出目录：`models/walk_forward_rich/`

大量逻辑（配置加载、分片、universe 选择、sell-side 计算、LightGBM 训练、
GCS 读写）从 v1 直接 import 复用，避免代码重复。

用法::

    export ASHARE_USE_GCLOUD_ACCESS_TOKEN=1
    python -m strategy.ml_rich_picker.walk_forward \\
        --config strategy/ml_rich_picker/walk_forward_config.yaml
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

# 复用 v1 框架（PRD_20260524_12/13/14 已交付）
from strategy.ml_multi_horizon_picker.walk_forward import (
    WalkForwardConfig,
    load_walk_forward_config,
    list_month_end_dates,
    shard_retrain_dates,
    _make_bq_client,
    _load_volume_from_bq,
    _enrich_sell_features_grouped,
    _train_lgbm,
    _select_universe_at_date,
    _write_text_local_or_gcs,
    _is_gcs_path,
    _join_path,
)
from strategy.ml_multi_horizon_picker.labels import (
    build_buy_labels_cross_section,
    build_sell_label,
)
from strategy.ml_multi_horizon_picker.model_storage import (
    BUY_HORIZONS,
    SELL_MODEL_NAME,
    save_bundle,
)
from strategy.ml_multi_horizon_picker.tradable import filter_codes, merge_permissions

from strategy.ml_rich_picker.features import (
    DAILY_FEATURE_COLUMNS,
    FUNDAMENTAL_FEATURE_COLUMNS,
    EVENT_FEATURE_COLUMNS,
    RICH_BUY_FEATURE_COLUMNS,
    RICH_SELL_FEATURE_COLUMNS,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Rich SQL：3 表 JOIN
# ──────────────────────────────────────────────────────────────────────

def _load_rich_features_from_bq(
    cfg: WalkForwardConfig,
    start_date: str,
    end_date: str,
    code_filter: Optional[List[str]] = None,
) -> pd.DataFrame:
    """3-表 JOIN 拉取富特征宽表。

    daily: dws_equity_daily_features (17 维技术 + close/high/ma_60/amount)
    fundamental: dws_equity_fundamental_features (8 维基本面)
    event: dws_equity_event_money_flow_features_1d (5 维资金流/事件)

    INNER JOIN daily（必须有日线）; LEFT JOIN fundamental & event（缺失时 NaN）。
    LightGBM 原生支持 NaN，不做填充避免 lookahead。

    Args:
        code_filter: SQL 层股票池过滤，强烈推荐传入（避免拉全市场 OOM）

    Returns:
        宽表 DataFrame，列含：
        equity_code, date, close, high, ma_60, amount,
        DAILY_FEATURE_COLUMNS (17), FUNDAMENTAL_FEATURE_COLUMNS (8),
        EVENT_FEATURE_COLUMNS (5)
    """
    try:
        from google.cloud import bigquery  # type: ignore
    except ImportError as exc:
        raise ImportError("google-cloud-bigquery 未安装") from exc

    project = cfg.bq_project
    dataset = cfg.bq_dataset
    fund_tbl = "dws_equity_fundamental_features"
    event_tbl = "dws_equity_event_money_flow_features_1d"

    daily_select = ", ".join(
        ["equity_code", "date", "close", "high", "ma_60", "amount"]
        + DAILY_FEATURE_COLUMNS
    )

    sd = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:8]}"
    ed = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:8]}"

    params = [
        bigquery.ScalarQueryParameter("start_date", "STRING", sd),
        bigquery.ScalarQueryParameter("end_date", "STRING", ed),
        bigquery.ScalarQueryParameter("adjust", "STRING", cfg.adjust_type),
    ]
    code_clause_daily = ""
    code_clause_other = ""
    if code_filter:
        code_clause_daily = "AND equity_code IN UNNEST(@codes)"
        code_clause_other = "AND equity_code IN UNNEST(@codes)"
        params.append(
            bigquery.ArrayQueryParameter("codes", "STRING", list(code_filter))
        )

    sql = f"""
WITH daily AS (
    SELECT {daily_select}
    FROM `{project}.{dataset}.{cfg.bq_table_daily}`
    WHERE date BETWEEN DATE(@start_date) AND DATE(@end_date)
      AND adjust_type = @adjust
      {code_clause_daily}
),
fundamental AS (
    SELECT
        equity_code, date,
        pe_basic, pb, roe,
        gross_margin, net_margin, debt_to_assets,
        LN(market_cap + 1) AS log_market_cap,
        asset_turnover
    FROM `{project}.{dataset}.{fund_tbl}`
    WHERE date BETWEEN DATE(@start_date) AND DATE(@end_date)
      {code_clause_other}
),
event AS (
    SELECT
        equity_code, date,
        net_inflow_amount, main_net_inflow_amount,
        dragon_tiger_net_amount, limit_up_streak, is_kpl_event
    FROM `{project}.{dataset}.{event_tbl}`
    WHERE date BETWEEN DATE(@start_date) AND DATE(@end_date)
      {code_clause_other}
)
SELECT
    d.equity_code,
    d.date,
    d.close, d.high, d.ma_60, d.amount,
    {', '.join(f'd.{c}' for c in DAILY_FEATURE_COLUMNS)},
    f.pe_basic, f.pb, f.roe,
    f.gross_margin, f.net_margin, f.debt_to_assets,
    f.log_market_cap, f.asset_turnover,
    SAFE_DIVIDE(e.net_inflow_amount, d.amount) AS net_inflow_pct,
    SAFE_DIVIDE(e.main_net_inflow_amount, d.amount) AS main_net_inflow_pct,
    SAFE_DIVIDE(e.dragon_tiger_net_amount, d.amount) AS dragon_tiger_net_pct,
    e.limit_up_streak,
    CAST(IFNULL(e.is_kpl_event, FALSE) AS INT64) AS is_kpl_event_int
FROM daily d
LEFT JOIN fundamental f USING (equity_code, date)
LEFT JOIN event e USING (equity_code, date)
"""

    client = _make_bq_client(cfg)
    job_config = bigquery.QueryJobConfig(query_parameters=params)
    logger.info(
        f"Rich SQL: 3 表 JOIN ({start_date} ~ {end_date}, adjust={cfg.adjust_type}, "
        f"codes={len(code_filter) if code_filter else 'ALL'})"
    )
    df = client.query(sql, job_config=job_config, location=cfg.bq_location).result().to_dataframe()
    logger.info(f"Rich 读取完成: {len(df):,} 行 × {len(df.columns)} 列")

    # 强转 float（含 NaN 兼容）
    float_cols = (
        ["close", "high", "ma_60", "amount"]
        + DAILY_FEATURE_COLUMNS
        + FUNDAMENTAL_FEATURE_COLUMNS
        + EVENT_FEATURE_COLUMNS
    )
    for col in float_cols:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").astype(float)
    # date 列：BQ DATE → datetime → YYYYMMDD 字符串
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
    return df


# ──────────────────────────────────────────────────────────────────────
# 单时点训练（rich 版本）
# ──────────────────────────────────────────────────────────────────────

def _train_one_retrain_point_rich(
    enriched_df: pd.DataFrame,
    universe: List[str],
    retrain_end: str,
    cfg: WalkForwardConfig,
) -> Tuple[Dict[int, Any], Optional[Any], Dict[str, Any]]:
    """对单个重训点训 4 buy + 1 sell 模型（rich 特征版本）。

    与 v1 _train_one_retrain_point 区别：
    - 使用 RICH_BUY_FEATURE_COLUMNS (30) / RICH_SELL_FEATURE_COLUMNS (35)
    - 训练数据允许特征 NaN（LightGBM 原生支持），不强制 dropna 全部列
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
        "feature_set": "rich",
        "buy_feature_count": len(RICH_BUY_FEATURE_COLUMNS),
        "sell_feature_count": len(RICH_SELL_FEATURE_COLUMNS),
    }
    if len(window) < 500:
        logger.warning(f"[{retrain_end}] 训练窗样本极少 ({len(window)} 行)")

    # 标签
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

    # ── Buy 模型 ──
    # 注意：rich 特征里 fundamental + event 可能 NaN。LightGBM 原生支持 NaN，
    # 但需要保证至少 daily 17 维 + label 不为 NaN（drop 时只 drop label，不 drop features）
    buy_models: Dict[int, Any] = {}
    for h in cfg.buy_horizons:
        label_col = f"label_h{h}"
        # 只对 daily 17 维 + label 做 dropna，rich 列保留 NaN 给 LightGBM
        required_non_null = DAILY_FEATURE_COLUMNS + [label_col, "date"]
        sub = window.dropna(subset=required_non_null)
        tr = sub[sub["date"] < valid_start]
        va = sub[(sub["date"] >= valid_start) & (sub["date"] <= retrain_end)]
        if len(tr) < 200 or len(va) < 30:
            logger.warning(
                f"[{retrain_end}] buy_h{h} 样本不足 train={len(tr)} valid={len(va)}，跳过"
            )
            continue
        Xtr = tr[RICH_BUY_FEATURE_COLUMNS].values
        ytr = tr[label_col].astype(int).values
        Xva = va[RICH_BUY_FEATURE_COLUMNS].values
        yva = va[label_col].astype(int).values
        model = _train_lgbm(Xtr, ytr, Xva, yva, cfg.lightgbm_params)
        buy_models[h] = model
        metadata[f"buy_h{h}_train_rows"] = int(len(tr))
        metadata[f"buy_h{h}_valid_rows"] = int(len(va))

    # ── Sell 模型 ──
    sell_model = None
    required_non_null = DAILY_FEATURE_COLUMNS + ["label_sell", "date"]
    sub = window.dropna(subset=required_non_null)
    tr = sub[sub["date"] < valid_start]
    va = sub[(sub["date"] >= valid_start) & (sub["date"] <= retrain_end)]
    if len(tr) >= 200 and len(va) >= 30:
        Xtr = tr[RICH_SELL_FEATURE_COLUMNS].values
        ytr = tr["label_sell"].astype(int).values
        Xva = va[RICH_SELL_FEATURE_COLUMNS].values
        yva = va["label_sell"].astype(int).values
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
    parser = argparse.ArgumentParser(prog="walk_forward_rich")
    cfg_group = parser.add_mutually_exclusive_group(required=True)
    cfg_group.add_argument("--config", help="本地 yaml")
    cfg_group.add_argument("--config-gcs", help="GCS yaml")
    parser.add_argument("--dry-run", action="store_true", help="只列重训点不训练")
    parser.add_argument("--limit", type=int, default=0, help="只跑前 N 个重训点")
    parser.add_argument("--skip-registry", action="store_true", help="不写 registry（分片任务）")
    args = parser.parse_args(argv)

    config_path = args.config or args.config_gcs
    cfg = load_walk_forward_config(config_path)

    # 分片
    task_index_env = os.environ.get("CLOUD_RUN_TASK_INDEX")
    task_count_env = os.environ.get("CLOUD_RUN_TASK_COUNT")
    task_index = int(task_index_env) if task_index_env is not None else None
    task_count = int(task_count_env) if task_count_env is not None else None

    all_retrain_dates = list_month_end_dates(cfg.initial_train_end, cfg.final_retrain_date)
    if args.limit > 0:
        all_retrain_dates = all_retrain_dates[: args.limit]

    retrain_dates = shard_retrain_dates(all_retrain_dates, task_index, task_count)
    if task_index is not None and task_count is not None:
        logger.info(
            f"分片：CLOUD_RUN_TASK_INDEX={task_index}/{task_count} "
            f"处理 {len(retrain_dates)}/{len(all_retrain_dates)} 个时点"
        )
    if not retrain_dates:
        logger.warning("本任务无重训点，退出")
        return 0
    logger.info(f"本任务重训点: {retrain_dates[0]} ~ {retrain_dates[-1]} (共 {len(retrain_dates)})")
    logger.info(
        f"特征维度: buy={len(RICH_BUY_FEATURE_COLUMNS)}, sell={len(RICH_SELL_FEATURE_COLUMNS)}"
    )

    # 数据范围
    max_train_start = (
        pd.to_datetime(retrain_dates[0]) - timedelta(days=365 * cfg.rolling_window_years + 30)
    ).strftime("%Y%m%d")
    data_end = (
        pd.to_datetime(retrain_dates[-1]) + timedelta(days=cfg.sell_lookforward + 30)
    ).strftime("%Y%m%d")

    if args.dry_run:
        print(f"将训练 {len(retrain_dates)} 个时点（rich 特征）")
        print(f"数据范围: {max_train_start} ~ {data_end}")
        print(f"buy 特征: {RICH_BUY_FEATURE_COLUMNS}")
        print(f"sell 特征: {RICH_SELL_FEATURE_COLUMNS}")
        return 0

    # Phase 1: volume → universe 并集
    logger.info("Phase 1: 拉 volume 算 universe 并集…")
    volume_df = _load_volume_from_bq(cfg, max_train_start, data_end)
    universe_union: set = set()
    if cfg.fixed_codes:
        universe_union = set(filter_codes(cfg.fixed_codes, cfg.trading_permissions))
    else:
        for retrain_date in retrain_dates:
            snap = volume_df[volume_df["date"] == retrain_date]
            if snap.empty:
                prior = volume_df[volume_df["date"] <= retrain_date]
                if not prior.empty:
                    snap = volume_df[volume_df["date"] == prior["date"].max()]
            if snap.empty:
                continue
            cands = filter_codes(snap["equity_code"].unique().tolist(), cfg.trading_permissions)
            lookback_start_d = (
                pd.to_datetime(retrain_date) - timedelta(days=cfg.liquidity_lookback_days * 2)
            ).strftime("%Y%m%d")
            recent = volume_df[
                (volume_df["date"] >= lookback_start_d)
                & (volume_df["date"] <= retrain_date)
                & (volume_df["equity_code"].isin(cands))
            ]
            avg_amt = recent.groupby("equity_code")["amount"].mean().sort_values(ascending=False)
            universe_union.update(avg_amt.head(cfg.liquidity_top_n).index.tolist())

    universe_codes = sorted(universe_union)
    logger.info(
        f"Phase 1 完成: universe 并集 = {len(universe_codes)} 股 "
        f"(top_n={cfg.liquidity_top_n}, 时点数={len(retrain_dates)})"
    )
    if not universe_codes:
        logger.error("universe 并集为空，退出")
        return 1

    # Phase 2: rich features 3-表 JOIN
    logger.info("Phase 2: 拉 rich features (3 表 JOIN)…")
    features_df = _load_rich_features_from_bq(
        cfg, max_train_start, data_end, code_filter=universe_codes
    )

    # Phase 3: sell-side 5 维风险特征
    logger.info("Phase 3: 计算 sell-side 风险特征…")
    features_df = _enrich_sell_features_grouped(features_df)
    logger.info(f"Sell-side 特征完成，最终 {len(features_df):,} 行")

    # Phase 4: 每个重训点训模型
    model_root = cfg.model_root
    if not _is_gcs_path(model_root):
        Path(model_root).mkdir(parents=True, exist_ok=True)

    successful_dates: List[str] = []
    all_metadata: Dict[str, Dict[str, Any]] = {}
    for retrain_date in retrain_dates:
        logger.info(f"━━━━━━━━━━ 重训 @ {retrain_date} ━━━━━━━━━━")
        universe = _select_universe_at_date(features_df, volume_df, cfg, retrain_date)
        if not universe:
            logger.warning(f"[{retrain_date}] universe 为空，跳过")
            continue
        logger.info(f"[{retrain_date}] universe = {len(universe)} 股")
        buy_models, sell_model, meta = _train_one_retrain_point_rich(
            features_df, universe, retrain_date, cfg
        )
        if not buy_models and sell_model is None:
            logger.warning(f"[{retrain_date}] 全部模型训练失败，跳过保存")
            continue
        target_dir = _join_path(model_root, retrain_date)
        save_bundle(target_dir, buy_models, sell_model)
        meta_path = _join_path(target_dir, "metadata.json")
        _write_text_local_or_gcs(meta_path, json.dumps(meta, indent=2, ensure_ascii=False))
        all_metadata[retrain_date] = meta
        successful_dates.append(retrain_date)

    # Phase 5: 写注册表（分片任务跳过）
    is_sharded = task_index is not None and task_count is not None and task_count > 1
    if args.skip_registry or is_sharded:
        logger.info("分片模式：跳过 registry.json（由 build_registry 合并）")
    else:
        from strategy.ml_multi_horizon_picker.model_registry import build_registry
        registry = build_registry(str(model_root), successful_dates)
        registry_path = _join_path(model_root, "registry.json")
        _write_text_local_or_gcs(
            registry_path,
            json.dumps(
                {
                    "model_root": str(model_root),
                    "entries": [
                        {"train_end_date": d, "model_dir": _join_path(model_root, d)}
                        for d in successful_dates
                    ],
                },
                indent=2,
                ensure_ascii=False,
            ),
        )
        logger.info(f"registry.json 写入 {registry_path}（{len(successful_dates)} 时点）")

    summary_name = (
        f"metadata_summary_task{task_index}.json" if is_sharded else "metadata_summary.json"
    )
    summary_path = _join_path(model_root, summary_name)
    _write_text_local_or_gcs(summary_path, json.dumps(all_metadata, indent=2, ensure_ascii=False))

    logger.info("✅ Rich 走步训练完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
