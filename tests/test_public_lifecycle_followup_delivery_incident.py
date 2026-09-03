"""Live incident: publicly confirmed setups stopped emitting lifecycle follow-ups.

CONFIRMED was delivered publicly, but ZONE ENGAGED and every TP milestone were
silently lost. Confidence decay terminally EXPIRED the committed setup after a
few idle scans, which froze canonical outcome progress, so entry and target
milestones could never be persisted or projected to the public outbox.

These tests drive the real ``SetupLifecycleService`` state machine and the real
``TelegramLifecycleDeliveryService``, reopening SQLite between every stage so
persisted state is the only source of truth.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.alerts.telegram_lifecycle import SQLiteTelegramAlertAttemptRepository
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import (
    SetupLifecycleService,
    observation_from_symbol_result,
)
from app.lifecycle.state_machine import (
    evaluate_lifecycle_transition,
    observed_state,
)

from test_triggered_confirmed_telegram_delivery import (
    FakeSender,
    _public_setup_symbol,
    _run_result,
    _service,
    _trade_idea,
    run,
)

BASE = datetime(2026, 9, 2, 10, 0, tzinfo=UTC)
TIMEFRAME = "15m"


def _candle(index: int, *, high: str, low: str) -> dict[str, object]:
    opened = BASE + timedelta(minutes=15 * index)
    return {
        "timestamp": int(opened.timestamp() * 1000),
        "open": Decimal(low),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(high),
        "volume": Decimal("10"),
    }


def _decision(index: int) -> datetime:
    return BASE + timedelta(minutes=15 * (index + 1))


def _confirmed_symbol(signal_id: str):
    """A grade-A confirmed setup that is already publicly deliverable."""

    symbol = _public_setup_symbol(
        signal_id=signal_id,
        state=SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
    )
    strategy_diagnostics = {
        mode: {
            **diagnostics,
            "rr_to_tp2": Decimal("4"),
            "planned_rr": Decimal("4"),
            "opportunity_score": Decimal("95"),
        }
        for mode, diagnostics in symbol.strategy_diagnostics.items()
    }
    first_close = _decision(0).isoformat()
    record = symbol.lifecycle_state.model_copy(
        update={
            "first_seen_at": first_close,
            "last_seen_at": first_close,
            "last_transition_at": first_close,
            "confirmed_at": first_close,
            "rr": "4",
            "quality_grade_current": "A",
            "confirmation_count": 3,
            "required_confirmation_cycles": 2,
        }
    )
    transition = symbol.lifecycle_transition.model_copy(update={"record": record})
    return symbol.model_copy(
        update={
            "strategy_diagnostics": strategy_diagnostics,
            "trade_idea": _trade_idea(
                best_rr=Decimal("4"),
                opportunity_score=Decimal("95"),
            ),
            "lifecycle_state": record,
            "lifecycle_transition": transition,
        }
    )


def _idle_scan(db_path: Path, symbol_result, *, index: int):
    """One watch iteration where nothing about the setup changed."""

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        updated = SetupLifecycleService(db_path).apply_to_symbol_result(
            symbol_result,
            repository=repository,
            scan_run_id=f"idle-{index}",
            now=_decision(index).isoformat(),
        )
    return updated


def _evaluate(db_path: Path, lifecycle_id: str, candles: list[dict[str, object]]):
    """Advance canonical outcome progress from persisted state only."""

    index = len(candles) - 1
    decision = _decision(index)
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        record = repository.get_record_by_lifecycle_id(lifecycle_id)
        assert record is not None
        return evaluate_closed_candle_outcomes(
            record,
            execution_candles=candles,
            execution_timeframe=TIMEFRAME,
            decision_timestamp=decision,
            evaluated_at=decision.isoformat(),
            repository=repository,
            scan_run_id=f"outcome-{index}",
        )


def _deliver(db_path: Path, symbol_result, *, scan_run_id: str):
    """A fresh service object and sender, as a restarted runtime would build."""

    sender = FakeSender()
    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(symbol_result),
            scan_run_id=scan_run_id,
        )
    )
    return summary, sender


def _current_result(symbol_result, outcome):
    return symbol_result.model_copy(
        update={
            "lifecycle_state": outcome.record,
            "lifecycle_transition": None,
            "lifecycle_transitions": (),
            "lifecycle_outcome_progress": outcome.progress,
        }
    )


def _sent_public_events(db_path: Path) -> tuple[str, ...]:
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        rows = repository._connection.execute(
            """
            SELECT event_type FROM public_alert_events
            WHERE status = 'SENT' AND delivery_state = 'SENT'
            ORDER BY id
            """
        ).fetchall()
    return tuple(row["event_type"] for row in rows)


def test_idle_scans_after_public_confirmation_keep_the_full_followup_chain(
    tmp_path: Path,
) -> None:
    """CONFIRMED -> idle scans -> ZONE -> TP1 -> TP2 -> TP3, across restarts."""

    db_path = tmp_path / "followup.db"
    lifecycle_id = "followup-generation"
    confirmed = _confirmed_symbol(lifecycle_id)

    # --- Stage 1: public confirmation.
    summary, sender = _deliver(db_path, confirmed, scan_run_id="confirm")
    assert summary.sent == 1
    assert len(sender.messages) == 1
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(confirmed.lifecycle_state)

    # --- Stage 2: several idle watch iterations while price waits below the
    # zone. Confidence decay may downgrade the setup, but it must never turn a
    # publicly committed signal terminal.
    idle = confirmed
    for index in (1, 2, 3, 4, 5):
        idle = _idle_scan(db_path, idle, index=index)
        record = idle.lifecycle_state
        assert record is not None
        assert record.current_state == SetupLifecycleState.CONFIRMED, (
            f"idle scan {index} terminated a committed public setup: "
            f"{record.current_state} ({record.decay_reason})"
        )
        idle = confirmed.model_copy(update={"lifecycle_state": record})

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        persisted = repository.get_record_by_lifecycle_id(lifecycle_id)
    assert persisted is not None
    assert persisted.current_state == SetupLifecycleState.CONFIRMED
    # Decay is still observable; it simply is not terminal any more.
    assert persisted.decay_count >= 3
    assert persisted.decay_reason == "no price reaction or lifecycle progress"

    candles = [_candle(index, high="99", low="97") for index in range(4)]

    # --- Stage 3: price finally trades into the entry zone.
    candles.append(_candle(4, high="103", low="99"))
    entered = _evaluate(db_path, lifecycle_id, candles)
    assert entered.progress.entry_at == _decision(4).isoformat()
    assert entered.record.current_state == SetupLifecycleState.MANAGING
    summary, sender = _deliver(db_path, _current_result(confirmed, entered), scan_run_id="zone")
    assert summary.sent == 1
    assert _sent_public_events(db_path) == ("signal_confirmed", "limit_hit")

    # --- Stage 4: TP1.
    candles.append(_candle(5, high="110", low="103"))
    tp1 = _evaluate(db_path, lifecycle_id, candles)
    assert tp1.progress.tp1_at == _decision(5).isoformat()
    summary, sender = _deliver(db_path, _current_result(confirmed, tp1), scan_run_id="tp1")
    assert summary.sent == 1

    # --- Stage 5: TP2.
    candles.append(_candle(6, high="115", low="110"))
    tp2 = _evaluate(db_path, lifecycle_id, candles)
    assert tp2.progress.tp2_at == _decision(6).isoformat()
    summary, sender = _deliver(db_path, _current_result(confirmed, tp2), scan_run_id="tp2")
    assert summary.sent == 1

    # --- Stage 6: TP3.
    candles.append(_candle(7, high="120", low="115"))
    tp3 = _evaluate(db_path, lifecycle_id, candles)
    assert tp3.progress.tp3_at == _decision(7).isoformat()
    assert tp3.progress.terminal_outcome == SetupLifecycleState.TP_HIT.value
    summary, sender = _deliver(db_path, _current_result(confirmed, tp3), scan_run_id="tp3")
    assert summary.sent == 1

    assert _sent_public_events(db_path) == (
        "signal_confirmed",
        "limit_hit",
        "tp1_hit",
        "tp2_hit",
        "tp3_hit",
    )

    # --- Stage 7: a further restarted scan must not duplicate anything.
    summary, sender = _deliver(db_path, _current_result(confirmed, tp3), scan_run_id="repeat")
    assert summary.sent == 0
    assert sender.messages == []
    assert _sent_public_events(db_path) == (
        "signal_confirmed",
        "limit_hit",
        "tp1_hit",
        "tp2_hit",
        "tp3_hit",
    )


def test_confidence_decay_downgrades_confirmed_setup_without_expiring_it(
    tmp_path: Path,
) -> None:
    """Decay keeps degrading ranking but cannot manufacture a terminal state."""

    db_path = tmp_path / "decay.db"
    lifecycle_id = "decay-generation"
    confirmed = _confirmed_symbol(lifecycle_id)
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(confirmed.lifecycle_state)

    grades: list[str] = []
    current = confirmed
    for index in range(1, 7):
        current = _idle_scan(db_path, current, index=index)
        record = current.lifecycle_state
        assert record is not None
        grades.append(record.quality_grade_current)
        assert record.current_state == SetupLifecycleState.CONFIRMED
        assert record.failed_gate != "confidence_decay"
        current = confirmed.model_copy(update={"lifecycle_state": record})

    # A- then B+ then held at the decay floor rather than EXPIRED.
    assert grades[0] == "A-"
    assert grades[1] == "B+"
    assert set(grades[2:]) == {"B+"}
    assert current.lifecycle_state.decay_count == 6


def test_true_entry_window_expiry_still_expires_a_confirmed_setup(
    tmp_path: Path,
) -> None:
    """The canonical entry-window expiry contract is untouched by the fix."""

    del tmp_path
    lifecycle_id = "expiry-generation"
    confirmed = _confirmed_symbol(lifecycle_id)
    record = confirmed.lifecycle_state
    assert record is not None

    observation = observation_from_symbol_result(
        confirmed,
        min_score_for_idea=Decimal("80"),
    )
    # Sanity: without the canonical expiry signal the committed setup is held.
    held = evaluate_lifecycle_transition(
        record,
        observation,
        lifecycle_id=lifecycle_id,
        now=_decision(1).isoformat(),
    )
    assert held.record is not None
    assert held.record.current_state == SetupLifecycleState.CONFIRMED

    # The real entry window lapsing still retires the setup.
    lapsed = evaluate_lifecycle_transition(
        record,
        replace(observation, expired=True),
        lifecycle_id=lifecycle_id,
        now=_decision(2).isoformat(),
    )
    assert lapsed.record is not None
    assert lapsed.record.current_state == SetupLifecycleState.EXPIRED
    assert observed_state(replace(observation, expired=True)) == SetupLifecycleState.EXPIRED


def test_structural_invalidation_still_terminates_a_confirmed_setup(
    tmp_path: Path,
) -> None:
    """Invalidation semantics remain intact for committed public setups."""

    db_path = tmp_path / "invalidated.db"
    lifecycle_id = "invalidated-generation"
    confirmed = _confirmed_symbol(lifecycle_id)
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(confirmed.lifecycle_state)

    invalidated_diagnostics = {
        mode: {
            **diagnostics,
            "first_failed_gate": "structural_invalidation",
            "pullback_failure_type": "structural_acceptance",
            "acceptance_status": "accepted_beyond_invalidation",
        }
        for mode, diagnostics in confirmed.strategy_diagnostics.items()
    }
    broken = confirmed.model_copy(
        update={"strategy_diagnostics": invalidated_diagnostics}
    )

    updated = _idle_scan(db_path, broken, index=1)
    assert updated.lifecycle_state is not None
    assert updated.lifecycle_state.current_state == SetupLifecycleState.INVALIDATED


def test_uncommitted_candidate_states_still_expire_through_confidence_decay(
    tmp_path: Path,
) -> None:
    """Pre-commitment candidates keep their existing decay garbage collection."""

    db_path = tmp_path / "watchlisted.db"
    lifecycle_id = "watchlisted-generation"
    confirmed = _confirmed_symbol(lifecycle_id)
    watchlisted = confirmed.lifecycle_state.model_copy(
        update={
            "current_state": SetupLifecycleState.WATCHLISTED,
            "previous_state": SetupLifecycleState.DISCOVERED,
            "quality_grade_current": "A",
        }
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(watchlisted)

    # A watchlisted candidate is not publicly committed, so confidence decay
    # must still be able to retire it.
    from app.lifecycle.state_machine import (
        CONFIDENCE_DECAY_TERMINAL_EXEMPT_STATES,
        DECAYABLE_STATES,
    )

    assert SetupLifecycleState.WATCHLISTED in DECAYABLE_STATES
    assert SetupLifecycleState.WATCHLISTED not in CONFIDENCE_DECAY_TERMINAL_EXEMPT_STATES
    assert CONFIDENCE_DECAY_TERMINAL_EXEMPT_STATES == frozenset(
        {SetupLifecycleState.CONFIRMED}
    )
