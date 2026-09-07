"""Read-only CCI evidence baseline audit for an explicit development/test database."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.analytics.evidence_baseline_audit import (
    EvidenceAuditError,
    build_evidence_baseline,
    dumps_evidence_payload,
)
from app.analytics.evidence_time import EvidenceTimestampError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Read an explicit SQLite database in URI mode=ro with PRAGMA query_only and emit a "
            "deterministic evidence baseline for [start, cutoff). This command never creates, "
            "migrates, repairs, or writes the supplied database. It refuses the live runtime path "
            "and the basename main_live_runtime.sqlite. There is no default database path."
        )
    )
    parser.add_argument("--database-path", required=True, type=Path)
    parser.add_argument(
        "--start",
        required=True,
        help="Inclusive timezone-aware ISO-8601 timestamp. Naive values are rejected.",
    )
    parser.add_argument(
        "--cutoff",
        required=True,
        help="Exclusive timezone-aware ISO-8601 timestamp. Naive values are rejected.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = build_evidence_baseline(
            args.database_path,
            start=args.start,
            cutoff=args.cutoff,
        )
    except (EvidenceAuditError, EvidenceTimestampError) as exc:
        print(f"Evidence baseline audit failed safely: {exc}", file=sys.stderr)
        return 2
    print(dumps_evidence_payload(payload))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
