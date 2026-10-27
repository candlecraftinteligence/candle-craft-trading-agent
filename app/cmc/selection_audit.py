"""Bounded selection provenance; Telegram delivery never constitutes a trade fill."""
from __future__ import annotations

import asyncio
import logging
from datetime import UTC

from app.cmc import integration
from app.cmc.models import stable_id
from app.cmc.leverage import selection_leverage

logger = logging.getLogger(__name__)


def order_delivery_ties(result, mode):
    """Reorder only occupied slots sharing an exact canonical quality rank.

    No candidate is added/removed, no capacity is changed, and prior-public
    follow-ups stay in their original slots. This also works with V1's current
    CONFIRMED delivery policy; the legacy WATCHLIST path need not be enabled.
    """
    if mode != "decision":
        return result
    groups = {}
    ordered = list(result.results)
    for index, row in enumerate(ordered):
        evaluation = integration.decision_for(row)
        if (evaluation is not None and evaluation.mode.value == "decision"
                and evaluation.candidate.cci_eligible and evaluation.candidate.is_new_signal
                and evaluation.final_eligible and evaluation.candidate.canonical_rank):
            groups.setdefault(evaluation.candidate.canonical_rank, []).append((index, row, evaluation.priority_delta))
    for group in groups.values():
        for slot, item in zip(group, sorted(group, key=lambda item: -item[2])):
            ordered[slot[0]] = item[1]
    return result.model_copy(update={"results": tuple(ordered)})


def _order_refs(rows):
    return [stable_id((row.symbol, getattr(row.lifecycle_state, "setup_id", None),
                      getattr(row.lifecycle_state, "plan_version_id", None))) for row in rows]


async def audit_selection(result, settings, selection, *, selector, scan_run_id):
    """Persist exact selection intent before authority reaches the delivery loop.

    The same canonical selector and rate-limit cutoff compute both alternatives.
    If the audit cannot be persisted, discard CMC authority and use baseline.
    """
    if settings.cmc_mode not in {"shadow", "decision"}:
        return result, selection, None
    baseline_result = integration.clear_evaluations(result)
    baseline = selector(baseline_result.results)
    try:
        service = await integration.service_for(settings)
        if service is None:
            return baseline_result, baseline, None
        ordered_result = order_delivery_ties(result, settings.cmc_mode)
        evaluations = []
        for row in result.results:
            evaluation = integration.decision_for(row)
            if evaluation is None:
                continue
            evaluations.append({"evaluation_id": stable_id(evaluation),
                "candidate": evaluation.candidate.model_dump(mode="json"),
                "mode": evaluation.mode.value, "decision_at": evaluation.decision_at.isoformat(),
                "priority_delta": evaluation.priority_delta, "vetoed": evaluation.vetoed,
                "hypothetical_priority_delta": evaluation.hypothetical_priority_delta,
                "hypothetical_veto": evaluation.hypothetical_veto,
                "enforced_rule_hashes": [r.specification_hash for r in evaluation.recommendations if r.enforced]})
        before, after = set(baseline.selected_signal_ids), set(selection.selected_signal_ids)
        at = integration.datetime.now(UTC)
        record = {"schema_version": "cmc-selection/1", "scan_run_id": scan_run_id,
            "leverage":selection_leverage(tuple(row.cmc_evaluation for row in result.results
                if integration.decision_for(row) is not None)),
            "mode": settings.cmc_mode, "selected_at": at.isoformat(),
            "baseline_selected_signal_ids": sorted(before), "selected_signal_ids": sorted(after),
            "retained_signal_ids": sorted(before & after),
            "displaced_signal_ids": sorted(before - after), "introduced_signal_ids": sorted(after - before),
            "selection_changed": before != after, "evaluations": evaluations,
            "baseline_delivery_order": _order_refs(result.results),
            "delivery_order": _order_refs(ordered_result.results),
            "delivery_order_changed": _order_refs(result.results) != _order_refs(ordered_result.results),
            "status": "PERSISTED_SELECTION_INTENT_NOT_DELIVERY_OR_FILL_PROOF"}
        identity = await asyncio.wait_for(asyncio.to_thread(service.store.put,
            "selection_intent", record, acquired_at=at, decision_at=at), timeout=0.25)
        return ordered_result, selection, identity
    except Exception as exc:
        logger.warning("CMC selection audit unavailable; baseline retained: %s", type(exc).__name__)
        return baseline_result, baseline, None


async def record_application(settings, intent_id, deliveries):
    """Record observed send/block status; never infer fills or economic outcomes.

    Post-delivery audit failure cannot undo a send. The durable pre-application
    intent and canonical Telegram attempt ledger remain the authoritative trail.
    """
    if intent_id is None:
        return
    try:
        service = await integration.service_for(settings)
        if service is None:
            return
        rows = [{"signal_id": d.signal_id, "symbol": d.symbol,
            "alert_type": d.alert_type, "status": d.status,
            "cmc_veto_observed": d.error_message == "cmc_validated_rule_veto"} for d in deliveries]
        at = integration.datetime.now(UTC)
        await asyncio.wait_for(asyncio.to_thread(service.store.put,
            "selection_application", {"intent_id": intent_id, "observed_at": at.isoformat(),
                "status": "DELIVERY_BOUNDARY_COMPLETED", "deliveries": rows,
                "economic_outcomes": "N/A", "fills": "N/A"}, acquired_at=at), timeout=0.25)
    except Exception as exc:
        logger.warning("CMC delivery observation unavailable: %s", type(exc).__name__)
