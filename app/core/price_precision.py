from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


def public_price_decimal_places(value: Decimal) -> int:
    magnitude = abs(value)
    if magnitude >= Decimal("10"):
        return 2
    if magnitude >= Decimal("1"):
        return 4
    if magnitude >= Decimal("0.01"):
        return 5
    return 8


def quantize_public_price(value: Decimal) -> Decimal:
    places = public_price_decimal_places(value)
    quantum = Decimal(1).scaleb(-places)
    return value.quantize(quantum, rounding=ROUND_HALF_UP)
