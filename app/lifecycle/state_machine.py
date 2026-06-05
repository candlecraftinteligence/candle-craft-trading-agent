from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.dtos import NA
from app.lifecycle.models import (
    SetupLifecycleEvent,
    SetupLifecycleRecord,
    SetupLifecycleState,
    SetupTransitionReason,
    SetupTransitionResult,
)

ACTIVE_PROGRESSION = (
    SetupLifecycleState.DISCOVERED,
    SetupLifecycleState.WATCHLISTED,
    SetupLifecycleState.STALKING,
    SetupLifecycleState.TRIGGERED,
    SetupLifecycleState.CONFIRMED,
    SetupLifecycleState.A_GRADE_WATCH,
    SetupLifecycleState.EXECUTING,
    SetupLifecycleState.MANAGING,
)
VALID_STATES = {
    SetupLifecycleState.CONFIRMED,
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
WATCH_PRIORITY_STATES = (
    SetupLifecycleState.A_GRADE_WATCH,
    SetupLifecycleState.STALKING,
    SetupLifecycleState.TRIGGERED,
    SetupLifecycleState.CONFIRMED,
    SetupLifecycleState.WATCHLISTED,
)
DEFAULT_COOLDOWN_HOURS = 24

ALLOWED_TRANSITIONS: dict[SetupLifecycleState, set[SetupLifecycleState]] = {
    SetupLifecycleState.DISCOVERED: {
        SetupLifecycleState.WATCHLISTED,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.REJECTED,
    },
    SetupLifecycleState.REJECTED: {
        SetupLifecycleState.WATCHLISTED,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.ARCHIVED,
    },
    SetupLifecycleState.WATCHLISTED: {
        SetupLifecycleState.STALKING,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.REJECTED,
    },
    SetupLifecycleState.STALKING: {
        SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.WATCHLISTED,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.REJECTED,
    },
    SetupLifecycleState.TRIGGERED: {
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.STALKING,
        SetupLifecycleState.INVALIDATED,
    },
    SetupLifecycleState.CONFIRMED: {
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.EXECUTING,
        SetupLifecycleState.INVALIDATED,
        SetupLifecycleState.EXPIRED,
    },
    SetupLifecycleState.A_GRADE_WATCH: {
        SetupLifecycleState.EXECUTING,
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
    edge_score: str = NA
    failed_gate: str = NA
    regime_state: str = NA
    action_label: str = NA
    invalidation_reason: str = NA
    sweep_detected: bool = False
    structure_shift_detected: bool = False
    pullback_valid: bool = False
    rr_valid: bool = False
    valid_trade_idea: bool = False
    limit_fill_required: bool = False
    a_grade_watch_candidate: bool = False
    entry_filled: bool = False
    tp_hit: bool = False
    sl_hit: bool = False
    invalidated: bool = False
    expired: bool = False

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
    if record.current_state == to_state:
        updated = record.model_copy(
            update={
                "last_seen_at": timestamp,
                "readiness_score": _bounded_score(readiness_score, record.readiness_score),
                "quality_score": _bounded_score(quality_score, record.quality_score),
                "failed_gate": _text_or(failed_gate, record.failed_gate),
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
        failed_gate=_text_or(failed_gate, record.failed_gate),
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
) -> SetupTransitionResult:
    timestamp = now or now_utc_iso()
    if record is None:
        initial_state = initial_state_for_observation(observation)
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
            failed_gate=_text(observation.failed_gate),
            readiness_score=_bounded_score(observation.readiness_score, 0),
            quality_score=_bounded_score(observation.quality_score, 0),
            edge_score=_text(observation.edge_score),
            regime_state=_text(observation.regime_state),
            action_label=_text(observation.action_label),
            invalidation_reason=_invalidation_reason(observation),
            cooldown_until=None,
            archived_at=timestamp if initial_state == SetupLifecycleState.ARCHIVED else None,
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
            notes=_reason_for_state(initial_state, observation, initialized=True).value,
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

    updated_record = _record_with_observation(record, observation, timestamp)
    next_state = next_state_for_observation(updated_record, observation, timestamp)
    reason = _reason_for_state(next_state, observation)
    return transition_record(
        updated_record,
        next_state,
        reason=reason,
        now=timestamp,
        scan_run_id=scan_run_id,
        readiness_score=observation.readiness_score,
        quality_score=observation.quality_score,
        failed_gate=observation.failed_gate,
        notes=reason.value if next_state != updated_record.current_state else SetupTransitionReason.NO_CHANGE.value,
    )


def initial_state_for_observation(observation: LifecycleObservation) -> SetupLifecycleState:
    target = observed_state(observation)
    if observation.a_grade_watch_candidate:
        return SetupLifecycleState.A_GRADE_WATCH
    if target in {SetupLifecycleState.EXECUTING, SetupLifecycleState.MANAGING}:
        return SetupLifecycleState.CONFIRMED
    return target


def observed_state(observation: LifecycleObservation) -> SetupLifecycleState:
    if observation.tp_hit:
        return SetupLifecycleState.TP_HIT
    if observation.sl_hit:
        return SetupLifecycleState.SL_HIT
    if observation.invalidated:
        return SetupLifecycleState.INVALIDATED
    if observation.expired:
        return SetupLifecycleState.EXPIRED
    if observation.a_grade_watch_candidate:
        return SetupLifecycleState.EXECUTING if observation.entry_filled else SetupLifecycleState.A_GRADE_WATCH
    if observation.valid_trade_idea or observation.pullback_and_rr_valid:
        return SetupLifecycleState.CONFIRMED
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
        if observation.invalidated or _active_setup_invalidated(current, target):
            return SetupLifecycleState.INVALIDATED
        if observation.expired:
            return SetupLifecycleState.EXPIRED
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
        if observation.entry_filled and target in {SetupLifecycleState.A_GRADE_WATCH, SetupLifecycleState.EXECUTING}:
            return SetupLifecycleState.EXECUTING
        return current
    if current == SetupLifecycleState.REJECTED:
        if target == SetupLifecycleState.A_GRADE_WATCH:
            return SetupLifecycleState.A_GRADE_WATCH
        if target in WATCH_PRIORITY_STATES or target in VALID_STATES:
            return SetupLifecycleState.WATCHLISTED if observation.readiness_score > record.readiness_score else current
        return current
    if current == SetupLifecycleState.DISCOVERED:
        if target == SetupLifecycleState.REJECTED:
            return SetupLifecycleState.REJECTED
        if target == SetupLifecycleState.A_GRADE_WATCH:
            return SetupLifecycleState.A_GRADE_WATCH
        if target in WATCH_PRIORITY_STATES or target in VALID_STATES:
            return SetupLifecycleState.WATCHLISTED
        return current
    if current == SetupLifecycleState.WATCHLISTED:
        if target == SetupLifecycleState.REJECTED:
            return SetupLifecycleState.REJECTED
        if target == SetupLifecycleState.A_GRADE_WATCH:
            return SetupLifecycleState.A_GRADE_WATCH
        if observation.sweep_detected:
            return SetupLifecycleState.STALKING
        return current
    if current == SetupLifecycleState.STALKING:
        if target == SetupLifecycleState.REJECTED:
            return SetupLifecycleState.REJECTED
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
        if target == SetupLifecycleState.A_GRADE_WATCH:
            return SetupLifecycleState.A_GRADE_WATCH
        if observation.pullback_and_rr_valid or observation.valid_trade_idea:
            return SetupLifecycleState.CONFIRMED
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


def _record_with_observation(
    record: SetupLifecycleRecord,
    observation: LifecycleObservation,
    timestamp: str,
) -> SetupLifecycleRecord:
    return record.model_copy(
        update={
            "last_seen_at": timestamp,
            "failed_gate": _text(observation.failed_gate),
            "readiness_score": _bounded_score(observation.readiness_score, record.readiness_score),
            "quality_score": _bounded_score(observation.quality_score, record.quality_score),
            "edge_score": _text(observation.edge_score),
            "regime_state": _text(observation.regime_state),
            "action_label": _text(observation.action_label),
            "invalidation_reason": _invalidation_reason(observation),
        }
    )


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
        return SetupTransitionReason.STRUCTURE_SHIFT_CONFIRMED
    if state == SetupLifecycleState.CONFIRMED:
        return SetupTransitionReason.PULLBACK_RR_VALID
    if state == SetupLifecycleState.A_GRADE_WATCH:
        return SetupTransitionReason.A_GRADE_WATCH
    if state == SetupLifecycleState.EXECUTING:
        if observation.entry_filled and (observation.a_grade_watch_candidate or observation.limit_fill_required):
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
        current in {SetupLifecycleState.CONFIRMED, SetupLifecycleState.A_GRADE_WATCH, SetupLifecycleState.EXECUTING}
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


__all__ = [
    "ACTIVE_PROGRESSION",
    "ALLOWED_TRANSITIONS",
    "DEFAULT_COOLDOWN_HOURS",
    "LifecycleObservation",
    "OUTCOME_STATES",
    "VALID_STATES",
    "WATCH_PRIORITY_STATES",
    "entry_zone_touched",
    "evaluate_lifecycle_transition",
    "initial_state_for_observation",
    "is_watchable_lifecycle_state",
    "now_utc_iso",
    "observed_state",
    "transition_allowed",
    "transition_record",
]
