from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analytics.replay_dataset_export import export_replay_dataset_from_files  # noqa: E402
from app.analytics.replay_dataset_quality import (  # noqa: E402
    ReplayDatasetQualityResult,
    analyze_replay_dataset_files,
    analyze_replay_export_result,
    quality_result_to_dict,
)

MAX_HUMAN_ITEMS = 8
MAX_HUMAN_ISSUES = 40


def default_artifact_paths(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    candidates = (
        project_root / "scan_output.json",
        project_root / "scan_runs" / "latest_scan.json",
        project_root / "scan_runs" / "watch_state.json",
        project_root / "scan_runs" / "performance_memory.json",
    )
    return tuple(path for path in candidates if path.exists())


def print_human_report(result: ReplayDatasetQualityResult) -> None:
    summary = result.summary
    _safe_print("Replay Dataset Quality Metrics")
    _safe_print(f"Source: {result.source}")
    _safe_print(f"Rows: {summary.total_rows}")
    _safe_print(f"Quality score: {summary.quality_score:.2f}/100")
    _safe_print(f"Replay ready rate: {summary.replay_ready_rate:.2%}")
    _safe_print(f"Replay ready: {summary.replay_ready_rows}")
    _safe_print(f"Replay not ready: {summary.replay_not_ready_rows}")
    _safe_print(f"Symbols: {summary.symbol_count}")
    _safe_print(
        "Row presence: "
        f"no_setup={summary.no_setup_rows}, "
        f"trade_idea={summary.trade_idea_rows}, "
        f"alert={summary.alert_rows}, "
        f"journal={summary.journal_entry_rows}"
    )
    _safe_print(f"Issues: {result.warning_count} warning, {result.error_count} error")
    _safe_print()

    _print_counter("Top statuses", summary.status_counts)
    _print_counter("Top lifecycle statuses", summary.normalized_lifecycle_status_counts)
    _print_counter("Top readiness warnings", summary.readiness_warning_counts)
    _print_missing_fields(summary.field_quality)

    if result.issues:
        _safe_print()
        _safe_print("Warning/Error Summary")
        for issue in result.issues[:MAX_HUMAN_ISSUES]:
            _safe_print(f"- {issue.severity.upper()} {issue.code} ({issue.path}): {issue.message}")
        omitted = len(result.issues) - MAX_HUMAN_ISSUES
        if omitted > 0:
            _safe_print(f"- INFO issue_output_truncated (root): {omitted} additional issues omitted; use --json.")

    _safe_print()
    _safe_print(f"Safety: {result.safety_note}")


def print_json_report(result: ReplayDatasetQualityResult) -> None:
    print(json.dumps(quality_result_to_dict(result), indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compute read-only replay dataset quality metrics.",
    )
    parser.add_argument("--input", dest="inputs", action="append", type=Path, help="Replay artifact or JSONL dataset path.")
    parser.add_argument(
        "--input-format",
        choices=("artifacts", "jsonl"),
        default="artifacts",
        help="Treat input paths as Phase 44C artifact sources or JSONL replay dataset files.",
    )
    parser.add_argument("--json", action="store_true", help="Print machine-readable quality metrics.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings or errors are found.")
    args = parser.parse_args(argv)

    paths = list(args.inputs) if args.inputs else list(default_artifact_paths())
    if args.input_format == "jsonl":
        result = analyze_replay_dataset_files(paths)
    else:
        export_result = export_replay_dataset_from_files(paths)
        result = analyze_replay_export_result(export_result, source="default_artifacts" if not args.inputs else "artifacts")

    if args.json:
        print_json_report(result)
    else:
        print_human_report(result)

    if result.error_count or (args.strict and result.warning_count):
        return 1
    return 0


def _print_counter(label: str, counts: dict[str, int]) -> None:
    _safe_print(f"{label}: {_format_counter(counts)}")


def _format_counter(counts: dict[str, int]) -> str:
    if not counts:
        return "N/A"
    items = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:MAX_HUMAN_ITEMS]
    return ", ".join(f"{name}={count}" for name, count in items)


def _print_missing_fields(field_quality) -> None:
    missing = sorted(
        (quality for quality in field_quality if quality.missing_count > 0),
        key=lambda quality: (-quality.missing_count, quality.field_name),
    )[:MAX_HUMAN_ITEMS]
    if not missing:
        _safe_print("Top missing fields: N/A")
        return
    formatted = ", ".join(
        f"{quality.field_name}={quality.missing_count} ({quality.completeness_rate:.0%} complete)" for quality in missing
    )
    _safe_print(f"Top missing fields: {formatted}")


def _safe_print(value: object = "") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


if __name__ == "__main__":
    raise SystemExit(main())
