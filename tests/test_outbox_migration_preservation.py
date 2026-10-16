"""Synthetic outbox history must survive later migrations and current-schema opens."""
import sqlite3

import pytest

from app.runtime_epoch.startup import migrate_existing_database
from app.storage import database as db
from tests.test_confirmation_consumption_migration import _v26

pytestmark = pytest.mark.no_auto_epoch


def _seed_outbox(path):
    cases = (
        ("placeholder", "RESERVED", "PENDING", "reserved", "N/A", 0),
        ("sent", "SENT", "SENT", "sent", "SENT", 1),
        ("uncertain", "RESERVED", "UNCERTAIN", "reserved", "UNCERTAIN", 1),
        ("failed", "FAILED", "FAILED_FINAL", "failed", "FAILED_FINAL", 3),
        ("in-flight", "RESERVED", "IN_FLIGHT", "reserved", "IN_FLIGHT", 1),
        ("pending", "RESERVED", "PENDING", "reserved", "PENDING", 0),
    )
    with sqlite3.connect(path) as connection:
        for label, status, state, telegram_status, attempt_state, count in cases:
            key = f"synthetic-{label}|limit_hit"
            payload = "N/A" if label == "placeholder" else f"Synthetic {label} payload"
            message_hash = "N/A" if label == "placeholder" else f"synthetic-{label}-hash"
            timestamp = "2026-03-01T13:00:00Z"
            uncertain_at = timestamp if state == "UNCERTAIN" else None
            sent_at = timestamp if state == "SENT" else None
            error = "transport_acceptance_unknown" if uncertain_at else "N/A"
            attempt_id = f"attempt-{label}" if count else None
            reservation = connection.execute(
                """INSERT INTO telegram_alert_attempts (
                    signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
                    telegram_status, message_hash, sent_at, attempted_at,
                    public_watchlist_plan_id, public_watchlist_event_key, delivery_state,
                    delivery_attempt_id, delivery_attempt_count, delivery_last_error_category
                ) VALUES (?, 'TESTUSDT', 'long', 'TRIGGERED', 'LIMIT_HIT', 'TRIGGERED',
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (f"signal-{label}", telegram_status, message_hash, sent_at, timestamp,
                 f"plan-{label}", key, attempt_state, attempt_id, count, error),
            ).lastrowid
            event = connection.execute(
                """INSERT INTO public_alert_events (
                    canonical_plan_id, event_type, event_key, symbol, side, status,
                    delivery_state, payload_text, message_hash, attempt_count, attempt_id,
                    reserved_at, sent_at, uncertain_at, last_error_category,
                    created_at, updated_at, canonical_reservation_attempt_id
                ) VALUES (?, 'limit_hit', ?, 'TESTUSDT', 'long', ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?)""",
                (f"plan-{label}", key, status, state, payload, message_hash, count,
                 attempt_id, timestamp, sent_at, uncertain_at, error, timestamp,
                 timestamp, reservation),
            ).lastrowid
            # A placeholder has no prepared transport parts, as in the failing Runtime shape.
            if label != "placeholder":
                connection.execute(
                    """INSERT INTO public_alert_delivery_parts (
                        public_alert_event_id, event_key, part_index, part_count,
                        payload_text, payload_hash, delivery_state, attempt_count,
                        attempt_id, sent_at, last_error_category, created_at, updated_at
                    ) VALUES (?, ?, 1, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (event, key, payload, message_hash, state, count, attempt_id,
                     sent_at, error, timestamp, timestamp),
                )


def _rows(path):
    with sqlite3.connect(path) as connection:
        tables = [r[0] for r in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        )]
        return {
            name: connection.execute(
                'SELECT * FROM "' + name.replace('"', '""') + '" ORDER BY rowid'
            ).fetchall()
            for name in tables
        }


@pytest.mark.parametrize("source_schema", [26, 27])
def test_existing_outbox_rows_survive_migration_and_reopen(tmp_path, source_schema):
    path = _v26(tmp_path / "synthetic-history.db")
    if source_schema == 27:
        migrate_existing_database(path)
    _seed_outbox(path)
    before = _rows(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == source_schema
    migrate_existing_database(path)
    after = _rows(path)
    assert after.keys() == before.keys()
    for table in before:
        if table == "runtime_operational_origins" and source_schema == 26:
            assert [row[:-1] for row in after[table]] == before[table]
            assert all(row[-1] is None for row in after[table])
        else:
            assert after[table] == before[table], table
    migrate_existing_database(path)
    assert _rows(path) == after
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 27
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("fail_after", ["column", "version"])
def test_failed_v27_migration_preserves_populated_outbox(tmp_path, monkeypatch, fail_after):
    path = _v26(tmp_path / "synthetic-failure.db")
    _seed_outbox(path)
    before = _rows(path)
    original = db._ensure_column

    def fail(connection, table, column, declaration):
        original(connection, table, column, declaration)
        if column == "lifecycle_consumption_json":
            if fail_after == "version":
                connection.execute("PRAGMA user_version = 27")
            raise sqlite3.OperationalError("injected v27 migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(db, "_ensure_column", fail)
        with pytest.raises(db.StorageError):
            migrate_existing_database(path)
    assert _rows(path) == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 26
    migrate_existing_database(path)
    after = _rows(path)
    for table in ("public_alert_events", "telegram_alert_attempts", "public_alert_delivery_parts"):
        assert after[table] == before[table]
