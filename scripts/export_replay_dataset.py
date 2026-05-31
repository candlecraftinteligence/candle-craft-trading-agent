from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analytics.replay_dataset_export import (  # noqa: E402
    ReplayDatasetExportResult,
    export_replay_dataset_from_files,
    rows_to_csv,
    rows_to_jsonl,
)

MAX_HUMAN_MESSAGES = 40


def default_artifact_paths(project_root: Path = PROJECT_ROOT) -> tuple[Path, ...]:
    candidates = (
        project_root / "scan_output.json",
        project_root / "scan_runs" / "latest_scan.json",
        project_root / "scan_runs" / "watch_state.json",
        project_root / "scan_runs" / "performance_memory.json",
    )
    return tuple(path for path in candidates if path.exists())


def print_human_summary(
    result: ReplayDatasetExportResult,
    *,
    dry_run: bool,
    output_path: Path | None,
    output_format: str,
) -> None:
    _safe_print("Replay Dataset Export")
    _safe_print(f"Artifacts inspected: {result.summary.file_count}")
    _safe_print(f"Rows: {result.summary.row_count}")
    _safe_print(f"Replay ready: {result.summary.replay_ready_count}")
    _safe_print(f"Replay not ready: {result.summary.replay_not_ready_count}")
    _safe_print(f"Issues: {result.summary.warning_count} warning, {result.summary.error_count} error")
    if result.summary.artifact_counts:
        counts = ", ".join(f"{name}={count}" for name, count in result.summary.artifact_counts.items())
        _safe_print(f"Artifact types: {counts}")
    if dry_run:
        _safe_print("Output: dry-run; no replay export file written.")
    elif output_path is not None:
        _safe_print(f"Output: {output_path} ({output_format})")

    for label, messages in (("ERROR", result.errors), ("WARNING", result.warnings)):
        for message in messages[:MAX_HUMAN_MESSAGES]:
            _safe_print(f"- {label}: {message}")
        omitted = len(messages) - MAX_HUMAN_MESSAGES
        if omitted > 0:
            _safe_print(f"- {label}: {omitted} additional messages omitted; use --json-summary for counts.")


def print_json_summary(
    result: ReplayDatasetExportResult,
    *,
    dry_run: bool,
    output_path: Path | None,
    output_format: str,
) -> None:
    payload = {
        "summary": asdict(result.summary),
        "warnings": list(result.warnings),
        "errors": list(result.errors),
        "output": {
            "dry_run": dry_run,
            "path": None if output_path is None else str(output_path),
            "format": output_format,
        },
    }
    print(json.dumps(payload, indent=2, sort_keys=True))


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Export read-only scanner/lifecycle/performance artifacts to replay dataset rows.",
    )
    parser.add_argument("--input", dest="inputs", action="append", type=Path, help="JSON artifact path to export.")
    parser.add_argument("--output", type=Path, help="Replay dataset output path.")
    parser.add_argument("--format", choices=("jsonl", "csv"), default="jsonl", help="Output format.")
    parser.add_argument("--json-summary", action="store_true", help="Print machine-readable summary only.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write an output file.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings or errors are found.")
    args = parser.parse_args(argv)

    paths = list(args.inputs) if args.inputs else list(default_artifact_paths())
    result = export_replay_dataset_from_files(paths)

    effective_dry_run = bool(args.dry_run or args.output is None)
    output_path = args.output
    if output_path is not None and not effective_dry_run and not result.errors:
        content = rows_to_jsonl(list(result.rows)) if args.format == "jsonl" else rows_to_csv(list(result.rows))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(content, encoding="utf-8")

    if args.json_summary:
        print_json_summary(result, dry_run=effective_dry_run, output_path=output_path, output_format=args.format)
    else:
        print_human_summary(result, dry_run=effective_dry_run, output_path=output_path, output_format=args.format)

    if result.errors or (args.strict and result.warnings):
        return 1
    return 0


def _safe_print(value: object = "") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"))


if __name__ == "__main__":
    raise SystemExit(main())
