from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCAN_PATHS = (
    PROJECT_ROOT / "scan_runs" / "nightly_latest_scan.json",
    PROJECT_ROOT / "scan_runs" / "latest_scan.json",
    PROJECT_ROOT / "scan_output.json",
)

ACTIVE_LIFECYCLE_STATES = {"CONFIRMED", "EXECUTING", "TRIGGERED"}
FAILED_DISPLAY_STATUSES = {"no_setup", "rejected", "rejected_by_scoring", "near_miss"}
FAILED_STAGES = {"pullback", "structure", "ob_fvg", "rr", "scoring", "target_integrity"}


def audit_scan_row_visibility(payload: Mapping[str, Any]) -> dict[str, Any]:
    rows = _result_rows(payload)
    failed_stage_counts = Counter(_text(row.get("failed_stage")) for row in rows)
    display_status_counts = Counter(_text(row.get("display_status")) for row in rows)
    lifecycle_state_counts = Counter(_text(row.get("lifecycle_current_state")) for row in rows)
    failed_stage_counts.pop("N/A", None)
    display_status_counts.pop("N/A", None)
    lifecycle_state_counts.pop("N/A", None)

    active_failed_rows = [row for row in rows if _active_failed_row(row)]
    active_failed_with_lifecycle_integrity = [
        row
        for row in active_failed_rows
        if _text(row.get("lifecycle_integrity_status")) == "STALE_OR_DEGRADED"
        and row.get("current_scan_gate_valid") is False
    ]
    active_failed_missing_lifecycle_integrity = [
        row for row in active_failed_rows if row not in active_failed_with_lifecycle_integrity
    ]

    return {
        "total_rows": len(rows),
        "run_id": _text(payload.get("run_id")),
        "top_level_failed_stage_target_integrity": failed_stage_counts.get("target_integrity", 0),
        "top_level_display_status_target_blocked": display_status_counts.get("target_blocked", 0),
        "active_failed_with_lifecycle_integrity": len(active_failed_with_lifecycle_integrity),
        "active_failed_missing_lifecycle_integrity": len(active_failed_missing_lifecycle_integrity),
        "failed_stage_counts": dict(sorted(failed_stage_counts.items())),
        "display_status_counts": dict(sorted(display_status_counts.items())),
        "lifecycle_state_counts": dict(sorted(lifecycle_state_counts.items())),
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit top-level scan row visibility fields.")
    parser.add_argument("path", nargs="?", type=Path, help="Saved scan JSON path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable audit output.")
    args = parser.parse_args(argv)

    path = args.path or _default_scan_path()
    if path is None:
        raise SystemExit("No saved scan JSON found. Pass a path explicitly.")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise SystemExit(f"Saved scan JSON must be an object: {path}")
    result = audit_scan_row_visibility(payload)
    if args.json:
        _safe_print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    for key, value in result.items():
        _safe_print(f"{key}: {json.dumps(value, sort_keys=True) if isinstance(value, dict) else value}")
    return 0


def _default_scan_path() -> Path | None:
    for path in DEFAULT_SCAN_PATHS:
        if path.exists():
            return path
    return None


def _result_rows(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    rows = payload.get("results")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
        return ()
    return tuple(row for row in rows if isinstance(row, Mapping))


def _active_failed_row(row: Mapping[str, Any]) -> bool:
    state = _text(row.get("lifecycle_current_state"))
    if state not in ACTIVE_LIFECYCLE_STATES:
        return False
    display_status = _text(row.get("display_status"))
    failed_stage = _text(row.get("failed_stage"))
    return display_status in FAILED_DISPLAY_STATUSES or failed_stage in FAILED_STAGES


def _text(value: Any) -> str:
    if value is None or value == "":
        return "N/A"
    return str(value)


def _safe_print(value: object = "") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


if __name__ == "__main__":
    raise SystemExit(main())
