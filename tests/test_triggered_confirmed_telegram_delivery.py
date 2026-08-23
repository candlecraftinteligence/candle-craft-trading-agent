from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import (
    SQLiteTelegramAlertAttemptRepository,
    TelegramAlertType,
    TelegramEligibilityContext,
    TelegramLifecycleDeliveryService,
    is_public_lifecycle_event,
    telegram_alert_decision_for_symbol,
    _terminal_alert_type_for_lifecycle_state,
)
from app.alerts.telegram_routing import TelegramDestination
from app.alerts.telegram_sender import TelegramSendResult, TelegramSender
from app.analytics.setup_quality import SetupQualityGrade
from app.core.config import Settings
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.pipeline.scanner_runner import ScannerPipelineStatus

from test_telegram_lifecycle_delivery_phase42 import (
    FakeSender,
    _diagnostics,
    _run_result,
    _setup_quality_with_grade,
    _store_lifecycle_record,
    _symbol,
    _trade_idea,
    _with_lifecycle_fields,
    run,
)

from test_lifecycle_outcomes import (
    _entry as _outcome_entry,
    _evaluate as _evaluate_outcomes,
    _prime as _prime_outcomes,
)


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        telegram_dry_run=True,
        telegram_signals_enabled=False,
        telegram_public_watchlist_enabled=False,
        local_manual_mode=True,
        order_execution_enabled=False,
    )


def _service(db_path, sender: FakeSender) -> TelegramLifecycleDeliveryService:
    return TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_settings(),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )


class TimeoutThenSentSender(FakeSender):
    async def send_part(
        self,
        text: str,
        *,
        part_number: int,
        total_parts: int,
        **kwargs: object,
    ) -> TelegramSendResult:
        self.status = "failed" if not self.messages else "sent"
        result = await super().send_part(
            text,
            part_number=part_number,
            total_parts=total_parts,
            **kwargs,
        )
        if result.status != "failed":
            return result
        return TelegramSendResult(
            status="failed",
            detail="Telegram request timed out.",
            telegram_results=result.telegram_results,
            error_message="network_timeout",
            delivery_state="RETRYABLE",
        )


def _generated_entry_batch(
    db_path: Path,
    *,
    lifecycle_id: str = "eth-fast-progression",
):
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        record, candles, _ = _prime_outcomes(
            repository,
            lifecycle_id=lifecycle_id,
            mode="swing",
        )
        candles.append(_outcome_entry(1, "long"))
        outcome = _evaluate_outcomes(repository, record, candles)

    symbol = _symbol(
        SetupLifecycleState.MANAGING,
        previous=SetupLifecycleState.EXECUTING,
        signal_id=lifecycle_id,
    )
    return symbol.model_copy(
        update={
            "symbol": "ETHUSDT",
            "lifecycle_state": outcome.record.model_copy(update={"symbol": "ETHUSDT"}),
            "lifecycle_transition": outcome.last_transition.model_copy(update={"symbol": "ETHUSDT"}),
            "lifecycle_transitions": tuple(
                item.model_copy(
                    update={
                        "symbol": "ETHUSDT",
                        "record": item.record.model_copy(update={"symbol": "ETHUSDT"}),
                    }
                )
                for item in outcome.transitions
            ),
        }
    )


@pytest.mark.parametrize(
    "state,previous",
    (
        (SetupLifecycleState.WATCHLISTED, SetupLifecycleState.DISCOVERED),
        (SetupLifecycleState.STALKING, SetupLifecycleState.WATCHLISTED),
    ),
)
def test_watch_and_stalking_persist_internally_without_public_delivery(tmp_path, state, previous) -> None:
    db_path = tmp_path / f"{state.value.lower()}.db"
    symbol = _symbol(state, previous=previous, signal_id=f"internal-{state.value.lower()}")
    assert symbol.lifecycle_state is not None
    _store_lifecycle_record(db_path, symbol.lifecycle_state)
    sender = FakeSender()

    summary = run(_service(db_path, sender).deliver_for_run(_run_result(symbol), scan_run_id="internal-only"))

    assert summary.sent == 0
    assert sender.messages == []
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        persisted = repository.get_record_by_lifecycle_id(symbol.lifecycle_state.lifecycle_id)
    assert persisted is not None
    assert persisted.current_state == state


@pytest.mark.parametrize(
    "state",
    (
        SetupLifecycleState.REJECTED,
        SetupLifecycleState.COOLDOWN,
    ),
)
def test_rejected_and_cooldown_are_not_public(tmp_path, state) -> None:
    previous = SetupLifecycleState.TRIGGERED if state == SetupLifecycleState.COOLDOWN else None
    symbol = _symbol(state, previous=previous, signal_id=f"internal-{state.value.lower()}")
    sender = FakeSender()

    summary = run(_service(tmp_path / f"{state.value}.db", sender).deliver_for_run(_run_result(symbol)))

    assert summary.sent == 0
    assert sender.messages == []
    assert is_public_lifecycle_event(state) is False


def test_triggered_is_public_once_and_semantically_not_confirmed(tmp_path) -> None:
    db_path = tmp_path / "triggered.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    symbol = _symbol(
        SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
        signal_id="triggered-once",
    )

    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="triggered-1"))
    repeated = run(service.deliver_for_run(_run_result(symbol), scan_run_id="triggered-2"))

    assert first.sent == 1
    assert repeated.sent == 0
    assert len(sender.messages) == 1
    assert "TRIGGERED" in sender.messages[0]
    assert "Awaiting Final Confirmation" in sender.messages[0]
    assert "This is not a confirmed signal" in sender.messages[0]
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts(signal_id=first.deliveries[0].signal_id)
    assert [attempt.alert_type for attempt in attempts] == [TelegramAlertType.SETUP_TRIGGERED.value]


def test_confirmed_is_public_once_and_accepts_real_lifecycle_b_plus(tmp_path) -> None:
    db_path = tmp_path / "confirmed.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id="confirmed-once",
        setup_quality=_setup_quality_with_grade(SetupQualityGrade.B_PLUS, quality_score=78),
    )

    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="confirmed-1"))
    repeated = run(service.deliver_for_run(_run_result(symbol), scan_run_id="confirmed-2"))

    assert first.sent == 1
    assert repeated.sent == 0
    assert len(sender.messages) == 1
    assert "CONFIRMED SIGNAL" in sender.messages[0]
    assert "Confirmation criteria satisfied" in sender.messages[0]



def test_triggered_does_not_suppress_later_confirmed(tmp_path) -> None:
    db_path = tmp_path / "triggered-confirmed.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    triggered = _symbol(
        SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
        signal_id="progression",
    )
    confirmed = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id="progression",
    )

    first = run(service.deliver_for_run(_run_result(triggered), scan_run_id="progression-triggered"))
    second = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="progression-confirmed"))
    repeated = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="progression-confirmed-repeat"))

    assert first.sent == 1
    assert second.sent == 1
    assert repeated.sent == 0
    assert len(sender.messages) == 2
    assert "TRIGGERED" in sender.messages[0]
    assert "CONFIRMED SIGNAL" in sender.messages[1]
    assert first.deliveries[0].signal_id == second.deliveries[0].signal_id


def test_restart_does_not_resend_same_setup(tmp_path) -> None:
    db_path = tmp_path / "restart.db"
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id="restart-confirmed",
    )
    first_sender = FakeSender()
    second_sender = FakeSender()

    first = run(_service(db_path, first_sender).deliver_for_run(_run_result(symbol), scan_run_id="before-restart"))
    repeated = run(_service(db_path, second_sender).deliver_for_run(_run_result(symbol), scan_run_id="after-restart"))

    assert first.sent == 1
    assert repeated.sent == 0
    assert len(first_sender.messages) == 1
    assert second_sender.messages == []


def test_new_setup_identity_is_not_suppressed_by_old_same_tuple(tmp_path) -> None:
    db_path = tmp_path / "new-setup.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    first_setup = _symbol(
        SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
        signal_id="broad-lifecycle-id",
    )
    second_setup = _with_lifecycle_fields(
        first_setup,
        setup_identity="BTCUSDT|swing|long|new-plan-geometry",
    )

    first = run(service.deliver_for_run(_run_result(first_setup), scan_run_id="old-setup"))
    second = run(service.deliver_for_run(_run_result(second_setup), scan_run_id="new-setup"))

    assert first.sent == 1
    assert second.sent == 1
    assert len(sender.messages) == 2
    assert first.deliveries[0].signal_id != second.deliveries[0].signal_id


@pytest.mark.parametrize(
    "state,diagnostics,current_price,expected",
    (
        (SetupLifecycleState.TP_HIT, _diagnostics(outcome_status="tp1_hit"), Decimal("110"), TelegramAlertType.TP1_HIT),
        (SetupLifecycleState.SL_HIT, _diagnostics(), Decimal("95"), TelegramAlertType.SL_HIT),
    ),
)
def test_triggered_setup_keeps_tp_and_sl_updates_functional(tmp_path, state, diagnostics, current_price, expected) -> None:
    db_path = tmp_path / f"{expected.value}.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    triggered = _symbol(
        SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
        signal_id=f"follow-up-{expected.value}",
    )
    outcome = _symbol(
        state,
        previous=SetupLifecycleState.MANAGING,
        diagnostics=diagnostics,
        signal_id=f"follow-up-{expected.value}",
    ).model_copy(update={"current_price": current_price})

    initial = run(service.deliver_for_run(_run_result(triggered), scan_run_id="follow-up-initial"))
    update = run(service.deliver_for_run(_run_result(outcome), scan_run_id="follow-up-update"))

    assert initial.sent == 1
    assert update.sent == 1
    assert update.deliveries[0].alert_type == expected.value
    assert len(sender.messages) == 2


def test_confirmation_rr_gate_remains_unchanged() -> None:
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        diagnostics=_diagnostics(rr_to_tp2=Decimal("2.9")),
        trade_idea=_trade_idea(best_rr=Decimal("2.9")),
    )

    decision = telegram_alert_decision_for_symbol(
        symbol,
        eligibility_context=TelegramEligibilityContext(
            min_rr=Decimal("3"),
            min_score_for_idea=Decimal("80"),
        ),
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED
    assert "planned_rr_below_min:2.9<3" in decision.reason


def test_false_confirmed_candidate_remains_blocked(tmp_path) -> None:
    rejection = "Setup rejected by scoring; confirmation requirements were not satisfied."
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id="false-confirmed",
        status=ScannerPipelineStatus.REJECTED_BY_SCORING,
        rejection_reason=rejection,
        rejection_reasons=(rejection,),
        trade_idea=None,
    )
    sender = FakeSender()

    summary = run(
        _service(tmp_path / "false-confirmed.db", sender).deliver_for_run(
            _run_result(symbol),
            scan_run_id="false-confirmed",
        )
    )

    assert summary.sent == 0
    assert sender.messages == []


def test_invalidated_update_requires_and_uses_prior_public_setup(tmp_path) -> None:
    signal_id = "invalidated-follow-up"
    without_prior_sender = FakeSender()
    invalidated = _symbol(
        SetupLifecycleState.INVALIDATED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id=signal_id,
    )
    without_prior = run(
        _service(tmp_path / "no-prior.db", without_prior_sender).deliver_for_run(
            _run_result(invalidated),
            scan_run_id="no-prior",
        )
    )
    assert without_prior.sent == 0
    assert without_prior_sender.messages == []

    db_path = tmp_path / "with-prior.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    triggered = _symbol(
        SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
        signal_id=signal_id,
    )

    initial = run(service.deliver_for_run(_run_result(triggered), scan_run_id="prior"))
    update = run(service.deliver_for_run(_run_result(invalidated), scan_run_id="invalidated"))

    assert initial.sent == 1
    assert update.sent == 1
    assert update.deliveries[0].alert_type == TelegramAlertType.INVALIDATED.value


def test_cooldown_stays_internal_even_after_public_trigger(tmp_path) -> None:
    db_path = tmp_path / "cooldown-after-trigger.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    signal_id = "cooldown-after-trigger"
    triggered = _symbol(
        SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
        signal_id=signal_id,
    )
    cooldown = _symbol(
        SetupLifecycleState.COOLDOWN,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id=signal_id,
    )

    initial = run(service.deliver_for_run(_run_result(triggered), scan_run_id="trigger"))
    internal = run(service.deliver_for_run(_run_result(cooldown), scan_run_id="cooldown"))

    assert initial.sent == 1
    assert internal.sent == 0
    assert len(sender.messages) == 1
    assert _terminal_alert_type_for_lifecycle_state("cooldown") is None
    assert _terminal_alert_type_for_lifecycle_state("cooled_down") is None


def test_telegram_lifecycle_delivery_contains_no_order_execution_calls() -> None:
    source = Path("app/alerts/telegram_lifecycle.py").read_text(encoding="utf-8").lower()

    for forbidden in ("execute_order", "place_order", "create_order"):
        assert forbidden not in source


def test_same_geometry_triggered_and_confirmed_deliver_once_per_generation(tmp_path) -> None:
    db_path = tmp_path / "same-geometry-generations.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    geometry = (
        "BTCUSDT|swing|long|100|102|95|"
        "Invalid if price accepts below 95."
    )

    def generation_symbol(
        state: SetupLifecycleState,
        *,
        previous: SetupLifecycleState,
        generation_id: str,
    ):
        symbol = _symbol(
            state,
            previous=previous,
            signal_id=generation_id,
            setup_quality=_setup_quality_with_grade(
                SetupQualityGrade.B_PLUS,
                quality_score=78,
            ),
        )
        return _with_lifecycle_fields(symbol, setup_identity=geometry)

    generation_a_triggered = generation_symbol(
        SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
        generation_id="generation-a",
    )
    generation_a_confirmed = generation_symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        generation_id="generation-a",
    )
    generation_b_triggered = generation_symbol(
        SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
        generation_id="generation-b",
    )
    generation_b_confirmed = generation_symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        generation_id="generation-b",
    )

    deliveries = []
    for scan_run_id, symbol in (
        ("a-triggered", generation_a_triggered),
        ("a-confirmed", generation_a_confirmed),
        ("b-triggered", generation_b_triggered),
        ("b-confirmed", generation_b_confirmed),
    ):
        first = run(
            service.deliver_for_run(
                _run_result(symbol),
                scan_run_id=scan_run_id,
            )
        )
        repeated = run(
            service.deliver_for_run(
                _run_result(symbol),
                scan_run_id=f"{scan_run_id}-repeat",
            )
        )
        assert first.sent == 1
        assert repeated.sent == 0
        deliveries.append(first.deliveries[0])

    assert len(sender.messages) == 4
    assert "TRIGGERED" in sender.messages[0]
    assert "CONFIRMED SIGNAL" in sender.messages[1]
    assert "TRIGGERED" in sender.messages[2]
    assert "CONFIRMED SIGNAL" in sender.messages[3]
    assert deliveries[0].signal_id == deliveries[1].signal_id
    assert deliveries[2].signal_id == deliveries[3].signal_id
    assert deliveries[0].signal_id != deliveries[2].signal_id


def test_same_scan_triggered_and_confirmed_are_delivered_in_order(tmp_path) -> None:
    db_path = tmp_path / "same-scan.db"
    symbol = _generated_entry_batch(db_path)
    sender = FakeSender()

    summary = run(_service(db_path, sender).deliver_for_run(_run_result(symbol), scan_run_id="eth-scan"))

    assert summary.sent >= 2
    assert "TRIGGERED" in sender.messages[0]
    assert "CONFIRMED SIGNAL" in sender.messages[1]
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
    public_initial = [
        attempt.alert_type
        for attempt in attempts
        if attempt.alert_type in {
            TelegramAlertType.SETUP_TRIGGERED.value,
            TelegramAlertType.SIGNAL_CONFIRMED.value,
        }
    ]
    assert public_initial == [
        TelegramAlertType.SETUP_TRIGGERED.value,
        TelegramAlertType.SIGNAL_CONFIRMED.value,
    ]


def test_full_eth_sequence_preserves_both_public_attempts_before_management(tmp_path) -> None:
    db_path = tmp_path / "eth-full.db"
    symbol = _generated_entry_batch(db_path)
    assert tuple(item.to_state for item in symbol.lifecycle_transitions) == (
        SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.EXECUTING,
        SetupLifecycleState.MANAGING,
    )
    assert "lifecycle_transitions" not in symbol.model_dump(mode="json")
    sender = FakeSender()

    run(_service(db_path, sender).deliver_for_run(_run_result(symbol), scan_run_id="2026-08-22T15:05:55Z"))

    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
        public_events = repository._connection.execute(
            """
            SELECT event_type, status, delivery_state, telegram_message_id
            FROM public_alert_events
            ORDER BY id
            """
        ).fetchall()
        delivery_parts = repository._connection.execute(
            """
            SELECT event_key, delivery_state, telegram_message_id
            FROM public_alert_delivery_parts
            ORDER BY id
            """
        ).fetchall()
    attempted_types = [attempt.attempted_alert_type for attempt in attempts]
    assert attempted_types.count(TelegramAlertType.SETUP_TRIGGERED.value) == 1
    assert attempted_types.count(TelegramAlertType.SIGNAL_CONFIRMED.value) == 1
    assert attempted_types.index(TelegramAlertType.SETUP_TRIGGERED.value) < attempted_types.index(
        TelegramAlertType.SIGNAL_CONFIRMED.value
    )
    assert "TRIGGERED" in sender.messages[0]
    assert "CONFIRMED SIGNAL" in sender.messages[1]
    limit_attempt = next(
        attempt for attempt in attempts if attempt.attempted_alert_type == TelegramAlertType.LIMIT_HIT.value
    )
    assert attempted_types.index(TelegramAlertType.SIGNAL_CONFIRMED.value) < attempted_types.index(
        TelegramAlertType.LIMIT_HIT.value
    )
    assert limit_attempt.telegram_status == "sent"
    assert [(row[0], row[1], row[2], row[3]) for row in public_events] == [
        ("setup_triggered", "SENT", "SENT", "1"),
        ("signal_confirmed", "SENT", "SENT", "2"),
    ]
    assert [(row[1], row[2]) for row in delivery_parts] == [
        ("SENT", "1"),
        ("SENT", "2"),
    ]
    public_initial_attempts = [
        attempt for attempt in attempts if attempt.attempted_alert_type in {
            TelegramAlertType.SETUP_TRIGGERED.value,
            TelegramAlertType.SIGNAL_CONFIRMED.value,
        }
    ]
    assert [attempt.telegram_message_id for attempt in public_initial_attempts] == ["1", "2"]
    assert all(attempt.delivery_state == "SENT" for attempt in public_initial_attempts)


def test_triggered_timeout_does_not_suppress_confirmed_attempt(tmp_path) -> None:
    db_path = tmp_path / "failure-isolation.db"
    symbol = _generated_entry_batch(db_path)
    sender = TimeoutThenSentSender()

    run(_service(db_path, sender).deliver_for_run(_run_result(symbol), scan_run_id="failure-isolation"))

    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
        public_events = repository._connection.execute(
            """
            SELECT event_type, status, delivery_state
            FROM public_alert_events
            ORDER BY id
            """
        ).fetchall()
    triggered = next(item for item in attempts if item.alert_type == TelegramAlertType.SETUP_TRIGGERED.value)
    confirmed = next(item for item in attempts if item.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value)
    assert triggered.telegram_status == "failed"
    assert triggered.error_message == "network_timeout"
    assert confirmed.telegram_status == "sent"
    assert [(row[0], row[1], row[2]) for row in public_events] == [
        ("setup_triggered", "FAILED", "FAILED_FINAL"),
        ("signal_confirmed", "SENT", "SENT"),
    ]
    assert "CONFIRMED SIGNAL" in sender.messages[1]


def test_restart_after_full_batch_does_not_resend_successful_events(tmp_path) -> None:
    db_path = tmp_path / "batch-restart.db"
    symbol = _generated_entry_batch(db_path)
    first_sender = FakeSender()
    second_sender = FakeSender()

    first = run(_service(db_path, first_sender).deliver_for_run(_run_result(symbol), scan_run_id="before"))
    repeated = run(_service(db_path, second_sender).deliver_for_run(_run_result(symbol), scan_run_id="after"))

    assert first.sent >= 2
    assert repeated.sent == 0
    assert second_sender.messages == []
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
    assert sum(item.alert_type == TelegramAlertType.SETUP_TRIGGERED.value for item in attempts) == 1
    assert sum(item.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value for item in attempts) == 1


def test_restart_after_triggered_sends_only_missing_confirmed(tmp_path) -> None:
    db_path = tmp_path / "partial-restart.db"
    symbol = _generated_entry_batch(db_path)
    triggered_transition = next(
        item for item in symbol.lifecycle_transitions if item.to_state == SetupLifecycleState.TRIGGERED
    )
    triggered_only = symbol.model_copy(
        update={
            "lifecycle_state": triggered_transition.record,
            "lifecycle_transition": triggered_transition,
            "lifecycle_transitions": (triggered_transition,),
        }
    )
    first_sender = FakeSender()
    second_sender = FakeSender()

    first = run(_service(db_path, first_sender).deliver_for_run(_run_result(triggered_only), scan_run_id="triggered"))
    resumed = run(_service(db_path, second_sender).deliver_for_run(_run_result(symbol), scan_run_id="resumed"))

    assert first.sent == 1
    assert resumed.sent >= 1
    assert all("TRIGGERED" not in message for message in second_sender.messages)
    assert "CONFIRMED SIGNAL" in second_sender.messages[0]


def test_duplicate_public_transition_in_same_batch_sends_once(tmp_path) -> None:
    db_path = tmp_path / "same-batch-duplicate.db"
    symbol = _generated_entry_batch(db_path)
    triggered = next(item for item in symbol.lifecycle_transitions if item.to_state == SetupLifecycleState.TRIGGERED)
    duplicate_batch = symbol.model_copy(
        update={
            "lifecycle_state": triggered.record,
            "lifecycle_transition": triggered,
            "lifecycle_transitions": (triggered, triggered),
        }
    )
    sender = FakeSender()

    summary = run(_service(db_path, sender).deliver_for_run(_run_result(duplicate_batch), scan_run_id="duplicate"))

    assert summary.sent == 1
    assert len(sender.messages) == 1
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
    assert [item.alert_type for item in attempts] == [TelegramAlertType.SETUP_TRIGGERED.value]


def test_per_symbol_batch_path_delivers_triggered_then_confirmed(tmp_path) -> None:
    db_path = tmp_path / "per-symbol.db"
    symbol = _generated_entry_batch(db_path)
    sender = FakeSender()
    service = _service(db_path, sender)

    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        deliveries = run(
            service.deliver_transitions_for_symbol(
                symbol,
                repository=repository,
                scan_run_id="per-symbol",
                eligibility_context=TelegramEligibilityContext(
                    min_rr=Decimal("3"),
                    min_score_for_idea=Decimal("80"),
                ),
            )
        )

    public_types = [
        item.alert_type
        for item in deliveries
        if item.alert_type in {
            TelegramAlertType.SETUP_TRIGGERED.value,
            TelegramAlertType.SIGNAL_CONFIRMED.value,
        }
    ]
    assert public_types == [
        TelegramAlertType.SETUP_TRIGGERED.value,
        TelegramAlertType.SIGNAL_CONFIRMED.value,
    ]
    assert "TRIGGERED" in sender.messages[0]
    assert "CONFIRMED SIGNAL" in sender.messages[1]


def test_executing_or_managing_alone_does_not_synthesize_confirmed(tmp_path) -> None:
    for state, previous in (
        (SetupLifecycleState.EXECUTING, SetupLifecycleState.CONFIRMED),
        (SetupLifecycleState.MANAGING, SetupLifecycleState.EXECUTING),
    ):
        db_path = tmp_path / f"{state.value.lower()}.db"
        sender = FakeSender()
        symbol = _symbol(state, previous=previous, signal_id=f"only-{state.value}")

        run(_service(db_path, sender).deliver_for_run(_run_result(symbol), scan_run_id=state.value))

        with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
            attempts = repository.list_attempts()
        assert not any(item.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value for item in attempts)
        assert not any("CONFIRMED SIGNAL" in message for message in sender.messages)


def test_missing_credentials_block_each_public_transition_safely(tmp_path) -> None:
    db_path = tmp_path / "missing-credentials.db"
    symbol = _generated_entry_batch(db_path)
    sender = TelegramSender(
        bot_token=None,
        chat_id=None,
        signals_enabled=True,
        dry_run=False,
        local_manual_mode=True,
        destination=TelegramDestination.PUBLIC_CHAT,
    )

    run(_service(db_path, sender).deliver_for_run(_run_result(symbol), scan_run_id="missing-creds"))

    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
    initial_attempts = [
        item for item in attempts if item.alert_type in {
            TelegramAlertType.SETUP_TRIGGERED.value,
            TelegramAlertType.SIGNAL_CONFIRMED.value,
        }
    ]
    assert len(initial_attempts) == 2
    assert all(item.telegram_status == "skipped" for item in initial_attempts)
    assert all(item.error_message == "missing_telegram_credentials" for item in initial_attempts)


def test_scanner_delivery_has_no_polling_listener_dependency(tmp_path) -> None:
    run_scan_source = Path("scripts/run_scan.py").read_text(encoding="utf-8")
    lifecycle_source = Path("app/alerts/telegram_lifecycle.py").read_text(encoding="utf-8")
    assert "run_telegram_bot" not in run_scan_source
    assert "run_telegram_bot" not in lifecycle_source

    db_path = tmp_path / "listener-independent.db"
    symbol = _generated_entry_batch(db_path)
    sender = FakeSender()
    summary = run(_service(db_path, sender).deliver_for_run(_run_result(symbol), scan_run_id="no-listener"))

    assert summary.sent >= 2
    assert "TRIGGERED" in sender.messages[0]
    assert "CONFIRMED SIGNAL" in sender.messages[1]


def test_concurrent_polling_listener_does_not_change_scanner_sender_semantics(tmp_path) -> None:
    db_path = tmp_path / "listener-active.db"
    symbol = _generated_entry_batch(db_path)
    sender = FakeSender()
    listener_running = asyncio.Event()
    listener_stop = asyncio.Event()

    async def polling_listener_process() -> None:
        listener_running.set()
        await listener_stop.wait()

    async def scenario():
        listener_task = asyncio.create_task(polling_listener_process())
        await listener_running.wait()
        try:
            return await _service(db_path, sender).deliver_for_run(
                _run_result(symbol),
                scan_run_id="listener-active",
            )
        finally:
            listener_stop.set()
            await listener_task

    summary = run(scenario())
    assert summary.sent >= 2
    assert "TRIGGERED" in sender.messages[0]
    assert "CONFIRMED SIGNAL" in sender.messages[1]
