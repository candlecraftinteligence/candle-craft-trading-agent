from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    entry_touched as _entry_touched,
    newly_touched_targets as _newly_touched_targets,
    stop_touched as _stop_touched,
    stored_plan_geometry as _stored_plan_geometry,
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



@dataclass(frozen=True)
class LifecycleOutcomeEvaluation:
    record: SetupLifecycleRecord
    progress: SetupLifecycleOutcomeProgress | None
    last_transition: SetupTransitionResult | None = None
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
    plan_identity = canonical_plan_identity(record)
    progress = repository.get_outcome_progress(
        lifecycle_id=record.lifecycle_id,
        plan_identity=plan_identity,
    )

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

    try:
        geometry = _stored_plan_geometry(record)
    except ValueError as exc:
        progress = _integrity_failure(
            progress,
            status=INTEGRITY_FAILED,
            diagnostic=f"invalid_stored_plan_geometry:{exc}",
            evaluated_at=evaluated_at,
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

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

    if progress.evaluation_cursor_open_at is None:
        baseline = window.timeline[-1]
        progress = _with_cursor(
            progress,
            baseline,
            evaluated_at=evaluated_at,
            diagnostic="cursor_initialized_without_retroactive_evaluation",
            processed_candles=0,
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

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

    latest_open = window.timeline[-1].open_timestamp
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

    pending = tuple(
        (causal, high, low)
        for causal, (high, low) in zip(window.timeline, candle_ranges, strict=True)
        if causal.open_timestamp > cursor_open
    )
    if not pending:
        progress = progress.model_copy(
            update={
                "integrity_status": INTEGRITY_VERIFIED,
                "diagnostic": NA,
                "last_evaluated_at": evaluated_at,
            }
        )
        repository.upsert_outcome_progress(progress)
        return LifecycleOutcomeEvaluation(record=record, progress=progress)

    expected_open = cursor_open + timeframe_duration(normalized_timeframe)
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
    processed = 0
    for causal, high, low in pending:
        processed += 1
        candle_close = causal.close_timestamp.isoformat()
        if progress.entry_at is None:
            if _entry_touched(high, low, geometry):
                progress = progress.model_copy(update={"entry_at": candle_close})
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
                )
                current_record, entry_transition = _advance_to_managing(
                    current_record,
                    repository=repository,
                    causal=causal,
                    evaluated_at=evaluated_at,
                    scan_run_id=scan_run_id,
                    plan_identity=plan_identity,
                )
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
