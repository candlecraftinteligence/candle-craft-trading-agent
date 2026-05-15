from __future__ import annotations

from decimal import Decimal

from app.analytics.pullback_zones import PullbackZoneInput, analyze_pullback_zone, calculate_fib_alignment
from app.data.dtos import NA


def _base_candles() -> list[dict[str, Decimal | int]]:
    return [
        {
            "timestamp": index,
            "open": Decimal("110"),
            "high": Decimal("112"),
            "low": Decimal("108"),
            "close": Decimal("110"),
            "volume": Decimal("100"),
        }
        for index in range(10)
    ]


def _bullish_valid_candles() -> list[dict[str, Decimal | int]]:
    candles = _base_candles()
    candles[5] = {
        "timestamp": 5,
        "open": Decimal("108"),
        "high": Decimal("112"),
        "low": Decimal("100"),
        "close": Decimal("106"),
        "volume": Decimal("100"),
    }
    candles[7] = {
        "timestamp": 7,
        "open": Decimal("117.2"),
        "high": Decimal("117.2"),
        "low": Decimal("114"),
        "close": Decimal("115"),
        "volume": Decimal("100"),
    }
    candles[9] = {
        "timestamp": 9,
        "open": Decimal("120"),
        "high": Decimal("145"),
        "low": Decimal("118"),
        "close": Decimal("143"),
        "volume": Decimal("100"),
    }
    return candles


def _bearish_valid_candles() -> list[dict[str, Decimal | int]]:
    candles = _base_candles()
    candles[5] = {
        "timestamp": 5,
        "open": Decimal("142"),
        "high": Decimal("150"),
        "low": Decimal("140"),
        "close": Decimal("144"),
        "volume": Decimal("100"),
    }
    candles[7] = {
        "timestamp": 7,
        "open": Decimal("132.8"),
        "high": Decimal("136"),
        "low": Decimal("132.8"),
        "close": Decimal("135"),
        "volume": Decimal("100"),
    }
    candles[9] = {
        "timestamp": 9,
        "open": Decimal("130"),
        "high": Decimal("132"),
        "low": Decimal("105"),
        "close": Decimal("107"),
        "volume": Decimal("100"),
    }
    return candles


def _input(
    candles: list[dict[str, Decimal | int]],
    *,
    direction: str = "long",
    minimum_rr: Decimal = Decimal("2.5"),
    atr_15m: Decimal = Decimal("1"),
    poc: Decimal | str = NA,
) -> PullbackZoneInput:
    return PullbackZoneInput(
        symbol="BTCUSDT",
        direction=direction,
        execution_timeframe="15m",
        confirmation_timeframe="5m",
        candles_15m=candles,
        candles_5m=candles,
        sweep_candle_index=5,
        bos_choch_candle_index=9,
        latest_price=candles[-1]["close"],
        atr_15m=atr_15m,
        tick_size=Decimal("0.01"),
        minimum_rr=minimum_rr,
        poc=poc,
    )


def test_bullish_fvg_detection() -> None:
    result = analyze_pullback_zone(_input(_bullish_valid_candles()))

    assert result.fvg_zone.is_present is True
    assert result.fvg_zone.low == Decimal("117.20000000")
    assert result.fvg_zone.high == Decimal("118.00000000")


def test_bearish_fvg_detection() -> None:
    result = analyze_pullback_zone(_input(_bearish_valid_candles(), direction="short"))

    assert result.fvg_zone.is_present is True
    assert result.fvg_zone.low == Decimal("132.00000000")
    assert result.fvg_zone.high == Decimal("132.80000000")


def test_bullish_ob_detection() -> None:
    result = analyze_pullback_zone(_input(_bullish_valid_candles()))

    assert result.ob_zone.is_present is True
    assert result.ob_zone.body_low == Decimal("115.00000000")
    assert result.ob_zone.body_high == Decimal("117.20000000")


def test_bearish_ob_detection() -> None:
    result = analyze_pullback_zone(_input(_bearish_valid_candles(), direction="short"))

    assert result.ob_zone.is_present is True
    assert result.ob_zone.body_low == Decimal("132.80000000")
    assert result.ob_zone.body_high == Decimal("135.00000000")


def test_fib_levels_calculated_correctly() -> None:
    result = calculate_fib_alignment(
        direction="long",
        sweep_price=Decimal("100"),
        bos_price=Decimal("145"),
        entry_price=Decimal("117.2"),
    )

    assert result.fib_382 == Decimal("127.81000000")
    assert result.fib_618 == Decimal("117.19000000")
    assert result.fib_65 == Decimal("115.75000000")
    assert result.fib_786 == Decimal("109.63000000")


def test_ob_fvg_overlap_inside_fib_zone_becomes_valid_pullback_zone() -> None:
    result = analyze_pullback_zone(_input(_bullish_valid_candles(), minimum_rr=Decimal("3.0")))

    assert result.valid is True
    assert result.selected_zone_type == "OB_FVG_OVERLAP"
    assert result.entry == Decimal("117.20000000")
    assert result.fib_alignment.is_aligned is True


def test_no_ob_fvg_returns_no_ob_or_fvg_zone() -> None:
    candles = _base_candles()
    candles[5]["low"] = Decimal("100")
    candles[9]["high"] = Decimal("145")
    candles[9]["close"] = Decimal("143")

    result = analyze_pullback_zone(_input(candles))

    assert result.valid is False
    assert result.first_failed_gate == "no_ob_or_fvg_zone"
    assert result.ob_zone.is_present is False
    assert result.fvg_zone.is_present is False


def test_missing_displacement_impulse_returns_missing_displacement_impulse() -> None:
    result = analyze_pullback_zone(
        _input(_bullish_valid_candles()).model_copy(update={"bos_choch_candle_index": NA})
    )

    assert result.valid is False
    assert result.first_failed_gate == "missing_displacement_impulse"


def test_unusable_indices_with_candles_return_no_displacement_candle() -> None:
    result = analyze_pullback_zone(
        _input(_bullish_valid_candles()).model_copy(update={"bos_choch_candle_index": 5})
    )

    assert result.valid is False
    assert result.first_failed_gate == "no_displacement_candle"


def test_confirmation_timeframe_indices_drive_pullback_calculation() -> None:
    result = analyze_pullback_zone(
        _input(_bullish_valid_candles(), minimum_rr=Decimal("3.0")).model_copy(
            update={
                "calculation_timeframe": "5m",
                "candles_15m": _base_candles(),
                "candles_5m": _bullish_valid_candles(),
                "sweep_candle_index": 5,
                "bos_choch_candle_index": 9,
            }
        )
    )

    assert result.valid is True
    assert result.calculation_timeframe == "5m"
    assert result.sweep_candle_index == 5
    assert result.bos_choch_candle_index == 9
    assert result.displacement_start_index == 5
    assert result.displacement_end_index == 9


def test_pullback_deeper_than_0786_rejects() -> None:
    candles = _bullish_valid_candles()
    candles.append(
        {
            "timestamp": 10,
            "open": Decimal("120"),
            "high": Decimal("121"),
            "low": Decimal("108"),
            "close": Decimal("116"),
            "volume": Decimal("100"),
        }
    )

    result = analyze_pullback_zone(_input(candles, minimum_rr=Decimal("3.0")))

    assert result.valid is False
    assert result.first_failed_gate == "pullback_too_deep"


def test_rr_below_25_rejects() -> None:
    result = analyze_pullback_zone(_input(_bullish_valid_candles(), atr_15m=Decimal("90")))

    assert result.valid is False
    assert result.first_failed_gate == "rr_below_minimum"


def test_challenge_rr_below_30_rejects() -> None:
    result = analyze_pullback_zone(_input(_bullish_valid_candles(), minimum_rr=Decimal("3.0"), atr_15m=Decimal("20")))

    assert result.valid is False
    assert result.first_failed_gate == "rr_below_minimum"


def test_valid_zone_produces_entry_stop_targets_and_rr() -> None:
    result = analyze_pullback_zone(_input(_bullish_valid_candles(), minimum_rr=Decimal("3.0")))

    assert result.valid is True
    assert result.entry != NA
    assert result.stop != NA
    assert result.tp1 != NA
    assert result.tp2 != NA
    assert result.rr_to_tp2 >= Decimal("3.0")


def test_poc_is_confluence_only() -> None:
    result = analyze_pullback_zone(_input(_bullish_valid_candles(), minimum_rr=Decimal("3.0"), poc=Decimal("117.2")))

    assert result.valid is True
    assert result.selected_zone.confluence == ("POC inside pullback zone",)


def test_poc_alone_does_not_create_setup() -> None:
    candles = _base_candles()
    candles[5]["low"] = Decimal("100")
    candles[9]["high"] = Decimal("145")
    candles[9]["close"] = Decimal("143")

    result = analyze_pullback_zone(_input(candles, poc=Decimal("117.2")))

    assert result.valid is False
    assert result.first_failed_gate == "no_ob_or_fvg_zone"
