"""Synthetic Runtime-epoch helpers for DEV tests. Never used on Runtime."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.lifecycle.models import SetupLifecycleRecord
from app.runtime_epoch.authority import initialize_runtime_epoch, load_active_runtime_epoch
from app.runtime_epoch.models import RUNTIME_EPOCH_CONTRACT_VERSION, RuntimeEpochIdentity
from app.runtime_epoch.origin import evaluate_symbol_origin, register_operational_run, register_operational_scan_run
from app.runtime_epoch.ownership import (
    PUBLIC_CHAIN_STARTER_EVENT_TYPES,
    PUBLIC_EVENT_FAMILY_TYPES,
    PUBLIC_ROOT_EVENT_TYPES,
    bind_canonical_reservation_attempt,
    normalize_public_event_type,
)
from app.storage.database import open_initialized_database

SYNTHETIC_EPOCH_ID = "cci-test-epoch-synthetic"
SYNTHETIC_CUTOFF_AT = "2000-01-01T00:00:00Z"
SYNTHETIC_NOW = "2000-01-02T00:00:00Z"
SYNTHETIC_RELEASE_SHA = "eef92b89f168bbb016715f3486b9f6bf2824f53f"
SYNTHETIC_GENERATION_BINDING = "synthetic-dev-proof-not-runtime"
SYNTHETIC_IDENTITY = RuntimeEpochIdentity(
    epoch_id=SYNTHETIC_EPOCH_ID,
    cutoff_at=SYNTHETIC_CUTOFF_AT,
    contract_version=RUNTIME_EPOCH_CONTRACT_VERSION,
    reviewed_release_sha=SYNTHETIC_RELEASE_SHA,
    generation_binding=SYNTHETIC_GENERATION_BINDING,
)

_LIFECYCLE_SNAPSHOT_TABLES = (
    "setup_lifecycle_records",
    "setup_lifecycle_events",
    "setup_lifecycle_outcome_progress",
    "setup_outcome_analytics",
    "public_alert_events",
    "public_alert_delivery_parts",
    "telegram_alert_attempts",
    "scan_runs",
    "symbol_results",
    "setup_candidates",
)


def ensure_synthetic_test_epoch(connection: sqlite3.Connection):
    existing = load_active_runtime_epoch(connection)
    if existing is not None:
        return existing
    return initialize_runtime_epoch(connection, SYNTHETIC_IDENTITY, activated_at=SYNTHETIC_CUTOFF_AT)


def bootstrap_operational_test_database(path: Path | str):
    database_path = Path(path)
    connection = open_initialized_database(database_path)
    try:
        ensure_synthetic_test_epoch(connection)
        connection.commit()
    finally:
        connection.close()
    return database_path


def grant_synthetic_origin(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    run_id: str,
    now: str = SYNTHETIC_NOW,
):
    register_operational_run(connection, run_id=run_id, registered_at=now)
    return evaluate_symbol_origin(
        connection,
        run_id=run_id,
        symbol=symbol,
        evaluation_kind="live_scan",
        evaluation_completed_at=now,
        decision_cutoff_at=now,
        producer_observed_at=now,
    )


def attach_synthetic_origin_to_record(
    connection: sqlite3.Connection,
    record: SetupLifecycleRecord,
) -> SetupLifecycleRecord:
    epoch = load_active_runtime_epoch(connection)
    if epoch is None:
        return record
    if record.runtime_epoch_id and record.runtime_epoch_id != epoch.epoch_id:
        return record
    origin_id = record.creation_origin_id
    if not origin_id:
        decision = grant_synthetic_origin(
            connection,
            symbol=record.symbol,
            run_id=f"test-origin-{record.lifecycle_id}",
        )
        origin_id = decision.origin_id
    return record.model_copy(
        update={
            "runtime_epoch_id": epoch.epoch_id,
            "creation_origin_id": origin_id,
        }
    )


def stamp_sql_lifecycle_row(connection: sqlite3.Connection, *, lifecycle_id: str, symbol: str) -> None:
    epoch = load_active_runtime_epoch(connection)
    if epoch is None:
        return
    existing = connection.execute(
        "SELECT runtime_epoch_id, direction FROM setup_lifecycle_records WHERE lifecycle_id = ?",
        (lifecycle_id,),
    ).fetchone()
    if existing is None:
        return
    if existing["runtime_epoch_id"] in (None, ""):
        decision = grant_synthetic_origin(
            connection,
            symbol=symbol,
            run_id=f"test-sql-{lifecycle_id}",
        )
        if not decision.granted or not decision.origin_id:
            return
        connection.execute(
            """
            UPDATE setup_lifecycle_records
            SET runtime_epoch_id = ?, creation_origin_id = ?
            WHERE lifecycle_id = ? AND runtime_epoch_id IS NULL
            """,
            (epoch.epoch_id, decision.origin_id, lifecycle_id),
        )
    normalized_side = str(existing["direction"] or "long").strip().lower() or "long"
    connection.execute(
        """
        UPDATE public_alert_events
        SET origin_lifecycle_id = ?
        WHERE runtime_epoch_id = ?
          AND UPPER(symbol) = UPPER(?)
          AND lower(side) = ?
          AND origin_lifecycle_id LIKE 'epoch-origin::%'
        """,
        (lifecycle_id, epoch.epoch_id, symbol, normalized_side),
    )


def stamp_sql_public_event(
    connection: sqlite3.Connection,
    *,
    event_key: str,
    symbol: str,
    side: str,
    event_type: str,
    status: str,
    timestamp: str,
) -> None:
    epoch = load_active_runtime_epoch(connection)
    if epoch is None:
        return
    normalized_symbol = str(symbol).upper()
    normalized_side = str(side or "long").strip().lower() or "long"
    normalized_type = normalize_public_event_type(event_type)
    if normalized_type not in PUBLIC_EVENT_FAMILY_TYPES:
        normalized_type = "initial_watchlist"
    owned = connection.execute(
        """
        SELECT lifecycle_id FROM setup_lifecycle_records
        WHERE UPPER(symbol) = ? AND lower(direction) = ? AND runtime_epoch_id = ?
        ORDER BY CASE WHEN is_current = 1 THEN 0 ELSE 1 END, last_seen_at DESC
        LIMIT 1
        """,
        (normalized_symbol, normalized_side, epoch.epoch_id),
    ).fetchone()
    if owned is not None:
        origin_lifecycle_id = str(owned["lifecycle_id"] if "lifecycle_id" in owned.keys() else owned[0])
    else:
        origin_lifecycle_id = f"epoch-origin::{normalized_symbol}::{normalized_side}"
    existing_life = connection.execute(
        "SELECT lifecycle_id FROM setup_lifecycle_records WHERE lifecycle_id = ?",
        (origin_lifecycle_id,),
    ).fetchone()
    if existing_life is None:
        decision = grant_synthetic_origin(
            connection,
            symbol=normalized_symbol,
            run_id=f"test-stamp-{origin_lifecycle_id}",
            now=SYNTHETIC_NOW,
        )
        if decision.granted and decision.origin_id:
            connection.execute(
                """
                INSERT INTO setup_lifecycle_records (
                    lifecycle_id, symbol, mode, direction, current_state,
                    first_seen_at, last_seen_at, last_transition_at, is_current,
                    runtime_epoch_id, creation_origin_id
                ) VALUES (?, ?, 'epoch-origin', ?, 'WATCHLISTED', ?, ?, ?, 0, ?, ?)
                """,
                (
                    origin_lifecycle_id,
                    normalized_symbol,
                    normalized_side,
                    SYNTHETIC_NOW,
                    SYNTHETIC_NOW,
                    SYNTHETIC_NOW,
                    epoch.epoch_id,
                    decision.origin_id,
                ),
            )
    origin_root_event_id = None
    if normalized_type not in PUBLIC_ROOT_EVENT_TYPES:
        origin_root_event_id = _lookup_stamped_chain_starter_id(
            connection,
            origin_lifecycle_id=origin_lifecycle_id,
            epoch_id=epoch.epoch_id,
        )
        if origin_root_event_id is None and normalized_type not in PUBLIC_CHAIN_STARTER_EVENT_TYPES:
            origin_root_event_id = _seed_stamped_initial_watchlist_root(
                connection,
                origin_lifecycle_id=origin_lifecycle_id,
                symbol=normalized_symbol,
                side=normalized_side,
                epoch_id=epoch.epoch_id,
                timestamp=timestamp,
            )
    if normalized_type in PUBLIC_ROOT_EVENT_TYPES:
        origin_root_event_id = None
    delivery_state = "SENT" if str(status).strip().upper() == "SENT" else "PENDING"
    sent_at = timestamp if delivery_state == "SENT" else None
    completed_at = timestamp if delivery_state == "SENT" else None
    connection.execute(
        """
        INSERT OR IGNORE INTO public_alert_events (
            canonical_plan_id, event_type, event_key, symbol, side, status,
            reserved_at, sent_at, created_at, updated_at, runtime_epoch_id,
            origin_lifecycle_id, origin_root_event_id, delivery_state, completed_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_key,
            normalized_type,
            event_key,
            normalized_symbol,
            normalized_side,
            status,
            timestamp,
            sent_at,
            timestamp,
            timestamp,
            epoch.epoch_id,
            origin_lifecycle_id,
            origin_root_event_id,
            delivery_state,
            completed_at,
        ),
    )
    connection.execute(
        """
        UPDATE public_alert_events
        SET origin_lifecycle_id = COALESCE(NULLIF(origin_lifecycle_id, ''), ?),
            event_type = CASE
                WHEN event_type IS NULL OR event_type = '' THEN ?
                ELSE event_type
            END,
            origin_root_event_id = CASE
                WHEN origin_root_event_id IS NULL THEN ?
                ELSE origin_root_event_id
            END
        WHERE event_key = ?
          AND runtime_epoch_id = ?
        """,
        (
            origin_lifecycle_id,
            normalized_type,
            origin_root_event_id,
            event_key,
            epoch.epoch_id,
        ),
    )
    event = connection.execute(
        "SELECT id FROM public_alert_events WHERE event_key = ?",
        (event_key,),
    ).fetchone()
    if event is None:
        return
    event_id = int(event["id"] if hasattr(event, "keys") and "id" in event.keys() else event[0])
    attempt = connection.execute(
        """
        SELECT id FROM telegram_alert_attempts
        WHERE public_watchlist_event_key = ?
          AND lower(telegram_status) NOT IN ('blocked', 'skipped', 'failed')
        ORDER BY id DESC
        LIMIT 1
        """,
        (event_key,),
    ).fetchone()
    if attempt is None:
        return
    attempt_id = int(attempt["id"] if hasattr(attempt, "keys") and "id" in attempt.keys() else attempt[0])
    bind_canonical_reservation_attempt(
        connection,
        event_id=event_id,
        attempt_id=attempt_id,
    )


def register_synthetic_scan_run(database_path: Path | str, run_id: str, *, registered_at: str = SYNTHETIC_NOW) -> None:
    """DEV-only: persist a current-epoch operational run for diagnostic compaction tests."""

    path = Path(database_path)
    connection = open_initialized_database(path)
    connection.close()
    register_operational_scan_run(
        path,
        run_id=run_id,
        registered_at=registered_at,
        expected_identity=SYNTHETIC_IDENTITY,
    )


def _lookup_stamped_chain_starter_id(
    connection: sqlite3.Connection,
    *,
    origin_lifecycle_id: str,
    epoch_id: str,
) -> int | None:
    row = connection.execute(
        """
        SELECT id FROM public_alert_events
        WHERE origin_lifecycle_id = ?
          AND runtime_epoch_id = ?
          AND event_type IN ('initial_watchlist', 'setup_triggered', 'signal_confirmed')
        ORDER BY
          CASE event_type
            WHEN 'initial_watchlist' THEN 0
            WHEN 'setup_triggered' THEN 1
            ELSE 2
          END,
          id ASC
        LIMIT 1
        """,
        (origin_lifecycle_id, epoch_id),
    ).fetchone()
    if row is None:
        return None
    return int(row[0] if not hasattr(row, "keys") else row["id"])


def _seed_stamped_initial_watchlist_root(
    connection: sqlite3.Connection,
    *,
    origin_lifecycle_id: str,
    symbol: str,
    side: str,
    epoch_id: str,
    timestamp: str,
) -> int | None:
    event_key = f"stamp-root|{origin_lifecycle_id}|initial_watchlist"
    connection.execute(
        """
        INSERT OR IGNORE INTO public_alert_events (
            canonical_plan_id, event_type, event_key, symbol, side, status,
            reserved_at, sent_at, created_at, updated_at, runtime_epoch_id,
            origin_lifecycle_id, origin_root_event_id, delivery_state, completed_at
        ) VALUES (?, 'initial_watchlist', ?, ?, ?, 'SENT', ?, ?, ?, ?, ?, ?, NULL, 'SENT', ?)
        """,
        (
            origin_lifecycle_id,
            event_key,
            symbol,
            side,
            timestamp,
            timestamp,
            timestamp,
            timestamp,
            epoch_id,
            origin_lifecycle_id,
            timestamp,
        ),
    )
    return _lookup_stamped_chain_starter_id(
        connection,
        origin_lifecycle_id=origin_lifecycle_id,
        epoch_id=epoch_id,
    )


def snapshot_tables(connection: sqlite3.Connection) -> dict[str, tuple[tuple[Any, ...], ...]]:
    snapshots: dict[str, tuple[tuple[Any, ...], ...]] = {}
    for table in _LIFECYCLE_SNAPSHOT_TABLES:
        present = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if present is None:
            continue
        rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
        snapshots[table] = tuple(tuple(row) for row in rows)
    return snapshots


def assert_legacy_sent_consumption_frozen(
    *,
    before: dict[str, tuple[tuple[Any, ...], ...]],
    after: dict[str, tuple[tuple[Any, ...], ...]],
    extra_attempts: Sequence[tuple[Any, ...]],
) -> None:
    """Legacy SENT events stay consumed; diagnostic audit rows may appear."""

    assert after["public_alert_events"] == before["public_alert_events"]
    for row in before["telegram_alert_attempts"]:
        assert row in after["telegram_alert_attempts"]
    operational = {"pending", "retryable", "in_flight", "uncertain", "sent"}
    operational_delivery = {"PENDING", "RETRYABLE", "IN_FLIGHT", "UNCERTAIN", "SENT"}
    for status, delivery_state in extra_attempts:
        assert str(status or "").strip().lower() not in operational
        assert str(delivery_state or "N/A").strip().upper() not in operational_delivery


def seed_legacy_lifecycle(
    connection: sqlite3.Connection,
    *,
    lifecycle_id: str,
    symbol: str,
    mode: str = "swing",
    direction: str = "long",
    current_state: str,
    last_seen_at: str = "2026-09-09T15:37:42Z",
    cooldown_until: str | None = None,
    structural_anchor: str = "N/A",
    entry_low: str = "100",
    entry_high: str = "102",
    stop_loss: str = "95",
    tp1: str = "110",
    tp2: str = "115",
    tp3: str = "120",
    rr: str = "3.0",
    confirmation_count: int = 2,
) -> None:
    connection.execute(
        """
        INSERT INTO setup_lifecycle_records (
            lifecycle_id, symbol, mode, direction, current_state, previous_state,
            first_seen_at, last_seen_at, last_transition_at, is_current,
            entry_low, entry_high, stop_loss, tp1, tp2, tp3, rr,
            cooldown_until, structural_anchor, confirmation_count,
            required_confirmation_cycles
        ) VALUES (?, ?, ?, ?, ?, 'N/A', ?, ?, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 2)
        """,
        (
            lifecycle_id,
            symbol.upper(),
            mode.lower(),
            direction.lower(),
            current_state,
            last_seen_at,
            last_seen_at,
            last_seen_at,
            entry_low,
            entry_high,
            stop_loss,
            tp1,
            tp2,
            tp3,
            rr,
            cooldown_until,
            structural_anchor,
            confirmation_count,
        ),
    )


def seed_legacy_public_event(
    connection: sqlite3.Connection,
    *,
    event_key: str,
    symbol: str = "BTCUSDT",
    status: str = "SENT",
    delivery_state: str = "SENT",
    payload_text: str = "legacy payload",
    message_hash: str = "legacy-hash",
    created_at: str = "2026-09-09T15:37:42Z",
    origin_lifecycle_id: str | None = None,
) -> int:
    columns = {
        str(row[1])
        for row in connection.execute("PRAGMA table_info(public_alert_events)")
    }
    fields = [
        "canonical_plan_id",
        "event_type",
        "event_key",
        "symbol",
        "side",
        "status",
        "reserved_at",
        "sent_at",
        "delivery_state",
        "payload_text",
        "message_hash",
        "created_at",
        "updated_at",
    ]
    values: list[Any] = [
        "legacy-plan",
        "initial_watchlist",
        event_key,
        symbol.upper(),
        "long",
        status,
        created_at,
        created_at if delivery_state == "SENT" else None,
        delivery_state,
        payload_text,
        message_hash,
        created_at,
        created_at,
    ]
    if "origin_lifecycle_id" in columns:
        fields.append("origin_lifecycle_id")
        values.append(origin_lifecycle_id)
    placeholders = ", ".join("?" for _ in fields)
    cursor = connection.execute(
        f"INSERT INTO public_alert_events ({', '.join(fields)}) VALUES ({placeholders})",
        values,
    )
    return int(cursor.lastrowid)


def seed_legacy_attempt(
    connection: sqlite3.Connection,
    *,
    signal_id: str,
    event_key: str,
    telegram_status: str = "sent",
    delivery_state: str = "SENT",
    attempted_at: str = "2026-09-09T15:37:42Z",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO telegram_alert_attempts (
            signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
            sent_at, attempted_at, telegram_status, message_hash,
            attempted_alert_type, public_watchlist_plan_id,
            public_watchlist_event_key, public_alert_event_type,
            delivery_state, delivery_part_count
        ) VALUES (
            ?, 'BTCUSDT', 'long', 'WATCHLISTED', 'WATCHLIST', 'WATCHLISTED',
            ?, ?, ?, 'legacy-hash', 'WATCHLIST', 'legacy-plan', ?,
            'initial_watchlist', ?, 1
        )
        """,
        (
            signal_id,
            attempted_at if telegram_status == "sent" else None,
            attempted_at,
            telegram_status,
            event_key,
            delivery_state,
        ),
    )
    return int(cursor.lastrowid)
