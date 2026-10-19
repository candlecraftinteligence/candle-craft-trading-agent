"""Finite CLI for the market-data foundation research phase."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import httpx

from research.market_data_foundation.budget import StudyBudget
from research.market_data_foundation.cadence import (
    cadence_contract,
    freshness_microbenchmark,
    usage_projection,
)
from research.market_data_foundation.constants import LOWERED_REASON
from research.market_data_foundation.integrity import canonical_findings
from research.market_data_foundation.online import replay_saved, run_public_sample
from research.market_data_foundation.study import offline_metric_comparison, selected_universe_benchmark
from research.market_data_foundation.paths import default_research_root, resolve_research_root
from research.market_data_foundation.reports import render_package, write_package
from research.market_data_foundation.safety import enforce_research_process
from research.market_data_foundation.shadow import (
    acquisition_skew_example,
    cache_provenance,
    derivatives_sensitivity,
    freshness_sensitivity,
    public_gate_probe,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="CCI market-data foundation research harness")
    parser.add_argument(
        "mode",
        choices=("offline-audit", "replay", "shadow", "benchmark", "sample", "write-docs"),
    )
    parser.add_argument("--research-root", type=Path, default=None)
    parser.add_argument("--docs", action="store_true")
    parser.add_argument("--online-summary", type=Path, default=None)
    parser.add_argument("--decision-cutoff", default=None, help="Explicit UTC cutoff required for replay")
    parser.add_argument("--code-sha", default=None, help="Code provenance recorded on a sample. Defaults to git HEAD.")
    args = parser.parse_args(argv)
    flags = enforce_research_process()
    root = resolve_research_root(args.research_root or default_research_root())
    root.mkdir(parents=True, exist_ok=True)
    online = _load_online(args.online_summary)
    if args.mode == "sample":
        budget = StudyBudget()
        with httpx.Client(timeout=budget.timeout_seconds, headers={"User-Agent": "cci-market-data-foundation/dev"}) as client:
            online = run_public_sample(
                root=root,
                client=client,
                budget=budget,
                transport_label="live",
                code_sha=_code_sha(args.code_sha),
            )
        _emit(root, "sample", {"process_flags": flags, "lowered_reason": LOWERED_REASON, "online": online})
    elif args.mode == "benchmark":
        comparison = offline_metric_comparison()
        selected = tuple(comparison["selected"])
        _emit(
            root,
            "benchmark",
            {
                "process_flags": flags,
                "cadence": cadence_contract(),
                "microbenchmark": freshness_microbenchmark(),
                "selected_universe": selected_universe_benchmark(selected),
                "comparison": comparison,
                "usage": usage_projection(
                    measured_calls_per_cycle=None,
                    measured_weight_per_cycle=None,
                ),
                "live_network": "not_executed",
            },
        )
    elif args.mode == "replay":
        cutoff = _parse_cutoff(args.decision_cutoff)
        _emit(
            root,
            "replay",
            {
                "process_flags": flags,
                "label": "captured_artifact",
                "code_sha": _code_sha(args.code_sha),
                "replay": replay_saved(root, decision_cutoff=cutoff),
            },
        )
    elif args.mode == "write-docs":
        pass
    else:
        _emit(
            root,
            args.mode,
            {
                "process_flags": flags,
                "findings": [item.as_dict() for item in canonical_findings()],
                "public_gate": public_gate_probe(),
                "freshness": freshness_sensitivity(),
                "derivatives": derivatives_sensitivity(),
                "cache": cache_provenance(root),
                "skew": acquisition_skew_example(),
                "cadence": cadence_contract(),
            },
        )
    if args.docs or args.mode == "write-docs":
        write_package(_docs_from_repo(), online=online)
    return 0


def _code_sha(explicit: str | None) -> str:
    if explicit and explicit.strip():
        return explicit.strip()
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=REPOSITORY_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _parse_cutoff(value: str | None) -> datetime:
    if value is None or not str(value).strip():
        raise SystemExit("replay requires --decision-cutoff as an aware UTC timestamp")
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit("replay decision cutoff must be an aware ISO-8601 timestamp") from exc
    if moment.tzinfo is None:
        raise SystemExit("replay decision cutoff must include a timezone")
    return moment


def _docs_from_repo() -> Path:
    from research.market_data_foundation.paths import repository_root

    return repository_root() / "docs" / "research" / "market_data_foundation"


def _load_online(path: Path | None) -> dict | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise SystemExit("online summary must be a JSON object")
    return payload


def _emit(root: Path, mode: str, payload: dict) -> None:
    from research.market_data_foundation.paths import resolve_output

    path = resolve_output(root, f"{mode}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True, default=str), encoding="utf-8")
    preview = render_package()
    print(f"wrote {path}")
    print(f"package sections {len(preview)}")
    print(f"mode {mode}")


if __name__ == "__main__":
    raise SystemExit(main())
