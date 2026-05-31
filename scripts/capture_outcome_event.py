from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backtesting.outcome_event_capture import (  # noqa: E402
    OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION,
    OutcomeEventCaptureSummary,
    OutcomeEventIssue,
    append_outcome_event,
    build_outcome_event_record,
    outcome_event_record_to_dict,
    outcome_event_summary_to_dict,
)

DEFAULT_OUTPUT_PATH = PROJECT_ROOT / "replay_validation" / "outcome_events.jsonl"
MAX_HUMAN_ISSUES = 30


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Capture one local outcome event payload as JSONL.")
    parser.add_argument("--input-json", required=True, type=Path, help="Path to one outcome event JSON object.")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT_PATH, help="Output JSONL path.")
    parser.add_argument("--dry-run", action="store_true", help="Validate and summarize without appending.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable capture result.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings, blockers, or errors are found.")
    parser.add_argument("--allow-blockers", action="store_true", help="Append invalid/blocking records for research audit.")
    args = parser.parse_args(argv)

    payload, load_issues = _load_payload(args.input_json)
    appended = False
    append_issues: tuple[OutcomeEventIssue, ...] = ()
    append_path = Path(args.output)
    record = None

    if payload is not None:
        record = build_outcome_event_record(payload, source=str(args.input_json))
        if not record.blockers and not args.dry_run:
            append_result = append_outcome_event(append_path, record)
            appended = append_result.appended
            append_issues = append_result.issues
            record = append_result.record or record
        elif record.blockers and args.allow_blockers and not args.dry_run:
            append_result = append_outcome_event(append_path, record)
            appended = append_result.appended
            append_issues = append_result.issues
            record = append_result.record or record

    issues = tuple(load_issues) + append_issues
    if record is not None:
        issues = issues + tuple(
            OutcomeEventIssue(
                severity="blocker",
                code="record_blocker",
                message=blocker,
                path="record.blockers",
                event_id=record.event_id,
            )
            for blocker in record.blockers
        )
        issues = issues + tuple(
            OutcomeEventIssue(
                severity="warning",
                code="record_warning",
                message=warning,
                path="record.warnings",
                event_id=record.event_id,
            )
            for warning in record.warnings
        )

    summary = _summary_for(record, issues)
    result = {
        "schema_version": OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION,
        "input_path": str(args.input_json),
        "output_path": str(append_path),
        "dry_run": args.dry_run,
        "allow_blockers": args.allow_blockers,
        "appended": appended,
        "summary": outcome_event_summary_to_dict(summary),
        "issues": [_issue_to_dict(issue) for issue in issues],
    }
    if args.json and record is not None:
        result["record"] = outcome_event_record_to_dict(record)

    if args.json:
        _safe_print(json.dumps(result, indent=2, sort_keys=True) + "\n", end="")
    else:
        _print_human(result, record=record)

    has_errors = summary.error_count > 0
    has_blockers = summary.blocker_count > 0
    has_warnings = summary.warning_count > 0
    if has_errors:
        return 1
    if has_blockers and not args.allow_blockers:
        return 1
    if args.strict and (has_warnings or has_blockers):
        return 1
    return 0


def _load_payload(path: Path) -> tuple[dict[str, Any] | None, tuple[OutcomeEventIssue, ...]]:
    if not path.exists():
        return None, (
            OutcomeEventIssue(
                severity="error",
                code="input_missing",
                message=f"Input JSON file not found: {path}",
                path=str(path),
            ),
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, (
            OutcomeEventIssue(
                severity="error",
                code="invalid_input_json",
                message=f"Input JSON is invalid: {exc.msg}",
                path=str(path),
                line_number=exc.lineno,
            ),
        )
    except OSError as exc:
        return None, (
            OutcomeEventIssue(
                severity="error",
                code="input_read_failed",
                message=f"Input JSON could not be read: {exc}",
                path=str(path),
            ),
        )
    if not isinstance(payload, dict):
        return None, (
            OutcomeEventIssue(
                severity="error",
                code="invalid_input_payload",
                message="Input JSON must contain a single outcome event object.",
                path=str(path),
            ),
        )
    return payload, ()


def _summary_for(record: object | None, issues: Sequence[OutcomeEventIssue]) -> OutcomeEventCaptureSummary:
    if record is None:
        return OutcomeEventCaptureSummary(
            warning_count=sum(1 for issue in issues if issue.severity == "warning"),
            blocker_count=sum(1 for issue in issues if issue.severity == "blocker"),
            error_count=sum(1 for issue in issues if issue.severity == "error"),
            is_valid=not any(issue.severity in {"error", "blocker"} for issue in issues),
        )

    from app.backtesting.outcome_event_capture import summarize_outcome_events

    summary = summarize_outcome_events([record])
    return OutcomeEventCaptureSummary(
        **{
            **outcome_event_summary_to_dict(summary),
            "warning_count": summary.warning_count + sum(1 for issue in issues if issue.severity == "warning"),
            "blocker_count": summary.blocker_count + sum(1 for issue in issues if issue.severity == "blocker"),
            "error_count": summary.error_count + sum(1 for issue in issues if issue.severity == "error"),
            "is_valid": not any(issue.severity in {"error", "blocker"} for issue in issues) and summary.is_valid,
        }
    )


def _print_human(result: dict[str, Any], *, record: object | None) -> None:
    summary = result["summary"]
    _safe_print("Outcome Event Capture")
    _safe_print(f"Input: {result['input_path']}")
    _safe_print(f"Output: {result['output_path']}")
    _safe_print(f"Dry run: {result['dry_run']}")
    _safe_print(f"Appended: {result['appended']}")
    if record is not None:
        _safe_print(f"Event ID: {getattr(record, 'event_id', 'N/A')}")
        _safe_print(f"Outcome status: {getattr(record, 'outcome_status', 'N/A')}")
    _safe_print(f"Events: {summary['total_events']}")
    _safe_print(
        f"Issues: {summary['warning_count']} warning, "
        f"{summary['blocker_count']} blocker, {summary['error_count']} error"
    )
    issues = result["issues"]
    if issues:
        _safe_print()
        _safe_print("Warning/Blocker/Error Summary")
        for issue in issues[:MAX_HUMAN_ISSUES]:
            _safe_print(f"- {issue['severity'].upper()} {issue['code']} ({issue['path']}): {issue['message']}")
        omitted = len(issues) - MAX_HUMAN_ISSUES
        if omitted > 0:
            _safe_print(f"- INFO issue_output_truncated (root): {omitted} additional findings omitted; use --json.")
    _safe_print()
    _safe_print(f"Safety: {summary['safety_note']}")


def _issue_to_dict(issue: OutcomeEventIssue) -> dict[str, Any]:
    return _jsonable(asdict(issue))


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return _jsonable(asdict(value))
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    return value


def _safe_print(value: object = "", *, end: str = "\n") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"), end=end)


if __name__ == "__main__":
    raise SystemExit(main())
