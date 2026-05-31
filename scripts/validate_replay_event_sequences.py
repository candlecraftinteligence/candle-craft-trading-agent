from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backtesting.replay_event_sequence_validator import (  # noqa: E402
    ReplaySequenceValidationResult,
    replay_sequence_validation_result_to_dict,
    validate_replay_event_sequence_from_files,
)

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


def print_human_summary(result: ReplaySequenceValidationResult, *, dry_run: bool, output_path: Path | None) -> None:
    summary = result.summary
    _safe_print("Historical Replay Event Sequence Validation")
    _safe_print(f"Source: {result.source}")
    _safe_print(f"Events: {summary.total_events}")
    _safe_print(f"Groups: {summary.total_groups}")
    _safe_print(f"Sequence ready: {summary.sequence_ready_groups}")
    _safe_print(f"Sequence not ready: {summary.sequence_not_ready_groups}")
    _safe_print(f"Sequence ready rate: {summary.sequence_ready_rate:.2%}")
    _safe_print(f"Negative example groups: {summary.negative_example_groups}")
    _safe_print(f"Trade-like groups: {summary.trade_like_groups}")
    _safe_print(f"Terminal groups: {summary.terminal_groups}")
    _safe_print(f"Unknown identity groups: {summary.unknown_identity_groups}")
    _safe_print(f"Groups missing timestamps: {summary.groups_missing_timestamps}")
    _safe_print(f"Suspicious transition groups: {summary.groups_with_suspicious_transitions}")
    _safe_print(f"Duplicate events: {summary.duplicate_event_count}")
    _safe_print(f"Timestamp order issues: {summary.timestamp_order_issue_count}")
    _safe_print(f"Issues: {summary.warning_count} warning, {summary.error_count} error")
    _safe_print(f"Output: {_output_summary(dry_run=dry_run, output_path=output_path)}")
    _safe_print()

    _print_counter("Top statuses", summary.status_counts)
    _print_counter("Top event types", summary.event_type_counts)
    _print_counter("Top transitions", summary.transition_counts)
    _print_counter("Top issue codes", summary.top_issue_codes)

    visible_issues = [issue for issue in result.issues if issue.severity != "info"]
    if visible_issues:
        _safe_print()
        _safe_print("Warning/Error Summary")
        for issue in visible_issues[:MAX_HUMAN_ISSUES]:
            _safe_print(f"- {issue.severity.upper()} {issue.code} ({issue.group_key}): {issue.message}")
        omitted = len(visible_issues) - MAX_HUMAN_ISSUES
        if omitted > 0:
            _safe_print(f"- INFO issue_output_truncated (root): {omitted} additional findings omitted; use --json.")

    _safe_print()
    _safe_print(f"Safety: {result.safety_note}")


def print_json_result(result: ReplaySequenceValidationResult) -> None:
    print(json.dumps(replay_sequence_validation_result_to_dict(result), indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate read-only historical replay event sequence consistency from local artifacts.",
    )
    parser.add_argument("--input", dest="inputs", action="append", type=Path, help="Replay artifact JSON path.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable event sequence validation.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings or errors are found.")
    parser.add_argument("--output", type=Path, help="Optional JSON event sequence validation output path.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write an output file.")
    args = parser.parse_args(argv)

    paths = list(args.inputs) if args.inputs else list(default_artifact_paths())
    result = validate_replay_event_sequence_from_files(paths)
    payload = replay_sequence_validation_result_to_dict(result)
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    if args.output is not None and not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")

    if args.json:
        _safe_print(content, end="")
    else:
        print_human_summary(result, dry_run=args.dry_run, output_path=args.output)

    if result.summary.error_count or (args.strict and result.summary.warning_count):
        return 1
    return 0


def _output_summary(*, dry_run: bool, output_path: Path | None) -> str:
    if output_path is None:
        return "not requested"
    if dry_run:
        return "dry-run; no validation output file written"
    return str(output_path)


def _print_counter(label: str, counts: dict[str, int]) -> None:
    _safe_print(f"{label}: {_format_counter(counts)}")


def _format_counter(counts: dict[str, int]) -> str:
    if not counts:
        return "N/A"
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_HUMAN_ITEMS]
    return ", ".join(f"{name}={count}" for name, count in items)


def _safe_print(value: object = "", *, end: str = "\n") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"), end=end)


if __name__ == "__main__":
    raise SystemExit(main())
