from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
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
SHADOW_RULE_VERSION = "candle-integrity-shadow-v1"
_MAX_SHADOW_SAMPLE_INDICES = 10


class CandleIntegrityReason(str, Enum):
    MISSING_OPEN_TIMESTAMP = "missing_open_timestamp"
    INVALID_OPEN_TIMESTAMP = "invalid_open_timestamp"
    INVALID_CLOSE_TIMESTAMP = "invalid_close_timestamp"
    INVALID_TIMEFRAME = "invalid_timeframe"
    DUPLICATE_TIMESTAMP = "duplicate_timestamp"
    OUT_OF_ORDER = "out_of_order"
    OVERLAPPING_INTERVAL = "overlapping_interval"
    CONTINUITY_GAP = "continuity_gap"
    INSUFFICIENT_CLOSED_HISTORY = "insufficient_closed_history"
    INVALID_CANDLE = "invalid_candle"
    MISSING_PRICE = "missing_price"
    INVALID_PRICE = "invalid_price"
    INVALID_OHLC_GEOMETRY = "invalid_ohlc_geometry"
    MISSING_VOLUME = "missing_volume"
    INVALID_VOLUME = "invalid_volume"


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
    index: int | None = None

    @property
    def derived_close_timestamp(self) -> datetime:
        return self.close_timestamp

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


@dataclass(frozen=True)
class ValidatedCandleValues:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None
    quote_volume: Decimal | None = None
    turnover: Decimal | None = None
    trade_count: int | None = None


@dataclass(frozen=True)
class CandleShadowDiagnostic:
    rule_version: str
    consumer: str
    source: str
    timeframe: str
    observation_kind: str
    sample_count: int
    old_decision: str
    new_decision: str
    would_flag_under_strict_shadow: bool
    disagreement_count: int
    reason_counts: dict[str, int]
    sampled_indices: dict[str, tuple[int, ...]]
    promotion_evidence_required: tuple[str, ...]
    total_flag_count: int = 0


_MAX_RECORDED_SHADOW_DIAGNOSTICS = 256
_RECORDED_SHADOW_DIAGNOSTICS: list[CandleShadowDiagnostic] = []


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
    if isinstance(value, int):
        try:
            return datetime.fromtimestamp(value / 1000, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise ValueError(f"{field_name} is outside the supported UTC range") from exc
    if isinstance(value, float):
        if not math.isfinite(value) or not value.is_integer():
            raise ValueError(f"{field_name} must be an integral epoch-millisecond value")
        return normalize_utc_timestamp(int(value), field_name=field_name)
    if isinstance(value, Decimal):
        if not value.is_finite() or value != int(value):
            raise ValueError(f"{field_name} must be an integral epoch-millisecond value")
        return normalize_utc_timestamp(int(value), field_name=field_name)
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


def validate_candle_values(
    candle: Any,
    *,
    index: int | None = None,
    timeframe: str | None = None,
    require_volume: bool = True,
) -> ValidatedCandleValues:
    """Validate candle OHLC price positivity, cross-field geometry, and volume.

    ``require_volume=True`` is the provider/OHLCV default. Price-only consumers
    (replay prefixes, owner latest-close telemetry, freshness, sector features)
    must pass ``require_volume=False``. Missing volume stays ``None`` and is never
    fabricated as zero. Every supplied ``quote_volume`` and ``turnover`` value is
    validated independently; the fields are not interchangeable.
    """

    normalized_timeframe = str(timeframe or _field(candle, "interval") or "unknown").strip().lower() or "unknown"
    if candle is None or isinstance(candle, (bool, str, bytes, bytearray, int, float, Decimal, list, tuple, set, frozenset)):
        raise CandleIntegrityError(
            CandleIntegrityReason.INVALID_CANDLE,
            "candle must be a mapping or candle object",
            timeframe=normalized_timeframe,
            index=index,
        )
    if not isinstance(candle, Mapping) and not any(
        hasattr(candle, name) for name in ("open", "high", "low", "close")
    ):
        raise CandleIntegrityError(
            CandleIntegrityReason.INVALID_CANDLE,
            "candle does not expose OHLC fields",
            timeframe=normalized_timeframe,
            index=index,
        )

    parsed_prices: dict[str, Decimal] = {}
    for price_name in ("open", "high", "low", "close"):
        if not _has_field(candle, price_name):
            raise CandleIntegrityError(
                CandleIntegrityReason.MISSING_PRICE,
                f"candle is missing required price field '{price_name}'",
                timeframe=normalized_timeframe,
                index=index,
            )
        raw_price = _field(candle, price_name)
        if _is_missing(raw_price) or (isinstance(raw_price, str) and not raw_price.strip()):
            raise CandleIntegrityError(
                CandleIntegrityReason.MISSING_PRICE,
                f"candle is missing required price field '{price_name}'",
                timeframe=normalized_timeframe,
                index=index,
            )
        price_value = _parse_finite_decimal(raw_price)
        if price_value is None or price_value <= 0:
            raise CandleIntegrityError(
                CandleIntegrityReason.INVALID_PRICE,
                f"candle price field '{price_name}' must be a finite positive number",
                timeframe=normalized_timeframe,
                index=index,
            )
        parsed_prices[price_name] = price_value

    open_price = parsed_prices["open"]
    high_price = parsed_prices["high"]
    low_price = parsed_prices["low"]
    close_price = parsed_prices["close"]
    if (
        low_price > high_price
        or not (low_price <= open_price <= high_price)
        or not (low_price <= close_price <= high_price)
    ):
        raise CandleIntegrityError(
            CandleIntegrityReason.INVALID_OHLC_GEOMETRY,
            (
                "candle OHLC geometry violates low <= open, close <= high "
                f"(open={open_price}, high={high_price}, low={low_price}, close={close_price})"
            ),
            timeframe=normalized_timeframe,
            index=index,
        )

    has_volume = _has_field(candle, "volume")
    raw_volume = _field(candle, "volume") if has_volume else None
    if not has_volume or raw_volume is None or (isinstance(raw_volume, str) and raw_volume.strip() == NA):
        if require_volume:
            raise CandleIntegrityError(
                CandleIntegrityReason.MISSING_VOLUME,
                "candle is missing required volume field 'volume'",
                timeframe=normalized_timeframe,
                index=index,
            )
        volume_value: Decimal | None = None
    else:
        if isinstance(raw_volume, str) and not raw_volume.strip():
            raise CandleIntegrityError(
                CandleIntegrityReason.INVALID_VOLUME,
                "candle field 'volume' is blank",
                timeframe=normalized_timeframe,
                index=index,
            )
        volume_value = _parse_finite_decimal(raw_volume)
        if volume_value is None or volume_value < 0:
            raise CandleIntegrityError(
                CandleIntegrityReason.INVALID_VOLUME,
                "candle field 'volume' must be a finite non-negative number",
                timeframe=normalized_timeframe,
                index=index,
            )

    parsed_optional_volumes: dict[str, Decimal] = {}
    for quote_field in ("quote_volume", "turnover"):
        if not _has_field(candle, quote_field):
            continue
        raw_quote = _field(candle, quote_field)
        if raw_quote is None or (isinstance(raw_quote, str) and raw_quote.strip() == NA):
            continue
        if isinstance(raw_quote, str) and not raw_quote.strip():
            raise CandleIntegrityError(
                CandleIntegrityReason.INVALID_VOLUME,
                f"candle optional field '{quote_field}' is blank",
                timeframe=normalized_timeframe,
                index=index,
            )
        parsed_quote = _parse_finite_decimal(raw_quote)
        if parsed_quote is None or parsed_quote < 0:
            raise CandleIntegrityError(
                CandleIntegrityReason.INVALID_VOLUME,
                f"candle optional field '{quote_field}' must be a finite non-negative number",
                timeframe=normalized_timeframe,
                index=index,
            )
        parsed_optional_volumes[quote_field] = parsed_quote
    quote_volume_value = parsed_optional_volumes.get("quote_volume")
    turnover_value = parsed_optional_volumes.get("turnover")

    trade_count_value: int | None = None
    if _has_field(candle, "trade_count"):
        raw_trades = _field(candle, "trade_count")
        if raw_trades is not None and not (isinstance(raw_trades, str) and raw_trades.strip() == NA):
            parsed_trades = _parse_non_negative_int(raw_trades)
            if parsed_trades is None:
                raise CandleIntegrityError(
                    CandleIntegrityReason.INVALID_VOLUME,
                    "candle optional field 'trade_count' must be a non-negative integer",
                    timeframe=normalized_timeframe,
                    index=index,
                )
            trade_count_value = parsed_trades

    return ValidatedCandleValues(
        open=open_price,
        high=high_price,
        low=low_price,
        close=close_price,
        volume=volume_value,
        quote_volume=quote_volume_value,
        turnover=turnover_value,
        trade_count=trade_count_value,
    )


def validate_candle_sequence(
    candles: Sequence[Any],
    *,
    timeframe: str,
    require_continuity: bool = True,
    require_volume: bool = True,
    validate_values: bool = True,
) -> tuple[CausalCandle, ...]:
    if isinstance(candles, (str, bytes)) or not isinstance(candles, Sequence):
        raise ValueError("candles must be a sequence")

    normalized_timeframe = str(timeframe).strip().lower()
    duration = timeframe_duration(normalized_timeframe)
    timeline: list[CausalCandle] = []
    previous_open: datetime | None = None

    for index, candle in enumerate(candles):
        if candle is None or isinstance(candle, (bool, str, bytes, bytearray, int, float, Decimal, list, tuple, set, frozenset)):
            raise CandleIntegrityError(
                CandleIntegrityReason.INVALID_CANDLE,
                "candle must be a mapping or candle object",
                timeframe=normalized_timeframe,
                index=index,
            )
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

        try:
            derived_close = open_timestamp + duration
        except OverflowError as exc:
            raise CandleIntegrityError(
                CandleIntegrityReason.INVALID_OPEN_TIMESTAMP,
                "candle close boundary overflows supported UTC range",
                timeframe=normalized_timeframe,
                index=index,
            ) from exc

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
            if open_timestamp < expected_open:
                raise CandleIntegrityError(
                    CandleIntegrityReason.OVERLAPPING_INTERVAL,
                    (
                        f"candle open {open_timestamp.isoformat()} overlaps prior interval closing at "
                        f"{expected_open.isoformat()}"
                    ),
                    timeframe=normalized_timeframe,
                    index=index,
                )
            if require_continuity and open_timestamp != expected_open:
                raise CandleIntegrityError(
                    CandleIntegrityReason.CONTINUITY_GAP,
                    f"expected open {expected_open.isoformat()}, received {open_timestamp.isoformat()}",
                    timeframe=normalized_timeframe,
                    index=index,
                )

        if validate_values:
            validate_candle_values(
                candle,
                index=index,
                timeframe=normalized_timeframe,
                require_volume=require_volume,
            )

        timeline.append(
            CausalCandle(
                source=candle,
                open_timestamp=open_timestamp,
                close_timestamp=close_timestamp,
                index=index,
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
    require_volume: bool = True,
    validate_values: bool = True,
) -> ClosedCandleWindow:
    if minimum_closed_history < 0:
        raise ValueError("minimum_closed_history must be zero or greater")
    normalized_timeframe = str(timeframe).strip().lower()
    decision_utc = normalize_utc_timestamp(decision_timestamp, field_name="decision_timestamp")
    timeline = validate_candle_sequence(
        candles,
        timeframe=normalized_timeframe,
        require_continuity=require_continuity,
        require_volume=require_volume,
        validate_values=False,
    )
    eligible = tuple(item for item in timeline if item.close_timestamp <= decision_utc)
    if validate_values:
        for item in eligible:
            validate_candle_values(
                item.source,
                index=item.index,
                timeframe=normalized_timeframe,
                require_volume=require_volume,
            )
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


def evaluate_candle_shadow_diagnostics(
    candles: Sequence[Any],
    *,
    timeframe: str,
    consumer: str,
    source: str = "unknown",
    synthetic_fixture: bool = False,
    record: bool = False,
) -> CandleShadowDiagnostic:
    """Observational shadow diagnostics for candidate rules lacking hard-gate proof.

    Leaves eligibility and lifecycle behavior unchanged while recording bounded,
    deterministic per-candle decision disagreement counts and rule flag counts.
    """

    normalized_timeframe = str(timeframe).strip().lower() or "unknown"
    try:
        duration = timeframe_duration(normalized_timeframe)
        duration_ms = int(duration.total_seconds() * 1000)
    except CandleIntegrityError:
        duration_ms = 0

    reason_counts: dict[str, int] = {}
    sampled_indices: dict[str, list[int]] = {}
    disagreeing_indices: set[int] = set()
    enforced_rejected_count = 0

    def _record(reason: str, idx: int) -> None:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
        disagreeing_indices.add(idx)
        bucket = sampled_indices.setdefault(reason, [])
        if len(bucket) < _MAX_SHADOW_SAMPLE_INDICES:
            bucket.append(idx)

    for index, candle in enumerate(candles):
        try:
            values = validate_candle_values(
                candle,
                index=index,
                timeframe=normalized_timeframe,
                require_volume=False,
            )
        except CandleIntegrityError:
            enforced_rejected_count += 1
            continue

        if values.volume is not None and values.volume == 0:
            if values.open == values.high == values.low == values.close:
                _record("flat_zero_volume_bar", index)
            else:
                _record("positive_range_zero_volume_bar", index)

        for opt_quote in (values.quote_volume, values.turnover):
            if values.volume is not None and opt_quote is not None:
                if values.volume == 0 and opt_quote > 0:
                    _record("quote_volume_positive_on_zero_base_volume", index)
                    break
                if values.volume > 0 and opt_quote == 0:
                    _record("quote_volume_zero_on_positive_base_volume", index)
                    break

        if values.low > 0 and (values.high - values.low) / values.low > Decimal("10"):
            _record("extreme_range_to_low_ratio", index)

        if duration_ms > 0:
            raw_open = _first_present_field(candle, ("timestamp", "open_timestamp", "opened_at"))
            if not _is_missing(raw_open):
                try:
                    open_dt = normalize_utc_timestamp(raw_open, field_name="open_timestamp")
                    open_ms = _epoch_milliseconds(open_dt)
                    if open_ms % duration_ms != 0:
                        _record("unanchored_open_timestamp", index)
                    raw_close = _first_present_field(candle, ("close_timestamp", "closed_at"))
                    if not _is_missing(raw_close):
                        close_dt = normalize_utc_timestamp(raw_close, field_name="close_timestamp")
                        if close_dt > open_dt + timedelta(milliseconds=duration_ms):
                            _record("close_timestamp_exceeds_scheduled_close", index)
                except (ValueError, OverflowError):
                    pass

    disagreement_count = len(disagreeing_indices)
    total_flag_count = sum(reason_counts.values())
    old_decision = "reject" if enforced_rejected_count > 0 else "pass"
    new_decision = (
        "reject"
        if enforced_rejected_count > 0
        else ("shadow_flag" if disagreement_count > 0 else "pass")
    )
    diagnostic = CandleShadowDiagnostic(
        rule_version=SHADOW_RULE_VERSION,
        consumer=consumer,
        source=source,
        timeframe=normalized_timeframe,
        observation_kind="synthetic_fixture" if synthetic_fixture else "exchange_observation",
        sample_count=len(candles),
        old_decision=old_decision,
        new_decision=new_decision,
        would_flag_under_strict_shadow=disagreement_count > 0,
        disagreement_count=disagreement_count,
        reason_counts=dict(sorted(reason_counts.items())),
        sampled_indices={key: tuple(indices) for key, indices in sorted(sampled_indices.items())},
        promotion_evidence_required=(
            "Cross-venue Binance USD-M and Bybit Linear empirical frequency audit over >=30 days",
            "Zero false-rejection proof on legitimate low-liquidity closed intervals",
            "Explicit Astra review approval before promoting any shadow rule to a hard gate",
        ),
        total_flag_count=total_flag_count,
    )
    if record:
        if len(_RECORDED_SHADOW_DIAGNOSTICS) >= _MAX_RECORDED_SHADOW_DIAGNOSTICS:
            _RECORDED_SHADOW_DIAGNOSTICS.pop(0)
        _RECORDED_SHADOW_DIAGNOSTICS.append(diagnostic)
    return diagnostic


def get_recorded_candle_shadow_diagnostics() -> tuple[CandleShadowDiagnostic, ...]:
    return tuple(_RECORDED_SHADOW_DIAGNOSTICS)


def clear_recorded_candle_shadow_diagnostics() -> None:
    _RECORDED_SHADOW_DIAGNOSTICS.clear()


def normalize_optional_timestamp_ms(value: Any, *, field_name: str = "timestamp") -> int | str:
    """Validate an optional candle timestamp without truncation, or return NA when absent."""

    if _is_missing(value):
        return NA
    dt = normalize_utc_timestamp(value, field_name=field_name)
    return _epoch_milliseconds(dt)


def validate_optional_candle_metadata(candle: Any, *, path: str) -> None:
    """Validate optional quote_volume, turnover, trade_count, and close_timestamp when supplied."""

    for quote_field in ("quote_volume", "turnover"):
        if not _has_field(candle, quote_field):
            continue
        raw_quote = _field(candle, quote_field)
        if raw_quote is None or (isinstance(raw_quote, str) and raw_quote.strip() == NA):
            continue
        if isinstance(raw_quote, str) and not raw_quote.strip():
            raise ValueError(f"Malformed candle {path}.{quote_field}: value is blank.")
        parsed_quote = _parse_finite_decimal(raw_quote)
        if parsed_quote is None or parsed_quote < 0:
            raise ValueError(
                f"Malformed candle {path}.{quote_field}: must be a finite non-negative decimal."
            )

    if _has_field(candle, "trade_count"):
        raw_trades = _field(candle, "trade_count")
        if raw_trades is not None and not (isinstance(raw_trades, str) and raw_trades.strip() == NA):
            parsed_trades = _parse_non_negative_int(raw_trades)
            if parsed_trades is None:
                raise ValueError(
                    f"Malformed candle {path}.trade_count: must be a non-negative integer."
                )

    for close_field in ("close_timestamp", "closed_at"):
        if not _has_field(candle, close_field):
            continue
        raw_close = _field(candle, close_field)
        if _is_missing(raw_close):
            continue
        normalize_utc_timestamp(raw_close, field_name=f"{path}.{close_field}")


def _has_field(candle: Any, name: str) -> bool:
    if isinstance(candle, Mapping):
        return name in candle
    return hasattr(candle, name)


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


def _parse_finite_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, Decimal):
        return value if value.is_finite() else None
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            return None
        return Decimal(str(value))
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or cleaned.upper() == NA:
            return None
        try:
            parsed = Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None
        return parsed if parsed.is_finite() else None
    return None


def _parse_non_negative_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned or not cleaned.isdigit():
            return None
        return int(cleaned)
    return None


def _epoch_milliseconds(value: datetime) -> int:
    delta = value - _EPOCH_UTC
    return (delta.days * 86_400_000) + (delta.seconds * 1000) + (delta.microseconds // 1000)


__all__ = [
    "CandleIntegrityError",
    "CandleIntegrityReason",
    "CandleShadowDiagnostic",
    "CausalCandle",
    "ClosedCandleWindow",
    "SHADOW_RULE_VERSION",
    "ValidatedCandleValues",
    "clear_recorded_candle_shadow_diagnostics",
    "closed_candles_as_of",
    "evaluate_candle_shadow_diagnostics",
    "get_recorded_candle_shadow_diagnostics",
    "normalize_optional_timestamp_ms",
    "normalize_utc_timestamp",
    "timeframe_duration",
    "validate_candle_sequence",
    "validate_candle_values",
    "validate_optional_candle_metadata",
]
