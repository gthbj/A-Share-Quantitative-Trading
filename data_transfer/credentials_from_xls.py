from __future__ import annotations

import argparse
from pathlib import Path

import yaml
import xlrd


def read_first_api_key(xls_path: Path) -> str:
    book = xlrd.open_workbook(str(xls_path))
    for sheet in book.sheets():
        for row_idx in range(sheet.nrows):
            for col_idx in range(sheet.ncols):
                value = str(sheet.cell_value(row_idx, col_idx)).strip()
                if value.startswith("AIza"):
                    return value
    raise ValueError(f"No Google API key found in {xls_path}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Extract local GCP API key Excel into ignored YAML.")
    parser.add_argument("--xls", default="D:/GCP_API_KEY.xls")
    parser.add_argument("--out", default="D:/git/A-Share-Quantitative-Trading/config/secrets.yaml")
    args = parser.parse_args()

    xls_path = Path(args.xls)
    out_path = Path(args.out)
    api_key = read_first_api_key(xls_path)

    existing = {}
    if out_path.exists():
        existing = yaml.safe_load(out_path.read_text(encoding="utf-8")) or {}

    existing.setdefault("gcp", {})
    existing["gcp"]["api_key"] = api_key
    existing["gcp"]["credential_note"] = (
        "API keys identify a project but do not authorize private GCS object uploads. "
        "Use a service account JSON for unattended uploads."
    )

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(yaml.safe_dump(existing, allow_unicode=True, sort_keys=False), encoding="utf-8")
    print(f"Wrote secret config: {out_path}")
    print("Credential type found: api_key")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
