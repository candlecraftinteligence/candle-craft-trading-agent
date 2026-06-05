from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.data.dtos import NA

WATCHLIST_EXPIRY_HOURS = 48
WATCHLIST_EXPIRY_AGE = timedelta(hours=WATCHLIST_EXPIRY_HOURS)
WATCHLIST_EXPIRY_REASON = "watchlist_expired_48h"
WATCHLIST_EXPIRY_TIMESTAMP_NA_REASON = "watchlist_expiry_timestamp_na"
WATCHLIST_EXPIRY_TIMESTAMP_UNVERIFIED_REASON = "watchlist_expiry_timestamp_unverified"

WATCHLIST_EXPIRABLE_STATE_KEYS = {
    "watch",
    "watchlist",
    "watchlisted",
    "watchlist_only",
    "watching",
    "stalking",
    "conditional_watch",
    "conditional_watch_candidate",
    "conditional_watchlist",
    "watchlist_near_miss",
}

WATCHLIST_NON_EXPIRING_STATE_KEYS = {
    "triggered",
    "confirmed",
    "executing",
    "managing",
    "limit_hit",
    "limit_zone_hit",
    "signal_confirmed",
    "tp_hit",
    "tp1_hit",
    "tp2_hit",
    "tp3_hit",
    "sl_hit",
    "invalidated",
    "expired",
    "cooldown",
    "cooled_down",
    "no_longer_tracking",
    "removed",
    "cancelled",
    "canceled",
    "closed",
    "archived",
}


@dataclass(frozen=True)
class WatchlistExpiryDecision:
    expired: bool
    reason: str = NA
    anchor_timestamp: str = NA
    timestamp_available: bool = True


def watchlist_expiry_decision(
    *,
    timestamp_candidates: Sequence[Any],
    state_candidates: Sequence[Any],
    now: datetime | None = None,
) -> WatchlistExpiryDecision:
    if not is_expirable_watch_state(state_candidates):
        return WatchlistExpiryDecision(False)

    anchor = _first_display(timestamp_candidates)
    if anchor == NA:
        return WatchlistExpiryDecision(False, WATCHLIST_EXPIRY_TIMESTAMP_NA_REASON, timestamp_available=False)

    parsed = parse_utc_timestamp(anchor)
    if parsed is None:
        return WatchlistExpiryDecision(
            False,
            WATCHLIST_EXPIRY_TIMESTAMP_UNVERIFIED_REASON,
            anchor_timestamp=anchor,
            timestamp_available=False,
        )

    reference = now or datetime.now(UTC)
    if reference - parsed > WATCHLIST_EXPIRY_AGE:
        return WatchlistExpiryDecision(True, WATCHLIST_EXPIRY_REASON, anchor_timestamp=anchor)
    return WatchlistExpiryDecision(False, anchor_timestamp=anchor)


def is_expirable_watch_state(values: Sequence[Any]) -> bool:
    keys = {_status_key(value) for value in values if _status_key(value)}
    if keys & WATCHLIST_NON_EXPIRING_STATE_KEYS:
        return False
    return bool(keys & WATCHLIST_EXPIRABLE_STATE_KEYS)


def parse_utc_timestamp(value: Any) -> datetime | None:
    text = _display(value)
    if text == NA:
        return None
    normalized = text.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _first_display(values: Sequence[Any]) -> str:
    for value in values:
        text = _display(value)
        if text != NA:
            return text
    return NA


def _status_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    key = text.lower().strip().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _display(value: Any) -> str:
    if value is None or value == "" or value == NA:
        return NA
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool)):
        value = value.value
    if isinstance(value, bool):
        return NA
    text = " ".join(str(value).split())
    return text if text else NA


__all__ = [
    "WATCHLIST_EXPIRY_AGE",
    "WATCHLIST_EXPIRY_HOURS",
    "WATCHLIST_EXPIRY_REASON",
    "WATCHLIST_EXPIRY_TIMESTAMP_NA_REASON",
    "WATCHLIST_EXPIRY_TIMESTAMP_UNVERIFIED_REASON",
    "WatchlistExpiryDecision",
    "is_expirable_watch_state",
    "parse_utc_timestamp",
    "watchlist_expiry_decision",
]
