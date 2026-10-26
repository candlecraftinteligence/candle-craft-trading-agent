"""Selection-only boundary after canonical lifecycle/quality decisions, before send."""
from __future__ import annotations

import asyncio
import logging
from decimal import Decimal
from datetime import UTC, datetime

from pydantic import TypeAdapter

from app.cmc.features import build_features, regime_compatibility
from app.cmc.models import Mode, stable_id
from app.cmc.native import consistency_feature, native_observations
from app.cmc.policy import Activation, Candidate, Evaluation, evaluate
from app.cmc.runtime import service_for

logger = logging.getLogger(__name__)


def load_activations(path):
    if path is None:
        return ()
    if path.stat().st_size > 128 * 1024:
        raise ValueError("activation manifest budget")
    return TypeAdapter(tuple[Activation, ...]).validate_json(path.read_text(encoding="utf-8"))


async def evaluate_delivery_run(result, settings, *, candidate_builder, service=None, decision_at=None):
    """Only sidecar-persisted decisions can affect selection; any failure preserves baseline."""
    mode = Mode(settings.cmc_mode)
    if mode in {Mode.OFF, Mode.COLLECT}:
        return result
    original_cutoff = result.config.decision_timestamp
    cutoff = decision_at or datetime.now(UTC)
    if (original_cutoff is None or original_cutoff.tzinfo is None or cutoff.tzinfo is None
            or original_cutoff > cutoff):
        return result
    try:
        service = service or await service_for(settings)
        if service is None:
            return result
        snapshot = service.snapshot  # frozen once, never per-symbol provider calls
        activations = await asyncio.wait_for(asyncio.to_thread(load_activations, settings.cmc_activation_manifest), timeout=0.1)
        updated, records = [], []
        for symbol_result in result.results:
            candidate = candidate_builder(symbol_result)
            if not candidate.cci_eligible or not candidate.is_new_signal:
                evaluation=evaluate(candidate,(),mode=mode,as_of=cutoff,activations=activations)
                records.append({"evaluation":evaluation.model_dump(mode="json"),"features":[],"comparisons":[],
                    "native_observations":[],"application_status":"EVALUATED_NOT_DELIVERY_PROOF","batch_id":snapshot.batch_id})
                updated.append(symbol_result.model_copy(update={"cmc_evaluation":evaluation}))
                continue
            instruments = {o.instrument.key:o.instrument for o in snapshot.observations
                if o.instrument and o.instrument.symbol == symbol_result.symbol and o.instrument.exchange == result.config.exchange
                and o.status_at(cutoff).value == "VERIFIED"}
            instrument = next(iter(instruments.values())) if len(instruments) == 1 else None
            observations = tuple(o for o in snapshot.observations if o.instrument is None or
                instrument is not None and o.instrument.cmc_id == instrument.cmc_id)
            native = native_observations(symbol_result.cmc_native_evidence or {}, instrument) if instrument else ()
            features = build_features(observations, as_of=cutoff,
                cmc_id=instrument.cmc_id if instrument else None, instrument_key=instrument.key if instrument else None)
            consistency, comparisons = consistency_feature(observations, native, as_of=cutoff)
            features = (*features, consistency)
            regime=result.market_regime
            if regime.enabled and not regime.missing_data and not regime.unverified_data:
                risk=next(f for f in features if f.name=="CmcGlobalMarketRisk")
                features=(*features,regime_compatibility(risk,regime_state=regime.state.value,
                    direction=candidate.direction,regime_at=original_cutoff,
                    regime_ref="cci-regime:"+stable_id(regime),as_of=cutoff))
            evaluation = evaluate(candidate, features, mode=mode, as_of=cutoff, activations=activations)
            records.append({"evaluation":evaluation.model_dump(mode="json"),
                "features":[f.model_dump(mode="json") for f in features], "comparisons":comparisons,
                "native_observations":[o.model_dump(mode="json") for o in native],
                "application_status":"EVALUATED_NOT_DELIVERY_PROOF",
                "batch_id":snapshot.batch_id})
            updated.append(symbol_result.model_copy(update={"cmc_evaluation":evaluation}))
        await asyncio.wait_for(asyncio.to_thread(service.store.put, "policy_batch", records,
            acquired_at=datetime.now(UTC), decision_at=cutoff), timeout=0.25)
        return result.model_copy(update={"results":tuple(updated)})
    except Exception as exc:
        logger.warning("CMC policy unavailable; canonical selection retained: %s", type(exc).__name__)
        # Never reuse attached enforcement if a resumed result crosses a failed/new boundary.
        return result.model_copy(update={"results":tuple(r.model_copy(update={"cmc_evaluation":None}) for r in result.results)})


def decision_for(symbol_result):
    value = getattr(symbol_result, "cmc_evaluation", None)
    if not isinstance(value, Evaluation):
        return None
    enforced = [r for r in value.recommendations if r.enforced]
    now = datetime.now(UTC)
    if any(not r.data_timestamps or any((now-t).total_seconds() > 300 or t > now for t in r.data_timestamps) for r in enforced):
        return None
    record = getattr(symbol_result, "lifecycle_state", None)
    if (record is None or value.candidate.setup_id != record.setup_id or
            value.candidate.economic_plan_id != record.plan_version_id or value.candidate.symbol != symbol_result.symbol
            or value.candidate.direction != record.direction):
        return None
    return value


def priority(symbol_result):
    decision = decision_for(symbol_result)
    return decision.priority_delta if decision and decision.mode == Mode.DECISION else 0


def veto_applies(symbol_result, *, alert_type: str, previously_public: bool):
    decision = decision_for(symbol_result)
    return bool(decision and decision.mode == Mode.DECISION and decision.vetoed
                and decision.candidate.is_new_signal and not previously_public
                and alert_type.upper() in {"WATCHLIST", "SIGNAL_CONFIRMED"})


async def prepare_selection(result, settings, context, repository=None, *, service=None):
    if settings.cmc_mode in {"off", "collect"}:
        return result
    # Local import avoids changing the canonical alert module's import ownership.
    from app.alerts.telegram_lifecycle import (
        telegram_alert_decision_for_symbol, _signal_id_candidates,
        _confirmed_alert_attempt_prefilter, _public_watchlist_gate_result,
        _public_watchlist_scan_candidate_sort_key,
    )
    from app.formatters.telegram_signal_formatter import TelegramAlertType

    def candidate_builder(symbol_result):
        prior = repository.get_prior_public_alert(signal_ids=_signal_id_candidates(symbol_result)) if repository else None
        decision = telegram_alert_decision_for_symbol(symbol_result,
            previously_active_sent=prior is not None, eligibility_context=context)
        eligible = decision.eligible
        if eligible and decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED:
            eligible = _confirmed_alert_attempt_prefilter(symbol_result, decision.message, context).passed
        elif eligible and decision.alert_type == TelegramAlertType.WATCHLIST:
            eligible = _public_watchlist_gate_result(symbol_result, decision.message, context).allowed
        record = symbol_result.lifecycle_state
        sort_key=_public_watchlist_scan_candidate_sort_key(symbol_result,decision.message) if decision.message else None
        return Candidate(setup_id=record.setup_id or "N/A" if record else "N/A",
            economic_plan_id=record.plan_version_id or "N/A" if record else "N/A",
            symbol=symbol_result.symbol, direction=record.direction if record else "N/A",
            original_decision_at=result.config.decision_timestamp,
            canonical_rank=tuple(Decimal(str(x)) for x in sort_key[:4]) if sort_key else (),
            baseline_tie_rank=(Decimal(sort_key[-2]),) if sort_key else (),
            cci_eligible=eligible, is_new_signal=prior is None and decision.alert_type in {
                TelegramAlertType.WATCHLIST,TelegramAlertType.SIGNAL_CONFIRMED})

    return await evaluate_delivery_run(result, settings, candidate_builder=candidate_builder, service=service)
