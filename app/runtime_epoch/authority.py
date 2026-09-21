"""Single immutable operational Runtime epoch authority."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Any

from app.data.dtos import NA
from app.runtime_epoch.errors import RuntimeEpochConfigurationError
from app.runtime_epoch.models import (
    ACTIVE_EPOCH_CONTROL_KEY,
    RUNTIME_EPOCH_CONTRACT_VERSION,
    RuntimeEpochIdentity,
    RuntimeEpochRecord,
)
from app.runtime_epoch.time_contract import comparable_utc, parse_utc

_REQUIRED_IDENTITY_FIELDS = (
    "epoch_id",
    "cutoff_at",
    "contract_version",
    "reviewed_release_sha",
    "generation_binding",
)


def initialize_runtime_epoch(
    connection: sqlite3.Connection,
    identity: RuntimeEpochIdentity,
    *,
    activated_at: str | None = None,
) -> RuntimeEpochRecord:
    """Idempotently persist the sole operational epoch. Schema migration never calls this."""

    normalized = _validated_identity(identity)
    existing = load_active_runtime_epoch(connection)
    if existing is not None:
        if _same_identity(existing.identity, normalized):
            return existing
        raise RuntimeEpochConfigurationError(
            "Active runtime epoch already exists with a different identity, cutoff, or binding."
        )
    timestamp = _activation_timestamp(activated_at)
    _require_tables(connection)
    in_transaction = connection.in_transaction
    if not in_transaction:
        connection.execute("BEGIN IMMEDIATE")
    try:
        raced = load_active_runtime_epoch(connection)
        if raced is not None:
            if _same_identity(raced.identity, normalized):
                if not in_transaction:
                    connection.commit()
                return raced
            raise RuntimeEpochConfigurationError(
                "Active runtime epoch already exists with a different identity, cutoff, or binding."
            )
        connection.execute(
            """
            INSERT INTO runtime_epochs (
                epoch_id, contract_version, activated_at, cutoff_at,
                reviewed_release_sha, generation_binding, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                normalized.epoch_id,
                normalized.contract_version,
                timestamp,
                normalized.cutoff_at,
                normalized.reviewed_release_sha,
                normalized.generation_binding,
                timestamp,
            ),
        )
        connection.execute(
            """
            INSERT INTO runtime_epoch_control (control_key, epoch_id)
            VALUES (?, ?)
            """,
            (ACTIVE_EPOCH_CONTROL_KEY, normalized.epoch_id),
        )
        if not in_transaction:
            connection.commit()
    except Exception:
        if not in_transaction and connection.in_transaction:
            connection.rollback()
        raise
    loaded = load_active_runtime_epoch(connection)
    if loaded is None:
        raise RuntimeEpochConfigurationError("Runtime epoch initialization did not persist an active epoch.")
    return loaded


def load_active_runtime_epoch(connection: sqlite3.Connection) -> RuntimeEpochRecord | None:
    if not _has_table(connection, "runtime_epoch_control"):
        return None
    row = connection.execute(
        """
        SELECT epoch.epoch_id AS epoch_id,
               epoch.contract_version AS contract_version,
               epoch.activated_at AS activated_at,
               epoch.cutoff_at AS cutoff_at,
               epoch.reviewed_release_sha AS reviewed_release_sha,
               epoch.generation_binding AS generation_binding,
               epoch.created_at AS created_at
        FROM runtime_epoch_control AS control
        JOIN runtime_epochs AS epoch ON epoch.epoch_id = control.epoch_id
        WHERE control.control_key = ?
        """,
        (ACTIVE_EPOCH_CONTROL_KEY,),
    ).fetchone()
    if row is None:
        return None
    return _record_from_row(row)


def require_active_runtime_epoch(connection: sqlite3.Connection) -> RuntimeEpochRecord:
    epoch = load_active_runtime_epoch(connection)
    if epoch is None:
        raise RuntimeEpochConfigurationError(
            "Operational runtime epoch is missing; refusing to initialize a default boundary."
        )
    return epoch


def require_expected_runtime_epoch(
    connection: sqlite3.Connection,
    expected: RuntimeEpochIdentity | str,
) -> RuntimeEpochRecord:
    epoch = require_active_runtime_epoch(connection)
    if isinstance(expected, str):
        raise RuntimeEpochConfigurationError(
            "Operational open requires the complete expected runtime identity, not epoch id alone."
        )
    if not _same_identity(epoch.identity, expected):
        raise RuntimeEpochConfigurationError(
            "Selected runtime identity does not match the durable epoch, release, or generation binding."
        )
    return epoch


def _validated_identity(identity: RuntimeEpochIdentity) -> RuntimeEpochIdentity:
    epoch_id = _required_text(identity.epoch_id, "epoch_id")
    cutoff = comparable_utc(identity.cutoff_at)
    if cutoff is None:
        raise RuntimeEpochConfigurationError("Runtime epoch cutoff_at must be a parseable UTC timestamp.")
    contract = _required_text(identity.contract_version, "contract_version")
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


def _same_identity(left: RuntimeEpochIdentity, right: RuntimeEpochIdentity) -> bool:
    return (
        left.epoch_id == right.epoch_id
        and left.cutoff_at == right.cutoff_at
        and left.contract_version == right.contract_version
        and left.reviewed_release_sha == right.reviewed_release_sha
        and left.generation_binding == right.generation_binding
    )


def _activation_timestamp(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    parsed = parse_utc(value)
    if parsed is None:
        raise RuntimeEpochConfigurationError("Runtime epoch activated_at must be a parseable UTC timestamp.")
    return comparable_utc(parsed) or str(value)


def _record_from_row(row: sqlite3.Row | Any) -> RuntimeEpochRecord:
    return RuntimeEpochRecord(
        epoch_id=str(_row_value(row, "epoch_id", 0)),
        contract_version=str(_row_value(row, "contract_version", 1)),
        activated_at=str(_row_value(row, "activated_at", 2)),
        cutoff_at=str(_row_value(row, "cutoff_at", 3)),
        reviewed_release_sha=str(_row_value(row, "reviewed_release_sha", 4)),
        generation_binding=str(_row_value(row, "generation_binding", 5)),
        created_at=str(_row_value(row, "created_at", 6)),
    )


def _row_value(row: sqlite3.Row | Any, key: str, index: int) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key]
    if isinstance(row, dict):
        return row[key]
    try:
        return row[key]
    except (TypeError, KeyError, IndexError):
        return row[index]


def _required_text(value: Any, field_name: str) -> str:
    text = str(value or "").strip()
    if not text or text.upper() == NA:
        raise RuntimeEpochConfigurationError(f"Runtime epoch {field_name} is required.")
    return text


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def _require_tables(connection: sqlite3.Connection) -> None:
    if not _has_table(connection, "runtime_epochs") or not _has_table(connection, "runtime_epoch_control"):
        raise RuntimeEpochConfigurationError(
            "Runtime epoch tables are missing; explicit schema migration is required first."
        )


del _REQUIRED_IDENTITY_FIELDS
