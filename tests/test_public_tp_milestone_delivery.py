from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import (
    SQLiteTelegramAlertAttemptRepository,
    public_outcome_tracking_diagnostics,
)
from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import TelegramAlertType
from app.lifecycle.models import SetupLifecycleOutcomeProgress, SetupLifecycleState
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.storage.models import TelegramAlertAttemptRecord

from test_triggered_confirmed_telegram_delivery import (
    FakeSender,
    TransportStateSequenceSender,
    _empty_run_result,
    _make_lifecycle_retry_due,
    _public_setup_symbol,
    _run_result,
    _service,
    run,
)


ENTRY_AT = "2026-08-30T10:05:00+00:00"
TP1_AT = "2026-08-30T10:10:00+00:00"
TP2_AT = "2026-08-30T10:15:00+00:00"
TP3_AT = "2026-08-30T10:20:00+00:00"
STOP_AT = "2026-08-30T10:25:00+00:00"


def _send_confirmed_root(db_path: Path, sender, *, symbol=None):
    confirmed = symbol or _public_setup_symbol(
        signal_id="public-confirmed-root",
        state=SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
    )
    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(confirmed),
            scan_run_id="confirmed-root",
        )
    )
    assert summary.sent == 1
    return confirmed


def _persist_progress(
    db_path: Path,
    symbol,
    *,
    tp_count: int = 0,
    stop: bool = False,
    integrity_status: str = "Verified",
    diagnostic: str = NA,
    invalidated_at: str | None = None,
    tp1_at: str | None = None,
    limit_hit_already_sent: bool = True,
):
    if stop:
        state = SetupLifecycleState.SL_HIT
        previous = SetupLifecycleState.MANAGING
        terminal = SetupLifecycleState.SL_HIT.value
        outcome_at = STOP_AT
    elif tp_count >= 3:
        state = SetupLifecycleState.TP_HIT
        previous = SetupLifecycleState.MANAGING
        terminal = SetupLifecycleState.TP_HIT.value
        outcome_at = TP3_AT
    elif invalidated_at is not None:
        state = SetupLifecycleState.INVALIDATED
        previous = SetupLifecycleState.MANAGING
        terminal = SetupLifecycleState.INVALIDATED.value
        outcome_at = invalidated_at
    else:
        state = SetupLifecycleState.MANAGING
        previous = SetupLifecycleState.EXECUTING
        terminal = NA
        outcome_at = None
    record = symbol.lifecycle_state.model_copy(
        update={
            "current_state": state,
            "previous_state": previous,
            "last_seen_at": STOP_AT,
            "last_transition_at": outcome_at or ENTRY_AT,
        }
    )
    progress = SetupLifecycleOutcomeProgress(
        lifecycle_id=record.lifecycle_id,
        plan_identity=canonical_plan_identity(record),
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        execution_timeframe="5m",
        tracking_start_at="2026-08-30T10:00:00+00:00",
        evaluation_cursor_open_at="2026-08-30T10:20:00+00:00",
        evaluation_cursor_close_at="2026-08-30T10:25:00+00:00",
        entry_at=ENTRY_AT,
        tp1_at=tp1_at if tp1_at is not None else TP1_AT if tp_count >= 1 else None,
        tp2_at=TP2_AT if tp_count >= 2 else None,
        tp3_at=TP3_AT if tp_count >= 3 else None,
        stop_at=STOP_AT if stop else None,
        invalidated_at=invalidated_at,
        outcome_at=outcome_at,
        terminal_outcome=terminal,
        integrity_status=integrity_status,
        diagnostic=diagnostic,
        metadata_json='{"source":"canonical_lifecycle_closed_execution_candles"}',
        first_evaluated_at=ENTRY_AT,
        last_evaluated_at=STOP_AT,
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        existing = repository.get_record(
            symbol=record.symbol,
            mode=record.mode,
            direction=record.direction,
        )
        if existing is not None and existing.lifecycle_id != record.lifecycle_id:
            repository.supersede_record(existing.lifecycle_id)
        repository.upsert_record(record)
        repository.upsert_outcome_progress(progress)
    if limit_hit_already_sent:
        _insert_sent_limit_hit(db_path, lifecycle_id=record.lifecycle_id)
    return record, progress


def _sent_outcomes(db_path: Path):
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        return tuple(
            item
            for item in repository.list_attempts()
            if item.telegram_status == "sent"
            and item.alert_type in {
                TelegramAlertType.TP1_HIT.value,
                TelegramAlertType.TP2_HIT.value,
                TelegramAlertType.TP3_HIT.value,
                TelegramAlertType.SL_HIT.value,
            }
        )


def _insert_sent_limit_hit(
    db_path: Path,
    *,
    lifecycle_id: str | None = None,
) -> bool:
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        roots = list(repository.list_publicly_tracked_signals())
        if lifecycle_id is not None:
            roots = [
                item
                for item in roots
                if item.signal_id == lifecycle_id
                or item.signal_id.rpartition("-SETUP-")[0] == lifecycle_id
            ]
        if not roots:
            return False
        confirmed = roots[0]
        if repository.has_attempt(
            signal_id=confirmed.signal_id,
            alert_type=TelegramAlertType.LIMIT_HIT,
        ):
            return True
        now = "2026-08-30T10:06:00+00:00"
        assert repository.insert_attempt(
            replace(
                confirmed,
                id=None,
                alert_type=TelegramAlertType.LIMIT_HIT.value,
                attempted_alert_type=TelegramAlertType.LIMIT_HIT.value,
                previous_state=SetupLifecycleState.EXECUTING.value,
                new_state=SetupLifecycleState.MANAGING.value,
                lifecycle_state=SetupLifecycleState.MANAGING.value,
                attempted_at=now,
                sent_at=now,
                telegram_status="sent",
                delivery_state=NA,
                message_hash="limit-hit",
                telegram_message_id=None,
                telegram_chat_id=None,
                public_watchlist_plan_id=NA,
                public_watchlist_event_key=NA,
                public_alert_event_type=NA,
            )
        )
        return True


def _insert_legacy_watchlist_root(db_path: Path, symbol) -> None:
    record = symbol.lifecycle_state
    sent_at = "2026-08-30T10:00:00+00:00"
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        assert repository.insert_attempt(
            TelegramAlertAttemptRecord(
                signal_id=record.lifecycle_id,
                symbol=record.symbol,
                direction=record.direction,
                previous_state=SetupLifecycleState.DISCOVERED.value,
                new_state=SetupLifecycleState.WATCHLISTED.value,
                alert_type=TelegramAlertType.WATCHLIST.value,
                lifecycle_state=SetupLifecycleState.WATCHLISTED.value,
                sent_at=sent_at,
                attempted_at=sent_at,
                telegram_status="sent",
                message_hash="legacy-watchlist",
                attempted_alert_type=TelegramAlertType.WATCHLIST.value,
                entry_low=record.entry_low,
                entry_high=record.entry_high,
                stop_loss=record.stop_loss,
                tp1=record.tp1,
                tp2=record.tp2,
                tp3=record.tp3,
                rr_planned=record.rr,
                delivery_state=NA,
            )
        )


def test_production_like_confirmed_without_watchlist_delivers_tp1_once(tmp_path) -> None:
    db_path = tmp_path / "production-like.db"
    sender = FakeSender()
    triggered = _public_setup_symbol(
        signal_id="production-like",
        state=SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
    )
    confirmed = _public_setup_symbol(
        signal_id="production-like",
        state=SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
    )
    assert run(_service(db_path, sender).deliver_for_run(_run_result(triggered))).sent == 0
    assert run(_service(db_path, sender).deliver_for_run(_run_result(confirmed))).sent == 1
    _insert_sent_limit_hit(db_path)
    _persist_progress(db_path, confirmed, tp_count=1)
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        assert repository.list_sent_watchlist_alerts() == ()
        assert len(repository.list_publicly_tracked_signals()) == 1

    first = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    repeated = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert first.sent == 1
    assert repeated.sent == 0
    assert [item.alert_type for item in _sent_outcomes(db_path)] == [
        TelegramAlertType.TP1_HIT.value
    ]
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        assert repository._connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts WHERE alert_type = 'WATCHLIST'"
        ).fetchone()[0] == 0
        assert repository._connection.execute(
            "SELECT COUNT(*) FROM public_alert_events WHERE event_type = 'tp1_hit'"
        ).fetchone()[0] == 1


def test_confirmed_without_touched_target_creates_no_tp(tmp_path) -> None:
    db_path = tmp_path / "not-touched.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(db_path, confirmed, tp_count=0)

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 0
    assert _sent_outcomes(db_path) == ()


def test_canonical_entry_progress_delivers_limit_once_without_current_price_gate(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "canonical-limit.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    record, progress = _persist_progress(
        db_path,
        confirmed,
        tp_count=0,
        limit_hit_already_sent=False,
    )
    away_from_zone = confirmed.model_copy(
        update={
            "current_price": "150",
            "lifecycle_state": record,
            "lifecycle_transition": None,
            "lifecycle_transitions": (),
            "lifecycle_outcome_progress": progress,
        }
    )

    first = run(_service(db_path, sender).deliver_for_run(_run_result(away_from_zone)))
    restarted = run(
        _service(db_path, FakeSender()).deliver_for_run(_empty_run_result())
    )

    assert first.sent == 1
    assert restarted.sent == 0
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
        assert sum(
            item.alert_type == TelegramAlertType.LIMIT_HIT.value
            and item.telegram_status == "sent"
            for item in attempts
        ) == 1
        assert sum(
            item.alert_type == TelegramAlertType.WATCHLIST.value
            for item in attempts
        ) == 0


@pytest.mark.parametrize("tp_count", (2, 3))
def test_multiple_canonical_targets_are_consolidated_to_highest_once(tmp_path, tp_count) -> None:
    db_path = tmp_path / f"multi-{tp_count}.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(db_path, confirmed, tp_count=tp_count)

    first = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    repeated = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    expected = [
        TelegramAlertType.TP2_HIT.value
        if tp_count == 2
        else TelegramAlertType.TP3_HIT.value
    ]
    assert first.sent == 1
    assert repeated.sent == 0
    assert [item.alert_type for item in _sent_outcomes(db_path)] == expected
    assert sum("TP1 SECURED" in message for message in sender.messages) == 0
    assert sum("TP2 SECURED" in message for message in sender.messages) == (tp_count == 2)
    assert sum("FULL TARGET" in message for message in sender.messages) == (tp_count == 3)


def test_restart_after_sent_and_before_send_are_both_safe(tmp_path) -> None:
    db_path = tmp_path / "restart.db"
    first_sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, first_sender)
    _persist_progress(db_path, confirmed, tp_count=1)

    delivery_sender = FakeSender()
    delivered = run(_service(db_path, delivery_sender).deliver_for_run(_empty_run_result()))
    restarted = run(_service(db_path, FakeSender()).deliver_for_run(_empty_run_result()))

    assert delivered.sent == 1
    assert restarted.sent == 0
    assert len(delivery_sender.messages) == 1


@pytest.mark.parametrize(
    ("tp_count", "expected_alert_type"),
    (
        (1, TelegramAlertType.TP1_HIT),
        (2, TelegramAlertType.TP2_HIT),
    ),
)
def test_retryable_tp_attempt_retries_and_sends_exactly_once(
    tmp_path,
    tp_count,
    expected_alert_type,
) -> None:
    db_path = tmp_path / f"retryable-{tp_count}.db"
    sender = TransportStateSequenceSender("SENT", "RETRYABLE", "SENT")
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(db_path, confirmed, tp_count=tp_count)

    failed = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    _make_lifecycle_retry_due(db_path)
    retried = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    repeated = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert failed.failed == 1
    assert retried.sent == 1
    assert repeated.sent == 0
    assert [item.alert_type for item in _sent_outcomes(db_path)] == [
        expected_alert_type.value
    ]


def test_blocked_tp_row_does_not_consume_canonical_milestone(tmp_path) -> None:
    db_path = tmp_path / "blocked.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(db_path, confirmed, tp_count=1)
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        root = next(
            item
            for item in repository.list_attempts()
            if item.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value
        )
        assert repository.insert_attempt(
            TelegramAlertAttemptRecord(
                signal_id=root.signal_id,
                symbol=root.symbol,
                direction=root.direction,
                previous_state=SetupLifecycleState.EXECUTING.value,
                new_state=SetupLifecycleState.MANAGING.value,
                alert_type=TelegramAlertType.TP1_HIT.value,
                lifecycle_state=SetupLifecycleState.MANAGING.value,
                sent_at=None,
                attempted_at=TP1_AT,
                telegram_status="blocked",
                message_hash="blocked-tp1",
                attempted_alert_type=TelegramAlertType.TP1_HIT.value,
                delivery_state=NA,
                blocked_reason="test_blocked_first_attempt",
            )
        )

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 1
    assert [item.alert_type for item in _sent_outcomes(db_path)] == [
        TelegramAlertType.TP1_HIT.value
    ]


def test_uncertain_tp_is_not_automatically_resent(tmp_path) -> None:
    db_path = tmp_path / "uncertain.db"
    sender = TransportStateSequenceSender("SENT", "UNCERTAIN")
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(db_path, confirmed, tp_count=1)

    uncertain = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    repeated = run(_service(db_path, FakeSender()).deliver_for_run(_empty_run_result()))

    assert uncertain.sent == 0
    assert repeated.sent == 0
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempt = next(
            (
                item
                for item in repository.list_attempts()
                if item.alert_type == TelegramAlertType.TP1_HIT.value
            ),
            None,
        )
        assert attempt is not None
        assert attempt.delivery_state == "UNCERTAIN"


def test_invalidated_before_target_is_audited_and_not_published(tmp_path) -> None:
    db_path = tmp_path / "invalid-before-target.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(
        db_path,
        confirmed,
        tp_count=1,
        invalidated_at="2026-08-30T10:08:00+00:00",
    )

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 0
    assert _sent_outcomes(db_path) == ()
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        audit = next(
            item
            for item in repository.list_attempts()
            if item.attempted_alert_type == "PUBLIC_OUTCOME_TRACKING"
        )
        assert audit.blocked_reason == "canonical_outcome_tp1_hit_after_terminal_cutoff"


def test_tp1_before_later_invalidation_remains_publishable(tmp_path) -> None:
    db_path = tmp_path / "tp-before-invalidation.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(
        db_path,
        confirmed,
        tp_count=1,
        invalidated_at="2026-08-30T10:12:00+00:00",
    )

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 1
    assert [item.alert_type for item in _sent_outcomes(db_path)] == [
        TelegramAlertType.TP1_HIT.value
    ]


def test_canonical_sl_is_delivered_once(tmp_path) -> None:
    db_path = tmp_path / "sl.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(db_path, confirmed, stop=True)

    first = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    repeated = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert first.sent == 1
    assert repeated.sent == 0
    assert [item.alert_type for item in _sent_outcomes(db_path)] == [
        TelegramAlertType.SL_HIT.value
    ]


def test_unverified_progress_is_audited_without_fabricated_tp(tmp_path) -> None:
    db_path = tmp_path / "gap.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(
        db_path,
        confirmed,
        tp_count=1,
        integrity_status="Unverified",
        diagnostic="missing_execution_candle_history:expected_open=...",
    )

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 0
    assert _sent_outcomes(db_path) == ()
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        audit = next(
            item
            for item in repository.list_attempts()
            if item.attempted_alert_type == "PUBLIC_OUTCOME_TRACKING"
        )
        assert "missing_execution_candle_history" in audit.blocked_reason


def test_legacy_sent_watchlist_remains_canonical_outcome_trackable(tmp_path) -> None:
    db_path = tmp_path / "legacy-watchlist.db"
    symbol = _public_setup_symbol(signal_id="legacy-watchlist")
    _insert_legacy_watchlist_root(db_path, symbol)
    _persist_progress(db_path, symbol, tp_count=1)
    sender = FakeSender()

    first = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    repeated = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert first.sent == 1
    assert repeated.sent == 0
    assert [item.alert_type for item in _sent_outcomes(db_path)] == [
        TelegramAlertType.TP1_HIT.value
    ]


def test_pr96_equivalent_mode_projection_has_one_tp1(tmp_path) -> None:
    db_path = tmp_path / "mode-neutral.db"
    sender = FakeSender()
    confirmed = _public_setup_symbol(
        signal_id="scalp-confirmed-generation",
        state=SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        mode="scalp",
        source_modes=("scalp", "swing"),
    )
    swing = _public_setup_symbol(
        signal_id="swing-outcome-generation",
        state=SetupLifecycleState.MANAGING,
        previous=SetupLifecycleState.EXECUTING,
        mode="swing",
        source_modes=("swing",),
    )
    _send_confirmed_root(db_path, sender, symbol=confirmed)
    # Retain a real exact-generation progress row so the stale SCALP lifecycle
    # cannot shadow the later equivalent SWING projection.
    _persist_progress(db_path, confirmed, tp_count=0)
    record, _ = _persist_progress(db_path, swing, tp_count=1)
    current = swing.model_copy(
        update={"lifecycle_state": record, "lifecycle_transition": None}
    )

    first = run(_service(db_path, sender).deliver_for_run(_run_result(current)))
    repeated = run(_service(db_path, sender).deliver_for_run(_run_result(current)))

    assert first.sent == 1
    assert repeated.sent == 0
    assert len(_sent_outcomes(db_path)) == 1
    with (
        SQLiteTelegramAlertAttemptRepository(db_path) as alert_repository,
        SQLiteSetupLifecycleRepository(db_path) as lifecycle_repository,
    ):
        diagnostics = public_outcome_tracking_diagnostics(
            alert_repository=alert_repository,
            lifecycle_repository=lifecycle_repository,
            current_results=(current,),
        )
        audits = tuple(
            item
            for item in alert_repository.list_attempts()
            if item.attempted_alert_type == "PUBLIC_OUTCOME_TRACKING"
        )
    assert len(diagnostics) == 1
    assert diagnostics[0].match_status == "MATCHED"
    assert diagnostics[0].matched_lifecycle_id == record.lifecycle_id
    assert diagnostics[0].public_economic_setup_id != NA
    assert diagnostics[0].tracking_start_at == ENTRY_AT.replace("10:05", "10:00")
    assert not any(
        "public_outcome_tracking_no_lifecycle_match" in item.blocked_reason
        for item in audits
    )


@pytest.mark.parametrize(
    ("second_kwargs", "expected_direction"),
    (
        (
            {
                "entry_low": 110,
                "entry_high": 112,
                "stop_loss": 105,
                "structural_anchor": "execution_sweep|15m|new-setup",
            },
            "long",
        ),
        (
            {
                "direction": "short",
                "stop_loss": 108,
                "structural_anchor": "execution_sweep|15m|opposite-setup",
            },
            "short",
        ),
    ),
)
def test_new_or_opposite_economic_setup_gets_independent_tp1(
    tmp_path,
    second_kwargs,
    expected_direction,
) -> None:
    db_path = tmp_path / f"independent-{expected_direction}.db"
    sender = FakeSender()
    first = _public_setup_symbol(
        signal_id="economic-setup-one",
        state=SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
    )
    second = _public_setup_symbol(
        signal_id=f"economic-setup-two-{expected_direction}",
        state=SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        **second_kwargs,
    )
    _send_confirmed_root(db_path, sender, symbol=first)
    _send_confirmed_root(db_path, sender, symbol=second)
    _persist_progress(db_path, first, tp_count=1)
    _persist_progress(db_path, second, tp_count=1)

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 2
    outcomes = _sent_outcomes(db_path)
    assert len(outcomes) == 2
    assert {item.direction for item in outcomes} >= {"long", expected_direction}


def test_tp3_terminal_projection_uses_same_public_event_once(tmp_path) -> None:
    db_path = tmp_path / "tp3-terminal.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    _insert_sent_limit_hit(db_path)
    record, progress = _persist_progress(db_path, confirmed, tp_count=3)
    diagnostics = {
        mode: {**values, "outcome_status": "tp3_hit"}
        for mode, values in confirmed.strategy_diagnostics.items()
    }
    terminal = _public_setup_symbol(
        signal_id=confirmed.lifecycle_state.lifecycle_id,
        state=SetupLifecycleState.TP_HIT,
        previous=SetupLifecycleState.MANAGING,
    ).model_copy(
        update={
            "lifecycle_state": record,
            "strategy_diagnostics": diagnostics,
            "current_price": record.tp3,
            "lifecycle_outcome_progress": progress,
        }
    )

    summary = run(_service(db_path, sender).deliver_for_run(_run_result(terminal)))

    assert summary.sent == 1
    assert [item.alert_type for item in _sent_outcomes(db_path)] == [
        TelegramAlertType.TP3_HIT.value,
    ]
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        assert repository._connection.execute(
            "SELECT COUNT(*) FROM public_alert_events WHERE event_type = 'tp3_hit'"
        ).fetchone()[0] == 1


def test_malformed_stored_targets_are_audited_without_tp(tmp_path) -> None:
    db_path = tmp_path / "malformed-targets.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    malformed_record = confirmed.lifecycle_state.model_copy(update={"tp2": "bad-target"})
    malformed = confirmed.model_copy(update={"lifecycle_state": malformed_record})
    _persist_progress(db_path, malformed, tp_count=1)

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 0
    assert _sent_outcomes(db_path) == ()
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        audit = next(
            item
            for item in repository.list_attempts()
            if item.attempted_alert_type == "PUBLIC_OUTCOME_TRACKING"
        )
        assert "invalid_stored_geometry" in audit.blocked_reason


def test_transport_failure_for_one_signal_does_not_stop_other_signal(tmp_path) -> None:
    db_path = tmp_path / "transport-isolation.db"
    sender = TransportStateSequenceSender("SENT", "SENT", "UNCERTAIN", "SENT")
    first = _public_setup_symbol(
        signal_id="transport-one",
        state=SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        structural_anchor="execution_sweep|15m|transport-one",
    )
    second = _public_setup_symbol(
        signal_id="transport-two",
        state=SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        structural_anchor="execution_sweep|15m|transport-two",
    )
    _send_confirmed_root(db_path, sender, symbol=first)
    _send_confirmed_root(db_path, sender, symbol=second)
    _persist_progress(db_path, first, tp_count=1)
    _persist_progress(db_path, second, tp_count=1)

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 1
    assert len(_sent_outcomes(db_path)) == 1
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        states = {
            item.delivery_state
            for item in repository.list_attempts()
            if item.alert_type == TelegramAlertType.TP1_HIT.value
        }
        assert states == {"SENT", "UNCERTAIN"}
