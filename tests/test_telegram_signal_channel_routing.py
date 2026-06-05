from __future__ import annotations

import asyncio
from pathlib import Path

from app.alerts.telegram_routing import (
    TelegramDestination,
    TelegramMessageType,
    can_send_to_destination,
)
from app.telegram_admin import TelegramAdminCommandService, TelegramAdminConfig, process_telegram_admin_commands
from tests.test_telegram_admin_commands import FakeCommandTransport


def test_signal_channel_allows_only_signal_message_types() -> None:
    for message_type in (
        TelegramMessageType.WOLF_BRIEFING,
        TelegramMessageType.PUBLIC_SIGNAL,
        TelegramMessageType.LIFECYCLE_UPDATE,
        TelegramMessageType.LIMIT_ZONE_HIT,
        TelegramMessageType.TP_HIT,
        TelegramMessageType.SL_HIT,
        TelegramMessageType.INVALIDATED,
        TelegramMessageType.WATCHLIST_CONFIRMED,
    ):
        assert can_send_to_destination(TelegramDestination.SIGNAL_CHANNEL, message_type).allowed is True

    for message_type in (
        TelegramMessageType.WELCOME,
        TelegramMessageType.ONBOARDING,
        TelegramMessageType.DONATE,
        TelegramMessageType.ADMIN_REPORT,
        TelegramMessageType.ADMIN_MENU,
        TelegramMessageType.PUBLIC_MENU,
        TelegramMessageType.DIAGNOSTICS,
        TelegramMessageType.SCAN_SUMMARY,
        TelegramMessageType.REJECTED_SETUP,
        TelegramMessageType.NO_VALID_SETUP,
        TelegramMessageType.KEYBOARD_MENU,
    ):
        decision = can_send_to_destination(TelegramDestination.SIGNAL_CHANNEL, message_type)
        assert decision.allowed is False
        assert decision.reason == "message_type_not_allowed_for_signal_channel"


def test_start_welcome_update_cannot_send_to_signal_channel_id(tmp_path: Path) -> None:
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_channel_id="signal-channel",
            ),
            command_service=TelegramAdminCommandService(project_root=tmp_path),
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_update(1, "signal-channel", "/start"),),
        )
    )

    assert result.delivery_status == "ignored_unauthorized"
    assert transport.send_calls == []


def test_watchlists_dashboard_cannot_send_to_signal_channel_id(tmp_path: Path) -> None:
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_channel_id="signal-channel",
            ),
            command_service=TelegramAdminCommandService(project_root=tmp_path),
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_update(2, "signal-channel", "/watchlists"),),
        )
    )

    assert result.delivery_status == "ignored_unauthorized"
    assert transport.send_calls == []


def _update(update_id: int, chat_id: str, text: str) -> dict[str, object]:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}
