"""Prospective plan_version_id binding for outcome progress.

Historical NULL rows are not guessed. A bind-forward is allowed only when the
same lifecycle's own progress row was marked while the plan was unlocked and
the locked economics later remint to that lifecycle's plan_version_id.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from app.data.dtos import NA
from app.lifecycle.economic_identity import (
    REASON_PLAN_VERSION_INVARIANT_VIOLATION,
    proven_progress_plan_version_id,
)
from app.lifecycle.models import SetupLifecycleOutcomeProgress, SetupLifecycleRecord
from app.lifecycle.outcome_policy import canonical_plan_identity

PLAN_VERSION_BINDING_AWAITING = "awaiting_proven_plan_version"
PLAN_VERSION_BINDING_BOUND_AT_CREATION = "bound_at_creation"
PLAN_VERSION_BINDING_BOUND_FORWARD = "bound_forward_to_proven_plan_version"
PLAN_VERSION_BINDING_UNRESOLVED = "unresolved"
PLAN_VERSION_BINDING_KEY = "plan_version_binding"


@dataclass(frozen=True)
class PlanVersionBindDecision:
    progress: SetupLifecycleOutcomeProgress
    bound: bool
    reason: str


def initial_plan_version_binding(record: SetupLifecycleRecord) -> str:
    """Marker stored on a newly created prospective progress row."""

    if proven_progress_plan_version_id(record) is not None:
        return PLAN_VERSION_BINDING_BOUND_AT_CREATION
    reason = record.economic_identity_reason or ""
    if REASON_PLAN_VERSION_INVARIANT_VIOLATION in reason:
        return PLAN_VERSION_BINDING_UNRESOLVED
    if _latched_plan_version_id(record) is None:
        return PLAN_VERSION_BINDING_AWAITING
    return PLAN_VERSION_BINDING_UNRESOLVED


def bind_progress_to_proven_plan_version(
    progress: SetupLifecycleOutcomeProgress,
    record: SetupLifecycleRecord,
) -> PlanVersionBindDecision:
    """Bind a NULL prospective row, or leave the row unchanged.

    A different lifecycle, a wrong plan version, changed economics, missing
    provenance, or geometry that does not remint all fail closed.
    """

    if progress.lifecycle_id != record.lifecycle_id:
        return PlanVersionBindDecision(progress, False, "different_lifecycle")

    proven = proven_progress_plan_version_id(record)
    stored = progress.plan_version_id
    if stored is not None:
        if proven is not None and stored == proven:
            return PlanVersionBindDecision(progress, False, "preserved")
        if proven is None:
            return PlanVersionBindDecision(progress, False, "preserved_unproven_economics")
        return PlanVersionBindDecision(progress, False, "wrong_plan_version")

    if proven is None:
        return PlanVersionBindDecision(progress, False, "unresolved")

    metadata = _metadata(progress)
    if metadata.get(PLAN_VERSION_BINDING_KEY) != PLAN_VERSION_BINDING_AWAITING:
        return PlanVersionBindDecision(progress, False, "unresolved_legacy")
    if progress.plan_identity != canonical_plan_identity(record):
        return PlanVersionBindDecision(progress, False, "economics_mismatch")

    updated_metadata = dict(metadata)
    updated_metadata[PLAN_VERSION_BINDING_KEY] = PLAN_VERSION_BINDING_BOUND_FORWARD
    bound = progress.model_copy(
        update={
            "plan_version_id": proven,
            "metadata_json": json.dumps(updated_metadata, sort_keys=True, separators=(",", ":")),
        }
    )
    return PlanVersionBindDecision(bound, True, "bound_forward")


def resolve_persisted_plan_version(
    progress: SetupLifecycleOutcomeProgress,
    record: SetupLifecycleRecord,
    existing: SetupLifecycleOutcomeProgress | None,
) -> SetupLifecycleOutcomeProgress:
    """Return the progress row that may be written.

    A stored plan_version_id is immutable. An existing NULL row binds only when
    that persisted row already carries the awaiting marker and its plan identity
    matches the lifecycle's economics. A caller-supplied id or a rewritten
    marker is not provenance. A brand-new row may keep the proven id only when
    its plan identity matches those economics.
    """

    if progress.lifecycle_id != record.lifecycle_id:
        return _with_plan_version(progress, existing.plan_version_id if existing is not None else None)

    existing_id = existing.plan_version_id if existing is not None else None
    if existing_id is not None:
        return _with_plan_version(progress, existing_id)

    if existing is not None:
        return _bind_existing_null_row(progress, record, existing)

    proven = proven_progress_plan_version_id(record)
    identity_matches = progress.plan_identity == canonical_plan_identity(record)
    incoming = progress.plan_version_id
    if incoming is not None and incoming == proven and identity_matches:
        return progress
    if not identity_matches:
        return _without_binding_provenance(progress)
    if incoming is not None:
        return _with_plan_version(progress, None)
    return progress


def _bind_existing_null_row(
    progress: SetupLifecycleOutcomeProgress,
    record: SetupLifecycleRecord,
    existing: SetupLifecycleOutcomeProgress,
) -> SetupLifecycleOutcomeProgress:
    """Bind from the persisted row. Incoming metadata cannot create provenance."""

    decision = bind_progress_to_proven_plan_version(existing, record)
    identity_matches = (
        progress.plan_identity == existing.plan_identity
        and existing.plan_identity == canonical_plan_identity(record)
    )
    if decision.bound and identity_matches and decision.progress.plan_version_id is not None:
        metadata = _metadata(progress)
        metadata[PLAN_VERSION_BINDING_KEY] = PLAN_VERSION_BINDING_BOUND_FORWARD
        return progress.model_copy(
            update={
                "plan_version_id": decision.progress.plan_version_id,
                "metadata_json": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            }
        )
    existing_marker = _metadata(existing).get(PLAN_VERSION_BINDING_KEY)
    if identity_matches and existing_marker == PLAN_VERSION_BINDING_AWAITING:
        metadata = _metadata(progress)
        metadata[PLAN_VERSION_BINDING_KEY] = PLAN_VERSION_BINDING_AWAITING
        return progress.model_copy(
            update={
                "plan_version_id": None,
                "metadata_json": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
            }
        )
    return _without_binding_provenance(progress)


def _without_binding_provenance(
    progress: SetupLifecycleOutcomeProgress,
) -> SetupLifecycleOutcomeProgress:
    metadata = _metadata(progress)
    metadata.pop(PLAN_VERSION_BINDING_KEY, None)
    return progress.model_copy(
        update={
            "plan_version_id": None,
            "metadata_json": json.dumps(metadata, sort_keys=True, separators=(",", ":")),
        }
    )


def _with_plan_version(
    progress: SetupLifecycleOutcomeProgress,
    plan_version_id: str | None,
) -> SetupLifecycleOutcomeProgress:
    if progress.plan_version_id == plan_version_id:
        return progress
    return progress.model_copy(update={"plan_version_id": plan_version_id})


def _latched_plan_version_id(record: SetupLifecycleRecord) -> str | None:
    value = record.plan_version_id
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == NA:
        return None
    return text


def _metadata(progress: SetupLifecycleOutcomeProgress) -> dict[str, Any]:
    try:
        value = json.loads(progress.metadata_json)
    except (TypeError, json.JSONDecodeError):
        return {}
    return dict(value) if isinstance(value, Mapping) else {}


__all__ = [
    "PLAN_VERSION_BINDING_AWAITING",
    "PLAN_VERSION_BINDING_BOUND_AT_CREATION",
    "PLAN_VERSION_BINDING_BOUND_FORWARD",
    "PLAN_VERSION_BINDING_KEY",
    "PLAN_VERSION_BINDING_UNRESOLVED",
    "PlanVersionBindDecision",
    "bind_progress_to_proven_plan_version",
    "initial_plan_version_binding",
    "resolve_persisted_plan_version",
]
