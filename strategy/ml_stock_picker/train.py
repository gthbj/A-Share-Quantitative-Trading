"""模型训练脚本：日线中频 LightGBM / XGBoost 选股模型。

推荐在 GCP Spot VM 上运行：
  python strategy/ml_stock_picker/train.py --config strategy/ml_stock_picker/train_config.yaml

训练流程：
  1. 优先从 BigQuery DWS 拉取技术 + 基本面 + 事件/资金流增强特征
  2. 兼容旧路径：从日K线逐只股票构建技术指标特征
  3. 计算未来 horizon 天对数收益标签（仅训练使用）
  4. 截面分位数二值化标签（top30%=1, bottom30%=0）
  5. 按时间切分训练/验证集，避免未来泄漏
  6. 保存模型到本地或 GCS
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd
import yaml

from strategy.ml_stock_picker.features import FeatureEngineer
from strategy.ml_stock_picker.model_storage import save_model
from utils.logger import get_logger, setup_logging

logger = get_logger(__name__)


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def build_data_source(cfg: dict):
    """根据配置构造数据源。"""
    source_type = cfg.get("data_source", "bigquery")
    if source_type in {"bigquery", "bigquery_dws"}:
        from data_layer.bigquery_source import BigQueryDataSource
        bq_cfg = cfg.get("bigquery", {})
        return BigQueryDataSource(
            project_id=bq_cfg.get("project_id", ""),
            dataset=bq_cfg.get("dataset", "ashare"),
            location=bq_cfg.get("location", "asia-east2"),
            credentials_path=bq_cfg.get("credentials_path", ""),
            use_cache=True,
            tables=bq_cfg.get("tables", {}),
        )
    elif source_type == "local":
        from data_layer.local_storage import LocalStorage
        return LocalStorage(root_dir=cfg.get("local_dir", "data/raw"))
    else:
        raise ValueError(f"不支持的数据源类型: {source_type}")


def fetch_train_data(
    data_source,
    universe: List[str],
    start_date: str,
    end_date: str,
    period: str = "daily",
) -> Dict[str, pd.DataFrame]:
    """拉取训练区间全部股票行情。"""
    logger.info(f"拉取训练数据: {len(universe)} 只股票, {start_date} ~ {end_date}")
    return data_source.get_multi_bars(universe, start_date, end_date, period=period)


def build_dataset(
    all_bars: Dict[str, pd.DataFrame],
    feature_engineer: FeatureEngineer,
    top_pct: float = 0.30,
    bottom_pct: float = 0.30,
) -> pd.DataFrame:
    """为全市场构建特征+标签数据集。

    流程：
      1. 逐只股票计算特征和标签（未来收益）
      2. 合并为全市场长表
      3. 按交易日截面分位数，将标签二值化
    """
    records = []
    for code, df in all_bars.items():
        if df.empty or len(df) < feature_engineer.feature_window + 5:
            continue
        feat_df = feature_engineer.compute_features(df)
        labeled = feature_engineer.compute_label(feat_df)
        labeled["code"] = code
        records.append(labeled)

    if not records:
        return pd.DataFrame()

    full = pd.concat(records, ignore_index=True)
    full = full.sort_values(["date", "code"]).reset_index(drop=True)

    return assign_cross_section_labels(full, top_pct=top_pct, bottom_pct=bottom_pct)


def assign_cross_section_labels(
    full: pd.DataFrame,
    top_pct: float = 0.30,
    bottom_pct: float = 0.30,
) -> pd.DataFrame:
    """按交易日截面分位数，将未来收益标签二值化。"""
    if full.empty:
        return full.copy()
    full = full.dropna(subset=["label_return"]).copy()

    def _binarize(group: pd.DataFrame) -> pd.DataFrame:
        if len(group) < 10:
            group["label_class"] = np.nan
            return group
        q_high = group["label_return"].quantile(1 - top_pct)
        q_low = group["label_return"].quantile(bottom_pct)
        group["label_class"] = np.nan
        group.loc[group["label_return"] >= q_high, "label_class"] = 1
        group.loc[group["label_return"] <= q_low, "label_class"] = 0
        return group

    full = full.groupby("date", group_keys=False).apply(_binarize)
    return full.dropna(subset=["label_class"]).reset_index(drop=True)


def fetch_dws_train_dataset(
    data_source,
    universe: List[str],
    start_date: str,
    end_date: str,
    feature_engineer: FeatureEngineer,
    feature_set: str,
    label_horizon: int,
    top_pct: float,
    bottom_pct: float,
) -> pd.DataFrame:
    """从 BigQuery DWS 直接读取增强特征并构建训练集。"""
    loader = getattr(data_source, "get_equity_feature_history", None)
    if loader is None:
        raise ValueError("当前数据源不支持 get_equity_feature_history")
    logger.info(
        f"从 BigQuery DWS 拉取训练特征: universe={len(universe)}, "
        f"feature_set={feature_set}, {start_date}~{end_date}"
    )
    raw = loader(
        universe,
        start_date,
        end_date,
        feature_set=feature_set,
        label_horizon=label_horizon,
    )
    if raw.empty:
        return raw
    raw = feature_engineer.prepare_model_frame(
        raw, feature_set=feature_set, require_technical=True
    )
    return assign_cross_section_labels(raw, top_pct=top_pct, bottom_pct=bottom_pct)


def split_train_validation(
    df: pd.DataFrame,
    validation_ratio: float = 0.2,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """按时间切分训练集和验证集，避免随机打散导致未来泄漏。"""
    if df.empty or validation_ratio <= 0:
        return df.copy(), pd.DataFrame()
    dates = sorted(df["date"].astype(str).unique())
    if len(dates) < 5:
        return df.copy(), pd.DataFrame()
    cutoff_idx = max(int(len(dates) * (1 - validation_ratio)), 1)
    cutoff_idx = min(cutoff_idx, len(dates) - 1)
    cutoff_date = dates[cutoff_idx]
    train_df = df[df["date"].astype(str) < cutoff_date].copy()
    valid_df = df[df["date"].astype(str) >= cutoff_date].copy()
    return train_df.reset_index(drop=True), valid_df.reset_index(drop=True)


def train_model(
    df: pd.DataFrame,
    model_type: str = "lightgbm",
    model_params: Optional[dict] = None,
    feature_set: str = "technical",
) -> Any:
    """训练分类模型。"""
    feature_cols = FeatureEngineer.feature_columns(feature_set)
    df = df.dropna(subset=["label_class"]).reset_index(drop=True)
    X = df[feature_cols].values
    y = df["label_class"].astype(int).values

    logger.info(f"训练样本: {len(X)} 条, 正样本: {y.sum()}, 负样本: {len(y) - y.sum()}")

    if model_type == "lightgbm":
        try:
            import lightgbm as lgb
        except ImportError:
            raise ImportError("未安装 lightgbm，请执行 pip install lightgbm")
        params = model_params or {
            "objective": "binary",
            "metric": "auc",
            "boosting_type": "gbdt",
            "num_leaves": 31,
            "learning_rate": 0.05,
            "feature_fraction": 0.9,
            "bagging_fraction": 0.8,
            "bagging_freq": 5,
            "verbose": -1,
        }
        train_data = lgb.Dataset(X, label=y, feature_name=feature_cols)
        model = lgb.train(params, train_data, num_boost_round=100)
        return model

    elif model_type == "xgboost":
        try:
            import xgboost as xgb
        except ImportError:
            raise ImportError("未安装 xgboost，请执行 pip install xgboost")
        params = model_params or {
            "objective": "binary:logistic",
            "eval_metric": "auc",
            "max_depth": 6,
            "learning_rate": 0.05,
            "subsample": 0.8,
            "colsample_bytree": 0.9,
            "verbosity": 0,
        }
        dtrain = xgb.DMatrix(X, label=y)
        model = xgb.train(params, dtrain, num_boost_round=100)
        return model
    else:
        raise ValueError(f"不支持的模型类型: {model_type}")


def evaluate_model(
    df: pd.DataFrame,
    model: Any,
    model_type: str,
    feature_set: str = "technical",
) -> dict:
    """评估模型：计算 AUC、IC、RankIC。"""
    return evaluate_model_with_features(df, model, model_type, feature_set=feature_set)


def evaluate_model_with_features(
    df: pd.DataFrame,
    model: Any,
    model_type: str,
    feature_set: str = "technical",
) -> dict:
    """评估模型：计算 AUC、IC、RankIC。"""
    if df.empty:
        return {"auc": np.nan, "ic": np.nan, "rank_ic": np.nan}
    feature_cols = FeatureEngineer.feature_columns(feature_set)
    X = df[feature_cols].values
    y = df["label_class"].astype(int).values

    if model_type == "lightgbm":
        pred = model.predict(X)
    elif model_type == "xgboost":
        import xgboost as xgb
        pred = model.predict(xgb.DMatrix(X))
    else:
        pred = np.zeros(len(y))

    # AUC
    try:
        from sklearn.metrics import roc_auc_score
        auc = roc_auc_score(y, pred)
    except Exception:
        auc = np.nan

    # IC / RankIC（按日截面计算后取平均）
    df = df.copy()
    df["pred"] = pred

    def _daily_ic(group: pd.DataFrame) -> pd.Series:
        if len(group) < 5:
            return pd.Series({"ic": np.nan, "rank_ic": np.nan})
        ic = group["pred"].corr(group["label_return"], method="pearson")
        rank_ic = group["pred"].corr(group["label_return"], method="spearman")
        return pd.Series({"ic": ic, "rank_ic": rank_ic})

    daily = df.groupby("date").apply(_daily_ic)
    mean_ic = daily["ic"].mean()
    mean_rank_ic = daily["rank_ic"].mean()

    logger.info(f"评估结果: AUC={auc:.4f}, IC={mean_ic:.4f}, RankIC={mean_rank_ic:.4f}")
    return {"auc": auc, "ic": mean_ic, "rank_ic": mean_rank_ic}


def main() -> int:
    parser = argparse.ArgumentParser(description="Train ML stock picker model")
    parser.add_argument("--config", default="strategy/ml_stock_picker/train_config.yaml")
    parser.add_argument("--output", default="", help="模型输出路径，覆盖配置文件")
    args = parser.parse_args()

    cfg = load_config(args.config)
    setup_logging(level=cfg.get("logging", {}).get("level", "INFO"))

    train_cfg = cfg.get("train", {})
    model_type = train_cfg.get("model_type", "lightgbm")
    model_params = train_cfg.get("model_params", {})
    feature_window = train_cfg.get("feature_window", 20)
    label_horizon = train_cfg.get("label_horizon", 5)
    feature_set = train_cfg.get("feature_set", "enhanced")
    top_pct = train_cfg.get("top_pct", 0.30)
    bottom_pct = train_cfg.get("bottom_pct", 0.30)
    validation_ratio = float(train_cfg.get("validation_ratio", 0.2))

    universe = train_cfg.get("universe", [])
    if not universe:
        logger.error("训练配置中 universe 为空，请指定股票池")
        return 1

    start_date = train_cfg.get("start_date", "")
    end_date = train_cfg.get("end_date", "")
    if not start_date or not end_date:
        logger.error("训练配置中 start_date / end_date 为空")
        return 1

    data_source = build_data_source(cfg)
    fe = FeatureEngineer(feature_window=feature_window, label_horizon=label_horizon)
    source_type = cfg.get("data_source", "bigquery")

    if source_type == "bigquery_dws":
        dataset = fetch_dws_train_dataset(
            data_source,
            universe,
            start_date,
            end_date,
            feature_engineer=fe,
            feature_set=feature_set,
            label_horizon=label_horizon,
            top_pct=top_pct,
            bottom_pct=bottom_pct,
        )
    else:
        if feature_set == "enhanced":
            logger.warning("非 bigquery_dws 数据源不提供增强特征，feature_set 降级为 technical")
            feature_set = "technical"
        # 为特征计算预留前置数据
        train_start_dt = datetime.strptime(start_date, "%Y%m%d")
        buffer_days = max(feature_window, label_horizon) + 10
        adjusted_start = (train_start_dt - timedelta(days=buffer_days)).strftime("%Y%m%d")

        all_bars = fetch_train_data(data_source, universe, adjusted_start, end_date)
        if not all_bars:
            logger.error("未获取到任何训练数据")
            return 1
        dataset = build_dataset(all_bars, fe, top_pct=top_pct, bottom_pct=bottom_pct)
        dataset = fe.prepare_model_frame(dataset, feature_set=feature_set, require_technical=True)

    if dataset.empty:
        logger.error("构建数据集后为空，请检查数据范围和特征计算")
        return 1

    logger.info(f"数据集构建完成: {len(dataset)} 条, 列={list(dataset.columns)}")

    train_df, valid_df = split_train_validation(dataset, validation_ratio=validation_ratio)
    if train_df.empty:
        logger.error("训练集为空，请检查 validation_ratio 或数据范围")
        return 1
    logger.info(
        f"时间切分完成: train={len(train_df)} 条, validation={len(valid_df)} 条, "
        f"feature_set={feature_set}"
    )

    model = train_model(
        train_df,
        model_type=model_type,
        model_params=model_params,
        feature_set=feature_set,
    )
    eval_df = valid_df if not valid_df.empty else train_df
    metrics = evaluate_model_with_features(eval_df, model, model_type, feature_set=feature_set)
    metrics.update(
        {
            "feature_set": feature_set,
            "train_rows": int(len(train_df)),
            "validation_rows": int(len(valid_df)),
            "validation_ratio": validation_ratio,
        }
    )

    output_path = args.output or train_cfg.get("model_output_path", "")
    if output_path:
        save_model(model, output_path)
        # 同时保存评估指标
        metrics_path = str(Path(output_path).with_suffix(".metrics.json"))
        import json
        with open(metrics_path, "w", encoding="utf-8") as f:
            json.dump(metrics, f, indent=2, ensure_ascii=False)
        logger.info(f"评估指标已保存: {metrics_path}")
    else:
        logger.warning("未配置 model_output_path，模型仅保存在内存中")

    return 0


if __name__ == "__main__":
    sys.exit(main())
