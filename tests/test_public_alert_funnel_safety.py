from __future__ import annotations

from app.analytics.public_alert_funnel import normalize_public_block_reasons
from app.alerts.telegram_lifecycle import (
    TelegramEligibilityContext,
    _public_watchlist_gate_result,
    telegram_signal_message_from_symbol,
)
from tests.test_telegram_lifecycle_delivery_phase42 import _public_target_caution_symbol


def test_public_block_normalization_is_reporting_only_for_gate_result() -> None:
    symbol = _public_target_caution_symbol(rr="2.79", signal_id="target-caution-reporting-only")
    message = telegram_signal_message_from_symbol(symbol)
    before = _public_watchlist_gate_result(symbol, message, TelegramEligibilityContext())

    categories = normalize_public_block_reasons("blocked:" + "; ".join(before.blocking_reasons))
    after = _public_watchlist_gate_result(symbol, message, TelegramEligibilityContext())

    assert "TARGET_CAUTION_RR_BELOW_2_8" in categories
    assert before == after
    assert before.allowed is False
    assert "public_block_target_caution_rr_below_2_8" in before.blocking_reasons
