from __future__ import annotations

from decimal import Decimal

from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import (
    FOOTER,
    HEADER_PREFIX,
    TelegramAlertType,
    TelegramSignalMessage,
    format_public_no_trade_message,
    format_telegram_price,
    format_telegram_rr,
    format_telegram_signal_message,
)


def _message(**overrides: object) -> TelegramSignalMessage:
    data = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "signal_id": "sig-001",
        "mode": "scalp",
        "quality": "A",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop_loss": Decimal("95"),
        "tp1": Decimal("110"),
        "tp2": Decimal("115"),
        "tp3": Decimal("120"),
        "planned_rr": Decimal("2.91918017"),
        "structure_reason": "Sweep and reclaim into valid pullback.",
        "confirmation_needed": "5m BOS/CHoCH.",
        "needs_next": ("Price must trade into the Limit Zone.",),
        "invalidation_reason": "Invalid if price accepts below 95.",
        "confluence": "LTF BOS/CHoCH confirmed.",
    }
    data.update(overrides)
    return TelegramSignalMessage(**data)


def test_all_public_signal_messages_end_with_candle_craft_footer() -> None:
    for alert_type in TelegramAlertType:
        text = format_telegram_signal_message(alert_type, _message())

        assert text.startswith(HEADER_PREFIX)
        assert text.endswith(FOOTER)


def test_valid_scalp_signal_renders_premium_format() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())

    assert "🐺🟠 SCALP SIGNAL — BTCUSDT" in text
    assert "The wolf found liquidity." in text
    assert "Bias: LONG" in text
    assert "Status: CONFIRMED" in text
    assert "Quality: A" in text
    assert "RR: 2.9R" in text
    assert "🎯 Trade Map" in text
    assert "Entry Zone: 100 – 102" in text
    assert "TP1: 110" in text
    assert "TP2: 115" in text
    assert "TP3: 120" in text
    assert "Now we wait for execution inside the limit zone — no chase." in text
    assert "⚠️ Manual execution only. Manage risk." in text
    assert "Signal ID" not in text


def test_watchlist_renders_stalking_language_and_not_confirmed() -> None:
    text = format_telegram_signal_message(TelegramAlertType.WATCHLIST, _message())

    assert "🐺🟠 WATCHLIST — BTCUSDT" in text
    assert "The wolf is stalking this one." in text
    assert "Status: WATCHLIST" in text
    assert "Potential RR: 2.9R" in text
    assert "👀 What we want to see" in text
    assert "No confirmation = no trade." in text
    assert "CONFIRMED SIGNAL" not in text
    assert "Status: CONFIRMED" not in text


def test_watchlist_upgraded_requires_upgrade_flag() -> None:
    plain = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())
    upgraded = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(upgraded_from_watchlist=True),
    )

    assert "WATCHLIST UPGRADED" not in plain
    assert "🐺🟠 WATCHLIST UPGRADED — BTCUSDT" in upgraded
    assert "The wolf has confirmation." in upgraded
    assert "Previous state: WATCHLIST" in upgraded
    assert "New state: CONFIRMED SIGNAL" in upgraded
    assert "What changed:\nSweep and reclaim into valid pullback." in upgraded


def test_limit_zone_hit_renders_hunting_zone_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.LIMIT_HIT, _message())

    assert "🐺🟠 SCALP SIGNAL — BTCUSDT" in text
    assert "Entry Zone Touched." in text
    assert "Status: LIMIT ZONE HIT" in text
    assert "Entry Zone: 100 – 102" in text
    assert "TP1: 110" in text
    assert "Manual execution only. Manage risk." in text
    assert "TP1 HIT" not in text


def test_tp1_renders_partial_win_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.TP1_HIT, _message())

    assert "🐺🟠 TP1 HIT — BTCUSDT" in text
    assert "First target secured." in text
    assert "Status: PARTIAL WIN" in text
    assert "Risk should now be reduced according to your own plan." in text
    assert "The wolf eats step by step." in text


def test_tp2_renders_strong_follow_through_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.TP2_HIT, _message())

    assert "🐺🟠 TP2 HIT — BTCUSDT" in text
    assert "The move is developing cleanly." in text
    assert "Status: STRONG FOLLOW-THROUGH" in text
    assert "Remaining target:\nTP3: 120" in text


def test_tp3_renders_trade_complete_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.TP3_HIT, _message())

    assert "🐺🟠 TP3 HIT — BTCUSDT" in text
    assert "Full target sequence completed." in text
    assert "Final Target: 120" in text
    assert "Status: TRADE COMPLETE" in text
    assert "The wolf tracked it from liquidity to expansion." in text


def test_stop_hit_renders_controlled_loss_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SL_HIT, _message())

    assert "🐺🟠 STOP HIT — BTCUSDT" in text
    assert "Setup invalidated." in text
    assert "Status: CLOSED" in text
    assert "Small controlled losses protect us for the next A-grade opportunity." in text


def test_watchlist_invalidated_renders_wolf_walks_away_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.INVALIDATED, _message(was_watchlist=True))

    assert "🐺🟠 WATCHLIST INVALIDATED — BTCUSDT" in text
    assert "The wolf walks away." in text
    assert "No forced trades." in text
    assert "No weak confirmations." in text


def test_signal_invalidated_renders_cancelled_no_chase_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.INVALIDATED, _message(was_watchlist=False))

    assert "🐺🟠 SIGNAL INVALIDATED — BTCUSDT" in text
    assert "The setup is cancelled." in text
    assert "No chase." in text
    assert "The setup no longer meets Candle Craft rules." in text


def test_no_trade_output_does_not_expose_internal_debug_codes() -> None:
    text = format_public_no_trade_message(
        _message(),
        "first_failed_gate=missing_confirmation_structure_shift; strategy_diagnostics={raw}",
    )

    assert "🐺🟠 NO TRADE — BTCUSDT" in text
    assert "Status: NO VALID SETUP" in text
    assert "Reason: Confirmation is not clean yet." in text
    assert "first_failed_gate" not in text
    assert "missing_confirmation_structure_shift" not in text
    assert "strategy_diagnostics" not in text


def test_missing_tp3_renders_na() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message(tp3=NA))

    assert "TP3: N/A" in text


def test_missing_reason_renders_na_without_inventing_confirmation() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(structure_reason=NA, confluence=NA),
    )

    reason = text.split("🧠 Why this setup matters\n", 1)[1].split("\nNow we wait", 1)[0]
    assert reason == "N/A"
    assert "confirmation" not in reason.lower()


def test_missing_invalidation_uses_safe_stop_fallback() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(invalidation_reason=NA),
    )

    assert "🚫 Invalid if\nPrice accepts below 95." in text


def test_rejected_no_setup_output_is_not_converted_to_valid_signal() -> None:
    text = format_public_no_trade_message(_message(), "Opportunity score is below 80.")

    assert "NO TRADE" in text
    assert "SIGNAL — BTCUSDT" not in text
    assert "Status: CONFIRMED" not in text
    assert "Reason: Quality is not strong enough yet." in text


def test_price_display_formats_clean_public_values() -> None:
    assert format_telegram_price(Decimal("73.252056")) == "73.25"
    assert format_telegram_price(Decimal("109.99894")) == "110"
    assert format_telegram_price(Decimal("0.0457434")) == "0.04574"
    assert format_telegram_price(Decimal("0.16737736")) == "0.16738"
    assert format_telegram_price(Decimal("70000.123456")) == "70000.12"
    assert "E" not in format_telegram_price(Decimal("0.000000123456")).upper()
    assert format_telegram_price(NA) == NA
    assert format_telegram_price({"price": "73.25"}) == NA


def test_rr_display_formats_clean_public_values() -> None:
    assert format_telegram_rr(Decimal("2.91918017")) == "2.9R"
    assert format_telegram_rr(Decimal("3.000000")) == "3R"
    assert format_telegram_rr(Decimal("3.246")) == "3.2R"
    assert format_telegram_rr(NA) == NA
    assert format_telegram_rr("not numeric") == NA
