from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display, representative_strategy_diagnostics
from app.lifecycle.models import (
    SetupLifecycleRecord,
    SetupLifecycleState,
    SetupOutcomeAnalyticsRecord,
    SetupTransitionReason,
    SetupTransitionResult,
)
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.state_machine import (
    DEFAULT_CONFIRMATION_CYCLES,
    DEFAULT_SETUP_MERGE_TOLERANCE_PCT,
    LifecycleObservation,
    WATCH_PRIORITY_STATES,
    entry_zone_touched,
    evaluate_lifecycle_transition,
    is_watchable_lifecycle_state,
    now_utc_iso,
)
from app.pipeline.scanner_runner import ScannerRunResult, ScannerSymbolResult
from app.storage.database import DEFAULT_DATABASE_PATH
from app.storage.symbol_health import load_symbol_health_records

PUBLIC_A_GRADE_MIN_RR = Decimal("3.0")
A_GRADE_WATCH_GRADES = {"a", "a+"}
A_GRADE_ALLOWED_WAITING_GATES = {
    "challenge_limit_entry_missing",
    "entry_limit_missing",
    "entry_not_hit",
    "entry_not_touched",
    "entry_pending",
    "entry_zone_not_touched",
    "entry_zone_pending",
    "limit_entry_missing",
    "limit_fill_pending",
    "limit_not_hit",
    "limit_not_touched",
    "limit_zone_not_touched",
    "price_has_not_touched_entry_zone",
    "price_not_in_entry_zone",
    "waiting_limit_fill",
    "waiting_limit_zone",
}
A_GRADE_HARD_STATUS_BLOCKERS = {
    "failed",
    "rejected_by_derivatives",
    "rejected_by_regime",
    "rejected_by_risk",
    "rejected_by_scoring",
    "rejected_by_technical",
    "scan_error",
}
A_GRADE_HARD_GATE_BLOCKERS = {
    "body_acceptance_failure",
    "challenge_rr_below_3",
    "challenge_trust_below_85",
    "derivatives_conflict",
    "entry_window_expired",
    "funding_oi_guard",
    "invalidation_missing",
    "invalidation_triggered",
    "invalidated",
    "missing_entry",
    "missing_entry_zone",
    "missing_invalidation",
    "missing_rr",
    "missing_stop",
    "missing_target",
    "missing_targets",
    "missing_tp",
    "missing_tp1",
    "missing_tp2",
    "no_displacement_candle",
    "no_ob_or_fvg_zone",
    "pullback_beyond_786",
    "pullback_too_deep",
    "quality_filter",
    "regime_compatibility",
    "risk",
    "risk_validation_failed",
    "rr_below_minimum",
    "rr_too_low",
    "scanner_error",
    "stop_wrong_side",
    "structural_breakdown",
    "target_integrity",
    "target_order_invalid",
    "targets_not_monotonic",
    "trust_meter_below_minimum",
    "wrong_side_stop",
}
TARGET_INTEGRITY_BLOCKED_KEYS = {"blocked", "failed", "fail", "rejected", "reject"}


class SetupLifecycleService:
    def __init__(
        self,
        database_path: Path | str = DEFAULT_DATABASE_PATH,
        *,
        confirmation_cycles: int | None = None,
        setup_tolerance_pct: Decimal | str | None = None,
    ) -> None:
        self.database_path = Path(database_path)
        self.confirmation_cycles = _confirmation_cycles(confirmation_cycles)
        self.setup_tolerance_pct = _setup_tolerance_pct(setup_tolerance_pct)

    def apply_to_run_result(
        self,
        result: ScannerRunResult,
        *,
        scan_run_id: str | None = None,
        now: str | None = None,
    ) -> ScannerRunResult:
        timestamp = now or now_utc_iso()
        updated_results: list[ScannerSymbolResult] = []
        process_summary = _empty_process_summary(scanned_symbols=len(result.results))
        health_records = _load_health_records(self.database_path, tuple(item.symbol for item in result.results))
        with SQLiteSetupLifecycleRepository(self.database_path) as repository:
            for symbol_result in result.results:
                updated, meta = self._apply_to_symbol_result_with_meta(
                    symbol_result,
                    repository=repository,
                    scan_run_id=scan_run_id,
                    now=timestamp,
                    symbol_health_record=health_records.get(symbol_result.symbol),
                )
                updated_results.append(updated)
                _add_process_meta(process_summary, meta)
        return result.model_copy(update={"results": tuple(updated_results), "scanner_process_summary": process_summary})

    def apply_to_symbol_result(
        self,
        symbol_result: ScannerSymbolResult,
        *,
        repository: SQLiteSetupLifecycleRepository,
        scan_run_id: str | None,
        now: str,
    ) -> ScannerSymbolResult:
        updated, _meta = self._apply_to_symbol_result_with_meta(
            symbol_result,
            repository=repository,
            scan_run_id=scan_run_id,
            now=now,
            symbol_health_record=None,
        )
        return updated

    def _apply_to_symbol_result_with_meta(
        self,
        symbol_result: ScannerSymbolResult,
        *,
        repository: SQLiteSetupLifecycleRepository,
        scan_run_id: str | None,
        now: str,
        symbol_health_record: Any | None,
    ) -> tuple[ScannerSymbolResult, dict[str, Any]]:
        observation = observation_from_symbol_result(symbol_result)
        existing = repository.get_record(
            symbol=observation.symbol,
            mode=observation.mode,
            direction=observation.direction,
        )
        health_penalty_cycles = _symbol_health_penalty_cycles(symbol_health_record)
        health_score = getattr(symbol_health_record, "current_health_score", NA) if symbol_health_record is not None else NA
        transition = evaluate_lifecycle_transition(
            existing,
            observation,
            lifecycle_id=existing.lifecycle_id if existing is not None else uuid4().hex,
            now=now,
            scan_run_id=scan_run_id,
            required_confirmation_cycles=self.confirmation_cycles,
            setup_tolerance_pct=self.setup_tolerance_pct,
            symbol_health_score=health_score,
            symbol_health_penalty_cycles=health_penalty_cycles,
        )
        if transition.record is not None:
            repository.upsert_record(transition.record)
        if transition.event is not None:
            repository.insert_event(transition.event)
        if transition.record is not None:
            outcome_record = _outcome_analytics_record(repository, transition, observation)
            if outcome_record is not None:
                repository.upsert_outcome_analytics(outcome_record)
        updated = symbol_result.model_copy(
            update={
                "lifecycle_state": transition.record,
                "lifecycle_transition": transition,
            }
        )
        meta = _process_meta(
            existing=existing,
            transition=transition,
            symbol_health_penalty_cycles=health_penalty_cycles,
        )
        return updated, meta

    def reset(self) -> None:
        with SQLiteSetupLifecycleRepository(self.database_path) as repository:
            repository.reset()


def apply_lifecycle_to_run_result(
    result: ScannerRunResult,
    *,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    scan_run_id: str | None = None,
    now: str | None = None,
    confirmation_cycles: int | None = None,
    setup_tolerance_pct: Decimal | str | None = None,
) -> ScannerRunResult:
    return SetupLifecycleService(
        database_path,
        confirmation_cycles=confirmation_cycles,
        setup_tolerance_pct=setup_tolerance_pct,
    ).apply_to_run_result(result, scan_run_id=scan_run_id, now=now)


def observation_from_symbol_result(symbol_result: ScannerSymbolResult) -> LifecycleObservation:
    display = build_symbol_display(symbol_result)
    diagnostics = representative_strategy_diagnostics(symbol_result)
    gates_passed = _sequence_values(diagnostics.get("gates_passed"))
    gates_failed = _sequence_values(diagnostics.get("gates_failed"))
    failed_gate = _first_non_na(display.failed_gate, diagnostics.get("first_failed_gate"), symbol_result.rejection_stage)
    pullback_failure_type = _pullback_failure_type(symbol_result, diagnostics)
    acceptance_status = _acceptance_status(symbol_result, diagnostics)
    mode = _mode_from_result(symbol_result, diagnostics)
    direction = _direction_from_result(symbol_result, diagnostics)
    rr = _decimal_or_none(_first_non_na(diagnostics.get("rr_to_tp2"), _risk_best_rr(symbol_result)))
    required_rr = Decimal("3.0") if mode == "challenge" else Decimal("2.5")
    pullback_status = _display(diagnostics.get("pullback_zone_status")).lower()
    valid_trade_idea = _valid_trade_idea_exists(symbol_result, display.display_status)
    pullback_valid = (
        valid_trade_idea
        or pullback_status in {"valid", "passed"}
        or "pullback_zone" in gates_passed
    )
    rr_valid = valid_trade_idea or (rr is not None and rr >= required_rr and not set(gates_failed) & _rr_failure_gates())
    sweep_detected = (
        bool(symbol_result.sweep_detected)
        or _display(diagnostics.get("execution_sweep_status")) == "passed"
        or "sweep" in gates_passed
    )
    structure_shift_detected = (
        bool(symbol_result.bos_detected or symbol_result.choch_detected)
        or _display(diagnostics.get("confirmation_structure_shift_status")) == "passed"
        or "bos_choch" in gates_passed
    )
    quality_score = _int_or_zero(getattr(symbol_result.setup_quality, "quality_score", 0))
    quality_grade = _quality_grade_text(symbol_result, diagnostics)
    edge_score = _first_non_na(
        getattr(symbol_result.setup_quality, "profitability_edge_score", NA),
        symbol_result.historical_expectancy,
        symbol_result.expectancy_metrics.get("expectancy") if symbol_result.expectancy_metrics else NA,
    )
    a_grade_watch_candidate = _a_grade_watch_candidate(
        symbol_result,
        diagnostics,
        mode=mode,
        direction=direction,
        quality_grade=quality_grade,
        rr=rr,
        required_rr=required_rr,
        failed_gate=failed_gate,
        gates_failed=gates_failed,
    )
    requires_limit_fill = a_grade_watch_candidate or _requires_limit_fill_before_active(symbol_result, diagnostics)
    if (
        not a_grade_watch_candidate
        and not valid_trade_idea
        and not symbol_result.valid_strategy_modes
        and _status_key(failed_gate)
    ):
        rr_valid = False
    entry_low, entry_high = _entry_zone_values(symbol_result, diagnostics)
    stop_loss = _stop_value(symbol_result, diagnostics)
    targets = _target_values(symbol_result, diagnostics)
    latest_high, latest_low = _latest_observed_range_values(symbol_result, diagnostics)

    return LifecycleObservation(
        symbol=symbol_result.symbol,
        mode=mode,
        direction=direction,
        readiness_score=display.readiness_score,
        readiness_label=display.readiness_label,
        quality_score=quality_score,
        quality_grade=quality_grade,
        entry_low=_display(entry_low),
        entry_high=_display(entry_high),
        stop_loss=_display(stop_loss),
        tp1=_display(targets[0]),
        tp2=_display(targets[1]),
        tp3=_display(targets[2]),
        rr=_display(rr),
        current_price=_display(_current_price_value(symbol_result, diagnostics)),
        latest_high=_display(latest_high),
        latest_low=_display(latest_low),
        edge_score=_display(edge_score),
        failed_gate=failed_gate,
        regime_state=_first_non_na(symbol_result.regime_state, symbol_result.regime_diagnostics.get("state")),
        action_label=display.action_label,
        invalidation_reason=_invalidation_reason(
            symbol_result,
            diagnostics,
            failed_gate,
            pullback_failure_type,
            acceptance_status,
        ),
        sweep_detected=sweep_detected,
        structure_shift_detected=structure_shift_detected,
        pullback_valid=pullback_valid,
        rr_valid=rr_valid,
        valid_trade_idea=valid_trade_idea,
        limit_fill_required=requires_limit_fill,
        a_grade_watch_candidate=a_grade_watch_candidate,
        entry_filled=_entry_zone_touched_for_result(symbol_result, diagnostics),
        invalidated=_structural_acceptance_invalidated(pullback_failure_type, acceptance_status, failed_gate),
        expired=_status_key(failed_gate) == "entry_window_expired",
    )


def prioritize_watch_symbols(
    symbols: Sequence[str],
    *,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    now: str | None = None,
) -> tuple[str, ...]:
    ordered = tuple(dict.fromkeys(_display(symbol).upper() for symbol in symbols if _display(symbol) != NA))
    if not ordered:
        return ()

    with SQLiteSetupLifecycleRepository(database_path) as repository:
        records = repository.get_records_for_symbols(ordered)

    records_by_symbol: dict[str, list[SetupLifecycleRecord]] = {}
    for record in records:
        records_by_symbol.setdefault(record.symbol, []).append(record)

    priority_index = {state: index for index, state in enumerate(WATCH_PRIORITY_STATES)}
    prioritized: list[tuple[int, int, str]] = []
    passthrough: list[tuple[int, str]] = []
    for original_index, symbol in enumerate(ordered):
        symbol_records = records_by_symbol.get(symbol, [])
        if not symbol_records:
            passthrough.append((original_index, symbol))
            continue
        watchable = [record for record in symbol_records if is_watchable_lifecycle_state(record, now=now)]
        if not watchable:
            continue
        best = min(
            watchable,
            key=lambda record: priority_index.get(record.current_state, len(priority_index)),
        )
        prioritized.append((priority_index.get(best.current_state, len(priority_index)), original_index, symbol))

    prioritized.sort()
    passthrough.sort()
    output: list[str] = []
    for _priority, _index, symbol in prioritized:
        if symbol not in output:
            output.append(symbol)
    for _index, symbol in passthrough:
        if symbol not in output:
            output.append(symbol)
    return tuple(output)


def _confirmation_cycles(value: int | None) -> int:
    if value is None:
        raw = os.getenv("SCANNER_CONFIRMATION_CYCLES")
        value = raw if raw not in (None, "") else DEFAULT_CONFIRMATION_CYCLES
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return DEFAULT_CONFIRMATION_CYCLES


def _setup_tolerance_pct(value: Decimal | str | None) -> Decimal:
    if value is None:
        raw = os.getenv("SCANNER_SETUP_MERGE_TOLERANCE_PCT")
        value = raw if raw not in (None, "") else DEFAULT_SETUP_MERGE_TOLERANCE_PCT
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return DEFAULT_SETUP_MERGE_TOLERANCE_PCT
    if not decimal.is_finite() or decimal < 0:
        return DEFAULT_SETUP_MERGE_TOLERANCE_PCT
    return decimal


def _load_health_records(database_path: Path | str, symbols: Sequence[str]) -> dict[str, Any]:
    if not symbols:
        return {}
    try:
        return load_symbol_health_records(database_path, symbols)
    except Exception:
        return {}


def _symbol_health_penalty_cycles(record: Any | None) -> int:
    if record is None:
        return 0
    health_score = _int_or_zero(getattr(record, "current_health_score", 0))
    bad_events = sum(
        _int_or_zero(getattr(record, name, 0))
        for name in (
            "invalidation_count",
            "expired_setup_count",
            "rejected_setup_count",
            "false_confirmation_count",
            "malformed_setup_event_count",
            "stop_breach_after_confirmation_count",
            "duplicate_noisy_setup_count",
        )
    )
    return 1 if health_score < 40 or bad_events >= 3 else 0


def _empty_process_summary(*, scanned_symbols: int) -> dict[str, Any]:
    return {
        "scanned_symbols": scanned_symbols,
        "new_candidates": 0,
        "merged_duplicates": 0,
        "confirmation_pending": 0,
        "confirmed_after_multi_scan": 0,
        "decayed": 0,
        "expired": 0,
        "symbol_health_penalties_applied": 0,
    }


def _add_process_meta(summary: dict[str, Any], meta: Mapping[str, Any]) -> None:
    for key in (
        "new_candidates",
        "merged_duplicates",
        "confirmation_pending",
        "confirmed_after_multi_scan",
        "decayed",
        "expired",
        "symbol_health_penalties_applied",
    ):
        summary[key] = int(summary.get(key, 0)) + int(meta.get(key, 0))


def _process_meta(
    *,
    existing: SetupLifecycleRecord | None,
    transition: SetupTransitionResult,
    symbol_health_penalty_cycles: int,
) -> dict[str, int]:
    record = transition.record
    previous_decay = existing.decay_count if existing is not None else 0
    confirmation_pending = 0
    if record is not None and record.confirmation_count and record.confirmation_count < record.required_confirmation_cycles:
        confirmation_pending = 1
    return {
        "new_candidates": 1 if existing is None and record is not None else 0,
        "merged_duplicates": 1 if existing is not None and record is not None else 0,
        "confirmation_pending": confirmation_pending,
        "confirmed_after_multi_scan": 1 if transition.reason == SetupTransitionReason.MULTI_SCAN_CONFIRMED else 0,
        "decayed": 1 if record is not None and record.decay_count > previous_decay else 0,
        "expired": 1
        if record is not None
        and record.current_state == SetupLifecycleState.EXPIRED
        and (existing is None or existing.current_state != SetupLifecycleState.EXPIRED)
        else 0,
        "symbol_health_penalties_applied": 1 if symbol_health_penalty_cycles > 0 else 0,
    }


def _outcome_analytics_record(
    repository: SQLiteSetupLifecycleRepository,
    transition: SetupTransitionResult,
    observation: LifecycleObservation,
) -> SetupOutcomeAnalyticsRecord | None:
    record = transition.record
    if record is None:
        return None
    final_outcome = _final_outcome_for_state(record.current_state)
    if final_outcome == NA:
        return None
    events = repository.list_events(lifecycle_id=record.lifecycle_id)
    lifecycle_path = " > ".join(event.to_state.value for event in events)
    if not lifecycle_path:
        lifecycle_path = record.current_state.value
    payload = {
        "lifecycle_id": record.lifecycle_id,
        "state": record.current_state.value,
        "reason": transition.reason.value,
        "confirmation_count": record.confirmation_count,
        "required_confirmation_cycles": record.required_confirmation_cycles,
        "decay_count": record.decay_count,
        "decay_reason": record.decay_reason,
    }
    return SetupOutcomeAnalyticsRecord(
        lifecycle_id=record.lifecycle_id,
        symbol=record.symbol,
        bias=record.direction,
        first_seen_at=record.first_seen_at,
        confirmed_at=record.confirmed_at or NA,
        entry_zone=_zone_text(record.entry_low, record.entry_high),
        stop_loss=record.stop_loss,
        tp1=record.tp1,
        tp2=record.tp2,
        tp3=record.tp3,
        quality_at_first_detection=record.quality_grade_first_seen,
        quality_at_confirmation=record.quality_grade_confirmed,
        rr=record.rr,
        lifecycle_path=lifecycle_path,
        final_outcome=final_outcome,
        failure_reason=_failure_reason(record),
        outcome_reason=transition.reason.value,
        regime_context=record.regime_state,
        symbol_health_at_detection=record.symbol_health_score_at_detection,
        raw_payload_json=json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True),
    )


def _final_outcome_for_state(state: SetupLifecycleState) -> str:
    if state == SetupLifecycleState.TP_HIT:
        return "TP1_HIT"
    if state == SetupLifecycleState.SL_HIT:
        return "SL_HIT"
    if state == SetupLifecycleState.INVALIDATED:
        return "INVALIDATED"
    if state == SetupLifecycleState.EXPIRED:
        return "EXPIRED"
    if state == SetupLifecycleState.REJECTED:
        return "REJECTED"
    if state == SetupLifecycleState.COOLDOWN:
        return "COOLDOWN"
    return NA


def _failure_reason(record: SetupLifecycleRecord) -> str:
    if record.current_state in {
        SetupLifecycleState.INVALIDATED,
        SetupLifecycleState.EXPIRED,
        SetupLifecycleState.REJECTED,
        SetupLifecycleState.SL_HIT,
    }:
        return _first_non_na(record.invalidation_reason, record.failed_gate, record.decay_reason)
    return NA


def _zone_text(low: Any, high: Any) -> str:
    low_text = _display(low)
    high_text = _display(high)
    if low_text == NA and high_text == NA:
        return NA
    if high_text == NA or high_text == low_text:
        return low_text
    if low_text == NA:
        return high_text
    return f"{low_text}-{high_text}"


def _mode_from_result(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    if symbol_result.valid_strategy_modes:
        return symbol_result.valid_strategy_modes[0].lower()
    if symbol_result.rejected_strategy_modes:
        return symbol_result.rejected_strategy_modes[0].lower()
    mode = _display(diagnostics.get("mode")).lower()
    if mode != NA.lower():
        return mode
    setup_type = _display(getattr(symbol_result.trade_idea, "setup_type", NA))
    for candidate in ("challenge", "swing", "scalp"):
        if candidate in setup_type:
            return candidate
    return NA


def _direction_from_result(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    trade_direction = _display(getattr(symbol_result.trade_idea, "direction", NA)).lower()
    if trade_direction in {"long", "short"}:
        return trade_direction
    for key in ("bias", "direction"):
        value = _display(diagnostics.get(key)).lower()
        if value in {"long", "short"}:
            return value
    return NA


def _valid_trade_idea_exists(symbol_result: ScannerSymbolResult, display_status: str) -> bool:
    trade_idea = symbol_result.trade_idea
    if trade_idea is None:
        return False
    quality_gate = getattr(trade_idea, "quality_gate_result", None)
    if quality_gate is not None and getattr(quality_gate, "passed", True) is not True:
        return False
    return display_status == "valid_setup"


def _risk_best_rr(symbol_result: ScannerSymbolResult) -> Any:
    risk_decision = symbol_result.risk_decision
    return getattr(risk_decision, "best_risk_reward_ratio", NA) if risk_decision is not None else NA


def _risk_validation_failed(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> bool:
    risk_decision = symbol_result.risk_decision
    if risk_decision is not None and getattr(risk_decision, "approved", True) is not True:
        return True
    risk_approved = diagnostics.get("risk_approved")
    if isinstance(risk_approved, bool) and risk_approved is False:
        return True
    risk_status = _status_key(_first_non_na(diagnostics.get("risk_status"), diagnostics.get("risk_validation_status")))
    return risk_status in {"blocked", "failed", "fail", "rejected", "reject"}


def _requires_limit_fill_before_active(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> bool:
    return _quality_grade_key(_quality_grade_text(symbol_result, diagnostics)) in {"a", "a+"}


def _a_grade_watch_candidate(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    *,
    mode: str,
    direction: str,
    quality_grade: str,
    rr: Decimal | None,
    required_rr: Decimal,
    failed_gate: str,
    gates_failed: Sequence[str],
) -> bool:
    if _quality_grade_key(quality_grade) not in A_GRADE_WATCH_GRADES:
        return False
    if _display(symbol_result.symbol) == NA or _display(symbol_result.symbol).upper() == NA:
        return False
    side = _display(direction).lower()
    if side not in {"long", "short"}:
        return False
    if _display(symbol_result.error_message) != NA:
        return False
    if _status_keys(symbol_result) & A_GRADE_HARD_STATUS_BLOCKERS:
        return False
    if _risk_validation_failed(symbol_result, diagnostics):
        return False
    if _target_integrity_failed(diagnostics):
        return False

    gate_keys = _gate_keys(failed_gate, gates_failed, diagnostics)
    hard_gates = gate_keys & A_GRADE_HARD_GATE_BLOCKERS
    if hard_gates:
        return False
    actionable_gates = {gate for gate in gate_keys if gate and gate != "n_a"}
    if actionable_gates and not actionable_gates <= A_GRADE_ALLOWED_WAITING_GATES:
        return False

    entry_low, entry_high = (_decimal_or_none(value) for value in _entry_zone_values(symbol_result, diagnostics))
    stop = _decimal_or_none(_stop_value(symbol_result, diagnostics))
    targets = _target_values(symbol_result, diagnostics)
    tp1 = _decimal_or_none(targets[0])
    tp2 = _decimal_or_none(targets[1])
    tp3 = _decimal_or_none(targets[2])
    invalidation = _invalidation_value(symbol_result, diagnostics)
    min_rr = max(required_rr, PUBLIC_A_GRADE_MIN_RR)

    if entry_low is None or entry_high is None:
        return False
    if stop is None or tp1 is None or tp2 is None:
        return False
    if _display(invalidation) == NA:
        return False
    if rr is None or rr < min_rr:
        return False

    return _trade_map_geometry_valid(
        side=side,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        targets=tuple(target for target in (tp1, tp2, tp3) if target is not None),
    )


def _quality_grade_text(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    setup_quality = symbol_result.setup_quality
    trade_idea = symbol_result.trade_idea
    return _first_non_na(
        getattr(getattr(setup_quality, "quality_grade", None), "value", NA),
        getattr(trade_idea, "grade", NA) if trade_idea is not None else NA,
        diagnostics.get("quality_grade"),
        diagnostics.get("trust_grade"),
        diagnostics.get("grade"),
    )


def _quality_grade_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    return text.lower().strip().replace(" ", "")


def _status_keys(symbol_result: ScannerSymbolResult) -> set[str]:
    values: list[Any] = [getattr(symbol_result.status, "value", symbol_result.status)]
    values.extend(getattr(status, "value", status) for status in symbol_result.status_history)
    return {_status_key(value) for value in values if _status_key(value)}


def _gate_keys(failed_gate: Any, gates_failed: Sequence[str], diagnostics: Mapping[str, Any]) -> set[str]:
    values: list[Any] = [failed_gate]
    values.extend(gates_failed)
    values.extend(_sequence_values(diagnostics.get("hard_rejection_reasons")))
    values.extend(_sequence_values(diagnostics.get("blocking_reasons")))
    return {_status_key(value) for value in values if _status_key(value)}


def _target_integrity_failed(diagnostics: Mapping[str, Any]) -> bool:
    status = _status_key(
        _first_non_na(
            diagnostics.get("target_integrity_status"),
            diagnostics.get("target_status"),
            diagnostics.get("target_validation_status"),
        )
    )
    if status in TARGET_INTEGRITY_BLOCKED_KEYS:
        return True
    return _status_key(diagnostics.get("first_failed_gate")) == "target_integrity" or "target_integrity" in {
        _status_key(value) for value in _sequence_values(diagnostics.get("gates_failed"))
    }


def _stop_value(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> Any:
    trade_idea = symbol_result.trade_idea
    return _first_non_na(
        diagnostics.get("stop_loss"),
        diagnostics.get("stop"),
        _level_field(getattr(trade_idea, "stop_loss", None), "price") if trade_idea is not None else NA,
    )


def _target_values(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> tuple[Any, Any, Any]:
    trade_idea = symbol_result.trade_idea
    return (
        _first_non_na(diagnostics.get("tp1"), diagnostics.get("target_1"), _take_profit(trade_idea, 1)),
        _first_non_na(diagnostics.get("tp2"), diagnostics.get("target_2"), _take_profit(trade_idea, 2)),
        _first_non_na(diagnostics.get("tp3"), diagnostics.get("target_3"), _take_profit(trade_idea, 3)),
    )


def _take_profit(trade_idea: Any | None, target_number: int) -> Any:
    targets = getattr(trade_idea, "take_profits", ()) if trade_idea is not None else ()
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes, bytearray, Mapping)):
        return NA
    index = target_number - 1
    if index >= len(targets):
        return NA
    return _level_field(targets[index], "price")


def _invalidation_value(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> Any:
    trade_idea = symbol_result.trade_idea
    return _first_non_na(
        getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA,
        diagnostics.get("invalidation"),
        diagnostics.get("watchlist_invalidation"),
        diagnostics.get("invalidation_reason"),
    )


def _trade_map_geometry_valid(
    *,
    side: str,
    entry_low: Decimal,
    entry_high: Decimal,
    stop: Decimal,
    targets: Sequence[Decimal],
) -> bool:
    entry_reference = (min(entry_low, entry_high) + max(entry_low, entry_high)) / Decimal("2")
    if side == "long" and stop >= entry_reference:
        return False
    if side == "short" and stop <= entry_reference:
        return False
    for target in targets:
        if side == "long" and target <= entry_reference:
            return False
        if side == "short" and target >= entry_reference:
            return False
    for left, right in zip(targets, targets[1:]):
        if side == "long" and left >= right:
            return False
        if side == "short" and left <= right:
            return False
    return True


def _entry_zone_touched_for_result(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> bool:
    entry_low, entry_high = _entry_zone_values(symbol_result, diagnostics)
    if _display(entry_low) == NA or _display(entry_high) == NA:
        return False

    for high, low in _latest_range_candidates(symbol_result, diagnostics):
        if entry_zone_touched(high, low, entry_low, entry_high):
            return True
    return False


def _entry_zone_values(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> tuple[Any, Any]:
    trade_idea = symbol_result.trade_idea
    entry_zone = getattr(trade_idea, "entry_zone", None) if trade_idea is not None else None
    diagnostic_zone_low, diagnostic_zone_high = _zone_from_value(diagnostics.get("entry_zone"))
    watch_zone_low, watch_zone_high = _zone_from_value(diagnostics.get("watch_zone"))
    entry_text_low, entry_text_high = _zone_from_value(diagnostics.get("entry"))
    return (
        _first_non_na(
            diagnostics.get("entry_low"),
            _mapping_value(diagnostics.get("entry_zone"), "low"),
            _level_field(entry_zone, "low"),
            _level_field(entry_zone, "price"),
            diagnostic_zone_low,
            watch_zone_low,
            entry_text_low,
        ),
        _first_non_na(
            diagnostics.get("entry_high"),
            _mapping_value(diagnostics.get("entry_zone"), "high"),
            _level_field(entry_zone, "high"),
            _level_field(entry_zone, "price"),
            diagnostic_zone_high,
            watch_zone_high,
            entry_text_high,
        ),
    )


def _latest_range_candidates(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
) -> tuple[tuple[Any, Any], ...]:
    candidates: list[tuple[Any, Any]] = []
    for key in ("current_candle", "latest_candle", "candle"):
        high, low = _range_from_value(diagnostics.get(key))
        if _display(high) != NA and _display(low) != NA:
            candidates.append((high, low))

    for high_key, low_key in (
        ("candle_high", "candle_low"),
        ("latest_high", "latest_low"),
        ("current_high", "current_low"),
        ("high", "low"),
    ):
        high = diagnostics.get(high_key)
        low = diagnostics.get(low_key)
        if _display(high) != NA and _display(low) != NA:
            candidates.append((high, low))

    if _display(symbol_result.latest_high) != NA and _display(symbol_result.latest_low) != NA:
        candidates.append((symbol_result.latest_high, symbol_result.latest_low))

    for key in ("candles_5m", "candles_15m", "candles_1h", "candles_4h", "candles_12h", "candles_2d", "candles"):
        values = diagnostics.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray, Mapping)) or not values:
            continue
        high, low = _range_from_value(values[-1])
        if _display(high) != NA and _display(low) != NA:
            candidates.append((high, low))

    current_price = _first_non_na(
        diagnostics.get("current_price"),
        diagnostics.get("price"),
        diagnostics.get("last_price"),
        symbol_result.current_price,
        symbol_result.latest_close,
    )
    if _display(current_price) != NA:
        candidates.append((current_price, current_price))

    return tuple(candidates)


def _latest_observed_range_values(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
) -> tuple[Any, Any]:
    candidates = _latest_range_candidates(symbol_result, diagnostics)
    return candidates[0] if candidates else (NA, NA)


def _current_price_value(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> Any:
    return _first_non_na(
        diagnostics.get("current_price"),
        diagnostics.get("price"),
        diagnostics.get("last_price"),
        symbol_result.current_price,
        symbol_result.latest_close,
    )


def _range_from_value(value: Any) -> tuple[Any, Any]:
    if isinstance(value, Mapping):
        return value.get("high", NA), value.get("low", NA)
    return getattr(value, "high", NA), getattr(value, "low", NA)


def _zone_from_value(value: Any) -> tuple[Any, Any]:
    if isinstance(value, Mapping):
        return (
            _first_non_na(value.get("low"), value.get("entry_low")),
            _first_non_na(value.get("high"), value.get("entry_high")),
        )
    text = _display(value)
    if text == NA:
        return NA, NA
    normalized = text.replace("–", "-").replace("—", "-")
    parts = [part.strip() for part in normalized.split("-") if part.strip()]
    if len(parts) == 1:
        return parts[0], parts[0]
    if len(parts) >= 2:
        return parts[0], parts[1]
    return NA, NA


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key, NA) if isinstance(value, Mapping) else NA


def _level_field(level: Any, field: str) -> Any:
    return getattr(level, field, NA)


def _invalidation_reason(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    failed_gate: str,
    pullback_failure_type: str = NA,
    acceptance_status: str = NA,
) -> str:
    if acceptance_status == "STRUCTURAL_BREAKDOWN" or failed_gate == "structural_breakdown":
        return "structure broke after body acceptance beyond 0.786"
    if acceptance_status == "BODY_ACCEPTANCE_FAILURE" or failed_gate == "body_acceptance_failure":
        return "body accepted beyond 0.786 invalidation zone"
    if pullback_failure_type == "TOO_DEEP" or failed_gate in {"pullback_too_deep", "pullback_beyond_786"}:
        return "pullback exceeded valid structure depth"
    trade_idea = symbol_result.trade_idea
    for value in (
        getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA,
        diagnostics.get("invalidation"),
        diagnostics.get("pullback_failure_reason"),
        symbol_result.rejection_reason,
        failed_gate,
    ):
        text = _display(value)
        if text != NA:
            return text
    return NA


def _pullback_failure_type(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    intelligence = symbol_result.pullback_intelligence
    if intelligence is not None:
        return _display(getattr(intelligence.pullback_failure_type, "value", intelligence.pullback_failure_type))
    payload = diagnostics.get("pullback_intelligence")
    if isinstance(payload, Mapping):
        return _display(payload.get("pullback_failure_type"))
    return NA


def _acceptance_status(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    intelligence = symbol_result.pullback_intelligence
    if intelligence is not None:
        return _display(getattr(intelligence, "acceptance_status", NA))
    payload = diagnostics.get("pullback_intelligence")
    if isinstance(payload, Mapping):
        status = _display(payload.get("acceptance_status"))
        if status != NA:
            return status
        structure = payload.get("wick_close_structure")
        if isinstance(structure, Mapping):
            return _display(structure.get("acceptance_status"))
    status = _display(diagnostics.get("acceptance_status"))
    if status != NA:
        return status
    structure = diagnostics.get("wick_close_structure")
    if isinstance(structure, Mapping):
        return _display(structure.get("acceptance_status"))
    return NA


def _structural_acceptance_invalidated(pullback_failure_type: str, acceptance_status: str, failed_gate: str) -> bool:
    return (
        pullback_failure_type == "TOO_DEEP"
        or failed_gate in {"pullback_too_deep", "pullback_beyond_786", "body_acceptance_failure", "structural_breakdown"}
        or acceptance_status in {"BODY_ACCEPTANCE_FAILURE", "STRUCTURAL_BREAKDOWN"}
    )


def _rr_failure_gates() -> set[str]:
    return {
        "missing_rr",
        "missing_target",
        "rr_below_minimum",
        "challenge_rr_below_3",
        "rr_too_low",
    }


def _sequence_values(values: Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(_display(value) for value in values if _display(value) != NA)


def _first_non_na(*values: Any) -> str:
    for value in values:
        text = _display(value)
        if text != NA:
            return text
    return NA


def _decimal_or_none(value: Any) -> Decimal | None:
    text = _display(value)
    if text == NA:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _int_or_zero(value: Any) -> int:
    try:
        return max(0, min(100, int(value)))
    except (TypeError, ValueError):
        return 0


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _status_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    key = text.lower().strip().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


__all__ = [
    "SetupLifecycleService",
    "apply_lifecycle_to_run_result",
    "observation_from_symbol_result",
    "prioritize_watch_symbols",
]
