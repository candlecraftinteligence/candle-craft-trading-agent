"""Offline release evidence generator. No credentials, provider calls or live services."""
from __future__ import annotations

import asyncio
import hashlib
import json
import sys
import time
import tracemalloc
from pathlib import Path
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

from app.cmc.authenticated_validation import analyze_capture, as_fetch, load_sources
from app.cmc.endpoints import Endpoint
from app.cmc.features import build_features
from app.cmc.models import Mode, encoded, stable_id, utc
from app.cmc.normalize import normalize_context
from app.cmc.policy import RULES, evaluate, rank
from tests import cmc_decision_fixture as canonical
from tests.fixtures.cmc import AT, approval, candidate, feature, obs


def policy_case(name, features, at, *, synthetic_approval=None):
    candidates = (candidate(symbol="AAA", economic_plan_id=name+":long", canonical_rank=(-88,)),
        candidate(symbol="ZZZ", direction="short", economic_plan_id=name+":short", canonical_rank=(-88,)),
        candidate(symbol="BETTER", economic_plan_id=name+":better", canonical_rank=(-89,)),
        candidate(symbol="REJECTED", economic_plan_id=name+":rejected", cci_eligible=False, canonical_rank=(-100,)))
    baseline = [c.economic_plan_id for c in sorted(candidates, key=lambda c:(c.canonical_rank,c.symbol)) if c.cci_eligible][:2]
    modes = {"BASELINE_CCI": {"selected_plan_ids":baseline}}
    for mode in Mode:
        evaluations = tuple(evaluate(c, features, mode=mode, as_of=at,
            activations=(synthetic_approval,) if synthetic_approval else ()) for c in candidates)
        selected = [e.candidate.economic_plan_id for e in rank(evaluations)][:2]
        assert len(selected) <= 2 and not any(p.endswith(":rejected") for p in selected)
        assert all(e.final_eligible == e.candidate.cci_eligible or e.vetoed for e in evaluations)
        if not synthetic_approval or mode != Mode.DECISION:
            assert selected == baseline
        modes["CMC_"+mode.value.upper()] = {"selected_plan_ids":selected,
            "diff_from_baseline":{"displaced":sorted(set(baseline)-set(selected)),
                                  "introduced":sorted(set(selected)-set(baseline))},
            "evaluations":[e.model_dump(mode="json") for e in evaluations]}
    return {"name":name, "synthetic_approval_for_mechanics_only":synthetic_approval is not None, "modes":modes}


async def generate():
    artifact = json.loads((ROOT/"docs/research/cmc_hybrid_main_fixed_decisions.json").read_text())
    original_config = canonical._config
    canonical_modes = {"BASELINE_CCI":{"artifact":"docs/research/cmc_hybrid_main_fixed_decisions.json",
        "protected_output_sha256":stable_id(artifact)}}
    for mode in Mode:
        def config(*args, **kwargs):
            return original_config(*args, **kwargs).model_copy(update={"cmc_mode":mode.value})
        with patch.object(canonical, "_config", config), patch("app.cmc.runtime.begin_collection", new=AsyncMock(return_value=None)):
            result = await canonical.fixed_decisions()
        result = json.loads(json.dumps(result, default=str))
        result["config"].pop("cmc_mode", None)
        assert result == artifact
        canonical_modes["CMC_"+mode.value.upper()] = {"matches_main_baseline":True,
            "protected_output_sha256":stable_id(result)}
    fixture = ROOT/"tests/fixtures/cmc_authenticated"
    report = analyze_capture(fixture, fixture/"bindings.reviewed.json")
    _, sources = load_sources(fixture)
    context = []
    for endpoint in (Endpoint.GLOBAL,Endpoint.LIQUIDATIONS):
        r,p = max(((r,p) for r,p in sources if r["endpoint"]==endpoint.value), key=lambda item:item[0]["acquired_at"])
        context.extend(normalize_context(as_fetch(r,p)))
    actual_at = utc(report["validation_cutoff"])
    actual = build_features(tuple(context), as_of=actual_at)
    adverse = (feature("CmcGlobalMarketRisk",4),feature("CmcLiquidationPressure",3))
    veto = (feature("CmcLiquidationPressure",6),feature("CmcLiquidationImbalance","0.8"))
    cases = [policy_case("real_recorded_context_no_activation",actual,actual_at),
        policy_case("synthetic_priority_disabled",adverse,AT),
        policy_case("synthetic_priority_authorized",adverse,AT,synthetic_approval=approval("adverse_regime")),
        policy_case("synthetic_veto_authorized",veto,AT,synthetic_approval=approval("liquidation_stress")),
        policy_case("missing_provider",(),AT),policy_case("expired_evidence",adverse,AT.replace(day=2),synthetic_approval=approval("adverse_regime"))]
    observations = (*tuple(context), *(obs("unused",str(i)).model_copy(update={
        "source_at":actual_at,"acquired_at":actual_at}) for i in range(1500)))
    # Benchmark only local pure feature/policy CPU and Python allocations. This is
    # not a Runtime-host RSS or latency certification and makes no market calls.
    tracemalloc.start()
    started = time.perf_counter()
    for i in range(100):
        values = build_features(observations, as_of=actual_at)
        evaluate(candidate(symbol=str(i)),values,mode=Mode.SHADOW,as_of=actual_at)
    elapsed = time.perf_counter()-started
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    started = time.perf_counter()
    for i in range(100):
        evaluate(candidate(symbol=str(i)),build_features(observations,as_of=actual_at),mode=Mode.SHADOW,as_of=actual_at)
    untraced = time.perf_counter()-started
    return {"schema_version":"cmc-release-regression/1", "network_calls":0,"credentials_read":False,
        "live_scanner_started":False,"production_rules_activated":False,
        "main_baseline_artifact_sha256":hashlib.sha256((ROOT/"docs/research/cmc_hybrid_main_fixed_decisions.json").read_bytes()).hexdigest(),
        "canonical_modes":canonical_modes,"policy_cases":cases,
        "real_context_cutoff":report["validation_cutoff"],"real_context_evidence_ids":[o.id for o in context],
        "local_benchmark":{"candidates":100,"snapshot_observations":len(observations),
            "seconds_with_tracemalloc":elapsed,"peak_python_incremental_bytes":peak,
            "seconds_without_tracemalloc":untraced,
            "runtime_host_memory_and_latency":"N/A: must be measured on the 4 GB Runtime host"},
        "rules":[r.model_dump(mode="json")|{"specification_hash":r.specification_hash} for r in RULES],
        "economic_improvement_proven":False}


if __name__ == "__main__":
    output = Path(sys.argv[1])
    result = asyncio.run(generate())
    output.write_text(encoded(result)+"\n",encoding="utf-8")
    print(json.dumps({"output":str(output),"canonical_modes_equal":len(result["canonical_modes"]),
        "policy_cases":len(result["policy_cases"]),"benchmark":result["local_benchmark"]}))
