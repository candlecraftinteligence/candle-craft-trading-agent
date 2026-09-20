"""Operational startup: selected DB plus expected epoch, fail closed, no auto-create."""

from __future__ import annotations

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
)


@dataclass(frozen=True)
class OperationalRuntime:
    database_path: Path
    epoch: RuntimeEpochRecord
    watch_state_path: Path


def open_operational_database(
    path: Path | str,
    *,
    expected_epoch_id: str,
) -> tuple[object, RuntimeEpochRecord]:
    """Open an existing DB without creating, migrating, or repairing a missing boundary."""

    database_path = Path(path)
    if not database_path.exists() or not database_path.is_file():
        raise RuntimeEpochConfigurationError(
            f"Operational database is missing; refusing to auto-create {database_path}."
        )
    try:
        connection = connect_database(database_path)
    except DatabaseMissingError as exc:
        raise RuntimeEpochConfigurationError(str(exc)) from exc
    try:
        schema_version = identify_schema_version(connection)
        if schema_version > SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"Unsupported database schema version {schema_version}; "
                f"this runtime supports up to version {SCHEMA_VERSION}."
            )
        if schema_version != SCHEMA_VERSION:
            connection.close()
            raise RuntimeEpochConfigurationError(
                f"Operational database schema is {schema_version}; expected {SCHEMA_VERSION}. "
                "Explicit offline migration is required."
            )
        epoch = require_expected_runtime_epoch(connection, expected_epoch_id)
        return connection, epoch
    except Exception:
        connection.close()
        raise


def require_operational_runtime(
    *,
    database_path: Path | str,
    expected_epoch_id: str,
    watch_state_base_dir: Path | str = Path("scan_runs"),
) -> OperationalRuntime:
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
