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

from app.analytics.replay_dataset_export import export_replay_dataset_from_files  # noqa: E402
from app.analytics.replay_failure_taxonomy import (  # noqa: E402
    ReplayFailurePattern,
    ReplayFailureTaxonomyDimension,
    ReplayFailureTaxonomyResult,
    analyze_replay_failure_taxonomy,
    analyze_replay_failure_taxonomy_from_files,
    failure_taxonomy_result_to_dict,
)

MAX_HUMAN_ITEMS = 10
MAX_HUMAN_MESSAGES = 40


def default_artifact_paths(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    candidates = (
        project_root / "scan_output.json",
        project_root / "scan_runs" / "latest_scan.json",
        project_root / "scan_runs" / "watch_state.json",
        project_root / "scan_runs" / "performance_memory.json",
    )
    return tuple(path for path in candidates if path.exists())


def print_human_report(result: ReplayFailureTaxonomyResult) -> None:
    summary = result.summary
    _safe_print("Replay Failure Taxonomy Report")
    _safe_print(f"Source: {result.source}")
    _safe_print(f"Rows: {summary.total_rows}")
    _safe_print(f"Failure rows: {summary.failure_rows}")
    _safe_print(f"Failure row rate: {summary.failure_row_rate:.2%}")
    _safe_print(f"Issues: {result.warning_count} warning, {result.error_count} error")
    _safe_print()

    _print_counter("Top failure families", _dimension_counts(result, "failure_family"))
    _print_counter("Top first_failed_gate", _dimension_counts(result, "first_failed_gate"))
    _print_counter("Top rejection reasons", _dimension_counts(result, "rejection_reason"))
    _print_patterns("Top strategy mode failure patterns", _patterns(result, "failure_family+strategy_mode"))
    _print_patterns("Top symbol failure patterns", _patterns(result, "failure_family+symbol"))
    _print_patterns("Top timeframe failure patterns", _patterns(result, "failure_family+timeframe"))

    if result.warnings or result.errors:
        _safe_print()
        _safe_print("Warning/Error Summary")
        for message in result.errors[:MAX_HUMAN_MESSAGES]:
            _safe_print(f"- ERROR {message}")
        for message in result.warnings[:MAX_HUMAN_MESSAGES]:
            _safe_print(f"- WARNING {message}")
        omitted = len(result.errors) + len(result.warnings) - (MAX_HUMAN_MESSAGES * 2)
        if omitted > 0:
            _safe_print(f"- INFO message_output_truncated: {omitted} additional messages omitted; use --json.")

    _safe_print()
    _safe_print(f"Safety: {result.safety_note}")


def print_json_report(result: ReplayFailureTaxonomyResult) -> None:
    print(json.dumps(failure_taxonomy_result_to_dict(result), indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute read-only replay failure taxonomy reporting.",
    )
    parser.add_argument("--input", dest="inputs", action="append", type=Path, help="Replay artifact or JSONL dataset path.")
    parser.add_argument(
        "--input-format",
        choices=("artifacts", "jsonl"),
        default="artifacts",
        help="Treat input paths as Phase 44C artifact sources or JSONL replay dataset files.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable failure taxonomy report.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings or errors are found.")
    parser.add_argument("--top-n", type=int, default=10, help="Number of top buckets or patterns to include.")
    args = parser.parse_args(argv)

    paths = list(args.inputs) if args.inputs else list(default_artifact_paths())
    if args.input_format == "jsonl":
        result = analyze_replay_failure_taxonomy_from_files(paths, top_n=args.top_n)
    else:
        export_result = export_replay_dataset_from_files(paths)
        result = analyze_replay_failure_taxonomy(
            list(export_result.rows),
            source="default_artifacts" if not args.inputs else "artifacts",
            top_n=args.top_n,
        )
        result = _with_export_messages(result, export_result.warnings, export_result.errors)

    if args.json:
        print_json_report(result)
    else:
        print_human_report(result)

    if result.error_count or (args.strict and result.warning_count):
        return 1
    return 0


def _with_export_messages(
    result: ReplayFailureTaxonomyResult,
    warnings: Sequence[str],
    errors: Sequence[str],
) -> ReplayFailureTaxonomyResult:
    messages: list[str] = []
    if warnings:
        messages.append(
            f"export_warnings_present: Replay export reported {len(warnings)} warning(s); "
            "failure taxonomy aggregates row failure evidence separately."
        )
    error_messages = tuple(f"export_error: {message}" for message in errors)
    if not messages and not error_messages:
        return result

    combined_warnings = tuple(messages) + result.warnings
    combined_errors = error_messages + result.errors
    summary = replace(
        result.summary,
        warning_count=len(combined_warnings),
        error_count=len(combined_errors),
        is_valid=len(combined_errors) == 0,
    )
    return replace(
        result,
        is_valid=len(combined_errors) == 0,
        warning_count=len(combined_warnings),
        error_count=len(combined_errors),
        summary=summary,
        warnings=combined_warnings,
        errors=combined_errors,
    )


def _dimension_counts(result: ReplayFailureTaxonomyResult, dimension_name: str) -> dict[str, int]:
    dimension = _dimension(result, dimension_name)
    if dimension is None:
        return {}
    return {bucket.key: bucket.count for bucket in dimension.top_buckets}


def _dimension(result: ReplayFailureTaxonomyResult, dimension_name: str) -> ReplayFailureTaxonomyDimension | None:
    for dimension in result.dimensions:
        if dimension.dimension_name == dimension_name:
            return dimension
    return None


def _patterns(result: ReplayFailureTaxonomyResult, pattern_name: str) -> tuple[ReplayFailurePattern, ...]:
    return tuple(pattern for pattern in result.patterns if pattern.pattern_name == pattern_name)[:MAX_HUMAN_ITEMS]


def _print_counter(label: str, counts: dict[str, int]) -> None:
    _safe_print(f"{label}: {_format_counter(counts)}")


def _format_counter(counts: dict[str, int]) -> str:
    if not counts:
        return "N/A"
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_HUMAN_ITEMS]
    return ", ".join(f"{name}={count}" for name, count in items)


def _print_patterns(label: str, patterns: Sequence[ReplayFailurePattern]) -> None:
    if not patterns:
        _safe_print(f"{label}: N/A")
        return
    formatted = ", ".join(f"{pattern.key}={pattern.count}" for pattern in patterns[:MAX_HUMAN_ITEMS])
    _safe_print(f"{label}: {formatted}")


def _safe_print(value: object = "") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


if __name__ == "__main__":
    raise SystemExit(main())
