from __future__ import annotations

from decimal import Decimal

import pytest

from app.analytics.derivatives_enrichment import enrich_derivatives
from app.data.dtos import NA
from app.strategies.liquidity_grab_pullback import (
    DEFAULT_CONFIRMATION_TIMEFRAME,
    LiquidityGrabEngine,
    LiquidityGrabInput,
    LiquidityGrabMode,
    analyze_liquidity_grab_pullback,
    calculate_fib_alignment,
    confirm_momentum,
    detect_fair_value_gap,
    detect_liquidity_sweep,
    detect_order_block,
    detect_structure_shift,
)


def _base_candles(count: int = 45, *, volume: Decimal = Decimal("100")) -> list[dict[str, Decimal | int]]:
    return [
        {
            "timestamp": index,
            "open": Decimal("100"),
            "high": Decimal("105"),
            "low": Decimal("95"),
            "close": Decimal("100"),
            "volume": volume,
        }
        for index in range(count)
    ]


def _bullish_sweep_candles(*, sweep_low: Decimal, sweep_close: Decimal) -> list[dict[str, Decimal | int]]:
    candles = _base_candles()
    candles[20]["low"] = Decimal("90")
    candles[-1]["low"] = sweep_low
    candles[-1]["close"] = sweep_close
    return candles


def _bearish_sweep_candles(*, sweep_high: Decimal, sweep_close: Decimal) -> list[dict[str, Decimal | int]]:
    candles = _base_candles()
    candles[20]["high"] = Decimal("110")
    candles[-1]["high"] = sweep_high
    candles[-1]["close"] = sweep_close
    return candles


def _full_bullish_setup_candles(*, with_fvg: bool = True, sweep_volume: Decimal = Decimal("200")) -> list[dict[str, Decimal | int]]:
    candles = _base_candles(36)
    candles[20]["low"] = Decimal("90")
    candles[24]["high"] = Decimal("110")
    candles[30]["low"] = Decimal("85")
    candles[30]["close"] = Decimal("91")
    candles[30]["volume"] = sweep_volume
    candles[33]["open"] = Decimal("99")
    candles[33]["close"] = Decimal("97")
    candles[33]["low"] = Decimal("95")
    candles[33]["high"] = Decimal("100") if with_fvg else Decimal("103")
    candles[35]["open"] = Decimal("104")
    candles[35]["high"] = Decimal("114")
    candles[35]["low"] = Decimal("101")
    candles[35]["close"] = Decimal("112")
    return candles


def _mtf_execution_sweep_candles() -> list[dict[str, Decimal | int]]:
    candles = _full_bullish_setup_candles()
    for index, candle in enumerate(candles):
        candle["timestamp"] = index * 15
    return candles


def _mtf_confirmation_bos_candles() -> list[dict[str, Decimal | int]]:
    candles = _base_candles(100)
    for index, candle in enumerate(candles):
        candle["timestamp"] = index * 5

    candles[60]["low"] = Decimal("90")
    candles[84]["high"] = Decimal("110")
    candles[90]["open"] = Decimal("89")
    candles[90]["high"] = Decimal("100")
    candles[90]["low"] = Decimal("85")
    candles[90]["close"] = Decimal("91")
    candles[91]["open"] = Decimal("99")
    candles[91]["high"] = Decimal("100")
    candles[91]["low"] = Decimal("95")
    candles[91]["close"] = Decimal("97")
    candles[92]["open"] = Decimal("104")
    candles[92]["high"] = Decimal("114")
    candles[92]["low"] = Decimal("101")
    candles[92]["close"] = Decimal("112")
    candles[92]["volume"] = Decimal("300")
    return candles


def _missing_ob_fvg_candles() -> list[dict[str, Decimal | int]]:
    candles = _base_candles(45)
    candles[20]["low"] = Decimal("90")
    candles[24]["high"] = Decimal("110")
    candles[30]["open"] = Decimal("89")
    candles[30]["low"] = Decimal("85")
    candles[30]["close"] = Decimal("91")
    candles[35]["open"] = Decimal("104")
    candles[35]["high"] = Decimal("114")
    candles[35]["low"] = Decimal("100")
    candles[35]["close"] = Decimal("112")
    return candles


def _fib_failure_candles() -> list[dict[str, Decimal | int]]:
    candles = _full_bullish_setup_candles()
    candles.append(
        {
            "timestamp": 36,
            "open": Decimal("112"),
            "high": Decimal("113"),
            "low": Decimal("87"),
            "close": Decimal("87"),
            "volume": Decimal("100"),
        }
    )
    return candles


def _rr_failure_candles() -> list[dict[str, Decimal | int]]:
    candles = _full_bullish_setup_candles()
    candles.append(
        {
            "timestamp": 36,
            "open": Decimal("112"),
            "high": Decimal("400"),
            "low": Decimal("112"),
            "close": Decimal("120"),
            "volume": Decimal("100"),
        }
    )
    return candles


def _challenge_low_trust_candles() -> list[dict[str, Decimal | int]]:
    candles = _base_candles(36)
    candles[20]["low"] = Decimal("90")
    candles[24]["high"] = Decimal("110")
    candles[30]["low"] = Decimal("85")
    candles[30]["close"] = Decimal("91")
    candles[33]["open"] = Decimal("108")
    candles[33]["close"] = Decimal("106")
    candles[33]["low"] = Decimal("105")
    candles[33]["high"] = Decimal("108")
    candles[35]["open"] = Decimal("112")
    candles[35]["high"] = Decimal("145")
    candles[35]["low"] = Decimal("109")
    candles[35]["close"] = Decimal("143")
    return candles


def _trend_candles(count: int = 30) -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    for index in range(count):
        price = Decimal(100 + index)
        candles.append(
            {
                "timestamp": index,
                "open": price,
                "high": price + Decimal("2"),
                "low": price - Decimal("2"),
                "close": price + Decimal("1"),
                "volume": Decimal("100"),
            }
        )
    return candles


def test_bullish_sweep_requires_wick_at_least_035_atr_and_close_back_inside() -> None:
    valid = detect_liquidity_sweep(_bullish_sweep_candles(sweep_low=Decimal("86.5"), sweep_close=Decimal("91")))
    weak_wick = detect_liquidity_sweep(_bullish_sweep_candles(sweep_low=Decimal("86.6"), sweep_close=Decimal("91")))
    no_reclaim = detect_liquidity_sweep(_bullish_sweep_candles(sweep_low=Decimal("86.5"), sweep_close=Decimal("89.5")))

    assert valid.is_present is True
    assert valid.direction == "bullish"
    assert weak_wick.is_present is False
    assert no_reclaim.is_present is False


def test_bearish_sweep_requires_wick_at_least_035_atr_and_close_back_inside() -> None:
    valid = detect_liquidity_sweep(_bearish_sweep_candles(sweep_high=Decimal("113.5"), sweep_close=Decimal("109")))
    weak_wick = detect_liquidity_sweep(_bearish_sweep_candles(sweep_high=Decimal("113.4"), sweep_close=Decimal("109")))
    no_reclaim = detect_liquidity_sweep(_bearish_sweep_candles(sweep_high=Decimal("113.5"), sweep_close=Decimal("110.5")))

    assert valid.is_present is True
    assert valid.direction == "bearish"
    assert weak_wick.is_present is False
    assert no_reclaim.is_present is False


def test_bullish_bos_requires_close_above_prior_swing_high() -> None:
    candles = _base_candles()
    candles[20]["high"] = Decimal("110")
    candles[-1]["high"] = Decimal("112")
    candles[-1]["close"] = Decimal("111")

    no_close_break = _base_candles()
    no_close_break[20]["high"] = Decimal("110")
    no_close_break[-1]["high"] = Decimal("112")
    no_close_break[-1]["close"] = Decimal("109")

    assert detect_structure_shift(candles, direction="bullish").is_present is True
    assert detect_structure_shift(no_close_break, direction="bullish").is_present is False


def test_bearish_bos_requires_close_below_prior_swing_low() -> None:
    candles = _base_candles()
    candles[20]["low"] = Decimal("90")
    candles[-1]["low"] = Decimal("88")
    candles[-1]["close"] = Decimal("89")

    no_close_break = _base_candles()
    no_close_break[20]["low"] = Decimal("90")
    no_close_break[-1]["low"] = Decimal("88")
    no_close_break[-1]["close"] = Decimal("91")

    assert detect_structure_shift(candles, direction="bearish").is_present is True
    assert detect_structure_shift(no_close_break, direction="bearish").is_present is False


def test_bullish_fvg_detection() -> None:
    candles = [
        {"timestamp": 0, "open": Decimal("99"), "high": Decimal("100"), "low": Decimal("98"), "close": Decimal("99"), "volume": Decimal("1")},
        {"timestamp": 1, "open": Decimal("100"), "high": Decimal("101"), "low": Decimal("99"), "close": Decimal("100"), "volume": Decimal("1")},
        {"timestamp": 2, "open": Decimal("102"), "high": Decimal("103"), "low": Decimal("101"), "close": Decimal("102"), "volume": Decimal("1")},
    ]

    result = detect_fair_value_gap(candles, "bullish")

    assert result.is_present is True
    assert result.low == Decimal("100.00000000")
    assert result.high == Decimal("101.00000000")


def test_bearish_fvg_detection() -> None:
    candles = [
        {"timestamp": 0, "open": Decimal("101"), "high": Decimal("102"), "low": Decimal("100"), "close": Decimal("101"), "volume": Decimal("1")},
        {"timestamp": 1, "open": Decimal("100"), "high": Decimal("101"), "low": Decimal("99"), "close": Decimal("100"), "volume": Decimal("1")},
        {"timestamp": 2, "open": Decimal("98"), "high": Decimal("99"), "low": Decimal("97"), "close": Decimal("98"), "volume": Decimal("1")},
    ]

    result = detect_fair_value_gap(candles, "bearish")

    assert result.is_present is True
    assert result.low == Decimal("97.00000000")
    assert result.high == Decimal("100.00000000")


def test_bullish_ob_detection() -> None:
    candles = _base_candles(5)
    candles[2]["open"] = Decimal("105")
    candles[2]["close"] = Decimal("100")
    candles[2]["high"] = Decimal("106")
    candles[2]["low"] = Decimal("99")
    candles[3]["close"] = Decimal("104")

    result = detect_order_block(candles, "bullish", bos_index=4)

    assert result.is_present is True
    assert result.low == Decimal("100.00000000")
    assert result.high == Decimal("105.00000000")
    assert result.midpoint == Decimal("102.50000000")


def test_bearish_ob_detection() -> None:
    candles = _base_candles(5)
    candles[2]["open"] = Decimal("100")
    candles[2]["close"] = Decimal("105")
    candles[2]["high"] = Decimal("106")
    candles[2]["low"] = Decimal("99")
    candles[3]["close"] = Decimal("96")

    result = detect_order_block(candles, "bearish", bos_index=4)

    assert result.is_present is True
    assert result.low == Decimal("100.00000000")
    assert result.high == Decimal("105.00000000")
    assert result.midpoint == Decimal("102.50000000")


def test_fib_alignment_valid_in_preferred_zone() -> None:
    result = calculate_fib_alignment(
        direction="bullish",
        sweep_price=Decimal("90"),
        bos_price=Decimal("110"),
        entry_price=Decimal("100"),
    )

    assert result.is_aligned is True
    assert result.retracement == Decimal("0.50000000")


def test_fib_drift_to_065_only_allowed_in_aggressive_mode() -> None:
    conservative = calculate_fib_alignment(
        direction="bullish",
        sweep_price=Decimal("90"),
        bos_price=Decimal("110"),
        entry_price=Decimal("97"),
    )
    aggressive = calculate_fib_alignment(
        direction="bullish",
        sweep_price=Decimal("90"),
        bos_price=Decimal("110"),
        entry_price=Decimal("97"),
        aggressive_toggle=True,
    )

    assert conservative.is_aligned is False
    assert aggressive.is_aligned is True
    assert aggressive.aggressive_drift_used is True


def test_reject_pullback_deeper_than_0786() -> None:
    result = calculate_fib_alignment(
        direction="bullish",
        sweep_price=Decimal("90"),
        bos_price=Decimal("110"),
        entry_price=Decimal("100"),
        deepest_pullback=Decimal("0.80"),
    )

    assert result.is_aligned is False
    assert result.rejected_deeper_than_786 is True


def test_volume_confirmation_requires_15x_20_bar_average() -> None:
    candles = _base_candles(25)
    candles[20]["volume"] = Decimal("150")
    confirmed = confirm_momentum(candles, 20)
    candles[20]["volume"] = Decimal("149")
    rejected = confirm_momentum(candles, 20)

    assert confirmed.is_confirmed is True
    assert confirmed.volume_status == "confirmed"
    assert rejected.is_confirmed is False
    assert rejected.volume_status == "not_confirmed"


def test_reject_if_rr_to_tp2_below_25() -> None:
    result = analyze_liquidity_grab_pullback(
        {
            "symbol": "SOLUSDT",
            "mode": "swing",
            "candles_15m": _rr_failure_candles(),
            "candles_5m": _full_bullish_setup_candles(),
            "candles_2d": _trend_candles(),
            "user_resistance_levels": (Decimal("112"), Decimal("120")),
        }
    )

    assert result.swing.is_valid is False
    assert result.swing.first_failed_gate == "rr_below_minimum"


def test_liquidity_diagnostics_explain_failed_sweep() -> None:
    result = analyze_liquidity_grab_pullback(
        {"symbol": "BTCUSDT", "candles_15m": _base_candles(), "candles_5m": _base_candles()}
    )

    assert result.swing.gates_failed[0] == "missing_confirmed_sweep"
    assert "Sweep: failed" in result.swing.strategy_diagnostics
    assert "No candle swept prior swing by at least 0.35 ATR and closed back inside." in result.swing.strategy_diagnostics
    assert result.swing.structure_shift_diagnostics == "N/A because sweep failed."


def test_liquidity_diagnostics_explain_failed_bos_choch() -> None:
    result = analyze_liquidity_grab_pullback(
        {
            "symbol": "BTCUSDT",
            "candles_15m": _bullish_sweep_candles(sweep_low=Decimal("86.5"), sweep_close=Decimal("91")),
            "candles_5m": _base_candles(),
        }
    )

    assert result.swing.gates_failed[0] == "missing_confirmation_structure_shift"
    assert "Sweep: passed" in result.swing.strategy_diagnostics
    assert "15m BOS/CHoCH: failed" in result.swing.strategy_diagnostics
    assert "No 15m BOS/CHoCH close beyond the required LTF swing." in result.swing.structure_shift_diagnostics


def test_liquidity_diagnostics_explain_missing_ob_fvg() -> None:
    result = analyze_liquidity_grab_pullback(
        {"symbol": "BTCUSDT", "confirmation_timeframe": "5m", "candles_15m": _full_bullish_setup_candles(), "candles_5m": _missing_ob_fvg_candles()}
    )

    assert result.swing.gates_failed[0] == "no_ob_or_fvg_zone"
    assert result.swing.ob_fvg_diagnostics == "failed: OB missing; FVG missing."
    assert "OB/FVG: failed" in result.swing.strategy_diagnostics
    assert result.swing.fib_diagnostics == "N/A because OB/FVG failed."


def test_liquidity_diagnostics_explain_failed_fib_alignment() -> None:
    result = analyze_liquidity_grab_pullback(
        {"symbol": "BTCUSDT", "confirmation_timeframe": "5m", "candles_15m": _full_bullish_setup_candles(), "candles_5m": _fib_failure_candles()}
    )

    assert result.swing.gates_failed[0] == "body_acceptance_failure"
    assert "Pullback Zone: failed" in result.swing.strategy_diagnostics
    assert "Candle body closed beyond 0.786 before entry." in result.swing.fib_diagnostics


def test_liquidity_diagnostics_explain_failed_rr() -> None:
    result = analyze_liquidity_grab_pullback(
        {
            "symbol": "SOLUSDT",
            "mode": "swing",
            "candles_15m": _rr_failure_candles(),
            "candles_5m": _full_bullish_setup_candles(),
            "candles_2d": _trend_candles(),
            "user_resistance_levels": (Decimal("112"), Decimal("120")),
        }
    )

    assert result.swing.gates_failed[0] == "rr_below_minimum"
    assert "RR: failed" in result.swing.strategy_diagnostics
    assert "is below 2.5" in result.swing.rr_diagnostics


def test_challenge_rejects_if_trust_meter_below_85() -> None:
    result = analyze_liquidity_grab_pullback(
        {
            "symbol": "SOLUSDT",
            "mode": LiquidityGrabMode.challenge,
            "candles_15m": _challenge_low_trust_candles(),
            "candles_5m": _challenge_low_trust_candles(),
        }
    )

    assert result.challenge.trust_meter.percentage == 80
    assert result.challenge.is_valid is False
    assert any(violation.code == "challenge_trust_below_85" for violation in result.challenge.gate_result.violations)


def test_challenge_rejects_if_rr_below_30() -> None:
    result = analyze_liquidity_grab_pullback(
        {
            "symbol": "SOLUSDT",
            "mode": LiquidityGrabMode.challenge,
            "candles_15m": _full_bullish_setup_candles(),
            "candles_5m": _full_bullish_setup_candles(),
            "candles_2d": _trend_candles(),
            "user_resistance_levels": (Decimal("112"), Decimal("120")),
        }
    )

    assert result.challenge.is_valid is False
    assert result.challenge.first_failed_gate == "rr_below_minimum"
    assert any(violation.code == "rr_below_minimum" for violation in result.challenge.gate_result.violations)


def test_challenge_failure_output_exact_message() -> None:
    result = LiquidityGrabEngine().analyze({"symbol": "BTCUSDT", "mode": "challenge"})

    assert result.formatted_output.challenge_setup == "No valid challenge setup."


def test_invalid_swing_and_scalp_outputs_remain_exact_messages() -> None:
    result = LiquidityGrabEngine().analyze({"symbol": "BTCUSDT"})

    assert result.formatted_output.swing_setup == "No valid swing setup."
    assert result.formatted_output.scalp_setup == "No valid scalp setup."


def test_default_confirmation_timeframe_is_m15() -> None:
    assert DEFAULT_CONFIRMATION_TIMEFRAME == "15m"
    assert LiquidityGrabInput(symbol="BTCUSDT").confirmation_timeframe == "15m"


def test_rejects_setup_when_ltf_confirmation_is_missing() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "challenge",
            "candles_2d": _trend_candles(),
            "candles_12h": _trend_candles(),
            "htf_2d_context_source": "synthetic_from_1d",
        }
    )

    assert result.challenge.is_valid is False
    assert result.challenge.first_failed_gate == "no_execution_candles"
    assert result.challenge.ltf_confirmation_status == NA
    assert result.challenge.htf_2d_context_source == "synthetic_from_1d"


def test_explicit_m5_confirmation_override_rejects_when_m5_candles_are_missing() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "swing",
            "confirmation_timeframe": "5m",
            "candles_15m": _mtf_execution_sweep_candles(),
            "candles_2d": _trend_candles(),
        }
    )

    assert result.swing.is_valid is False
    assert result.swing.first_failed_gate == "missing_confirmation_candles"
    assert result.swing.confirmation_timeframe == "5m"
    assert result.swing.ltf_confirmation_timeframe == "5m"


def test_explicit_m5_confirmation_allows_ob_fvg_checks_to_run() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "swing",
            "confirmation_timeframe": "5m",
            "candles_15m": _full_bullish_setup_candles(),
            "candles_5m": _missing_ob_fvg_candles(),
        }
    )

    assert result.swing.first_failed_gate == "no_ob_or_fvg_zone"
    assert result.swing.confirmation_structure_shift_status == "passed"
    assert result.swing.ob_fvg_diagnostics == "failed: OB missing; FVG missing."


def test_5m_bos_passed_uses_confirmation_indices_for_pullback() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "swing",
            "confirmation_timeframe": "5m",
            "candles_15m": _mtf_execution_sweep_candles(),
            "candles_5m": _mtf_confirmation_bos_candles(),
            "candles_2d": _trend_candles(),
        }
    )

    assert result.swing.confirmation_structure_shift_status == "passed"
    assert result.swing.pullback_zone.calculation_timeframe == "5m"
    assert result.swing.pullback_zone.sweep_candle_index == 90
    assert result.swing.pullback_zone.bos_choch_candle_index == 92
    assert result.swing.pullback_zone.displacement_start_index == 90
    assert result.swing.pullback_zone.displacement_end_index == 92
    assert result.swing.first_failed_gate != "missing_displacement_impulse"
    assert result.swing.pullback_failure_reason != "Displacement impulse is N/A because sweep and BOS/CHoCH indices are not usable."


def test_5m_bos_passed_with_candles_gets_specific_pullback_rejection() -> None:
    confirmation = _mtf_confirmation_bos_candles()
    confirmation[91]["open"] = Decimal("98")
    confirmation[91]["close"] = Decimal("98")
    confirmation[91]["high"] = Decimal("100")
    confirmation[91]["low"] = Decimal("96")
    confirmation[92]["low"] = Decimal("99")

    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "swing",
            "confirmation_timeframe": "5m",
            "candles_15m": _mtf_execution_sweep_candles(),
            "candles_5m": confirmation,
            "candles_2d": _trend_candles(),
        }
    )

    assert result.swing.confirmation_structure_shift_status == "passed"
    assert result.swing.first_failed_gate == "no_ob_or_fvg_zone"
    assert result.swing.first_failed_gate != "missing_displacement_impulse"


def test_eth_style_failed_bos_choch_still_rejects_before_pullback() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "ETHUSDT", "confirmation_timeframe": "5m",
            "mode": "swing",
            "candles_15m": _mtf_execution_sweep_candles(),
            "candles_5m": _base_candles(100),
        }
    )

    assert result.swing.confirmation_structure_shift_status == "failed"
    assert result.swing.first_failed_gate == "missing_confirmation_structure_shift"
    assert result.swing.pullback_zone_status == "N/A"
    assert result.swing.pullback_calculation_timeframe == NA


def test_missing_optional_context_marked_na() -> None:
    result = LiquidityGrabEngine().analyze({"symbol": "BTCUSDT"})

    assert "poc: N/A" in result.missing_data
    assert "cvd: N/A" in result.missing_data
    assert "liquidation_data: N/A" in result.missing_data
    assert "btc_d_context: N/A" in result.missing_data
    assert "event_risk_context: N/A" in result.missing_data
    assert "weekend_filter: N/A" in result.missing_data


def test_liquidity_grab_output_uses_poc_when_available() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "swing",
            "candles_15m": _full_bullish_setup_candles(),
            "candles_5m": _full_bullish_setup_candles(),
            "candles_2d": _trend_candles(),
            "poc": Decimal("102.5"),
            "volume_profile_source": "estimated_from_candles",
        }
    )

    assert result.swing.poc == Decimal("102.5")
    assert "POC available from estimated candle volume profile." in result.swing.strategy_diagnostics
    assert "POC: [102.5]" in result.formatted_output.swing_setup


def test_liquidity_grab_keeps_poc_na_when_unavailable() -> None:
    result = LiquidityGrabEngine().analyze({"symbol": "BTCUSDT", "mode": "swing"})

    assert result.swing.poc == NA
    assert "POC N/A because volume data missing/insufficient." in result.swing.strategy_diagnostics


def test_poc_alone_does_not_create_trade_idea() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "swing",
            "poc": Decimal("102.5"),
            "volume_profile_source": "estimated_from_candles",
        }
    )

    assert result.swing.is_valid is False
    assert result.swing.first_failed_gate == "no_execution_candles"
    assert result.swing.gates_passed == ()


def test_derivatives_used_as_confluence_only_on_valid_setup() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "swing",
            "candles_15m": _full_bullish_setup_candles(),
            "candles_5m": _full_bullish_setup_candles(),
            "candles_2d": _trend_candles(),
            "derivatives_enrichment": enrich_derivatives(
                {
                    "symbol": "BTCUSDT",
                    "exchange": "binance",
                    "latest_price": Decimal("112"),
                    "current_funding_rate": Decimal("0.0001"),
                    "current_open_interest": Decimal("105"),
                    "previous_open_interest": Decimal("100"),
                    "candles_15m": _full_bullish_setup_candles(),
                    "long_short_ratio": Decimal("1.10"),
                }
            ),
        }
    )

    assert result.swing.is_valid is True
    assert result.swing.derivatives_supports_trade is True
    assert result.swing.derivatives_conflict_reason == NA


def test_severe_derivatives_conflict_can_reject_after_technical_gates_pass() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "swing",
            "candles_15m": _full_bullish_setup_candles(),
            "candles_5m": _full_bullish_setup_candles(),
            "candles_2d": _trend_candles(),
            "derivatives_enrichment": enrich_derivatives(
                {
                    "symbol": "BTCUSDT",
                    "exchange": "binance",
                    "latest_price": Decimal("112"),
                    "current_funding_rate": Decimal("0.0015"),
                    "current_open_interest": Decimal("120"),
                    "previous_open_interest": Decimal("100"),
                    "candles_15m": _full_bullish_setup_candles(),
                    "long_short_ratio": Decimal("2.00"),
                }
            ),
        }
    )

    assert result.swing.is_valid is False
    assert "sweep" in result.swing.gates_passed
    assert "bos_choch" in result.swing.gates_passed
    assert "derivatives_conflict" in result.swing.gates_failed
    assert result.swing.derivatives_supports_trade is False
    assert "Severe derivatives conflict against long" in result.swing.derivatives_conflict_reason


def test_missing_derivatives_does_not_reject_by_itself() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "swing",
            "candles_15m": _full_bullish_setup_candles(),
            "candles_5m": _full_bullish_setup_candles(),
            "candles_2d": _trend_candles(),
        }
    )

    assert result.swing.is_valid is True
    assert "derivatives_conflict" not in result.swing.gates_failed
    assert result.swing.derivatives_supports_trade == NA


def test_derivatives_alone_cannot_create_setup() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "swing",
            "derivatives_enrichment": enrich_derivatives(
                {
                    "symbol": "BTCUSDT",
                    "exchange": "binance",
                    "latest_price": Decimal("112"),
                    "current_funding_rate": Decimal("0.0001"),
                    "current_open_interest": Decimal("105"),
                    "previous_open_interest": Decimal("100"),
                    "long_short_ratio": Decimal("1.10"),
                }
            ),
        }
    )

    assert result.swing.is_valid is False
    assert result.swing.first_failed_gate == "no_execution_candles"


def test_rejected_setup_remains_rejected_with_poc_context() -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": "swing",
            "candles_15m": _base_candles(),
            "candles_5m": _base_candles(),
            "poc": Decimal("102.5"),
            "volume_profile_source": "estimated_from_candles",
        }
    )

    assert result.swing.is_valid is False
    assert result.swing.first_failed_gate == "missing_confirmed_sweep"
    assert "Confirmed liquidity sweep is required." in result.swing.hard_rejection_reasons


def test_output_includes_all_sections_and_closing_line() -> None:
    result = LiquidityGrabEngine().analyze({"symbol": "BTCUSDT"})
    output = result.formatted_output.full_text

    assert "🟢 Challenge Setup" in output
    assert "🔵 Swing Setup" in output
    assert "🔴 Scalp Setup" in output
    assert output.endswith("⚔️ Candle Craft | Signal. Structure. Execution.")


@pytest.mark.parametrize("mode", ("swing", "scalp"))
def test_trade_plan_integrity_passes_on_valid_shared_mode_path(mode: str) -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": mode,
            "candles_15m": _full_bullish_setup_candles(),
            "candles_5m": _full_bullish_setup_candles(),
            "candles_2d": _trend_candles(),
        }
    )
    setup = getattr(result, mode)

    assert setup.is_valid is True
    assert setup.pullback_zone.trade_plan_integrity == "PASS"
    assert setup.pullback_zone.rr_entry_reference_type == "zone_low_limit"
    assert setup.pullback_zone.rr_target_reference == "tp2"


@pytest.mark.parametrize("mode", ("swing", "scalp"))
def test_tp1_inside_zone_is_rejected_on_shared_mode_path(mode: str) -> None:
    result = LiquidityGrabEngine().analyze(
        {
            "symbol": "BTCUSDT",
            "mode": mode,
            "candles_15m": _full_bullish_setup_candles(),
            "candles_5m": _full_bullish_setup_candles(),
            "candles_2d": _trend_candles(),
            "user_resistance_levels": (Decimal("98"),),
        }
    )
    setup = getattr(result, mode)

    assert setup.is_valid is False
    assert setup.first_failed_gate == "trade_plan_integrity"
    assert setup.pullback_zone.trade_plan_integrity == "FAIL"
    assert setup.pullback_zone.trade_plan_integrity_reason == "tp1_target_inside_entry_zone"
