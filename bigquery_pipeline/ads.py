from __future__ import annotations


def transform_ads(config: dict, target_table: str | None = None) -> None:
    raise RuntimeError(
        "ADS transforms have been split out of gcs_to_bigquery. "
        "No ADS rebuild is required for the financial indicator fix."
    )


def audit_ads(config: dict, target_table: str | None = None) -> None:
    raise RuntimeError(
        "ADS audits have been split out of gcs_to_bigquery. "
        "No ADS audit is required for the financial indicator fix."
    )

