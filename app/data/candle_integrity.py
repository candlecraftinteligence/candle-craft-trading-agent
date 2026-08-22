from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum
from typing import Any

from app.data.dtos import NA

_TIMEFRAME_PATTERN = re.compile(r"^(?P<count>[1-9][0-9]*)(?P<unit>[mhdw])$")
_EPOCH_UTC = datetime(1970, 1, 1, tzinfo=UTC)
_UNIT_DURATION = {
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
    "w": timedelta(weeks=1),
}


class CandleIntegrityReason(str, Enum):
    MISSING_OPEN_TIMESTAMP = "missing_open_timestamp"
    INVALID_OPEN_TIMESTAMP = "invalid_open_timestamp"
    INVALID_CLOSE_TIMESTAMP = "invalid_close_timestamp"
    INVALID_TIMEFRAME = "invalid_timeframe"
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"
    OUT_OF_ORDER = "out_of_order"
    CONTINUITY_GAP = "continuity_gap"
    INSUFFICIENT_CLOSED_HISTORY = "insufficient_closed_history"


class CandleIntegrityError(ValueError):
    def __init__(
        self,
        reason: CandleIntegrityReason,
        message: str,
        *,
        timeframe: str,
        index: int | None = None,
        available_count: int | None = None,
        required_count: int | None = None,
    ) -> None:
        self.reason = reason
        self.timeframe = timeframe
        self.index = index
        self.available_count = available_count
        self.required_count = required_count
        location = f" index={index}" if index is not None else ""
        super().__init__(f"candle_integrity:{reason.value} timeframe={timeframe}{location}: {message}")


@dataclass(frozen=True)
class CausalCandle:
    source: Any
    open_timestamp: datetime
    close_timestamp: datetime

    @property
    def open_timestamp_ms(self) -> int:
        return _epoch_milliseconds(self.open_timestamp)

    @property
    def close_timestamp_ms(self) -> int:
        return _epoch_milliseconds(self.close_timestamp)


@dataclass(frozen=True)
class ClosedCandleWindow:
    candles: tuple[Any, ...]
    timeline: tuple[CausalCandle, ...]
    decision_timestamp: datetime
    excluded_unclosed_count: int


def timeframe_duration(timeframe: str) -> timedelta:
    normalized = str(timeframe).strip().lower()
    match = _TIMEFRAME_PATTERN.fullmatch(normalized)
    if match is None:
        raise CandleIntegrityError(
            CandleIntegrityReason.INVALID_TIMEFRAME,
            f"unsupported fixed-duration timeframe {timeframe!r}",
            timeframe=normalized or str(timeframe),
        )
    return _UNIT_DURATION[match.group("unit")] * int(match.group("count"))


def normalize_utc_timestamp(value: Any, *, field_name: str = "timestamp") -> datetime:
    """Normalize an epoch-millisecond or datetime-like timestamp to aware UTC.

    Exchange DTO integer timestamps are epoch milliseconds. Naive datetimes are
    interpreted as UTC so comparisons never mix naive and aware values.
    """

    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an epoch-millisecond or datetime value")
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"{field_name} is outside the supported UTC range") from exc
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or cleaned == NA:
            raise ValueError(f"{field_name} is missing")
        try:
            integer = int(cleaned)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(cleaned.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError(f"{field_name} is not a valid UTC timestamp") from exc
            return normalize_utc_timestamp(parsed, field_name=field_name)
        return normalize_utc_timestamp(integer, field_name=field_name)
    raise ValueError(f"{field_name} must be an epoch-millisecond or datetime value")


def validate_candle_sequence(
    candles: Sequence[Any],
    *,
    timeframe: str,
    require_continuity: bool = True,
) -> tuple[CausalCandle, ...]:
    if isinstance(candles, (str, bytes)) or not isinstance(candles, Sequence):
        raise ValueError("candles must be a sequence")

    normalized_timeframe = str(timeframe).strip().lower()
    duration = timeframe_duration(normalized_timeframe)
    timeline: list[CausalCandle] = []
    previous_open: datetime | None = None

    for index, candle in enumerate(candles):
        raw_open = _first_present_field(candle, ("timestamp", "open_timestamp", "opened_at"))
        if _is_missing(raw_open):
            raise CandleIntegrityError(
                CandleIntegrityReason.MISSING_OPEN_TIMESTAMP,
                "candle has no explicit open timestamp",
                timeframe=normalized_timeframe,
                index=index,
            )
        try:
            open_timestamp = normalize_utc_timestamp(raw_open, field_name="open_timestamp")
        except ValueError as exc:
            raise CandleIntegrityError(
                CandleIntegrityReason.INVALID_OPEN_TIMESTAMP,
                str(exc),
                timeframe=normalized_timeframe,
                index=index,
            ) from exc

        derived_close = open_timestamp + duration
        raw_close = _first_present_field(candle, ("close_timestamp", "closed_at"))
        if _is_missing(raw_close):
            close_timestamp = derived_close
        else:
            try:
                close_timestamp = normalize_utc_timestamp(raw_close, field_name="close_timestamp")
            except ValueError as exc:
                raise CandleIntegrityError(
                    CandleIntegrityReason.INVALID_CLOSE_TIMESTAMP,
                    str(exc),
                    timeframe=normalized_timeframe,
                    index=index,
                ) from exc
            earliest_exchange_close = derived_close - timedelta(milliseconds=1)
            if close_timestamp < earliest_exchange_close or close_timestamp > derived_close:
                raise CandleIntegrityError(
                    CandleIntegrityReason.INVALID_CLOSE_TIMESTAMP,
                    "explicit close timestamp does not match the deterministic timeframe boundary",
                    timeframe=normalized_timeframe,
                    index=index,
                )

        if previous_open is not None:
            if open_timestamp == previous_open:
                raise CandleIntegrityError(
                    CandleIntegrityReason.DUPLICATE_TIMESTAMP,
                    f"duplicate candle open at {open_timestamp.isoformat()}",
                    timeframe=normalized_timeframe,
                    index=index,
                )
            if open_timestamp < previous_open:
                raise CandleIntegrityError(
                    CandleIntegrityReason.OUT_OF_ORDER,
                    f"candle open {open_timestamp.isoformat()} precedes the prior open {previous_open.isoformat()}",
                    timeframe=normalized_timeframe,
                    index=index,
                )
            expected_open = previous_open + duration
            if require_continuity and open_timestamp != expected_open:
                raise CandleIntegrityError(
                    CandleIntegrityReason.CONTINUITY_GAP,
                    f"expected open {expected_open.isoformat()}, received {open_timestamp.isoformat()}",
                    timeframe=normalized_timeframe,
                    index=index,
                )

        timeline.append(
            CausalCandle(
                source=candle,
                open_timestamp=open_timestamp,
                close_timestamp=close_timestamp,
            )
        )
        previous_open = open_timestamp

    return tuple(timeline)


def closed_candles_as_of(
    candles: Sequence[Any],
    *,
    timeframe: str,
    decision_timestamp: Any,
    minimum_closed_history: int = 1,
    require_continuity: bool = True,
) -> ClosedCandleWindow:
    if minimum_closed_history < 0:
        raise ValueError("minimum_closed_history must be zero or greater")
    normalized_timeframe = str(timeframe).strip().lower()
    decision_utc = normalize_utc_timestamp(decision_timestamp, field_name="decision_timestamp")
    timeline = validate_candle_sequence(
        candles,
        timeframe=normalized_timeframe,
        require_continuity=require_continuity,
    )
    eligible = tuple(item for item in timeline if item.close_timestamp <= decision_utc)
    if len(eligible) < minimum_closed_history:
        raise CandleIntegrityError(
            CandleIntegrityReason.INSUFFICIENT_CLOSED_HISTORY,
            (
                f"received {len(timeline)} candles but only {len(eligible)} were closed by "
                f"{decision_utc.isoformat()}; required at least {minimum_closed_history}"
            ),
            timeframe=normalized_timeframe,
            available_count=len(eligible),
            required_count=minimum_closed_history,
        )
    return ClosedCandleWindow(
        candles=tuple(item.source for item in eligible),
        timeline=eligible,
        decision_timestamp=decision_utc,
        excluded_unclosed_count=len(timeline) - len(eligible),
    )


def _field(candle: Any, name: str) -> Any:
    if isinstance(candle, Mapping):
        return candle.get(name)
    return getattr(candle, name, None)


def _first_present_field(candle: Any, names: Sequence[str]) -> Any:
    for name in names:
        value = _field(candle, name)
        if not _is_missing(value):
            return value
    return None


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


def _epoch_milliseconds(value: datetime) -> int:
    delta = value - _EPOCH_UTC
    return (delta.days * 86_400_000) + (delta.seconds * 1000) + (delta.microseconds // 1000)


__all__ = [
    "CandleIntegrityError",
    "CandleIntegrityReason",
    "CausalCandle",
    "ClosedCandleWindow",
    "closed_candles_as_of",
    "normalize_utc_timestamp",
    "timeframe_duration",
    "validate_candle_sequence",
]
