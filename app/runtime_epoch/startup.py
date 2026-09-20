"""Operational startup: selected DB plus expected epoch, fail closed, no auto-create."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from app.runtime_epoch.authority import require_expected_runtime_epoch
from app.runtime_epoch.errors import RuntimeEpochConfigurationError
from app.runtime_epoch.models import RuntimeEpochRecord
from app.runtime_epoch.watch_state import operational_watch_state_path, refuse_legacy_watch_fallback
from app.storage.database import (
    SCHEMA_VERSION,
    DatabaseMissingError,
    UnsupportedSchemaVersionError,
    connect_database,
    identify_schema_version,
    open_initialized_database,
)


@dataclass(frozen=True)
class OperationalRuntime:
    database_path: Path
    epoch: RuntimeEpochRecord
    watch_state_path: Path


def resolve_expected_operational_epoch_id(explicit: str | None = None) -> str:
    expected = str(explicit or os.environ.get("RUNTIME_EPOCH_ID") or "").strip()
    if not expected:
        raise RuntimeEpochConfigurationError(
            "Operational open requires an explicit expected runtime epoch id."
        )
    return expected


def inspect_operational_database(
    path: Path | str,
    *,
    expected_epoch_id: str,
) -> RuntimeEpochRecord:
    """Validate an existing selected DB without creating, migrating, or repairing it."""

    database_path = Path(path)
    if not database_path.exists() or not database_path.is_file():
        raise RuntimeEpochConfigurationError(
            f"Operational database is missing; refusing to auto-create {database_path}."
        )
    expected = resolve_expected_operational_epoch_id(expected_epoch_id)
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect_inspect_only(database_path)
        schema_version = identify_schema_version(connection)
        if schema_version > SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"Unsupported database schema version {schema_version}; "
                f"this runtime supports up to version {SCHEMA_VERSION}."
            )
        if schema_version != SCHEMA_VERSION:
            raise RuntimeEpochConfigurationError(
                f"Operational database schema is {schema_version}; expected {SCHEMA_VERSION}. "
                "Explicit offline migration is required."
            )
        return require_expected_runtime_epoch(connection, expected)
    except DatabaseMissingError as exc:
        raise RuntimeEpochConfigurationError(str(exc)) from exc
    finally:
        if connection is not None:
            connection.close()


def open_operational_database(
    path: Path | str,
    *,
    expected_epoch_id: str,
) -> tuple[sqlite3.Connection, RuntimeEpochRecord]:
    """Open an existing DB without creating, migrating, or repairing a missing boundary."""

    epoch = inspect_operational_database(path, expected_epoch_id=expected_epoch_id)
    connection = connect_database(path)
    try:
        schema_version = identify_schema_version(connection)
        if schema_version != SCHEMA_VERSION:
            connection.close()
            raise RuntimeEpochConfigurationError(
                f"Operational database schema is {schema_version}; expected {SCHEMA_VERSION}. "
                "Explicit offline migration is required."
            )
        require_expected_runtime_epoch(connection, epoch.epoch_id)
        return connection, epoch
    except Exception:
        connection.close()
        raise


def open_operational_service_database(
    path: Path | str,
    *,
    expected_epoch_id: str | None = None,
) -> tuple[sqlite3.Connection, RuntimeEpochRecord]:
    """Fail-closed opener for operational repositories and services."""

    expected = resolve_expected_operational_epoch_id(expected_epoch_id)
    return open_operational_database(path, expected_epoch_id=expected)


def open_repository_database(
    path: Path | str,
    *,
    expected_epoch_id: str | None = None,
) -> sqlite3.Connection:
    """Open a repository connection. Operational when an expected epoch is selected."""

    expected = str(expected_epoch_id or os.environ.get("RUNTIME_EPOCH_ID") or "").strip()
    if expected:
        connection, _epoch = open_operational_service_database(path, expected_epoch_id=expected)
        return connection
    return open_initialized_database(path)


def require_operational_runtime(
    *,
    database_path: Path | str,
    expected_epoch_id: str,
    watch_state_base_dir: Path | str = Path("scan_runs"),
) -> OperationalRuntime:
    inspect_operational_database(database_path, expected_epoch_id=expected_epoch_id)
    connection, epoch = open_operational_database(database_path, expected_epoch_id=expected_epoch_id)
    try:
        watch_path = operational_watch_state_path(epoch.epoch_id, base_dir=watch_state_base_dir)
        refuse_legacy_watch_fallback(watch_path, epoch, base_dir=watch_state_base_dir)
    finally:
        connection.close()
    return OperationalRuntime(
        database_path=Path(database_path),
        epoch=epoch,
        watch_state_path=watch_path,
    )


def migrate_existing_database(path: Path | str) -> None:
    """Explicit offline schema migration. Does not create a database or an epoch."""

    from app.storage.database import initialize_database

    database_path = Path(path)
    if not database_path.exists() or not database_path.is_file():
        raise RuntimeEpochConfigurationError(
            f"Offline migration requires an existing database file: {database_path}."
        )
    connection = connect_database(database_path)
    try:
        initialize_database(connection)
    finally:
        connection.close()


def _connect_inspect_only(database_path: Path) -> sqlite3.Connection:
    resolved = database_path.resolve(strict=True)
    uri = f"{resolved.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection
