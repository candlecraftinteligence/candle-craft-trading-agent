"""Offline research / explicit bounded CMC-only probes. Never starts the scanner."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import SecretStr, TypeAdapter

from app.cmc.client import CmcClient
from app.cmc.collection import Collector, load_manifest
from app.cmc.endpoints import Endpoint
from app.cmc.models import EvidenceState, Feature, Mode
from app.cmc.normalize import normalize_context
from app.cmc.policy import Activation, Candidate, RULES, evaluate
from app.cmc.research import Outcome, ResearchRow, compare_policies
from app.cmc.storage import Sidecar


def demo():
    """Deliberately includes a vetoed winner and a vetoed loser; not market data."""
    at = datetime(2026, 1, 1, tzinfo=UTC)
    rule = next(r for r in RULES if r.id == "data_divergence")
    activation = Activation(rule_id=rule.id, version=rule.version, specification_hash=rule.specification_hash,
        enabled=True, source_contract_ref="synthetic:test", causal_evaluation_ref="synthetic:test",
        out_of_sample_or_prospective_ref="synthetic:test", tests_ref="synthetic:test", hard_gate_compatibility_ref="synthetic:test",
        baseline_comparison_ref="synthetic:test", no_material_negative_regression=True, owner_approval_ref="synthetic:unit-test-only",
        approved_at=at, expires_at=datetime(2026,1,2,tzinfo=UTC))
    rows = []
    for index, value in enumerate((Decimal("-1"), Decimal("2"), Decimal("0.5"))):
        candidate = Candidate(setup_id=f"synthetic-setup-{index}", economic_plan_id=f"synthetic-plan-{index}",
            symbol=f"SYNTHETIC{index}", direction="long", cci_eligible=True)
        feature = Feature(name="CmcDataConsistency", value=-1 if index<2 else 1, units="classification",
            state=EvidenceState.VERIFIED, evidence_ids=(f"synthetic-evidence-{index}",),
            input_timestamps=(at,), acquired_timestamps=(at,), reason="synthetic fixture")
        evaluation = evaluate(candidate,(feature,),mode=Mode.DECISION,as_of=at,activations=(activation,))
        rows.append(ResearchRow(selection_group=f"synthetic-scan-{index}",evaluation=evaluation,
            outcome=Outcome(economic_plan_id=candidate.economic_plan_id,verified=True,attribution_ref="synthetic:test",
                closed_at=datetime(2026,1,2,tzinfo=UTC),entry_activated=True,realized_net_r=value,
                fees_r=Decimal("0.01"),slippage_r=Decimal("0.01")),cohort="synthetic"))
    report = compare_policies(tuple(rows))
    report["synthetic_fixture_only"] = True
    return report


async def probe(args):
    store = Sidecar(args.sidecar)
    key = os.environ.get("CMC_API_KEY") if args.authenticated else None
    if args.authenticated and not key:
        raise ValueError("CMC_API_KEY must be supplied securely by the owner")
    client = CmcClient(store=store,api_key=SecretStr(key) if key else None,keyless=not args.authenticated)
    try:
        if args.collect_once:
            snapshot = await asyncio.wait_for(Collector(client,store,manifest=load_manifest(args.identity_manifest)).collect(),60)
            return {"batch_id":snapshot.batch_id,"observations":len(snapshot.observations),"errors":snapshot.errors,
                    "capabilities":client.capabilities,"usage":store.usage(datetime.now(UTC))}
        outputs = []
        for endpoint,params in ((Endpoint.LISTINGS,{"limit":1}), (Endpoint.MAP,{"limit":1}),
                (Endpoint.GLOBAL,{}), (Endpoint.FEAR_GREED,{}), (Endpoint.ALTCOIN,{}),
                (Endpoint.EXCHANGE_PAIRS,{"exchange_slug":"binance","limit":1}),
                (Endpoint.LIQUIDATIONS,{}), (Endpoint.KEY_INFO,{})):
            result = await client.get(endpoint,params)
            outputs.append({"endpoint":endpoint.value,"state":result.state.value,"error":result.error,
                "authenticated":result.authenticated,"credits_reported":result.credits_reported,
                "acquired_at":result.acquired_at.isoformat(),
                "normalized_states":[o.state.value for o in normalize_context(result)]
                    if endpoint not in {Endpoint.EXCHANGE_PAIRS,Endpoint.KEY_INFO} else [],
                "normalized_metrics":[{"metric":o.metric,"state":o.state.value,"source_at":o.source_at.isoformat() if o.source_at else None}
                    for o in normalize_context(result)] if endpoint not in {Endpoint.EXCHANGE_PAIRS,Endpoint.KEY_INFO} else []})
        return {"probe":outputs,"usage":store.usage(datetime.now(UTC)),
                "authenticated_api_verified":any(x["authenticated"] and not x["error"] for x in outputs)}
    finally:
        await client.aclose()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--demo",action="store_true")
    group.add_argument("--evaluate",type=Path)
    group.add_argument("--probe",action="store_true")
    group.add_argument("--collect-once",action="store_true")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--sidecar",type=Path,default=Path("research_runs/cmc_probe.sqlite"))
    parser.add_argument("--identity-manifest",type=Path)
    parser.add_argument("--authenticated",action="store_true",help="Owner-supplied CMC_API_KEY; otherwise keyless only")
    args = parser.parse_args()
    if args.demo:
        result = demo()
    elif args.evaluate:
        if args.evaluate.stat().st_size > 16*1024*1024:
            raise ValueError("research input budget")
        result = compare_policies(TypeAdapter(tuple[ResearchRow,...]).validate_json(args.evaluate.read_text(encoding="utf-8")))
    else:
        result = asyncio.run(probe(args))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2,sort_keys=True,default=str)+"\n",encoding="utf-8")
    print(f"CMC report written: {args.output}")


if __name__ == "__main__":
    main()
