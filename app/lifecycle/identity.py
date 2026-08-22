from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState

GENERATION_IDENTITY_VERSION = "cci-lifecycle-generation-v1"


def setup_geometry_identity(
    *,
    symbol: Any,
    mode: Any,
    direction: Any,
    entry_low: Any,
    entry_high: Any,
    stop_loss: Any,
    invalidation_reason: Any,
) -> str:
    """Return the established PR #82-compatible setup geometry identity."""

    return "|".join(
        _text(value)
        for value in (
            _symbol(symbol),
            _identity_text(mode),
            _identity_text(direction),
            entry_low,
            entry_high,
            stop_loss,
            invalidation_reason,
        )
    )


def new_setup_generation_id(
    *,
    symbol: Any,
    mode: Any,
    direction: Any,
    structural_anchor: Any,
    fallback_factory: Callable[[], str] | None = None,
) -> str:
    """Create one immutable generation ID at a genuine lifecycle boundary.

    A market-event anchor makes the ID deterministic and replay-safe. Setups
    without an anchor receive one persisted UUID at creation; the UUID is never
    regenerated while that lifecycle remains current.
    """

    anchor = _text(structural_anchor)
    if anchor != NA:
        seed = "\x1f".join(
            (
                GENERATION_IDENTITY_VERSION,
                _symbol(symbol),
                _identity_text(mode),
                _identity_text(direction),
                anchor,
            )
        )
        return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:32]
    factory = fallback_factory or (lambda: uuid4().hex)
    return _text(factory())


def generation_rotation_reason(
    record: SetupLifecycleRecord | None,
    *,
    observed_structural_anchor: Any,
    setup_observable: bool,
    terminal_observation: bool,
    now: str,
) -> str | None:
    """Return the deterministic reason to start a new persisted generation."""

    if record is None or not setup_observable or terminal_observation:
        return None

    observed_anchor = _text(observed_structural_anchor)
    stored_anchor = _text(record.structural_anchor)
    if observed_anchor != NA and stored_anchor != NA:
        if observed_anchor != stored_anchor:
            return "new_structural_anchor"
        return None
    if stored_anchor != NA and observed_anchor == NA:
        return None

    if record.current_state == SetupLifecycleState.ARCHIVED:
        return "archived_lifecycle_new_setup"
    if record.current_state == SetupLifecycleState.COOLDOWN and _timestamp_reached(
        record.cooldown_until,
        now,
    ):
        return "completed_cooldown_new_setup"
    return None


def _timestamp_reached(value: Any, now: Any) -> bool:
    expiry = _parse_timestamp(value)
    current = _parse_timestamp(now)
    if expiry is None or current is None:
        return False
    return current >= expiry


def _parse_timestamp(value: Any) -> datetime | None:
    text = _text(value)
    if text == NA:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _symbol(value: Any) -> str:
    text = _text(value)
    return text.upper() if text != NA else NA


def _identity_text(value: Any) -> str:
    text = _text(value)
    return text.lower() if text != NA else NA


def _text(value: Any) -> str:
    if value is None:
        return NA
    text = str(value).strip()
    return text if text and text.upper() != NA else NA


__all__ = [
    "GENERATION_IDENTITY_VERSION",
    "generation_rotation_reason",
    "new_setup_generation_id",
    "setup_geometry_identity",
]
