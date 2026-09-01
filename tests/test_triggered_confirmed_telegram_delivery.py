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
    telegram_signal_message_from_symbol,
    _lifecycle_setup_delivery_signal_id,
    _public_economic_setup_plan,
    _terminal_alert_type_for_lifecycle_state,
)
from app.alerts.telegram_routing import TelegramDestination
from app.alerts.telegram_sender import TelegramSendResult, TelegramSender
from app.analytics.setup_quality import SetupQualityGrade
from app.core.config import Settings
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.pipeline.scanner_runner import ScannerPipelineStatus

from test_telegram_lifecycle_delivery_phase42 import (
    FakeSender,
    _diagnostics,
    _empty_run_result,
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


def _public_setup_symbol(
    *,
    signal_id: str,
    state: SetupLifecycleState = SetupLifecycleState.CONFIRMED,
    previous: SetupLifecycleState = SetupLifecycleState.TRIGGERED,
    mode: str = "swing",
    source_modes: tuple[str, ...] = ("swing",),
    direction: str = "long",
    structural_anchor: str = "execution_sweep|15m|1789987200000",
    entry_low: Decimal = Decimal("100"),
    entry_high: Decimal = Decimal("102"),
    stop_loss: Decimal = Decimal("95"),
    tick_size: Decimal = Decimal("0.01"),
):
    short = direction == "short"
    risk = stop_loss - entry_high if short else entry_low - stop_loss
    tp1 = entry_low - risk if short else entry_high + risk
    tp2 = entry_low - (risk * Decimal("2")) if short else entry_high + (risk * Decimal("2"))
    tp3 = entry_low - (risk * Decimal("3")) if short else entry_high + (risk * Decimal("3"))
    invalidation = (
        f"Invalid if price accepts above {stop_loss}."
        if short
        else f"Invalid if price accepts below {stop_loss}."
    )
    diagnostics = _diagnostics(
        mode=mode,
        bias=direction,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=stop_loss,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        invalidation=invalidation,
        tick_size=tick_size,
        setup_type=f"liquidity_grab_pullback_{mode}",
    )
    symbol = _symbol(
        state,
        previous=previous,
        signal_id=signal_id,
        diagnostics=diagnostics,
        setup_quality=_setup_quality_with_grade(SetupQualityGrade.B_PLUS, quality_score=78)
        if state == SetupLifecycleState.CONFIRMED
        else None,
    )
    record = symbol.lifecycle_state.model_copy(
        update={
            "lifecycle_id": signal_id,
            "mode": mode,
            "direction": direction,
            "structural_anchor": structural_anchor,
            "entry_low": str(entry_low),
            "entry_high": str(entry_high),
            "stop_loss": str(stop_loss),
            "tp1": str(tp1),
            "tp2": str(tp2),
            "tp3": str(tp3),
            "invalidation_reason": invalidation,
            "invalidation_logic": invalidation,
            "setup_identity": (
                f"BTCUSDT|{mode}|{direction}|{entry_low}|{entry_high}|{stop_loss}|{invalidation}"
            ),
        }
    )
    transition = symbol.lifecycle_transition.model_copy(
        update={"lifecycle_id": signal_id, "record": record}
    )
    return symbol.model_copy(
        update={
            "valid_strategy_modes": source_modes,
            "strategy_diagnostics": {
                source_mode: {**diagnostics, "mode": source_mode}
                for source_mode in source_modes
            },
            "lifecycle_state": record,
            "lifecycle_transition": transition,
        }
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


class TransportStateSequenceSender(FakeSender):
    def __init__(self, *states: str) -> None:
        super().__init__()
        self.states = list(states)

    async def send_part(
        self,
        text: str,
        *,
        part_number: int,
        total_parts: int,
        **kwargs: object,
    ) -> TelegramSendResult:
        if not self.states:
            raise AssertionError("Unexpected Telegram transport attempt.")
        state = self.states.pop(0)
        sent = state == "SENT"
        self.messages.append(text)
        self.calls.append(kwargs)
        error_category = None if sent else f"test_{state.lower()}"
        transport = {
            "status": "sent" if sent else "failed",
            "delivery_state": state,
            "part_number": part_number,
            "total_parts": total_parts,
            "message_id": len(self.messages) if sent else None,
            "chat_id": "fake-public-chat" if sent else None,
            "retry_after": 0 if state == "RETRYABLE" else None,
            "error_category": error_category,
            "error": error_category,
        }
        return TelegramSendResult(
            status="sent" if sent else "failed",
            detail="sent" if sent else f"Telegram transport ended in {state}.",
            telegram_results=(transport,),
            error_message=NA if sent else str(error_category),
            delivery_state=state,
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


def _entry_transition_result(symbol, state: SetupLifecycleState):
    transition = next(item for item in symbol.lifecycle_transitions if item.to_state == state)
    return symbol.model_copy(
        update={
            "lifecycle_state": transition.record,
            "lifecycle_transition": transition,
            "lifecycle_transitions": (transition,),
        }
    )


def _triggered_confirmed_result(symbol):
    transitions = tuple(
        item
        for item in symbol.lifecycle_transitions
        if item.to_state in {
            SetupLifecycleState.TRIGGERED,
            SetupLifecycleState.CONFIRMED,
        }
    )
    return symbol.model_copy(
        update={
            "lifecycle_state": transitions[-1].record,
            "lifecycle_transition": transitions[-1],
            "lifecycle_transitions": transitions,
        }
    )


def _make_lifecycle_retry_due(db_path: Path) -> None:
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        repository._connection.execute(
            """
            UPDATE public_alert_events
            SET next_retry_at = '2000-01-01T00:00:00Z'
            WHERE delivery_state = 'RETRYABLE'
            """
        )
        repository._connection.execute(
            """
            UPDATE public_alert_delivery_parts
            SET next_retry_at = '2000-01-01T00:00:00Z'
            WHERE delivery_state = 'RETRYABLE'
            """
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


def test_triggered_is_internal_only_and_audited_once_per_observation(tmp_path) -> None:
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

    assert first.sent == 0
    assert repeated.sent == 0
    assert sender.messages == []
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts(signal_id=first.deliveries[0].signal_id)
    assert len(attempts) == 1
    assert all(attempt.telegram_status == "skipped" for attempt in attempts)
    assert all(attempt.dedupe_reason == "public_triggered_internal_only" for attempt in attempts)


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
    assert "SIGNAL CONFIRMED" in sender.messages[0]
    assert "🐺 Hunt live." in sender.messages[0]



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

    assert first.sent == 0
    assert second.sent == 1
    assert repeated.sent == 0
    assert len(sender.messages) == 1
    assert "SIGNAL CONFIRMED" in sender.messages[0]
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


@pytest.mark.parametrize(
    "first_mode,first_sources,second_mode,second_sources",
    (
        ("scalp", ("scalp", "swing"), "swing", ("swing",)),
        ("swing", ("swing",), "scalp", ("scalp", "swing")),
    ),
)
def test_mode_projection_change_does_not_resend_equivalent_trigger(
    tmp_path,
    first_mode,
    first_sources,
    second_mode,
    second_sources,
) -> None:
    db_path = tmp_path / f"mode-{first_mode}-{second_mode}.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    first_setup = _public_setup_symbol(
        signal_id=f"{first_mode}-generation",
        mode=first_mode,
        source_modes=first_sources,
    )
    second_setup = _public_setup_symbol(
        signal_id=f"{second_mode}-generation",
        mode=second_mode,
        source_modes=second_sources,
    )

    first = run(service.deliver_for_run(_run_result(first_setup), scan_run_id="first-mode"))
    repeated = run(service.deliver_for_run(_run_result(second_setup), scan_run_id="second-mode"))

    assert first.sent == 1
    assert repeated.sent == 0
    assert repeated.deliveries[0].status == "duplicate"
    assert repeated.deliveries[0].error_message == "duplicate_equivalent_public_setup"
    assert len(sender.messages) == 1


def test_mode_specific_lifecycle_ids_collapse_to_one_public_economic_identity() -> None:
    confluence = _public_setup_symbol(
        signal_id="identity-confluence-generation",
        mode="scalp",
        source_modes=("scalp", "swing"),
    )
    swing_only = _public_setup_symbol(
        signal_id="identity-swing-generation",
        mode="swing",
        source_modes=("swing",),
    )

    old_confluence_id = _lifecycle_setup_delivery_signal_id(confluence)
    old_swing_id = _lifecycle_setup_delivery_signal_id(swing_only)
    confluence_plan = _public_economic_setup_plan(
        confluence,
        telegram_signal_message_from_symbol(confluence),
    )
    swing_plan = _public_economic_setup_plan(
        swing_only,
        telegram_signal_message_from_symbol(swing_only),
    )

    assert old_confluence_id != old_swing_id
    assert confluence_plan.plan_id == swing_plan.plan_id
    assert confluence_plan.structural_anchor == swing_plan.structural_anchor
    assert confluence_plan.source_modes != swing_plan.source_modes


def test_tp_update_still_bridges_after_equivalent_mode_trigger_is_suppressed(tmp_path) -> None:
    db_path = tmp_path / "mode-trigger-tp.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    confluence = _public_setup_symbol(
        signal_id="tp-confluence-generation",
        mode="scalp",
        source_modes=("scalp", "swing"),
    )
    swing_only = _public_setup_symbol(
        signal_id="tp-swing-generation",
        mode="swing",
        source_modes=("swing",),
    )
    tp1 = _public_setup_symbol(
        signal_id="tp-swing-generation",
        state=SetupLifecycleState.TP_HIT,
        previous=SetupLifecycleState.MANAGING,
        mode="swing",
        source_modes=("swing",),
    )
    tp1 = tp1.model_copy(
        update={
            "current_price": Decimal("107"),
            "strategy_diagnostics": {
                mode: {**diagnostics, "outcome_status": "tp1_hit"}
                for mode, diagnostics in tp1.strategy_diagnostics.items()
            },
        }
    )

    initial = run(service.deliver_for_run(_run_result(confluence), scan_run_id="initial"))
    duplicate = run(service.deliver_for_run(_run_result(swing_only), scan_run_id="mode-change"))
    update = run(service.deliver_for_run(_run_result(tp1), scan_run_id="tp1"))

    assert initial.sent == 1
    assert duplicate.sent == 0
    assert update.sent == 1
    assert update.deliveries[0].alert_type == TelegramAlertType.TP1_HIT.value
    assert len(sender.messages) == 2


def test_equivalent_mode_projection_remains_deduped_after_repository_reload(tmp_path) -> None:
    db_path = tmp_path / "equivalent-reload.db"
    first_sender = FakeSender()
    second_sender = FakeSender()
    confluence = _public_setup_symbol(
        signal_id="confluence-generation",
        mode="scalp",
        source_modes=("scalp", "swing"),
    )
    swing_only = _public_setup_symbol(
        signal_id="swing-generation",
        mode="swing",
        source_modes=("swing",),
    )

    first = run(_service(db_path, first_sender).deliver_for_run(_run_result(confluence), scan_run_id="before"))
    repeated = run(_service(db_path, second_sender).deliver_for_run(_run_result(swing_only), scan_run_id="after"))

    assert first.sent == 1
    assert repeated.sent == 0
    assert len(first_sender.messages) == 1
    assert second_sender.messages == []


def test_pre_v18_event_recovers_persisted_lifecycle_anchor_before_dedupe(tmp_path) -> None:
    db_path = tmp_path / "legacy-anchor-reload.db"
    first_sender = FakeSender()
    second_sender = FakeSender()
    confluence = _public_setup_symbol(
        signal_id="legacy-confluence-generation",
        mode="scalp",
        source_modes=("scalp", "swing"),
    )
    swing_only = _public_setup_symbol(
        signal_id="current-swing-generation",
        mode="swing",
        source_modes=("swing",),
    )
    _store_lifecycle_record(db_path, confluence.lifecycle_state)

    first = run(_service(db_path, first_sender).deliver_for_run(_run_result(confluence), scan_run_id="legacy-first"))
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        legacy_plan_id = repository._connection.execute(
            "SELECT signal_id FROM telegram_alert_attempts WHERE telegram_status = 'sent'"
        ).fetchone()[0]
        legacy_event_key = f"{legacy_plan_id}|setup_triggered"
        repository._connection.execute(
            """
            UPDATE public_alert_events
            SET canonical_plan_id = ?, event_key = ?, structural_anchor = 'N/A'
            """,
            (legacy_plan_id, legacy_event_key),
        )
        repository._connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET public_watchlist_plan_id = ?, public_watchlist_event_key = ?
            WHERE telegram_status = 'sent'
            """,
            (legacy_plan_id, legacy_event_key),
        )

    repeated = run(_service(db_path, second_sender).deliver_for_run(_run_result(swing_only), scan_run_id="legacy-reload"))

    assert first.sent == 1
    assert repeated.sent == 0
    assert second_sender.messages == []
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        anchor = repository._connection.execute(
            "SELECT structural_anchor FROM public_alert_events WHERE status = 'SENT'"
        ).fetchone()[0]
    assert anchor == "execution_sweep|15m|1789987200000"


def test_tiny_tick_size_stop_movement_is_same_public_setup(tmp_path) -> None:
    db_path = tmp_path / "tick-equivalent.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    common = {
        "mode": "swing",
        "source_modes": ("swing",),
        "structural_anchor": "execution_sweep|15m|jup-sweep",
        "entry_low": Decimal("0.21690"),
        "entry_high": Decimal("0.21731"),
        "tick_size": Decimal("0.00001"),
    }
    first_setup = _public_setup_symbol(
        signal_id="jup-generation-a",
        stop_loss=Decimal("0.21941"),
        direction="short",
        **common,
    )
    second_setup = _public_setup_symbol(
        signal_id="jup-generation-b",
        stop_loss=Decimal("0.21942"),
        direction="short",
        **common,
    )

    first = run(service.deliver_for_run(_run_result(first_setup), scan_run_id="jup-a"))
    repeated = run(service.deliver_for_run(_run_result(second_setup), scan_run_id="jup-b"))

    assert first.sent == 1
    assert repeated.sent == 0
    assert repeated.deliveries[0].error_message == "duplicate_equivalent_public_setup"
    assert len(sender.messages) == 1


def test_equivalent_stop_jitter_across_normalization_bucket_is_deduped(tmp_path) -> None:
    db_path = tmp_path / "tick-bucket-equivalent.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    common = {
        "mode": "swing",
        "source_modes": ("swing",),
        "direction": "short",
        "structural_anchor": "execution_sweep|15m|jup-bucket-sweep",
        "entry_low": Decimal("0.21690"),
        "entry_high": Decimal("0.21731"),
        "tick_size": Decimal("0.00001"),
    }
    first_setup = _public_setup_symbol(signal_id="jup-bucket-a", stop_loss=Decimal("0.21934"), **common)
    second_setup = _public_setup_symbol(signal_id="jup-bucket-b", stop_loss=Decimal("0.21936"), **common)
    first_plan = _public_economic_setup_plan(first_setup, telegram_signal_message_from_symbol(first_setup))
    second_plan = _public_economic_setup_plan(second_setup, telegram_signal_message_from_symbol(second_setup))

    first = run(service.deliver_for_run(_run_result(first_setup), scan_run_id="bucket-a"))
    repeated = run(service.deliver_for_run(_run_result(second_setup), scan_run_id="bucket-b"))

    assert first_plan.plan_id != second_plan.plan_id
    assert first.sent == 1
    assert repeated.sent == 0
    assert repeated.deliveries[0].error_message == "duplicate_equivalent_public_setup"
    assert len(sender.messages) == 1


def test_material_entry_change_with_same_anchor_remains_publishable(tmp_path) -> None:
    db_path = tmp_path / "material-entry.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    first_setup = _public_setup_symbol(signal_id="entry-a")
    second_setup = _public_setup_symbol(
        signal_id="entry-b",
        entry_low=Decimal("110"),
        entry_high=Decimal("112"),
        stop_loss=Decimal("105"),
    )

    first = run(service.deliver_for_run(_run_result(first_setup), scan_run_id="entry-a"))
    second = run(service.deliver_for_run(_run_result(second_setup), scan_run_id="entry-b"))

    assert first.sent == 1
    assert second.sent == 1
    assert len(sender.messages) == 2


def test_new_structural_anchor_remains_publishable_with_same_geometry(tmp_path) -> None:
    db_path = tmp_path / "new-anchor.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    first_setup = _public_setup_symbol(signal_id="anchor-a", structural_anchor="execution_sweep|15m|a")
    second_setup = _public_setup_symbol(signal_id="anchor-b", structural_anchor="execution_sweep|15m|b")

    first = run(service.deliver_for_run(_run_result(first_setup), scan_run_id="anchor-a"))
    second = run(service.deliver_for_run(_run_result(second_setup), scan_run_id="anchor-b"))

    assert first.sent == 1
    assert second.sent == 1
    assert len(sender.messages) == 2


def test_opposite_direction_remains_publishable(tmp_path) -> None:
    db_path = tmp_path / "opposite-direction.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    long_setup = _public_setup_symbol(signal_id="long-generation")
    short_setup = _public_setup_symbol(
        signal_id="short-generation",
        direction="short",
        stop_loss=Decimal("105"),
    )

    first = run(service.deliver_for_run(_run_result(long_setup), scan_run_id="long"))
    second = run(service.deliver_for_run(_run_result(short_setup), scan_run_id="short"))

    assert first.sent == 1
    assert second.sent == 1
    assert len(sender.messages) == 2


def test_equivalent_trigger_does_not_suppress_confirmed_across_mode_change(tmp_path) -> None:
    db_path = tmp_path / "mode-progress.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    triggered = _public_setup_symbol(
        signal_id="scalp-triggered-generation",
        state=SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
        mode="scalp",
        source_modes=("scalp", "swing"),
    )
    confirmed = _public_setup_symbol(
        signal_id="swing-confirmed-generation",
        state=SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        mode="swing",
        source_modes=("swing",),
    )

    first = run(service.deliver_for_run(_run_result(triggered), scan_run_id="triggered"))
    second = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="confirmed"))
    repeated = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="confirmed-repeat"))

    assert first.sent == 0
    assert second.sent == 1
    assert repeated.sent == 0
    assert len(sender.messages) == 1
    assert "SIGNAL CONFIRMED" in sender.messages[0]


def test_retryable_equivalent_mode_projection_retries_prior_committed_intent(tmp_path) -> None:
    db_path = tmp_path / "equivalent-retry.db"
    sender = TransportStateSequenceSender("RETRYABLE", "SENT")
    confluence = _public_setup_symbol(
        signal_id="retry-confluence",
        mode="scalp",
        source_modes=("scalp", "swing"),
    )
    swing_only = _public_setup_symbol(
        signal_id="retry-swing",
        mode="swing",
        source_modes=("swing",),
    )

    first = run(_service(db_path, sender).deliver_for_run(_run_result(confluence), scan_run_id="retry-first"))
    _make_lifecycle_retry_due(db_path)
    retried = run(_service(db_path, sender).deliver_for_run(_run_result(swing_only), scan_run_id="retry-second"))

    assert first.sent == 0
    assert retried.sent == 1
    assert len(sender.messages) == 2
    assert sender.states == []


def test_materially_new_entry_geometry_is_not_suppressed_by_old_same_tuple(tmp_path) -> None:
    db_path = tmp_path / "new-setup.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    first_setup = _symbol(
        SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
        signal_id="broad-lifecycle-id",
    )
    second_setup = _public_setup_symbol(
        signal_id="new-plan-lifecycle-id",
        structural_anchor="N/A",
        entry_low=Decimal("110"),
        entry_high=Decimal("112"),
        stop_loss=Decimal("105"),
    )

    first = run(service.deliver_for_run(_run_result(first_setup), scan_run_id="old-setup"))
    second = run(service.deliver_for_run(_run_result(second_setup), scan_run_id="new-setup"))

    assert first.sent == 0
    assert second.sent == 1
    assert len(sender.messages) == 1
    assert first.deliveries[0].signal_id != second.deliveries[0].signal_id


@pytest.mark.parametrize(
    "state,diagnostics,current_price,expected",
    (
        (SetupLifecycleState.TP_HIT, _diagnostics(outcome_status="tp1_hit"), Decimal("110"), TelegramAlertType.TP1_HIT),
        (SetupLifecycleState.SL_HIT, _diagnostics(), Decimal("95"), TelegramAlertType.SL_HIT),
    ),
)
def test_confirmed_setup_keeps_tp_and_sl_updates_functional(tmp_path, state, diagnostics, current_price, expected) -> None:
    db_path = tmp_path / f"{expected.value}.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    confirmed = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id=f"follow-up-{expected.value}",
    )
    outcome = _symbol(
        state,
        previous=SetupLifecycleState.MANAGING,
        diagnostics=diagnostics,
        signal_id=f"follow-up-{expected.value}",
    ).model_copy(update={"current_price": current_price})

    initial = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="follow-up-initial"))
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
    confirmed = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id=signal_id,
    )

    initial = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="prior"))
    update = run(service.deliver_for_run(_run_result(invalidated), scan_run_id="invalidated"))

    assert initial.sent == 1
    assert update.sent == 1
    assert update.deliveries[0].alert_type == TelegramAlertType.INVALIDATED.value


def test_cooldown_stays_internal_even_after_public_confirmation(tmp_path) -> None:
    db_path = tmp_path / "cooldown-after-trigger.db"
    sender = FakeSender()
    service = _service(db_path, sender)
    signal_id = "cooldown-after-trigger"
    confirmed = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id=signal_id,
    )
    cooldown = _symbol(
        SetupLifecycleState.COOLDOWN,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id=signal_id,
    )

    initial = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="confirmed"))
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
        return _with_lifecycle_fields(
            symbol,
            setup_identity=geometry,
            structural_anchor=f"execution_sweep|15m|{generation_id}",
        )

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
        assert first.sent == (
            1 if symbol.lifecycle_state.current_state == SetupLifecycleState.CONFIRMED else 0
        )
        assert repeated.sent == 0
        deliveries.append(first.deliveries[0])

    assert len(sender.messages) == 2
    assert "SIGNAL CONFIRMED" in sender.messages[0]
    assert "SIGNAL CONFIRMED" in sender.messages[1]
    assert deliveries[0].signal_id == deliveries[1].signal_id
    assert deliveries[2].signal_id == deliveries[3].signal_id
    assert deliveries[0].signal_id != deliveries[2].signal_id


def test_same_scan_triggered_and_confirmed_are_coalesced(tmp_path) -> None:
    db_path = tmp_path / "same-scan.db"
    symbol = _generated_entry_batch(db_path)
    sender = FakeSender()

    summary = run(_service(db_path, sender).deliver_for_run(_run_result(symbol), scan_run_id="eth-scan"))

    assert summary.sent == 1
    assert len(sender.messages) == 1
    assert "SIGNAL CONFIRMED · 🎯 ZONE ACTIVE" in sender.messages[0]
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
    public_initial = [
        attempt.attempted_alert_type
        for attempt in attempts
        if attempt.attempted_alert_type in {
            TelegramAlertType.SETUP_TRIGGERED.value,
            TelegramAlertType.SIGNAL_CONFIRMED.value,
        }
    ]
    assert public_initial == [
        TelegramAlertType.SETUP_TRIGGERED.value,
        TelegramAlertType.SIGNAL_CONFIRMED.value,
    ]


def test_full_eth_sequence_preserves_suppression_audits_before_management(tmp_path) -> None:
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
    assert len(sender.messages) == 1
    assert "SIGNAL CONFIRMED · 🎯 ZONE ACTIVE" in sender.messages[0]
    limit_attempt = next(
        attempt for attempt in attempts if attempt.attempted_alert_type == TelegramAlertType.LIMIT_HIT.value
    )
    assert attempted_types.index(TelegramAlertType.SIGNAL_CONFIRMED.value) < attempted_types.index(
        TelegramAlertType.LIMIT_HIT.value
    )
    assert limit_attempt.telegram_status == "skipped"
    assert limit_attempt.dedupe_reason == "public_limit_hit_coalesced_into_confirmation"
    assert [(row[0], row[1], row[2], row[3]) for row in public_events] == [
        ("signal_confirmed", "SENT", "SENT", "1"),
    ]
    assert [(row[1], row[2]) for row in delivery_parts] == [
        ("SENT", "1"),
    ]
    public_initial_attempts = [
        attempt for attempt in attempts if attempt.attempted_alert_type in {
            TelegramAlertType.SETUP_TRIGGERED.value,
            TelegramAlertType.SIGNAL_CONFIRMED.value,
        }
    ]
    assert [attempt.telegram_message_id for attempt in public_initial_attempts] == [None, "1"]
    assert [attempt.telegram_status for attempt in public_initial_attempts] == ["skipped", "sent"]


def test_internal_triggered_does_not_consume_confirmed_transport_attempt(tmp_path) -> None:
    db_path = tmp_path / "failure-isolation.db"
    symbol = _generated_entry_batch(db_path)
    sender = FakeSender()

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
    triggered = next(
        item
        for item in attempts
        if item.attempted_alert_type == TelegramAlertType.SETUP_TRIGGERED.value
    )
    confirmed = next(item for item in attempts if item.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value)
    assert triggered.telegram_status == "skipped"
    assert triggered.dedupe_reason == "public_triggered_coalesced_into_confirmation"
    assert confirmed.telegram_status == "sent"
    assert [(row[0], row[1], row[2]) for row in public_events] == [
        ("signal_confirmed", "SENT", "SENT"),
    ]
    assert len(sender.messages) == 1
    assert "SIGNAL CONFIRMED" in sender.messages[0]


@pytest.mark.parametrize(
    ("state", "alert_type"),
    (
        (SetupLifecycleState.CONFIRMED, TelegramAlertType.SIGNAL_CONFIRMED),
    ),
)
def test_retryable_public_lifecycle_event_recovers_once_and_reuses_identity(
    tmp_path,
    state,
    alert_type,
) -> None:
    db_path = tmp_path / f"{alert_type.value.lower()}-retry.db"
    symbol = _entry_transition_result(_generated_entry_batch(db_path), state)
    sender = TransportStateSequenceSender("RETRYABLE", "SENT")
    service = _service(db_path, sender)

    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="initial"))
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        first_attempt = next(
            item
            for item in repository.list_attempts()
            if item.alert_type == alert_type.value
        )
        first_event = repository._connection.execute(
            """
            SELECT id, status, delivery_state, attempt_count
            FROM public_alert_events
            WHERE event_type = ?
            """,
            (alert_type.value.lower(),),
        ).fetchone()
    assert first.failed == 1
    assert first_attempt is not None
    assert first_attempt.telegram_status == "retryable"
    assert first_attempt.delivery_state == "RETRYABLE"
    assert tuple(first_event) == (first_event[0], "RESERVED", "RETRYABLE", 1)

    _make_lifecycle_retry_due(db_path)
    recovered = run(service.deliver_for_run(_empty_run_result(), scan_run_id="recovery"))
    repeated = run(service.deliver_for_run(_empty_run_result(), scan_run_id="after-sent"))

    assert recovered.sent == 1
    assert repeated.sent == 0
    assert len(sender.messages) == 2
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        final_attempt = next(
            item
            for item in repository.list_attempts()
            if item.alert_type == alert_type.value
        )
        final_event = repository._connection.execute(
            """
            SELECT id, status, delivery_state, attempt_count
            FROM public_alert_events
            WHERE event_type = ?
            """,
            (alert_type.value.lower(),),
        ).fetchone()
        attempt_rows = repository._connection.execute(
            """
            SELECT id, delivery_attempt_count
            FROM telegram_alert_attempts
            WHERE attempted_alert_type = ?
            """,
            (alert_type.value,),
        ).fetchall()
    assert final_attempt is not None
    assert final_attempt.id == first_attempt.id
    assert final_attempt.telegram_status == "sent"
    assert final_attempt.delivery_state == "SENT"
    assert tuple(final_event) == (first_event[0], "SENT", "SENT", 2)
    assert [(row[0], row[1]) for row in attempt_rows] == [(first_attempt.id, 2)]


def test_coalesced_triggered_never_becomes_a_recovery_send(tmp_path) -> None:
    db_path = tmp_path / "triggered-retry-confirmed-sent.db"
    symbol = _triggered_confirmed_result(_generated_entry_batch(db_path))
    sender = FakeSender()
    service = _service(db_path, sender)

    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="batch"))

    assert first.sent == 1
    assert len(sender.messages) == 1
    assert "SIGNAL CONFIRMED" in sender.messages[0]
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        initial_states = repository._connection.execute(
            """
            SELECT event_type, delivery_state
            FROM public_alert_events
            ORDER BY id
            """
        ).fetchall()
    assert [(row[0], row[1]) for row in initial_states] == [("signal_confirmed", "SENT")]

    recovered = run(service.deliver_for_run(_empty_run_result(), scan_run_id="recovery"))
    repeated = run(service.deliver_for_run(_empty_run_result(), scan_run_id="deduped"))

    assert recovered.sent == 0
    assert repeated.sent == 0
    assert len(sender.messages) == 1
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        final_states = repository._connection.execute(
            """
            SELECT event_type, delivery_state
            FROM public_alert_events
            ORDER BY id
            """
        ).fetchall()
    assert [(row[0], row[1]) for row in final_states] == [("signal_confirmed", "SENT")]


def test_failed_final_public_lifecycle_event_is_terminal(tmp_path) -> None:
    db_path = tmp_path / "failed-final.db"
    symbol = _entry_transition_result(
        _generated_entry_batch(db_path),
        SetupLifecycleState.CONFIRMED,
    )
    sender = TransportStateSequenceSender("FAILED_FINAL", "SENT")
    service = _service(db_path, sender)

    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="terminal"))
    recovered = run(service.deliver_for_run(_empty_run_result(), scan_run_id="no-retry"))

    assert first.failed == 1
    assert recovered.sent == 0
    assert len(sender.messages) == 1
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        event = repository._connection.execute(
            "SELECT status, delivery_state, attempt_count FROM public_alert_events"
        ).fetchone()
        attempt = repository.list_attempts()[0]
    assert tuple(event) == ("FAILED", "FAILED_FINAL", 1)
    assert attempt.telegram_status == "failed_final"
    assert attempt.delivery_state == "FAILED_FINAL"


def test_uncertain_public_lifecycle_event_never_auto_resends(tmp_path) -> None:
    db_path = tmp_path / "uncertain.db"
    symbol = _entry_transition_result(
        _generated_entry_batch(db_path),
        SetupLifecycleState.CONFIRMED,
    )
    sender = TransportStateSequenceSender("UNCERTAIN", "SENT")
    service = _service(db_path, sender)

    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="uncertain"))
    recovered = run(service.deliver_for_run(_empty_run_result(), scan_run_id="no-resend"))

    assert first.skipped == 1
    assert recovered.sent == 0
    assert len(sender.messages) == 1
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        event = repository._connection.execute(
            "SELECT status, delivery_state, attempt_count FROM public_alert_events"
        ).fetchone()
        attempt = repository.list_attempts()[0]
    assert tuple(event) == ("FAILED", "UNCERTAIN", 1)
    assert attempt.telegram_status == "uncertain"
    assert attempt.delivery_state == "UNCERTAIN"


def test_lifecycle_history_without_claimable_outbox_intent_is_not_replayed(tmp_path) -> None:
    db_path = tmp_path / "historical-lifecycle.db"
    _generated_entry_batch(db_path, lifecycle_id="historical-only")
    sender = TransportStateSequenceSender("SENT")

    summary = run(
        _service(db_path, sender).deliver_for_run(
            _empty_run_result(),
            scan_run_id="restart",
        )
    )

    assert summary.sent == 0
    assert sender.messages == []
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        assert len(repository.list_events(lifecycle_id="historical-only")) >= 4
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        assert repository.list_attempts() == ()
        assert repository._connection.execute(
            "SELECT COUNT(*) FROM public_alert_events"
        ).fetchone()[0] == 0


def test_restart_after_full_batch_does_not_resend_successful_events(tmp_path) -> None:
    db_path = tmp_path / "batch-restart.db"
    symbol = _generated_entry_batch(db_path)
    first_sender = FakeSender()
    second_sender = FakeSender()

    first = run(_service(db_path, first_sender).deliver_for_run(_run_result(symbol), scan_run_id="before"))
    repeated = run(_service(db_path, second_sender).deliver_for_run(_run_result(symbol), scan_run_id="after"))

    assert first.sent == 1
    assert repeated.sent == 0
    assert second_sender.messages == []
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
    assert sum(item.attempted_alert_type == TelegramAlertType.SETUP_TRIGGERED.value for item in attempts) == 1
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

    assert first.sent == 0
    assert resumed.sent == 1
    assert all("HUNT ACTIVE" not in message for message in second_sender.messages)
    assert "SIGNAL CONFIRMED" in second_sender.messages[0]


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

    assert summary.sent == 0
    assert sender.messages == []
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts()
    assert len(attempts) == 1
    assert all(item.telegram_status == "skipped" for item in attempts)
    assert all(item.dedupe_reason == "public_triggered_internal_only" for item in attempts)


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
    assert len(sender.messages) == 1
    assert "SIGNAL CONFIRMED · 🎯 ZONE ACTIVE" in sender.messages[0]


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
        assert not any("SIGNAL CONFIRMED" in message for message in sender.messages)


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
        item for item in attempts if item.attempted_alert_type in {
            TelegramAlertType.SETUP_TRIGGERED.value,
            TelegramAlertType.SIGNAL_CONFIRMED.value,
        }
    ]
    assert len(initial_attempts) == 2
    triggered, confirmed = initial_attempts
    assert triggered.telegram_status == "skipped"
    assert triggered.dedupe_reason == "public_triggered_coalesced_into_confirmation"
    assert confirmed.telegram_status == "failed_final"
    assert confirmed.delivery_state == "FAILED_FINAL"
    assert confirmed.error_message == "missing_telegram_credentials"


def test_scanner_delivery_has_no_polling_listener_dependency(tmp_path) -> None:
    run_scan_source = Path("scripts/run_scan.py").read_text(encoding="utf-8")
    lifecycle_source = Path("app/alerts/telegram_lifecycle.py").read_text(encoding="utf-8")
    assert "run_telegram_bot" not in run_scan_source
    assert "run_telegram_bot" not in lifecycle_source

    db_path = tmp_path / "listener-independent.db"
    symbol = _generated_entry_batch(db_path)
    sender = FakeSender()
    summary = run(_service(db_path, sender).deliver_for_run(_run_result(symbol), scan_run_id="no-listener"))

    assert summary.sent == 1
    assert len(sender.messages) == 1
    assert "SIGNAL CONFIRMED · 🎯 ZONE ACTIVE" in sender.messages[0]


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
    assert summary.sent == 1
    assert len(sender.messages) == 1
    assert "SIGNAL CONFIRMED · 🎯 ZONE ACTIVE" in sender.messages[0]
