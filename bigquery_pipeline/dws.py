from __future__ import annotations

from collections.abc import Callable

from .client import bq_client, dwd_table_name, dws_table_name, table_id
from .fundamental import audit_equity_fundamental_features, transform_equity_fundamental_features
from .financial import audit_equity_valuation_features, transform_equity_valuation_features
from .sql import create_table_prefix, quote_table, sql_string_list


def build_daily_feature_sql(
    source_id: str,
    destination_id: str,
    code_column: str,
    include_adjust_type: bool,
) -> str:
    adjust_filter = "AND adjust_type = 'qfq'" if include_adjust_type else ""
    adjust_select = "adjust_type," if include_adjust_type else ""
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=[code_column])
    return f"""{prefix}
WITH base AS (
  SELECT
    {code_column},
    date,
    partition_month,
    {adjust_select}
    open,
    high,
    low,
    close,
    volume,
    amount
  FROM {quote_table(source_id)}
  WHERE date IS NOT NULL
    AND {code_column} IS NOT NULL
    AND close IS NOT NULL
    AND SAFE_CAST(close AS FLOAT64) > 0
    {adjust_filter}
),
ordered AS (
  SELECT
    *,
    LAG(close, 1) OVER code_date AS lag_close_1,
    LAG(close, 5) OVER code_date AS lag_close_5,
    LAG(close, 10) OVER code_date AS lag_close_10,
    LAG(close, 20) OVER code_date AS lag_close_20
  FROM base
  WINDOW code_date AS (PARTITION BY {code_column} ORDER BY date)
),
derived AS (
  SELECT
    *,
    SAFE_CAST(close AS FLOAT64) - SAFE_CAST(lag_close_1 AS FLOAT64) AS close_delta,
    LOG(SAFE_CAST(close AS FLOAT64)) - LOG(SAFE_CAST(lag_close_1 AS FLOAT64)) AS return_1d,
    LOG(SAFE_CAST(close AS FLOAT64)) - LOG(SAFE_CAST(lag_close_5 AS FLOAT64)) AS return_5d,
    LOG(SAFE_CAST(close AS FLOAT64)) - LOG(SAFE_CAST(lag_close_10 AS FLOAT64)) AS return_10d,
    LOG(SAFE_CAST(close AS FLOAT64)) - LOG(SAFE_CAST(lag_close_20 AS FLOAT64)) AS return_20d
  FROM ordered
),
rolling_raw AS (
  SELECT
    *,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w5 AS ma_5,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w10 AS ma_10,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w12 AS ma_12,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w20 AS ma_20,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w26 AS ma_26,
    AVG(SAFE_CAST(close AS FLOAT64)) OVER w60 AS ma_60,
    AVG(SAFE_CAST(volume AS FLOAT64)) OVER w5 AS volume_ma_5,
    AVG(SAFE_CAST(volume AS FLOAT64)) OVER w20 AS volume_ma_20,
    AVG(SAFE_CAST(amount AS FLOAT64)) OVER w5 AS amount_ma_5,
    AVG(SAFE_CAST(amount AS FLOAT64)) OVER w20 AS amount_ma_20,
    STDDEV_SAMP(SAFE_CAST(close AS FLOAT64)) OVER w5 AS std_5d,
    STDDEV_SAMP(SAFE_CAST(close AS FLOAT64)) OVER w20 AS std_20d,
    STDDEV_SAMP(return_1d) OVER w20 AS volatility_20,
    MAX(SAFE_CAST(high AS FLOAT64)) OVER w20 AS high_20d,
    MIN(SAFE_CAST(low AS FLOAT64)) OVER w20 AS low_20d,
    AVG(GREATEST(close_delta, 0)) OVER w14 AS avg_gain_14,
    AVG(ABS(LEAST(close_delta, 0))) OVER w14 AS avg_loss_14
  FROM derived
  WINDOW
    w5 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW),
    w10 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW),
    w12 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 11 PRECEDING AND CURRENT ROW),
    w14 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 13 PRECEDING AND CURRENT ROW),
    w20 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW),
    w26 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 25 PRECEDING AND CURRENT ROW),
    w60 AS (PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW)
),
with_macd AS (
  SELECT
    *,
    ma_12 - ma_26 AS macd_diff,
    AVG(ma_12 - ma_26) OVER (
      PARTITION BY {code_column} ORDER BY date ROWS BETWEEN 8 PRECEDING AND CURRENT ROW
    ) AS macd_signal
  FROM rolling_raw
)
SELECT
  {code_column},
  date,
  partition_month,
  {adjust_select}
  open,
  high,
  low,
  close,
  volume,
  amount,
  return_1d,
  return_5d,
  return_10d,
  return_20d,
  ma_5,
  ma_10,
  ma_20,
  ma_60,
  SAFE_DIVIDE(SAFE_CAST(volume AS FLOAT64), NULLIF(volume_ma_5, 0)) AS volume_ma5_ratio,
  SAFE_DIVIDE(SAFE_CAST(volume AS FLOAT64), NULLIF(volume_ma_20, 0)) AS volume_ma20_ratio,
  SAFE_DIVIDE(SAFE_CAST(amount AS FLOAT64), NULLIF(amount_ma_5, 0)) AS amount_ma5_ratio,
  SAFE_DIVIDE(SAFE_CAST(amount AS FLOAT64), NULLIF(amount_ma_20, 0)) AS amount_ma20_ratio,
  std_5d,
  std_20d,
  SAFE_DIVIDE(std_5d, NULLIF(std_20d, 0)) AS std_ratio,
  volatility_20,
  100 - SAFE_DIVIDE(100, 1 + SAFE_DIVIDE(avg_gain_14, NULLIF(avg_loss_14, 0))) AS rsi_14,
  macd_diff,
  macd_signal,
  macd_diff - macd_signal AS macd_hist,
  SAFE_DIVIDE(SAFE_CAST(close AS FLOAT64) - low_20d, NULLIF(high_20d - low_20d, 0)) AS close_to_high_20d,
  SAFE_DIVIDE(SAFE_CAST(close AS FLOAT64), NULLIF(ma_5, 0)) - 1 AS close_to_ma5,
  SAFE_DIVIDE(SAFE_CAST(close AS FLOAT64), NULLIF(ma_20, 0)) - 1 AS close_to_ma20,
  CURRENT_TIMESTAMP() AS feature_generated_at
FROM with_macd
"""


def _run_query(config: dict, sql: str, label: str) -> None:
    client = bq_client(config)
    job = client.query(sql)
    job.result()
    print(f"Transformed {label}; job_id={job.job_id}")


def transform_equity_daily_features(config: dict) -> None:
    _run_query(
        config,
        build_daily_feature_sql(
            table_id(config, dwd_table_name(config, "fact_equity_kline_1d")),
            table_id(config, dws_table_name(config, "equity_daily_features")),
            "equity_code",
            include_adjust_type=True,
        ),
        table_id(config, dws_table_name(config, "equity_daily_features")),
    )


def transform_fund_daily_features(config: dict) -> None:
    _run_query(
        config,
        build_daily_feature_sql(
            table_id(config, dwd_table_name(config, "fact_fund_kline_1d")),
            table_id(config, dws_table_name(config, "fund_daily_features")),
            "fund_code",
            include_adjust_type=True,
        ),
        table_id(config, dws_table_name(config, "fund_daily_features")),
    )


def transform_index_daily_features(config: dict) -> None:
    _run_query(
        config,
        build_daily_feature_sql(
            table_id(config, dwd_table_name(config, "fact_index_kline_1d")),
            table_id(config, dws_table_name(config, "index_daily_features")),
            "index_code",
            include_adjust_type=False,
        ),
        table_id(config, dws_table_name(config, "index_daily_features")),
    )


def build_portfolio_asset_returns_sql(config: dict) -> str:
    destination_id = table_id(config, dws_table_name(config, "portfolio_asset_returns_1d"))
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["asset_type", "asset_code"])
    return f"""{prefix}
SELECT 'equity' AS asset_type, equity_code AS asset_code, date, partition_month, close,
  return_1d, return_5d, return_20d, volatility_20, CURRENT_TIMESTAMP() AS feature_generated_at
FROM {quote_table(table_id(config, dws_table_name(config, "equity_daily_features")))}
UNION ALL
SELECT 'fund' AS asset_type, fund_code AS asset_code, date, partition_month, close,
  return_1d, return_5d, return_20d, volatility_20, CURRENT_TIMESTAMP() AS feature_generated_at
FROM {quote_table(table_id(config, dws_table_name(config, "fund_daily_features")))}
UNION ALL
SELECT 'index' AS asset_type, index_code AS asset_code, date, partition_month, close,
  return_1d, return_5d, return_20d, volatility_20, CURRENT_TIMESTAMP() AS feature_generated_at
FROM {quote_table(table_id(config, dws_table_name(config, "index_daily_features")))}
"""


def transform_portfolio_asset_returns(config: dict) -> None:
    _run_query(config, build_portfolio_asset_returns_sql(config), table_id(config, dws_table_name(config, "portfolio_asset_returns_1d")))


def build_board_component_latest_sql(config: dict) -> str:
    destination_id = table_id(config, dws_table_name(config, "board_component_latest"))
    prefix = create_table_prefix(destination_id, partition=False, cluster_by=["board_code", "equity_code"])
    return f"""{prefix}
SELECT
  board_code,
  equity_code,
  security_code,
  board_name,
  equity_name,
  date AS latest_date,
  CURRENT_TIMESTAMP() AS feature_generated_at
FROM {quote_table(table_id(config, dwd_table_name(config, "fact_board_component_1d")))}
WHERE board_code IS NOT NULL
  AND equity_code IS NOT NULL
QUALIFY ROW_NUMBER() OVER (
  PARTITION BY board_code, equity_code
  ORDER BY date DESC, source_file DESC, source_hash DESC
) = 1
"""


def transform_board_component_latest(config: dict) -> None:
    _run_query(config, build_board_component_latest_sql(config), table_id(config, dws_table_name(config, "board_component_latest")))


def pair_candidate_universe(config: dict) -> list[str]:
    configured = config.get("defaults", {}).get("ads", {}).get("pair_candidate_universe") or []
    return [str(code).strip().upper() for code in configured if str(code).strip()]


def build_pair_candidate_stats_sql(config: dict) -> str:
    destination_id = table_id(config, dws_table_name(config, "pair_candidate_stats"))
    universe = pair_candidate_universe(config)
    if not universe:
        raise RuntimeError("defaults.ads.pair_candidate_universe must not be empty")
    prefix = create_table_prefix(destination_id, partition=False, cluster_by=["code_x", "code_y"])
    return f"""{prefix}
WITH max_date AS (
  SELECT MAX(date) AS end_date
  FROM {quote_table(table_id(config, dws_table_name(config, "equity_daily_features")))}
  WHERE equity_code IN ({sql_string_list(universe)})
),
base AS (
  SELECT
    equity_code,
    date,
    SAFE_CAST(close AS FLOAT64) AS close,
    return_1d
  FROM {quote_table(table_id(config, dws_table_name(config, "equity_daily_features")))}, max_date
  WHERE equity_code IN ({sql_string_list(universe)})
    AND date >= DATE_SUB(end_date, INTERVAL 756 DAY)
    AND close IS NOT NULL
    AND return_1d IS NOT NULL
),
pairs AS (
  SELECT
    x.equity_code AS code_x,
    y.equity_code AS code_y,
    COUNT(*) AS observation_count,
    CORR(x.return_1d, y.return_1d) AS return_corr,
    SAFE_DIVIDE(COVAR_SAMP(x.return_1d, y.return_1d), NULLIF(VAR_SAMP(y.return_1d), 0)) AS beta_xy,
    AVG(LOG(x.close) - LOG(y.close)) AS avg_log_spread,
    STDDEV_SAMP(LOG(x.close) - LOG(y.close)) AS std_log_spread,
    MAX(x.date) AS latest_date
  FROM base AS x
  JOIN base AS y
    ON x.date = y.date
   AND x.equity_code < y.equity_code
  GROUP BY code_x, code_y
)
SELECT *, CURRENT_TIMESTAMP() AS feature_generated_at
FROM pairs
WHERE observation_count >= 120
"""


def transform_pair_candidate_stats(config: dict) -> None:
    _run_query(config, build_pair_candidate_stats_sql(config), table_id(config, dws_table_name(config, "pair_candidate_stats")))


def build_event_money_flow_features_sql(config: dict) -> str:
    destination_id = table_id(config, dws_table_name(config, "equity_event_money_flow_features_1d"))
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["equity_code"])
    money = table_id(config, dwd_table_name(config, "fact_money_flow_1d"))
    dragon = table_id(config, dwd_table_name(config, "fact_dragon_tiger_seat_1d"))
    kpl = table_id(config, dwd_table_name(config, "fact_kpl_board_1d"))
    return f"""{prefix}
WITH money AS (
  SELECT
    equity_code,
    date,
    ANY_VALUE(partition_month) AS partition_month,
    SUM(SAFE_CAST(net_inflow_amount AS FLOAT64)) AS net_inflow_amount,
    SUM(SAFE_CAST(main_net_inflow_amount AS FLOAT64)) AS main_net_inflow_amount,
    SUM(SAFE_CAST(extra_large_net_inflow_amount AS FLOAT64)) AS extra_large_net_inflow_amount
  FROM {quote_table(money)}
  WHERE equity_code IS NOT NULL AND date IS NOT NULL
  GROUP BY equity_code, date
),
dragon AS (
  SELECT
    equity_code,
    date,
    ANY_VALUE(partition_month) AS partition_month,
    SUM(SAFE_CAST(buy_amount AS FLOAT64)) AS dragon_tiger_buy_amount,
    SUM(SAFE_CAST(sell_amount AS FLOAT64)) AS dragon_tiger_sell_amount,
    SUM(SAFE_CAST(net_amount AS FLOAT64)) AS dragon_tiger_net_amount,
    COUNT(*) AS dragon_tiger_row_count,
    COUNT(DISTINCT department_name) AS dragon_tiger_department_count
  FROM {quote_table(dragon)}
  WHERE equity_code IS NOT NULL AND date IS NOT NULL
  GROUP BY equity_code, date
),
kpl AS (
  SELECT
    equity_code,
    date,
    ANY_VALUE(partition_month) AS partition_month,
    TRUE AS is_kpl_event,
    MAX(SAFE_CAST(limit_up_streak AS FLOAT64)) AS limit_up_streak,
    SUM(SAFE_CAST(main_net_amount AS FLOAT64)) AS kpl_main_net_amount,
    ANY_VALUE(limit_up_reason) AS limit_up_reason,
    ANY_VALUE(tags) AS tags,
    ANY_VALUE(board_names) AS board_names
  FROM {quote_table(kpl)}
  WHERE equity_code IS NOT NULL AND date IS NOT NULL
  GROUP BY equity_code, date
),
keys AS (
  SELECT equity_code, date FROM money
  UNION DISTINCT SELECT equity_code, date FROM dragon
  UNION DISTINCT SELECT equity_code, date FROM kpl
)
SELECT
  keys.equity_code,
  keys.date,
  COALESCE(money.partition_month, dragon.partition_month, kpl.partition_month) AS partition_month,
  money.net_inflow_amount,
  money.main_net_inflow_amount,
  money.extra_large_net_inflow_amount,
  dragon.dragon_tiger_buy_amount,
  dragon.dragon_tiger_sell_amount,
  dragon.dragon_tiger_net_amount,
  dragon.dragon_tiger_row_count,
  dragon.dragon_tiger_department_count,
  COALESCE(kpl.is_kpl_event, FALSE) AS is_kpl_event,
  kpl.limit_up_streak,
  kpl.kpl_main_net_amount,
  kpl.limit_up_reason,
  kpl.tags,
  kpl.board_names,
  CURRENT_TIMESTAMP() AS feature_generated_at
FROM keys
LEFT JOIN money USING (equity_code, date)
LEFT JOIN dragon USING (equity_code, date)
LEFT JOIN kpl USING (equity_code, date)
"""


def transform_event_money_flow_features(config: dict) -> None:
    _run_query(
        config,
        build_event_money_flow_features_sql(config),
        table_id(config, dws_table_name(config, "equity_event_money_flow_features_1d")),
    )


DWS_TRANSFORMS: dict[str, Callable[[dict], None]] = {
    "equity_daily_features": transform_equity_daily_features,
    "fund_daily_features": transform_fund_daily_features,
    "index_daily_features": transform_index_daily_features,
    "portfolio_asset_returns_1d": transform_portfolio_asset_returns,
    "board_component_latest": transform_board_component_latest,
    "pair_candidate_stats": transform_pair_candidate_stats,
    "equity_valuation_features": transform_equity_valuation_features,
    "equity_fundamental_features": transform_equity_fundamental_features,
    "equity_event_money_flow_features_1d": transform_event_money_flow_features,
}


DWS_AUDIT_SPECS: dict[str, set[str]] = {
    "dws_equity_daily_features": {"equity_code", "date", "partition_month", "return_20d", "rsi_14", "macd_hist"},
    "dws_fund_daily_features": {"fund_code", "date", "partition_month", "ma_5", "ma_20", "return_20d"},
    "dws_index_daily_features": {"index_code", "date", "partition_month", "return_20d", "volatility_20"},
    "dws_portfolio_asset_returns_1d": {"asset_type", "asset_code", "date", "return_1d", "volatility_20"},
    "dws_board_component_latest": {"board_code", "equity_code", "latest_date"},
    "dws_pair_candidate_stats": {"code_x", "code_y", "observation_count", "return_corr", "beta_xy"},
    "dws_equity_event_money_flow_features_1d": {"equity_code", "date", "net_inflow_amount", "dragon_tiger_net_amount", "is_kpl_event", "limit_up_streak"},
}


SPECIAL_DWS_AUDITS: dict[str, Callable[[dict], dict]] = {
    "equity_valuation_features": audit_equity_valuation_features,
    "equity_fundamental_features": audit_equity_fundamental_features,
}


def _select(registry: dict, target_table: str | None, layer: str) -> dict:
    if target_table is None:
        return registry
    key = target_table.removeprefix(f"{layer}_")
    if key not in registry:
        raise RuntimeError(f"Unknown {layer.upper()} target table: {target_table}")
    return {key: registry[key]}


def transform_dws(config: dict, target_table: str | None = None) -> None:
    for transform in _select(DWS_TRANSFORMS, target_table, "dws").values():
        transform(config)


def audit_dws(config: dict, target_table: str | None = None) -> None:
    if target_table and target_table.removeprefix("dws_") in SPECIAL_DWS_AUDITS:
        SPECIAL_DWS_AUDITS[target_table.removeprefix("dws_")](config)
        return
    if target_table is None:
        for audit in SPECIAL_DWS_AUDITS.values():
            audit(config)

    client = bq_client(config)
    specs = DWS_AUDIT_SPECS
    if target_table:
        table_name = target_table if target_table.startswith("dws_") else dws_table_name(config, target_table)
        specs = {table_name: specs[table_name]} if table_name in specs else {}
    if not specs:
        if target_table:
            raise RuntimeError(f"No DWS audit spec found for {target_table}")
        return

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
        raise RuntimeError("DWS audit failed:\n" + "\n".join(errors))
    print(f"DWS audit passed: {len(specs) + (0 if target_table else len(SPECIAL_DWS_AUDITS))} tables covered")


def table_exists(config: dict, target_table: str) -> bool:
    client = bq_client(config)
    table_name = target_table if target_table.startswith("dws_") else dws_table_name(config, target_table)
    try:
        client.get_table(table_id(config, table_name))
        return True
    except Exception:
        return False
