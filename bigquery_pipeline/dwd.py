from __future__ import annotations

import re
from collections.abc import Sequence

from .client import bq_client, dataset_id, dwd_table_name, table_id, table_prefix
from .financial import audit_financial_indicator, load_financial_indicator
from .sql import (
    bool_sql,
    create_table_prefix,
    date_sql,
    lineage_select_items,
    normalize_code_sql,
    normalize_index_code_sql,
    nullable_string_sql,
    numeric_sql,
    partition_month_sql,
    quote_ident,
    quote_table,
    source_column_expr,
)


CONTROL_ODS_TABLES = {
    "ods_external_manifest",
    "ods_external_errors",
    "ods_gcs_load_manifest",
    "ods_gcs_load_errors",
}

REQUIRED_DWD_FIELDS: dict[str, set[str]] = {
    "dwd_fact_equity_kline_1d": {"equity_code", "date", "partition_month", "adjust_type", "open", "high", "low", "close", "volume", "amount"},
    "dwd_fact_fund_kline_1d": {"fund_code", "date", "partition_month", "adjust_type", "open", "high", "low", "close", "volume", "amount"},
    "dwd_fact_index_kline_1d": {"index_code", "date", "partition_month", "open", "high", "low", "close", "volume", "amount"},
    "dwd_fact_board_component_1d": {"board_code", "equity_code", "date", "partition_month"},
    "dwd_dim_security": {"security_code", "security_name", "security_type", "list_date", "is_active"},
    "dwd_fact_financial_indicator": {"equity_code", "announcement_date", "report_period", "partition_month"},
    "dwd_fact_kpl_board_1d": {"equity_code", "date", "partition_month", "limit_up_reason", "limit_up_streak"},
    "dwd_fact_dragon_tiger_seat_1d": {"equity_code", "date", "partition_month", "department_name", "net_amount"},
    "dwd_fact_money_flow_1d": {"equity_code", "date", "partition_month", "net_inflow_amount"},
    "dwd_dim_index_profile": {"index_code", "index_name"},
    "dwd_fact_index_component_1d": {"index_code", "equity_code", "date", "partition_month"},
    "dwd_dim_citic_industry": {"industry_code", "industry_name"},
    "dwd_dim_sw_industry": {"industry_code", "industry_name"},
    "dwd_fact_citic_industry_component_history": {"equity_code", "industry_code"},
    "dwd_fact_sw_industry_component_1d": {"equity_code", "industry_code", "date", "partition_month"},
    "dwd_fact_citic_industry_kline_1d": {"industry_code", "date", "partition_month", "close"},
    "dwd_fact_sw_industry_kline_1d": {"industry_code", "date", "partition_month", "close"},
    "dwd_fact_index_market_indicator_1d": {"index_code", "date", "partition_month", "pe", "pb"},
}


def table_columns(table) -> list[str]:
    return [field.name for field in table.schema]


def normalize_target_table(target_table: str | None) -> str | None:
    if target_table is None:
        return None
    value = target_table.removeprefix("dwd_").removeprefix("ods_")
    return value


def resolve_dwd_source_tables(config: dict, client) -> dict[str, str]:
    result: dict[str, str] = {}
    ods_prefix = table_prefix(config, "ods")
    for table in client.list_tables(dataset_id(config)):
        table_name = table.table_id
        if table_name in CONTROL_ODS_TABLES:
            continue
        if table_name.startswith(ods_prefix):
            result[table_name.removeprefix(ods_prefix)] = table_name
    return dict(sorted(result.items()))


def by_ordinal(columns: Sequence[str], one_based_idx: int) -> str | None:
    idx = one_based_idx - 1
    if idx < 0 or idx >= len(columns):
        return None
    return f"t.{quote_ident(columns[idx])}"


def source_expr(
    columns: Sequence[str] | set[str],
    candidates: Sequence[str],
    ordinal: int | None = None,
) -> str | None:
    found = source_column_expr(columns, candidates)
    if found is not None:
        return found
    if ordinal is not None and not isinstance(columns, set):
        return by_ordinal(columns, ordinal)
    return None


def build_kline_dwd_sql(table_key: str, source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    if "equity" in table_key:
        code_column = "equity_code"
        code_candidates = ["equity_code", "security_code", "股票代码", "证券代码", "代码"]
        cluster = ["equity_code"]
        ordinal_map = {"date": 1, "code": 2, "open": 3, "close": 4, "high": 5, "low": 6, "volume": 7, "amount": 8}
    elif "fund" in table_key:
        code_column = "fund_code"
        code_candidates = ["fund_code", "security_code", "基金代码", "基金交易代码", "代码"]
        cluster = ["fund_code"]
        ordinal_map = {"date": 1, "code": 2, "open": 3, "close": 4, "high": 5, "low": 6, "volume": 7, "amount": 8}
    elif "index" in table_key:
        code_column = "index_code"
        code_candidates = ["index_code", "security_code", "指数代码", "代码"]
        cluster = ["index_code"]
        ordinal_map = {"date": 2, "code": 1, "open": 4, "close": 5, "high": 6, "low": 7, "volume": 8, "amount": 9}
    else:
        code_column = "board_code"
        code_candidates = ["board_code", "security_code", "板块代码", "指数代码", "代码"]
        cluster = ["board_code"]
        ordinal_map = {"date": 2, "code": 1, "open": 4, "close": 5, "high": 6, "low": 7, "volume": 8, "amount": 9}

    date_expr = source_expr(columns, ["date", "日期", "交易日期"], ordinal_map["date"])
    parsed_date = date_sql(date_expr)
    raw_code = source_expr(columns, code_candidates, ordinal_map["code"])
    period = table_key.rsplit("_", 1)[-1]
    has_adjust = code_column in {"equity_code", "fund_code"}
    adjust_items = ["'qfq' AS adjust_type"] if has_adjust else []
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{(normalize_index_code_sql if code_column == 'index_code' else normalize_code_sql)(raw_code)} AS {code_column}",
        *adjust_items,
        f"'{period}' AS period",
        f"{numeric_sql(source_expr(columns, ['open', 'open_raw', '开盘'], ordinal_map['open']))} AS open",
        f"{numeric_sql(source_expr(columns, ['high', 'high_raw', '最高'], ordinal_map['high']))} AS high",
        f"{numeric_sql(source_expr(columns, ['low', 'low_raw', '最低'], ordinal_map['low']))} AS low",
        f"{numeric_sql(source_expr(columns, ['close', 'close_raw', '收盘'], ordinal_map['close']))} AS close",
        f"{numeric_sql(source_expr(columns, ['volume', 'volume_raw', '成交量'], ordinal_map['volume']))} AS volume",
        f"{numeric_sql(source_expr(columns, ['amount', 'amount_raw', '成交额'], ordinal_map['amount']))} AS amount",
        *lineage_select_items(columns),
    ]
    key_columns = [code_column, "date"] + (["adjust_type"] if has_adjust else [])
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=cluster + (["adjust_type"] if has_adjust else []))
    return _dedup_sql(prefix, source_id, select_items, key_columns, f"date IS NOT NULL AND {code_column} IS NOT NULL")


def _dedup_sql(
    prefix: str,
    source_id: str,
    select_items: Sequence[str],
    key_columns: Sequence[str],
    where_clause: str,
) -> str:
    partition_by = ", ".join(key_columns)
    return f"""{prefix}
WITH normalized AS (
  SELECT
    {",\n    ".join(select_items)}
  FROM {quote_table(source_id)} AS t
),
ranked AS (
  SELECT
    *,
    ROW_NUMBER() OVER (
      PARTITION BY {partition_by}
      ORDER BY source_file DESC, source_entry DESC, source_hash DESC
    ) AS rn
  FROM normalized
  WHERE {where_clause}
)
SELECT * EXCEPT(rn)
FROM ranked
WHERE rn = 1
"""


def build_board_component_dwd_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    parsed_date = date_sql(source_expr(columns, ["date", "日期", "交易日期"], 1))
    board_expr = source_expr(columns, ["board_code", "板块代码", "指数代码", "security_code"], 7)
    equity_expr = source_expr(columns, ["equity_code", "成分股票代码", "股票代码"], 8)
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{normalize_code_sql(board_expr)} AS board_code",
        f"{normalize_code_sql(equity_expr)} AS equity_code",
        f"{normalize_code_sql(equity_expr)} AS security_code",
        f"{nullable_string_sql(source_expr(columns, ['指数名称', '板块名称'], 6))} AS board_name",
        f"{nullable_string_sql(source_expr(columns, ['成分股票名称', '股票名称'], 9))} AS equity_name",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["board_code", "equity_code"])
    return _dedup_sql(prefix, source_id, select_items, ["date", "board_code", "equity_code"], "date IS NOT NULL AND board_code IS NOT NULL AND equity_code IS NOT NULL")


def build_dim_security_dwd_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    code_expr = source_expr(columns, ["security_code", "TS代码", "股票代码", "证券代码", "代码"], 1)
    list_date = date_sql(source_expr(columns, ["上市日期", "list_date", "date"], 12))
    delist_date = date_sql(source_expr(columns, ["退市日期", "delist_date"], 13))
    status_expr = nullable_string_sql(source_expr(columns, ["上市状态", "status"], 11))
    select_items = [
        f"{normalize_code_sql(code_expr)} AS security_code",
        f"{nullable_string_sql(source_expr(columns, ['股票名称', '证券名称', '名称', 'security_name'], 3))} AS security_name",
        "'stock' AS security_type",
        f"{nullable_string_sql(source_expr(columns, ['所属行业', 'industry'], 5))} AS industry",
        f"{nullable_string_sql(source_expr(columns, ['市场类型', 'market_type'], 9))} AS market_type",
        f"{nullable_string_sql(source_expr(columns, ['交易所代码', 'exchange_code'], 10))} AS exchange_code",
        f"{list_date} AS list_date",
        f"{delist_date} AS delist_date",
        f"({status_expr} IS NULL OR {status_expr} != '退市') AS is_active",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=False, cluster_by=["security_code", "security_type"])
    return _dedup_sql(prefix, source_id, select_items, ["security_code"], "security_code IS NOT NULL")


def build_adjust_factor_dwd_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    parsed_date = date_sql(source_expr(columns, ["date", "日期", "交易日期"], 1))
    code_expr = source_expr(columns, ["equity_code", "security_code", "股票代码", "证券代码", "代码"], 2)
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{normalize_code_sql(code_expr)} AS equity_code",
        f"{numeric_sql(source_expr(columns, ['adjust_factor', '复权因子'], 3))} AS adjust_factor",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["equity_code"])
    return _dedup_sql(prefix, source_id, select_items, ["equity_code", "date"], "equity_code IS NOT NULL AND date IS NOT NULL")


def build_kpl_board_dwd_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    parsed_date = date_sql(source_expr(columns, ["date", "日期", "交易日期"], 1))
    code_expr = source_expr(columns, ["equity_code", "security_code", "股票代码", "证券代码", "代码"], 2)
    streak_expr = source_expr(columns, ["N连板", "连板数", "N__"], 12)
    streak_text = f"TRIM(CAST({streak_expr} AS STRING))" if streak_expr else "''"
    streak_source = streak_expr or "CAST(NULL AS STRING)"
    streak_sql = f"""CASE
    WHEN {streak_source} IS NULL THEN NULL
    WHEN {streak_text} LIKE '%首板%' THEN 1
    WHEN REGEXP_CONTAINS({streak_text}, r'\\d+') THEN SAFE_CAST(REGEXP_EXTRACT({streak_text}, r'(\\d+)') AS NUMERIC)
    ELSE SAFE_CAST({streak_source} AS NUMERIC)
  END"""
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{normalize_code_sql(code_expr)} AS equity_code",
        f"{nullable_string_sql(source_expr(columns, ['名称', '股票名称', 'security_name'], 3))} AS security_name",
        f"{nullable_string_sql(source_expr(columns, ['初次涨停时间'], 4))} AS first_limit_up_time",
        f"{nullable_string_sql(source_expr(columns, ['最后涨停时间'], 5))} AS last_limit_up_time",
        f"{nullable_string_sql(source_expr(columns, ['炸板时间'], 6))} AS open_board_time",
        f"{nullable_string_sql(source_expr(columns, ['跌停时间'], 7))} AS limit_down_time",
        f"{nullable_string_sql(source_expr(columns, ['涨停原因'], 8))} AS limit_up_reason",
        f"{nullable_string_sql(source_expr(columns, ['标签'], 9))} AS tags",
        f"{nullable_string_sql(source_expr(columns, ['板块'], 10))} AS board_names",
        f"{numeric_sql(source_expr(columns, ['主力净额(元)', '主力净额', '____________'], 11))} AS main_net_amount",
        f"{streak_sql} AS limit_up_streak",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["equity_code"])
    return _dedup_sql(prefix, source_id, select_items, ["equity_code", "date"], "equity_code IS NOT NULL AND date IS NOT NULL")


def build_dragon_tiger_dwd_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    parsed_date = date_sql(source_expr(columns, ["date", "日期", "交易日期"], 1))
    code_expr = source_expr(columns, ["equity_code", "security_code", "股票代码", "证券代码", "代码"], 2)
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{normalize_code_sql(code_expr)} AS equity_code",
        f"{nullable_string_sql(source_expr(columns, ['股票名称', '名称', 'security_name'], 3))} AS security_name",
        f"{nullable_string_sql(source_expr(columns, ['买卖类型'], 4))} AS trade_side",
        f"{nullable_string_sql(source_expr(columns, ['营业部名称'], 5))} AS department_name",
        f"{numeric_sql(source_expr(columns, ['买入额(元)', '买入额', '_______'], 6))} AS buy_amount",
        f"{numeric_sql(source_expr(columns, ['卖出额(元)', '卖出额', '________'], 7))} AS sell_amount",
        f"{numeric_sql(source_expr(columns, ['净成交额(元)', '净成交额', '___________'], 8))} AS net_amount",
        f"{nullable_string_sql(source_expr(columns, ['上榜理由'], 9))} AS list_reason",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["equity_code"])
    return _dedup_sql(prefix, source_id, select_items, ["equity_code", "date", "department_name", "trade_side"], "equity_code IS NOT NULL AND date IS NOT NULL")


def build_money_flow_dwd_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    parsed_date = date_sql(source_expr(columns, ["date", "日期", "交易日期"], 1))
    code_expr = source_expr(columns, ["equity_code", "security_code", "股票代码", "证券代码", "代码"], 2)
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{normalize_code_sql(code_expr)} AS equity_code",
        f"{nullable_string_sql(source_expr(columns, ['名称', '股票名称', 'security_name'], 3))} AS security_name",
        f"{numeric_sql(source_expr(columns, ['净流入量(手)', '净流入量', '______________________']))} AS net_inflow_volume",
        f"{numeric_sql(source_expr(columns, ['净流入额(万元)', '净流入额', '________________________']))} AS net_inflow_amount",
        f"{numeric_sql(source_expr(columns, ['主力净流入额(万元)', '主力净流入额', '大单净流入额(万元)', '_________________']))} AS main_net_inflow_amount",
        f"{numeric_sql(source_expr(columns, ['超大单净流入额(万元)', '特大单净流入额(万元)', '_____________']))} AS extra_large_net_inflow_amount",
        f"{numeric_sql(source_expr(columns, ['大单净流入额(万元)', '_________________']))} AS large_net_inflow_amount",
        f"{numeric_sql(source_expr(columns, ['中单净流入额(万元)', '_____________']))} AS medium_net_inflow_amount",
        f"{numeric_sql(source_expr(columns, ['小单净流入额(万元)', '_________']))} AS small_net_inflow_amount",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["equity_code"])
    return _dedup_sql(prefix, source_id, select_items, ["equity_code", "date"], "equity_code IS NOT NULL AND date IS NOT NULL")


def build_dim_index_profile_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    code_expr = source_expr(columns, ["index_code", "指数代码", "代码"], 1)
    launch_date = date_sql(source_expr(columns, ["发布日期", "发布日", "launch_date"], 8))
    select_items = [
        f"{normalize_index_code_sql(code_expr)} AS index_code",
        f"{nullable_string_sql(source_expr(columns, ['简称', '指数简称', '指数名称', '名称'], 2))} AS index_name",
        f"{nullable_string_sql(source_expr(columns, ['市场'], 3))} AS market",
        f"{nullable_string_sql(source_expr(columns, ['发布方'], 4))} AS publisher",
        f"{nullable_string_sql(source_expr(columns, ['指数类别'], 5))} AS index_category",
        f"{nullable_string_sql(source_expr(columns, ['基期'], 6))} AS base_period",
        f"{numeric_sql(source_expr(columns, ['基点'], 7))} AS base_point",
        f"{launch_date} AS launch_date",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=False, cluster_by=["index_code"])
    return _dedup_sql(prefix, source_id, select_items, ["index_code"], "index_code IS NOT NULL")


def build_index_component_dwd_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    parsed_date = date_sql(source_expr(columns, ["date", "日期", "交易日期"], 3))
    index_expr = source_expr(columns, ["index_code", "指数代码", "代码"], 1)
    equity_expr = source_expr(columns, ["equity_code", "成分股票代码", "股票代码", "证券代码"], 2)
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{normalize_index_code_sql(index_expr)} AS index_code",
        f"{normalize_code_sql(equity_expr)} AS equity_code",
        f"{numeric_sql(source_expr(columns, ['权重', 'weight'], 4))} AS weight",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["index_code", "equity_code"])
    return _dedup_sql(prefix, source_id, select_items, ["date", "index_code", "equity_code"], "date IS NOT NULL AND index_code IS NOT NULL AND equity_code IS NOT NULL")


def build_industry_dim_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    code_expr = source_expr(columns, ["industry_code", "行业代码", "指数代码", "代码"], 4)
    select_items = [
        f"{nullable_string_sql(code_expr)} AS industry_code",
        f"{nullable_string_sql(source_expr(columns, ['industry_name', '行业名称', '简称', '名称'], 2))} AS industry_name",
        f"{nullable_string_sql(source_expr(columns, ['行业分级', 'level', 'industry_level'], 3))} AS industry_level",
        f"{nullable_string_sql(source_expr(columns, ['父级代码', 'parent_code'], 6))} AS parent_code",
        f"{bool_sql(source_expr(columns, ['是否发布指数', 'has_index'], 5))} AS has_index",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=False, cluster_by=["industry_code"])
    return _dedup_sql(prefix, source_id, select_items, ["industry_code"], "industry_code IS NOT NULL")


def build_citic_component_history_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    include_date = date_sql(source_expr(columns, ["纳入日期", "include_date"], 9))
    exclude_date = date_sql(source_expr(columns, ["剔除日期", "exclude_date"], 10))
    equity_expr = source_expr(columns, ["equity_code", "股票代码", "证券代码"], 7)
    industry_expr = source_expr(columns, ["三级行业代码", "二级行业代码", "一级行业代码", "行业代码"], 5)
    select_items = [
        f"{normalize_code_sql(equity_expr)} AS equity_code",
        f"{nullable_string_sql(source_expr(columns, ['股票名称', '名称'], 8))} AS equity_name",
        f"{nullable_string_sql(source_expr(columns, ['一级行业代码'], 1))} AS industry_code_l1",
        f"{nullable_string_sql(source_expr(columns, ['一级行业名称'], 2))} AS industry_name_l1",
        f"{nullable_string_sql(source_expr(columns, ['二级行业代码'], 3))} AS industry_code_l2",
        f"{nullable_string_sql(source_expr(columns, ['二级行业名称'], 4))} AS industry_name_l2",
        f"{nullable_string_sql(industry_expr)} AS industry_code",
        f"{nullable_string_sql(source_expr(columns, ['三级行业名称', '行业名称'], 6))} AS industry_name",
        f"{include_date} AS include_date",
        f"{exclude_date} AS exclude_date",
        f"{bool_sql(source_expr(columns, ['是否最新', 'is_latest'], 11))} AS is_latest",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=False, cluster_by=["industry_code", "equity_code"])
    return _dedup_sql(prefix, source_id, select_items, ["equity_code", "industry_code", "include_date"], "equity_code IS NOT NULL AND industry_code IS NOT NULL")


def build_sw_component_dwd_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    parsed_date = date_sql(source_expr(columns, ["date", "日期", "交易日期"], 1))
    equity_expr = source_expr(columns, ["equity_code", "股票代码", "证券代码", "成分股票代码"], 2)
    industry_expr = source_expr(columns, ["industry_code", "行业代码", "指数代码"], 4)
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{normalize_code_sql(equity_expr)} AS equity_code",
        f"{nullable_string_sql(source_expr(columns, ['股票名称', '名称'], 3))} AS equity_name",
        f"{nullable_string_sql(industry_expr)} AS industry_code",
        f"{nullable_string_sql(source_expr(columns, ['行业名称', 'industry_name'], 5))} AS industry_name",
        f"{nullable_string_sql(source_expr(columns, ['行业分级', 'industry_level'], 6))} AS industry_level",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["industry_code", "equity_code"])
    return _dedup_sql(prefix, source_id, select_items, ["date", "equity_code", "industry_code"], "date IS NOT NULL AND equity_code IS NOT NULL AND industry_code IS NOT NULL")


def build_industry_kline_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    parsed_date = date_sql(source_expr(columns, ["date", "日期", "交易日期"], 2))
    industry_expr = source_expr(columns, ["industry_code", "指数代码", "行业代码", "代码"], 1)
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{nullable_string_sql(industry_expr)} AS industry_code",
        f"{nullable_string_sql(source_expr(columns, ['行业名称', '名称'], 3))} AS industry_name",
        f"{numeric_sql(source_expr(columns, ['open', '开盘'], 4))} AS open",
        f"{numeric_sql(source_expr(columns, ['high', '最高'], 6))} AS high",
        f"{numeric_sql(source_expr(columns, ['low', '最低'], 7))} AS low",
        f"{numeric_sql(source_expr(columns, ['close', '收盘'], 5))} AS close",
        f"{numeric_sql(source_expr(columns, ['volume', '成交量'], 8))} AS volume",
        f"{numeric_sql(source_expr(columns, ['amount', '成交额'], 9))} AS amount",
        f"{numeric_sql(source_expr(columns, ['市盈率', 'pe']))} AS pe",
        f"{numeric_sql(source_expr(columns, ['市净率', 'pb']))} AS pb",
        f"{numeric_sql(source_expr(columns, ['流通市值(万元)', '流通市值']))} AS float_market_cap",
        f"{numeric_sql(source_expr(columns, ['总市值(万元)', '总市值']))} AS total_market_cap",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["industry_code"])
    return _dedup_sql(prefix, source_id, select_items, ["industry_code", "date"], "industry_code IS NOT NULL AND date IS NOT NULL")


def build_index_market_indicator_sql(source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    parsed_date = date_sql(source_expr(columns, ["date", "日期", "交易日期"], 2))
    index_expr = source_expr(columns, ["index_code", "指数代码", "代码"], 1)
    select_items = [
        f"{parsed_date} AS date",
        f"{partition_month_sql(columns, parsed_date)} AS partition_month",
        f"{normalize_index_code_sql(index_expr)} AS index_code",
        f"{numeric_sql(source_expr(columns, ['总市值(元)', '总市值'], 3))} AS total_market_cap",
        f"{numeric_sql(source_expr(columns, ['流通市值(元)', '流通市值'], 4))} AS float_market_cap",
        f"{numeric_sql(source_expr(columns, ['总股本(股)', '总股本'], 5))} AS total_share",
        f"{numeric_sql(source_expr(columns, ['流通股本(股)', '流通股本'], 6))} AS float_share",
        f"{numeric_sql(source_expr(columns, ['自由流通股本(股)', '自由流通股本'], 7))} AS free_float_share",
        f"{numeric_sql(source_expr(columns, ['换手率'], 8))} AS turnover_rate",
        f"{numeric_sql(source_expr(columns, ['市盈率'], 9))} AS pe",
        f"{numeric_sql(source_expr(columns, ['市盈率TTM', '市盈率 TTM'], 10))} AS pe_ttm",
        f"{numeric_sql(source_expr(columns, ['市净率'], 11))} AS pb",
        *lineage_select_items(columns),
    ]
    prefix = create_table_prefix(destination_id, partition=True, partition_field="partition_month", cluster_by=["index_code"])
    return _dedup_sql(prefix, source_id, select_items, ["index_code", "date"], "index_code IS NOT NULL AND date IS NOT NULL")


def generic_code_candidates(table_key: str) -> tuple[str | None, list[str], bool]:
    if "board" in table_key:
        return "board_code", ["board_code", "板块代码", "指数代码", "security_code"], False
    if "fund" in table_key:
        return "fund_code", ["fund_code", "基金代码", "基金交易代码", "security_code", "代码"], False
    if "index" in table_key:
        return "index_code", ["index_code", "指数代码", "security_code", "代码"], True
    if table_key.startswith("fact_"):
        return "equity_code", ["equity_code", "security_code", "股票代码", "证券代码", "代码"], False
    if table_key == "dim_index":
        return "index_code", ["index_code", "指数代码", "security_code", "代码"], True
    if table_key == "dim_board":
        return "board_code", ["board_code", "板块代码", "security_code", "代码"], False
    return None, [], False


def build_generic_dwd_sql(table_key: str, source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    date_expr = source_expr(columns, ["date", "日期", "交易日期", "公告日期", "实际公告日期", "纳入日期"])
    parsed_date = date_sql(date_expr)
    code_column, code_candidates, is_index = generic_code_candidates(table_key)
    select_items: list[str] = []
    if date_expr is not None:
        select_items.append(f"{parsed_date} AS date")
        select_items.append(f"{partition_month_sql(columns, parsed_date)} AS partition_month")
    elif "partition_month" in columns:
        select_items.append(f"{partition_month_sql(columns)} AS partition_month")
    if code_column:
        normalizer = normalize_index_code_sql if is_index else normalize_code_sql
        select_items.append(f"{normalizer(source_expr(columns, code_candidates))} AS {code_column}")
    if source_column_expr(columns, ["报告期", "report_period", "report_period_raw"]):
        select_items.append(f"{nullable_string_sql(source_column_expr(columns, ['报告期', 'report_period', 'report_period_raw']))} AS report_period")
    if source_column_expr(columns, ["公告日期", "实际公告日期", "announcement_date_raw", "actual_announcement_date_raw"]):
        select_items.append(f"{date_sql(source_column_expr(columns, ['实际公告日期', '公告日期', 'actual_announcement_date_raw', 'announcement_date_raw']))} AS announcement_date")
    select_items.extend(lineage_select_items(columns))
    cluster = [code_column] if code_column else []
    prefix = create_table_prefix(destination_id, partition=False, cluster_by=cluster)
    return f"""{prefix}
SELECT
  {",\n  ".join(select_items)}
FROM {quote_table(source_id)} AS t
"""


def build_dwd_transform_sql(table_key: str, source_id: str, destination_id: str, columns: Sequence[str]) -> str:
    if re.fullmatch(r"fact_(equity|fund|index|board)_kline_(1d|1w|1mo)", table_key):
        return build_kline_dwd_sql(table_key, source_id, destination_id, columns)
    if table_key == "fact_board_component_1d":
        return build_board_component_dwd_sql(source_id, destination_id, columns)
    if table_key == "dim_security":
        return build_dim_security_dwd_sql(source_id, destination_id, columns)
    if table_key == "fact_adjust_factor":
        return build_adjust_factor_dwd_sql(source_id, destination_id, columns)
    if table_key == "fact_kpl_board_1d":
        return build_kpl_board_dwd_sql(source_id, destination_id, columns)
    if table_key == "fact_dragon_tiger_seat_1d":
        return build_dragon_tiger_dwd_sql(source_id, destination_id, columns)
    if table_key == "fact_money_flow_1d":
        return build_money_flow_dwd_sql(source_id, destination_id, columns)
    if table_key == "dim_index_profile":
        return build_dim_index_profile_sql(source_id, destination_id, columns)
    if table_key == "fact_index_component_1d":
        return build_index_component_dwd_sql(source_id, destination_id, columns)
    if table_key in {"dim_citic_industry", "dim_sw_industry"}:
        return build_industry_dim_sql(source_id, destination_id, columns)
    if table_key == "fact_citic_industry_component_history":
        return build_citic_component_history_sql(source_id, destination_id, columns)
    if table_key == "fact_sw_industry_component_1d":
        return build_sw_component_dwd_sql(source_id, destination_id, columns)
    if table_key in {"fact_citic_industry_kline_1d", "fact_sw_industry_kline_1d"}:
        return build_industry_kline_sql(source_id, destination_id, columns)
    if table_key == "fact_index_market_indicator_1d":
        return build_index_market_indicator_sql(source_id, destination_id, columns)
    return build_generic_dwd_sql(table_key, source_id, destination_id, columns)


def transform_dwd(config: dict, target_table: str | None = None) -> None:
    normalized_target = normalize_target_table(target_table)
    if normalized_target == "fact_financial_indicator":
        load_financial_indicator(config)
        return

    client = bq_client(config)
    source_tables = resolve_dwd_source_tables(config, client)
    if normalized_target:
        source_tables = {normalized_target: source_tables[normalized_target]} if normalized_target in source_tables else {}
    if not source_tables:
        raise RuntimeError(f"No ODS source tables found for DWD transform: {target_table or '<all>'}")

    for table_key, ods_name in source_tables.items():
        if table_key == "fact_financial_indicator":
            print("Skipping dwd_fact_financial_indicator in generic DWD transform; use repair-financial-indicator.")
            continue
        source_id = table_id(config, ods_name)
        destination_name = dwd_table_name(config, table_key)
        destination_id = table_id(config, destination_name)
        source_table = client.get_table(source_id)
        sql = build_dwd_transform_sql(table_key, source_id, destination_id, table_columns(source_table))
        job = client.query(sql)
        job.result()
        print(f"Transformed {source_id} -> {destination_id}; job_id={job.job_id}")


def audit_dwd(config: dict, target_table: str | None = None) -> None:
    client = bq_client(config)
    source_tables = resolve_dwd_source_tables(config, client)
    normalized_target = normalize_target_table(target_table)
    if normalized_target:
        source_tables = {normalized_target: source_tables[normalized_target]} if normalized_target in source_tables else {}
    if not source_tables:
        raise RuntimeError(f"No ODS source tables found for DWD audit: {target_table or '<all>'}")

    missing: list[str] = []
    zero_rows: list[str] = []
    schema_errors: list[str] = []

    for table_key in sorted(source_tables):
        dwd_name = dwd_table_name(config, table_key)
        full_id = table_id(config, dwd_name)
        try:
            table = client.get_table(full_id)
        except Exception as exc:
            missing.append(f"{full_id}: {exc}")
            continue
        row_count = int(table.num_rows or 0)
        fields = set(table_columns(table))
        absent = REQUIRED_DWD_FIELDS.get(dwd_name, set()) - fields
        if row_count == 0:
            zero_rows.append(full_id)
        if absent:
            schema_errors.append(f"{full_id}: missing fields {sorted(absent)}")
        status = "pass" if row_count > 0 and not absent else "failed"
        print(f"{full_id}: rows={row_count} fields={len(fields)} status={status}")

    if not normalized_target or normalized_target == "fact_financial_indicator":
        audit_financial_indicator(config)

    errors: list[str] = []
    if missing:
        errors.append(f"missing DWD tables ({len(missing)}):\n" + "\n".join(missing[:20]))
    if zero_rows:
        errors.append(f"zero-row DWD tables ({len(zero_rows)}):\n" + "\n".join(zero_rows[:20]))
    if schema_errors:
        errors.append(f"DWD schema errors ({len(schema_errors)}):\n" + "\n".join(schema_errors[:20]))
    if errors:
        raise RuntimeError("DWD audit failed:\n" + "\n".join(errors))

    print(f"DWD audit passed: {len(source_tables)} source tables covered")


def row_count(config: dict, table_name: str) -> int:
    client = bq_client(config)
    full_id = table_id(config, table_name)
    row = next(iter(client.query(f"SELECT COUNT(*) AS row_count FROM {quote_table(full_id)}").result()))
    return int(row["row_count"])
