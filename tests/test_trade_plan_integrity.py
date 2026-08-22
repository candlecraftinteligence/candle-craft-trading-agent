from __future__ import annotations

from decimal import Decimal

import pytest

from app.analytics.pullback_zones import PullbackZoneInput, _targets
from app.core.price_precision import quantize_public_price
from app.core.trade_plan_integrity import validate_trade_plan


def _long_plan(**overrides: object):
    values: dict[str, object] = {
        "direction": "long",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "entry_reference": Decimal("100"),
        "stop_loss": Decimal("95"),
        "tp1": Decimal("110"),
        "tp2": Decimal("115"),
        "tp3": Decimal("120"),
        "entry_reference_type": "zone_low_limit",
    }
    values.update(overrides)
    return validate_trade_plan(**values)


def _short_plan(**overrides: object):
    values: dict[str, object] = {
        "direction": "short",
        "entry_low": Decimal("98"),
        "entry_high": Decimal("100"),
        "entry_reference": Decimal("100"),
        "stop_loss": Decimal("105"),
        "tp1": Decimal("90"),
        "tp2": Decimal("85"),
        "tp3": Decimal("80"),
        "entry_reference_type": "zone_high_limit",
    }
    values.update(overrides)
    return validate_trade_plan(**values)


def test_valid_long_plan_has_exact_tp2_rr() -> None:
    result = _long_plan()

    assert result.valid is True
    assert result.risk_distance == Decimal("5")
    assert result.reward_distance == Decimal("15")
    assert result.rr == Decimal("3")
    assert result.rr_target == "tp2"


def test_valid_short_plan_has_exact_tp2_rr() -> None:
    result = _short_plan()

    assert result.valid is True
    assert result.risk_distance == Decimal("5")
    assert result.reward_distance == Decimal("15")
    assert result.rr == Decimal("3")


@pytest.mark.parametrize(
    ("factory", "stop", "reason"),
    (
        (_long_plan, Decimal("101"), "stop_inside_entry_zone"),
        (_long_plan, Decimal("103"), "stop_wrong_side"),
        (_short_plan, Decimal("99"), "stop_inside_entry_zone"),
        (_short_plan, Decimal("97"), "stop_wrong_side"),
    ),
)
def test_wrong_side_or_inside_stop_is_rejected(factory, stop: Decimal, reason: str) -> None:
    result = factory(stop_loss=stop)

    assert result.valid is False
    assert result.reason == reason


def test_zero_risk_is_rejected_explicitly() -> None:
    result = _long_plan(entry_low=Decimal("100"), entry_high=Decimal("100"), stop_loss=Decimal("100"))

    assert result.valid is False
    assert result.reason == "zero_risk"


@pytest.mark.parametrize(
    ("factory", "overrides", "reason"),
    (
        (_long_plan, {"tp1": Decimal("101")}, "tp1_target_inside_entry_zone"),
        (_short_plan, {"tp1": Decimal("99")}, "tp1_target_inside_entry_zone"),
        (_long_plan, {"tp1": Decimal("99")}, "tp1_target_wrong_side"),
        (_short_plan, {"tp1": Decimal("103")}, "tp1_target_wrong_side"),
    ),
)
def test_inside_zone_or_wrong_side_target_is_rejected(factory, overrides: dict[str, Decimal], reason: str) -> None:
    result = factory(**overrides)

    assert result.valid is False
    assert result.reason == reason


@pytest.mark.parametrize(
    ("result", "reason"),
    (
        (_long_plan(tp1=Decimal("116")), "target_order_invalid"),
        (_short_plan(tp1=Decimal("84")), "target_order_invalid"),
        (_long_plan(tp2=Decimal("110")), "duplicate_targets"),
        (_short_plan(tp2=Decimal("90")), "duplicate_targets"),
    ),
)
def test_wrong_order_and_duplicate_targets_are_rejected(result, reason: str) -> None:
    assert result.valid is False
    assert result.reason == reason


def test_zero_and_negative_reward_are_rejected() -> None:
    zero = validate_trade_plan(
        direction="long",
        entry_low=Decimal("100"),
        entry_high=Decimal("100"),
        entry_reference=Decimal("100"),
        stop_loss=Decimal("95"),
        tp2=Decimal("100"),
        require_all_targets=False,
    )
    negative = validate_trade_plan(
        direction="short",
        entry_low=Decimal("100"),
        entry_high=Decimal("100"),
        entry_reference=Decimal("100"),
        stop_loss=Decimal("105"),
        tp2=Decimal("101"),
        require_all_targets=False,
    )

    assert zero.valid is False
    assert zero.reason == "zero_reward"
    assert negative.valid is False
    assert negative.reason == "negative_reward"


@pytest.mark.parametrize(
    ("tp2", "expected"),
    (
        (Decimal("112.5"), True),
        (Decimal("112.49999999"), False),
        (Decimal("112.50000001"), True),
    ),
)
def test_minimum_rr_boundary_uses_unrounded_decimal(tp2: Decimal, expected: bool) -> None:
    result = _long_plan(
        tp1=Decimal("110"),
        tp2=tp2,
        tp3=Decimal("115"),
        minimum_rr=Decimal("2.5"),
    )

    assert result.valid is expected
    if not expected:
        assert result.reason == "rr_below_minimum"


@pytest.mark.parametrize("value", ("bad", "NaN", "Infinity", "-Infinity"))
def test_malformed_or_non_finite_numeric_input_fails_safely(value: str) -> None:
    result = _long_plan(tp2=value)

    assert result.valid is False
    assert result.reason.startswith(("malformed_numeric", "non_finite_numeric"))


def test_favorable_edge_policy_is_explicit_and_quantified() -> None:
    low_edge = _long_plan(entry_high=Decimal("103"), stop_loss=Decimal("90"), tp1=Decimal("110"), tp2=Decimal("125"), tp3=Decimal("130"))
    midpoint = _long_plan(entry_high=Decimal("103"), entry_reference=Decimal("101.5"), stop_loss=Decimal("90"), tp1=Decimal("110"), tp2=Decimal("125"), tp3=Decimal("130"), entry_reference_type="zone_midpoint")
    high_edge = _long_plan(entry_high=Decimal("103"), entry_reference=Decimal("103"), stop_loss=Decimal("90"), tp1=Decimal("110"), tp2=Decimal("125"), tp3=Decimal("130"), entry_reference_type="zone_high")

    assert low_edge.rr == Decimal("2.5")
    assert midpoint.rr == Decimal("23.5") / Decimal("11.5")
    assert high_edge.rr == Decimal("22") / Decimal("13")
    assert low_edge.rr > midpoint.rr > high_edge.rr
    assert low_edge.entry_reference_type == "zone_low_limit"


def test_publishable_rounding_cannot_preserve_a_raw_only_threshold_pass() -> None:
    raw = validate_trade_plan(
        direction="long",
        entry_low=Decimal("100.006"),
        entry_high=Decimal("100.006"),
        entry_reference=Decimal("100.006"),
        stop_loss=Decimal("99.011"),
        tp1=Decimal("101.50"),
        tp2=Decimal("102.4935"),
        tp3=Decimal("103.50"),
        minimum_rr=Decimal("2.5"),
    )
    published = validate_trade_plan(
        direction="long",
        entry_low=quantize_public_price(Decimal("100.006")),
        entry_high=quantize_public_price(Decimal("100.006")),
        entry_reference=quantize_public_price(Decimal("100.006")),
        stop_loss=quantize_public_price(Decimal("99.011")),
        tp1=quantize_public_price(Decimal("101.50")),
        tp2=quantize_public_price(Decimal("102.4935")),
        tp3=quantize_public_price(Decimal("103.50")),
        minimum_rr=Decimal("2.5"),
    )

    assert raw.valid is True
    assert raw.rr == Decimal("2.5")
    assert published.valid is False
    assert published.reason == "rr_below_minimum"
    assert published.rr == Decimal("2.48")


def _target_input(*, resistance: Decimal | None = None) -> PullbackZoneInput:
    return PullbackZoneInput(
        symbol="BTCUSDT",
        direction="long",
        execution_timeframe="15m",
        confirmation_timeframe="5m",
        candles_15m=(),
        candles_5m=(),
        sweep_candle_index=0,
        bos_choch_candle_index=1,
        user_resistance_levels=() if resistance is None else (resistance,),
    )


def test_tp1_inside_entry_reproduction_is_rejected_without_repair() -> None:
    tp1, tp2, tp3 = _targets(_target_input(resistance=Decimal("101")), Decimal("100"), Decimal("90"), Decimal("110"))
    result = _long_plan(entry_high=Decimal("102"), stop_loss=Decimal("89"), tp1=tp1, tp2=tp2, tp3=tp3)

    assert tp1 == Decimal("101.00000000")
    assert result.valid is False
    assert result.reason == "tp1_target_inside_entry_zone"


def test_tp1_beyond_tp2_reproduction_is_rejected_without_reordering() -> None:
    tp1, tp2, tp3 = _targets(_target_input(resistance=Decimal("130")), Decimal("100"), Decimal("90"), Decimal("110"))
    result = _long_plan(stop_loss=Decimal("89"), tp1=tp1, tp2=tp2, tp3=tp3)

    assert (tp1, tp2, tp3) == (Decimal("130.00000000"), Decimal("122.36000000"), Decimal("130.00000000"))
    assert result.valid is False
    assert result.reason == "duplicate_targets"


def test_extreme_wick_expands_fib_targets_but_does_not_bypass_geometry() -> None:
    normal = _targets(_target_input(), Decimal("100"), Decimal("90"), Decimal("110"))
    outlier = _targets(_target_input(), Decimal("100"), Decimal("90"), Decimal("210"))
    result = _long_plan(stop_loss=Decimal("89"), tp1=outlier[0], tp2=outlier[1], tp3=outlier[2])

    assert outlier[1] > normal[1]
    assert outlier[1] == Decimal("284.16000000")
    assert result.valid is True
