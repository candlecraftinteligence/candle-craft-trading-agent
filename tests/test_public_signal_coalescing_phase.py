from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

from app.alerts.telegram_lifecycle import (
    SQLiteTelegramAlertAttemptRepository,
    public_outcome_tracking_diagnostics,
)
from app.alerts.telegram_routing import TelegramDestination
from app.alerts.telegram_sender import TelegramSender
from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import (
    SignalEdgeEvidence,
    SignalMessageContext,
    TelegramAlertType,
    TelegramSignalMessage,
    format_telegram_signal_message,
)
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository

from test_public_tp_milestone_delivery import (
    _persist_progress,
    _send_confirmed_root,
    _sent_outcomes,
)
from test_triggered_confirmed_telegram_delivery import (
    FakeSender,
    TransportStateSequenceSender,
    _empty_run_result,
    _generated_entry_batch,
    _make_lifecycle_retry_due,
    _public_setup_symbol,
    _run_result,
    _service,
    run,
)


def _confirmed_only_batch(db_path):
    batch = _generated_entry_batch(db_path)
    transitions = batch.lifecycle_transitions[:2]
    confirmed = transitions[-1]
    assert confirmed.record is not None
    return batch.model_copy(
        update={
            "lifecycle_state": confirmed.record,
            "lifecycle_transition": confirmed,
            "lifecycle_transitions": transitions,
        }
    )


def _message(**overrides):
    values = {
        "symbol": "ICPUSDT",
        "direction": "long",
        "mode": "scalp",
        "primary_mode": "scalp",
        "source_modes": ("scalp", "swing"),
        "quality": "A",
        "quality_score": 91,
        "entry_low": Decimal("2.391"),
        "entry_high": Decimal("2.395"),
        "stop_loss": Decimal("2.3765"),
        "tp1": Decimal("2.414"),
        "tp2": Decimal("2.4282"),
        "tp3": Decimal("2.440"),
        "planned_rr": Decimal("3.22"),
        "edge_evidence": SignalEdgeEvidence(
            sweep_present=True,
            structure_present=True,
            structure_kind="BOS",
            selected_zone_type="OB",
            fib_aligned=True,
        ),
    }
    values.update(overrides)
    message = TelegramSignalMessage(**values)
    return replace(
        message,
        signal_context=SignalMessageContext(
            symbol=message.symbol,
            direction=message.direction,
            primary_mode=message.primary_mode,
            source_modes=message.source_modes,
            grade=message.quality,
            quality_score=message.quality_score,
            entry_low=message.entry_low,
            entry_high=message.entry_high,
            stop_loss=message.stop_loss,
            tp1=message.tp1,
            tp2=message.tp2,
            tp3=message.tp3,
            rr=message.planned_rr,
            edge_evidence=message.edge_evidence,
            confluence_valid=True,
        ),
    )


def test_triggered_alone_is_internal_only_with_deterministic_audit(tmp_path) -> None:
    db_path = tmp_path / "triggered-internal.db"
    sender = FakeSender()
    triggered = _public_setup_symbol(
        signal_id="triggered-internal",
        state=SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.STALKING,
    )

    summary = run(_service(db_path, sender).deliver_for_run(_run_result(triggered)))

    assert summary.sent == 0
    assert sender.messages == []
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        audit = next(
            item
            for item in repository.list_attempts()
            if item.attempted_alert_type == TelegramAlertType.SETUP_TRIGGERED.value
        )
    assert audit.telegram_status == "skipped"
    assert audit.dedupe_reason == "public_triggered_internal_only"


def test_triggered_and_confirmed_batch_sends_one_confirmed(tmp_path) -> None:
    db_path = tmp_path / "triggered-confirmed.db"
    sender = FakeSender()

    summary = run(
        _service(db_path, sender).deliver_for_run(
            _run_result(_confirmed_only_batch(db_path))
        )
    )

    assert summary.sent == 1
    assert len(sender.messages) == 1
    assert "SIGNAL CONFIRMED" in sender.messages[0]
    assert "ZONE ACTIVE" not in sender.messages[0]
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        reasons = {item.dedupe_reason for item in repository.list_attempts()}
    assert "public_triggered_coalesced_into_confirmation" in reasons


def test_triggered_confirmed_and_limit_batch_sends_one_combined_card_and_keeps_history(tmp_path) -> None:
    db_path = tmp_path / "combined.db"
    sender = FakeSender()
    batch = _generated_entry_batch(db_path)

    summary = run(_service(db_path, sender).deliver_for_run(_run_result(batch)))

    assert summary.sent == 1
    assert len(sender.messages) == 1
    assert "SIGNAL CONFIRMED · 🎯 ZONE ACTIVE" in sender.messages[0]
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        reasons = {item.dedupe_reason for item in repository.list_attempts()}
    assert "public_triggered_coalesced_into_confirmation" in reasons
    assert "public_limit_hit_coalesced_into_confirmation" in reasons
    assert tuple(item.to_state for item in batch.lifecycle_transitions) == (
        SetupLifecycleState.TRIGGERED,
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.EXECUTING,
        SetupLifecycleState.MANAGING,
    )


def test_limit_hit_in_later_iteration_sends_one_short_follow_up(tmp_path) -> None:
    db_path = tmp_path / "later-limit.db"
    sender = FakeSender()
    confirmed = _public_setup_symbol(signal_id="later-limit")
    initial = run(_service(db_path, sender).deliver_for_run(_run_result(confirmed)))
    managing = _public_setup_symbol(
        signal_id="later-limit",
        state=SetupLifecycleState.MANAGING,
        previous=SetupLifecycleState.EXECUTING,
    )

    follow_up = run(_service(db_path, sender).deliver_for_run(_run_result(managing)))

    assert initial.sent == 1
    assert follow_up.sent == 1
    assert len(sender.messages) == 2
    assert sender.messages[-1].startswith("🎯 BTCUSDT · ZONE ENGAGED")
    assert "Price has reached the mapped territory." in sender.messages[-1]
    assert "SIGNAL CONFIRMED" not in sender.messages[-1]


def test_confirmed_formatter_matches_compact_contract_and_structured_edge_only() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())

    assert text == (
        "🐺 ICPUSDT · LONG · SCALP/SWING\n\n"
        "🟢 SIGNAL CONFIRMED\n\n"
        "A · Score 91 · 3.22R\n\n"
        "━━━━━━━━━━━━━\n\n"
        "🎯 ENTRY 2.391 – 2.395\n\n"
        "🛡 SL 2.3765\n\n"
        "TP1 2.414\n\n"
        "TP2 2.4282\n\n"
        "TP3 2.44\n\n"
        "━━━━━━━━━━━━━\n\n"
        "🧠 EDGE\n\n"
        "Sweep ✓ · BOS ✓ · OB/Fib ✓\n\n"
        "⚔️ EXECUTION\n\n"
        "Wait for the mapped zone. No chase.\n\n"
        "🐺 Hunt live.\n\n"
        "CCI · Signal. Structure. Execution."
    )
    assert "Not financial advice." not in text
    assert "Manual execution" not in text
    assert "body-closes" not in text

    prose_only = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(
            edge_evidence=None,
            structure_reason="Sweep BOS OB Fib all confirmed.",
            confluence="CHoCH and FVG confirmed.",
        ),
    )
    assert "🧠 EDGE" not in prose_only
    for token in ("Sweep ✓", "BOS ✓", "CHoCH ✓", "OB/Fib ✓", "FVG ✓"):
        assert token not in prose_only


def test_zone_and_outcome_formatter_examples() -> None:
    assert "Zone active. Watch the reaction. No chase." in format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        replace(_message(), zone_active=True),
    )
    assert format_telegram_signal_message(TelegramAlertType.LIMIT_HIT, _message()).startswith(
        "🎯 ICPUSDT · ZONE ENGAGED\n\nEntry 2.391 – 2.395"
    )
    assert "Next → TP2 2.4282" in format_telegram_signal_message(
        TelegramAlertType.TP1_HIT, _message()
    )
    assert "TP1 ✓ · TP2 ✓" in format_telegram_signal_message(
        TelegramAlertType.TP2_HIT,
        replace(_message(), coalesced_milestones=("tp1", "tp2")),
    )
    assert "TP1 ✓ · TP2 ✓ · TP3 ✓" in format_telegram_signal_message(
        TelegramAlertType.TP3_HIT,
        replace(_message(), coalesced_milestones=("tp1", "tp2", "tp3")),
    )
    sl = format_telegram_signal_message(TelegramAlertType.SL_HIT, _message())
    assert "Structural thesis failed." in sl
    assert "No reinterpretation. Next setup." in sl


def test_tp1_then_tp2_delivery_and_success_dedupe(tmp_path) -> None:
    db_path = tmp_path / "tp-sequence.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(db_path, confirmed, tp_count=1)

    tp1 = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    _persist_progress(db_path, confirmed, tp_count=2)
    tp2 = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    repeated = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert tp1.sent == 1
    assert tp2.sent == 1
    assert repeated.sent == 0
    assert [item.alert_type for item in _sent_outcomes(db_path)] == [
        TelegramAlertType.TP1_HIT.value,
        TelegramAlertType.TP2_HIT.value,
    ]
    assert "TP1 ✓ · TP2 ✓" not in sender.messages[-1]


def test_tp1_and_tp2_between_scans_send_one_consolidated_tp2(tmp_path) -> None:
    db_path = tmp_path / "tp2-consolidated.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(db_path, confirmed, tp_count=2)

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 1
    assert "🔥 BTCUSDT · TP2 SECURED" in sender.messages[-1]
    assert "TP1 ✓ · TP2 ✓" in sender.messages[-1]
    assert [item.alert_type for item in _sent_outcomes(db_path)] == [
        TelegramAlertType.TP2_HIT.value
    ]


def test_restart_with_tp1_and_tp2_persisted_sends_one_consolidated_tp2(tmp_path) -> None:
    db_path = tmp_path / "restart-tp2-consolidated.db"
    confirmed = _send_confirmed_root(db_path, FakeSender())
    _persist_progress(db_path, confirmed, tp_count=2)
    restarted_sender = FakeSender()

    summary = run(
        _service(db_path, restarted_sender).deliver_for_run(_empty_run_result())
    )
    repeated = run(
        _service(db_path, restarted_sender).deliver_for_run(_empty_run_result())
    )

    assert summary.sent == 1
    assert repeated.sent == 0
    assert len(restarted_sender.messages) == 1
    assert "🔥 BTCUSDT · TP2 SECURED" in restarted_sender.messages[0]
    assert "TP1 ✓ · TP2 ✓" in restarted_sender.messages[0]


def test_tp3_with_missed_lower_targets_sends_one_full_target(tmp_path) -> None:
    db_path = tmp_path / "tp3-consolidated.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    _persist_progress(db_path, confirmed, tp_count=3)

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    repeated = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 1
    assert repeated.sent == 0
    assert "🏆 BTCUSDT · FULL TARGET" in sender.messages[-1]
    assert "TP1 ✓ · TP2 ✓ · TP3 ✓" in sender.messages[-1]
    assert [item.alert_type for item in _sent_outcomes(db_path)] == [
        TelegramAlertType.TP3_HIT.value
    ]


def test_restart_and_retryable_tp1_reconcile_exactly_once(tmp_path) -> None:
    db_path = tmp_path / "restart-tp1.db"
    confirmed = _send_confirmed_root(db_path, FakeSender())
    _persist_progress(db_path, confirmed, tp_count=1)
    sender = TransportStateSequenceSender("RETRYABLE", "SENT")

    failed = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    _make_lifecycle_retry_due(db_path)
    recovered = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    repeated = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert failed.failed >= 1
    assert recovered.sent == 1
    assert repeated.sent == 0
    assert len(sender.messages) == 2
    assert [item.alert_type for item in _sent_outcomes(db_path)] == [
        TelegramAlertType.TP1_HIT.value
    ]


def test_failed_final_tp1_remains_terminal_bounded_and_auditable(tmp_path) -> None:
    db_path = tmp_path / "failed-final-tp1.db"
    confirmed = _send_confirmed_root(db_path, FakeSender())
    record, expected_progress = _persist_progress(db_path, confirmed, tp_count=1)
    sender = TransportStateSequenceSender("FAILED_FINAL", "SENT")

    failed = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        before = repository._connection.execute(
            """
            SELECT delivery_state, max_attempts, attempt_count, sent_at
            FROM public_alert_events
            WHERE event_type = 'tp1_hit'
            """
        ).fetchone()
    restarted_sender = FakeSender()
    repeated = tuple(
        run(_service(db_path, restarted_sender).deliver_for_run(_empty_run_result()))
        for _ in range(3)
    )

    assert failed.failed >= 1
    assert all(summary.sent == 0 for summary in repeated)
    assert restarted_sender.messages == []
    assert sender.states == ["SENT"]
    assert _sent_outcomes(db_path) == ()
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        after = repository._connection.execute(
            """
            SELECT delivery_state, max_attempts, attempt_count, sent_at
            FROM public_alert_events
            WHERE event_type = 'tp1_hit'
            """
        ).fetchone()
        attempt = next(
            item
            for item in repository.list_attempts()
            if item.alert_type == TelegramAlertType.TP1_HIT.value
        )
    assert tuple(before) == tuple(after)
    assert after["delivery_state"] == "FAILED_FINAL"
    assert after["sent_at"] is None
    assert attempt.telegram_status == "failed_final"
    assert attempt.delivery_state == "FAILED_FINAL"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        progress = repository.get_outcome_progress(
            lifecycle_id=record.lifecycle_id,
            plan_identity=expected_progress.plan_identity,
        )
    assert progress is not None
    assert progress.tp1_at == expected_progress.tp1_at


def test_missing_credentials_failed_final_target_stays_terminal_after_restart(
    tmp_path,
) -> None:
    db_path = tmp_path / "failed-final-missing-credentials.db"
    confirmed = _send_confirmed_root(db_path, FakeSender())
    _persist_progress(db_path, confirmed, tp_count=1)
    missing_credentials_sender = TelegramSender(
        bot_token=None,
        chat_id=None,
        signals_enabled=True,
        dry_run=False,
        local_manual_mode=True,
        destination=TelegramDestination.PUBLIC_CHAT,
    )

    failed = run(
        _service(db_path, missing_credentials_sender).deliver_for_run(
            _empty_run_result()
        )
    )
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        before = repository._connection.execute(
            """
            SELECT delivery_state, max_attempts, attempt_count, sent_at
            FROM public_alert_events
            WHERE event_type = 'tp1_hit'
            """
        ).fetchone()
    restarted_sender = FakeSender()
    restarted = tuple(
        run(_service(db_path, restarted_sender).deliver_for_run(_empty_run_result()))
        for _ in range(2)
    )

    assert failed.failed >= 1
    assert all(summary.sent == 0 for summary in restarted)
    assert restarted_sender.messages == []
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        after = repository._connection.execute(
            """
            SELECT delivery_state, max_attempts, attempt_count, sent_at
            FROM public_alert_events
            WHERE event_type = 'tp1_hit'
            """
        ).fetchone()
        attempt = next(
            item
            for item in repository.list_attempts()
            if item.alert_type == TelegramAlertType.TP1_HIT.value
        )
    assert tuple(before) == tuple(after)
    assert after["delivery_state"] == "FAILED_FINAL"
    assert after["sent_at"] is None
    assert attempt.error_message == "missing_telegram_credentials"
    assert _sent_outcomes(db_path) == ()


def test_different_generation_and_opposite_direction_cannot_inherit_tp(tmp_path) -> None:
    for name, overrides in (
        ("generation", {"structural_anchor": "execution_sweep|15m|other"}),
        ("direction", {"direction": "short", "structural_anchor": "execution_sweep|15m|short"}),
    ):
        db_path = tmp_path / f"identity-{name}.db"
        sender = FakeSender()
        _send_confirmed_root(db_path, sender)
        other = _public_setup_symbol(
            signal_id=f"other-{name}",
            state=SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            **overrides,
        )
        _persist_progress(db_path, other, tp_count=1)
        before = len(sender.messages)

        summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

        assert summary.sent == 0
        assert len(sender.messages) == before
        assert _sent_outcomes(db_path) == ()


def test_same_geometry_new_generation_cannot_inherit_tp(tmp_path) -> None:
    db_path = tmp_path / "identity-same-geometry-generation.db"
    sender = FakeSender()
    _send_confirmed_root(db_path, sender)
    other = _public_setup_symbol(signal_id="same-geometry-new-generation")
    _persist_progress(db_path, other, tp_count=1)
    before = len(sender.messages)

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 0
    assert len(sender.messages) == before
    assert _sent_outcomes(db_path) == ()
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        audits = [
            item
            for item in repository.list_attempts()
            if item.attempted_alert_type == "PUBLIC_OUTCOME_TRACKING"
        ]
    assert audits
    assert audits[-1].blocked_reason == (
        "public_outcome_tracking_lifecycle_generation_mismatch"
    )
    with (
        SQLiteTelegramAlertAttemptRepository(db_path) as alert_repository,
        SQLiteSetupLifecycleRepository(db_path) as lifecycle_repository,
    ):
        diagnostics = public_outcome_tracking_diagnostics(
            alert_repository=alert_repository,
            lifecycle_repository=lifecycle_repository,
        )
    assert diagnostics[0].match_status == "NO_MATCH"
    assert diagnostics[0].match_reason == (
        "public_outcome_tracking_lifecycle_generation_mismatch"
    )


def test_legacy_multi_generation_identity_is_ambiguous_and_blocks(tmp_path) -> None:
    db_path = tmp_path / "identity-ambiguous.db"
    sender = FakeSender()
    confirmed = _send_confirmed_root(db_path, sender)
    record, progress = _persist_progress(db_path, confirmed, tp_count=1)
    other_record = record.model_copy(
        update={
            "lifecycle_id": "legacy-ambiguous-other",
            "mode": "scalp",
            "is_current": True,
        }
    )
    other_progress = progress.model_copy(
        update={
            "lifecycle_id": other_record.lifecycle_id,
            "plan_identity": canonical_plan_identity(other_record),
            "mode": other_record.mode,
        }
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(other_record)
        repository.upsert_outcome_progress(other_progress)
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        root = next(
            item
            for item in repository.list_attempts()
            if item.alert_type == TelegramAlertType.SIGNAL_CONFIRMED.value
        )
        repository._connection.execute(
            "UPDATE telegram_alert_attempts SET signal_id = ? WHERE id = ?",
            ("legacy-opaque-root", root.id),
        )
        repository._connection.commit()
    before = len(sender.messages)

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 0
    assert len(sender.messages) == before
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        audits = [
            item
            for item in repository.list_attempts()
            if item.attempted_alert_type == "PUBLIC_OUTCOME_TRACKING"
        ]
    assert audits
    assert audits[-1].blocked_reason == "public_outcome_tracking_identity_ambiguous"
    with (
        SQLiteTelegramAlertAttemptRepository(db_path) as alert_repository,
        SQLiteSetupLifecycleRepository(db_path) as lifecycle_repository,
    ):
        diagnostics = public_outcome_tracking_diagnostics(
            alert_repository=alert_repository,
            lifecycle_repository=lifecycle_repository,
        )
    assert diagnostics[0].match_status == "AMBIGUOUS"
    assert diagnostics[0].match_reason == "public_outcome_tracking_identity_ambiguous"


def test_public_outcome_no_match_is_persisted_and_queryable(tmp_path) -> None:
    db_path = tmp_path / "identity-no-match.db"
    sender = FakeSender()
    _send_confirmed_root(db_path, sender)

    summary = run(_service(db_path, sender).deliver_for_run(_empty_run_result()))

    assert summary.sent == 0
    with (
        SQLiteTelegramAlertAttemptRepository(db_path) as alert_repository,
        SQLiteSetupLifecycleRepository(db_path) as lifecycle_repository,
    ):
        diagnostics = public_outcome_tracking_diagnostics(
            alert_repository=alert_repository,
            lifecycle_repository=lifecycle_repository,
        )
        audits = tuple(
            item
            for item in alert_repository.list_attempts()
            if item.attempted_alert_type == "PUBLIC_OUTCOME_TRACKING"
        )
    assert len(diagnostics) == 1
    assert diagnostics[0].match_status == "NO_MATCH"
    assert diagnostics[0].match_reason.startswith(
        "public_outcome_tracking_no_lifecycle_match:"
    )
    assert audits
    assert audits[-1].blocked_reason == diagnostics[0].match_reason


def test_all_persisted_target_timestamps_remain_independent(tmp_path) -> None:
    db_path = tmp_path / "timestamps.db"
    confirmed = _send_confirmed_root(db_path, FakeSender())
    record, expected = _persist_progress(db_path, confirmed, tp_count=3)
    run(_service(db_path, FakeSender()).deliver_for_run(_empty_run_result()))

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        actual = repository.get_outcome_progress(
            lifecycle_id=record.lifecycle_id,
            plan_identity=expected.plan_identity,
        )
    assert actual is not None
    assert (actual.tp1_at, actual.tp2_at, actual.tp3_at) == (
        expected.tp1_at,
        expected.tp2_at,
        expected.tp3_at,
    )
