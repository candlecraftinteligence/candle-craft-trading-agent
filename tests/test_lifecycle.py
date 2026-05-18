from __future__ import annotations

from decimal import Decimal

from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState, SetupTransitionReason
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import apply_lifecycle_to_run_result, prioritize_watch_symbols
from app.lifecycle.state_machine import LifecycleObservation, evaluate_lifecycle_transition, transition_record
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from app.research.queries import ResearchFilters, build_research_report


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

    confirmed = evaluate_lifecycle_transition(
        triggered.record,
        _observation(sweep_detected=True, structure_shift_detected=True, pullback_valid=True, rr_valid=True),
        lifecycle_id="life_1",
        now="2026-05-18T09:15:00+00:00",
    )
    assert confirmed.to_state == SetupLifecycleState.CONFIRMED

    executing = evaluate_lifecycle_transition(
        confirmed.record,
        _observation(valid_trade_idea=True, pullback_valid=True, rr_valid=True),
        lifecycle_id="life_1",
        now="2026-05-18T09:20:00+00:00",
    )
    assert executing.to_state == SetupLifecycleState.EXECUTING


def test_invalid_state_transition_rejected() -> None:
    result = transition_record(
        _record(SetupLifecycleState.WATCHLISTED),
        SetupLifecycleState.EXECUTING,
        reason=SetupTransitionReason.VALID_TRADE_IDEA,
        now="2026-05-18T09:00:00+00:00",
    )

    assert result.allowed is False
    assert result.transitioned is False
    assert "WATCHLISTED cannot move directly to EXECUTING" in result.notes


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

    assert symbols == ("STALKUSDT", "TRIGGERUSDT", "CONFIRMUSDT", "WATCHUSDT", "NEWUSDT")


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
    confirmed = evaluate_lifecycle_transition(
        triggered.record,
        _observation(sweep_detected=True, structure_shift_detected=True, pullback_valid=True, rr_valid=True),
        lifecycle_id="life_1",
        now="2026-05-18T09:15:00+00:00",
        scan_run_id="run_4",
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
