from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analytics.scan_persistence_audit import (  # noqa: E402
    ScanPersistenceAuditResult,
    audit_scan_persistence_file,
)


def default_artifact_paths(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    candidates: list[Path] = []
    scan_output = project_root / "scan_output.json"
    if scan_output.exists():
        candidates.append(scan_output)

    scan_runs_dir = project_root / "scan_runs"
    if scan_runs_dir.exists():
        candidates.extend(sorted(scan_runs_dir.glob("*.json")))

    unique: list[Path] = []
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(path)
    return tuple(unique)


def print_human_report(results: Sequence[ScanPersistenceAuditResult]) -> None:
    print("Scan Persistence Audit")
    print(f"Artifacts inspected: {len(results)}")
    print()

    if not results:
        print("No default scan persistence JSON artifacts were found.")
        print("Default discovery checks scan_output.json and scan_runs/*.json.")
        return

    for result in results:
        status = "ERROR" if result.error_count else "OK"
        print(f"[{status}] {result.source}")
        print(f"  Type: {result.artifact_type}")
        print(f"  Valid: {str(result.is_valid).lower()}")
        print(f"  Results: {_display_count(result.result_count)}")
        print(f"  Symbols: {_display_count(result.symbol_count)}")
        print(f"  Issues: {result.info_count} info, {result.warning_count} warning, {result.error_count} error")
        if result.status_counts:
            statuses = ", ".join(f"{status}={count}" for status, count in result.status_counts.items())
            print(f"  Statuses: {statuses}")
        if result.inspected_fields:
            print(f"  Inspected fields: {', '.join(result.inspected_fields)}")
        for issue in result.issues:
            print(f"  - {issue.severity.upper()} {issue.code} ({issue.path}): {issue.message}")
        print(f"  Safety: {result.safety_note}")
        print()


def print_json_report(results: Sequence[ScanPersistenceAuditResult]) -> None:
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
        description="Read-only audit of local scan persistence JSON artifacts.",
    )
    parser.add_argument("paths", nargs="*", type=Path, help="JSON artifact path(s) to audit.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable audit results.")
    args = parser.parse_args(argv)

    paths = tuple(args.paths) if args.paths else default_artifact_paths()
    results = tuple(audit_scan_persistence_file(path) for path in paths)

    if args.json:
        print_json_report(results)
    else:
        print_human_report(results)
    return 1 if any(result.error_count for result in results) else 0


def _display_count(value: int | None) -> str:
    return "N/A" if value is None else str(value)


if __name__ == "__main__":
    raise SystemExit(main())
