"""P3_PREFIX compact supplied-prefix disposition evidence.

This module records one pass-local envelope for the most recent completed
eligibility-filter application whose same-pass progress write was accepted.
It does not mint evaluation/trade ids, store candle payloads, or prove
complete market coverage.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Final

from app.data.candle_integrity import CausalCandle, ClosedCandleWindow, normalize_utc_timestamp
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleOutcomeProgress

PREFIX_EVIDENCE_CONTRACT_VERSION: Final[str] = "cci-eligibility-prefix-evidence-v1"
PREFIX_EVIDENCE_STATUS_KNOWN: Final[str] = "known"
PREFIX_EVIDENCE_STATUS_MALFORMED: Final[str] = "malformed"
PREFIX_EVIDENCE_STATUS_CONFLICTING: Final[str] = "conflicting"

DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES: Final[str] = "NO_ELIGIBLE_CLOSED_CANDLES"
DISPOSITION_WAITING_FOR_TRACKING_START: Final[str] = "WAITING_FOR_TRACKING_START"
DISPOSITION_NO_NEW_PENDING_CANDLES: Final[str] = "NO_NEW_PENDING_CANDLES"
DISPOSITION_PENDING_SUFFIX_EXHAUSTED: Final[str] = "PENDING_SUFFIX_EXHAUSTED"
DISPOSITION_POLICY_TERMINAL: Final[str] = "POLICY_TERMINAL"
DISPOSITION_POST_FILTER_BLOCKED: Final[str] = "POST_FILTER_BLOCKED"
DISPOSITION_PROCESSING_ABORTED: Final[str] = "PROCESSING_ABORTED"

PREFIX_DISPOSITIONS: Final[frozenset[str]] = frozenset(
    {
        DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES,
        DISPOSITION_WAITING_FOR_TRACKING_START,
        DISPOSITION_NO_NEW_PENDING_CANDLES,
        DISPOSITION_PENDING_SUFFIX_EXHAUSTED,
        DISPOSITION_POLICY_TERMINAL,
        DISPOSITION_POST_FILTER_BLOCKED,
        DISPOSITION_PROCESSING_ABORTED,
    }
)

_WINDOW_KEYS: Final[tuple[str, ...]] = (
    "count",
    "first_open_at",
    "last_open_at",
    "last_close_at",
)
_PENDING_KEYS: Final[tuple[str, ...]] = (
    "established",
    "count",
    "first_open_at",
    "last_open_at",
    "last_close_at",
    "expected_next_open_at",
    "unknown_reason",
)
_COMPLETED_KEYS: Final[tuple[str, ...]] = ("count", "last_open_at", "last_close_at")
_CURSOR_KEYS: Final[tuple[str, ...]] = ("open_at", "close_at")
_ENVELOPE_KEYS: Final[frozenset[str]] = frozenset(
    {
        "contract_version",
        "lifecycle_id",
        "plan_identity",
        "applied_cutoff",
        "execution_timeframe",
        "tracking_start_at",
        "cursor_before",
        "supplied_window",
        "pending_suffix",
        "completed_work",
        "cursor_after",
        "disposition",
        "pending_suffix_exhausted",
    }
)


@dataclass
class PrefixPassEvidence:
    """Mutable pass-local witness for one returned eligibility filter."""

    execution_timeframe: str
    applied_cutoff: str
    supplied: tuple[CausalCandle, ...]
    cursor_before_open_at: str | None
    cursor_before_close_at: str | None
    pending: tuple[CausalCandle, ...] | None = None
    expected_next_open_at: datetime | None = None
    pending_unknown_reason: str | None = "pending_rules_not_evaluated"
    disposition: str | None = None
    _completed: list[CausalCandle] = field(default_factory=list)

    def mark_pending(
        self,
        pending: Sequence[CausalCandle],
        *,
        expected_next_open: datetime | None,
    ) -> None:
        self.pending = tuple(pending)
        self.expected_next_open_at = expected_next_open
        self.pending_unknown_reason = None

    def record_completed(self, causal: CausalCandle) -> None:
        self._completed.append(causal)

    @property
    def completed(self) -> tuple[CausalCandle, ...]:
        return tuple(self._completed)

    @property
    def pending_established(self) -> bool:
        return self.pending is not None


def start_prefix_pass_evidence(
    progress: SetupLifecycleOutcomeProgress,
    window: ClosedCandleWindow,
    *,
    execution_timeframe: str,
) -> PrefixPassEvidence:
    return PrefixPassEvidence(
        execution_timeframe=str(execution_timeframe).strip().lower(),
        applied_cutoff=window.decision_timestamp.isoformat(),
        supplied=tuple(window.timeline),
        cursor_before_open_at=_optional_text(progress.evaluation_cursor_open_at),
        cursor_before_close_at=_optional_text(progress.evaluation_cursor_close_at),
    )


def bind_prefix_disposition(
    progress: SetupLifecycleOutcomeProgress,
    evidence: PrefixPassEvidence,
    *,
    disposition: str | None,
) -> SetupLifecycleOutcomeProgress:
    """Attach a validated envelope, or NULL when the new evidence is unusable."""

    if disposition is not None:
        evidence.disposition = disposition
    payload = build_prefix_evidence_payload(progress, evidence)
    return progress.model_copy(
        update={"last_eligibility_prefix_evidence_json": serialize_prefix_evidence(payload)}
    )


def build_prefix_evidence_payload(
    progress: SetupLifecycleOutcomeProgress,
    evidence: PrefixPassEvidence,
) -> dict[str, Any] | None:
    disposition = evidence.disposition
    if disposition not in PREFIX_DISPOSITIONS:
        return None
    pending = evidence.pending
    pending_established = pending is not None
    pending_count = len(pending) if pending_established else None
    completed = evidence.completed
    completed_count = len(completed)
    exhausted = _pending_suffix_exhausted(
        disposition=disposition,
        pending_established=pending_established,
        pending_count=pending_count,
        completed_count=completed_count,
    )
    payload = {
        "contract_version": PREFIX_EVIDENCE_CONTRACT_VERSION,
        "lifecycle_id": progress.lifecycle_id,
        "plan_identity": progress.plan_identity,
        "applied_cutoff": evidence.applied_cutoff,
        "execution_timeframe": evidence.execution_timeframe,
        "tracking_start_at": _optional_text(progress.tracking_start_at),
        "cursor_before": _cursor_pair(
            evidence.cursor_before_open_at,
            evidence.cursor_before_close_at,
        ),
        "supplied_window": _window_bounds(evidence.supplied),
        "pending_suffix": _pending_bounds(
            pending,
            established=pending_established,
            expected_next_open_at=_iso(evidence.expected_next_open_at),
            unknown_reason=None if pending_established else evidence.pending_unknown_reason,
        ),
        "completed_work": {
            "count": completed_count,
            "last_open_at": _iso(completed[-1].open_timestamp) if completed else None,
            "last_close_at": _iso(completed[-1].close_timestamp) if completed else None,
        },
        "cursor_after": _cursor_pair(
            _optional_text(progress.evaluation_cursor_open_at),
            _optional_text(progress.evaluation_cursor_close_at),
        ),
        "disposition": disposition,
        "pending_suffix_exhausted": exhausted,
    }
    if not _structurally_valid_envelope(payload):
        return None
    return payload


def serialize_prefix_evidence(payload: Mapping[str, Any] | None) -> str | None:
    if payload is None:
        return None
    if not _structurally_valid_envelope(payload):
        return None
    return json.dumps(dict(payload), sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def diagnose_prefix_evidence(
    *,
    raw_json: str | None,
    lifecycle_id: str,
    plan_identity: str,
    applied_cutoff: str | None,
    execution_timeframe: str | None,
) -> dict[str, Any]:
    """Read-only diagnostic of stored envelope text. Never invents a positive claim."""

    raw = _optional_text(raw_json)
    contract = (
        "Compact same-pass supplied-prefix disposition for the last accepted eligibility "
        "application. Not complete market coverage, not an evaluation occurrence, and not "
        "a canonical outcome."
    )
    if raw is None:
        return {
            "status": "unavailable",
            "contract": contract,
            "raw": None,
            "payload": None,
            "conflicts": [],
            "disposition": None,
            "pending_suffix_exhausted": None,
        }
    try:
        loaded = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {
            "status": PREFIX_EVIDENCE_STATUS_MALFORMED,
            "contract": contract,
            "raw": raw,
            "payload": None,
            "conflicts": [{"field": "last_eligibility_prefix_evidence_json", "reason": "not_json_object"}],
            "disposition": None,
            "pending_suffix_exhausted": None,
        }
    if not isinstance(loaded, Mapping) or not _structurally_valid_envelope(loaded):
        return {
            "status": PREFIX_EVIDENCE_STATUS_MALFORMED,
            "contract": contract,
            "raw": raw,
            "payload": loaded if isinstance(loaded, Mapping) else None,
            "conflicts": [
                {"field": "last_eligibility_prefix_evidence_json", "reason": "invalid_envelope_structure"}
            ],
            "disposition": None,
            "pending_suffix_exhausted": None,
        }
    conflicts = _owner_conflicts(
        loaded,
        lifecycle_id=lifecycle_id,
        plan_identity=plan_identity,
        applied_cutoff=applied_cutoff,
        execution_timeframe=execution_timeframe,
    )
    if conflicts:
        return {
            "status": PREFIX_EVIDENCE_STATUS_CONFLICTING,
            "contract": contract,
            "raw": raw,
            "payload": dict(loaded),
            "conflicts": conflicts,
            "disposition": None,
            "pending_suffix_exhausted": None,
        }
    exhausted = loaded.get("pending_suffix_exhausted")
    return {
        "status": PREFIX_EVIDENCE_STATUS_KNOWN,
        "contract": contract,
        "raw": raw,
        "payload": dict(loaded),
        "conflicts": [],
        "disposition": loaded.get("disposition"),
        "pending_suffix_exhausted": exhausted if exhausted is None or isinstance(exhausted, bool) else None,
    }


def _pending_suffix_exhausted(
    *,
    disposition: str,
    pending_established: bool,
    pending_count: int | None,
    completed_count: int,
) -> bool | None:
    if disposition == DISPOSITION_NO_NEW_PENDING_CANDLES:
        return True
    if disposition in {
        DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES,
        DISPOSITION_WAITING_FOR_TRACKING_START,
        DISPOSITION_POST_FILTER_BLOCKED,
    }:
        return None
    if not pending_established or pending_count is None:
        return None
    if disposition == DISPOSITION_PROCESSING_ABORTED:
        return False
    if disposition == DISPOSITION_PENDING_SUFFIX_EXHAUSTED:
        return True
    if disposition == DISPOSITION_POLICY_TERMINAL:
        return completed_count == pending_count
    return None


def _window_bounds(candles: Sequence[CausalCandle]) -> dict[str, Any]:
    if not candles:
        return {
            "count": 0,
            "first_open_at": None,
            "last_open_at": None,
            "last_close_at": None,
        }
    return {
        "count": len(candles),
        "first_open_at": _iso(candles[0].open_timestamp),
        "last_open_at": _iso(candles[-1].open_timestamp),
        "last_close_at": _iso(candles[-1].close_timestamp),
    }


def _pending_bounds(
    candles: Sequence[CausalCandle] | None,
    *,
    established: bool,
    expected_next_open_at: str | None,
    unknown_reason: str | None,
) -> dict[str, Any]:
    if not established or candles is None:
        return {
            "established": False,
            "count": None,
            "first_open_at": None,
            "last_open_at": None,
            "last_close_at": None,
            "expected_next_open_at": expected_next_open_at,
            "unknown_reason": unknown_reason or "pending_rules_not_evaluated",
        }
    bounds = _window_bounds(candles)
    return {
        "established": True,
        "count": bounds["count"],
        "first_open_at": bounds["first_open_at"],
        "last_open_at": bounds["last_open_at"],
        "last_close_at": bounds["last_close_at"],
        "expected_next_open_at": expected_next_open_at,
        "unknown_reason": None,
    }


def _cursor_pair(open_at: str | None, close_at: str | None) -> dict[str, str | None] | None:
    if open_at is None and close_at is None:
        return None
    return {"open_at": open_at, "close_at": close_at}


def _structurally_valid_envelope(payload: Mapping[str, Any]) -> bool:
    if set(payload) != _ENVELOPE_KEYS:
        return False
    if payload.get("contract_version") != PREFIX_EVIDENCE_CONTRACT_VERSION:
        return False
    if not _required_text(payload.get("lifecycle_id")):
        return False
    if not _required_text(payload.get("plan_identity")):
        return False
    if not _valid_timestamp(payload.get("applied_cutoff"), field_name="applied_cutoff"):
        return False
    if not _required_text(payload.get("execution_timeframe")):
        return False
    if payload.get("tracking_start_at") is not None and not _valid_timestamp(
        payload.get("tracking_start_at"),
        field_name="tracking_start_at",
    ):
        return False
    if not _valid_cursor_pair(payload.get("cursor_before"), field_name="cursor_before"):
        return False
    if not _valid_cursor_pair(payload.get("cursor_after"), field_name="cursor_after"):
        return False
    if not _valid_window(payload.get("supplied_window"), allow_zero=True):
        return False
    pending = payload.get("pending_suffix")
    if not isinstance(pending, Mapping) or set(pending) != set(_PENDING_KEYS):
        return False
    if not isinstance(pending.get("established"), bool):
        return False
    if pending["established"]:
        if not _valid_window(pending, allow_zero=True, extra_allowed=set(_PENDING_KEYS) - set(_WINDOW_KEYS)):
            return False
        if pending.get("unknown_reason") is not None:
            return False
        expected = pending.get("expected_next_open_at")
        if expected is not None and not _valid_timestamp(expected, field_name="expected_next_open_at"):
            return False
    else:
        if pending.get("count") is not None:
            return False
        for key in ("first_open_at", "last_open_at", "last_close_at"):
            if pending.get(key) is not None:
                return False
        if not _required_text(pending.get("unknown_reason")):
            return False
        expected = pending.get("expected_next_open_at")
        if expected is not None and not _valid_timestamp(expected, field_name="expected_next_open_at"):
            return False
    completed = payload.get("completed_work")
    if not isinstance(completed, Mapping) or set(completed) != set(_COMPLETED_KEYS):
        return False
    if not _non_negative_int(completed.get("count")):
        return False
    if completed["count"] == 0:
        if completed.get("last_open_at") is not None or completed.get("last_close_at") is not None:
            return False
    else:
        if not _valid_timestamp(completed.get("last_open_at"), field_name="completed_last_open_at"):
            return False
        if not _valid_timestamp(completed.get("last_close_at"), field_name="completed_last_close_at"):
            return False
    if pending["established"] and _non_negative_int(pending.get("count")):
        if completed["count"] > int(pending["count"]):
            return False
    if payload.get("disposition") not in PREFIX_DISPOSITIONS:
        return False
    exhausted = payload.get("pending_suffix_exhausted")
    if exhausted is not None and not isinstance(exhausted, bool):
        return False
    return True


def _valid_window(
    value: Any,
    *,
    allow_zero: bool,
    extra_allowed: set[str] | None = None,
) -> bool:
    if not isinstance(value, Mapping):
        return False
    allowed = set(_WINDOW_KEYS) | (extra_allowed or set())
    if extra_allowed is None and set(value) != set(_WINDOW_KEYS):
        return False
    if extra_allowed is not None and not set(_WINDOW_KEYS) <= set(value) <= allowed:
        return False
    count = value.get("count")
    if not _non_negative_int(count):
        return False
    if count == 0:
        return (
            allow_zero
            and value.get("first_open_at") is None
            and value.get("last_open_at") is None
            and value.get("last_close_at") is None
        )
    return (
        _valid_timestamp(value.get("first_open_at"), field_name="first_open_at")
        and _valid_timestamp(value.get("last_open_at"), field_name="last_open_at")
        and _valid_timestamp(value.get("last_close_at"), field_name="last_close_at")
    )


def _valid_cursor_pair(value: Any, *, field_name: str) -> bool:
    if value is None:
        return True
    if not isinstance(value, Mapping) or set(value) != set(_CURSOR_KEYS):
        return False
    open_at = value.get("open_at")
    close_at = value.get("close_at")
    if open_at is None and close_at is None:
        return False
    if open_at is not None and not _valid_timestamp(open_at, field_name=f"{field_name}_open_at"):
        return False
    if close_at is not None and not _valid_timestamp(close_at, field_name=f"{field_name}_close_at"):
        return False
    return True


def _owner_conflicts(
    payload: Mapping[str, Any],
    *,
    lifecycle_id: str,
    plan_identity: str,
    applied_cutoff: str | None,
    execution_timeframe: str | None,
) -> list[dict[str, str]]:
    conflicts: list[dict[str, str]] = []
    if payload.get("lifecycle_id") != lifecycle_id:
        conflicts.append({"field": "lifecycle_id", "reason": "envelope_owner_mismatch"})
    if payload.get("plan_identity") != plan_identity:
        conflicts.append({"field": "plan_identity", "reason": "envelope_owner_mismatch"})
    cutoff = _optional_text(applied_cutoff)
    envelope_cutoff = _optional_text(payload.get("applied_cutoff"))
    if cutoff != envelope_cutoff:
        conflicts.append({"field": "applied_cutoff", "reason": "envelope_cutoff_mismatch"})
    timeframe = _optional_text(execution_timeframe)
    if timeframe and str(payload.get("execution_timeframe") or "").strip().lower() != timeframe.lower():
        conflicts.append({"field": "execution_timeframe", "reason": "envelope_timeframe_mismatch"})
    return conflicts


def _valid_timestamp(value: Any, *, field_name: str) -> bool:
    text = _optional_text(value)
    if text is None:
        return False
    try:
        normalize_utc_timestamp(text, field_name=field_name)
    except ValueError:
        return False
    return True


def _non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _required_text(value: Any) -> bool:
    return _optional_text(value) is not None


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == NA:
        return None
    return text


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


__all__ = [
    "DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES",
    "DISPOSITION_NO_NEW_PENDING_CANDLES",
    "DISPOSITION_PENDING_SUFFIX_EXHAUSTED",
    "DISPOSITION_POLICY_TERMINAL",
    "DISPOSITION_POST_FILTER_BLOCKED",
    "DISPOSITION_PROCESSING_ABORTED",
    "DISPOSITION_WAITING_FOR_TRACKING_START",
    "PREFIX_DISPOSITIONS",
    "PREFIX_EVIDENCE_CONTRACT_VERSION",
    "PREFIX_EVIDENCE_STATUS_CONFLICTING",
    "PREFIX_EVIDENCE_STATUS_KNOWN",
    "PREFIX_EVIDENCE_STATUS_MALFORMED",
    "PrefixPassEvidence",
    "bind_prefix_disposition",
    "build_prefix_evidence_payload",
    "diagnose_prefix_evidence",
    "serialize_prefix_evidence",
    "start_prefix_pass_evidence",
]
