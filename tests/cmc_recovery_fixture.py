"""Reproducible offline recovery evidence. Never opens Runtime or credentials."""
import asyncio
import json
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
sys.path.insert(0,str(ROOT/"tests"))

from app.cmc import integration
from app.cmc.authenticated_validation import analyze_capture, as_fetch, load_sources
from app.cmc.endpoints import Endpoint
from app.cmc.features import build_features
from app.cmc.forecast import comparisons
from app.cmc.identity_review import propose_identities
from app.cmc.leverage import selection_leverage
from app.cmc.models import Mode, Snapshot, utc
from app.cmc.normalize import normalize_context
from app.cmc.policy import evaluate
from app.cmc.storage import Sidecar
from tests.fixtures.cmc import candidate, obs
from test_cmc_integration import setup_symbol
from test_telegram_lifecycle_delivery_phase42 import _run_result


async def generate():
    root = ROOT/"tests/fixtures/cmc_authenticated"
    _, sources = load_sources(root)
    review = analyze_capture(root,root/"bindings.reviewed.json")
    at = utc(review["validation_cutoff"])
    manifest, identity_evidence = propose_identities(root,as_of=utc("2026-10-27T00:00:00Z"))
    values = []
    for endpoint in (Endpoint.GLOBAL,Endpoint.LIQUIDATIONS):
        record, payload = max(((r,p) for r,p in sources if r["endpoint"] == endpoint.value),
            key=lambda item:item[0]["acquired_at"])
        values.extend(normalize_context(as_fetch(record,payload)))
    features = build_features(tuple(values),as_of=at)
    real = evaluate(candidate(original_decision_at=at),features,mode=Mode.SHADOW,as_of=at)
    observations = (*values, *(obs("unused",i).model_copy(update={"source_at":at,"acquired_at":at}) for i in range(2400)))
    symbol = setup_symbol()
    result = _run_result(symbol)
    result = result.model_copy(update={"config":result.config.model_copy(update={"decision_timestamp":at}),
        "results":tuple(symbol.model_copy(update={"symbol":f"SYNTHETIC{i}"}) for i in range(100))})
    class FixedNow(datetime):
        @classmethod
        def now(cls,tz=None): return at
    samples = []
    with tempfile.TemporaryDirectory(prefix="cci-cmc-recovery-") as directory:
        service = SimpleNamespace(store=Sidecar(Path(directory)/"cmc_benchmark.sqlite"),
            snapshot=Snapshot(observations=observations,batch_id="benchmark:recorded-context-and-synthetic-padding"))
        with patch.object(integration,"datetime",FixedNow):
            for traced in (False,True):
                if traced: tracemalloc.start()
                started = time.perf_counter()
                output = await integration.evaluate_delivery_run(result,SimpleNamespace(cmc_mode="shadow",cmc_activation_manifest=None),
                    service=service,candidate_builder=lambda s:candidate(symbol=s.symbol,setup_id=s.symbol,
                        economic_plan_id=s.symbol),decision_at=at)
                elapsed = time.perf_counter()-started
                peak = tracemalloc.get_traced_memory()[1] if traced else None
                if traced: tracemalloc.stop()
                assert all(r.cmc_evaluation for r in output.results), "benchmark lost policy persistence"
                samples.append({"tracemalloc":traced,"seconds_including_sidecar_write":elapsed,
                    "peak_python_bytes":peak,"candidates":100,"snapshot_observations":len(observations)})
    evidence = {"schema_version":"cmc-recovery/1","credentials_read":False,"provider_requests":0,
        "runtime_database_opened":False,"live_scanner_started":False,"owner_approvals_created":False,
        "identity_evidence":identity_evidence,"recorded_context_cutoff":at.isoformat(),
        "recorded_context_features":[f.model_dump(mode="json") for f in features],
        "recorded_context_evaluation":real.model_dump(mode="json"),
        "selection_leverage_recorded_candidate_fixture":selection_leverage((real,)),
        "prospective_selection_leverage":"BLOCKED_EXTERNAL: no sanitized Runtime cohort supplied",
        "benchmark":samples,"runtime_host_RSS_and_latency":"BLOCKED_EXTERNAL: separate 4 GB Runtime host",
        "economic_improvement":"N/A: no verified prospective outcome cohort"}
    return manifest,evidence,comparisons()


if __name__ == "__main__":
    manifest,evidence,forecast = asyncio.run(generate())
    (ROOT/"config/cmc/identity.proposed.json").write_text(manifest.model_dump_json(indent=2)+"\n",encoding="utf-8")
    for name,value in (("cmc_recovery_evidence.json",evidence),("cmc_recovery_budget.json",forecast)):
        (ROOT/"docs/research"/name).write_text(json.dumps(value,indent=2,sort_keys=True)+"\n",encoding="utf-8")
    print(json.dumps({"identity_proposals":len(manifest.bindings),"benchmarks":evidence["benchmark"],
        "forecast_cases":list(forecast["cases"])}))
