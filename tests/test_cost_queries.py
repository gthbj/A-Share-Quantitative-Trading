"""analytics.cost 查询模板与配置单元测试。

PRD_20260524_04 §9.3 验收用例 6 / 7：
    - 净成本公式必须扣抵 credits
    - 所有 SQL 模板必须包含 _PARTITIONTIME 过滤
"""

from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

import pytest

# google-cloud-bigquery 是可选依赖，未装时跳过部分用例
bigquery = pytest.importorskip(
    "google.cloud.bigquery", reason="需要 google-cloud-bigquery 才能构造参数"
)

from analytics.cost import queries
from analytics.cost.cli import (
    BillingConfig,
    _expand_account_id,
    load_billing_config,
)


# ---------- 模板内容 ----------

def test_monthly_by_service_sql_contains_partition_filter():
    """PRD §9.3 用例 7：monthly_by_service 必须有 _PARTITIONTIME 过滤。"""
    bundle = queries.monthly_by_service(
        "p.gcp_billing.gcp_billing_export_v1_X",
        date(2026, 4, 1),
        date(2026, 5, 1),
    )
    assert "_PARTITIONTIME" in bundle.sql


def test_monthly_by_resource_sql_contains_partition_filter():
    bundle = queries.monthly_by_resource(
        "p.gcp_billing.gcp_billing_export_resource_v1_X",
        date(2026, 5, 1),
    )
    assert "_PARTITIONTIME" in bundle.sql


def test_daily_trend_sql_contains_partition_filter():
    bundle = queries.daily_trend(
        "p.gcp_billing.gcp_billing_export_v1_X",
        date(2026, 5, 1),
        date(2026, 5, 7),
    )
    assert "_PARTITIONTIME" in bundle.sql


def test_net_cost_includes_credits():
    """PRD §9.3 用例 6：所有模板的净成本必须把 credits 加进来。"""
    bundles = [
        queries.monthly_by_service(
            "t", date(2026, 4, 1), date(2026, 5, 1)
        ),
        queries.monthly_by_resource("t", date(2026, 5, 1)),
        queries.daily_trend("t", date(2026, 5, 1), date(2026, 5, 7)),
    ]
    for b in bundles:
        # 必须 SUM(cost) 和 UNNEST(credits) 都出现
        assert re.search(r"SUM\s*\(\s*cost\s*\)", b.sql, re.I), b.sql
        assert "UNNEST(credits)" in b.sql, b.sql


# ---------- 参数化 ----------

def test_service_filter_added_only_when_supplied():
    no_filter = queries.daily_trend("t", date(2026, 5, 1), date(2026, 5, 7))
    assert "@service_filter" not in no_filter.sql
    assert all(p.name != "service_filter" for p in no_filter.params)

    with_filter = queries.daily_trend(
        "t", date(2026, 5, 1), date(2026, 5, 7), service_filter="BigQuery"
    )
    assert "@service_filter" in with_filter.sql
    assert any(p.name == "service_filter" for p in with_filter.params)


def test_monthly_by_resource_top_n_param():
    bundle = queries.monthly_by_resource("t", date(2026, 5, 1), top_n=10)
    top_n_param = next(p for p in bundle.params if p.name == "top_n")
    assert top_n_param.value == 10


# ---------- 配置加载 ----------

def test_expand_account_id_replaces_dash():
    expanded = _expand_account_id(
        "gcp_billing_export_v1_${billing_account_id}",
        "01ABCD-23EFGH-45IJKL",
    )
    assert expanded == "gcp_billing_export_v1_01ABCD_23EFGH_45IJKL"


def test_load_billing_config_requires_account_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("GCP_BILLING_ACCOUNT_ID", raising=False)
    cfg_file = tmp_path / "gcp_billing.yaml"
    cfg_file.write_text(
        "project_id: data-aquarium\n"
        "billing_dataset: gcp_billing\n"
        "billing_account_id: ''\n"
        "location: asia-east2\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="billing_account_id"):
        load_billing_config(cfg_file)


def test_load_billing_config_env_var_overrides(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    cfg_file = tmp_path / "gcp_billing.yaml"
    cfg_file.write_text(
        "project_id: data-aquarium\n"
        "billing_dataset: gcp_billing\n"
        "billing_account_id: 'in-config'\n"
        "location: asia-east2\n"
        "tables:\n"
        "  standard: 'gcp_billing_export_v1_${billing_account_id}'\n"
        "  detailed: 'gcp_billing_export_resource_v1_${billing_account_id}'\n"
        "  pricing: 'cloud_pricing_export'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("GCP_BILLING_ACCOUNT_ID", "01AB-23CD-45EF")
    cfg = load_billing_config(cfg_file)
    assert cfg.billing_account_id == "01AB-23CD-45EF"
    # 表名内 "-" 替换为 "_"
    assert cfg.standard_table_name == "gcp_billing_export_v1_01AB_23CD_45EF"
    assert cfg.detailed_table_name == "gcp_billing_export_resource_v1_01AB_23CD_45EF"
    assert cfg.standard_table == "data-aquarium.gcp_billing.gcp_billing_export_v1_01AB_23CD_45EF"


def test_load_billing_config_from_repo_default():
    """仓库内 config/gcp_billing.yaml 必须能被解析（即使 ID 为空只到加载阶段）。"""
    path = Path("config/gcp_billing.yaml")
    if not path.exists():
        pytest.skip("当前工作目录无 config/gcp_billing.yaml")
    # 模拟设置环境变量，避免 account_id 为空报错
    os.environ["GCP_BILLING_ACCOUNT_ID"] = "TEST-ACCT-ID01"
    try:
        cfg = load_billing_config(path)
        assert cfg.project_id == "data-aquarium"
        assert cfg.billing_dataset == "gcp_billing"
        assert cfg.location == "asia-east2"
    finally:
        del os.environ["GCP_BILLING_ACCOUNT_ID"]
