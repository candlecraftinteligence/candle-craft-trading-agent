"""Shared operational ownership policy for lifecycle and public effects."""

from __future__ import annotations

import sqlite3
from typing import Any

from app.data.dtos import NA
from app.runtime_epoch.authority import load_active_runtime_epoch, require_active_runtime_epoch
from app.runtime_epoch.errors import (
    RuntimeEpochIdentityCollisionError,
    RuntimeEpochOriginError,
    RuntimeEpochOwnershipError,
)
from app.runtime_epoch.models import ORIGIN_STATUS_GRANTED, PublicOwnershipDecision, RuntimeEpochRecord
from app.runtime_epoch.origin import load_granted_origin
from app.runtime_epoch.time_contract import parse_utc


def canonical_operational_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text or text.upper() == NA:
        return ""
    return text


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
    symbol: str | None = None,
    epoch: RuntimeEpochRecord | None = None,
) -> RuntimeEpochRecord:
    epoch = epoch or require_active_runtime_epoch(connection)
    normalized_id = str(lifecycle_id or "").strip()
    if not normalized_id or normalized_id.upper() == NA:
        raise RuntimeEpochOwnershipError("Lifecycle write requires a lifecycle_id.")
    existing = connection.execute(
        "SELECT lifecycle_id, symbol, runtime_epoch_id, creation_origin_id FROM setup_lifecycle_records WHERE lifecycle_id = ?",
        (normalized_id,),
    ).fetchone()
    record_symbol = canonical_operational_symbol(symbol)
    if existing is None:
        if not inserting:
            raise RuntimeEpochOwnershipError(f"Lifecycle {normalized_id} does not exist.")
        if record_epoch_id != epoch.epoch_id:
            raise RuntimeEpochOwnershipError("New lifecycle records must carry the active runtime epoch.")
        origin_id = str(creation_origin_id or "").strip()
        if not origin_id:
            raise RuntimeEpochOriginError("New lifecycle records require a granted creation origin.")
        _require_origin_owns_symbol(
            connection,
            origin_id=origin_id,
            symbol=record_symbol,
            epoch=epoch,
        )
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
    origin_id = str(existing_origin or creation_origin_id or "").strip()
    owned_symbol = record_symbol or canonical_operational_symbol(existing["symbol"])
    if origin_id:
        _require_origin_owns_symbol(
            connection,
            origin_id=origin_id,
            symbol=owned_symbol,
            epoch=epoch,
        )
    existing_symbol = canonical_operational_symbol(existing["symbol"])
    if record_symbol and existing_symbol and record_symbol != existing_symbol:
        raise RuntimeEpochOwnershipError("Lifecycle symbol is immutable relative to its creation origin.")
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
    origin_id = str(row["creation_origin_id"] or "").strip()
    if not origin_id:
        raise RuntimeEpochOriginError("Lifecycle is missing a granted creation origin.")
    _require_origin_owns_symbol(
        connection,
        origin_id=origin_id,
        symbol=canonical_operational_symbol(row["symbol"]),
        epoch=epoch,
    )
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
    if event is None:
        return PublicOwnershipDecision(
            allowed=False,
            reason="public_event_missing",
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
    chain_reason = public_ownership_chain_reason(connection, event, epoch=loaded)
    if chain_reason is not None:
        return PublicOwnershipDecision(
            allowed=False,
            reason=chain_reason,
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


def public_ownership_chain_reason(
    connection: sqlite3.Connection,
    event: sqlite3.Row | Any,
    *,
    epoch: RuntimeEpochRecord,
) -> str | None:
    origin_lifecycle_id = _optional_text(_field(event, "origin_lifecycle_id"))
    if origin_lifecycle_id is None:
        return "public_event_missing_origin_lifecycle"
    try:
        lifecycle = require_lifecycle_public_intent(
            connection,
            origin_lifecycle_id=origin_lifecycle_id,
            epoch=epoch,
        )
    except RuntimeEpochOwnershipError as exc:
        return str(exc) or "public_event_lifecycle_ownership_invalid"
    except RuntimeEpochOriginError as exc:
        return str(exc) or "public_event_origin_ownership_invalid"
    event_symbol = canonical_operational_symbol(_field(event, "symbol"))
    lifecycle_symbol = canonical_operational_symbol(lifecycle["symbol"])
    if not event_symbol or event_symbol != lifecycle_symbol:
        return "public_event_symbol_mismatch"
    return None


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
    expected_symbol: str | None = None,
) -> sqlite3.Row:
    epoch = epoch or require_active_runtime_epoch(connection)
    normalized_id = str(origin_lifecycle_id or "").strip()
    if not normalized_id or normalized_id.upper() == NA:
        raise RuntimeEpochOwnershipError("Public intent requires an originating lifecycle id.")
    row = connection.execute(
        "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
        (normalized_id,),
    ).fetchone()
    if row is None:
        raise RuntimeEpochOwnershipError("Public intent requires a persisted originating lifecycle.")
    owned = require_current_epoch_lifecycle_id(connection, normalized_id, epoch=epoch)
    expected = canonical_operational_symbol(expected_symbol)
    actual = canonical_operational_symbol(owned["symbol"])
    if expected and actual and expected != actual:
        raise RuntimeEpochOwnershipError("Public intent lifecycle symbol does not match the plan symbol.")
    return owned


def require_public_part_mutation(
    connection: sqlite3.Connection,
    part_id: int,
    *,
    epoch: RuntimeEpochRecord | None = None,
) -> sqlite3.Row:
    row = connection.execute(
        """
        SELECT event.*
        FROM public_alert_delivery_parts AS part
        JOIN public_alert_events AS event ON event.id = part.public_alert_event_id
        WHERE part.id = ?
        """,
        (int(part_id),),
    ).fetchone()
    if row is None:
        raise RuntimeEpochOwnershipError("Public delivery part does not exist or is unowned.")
    require_public_event_mutation(connection, row, epoch=epoch)
    return row


def require_public_attempt_mutation(
    connection: sqlite3.Connection,
    attempt_id: int,
    *,
    epoch: RuntimeEpochRecord | None = None,
) -> sqlite3.Row:
    attempt = connection.execute(
        "SELECT * FROM telegram_alert_attempts WHERE id = ?",
        (int(attempt_id),),
    ).fetchone()
    if attempt is None:
        raise RuntimeEpochOwnershipError("Telegram attempt does not exist.")
    event_key = _optional_text(attempt["public_watchlist_event_key"])
    if event_key is None:
        raise RuntimeEpochOwnershipError("Attempt is not an owned public delivery record.")
    event = connection.execute(
        "SELECT * FROM public_alert_events WHERE event_key = ?",
        (event_key,),
    ).fetchone()
    if event is None:
        raise RuntimeEpochOwnershipError("Public event for attempt is missing.")
    require_public_event_mutation(connection, event, epoch=epoch)
    return attempt


def public_event_mutation_allowed(
    connection: sqlite3.Connection,
    event: sqlite3.Row | Any | None,
    *,
    epoch: RuntimeEpochRecord | None = None,
) -> bool:
    return decide_public_effect(connection, event, epoch=epoch).allowed


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


def _require_origin_owns_symbol(
    connection: sqlite3.Connection,
    *,
    origin_id: str,
    symbol: str,
    epoch: RuntimeEpochRecord,
) -> sqlite3.Row:
    origin = load_granted_origin(connection, origin_id)
    if origin is None:
        raise RuntimeEpochOriginError("Creation origin is missing or was not granted.")
    if str(origin["runtime_epoch_id"]) != epoch.epoch_id:
        raise RuntimeEpochOriginError("Creation origin is not bound to the active runtime epoch.")
    origin_symbol = canonical_operational_symbol(origin["symbol"])
    if not symbol or origin_symbol != symbol:
        raise RuntimeEpochOriginError("Creation origin symbol does not match the lifecycle symbol.")
    run_id = str(origin["run_id"] or "").strip()
    if not run_id:
        raise RuntimeEpochOriginError("Creation origin is missing its operational run.")
    run_row = connection.execute(
        "SELECT * FROM runtime_operational_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if run_row is None:
        raise RuntimeEpochOriginError("Creation origin run is not registered.")
    if str(run_row["runtime_epoch_id"]) != epoch.epoch_id:
        raise RuntimeEpochOriginError("Creation origin run is not bound to the active runtime epoch.")
    if str(origin["status"]) != ORIGIN_STATUS_GRANTED:
        raise RuntimeEpochOriginError("Creation origin was not granted.")
    return origin


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
