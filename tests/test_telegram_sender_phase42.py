from __future__ import annotations

import asyncio
import logging

import httpx
import pytest

from app.alerts.telegram_sender import PublicWatchlistSendGuard, TelegramSender
from app.alerts.telegram_routing import TelegramDestination, TelegramMessageType
from app.core.config import Settings


def run(coro):
    return asyncio.run(coro)


@pytest.mark.parametrize("dry_run", [True, False])
def test_disabled_telegram_signals_do_not_call_api(monkeypatch, dry_run: bool) -> None:
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("Telegram API should not be called when signals are disabled")

    monkeypatch.setattr("app.alerts.telegram_sender.send_telegram_messages", fail_if_called)
    sender = TelegramSender(bot_token="secret-token", chat_id="chat", signals_enabled=False, dry_run=dry_run)

    result = run(sender.send_text("hello"))

    assert result.status == "skipped"
    assert result.error_message == "telegram_sending_disabled"


def test_missing_token_or_chat_id_does_not_crash() -> None:
    token_missing = run(TelegramSender(bot_token=None, chat_id="chat", signals_enabled=True, dry_run=False).send_text("hello"))
    chat_missing = run(TelegramSender(bot_token="token", chat_id=None, signals_enabled=True, dry_run=False).send_text("hello"))

    assert token_missing.status == "skipped"
    assert token_missing.error_message == "missing_telegram_credentials"
    assert chat_missing.status == "skipped"
    assert chat_missing.error_message == "missing_telegram_credentials"


def test_network_error_returns_failure_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timeout")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
    try:
        result = run(
            TelegramSender(
                bot_token="token",
                chat_id="chat",
                signals_enabled=True,
                dry_run=False,
                http_client=client,
                api_base_url="https://telegram.test",
            ).send_text("hello")
        )
    finally:
        run(client.aclose())

    assert result.status == "failed"
    assert "timed out" in result.error_message


def test_bad_telegram_response_returns_failure_safely() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"ok": False, "description": "Bad Request"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
    try:
        result = run(
            TelegramSender(
                bot_token="token",
                chat_id="chat",
                signals_enabled=True,
                dry_run=False,
                http_client=client,
                api_base_url="https://telegram.test",
            ).send_text("hello")
        )
    finally:
        run(client.aclose())

    assert result.status == "failed"
    assert "HTTP 400" in result.error_message


def test_token_is_not_logged_when_delivery_is_skipped(caplog) -> None:
    caplog.set_level(logging.WARNING)
    sender = TelegramSender(bot_token="super-secret-token", chat_id=None, signals_enabled=True, dry_run=False)

    result = run(sender.send_text("hello"))

    assert result.status == "skipped"
    assert "super-secret-token" not in caplog.text


def test_signal_channel_blocks_welcome_before_api_call(monkeypatch, caplog) -> None:
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("blocked signal-channel messages must not call Telegram API")

    monkeypatch.setattr("app.alerts.telegram_sender.send_telegram_messages", fail_if_called)
    caplog.set_level(logging.WARNING)
    sender = TelegramSender(
        bot_token="super-secret-token",
        chat_id="signal-channel",
        signals_enabled=True,
        dry_run=False,
        destination=TelegramDestination.SIGNAL_CHANNEL,
    )

    result = run(sender.send_text("welcome", message_type=TelegramMessageType.WELCOME))

    assert result.status == "skipped"
    assert result.error_message == "message_type_not_allowed_for_signal_channel"
    assert "super-secret-token" not in caplog.text


def test_public_watchlist_requires_reservation_guard_before_api_call(monkeypatch) -> None:
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("public WATCHLIST without reservation must not call Telegram API")

    monkeypatch.setattr("app.alerts.telegram_sender.send_telegram_messages", fail_if_called)
    sender = TelegramSender(bot_token="token", chat_id="chat", signals_enabled=True, dry_run=False)

    result = run(sender.send_text("watch", message_type=TelegramMessageType.PUBLIC_WATCHLIST))

    assert result.status == "skipped"
    assert result.error_message == "public_watchlist_missing_required_reservation"


def test_public_watchlist_reservation_guard_allows_api_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
    try:
        result = run(
            TelegramSender(
                bot_token="token",
                chat_id="chat",
                signals_enabled=True,
                dry_run=False,
                http_client=client,
                api_base_url="https://telegram.test",
            ).send_text(
                "watch",
                message_type=TelegramMessageType.PUBLIC_WATCHLIST,
                public_watchlist_guard=PublicWatchlistSendGuard(
                    event_key="PLAN|initial_watchlist",
                    reservation_id=1,
                    attempt_id="attempt-1",
                    event_id=1,
                ),
            )
        )
    finally:
        run(client.aclose())

    assert result.status == "sent"


def test_dry_run_from_settings_stops_before_http_and_is_not_sent() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    settings = Settings(
        _env_file=None,
        telegram_bot_token="valid-looking-test-token",
        telegram_public_chat_id="valid-looking-test-chat",
        telegram_signals_enabled=True,
        telegram_dry_run=True,
        local_manual_mode=True,
        order_execution_enabled=False,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
    try:
        sender = TelegramSender.from_settings(settings, http_client=client, api_base_url="https://telegram.test")
        result = run(sender.send_text("hello", message_type=TelegramMessageType.PUBLIC_SIGNAL))
    finally:
        run(client.aclose())

    assert requests == []
    assert result.status == "skipped"
    assert result.sent is False
    assert result.error_message == "telegram_dry_run_enabled"


def test_dry_run_false_from_settings_preserves_successful_http_send() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 1}})

    settings = Settings(
        _env_file=None,
        telegram_bot_token="valid-looking-test-token",
        telegram_public_chat_id="valid-looking-test-chat",
        telegram_signals_enabled=True,
        telegram_dry_run=False,
        local_manual_mode=True,
        order_execution_enabled=False,
    )
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
    try:
        sender = TelegramSender.from_settings(settings, http_client=client, api_base_url="https://telegram.test")
        result = run(sender.send_text("hello", message_type=TelegramMessageType.PUBLIC_SIGNAL))
    finally:
        run(client.aclose())

    assert len(requests) == 1
    assert result.status == "sent"
    assert result.sent is True
