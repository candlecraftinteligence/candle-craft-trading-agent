from __future__ import annotations

import asyncio
from dataclasses import replace
from decimal import Decimal
import json

import httpx
import pytest

from app.alerts.telegram import send_telegram_message_part
from app.alerts.telegram_rich_message import RICH_MESSAGE_PREFIX, pack_rich_message, rich_message_html
from app.alerts.telegram_routing import TelegramMessageType
from app.alerts.telegram_sender import TelegramSender
from app.alerts.templates import split_message
from app.core.config import Settings
from app.formatters.telegram_precision_brief import tradingview_chart_url
from app.formatters.telegram_signal_formatter import (
    PUBLIC_TRADE_MAP_RISK_WARNING, SignalEdgeEvidence, SignalMessageContext,
    TelegramAlertType, TelegramSignalMessage, format_telegram_signal_message,
)


def message(**changes):
    data = dict(symbol="QNTUSDT", direction="long", market_exchange="binance",
                entry_low=Decimal("255.33"), entry_high=Decimal("256.20"),
                stop_loss=Decimal("250.06"), tp1=Decimal("258.99"),
                tp2=Decimal("271.41"), tp3=Decimal("276.38"),
                confirmation_needed="5m close above 256.8.",
                edge_evidence=SignalEdgeEvidence(
                    sweep_present=True, sweep_direction="bullish", swept_level=Decimal("254"),
                    sweep_wick=Decimal("253.5"), structure_present=True,
                    structure_timeframe="5m", structure_kind="CHoCH", structure_direction="bullish",
                    structure_level=Decimal("256.8"), structure_close=Decimal("257"),
                    selected_zone_type="FVG", entry_low=Decimal("255.33"), entry_high=Decimal("256.20")))
    data.update(changes)
    return TelegramSignalMessage(**data)


def render(alert=TelegramAlertType.SIGNAL_CONFIRMED, **changes):
    return rich_message_html(format_telegram_signal_message(alert, message(**changes), presentation="precision"))


def test_precise_evidence_levels_and_native_controls():
    html = render()
    assert "Sweep: wick 253.5 below swing low 254." in html
    assert "5m CHoCH: candle closed 257 above 256.8." in html
    assert "closed back" not in html  # A sweep flag alone is not reclaim-close evidence.
    assert '<table compact>' in html and '<details><summary>Evidence details</summary>' in html
    assert html.count(PUBLIC_TRADE_MAP_RISK_WARNING) == 1
    assert html.index("Invalidation:") < html.index("<details>")
    assert "hard stop on price contact at or below 250.06" in html
    assert "QNTUSDT.P" in html and "BINANCE%3A" in html
    assert 'type="copy_text"' in html and "Copy levels" in html
    assert "Hunt" not in html and "Score" not in html and "✓" not in html


def test_short_evidence_uses_upside_sweep_and_close_below():
    evidence = replace(message().edge_evidence, sweep_direction="bearish", swept_level=Decimal("260"),
                       sweep_wick=Decimal("261"), structure_direction="bearish",
                       structure_level=Decimal("258"), structure_close=Decimal("257"))
    html = render(direction="short", edge_evidence=evidence)
    assert "wick 261 above swing high 260" in html and "closed 257 below 258" in html
    assert "at or above 250.06" in html


def test_missing_and_contradictory_evidence_never_invents_confirmation():
    html = render(edge_evidence=None)
    assert "Setup evidence: N/A" in html and "Sweep:" not in html and "CHoCH" not in html
    evidence = replace(message().edge_evidence, sweep_wick=Decimal("260"), structure_close=Decimal("250"))
    html = render(edge_evidence=evidence)
    assert html.count("Unverified") >= 3
    assert "candle closed" not in html and "closed back" not in html


@pytest.mark.parametrize("alert", [TelegramAlertType.WATCHLIST, TelegramAlertType.SETUP_TRIGGERED])
def test_pending_states_and_conditions_remain_explicit(alert):
    html = render(alert)
    assert "CONFIRMATION" in html and "5m close above 256.8." in html
    assert "<i>CONFIRMED" not in html


def test_confirmed_message_with_pending_context_is_not_promoted_by_formatter():
    html = render(actionability_state="CONFIRMATION_REQUIRED")
    assert "<i>CONFIRMED" not in html


def test_effective_context_levels_override_message_without_changing_geometry():
    context = SignalMessageContext(entry_low=Decimal("100"), entry_high=Decimal("101"),
                                   stop_loss=Decimal("90"), tp1=Decimal("110"), tp2=Decimal("120"), tp3=Decimal("130"))
    html = render(signal_context=context)
    assert "100 – 101" in html and "255.33" not in html


def test_source_text_is_escaped_in_html_and_copy_attribute():
    html = render(TelegramAlertType.WATCHLIST, confirmation_needed='<a href="https://evil.test">fake</a> & wait.')
    assert "&lt;a href=&quot;https://evil.test&quot;&gt;" in html
    assert '<a href="https://evil.test">' not in html


def test_risk_and_target_caution_are_visible_not_collapsed():
    html = render(target_failure_severity="target_caution_actionable", target_warning_reason="TP2 inside range.")
    assert html.index("TP1 priority") < html.index("<details>")
    assert "TP2 inside range." in html
    assert html.count(PUBLIC_TRADE_MAP_RISK_WARNING) == 1


@pytest.mark.parametrize("exchange,expected", [("binance", "BINANCE"), ("bybit", "BYBIT")])
def test_exact_perpetual_chart_mapping(exchange, expected):
    assert tradingview_chart_url("QNTUSDT", exchange) == f"https://www.tradingview.com/chart/?symbol={expected}%3AQNTUSDT.P"


@pytest.mark.parametrize("symbol,exchange", [("QNTUSDT", "N/A"), ("QNTUSDT", "other"),
    ("BTCUSDT&symbol=EVIL", "binance"), ("ETH/BTC", "binance"), ("BTCUSDT.P", "binance")])
def test_no_guessing_or_injected_symbols(symbol, exchange):
    assert tradingview_chart_url(symbol, exchange) is None
    assert 'type="url"' not in render(symbol=symbol, market_exchange=exchange)


def test_legacy_is_the_explicit_rollout_default():
    assert Settings(_env_file=None).telegram_signal_presentation == "legacy"
    assert not format_telegram_signal_message("SIGNAL_CONFIRMED", message()).startswith(RICH_MESSAGE_PREFIX)
    with pytest.raises(ValueError):
        Settings(_env_file=None, telegram_signal_presentation="typo")


@pytest.mark.parametrize("alert", list(TelegramAlertType))
def test_all_precision_events_fit_one_durable_part(alert):
    payload = format_telegram_signal_message(alert, message(), presentation="precision")
    assert len(payload) <= 4096
    assert split_message(payload) == (payload,)


def test_lifecycle_updates_are_short_and_do_not_claim_realized_profit_or_moved_stop():
    for alert in (TelegramAlertType.LIMIT_HIT, TelegramAlertType.TP1_HIT, TelegramAlertType.TP2_HIT,
                  TelegramAlertType.TP3_HIT, TelegramAlertType.SL_HIT, TelegramAlertType.BREAKEVEN):
        html = render(alert)
        assert len(html) < 700 and "Unverified" in html
        assert "secured" not in html and "Move SL" not in html and "Hunt" not in html
    assert "Next tracked: TP2 271.41" in render(TelegramAlertType.TP1_HIT)
    assert "Zone contact only" in render(TelegramAlertType.LIMIT_HIT)


def test_coalesced_targets_preserve_recorded_milestones():
    assert "Recorded: TP1 · TP2 · TP3" in render(TelegramAlertType.TP3_HIT, coalesced_milestones=("TP1_HIT", "TP2_HIT", "TP3_HIT"))


def test_oversized_rich_payloads_are_never_split_into_malformed_sends():
    payload = pack_rich_message("<h2>test</h2>")
    with pytest.raises(ValueError):
        split_message(payload, 10)
    with pytest.raises(ValueError):
        pack_rich_message("a" * 4096)


def test_long_escaped_source_fields_stay_within_durable_part_limit():
    payload = format_telegram_signal_message("WATCHLIST", message(confirmation_needed="&" * 3000,
        htf_bias="<" * 3000, derivatives_status='"' * 3000), presentation="precision")
    assert len(payload) <= 4096
    rich_message_html(payload)


def test_transport_sends_reserved_rich_body_exactly_once():
    payload = format_telegram_signal_message("SIGNAL_CONFIRMED", message(), presentation="precision")
    seen = []
    async def run():
        def handler(request):
            seen.append(request)
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 23, "chat": {"id": -1001}}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test") as client:
            return await send_telegram_message_part(bot_token="test-token", chat_id="-1001", message=payload, http_client=client)
    result = asyncio.run(run())
    assert result["delivery_state"] == "SENT" and len(seen) == 1
    assert seen[0].url.path.endswith("/sendRichMessage")
    body = json.loads(seen[0].content)
    assert body == {"chat_id": "-1001", "rich_message": {"html": rich_message_html(payload), "skip_entity_detection": True}}


@pytest.mark.parametrize("status,expected", [(400, "FAILED_FINAL"), (429, "RETRYABLE"), (500, "RETRYABLE")])
def test_rich_delivery_preserves_rejection_classification_and_never_falls_back_after_send(status, expected):
    seen = []
    async def run():
        def handler(request):
            seen.append(request)
            return httpx.Response(status, json={"ok": False})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test") as client:
            return await send_telegram_message_part(bot_token="test-token", chat_id="-1001", message=pack_rich_message("<b>test</b>"), http_client=client)
    assert asyncio.run(run())["delivery_state"] == expected
    assert len(seen) == 1


def test_rich_timeout_is_uncertain_with_no_second_send():
    seen = []
    async def run():
        def handler(request):
            seen.append(request)
            raise httpx.ReadTimeout("test timeout")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test") as client:
            return await send_telegram_message_part(bot_token="test-token", chat_id="-1001", message=pack_rich_message("<b>test</b>"), http_client=client)
    assert asyncio.run(run())["delivery_state"] == "UNCERTAIN"
    assert len(seen) == 1


def test_invalid_rich_parts_fail_before_transmission():
    async def run():
        def handler(request):
            pytest.fail("Malformed rich message must never be sent")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test") as client:
            for payload, count in ((RICH_MESSAGE_PREFIX, 1), (pack_rich_message("<b>test</b>"), 2)):
                result = await send_telegram_message_part(bot_token="test-token", chat_id="-1001", message=payload, total_parts=count, http_client=client)
                assert result["delivery_state"] == "FAILED_FINAL"
    asyncio.run(run())


@pytest.mark.parametrize("enabled,dry_run", [(False, False), (True, True)])
def test_rich_messages_cannot_bypass_disabled_or_dry_run_policy(enabled, dry_run):
    async def run():
        def handler(request):
            pytest.fail("Delivery guard must run before rich-message transmission")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test") as client:
            sender = TelegramSender(bot_token="test-token", chat_id="-1001", signals_enabled=enabled,
                                    dry_run=dry_run, http_client=client)
            return await sender.send_text(pack_rich_message("<b>test</b>"), message_type=TelegramMessageType.PUBLIC_SIGNAL)
    assert asyncio.run(run()).status == "skipped"
