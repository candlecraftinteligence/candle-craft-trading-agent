from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.data.dtos import NA, MaybeDecimal, MaybeInt

OUTPUT_QUANT = Decimal("0.00000001")
BASE_MIN_RR = Decimal("2.5")
CHALLENGE_MIN_RR = Decimal("3.0")
DEFAULT_TICK_SIZE = Decimal("0.00000001")
ATR_STOP_BUFFER_MULTIPLIER = Decimal("0.10")

DecimalLike = Decimal | int | str
TradeDirection = Literal["long", "short"]
AnyDirection = Literal["long", "short", "bullish", "bearish", "N/A"]
ZoneType = Literal["OB", "FVG", "OB_FVG_OVERLAP", "N/A"]


class FibAlignmentResult(BaseModel):
    is_aligned: bool = False
    direction: AnyDirection = NA
    retracement: MaybeDecimal = NA
    preferred_low: MaybeDecimal = NA
    preferred_high: MaybeDecimal = NA
    fib_382: MaybeDecimal = NA
    fib_618: MaybeDecimal = NA
    fib_65: MaybeDecimal = NA
    fib_786: MaybeDecimal = NA
    fib_min: MaybeDecimal = NA
    fib_max: MaybeDecimal = NA
    status: str = NA
    aggressive_drift_used: bool = False
    rejected_deeper_than_786: bool = False
    reason: str = "Fib alignment is N/A because impulse or entry data is missing."

    model_config = ConfigDict(frozen=True)


class PullbackZone(BaseModel):
    is_present: bool = False
    zone_type: ZoneType = NA
    direction: TradeDirection | Literal["N/A"] = NA
    creation_index: MaybeInt = NA
    low: MaybeDecimal = NA
    high: MaybeDecimal = NA
    midpoint: MaybeDecimal = NA
    body_low: MaybeDecimal = NA
    body_high: MaybeDecimal = NA
    wick_low: MaybeDecimal = NA
    wick_high: MaybeDecimal = NA
    fill_low: MaybeDecimal = NA
    fill_high: MaybeDecimal = NA
    entry_low: MaybeDecimal = NA
    entry_high: MaybeDecimal = NA
    fib_min: MaybeDecimal = NA
    fib_max: MaybeDecimal = NA
    fib_alignment_status: str = NA
    freshness_status: str = NA
    invalidation_level: MaybeDecimal = NA
    atr_stop_buffer: MaybeDecimal = NA
    confluence: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    reason: str = NA

    model_config = ConfigDict(frozen=True)


class PullbackZoneInput(BaseModel):
    symbol: str
    direction: TradeDirection
    execution_timeframe: str
    confirmation_timeframe: str
    calculation_timeframe: str = NA
    candles_15m: Sequence[Any]
    candles_5m: Sequence[Any]
    sweep_candle_index: MaybeInt
    bos_choch_candle_index: MaybeInt = NA
    latest_price: MaybeDecimal = NA
    atr_15m: MaybeDecimal = NA
    tick_size: MaybeDecimal = NA
    aggressive_toggle: bool = False
    minimum_rr: Decimal = BASE_MIN_RR
    poc: MaybeDecimal = NA
    value_area_high: MaybeDecimal = NA
    value_area_low: MaybeDecimal = NA
    liquidity_below: Sequence[Any] | Any | None = None
    liquidity_above: Sequence[Any] | Any | None = None
    user_support_levels: Sequence[Any] | Any | None = None
    user_resistance_levels: Sequence[Any] | Any | None = None

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    @field_validator("symbol")
    @classmethod
    def _symbol_not_blank(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator("execution_timeframe", "confirmation_timeframe")
    @classmethod
    def _timeframe_not_blank(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("timeframe must not be blank")
        return normalized

    @field_validator("minimum_rr", mode="before")
    @classmethod
    def _minimum_rr_decimal(cls, value: Any) -> Any:
        return _decimal_from(value, "minimum_rr")


class PullbackZoneResult(BaseModel):
    valid: bool = False
    direction: TradeDirection | Literal["N/A"] = NA
    pullback_zone_status: Literal["valid", "failed", "N/A"] = "N/A"
    first_failed_gate: str = NA
    pullback_failure_reason: str = NA
    calculation_timeframe: str = NA
    sweep_candle_index: MaybeInt = NA
    bos_choch_candle_index: MaybeInt = NA
    displacement_start_index: MaybeInt = NA
    displacement_end_index: MaybeInt = NA
    selected_zone_type: ZoneType = NA
    selected_zone: PullbackZone = PullbackZone()
    ob_zone: PullbackZone = PullbackZone(zone_type="OB")
    fvg_zone: PullbackZone = PullbackZone(zone_type="FVG")
    fib_alignment: FibAlignmentResult = FibAlignmentResult()
    impulse_start: MaybeDecimal = NA
    impulse_end: MaybeDecimal = NA
    impulse_low: MaybeDecimal = NA
    impulse_high: MaybeDecimal = NA
    sweep_price: MaybeDecimal = NA
    bos_price: MaybeDecimal = NA
    fib_382: MaybeDecimal = NA
    fib_618: MaybeDecimal = NA
    fib_65: MaybeDecimal = NA
    fib_786: MaybeDecimal = NA
    fib_min: MaybeDecimal = NA
    fib_max: MaybeDecimal = NA
    pullback_depth_ratio: MaybeDecimal = NA
    entry_low: MaybeDecimal = NA
    entry_high: MaybeDecimal = NA
    entry: MaybeDecimal = NA
    stop: MaybeDecimal = NA
    tp1: MaybeDecimal = NA
    tp2: MaybeDecimal = NA
    tp3: MaybeDecimal = NA
    rr_to_tp2: MaybeDecimal = NA
    atr_stop_buffer: MaybeDecimal = NA
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


def analyze_pullback_zone(input_data: PullbackZoneInput | Mapping[str, Any]) -> PullbackZoneResult:
    data = input_data if isinstance(input_data, PullbackZoneInput) else PullbackZoneInput.model_validate(input_data)
    calculation_timeframe, candle_values, candle_label = _calculation_candle_source(data)
    candles, errors = _normalize_candles(candle_values, candle_label)
    if errors:
        raise ValueError(errors[0])

    sweep_index = _index_or_none(data.sweep_candle_index)
    bos_index = _index_or_none(data.bos_choch_candle_index)
    index_update = {
        "calculation_timeframe": calculation_timeframe,
        "sweep_candle_index": sweep_index if sweep_index is not None else NA,
        "bos_choch_candle_index": bos_index if bos_index is not None else NA,
    }
    if not candles:
        return _failed_result(
            data,
            "missing_confirmation_candles",
            f"Pullback calculation candles are missing for {calculation_timeframe}.",
            **index_update,
        )
    if sweep_index is None or bos_index is None:
        return _failed_result(
            data,
            "missing_displacement_impulse",
            "Displacement impulse is N/A because sweep or BOS/CHoCH index is unavailable.",
            **index_update,
        )
    if (
        sweep_index < 0
        or bos_index <= sweep_index
        or bos_index >= len(candles)
    ):
        return _failed_result(
            data,
            "no_displacement_candle",
            f"No displacement candle could be evaluated between sweep and BOS/CHoCH on {calculation_timeframe}.",
            **index_update,
        )

    impulse = _displacement_impulse(candles, data.direction, sweep_index, bos_index)
    if impulse is None:
        return _failed_result(
            data,
            "no_displacement_candle",
            f"No displacement candle was found from the sweep wick to BOS/CHoCH on {calculation_timeframe}.",
            **index_update,
        )

    impulse_start, impulse_end, impulse_low, impulse_high = impulse
    fib_levels = _fib_levels(data.direction, impulse_start, impulse_end)
    pullback_depth_ratio = _deepest_pullback_ratio(
        candles,
        data.direction,
        bos_index,
        impulse_start,
        impulse_end,
        data.latest_price,
    )
    fib_alignment = _fib_alignment_for_levels(data.direction, impulse_start, impulse_end, None, data.aggressive_toggle)
    deep_reject = _pullback_deeper_than_786(candles, data.direction, bos_index, fib_levels, data.latest_price)
    ob_zone = _detect_order_block(candles, data.direction, sweep_index, bos_index, impulse_end)
    fvg_zone = _detect_fvg(candles, data.direction, sweep_index, bos_index)

    base_update = {
        "direction": data.direction,
        **index_update,
        "displacement_start_index": sweep_index,
        "displacement_end_index": bos_index,
        "ob_zone": ob_zone,
        "fvg_zone": fvg_zone,
        "impulse_start": _quantize(impulse_start),
        "impulse_end": _quantize(impulse_end),
        "impulse_low": _quantize(impulse_low),
        "impulse_high": _quantize(impulse_high),
        "sweep_price": _quantize(impulse_start),
        "bos_price": _quantize(impulse_end),
        "fib_382": fib_levels["0.382"],
        "fib_618": fib_levels["0.618"],
        "fib_65": fib_levels["0.65"],
        "fib_786": fib_levels["0.786"],
        "fib_min": fib_levels["min"],
        "fib_max": fib_levels["max"],
        "pullback_depth_ratio": pullback_depth_ratio if pullback_depth_ratio is not None else NA,
    }

    if deep_reject:
        return _failed_result(
            data,
            "pullback_too_deep",
            "Pullback tagged beyond 0.786 before entry.",
            **base_update,
            fib_alignment=fib_alignment.model_copy(
                update={
                    "rejected_deeper_than_786": True,
                    "status": "pullback_too_deep",
                    "reason": "Pullback tagged beyond 0.786 before entry.",
                }
            ),
        )

    if not ob_zone.is_present and not fvg_zone.is_present:
        return _failed_result(
            data,
            "no_ob_or_fvg_zone",
            f"No valid OB or FVG was found inside the {calculation_timeframe} displacement impulse.",
            **base_update,
            fib_alignment=fib_alignment,
        )

    selected = _select_zone(data, ob_zone, fvg_zone, fib_levels)
    if selected is None:
        return _failed_result(
            data,
            "no_ob_or_fvg_zone",
            "No OB/FVG zone overlapped the 0.382 to 0.618 fib pullback zone.",
            **base_update,
            fib_alignment=fib_alignment.model_copy(
                update={
                    "status": "failed_no_overlap",
                    "reason": "OB/FVG zones did not overlap the preferred fib retracement zone.",
                }
            ),
        )

    entry = _entry_price(data.direction, selected)
    fib_alignment = _fib_alignment_for_levels(data.direction, impulse_start, impulse_end, entry, data.aggressive_toggle)
    if not fib_alignment.is_aligned:
        return _failed_result(
            data,
            "no_ob_or_fvg_zone",
            fib_alignment.reason,
            **base_update,
            selected_zone=selected,
            selected_zone_type=selected.zone_type,
            entry_low=selected.entry_low,
            entry_high=selected.entry_high,
            entry=_quantize(entry),
            fib_alignment=fib_alignment,
        )

    stop, atr_stop_buffer = _stop_price(data, candles[sweep_index], ob_zone, entry)
    if stop == NA:
        return _failed_result(
            data,
            "missing_stop",
            "Stop is N/A because structure edge is not on the valid side of entry.",
            **base_update,
            selected_zone=selected,
            selected_zone_type=selected.zone_type,
            entry_low=selected.entry_low,
            entry_high=selected.entry_high,
            entry=_quantize(entry),
            fib_alignment=fib_alignment,
        )

    tp1, tp2, tp3 = _targets(data, entry, impulse_start, impulse_end)
    rr_to_tp2 = _risk_reward(data.direction, entry, _decimal_from(stop, "stop"), tp2)
    if rr_to_tp2 == NA:
        return _failed_result(
            data,
            "missing_target",
            "Target or RR is N/A after pullback zone selection.",
            **base_update,
            selected_zone=selected,
            selected_zone_type=selected.zone_type,
            entry_low=selected.entry_low,
            entry_high=selected.entry_high,
            entry=_quantize(entry),
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            atr_stop_buffer=atr_stop_buffer,
            fib_alignment=fib_alignment,
        )

    if _decimal_from(rr_to_tp2, "rr_to_tp2") < data.minimum_rr:
        return _failed_result(
            data,
            "rr_below_minimum",
            f"RR to TP2 {_display(rr_to_tp2)} is below {data.minimum_rr}.",
            **base_update,
            selected_zone=selected,
            selected_zone_type=selected.zone_type,
            entry_low=selected.entry_low,
            entry_high=selected.entry_high,
            entry=_quantize(entry),
            stop=stop,
            tp1=tp1,
            tp2=tp2,
            tp3=tp3,
            rr_to_tp2=rr_to_tp2,
            atr_stop_buffer=atr_stop_buffer,
            fib_alignment=fib_alignment,
        )

    selected = selected.model_copy(
        update={
            "invalidation_level": stop,
            "atr_stop_buffer": atr_stop_buffer,
            "fib_alignment_status": fib_alignment.status,
        }
    )
    return PullbackZoneResult(
        valid=True,
        direction=data.direction,
        pullback_zone_status="valid",
        calculation_timeframe=calculation_timeframe,
        sweep_candle_index=sweep_index,
        bos_choch_candle_index=bos_index,
        displacement_start_index=sweep_index,
        displacement_end_index=bos_index,
        selected_zone_type=selected.zone_type,
        selected_zone=selected,
        ob_zone=ob_zone,
        fvg_zone=fvg_zone,
        fib_alignment=fib_alignment,
        impulse_start=_quantize(impulse_start),
        impulse_end=_quantize(impulse_end),
        impulse_low=_quantize(impulse_low),
        impulse_high=_quantize(impulse_high),
        sweep_price=_quantize(impulse_start),
        bos_price=_quantize(impulse_end),
        fib_382=fib_levels["0.382"],
        fib_618=fib_levels["0.618"],
        fib_65=fib_levels["0.65"],
        fib_786=fib_levels["0.786"],
        fib_min=fib_levels["min"],
        fib_max=fib_levels["max"],
        pullback_depth_ratio=pullback_depth_ratio if pullback_depth_ratio is not None else NA,
        entry_low=selected.entry_low,
        entry_high=selected.entry_high,
        entry=_quantize(entry),
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        rr_to_tp2=rr_to_tp2,
        atr_stop_buffer=atr_stop_buffer,
        warnings=selected.warnings,
    )


def calculate_fib_alignment(
    *,
    direction: AnyDirection,
    sweep_price: DecimalLike,
    bos_price: DecimalLike,
    entry_price: DecimalLike,
    aggressive_toggle: bool = False,
    deepest_pullback: DecimalLike | None = None,
) -> FibAlignmentResult:
    normalized_direction = _trade_direction(direction)
    if normalized_direction is None:
        return FibAlignmentResult(reason="Fib alignment is N/A because direction is N/A.")

    sweep = _decimal_from(sweep_price, "sweep_price")
    bos = _decimal_from(bos_price, "bos_price")
    entry = _decimal_from(entry_price, "entry_price")
    impulse = abs(bos - sweep)
    if impulse <= 0:
        return FibAlignmentResult(
            direction=direction,
            status="missing_displacement_impulse",
            reason="Fib alignment failed because impulse range is zero.",
        )

    deepest_ratio = _pullback_ratio(normalized_direction, sweep, bos, deepest_pullback)
    if deepest_ratio is not None and deepest_ratio > Decimal("0.786"):
        levels = _fib_levels(normalized_direction, sweep, bos)
        return FibAlignmentResult(
            direction=direction,
            retracement=_quantize(_retracement(normalized_direction, sweep, bos, entry)),
            rejected_deeper_than_786=True,
            status="pullback_too_deep",
            reason="Pullback tagged beyond 0.786 before entry.",
            **_fib_result_levels(levels, aggressive_toggle),
        )

    return _fib_alignment_for_levels(normalized_direction, sweep, bos, entry, aggressive_toggle, original_direction=direction)


def _calculation_candle_source(data: PullbackZoneInput) -> tuple[str, Sequence[Any], str]:
    timeframe = data.calculation_timeframe.strip().lower() if data.calculation_timeframe != NA else ""
    if not timeframe:
        timeframe = data.execution_timeframe

    if timeframe == data.confirmation_timeframe:
        return timeframe, data.candles_5m, "candles_5m"
    if timeframe == data.execution_timeframe:
        return timeframe, data.candles_15m, "candles_15m"
    if timeframe == "5m":
        return timeframe, data.candles_5m, "candles_5m"
    return timeframe, data.candles_15m, "candles_15m"


def _failed_result(data: PullbackZoneInput, gate: str, reason: str, **updates: Any) -> PullbackZoneResult:
    payload: dict[str, Any] = {
        "direction": data.direction,
        "first_failed_gate": gate,
        "pullback_failure_reason": reason,
        "pullback_zone_status": "failed",
    }
    payload.update(updates)
    return PullbackZoneResult(**payload)


def _displacement_impulse(
    candles: Sequence[_Candle],
    direction: TradeDirection,
    sweep_index: int,
    bos_index: int,
) -> tuple[Decimal, Decimal, Decimal, Decimal] | None:
    sample = candles[sweep_index : bos_index + 1]
    if not sample:
        return None

    if direction == "long":
        start = candles[sweep_index].low
        end = max(candle.high for candle in sample)
        if end <= start:
            return None
    else:
        start = candles[sweep_index].high
        end = min(candle.low for candle in sample)
        if end >= start:
            return None

    impulse_low = min(candle.low for candle in sample)
    impulse_high = max(candle.high for candle in sample)
    return start, end, impulse_low, impulse_high


def _detect_fvg(candles: Sequence[_Candle], direction: TradeDirection, sweep_index: int, bos_index: int) -> PullbackZone:
    found: list[PullbackZone] = []
    first_index = max(2, sweep_index + 2)
    for index in range(first_index, bos_index + 1):
        left = candles[index - 2]
        current = candles[index]
        if direction == "long" and current.low > left.high:
            low = left.high
            high = current.low
            found.append(
                _zone(
                    direction=direction,
                    zone_type="FVG",
                    creation_index=index,
                    low=low,
                    high=high,
                    freshness_status=_fvg_freshness(candles, direction, index, low, high),
                    fill_low=low,
                    fill_high=(low + high) / Decimal("2"),
                    reason="Bullish FVG found where candle[i].low is above candle[i-2].high.",
                )
            )
        if direction == "short" and current.high < left.low:
            low = current.high
            high = left.low
            found.append(
                _zone(
                    direction=direction,
                    zone_type="FVG",
                    creation_index=index,
                    low=low,
                    high=high,
                    freshness_status=_fvg_freshness(candles, direction, index, low, high),
                    fill_low=(low + high) / Decimal("2"),
                    fill_high=high,
                    reason="Bearish FVG found where candle[i].high is below candle[i-2].low.",
                )
            )
    if not found:
        return PullbackZone(zone_type="FVG", reason="FVG is N/A because no valid imbalance was found.")
    return sorted(found, key=_freshness_sort_key, reverse=True)[0]


def _detect_order_block(
    candles: Sequence[_Candle],
    direction: TradeDirection,
    sweep_index: int,
    bos_index: int,
    impulse_end: Decimal,
) -> PullbackZone:
    for index in range(bos_index - 1, sweep_index - 1, -1):
        candle = candles[index]
        if direction == "long" and candle.close < candle.open and impulse_end > candle.high:
            return _order_block_zone(candles, direction, candle, freshness_start_index=bos_index + 1)
        if direction == "short" and candle.close > candle.open and impulse_end < candle.low:
            return _order_block_zone(candles, direction, candle, freshness_start_index=bos_index + 1)
    return PullbackZone(zone_type="OB", reason="OB is N/A because no qualifying opposite-color candle was found.")


def _order_block_zone(
    candles: Sequence[_Candle],
    direction: TradeDirection,
    candle: _Candle,
    *,
    freshness_start_index: int,
) -> PullbackZone:
    body_low = min(candle.open, candle.close)
    body_high = max(candle.open, candle.close)
    return _zone(
        direction=direction,
        zone_type="OB",
        creation_index=candle.index,
        low=body_low,
        high=body_high,
        body_low=body_low,
        body_high=body_high,
        wick_low=candle.low,
        wick_high=candle.high,
        freshness_status=_ob_freshness(candles, direction, freshness_start_index, body_low, body_high),
        reason="Order block is the last opposite-color candle body before displacement/BOS.",
    )


def _select_zone(
    data: PullbackZoneInput,
    ob_zone: PullbackZone,
    fvg_zone: PullbackZone,
    fib_levels: Mapping[str, Decimal],
) -> PullbackZone | None:
    candidates = _candidate_zones(ob_zone, fvg_zone)
    fib_max = fib_levels["max_65"] if data.aggressive_toggle else fib_levels["max"]
    eligible: list[tuple[tuple[int, int, int], PullbackZone]] = []
    for candidate in candidates:
        if candidate.freshness_status == "mitigated":
            continue
        overlap = _overlap(
            _decimal_from(candidate.low, "zone.low"),
            _decimal_from(candidate.high, "zone.high"),
            fib_levels["min"],
            fib_max,
        )
        if overlap is None:
            continue
        entry_low, entry_high = overlap
        midpoint = (entry_low + entry_high) / Decimal("2")
        confluence = _profile_confluence(data, entry_low, entry_high)
        score = (
            3 if candidate.zone_type == "OB_FVG_OVERLAP" else 2,
            2 if candidate.freshness_status == "fresh" else 1,
            1 if confluence else 0,
        )
        eligible.append(
            (
                score,
                candidate.model_copy(
                    update={
                        "entry_low": _quantize(entry_low),
                        "entry_high": _quantize(entry_high),
                        "midpoint": _quantize(midpoint),
                        "fib_min": fib_levels["min"],
                        "fib_max": fib_max,
                        "fib_alignment_status": "aligned",
                        "confluence": confluence,
                    }
                ),
            )
        )
    if not eligible:
        return None
    return sorted(eligible, key=lambda item: item[0], reverse=True)[0][1]


def _candidate_zones(ob_zone: PullbackZone, fvg_zone: PullbackZone) -> tuple[PullbackZone, ...]:
    candidates: list[PullbackZone] = []
    if ob_zone.is_present and fvg_zone.is_present:
        overlap = _overlap(
            _decimal_from(ob_zone.low, "ob.low"),
            _decimal_from(ob_zone.high, "ob.high"),
            _decimal_from(fvg_zone.low, "fvg.low"),
            _decimal_from(fvg_zone.high, "fvg.high"),
        )
        if overlap is not None:
            low, high = overlap
            freshness = "fresh" if ob_zone.freshness_status == "fresh" and fvg_zone.freshness_status == "fresh" else "partially_mitigated"
            if "mitigated" in (ob_zone.freshness_status, fvg_zone.freshness_status):
                freshness = "mitigated"
            candidates.append(
                _zone(
                    direction=ob_zone.direction if ob_zone.direction != NA else fvg_zone.direction,
                    zone_type="OB_FVG_OVERLAP",
                    creation_index=max(int(ob_zone.creation_index), int(fvg_zone.creation_index)),
                    low=low,
                    high=high,
                    freshness_status=freshness,
                    warnings=tuple(_unique_strings((*ob_zone.warnings, *fvg_zone.warnings))),
                    reason="OB and FVG overlap inside the displacement pullback area.",
                )
            )
    if ob_zone.is_present:
        candidates.append(ob_zone)
    if fvg_zone.is_present:
        candidates.append(fvg_zone)
    return tuple(candidates)


def _stop_price(data: PullbackZoneInput, sweep_candle: _Candle, ob_zone: PullbackZone, entry: Decimal) -> tuple[MaybeDecimal, MaybeDecimal]:
    tick_size = _tick_size(data.tick_size)
    atr_buffer: MaybeDecimal = NA
    if data.atr_15m != NA:
        atr = _decimal_from(data.atr_15m, "atr_15m")
        if atr > 0:
            atr_buffer = _quantize(atr * ATR_STOP_BUFFER_MULTIPLIER)

    if data.direction == "long":
        structure_edge = sweep_candle.low
        if ob_zone.is_present and ob_zone.wick_low != NA:
            structure_edge = min(structure_edge, _decimal_from(ob_zone.wick_low, "ob.wick_low"))
        stop = structure_edge - (atr_buffer if atr_buffer != NA else tick_size)
        return (_quantize(stop), atr_buffer) if stop < entry else (NA, atr_buffer)

    structure_edge = sweep_candle.high
    if ob_zone.is_present and ob_zone.wick_high != NA:
        structure_edge = max(structure_edge, _decimal_from(ob_zone.wick_high, "ob.wick_high"))
    stop = structure_edge + (atr_buffer if atr_buffer != NA else tick_size)
    return (_quantize(stop), atr_buffer) if stop > entry else (NA, atr_buffer)


def _targets(
    data: PullbackZoneInput,
    entry: Decimal,
    impulse_start: Decimal,
    impulse_end: Decimal,
) -> tuple[MaybeDecimal, MaybeDecimal, MaybeDecimal]:
    impulse = abs(impulse_end - impulse_start)
    if impulse <= 0:
        return NA, NA, NA

    if data.direction == "long":
        opposing = sorted(
            level
            for level in _extract_levels(data.user_resistance_levels) + _extract_levels(data.liquidity_above)
            if level > entry
        )
        fib_1272 = impulse_start + impulse * Decimal("1.272")
        tp1 = opposing[0] if opposing else fib_1272
        if tp1 <= entry:
            tp1 = fib_1272
        return _quantize(tp1), _quantize(impulse_start + impulse * Decimal("1.618")), _quantize(
            impulse_start + impulse * Decimal("2.0")
        )

    opposing = sorted(
        (
            level
            for level in _extract_levels(data.user_support_levels) + _extract_levels(data.liquidity_below)
            if level < entry
        ),
        reverse=True,
    )
    fib_1272 = impulse_start - impulse * Decimal("1.272")
    tp1 = opposing[0] if opposing else fib_1272
    if tp1 >= entry:
        tp1 = fib_1272
    return _quantize(tp1), _quantize(impulse_start - impulse * Decimal("1.618")), _quantize(
        impulse_start - impulse * Decimal("2.0")
    )


def _risk_reward(direction: TradeDirection, entry: Decimal, stop: Decimal, target: MaybeDecimal) -> MaybeDecimal:
    if target == NA:
        return NA
    target_decimal = _decimal_from(target, "target")
    risk = abs(entry - stop)
    if risk <= 0:
        return NA
    if direction == "long" and target_decimal <= entry:
        return NA
    if direction == "short" and target_decimal >= entry:
        return NA
    return _quantize(abs(target_decimal - entry) / risk)


def _fib_alignment_for_levels(
    direction: TradeDirection,
    sweep: Decimal,
    bos: Decimal,
    entry: Decimal | None,
    aggressive_toggle: bool,
    *,
    original_direction: AnyDirection | None = None,
) -> FibAlignmentResult:
    levels = _fib_levels(direction, sweep, bos)
    if entry is None:
        return FibAlignmentResult(
            direction=original_direction or direction,
            status="N/A",
            reason="Fib alignment is N/A because entry data is missing.",
            **_fib_result_levels(levels, aggressive_toggle),
        )

    retracement = _retracement(direction, sweep, bos, entry)
    max_retrace = Decimal("0.65") if aggressive_toggle else Decimal("0.618")
    min_retrace = Decimal("0.382")
    aggressive_drift_used = retracement > Decimal("0.618") and retracement <= Decimal("0.65")
    if min_retrace <= retracement <= max_retrace:
        return FibAlignmentResult(
            is_aligned=True,
            direction=original_direction or direction,
            retracement=_quantize(retracement),
            status="aligned_aggressive_0_65" if aggressive_drift_used else "aligned",
            aggressive_drift_used=aggressive_drift_used,
            reason="Entry is aligned with the preferred 0.382 to 0.618 fib zone."
            if not aggressive_drift_used
            else "Entry used aggressive drift to 0.65 with aggressive mode enabled.",
            **_fib_result_levels(levels, aggressive_toggle),
        )

    return FibAlignmentResult(
        direction=original_direction or direction,
        retracement=_quantize(retracement),
        status="failed_outside_preferred_zone",
        reason="Entry is outside the preferred fib retracement zone.",
        **_fib_result_levels(levels, aggressive_toggle),
    )


def _fib_result_levels(levels: Mapping[str, Decimal], aggressive_toggle: bool) -> dict[str, Decimal]:
    return {
        "preferred_low": levels["min"],
        "preferred_high": levels["max"],
        "fib_382": levels["0.382"],
        "fib_618": levels["0.618"],
        "fib_65": levels["0.65"],
        "fib_786": levels["0.786"],
        "fib_min": levels["min"],
        "fib_max": levels["max_65"] if aggressive_toggle else levels["max"],
    }


def _fib_levels(direction: TradeDirection, sweep: Decimal, bos: Decimal) -> dict[str, Decimal]:
    impulse = abs(bos - sweep)
    if direction == "long":
        fib_382 = bos - impulse * Decimal("0.382")
        fib_618 = bos - impulse * Decimal("0.618")
        fib_65 = bos - impulse * Decimal("0.65")
        fib_786 = bos - impulse * Decimal("0.786")
    else:
        fib_382 = bos + impulse * Decimal("0.382")
        fib_618 = bos + impulse * Decimal("0.618")
        fib_65 = bos + impulse * Decimal("0.65")
        fib_786 = bos + impulse * Decimal("0.786")
    return {
        "0.382": _quantize(fib_382),
        "0.618": _quantize(fib_618),
        "0.65": _quantize(fib_65),
        "0.786": _quantize(fib_786),
        "min": _quantize(min(fib_382, fib_618)),
        "max": _quantize(max(fib_382, fib_618)),
        "max_65": _quantize(max(fib_382, fib_65)),
    }


def _pullback_deeper_than_786(
    candles: Sequence[_Candle],
    direction: TradeDirection,
    bos_index: int,
    fib_levels: Mapping[str, Decimal],
    latest_price: MaybeDecimal,
) -> bool:
    sample = candles[bos_index + 1 :]
    fib_786 = fib_levels["0.786"]
    if direction == "long":
        if any(candle.low < fib_786 for candle in sample):
            return True
        return latest_price != NA and _decimal_from(latest_price, "latest_price") < fib_786
    if any(candle.high > fib_786 for candle in sample):
        return True
    return latest_price != NA and _decimal_from(latest_price, "latest_price") > fib_786


def _deepest_pullback_ratio(
    candles: Sequence[_Candle],
    direction: TradeDirection,
    bos_index: int,
    sweep: Decimal,
    bos: Decimal,
    latest_price: MaybeDecimal,
) -> Decimal | None:
    impulse = abs(bos - sweep)
    if impulse <= 0:
        return None

    sample = candles[bos_index + 1 :]
    if direction == "long":
        pullback_prices = [candle.low for candle in sample]
        if latest_price != NA:
            pullback_prices.append(_decimal_from(latest_price, "latest_price"))
        if not pullback_prices:
            return Decimal("0")
        ratio = (bos - min(pullback_prices)) / impulse
    else:
        pullback_prices = [candle.high for candle in sample]
        if latest_price != NA:
            pullback_prices.append(_decimal_from(latest_price, "latest_price"))
        if not pullback_prices:
            return Decimal("0")
        ratio = (max(pullback_prices) - bos) / impulse
    return _quantize(max(Decimal("0"), ratio))


def _pullback_ratio(
    direction: TradeDirection,
    sweep: Decimal,
    bos: Decimal,
    deepest_pullback: DecimalLike | None,
) -> Decimal | None:
    if deepest_pullback is None:
        return None
    value = _decimal_from(deepest_pullback, "deepest_pullback")
    if Decimal("0") <= value <= Decimal("1"):
        return value
    impulse = abs(bos - sweep)
    if impulse <= 0:
        return None
    if direction == "long":
        return (bos - value) / impulse
    return (value - bos) / impulse


def _retracement(direction: TradeDirection, sweep: Decimal, bos: Decimal, entry: Decimal) -> Decimal:
    impulse = abs(bos - sweep)
    if direction == "long":
        return (bos - entry) / impulse
    return (entry - bos) / impulse


def _entry_price(direction: TradeDirection, zone: PullbackZone) -> Decimal:
    if direction == "long":
        return _decimal_from(zone.entry_low, "zone.entry_low")
    return _decimal_from(zone.entry_high, "zone.entry_high")


def _overlap(low_a: Decimal, high_a: Decimal, low_b: Decimal, high_b: Decimal) -> tuple[Decimal, Decimal] | None:
    low = max(min(low_a, high_a), min(low_b, high_b))
    high = min(max(low_a, high_a), max(low_b, high_b))
    if low <= high:
        return low, high
    return None


def _zone(
    *,
    direction: TradeDirection | Literal["N/A"],
    zone_type: ZoneType,
    creation_index: int,
    low: Decimal,
    high: Decimal,
    freshness_status: str,
    body_low: Decimal | Literal["N/A"] = NA,
    body_high: Decimal | Literal["N/A"] = NA,
    wick_low: Decimal | Literal["N/A"] = NA,
    wick_high: Decimal | Literal["N/A"] = NA,
    fill_low: Decimal | Literal["N/A"] = NA,
    fill_high: Decimal | Literal["N/A"] = NA,
    warnings: Sequence[str] = (),
    reason: str = NA,
) -> PullbackZone:
    midpoint = (low + high) / Decimal("2")
    zone_warnings = list(warnings)
    if freshness_status == "mitigated":
        zone_warnings.append("Zone was already fully mitigated before entry.")
    return PullbackZone(
        is_present=True,
        zone_type=zone_type,
        direction=direction,
        creation_index=creation_index,
        low=_quantize(low),
        high=_quantize(high),
        midpoint=_quantize(midpoint),
        body_low=NA if body_low == NA else _quantize(body_low),
        body_high=NA if body_high == NA else _quantize(body_high),
        wick_low=NA if wick_low == NA else _quantize(wick_low),
        wick_high=NA if wick_high == NA else _quantize(wick_high),
        fill_low=NA if fill_low == NA else _quantize(fill_low),
        fill_high=NA if fill_high == NA else _quantize(fill_high),
        freshness_status=freshness_status,
        warnings=tuple(_unique_strings(zone_warnings)),
        reason=reason,
    )


def _fvg_freshness(
    candles: Sequence[_Candle],
    direction: TradeDirection,
    creation_index: int,
    low: Decimal,
    high: Decimal,
) -> str:
    midpoint = (low + high) / Decimal("2")
    sample = candles[creation_index + 1 :]
    if not sample:
        return "fresh"
    if direction == "long":
        if any(candle.low <= low for candle in sample):
            return "mitigated"
        if any(candle.low <= midpoint for candle in sample):
            return "partially_mitigated"
        return "fresh"
    if any(candle.high >= high for candle in sample):
        return "mitigated"
    if any(candle.high >= midpoint for candle in sample):
        return "partially_mitigated"
    return "fresh"


def _ob_freshness(
    candles: Sequence[_Candle],
    direction: TradeDirection,
    freshness_start_index: int,
    low: Decimal,
    high: Decimal,
) -> str:
    sample = candles[freshness_start_index:]
    if not sample:
        return "fresh"
    if direction == "long":
        if any(candle.low <= low for candle in sample):
            return "mitigated"
        if any(candle.low <= high for candle in sample):
            return "partially_mitigated"
        return "fresh"
    if any(candle.high >= high for candle in sample):
        return "mitigated"
    if any(candle.high >= low for candle in sample):
        return "partially_mitigated"
    return "fresh"


def _freshness_sort_key(zone: PullbackZone) -> tuple[int, int]:
    freshness_score = {"fresh": 2, "partially_mitigated": 1, "mitigated": 0}.get(zone.freshness_status, 0)
    creation_index = int(zone.creation_index) if zone.creation_index != NA else -1
    return freshness_score, creation_index


def _profile_confluence(data: PullbackZoneInput, low: Decimal, high: Decimal) -> tuple[str, ...]:
    values: list[str] = []
    for label, value in (("POC", data.poc), ("VAL", data.value_area_low), ("VAH", data.value_area_high)):
        if value == NA:
            continue
        decimal = _decimal_from(value, label.lower())
        if low <= decimal <= high:
            values.append(f"{label} inside pullback zone")
    return tuple(values)


def _normalize_candles(candles: Sequence[Any], label: str) -> tuple[tuple[_Candle, ...], tuple[str, ...]]:
    if isinstance(candles, (str, bytes)) or not isinstance(candles, Sequence):
        return (), (f"Malformed {label}: expected a sequence of candle objects.",)

    normalized: list[_Candle] = []
    errors: list[str] = []
    for index, candle in enumerate(candles):
        required: dict[str, Decimal] = {}
        for field in ("open", "high", "low", "close"):
            value = _get_field(candle, field)
            if _is_missing(value):
                errors.append(f"Missing required OHLC field {label}[{index}].{field}.")
                continue
            try:
                required[field] = _decimal_from(value, f"{label}[{index}].{field}")
            except ValueError as exc:
                errors.append(str(exc))

        if len(required) != 4:
            continue
        if required["high"] < required["low"]:
            errors.append(f"Malformed candle {label}[{index}]: high is lower than low.")
            continue
        if required["high"] < max(required["open"], required["close"]):
            errors.append(f"Malformed candle {label}[{index}]: high is below open or close.")
            continue
        if required["low"] > min(required["open"], required["close"]):
            errors.append(f"Malformed candle {label}[{index}]: low is above open or close.")
            continue

        volume_value = _get_field(candle, "volume")
        try:
            volume = NA if _is_missing(volume_value) else _decimal_from(volume_value, f"{label}[{index}].volume")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if volume != NA and volume < 0:
            errors.append(f"Malformed candle {label}[{index}].volume: volume cannot be negative.")
            continue

        normalized.append(
            _Candle(
                index=index,
                timestamp=_normalize_timestamp(_get_field(candle, "timestamp")),
                open=required["open"],
                high=required["high"],
                low=required["low"],
                close=required["close"],
                volume=volume,
            )
        )
    return tuple(normalized), tuple(errors)


def _extract_levels(value: Any | None) -> list[Decimal]:
    if _is_missing(value):
        return []
    if isinstance(value, Mapping):
        levels: list[Decimal] = []
        for key in ("levels", "prices", "support", "resistance", "below", "above"):
            if key in value:
                levels.extend(_extract_levels(value[key]))
        if not levels:
            for item in value.values():
                try:
                    levels.append(_decimal_from(item, "level"))
                except ValueError:
                    continue
        return levels
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        levels = []
        for item in value:
            levels.extend(_extract_levels(item))
        return levels
    try:
        return [_decimal_from(value, "level")]
    except ValueError:
        return []


def _trade_direction(direction: AnyDirection) -> TradeDirection | None:
    if direction in ("long", "bullish"):
        return "long"
    if direction in ("short", "bearish"):
        return "short"
    return None


def _index_or_none(value: MaybeInt) -> int | None:
    if value == NA:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _tick_size(value: MaybeDecimal) -> Decimal:
    if value == NA:
        return DEFAULT_TICK_SIZE
    tick_size = _decimal_from(value, "tick_size")
    return tick_size if tick_size > 0 else DEFAULT_TICK_SIZE


def _get_field(candle: Any, field: str) -> Any:
    if isinstance(candle, Mapping):
        return candle.get(field)
    return getattr(candle, field, None)


def _normalize_timestamp(value: Any) -> MaybeInt:
    if _is_missing(value):
        return NA
    try:
        return int(value)
    except (TypeError, ValueError):
        return NA


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Malformed pullback-zone data at {path}: invalid decimal {value!r}.") from exc
    if not decimal.is_finite():
        raise ValueError(f"Malformed pullback-zone data at {path}: invalid decimal {value!r}.")
    return decimal


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _display(value: Any) -> str:
    if value == NA or value is None:
        return NA
    if isinstance(value, Decimal):
        text = format(value.quantize(OUTPUT_QUANT), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in output:
            output.append(cleaned)
    return tuple(output)


__all__ = [
    "FibAlignmentResult",
    "PullbackZone",
    "PullbackZoneInput",
    "PullbackZoneResult",
    "analyze_pullback_zone",
    "calculate_fib_alignment",
]
