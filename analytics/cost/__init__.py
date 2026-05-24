"""GCP 账单导出 BigQuery 分析工具包。

配套 PRD：PRD_20260524_04。

模块组成：
    queries  - 参数化 SQL 模板（按服务/按资源/按日趋势）
    cli      - 命令行入口，python -m analytics.cost <子命令>

设计原则：
    - 账单 dataset 与业务 dataset 分离，不进 BigQueryDataSource 查询路径
    - 所有查询必须包含 _PARTITIONTIME 过滤，避免全表扫描
    - 净成本 = SUM(cost) + SUM(unnested credits.amount)
"""

from analytics.cost.queries import (
    monthly_by_service,
    monthly_by_resource,
    daily_trend,
)

__all__ = [
    "monthly_by_service",
    "monthly_by_resource",
    "daily_trend",
]
