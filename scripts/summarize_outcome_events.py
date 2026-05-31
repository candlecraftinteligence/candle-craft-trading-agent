from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backtesting.outcome_event_capture import (  # noqa: E402
    OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION,
    OutcomeEventIssue,
    outcome_event_summary_to_dict,
    read_outcome_events,
)

DEFAULT_INPUT_PATH = PROJECT_ROOT / "replay_validation" / "outcome_events.jsonl"
MAX_HUMAN_ISSUES = 30
MAX_HUMAN_COUNTER_ITEMS = 10


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Summarize local outcome event JSONL records.")
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT_PATH, help="Outcome event JSONL path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable outcome event summary.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings or errors are found.")
    args = parser.parse_args(argv)

    result = read_outcome_events(args.input)
    payload = {
        "schema_version": OUTCOME_EVENT_CAPTURE_SCHEMA_VERSION,
        "input_path": str(args.input),
        "summary": outcome_event_summary_to_dict(result.summary),
        "issues": [_issue_to_dict(issue) for issue in result.issues],
    }

    if args.json:
        _safe_print(json.dumps(payload, indent=2, sort_keys=True) + "\n", end="")
    else:
        _print_human(payload)

    has_errors = result.summary.error_count > 0
    has_warnings = result.summary.warning_count > 0
    if has_errors or (args.strict and has_warnings):
        return 1
    return 0


def _print_human(payload: dict[str, Any]) -> None:
    summary = payload["summary"]
    _safe_print("Outcome Event Summary")
    _safe_print(f"Input: {payload['input_path']}")
    _safe_print(f"Events: {summary['total_events']}")
    _safe_print(f"Valid events: {summary['valid_events']}")
    _safe_print(f"Invalid events: {summary['invalid_events']}")
    _safe_print(f"Terminal events: {summary['terminal_events']}")
    _safe_print(f"Open events: {summary['open_events']}")
    _safe_print(f"Negative examples: {summary['negative_example_events']}")
    _safe_print(f"Events with result_r: {summary['events_with_result_r']}")
    _safe_print(f"Symbols: {summary['symbol_count']}")
    _safe_print(
        f"Issues: {summary['warning_count']} warning, "
        f"{summary['blocker_count']} blocker, {summary['error_count']} error"
    )
    _print_counter("Outcome statuses", summary["outcome_status_counts"])
    _print_counter("Terminal reasons", summary["terminal_reason_counts"])
    _print_counter("Strategy modes", summary["strategy_mode_counts"])

    issues = payload["issues"]
    if issues:
        _safe_print()
        _safe_print("Warning/Blocker/Error Summary")
        for issue in issues[:MAX_HUMAN_ISSUES]:
            line = f"- {issue['severity'].upper()} {issue['code']} ({issue['path']}): {issue['message']}"
            if issue.get("line_number"):
                line += f" [line {issue['line_number']}]"
            _safe_print(line)
        omitted = len(issues) - MAX_HUMAN_ISSUES
        if omitted > 0:
            _safe_print(f"- INFO issue_output_truncated (root): {omitted} additional findings omitted; use --json.")

    _safe_print()
    _safe_print(f"Safety: {summary['safety_note']}")


def _print_counter(label: str, counts: dict[str, int]) -> None:
    _safe_print(f"{label}: {_format_counter(counts)}")


def _format_counter(counts: dict[str, int]) -> str:
    if not counts:
        return "N/A"
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_HUMAN_COUNTER_ITEMS]
    return ", ".join(f"{name}={count}" for name, count in items)


def _issue_to_dict(issue: OutcomeEventIssue) -> dict[str, Any]:
    return asdict(issue)


def _safe_print(value: object = "", *, end: str = "\n") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"), end=end)


if __name__ == "__main__":
    raise SystemExit(main())
