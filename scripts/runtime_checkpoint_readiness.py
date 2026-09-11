"""One-shot Runtime checkpoint preflight collector and evidence assessment CLI.

Requires an explicit existing source path and an explicit new output path.
This command never creates, migrates, or writes a database, never defaults to a
live Runtime path, and never labels a collector checkout as the deployed release.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.storage.database import StorageError
from app.storage.runtime_checkpoint_readiness import (
    BUSY_TIMEOUT_MS,
    QUERY_DEADLINE_MS,
    RECENT_RUN_LIMIT,
    ReadinessError,
    assess_runtime_checkpoint_evidence,
    collect_runtime_checkpoint_preflight,
    load_json_object,
    write_report_exclusive,
)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    try:
        if args.command == "collect":
            report = collect_runtime_checkpoint_preflight(
                args.source_path,
                filesystem_only=args.filesystem_only,
                include_recent_runs=args.include_recent_runs,
                busy_timeout_ms=args.busy_timeout_ms,
                query_deadline_ms=args.query_deadline_ms,
                recent_run_limit=args.recent_run_limit,
            )
            written = write_report_exclusive(report, args.output_path)
            print(f"wrote {written}")
            return 0
        if args.command == "assess":
            packet = load_json_object(args.evidence_path)
            assessment = assess_runtime_checkpoint_evidence(packet)
            written = write_report_exclusive(assessment, args.output_path)
            print(f"wrote {written}")
            print(f"disposition={assessment['overall_disposition']}")
            print(f"operational_status={assessment['operational_status']}")
            print("go_for_runtime_deployment=false")
            return 0 if assessment["overall_disposition"] != "ADVERSE_MEASURED_RESULT" else 2
    except (ReadinessError, StorageError, OSError) as exc:
        print(f"Runtime checkpoint readiness failed safely: {exc}", file=sys.stderr)
        return 2
    raise AssertionError(f"Unhandled command: {args.command}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Bounded read-only Runtime checkpoint preflight. No default database path. "
            "Does not deploy, migrate, or authorize cutover."
        )
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    collect = subparsers.add_parser(
        "collect",
        help="Observe an explicit existing file (filesystem and optional bounded SQLite metadata).",
    )
    collect.add_argument("--source-path", type=Path, required=True)
    collect.add_argument("--output-path", type=Path, required=True)
    collect.add_argument(
        "--filesystem-only",
        action="store_true",
        help="Report main/WAL/SHM sizes and volume facts without opening SQLite.",
    )
    collect.add_argument(
        "--include-recent-runs",
        action="store_true",
        help="Optional indexed scan_runs sample (fixed LIMIT, proven index, deadline).",
    )
    collect.add_argument("--busy-timeout-ms", type=int, default=BUSY_TIMEOUT_MS)
    collect.add_argument("--query-deadline-ms", type=int, default=QUERY_DEADLINE_MS)
    collect.add_argument("--recent-run-limit", type=int, default=RECENT_RUN_LIMIT)

    assess = subparsers.add_parser(
        "assess",
        help="Assess a supplied evidence packet. Never manufactures GO_FOR_RUNTIME_DEPLOYMENT.",
    )
    assess.add_argument("--evidence-path", type=Path, required=True)
    assess.add_argument("--output-path", type=Path, required=True)
    return parser.parse_args(argv)


if __name__ == "__main__":
    raise SystemExit(main())
