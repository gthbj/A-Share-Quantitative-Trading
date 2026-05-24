from __future__ import annotations

from .client import bq_client, dws_table_name, table_id
from .fundamental import audit_equity_fundamental_features, transform_equity_fundamental_features
from .financial import audit_equity_valuation_features, transform_equity_valuation_features


DWS_TRANSFORMS = {
    "equity_valuation_features": transform_equity_valuation_features,
    "equity_fundamental_features": transform_equity_fundamental_features,
}

DWS_AUDITS = {
    "equity_valuation_features": audit_equity_valuation_features,
    "equity_fundamental_features": audit_equity_fundamental_features,
}


def transform_dws(config: dict, target_table: str | None = None) -> None:
    selected = _select(DWS_TRANSFORMS, target_table, "dws")
    for transform in selected.values():
        transform(config)


def audit_dws(config: dict, target_table: str | None = None) -> None:
    selected = _select(DWS_AUDITS, target_table, "dws")
    for audit in selected.values():
        audit(config)


def _select(registry: dict, target_table: str | None, layer: str) -> dict:
    if target_table is None:
        return registry
    key = target_table.removeprefix(f"{layer}_")
    if key not in registry:
        raise RuntimeError(f"Unknown {layer.upper()} target table: {target_table}")
    return {key: registry[key]}


def table_exists(config: dict, target_table: str) -> bool:
    client = bq_client(config)
    table_name = target_table if target_table.startswith("dws_") else dws_table_name(config, target_table)
    try:
        client.get_table(table_id(config, table_name))
        return True
    except Exception:
        return False
