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

