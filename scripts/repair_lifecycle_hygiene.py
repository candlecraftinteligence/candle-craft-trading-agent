from __future__ import annotations

import argparse
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.analytics.public_signal_quality import normalize_grade
from app.data.dtos import NA
from app.lifecycle.eligibility import (
    active_signal_eligible,
    has_no_public_blockers,
    has_valid_direction,
    has_valid_trade_map,
    is_active_signal_state,
    is_terminal_state,
    public_watchlist_eligible,
)
from app.storage.database import connect_database, initialize_database, open_read_only_database
from app.storage.maintenance import create_verified_backup

WARNING = (
    "WARNING: This script is for runtime DB hygiene after backup and tested merge. "
    "Dry-run is the default and no rows are ever deleted."
)


@dataclass
class RepairSummary:
    rows_scanned: int = 0
    rows_to_archive: int = 0
    rows_archived: int = 0
    excluded_from_watchlist: int = 0
    missing_trade_map: int = 0
    terminal_states: int = 0
    reject_grade: int = 0
    direction_na: int = 0
    telegram_bad_sent_at: int = 0
    telegram_sent_at_cleared: int = 0
    sample_lifecycle_ids: list[str] = field(default_factory=list)
    sample_telegram_ids: list[int] = field(default_factory=list)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    db_path = Path(args.database_path)
    if not db_path.exists():
        raise SystemExit(f"Database does not exist: {db_path}")

    print(WARNING)
    if args.apply and not args.no_backup:
        if args.archive_directory is None:
            raise SystemExit("--archive-directory is required for the automatic verified backup on --apply.")
        backup = _create_backup(
            db_path,
            Path(args.archive_directory),
            allow_unsafe_temp=args.allow_unsafe_temp,
        )
        print(f"Verified backup created: {backup['snapshot_path']}")
        print(f"Backup manifest created: {backup['manifest_path']}")

    connection = (
        connect_database(db_path)
        if args.apply
        else open_read_only_database(db_path)
    )
    with connection:
        if args.apply:
            initialize_database(connection)
        summary = repair_database(
            connection,
            apply=args.apply,
            limit=args.limit,
            verbose=args.verbose,
        )
        if args.apply:
            connection.commit()

    _print_summary(summary, applied=args.apply)
    return 0


def repair_database(
    connection: sqlite3.Connection,
    *,
    apply: bool = False,
    limit: int | None = None,
    verbose: bool = False,
) -> RepairSummary:
    summary = RepairSummary()
    _inspect_lifecycle_records(connection, summary, apply=apply, limit=limit, verbose=verbose)
    _repair_telegram_attempt_timestamps(connection, summary, apply=apply, limit=limit, verbose=verbose)
    return summary


def _inspect_lifecycle_records(
    connection: sqlite3.Connection,
    summary: RepairSummary,
    *,
    apply: bool,
    limit: int | None,
    verbose: bool,
) -> None:
    if not _table_exists(connection, "setup_lifecycle_records"):
        return
    columns = _table_columns(connection, "setup_lifecycle_records")
    select_columns = ["rowid AS _rowid", *columns]
    limit_clause = " LIMIT ?" if limit is not None else ""
    params: tuple[Any, ...] = (limit,) if limit is not None else ()
    rows = connection.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM setup_lifecycle_records
        ORDER BY rowid ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    now = _now()
    archive_ids: list[str] = []
    for row in rows:
        record = dict(row)
        summary.rows_scanned += 1
        if not public_watchlist_eligible(record):
            summary.excluded_from_watchlist += 1
        if not has_valid_trade_map(record):
            summary.missing_trade_map += 1
        if is_terminal_state(record.get("current_state")):
            summary.terminal_states += 1
        if normalize_grade(record.get("quality_grade_current")) == "Reject":
            summary.reject_grade += 1
        if not has_valid_direction(record):
            summary.direction_na += 1

        if _should_archive_lifecycle_record(record):
            summary.rows_to_archive += 1
            lifecycle_id = str(record.get("lifecycle_id"))
            archive_ids.append(lifecycle_id)
            if verbose and len(summary.sample_lifecycle_ids) < 10:
                summary.sample_lifecycle_ids.append(lifecycle_id)

    if apply and archive_ids:
        placeholders = ",".join("?" for _ in archive_ids)
        cursor = connection.execute(
            f"""
            UPDATE setup_lifecycle_records
            SET archived_at = ?
            WHERE archived_at IS NULL
              AND lifecycle_id IN ({placeholders})
            """,
            (now, *archive_ids),
        )
        summary.rows_archived = max(0, cursor.rowcount)


def _repair_telegram_attempt_timestamps(
    connection: sqlite3.Connection,
    summary: RepairSummary,
    *,
    apply: bool,
    limit: int | None,
    verbose: bool,
) -> None:
    if not _table_exists(connection, "telegram_alert_attempts"):
        return
    columns = _table_columns(connection, "telegram_alert_attempts")
    if "sent_at" not in columns or "telegram_status" not in columns:
        return
    if apply and "attempted_at" not in columns:
        connection.execute("ALTER TABLE telegram_alert_attempts ADD COLUMN attempted_at TEXT NOT NULL DEFAULT 'N/A'")
        columns = _table_columns(connection, "telegram_alert_attempts")

    limit_clause = " LIMIT ?" if limit is not None else ""
    params: tuple[Any, ...] = (limit,) if limit is not None else ()
    rows = connection.execute(
        f"""
        SELECT id, telegram_status, sent_at, {'attempted_at' if 'attempted_at' in columns else "'N/A' AS attempted_at"}
        FROM telegram_alert_attempts
        WHERE telegram_status IN ('blocked', 'skipped', 'failed')
          AND sent_at IS NOT NULL
          AND sent_at NOT IN ('', 'N/A')
        ORDER BY id ASC
        {limit_clause}
        """,
        params,
    ).fetchall()
    summary.telegram_bad_sent_at = len(rows)
    summary.sample_telegram_ids.extend(int(row["id"]) for row in rows[:10])
    if not apply:
        summary.telegram_sent_at_cleared = len(rows)
    if apply and rows:
        if "attempted_at" in columns:
            connection.execute(
                """
                UPDATE telegram_alert_attempts
                SET attempted_at = sent_at
                WHERE telegram_status IN ('blocked', 'skipped', 'failed')
                  AND sent_at IS NOT NULL
                  AND sent_at NOT IN ('', 'N/A')
                  AND (attempted_at IS NULL OR attempted_at = '' OR attempted_at = 'N/A')
                """
            )
        cursor = connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET sent_at = NULL
            WHERE telegram_status IN ('blocked', 'skipped', 'failed')
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
            """
        )
        summary.telegram_sent_at_cleared = max(0, cursor.rowcount)


def _should_archive_lifecycle_record(record: dict[str, Any]) -> bool:
    if _display(record.get("archived_at")) != NA:
        return False
    if active_signal_eligible(record):
        return False
    if is_active_signal_state(record.get("current_state")):
        return False
    if is_terminal_state(record.get("current_state")):
        return True
    if normalize_grade(record.get("quality_grade_current")) == "Reject":
        return True
    if not has_valid_direction(record):
        return True
    if not has_valid_trade_map(record):
        return True
    if not has_no_public_blockers(record):
        return True
    return False


def _create_backup(
    db_path: Path,
    archive_directory: Path,
    *,
    allow_unsafe_temp: bool = False,
) -> dict[str, Any]:
    return create_verified_backup(
        db_path,
        archive_directory,
        label="pre-lifecycle-hygiene",
        allow_unsafe_temp=allow_unsafe_temp,
    )


def _print_summary(summary: RepairSummary, *, applied: bool) -> None:
    verb = "changed" if applied else "would change"
    print(f"Rows scanned: {summary.rows_scanned}")
    print(f"Rows that would be archived: {summary.rows_to_archive}")
    print(f"Rows archived: {summary.rows_archived}")
    print(f"Rows excluded from watchlist: {summary.excluded_from_watchlist}")
    print(f"Rows with missing trade map: {summary.missing_trade_map}")
    print(f"Rows with terminal states: {summary.terminal_states}")
    print(f"Rows with Reject grade: {summary.reject_grade}")
    print(f"Rows with direction=n/a: {summary.direction_na}")
    print(f"Telegram blocked/skipped/failed rows with sent_at populated: {summary.telegram_bad_sent_at}")
    print(f"Telegram sent_at rows {verb}: {summary.telegram_sent_at_cleared}")
    if summary.sample_lifecycle_ids:
        print(f"Sample lifecycle IDs: {', '.join(summary.sample_lifecycle_ids)}")
    if summary.sample_telegram_ids:
        print(f"Sample Telegram attempt IDs: {', '.join(str(value) for value in summary.sample_telegram_ids)}")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Repair lifecycle/watchlist and Telegram alert audit hygiene.")
    parser.add_argument("--database-path", required=True, help="SQLite database path to inspect or repair.")
    parser.add_argument("--apply", action="store_true", help="Apply repairs. Default is dry-run.")
    parser.add_argument("--backup", action="store_true", help="Accepted for compatibility; backups are automatic on --apply.")
    parser.add_argument("--no-backup", action="store_true", help="Explicitly skip the automatic verified backup on --apply.")
    parser.add_argument(
        "--archive-directory",
        type=Path,
        help="Explicit durable directory for the verified pre-repair snapshot.",
    )
    parser.add_argument(
        "--allow-unsafe-temp",
        action="store_true",
        help="Allow a temporary archive directory for controlled testing only.",
    )
    parser.add_argument("--limit", type=int, help="Limit rows scanned for inspection/testing.")
    parser.add_argument("--verbose", action="store_true", help="Print sample row identifiers.")
    return parser.parse_args(argv)


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table_name: str) -> list[str]:
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()]


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _display(value: Any) -> str:
    if value is None or value == "" or value == NA:
        return NA
    text = " ".join(str(value).split())
    return text if text and text.upper() != NA else NA


if __name__ == "__main__":
    raise SystemExit(main())
