"""Versioned, discrete Fibonacci entry policy. No market-data acquisition here."""
from __future__ import annotations

from collections.abc import Sequence
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

    model_config = ConfigDict(frozen=True)


def target_price(direction: str, start: Decimal, end: Decimal, ratio: Decimal) -> Decimal:
    if ratio not in ALLOWED_TARGETS:
        raise ValueError("unsupported_selective_fib_target")
    if direction not in {"long", "short"} or not all(v.is_finite() and v > 0 for v in (start, end)):
        raise ValueError("invalid_impulse")
    if (direction == "long" and end <= start) or (direction == "short" and end >= start):
        raise ValueError("invalid_impulse_direction")
    return end - (end - start) * ratio


def acceptance_sequence(candles: Sequence[Any], direction: str, boundary: Decimal, *, reclaim: bool) -> str:
    """Two real closed directional bodies; reclaim additionally needs an earlier touch.

    The second body must close beyond the first body's entire candle. A ticker
    price and a wick alone can never provide this evidence.
    """
    touched = not reclaim
    prior = None
    for candle in candles:
        if direction == "long":
            touched = touched or candle.low <= boundary
            accepted = candle.close > candle.open >= boundary
            follow = prior is not None and candle.close > prior.high
        else:
            touched = touched or candle.high >= boundary
            accepted = candle.close < candle.open <= boundary
            follow = prior is not None and candle.close < prior.low
        if touched and accepted and follow:
            return "strong_reclaim" if reclaim else "structural_acceptance"
        prior = candle if touched and accepted else None
    return "weak_or_missing_reclaim" if reclaim else "missing_structural_acceptance"


def select_target(*, direction: str, start: Decimal, end: Decimal, zones: Sequence[Any],
                  tick_size: Decimal | None, precision_source: str, timeframe: str,
                  candles: Sequence[Any], sweep_index: int, bos_index: int,
                  structure_confirmed: bool, canonical_confirmation: bool) -> tuple[int | None, SelectiveFibEvidence]:
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
            if zone.freshness_status == "mitigated":
                reasons.append("zone_mitigated")
            if abs(price - raw) > tolerance:
                reasons.append("precision_exceeds_policy_tolerance")
            if price.as_tuple().exponent < -8:
                reasons.append("precision_exceeds_trade_plan_contract")
            status = "existing_structural_gates"
            if ratio >= Decimal("0.750"):
                status = acceptance_sequence(candles[bos_index + 1:], direction, raw, reclaim=ratio == Decimal("0.786"))
                if not structure_confirmed:
                    reasons.append("deep_structure_unconfirmed")
                if not canonical_confirmation:
                    reasons.append("deep_canonical_confirmation_missing")
                if zone.freshness_status != "fresh":
                    reasons.append("deep_zone_not_fresh")
                if status not in {"structural_acceptance", "strong_reclaim"}:
                    reasons.append(status)
            if ratio == Decimal("0.786"):
                # The current lifecycle invalidates at 0.786 while the stop is
                # beyond the sweep. Re-entry at this boundary has no approved
                # lifecycle/stop contract, even after a strong reclaim.
                reasons.append("deep_0786_watch_only_lifecycle_stop_contract")
            rejected.extend(f"{zone.zone_type}:{ratio}:{reason}" for reason in reasons)
            evidence = SelectiveFibEvidence(**base, selected_ratio=ratio, calculated_price=raw,
                execution_price=price, intersection_low=price, intersection_high=price,
                original_zone_low=zone.low, original_zone_high=zone.high,
                structural_confluence=(zone.zone_type, zone.freshness_status), reclaim_status=status,
                execution_eligible=not reasons, rejection_reasons=tuple(reasons))
            # Structural overlap and freshness rank first, then shallower risk.
            # Distance to current price is deliberately absent.
            score = (not reasons, zone.zone_type == "OB_FVG_OVERLAP", zone.freshness_status == "fresh", -ratio)
            eligible.append((score, index, evidence))
    if not eligible:
        return None, SelectiveFibEvidence(**base, rejection_reasons=("no_approved_target_intersection",),
                                         candidate_rejections=tuple(rejected))
    _, index, evidence = max(eligible, key=lambda item: item[0])
    return index, evidence.model_copy(update={"candidate_rejections": tuple(rejected)})
