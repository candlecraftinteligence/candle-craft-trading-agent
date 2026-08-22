from __future__ import annotations

from decimal import Decimal

from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import (
    FOOTER,
    HEADER_PREFIX,
    TelegramAlertType,
    SignalMessageContext,
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
        "confirmation_needed": "15m BOS/CHoCH.",
        "confirmation_timeframe": "15m",
        "needs_next": ("Price must trade into the Limit Zone.",),
        "invalidation_reason": "Invalid if price accepts below 95.",
        "confluence": "LTF BOS/CHoCH confirmed.",
    }
    data.update(overrides)
    return TelegramSignalMessage(**data)


def test_all_public_signal_messages_end_with_candle_craft_footer() -> None:
    for alert_type in TelegramAlertType:
        text = format_telegram_signal_message(alert_type, _message())

        if alert_type in {
            TelegramAlertType.WATCHLIST,
            TelegramAlertType.SETUP_TRIGGERED,
            TelegramAlertType.SIGNAL_CONFIRMED,
        }:
            assert text.startswith("🐺 Candle Craft Intelligence")
        else:
            assert text.startswith((HEADER_PREFIX, "\U0001F7E1"))
        assert text.endswith(FOOTER)


def test_valid_scalp_signal_renders_premium_compact_card() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())

    assert text.startswith("🐺 Candle Craft Intelligence")
    assert "BTCUSDT · LONG · SCALP" in text
    assert "Grade: A | Score: N/A | RR: 2.92R" in text
    assert "Status: 🟢 CONFIRMED SIGNAL" in text
    assert "Confirmation criteria satisfied." in text
    assert "🐺 Wolf found downside liquidity." in text
    assert "Now we track the reaction." in text
    assert "🎯 Trade Map" in text
    assert "Entry: 100 – 102" in text
    assert "Stop: 95" in text
    assert "TP1: 110" in text
    assert "TP2: 115" in text
    assert "TP3: 120" in text
    assert "🧠 Edge" in text
    assert "Downside liquidity was swept and reclaimed." in text
    assert "15m structure shifted bullish." in text
    assert "⚠️ Execution" in text
    assert "No chase. Entry only inside the mapped zone." in text
    assert "Invalid if price body-closes and accepts below 95." in text
    assert "Not financial advice." in text
    assert "Actionability" + ":" not in text
    assert "Why this setup " + "matters" not in text
    assert "Execution " + "notes" not in text
    assert "What we want " + "next" not in text
    assert "Signal ID" not in text


def test_signal_formatter_uses_explicit_m5_confirmation_override() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(confirmation_timeframe="5m"),
    )

    assert "5m structure shifted bullish." in text
    assert "15m structure shifted bullish." not in text

def test_public_watchlist_formatter_matches_compact_watch_shape() -> None:
    text = format_telegram_signal_message(TelegramAlertType.WATCHLIST, _message())

    assert text.startswith("🐺 Candle Craft Intelligence")
    assert "BTCUSDT · LONG · SCALP" in text
    assert "Grade: A | Score: N/A | RR: 2.92R" in text
    assert "Status: 🟡 Watch · Confirmation Needed" in text
    assert "🐺 Wolf found downside liquidity." in text
    assert "🎯 Trade Map" in text
    assert "Entry: 100 – 102" in text
    assert "Stop: 95" in text
    assert "TP1: 110" in text
    assert "TP2: 115" in text
    assert "TP3: 120" in text
    assert "⚠️ Execution" in text
    assert "Wait for confirmation. No entry until structure accepts back through the trigger zone." in text
    assert "Invalid if price body-closes and accepts below 95." in text
    assert "Candle Craft | Signal. Structure. Execution." in text
    assert "Actionability" + ":" not in text
    assert "Why this setup " + "matters" not in text
    assert "Execution " + "notes" not in text
    assert "What we want " + "next" not in text
    assert "Potential RR" not in text
    assert "No confirmation = no trade." not in text
    assert "CONFIRMED" not in text


def test_public_watchlist_target_caution_renders_single_tp1_priority_warning() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(
            actionability_state="A_GRADE_ACTIONABLE_TARGET_CAUTION",
            target_failure_severity="target_caution_actionable",
            target_warning_reason="TP2 remains inside recent chop/range.",
        ),
    )

    assert "Status: 🟠 A-Grade Setup · TP1 Priority" in text
    assert "No chase. Entry only inside the mapped zone." in text
    assert "TP1 reaction matters because TP2/TP3 path is choppy." in text
    assert text.count("TP1 reaction matters because TP2/TP3 path is choppy.") == 1
    assert "Actionability" + ":" not in text
    assert "TARGET " + "CAUTION" not in text
    assert "Target path is choppy/tighter" not in text
    assert "Reduce aggression until price clears chop" not in text
    assert "target clean" not in text.lower()
    assert "clean target" not in text.lower()


def test_short_liquidity_rejection_signal_tracks_rejection_and_invalidates_above_stop() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(
            direction="short",
            stop_loss=Decimal("105"),
            structure_reason="Upside liquidity was swept and rejected.",
            confluence="LTF BOS/CHoCH confirmed.",
            invalidation_reason="Invalid if price accepts above 105.",
        ),
    )

    assert "BTCUSDT · SHORT · SCALP" in text
    assert "🐺 Wolf found upside liquidity." in text
    assert "Now we track the rejection." in text
    assert "Upside liquidity was swept and rejected." in text
    assert "Invalid if price body-closes and accepts above 105." in text


def test_public_signal_missing_liquidity_uses_safe_wolf_fallback() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(structure_reason="Pullback zone is mapped.", confluence="LTF BOS/CHoCH confirmed."),
    )

    assert "Wolf found downside liquidity" not in text
    assert "Wolf found upside liquidity" not in text
    assert "🐺 Wolf is tracking liquidity." in text
    assert "Now we wait for confirmation." in text


def test_public_watchlist_formatter_uses_short_bias_and_stop() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(
            direction="short",
            stop_loss=Decimal("105"),
            structure_reason="Upside liquidity was swept and rejected.",
            invalidation_reason="Invalid if price accepts above 105.",
        ),
    )

    assert "BTCUSDT · SHORT · SCALP" in text
    assert "Stop: 105" in text
    assert "Invalid if price body-closes and accepts above 105." in text
    assert "Limit Zone must hold as resistance" not in text
    assert "Bearish structure must remain valid" not in text
    assert "Invalid below/above" not in text



def test_watchlist_formatter_shape() -> None:
    test_public_watchlist_formatter_matches_compact_watch_shape()


def test_triggered_waiting_confirmation_formatter_shape() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(
            watchlist_status="LIMIT_ZONE_HIT_WAITING_CONFIRMATION",
            structure_reason="Liquidity has been swept, but structure has not fully confirmed yet. This is a stalking setup - confirmation is still required before aggressive execution.",
        ),
    )

    assert "BTCUSDT · LONG · SCALP" in text
    assert "Status: 🟡 Watch · Confirmation Needed" in text
    assert "Wait for confirmation. No entry until structure accepts back through the trigger zone." in text
    assert "Status: LIMIT ZONE HIT" not in text
    assert "No confirmation = no trade." not in text
    assert "CONFIRMED SIGNAL" not in text
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

    assert "🐺🟠 ENTRY ZONE TOUCHED — BTCUSDT" in text
    assert "Entry zone touched." in text
    assert "Status: AWAITING FOLLOW-THROUGH" in text
    assert "Direction: LONG" in text
    assert "Quality: A" in text
    assert "Entry Zone: 100 – 102" in text
    assert "Invalidation: Invalid if price accepts below 95." in text
    assert "Use the existing published plan only." in text
    assert "No confirmation = no chase." in text
    assert "TP1: 110" not in text
    assert "TAKE PROFIT HIT" not in text
    lowered = text.lower()
    assert "scalp signal" not in lowered
    assert "active for manual execution" not in lowered
    assert "status: confirmed" not in lowered
    assert "executing" not in lowered


def test_tp1_renders_partial_win_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.TP1_HIT, _message())

    assert "🐺🟠 TAKE PROFIT HIT — BTCUSDT" in text
    assert "First target secured." in text
    assert "Status: PARTIAL WIN" in text
    assert "Risk should now be reduced according to your own plan." in text
    assert "The wolf eats step by step." in text


def test_tp2_renders_strong_follow_through_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.TP2_HIT, _message())

    assert "🐺🟠 TAKE PROFIT HIT — BTCUSDT" in text
    assert "The move is developing cleanly." in text
    assert "Status: STRONG FOLLOW-THROUGH" in text
    assert "Remaining target:\nTP3: 120" in text


def test_tp3_renders_trade_complete_language() -> None:
    text = format_telegram_signal_message(TelegramAlertType.TP3_HIT, _message())

    assert "🐺🟠 TAKE PROFIT HIT — BTCUSDT" in text
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


def test_missing_tp3_renders_labeled_na_target() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message(tp3=NA))

    assert "🎯 Trade Map" in text
    assert "TP1: 110" in text
    assert "TP2: 115" in text
    assert "TP3: N/A" in text
    assert "Trade Map (incomplete stored context)" not in text
    assert "Missing: TP3" not in text


def test_missing_reason_renders_na_edge_without_inventing_confirmation() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(structure_reason=NA, confluence=NA),
    )

    edge = text.split("🧠 Edge\n", 1)[1].split("\n\n⚠️ Execution", 1)[0]
    assert edge == "N/A"
    assert "confirmation" not in edge.lower()


def test_missing_invalidation_uses_directional_body_close_stop_fallback() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(invalidation_reason=NA),
    )

    assert "Invalid if price body-closes and accepts below 95." in text


def test_swing_signal_labels_swing_setup() -> None:
    text = format_telegram_signal_message(TelegramAlertType.WATCHLIST, _message(mode="swing"))

    assert "BTCUSDT · LONG · SWING" in text
    assert "BTCUSDT · LONG · SCALP" not in text


def test_combined_source_modes_need_valid_confluence_flag() -> None:
    selected_scalp = _message(
        mode="scalp",
        signal_context=SignalMessageContext(
            symbol="BTCUSDT",
            direction="long",
            primary_mode="scalp",
            source_modes=("scalp", "swing"),
            confluence_valid=False,
        ),
    )
    confluence = _message(
        mode="scalp",
        signal_context=SignalMessageContext(
            symbol="BTCUSDT",
            direction="long",
            primary_mode="scalp",
            secondary_modes=("swing",),
            source_modes=("scalp", "swing"),
            confluence_valid=True,
        ),
    )

    assert "BTCUSDT · LONG · SCALP" in format_telegram_signal_message(TelegramAlertType.WATCHLIST, selected_scalp)
    assert "BTCUSDT · LONG · SCALP/SWING" in format_telegram_signal_message(TelegramAlertType.WATCHLIST, confluence)


def test_a_plus_grade_and_score_are_shown() -> None:
    text = format_telegram_signal_message(TelegramAlertType.WATCHLIST, _message(quality="A+", quality_score=96))

    assert "Grade: A+ | Score: 96 | RR: 2.92R" in text


def test_long_and_short_invalidation_are_direction_aware() -> None:
    long_text = format_telegram_signal_message(TelegramAlertType.WATCHLIST, _message(invalidation_reason=NA))
    short_text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(direction="short", stop_loss=Decimal("105"), invalidation_reason=NA),
    )

    assert "Invalid if price body-closes and accepts below 95." in long_text
    assert "Invalid if price body-closes and accepts above 105." in short_text


def test_missing_invalidation_level_does_not_invent_number() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(stop_loss=NA, invalidation_reason=NA, watchlist_invalidation_reason=NA),
    )

    assert "Invalidation: N/A." in text
    assert "95" not in text


def test_structured_why_points_replace_generic_fallback_text() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(
            structure_reason="Setup quality does not provide enough deterministic edge.",
            signal_context=SignalMessageContext(
                symbol="BTCUSDT",
                direction="long",
                primary_mode="scalp",
                source_modes=("scalp",),
                why_it_matters_points=(
                    "Downside liquidity was swept before the setup mapped.",
                    "15m BOS/CHoCH confirms the structure shift.",
                    "Pullback is mapped into an OB/FVG reaction zone.",
                    "Pullback depth aligns with the fib pocket.",
                    "Target integrity leaves a clean RR path toward TP2.",
                ),
            ),
        ),
    )

    assert "Downside liquidity was swept." in text
    assert "15m structure shifted bullish, with pullback aligned inside OB/FVG + fib reaction zone." in text
    assert "Setup quality does not provide enough deterministic edge" not in text
    assert "clean RR path" not in text
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
    assert format_telegram_rr(Decimal("2.91918017")) == "2.92R"
    assert format_telegram_rr(Decimal("3.000000")) == "3.00R"
    assert format_telegram_rr(Decimal("3.246")) == "3.25R"
    assert format_telegram_rr(NA) == NA
    assert format_telegram_rr("not numeric") == NA
