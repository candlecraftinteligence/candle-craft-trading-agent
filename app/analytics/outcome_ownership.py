"""P3A read-only outcome ownership reconciliation projection.

Consumes explicitly supplied lifecycle, progress, event, and analytics records.
It does not open a database, evaluate candles, write storage, mint occurrence
ids, or feed public delivery, scanner, health, memory, or replay consumers.

This projection may group evidence under a proven ``plan_version_id`` and may
emit a diagnostic plan-level interpretation when a single coherent evaluation
context already exists in the supplied records. It does **not** establish a
persisted canonical outcome owner, a fill occurrence, or a unique trade.
"""

from __future__ import annotations

import copy
import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Final

from app.analytics.evidence_contract import UNAVAILABLE, UNSAFE
from app.data.dtos import NA
from app.lifecycle.economic_identity import (
    REASON_PLAN_VERSION_INVARIANT_VIOLATION,
    mint_plan_version_id,
)
from app.lifecycle.models import SetupLifecycleState, SetupTransitionReason
from app.lifecycle.outcome_policy import canonical_plan_identity, compatible_plan_identities

OUTCOME_OWNERSHIP_VERSION: Final[str] = "cci-outcome-ownership-v1"

STATUS_VERIFIED: Final[str] = "verified_attribution"
STATUS_MISSING_LEGACY: Final[str] = "missing_legacy_identity"
STATUS_CONFLICTING_IDENTITY: Final[str] = "conflicting_identity_economics"
STATUS_AMBIGUOUS_CONTEXT: Final[str] = "ambiguous_evaluation_context"
STATUS_INCOMPLETE: Final[str] = "incomplete_evidence"
STATUS_CONFLICTING_ECONOMIC: Final[str] = "conflicting_economic_results"

INTEGRITY_CATEGORIES: Final[tuple[str, ...]] = (
    STATUS_VERIFIED,
    STATUS_MISSING_LEGACY,
    STATUS_CONFLICTING_IDENTITY,
    STATUS_AMBIGUOUS_CONTEXT,
    STATUS_INCOMPLETE,
    STATUS_CONFLICTING_ECONOMIC,
)

PROGRESS_TABLE: Final[str] = "setup_lifecycle_outcome_progress"
RECORD_TABLE: Final[str] = "setup_lifecycle_records"
EVENT_TABLE: Final[str] = "setup_lifecycle_events"
ANALYTICS_TABLE: Final[str] = "setup_outcome_analytics"

MILESTONE_FIELDS: Final[tuple[str, ...]] = (
    "entry_at",
    "tp1_at",
    "tp2_at",
    "tp3_at",
    "stop_at",
    "invalidated_at",
    "outcome_at",
)
CONTEXT_FIELDS: Final[tuple[str, ...]] = (
    "tracking_start_at",
    "execution_timeframe",
    "first_evaluated_at",
)
ECONOMIC_TERMINALS: Final[frozenset[str]] = frozenset(
    {
        SetupLifecycleState.TP_HIT.value,
        SetupLifecycleState.SL_HIT.value,
        SetupLifecycleState.INVALIDATED.value,
        SetupLifecycleState.EXPIRED.value,
    }
)
LIFECYCLE_SUCCESSORS: Final[frozenset[str]] = frozenset(
    {
        SetupLifecycleState.COOLDOWN.value,
        SetupLifecycleState.COOLED_DOWN.value,
        SetupLifecycleState.ARCHIVED.value,
        SetupLifecycleState.NO_LONGER_TRACKING.value,
        SetupLifecycleState.REMOVED.value,
    }
)
ANALYTICS_ECONOMIC: Final[frozenset[str]] = frozenset(
    {"TP3_HIT", "SL_HIT", "INVALIDATED", "EXPIRED", "REJECTED"}
)
ANALYTICS_SUCCESSORS: Final[frozenset[str]] = frozenset({"COOLDOWN"})
PROGRESS_PHYSICAL_FIELDS: Final[tuple[str, ...]] = ("lifecycle_id", "plan_identity")

_ENTRY_ACTIVATED: Final[str] = SetupTransitionReason.ENTRY_ACTIVATED.value
_ENTRY_ZONE_TOUCHED: Final[str] = SetupTransitionReason.ENTRY_ZONE_TOUCHED.value
_ENTRY_FILL_SIMULATED: Final[str] = SetupTransitionReason.ENTRY_FILL_SIMULATED.value
_TP1_MILESTONE: Final[str] = SetupTransitionReason.TP1_MILESTONE.value
_TP2_MILESTONE: Final[str] = SetupTransitionReason.TP2_MILESTONE.value
_TP3_MILESTONE: Final[str] = SetupTransitionReason.TP3_MILESTONE.value


def project_outcome_ownership(
    *,
    lifecycle_records: Sequence[Any] = (),
    progress_rows: Sequence[Any] = (),
    event_rows: Sequence[Any] = (),
    analytics_rows: Sequence[Any] = (),
    provenance: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Project ownership diagnostics from already-supplied records."""

    meta = _provenance(provenance)
    records = _retain_source_records(RECORD_TABLE, lifecycle_records, meta["default_namespace"], ("lifecycle_id",))
    progress = _retain_source_records(
        PROGRESS_TABLE,
        progress_rows,
        meta["default_namespace"],
        PROGRESS_PHYSICAL_FIELDS,
    )
    events = _retain_source_records(EVENT_TABLE, event_rows, meta["default_namespace"], ("event_id", "lifecycle_id"))
    analytics = _retain_source_records(
        ANALYTICS_TABLE,
        analytics_rows,
        meta["default_namespace"],
        ("lifecycle_id", "final_outcome"),
    )

    evaluations = _build_evaluations(
        records,
        progress,
        events,
        analytics,
        coverage_complete=bool(meta["coverage_complete"]),
    )
    inventory = _plan_identity_inventory(records, evaluations)
    interpretations = _plan_interpretations(evaluations)
    integrity = _integrity_rollup(records, evaluations)
    counts = _counts(records, progress, events, analytics, evaluations, interpretations, inventory)
    return {
        "ownership_version": OUTCOME_OWNERSHIP_VERSION,
        "feeds_operational_decisions": False,
        "schema_version_unchanged": True,
        "canonical_outcome_per_plan_version": {
            "established": False,
            "status": "unproven",
            "unit": "persisted authoritative plan-outcome",
            "reason": (
                "Current producers persist outcomes on UNIQUE(lifecycle_id, plan_identity). "
                "plan_version_id is not a column on outcome progress or analytics. "
                "A diagnostic interpretation is not a canonical stored owner."
            ),
        },
        "provenance": meta,
        "source_evidence": {
            "lifecycle_records": _source_summary(records),
            "outcome_progress": _source_summary(progress),
            "lifecycle_events": _source_summary(events),
            "outcome_analytics": _source_summary(analytics),
        },
        "integrity": integrity,
        "plan_identity_inventory": inventory,
        "evaluations": evaluations,
        "plan_interpretations": interpretations,
        "counts": counts,
        "unavailable_metrics": _unavailable_metrics(),
        "p2a_event_units": _p2a_event_units(events),
        "raw_versus_diagnostic": _raw_versus_diagnostic(
            progress,
            evaluations,
            interpretations,
            inventory,
        ),
    }


def dumps_outcome_ownership_report(payload: Mapping[str, Any]) -> str:
    """Deterministic JSON serialization of a P3A projection."""

    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _provenance(value: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = dict(value or {})
    coverage_complete = bool(raw.get("coverage_complete", False))
    return {
        "synthetic": bool(raw.get("synthetic", True)),
        "label": _text(raw.get("label")) or "unspecified-synthetic-fixture",
        "audited_commit": _text(raw.get("audited_commit")) or NA,
        "default_namespace": _text(raw.get("source_namespace")) or "unspecified",
        "as_of": _optional_text(raw.get("as_of")),
        "evaluation_context": _optional_text(raw.get("evaluation_context")),
        "coverage_complete": coverage_complete,
        "coverage_note": (
            "Supplied records are the population. Absence of a row is not a losing "
            "trade and not proof of an empty historical universe."
            if not coverage_complete
            else "Caller asserted that the supplied records are complete for this fixture."
        ),
        "not_live_audit": True,
    }


def _mapping(row: Any) -> dict[str, Any]:
    if hasattr(row, "model_dump"):
        dumped = row.model_dump(mode="json")
        return copy.deepcopy(dumped)
    if isinstance(row, Mapping):
        return copy.deepcopy(dict(row))
    raise TypeError(f"unsupported outcome-ownership input type: {type(row)!r}")


def _retain_source_records(
    table: str,
    rows: Sequence[Any],
    default_namespace: str,
    key_fields: Sequence[str],
) -> list[dict[str, Any]]:
    retained: list[dict[str, Any]] = []
    seen_digest: set[tuple[Any, ...]] = set()
    for index, raw in enumerate(rows):
        payload = _mapping(raw)
        namespace = _namespace(payload, default_namespace)
        physical = _physical_key(payload, key_fields, index)
        digest = _payload_digest(payload)
        identity = (table, namespace, tuple(sorted(physical.items())), digest)
        if identity in seen_digest:
            continue
        seen_digest.add(identity)
        retained.append(
            {
                "table": table,
                "source_namespace": namespace,
                "physical_key": physical,
                "payload": payload,
                "digest": digest,
                "input_index": index,
            }
        )
    retained.sort(key=lambda item: _sort_tuple(item))
    return retained


def _namespace(payload: Mapping[str, Any], default: str) -> str:
    for name in ("source_namespace", "fixture_namespace", "evaluation_namespace"):
        text = _text(payload.get(name))
        if text:
            return text
    return default


def _physical_key(payload: Mapping[str, Any], key_fields: Sequence[str], index: int) -> dict[str, Any]:
    key: dict[str, Any] = {}
    for field in key_fields:
        if field in payload and payload[field] not in (None, ""):
            key[field] = payload[field]
    if "id" in payload and payload["id"] not in (None, ""):
        key["id"] = payload["id"]
    if not key:
        key["input_index"] = index
    return key


def _payload_digest(payload: Mapping[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _source_ref(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "table": item["table"],
        "source_namespace": item["source_namespace"],
        "physical_key": item["physical_key"],
        "digest": item["digest"],
    }


def _source_summary(items: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "retained_count": len(items),
        "references": [_source_ref(item) for item in items],
        "unit": "supplied source evidence row after exact-duplicate collapse",
    }


def _build_evaluations(
    records: Sequence[Mapping[str, Any]],
    progress: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    analytics: Sequence[Mapping[str, Any]],
    *,
    coverage_complete: bool,
) -> list[dict[str, Any]]:
    record_index = _index_records(records)
    grouped_progress = _group_items(progress, PROGRESS_PHYSICAL_FIELDS)
    evaluations: list[dict[str, Any]] = []
    for key, snapshots in grouped_progress.items():
        namespace, lifecycle_id, plan_identity = key
        record_match = record_index.get((namespace, lifecycle_id))
        merged, merge_conflicts = _merge_progress_snapshots(snapshots)
        attribution = _attribute_progress(merged, record_match, plan_identity)
        related_events = [
            item
            for item in events
            if item["source_namespace"] == namespace
            and _text(item["payload"].get("lifecycle_id")) == lifecycle_id
            and _event_matches_plan(item, plan_identity)
        ]
        related_analytics = [
            item
            for item in analytics
            if item["source_namespace"] == namespace
            and _text(item["payload"].get("lifecycle_id")) == lifecycle_id
        ]
        bound_analytics, unbound_analytics = _split_analytics_by_plan(
            related_analytics, plan_identity
        )
        entry_evidence_present = any(_event_is_entry_evidence(item) for item in related_events)
        economic = _economic_view(
            merged,
            bound_analytics,
            record_match,
            coverage_complete=coverage_complete,
            entry_evidence_present=entry_evidence_present,
        )
        integrity = attribution["status"]
        if merge_conflicts or economic["conflict"]:
            integrity = STATUS_CONFLICTING_ECONOMIC
        evaluations.append(
            {
                "source_namespace": namespace,
                "lifecycle_id": lifecycle_id,
                "plan_identity": plan_identity,
                "integrity_status": integrity,
                "attribution": attribution,
                "economic": economic,
                "merge_conflicts": merge_conflicts,
                "progress_evidence": [_source_ref(item) for item in snapshots],
                "event_evidence": [_source_ref(item) for item in related_events],
                "analytics_evidence": [_source_ref(item) for item in related_analytics],
                "analytics_plan_bound_evidence": [_source_ref(item) for item in bound_analytics],
                "analytics_lifecycle_unbound_evidence": [
                    _source_ref(item) for item in unbound_analytics
                ],
                "raw_labels": {
                    "progress_terminal_outcome": _optional_text(merged.get("terminal_outcome")),
                    "lifecycle_current_state": _optional_text(
                        record_match["payload"].get("current_state") if record_match else None
                    ),
                    "analytics_final_outcomes": [
                        _text(item["payload"].get("final_outcome")) for item in related_analytics
                    ],
                    "analytics_plan_bound_final_outcomes": [
                        _text(item["payload"].get("final_outcome")) for item in bound_analytics
                    ],
                    "analytics_lifecycle_unbound_final_outcomes": [
                        _text(item["payload"].get("final_outcome")) for item in unbound_analytics
                    ],
                    "economic_identity_reason": _optional_text(
                        record_match["payload"].get("economic_identity_reason") if record_match else None
                    ),
                },
                "evaluation_context": {
                    "tracking_start_at": _optional_text(merged.get("tracking_start_at")),
                    "execution_timeframe": _optional_text(merged.get("execution_timeframe")),
                    "first_evaluated_at": _optional_text(merged.get("first_evaluated_at")),
                    "source_namespace": namespace,
                    "complete": bool(_optional_text(merged.get("tracking_start_at"))),
                },
            }
        )
    evaluations.sort(key=lambda item: (item["source_namespace"], item["lifecycle_id"], item["plan_identity"]))
    evaluations.extend(_record_only_evaluations(records, evaluations, analytics, events))
    return evaluations


def _index_records(records: Sequence[Mapping[str, Any]]) -> dict[tuple[str, str], dict[str, Any] | None]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for item in records:
        lifecycle_id = _text(item["payload"].get("lifecycle_id"))
        if not lifecycle_id:
            continue
        grouped.setdefault((item["source_namespace"], lifecycle_id), []).append(item)
    indexed: dict[tuple[str, str], dict[str, Any] | None] = {}
    for key, items in grouped.items():
        if len(items) == 1:
            indexed[key] = items[0]
        else:
            indexed[key] = {
                "ambiguous_records": items,
                "payload": items[0]["payload"],
                "table": RECORD_TABLE,
                "source_namespace": key[0],
                "physical_key": {"lifecycle_id": key[1]},
                "digest": "ambiguous",
            }
    return indexed


def _group_items(
    items: Sequence[Mapping[str, Any]],
    fields: Sequence[str],
) -> dict[tuple[str, str, str], list[dict[str, Any]]]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for item in items:
        payload = item["payload"]
        key = (
            item["source_namespace"],
            _text(payload.get(fields[0])),
            _text(payload.get(fields[1])) if len(fields) > 1 else "",
        )
        grouped.setdefault(key, []).append(item)
    return grouped


def _merge_progress_snapshots(
    snapshots: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    if not snapshots:
        return {}, []
    merged = copy.deepcopy(snapshots[0]["payload"])
    conflicts: list[dict[str, Any]] = []
    digests = {item["digest"] for item in snapshots}
    if len(digests) == 1:
        return merged, conflicts
    conflicts.append(
        {
            "field": "payload",
            "values": sorted(digests),
            "reason": "same_physical_progress_key_has_contradictory_payloads",
            "evidence": [_source_ref(item) for item in snapshots],
        }
    )
    return merged, conflicts


def _attribute_progress(
    progress: Mapping[str, Any],
    record_match: Mapping[str, Any] | None,
    plan_identity: str,
) -> dict[str, Any]:
    if not plan_identity:
        return _attribution(STATUS_INCOMPLETE, "progress row is missing plan_identity")
    if record_match is None:
        return _attribution(
            STATUS_INCOMPLETE,
            "no lifecycle record was supplied for this lifecycle_id in this source namespace; "
            "do not invent plan_version_id",
        )
    if "ambiguous_records" in record_match:
        return _attribution(
            STATUS_AMBIGUOUS_CONTEXT,
            "multiple lifecycle records were supplied for one lifecycle_id in the same namespace",
        )
    record = record_match["payload"]
    stored_plan = _optional_text(record.get("plan_version_id"))
    reason = _optional_text(record.get("economic_identity_reason"))
    identity_conflict = bool(reason and REASON_PLAN_VERSION_INVARIANT_VIOLATION in reason)
    compatible = _compatible_identities(record)
    canonical = compatible[0] if compatible else None
    matches_current_geometry = plan_identity in compatible if compatible else False
    remint = _remint_plan_version_id(record)

    if identity_conflict:
        return _attribution(
            STATUS_CONFLICTING_IDENTITY,
            "lifecycle row reports plan_version_invariant_violation; current geometry cannot "
            "prove ownership of this progress row. Do not lend the latched id to a newer unbound plan.",
            stored_plan_version_id=stored_plan,
            reminted_plan_version_id=remint["identity"],
            canonical_plan_identity=canonical,
            matches_current_geometry=matches_current_geometry,
        )
    if stored_plan is None:
        status = STATUS_MISSING_LEGACY
        detail = "plan_version_id is unavailable on the supplied lifecycle record (legacy/unlocked/null)"
        if not matches_current_geometry:
            detail += "; progress plan_identity also does not match current record geometry"
        return _attribution(
            status,
            detail,
            stored_plan_version_id=None,
            reminted_plan_version_id=remint["identity"],
            canonical_plan_identity=canonical,
            matches_current_geometry=matches_current_geometry,
        )
    if remint["identity"] is None:
        return _attribution(
            STATUS_INCOMPLETE,
            remint["reason"] or "cannot remint plan_version_id from the supplied lifecycle snapshot",
            stored_plan_version_id=stored_plan,
            reminted_plan_version_id=None,
            canonical_plan_identity=canonical,
            matches_current_geometry=matches_current_geometry,
        )
    if remint["identity"] != stored_plan:
        return _attribution(
            STATUS_CONFLICTING_IDENTITY,
            "stored plan_version_id does not remint from the supplied lifecycle economics",
            stored_plan_version_id=stored_plan,
            reminted_plan_version_id=remint["identity"],
            canonical_plan_identity=canonical,
            matches_current_geometry=matches_current_geometry,
        )
    if not matches_current_geometry:
        return _attribution(
            STATUS_INCOMPLETE,
            "progress plan_identity does not match canonical/compatible identities of the supplied "
            "lifecycle snapshot; a newer mutable record cannot lend its plan_version_id to an older "
            "unbound outcome, and compatibility aliases alone are not proof",
            stored_plan_version_id=stored_plan,
            reminted_plan_version_id=remint["identity"],
            canonical_plan_identity=canonical,
            matches_current_geometry=False,
        )
    return _attribution(
        STATUS_VERIFIED,
        "progress plan_identity matches the supplied lifecycle snapshot and stored plan_version_id remints",
        stored_plan_version_id=stored_plan,
        reminted_plan_version_id=remint["identity"],
        canonical_plan_identity=canonical,
        matches_current_geometry=True,
        plan_version_id=stored_plan,
    )


def _attribution(status: str, reason: str, **extra: Any) -> dict[str, Any]:
    payload = {"status": status, "reason": reason, "plan_version_id": extra.get("plan_version_id")}
    payload.update(extra)
    return payload


def _compatible_identities(record: Mapping[str, Any]) -> tuple[str, ...]:
    try:
        identities = compatible_plan_identities(record)
        canonical = canonical_plan_identity(record)
        return tuple(dict.fromkeys((canonical, *identities)))
    except (TypeError, ValueError, AttributeError):
        return ()


def _remint_plan_version_id(record: Mapping[str, Any]) -> dict[str, Any]:
    setup_id = _optional_text(record.get("setup_id"))
    if setup_id is None:
        return {"identity": None, "reason": "setup_id_unavailable"}
    invalidation = _optional_text(record.get("invalidation_logic")) or _optional_text(
        record.get("invalidation_reason")
    )
    result = mint_plan_version_id(
        setup_id=setup_id,
        entry_low=record.get("entry_low"),
        entry_high=record.get("entry_high"),
        stop_loss=record.get("stop_loss"),
        tp1=record.get("tp1"),
        tp2=record.get("tp2"),
        tp3=record.get("tp3"),
        invalidation=invalidation,
    )
    return {"identity": result.identity, "reason": result.reason}


def _economic_view(
    progress: Mapping[str, Any],
    analytics: Sequence[Mapping[str, Any]],
    record_match: Mapping[str, Any] | None,
    *,
    coverage_complete: bool,
    entry_evidence_present: bool,
) -> dict[str, Any]:
    entry_at = _optional_text(progress.get("entry_at"))
    tp1_at = _optional_text(progress.get("tp1_at"))
    tp2_at = _optional_text(progress.get("tp2_at"))
    tp3_at = _optional_text(progress.get("tp3_at"))
    stop_at = _optional_text(progress.get("stop_at"))
    invalidated_at = _optional_text(progress.get("invalidated_at"))
    terminal = _terminal(progress.get("terminal_outcome"))
    lifecycle_state = _optional_text(
        record_match["payload"].get("current_state") if record_match else None
    )
    analytic_economic = [
        _text(item["payload"].get("final_outcome"))
        for item in analytics
        if _text(item["payload"].get("final_outcome")) in ANALYTICS_ECONOMIC
    ]
    successor_only = [
        _text(item["payload"].get("final_outcome"))
        for item in analytics
        if _text(item["payload"].get("final_outcome")) in ANALYTICS_SUCCESSORS
    ]
    conflict = False
    conflict_reason = None
    if terminal == SetupLifecycleState.TP_HIT.value and tp3_at is None:
        generic_tp = True
    else:
        generic_tp = False
    if terminal == SetupLifecycleState.SL_HIT.value and "TP3_HIT" in analytic_economic:
        conflict = True
        conflict_reason = "analytics_tp3_conflicts_with_progress_stop"
    if terminal == SetupLifecycleState.TP_HIT.value and "SL_HIT" in analytic_economic:
        conflict = True
        conflict_reason = "analytics_stop_conflicts_with_progress_tp"
    entry_relationship = _entry_relationship(
        terminal=terminal,
        entry_at=entry_at,
        coverage_complete=coverage_complete,
        entry_evidence_present=entry_evidence_present,
    )
    if generic_tp:
        semantic_terminal = "generic_tp_hit_not_promoted"
    elif terminal == SetupLifecycleState.TP_HIT.value and tp3_at is not None:
        semantic_terminal = "tp3_terminal"
    elif terminal == SetupLifecycleState.SL_HIT.value:
        semantic_terminal = (
            "stop_after_entry" if entry_relationship == "after_entry" else "stop_entry_relationship_uncertain"
        )
    elif terminal == SetupLifecycleState.INVALIDATED.value:
        if entry_relationship == "after_entry":
            semantic_terminal = "invalidation_after_entry"
        elif entry_relationship == "before_entry":
            semantic_terminal = "invalidation_before_entry"
        else:
            semantic_terminal = "invalidation_entry_relationship_uncertain"
    elif terminal == SetupLifecycleState.EXPIRED.value:
        if entry_relationship == "after_entry":
            semantic_terminal = "expiry_after_entry"
        elif entry_relationship == "before_entry":
            semantic_terminal = "expiry_before_entry"
        else:
            semantic_terminal = "expiry_entry_relationship_uncertain"
    elif terminal:
        semantic_terminal = "other_progress_terminal"
    elif entry_at:
        semantic_terminal = "open_after_entry"
    else:
        semantic_terminal = "open_or_unresolved_without_entry"

    if lifecycle_state in LIFECYCLE_SUCCESSORS and terminal in ECONOMIC_TERMINALS:
        successor_note = (
            "later lifecycle COOLDOWN/ARCHIVED does not erase supported economic terminal evidence"
        )
    else:
        successor_note = None
    if successor_only and terminal in ECONOMIC_TERMINALS:
        successor_note = successor_note or (
            "analytics COOLDOWN is a lifecycle successor snapshot, not an economic replacement"
        )
    return {
        "entry_at": entry_at,
        "tp1_at": tp1_at,
        "tp2_at": tp2_at,
        "tp3_at": tp3_at,
        "stop_at": stop_at,
        "invalidated_at": invalidated_at,
        "progress_terminal_outcome": terminal,
        "semantic_terminal": semantic_terminal,
        "entry_relationship": entry_relationship,
        "generic_tp_hit_not_promoted": generic_tp,
        "lifecycle_successor_state": lifecycle_state if lifecycle_state in LIFECYCLE_SUCCESSORS else None,
        "successor_note": successor_note,
        "conflict": conflict,
        "conflict_reason": conflict_reason,
        "analytic_economic_labels": analytic_economic,
        "fill_occurrence": False,
        "unique_trade": False,
        "simulated_entry_distinct_from_fill_occurrence": True,
    }


def _record_only_evaluations(
    records: Sequence[Mapping[str, Any]],
    evaluations: Sequence[Mapping[str, Any]],
    analytics: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    seen = {(item["source_namespace"], item["lifecycle_id"]) for item in evaluations}
    extras: list[dict[str, Any]] = []
    for item in records:
        lifecycle_id = _text(item["payload"].get("lifecycle_id"))
        key = (item["source_namespace"], lifecycle_id)
        if not lifecycle_id or key in seen:
            continue
        stored_plan = _optional_text(item["payload"].get("plan_version_id"))
        remint = _remint_plan_version_id(item["payload"])
        reason = _optional_text(item["payload"].get("economic_identity_reason"))
        if reason and REASON_PLAN_VERSION_INVARIANT_VIOLATION in reason:
            status = STATUS_CONFLICTING_IDENTITY
            detail = "lifecycle record reports plan_version_invariant_violation and has no matching progress"
        elif stored_plan and remint["identity"] == stored_plan:
            status = STATUS_INCOMPLETE
            detail = (
                "verified plan identity is represented on the lifecycle record, but no outcome "
                "progress row was supplied; absence is not a loss"
            )
        elif stored_plan is None:
            status = STATUS_MISSING_LEGACY
            detail = "lifecycle record has no plan_version_id and no outcome progress"
        else:
            status = STATUS_CONFLICTING_IDENTITY
            detail = "stored plan_version_id does not remint from this lifecycle snapshot"
        related_analytics = [
            row
            for row in analytics
            if row["source_namespace"] == item["source_namespace"]
            and _text(row["payload"].get("lifecycle_id")) == lifecycle_id
        ]
        related_events = [
            row
            for row in events
            if row["source_namespace"] == item["source_namespace"]
            and _text(row["payload"].get("lifecycle_id")) == lifecycle_id
        ]
        extras.append(
            {
                "source_namespace": item["source_namespace"],
                "lifecycle_id": lifecycle_id,
                "plan_identity": None,
                "integrity_status": status,
                "attribution": _attribution(
                    status,
                    detail,
                    stored_plan_version_id=stored_plan,
                    reminted_plan_version_id=remint["identity"],
                    plan_version_id=stored_plan if status == STATUS_INCOMPLETE and stored_plan else None,
                ),
                "economic": {
                    "semantic_terminal": "no_outcome_progress_supplied",
                    "conflict": False,
                    "fill_occurrence": False,
                    "unique_trade": False,
                },
                "merge_conflicts": [],
                "progress_evidence": [],
                "event_evidence": [_source_ref(row) for row in related_events],
                "analytics_evidence": [_source_ref(row) for row in related_analytics],
                "analytics_plan_bound_evidence": [],
                "analytics_lifecycle_unbound_evidence": [
                    _source_ref(row) for row in related_analytics
                ],
                "raw_labels": {
                    "lifecycle_current_state": _optional_text(item["payload"].get("current_state")),
                    "economic_identity_reason": reason,
                },
                "evaluation_context": {
                    "tracking_start_at": None,
                    "source_namespace": item["source_namespace"],
                    "complete": False,
                },
            }
        )
        seen.add(key)
    extras.sort(key=lambda item: (item["source_namespace"], item["lifecycle_id"] or ""))
    return extras


def _plan_identity_inventory(
    records: Sequence[Mapping[str, Any]],
    evaluations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    verified: dict[str, dict[str, Any]] = {}
    for item in records:
        payload = item["payload"]
        stored = _optional_text(payload.get("plan_version_id"))
        remint = _remint_plan_version_id(payload)
        reason = _optional_text(payload.get("economic_identity_reason"))
        if stored is None or remint["identity"] != stored:
            continue
        if reason and REASON_PLAN_VERSION_INVARIANT_VIOLATION in reason:
            continue
        verified.setdefault(
            stored,
            {
                "plan_version_id": stored,
                "lifecycle_ids": [],
                "source_namespaces": [],
            },
        )
        lifecycle_id = _text(payload.get("lifecycle_id"))
        if lifecycle_id and lifecycle_id not in verified[stored]["lifecycle_ids"]:
            verified[stored]["lifecycle_ids"].append(lifecycle_id)
        if item["source_namespace"] not in verified[stored]["source_namespaces"]:
            verified[stored]["source_namespaces"].append(item["source_namespace"])
    for evaluation in evaluations:
        plan_id = evaluation["attribution"].get("plan_version_id")
        if evaluation["integrity_status"] != STATUS_VERIFIED or not plan_id:
            continue
        verified.setdefault(
            plan_id,
            {
                "plan_version_id": plan_id,
                "lifecycle_ids": [],
                "source_namespaces": [],
            },
        )
        if evaluation["lifecycle_id"] not in verified[plan_id]["lifecycle_ids"]:
            verified[plan_id]["lifecycle_ids"].append(evaluation["lifecycle_id"])
        if evaluation["source_namespace"] not in verified[plan_id]["source_namespaces"]:
            verified[plan_id]["source_namespaces"].append(evaluation["source_namespace"])
    items = [verified[key] for key in sorted(verified)]
    return {
        "count": len(items),
        "unit": "verified plan_version_id represented in supplied evidence",
        "denominator": "supplied lifecycle records and verified progress bindings only",
        "values": items,
        "note": (
            "One plan_version_id counts once even when multiple lifecycle generations "
            "share it. That inventory count is not one economic outcome."
        ),
    }


def _plan_interpretations(evaluations: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for evaluation in evaluations:
        plan_id = evaluation["attribution"].get("plan_version_id")
        if evaluation["integrity_status"] != STATUS_VERIFIED or not plan_id:
            continue
        grouped.setdefault((evaluation["source_namespace"], plan_id), []).append(evaluation)

    interpretations: list[dict[str, Any]] = []
    for (namespace, plan_id), members in grouped.items():
        contexts = {
            (
                member["evaluation_context"].get("tracking_start_at"),
                member["lifecycle_id"],
                member["plan_identity"],
            )
            for member in members
        }
        if len(members) != 1:
            interpretations.append(
                {
                    "interpretable": False,
                    "plan_version_id": plan_id,
                    "source_namespace": namespace,
                    "integrity_status": STATUS_AMBIGUOUS_CONTEXT,
                    "reason": (
                        "one plan_version_id appears in multiple lifecycle/plan_identity "
                        "evaluation windows; collapsing them would invent a single history"
                    ),
                    "member_evaluations": [
                        {
                            "lifecycle_id": member["lifecycle_id"],
                            "plan_identity": member["plan_identity"],
                            "tracking_start_at": member["evaluation_context"].get("tracking_start_at"),
                        }
                        for member in members
                    ],
                    "economic_status": None,
                    "source_refs": [ref for member in members for ref in member["progress_evidence"]],
                }
            )
            continue
        member = members[0]
        if member["economic"].get("conflict") or member["merge_conflicts"]:
            interpretations.append(
                {
                    "interpretable": False,
                    "plan_version_id": plan_id,
                    "source_namespace": namespace,
                    "integrity_status": STATUS_CONFLICTING_ECONOMIC,
                    "reason": member["economic"].get("conflict_reason") or "contradictory economic evidence",
                    "member_evaluations": [
                        {
                            "lifecycle_id": member["lifecycle_id"],
                            "plan_identity": member["plan_identity"],
                        }
                    ],
                    "economic_status": None,
                    "retained_raw_terminal_outcome": member["economic"].get("progress_terminal_outcome"),
                    "source_refs": list(member["progress_evidence"]),
                }
            )
            continue
        if not member["evaluation_context"].get("complete"):
            terminal = member["economic"].get("progress_terminal_outcome")
            interpretations.append(
                {
                    "interpretable": False,
                    "plan_version_id": plan_id,
                    "source_namespace": namespace,
                    "integrity_status": STATUS_AMBIGUOUS_CONTEXT,
                    "reason": (
                        "verified plan binding is present and terminal progress evidence is retained, "
                        "but tracking_start_at is missing; a lifecycle terminal does not by itself "
                        "prove a coherent evaluation window. Missing anchors are not invented."
                        if terminal
                        else "evaluation start/horizon is not durably bound; missing anchors are not invented"
                    ),
                    "member_evaluations": [
                        {
                            "lifecycle_id": member["lifecycle_id"],
                            "plan_identity": member["plan_identity"],
                            "tracking_start_at": member["evaluation_context"].get("tracking_start_at"),
                        }
                    ],
                    "economic_status": None,
                    "retained_raw_terminal_outcome": terminal,
                    "retained_raw_labels": member.get("raw_labels"),
                    "evaluation_context": {
                        "tracking_start_at": member["evaluation_context"].get("tracking_start_at"),
                        "complete": False,
                    },
                    "source_refs": list(member["progress_evidence"]),
                }
            )
            continue
        interpretations.append(
            {
                "interpretable": True,
                "plan_version_id": plan_id,
                "source_namespace": namespace,
                "integrity_status": STATUS_VERIFIED,
                "lifecycle_id": member["lifecycle_id"],
                "plan_identity": member["plan_identity"],
                "economic_status": member["economic"]["semantic_terminal"],
                "milestones": {
                    "entry_at": member["economic"].get("entry_at"),
                    "tp1_at": member["economic"].get("tp1_at"),
                    "tp2_at": member["economic"].get("tp2_at"),
                    "tp3_at": member["economic"].get("tp3_at"),
                    "stop_at": member["economic"].get("stop_at"),
                    "invalidated_at": member["economic"].get("invalidated_at"),
                },
                "progress_terminal_outcome": member["economic"].get("progress_terminal_outcome"),
                "successor_note": member["economic"].get("successor_note"),
                "generic_tp_hit_not_promoted": member["economic"].get("generic_tp_hit_not_promoted"),
                "not_a_fill_occurrence": True,
                "not_a_unique_trade": True,
                "source_refs": list(member["progress_evidence"]),
                "retained_event_refs": list(member["event_evidence"]),
                "reason": "single verified evaluation context with monotonic progress evidence",
                "evaluation_context_complete": True,
                "contexts_observed": len(contexts),
            }
        )
    interpretations.sort(
        key=lambda item: (
            item.get("source_namespace") or "",
            item.get("plan_version_id") or "",
            item.get("lifecycle_id") or "",
        )
    )
    return interpretations


def _integrity_rollup(
    records: Sequence[Mapping[str, Any]],
    evaluations: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    categories = {name: 0 for name in INTEGRITY_CATEGORIES}
    for evaluation in evaluations:
        status = evaluation["integrity_status"]
        if status in categories:
            categories[status] += 1
        else:
            categories[STATUS_INCOMPLETE] += 1
    return {
        "unit": "diagnostic evaluation group (progress key or record-only lifecycle)",
        "mutually_exclusive": True,
        "categories": categories,
        "total": sum(categories.values()),
        "supplied_lifecycle_records": len(records),
        "all_supplied_evaluations_accounted": sum(categories.values()) == len(evaluations),
        "note": (
            "Integrity categories describe attribution, not win/loss. "
            "COOLDOWN/ARCHIVED is not an integrity bucket of its own."
        ),
    }


def _counts(
    records: Sequence[Mapping[str, Any]],
    progress: Sequence[Mapping[str, Any]],
    events: Sequence[Mapping[str, Any]],
    analytics: Sequence[Mapping[str, Any]],
    evaluations: Sequence[Mapping[str, Any]],
    interpretations: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Any],
) -> dict[str, Any]:
    interpretable = [item for item in interpretations if item.get("interpretable")]
    overlapping = {
        "entry_activation": sum(1 for item in interpretable if item["milestones"].get("entry_at")),
        "tp1": sum(1 for item in interpretable if item["milestones"].get("tp1_at")),
        "tp2": sum(1 for item in interpretable if item["milestones"].get("tp2_at")),
        "tp3": sum(1 for item in interpretable if item["milestones"].get("tp3_at")),
        "stop_after_entry": sum(1 for item in interpretable if item["economic_status"] == "stop_after_entry"),
        "invalidation_before_entry": sum(
            1 for item in interpretable if item["economic_status"] == "invalidation_before_entry"
        ),
        "expiry_before_entry": sum(
            1 for item in interpretable if item["economic_status"] == "expiry_before_entry"
        ),
        "open_or_unresolved": sum(
            1
            for item in interpretable
            if item["economic_status"] in {"open_after_entry", "open_or_unresolved_without_entry"}
        ),
    }
    return {
        "raw_source_rows": {
            "lifecycle_records": len(records),
            "outcome_progress": len(progress),
            "lifecycle_events": len(events),
            "outcome_analytics": len(analytics),
            "unit": "retained supplied rows after exact-duplicate collapse",
            "denominator": "caller-supplied input only",
        },
        "verified_plan_identities": {
            "value": inventory["count"],
            "unit": inventory["unit"],
            "denominator": inventory["denominator"],
        },
        "interpretable_plan_outcomes": {
            "value": len(interpretable),
            "unit": "diagnostic plan-level interpretation with one coherent evaluation context",
            "denominator": "verified plan_version_id groups that are not split across windows",
            "not": "unique trades, fills, or persisted canonical outcomes",
        },
        "unresolved_plan_groups": {
            "value": sum(1 for item in interpretations if not item.get("interpretable")),
            "unit": "verified plan_version_id that cannot be reduced to one outcome",
        },
        "overlapping_milestones": {
            **overlapping,
            "overlapping": True,
            "unit": "interpretable plan evaluations where the named timestamp is present",
            "note": (
                "A plan that reaches TP3 may also count toward TP1 and TP2. "
                "These counts are not disjoint trades."
            ),
        },
        "evaluation_groups": {
            "value": len(evaluations),
            "unit": "progress-key or record-only diagnostic group",
        },
    }


def _unavailable_metrics() -> dict[str, Any]:
    reason_trade = (
        "Unique-trade, fill-occurrence, win-rate, expectancy, and P&L metrics require an "
        "authoritative occurrence identity and an explicit exit policy. P3A does not mint them."
    )
    return {
        "fill_occurrence_count": {"status": UNAVAILABLE, "value": None, "reason": reason_trade},
        "manual_fill_count": {"status": UNAVAILABLE, "value": None, "reason": reason_trade},
        "unique_trade_count": {"status": UNAVAILABLE, "value": None, "reason": reason_trade},
        "unique_activation_occurrence_count": {
            "status": UNAVAILABLE,
            "value": None,
            "reason": "P2A already established that ENTRY_ACTIVATED event records are not occurrence ids.",
        },
        "win_rate": {"status": UNAVAILABLE, "value": None, "reason": reason_trade},
        "expectancy": {"status": UNAVAILABLE, "value": None, "reason": reason_trade},
        "realized_pnl": {"status": UNAVAILABLE, "value": None, "reason": reason_trade},
        "research_safety": UNSAFE,
    }


def _p2a_event_units(events: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    def _count(reason: str) -> int:
        matched = [item for item in events if _event_reason(item) == reason]
        return _event_record_count(matched)

    return {
        "unit": "setup_lifecycle_events row",
        "event_record_identity": (
            "(source_namespace, event_id) when event_id is present; otherwise the retained event row"
        ),
        "not_a_fill_occurrence": True,
        "not_a_unique_trade": True,
        "entry_activated_event_records": {
            "value": _count(_ENTRY_ACTIVATED),
            "not": "unique fill occurrence",
        },
        "entry_zone_touched_event_records": {
            "value": _count(_ENTRY_ZONE_TOUCHED),
            "not": "entry activation or fill",
        },
        "entry_fill_simulated_event_records": {
            "value": _count(_ENTRY_FILL_SIMULATED),
            "not": "verified manual fill or unique trade",
            "simulated_versus_confirmed": "indistinguishable",
        },
        "tp1_milestone_event_records": {"value": _count(_TP1_MILESTONE)},
        "tp2_milestone_event_records": {"value": _count(_TP2_MILESTONE)},
        "tp3_milestone_event_records": {"value": _count(_TP3_MILESTONE)},
        "watch_alert_activation": {
            "status": UNAVAILABLE,
            "value": None,
            "reason": "Watch alerts are not inferred from outcome tables.",
        },
        "public_delivery_creates_outcome": False,
        "setup_trigger_creates_fill": False,
        "zone_touch_creates_fill": False,
        "generic_entry_activation_creates_unique_trade": False,
    }


def _raw_versus_diagnostic(
    progress: Sequence[Mapping[str, Any]],
    evaluations: Sequence[Mapping[str, Any]],
    interpretations: Sequence[Mapping[str, Any]],
    inventory: Mapping[str, Any],
) -> list[dict[str, Any]]:
    raw_progress = len(progress)
    interpretable = sum(1 for item in interpretations if item.get("interpretable"))
    return [
        {
            "comparison": "raw_progress_rows_versus_interpretable_plan_outcomes",
            "raw": raw_progress,
            "diagnostic": interpretable,
            "reason": (
                "Progress rows are mutable projections keyed by (lifecycle_id, plan_identity). "
                "Interpretable outcomes require verified plan_version_id binding and one evaluation window."
            ),
            "source_trail": "setup_lifecycle_outcome_progress retained rows -> evaluations -> plan_interpretations",
        },
        {
            "comparison": "raw_progress_rows_versus_verified_plan_identities",
            "raw": raw_progress,
            "diagnostic": inventory["count"],
            "reason": (
                "Multiple progress rows can share one plan_version_id across generations, "
                "and rows without a proven binding do not enter the verified inventory."
            ),
            "source_trail": "progress + lifecycle records -> plan_identity_inventory",
        },
        {
            "comparison": "lifecycle_labels_versus_economic_terminals",
            "raw": [
                evaluation["raw_labels"].get("lifecycle_current_state")
                for evaluation in evaluations
            ],
            "diagnostic": [
                evaluation["economic"].get("semantic_terminal")
                for evaluation in evaluations
                if evaluation.get("plan_identity")
            ],
            "reason": (
                "EXPIRED/COOLDOWN/ARCHIVED/REJECTED are lifecycle labels. Economic terminals "
                "remain the progress terminal_outcome when present."
            ),
            "source_trail": "setup_lifecycle_records.current_state vs setup_lifecycle_outcome_progress.terminal_outcome",
        },
    ]


def _event_record_count(events: Sequence[Mapping[str, Any]]) -> int:
    identities: set[tuple[str, ...]] = set()
    for item in events:
        event_id = item["payload"].get("event_id")
        if event_id not in (None, ""):
            identities.add(("event_id", str(item["source_namespace"]), str(event_id)))
        else:
            identities.add(("retained_row", str(item["source_namespace"]), str(item["digest"])))
    return len(identities)


def _entry_relationship(
    *,
    terminal: str | None,
    entry_at: str | None,
    coverage_complete: bool,
    entry_evidence_present: bool,
) -> str:
    if entry_at:
        return "after_entry"
    if terminal == SetupLifecycleState.SL_HIT.value:
        return "uncertain"
    if terminal in {
        SetupLifecycleState.INVALIDATED.value,
        SetupLifecycleState.EXPIRED.value,
    }:
        if entry_evidence_present or not coverage_complete:
            return "uncertain"
        return "before_entry"
    return "uncertain"


def _event_is_entry_evidence(item: Mapping[str, Any]) -> bool:
    return _event_reason(item) in {_ENTRY_ACTIVATED, _ENTRY_FILL_SIMULATED}


def _split_analytics_by_plan(
    related_analytics: Sequence[Mapping[str, Any]],
    plan_identity: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bound: list[dict[str, Any]] = []
    unbound: list[dict[str, Any]] = []
    for item in related_analytics:
        bound_id = _analytics_plan_identity(item)
        if bound_id is not None and bound_id == plan_identity:
            bound.append(item)
        else:
            unbound.append(item)
    return bound, unbound


def _analytics_plan_identity(item: Mapping[str, Any]) -> str | None:
    payload = item["payload"]
    direct = _optional_text(payload.get("plan_identity"))
    if direct:
        return direct
    nested = payload.get("outcome_progress")
    if isinstance(nested, Mapping):
        noted = _optional_text(nested.get("plan_identity"))
        if noted:
            return noted
    raw = payload.get("raw_payload_json")
    if not raw or raw == NA:
        return None
    try:
        parsed = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(parsed, Mapping):
        return None
    nested = parsed.get("outcome_progress")
    if isinstance(nested, Mapping):
        return _optional_text(nested.get("plan_identity"))
    return _optional_text(parsed.get("plan_identity"))


def _event_matches_plan(item: Mapping[str, Any], plan_identity: str) -> bool:
    noted = _event_plan_identity(item)
    if noted is None:
        return True
    return noted == plan_identity


def _event_plan_identity(item: Mapping[str, Any]) -> str | None:
    payload = item["payload"]
    direct = _optional_text(payload.get("plan_identity"))
    if direct:
        return direct
    notes = payload.get("notes")
    if not notes or notes == NA:
        return None
    try:
        parsed = json.loads(notes) if isinstance(notes, str) else notes
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if isinstance(parsed, Mapping):
        return _optional_text(parsed.get("plan_identity"))
    return None


def _event_reason(item: Mapping[str, Any]) -> str:
    payload = item["payload"]
    value = payload.get("reason")
    if hasattr(value, "value"):
        return str(value.value)
    return _text(value)


def _terminal(value: Any) -> str | None:
    text = _optional_text(value)
    if text is None:
        return None
    if hasattr(value, "value") and not isinstance(value, str):
        text = str(value.value)
    return text


def _text(value: Any) -> str:
    if value is None:
        return ""
    if hasattr(value, "value") and not isinstance(value, str):
        value = value.value
    text = str(value).strip()
    return text


def _optional_text(value: Any) -> str | None:
    text = _text(value)
    if not text or text.upper() == NA:
        return None
    return text


def _sort_tuple(item: Mapping[str, Any]) -> tuple[Any, ...]:
    key = item.get("physical_key") or {}
    return (
        item.get("source_namespace") or "",
        item.get("table") or "",
        tuple(sorted((str(name), str(val)) for name, val in key.items())),
        item.get("digest") or "",
    )


__all__ = [
    "INTEGRITY_CATEGORIES",
    "OUTCOME_OWNERSHIP_VERSION",
    "STATUS_AMBIGUOUS_CONTEXT",
    "STATUS_CONFLICTING_ECONOMIC",
    "STATUS_CONFLICTING_IDENTITY",
    "STATUS_INCOMPLETE",
    "STATUS_MISSING_LEGACY",
    "STATUS_VERIFIED",
    "dumps_outcome_ownership_report",
    "project_outcome_ownership",
]
