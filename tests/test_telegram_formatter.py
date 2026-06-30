from __future__ import annotations

from decimal import Decimal

from app.analytics.setup_quality import validate_setup_quality
from app.data.dtos import NA
from app.formatters.telegram_formatter import (
    format_no_setup_message,
    format_telegram_strategy_output,
    format_valid_setup_message,
)
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerSymbolResult
from app.strategies.liquidity_grab_pullback import LiquidityGrabEngine


def _valid_symbol_result(**overrides: object) -> ScannerSymbolResult:
    data = {
        "symbol": "BTCUSDT",
        "status": ScannerPipelineStatus.IDEA_CREATED,
        "status_history": (ScannerPipelineStatus.IDEA_CREATED,),
        "valid_strategy_modes": ("scalp",),
        "strategy_diagnostics": {
            "scalp": {
                "is_valid": True,
                "mode": "scalp",
                "bias": "long",
                "entry_low": Decimal("103100"),
                "entry_high": Decimal("103300"),
                "stop": Decimal("102400"),
                "tp1": Decimal("104800"),
                "tp2": Decimal("106000"),
                "tp3": Decimal("107500"),
                "rr_to_tp2": Decimal("3.246"),
                "invalidation": "Invalid if price accepts below 102400.",
                "structure_reason": "Sweep and reclaim into valid pullback.",
                "trust_grade": "A",
            }
        },
    }
    data.update(overrides)
    return ScannerSymbolResult(**data)


def _rejected_symbol_result(**overrides: object) -> ScannerSymbolResult:
    data = {
        "symbol": "BTCUSDT",
        "status": ScannerPipelineStatus.SCANNED_NO_SETUP,
        "status_history": (ScannerPipelineStatus.SCANNED_NO_SETUP,),
        "rejection_reason": "No valid Liquidity-Grab Pullback setup.",
        "rejection_reasons": ("No valid Liquidity-Grab Pullback setup.",),
        "strategy_diagnostics": {
            "challenge": {
                "is_valid": False,
                "mode": "challenge",
                "bias": "long",
                "htf_2d_trend": "bullish",
                "mtf_12h_trend": "bullish",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "failed",
                "first_failed_gate": "missing_confirmation_structure_shift",
                "confirmation_bos_choch_reason": "No 5m BOS/CHoCH close beyond the required LTF swing.",
                "gates_failed": ("missing_confirmation_structure_shift",),
                "hard_rejection_reasons": ("No 5m BOS/CHoCH close beyond the required LTF swing.",),
            }
        },
        "rejected_strategy_modes": ("challenge",),
    }
    data.update(overrides)
    return ScannerSymbolResult(**data)


def test_valid_setup_formatting_is_premium_telegram_ready() -> None:
    text = format_telegram_strategy_output(_valid_symbol_result())

    assert text.startswith("🐺 Candle Craft Intelligence")
    assert "BTCUSDT · LONG · SCALP" in text
    assert "Grade: A | Score: N/A | RR: 3.25R" in text
    assert "Status: 🟢 Entry Zone Active" in text
    assert "Entry: 103100 – 103300" in text
    assert "TP1: 104800" in text
    assert "TP2: 106000" in text
    assert "TP3: 107500" in text
    assert "No chase. Entry only inside the mapped zone." in text
    assert "Invalid if price body-closes and accepts below 102400." in text
    assert "Signal ID" not in text
    assert "strategy_diagnostics" not in text
    assert text.endswith("Candle Craft | Signal. Structure. Execution.")

def test_no_setup_formatting_is_premium_no_trade() -> None:
    text = format_no_setup_message(_rejected_symbol_result())

    assert "🐺🟠 NO TRADE — BTCUSDT" in text
    assert "The wolf is watching, but not entering." in text
    assert "Status: NO VALID SETUP" in text
    assert "Reason: No 5m BOS/CHoCH close beyond the required LTF swing." in text
    assert "No confirmation = no trade." in text
    assert "missing_confirmation_structure_shift" not in text
    assert "first_failed_gate" not in text
    assert text.endswith("Candle Craft | Signal. Structure. Execution.")


def test_na_values_are_preserved_in_valid_setup_formatting() -> None:
    text = format_valid_setup_message(
        _valid_symbol_result(),
        mode="scalp",
        setup=None,
    )

    assert "TP3: 107500" in text
    missing_tp3 = format_telegram_strategy_output(
        _valid_symbol_result(
            strategy_diagnostics={
                "scalp": {
                    "is_valid": True,
                    "mode": "scalp",
                    "bias": "long",
                    "entry_low": Decimal("103100"),
                    "entry_high": Decimal("103300"),
                    "stop": Decimal("102400"),
                    "tp1": Decimal("104800"),
                    "tp2": Decimal("106000"),
                    "tp3": NA,
                    "rr_to_tp2": Decimal("3.246"),
                    "invalidation": "Invalid if price accepts below 102400.",
                }
            }
        )
    )
    assert "🎯 Trade Map" in missing_tp3
    assert "TP3: N/A" in missing_tp3


def test_full_diagnostics_remain_explicit_cli_diagnostics() -> None:
    default = format_no_setup_message(_rejected_symbol_result())
    full = format_no_setup_message(_rejected_symbol_result(), diagnostics_level="full")

    assert "Diagnostics" not in default
    assert "Diagnostics" in full
    assert "Failed gates: missing_confirmation_structure_shift" in full
    assert full.endswith("Candle Craft | Signal. Structure. Execution.")


def test_formatter_does_not_send_live_telegram(monkeypatch) -> None:
    async def fail_if_called(*args, **kwargs):  # pragma: no cover - should never run
        raise AssertionError("Telegram transport should not be called by the formatter")

    monkeypatch.setattr("app.alerts.telegram.send_telegram_messages", fail_if_called)

    text = format_telegram_strategy_output(_rejected_symbol_result())

    assert "NO TRADE" in text


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

    assert "NO TRADE" in text
    assert result.swing.is_valid is False
    assert result.swing.first_failed_gate == "no_execution_candles"
    assert result.swing.model_dump() == before


def test_quality_rejected_setup_does_not_format_as_telegram_signal() -> None:
    symbol_result = _valid_symbol_result(
        setup_quality=validate_setup_quality(
            {
                "setup_valid": False,
                "sweep_passed": True,
                "confirmation_passed": False,
                "first_failed_gate": "missing_confirmation_structure_shift",
                "gates_passed": ("sweep",),
            }
        )
    )

    text = format_telegram_strategy_output(symbol_result)

    assert "NO TRADE" in text
    assert "Status: CONFIRMED" not in text
