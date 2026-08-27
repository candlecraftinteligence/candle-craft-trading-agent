from __future__ import annotations

from datetime import datetime

from app.context.models import ContextStatus, ContextValue, WeekendContextPayload
from app.data.candle_integrity import normalize_utc_timestamp

WEEKEND_SOURCE = "internal_clock/calendar_utc"


def build_weekend_context(observed_at: datetime) -> ContextValue:
    observed_utc = normalize_utc_timestamp(observed_at, field_name="weekend_observed_at")
    weekday = observed_utc.weekday()
    is_weekend = weekday in (5, 6)
    return ContextValue(
        value=WeekendContextPayload(
            is_weekend=is_weekend,
            utc_weekday=weekday,
            utc_weekday_name=observed_utc.strftime("%A"),
            session_label=_session_label(observed_utc, is_weekend=is_weekend),
        ),
        source=WEEKEND_SOURCE,
        observed_at=observed_utc,
        age_seconds=0.0,
        status=ContextStatus.VERIFIED,
    )


def _session_label(observed_at: datetime, *, is_weekend: bool) -> str:
    if is_weekend:
        return "weekend"
    hour = observed_at.hour
    if 0 <= hour < 8:
        return "asia"
    if 8 <= hour < 13:
        return "europe"
    if 13 <= hour < 21:
        return "us"
    return "off_hours"


__all__ = ["WEEKEND_SOURCE", "build_weekend_context"]
