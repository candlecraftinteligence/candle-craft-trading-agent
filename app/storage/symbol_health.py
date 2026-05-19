from __future__ import annotations

import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from contextlib import closing
from pathlib import Path
from typing import Any

from app.analytics.symbol_health import (
    DEFAULT_MAX_TIMEOUT_STRIKES,
    DEFAULT_SYMBOL_COOLDOWN_MINUTES,
    SymbolHealthRecord,
    SymbolPriorityPlan,
    build_symbol_health_summary,
    now_utc_iso,
    update_symbol_health_records,
)
from app.data.dtos import NA
from app.storage.database import DEFAULT_DATABASE_PATH, StorageError, open_initialized_database


def load_symbol_health_records(
    database_path: Path | str = DEFAULT_DATABASE_PATH,
    symbols: Sequence[str] | None = None,
) -> dict[str, SymbolHealthRecord]:
    try:
        with closing(open_initialized_database(database_path)) as connection:
            return _load_symbol_health_records(connection, symbols)
    except sqlite3.Error as exc:
        raise StorageError(f"Unable to read symbol health database: {database_path}") from exc


def save_symbol_health_records(
    database_path: Path | str,
    records: Mapping[str, SymbolHealthRecord],
) -> None:
    try:
        with closing(open_initialized_database(database_path)) as connection:
            _upsert_symbol_health_records(connection, records.values())
            connection.commit()
    except sqlite3.Error as exc:
        raise StorageError(f"Unable to store symbol health database: {database_path}") from exc


def update_symbol_health_for_result(
    database_path: Path | str,
    result: Any,
    *,
    plan: SymbolPriorityPlan | None = None,
    cooldown_minutes: float = DEFAULT_SYMBOL_COOLDOWN_MINUTES,
    max_timeout_strikes: int = DEFAULT_MAX_TIMEOUT_STRIKES,
    now: str | None = None,
    enabled: bool = True,
) -> tuple[dict[str, SymbolHealthRecord], dict[str, Any]]:
    timestamp = now or now_utc_iso()
    symbols = _health_symbols_for_result(result, plan)
    priority_by_symbol = plan.priority_by_symbol() if plan is not None else {}
    try:
        with closing(open_initialized_database(database_path)) as connection:
            existing = _load_symbol_health_records(connection, symbols)
            updated = update_symbol_health_records(
                existing,
                tuple(getattr(result, "results", ())),
                priority_by_symbol=priority_by_symbol,
                now=timestamp,
                cooldown_minutes=cooldown_minutes,
                max_timeout_strikes=max_timeout_strikes,
            )
            _upsert_symbol_health_records(connection, updated.values())
            connection.commit()
    except sqlite3.Error as exc:
        raise StorageError(f"Unable to update symbol health database: {database_path}") from exc

    summary = build_symbol_health_summary(
        enabled=enabled,
        plan=plan,
        records=updated,
        symbol_results=tuple(getattr(result, "results", ())),
    )
    return updated, summary


def symbol_health_records_from_payload(payload: Mapping[str, Any] | None) -> dict[str, SymbolHealthRecord]:
    if not isinstance(payload, Mapping):
        return {}
    raw_records = payload.get("records")
    if not isinstance(raw_records, Mapping):
        return {}
    records: dict[str, SymbolHealthRecord] = {}
    for symbol, raw_record in raw_records.items():
        if not isinstance(raw_record, Mapping):
            continue
        try:
            record = SymbolHealthRecord.model_validate(raw_record)
        except Exception:
            continue
        records[str(symbol).upper()] = record
    return records


def _load_symbol_health_records(
    connection: sqlite3.Connection,
    symbols: Sequence[str] | None = None,
) -> dict[str, SymbolHealthRecord]:
    if symbols is None:
        rows = connection.execute("SELECT * FROM symbol_health").fetchall()
    else:
        normalized = tuple(dict.fromkeys(_symbol(symbol) for symbol in symbols if _symbol(symbol) != NA))
        if not normalized:
            return {}
        placeholders = ",".join("?" for _ in normalized)
        rows = connection.execute(
            f"SELECT * FROM symbol_health WHERE symbol IN ({placeholders})",
            normalized,
        ).fetchall()
    records: dict[str, SymbolHealthRecord] = {}
    for row in rows:
        record = _symbol_health_from_row(row)
        records[record.symbol] = record
    return records


def _upsert_symbol_health_records(
    connection: sqlite3.Connection,
    records: Iterable[SymbolHealthRecord],
) -> None:
    rows = tuple(records)
    if not rows:
        return
    connection.executemany(
        """
        INSERT INTO symbol_health (
            symbol, successful_scans, timeout_count, data_issue_count,
            average_runtime_sec, last_success_at, last_timeout_at,
            current_health_score, cooldown_until, timeout_strikes,
            last_priority_rank, last_prioritized_at, last_scanned_at,
            last_data_issue_at, last_display_bucket, last_readiness_label,
            useful_scan_count, rejected_count, last_rejected_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(symbol) DO UPDATE SET
            successful_scans = excluded.successful_scans,
            timeout_count = excluded.timeout_count,
            data_issue_count = excluded.data_issue_count,
            average_runtime_sec = excluded.average_runtime_sec,
            last_success_at = excluded.last_success_at,
            last_timeout_at = excluded.last_timeout_at,
            current_health_score = excluded.current_health_score,
            cooldown_until = excluded.cooldown_until,
            timeout_strikes = excluded.timeout_strikes,
            last_priority_rank = excluded.last_priority_rank,
            last_prioritized_at = excluded.last_prioritized_at,
            last_scanned_at = excluded.last_scanned_at,
            last_data_issue_at = excluded.last_data_issue_at,
            last_display_bucket = excluded.last_display_bucket,
            last_readiness_label = excluded.last_readiness_label,
            useful_scan_count = excluded.useful_scan_count,
            rejected_count = excluded.rejected_count,
            last_rejected_at = excluded.last_rejected_at
        """,
        [_symbol_health_params(record) for record in rows],
    )


def _symbol_health_from_row(row: sqlite3.Row) -> SymbolHealthRecord:
    return SymbolHealthRecord(
        symbol=row["symbol"],
        successful_scans=int(row["successful_scans"] or 0),
        timeout_count=int(row["timeout_count"] or 0),
        data_issue_count=int(row["data_issue_count"] or 0),
        average_runtime_sec=float(row["average_runtime_sec"] or 0.0),
        last_success_at=row["last_success_at"],
        last_timeout_at=row["last_timeout_at"],
        current_health_score=int(row["current_health_score"] or 0),
        cooldown_until=row["cooldown_until"],
        timeout_strikes=int(row["timeout_strikes"] or 0),
        last_priority_rank=row["last_priority_rank"],
        last_prioritized_at=row["last_prioritized_at"],
        last_scanned_at=row["last_scanned_at"],
        last_data_issue_at=row["last_data_issue_at"],
        last_display_bucket=row["last_display_bucket"],
        last_readiness_label=row["last_readiness_label"],
        useful_scan_count=int(row["useful_scan_count"] or 0),
        rejected_count=int(row["rejected_count"] or 0),
        last_rejected_at=row["last_rejected_at"],
    )


def _symbol_health_params(record: SymbolHealthRecord) -> tuple[Any, ...]:
    return (
        record.symbol,
        record.successful_scans,
        record.timeout_count,
        record.data_issue_count,
        record.average_runtime_sec,
        record.last_success_at,
        record.last_timeout_at,
        record.current_health_score,
        record.cooldown_until,
        record.timeout_strikes,
        record.last_priority_rank,
        record.last_prioritized_at,
        record.last_scanned_at,
        record.last_data_issue_at,
        record.last_display_bucket,
        record.last_readiness_label,
        record.useful_scan_count,
        record.rejected_count,
        record.last_rejected_at,
    )


def _health_symbols_for_result(result: Any, plan: SymbolPriorityPlan | None) -> tuple[str, ...]:
    symbols: list[str] = []
    if plan is not None:
        symbols.extend(plan.original_symbols)
    config = getattr(result, "config", None)
    for item in getattr(config, "symbols", ()):
        symbols.append(_symbol(getattr(item, "symbol", item)))
    for symbol_result in getattr(result, "results", ()):
        symbols.append(_symbol(getattr(symbol_result, "symbol", NA)))
    return tuple(dict.fromkeys(symbol for symbol in symbols if symbol != NA))


def _symbol(value: Any) -> str:
    text = str(value).strip().upper()
    return text if text else NA


__all__ = [
    "load_symbol_health_records",
    "save_symbol_health_records",
    "symbol_health_records_from_payload",
    "update_symbol_health_for_result",
    "_load_symbol_health_records",
    "_upsert_symbol_health_records",
]
