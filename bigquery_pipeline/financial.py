from __future__ import annotations

import hashlib
import tempfile
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterable
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


FINANCIAL_FIELD_CANDIDATES: dict[str, list[str]] = {
    "equity_code": ["equity_code", "security_code", "股票代码", "证券代码", "代码"],
    "announcement_date": ["announcement_date", "actual_announcement_date", "实际公告日期", "公告日期"],
    "report_period": ["report_period", "报告期"],
    "eps_basic": ["eps_basic", "基本每股收益"],
    "eps_diluted": ["eps_diluted", "稀释每股收益"],
    "revenue_per_share": ["revenue_per_share", "每股营业总收入", "每股营业收入"],
    "bps": ["bps", "book_value_per_share", "每股净资产"],
    "ocfps": ["ocfps", "每股经营现金流", "每股经营活动产生的现金流量净额"],
    "gross_margin": ["gross_margin", "销售毛利率"],
    "net_margin": ["net_margin", "销售净利率"],
    "roe": ["roe", "净资产收益率"],
    "roe_weighted": ["roe_weighted", "加权平均净资产收益率"],
    "debt_to_assets": ["debt_to_assets", "资产负债率"],
    "asset_turnover": ["asset_turnover", "总资产周转率"],
    "current_ratio": ["current_ratio", "流动比率"],
    "quick_ratio": ["quick_ratio", "速动比率"],
    "source_entry": ["source_entry"],
}

NUMERIC_FIELDS = [
    "eps_basic",
    "eps_diluted",
    "revenue_per_share",
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


def financial_source_prefix(config: dict) -> str:
    gcs = config["gcs"]
    root_prefix = str(gcs["prefix"]).strip("/")
    table_name = config.get("financial_indicator", {}).get("source_table", "fact_financial_indicator")
    return f"{root_prefix}/{table_name}/"


def list_financial_parquet_blobs(config: dict):
    client = storage_client(config)
    bucket = client.bucket(config["gcs"]["bucket"])
    prefix = financial_source_prefix(config)
    blobs = [
        blob
        for blob in client.list_blobs(bucket, prefix=prefix)
        if blob.name.endswith(".parquet") and not blob.name.rsplit("/", 1)[-1].startswith("_")
    ]
    blobs.sort(key=lambda blob: blob.name)
    max_files = config.get("financial_indicator", {}).get("max_files")
    if max_files:
        blobs = blobs[: int(max_files)]
    if not blobs:
        raise RuntimeError(f"No financial indicator parquet files found under gs://{bucket.name}/{prefix}")
    return blobs


def chunked(items: list, size: int) -> Iterable[list]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _existing_columns(schema_names: list[str]) -> list[str]:
    wanted: list[str] = []
    for candidates in FINANCIAL_FIELD_CANDIDATES.values():
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


def normalize_financial_frame(frame: pd.DataFrame, source_uri: str) -> pd.DataFrame:
    result = pd.DataFrame(index=frame.index)
    result["equity_code"] = _first_existing(frame, FINANCIAL_FIELD_CANDIDATES["equity_code"]).map(normalize_security_code)
    result["announcement_date"] = _first_existing(frame, FINANCIAL_FIELD_CANDIDATES["announcement_date"]).map(coerce_date_value)
    result["report_period"] = _first_existing(frame, FINANCIAL_FIELD_CANDIDATES["report_period"]).map(coerce_report_period)
    result["partition_month"] = result["announcement_date"].map(partition_month_from_date).astype("Int64")
    for field in NUMERIC_FIELDS:
        result[field] = pd.to_numeric(_first_existing(frame, FINANCIAL_FIELD_CANDIDATES[field]), errors="coerce")
    result["source_file"] = source_uri
    result["source_entry"] = _first_existing(frame, FINANCIAL_FIELD_CANDIDATES["source_entry"]).astype("string")
    hash_frame = result[["equity_code", "announcement_date", "report_period", "source_file", "source_entry"]].astype("string").fillna("")
    result["source_hash"] = [
        hashlib.sha256("|".join(values).encode("utf-8")).hexdigest()
        for values in hash_frame.itertuples(index=False, name=None)
    ]
    result["ingested_at"] = pd.Timestamp.utcnow()
    result = result[result["equity_code"].notna() | result["announcement_date"].notna() | result["report_period"].notna()]
    return result.reset_index(drop=True)


def read_financial_blob(blob) -> pd.DataFrame:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise RuntimeError("Missing dependency: pyarrow. Install dependencies with: python -m pip install -r requirements.txt") from exc

    source_uri = f"gs://{blob.bucket.name}/{blob.name}"
    with tempfile.TemporaryDirectory(prefix="ashare_financial_") as tmpdir:
        local_path = Path(tmpdir) / Path(blob.name).name
        blob.download_to_filename(local_path)
        parquet_file = pq.ParquetFile(local_path)
        columns = _existing_columns(parquet_file.schema_arrow.names)
        if not columns:
            return pd.DataFrame()
        table = parquet_file.read(columns=columns)
        return normalize_financial_frame(table.to_pandas(), source_uri)


def financial_table_ddl(config: dict) -> str:
    destination = table_id(config, dwd_table_name(config, "fact_financial_indicator"))
    return f"""
CREATE OR REPLACE TABLE {quote_table(destination)} (
  equity_code STRING,
  announcement_date DATE,
  report_period STRING,
  partition_month INT64,
  eps_basic FLOAT64,
  eps_diluted FLOAT64,
  revenue_per_share FLOAT64,
  bps FLOAT64,
  ocfps FLOAT64,
  gross_margin FLOAT64,
  net_margin FLOAT64,
  roe FLOAT64,
  roe_weighted FLOAT64,
  debt_to_assets FLOAT64,
  asset_turnover FLOAT64,
  current_ratio FLOAT64,
  quick_ratio FLOAT64,
  source_file STRING,
  source_entry STRING,
  source_hash STRING,
  ingested_at TIMESTAMP
)
PARTITION BY RANGE_BUCKET(partition_month, GENERATE_ARRAY(199001, 210001, 100))
CLUSTER BY equity_code
"""


def financial_load_schema():
    bigquery = require_bigquery()
    return [
        bigquery.SchemaField("equity_code", "STRING"),
        bigquery.SchemaField("announcement_date", "DATE"),
        bigquery.SchemaField("report_period", "STRING"),
        bigquery.SchemaField("partition_month", "INT64"),
        bigquery.SchemaField("eps_basic", "FLOAT64"),
        bigquery.SchemaField("eps_diluted", "FLOAT64"),
        bigquery.SchemaField("revenue_per_share", "FLOAT64"),
        bigquery.SchemaField("bps", "FLOAT64"),
        bigquery.SchemaField("ocfps", "FLOAT64"),
        bigquery.SchemaField("gross_margin", "FLOAT64"),
        bigquery.SchemaField("net_margin", "FLOAT64"),
        bigquery.SchemaField("roe", "FLOAT64"),
        bigquery.SchemaField("roe_weighted", "FLOAT64"),
        bigquery.SchemaField("debt_to_assets", "FLOAT64"),
        bigquery.SchemaField("asset_turnover", "FLOAT64"),
        bigquery.SchemaField("current_ratio", "FLOAT64"),
        bigquery.SchemaField("quick_ratio", "FLOAT64"),
        bigquery.SchemaField("source_file", "STRING"),
        bigquery.SchemaField("source_entry", "STRING"),
        bigquery.SchemaField("source_hash", "STRING"),
        bigquery.SchemaField("ingested_at", "TIMESTAMP"),
    ]


def load_financial_indicator(config: dict) -> None:
    bigquery = require_bigquery()
    client = bq_client(config)
    destination = table_id(config, dwd_table_name(config, "fact_financial_indicator"))
    client.delete_table(destination, not_found_ok=True)
    client.query(financial_table_ddl(config)).result()

    blobs = list_financial_parquet_blobs(config)
    batch_size = int(config.get("financial_indicator", {}).get("batch_files", 100))
    workers = int(config.get("financial_indicator", {}).get("parallel_read_workers", 1))
    total_rows = 0
    for batch_index, blob_batch in enumerate(chunked(blobs, batch_size), start=1):
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                frames = list(executor.map(read_financial_blob, blob_batch))
        else:
            frames = [read_financial_blob(blob) for blob in blob_batch]
        frames = [frame for frame in frames if not frame.empty]
        if not frames:
            print(f"financial batch {batch_index}: no rows", flush=True)
            continue
        batch = pd.concat(frames, ignore_index=True)
        job_config = bigquery.LoadJobConfig(
            schema=financial_load_schema(),
            write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
        )
        job = client.load_table_from_dataframe(batch, destination, job_config=job_config)
        job.result()
        total_rows += len(batch)
        print(
            f"Loaded financial batch {batch_index}: files={len(blob_batch)} rows={len(batch)} "
            f"total_rows={total_rows} job_id={job.job_id}",
            flush=True,
        )

    if total_rows == 0:
        raise RuntimeError("No rows loaded into dwd_fact_financial_indicator")
    print(f"Loaded {total_rows} rows into {destination}", flush=True)


def audit_financial_indicator(config: dict) -> dict:
    client = bq_client(config)
    full_id = table_id(config, dwd_table_name(config, "fact_financial_indicator"))
    table = client.get_table(full_id)
    fields = {field.name for field in table.schema}
    required = {
        "equity_code",
        "announcement_date",
        "report_period",
        "eps_basic",
        "bps",
        "roe",
    }
    missing = required - fields
    if missing:
        raise RuntimeError(f"Financial indicator audit failed: {full_id} missing fields {sorted(missing)}")
    sql = f"""
SELECT
  COUNT(*) AS row_count,
  COUNTIF(equity_code IS NOT NULL) AS equity_code_rows,
  COUNTIF(announcement_date IS NOT NULL) AS announcement_date_rows,
  COUNTIF(report_period IS NOT NULL) AS report_period_rows,
  COUNTIF(eps_basic IS NOT NULL) AS eps_basic_rows,
  COUNTIF(bps IS NOT NULL) AS bps_rows,
  COUNTIF(roe IS NOT NULL) AS roe_rows
FROM {quote_table(full_id)}
"""
    bq_row = next(iter(client.query(sql).result()))
    row = {key: bq_row[key] for key in bq_row.keys()}
    errors = []
    if int(row["row_count"] or 0) <= 0:
        errors.append("row_count is 0")
    for field in ("equity_code_rows", "announcement_date_rows", "report_period_rows"):
        if int(row[field] or 0) <= 0:
            errors.append(f"{field} is 0")
    if not any(int(row[field] or 0) > 0 for field in ("eps_basic_rows", "bps_rows", "roe_rows")):
        errors.append("all core financial metric counts are 0")
    print(f"{full_id}: {row}")
    if errors:
        raise RuntimeError("Financial indicator audit failed: " + "; ".join(errors))
    return row


def build_equity_valuation_features_sql(config: dict) -> str:
    destination = table_id(config, dws_table_name(config, "equity_valuation_features"))
    price_table = table_id(config, dwd_table_name(config, "fact_equity_kline_1d"))
    financial_table = table_id(config, dwd_table_name(config, "fact_financial_indicator"))
    prefix = create_table_prefix(destination, partition=True, partition_field="partition_month", cluster_by=["equity_code"])
    return f"""{prefix}
WITH financial_dedup AS (
  SELECT
    equity_code,
    announcement_date,
    report_period,
    eps_basic,
    eps_diluted,
    bps,
    roe,
    roe_weighted,
    gross_margin,
    net_margin,
    debt_to_assets,
    asset_turnover,
    current_ratio,
    quick_ratio
  FROM {quote_table(financial_table)}
  WHERE equity_code IS NOT NULL
    AND announcement_date IS NOT NULL
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY equity_code, announcement_date
    ORDER BY report_period DESC, source_file DESC, source_hash DESC
  ) = 1
),
financial_intervals AS (
  SELECT
    *,
    LEAD(announcement_date) OVER (
      PARTITION BY equity_code
      ORDER BY announcement_date, report_period
    ) AS next_announcement_date
  FROM financial_dedup
),
price_base AS (
  SELECT
    equity_code,
    date,
    partition_month,
    close
  FROM {quote_table(price_table)}
  WHERE equity_code IS NOT NULL
    AND date IS NOT NULL
    AND close IS NOT NULL
    AND SAFE_CAST(close AS FLOAT64) > 0
    AND adjust_type = 'none'
)
SELECT
  p.equity_code,
  p.date,
  p.partition_month,
  SAFE_CAST(p.close AS FLOAT64) AS close,
  f.announcement_date,
  f.report_period,
  f.eps_basic,
  f.eps_diluted,
  f.bps,
  f.roe,
  f.roe_weighted,
  f.gross_margin,
  f.net_margin,
  f.debt_to_assets,
  f.asset_turnover,
  f.current_ratio,
  f.quick_ratio,
  SAFE_DIVIDE(SAFE_CAST(p.close AS FLOAT64), NULLIF(f.eps_basic, 0)) AS pe_basic,
  SAFE_DIVIDE(SAFE_CAST(p.close AS FLOAT64), NULLIF(f.bps, 0)) AS pb,
  CURRENT_TIMESTAMP() AS feature_generated_at
FROM price_base AS p
JOIN financial_intervals AS f
  ON p.equity_code = f.equity_code
 AND p.date >= f.announcement_date
 AND (f.next_announcement_date IS NULL OR p.date < f.next_announcement_date)
"""


def transform_equity_valuation_features(config: dict) -> None:
    client = bq_client(config)
    destination = table_id(config, dws_table_name(config, "equity_valuation_features"))
    job = client.query(build_equity_valuation_features_sql(config))
    job.result()
    print(f"Transformed {destination}; job_id={job.job_id}")


def audit_equity_valuation_features(config: dict) -> dict:
    client = bq_client(config)
    full_id = table_id(config, dws_table_name(config, "equity_valuation_features"))
    sql = f"""
SELECT
  COUNT(*) AS row_count,
  COUNTIF(pe_basic IS NOT NULL) AS pe_rows,
  COUNTIF(pb IS NOT NULL) AS pb_rows,
  COUNTIF(roe IS NOT NULL) AS roe_rows
FROM {quote_table(full_id)}
"""
    bq_row = next(iter(client.query(sql).result()))
    row = {key: bq_row[key] for key in bq_row.keys()}
    errors = []
    if int(row["row_count"] or 0) <= 0:
        errors.append("row_count is 0")
    if not any(int(row[field] or 0) > 0 for field in ("pe_rows", "pb_rows", "roe_rows")):
        errors.append("pe_rows, pb_rows and roe_rows are all 0")
    print(f"{full_id}: {row}")
    if errors:
        raise RuntimeError("Equity valuation feature audit failed: " + "; ".join(errors))
    return row
