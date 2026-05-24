from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

from .client import bq_client, dwd_table_name, dws_table_name, require_bigquery, storage_client, table_id
from .sql import (
    coerce_date_value,
    coerce_report_period,
    create_table_prefix,
    normalize_security_code,
    partition_month_from_date,
    quote_table,
)


INCOME_FIELD_CANDIDATES: dict[str, list[str]] = {
    "equity_code": ["equity_code", "security_code", "股票代码", "证券代码", "代码"],
    "announcement_date": ["actual_announcement_date", "announcement_date", "实际公告日期", "公告日期"],
    "report_period": ["report_period", "报告期"],
    "eps_basic": ["eps_basic", "基本每股收益"],
    "eps_diluted": ["eps_diluted", "稀释每股收益"],
    "total_revenue": ["total_revenue", "营业总收入"],
    "operating_revenue": ["operating_revenue", "营业收入"],
    "operating_cost": ["operating_cost", "减:营业成本"],
    "total_operating_cost": ["total_operating_cost", "营业总成本"],
    "selling_expense": ["selling_expense", "减:销售费用"],
    "admin_expense": ["admin_expense", "减:管理费用"],
    "rd_expense": ["rd_expense", "研发费用"],
    "finance_expense": ["finance_expense", "减:财务费用"],
    "operating_profit": ["operating_profit", "营业利润"],
    "total_profit": ["total_profit", "利润总额"],
    "net_profit": ["net_profit", "净利润(含少数股东损益)"],
    "net_profit_parent": ["net_profit_parent", "净利润(不含少数股东损益)"],
    "ebit": ["ebit", "息税前利润"],
    "ebitda": ["ebitda", "息税折旧摊销前利润"],
    "source_entry": ["source_entry"],
}

BALANCE_FIELD_CANDIDATES: dict[str, list[str]] = {
    "equity_code": ["equity_code", "security_code", "股票代码", "证券代码", "代码"],
    "announcement_date": ["actual_announcement_date", "announcement_date", "实际公告日期", "公告日期"],
    "report_period": ["report_period", "报告期"],
    "total_share": ["total_share", "期末总股本"],
    "capital_reserve": ["capital_reserve", "资本公积金"],
    "retained_earnings": ["retained_earnings", "未分配利润"],
    "surplus_reserve": ["surplus_reserve", "盈余公积金"],
    "total_equity_parent": ["total_equity_parent", "股东权益合计(不含少数股东权益)"],
    "total_equity": ["total_equity", "股东权益合计(含少数股东权益)"],
    "monetary_fund": ["monetary_fund", "货币资金"],
    "accounts_receivable": ["accounts_receivable", "应收账款", "应收票据及应收账款"],
    "inventory": ["inventory", "存货"],
    "total_current_assets": ["total_current_assets", "流动资产合计"],
    "fixed_assets": ["fixed_assets", "固定资产(合计)", "固定资产"],
    "goodwill": ["goodwill", "商誉"],
    "total_assets": ["total_assets", "资产总计"],
    "short_term_borrowing": ["short_term_borrowing", "短期借款"],
    "accounts_payable": ["accounts_payable", "应付账款", "应付票据及应付账款"],
    "total_current_liabilities": ["total_current_liabilities", "流动负债合计"],
    "long_term_borrowing": ["long_term_borrowing", "长期借款"],
    "total_noncurrent_liabilities": ["total_noncurrent_liabilities", "非流动负债合计"],
    "total_liabilities": ["total_liabilities", "负债合计"],
    "source_entry": ["source_entry"],
}

INCOME_NUMERIC_FIELDS = [
    "eps_basic",
    "eps_diluted",
    "total_revenue",
    "operating_revenue",
    "operating_cost",
    "total_operating_cost",
    "selling_expense",
    "admin_expense",
    "rd_expense",
    "finance_expense",
    "operating_profit",
    "total_profit",
    "net_profit",
    "net_profit_parent",
    "ebit",
    "ebitda",
]

BALANCE_NUMERIC_FIELDS = [
    "total_share",
    "capital_reserve",
    "retained_earnings",
    "surplus_reserve",
    "total_equity_parent",
    "total_equity",
    "monetary_fund",
    "accounts_receivable",
    "inventory",
    "total_current_assets",
    "fixed_assets",
    "goodwill",
    "total_assets",
    "short_term_borrowing",
    "accounts_payable",
    "total_current_liabilities",
    "long_term_borrowing",
    "total_noncurrent_liabilities",
    "total_liabilities",
]


def chunked(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def fundamental_source_prefix(config: dict, source_table: str) -> str:
    gcs = config["gcs"]
    return f"{str(gcs['prefix']).strip('/')}/{source_table}/"


def list_source_parquet_blobs(config: dict, source_table: str):
    client = storage_client(config)
    bucket = client.bucket(config["gcs"]["bucket"])
    prefix = fundamental_source_prefix(config, source_table)
    blobs = [
        blob
        for blob in client.list_blobs(bucket, prefix=prefix)
        if blob.name.endswith(".parquet") and not blob.name.rsplit("/", 1)[-1].startswith("_")
    ]
    blobs.sort(key=lambda blob: blob.name)
    max_files = config.get("fundamental", {}).get("max_files")
    if max_files:
        blobs = blobs[: int(max_files)]
    if not blobs:
        raise RuntimeError(f"No parquet files found under gs://{bucket.name}/{prefix}")
    return blobs


def _existing_columns(schema_names: list[str], field_candidates: dict[str, list[str]]) -> list[str]:
    wanted: list[str] = []
    for candidates in field_candidates.values():
        for candidate in candidates:
            if candidate in schema_names and candidate not in wanted:
                wanted.append(candidate)
                break
    return wanted


def _first_existing(frame: pd.DataFrame, candidates: list[str]) -> pd.Series:
    for candidate in candidates:
        if candidate in frame.columns:
            return frame[candidate]
    return pd.Series([pd.NA] * len(frame), index=frame.index)


def _normalize_statement_frame(
    frame: pd.DataFrame,
    source_uri: str,
    field_candidates: dict[str, list[str]],
    numeric_fields: list[str],
) -> pd.DataFrame:
    result = pd.DataFrame(index=frame.index)
    result["equity_code"] = _first_existing(frame, field_candidates["equity_code"]).map(normalize_security_code)
    result["announcement_date"] = _first_existing(frame, field_candidates["announcement_date"]).map(coerce_date_value)
    result["report_period"] = _first_existing(frame, field_candidates["report_period"]).map(coerce_report_period)
    result["partition_month"] = result["announcement_date"].map(partition_month_from_date).astype("Int64")
    for field in numeric_fields:
        result[field] = pd.to_numeric(_first_existing(frame, field_candidates[field]), errors="coerce")
    result["source_file"] = source_uri
    result["source_entry"] = _first_existing(frame, field_candidates["source_entry"]).astype("string")
    hash_frame = result[["equity_code", "announcement_date", "report_period", "source_file", "source_entry"]].astype("string").fillna("")
    result["source_hash"] = [
        hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()
        for values in hash_frame.itertuples(index=False, name=None)
    ]
    result["ingested_at"] = pd.Timestamp.utcnow()
    result = result[result["equity_code"].notna() | result["announcement_date"].notna() | result["report_period"].notna()]
    return result.reset_index(drop=True)


def normalize_income_frame(frame: pd.DataFrame, source_uri: str) -> pd.DataFrame:
    return _normalize_statement_frame(frame, source_uri, INCOME_FIELD_CANDIDATES, INCOME_NUMERIC_FIELDS)


def normalize_balance_frame(frame: pd.DataFrame, source_uri: str) -> pd.DataFrame:
    return _normalize_statement_frame(frame, source_uri, BALANCE_FIELD_CANDIDATES, BALANCE_NUMERIC_FIELDS)


def read_statement_blob(blob, field_candidates: dict[str, list[str]], normalizer) -> pd.DataFrame:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Missing dependency: pyarrow. Install dependencies with: python -m pip install -r requirements.txt") from exc

    source_uri = f"gs://{blob.bucket.name}/{blob.name}"
    with tempfile.TemporaryDirectory(prefix="ashare_fundamental_") as tmpdir:
        local_path = Path(tmpdir) / Path(blob.name).name
        blob.download_to_filename(local_path)
        parquet_file = pq.ParquetFile(local_path)
        columns = _existing_columns(parquet_file.schema_arrow.names, field_candidates)
        if not columns:
            return pd.DataFrame()
        table = parquet_file.read(columns=columns)
        return normalizer(table.to_pandas(), source_uri)


def _schema_for(fields: list[str]):
    bigquery = require_bigquery()
    schema = [
        bigquery.SchemaField("equity_code", "STRING"),
        bigquery.SchemaField("announcement_date", "DATE"),
        bigquery.SchemaField("report_period", "STRING"),
        bigquery.SchemaField("partition_month", "INT64"),
    ]
    schema.extend(bigquery.SchemaField(field, "FLOAT64") for field in fields)
    schema.extend(
        [
            bigquery.SchemaField("source_file", "STRING"),
            bigquery.SchemaField("source_entry", "STRING"),
            bigquery.SchemaField("source_hash", "STRING"),
            bigquery.SchemaField("ingested_at", "TIMESTAMP"),
        ]
    )
    return schema


def _ddl_for(config: dict, table_name: str, fields: list[str]) -> str:
    destination = table_id(config, table_name)
    numeric_columns = ",\n  ".join(f"{field} FLOAT64" for field in fields)
    return f"""
CREATE OR REPLACE TABLE {quote_table(destination)} (
  equity_code STRING,
  announcement_date DATE,
  report_period STRING,
  partition_month INT64,
  {numeric_columns},
  source_file STRING,
  source_entry STRING,
  source_hash STRING,
  ingested_at TIMESTAMP
)
PARTITION BY RANGE_BUCKET(partition_month, GENERATE_ARRAY(199001, 210001, 100))
CLUSTER BY equity_code
"""


def _load_core_table(
    config: dict,
    source_table: str,
    destination_table: str,
    field_candidates: dict[str, list[str]],
    numeric_fields: list[str],
    normalizer,
) -> None:
    bigquery = require_bigquery()
    client = bq_client(config)
    destination = table_id(config, destination_table)
    client.delete_table(destination, not_found_ok=True)
    client.query(_ddl_for(config, destination_table, numeric_fields)).result()

    blobs = list_source_parquet_blobs(config, source_table)
    batch_size = int(config.get("fundamental", {}).get("batch_files", 400))
    workers = int(config.get("fundamental", {}).get("parallel_read_workers", 16))
    total_rows = 0
    for batch_index, blob_batch in enumerate(chunked(blobs, batch_size), start=1):
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                frames = list(
                    executor.map(
                        lambda blob: read_statement_blob(blob, field_candidates, normalizer),
                        blob_batch,
                    )
                )
        else:
            frames = [read_statement_blob(blob, field_candidates, normalizer) for blob in blob_batch]
        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            print(f"{destination_table} batch {batch_index}: no rows", flush=True)
            continue
        batch = pd.concat(frames, ignore_index=True)
        job_config = bigquery.LoadJobConfig(
            schema=_schema_for(numeric_fields),
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        )
        job = client.load_table_from_dataframe(batch, destination, job_config=job_config)
        job.result()
        total_rows += len(batch)
        print(
            f"Loaded {destination_table} batch {batch_index}: files={len(blob_batch)} "
            f"rows={len(batch)} total_rows={total_rows} job_id={job.job_id}",
            flush=True,
        )
    if total_rows == 0:
        raise RuntimeError(f"No rows loaded into {destination_table}")
    print(f"Loaded {total_rows} rows into {destination}", flush=True)


def load_income_statement_core(config: dict) -> None:
    _load_core_table(
        config,
        "fact_income_statement",
        dwd_table_name(config, "fact_income_statement_core"),
        INCOME_FIELD_CANDIDATES,
        INCOME_NUMERIC_FIELDS,
        normalize_income_frame,
    )


def load_balance_sheet_core(config: dict) -> None:
    _load_core_table(
        config,
        "fact_balance_sheet",
        dwd_table_name(config, "fact_balance_sheet_core"),
        BALANCE_FIELD_CANDIDATES,
        BALANCE_NUMERIC_FIELDS,
        normalize_balance_frame,
    )


def repair_fundamental_inputs(config: dict) -> None:
    load_income_statement_core(config)
    load_balance_sheet_core(config)


def _query_single_row(config: dict, sql: str) -> dict:
    client = bq_client(config)
    bq_row = next(iter(client.query(sql).result()))
    return {key: bq_row[key] for key in bq_row.keys()}


def audit_income_statement_core(config: dict) -> dict:
    full_id = table_id(config, dwd_table_name(config, "fact_income_statement_core"))
    row = _query_single_row(
        config,
        f"""
SELECT
  COUNT(*) AS row_count,
  COUNTIF(equity_code IS NOT NULL) AS code_rows,
  COUNTIF(announcement_date IS NOT NULL) AS announcement_rows,
  COUNTIF(report_period IS NOT NULL) AS report_period_rows,
  COUNTIF(total_revenue IS NOT NULL OR net_profit_parent IS NOT NULL) AS metric_rows
FROM {quote_table(full_id)}
""",
    )
    _raise_if_bad(row, "income statement core")
    print(f"{full_id}: {row}")
    return row


def audit_balance_sheet_core(config: dict) -> dict:
    full_id = table_id(config, dwd_table_name(config, "fact_balance_sheet_core"))
    row = _query_single_row(
        config,
        f"""
SELECT
  COUNT(*) AS row_count,
  COUNTIF(equity_code IS NOT NULL) AS code_rows,
  COUNTIF(announcement_date IS NOT NULL) AS announcement_rows,
  COUNTIF(report_period IS NOT NULL) AS report_period_rows,
  COUNTIF(total_assets IS NOT NULL OR total_liabilities IS NOT NULL) AS metric_rows
FROM {quote_table(full_id)}
""",
    )
    _raise_if_bad(row, "balance sheet core")
    print(f"{full_id}: {row}")
    return row


def audit_fundamental_inputs(config: dict) -> None:
    audit_income_statement_core(config)
    audit_balance_sheet_core(config)


def _raise_if_bad(row: dict, label: str) -> None:
    errors = []
    for field in ("row_count", "code_rows", "announcement_rows", "report_period_rows", "metric_rows"):
        if int(row[field] or 0) <= 0:
            errors.append(f"{field} is 0")
    if errors:
        raise RuntimeError(f"{label} audit failed: " + "; ".join(errors))


def _interval_cte(source_name: str, table_name: str, value_columns: list[str], config: dict) -> str:
    full_id = table_id(config, table_name)
    select_columns = ",\n    ".join(value_columns)
    return f"""
{source_name}_dedup AS (
  SELECT
    equity_code,
    announcement_date,
    report_period,
    {select_columns}
  FROM {quote_table(full_id)}
  WHERE equity_code IS NOT NULL
    AND announcement_date IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY equity_code, announcement_date
    ORDER BY report_period DESC, source_file DESC, source_hash DESC
  ) = 1
),
{source_name}_intervals AS (
  SELECT
    *,
    LEAD(announcement_date) OVER (
      PARTITION BY equity_code
      ORDER BY announcement_date, report_period
    ) AS next_announcement_date
  FROM {source_name}_dedup
)"""


def build_equity_fundamental_features_sql(config: dict) -> str:
    destination = table_id(config, dws_table_name(config, "equity_fundamental_features"))
    price_table = table_id(config, dwd_table_name(config, "fact_equity_kline_1d"))
    financial_table = dwd_table_name(config, "fact_financial_indicator")
    income_table = dwd_table_name(config, "fact_income_statement_core")
    balance_table = dwd_table_name(config, "fact_balance_sheet_core")
    prefix = create_table_prefix(destination, partition=True, partition_field="partition_month", cluster_by=["equity_code"])
    financial_columns = [
        "eps_basic",
        "eps_diluted",
        "bps",
        "ocfps",
        "gross_margin",
        "net_margin",
        "roe",
        "roe_weighted",
        "debt_to_assets",
        "asset_turnover",
        "current_ratio",
        "quick_ratio",
    ]
    income_columns = INCOME_NUMERIC_FIELDS
    balance_columns = BALANCE_NUMERIC_FIELDS
    return f"""{prefix}
WITH
{_interval_cte("financial", financial_table, financial_columns, config)},
{_interval_cte("income", income_table, income_columns, config)},
{_interval_cte("balance", balance_table, balance_columns, config)},
price_base AS (
  SELECT
    equity_code,
    date,
    partition_month,
    SAFE_CAST(close AS FLOAT64) AS close,
    SAFE_CAST(volume AS FLOAT64) AS volume,
    SAFE_CAST(amount AS FLOAT64) AS amount
  FROM {quote_table(price_table)}
  WHERE equity_code IS NOT NULL
    AND date IS NOT NULL
    AND close IS NOT NULL
    AND SAFE_CAST(close AS FLOAT64) > 0
    AND adjust_type = 'qfq'
)
SELECT
  p.equity_code,
  p.date,
  p.partition_month,
  p.close,
  p.volume,
  p.amount,
  f.announcement_date AS financial_announcement_date,
  f.report_period AS financial_report_period,
  i.announcement_date AS income_announcement_date,
  i.report_period AS income_report_period,
  b.announcement_date AS balance_announcement_date,
  b.report_period AS balance_report_period,
  f.eps_basic,
  f.eps_diluted,
  f.bps,
  f.ocfps,
  f.gross_margin,
  f.net_margin,
  f.roe,
  f.roe_weighted,
  f.debt_to_assets,
  f.asset_turnover,
  f.current_ratio,
  f.quick_ratio,
  i.total_revenue,
  i.operating_revenue,
  i.operating_cost,
  i.total_operating_cost,
  i.selling_expense,
  i.admin_expense,
  i.rd_expense,
  i.finance_expense,
  i.operating_profit,
  i.total_profit,
  i.net_profit,
  i.net_profit_parent,
  i.ebit,
  i.ebitda,
  b.total_share,
  b.capital_reserve,
  b.retained_earnings,
  b.surplus_reserve,
  b.total_equity_parent,
  b.total_equity,
  b.monetary_fund,
  b.accounts_receivable,
  b.inventory,
  b.total_current_assets,
  b.fixed_assets,
  b.goodwill,
  b.total_assets,
  b.short_term_borrowing,
  b.accounts_payable,
  b.total_current_liabilities,
  b.long_term_borrowing,
  b.total_noncurrent_liabilities,
  b.total_liabilities,
  SAFE_DIVIDE(p.close, NULLIF(f.eps_basic, 0)) AS pe_basic,
  SAFE_DIVIDE(p.close, NULLIF(f.bps, 0)) AS pb,
  p.close * b.total_share AS market_cap,
  SAFE_DIVIDE(i.operating_revenue - i.operating_cost, NULLIF(i.operating_revenue, 0)) AS gross_margin_from_income,
  SAFE_DIVIDE(i.net_profit_parent, NULLIF(i.operating_revenue, 0)) AS net_margin_from_income,
  SAFE_DIVIDE(b.total_liabilities, NULLIF(b.total_assets, 0)) AS debt_to_assets_from_balance,
  SAFE_DIVIDE(b.total_current_assets, NULLIF(b.total_current_liabilities, 0)) AS current_ratio_from_balance,
  SAFE_DIVIDE(i.operating_revenue, NULLIF(b.total_assets, 0)) AS asset_turnover_from_income_balance,
  CURRENT_TIMESTAMP() AS feature_generated_at
FROM price_base AS p
LEFT JOIN financial_intervals AS f
  ON p.equity_code = f.equity_code
 AND p.date >= f.announcement_date
 AND (f.next_announcement_date IS NULL OR p.date < f.next_announcement_date)
LEFT JOIN income_intervals AS i
  ON p.equity_code = i.equity_code
 AND p.date >= i.announcement_date
 AND (i.next_announcement_date IS NULL OR p.date < i.next_announcement_date)
LEFT JOIN balance_intervals AS b
  ON p.equity_code = b.equity_code
 AND p.date >= b.announcement_date
 AND (b.next_announcement_date IS NULL OR p.date < b.next_announcement_date)
WHERE f.announcement_date IS NOT NULL
   OR i.announcement_date IS NOT NULL
   OR b.announcement_date IS NOT NULL
"""


def transform_equity_fundamental_features(config: dict) -> None:
    client = bq_client(config)
    destination = table_id(config, dws_table_name(config, "equity_fundamental_features"))
    job = client.query(build_equity_fundamental_features_sql(config))
    job.result()
    print(f"Transformed {destination}; job_id={job.job_id}")


def audit_equity_fundamental_features(config: dict) -> dict:
    full_id = table_id(config, dws_table_name(config, "equity_fundamental_features"))
    row = _query_single_row(
        config,
        f"""
SELECT
  COUNT(*) AS row_count,
  COUNTIF(pe_basic IS NOT NULL) AS pe_rows,
  COUNTIF(pb IS NOT NULL) AS pb_rows,
  COUNTIF(total_revenue IS NOT NULL) AS revenue_rows,
  COUNTIF(total_assets IS NOT NULL) AS asset_rows,
  COUNTIF(market_cap IS NOT NULL) AS market_cap_rows
FROM {quote_table(full_id)}
""",
    )
    errors = []
    for field in ("row_count", "pe_rows", "pb_rows", "revenue_rows", "asset_rows", "market_cap_rows"):
        if int(row[field] or 0) <= 0:
            errors.append(f"{field} is 0")
    print(f"{full_id}: {row}")
    if errors:
        raise RuntimeError("Equity fundamental feature audit failed: " + "; ".join(errors))
    return row
