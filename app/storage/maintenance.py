from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.data.dtos import NA
from app.storage.database import (
    SCHEMA_VERSION,
    WRITABLE_BUSY_TIMEOUT_MS,
    StorageError,
    connect_database,
    identify_schema_version,
    open_read_only_database,
)

TOOL_VERSION = "cci-sqlite-maintenance-v1"
MANIFEST_FORMAT_VERSION = 1
NOT_ENOUGH_DATA = "not enough data"
BACKUP_PAGE_BATCH = 256
BACKUP_SPACE_MARGIN_BYTES = 1_048_576

CORE_TABLES = (
    "scan_runs",
    "symbol_results",
    "setup_candidates",
    "replay_results",
    "setup_lifecycle_records",
    "setup_lifecycle_events",
    "telegram_alert_attempts",
)
DIAGNOSTIC_TABLES = (
    *CORE_TABLES,
    "setup_lifecycle_outcome_progress",
    "setup_outcome_analytics",
    "public_alert_events",
    "public_alert_delivery_parts",
    "symbol_health",
    "symbol_health_events",
)
CHECKPOINT_MODES = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}


class MaintenanceError(StorageError):
    """Raised when a SQLite maintenance operation cannot finish safely."""


@dataclass(frozen=True)
class WarningThresholds:
    low_free_bytes: int = 10 * 1024**3
    low_free_percent: float = 10.0
    large_wal_bytes: int = 1024**3
    rapid_growth_bytes_per_day: int = 1024**3
    integrity_max_age_hours: float = 168.0
    backup_max_age_hours: float = 24.0


def inspect_database(
    database_path: Path | str,
    *,
    thresholds: WarningThresholds | None = None,
    archive_directory: Path | str | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a non-mutating SQLite footprint, contents, and capacity report."""

    path = _existing_database_path(database_path)
    active_thresholds = thresholds or WarningThresholds()
    observed_at = _utc(now)
    with open_read_only_database(path, require_supported_schema=False) as connection:
        schema_version = identify_schema_version(connection)
        tables = _table_names(connection)
        page_count = _pragma_int(connection, "page_count")
        page_size = _pragma_int(connection, "page_size")
        freelist_count = _pragma_int(connection, "freelist_count")
        journal_mode = _database_journal_mode(path, connection)
        table_counts = _table_counts(connection, tables, DIAGNOSTIC_TABLES)
        largest_tables = _largest_tables(connection)
        scan_timestamps = _scan_timestamp_range(connection, tables)
        lifecycle_counts = {
            "records": table_counts.get("setup_lifecycle_records", NA),
            "events": table_counts.get("setup_lifecycle_events", NA),
            "outcome_progress": table_counts.get("setup_lifecycle_outcome_progress", NA),
            "outcomes": table_counts.get("setup_outcome_analytics", NA),
        }
        outbox_state_counts = _outbox_state_counts(connection, tables)

    sizes = sqlite_file_sizes(path)
    usage = shutil.disk_usage(path.parent)
    percent_free = (usage.free / usage.total * 100.0) if usage.total else 0.0
    logical_bytes = page_count * page_size
    freelist_percent = (freelist_count / page_count * 100.0) if page_count else 0.0
    archive_history = _archive_history(path, archive_directory)
    growth = _growth_estimate(archive_history)
    days_until_threshold = _days_until_capacity_warning(
        free_bytes=usage.free,
        total_bytes=usage.total,
        growth_bytes_per_day=growth.get("bytes_per_day"),
        thresholds=active_thresholds,
    )
    warnings = _diagnostic_warnings(
        sizes=sizes,
        free_bytes=usage.free,
        percent_free=percent_free,
        growth=growth,
        archive_history=archive_history,
        thresholds=active_thresholds,
        now=observed_at,
    )
    return {
        "tool_version": TOOL_VERSION,
        "inspection_mode": "read-only",
        "database_path": str(path),
        "observed_utc": _iso_utc(observed_at),
        "schema_version": schema_version,
        "supported_schema_version": SCHEMA_VERSION,
        "schema_status": _schema_status(schema_version),
        "journal_mode": journal_mode,
        "wal_checkpoint_state": "not requested (read-only inspection)",
        "page_count": page_count,
        "page_size": page_size,
        "logical_database_bytes": logical_bytes,
        "freelist_count": freelist_count,
        "freelist_percent": round(freelist_percent, 4),
        **sizes,
        "filesystem_total_bytes": usage.total,
        "filesystem_free_bytes": usage.free,
        "filesystem_percent_free": round(percent_free, 4),
        "largest_tables": largest_tables,
        "table_counts": table_counts,
        "scan_run_count": table_counts.get("scan_runs", NA),
        "lifecycle_counts": lifecycle_counts,
        "outbox_state_counts": outbox_state_counts,
        **scan_timestamps,
        "recent_daily_growth": growth,
        "estimated_days_until_warning_threshold": days_until_threshold,
        "backup_metadata": _archive_summary(archive_history, observed_at),
        "warning_thresholds": {
            "low_free_bytes": active_thresholds.low_free_bytes,
            "low_free_percent": active_thresholds.low_free_percent,
            "large_wal_bytes": active_thresholds.large_wal_bytes,
            "rapid_growth_bytes_per_day": active_thresholds.rapid_growth_bytes_per_day,
            "integrity_max_age_hours": active_thresholds.integrity_max_age_hours,
            "backup_max_age_hours": active_thresholds.backup_max_age_hours,
        },
        "warnings": warnings,
        "automatic_deletion": False,
    }


def check_database(database_path: Path | str, *, full: bool = False) -> dict[str, Any]:
    """Run quick or explicitly requested full integrity checks without writes."""

    path = _existing_database_path(database_path)
    pragma = "integrity_check" if full else "quick_check"
    with open_read_only_database(path) as connection:
        schema_version = identify_schema_version(connection)
        integrity_rows = [str(row[0]) for row in connection.execute(f"PRAGMA {pragma}").fetchall()]
        foreign_key_rows = _foreign_key_check(connection)
        tables = _table_names(connection)
        missing_core_tables = sorted(set(CORE_TABLES) - tables)
        page_count = _pragma_int(connection, "page_count")
        page_size = _pragma_int(connection, "page_size")
        freelist_count = _pragma_int(connection, "freelist_count")
        table_counts = _table_counts(connection, tables, DIAGNOSTIC_TABLES)
    integrity_ok = integrity_rows == ["ok"]
    foreign_keys_ok = not foreign_key_rows
    return {
        "tool_version": TOOL_VERSION,
        "check_mode": "full" if full else "quick",
        "database_path": str(path),
        "schema_version": schema_version,
        "supported_schema_version": SCHEMA_VERSION,
        "schema_status": _schema_status(schema_version),
        "integrity_pragma": pragma,
        "integrity_result": integrity_rows,
        "integrity_ok": integrity_ok,
        "foreign_key_check": foreign_key_rows,
        "foreign_key_ok": foreign_keys_ok,
        "missing_core_tables": missing_core_tables,
        "page_count": page_count,
        "page_size": page_size,
        "freelist_count": freelist_count,
        "table_counts": table_counts,
        "ok": integrity_ok and foreign_keys_ok and not missing_core_tables,
        "automatic_repair": False,
    }


def sqlite_file_sizes(database_path: Path | str) -> dict[str, int]:
    path = Path(database_path)
    main_size = _file_size(path)
    wal_size = _file_size(Path(f"{path}-wal"))
    shm_size = _file_size(Path(f"{path}-shm"))
    return {
        "database_size_bytes": main_size,
        "wal_size_bytes": wal_size,
        "shm_size_bytes": shm_size,
        "sqlite_footprint_bytes": main_size + wal_size + shm_size,
    }


def plan_backup(
    source_database: Path | str,
    archive_directory: Path | str,
    *,
    label: str | None = None,
    allow_unsafe_temp: bool = False,
    now: datetime | None = None,
    unique_suffix: str | None = None,
) -> dict[str, Any]:
    """Validate and describe a backup without creating any path or artifact."""

    source = _existing_database_path(source_database)
    archive = _archive_path(archive_directory)
    if archive.exists() and not archive.is_dir():
        raise MaintenanceError(f"Archive path is not a directory: {archive}")
    if not allow_unsafe_temp and _is_within(archive, Path(tempfile.gettempdir()).resolve()):
        raise MaintenanceError(
            f"Archive directory is inside an unsafe temporary directory: {archive}. "
            "Choose durable storage or explicitly allow the temporary path."
        )

    created = _utc(now)
    normalized_label = _safe_label(label)
    suffix = _safe_suffix(unique_suffix or uuid4().hex[:12])
    timestamp = created.strftime("%Y%m%dT%H%M%SZ")
    with open_read_only_database(source) as connection:
        schema_version = identify_schema_version(connection)
        page_count = _pragma_int(connection, "page_count")
        page_size = _pragma_int(connection, "page_size")
        tables = _table_names(connection)
        missing_core_tables = sorted(set(CORE_TABLES) - tables)
    if missing_core_tables:
        raise MaintenanceError(
            "Source database is missing expected core tables: " + ", ".join(missing_core_tables)
        )

    label_component = f"-{normalized_label}" if normalized_label else ""
    filename = f"cci-{timestamp}-schema-v{schema_version}{label_component}-{suffix}.sqlite"
    snapshot = (archive / filename).resolve(strict=False)
    manifest = Path(f"{snapshot}.manifest.json")
    partial = Path(f"{snapshot}.partial")
    manifest_partial = Path(f"{manifest}.partial")
    if _same_path(source, snapshot):
        raise MaintenanceError("Backup destination resolves to the live source database.")
    for candidate in (snapshot, manifest, partial, manifest_partial):
        if candidate.exists():
            raise MaintenanceError(f"Backup destination already exists and will not be overwritten: {candidate}")

    sizes = sqlite_file_sizes(source)
    logical_size = page_count * page_size
    estimated_snapshot_size = max(logical_size, sizes["database_size_bytes"] + sizes["wal_size_bytes"])
    required_free_bytes = estimated_snapshot_size + max(
        BACKUP_SPACE_MARGIN_BYTES,
        estimated_snapshot_size // 20,
    )
    usage_path = _nearest_existing_directory(archive)
    usage = shutil.disk_usage(usage_path)
    if usage.free < required_free_bytes:
        raise MaintenanceError(
            "Insufficient free space for verified backup: "
            f"required at least {required_free_bytes} bytes, found {usage.free} bytes at {usage_path}."
        )
    return {
        "tool_version": TOOL_VERSION,
        "status": "planned",
        "source_database": str(source),
        "archive_directory": str(archive),
        "snapshot_path": str(snapshot),
        "manifest_path": str(manifest),
        "partial_snapshot_path": str(partial),
        "partial_manifest_path": str(manifest_partial),
        "created_utc": _iso_utc(created),
        "schema_version": schema_version,
        "label": normalized_label or NA,
        "source_size_bytes": sizes["database_size_bytes"],
        "source_sqlite_footprint_bytes": sizes["sqlite_footprint_bytes"],
        "estimated_snapshot_size_bytes": estimated_snapshot_size,
        "required_free_bytes": required_free_bytes,
        "destination_free_bytes": usage.free,
        "writes_planned": (str(partial), str(manifest_partial), str(snapshot), str(manifest)),
    }


def create_verified_backup(
    source_database: Path | str,
    archive_directory: Path | str,
    *,
    label: str | None = None,
    dry_run: bool = False,
    allow_unsafe_temp: bool = False,
    progress: Callable[[int, int], None] | None = None,
    now: datetime | None = None,
    unique_suffix: str | None = None,
) -> dict[str, Any]:
    """Create, verify, checksum, manifest, and atomically promote an online snapshot."""

    plan = plan_backup(
        source_database,
        archive_directory,
        label=label,
        allow_unsafe_temp=allow_unsafe_temp,
        now=now,
        unique_suffix=unique_suffix,
    )
    if dry_run:
        return {**plan, "status": "dry-run", "writes_performed": False}

    source = Path(plan["source_database"])
    archive = Path(plan["archive_directory"])
    snapshot = Path(plan["snapshot_path"])
    manifest_path = Path(plan["manifest_path"])
    partial = Path(plan["partial_snapshot_path"])
    manifest_partial = Path(plan["partial_manifest_path"])
    created_paths: list[Path] = []
    source_connection: sqlite3.Connection | None = None
    destination_connection: sqlite3.Connection | None = None
    try:
        archive.mkdir(parents=True, exist_ok=True)
        for candidate in (snapshot, manifest_path, partial, manifest_partial):
            if candidate.exists():
                raise MaintenanceError(
                    f"Backup destination appeared after planning and will not be overwritten: {candidate}"
                )

        source_connection = open_read_only_database(source)
        source_connection.execute("BEGIN")
        source_schema_version = identify_schema_version(source_connection)
        source_tables = _table_names(source_connection)
        source_table_counts = _table_counts(source_connection, source_tables, DIAGNOSTIC_TABLES)
        source_core_counts = {table: source_table_counts[table] for table in CORE_TABLES}
        source_journal_mode = _database_journal_mode(source, source_connection)

        _create_empty_exclusive(partial)
        created_paths.append(partial)
        destination_connection = sqlite3.connect(partial)
        _run_online_backup(
            source_connection,
            destination_connection,
            progress=progress,
        )
        destination_connection.close()
        destination_connection = None
        source_connection.rollback()
        source_connection.close()
        source_connection = None

        verification = _verify_snapshot(
            partial,
            expected_schema_version=source_schema_version,
            expected_core_counts=source_core_counts,
        )
        if not verification["ok"]:
            raise MaintenanceError(
                "Snapshot verification failed; partial snapshot was not promoted: "
                + "; ".join(verification["errors"])
            )

        snapshot_size = partial.stat().st_size
        digest = sha256_file(partial)
        manifest = {
            "manifest_format_version": MANIFEST_FORMAT_VERSION,
            "tool_version": TOOL_VERSION,
            "supported_schema_version": SCHEMA_VERSION,
            "source_path": str(source),
            "snapshot_filename": snapshot.name,
            "manifest_filename": manifest_path.name,
            "created_utc": plan["created_utc"],
            "label": plan["label"],
            "source_schema_version": source_schema_version,
            "snapshot_schema_version": verification["schema_version"],
            "source_size_bytes": plan["source_size_bytes"],
            "source_sqlite_footprint_bytes": plan["source_sqlite_footprint_bytes"],
            "snapshot_size_bytes": snapshot_size,
            "sha256": digest,
            "backup_method": "Python sqlite3 online backup API",
            "source_journal_mode": source_journal_mode,
            "integrity_check": {
                "pragma": "quick_check",
                "ok": verification["integrity_ok"],
                "result": verification["integrity_result"],
            },
            "foreign_key_check": {
                "ok": verification["foreign_key_ok"],
                "violations": verification["foreign_key_check"],
            },
            "expected_core_tables": list(CORE_TABLES),
            "core_table_counts": source_core_counts,
            "automatic_deletion": False,
        }
        _write_json_exclusive(manifest_partial, manifest)
        created_paths.append(manifest_partial)
        _validate_manifest_payload(manifest, snapshot_path=partial, expected_snapshot_name=snapshot.name)

        _promote_no_overwrite(partial, snapshot)
        created_paths.remove(partial)
        created_paths.append(snapshot)
        _promote_no_overwrite(manifest_partial, manifest_path)
        created_paths.remove(manifest_partial)
        created_paths.append(manifest_path)
        return {
            **plan,
            "status": "verified",
            "writes_performed": True,
            "snapshot_size_bytes": snapshot_size,
            "sha256": digest,
            "verification": verification,
            "manifest": manifest,
        }
    except BaseException as exc:
        if destination_connection is not None:
            destination_connection.close()
        if source_connection is not None:
            if source_connection.in_transaction:
                source_connection.rollback()
            source_connection.close()
        _remove_created_artifacts(created_paths)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        if isinstance(exc, MaintenanceError):
            raise
        raise MaintenanceError(f"Verified online backup failed: {exc}") from exc


def verify_backup(
    snapshot_path: Path | str,
    *,
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """Verify a promoted snapshot and its manifest without modifying either file."""

    snapshot = _existing_database_path(snapshot_path)
    manifest = Path(manifest_path) if manifest_path is not None else Path(f"{snapshot}.manifest.json")
    if not manifest.exists() or not manifest.is_file():
        raise MaintenanceError(f"Backup manifest does not exist: {manifest}")
    try:
        payload = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintenanceError(f"Unable to read backup manifest: {manifest}") from exc
    if not isinstance(payload, Mapping):
        raise MaintenanceError(f"Backup manifest is not a JSON object: {manifest}")

    expected_schema = _manifest_int(payload, "snapshot_schema_version")
    expected_counts = payload.get("core_table_counts")
    if not isinstance(expected_counts, Mapping):
        raise MaintenanceError("Backup manifest is missing core_table_counts.")
    verification = _verify_snapshot(
        snapshot,
        expected_schema_version=expected_schema,
        expected_core_counts={str(key): int(value) for key, value in expected_counts.items()},
    )
    errors = list(verification["errors"])
    if payload.get("snapshot_filename") != snapshot.name:
        errors.append("manifest snapshot filename does not match")
    actual_size = snapshot.stat().st_size
    if _manifest_int(payload, "snapshot_size_bytes") != actual_size:
        errors.append("manifest snapshot size does not match")
    actual_sha256 = sha256_file(snapshot)
    if str(payload.get("sha256", "")).lower() != actual_sha256:
        errors.append("manifest SHA-256 does not match")
    return {
        "tool_version": TOOL_VERSION,
        "snapshot_path": str(snapshot),
        "manifest_path": str(manifest.resolve()),
        "snapshot_size_bytes": actual_size,
        "sha256": actual_sha256,
        "database_verification": verification,
        "errors": errors,
        "ok": not errors,
    }


def checkpoint_database(
    database_path: Path | str,
    *,
    mode: str = "PASSIVE",
    busy_timeout_ms: int = WRITABLE_BUSY_TIMEOUT_MS,
) -> dict[str, Any]:
    """Explicitly checkpoint an existing WAL database through a writable connection."""

    path = _existing_database_path(database_path)
    normalized_mode = str(mode).strip().upper()
    if normalized_mode not in CHECKPOINT_MODES:
        raise MaintenanceError(
            f"Unsupported checkpoint mode {mode!r}; choose one of {', '.join(sorted(CHECKPOINT_MODES))}."
        )
    connection = connect_database(path, busy_timeout_ms=busy_timeout_ms)
    try:
        schema_version = identify_schema_version(connection)
        if schema_version > SCHEMA_VERSION:
            raise MaintenanceError(
                f"Unsupported database schema version {schema_version}; "
                f"this runtime supports up to version {SCHEMA_VERSION}."
            )
        row = connection.execute(f"PRAGMA wal_checkpoint({normalized_mode})").fetchone()
        if row is None or len(row) < 3:
            raise MaintenanceError("SQLite did not return WAL checkpoint status.")
        busy, log_pages, checkpointed_pages = (int(row[0]), int(row[1]), int(row[2]))
        complete = busy == 0 and checkpointed_pages >= log_pages
        return {
            "tool_version": TOOL_VERSION,
            "database_path": str(path),
            "schema_version": schema_version,
            "checkpoint_mode": normalized_mode,
            "busy": busy,
            "wal_log_pages": log_pages,
            "checkpointed_pages": checkpointed_pages,
            "complete": complete,
            "status": "complete" if complete else "busy-or-incomplete",
        }
    except sqlite3.Error as exc:
        raise MaintenanceError(f"WAL checkpoint failed for {path}: {exc}") from exc
    finally:
        connection.close()


def sha256_file(path: Path | str) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run_online_backup(
    source: sqlite3.Connection,
    destination: sqlite3.Connection,
    *,
    progress: Callable[[int, int], None] | None,
) -> None:
    last_reported = -1

    def report(status: int, remaining: int, total: int) -> None:
        nonlocal last_reported
        del status
        copied = max(0, total - remaining)
        if progress is None:
            return
        reporting_step = max(1, total // 20)
        if remaining == 0 or copied - last_reported >= reporting_step:
            last_reported = copied
            progress(copied, total)

    source.backup(destination, pages=BACKUP_PAGE_BATCH, progress=report, sleep=0.05)


def _verify_snapshot(
    snapshot_path: Path,
    *,
    expected_schema_version: int,
    expected_core_counts: Mapping[str, int],
) -> dict[str, Any]:
    errors: list[str] = []
    with open_read_only_database(snapshot_path) as connection:
        schema_version = identify_schema_version(connection)
        integrity_result = [str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall()]
        foreign_key_rows = _foreign_key_check(connection)
        tables = _table_names(connection)
        missing_core_tables = sorted(set(CORE_TABLES) - tables)
        table_counts = _table_counts(connection, tables, tuple(expected_core_counts))
    if schema_version != expected_schema_version:
        errors.append(
            f"schema version mismatch: expected {expected_schema_version}, found {schema_version}"
        )
    if integrity_result != ["ok"]:
        errors.append("quick_check failed")
    if foreign_key_rows:
        errors.append("foreign_key_check reported violations")
    if missing_core_tables:
        errors.append("missing core tables: " + ", ".join(missing_core_tables))
    for table, expected_count in expected_core_counts.items():
        actual_count = table_counts.get(table, NA)
        if actual_count != expected_count:
            errors.append(
                f"table count mismatch for {table}: expected {expected_count}, found {actual_count}"
            )
    return {
        "schema_version": schema_version,
        "integrity_result": integrity_result,
        "integrity_ok": integrity_result == ["ok"],
        "foreign_key_check": foreign_key_rows,
        "foreign_key_ok": not foreign_key_rows,
        "missing_core_tables": missing_core_tables,
        "core_table_counts": table_counts,
        "errors": errors,
        "ok": not errors,
    }


def _foreign_key_check(connection: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = connection.execute("PRAGMA foreign_key_check").fetchall()
    return [
        {
            "table": str(row[0]),
            "rowid": row[1],
            "parent": str(row[2]),
            "foreign_key_id": int(row[3]),
        }
        for row in rows
    ]


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _table_counts(
    connection: sqlite3.Connection,
    existing_tables: set[str],
    requested_tables: Sequence[str],
) -> dict[str, int | str]:
    counts: dict[str, int | str] = {}
    for table in requested_tables:
        if table not in existing_tables:
            counts[table] = NA
            continue
        identifier = table.replace('"', '""')
        counts[table] = int(connection.execute(f'SELECT COUNT(*) FROM "{identifier}"').fetchone()[0])
    return counts


def _largest_tables(connection: sqlite3.Connection, *, limit: int = 10) -> list[dict[str, Any]] | str:
    try:
        rows = connection.execute(
            """
            SELECT name, SUM(pgsize) AS size_bytes
            FROM dbstat
            WHERE name NOT LIKE 'sqlite_%'
            GROUP BY name
            ORDER BY size_bytes DESC, name ASC
            LIMIT ?
            """,
            (max(1, int(limit)),),
        ).fetchall()
    except sqlite3.Error:
        return NOT_ENOUGH_DATA
    return [{"table": str(row[0]), "size_bytes": int(row[1] or 0)} for row in rows]


def _scan_timestamp_range(
    connection: sqlite3.Connection,
    tables: set[str],
) -> dict[str, str]:
    if "scan_runs" not in tables:
        return {"oldest_scan_timestamp": NA, "newest_scan_timestamp": NA}
    row = connection.execute("SELECT MIN(timestamp), MAX(timestamp) FROM scan_runs").fetchone()
    return {
        "oldest_scan_timestamp": str(row[0]) if row and row[0] else NA,
        "newest_scan_timestamp": str(row[1]) if row and row[1] else NA,
    }


def _outbox_state_counts(
    connection: sqlite3.Connection,
    tables: set[str],
) -> dict[str, Any]:
    return {
        "telegram_alert_attempts": _group_counts(
            connection, tables, "telegram_alert_attempts", "telegram_status"
        ),
        "public_alert_events": _group_counts(
            connection, tables, "public_alert_events", "delivery_state"
        ),
        "public_alert_delivery_parts": _group_counts(
            connection, tables, "public_alert_delivery_parts", "delivery_state"
        ),
    }


def _group_counts(
    connection: sqlite3.Connection,
    tables: set[str],
    table: str,
    column: str,
) -> dict[str, int] | str:
    if table not in tables:
        return NA
    try:
        rows = connection.execute(
            f'SELECT "{column}", COUNT(*) FROM "{table}" GROUP BY "{column}" ORDER BY "{column}"'
        ).fetchall()
    except sqlite3.Error:
        return NA
    return {str(row[0] if row[0] not in (None, "") else NA): int(row[1]) for row in rows}


def _archive_history(
    source: Path,
    archive_directory: Path | str | None,
) -> list[dict[str, Any]]:
    if archive_directory is None:
        return []
    archive = Path(archive_directory).expanduser().resolve(strict=False)
    if not archive.exists() or not archive.is_dir():
        return []
    history: list[dict[str, Any]] = []
    for manifest_path in archive.glob("*.sqlite.manifest.json"):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                continue
            if not _same_path(Path(str(payload.get("source_path", ""))), source):
                continue
            created = _parse_utc(payload.get("created_utc"))
            snapshot_size = int(payload.get("snapshot_size_bytes"))
            integrity = payload.get("integrity_check")
            if not isinstance(integrity, Mapping):
                continue
            history.append(
                {
                    "manifest_path": str(manifest_path.resolve()),
                    "created": created,
                    "created_utc": _iso_utc(created),
                    "snapshot_size_bytes": snapshot_size,
                    "integrity_ok": bool(integrity.get("ok")),
                }
            )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            continue
    history.sort(key=lambda item: item["created"])
    return history


def _growth_estimate(history: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(history) < 2:
        return {"status": NOT_ENOUGH_DATA, "bytes_per_day": None, "basis": "verified snapshots"}
    older = history[-2]
    newer = history[-1]
    elapsed = newer["created"] - older["created"]
    elapsed_days = elapsed.total_seconds() / 86_400
    if elapsed_days <= 0:
        return {"status": NOT_ENOUGH_DATA, "bytes_per_day": None, "basis": "verified snapshots"}
    delta = int(newer["snapshot_size_bytes"]) - int(older["snapshot_size_bytes"])
    return {
        "status": "estimated",
        "bytes_per_day": round(delta / elapsed_days, 2),
        "elapsed_days": round(elapsed_days, 4),
        "size_delta_bytes": delta,
        "basis": "two latest verified snapshot manifests",
    }


def _days_until_capacity_warning(
    *,
    free_bytes: int,
    total_bytes: int,
    growth_bytes_per_day: Any,
    thresholds: WarningThresholds,
) -> float | str:
    if not isinstance(growth_bytes_per_day, (int, float)) or growth_bytes_per_day <= 0:
        return NOT_ENOUGH_DATA
    percent_floor_bytes = int(total_bytes * thresholds.low_free_percent / 100.0)
    warning_floor = max(thresholds.low_free_bytes, percent_floor_bytes)
    remaining = max(0, free_bytes - warning_floor)
    return round(remaining / growth_bytes_per_day, 2)


def _diagnostic_warnings(
    *,
    sizes: Mapping[str, int],
    free_bytes: int,
    percent_free: float,
    growth: Mapping[str, Any],
    archive_history: Sequence[Mapping[str, Any]],
    thresholds: WarningThresholds,
    now: datetime,
) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    if free_bytes < thresholds.low_free_bytes:
        warnings.append(
            _warning("low_free_bytes", free_bytes, thresholds.low_free_bytes, "Filesystem free bytes are low.")
        )
    if percent_free < thresholds.low_free_percent:
        warnings.append(
            _warning(
                "low_free_percent",
                round(percent_free, 4),
                thresholds.low_free_percent,
                "Filesystem free percentage is low.",
            )
        )
    if sizes["wal_size_bytes"] > thresholds.large_wal_bytes:
        warnings.append(
            _warning(
                "large_wal",
                sizes["wal_size_bytes"],
                thresholds.large_wal_bytes,
                "WAL size exceeds the configured diagnostic threshold.",
            )
        )
    growth_rate = growth.get("bytes_per_day")
    if isinstance(growth_rate, (int, float)) and growth_rate > thresholds.rapid_growth_bytes_per_day:
        warnings.append(
            _warning(
                "rapid_recent_growth",
                growth_rate,
                thresholds.rapid_growth_bytes_per_day,
                "Estimated recent database growth is rapid.",
            )
        )
    if archive_history:
        latest = archive_history[-1]
        age_hours = max(0.0, (now - latest["created"]).total_seconds() / 3_600)
        if age_hours > thresholds.backup_max_age_hours:
            warnings.append(
                _warning(
                    "backup_overdue",
                    round(age_hours, 2),
                    thresholds.backup_max_age_hours,
                    "Latest verified backup is older than the configured threshold.",
                )
            )
        if latest.get("integrity_ok") and age_hours > thresholds.integrity_max_age_hours:
            warnings.append(
                _warning(
                    "integrity_check_overdue",
                    round(age_hours, 2),
                    thresholds.integrity_max_age_hours,
                    "Latest recorded integrity check is older than the configured threshold.",
                )
            )
    return warnings


def _warning(code: str, actual: Any, threshold: Any, message: str) -> dict[str, Any]:
    return {"code": code, "actual": actual, "threshold": threshold, "message": message, "diagnostic_only": True}


def _archive_summary(history: Sequence[Mapping[str, Any]], now: datetime) -> dict[str, Any]:
    if not history:
        return {"status": NOT_ENOUGH_DATA, "verified_backup_count": 0}
    latest = history[-1]
    return {
        "status": "available",
        "verified_backup_count": len(history),
        "latest_created_utc": latest["created_utc"],
        "latest_age_hours": round(max(0.0, (now - latest["created"]).total_seconds() / 3_600), 2),
        "latest_manifest_path": latest["manifest_path"],
    }


def _existing_database_path(value: Path | str) -> Path:
    path = Path(value)
    if not path.exists():
        raise MaintenanceError(f"Database does not exist: {path}")
    if not path.is_file():
        raise MaintenanceError(f"Database path is not a file: {path}")
    try:
        return path.resolve(strict=True)
    except OSError as exc:
        raise MaintenanceError(f"Unable to resolve database path: {path}") from exc


def _archive_path(value: Path | str) -> Path:
    text = str(value).strip()
    if not text:
        raise MaintenanceError("Archive directory must be an explicit non-empty path.")
    try:
        return Path(text).expanduser().resolve(strict=False)
    except OSError as exc:
        raise MaintenanceError(f"Unable to resolve archive directory: {text}") from exc


def _nearest_existing_directory(path: Path) -> Path:
    candidate = path
    while not candidate.exists():
        parent = candidate.parent
        if parent == candidate:
            raise MaintenanceError(f"Archive path has no existing filesystem parent: {path}")
        candidate = parent
    if not candidate.is_dir():
        candidate = candidate.parent
    return candidate


def _safe_label(value: str | None) -> str:
    if value is None or not value.strip():
        return ""
    normalized = re.sub(r"[^A-Za-z0-9_-]+", "-", value.strip()).strip("-_").lower()
    if not normalized:
        raise MaintenanceError("Backup label contains no safe filename characters.")
    return normalized[:48]


def _safe_suffix(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9]+", "", str(value))
    if len(normalized) < 6:
        raise MaintenanceError("Backup unique suffix must contain at least six alphanumeric characters.")
    return normalized[:32].lower()


def _same_path(left: Path, right: Path) -> bool:
    try:
        left_text = os.path.normcase(str(left.expanduser().resolve(strict=False)))
        right_text = os.path.normcase(str(right.expanduser().resolve(strict=False)))
    except OSError:
        return False
    return left_text == right_text


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _file_size(path: Path) -> int:
    try:
        return path.stat().st_size if path.is_file() else 0
    except OSError:
        return 0


def _pragma_int(connection: sqlite3.Connection, pragma: str) -> int:
    row = connection.execute(f"PRAGMA {pragma}").fetchone()
    if row is None:
        raise MaintenanceError(f"SQLite did not return PRAGMA {pragma}.")
    return int(row[0])


def _pragma_text(connection: sqlite3.Connection, pragma: str) -> str:
    row = connection.execute(f"PRAGMA {pragma}").fetchone()
    if row is None or row[0] is None:
        return NA
    return str(row[0])

def _database_journal_mode(path: Path, connection: sqlite3.Connection) -> str:
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError:
        header = b""
    if (
        header.startswith(b"SQLite format 3\x00")
        and len(header) >= 20
        and header[18] == 2
        and header[19] == 2
    ):
        return "wal"
    return _pragma_text(connection, "journal_mode")




def _schema_status(schema_version: int) -> str:
    if schema_version == SCHEMA_VERSION:
        return "current"
    if schema_version > SCHEMA_VERSION:
        return "unsupported-newer"
    if schema_version <= 0:
        return "unversioned"
    return "older-supported-for-read-only-inspection"


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any) -> datetime:
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    return _utc(parsed)


def _create_empty_exclusive(path: Path) -> None:
    try:
        with path.open("xb"):
            pass
    except FileExistsError as exc:
        raise MaintenanceError(f"Refusing to overwrite existing partial backup: {path}") from exc
    except OSError as exc:
        raise MaintenanceError(f"Unable to create partial backup: {path}") from exc



def _write_json_exclusive(path: Path, payload: Mapping[str, Any]) -> None:
    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            created = True
            json.dump(payload, handle, indent=2, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, TypeError, ValueError) as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise MaintenanceError(f"Unable to write partial backup manifest: {path}") from exc


def _validate_manifest_payload(
    payload: Mapping[str, Any],
    *,
    snapshot_path: Path,
    expected_snapshot_name: str,
) -> None:
    if payload.get("snapshot_filename") != expected_snapshot_name:
        raise MaintenanceError("Generated manifest snapshot filename is inconsistent.")
    if int(payload.get("snapshot_size_bytes", -1)) != snapshot_path.stat().st_size:
        raise MaintenanceError("Generated manifest snapshot size is inconsistent.")
    if str(payload.get("sha256", "")) != sha256_file(snapshot_path):
        raise MaintenanceError("Generated manifest SHA-256 is inconsistent.")


def _promote_no_overwrite(partial: Path, final: Path) -> None:
    if final.exists():
        raise MaintenanceError(f"Refusing to overwrite existing backup artifact: {final}")
    try:
        os.rename(partial, final)
    except OSError as exc:
        raise MaintenanceError(f"Unable to atomically promote {partial.name} to {final.name}") from exc


def _remove_created_artifacts(paths: Sequence[Path]) -> None:
    for path in reversed(tuple(paths)):
        try:
            if path.is_file():
                path.unlink()
        except OSError:
            # A failed cleanup remains visibly named as a partial artifact; the source is untouched.
            continue


def _manifest_int(payload: Mapping[str, Any], key: str) -> int:
    try:
        return int(payload[key])
    except (KeyError, TypeError, ValueError) as exc:
        raise MaintenanceError(f"Backup manifest has invalid {key}.") from exc


__all__ = [
    "CORE_TABLES",
    "MANIFEST_FORMAT_VERSION",
    "MaintenanceError",
    "NOT_ENOUGH_DATA",
    "TOOL_VERSION",
    "WarningThresholds",
    "check_database",
    "checkpoint_database",
    "create_verified_backup",
    "inspect_database",
    "plan_backup",
    "sha256_file",
    "sqlite_file_sizes",
    "verify_backup",
]
