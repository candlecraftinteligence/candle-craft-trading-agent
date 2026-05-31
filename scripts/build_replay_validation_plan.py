from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.backtesting.replay_validation_scaffold import (  # noqa: E402
    ReplayValidationResult,
    build_replay_validation_plan_from_files,
    replay_validation_result_to_dict,
)

MAX_HUMAN_ITEMS = 10
MAX_HUMAN_ISSUES = 40


def default_artifact_paths(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    candidates = (
        project_root / "scan_output.json",
        project_root / "scan_runs" / "latest_scan.json",
        project_root / "scan_runs" / "watch_state.json",
        project_root / "scan_runs" / "performance_memory.json",
    )
    return tuple(path for path in candidates if path.exists())


def print_human_summary(result: ReplayValidationResult, *, dry_run: bool, output_path: Path | None) -> None:
    _safe_print("Historical Replay Validation Plan")
    _safe_print(f"Source: {result.source}")
    _safe_print(f"Candidates: {result.total_candidates}")
    _safe_print(f"Validation ready: {result.validation_ready_candidates}")
    _safe_print(f"Validation not ready: {result.validation_not_ready_candidates}")
    _safe_print(f"Validation ready rate: {result.validation_ready_rate:.2%}")
    _safe_print(f"Negative examples: {result.negative_example_candidates}")
    _safe_print(f"Trade-like candidates: {result.trade_like_candidates}")
    _safe_print(f"Terminal outcome candidates: {result.terminal_outcome_candidates}")
    _safe_print(f"Timeline events: {result.timeline_event_count}")
    _safe_print(f"Symbols: {result.symbol_count}")
    _safe_print(f"Issues: {result.warning_count} warning, {result.error_count} error")
    _safe_print(f"Output: {_output_summary(dry_run=dry_run, output_path=output_path)}")
    _safe_print()

    _print_counter("Timeframes", result.timeframe_counts)
    _print_counter("Strategy modes", result.strategy_mode_counts)
    _print_counter("Lifecycle buckets", result.lifecycle_bucket_counts)
    _print_counter("Setup research buckets", result.setup_research_bucket_counts)
    _print_counter("Validation blockers", result.blocker_counts)
    _print_counter("Validation warnings", result.warning_counts)

    if result.issues:
        _safe_print()
        _safe_print("Warning/Error Summary")
        for issue in result.issues[:MAX_HUMAN_ISSUES]:
            if issue.severity == "info":
                continue
            _safe_print(f"- {issue.severity.upper()} {issue.code} ({issue.path}): {issue.message}")
        omitted = len(result.issues) - MAX_HUMAN_ISSUES
        if omitted > 0:
            _safe_print(f"- INFO issue_output_truncated (root): {omitted} additional findings omitted; use --json.")

    _safe_print()
    _safe_print(f"Safety: {result.safety_note}")


def print_json_result(result: ReplayValidationResult) -> None:
    print(json.dumps(replay_validation_result_to_dict(result), indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build a read-only historical replay validation plan from local replay artifacts.",
    )
    parser.add_argument("--input", dest="inputs", action="append", type=Path, help="Replay artifact JSON path.")
    parser.add_argument("--json", action="store_true", help="Print a machine-readable validation plan.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings or errors are found.")
    parser.add_argument("--output", type=Path, help="Optional JSON validation plan output path.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write an output file.")
    args = parser.parse_args(argv)

    paths = list(args.inputs) if args.inputs else list(default_artifact_paths())
    result = build_replay_validation_plan_from_files(paths)
    payload = replay_validation_result_to_dict(result)
    content = json.dumps(payload, indent=2, sort_keys=True) + "\n"

    if args.output is not None and not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")

    if args.json:
        _safe_print(content, end="")
    else:
        print_human_summary(result, dry_run=args.dry_run, output_path=args.output)

    if result.error_count or (args.strict and result.warning_count):
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
