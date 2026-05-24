#!/usr/bin/env python3
"""从 Cloud Run 日志同步 DAY_* 结构化行到本地 live CSV。

脚本每次从指定时间点开始重读日志并重写 CSV，避免增量状态损坏导致重复行。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from analytics.daily_diagnostics import (
    DAILY_CANDIDATE_HEADERS,
    DAILY_POSITION_HEADERS,
    DAILY_SUMMARY_HEADERS,
    write_two_header_csv,
)


def _parse_value(raw: str) -> Any:
    if raw == "":
        return ""
    lower = raw.lower()
    if lower == "true":
        return True
    if lower == "false":
        return False
    try:
        if any(ch in raw for ch in (".", "e", "E")):
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _parse_kv(payload: str, marker: str) -> dict[str, Any] | None:
    if marker not in payload:
        return None
    raw = payload.split(marker, 1)[1].strip()
    row: dict[str, Any] = {}
    for part in raw.split():
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        row[key] = _parse_value(value)
    return row if row else None


def _read_logs(project: str, job_name: str, since: str, limit: int) -> list[str]:
    flt = (
        'resource.type="cloud_run_job" '
        f'AND resource.labels.job_name="{job_name}" '
        f'AND timestamp>="{since}"'
    )
    cmd = [
        "gcloud",
        "logging",
        "read",
        flt,
        f"--project={project}",
        f"--limit={limit}",
        "--format=json",
    ]
    proc = subprocess.run(cmd, check=True, capture_output=True, text=True)
    entries = json.loads(proc.stdout or "[]")
    payloads = []
    for entry in entries:
        text = entry.get("textPayload")
        if text:
            payloads.append(str(text))
    return payloads


def sync_logs(project: str, job_name: str, since: str, output_dir: Path, limit: int) -> dict[str, int]:
    payloads = _read_logs(project, job_name, since, limit)
    summaries: dict[str, dict[str, Any]] = {}
    positions: dict[tuple[str, str], dict[str, Any]] = {}
    candidates: dict[tuple[str, int, str], dict[str, Any]] = {}

    for payload in payloads:
        summary = _parse_kv(payload, "DAY_SUMMARY")
        if summary and "date" in summary:
            summaries[str(summary["date"])] = summary
            continue

        position = _parse_kv(payload, "DAY_POSITION")
        if position and "date" in position and "code" in position:
            positions[(str(position["date"]), str(position["code"]))] = position
            continue

        candidate = _parse_kv(payload, "DAY_CANDIDATE")
        if candidate and "date" in candidate and "rank" in candidate and "code" in candidate:
            candidates[
                (str(candidate["date"]), int(candidate["rank"]), str(candidate["code"]))
            ] = candidate

    output_dir.mkdir(parents=True, exist_ok=True)
    write_two_header_csv(
        output_dir / "daily_log_live.csv",
        DAILY_SUMMARY_HEADERS,
        [summaries[k] for k in sorted(summaries)],
    )
    write_two_header_csv(
        output_dir / "daily_positions_live.csv",
        DAILY_POSITION_HEADERS,
        [positions[k] for k in sorted(positions)],
    )
    write_two_header_csv(
        output_dir / "daily_candidates_live.csv",
        DAILY_CANDIDATE_HEADERS,
        [candidates[k] for k in sorted(candidates)],
    )
    return {
        "summary_rows": len(summaries),
        "position_rows": len(positions),
        "candidate_rows": len(candidates),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", default="data-aquarium")
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--since", required=True, help="日志起始时间，如 2026-05-24T15:11:30Z")
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--limit", type=int, default=50000)
    args = parser.parse_args()

    counts = sync_logs(
        project=args.project,
        job_name=args.job_name,
        since=args.since,
        output_dir=Path(args.output_dir),
        limit=args.limit,
    )
    print(
        "synced "
        f"summary={counts['summary_rows']} "
        f"positions={counts['position_rows']} "
        f"candidates={counts['candidate_rows']} "
        f"to={args.output_dir}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
