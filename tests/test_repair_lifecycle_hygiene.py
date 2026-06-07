from __future__ import annotations

import sqlite3

from app.storage.database import open_initialized_database
from scripts.repair_lifecycle_hygiene import repair_database


def _fixture_db(tmp_path):
    db_path = tmp_path / "hygiene.sqlite"
    connection = open_initialized_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO setup_lifecycle_records (
                lifecycle_id, symbol, mode, direction, current_state, previous_state,
                first_seen_at, last_seen_at, last_transition_at, failed_gate,
                readiness_score, quality_score, edge_score, regime_state, action_label,
                invalidation_reason, entry_low, entry_high, stop_loss, tp1, tp2, tp3,
                rr, quality_grade_current
            ) VALUES (
                'life-bad', 'BADUSDT', 'swing', 'n/a', 'INVALIDATED', 'CONFIRMED',
                '2026-06-07T00:00:00Z', '2026-06-07T00:00:00Z', '2026-06-07T00:00:00Z',
                'rejected_no_edge', 0, 0, 'N/A', 'mixed', 'N/A',
                'Setup invalidated.', 'N/A', 'N/A', 'N/A', 'N/A', 'N/A', 'N/A',
                'N/A', 'Reject'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, previous_state, new_state, alert_type,
                lifecycle_state, sent_at, attempted_at, telegram_status, message_hash,
                attempted_alert_type
            ) VALUES (
                'sig-blocked', 'BADUSDT', 'long', 'N/A', 'WATCHLISTED', 'WATCHLIST_BLOCKED_abc',
                'WATCHLISTED', '2026-06-07T00:00:00Z', 'N/A', 'blocked', 'hash',
                'WATCHLIST'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    return db_path


def _db_state(db_path):
    with sqlite3.connect(db_path) as connection:
        lifecycle_count = connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0]
        archived_at = connection.execute(
            "SELECT archived_at FROM setup_lifecycle_records WHERE lifecycle_id = 'life-bad'"
        ).fetchone()[0]
        sent_at, attempted_at = connection.execute(
            "SELECT sent_at, attempted_at FROM telegram_alert_attempts WHERE signal_id = 'sig-blocked'"
        ).fetchone()
    return lifecycle_count, archived_at, sent_at, attempted_at


def test_repair_lifecycle_hygiene_dry_run_makes_no_changes(tmp_path) -> None:
    db_path = _fixture_db(tmp_path)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        summary = repair_database(connection, apply=False)

    assert summary.rows_scanned == 1
    assert summary.rows_to_archive == 1
    assert summary.telegram_bad_sent_at == 1
    assert _db_state(db_path) == (1, None, "2026-06-07T00:00:00Z", "N/A")


def test_repair_lifecycle_hygiene_apply_clears_blocked_sent_at_and_preserves_rows(tmp_path) -> None:
    db_path = _fixture_db(tmp_path)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        summary = repair_database(connection, apply=True)
        connection.commit()

    lifecycle_count, archived_at, sent_at, attempted_at = _db_state(db_path)
    assert summary.rows_archived == 1
    assert summary.telegram_sent_at_cleared == 1
    assert lifecycle_count == 1
    assert archived_at is not None
    assert sent_at is None
    assert attempted_at == "2026-06-07T00:00:00Z"


def test_repair_lifecycle_hygiene_is_idempotent(tmp_path) -> None:
    db_path = _fixture_db(tmp_path)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        repair_database(connection, apply=True)
        connection.commit()
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        second = repair_database(connection, apply=True)
        connection.commit()

    assert second.rows_archived == 0
    assert second.telegram_sent_at_cleared == 0
    assert _db_state(db_path)[0] == 1
