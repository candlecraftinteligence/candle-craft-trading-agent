from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.lifecycle.models import (
    SetupLifecycleEvent,
    SetupLifecycleOutcomeProgress,
    SetupLifecycleRecord,
    SetupLifecycleState,
    SetupTransitionReason,
)
from app.lifecycle.outcomes import canonical_plan_identity, evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import SetupLifecycleService, observation_from_symbol_result
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerSymbolResult

BASE = datetime(2026, 1, 1, tzinfo=UTC)
TIMEFRAME = "5m"


def _record(
    *,
    lifecycle_id: str = "life-1",
    mode: str = "challenge",
    direction: str = "long",
    state: SetupLifecycleState = SetupLifecycleState.ACTIONABLE_A_GRADE,
) -> SetupLifecycleRecord:
    levels = (
        {
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "90",
            "tp1": "110",
            "tp2": "120",
            "tp3": "130",
        }
        if direction == "long"
        else {
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "112",
            "tp1": "92",
            "tp2": "84",
            "tp3": "76",
        }
    )
    return SetupLifecycleRecord(
        lifecycle_id=lifecycle_id,
        symbol="BTCUSDT",
        mode=mode,
        direction=direction,
        current_state=state,
        first_seen_at=BASE.isoformat(),
        last_seen_at=BASE.isoformat(),
        last_transition_at=BASE.isoformat(),
        confirmed_at=BASE.isoformat(),
        invalidation_reason="Closed structure beyond the stored stop invalidates the plan.",
        invalidation_logic="Closed structure beyond the stored stop invalidates the plan.",
        setup_identity=f"{lifecycle_id}-setup",
        **levels,
    )


def _candle(index: int, *, high: str, low: str) -> dict[str, object]:
    opened = BASE + timedelta(minutes=5 * index)
    return {
        "timestamp": int(opened.timestamp() * 1000),
        "open": Decimal(low),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(high),
        "volume": Decimal("10"),
    }


def _baseline(direction: str) -> dict[str, object]:
    if direction == "long":
        return _candle(0, high="99", low="95")
    return _candle(0, high="107", low="103")


def _entry(index: int, direction: str) -> dict[str, object]:
    del direction
    return _candle(index, high="103", low="99")


def _target(index: int, direction: str, target: int) -> dict[str, object]:
    if direction == "long":
        values = {
            1: ("111", "103"),
            2: ("121", "111"),
            3: ("131", "121"),
        }
    else:
        values = {
            1: ("99", "91"),
            2: ("91", "83"),
            3: ("83", "75"),
        }
    high, low = values[target]
    return _candle(index, high=high, low=low)


def _stop(index: int, direction: str) -> dict[str, object]:
    if direction == "long":
        return _candle(index, high="105", low="89")
    return _candle(index, high="113", low="99")


def _decision(index: int) -> str:
    return (BASE + timedelta(minutes=5 * (index + 1))).isoformat()


def _evaluate(
    repository: SQLiteSetupLifecycleRepository,
    record: SetupLifecycleRecord,
    candles: list[dict[str, object]],
    *,
    decision_index: int | None = None,
):
    index = len(candles) - 1 if decision_index is None else decision_index
    return evaluate_closed_candle_outcomes(
        record,
        execution_candles=candles,
        execution_timeframe=TIMEFRAME,
        decision_timestamp=_decision(index),
        evaluated_at=_decision(index),
        repository=repository,
        scan_run_id=f"scan-{index}",
    )


def _prime(
    repository: SQLiteSetupLifecycleRepository,
    *,
    direction: str = "long",
    lifecycle_id: str = "life-1",
    mode: str = "challenge",
):
    record = _record(lifecycle_id=lifecycle_id, mode=mode, direction=direction)
    repository.upsert_record(record)
    candles = [_baseline(direction)]
    result = _evaluate(repository, record, candles)
    assert result.progress is not None
    assert result.progress.evaluation_cursor_open_at == BASE.isoformat()
    assert result.progress.entry_at is None
    return result.record, candles, result.progress


def test_fresh_progress_evaluates_first_eligible_entry_candle_exactly_once(
    tmp_path: Path,
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record = _record()
        repository.upsert_record(record)

        result = _evaluate(repository, record, [_entry(0, "long")])

        assert result.processed_candles == 1
        assert result.progress is not None
        assert result.progress.tracking_start_at == BASE.isoformat()
        assert result.progress.evaluation_cursor_open_at == BASE.isoformat()
        assert result.progress.entry_at == _decision(0)
        assert result.progress.integrity_status == "Verified"
        assert result.progress.diagnostic == "N/A"
        assert result.record.current_state == SetupLifecycleState.MANAGING
        events = repository.list_events(lifecycle_id=record.lifecycle_id)
        assert sum(
            event.reason == SetupTransitionReason.ENTRY_ACTIVATED
            for event in events
        ) == 1


def test_fresh_no_touch_advances_cursor_and_next_candle_can_activate_entry(
    tmp_path: Path,
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record = _record()
        repository.upsert_record(record)
        candles = [_baseline("long")]

        first = _evaluate(repository, record, candles)
        assert first.processed_candles == 1
        assert first.progress.entry_at is None
        assert first.progress.evaluation_cursor_open_at == BASE.isoformat()

        candles.append(_entry(1, "long"))
        second = _evaluate(repository, first.record, candles)
        assert second.processed_candles == 1
        assert second.progress.entry_at == _decision(1)
        assert second.progress.evaluation_cursor_open_at == (
            BASE + timedelta(minutes=5)
        ).isoformat()


def test_tracking_boundary_excludes_pre_setup_candle(
    tmp_path: Path,
) -> None:
    boundary = BASE + timedelta(minutes=10)
    record = _record().model_copy(
        update={
            "last_seen_at": boundary.isoformat(),
            "last_transition_at": boundary.isoformat(),
            "confirmed_at": boundary.isoformat(),
        }
    )
    candles = [
        _entry(0, "long"),
        _candle(1, high="99", low="95"),
    ]
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        repository.upsert_record(record)
        result = _evaluate(repository, record, candles)

        expected_start = boundary
        assert result.processed_candles == 0
        assert result.progress.tracking_start_at == expected_start.isoformat()
        assert result.progress.evaluation_cursor_open_at is None
        assert result.progress.entry_at is None
        assert (
            json.loads(result.progress.metadata_json)["tracking_boundary_source"]
            == "lifecycle_confirmed_at"
        )

        candles.append(_candle(2, high="99", low="95"))
        next_result = _evaluate(repository, record, candles)
        assert next_result.processed_candles == 1
        assert next_result.progress.evaluation_cursor_open_at == expected_start.isoformat()


def test_partial_confirmation_candle_cannot_prove_entry_and_restart_is_stable(
    tmp_path: Path,
) -> None:
    boundary = BASE + timedelta(seconds=1)
    record = _record().model_copy(
        update={
            "confirmed_at": boundary.isoformat(),
            "last_seen_at": boundary.isoformat(),
            "last_transition_at": boundary.isoformat(),
        }
    )
    ambiguous_candles = [_entry(0, "long")]
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        repository.upsert_record(record)

        first = _evaluate(repository, record, ambiguous_candles)
        restarted = _evaluate(repository, record, ambiguous_candles)

        assert first.processed_candles == 0
        assert first.progress.entry_at is None
        assert first.progress.evaluation_cursor_open_at is None
        assert first.progress.tracking_start_at == (
            BASE + timedelta(minutes=5)
        ).isoformat()
        assert first.progress.integrity_status == "Verified"
        assert first.progress.diagnostic == "partial_boundary_candle_not_entry_eligible"
        metadata = json.loads(first.progress.metadata_json)
        assert (
            metadata["boundary_candle_eligibility_decision"]
            == "partially_overlapping_boundary"
        )
        assert restarted.progress.entry_at is None
        assert restarted.processed_candles == 0

        full_post_boundary = [*ambiguous_candles, _entry(1, "long")]
        entered = _evaluate(repository, record, full_post_boundary)

        assert entered.processed_candles == 1
        assert entered.progress.entry_at == _decision(1)
        evidence = json.loads(entered.progress.metadata_json)
        assert evidence["entry_evidence_type"] == (
            "fully_post_boundary_closed_execution_candle_range"
        )
        assert evidence["entry_evidence_eligibility_decision"] == "fully_post_boundary"
        assert evidence["tracking_boundary_timestamp"] == boundary.isoformat()
        assert evidence["causal_candle_open"] == (
            BASE + timedelta(minutes=5)
        ).isoformat()
        assert evidence["causal_candle_close"] == _decision(1)
        events = repository.list_events(lifecycle_id=record.lifecycle_id)
        entry_event = next(
            event
            for event in events
            if event.reason == SetupTransitionReason.ENTRY_ACTIVATED
        )
        event_evidence = json.loads(entry_event.notes)
        assert event_evidence["entry_evidence_type"] == evidence["entry_evidence_type"]
        assert event_evidence["candle_high"] == "103"
        assert event_evidence["candle_low"] == "99"


def test_legacy_unfilled_cursor_is_preserved_and_only_next_unseen_touch_can_enter(
    tmp_path: Path,
) -> None:
    boundary = BASE + timedelta(minutes=7)
    record = _record(lifecycle_id="legacy-prospective").model_copy(
        update={
            "confirmed_at": boundary.isoformat(),
            "last_seen_at": boundary.isoformat(),
            "last_transition_at": boundary.isoformat(),
        }
    )
    consumed_cursor = BASE + timedelta(minutes=15)
    progress = SetupLifecycleOutcomeProgress(
        lifecycle_id=record.lifecycle_id,
        plan_identity=canonical_plan_identity(record),
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        execution_timeframe=TIMEFRAME,
        tracking_start_at=consumed_cursor.isoformat(),
        evaluation_cursor_open_at=consumed_cursor.isoformat(),
        evaluation_cursor_close_at=(consumed_cursor + timedelta(minutes=5)).isoformat(),
        integrity_status="Verified",
        metadata_json=json.dumps({"source": "legacy-v19-runtime"}),
        first_evaluated_at=BASE.isoformat(),
        last_evaluated_at=(consumed_cursor + timedelta(minutes=5)).isoformat(),
    )
    consumed = [
        _candle(0, high="99", low="95"),
        _entry(1, "long"),
        _candle(2, high="99", low="95"),
        _candle(3, high="99", low="95"),
    ]
    db_path = tmp_path / "legacy-prospective.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        repository.upsert_outcome_progress(progress)

        deployed = _evaluate(repository, record, consumed)
        restarted = _evaluate(repository, record, consumed)

        assert deployed.processed_candles == 0
        assert deployed.progress is not None
        assert deployed.progress.entry_at is None
        assert deployed.progress.evaluation_cursor_open_at == consumed_cursor.isoformat()
        assert deployed.progress.evaluation_cursor_close_at == (
            consumed_cursor + timedelta(minutes=5)
        ).isoformat()
        assert deployed.progress.tracking_start_at == (
            consumed_cursor + timedelta(minutes=5)
        ).isoformat()
        metadata = json.loads(deployed.progress.metadata_json)
        assert metadata["entry_causality_migration"] == (
            "legacy_unfilled_cursor_adopted_prospectively"
        )
        assert metadata["legacy_evaluation_cursor_open_at_preserved"] == (
            consumed_cursor.isoformat()
        )
        assert restarted.processed_candles == 0
        assert restarted.progress.entry_at is None
        assert restarted.progress.evaluation_cursor_open_at == consumed_cursor.isoformat()
        assert not any(
            event.reason == SetupTransitionReason.ENTRY_ACTIVATED
            for event in repository.list_events(lifecycle_id=record.lifecycle_id)
        )

        next_unseen_touch = [*consumed, _entry(4, "long")]
        entered = _evaluate(repository, record, next_unseen_touch)

        assert entered.processed_candles == 1
        assert entered.progress.entry_at == _decision(4)
        assert entered.progress.evaluation_cursor_open_at == (
            BASE + timedelta(minutes=20)
        ).isoformat()
        assert sum(
            event.reason == SetupTransitionReason.ENTRY_ACTIVATED
            for event in repository.list_events(lifecycle_id=record.lifecycle_id)
        ) == 1


def test_legacy_cursor_candle_straddling_confirmation_is_not_reinterpreted(
    tmp_path: Path,
) -> None:
    cursor_open = BASE + timedelta(minutes=15)
    boundary = cursor_open + timedelta(seconds=1)
    record = _record(lifecycle_id="legacy-partial-cursor").model_copy(
        update={
            "confirmed_at": boundary.isoformat(),
            "last_seen_at": boundary.isoformat(),
            "last_transition_at": boundary.isoformat(),
        }
    )
    progress = SetupLifecycleOutcomeProgress(
        lifecycle_id=record.lifecycle_id,
        plan_identity=canonical_plan_identity(record),
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        execution_timeframe=TIMEFRAME,
        tracking_start_at=cursor_open.isoformat(),
        evaluation_cursor_open_at=cursor_open.isoformat(),
        evaluation_cursor_close_at=(cursor_open + timedelta(minutes=5)).isoformat(),
        integrity_status="Verified",
        metadata_json="{}",
        first_evaluated_at=BASE.isoformat(),
        last_evaluated_at=(cursor_open + timedelta(minutes=5)).isoformat(),
    )
    consumed = [
        _candle(0, high="99", low="95"),
        _candle(1, high="99", low="95"),
        _candle(2, high="99", low="95"),
        _entry(3, "long"),
    ]
    with SQLiteSetupLifecycleRepository(tmp_path / "legacy-partial.db") as repository:
        repository.upsert_record(record)
        repository.upsert_outcome_progress(progress)

        deployed = _evaluate(repository, record, consumed)

        assert deployed.processed_candles == 0
        assert deployed.progress.entry_at is None
        assert deployed.progress.evaluation_cursor_open_at == cursor_open.isoformat()
        assert deployed.progress.tracking_start_at == (
            cursor_open + timedelta(minutes=5)
        ).isoformat()

        next_full_miss = [*consumed, _candle(4, high="99", low="95")]
        waited = _evaluate(repository, record, next_full_miss)

        assert waited.processed_candles == 1
        assert waited.progress.entry_at is None
        assert waited.progress.evaluation_cursor_open_at == (
            BASE + timedelta(minutes=20)
        ).isoformat()


def test_confirmation_boundary_exactly_at_candle_open_allows_that_candle(
    tmp_path: Path,
) -> None:
    record = _record().model_copy(update={"confirmed_at": BASE.isoformat()})
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        repository.upsert_record(record)

        result = _evaluate(repository, record, [_entry(0, "long")])

        assert result.processed_candles == 1
        assert result.progress.entry_at == _decision(0)
        metadata = json.loads(result.progress.metadata_json)
        assert (
            metadata["boundary_candle_eligibility_decision"]
            == "boundary_exactly_at_candle_open"
        )


@pytest.mark.parametrize("direction", ["long", "short"])
def test_tp_and_stop_before_entry_do_not_count(tmp_path: Path, direction: str) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record, candles, _ = _prime(repository, direction=direction)
        if direction == "long":
            candles.append(_candle(1, high="115", low="105"))
            candles.append(_candle(2, high="95", low="89"))
        else:
            candles.append(_candle(1, high="99", low="90"))
            candles.append(_candle(2, high="113", low="105"))
        result = _evaluate(repository, record, candles)

        assert result.record.current_state != SetupLifecycleState.SL_HIT
        assert result.progress is not None
        assert result.progress.entry_at is None
        assert result.progress.tp1_at is None
        assert result.progress.stop_at is None
        assert result.progress.terminal_outcome == "N/A"


def test_invalidation_before_entry_remains_authoritative(tmp_path: Path) -> None:
    record = _record(state=SetupLifecycleState.INVALIDATED)
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        repository.upsert_record(record)
        result = _evaluate(repository, record, [_baseline("long")])

        assert result.record.current_state == SetupLifecycleState.INVALIDATED
        assert result.progress is not None
        assert result.progress.entry_at is None
        assert result.progress.stop_at is None
        assert result.progress.invalidated_at == record.last_transition_at
        assert result.progress.terminal_outcome == SetupLifecycleState.INVALIDATED.value


@pytest.mark.parametrize("direction", ["long", "short"])
def test_entry_is_recorded_exactly_once_and_survives_restart(
    tmp_path: Path,
    direction: str,
) -> None:
    db_path = tmp_path / "outcomes.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        record, candles, _ = _prime(repository, direction=direction)
        candles.append(_entry(1, direction))
        first = _evaluate(repository, record, candles)
        entry_at = first.progress.entry_at
        candles.append(_entry(2, direction))
        repeated = _evaluate(repository, first.record, candles)
        entry_events = [
            event
            for event in repository.list_events(lifecycle_id=record.lifecycle_id)
            if event.reason == SetupTransitionReason.ENTRY_ACTIVATED
        ]
        assert repeated.progress.entry_at == entry_at
        assert len(entry_events) == 1

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        stored_record = repository.get_record_by_lifecycle_id(record.lifecycle_id)
        stored_progress = repository.get_outcome_progress(
            lifecycle_id=record.lifecycle_id,
            plan_identity=canonical_plan_identity(stored_record),
        )
        assert stored_progress is not None
        assert stored_progress.entry_at == entry_at


@pytest.mark.parametrize("direction", ["long", "short"])
def test_tp1_tp2_tp3_are_sequential_idempotent_and_tp3_is_terminal(
    tmp_path: Path,
    direction: str,
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record, candles, _ = _prime(repository, direction=direction)
        candles.append(_entry(1, direction))
        entered = _evaluate(repository, record, candles)
        candles.append(_target(2, direction, 1))
        tp1 = _evaluate(repository, entered.record, candles)
        candles.append(_target(3, direction, 2))
        tp2 = _evaluate(repository, tp1.record, candles)
        candles.append(_target(4, direction, 3))
        tp3 = _evaluate(repository, tp2.record, candles)
        repeated = _evaluate(repository, tp3.record, candles)

        assert tp1.progress.tp1_at == _decision(2)
        assert tp1.progress.tp2_at is None
        assert tp2.progress.tp1_at == _decision(2)
        assert tp2.progress.tp2_at == _decision(3)
        assert tp3.progress.tp3_at == _decision(4)
        assert tp3.progress.outcome_at == _decision(4)
        assert tp3.progress.terminal_outcome == SetupLifecycleState.TP_HIT.value
        assert tp3.record.current_state == SetupLifecycleState.TP_HIT
        assert repeated.record.current_state == SetupLifecycleState.TP_HIT
        reasons = [
            event.reason
            for event in repository.list_events(lifecycle_id=record.lifecycle_id)
        ]
        assert reasons.count(SetupTransitionReason.TP1_MILESTONE) == 1
        assert reasons.count(SetupTransitionReason.TP2_MILESTONE) == 1
        assert reasons.count(SetupTransitionReason.TP3_MILESTONE) == 1
        assert reasons.count(SetupTransitionReason.TAKE_PROFIT_HIT) == 1


@pytest.mark.parametrize(
    ("direction", "targets_before_stop"),
    [
        ("long", 0),
        ("long", 1),
        ("long", 2),
        ("short", 0),
        ("short", 1),
        ("short", 2),
    ],
)
def test_stop_after_entry_preserves_prior_targets(
    tmp_path: Path,
    direction: str,
    targets_before_stop: int,
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record, candles, _ = _prime(repository, direction=direction)
        candles.append(_entry(1, direction))
        result = _evaluate(repository, record, candles)
        for number in range(1, targets_before_stop + 1):
            candles.append(_target(len(candles), direction, number))
            result = _evaluate(repository, result.record, candles)
        candles.append(_stop(len(candles), direction))
        stopped = _evaluate(repository, result.record, candles)

        assert stopped.record.current_state == SetupLifecycleState.SL_HIT
        assert stopped.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
        assert stopped.progress.stop_at == _decision(len(candles) - 1)
        for number in range(1, 4):
            value = getattr(stopped.progress, f"tp{number}_at")
            assert (value is not None) is (number <= targets_before_stop)


@pytest.mark.parametrize("direction", ["long", "short"])
def test_entry_and_stop_same_candle_resolves_to_stop_without_targets(
    tmp_path: Path,
    direction: str,
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record, candles, _ = _prime(repository, direction=direction)
        candles.append(
            _candle(1, high="103", low="89")
            if direction == "long"
            else _candle(1, high="113", low="99")
        )
        result = _evaluate(repository, record, candles)

        assert result.progress.entry_at == _decision(1)
        assert result.progress.stop_at == _decision(1)
        assert result.progress.tp1_at is None
        assert result.record.current_state == SetupLifecycleState.SL_HIT
        stop_event = next(
            event
            for event in repository.list_events(lifecycle_id=record.lifecycle_id)
            if event.reason == SetupTransitionReason.STOP_LOSS_HIT
        )
        assert "entry_and_stop_same_candle_stop_wins" in stop_event.notes


@pytest.mark.parametrize("direction", ["long", "short"])
def test_entry_and_target_same_candle_does_not_award_ambiguous_target(
    tmp_path: Path,
    direction: str,
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record, candles, _ = _prime(repository, direction=direction)
        candles.append(
            _candle(1, high="115", low="99")
            if direction == "long"
            else _candle(1, high="103", low="90")
        )
        result = _evaluate(repository, record, candles)

        assert result.progress.entry_at == _decision(1)
        assert result.progress.tp1_at is None
        assert result.progress.stop_at is None
        assert result.record.current_state == SetupLifecycleState.MANAGING
        assert tuple(item.to_state for item in result.transitions) == (
            SetupLifecycleState.TRIGGERED,
            SetupLifecycleState.CONFIRMED,
            SetupLifecycleState.EXECUTING,
            SetupLifecycleState.MANAGING,
        )
        assert result.last_transition == result.transitions[-1]


@pytest.mark.parametrize("direction", ["long", "short"])
def test_post_entry_stop_and_target_same_candle_stop_wins(
    tmp_path: Path,
    direction: str,
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record, candles, _ = _prime(repository, direction=direction)
        candles.append(_entry(1, direction))
        entered = _evaluate(repository, record, candles)
        candles.append(
            _candle(2, high="115", low="89")
            if direction == "long"
            else _candle(2, high="113", low="90")
        )
        result = _evaluate(repository, entered.record, candles)

        assert result.record.current_state == SetupLifecycleState.SL_HIT
        assert result.progress.tp1_at is None
        stop_event = next(
            event
            for event in repository.list_events(lifecycle_id=record.lifecycle_id)
            if event.reason == SetupTransitionReason.STOP_LOSS_HIT
        )
        assert "post_entry_stop_and_target_same_candle_stop_wins" in stop_event.notes


@pytest.mark.parametrize("direction", ["long", "short"])
def test_multiple_targets_in_one_candle_record_sequential_milestones(
    tmp_path: Path,
    direction: str,
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record, candles, _ = _prime(repository, direction=direction)
        candles.append(_entry(1, direction))
        entered = _evaluate(repository, record, candles)
        candles.append(
            _candle(2, high="125", low="103")
            if direction == "long"
            else _candle(2, high="99", low="80")
        )
        result = _evaluate(repository, entered.record, candles)

        assert result.progress.tp1_at == _decision(2)
        assert result.progress.tp2_at == _decision(2)
        assert result.progress.tp3_at is None
        reasons = [
            event.reason
            for event in repository.list_events(lifecycle_id=record.lifecycle_id)
        ]
        assert reasons.index(SetupTransitionReason.TP1_MILESTONE) < reasons.index(
            SetupTransitionReason.TP2_MILESTONE
        )


def test_open_future_candle_cannot_activate_entry(tmp_path: Path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record, candles, _ = _prime(repository)
        candles.append(_entry(1, "long"))
        before_close = evaluate_closed_candle_outcomes(
            record,
            execution_candles=candles,
            execution_timeframe=TIMEFRAME,
            decision_timestamp=BASE + timedelta(minutes=7),
            evaluated_at=(BASE + timedelta(minutes=7)).isoformat(),
            repository=repository,
        )
        assert before_close.progress.entry_at is None

        after_close = _evaluate(repository, before_close.record, candles)
        assert after_close.progress.entry_at == _decision(1)


@pytest.mark.parametrize(
    "bad_candles",
    [
        [_baseline("long"), _candle(1, high="103", low="99"), _candle(1, high="104", low="98")],
        [_baseline("long"), _candle(2, high="103", low="99"), _candle(1, high="104", low="98")],
    ],
)
def test_duplicate_or_out_of_order_candles_do_not_change_state(
    tmp_path: Path,
    bad_candles: list[dict[str, object]],
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record, _, original = _prime(repository)
        result = _evaluate(repository, record, bad_candles)

        assert result.record.current_state == record.current_state
        assert result.progress.entry_at is None
        assert result.progress.evaluation_cursor_open_at == original.evaluation_cursor_open_at
        assert result.progress.integrity_status == "Unverified"
        assert "candle_integrity" in result.progress.diagnostic


def test_missing_or_gapped_history_creates_precise_diagnostic(tmp_path: Path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record = _record()
        repository.upsert_record(record)
        missing = _evaluate(repository, record, [])
        assert missing.progress.diagnostic == "missing_execution_candle_history"

        repository.upsert_record(record)
        baseline = _evaluate(repository, record, [_baseline("long")])
        gapped = _evaluate(
            repository,
            baseline.record,
            [_baseline("long"), _candle(2, high="103", low="99")],
        )
        assert "continuity_gap" in gapped.progress.diagnostic
        assert gapped.progress.entry_at is None


def test_history_starting_after_boundary_does_not_weaken_data_health_gate(
    tmp_path: Path,
) -> None:
    record = _record()
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        repository.upsert_record(record)

        result = _evaluate(
            repository,
            record,
            [_entry(1, "long")],
            decision_index=1,
        )

        assert result.progress.entry_at is None
        assert result.progress.integrity_status == "Unverified"
        assert "missing_execution_candle_history" in result.progress.diagnostic


def test_restart_catches_up_all_downtime_candles_in_order(tmp_path: Path) -> None:
    db_path = tmp_path / "outcomes.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        record, candles, baseline = _prime(repository)

    candles.extend(
        [
            _entry(1, "long"),
            _target(2, "long", 1),
            _target(3, "long", 2),
        ]
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        stored = repository.get_record_by_lifecycle_id(record.lifecycle_id)
        result = _evaluate(repository, stored, candles)
        assert result.processed_candles == 3
        assert result.progress.entry_at == _decision(1)
        assert result.progress.tp1_at == _decision(2)
        assert result.progress.tp2_at == _decision(3)
        assert result.progress.evaluation_cursor_open_at != baseline.evaluation_cursor_open_at


def test_persisted_cursor_never_rewinds_when_history_is_stale(tmp_path: Path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record, candles, _ = _prime(repository)
        candles.append(_entry(1, "long"))
        entered = _evaluate(repository, record, candles)
        cursor = entered.progress.evaluation_cursor_open_at
        entry_at = entered.progress.entry_at

        stale = _evaluate(
            repository,
            entered.record,
            [_baseline("long")],
            decision_index=0,
        )

        assert stale.processed_candles == 0
        assert stale.progress.evaluation_cursor_open_at == cursor
        assert stale.progress.entry_at == entry_at
        assert stale.progress.integrity_status == "Unverified"
        assert "stale_execution_candle_history" in stale.progress.diagnostic


def test_numeric_presentation_jitter_keeps_one_canonical_progress_row(
    tmp_path: Path,
) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record = _record().model_copy(
            update={
                "entry_low": "100.0",
                "entry_high": "102.00",
                "stop_loss": "90.000",
                "tp1": "110.0",
                "tp2": "120.00",
                "tp3": "130.000",
            }
        )
        repository.upsert_record(record)
        first = _evaluate(repository, record, [_baseline("long")])
        first_identity = first.progress.plan_identity

        equivalent = first.record.model_copy(
            update={
                "entry_low": "100.0000",
                "entry_high": "102.0",
                "stop_loss": "90.0",
                "tp1": "110.000",
                "tp2": "120.0",
                "tp3": "130.00",
            }
        )
        repository.upsert_record(equivalent)
        second = _evaluate(
            repository,
            equivalent,
            [_baseline("long"), _candle(1, high="99", low="95")],
        )

        assert canonical_plan_identity(equivalent) == first_identity
        assert second.progress.plan_identity == first_identity
        assert second.processed_candles == 1
        assert len(repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)) == 1


def test_new_plan_and_multiple_plans_never_exchange_progress(tmp_path: Path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        record, candles, old_progress = _prime(repository)
        candles.append(_entry(1, "long"))
        entered = _evaluate(repository, record, candles)
        old_plan_identity = entered.progress.plan_identity

        replacement = entered.record.model_copy(
            update={
                "current_state": SetupLifecycleState.ACTIONABLE_A_GRADE,
                "entry_low": "200",
                "entry_high": "202",
                "stop_loss": "190",
                "tp1": "210",
                "tp2": "220",
                "tp3": "230",
                "invalidation_logic": "Closed structure below 190 invalidates the replacement plan.",
            }
        )
        repository.upsert_record(replacement)
        replacement_result = _evaluate(
            repository,
            replacement,
            [_candle(0, high="199", low="195")],
        )
        assert replacement_result.progress.plan_identity != old_plan_identity
        assert replacement_result.progress.entry_at is None

        second = _record(lifecycle_id="life-2", mode="swing", direction="long")
        repository.upsert_record(second)
        second_result = _evaluate(repository, second, [_baseline("long")])
        assert second_result.progress.plan_identity not in {
            old_plan_identity,
            replacement_result.progress.plan_identity,
        }
        all_progress = repository.list_outcome_progress(symbol="BTCUSDT")
        assert len(all_progress) == 3
        assert sum(item.entry_at is not None for item in all_progress) == 1

def test_scanner_result_hands_finalized_execution_candles_to_lifecycle(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "outcomes.db"
    record = _record(state=SetupLifecycleState.ACTIONABLE_A_GRADE)
    baseline = _baseline("long")
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        strategy_diagnostics={
            "challenge": {
                "mode": "challenge",
                "bias": "long",
                "invalidation": record.invalidation_logic,
            }
        },
        valid_strategy_modes=("challenge",),
        lifecycle_execution_candles=(baseline,),
        lifecycle_execution_timeframe=TIMEFRAME,
        lifecycle_decision_timestamp=BASE + timedelta(minutes=5),
    )
    assert observation_from_symbol_result(symbol_result).closed_candle_outcomes_managed is True
    assert "lifecycle_execution_candles" not in symbol_result.model_dump()

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        service = SetupLifecycleService(db_path)
        initialized = service.apply_to_symbol_result(
            symbol_result,
            repository=repository,
            scan_run_id="scan-0",
            now=_decision(0),
        )
        assert initialized.lifecycle_outcome_progress is not None
        assert initialized.lifecycle_outcome_progress.entry_at is None

        entry_result = symbol_result.model_copy(
            update={
                "lifecycle_execution_candles": (baseline, _entry(1, "long")),
                "lifecycle_decision_timestamp": BASE + timedelta(minutes=10),
            }
        )
        activated = service.apply_to_symbol_result(
            entry_result,
            repository=repository,
            scan_run_id="scan-1",
            now=_decision(1),
        )
        assert activated.lifecycle_outcome_progress.entry_at == _decision(1)
        assert activated.lifecycle_state.current_state == SetupLifecycleState.MANAGING
        assert tuple(item.to_state for item in activated.lifecycle_transitions) == (
            SetupLifecycleState.TRIGGERED,
            SetupLifecycleState.CONFIRMED,
            SetupLifecycleState.EXECUTING,
            SetupLifecycleState.MANAGING,
        )


def test_terminal_progress_is_queryable_once_and_public_tables_remain_silent(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "outcomes.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        record, candles, _ = _prime(repository)
        candles.append(_entry(1, "long"))
        entered = _evaluate(repository, record, candles)
        candles.append(_target(2, "long", 3))
        terminal = _evaluate(repository, entered.record, candles)
        repeated = _evaluate(repository, terminal.record, candles)

        rows = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)
        assert len(rows) == 1
        assert rows[0].terminal_outcome == SetupLifecycleState.TP_HIT.value
        assert repeated.progress.outcome_at == terminal.progress.outcome_at

    with sqlite3.connect(db_path) as connection:
        telegram_attempts = connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts"
        ).fetchone()[0]
        public_events = connection.execute(
            "SELECT COUNT(*) FROM public_alert_events"
        ).fetchone()[0]
    assert telegram_attempts == 0
    assert public_events == 0


def test_invalid_stored_geometry_is_visible_and_cannot_invent_outcome(tmp_path: Path) -> None:
    record = _record().model_copy(update={"stop_loss": "105"})
    with SQLiteSetupLifecycleRepository(tmp_path / "outcomes.db") as repository:
        repository.upsert_record(record)
        result = _evaluate(repository, record, [_baseline("long")])

        assert result.record.current_state == record.current_state
        assert result.progress is None
        assert repository.list_outcome_progress(lifecycle_id=record.lifecycle_id) == ()



def test_terminal_outcome_reaches_analytics_consumer_exactly_once(tmp_path: Path) -> None:
    db_path = tmp_path / "outcomes.db"
    record = _record(state=SetupLifecycleState.MANAGING)
    baseline = _baseline("long")
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        strategy_diagnostics={
            "challenge": {
                "mode": "challenge",
                "bias": "long",
                "invalidation": record.invalidation_logic,
            }
        },
        valid_strategy_modes=("challenge",),
        lifecycle_execution_candles=(baseline,),
        lifecycle_execution_timeframe=TIMEFRAME,
        lifecycle_decision_timestamp=BASE + timedelta(minutes=5),
    )

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        service = SetupLifecycleService(db_path)
        initialized = service.apply_to_symbol_result(
            symbol_result,
            repository=repository,
            scan_run_id="scan-0",
            now=_decision(0),
        )
        entered_symbol = symbol_result.model_copy(
            update={
                "lifecycle_execution_candles": (baseline, _entry(1, "long")),
                "lifecycle_decision_timestamp": BASE + timedelta(minutes=10),
            }
        )
        entered = service.apply_to_symbol_result(
            entered_symbol,
            repository=repository,
            scan_run_id="scan-1",
            now=_decision(1),
        )
        terminal_symbol = symbol_result.model_copy(
            update={
                "lifecycle_execution_candles": (
                    baseline,
                    _entry(1, "long"),
                    _target(2, "long", 3),
                ),
                "lifecycle_decision_timestamp": BASE + timedelta(minutes=15),
            }
        )
        terminal = service.apply_to_symbol_result(
            terminal_symbol,
            repository=repository,
            scan_run_id="scan-2",
            now=_decision(2),
        )
        analytics = repository.list_outcome_analytics(symbol="BTCUSDT")
        assert terminal.lifecycle_state.current_state == SetupLifecycleState.TP_HIT
        assert len(analytics) == 1
        assert analytics[0].final_outcome == "TP3_HIT"
        payload = json.loads(analytics[0].raw_payload_json)
        assert payload["outcome_progress"]["entry_at"] == _decision(1)
        assert payload["outcome_progress"]["tp1_at"] == _decision(2)
        assert payload["outcome_progress"]["tp2_at"] == _decision(2)
        assert payload["outcome_progress"]["tp3_at"] == _decision(2)

        repeated = service.apply_to_symbol_result(
            terminal_symbol,
            repository=repository,
            scan_run_id="scan-3",
            now=_decision(3),
        )
        repeated_analytics = repository.list_outcome_analytics(symbol="BTCUSDT")
        assert repeated.lifecycle_state.current_state == SetupLifecycleState.TP_HIT
        assert len(repeated_analytics) == 1
        assert repeated_analytics[0].raw_payload_json == analytics[0].raw_payload_json
