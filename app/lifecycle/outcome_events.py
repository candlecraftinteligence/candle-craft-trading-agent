from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from app.data.candle_integrity import CausalCandle
from app.data.dtos import NA
from app.lifecycle.models import (
    SetupLifecycleEvent,
    SetupLifecycleOutcomeProgress,
    SetupLifecycleRecord,
    SetupLifecycleState,
    SetupTransitionReason,
    SetupTransitionResult,
)
from app.lifecycle.outcome_policy import StoredPlanGeometry
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.state_machine import transition_record


def advance_to_managing(
    record: SetupLifecycleRecord,
    *,
    repository: SQLiteSetupLifecycleRepository,
    causal: CausalCandle,
    evaluated_at: str,
    scan_run_id: str | None,
    plan_identity: str,
) -> tuple[
    SetupLifecycleRecord,
    SetupTransitionResult | None,
    tuple[SetupTransitionResult, ...],
]:
    current = record
    last_transition: SetupTransitionResult | None = None
    transitions: list[SetupTransitionResult] = []
    next_state = {
        SetupLifecycleState.WATCHLISTED: SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.STALKING: SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.ACTIONABLE_A_GRADE: SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.A_GRADE_WATCH: SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.TRIGGERED: SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.CONFIRMED: SetupLifecycleState.EXECUTING,
        SetupLifecycleState.EXECUTING: SetupLifecycleState.MANAGING,
    }
    for _ in range(4):
        target = next_state.get(current.current_state)
        if target is None:
            break
        reason = (
            SetupTransitionReason.ENTRY_ZONE_TOUCHED
            if target == SetupLifecycleState.TRIGGERED
            else SetupTransitionReason.ENTRY_FILL_SIMULATED
        )
        transition = transition_record(
            current,
            target,
            reason=reason,
            now=evaluated_at,
            scan_run_id=scan_run_id,
            notes=_event_notes(
                event_type="entry_state_progression",
                causal=causal,
                plan_identity=plan_identity,
                policy_reason=NA,
                level=NA,
            ),
        )
        if not transition.allowed or not transition.transitioned or transition.record is None:
            break
        repository.upsert_record(transition.record)
        if transition.event is not None:
            repository.insert_event(transition.event)
        current = transition.record
        last_transition = transition
        transitions.append(transition)
        if current.current_state == SetupLifecycleState.MANAGING:
            break
    return current, last_transition, tuple(transitions)


def record_stop(
    record: SetupLifecycleRecord,
    progress: SetupLifecycleOutcomeProgress,
    *,
    geometry: StoredPlanGeometry,
    causal: CausalCandle,
    evaluated_at: str,
    repository: SQLiteSetupLifecycleRepository,
    scan_run_id: str | None,
    plan_identity: str,
    ambiguity_reason: str,
) -> tuple[SetupLifecycleRecord, SetupTransitionResult, SetupLifecycleOutcomeProgress]:
    updated_record, transition = record_terminal_transition(
        record,
        to_state=SetupLifecycleState.SL_HIT,
        reason=SetupTransitionReason.STOP_LOSS_HIT,
        event_type="terminal_sl_hit",
        causal=causal,
        evaluated_at=evaluated_at,
        repository=repository,
        scan_run_id=scan_run_id,
        plan_identity=plan_identity,
        policy_reason=ambiguity_reason,
        level=str(geometry.stop_loss),
    )
    candle_close = causal.close_timestamp.isoformat()
    return (
        updated_record,
        transition,
        progress.model_copy(
            update={
                "stop_at": candle_close,
                "outcome_at": candle_close,
                "terminal_outcome": SetupLifecycleState.SL_HIT.value,
            }
        ),
    )


def record_terminal_transition(
    record: SetupLifecycleRecord,
    *,
    to_state: SetupLifecycleState,
    reason: SetupTransitionReason,
    event_type: str,
    causal: CausalCandle,
    evaluated_at: str,
    repository: SQLiteSetupLifecycleRepository,
    scan_run_id: str | None,
    plan_identity: str,
    policy_reason: str = NA,
    level: str = NA,
) -> tuple[SetupLifecycleRecord, SetupTransitionResult]:
    transition = transition_record(
        record,
        to_state,
        reason=reason,
        now=evaluated_at,
        scan_run_id=scan_run_id,
        notes=_event_notes(
            event_type=event_type,
            causal=causal,
            plan_identity=plan_identity,
            policy_reason=policy_reason,
            level=level,
        ),
    )
    if not transition.allowed or not transition.transitioned or transition.record is None:
        raise ValueError(
            "outcome_transition_rejected:"
            f"{record.current_state.value}->{to_state.value}"
        )
    repository.upsert_record(transition.record)
    if transition.event is not None:
        repository.insert_event(transition.event)
    return transition.record, transition


def insert_milestone_event(
    repository: SQLiteSetupLifecycleRepository,
    record: SetupLifecycleRecord,
    *,
    reason: SetupTransitionReason,
    event_type: str,
    causal: CausalCandle,
    evaluated_at: str,
    scan_run_id: str | None,
    plan_identity: str,
    level: str,
    evidence: Mapping[str, Any] | None = None,
) -> None:
    repository.insert_event(
        SetupLifecycleEvent(
            lifecycle_id=record.lifecycle_id,
            timestamp=evaluated_at,
            symbol=record.symbol,
            from_state=record.current_state,
            to_state=record.current_state,
            reason=reason,
            scan_run_id=scan_run_id,
            readiness_score=record.readiness_score,
            quality_score=record.quality_score,
            failed_gate=NA,
            notes=_event_notes(
                event_type=event_type,
                causal=causal,
                plan_identity=plan_identity,
                policy_reason=NA,
                level=level,
                evidence=evidence,
            ),
        )
    )


def target_reason(target_number: int) -> SetupTransitionReason:
    return {
        1: SetupTransitionReason.TP1_MILESTONE,
        2: SetupTransitionReason.TP2_MILESTONE,
        3: SetupTransitionReason.TP3_MILESTONE,
    }[target_number]


def _event_notes(
    *,
    event_type: str,
    causal: CausalCandle,
    plan_identity: str,
    policy_reason: str,
    level: str,
    evidence: Mapping[str, Any] | None = None,
) -> str:
    payload = {
        "candle_close_at": causal.close_timestamp.isoformat(),
        "candle_open_at": causal.open_timestamp.isoformat(),
        "event_type": event_type,
        "level": level,
        "plan_identity": plan_identity,
        "policy_reason": policy_reason,
        "source": "canonical_lifecycle_closed_execution_candles",
    }
    if evidence:
        payload.update({str(key): value for key, value in evidence.items()})
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


__all__ = [
    "advance_to_managing",
    "insert_milestone_event",
    "record_stop",
    "record_terminal_transition",
    "target_reason",
]
