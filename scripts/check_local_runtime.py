from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.core.local_runtime_checks import (  # noqa: E402
    collect_local_diagnostics,
    diagnostic_summary,
    diagnostics_to_dicts,
    has_hard_blockers,
)


def _format_value(value: Any) -> str:
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, sort_keys=True)
    return str(value)


def _print_details(details: Mapping[str, Any]) -> None:
    for key, value in details.items():
        print(f"    {key}: {_format_value(value)}")


def print_human_report(project_root: Path) -> int:
    diagnostics = collect_local_diagnostics(project_root)
    summary = diagnostic_summary(diagnostics)

    print("Local Runtime Diagnostics")
    print(f"Project: {project_root}")
    print(f"Summary: {summary['ok']} ok, {summary['warning']} warning, {summary['error']} error")
    print()

    for diagnostic in diagnostics:
        print(f"[{diagnostic.status.upper()}] {diagnostic.name}: {diagnostic.message}")
        if diagnostic.details:
            _print_details(diagnostic.details)

    return 1 if has_hard_blockers(diagnostics) else 0


def print_json_report(project_root: Path) -> int:
    diagnostics = collect_local_diagnostics(project_root)
    payload = {
        "project_root": str(project_root),
        "summary": diagnostic_summary(diagnostics),
        "diagnostics": diagnostics_to_dicts(diagnostics),
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 1 if has_hard_blockers(diagnostics) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Check local runtime readiness without calling live trading systems.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable diagnostics.")
    args = parser.parse_args()

    project_root = PROJECT_ROOT.resolve()
    if args.json:
        return print_json_report(project_root)
    return print_human_report(project_root)


if __name__ == "__main__":
    raise SystemExit(main())

