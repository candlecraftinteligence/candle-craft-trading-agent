"""Independent 0.618 / 0.750 / 0.786 evaluation and liquidity-protected stops.

A wick that reaches a level is a price milestone. A directional body that
closes back through that level is the structural confirmation. Neither is
inferred from the other.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

DISCRETE_FIB_RATIOS: tuple[Decimal, ...] = (
    Decimal("0.618"),
    Decimal("0.750"),
    Decimal("0.786"),
)


@dataclass(frozen=True)
class DiscreteFibLevel:
    ratio: Decimal
    price: Decimal
    wick_touched: bool
    body_confirmed: bool
    zone_intersection: bool
    execution_eligible: bool
    reason: str

    def summary(self) -> str:
        return (
            f"{self.ratio}:{self.reason}:wick={int(self.wick_touched)}:"
            f"body={int(self.body_confirmed)}:zone={int(self.zone_intersection)}"
        )


def level_price(direction: str, sweep: Decimal, bos: Decimal, ratio: Decimal) -> Decimal:
    if ratio not in DISCRETE_FIB_RATIOS:
        raise ValueError("unsupported_discrete_fib_ratio")
    if direction not in {"long", "short"}:
        raise ValueError("unsupported_direction")
    impulse = abs(bos - sweep)
    if impulse <= 0 or not all(value.is_finite() and value > 0 for value in (sweep, bos)):
        raise ValueError("invalid_impulse")
    if direction == "long" and bos <= sweep:
        raise ValueError("invalid_impulse_direction")
    if direction == "short" and bos >= sweep:
        raise ValueError("invalid_impulse_direction")
    if direction == "long":
        return bos - (impulse * ratio)
    return bos + (impulse * ratio)


def evaluate_discrete_levels(
    *,
    direction: str,
    sweep: Decimal,
    bos: Decimal,
    zones: Sequence[tuple[str, Decimal, Decimal, str]],
    candles: Sequence[Any],
) -> tuple[DiscreteFibLevel, ...]:
    """Evaluate each ratio on its own. Shallower ratios are listed first."""

    levels: list[DiscreteFibLevel] = []
    for ratio in DISCRETE_FIB_RATIOS:
        price = level_price(direction, sweep, bos, ratio)
        wick_touched, body_confirmed = _interaction(direction, price, candles)
        zone_intersection = _intersects_fresh_zone(direction, price, zones)
        if not zone_intersection:
            reason = "no_fresh_zone_intersection"
            eligible = False
        elif not wick_touched and not body_confirmed:
            reason = "untouched"
            eligible = False
        elif not body_confirmed:
            reason = "wick_touch_only"
            eligible = False
        else:
            reason = "body_confirmed"
            eligible = True
        levels.append(
            DiscreteFibLevel(
                ratio=ratio,
                price=price,
                wick_touched=wick_touched,
                body_confirmed=body_confirmed,
                zone_intersection=zone_intersection,
                execution_eligible=eligible,
                reason=reason,
            )
        )
    return tuple(levels)


def protected_stop_price(
    *,
    direction: str,
    entry: Decimal,
    structure_extreme: Decimal,
    liquidity_levels: Sequence[Decimal] = (),
    atr_buffer: Decimal | None = None,
    spread: Decimal | None = None,
    tick_size: Decimal,
    local_search: Decimal | None = None,
) -> Decimal | None:
    """Place the stop beyond structure and nearby liquidity past that edge.

    Only liquidity within ``local_search`` of the structural extreme is
    included. Distant levels stay diagnostic. The buffer is the larger of the
    ATR buffer, the spread, and one tick. It is not increased to manufacture
    reward distance.
    """

    if direction not in {"long", "short"} or tick_size <= 0:
        return None
    if not all(value.is_finite() and value > 0 for value in (entry, structure_extreme)):
        return None
    search = local_search
    if search is None and atr_buffer is not None and atr_buffer > 0:
        # The stop buffer is 0.10 ATR. Local search uses one full ATR.
        search = atr_buffer * 10
    anchors = [structure_extreme]
    for level in liquidity_levels:
        if not level.is_finite() or level <= 0:
            continue
        if search is not None and abs(level - structure_extreme) > search:
            continue
        if direction == "long" and level <= structure_extreme:
            anchors.append(level)
        elif direction == "short" and level >= structure_extreme:
            anchors.append(level)
    extreme = min(anchors) if direction == "long" else max(anchors)
    buffer = tick_size
    if atr_buffer is not None and atr_buffer > 0:
        buffer = max(buffer, atr_buffer)
    if spread is not None and spread > 0:
        buffer = max(buffer, spread)
    stop = extreme - buffer if direction == "long" else extreme + buffer
    if direction == "long" and stop < entry:
        return stop
    if direction == "short" and stop > entry:
        return stop
    return None


def _intersects_fresh_zone(
    direction: str,
    price: Decimal,
    zones: Sequence[tuple[str, Decimal, Decimal, str]],
) -> bool:
    del direction
    for _zone_type, low, high, freshness in zones:
        if freshness != "fresh":
            continue
        if min(low, high) <= price <= max(low, high):
            return True
    return False


def _interaction(direction: str, level: Decimal, candles: Sequence[Any]) -> tuple[bool, bool]:
    wick_touched = False
    body_confirmed = False
    for candle in candles:
        low = candle.low
        high = candle.high
        opened = candle.open
        closed = candle.close
        reached = low <= level <= high
        wick_touched = wick_touched or reached
        if not wick_touched:
            continue
        if direction == "long" and closed > opened and closed >= level and min(opened, closed) <= level:
            body_confirmed = True
        elif direction == "short" and closed < opened and closed <= level and max(opened, closed) >= level:
            body_confirmed = True
    return wick_touched, body_confirmed
