"""GCP 账单成本分析 CLI。

用法：
    python -m analytics.cost monthly-by-service [--months N]
    python -m analytics.cost monthly-by-resource --month YYYYMM [--service NAME] [--top-n N]
    python -m analytics.cost daily-trend [--days N] [--service NAME]

配套 PRD：PRD_20260524_04。
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable

import yaml

from analytics.cost import queries
from analytics.cost.queries import QueryBundle


DEFAULT_CONFIG_PATH = Path("config/gcp_billing.yaml")


# ---------- 配置 ----------

@dataclass(frozen=True)
class BillingConfig:
    project_id: str
    billing_dataset: str
    billing_account_id: str
    location: str
    standard_table_name: str
    detailed_table_name: str
    pricing_table_name: str
    lookback_days: int
    currency: str

    @property
    def standard_table(self) -> str:
        return f"{self.project_id}.{self.billing_dataset}.{self.standard_table_name}"

    @property
    def detailed_table(self) -> str:
        return f"{self.project_id}.{self.billing_dataset}.{self.detailed_table_name}"


def _expand_account_id(template: str, account_id: str) -> str:
    """BigQuery 表名不允许 '-'，需替换为 '_'。"""
    return template.replace("${billing_account_id}", account_id.replace("-", "_"))


def load_billing_config(path: Path | str = DEFAULT_CONFIG_PATH) -> BillingConfig:
    config_path = Path(path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"找不到配置文件 {config_path}。请确认在项目根目录运行，"
            f"或参考 PRD_20260524_04。"
        )
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}

    # billing_account_id 优先级：环境变量 > config 文件
    account_id = (
        os.environ.get("GCP_BILLING_ACCOUNT_ID", "").strip()
        or str(raw.get("billing_account_id", "")).strip()
    )
    if not account_id:
        raise RuntimeError(
            "billing_account_id 未配置。请在 config/gcp_billing.yaml 填入，"
            "或设置环境变量 GCP_BILLING_ACCOUNT_ID。\n"
            "Account ID 可在 GCP Console → Billing → 选定账号 → "
            "Account management 顶部获取，形如 01ABCD-23EFGH-45IJKL。"
        )

    tables = raw.get("tables", {})
    defaults = raw.get("defaults", {})
    return BillingConfig(
        project_id=str(raw.get("project_id", "data-aquarium")),
        billing_dataset=str(raw.get("billing_dataset", "gcp_billing")),
        billing_account_id=account_id,
        location=str(raw.get("location", "asia-east2")),
        standard_table_name=_expand_account_id(
            str(tables.get("standard", "gcp_billing_export_v1_${billing_account_id}")),
            account_id,
        ),
        detailed_table_name=_expand_account_id(
            str(tables.get(
                "detailed",
                "gcp_billing_export_resource_v1_${billing_account_id}",
            )),
            account_id,
        ),
        pricing_table_name=str(tables.get("pricing", "cloud_pricing_export")),
        lookback_days=int(defaults.get("lookback_days", 30)),
        currency=str(defaults.get("currency", "USD")),
    )


# ---------- 渲染 ----------

def _print_table(rows: Iterable[dict], columns: list[str]) -> None:
    rows = list(rows)
    if not rows:
        print("(无数据)")
        return
    widths = {c: max(len(c), max((len(str(r.get(c, ""))) for r in rows), default=0)) for c in columns}
    header = " | ".join(c.ljust(widths[c]) for c in columns)
    sep = "-+-".join("-" * widths[c] for c in columns)
    print(header)
    print(sep)
    for r in rows:
        print(" | ".join(str(r.get(c, "")).ljust(widths[c]) for c in columns))


def _ascii_bar(value: float, max_value: float, width: int = 40) -> str:
    if max_value <= 0:
        return ""
    filled = int(round(width * value / max_value))
    return "█" * filled + "·" * (width - filled)


# ---------- BigQuery 执行 ----------

def _run_query(cfg: BillingConfig, bundle: QueryBundle) -> list[dict]:
    """执行 SQL 并返回 dict 列表。

    认证走 google.cloud.bigquery 默认 ADC，与项目其他位置保持一致。
    不依赖 bigquery_pipeline/config.yaml（账单分析独立）。
    """
    from google.cloud import bigquery  # type: ignore

    client = bigquery.Client(project=cfg.project_id, location=cfg.location)
    job_config = bigquery.QueryJobConfig(query_parameters=bundle.params)
    job = client.query(bundle.sql, job_config=job_config, location=cfg.location)
    return [dict(row) for row in job.result()]


# ---------- 子命令 ----------

def cmd_monthly_by_service(args: argparse.Namespace) -> int:
    cfg = load_billing_config(args.config)
    today = date.today()
    start_month = (today.replace(day=1)
                   - timedelta(days=31 * (args.months - 1))).replace(day=1)
    end_month = today.replace(day=1)

    bundle = queries.monthly_by_service(cfg.standard_table, start_month, end_month)
    if args.dry_run:
        print(bundle.sql)
        return 0
    rows = _run_query(cfg, bundle)
    _print_table(rows, ["invoice_month", "service", "net_cost", "currency"])
    return 0


def cmd_monthly_by_resource(args: argparse.Namespace) -> int:
    cfg = load_billing_config(args.config)
    month = datetime.strptime(args.month, "%Y%m").date().replace(day=1)
    bundle = queries.monthly_by_resource(
        cfg.detailed_table, month, args.service, args.top_n
    )
    if args.dry_run:
        print(bundle.sql)
        return 0
    rows = _run_query(cfg, bundle)
    _print_table(
        rows,
        ["service", "sku", "resource_name", "resource_global_name", "net_cost", "currency"],
    )
    return 0


def cmd_daily_trend(args: argparse.Namespace) -> int:
    cfg = load_billing_config(args.config)
    end_date = date.today()
    start_date = end_date - timedelta(days=args.days - 1)
    bundle = queries.daily_trend(
        cfg.standard_table, start_date, end_date, args.service
    )
    if args.dry_run:
        print(bundle.sql)
        return 0
    rows = _run_query(cfg, bundle)
    if not rows:
        print("(无数据)")
        return 0
    max_cost = max((float(r["net_cost"]) for r in rows), default=0.0)
    print(f"{'usage_date':<12} | {'net_cost':>10} | trend")
    print("-" * 70)
    for r in rows:
        cost = float(r["net_cost"])
        bar = _ascii_bar(cost, max_cost)
        print(f"{str(r['usage_date']):<12} | {cost:>10.4f} | {bar}")
    return 0


# ---------- argparse ----------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m analytics.cost",
        description="GCP 账单导出 BigQuery 成本分析（PRD_20260524_04）",
    )
    # 公共 flag 放在父 parser，并在每个子 parser 上再挂一份，
    # 这样 `--dry-run` 在子命令前后均可用。
    def _add_common(parser_obj):
        parser_obj.add_argument(
            "--config",
            default=str(DEFAULT_CONFIG_PATH),
            help=f"配置文件路径 (默认 {DEFAULT_CONFIG_PATH})",
        )
        parser_obj.add_argument(
            "--dry-run",
            action="store_true",
            help="只打印 SQL，不实际执行",
        )

    _add_common(p)
    sub = p.add_subparsers(dest="cmd", required=True)

    s1 = sub.add_parser("monthly-by-service", help="近 N 个月按服务汇总净成本")
    _add_common(s1)
    s1.add_argument("--months", type=int, default=3, help="回看月数 (默认 3)")
    s1.set_defaults(func=cmd_monthly_by_service)

    s2 = sub.add_parser("monthly-by-resource", help="某月按资源拆分净成本")
    _add_common(s2)
    s2.add_argument("--month", required=True, help="目标月份 YYYYMM，如 202605")
    s2.add_argument("--service", default=None, help="服务名过滤（精确匹配）")
    s2.add_argument("--top-n", type=int, default=50, help="返回前 N (默认 50)")
    s2.set_defaults(func=cmd_monthly_by_resource)

    s3 = sub.add_parser("daily-trend", help="按日成本趋势")
    _add_common(s3)
    s3.add_argument("--days", type=int, default=30, help="回看天数 (默认 30)")
    s3.add_argument("--service", default=None, help="服务名过滤")
    s3.set_defaults(func=cmd_daily_trend)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
