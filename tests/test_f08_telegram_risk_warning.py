from __future__ import annotations

import hashlib
import sqlite3
from decimal import Decimal

from app.agents.alert_agent import AlertAgent
from app.alerts.integrity_manifest import (
    CANONICAL_PUBLIC_RISK_WARNING,
    LEGACY_POSITION_SIZE_RISK_WARNING,
    audit_alert_integrity_artifact,
    build_alert_integrity_manifest,
)
from app.alerts.templates import DEFAULT_RISK_WARNING
from app.alerts.telegram_lifecycle import (
    DEFAULT_CONFIRMED_MIN_RR,
    PUBLIC_SIGNAL_MIN_RR,
    SQLiteTelegramAlertAttemptRepository,
)
from app.alerts.templates import split_message
from app.analytics.public_signal_quality import (
    MIN_PUBLIC_SIGNAL_GRADE,
    MIN_PUBLIC_SETUP_QUALITY_SCORE,
)
from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import (
    CCI_FOOTER,
    FOOTER,
    PUBLIC_TRADE_MAP_RISK_WARNING,
    TelegramAlertType,
    TelegramSignalMessage,
    format_public_no_trade_message,
    format_telegram_signal_message,
    format_watchlist_upgraded_message,
)
from app.storage.database import SCHEMA_VERSION

from test_alert_agent import _idea
from test_telegram_lifecycle_delivery_phase42 import FakeSender, _run_result, run
from test_telegram_signal_formatter_phase42 import _message
from test_triggered_confirmed_telegram_delivery import _public_setup_symbol, _service


EXCLUDED_FOLLOW_UPS = (
    TelegramAlertType.LIMIT_HIT,
    TelegramAlertType.TP1_HIT,
    TelegramAlertType.TP2_HIT,
    TelegramAlertType.TP3_HIT,
    TelegramAlertType.SL_HIT,
    TelegramAlertType.INVALIDATED,
    TelegramAlertType.EXPIRED,
    TelegramAlertType.NO_LONGER_TRACKING,
)


def _warning_count(text: str) -> int:
    return sum(1 for line in text.splitlines() if line.strip() == PUBLIC_TRADE_MAP_RISK_WARNING)


def _manifest(message: str, parts: tuple[str, ...] | None = None):
    return build_alert_integrity_manifest(
        trade_idea=_idea(),
        formatted_message=message,
        message_parts=(message,) if parts is None else parts,
        channel="telegram",
        status="dry_run",
        dry_run=True,
        deduplication_key="BTCUSDT-15m-liquidity_grab_pullback_swing",
    )


def test_public_gates_and_schema_stay_at_the_accepted_floor() -> None:
    assert MIN_PUBLIC_SETUP_QUALITY_SCORE == Decimal("88")
    assert MIN_PUBLIC_SIGNAL_GRADE == "A"
    assert PUBLIC_SIGNAL_MIN_RR == Decimal("3")
    assert DEFAULT_CONFIRMED_MIN_RR == Decimal("3")
    assert SCHEMA_VERSION == 26


def test_trade_map_announcements_carry_one_warning_before_the_signature() -> None:
    confirmed = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())
    watchlist = format_telegram_signal_message(TelegramAlertType.WATCHLIST, _message())
    triggered = format_telegram_signal_message(TelegramAlertType.SETUP_TRIGGERED, _message())
    upgraded = format_watchlist_upgraded_message(_message(upgraded_from_watchlist=True))
    research = format_telegram_signal_message(TelegramAlertType.RESEARCH_WATCH, _message())

    for text, signature in (
        (confirmed, CCI_FOOTER),
        (watchlist, FOOTER),
        (triggered, FOOTER),
        (upgraded, FOOTER),
        (research, FOOTER),
    ):
        assert _warning_count(text) == 1
        assert text.index(PUBLIC_TRADE_MAP_RISK_WARNING) < text.index(signature)

    assert "🟢 SIGNAL CONFIRMED" in confirmed
    assert "SIGNAL CONFIRMED" not in watchlist
    assert "CONFIRMATION PENDING" in triggered
    assert "SIGNAL CONFIRMED" not in triggered
    assert "WATCHLIST UPGRADED" in upgraded
    assert "🟢 SIGNAL CONFIRMED" in upgraded
    assert "Research Watch" in research
    assert "SIGNAL CONFIRMED" not in research
    assert "Invalid if price body-closes and accepts below 95." in watchlist
    assert "🛡 SL 95" in confirmed


def test_research_watch_without_a_trade_map_omits_the_warning() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.RESEARCH_WATCH,
        TelegramSignalMessage(symbol="BTCUSDT", direction=NA, quality="B"),
    )

    assert "Trade map:" in text
    assert "N/A" in text
    assert PUBLIC_TRADE_MAP_RISK_WARNING not in text
    assert "SIGNAL CONFIRMED" not in text


def test_missing_plan_fields_keep_na_and_do_not_invent_a_warning_value() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        TelegramSignalMessage(symbol="ETHUSDT", direction="long"),
    )

    assert _warning_count(text) == 1
    assert "Risk warning: N/A" not in text
    assert "🎯 ENTRY N/A" in text
    assert "🛡 SL N/A" in text
    assert "TP1 N/A" in text


def test_outcome_admin_and_no_trade_text_are_not_given_a_trade_map_warning() -> None:
    message = _message()
    for alert_type in EXCLUDED_FOLLOW_UPS:
        text = format_telegram_signal_message(alert_type, message)
        assert PUBLIC_TRADE_MAP_RISK_WARNING not in text
    no_trade = format_public_no_trade_message(message, "quality_below_min")
    assert "NO TRADE" in no_trade
    assert PUBLIC_TRADE_MAP_RISK_WARNING not in no_trade
    assert "No chase." in format_telegram_signal_message(
        TelegramAlertType.INVALIDATED,
        _message(was_watchlist=False),
    )


def test_explicit_warning_audit_rejects_discipline_and_placeholders() -> None:
    absent = (
        "No chase.",
        "No chase. We want clean entry reaction and fast movement away from chop.",
        "Manual execution only. Manage risk.",
        "Not financial advice.",
        "This is not financial advice. Position size must be based on stop-loss risk, not desired profit.",
        "Risk warning:",
        "Risk warning: ",
        "Risk warning: N/A",
        "Risk warning: N/A.",
        "Risk warning: TBD",
        "Risk warning: ...",
        "Risk warning: No chase.",
        "Risk warning: No chase. Manual execution only.",
        "Risk warning: No chase. Manage risk.",
        "Risk warning: Not",
        "Risk warning: manual execution",
        "Risk warning: Not financial advice. Trading can result in losses",
        f"Risk warning: {DEFAULT_RISK_WARNING}",
        "Risk warning: crypto derivatives are high risk. Manual review only.",
        "Risk warning: This is not financial advice. Pullback ideas are conditional and must be invalidated at the stop.",
    )
    for text in absent:
        manifest = _manifest(text)
        assert manifest.safety_checks["message_has_risk_warning"] is False, text
        assert any(issue.code == "message_missing_risk_warning" for issue in manifest.issues), text
        assert manifest.message_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()
        assert manifest.is_valid is False

    assert CANONICAL_PUBLIC_RISK_WARNING == PUBLIC_TRADE_MAP_RISK_WARNING
    assert LEGACY_POSITION_SIZE_RISK_WARNING == (
        "Risk warning: This is not financial advice. Position size must be based on "
        "stop-loss risk, not desired profit."
    )
    card = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())
    spaced = "risk   WARNING:  not financial advice.   trading can result in losses."
    for warning in (PUBLIC_TRADE_MAP_RISK_WARNING, LEGACY_POSITION_SIZE_RISK_WARNING, spaced):
        text = card.replace(PUBLIC_TRADE_MAP_RISK_WARNING, warning)
        manifest = _manifest(text)
        assert manifest.safety_checks["message_has_risk_warning"] is True, warning
        assert manifest.safety_checks["message_has_invalidation"] is True
        assert manifest.is_valid is True, [issue.code for issue in manifest.issues]
        assert manifest.message_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()

    assert PUBLIC_TRADE_MAP_RISK_WARNING not in LEGACY_POSITION_SIZE_RISK_WARNING


def test_warning_dropped_from_emitted_parts_is_not_present() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())
    dropped = text.replace(f"\n\n{PUBLIC_TRADE_MAP_RISK_WARNING}", "")
    assert PUBLIC_TRADE_MAP_RISK_WARNING not in dropped

    hidden = _manifest(text, (dropped,))
    assert hidden.safety_checks["message_has_risk_warning"] is False

    parts = split_message(text, max_length=180)
    assert len(parts) > 1
    assert all(len(part) <= 180 for part in parts)
    assert sum(part.count(PUBLIC_TRADE_MAP_RISK_WARNING) for part in parts) == 1
    assert PUBLIC_TRADE_MAP_RISK_WARNING.isascii()
    assert text.encode("utf-8").decode("utf-8") == text
    emitted = _manifest(text, parts)
    assert emitted.safety_checks["message_has_risk_warning"] is True
    assert emitted.safety_checks["message_has_invalidation"] is True


def test_complete_cross_part_warning_counts_and_a_dropped_continuation_does_not() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())
    parts = split_message(text, max_length=30)
    assert "advice. Trading can result in" in parts
    assert "losses." in parts
    assert all(len(part) <= 30 for part in parts)
    complete = _manifest(text, parts)
    assert complete.safety_checks["message_has_risk_warning"] is True
    assert complete.safety_checks["message_has_invalidation"] is True
    assert " ".join(" ".join(parts).split()).count(PUBLIC_TRADE_MAP_RISK_WARNING) == 1

    kept = tuple(
        part
        for part in parts
        if part not in {"advice. Trading can result in", "losses."}
    )
    assert "Risk warning: Not financial" in kept
    assert PUBLIC_TRADE_MAP_RISK_WARNING not in " ".join(" ".join(kept).split())
    assert text == format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())
    dropped = _manifest(text, kept)
    assert dropped.safety_checks["message_has_risk_warning"] is False
    assert any(issue.code == "message_missing_risk_warning" for issue in dropped.issues)
    assert dropped.safety_checks["message_has_invalidation"] is True
    assert dropped.message_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_explicit_empty_parts_do_not_borrow_the_source_warning() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())
    manifest = _manifest(text, ())
    codes = [issue.code for issue in manifest.issues]
    assert "missing_message_parts" in codes
    assert manifest.safety_checks["message_has_risk_warning"] is False
    assert "message_missing_risk_warning" in codes
    assert manifest.safety_checks["message_has_invalidation"] is True
    assert manifest.message_sha256 == hashlib.sha256(text.encode("utf-8")).hexdigest()


_OMITTED_PARTS = object()


def _stored_artifact(message_parts: object = _OMITTED_PARTS) -> tuple[dict[str, object], str, str]:
    idea = _idea()
    text = AlertAgent().format(idea)
    manifest = build_alert_integrity_manifest(
        trade_idea=idea,
        formatted_message=text,
        message_parts=(text,),
        channel="telegram",
        status="dry_run",
        dry_run=True,
        deduplication_key="synthetic:f08-parts-evidence",
    )
    assert manifest.is_valid is True
    stored = manifest.model_dump(mode="python")
    alert_result: dict[str, object] = {
        "formatted_message": text,
        "channel": "telegram",
        "status": "dry_run",
        "dry_run": True,
        "deduplication_key": "synthetic:f08-parts-evidence",
        "integrity_manifest": stored,
    }
    if message_parts is not _OMITTED_PARTS:
        alert_result["message_parts"] = message_parts
    artifact = {
        "trade_idea": idea.model_dump(mode="python"),
        "alert_result": alert_result,
    }
    return artifact, stored["message_sha256"], stored["payload_sha256"]


def test_artifact_audit_keeps_explicit_empty_parts_empty() -> None:
    artifact, message_sha, payload_sha = _stored_artifact([])
    result = audit_alert_integrity_artifact(artifact)
    codes = [issue.code for record in result.records for issue in record.issues]
    assert result.summary.is_valid is False
    assert "message_missing_risk_warning" in codes
    assert "missing_message_parts" in codes
    assert artifact["alert_result"]["integrity_manifest"]["message_sha256"] == message_sha  # type: ignore[index]
    assert artifact["alert_result"]["integrity_manifest"]["payload_sha256"] == payload_sha  # type: ignore[index]
    assert artifact["alert_result"]["message_parts"] == []  # type: ignore[index]


def test_omitted_message_parts_use_the_formatted_message_as_one_legacy_part() -> None:
    artifact, message_sha, payload_sha = _stored_artifact()
    assert "message_parts" not in artifact["alert_result"]  # type: ignore[operator]
    result = audit_alert_integrity_artifact(artifact)
    codes = [issue.code for record in result.records for issue in record.issues]
    assert result.summary.is_valid is True
    assert "message_missing_risk_warning" not in codes
    assert "missing_message_parts" not in codes
    assert "malformed_message_parts" not in codes
    assert artifact["alert_result"]["integrity_manifest"]["message_sha256"] == message_sha  # type: ignore[index]
    assert artifact["alert_result"]["integrity_manifest"]["payload_sha256"] == payload_sha  # type: ignore[index]


def test_malformed_message_parts_are_not_emitted_warning_evidence() -> None:
    text = AlertAgent().format(_idea())
    malformed_values = (
        text,
        {"part": PUBLIC_TRADE_MAP_RISK_WARNING},
        None,
        [text, None],
        [PUBLIC_TRADE_MAP_RISK_WARNING, 1],
    )
    for value in malformed_values:
        artifact, message_sha, payload_sha = _stored_artifact(value)
        result = audit_alert_integrity_artifact(artifact)
        codes = [issue.code for record in result.records for issue in record.issues]
        assert result.summary.is_valid is False, value
        assert "malformed_message_parts" in codes
        assert "message_missing_risk_warning" in codes
        assert "missing_message_parts" in codes
        assert artifact["alert_result"]["message_parts"] == value  # type: ignore[index]
        assert artifact["alert_result"]["integrity_manifest"]["message_sha256"] == message_sha  # type: ignore[index]
        assert artifact["alert_result"]["integrity_manifest"]["payload_sha256"] == payload_sha  # type: ignore[index]


def test_alert_agent_keeps_internal_risk_warning_metadata() -> None:
    idea = _idea().model_copy(update={"risk_warning": "N/A"})
    message = AlertAgent().format(idea)

    assert idea.risk_warning == "N/A"
    assert _warning_count(message) == 1
    assert "Risk warning: N/A" not in message
    assert "Position size must be based on stop-loss risk" not in message


def test_sent_economic_event_survives_a_formatter_change_and_restart(tmp_path) -> None:
    db_path = tmp_path / "f08-sent-continuity.db"
    sender = FakeSender()
    symbol = _public_setup_symbol(signal_id="f08-sent-continuity")
    first = run(_service(db_path, sender).deliver_for_run(_run_result(symbol)))

    assert first.sent == 1
    assert len(sender.messages) == 1
    assert _warning_count(sender.messages[0]) == 1

    legacy = sender.messages[0].replace(f"\n\n{PUBLIC_TRADE_MAP_RISK_WARNING}", "")
    assert PUBLIC_TRADE_MAP_RISK_WARNING not in legacy
    legacy_hash = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    part_hash = legacy_hash

    with sqlite3.connect(db_path) as connection:
        event = connection.execute(
            "SELECT id, event_key, status, delivery_state FROM public_alert_events"
        ).fetchone()
        assert event is not None
        event_id = int(event[0])
        connection.execute(
            """
            UPDATE public_alert_events
            SET payload_text = ?, message_hash = ?, status = 'SENT', delivery_state = 'SENT'
            WHERE id = ?
            """,
            (legacy, legacy_hash, event_id),
        )
        connection.execute(
            """
            UPDATE public_alert_delivery_parts
            SET payload_text = ?, payload_hash = ?
            WHERE public_alert_event_id = ?
            """,
            (legacy, part_hash, event_id),
        )
        connection.commit()

    restarted = FakeSender()
    second = run(_service(db_path, restarted).deliver_for_run(_run_result(symbol)))

    assert restarted.messages == []
    assert second.sent == 0
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT payload_text, message_hash, status, delivery_state
            FROM public_alert_events WHERE id = ?
            """,
            (event_id,),
        ).fetchone()
        part = connection.execute(
            """
            SELECT payload_text, payload_hash
            FROM public_alert_delivery_parts
            WHERE public_alert_event_id = ?
            """,
            (event_id,),
        ).fetchone()
    assert row == (legacy, legacy_hash, "SENT", "SENT")
    assert part[0] == legacy
    assert part[1] == part_hash


def test_pending_restart_sends_the_persisted_legacy_payload(tmp_path) -> None:
    db_path = tmp_path / "f08-pending-restart.db"
    sender = FakeSender()
    symbol = _public_setup_symbol(signal_id="f08-pending-restart")
    first = run(_service(db_path, sender).deliver_for_run(_run_result(symbol)))
    assert first.sent == 1

    legacy = "Legacy confirmed card.\nNo chase.\nCCI · Signal. Structure. Execution."
    legacy_hash = hashlib.sha256(legacy.encode("utf-8")).hexdigest()
    with sqlite3.connect(db_path) as connection:
        event_id = int(connection.execute("SELECT id FROM public_alert_events").fetchone()[0])
        connection.execute(
            """
            UPDATE public_alert_events
            SET payload_text = ?, message_hash = ?, status = 'RESERVED',
                delivery_state = 'PENDING', attempt_count = 0
            WHERE id = ?
            """,
            (legacy, legacy_hash, event_id),
        )
        connection.execute(
            """
            UPDATE public_alert_delivery_parts
            SET payload_text = ?, payload_hash = ?, delivery_state = 'PENDING'
            WHERE public_alert_event_id = ?
            """,
            (legacy, legacy_hash, event_id),
        )
        connection.commit()

    recovered_sender = FakeSender()
    service = _service(db_path, recovered_sender)
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        recovered = run(service.recover_public_lifecycle_alerts(repository=repository))
    assert len(recovered) == 1
    assert recovered[0].status == "sent"
    assert recovered_sender.messages == [legacy]
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT payload_text, message_hash FROM public_alert_events WHERE id = ?",
            (event_id,),
        ).fetchone()
    assert row == (legacy, legacy_hash)
