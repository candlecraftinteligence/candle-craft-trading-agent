from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.data.candle_integrity import (
    CandleIntegrityError,
    CausalCandle,
    closed_candles_as_of,
    normalize_utc_timestamp,
    timeframe_duration,
)
from app.data.dtos import NA
from app.lifecycle.models import (
    SetupLifecycleOutcomeProgress,
    SetupLifecycleRecord,
    SetupLifecycleState,
    SetupTransitionReason,
    SetupTransitionResult,
)
from app.lifecycle.outcome_events import (
    advance_to_managing as _advance_to_managing,
    insert_milestone_event as _insert_milestone_event,
    record_stop as _record_stop,
    record_terminal_transition as _record_terminal_transition,
    target_reason as _target_reason,
)
from app.lifecycle.outcome_policy import (
    _text,
    candle_range as _candle_range,
    canonical_plan_identity,
    compatible_plan_identities,
    entry_touched as _entry_touched,
    newly_touched_targets as _newly_touched_targets,
    stop_touched as _stop_touched,
    stored_plan_geometry as _stored_plan_geometry,
    stored_plan_geometry_failure,
)
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository

OUTCOME_ELIGIBLE_STATES = frozenset(
    {
        SetupLifecycleState.WATCHLISTED,
        SetupLifecycleState.STALKING,
        SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.ACTIONABLE_A_GRADE,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.EXECUTING,
        SetupLifecycleState.MANAGING,
    }
)
TERMINAL_OUTCOME_STATES = frozenset(
    {
        SetupLifecycleState.TP_HIT,
        SetupLifecycleState.SL_HIT,
        SetupLifecycleState.INVALIDATED,
        SetupLifecycleState.EXPIRED,
    }
)
INTEGRITY_VERIFIED = "Verified"
INTEGRITY_UNVERIFIED = "Unverified"
INTEGRITY_FAILED = "Failed"
ENTRY_CAUSALITY_CONTRACT = "fully_post_boundary_closed_candle_v1"
ENTRY_EVIDENCE_TYPE = "fully_post_boundary_closed_execution_candle_range"



@dataclass(frozen=True)
class LifecycleOutcomeEvaluation:
    record: SetupLifecycleRecord
    progress: SetupLifecycleOutcomeProgress | None
    last_transition: SetupTransitionResult | None = None
    transitions: tuple[SetupTransitionResult, ...] = ()
    processed_candles: int = 0



def evaluate_closed_candle_outcomes(
    record: SetupLifecycleRecord,
    *,
    execution_candles: Sequence[Any],
    execution_timeframe: str,
    decision_timestamp: Any,
    evaluated_at: str,
    repository: SQLiteSetupLifecycleRepository,
    scan_run_id: str | None = None,
) -> LifecycleOutcomeEvaluation:
    plan_identities = compatible_plan_identities(record)
    progress = next(
        (
            candidate
            for identity in plan_identities
            if (
                candidate := repository.get_outcome_progress(
                    lifecycle_id=record.lifecycle_id,
                    plan_identity=identity,
                )
            )
            is not None
        ),
        None,
    )
    plan_identity = progress.plan_identity if progress is not None else plan_identities[0]

    # Malformed legacy plans are preserved as evidence, but never create or
    # advance outcome progress. Explicit hygiene owns any quarantine transition.
    if stored_plan_geometry_failure(record) is not None:
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

    if progress is not None and progress.terminal_outcome != NA:
        return LifecycleOutcomeEvaluation(record=record, progress=progress)
    if record.current_state in TERMINAL_OUTCOME_STATES:
        progress = _terminal_progress_for_record(
            record,
            progress=progress,
            plan_identity=plan_identity,
            execution_timeframe=execution_timeframe,
            evaluated_at=evaluated_at,
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)
    if record.current_state not in OUTCOME_ELIGIBLE_STATES:
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

    progress = progress or _new_progress(
        record,
        plan_identity=plan_identity,
        execution_timeframe=execution_timeframe,
        evaluated_at=evaluated_at,
    )
    normalized_timeframe = _text(execution_timeframe).lower()
    if normalized_timeframe == NA.lower():
        progress = _integrity_failure(
            progress,
            status=INTEGRITY_UNVERIFIED,
            diagnostic="missing_execution_timeframe",
            evaluated_at=evaluated_at,
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)
    if progress.execution_timeframe != normalized_timeframe:
        progress = _integrity_failure(
            progress,
            status=INTEGRITY_FAILED,
            diagnostic=(
                "execution_timeframe_changed:"
                f"{progress.execution_timeframe}->{normalized_timeframe}"
            ),
            evaluated_at=evaluated_at,
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

    geometry = _stored_plan_geometry(record)

    if not execution_candles:
        progress = _integrity_failure(
            progress,
            status=INTEGRITY_UNVERIFIED,
            diagnostic="missing_execution_candle_history",
            evaluated_at=evaluated_at,
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

    try:
        window = closed_candles_as_of(
            execution_candles,
            timeframe=normalized_timeframe,
            decision_timestamp=decision_timestamp,
            minimum_closed_history=0,
            require_continuity=True,
        )
        candle_ranges = tuple(_candle_range(item) for item in window.timeline)
    except (CandleIntegrityError, ValueError) as exc:
        progress = _integrity_failure(
            progress,
            status=INTEGRITY_UNVERIFIED,
            diagnostic=str(exc),
            evaluated_at=evaluated_at,
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

    if not window.timeline:
        progress = _integrity_failure(
            progress,
            status=INTEGRITY_UNVERIFIED,
            diagnostic="no_closed_execution_candles_at_decision_boundary",
            evaluated_at=evaluated_at,
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

    if (
        progress.entry_at is None
        and _metadata(progress).get("entry_causality_contract") != ENTRY_CAUSALITY_CONTRACT
    ):
        metadata = _metadata(progress)
        metadata.update(
            {
                "entry_causality_contract": ENTRY_CAUSALITY_CONTRACT,
                "entry_causality_migration": "legacy_unfilled_cursor_rebased",
            }
        )
        progress = progress.model_copy(
            update={
                "tracking_start_at": None,
                "evaluation_cursor_open_at": None,
                "evaluation_cursor_close_at": None,
                "metadata_json": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            }
        )

    fresh_cursor = progress.evaluation_cursor_open_at is None
    if progress.tracking_start_at is None:
        if fresh_cursor:
            try:
                (
                    tracking_start,
                    boundary_timestamp,
                    boundary_source,
                    boundary_eligibility,
                    boundary_diagnostic,
                ) = _tracking_start_boundary(
                    record,
                    progress=progress,
                    timeline=window.timeline,
                    evaluated_at=evaluated_at,
                    repository=repository,
                    compatible_identities=plan_identities,
                    execution_timeframe=normalized_timeframe,
                )
            except ValueError as exc:
                progress = _integrity_failure(
                    progress,
                    status=INTEGRITY_UNVERIFIED,
                    diagnostic=str(exc),
                    evaluated_at=evaluated_at,
                )
                repository.upsert_outcome_progress(progress)
                return LifecycleOutcomeEvaluation(record=record, progress=progress)
            progress = _with_tracking_start(
                progress,
                tracking_start=tracking_start,
                boundary_timestamp=boundary_timestamp,
                boundary_source=boundary_source,
                boundary_eligibility=boundary_eligibility,
                boundary_diagnostic=boundary_diagnostic,
            )
        else:
            try:
                existing_cursor = normalize_utc_timestamp(
                    progress.evaluation_cursor_open_at,
                    field_name="evaluation_cursor_open_at",
                )
            except ValueError as exc:
                progress = _integrity_failure(
                    progress,
                    status=INTEGRITY_FAILED,
                    diagnostic=f"invalid_persisted_evaluation_cursor:{exc}",
                    evaluated_at=evaluated_at,
                )
                repository.upsert_outcome_progress(progress)
                return LifecycleOutcomeEvaluation(record=record, progress=progress)
            progress = _with_tracking_start(
                progress,
                tracking_start=existing_cursor,
                boundary_timestamp=progress.evaluation_cursor_open_at,
                boundary_source="existing_cursor_v19_backfill",
                boundary_eligibility="legacy_entry_already_persisted",
                boundary_diagnostic=NA,
            )

    try:
        tracking_start = normalize_utc_timestamp(
            progress.tracking_start_at,
            field_name="tracking_start_at",
        )
    except ValueError as exc:
        progress = _integrity_failure(
            progress,
            status=INTEGRITY_FAILED,
            diagnostic=f"invalid_persisted_tracking_start:{exc}",
            evaluated_at=evaluated_at,
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

    latest_open = window.timeline[-1].open_timestamp
    if fresh_cursor:
        expected_open = tracking_start
        pending = tuple(
            (causal, high, low)
            for causal, (high, low) in zip(window.timeline, candle_ranges, strict=True)
            if causal.open_timestamp >= tracking_start
        )
    else:
        try:
            cursor_open = normalize_utc_timestamp(
                progress.evaluation_cursor_open_at,
                field_name="evaluation_cursor_open_at",
            )
        except ValueError as exc:
            progress = _integrity_failure(
                progress,
                status=INTEGRITY_FAILED,
                diagnostic=f"invalid_persisted_evaluation_cursor:{exc}",
                evaluated_at=evaluated_at,
            )
            repository.upsert_outcome_progress(progress)
            return LifecycleOutcomeEvaluation(record=record, progress=progress)
        if latest_open < cursor_open:
            progress = _integrity_failure(
                progress,
                status=INTEGRITY_UNVERIFIED,
                diagnostic=(
                    "stale_execution_candle_history:"
                    f"latest={latest_open.isoformat()} cursor={cursor_open.isoformat()}"
                ),
                evaluated_at=evaluated_at,
            )
            repository.upsert_outcome_progress(progress)
            return LifecycleOutcomeEvaluation(record=record, progress=progress)
        expected_open = cursor_open + timeframe_duration(normalized_timeframe)
        pending = tuple(
            (causal, high, low)
            for causal, (high, low) in zip(window.timeline, candle_ranges, strict=True)
            if causal.open_timestamp > cursor_open
        )

    if not pending:
        if fresh_cursor:
            if latest_open < tracking_start:
                metadata = _metadata(progress)
                waiting_diagnostic = str(
                    metadata.get(
                        "boundary_diagnostic",
                        "awaiting_first_fully_post_boundary_candle",
                    )
                )
                progress = progress.model_copy(
                    update={
                        "integrity_status": INTEGRITY_VERIFIED,
                        "diagnostic": waiting_diagnostic,
                        "last_evaluated_at": evaluated_at,
                    }
                )
                repository.upsert_outcome_progress(progress)
                return LifecycleOutcomeEvaluation(record=record, progress=progress)
            progress = _integrity_failure(
                progress,
                status=INTEGRITY_UNVERIFIED,
                diagnostic=(
                    "missing_execution_candle_history:"
                    f"tracking_start_at={tracking_start.isoformat()} "
                    f"latest_open={latest_open.isoformat()}"
                ),
                evaluated_at=evaluated_at,
            )
            repository.upsert_outcome_progress(progress)
            return LifecycleOutcomeEvaluation(record=record, progress=progress)
        progress = progress.model_copy(
            update={
                "integrity_status": INTEGRITY_VERIFIED,
                "diagnostic": NA,
                "last_evaluated_at": evaluated_at,
            }
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

    if pending[0][0].open_timestamp != expected_open:
        progress = _integrity_failure(
            progress,
            status=INTEGRITY_UNVERIFIED,
            diagnostic=(
                "missing_execution_candle_history:"
                f"expected_open={expected_open.isoformat()} "
                f"received_open={pending[0][0].open_timestamp.isoformat()}"
            ),
            evaluated_at=evaluated_at,
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

    current_record = record
    last_transition: SetupTransitionResult | None = None
    transitions: list[SetupTransitionResult] = []
    processed = 0
    for causal, high, low in pending:
        processed += 1
        candle_close = causal.close_timestamp.isoformat()
        if progress.entry_at is None:
            if _entry_touched(high, low, geometry):
                evidence = _entry_evidence(
                    current_record,
                    progress=progress,
                    causal=causal,
                    candle_high=high,
                    candle_low=low,
                    entry_low=geometry.entry_low,
                    entry_high=geometry.entry_high,
                    entry_at=candle_close,
                    evaluated_at=evaluated_at,
                )
                progress = _with_entry_evidence(
                    progress,
                    entry_at=candle_close,
                    evidence=evidence,
                )
                _insert_milestone_event(
                    repository,
                    current_record,
                    reason=SetupTransitionReason.ENTRY_ACTIVATED,
                    event_type="entry_activated",
                    causal=causal,
                    evaluated_at=evaluated_at,
                    scan_run_id=scan_run_id,
                    plan_identity=plan_identity,
                    level=f"{geometry.entry_low}:{geometry.entry_high}",
                    evidence=evidence,
                )
                current_record, entry_transition, entry_transitions = _advance_to_managing(
                    current_record,
                    repository=repository,
                    causal=causal,
                    evaluated_at=evaluated_at,
                    scan_run_id=scan_run_id,
                    plan_identity=plan_identity,
                )
                transitions.extend(entry_transitions)
                last_transition = entry_transition or last_transition
                if current_record.current_state != SetupLifecycleState.MANAGING:
                    progress = _integrity_failure(
                        progress,
                        status=INTEGRITY_FAILED,
                        diagnostic=(
                            "entry_state_progression_failed:"
                            f"state={current_record.current_state.value}"
                        ),
                        evaluated_at=evaluated_at,
                    )
                    break
                if _stop_touched(high, low, geometry):
                    current_record, last_transition, progress = _record_stop(
                        current_record,
                        progress,
                        geometry=geometry,
                        causal=causal,
                        evaluated_at=evaluated_at,
                        repository=repository,
                        scan_run_id=scan_run_id,
                        plan_identity=plan_identity,
                        ambiguity_reason="entry_and_stop_same_candle_stop_wins",
                    )
                    transitions.append(last_transition)
            # Established policy: the entry candle can prove the fill (and a
            # conservative same-candle stop), but targets begin with the next
            # closed execution candle.
            progress = _with_cursor(
                progress,
                causal,
                evaluated_at=evaluated_at,
                processed_candles=1,
            )
            if progress.terminal_outcome != NA:
                break
            continue

        touched_targets = _newly_touched_targets(high, low, geometry, progress)
        if _stop_touched(high, low, geometry):
            ambiguity_reason = (
                "post_entry_stop_and_target_same_candle_stop_wins"
                if touched_targets
                else NA
            )
            current_record, last_transition, progress = _record_stop(
                current_record,
                progress,
                geometry=geometry,
                causal=causal,
                evaluated_at=evaluated_at,
                repository=repository,
                scan_run_id=scan_run_id,
                plan_identity=plan_identity,
                ambiguity_reason=ambiguity_reason,
            )
            transitions.append(last_transition)
        else:
            for target_number, target in touched_targets:
                progress = progress.model_copy(
                    update={f"tp{target_number}_at": candle_close}
                )
                _insert_milestone_event(
                    repository,
                    current_record,
                    reason=_target_reason(target_number),
                    event_type=f"tp{target_number}_hit",
                    causal=causal,
                    evaluated_at=evaluated_at,
                    scan_run_id=scan_run_id,
                    plan_identity=plan_identity,
                    level=str(target),
                )
            if progress.tp3_at is not None:
                current_record, last_transition = _record_terminal_transition(
                    current_record,
                    to_state=SetupLifecycleState.TP_HIT,
                    reason=SetupTransitionReason.TAKE_PROFIT_HIT,
                    event_type="terminal_tp_hit",
                    causal=causal,
                    evaluated_at=evaluated_at,
                    repository=repository,
                    scan_run_id=scan_run_id,
                    plan_identity=plan_identity,
                )
                transitions.append(last_transition)
                progress = progress.model_copy(
                    update={
                        "terminal_outcome": SetupLifecycleState.TP_HIT.value,
                        "outcome_at": candle_close,
                    }
                )

        progress = _with_cursor(
            progress,
            causal,
            evaluated_at=evaluated_at,
            processed_candles=1,
        )
        if progress.terminal_outcome != NA:
            break

    repository.upsert_record(current_record)
    repository.upsert_outcome_progress(progress)
    return LifecycleOutcomeEvaluation(
        record=current_record,
        progress=progress,
        last_transition=last_transition,
        transitions=tuple(transitions),
        processed_candles=processed,
    )


def _new_progress(
    record: SetupLifecycleRecord,
    *,
    plan_identity: str,
    execution_timeframe: str,
    evaluated_at: str,
) -> SetupLifecycleOutcomeProgress:
    return SetupLifecycleOutcomeProgress(
        lifecycle_id=record.lifecycle_id,
        plan_identity=plan_identity,
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        execution_timeframe=_text(execution_timeframe).lower(),
        integrity_status=NA,
        diagnostic=NA,
        metadata_json=json.dumps(
            {
                "ambiguity_policy": "conservative_stop_wins",
                "entry_causality_contract": ENTRY_CAUSALITY_CONTRACT,
                "plan_identity": plan_identity,
                "processed_candle_count": 0,
                "source": "canonical_lifecycle_closed_execution_candles",
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        first_evaluated_at=evaluated_at,
        last_evaluated_at=evaluated_at,
    )


def _terminal_progress_for_record(
    record: SetupLifecycleRecord,
    *,
    progress: SetupLifecycleOutcomeProgress | None,
    plan_identity: str,
    execution_timeframe: str,
    evaluated_at: str,
) -> SetupLifecycleOutcomeProgress:
    progress = progress or _new_progress(
        record,
        plan_identity=plan_identity,
        execution_timeframe=execution_timeframe,
        evaluated_at=evaluated_at,
    )
    updates: dict[str, Any] = {
        "terminal_outcome": record.current_state.value,
        "outcome_at": progress.outcome_at or record.last_transition_at,
        "last_evaluated_at": evaluated_at,
    }
    if record.current_state == SetupLifecycleState.INVALIDATED:
        updates["invalidated_at"] = progress.invalidated_at or record.last_transition_at
    if progress.integrity_status == NA:
        updates["integrity_status"] = INTEGRITY_UNVERIFIED
        updates["diagnostic"] = "terminal_state_preceded_canonical_outcome_cursor"
    return progress.model_copy(update=updates)


def _tracking_start_boundary(
    record: SetupLifecycleRecord,
    *,
    progress: SetupLifecycleOutcomeProgress,
    timeline: Sequence[CausalCandle],
    evaluated_at: str,
    repository: SQLiteSetupLifecycleRepository,
    compatible_identities: Sequence[str],
    execution_timeframe: str,
) -> tuple[datetime, str, str, str, str]:
    confirmation_value = record.confirmed_at
    confirmation_source = "lifecycle_confirmed_at"
    if not confirmation_value or _text(confirmation_value) == NA:
        confirmed_event = next(
            (
                event
                for event in repository.list_events(lifecycle_id=record.lifecycle_id)
                if event.to_state == SetupLifecycleState.CONFIRMED
            ),
            None,
        )
        if confirmed_event is None:
            raise ValueError("missing_entry_tracking_confirmation_boundary")
        confirmation_value = confirmed_event.timestamp
        confirmation_source = "lifecycle_confirmed_event"
    try:
        confirmation_timestamp = normalize_utc_timestamp(
            confirmation_value,
            field_name="entry_tracking_confirmation_boundary",
        )
    except ValueError as exc:
        raise ValueError(f"invalid_entry_tracking_confirmation_boundary:{exc}") from exc

    compatible_identity_set = set(compatible_identities)
    historical_plans = tuple(
        item
        for item in repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)
        if item.plan_identity not in compatible_identity_set
    )
    if historical_plans:
        material_plan_value = progress.first_evaluated_at or evaluated_at
        try:
            material_plan_timestamp = normalize_utc_timestamp(
                material_plan_value,
                field_name="material_plan_first_evaluated_at",
            )
        except ValueError as exc:
            raise ValueError(f"invalid_material_plan_tracking_boundary:{exc}") from exc
        if material_plan_timestamp > confirmation_timestamp:
            boundary_timestamp = material_plan_timestamp
            boundary_source = "material_plan_first_evaluated_at_after_confirmation"
        else:
            boundary_timestamp = confirmation_timestamp
            boundary_source = confirmation_source
    else:
        boundary_timestamp = confirmation_timestamp
        boundary_source = confirmation_source

    earliest_open = timeline[0].open_timestamp
    if boundary_timestamp < earliest_open:
        raise ValueError(
            "missing_execution_candle_history:"
            f"tracking_boundary_at={boundary_timestamp.isoformat()} "
            f"earliest_open={earliest_open.isoformat()}"
        )
    duration = timeframe_duration(execution_timeframe)
    tracking_start = _first_fully_post_boundary_open(
        boundary_timestamp,
        timeline=timeline,
        duration=duration,
    )
    if boundary_timestamp >= timeline[0].open_timestamp and tracking_start > boundary_timestamp:
        boundary_eligibility = "partially_overlapping_boundary"
        boundary_diagnostic = "partial_boundary_candle_not_entry_eligible"
    elif tracking_start == boundary_timestamp:
        boundary_eligibility = "boundary_exactly_at_candle_open"
        boundary_diagnostic = NA
    else:
        boundary_eligibility = "fully_post_boundary_history_start"
        boundary_diagnostic = NA
    return (
        tracking_start,
        boundary_timestamp.isoformat(),
        boundary_source,
        boundary_eligibility,
        boundary_diagnostic,
    )


def _first_fully_post_boundary_open(
    boundary_timestamp: datetime,
    *,
    timeline: Sequence[CausalCandle],
    duration: timedelta,
) -> datetime:
    earliest_open = timeline[0].open_timestamp
    if boundary_timestamp <= earliest_open:
        return earliest_open
    elapsed = boundary_timestamp - earliest_open
    periods = elapsed // duration
    candidate = earliest_open + (duration * periods)
    if candidate < boundary_timestamp:
        candidate += duration
    return candidate


def _with_tracking_start(
    progress: SetupLifecycleOutcomeProgress,
    *,
    tracking_start: datetime,
    boundary_timestamp: str,
    boundary_source: str,
    boundary_eligibility: str,
    boundary_diagnostic: str,
) -> SetupLifecycleOutcomeProgress:
    metadata = _metadata(progress)
    metadata.update(
        {
            "tracking_boundary_source": boundary_source,
            "tracking_boundary_timestamp": boundary_timestamp,
            "boundary_candle_eligibility_decision": boundary_eligibility,
            "boundary_diagnostic": boundary_diagnostic,
            "entry_causality_contract": ENTRY_CAUSALITY_CONTRACT,
            "tracking_start_at": tracking_start.isoformat(),
        }
    )
    return progress.model_copy(
        update={
            "tracking_start_at": tracking_start.isoformat(),
            "metadata_json": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        }
    )




def _with_cursor(
    progress: SetupLifecycleOutcomeProgress,
    causal: CausalCandle,
    *,
    evaluated_at: str,
    diagnostic: str = NA,
    processed_candles: int,
) -> SetupLifecycleOutcomeProgress:
    metadata = _metadata(progress)
    metadata["processed_candle_count"] = int(metadata.get("processed_candle_count", 0)) + processed_candles
    metadata["last_candle_open_at"] = causal.open_timestamp.isoformat()
    metadata["last_candle_close_at"] = causal.close_timestamp.isoformat()
    return progress.model_copy(
        update={
            "evaluation_cursor_open_at": causal.open_timestamp.isoformat(),
            "evaluation_cursor_close_at": causal.close_timestamp.isoformat(),
            "integrity_status": INTEGRITY_VERIFIED,
            "diagnostic": diagnostic,
            "metadata_json": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            "last_evaluated_at": evaluated_at,
        }
    )


def _entry_evidence(
    record: SetupLifecycleRecord,
    *,
    progress: SetupLifecycleOutcomeProgress,
    causal: CausalCandle,
    candle_high: Any,
    candle_low: Any,
    entry_low: Any,
    entry_high: Any,
    entry_at: str,
    evaluated_at: str,
) -> dict[str, str]:
    metadata = _metadata(progress)
    return {
        "lifecycle_id": record.lifecycle_id,
        "symbol": record.symbol,
        "direction": record.direction,
        "entry_low": _text(entry_low),
        "entry_high": _text(entry_high),
        "tracking_boundary_timestamp": _text(
            metadata.get("tracking_boundary_timestamp")
        ),
        "tracking_boundary_source": _text(metadata.get("tracking_boundary_source")),
        "causal_candle_open": causal.open_timestamp.isoformat(),
        "causal_candle_close": causal.close_timestamp.isoformat(),
        "candle_high": _text(candle_high),
        "candle_low": _text(candle_low),
        "entry_evidence_type": ENTRY_EVIDENCE_TYPE,
        "entry_evidence_eligibility_decision": "fully_post_boundary",
        "entry_at": entry_at,
        "evaluated_at": evaluated_at,
    }


def _with_entry_evidence(
    progress: SetupLifecycleOutcomeProgress,
    *,
    entry_at: str,
    evidence: Mapping[str, Any],
) -> SetupLifecycleOutcomeProgress:
    metadata = _metadata(progress)
    metadata.update({str(key): value for key, value in evidence.items()})
    return progress.model_copy(
        update={
            "entry_at": entry_at,
            "metadata_json": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        }
    )


def _integrity_failure(
    progress: SetupLifecycleOutcomeProgress,
    *,
    status: str,
    diagnostic: str,
    evaluated_at: str,
) -> SetupLifecycleOutcomeProgress:
    metadata = _metadata(progress)
    metadata["last_integrity_diagnostic"] = diagnostic
    return progress.model_copy(
        update={
            "integrity_status": status,
            "diagnostic": diagnostic,
            "metadata_json": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            "last_evaluated_at": evaluated_at,
        }
    )


def _metadata(progress: SetupLifecycleOutcomeProgress) -> dict[str, Any]:
    try:
        value = json.loads(progress.metadata_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}



__all__ = [
    "INTEGRITY_FAILED",
    "INTEGRITY_UNVERIFIED",
    "INTEGRITY_VERIFIED",
    "LifecycleOutcomeEvaluation",
    "canonical_plan_identity",
    "evaluate_closed_candle_outcomes",
]
