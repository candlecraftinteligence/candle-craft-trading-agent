"""Explicit UTC normalization. Unknown units are not guessed from acquisition time."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

TimestampUnit = Literal["s", "ms"]


class TimestampContractError(ValueError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def normalize_explicit(value: int | str, unit: TimestampUnit) -> datetime:
    """Convert a declared epoch unit to UTC. The unit argument is mandatory."""

    if isinstance(value, bool):
        raise TimestampContractError("bool_rejected")
    try:
        integer = int(str(value).strip())
    except (TypeError, ValueError) as exc:
        raise TimestampContractError("not_an_integer") from exc
    seconds = integer / 1000 if unit == "ms" else integer
    try:
        moment = datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        raise TimestampContractError("outside_utc_range") from exc
    return moment


def classify_unit_confusion(value: int, *, interval: timedelta, sibling_delta: int | None) -> str:
    """Return a reason when a sibling spacing only makes sense in the other unit.

    This does not rewrite the value. Callers keep the original integer.
    """

    if sibling_delta is None:
        return "unit_unverified"
    expected_ms = int(interval.total_seconds() * 1000)
    if sibling_delta == expected_ms:
        return "spacing_matches_milliseconds"
    if sibling_delta == int(interval.total_seconds()):
        return "spacing_matches_seconds"
    return "spacing_matches_neither"


def originally_available(
    *,
    measurement_at: datetime | None,
    available_to_cci_at: datetime | None,
    decision_cutoff: datetime,
) -> str:
    """Historical reconstruction needs both the measurement time and CCI possession.

    Fetching a bar later can support structure checks. It does not prove the bar
    was known at an earlier decision. Acquisition time is not a producer time.
    """

    if measurement_at is None or available_to_cci_at is None:
        return "unverified"
    if measurement_at <= decision_cutoff and available_to_cci_at <= decision_cutoff:
        return "verified"
    return "incompatible"


def candle_times(*, open_at: datetime, duration: timedelta) -> dict[str, datetime]:
    """A candle open is not its close. The scheduled close is open plus duration."""

    return {
        "open_at": open_at,
        "scheduled_close_at": open_at + duration,
    }


def future_timestamp(moment: datetime, *, received_at: datetime, skew: timedelta = timedelta(seconds=5)) -> bool:
    return moment > received_at + skew


def event_age_seconds(*, measurement_at: datetime | None, received_at: datetime | None) -> Decimal | None:
    if measurement_at is None or received_at is None:
        return None
    return Decimal(str((received_at - measurement_at).total_seconds()))
