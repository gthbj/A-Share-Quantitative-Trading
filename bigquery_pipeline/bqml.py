from __future__ import annotations

from datetime import datetime, timedelta

from .client import bq_client, table_id
from .sql import create_table_prefix, quote_table


FEATURE_COLUMNS = [
    "return_1d",
    "return_5d",
    "return_10d",
    "return_20d",
    "volume_ma5_ratio",
    "volume_ma20_ratio",
    "amount_ma5_ratio",
    "std_5d",
    "std_20d",
    "std_ratio",
    "rsi_14",
    "macd_diff",
    "macd_signal",
    "macd_hist",
    "close_to_high_20d",
    "close_to_ma5",
    "close_to_ma20",
    "pe_basic",
    "pb",
    "roe",
    "gross_margin",
    "net_margin",
    "debt_to_assets",
    "current_ratio",
    "asset_turnover",
    "market_cap_log",
    "net_inflow_to_amount",
    "main_net_inflow_to_amount",
    "dragon_tiger_net_to_amount",
    "dragon_tiger_department_count",
    "limit_up_streak",
    "is_kpl_event",
]


def bqml_config(config: dict) -> dict:
    defaults = {
        "model_name": "bqml_ml_stock_picker_baseline",
        "prediction_table": "ads_signal_ml_stock_picker_bqml_1d",
        "train_start_date": "20180101",
        "train_end_date": "20241231",
        "eval_start_date": "20250101",
        "eval_end_date": "20251231",
        "prediction_start_date": "20250101",
        "prediction_end_date": "20260522",
        "label_horizon": 5,
        "top_pct": 0.30,
        "bottom_pct": 0.30,
        "top_n": 50,
        "max_iterations": 30,
        "learn_rate": 0.05,
        "max_tree_depth": 6,
        "subsample": 0.8,
    }
    cfg = dict(defaults)
    cfg.update(config.get("bqml", {}).get("ml_stock_picker", {}) or {})
    return cfg


def _yyyymmdd(value: str) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    if len(digits) < 8:
        raise ValueError(f"date must be YYYYMMDD-like: {value}")
    return digits[:8]


def _date_literal(value: str) -> str:
    date_text = _yyyymmdd(value)
    return f"{date_text[:4]}-{date_text[4:6]}-{date_text[6:8]}"


def _add_days(value: str, days: int) -> str:
    dt = datetime.strptime(_yyyymmdd(value), "%Y%m%d") + timedelta(days=days)
    return dt.strftime("%Y%m%d")


def _feature_select_list() -> str:
    return ",\n  ".join(FEATURE_COLUMNS)


def _feature_sql(config: dict, start_date: str, end_date: str) -> str:
    daily = table_id(config, "dws_equity_daily_features")
    fundamental = table_id(config, "dws_equity_fundamental_features")
    event = table_id(config, "dws_equity_event_money_flow_features_1d")
    start_month = int(_yyyymmdd(start_date)[:6])
    end_month = int(_yyyymmdd(end_date)[:6])
    return f"""SELECT
  b.equity_code,
  b.date,
  b.partition_month,
  SAFE_CAST(b.close AS FLOAT64) AS close,
  SAFE_CAST(b.return_1d AS FLOAT64) AS return_1d,
  SAFE_CAST(b.return_5d AS FLOAT64) AS return_5d,
  SAFE_CAST(b.return_10d AS FLOAT64) AS return_10d,
  SAFE_CAST(b.return_20d AS FLOAT64) AS return_20d,
  SAFE_CAST(b.volume_ma5_ratio AS FLOAT64) AS volume_ma5_ratio,
  SAFE_CAST(b.volume_ma20_ratio AS FLOAT64) AS volume_ma20_ratio,
  SAFE_CAST(b.amount_ma5_ratio AS FLOAT64) AS amount_ma5_ratio,
  SAFE_CAST(b.std_5d AS FLOAT64) AS std_5d,
  SAFE_CAST(b.std_20d AS FLOAT64) AS std_20d,
  SAFE_CAST(b.std_ratio AS FLOAT64) AS std_ratio,
  SAFE_CAST(b.rsi_14 AS FLOAT64) AS rsi_14,
  SAFE_CAST(b.macd_diff AS FLOAT64) AS macd_diff,
  SAFE_CAST(b.macd_signal AS FLOAT64) AS macd_signal,
  SAFE_CAST(b.macd_hist AS FLOAT64) AS macd_hist,
  SAFE_CAST(b.close_to_high_20d AS FLOAT64) AS close_to_high_20d,
  SAFE_CAST(b.close_to_ma5 AS FLOAT64) AS close_to_ma5,
  SAFE_CAST(b.close_to_ma20 AS FLOAT64) AS close_to_ma20,
  SAFE_CAST(f.pe_basic AS FLOAT64) AS pe_basic,
  SAFE_CAST(f.pb AS FLOAT64) AS pb,
  SAFE_CAST(f.roe AS FLOAT64) AS roe,
  COALESCE(SAFE_CAST(f.gross_margin_from_income AS FLOAT64), SAFE_CAST(f.gross_margin AS FLOAT64)) AS gross_margin,
  COALESCE(SAFE_CAST(f.net_margin_from_income AS FLOAT64), SAFE_CAST(f.net_margin AS FLOAT64)) AS net_margin,
  COALESCE(SAFE_CAST(f.debt_to_assets_from_balance AS FLOAT64), SAFE_CAST(f.debt_to_assets AS FLOAT64)) AS debt_to_assets,
  COALESCE(SAFE_CAST(f.current_ratio_from_balance AS FLOAT64), SAFE_CAST(f.current_ratio AS FLOAT64)) AS current_ratio,
  COALESCE(SAFE_CAST(f.asset_turnover_from_income_balance AS FLOAT64), SAFE_CAST(f.asset_turnover AS FLOAT64)) AS asset_turnover,
  LOG(GREATEST(COALESCE(SAFE_CAST(f.market_cap AS FLOAT64), 0), 1)) AS market_cap_log,
  SAFE_DIVIDE(SAFE_CAST(e.net_inflow_amount AS FLOAT64), NULLIF(SAFE_CAST(b.amount AS FLOAT64), 0)) AS net_inflow_to_amount,
  SAFE_DIVIDE(SAFE_CAST(e.main_net_inflow_amount AS FLOAT64), NULLIF(SAFE_CAST(b.amount AS FLOAT64), 0)) AS main_net_inflow_to_amount,
  SAFE_DIVIDE(SAFE_CAST(e.dragon_tiger_net_amount AS FLOAT64), NULLIF(SAFE_CAST(b.amount AS FLOAT64), 0)) AS dragon_tiger_net_to_amount,
  SAFE_CAST(e.dragon_tiger_department_count AS FLOAT64) AS dragon_tiger_department_count,
  COALESCE(SAFE_CAST(e.limit_up_streak AS FLOAT64), 0) AS limit_up_streak,
  CASE WHEN e.is_kpl_event THEN 1.0 ELSE 0.0 END AS is_kpl_event
FROM {quote_table(daily)} AS b
LEFT JOIN {quote_table(fundamental)} AS f
  ON b.equity_code = f.equity_code AND b.date = f.date
LEFT JOIN {quote_table(event)} AS e
  ON b.equity_code = e.equity_code AND b.date = e.date
WHERE b.adjust_type = 'qfq'
  AND b.partition_month BETWEEN {start_month} AND {end_month}
  AND b.date BETWEEN DATE '{_date_literal(start_date)}' AND DATE '{_date_literal(end_date)}'
  AND b.close IS NOT NULL
  AND b.close > 0
  AND b.return_20d IS NOT NULL
  AND b.volume_ma20_ratio IS NOT NULL
  AND b.std_ratio IS NOT NULL"""


def _training_examples_sql(config: dict) -> str:
    cfg = bqml_config(config)
    label_horizon = int(cfg["label_horizon"])
    train_start = cfg["train_start_date"]
    eval_end = cfg["eval_end_date"]
    extended_end = _add_days(eval_end, max(label_horizon * 4 + 20, 30))
    high_offset = int(round((1 - float(cfg["top_pct"])) * 100))
    low_offset = int(round(float(cfg["bottom_pct"]) * 100))
    feature_sql = _feature_sql(config, train_start, extended_end)
    return f"""WITH features AS (
{feature_sql}
),
labeled AS (
  SELECT
    *,
    LOG(LEAD(close, {label_horizon}) OVER (
      PARTITION BY equity_code ORDER BY date
    )) - LOG(close) AS label_return
  FROM features
),
scored AS (
  SELECT *
  FROM labeled
  WHERE date BETWEEN DATE '{_date_literal(train_start)}' AND DATE '{_date_literal(eval_end)}'
    AND label_return IS NOT NULL
),
thresholds AS (
  SELECT
    date,
    APPROX_QUANTILES(label_return, 100)[OFFSET({low_offset})] AS low_q,
    APPROX_QUANTILES(label_return, 100)[OFFSET({high_offset})] AS high_q,
    COUNT(*) AS row_count
  FROM scored
  GROUP BY date
),
examples AS (
  SELECT
    s.*,
    CASE
      WHEN s.label_return >= t.high_q THEN 1
      WHEN s.label_return <= t.low_q THEN 0
      ELSE NULL
    END AS label_class,
    s.date BETWEEN DATE '{_date_literal(cfg["eval_start_date"])}' AND DATE '{_date_literal(cfg["eval_end_date"])}' AS is_eval
  FROM scored AS s
  JOIN thresholds AS t USING (date)
  WHERE t.row_count >= 10
)
SELECT
  label_class,
  is_eval,
  {_feature_select_list()}
FROM examples
WHERE label_class IS NOT NULL"""


def build_train_ml_stock_picker_bqml_sql(config: dict) -> str:
    cfg = bqml_config(config)
    model_id = table_id(config, cfg["model_name"])
    return f"""CREATE OR REPLACE MODEL {quote_table(model_id)}
OPTIONS(
  MODEL_TYPE = 'BOOSTED_TREE_CLASSIFIER',
  INPUT_LABEL_COLS = ['label_class'],
  DATA_SPLIT_METHOD = 'CUSTOM',
  DATA_SPLIT_COL = 'is_eval',
  MAX_ITERATIONS = {int(cfg["max_iterations"])},
  LEARN_RATE = {float(cfg["learn_rate"])},
  MAX_TREE_DEPTH = {int(cfg["max_tree_depth"])},
  SUBSAMPLE = {float(cfg["subsample"])},
  AUTO_CLASS_WEIGHTS = TRUE
) AS
{_training_examples_sql(config)}
"""


def build_predict_ml_stock_picker_bqml_sql(config: dict) -> str:
    cfg = bqml_config(config)
    model_id = table_id(config, cfg["model_name"])
    prediction_id = table_id(config, cfg["prediction_table"])
    prefix = create_table_prefix(
        prediction_id,
        partition=True,
        partition_field="partition_month",
        cluster_by=["date", "equity_code"],
    )
    feature_sql = _feature_sql(config, cfg["prediction_start_date"], cfg["prediction_end_date"])
    top_n = int(cfg["top_n"])
    return f"""{prefix}
WITH features AS (
{feature_sql}
),
predicted AS (
  SELECT *
  FROM ML.PREDICT(
    MODEL {quote_table(model_id)},
    (
      SELECT
        equity_code,
        date,
        partition_month,
        close,
        {_feature_select_list()}
      FROM features
    )
  )
),
scored AS (
  SELECT
    equity_code,
    date,
    partition_month,
    close,
    COALESCE(
      (
        SELECT SAFE_CAST(prob AS FLOAT64)
        FROM UNNEST(predicted_label_class_probs)
        WHERE CAST(label AS STRING) = '1'
        LIMIT 1
      ),
      CAST(predicted_label_class AS FLOAT64)
    ) AS prob_up,
    predicted_label_class,
    {_feature_select_list()}
  FROM predicted
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY date ORDER BY prob_up DESC, equity_code) AS score_rank
  FROM scored
)
SELECT
  equity_code,
  date,
  partition_month,
  close,
  prob_up,
  predicted_label_class,
  score_rank,
  {top_n} AS top_n,
  score_rank <= {top_n} AS is_selected,
  CASE WHEN score_rank <= {top_n} THEN 1 ELSE 0 END AS signal,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM ranked
"""


def build_evaluate_ml_stock_picker_bqml_sql(config: dict) -> str:
    cfg = bqml_config(config)
    model_id = table_id(config, cfg["model_name"])
    return f"SELECT * FROM ML.EVALUATE(MODEL {quote_table(model_id)})"


def train_ml_stock_picker_bqml(config: dict) -> None:
    client = bq_client(config)
    job = client.query(build_train_ml_stock_picker_bqml_sql(config))
    job.result()
    print(f"Trained BigQuery ML model {table_id(config, bqml_config(config)['model_name'])}; job_id={job.job_id}")


def predict_ml_stock_picker_bqml(config: dict) -> None:
    client = bq_client(config)
    job = client.query(build_predict_ml_stock_picker_bqml_sql(config))
    job.result()
    print(f"Wrote BigQuery ML predictions {table_id(config, bqml_config(config)['prediction_table'])}; job_id={job.job_id}")


def audit_ml_stock_picker_bqml(config: dict) -> dict:
    client = bq_client(config)
    cfg = bqml_config(config)
    model_id = table_id(config, cfg["model_name"])
    prediction_id = table_id(config, cfg["prediction_table"])

    model = client.get_model(model_id)
    table = client.get_table(prediction_id)

    metrics = client.query(build_evaluate_ml_stock_picker_bqml_sql(config)).to_dataframe()
    selected = client.query(
        f"""
        SELECT
          COUNT(*) AS row_count,
          COUNTIF(is_selected) AS selected_count,
          MAX(selected_per_date) AS max_selected_per_date,
          COUNT(DISTINCT date) AS date_count
        FROM (
          SELECT
            *,
            COUNTIF(is_selected) OVER (PARTITION BY date) AS selected_per_date
          FROM {quote_table(prediction_id)}
        )
        """
    ).to_dataframe()

    row_count = int(table.num_rows or 0)
    if row_count <= 0:
        raise RuntimeError(f"{prediction_id}: row_count is 0")
    max_selected = int(selected.loc[0, "max_selected_per_date"] or 0)
    if max_selected > int(cfg["top_n"]):
        raise RuntimeError(f"{prediction_id}: max_selected_per_date={max_selected} > top_n={cfg['top_n']}")

    print(f"{model_id}: model_type={getattr(model, 'model_type', 'unknown')}")
    print(metrics.to_string(index=False))
    print(selected.to_string(index=False))
    print(f"BigQuery ML baseline audit passed: rows={row_count}")
    return {
        "model_id": model_id,
        "prediction_id": prediction_id,
        "prediction_rows": row_count,
        "max_selected_per_date": max_selected,
    }
