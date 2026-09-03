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

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.alerts.telegram_lifecycle import SQLiteTelegramAlertAttemptRepository
from app.alerts.watchlist_expiry import WATCHLIST_NON_EXPIRING_STATE_KEYS
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import (
    SetupLifecycleService,
    observation_from_symbol_result,
)
from app.lifecycle.state_machine import (
    CONFIDENCE_DECAY_TERMINAL_EXEMPT_STATES,
    DECAYABLE_STATES,
    evaluate_lifecycle_transition,
    observed_state,
)
from app.strategies.liquidity_grab_pullback import (
    LiquidityGrabMode,
    analyze_liquidity_grab_pullback,
)

from test_liquidity_grab_pullback import _full_bullish_setup_candles
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


def _stale_bos_candles() -> list[dict[str, object]]:
    """A confirmed setup whose price ran away without ever tagging the zone."""

    candles = _full_bullish_setup_candles()
    for index in range(36, 60):
        candles.append(
            {
                "timestamp": index,
                "open": Decimal("107"),
                "high": Decimal("110"),
                "low": Decimal("106"),
                "close": Decimal("108"),
                "volume": Decimal("100"),
            }
        )
    return candles


def _strategy_violation_codes(mode: LiquidityGrabMode) -> tuple[str, ...]:
    candles = _stale_bos_candles()
    result = analyze_liquidity_grab_pullback(
        {
            "symbol": "BTCUSDT",
            "mode": mode,
            "candles_15m": candles,
            "candles_5m": candles,
        }
    )
    setup = getattr(result, mode.value)
    return tuple(violation.code for violation in setup.gate_result.violations)


def test_scalp_entry_window_expiry_is_generated_by_the_production_strategy() -> None:
    """SCALP has a real bars-since-BOS entry window; SWING does not."""

    assert "entry_window_expired" in _strategy_violation_codes(LiquidityGrabMode.scalp)
    assert "entry_window_expired" not in _strategy_violation_codes(
        LiquidityGrabMode.swing
    )


def test_production_entry_window_expiry_derives_observation_expired() -> None:
    """The scanner diagnostic drives observation.expired without manual toggling."""

    lifecycle_id = "scalp-expiry-generation"
    confirmed = _confirmed_symbol(lifecycle_id)
    lapsed = confirmed.model_copy(
        update={
            "strategy_diagnostics": {
                mode: {**diagnostics, "first_failed_gate": "entry_window_expired"}
                for mode, diagnostics in confirmed.strategy_diagnostics.items()
            }
        }
    )

    observation = observation_from_symbol_result(lapsed, min_score_for_idea=Decimal("80"))
    assert observation.failed_gate == "entry_window_expired"
    assert observation.expired is True
    assert observed_state(observation) == SetupLifecycleState.EXPIRED


def test_production_entry_window_expiry_expires_an_unfilled_a_grade_setup() -> None:
    """An -> EXPIRED path driven by a derived observation rather than a flag.

    ``A_GRADE_WATCH`` and ``ACTIONABLE_A_GRADE`` are the pre-fill states whose
    ``_next_state`` branches consult ``observation.expired`` without an
    invalidation precedence check, so they are where the canonical expiry
    contract is directly observable end to end.
    """

    lifecycle_id = "scalp-expiry-a-grade"
    confirmed = _confirmed_symbol(lifecycle_id)
    lapsed = confirmed.model_copy(
        update={
            "strategy_diagnostics": {
                mode: {**diagnostics, "first_failed_gate": "entry_window_expired"}
                for mode, diagnostics in confirmed.strategy_diagnostics.items()
            }
        }
    )
    observation = observation_from_symbol_result(lapsed, min_score_for_idea=Decimal("80"))
    assert observation.expired is True
    assert observation.invalidated is False

    for state in (
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.ACTIONABLE_A_GRADE,
    ):
        record = confirmed.lifecycle_state.model_copy(
            update={"current_state": state, "previous_state": SetupLifecycleState.TRIGGERED}
        )
        transition = evaluate_lifecycle_transition(
            record,
            observation,
            lifecycle_id=lifecycle_id,
            now=_decision(1).isoformat(),
        )
        assert transition.record is not None
        assert transition.record.current_state == SetupLifecycleState.EXPIRED, (
            f"{state} did not honour the canonical entry-window expiry"
        )


def test_production_entry_window_expiry_terminates_a_confirmed_setup() -> None:
    """A lapsed entry window still retires a confirmed setup.

    For CONFIRMED the state machine checks invalidation before expiry, and the
    same ``failed_gate`` that raises ``observation.expired`` also raises the
    ``failed_confirmation_gate`` blocker, so the canonical terminal state here
    is INVALIDATED rather than EXPIRED. Either way the setup is retired and
    canonical outcome tracking stops.
    """

    lifecycle_id = "scalp-expiry-confirmed"
    confirmed = _confirmed_symbol(lifecycle_id)
    record = confirmed.lifecycle_state
    assert record is not None

    held = evaluate_lifecycle_transition(
        record,
        observation_from_symbol_result(confirmed, min_score_for_idea=Decimal("80")),
        lifecycle_id=lifecycle_id,
        now=_decision(1).isoformat(),
    )
    assert held.record is not None
    assert held.record.current_state == SetupLifecycleState.CONFIRMED

    lapsed = confirmed.model_copy(
        update={
            "strategy_diagnostics": {
                mode: {**diagnostics, "first_failed_gate": "entry_window_expired"}
                for mode, diagnostics in confirmed.strategy_diagnostics.items()
            }
        }
    )
    observation = observation_from_symbol_result(lapsed, min_score_for_idea=Decimal("80"))
    assert observation.expired is True

    retired = evaluate_lifecycle_transition(
        record,
        observation,
        lifecycle_id=lifecycle_id,
        now=_decision(2).isoformat(),
    )
    assert retired.record is not None
    assert retired.record.current_state == SetupLifecycleState.INVALIDATED
    assert retired.record.failed_gate == "entry_window_expired"


def test_swing_confirmed_setup_has_no_entry_window_time_ceiling() -> None:
    """Documents the audited architectural gap for SWING.

    SWING never raises ``entry_window_expired``, so an unfilled confirmed SWING
    setup is retired only by structural invalidation, a closed-candle outcome,
    or generation rotation. There is deliberately no invented time ceiling here.
    """

    assert "entry_window_expired" not in _strategy_violation_codes(
        LiquidityGrabMode.swing
    )

    confirmed = _confirmed_symbol("swing-no-ceiling")
    observation = observation_from_symbol_result(confirmed, min_score_for_idea=Decimal("80"))
    assert observation.expired is False

    # Confirmed setups are excluded from the 48h public watchlist expiry sweep,
    # so that sweep cannot retire them either.
    assert "confirmed" in WATCHLIST_NON_EXPIRING_STATE_KEYS


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

    assert SetupLifecycleState.WATCHLISTED in DECAYABLE_STATES
    assert SetupLifecycleState.WATCHLISTED not in CONFIDENCE_DECAY_TERMINAL_EXEMPT_STATES
    assert CONFIDENCE_DECAY_TERMINAL_EXEMPT_STATES == frozenset(
        {SetupLifecycleState.CONFIRMED}
    )

    # A watchlisted candidate is not committed to anything, so repeated idle
    # scans must still garbage collect it through confidence decay.
    record = watchlisted
    states: list[SetupLifecycleState] = []
    for index in range(1, 9):
        transition = evaluate_lifecycle_transition(
            record,
            observation_from_symbol_result(
                confirmed.model_copy(update={"lifecycle_state": record}),
                min_score_for_idea=Decimal("80"),
            ),
            lifecycle_id=lifecycle_id,
            now=_decision(index).isoformat(),
        )
        assert transition.record is not None
        record = transition.record
        states.append(record.current_state)
        if record.current_state == SetupLifecycleState.EXPIRED:
            break

    assert record.current_state == SetupLifecycleState.EXPIRED, (
        f"confidence decay no longer retires uncommitted candidates: {states}"
    )
    assert record.failed_gate == "confidence_decay"
    assert record.decay_count >= 3
