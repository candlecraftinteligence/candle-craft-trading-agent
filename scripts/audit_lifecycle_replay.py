from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analytics.lifecycle_replay_audit import (  # noqa: E402
    LifecycleReplayAuditResult,
    audit_lifecycle_file,
)

MAX_HUMAN_ISSUES_PER_ARTIFACT = 80


def default_artifact_paths(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    candidates = (
        project_root / "scan_output.json",
        project_root / "scan_runs" / "latest_scan.json",
        project_root / "scan_runs" / "watch_state.json",
        project_root / "scan_runs" / "performance_memory.json",
    )
    return tuple(path for path in candidates if path.exists())


def print_human_report(results: Sequence[LifecycleReplayAuditResult]) -> None:
    _safe_print("Lifecycle Replay Readiness Audit")
    _safe_print(f"Artifacts inspected: {len(results)}")
    _safe_print()

    if not results:
        _safe_print("No default lifecycle replay JSON artifacts were found.")
        _safe_print("Default discovery checks scan_output.json, latest_scan.json, watch_state.json, and performance_memory.json.")
        return

    for result in results:
        status = "ERROR" if result.error_count else "WARNING" if result.warning_count else "OK"
        _safe_print(f"[{status}] {result.source}")
        _safe_print(f"  Valid: {str(result.is_valid).lower()}")
        _safe_print(f"  Records: {result.record_count}")
        _safe_print(f"  Symbols: {result.symbol_count}")
        _safe_print(f"  Issues: {result.info_count} info, {result.warning_count} warning, {result.error_count} error")
        if result.status_counts:
            statuses = ", ".join(f"{status}={count}" for status, count in result.status_counts.items())
            _safe_print(f"  Statuses: {statuses}")
        if result.inspected_fields:
            _safe_print(f"  Inspected fields: {', '.join(result.inspected_fields)}")
        for issue in result.issues[:MAX_HUMAN_ISSUES_PER_ARTIFACT]:
            _safe_print(f"  - {issue.severity.upper()} {issue.code} ({issue.path}): {issue.message}")
        omitted = len(result.issues) - MAX_HUMAN_ISSUES_PER_ARTIFACT
        if omitted > 0:
            _safe_print(f"  - INFO issue_output_truncated (root): {omitted} additional issues omitted; use --json for full detail.")
        _safe_print(f"  Safety: {result.safety_note}")
        _safe_print()


def print_json_report(results: Sequence[LifecycleReplayAuditResult]) -> None:
    total_errors = sum(result.error_count for result in results)
    total_warnings = sum(result.warning_count for result in results)
    total_info = sum(result.info_count for result in results)
    payload = {
        "summary": {
            "artifact_count": len(results),
            "info_count": total_info,
            "warning_count": total_warnings,
            "error_count": total_errors,
            "is_valid": total_errors == 0,
        },
        "results": [result.model_dump(mode="json") for result in results],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of lifecycle replay readiness in local JSON artifacts.",
    )
    parser.add_argument("paths", nargs="*", type=Path, help="JSON artifact path(s) to audit.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable audit results.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings or errors are found.")
    args = parser.parse_args(argv)

    paths = tuple(args.paths) if args.paths else default_artifact_paths()
    results = tuple(audit_lifecycle_file(path) for path in paths)

    if args.json:
        print_json_report(results)
    else:
        print_human_report(results)

    has_errors = any(result.error_count for result in results)
    has_warnings = any(result.warning_count for result in results)
    if has_errors or (args.strict and has_warnings):
        return 1
    return 0


def _safe_print(value: object = "") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


if __name__ == "__main__":
    raise SystemExit(main())
