"""一键训练 4 个 buy 模型 + 1 个 sell 模型。

用法：
    python -m strategy.ml_multi_horizon_picker.train \
        --config strategy/ml_multi_horizon_picker/train_config.yaml

数据来源：BigQuery `ashare.dws_equity_daily_features`
认证：ADC（与 bigquery_pipeline 一致）

输出：5 个 pickle 文件到 train_config.output.model_dir。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

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
from strategy.ml_multi_horizon_picker.model_storage import (
    BUY_HORIZONS,
    SELL_MODEL_NAME,
    save_bundle,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────
# 数据加载
# ──────────────────────────────────────────────────────────────────

def _load_from_bigquery(cfg: dict, start_date: str, end_date: str) -> pd.DataFrame:
    """从 dws_equity_daily_features 读 17 维 + close + high。"""
    try:
        from google.cloud import bigquery  # type: ignore
    except ImportError as exc:
        raise ImportError("google-cloud-bigquery 未安装") from exc

    bq_cfg = cfg["data"]["bigquery"]
    project_id = bq_cfg["project_id"]
    dataset = bq_cfg["dataset"]
    location = bq_cfg["location"]
    table = bq_cfg["table_daily"]
    adjust = cfg["data"].get("adjust_type", "qfq")

    fq_table = f"`{project_id}.{dataset}.{table}`"
    universe = cfg.get("universe") or []
    univ_clause = ""
    if universe:
        codes = ",".join([f'"{c}"' for c in universe])
        univ_clause = f"AND equity_code IN ({codes})"

    sql = f"""
        SELECT
          equity_code, date, close, high,
          {', '.join(BUY_FEATURE_COLUMNS)}, ma_60
        FROM {fq_table}
        WHERE date BETWEEN @start_date AND @end_date
          AND adjust_type = @adjust
          {univ_clause}
    """
    client = bigquery.Client(project=project_id, location=location)
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ScalarQueryParameter("start_date", "STRING", start_date),
            bigquery.ScalarQueryParameter("end_date", "STRING", end_date),
            bigquery.ScalarQueryParameter("adjust", "STRING", adjust),
        ]
    )
    logger.info(f"从 BigQuery 读取 {table} ({start_date} ~ {end_date}, adjust={adjust})")
    df = client.query(sql, job_config=job_config, location=location).result().to_dataframe()
    logger.info(f"读取完成: {len(df)} 行 × {len(df.columns)} 列")
    return df


def _enrich_sell_features(df: pd.DataFrame) -> pd.DataFrame:
    """为每只股票计算 5 维 sell-side 风险特征。"""
    enriched_groups = []
    for code, group in df.groupby("equity_code"):
        out = compute_sell_risk_features(group)
        enriched_groups.append(out)
    return pd.concat(enriched_groups, ignore_index=True)


# ──────────────────────────────────────────────────────────────────
# 模型训练
# ──────────────────────────────────────────────────────────────────

def _train_lightgbm(
    X_train: np.ndarray,
    y_train: np.ndarray,
    X_valid: np.ndarray,
    y_valid: np.ndarray,
    params: dict,
) -> Any:
    """训练单个 LightGBM 二分类模型。"""
    try:
        import lightgbm as lgb
    except ImportError as exc:
        raise ImportError("lightgbm 未安装，请 pip install lightgbm") from exc

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

    model = lgb.train(
        lgb_params,
        train_set,
        num_boost_round=params.get("num_boost_round", 200),
        valid_sets=[train_set, valid_set],
        callbacks=[
            lgb.early_stopping(params.get("early_stopping_rounds", 25), verbose=False),
            lgb.log_evaluation(period=0),
        ],
    )
    return model


def _evaluate(model: Any, X: np.ndarray, y: np.ndarray, label: str) -> Dict[str, float]:
    """简单评估：accuracy + AUC（如可）。"""
    pred = model.predict(X)
    pred_class = (pred >= 0.5).astype(int)
    acc = float(np.mean(pred_class == y))
    metrics = {"accuracy": acc, "n": int(len(y)), "pos_ratio": float(np.mean(y))}
    try:
        from sklearn.metrics import roc_auc_score
        metrics["auc"] = float(roc_auc_score(y, pred))
    except Exception:
        pass
    logger.info(f"[{label}] {metrics}")
    return metrics


# ──────────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────────

def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="train_ml_multi_horizon")
    parser.add_argument("--config", required=True, help="train_config.yaml 路径")
    parser.add_argument(
        "--skip-buy",
        action="store_true",
        help="只训卖出模型，跳过 4 个 buy 模型",
    )
    parser.add_argument(
        "--skip-sell",
        action="store_true",
        help="只训 buy 模型，跳过 sell",
    )
    args = parser.parse_args(argv)

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))

    # 1. 加载训练 + 验证数据
    train_start = cfg["window"]["train_start_date"]
    train_end = cfg["window"]["train_end_date"]
    valid_start = cfg["window"]["valid_start_date"]
    valid_end = cfg["window"]["valid_end_date"]

    source = cfg["data"]["source"]
    if source != "bigquery":
        raise NotImplementedError(f"v1 仅支持 bigquery 源，得到 {source}")

    train_df = _load_from_bigquery(cfg, train_start, train_end)
    valid_df = _load_from_bigquery(cfg, valid_start, valid_end)

    # 2. 计算 sell-side 风险特征
    logger.info("计算 sell-side 风险特征 (训练集)")
    train_df = _enrich_sell_features(train_df)
    logger.info("计算 sell-side 风险特征 (验证集)")
    valid_df = _enrich_sell_features(valid_df)

    metadata: Dict[str, Any] = {"train_rows": len(train_df), "valid_rows": len(valid_df)}

    # 3. 训 4 个 buy 模型
    buy_models: Dict[int, Any] = {}
    if not args.skip_buy:
        train_df_b = build_buy_labels_cross_section(
            train_df,
            horizons=cfg["labels"]["buy_horizons"],
            top_q=cfg["labels"]["buy_top_quantile"],
            bottom_q=cfg["labels"]["buy_bottom_quantile"],
        )
        valid_df_b = build_buy_labels_cross_section(
            valid_df,
            horizons=cfg["labels"]["buy_horizons"],
            top_q=cfg["labels"]["buy_top_quantile"],
            bottom_q=cfg["labels"]["buy_bottom_quantile"],
        )

        for h in cfg["labels"]["buy_horizons"]:
            label_col = f"label_h{h}"
            tr = train_df_b.dropna(subset=BUY_FEATURE_COLUMNS + [label_col])
            va = valid_df_b.dropna(subset=BUY_FEATURE_COLUMNS + [label_col])
            if len(tr) < 1000 or len(va) < 100:
                logger.warning(
                    f"buy_h{h} 样本不足 (train={len(tr)}, valid={len(va)})，跳过"
                )
                continue
            Xtr = tr[BUY_FEATURE_COLUMNS].values
            ytr = tr[label_col].astype(int).values
            Xva = va[BUY_FEATURE_COLUMNS].values
            yva = va[label_col].astype(int).values
            logger.info(f"训练 buy_h{h}: train={len(tr)}, valid={len(va)}")
            model = _train_lightgbm(Xtr, ytr, Xva, yva, cfg["lightgbm"])
            metadata[f"buy_h{h}"] = _evaluate(model, Xva, yva, f"buy_h{h} valid")
            buy_models[h] = model

    # 4. 训 sell 模型
    sell_model = None
    if not args.skip_sell:
        train_df_s = build_sell_label(
            train_df,
            lookforward=cfg["labels"]["sell_lookforward"],
            drawdown_threshold=cfg["labels"]["sell_drawdown_threshold"],
        )
        valid_df_s = build_sell_label(
            valid_df,
            lookforward=cfg["labels"]["sell_lookforward"],
            drawdown_threshold=cfg["labels"]["sell_drawdown_threshold"],
        )

        tr = train_df_s.dropna(subset=SELL_FEATURE_COLUMNS + ["label_sell"])
        va = valid_df_s.dropna(subset=SELL_FEATURE_COLUMNS + ["label_sell"])
        if len(tr) >= 1000 and len(va) >= 100:
            Xtr = tr[SELL_FEATURE_COLUMNS].values
            ytr = tr["label_sell"].astype(int).values
            Xva = va[SELL_FEATURE_COLUMNS].values
            yva = va["label_sell"].astype(int).values
            logger.info(f"训练 sell_v1: train={len(tr)}, valid={len(va)}")
            sell_model = _train_lightgbm(Xtr, ytr, Xva, yva, cfg["lightgbm"])
            metadata[SELL_MODEL_NAME] = _evaluate(sell_model, Xva, yva, "sell_v1 valid")
        else:
            logger.warning(
                f"sell 样本不足 (train={len(tr)}, valid={len(va)})，跳过"
            )

    # 5. 保存
    model_dir = cfg["output"]["model_dir"]
    save_bundle(model_dir, buy_models, sell_model)
    if cfg["output"].get("save_metadata", True):
        meta_path = Path(model_dir) / "metadata.json"
        meta_path.parent.mkdir(parents=True, exist_ok=True)
        meta_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), "utf-8")
        logger.info(f"metadata.json 已写入 {meta_path}")

    logger.info("训练完成。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
