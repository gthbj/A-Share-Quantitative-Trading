from __future__ import annotations

import argparse
from pathlib import Path

from .ads import audit_ads, transform_ads
from .client import DEFAULT_CONFIG_PATH, load_config
from .dwd import audit_dwd, transform_dwd
from .dws import audit_dws, transform_dws
from .fundamental import (
    audit_equity_fundamental_features,
    audit_fundamental_inputs,
    repair_fundamental_inputs,
    transform_equity_fundamental_features,
)
from .financial import (
    audit_equity_valuation_features,
    audit_financial_indicator,
    load_financial_indicator,
    transform_equity_valuation_features,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run BigQuery-internal A-share DWD/DWS/ADS pipeline tasks.")
    sub = parser.add_subparsers(dest="command", required=True)

    for command in (
        "repair-financial-indicator",
        "audit-financial-indicator",
        "transform-equity-valuation-features",
        "audit-equity-valuation-features",
        "repair-fundamental-inputs",
        "audit-fundamental-inputs",
        "transform-equity-fundamental-features",
        "audit-equity-fundamental-features",
        "transform-dwd",
        "audit-dwd",
        "transform-dws",
        "audit-dws",
        "transform-ads",
        "audit-ads",
    ):
        p = sub.add_parser(command)
        p.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))

    sub.choices["transform-dwd"].add_argument("--table")
    sub.choices["audit-dwd"].add_argument("--table")
    sub.choices["transform-dws"].add_argument("--table")
    sub.choices["audit-dws"].add_argument("--table")
    sub.choices["transform-ads"].add_argument("--table")
    sub.choices["audit-ads"].add_argument("--table")

    args = parser.parse_args()
    config = load_config(Path(args.config))

    if args.command == "repair-financial-indicator":
        load_financial_indicator(config)
        return 0
    if args.command == "audit-financial-indicator":
        audit_financial_indicator(config)
        return 0
    if args.command == "transform-equity-valuation-features":
        transform_equity_valuation_features(config)
        return 0
    if args.command == "audit-equity-valuation-features":
        audit_equity_valuation_features(config)
        return 0
    if args.command == "repair-fundamental-inputs":
        repair_fundamental_inputs(config)
        return 0
    if args.command == "audit-fundamental-inputs":
        audit_fundamental_inputs(config)
        return 0
    if args.command == "transform-equity-fundamental-features":
        transform_equity_fundamental_features(config)
        return 0
    if args.command == "audit-equity-fundamental-features":
        audit_equity_fundamental_features(config)
        return 0
    if args.command == "transform-dwd":
        transform_dwd(config, target_table=args.table)
        return 0
    if args.command == "audit-dwd":
        audit_dwd(config, target_table=args.table)
        return 0
    if args.command == "transform-dws":
        transform_dws(config, target_table=args.table)
        return 0
    if args.command == "audit-dws":
        audit_dws(config, target_table=args.table)
        return 0
    if args.command == "transform-ads":
        transform_ads(config, target_table=args.table)
        return 0
    if args.command == "audit-ads":
        audit_ads(config, target_table=args.table)
        return 0
    raise AssertionError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
