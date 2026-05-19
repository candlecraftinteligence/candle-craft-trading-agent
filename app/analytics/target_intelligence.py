from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

from app.data.dtos import NA, MaybeDecimal, MaybeInt

OUTPUT_QUANT = Decimal("0.00000001")
DEFAULT_REQUIRED_RR = Decimal("2.5")
DEFAULT_SWING_LOOKBACK = 2
DEFAULT_RANGE_WINDOW = 48
DEFAULT_EQUAL_LEVEL_TOLERANCE_PCT = Decimal("0.001")
FIB_EXTENSIONS = (Decimal("1.272"), Decimal("1.618"), Decimal("2.0"))

STRUCTURE_BLOCK_SOURCES = {
    "opposing_swing_high",
    "opposing_swing_low",
    "equal_highs",
    "equal_lows",
    "range_high",
    "range_low",
    "prior_bos_origin",
    "provided_opposing_liquidity",
}
HTF_BLOCK_SOURCES = {"htf_supply_proxy", "htf_demand_proxy"}


class TargetQualityGrade(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    REJECT = "Reject"


class TargetFailureType(str, Enum):
    NO_CLEAR_TARGET = "NO_CLEAR_TARGET"
    TP_TOO_CLOSE = "TP_TOO_CLOSE"
    OPPOSING_STRUCTURE_BLOCK = "OPPOSING_STRUCTURE_BLOCK"
    RR_BELOW_MINIMUM = "RR_BELOW_MINIMUM"
    TARGET_INSIDE_CHOP = "TARGET_INSIDE_CHOP"
    HTF_RESISTANCE_TOO_CLOSE = "HTF_RESISTANCE_TOO_CLOSE"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"


class LiquidityTarget(BaseModel):
    price: MaybeDecimal = NA
    target_type: str = NA
    source: str = NA
    confidence_score: MaybeInt = NA
    distance: MaybeDecimal = NA
    rr: MaybeDecimal = NA
    is_blocking: bool = False
    notes: str = NA

    model_config = ConfigDict(frozen=True)


class RRProjection(BaseModel):
    target_label: Literal["TP1", "TP2", "TP3"]
    target_price: MaybeDecimal = NA
    target_source: str = NA
    distance: MaybeDecimal = NA
    rr: MaybeDecimal = NA
    meets_minimum: bool | Literal["N/A"] = NA

    model_config = ConfigDict(frozen=True)


class TargetIntelligenceInput(BaseModel):
    symbol: str = NA
    mode: str = NA
    direction: str = NA
    entry: MaybeDecimal = NA
    stop: MaybeDecimal = NA
    current_price: MaybeDecimal = NA
    minimum_rr: Decimal = DEFAULT_REQUIRED_RR
    candles: Sequence[Any] = ()
    htf_candles: Sequence[Any] = ()
    recent_range_high: MaybeDecimal = NA
    recent_range_low: MaybeDecimal = NA
    nearest_support: MaybeDecimal = NA
    nearest_resistance: MaybeDecimal = NA
    bos_origin_price: MaybeDecimal = NA
    impulse_start: MaybeDecimal = NA
    impulse_end: MaybeDecimal = NA
    poc: MaybeDecimal = NA
    value_area_high: MaybeDecimal = NA
    value_area_low: MaybeDecimal = NA
    nearest_high_volume_node: MaybeDecimal = NA
    nearest_low_volume_node: MaybeDecimal = NA
    liquidity_below: Sequence[Any] | Any | None = None
    liquidity_above: Sequence[Any] | Any | None = None
    user_support_levels: Sequence[Any] | Any | None = None
    user_resistance_levels: Sequence[Any] | Any | None = None
    swing_lookback: int = DEFAULT_SWING_LOOKBACK
    range_window: int = DEFAULT_RANGE_WINDOW
    equal_level_tolerance_pct: Decimal = DEFAULT_EQUAL_LEVEL_TOLERANCE_PCT
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    @field_validator("symbol", "mode", "direction", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        text = _display(value)
        return text if text else NA

    @field_validator(
        "entry",
        "stop",
        "current_price",
        "recent_range_high",
        "recent_range_low",
        "nearest_support",
        "nearest_resistance",
        "bos_origin_price",
        "impulse_start",
        "impulse_end",
        "poc",
        "value_area_high",
        "value_area_low",
        "nearest_high_volume_node",
        "nearest_low_volume_node",
        mode="before",
    )
    @classmethod
    def _normalize_optional_decimal(cls, value: Any) -> Any:
        if _is_missing(value):
            return NA
        return _quantize(_decimal_from(value, "target_intelligence"))

    @field_validator("minimum_rr", "equal_level_tolerance_pct", mode="before")
    @classmethod
    def _normalize_decimal(cls, value: Any, info: ValidationInfo) -> Decimal:
        if _is_missing(value):
            if info.field_name == "equal_level_tolerance_pct":
                return DEFAULT_EQUAL_LEVEL_TOLERANCE_PCT
            return DEFAULT_REQUIRED_RR
        return _quantize(_decimal_from(value, "target_intelligence"))

    @field_validator("swing_lookback", "range_window")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        return max(1, int(value))

    @field_validator("missing_data", "unverified_data", mode="before")
    @classmethod
    def _normalize_tuple(cls, value: Any) -> tuple[str, ...]:
        return _sequence_values(value)


class TargetIntelligenceResult(BaseModel):
    is_diagnostic_only: bool = True
    tp1_candidate: MaybeDecimal = NA
    tp2_candidate: MaybeDecimal = NA
    tp3_candidate: MaybeDecimal = NA
    nearest_opposing_liquidity: MaybeDecimal = NA
    target_distance: MaybeDecimal = NA
    clean_path_distance: MaybeDecimal = NA
    rr_to_tp1: MaybeDecimal = NA
    rr_to_tp2: MaybeDecimal = NA
    rr_to_tp3: MaybeDecimal = NA
    target_quality_grade: TargetQualityGrade = TargetQualityGrade.REJECT
    target_failure_type: TargetFailureType | Literal["N/A"] = NA
    rr_compression_reason: str = NA
    target_confidence: MaybeInt = NA
    next_target_condition: str = NA
    liquidity_targets: tuple[LiquidityTarget, ...] = ()
    rr_projections: tuple[RRProjection, ...] = ()
    warnings: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


@dataclass(frozen=True)
class _Candle:
    index: int
    timestamp: MaybeInt
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: MaybeDecimal


@dataclass(frozen=True)
class _SwingPoint:
    kind: Literal["high", "low"]
    index: int
    confirmed_at_index: int
    price: Decimal


def build_target_intelligence(
    input_data: TargetIntelligenceInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> TargetIntelligenceResult:
    payload: dict[str, Any] = {}
    if input_data is not None:
        if isinstance(input_data, TargetIntelligenceInput):
            payload.update(input_data.model_dump())
        else:
            payload.update(dict(input_data))
    payload.update(overrides)
    data = TargetIntelligenceInput.model_validate(payload)

    direction = _normalize_direction(data.direction)
    if direction == NA or data.entry == NA or data.stop == NA:
        return _result(
            data=data,
            failure_type=TargetFailureType.DATA_INCOMPLETE,
            rr_compression_reason="Entry, stop, or direction is N/A; target RR cannot be evaluated.",
            warnings=_warnings(data, "Required entry/stop/direction data is incomplete."),
        )

    entry = _decimal_from(data.entry, "entry")
    stop = _decimal_from(data.stop, "stop")
    risk = abs(entry - stop)
    if risk <= 0 or (direction == "long" and stop >= entry) or (direction == "short" and stop <= entry):
        return _result(
            data=data,
            failure_type=TargetFailureType.DATA_INCOMPLETE,
            rr_compression_reason="Entry and stop do not define positive directional risk.",
            warnings=_warnings(data, "Directional risk is invalid or N/A."),
        )

    candles, candle_warnings = _normalize_candles(data.candles)
    htf_candles, htf_warnings = _normalize_candles(data.htf_candles)
    targets = _target_zones(data, direction, entry, stop, candles, htf_candles)
    targets = _dedupe_targets(targets, _level_tolerance(data, entry))
    targets = _ordered_targets(targets, direction)

    if not targets:
        return _result(
            data=data,
            failure_type=TargetFailureType.NO_CLEAR_TARGET,
            rr_compression_reason="No opposing liquidity, HTF proxy, volume-profile level, or fib extension is available.",
            target_confidence=0,
            warnings=_warnings(data, *candle_warnings, *htf_warnings),
        )

    projections = _rr_projections(targets, data.minimum_rr)
    tp1 = targets[0] if len(targets) > 0 else None
    tp2 = targets[1] if len(targets) > 1 else None
    tp3 = targets[2] if len(targets) > 2 else None
    rr_to_tp2 = _target_rr(tp2)
    target_distance = _target_distance(tp2 or tp1)
    clean_path_distance = _target_distance(tp1)
    confidence = _target_confidence(targets, data)
    failure_type = _failure_type(
        data=data,
        direction=direction,
        entry=entry,
        risk=risk,
        targets=targets,
        tp2=tp2,
        rr_to_tp2=rr_to_tp2,
    )
    grade = _quality_grade(
        failure_type=failure_type,
        rr_to_tp2=rr_to_tp2,
        minimum_rr=data.minimum_rr,
        confidence=confidence,
    )
    return TargetIntelligenceResult(
        tp1_candidate=tp1.price if tp1 is not None else NA,
        tp2_candidate=tp2.price if tp2 is not None else NA,
        tp3_candidate=tp3.price if tp3 is not None else NA,
        nearest_opposing_liquidity=tp1.price if tp1 is not None else NA,
        target_distance=target_distance,
        clean_path_distance=clean_path_distance,
        rr_to_tp1=_target_rr(tp1),
        rr_to_tp2=rr_to_tp2,
        rr_to_tp3=_target_rr(tp3),
        target_quality_grade=grade,
        target_failure_type=failure_type,
        rr_compression_reason=_rr_compression_reason(
            data=data,
            direction=direction,
            risk=risk,
            targets=targets,
            tp2=tp2,
            failure_type=failure_type,
            candles=candles,
        ),
        target_confidence=confidence,
        next_target_condition=_next_condition(failure_type),
        liquidity_targets=targets,
        rr_projections=projections,
        warnings=_warnings(data, *candle_warnings, *htf_warnings, *_failure_warnings(failure_type)),
    )


def _target_zones(
    data: TargetIntelligenceInput,
    direction: Literal["long", "short"],
    entry: Decimal,
    stop: Decimal,
    candles: Sequence[_Candle],
    htf_candles: Sequence[_Candle],
) -> tuple[LiquidityTarget, ...]:
    targets: list[LiquidityTarget] = []
    risk = abs(entry - stop)
    targets.extend(_opposing_swings(candles, direction, entry, risk, data.swing_lookback))
    targets.extend(_equal_liquidity(candles, direction, entry, risk, _level_tolerance(data, entry)))
    targets.extend(_range_target(data, candles, direction, entry, risk))
    targets.extend(_prior_bos_origin(data, direction, entry, risk))
    targets.extend(_htf_supply_demand(htf_candles, direction, entry, risk, data.swing_lookback, data.range_window))
    targets.extend(_volume_profile_targets(data, direction, entry, risk))
    targets.extend(_fib_extension_targets(data, direction, entry, risk))
    targets.extend(_provided_liquidity_targets(data, direction, entry, risk))
    return tuple(targets)


def _opposing_swings(
    candles: Sequence[_Candle],
    direction: Literal["long", "short"],
    entry: Decimal,
    risk: Decimal,
    lookback: int,
) -> tuple[LiquidityTarget, ...]:
    swings = _detect_swings(candles, lookback)
    if direction == "long":
        levels = [point.price for point in swings if point.kind == "high" and point.price > entry]
        source = "opposing_swing_high"
        target_type = "swing_high"
    else:
        levels = [point.price for point in swings if point.kind == "low" and point.price < entry]
        source = "opposing_swing_low"
        target_type = "swing_low"
    return tuple(
        _liquidity_target(
            price=level,
            target_type=target_type,
            source=source,
            confidence=72,
            entry=entry,
            risk=risk,
        )
        for level in levels
    )


def _equal_liquidity(
    candles: Sequence[_Candle],
    direction: Literal["long", "short"],
    entry: Decimal,
    risk: Decimal,
    tolerance: Decimal,
) -> tuple[LiquidityTarget, ...]:
    if not candles:
        return ()
    values = [candle.high if direction == "long" else candle.low for candle in candles]
    side_values = [value for value in values if _on_target_side(direction, value, entry)]
    clustered = _equal_clusters(side_values, tolerance)
    source = "equal_highs" if direction == "long" else "equal_lows"
    target_type = "equal_high" if direction == "long" else "equal_low"
    return tuple(
        _liquidity_target(
            price=level,
            target_type=target_type,
            source=source,
            confidence=78,
            entry=entry,
            risk=risk,
            notes=f"{count} touches within tolerance.",
        )
        for level, count in clustered
    )


def _range_target(
    data: TargetIntelligenceInput,
    candles: Sequence[_Candle],
    direction: Literal["long", "short"],
    entry: Decimal,
    risk: Decimal,
) -> tuple[LiquidityTarget, ...]:
    if direction == "long":
        level = data.recent_range_high
        if level == NA and candles:
            sample = candles[-data.range_window :]
            level = _quantize(max(candle.high for candle in sample))
        source = "range_high"
        target_type = "range_high"
    else:
        level = data.recent_range_low
        if level == NA and candles:
            sample = candles[-data.range_window :]
            level = _quantize(min(candle.low for candle in sample))
        source = "range_low"
        target_type = "range_low"
    if level == NA:
        return ()
    price = _decimal_from(level, "range_target")
    if not _on_target_side(direction, price, entry):
        return ()
    return (
        _liquidity_target(
            price=price,
            target_type=target_type,
            source=source,
            confidence=62,
            entry=entry,
            risk=risk,
        ),
    )


def _prior_bos_origin(
    data: TargetIntelligenceInput,
    direction: Literal["long", "short"],
    entry: Decimal,
    risk: Decimal,
) -> tuple[LiquidityTarget, ...]:
    if data.bos_origin_price == NA:
        return ()
    price = _decimal_from(data.bos_origin_price, "bos_origin_price")
    if not _on_target_side(direction, price, entry):
        return ()
    return (
        _liquidity_target(
            price=price,
            target_type="bos_origin",
            source="prior_bos_origin",
            confidence=58,
            entry=entry,
            risk=risk,
        ),
    )


def _htf_supply_demand(
    htf_candles: Sequence[_Candle],
    direction: Literal["long", "short"],
    entry: Decimal,
    risk: Decimal,
    lookback: int,
    range_window: int,
) -> tuple[LiquidityTarget, ...]:
    if not htf_candles:
        return ()
    targets: list[LiquidityTarget] = []
    swings = _detect_swings(htf_candles, lookback)
    if direction == "long":
        levels = [point.price for point in swings if point.kind == "high" and point.price > entry]
        source = "htf_supply_proxy"
        target_type = "htf_supply"
        range_level = max(candle.high for candle in htf_candles[-range_window:])
    else:
        levels = [point.price for point in swings if point.kind == "low" and point.price < entry]
        source = "htf_demand_proxy"
        target_type = "htf_demand"
        range_level = min(candle.low for candle in htf_candles[-range_window:])
    if _on_target_side(direction, range_level, entry):
        levels.append(range_level)
    for level in levels:
        targets.append(
            _liquidity_target(
                price=level,
                target_type=target_type,
                source=source,
                confidence=55,
                entry=entry,
                risk=risk,
            )
        )
    return tuple(targets)


def _volume_profile_targets(
    data: TargetIntelligenceInput,
    direction: Literal["long", "short"],
    entry: Decimal,
    risk: Decimal,
) -> tuple[LiquidityTarget, ...]:
    values: list[tuple[MaybeDecimal, str, str]] = []
    if direction == "long":
        values.extend(
            (
                (data.value_area_high, "volume_profile_vah", "value_area_high"),
                (data.nearest_high_volume_node, "volume_profile_hvn", "high_volume_node"),
                (data.poc, "volume_profile_poc", "poc"),
            )
        )
    else:
        values.extend(
            (
                (data.value_area_low, "volume_profile_val", "value_area_low"),
                (data.nearest_low_volume_node, "volume_profile_lvn", "low_volume_node"),
                (data.poc, "volume_profile_poc", "poc"),
            )
        )
    targets = []
    for value, source, target_type in values:
        if value == NA:
            continue
        price = _decimal_from(value, source)
        if _on_target_side(direction, price, entry):
            targets.append(
                _liquidity_target(
                    price=price,
                    target_type=target_type,
                    source=source,
                    confidence=52,
                    entry=entry,
                    risk=risk,
                )
            )
    return tuple(targets)


def _fib_extension_targets(
    data: TargetIntelligenceInput,
    direction: Literal["long", "short"],
    entry: Decimal,
    risk: Decimal,
) -> tuple[LiquidityTarget, ...]:
    if data.impulse_start == NA or data.impulse_end == NA:
        return ()
    start = _decimal_from(data.impulse_start, "impulse_start")
    end = _decimal_from(data.impulse_end, "impulse_end")
    impulse = abs(end - start)
    if impulse <= 0:
        return ()
    targets = []
    for extension in FIB_EXTENSIONS:
        price = start + impulse * extension if direction == "long" else start - impulse * extension
        if _on_target_side(direction, price, entry):
            label = str(extension).rstrip("0").rstrip(".")
            targets.append(
                _liquidity_target(
                    price=price,
                    target_type=f"fib_{label}",
                    source=f"fib_extension_{label}",
                    confidence=50,
                    entry=entry,
                    risk=risk,
                )
            )
    return tuple(targets)


def _provided_liquidity_targets(
    data: TargetIntelligenceInput,
    direction: Literal["long", "short"],
    entry: Decimal,
    risk: Decimal,
) -> tuple[LiquidityTarget, ...]:
    if direction == "long":
        raw_levels = (*_extract_levels(data.user_resistance_levels), *_extract_levels(data.liquidity_above))
    else:
        raw_levels = (*_extract_levels(data.user_support_levels), *_extract_levels(data.liquidity_below))
    return tuple(
        _liquidity_target(
            price=level,
            target_type="provided_liquidity",
            source="provided_opposing_liquidity",
            confidence=65,
            entry=entry,
            risk=risk,
        )
        for level in raw_levels
        if _on_target_side(direction, level, entry)
    )


def _liquidity_target(
    *,
    price: Decimal,
    target_type: str,
    source: str,
    confidence: int,
    entry: Decimal,
    risk: Decimal,
    notes: str = NA,
) -> LiquidityTarget:
    distance = abs(price - entry)
    rr: MaybeDecimal = NA if risk <= 0 else _quantize(distance / risk)
    return LiquidityTarget(
        price=_quantize(price),
        target_type=target_type,
        source=source,
        confidence_score=confidence,
        distance=_quantize(distance),
        rr=rr,
        is_blocking=source in STRUCTURE_BLOCK_SOURCES or source in HTF_BLOCK_SOURCES,
        notes=notes,
    )


def _failure_type(
    *,
    data: TargetIntelligenceInput,
    direction: Literal["long", "short"],
    entry: Decimal,
    risk: Decimal,
    targets: Sequence[LiquidityTarget],
    tp2: LiquidityTarget | None,
    rr_to_tp2: MaybeDecimal,
) -> TargetFailureType | Literal["N/A"]:
    if data.missing_data and _critical_missing_data(data.missing_data):
        return TargetFailureType.DATA_INCOMPLETE
    if tp2 is None:
        return TargetFailureType.TP_TOO_CLOSE
    clean_rr = _target_rr(targets[0])
    if _target_inside_chop(data, direction, entry, tp2):
        return TargetFailureType.TARGET_INSIDE_CHOP
    if clean_rr != NA and clean_rr < data.minimum_rr and targets[0].source in HTF_BLOCK_SOURCES:
        return TargetFailureType.HTF_RESISTANCE_TOO_CLOSE
    if clean_rr != NA and clean_rr < data.minimum_rr and targets[0].source in STRUCTURE_BLOCK_SOURCES:
        return TargetFailureType.OPPOSING_STRUCTURE_BLOCK
    if rr_to_tp2 == NA:
        return TargetFailureType.NO_CLEAR_TARGET
    if rr_to_tp2 < data.minimum_rr:
        if _target_distance(tp2) != NA and _decimal_from(_target_distance(tp2), "target_distance") <= risk:
            return TargetFailureType.TP_TOO_CLOSE
        return TargetFailureType.RR_BELOW_MINIMUM
    return NA


def _quality_grade(
    *,
    failure_type: TargetFailureType | Literal["N/A"],
    rr_to_tp2: MaybeDecimal,
    minimum_rr: Decimal,
    confidence: MaybeInt,
) -> TargetQualityGrade:
    if failure_type != NA:
        return TargetQualityGrade.REJECT
    score = 0 if confidence == NA else int(confidence)
    if rr_to_tp2 != NA and rr_to_tp2 >= minimum_rr * Decimal("1.20") and score >= 75:
        return TargetQualityGrade.A
    if rr_to_tp2 != NA and rr_to_tp2 >= minimum_rr and score >= 60:
        return TargetQualityGrade.B
    return TargetQualityGrade.C


def _rr_compression_reason(
    *,
    data: TargetIntelligenceInput,
    direction: Literal["long", "short"],
    risk: Decimal,
    targets: Sequence[LiquidityTarget],
    tp2: LiquidityTarget | None,
    failure_type: TargetFailureType | Literal["N/A"],
    candles: Sequence[_Candle],
) -> str:
    if failure_type == NA:
        return NA
    if failure_type == TargetFailureType.DATA_INCOMPLETE:
        return "Required target or RR data is incomplete."
    if failure_type == TargetFailureType.NO_CLEAR_TARGET:
        return "No clear TP2 can be mapped from available candle structure."
    if tp2 is None:
        return "Only one usable target is visible; TP2 is N/A rather than invented."

    clean_rr = _target_rr(targets[0])
    if failure_type == TargetFailureType.HTF_RESISTANCE_TOO_CLOSE:
        return "HTF supply/demand proxy is too close to justify the required RR."
    if failure_type == TargetFailureType.OPPOSING_STRUCTURE_BLOCK:
        return "Opposing structure is too close; the clean path ends before required RR."
    if failure_type == TargetFailureType.TARGET_INSIDE_CHOP:
        return "TP2 remains inside recent chop/range, so profit room is unreliable."
    if failure_type == TargetFailureType.TP_TOO_CLOSE:
        return "TP2 candidate is too close to entry for the current stop."

    average_range = _average_candle_range(candles)
    if average_range is not None and risk > average_range * Decimal("2"):
        return "Stop is too wide for visible TP2 distance."
    if clean_rr != NA and clean_rr < data.minimum_rr:
        return "Opposing liquidity is too close; a wider clean TP2 or better entry is required."
    return "Entry is too late for the visible TP2 room; RR remains below the minimum."


def _next_condition(failure_type: TargetFailureType | Literal["N/A"]) -> str:
    if failure_type == TargetFailureType.DATA_INCOMPLETE:
        return "Complete candle and level data required."
    if failure_type == TargetFailureType.NO_CLEAR_TARGET:
        return "Clear opposing liquidity or valid fib extension must form before activation."
    if failure_type in {TargetFailureType.TP_TOO_CLOSE, TargetFailureType.RR_BELOW_MINIMUM}:
        return "Wider clean TP2 or better entry required"
    if failure_type == TargetFailureType.OPPOSING_STRUCTURE_BLOCK:
        return "Opposing structure must clear or entry must improve before RR can pass."
    if failure_type == TargetFailureType.TARGET_INSIDE_CHOP:
        return "Target must expand beyond chop/range before activation."
    if failure_type == TargetFailureType.HTF_RESISTANCE_TOO_CLOSE:
        return "HTF obstacle must clear or setup needs a better entry."
    return "Maintain clean path to TP2 with RR above minimum."


def _failure_warnings(failure_type: TargetFailureType | Literal["N/A"]) -> tuple[str, ...]:
    if failure_type == NA:
        return ()
    return ("Diagnostic only; target intelligence does not loosen RR or strategy gates.",)


def _result(
    *,
    data: TargetIntelligenceInput,
    failure_type: TargetFailureType,
    rr_compression_reason: str,
    target_confidence: MaybeInt = NA,
    warnings: Sequence[str] = (),
) -> TargetIntelligenceResult:
    return TargetIntelligenceResult(
        target_failure_type=failure_type,
        rr_compression_reason=rr_compression_reason,
        target_confidence=target_confidence,
        next_target_condition=_next_condition(failure_type),
        warnings=_warnings(data, *warnings, *_failure_warnings(failure_type)),
    )


def _rr_projections(targets: Sequence[LiquidityTarget], minimum_rr: Decimal) -> tuple[RRProjection, ...]:
    projections: list[RRProjection] = []
    for index, label in enumerate(("TP1", "TP2", "TP3")):
        target = targets[index] if index < len(targets) else None
        rr = _target_rr(target)
        projections.append(
            RRProjection(
                target_label=label,
                target_price=target.price if target is not None else NA,
                target_source=target.source if target is not None else NA,
                distance=target.distance if target is not None else NA,
                rr=rr,
                meets_minimum=rr >= minimum_rr if rr != NA else NA,
            )
        )
    return tuple(projections)


def _target_confidence(targets: Sequence[LiquidityTarget], data: TargetIntelligenceInput) -> MaybeInt:
    if not targets:
        return 0
    top = targets[: min(3, len(targets))]
    scores = [int(target.confidence_score) for target in top if target.confidence_score != NA]
    if not scores:
        return NA
    score = int(sum(scores) / len(scores))
    score += min(15, max(0, len(targets) - 2) * 3)
    if data.unverified_data:
        score -= 10
    return max(0, min(100, score))


def _target_inside_chop(
    data: TargetIntelligenceInput,
    direction: Literal["long", "short"],
    entry: Decimal,
    tp2: LiquidityTarget,
) -> bool:
    if data.recent_range_high == NA or data.recent_range_low == NA:
        return False
    high = _decimal_from(data.recent_range_high, "recent_range_high")
    low = _decimal_from(data.recent_range_low, "recent_range_low")
    if high <= low or not (low <= entry <= high):
        return False
    target = _decimal_from(tp2.price, "tp2")
    if direction == "long":
        return target <= high and tp2.source in {"range_high", "equal_highs", "volume_profile_vah", "volume_profile_poc"}
    return target >= low and tp2.source in {"range_low", "equal_lows", "volume_profile_val", "volume_profile_poc"}


def _dedupe_targets(targets: Sequence[LiquidityTarget], tolerance: Decimal) -> tuple[LiquidityTarget, ...]:
    deduped: list[LiquidityTarget] = []
    for target in targets:
        if target.price == NA:
            continue
        price = _decimal_from(target.price, "target.price")
        match_index = next(
            (
                index
                for index, existing in enumerate(deduped)
                if existing.price != NA and abs(_decimal_from(existing.price, "existing.price") - price) <= tolerance
            ),
            None,
        )
        if match_index is None:
            deduped.append(target)
            continue
        existing = deduped[match_index]
        existing_confidence = 0 if existing.confidence_score == NA else int(existing.confidence_score)
        target_confidence = 0 if target.confidence_score == NA else int(target.confidence_score)
        if target_confidence > existing_confidence:
            deduped[match_index] = target.model_copy(
                update={"notes": _merged_notes(existing.notes, target.notes, existing.source)}
            )
        elif existing.source != target.source:
            deduped[match_index] = existing.model_copy(
                update={"notes": _merged_notes(existing.notes, target.notes, target.source)}
            )
    return tuple(deduped)


def _ordered_targets(targets: Sequence[LiquidityTarget], direction: Literal["long", "short"]) -> tuple[LiquidityTarget, ...]:
    reverse = direction == "short"
    return tuple(sorted(targets, key=lambda target: _decimal_from(target.price, "target.price"), reverse=reverse))


def _target_rr(target: LiquidityTarget | None) -> MaybeDecimal:
    if target is None:
        return NA
    return target.rr


def _target_distance(target: LiquidityTarget | None) -> MaybeDecimal:
    if target is None:
        return NA
    return target.distance


def _detect_swings(candles: Sequence[_Candle], lookback: int) -> tuple[_SwingPoint, ...]:
    if len(candles) < lookback * 2 + 1:
        return ()
    points: list[_SwingPoint] = []
    for index in range(lookback, len(candles) - lookback):
        current = candles[index]
        left = candles[index - lookback : index]
        right = candles[index + 1 : index + lookback + 1]
        if all(current.high > candle.high for candle in (*left, *right)):
            points.append(
                _SwingPoint(
                    kind="high",
                    index=current.index,
                    confirmed_at_index=current.index + lookback,
                    price=current.high,
                )
            )
        if all(current.low < candle.low for candle in (*left, *right)):
            points.append(
                _SwingPoint(
                    kind="low",
                    index=current.index,
                    confirmed_at_index=current.index + lookback,
                    price=current.low,
                )
            )
    return tuple(points)


def _equal_clusters(values: Sequence[Decimal], tolerance: Decimal) -> tuple[tuple[Decimal, int], ...]:
    if len(values) < 2:
        return ()
    sorted_values = sorted(values)
    clusters: list[list[Decimal]] = []
    current: list[Decimal] = []
    anchor: Decimal | None = None
    for value in sorted_values:
        if anchor is None or abs(value - anchor) <= tolerance:
            current.append(value)
            anchor = value if anchor is None else anchor
            continue
        if len(current) >= 2:
            clusters.append(current)
        current = [value]
        anchor = value
    if len(current) >= 2:
        clusters.append(current)
    return tuple((_quantize(sum(cluster, Decimal("0")) / Decimal(len(cluster))), len(cluster)) for cluster in clusters)


def _normalize_candles(candles: Sequence[Any]) -> tuple[tuple[_Candle, ...], tuple[str, ...]]:
    if not candles:
        return (), ()
    normalized: list[_Candle] = []
    warnings: list[str] = []
    for index, candle in enumerate(candles):
        try:
            open_price = _decimal_from(_field(candle, "open"), f"candles[{index}].open")
            high = _decimal_from(_field(candle, "high"), f"candles[{index}].high")
            low = _decimal_from(_field(candle, "low"), f"candles[{index}].low")
            close = _decimal_from(_field(candle, "close"), f"candles[{index}].close")
        except ValueError:
            warnings.append(f"candles[{index}]: N/A")
            continue
        if high < low or high < max(open_price, close) or low > min(open_price, close):
            warnings.append(f"candles[{index}]: malformed")
            continue
        volume_value = _field(candle, "volume")
        volume: MaybeDecimal = NA if _is_missing(volume_value) else _decimal_from(volume_value, f"candles[{index}].volume")
        normalized.append(
            _Candle(
                index=int(_field(candle, "index") if _field(candle, "index") is not None else index),
                timestamp=_field(candle, "timestamp") if _field(candle, "timestamp") is not None else NA,
                open=open_price,
                high=high,
                low=low,
                close=close,
                volume=volume,
            )
        )
    return tuple(normalized), tuple(warnings)


def _extract_levels(values: Any) -> tuple[Decimal, ...]:
    if values is None or values == NA:
        return ()
    if isinstance(values, (str, bytes)):
        raw_values: Sequence[Any] = (values,)
    elif isinstance(values, Sequence):
        raw_values = values
    else:
        raw_values = (values,)

    levels: list[Decimal] = []
    for value in raw_values:
        if _is_missing(value):
            continue
        if isinstance(value, Mapping):
            for key in ("price", "level", "value"):
                if key in value and not _is_missing(value[key]):
                    value = value[key]
                    break
            else:
                continue
        try:
            levels.append(_quantize(_decimal_from(value, "level")))
        except ValueError:
            continue
    return tuple(levels)


def _average_candle_range(candles: Sequence[_Candle]) -> Decimal | None:
    if not candles:
        return None
    values = [candle.high - candle.low for candle in candles[-20:] if candle.high >= candle.low]
    if not values:
        return None
    return sum(values, Decimal("0")) / Decimal(len(values))


def _critical_missing_data(values: Sequence[str]) -> bool:
    return any(str(value).startswith(("candles:", "candles_15m:", "candles_5m:", "entry:", "stop:")) for value in values)


def _warnings(data: TargetIntelligenceInput, *values: str) -> tuple[str, ...]:
    warnings: list[str] = []
    if data.unverified_data:
        warnings.append("Some target source data is Unverified.")
    for value in values:
        text = _display(value)
        if text != NA and text not in warnings:
            warnings.append(text)
    return tuple(warnings)


def _level_tolerance(data: TargetIntelligenceInput, entry: Decimal) -> Decimal:
    pct = data.equal_level_tolerance_pct
    if pct <= 0:
        return OUTPUT_QUANT
    return max(OUTPUT_QUANT, abs(entry) * pct)


def _on_target_side(direction: Literal["long", "short"], price: Decimal, entry: Decimal) -> bool:
    return price > entry if direction == "long" else price < entry


def _normalize_direction(value: Any) -> Literal["long", "short", "N/A"]:
    text = _display(value).lower()
    if text in {"long", "bullish", "buy"}:
        return "long"
    if text in {"short", "bearish", "sell"}:
        return "short"
    return NA


def _merged_notes(existing: str, incoming: str, source: str) -> str:
    values = [value for value in (existing, incoming, f"confluence: {source}") if _display(value) != NA]
    output: list[str] = []
    for value in values:
        if value not in output:
            output.append(value)
    return "; ".join(output) if output else NA


def _field(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _sequence_values(values: Any) -> tuple[str, ...]:
    if values is None or isinstance(values, (str, bytes)):
        return () if _is_missing(values) else (_display(values),)
    if not isinstance(values, Sequence):
        return ()
    return tuple(_display(value) for value in values if _display(value) != NA)


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid target intelligence decimal at {path}: {value!r}") from exc
    if not decimal.is_finite():
        raise ValueError(f"Invalid target intelligence decimal at {path}: {value!r}")
    return decimal


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if isinstance(value, Enum):
        return str(value.value)
    if value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


__all__ = [
    "LiquidityTarget",
    "RRProjection",
    "TargetFailureType",
    "TargetIntelligenceInput",
    "TargetIntelligenceResult",
    "TargetQualityGrade",
    "build_target_intelligence",
]
