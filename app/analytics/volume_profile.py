from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.data.dtos import NA, MaybeDecimal

VOLUME_PROFILE_SOURCE = "estimated_from_candles"
OUTPUT_QUANT = Decimal("0.00000001")
DEFAULT_BUCKET_COUNT = 48
DEFAULT_VALUE_AREA_PCT = Decimal("0.70")
NODE_COUNT = 5


class VolumeProfileInput(BaseModel):
    symbol: str
    timeframe: str
    candles: Sequence[Any] = ()
    bucket_count: int = DEFAULT_BUCKET_COUNT
    value_area_pct: Decimal = DEFAULT_VALUE_AREA_PCT

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol", "timeframe")
    @classmethod
    def _text_not_blank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("symbol and timeframe must not be blank")
        return normalized

    @field_validator("candles")
    @classmethod
    def _candles_must_be_sequence(cls, value: Sequence[Any]) -> Sequence[Any]:
        if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
            raise ValueError("candles must be a sequence of candle objects")
        return value

    @field_validator("bucket_count")
    @classmethod
    def _bucket_count_in_range(cls, value: int) -> int:
        if value < 1:
            raise ValueError("bucket_count must be at least 1")
        return value

    @field_validator("value_area_pct", mode="before")
    @classmethod
    def _value_area_pct_decimal(cls, value: Any) -> Decimal:
        try:
            decimal = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"Invalid value_area_pct: {value!r}") from exc
        if decimal <= 0 or decimal > 1:
            raise ValueError("value_area_pct must be greater than 0 and less than or equal to 1")
        return decimal


class VolumeProfileLevel(BaseModel):
    bucket_index: int
    price_low: MaybeDecimal
    price_high: MaybeDecimal
    price: MaybeDecimal
    volume: MaybeDecimal
    volume_pct: MaybeDecimal

    model_config = ConfigDict(frozen=True)


class VolumeProfileResult(BaseModel):
    symbol: str
    timeframe: str
    source: Literal["estimated_from_candles"] = VOLUME_PROFILE_SOURCE
    poc: MaybeDecimal = NA
    value_area_high: MaybeDecimal = NA
    value_area_low: MaybeDecimal = NA
    high_volume_nodes: tuple[VolumeProfileLevel, ...] = ()
    low_volume_nodes: tuple[VolumeProfileLevel, ...] = ()
    nearest_high_volume_node: MaybeDecimal = NA
    nearest_low_volume_node: MaybeDecimal = NA
    price_range_high: MaybeDecimal = NA
    price_range_low: MaybeDecimal = NA
    total_volume: MaybeDecimal = NA
    candles_used: int = 0
    missing_data: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


@dataclass(frozen=True)
class _ProfileCandle:
    index: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: MaybeDecimal


def calculate_volume_profile(
    profile_input: VolumeProfileInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> VolumeProfileResult:
    data = _normalize_input(profile_input, overrides)
    candles, missing_volume_count = _normalize_candles(data.candles)

    if not candles:
        return _na_result(
            data,
            missing_data=("candles: N/A",),
            warnings=("Volume profile is N/A because no candles were supplied.",),
        )

    usable = tuple(candle for candle in candles if candle.volume != NA)
    if not usable:
        return _na_result(
            data,
            missing_data=("volume: N/A",),
            warnings=("Volume profile is N/A because candles contain no volume data.",),
        )

    warnings: list[str] = []
    if missing_volume_count:
        warnings.append(
            f"{missing_volume_count} candle(s) had volume N/A and were excluded from the estimated profile."
        )

    price_low = min(candle.low for candle in usable)
    price_high = max(candle.high for candle in usable)
    bucket_count = 1 if price_high == price_low else data.bucket_count
    buckets = _bucket_ranges(price_low, price_high, bucket_count)
    allocated = [Decimal("0") for _ in range(bucket_count)]

    for candle in usable:
        volume = _decimal_from(candle.volume, f"candles[{candle.index}].volume")
        if volume == 0:
            continue
        if candle.high == candle.low or bucket_count == 1:
            allocated[_bucket_index(candle.close, price_low, price_high, bucket_count)] += volume
            continue

        candle_range = candle.high - candle.low
        start = _bucket_index(candle.low, price_low, price_high, bucket_count)
        end = _bucket_index(candle.high, price_low, price_high, bucket_count)
        for index in range(start, end + 1):
            bucket_low, bucket_high = buckets[index]
            overlap = min(candle.high, bucket_high) - max(candle.low, bucket_low)
            if overlap > 0:
                allocated[index] += volume * (overlap / candle_range)

    total_volume = sum(allocated, Decimal("0"))
    if total_volume <= 0:
        return _na_result(
            data,
            warnings=("Volume profile is N/A because total candle volume is zero.", *warnings),
        )

    levels = tuple(_level(index, buckets[index], allocated[index], total_volume) for index in range(bucket_count))
    poc_index = sorted(range(bucket_count), key=lambda index: (-allocated[index], index))[0]
    value_area_indices = _value_area_indices(allocated, total_volume, data.value_area_pct)
    high_volume_nodes = tuple(
        levels[index]
        for index in sorted(range(bucket_count), key=lambda item: (-allocated[item], item))[: min(NODE_COUNT, bucket_count)]
        if allocated[index] > 0
    )
    low_volume_nodes = tuple(
        levels[index]
        for index in sorted(range(bucket_count), key=lambda item: (allocated[item], item))[: min(NODE_COUNT, bucket_count)]
    )
    latest_close = candles[-1].close

    return VolumeProfileResult(
        symbol=data.symbol,
        timeframe=data.timeframe,
        poc=levels[poc_index].price,
        value_area_high=_quantize(max(buckets[index][1] for index in value_area_indices)),
        value_area_low=_quantize(min(buckets[index][0] for index in value_area_indices)),
        high_volume_nodes=high_volume_nodes,
        low_volume_nodes=low_volume_nodes,
        nearest_high_volume_node=_nearest_level(high_volume_nodes, latest_close),
        nearest_low_volume_node=_nearest_level(low_volume_nodes, latest_close),
        price_range_high=_quantize(price_high),
        price_range_low=_quantize(price_low),
        total_volume=_quantize(total_volume),
        candles_used=len(usable),
        missing_data=("volume: N/A",) if missing_volume_count else (),
        warnings=tuple(warnings),
    )


def _normalize_input(
    profile_input: VolumeProfileInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> VolumeProfileInput:
    if profile_input is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(profile_input, VolumeProfileInput):
        raw = profile_input.model_dump()
        raw.update(overrides)
    else:
        raw = dict(profile_input)
        raw.update(overrides)
    return VolumeProfileInput.model_validate(raw)


def _normalize_candles(candles: Sequence[Any]) -> tuple[tuple[_ProfileCandle, ...], int]:
    normalized: list[_ProfileCandle] = []
    missing_volume_count = 0
    for index, candle in enumerate(candles):
        required: dict[str, Decimal] = {}
        for field in ("open", "high", "low", "close"):
            value = _field(candle, field)
            if _is_missing(value):
                raise ValueError(f"Missing required OHLC field candles[{index}].{field}.")
            required[field] = _decimal_from(value, f"candles[{index}].{field}")

        if required["high"] < required["low"]:
            raise ValueError(f"Malformed candle candles[{index}]: high is lower than low.")
        if required["high"] < max(required["open"], required["close"]):
            raise ValueError(f"Malformed candle candles[{index}]: high is below open or close.")
        if required["low"] > min(required["open"], required["close"]):
            raise ValueError(f"Malformed candle candles[{index}]: low is above open or close.")

        volume_value = _field(candle, "volume")
        if _is_missing(volume_value):
            volume: MaybeDecimal = NA
            missing_volume_count += 1
        else:
            volume = _decimal_from(volume_value, f"candles[{index}].volume")
            if volume < 0:
                raise ValueError(f"Malformed candle candles[{index}].volume: volume cannot be negative.")

        normalized.append(
            _ProfileCandle(
                index=index,
                open=required["open"],
                high=required["high"],
                low=required["low"],
                close=required["close"],
                volume=volume,
            )
        )
    return tuple(normalized), missing_volume_count


def _bucket_ranges(price_low: Decimal, price_high: Decimal, bucket_count: int) -> tuple[tuple[Decimal, Decimal], ...]:
    if bucket_count == 1:
        return ((price_low, price_high),)
    bucket_size = (price_high - price_low) / Decimal(bucket_count)
    buckets: list[tuple[Decimal, Decimal]] = []
    for index in range(bucket_count):
        low = price_low + bucket_size * index
        high = price_high if index == bucket_count - 1 else price_low + bucket_size * (index + 1)
        buckets.append((low, high))
    return tuple(buckets)


def _bucket_index(price: Decimal, price_low: Decimal, price_high: Decimal, bucket_count: int) -> int:
    if bucket_count == 1 or price_high == price_low:
        return 0
    if price <= price_low:
        return 0
    if price >= price_high:
        return bucket_count - 1
    bucket_size = (price_high - price_low) / Decimal(bucket_count)
    index = int(((price - price_low) / bucket_size).to_integral_value(rounding=ROUND_FLOOR))
    return min(max(index, 0), bucket_count - 1)


def _level(
    index: int,
    bucket: tuple[Decimal, Decimal],
    volume: Decimal,
    total_volume: Decimal,
) -> VolumeProfileLevel:
    low, high = bucket
    midpoint = (low + high) / Decimal("2")
    return VolumeProfileLevel(
        bucket_index=index,
        price_low=_quantize(low),
        price_high=_quantize(high),
        price=_quantize(midpoint),
        volume=_quantize(volume),
        volume_pct=_quantize(volume / total_volume * Decimal("100")),
    )


def _value_area_indices(
    allocated: Sequence[Decimal],
    total_volume: Decimal,
    value_area_pct: Decimal,
) -> tuple[int, ...]:
    target = total_volume * value_area_pct
    selected: list[int] = []
    selected_volume = Decimal("0")
    for index in sorted(range(len(allocated)), key=lambda item: (-allocated[item], item)):
        selected.append(index)
        selected_volume += allocated[index]
        if selected_volume >= target:
            break
    return tuple(selected)


def _nearest_level(levels: Sequence[VolumeProfileLevel], price: Decimal) -> MaybeDecimal:
    decimal_levels = [level for level in levels if level.price != NA]
    if not decimal_levels:
        return NA
    nearest = min(
        decimal_levels,
        key=lambda level: (abs(_decimal_from(level.price, "level.price") - price), level.bucket_index),
    )
    return nearest.price


def _na_result(
    data: VolumeProfileInput,
    *,
    missing_data: Sequence[str] = (),
    warnings: Sequence[str] = (),
) -> VolumeProfileResult:
    return VolumeProfileResult(
        symbol=data.symbol,
        timeframe=data.timeframe,
        missing_data=tuple(missing_data),
        warnings=tuple(warnings),
    )


def _field(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Malformed volume profile data at {path}: invalid decimal {value!r}.") from exc
    if not decimal.is_finite():
        raise ValueError(f"Malformed volume profile data at {path}: invalid decimal {value!r}.")
    return decimal


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


__all__ = [
    "VOLUME_PROFILE_SOURCE",
    "VolumeProfileInput",
    "VolumeProfileLevel",
    "VolumeProfileResult",
    "calculate_volume_profile",
]
