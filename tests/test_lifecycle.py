from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal

import pytest

from app.lifecycle import service as lifecycle_service_module
from app.agents.trade_idea import create_trade_idea
from app.analytics.setup_quality import SetupQualityGrade, SetupQualityResult, SetupQualityState
from app.analytics.symbol_health import SymbolHealthRecord
from app.data.dtos import NA
from app.lifecycle.identity import generation_rotation_reason, new_setup_generation_id
from app.lifecycle.models import SetupLifecycleEvent, SetupLifecycleRecord, SetupLifecycleState, SetupTransitionReason
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import apply_lifecycle_to_run_result, prioritize_watch_symbols
from app.lifecycle.state_machine import LifecycleObservation, evaluate_lifecycle_transition, transition_record
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from app.research.queries import ResearchFilters, build_research_report
from app.storage.symbol_health import save_symbol_health_records
from scripts import run_scan


def _record(
    state: SetupLifecycleState,
    *,
    lifecycle_id: str = "life_1",
    symbol: str = "BTCUSDT",
    mode: str = "swing",
    direction: str = "long",
    now: str = "2026-05-18T09:00:00+00:00",
) -> SetupLifecycleRecord:
    levels = (
        {
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "95",
            "tp1": "110",
            "tp2": "117",
            "tp3": "124",
        }
        if direction == "long"
        else {
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "107",
            "tp1": "92",
            "tp2": "85",
            "tp3": "78",
        }
    )
    return SetupLifecycleRecord(
        lifecycle_id=lifecycle_id,
        symbol=symbol,
        mode=mode,
        direction=direction,
        current_state=state,
        first_seen_at=now,
        last_seen_at=now,
        last_transition_at=now,
        readiness_score=60,
        quality_score=50,
        invalidation_reason="Closed structure beyond the stored stop invalidates the plan.",
        invalidation_logic="Closed structure beyond the stored stop invalidates the plan.",
        rr="3.2",
        setup_identity=f"{symbol}|{mode}|{direction}|seed",
        **levels,
    )


def _insert_lifecycle_history(
    repository: SQLiteSetupLifecycleRepository,
    *,
    lifecycle_id: str,
    symbol: str,
    states: tuple[SetupLifecycleState, ...],
    timestamps: tuple[str, ...],
    failed_gate: str = "N/A",
    invalidation_reason: str = "N/A",
    regime_state: str = "trend_expansion",
    readiness_score: int = 70,
    quality_score: int = 60,
    last_seen_at: str | None = None,
) -> None:
    assert len(states) == len(timestamps)
    previous_state = states[-2] if len(states) > 1 else None
    record = SetupLifecycleRecord(
        lifecycle_id=lifecycle_id,
        symbol=symbol,
        mode="swing",
        direction="long",
        current_state=states[-1],
        previous_state=previous_state,
        first_seen_at=timestamps[0],
        last_seen_at=last_seen_at or timestamps[-1],
        last_transition_at=timestamps[-1],
        failed_gate=failed_gate,
        readiness_score=readiness_score,
        quality_score=quality_score,
        regime_state=regime_state,
        invalidation_reason=invalidation_reason,
        archived_at=timestamps[-1] if states[-1] == SetupLifecycleState.ARCHIVED else None,
    )
    repository.upsert_record(record)
    for index, state in enumerate(states):
        repository.insert_event(
            SetupLifecycleEvent(
                lifecycle_id=lifecycle_id,
                timestamp=timestamps[index],
                symbol=symbol,
                from_state=states[index - 1] if index > 0 else None,
                to_state=state,
                reason=SetupTransitionReason.NO_CHANGE,
                readiness_score=readiness_score,
                quality_score=quality_score,
                failed_gate=failed_gate,
            )
        )


def _seed_phase_37_lifecycle_database(db_path) -> None:
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        _insert_lifecycle_history(
            repository,
            lifecycle_id="life_btc",
            symbol="BTCUSDT",
            states=(
                SetupLifecycleState.WATCHLISTED,
                SetupLifecycleState.STALKING,
                SetupLifecycleState.TRIGGERED,
                SetupLifecycleState.CONFIRMED,
                SetupLifecycleState.EXECUTING,
                SetupLifecycleState.TP_HIT,
            ),
            timestamps=(
                "2026-05-18T09:00:00+00:00",
                "2026-05-18T10:00:00+00:00",
                "2026-05-18T11:00:00+00:00",
                "2026-05-18T12:00:00+00:00",
                "2026-05-18T13:00:00+00:00",
                "2026-05-18T14:00:00+00:00",
            ),
            readiness_score=92,
            quality_score=88,
        )
        _insert_lifecycle_history(
            repository,
            lifecycle_id="life_eth",
            symbol="ETHUSDT",
            states=(
                SetupLifecycleState.WATCHLISTED,
                SetupLifecycleState.STALKING,
                SetupLifecycleState.TRIGGERED,
                SetupLifecycleState.INVALIDATED,
            ),
            timestamps=(
                "2026-05-18T09:00:00+00:00",
                "2026-05-18T09:30:00+00:00",
                "2026-05-18T10:00:00+00:00",
                "2026-05-18T10:30:00+00:00",
            ),
            failed_gate="rr_below_minimum",
            invalidation_reason="Pullback invalidated.",
            readiness_score=61,
            quality_score=54,
        )
        _insert_lifecycle_history(
            repository,
            lifecycle_id="life_sol",
            symbol="SOLUSDT",
            states=(SetupLifecycleState.WATCHLISTED, SetupLifecycleState.STALKING),
            timestamps=("2026-05-18T09:00:00+00:00", "2026-05-18T09:15:00+00:00"),
            failed_gate="missing_confirmation_structure_shift",
            readiness_score=74,
            quality_score=63,
            last_seen_at="2026-05-18T14:00:00+00:00",
        )
        _insert_lifecycle_history(
            repository,
            lifecycle_id="life_ada",
            symbol="ADAUSDT",
            states=(
                SetupLifecycleState.WATCHLISTED,
                SetupLifecycleState.STALKING,
                SetupLifecycleState.TRIGGERED,
                SetupLifecycleState.CONFIRMED,
            ),
            timestamps=(
                "2026-05-18T09:00:00+00:00",
                "2026-05-18T10:00:00+00:00",
                "2026-05-18T11:00:00+00:00",
                "2026-05-18T11:30:00+00:00",
            ),
            readiness_score=84,
            quality_score=79,
            last_seen_at="2026-05-18T14:00:00+00:00",
        )
        _insert_lifecycle_history(
            repository,
            lifecycle_id="life_xrp",
            symbol="XRPUSDT",
            states=(SetupLifecycleState.WATCHLISTED,),
            timestamps=("2026-05-18T09:00:00+00:00",),
            failed_gate="missing_confirmed_sweep",
            readiness_score=51,
            quality_score=44,
            last_seen_at="2026-05-18T14:00:00+00:00",
        )
        _insert_lifecycle_history(
            repository,
            lifecycle_id="life_bnb",
            symbol="BNBUSDT",
            states=(SetupLifecycleState.WATCHLISTED,),
            timestamps=("2026-05-18T09:30:00+00:00",),
            failed_gate="missing_confirmed_sweep",
            readiness_score=52,
            quality_score=45,
            last_seen_at="2026-05-18T14:00:00+00:00",
        )


def _observation(**overrides) -> LifecycleObservation:
    data = {
        "symbol": "BTCUSDT",
        "mode": "swing",
        "direction": "long",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "117",
        "tp3": "124",
        "rr": "3.2",
        "invalidation_reason": "Invalid if price accepts below 95.",
        "readiness_score": 70,
        "readiness_label": "WATCH",
        "quality_score": 65,
        "failed_gate": "rr_below_minimum",
    }
    data.update(overrides)
    return LifecycleObservation(**data)


def _confirmed_observation(**overrides) -> LifecycleObservation:
    data = {
        "symbol": "BTCUSDT",
        "mode": "swing",
        "direction": "long",
        "readiness_score": 85,
        "readiness_label": "VALID SETUP",
        "quality_score": 82,
        "quality_grade": "B+",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "117",
        "tp3": "124",
        "rr": "3.2",
        "failed_gate": NA,
        "invalidation_reason": "Invalid if price accepts below 95.",
        "sweep_detected": True,
        "structure_shift_detected": True,
        "pullback_valid": True,
        "rr_valid": True,
        "valid_trade_idea": True,
        "core_status": "idea_created",
        "setup_quality_state": "high_quality_trade",
        "technical_score": "70",
        "opportunity_score": "88",
        "min_technical_score": "50",
        "min_opportunity_score": "80",
    }
    data.update(overrides)
    return LifecycleObservation(**data)


def _scan_result(symbol_result: ScannerSymbolResult) -> ScannerRunResult:
    config = ScannerRunConfig.model_validate(
        {
            "symbols": [symbol_result.symbol],
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )
    return ScannerRunResult(
        config=config,
        results=(symbol_result,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )


def _near_miss_symbol(symbol: str = "BTCUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "failed",
                "first_failed_gate": "missing_confirmation_structure_shift",
                "gates_passed": ("sweep",),
                "gates_failed": ("missing_confirmation_structure_shift",),
            }
        },
    )


def _too_deep_pullback_symbol(symbol: str = "BTCUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "failed",
                "first_failed_gate": "pullback_too_deep",
                "gates_passed": ("sweep", "bos_choch"),
                "gates_failed": ("pullback_too_deep",),
                "pullback_depth_ratio": Decimal("0.82"),
                "fib_alignment_status": "pullback_too_deep",
                "pullback_failure_reason": "Pullback tagged beyond 0.786 before entry.",
            }
        },
    )


def _wick_reclaim_symbol(symbol: str = "BTCUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "failed",
                "first_failed_gate": "wick_sweep_reclaim",
                "gates_passed": ("sweep", "bos_choch"),
                "gates_failed": ("wick_sweep_reclaim",),
                "pullback_depth_ratio": Decimal("0.82"),
                "wick_depth_ratio": Decimal("0.82"),
                "close_depth_ratio": Decimal("0.76"),
                "body_acceptance_ratio": Decimal("0.76"),
                "reclaim_detected": True,
                "reclaim_strength": "weak",
                "candles_below_fib_zone": 0,
                "acceptance_status": "WICK_SWEEP_RECLAIM",
                "structural_reclaim_status": "intact",
                "pullback_failure_reason": "Wick swept beyond 0.786 but body reclaimed the zone.",
            }
        },
    )


def _body_acceptance_symbol(symbol: str = "BTCUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "failed",
                "first_failed_gate": "body_acceptance_failure",
                "gates_passed": ("sweep", "bos_choch"),
                "gates_failed": ("body_acceptance_failure",),
                "pullback_depth_ratio": Decimal("0.82"),
                "wick_depth_ratio": Decimal("0.82"),
                "close_depth_ratio": Decimal("0.82"),
                "body_acceptance_ratio": Decimal("0.82"),
                "reclaim_detected": False,
                "reclaim_strength": "N/A",
                "candles_below_fib_zone": 1,
                "acceptance_status": "BODY_ACCEPTANCE_FAILURE",
                "structural_reclaim_status": "intact",
                "pullback_failure_reason": "Candle body closed beyond 0.786 before entry.",
            }
        },
    )


def _setup_quality_result(grade: SetupQualityGrade, *, score: int = 90) -> SetupQualityResult:
    return SetupQualityResult(
        quality_state=SetupQualityState.HIGH_QUALITY_TRADE,
        quality_grade=grade,
        quality_score=score,
        tradeability_score=score,
        profitability_edge_score=score,
        execution_risk_score=max(0, 100 - score),
        strongest_factors=("structure", "RR meets threshold"),
        weakest_factors=(),
        decision_reason="Synthetic A-grade setup.",
        action_label="Trade candidate",
    )


def _trade_idea(
    *,
    symbol: str = "BTCUSDT",
    direction: str = "long",
    entry_low: Decimal = Decimal("100"),
    entry_high: Decimal = Decimal("102"),
    stop: Decimal = Decimal("95"),
    rr: Decimal = Decimal("3.2"),
    opportunity_score: Decimal = Decimal("88"),
):
    return create_trade_idea(
        {
            "symbol": symbol,
            "exchange": "binance",
            "market_type": "perpetual",
            "direction": direction,
            "timeframe": "15m",
            "setup_type": "liquidity_grab_pullback_swing",
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": stop,
            "take_profit_targets": (
                (entry_high + Decimal("8"), entry_high + Decimal("15"), entry_high + Decimal("22"))
                if direction == "long"
                else (entry_low - Decimal("8"), entry_low - Decimal("15"), entry_low - Decimal("22"))
            ),
            "invalidation": f"Invalid if price accepts beyond {stop}.",
            "opportunity_score": opportunity_score,
            "opportunity_grade": "A",
            "opportunity_decision": "alert_candidate",
            "risk_approved": True,
            "best_rr": rr,
            "technical_summary": "Sweep and reclaim into valid pullback.",
            "derivatives_summary": "Funding normal while open interest is rising.",
            "confirmed_facts": ("LTF BOS/CHoCH confirmed.",),
            "cancel_condition": "Cancel if price accepts beyond invalidation.",
        }
    )


def _a_grade_watch_symbol(
    *,
    grade: SetupQualityGrade = SetupQualityGrade.A,
    symbol: str = "BTCUSDT",
    latest_high: object = Decimal("110"),
    latest_low: object = Decimal("108"),
    current_price: object = NA,
    status: ScannerPipelineStatus = ScannerPipelineStatus.SCANNED_NO_SETUP,
    diagnostics_overrides: dict[str, object] | None = None,
) -> ScannerSymbolResult:
    diagnostics: dict[str, object] = {
        "mode": "swing",
        "bias": "long",
        "execution_sweep_status": "passed",
        "confirmation_structure_shift_status": "passed",
        "pullback_zone_status": "valid",
        "first_failed_gate": "limit_zone_not_touched",
        "gates_passed": ("sweep", "bos_choch", "pullback_zone", "target_integrity"),
        "gates_failed": ("limit_zone_not_touched",),
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop": Decimal("95"),
        "tp1": Decimal("110"),
        "tp2": Decimal("115"),
        "tp3": Decimal("120"),
        "rr_to_tp2": Decimal("3.2"),
        "invalidation": "Invalid if price accepts below 95.",
        "quality_grade": grade.value,
    }
    diagnostics.update(diagnostics_overrides or {})
    return ScannerSymbolResult(
        symbol=symbol,
        status=status,
        status_history=(status,),
        current_price=current_price,
        latest_high=latest_high,
        latest_low=latest_low,
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={"swing": diagnostics},
        setup_quality=_setup_quality_result(grade, score=92 if grade == SetupQualityGrade.A_PLUS else 88),
    )


def _confirmed_candidate_symbol(
    *,
    symbol: str = "BTCUSDT",
    direction: str = "long",
    entry_low: Decimal = Decimal("100"),
    entry_high: Decimal = Decimal("102"),
    stop: Decimal = Decimal("95"),
    quality_grade: SetupQualityGrade = SetupQualityGrade.B_PLUS,
    rr: Decimal = Decimal("3.2"),
    technical_score: object = 70,
    opportunity_score: Decimal = Decimal("88"),
) -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        latest_high=Decimal("110"),
        latest_low=Decimal("108"),
        technical_score=technical_score,
        trade_idea=_trade_idea(
            symbol=symbol,
            direction=direction,
            entry_low=entry_low,
            entry_high=entry_high,
            stop=stop,
            rr=rr,
            opportunity_score=opportunity_score,
        ),
        valid_strategy_modes=("swing",),
        rejected_strategy_modes=(),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": direction,
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": (),
                "entry_low": entry_low,
                "entry_high": entry_high,
                "stop": stop,
                "tp1": entry_high + Decimal("8") if direction == "long" else entry_low - Decimal("8"),
                "tp2": entry_high + Decimal("15") if direction == "long" else entry_low - Decimal("15"),
                "tp3": entry_high + Decimal("22") if direction == "long" else entry_low - Decimal("22"),
                "rr_to_tp2": rr,
                "opportunity_score": opportunity_score,
                "invalidation": f"Invalid if price accepts beyond {stop}.",
                "quality_grade": quality_grade.value,
            }
        },
        setup_quality=_setup_quality_result(quality_grade, score=82),
    )


def _public_watchlist_candidate_symbol(
    *,
    symbol: str = "BTCUSDT",
    failed_gate: str = "rr_below_minimum",
    rr: object = Decimal("2.6"),
    entry_low: object = Decimal("100"),
    entry_high: object = Decimal("102"),
    stop: object = Decimal("95"),
    tp1: object = Decimal("110"),
    quality_grade: SetupQualityGrade = SetupQualityGrade.B_PLUS,
) -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        latest_high=Decimal("110"),
        latest_low=Decimal("108"),
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "first_failed_gate": failed_gate,
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": (failed_gate,),
                "entry_low": entry_low,
                "entry_high": entry_high,
                "stop": stop,
                "tp1": tp1,
                "tp2": Decimal("117") if tp1 != NA else NA,
                "tp3": Decimal("124") if tp1 != NA else NA,
                "rr_to_tp2": rr,
                "invalidation": "Invalid if price accepts below 95." if stop != NA else NA,
                "quality_grade": quality_grade.value,
                "confirmation_needed": "Wait for clean structure confirmation.",
            }
        },
        setup_quality=_setup_quality_result(quality_grade, score=82),
    )


def _apply_single_lifecycle(symbol_result: ScannerSymbolResult, tmp_path, *, now: str):
    return apply_lifecycle_to_run_result(
        _scan_result(symbol_result),
        database_path=tmp_path / "a_grade_lifecycle.db",
        scan_run_id="run-a-grade",
        now=now,
    ).results[0]


def test_lifecycle_structure_shift_notes_follow_configured_timeframe_and_read_legacy_m5() -> None:
    stalking = _record(SetupLifecycleState.STALKING)
    transition = evaluate_lifecycle_transition(
        stalking,
        _observation(
            sweep_detected=True,
            structure_shift_detected=True,
            confirmation_timeframe="15m",
        ),
        lifecycle_id=stalking.lifecycle_id,
        now="2026-05-18T09:10:00+00:00",
    )

    assert transition.reason == SetupTransitionReason.STRUCTURE_SHIFT_CONFIRMED
    assert transition.notes == "15m BOS/CHoCH confirmed after sweep."

    historical_event = SetupLifecycleEvent.model_validate(
        {
            "lifecycle_id": "legacy_5m",
            "timestamp": "2026-05-01T09:10:00+00:00",
            "symbol": "BTCUSDT",
            "from_state": SetupLifecycleState.STALKING.value,
            "to_state": SetupLifecycleState.TRIGGERED.value,
            "reason": SetupTransitionReason.STRUCTURE_SHIFT_CONFIRMED.value,
            "notes": "5m BOS/CHoCH confirmed after sweep.",
        }
    )

    assert historical_event.notes == "5m BOS/CHoCH confirmed after sweep."

def test_valid_state_progression() -> None:
    initial = evaluate_lifecycle_transition(
        None,
        _observation(readiness_score=55, readiness_label="WATCH"),
        lifecycle_id="life_1",
        now="2026-05-18T09:00:00+00:00",
    ).record
    assert initial is not None
    assert initial.current_state == SetupLifecycleState.WATCHLISTED

    stalking = evaluate_lifecycle_transition(
        initial,
        _observation(sweep_detected=True),
        lifecycle_id="life_1",
        now="2026-05-18T09:05:00+00:00",
    )
    assert stalking.to_state == SetupLifecycleState.STALKING
    assert stalking.reason == SetupTransitionReason.SWEEP_APPEARED

    triggered = evaluate_lifecycle_transition(
        stalking.record,
        _observation(sweep_detected=True, structure_shift_detected=True),
        lifecycle_id="life_1",
        now="2026-05-18T09:10:00+00:00",
    )
    assert triggered.to_state == SetupLifecycleState.TRIGGERED

    pending_confirmation = evaluate_lifecycle_transition(
        triggered.record,
        _confirmed_observation(invalidation_reason="Invalid below stop."),
        lifecycle_id="life_1",
        now="2026-05-18T09:15:00+00:00",
    )
    assert pending_confirmation.to_state == SetupLifecycleState.TRIGGERED
    assert pending_confirmation.record is not None
    assert pending_confirmation.record.confirmation_count == 1

    confirmed = evaluate_lifecycle_transition(
        pending_confirmation.record,
        _confirmed_observation(invalidation_reason="Invalid below stop."),
        lifecycle_id="life_1",
        now="2026-05-18T09:20:00+00:00",
    )
    assert confirmed.to_state == SetupLifecycleState.CONFIRMED

    executing = evaluate_lifecycle_transition(
        confirmed.record,
        _confirmed_observation(invalidation_reason="Invalid below stop."),
        lifecycle_id="life_1",
        now="2026-05-18T09:25:00+00:00",
    )
    assert executing.to_state == SetupLifecycleState.EXECUTING


def test_a_plus_complete_map_enters_a_grade_watch_without_valid_status(tmp_path) -> None:
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(grade=SetupQualityGrade.A_PLUS),
        tmp_path,
        now="2026-05-18T09:00:00+00:00",
    )

    assert symbol_result.valid_strategy_modes == ()
    assert symbol_result.trade_idea is None
    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.EXECUTING


def test_a_complete_map_enters_a_grade_watch_without_valid_status(tmp_path) -> None:
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(grade=SetupQualityGrade.A),
        tmp_path,
        now="2026-05-18T09:00:00+00:00",
    )

    assert symbol_result.valid_strategy_modes == ()
    assert symbol_result.trade_idea is None
    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.ACTIONABLE_A_GRADE


def test_a_minus_clean_trade_map_with_rr_2_5_becomes_actionable_a_grade(tmp_path) -> None:
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(
            grade=SetupQualityGrade.A_MINUS,
            diagnostics_overrides={
                "rr_to_tp2": Decimal("2.6"),
                "technical_score": Decimal("70"),
                "opportunity_score": Decimal("88"),
            },
        ).model_copy(update={"technical_score": 70}),
        tmp_path,
        now="2026-05-18T09:01:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.CONFIRMED
    assert symbol_result.actionability_state == "A_GRADE_ACTIONABLE"
    assert symbol_result.candidate_quality_grade == "A-"
    assert symbol_result.final_quality_grade == "A-"
    assert symbol_result.final_failed_gate == NA
    assert symbol_result.final_block_reason == NA
    assert symbol_result.lifecycle_state.actionability_state == "A_GRADE_ACTIONABLE"


def test_a_grade_candidate_with_low_technical_score_blocks_by_final_scoring(tmp_path) -> None:
    db_path = tmp_path / "a_grade_lifecycle.db"
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(
            grade=SetupQualityGrade.A,
            diagnostics_overrides={
                "first_failed_gate": "scoring",
                "gates_failed": ("scoring",),
                "technical_score": Decimal("49"),
                "opportunity_score": Decimal("88"),
            },
        ).model_copy(
            update={
                "technical_score": 49,
                "rejection_stage": "scoring",
                "rejection_reason": "Technical score is below 50.",
            }
        ),
        tmp_path,
        now="2026-05-18T09:02:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.actionability_state == "A_GRADE_BLOCKED_BY_SCORING"
    assert symbol_result.final_quality_grade == "Blocked"
    assert symbol_result.final_failed_gate == "scoring"
    assert symbol_result.final_block_reason == "A-grade candidate, but blocked by final scoring."

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        records = repository.get_records_for_symbols(("BTCUSDT",))
    assert records[0].candidate_quality_grade == "A"
    assert records[0].technical_score == "49"
    assert records[0].opportunity_score == "88"
    assert records[0].actionability_state == "A_GRADE_BLOCKED_BY_SCORING"
    assert records[0].final_block_reason == "A-grade candidate, but blocked by final scoring."


def test_a_grade_candidate_with_target_integrity_failure_blocks_by_target(tmp_path) -> None:
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(
            grade=SetupQualityGrade.A_PLUS,
            symbol="TARGETUSDT",
            diagnostics_overrides={
                "first_failed_gate": "target_integrity",
                "gates_failed": ("target_integrity",),
                "target_integrity_status": "blocked",
                "target_failure": "RR_BELOW_MINIMUM",
                "target_integrity_reason": "Clean target path is too compressed.",
                "technical_score": Decimal("70"),
                "opportunity_score": Decimal("88"),
            },
        ).model_copy(update={"technical_score": 70, "rejection_stage": "target_integrity"}),
        tmp_path,
        now="2026-05-18T09:03:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.actionability_state == "A_GRADE_BLOCKED_BY_TARGET"
    assert symbol_result.final_quality_grade == "Blocked"
    assert symbol_result.final_failed_gate == "target_integrity"
    assert symbol_result.final_block_reason == "A-grade candidate, but blocked by target integrity."
    assert symbol_result.target_integrity_status == "blocked"
    assert symbol_result.target_failure == "RR_BELOW_MINIMUM"


def test_a_grade_target_inside_chop_strong_scores_becomes_actionable_target_caution(tmp_path) -> None:
    db_path = tmp_path / "a_grade_lifecycle.db"
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(
            grade=SetupQualityGrade.A,
            symbol="CHOPUSDT",
            diagnostics_overrides={
                "first_failed_gate": "target_inside_chop",
                "gates_failed": ("target_inside_chop",),
                "target_integrity_status": "warning",
                "target_failure": "TARGET_INSIDE_CHOP",
                "target_failure_severity": "soft_target_warning",
                "target_warning_reason": "TP2 remains inside recent chop/range.",
                "technical_score": Decimal("95"),
                "opportunity_score": Decimal("94"),
                "regime_compatibility_label": "Supportive",
            },
        ).model_copy(update={"technical_score": Decimal("95")}),
        tmp_path,
        now="2026-05-18T09:04:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.actionability_state == "A_GRADE_ACTIONABLE_TARGET_CAUTION"
    assert symbol_result.final_quality_grade == "A"
    assert symbol_result.final_failed_gate == NA
    assert symbol_result.final_block_reason == NA
    assert symbol_result.target_integrity_status == "warning"
    assert symbol_result.target_failure == "TARGET_INSIDE_CHOP"
    assert symbol_result.target_failure_severity == "target_caution_actionable"
    assert symbol_result.target_warning_reason == "TP2 remains inside recent chop/range."

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        records = repository.get_records_for_symbols(("CHOPUSDT",))
    assert records[0].actionability_state == "A_GRADE_ACTIONABLE_TARGET_CAUTION"
    assert records[0].target_failure_severity == "target_caution_actionable"
    assert records[0].target_warning_reason == "TP2 remains inside recent chop/range."


def test_b_plus_target_caution_does_not_become_actionable(tmp_path) -> None:
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(
            grade=SetupQualityGrade.B_PLUS,
            symbol="BPLUSUSDT",
            diagnostics_overrides={
                "first_failed_gate": "target_inside_chop",
                "gates_failed": ("target_inside_chop",),
                "target_integrity_status": "warning",
                "target_failure": "TARGET_INSIDE_CHOP",
                "target_failure_severity": "soft_target_warning",
                "target_warning_reason": "TP2 remains inside recent chop/range.",
                "technical_score": Decimal("95"),
                "opportunity_score": Decimal("94"),
            },
        ).model_copy(update={"technical_score": Decimal("95")}),
        tmp_path,
        now="2026-05-18T09:05:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.actionability_state == "NOT_A_GRADE_CANDIDATE"
    assert symbol_result.final_quality_grade == "B+"


def test_invalid_tp_order_remains_blocked_by_target(tmp_path) -> None:
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(
            grade=SetupQualityGrade.A,
            symbol="BADTPUSDT",
            diagnostics_overrides={
                "first_failed_gate": "targets_not_monotonic",
                "gates_failed": ("targets_not_monotonic",),
                "tp1": Decimal("115"),
                "tp2": Decimal("110"),
                "tp3": Decimal("120"),
                "technical_score": Decimal("95"),
                "opportunity_score": Decimal("94"),
            },
        ).model_copy(update={"technical_score": Decimal("95")}),
        tmp_path,
        now="2026-05-18T09:06:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.actionability_state == "A_GRADE_BLOCKED_BY_TARGET"
    assert symbol_result.final_quality_grade == "Blocked"
    assert symbol_result.final_failed_gate == "target_integrity"


def test_rr_below_2_5_remains_blocked(tmp_path) -> None:
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(
            grade=SetupQualityGrade.A,
            symbol="LOWRRUSDT",
            diagnostics_overrides={
                "first_failed_gate": "rr_below_minimum",
                "gates_failed": ("rr_below_minimum",),
                "rr_to_tp2": Decimal("2.49"),
                "technical_score": Decimal("95"),
                "opportunity_score": Decimal("94"),
            },
        ).model_copy(update={"technical_score": Decimal("95")}),
        tmp_path,
        now="2026-05-18T09:07:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.actionability_state != "A_GRADE_ACTIONABLE"
    assert symbol_result.actionability_state != "A_GRADE_ACTIONABLE_TARGET_CAUTION"
    assert symbol_result.final_quality_grade == "Blocked"
    assert symbol_result.final_failed_gate == "rr_below_minimum"


def test_opposing_structure_too_close_remains_blocked_by_target(tmp_path) -> None:
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(
            grade=SetupQualityGrade.A_PLUS,
            symbol="OPPOSEUSDT",
            diagnostics_overrides={
                "first_failed_gate": "target_integrity",
                "gates_failed": ("target_integrity",),
                "target_integrity_status": "blocked",
                "target_failure": "OPPOSING_STRUCTURE_BLOCK",
                "target_failure_severity": "fatal_target_failure",
                "target_warning_reason": "Opposing structure blocks the clean path before minimum RR.",
                "technical_score": Decimal("95"),
                "opportunity_score": Decimal("94"),
            },
        ).model_copy(update={"technical_score": Decimal("95"), "rejection_stage": "target_integrity"}),
        tmp_path,
        now="2026-05-18T09:08:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.actionability_state == "A_GRADE_BLOCKED_BY_TARGET"
    assert symbol_result.final_quality_grade == "Blocked"
    assert symbol_result.final_failed_gate == "target_integrity"
    assert symbol_result.target_failure_severity == "fatal_target_failure"

def test_rejected_clean_a_grade_trade_map_promotes_to_actionable_a_grade(tmp_path) -> None:
    db_path = tmp_path / "rejected-actionable-a-grade.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(_record(SetupLifecycleState.REJECTED))

    result = apply_lifecycle_to_run_result(
        _scan_result(_a_grade_watch_symbol(grade=SetupQualityGrade.A_PLUS)),
        database_path=db_path,
        scan_run_id="run-rejected-actionable-a",
        now="2026-05-18T09:05:00+00:00",
    )
    symbol_result = result.results[0]

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_transition is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.lifecycle_transition.from_state == SetupLifecycleState.REJECTED
    assert symbol_result.lifecycle_transition.reason == SetupTransitionReason.ACTIONABLE_A_GRADE


def test_a_grade_watch_does_not_become_active_before_limit_zone_touch(tmp_path) -> None:
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(latest_high=Decimal("110"), latest_low=Decimal("108")),
        tmp_path,
        now="2026-05-18T09:00:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.EXECUTING


def test_a_plus_a_grade_watch_promotes_when_candle_overlaps_entry_zone(tmp_path) -> None:
    _apply_single_lifecycle(
        _a_grade_watch_symbol(grade=SetupQualityGrade.A_PLUS, latest_high=Decimal("110"), latest_low=Decimal("108")),
        tmp_path,
        now="2026-05-18T09:00:00+00:00",
    )

    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(grade=SetupQualityGrade.A_PLUS, latest_high=Decimal("102.5"), latest_low=Decimal("101")),
        tmp_path,
        now="2026-05-18T09:05:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_transition is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.TRIGGERED
    assert symbol_result.lifecycle_transition.from_state == SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.lifecycle_transition.reason == SetupTransitionReason.ENTRY_ZONE_TOUCHED


def test_a_a_grade_watch_promotes_when_candle_overlaps_entry_zone(tmp_path) -> None:
    _apply_single_lifecycle(
        _a_grade_watch_symbol(grade=SetupQualityGrade.A, latest_high=Decimal("110"), latest_low=Decimal("108")),
        tmp_path,
        now="2026-05-18T09:00:00+00:00",
    )

    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(grade=SetupQualityGrade.A, latest_high=Decimal("101.5"), latest_low=Decimal("99.5")),
        tmp_path,
        now="2026-05-18T09:05:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.TRIGGERED


def test_a_grade_watch_promotes_from_current_price_inside_zone_when_candle_range_unavailable(tmp_path) -> None:
    _apply_single_lifecycle(
        _a_grade_watch_symbol(latest_high=Decimal("110"), latest_low=Decimal("108")),
        tmp_path,
        now="2026-05-18T09:00:00+00:00",
    )

    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(latest_high=NA, latest_low=NA, current_price=Decimal("101")),
        tmp_path,
        now="2026-05-18T09:05:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.TRIGGERED


def test_watchlisted_setup_promotes_from_stored_entry_zone_touch_without_recalculating_levels(tmp_path) -> None:
    db_path = tmp_path / "watch-touch.db"
    stored = _record(SetupLifecycleState.WATCHLISTED).model_copy(
        update={
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "95",
            "tp1": "110",
            "tp2": "115",
            "tp3": "120",
            "rr": "3.2",
            "invalidation_reason": "Invalid if price accepts below 95.",
            "invalidation_logic": "Invalid if price accepts below 95.",
        }
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(stored)

    current_scan = _confirmed_candidate_symbol(
        entry_low=Decimal("200"),
        entry_high=Decimal("202"),
        stop=Decimal("190"),
    ).model_copy(update={"current_price": Decimal("101"), "latest_high": NA, "latest_low": NA})

    result = apply_lifecycle_to_run_result(
        _scan_result(current_scan),
        database_path=db_path,
        scan_run_id="run-touch",
        now="2026-05-18T09:05:00+00:00",
    )
    lifecycle = result.results[0].lifecycle_state
    transition = result.results[0].lifecycle_transition

    assert lifecycle is not None
    assert transition is not None
    assert lifecycle.current_state == SetupLifecycleState.TRIGGERED
    assert transition.from_state == SetupLifecycleState.WATCHLISTED
    assert transition.reason == SetupTransitionReason.ENTRY_ZONE_TOUCHED
    assert lifecycle.entry_low == "100"
    assert lifecycle.entry_high == "102"
    assert lifecycle.stop_loss == "95"
    assert lifecycle.tp1 == "110"
    assert lifecycle.invalidation_reason == "Invalid if price accepts below 95."

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        records = repository.get_records_for_symbols(("BTCUSDT",))

    assert len(records) == 1


def test_watchlisted_setup_uses_current_price_for_stored_entry_zone_touch(tmp_path) -> None:
    db_path = tmp_path / "watch-current-price-touch.db"
    stored = _record(SetupLifecycleState.WATCHLISTED).model_copy(
        update={
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "95",
            "tp1": "110",
            "tp2": "115",
            "tp3": "120",
            "rr": "3.2",
            "invalidation_reason": "Invalid if price accepts below 95.",
        }
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(stored)

    current_scan = _confirmed_candidate_symbol(
        entry_low=Decimal("200"),
        entry_high=Decimal("202"),
        stop=Decimal("190"),
    ).model_copy(update={"current_price": Decimal("103"), "latest_high": Decimal("102"), "latest_low": Decimal("100")})

    result = apply_lifecycle_to_run_result(
        _scan_result(current_scan),
        database_path=db_path,
        scan_run_id="run-current-price-outside",
        now="2026-05-18T09:05:00+00:00",
    )
    lifecycle = result.results[0].lifecycle_state
    transition = result.results[0].lifecycle_transition

    assert lifecycle is not None
    assert transition is not None
    assert lifecycle.current_state != SetupLifecycleState.EXECUTING
    assert transition.reason != SetupTransitionReason.ENTRY_ZONE_TOUCHED


def test_stalking_short_setup_promotes_from_stored_entry_zone_touch(tmp_path) -> None:
    db_path = tmp_path / "stalk-short-touch.db"
    stored = _record(
        SetupLifecycleState.STALKING,
        direction="short",
    ).model_copy(
        update={
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "105",
            "tp1": "95",
            "tp2": "90",
            "tp3": "85",
            "rr": "3.1",
            "invalidation_reason": "Invalid if price accepts above 105.",
            "invalidation_logic": "Invalid if price accepts above 105.",
        }
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(stored)

    current_scan = _confirmed_candidate_symbol(
        direction="short",
        entry_low=Decimal("200"),
        entry_high=Decimal("202"),
        stop=Decimal("210"),
    ).model_copy(update={"current_price": Decimal("101"), "latest_high": NA, "latest_low": NA})

    result = apply_lifecycle_to_run_result(
        _scan_result(current_scan),
        database_path=db_path,
        scan_run_id="run-short-touch",
        now="2026-05-18T09:05:00+00:00",
    )
    lifecycle = result.results[0].lifecycle_state
    transition = result.results[0].lifecycle_transition

    assert lifecycle is not None
    assert transition is not None
    assert lifecycle.current_state == SetupLifecycleState.TRIGGERED
    assert transition.from_state == SetupLifecycleState.STALKING
    assert transition.reason == SetupTransitionReason.ENTRY_ZONE_TOUCHED
    assert lifecycle.entry_low == "100"
    assert lifecycle.entry_high == "102"
    assert lifecycle.tp1 == "95"


def test_plan_locking_includes_a_grade_watch_and_confirmed(tmp_path) -> None:
    locked_levels = {
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "115",
        "tp3": "120",
        "rr": "3.2",
        "invalidation_reason": "Invalid if price accepts below 95.",
    }
    for state in (SetupLifecycleState.ACTIONABLE_A_GRADE, SetupLifecycleState.A_GRADE_WATCH, SetupLifecycleState.CONFIRMED):
        db_path = tmp_path / f"{state.value}.db"
        stored = _record(state, lifecycle_id=f"life-{state.value}").model_copy(update=locked_levels)
        with SQLiteSetupLifecycleRepository(db_path) as repository:
            repository.upsert_record(stored)

        current_scan = _confirmed_candidate_symbol(
            entry_low=Decimal("200"),
            entry_high=Decimal("202"),
            stop=Decimal("190"),
            rr=Decimal("4"),
        )
        result = apply_lifecycle_to_run_result(
            _scan_result(current_scan),
            database_path=db_path,
            scan_run_id=f"run-{state.value}",
            now="2026-05-18T09:10:00+00:00",
        )
        lifecycle = result.results[0].lifecycle_state

        assert lifecycle is not None
        assert lifecycle.entry_low == "100"
        assert lifecycle.entry_high == "102"
        assert lifecycle.stop_loss == "95"
        assert lifecycle.tp1 == "110"
        assert lifecycle.tp2 == "115"
        assert lifecycle.tp3 == "120"
        assert lifecycle.rr == "3.2"


def test_lower_grade_setup_is_not_promoted_by_a_grade_watch_path(tmp_path) -> None:
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(grade=SetupQualityGrade.B_PLUS),
        tmp_path,
        now="2026-05-18T09:00:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.ACTIONABLE_A_GRADE
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.A_GRADE_WATCH
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.EXECUTING
    assert symbol_result.actionability_state == "NOT_A_GRADE_CANDIDATE"


def test_a_grade_watch_creation_blocks_missing_entry_stop_invalidation_rr_and_target_integrity(tmp_path) -> None:
    cases = (
        {"entry_low": NA, "entry_high": NA, "first_failed_gate": "missing_entry_zone", "gates_failed": ("missing_entry_zone",)},
        {"stop": NA, "first_failed_gate": "missing_stop", "gates_failed": ("missing_stop",)},
        {"invalidation": NA, "first_failed_gate": "missing_invalidation", "gates_failed": ("missing_invalidation",)},
        {"rr_to_tp2": Decimal("2.2"), "first_failed_gate": "rr_below_minimum", "gates_failed": ("rr_below_minimum",)},
        {
            "target_integrity_status": "blocked",
            "first_failed_gate": "target_integrity",
            "gates_failed": ("target_integrity",),
        },
    )

    for index, overrides in enumerate(cases):
        symbol_result = _apply_single_lifecycle(
            _a_grade_watch_symbol(symbol=f"CASE{index}USDT", diagnostics_overrides=overrides),
            tmp_path,
            now=f"2026-05-18T09:{index:02d}:00+00:00",
        )

        assert symbol_result.lifecycle_state is not None
        assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.ACTIONABLE_A_GRADE
        assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.A_GRADE_WATCH
        assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.EXECUTING


def test_a_grade_watch_invalidates_instead_of_activating_when_invalidation_happens_first(tmp_path) -> None:
    _apply_single_lifecycle(
        _a_grade_watch_symbol(latest_high=Decimal("110"), latest_low=Decimal("108")),
        tmp_path,
        now="2026-05-18T09:00:00+00:00",
    )

    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(
            latest_high=Decimal("102"),
            latest_low=Decimal("101"),
            diagnostics_overrides={
                "first_failed_gate": "body_acceptance_failure",
                "gates_failed": ("body_acceptance_failure",),
                "acceptance_status": "BODY_ACCEPTANCE_FAILURE",
                "pullback_failure_reason": "Body accepted beyond invalidation before entry.",
            },
        ),
        tmp_path,
        now="2026-05-18T09:05:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.INVALIDATED


def test_lifecycle_invalidates_triggered_setup_on_too_deep_pullback(tmp_path) -> None:
    db_path = tmp_path / "lifecycle.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(_record(SetupLifecycleState.TRIGGERED))

    result = apply_lifecycle_to_run_result(
        _scan_result(_too_deep_pullback_symbol()),
        database_path=db_path,
        now="2026-05-18T09:30:00+00:00",
    )

    lifecycle = result.results[0].lifecycle_state
    transition = result.results[0].lifecycle_transition
    assert lifecycle is not None
    assert transition is not None
    assert lifecycle.current_state == SetupLifecycleState.INVALIDATED
    assert lifecycle.invalidation_reason == "pullback exceeded valid structure depth"
    assert transition.reason == SetupTransitionReason.SETUP_INVALIDATED


def test_lifecycle_keeps_wick_sweep_reclaim_triggered(tmp_path) -> None:
    db_path = tmp_path / "lifecycle.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(_record(SetupLifecycleState.TRIGGERED))

    result = apply_lifecycle_to_run_result(
        _scan_result(_wick_reclaim_symbol()),
        database_path=db_path,
        now="2026-05-18T09:30:00+00:00",
    )

    lifecycle = result.results[0].lifecycle_state
    transition = result.results[0].lifecycle_transition
    assert lifecycle is not None
    assert transition is not None
    assert lifecycle.current_state == SetupLifecycleState.TRIGGERED
    assert transition.transitioned is False


def test_lifecycle_invalidates_body_acceptance_failure(tmp_path) -> None:
    db_path = tmp_path / "lifecycle.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(_record(SetupLifecycleState.TRIGGERED))

    result = apply_lifecycle_to_run_result(
        _scan_result(_body_acceptance_symbol()),
        database_path=db_path,
        now="2026-05-18T09:30:00+00:00",
    )

    lifecycle = result.results[0].lifecycle_state
    transition = result.results[0].lifecycle_transition
    assert lifecycle is not None
    assert transition is not None
    assert lifecycle.current_state == SetupLifecycleState.INVALIDATED
    assert lifecycle.invalidation_reason == "body accepted beyond 0.786 invalidation zone"
    assert transition.reason == SetupTransitionReason.SETUP_INVALIDATED


def test_invalid_state_transition_rejected() -> None:
    result = transition_record(
        _record(SetupLifecycleState.WATCHLISTED),
        SetupLifecycleState.EXECUTING,
        reason=SetupTransitionReason.VALID_TRADE_IDEA,
        now="2026-05-18T09:00:00+00:00",
    )

    assert result.allowed is False
    assert result.transitioned is False
    assert "WATCHLISTED cannot move directly to EXECUTING." in result.notes


def test_cooldown_and_archive_behavior() -> None:
    cooldown = evaluate_lifecycle_transition(
        _record(SetupLifecycleState.INVALIDATED),
        _observation(),
        lifecycle_id="life_1",
        now="2026-05-18T09:00:00+00:00",
    )
    assert cooldown.to_state == SetupLifecycleState.COOLDOWN
    assert cooldown.record is not None
    assert cooldown.record.cooldown_until is not None

    expired_cooldown = cooldown.record.model_copy(update={"cooldown_until": "2026-05-18T09:30:00+00:00"})
    archived = evaluate_lifecycle_transition(
        expired_cooldown,
        _observation(readiness_score=0, readiness_label="REJECTED", failed_gate="missing_confirmed_sweep"),
        lifecycle_id="life_1",
        now="2026-05-18T10:00:00+00:00",
    )
    assert archived.to_state == SetupLifecycleState.ARCHIVED
    assert archived.record is not None
    assert archived.record.archived_at == "2026-05-18T10:00:00+00:00"


def test_transition_history_persistence(tmp_path) -> None:
    db_path = tmp_path / "life.db"
    record = _record(SetupLifecycleState.WATCHLISTED)
    event = transition_record(
        record,
        SetupLifecycleState.STALKING,
        reason=SetupTransitionReason.SWEEP_APPEARED,
        now="2026-05-18T09:05:00+00:00",
        scan_run_id="run_1",
    )
    assert event.record is not None
    assert event.event is not None

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(event.record)
        repository.insert_event(event.event)

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        stored = repository.get_record(symbol="BTCUSDT", mode="swing", direction="long")
        events = repository.list_events(symbol="BTCUSDT")

    assert stored is not None
    assert stored.current_state == SetupLifecycleState.STALKING
    assert events[0].scan_run_id == "run_1"
    assert events[0].from_state == SetupLifecycleState.WATCHLISTED
    assert events[0].to_state == SetupLifecycleState.STALKING


def test_scanner_integration_and_json_lifecycle_output(tmp_path) -> None:
    result = apply_lifecycle_to_run_result(
        _scan_result(_near_miss_symbol()),
        database_path=tmp_path / "life.db",
        scan_run_id="run_1",
        now="2026-05-18T09:00:00+00:00",
    )

    symbol_result = result.results[0]
    payload = result.model_dump(mode="json")

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.REJECTED
    assert symbol_result.lifecycle_state.failed_gate.startswith("invalid_stored_plan_geometry:")
    assert payload["results"][0]["lifecycle_state"]["current_state"] == "REJECTED"
    assert payload["results"][0]["lifecycle_transition"]["event"]["scan_run_id"] == "run_1"


def test_one_scan_candidate_does_not_become_confirmed_or_active(tmp_path) -> None:
    result = apply_lifecycle_to_run_result(
        _scan_result(_confirmed_candidate_symbol()),
        database_path=tmp_path / "life.db",
        scan_run_id="run_1",
        now="2026-05-18T09:00:00+00:00",
        confirmation_cycles=2,
        setup_tolerance_pct=Decimal("0.5"),
    )

    lifecycle = result.results[0].lifecycle_state
    assert lifecycle is not None
    assert lifecycle.current_state == SetupLifecycleState.TRIGGERED
    assert lifecycle.confirmation_count == 1
    assert lifecycle.required_confirmation_cycles == 2
    assert lifecycle.confirmed_at is None
    assert result.scanner_process_summary["confirmation_pending"] == 1


def test_rejected_result_never_initializes_as_triggered(tmp_path) -> None:
    result = apply_lifecycle_to_run_result(
        _scan_result(
            _public_watchlist_candidate_symbol(
                failed_gate="no_ob_or_fvg_zone",
                rr=NA,
                entry_low=NA,
                entry_high=NA,
                stop=NA,
                tp1=NA,
                quality_grade=SetupQualityGrade.REJECT,
            )
        ),
        database_path=tmp_path / "rejected.db",
        scan_run_id="run-rejected",
        now="2026-05-18T09:00:00+00:00",
    )

    lifecycle = result.results[0].lifecycle_state
    assert lifecycle is not None
    assert lifecycle.current_state != SetupLifecycleState.TRIGGERED
    assert lifecycle.current_state == SetupLifecycleState.REJECTED


def test_public_watchlist_candidate_initializes_as_watch_or_stalking(tmp_path) -> None:
    result = apply_lifecycle_to_run_result(
        _scan_result(_public_watchlist_candidate_symbol(failed_gate="rr_below_minimum", rr=Decimal("2.6"))),
        database_path=tmp_path / "public-watch.db",
        scan_run_id="run-public-watch",
        now="2026-05-18T09:00:00+00:00",
    )

    lifecycle = result.results[0].lifecycle_state
    assert lifecycle is not None
    assert lifecycle.current_state in {SetupLifecycleState.WATCHLISTED, SetupLifecycleState.STALKING}
    assert lifecycle.current_state != SetupLifecycleState.TRIGGERED


def test_candidate_becomes_confirmed_after_required_consecutive_scans(tmp_path) -> None:
    db_path = tmp_path / "life.db"
    apply_lifecycle_to_run_result(
        _scan_result(_confirmed_candidate_symbol()),
        database_path=db_path,
        scan_run_id="run_1",
        now="2026-05-18T09:00:00+00:00",
        confirmation_cycles=2,
        setup_tolerance_pct=Decimal("0.5"),
    )

    result = apply_lifecycle_to_run_result(
        _scan_result(_confirmed_candidate_symbol()),
        database_path=db_path,
        scan_run_id="run_2",
        now="2026-05-18T09:05:00+00:00",
        confirmation_cycles=2,
        setup_tolerance_pct=Decimal("0.5"),
    )

    lifecycle = result.results[0].lifecycle_state
    transition = result.results[0].lifecycle_transition
    assert lifecycle is not None
    assert transition is not None
    assert lifecycle.current_state == SetupLifecycleState.CONFIRMED
    assert lifecycle.confirmation_count == 2
    assert lifecycle.confirmed_at == "2026-05-18T09:05:00+00:00"
    assert transition.reason == SetupTransitionReason.MULTI_SCAN_CONFIRMED
    assert result.scanner_process_summary["confirmed_after_multi_scan"] == 1


def _assert_observation_never_promotes_to_confirmed(observation: LifecycleObservation) -> SetupTransitionResult:
    record = _record(SetupLifecycleState.TRIGGERED).model_copy(
        update={
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "95",
            "tp1": "110",
            "tp2": "117",
            "tp3": "124",
            "rr": "3.2",
            "confirmation_count": 1,
            "required_confirmation_cycles": 2,
            "quality_grade_current": "B+",
            "invalidation_reason": "Invalid if price accepts below 95.",
            "invalidation_logic": "Invalid if price accepts below 95.",
        }
    )
    result = evaluate_lifecycle_transition(
        record,
        observation,
        lifecycle_id=record.lifecycle_id,
        now="2026-05-18T09:05:00+00:00",
        required_confirmation_cycles=2,
    )
    assert result.record is not None
    assert result.record.current_state != SetupLifecycleState.CONFIRMED
    assert result.record.confirmed_at is None
    return result


def test_rejected_by_scoring_never_promotes_to_confirmed() -> None:
    result = _assert_observation_never_promotes_to_confirmed(
        _confirmed_observation(core_status="rejected_by_scoring")
    )
    assert result.record.current_state in {SetupLifecycleState.TRIGGERED, SetupLifecycleState.INVALIDATED}


def test_failed_confirmation_gate_scoring_never_promotes_to_confirmed() -> None:
    result = _assert_observation_never_promotes_to_confirmed(
        _confirmed_observation(failed_gate="scoring")
    )
    assert result.record.current_state in {SetupLifecycleState.TRIGGERED, SetupLifecycleState.INVALIDATED}


def test_watchlist_near_miss_never_promotes_to_signal_confirmed() -> None:
    result = _assert_observation_never_promotes_to_confirmed(
        _confirmed_observation(setup_quality_state="watchlist_near_miss")
    )
    assert result.record.current_state == SetupLifecycleState.TRIGGERED


def test_confirmed_grade_below_min_never_promotes_to_confirmed() -> None:
    result = _assert_observation_never_promotes_to_confirmed(
        _confirmed_observation(quality_grade="B", quality_score=70)
    )
    assert result.record.current_state == SetupLifecycleState.TRIGGERED


def test_trade_idea_missing_never_promotes_to_confirmed() -> None:
    result = _assert_observation_never_promotes_to_confirmed(
        _confirmed_observation(valid_trade_idea=False)
    )
    assert result.record.current_state == SetupLifecycleState.TRIGGERED


def test_technical_score_below_min_never_promotes_to_confirmed() -> None:
    result = _assert_observation_never_promotes_to_confirmed(
        _confirmed_observation(technical_score="49")
    )
    assert result.record.current_state == SetupLifecycleState.TRIGGERED


def test_opportunity_score_below_min_never_promotes_to_confirmed() -> None:
    result = _assert_observation_never_promotes_to_confirmed(
        _confirmed_observation(opportunity_score="79")
    )
    assert result.record.current_state == SetupLifecycleState.TRIGGERED


def test_active_invalidation_never_promotes_to_confirmed() -> None:
    result = _assert_observation_never_promotes_to_confirmed(
        _confirmed_observation(active_invalidation_reason="technical_score_is_below_50")
    )
    assert result.record.current_state == SetupLifecycleState.INVALIDATED


def test_confirmed_signal_clears_stale_failed_gate_on_promotion() -> None:
    record = _record(SetupLifecycleState.TRIGGERED).model_copy(
        update={
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "95",
            "tp1": "110",
            "tp2": "117",
            "tp3": "124",
            "rr": "3.2",
            "confirmation_count": 1,
            "required_confirmation_cycles": 2,
            "quality_grade_current": "B+",
            "failed_gate": "missing_confirmation_structure_shift",
            "invalidation_reason": "Invalid if price accepts below 95.",
            "invalidation_logic": "Invalid if price accepts below 95.",
        }
    )
    observation = _confirmed_observation(
        failed_gate=NA,
        quality_grade="B+",
        quality_score=82,
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="117",
        tp3="124",
        rr="3.2",
        invalidation_reason="Invalid if price accepts below 95.",
        sweep_detected=True,
        structure_shift_detected=True,
        pullback_valid=True,
        rr_valid=True,
        valid_trade_idea=True,
    )

    result = evaluate_lifecycle_transition(
        record,
        observation,
        lifecycle_id=record.lifecycle_id,
        now="2026-05-18T09:05:00+00:00",
        required_confirmation_cycles=2,
    )

    assert result.record.current_state == SetupLifecycleState.CONFIRMED
    assert result.record.failed_gate == NA
    assert result.event is not None
    assert result.event.failed_gate == NA


def test_confirmed_signal_clears_stale_invalidation_reason_on_valid_promotion() -> None:
    record = _record(SetupLifecycleState.TRIGGERED).model_copy(
        update={
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "95",
            "tp1": "110",
            "tp2": "117",
            "tp3": "124",
            "rr": "3.2",
            "confirmation_count": 1,
            "required_confirmation_cycles": 2,
            "quality_grade_current": "B+",
            "failed_gate": "regime_compatibility",
            "invalidation_reason": "Setup rejected by regime weakness.",
            "invalidation_logic": "Invalid if price accepts below 95.",
        }
    )
    observation = _confirmed_observation(
        failed_gate=NA,
        quality_grade="B+",
        quality_score=82,
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="117",
        tp3="124",
        rr="3.2",
        invalidation_reason="Invalid if price accepts below 95.",
        sweep_detected=True,
        structure_shift_detected=True,
        pullback_valid=True,
        rr_valid=True,
        valid_trade_idea=True,
    )

    result = evaluate_lifecycle_transition(
        record,
        observation,
        lifecycle_id=record.lifecycle_id,
        now="2026-05-18T09:05:00+00:00",
        required_confirmation_cycles=2,
    )

    assert result.record.current_state == SetupLifecycleState.CONFIRMED
    assert result.record.invalidation_reason == "Invalid if price accepts below 95."
    assert result.record.failed_gate == NA


def test_contradictory_second_scan_resets_confirmation_count(tmp_path) -> None:
    db_path = tmp_path / "life.db"
    apply_lifecycle_to_run_result(
        _scan_result(_confirmed_candidate_symbol()),
        database_path=db_path,
        scan_run_id="run_1",
        now="2026-05-18T09:00:00+00:00",
        confirmation_cycles=2,
        setup_tolerance_pct=Decimal("0.5"),
    )

    result = apply_lifecycle_to_run_result(
        _scan_result(_confirmed_candidate_symbol(entry_low=Decimal("120"), entry_high=Decimal("122"), stop=Decimal("114"))),
        database_path=db_path,
        scan_run_id="run_2",
        now="2026-05-18T09:05:00+00:00",
        confirmation_cycles=2,
        setup_tolerance_pct=Decimal("0.5"),
    )

    lifecycle = result.results[0].lifecycle_state
    assert lifecycle is not None
    assert lifecycle.current_state == SetupLifecycleState.TRIGGERED
    assert lifecycle.confirmation_count == 1
    assert lifecycle.entry_low == "120"
    assert lifecycle.confirmed_at is None


def test_duplicate_candidate_merges_and_preserves_first_seen_at(tmp_path) -> None:
    db_path = tmp_path / "life.db"
    first = apply_lifecycle_to_run_result(
        _scan_result(_confirmed_candidate_symbol()),
        database_path=db_path,
        scan_run_id="run_1",
        now="2026-05-18T09:00:00+00:00",
        confirmation_cycles=2,
        setup_tolerance_pct=Decimal("0.5"),
    )
    assert first.scanner_process_summary["new_candidates"] == 1

    second = apply_lifecycle_to_run_result(
        _scan_result(
            _confirmed_candidate_symbol(
                entry_low=Decimal("100.20"),
                entry_high=Decimal("102.20"),
                stop=Decimal("95.10"),
            )
        ),
        database_path=db_path,
        scan_run_id="run_2",
        now="2026-05-18T09:05:00+00:00",
        confirmation_cycles=2,
        setup_tolerance_pct=Decimal("0.5"),
    )

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        records = repository.get_records_for_symbols(("BTCUSDT",))

    assert len(records) == 1
    assert records[0].first_seen_at == "2026-05-18T09:00:00+00:00"
    assert records[0].last_seen_at == "2026-05-18T09:05:00+00:00"
    assert records[0].entry_low == "100.2"
    assert records[0].confirmation_count == 2
    assert second.scanner_process_summary["merged_duplicates"] == 1


def test_confidence_decays_and_expired_outcome_analytics_are_stored(tmp_path) -> None:
    db_path = tmp_path / "life.db"
    times = (
        "2026-05-18T09:00:00+00:00",
        "2026-05-18T09:05:00+00:00",
        "2026-05-18T09:10:00+00:00",
        "2026-05-18T09:15:00+00:00",
        "2026-05-18T09:20:00+00:00",
        "2026-05-18T09:25:00+00:00",
    )
    latest = None
    for index, timestamp in enumerate(times):
        latest = apply_lifecycle_to_run_result(
            _scan_result(_a_grade_watch_symbol(grade=SetupQualityGrade.A_PLUS)),
            database_path=db_path,
            scan_run_id=f"run_{index}",
            now=timestamp,
            confirmation_cycles=2,
            setup_tolerance_pct=Decimal("0.5"),
        )

    assert latest is not None
    lifecycle = latest.results[0].lifecycle_state
    assert lifecycle is not None
    assert lifecycle.current_state == SetupLifecycleState.EXPIRED
    assert lifecycle.quality_grade_current == "Expired"
    assert lifecycle.decay_count == 4
    assert lifecycle.failed_gate == "confidence_decay"
    assert latest.scanner_process_summary["expired"] == 1

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        analytics = repository.list_outcome_analytics(symbol="BTCUSDT")

    assert len(analytics) == 1
    assert analytics[0].final_outcome == "EXPIRED"
    assert analytics[0].quality_at_first_detection == "A+"
    assert analytics[0].failure_reason == "no price reaction or lifecycle progress"


def test_poor_symbol_health_requires_extra_confirmation_cycle(tmp_path) -> None:
    db_path = tmp_path / "life.db"
    save_symbol_health_records(
        db_path,
        {
            "BTCUSDT": SymbolHealthRecord(
                symbol="BTCUSDT",
                current_health_score=25,
                invalidation_count=3,
                expired_setup_count=1,
            )
        },
    )
    apply_lifecycle_to_run_result(
        _scan_result(_confirmed_candidate_symbol()),
        database_path=db_path,
        scan_run_id="run_1",
        now="2026-05-18T09:00:00+00:00",
        confirmation_cycles=2,
        setup_tolerance_pct=Decimal("0.5"),
    )

    second = apply_lifecycle_to_run_result(
        _scan_result(_confirmed_candidate_symbol()),
        database_path=db_path,
        scan_run_id="run_2",
        now="2026-05-18T09:05:00+00:00",
        confirmation_cycles=2,
        setup_tolerance_pct=Decimal("0.5"),
    )

    lifecycle = second.results[0].lifecycle_state
    assert lifecycle is not None
    assert lifecycle.current_state == SetupLifecycleState.TRIGGERED
    assert lifecycle.confirmation_count == 2
    assert lifecycle.required_confirmation_cycles == 3
    assert lifecycle.symbol_health_penalty_cycles == 1
    assert second.scanner_process_summary["symbol_health_penalties_applied"] == 1


def test_watch_mode_lifecycle_prioritization(tmp_path) -> None:
    db_path = tmp_path / "life.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(_record(SetupLifecycleState.WATCHLISTED, symbol="WATCHUSDT", lifecycle_id="life_watch"))
        repository.upsert_record(_record(SetupLifecycleState.CONFIRMED, symbol="CONFIRMUSDT", lifecycle_id="life_confirm"))
        repository.upsert_record(_record(SetupLifecycleState.TRIGGERED, symbol="TRIGGERUSDT", lifecycle_id="life_trigger"))
        repository.upsert_record(_record(SetupLifecycleState.STALKING, symbol="STALKUSDT", lifecycle_id="life_stalk"))
        repository.upsert_record(_record(SetupLifecycleState.ARCHIVED, symbol="OLDUSDT", lifecycle_id="life_old"))

    symbols = prioritize_watch_symbols(
        ("WATCHUSDT", "OLDUSDT", "CONFIRMUSDT", "TRIGGERUSDT", "STALKUSDT", "NEWUSDT"),
        database_path=db_path,
    )

    assert symbols == ("STALKUSDT", "TRIGGERUSDT", "CONFIRMUSDT", "WATCHUSDT", "OLDUSDT", "NEWUSDT")


def test_research_lifecycle_queries(tmp_path) -> None:
    db_path = tmp_path / "life.db"
    initial = evaluate_lifecycle_transition(
        None,
        _observation(readiness_score=60, readiness_label="WATCH"),
        lifecycle_id="life_1",
        now="2026-05-18T09:00:00+00:00",
        scan_run_id="run_1",
    )
    stalking = evaluate_lifecycle_transition(
        initial.record,
        _observation(sweep_detected=True),
        lifecycle_id="life_1",
        now="2026-05-18T09:05:00+00:00",
        scan_run_id="run_2",
    )
    triggered = evaluate_lifecycle_transition(
        stalking.record,
        _observation(sweep_detected=True, structure_shift_detected=True),
        lifecycle_id="life_1",
        now="2026-05-18T09:10:00+00:00",
        scan_run_id="run_3",
    )
    pending_confirmation = evaluate_lifecycle_transition(
        triggered.record,
        _confirmed_observation(invalidation_reason="Invalid below stop."),
        lifecycle_id="life_1",
        now="2026-05-18T09:15:00+00:00",
        scan_run_id="run_4",
    )
    confirmed = evaluate_lifecycle_transition(
        pending_confirmation.record,
        _confirmed_observation(invalidation_reason="Invalid below stop."),
        lifecycle_id="life_1",
        now="2026-05-18T09:20:00+00:00",
        scan_run_id="run_5",
    )
    transitions = (initial, stalking, triggered, confirmed)
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        assert confirmed.record is not None
        repository.upsert_record(confirmed.record)
        for transition in transitions:
            assert transition.event is not None
            repository.insert_event(transition.event)

    summary = build_research_report(db_path, query="lifecycle_summary")
    conversion = build_research_report(db_path, query="lifecycle_conversion")
    detail = build_research_report(
        db_path,
        query="lifecycle_symbol_detail",
        filters=ResearchFilters(symbol="BTCUSDT"),
    )

    assert summary["total_lifecycles"] == 1
    assert conversion["watchlisted_to_valid"]["conversion_rate_pct"] == 100
    assert conversion["triggered_to_confirmed"]["conversion_rate_pct"] == 100
    assert detail["lifecycles"][0]["current_state"] == "CONFIRMED"
    assert detail["recent_transitions"][0]["to_state"] == "CONFIRMED"


def test_phase_37_lifecycle_funnel_counts_and_conversion_rates(tmp_path) -> None:
    db_path = tmp_path / "phase_37.db"
    _seed_phase_37_lifecycle_database(db_path)

    report = build_research_report(
        db_path,
        query="lifecycle_conversion",
        filters=ResearchFilters(lifecycle_stale_hours=2),
    )

    assert report["total_lifecycles"] == 6
    assert report["active_lifecycles"] == 4
    assert report["archived_lifecycles"] == 0
    assert report["funnel_counts"]["WATCHLISTED"] == 6
    assert report["funnel_counts"]["STALKING"] == 4
    assert report["funnel_counts"]["TRIGGERED"] == 3
    assert report["funnel_counts"]["CONFIRMED"] == 2
    assert report["funnel_counts"]["EXECUTING"] == 1
    assert report["funnel_counts"]["TP_HIT"] == 1
    assert report["conversion_rates"]["watchlisted_to_stalking_pct"] == 66.67
    assert report["conversion_rates"]["stalking_to_triggered_pct"] == 75
    assert report["conversion_rates"]["triggered_to_confirmed_pct"] == 66.67
    assert report["conversion_rates"]["confirmed_to_executing_pct"] == 50
    assert report["conversion_rates"]["executing_to_tp_hit_pct"] == 100


def test_phase_37_lifecycle_zero_denominator_handling(tmp_path) -> None:
    db_path = tmp_path / "empty_lifecycle.db"
    with SQLiteSetupLifecycleRepository(db_path):
        pass

    report = build_research_report(db_path, query="lifecycle_conversion")

    assert report["funnel_counts"]["WATCHLISTED"] == 0
    assert report["conversion_rates"]["watchlisted_to_stalking_pct"] == "N/A"
    assert report["conversion_rates"]["confirmed_to_executing_pct"] == "N/A"


def test_phase_37_lifecycle_dropoffs_and_failed_gate_grouping(tmp_path) -> None:
    db_path = tmp_path / "phase_37.db"
    _seed_phase_37_lifecycle_database(db_path)

    report = build_research_report(
        db_path,
        query="lifecycle_dropoffs",
        filters=ResearchFilters(lifecycle_stale_hours=2),
    )
    dropoffs = report["dropoff_stats"]
    stage_counts = {row["stage"]: row["count"] for row in dropoffs["dropoff_stages"]}
    gate_counts = {row["failed_gate"]: row["count"] for row in dropoffs["failed_gate_counts"]}

    assert dropoffs["biggest_dropoff_stage"] == "WATCHLISTED"
    assert stage_counts["WATCHLISTED"] == 2
    assert stage_counts["TRIGGERED"] == 1
    assert gate_counts["missing_confirmed_sweep"] == 2
    assert gate_counts["rr_below_minimum"] == 1
    assert dropoffs["most_common_invalidation_reason"] == "Pullback invalidated."
    assert dropoffs["average_readiness_score"] != "N/A"
    assert dropoffs["average_quality_score"] != "N/A"
    assert dropoffs["most_common_regime_state"] == "trend_expansion"


def test_phase_37_lifecycle_symbol_conversion_stats(tmp_path) -> None:
    db_path = tmp_path / "phase_37.db"
    _seed_phase_37_lifecycle_database(db_path)

    report = build_research_report(db_path, query="lifecycle_symbol_conversion")
    symbols = {row["symbol"]: row for row in report["per_symbol_conversion"]}

    assert symbols["BTCUSDT"]["lifecycle_count"] == 1
    assert symbols["BTCUSDT"]["highest_state_reached"] == "TP_HIT"
    assert symbols["BTCUSDT"]["conversion_to_confirmed_pct"] == 100
    assert symbols["BTCUSDT"]["conversion_to_executing_pct"] == 100
    assert symbols["BTCUSDT"]["average_time_to_highest_state_seconds"] == 18000
    assert symbols["ETHUSDT"]["highest_state_reached"] == "INVALIDATED"
    assert symbols["ETHUSDT"]["most_common_failure_point"] == "TRIGGERED"


def test_phase_37_lifecycle_state_duration_and_stale_detection(tmp_path) -> None:
    db_path = tmp_path / "phase_37.db"
    _seed_phase_37_lifecycle_database(db_path)

    report = build_research_report(
        db_path,
        query="lifecycle_state_duration",
        filters=ResearchFilters(lifecycle_stale_hours=2),
    )
    stats = report["state_duration_stats"]
    duration_by_state = {row["state"]: row for row in stats["states"]}
    stale_symbols = {row["symbol"] for row in report["stale_lifecycles"]}

    assert duration_by_state["STALKING"]["median_seconds"] == 3600
    assert stats["stale_lifecycle_count"] == 4
    assert stale_symbols == {"ADAUSDT", "BNBUSDT", "SOLUSDT", "XRPUSDT"}
    assert stats["longest_stuck_symbols"][0]["symbol"] == "XRPUSDT"
    assert stats["longest_stuck_symbols"][0]["hours_in_state"] == 5


def test_phase_37_lifecycle_json_output_includes_analytics_keys(tmp_path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "phase_37.db"
    output_path = tmp_path / "phase_37_research.json"
    _seed_phase_37_lifecycle_database(db_path)

    def fail_scanner(*args, **kwargs):
        raise AssertionError("research command should not run scanner")

    monkeypatch.setattr(run_scan, "ScannerRunner", fail_scanner)

    asyncio.run(
        run_scan.main(
            [
                "--research",
                "--research-query",
                "lifecycle_conversion",
                "--database-path",
                str(db_path),
                "--lifecycle-stale-hours",
                "2",
                "--research-output-json",
                str(output_path),
            ]
        )
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    captured = capsys.readouterr()
    assert payload["query"] == "lifecycle_conversion"
    assert payload["filters"]["lifecycle_stale_hours"] == 2
    for key in (
        "funnel_counts",
        "conversion_rates",
        "dropoff_stats",
        "state_duration_stats",
        "stale_lifecycles",
        "per_symbol_conversion",
    ):
        assert key in payload
    assert f"Exported research report: {output_path}" in captured.out


def test_one_malformed_lifecycle_candidate_does_not_erase_valid_candidates(
    tmp_path,
    monkeypatch,
) -> None:
    bad = _near_miss_symbol("BADUSDT")
    good = _near_miss_symbol("GOODUSDT")
    single = _scan_result(good)
    config = ScannerRunConfig.model_validate(
        {**single.config.model_dump(mode="python"), "symbols": ["BADUSDT", "GOODUSDT"]}
    )
    run_result = single.model_copy(
        update={
            "config": config,
            "results": (bad, good),
            "scanned_symbols": 2,
        }
    )
    original = lifecycle_service_module.observation_from_symbol_result

    def malformed_one(symbol_result, *, min_score_for_idea):
        if symbol_result.symbol == "BADUSDT":
            raise ValueError("malformed lifecycle observation")
        return original(symbol_result, min_score_for_idea=min_score_for_idea)

    monkeypatch.setattr(
        lifecycle_service_module,
        "observation_from_symbol_result",
        malformed_one,
    )
    database_path = tmp_path / "isolated_lifecycle.db"

    updated = apply_lifecycle_to_run_result(
        run_result,
        database_path=database_path,
        scan_run_id="run_isolated",
        now="2026-07-17T10:00:00+00:00",
    )

    assert updated.results[0].lifecycle_state is None
    assert updated.results[1].lifecycle_state is not None
    assert updated.scanner_process_summary["status"] == "PARTIAL"
    assert updated.scanner_process_summary["failed_symbols"] == 1
    assert updated.scanner_process_summary["errors"][0]["symbol"] == "BADUSDT"
    with SQLiteSetupLifecycleRepository(database_path) as repository:
        assert repository.list_records_for_symbol(symbol="BADUSDT") == ()
        assert len(repository.list_records_for_symbol(symbol="GOODUSDT")) == 1


def test_lifecycle_transaction_failure_propagates_and_rolls_back(tmp_path, monkeypatch) -> None:
    database_path = tmp_path / "failed_transaction.db"
    original_upsert = SQLiteSetupLifecycleRepository.upsert_record
    upsert_calls = 0

    def fail_second_upsert(self, record):
        nonlocal upsert_calls
        upsert_calls += 1
        if upsert_calls == 2:
            raise sqlite3.OperationalError("database disk I/O failure")
        return original_upsert(self, record)

    monkeypatch.setattr(SQLiteSetupLifecycleRepository, "upsert_record", fail_second_upsert)
    first = _near_miss_symbol("ONEUSDT")
    second = _near_miss_symbol("TWOUSDT")
    single = _scan_result(first)
    config = ScannerRunConfig.model_validate(
        {**single.config.model_dump(mode="python"), "symbols": ["ONEUSDT", "TWOUSDT"]}
    )
    run_result = single.model_copy(
        update={"config": config, "results": (first, second), "scanned_symbols": 2}
    )

    with pytest.raises(sqlite3.OperationalError, match="disk I/O"):
        apply_lifecycle_to_run_result(
            run_result,
            database_path=database_path,
            scan_run_id="run_transaction_failure",
            now="2026-07-17T10:00:00+00:00",
        )

    with SQLiteSetupLifecycleRepository(database_path) as repository:
        assert repository.list_records_for_symbol(symbol="ONEUSDT") == ()
        assert repository.list_records_for_symbol(symbol="TWOUSDT") == ()


def _with_generation_anchor(
    symbol_result: ScannerSymbolResult,
    anchor: str,
) -> ScannerSymbolResult:
    diagnostics = {
        mode: {**dict(payload), "setup_generation_anchor": anchor}
        for mode, payload in symbol_result.strategy_diagnostics.items()
    }
    return symbol_result.model_copy(update={"strategy_diagnostics": diagnostics})


def test_generation_id_is_deterministic_for_market_anchor_and_isolated_by_tuple() -> None:
    inputs = {
        "symbol": "BTCUSDT",
        "mode": "swing",
        "direction": "long",
        "structural_anchor": "execution_sweep|15m|1789987200000",
    }

    generation = new_setup_generation_id(**inputs)

    assert new_setup_generation_id(**inputs) == generation
    assert new_setup_generation_id(**{**inputs, "direction": "short"}) != generation
    assert new_setup_generation_id(**{**inputs, "mode": "scalp"}) != generation


def test_generation_id_remains_stable_through_active_and_terminal_transitions() -> None:
    generation_id = "generation-a"
    anchor = "execution_sweep|15m|1789987200000"
    watch = evaluate_lifecycle_transition(
        None,
        _observation(
            readiness_score=55,
            readiness_label="WATCH",
            structural_anchor=anchor,
        ),
        lifecycle_id=generation_id,
        now="2026-05-18T09:00:00+00:00",
    )
    stalking = evaluate_lifecycle_transition(
        watch.record,
        _observation(sweep_detected=True, structural_anchor=anchor),
        lifecycle_id=generation_id,
        now="2026-05-18T09:05:00+00:00",
    )
    triggered = evaluate_lifecycle_transition(
        stalking.record,
        _observation(
            sweep_detected=True,
            structure_shift_detected=True,
            structural_anchor=anchor,
        ),
        lifecycle_id=generation_id,
        now="2026-05-18T09:10:00+00:00",
    )
    pending = evaluate_lifecycle_transition(
        triggered.record,
        _confirmed_observation(
            invalidation_reason="Invalid below stop.",
            structural_anchor=anchor,
        ),
        lifecycle_id=generation_id,
        now="2026-05-18T09:15:00+00:00",
    )
    confirmed = evaluate_lifecycle_transition(
        pending.record,
        _confirmed_observation(
            invalidation_reason="Invalid below stop.",
            structural_anchor=anchor,
        ),
        lifecycle_id=generation_id,
        now="2026-05-18T09:20:00+00:00",
    )
    executing = evaluate_lifecycle_transition(
        confirmed.record,
        _confirmed_observation(
            invalidation_reason="Invalid below stop.",
            structural_anchor=anchor,
        ),
        lifecycle_id=generation_id,
        now="2026-05-18T09:25:00+00:00",
    )
    managing = transition_record(
        executing.record,
        SetupLifecycleState.MANAGING,
        reason=SetupTransitionReason.ENTRY_ACTIVATED,
        now="2026-05-18T09:30:00+00:00",
    )

    records = (
        watch.record,
        stalking.record,
        triggered.record,
        pending.record,
        confirmed.record,
        executing.record,
        managing.record,
    )
    assert all(record is not None for record in records)
    assert {record.setup_generation_id for record in records if record is not None} == {
        generation_id
    }
    assert all(record.structural_anchor == anchor for record in records if record is not None)

    terminal_reasons = {
        SetupLifecycleState.TP_HIT: SetupTransitionReason.TAKE_PROFIT_HIT,
        SetupLifecycleState.SL_HIT: SetupTransitionReason.STOP_LOSS_HIT,
        SetupLifecycleState.INVALIDATED: SetupTransitionReason.SETUP_INVALIDATED,
        SetupLifecycleState.EXPIRED: SetupTransitionReason.SETUP_EXPIRED,
    }
    for terminal_state, reason in terminal_reasons.items():
        terminal = transition_record(
            managing.record,
            terminal_state,
            reason=reason,
            now="2026-05-18T09:35:00+00:00",
        )
        assert terminal.record is not None
        assert terminal.record.setup_generation_id == generation_id
        assert terminal.event is not None
        assert terminal.event.lifecycle_id == generation_id


@pytest.mark.parametrize(
    "state",
    (
        SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.MANAGING,
        SetupLifecycleState.TP_HIT,
        SetupLifecycleState.SL_HIT,
        SetupLifecycleState.INVALIDATED,
        SetupLifecycleState.EXPIRED,
        SetupLifecycleState.COOLDOWN,
        SetupLifecycleState.ARCHIVED,
    ),
)
def test_same_known_structural_anchor_never_rotates_generation(state) -> None:
    anchor = "execution_sweep|15m|1789987200000"
    record = _record(state, lifecycle_id="generation-a").model_copy(
        update={
            "structural_anchor": anchor,
            "cooldown_until": "2026-05-18T09:30:00+00:00",
        }
    )

    reason = generation_rotation_reason(
        record,
        observed_structural_anchor=anchor,
        setup_observable=True,
        terminal_observation=state
        in {
            SetupLifecycleState.TP_HIT,
            SetupLifecycleState.SL_HIT,
            SetupLifecycleState.INVALIDATED,
            SetupLifecycleState.EXPIRED,
        },
        now="2026-05-18T10:00:00+00:00",
    )

    assert reason is None


def test_restart_reuses_active_generation_and_confirmation_progress(tmp_path) -> None:
    db_path = tmp_path / "generation-restart.db"
    candidate = _with_generation_anchor(_confirmed_candidate_symbol(), "sweep-a")

    first = apply_lifecycle_to_run_result(
        _scan_result(candidate),
        database_path=db_path,
        scan_run_id="generation-a-1",
        now="2026-05-18T09:00:00+00:00",
        confirmation_cycles=2,
    )
    first_record = first.results[0].lifecycle_state
    assert first_record is not None
    assert first_record.current_state == SetupLifecycleState.TRIGGERED
    assert first_record.confirmation_count == 1

    second = apply_lifecycle_to_run_result(
        _scan_result(candidate),
        database_path=db_path,
        scan_run_id="generation-a-2",
        now="2026-05-18T09:05:00+00:00",
        confirmation_cycles=2,
    )
    second_record = second.results[0].lifecycle_state
    assert second_record is not None
    assert second_record.current_state == SetupLifecycleState.CONFIRMED
    assert second_record.confirmation_count == 2
    assert second_record.lifecycle_id == first_record.lifecycle_id
    assert second_record.structural_anchor == "setup_generation_anchor|sweep-a"

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        restored = repository.get_record(
            symbol="BTCUSDT",
            mode="swing",
            direction="long",
        )
    assert restored is not None
    assert restored.lifecycle_id == first_record.lifecycle_id
    assert restored.confirmation_count == 2


def test_completed_same_geometry_new_sweep_creates_isolated_generation(tmp_path) -> None:
    db_path = tmp_path / "generation-isolation.db"
    setup_a = _with_generation_anchor(_confirmed_candidate_symbol(), "sweep-a")
    first = apply_lifecycle_to_run_result(
        _scan_result(setup_a),
        database_path=db_path,
        scan_run_id="generation-a",
        now="2026-05-18T09:00:00+00:00",
        confirmation_cycles=2,
    )
    generation_a = first.results[0].lifecycle_state
    assert generation_a is not None

    completed_a = generation_a.model_copy(
        update={
            "current_state": SetupLifecycleState.COOLDOWN,
            "previous_state": SetupLifecycleState.SL_HIT,
            "confirmation_count": 2,
            "confirmed_at": "2026-05-18T09:05:00+00:00",
            "cooldown_until": "2026-06-08T10:00:00+00:00",
        }
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(completed_a)

    setup_b = _with_generation_anchor(_confirmed_candidate_symbol(), "sweep-b")
    second = apply_lifecycle_to_run_result(
        _scan_result(setup_b),
        database_path=db_path,
        scan_run_id="generation-b",
        now="2026-06-08T09:00:00+00:00",
        confirmation_cycles=2,
    )
    generation_b = second.results[0].lifecycle_state
    assert generation_b is not None
    assert generation_b.lifecycle_id != generation_a.lifecycle_id
    assert generation_b.setup_identity == generation_a.setup_identity
    assert generation_b.current_state == SetupLifecycleState.TRIGGERED
    assert generation_b.confirmation_count == 1
    assert generation_b.confirmed_at is None
    assert generation_b.cooldown_until is None
    assert generation_b.first_seen_at == "2026-06-08T09:00:00+00:00"
    assert generation_b.structural_anchor == "setup_generation_anchor|sweep-b"

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        current = repository.get_record(
            symbol="BTCUSDT",
            mode="swing",
            direction="long",
        )
        historical = repository.get_record_by_lifecycle_id(generation_a.lifecycle_id)
        generations = repository.list_records_for_symbol(symbol="BTCUSDT")

    assert current is not None
    assert current.lifecycle_id == generation_b.lifecycle_id
    assert current.is_current is True
    assert historical is not None
    assert historical.is_current is False
    assert historical.confirmation_count == 2
    assert historical.cooldown_until == "2026-06-08T10:00:00+00:00"
    assert len(generations) == 2


def test_legacy_active_generation_is_reused_and_backfilled_conservatively() -> None:
    legacy = _record(
        SetupLifecycleState.TRIGGERED,
        lifecycle_id="legacy-generation",
    ).model_copy(update={"structural_anchor": NA, "confirmation_count": 1})

    assert generation_rotation_reason(
        legacy,
        observed_structural_anchor="execution_sweep|15m|1789987200000",
        setup_observable=True,
        terminal_observation=False,
        now="2026-05-18T09:05:00+00:00",
    ) is None


def test_structural_anchor_uses_execution_sweep_candle_timestamp() -> None:
    candidate = _confirmed_candidate_symbol()
    diagnostics = {
        "swing": {
            **dict(candidate.strategy_diagnostics["swing"]),
            "execution_sweep_candle_index": 1,
            "execution_timeframe": "15m",
        }
    }
    anchored = candidate.model_copy(
        update={
            "strategy_diagnostics": diagnostics,
            "lifecycle_execution_candles": (
                {"timestamp": 1789986300000},
                {"timestamp": 1789987200000},
            ),
            "lifecycle_execution_timeframe": "15m",
        }
    )

    observation = lifecycle_service_module.observation_from_symbol_result(anchored)

    assert observation.structural_anchor == (
        "execution_sweep|15m|1789987200000"
    )


def test_new_generation_does_not_inherit_locked_plan(tmp_path) -> None:
    db_path = tmp_path / "generation-plan-isolation.db"
    setup_a = _with_generation_anchor(_confirmed_candidate_symbol(), "sweep-a")
    first = apply_lifecycle_to_run_result(
        _scan_result(setup_a),
        database_path=db_path,
        scan_run_id="plan-a",
        now="2026-05-18T09:00:00+00:00",
        confirmation_cycles=2,
    )
    generation_a = first.results[0].lifecycle_state
    assert generation_a is not None
    locked_a = generation_a.model_copy(
        update={
            "current_state": SetupLifecycleState.CONFIRMED,
            "confirmation_count": 2,
            "confirmed_at": "2026-05-18T09:05:00+00:00",
        }
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(locked_a)

    setup_b = _with_generation_anchor(
        _confirmed_candidate_symbol(
            entry_low=Decimal("104"),
            entry_high=Decimal("106"),
            stop=Decimal("99"),
        ),
        "sweep-b",
    )
    second = apply_lifecycle_to_run_result(
        _scan_result(setup_b),
        database_path=db_path,
        scan_run_id="plan-b",
        now="2026-06-08T09:00:00+00:00",
        confirmation_cycles=2,
    )
    generation_b = second.results[0].lifecycle_state
    assert generation_b is not None
    assert generation_b.lifecycle_id != generation_a.lifecycle_id
    assert generation_b.entry_low == "104"
    assert generation_b.entry_high == "106"
    assert generation_b.stop_loss == "99"
    assert generation_b.invalidation_logic != locked_a.invalidation_logic
    assert generation_b.confirmation_count == 1
    assert generation_b.confirmed_at is None

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        historical = repository.get_record_by_lifecycle_id(generation_a.lifecycle_id)
    assert historical is not None
    assert historical.entry_low == "100"
    assert historical.entry_high == "102"
    assert historical.stop_loss == "95"
    assert historical.is_current is False
