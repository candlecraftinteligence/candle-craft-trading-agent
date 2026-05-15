from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.dtos import NA, CandleDTO, MaybeDecimal, MaybeInt


def resample_ohlcv_candles(candles: Sequence[Any], target_interval: str = "2d") -> list[Any]:
    """Merge complete 1D OHLCV candle pairs into synthetic 2D candles.

    Odd trailing candles are ignored because they do not form a complete 2D
    candle. DTO inputs return DTO outputs; mapping inputs return dictionaries.
    """

    normalized_interval = target_interval.strip().lower()
    if normalized_interval != "2d":
        raise ValueError("resample_ohlcv_candles currently supports target_interval='2d' only")
    if isinstance(candles, (str, bytes)) or not isinstance(candles, Sequence):
        raise ValueError("candles must be a sequence of OHLCV candle objects")

    output: list[Any] = []
    for index in range(0, len(candles) - 1, 2):
        first = candles[index]
        second = candles[index + 1]
        open_price = _decimal_field(first, "open")
        high_price = max(_decimal_field(first, "high"), _decimal_field(second, "high"))
        low_price = min(_decimal_field(first, "low"), _decimal_field(second, "low"))
        close_price = _decimal_field(second, "close")
        volume = _decimal_field(first, "volume") + _decimal_field(second, "volume")
        timestamp = _int_field(first, "timestamp")

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
                    close_timestamp=_optional_int_field(second, "close_timestamp"),
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


def _optional_int_field(candle: Any, name: str) -> MaybeInt:
    value = _field(candle, name)
    if value is None or value == "" or value == NA:
        return NA
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
