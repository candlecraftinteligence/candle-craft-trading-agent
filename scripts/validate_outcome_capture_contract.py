from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backtesting.outcome_capture_contract import (  # noqa: E402
    OutcomeCaptureIssue,
    OutcomeCaptureValidationResult,
    build_outcome_capture_record,
    outcome_capture_result_to_dict,
    validate_outcome_capture_records,
)
from app.backtesting.replay_validation_scaffold import build_replay_validation_plan_from_files  # noqa: E402

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


def build_outcome_capture_validation_from_files(paths: list[Path]) -> OutcomeCaptureValidationResult:
    validation_result = build_replay_validation_plan_from_files(paths)
    records = [
        build_outcome_capture_record(candidate, source=validation_result.source)
        for candidate in validation_result.candidates
    ]
    result = validate_outcome_capture_records(records, source=validation_result.source)
    return _append_replay_validation_issues(result, validation_result.issues)


def print_human_summary(result: OutcomeCaptureValidationResult, *, dry_run: bool, output_path: Path | None) -> None:
    summary = result.summary
    _safe_print("Outcome Capture Contract Validation")
    _safe_print(f"Source: {result.source}")
    _safe_print(f"Records: {summary.total_records}")
    _safe_print(f"Valid records: {summary.valid_records}")
    _safe_print(f"Invalid records: {summary.invalid_records}")
    _safe_print(f"Captured records: {summary.captured_records}")
    _safe_print(f"Incomplete records: {summary.incomplete_records}")
    _safe_print(f"Negative examples: {summary.negative_example_records}")
    _safe_print(f"Terminal records: {summary.terminal_records}")
    _safe_print(f"Open records: {summary.open_records}")
    _safe_print(f"Unknown outcome records: {summary.unknown_outcome_records}")
    _safe_print(f"Records with result_r: {summary.records_with_result_r}")
    _safe_print(f"Records missing result_r: {summary.records_missing_result_r}")
    _safe_print(f"Records missing exit price: {summary.records_missing_exit_price}")
    _safe_print(f"Records missing terminal timestamp: {summary.records_missing_terminal_timestamp}")
    _safe_print(
        f"Issues: {summary.warning_count} warning, {summary.blocker_count} blocker, {summary.error_count} error"
    )
    _safe_print(f"Output: {_output_summary(dry_run=dry_run, output_path=output_path)}")
    _safe_print()

    _print_counter("Outcome statuses", summary.outcome_status_counts)
    _print_counter("Terminal reasons", summary.terminal_reason_counts)
    _print_counter("Missing fields", summary.field_missing_counts)

    visible_issues = [issue for issue in result.issues if issue.severity in {"warning", "blocker", "error"}]
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


def print_json_result(result: OutcomeCaptureValidationResult) -> None:
    print(json.dumps(_cli_result_to_dict(result), indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the read-only outcome capture contract from replay validation candidates.",
    )
    parser.add_argument("--input", dest="inputs", action="append", type=Path, help="Replay artifact JSON path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable outcome capture validation.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings, blockers, or errors are found.")
    parser.add_argument("--output", type=Path, help="Optional JSON outcome capture validation summary path.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write an output file.")
    args = parser.parse_args(argv)

    paths = list(args.inputs) if args.inputs else list(default_artifact_paths())
    result = build_outcome_capture_validation_from_files(paths)

    if args.output is not None and not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(_summary_result_to_dict(result), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if args.json:
        _safe_print(json.dumps(_cli_result_to_dict(result), indent=2, sort_keys=True) + "\n", end="")
    else:
        print_human_summary(result, dry_run=args.dry_run, output_path=args.output)

    if result.summary.error_count:
        return 1
    if args.strict and (result.summary.warning_count or result.summary.blocker_count):
        return 1
    return 0


def _append_replay_validation_issues(
    result: OutcomeCaptureValidationResult,
    replay_issues: Sequence[object],
) -> OutcomeCaptureValidationResult:
    mapped_issues = tuple(_issue_from_replay_validation_issue(issue) for issue in replay_issues if _issue_severity(issue) != "info")
    if not mapped_issues:
        return result

    warning_count = result.summary.warning_count + sum(1 for issue in mapped_issues if issue.severity == "warning")
    error_count = result.summary.error_count + sum(1 for issue in mapped_issues if issue.severity == "error")
    blocker_count = result.summary.blocker_count + sum(1 for issue in mapped_issues if issue.severity == "blocker")
    summary = replace(
        result.summary,
        warning_count=warning_count,
        error_count=error_count,
        blocker_count=blocker_count,
        is_valid=error_count == 0 and blocker_count == 0,
    )
    return replace(result, summary=summary, issues=result.issues + mapped_issues)


def _issue_from_replay_validation_issue(issue: object) -> OutcomeCaptureIssue:
    severity = _issue_severity(issue)
    if severity == "blocker":
        severity = "warning"
    return OutcomeCaptureIssue(
        severity=severity,
        code=f"replay_validation_{_text(getattr(issue, 'code', 'issue'))}",
        message=_text(getattr(issue, "message", issue)),
        path=_text(getattr(issue, "path", "replay_validation")),
    )


def _issue_severity(issue: object) -> str:
    severity = _text(getattr(issue, "severity", "warning")).lower()
    if severity in {"warning", "error", "blocker", "info"}:
        return severity
    return "warning"


def _cli_result_to_dict(result: OutcomeCaptureValidationResult) -> dict:
    payload = outcome_capture_result_to_dict(result)
    payload.pop("records", None)
    return payload


def _summary_result_to_dict(result: OutcomeCaptureValidationResult) -> dict:
    payload = outcome_capture_result_to_dict(result)
    return {
        "schema_version": payload["schema_version"],
        "source": payload["source"],
        "summary": payload["summary"],
        "safety_note": payload["safety_note"],
    }


def _output_summary(*, dry_run: bool, output_path: Path | None) -> str:
    if output_path is None:
        return "not requested"
    if dry_run:
        return "dry-run; no outcome capture validation file written"
    return str(output_path)


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
