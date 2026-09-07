"""Prospective immutable economic identity primitives (CCI P1).

``setup_id`` is structural setup lineage. ``plan_version_id`` is one immutable
version of plan economics/geometry. Neither replaces ``setup_identity``,
``lifecycle_id``, ``plan_identity``, or public ``event_key``.

P1 deliberately does **not** encode an exit/fill-policy version: the repository
has no truthful explicit policy contract to hash. ``plan_version_id`` is plan
economics only, not a trade occurrence.

Tick metadata is not available on the lifecycle write path. Identities therefore
use Decimal canonicalization without tick quantization. Callers that *do* have a
verified tick may pass ``tick_size`` to reject off-tick prices; values are never
silently rounded onto a tick.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleRecord

ECONOMIC_IDENTITY_SCHEMA_VERSION = "cci-economic-identity-v1"
SETUP_ID_SCHEMA_VERSION = "cci-setup-id-v1"
PLAN_VERSION_ID_SCHEMA_VERSION = "cci-plan-version-v1"
SETUP_ID_PREFIX = "setup-"
PLAN_VERSION_ID_PREFIX = "plan-version-"
CANONICAL_SEPARATOR = "\x1f"
OPTIONAL_NULL_TOKEN = "optional-null"

SETUP_ID_FIELD_ORDER: tuple[str, ...] = (
    "schema_version",
    "instrument_venue",
    "symbol",
    "direction",
    "mode",
    "structural_anchor",
)
PLAN_VERSION_ID_FIELD_ORDER: tuple[str, ...] = (
    "schema_version",
    "setup_id",
    "entry_low",
    "entry_high",
    "stop_loss",
    "tp1",
    "tp2",
    "tp3",
    "invalidation",
)

REASON_MISSING_INSTRUMENT_VENUE = "missing_instrument_venue"
REASON_MISSING_SYMBOL = "missing_symbol"
REASON_MISSING_DIRECTION = "missing_direction"
REASON_MISSING_MODE = "missing_mode"
REASON_MISSING_STRUCTURAL_ANCHOR = "missing_structural_anchor"
REASON_MISSING_SETUP_ID = "setup_id_unavailable"
REASON_MISSING_INVALIDATION = "missing_invalidation"
REASON_PLAN_GEOMETRY_UNLOCKED = "plan_geometry_unlocked"
REASON_INVALID_STORED_PLAN_GEOMETRY = "invalid_stored_plan_geometry"
REASON_PLAN_VERSION_INVARIANT_VIOLATION = "plan_version_invariant_violation"


class IdentityFieldKind(str, Enum):
    VALUE = "value"
    REQUIRED_MISSING = "required_missing"
    OPTIONAL_NULL = "optional_null"
    REJECTED = "rejected"


@dataclass(frozen=True)
class CanonicalField:
    name: str
    kind: IdentityFieldKind
    canonical: str | None = None
    reason: str | None = None


@dataclass(frozen=True)
class IdentityMintResult:
    identity: str | None
    available: bool
    reason: str | None
    fields: tuple[CanonicalField, ...] = ()

    @classmethod
    def unavailable(
        cls,
        reason: str,
        fields: tuple[CanonicalField, ...] = (),
    ) -> IdentityMintResult:
        return cls(identity=None, available=False, reason=reason, fields=fields)


def mint_setup_id(
    *,
    instrument_venue: Any,
    symbol: Any,
    direction: Any,
    mode: Any,
    structural_anchor: Any,
) -> IdentityMintResult:
    """Return a deterministic setup lineage id, or unavailable if required evidence is missing."""

    fields = (
        CanonicalField(
            name="schema_version",
            kind=IdentityFieldKind.VALUE,
            canonical=SETUP_ID_SCHEMA_VERSION,
        ),
        _required_identity_text("instrument_venue", instrument_venue, case="lower"),
        _required_symbol("symbol", symbol),
        _required_direction("direction", direction),
        _required_identity_text("mode", mode, case="lower"),
        _required_identity_text("structural_anchor", structural_anchor, case=None),
    )
    blocking = _first_blocking_reason(fields)
    if blocking is not None:
        return IdentityMintResult.unavailable(blocking, fields)
    return IdentityMintResult(
        identity=_digest(SETUP_ID_PREFIX, tuple(_canonical_value(field) for field in fields)),
        available=True,
        reason=None,
        fields=fields,
    )


def mint_plan_version_id(
    *,
    setup_id: Any,
    entry_low: Any,
    entry_high: Any,
    stop_loss: Any,
    tp1: Any,
    tp2: Any,
    tp3: Any,
    invalidation: Any,
    tick_size: Any = None,
) -> IdentityMintResult:
    """Return a deterministic immutable plan-economics id, independent of generation identity."""

    tick = _optional_tick_size(tick_size)
    if tick_size not in (None, "", NA) and tick is None:
        rejected = CanonicalField(
            name="tick_size",
            kind=IdentityFieldKind.REJECTED,
            reason="invalid_tick_size",
        )
        return IdentityMintResult.unavailable("invalid_tick_size", (rejected,))

    fields = (
        CanonicalField(
            name="schema_version",
            kind=IdentityFieldKind.VALUE,
            canonical=PLAN_VERSION_ID_SCHEMA_VERSION,
        ),
        _required_identity_text("setup_id", setup_id, case=None),
        _required_price("entry_low", entry_low, tick_size=tick),
        _required_price("entry_high", entry_high, tick_size=tick),
        _required_price("stop_loss", stop_loss, tick_size=tick),
        _required_price("tp1", tp1, tick_size=tick),
        _required_price("tp2", tp2, tick_size=tick),
        _required_price("tp3", tp3, tick_size=tick),
        _required_identity_text("invalidation", invalidation, case=None),
    )
    blocking = _first_blocking_reason(fields)
    if blocking is not None:
        return IdentityMintResult.unavailable(blocking, fields)
    return IdentityMintResult(
        identity=_digest(
            PLAN_VERSION_ID_PREFIX,
            tuple(_canonical_value(field) for field in fields),
        ),
        available=True,
        reason=None,
        fields=fields,
    )


def latch_economic_identities(
    record: SetupLifecycleRecord,
    *,
    instrument_venue: Any,
    plan_locked: bool,
    previous: SetupLifecycleRecord | None = None,
    tick_size: Any = None,
) -> SetupLifecycleRecord:
    """Latch prospective identities onto a lifecycle record without rewriting current ids.

    ``setup_id`` latches once lineage evidence exists and then never mutates.
    ``plan_version_id`` latches only while plan geometry is locked under the
    current ``PLAN_LOCK_STATES`` contract. TRIGGERED remains unlocked; complete
    economics in that state stay unlatched rather than claiming immutability.
    A later geometry change after latch is an invariant violation, not a silent
    overwrite and not a lifecycle-state repair.
    """

    prior = previous if previous is not None else record
    latched_setup = _optional_latched_id(prior.setup_id) or _optional_latched_id(record.setup_id)
    latched_plan = _optional_latched_id(prior.plan_version_id) or _optional_latched_id(
        record.plan_version_id
    )

    setup_result = (
        IdentityMintResult(identity=latched_setup, available=True, reason=None)
        if latched_setup is not None
        else mint_setup_id(
            instrument_venue=instrument_venue,
            symbol=record.symbol,
            direction=record.direction,
            mode=record.mode,
            structural_anchor=record.structural_anchor,
        )
    )
    setup_id = setup_result.identity
    reasons: list[str] = []
    if not setup_result.available and setup_result.reason:
        reasons.append(setup_result.reason)

    plan_result: IdentityMintResult
    if setup_id is None:
        plan_result = IdentityMintResult.unavailable(REASON_MISSING_SETUP_ID)
    else:
        candidate = mint_plan_version_id(
            setup_id=setup_id,
            entry_low=record.entry_low,
            entry_high=record.entry_high,
            stop_loss=record.stop_loss,
            tp1=record.tp1,
            tp2=record.tp2,
            tp3=record.tp3,
            invalidation=_stored_invalidation(record),
            tick_size=tick_size,
        )
        if latched_plan is not None:
            if (
                candidate.available
                and candidate.identity is not None
                and candidate.identity != latched_plan
            ):
                plan_result = IdentityMintResult(
                    identity=latched_plan,
                    available=True,
                    reason=REASON_PLAN_VERSION_INVARIANT_VIOLATION,
                )
            else:
                plan_result = IdentityMintResult(identity=latched_plan, available=True, reason=None)
        elif not plan_locked:
            plan_result = IdentityMintResult.unavailable(REASON_PLAN_GEOMETRY_UNLOCKED)
        elif not _record_has_valid_plan_geometry(record):
            plan_result = IdentityMintResult.unavailable(REASON_INVALID_STORED_PLAN_GEOMETRY)
        else:
            plan_result = candidate

    plan_version_id = plan_result.identity if plan_result.available else None
    if plan_result.reason:
        reasons.append(plan_result.reason)

    reason = _join_reasons(reasons)
    if (
        record.setup_id == setup_id
        and record.plan_version_id == plan_version_id
        and record.economic_identity_reason == reason
    ):
        return record
    return record.model_copy(
        update={
            "setup_id": setup_id,
            "plan_version_id": plan_version_id,
            "economic_identity_reason": reason,
        }
    )


def _record_has_valid_plan_geometry(record: SetupLifecycleRecord) -> bool:
    from app.lifecycle.outcome_policy import has_valid_stored_plan_geometry

    return has_valid_stored_plan_geometry(record)


def _digest(prefix: str, parts: tuple[str, ...]) -> str:
    payload = CANONICAL_SEPARATOR.join(parts)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return f"{prefix}{digest}"


def _canonical_value(field: CanonicalField) -> str:
    if field.kind is IdentityFieldKind.OPTIONAL_NULL:
        return OPTIONAL_NULL_TOKEN
    if field.kind is not IdentityFieldKind.VALUE or field.canonical is None:
        raise RuntimeError(f"refusing to hash non-value identity field {field.name}")
    return field.canonical


def _first_blocking_reason(fields: tuple[CanonicalField, ...]) -> str | None:
    for field in fields:
        if field.kind is IdentityFieldKind.VALUE:
            continue
        if field.kind is IdentityFieldKind.OPTIONAL_NULL:
            continue
        if field.reason:
            return field.reason
        if field.kind is IdentityFieldKind.REQUIRED_MISSING:
            return f"missing_{field.name}"
        return f"rejected_{field.name}"
    return None


def _required_symbol(name: str, value: Any) -> CanonicalField:
    text = _identity_text(value, case="upper")
    if text is None:
        return CanonicalField(name=name, kind=IdentityFieldKind.REQUIRED_MISSING, reason=REASON_MISSING_SYMBOL)
    return CanonicalField(name=name, kind=IdentityFieldKind.VALUE, canonical=text)


def _required_direction(name: str, value: Any) -> CanonicalField:
    text = _identity_text(value, case="lower")
    if text is None:
        return CanonicalField(
            name=name,
            kind=IdentityFieldKind.REQUIRED_MISSING,
            reason=REASON_MISSING_DIRECTION,
        )
    if text not in {"long", "short"}:
        return CanonicalField(
            name=name,
            kind=IdentityFieldKind.REJECTED,
            reason=f"invalid_{name}",
        )
    return CanonicalField(name=name, kind=IdentityFieldKind.VALUE, canonical=text)


def _required_identity_text(name: str, value: Any, *, case: str | None) -> CanonicalField:
    text = _identity_text(value, case=case)
    if text is None:
        reason = {
            "instrument_venue": REASON_MISSING_INSTRUMENT_VENUE,
            "mode": REASON_MISSING_MODE,
            "structural_anchor": REASON_MISSING_STRUCTURAL_ANCHOR,
            "setup_id": REASON_MISSING_SETUP_ID,
            "invalidation": REASON_MISSING_INVALIDATION,
        }.get(name, f"missing_{name}")
        return CanonicalField(name=name, kind=IdentityFieldKind.REQUIRED_MISSING, reason=reason)
    return CanonicalField(name=name, kind=IdentityFieldKind.VALUE, canonical=text)


def _required_price(name: str, value: Any, *, tick_size: Decimal | None) -> CanonicalField:
    if _is_required_missing(value):
        return CanonicalField(name=name, kind=IdentityFieldKind.REQUIRED_MISSING, reason=f"missing_{name}")
    if isinstance(value, bool) or isinstance(value, float):
        return CanonicalField(name=name, kind=IdentityFieldKind.REJECTED, reason=f"rejected_{name}")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return CanonicalField(name=name, kind=IdentityFieldKind.REJECTED, reason=f"malformed_{name}")
    if not number.is_finite():
        return CanonicalField(name=name, kind=IdentityFieldKind.REJECTED, reason=f"non_finite_{name}")
    if tick_size is not None and not _price_on_tick(number, tick_size):
        return CanonicalField(name=name, kind=IdentityFieldKind.REJECTED, reason=f"off_tick_{name}")
    canonical = format(number.normalize(), "f")
    if canonical in {"-0", "-0.0"}:
        canonical = "0"
    return CanonicalField(name=name, kind=IdentityFieldKind.VALUE, canonical=canonical)


def _optional_tick_size(value: Any) -> Decimal | None:
    if value in (None, "", NA):
        return None
    if isinstance(value, bool) or isinstance(value, float):
        return None
    try:
        tick = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError, ArithmeticError):
        return None
    if not tick.is_finite() or tick <= 0:
        return None
    return tick


def _price_on_tick(price: Decimal, tick_size: Decimal) -> bool:
    quotient = price / tick_size
    return quotient == quotient.to_integral_value()


def _identity_text(value: Any, *, case: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.upper() == NA:
        return None
    if case == "lower":
        return text.lower()
    if case == "upper":
        return text.upper()
    return text


def _is_required_missing(value: Any) -> bool:
    if value is None:
        return True
    text = str(value).strip()
    return not text or text.upper() == NA


def _stored_invalidation(record: Any) -> Any:
    for name in ("invalidation_logic", "invalidation_reason"):
        value = _field(record, name)
        text = _identity_text(value, case=None)
        if text is not None:
            return text
    return None


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _optional_latched_id(value: Any) -> str | None:
    return _identity_text(value, case=None)


def _join_reasons(reasons: list[str]) -> str | None:
    unique = tuple(dict.fromkeys(reason for reason in reasons if reason))
    if not unique:
        return None
    return ";".join(unique)


__all__ = [
    "CANONICAL_SEPARATOR",
    "CanonicalField",
    "ECONOMIC_IDENTITY_SCHEMA_VERSION",
    "IdentityFieldKind",
    "IdentityMintResult",
    "OPTIONAL_NULL_TOKEN",
    "PLAN_VERSION_ID_FIELD_ORDER",
    "PLAN_VERSION_ID_PREFIX",
    "PLAN_VERSION_ID_SCHEMA_VERSION",
    "REASON_INVALID_STORED_PLAN_GEOMETRY",
    "REASON_MISSING_DIRECTION",
    "REASON_MISSING_INSTRUMENT_VENUE",
    "REASON_MISSING_INVALIDATION",
    "REASON_MISSING_MODE",
    "REASON_MISSING_SETUP_ID",
    "REASON_MISSING_STRUCTURAL_ANCHOR",
    "REASON_MISSING_SYMBOL",
    "REASON_PLAN_GEOMETRY_UNLOCKED",
    "REASON_PLAN_VERSION_INVARIANT_VIOLATION",
    "SETUP_ID_FIELD_ORDER",
    "SETUP_ID_PREFIX",
    "SETUP_ID_SCHEMA_VERSION",
    "latch_economic_identities",
    "mint_plan_version_id",
    "mint_setup_id",
]
