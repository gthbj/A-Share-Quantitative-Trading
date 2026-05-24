from __future__ import annotations

import re
from datetime import date
from typing import Sequence

import pandas as pd


def quote_ident(name: str) -> str:
    return "`" + name.replace("`", "``") + "`"


def quote_table(full_table_id: str) -> str:
    return "`" + full_table_id.replace("`", "``") + "`"


def sql_string(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def sql_string_list(values: Sequence[str]) -> str:
    return ", ".join(sql_string(value) for value in values)


def month_range_boundaries(start_year: int = 1990, end_year: int = 2100) -> str:
    return f"GENERATE_ARRAY({start_year}01, {end_year}01, 100)"


def create_table_prefix(
    table_id_to_write: str,
    partition: bool,
    cluster_by: Sequence[str],
    partition_field: str | None = None,
) -> str:
    sql = f"CREATE OR REPLACE TABLE {quote_table(table_id_to_write)}"
    if partition:
        if partition_field == "date":
            sql += "\nPARTITION BY date"
        elif partition_field == "partition_month":
            sql += f"\nPARTITION BY RANGE_BUCKET(partition_month, {month_range_boundaries()})"
    if cluster_by:
        sql += "\nCLUSTER BY " + ", ".join(cluster_by)
    return sql + "\nAS"


def source_column_expr(columns: Sequence[str] | set[str], candidates: Sequence[str]) -> str | None:
    available = set(columns)
    for candidate in candidates:
        if candidate in available:
            return f"t.{quote_ident(candidate)}"
    return None


def nullable_string_sql(expr: str | None) -> str:
    if expr is None:
        return "CAST(NULL AS STRING)"
    return f"NULLIF(TRIM(CAST({expr} AS STRING)), '')"


def numeric_sql(expr: str | None) -> str:
    if expr is None:
        return "CAST(NULL AS NUMERIC)"
    return f"SAFE_CAST(NULLIF(TRIM(CAST({expr} AS STRING)), '') AS NUMERIC)"


def bool_sql(expr: str | None) -> str:
    if expr is None:
        return "CAST(NULL AS BOOL)"
    text = f"LOWER(TRIM(CAST({expr} AS STRING)))"
    return f"""CASE
    WHEN {text} IN ('true', '1', 'yes', 'y', '是', '最新') THEN TRUE
    WHEN {text} IN ('false', '0', 'no', 'n', '否') THEN FALSE
    ELSE SAFE_CAST({expr} AS BOOL)
  END"""


def date_sql(expr: str | None) -> str:
    if expr is None:
        return "CAST(NULL AS DATE)"
    text = nullable_string_sql(expr)
    return (
        f"COALESCE(SAFE_CAST({expr} AS DATE), "
        f"SAFE.PARSE_DATE('%Y-%m-%d', {text}), "
        f"SAFE.PARSE_DATE('%Y/%m/%d', {text}), "
        f"SAFE.PARSE_DATE('%Y%m%d', {text}))"
    )


def normalize_code_sql(expr: str | None) -> str:
    if expr is None:
        return "CAST(NULL AS STRING)"
    text = f"UPPER(REPLACE(TRIM(CAST({expr} AS STRING)), '_', '.'))"
    return f"""CASE
    WHEN {expr} IS NULL OR TRIM(CAST({expr} AS STRING)) = '' THEN NULL
    WHEN REGEXP_CONTAINS({text}, r'^\\d{{6}}$') THEN
      CASE
        WHEN STARTS_WITH({text}, '43') OR STARTS_WITH({text}, '83')
          OR STARTS_WITH({text}, '87') OR STARTS_WITH({text}, '88')
          OR STARTS_WITH({text}, '92') THEN CONCAT({text}, '.BJ')
        WHEN STARTS_WITH({text}, '5') OR STARTS_WITH({text}, '6')
          OR STARTS_WITH({text}, '9') THEN CONCAT({text}, '.SH')
        ELSE CONCAT({text}, '.SZ')
      END
    WHEN REGEXP_CONTAINS({text}, r'^(SH|SZ|BJ)\\d{{6}}$') THEN CONCAT(SUBSTR({text}, 3), '.', SUBSTR({text}, 1, 2))
    ELSE {text}
  END"""


def normalize_index_code_sql(expr: str | None) -> str:
    if expr is None:
        return "CAST(NULL AS STRING)"
    text = f"UPPER(REPLACE(TRIM(CAST({expr} AS STRING)), '_', '.'))"
    return f"""CASE
    WHEN {expr} IS NULL OR TRIM(CAST({expr} AS STRING)) = '' THEN NULL
    WHEN REGEXP_CONTAINS({text}, r'^399\\d{{3}}\\.(SH|SZ|BJ)$') THEN CONCAT(SUBSTR({text}, 1, 6), '.SZ')
    WHEN REGEXP_CONTAINS({text}, r'^(000|930|932|950)\\d{{3}}\\.(SH|SZ|BJ)$') THEN CONCAT(SUBSTR({text}, 1, 6), '.SH')
    WHEN REGEXP_CONTAINS({text}, r'^399\\d{{3}}$') THEN CONCAT({text}, '.SZ')
    WHEN REGEXP_CONTAINS({text}, r'^(000|930|932|950)\\d{{3}}$') THEN CONCAT({text}, '.SH')
    WHEN REGEXP_CONTAINS({text}, r'^\\d{{6}}$') THEN
      CASE
        WHEN STARTS_WITH({text}, '39') THEN CONCAT({text}, '.SZ')
        ELSE CONCAT({text}, '.SH')
      END
    WHEN REGEXP_CONTAINS({text}, r'^(SH|SZ|BJ)\\d{{6}}$') THEN CONCAT(SUBSTR({text}, 3), '.', SUBSTR({text}, 1, 2))
    ELSE {text}
  END"""


def partition_month_sql(columns: Sequence[str] | set[str], parsed_date_expr: str | None = None) -> str:
    if "partition_month" in set(columns):
        return "SAFE_CAST(t.`partition_month` AS INT64)"
    if parsed_date_expr:
        return f"SAFE_CAST(FORMAT_DATE('%Y%m', {parsed_date_expr}) AS INT64)"
    return "CAST(NULL AS INT64)"


def source_payload_sql() -> str:
    return "TO_JSON_STRING(t)"


def source_hash_sql() -> str:
    return f"TO_HEX(SHA256({source_payload_sql()}))"


def lineage_select_items(columns: Sequence[str] | set[str]) -> list[str]:
    return [
        f"{nullable_string_sql(source_column_expr(columns, ['source_file']))} AS source_file",
        f"{nullable_string_sql(source_column_expr(columns, ['source_entry']))} AS source_entry",
        f"{nullable_string_sql(source_column_expr(columns, ['target_table']))} AS target_table",
        f"{source_hash_sql()} AS source_hash",
        "CURRENT_TIMESTAMP() AS ingested_at",
        f"{source_payload_sql()} AS source_payload_json",
    ]


def normalize_security_code(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip().upper().replace("_", ".")
    if not text:
        return None
    if text.endswith(".0") and re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    if re.fullmatch(r"\d{6}", text):
        if text.startswith(("43", "83", "87", "88", "92")):
            return f"{text}.BJ"
        if text.startswith(("5", "6", "9")):
            return f"{text}.SH"
        return f"{text}.SZ"
    if re.fullmatch(r"(SH|SZ|BJ)\d{6}", text):
        return f"{text[2:]}.{text[:2]}"
    if re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", text):
        return text
    return text


def coerce_date_value(value: object) -> date | None:
    if value is None or pd.isna(value):
        return None
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(".0") and re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    if re.fullmatch(r"\d{8}", text):
        parsed = pd.to_datetime(text, format="%Y%m%d", errors="coerce")
    else:
        parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date()


def coerce_report_period(value: object) -> str | None:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith(".0") and re.fullmatch(r"\d+\.0", text):
        text = text[:-2]
    return text


def partition_month_from_date(value: date | None) -> int | None:
    if value is None or pd.isna(value):
        return None
    return int(f"{value.year:04d}{value.month:02d}")
