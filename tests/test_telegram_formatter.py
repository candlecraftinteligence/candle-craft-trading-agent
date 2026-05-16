from __future__ import annotations

from decimal import Decimal

from app.data.dtos import NA
from app.analytics.setup_quality import validate_setup_quality
from app.formatters.telegram_formatter import (
    format_no_setup_message,
    format_telegram_strategy_output,
    format_valid_setup_message,
)
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerSymbolResult
from app.strategies.liquidity_grab_pullback import (
    LiquidityGrabEngine,
    LiquidityGrabMode,
    LiquidityGrabResult,
    LiquidityGrabSetup,
    LiquiditySweepSignal,
    StrategyFormattedOutput,
    TrustMeterResult,
)


def _valid_challenge_setup() -> LiquidityGrabSetup:
    return LiquidityGrabSetup(
        mode=LiquidityGrabMode.challenge,
        is_valid=True,
        status="Pending",
        bias="long",
        timeframe="15m",
        trend="bullish",
        htf_timeframe="2d",
        bias_timeframe="12h",
        execution_timeframe="15m",
        confirmation_timeframe="5m",
        htf_2d_trend="bullish",
        mtf_12h_trend="bullish",
        current_price=Decimal("104250.5"),
        poc=Decimal("103900"),
        sweep=LiquiditySweepSignal(
            is_present=True,
            direction="bullish",
            wick_price=Decimal("102500"),
            swing_level=Decimal("102800"),
        ),
        entry_low=Decimal("103100"),
        entry_high=Decimal("103300"),
        entry=Decimal("103200"),
        stop=Decimal("102400"),
        tp1=Decimal("104800"),
        tp2=Decimal("106000"),
        tp3=Decimal("107500"),
        rr_to_tp2=Decimal("3.2"),
        invalidation="Invalid if price accepts below 102400.",
        trust_meter=TrustMeterResult(score=88, percentage=88, grade="A", risk_tier="base"),
        gates_passed=("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
    )


def _valid_symbol_result() -> ScannerSymbolResult:
    setup = _valid_challenge_setup()
    strategy_result = LiquidityGrabResult(
        symbol="BTCUSDT",
        requested_mode=LiquidityGrabMode.challenge,
        challenge=setup,
        swing=LiquidityGrabSetup(mode=LiquidityGrabMode.swing),
        scalp=LiquidityGrabSetup(mode=LiquidityGrabMode.scalp),
        formatted_output=StrategyFormattedOutput(
            challenge_setup="valid",
            swing_setup="No valid swing setup.",
            scalp_setup="No valid scalp setup.",
            full_text="valid",
        ),
    )
    return ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        current_price=Decimal("104250.5"),
        latest_close=Decimal("104250.5"),
        nearest_support=Decimal("102800"),
        nearest_resistance=Decimal("106000"),
        funding_rate=Decimal("0.0001"),
        funding_status="normal",
        open_interest=Decimal("1500000"),
        open_interest_change_pct=Decimal("4.5"),
        oi_direction="rising",
        price_oi_relationship="long_building_or_breakout_participation",
        derivatives_score=86,
        poc=Decimal("103900"),
        value_area_high=Decimal("105000"),
        value_area_low=Decimal("102900"),
        valid_strategy_modes=("challenge",),
        strategy_results={"challenge": strategy_result},
        strategy_diagnostics={
            "challenge": {
                "is_valid": True,
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
            }
        },
    )


def _rejected_symbol_result() -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejection_reason="No valid Liquidity-Grab Pullback setup.",
        rejection_reasons=("No valid Liquidity-Grab Pullback setup.",),
        strategy_diagnostics={
            "challenge": {
                "is_valid": False,
                "htf_2d_trend": "bearish",
                "mtf_12h_trend": "neutral",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "failed",
                "pullback_zone_status": NA,
                "first_failed_gate": "missing_confirmation_structure_shift",
                "confirmation_bos_choch_reason": "No 5m BOS/CHoCH close beyond the required LTF swing.",
                "gates_failed": ("missing_confirmation_structure_shift",),
                "hard_rejection_reasons": ("No 5m BOS/CHoCH close beyond the required LTF swing.",),
            }
        },
        rejected_strategy_modes=("challenge",),
    )


def test_valid_setup_formatting_is_telegram_ready() -> None:
    text = format_telegram_strategy_output(_valid_symbol_result())

    assert "BTCUSDT \u2014 Valid Setup" in text
    assert "\U0001F4CD Bias" in text
    assert "• 2D HTF: bullish" in text
    assert "✅ Passed" in text
    assert "• 15m sweep" in text
    assert "• 5m BOS/CHoCH" in text
    assert "• Funding: 0.0001 / normal" in text
    assert "• OI: 1500000 / 4.5% / rising" in text
    assert "• Entry: 103100 - 103300" in text
    assert "• Trust Meter: A + 88%" in text
    assert "• Invalidation: Invalid if price accepts below 102400." in text
    assert "• Risk warning:" in text
    assert "Trade idea created. Status: Pending." in text
    assert text.endswith("\u2694\ufe0f Candle Craft | Signal. Structure. Execution.")


def test_no_setup_formatting_is_readable() -> None:
    text = format_no_setup_message(_rejected_symbol_result())

    assert "BTCUSDT \u2014 No Valid Setup" in text
    assert "\U0001F4CD Bias" in text
    assert "• 2D HTF: bearish" in text
    assert "✅ Passed" in text
    assert "• 15m sweep" in text
    assert "❌ Failed" in text
    assert "• 5m BOS/CHoCH" in text
    assert "No valid setup. No trade. Wait for confirmation." in text
    assert "Needs next" in text


def test_na_values_are_preserved_in_valid_setup_formatting() -> None:
    symbol_result = _valid_symbol_result().model_copy(
        update={
            "poc": NA,
            "value_area_high": NA,
            "value_area_low": NA,
            "funding_rate": NA,
            "funding_status": NA,
            "open_interest": NA,
            "open_interest_change_pct": NA,
            "oi_direction": NA,
            "price_oi_relationship": NA,
            "derivatives_score": NA,
        }
    )
    setup = _valid_challenge_setup().model_copy(update={"poc": NA, "tp3": NA})

    text = format_valid_setup_message(symbol_result, mode="challenge", setup=setup)

    assert "• POC: N/A" in text
    assert "• VAH/VAL: N/A" in text
    assert "• Funding: N/A" in text
    assert "• OI: N/A" in text
    assert "• RR: 3.2" in text


def test_failed_gate_formatting_uses_clean_reason() -> None:
    text = format_no_setup_message(_rejected_symbol_result())

    assert "• Gate: missing_confirmation_structure_shift" in text
    assert "🧠 Why" in text
    assert "No 5m BOS/CHoCH close beyond the required LTF swing." in text
    assert "missing_confirmation_structure_shift\n\n🧠 Why" in text


def test_compact_mode_and_full_diagnostics_are_supported() -> None:
    compact = format_telegram_strategy_output(_valid_symbol_result(), compact=True)
    full = format_no_setup_message(_rejected_symbol_result(), diagnostics_level="full")

    assert "\U0001F4CD Bias" not in compact
    assert "BTCUSDT — Valid Setup | Challenge A 88%" in compact
    assert "Diagnostics" in full
    assert "• Failed gates: missing_confirmation_structure_shift" in full


def test_formatter_does_not_send_live_telegram(monkeypatch) -> None:
    async def fail_if_called(*args, **kwargs):  # pragma: no cover - should never run
        raise AssertionError("Telegram transport should not be called by the formatter")

    monkeypatch.setattr("app.alerts.telegram.send_telegram_messages", fail_if_called)

    text = format_telegram_strategy_output(_rejected_symbol_result())

    assert "No Valid Setup" in text


def test_formatter_does_not_change_strategy_gate_results() -> None:
    result = LiquidityGrabEngine().analyze({"symbol": "BTCUSDT", "mode": "swing"})
    before = result.swing.model_dump()
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        strategy_results={"swing": result},
        strategy_diagnostics={
            "swing": {
                "is_valid": result.swing.is_valid,
                "first_failed_gate": result.swing.first_failed_gate,
                "hard_rejection_reasons": result.swing.hard_rejection_reasons,
            }
        },
        rejected_strategy_modes=("swing",),
    )

    text = format_telegram_strategy_output(symbol_result)

    assert "No Valid Setup" in text
    assert result.swing.is_valid is False
    assert result.swing.first_failed_gate == "no_execution_candles"
    assert result.swing.model_dump() == before


def test_valid_setup_includes_quality_grade_and_score_when_evaluated() -> None:
    symbol_result = _valid_symbol_result().model_copy(
        update={
            "setup_quality": validate_setup_quality(
                {
                    "setup_valid": True,
                    "bias": "long",
                    "rr_to_tp2": Decimal("3.2"),
                    "sweep_passed": True,
                    "confirmation_passed": True,
                    "pullback_valid": True,
                    "ob_or_fvg_valid": True,
                    "fib_valid": True,
                    "htf_2d_trend": "bullish",
                    "mtf_12h_trend": "bullish",
                    "trust_percentage": 90,
                    "poc_available": True,
                    "value_area_available": True,
                    "derivatives_supports_trade": True,
                    "derivatives_score": 86,
                    "funding_status": "normal",
                    "crowding_risk": "low",
                    "risk_approved": True,
                    "data_quality_score": Decimal("90"),
                }
            )
        }
    )

    text = format_telegram_strategy_output(symbol_result)

    assert "Quality: HIGH_QUALITY_TRADE" in text
    assert "Grade/Score:" in text


def test_quality_rejected_setup_does_not_format_as_telegram_signal() -> None:
    symbol_result = _valid_symbol_result().model_copy(
        update={
            "setup_quality": validate_setup_quality(
                {
                    "setup_valid": False,
                    "sweep_passed": True,
                    "confirmation_passed": False,
                    "first_failed_gate": "missing_confirmation_structure_shift",
                    "gates_passed": ("sweep",),
                }
            )
        }
    )

    text = format_telegram_strategy_output(symbol_result)

    assert "Valid Setup" not in text
    assert "BTCUSDT — No valid trade" in text
    assert "Action: Wait for confirmation" in text
