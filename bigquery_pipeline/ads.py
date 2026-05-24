from __future__ import annotations

from collections.abc import Callable

from .client import ads_table_name, bq_client, dws_table_name, table_id
from .sql import create_table_prefix, quote_table


def ads_config(config: dict) -> dict:
    return config.get("defaults", {}).get("ads", {})


def _run_query(config: dict, sql: str, label: str) -> None:
    client = bq_client(config)
    job = client.query(sql)
    job.result()
    print(f"Transformed {label}; job_id={job.job_id}")


def build_double_ma_signal_sql(config: dict) -> str:
    destination_id = table_id(config, ads_table_name(config, "signal_double_ma_1d"))
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["fund_code"])
    return f"""{prefix}
SELECT
  fund_code,
  date,
  partition_month,
  close,
  ma_5,
  ma_20,
  CASE WHEN ma_5 > ma_20 THEN 1 ELSE 0 END AS signal,
  CASE WHEN ma_5 > ma_20 THEN 'long' ELSE 'flat' END AS signal_label,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM {quote_table(table_id(config, dws_table_name(config, "fund_daily_features")))}
WHERE ma_5 IS NOT NULL
  AND ma_20 IS NOT NULL
"""


def build_ml_stock_picker_signal_sql(config: dict) -> str:
    top_n = int(ads_config(config).get("ml_stock_picker_top_n", 50))
    destination_id = table_id(config, ads_table_name(config, "signal_ml_stock_picker_1d"))
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["date", "equity_code"])
    return f"""{prefix}
WITH scored AS (
  SELECT
    equity_code,
    date,
    partition_month,
    close,
    0.35 * COALESCE(return_20d, 0)
      + 0.20 * COALESCE(return_5d, 0)
      + 0.15 * COALESCE(volume_ma20_ratio - 1, 0)
      - 0.20 * COALESCE(std_ratio, 0)
      + 0.10 * COALESCE(close_to_ma20, 0) AS score_proxy,
    return_1d,
    return_5d,
    return_20d,
    volume_ma20_ratio,
    std_ratio,
    rsi_14,
    macd_hist,
    close_to_ma20
  FROM {quote_table(table_id(config, dws_table_name(config, "equity_daily_features")))}
  WHERE return_20d IS NOT NULL
    AND volume_ma20_ratio IS NOT NULL
    AND std_ratio IS NOT NULL
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY date ORDER BY score_proxy DESC, equity_code) AS score_rank
  FROM scored
)
SELECT
  equity_code,
  date,
  partition_month,
  close,
  score_proxy,
  score_rank,
  {top_n} AS top_n,
  score_rank <= {top_n} AS is_selected,
  CASE WHEN score_rank <= {top_n} THEN 1 ELSE 0 END AS signal,
  return_1d,
  return_5d,
  return_20d,
  volume_ma20_ratio,
  std_ratio,
  rsi_14,
  macd_hist,
  close_to_ma20,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM ranked
"""


def build_volatility_timing_signal_sql(config: dict) -> str:
    destination_id = table_id(config, ads_table_name(config, "signal_volatility_timing_1d"))
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["index_code"])
    return f"""{prefix}
WITH base AS (
  SELECT
    index_code,
    date,
    partition_month,
    close,
    return_20d,
    volatility_20,
    AVG(volatility_20) OVER (
      PARTITION BY index_code ORDER BY date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW
    ) AS volatility_252_avg
  FROM {quote_table(table_id(config, dws_table_name(config, "index_daily_features")))}
  WHERE return_20d IS NOT NULL
    AND volatility_20 IS NOT NULL
)
SELECT
  index_code,
  date,
  partition_month,
  close,
  return_20d,
  volatility_20,
  volatility_252_avg,
  CASE
    WHEN volatility_20 > volatility_252_avg * 1.25 THEN 0.5
    WHEN return_20d > 0 THEN 1.0
    ELSE 0.3
  END AS position_scale,
  CASE
    WHEN volatility_20 > volatility_252_avg * 1.25 THEN 'high_volatility'
    WHEN return_20d > 0 THEN 'risk_on'
    ELSE 'risk_off'
  END AS signal_label,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM base
WHERE volatility_252_avg IS NOT NULL
"""


def build_regime_switching_signal_sql(config: dict) -> str:
    destination_id = table_id(config, ads_table_name(config, "signal_regime_switching_1d"))
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["index_code", "regime_label"])
    return f"""{prefix}
WITH base AS (
  SELECT
    index_code,
    date,
    partition_month,
    close,
    return_20d,
    volatility_20,
    amount_ma20_ratio,
    AVG(volatility_20) OVER (
      PARTITION BY index_code ORDER BY date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW
    ) AS volatility_252_avg
  FROM {quote_table(table_id(config, dws_table_name(config, "index_daily_features")))}
  WHERE return_20d IS NOT NULL
    AND volatility_20 IS NOT NULL
)
SELECT
  index_code,
  date,
  partition_month,
  close,
  return_20d,
  volatility_20,
  amount_ma20_ratio,
  CASE
    WHEN return_20d > 0 AND volatility_20 <= volatility_252_avg THEN 'bull'
    WHEN return_20d < 0 AND volatility_20 > volatility_252_avg THEN 'bear'
    ELSE 'sideways'
  END AS regime_label,
  CASE
    WHEN return_20d > 0 AND volatility_20 <= volatility_252_avg THEN 1.0
    WHEN return_20d < 0 AND volatility_20 > volatility_252_avg THEN 0.0
    ELSE 0.5
  END AS position_pct,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM base
WHERE volatility_252_avg IS NOT NULL
"""


def build_portfolio_risk_snapshot_sql(config: dict) -> str:
    destination_id = table_id(config, ads_table_name(config, "portfolio_risk_snapshot_1d"))
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["asset_type"])
    return f"""{prefix}
SELECT
  date,
  partition_month,
  asset_type,
  COUNT(DISTINCT asset_code) AS asset_count,
  AVG(return_1d) AS avg_return_1d,
  AVG(return_20d) AS avg_return_20d,
  AVG(volatility_20) AS avg_volatility_20,
  COUNTIF(volatility_20 > 0.05) AS high_vol_asset_count,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM {quote_table(table_id(config, dws_table_name(config, "portfolio_asset_returns_1d")))}
WHERE return_1d IS NOT NULL
GROUP BY date, partition_month, asset_type
"""


def build_event_money_flow_signal_sql(config: dict) -> str:
    top_n = int(ads_config(config).get("event_money_flow_top_n", 100))
    destination_id = table_id(config, ads_table_name(config, "signal_event_money_flow_1d"))
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["date", "equity_code"])
    source = table_id(config, dws_table_name(config, "equity_event_money_flow_features_1d"))
    return f"""{prefix}
WITH scored AS (
  SELECT
    equity_code,
    date,
    partition_month,
    net_inflow_amount,
    main_net_inflow_amount,
    extra_large_net_inflow_amount,
    dragon_tiger_net_amount,
    dragon_tiger_department_count,
    is_kpl_event,
    limit_up_streak,
    limit_up_reason,
    tags,
    board_names,
    COALESCE(net_inflow_amount, 0)
      + 0.5 * COALESCE(main_net_inflow_amount, 0)
      + 0.5 * COALESCE(extra_large_net_inflow_amount, 0)
      + COALESCE(dragon_tiger_net_amount, 0) / 10000.0
      + 1000.0 * COALESCE(limit_up_streak, 0)
      + CASE WHEN is_kpl_event THEN 500.0 ELSE 0.0 END AS score_proxy
  FROM {quote_table(source)}
  WHERE equity_code IS NOT NULL
    AND date IS NOT NULL
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (PARTITION BY date ORDER BY score_proxy DESC, equity_code) AS score_rank
  FROM scored
)
SELECT
  equity_code,
  date,
  partition_month,
  net_inflow_amount,
  main_net_inflow_amount,
  extra_large_net_inflow_amount,
  dragon_tiger_net_amount,
  dragon_tiger_department_count,
  is_kpl_event,
  limit_up_streak,
  limit_up_reason,
  tags,
  board_names,
  score_proxy,
  score_rank,
  {top_n} AS top_n,
  score_rank <= {top_n} AS is_selected,
  CASE WHEN score_rank <= {top_n} THEN 1 ELSE 0 END AS signal,
  CURRENT_TIMESTAMP() AS signal_generated_at
FROM ranked
"""


def transform_double_ma_signal(config: dict) -> None:
    _run_query(config, build_double_ma_signal_sql(config), table_id(config, ads_table_name(config, "signal_double_ma_1d")))


def transform_ml_stock_picker_signal(config: dict) -> None:
    _run_query(config, build_ml_stock_picker_signal_sql(config), table_id(config, ads_table_name(config, "signal_ml_stock_picker_1d")))


def transform_volatility_timing_signal(config: dict) -> None:
    _run_query(config, build_volatility_timing_signal_sql(config), table_id(config, ads_table_name(config, "signal_volatility_timing_1d")))


def transform_regime_switching_signal(config: dict) -> None:
    _run_query(config, build_regime_switching_signal_sql(config), table_id(config, ads_table_name(config, "signal_regime_switching_1d")))


def transform_portfolio_risk_snapshot(config: dict) -> None:
    _run_query(config, build_portfolio_risk_snapshot_sql(config), table_id(config, ads_table_name(config, "portfolio_risk_snapshot_1d")))


def transform_event_money_flow_signal(config: dict) -> None:
    _run_query(config, build_event_money_flow_signal_sql(config), table_id(config, ads_table_name(config, "signal_event_money_flow_1d")))


ADS_TRANSFORMS: dict[str, Callable[[dict], None]] = {
    "signal_double_ma_1d": transform_double_ma_signal,
    "signal_ml_stock_picker_1d": transform_ml_stock_picker_signal,
    "signal_volatility_timing_1d": transform_volatility_timing_signal,
    "signal_regime_switching_1d": transform_regime_switching_signal,
    "portfolio_risk_snapshot_1d": transform_portfolio_risk_snapshot,
    "signal_event_money_flow_1d": transform_event_money_flow_signal,
}


ADS_AUDIT_SPECS: dict[str, set[str]] = {
    "ads_signal_double_ma_1d": {"fund_code", "date", "signal", "ma_5", "ma_20"},
    "ads_signal_ml_stock_picker_1d": {"equity_code", "date", "score_proxy", "score_rank", "signal"},
    "ads_signal_volatility_timing_1d": {"index_code", "date", "position_scale", "signal_label"},
    "ads_signal_regime_switching_1d": {"index_code", "date", "regime_label", "position_pct"},
    "ads_portfolio_risk_snapshot_1d": {"date", "asset_type", "asset_count", "avg_volatility_20"},
    "ads_signal_event_money_flow_1d": {"equity_code", "date", "score_proxy", "score_rank", "signal", "is_selected"},
}


def _select(registry: dict, target_table: str | None) -> dict:
    if target_table is None:
        return registry
    key = target_table.removeprefix("ads_")
    if key not in registry:
        raise RuntimeError(f"Unknown ADS target table: {target_table}")
    return {key: registry[key]}


def transform_ads(config: dict, target_table: str | None = None) -> None:
    for transform in _select(ADS_TRANSFORMS, target_table).values():
        transform(config)


def audit_ads(config: dict, target_table: str | None = None) -> None:
    client = bq_client(config)
    specs = ADS_AUDIT_SPECS
    if target_table:
        table_name = target_table if target_table.startswith("ads_") else ads_table_name(config, target_table)
        specs = {table_name: specs[table_name]} if table_name in specs else {}
    if not specs:
        raise RuntimeError(f"No ADS audit spec found for {target_table}")

    errors: list[str] = []
    for table_name, required_fields in specs.items():
        full_id = table_id(config, table_name)
        table = client.get_table(full_id)
        row_count = int(table.num_rows or 0)
        fields = {field.name for field in table.schema}
        absent = required_fields - fields
        status = "pass" if row_count > 0 and not absent else "failed"
        print(f"{full_id}: rows={row_count} fields={len(fields)} status={status}")
        if row_count <= 0:
            errors.append(f"{full_id}: row_count is 0")
        if absent:
            errors.append(f"{full_id}: missing fields {sorted(absent)}")
    if errors:
        raise RuntimeError("ADS audit failed:\n" + "\n".join(errors))
    print(f"ADS audit passed: {len(specs)} tables covered")
