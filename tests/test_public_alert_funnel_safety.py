from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from app.analytics.public_alert_funnel import (
    build_public_alert_funnel_report,
    classify_block_stage,
    is_otherwise_publishable_near_miss,
    normalize_public_block_reasons,
)
from app.alerts.telegram_sender import TelegramSender
from app.alerts.telegram_lifecycle import (
    TelegramEligibilityContext,
    _public_watchlist_gate_result,
    telegram_signal_message_from_symbol,
)
from tests.test_telegram_lifecycle_delivery_phase42 import _public_target_caution_symbol


def test_public_block_normalization_is_reporting_only_for_gate_result() -> None:
    symbol = _public_target_caution_symbol(rr="2.79", signal_id="target-caution-reporting-only")
    message = telegram_signal_message_from_symbol(symbol)
    before = _public_watchlist_gate_result(symbol, message, TelegramEligibilityContext())

    raw_reason = "blocked:" + "; ".join(before.blocking_reasons)
    categories = normalize_public_block_reasons(raw_reason)
    stage = classify_block_stage(raw_reason)
    publishable = is_otherwise_publishable_near_miss(
        {
            "telegram_status": "blocked",
            "setup_quality_score": "91",
            "rr_planned": "2.79",
            "min_rr": "3",
            "technical_score": "100",
            "opportunity_score": "97",
            "blocked_reason": raw_reason,
            "dedupe_reason": "N/A",
        }
    )
    after = _public_watchlist_gate_result(symbol, message, TelegramEligibilityContext())

    assert "TARGET_CAUTION_RR_BELOW_2_8" in categories
    assert stage == "TARGET_CAUTION_GATE"
    assert publishable is False
    assert before == after
    assert before.allowed is False
    assert "public_block_target_caution_rr_below_2_8" in before.blocking_reasons


def test_public_funnel_diagnostics_do_not_call_telegram_sender(tmp_path, monkeypatch) -> None:
    from tests.test_public_alert_funnel import _seed_public_funnel_database

    def fail_send(*args, **kwargs):
        raise AssertionError("diagnostics must not send Telegram messages")

    monkeypatch.setattr(TelegramSender, "send_message", fail_send)
    monkeypatch.setattr(TelegramSender, "send_text", fail_send)
    db_path = tmp_path / "public-funnel-sender-safety.sqlite"
    _seed_public_funnel_database(db_path)

    report = build_public_alert_funnel_report(
        db_path,
        hours=24,
        limit=5,
        now=datetime(2026, 7, 3, 12, tzinfo=UTC),
    )

    assert report["source_available"] is True


def test_public_funnel_diagnostics_do_not_write_database(tmp_path) -> None:
    from tests.test_public_alert_funnel import _seed_public_funnel_database

    db_path = tmp_path / "public-funnel-readonly-safety.sqlite"
    _seed_public_funnel_database(db_path)
    with sqlite3.connect(db_path) as connection:
        before_count = connection.execute("SELECT COUNT(*) FROM telegram_alert_attempts").fetchone()[0]
        before_max_seen = connection.execute("SELECT MAX(seen_count) FROM telegram_alert_attempts").fetchone()[0]

    report = build_public_alert_funnel_report(
        db_path,
        hours=24,
        limit=5,
        now=datetime(2026, 7, 3, 12, tzinfo=UTC),
    )

    with sqlite3.connect(db_path) as connection:
        after_count = connection.execute("SELECT COUNT(*) FROM telegram_alert_attempts").fetchone()[0]
        after_max_seen = connection.execute("SELECT MAX(seen_count) FROM telegram_alert_attempts").fetchone()[0]

    assert report["source_available"] is True
    assert after_count == before_count
    assert after_max_seen == before_max_seen
