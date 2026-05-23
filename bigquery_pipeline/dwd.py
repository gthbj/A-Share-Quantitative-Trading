from __future__ import annotations

from .client import bq_client, dwd_table_name, table_id
from .financial import audit_financial_indicator, load_financial_indicator
from .sql import quote_table


REQUIRED_DWD_FIELDS: dict[str, set[str]] = {
    "dwd_fact_equity_kline_1d": {"equity_code", "date", "partition_month", "adjust_type", "open", "high", "low", "close", "volume", "amount"},
    "dwd_fact_fund_kline_1d": {"fund_code", "date", "partition_month", "adjust_type", "open", "high", "low", "close", "volume", "amount"},
    "dwd_fact_index_kline_1d": {"index_code", "date", "partition_month", "open", "high", "low", "close", "volume", "amount"},
    "dwd_fact_board_component_1d": {"board_code", "equity_code", "date", "partition_month"},
    "dwd_dim_security": {"security_code", "security_name", "security_type", "list_date", "is_active"},
    "dwd_fact_financial_indicator": {"equity_code", "announcement_date", "report_period", "partition_month"},
}


def transform_dwd(config: dict, target_table: str | None = None) -> None:
    normalized = (target_table or "fact_financial_indicator").removeprefix("dwd_")
    if normalized != "fact_financial_indicator":
        raise RuntimeError(
            "Generic DWD transforms now belong in bigquery_pipeline but only "
            "fact_financial_indicator has an active rebuild path in this change."
        )
    load_financial_indicator(config)


def audit_dwd(config: dict, target_table: str | None = None) -> None:
    client = bq_client(config)
    specs = REQUIRED_DWD_FIELDS
    if target_table:
        table_name = target_table if target_table.startswith("dwd_") else dwd_table_name(config, target_table)
        specs = {table_name: REQUIRED_DWD_FIELDS[table_name]} if table_name in REQUIRED_DWD_FIELDS else {}
    if not specs:
        raise RuntimeError(f"No DWD audit spec found for {target_table}")

    errors: list[str] = []
    for table_name, required_fields in specs.items():
        full_id = table_id(config, table_name)
        table = client.get_table(full_id)
        row_count = int(table.num_rows or 0)
        fields = {field.name for field in table.schema}
        missing = required_fields - fields
        if row_count <= 0:
            errors.append(f"{full_id}: row_count is 0")
        if missing:
            errors.append(f"{full_id}: missing fields {sorted(missing)}")
        print(f"{full_id}: rows={row_count} fields={len(fields)} status={'pass' if row_count > 0 and not missing else 'failed'}")

    if not target_table or target_table.removeprefix("dwd_") == "fact_financial_indicator":
        audit_financial_indicator(config)

    if errors:
        raise RuntimeError("DWD audit failed:\n" + "\n".join(errors))


def row_count(config: dict, table_name: str) -> int:
    client = bq_client(config)
    full_id = table_id(config, table_name)
    row = next(iter(client.query(f"SELECT COUNT(*) AS row_count FROM {quote_table(full_id)}").result()))
    return int(row["row_count"])

