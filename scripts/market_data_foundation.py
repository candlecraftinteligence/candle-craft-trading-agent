"""Finite CLI for the market-data foundation research phase."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

import httpx

from research.market_data_foundation.budget import StudyBudget
from research.market_data_foundation.cadence import (
    cadence_contract,
    freshness_microbenchmark,
    project_acquisition,
    usage_projection,
)
from research.market_data_foundation.constants import LOWERED_REASON
from research.market_data_foundation.integrity import canonical_findings
from research.market_data_foundation.online import run_public_sample
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
    args = parser.parse_args(argv)
    flags = enforce_research_process()
    root = resolve_research_root(args.research_root or default_research_root())
    root.mkdir(parents=True, exist_ok=True)
    online = _load_online(args.online_summary)
    if args.mode == "sample":
        budget = StudyBudget()
        with httpx.Client(timeout=budget.timeout_seconds, headers={"User-Agent": "cci-market-data-foundation/dev"}) as client:
            online = run_public_sample(root=root, client=client, budget=budget, transport_label="live")
        _emit(root, "sample", {"process_flags": flags, "lowered_reason": LOWERED_REASON, "online": online})
    elif args.mode == "benchmark":
        bench = freshness_microbenchmark()
        latencies = [
            call["latency_ms"] / 1000
            for call in (online or {}).get("calls", [])
            if isinstance(call.get("latency_ms"), int)
        ]
        _emit(
            root,
            "benchmark",
            {
                "process_flags": flags,
                "cadence": cadence_contract(),
                "microbenchmark": bench,
                "projection": project_acquisition(
                    measured_latency_seconds=latencies,
                    requests_per_cycle=len(latencies),
                ),
                "usage": usage_projection(
                    measured_calls_per_cycle=len(latencies) or None,
                    measured_weight_per_cycle=None,
                ),
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
