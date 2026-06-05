from __future__ import annotations

from pathlib import Path

from app.formatters.telegram_signal_detail import (
    TelegramSignalDetail,
    format_signal_detail,
    format_signal_detail_why_valid,
)


def test_signal_detail_formatter_full_active_signal_data() -> None:
    text = format_signal_detail(
        TelegramSignalDetail(
            symbol="ADAUSDT",
            bias="long",
            status="confirmed",
            quality="A-",
            lifecycle=("confirmed", "executing"),
            entry_low="100",
            entry_high="102",
            stop_loss="95",
            tp1="110",
            tp2="118",
            tp3="125",
            why_it_matters="Downside liquidity swept and structure shifted.",
            invalid_if="Price accepts below 95.",
        )
    )

    assert text.startswith("🐺🟠 ADAUSDT — SIGNAL DETAIL")
    assert "Bias: LONG" in text
    assert "Status: CONFIRMED" in text
    assert "Quality: A-" in text
    assert "Lifecycle: CONFIRMED → EXECUTING" in text
    assert "🎯 Trade Map" in text
    assert "Entry: 100 – 102" in text
    assert "Stop: 95" in text
    assert "TP1: 110" in text
    assert "TP2: 118" in text
    assert "TP3: 125" in text
    assert "🧠 Why it matters" in text
    assert "🚫 Invalid if" in text
    assert text.endswith("Candle Craft | Signal. Structure. Execution.")


def test_signal_detail_formatter_missing_fields_use_na_and_preserve_unverified() -> None:
    missing = format_signal_detail(TelegramSignalDetail(symbol="ETHUSDT"))

    assert "Bias: N/A" in missing
    assert "Status: N/A" in missing
    assert "Quality: N/A" in missing
    assert "Lifecycle: N/A" in missing
    assert "Entry: N/A" in missing
    assert "Stop: N/A" in missing
    assert "TP1: N/A" in missing
    assert "TP2: N/A" in missing
    assert "TP3: N/A" in missing
    assert "\nN/A\n\n🚫 Invalid if\nN/A\n" in missing

    unverified = format_signal_detail(
        TelegramSignalDetail(
            symbol="ETHUSDT",
            bias="Unverified",
            status="Unverified",
            quality="Unverified",
            lifecycle="Unverified",
            entry_low="Unverified",
            stop_loss="Unverified",
            why_it_matters="Unverified",
            invalid_if="Unverified",
        )
    )

    assert "Bias: Unverified" in unverified
    assert "Status: Unverified" in unverified
    assert "Quality: Unverified" in unverified
    assert "Lifecycle: Unverified" in unverified
    assert "Entry: Unverified" in unverified
    assert "Stop: Unverified" in unverified
    assert "Why it matters\nUnverified" in unverified
    assert "Invalid if\nUnverified" in unverified


def test_signal_detail_why_valid_uses_confirmed_facts_and_na_where_missing() -> None:
    text = format_signal_detail_why_valid(
        TelegramSignalDetail(
            symbol="BTCUSDT",
            confirmed_facts=("Sweep confirmed.", "Structure shift confirmed."),
            confirmed_gates=("Quality gate passed.",),
        )
    )

    assert text.startswith("🐺🟠 BTCUSDT — WHY VALID?")
    assert "Confirmed facts\nSweep confirmed.\nStructure shift confirmed." in text
    assert "Confirmed gates\nQuality gate passed." in text
    assert text.endswith("Candle Craft | Signal. Structure. Execution.")

    missing = format_signal_detail_why_valid(TelegramSignalDetail(symbol="BTCUSDT"))

    assert "Confirmed facts\nN/A" in missing
    assert "Confirmed gates\nN/A" in missing


def test_signal_detail_formatter_has_no_mock_symbol_or_prices_in_production_source() -> None:
    source = Path("app/formatters/telegram_signal_detail.py").read_text(encoding="utf-8")

    assert "SOLUSDT.P" not in source
    for mock_price in ("143.20", "144.10", "141.60", "146.80", "149.40", "152.00"):
        assert mock_price not in source
