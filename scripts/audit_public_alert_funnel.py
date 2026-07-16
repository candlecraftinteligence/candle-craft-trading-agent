from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analytics.public_alert_funnel import (  # noqa: E402
    build_public_alert_funnel_report,
    format_public_alert_funnel_report,
)


def parse_as_of_timestamp(value: str) -> datetime:
    """Parse an explicit timezone-aware timestamp and normalize it to UTC."""

    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a valid ISO-8601 timestamp with a UTC offset or Z"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise argparse.ArgumentTypeError(
            "must include a UTC offset or Z; timezone-naive timestamps are not allowed"
        )
    return parsed.astimezone(UTC)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Read-only diagnostics for public Telegram alert publication stops.",
    )
    parser.add_argument(
        "--database-path",
        type=Path,
        default=Path("scan_runs") / "main_live_runtime.sqlite",
        help="SQLite runtime database to inspect read-only.",
    )
    parser.add_argument("--hours", type=int, default=24, help="Lookback window in hours.")
    parser.add_argument("--limit", type=int, default=20, help="Maximum rows per detail section.")
    parser.add_argument(
        "--as-of",
        type=parse_as_of_timestamp,
        help="Timezone-aware ISO-8601 report time; defaults to the current UTC time.",
    )
    args = parser.parse_args(argv)

    report = build_public_alert_funnel_report(
        args.database_path,
        hours=args.hours,
        limit=args.limit,
        now=args.as_of,
    )
    print(format_public_alert_funnel_report(report))
    return 0 if report.get("source_available") else 1


if __name__ == "__main__":
    raise SystemExit(main())
