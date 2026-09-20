"""Operational startup: selected DB plus expected identity, fail closed, no auto-create."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.data.dtos import NA
from app.runtime_epoch.authority import require_expected_runtime_epoch
from app.runtime_epoch.errors import RuntimeEpochConfigurationError
from app.runtime_epoch.models import RUNTIME_EPOCH_CONTRACT_VERSION, RuntimeEpochIdentity, RuntimeEpochRecord
from app.runtime_epoch.watch_state import operational_watch_state_path, refuse_legacy_watch_fallback
from app.storage.database import (
    SCHEMA_VERSION,
    DatabaseMissingError,
    UnsupportedSchemaVersionError,
    connect_database,
    identify_schema_version,
)


@dataclass(frozen=True)
class OperationalContext:
    database_path: Path
    schema_version: int
    identity: RuntimeEpochIdentity
    epoch: RuntimeEpochRecord


@dataclass(frozen=True)
class OperationalRuntime:
    database_path: Path
    epoch: RuntimeEpochRecord
    watch_state_path: Path
    context: OperationalContext


def require_expected_operational_identity(
    expected_identity: RuntimeEpochIdentity | None = None,
    *,
    epoch_id: str | None = None,
    cutoff_at: str | None = None,
    reviewed_release_sha: str | None = None,
    generation_binding: str | None = None,
    contract_version: str | None = None,
) -> RuntimeEpochIdentity:
    """Require the complete durable identity. Epoch id alone is not authority."""

    if expected_identity is not None:
        return _complete_identity(expected_identity)
    if epoch_id and not (cutoff_at and reviewed_release_sha and generation_binding):
        raise RuntimeEpochConfigurationError(
            "Operational open requires the complete expected runtime identity, not epoch id alone."
        )
    if not epoch_id or not cutoff_at or not reviewed_release_sha or not generation_binding:
        raise RuntimeEpochConfigurationError(
            "Operational open requires an explicit expected runtime identity."
        )
    return _complete_identity(
        RuntimeEpochIdentity(
            epoch_id=str(epoch_id).strip(),
            cutoff_at=str(cutoff_at).strip(),
            contract_version=str(contract_version or RUNTIME_EPOCH_CONTRACT_VERSION).strip(),
            reviewed_release_sha=str(reviewed_release_sha).strip(),
            generation_binding=str(generation_binding).strip(),
        )
    )


def identity_from_settings(settings: Any) -> RuntimeEpochIdentity:
    return require_expected_operational_identity(
        epoch_id=getattr(settings, "runtime_epoch_id", None),
        cutoff_at=getattr(settings, "runtime_epoch_cutoff_at", None),
        reviewed_release_sha=getattr(settings, "runtime_reviewed_release_sha", None),
        generation_binding=getattr(settings, "runtime_generation_binding", None),
        contract_version=getattr(settings, "runtime_epoch_contract_version", None),
    )


def inspect_operational_database(
    path: Path | str,
    *,
    expected_identity: RuntimeEpochIdentity | None = None,
    expected_epoch_id: str | None = None,
) -> RuntimeEpochRecord:
    """Validate an existing selected DB without creating, migrating, or repairing it."""

    identity = require_expected_operational_identity(
        expected_identity,
        epoch_id=expected_epoch_id,
    )
    database_path = Path(path)
    if not database_path.exists() or not database_path.is_file():
        raise RuntimeEpochConfigurationError(
            f"Operational database is missing; refusing to auto-create {database_path}."
        )
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
        return require_expected_runtime_epoch(connection, identity)
    except DatabaseMissingError as exc:
        raise RuntimeEpochConfigurationError(str(exc)) from exc
    finally:
        if connection is not None:
            connection.close()


def open_operational_database(
    path: Path | str,
    *,
    expected_identity: RuntimeEpochIdentity | None = None,
    expected_epoch_id: str | None = None,
) -> tuple[sqlite3.Connection, RuntimeEpochRecord]:
    """Open an existing DB without creating, migrating, or repairing a missing boundary."""

    identity = require_expected_operational_identity(
        expected_identity,
        epoch_id=expected_epoch_id,
    )
    epoch = inspect_operational_database(path, expected_identity=identity)
    connection = connect_database(path)
    try:
        schema_version = identify_schema_version(connection)
        if schema_version != SCHEMA_VERSION:
            connection.close()
            raise RuntimeEpochConfigurationError(
                f"Operational database schema is {schema_version}; expected {SCHEMA_VERSION}. "
                "Explicit offline migration is required."
            )
        require_expected_runtime_epoch(connection, identity)
        return connection, epoch
    except Exception:
        connection.close()
        raise


def open_operational_context(
    path: Path | str,
    *,
    expected_identity: RuntimeEpochIdentity,
) -> tuple[sqlite3.Connection, OperationalContext]:
    identity = require_expected_operational_identity(expected_identity)
    connection, epoch = open_operational_database(path, expected_identity=identity)
    context = OperationalContext(
        database_path=Path(path),
        schema_version=SCHEMA_VERSION,
        identity=epoch.identity,
        epoch=epoch,
    )
    return connection, context


def open_operational_service_database(
    path: Path | str,
    *,
    expected_identity: RuntimeEpochIdentity | None = None,
    expected_epoch_id: str | None = None,
) -> tuple[sqlite3.Connection, RuntimeEpochRecord]:
    """Fail-closed opener for operational repositories and services."""

    identity = require_expected_operational_identity(
        expected_identity,
        epoch_id=expected_epoch_id,
    )
    return open_operational_database(path, expected_identity=identity)


def open_repository_database(
    path: Path | str,
    *,
    expected_identity: RuntimeEpochIdentity | None = None,
    expected_epoch_id: str | None = None,
) -> sqlite3.Connection:
    """Open an operational repository connection. Missing identity is a configuration error."""

    identity = require_expected_operational_identity(
        expected_identity,
        epoch_id=expected_epoch_id,
    )
    connection, _epoch = open_operational_database(path, expected_identity=identity)
    return connection


def require_operational_runtime(
    *,
    database_path: Path | str,
    expected_identity: RuntimeEpochIdentity | None = None,
    expected_epoch_id: str | None = None,
    watch_state_base_dir: Path | str = Path("scan_runs"),
) -> OperationalRuntime:
    identity = require_expected_operational_identity(
        expected_identity,
        epoch_id=expected_epoch_id,
    )
    inspect_operational_database(database_path, expected_identity=identity)
    connection, context = open_operational_context(database_path, expected_identity=identity)
    try:
        watch_path = operational_watch_state_path(context.epoch.epoch_id, base_dir=watch_state_base_dir)
        refuse_legacy_watch_fallback(watch_path, context.epoch, base_dir=watch_state_base_dir)
    finally:
        connection.close()
    return OperationalRuntime(
        database_path=Path(database_path),
        epoch=context.epoch,
        watch_state_path=watch_path,
        context=context,
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


def _complete_identity(identity: RuntimeEpochIdentity) -> RuntimeEpochIdentity:
    epoch_id = _required_text(identity.epoch_id, "epoch_id")
    cutoff = _required_text(identity.cutoff_at, "cutoff_at")
    contract = _required_text(identity.contract_version, "contract_version") or RUNTIME_EPOCH_CONTRACT_VERSION
    if contract != RUNTIME_EPOCH_CONTRACT_VERSION:
        raise RuntimeEpochConfigurationError(
            f"Unsupported runtime epoch contract {contract}; expected {RUNTIME_EPOCH_CONTRACT_VERSION}."
        )
    release = _required_text(identity.reviewed_release_sha, "reviewed_release_sha")
    binding = _required_text(identity.generation_binding, "generation_binding")
    return RuntimeEpochIdentity(
        epoch_id=epoch_id,
        cutoff_at=cutoff,
        contract_version=contract,
        reviewed_release_sha=release,
        generation_binding=binding,
    )


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or text.upper() == NA:
        raise RuntimeEpochConfigurationError(f"Runtime epoch {field_name} is required.")
    return text


def _connect_inspect_only(database_path: Path) -> sqlite3.Connection:
    resolved = database_path.resolve(strict=True)
    uri = f"{resolved.as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only = ON")
    return connection
