from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.core.minimum_rr import hard_mode_minimum_rr
from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display, representative_strategy_diagnostics
from app.lifecycle.identity import generation_rotation_reason, new_setup_generation_id
from app.lifecycle.models import (
    ACTIVE_LIFECYCLE_MONITORING_STATES,
    SetupLifecycleRecord,
    SetupLifecycleOutcomeProgress,
    SetupLifecycleState,
    SetupOutcomeAnalyticsRecord,
    SetupTransitionReason,
    SetupTransitionResult,
    lifecycle_monitoring_priority,
)
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.outcome_policy import stored_plan_geometry_failure
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.state_machine import (
    DEFAULT_CONFIRMATION_CYCLES,
    DEFAULT_SETUP_MERGE_TOLERANCE_PCT,
    LifecycleObservation,
    confirmed_observation_block_reasons,
    entry_zone_touched,
    evaluate_lifecycle_transition,
    now_utc_iso,
)
from app.pipeline.scanner_runner import ScannerRunResult, ScannerSymbolResult
from app.storage.database import DEFAULT_DATABASE_PATH
from app.storage.symbol_health import load_symbol_health_records

logger = logging.getLogger(__name__)

ACTIONABLE_A_GRADE_MIN_RR = Decimal("2.5")
ACTIONABLE_A_GRADE_GRADES = {"a+", "a", "a-"}
A_GRADE_ACTIONABLE_STATE = "A_GRADE_ACTIONABLE"
A_GRADE_ACTIONABLE_TARGET_CAUTION_STATE = "A_GRADE_ACTIONABLE_TARGET_CAUTION"
A_GRADE_BLOCKED_BY_SCORING_STATE = "A_GRADE_BLOCKED_BY_SCORING"
A_GRADE_BLOCKED_BY_TARGET_STATE = "A_GRADE_BLOCKED_BY_TARGET"
A_GRADE_BLOCKED_BY_ENTRY_WINDOW_STATE = "A_GRADE_BLOCKED_BY_ENTRY_WINDOW"
A_GRADE_BLOCKED_BY_TRUST_STATE = "A_GRADE_BLOCKED_BY_TRUST"
A_GRADE_BLOCKED_BY_FINAL_GATES_STATE = "A_GRADE_BLOCKED_BY_FINAL_GATES"
NOT_A_GRADE_CANDIDATE_STATE = "NOT_A_GRADE_CANDIDATE"
A_GRADE_BLOCKED_STATES = {
    A_GRADE_BLOCKED_BY_SCORING_STATE,
    A_GRADE_BLOCKED_BY_TARGET_STATE,
    A_GRADE_BLOCKED_BY_ENTRY_WINDOW_STATE,
    A_GRADE_BLOCKED_BY_TRUST_STATE,
    A_GRADE_BLOCKED_BY_FINAL_GATES_STATE,
}
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
TARGET_INTEGRITY_PASSED_KEYS = {"ok", "pass", "passed", "valid"}
TARGET_FAILURE_SEVERITY_FATAL = "fatal_target_failure"
TARGET_FAILURE_SEVERITY_SOFT = "soft_target_warning"
TARGET_FAILURE_SEVERITY_CAUTION = "target_caution_actionable"
TARGET_FAILURE_SEVERITY_PASSED = "target_passed"
TARGET_CAUTION_STATUS_KEYS = {"warning", "warn", "caution", "soft_warning", "target_caution"}
TARGET_INSIDE_CHOP_KEYS = {"target_inside_chop", "tp2_inside_chop", "tp2_inside_range", "tp2_remains_inside_recent_chop_range"}
TARGET_CAUTION_ALLOWED_GATES = {"target_integrity", "target_integrity_failed", "target_inside_chop"}
ACTIONABLE_A_GRADE_ALLOWED_NON_FATAL_GATES = A_GRADE_ALLOWED_WAITING_GATES | {
    "challenge_rr_below_3",
    "rr_below_minimum",
    "rr_too_low",
}
ACTIONABLE_A_GRADE_FATAL_GATE_BLOCKERS = A_GRADE_HARD_GATE_BLOCKERS - {
    "challenge_rr_below_3",
    "rr_below_minimum",
    "rr_too_low",
}


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
        prepared: list[tuple[ScannerSymbolResult, LifecycleObservation | None]] = []
        for symbol_result in result.results:
            status = getattr(symbol_result.status, "value", symbol_result.status)
            if status == "not_run":
                prepared.append((symbol_result, None))
                process_summary["skipped_not_run_symbols"] += 1
                continue
            try:
                observation = observation_from_symbol_result(
                    symbol_result, min_score_for_idea=result.config.min_score_for_idea
                )
            except (InvalidOperation, TypeError, ValueError) as exc:
                _record_lifecycle_symbol_error(process_summary, symbol_result.symbol, exc)
                prepared.append((symbol_result, None))
                continue
            prepared.append((symbol_result, observation))

        health_records = _load_health_records(self.database_path, tuple(item.symbol for item in result.results))
        with SQLiteSetupLifecycleRepository(self.database_path) as repository:
            connection = repository.connection
            assert connection is not None
            connection.execute("BEGIN IMMEDIATE")
            for symbol_result, observation in prepared:
                if observation is None:
                    updated_results.append(symbol_result)
                    continue
                updated, meta, error = self._apply_to_symbol_result_isolated(
                    symbol_result,
                    observation=observation,
                    repository=repository,
                    scan_run_id=scan_run_id,
                    now=timestamp,
                    symbol_health_record=health_records.get(symbol_result.symbol),
                    min_score_for_idea=result.config.min_score_for_idea,
                )
                if error is not None:
                    _record_lifecycle_symbol_error(process_summary, symbol_result.symbol, error)
                    updated_results.append(symbol_result)
                    continue
                assert updated is not None
                updated_results.append(updated)
                _add_process_meta(process_summary, meta)
        process_summary["processed_symbols"] = (
            len(updated_results)
            - process_summary["failed_symbols"]
            - process_summary["skipped_not_run_symbols"]
        )
        process_summary["status"] = "PARTIAL" if process_summary["failed_symbols"] else "SUCCESS"
        return result.model_copy(
            update={"results": tuple(updated_results), "scanner_process_summary": process_summary}
        )

    def _apply_to_symbol_result_isolated(
        self,
        symbol_result: ScannerSymbolResult,
        *,
        observation: LifecycleObservation,
        repository: SQLiteSetupLifecycleRepository,
        scan_run_id: str | None,
        now: str,
        symbol_health_record: Any | None,
        min_score_for_idea: Any,
    ) -> tuple[
        ScannerSymbolResult | None,
        dict[str, Any],
        Exception | None,
    ]:
        connection = repository.connection
        assert connection is not None
        connection.execute("SAVEPOINT lifecycle_symbol")
        try:
            updated, meta = self._apply_to_symbol_result_with_meta(
                symbol_result,
                observation=observation,
                repository=repository,
                scan_run_id=scan_run_id,
                now=now,
                symbol_health_record=symbol_health_record,
                min_score_for_idea=min_score_for_idea,
            )
        except (InvalidOperation, TypeError, ValueError) as exc:
            connection.execute("ROLLBACK TO lifecycle_symbol")
            connection.execute("RELEASE lifecycle_symbol")
            return None, {}, exc
        except sqlite3.Error:
            connection.execute("ROLLBACK TO lifecycle_symbol")
            connection.execute("RELEASE lifecycle_symbol")
            raise
        else:
            connection.execute("RELEASE lifecycle_symbol")
            return updated, meta, None

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
            min_score_for_idea=Decimal("80"),
        )
        return updated

    def _apply_to_symbol_result_with_meta(
        self,
        symbol_result: ScannerSymbolResult,
        *,
        observation: LifecycleObservation | None = None,
        repository: SQLiteSetupLifecycleRepository,
        scan_run_id: str | None,
        now: str,
        symbol_health_record: Any | None,
        min_score_for_idea: Any,
    ) -> tuple[ScannerSymbolResult, dict[str, Any]]:
        if observation is None:
            observation = observation_from_symbol_result(symbol_result, min_score_for_idea=min_score_for_idea)
        existing = repository.get_record(
            symbol=observation.symbol,
            mode=observation.mode,
            direction=observation.direction,
        )
        prior_generation = existing
        rotation_reason = generation_rotation_reason(
            existing,
            observed_structural_anchor=observation.structural_anchor,
            setup_observable=_generation_setup_observable(observation),
            terminal_observation=_generation_terminal_observation(observation),
            now=now,
        )
        if rotation_reason is not None:
            existing = None
        lifecycle_id = (
            existing.lifecycle_id
            if existing is not None
            else new_setup_generation_id(
                symbol=observation.symbol,
                mode=observation.mode,
                direction=observation.direction,
                structural_anchor=observation.structural_anchor,
            )
        )
        health_penalty_cycles = _symbol_health_penalty_cycles(symbol_health_record)
        health_score = getattr(symbol_health_record, "current_health_score", NA) if symbol_health_record is not None else NA
        transition = evaluate_lifecycle_transition(
            existing,
            observation,
            lifecycle_id=lifecycle_id,
            now=now,
            scan_run_id=scan_run_id,
            required_confirmation_cycles=self.confirmation_cycles,
            setup_tolerance_pct=self.setup_tolerance_pct,
            symbol_health_score=health_score,
            symbol_health_penalty_cycles=health_penalty_cycles,
        )
        final_record = transition.record
        outcome_progress: SetupLifecycleOutcomeProgress | None = None
        effective_transition = transition
        persistence_blocked = (
            not transition.allowed
            and transition.notes.startswith("invalid_stored_plan_geometry:")
        )
        if final_record is not None and not persistence_blocked:
            if rotation_reason is not None and prior_generation is not None:
                repository.supersede_record(prior_generation.lifecycle_id)
            repository.upsert_record(final_record)
        if transition.event is not None and not persistence_blocked:
            repository.insert_event(transition.event)
        _log_lifecycle_actionability_audit(symbol_result, observation, transition)

        execution_candles = symbol_result.lifecycle_execution_candles
        if final_record is not None and execution_candles is not None:
            outcome_evaluation = evaluate_closed_candle_outcomes(
                final_record,
                execution_candles=execution_candles,
                execution_timeframe=symbol_result.lifecycle_execution_timeframe,
                decision_timestamp=symbol_result.lifecycle_decision_timestamp or now,
                evaluated_at=now,
                repository=repository,
                scan_run_id=scan_run_id,
            )
            final_record = outcome_evaluation.record
            outcome_progress = outcome_evaluation.progress
            if outcome_evaluation.last_transition is not None:
                effective_transition = outcome_evaluation.last_transition

        if final_record is not None:
            effective_transition = effective_transition.model_copy(update={"record": final_record})
            outcome_record = _outcome_analytics_record(
                repository,
                effective_transition,
                observation,
                outcome_progress=outcome_progress,
            )
            if outcome_record is not None:
                already_persisted = any(
                    item.lifecycle_id == outcome_record.lifecycle_id
                    and item.final_outcome == outcome_record.final_outcome
                    for item in repository.list_outcome_analytics(symbol=outcome_record.symbol)
                )
                if effective_transition.transitioned or not already_persisted:
                    repository.upsert_outcome_analytics(outcome_record)
        audit_updates = {
            "candidate_quality_grade": observation.candidate_quality_grade,
            "final_quality_grade": observation.final_quality_grade,
            "final_failed_gate": observation.final_failed_gate,
            "final_block_reason": observation.final_block_reason,
            "target_integrity_status": observation.target_integrity_status,
            "target_failure": observation.target_failure,
            "target_failure_severity": observation.target_failure_severity,
            "target_warning_reason": observation.target_warning_reason,
            "actionability_state": observation.actionability_state,
        }
        updated = symbol_result.model_copy(
            update={
                "lifecycle_state": final_record,
                "lifecycle_transition": effective_transition,
                "lifecycle_outcome_progress": outcome_progress,
                **audit_updates,
            }
        )
        meta = _process_meta(
            existing=existing,
            transition=effective_transition,
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


def observation_from_symbol_result(
    symbol_result: ScannerSymbolResult,
    *,
    min_score_for_idea: Any = Decimal("80"),
) -> LifecycleObservation:
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
    required_rr = _decimal_or_none(diagnostics.get("effective_minimum_rr"))
    if required_rr is None:
        safe_mode = mode if mode in {"challenge", "scalp", "swing"} else "swing"
        required_rr = hard_mode_minimum_rr(safe_mode)
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
    quality_state = _setup_quality_state_text(symbol_result)
    technical_score_text = _display(symbol_result.technical_score)
    opportunity_score_text = _display(_opportunity_score(symbol_result, diagnostics))
    edge_score = _first_non_na(
        getattr(symbol_result.setup_quality, "profitability_edge_score", NA),
        symbol_result.historical_expectancy,
        symbol_result.expectancy_metrics.get("expectancy") if symbol_result.expectancy_metrics else NA,
    )
    actionable_a_grade_candidate, actionable_grade_reason = _actionable_a_grade_decision(
        symbol_result,
        diagnostics,
        mode=mode,
        direction=direction,
        quality_grade=quality_grade,
        rr=rr,
        failed_gate=failed_gate,
        gates_failed=gates_failed,
        technical_score=technical_score_text,
        opportunity_score=opportunity_score_text,
        min_score_for_idea=min_score_for_idea,
    )
    actionability_state, final_failed_gate, final_block_reason, final_quality_grade = _a_grade_actionability_fields(
        symbol_result,
        diagnostics,
        quality_grade=quality_grade,
        actionable=actionable_a_grade_candidate,
        actionable_reason=actionable_grade_reason,
        failed_gate=failed_gate,
        gates_failed=gates_failed,
        technical_score=technical_score_text,
        opportunity_score=opportunity_score_text,
        min_score_for_idea=min_score_for_idea,
    )
    target_integrity_status = _target_integrity_status_text(diagnostics)
    target_failure = _target_failure_text(diagnostics)
    target_failure_severity = _target_failure_severity_text(
        diagnostics,
        actionability_state=actionability_state,
    )
    target_warning_reason = _target_warning_reason_text(diagnostics)
    a_grade_watch_candidate = False
    requires_limit_fill = actionable_a_grade_candidate or _requires_limit_fill_before_active(symbol_result, diagnostics)
    if (
        not actionable_a_grade_candidate
        and not valid_trade_idea
        and not symbol_result.valid_strategy_modes
        and _status_key(failed_gate)
    ):
        rr_valid = False
    entry_low, entry_high = _entry_zone_values(symbol_result, diagnostics)
    stop_loss = _stop_value(symbol_result, diagnostics)
    targets = _target_values(symbol_result, diagnostics)
    latest_high, latest_low = _latest_observed_range_values(symbol_result, diagnostics)

    observation = LifecycleObservation(
        symbol=symbol_result.symbol,
        mode=mode,
        direction=direction,
        readiness_score=display.readiness_score,
        readiness_label=display.readiness_label,
        quality_score=quality_score,
        quality_grade=quality_grade,
        candidate_quality_grade=quality_grade,
        final_quality_grade=final_quality_grade,
        final_failed_gate=final_failed_gate,
        final_block_reason=final_block_reason,
        target_integrity_status=target_integrity_status,
        target_failure=target_failure,
        target_failure_severity=target_failure_severity,
        target_warning_reason=target_warning_reason,
        actionability_state=actionability_state,
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
        confirmation_timeframe=_display(diagnostics.get('confirmation_timeframe')),
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
        core_status=_display(getattr(symbol_result.status, "value", symbol_result.status)),
        setup_quality_state=quality_state,
        technical_score=technical_score_text,
        opportunity_score=opportunity_score_text,
        min_technical_score="50",
        min_opportunity_score=_display(min_score_for_idea),
        active_rejection_reason=_first_non_na(
            diagnostics.get("active_rejection_reason"),
            diagnostics.get("current_rejection_reason"),
        ),
        active_invalidation_reason=_first_non_na(
            diagnostics.get("active_invalidation_reason"),
            diagnostics.get("current_invalidation_reason"),
        ),
        data_health_failed=_data_health_failed(symbol_result, diagnostics),
        limit_fill_required=requires_limit_fill,
        actionable_a_grade_candidate=actionable_a_grade_candidate,
        a_grade_watch_candidate=a_grade_watch_candidate,
        actionable_grade_reason=actionable_grade_reason,
        entry_filled=(
            _entry_zone_touched_for_result(symbol_result, diagnostics)
            if symbol_result.lifecycle_execution_candles is None else False
        ),
        invalidated=_structural_acceptance_invalidated(pullback_failure_type, acceptance_status, failed_gate),
        expired=_status_key(failed_gate) == "entry_window_expired",
        closed_candle_outcomes_managed=symbol_result.lifecycle_execution_candles is not None,
        structural_anchor=_structural_anchor_from_symbol_result(symbol_result, diagnostics),
    )
    return replace(
        observation,
        confirmation_block_reason=_audit_reason(confirmed_observation_block_reasons(observation)),
    )


def prioritize_watch_symbols(
    symbols: Sequence[str],
    *,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    now: str | None = None,
) -> tuple[str, ...]:
    ordered = tuple(dict.fromkeys(_display(symbol).upper() for symbol in symbols if _display(symbol) != NA))
    active_symbols = active_lifecycle_symbols(ordered, database_path=database_path, now=now)
    active_set = set(active_symbols)
    return (*active_symbols, *(symbol for symbol in ordered if symbol not in active_set))


def active_lifecycle_symbols(
    symbols: Sequence[str] = (),
    *,
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    now: str | None = None,
) -> tuple[str, ...]:
    del now  # Active monitoring is reconstructed solely from persisted lifecycle state.
    ordered = tuple(dict.fromkeys(_display(symbol).upper() for symbol in symbols if _display(symbol) != NA))
    original_index = {symbol: index for index, symbol in enumerate(ordered)}
    with SQLiteSetupLifecycleRepository(database_path) as repository:
        records = repository.get_records_for_states(ACTIVE_LIFECYCLE_MONITORING_STATES)

    best_priority_by_symbol: dict[str, int] = {}
    for record in records:
        rank = lifecycle_monitoring_priority(record.current_state)
        prior_rank = best_priority_by_symbol.get(record.symbol)
        if prior_rank is None or rank < prior_rank:
            best_priority_by_symbol[record.symbol] = rank

    fallback_index = len(ordered)
    return tuple(
        sorted(
            best_priority_by_symbol,
            key=lambda symbol: (
                best_priority_by_symbol[symbol],
                original_index.get(symbol, fallback_index),
                symbol,
            ),
        )
    )


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


def _generation_setup_observable(observation: LifecycleObservation) -> bool:
    return bool(
        observation.valid_trade_idea
        or observation.pullback_and_rr_valid
        or observation.actionable_a_grade_candidate
        or observation.a_grade_watch_candidate
        or observation.sweep_detected
        or observation.readiness_score >= 50
        or _display(observation.readiness_label).upper() in {"WATCH", "HOT WATCH", "VALID SETUP"}
    )


def _generation_terminal_observation(observation: LifecycleObservation) -> bool:
    return bool(
        observation.tp_hit
        or observation.sl_hit
        or observation.invalidated
        or observation.expired
    )


def _structural_anchor_from_symbol_result(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
) -> str:
    for key in (
        "setup_generation_anchor",
        "setup_anchor",
        "execution_sweep_timestamp",
        "sweep_timestamp",
        "sweep_open_timestamp",
        "sweep_structure_id",
        "sweep_id",
    ):
        value = _display(diagnostics.get(key))
        if value != NA:
            return f"{key}|{value}"

    index = _index_or_none(diagnostics.get("execution_sweep_candle_index"))
    candles = tuple(symbol_result.lifecycle_execution_candles or ())
    if index is None or index < 0 or index >= len(candles):
        return NA
    timestamp = _candle_timestamp(candles[index])
    if timestamp == NA:
        return NA
    timeframe = _first_non_na(
        symbol_result.lifecycle_execution_timeframe,
        diagnostics.get("execution_timeframe"),
    )
    return f"execution_sweep|{timeframe}|{timestamp}"


def _candle_timestamp(candle: Any) -> str:
    for key in ("timestamp", "open_timestamp", "opened_at"):
        value = _field_value(candle, key)
        text = _display(value)
        if text != NA:
            return text
    return NA


def _field_value(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key, NA)
    return getattr(value, key, NA)


def _index_or_none(value: Any) -> int | None:
    if value in (None, "", NA):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_health_records(database_path: Path | str, symbols: Sequence[str]) -> dict[str, Any]:
    if not symbols:
        return {}
    return load_symbol_health_records(database_path, symbols)


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
        "processed_symbols": 0,
        "failed_symbols": 0,
        "skipped_not_run_symbols": 0,
        "status": "SUCCESS",
        "errors": [],
        "new_candidates": 0,
        "merged_duplicates": 0,
        "confirmation_pending": 0,
        "confirmed_after_multi_scan": 0,
        "decayed": 0,
        "expired": 0,
        "actionable_a_grade": 0,
        "symbol_health_penalties_applied": 0,
    }


def _record_lifecycle_symbol_error(
    summary: dict[str, Any],
    symbol: str,
    exc: Exception,
) -> None:
    summary["failed_symbols"] = int(summary.get("failed_symbols", 0)) + 1
    errors = summary.setdefault("errors", [])
    errors.append(
        {
            "symbol": symbol,
            "type": type(exc).__name__,
            "detail": str(exc).strip() or type(exc).__name__,
        }
    )


def _add_process_meta(summary: dict[str, Any], meta: Mapping[str, Any]) -> None:
    for key in (
        "new_candidates",
        "merged_duplicates",
        "confirmation_pending",
        "confirmed_after_multi_scan",
        "decayed",
        "expired",
        "actionable_a_grade",
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
        "actionable_a_grade": 1
        if record is not None and record.current_state == SetupLifecycleState.ACTIONABLE_A_GRADE
        else 0,
        "symbol_health_penalties_applied": 1 if symbol_health_penalty_cycles > 0 else 0,
    }


def _log_lifecycle_actionability_audit(
    symbol_result: ScannerSymbolResult,
    observation: LifecycleObservation,
    transition: SetupTransitionResult,
) -> None:
    previous_state = transition.from_state.value if transition.from_state is not None else NA
    new_state = transition.to_state.value
    lifecycle_promotion_reason = transition.reason.value if transition.transitioned else SetupTransitionReason.NO_CHANGE.value
    logger.info(
        "lifecycle_actionability_audit symbol=%s public_decision=%s public_block_reason=%s "
        "actionability_state=%s final_failed_gate=%s final_block_reason=%s "
        "actionable_grade_reason=%s confirmation_block_reason=%s lifecycle_promotion_reason=%s "
        "previous_state=%s new_state=%s",
        symbol_result.symbol,
        "not_evaluated",
        NA,
        observation.actionability_state,
        observation.final_failed_gate,
        observation.final_block_reason,
        observation.actionable_grade_reason,
        observation.confirmation_block_reason,
        lifecycle_promotion_reason,
        previous_state,
        new_state,
    )


def _audit_reason(reasons: Sequence[str]) -> str:
    cleaned = tuple(_display(reason) for reason in reasons if _display(reason) != NA)
    return ";".join(cleaned) if cleaned else NA


def _outcome_analytics_record(
    repository: SQLiteSetupLifecycleRepository,
    transition: SetupTransitionResult,
    observation: LifecycleObservation,
    outcome_progress: SetupLifecycleOutcomeProgress | None = None,
) -> SetupOutcomeAnalyticsRecord | None:
    record = transition.record
    if record is None:
        return None
    if stored_plan_geometry_failure(record) is not None:
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
        "outcome_progress": outcome_progress.model_dump(mode="json") if outcome_progress is not None else None,
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
    # TP_HIT is terminal only after TP3; TP1 and TP2 remain internal milestones.
    if state == SetupLifecycleState.TP_HIT:
        return "TP3_HIT"
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


def _actionable_a_grade_decision(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    *,
    mode: str,
    direction: str,
    quality_grade: str,
    rr: Decimal | None,
    failed_gate: str,
    gates_failed: Sequence[str],
    technical_score: Any = NA,
    opportunity_score: Any = NA,
    min_score_for_idea: Any = Decimal("80"),
) -> tuple[bool, str]:
    grade_key = _quality_grade_key(quality_grade)
    if grade_key not in ACTIONABLE_A_GRADE_GRADES:
        return False, "grade_below_a_minus"
    if _display(symbol_result.symbol) == NA or _display(symbol_result.symbol).upper() == NA:
        return False, "missing_symbol"
    side = _display(direction).lower()
    if side not in {"long", "short"}:
        return False, "missing_side"
    if _display(symbol_result.error_message) != NA:
        return False, "scanner_error"
    if _status_keys(symbol_result) & A_GRADE_HARD_STATUS_BLOCKERS:
        return False, "hard_status_blocked"
    if _risk_validation_failed(symbol_result, diagnostics):
        return False, "risk_validation_failed"

    soft_target_warning = _soft_target_warning(diagnostics)
    if _target_integrity_failed(diagnostics) and not soft_target_warning:
        return False, "target_integrity_failed"
    if not (_target_integrity_passed(diagnostics) or soft_target_warning):
        return False, "target_integrity_not_passed"

    technical_value = _decimal_or_none(technical_score)
    opportunity_value = _decimal_or_none(opportunity_score)
    technical_min = Decimal("90") if soft_target_warning else Decimal("50")
    if technical_value is None and soft_target_warning:
        return False, "technical_score_missing_for_target_caution"
    if technical_value is not None and technical_value < technical_min:
        return False, f"technical_score_below_min:{_display(technical_value)}<{_display(technical_min)}"
    opportunity_min = Decimal("90") if soft_target_warning else (_decimal_or_none(min_score_for_idea) or Decimal("80"))
    if opportunity_value is None and soft_target_warning:
        return False, "opportunity_score_missing_for_target_caution"
    if opportunity_value is not None and opportunity_value < opportunity_min:
        return False, f"opportunity_score_below_min:{_display(opportunity_value)}<{_display(opportunity_min)}"
    if not _clean_pullback_acceptance(diagnostics, failed_gate=failed_gate):
        return False, "pullback_acceptance_not_clean"
    if not _confirmation_structure_shift_passed_when_available(symbol_result, diagnostics):
        return False, "confirmation_structure_shift_not_passed"

    gate_keys = _gate_keys(failed_gate, gates_failed, diagnostics)
    soft_allowed_gates = TARGET_CAUTION_ALLOWED_GATES if soft_target_warning else set()
    if soft_target_warning and gate_keys & {"missing_rr", "rr_below_minimum", "rr_too_low", "challenge_rr_below_3"}:
        return False, "target_caution_rr_gate_present"
    hard_gates = (gate_keys - soft_allowed_gates) & ACTIONABLE_A_GRADE_FATAL_GATE_BLOCKERS
    if hard_gates:
        return False, "fatal_failed_gate:" + ",".join(sorted(hard_gates))
    actionable_gates = {gate for gate in gate_keys if gate and gate != "n_a"}
    unexpected_gates = actionable_gates - ACTIONABLE_A_GRADE_ALLOWED_NON_FATAL_GATES - soft_allowed_gates
    if unexpected_gates:
        return False, "unexpected_failed_gate:" + ",".join(sorted(unexpected_gates))
    if soft_target_warning and not _market_regime_acceptable(symbol_result, diagnostics):
        return False, "market_regime_not_acceptable_for_target_caution"

    entry_low, entry_high = (_decimal_or_none(value) for value in _entry_zone_values(symbol_result, diagnostics))
    stop = _decimal_or_none(_stop_value(symbol_result, diagnostics))
    targets = _target_values(symbol_result, diagnostics)
    tp1 = _decimal_or_none(targets[0])
    tp2 = _decimal_or_none(targets[1])
    tp3 = _decimal_or_none(targets[2])
    invalidation = _invalidation_value(symbol_result, diagnostics)

    if entry_low is None or entry_high is None:
        return False, "missing_entry_zone"
    if stop is None:
        return False, "missing_stop"
    if tp1 is None or tp2 is None or tp3 is None:
        return False, "missing_targets"
    if _display(invalidation) == NA:
        return False, "missing_invalidation"
    if rr is None:
        return False, "rr_missing"
    if rr < ACTIONABLE_A_GRADE_MIN_RR:
        return False, f"rr_below_actionable_min:{_display(rr)}<{_display(ACTIONABLE_A_GRADE_MIN_RR)}"
    if not _trade_map_geometry_valid(
        side=side,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop,
        targets=(tp1, tp2, tp3),
    ):
        return False, "trade_map_geometry_invalid"
    return True, "target_caution_actionable" if soft_target_warning else "clean_a_grade_trade_map"


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


def _setup_quality_state_text(symbol_result: ScannerSymbolResult) -> str:
    quality_state = getattr(symbol_result.setup_quality, "quality_state", NA)
    return _display(getattr(quality_state, "value", quality_state))


def _opportunity_score(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> Any:
    score_result = symbol_result.score_result
    trade_idea = symbol_result.trade_idea
    return _first_non_na(
        getattr(score_result, "total_score", NA) if score_result is not None else NA,
        getattr(trade_idea, "confidence_score", NA) if trade_idea is not None else NA,
        diagnostics.get("opportunity_score"),
        diagnostics.get("total_score"),
    )


def _data_health_failed(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> bool:
    if any(
        _sequence_values(value)
        for value in (
            symbol_result.missing_data,
            symbol_result.unverified_data,
            symbol_result.strategy_missing_data,
            symbol_result.strategy_unverified_data,
            symbol_result.derivatives_missing_data,
            symbol_result.derivatives_unverified_data,
            diagnostics.get("missing_data"),
            diagnostics.get("unverified_data"),
        )
    ):
        return True
    score_result = symbol_result.score_result
    return bool(
        score_result is not None
        and (
            _sequence_values(getattr(score_result, "missing_data", ()))
            or _sequence_values(getattr(score_result, "unverified_data", ()))
        )
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


def _target_integrity_passed(diagnostics: Mapping[str, Any]) -> bool:
    status = _status_key(
        _first_non_na(
            diagnostics.get("target_integrity_status"),
            diagnostics.get("target_status"),
            diagnostics.get("target_validation_status"),
        )
    )
    if status in TARGET_INTEGRITY_PASSED_KEYS:
        return True
    return "target_integrity" in {_status_key(value) for value in _sequence_values(diagnostics.get("gates_passed"))}


def _target_integrity_status_text(diagnostics: Mapping[str, Any]) -> str:
    status = _first_non_na(
        diagnostics.get("target_integrity_status"),
        diagnostics.get("target_status"),
        diagnostics.get("target_validation_status"),
    )
    if _display(status) != NA:
        return _display(status)
    if _target_integrity_failed(diagnostics):
        return "blocked"
    if _target_integrity_passed(diagnostics):
        return "passed"
    return NA


def _target_failure_text(diagnostics: Mapping[str, Any]) -> str:
    return _first_non_na(
        diagnostics.get("target_failure"),
        diagnostics.get("target_failure_type"),
        diagnostics.get("target_integrity_reason"),
        diagnostics.get("target_integrity_warning"),
    )


def _target_warning_reason_text(diagnostics: Mapping[str, Any]) -> str:
    return _first_non_na(
        diagnostics.get("target_warning_reason"),
        diagnostics.get("target_integrity_warning"),
        diagnostics.get("target_integrity_reason"),
    )


def _target_failure_severity_text(
    diagnostics: Mapping[str, Any],
    *,
    actionability_state: str = NA,
) -> str:
    if _status_key(actionability_state) == "a_grade_actionable_target_caution":
        return TARGET_FAILURE_SEVERITY_CAUTION
    explicit = _display(diagnostics.get("target_failure_severity"))
    if explicit != NA:
        return explicit
    if _soft_target_warning(diagnostics):
        return TARGET_FAILURE_SEVERITY_SOFT
    if _target_integrity_failed(diagnostics):
        return TARGET_FAILURE_SEVERITY_FATAL
    if _target_integrity_passed(diagnostics):
        return TARGET_FAILURE_SEVERITY_PASSED
    return NA


def _soft_target_warning(diagnostics: Mapping[str, Any]) -> bool:
    if not _target_inside_chop_warning(diagnostics):
        return False
    severity = _status_key(diagnostics.get("target_failure_severity"))
    if severity in {"soft_target_warning", "target_caution_actionable"}:
        return True
    status = _status_key(
        _first_non_na(
            diagnostics.get("target_integrity_status"),
            diagnostics.get("target_status"),
            diagnostics.get("target_validation_status"),
        )
    )
    if status in TARGET_CAUTION_STATUS_KEYS:
        return True
    return bool({"target_inside_chop", "target_integrity"} & _target_failure_keys(diagnostics))


def _target_inside_chop_warning(diagnostics: Mapping[str, Any]) -> bool:
    keys = _target_failure_keys(diagnostics)
    if keys & TARGET_INSIDE_CHOP_KEYS:
        return True
    haystack = _status_key(
        " ".join(
            _display(value)
            for value in (
                diagnostics.get("target_failure"),
                diagnostics.get("target_failure_type"),
                diagnostics.get("target_integrity_reason"),
                diagnostics.get("target_integrity_warning"),
                diagnostics.get("target_warning_reason"),
            )
            if _display(value) != NA
        )
    )
    return "tp2" in haystack and ("chop" in haystack or "range" in haystack)


def _target_failure_keys(diagnostics: Mapping[str, Any]) -> set[str]:
    values: list[Any] = [
        diagnostics.get("target_failure"),
        diagnostics.get("target_failure_type"),
        diagnostics.get("target_integrity_reason"),
        diagnostics.get("target_integrity_warning"),
        diagnostics.get("target_warning_reason"),
    ]
    values.extend(_sequence_values(diagnostics.get("gates_failed")))
    return {_status_key(value) for value in values if _status_key(value)}


def _market_regime_acceptable(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> bool:
    if bool(getattr(symbol_result, "regime_blocked", False)):
        return False
    blocked = {
        "regime_compatibility",
        "regime_blocked",
        "hard_regime_block",
        "market_condition_blocked",
        "market_condition_not_ready",
        "btc_eth_regime_blocked",
        "rejected_by_regime",
        "weak_regime_fit",
    }
    if (_gate_keys(NA, (), diagnostics) | _status_keys(symbol_result)) & blocked:
        return False
    for value in (
        getattr(symbol_result, "regime_compatibility_label", NA),
        getattr(symbol_result, "regime_state", NA),
        diagnostics.get("regime_compatibility_label"),
        diagnostics.get("market_condition_status"),
        diagnostics.get("regime_state"),
    ):
        key = _status_key(value)
        if key in blocked or any(token in key for token in ("blocked", "rejected", "weak_regime", "not_ready")):
            return False
    return True


def _a_grade_actionability_fields(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    *,
    quality_grade: str,
    actionable: bool,
    actionable_reason: str,
    failed_gate: str,
    gates_failed: Sequence[str],
    technical_score: Any,
    opportunity_score: Any,
    min_score_for_idea: Any,
) -> tuple[str, str, str, str]:
    candidate_grade = _display(quality_grade)
    if _quality_grade_key(candidate_grade) not in ACTIONABLE_A_GRADE_GRADES:
        return NOT_A_GRADE_CANDIDATE_STATE, _text_final_gate(failed_gate), NA, candidate_grade
    soft_target_warning = _soft_target_warning(diagnostics)
    if actionable:
        state = (
            A_GRADE_ACTIONABLE_TARGET_CAUTION_STATE
            if soft_target_warning or _status_key(actionable_reason) == "target_caution_actionable"
            else A_GRADE_ACTIONABLE_STATE
        )
        return state, NA, NA, candidate_grade

    gate_keys = _gate_keys(failed_gate, gates_failed, diagnostics)
    reason_key = _status_key(actionable_reason)
    status_keys = _status_keys(symbol_result)
    final_gate = _text_final_gate(failed_gate)

    target_gate_blocked = gate_keys & {"target_integrity", "target_integrity_failed", "target_order_invalid", "targets_not_monotonic"}
    if (_target_integrity_failed(diagnostics) and not soft_target_warning) or (target_gate_blocked and not soft_target_warning):
        return (
            A_GRADE_BLOCKED_BY_TARGET_STATE,
            "target_integrity",
            "A-grade candidate, but blocked by target integrity.",
            "Blocked",
        )
    if "entry_window_expired" in gate_keys or "entry_window_expired" in reason_key:
        return (
            A_GRADE_BLOCKED_BY_ENTRY_WINDOW_STATE,
            "entry_window_expired",
            "A-grade candidate, but blocked by expired entry window.",
            "Blocked",
        )
    if gate_keys & {"trust_meter_below_minimum", "challenge_trust_below_85"} or "trust" in reason_key:
        return (
            A_GRADE_BLOCKED_BY_TRUST_STATE,
            "trust_meter_below_minimum",
            "A-grade candidate, but blocked by trust meter.",
            "Blocked",
        )
    if (
        "rejected_by_scoring" in status_keys
        or gate_keys & {"scoring", "quality_filter", "technical_score_below_min", "opportunity_score_below_min"}
        or _score_below_minimum(technical_score, Decimal("50"))
        or _score_below_minimum(opportunity_score, _decimal_or_none(min_score_for_idea) or Decimal("80"))
        or "score" in reason_key
        or (reason_key == "hard_status_blocked" and "rejected_by_scoring" in status_keys)
    ):
        return (
            A_GRADE_BLOCKED_BY_SCORING_STATE,
            "scoring",
            "A-grade candidate, but blocked by final scoring.",
            "Blocked",
        )
    return (
        A_GRADE_BLOCKED_BY_FINAL_GATES_STATE,
        final_gate,
        "A-grade candidate, but blocked by final gates.",
        "Blocked",
    )


def _score_below_minimum(value: Any, minimum: Decimal) -> bool:
    parsed = _decimal_or_none(value)
    return parsed is not None and parsed < minimum


def _text_final_gate(value: Any) -> str:
    text = _display(value)
    return text if text != NA else NA


def _clean_pullback_acceptance(diagnostics: Mapping[str, Any], *, failed_gate: Any) -> bool:
    blocked = {
        "body_acceptance_failure",
        "pullback_beyond_786",
        "pullback_too_deep",
        "structural_breakdown",
    }
    if _status_key(failed_gate) in blocked:
        return False
    if any(_status_key(value) in blocked for value in _sequence_values(diagnostics.get("gates_failed"))):
        return False
    status = _status_key(diagnostics.get("pullback_zone_status"))
    if status in {"accepted", "clean", "pass", "passed", "valid"}:
        return True
    return "pullback_zone" in {_status_key(value) for value in _sequence_values(diagnostics.get("gates_passed"))}


def _confirmation_structure_shift_passed_when_available(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
) -> bool:
    status = _status_key(diagnostics.get("confirmation_structure_shift_status"))
    if status in {"confirmed", "pass", "passed", "valid"}:
        return True
    if status in {"blocked", "failed", "fail", "rejected", "reject"}:
        return False
    if bool(symbol_result.bos_detected or symbol_result.choch_detected):
        return True
    return "bos_choch" in {_status_key(value) for value in _sequence_values(diagnostics.get("gates_passed"))} or not status


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
    "active_lifecycle_symbols",
]
