from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from app.alerts.public_identity import canonical_public_event_key
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleState, SetupTransitionReason
from app.lifecycle.outcome_policy import canonical_plan_identity, stored_plan_geometry_failure
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.state_machine import transition_allowed, transition_record
from app.storage.database import SCHEMA_VERSION


QUARANTINE_REASON_CODE = SetupTransitionReason.LEGACY_INVALID_STORED_PLAN_GEOMETRY.value
FAILED_GEOMETRY_DIAGNOSTIC = "invalid_stored_plan_geometry"
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
DEPENDENCY_BLOCKING_STATES = frozenset(
    {
        SetupLifecycleState.EXECUTING.value,
        SetupLifecycleState.MANAGING.value,
        SetupLifecycleState.TP_HIT.value,
        SetupLifecycleState.SL_HIT.value,
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
OUTCOME_TIMESTAMP_COLUMNS = (
    "entry_at",
    "tp1_at",
    "tp2_at",
    "tp3_at",
    "stop_at",
    "invalidated_at",
    "outcome_at",
)
UNRESOLVED_DELIVERY_STATES = frozenset(
    {"reserved", "claimed", "pending", "in_flight", "retryable", "uncertain"}
)
RESOLVED_NO_DELIVERY_STATES = frozenset(
    {
        "blocked",
        "cancelled",
        "canceled",
        "duplicate",
        "failed",
        "failed_final",
        "ineligible",
        "policy_disabled",
        "skipped",
        "skipped_dry_run",
    }
)
CANONICAL_OUTCOME_PLAN_ID = re.compile(r"^plan-[0-9a-f]{64}$")
CANONICAL_PUBLIC_PLAN_ID = re.compile(r"^[A-Z0-9]+[|](?:long|short)[|][0-9a-f]{20}$")


class LifecycleHygieneError(RuntimeError):
    """The requested audited lifecycle hygiene operation is not safe to apply."""


class DependencyOwnership(str, Enum):
    CURRENT_PLAN_IDENTITY = "current_plan_identity"
    HISTORICAL_NON_CURRENT_PLAN_IDENTITY = "historical_non_current_plan_identity"
    DIRECT_CURRENT_PLAN_PUBLIC_DELIVERY = "direct_current_plan_public_delivery"
    AMBIGUOUS_UNPROVEN = "ambiguous_unproven_ownership"


@dataclass(frozen=True)
class DependencyAssessment:
    ownership: tuple[DependencyOwnership, ...] = ()
    reasons: tuple[str, ...] = ()


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
    dependency_ownership: tuple[str, ...] = ()

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
            "dependency_ownership": list(self.dependency_ownership),
        }


@dataclass(frozen=True)
class GeometryHygieneManifestItem:
    lifecycle_id: str
    current_plan_identity: str
    expected_state: str
    approved_transition: str

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GeometryHygieneManifestItem:
        return cls(
            lifecycle_id=_required_manifest_text(value, "lifecycle_id"),
            current_plan_identity=_required_manifest_text(value, "current_plan_identity"),
            expected_state=_required_manifest_text(value, "expected_state").upper(),
            approved_transition=_required_manifest_text(value, "approved_transition").upper(),
        )

    def as_dict(self) -> dict[str, str]:
        return {
            "lifecycle_id": self.lifecycle_id,
            "current_plan_identity": self.current_plan_identity,
            "expected_state": self.expected_state,
            "approved_transition": self.approved_transition,
        }


@dataclass(frozen=True)
class GeometryHygieneManifest:
    schema_version: int
    plan_fingerprint: str
    items: tuple[GeometryHygieneManifestItem, ...]

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> GeometryHygieneManifest:
        try:
            schema_version = int(value["schema_version"])
            plan_fingerprint = _required_manifest_text(value, "plan_fingerprint")
            raw_items = value["items"]
        except (KeyError, TypeError, ValueError) as exc:
            raise LifecycleHygieneError("Lifecycle hygiene manifest is malformed.") from exc
        if not isinstance(raw_items, list):
            raise LifecycleHygieneError("Lifecycle hygiene manifest items must be a JSON list.")
        try:
            items = tuple(GeometryHygieneManifestItem.from_dict(item) for item in raw_items)
        except (TypeError, AttributeError) as exc:
            raise LifecycleHygieneError("Lifecycle hygiene manifest item is malformed.") from exc
        return cls(
            schema_version=schema_version,
            plan_fingerprint=plan_fingerprint,
            items=items,
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "plan_fingerprint": self.plan_fingerprint,
            "items": [item.as_dict() for item in self.items],
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

    @property
    def manifest_template(self) -> GeometryHygieneManifest:
        return build_geometry_hygiene_manifest(self)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "preflight": self.preflight,
            "fingerprint": self.fingerprint,
            "manifest_template": self.manifest_template.as_dict(),
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


def build_geometry_hygiene_manifest(plan: GeometryHygienePlan) -> GeometryHygieneManifest:
    return GeometryHygieneManifest(
        schema_version=plan.schema_version,
        plan_fingerprint=plan.fingerprint,
        items=tuple(
            GeometryHygieneManifestItem(
                lifecycle_id=item.lifecycle_id,
                current_plan_identity=item.plan_identity,
                expected_state=item.current_state,
                approved_transition=item.proposed_state,
            )
            for item in plan.safe_to_quarantine
        ),
    )


def validate_geometry_hygiene_manifest(
    plan: GeometryHygienePlan,
    manifest: GeometryHygieneManifest | Mapping[str, Any],
) -> GeometryHygieneManifest:
    normalized = (
        manifest
        if isinstance(manifest, GeometryHygieneManifest)
        else GeometryHygieneManifest.from_dict(manifest)
    )
    if normalized.schema_version != plan.schema_version:
        raise LifecycleHygieneError("Lifecycle hygiene manifest schema version does not match the audit.")
    if normalized.plan_fingerprint != plan.fingerprint:
        raise LifecycleHygieneError("Lifecycle hygiene manifest fingerprint does not match the audit.")
    approved_by_lifecycle: dict[str, GeometryHygieneManifestItem] = {}
    for item in normalized.items:
        if item.lifecycle_id in approved_by_lifecycle:
            raise LifecycleHygieneError(
                f"Lifecycle hygiene manifest repeats lifecycle {item.lifecycle_id}."
            )
        approved_by_lifecycle[item.lifecycle_id] = item
    safe_by_lifecycle = {item.lifecycle_id: item for item in plan.safe_to_quarantine}
    if set(approved_by_lifecycle) != set(safe_by_lifecycle):
        raise LifecycleHygieneError(
            "Lifecycle hygiene manifest must exactly cover the audited safe quarantine set."
        )
    for lifecycle_id, approved in approved_by_lifecycle.items():
        item = safe_by_lifecycle[lifecycle_id]
        if (
            approved.current_plan_identity != item.plan_identity
            or approved.expected_state != item.current_state
            or approved.approved_transition != item.proposed_state
        ):
            raise LifecycleHygieneError(
                f"Lifecycle hygiene manifest identity or transition mismatch for {lifecycle_id}."
            )
        try:
            current_state = SetupLifecycleState(approved.expected_state)
            proposed_state = SetupLifecycleState(approved.approved_transition)
        except ValueError as exc:
            raise LifecycleHygieneError(
                f"Lifecycle hygiene manifest contains an unknown state for {lifecycle_id}."
            ) from exc
        if not transition_allowed(current_state, proposed_state):
            raise LifecycleHygieneError(
                f"Lifecycle hygiene manifest transition is illegal for {lifecycle_id}."
            )
    return normalized


def apply_invalid_lifecycle_geometry_quarantine(
    connection: sqlite3.Connection,
    plan: GeometryHygienePlan,
    *,
    manifest: GeometryHygieneManifest | Mapping[str, Any],
    now: str | None = None,
) -> GeometryHygienePlan:
    timestamp = now or _now()
    approved = validate_geometry_hygiene_manifest(plan, manifest)
    if connection.in_transaction:
        raise LifecycleHygieneError("Lifecycle hygiene apply requires a clean connection.")
    connection.execute("BEGIN IMMEDIATE")
    try:
        current = audit_invalid_lifecycle_geometry(connection)
        if current.fingerprint != plan.fingerprint:
            raise LifecycleHygieneError(
                "Lifecycle hygiene plan is stale; audit again before applying any quarantine."
            )
        approved = validate_geometry_hygiene_manifest(current, approved)
        approvals = {item.lifecycle_id: item for item in approved.items}
        for item in current.safe_to_quarantine:
            _apply_item(connection, item, approvals[item.lifecycle_id], timestamp)
        postflight = validate_lifecycle_hygiene_preflight(connection)
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise

    final_postflight = validate_lifecycle_hygiene_preflight(connection)
    return replace(
        current,
        applied_count=len(current.safe_to_quarantine),
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
    plan_identity = canonical_plan_identity(record)
    base = {
        "lifecycle_id": lifecycle_id,
        "symbol": _text(record.get("symbol")).upper(),
        "mode": _text(record.get("mode")).lower(),
        "current_state": current_state,
        "geometry_failure": failure,
        "plan_identity": plan_identity,
        "setup_identity": _text(record.get("setup_identity")),
    }
    if current_state in DEPENDENCY_BLOCKING_STATES:
        return GeometryHygieneItem(
            **base,
            classification="requires_manual_review",
            reasons=("execution_or_terminal_market_state",),
        )
    if current_state not in OUTCOME_ELIGIBLE_STATES:
        reasons = ["non_outcome_eligible_preserve_unchanged"]
        if _text(record.get("failed_gate")) == QUARANTINE_REASON_CODE:
            reasons.append("already_quarantined")
        return GeometryHygieneItem(
            **base,
            classification="historical_preserve",
            reasons=tuple(reasons),
        )

    progress = _outcome_progress_assessment(
        connection,
        record=record,
        plan_identity=plan_identity,
        geometry_failure=failure,
    )
    delivery = _public_delivery_assessment(
        connection,
        record=record,
        plan_identity=plan_identity,
    )
    assessment = _merge_assessments(progress, delivery)
    reasons = list(assessment.reasons)
    proposed = SAFE_QUARANTINE_TRANSITIONS.get(current_state)
    if proposed is None:
        reasons.append("execution_or_terminal_market_state")
    if _status_key(record.get("actionability_state")) != "not_a_grade_candidate":
        reasons.append("not_pre_public_not_a_grade_candidate")
    ownership = tuple(item.value for item in assessment.ownership)
    if reasons:
        return GeometryHygieneItem(
            **base,
            classification="requires_manual_review",
            proposed_state=proposed.value if proposed is not None else NA,
            reasons=tuple(dict.fromkeys(reasons)),
            dependency_ownership=ownership,
        )
    assert proposed is not None
    if not transition_allowed(SetupLifecycleState(current_state), proposed):
        return GeometryHygieneItem(
            **base,
            classification="requires_manual_review",
            proposed_state=proposed.value,
            reasons=("unexpected_illegal_quarantine_transition",),
            dependency_ownership=ownership,
        )
    return GeometryHygieneItem(
        **base,
        classification="safe_to_quarantine",
        proposed_state=proposed.value,
        dependency_ownership=ownership,
    )


def _outcome_progress_assessment(
    connection: sqlite3.Connection,
    *,
    record: dict[str, Any],
    plan_identity: str,
    geometry_failure: str,
) -> DependencyAssessment:
    lifecycle_id = _text(record.get("lifecycle_id"))
    rows = connection.execute(
        "SELECT * FROM setup_lifecycle_outcome_progress WHERE lifecycle_id = ? ORDER BY id",
        (lifecycle_id,),
    ).fetchall()
    ownership: list[DependencyOwnership] = []
    reasons: list[str] = []
    current_rows = 0
    for row in rows:
        progress = dict(row)
        progress_plan_identity = _text(progress.get("plan_identity"))
        if progress_plan_identity == plan_identity:
            current_rows += 1
            ownership.append(DependencyOwnership.CURRENT_PLAN_IDENTITY)
            reasons.extend(
                _current_progress_reasons(
                    record,
                    progress,
                    geometry_failure=geometry_failure,
                )
            )
        elif _is_canonical_outcome_plan_identity(progress_plan_identity):
            ownership.append(DependencyOwnership.HISTORICAL_NON_CURRENT_PLAN_IDENTITY)
        else:
            ownership.append(DependencyOwnership.AMBIGUOUS_UNPROVEN)
            reasons.append("ambiguous_or_malformed_outcome_plan_identity")
    if current_rows == 0:
        ownership.append(DependencyOwnership.AMBIGUOUS_UNPROVEN)
        reasons.append("missing_current_plan_outcome_progress")
    elif current_rows != 1:
        ownership.append(DependencyOwnership.AMBIGUOUS_UNPROVEN)
        reasons.append("ambiguous_current_plan_outcome_progress")
    return DependencyAssessment(
        ownership=_ordered_ownership(ownership),
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _current_progress_reasons(
    record: dict[str, Any],
    progress: dict[str, Any],
    *,
    geometry_failure: str,
) -> list[str]:
    reasons: list[str] = []
    if (
        _text(progress.get("symbol")).upper() != _text(record.get("symbol")).upper()
        or _text(progress.get("mode")).lower() != _text(record.get("mode")).lower()
        or _status_key(progress.get("direction")) != _status_key(record.get("direction"))
    ):
        reasons.append("current_plan_progress_metadata_mismatch")
    expected_diagnostic = f"{FAILED_GEOMETRY_DIAGNOSTIC}:{geometry_failure}"
    if _text(progress.get("diagnostic")) != expected_diagnostic:
        reasons.append("current_plan_progress_not_failed_geometry_only")
    if any(_text(progress.get(column)) != NA for column in OUTCOME_TIMESTAMP_COLUMNS):
        reasons.append("current_plan_market_progress")
    if _text(progress.get("terminal_outcome")) != NA:
        reasons.append("current_plan_terminal_outcome")
    if _status_key(progress.get("integrity_status")) == "verified":
        reasons.append("current_plan_verified_integrity")
    return reasons


def _public_delivery_assessment(
    connection: sqlite3.Connection,
    *,
    record: dict[str, Any],
    plan_identity: str,
) -> DependencyAssessment:
    lifecycle_id = _text(record.get("lifecycle_id"))
    symbol = _text(record.get("symbol")).upper()
    event_prefix = f"{plan_identity}|%"
    ownership: list[DependencyOwnership] = []
    reasons: list[str] = []

    attempt_rows = connection.execute(
        """
        SELECT signal_id, symbol, telegram_status, sent_at, delivery_state,
               public_watchlist_plan_id, public_watchlist_event_key,
               public_alert_event_type
        FROM telegram_alert_attempts
        WHERE signal_id = ?
           OR public_watchlist_plan_id = ?
           OR public_watchlist_event_key LIKE ?
           OR symbol = ?
        ORDER BY id
        """,
        (lifecycle_id, plan_identity, event_prefix, symbol),
    ).fetchall()
    for row in attempt_rows:
        attempt = dict(row)
        direct_lifecycle = _text(attempt.get("signal_id")) == lifecycle_id
        owner = _public_identity_ownership(
            plan_identity=plan_identity,
            candidate_plan_id=attempt.get("public_watchlist_plan_id"),
            event_type=attempt.get("public_alert_event_type"),
            event_key=attempt.get("public_watchlist_event_key"),
            direct_lifecycle=direct_lifecycle,
            require_setup_identity=False,
            setup_identity=NA,
        )
        if owner is None:
            continue
        ownership.append(owner)
        if owner == DependencyOwnership.DIRECT_CURRENT_PLAN_PUBLIC_DELIVERY:
            reasons.extend(
                _delivery_state_reasons(
                    status=attempt.get("telegram_status"),
                    delivery_state=attempt.get("delivery_state"),
                    sent_at=attempt.get("sent_at"),
                )
            )
        elif owner == DependencyOwnership.AMBIGUOUS_UNPROVEN:
            reasons.append("ambiguous_public_delivery_identity")

    event_rows = connection.execute(
        """
        SELECT *
        FROM public_alert_events
        WHERE canonical_plan_id = ?
           OR event_key LIKE ?
           OR symbol = ?
        ORDER BY id
        """,
        (plan_identity, event_prefix, symbol),
    ).fetchall()
    for row in event_rows:
        event = dict(row)
        owner = _public_identity_ownership(
            plan_identity=plan_identity,
            candidate_plan_id=event.get("canonical_plan_id"),
            event_type=event.get("event_type"),
            event_key=event.get("event_key"),
            direct_lifecycle=False,
            require_setup_identity=True,
            setup_identity=event.get("setup_family"),
        )
        if owner is None:
            continue
        ownership.append(owner)
        if owner == DependencyOwnership.DIRECT_CURRENT_PLAN_PUBLIC_DELIVERY:
            reasons.extend(
                _delivery_state_reasons(
                    status=event.get("status"),
                    delivery_state=event.get("delivery_state"),
                    sent_at=event.get("sent_at"),
                )
            )
            part_rows = connection.execute(
                """
                SELECT event_key, delivery_state, sent_at
                FROM public_alert_delivery_parts
                WHERE public_alert_event_id = ?
                ORDER BY part_index
                """,
                (event["id"],),
            ).fetchall()
            expected_event_key = _text(event.get("event_key"))
            for part_row in part_rows:
                part = dict(part_row)
                if _text(part.get("event_key")) != expected_event_key:
                    ownership.append(DependencyOwnership.AMBIGUOUS_UNPROVEN)
                    reasons.append("ambiguous_public_delivery_part_identity")
                    continue
                reasons.extend(
                    _delivery_state_reasons(
                        status=NA,
                        delivery_state=part.get("delivery_state"),
                        sent_at=part.get("sent_at"),
                    )
                )
        elif owner == DependencyOwnership.AMBIGUOUS_UNPROVEN:
            reasons.append("ambiguous_public_delivery_identity")

    return DependencyAssessment(
        ownership=_ordered_ownership(ownership),
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _public_identity_ownership(
    *,
    plan_identity: str,
    candidate_plan_id: Any,
    event_type: Any,
    event_key: Any,
    direct_lifecycle: bool,
    require_setup_identity: bool,
    setup_identity: Any,
) -> DependencyOwnership | None:
    candidate_plan = _text(candidate_plan_id)
    candidate_event_type = _status_key(event_type)
    candidate_event_key = _text(event_key)
    expected_current_event_key = (
        canonical_public_event_key(plan_identity, candidate_event_type)
        if candidate_event_type
        else NA
    )
    plan_matches = candidate_plan == plan_identity
    event_matches = (
        expected_current_event_key != NA and candidate_event_key == expected_current_event_key
    )
    setup_present = not require_setup_identity or _text(setup_identity) != NA
    if plan_matches and event_matches and setup_present:
        return DependencyOwnership.DIRECT_CURRENT_PLAN_PUBLIC_DELIVERY
    if plan_matches or event_matches:
        return DependencyOwnership.AMBIGUOUS_UNPROVEN

    complete_identity = (
        _is_canonical_public_or_outcome_plan_identity(candidate_plan)
        and bool(candidate_event_type)
        and candidate_event_key
        == canonical_public_event_key(candidate_plan, candidate_event_type)
        and setup_present
    )
    if complete_identity:
        return DependencyOwnership.HISTORICAL_NON_CURRENT_PLAN_IDENTITY
    if direct_lifecycle:
        return DependencyOwnership.AMBIGUOUS_UNPROVEN
    return None


def _delivery_state_reasons(
    *,
    status: Any,
    delivery_state: Any,
    sent_at: Any,
) -> list[str]:
    normalized_status = _status_key(status)
    normalized_delivery = _status_key(delivery_state)
    if (
        normalized_status == "sent"
        or normalized_delivery == "sent"
        or _text(sent_at) != NA
    ):
        return ["confirmed_public_delivery"]
    if (
        normalized_status in UNRESOLVED_DELIVERY_STATES
        or normalized_delivery in UNRESOLVED_DELIVERY_STATES
    ):
        return ["public_delivery_reservation_or_uncertainty"]
    states = {state for state in (normalized_status, normalized_delivery) if state}
    if not states or not states.issubset(RESOLVED_NO_DELIVERY_STATES):
        return ["unresolved_public_delivery_state"]
    return []


def _merge_assessments(*assessments: DependencyAssessment) -> DependencyAssessment:
    ownership = [
        item
        for assessment in assessments
        for item in assessment.ownership
    ]
    reasons = [
        reason
        for assessment in assessments
        for reason in assessment.reasons
    ]
    return DependencyAssessment(
        ownership=_ordered_ownership(ownership),
        reasons=tuple(dict.fromkeys(reasons)),
    )


def _ordered_ownership(
    values: list[DependencyOwnership],
) -> tuple[DependencyOwnership, ...]:
    present = set(values)
    return tuple(item for item in DependencyOwnership if item in present)


def _is_canonical_outcome_plan_identity(value: Any) -> bool:
    return CANONICAL_OUTCOME_PLAN_ID.fullmatch(_text(value)) is not None


def _is_canonical_public_or_outcome_plan_identity(value: Any) -> bool:
    normalized = _text(value)
    return (
        CANONICAL_OUTCOME_PLAN_ID.fullmatch(normalized) is not None
        or CANONICAL_PUBLIC_PLAN_ID.fullmatch(normalized) is not None
    )


def _apply_item(
    connection: sqlite3.Connection,
    item: GeometryHygieneItem,
    approved: GeometryHygieneManifestItem,
    timestamp: str,
) -> None:
    row = connection.execute(
        "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
        (item.lifecycle_id,),
    ).fetchone()
    if row is None:
        raise LifecycleHygieneError(f"Lifecycle {item.lifecycle_id} no longer exists.")
    record_row = dict(row)
    failure = stored_plan_geometry_failure(record_row)
    if failure is None:
        raise LifecycleHygieneError(f"Lifecycle {item.lifecycle_id} geometry is no longer malformed.")
    fresh = _classify_record(connection, record_row, failure)
    if fresh != item or fresh.classification != "safe_to_quarantine":
        raise LifecycleHygieneError(
            f"Lifecycle {item.lifecycle_id} preconditions changed immediately before write."
        )
    if (
        approved.lifecycle_id != fresh.lifecycle_id
        or approved.current_plan_identity != fresh.plan_identity
        or approved.expected_state != fresh.current_state
        or approved.approved_transition != fresh.proposed_state
    ):
        raise LifecycleHygieneError(f"Lifecycle {item.lifecycle_id} is not approved by the manifest.")

    repository = SQLiteSetupLifecycleRepository()
    repository.connection = connection
    record = repository.get_record_by_lifecycle_id(item.lifecycle_id)
    if record is None:
        raise LifecycleHygieneError(f"Lifecycle {item.lifecycle_id} no longer exists.")
    proposed = SetupLifecycleState(item.proposed_state)
    if not transition_allowed(record.current_state, proposed):
        raise LifecycleHygieneError(f"Lifecycle {item.lifecycle_id} transition is no longer legal.")
    transition = transition_record(
        record,
        proposed,
        reason=SetupTransitionReason.LEGACY_INVALID_STORED_PLAN_GEOMETRY,
        now=timestamp,
        failed_gate=QUARANTINE_REASON_CODE,
        notes=f"{QUARANTINE_REASON_CODE}:{failure}",
    )
    if not transition.transitioned or transition.event is None or transition.record is None:
        raise LifecycleHygieneError(
            f"Lifecycle {item.lifecycle_id} could not make the planned legal transition."
        )
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


def _required_manifest_text(value: Mapping[str, Any], key: str) -> str:
    try:
        normalized = _text(value[key])
    except KeyError as exc:
        raise LifecycleHygieneError(f"Lifecycle hygiene manifest is missing {key}.") from exc
    if normalized == NA:
        raise LifecycleHygieneError(f"Lifecycle hygiene manifest {key} must not be N/A.")
    return normalized


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
    normalized = text.replace("-", "_").replace(" ", "_")
    while "__" in normalized:
        normalized = normalized.replace("__", "_")
    return normalized.strip("_")


__all__ = [
    "DependencyOwnership",
    "GeometryHygieneItem",
    "GeometryHygieneManifest",
    "GeometryHygieneManifestItem",
    "GeometryHygienePlan",
    "LifecycleHygieneError",
    "QUARANTINE_REASON_CODE",
    "apply_invalid_lifecycle_geometry_quarantine",
    "audit_invalid_lifecycle_geometry",
    "build_geometry_hygiene_manifest",
    "validate_geometry_hygiene_manifest",
    "validate_lifecycle_hygiene_preflight",
]
