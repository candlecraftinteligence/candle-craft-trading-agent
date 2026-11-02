"""Generate bounded DEV review evidence from committed historical research only.

No credentials, provider calls, Runtime paths, databases or services are read.
The owner-supplied 2026-11-02 staging handoff is research input, not approval.
"""
import hashlib
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0,str(ROOT))

from app.cmc.economics import collection_plan, expected_freshness
from app.cmc.profiles import profile_for


def build_evidence():
    universe_path = ROOT/"runtime_audits/universe_review.json"
    universe_raw = universe_path.read_bytes()
    universe = json.loads(universe_raw)
    proposed_path = ROOT/"config/cmc/identity.runtime-repair.proposed.json"
    proposed_raw = proposed_path.read_bytes()
    proposed = json.loads(proposed_raw)
    bindings = {b["contract_symbol"]:b for b in proposed["bindings"]}
    symbols = universe["symbols"]
    if len(symbols) != 55 or len(set(symbols)) != 55 or any(b["approved"] for b in bindings.values()):
        raise ValueError("historical bounded unapproved research contract differs")
    rows = []
    for symbol in symbols:
        binding = bindings.get(symbol)
        reasons = ["no_current_owner_approved_mapping", "refresh_exact_sources_before_review"]
        if symbol in {"1000LUNCUSDT","1000PEPEUSDT","1000SHIBUSDT"}:
            reasons.append("independent_multiplier_qualification_required; no prefix stripping")
        if symbol == "ARBUSDT": reasons.append("owner_staging_reports_market_1238049_exclusions_volume; remains_withheld")
        if symbol == "币安人生USDT": reasons.append("native_Unicode_identity_retained; symbol_filter_400; ranked_map_research_only")
        rows.append({"symbol":symbol,"binding_state":"UNAPPROVED","approved":False,
            "owner_approval_ref":None,"approved_at":None,
            "historical_candidate":{k:binding[k] for k in ("cmc_id","cmc_slug","exchange_id","market_id","acquired_at","expires_at","evidence_ref")}
                if binding else "N/A: latest expanded 55-row capture not supplied on DEV",
            "source_evidence":[{"file":"runtime_audits/universe_review.json",
                "sha256":hashlib.sha256(universe_raw).hexdigest(),
                "acquired_at":universe["diagnostics"]["acquisition_timestamp"]},
                {"source":"owner-provided Runtime staging handoff, 2026-11-02; research only",
                 "claim":"55 provisional candidates; zero approved; semantics unverified",
                 "raw_capture_on_dev":"N/A"}],
            "withholding_reasons":reasons,
            "required_approval":"Fresh exact ID/slug/native perpetual/exchange/market/multiplier binding, expiry, owner approval reference",
            "separate_semantic_verification":{"oi_counting_convention":"Unverified","oi_units":"Unverified",
                "funding_interval":"Unverified","funding_semantics":"Unverified","index_currency":"Unverified"}})
    profile = profile_for("CUSTOM",custom_path=ROOT/"config/cmc/combined-shadow.collection.json")
    return {"label":"DEV research review inventory; no activation or identity approval",
        "historical_roster_count":55,"latest_roster_equality":"Unverified: expanded Runtime rows not supplied",
        "latest_staging_reported_candidates":55,"currently_approved_mappings":0,
        "historical_manifest_sha256":hashlib.sha256(proposed_raw).hexdigest(),"identities":rows,
        "collection_economics":collection_plan(profile),
        "contract_freshness":expected_freshness(),
        "global_freshness_zero_lag":expected_freshness(cadence_seconds=1800),
        "global_freshness_hypothetical_60s_lag":expected_freshness(cadence_seconds=1800,source_max_age_seconds=240),
        "runtime_resource_readiness":"BLOCKED: reported free physical RAM below historical role peaks plus floor; CMC incremental Runtime RSS and commit headroom unmeasured",
        "staging_usage_reconciliation":{"account_entitlement":15000,"account_used_before_validation":4,
            "validation_ledger_reported_credits":12,"validation_requests":18,"account_used_after_validation":16,
            "remaining_at_observation":14984,"remaining_now":"N/A",
            "effective_daily":388,"effective_monthly":11988,
            "explanation":"12 validation credits consume the account once; reduced application allowances reserve headroom, not a second account debit. Existing October Runtime reservations remain retained in the same sidecar ledger."}}


if __name__ == "__main__":
    print(json.dumps(build_evidence(),indent=2,ensure_ascii=True))
