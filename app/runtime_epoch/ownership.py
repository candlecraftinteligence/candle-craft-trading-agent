"""Shared operational ownership policy for lifecycle and public effects."""

from __future__ import annotations

import sqlite3
from typing import Any

from app.data.dtos import NA
from app.runtime_epoch.authority import load_active_runtime_epoch, require_active_runtime_epoch
from app.runtime_epoch.errors import (
    RuntimeEpochConfigurationError,
    RuntimeEpochIdentityCollisionError,
    RuntimeEpochOriginError,
    RuntimeEpochOwnershipError,
)
from app.runtime_epoch.models import PublicOwnershipDecision, RuntimeEpochRecord
from app.runtime_epoch.origin import load_granted_origin
from app.runtime_epoch.time_contract import parse_utc


def lifecycle_row_epoch_id(row: sqlite3.Row | None) -> str | None:
    if row is None:
        return None
    keys = row.keys() if hasattr(row, "keys") else []
    if "runtime_epoch_id" not in keys:
        return None
    value = row["runtime_epoch_id"]
    text = str(value).strip() if value is not None else ""
    return text or None


def require_current_epoch_lifecycle_write(
    connection: sqlite3.Connection,
    *,
    lifecycle_id: str,
    record_epoch_id: str | None,
    creation_origin_id: str | None,
    inserting: bool,
    epoch: RuntimeEpochRecord | None = None,
) -> RuntimeEpochRecord:
    epoch = epoch or require_active_runtime_epoch(connection)
    normalized_id = str(lifecycle_id or "").strip()
    if not normalized_id or normalized_id.upper() == NA:
        raise RuntimeEpochOwnershipError("Lifecycle write requires a lifecycle_id.")
    existing = connection.execute(
        "SELECT lifecycle_id, runtime_epoch_id, creation_origin_id FROM setup_lifecycle_records WHERE lifecycle_id = ?",
        (normalized_id,),
    ).fetchone()
    if existing is None:
        if not inserting:
            raise RuntimeEpochOwnershipError(f"Lifecycle {normalized_id} does not exist.")
        if record_epoch_id != epoch.epoch_id:
            raise RuntimeEpochOwnershipError("New lifecycle records must carry the active runtime epoch.")
        origin_id = str(creation_origin_id or "").strip()
        if not origin_id:
            raise RuntimeEpochOriginError("New lifecycle records require a granted creation origin.")
        origin = load_granted_origin(connection, origin_id)
        if origin is None:
            raise RuntimeEpochOriginError("Creation origin is missing or was not granted.")
        if str(origin["runtime_epoch_id"]) != epoch.epoch_id:
            raise RuntimeEpochOriginError("Creation origin is not bound to the active runtime epoch.")
        return epoch
    existing_epoch = lifecycle_row_epoch_id(existing)
    if existing_epoch is None:
        raise RuntimeEpochIdentityCollisionError(
            "Legacy lifecycle identity collision: refusing to overwrite, relabel, or supersede an untagged row."
        )
    if existing_epoch != epoch.epoch_id:
        raise RuntimeEpochOwnershipError("Lifecycle ownership does not match the active runtime epoch.")
    if record_epoch_id not in (None, existing_epoch) and record_epoch_id != existing_epoch:
        raise RuntimeEpochOwnershipError("Runtime epoch membership is immutable.")
    existing_origin = existing["creation_origin_id"]
    if creation_origin_id and existing_origin and str(creation_origin_id) != str(existing_origin):
        raise RuntimeEpochOwnershipError("Creation origin is immutable.")
    return epoch


def require_current_epoch_lifecycle_id(
    connection: sqlite3.Connection,
    lifecycle_id: str,
    *,
    epoch: RuntimeEpochRecord | None = None,
) -> sqlite3.Row:
    epoch = epoch or require_active_runtime_epoch(connection)
    normalized_id = str(lifecycle_id or "").strip()
    if not normalized_id or normalized_id.upper() == NA:
        raise RuntimeEpochOwnershipError("Lifecycle ownership lookup requires a lifecycle_id.")
    row = connection.execute(
        "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
        (normalized_id,),
    ).fetchone()
    if row is None:
        raise RuntimeEpochOwnershipError(f"Lifecycle {normalized_id} does not exist.")
    row_epoch = lifecycle_row_epoch_id(row)
    if row_epoch is None:
        raise RuntimeEpochOwnershipError("Legacy or unattributed lifecycle rows are evidence-only.")
    if row_epoch != epoch.epoch_id:
        raise RuntimeEpochOwnershipError("Lifecycle ownership does not match the active runtime epoch.")
    return row


def public_event_epoch_id(row: sqlite3.Row | Any | None) -> str | None:
    if row is None:
        return None
    try:
        value = row["runtime_epoch_id"]
    except (KeyError, IndexError, TypeError):
        value = getattr(row, "runtime_epoch_id", None)
    text = str(value).strip() if value is not None else ""
    if not text or text.upper() == NA:
        return None
    return text


def decide_public_effect(
    connection: sqlite3.Connection,
    event: sqlite3.Row | Any | None,
    *,
    epoch: RuntimeEpochRecord | None = None,
) -> PublicOwnershipDecision:
    loaded = epoch or load_active_runtime_epoch(connection)
    if loaded is None:
        return PublicOwnershipDecision(
            allowed=False,
            reason="runtime_epoch_missing",
            mutate=False,
            send=False,
        )
    event_epoch = public_event_epoch_id(event)
    origin_lifecycle = _optional_text(_field(event, "origin_lifecycle_id"))
    if event_epoch is None:
        return PublicOwnershipDecision(
            allowed=False,
            reason="legacy_or_unattributed_public_event",
            runtime_epoch_id=None,
            origin_lifecycle_id=origin_lifecycle,
            mutate=False,
            send=False,
        )
    if event_epoch != loaded.epoch_id:
        return PublicOwnershipDecision(
            allowed=False,
            reason="public_event_epoch_mismatch",
            runtime_epoch_id=event_epoch,
            origin_lifecycle_id=origin_lifecycle,
            mutate=False,
            send=False,
        )
    return PublicOwnershipDecision(
        allowed=True,
        reason=NA,
        runtime_epoch_id=event_epoch,
        origin_lifecycle_id=origin_lifecycle,
        mutate=True,
        send=True,
    )


def require_public_event_mutation(
    connection: sqlite3.Connection,
    event: sqlite3.Row | Any | None,
    *,
    epoch: RuntimeEpochRecord | None = None,
) -> RuntimeEpochRecord:
    loaded = epoch or require_active_runtime_epoch(connection)
    decision = decide_public_effect(connection, event, epoch=loaded)
    if not decision.allowed:
        raise RuntimeEpochOwnershipError(decision.reason)
    return loaded


def require_lifecycle_public_intent(
    connection: sqlite3.Connection,
    *,
    origin_lifecycle_id: str,
    epoch: RuntimeEpochRecord | None = None,
) -> sqlite3.Row | None:
    epoch = epoch or require_active_runtime_epoch(connection)
    normalized_id = str(origin_lifecycle_id or "").strip()
    if not normalized_id or normalized_id.upper() == NA:
        raise RuntimeEpochOwnershipError("Public intent requires an originating lifecycle id.")
    row = connection.execute(
        "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
        (normalized_id,),
    ).fetchone()
    if row is None:
        return None
    return require_current_epoch_lifecycle_id(connection, normalized_id, epoch=epoch)


def legacy_cooldown_veto(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    mode: str,
    direction: str,
    now: str,
) -> str | None:
    """Return a veto reason when a legacy absolute cooldown still applies."""

    row = connection.execute(
        """
        SELECT cooldown_until
        FROM setup_lifecycle_records
        WHERE symbol = ? AND mode = ? AND direction = ?
          AND is_current = 1
          AND runtime_epoch_id IS NULL
        LIMIT 1
        """,
        (str(symbol).upper(), str(mode).lower(), str(direction).lower()),
    ).fetchone()
    if row is None:
        return None
    cooldown_until = row["cooldown_until"]
    if cooldown_until in (None, "", NA):
        return None
    expiry = parse_utc(cooldown_until)
    current = parse_utc(now)
    if expiry is None or current is None:
        return "legacy_cooldown_unparseable"
    if current < expiry:
        return "legacy_cooldown_active"
    return None


def _field(event: sqlite3.Row | Any | None, name: str) -> Any:
    if event is None:
        return None
    try:
        return event[name]
    except (KeyError, IndexError, TypeError):
        return getattr(event, name, None)


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == NA:
        return None
    return text
