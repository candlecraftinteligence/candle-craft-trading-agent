from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest

from app.formatters.telegram_wolf_briefing import (
    WOLF_BRIEFING_SIGNATURE,
    WolfBriefingFocusItem,
    WolfBriefingSnapshot,
    build_wolf_briefing_snapshot,
    format_wolf_briefing,
)
from app.telegram_admin import TelegramAdminCommandService, TelegramAdminConfig, process_telegram_admin_commands
from app.telegram_admin.commands import (
    WOLF_BRIEFING_PREVIEW_HEADER,
    WOLF_BRIEFING_PUBLISH_BUTTON_LABEL,
    WOLF_BRIEFING_REFRESH_BUTTON_LABEL,
    WOLF_BRIEFING_CANCEL_BUTTON_LABEL,
)


class FakeCommandTransport:
    def __init__(self) -> None:
        self.send_calls: list[dict[str, Any]] = []
        self.answer_callback_calls: list[dict[str, Any]] = []

    async def get_updates(self, *, bot_token: str, offset: int | None, limit: int, timeout: int):
        return ()

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


def test_wolf_briefing_formatter_with_normal_data() -> None:
    snapshot = WolfBriefingSnapshot(
        market_mood="Mixed",
        signal_quality="Selective watch only",
        best_action="Wait for confirmation",
        active_signal_count=1,
        watchlist_count=2,
        research_watch_count=1,
        near_miss_count=3,
        rejected_setup_count=4,
        focus_items=(
            WolfBriefingFocusItem("BTCUSDT", "Active signal", "Confirmed setup"),
            WolfBriefingFocusItem("ETHUSDT", "Watchlist", "Market condition pending"),
            WolfBriefingFocusItem("SOLUSDT", "Near miss", "Trust meter below minimum"),
        ),
    )

    text = format_wolf_briefing(snapshot)

    assert text.startswith("🐺🟠 WOLF BRIEFING")
    assert "Market Mood: Mixed" in text
    assert "Signal quality: Selective watch only" in text
    assert "Best action: Wait for confirmation" in text
    assert "Active signals: 1" in text
    assert "Watchlist: 2" in text
    assert "Research watch: 1" in text
    assert "Near misses: 3" in text
    assert "Rejected setups: 4" in text
    assert "BTCUSDT — Active signal: Confirmed setup" in text
    assert text.endswith(WOLF_BRIEFING_SIGNATURE)


def test_wolf_briefing_formatter_with_no_active_signals() -> None:
    text = format_wolf_briefing(
        WolfBriefingSnapshot(
            market_mood="N/A",
            signal_quality="Weak setups rejected",
            best_action="Stand down",
            active_signal_count=0,
            watchlist_count=0,
            near_miss_count=0,
            rejected_setup_count=6,
        )
    )

    assert "Active signals: 0" in text
    assert "Watchlist: 0" in text
    assert "Research watch: 0" in text
    assert "Focus:\nN/A" in text
    assert "No forced trades." in text


def test_wolf_briefing_formatter_marks_missing_data_as_na() -> None:
    text = format_wolf_briefing(WolfBriefingSnapshot(market_mood="", signal_quality=None, best_action=""))

    assert "Market Mood: N/A" in text
    assert "Signal quality: N/A" in text
    assert "Best action: N/A" in text


def test_wolf_briefing_formatter_marks_unreliable_data_as_unverified() -> None:
    text = format_wolf_briefing(
        WolfBriefingSnapshot(
            market_mood="unverified",
            signal_quality="unreliable",
            best_action="Wait for confirmation",
        )
    )

    assert "Market Mood: Unverified" in text
    assert "Signal quality: Unverified" in text


def test_wolf_briefing_formatter_does_not_include_regime_word() -> None:
    text = format_wolf_briefing(
        WolfBriefingSnapshot(
            market_mood="Risk-on Regime",
            signal_quality="Regime-filtered watch only",
            best_action="Avoid regime chasing",
            focus_items=(WolfBriefingFocusItem("BTCUSDT", "Near miss", "Regime weakness"),),
        )
    )

    assert "Regime" not in text
    assert "regime" not in text
    assert "Market Mood:" in text


def test_wolf_briefing_builder_uses_scan_rows_without_promoting_rejections() -> None:
    snapshot = build_wolf_briefing_snapshot(
        manifest_row={"market_regime": "MIXED", "valid_setup_count": 1, "near_miss_count": 1, "rejected_count": 1},
        scan_payload={
            "results": [
                {"symbol": "VALIDUSDT", "display_rank": 1, "display_status": "valid_setup"},
                {
                    "symbol": "NEARUSDT",
                    "display_rank": 2,
                    "display_status": "near_miss",
                    "short_reason": "Trust meter is below minimum.",
                },
                {
                    "symbol": "REJECTUSDT",
                    "display_rank": 3,
                    "display_status": "no_setup",
                    "short_reason": "No confirmation.",
                },
            ]
        },
        active_signal_count=None,
        watchlist_count=None,
    )

    text = format_wolf_briefing(snapshot)

    assert "Market Mood: Mixed" in text
    assert "Active signals: 0" in text
    assert "Watchlist: 0" in text
    assert "Research watch: 0" in text
    assert "Near misses: 1" in text
    assert "Rejected setups: 1" in text
    assert "NEARUSDT — Near miss: Trust meter is below minimum." in text
    assert "REJECTUSDT —" not in text


def _wolf_watch_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "symbol": "VALIDUSDT",
        "display_rank": 1,
        "lifecycle_current_state": "WATCHLISTED",
        "direction": "long",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "115",
        "tp3": "120",
        "rr": "3",
        "quality_grade_current": "B+",
    }
    row.update(overrides)
    return row


def _wolf_research_row(**overrides: Any) -> dict[str, Any]:
    row = {
        "symbol": "FILUSDT",
        "display_rank": 2,
        "status": "rejected_by_regime",
        "display_status": "near_miss",
        "display_bucket": "near_miss",
        "setup_quality_score": 70,
        "readiness_score": 55,
        "next_trigger_needed": "Wait for failed gate to clear / 5m BOS/CHoCH.",
        "regime_state": "HIGH_VOLATILITY",
        "regime_compatibility_label": "Hostile",
        "regime_confidence": 9,
        "rejection_reason": "Setup rejected by regime weakness; scalp compatibility Hostile.",
        "short_reason": "Setup rejected by market mood weakness.",
    }
    row.update(overrides)
    return row


def test_wolf_briefing_shows_research_watch_separately_from_public_watchlist() -> None:
    snapshot = build_wolf_briefing_snapshot(
        scan_payload={"results": [_wolf_watch_row(), _wolf_research_row()]},
        active_signal_count=None,
        watchlist_count=None,
    )

    text = format_wolf_briefing(snapshot)

    assert "Watchlist: 1" in text
    assert "Research watch: 1" in text
    assert "Near misses: 0" in text
    assert "FILUSDT — Research watch" in text


@pytest.mark.parametrize(
    "overrides",
    (
        {"lifecycle_current_state": "COOLDOWN"},
        {"lifecycle_current_state": "INVALIDATED"},
        {"lifecycle_current_state": "ARCHIVED"},
        {"quality_grade_current": "Reject"},
        {"direction": "n/a"},
        {"entry_low": "N/A"},
        {"entry_high": "N/A"},
        {"stop_loss": "N/A"},
        {"tp1": "N/A"},
        {"lifecycle_current_state": "TP1_HIT"},
        {"lifecycle_current_state": "TP2_HIT"},
        {"lifecycle_current_state": "TP3_HIT"},
        {"lifecycle_current_state": "SL_HIT"},
    ),
)
def test_wolf_briefing_excludes_non_watchable_lifecycle_rows_from_watchlist(overrides: dict[str, Any]) -> None:
    snapshot = build_wolf_briefing_snapshot(
        scan_payload={"results": [_wolf_watch_row(**overrides)]},
        active_signal_count=None,
        watchlist_count=None,
    )

    text = format_wolf_briefing(snapshot)

    assert "Watchlist: 0" in text
    assert "VALIDUSDT" not in text


def test_wolf_briefing_includes_valid_public_watchlist_row_in_count() -> None:
    snapshot = build_wolf_briefing_snapshot(
        scan_payload={"results": [_wolf_watch_row()]},
        active_signal_count=None,
        watchlist_count=None,
    )

    text = format_wolf_briefing(snapshot)

    assert "Active signals: 0" in text
    assert "Watchlist: 1" in text
    assert "Research watch: 0" in text


@pytest.mark.parametrize("state", ("CONFIRMED", "EXECUTING", "MANAGING"))
def test_wolf_briefing_counts_valid_active_signal_states(state: str) -> None:
    snapshot = build_wolf_briefing_snapshot(
        scan_payload={"results": [_wolf_watch_row(symbol=f"{state}USDT", lifecycle_current_state=state)]},
        active_signal_count=None,
        watchlist_count=None,
    )

    text = format_wolf_briefing(snapshot)

    assert "Active signals: 1" in text
    assert "Watchlist: 0" in text


def test_wolf_briefing_does_not_count_limit_hit_as_active_signal() -> None:
    snapshot = build_wolf_briefing_snapshot(
        scan_payload={"results": [_wolf_watch_row(symbol="LIMITHITUSDT", lifecycle_current_state="LIMIT_HIT")]},
        active_signal_count=None,
        watchlist_count=None,
    )

    text = format_wolf_briefing(snapshot)

    assert "Active signals: 0" in text
    assert "Watchlist: 0" in text


@pytest.mark.parametrize("state", ("REJECTED", "COOLDOWN", "INVALIDATED"))
def test_wolf_briefing_excludes_rejected_cooldown_invalidated_from_active_signals(state: str) -> None:
    snapshot = build_wolf_briefing_snapshot(
        scan_payload={"results": [_wolf_watch_row(lifecycle_current_state=state)]},
        active_signal_count=None,
        watchlist_count=None,
    )

    text = format_wolf_briefing(snapshot)

    assert "Active signals: 0" in text
    assert "Watchlist: 0" in text


def test_wolf_briefing_filters_terminal_watchlist_focus_items() -> None:
    snapshot = build_wolf_briefing_snapshot(
        watchlist_items=(
            WolfBriefingFocusItem("AVAXUSDT", "Watchlist", "TP2 HIT"),
            WolfBriefingFocusItem("BTCUSDT", "Watchlist", "Market condition pending"),
        ),
        active_signal_count=0,
        watchlist_count=None,
    )

    text = format_wolf_briefing(snapshot)

    assert "AVAXUSDT" not in text
    assert "BTCUSDT" in text
    assert "TP2 HIT" not in text


def test_wolf_command_disabled_records_audit_without_sending(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                wolf_briefing_enabled=False,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_update(1, "admin-chat", "/wolf"),),
        )
    )

    assert result.delivery_status == "skipped_disabled"
    assert result.sent_count == 0
    assert transport.send_calls == []
    records = _read_jsonl(tmp_path / "audit.jsonl")
    assert records[0]["command"] == "/wolf"
    assert records[0]["response_type"] == "wolf_briefing_disabled"


def test_wolf_button_dry_run_routes_to_briefing_without_sending(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=True,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                wolf_briefing_enabled=True,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_callback_update(2, "admin-chat", "admin:wolf"),),
            show_preview=True,
        )
    )

    assert result.delivery_status == "dry_run"
    assert result.sent_count == 0
    assert transport.send_calls == []
    assert "WOLF BRIEFING PREVIEW" in result.previews[0]
    records = _read_jsonl(tmp_path / "audit.jsonl")
    assert records[0]["command"] == "/wolf"


def test_admin_private_wolf_command_shows_preview_with_publish_button(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                wolf_briefing_enabled=True,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_update(3, "admin-chat", "/wolf"),),
        )
    )

    assert result.delivery_status == "sent_admin"
    assert result.sent_count == 1
    assert transport.send_calls[0]["chat_id"] == "admin-chat"
    assert transport.send_calls[0]["message"].startswith(WOLF_BRIEFING_PREVIEW_HEADER)
    assert "WOLF BRIEFING\n" not in transport.send_calls[0]["message"]
    assert transport.send_calls[0]["message"].endswith(WOLF_BRIEFING_SIGNATURE)
    assert "Regime" not in transport.send_calls[0]["message"]
    assert _button_labels(transport.send_calls[0]["reply_markup"]) == [
        WOLF_BRIEFING_PUBLISH_BUTTON_LABEL,
        WOLF_BRIEFING_REFRESH_BUTTON_LABEL,
        WOLF_BRIEFING_CANCEL_BUTTON_LABEL,
    ]


def test_admin_wolf_button_shows_preview_with_publish_button(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_channel_id="public-channel",
                wolf_briefing_enabled=True,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_callback_update(33, "admin-chat", "admin:wolf"),),
        )
    )

    assert result.delivery_status == "sent_admin"
    assert transport.send_calls[0]["chat_id"] == "admin-chat"
    assert transport.send_calls[0]["message"].startswith(WOLF_BRIEFING_PREVIEW_HEADER)
    assert WOLF_BRIEFING_PUBLISH_BUTTON_LABEL in _button_labels(transport.send_calls[0]["reply_markup"])


def test_admin_publish_sends_clean_wolf_briefing_to_configured_channel(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_channel_id="public-channel",
                wolf_briefing_enabled=True,
                wolf_briefing_channel_publish_enabled=True,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_callback_update(34, "admin-chat", "admin:wolf_publish"),),
        )
    )

    assert result.delivery_status == "sent_public"
    assert result.sent_count == 1
    assert len(transport.send_calls) == 2
    channel_call = transport.send_calls[0]
    admin_call = transport.send_calls[1]
    assert channel_call["chat_id"] == "public-channel"
    assert channel_call["reply_markup"] is None
    assert channel_call["message"].startswith("🐺🟠 WOLF BRIEFING")
    assert "PREVIEW" not in channel_call["message"]
    assert "Regime" not in channel_call["message"]
    assert channel_call["message"].endswith(WOLF_BRIEFING_SIGNATURE)
    assert admin_call["chat_id"] == "admin-chat"
    assert admin_call["reply_markup"] is None
    assert admin_call["message"] == "Wolf Briefing published to public channel."


def test_public_user_cannot_access_wolf_briefing(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                wolf_briefing_enabled=True,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_update(4, "public-chat", "/wolf"),),
        )
    )

    assert result.delivery_status == "sent_public"
    assert result.sent_count == 1
    assert "WOLF BRIEFING" not in transport.send_calls[0]["message"]
    assert "That signal desk view is not available here." in transport.send_calls[0]["message"]
    assert "admin" not in transport.send_calls[0]["message"].lower()


def test_public_user_cannot_publish_wolf_briefing(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_channel_id="public-channel",
                wolf_briefing_enabled=True,
                wolf_briefing_channel_publish_enabled=True,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_callback_update(5, "public-chat", "admin:wolf_publish"),),
        )
    )

    assert result.delivery_status == "sent_public"
    assert all(call["chat_id"] != "public-channel" for call in transport.send_calls)
    assert "published to public channel" not in transport.send_calls[0]["message"]


def test_public_user_does_not_see_publish_button_when_public_wolf_enabled(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                wolf_briefing_enabled=True,
                wolf_briefing_public_enabled=True,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_update(6, "public-chat", "/wolf"),),
        )
    )

    assert result.delivery_status == "sent_public"
    assert "WOLF BRIEFING" in transport.send_calls[0]["message"]
    assert "PREVIEW" not in transport.send_calls[0]["message"]
    assert WOLF_BRIEFING_PUBLISH_BUTTON_LABEL not in _button_labels(transport.send_calls[0]["reply_markup"])


def test_group_supergroup_and_channel_cannot_trigger_wolf_response(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                wolf_briefing_enabled=True,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(
                _update(7, "group-chat", "/wolf", chat_type="group"),
                _update(8, "supergroup-chat", "/wolf", chat_type="supergroup"),
                _channel_post_update(9, "channel-chat", "/wolf"),
            ),
        )
    )

    assert result.delivery_status == "ignored_unauthorized"
    assert result.sent_count == 0
    assert transport.send_calls == []


def test_public_wolf_flag_does_not_create_channel_bot_ui(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_channel_id="public-channel",
                wolf_briefing_enabled=True,
                wolf_briefing_public_enabled=True,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_channel_post_update(10, "public-channel", "/wolf"),),
        )
    )

    assert result.delivery_status == "ignored_unauthorized"
    assert transport.send_calls == []


def test_publish_disabled_informs_admin_without_channel_send(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_channel_id="public-channel",
                wolf_briefing_enabled=True,
                wolf_briefing_channel_publish_enabled=False,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_callback_update(11, "admin-chat", "admin:wolf_publish"),),
        )
    )

    assert result.delivery_status == "skipped_disabled"
    assert transport.send_calls == [
        {
            "bot_token": "secret-token",
            "chat_id": "admin-chat",
            "message": (
                "Wolf Briefing channel publishing is disabled. "
                "Enable TELEGRAM_WOLF_BRIEFING_CHANNEL_PUBLISH_ENABLED=true to publish manually."
            ),
            "reply_markup": None,
            "photo_path": None,
            "photo_url": None,
        }
    ]


def test_missing_publish_target_informs_admin_without_channel_send(tmp_path: Path) -> None:
    service = _write_wolf_artifacts(tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                commands_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                wolf_briefing_enabled=True,
                wolf_briefing_channel_publish_enabled=True,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_callback_update(12, "admin-chat", "admin:wolf_publish"),),
        )
    )

    assert result.delivery_status == "skipped_missing_credentials"
    assert len(transport.send_calls) == 1
    assert transport.send_calls[0]["chat_id"] == "admin-chat"
    assert "public channel is not configured" in transport.send_calls[0]["message"]


def _write_wolf_artifacts(project_root: Path) -> TelegramAdminCommandService:
    scan_dir = project_root / "scan_runs"
    scan_dir.mkdir(parents=True, exist_ok=True)
    scan_path = scan_dir / "latest_scan.json"
    scan_path.write_text(
        json.dumps(
            {
                "run_id": "run-wolf",
                "market_regime": {"state": "MIXED"},
                "results": [
                    {
                        "symbol": "NEARUSDT",
                        "display_rank": 1,
                        "display_status": "near_miss",
                        "display_bucket": "near_miss",
                        "short_reason": "Waiting for stronger confirmation.",
                    },
                    {
                        "symbol": "REJECTUSDT",
                        "display_rank": 2,
                        "display_status": "no_setup",
                        "display_bucket": "no_setup",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "run_id": "run-wolf",
        "timestamp": "2026-06-05T08:00:00Z",
        "market_regime": "MIXED",
        "valid_setup_count": 0,
        "near_miss_count": 1,
        "rejected_count": 1,
        "latest_scan_path": "scan_runs/latest_scan.json",
    }
    (scan_dir / "scan_run_manifest.jsonl").write_text(json.dumps(manifest) + "\n", encoding="utf-8")
    return TelegramAdminCommandService(project_root=project_root)


def _button_labels(reply_markup: dict[str, Any] | None) -> list[str]:
    if not isinstance(reply_markup, dict):
        return []
    keyboard = reply_markup.get("inline_keyboard")
    if not isinstance(keyboard, list):
        return []
    labels: list[str] = []
    for row in keyboard:
        if not isinstance(row, list):
            continue
        for item in row:
            if isinstance(item, dict):
                labels.append(str(item.get("text") or ""))
    return labels


def _update(update_id: int, chat_id: str, text: str, *, chat_type: str | None = None) -> dict[str, Any]:
    chat: dict[str, Any] = {"id": chat_id}
    if chat_type is not None:
        chat["type"] = chat_type
    return {"update_id": update_id, "message": {"chat": chat, "text": text}}


def _channel_post_update(update_id: int, chat_id: str, text: str) -> dict[str, Any]:
    return {"update_id": update_id, "channel_post": {"chat": {"id": chat_id, "type": "channel"}, "text": text}}


def _callback_update(update_id: int, chat_id: str, callback_data: str) -> dict[str, Any]:
    return {
        "update_id": update_id,
        "callback_query": {
            "id": f"callback-{update_id}",
            "from": {"id": chat_id},
            "message": {"chat": {"id": chat_id}},
            "data": callback_data,
        },
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
