from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.alerts.integrity_manifest import (  # noqa: E402
    AlertIntegrityAuditResult,
    alert_integrity_audit_to_dict,
    audit_alert_integrity_files,
)

MAX_HUMAN_RECORDS = 20
MAX_HUMAN_ISSUES = 40


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


def print_human_report(result: AlertIntegrityAuditResult) -> None:
    summary = result.summary
    _safe_print("Alert Integrity Manifest Audit")
    _safe_print(f"Source: {result.source}")
    _safe_print(f"Alerts inspected: {summary.alert_count}")
    _safe_print(f"Manifests present: {summary.manifest_count}")
    _safe_print(f"Missing manifests: {summary.missing_manifest_count}")
    _safe_print(f"Dry-run alerts: {summary.dry_run_alerts}")
    _safe_print(f"Live alerts: {summary.live_alerts}")
    _safe_print(f"Invalid alerts: {summary.invalid_alerts}")
    _safe_print(f"Issues: {summary.warning_count} warning, {summary.blocker_count} blocker, {summary.error_count} error")
    _safe_print()

    if result.records:
        _safe_print("Records")
        for record in result.records[:MAX_HUMAN_RECORDS]:
            valid = "valid" if record.manifest_valid else "review"
            _safe_print(
                f"- {record.path}: {record.symbol} status={record.status} channel={record.channel} "
                f"dry_run={str(record.dry_run).lower()} manifest={record.manifest_id} {valid}"
            )
        omitted = len(result.records) - MAX_HUMAN_RECORDS
        if omitted > 0:
            _safe_print(f"- INFO records_truncated: {omitted} additional records omitted; use --json.")
        _safe_print()

    visible_issues = [issue for issue in result.issues if issue.severity in {"warning", "blocker", "error"}]
    if visible_issues:
        _safe_print("Warning/Blocker/Error Summary")
        for issue in visible_issues[:MAX_HUMAN_ISSUES]:
            _safe_print(f"- {issue.severity.upper()} {issue.code} ({issue.path}): {issue.message}")
        omitted = len(visible_issues) - MAX_HUMAN_ISSUES
        if omitted > 0:
            _safe_print(f"- INFO issue_output_truncated (root): {omitted} additional findings omitted; use --json.")
        _safe_print()

    _safe_print(f"Safety: {result.safety_note}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only audit of alert integrity manifests in local scan/watch artifacts.",
    )
    parser.add_argument("paths", nargs="*", type=Path, help="JSON artifact path(s) to audit.")
    parser.add_argument("--input", dest="inputs", action="append", type=Path, help="JSON artifact path to audit.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable alert integrity audit.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings, blockers, or errors are found.")
    parser.add_argument("--output", type=Path, help="Optional JSON alert integrity audit output path.")
    args = parser.parse_args(argv)

    paths = tuple(args.paths) + tuple(args.inputs or ())
    if not paths:
        paths = default_artifact_paths()

    result = audit_alert_integrity_files(paths)
    payload = alert_integrity_audit_to_dict(result)

    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        _safe_print(json.dumps(payload, indent=2, sort_keys=True) + "\n", end="")
    else:
        print_human_report(result)

    if result.summary.error_count or result.summary.blocker_count:
        return 1
    if args.strict and result.summary.warning_count:
        return 1
    return 0


def _safe_print(value: object = "", *, end: str = "\n") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"), end=end)


if __name__ == "__main__":
    raise SystemExit(main())
