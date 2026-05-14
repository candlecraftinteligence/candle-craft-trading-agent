from __future__ import annotations

import asyncio
import json
from decimal import Decimal
from typing import Any

import httpx

from app.agents.alert_agent import AlertAgent, AlertChannel, AlertStatus
from app.agents.trade_idea import TradeIdeaResult, create_trade_idea
from app.alerts.templates import CANDLE_CRAFT_SIGNATURE, split_message


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _base_idea(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "symbol": "BTCUSDT",
        "exchange": "Binance",
        "market_type": "perpetual",
        "direction": "long",
        "timeframe": "1h",
        "setup_type": "liquidity_sweep_reclaim",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop_loss": Decimal("95"),
        "take_profit_targets": (Decimal("112"), Decimal("120")),
        "invalidation": "Price closes below the reclaimed range low.",
        "opportunity_score": Decimal("88"),
        "opportunity_grade": "A",
        "opportunity_decision": "alert_candidate",
        "risk_approved": True,
        "best_rr": Decimal("3.5"),
        "technical_summary": "Bullish sweep and reclaim at support",
        "derivatives_summary": "Open interest confirms participation without crowding",
        "confirmed_facts": ("Range low reclaimed",),
        "missing_data": (),
        "unverified_data": (),
        "cancel_condition": "Cancel if price accepts below the entry zone before trigger.",
    }
    data.update(overrides)
    return data


def _idea(**overrides: object) -> TradeIdeaResult:
    return create_trade_idea(_base_idea(**overrides))


def test_formats_valid_trade_idea() -> None:
    message = AlertAgent().format(_idea())

    assert "🟢 Trade Setup Alert — BTCUSDT" in message
    assert "Direction: long" in message
    assert "Exchange: Binance" in message
    assert "Market type: perpetual" in message
    assert "Entry zone: 100.00000000 - 102.00000000" in message
    assert "Stop loss: 95.00000000" in message
    assert "Take profits: TP1: 112.00000000; TP2: 120.00000000" in message
    assert "Risk warning: This is not financial advice." in message


def test_dry_run_does_not_call_telegram() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise AssertionError("Telegram transport should not be called in dry-run mode")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
        agent = AlertAgent(telegram_http_client=client)
        result = await agent.send(
            {
                "trade_idea": _idea(),
                "channel": "telegram",
                "telegram_bot_token": "token",
                "telegram_chat_id": "chat",
            }
        )
        await client.aclose()

        assert result.status == AlertStatus.DRY_RUN
        assert result.delivery_results[0].status == AlertStatus.DRY_RUN

    run(scenario())


def test_dry_run_returns_formatted_message() -> None:
    result = run(AlertAgent().send({"trade_idea": _idea()}))

    assert result.status == "dry_run"
    assert result.dry_run is True
    assert "Trade Setup Alert" in result.formatted_message
    assert result.message_parts == (result.formatted_message,)


def test_missing_telegram_token_rejects_live_send() -> None:
    result = run(
        AlertAgent().send(
            {
                "trade_idea": _idea(),
                "channel": "telegram",
                "dry_run": False,
                "telegram_chat_id": "chat",
            }
        )
    )

    assert result.status == "failed"
    assert "telegram_bot_token" in str(result.delivery_results[0].error)


def test_missing_telegram_chat_id_rejects_live_send() -> None:
    result = run(
        AlertAgent().send(
            {
                "trade_idea": _idea(),
                "channel": "telegram",
                "dry_run": False,
                "telegram_bot_token": "token",
            }
        )
    )

    assert result.status == "failed"
    assert "telegram_chat_id" in str(result.delivery_results[0].error)


def test_mocked_telegram_success() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            payload = json.loads(request.content.decode())
            assert request.url.path == "/bottoken/sendMessage"
            assert payload["chat_id"] == "chat"
            assert "Trade Setup Alert" in payload["text"]
            assert CANDLE_CRAFT_SIGNATURE in payload["text"]
            return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
        result = await AlertAgent(telegram_http_client=client).send(
            {
                "trade_idea": _idea(),
                "channel": "telegram",
                "dry_run": False,
                "telegram_bot_token": "token",
                "telegram_chat_id": "chat",
            }
        )
        await client.aclose()

        assert len(requests) == 1
        assert result.status == "sent"
        assert result.delivery_results[0].status == "sent"
        assert result.delivery_results[0].http_status == 200

    run(scenario())


def test_mocked_telegram_non_200_failure() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, json={"ok": False, "description": "Too Many Requests"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
        result = await AlertAgent(telegram_http_client=client).send(
            {
                "trade_idea": _idea(),
                "channel": "telegram",
                "dry_run": False,
                "telegram_bot_token": "token",
                "telegram_chat_id": "chat",
            }
        )
        await client.aclose()

        assert result.status == "failed"
        assert result.delivery_results[0].http_status == 429
        assert result.delivery_results[0].rate_limited is True
        assert "rate limited" in result.delivery_results[0].detail

    run(scenario())


def test_mocked_telegram_timeout_failure() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("timeout")

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
        result = await AlertAgent(telegram_http_client=client).send(
            {
                "trade_idea": _idea(),
                "channel": "telegram",
                "dry_run": False,
                "telegram_bot_token": "token",
                "telegram_chat_id": "chat",
            }
        )
        await client.aclose()

        assert result.status == "failed"
        assert "timed out" in result.delivery_results[0].detail

    run(scenario())


def test_missing_data_preserved_as_na() -> None:
    message = AlertAgent().format(
        _idea(
            technical_summary=None,
            missing_data=("funding: N/A",),
        )
    )

    assert "Reason for trade: Technical context: N/A." in message
    assert "Missing data: funding: N/A; technical_summary: N/A" in message


def test_unverified_data_preserved_as_unverified() -> None:
    message = AlertAgent().format(_idea(unverified_data=("funding: Unverified", "open_interest: Unverified")))

    assert "Unverified data: funding: Unverified; open_interest: Unverified" in message


def test_risk_warning_included() -> None:
    message = AlertAgent().format(_idea())

    assert "Risk warning:" in message
    assert "This is not financial advice." in message


def test_candle_craft_signature_included() -> None:
    message = AlertAgent().format(_idea())

    assert message.endswith(CANDLE_CRAFT_SIGNATURE)


def test_long_message_split_behavior() -> None:
    long_message = AlertAgent().format(_idea(confirmed_facts=tuple(f"Fact {index}" for index in range(80))))
    parts = split_message(long_message, max_length=300)

    assert len(parts) > 1
    assert all(len(part) <= 300 for part in parts)
    assert parts[-1].endswith(CANDLE_CRAFT_SIGNATURE)


def test_deduplication_key_is_marked_in_output() -> None:
    result = run(
        AlertAgent().send(
            {
                "trade_idea": _idea(),
                "deduplication_key": "BTCUSDT-1h-liquidity_sweep_reclaim",
            }
        )
    )

    assert result.deduplication_key == "BTCUSDT-1h-liquidity_sweep_reclaim"
    assert result.deduplication_marked is True
    assert result.status == "dry_run"
