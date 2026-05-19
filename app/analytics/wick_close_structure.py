from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.data.dtos import NA, MaybeDecimal

OUTPUT_QUANT = Decimal("0.00000001")
HARD_INVALIDATION_DEPTH = Decimal("0.786")
STRONG_RECLAIM_DEPTH = Decimal("0.65")

TradeDirection = Literal["long", "short"]


class AcceptanceStatus(str, Enum):
    WICK_SWEEP_RECLAIM = "WICK_SWEEP_RECLAIM"
    BODY_ACCEPTANCE_FAILURE = "BODY_ACCEPTANCE_FAILURE"
    DEEP_RECLAIM_VALID = "DEEP_RECLAIM_VALID"
    STRUCTURAL_BREAKDOWN = "STRUCTURAL_BREAKDOWN"
    CLEAN_PULLBACK = "CLEAN_PULLBACK"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"


@dataclass(frozen=True)
class _CandleView:
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal


class WickCloseStructure(BaseModel):
    wick_depth_ratio: MaybeDecimal = NA
    close_depth_ratio: MaybeDecimal = NA
    body_acceptance_ratio: MaybeDecimal = NA
    max_wick_breach: MaybeDecimal = NA
    max_body_breach: MaybeDecimal = NA
    reclaim_detected: bool | Literal["N/A"] = NA
    reclaim_strength: Literal["strong", "weak", "N/A"] = NA
    candles_below_fib_zone: int | Literal["N/A"] = NA
    acceptance_status: str = AcceptanceStatus.DATA_INCOMPLETE.value
    structural_reclaim_status: Literal["intact", "broken", "N/A"] = NA

    model_config = ConfigDict(frozen=True)

    @field_validator(
        "wick_depth_ratio",
        "close_depth_ratio",
        "body_acceptance_ratio",
        "max_wick_breach",
        "max_body_breach",
        mode="before",
    )
    @classmethod
    def _normalize_decimal(cls, value: Any) -> Any:
        if _is_missing(value):
            return NA
        return _quantize(_decimal_from(value, "wick_close_structure"))

    @field_validator("acceptance_status", mode="before")
    @classmethod
    def _normalize_acceptance_status(cls, value: Any) -> str:
        if isinstance(value, AcceptanceStatus):
            return value.value
        text = _display(value)
        return text if text != NA else AcceptanceStatus.DATA_INCOMPLETE.value


def analyze_wick_close_structure(
    candles: Sequence[Any],
    *,
    direction: TradeDirection,
    bos_index: int,
    sweep_price: Decimal,
    bos_price: Decimal,
    fib_786: Decimal,
    latest_price: MaybeDecimal = NA,
) -> WickCloseStructure:
    impulse = abs(bos_price - sweep_price)
    if impulse <= 0 or bos_index < 0 or bos_index >= len(candles):
        return WickCloseStructure()

    sample = _post_bos_sample(candles, bos_index, latest_price)
    if not sample:
        return WickCloseStructure(
            wick_depth_ratio=Decimal("0"),
            close_depth_ratio=Decimal("0"),
            body_acceptance_ratio=Decimal("0"),
            max_wick_breach=Decimal("0"),
            max_body_breach=Decimal("0"),
            reclaim_detected=False,
            reclaim_strength=NA,
            candles_below_fib_zone=0,
            acceptance_status=AcceptanceStatus.CLEAN_PULLBACK.value,
            structural_reclaim_status="intact",
        )

    if direction == "long":
        wick_depth = _depth_ratio(bos_price - min(candle.low for candle in sample), impulse)
        close_depth = _depth_ratio(bos_price - min(candle.close for candle in sample), impulse)
        close_breach_count = sum(1 for candle in sample if candle.close < fib_786)
        structural_break = any(candle.close < sweep_price for candle in sample)
        reclaim_detected = _long_reclaim_detected(sample, fib_786)
    else:
        wick_depth = _depth_ratio(max(candle.high for candle in sample) - bos_price, impulse)
        close_depth = _depth_ratio(max(candle.close for candle in sample) - bos_price, impulse)
        close_breach_count = sum(1 for candle in sample if candle.close > fib_786)
        structural_break = any(candle.close > sweep_price for candle in sample)
        reclaim_detected = _short_reclaim_detected(sample, fib_786)

    max_wick_breach = _breach_ratio(wick_depth)
    max_body_breach = _breach_ratio(close_depth)
    persistent_acceptance = close_breach_count >= 2
    structural_status: Literal["intact", "broken"] = (
        "broken" if structural_break or persistent_acceptance else "intact"
    )
    reclaim_strength = _reclaim_strength(reclaim_detected, close_depth)
    acceptance_status = _acceptance_status(
        wick_depth=wick_depth,
        close_depth=close_depth,
        reclaim_detected=reclaim_detected,
        reclaim_strength=reclaim_strength,
        structural_status=structural_status,
    )

    return WickCloseStructure(
        wick_depth_ratio=wick_depth,
        close_depth_ratio=close_depth,
        body_acceptance_ratio=close_depth,
        max_wick_breach=max_wick_breach,
        max_body_breach=max_body_breach,
        reclaim_detected=reclaim_detected,
        reclaim_strength=reclaim_strength,
        candles_below_fib_zone=close_breach_count,
        acceptance_status=acceptance_status.value,
        structural_reclaim_status=structural_status,
    )


def wick_close_fields(structure: WickCloseStructure) -> dict[str, Any]:
    return {
        "wick_close_structure": structure,
        "wick_depth_ratio": structure.wick_depth_ratio,
        "close_depth_ratio": structure.close_depth_ratio,
        "body_acceptance_ratio": structure.body_acceptance_ratio,
        "max_wick_breach": structure.max_wick_breach,
        "max_body_breach": structure.max_body_breach,
        "reclaim_detected": structure.reclaim_detected,
        "reclaim_strength": structure.reclaim_strength,
        "candles_below_fib_zone": structure.candles_below_fib_zone,
        "acceptance_status": structure.acceptance_status,
        "structural_reclaim_status": structure.structural_reclaim_status,
    }


def _post_bos_sample(candles: Sequence[Any], bos_index: int, latest_price: MaybeDecimal) -> tuple[_CandleView, ...]:
    sample: list[_CandleView] = []
    for candle in candles[bos_index + 1 :]:
        sample.append(_candle_view(candle))
    if latest_price != NA:
        latest = _decimal_from(latest_price, "latest_price")
        if not sample or sample[-1].close != latest:
            sample.append(_CandleView(open=latest, high=latest, low=latest, close=latest))
    return tuple(sample)


def _candle_view(candle: Any) -> _CandleView:
    open_price = _decimal_from(_get_field(candle, "open"), "candle.open")
    high = _decimal_from(_get_field(candle, "high"), "candle.high")
    low = _decimal_from(_get_field(candle, "low"), "candle.low")
    close = _decimal_from(_get_field(candle, "close"), "candle.close")
    return _CandleView(open=open_price, high=high, low=low, close=close)


def _long_reclaim_detected(sample: Sequence[_CandleView], fib_786: Decimal) -> bool:
    breach_seen = False
    for candle in sample:
        wick_breach = candle.low < fib_786
        close_inside = candle.close >= fib_786
        if wick_breach and close_inside:
            return True
        if breach_seen and close_inside:
            return True
        if wick_breach or candle.close < fib_786:
            breach_seen = True
    return False


def _short_reclaim_detected(sample: Sequence[_CandleView], fib_786: Decimal) -> bool:
    breach_seen = False
    for candle in sample:
        wick_breach = candle.high > fib_786
        close_inside = candle.close <= fib_786
        if wick_breach and close_inside:
            return True
        if breach_seen and close_inside:
            return True
        if wick_breach or candle.close > fib_786:
            breach_seen = True
    return False


def _reclaim_strength(reclaim_detected: bool, close_depth: Decimal) -> Literal["strong", "weak", "N/A"]:
    if not reclaim_detected:
        return NA
    return "strong" if close_depth <= STRONG_RECLAIM_DEPTH else "weak"


def _acceptance_status(
    *,
    wick_depth: Decimal,
    close_depth: Decimal,
    reclaim_detected: bool,
    reclaim_strength: str,
    structural_status: str,
) -> AcceptanceStatus:
    if structural_status == "broken":
        return AcceptanceStatus.STRUCTURAL_BREAKDOWN
    if close_depth > HARD_INVALIDATION_DEPTH:
        return AcceptanceStatus.BODY_ACCEPTANCE_FAILURE
    if wick_depth > HARD_INVALIDATION_DEPTH and reclaim_detected:
        if reclaim_strength == "strong":
            return AcceptanceStatus.DEEP_RECLAIM_VALID
        return AcceptanceStatus.WICK_SWEEP_RECLAIM
    if wick_depth > HARD_INVALIDATION_DEPTH:
        return AcceptanceStatus.WICK_SWEEP_RECLAIM
    return AcceptanceStatus.CLEAN_PULLBACK


def _depth_ratio(value: Decimal, impulse: Decimal) -> Decimal:
    return _quantize(max(Decimal("0"), value / impulse))


def _breach_ratio(depth: Decimal) -> Decimal:
    return _quantize(max(Decimal("0"), depth - HARD_INVALIDATION_DEPTH))


def _get_field(candle: Any, field: str) -> Any:
    if isinstance(candle, Mapping):
        return candle.get(field)
    return getattr(candle, field, None)


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Malformed wick-close data at {path}: invalid decimal {value!r}.") from exc
    if not decimal.is_finite():
        raise ValueError(f"Malformed wick-close data at {path}: invalid decimal {value!r}.")
    return decimal


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if isinstance(value, Enum):
        return str(value.value)
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


__all__ = [
    "AcceptanceStatus",
    "WickCloseStructure",
    "analyze_wick_close_structure",
    "wick_close_fields",
]
