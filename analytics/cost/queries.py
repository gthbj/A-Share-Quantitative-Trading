"""GCP 账单导出 BigQuery 查询模板。

所有模板返回 (sql, query_params) 二元组，
其中 query_params 为 google.cloud.bigquery.ScalarQueryParameter 列表，
配合 bigquery.QueryJobConfig(query_parameters=...) 使用。

约定：
    - 表名通过 {standard_table} / {detailed_table} 占位符注入，由调用方填入
      已展开 billing_account_id 的完整表 ID（形如 `project.dataset.table`）
    - 所有 WHERE 必带 _PARTITIONTIME 过滤
    - net_cost = SUM(cost) + SUM(credits.amount)
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, List, Tuple

try:
    from google.cloud import bigquery  # type: ignore
except ImportError:  # pragma: no cover - 仅在缺少依赖时触发，CLI 时再抛错
    bigquery = None  # type: ignore


@dataclass(frozen=True)
class QueryBundle:
    """SQL 文本 + 参数。返回给 CLI / Notebook 调用。"""

    sql: str
    params: List[Any]


def _require_bigquery():
    if bigquery is None:
        raise ImportError(
            "google-cloud-bigquery 未安装。请 pip install google-cloud-bigquery"
        )


# ---------- 通用片段 ----------

_NET_COST_EXPR = """
    SUM(cost)
    + SUM(
        IFNULL(
          (SELECT SUM(c.amount) FROM UNNEST(credits) AS c),
          0
        )
      )
""".strip()


# ---------- 模板 1：按服务月度汇总 ----------

def monthly_by_service(
    standard_table: str,
    start_month: date,
    end_month: date,
) -> QueryBundle:
    """按月、按 GCP 服务汇总净成本。

    Args:
        standard_table: 完整表 ID，形如
            "data-aquarium.gcp_billing.gcp_billing_export_v1_01ABCD_23EFGH_45IJKL"
        start_month: 起始月份（取月初日期）
        end_month: 结束月份（含，取月初日期）

    Returns:
        QueryBundle，SQL 含 _PARTITIONTIME 过滤。
    """
    _require_bigquery()
    sql = f"""
        SELECT
          FORMAT_DATE('%Y-%m', invoice_parsed.month_date) AS invoice_month,
          service.description                     AS service,
          ROUND({_NET_COST_EXPR}, 4)              AS net_cost,
          currency
        FROM `{standard_table}`,
          UNNEST([STRUCT(PARSE_DATE('%Y%m', invoice.month) AS month_date)]) AS invoice_parsed
        WHERE DATE(_PARTITIONTIME) >= @part_start
          AND DATE(_PARTITIONTIME) <= @part_end
          AND invoice_parsed.month_date BETWEEN @start_month AND @end_month
        GROUP BY invoice_month, service, currency
        ORDER BY invoice_month DESC, net_cost DESC
    """.strip()

    # _PARTITIONTIME 比 invoice month 多覆盖 10 天，以容纳跨月写入延迟
    from datetime import timedelta
    part_start = start_month - timedelta(days=10)
    part_end = end_month.replace(day=28) + timedelta(days=40)  # 越过月末

    params = [
        bigquery.ScalarQueryParameter("start_month", "DATE", start_month),
        bigquery.ScalarQueryParameter("end_month", "DATE", end_month),
        bigquery.ScalarQueryParameter("part_start", "DATE", part_start),
        bigquery.ScalarQueryParameter("part_end", "DATE", part_end),
    ]
    return QueryBundle(sql=sql, params=params)


# ---------- 模板 2：按资源月度明细 ----------

def monthly_by_resource(
    detailed_table: str,
    month: date,
    service_filter: str | None = None,
    top_n: int = 50,
) -> QueryBundle:
    """按资源拆分某一月的净成本（依赖 detailed export）。

    Args:
        detailed_table: 完整表 ID，
            "...gcp_billing_export_resource_v1_..."
        month: 目标月份（取月初日期）
        service_filter: 可选服务名过滤（service.description 精确匹配，
            例如 "BigQuery" / "Cloud Storage"），None 表示不过滤
        top_n: 返回前 N 个资源
    """
    _require_bigquery()
    service_clause = (
        "AND service.description = @service_filter" if service_filter else ""
    )

    sql = f"""
        SELECT
          service.description           AS service,
          sku.description               AS sku,
          resource.name                 AS resource_name,
          resource.global_name          AS resource_global_name,
          ROUND({_NET_COST_EXPR}, 4)    AS net_cost,
          currency
        FROM `{detailed_table}`
        WHERE DATE(_PARTITIONTIME) >= @part_start
          AND DATE(_PARTITIONTIME) <= @part_end
          AND PARSE_DATE('%Y%m', invoice.month) = @month
          {service_clause}
        GROUP BY service, sku, resource_name, resource_global_name, currency
        ORDER BY net_cost DESC
        LIMIT @top_n
    """.strip()

    from datetime import timedelta
    part_start = month - timedelta(days=10)
    part_end = month.replace(day=28) + timedelta(days=15)

    params: List[Any] = [
        bigquery.ScalarQueryParameter("month", "DATE", month),
        bigquery.ScalarQueryParameter("part_start", "DATE", part_start),
        bigquery.ScalarQueryParameter("part_end", "DATE", part_end),
        bigquery.ScalarQueryParameter("top_n", "INT64", top_n),
    ]
    if service_filter:
        params.append(
            bigquery.ScalarQueryParameter(
                "service_filter", "STRING", service_filter
            )
        )
    return QueryBundle(sql=sql, params=params)


# ---------- 模板 3：日成本趋势 ----------

def daily_trend(
    standard_table: str,
    start_date: date,
    end_date: date,
    service_filter: str | None = None,
) -> QueryBundle:
    """按日聚合净成本，用于趋势观察 / 异常日发现。"""
    _require_bigquery()
    service_clause = (
        "AND service.description = @service_filter" if service_filter else ""
    )

    sql = f"""
        SELECT
          DATE(usage_start_time)        AS usage_date,
          ROUND({_NET_COST_EXPR}, 4)    AS net_cost,
          currency
        FROM `{standard_table}`
        WHERE DATE(_PARTITIONTIME) >= @part_start
          AND DATE(_PARTITIONTIME) <= @part_end
          AND DATE(usage_start_time) BETWEEN @start_date AND @end_date
          {service_clause}
        GROUP BY usage_date, currency
        ORDER BY usage_date
    """.strip()

    from datetime import timedelta
    part_start = start_date - timedelta(days=5)
    part_end = end_date + timedelta(days=10)

    params: List[Any] = [
        bigquery.ScalarQueryParameter("start_date", "DATE", start_date),
        bigquery.ScalarQueryParameter("end_date", "DATE", end_date),
        bigquery.ScalarQueryParameter("part_start", "DATE", part_start),
        bigquery.ScalarQueryParameter("part_end", "DATE", part_end),
    ]
    if service_filter:
        params.append(
            bigquery.ScalarQueryParameter(
                "service_filter", "STRING", service_filter
            )
        )
    return QueryBundle(sql=sql, params=params)
