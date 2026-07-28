from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.dtos import NA
from app.lifecycle.models import (
    ACTIVE_LIFECYCLE_MONITORING_STATES,
    SetupLifecycleEvent,
    SetupLifecycleRecord,
    SetupLifecycleState,
    SetupTransitionReason,
    SetupTransitionResult,
)
from app.lifecycle.outcome_policy import (
    INVALID_STORED_PLAN_GEOMETRY,
    stored_plan_geometry_failure,
)

ACTIVE_PROGRESSION = (
    SetupLifecycleState.DISCOVERED,
    SetupLifecycleState.WATCHLISTED,
    SetupLifecycleState.STALKING,
    SetupLifecycleState.TRIGGERED,
    SetupLifecycleState.CONFIRMED,
    SetupLifecycleState.ACTIONABLE_A_GRADE,
    SetupLifecycleState.A_GRADE_WATCH,
    SetupLifecycleState.EXECUTING,
    SetupLifecycleState.MANAGING,
)
VALID_STATES = {
    SetupLifecycleState.CONFIRMED,
    SetupLifecycleState.ACTIONABLE_A_GRADE,
    SetupLifecycleState.A_GRADE_WATCH,
    SetupLifecycleState.EXECUTING,
    SetupLifecycleState.MANAGING,
    SetupLifecycleState.TP_HIT,
    SetupLifecycleState.SL_HIT,
}
OUTCOME_STATES = {
    SetupLifecycleState.TP_HIT,
    SetupLifecycleState.SL_HIT,
    SetupLifecycleState.INVALIDATED,
    SetupLifecycleState.EXPIRED,
}
WATCH_PRIORITY_STATES = ACTIVE_LIFECYCLE_MONITORING_STATES[2:]
DEFAULT_COOLDOWN_HOURS = 24
DEFAULT_CONFIRMATION_CYCLES = 2
DEFAULT_SETUP_MERGE_TOLERANCE_PCT = Decimal("0.5")
PUBLIC_WATCHLIST_MIN_RR = Decimal("2.5")
CONFIRMED_MIN_RR = Decimal("3")
MIN_TECHNICAL_SCORE = Decimal("50")
DEFAULT_MIN_OPPORTUNITY_SCORE = Decimal("80")
MIN_CONFIRMATION_GRADES = {"a+", "a", "a-", "b+"}
CONFIRMED_ALLOWED_QUALITY_STATE_KEYS = {
    "high_quality_trade",
    "valid_but_lower_quality",
}
CONFIRMED_REJECTED_STATUS_KEYS = {
    "scan_error",
    "scanned_no_setup",
    "rejected_by_technical",
    "rejected_by_derivatives",
    "rejected_by_risk",
    "rejected_by_scoring",
    "rejected_by_regime",
    "failed",
    "near_miss",
    "no_setup",
    "rejected",
}
CONFIRMATION_GATED_STATES = {
    SetupLifecycleState.CONFIRMED,
    SetupLifecycleState.EXECUTING,
    SetupLifecycleState.MANAGING,
}
DECAYABLE_STATES = {
    SetupLifecycleState.WATCHLISTED,
    SetupLifecycleState.STALKING,
    SetupLifecycleState.TRIGGERED,
    SetupLifecycleState.CONFIRMED,
    SetupLifecycleState.ACTIONABLE_A_GRADE,
    SetupLifecycleState.A_GRADE_WATCH,
}
DECAY_GRADE_PATH = ("a+", "a", "a-", "b+")
ENTRY_TOUCH_MONITOR_STATES = {
    SetupLifecycleState.WATCHLISTED,
    SetupLifecycleState.STALKING,
    SetupLifecycleState.ACTIONABLE_A_GRADE,
    SetupLifecycleState.A_GRADE_WATCH,
}
ENTRY_TOUCH_REASON_REQUIRED_STATES = {
    SetupLifecycleState.WATCHLISTED,
    SetupLifecycleState.ACTIONABLE_A_GRADE,
    SetupLifecycleState.A_GRADE_WATCH,
}
PLAN_LOCK_STATES = {
    SetupLifecycleState.WATCHLISTED,
    SetupLifecycleState.STALKING,
    SetupLifecycleState.CONFIRMED,
    SetupLifecycleState.ACTIONABLE_A_GRADE,
    SetupLifecycleState.A_GRADE_WATCH,
    SetupLifecycleState.EXECUTING,
    SetupLifecycleState.MANAGING,
}
INITIAL_PUBLIC_WATCHLIST_PENDING_GATES = frozenset(
    {
        "below_min_rr",
        "challenge_rr_below_3",
        "clean_trigger_pending",
        "confirmation_pending",
        "limit_zone_hold_pending",
        "limit_zone_not_touched",
        "missing_confirmation",
        "missing_confirmation_structure_shift",
        "pullback_confirmation_pending",
        "rr_below_minimum",
        "rr_too_low",
        "waiting_for_confirmation",
    }
)

ALLOWED_TRANSITIONS: dict[SetupLifecycleState, set[SetupLifecycleState]] = {
    SetupLifecycleState.DISCOVERED: {
        SetupLifecycleState.WATCHLISTED,
        SetupLifecycleState.ACTIONABLE_A_GRADE,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.REJECTED,
    },
    SetupLifecycleState.REJECTED: {
        SetupLifecycleState.WATCHLISTED,
        SetupLifecycleState.ACTIONABLE_A_GRADE,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.ARCHIVED,
    },
    SetupLifecycleState.WATCHLISTED: {
        SetupLifecycleState.STALKING,
        SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.ACTIONABLE_A_GRADE,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.REJECTED,
        SetupLifecycleState.EXPIRED,
    },
    SetupLifecycleState.STALKING: {
        SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.WATCHLISTED,
        SetupLifecycleState.ACTIONABLE_A_GRADE,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.REJECTED,
        SetupLifecycleState.EXPIRED,
    },
    SetupLifecycleState.TRIGGERED: {
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.ACTIONABLE_A_GRADE,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.STALKING,
        SetupLifecycleState.INVALIDATED,
        SetupLifecycleState.EXPIRED,
    },
    SetupLifecycleState.CONFIRMED: {
        SetupLifecycleState.ACTIONABLE_A_GRADE,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.EXECUTING,
        SetupLifecycleState.INVALIDATED,
        SetupLifecycleState.EXPIRED,
    },
    SetupLifecycleState.A_GRADE_WATCH: {
        SetupLifecycleState.ACTIONABLE_A_GRADE,
        SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.INVALIDATED,
        SetupLifecycleState.EXPIRED,
        SetupLifecycleState.COOLDOWN,
    },
    SetupLifecycleState.ACTIONABLE_A_GRADE: {
        SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.INVALIDATED,
        SetupLifecycleState.EXPIRED,
        SetupLifecycleState.COOLDOWN,
    },
    SetupLifecycleState.EXECUTING: {SetupLifecycleState.MANAGING, SetupLifecycleState.INVALIDATED, SetupLifecycleState.EXPIRED},
    SetupLifecycleState.MANAGING: {
        SetupLifecycleState.TP_HIT,
        SetupLifecycleState.SL_HIT,
        SetupLifecycleState.INVALIDATED,
        SetupLifecycleState.EXPIRED,
    },
    SetupLifecycleState.TP_HIT: {SetupLifecycleState.COOLDOWN},
    SetupLifecycleState.SL_HIT: {SetupLifecycleState.COOLDOWN},
    SetupLifecycleState.INVALIDATED: {SetupLifecycleState.COOLDOWN},
    SetupLifecycleState.EXPIRED: {SetupLifecycleState.COOLDOWN},
    SetupLifecycleState.COOLDOWN: {SetupLifecycleState.ARCHIVED, SetupLifecycleState.WATCHLISTED},
    SetupLifecycleState.ARCHIVED: {SetupLifecycleState.WATCHLISTED},
}


@dataclass(frozen=True)
class LifecycleObservation:
    symbol: str
    mode: str = NA
    direction: str = NA
    readiness_score: int = 0
    readiness_label: str = NA
    quality_score: int = 0
    quality_grade: str = NA
    entry_low: str = NA
    entry_high: str = NA
    stop_loss: str = NA
    tp1: str = NA
    tp2: str = NA
    tp3: str = NA
    rr: str = NA
    current_price: str = NA
    latest_high: str = NA
    latest_low: str = NA
    edge_score: str = NA
    failed_gate: str = NA
    candidate_quality_grade: str = NA
    final_quality_grade: str = NA
    final_failed_gate: str = NA
    final_block_reason: str = NA
    target_integrity_status: str = NA
    target_failure: str = NA
    target_failure_severity: str = NA
    target_warning_reason: str = NA
    actionability_state: str = NA
    regime_state: str = NA
    action_label: str = NA
    confirmation_timeframe: str = NA
    invalidation_reason: str = NA
    sweep_detected: bool = False
    structure_shift_detected: bool = False
    pullback_valid: bool = False
    rr_valid: bool = False
    valid_trade_idea: bool = False
    core_status: str = NA
    setup_quality_state: str = NA
    technical_score: str = NA
    opportunity_score: str = NA
    min_technical_score: str = "50"
    min_opportunity_score: str = "80"
    active_rejection_reason: str = NA
    active_invalidation_reason: str = NA
    data_health_failed: bool = False
    limit_fill_required: bool = False
    actionable_a_grade_candidate: bool = False
    a_grade_watch_candidate: bool = False
    actionable_grade_reason: str = NA
    confirmation_block_reason: str = NA
    lifecycle_promotion_reason: str = NA
    entry_filled: bool = False
    tp_hit: bool = False
    sl_hit: bool = False
    invalidated: bool = False
    expired: bool = False
    closed_candle_outcomes_managed: bool = False

    @property
    def pullback_and_rr_valid(self) -> bool:
        return self.pullback_valid and self.rr_valid


def now_utc_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def transition_allowed(from_state: SetupLifecycleState | None, to_state: SetupLifecycleState) -> bool:
    if from_state is None:
        return True
    return to_state in ALLOWED_TRANSITIONS.get(from_state, set())


def transition_record(
    record: SetupLifecycleRecord,
    to_state: SetupLifecycleState,
    *,
    reason: SetupTransitionReason,
    now: str | None = None,
    scan_run_id: str | None = None,
    readiness_score: int | None = None,
    quality_score: int | None = None,
    failed_gate: str | None = None,
    notes: str = NA,
) -> SetupTransitionResult:
    timestamp = now or now_utc_iso()
    event_failed_gate = _transition_failed_gate(record, to_state, failed_gate)
    if record.current_state == to_state:
        updated = record.model_copy(
            update={
                "last_seen_at": timestamp,
                "readiness_score": _bounded_score(readiness_score, record.readiness_score),
                "quality_score": _bounded_score(quality_score, record.quality_score),
                "failed_gate": event_failed_gate,
            }
        )
        return SetupTransitionResult(
            lifecycle_id=record.lifecycle_id,
            symbol=record.symbol,
            from_state=record.current_state,
            to_state=to_state,
            reason=SetupTransitionReason.NO_CHANGE,
            transitioned=False,
            notes=SetupTransitionReason.NO_CHANGE.value,
            record=updated,
        )

    if not transition_allowed(record.current_state, to_state):
        return SetupTransitionResult(
            lifecycle_id=record.lifecycle_id,
            symbol=record.symbol,
            from_state=record.current_state,
            to_state=to_state,
            reason=SetupTransitionReason.INVALID_TRANSITION,
            transitioned=False,
            allowed=False,
            notes=f"{record.current_state.value} cannot move directly to {to_state.value}.",
            record=record,
        )
    if _entry_touch_transition_requires_touch_reason(record.current_state, to_state, reason):
        return SetupTransitionResult(
            lifecycle_id=record.lifecycle_id,
            symbol=record.symbol,
            from_state=record.current_state,
            to_state=to_state,
            reason=SetupTransitionReason.INVALID_TRANSITION,
            transitioned=False,
            allowed=False,
            notes=f"{record.current_state.value} can move directly to {to_state.value} only after entry zone touch.",
            record=record,
        )

    event = SetupLifecycleEvent(
        lifecycle_id=record.lifecycle_id,
        timestamp=timestamp,
        symbol=record.symbol,
        from_state=record.current_state,
        to_state=to_state,
        reason=reason,
        scan_run_id=scan_run_id,
        readiness_score=_bounded_score(readiness_score, record.readiness_score),
        quality_score=_bounded_score(quality_score, record.quality_score),
        failed_gate=event_failed_gate,
        notes=notes,
    )
    updated = record.model_copy(
        update={
            "current_state": to_state,
            "previous_state": record.current_state,
            "last_seen_at": timestamp,
            "last_transition_at": timestamp,
            "readiness_score": event.readiness_score,
            "quality_score": event.quality_score,
            "failed_gate": event.failed_gate,
            "cooldown_until": _cooldown_until(record, to_state, timestamp),
            "archived_at": timestamp if to_state == SetupLifecycleState.ARCHIVED else record.archived_at,
        }
    )
    return SetupTransitionResult(
        lifecycle_id=record.lifecycle_id,
        symbol=record.symbol,
        from_state=record.current_state,
        to_state=to_state,
        reason=reason,
        transitioned=True,
        notes=notes,
        event=event,
        record=updated,
    )


def evaluate_lifecycle_transition(
    record: SetupLifecycleRecord | None,
    observation: LifecycleObservation,
    *,
    lifecycle_id: str,
    now: str | None = None,
    scan_run_id: str | None = None,
    required_confirmation_cycles: int = DEFAULT_CONFIRMATION_CYCLES,
    setup_tolerance_pct: Decimal = DEFAULT_SETUP_MERGE_TOLERANCE_PCT,
    symbol_health_score: Any = NA,
    symbol_health_penalty_cycles: int = 0,
) -> SetupTransitionResult:
    timestamp = now or now_utc_iso()
    required_cycles = _required_confirmation_cycles(required_confirmation_cycles, symbol_health_penalty_cycles)
    if record is None:
        initial_state = initial_state_for_observation(observation)
        confirmation_count = 1 if _confirmation_countable(observation) else 0
        if _target_requires_confirmation(initial_state) and confirmation_count < required_cycles:
            initial_state = _initial_pre_confirmation_state(observation)
        quality_grade = _quality_grade_or_score(observation)
        confirmed_at = timestamp if initial_state in CONFIRMATION_GATED_STATES and confirmation_count >= required_cycles else None
        confirmed_snapshot_valid = confirmed_at is not None and _valid_confirmed_observation(observation)
        new_record = SetupLifecycleRecord(
            lifecycle_id=lifecycle_id,
            symbol=observation.symbol,
            mode=_text(observation.mode).lower() if _text(observation.mode) != NA else NA,
            direction=_text(observation.direction).lower() if _text(observation.direction) != NA else NA,
            current_state=initial_state,
            previous_state=None,
            first_seen_at=timestamp,
            last_seen_at=timestamp,
            last_transition_at=timestamp,
            failed_gate=NA if confirmed_snapshot_valid else _text(observation.failed_gate),
            candidate_quality_grade=_text(observation.candidate_quality_grade),
            final_quality_grade=_text(observation.final_quality_grade),
            technical_score=_text(observation.technical_score),
            opportunity_score=_text(observation.opportunity_score),
            final_failed_gate=_text(observation.final_failed_gate),
            final_block_reason=_text(observation.final_block_reason),
            target_integrity_status=_text(observation.target_integrity_status),
            target_failure=_text(observation.target_failure),
            target_failure_severity=_text(observation.target_failure_severity),
            target_warning_reason=_text(observation.target_warning_reason),
            actionability_state=_text(observation.actionability_state),
            readiness_score=_bounded_score(observation.readiness_score, 0),
            quality_score=_bounded_score(observation.quality_score, 0),
            edge_score=_text(observation.edge_score),
            regime_state=_text(observation.regime_state),
            action_label=_text(observation.action_label),
            invalidation_reason=_text(observation.invalidation_reason)
            if confirmed_snapshot_valid
            else _invalidation_reason(observation),
            cooldown_until=None,
            archived_at=timestamp if initial_state == SetupLifecycleState.ARCHIVED else None,
            entry_low=_text(observation.entry_low),
            entry_high=_text(observation.entry_high),
            stop_loss=_text(observation.stop_loss),
            tp1=_text(observation.tp1),
            tp2=_text(observation.tp2),
            tp3=_text(observation.tp3),
            rr=_text(observation.rr),
            invalidation_logic=_text(observation.invalidation_reason),
            confirmation_count=confirmation_count,
            required_confirmation_cycles=required_cycles,
            quality_grade_first_seen=quality_grade,
            quality_grade_current=quality_grade,
            quality_grade_confirmed=quality_grade if confirmed_snapshot_valid else NA,
            confirmed_at=confirmed_at,
            decay_count=0,
            decay_reason=NA,
            symbol_health_score_at_detection=_text(symbol_health_score),
            symbol_health_penalty_cycles=max(0, int(symbol_health_penalty_cycles or 0)),
            setup_identity=_setup_identity(observation),
        )

        geometry_failure = stored_plan_geometry_failure(new_record)
        if initial_state in ACTIVE_LIFECYCLE_MONITORING_STATES and geometry_failure is not None:
            geometry_diagnostic = f"{INVALID_STORED_PLAN_GEOMETRY}:{geometry_failure}"
            initial_state = SetupLifecycleState.REJECTED
            confirmed_snapshot_valid = False
            new_record = new_record.model_copy(
                update={
                    "current_state": initial_state,
                    "failed_gate": geometry_diagnostic,
                    "confirmed_at": None,
                    "quality_grade_confirmed": NA,
                }
            )

        event = SetupLifecycleEvent(
            lifecycle_id=lifecycle_id,
            timestamp=timestamp,
            symbol=observation.symbol,
            from_state=None,
            to_state=initial_state,
            reason=_reason_for_state(initial_state, observation, initialized=True),
            scan_run_id=scan_run_id,
            readiness_score=new_record.readiness_score,
            quality_score=new_record.quality_score,
            failed_gate=new_record.failed_gate,
            notes=_transition_notes(_reason_for_state(initial_state, observation, initialized=True), observation),
        )
        return SetupTransitionResult(
            lifecycle_id=lifecycle_id,
            symbol=observation.symbol,
            from_state=None,
            to_state=initial_state,
            reason=event.reason,
            transitioned=True,
            notes=event.notes,
            event=event,
            record=new_record,
        )

    existing_geometry_failure = stored_plan_geometry_failure(record)
    if (
        record.current_state in ACTIVE_LIFECYCLE_MONITORING_STATES
        and existing_geometry_failure is not None
    ):
        diagnostic = f"{INVALID_STORED_PLAN_GEOMETRY}:{existing_geometry_failure}"
        return SetupTransitionResult(
            lifecycle_id=record.lifecycle_id,
            symbol=record.symbol,
            from_state=record.current_state,
            to_state=record.current_state,
            reason=SetupTransitionReason.INVALID_TRANSITION,
            transitioned=False,
            allowed=False,
            notes=diagnostic,
            record=record,
        )

    if (
        observation.closed_candle_outcomes_managed
        and record.current_state in {SetupLifecycleState.TP_HIT, SetupLifecycleState.SL_HIT}
    ):
        return transition_record(
            record,
            record.current_state,
            reason=SetupTransitionReason.NO_CHANGE,
            now=timestamp,
            scan_run_id=scan_run_id,
            readiness_score=record.readiness_score,
            quality_score=record.quality_score,
            failed_gate=record.failed_gate,
            notes="Canonical terminal lifecycle state is immutable.",
        )

    if not observation.closed_candle_outcomes_managed and _stored_monitoring_entry_zone_touched(record, observation):
        observation = _observation_with_stored_plan(observation, record, entry_filled=True)

    updated_record = _record_with_observation(
        record,
        observation,
        timestamp,
        setup_tolerance_pct=setup_tolerance_pct,
        required_confirmation_cycles=required_cycles,
        symbol_health_penalty_cycles=symbol_health_penalty_cycles,
    )
    next_state = next_state_for_observation(updated_record, observation, timestamp)
    geometry_diagnostic = NA
    geometry_failure = stored_plan_geometry_failure(updated_record)
    if next_state in ACTIVE_LIFECYCLE_MONITORING_STATES and geometry_failure is not None:
        geometry_diagnostic = f"{INVALID_STORED_PLAN_GEOMETRY}:{geometry_failure}"
        if transition_allowed(updated_record.current_state, SetupLifecycleState.REJECTED):
            next_state = SetupLifecycleState.REJECTED
        else:
            next_state = updated_record.current_state
        updated_record = updated_record.model_copy(
            update={"failed_gate": geometry_diagnostic}
        )

    next_state, updated_record, decay_reason = _apply_decay_if_needed(
        record,
        updated_record,
        observation,
        next_state,
    )
    reason = _reason_for_state(next_state, observation)
    if geometry_diagnostic != NA and next_state == updated_record.current_state:
        reason = SetupTransitionReason.INVALID_TRANSITION
    if decay_reason != NA:
        reason = SetupTransitionReason.SETUP_EXPIRED if next_state == SetupLifecycleState.EXPIRED else SetupTransitionReason.SETUP_DECAYED
    elif (
        next_state in CONFIRMATION_GATED_STATES
        and updated_record.confirmation_count >= updated_record.required_confirmation_cycles
        and record.confirmation_count < updated_record.required_confirmation_cycles
        and next_state == SetupLifecycleState.CONFIRMED
    ):
        reason = SetupTransitionReason.MULTI_SCAN_CONFIRMED
    return transition_record(
        updated_record,
        next_state,
        reason=reason,
        now=timestamp,
        scan_run_id=scan_run_id,
        readiness_score=observation.readiness_score,
        quality_score=observation.quality_score,
        failed_gate=(
            geometry_diagnostic
            if geometry_diagnostic != NA
            else updated_record.failed_gate
            if decay_reason != NA
            else observation.failed_gate
        ),
        notes=(
            geometry_diagnostic
            if geometry_diagnostic != NA
            else _transition_notes(reason, observation)
            if next_state != updated_record.current_state
            else SetupTransitionReason.NO_CHANGE.value
        ),
    )


def initial_state_for_observation(observation: LifecycleObservation) -> SetupLifecycleState:
    target = observed_state(observation)
    if target == SetupLifecycleState.ACTIONABLE_A_GRADE:
        return SetupLifecycleState.ACTIONABLE_A_GRADE
    if target == SetupLifecycleState.A_GRADE_WATCH:
        return SetupLifecycleState.A_GRADE_WATCH
    if target in {SetupLifecycleState.EXECUTING, SetupLifecycleState.MANAGING}:
        return SetupLifecycleState.CONFIRMED
    if target == SetupLifecycleState.TRIGGERED and _initial_trigger_blocked(observation):
        return _initial_non_trigger_state(observation)
    return target


def observed_state(observation: LifecycleObservation) -> SetupLifecycleState:
    if observation.tp_hit:
        return SetupLifecycleState.TP_HIT
    if observation.sl_hit:
        return SetupLifecycleState.SL_HIT
    if observation.invalidated:
        return SetupLifecycleState.INVALIDATED
    if _text(observation.active_invalidation_reason) != NA:
        return SetupLifecycleState.INVALIDATED
    if observation.expired:
        return SetupLifecycleState.EXPIRED
    if _confirmed_observation_ready(observation):
        return SetupLifecycleState.CONFIRMED
    if observation.actionable_a_grade_candidate:
        return SetupLifecycleState.ACTIONABLE_A_GRADE
    if observation.a_grade_watch_candidate:
        return SetupLifecycleState.A_GRADE_WATCH
    if observation.sweep_detected and observation.structure_shift_detected:
        return SetupLifecycleState.TRIGGERED
    if observation.sweep_detected:
        return SetupLifecycleState.STALKING
    if _is_watch_ready(observation):
        return SetupLifecycleState.WATCHLISTED
    if _text(observation.failed_gate) != NA:
        return SetupLifecycleState.REJECTED
    return SetupLifecycleState.DISCOVERED


def next_state_for_observation(
    record: SetupLifecycleRecord,
    observation: LifecycleObservation,
    timestamp: str,
) -> SetupLifecycleState:
    current = record.current_state
    target = observed_state(observation)
    target = _confirmation_gated_target(record, observation, target)

    if current == SetupLifecycleState.COOLDOWN:
        if _cooldown_expired(record.cooldown_until, timestamp):
            if target in WATCH_PRIORITY_STATES or target in VALID_STATES:
                return SetupLifecycleState.WATCHLISTED
            return SetupLifecycleState.ARCHIVED
        return current
    if current == SetupLifecycleState.ARCHIVED:
        if target in WATCH_PRIORITY_STATES or target in VALID_STATES:
            return SetupLifecycleState.WATCHLISTED
        return current
    if current in OUTCOME_STATES:
        return SetupLifecycleState.COOLDOWN
    if current == SetupLifecycleState.MANAGING:
        if observation.tp_hit:
            return SetupLifecycleState.TP_HIT
        if observation.sl_hit:
            return SetupLifecycleState.SL_HIT
        if observation.invalidated:
            return SetupLifecycleState.INVALIDATED
        if observation.expired:
            return SetupLifecycleState.EXPIRED
        return current
    if current == SetupLifecycleState.EXECUTING:
        if observation.invalidated:
            return SetupLifecycleState.INVALIDATED
        if observation.expired:
            return SetupLifecycleState.EXPIRED
        if observation.entry_filled:
            return SetupLifecycleState.MANAGING
        return current
    if current == SetupLifecycleState.CONFIRMED:
        if (
            observation.invalidated
            or _active_setup_invalidated(current, target)
            or _confirmed_observation_invalidates_active_signal(observation)
        ):
            return SetupLifecycleState.INVALIDATED
        if observation.expired:
            return SetupLifecycleState.EXPIRED
        if observation.actionable_a_grade_candidate and not observation.entry_filled:
            return SetupLifecycleState.ACTIONABLE_A_GRADE
        if observation.a_grade_watch_candidate and not observation.entry_filled:
            return SetupLifecycleState.A_GRADE_WATCH
        if observation.entry_filled:
            return SetupLifecycleState.EXECUTING
        if observation.valid_trade_idea:
            if observation.limit_fill_required:
                return current
            return SetupLifecycleState.EXECUTING
        return current
    if current == SetupLifecycleState.A_GRADE_WATCH:
        if observation.invalidated or target == SetupLifecycleState.REJECTED:
            return SetupLifecycleState.INVALIDATED
        if observation.expired:
            return SetupLifecycleState.EXPIRED
        if target == SetupLifecycleState.ACTIONABLE_A_GRADE:
            return SetupLifecycleState.ACTIONABLE_A_GRADE
        if observation.entry_filled:
            return SetupLifecycleState.TRIGGERED
        if target == SetupLifecycleState.CONFIRMED:
            return SetupLifecycleState.CONFIRMED
        return current
    if current == SetupLifecycleState.ACTIONABLE_A_GRADE:
        if observation.invalidated or target == SetupLifecycleState.REJECTED:
            return SetupLifecycleState.INVALIDATED
        if observation.expired:
            return SetupLifecycleState.EXPIRED
        if observation.entry_filled:
            return SetupLifecycleState.TRIGGERED
        if target == SetupLifecycleState.CONFIRMED:
            return SetupLifecycleState.CONFIRMED
        return current
    if current == SetupLifecycleState.REJECTED:
        if target == SetupLifecycleState.ACTIONABLE_A_GRADE:
            return SetupLifecycleState.ACTIONABLE_A_GRADE
        if target == SetupLifecycleState.A_GRADE_WATCH:
            return SetupLifecycleState.A_GRADE_WATCH
        if target in WATCH_PRIORITY_STATES or target in VALID_STATES:
            return SetupLifecycleState.WATCHLISTED if observation.readiness_score > record.readiness_score else current
        return current
    if current == SetupLifecycleState.DISCOVERED:
        if target == SetupLifecycleState.REJECTED:
            return SetupLifecycleState.REJECTED
        if target == SetupLifecycleState.ACTIONABLE_A_GRADE:
            return SetupLifecycleState.ACTIONABLE_A_GRADE
        if target == SetupLifecycleState.A_GRADE_WATCH:
            return SetupLifecycleState.A_GRADE_WATCH
        if target in WATCH_PRIORITY_STATES or target in VALID_STATES:
            return SetupLifecycleState.WATCHLISTED
        return current
    if current == SetupLifecycleState.WATCHLISTED:
        if observation.entry_filled and not observation.invalidated and not observation.expired and target != SetupLifecycleState.REJECTED:
            return SetupLifecycleState.TRIGGERED
        if target == SetupLifecycleState.REJECTED:
            return SetupLifecycleState.REJECTED
        if target == SetupLifecycleState.ACTIONABLE_A_GRADE:
            return SetupLifecycleState.ACTIONABLE_A_GRADE
        if target == SetupLifecycleState.A_GRADE_WATCH:
            return SetupLifecycleState.A_GRADE_WATCH
        if observation.sweep_detected:
            return SetupLifecycleState.STALKING
        return current
    if current == SetupLifecycleState.STALKING:
        if observation.entry_filled and not observation.invalidated and not observation.expired and target != SetupLifecycleState.REJECTED:
            return SetupLifecycleState.TRIGGERED
        if target == SetupLifecycleState.REJECTED:
            return SetupLifecycleState.REJECTED
        if target == SetupLifecycleState.ACTIONABLE_A_GRADE:
            return SetupLifecycleState.ACTIONABLE_A_GRADE
        if target == SetupLifecycleState.A_GRADE_WATCH:
            return SetupLifecycleState.A_GRADE_WATCH
        if observation.sweep_detected and observation.structure_shift_detected:
            return SetupLifecycleState.TRIGGERED
        if not observation.sweep_detected and target == SetupLifecycleState.WATCHLISTED:
            return SetupLifecycleState.WATCHLISTED
        return current
    if current == SetupLifecycleState.TRIGGERED:
        if observation.invalidated or target == SetupLifecycleState.REJECTED:
            return SetupLifecycleState.INVALIDATED
        if target == SetupLifecycleState.ACTIONABLE_A_GRADE:
            return SetupLifecycleState.ACTIONABLE_A_GRADE
        if target == SetupLifecycleState.A_GRADE_WATCH:
            return SetupLifecycleState.A_GRADE_WATCH
        if observation.pullback_and_rr_valid or observation.valid_trade_idea:
            return SetupLifecycleState.CONFIRMED if target == SetupLifecycleState.CONFIRMED else target
        if observation.sweep_detected and not observation.structure_shift_detected:
            return SetupLifecycleState.STALKING
        return current
    return current


def is_watchable_lifecycle_state(record: SetupLifecycleRecord, *, now: str | None = None) -> bool:
    if record.current_state == SetupLifecycleState.ARCHIVED:
        return False
    if record.current_state == SetupLifecycleState.COOLDOWN:
        return False
    return record.current_state in WATCH_PRIORITY_STATES


def entry_zone_touched(
    latest_high: Any,
    latest_low: Any,
    entry_low: Any,
    entry_high: Any,
) -> bool:
    """Return True when the latest price range overlaps the planned entry zone."""

    candle_high = _decimal_or_none(latest_high)
    candle_low = _decimal_or_none(latest_low)
    zone_low = _decimal_or_none(entry_low)
    zone_high = _decimal_or_none(entry_high)
    if candle_high is None or candle_low is None or zone_low is None or zone_high is None:
        return False

    latest_range_low = min(candle_high, candle_low)
    latest_range_high = max(candle_high, candle_low)
    entry_range_low = min(zone_low, zone_high)
    entry_range_high = max(zone_low, zone_high)
    return latest_range_high >= entry_range_low and latest_range_low <= entry_range_high


def _stored_monitoring_entry_zone_touched(
    record: SetupLifecycleRecord,
    observation: LifecycleObservation,
) -> bool:
    if record.current_state not in ENTRY_TOUCH_MONITOR_STATES:
        return False
    if _terminal_observation(observation):
        return False
    side = _status_key(observation.direction)
    if side not in {"long", "short"}:
        side = _status_key(record.direction)
    if side not in {"long", "short"}:
        return False
    if _decimal_or_none(observation.current_price) is not None:
        return _price_in_zone(observation.current_price, record.entry_low, record.entry_high)
    return entry_zone_touched(observation.latest_high, observation.latest_low, record.entry_low, record.entry_high)


def _price_in_zone(price: Any, entry_low: Any, entry_high: Any) -> bool:
    current = _decimal_or_none(price)
    zone_low = _decimal_or_none(entry_low)
    zone_high = _decimal_or_none(entry_high)
    if current is None or zone_low is None or zone_high is None:
        return False
    return min(zone_low, zone_high) <= current <= max(zone_low, zone_high)


def _observation_with_stored_plan(
    observation: LifecycleObservation,
    record: SetupLifecycleRecord,
    *,
    entry_filled: bool,
) -> LifecycleObservation:
    return replace(
        observation,
        entry_low=record.entry_low,
        entry_high=record.entry_high,
        stop_loss=record.stop_loss,
        tp1=record.tp1,
        tp2=record.tp2,
        tp3=record.tp3,
        rr=record.rr,
        invalidation_reason=record.invalidation_reason,
        entry_filled=entry_filled,
        limit_fill_required=True,
    )


def _record_with_observation(
    record: SetupLifecycleRecord,
    observation: LifecycleObservation,
    timestamp: str,
    *,
    setup_tolerance_pct: Decimal,
    required_confirmation_cycles: int,
    symbol_health_penalty_cycles: int,
) -> SetupLifecycleRecord:
    consistent = _setup_consistent(record, observation, setup_tolerance_pct)
    confirmation_count = _next_confirmation_count(
        record,
        observation,
        consistent=consistent,
        required_confirmation_cycles=required_confirmation_cycles,
    )
    quality_grade = _quality_grade_or_score(observation)
    current_grade = _current_quality_grade(record, quality_grade)
    confirmed_at = record.confirmed_at
    quality_grade_confirmed = record.quality_grade_confirmed
    confirmed_snapshot_valid = _valid_confirmed_observation(observation)
    if (
        confirmation_count >= required_confirmation_cycles
        and record.confirmation_count < required_confirmation_cycles
        and confirmed_snapshot_valid
    ):
        confirmed_at = timestamp
        quality_grade_confirmed = current_grade
    return record.model_copy(
        update={
            "last_seen_at": timestamp,
            "failed_gate": _text(observation.failed_gate),
            "candidate_quality_grade": _text(observation.candidate_quality_grade),
            "final_quality_grade": _text(observation.final_quality_grade),
            "technical_score": _text(observation.technical_score),
            "opportunity_score": _text(observation.opportunity_score),
            "final_failed_gate": _text(observation.final_failed_gate),
            "final_block_reason": _text(observation.final_block_reason),
            "target_integrity_status": _text(observation.target_integrity_status),
            "target_failure": _text(observation.target_failure),
            "target_failure_severity": _text(observation.target_failure_severity),
            "target_warning_reason": _text(observation.target_warning_reason),
            "actionability_state": _text(observation.actionability_state),
            "readiness_score": _bounded_score(observation.readiness_score, record.readiness_score),
            "quality_score": _bounded_score(observation.quality_score, record.quality_score),
            "edge_score": _text(observation.edge_score),
            "regime_state": _text(observation.regime_state),
            "action_label": _text(observation.action_label),
            "invalidation_reason": _confirmed_invalidation_value(
                record,
                observation,
                confirmed_snapshot_valid=confirmed_snapshot_valid,
            ),
            "entry_low": _plan_or_observed_value(record, record.entry_low, observation.entry_low),
            "entry_high": _plan_or_observed_value(record, record.entry_high, observation.entry_high),
            "stop_loss": _plan_or_observed_value(record, record.stop_loss, observation.stop_loss),
            "tp1": _plan_or_observed_value(record, record.tp1, observation.tp1),
            "tp2": _plan_or_observed_value(record, record.tp2, observation.tp2),
            "tp3": _plan_or_observed_value(record, record.tp3, observation.tp3),
            "rr": _plan_or_observed_value(record, record.rr, observation.rr),
            "invalidation_logic": _confirmed_invalidation_logic(
                record,
                observation,
                confirmed_snapshot_valid=confirmed_snapshot_valid,
            ),
            "confirmation_count": confirmation_count,
            "required_confirmation_cycles": required_confirmation_cycles,
            "quality_grade_current": current_grade,
            "quality_grade_confirmed": quality_grade_confirmed,
            "confirmed_at": confirmed_at,
            "symbol_health_penalty_cycles": max(
                record.symbol_health_penalty_cycles,
                max(0, int(symbol_health_penalty_cycles or 0)),
            ),
            "setup_identity": (
                record.setup_identity
                if record.current_state in PLAN_LOCK_STATES
                else _setup_identity(observation)
            ),
        }
    )


def _required_confirmation_cycles(base_cycles: int, symbol_health_penalty_cycles: int) -> int:
    try:
        base = int(base_cycles)
    except (TypeError, ValueError):
        base = DEFAULT_CONFIRMATION_CYCLES
    try:
        penalty = int(symbol_health_penalty_cycles or 0)
    except (TypeError, ValueError):
        penalty = 0
    return max(1, base) + max(0, penalty)


def _confirmation_gated_target(
    record: SetupLifecycleRecord,
    observation: LifecycleObservation,
    target: SetupLifecycleState,
) -> SetupLifecycleState:
    if not _target_requires_confirmation(target):
        return target
    if record.confirmation_count >= record.required_confirmation_cycles and _confirmed_observation_ready(observation):
        return target
    return _pre_confirmation_state(observation)


def _target_requires_confirmation(state: SetupLifecycleState) -> bool:
    return state in CONFIRMATION_GATED_STATES


def _pre_confirmation_state(observation: LifecycleObservation) -> SetupLifecycleState:
    if observation.sweep_detected and observation.structure_shift_detected:
        return SetupLifecycleState.TRIGGERED
    if observation.sweep_detected:
        return SetupLifecycleState.STALKING
    if _is_watch_ready(observation):
        return SetupLifecycleState.WATCHLISTED
    return SetupLifecycleState.DISCOVERED


def _initial_pre_confirmation_state(observation: LifecycleObservation) -> SetupLifecycleState:
    state = _pre_confirmation_state(observation)
    if state == SetupLifecycleState.TRIGGERED and _initial_trigger_blocked(observation):
        return _initial_non_trigger_state(observation)
    return state


def _initial_trigger_blocked(observation: LifecycleObservation) -> bool:
    if observation.entry_filled:
        return False
    if _text(observation.failed_gate) == NA and _confirmation_countable(observation):
        return False
    if _text(observation.failed_gate) != NA:
        return True
    if _decimal_or_none(observation.rr) is None:
        return True
    if _initial_reject_quality(observation):
        return True
    if not _initial_complete_trade_map(observation):
        return True
    if not (observation.rr_valid or observation.valid_trade_idea or observation.pullback_and_rr_valid):
        return True
    return False


def _initial_non_trigger_state(observation: LifecycleObservation) -> SetupLifecycleState:
    if observation.invalidated:
        return SetupLifecycleState.INVALIDATED
    if observation.expired:
        return SetupLifecycleState.EXPIRED
    if _initial_public_watchlist_candidate(observation):
        return SetupLifecycleState.STALKING if observation.sweep_detected else SetupLifecycleState.WATCHLISTED
    if _text(observation.failed_gate) != NA or _initial_incomplete_or_rejected_plan(observation):
        return SetupLifecycleState.REJECTED
    if observation.sweep_detected:
        return SetupLifecycleState.STALKING
    if _is_watch_ready(observation):
        return SetupLifecycleState.WATCHLISTED
    return SetupLifecycleState.DISCOVERED


def _initial_public_watchlist_candidate(observation: LifecycleObservation) -> bool:
    failed_gate = _status_key(observation.failed_gate)
    if failed_gate and failed_gate not in INITIAL_PUBLIC_WATCHLIST_PENDING_GATES:
        return False
    if _status_key(observation.direction) not in {"long", "short"}:
        return False
    if not _initial_complete_trade_map(observation):
        return False
    rr = _decimal_or_none(observation.rr)
    if rr is None or rr < PUBLIC_WATCHLIST_MIN_RR:
        return False
    if not _quality_at_least_b_plus(observation.quality_grade, observation.quality_score):
        return False
    return _text(observation.invalidation_reason) != NA


def _initial_complete_trade_map(observation: LifecycleObservation) -> bool:
    return all(
        _decimal_or_none(value) is not None
        for value in (observation.entry_low, observation.entry_high, observation.stop_loss, observation.tp1)
    )


def _initial_incomplete_or_rejected_plan(observation: LifecycleObservation) -> bool:
    return (
        _decimal_or_none(observation.rr) is None
        or _initial_reject_quality(observation)
        or not _initial_complete_trade_map(observation)
    )


def _initial_reject_quality(observation: LifecycleObservation) -> bool:
    return not _quality_at_least_b_plus(observation.quality_grade, observation.quality_score)


def _next_confirmation_count(
    record: SetupLifecycleRecord,
    observation: LifecycleObservation,
    *,
    consistent: bool,
    required_confirmation_cycles: int,
) -> int:
    if _terminal_observation(observation):
        return 0
    if not _confirmation_countable(observation):
        return record.confirmation_count if _setup_observable(observation) and consistent else 0
    if consistent:
        return min(record.confirmation_count + 1, max(1, required_confirmation_cycles))
    return 1


def _confirmation_countable(observation: LifecycleObservation) -> bool:
    return (
        _confirmed_observation_ready(observation)
        or observation.actionable_a_grade_candidate
        or observation.a_grade_watch_candidate
    )


def _valid_confirmed_observation(observation: LifecycleObservation) -> bool:
    return _confirmed_observation_ready(observation) and not _terminal_observation(observation)


def _confirmed_observation_ready(observation: LifecycleObservation) -> bool:
    return not _confirmed_observation_blockers(observation)


def confirmed_observation_block_reasons(observation: LifecycleObservation) -> tuple[str, ...]:
    return _confirmed_observation_blockers(observation)


def _confirmed_observation_invalidates_active_signal(observation: LifecycleObservation) -> bool:
    blockers = _confirmed_observation_blockers(observation)
    active_blockers = {
        "active_rejection_reason",
        "active_invalidation",
        "core_status_blocked",
        "failed_confirmation_gate",
        "data_health_failed",
    }
    return any(blocker.split(":", 1)[0] in active_blockers for blocker in blockers)


def _confirmed_observation_blockers(observation: LifecycleObservation) -> tuple[str, ...]:
    blockers: list[str] = []

    core_status = _status_key(observation.core_status)
    if core_status in CONFIRMED_REJECTED_STATUS_KEYS:
        blockers.append(f"core_status_blocked:{core_status}")

    failed_gate = _status_key(observation.failed_gate)
    if failed_gate:
        blockers.append(f"failed_confirmation_gate:{failed_gate}")

    if _text(observation.active_rejection_reason) != NA:
        blockers.append("active_rejection_reason")
    if _text(observation.active_invalidation_reason) != NA:
        blockers.append("active_invalidation")
    if observation.data_health_failed:
        blockers.append("data_health_failed")

    if not observation.valid_trade_idea:
        blockers.append("trade_idea_missing")

    if _status_key(observation.direction) not in {"long", "short"}:
        blockers.append("missing_side")
    if _decimal_or_none(observation.entry_low) is None or _decimal_or_none(observation.entry_high) is None:
        blockers.append("missing_entry_zone")
    if _decimal_or_none(observation.stop_loss) is None:
        blockers.append("missing_stop")
    if _text(observation.invalidation_reason) == NA:
        blockers.append("missing_invalidation")

    rr_value = _decimal_or_none(observation.rr)
    if rr_value is None:
        blockers.append("rr_missing")
    elif rr_value < CONFIRMED_MIN_RR:
        blockers.append(f"rr_below_min:{_text(rr_value)}<{_text(CONFIRMED_MIN_RR)}")

    if not _quality_at_least_b_plus(observation.quality_grade, observation.quality_score):
        blockers.append("confirmed_grade_below_min")

    quality_state = _status_key(observation.setup_quality_state)
    if quality_state and quality_state not in CONFIRMED_ALLOWED_QUALITY_STATE_KEYS:
        blockers.append(f"setup_quality_not_confirmed:{quality_state}")
        if quality_state == "watchlist_near_miss":
            blockers.append("watchlist_near_miss_not_confirmed")

    technical_score = _decimal_or_none(observation.technical_score)
    min_technical = _decimal_or_none(observation.min_technical_score) or MIN_TECHNICAL_SCORE
    if technical_score is None:
        blockers.append("technical_score_missing")
    elif technical_score < min_technical:
        blockers.append(f"technical_score_below_min:{_text(technical_score)}<{_text(min_technical)}")

    opportunity_score = _decimal_or_none(observation.opportunity_score)
    min_opportunity = _decimal_or_none(observation.min_opportunity_score) or DEFAULT_MIN_OPPORTUNITY_SCORE
    if opportunity_score is None:
        blockers.append("opportunity_score_missing")
    elif opportunity_score < min_opportunity:
        blockers.append(f"opportunity_score_below_min:{_text(opportunity_score)}<{_text(min_opportunity)}")

    return tuple(dict.fromkeys(blockers))


def _setup_observable(observation: LifecycleObservation) -> bool:
    if (
        observation.valid_trade_idea
        or observation.pullback_and_rr_valid
        or observation.actionable_a_grade_candidate
        or observation.a_grade_watch_candidate
    ):
        return True
    return observation.sweep_detected or _is_watch_ready(observation)


def _terminal_observation(observation: LifecycleObservation) -> bool:
    return observation.tp_hit or observation.sl_hit or observation.invalidated or observation.expired


def _plan_or_observed_value(record: SetupLifecycleRecord, stored: Any, observed: Any) -> str:
    if record.current_state in PLAN_LOCK_STATES:
        stored_text = _text(stored)
        if stored_text != NA:
            return stored_text
    return _text(observed)


def _confirmed_invalidation_value(
    record: SetupLifecycleRecord,
    observation: LifecycleObservation,
    *,
    confirmed_snapshot_valid: bool,
) -> str:
    if confirmed_snapshot_valid and _stale_rejection_text(record.invalidation_reason, record.failed_gate):
        return _text(observation.invalidation_reason)
    return _plan_or_observed_value(record, record.invalidation_reason, _invalidation_reason(observation))


def _confirmed_invalidation_logic(
    record: SetupLifecycleRecord,
    observation: LifecycleObservation,
    *,
    confirmed_snapshot_valid: bool,
) -> str:
    if confirmed_snapshot_valid and _stale_rejection_text(record.invalidation_logic, record.failed_gate):
        return _text(observation.invalidation_reason)
    return _plan_or_observed_value(record, record.invalidation_logic, observation.invalidation_reason)


def _transition_failed_gate(
    record: SetupLifecycleRecord,
    to_state: SetupLifecycleState,
    failed_gate: str | None,
) -> str:
    if to_state == SetupLifecycleState.CONFIRMED and _text(failed_gate) == NA:
        return NA
    return _text_or(failed_gate, record.failed_gate)


def _stale_rejection_text(value: Any, failed_gate: Any = NA) -> bool:
    text = _text(value)
    if text == NA:
        return False
    failed_gate_text = _text(failed_gate)
    if failed_gate_text != NA and _status_key(text) == _status_key(failed_gate_text):
        return True
    lowered = text.lower()
    return any(
        fragment in lowered
        for fragment in (
            "technical score",
            "opportunity score",
            "scanner minimum",
            "below scanner",
            "below minimum",
            "rr below",
            "risk/reward below",
            "missing required",
            "missing field",
            "no valid setup",
            "no setup",
            "near miss",
            "rejected setup",
            "setup rejected",
            "failed gate",
            "gate failed",
            "hard rejection",
        )
    )


def _entry_touch_transition_requires_touch_reason(
    from_state: SetupLifecycleState | None,
    to_state: SetupLifecycleState,
    reason: SetupTransitionReason,
) -> bool:
    return (
        from_state in ENTRY_TOUCH_REASON_REQUIRED_STATES
        and to_state == SetupLifecycleState.TRIGGERED
        and reason != SetupTransitionReason.ENTRY_ZONE_TOUCHED
    )


def _setup_consistent(
    record: SetupLifecycleRecord,
    observation: LifecycleObservation,
    tolerance_pct: Decimal,
) -> bool:
    if record.symbol != observation.symbol.upper():
        return False
    if _identity_text(record.mode) != _identity_text(observation.mode):
        return False
    if _identity_text(record.direction) != _identity_text(observation.direction):
        return False
    if not _price_similar(record.entry_low, observation.entry_low, tolerance_pct):
        return False
    if not _price_similar(record.entry_high, observation.entry_high, tolerance_pct):
        return False
    if not _price_similar(record.stop_loss, observation.stop_loss, tolerance_pct):
        return False
    return _logic_similar(record.invalidation_logic or record.invalidation_reason, observation.invalidation_reason)


def _price_similar(left: Any, right: Any, tolerance_pct: Decimal) -> bool:
    left_decimal = _decimal_or_none(left)
    right_decimal = _decimal_or_none(right)
    if left_decimal is None or right_decimal is None:
        return True
    reference = max(abs(left_decimal), abs(right_decimal), Decimal("1"))
    tolerance = reference * max(tolerance_pct, Decimal("0")) / Decimal("100")
    return abs(left_decimal - right_decimal) <= tolerance


def _logic_similar(left: Any, right: Any) -> bool:
    left_key = _status_key(left)
    right_key = _status_key(right)
    if not left_key or not right_key:
        return True
    if left_key == right_key:
        return True
    return _numeric_normalized_logic(left_key) == _numeric_normalized_logic(right_key)


def _numeric_normalized_logic(value: str) -> str:
    return re.sub(r"\d+(?:\.\d+)?", "#", value)


def _quality_at_least_b_plus(grade: Any, score: int) -> bool:
    key = _grade_key(grade)
    if key in MIN_CONFIRMATION_GRADES:
        return True
    if key:
        return False
    return _bounded_score(score, 0) >= 75


def _quality_grade_or_score(observation: LifecycleObservation) -> str:
    grade = _text(observation.quality_grade)
    if grade != NA:
        return grade
    score = _bounded_score(observation.quality_score, 0)
    if score >= 90:
        return "A+"
    if score >= 85:
        return "A"
    if score >= 80:
        return "A-"
    if score >= 75:
        return "B+"
    if score >= 65:
        return "B"
    if score >= 55:
        return "B-"
    return "Reject"


def _current_quality_grade(record: SetupLifecycleRecord, observed_grade: str) -> str:
    if record.decay_count > 0 and _text(record.quality_grade_current) != NA:
        return record.quality_grade_current
    return observed_grade


def _apply_decay_if_needed(
    previous: SetupLifecycleRecord,
    updated: SetupLifecycleRecord,
    observation: LifecycleObservation,
    next_state: SetupLifecycleState,
) -> tuple[SetupLifecycleState, SetupLifecycleRecord, str]:
    if next_state != previous.current_state or next_state not in DECAYABLE_STATES:
        return next_state, updated, NA
    if updated.confirmation_count > previous.confirmation_count:
        return next_state, updated, NA
    if updated.setup_identity != previous.setup_identity:
        return next_state, updated, NA
    if observation.entry_filled or _terminal_observation(observation):
        return next_state, updated, NA

    next_grade = _decayed_grade(updated.quality_grade_current)
    if next_grade == NA:
        return next_state, updated, NA
    reason = "no price reaction or lifecycle progress"
    update = {
        "decay_count": updated.decay_count + 1,
        "decay_reason": reason,
    }
    if next_grade == "EXPIRED":
        update["quality_grade_current"] = "Expired"
        update["failed_gate"] = "confidence_decay"
        update["invalidation_reason"] = reason
        return SetupLifecycleState.EXPIRED, updated.model_copy(update=update), reason
    update["quality_grade_current"] = next_grade
    return next_state, updated.model_copy(update=update), reason


def _decayed_grade(value: Any) -> str:
    key = _grade_key(value)
    if key not in DECAY_GRADE_PATH:
        return NA
    index = DECAY_GRADE_PATH.index(key)
    if index + 1 >= len(DECAY_GRADE_PATH):
        return "EXPIRED"
    return _grade_label(DECAY_GRADE_PATH[index + 1])


def _grade_key(value: Any) -> str:
    return _text(value).lower().replace(" ", "")


def _grade_label(key: str) -> str:
    return {"a+": "A+", "a": "A", "a-": "A-", "b+": "B+"}.get(key, NA)


def _setup_identity(observation: LifecycleObservation) -> str:
    return "|".join(
        _text(value)
        for value in (
            observation.symbol.upper(),
            _identity_text(observation.mode),
            _identity_text(observation.direction),
            observation.entry_low,
            observation.entry_high,
            observation.stop_loss,
            observation.invalidation_reason,
        )
    )


def _identity_text(value: Any) -> str:
    text = _text(value).lower()
    return text if text != NA.lower() else NA


def _transition_notes(reason: SetupTransitionReason, observation: LifecycleObservation) -> str:
    timeframe = _text(observation.confirmation_timeframe)
    if reason == SetupTransitionReason.STRUCTURE_SHIFT_CONFIRMED and timeframe != NA:
        return f"{timeframe} BOS/CHoCH confirmed after sweep."
    return reason.value


def _reason_for_state(
    state: SetupLifecycleState,
    observation: LifecycleObservation,
    *,
    initialized: bool = False,
) -> SetupTransitionReason:
    if initialized:
        return SetupTransitionReason.INITIALIZED
    if state == SetupLifecycleState.DISCOVERED:
        return SetupTransitionReason.DISCOVERED
    if state == SetupLifecycleState.REJECTED:
        return SetupTransitionReason.REJECTED
    if state == SetupLifecycleState.WATCHLISTED:
        return SetupTransitionReason.READINESS_IMPROVED
    if state == SetupLifecycleState.STALKING:
        return SetupTransitionReason.SWEEP_APPEARED
    if state == SetupLifecycleState.TRIGGERED:
        if observation.entry_filled:
            return SetupTransitionReason.ENTRY_ZONE_TOUCHED
        return SetupTransitionReason.STRUCTURE_SHIFT_CONFIRMED
    if state == SetupLifecycleState.CONFIRMED:
        return SetupTransitionReason.PULLBACK_RR_VALID
    if state == SetupLifecycleState.ACTIONABLE_A_GRADE:
        return SetupTransitionReason.ACTIONABLE_A_GRADE
    if state == SetupLifecycleState.A_GRADE_WATCH:
        return SetupTransitionReason.A_GRADE_WATCH
    if state == SetupLifecycleState.EXECUTING:
        if observation.entry_filled:
            return SetupTransitionReason.ENTRY_ZONE_TOUCHED
        return SetupTransitionReason.VALID_TRADE_IDEA
    if state == SetupLifecycleState.MANAGING:
        return SetupTransitionReason.ENTRY_FILL_SIMULATED
    if state == SetupLifecycleState.TP_HIT:
        return SetupTransitionReason.TAKE_PROFIT_HIT
    if state == SetupLifecycleState.SL_HIT:
        return SetupTransitionReason.STOP_LOSS_HIT
    if state == SetupLifecycleState.INVALIDATED:
        return SetupTransitionReason.SETUP_INVALIDATED
    if state == SetupLifecycleState.EXPIRED:
        return SetupTransitionReason.SETUP_EXPIRED
    if state == SetupLifecycleState.COOLDOWN:
        return SetupTransitionReason.COOLDOWN_STARTED
    if state == SetupLifecycleState.ARCHIVED:
        return SetupTransitionReason.COOLDOWN_EXPIRED
    return SetupTransitionReason.NO_CHANGE


def _is_watch_ready(observation: LifecycleObservation) -> bool:
    label = _text(observation.readiness_label).upper()
    return label in {"WATCH", "HOT WATCH", "VALID SETUP"} or observation.readiness_score >= 50


def _active_setup_invalidated(current: SetupLifecycleState, target: SetupLifecycleState) -> bool:
    return (
        current
        in {
            SetupLifecycleState.CONFIRMED,
            SetupLifecycleState.ACTIONABLE_A_GRADE,
            SetupLifecycleState.A_GRADE_WATCH,
            SetupLifecycleState.EXECUTING,
        }
        and target == SetupLifecycleState.REJECTED
    )


def _cooldown_until(record: SetupLifecycleRecord, to_state: SetupLifecycleState, timestamp: str) -> str | None:
    if to_state != SetupLifecycleState.COOLDOWN:
        if to_state == SetupLifecycleState.ARCHIVED:
            return record.cooldown_until
        return None
    parsed = _parse_timestamp(timestamp)
    if parsed is None:
        return record.cooldown_until
    return (parsed + timedelta(hours=DEFAULT_COOLDOWN_HOURS)).replace(microsecond=0).isoformat()


def _cooldown_expired(cooldown_until: str | None, timestamp: str) -> bool:
    if cooldown_until is None:
        return True
    expiry = _parse_timestamp(cooldown_until)
    current = _parse_timestamp(timestamp)
    if expiry is None or current is None:
        return False
    return current >= expiry


def _parse_timestamp(value: str | None) -> datetime | None:
    if value is None or value == NA:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _invalidation_reason(observation: LifecycleObservation) -> str:
    for value in (observation.invalidation_reason, observation.failed_gate):
        text = _text(value)
        if text != NA:
            return text
    return NA


def _bounded_score(value: Any, default: int) -> int:
    if value is None or value == NA:
        return default
    try:
        score = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(100, score))


def _decimal_or_none(value: Any) -> Decimal | None:
    text = _text(value)
    if text == NA:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _text_or(value: str | None, default: str) -> str:
    text = _text(value)
    return default if text == NA else text


def _text(value: Any) -> str:
    if value is None or value == "":
        return NA
    text = str(value).strip()
    return text if text else NA


def _status_key(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return ""
    key = text.lower().strip().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


__all__ = [
    "ACTIVE_PROGRESSION",
    "ALLOWED_TRANSITIONS",
    "DEFAULT_COOLDOWN_HOURS",
    "DEFAULT_CONFIRMATION_CYCLES",
    "DEFAULT_SETUP_MERGE_TOLERANCE_PCT",
    "LifecycleObservation",
    "OUTCOME_STATES",
    "VALID_STATES",
    "WATCH_PRIORITY_STATES",
    "confirmed_observation_block_reasons",
    "entry_zone_touched",
    "evaluate_lifecycle_transition",
    "initial_state_for_observation",
    "is_watchable_lifecycle_state",
    "now_utc_iso",
    "observed_state",
    "transition_allowed",
    "transition_record",
]
