"""Versioned, discrete Fibonacci entry policy. No market-data acquisition here."""
from __future__ import annotations

from collections.abc import Callable, Sequence
from decimal import Decimal, ROUND_HALF_EVEN
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.data.dtos import NA, MaybeDecimal

LEGACY_VERSION = "liquidity-grab-v1"
SELECTIVE_VERSION = "selective-fib-mtf-v2"
StrategyVersion = Literal["liquidity-grab-v1", "selective-fib-mtf-v2"]
ALLOWED_TARGETS = (Decimal("0.618"), Decimal("0.750"), Decimal("0.786"))
# At most half a verified tick AND one basis point of the source impulse.
MAX_IMPULSE_TOLERANCE = Decimal("0.0001")


class SelectiveFibEvidence(BaseModel):
    strategy_version: str = LEGACY_VERSION
    selected_ratio: MaybeDecimal = NA
    impulse_start: MaybeDecimal = NA
    impulse_end: MaybeDecimal = NA
    sweep_index: int | None = None
    bos_index: int | None = None
    sweep_timestamp: int | str = NA
    bos_timestamp: int | str = NA
    calculated_price: MaybeDecimal = NA
    execution_price: MaybeDecimal = NA
    intersection_low: MaybeDecimal = NA
    intersection_high: MaybeDecimal = NA
    original_zone_low: MaybeDecimal = NA
    original_zone_high: MaybeDecimal = NA
    direction: str = NA
    timeframe: str = NA
    structural_confluence: tuple[str, ...] = ()
    precision_source: str = NA
    tick_size: MaybeDecimal = NA
    price_tolerance: MaybeDecimal = NA
    reclaim_status: str = NA
    execution_eligible: bool = False
    rejection_reasons: tuple[str, ...] = ()
    candidate_rejections: tuple[str, ...] = ()
    level_touched: bool = False
    touch_index: int | None = None
    confirmation_index: int | None = None
    candidate_evidence: tuple[dict[str, Any], ...] = ()
    zone_freshness_before_touch: str = NA
    zone_freshness_at_decision: str = NA

    model_config = ConfigDict(frozen=True)


def target_price(direction: str, start: Decimal, end: Decimal, ratio: Decimal) -> Decimal:
    if ratio not in ALLOWED_TARGETS:
        raise ValueError("unsupported_selective_fib_target")
    if direction not in {"long", "short"} or not all(v.is_finite() and v > 0 for v in (start, end)):
        raise ValueError("invalid_impulse")
    if (direction == "long" and end <= start) or (direction == "short" and end >= start):
        raise ValueError("invalid_impulse_direction")
    return end - (end - start) * ratio


def level_reaction(candles: Sequence[Any], direction: str, boundary: Decimal, *, offset: int):
    """Wick contact is a price event; a closed directional body is confirmation.

    The caller supplies only closed, causally eligible execution candles. A
    reaction may close on the contact candle, whose close follows its extrema.
    This is never evidence of an exchange fill or a user execution.
    """
    touch = None
    for index, candle in enumerate(candles, offset):
        if touch is None and candle.low <= boundary <= candle.high:
            touch = index
        if touch is not None:
            accepted = (candle.close > boundary and candle.close > candle.open if direction == "long"
                        else candle.close < boundary and candle.close < candle.open)
            if accepted:
                return "closed_reaction", touch, index
    return ("entry_zone_not_touched" if touch is None else "entry_confirmation_missing"), touch, None


def select_target(*, direction: str, start: Decimal, end: Decimal, zones: Sequence[Any],
                  tick_size: Decimal | None, precision_source: str, timeframe: str,
                  candles: Sequence[Any], sweep_index: int, bos_index: int,
                  structure_confirmed: bool, canonical_confirmation: bool,
                  freshness_before: Callable[[Any, int], str] | None = None) -> tuple[int | None, SelectiveFibEvidence]:
    base = dict(strategy_version=SELECTIVE_VERSION, direction=direction, timeframe=timeframe,
                impulse_start=start, impulse_end=end, sweep_index=sweep_index, bos_index=bos_index,
                sweep_timestamp=candles[sweep_index].timestamp, bos_timestamp=candles[bos_index].timestamp,
                precision_source=precision_source)
    if tick_size is None or not tick_size.is_finite() or tick_size <= 0 or precision_source == NA:
        return None, SelectiveFibEvidence(**base, rejection_reasons=("instrument_precision_unverified",))
    tolerance = min(tick_size / 2, abs(end - start) * MAX_IMPULSE_TOLERANCE)
    base.update(tick_size=tick_size, price_tolerance=tolerance)
    eligible = []
    rejected = []
    evaluated = []
    for index, zone in enumerate(zones):
        if not zone.is_present or zone.direction != direction or zone.freshness_status not in {"fresh", "partially_mitigated", "mitigated"}:
            rejected.append(f"{zone.zone_type}:invalid_or_mitigated_zone")
            continue
        for ratio in ALLOWED_TARGETS:
            raw = target_price(direction, start, end, ratio)
            price = (raw / tick_size).to_integral_value(rounding=ROUND_HALF_EVEN) * tick_size
            # Both the mathematical target and the actual tick price must be
            # inside the original structural zone. Never expand a zone.
            if not (zone.low <= raw <= zone.high and zone.low <= price <= zone.high):
                rejected.append(f"{zone.zone_type}:{ratio}:no_intersection")
                continue
            reasons = []
            if abs(price - raw) > tolerance:
                reasons.append("precision_exceeds_policy_tolerance")
            if price.as_tuple().exponent < -8:
                reasons.append("precision_exceeds_trade_plan_contract")
            status, touch_index, confirmation_index = level_reaction(
                candles[bos_index + 1:], direction, price, offset=bos_index + 1)
            freshness = (freshness_before(zone, touch_index if touch_index is not None else len(candles))
                         if freshness_before is not None else zone.freshness_status)
            if freshness == "mitigated":
                reasons.append("zone_mitigated")
            if not structure_confirmed:
                reasons.append("structure_unconfirmed")
            if not canonical_confirmation:
                reasons.append("canonical_confirmation_missing")
            if ratio >= Decimal("0.750") and freshness != "fresh":
                reasons.append("deep_zone_not_fresh")
            if status != "closed_reaction":
                reasons.append(status)
            rejected.extend(f"{zone.zone_type}:{ratio}:{reason}" for reason in reasons)
            evaluated.append({"ratio": str(ratio), "price": str(raw), "zone": zone.zone_type,
                              "touched": touch_index is not None, "touch_index": touch_index,
                              "confirmation_index": confirmation_index, "status": status,
                              "eligible": not reasons, "freshness_before_touch": freshness,
                              "rejections": tuple(reasons)})
            evidence = SelectiveFibEvidence(**base, selected_ratio=ratio, calculated_price=raw,
                execution_price=price, intersection_low=price, intersection_high=price,
                original_zone_low=zone.low, original_zone_high=zone.high,
                structural_confluence=(zone.zone_type, freshness), reclaim_status=status,
                zone_freshness_before_touch=freshness, zone_freshness_at_decision=zone.freshness_status,
                level_touched=touch_index is not None, touch_index=touch_index,
                confirmation_index=confirmation_index,
                execution_eligible=not reasons, rejection_reasons=tuple(reasons))
            # Structural overlap and freshness rank first, then shallower risk.
            # Distance to current price is deliberately absent.
            score = (not reasons, zone.zone_type == "OB_FVG_OVERLAP", freshness == "fresh", -ratio)
            eligible.append((score, index, evidence))
    if not eligible:
        return None, SelectiveFibEvidence(**base, rejection_reasons=("no_approved_target_intersection",),
                                         candidate_rejections=tuple(rejected), candidate_evidence=tuple(evaluated))
    _, index, evidence = max(eligible, key=lambda item: item[0])
    return index, evidence.model_copy(update={"candidate_rejections": tuple(rejected), "candidate_evidence": tuple(evaluated)})
