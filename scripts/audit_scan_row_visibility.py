from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCAN_RUN_MANIFEST_NAME = Path("scan_runs") / "scan_run_manifest.jsonl"
STALE_RUN_ID_WARNING = "Audited file run_id does not match latest manifest run_id; this file may be stale."

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

    source = _resolve_audit_source(args.path)
    payload = json.loads(source.audited_path.read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping):
        raise SystemExit(f"Saved scan JSON must be an object: {source.audited_path}")
    result = audit_scan_row_visibility(payload)
    result.update(_source_fields(source, result["run_id"]))
    if args.json:
        _safe_print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if result["warning"] != "N/A":
        _safe_print(f"WARNING: {result['warning']}")
    for key, value in result.items():
        _safe_print(f"{key}: {json.dumps(value, sort_keys=True) if isinstance(value, dict) else value}")
    return 0


class AuditSource:
    def __init__(
        self,
        *,
        audited_path: Path,
        manifest_run_id: str = "N/A",
        manifest_latest_scan_path: str = "N/A",
        warning: str = "N/A",
    ) -> None:
        self.audited_path = audited_path
        self.manifest_run_id = manifest_run_id
        self.manifest_latest_scan_path = manifest_latest_scan_path
        self.warning = warning


def _resolve_audit_source(path_arg: Path | None) -> AuditSource:
    manifest_row = _latest_manifest_row()
    if path_arg is None:
        if manifest_row is None:
            raise SystemExit(
                f"No scan path was provided and no manifest rows were found in {_manifest_path()}. "
                "Run a scan that writes scan_runs/scan_run_manifest.jsonl or pass a saved scan JSON path."
            )
        latest_scan_path = _text(manifest_row.get("latest_scan_path"))
        if latest_scan_path == "N/A":
            raise SystemExit(
                "Latest manifest row has no latest_scan_path. "
                "Run a validation scan with --save-run scan_runs/latest_scan.json or pass a saved scan JSON path."
            )
        path = _resolve_project_path(latest_scan_path)
        if not path.exists():
            raise SystemExit(f"Manifest latest_scan_path does not exist: {path}")
        return AuditSource(
            audited_path=path,
            manifest_run_id=_text(manifest_row.get("run_id")),
            manifest_latest_scan_path=latest_scan_path,
        )

    path = path_arg
    if not path.is_absolute():
        path = _resolve_project_path(str(path))
    if not path.exists():
        raise SystemExit(f"Saved scan JSON not found: {path}")
    manifest_run_id = _text(manifest_row.get("run_id")) if manifest_row is not None else "N/A"
    manifest_latest_scan_path = _text(manifest_row.get("latest_scan_path")) if manifest_row is not None else "N/A"
    return AuditSource(
        audited_path=path,
        manifest_run_id=manifest_run_id,
        manifest_latest_scan_path=manifest_latest_scan_path,
    )


def _source_fields(source: AuditSource, audited_file_run_id: Any) -> dict[str, Any]:
    audited_run_id = _text(audited_file_run_id)
    run_ids_match = source.manifest_run_id != "N/A" and source.manifest_run_id == audited_run_id
    warning = source.warning
    if source.manifest_run_id != "N/A" and audited_run_id != "N/A" and not run_ids_match:
        warning = STALE_RUN_ID_WARNING
    return {
        "manifest_run_id": source.manifest_run_id,
        "audited_path": str(source.audited_path),
        "audited_file_run_id": audited_run_id,
        "run_ids_match": run_ids_match,
        "manifest_latest_scan_path": source.manifest_latest_scan_path,
        "warning": warning,
    }


def _latest_manifest_row() -> Mapping[str, Any] | None:
    path = _manifest_path()
    if not path.exists():
        return None
    latest: Mapping[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, Mapping):
            latest = payload
    return latest


def _manifest_path() -> Path:
    return PROJECT_ROOT / SCAN_RUN_MANIFEST_NAME


def _resolve_project_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute():
        return path
    return PROJECT_ROOT / path


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
