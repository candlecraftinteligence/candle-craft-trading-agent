from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import AbstractContextManager
from pathlib import Path
from typing import Any

from app.data.dtos import NA
from app.lifecycle.models import (
    SetupLifecycleEvent,
    SetupLifecycleRecord,
    SetupLifecycleState,
    SetupTransitionReason,
)
from app.storage.database import DEFAULT_DATABASE_PATH, StorageError, open_initialized_database


class SQLiteSetupLifecycleRepository(AbstractContextManager["SQLiteSetupLifecycleRepository"]):
    def __init__(self, database_path: Path | str = DEFAULT_DATABASE_PATH) -> None:
        self.database_path = Path(database_path)
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> SQLiteSetupLifecycleRepository:
        self.connection = open_initialized_database(self.database_path)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self.connection is None:
            return
        if exc_type is None:
            self.connection.commit()
        self.connection.close()
        self.connection = None

    def get_record(self, *, symbol: str, mode: str, direction: str) -> SetupLifecycleRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM setup_lifecycle_records
            WHERE symbol = ? AND mode = ? AND direction = ?
            """,
            (_symbol(symbol), _identity_text(mode), _identity_text(direction)),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def get_record_by_lifecycle_id(self, lifecycle_id: str) -> SetupLifecycleRecord | None:
        normalized = _lifecycle_id_text(lifecycle_id)
        if normalized == NA:
            return None
        row = self._connection.execute(
            """
            SELECT * FROM setup_lifecycle_records
            WHERE lifecycle_id = ?
            """,
            (normalized,),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def list_records_for_symbol(
        self,
        *,
        symbol: str,
        direction: str | None = None,
    ) -> tuple[SetupLifecycleRecord, ...]:
        normalized_symbol = _symbol(symbol)
        if normalized_symbol == NA:
            return ()
        params: list[Any] = [normalized_symbol]
        direction_clause = ""
        normalized_direction = _identity_text(direction)
        if normalized_direction != NA:
            direction_clause = "AND direction = ?"
            params.append(normalized_direction)
        rows = self._connection.execute(
            f"""
            SELECT * FROM setup_lifecycle_records
            WHERE symbol = ?
              {direction_clause}
            ORDER BY last_seen_at DESC, lifecycle_id ASC
            """,
            params,
        ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def get_records_for_symbols(self, symbols: Sequence[str]) -> tuple[SetupLifecycleRecord, ...]:
        normalized = tuple(dict.fromkeys(_symbol(symbol) for symbol in symbols if _symbol(symbol) != NA))
        if not normalized:
            return ()
        placeholders = ",".join("?" for _ in normalized)
        rows = self._connection.execute(
            f"""
            SELECT * FROM setup_lifecycle_records
            WHERE symbol IN ({placeholders})
            ORDER BY last_seen_at DESC
            """,
            normalized,
        ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def upsert_record(self, record: SetupLifecycleRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO setup_lifecycle_records (
                lifecycle_id, symbol, mode, direction, current_state, previous_state,
                first_seen_at, last_seen_at, last_transition_at, failed_gate,
                readiness_score, quality_score, edge_score, regime_state, action_label,
                invalidation_reason, cooldown_until, archived_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(lifecycle_id) DO UPDATE SET
                symbol = excluded.symbol,
                mode = excluded.mode,
                direction = excluded.direction,
                current_state = excluded.current_state,
                previous_state = excluded.previous_state,
                first_seen_at = excluded.first_seen_at,
                last_seen_at = excluded.last_seen_at,
                last_transition_at = excluded.last_transition_at,
                failed_gate = excluded.failed_gate,
                readiness_score = excluded.readiness_score,
                quality_score = excluded.quality_score,
                edge_score = excluded.edge_score,
                regime_state = excluded.regime_state,
                action_label = excluded.action_label,
                invalidation_reason = excluded.invalidation_reason,
                cooldown_until = excluded.cooldown_until,
                archived_at = excluded.archived_at
            """,
            _record_params(record),
        )

    def insert_event(self, event: SetupLifecycleEvent) -> None:
        self._connection.execute(
            """
            INSERT INTO setup_lifecycle_events (
                lifecycle_id, timestamp, symbol, from_state, to_state, reason,
                scan_run_id, readiness_score, quality_score, failed_gate, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _event_params(event),
        )

    def list_events(
        self,
        *,
        lifecycle_id: str | None = None,
        symbol: str | None = None,
        limit: int | None = None,
    ) -> tuple[SetupLifecycleEvent, ...]:
        clauses: list[str] = []
        params: list[Any] = []
        if lifecycle_id is not None:
            clauses.append("lifecycle_id = ?")
            params.append(lifecycle_id)
        if symbol is not None:
            clauses.append("symbol = ?")
            params.append(_symbol(symbol))
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT * FROM setup_lifecycle_events
            {where}
            ORDER BY timestamp ASC, event_id ASC
        """
        if limit is not None:
            sql += " LIMIT ?"
            params.append(max(1, int(limit)))
        rows = self._connection.execute(sql, params).fetchall()
        return tuple(_event_from_row(row) for row in rows)

    def reset(self) -> None:
        self._connection.execute("DELETE FROM setup_lifecycle_events")
        self._connection.execute("DELETE FROM setup_lifecycle_records")

    @property
    def _connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise StorageError("Lifecycle repository is not open.")
        return self.connection


def _record_params(record: SetupLifecycleRecord) -> tuple[Any, ...]:
    return (
        record.lifecycle_id,
        record.symbol,
        _identity_text(record.mode),
        _identity_text(record.direction),
        record.current_state.value,
        record.previous_state.value if record.previous_state is not None else NA,
        record.first_seen_at,
        record.last_seen_at,
        record.last_transition_at,
        record.failed_gate,
        record.readiness_score,
        record.quality_score,
        record.edge_score,
        record.regime_state,
        record.action_label,
        record.invalidation_reason,
        record.cooldown_until,
        record.archived_at,
    )


def _event_params(event: SetupLifecycleEvent) -> tuple[Any, ...]:
    return (
        event.lifecycle_id,
        event.timestamp,
        event.symbol,
        event.from_state.value if event.from_state is not None else NA,
        event.to_state.value,
        event.reason.value,
        event.scan_run_id,
        event.readiness_score,
        event.quality_score,
        event.failed_gate,
        event.notes,
    )


def _record_from_row(row: sqlite3.Row) -> SetupLifecycleRecord:
    previous_state = _state_or_none(row["previous_state"])
    return SetupLifecycleRecord(
        lifecycle_id=row["lifecycle_id"],
        symbol=row["symbol"],
        mode=row["mode"],
        direction=row["direction"],
        current_state=SetupLifecycleState(row["current_state"]),
        previous_state=previous_state,
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        last_transition_at=row["last_transition_at"],
        failed_gate=row["failed_gate"],
        readiness_score=int(row["readiness_score"] or 0),
        quality_score=int(row["quality_score"] or 0),
        edge_score=row["edge_score"],
        regime_state=row["regime_state"],
        action_label=row["action_label"],
        invalidation_reason=row["invalidation_reason"],
        cooldown_until=row["cooldown_until"],
        archived_at=row["archived_at"],
    )


def _event_from_row(row: sqlite3.Row) -> SetupLifecycleEvent:
    reason = _reason_from_value(row["reason"])
    return SetupLifecycleEvent(
        lifecycle_id=row["lifecycle_id"],
        timestamp=row["timestamp"],
        symbol=row["symbol"],
        from_state=_state_or_none(row["from_state"]),
        to_state=SetupLifecycleState(row["to_state"]),
        reason=reason,
        scan_run_id=row["scan_run_id"],
        readiness_score=int(row["readiness_score"] or 0),
        quality_score=int(row["quality_score"] or 0),
        failed_gate=row["failed_gate"],
        notes=row["notes"],
    )


def _state_or_none(value: Any) -> SetupLifecycleState | None:
    if value in (None, "", NA):
        return None
    return SetupLifecycleState(str(value))


def _reason_from_value(value: Any) -> SetupTransitionReason:
    text = str(value)
    for reason in SetupTransitionReason:
        if reason.value == text or reason.name == text:
            return reason
    return SetupTransitionReason.NO_CHANGE


def _symbol(value: str) -> str:
    text = str(value).strip().upper()
    return text if text else NA


def _identity_text(value: str | None) -> str:
    if value is None:
        return NA
    text = str(value).strip().lower()
    return text if text else NA


def _lifecycle_id_text(value: str | None) -> str:
    if value is None:
        return NA
    text = str(value).strip()
    return text if text else NA


__all__ = ["SQLiteSetupLifecycleRepository"]
