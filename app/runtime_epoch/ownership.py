"""Shared operational ownership policy for lifecycle and public effects."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from typing import Any

from app.data.dtos import NA
from app.runtime_epoch.authority import load_active_runtime_epoch, require_active_runtime_epoch
from app.runtime_epoch.errors import (
    RuntimeEpochIdentityCollisionError,
    RuntimeEpochOriginError,
    RuntimeEpochOwnershipError,
)
from app.runtime_epoch.models import ORIGIN_STATUS_GRANTED, PublicOwnershipDecision, RuntimeEpochRecord
from app.lifecycle.outcome_policy import canonical_stored_price
from app.runtime_epoch.origin import load_granted_origin
from app.runtime_epoch.time_contract import parse_utc

PUBLIC_ROOT_EVENT_TYPES = frozenset({"initial_watchlist"})
PUBLIC_CHAIN_STARTER_EVENT_TYPES = frozenset(
    {
        "initial_watchlist",
        "setup_triggered",
        "signal_confirmed",
    }
)
PUBLIC_FOLLOWUP_EVENT_TYPES = frozenset(
    {
        "setup_triggered",
        "signal_confirmed",
        "limit_hit",
        "tp1_hit",
        "tp2_hit",
        "tp3_hit",
        "sl_hit",
        "invalidated",
        "expired",
        "cooldown",
        "no_longer_tracking",
    }
)
PUBLIC_EVENT_FAMILY_TYPES = PUBLIC_ROOT_EVENT_TYPES | PUBLIC_FOLLOWUP_EVENT_TYPES
_CHAIN_STARTER_LOOKUP_ORDER = (
    "initial_watchlist",
    "setup_triggered",
    "signal_confirmed",
)
OPERATIONAL_ATTEMPT_STATUSES = frozenset(
    {
        "pending",
        "retryable",
        "in_flight",
        "uncertain",
        "sent",
        "reserved",
    }
)
OPERATIONAL_DELIVERY_STATES = frozenset(
    {
        "PENDING",
        "RETRYABLE",
        "IN_FLIGHT",
        "UNCERTAIN",
        "SENT",
        "RESERVED",
    }
)
AUDIT_ONLY_ATTEMPT_STATUSES = frozenset({"skipped", "blocked", "failed"})


def canonical_operational_symbol(value: Any) -> str:
    text = str(value or "").strip().upper()
    if not text or text.upper() == NA:
        return ""
    return text


def canonical_operational_direction(value: Any) -> str:
    text = str(value or "").strip().lower()
    if not text or text.upper() == NA:
        return ""
    if text in {"long", "buy"}:
        return "long"
    if text in {"short", "sell"}:
        return "short"
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
    if not str(existing_origin or "").strip():
        raise RuntimeEpochOriginError(
            "Malformed current-epoch lifecycle is missing a granted creation origin."
        )
    if creation_origin_id and existing_origin and str(creation_origin_id) != str(existing_origin):
        raise RuntimeEpochOwnershipError("Creation origin is immutable.")
    origin_id = str(existing_origin).strip()
    owned_symbol = record_symbol or canonical_operational_symbol(existing["symbol"])
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
    event_side = canonical_operational_direction(_field(event, "side"))
    lifecycle_direction = canonical_operational_direction(lifecycle["direction"])
    if not event_side or not lifecycle_direction or event_side != lifecycle_direction:
        return "public_event_direction_mismatch"
    root_reason = public_root_relationship_reason(connection, event, epoch=epoch)
    if root_reason is not None:
        return root_reason
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
    expected_direction: str | None = None,
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
    expected_dir = canonical_operational_direction(expected_direction)
    actual_dir = canonical_operational_direction(owned["direction"])
    if expected_dir and actual_dir and expected_dir != actual_dir:
        raise RuntimeEpochOwnershipError("Public intent lifecycle direction does not match the event side.")
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
    if attempt_is_audit_only(attempt):
        raise RuntimeEpochOwnershipError("public_attempt_is_audit_only")
    canonical = canonical_reservation_attempt_id(event)
    if canonical is not None and int(canonical) != int(attempt_id):
        raise RuntimeEpochOwnershipError("public_attempt_not_canonical")
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


def normalize_public_event_type(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in text:
        text = text.replace("__", "_")
    text = text.strip("_")
    if text == "watchlist":
        return "initial_watchlist"
    if text in {"invalidation", "invalid"}:
        return "invalidated"
    if text in {"expiry", "watchlist_expiry"}:
        return "expired"
    return text


def public_root_relationship_reason(
    connection: sqlite3.Connection,
    event: sqlite3.Row | Any,
    *,
    epoch: RuntimeEpochRecord,
) -> str | None:
    event_type = normalize_public_event_type(_field(event, "event_type"))
    root_id = _optional_int(_field(event, "origin_root_event_id"))
    if event_type not in PUBLIC_EVENT_FAMILY_TYPES:
        return "public_event_family_unknown"
    if event_type in PUBLIC_ROOT_EVENT_TYPES:
        if root_id is not None:
            return "public_root_event_must_not_have_predecessor"
        return None
    if event_type in PUBLIC_CHAIN_STARTER_EVENT_TYPES and root_id is None:
        return None
    if root_id is None:
        return "public_followup_missing_required_root"
    root = connection.execute(
        "SELECT * FROM public_alert_events WHERE id = ?",
        (root_id,),
    ).fetchone()
    if root is None:
        return "public_followup_root_missing"
    return public_root_event_mismatch_reason(connection, event, root, epoch=epoch)


def public_root_event_mismatch_reason(
    connection: sqlite3.Connection,
    event: sqlite3.Row | Any,
    root: sqlite3.Row | Any,
    *,
    epoch: RuntimeEpochRecord,
) -> str | None:
    root_epoch = public_event_epoch_id(root)
    if root_epoch is None:
        return "public_followup_root_legacy_or_unattributed"
    if root_epoch != epoch.epoch_id:
        return "public_followup_root_epoch_mismatch"
    root_type = normalize_public_event_type(_field(root, "event_type"))
    if root_type not in PUBLIC_CHAIN_STARTER_EVENT_TYPES:
        return "public_followup_root_incompatible_family"
    event_lifecycle = _optional_text(_field(event, "origin_lifecycle_id"))
    root_lifecycle = _optional_text(_field(root, "origin_lifecycle_id"))
    if not event_lifecycle or event_lifecycle != root_lifecycle:
        return "public_followup_root_lifecycle_mismatch"
    event_symbol = canonical_operational_symbol(_field(event, "symbol"))
    root_symbol = canonical_operational_symbol(_field(root, "symbol"))
    if not event_symbol or event_symbol != root_symbol:
        return "public_followup_root_symbol_mismatch"
    event_side = canonical_operational_direction(_field(event, "side"))
    root_side = canonical_operational_direction(_field(root, "side"))
    if not event_side or event_side != root_side:
        return "public_followup_root_side_mismatch"
    try:
        require_lifecycle_public_intent(
            connection,
            origin_lifecycle_id=str(root_lifecycle),
            epoch=epoch,
            expected_symbol=root_symbol,
            expected_direction=root_side,
        )
    except (RuntimeEpochOwnershipError, RuntimeEpochOriginError) as exc:
        return str(exc) or "public_followup_root_lifecycle_unowned"
    return None


def require_public_root_for_insert(
    connection: sqlite3.Connection,
    *,
    event_type: str,
    origin_root_event_id: int | None,
    origin_lifecycle_id: str,
    symbol: str,
    side: str,
    canonical_plan_id: str | None,
    epoch: RuntimeEpochRecord,
) -> int | None:
    normalized_type = normalize_public_event_type(event_type)
    if normalized_type not in PUBLIC_EVENT_FAMILY_TYPES:
        raise RuntimeEpochOwnershipError("public_event_family_unknown")
    require_lifecycle_public_intent(
        connection,
        origin_lifecycle_id=origin_lifecycle_id,
        epoch=epoch,
        expected_symbol=symbol,
        expected_direction=side,
    )
    if normalized_type in PUBLIC_ROOT_EVENT_TYPES:
        if origin_root_event_id is not None:
            raise RuntimeEpochOwnershipError("public_root_event_must_not_have_predecessor")
        return None
    root_id = origin_root_event_id
    if root_id is None:
        root_id = _lookup_owned_public_root_id(
            connection,
            origin_lifecycle_id=origin_lifecycle_id,
            symbol=symbol,
            side=side,
            canonical_plan_id=canonical_plan_id,
            epoch=epoch,
        )
    if root_id is None and normalized_type in PUBLIC_CHAIN_STARTER_EVENT_TYPES:
        return None
    if root_id is None:
        raise RuntimeEpochOwnershipError("public_followup_missing_required_root")
    root = connection.execute(
        "SELECT * FROM public_alert_events WHERE id = ?",
        (int(root_id),),
    ).fetchone()
    if root is None:
        raise RuntimeEpochOwnershipError("public_followup_root_missing")
    synthetic_event = {
        "event_type": normalized_type,
        "origin_root_event_id": int(root_id),
        "origin_lifecycle_id": origin_lifecycle_id,
        "symbol": symbol,
        "side": side,
        "canonical_plan_id": canonical_plan_id,
        "runtime_epoch_id": epoch.epoch_id,
    }
    reason = public_root_event_mismatch_reason(connection, synthetic_event, root, epoch=epoch)
    if reason is not None:
        raise RuntimeEpochOwnershipError(reason)
    return int(root_id)


def require_event_reservation_association(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    reservation_id: int,
    epoch: RuntimeEpochRecord | None = None,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    event = connection.execute(
        "SELECT * FROM public_alert_events WHERE id = ?",
        (int(event_id),),
    ).fetchone()
    require_public_event_mutation(connection, event, epoch=epoch)
    assert event is not None
    reservation = connection.execute(
        "SELECT * FROM telegram_alert_attempts WHERE id = ?",
        (int(reservation_id),),
    ).fetchone()
    if reservation is None:
        raise RuntimeEpochOwnershipError("public_reservation_missing")
    event_key = _optional_text(event["event_key"])
    reservation_key = _optional_text(reservation["public_watchlist_event_key"])
    if event_key is None or reservation_key is None or event_key != reservation_key:
        raise RuntimeEpochOwnershipError("public_reservation_event_mismatch")
    reservation_symbol = canonical_operational_symbol(reservation["symbol"])
    event_symbol = canonical_operational_symbol(event["symbol"])
    if reservation_symbol and event_symbol and reservation_symbol != event_symbol:
        raise RuntimeEpochOwnershipError("public_reservation_symbol_mismatch")
    reservation_dir = canonical_operational_direction(reservation["direction"])
    event_side = canonical_operational_direction(event["side"])
    if reservation_dir and event_side and reservation_dir != event_side:
        raise RuntimeEpochOwnershipError("public_reservation_direction_mismatch")
    if attempt_is_audit_only(reservation):
        raise RuntimeEpochOwnershipError("public_reservation_is_audit_only")
    canonical = canonical_reservation_attempt_id(event)
    if canonical is None:
        raise RuntimeEpochOwnershipError("public_canonical_reservation_missing")
    if int(canonical) != int(reservation_id):
        raise RuntimeEpochOwnershipError("public_reservation_not_canonical")
    mismatch = public_reservation_record_mismatch_reason(
        connection,
        reservation,
        event,
        epoch=epoch,
    )
    if mismatch is not None:
        raise RuntimeEpochOwnershipError(mismatch)
    return event, reservation


def require_canonical_reservation_for_public_effect(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    epoch: RuntimeEpochRecord | None = None,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    """Resolve the persisted canonical pointer and require identity plus semantics."""

    event = connection.execute(
        "SELECT * FROM public_alert_events WHERE id = ?",
        (int(event_id),),
    ).fetchone()
    if event is None:
        raise RuntimeEpochOwnershipError("public_event_missing")
    canonical = canonical_reservation_attempt_id(event)
    if canonical is None:
        raise RuntimeEpochOwnershipError("public_canonical_reservation_missing")
    return require_event_reservation_association(
        connection,
        event_id=int(event_id),
        reservation_id=int(canonical),
        epoch=epoch,
    )


def require_event_part_association(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    part_id: int,
    epoch: RuntimeEpochRecord | None = None,
) -> tuple[sqlite3.Row, sqlite3.Row]:
    event = connection.execute(
        "SELECT * FROM public_alert_events WHERE id = ?",
        (int(event_id),),
    ).fetchone()
    require_public_event_mutation(connection, event, epoch=epoch)
    assert event is not None
    part = connection.execute(
        "SELECT * FROM public_alert_delivery_parts WHERE id = ?",
        (int(part_id),),
    ).fetchone()
    if part is None:
        raise RuntimeEpochOwnershipError("public_delivery_part_missing")
    if int(part["public_alert_event_id"]) != int(event_id):
        raise RuntimeEpochOwnershipError("public_delivery_part_event_mismatch")
    part_key = _optional_text(part["event_key"])
    event_key = _optional_text(event["event_key"])
    if part_key is not None and event_key is not None and part_key != event_key:
        raise RuntimeEpochOwnershipError("public_delivery_part_event_key_mismatch")
    return event, part


def require_part_claim_association(
    connection: sqlite3.Connection,
    *,
    part_id: int,
    attempt_id: str,
    epoch: RuntimeEpochRecord | None = None,
) -> sqlite3.Row:
    event = require_public_part_mutation(connection, int(part_id), epoch=epoch)
    claim = _optional_text(event["attempt_id"]) if "attempt_id" in event.keys() else None
    supplied = str(attempt_id or "").strip()
    if not supplied or claim is None or supplied != claim:
        raise RuntimeEpochOwnershipError("public_part_claim_mismatch")
    return event


def attempt_has_operational_history(row: sqlite3.Row | Any) -> bool:
    status = str(_field(row, "telegram_status") or "").strip().lower()
    delivery = str(_field(row, "delivery_state") or "").strip().upper()
    if status in OPERATIONAL_ATTEMPT_STATUSES:
        return True
    if delivery in OPERATIONAL_DELIVERY_STATES:
        return True
    return False


def attempt_is_audit_only(row: sqlite3.Row | Any) -> bool:
    status = str(_field(row, "telegram_status") or "").strip().lower()
    delivery = str(_field(row, "delivery_state") or "").strip().upper()
    if delivery in OPERATIONAL_DELIVERY_STATES:
        return False
    return status in AUDIT_ONLY_ATTEMPT_STATUSES


def canonical_reservation_attempt_id(event: sqlite3.Row | Any | None) -> int | None:
    return _optional_int(_field(event, "canonical_reservation_attempt_id"))


def bind_canonical_reservation_attempt(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    attempt_id: int,
) -> None:
    """Stamp canonical reservation only when the owned event has none. Never backfill."""

    connection.execute(
        """
        UPDATE public_alert_events
        SET canonical_reservation_attempt_id = ?
        WHERE id = ?
          AND canonical_reservation_attempt_id IS NULL
        """,
        (int(attempt_id), int(event_id)),
    )


def public_reservation_record_mismatch_reason(
    connection: sqlite3.Connection,
    record: sqlite3.Row | Any,
    event: sqlite3.Row | Any,
    *,
    epoch: RuntimeEpochRecord | None = None,
) -> str | None:
    loaded = epoch or require_active_runtime_epoch(connection)
    chain_reason = public_ownership_chain_reason(connection, event, epoch=loaded)
    if chain_reason is not None:
        return chain_reason
    record_key = _optional_text(_field(record, "public_watchlist_event_key"))
    event_key = _optional_text(_field(event, "event_key"))
    if record_key is None or event_key is None or record_key != event_key:
        return "public_reservation_event_mismatch"
    record_symbol = canonical_operational_symbol(_field(record, "symbol"))
    event_symbol = canonical_operational_symbol(_field(event, "symbol"))
    if not record_symbol or record_symbol != event_symbol:
        return "public_reservation_symbol_mismatch"
    record_dir = canonical_operational_direction(_field(record, "direction"))
    event_side = canonical_operational_direction(_field(event, "side"))
    if not record_dir or record_dir != event_side:
        return "public_reservation_direction_mismatch"
    event_plan = _optional_text(_field(event, "canonical_plan_id"))
    record_plan = _optional_text(_field(record, "public_watchlist_plan_id"))
    if event_plan is None or record_plan is None or record_plan != event_plan:
        return "public_reservation_plan_mismatch"
    family_reason = _reservation_event_family_mismatch_reason(record, event)
    if family_reason is not None:
        return family_reason
    origin_lifecycle_id = _optional_text(_field(event, "origin_lifecycle_id"))
    if origin_lifecycle_id is None:
        return "public_event_missing_origin_lifecycle"
    try:
        lifecycle = require_lifecycle_public_intent(
            connection,
            origin_lifecycle_id=origin_lifecycle_id,
            epoch=loaded,
            expected_symbol=event_symbol,
            expected_direction=event_side,
        )
    except (RuntimeEpochOwnershipError, RuntimeEpochOriginError) as exc:
        return str(exc) or "public_reservation_lifecycle_unowned"
    economic_reason = _reservation_economic_mismatch_reason(record, event, lifecycle)
    if economic_reason is not None:
        return economic_reason
    scan_run_reason = _reservation_scan_run_mismatch_reason(connection, record, epoch=loaded)
    if scan_run_reason is not None:
        return scan_run_reason
    return None


_RESERVATION_EVENT_TYPE_FIELDS = (
    ("public_alert_event_type", "public_reservation_event_family"),
    ("alert_type", "public_reservation_alert_type"),
    ("attempted_alert_type", "public_reservation_attempted_alert_type"),
)

_RESERVATION_EVENT_PRICE_FIELDS = (
    ("entry_low", "raw_entry_low"),
    ("entry_high", "raw_entry_high"),
    ("stop_loss", "raw_stop_loss"),
    ("normalized_entry_zone_low", "normalized_zone_low"),
    ("normalized_entry_zone_high", "normalized_zone_high"),
)

_RESERVATION_LIFECYCLE_PRICE_FIELDS = (
    ("entry_low", "entry_low"),
    ("entry_high", "entry_high"),
    ("stop_loss", "stop_loss"),
    ("tp1", "tp1"),
    ("tp2", "tp2"),
    ("tp3", "tp3"),
)


def _reservation_event_family_mismatch_reason(
    record: sqlite3.Row | Any,
    event: sqlite3.Row | Any,
) -> str | None:
    event_type = normalize_public_event_type(_field(event, "event_type"))
    if not event_type or event_type not in PUBLIC_EVENT_FAMILY_TYPES:
        return "public_event_family_unknown"
    saw_representation = False
    for field_name, reason_prefix in _RESERVATION_EVENT_TYPE_FIELDS:
        text = _optional_text(_field(record, field_name))
        if text is None:
            continue
        saw_representation = True
        normalized = normalize_public_event_type(text)
        if normalized not in PUBLIC_EVENT_FAMILY_TYPES:
            return f"{reason_prefix}_unknown"
        if normalized != event_type:
            return f"{reason_prefix}_mismatch"
    if not saw_representation:
        return "public_reservation_event_family_unknown"
    return None


def _reservation_economic_mismatch_reason(
    record: sqlite3.Row | Any,
    event: sqlite3.Row | Any,
    lifecycle: sqlite3.Row | Any,
) -> str | None:
    for record_field, event_field in _RESERVATION_EVENT_PRICE_FIELDS:
        if _economic_values_conflict(_field(record, record_field), _field(event, event_field)):
            return f"public_reservation_{record_field}_mismatch"
    if _optional_text(_field(record, "normalized_invalidation")) and _optional_text(
        _field(event, "normalized_invalidation")
    ):
        if _optional_text(_field(record, "normalized_invalidation")) != _optional_text(
            _field(event, "normalized_invalidation")
        ):
            return "public_reservation_normalized_invalidation_mismatch"
    for record_field, lifecycle_field in _RESERVATION_LIFECYCLE_PRICE_FIELDS:
        if _economic_values_conflict(_field(record, record_field), _field(lifecycle, lifecycle_field)):
            return f"public_reservation_{record_field}_mismatch"
    if _economic_values_conflict(_field(record, "rr_planned"), _field(lifecycle, "rr")):
        return "public_reservation_rr_planned_mismatch"
    record_setup = _optional_text(_field(record, "setup_id"))
    lifecycle_setup = _optional_text(_field(lifecycle, "setup_id"))
    if record_setup is not None and lifecycle_setup is not None and record_setup != lifecycle_setup:
        return "public_reservation_setup_mismatch"
    record_plan_version = _optional_text(_field(record, "plan_version_id"))
    lifecycle_plan_version = _optional_text(_field(lifecycle, "plan_version_id"))
    if (
        record_plan_version is not None
        and lifecycle_plan_version is not None
        and record_plan_version != lifecycle_plan_version
    ):
        return "public_reservation_plan_version_mismatch"
    return None


def _reservation_scan_run_mismatch_reason(
    connection: sqlite3.Connection,
    record: sqlite3.Row | Any,
    *,
    epoch: RuntimeEpochRecord | None = None,
) -> str | None:
    scan_run_id = _optional_text(_field(record, "scan_run_id"))
    if scan_run_id is None:
        return None
    if not operational_run_belongs_to_active_epoch(connection, scan_run_id, epoch=epoch):
        return "public_reservation_scan_run_unregistered"
    return None


def _economic_values_conflict(left: Any, right: Any) -> bool:
    left_price = canonical_stored_price(left)
    right_price = canonical_stored_price(right)
    if left_price == NA or right_price == NA:
        return False
    return left_price != right_price


def operational_run_belongs_to_active_epoch(
    connection: sqlite3.Connection,
    run_id: Any,
    *,
    epoch: RuntimeEpochRecord | None = None,
) -> bool:
    loaded = epoch or load_active_runtime_epoch(connection)
    if loaded is None:
        return False
    normalized = _optional_text(run_id)
    if normalized is None:
        return False
    row = connection.execute(
        "SELECT runtime_epoch_id FROM runtime_operational_runs WHERE run_id = ?",
        (normalized,),
    ).fetchone()
    return row is not None and str(row["runtime_epoch_id"]) == loaded.epoch_id


_ENRICHMENT_RANGE_SEPARATORS = ("–", "—", "-", "/")
_ENRICHMENT_DIRECTION_FIELDS = ("direction", "side", "bias")
_ENRICHMENT_MODE_FIELDS = ("mode", "strategy_mode", "source_mode")
_ENRICHMENT_SETUP_FIELDS = ("setup_id",)
_ENRICHMENT_PLAN_VERSION_FIELDS = ("plan_version_id",)
_ENRICHMENT_PLAN_ID_FIELDS = (
    "public_watchlist_plan_id",
    "canonical_plan_id",
    "plan_id",
)
_ENRICHMENT_LIFECYCLE_FIELDS = ("lifecycle_id",)
_ENRICHMENT_ENTRY_LOW_FIELDS = ("entry_low", "raw_entry_low", "normalized_entry_zone_low")
_ENRICHMENT_ENTRY_HIGH_FIELDS = ("entry_high", "raw_entry_high", "normalized_entry_zone_high")
_ENRICHMENT_ENTRY_ZONE_FIELDS = ("entry", "entry_zone")
_ENRICHMENT_STOP_FIELDS = ("stop_loss", "stop", "raw_stop_loss")
_ENRICHMENT_TP_FIELDS = (
    ("tp1", ("tp1",)),
    ("tp2", ("tp2",)),
    ("tp3", ("tp3",)),
)


def active_enrichment_belongs_to_owned_chain(
    connection: sqlite3.Connection,
    *,
    enrichment: Mapping[str, Any] | sqlite3.Row | None,
    raw: Mapping[str, Any] | None,
    lifecycle_row: Mapping[str, Any] | sqlite3.Row | None,
    attempt_row: Mapping[str, Any] | sqlite3.Row | None,
    epoch: RuntimeEpochRecord | None = None,
) -> bool:
    """Exact ACTIVE provenance: every present identity/economic representation must agree.

    ``connection`` and ``epoch`` remain part of the authority signature so callers
    pass the same owned-chain context used by reservation validation. A present
    raw/candidate ``run_id`` is never sufficient to grant enrichment, and an
    unregistered diagnostic run_id is not by itself a reject.
    """

    _ = (connection, epoch)
    row = _as_mapping(enrichment)
    payload = _as_mapping(raw)
    lifecycle = _as_mapping(lifecycle_row)
    attempt = _as_mapping(attempt_row)
    trade_idea = _as_mapping(payload.get("trade_idea"))
    sources = (row, payload, trade_idea)
    if _enrichment_symbols_conflict(sources, lifecycle, attempt):
        return False
    if _enrichment_directions_conflict(sources, lifecycle, attempt):
        return False
    if _enrichment_modes_conflict(sources, lifecycle):
        return False
    if _enrichment_identity_values_conflict(sources, lifecycle, _ENRICHMENT_LIFECYCLE_FIELDS, "lifecycle_id"):
        return False
    if _enrichment_identity_values_conflict(sources, lifecycle, _ENRICHMENT_SETUP_FIELDS, "setup_id"):
        return False
    if _enrichment_identity_values_conflict(sources, lifecycle, _ENRICHMENT_PLAN_VERSION_FIELDS, "plan_version_id"):
        return False
    owned_plan = _optional_text(attempt.get("public_watchlist_plan_id"))
    present_plans = _present_texts(sources, _ENRICHMENT_PLAN_ID_FIELDS)
    if len(set(present_plans)) > 1:
        return False
    if owned_plan is not None and present_plans and any(value != owned_plan for value in present_plans):
        return False
    if _enrichment_economics_conflict(sources, lifecycle, attempt):
        return False
    return _enrichment_has_exact_owned_association(
        sources,
        lifecycle=lifecycle,
        owned_plan=owned_plan,
        attempt=attempt,
    )


def _as_mapping(value: Mapping[str, Any] | sqlite3.Row | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    try:
        return dict(value)
    except (TypeError, ValueError):
        return {}


def _present_texts(
    sources: tuple[Mapping[str, Any], ...],
    field_names: tuple[str, ...],
) -> tuple[str, ...]:
    values: list[str] = []
    for source in sources:
        for field_name in field_names:
            text = _optional_text(source.get(field_name))
            if text is not None:
                values.append(text)
    return tuple(values)


def _enrichment_symbols_conflict(
    sources: tuple[Mapping[str, Any], ...],
    lifecycle: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> bool:
    present = tuple(
        symbol
        for source in sources
        for field_name in ("symbol",)
        if (symbol := canonical_operational_symbol(source.get(field_name)))
    )
    if len(set(present)) > 1:
        return True
    chain = canonical_operational_symbol(lifecycle.get("symbol")) or canonical_operational_symbol(
        attempt.get("symbol")
    )
    return bool(chain and present and any(value != chain for value in present))


def _enrichment_directions_conflict(
    sources: tuple[Mapping[str, Any], ...],
    lifecycle: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> bool:
    present = tuple(
        direction
        for source in sources
        for field_name in _ENRICHMENT_DIRECTION_FIELDS
        if (direction := canonical_operational_direction(source.get(field_name)))
    )
    if len(set(present)) > 1:
        return True
    chain = canonical_operational_direction(lifecycle.get("direction")) or canonical_operational_direction(
        attempt.get("direction")
    )
    return bool(chain and present and any(value != chain for value in present))


def _enrichment_modes_conflict(
    sources: tuple[Mapping[str, Any], ...],
    lifecycle: Mapping[str, Any],
) -> bool:
    present = tuple(value.lower() for value in _present_texts(sources, _ENRICHMENT_MODE_FIELDS))
    if len(set(present)) > 1:
        return True
    owned = _optional_text(lifecycle.get("mode"))
    if owned is None:
        return False
    owned_key = owned.lower()
    return bool(present and any(value != owned_key for value in present))


def _enrichment_identity_values_conflict(
    sources: tuple[Mapping[str, Any], ...],
    lifecycle: Mapping[str, Any],
    field_names: tuple[str, ...],
    owned_field: str,
) -> bool:
    present = _present_texts(sources, field_names)
    if len(set(present)) > 1:
        return True
    owned = _optional_text(lifecycle.get(owned_field))
    return bool(owned is not None and present and any(value != owned for value in present))


def _canonical_price_tuple(value: Any) -> tuple[str, ...]:
    text = _optional_text(value)
    if text is None:
        return ()
    for separator in _ENRICHMENT_RANGE_SEPARATORS:
        if separator in text:
            left, right = text.split(separator, 1)
            prices: list[str] = []
            for part in (left, right):
                price = canonical_stored_price(part)
                if price != NA:
                    prices.append(price)
            return tuple(prices)
    price = canonical_stored_price(text)
    return (price,) if price != NA else ()


def _merge_price_tuples(values: tuple[tuple[str, ...], ...]) -> tuple[str, ...] | None:
    merged: tuple[str, ...] = ()
    for prices in values:
        if not prices:
            continue
        if not merged:
            merged = prices
            continue
        if _price_tuples_conflict(merged, prices):
            return None
        if len(prices) > len(merged):
            merged = prices
    return merged


def _price_tuples_conflict(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    if not left or not right:
        return False
    if left == right:
        return False
    if len(left) == 2 and len(right) == 2:
        return tuple(sorted(left)) != tuple(sorted(right))
    if len(left) == 1 and len(right) == 1:
        return left[0] != right[0]
    if len(left) == 1:
        return left[0] not in right
    if len(right) == 1:
        return right[0] not in left
    return True


def _collect_source_prices(
    sources: tuple[Mapping[str, Any], ...],
    field_names: tuple[str, ...],
) -> tuple[str, ...] | None:
    collected = tuple(
        _canonical_price_tuple(source.get(field_name))
        for source in sources
        for field_name in field_names
    )
    if any(
        prices and other and _price_tuples_conflict(prices, other)
        for index, prices in enumerate(collected)
        for other in collected[index + 1 :]
    ):
        return None
    merged = _merge_price_tuples(collected)
    if merged is None:
        return None
    return merged


def _enrichment_economics_conflict(
    sources: tuple[Mapping[str, Any], ...],
    lifecycle: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> bool:
    chain_low = _merge_price_tuples(
        (_canonical_price_tuple(lifecycle.get("entry_low")), _canonical_price_tuple(attempt.get("entry_low")))
    )
    chain_high = _merge_price_tuples(
        (_canonical_price_tuple(lifecycle.get("entry_high")), _canonical_price_tuple(attempt.get("entry_high")))
    )
    if chain_low is None or chain_high is None:
        return True
    present_low = _collect_source_prices(sources, _ENRICHMENT_ENTRY_LOW_FIELDS)
    present_high = _collect_source_prices(sources, _ENRICHMENT_ENTRY_HIGH_FIELDS)
    present_zone = _collect_source_prices(sources, _ENRICHMENT_ENTRY_ZONE_FIELDS)
    if present_low is None or present_high is None or present_zone is None:
        return True
    if present_zone:
        if len(present_zone) >= 2:
            zone_low = (present_zone[0],)
            zone_high = (present_zone[-1],)
            if present_low and _price_tuples_conflict(present_low, zone_low):
                return True
            if present_high and _price_tuples_conflict(present_high, zone_high):
                return True
            if chain_low and _price_tuples_conflict(zone_low, chain_low):
                return True
            if chain_high and _price_tuples_conflict(zone_high, chain_high):
                return True
        else:
            if present_low and _price_tuples_conflict(present_low, present_zone):
                return True
            chain_entries = tuple(dict.fromkeys((*chain_low, *chain_high)))
            if chain_entries and present_zone[0] not in chain_entries:
                return True
    if present_low and chain_low and _price_tuples_conflict(present_low, chain_low):
        return True
    if present_high and chain_high and _price_tuples_conflict(present_high, chain_high):
        return True
    present_stop = _collect_source_prices(sources, _ENRICHMENT_STOP_FIELDS)
    if present_stop is None:
        return True
    chain_stop = _merge_price_tuples(
        (_canonical_price_tuple(lifecycle.get("stop_loss")), _canonical_price_tuple(attempt.get("stop_loss")))
    )
    if chain_stop is None:
        return True
    if present_stop and chain_stop and _price_tuples_conflict(present_stop, chain_stop):
        return True
    for chain_field, source_fields in _ENRICHMENT_TP_FIELDS:
        present = _collect_source_prices(sources, source_fields)
        if present is None:
            return True
        chain_value = _merge_price_tuples(
            (
                _canonical_price_tuple(lifecycle.get(chain_field)),
                _canonical_price_tuple(attempt.get(chain_field)),
            )
        )
        if chain_value is None:
            return True
        if present and chain_value and _price_tuples_conflict(present, chain_value):
            return True
    return False


def _enrichment_has_exact_owned_association(
    sources: tuple[Mapping[str, Any], ...],
    *,
    lifecycle: Mapping[str, Any],
    attempt: Mapping[str, Any],
    owned_plan: str | None,
) -> bool:
    owned_lifecycle = _optional_text(lifecycle.get("lifecycle_id"))
    present_lifecycle = _present_texts(sources, _ENRICHMENT_LIFECYCLE_FIELDS)
    if owned_lifecycle is not None and owned_lifecycle in present_lifecycle:
        return True
    owned_setup = _optional_text(lifecycle.get("setup_id"))
    present_setup = _present_texts(sources, _ENRICHMENT_SETUP_FIELDS)
    if owned_setup is not None and owned_setup in present_setup:
        return True
    owned_plan_version = _optional_text(lifecycle.get("plan_version_id"))
    present_plan_version = _present_texts(sources, _ENRICHMENT_PLAN_VERSION_FIELDS)
    if owned_plan_version is not None and owned_plan_version in present_plan_version:
        return True
    if owned_plan is not None and owned_plan in _present_texts(sources, _ENRICHMENT_PLAN_ID_FIELDS):
        return True
    return _enrichment_matches_owned_economic_identity(sources, lifecycle, attempt)


def _enrichment_matches_owned_economic_identity(
    sources: tuple[Mapping[str, Any], ...],
    lifecycle: Mapping[str, Any],
    attempt: Mapping[str, Any],
) -> bool:
    owned_mode = _optional_text(lifecycle.get("mode"))
    present_modes = tuple(value.lower() for value in _present_texts(sources, _ENRICHMENT_MODE_FIELDS))
    if owned_mode is None or not present_modes:
        return False
    chain_low = _merge_price_tuples(
        (_canonical_price_tuple(lifecycle.get("entry_low")), _canonical_price_tuple(attempt.get("entry_low")))
    ) or ()
    chain_high = _merge_price_tuples(
        (_canonical_price_tuple(lifecycle.get("entry_high")), _canonical_price_tuple(attempt.get("entry_high")))
    ) or ()
    chain_stop = _merge_price_tuples(
        (_canonical_price_tuple(lifecycle.get("stop_loss")), _canonical_price_tuple(attempt.get("stop_loss")))
    ) or ()
    chain_tp1 = _merge_price_tuples(
        (_canonical_price_tuple(lifecycle.get("tp1")), _canonical_price_tuple(attempt.get("tp1")))
    ) or ()
    chain_tp2 = _merge_price_tuples(
        (_canonical_price_tuple(lifecycle.get("tp2")), _canonical_price_tuple(attempt.get("tp2")))
    ) or ()
    chain_tp3 = _merge_price_tuples(
        (_canonical_price_tuple(lifecycle.get("tp3")), _canonical_price_tuple(attempt.get("tp3")))
    ) or ()
    if not (chain_low and chain_high and chain_stop and chain_tp1 and chain_tp2 and chain_tp3):
        return False
    present_low = _collect_source_prices(sources, _ENRICHMENT_ENTRY_LOW_FIELDS) or ()
    present_high = _collect_source_prices(sources, _ENRICHMENT_ENTRY_HIGH_FIELDS) or ()
    present_zone = _collect_source_prices(sources, _ENRICHMENT_ENTRY_ZONE_FIELDS) or ()
    present_stop = _collect_source_prices(sources, _ENRICHMENT_STOP_FIELDS) or ()
    present_tp1 = _collect_source_prices(sources, ("tp1",)) or ()
    present_tp2 = _collect_source_prices(sources, ("tp2",)) or ()
    present_tp3 = _collect_source_prices(sources, ("tp3",)) or ()
    if present_zone and len(present_zone) >= 2:
        present_low = present_low or (present_zone[0],)
        present_high = present_high or (present_zone[-1],)
    if not (present_low and present_high and present_stop and present_tp1 and present_tp2 and present_tp3):
        return False
    return not (
        _price_tuples_conflict(present_low, chain_low)
        or _price_tuples_conflict(present_high, chain_high)
        or _price_tuples_conflict(present_stop, chain_stop)
        or _price_tuples_conflict(present_tp1, chain_tp1)
        or _price_tuples_conflict(present_tp2, chain_tp2)
        or _price_tuples_conflict(present_tp3, chain_tp3)
    )


def attempt_has_current_epoch_run_lineage(
    connection: sqlite3.Connection,
    row: sqlite3.Row | Any,
    *,
    epoch: RuntimeEpochRecord | None = None,
) -> bool:
    loaded = epoch or load_active_runtime_epoch(connection)
    if loaded is None:
        return False
    for field_name in ("last_scan_run_id", "scan_run_id"):
        if operational_run_belongs_to_active_epoch(
            connection,
            _field(row, field_name),
            epoch=loaded,
        ):
            return True
    return False


def _lookup_owned_public_root_id(
    connection: sqlite3.Connection,
    *,
    origin_lifecycle_id: str,
    symbol: str,
    side: str,
    canonical_plan_id: str | None,
    epoch: RuntimeEpochRecord,
) -> int | None:
    lifecycle_id = str(origin_lifecycle_id or "").strip()
    if not lifecycle_id:
        return None
    rows = connection.execute(
        """
        SELECT * FROM public_alert_events
        WHERE origin_lifecycle_id = ?
          AND runtime_epoch_id = ?
          AND symbol = ?
          AND side = ?
        ORDER BY id ASC
        """,
        (
            lifecycle_id,
            epoch.epoch_id,
            canonical_operational_symbol(symbol),
            canonical_operational_direction(side),
        ),
    ).fetchall()
    expected_plan = _optional_text(canonical_plan_id)
    best_id: int | None = None
    best_rank = len(_CHAIN_STARTER_LOOKUP_ORDER)
    for row in rows:
        event_type = normalize_public_event_type(row["event_type"])
        if event_type not in PUBLIC_CHAIN_STARTER_EVENT_TYPES:
            continue
        if public_root_event_mismatch_reason(connection, row, row, epoch=epoch) is not None:
            continue
        if expected_plan:
            row_plan = _optional_text(row["canonical_plan_id"])
            if row_plan and row_plan != expected_plan:
                continue
        try:
            rank = _CHAIN_STARTER_LOOKUP_ORDER.index(event_type)
        except ValueError:
            rank = len(_CHAIN_STARTER_LOOKUP_ORDER)
        if rank < best_rank:
            best_id = int(row["id"])
            best_rank = rank
            if rank == 0:
                break
    return best_id


def _optional_int(value: Any) -> int | None:
    if value in (None, "", NA):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
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
