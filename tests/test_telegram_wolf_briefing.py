from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from app.formatters.telegram_wolf_briefing import (
    WOLF_BRIEFING_SIGNATURE,
    WolfBriefingFocusItem,
    WolfBriefingSnapshot,
    build_wolf_briefing_snapshot,
    format_wolf_briefing,
)
from app.telegram_admin import TelegramAdminCommandService, TelegramAdminConfig, process_telegram_admin_commands


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
        near_miss_count=3,
        rejected_setup_count=4,
        focus_items=(
            WolfBriefingFocusItem("BTCUSDT", "Active signal", "Confirmed setup"),
            WolfBriefingFocusItem("ETHUSDT", "Watchlist", "Waiting for Limit Zone"),
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
    assert "Active signals: 1" in text
    assert "Near misses: 1" in text
    assert "Rejected setups: 1" in text
    assert "NEARUSDT — Near miss: Trust meter is below minimum." in text
    assert "REJECTUSDT —" not in text


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
    assert "WOLF BRIEFING" in result.previews[0]
    records = _read_jsonl(tmp_path / "audit.jsonl")
    assert records[0]["command"] == "/wolf"


def test_wolf_command_sends_only_to_admin_when_enabled(tmp_path: Path) -> None:
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
    assert "WOLF BRIEFING" in transport.send_calls[0]["message"]
    assert transport.send_calls[0]["message"].endswith(WOLF_BRIEFING_SIGNATURE)
    assert "Regime" not in transport.send_calls[0]["message"]


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


def _update(update_id: int, chat_id: str, text: str) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


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
