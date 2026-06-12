from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from app.analytics.setup_quality import SetupQualityGrade, SetupQualityResult, SetupQualityState
from app.analytics.symbol_health import SymbolHealthRecord
from app.data.dtos import NA
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
        "readiness_score": 70,
        "readiness_label": "WATCH",
        "quality_score": 65,
        "failed_gate": "rr_below_minimum",
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
        "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
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
                "bias": direction,
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": (),
                "entry_low": entry_low,
                "entry_high": entry_high,
                "stop": stop,
                "tp1": Decimal("110"),
                "tp2": Decimal("117"),
                "tp3": Decimal("124"),
                "rr_to_tp2": rr,
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
        _observation(
            sweep_detected=True,
            structure_shift_detected=True,
            pullback_valid=True,
            rr_valid=True,
            quality_score=82,
            quality_grade="B+",
            invalidation_reason="Invalid below stop.",
        ),
        lifecycle_id="life_1",
        now="2026-05-18T09:15:00+00:00",
    )
    assert pending_confirmation.to_state == SetupLifecycleState.TRIGGERED
    assert pending_confirmation.record is not None
    assert pending_confirmation.record.confirmation_count == 1

    confirmed = evaluate_lifecycle_transition(
        pending_confirmation.record,
        _observation(
            sweep_detected=True,
            structure_shift_detected=True,
            pullback_valid=True,
            rr_valid=True,
            quality_score=82,
            quality_grade="B+",
            invalidation_reason="Invalid below stop.",
        ),
        lifecycle_id="life_1",
        now="2026-05-18T09:20:00+00:00",
    )
    assert confirmed.to_state == SetupLifecycleState.CONFIRMED

    executing = evaluate_lifecycle_transition(
        confirmed.record,
        _observation(valid_trade_idea=True, pullback_valid=True, rr_valid=True, invalidation_reason="Invalid below stop."),
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
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.A_GRADE_WATCH
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
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.A_GRADE_WATCH


def test_a_grade_watch_does_not_become_active_before_limit_zone_touch(tmp_path) -> None:
    symbol_result = _apply_single_lifecycle(
        _a_grade_watch_symbol(latest_high=Decimal("110"), latest_low=Decimal("108")),
        tmp_path,
        now="2026-05-18T09:00:00+00:00",
    )

    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.A_GRADE_WATCH
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
    assert symbol_result.lifecycle_transition.from_state == SetupLifecycleState.A_GRADE_WATCH
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
    for state in (SetupLifecycleState.A_GRADE_WATCH, SetupLifecycleState.CONFIRMED):
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
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.A_GRADE_WATCH
    assert symbol_result.lifecycle_state.current_state != SetupLifecycleState.EXECUTING


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
    assert symbol_result.lifecycle_state.current_state == SetupLifecycleState.STALKING
    assert payload["results"][0]["lifecycle_state"]["current_state"] == "STALKING"
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
        _observation(
            sweep_detected=True,
            structure_shift_detected=True,
            pullback_valid=True,
            rr_valid=True,
            quality_score=82,
            quality_grade="B+",
            invalidation_reason="Invalid below stop.",
        ),
        lifecycle_id="life_1",
        now="2026-05-18T09:15:00+00:00",
        scan_run_id="run_4",
    )
    confirmed = evaluate_lifecycle_transition(
        pending_confirmation.record,
        _observation(
            sweep_detected=True,
            structure_shift_detected=True,
            pullback_valid=True,
            rr_valid=True,
            quality_score=82,
            quality_grade="B+",
            invalidation_reason="Invalid below stop.",
        ),
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
