from __future__ import annotations

from decimal import Decimal

from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import (
    FOOTER,
    HEADER_PREFIX,
    TelegramAlertType,
    TelegramSignalMessage,
    format_telegram_price,
    format_telegram_rr,
    format_telegram_signal_message,
)


def _message(**overrides: object) -> TelegramSignalMessage:
    data = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "signal_id": "sig-001",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop_loss": Decimal("95"),
        "tp1": Decimal("110"),
        "tp2": Decimal("115"),
        "tp3": Decimal("120"),
        "planned_rr": Decimal("3"),
        "structure_reason": "Sweep and reclaim into valid pullback.",
        "confirmation_needed": "5m BOS/CHoCH.",
        "invalidation_reason": "Invalid if price accepts below 95.",
        "confluence": (
            "Structure is confirmed by a clean LTF BOS/CHoCH. "
            "Price is reacting from a valid OB reaction. Volume is candle-estimated. "
            "Derivatives are neutral: funding is normal while open interest is falling, "
            "so follow-through still needs structure to hold."
        ),
        "htf_bias": "bullish",
        "ob_fvg_status": "OB valid",
        "volume_status": "POC aligned",
        "derivatives_status": "Funding normal / OI rising",
    }
    data.update(overrides)
    return TelegramSignalMessage(**data)


def test_all_phase42_messages_start_with_header_and_end_with_footer() -> None:
    for alert_type in TelegramAlertType:
        text = format_telegram_signal_message(alert_type, _message())

        assert text.startswith(HEADER_PREFIX)
        assert text.endswith(FOOTER)


def test_watchlist_formatter_includes_required_fields() -> None:
    text = format_telegram_signal_message(TelegramAlertType.WATCHLIST, _message())

    assert "CANDLE CRAFT WATCHLIST" in text
    assert "Status:\nWATCHLIST" in text
    assert "Limit Zone:\n100 \u2013 102" in text
    assert "Watch Zone:" not in text
    assert "Current Context:\n" in text
    assert "Needs Next:\n1. Price must trade into the Limit Zone." in text
    assert "Potential Plan:\nEntry: 100 \u2013 102" in text
    assert "Potential Targets:\nTP1: 110\nTP2: 115\nTP3: 120" in text
    assert "System:\nWatchlist only. No active signal yet." in text


def test_watchlist_needs_next_excludes_internal_scanner_language() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(
            needs_next=(
                "Trust Meter or final confluence must reach the required threshold.",
                "RR and final quality gates must pass before confirmation.",
                "Price must trade into the Limit Zone.",
            ),
            planned_rr=Decimal("2.6"),
            min_rr=Decimal("2.7"),
            watchlist_invalidation_reason="Watchlist invalidates if price accepts below 95.",
        ),
    )

    needs_next = text.split("Needs Next:\n", 1)[1].split("\n\nPotential Plan:", 1)[0]
    for forbidden in (
        "Trust Meter",
        "RR",
        "risk/reward",
        "score",
        "scoring",
        "opportunity score",
        "final confluence threshold",
        "hard rejection",
    ):
        assert forbidden.lower() not in needs_next.lower()
    assert "Price must trade into the Limit Zone." in needs_next
    assert "Planned RR: 2.6R \u2014 watchlist only, final RR must improve to \u22652.7R before confirmation." in text
    assert "Watchlist invalidates if price accepts below 95." in text


def test_signal_confirmed_formatter_includes_required_fields() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())

    assert "CANDLE CRAFT SIGNAL CONFIRMED" in text
    assert "Status:\nCONFIRMED" in text
    assert "Entry Zone:\n100 \u2013 102" in text
    assert "Confluence:\nStructure is confirmed by a clean LTF BOS/CHoCH." in text
    assert "funding is normal while open interest is falling" in text
    assert "System:\nAlert only. Trade managed manually by Adam." in text


def test_lifecycle_update_formatters_include_required_fields() -> None:
    expected = {
        TelegramAlertType.LIMIT_HIT: ("Status:\nLIMIT ZONE HIT", "Price has reached the planned entry zone."),
        TelegramAlertType.TP1_HIT: ("Status:\nTP1 HIT", "First target reached."),
        TelegramAlertType.TP2_HIT: ("Status:\nTP2 HIT", "Second target reached."),
        TelegramAlertType.TP3_HIT: ("Status:\nTP3 HIT", "Final target reached."),
        TelegramAlertType.SL_HIT: ("Status:\nSL HIT", "Stop level reached."),
        TelegramAlertType.INVALIDATED: ("Status:\nINVALIDATED", "Watchlist removed from active tracking."),
        TelegramAlertType.EXPIRED: ("Status:\nEXPIRED", "Watchlist expired. No active signal."),
        TelegramAlertType.NO_LONGER_TRACKING: ("Status:\nNO LONGER TRACKING", "Watchlist removed from active tracking."),
    }

    for alert_type, required in expected.items():
        text = format_telegram_signal_message(alert_type, _message())
        for snippet in required:
            assert snippet in text
        assert "Signal ID:\nsig-001" in text


def test_watchlist_outcome_update_formatters_use_phase50_wording() -> None:
    cases = {
        TelegramAlertType.LIMIT_HIT: ("Status:\nLIMIT ZONE HIT", "Price has reached the planned watchlist Limit Zone."),
        TelegramAlertType.TP1_HIT: ("Status:\nTP1 HIT", "First target reached from the watchlist plan."),
        TelegramAlertType.TP2_HIT: ("Status:\nTP2 HIT", "Second target reached from the watchlist plan."),
        TelegramAlertType.TP3_HIT: ("Status:\nTP3 HIT", "Watchlist outcome tracking completed."),
        TelegramAlertType.SL_HIT: ("Status:\nSL HIT", "Watchlist outcome tracking closed."),
    }
    for alert_type, snippets in cases.items():
        text = format_telegram_signal_message(alert_type, _message(watchlist_outcome=True))

        assert text.startswith(HEADER_PREFIX)
        assert text.endswith(FOOTER)
        assert "Signal ID:\nsig-001" in text
        assert "Setup Type" not in text
        assert "Decimal(" not in text
        assert "{" not in text and "}" not in text
        assert "automatic execution" not in text.lower()
        for snippet in snippets:
            assert snippet in text


def test_terminal_update_formatters_are_short_and_clean() -> None:
    for alert_type in (
        TelegramAlertType.INVALIDATED,
        TelegramAlertType.EXPIRED,
        TelegramAlertType.NO_LONGER_TRACKING,
    ):
        text = format_telegram_signal_message(alert_type, _message())

        assert text.startswith(HEADER_PREFIX)
        assert text.endswith(FOOTER)
        assert "Reason:\nInvalid if price accepts below 95." in text
        assert "Setup Type" not in text
        assert "manual execution only" not in text
        assert "Decimal(" not in text
        assert "Lifecycle:" not in text
        assert "Research:" not in text


def test_public_formatter_omits_setup_type_and_manual_execution_status_suffix() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())

    assert "Setup Type" not in text
    assert "manual execution only" not in text
    assert "Status:\nCONFIRMED" in text


def test_unavailable_fields_render_as_na() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(tp3=None, planned_rr=None, ob_fvg_status="", derivatives_status=NA),
    )

    assert "TP3: N/A" in text
    assert "Planned RR:\nN/A" in text
    assert "N/AR" not in text
    assert "Confluence:" in text


def test_watchlist_formatter_allows_incomplete_plan_fields() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(
            entry_low=NA,
            entry_high=NA,
            stop_loss=NA,
            tp1=NA,
            tp2=NA,
            tp3=NA,
            planned_rr=NA,
            current_context=(
                "Price has produced a clean sweep and LTF BOS/CHoCH, but the setup is not confirmed yet "
                "because no valid OB/FVG pullback zone was found inside the displacement impulse."
            ),
            needs_next=(
                "A valid OB or FVG must be found inside the displacement impulse.",
                "The OB/FVG zone must overlap the preferred fib pullback zone.",
                "RR and final quality gates must still pass after a valid zone is found.",
            ),
            watchlist_invalidation_reason=(
                "Watchlist invalidates if the sweep/BOS/CHoCH context fails, expires, or price breaks "
                "the structure that created the watchlist candidate."
            ),
        ),
    )

    assert "Entry: N/A \u2013 N/A" in text
    assert "SL: N/A" in text
    assert "Planned RR: N/A" in text
    assert "System:\nWatchlist only. No active signal yet." in text
    for forbidden in ("Setup Type", "Signal confirmed", "Decimal(", "{", "}", "True", "False"):
        assert forbidden not in text


def test_confluence_formatter_does_not_dump_raw_internal_data() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(confluence="Derivatives context is N/A.", planned_rr=Decimal("3.20000000")),
    )

    assert "Confluence:\nDerivatives context is N/A." in text
    for forbidden in ("Decimal(", "{", "}", "true", "false", "funding_rate:", "open_interest:"):
        assert forbidden not in text


def test_planned_rr_is_rounded_cleanly() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(planned_rr=Decimal("3.23456789")),
    )

    assert "Planned RR:\n3.2R" in text
    assert "3.23456789R" not in text


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


def test_watchlist_formats_clean_targets_and_below_min_rr_warning() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(
            entry_low=Decimal("71.407944"),
            entry_high=Decimal("71.675"),
            stop_loss=Decimal("70.77363571"),
            tp1=Decimal("72.95123"),
            tp2=Decimal("73.252056"),
            tp3=Decimal("73.8167"),
            planned_rr=Decimal("2.61918017"),
            min_rr=Decimal("2.7"),
        ),
    )

    assert "Limit Zone:\n71.41 \u2013 71.68" in text
    assert "Entry: 71.41 \u2013 71.68" in text
    assert "SL: 70.77" in text
    assert "Potential Targets:\nTP1: 72.95\nTP2: 73.25\nTP3: 73.82" in text
    assert "Planned RR: 2.6R \u2014 watchlist only, final RR must improve to \u22652.7R before confirmation." in text
    assert "CANDLE CRAFT SIGNAL CONFIRMED" not in text
    assert "73.252056" not in text


def test_watchlist_formats_na_rr_validation_warning_when_trackable() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.WATCHLIST,
        _message(planned_rr=NA),
    )

    assert "Planned RR: N/A \u2014 final RR must validate before confirmation." in text


def test_confirmed_formats_clean_targets_and_rr() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(
            tp1=Decimal("72.95123"),
            tp2=Decimal("73.252056"),
            tp3=Decimal("73.8167"),
            planned_rr=Decimal("3.246"),
        ),
    )

    assert "Take Profits:\nTP1: 72.95\nTP2: 73.25\nTP3: 73.82" in text
    assert "Planned RR:\n3.2R" in text
    assert "73.252056" not in text


def test_invalidation_section_does_not_contain_rejection_text() -> None:
    text = format_telegram_signal_message(
        TelegramAlertType.SIGNAL_CONFIRMED,
        _message(
            invalidation_reason=(
                "Signal invalidates if price closes below 95 and accepts below the entry reclaim zone, "
                "confirming that the bullish continuation structure has failed."
            )
        ),
    )

    invalidation = text.split("Invalidation:\n", 1)[1].split("\n\nSignal ID:", 1)[0]
    assert "Technical score" not in invalidation
    assert "Opportunity score" not in invalidation
    assert invalidation.endswith(".")
