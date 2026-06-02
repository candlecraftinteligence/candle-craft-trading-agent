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
    assert "Confirmation Needed:\n5m BOS/CHoCH." in text
    assert "System:\nWatchlist only. No active signal yet." in text


def test_signal_confirmed_formatter_includes_required_fields() -> None:
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, _message())

    assert "CANDLE CRAFT SIGNAL CONFIRMED" in text
    assert "Status:\nCONFIRMED" in text
    assert "Entry Zone:\n100 \u2013 102" in text
    assert "\u2022 HTF Bias: bullish" in text
    assert "\u2022 OI/Funding/CVD: Funding normal / OI rising" in text
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
        _message(tp3=None, ob_fvg_status="", derivatives_status=NA),
    )

    assert "TP3: N/A" in text
    assert "\u2022 OB/FVG: N/A" in text
    assert "\u2022 OI/Funding/CVD: N/A" in text
