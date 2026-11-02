"""Presentation travels through the real admission/reservation/recovery path."""
import hashlib

from app.alerts.telegram_lifecycle import TelegramLifecycleDeliveryService, SQLiteTelegramAlertAttemptRepository
from app.alerts.telegram_rich_message import is_rich_message
from app.core.config import Settings
from test_triggered_confirmed_telegram_delivery import _public_setup_symbol, _make_lifecycle_retry_due
from test_telegram_lifecycle_delivery_phase42 import FakeSender, _run_result, run


def service(path, sender, style="precision"):
    return TelegramLifecycleDeliveryService(
        database_path=path, sender=sender,
        settings=Settings(_env_file=None, telegram_signal_presentation=style),
    )


def test_precision_public_signal_is_durable_and_not_resent_after_restart(tmp_path):
    path = tmp_path / "precision.db"
    symbol = _public_setup_symbol(signal_id="precision-original")
    sender = FakeSender()
    result = run(service(path, sender).deliver_for_run(_run_result(symbol), scan_run_id="precision-first"))
    assert result.sent == 1 and len(sender.messages) == 1
    payload = sender.messages[0]
    assert is_rich_message(payload)
    with SQLiteTelegramAlertAttemptRepository(path) as repo:
        event = repo._connection.execute("SELECT * FROM public_alert_events WHERE event_type='signal_confirmed'").fetchone()
        assert event["payload_text"] == payload
        assert event["message_hash"] == hashlib.sha256(payload.encode("utf-8")).hexdigest()
        part = repo._connection.execute("SELECT * FROM public_alert_delivery_parts WHERE public_alert_event_id=?", (event["id"],)).fetchone()
        assert part["payload_text"] == payload and part["part_count"] == 1
    restarted = FakeSender()
    repeated = run(service(path, restarted).deliver_for_run(_run_result(symbol), scan_run_id="precision-repeat"))
    assert repeated.sent == 0 and restarted.messages == []


def test_recovery_preserves_saved_format_even_after_style_rolls_back(tmp_path):
    path = tmp_path / "precision-recovery.db"
    sender = FakeSender(status="failed")
    result = run(service(path, sender).deliver_for_run(
        _run_result(_public_setup_symbol(signal_id="precision-retry")), scan_run_id="precision-before-retry"))
    assert result.sent == 0 and len(sender.messages) == 1
    assert is_rich_message(sender.messages[0])
    _make_lifecycle_retry_due(path)
    restarted = FakeSender()
    with SQLiteTelegramAlertAttemptRepository(path) as repo:
        run(service(path, restarted, "legacy").recover_public_lifecycle_alerts(repository=repo))
    assert restarted.messages == sender.messages
