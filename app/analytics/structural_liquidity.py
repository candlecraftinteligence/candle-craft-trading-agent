"""Bounded closed-candle liquidity proxies; no claim about actual stop orders."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import Any


@dataclass(frozen=True)
class LiquidityLevel:
    price: Decimal
    side: str
    timeframe: str
    index: int
    timestamp: Any
    swept: bool = False


def confirmed_levels(candles: Sequence[Any], *, timeframe: str, window: int = 128) -> tuple[LiquidityLevel, ...]:
    """Two closed neighbours on either side confirm a swing, including equal tops.

    Equal prices are liquidity proxies, never proof of exchange stop orders.
    Caller owns candle source identity, freshness and decision-time filtering.
    """
    if not 5 <= window <= 512:
        raise ValueError("liquidity_window_out_of_bounds")
    sample = candles[-window:]
    def field(candle, key):
        return candle.get(key) if isinstance(candle, Mapping) else getattr(candle, key)
    levels = []
    for index in range(2, len(sample)-2):
        for key, side in (("low", "below"), ("high", "above")):
            price = Decimal(str(field(sample[index], key)))
            neighbours = [Decimal(str(field(sample[j], key))) for j in (index-2, index-1, index+1, index+2)]
            if not price.is_finite() or price <= 0 or not all(p.is_finite() and p > 0 for p in neighbours):
                raise ValueError("invalid_liquidity_candle")
            pivot = (all(price <= p for p in neighbours) and any(price < p for p in neighbours)
                     if side == "below" else all(price >= p for p in neighbours) and any(price > p for p in neighbours))
            if pivot:
                swept = any(Decimal(str(field(later, key))) < price if side == "below"
                            else Decimal(str(field(later, key))) > price for later in sample[index+1:])
                levels.append(LiquidityLevel(price, side, timeframe, index+max(0, len(candles)-window),
                                             field(sample[index], "timestamp"), swept))
    return tuple(levels)


@dataclass(frozen=True)
class ProtectedStop:
    price: Decimal
    structural_invalidation: Decimal
    liquidity_boundary: Decimal
    buffer: Decimal
    included_levels: tuple[LiquidityLevel, ...]
    distant_levels_ignored: int


def protected_stop(*, direction: str, entry: Decimal, invalidation: Decimal, atr: Decimal,
                   tick_size: Decimal, spread: Decimal | None, levels: Sequence[LiquidityLevel]) -> ProtectedStop:
    values = (entry, invalidation, atr, tick_size)
    if direction not in {"long", "short"} or not all(p.is_finite() and p > 0 for p in values):
        raise ValueError("unjustified_structural_stop")
    if spread is not None and (not spread.is_finite() or spread < 0):
        raise ValueError("unverified_spread")
    buffer = max(tick_size, atr*Decimal("0.10"), spread or Decimal(0))
    side = "below" if direction == "long" else "above"
    adverse = [level for level in levels if level.side == side and
               (level.price <= invalidation if direction == "long" else level.price >= invalidation)]
    # One observed ATR bounds the local search. Distant liquidity never forces
    # an arbitrary wider stop; final TP2 economics still reject poor asymmetry.
    included = tuple(level for level in adverse if abs(level.price-invalidation) <= atr)
    boundary = (min if direction == "long" else max)([invalidation, *(level.price for level in included)])
    raw = boundary-buffer if direction == "long" else boundary+buffer
    rounding = ROUND_FLOOR if direction == "long" else ROUND_CEILING
    price = (raw/tick_size).to_integral_value(rounding=rounding)*tick_size
    if price <= 0 or (price >= entry if direction == "long" else price <= entry):
        raise ValueError("unjustified_structural_stop")
    return ProtectedStop(price, invalidation, boundary, buffer, included, len(adverse)-len(included))
