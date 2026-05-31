from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backtesting.outcome_event_capture import append_outcome_event  # noqa: E402
from app.backtesting.outcome_lifecycle_integration import (  # noqa: E402
    OUTCOME_LIFECYCLE_INTEGRATION_SCHEMA_VERSION,
    OutcomeLifecycleIntegrationResult,
    OutcomeLifecycleIssue,
    audit_outcome_lifecycle_from_files,
    outcome_lifecycle_result_to_dict,
)

DEFAULT_EVENTS_OUTPUT_PATH = PROJECT_ROOT / "replay_validation" / "outcome_events.jsonl"
MAX_HUMAN_ITEMS = 10
MAX_HUMAN_ISSUES = 30


def default_artifact_paths(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    candidates = (
        project_root / "scan_output.json",
        project_root / "scan_runs" / "latest_scan.json",
        project_root / "scan_runs" / "watch_state.json",
        project_root / "scan_runs" / "performance_memory.json",
    )
    return tuple(path for path in candidates if path.exists())


def print_human_summary(
    result: OutcomeLifecycleIntegrationResult,
    *,
    dry_run: bool,
    output_path: Path | None,
    append_report: dict[str, Any],
    append_issues: Sequence[OutcomeLifecycleIssue],
) -> None:
    summary = result.summary
    _safe_print("Outcome Event Lifecycle Integration")
    _safe_print(f"Source: {result.source}")
    _safe_print(f"Candidates: {summary.total_candidates}")
    _safe_print(f"Eligible draft events: {summary.eligible_candidates}")
    _safe_print(f"Ineligible candidates: {summary.ineligible_candidates}")
    _safe_print(f"Terminal candidates: {summary.terminal_candidates}")
    _safe_print(f"Open candidates: {summary.open_candidates}")
    _safe_print(f"Negative examples: {summary.negative_example_candidates}")
    _safe_print(f"Unknown statuses: {summary.unknown_status_candidates}")
    _safe_print(f"Issues: {summary.warning_count} warning, {summary.error_count} error")
    _safe_print(f"Output: {_output_summary(dry_run=dry_run, output_path=output_path)}")
    _safe_print(f"Append eligible: {_append_summary(append_report)}")
    _safe_print()

    _print_counter("Mapped outcome statuses", summary.mapped_outcome_status_counts)
    _print_counter("Mapped terminal reasons", summary.mapped_terminal_reason_counts)
    _print_counter("Readiness blockers", summary.blocker_counts)

    visible_issues = [issue for issue in result.issues if issue.severity in {"warning", "blocker", "error"}]
    visible_issues.extend(append_issues)
    if visible_issues:
        _safe_print()
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
        description="Audit read-only lifecycle-to-outcome event draft integration from local replay artifacts.",
    )
    parser.add_argument("--input", dest="inputs", action="append", type=Path, help="Replay artifact JSON path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable lifecycle integration audit.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings or errors are found.")
    parser.add_argument("--output", type=Path, help="Optional JSON lifecycle integration summary path.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write output files or append event JSONL.")
    parser.add_argument("--append-eligible", action="store_true", help="Append eligible draft event payloads to JSONL.")
    parser.add_argument(
        "--events-output",
        type=Path,
        default=DEFAULT_EVENTS_OUTPUT_PATH,
        help="Outcome event JSONL path used with --append-eligible.",
    )
    parser.add_argument(
        "--allow-blockers",
        action="store_true",
        help="Allow --append-eligible to append blocked draft payloads for explicit research audit.",
    )
    args = parser.parse_args(argv)

    paths = list(args.inputs) if args.inputs else list(default_artifact_paths())
    result = audit_outcome_lifecycle_from_files(paths)
    append_report, append_issues = _append_eligible_events(
        result,
        requested=args.append_eligible,
        dry_run=args.dry_run,
        allow_blockers=args.allow_blockers,
        events_output_path=args.events_output,
    )

    if args.output is not None and not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(
            json.dumps(_summary_result_to_dict(result, append_report), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    if args.json:
        _safe_print(json.dumps(_cli_result_to_dict(result, append_report, append_issues), indent=2, sort_keys=True) + "\n", end="")
    else:
        print_human_summary(
            result,
            dry_run=args.dry_run,
            output_path=args.output,
            append_report=append_report,
            append_issues=append_issues,
        )

    append_error_count = sum(1 for issue in append_issues if issue.severity == "error")
    append_warning_count = sum(1 for issue in append_issues if issue.severity == "warning")
    if result.summary.error_count or append_error_count:
        return 1
    if args.strict and (result.summary.warning_count or append_warning_count):
        return 1
    return 0


def _append_eligible_events(
    result: OutcomeLifecycleIntegrationResult,
    *,
    requested: bool,
    dry_run: bool,
    allow_blockers: bool,
    events_output_path: Path,
) -> tuple[dict[str, Any], tuple[OutcomeLifecycleIssue, ...]]:
    report = {
        "requested": requested,
        "dry_run": dry_run,
        "allow_blockers": allow_blockers,
        "events_output_path": str(events_output_path),
        "eligible_candidates": result.summary.eligible_candidates,
        "appended_events": 0,
        "dry_run_candidates": 0,
        "skipped_ineligible_candidates": 0,
        "skipped_blocked_candidates": 0,
    }
    issues: list[OutcomeLifecycleIssue] = []

    if not requested:
        return report, ()

    for candidate in result.candidates:
        if not candidate.is_outcome_event_eligible:
            report["skipped_ineligible_candidates"] += 1
            continue
        if candidate.blockers and not allow_blockers:
            report["skipped_blocked_candidates"] += 1
            issues.append(
                OutcomeLifecycleIssue(
                    severity="error",
                    code="append_blocked_candidate_refused",
                    message="Eligible draft has blockers; rerun with --allow-blockers to append it explicitly.",
                    path="append_eligible",
                    candidate_id=candidate.candidate_id,
                )
            )
            continue
        if dry_run:
            report["dry_run_candidates"] += 1
            continue

        append_result = append_outcome_event(events_output_path, candidate.payload_preview)
        if append_result.appended:
            report["appended_events"] += 1
        issues.extend(_issue_from_append_issue(issue, candidate_id=candidate.candidate_id) for issue in append_result.issues)

    return report, tuple(issues)


def _issue_from_append_issue(issue: object, *, candidate_id: str) -> OutcomeLifecycleIssue:
    severity = _text(getattr(issue, "severity", "warning")).lower()
    if severity not in {"warning", "blocker", "error"}:
        severity = "warning"
    return OutcomeLifecycleIssue(
        severity=severity,  # type: ignore[arg-type]
        code=f"append_{_text(getattr(issue, 'code', 'issue'))}",
        message=_text(getattr(issue, "message", issue)),
        path=_text(getattr(issue, "path", "append")),
        candidate_id=candidate_id,
        field_name=_text(getattr(issue, "field_name", "N/A")),
    )


def _cli_result_to_dict(
    result: OutcomeLifecycleIntegrationResult,
    append_report: Mapping[str, Any],
    append_issues: Sequence[OutcomeLifecycleIssue],
) -> dict[str, Any]:
    payload = outcome_lifecycle_result_to_dict(result)
    payload.pop("candidates", None)
    payload["append"] = dict(append_report)
    if append_issues:
        payload["append_issues"] = [_issue_to_dict(issue) for issue in append_issues]
    return payload


def _summary_result_to_dict(
    result: OutcomeLifecycleIntegrationResult,
    append_report: Mapping[str, Any],
) -> dict[str, Any]:
    payload = outcome_lifecycle_result_to_dict(result)
    return {
        "schema_version": payload["schema_version"],
        "source": payload["source"],
        "summary": payload["summary"],
        "append": dict(append_report),
        "safety_note": payload["safety_note"],
    }


def _issue_to_dict(issue: OutcomeLifecycleIssue) -> dict[str, Any]:
    return asdict(issue)


def _output_summary(*, dry_run: bool, output_path: Path | None) -> str:
    if output_path is None:
        return "not requested"
    if dry_run:
        return "dry-run; no lifecycle integration output file written"
    return str(output_path)


def _append_summary(report: Mapping[str, Any]) -> str:
    if not report["requested"]:
        return "not requested"
    if report["dry_run"]:
        return f"dry-run; {report['dry_run_candidates']} candidate(s) would append to {report['events_output_path']}"
    return (
        f"{report['appended_events']} appended to {report['events_output_path']}; "
        f"{report['skipped_blocked_candidates']} blocked skipped"
    )


def _print_counter(label: str, counts: dict[str, int]) -> None:
    _safe_print(f"{label}: {_format_counter(counts)}")


def _format_counter(counts: dict[str, int]) -> str:
    if not counts:
        return "N/A"
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_HUMAN_ITEMS]
    return ", ".join(f"{name}={count}" for name, count in items)


def _text(value: object) -> str:
    if value is None:
        return "N/A"
    text = str(value).strip()
    return text if text else "N/A"


def _safe_print(value: object = "", *, end: str = "\n") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"), end=end)


if __name__ == "__main__":
    raise SystemExit(main())
