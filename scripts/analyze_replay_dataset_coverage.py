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

from app.analytics.replay_dataset_coverage import (  # noqa: E402
    ReplayDatasetCoverageDimension,
    ReplayDatasetCoverageGap,
    ReplayDatasetCoverageResult,
    analyze_replay_dataset_coverage,
    analyze_replay_export_coverage_from_files,
    coverage_result_to_dict,
)
from app.analytics.replay_dataset_export import export_replay_dataset_from_files  # noqa: E402

MAX_HUMAN_ITEMS = 10
MAX_HUMAN_GAPS = 40


def default_artifact_paths(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    candidates = (
        project_root / "scan_output.json",
        project_root / "scan_runs" / "latest_scan.json",
        project_root / "scan_runs" / "watch_state.json",
        project_root / "scan_runs" / "performance_memory.json",
    )
    return tuple(path for path in candidates if path.exists())


def print_human_report(result: ReplayDatasetCoverageResult) -> None:
    summary = result.summary
    _safe_print("Replay Dataset Coverage Report")
    _safe_print(f"Source: {result.source}")
    _safe_print(f"Rows: {summary.total_rows}")
    _safe_print(f"Replay ready rate: {summary.replay_ready_rate:.2%}")
    _safe_print(f"Replay ready: {summary.replay_ready_rows}")
    _safe_print(f"Symbols: {summary.symbol_count}")
    _safe_print(f"Sparse symbols: {summary.sparse_symbol_count}")
    _safe_print(f"Issues: {result.warning_count} warning, {result.error_count} error")
    _safe_print()

    _print_counter("Lifecycle buckets", _dimension_counts(result, "lifecycle_bucket"))
    _print_counter("Setup research buckets", _dimension_counts(result, "setup_research_bucket"))
    _print_counter("Top statuses", _dimension_counts(result, "status"))
    _print_counter("Top strategy modes", _dimension_counts(result, "strategy_mode"))
    _print_counter("Top first_failed_gate", _dimension_counts(result, "first_failed_gate"))
    _print_counter("Top rejection reasons", _dimension_counts(result, "rejection_reason"))
    _safe_print(f"Sparse symbol summary: {_sparse_symbol_summary(result)}")

    if result.gaps:
        _safe_print()
        _safe_print("Warning/Error Summary")
        for gap in result.gaps[:MAX_HUMAN_GAPS]:
            _safe_print(f"- {gap.severity.upper()} {gap.code} ({gap.path}): {gap.message}")
        omitted = len(result.gaps) - MAX_HUMAN_GAPS
        if omitted > 0:
            _safe_print(f"- INFO gap_output_truncated (root): {omitted} additional findings omitted; use --json.")

    _safe_print()
    _safe_print(f"Safety: {result.safety_note}")


def print_json_report(result: ReplayDatasetCoverageResult) -> None:
    print(json.dumps(coverage_result_to_dict(result), indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute read-only replay dataset coverage reporting.",
    )
    parser.add_argument("--input", dest="inputs", action="append", type=Path, help="Replay artifact or JSONL dataset path.")
    parser.add_argument(
        "--input-format",
        choices=("artifacts", "jsonl"),
        default="artifacts",
        help="Treat input paths as Phase 44C artifact sources or JSONL replay dataset files.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable coverage report.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings or errors are found.")
    parser.add_argument("--top-n", type=int, default=10, help="Number of top buckets to include per dimension.")
    parser.add_argument(
        "--min-bucket-count",
        type=int,
        default=2,
        help="Bucket count below which a bucket is considered sparse.",
    )
    args = parser.parse_args(argv)

    paths = list(args.inputs) if args.inputs else list(default_artifact_paths())
    if args.input_format == "jsonl":
        result = analyze_replay_export_coverage_from_files(
            paths,
            top_n=args.top_n,
            min_bucket_count=args.min_bucket_count,
        )
    else:
        export_result = export_replay_dataset_from_files(paths)
        result = analyze_replay_dataset_coverage(
            list(export_result.rows),
            source="default_artifacts" if not args.inputs else "artifacts",
            top_n=args.top_n,
            min_bucket_count=args.min_bucket_count,
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
    result: ReplayDatasetCoverageResult,
    warnings: Sequence[str],
    errors: Sequence[str],
) -> ReplayDatasetCoverageResult:
    gaps: list[ReplayDatasetCoverageGap] = []
    if warnings:
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="warning",
                code="export_warnings_present",
                message=f"Replay export reported {len(warnings)} warning(s); coverage report aggregates row coverage separately.",
                path="export_result.warnings",
            )
        )
    for index, message in enumerate(errors):
        gaps.append(
            ReplayDatasetCoverageGap(
                severity="error",
                code="export_error",
                message=str(message),
                path=f"export_result.errors[{index}]",
            )
        )
    if not gaps:
        return result

    combined = tuple(gaps) + result.gaps
    warning_count = sum(1 for gap in combined if gap.severity == "warning")
    error_count = sum(1 for gap in combined if gap.severity == "error")
    summary = replace(result.summary, warning_count=warning_count, error_count=error_count, is_valid=error_count == 0)
    return replace(
        result,
        is_valid=error_count == 0,
        warning_count=warning_count,
        error_count=error_count,
        summary=summary,
        gaps=combined,
    )


def _dimension_counts(result: ReplayDatasetCoverageResult, dimension_name: str) -> dict[str, int]:
    dimension = _dimension(result, dimension_name)
    if dimension is None:
        return {}
    return {bucket.key: bucket.count for bucket in dimension.top_buckets}


def _dimension(result: ReplayDatasetCoverageResult, dimension_name: str) -> ReplayDatasetCoverageDimension | None:
    for dimension in result.dimensions:
        if dimension.dimension_name == dimension_name:
            return dimension
    return None


def _print_counter(label: str, counts: dict[str, int]) -> None:
    _safe_print(f"{label}: {_format_counter(counts)}")


def _format_counter(counts: dict[str, int]) -> str:
    if not counts:
        return "N/A"
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_HUMAN_ITEMS]
    return ", ".join(f"{name}={count}" for name, count in items)


def _sparse_symbol_summary(result: ReplayDatasetCoverageResult) -> str:
    dimension = _dimension(result, "symbol")
    if dimension is None or not dimension.sparse_buckets:
        return "N/A"
    examples = ", ".join(f"{bucket.key}={bucket.count}" for bucket in dimension.sparse_buckets[:MAX_HUMAN_ITEMS])
    return f"{dimension.sparse_bucket_count} sparse bucket(s): {examples}"


def _safe_print(value: object = "") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


if __name__ == "__main__":
    raise SystemExit(main())
