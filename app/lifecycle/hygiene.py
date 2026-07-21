from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any

from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleState, SetupTransitionReason
from app.lifecycle.outcome_policy import canonical_plan_identity, stored_plan_geometry_failure
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.state_machine import transition_allowed, transition_record
from app.storage.database import SCHEMA_VERSION


QUARANTINE_REASON_CODE = SetupTransitionReason.LEGACY_INVALID_STORED_PLAN_GEOMETRY.value
OUTCOME_ELIGIBLE_STATES = frozenset(
    {
        SetupLifecycleState.WATCHLISTED.value,
        SetupLifecycleState.STALKING.value,
        SetupLifecycleState.TRIGGERED.value,
        SetupLifecycleState.CONFIRMED.value,
        SetupLifecycleState.ACTIONABLE_A_GRADE.value,
        SetupLifecycleState.A_GRADE_WATCH.value,
        SetupLifecycleState.EXECUTING.value,
        SetupLifecycleState.MANAGING.value,
    }
)
SAFE_QUARANTINE_TRANSITIONS = {
    SetupLifecycleState.WATCHLISTED.value: SetupLifecycleState.REJECTED,
    SetupLifecycleState.STALKING.value: SetupLifecycleState.REJECTED,
    SetupLifecycleState.TRIGGERED.value: SetupLifecycleState.INVALIDATED,
    SetupLifecycleState.CONFIRMED.value: SetupLifecycleState.INVALIDATED,
    SetupLifecycleState.ACTIONABLE_A_GRADE.value: SetupLifecycleState.INVALIDATED,
    SetupLifecycleState.A_GRADE_WATCH.value: SetupLifecycleState.INVALIDATED,
}
REQUIRED_TABLES = frozenset(
    {
        "setup_lifecycle_records",
        "setup_lifecycle_events",
        "setup_lifecycle_outcome_progress",
        "telegram_alert_attempts",
        "public_alert_events",
        "public_alert_delivery_parts",
    }
)
REQUIRED_INDEXES = frozenset(
    {
        "ix_lifecycle_records_symbol_mode_direction",
        "ix_lifecycle_events_lifecycle_id",
        "ix_lifecycle_outcome_progress_active_plan",
        "ix_telegram_alert_attempts_signal",
        "ix_public_alert_events_status",
        "ix_public_alert_delivery_parts_event",
    }
)
PLAN_CONDITION_COLUMNS = (
    "symbol",
    "mode",
    "direction",
    "entry_low",
    "entry_high",
    "stop_loss",
    "tp1",
    "tp2",
    "tp3",
    "invalidation_logic",
    "invalidation_reason",
    "setup_identity",
)
MARKET_PROGRESS_COLUMNS = (
    "evaluation_cursor_open_at",
    "evaluation_cursor_close_at",
    "entry_at",
    "tp1_at",
    "tp2_at",
    "tp3_at",
    "stop_at",
    "invalidated_at",
    "outcome_at",
)


class LifecycleHygieneError(RuntimeError):
    """The requested audited lifecycle hygiene operation is not safe to apply."""


@dataclass(frozen=True)
class GeometryHygieneItem:
    lifecycle_id: str
    symbol: str
    mode: str
    current_state: str
    geometry_failure: str
    plan_identity: str
    setup_identity: str
    classification: str
    proposed_state: str = NA
    reasons: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "lifecycle_id": self.lifecycle_id,
            "symbol": self.symbol,
            "mode": self.mode,
            "current_state": self.current_state,
            "geometry_failure": self.geometry_failure,
            "plan_identity": self.plan_identity,
            "setup_identity": self.setup_identity,
            "classification": self.classification,
            "proposed_state": self.proposed_state,
            "reasons": list(self.reasons),
        }


@dataclass(frozen=True)
class GeometryHygienePlan:
    schema_version: int
    preflight: dict[str, Any]
    items: tuple[GeometryHygieneItem, ...]
    fingerprint: str
    applied_count: int = 0
    postflight: dict[str, Any] | None = None

    @property
    def safe_to_quarantine(self) -> tuple[GeometryHygieneItem, ...]:
        return tuple(item for item in self.items if item.classification == "safe_to_quarantine")

    @property
    def requires_manual_review(self) -> tuple[GeometryHygieneItem, ...]:
        return tuple(item for item in self.items if item.classification == "requires_manual_review")

    @property
    def historical_preserve(self) -> tuple[GeometryHygieneItem, ...]:
        return tuple(item for item in self.items if item.classification == "historical_preserve")

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "preflight": self.preflight,
            "fingerprint": self.fingerprint,
            "applied_count": self.applied_count,
            "counts": {
                "malformed_records": len(self.items),
                "safe_to_quarantine": len(self.safe_to_quarantine),
                "requires_manual_review": len(self.requires_manual_review),
                "historical_preserve": len(self.historical_preserve),
            },
            "items": [item.as_dict() for item in self.items],
            "postflight": self.postflight,
        }


def audit_invalid_lifecycle_geometry(connection: sqlite3.Connection) -> GeometryHygienePlan:
    preflight = validate_lifecycle_hygiene_preflight(connection)
    rows = connection.execute(
        "SELECT * FROM setup_lifecycle_records ORDER BY lifecycle_id ASC"
    ).fetchall()
    items: list[GeometryHygieneItem] = []
    for row in rows:
        record = dict(row)
        failure = stored_plan_geometry_failure(record)
        if failure is None:
            continue
        items.append(_classify_record(connection, record, failure))
    ordered = tuple(items)
    return GeometryHygienePlan(
        schema_version=SCHEMA_VERSION,
        preflight=preflight,
        items=ordered,
        fingerprint=_plan_fingerprint(ordered),
    )


def apply_invalid_lifecycle_geometry_quarantine(
    connection: sqlite3.Connection,
    plan: GeometryHygienePlan,
    *,
    now: str | None = None,
) -> GeometryHygienePlan:
    timestamp = now or _now()
    if connection.in_transaction:
        raise LifecycleHygieneError("Lifecycle hygiene apply requires a clean connection.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = audit_invalid_lifecycle_geometry(connection)
        if current.fingerprint != plan.fingerprint:
            raise LifecycleHygieneError(
                "Lifecycle hygiene plan is stale; audit again before applying any quarantine."
            )
        for item in plan.safe_to_quarantine:
            _apply_item(connection, item, timestamp)
        postflight = validate_lifecycle_hygiene_preflight(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise

    final_postflight = validate_lifecycle_hygiene_preflight(connection)
    return replace(
        plan,
        applied_count=len(plan.safe_to_quarantine),
        postflight={"before_commit": postflight, "after_commit": final_postflight},
    )


def validate_lifecycle_hygiene_preflight(connection: sqlite3.Connection) -> dict[str, Any]:
    version_row = connection.execute("PRAGMA user_version").fetchone()
    version = int(version_row[0]) if version_row is not None else -1
    if version != SCHEMA_VERSION:
        raise LifecycleHygieneError(
            f"Lifecycle hygiene requires schema v{SCHEMA_VERSION}; found v{version}."
        )
    present_tables = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    }
    missing_tables = tuple(sorted(REQUIRED_TABLES - present_tables))
    if missing_tables:
        raise LifecycleHygieneError(
            "Lifecycle hygiene required tables are missing: " + ", ".join(missing_tables)
        )
    present_indexes = {
        str(row[0])
        for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
    }
    missing_indexes = tuple(sorted(REQUIRED_INDEXES - present_indexes))
    if missing_indexes:
        raise LifecycleHygieneError(
            "Lifecycle hygiene required indexes are missing: " + ", ".join(missing_indexes)
        )
    quick_check = tuple(str(row[0]) for row in connection.execute("PRAGMA quick_check").fetchall())
    integrity_check = tuple(str(row[0]) for row in connection.execute("PRAGMA integrity_check").fetchall())
    foreign_key_rows = tuple(tuple(row) for row in connection.execute("PRAGMA foreign_key_check").fetchall())
    if quick_check != ("ok",) or integrity_check != ("ok",) or foreign_key_rows:
        raise LifecycleHygieneError("SQLite preflight failed; no lifecycle changes were applied.")
    return {
        "schema_version": version,
        "quick_check": quick_check[0],
        "integrity_check": integrity_check[0],
        "foreign_key_violations": len(foreign_key_rows),
        "required_tables_present": True,
        "required_indexes_present": True,
    }


def _classify_record(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    failure: str,
) -> GeometryHygieneItem:
    lifecycle_id = _text(record.get("lifecycle_id"))
    current_state = _text(record.get("current_state")).upper()
    base = {
        "lifecycle_id": lifecycle_id,
        "symbol": _text(record.get("symbol")).upper(),
        "mode": _text(record.get("mode")).lower(),
        "current_state": current_state,
        "geometry_failure": failure,
        "plan_identity": canonical_plan_identity(record),
        "setup_identity": _text(record.get("setup_identity")),
    }
    if current_state not in OUTCOME_ELIGIBLE_STATES:
        reasons = ["non_outcome_eligible_preserve_unchanged"]
        if _text(record.get("failed_gate")) == QUARANTINE_REASON_CODE:
            reasons.append("already_quarantined")
        return GeometryHygieneItem(
            **base,
            classification="historical_preserve",
            reasons=tuple(reasons),
        )

    reasons: list[str] = []
    proposed = SAFE_QUARANTINE_TRANSITIONS.get(current_state)
    if proposed is None:
        reasons.append("execution_or_managing_state")
    if _status_key(record.get("actionability_state")) != "not_a_grade_candidate":
        reasons.append("not_pre_public_not_a_grade_candidate")
    reasons.extend(_delivery_dependency_reasons(connection, record, base["plan_identity"]))
    reasons.extend(_outcome_progress_dependency_reasons(connection, lifecycle_id))
    if reasons:
        return GeometryHygieneItem(
            **base,
            classification="requires_manual_review",
            proposed_state=proposed.value if proposed is not None else NA,
            reasons=tuple(dict.fromkeys(reasons)),
        )
    assert proposed is not None
    if not transition_allowed(SetupLifecycleState(current_state), proposed):
        return GeometryHygieneItem(
            **base,
            classification="requires_manual_review",
            proposed_state=proposed.value,
            reasons=("unexpected_illegal_quarantine_transition",),
        )
    return GeometryHygieneItem(
        **base,
        classification="safe_to_quarantine",
        proposed_state=proposed.value,
    )


def _delivery_dependency_reasons(
    connection: sqlite3.Connection,
    record: dict[str, Any],
    plan_identity: str,
) -> list[str]:
    lifecycle_id = _text(record.get("lifecycle_id"))
    reasons: list[str] = []
    attempt_rows = connection.execute(
        """
        SELECT telegram_status, sent_at
        FROM telegram_alert_attempts
        WHERE signal_id = ?
        """,
        (lifecycle_id,),
    ).fetchall()
    for row in attempt_rows:
        status = _status_key(row[0])
        sent_at = _text(row[1])
        if status == "sent" or sent_at != NA:
            reasons.append("confirmed_public_delivery")
        elif status in {"reserved", "pending", "in_flight", "retryable", "uncertain"}:
            reasons.append("public_delivery_reservation_or_uncertainty")
    direction = _status_key(record.get("direction"))
    if direction in {"long", "short"}:
        event_rows = connection.execute(
            """
            SELECT status, delivery_state, sent_at
            FROM public_alert_events
            WHERE canonical_plan_id = ?
               OR (
                    symbol = ? AND side = ?
                AND raw_entry_low IS ? AND raw_entry_high IS ? AND raw_stop_loss IS ?
               )
            """,
            (
                plan_identity,
                _text(record.get("symbol")).upper(),
                direction,
                record.get("entry_low"),
                record.get("entry_high"),
                record.get("stop_loss"),
            ),
        ).fetchall()
    else:
        # An unsupported side cannot safely be matched to a canonical public plan.
        # Any same-symbol event is therefore ambiguous and blocks automation.
        event_rows = connection.execute(
            "SELECT status, delivery_state, sent_at FROM public_alert_events WHERE symbol = ?",
            (_text(record.get("symbol")).upper(),),
        ).fetchall()
    for row in event_rows:
        status = _status_key(row[0])
        delivery = _status_key(row[1])
        sent_at = _text(row[2])
        if status == "sent" or sent_at != NA:
            reasons.append("confirmed_public_delivery")
        elif status in {"reserved", "pending", "in_flight", "retryable", "uncertain"} or delivery in {
            "pending",
            "in_flight",
            "retryable",
            "uncertain",
        }:
            reasons.append("public_delivery_reservation_or_uncertainty")
    return list(dict.fromkeys(reasons))


def _outcome_progress_dependency_reasons(
    connection: sqlite3.Connection,
    lifecycle_id: str,
) -> list[str]:
    rows = connection.execute(
        "SELECT * FROM setup_lifecycle_outcome_progress WHERE lifecycle_id = ?",
        (lifecycle_id,),
    ).fetchall()
    reasons: list[str] = []
    for row in rows:
        progress = dict(row)
        if any(_text(progress.get(column)) != NA for column in MARKET_PROGRESS_COLUMNS):
            reasons.append("valid_or_partial_outcome_progress")
            continue
        if _text(progress.get("terminal_outcome")) != NA:
            reasons.append("valid_or_partial_outcome_progress")
            continue
        if _status_key(progress.get("integrity_status")) == "verified":
            reasons.append("valid_or_partial_outcome_progress")
            continue
        diagnostic = _text(progress.get("diagnostic"))
        if not diagnostic.startswith(f"{QUARANTINE_REASON_CODE}:") and not diagnostic.startswith(
            "invalid_stored_plan_geometry:"
        ):
            reasons.append("ambiguous_outcome_progress")
    return list(dict.fromkeys(reasons))


def _apply_item(connection: sqlite3.Connection, item: GeometryHygieneItem, timestamp: str) -> None:
    repository = SQLiteSetupLifecycleRepository()
    repository.connection = connection
    record = repository.get_record_by_lifecycle_id(item.lifecycle_id)
    if record is None:
        raise LifecycleHygieneError(f"Lifecycle {item.lifecycle_id} no longer exists.")
    failure = stored_plan_geometry_failure(record)
    if failure != item.geometry_failure:
        raise LifecycleHygieneError(f"Lifecycle {item.lifecycle_id} geometry changed after audit.")
    if canonical_plan_identity(record) != item.plan_identity:
        raise LifecycleHygieneError(f"Lifecycle {item.lifecycle_id} plan identity changed after audit.")
    if record.current_state.value != item.current_state or record.setup_identity != item.setup_identity:
        raise LifecycleHygieneError(f"Lifecycle {item.lifecycle_id} state changed after audit.")
    proposed = SetupLifecycleState(item.proposed_state)
    transition = transition_record(
        record,
        proposed,
        reason=SetupTransitionReason.LEGACY_INVALID_STORED_PLAN_GEOMETRY,
        now=timestamp,
        failed_gate=QUARANTINE_REASON_CODE,
        notes=f"{QUARANTINE_REASON_CODE}:{failure}",
    )
    if not transition.transitioned or transition.event is None or transition.record is None:
        raise LifecycleHygieneError(f"Lifecycle {item.lifecycle_id} could not make the planned legal transition.")
    updated = transition.record
    predicates = ["lifecycle_id = ?", "current_state = ?"]
    predicate_values: list[Any] = [item.lifecycle_id, item.current_state]
    for column in PLAN_CONDITION_COLUMNS:
        predicates.append(f"{column} IS ?")
        predicate_values.append(getattr(record, column))
    cursor = connection.execute(
        f"""
        UPDATE setup_lifecycle_records
        SET current_state = ?, previous_state = ?, last_seen_at = ?, last_transition_at = ?,
            failed_gate = ?, cooldown_until = ?, archived_at = ?
        WHERE {' AND '.join(predicates)}
        """,
        (
            updated.current_state.value,
            updated.previous_state.value if updated.previous_state is not None else NA,
            updated.last_seen_at,
            updated.last_transition_at,
            updated.failed_gate,
            updated.cooldown_until,
            updated.archived_at,
            *predicate_values,
        ),
    )
    if cursor.rowcount != 1:
        raise LifecycleHygieneError(f"Lifecycle {item.lifecycle_id} conditional update was not applied.")
    repository.insert_event(transition.event)


def _plan_fingerprint(items: tuple[GeometryHygieneItem, ...]) -> str:
    payload = [item.as_dict() for item in items]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    if value is None:
        return NA
    text = str(value).strip()
    return text if text and text.upper() != NA else NA


def _status_key(value: Any) -> str:
    text = _text(value).lower()
    if text == NA.lower():
        return ""
    return text.replace("-", "_").replace(" ", "_")


__all__ = [
    "GeometryHygieneItem",
    "GeometryHygienePlan",
    "LifecycleHygieneError",
    "QUARANTINE_REASON_CODE",
    "apply_invalid_lifecycle_geometry_quarantine",
    "audit_invalid_lifecycle_geometry",
    "validate_lifecycle_hygiene_preflight",
]
