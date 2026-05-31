from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Sequence


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analytics.replay_research_report import (  # noqa: E402
    build_replay_research_report_from_artifacts,
    default_replay_research_artifact_paths,
    format_replay_research_report_markdown,
    replay_research_report_to_dict,
)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a read-only consolidated replay research report.",
    )
    parser.add_argument("--input", dest="inputs", action="append", type=Path, help="Replay artifact JSON path.")
    format_group = parser.add_mutually_exclusive_group()
    format_group.add_argument("--json", action="store_true", help="Print a machine-readable report object.")
    format_group.add_argument("--markdown", action="store_true", help="Print a human-readable markdown report.")
    parser.add_argument("--output", type=Path, help="Optional report output path.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write an output file.")
    parser.add_argument("--strict", action="store_true", help="Exit 1 when warnings or errors are found.")
    parser.add_argument("--top-n", type=int, default=10, help="Number of top buckets to include.")
    args = parser.parse_args(argv)

    paths = list(args.inputs) if args.inputs else default_replay_research_artifact_paths(PROJECT_ROOT)
    result = build_replay_research_report_from_artifacts(
        paths,
        source="local_artifacts" if not args.inputs else "artifacts",
        top_n=args.top_n,
    )

    output_format = "json" if args.json else "markdown"
    if output_format == "json":
        content = json.dumps(replay_research_report_to_dict(result), indent=2, sort_keys=True) + "\n"
    else:
        content = format_replay_research_report_markdown(result)

    if args.output is not None and not args.dry_run:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(content, encoding="utf-8")

    _safe_print(content, end="")

    if result.errors or (args.strict and result.warnings):
        return 1
    return 0


def _safe_print(value: object = "", *, end: str = "\n") -> None:
    text = str(value)
    encoding = sys.stdout.encoding or "utf-8"
    print(text.encode(encoding, errors="replace").decode(encoding, errors="replace"), end=end)


if __name__ == "__main__":
    raise SystemExit(main())
