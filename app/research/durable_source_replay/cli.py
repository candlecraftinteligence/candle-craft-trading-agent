"""Read-only inspection and offline replay commands."""

from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from app.research.durable_source_replay.constants import EXIT_USAGE
from app.research.durable_source_replay.replay import inspect_capture, replay_capture


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.research.durable_source_replay",
        description="Inspect or replay one durable source-replay capture.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    inspect = sub.add_parser("inspect")
    inspect.add_argument("--evidence", required=True)
    inspect.add_argument("--capture-id", required=True)
    replay = sub.add_parser("replay")
    replay.add_argument("--evidence", required=True)
    replay.add_argument("--capture-id", required=True)
    replay.add_argument("--scratch", required=True)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "inspect":
        report = inspect_capture(evidence_path=args.evidence, capture_id=args.capture_id)
    else:
        report = replay_capture(
            evidence_path=args.evidence,
            capture_id=args.capture_id,
            scratch_dir=args.scratch,
        )
    json.dump(report, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return int(report["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
