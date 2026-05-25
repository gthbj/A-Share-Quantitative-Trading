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
    _enrich_sell_features_grouped,
    _train_lgbm,
    _select_universe_at_date,
    _read_text_local_or_gcs,
    _write_text_local_or_gcs,
    _is_gcs_path,
    _join_path,
)
from strategy.ml_multi_horizon_picker.model_storage import (
    BUY_HORIZONS,
)
from strategy.ml_multi_horizon_picker.tradable import filter_codes, merge_permissions

from strategy.ml_rich_picker.features import (
    DAILY_FEATURE_COLUMNS,
    EVENT_FEATURE_COLUMNS,
    FUNDAMENTAL_FEATURE_COLUMNS,
    RICH_BUY_FEATURE_COLUMNS,
    SELL_REGRESSION_FEATURE_COLUMNS,
)
from strategy.ml_rich_picker.model_storage import (
    SELL_REMAINING_DAYS_MODEL_NAME,
    save_rich_bundle,
)
from utils.logger import get_logger

logger = get_logger(__name__)


# ──────────────────────────────────────────────────────────────────────
# Rich SQL：3 表 JOIN
# ──────────────────────────────────────────────────────────────────────

def _to_yyyymmdd(date_value: str) -> str:
    digits = "".join(ch for ch in str(date_value) if ch.isdigit())
    if len(digits) < 8:
        raise ValueError(f"日期格式应包含 YYYYMMDD: {date_value}")
    return digits[:8]


def _partition_months_in_range(start_date: str, end_date: str) -> List[int]:
    """返回覆盖 [start_date, end_date] 的 BigQuery partition_month 列表。"""
    start = _to_yyyymmdd(start_date)
    end = _to_yyyymmdd(end_date)
    start_period = pd.Period(f"{start[:4]}-{start[4:6]}", freq="M")
    end_period = pd.Period(f"{end[:4]}-{end[4:6]}", freq="M")
    if end_period < start_period:
        return []
    return [
        int(period.strftime("%Y%m"))
        for period in pd.period_range(start_period, end_period)
    ]


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
    money_tbl = "dwd_fact_money_flow_1d"
    dragon_tbl = "dwd_fact_dragon_tiger_seat_1d"
    kpl_tbl = "dwd_fact_kpl_board_1d"

    daily_select = ", ".join(
        ["equity_code", "date", "open", "close", "high", "low", "ma_60", "amount"]
        + DAILY_FEATURE_COLUMNS
    )

    start_yyyymmdd = _to_yyyymmdd(start_date)
    end_yyyymmdd = _to_yyyymmdd(end_date)
    sd = f"{start_yyyymmdd[:4]}-{start_yyyymmdd[4:6]}-{start_yyyymmdd[6:8]}"
    ed = f"{end_yyyymmdd[:4]}-{end_yyyymmdd[4:6]}-{end_yyyymmdd[6:8]}"
    partition_months = _partition_months_in_range(start_yyyymmdd, end_yyyymmdd)
    source_start_yyyymmdd = (
        pd.to_datetime(start_yyyymmdd) - timedelta(days=10)
    ).strftime("%Y%m%d")
    source_sd = (
        f"{source_start_yyyymmdd[:4]}-{source_start_yyyymmdd[4:6]}-"
        f"{source_start_yyyymmdd[6:8]}"
    )
    source_partition_months = _partition_months_in_range(source_start_yyyymmdd, end_yyyymmdd)

    params = [
        bigquery.ScalarQueryParameter("start_date", "STRING", sd),
        bigquery.ScalarQueryParameter("source_start_date", "STRING", source_sd),
        bigquery.ScalarQueryParameter("end_date", "STRING", ed),
        bigquery.ScalarQueryParameter("adjust", "STRING", cfg.adjust_type),
        bigquery.ArrayQueryParameter("partition_months", "INT64", partition_months),
        bigquery.ArrayQueryParameter("source_partition_months", "INT64", source_partition_months),
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
    WHERE partition_month IN UNNEST(@partition_months)
      AND date BETWEEN DATE(@start_date) AND DATE(@end_date)
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
    WHERE partition_month IN UNNEST(@partition_months)
      AND date BETWEEN DATE(@start_date) AND DATE(@end_date)
      -- 财报同日公告可能在盘后；信号在 T 收盘后生成，保守要求公告日早于信号日。
      AND (financial_announcement_date IS NULL OR financial_announcement_date < date)
      AND (income_announcement_date IS NULL OR income_announcement_date < date)
      AND (balance_announcement_date IS NULL OR balance_announcement_date < date)
      {code_clause_other}
),
money AS (
    -- Tushare moneyflow 普通资金流约交易日 19:00 更新；本策略假设信号
    -- 在 20:00 后生成、T+1 开盘执行，因此 trade_date=T 可用于 T 信号。
    SELECT
        equity_code, date,
        SUM(SAFE_CAST(net_inflow_amount AS FLOAT64)) AS net_inflow_amount,
        SUM(SAFE_CAST(main_net_inflow_amount AS FLOAT64)) AS main_net_inflow_amount
    FROM `{project}.{dataset}.{money_tbl}`
    WHERE partition_month IN UNNEST(@partition_months)
      AND date BETWEEN DATE(@start_date) AND DATE(@end_date)
      {code_clause_other}
    GROUP BY equity_code, date
),
dragon AS (
    -- 龙虎榜每日明细约每日 20:00 更新；同 moneyflow，按 T 日信号可用处理。
    SELECT
        equity_code, date,
        SUM(SAFE_CAST(net_amount AS FLOAT64)) AS dragon_tiger_net_amount
    FROM `{project}.{dataset}.{dragon_tbl}`
    WHERE partition_month IN UNNEST(@partition_months)
      AND date BETWEEN DATE(@start_date) AND DATE(@end_date)
      {code_clause_other}
    GROUP BY equity_code, date
),
trade_dates AS (
    SELECT DISTINCT date
    FROM daily
),
kpl_raw AS (
    SELECT
        equity_code, date,
        MAX(SAFE_CAST(limit_up_streak AS FLOAT64)) AS limit_up_streak
    FROM `{project}.{dataset}.{kpl_tbl}`
    WHERE partition_month IN UNNEST(@source_partition_months)
      AND date BETWEEN DATE(@source_start_date) AND DATE(@end_date)
      {code_clause_other}
    GROUP BY equity_code, date
),
kpl_mapped AS (
    -- 开盘啦榜单次日 8:30 更新；T 日 KPL 只能用于下一交易日的收盘信号。
    SELECT
        k.equity_code,
        (
            SELECT MIN(td.date)
            FROM trade_dates td
            WHERE td.date > k.date
        ) AS available_signal_date,
        TRUE AS is_kpl_event,
        k.limit_up_streak
    FROM kpl_raw k
),
kpl AS (
    SELECT
        equity_code,
        available_signal_date,
        TRUE AS is_kpl_event,
        MAX(limit_up_streak) AS limit_up_streak
    FROM kpl_mapped
    WHERE available_signal_date IS NOT NULL
    GROUP BY equity_code, available_signal_date
)
SELECT
    d.equity_code,
    d.date,
    d.open, d.close, d.high, d.low, d.ma_60, d.amount,
    {', '.join(f'd.{c}' for c in DAILY_FEATURE_COLUMNS)},
    f.pe_basic, f.pb, f.roe,
    f.gross_margin, f.net_margin, f.debt_to_assets,
    f.log_market_cap, f.asset_turnover,
    SAFE_DIVIDE(money.net_inflow_amount, d.amount) AS net_inflow_pct,
    SAFE_DIVIDE(money.main_net_inflow_amount, d.amount) AS main_net_inflow_pct,
    SAFE_DIVIDE(dragon.dragon_tiger_net_amount, d.amount) AS dragon_tiger_net_pct,
    kpl.limit_up_streak,
    CAST(IFNULL(kpl.is_kpl_event, FALSE) AS INT64) AS is_kpl_event_int
FROM daily d
LEFT JOIN fundamental f USING (equity_code, date)
LEFT JOIN money USING (equity_code, date)
LEFT JOIN dragon USING (equity_code, date)
LEFT JOIN kpl
  ON kpl.equity_code = d.equity_code
 AND kpl.available_signal_date = d.date
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
        ["open", "close", "high", "low", "ma_60", "amount"]
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


def _load_volume_from_bq_partitioned(
    cfg: WalkForwardConfig,
    start_date: str,
    end_date: str,
) -> pd.DataFrame:
    """加载日成交额用于 liquidity 排名，并做 partition_month 裁剪。"""
    try:
        from google.cloud import bigquery  # type: ignore
    except ImportError:
        raise

    start_yyyymmdd = _to_yyyymmdd(start_date)
    end_yyyymmdd = _to_yyyymmdd(end_date)
    sd = f"{start_yyyymmdd[:4]}-{start_yyyymmdd[4:6]}-{start_yyyymmdd[6:8]}"
    ed = f"{end_yyyymmdd[:4]}-{end_yyyymmdd[4:6]}-{end_yyyymmdd[6:8]}"
    partition_months = _partition_months_in_range(start_yyyymmdd, end_yyyymmdd)
    fq = f"`{cfg.bq_project}.{cfg.bq_dataset}.{cfg.bq_table_kline}`"
    sql = f"""
        SELECT equity_code, date, amount
        FROM {fq}
        WHERE partition_month IN UNNEST(@partition_months)
          AND date BETWEEN DATE(@start_date) AND DATE(@end_date)
          AND adjust_type = @adjust
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("partition_months", "INT64", partition_months),
            bigquery.ScalarQueryParameter("start_date", "STRING", sd),
            bigquery.ScalarQueryParameter("end_date", "STRING", ed),
            bigquery.ScalarQueryParameter("adjust", "STRING", cfg.adjust_type),
        ]
    )
    client = _make_bq_client(cfg)
    df = client.query(sql, job_config=job_config, location=cfg.bq_location).result().to_dataframe()
    df["amount"] = pd.to_numeric(df["amount"], errors="coerce").astype(float)
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
    return df


def _compute_execution_horizon_return(group: pd.DataFrame, horizon: int) -> pd.Series:
    """T 收盘出信号、T+1 开盘成交，horizon 到期后开盘退出的收益。"""
    opens = pd.to_numeric(group["open"], errors="coerce").astype(float)
    entry_open = opens.shift(-1)
    exit_open = opens.shift(-(horizon + 1))
    return np.log(exit_open) - np.log(entry_open)


def _build_buy_labels_execution_open(
    df: pd.DataFrame,
    horizons: List[int],
    top_q: float,
    bottom_q: float,
) -> pd.DataFrame:
    """用真实执行口径生成 buy 标签：T+1 open → T+h+1 open。"""
    if df.empty:
        return df.copy()
    required = {"date", "equity_code", "open"}
    if not required.issubset(df.columns):
        raise ValueError(f"df 必须包含 {required}，实际 {set(df.columns)}")

    out = df.sort_values(["equity_code", "date"]).copy()
    for h in horizons:
        ret_col = f"future_exec_ret_h{h}"
        pieces = [
            _compute_execution_horizon_return(group, h)
            for _, group in out.groupby("equity_code")
        ]
        out[ret_col] = pd.concat(pieces).sort_index() if pieces else np.nan

    for h in horizons:
        ret_col = f"future_exec_ret_h{h}"
        label_col = f"label_h{h}"
        out[label_col] = np.nan

        def _label_section(group: pd.DataFrame) -> pd.Series:
            valid = group.dropna(subset=[ret_col])
            if len(valid) < 10:
                return pd.Series(np.nan, index=group.index)
            top_th = valid[ret_col].quantile(1 - top_q)
            bot_th = valid[ret_col].quantile(bottom_q)
            labels = pd.Series(np.nan, index=group.index)
            labels[group[ret_col] >= top_th] = 1.0
            labels[group[ret_col] <= bot_th] = 0.0
            return labels

        out[label_col] = (
            out.groupby("date", group_keys=False).apply(_label_section).astype(float)
        )
    return out


def _future_open_drawdown_from_execution(group: pd.DataFrame, lookforward: int) -> pd.Series:
    opens = pd.to_numeric(group["open"], errors="coerce").astype(float).to_numpy()
    if "low" in group.columns:
        lows = pd.to_numeric(group["low"], errors="coerce").astype(float).to_numpy()
    elif "close" in group.columns:
        lows = pd.to_numeric(group["close"], errors="coerce").astype(float).to_numpy()
    else:
        lows = np.full(len(group), np.nan, dtype=float)
    out = np.full(len(group), np.nan, dtype=float)
    for i in range(len(group) - 1):
        entry_idx = i + 1
        end = min(len(group), entry_idx + lookforward)
        if entry_idx >= len(group) or end <= entry_idx or not np.isfinite(opens[entry_idx]) or opens[entry_idx] <= 0:
            continue
        out[i] = (np.nanmin(lows[entry_idx:end]) - opens[entry_idx]) / opens[entry_idx]
    return pd.Series(out, index=group.index)


def _build_held_position_sell_training_frame(
    df: pd.DataFrame,
    lookforward: int,
    drawdown_threshold: float,
    holding_day_samples: Optional[List[int]] = None,
    decision_horizon: int = 5,
    underperform_quantile: Optional[float] = 0.30,
) -> pd.DataFrame:
    """[DEPRECATED] 旧的 binary sell label 构造函数。

    仅保留以兼容旧 sell_v1.pkl 二分类模型的训练路径；新模型一律走
    ``_build_optimal_remaining_days_training_frame``（回归）。
    """
    if df.empty:
        return df.copy()
    required = {"date", "equity_code", "open", "close", "high"}
    if not required.issubset(df.columns):
        raise ValueError(f"df 必须包含 {required}，实际 {set(df.columns)}")

    holding_day_samples = holding_day_samples or [0, 3, 10, 20]
    base = df.sort_values(["equity_code", "date"]).copy()
    pieces = [
        _future_open_drawdown_from_execution(group, lookforward)
        for _, group in base.groupby("equity_code")
    ]
    base["future_exec_max_dd"] = pd.concat(pieces).sort_index() if pieces else np.nan
    ret_pieces = [
        _compute_execution_horizon_return(group, lookforward)
        for _, group in base.groupby("equity_code")
    ]
    base["future_exec_ret"] = pd.concat(ret_pieces).sort_index() if ret_pieces else np.nan
    if underperform_quantile is not None:
        q = max(0.0, min(1.0, float(underperform_quantile)))

        def _date_underperform_threshold(group: pd.Series) -> float:
            valid = group.dropna()
            if len(valid) < 10:
                return np.nan
            return float(valid.quantile(q))

        base["future_underperform_threshold"] = base.groupby("date")[
            "future_exec_ret"
        ].transform(_date_underperform_threshold)
    else:
        base["future_underperform_threshold"] = np.nan

    frames: List[pd.DataFrame] = []
    for holding_days in holding_day_samples:
        sample = base.copy()
        grouped = sample.groupby("equity_code", group_keys=False)
        entry_open = grouped["open"].shift(holding_days)
        position_peak = grouped["high"].transform(
            lambda s: s.rolling(holding_days + 1, min_periods=1).max()
        )
        sample["holding_days"] = float(holding_days)
        sample["position_return"] = (
            pd.to_numeric(sample["close"], errors="coerce").astype(float)
            / pd.to_numeric(entry_open, errors="coerce").astype(float)
            - 1.0
        )
        sample["drawdown_from_position_peak"] = (
            pd.to_numeric(sample["close"], errors="coerce").astype(float)
            / pd.to_numeric(position_peak, errors="coerce").astype(float)
            - 1.0
        )
        sample["days_to_expected_horizon"] = float(int(decision_horizon) - holding_days)
        frames.append(sample)

    out = pd.concat(frames, ignore_index=True)
    out["label_sell"] = np.nan
    risk_hit = out["future_exec_max_dd"] <= drawdown_threshold
    underperform_hit = (
        out["future_exec_ret"].notna()
        & out["future_underperform_threshold"].notna()
        & (out["future_exec_ret"] <= out["future_underperform_threshold"])
    )
    valid = out["future_exec_max_dd"].notna() | out["future_exec_ret"].notna()
    out.loc[valid, "label_sell"] = (risk_hit | underperform_hit).loc[valid].astype(float)
    return out


def _compute_optimal_remaining_days_per_group(
    group: pd.DataFrame,
    lookforward: int,
    drawdown_threshold: float,
) -> pd.Series:
    """对单只股票计算每天的 ``optimal_remaining_days`` 标签。

    语义对齐**策略推理时刻**：站在 T 日收盘后做卖出决策，T+1 开盘是最早的执行时点。
    label k 表示「从 T 日决策起还要持有的交易日数」——

    - ``k = 0``：**立刻卖**（T+1 开盘卖出，不再持有任何一天） → 基线收益 = 0
    - ``k = 1``：再持有 1 天（T+1 开盘不卖，T+2 开盘卖）
    - ``k = N``：再持有 N 天，T+1+N 开盘卖

    每行的标签是 ``argmax_k``：在风控可控（持仓期累计 drawdown 不破
    ``drawdown_threshold``）的窗口里收益最高的 k\\*。

    与策略推理的对齐保证：当模型预测 ``remaining_days = 0`` 时，
    ``remaining_days_to_prob_sell`` 桥接出高 ``prob_sell``、父类 trigger 在
    T 收盘卖出 → T+1 开盘成交，正好对应训练 label k=0 的"T+1 开盘卖"语义。

    具体算法（向量化思路用循环表达）::

        for each i (T):
            entry_idx = i + 1                # T+1 的索引，"如果继续持有，明天开盘价"
            baseline_open = opens[entry_idx]
            best_k = 0
            best_return = 0.0                # k=0 = 立刻卖，固定收益 0
            running_min_low = baseline_open  # 还未持有任何一天，drawdown=0
            for k in 1 .. lookforward:
                # 持有第 k 天的 low 参与 drawdown
                day_idx = entry_idx + k - 1  # 持仓窗口最后一个完整交易日
                running_min_low = min(running_min_low, low[day_idx])
                if (running_min_low - baseline_open) / baseline_open < dd_threshold:
                    break
                exit_idx = entry_idx + k      # 持有 k 天后的下一个开盘
                cum_return = log(open[exit_idx]) - log(baseline_open)
                if cum_return > best_return:
                    best_k, best_return = k, cum_return
            label[i] = best_k

    返回 Series（与 group 同 index），缺数据的位置为 NaN。
    """
    n = len(group)
    out = np.full(n, np.nan, dtype=float)
    if n < 2:
        return pd.Series(out, index=group.index)

    opens = pd.to_numeric(group["open"], errors="coerce").to_numpy(dtype=float)
    # group["close"] / group["low"] 的存在性独立判断，避免 DataFrame.get 默认值
    # 被强制求值（pandas 不做懒求值）。
    if "low" in group.columns:
        lows = pd.to_numeric(group["low"], errors="coerce").to_numpy(dtype=float)
    elif "close" in group.columns:
        lows = pd.to_numeric(group["close"], errors="coerce").to_numpy(dtype=float)
    else:
        lows = np.full(n, np.nan, dtype=float)

    for i in range(n - 1):
        entry_idx = i + 1                  # T+1 开盘价对应索引（baseline）
        if entry_idx >= n:
            break
        baseline_open = opens[entry_idx]
        if not np.isfinite(baseline_open) or baseline_open <= 0:
            continue

        # k=0 = 立刻卖，作为 baseline 总是合法。后面 k>=1 才有 drawdown / exit。
        best_k = 0
        best_return = 0.0
        running_min_low = baseline_open

        # k 最大不能超过 lookforward，也不能让 exit_idx 越界
        kmax = min(lookforward, n - entry_idx - 1)
        for k in range(1, kmax + 1):
            # 持有第 k 天（持仓窗口的最后一个完整交易日）。day_idx = entry_idx + k - 1
            # 注意：k=1 时 day_idx=entry_idx=T+1，这天的 low 进入 drawdown 统计。
            day_idx = entry_idx + k - 1
            day_low = lows[day_idx] if day_idx < n else np.nan
            if np.isfinite(day_low) and day_low > 0:
                if day_low < running_min_low:
                    running_min_low = day_low
            cum_drawdown = (running_min_low - baseline_open) / baseline_open
            if cum_drawdown < drawdown_threshold:
                break

            exit_idx = entry_idx + k          # 持有 k 天后的开盘 = T+1+k
            if exit_idx >= n:
                break
            exit_open = opens[exit_idx]
            if not np.isfinite(exit_open) or exit_open <= 0:
                continue
            cum_return = np.log(exit_open) - np.log(baseline_open)
            if cum_return > best_return:
                best_return = cum_return
                best_k = k

        out[i] = float(best_k)

    return pd.Series(out, index=group.index)


def _build_optimal_remaining_days_training_frame(
    df: pd.DataFrame,
    lookforward: int,
    drawdown_threshold: float,
) -> pd.DataFrame:
    """构造 sell 回归模型的训练样本。

    每行一个 (date, code)，标签 ``label_optimal_remaining_days`` 是从当日
    收盘出信号、明天开盘进场的视角下，未来 ``lookforward`` 天内**风险可控
    的最佳卖出剩余天数 k\\*** （浮点，0 表示该立刻卖出）。

    与旧 binary label 的本质区别：
    - 不再做 holding_day 样本复制 ⇒ 同一个 (date, code) 只有一份标签
    - 标签是连续值而不是 0/1 ⇒ 训练用 regression，不再用 binary
    - 标签**独立于持仓状态** ⇒ 持仓状态影响在策略层做最终决策时合成
    """
    if df.empty:
        return df.copy()
    required = {"date", "equity_code", "open", "close"}
    if not required.issubset(df.columns):
        raise ValueError(f"df 必须包含 {required}，实际 {set(df.columns)}")

    base = df.sort_values(["equity_code", "date"]).copy()
    pieces = [
        _compute_optimal_remaining_days_per_group(group, lookforward, drawdown_threshold)
        for _, group in base.groupby("equity_code")
    ]
    if pieces:
        base["label_optimal_remaining_days"] = pd.concat(pieces).sort_index()
    else:
        base["label_optimal_remaining_days"] = np.nan
    return base


# ──────────────────────────────────────────────────────────────────────
# 单时点训练（rich 版本）
# ──────────────────────────────────────────────────────────────────────

def _binary_auc(y_true: np.ndarray, y_score: np.ndarray) -> Optional[float]:
    """无 sklearn 依赖的二分类 AUC。单类验证集返回 None。"""
    y = np.asarray(y_true, dtype=int)
    score = np.asarray(y_score, dtype=float)
    mask = np.isfinite(score)
    y = y[mask]
    score = score[mask]
    if len(y) == 0 or len(np.unique(y)) < 2:
        return None
    order = np.argsort(score)
    ranks = np.empty(len(score), dtype=float)
    sorted_score = score[order]
    i = 0
    while i < len(score):
        j = i + 1
        while j < len(score) and sorted_score[j] == sorted_score[i]:
            j += 1
        avg_rank = (i + 1 + j) / 2.0
        ranks[order[i:j]] = avg_rank
        i = j
    pos = y == 1
    n_pos = int(pos.sum())
    n_neg = int((~pos).sum())
    rank_sum_pos = float(ranks[pos].sum())
    return (rank_sum_pos - n_pos * (n_pos + 1) / 2.0) / (n_pos * n_neg)


def _quality_threshold(cfg: WalkForwardConfig, key: str, default: float) -> float:
    return float(getattr(cfg, key, default))


def _spearman_corr(y_true: np.ndarray, y_pred: np.ndarray) -> Optional[float]:
    """无 scipy 依赖的 Spearman 相关系数。"""
    y = np.asarray(y_true, dtype=float)
    p = np.asarray(y_pred, dtype=float)
    mask = np.isfinite(y) & np.isfinite(p)
    y = y[mask]
    p = p[mask]
    if len(y) < 5 or np.std(y) == 0 or np.std(p) == 0:
        return None

    def _rankdata(arr: np.ndarray) -> np.ndarray:
        order = np.argsort(arr)
        ranks = np.empty(len(arr), dtype=float)
        sorted_arr = arr[order]
        i = 0
        while i < len(arr):
            j = i + 1
            while j < len(arr) and sorted_arr[j] == sorted_arr[i]:
                j += 1
            avg_rank = (i + 1 + j) / 2.0
            ranks[order[i:j]] = avg_rank
            i = j
        return ranks

    r1 = _rankdata(y)
    r2 = _rankdata(p)
    return float(np.corrcoef(r1, r2)[0, 1])


def _train_lgbm_with_quality(
    Xtr: np.ndarray,
    ytr: np.ndarray,
    Xva: np.ndarray,
    yva: np.ndarray,
    cfg: WalkForwardConfig,
    model_name: str,
) -> Tuple[Optional[Any], Dict[str, Any]]:
    """训单个 LightGBM 并做验证集质量闸门。

    根据 ``model_name`` 自动选择评估指标：
    - ``SELL_REMAINING_DAYS_MODEL_NAME``：回归，用 Spearman 相关系数（rank IC）
      作为质量闸门，配合 MAE / RMSE 诊断
    - 其它（buy_hX）：二分类，用 AUC 做闸门
    """
    is_regression = model_name == SELL_REMAINING_DAYS_MODEL_NAME
    params = dict(cfg.lightgbm_params or {})
    if is_regression:
        # **必须强制覆盖**，不能用 setdefault：项目 lightgbm yaml 配置默认
        # objective=binary / metric=binary_logloss（为 buy 模型服务），如果只
        # 用 setdefault，sell 回归模型会被错按 binary 训练，输出全部落在 [0,1]，
        # 后续 sigmoid 桥接会把几乎所有持仓打成 prob_sell > 0.5。
        params["objective"] = "regression_l1"
        params["metric"] = ["l1", "l2"]
    model = _train_lgbm(Xtr, ytr, Xva, yva, params)
    pred = np.asarray(model.predict(Xva), dtype=float)
    pred_mean = float(np.nanmean(pred)) if len(pred) else float("nan")

    if is_regression:
        # 回归质量：Spearman rank IC（>= 阈值则通过）
        corr = _spearman_corr(yva, pred)
        mae = float(np.nanmean(np.abs(pred - yva))) if len(yva) else float("nan")
        rmse = float(np.sqrt(np.nanmean((pred - yva) ** 2))) if len(yva) else float("nan")
        y_mean = float(np.nanmean(yva)) if len(yva) else float("nan")
        y_std = float(np.nanstd(yva)) if len(yva) else float("nan")
        min_corr = _quality_threshold(cfg, "min_sell_rank_ic", 0.05)
        passed = corr is not None and corr >= min_corr
        metrics: Dict[str, Any] = {
            "valid_rank_ic": None if corr is None else float(corr),
            "valid_mae": mae,
            "valid_rmse": rmse,
            "valid_pred_mean": pred_mean,
            "valid_y_mean": y_mean,
            "valid_y_std": y_std,
            "min_valid_rank_ic": min_corr,
            "quality_pass": bool(passed),
            "objective": params.get("objective"),
        }
        if not passed:
            logger.warning(
                f"{model_name} 回归质量未过闸门: rank_ic={corr}, min={min_corr}, "
                f"mae={mae:.3f}, rmse={rmse:.3f}"
            )
            return None, metrics
        return model, metrics

    # 二分类（buy_hX）
    auc = _binary_auc(yva, pred)
    pos_ratio = float(np.mean(yva)) if len(yva) else float("nan")
    min_auc = _quality_threshold(cfg, "min_buy_auc", 0.52)
    passed = auc is not None and auc >= min_auc
    metrics = {
        "valid_auc": None if auc is None else float(auc),
        "valid_pred_mean": pred_mean,
        "valid_pos_ratio": pos_ratio,
        "min_valid_auc": min_auc,
        "quality_pass": bool(passed),
    }
    if not passed:
        logger.warning(
            f"{model_name} 验证质量未过闸门: auc={auc}, min_auc={min_auc}"
        )
        return None, metrics
    return model, metrics


def _train_one_retrain_point_rich(
    enriched_df: pd.DataFrame,
    universe: List[str],
    retrain_end: str,
    cfg: WalkForwardConfig,
) -> Tuple[Dict[int, Any], Optional[Any], Dict[str, Any]]:
    """对单个重训点训 4 buy + 1 sell 模型（rich 特征版本）。

    与 v1 _train_one_retrain_point 的区别：
    - buy 仍是 4 个独立的 binary classifier，特征 ``RICH_BUY_FEATURE_COLUMNS`` (30)
    - sell **改为 regression** 预测 ``optimal_remaining_days`` ∈ [0, lookforward]，
      特征 ``SELL_REGRESSION_FEATURE_COLUMNS`` (35)；不再做 5×holding 采样复制
    - 训练数据允许特征 NaN（LightGBM 原生支持）
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
        "sell_feature_count": len(SELL_REGRESSION_FEATURE_COLUMNS),
        "sell_model_type": "regression_optimal_remaining_days",
        "sell_lookforward": int(cfg.sell_lookforward),
        "sell_drawdown_threshold": float(cfg.sell_drawdown_threshold),
    }
    if len(window) < 500:
        logger.warning(f"[{retrain_end}] 训练窗样本极少 ({len(window)} 行)")

    # 标签：buy 使用 T+1 open → 到期 open 的真实执行收益口径。
    buy_window = _build_buy_labels_execution_open(
        window,
        horizons=cfg.buy_horizons,
        top_q=cfg.buy_top_quantile,
        bottom_q=cfg.buy_bottom_quantile,
    )
    # sell 用回归 label：未来 lookforward 天内风险可控的最优剩余天数 k*。
    # 一个 (date, code) 只生成一份样本，不再做 holding 复制；持仓状态在
    # 策略层 trigger 时合成。
    sell_window = _build_optimal_remaining_days_training_frame(
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
        required_non_null = DAILY_FEATURE_COLUMNS + ["open", label_col, "date"]
        sub = buy_window.dropna(subset=required_non_null)
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
        model, metrics = _train_lgbm_with_quality(Xtr, ytr, Xva, yva, cfg, f"buy_h{h}")
        if model is None:
            metadata[f"buy_h{h}_quality"] = metrics
            continue
        buy_models[h] = model
        metadata[f"buy_h{h}_train_rows"] = int(len(tr))
        metadata[f"buy_h{h}_valid_rows"] = int(len(va))
        metadata[f"buy_h{h}_quality"] = metrics

    # ── Sell 模型（回归 optimal_remaining_days）──
    sell_model = None
    sell_label_col = "label_optimal_remaining_days"
    # 不在 dropna 列里加 sell-side risk 5 维（它们前 60 天 warmup 内允许 NaN），
    # 只要 daily 17 维 + label + open 完整即可。
    required_non_null = DAILY_FEATURE_COLUMNS + ["open", sell_label_col, "date"]
    sub = sell_window.dropna(subset=required_non_null)
    tr = sub[sub["date"] < valid_start]
    va = sub[(sub["date"] >= valid_start) & (sub["date"] <= retrain_end)]
    if len(tr) >= 200 and len(va) >= 30:
        Xtr = tr[SELL_REGRESSION_FEATURE_COLUMNS].values
        ytr = tr[sell_label_col].astype(float).values
        Xva = va[SELL_REGRESSION_FEATURE_COLUMNS].values
        yva = va[sell_label_col].astype(float).values
        sell_model, metrics = _train_lgbm_with_quality(
            Xtr, ytr, Xva, yva, cfg, SELL_REMAINING_DAYS_MODEL_NAME,
        )
        metadata["sell_train_rows"] = int(len(tr))
        metadata["sell_valid_rows"] = int(len(va))
        metadata["sell_label_mean"] = float(np.nanmean(ytr))
        metadata["sell_label_std"] = float(np.nanstd(ytr))
        metadata["sell_label_zero_ratio"] = float(np.mean(ytr == 0.0))
        metadata["sell_quality"] = metrics
    else:
        logger.warning(
            f"[{retrain_end}] sell 样本不足 train={len(tr)} valid={len(va)}"
        )

    return buy_models, sell_model, metadata


def _missing_rich_model_components(
    buy_models: Dict[int, Any],
    sell_model: Optional[Any],
    required_horizons: Optional[List[int]] = None,
) -> List[str]:
    """返回 rich 策略正式回测必需但本次未训练出的模型名。"""
    required = required_horizons or [5]
    missing = [
        f"buy_h{h}"
        for h in required
        if h not in buy_models or buy_models[h] is None
    ]
    if sell_model is None:
        missing.append(SELL_REMAINING_DAYS_MODEL_NAME)
    return missing


def _attach_rich_validation_config(cfg: WalkForwardConfig, config_path: str) -> None:
    """把 rich 专用质量闸门挂到复用的 WalkForwardConfig 上。"""
    try:
        import yaml
        raw = yaml.safe_load(_read_text_local_or_gcs(config_path)) or {}
    except Exception as exc:
        logger.warning(f"读取 rich validation 配置失败，使用默认质量闸门: {exc}")
        raw = {}
    validation = raw.get("validation", {}) if isinstance(raw, dict) else {}
    labels = raw.get("labels", {}) if isinstance(raw, dict) else {}
    setattr(cfg, "min_buy_auc", float(validation.get("min_buy_auc", 0.52)))
    # 新 sell 回归模型质量闸门：Spearman rank IC（>= 0.05 视为有效信号）
    setattr(cfg, "min_sell_rank_ic", float(validation.get("min_sell_rank_ic", 0.05)))
    # 旧 binary sell 模型阈值（保留供向后兼容）
    setattr(cfg, "min_sell_auc", float(validation.get("min_sell_auc", 0.52)))
    setattr(cfg, "decision_horizon", int(labels.get("decision_horizon", 5)))
    setattr(
        cfg,
        "sell_underperform_quantile",
        float(labels.get("sell_underperform_quantile", 0.30)),
    )


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
    _attach_rich_validation_config(cfg, config_path)

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
        f"特征维度: buy={len(RICH_BUY_FEATURE_COLUMNS)}, "
        f"sell_regression={len(SELL_REGRESSION_FEATURE_COLUMNS)}"
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
        print(f"buy 特征 ({len(RICH_BUY_FEATURE_COLUMNS)} 维): {RICH_BUY_FEATURE_COLUMNS}")
        print(
            f"sell 回归特征 ({len(SELL_REGRESSION_FEATURE_COLUMNS)} 维): "
            f"{SELL_REGRESSION_FEATURE_COLUMNS}"
        )
        print(
            "sell label: optimal_remaining_days (回归), "
            f"lookforward={cfg.sell_lookforward}, "
            f"drawdown_threshold={cfg.sell_drawdown_threshold}"
        )
        return 0

    # Phase 1: volume → universe 并集
    logger.info("Phase 1: 拉 volume 算 universe 并集…")
    volume_df = _load_volume_from_bq_partitioned(cfg, max_train_start, data_end)
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
        required_horizons = [int(getattr(cfg, "decision_horizon", 5))]
        missing_models = _missing_rich_model_components(
            buy_models,
            sell_model,
            required_horizons=required_horizons,
        )
        meta["expected_buy_horizons"] = list(BUY_HORIZONS)
        meta["required_buy_horizons"] = required_horizons
        meta["model_bundle_complete"] = not missing_models
        meta["missing_models"] = missing_models
        if missing_models:
            logger.warning(
                f"[{retrain_date}] 模型包不完整，缺失 {missing_models}，跳过保存/registry"
            )
            all_metadata[retrain_date] = meta
            continue
        target_dir = _join_path(model_root, retrain_date)
        save_rich_bundle(target_dir, buy_models, sell_model)
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
