"""Explicit UTC timing contract for operational origin eligibility."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.data.dtos import NA


def parse_utc(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    text = _text(value)
    if text == NA:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def strictly_after(value: Any, cutoff: Any) -> bool:
    left = parse_utc(value)
    right = parse_utc(cutoff)
    if left is None or right is None:
        return False
    return left > right


def comparable_utc(value: Any) -> str | None:
    parsed = parse_utc(value)
    if parsed is None:
        return None
    return iso_utc(parsed)


def _text(value: Any) -> str:
    if value is None:
        return NA
    text = str(value).strip()
    return text if text and text.upper() != NA else NA
