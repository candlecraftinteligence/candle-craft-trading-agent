from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.analytics.discrete_fib_levels import (
    evaluate_discrete_levels,
    level_price,
    protected_stop_price,
)
from app.analytics.pullback_zones import analyze_pullback_zone
from tests.test_pullback_zones import _bullish_valid_candles, _input


def _candle(open_: str, high: str, low: str, close: str) -> SimpleNamespace:
    return SimpleNamespace(
        open=Decimal(open_),
        high=Decimal(high),
        low=Decimal(low),
        close=Decimal(close),
    )


def test_three_ratios_are_independent_prices() -> None:
    prices = {
        ratio: level_price("long", Decimal("100"), Decimal("200"), ratio)
        for ratio in (Decimal("0.618"), Decimal("0.750"), Decimal("0.786"))
    }
    assert prices[Decimal("0.618")] == Decimal("138.200")
    assert prices[Decimal("0.750")] == Decimal("125.000")
    assert prices[Decimal("0.786")] == Decimal("121.400")
    assert len(set(prices.values())) == 3


def test_wick_touch_is_not_body_confirmation() -> None:
    level = level_price("long", Decimal("100"), Decimal("200"), Decimal("0.750"))
    wick_only = (_candle("140", "141", "124", "140"),)
    confirmed = (
        SimpleNamespace(
            open=level - Decimal("1"),
            high=level + Decimal("2"),
            low=level - Decimal("1"),
            close=level + Decimal("1"),
        ),
    )
    wick = evaluate_discrete_levels(
        direction="long",
        sweep=Decimal("100"),
        bos=Decimal("200"),
        zones=(("OB", Decimal("120"), Decimal("130"), "fresh"),),
        candles=wick_only,
    )
    body = evaluate_discrete_levels(
        direction="long",
        sweep=Decimal("100"),
        bos=Decimal("200"),
        zones=(("OB", Decimal("120"), Decimal("130"), "fresh"),),
        candles=confirmed,
    )
    wick_750 = next(item for item in wick if item.ratio == Decimal("0.750"))
    body_750 = next(item for item in body if item.ratio == Decimal("0.750"))
    assert wick_750.wick_touched is True
    assert wick_750.body_confirmed is False
    assert wick_750.execution_eligible is False
    assert wick_750.reason == "wick_touch_only"
    assert body_750.body_confirmed is True
    assert body_750.execution_eligible is True
    assert all(item.execution_eligible is False for item in body if item.ratio != Decimal("0.750"))


def test_protected_stop_clears_liquidity_without_extra_padding() -> None:
    common = dict(
        direction="long",
        entry=Decimal("100"),
        structure_extreme=Decimal("90"),
        atr_buffer=Decimal("1"),
        spread=Decimal("0.2"),
        tick_size=Decimal("0.1"),
        local_search=Decimal("10"),
    )
    plain = protected_stop_price(**common)
    deeper = protected_stop_price(**common, liquidity_levels=(Decimal("85"), Decimal("95")))
    distant = protected_stop_price(**common, liquidity_levels=(Decimal("50"), Decimal("85")))
    assert plain == Decimal("89")
    assert deeper == Decimal("84")
    assert distant == Decimal("84")
    assert protected_stop_price(**common, liquidity_levels=()) == plain


def test_short_stop_stays_above_equal_highs() -> None:
    stop = protected_stop_price(
        direction="short",
        entry=Decimal("100"),
        structure_extreme=Decimal("110"),
        liquidity_levels=(Decimal("112"), Decimal("105")),
        atr_buffer=Decimal("1"),
        spread=None,
        tick_size=Decimal("0.5"),
    )
    assert stop == Decimal("113")


def test_invalid_ratio_and_impulse_fail_closed() -> None:
    with pytest.raises(ValueError):
        level_price("long", Decimal("100"), Decimal("200"), Decimal("0.500"))
    with pytest.raises(ValueError):
        level_price("long", Decimal("200"), Decimal("100"), Decimal("0.618"))


def test_valid_pullback_reports_all_three_levels() -> None:
    result = analyze_pullback_zone(_input(_bullish_valid_candles()))
    assert result.fib_618 != result.fib_750 != result.fib_786
    reported = " ".join(result.discrete_fib_levels)
    assert "0.618:" in reported
    assert "0.750:" in reported
    assert "0.786:" in reported
