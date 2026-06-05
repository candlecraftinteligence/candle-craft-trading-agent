from __future__ import annotations

import asyncio
import logging

import httpx

from app.alerts.telegram_sender import TelegramSender
from app.alerts.telegram_routing import TelegramDestination, TelegramMessageType


def run(coro):
    return asyncio.run(coro)


def test_disabled_telegram_signals_do_not_call_api(monkeypatch) -> None:
    async def fail_if_called(*args, **kwargs):
        raise AssertionError("Telegram API should not be called when signals are disabled")

    monkeypatch.setattr("app.alerts.telegram_sender.send_telegram_messages", fail_if_called)
    sender = TelegramSender(bot_token="secret-token", chat_id="chat", signals_enabled=False)

    result = run(sender.send_text("hello"))

    assert result.status == "skipped"
    assert result.error_message == "telegram_sending_disabled"


def test_missing_token_or_chat_id_does_not_crash() -> None:
    token_missing = run(TelegramSender(bot_token=None, chat_id="chat", signals_enabled=True).send_text("hello"))
    chat_missing = run(TelegramSender(bot_token="token", chat_id=None, signals_enabled=True).send_text("hello"))

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
    sender = TelegramSender(bot_token="super-secret-token", chat_id=None, signals_enabled=True)

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
        destination=TelegramDestination.SIGNAL_CHANNEL,
    )

    result = run(sender.send_text("welcome", message_type=TelegramMessageType.WELCOME))

    assert result.status == "skipped"
    assert result.error_message == "message_type_not_allowed_for_signal_channel"
    assert "super-secret-token" not in caplog.text
