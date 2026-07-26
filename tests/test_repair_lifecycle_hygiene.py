from __future__ import annotations

import sqlite3

import pytest

from app.lifecycle.hygiene import LifecycleHygieneError
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


def test_repair_lifecycle_hygiene_dry_run_preserves_historical_evidence(tmp_path) -> None:
    db_path = _fixture_db(tmp_path)
    before = _db_state(db_path)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        plan = repair_database(connection, apply=False)

    assert len(plan.items) == 1
    assert plan.historical_preserve[0].lifecycle_id == "life-bad"
    assert plan.historical_preserve[0].geometry_failure == "unsupported_direction:n/a"
    assert _db_state(db_path) == before


def test_repair_apply_requires_explicit_reviewed_manifest(tmp_path) -> None:
    db_path = _fixture_db(tmp_path)
    before = _db_state(db_path)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        with pytest.raises(LifecycleHygieneError, match="manifest"):
            repair_database(connection, apply=True)

    assert _db_state(db_path) == before


def test_repair_lifecycle_hygiene_apply_does_not_rewrite_historical_or_telegram_rows(
    tmp_path,
) -> None:
    db_path = _fixture_db(tmp_path)
    before = _db_state(db_path)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        plan = repair_database(connection, apply=False)
        applied = repair_database(
            connection,
            apply=True,
            manifest=plan.manifest_template,
        )

    assert applied.applied_count == 0
    assert _db_state(db_path) == before


def test_repair_lifecycle_hygiene_is_idempotent_for_historical_rows(tmp_path) -> None:
    db_path = _fixture_db(tmp_path)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        first_plan = repair_database(connection, apply=False)
        first = repair_database(
            connection,
            apply=True,
            manifest=first_plan.manifest_template,
        )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        second_plan = repair_database(connection, apply=False)
        second = repair_database(
            connection,
            apply=True,
            manifest=second_plan.manifest_template,
        )

    assert first.applied_count == 0
    assert second.applied_count == 0
    assert second.historical_preserve[0].lifecycle_id == "life-bad"
    assert _db_state(db_path)[0] == 1
