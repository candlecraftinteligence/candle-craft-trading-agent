from __future__ import annotations

from typing import Any


def canonical_public_event_key(canonical_plan_id: Any, event_type: Any) -> str:
    """Delegate to the existing canonical public-alert event-key builder."""

    from app.alerts.telegram_lifecycle import _public_watchlist_event_key

    return _public_watchlist_event_key(canonical_plan_id, event_type)


__all__ = ["canonical_public_event_key"]
