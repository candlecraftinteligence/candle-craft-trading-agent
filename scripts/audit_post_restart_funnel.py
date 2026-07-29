"""Create a read-only scanner-funnel audit from an existing SQLite database."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.analytics.post_restart_funnel_audit import (
    DEFAULT_MAX_DETAIL_RECORDS,
    DEFAULT_MAX_ROWS_PER_SOURCE,
    DEFAULT_MINIMUM_MEANINGFUL_WINDOW_SECONDS,
    FunnelAuditError,
    SOURCE_MODE_QUIESCENT_IMMUTABLE,
    build_post_restart_funnel_report,
    write_post_restart_funnel_reports,
)


def _decimal_fraction(value: str) -> Decimal:
    try:
        parsed = Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError("must be a decimal fraction between zero and one") from exc
    if parsed <= 0 or parsed > 1:
        raise argparse.ArgumentTypeError("must be greater than zero and no more than one")
    return parsed


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer greater than zero") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read a verified quiescent SQLite source in immutable mode=ro/query_only mode and write a post-restart scanner funnel audit. "
            "This command never migrates, copies, repairs, checkpoints, vacuums, or writes to the supplied database."
        )
    )
    parser.add_argument("--database-path", required=True, type=Path)
    parser.add_argument(
        "--source-mode",
        required=True,
        choices=(SOURCE_MODE_QUIESCENT_IMMUTABLE,),
        help="Requires a stopped scanner and no SQLite -wal/-shm sidecars; active-writer audits are refused.",
    )
    parser.add_argument("--window-start-utc", required=True)
    parser.add_argument(
        "--window-end-utc",
        default=None,
        help="ISO-8601 UTC end time. Defaults to the exact current UTC time, which is recorded in the report.",
    )
    parser.add_argument("--expected-watch-interval-sec", required=True, type=_positive_int)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--report-label", required=True)
    parser.add_argument(
        "--minimum-meaningful-window-sec",
        type=_positive_int,
        default=DEFAULT_MINIMUM_MEANINGFUL_WINDOW_SECONDS,
        help="Visible diagnosis threshold; default is 72 hours.",
    )
    parser.add_argument(
        "--dominant-blocker-minimum-share",
        type=_decimal_fraction,
        default=Decimal("0.5"),
        help="Visible exclusive-gate share threshold for DOMINANT_GATE_BLOCKER; default is 0.5.",
    )
    parser.add_argument(
        "--stall-threshold-sec",
        type=_positive_int,
        default=None,
        help="Optional explicit threshold for stalled lifecycle reporting; no stall threshold is invented when omitted.",
    )
    parser.add_argument("--max-rows-per-source", type=_positive_int, default=DEFAULT_MAX_ROWS_PER_SOURCE)
    parser.add_argument("--max-detail-records", type=_positive_int, default=DEFAULT_MAX_DETAIL_RECORDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolved_end = args.window_end_utc or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    try:
        report = build_post_restart_funnel_report(
            args.database_path,
            window_start_utc=args.window_start_utc,
            window_end_utc=resolved_end,
            expected_watch_interval_sec=args.expected_watch_interval_sec,
            report_label=args.report_label,
            source_mode=args.source_mode,
            minimum_meaningful_window_sec=args.minimum_meaningful_window_sec,
            dominant_blocker_minimum_share=args.dominant_blocker_minimum_share,
            stall_threshold_sec=args.stall_threshold_sec,
            max_rows_per_source=args.max_rows_per_source,
            max_detail_records=args.max_detail_records,
        )
        text_path, json_path = write_post_restart_funnel_reports(report, args.output_dir)
    except FunnelAuditError as exc:
        print(f"Post-restart funnel audit failed safely: {exc}", file=sys.stderr)
        return 2
    print(f"Text report: {text_path}")
    print(f"JSON report: {json_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
