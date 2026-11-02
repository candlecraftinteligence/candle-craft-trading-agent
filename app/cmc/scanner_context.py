"""Persisted SHADOW context for every analyzed candidate, including rejections."""
from __future__ import annotations

from collections import Counter


def scanner_context(snapshot, *, symbol, cutoff, assessed_at, mapping_status=None):
    from app.cmc.models import Snapshot
    snapshot = snapshot or Snapshot()
    values = []
    for observation in snapshot.observations:
        instrument = observation.instrument
        if instrument is not None and (instrument.exchange != "binance" or instrument.symbol != symbol):
            continue
        if instrument is None and observation.scope not in {"global", "global_all_contracts", "exchange_all_contracts"}:
            continue  # Asset observations require an exact approved contract binding.
        if observation.scope == "exchange_all_contracts" and observation.exchange_id != 270:
            continue  # Reviewed Binance exchange ID; another venue is not this contract's context.
        state = observation.status_at(cutoff)
        current = observation.status_at(assessed_at)
        admitted = state.value == current.value == "VERIFIED"
        values.append({"id": observation.id, "metric": observation.metric,
            "value": str(observation.value) if admitted else "N/A", "status": current.value if state.value == "VERIFIED" else state.value,
            "source_at": observation.source_at.isoformat() if observation.source_at else None,
            "acquired_at": observation.acquired_at.isoformat(), "endpoint": observation.endpoint,
            "scope": observation.scope, "units": observation.units, "provenance": observation.provenance,
            "cmc_id": observation.cmc_id, "contract_symbol": instrument.symbol if instrument else None,
            "reason": observation.reason or ("not_visible_at_structural_cutoff" if state.value == "UNAVAILABLE"
                else "source_freshness_expired" if current.value == "STALE" else ""),
            "binding_ref": instrument.binding_ref if instrument else None})
    global_available = any(v["status"] == "VERIFIED" and v["contract_symbol"] is None
        and v["scope"] in {"global", "global_all_contracts"} for v in values)
    contract_available = any(v["status"] == "VERIFIED" and v["contract_symbol"] == symbol
        and v["scope"] == "contract" for v in values)
    mapped = any(v["contract_symbol"] == symbol for v in values)
    errors = snapshot.errors
    if any("credit_budget" in e for e in errors):
        acquisition = "BUDGET_EXHAUSTED"
    elif any("collector_construction_failure" in e for e in errors):
        acquisition = "COLLECTOR_FAILURE"
    elif any(not e.startswith(("identity_manifest_empty:", "restored_identity_not_currently_owner_approved",
                              "map_requested_ids_complete:")) for e in errors):
        acquisition = "ACQUISITION_FAILURE"
    elif snapshot.batch_id == "N/A" and not values:
        acquisition = "PENDING_ACQUISITION"
    else:
        acquisition = "ACQUIRED_WITH_LIMITATIONS" if errors else "ACQUIRED"
    identity = "MAPPED" if mapped else mapping_status or "NO_APPROVED_INSTRUMENT_MAPPING"
    available = "VERIFIED_CONTRACT_CONTEXT" if contract_available else "GLOBAL_ONLY_CONTEXT" if global_available else "STALE_OR_UNAVAILABLE"
    return {"mode": "shadow", "usage": "external_context_only", "batch_id": snapshot.batch_id,
        "status": "VERIFIED_CONTEXT_PRESENT" if any(v["status"] == "VERIFIED" for v in values)
            else "N/A: acquisition_pending_or_no_usable_context",
        "strategy_cutoff": cutoff.isoformat(), "assessed_at": assessed_at.isoformat(),
        "observations": values[:128], "observation_count": len(values),
        "observations_truncated": len(values) > 128,
        "status_counts": dict(Counter(v["status"] for v in values)), "errors": list(snapshot.errors[:16]),
        "identity_status": identity,
        "context_status": available, "acquisition_status": acquisition,
        "global_context_status": "VERIFIED_GLOBAL_CONTEXT" if global_available else "STALE_OR_UNAVAILABLE",
        "exchange_context_status": "VERIFIED_EXCHANGE_AGGREGATE" if any(v["status"] == "VERIFIED"
            and v["scope"] == "exchange_all_contracts" for v in values) else "STALE_OR_UNAVAILABLE",
        "contract_context_status": "VERIFIED_CONTRACT_CONTEXT" if contract_available else "STALE_OR_UNAVAILABLE",
        "research_warning": "Observational research status; not a trading confirmation.",
        "setup_authority": False}
