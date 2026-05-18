from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from app.lifecycle.models import SetupLifecycleEvent, SetupLifecycleRecord, SetupLifecycleState, SetupTransitionReason
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import apply_lifecycle_to_run_result, prioritize_watch_symbols
from app.lifecycle.state_machine import LifecycleObservation, evaluate_lifecycle_transition, transition_record
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from app.research.queries import ResearchFilters, build_research_report
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
