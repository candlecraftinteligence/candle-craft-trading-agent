from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.alerts.telegram_lifecycle import SQLiteTelegramAlertAttemptRepository
from app.formatters.telegram_signal_formatter import TelegramAlertType
from app.lifecycle.models import SetupLifecycleState, SetupTransitionReason
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository

from test_triggered_confirmed_telegram_delivery import (
    FakeSender,
    _empty_run_result,
    _public_setup_symbol,
    _run_result,
    _service,
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
    symbol = _public_setup_symbol(
        signal_id=signal_id,
        state=SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
    )
    first_close = _decision(0).isoformat()
    record = symbol.lifecycle_state.model_copy(
        update={
            "first_seen_at": first_close,
            "last_seen_at": first_close,
            "last_transition_at": first_close,
            "confirmed_at": first_close,
        }
    )
    transition = symbol.lifecycle_transition.model_copy(update={"record": record})
    if transition.event is not None:
        transition = transition.model_copy(
            update={
                "event": transition.event.model_copy(
                    update={"timestamp": first_close}
                )
            }
        )
    return symbol.model_copy(
        update={
            "lifecycle_state": record,
            "lifecycle_transition": transition,
        }
    )


def _evaluate(
    db_path: Path,
    lifecycle_id: str,
    candles: list[dict[str, object]],
):
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
            scan_run_id=f"incident-scan-{index + 1}",
        )


def _sent_market_outcomes(db_path: Path) -> tuple[str, ...]:
    wanted = {
        TelegramAlertType.LIMIT_HIT.value,
        TelegramAlertType.TP1_HIT.value,
        TelegramAlertType.TP2_HIT.value,
        TelegramAlertType.TP3_HIT.value,
        TelegramAlertType.SL_HIT.value,
    }
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        return tuple(
            item.alert_type
            for item in repository.list_attempts()
            if item.telegram_status == "sent" and item.alert_type in wanted
        )


def test_confirmed_first_candle_entry_then_tp1_tp2_tp3_end_to_end(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "incident.db"
    sender = FakeSender()
    confirmed = _confirmed_symbol("incident-generation")
    root = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(confirmed),
            scan_run_id="incident-confirmed",
        )
    )
    assert root.sent == 1
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(confirmed.lifecycle_state)

    candles = [_candle(0, high="103", low="99")]
    entered = _evaluate(
        db_path,
        confirmed.lifecycle_state.lifecycle_id,
        candles,
    )
    away_from_zone = confirmed.model_copy(
        update={
            "current_price": Decimal("150"),
            "lifecycle_state": entered.record,
            "lifecycle_transition": None,
            "lifecycle_transitions": (),
            "lifecycle_outcome_progress": entered.progress,
        }
    )
    limit = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(away_from_zone),
            scan_run_id="incident-entry",
        )
    )
    assert entered.progress.entry_at == _decision(0).isoformat()
    assert entered.record.current_state == SetupLifecycleState.MANAGING
    assert limit.sent == 1
    assert _sent_market_outcomes(db_path) == (TelegramAlertType.LIMIT_HIT.value,)

    candles.append(_candle(1, high="108", low="103"))
    tp1_progress = _evaluate(db_path, entered.record.lifecycle_id, candles)
    tp1 = run(
        _service(db_path, sender).deliver_for_run(
            _empty_run_result(),
            scan_run_id="incident-tp1",
        )
    )
    assert tp1_progress.progress.tp1_at == _decision(1).isoformat()
    assert tp1.sent == 1

    candles.append(_candle(2, high="113", low="108"))
    tp2_progress = _evaluate(db_path, entered.record.lifecycle_id, candles)
    tp2 = run(
        _service(db_path, sender).deliver_for_run(
            _empty_run_result(),
            scan_run_id="incident-tp2",
        )
    )
    assert tp2_progress.progress.tp2_at == _decision(2).isoformat()
    assert tp2.sent == 1

    candles.append(_candle(3, high="118", low="113"))
    tp3_progress = _evaluate(db_path, entered.record.lifecycle_id, candles)
    tp3 = run(
        _service(db_path, sender).deliver_for_run(
            _empty_run_result(),
            scan_run_id="incident-tp3",
        )
    )
    assert tp3_progress.progress.tp3_at == _decision(3).isoformat()
    assert tp3_progress.progress.terminal_outcome == SetupLifecycleState.TP_HIT.value
    assert tp3_progress.record.current_state == SetupLifecycleState.TP_HIT
    assert tp3.sent == 1

    repeated = run(
        _service(db_path, FakeSender()).deliver_for_run(
            _empty_run_result(),
            scan_run_id="incident-restart",
        )
    )
    assert repeated.sent == 0
    assert _sent_market_outcomes(db_path) == (
        TelegramAlertType.LIMIT_HIT.value,
        TelegramAlertType.TP1_HIT.value,
        TelegramAlertType.TP2_HIT.value,
        TelegramAlertType.TP3_HIT.value,
    )
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
        assert sum(item.alert_type == TelegramAlertType.WATCHLIST.value for item in attempts) == 0
        assert {
            row["event_type"]: row["count"]
            for row in repository._connection.execute(
                """
                SELECT event_type, COUNT(*) AS count
                FROM public_alert_events
                WHERE event_type IN ('limit_hit', 'tp1_hit', 'tp2_hit', 'tp3_hit')
                GROUP BY event_type
                """
            ).fetchall()
        } == {
            "limit_hit": 1,
            "tp1_hit": 1,
            "tp2_hit": 1,
            "tp3_hit": 1,
        }
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        reasons = tuple(
            event.reason
            for event in repository.list_events(
                lifecycle_id=confirmed.lifecycle_state.lifecycle_id
            )
        )
        assert reasons.count(SetupTransitionReason.ENTRY_ACTIVATED) == 1
        assert reasons.count(SetupTransitionReason.TP1_MILESTONE) == 1
        assert reasons.count(SetupTransitionReason.TP2_MILESTONE) == 1
        assert reasons.count(SetupTransitionReason.TP3_MILESTONE) == 1


def test_confirmed_without_entry_touch_has_no_limit_or_tp_milestone(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "no-entry.db"
    sender = FakeSender()
    confirmed = _confirmed_symbol("no-entry-generation")
    assert run(
        _service(db_path, sender).deliver_for_run(
            _run_result(confirmed),
            scan_run_id="no-entry-confirmed",
        )
    ).sent == 1
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(confirmed.lifecycle_state)

    # This candle trades through all target prices but remains above the entry
    # zone, so targets cannot exist without canonical entry activation.
    progress = _evaluate(
        db_path,
        confirmed.lifecycle_state.lifecycle_id,
        [_candle(0, high="118", low="103")],
    )
    current = confirmed.model_copy(
        update={
            "current_price": Decimal("118"),
            "lifecycle_state": progress.record,
            "lifecycle_transition": None,
            "lifecycle_transitions": (),
            "lifecycle_outcome_progress": progress.progress,
        }
    )
    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(current),
            scan_run_id="no-entry-outcomes",
        )
    )

    assert summary.sent == 0
    assert progress.progress.entry_at is None
    assert progress.progress.tp1_at is None
    assert progress.progress.tp2_at is None
    assert progress.progress.tp3_at is None
    assert _sent_market_outcomes(db_path) == ()


def test_confirmed_invalidation_before_entry_sends_only_invalidated(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "invalid-before-entry.db"
    sender = FakeSender()
    confirmed = _confirmed_symbol("invalid-before-entry-generation")
    assert run(
        _service(db_path, sender).deliver_for_run(
            _run_result(confirmed),
            scan_run_id="invalid-before-entry-confirmed",
        )
    ).sent == 1
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(confirmed.lifecycle_state)

    candles = [_candle(0, high="99", low="96")]
    before_invalidation = _evaluate(
        db_path,
        confirmed.lifecycle_state.lifecycle_id,
        candles,
    )
    invalidated_at = _decision(1).isoformat()
    invalidated_record = before_invalidation.record.model_copy(
        update={
            "current_state": SetupLifecycleState.INVALIDATED,
            "previous_state": SetupLifecycleState.CONFIRMED,
            "last_seen_at": invalidated_at,
            "last_transition_at": invalidated_at,
        }
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(invalidated_record)
        terminal = evaluate_closed_candle_outcomes(
            invalidated_record,
            execution_candles=candles,
            execution_timeframe=TIMEFRAME,
            decision_timestamp=_decision(1),
            evaluated_at=invalidated_at,
            repository=repository,
            scan_run_id="invalid-before-entry-terminal",
        )

    invalidated = _public_setup_symbol(
        signal_id=confirmed.lifecycle_state.lifecycle_id,
        state=SetupLifecycleState.INVALIDATED,
        previous=SetupLifecycleState.CONFIRMED,
    )
    invalidated_transition = invalidated.lifecycle_transition.model_copy(
        update={"record": terminal.record}
    )
    invalidated = invalidated.model_copy(
        update={
            "lifecycle_state": terminal.record,
            "lifecycle_transition": invalidated_transition,
            "lifecycle_outcome_progress": terminal.progress,
        }
    )

    update = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(invalidated),
            scan_run_id="invalid-before-entry-public",
        )
    )

    assert update.sent == 1
    assert any(
        item.alert_type == TelegramAlertType.INVALIDATED.value
        and item.status == "sent"
        for item in update.deliveries
    )
    assert terminal.progress.entry_at is None
    assert terminal.progress.tp1_at is None
    assert terminal.progress.tp2_at is None
    assert terminal.progress.tp3_at is None
    assert _sent_market_outcomes(db_path) == ()
