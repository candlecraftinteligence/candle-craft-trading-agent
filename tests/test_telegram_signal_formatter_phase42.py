from __future__ import annotations

from decimal import Decimal

from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import (
    FOOTER,
    HEADER_PREFIX,
    TelegramAlertType,
    TelegramSignalMessage,
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
    assert "Watch Zone:\n100 \u2013 102" in text
    assert "Current Context:\n" in text
    assert "Needs Next:\n1. N/A \u2014 waiting for the next lifecycle update from the core engine." in text
    assert "Potential Plan:\nEntry: 100 \u2013 102" in text
    assert "System:\nWatchlist only. No active signal yet." in text


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
        TelegramAlertType.LIMIT_HIT: ("Status:\nLIMIT HIT", "Price has reached the planned entry zone."),
        TelegramAlertType.TP1_HIT: ("Status:\nTP1 HIT", "First target reached."),
        TelegramAlertType.TP2_HIT: ("Status:\nTP2 HIT", "Second target reached."),
        TelegramAlertType.TP3_HIT: ("Status:\nTP3 HIT", "Final target reached."),
        TelegramAlertType.SL_HIT: ("Status:\nSL HIT", "Stop level reached."),
        TelegramAlertType.INVALIDATED: ("Status:\nINVALIDATED", "Setup removed from active signal tracking."),
        TelegramAlertType.EXPIRED: ("Status:\nEXPIRED", "Setup did not confirm within the valid lifecycle window."),
    }

    for alert_type, required in expected.items():
        text = format_telegram_signal_message(alert_type, _message())
        for snippet in required:
            assert snippet in text
        assert "Signal ID:\nsig-001" in text


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
