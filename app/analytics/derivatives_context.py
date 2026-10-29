"""Compact native supporting context using the existing source-truth evaluator.

The structural decision cutoff is immutable. Later observations may be shown
as supporting context with their own explicit cutoff; they cannot create a
structural confirmation or be backdated into the strategy decision.
"""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.analytics.derivatives_source_truth import evaluate_derivatives_source_truth, observation_context_freshness
from app.cmc.models import stable_id, utc
from app.data.dtos import NA


def _current_metric(assessment, name, facts):
    if name == "funding_rate":
        if not facts:
            return False, ()
        state, reasons = observation_context_freshness(utc(facts[0].metric_time),
            utc(assessment.decision_cutoff), assessment.funding_cadence_ms)
        return state == "current", reasons
    if name == "oi_change_pct" and assessment.oi_change_context != "current":
        return False, tuple(code for code in assessment.reason_codes
                            if code in {"stale_for_current_context", "freshness_unjustified"})
    return True, ()


def assess_native_context(*, evidence, instrument, strategy_cutoff, observed_at):
    evidence = evidence or {}
    def evaluate(at):
        return evaluate_derivatives_source_truth(decision_cutoff=at, instrument=instrument,
            open_interest=evidence.get("open_interest"), open_interest_history=evidence.get("open_interest_history"),
            funding=evidence.get("funding_rate"), funding_history=evidence.get("funding_history"),
            account_ratio=evidence.get("long_short_ratio"))
    strategy = evaluate(strategy_cutoff)
    observed = evaluate(observed_at)
    metrics = {}
    for name, value, selected, excluded, slots in (
        ("funding_rate", observed.funding_rate, ("selected_funding",), "excluded_funding", ("funding_rate", "funding_history")),
        ("oi_change_pct", observed.oi_change_pct, ("selected_current_oi", "selected_previous_oi"), "excluded_oi", ("open_interest", "open_interest_history")),
        ("account_ratio", observed.account_ratio, ("selected_account_ratio",), "excluded_account_ratio", ("long_short_ratio",)),
    ):
        facts = [fact for fact in observed.lineage if fact.role in selected]
        rejected = [fact for fact in observed.lineage if fact.role == excluded]
        reasons = tuple(dict.fromkeys(code for fact in rejected for code in fact.reason_codes))
        current, freshness_reasons = _current_metric(observed, name, facts)
        reasons = tuple(dict.fromkeys((*reasons, *freshness_reasons)))
        missing = all(not getattr(evidence.get(slot), "observations", ()) for slot in slots)
        # Selected roles are the evaluator's admission decision. Informational
        # lineage notes (e.g. request-category provenance) are not blockers.
        admitted = current and value != NA and {fact.role for fact in facts} == set(selected)
        status = "VERIFIED" if admitted else "MISSING" if missing else "STALE" if any("stale" in code for code in reasons) else "UNVERIFIED"
        metrics[name] = {"status": status, "value": value if admitted else NA,
            "sources": [{"exchange": fact.exchange, "symbol": fact.requested_symbol,
                "endpoint": fact.endpoint, "field": fact.raw_field, "unit": fact.unit,
                "counting_convention": fact.counting_convention, "source_at": fact.metric_time,
                "acquired_at": fact.acquired_at, "delivered_at": fact.cache_delivered_at} for fact in facts],
            "reasons": reasons[:8]}
    context = {"policy_version": "native-supporting-context-v1",
        "strategy_cutoff": strategy_cutoff.isoformat() if strategy_cutoff else NA,
        "observed_at": observed_at.isoformat() if observed_at else NA,
        "usage": "supporting_context", "validated_confirmation": False,
        "metrics": metrics, "oi_interval": observed.oi_interval_label,
        "limitation": "Optional context cannot replace price structure or entry confirmation."}
    context["source_ref"] = stable_id(context)
    return strategy, context


def admitted_strategy_values(assessment):
    """Only values admitted at the structural cutoff reach existing numeric guards."""
    selected = {fact.role: fact for fact in assessment.lineage if fact.role.startswith("selected_")}
    def number(role):
        fact = selected.get(role)
        if fact is None:
            return NA
        value = Decimal(fact.raw_value)
        return value if value.is_finite() else NA
    funding = None
    if (assessment.funding_rate != NA and "selected_funding" in selected
            and _current_metric(assessment, "funding_rate", [selected["selected_funding"]])[0]):
        funding = {"funding_rate": Decimal(assessment.funding_rate), "source": "native_verified_at_strategy_cutoff"}
    current, previous = number("selected_current_oi"), number("selected_previous_oi")
    oi = {"open_interest": current} if assessment.oi_change_context == "current" and assessment.oi_change_pct != NA and current != NA and previous != NA else None
    return {"funding": funding, "funding_history": None, "open_interest": oi,
            "open_interest_history": None, "previous_open_interest": previous if oi is not None else NA,
            "long_short_ratio": Decimal(assessment.account_ratio) if assessment.account_ratio != NA else NA}


def observed_stream_context(snapshot, *, as_of: datetime, stale_seconds: float, limitation: str, expected_symbol: str | None = None):
    """Recheck age at assessment time; never promote a disconnected snapshot."""
    if snapshot is None:
        return {"status": "MISSING", "limitation": limitation}
    status = str(getattr(getattr(snapshot, "status", None), "value", "UNVERIFIED"))
    if status in {"UNAVAILABLE", "MISSING", "ERROR"}:
        return {"status": "MISSING" if status == "UNAVAILABLE" else status, "limitation": limitation}
    if (expected_symbol is not None and snapshot.symbol != expected_symbol) or not getattr(snapshot, "verified", False):
        status = "STALE" if status == "STALE" else "UNVERIFIED"
    at = getattr(snapshot, "observed_at", None)
    if at is None or as_of is None or at > as_of:
        return {"status": "STALE" if status == "STALE" else "UNVERIFIED", "limitation": limitation}
    age = (as_of-at).total_seconds()
    if status != "VERIFIED" or age > stale_seconds:
        return {"status": "STALE" if age > stale_seconds or status == "STALE" else status,
                "source_at": at.isoformat(), "limitation": limitation}
    # These are bounded aggregate snapshots, never raw streams or order books.
    value = snapshot.model_dump(mode="json", include={"symbol", "source", "usage", "windows",
        "best_bid", "best_ask", "spread_absolute", "spread_bps", "bands",
        "largest_bid_level", "largest_ask_level", "orderflow_summary", "liquidation_summary"})
    return {"status": "VERIFIED", "source_at": at.isoformat(), "age_seconds": age,
            "usage": "supporting_context", "snapshot": value, "limitation": limitation}


def observed_spread(snapshot, *, symbol, cutoff, stale_seconds):
    """A synchronized, causal spread may only increase modeled execution cost."""
    context = observed_stream_context(snapshot, as_of=cutoff, stale_seconds=stale_seconds,
        expected_symbol=symbol, limitation="Observed spread; future execution may differ.")
    if context["status"] != "VERIFIED":
        return NA
    value = getattr(snapshot, "spread_absolute", None)
    if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
        return NA
    return value
