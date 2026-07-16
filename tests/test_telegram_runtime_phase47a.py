from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx

from app.alerts.telegram_sender import (
    PUBLIC_DESTINATION_FALLBACK_WARNING,
    PUBLIC_DESTINATION_MISSING_WARNING,
    TelegramSender,
)
from app.alerts.telegram_routing import TelegramMessageType
from app.core.config import Settings
from app.storage.database import open_initialized_database
from app.telegram_admin import TelegramAdminCommandService, TelegramAdminConfig, process_telegram_admin_commands
from scripts import check_telegram_runtime, run_telegram_bot


def run(coro):
    return asyncio.run(coro)


class CaptureTelegramApi:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content.decode("utf-8"))
        self.payloads.append(payload)
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {
                    "message_id": len(self.payloads),
                    "chat": {"id": payload["chat_id"]},
                    "date": 1_717_200_000,
                },
            },
        )


class FakeCommandTransport:
    def __init__(
        self,
        updates: tuple[Mapping[str, Any], ...] = (),
        *,
        fail_get_message: str | None = None,
        interrupt_get: bool = False,
    ) -> None:
        self.updates = updates
        self.fail_get_message = fail_get_message
        self.interrupt_get = interrupt_get
        self.get_calls: list[dict[str, Any]] = []
        self.send_calls: list[dict[str, Any]] = []
        self.answer_callback_calls: list[dict[str, Any]] = []

    async def get_updates(self, *, bot_token: str, offset: int | None, limit: int, timeout: int):
        self.get_calls.append({"bot_token": bot_token, "offset": offset, "limit": limit, "timeout": timeout})
        if self.interrupt_get:
            raise KeyboardInterrupt
        if self.fail_get_message is not None:
            raise RuntimeError(self.fail_get_message)
        return self.updates

    async def send_message(
        self,
        *,
        bot_token: str,
        chat_id: str,
        message: str,
        reply_markup=None,
        photo_path=None,
        photo_url=None,
    ):
        self.send_calls.append(
            {
                "bot_token": bot_token,
                "chat_id": chat_id,
                "message": message,
                "reply_markup": reply_markup,
                "photo_path": photo_path,
                "photo_url": photo_url,
            }
        )
        return ({"status": "sent", "message_id": len(self.send_calls), "chat_id": chat_id},)

    async def answer_callback_query(self, *, bot_token: str, callback_query_id: str, text: str | None = None):
        self.answer_callback_calls.append(
            {"bot_token": bot_token, "callback_query_id": callback_query_id, "text": text}
        )
        return {"status": "sent"}


def _settings(**overrides: Any) -> Settings:
    defaults = {"_env_file": None, "order_execution_enabled": False}
    defaults.update(overrides)
    return Settings(**defaults)


def _update(update_id: int, chat_id: str, text: str) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def _screen_calls(transport: FakeCommandTransport) -> list[dict[str, Any]]:
    return [
        call
        for call in transport.send_calls
        if not (isinstance(call.get("reply_markup"), Mapping) and call["reply_markup"].get("remove_keyboard") is True)
    ]


def _seed_telegram_alert_attempts(db_path: Path) -> None:
    connection = open_initialized_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
                sent_at, telegram_status, message_hash, setup_quality_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "signal-confirmed-abcdef123456",
                "BTCUSDT",
                "long",
                "CONFIRMED",
                "SIGNAL_CONFIRMED",
                "CONFIRMED",
                "2026-06-04T12:00:00Z",
                "sent",
                "hash-sent",
                "B+",
            ),
        )
        connection.execute(
            """
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
                sent_at, telegram_status, message_hash, blocked_reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "blocked-signal",
                "ETHUSDT",
                "short",
                "CONFIRMED",
                "SIGNAL_CONFIRMED",
                "CONFIRMED",
                "2026-06-04T12:01:00Z",
                "blocked",
                "hash-blocked",
                "planned_rr_below_min",
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _send_with_settings(settings: Settings) -> tuple[CaptureTelegramApi, Any]:
    capture = CaptureTelegramApi()
    client = httpx.AsyncClient(transport=httpx.MockTransport(capture.handler), base_url="https://telegram.test")
    try:
        sender = TelegramSender.from_settings(settings, http_client=client, api_base_url="https://telegram.test")
        result = run(sender.send_text("hello", message_type=TelegramMessageType.PUBLIC_SIGNAL))
    finally:
        run(client.aclose())
    return capture, result


def test_public_lifecycle_alerts_use_public_chat_id_when_set() -> None:
    capture, result = _send_with_settings(
        _settings(
            telegram_bot_token="secret-token",
            telegram_chat_id="legacy-chat",
            telegram_admin_chat_id="admin-chat",
            telegram_public_chat_id="public-chat",
            telegram_public_channel_id="public-channel",
            telegram_signals_enabled=True,
            telegram_dry_run=False,
        )
    )

    assert result.status == "sent"
    assert capture.payloads[0]["chat_id"] == "public-chat"


def test_public_lifecycle_alerts_use_public_channel_when_public_chat_missing() -> None:
    capture, result = _send_with_settings(
        _settings(
            telegram_bot_token="secret-token",
            telegram_chat_id="legacy-chat",
            telegram_admin_chat_id="admin-chat",
            telegram_public_channel_id="public-channel",
            telegram_signals_enabled=True,
            telegram_dry_run=False,
        )
    )

    assert result.status == "sent"
    assert capture.payloads[0]["chat_id"] == "public-channel"


def test_public_lifecycle_alerts_use_legacy_chat_only_as_local_manual_fallback(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="app.alerts.telegram_sender")

    capture, result = _send_with_settings(
        _settings(
            telegram_bot_token="secret-token",
            telegram_chat_id="legacy-chat",
            telegram_admin_chat_id="admin-chat",
            telegram_signals_enabled=True,
            telegram_dry_run=False,
            local_manual_mode=True,
        )
    )

    assert result.status == "sent"
    assert capture.payloads[0]["chat_id"] == "legacy-chat"
    assert PUBLIC_DESTINATION_FALLBACK_WARNING in caplog.text
    assert "secret-token" not in caplog.text


def test_missing_public_destination_logs_warning_and_does_not_crash(caplog) -> None:
    caplog.set_level(logging.WARNING, logger="app.alerts.telegram_sender")

    sender = TelegramSender.from_settings(
        _settings(telegram_bot_token="secret-token", telegram_signals_enabled=True, telegram_dry_run=False)
    )
    result = run(sender.send_text("hello"))

    assert result.status == "skipped"
    assert result.error_message == "missing_telegram_credentials"
    assert PUBLIC_DESTINATION_MISSING_WARNING in caplog.text
    assert "secret-token" not in caplog.text


def test_admin_config_prefers_admin_chat_and_keeps_public_separate() -> None:
    config = TelegramAdminConfig.from_settings(
        _settings(
            telegram_bot_token="secret-token",
            telegram_chat_id="legacy-admin-chat",
            telegram_admin_chat_id="admin-chat",
            telegram_public_chat_id="public-chat",
            telegram_public_channel_id="public-channel",
            telegram_commands_enabled=True,
        )
    )
    fallback = TelegramAdminConfig.from_settings(
        _settings(
            telegram_bot_token="secret-token",
            telegram_chat_id="legacy-admin-chat",
            telegram_public_chat_id="public-chat",
            telegram_commands_enabled=True,
        )
    )

    assert config.admin_chat_id == "admin-chat"
    assert config.public_chat_id == "public-chat"
    assert config.public_channel_id == "public-channel"
    assert fallback.admin_chat_id == "legacy-admin-chat"
    assert fallback.public_chat_id == "public-chat"


def test_public_commands_and_reply_buttons_return_public_safe_screens(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_telegram_alert_attempts(db_path)
    service = TelegramAdminCommandService(project_root=tmp_path, database_path=db_path)
    transport = FakeCommandTransport()
    updates = (
        _update(1, "public-chat", "/start"),
        _update(2, "public-chat", "/menu"),
        _update(3, "public-chat", "/status"),
        _update(4, "public-chat", "/latest"),
        _update(5, "public-chat", "/about"),
        _update(6, "public-chat", "🐺 Menu"),
        _update(7, "public-chat", "📡 Status"),
        _update(8, "public-chat", "📊 Latest Alerts"),
        _update(9, "public-chat", "ℹ️ About"),
        _update(10, "public-chat", "unknown text"),
    )

    result = run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_chat_id="public-chat",
            ),
            command_service=service,
            transport=transport,
            state_path=tmp_path / "state.json",
            audit_path=tmp_path / "audit.jsonl",
            updates=updates,
        )
    )

    messages = [call["message"] for call in _screen_calls(transport)]
    assert result.delivery_status == "sent_public"
    assert result.sent_count == len(updates)
    assert "Candle Craft Intelligence" in messages[0]
    assert "Candle Craft public signal desk status." in messages[2]
    assert "BTCUSDT" in messages[3]
    assert "signal-confirmed" not in messages[3]
    assert "About Candle Craft" in messages[4]
    assert "Candle Craft public signal desk status." in messages[6]
    assert "BTCUSDT" in messages[7]
    assert "About Candle Craft" in messages[8]
    assert "Use the buttons below to access the signal channel and bot info." in messages[9]
    for message in messages:
        assert "System Desk" not in message
        assert "Configuration Desk" not in message
        assert "scan_runs" not in message
        assert "secret-token" not in message


def test_admin_latest_can_include_short_signal_id_and_blocked_count(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_telegram_alert_attempts(db_path)
    service = TelegramAdminCommandService(project_root=tmp_path, database_path=db_path)

    response = service.response_for("/latest")

    assert "Latest Alerts" in response.text
    assert "Sent alerts: 1" in response.text
    assert "Blocked attempts: 1" in response.text
    assert "BTCUSDT" in response.text
    assert "Signal: signal-c..." in response.text


def test_polling_error_is_sanitized_without_exposing_token(tmp_path: Path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    transport = FakeCommandTransport(fail_get_message="Telegram failed for secret-token and admin-chat")

    result = run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            command_service=service,
            transport=transport,
            state_path=tmp_path / "state.json",
            audit_path=tmp_path / "audit.jsonl",
        )
    )

    assert result.delivery_status == "failed"
    assert "[REDACTED]" in result.error_message
    assert "secret-token" not in result.error_message
    assert "admin-chat" not in result.error_message


def test_public_user_cannot_access_admin_only_config_details(tmp_path: Path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    transport = FakeCommandTransport()

    result = run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            command_service=service,
            transport=transport,
            state_path=tmp_path / "state.json",
            audit_path=tmp_path / "audit.jsonl",
            updates=(_update(1, "public-chat", "/config"),),
        )
    )

    message = _screen_calls(transport)[0]["message"]
    assert result.delivery_status == "sent_public"
    assert "Configuration Desk" not in message
    assert "Bot token" not in message
    assert "admin" not in message.lower()
    assert "That signal desk view is not available here." in message


def test_run_telegram_bot_once_starts_listener_without_scanner(capsys) -> None:
    exit_code = run_telegram_bot.main(
        ["--once", "--dry-run"],
        settings=_settings(telegram_commands_enabled=True, telegram_dry_run=True),
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Telegram UI listener process." in output
    assert "Scanner is not started by this process." in output
    assert "poll_status=dry_run" in output


def test_run_telegram_bot_exits_cleanly_on_keyboard_interrupt(capsys) -> None:
    transport = FakeCommandTransport(interrupt_get=True)

    exit_code = run_telegram_bot.main(
        ["--once"],
        settings=_settings(
            telegram_commands_enabled=True,
            telegram_dry_run=False,
            telegram_bot_token="secret-token",
            telegram_admin_chat_id="admin-chat",
        ),
        transport=transport,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Telegram UI listener stopped by user." in output
    assert "secret-token" not in output


def test_check_telegram_runtime_masks_token_and_does_not_send_by_default(capsys) -> None:
    exit_code = check_telegram_runtime.main(
        ["--skip-getme"],
        settings=_settings(
            telegram_bot_token="super-secret-token",
            telegram_chat_id="legacy-chat",
            telegram_admin_chat_id="admin-chat",
            telegram_public_chat_id="public-chat",
            telegram_signal_channel_invite_link="https://t.me/+runtime-private-invite",
            telegram_commands_enabled=True,
            telegram_dry_run=False,
        ),
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "telegram_bot_token=present ([REDACTED])" in output
    assert "signal_channel_invite_link_configured=true" in output
    assert "command_listener_script_present=true" in output
    assert "getme_status=skipped" in output
    assert "admin_test_status" not in output
    assert "public_test_status" not in output
    assert "super-secret-token" not in output
    assert "admin-chat" not in output
    assert "public-chat" not in output
    assert "https://t.me/+runtime-private-invite" not in output


def test_check_telegram_runtime_getme_uses_mocked_api_and_masks_token(capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert "/botsuper-secret-token/getMe" in str(request.url)
        return httpx.Response(200, json={"ok": True, "result": {"username": "candle_craft_bot"}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
    try:
        exit_code = check_telegram_runtime.main(
            ["--api-base-url", "https://telegram.test"],
            settings=_settings(
                telegram_bot_token="super-secret-token",
                telegram_admin_chat_id="admin-chat",
                telegram_public_chat_id="public-chat",
                telegram_dry_run=False,
            ),
            http_client=client,
        )
    finally:
        run(client.aclose())

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "getme_status=ok" in output
    assert "getme_bot_username=candle_craft_bot" in output
    assert "signal_channel_invite_link_configured=false" in output
    assert "super-secret-token" not in output
    assert "admin-chat" not in output
    assert "public-chat" not in output
