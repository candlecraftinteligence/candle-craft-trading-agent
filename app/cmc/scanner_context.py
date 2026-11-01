"""Persisted SHADOW context for every analyzed candidate, including rejections."""
from __future__ import annotations

from collections import Counter


def scanner_context(snapshot, *, symbol, cutoff, assessed_at):
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
    return {"mode": "shadow", "usage": "external_context_only", "batch_id": snapshot.batch_id,
        "status": "VERIFIED_CONTEXT_PRESENT" if any(v["status"] == "VERIFIED" for v in values)
            else "N/A: acquisition_pending_or_no_usable_context",
        "strategy_cutoff": cutoff.isoformat(), "assessed_at": assessed_at.isoformat(),
        "observations": values[:128], "observation_count": len(values),
        "observations_truncated": len(values) > 128,
        "status_counts": dict(Counter(v["status"] for v in values)), "errors": list(snapshot.errors[:16]),
        "identity_status": "MAPPED" if any(v["contract_symbol"] == symbol for v in values) else "N/A: no admitted exact contract mapping",
        "setup_authority": False}
