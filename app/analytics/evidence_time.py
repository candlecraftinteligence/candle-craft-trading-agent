"""UTC timestamp helpers for evidence contracts and read-only audits."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final

SQLITE_UTC_NAIVE_FORMAT: Final[str] = "%Y-%m-%d %H:%M:%S"


class EvidenceTimestampError(ValueError):
    """Raised when a timestamp cannot be used as an evidence bound."""


def parse_aware_utc_timestamp(value: str) -> datetime:
    """Parse an explicit timezone-aware timestamp and return UTC.

    Naive ISO-8601 values are rejected. ``Z`` and numeric offsets are accepted
    and converted to UTC. This helper never substitutes the current time.
    """

    text = str(value).strip()
    if not text:
        raise EvidenceTimestampError("timestamp is empty")
    normalized = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise EvidenceTimestampError("timestamp is not valid ISO-8601") from exc
    if parsed.tzinfo is None:
        raise EvidenceTimestampError(
            "naive timestamp rejected: timezone meaning is not established"
        )
    return parsed.astimezone(UTC)


def try_parse_stored_timestamp(
    value: object,
    *,
    sqlite_utc_naive_allowed: bool = False,
) -> tuple[datetime | None, str]:
    """Parse a stored timestamp without inventing a timezone.

    Returns ``(datetime, status)`` where status is ``aware_utc``,
    ``sqlite_utc_naive``, ``ambiguous_naive``, ``missing``, or ``invalid``.
    """

    if value is None:
        return None, "missing"
    text = str(value).strip()
    if not text or text.upper() == "N/A":
        return None, "missing"
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        if sqlite_utc_naive_allowed:
            try:
                parsed = datetime.strptime(text, SQLITE_UTC_NAIVE_FORMAT).replace(tzinfo=UTC)
            except ValueError:
                return None, "invalid"
            return parsed, "sqlite_utc_naive"
        return None, "invalid"
    if parsed.tzinfo is None:
        if sqlite_utc_naive_allowed:
            try:
                datetime.strptime(text, SQLITE_UTC_NAIVE_FORMAT)
            except ValueError:
                return None, "ambiguous_naive"
            return parsed.replace(tzinfo=UTC), "sqlite_utc_naive"
        return None, "ambiguous_naive"
    return parsed.astimezone(UTC), "aware_utc"


def utc_iso(value: datetime) -> str:
    """Return a stable UTC ISO-8601 string."""

    if value.tzinfo is None:
        raise EvidenceTimestampError("cannot serialize a naive datetime as UTC")
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def window_contains(value: datetime, *, start: datetime, cutoff: datetime) -> bool:
    """Return whether ``value`` is in the half-open window ``[start, cutoff)``."""

    return start <= value < cutoff


def validate_window(start: datetime, cutoff: datetime) -> None:
    if start.tzinfo is None or cutoff.tzinfo is None:
        raise EvidenceTimestampError("window bounds must be timezone-aware")
    if cutoff <= start:
        raise EvidenceTimestampError("cutoff must be later than start")


__all__ = [
    "EvidenceTimestampError",
    "SQLITE_UTC_NAIVE_FORMAT",
    "parse_aware_utc_timestamp",
    "try_parse_stored_timestamp",
    "utc_iso",
    "validate_window",
    "window_contains",
]
