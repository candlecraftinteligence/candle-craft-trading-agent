from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.candle_integrity import CandleIntegrityError, CandleIntegrityReason, closed_candles_as_of
from app.data.dtos import NA, CandleDTO, MaybeDecimal, MaybeInt

_DAY_MS = 86_400_000
_TWO_DAY_MS = _DAY_MS * 2


def resample_ohlcv_candles(
    candles: Sequence[Any],
    target_interval: str = "2d",
    *,
    decision_timestamp: Any,
) -> list[Any]:
    """Merge complete closed 1D candles into UTC-anchored 2D candles.

    Buckets are anchored to the Unix epoch in UTC. This makes a given daily
    candle belong to the same 2D interval regardless of fetch-window position.
    DTO inputs return DTO outputs; mapping inputs return dictionaries.
    """

    normalized_interval = target_interval.strip().lower()
    if normalized_interval != "2d":
        raise ValueError("resample_ohlcv_candles currently supports target_interval='2d' only")
    if isinstance(candles, (str, bytes)) or not isinstance(candles, Sequence):
        raise ValueError("candles must be a sequence of OHLCV candle objects")

    closed_window = closed_candles_as_of(
        candles,
        timeframe="1d",
        decision_timestamp=decision_timestamp,
        minimum_closed_history=0,
    )
    buckets: dict[int, list[Any]] = {}
    close_timestamps: dict[int, int] = {}
    for causal in closed_window.timeline:
        open_ms = causal.open_timestamp_ms
        if open_ms % _DAY_MS != 0:
            raise CandleIntegrityError(
                CandleIntegrityReason.INVALID_OPEN_TIMESTAMP,
                "synthetic 2d source must open on a 00:00 UTC daily boundary",
                timeframe="1d",
            )
        bucket = open_ms // _TWO_DAY_MS
        buckets.setdefault(bucket, []).append(causal.source)
        close_timestamps[bucket] = causal.close_timestamp_ms

    output: list[Any] = []
    for bucket, source_pair in buckets.items():
        bucket_start = bucket * _TWO_DAY_MS
        if len(source_pair) != 2:
            continue
        first, second = source_pair
        if _int_field(first, "timestamp") != bucket_start:
            continue
        if _int_field(second, "timestamp") != bucket_start + _DAY_MS:
            continue
        open_price = _decimal_field(first, "open")
        high_price = max(_decimal_field(first, "high"), _decimal_field(second, "high"))
        low_price = min(_decimal_field(first, "low"), _decimal_field(second, "low"))
        close_price = _decimal_field(second, "close")
        volume = _decimal_field(first, "volume") + _decimal_field(second, "volume")
        timestamp = bucket_start
        close_timestamp = close_timestamps[bucket]

        if isinstance(first, CandleDTO) and isinstance(second, CandleDTO):
            output.append(
                CandleDTO(
                    exchange=first.exchange,
                    symbol=first.symbol,
                    timestamp=timestamp,
                    interval=normalized_interval,
                    open=open_price,
                    high=high_price,
                    low=low_price,
                    close=close_price,
                    volume=volume,
                    close_timestamp=close_timestamp,
                    quote_volume=_sum_optional_decimals(first, second, "quote_volume"),
                    trade_count=_sum_optional_ints(first, second, "trade_count"),
                    raw_source={
                        "source_interval": first.interval,
                        "target_interval": normalized_interval,
                        "source_candles": (first.raw_source, second.raw_source),
                    },
                )
            )
            continue

        resampled = {
            "timestamp": timestamp,
            "close_timestamp": close_timestamp,
            "interval": normalized_interval,
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "volume": volume,
        }
        for optional_field in ("exchange", "symbol"):
            optional_value = _field(first, optional_field)
            if optional_value not in (None, "", NA):
                resampled[optional_field] = optional_value
        output.append(resampled)

    return output


def _field(candle: Any, name: str) -> Any:
    if isinstance(candle, Mapping):
        return candle.get(name)
    return getattr(candle, name, None)


def _decimal_field(candle: Any, name: str) -> Decimal:
    value = _field(candle, name)
    if value is None or value == "" or value == NA:
        raise ValueError(f"Missing required OHLCV field {name}.")
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid decimal value for {name}: {value!r}") from exc
    if not decimal.is_finite():
        raise ValueError(f"Invalid decimal value for {name}: {value!r}")
    return decimal


def _int_field(candle: Any, name: str) -> int:
    value = _field(candle, name)
    if value is None or value == "" or value == NA:
        raise ValueError(f"Missing required integer field {name}.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid integer value for {name}: {value!r}") from exc


def _sum_optional_decimals(first: Any, second: Any, name: str) -> MaybeDecimal:
    first_value = _field(first, name)
    second_value = _field(second, name)
    if first_value in (None, "", NA) or second_value in (None, "", NA):
        return NA
    return _decimal_field(first, name) + _decimal_field(second, name)


def _sum_optional_ints(first: Any, second: Any, name: str) -> MaybeInt:
    first_value = _field(first, name)
    second_value = _field(second, name)
    if first_value in (None, "", NA) or second_value in (None, "", NA):
        return NA
    return _int_field(first, name) + _int_field(second, name)


__all__ = ["resample_ohlcv_candles"]
