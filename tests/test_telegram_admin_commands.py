from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from app.alerts.integrity_manifest import build_alert_integrity_manifest
from app.core.config import Settings
from app.storage.database import open_initialized_database
from app.telegram_admin import (
    HttpxTelegramAdminCommandTransport,
    TelegramAdminCommandService,
    TelegramAdminConfig,
    process_telegram_admin_commands,
)
from app.telegram_admin.active_watchlists import WATCHLIST_DASHBOARD_FOOTER
from app.telegram_admin.commands import (
    ADMIN_CALLBACK_COMMANDS,
    ADMIN_MENU_BUTTON_CALLBACKS,
    ADMIN_MENU_BUTTON_ROWS,
    ADMIN_WOLF_BRIEFING_CALLBACK_COMMANDS,
    JOIN_SIGNAL_CHANNEL_BUTTON_LABEL,
    PUBLIC_CALLBACK_COMMANDS,
    PUBLIC_DASHBOARD_DISABLED_COMMAND,
    PUBLIC_MENU_BUTTON_CALLBACKS,
    PUBLIC_MENU_BUTTON_ROWS,
    SCREEN_FOOTER,
    SCREEN_HEADER,
    WATCHLIST_BACK_BUTTON_LABEL,
    WATCHLIST_REFRESH_BUTTON_LABEL,
    WOLF_BRIEFING_PUBLISH_BUTTON_LABEL,
    command_for_callback_data,
    normalize_admin_command,
)
from scripts import clear_telegram_native_command_menu as clear_menu_script
from scripts import process_telegram_admin_commands as process_script


class FakeCommandTransport:
    def __init__(
        self,
        updates: tuple[Mapping[str, Any], ...] = (),
        *,
        fail_send_with: str | None = None,
        fail_photo_send_with: str | None = None,
        fail_get: bool = False,
    ) -> None:
        self.updates = updates
        self.fail_send_with = fail_send_with
        self.fail_photo_send_with = fail_photo_send_with
        self.fail_get = fail_get
        self.get_calls: list[dict[str, Any]] = []
        self.send_calls: list[dict[str, Any]] = []
        self.answer_callback_calls: list[dict[str, Any]] = []

    async def get_updates(self, *, bot_token: str, offset: int | None, limit: int, timeout: int):
        self.get_calls.append({"bot_token": bot_token, "offset": offset, "limit": limit, "timeout": timeout})
        if self.fail_get:
            raise AssertionError("getUpdates should not be called")
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
        if (photo_path is not None or photo_url is not None) and self.fail_photo_send_with is not None:
            return ({"status": "failed", "error": self.fail_photo_send_with},)
        if self.fail_send_with is not None:
            return ({"status": "failed", "error": self.fail_send_with},)
        return ({"status": "sent", "message_id": 101, "chat_id": chat_id},)

    async def answer_callback_query(
        self,
        *,
        bot_token: str,
        callback_query_id: str,
        text: str | None = None,
    ):
        self.answer_callback_calls.append(
            {"bot_token": bot_token, "callback_query_id": callback_query_id, "text": text}
        )
        return {"status": "sent"}


def _write_artifacts(
    project_root: Path,
    *,
    rows: list[dict[str, Any]],
    run_id: str = "run-46c",
    manifest_extra: dict[str, Any] | None = None,
) -> TelegramAdminCommandService:
    scan_dir = project_root / "scan_runs"
    scan_dir.mkdir(parents=True, exist_ok=True)
    scan_path = scan_dir / "latest_scan.json"
    payload = {
        "run_id": run_id,
        "scanned_symbols": len(rows),
        "universe": {"mode": "manual", "label": "manual test universe"},
        "market_regime": {"state": "MIXED"},
        "results": rows,
    }
    scan_path.write_text(json.dumps(payload), encoding="utf-8")
    manifest_row = {
        "run_id": run_id,
        "timestamp": "2026-06-01T12:00:00+00:00",
        "universe_label": "manual test universe",
        "universe_mode": "manual",
        "market_regime": "MIXED",
        "regime_confidence": 72,
        "symbols_scanned": len(rows),
        "valid_setup_count": sum(1 for row in rows if row.get("display_status") == "valid_setup"),
        "near_miss_count": sum(
            1
            for row in rows
            if row.get("display_status") == "near_miss" and row.get("failed_stage") != "target_integrity"
        ),
        "rejected_count": sum(1 for row in rows if row.get("display_status") == "no_setup"),
        "alerts_blocked_by_target_integrity": sum(1 for row in rows if row.get("failed_stage") == "target_integrity"),
        "alerts_created": 0,
        "trade_ideas_created": sum(1 for row in rows if row.get("display_status") == "valid_setup"),
        "journal_entries_created": 0,
        "runtime_seconds": 1.25,
        "latest_scan_path": "scan_runs/latest_scan.json",
    }
    manifest_row.update(manifest_extra or {})
    (scan_dir / "scan_run_manifest.jsonl").write_text(json.dumps(manifest_row) + "\n", encoding="utf-8")
    return TelegramAdminCommandService(project_root=project_root)


def _insert_runtime_attempt(
    db_path: Path,
    *,
    signal_id: str,
    symbol: str,
    alert_type: str,
    status: str = "sent",
    direction: str = "long",
    setup_quality_score: str = "B+",
    rr_planned: str = "N/A",
    entry_low: str = "N/A",
    entry_high: str = "N/A",
    stop_loss: str = "N/A",
    tp1: str = "N/A",
    tp2: str = "N/A",
    tp3: str = "N/A",
    sent_at: str | None = None,
) -> None:
    effective_sent_at = sent_at or datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")
    connection = open_initialized_database(db_path)
    try:
        connection.execute(
            """
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
                sent_at, telegram_status, message_hash, scan_run_id, setup_quality_score,
                rr_planned, entry_low, entry_high, stop_loss, tp1, tp2, tp3
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                signal_id,
                symbol,
                direction,
                "CONFIRMED",
                alert_type,
                "CONFIRMED",
                effective_sent_at,
                status,
                f"hash-{signal_id}-{alert_type}",
                "run-46c",
                setup_quality_score,
                rr_planned,
                entry_low,
                entry_high,
                stop_loss,
                tp1,
                tp2,
                tp3,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _valid_row(symbol: str = "VALIDUSDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "display_rank": 1,
        "display_status": "valid_setup",
        "display_bucket": "valid",
        "side": "long",
        "grade": "A",
        "score": 88,
        "short_reason": "Trade idea created.",
        "next_trigger_needed": "N/A",
    }


def _near_row(symbol: str = "NEARUSDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "display_rank": 2,
        "display_status": "near_miss",
        "display_bucket": "near_miss",
        "failed_stage": "final",
        "short_reason": "Trust meter is below minimum.",
        "next_trigger_needed": "Wait for trust >= 80.",
        "lifecycle_integrity_status": "N/A",
        "strategy_diagnostics": {"swing": {"bias": "short", "trust_grade": "B", "trust_percentage": 62}},
        "rejected_strategy_modes": ["swing"],
    }


def _blocked_row(symbol: str = "TARGETUSDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "display_rank": 3,
        "display_status": "near_miss",
        "display_bucket": "near_miss",
        "failed_stage": "target_integrity",
        "side": "long",
        "target_integrity_failure_type": "RR_BELOW_MINIMUM",
        "target_integrity_reason": "Clean target path is too compressed.",
        "short_reason": "Target integrity guard blocked alert creation.",
        "next_trigger_needed": "Wait for target expansion above opposing structure.",
        "lifecycle_current_state": "TRIGGERED",
        "lifecycle_integrity_status": "STALE_OR_DEGRADED",
    }


def _alert_row(symbol: str = "ALERTUSDT") -> dict[str, Any]:
    trade_idea = {
        "symbol": symbol,
        "direction": "long",
        "timeframe": "15m",
        "setup_type": "liquidity_grab_pullback_swing",
        "entry_zone": {"low": "100", "high": "102"},
        "stop_loss": "95",
        "take_profits": [{"price": "112"}, {"price": "120"}],
        "invalidation": "Invalid below 95.",
        "cancel_condition": "Cancel if price closes below 95.",
        "risk_warning": "Manual review only; crypto derivatives are high risk.",
        "quality_gate_result": {"passed": True},
    }
    formatted_message = "\n".join(
        (
            "Direction: LONG",
            "Invalidation: Invalid below 95.",
            "Risk warning: Manual review only; crypto derivatives are high risk.",
        )
    )
    deduplication_key = f"{symbol}-15m-liquidity_grab_pullback_swing"
    integrity_manifest = build_alert_integrity_manifest(
        trade_idea=trade_idea,
        formatted_message=formatted_message,
        message_parts=(formatted_message,),
        channel="telegram",
        status="dry_run",
        dry_run=True,
        deduplication_key=deduplication_key,
    )
    return {
        "symbol": symbol,
        "display_rank": 1,
        "display_status": "valid_setup",
        "display_bucket": "valid",
        "status": "journal_entry_created",
        "status_history": ["idea_created", "alert_dry_run_created", "journal_entry_created"],
        "side": "long",
        "grade": "A",
        "score": 91,
        "short_reason": "Trade idea created.",
        "trade_idea": trade_idea,
        "alert_result": {
            "status": "dry_run",
            "delivery_status": "dry_run",
            "channel": "telegram",
            "dry_run": True,
            "formatted_message": formatted_message,
            "message_parts": (formatted_message,),
            "deduplication_key": deduplication_key,
            "integrity_manifest": integrity_manifest.model_dump(mode="json"),
        },
    }


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


def _cleanup_send_calls(transport: FakeCommandTransport) -> list[dict[str, Any]]:
    return [
        call
        for call in transport.send_calls
        if isinstance(call.get("reply_markup"), Mapping) and call["reply_markup"].get("remove_keyboard") is True
    ]


def _screen_send_calls(transport: FakeCommandTransport) -> list[dict[str, Any]]:
    return [
        call
        for call in transport.send_calls
        if not (isinstance(call.get("reply_markup"), Mapping) and call["reply_markup"].get("remove_keyboard") is True)
    ]


def _write_local_logo(project_root: Path) -> Path:
    logo_path = project_root / "assets" / "telegram" / "welcome.png"
    logo_path.parent.mkdir(parents=True, exist_ok=True)
    logo_path.write_bytes(b"fake-png-bytes")
    return logo_path.resolve()


def _assert_shell_screen(text: str) -> None:
    assert text.startswith(SCREEN_HEADER)
    assert text.endswith(SCREEN_FOOTER) or text.endswith(WATCHLIST_DASHBOARD_FOOTER)


def _button_labels(reply_markup: Mapping[str, Any] | None) -> list[str]:
    if reply_markup is None:
        return []
    keyboard = reply_markup.get("inline_keyboard")
    if not isinstance(keyboard, list):
        return []
    labels: list[str] = []
    for row in keyboard:
        if not isinstance(row, list):
            continue
        for item in row:
            if isinstance(item, Mapping):
                labels.append(str(item.get("text") or ""))
    return labels


def _callback_data_values(reply_markup: Mapping[str, Any] | None) -> list[str]:
    if reply_markup is None:
        return []
    keyboard = reply_markup.get("inline_keyboard")
    if not isinstance(keyboard, list):
        return []
    values: list[str] = []
    for row in keyboard:
        if not isinstance(row, list):
            continue
        for item in row:
            if isinstance(item, Mapping) and "callback_data" in item:
                values.append(str(item.get("callback_data") or ""))
    return values


def _inline_button_items(reply_markup: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    if reply_markup is None:
        return []
    keyboard = reply_markup.get("inline_keyboard")
    if not isinstance(keyboard, list):
        return []
    items: list[Mapping[str, Any]] = []
    for row in keyboard:
        if not isinstance(row, list):
            continue
        for item in row:
            if isinstance(item, Mapping):
                items.append(item)
    return items


def _copy_text_values(reply_markup: Mapping[str, Any] | None) -> list[str]:
    values: list[str] = []
    for item in _inline_button_items(reply_markup):
        copy_text = item.get("copy_text")
        if isinstance(copy_text, Mapping):
            values.append(str(copy_text.get("text") or ""))
    return values


def _url_button_items(reply_markup: Mapping[str, Any] | None) -> list[Mapping[str, Any]]:
    return [item for item in _inline_button_items(reply_markup) if "url" in item]


def _assert_join_signal_channel_button(
    reply_markup: Mapping[str, Any] | None,
    *,
    invite_link: str,
) -> None:
    items = [
        item
        for item in _url_button_items(reply_markup)
        if str(item.get("text") or "") == JOIN_SIGNAL_CHANNEL_BUTTON_LABEL
    ]
    assert len(items) == 1
    assert items[0]["url"] == invite_link
    assert "callback_data" not in items[0]
    assert "copy_text" not in items[0]


def _assert_no_join_signal_channel_button(reply_markup: Mapping[str, Any] | None) -> None:
    assert JOIN_SIGNAL_CHANNEL_BUTTON_LABEL not in _button_labels(reply_markup)
    assert _url_button_items(reply_markup) == []


def _assert_inline_markup(reply_markup: Mapping[str, Any] | None) -> None:
    assert reply_markup is not None
    assert "inline_keyboard" in reply_markup
    assert "keyboard" not in reply_markup
    assert "is_persistent" not in reply_markup
    assert "resize_keyboard" not in reply_markup
    assert "one_time_keyboard" not in reply_markup


def _assert_public_full_menu(reply_markup: Mapping[str, Any] | None) -> None:
    _assert_inline_markup(reply_markup)
    labels = _button_labels(reply_markup)
    expected = [label for row in PUBLIC_MENU_BUTTON_ROWS for label in row]
    assert labels == expected
    assert _callback_data_values(reply_markup) == [PUBLIC_MENU_BUTTON_CALLBACKS[label] for label in expected]
    admin_labels = {label for row in ADMIN_MENU_BUTTON_ROWS for label in row}
    assert admin_labels.isdisjoint(labels)


def _assert_admin_full_menu(reply_markup: Mapping[str, Any] | None) -> None:
    _assert_inline_markup(reply_markup)
    labels = _button_labels(reply_markup)
    expected = [label for row in ADMIN_MENU_BUTTON_ROWS for label in row]
    assert labels == expected
    assert _callback_data_values(reply_markup) == [ADMIN_MENU_BUTTON_CALLBACKS[label] for label in expected]


def _assert_public_menu_only(reply_markup: Mapping[str, Any] | None) -> None:
    assert reply_markup is not None
    _assert_inline_markup(reply_markup)
    labels = _button_labels(reply_markup)
    expected = [label for row in PUBLIC_MENU_BUTTON_ROWS for label in row]
    assert labels == expected or labels == ["↩ Back to Menu"]
    callbacks = _callback_data_values(reply_markup)
    assert callbacks == [PUBLIC_MENU_BUTTON_CALLBACKS[label] for label in expected] or callbacks == ["public:menu"]
    admin_labels = {label for row in ADMIN_MENU_BUTTON_ROWS for label in row}
    assert admin_labels.isdisjoint(labels)


def _assert_public_watchlist_controls(reply_markup: Mapping[str, Any] | None) -> None:
    assert reply_markup is not None
    _assert_inline_markup(reply_markup)
    assert _button_labels(reply_markup) == [WATCHLIST_REFRESH_BUTTON_LABEL, WATCHLIST_BACK_BUTTON_LABEL]
    assert _callback_data_values(reply_markup) == ["public:watchlist", "public:menu"]
    admin_labels = {label for row in ADMIN_MENU_BUTTON_ROWS for label in row}
    assert admin_labels.isdisjoint(_button_labels(reply_markup))


def _assert_public_donate_copy_markup(
    reply_markup: Mapping[str, Any] | None,
    *,
    usdt_ton: str | None = None,
    ton: str | None = None,
    btc: str | None = None,
) -> None:
    assert reply_markup is not None
    _assert_inline_markup(reply_markup)
    expected_labels: list[str] = []
    expected_copy_texts: list[str] = []
    if usdt_ton is not None:
        expected_labels.append("📋 USDT on TON")
        expected_copy_texts.append(usdt_ton)
    if ton is not None:
        expected_labels.append("📋 TON")
        expected_copy_texts.append(ton)
    if btc is not None:
        expected_labels.append("📋 BTC")
        expected_copy_texts.append(btc)
    expected_labels.append("⬅️ Back to Menu")
    assert _button_labels(reply_markup) == expected_labels
    assert _copy_text_values(reply_markup) == expected_copy_texts
    assert _callback_data_values(reply_markup) == ["public:menu"]
    for item in _inline_button_items(reply_markup):
        assert "url" not in item
        assert "pay" not in item


def _assert_admin_menu_only(reply_markup: Mapping[str, Any] | None) -> None:
    assert reply_markup is not None
    _assert_inline_markup(reply_markup)
    labels = _button_labels(reply_markup)
    expected = [label for row in ADMIN_MENU_BUTTON_ROWS for label in row]
    assert labels == expected or labels == ["↩ Back to Menu"]
    callbacks = _callback_data_values(reply_markup)
    assert callbacks == [ADMIN_MENU_BUTTON_CALLBACKS[label] for label in expected] or callbacks == ["admin:menu"]


def _assert_admin_watchlist_controls(reply_markup: Mapping[str, Any] | None) -> None:
    assert reply_markup is not None
    _assert_inline_markup(reply_markup)
    assert _button_labels(reply_markup) == [WATCHLIST_REFRESH_BUTTON_LABEL, WATCHLIST_BACK_BUTTON_LABEL]
    assert _callback_data_values(reply_markup) == ["admin:watchlists", "admin:menu"]


def _assert_no_execution_buttons(reply_markup: Mapping[str, Any] | None) -> None:
    labels = _button_labels(reply_markup)
    forbidden = ("buy", "sell", "execute", "order", "withdraw", "transfer")
    for label in labels:
        assert not any(word in label.lower() for word in forbidden)


def _expected_public_start_text() -> str:
    return "\n".join(
        (
            f"{SCREEN_HEADER} Candle Craft Intelligence",
            "",
            "Your AI-powered signal engine is online.",
            "",
            "Welcome to the Moon Trip signal desk.",
            "",
            (
                "Candle Craft filters crypto futures for clean structure, liquidity sweeps, confirmations, "
                "and high-quality setups."
            ),
            "",
            "No random signals.",
            "No market chasing.",
            "Only filtered opportunities when the structure is clean.",
            "",
            "━━━━━━━━━━━━━━━━━━",
            "Use the buttons below to access the signal channel and bot info.",
            "Signal channel invite link is not configured yet.",
            "",
            "System:",
            "Manual signal intelligence only. No order execution.",
            "",
            SCREEN_FOOTER,
        )
    )


def _expected_public_help_text() -> str:
    return "\n".join(
        (
            "📖 How to Use Candle Craft",
            "",
            "🐺 Welcome to the Candle Craft pack.",
            "",
            "This is not a candle-chasing bot.",
            "This is the Moon Trip signal desk — built for traders who wait for structure, liquidity, confirmation, and clean execution zones.",
            "",
            "The mission is simple:",
            "",
            "Less noise.",
            "Better setups.",
            "Sharper decisions.",
            "",
            "How to use it:",
            "",
            "1. 🦄 Join the signal channel",
            "This is where filtered Candle Craft alerts and lifecycle updates are posted.",
            "",
            "2. 🐺 Wait for the hunt",
            "No alert means no clean setup.",
            "Silence is not weakness — it means the wolf is waiting for better market structure.",
            "",
            "3. 🧭 Read the signal like a battle plan",
            "Focus on:",
            "• pair",
            "• direction",
            "• setup thesis",
            "• reaction zone",
            "• invalidation",
            "• targets",
            "• lifecycle status",
            "",
            "4. 🚫 Never chase candles",
            "If the move already left the zone, the opportunity is gone.",
            "The pack waits for the next clean setup.",
            "",
            "5. 🛡 Stay responsible",
            "Candle Craft provides manual signal intelligence only.",
            "It does not execute trades, manage funds, access accounts, or guarantee profits.",
            "",
            "This is not financial advice.",
            "All trading decisions remain your own responsibility.",
            "",
            "🌕 The Moon Trip is not about rushing.",
            "It is about patience, structure, and execution.",
            "",
            "🐺 Candle Craft | Signal. Structure. Execution.",
        )
    )

def _expected_public_donate_address_text(
    title: str,
    address: str,
    *,
    network: str,
    send_warning: str,
) -> str:
    return "\n".join(
        (
            f"{SCREEN_HEADER} {title}",
            "",
            "Address:",
            address,
            "",
            f"Network: {network}",
            send_warning,
            "",
            SCREEN_FOOTER,
        )
    )


def _assert_public_screen_safe(text: str) -> None:
    forbidden = (
        "System Desk",
        "Integrity Desk",
        "Configuration Desk",
        "secret-token",
        "admin-chat",
        "stranger-chat",
        "scan_runs",
        "latest_scan.json",
        "manifest",
        "payload",
        "malformed",
        "artifact",
        "dry_run",
        "target_integrity",
        "manual test universe",
        "raw chat",
        "admin-only",
        "admin desk",
    )
    lowered = text.lower()
    for phrase in forbidden:
        assert phrase.lower() not in lowered


def _rejected_row(symbol: str = "REJECTUSDT") -> dict[str, Any]:
    return {
        "symbol": symbol,
        "display_rank": 4,
        "display_status": "no_setup",
        "display_bucket": "no_setup",
        "side": "long",
        "grade": "C",
        "score": 44,
        "short_reason": "Structure did not confirm.",
    }


def test_start_response_contains_admin_desk_welcome_and_command_list(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    response = service.response_for("/start")

    _assert_shell_screen(response.text)
    assert response.text.startswith(f"{SCREEN_HEADER} Candle Craft Intelligence")
    assert response.response_type == "start"
    for row in ADMIN_MENU_BUTTON_ROWS:
        for label in row:
            assert label.split(maxsplit=1)[0] in response.text
    _assert_admin_full_menu(response.reply_markup)
    _assert_no_execution_buttons(response.reply_markup)
    assert response.reply_markup["inline_keyboard"][0][0]["text"] == "🐺 Wolf Briefing"
    assert response.reply_markup["inline_keyboard"][0][0]["callback_data"] == "admin:wolf"
    assert "No execution buttons" in response.text
    assert "Your market-structure command center." in response.text
    assert "Manual execution. Quality gates protected." in response.text
    assert "Quality over quantity." in response.text


def test_help_response_lists_commands_and_safety_note(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    response = service.response_for("/help")

    _assert_shell_screen(response.text)
    assert response.text.startswith(f"{SCREEN_HEADER} Command Guide")
    assert "/status" in response.text
    assert "View system health and scan state." in response.text
    assert "/wolf" in response.text
    assert "View the latest Wolf Briefing." in response.text
    assert "/audit" in response.text
    assert "View safety and duplicate checks." in response.text
    assert "No weak setup promotion." in response.text


def test_menu_button_labels_normalize_to_commands() -> None:
    assert normalize_admin_command("🐺 Wolf Briefing") == "/wolf"
    assert normalize_admin_command("📊 Status") == "/status"
    assert normalize_admin_command("🚨 Alerts") == "/alerts"
    assert normalize_admin_command("👁 Watchlists") == "/watchlists"
    assert normalize_admin_command("🧾 Integrity") == "/integrity"
    assert normalize_admin_command("⚙️ Config") == "/config"
    assert normalize_admin_command("❓ Guide") == "/guide"
    assert normalize_admin_command("📡 Last Scan") == PUBLIC_DASHBOARD_DISABLED_COMMAND
    assert normalize_admin_command("🔥 Active Signals") == PUBLIC_DASHBOARD_DISABLED_COMMAND
    assert normalize_admin_command("👁 Watchlist") == PUBLIC_DASHBOARD_DISABLED_COMMAND
    assert normalize_admin_command("👁 Watchlists") == "/watchlists"
    assert normalize_admin_command("👁 Watchlist Signals") == PUBLIC_DASHBOARD_DISABLED_COMMAND
    assert normalize_admin_command("Active Watchlists") == "/watchlists"
    assert normalize_admin_command("📖 How to Use") == "/help"
    assert normalize_admin_command("🌐 Social") == "/social"
    assert normalize_admin_command("❓ Help") == "/help"
    assert normalize_admin_command("🧡 Donate") == "/donate"


def test_inline_callback_data_maps_to_commands() -> None:
    for callback_data, command in PUBLIC_CALLBACK_COMMANDS.items():
        assert command_for_callback_data(callback_data) == ("public", command)
    for callback_data, command in ADMIN_WOLF_BRIEFING_CALLBACK_COMMANDS.items():
        assert command_for_callback_data(callback_data) == ("admin", command)
    for callback_data, command in ADMIN_CALLBACK_COMMANDS.items():
        assert command_for_callback_data(callback_data) == ("admin", command)
    assert command_for_callback_data("admin:unknown") == ("", "")


def test_menus_use_inline_keyboards_and_not_persistent_reply_keyboards(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    admin = service.response_for("/menu")
    public = service.public_response_for("/menu")

    _assert_admin_full_menu(admin.reply_markup)
    _assert_public_full_menu(public.reply_markup)
    _assert_no_execution_buttons(admin.reply_markup)
    _assert_no_execution_buttons(public.reply_markup)


def test_empty_states_use_premium_copy_without_developer_wording(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    watchlists = service.response_for("/watchlists")
    responses = (
        service.response_for("/status"),
        service.response_for("/alerts"),
        watchlists,
        service.response_for("/integrity"),
    )

    combined = "\n".join(response.text for response in responses)
    assert "No lifecycle alerts available right now." in combined
    assert "The wolf is stalking liquidity." in combined
    assert combined.count("None right now.") >= 3
    assert "No forced trades." in combined
    assert "No safety summary available yet." in combined
    assert "Preset lists:" not in watchlists.text
    assert "Data status: Unverified" not in combined
    assert "The latest scan record is not available" not in combined
    assert "manifest" not in combined.lower()
    assert "payload" not in combined.lower()


def test_long_run_ids_are_shortened_in_telegram_ui(tmp_path) -> None:
    long_run_id = "1234567890abcdef1234567890abcdef"
    service = _write_artifacts(tmp_path, rows=[_alert_row()], run_id=long_run_id)

    status = service.response_for("/status")
    public_lastscan = service.public_response_for("/lastscan")

    assert long_run_id not in status.text
    assert long_run_id not in public_lastscan.text
    assert "Run: 1234567890ab" in status.text
    assert "Last scan: 2026-06-01T12:00:00+00:00" in public_lastscan.text


def test_status_loads_latest_manifest_and_formats_core_counts(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row(), _near_row(), _blocked_row()])

    response = service.response_for("/status")

    _assert_shell_screen(response.text)
    assert response.text.startswith(f"{SCREEN_HEADER} System Desk")
    assert "Mode: Manual-only" in response.text
    assert "Execution: Disabled" in response.text
    assert "Quality gates: Protected" in response.text
    assert "Run: run-46c" in response.text
    assert "Symbol list: Manual" in response.text
    assert "Market Climate: Mixed" in response.text
    assert "Regime confidence: 72" in response.text
    assert "Symbols scanned: 3" in response.text
    assert "Confirmed setups: 1" in response.text
    assert "Research candidates: 1" in response.text
    assert "Alerts created: 0" in response.text
    assert "target-integrity" not in response.text
    assert "scan_runs/latest_scan.json" not in response.text
    assert "Weak setups stay rejected." in response.text


def test_lastscan_summarizes_latest_scan_without_raw_json_dump(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row(), _near_row()])

    response = service.response_for("/lastscan")

    _assert_shell_screen(response.text)
    assert "Latest Scan" in response.text
    assert "Top Confirmed Setups" in response.text
    assert "VALIDUSDT" in response.text
    assert "Direction: Long" in response.text
    assert "Grade: A" in response.text
    assert "Score: 88" in response.text
    assert "{" not in response.text
    assert "}" not in response.text


def test_lastscan_uses_near_misses_when_no_valid_setups(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_near_row()])

    response = service.response_for("/lastscan")

    _assert_shell_screen(response.text)
    assert "Top Near-Miss Candidates" in response.text
    assert "NEARUSDT" in response.text
    assert "Status: Monitoring" in response.text


def test_near_returns_top_near_miss_rows_and_uses_na_for_missing_data(tmp_path) -> None:
    missing_fields = {
        "symbol": "MISSINGUSDT",
        "display_rank": 1,
        "display_status": "near_miss",
        "display_bucket": "near_miss",
        "failed_stage": "rr",
    }
    service = _write_artifacts(tmp_path, rows=[missing_fields, _near_row()])

    response = service.response_for("/near")

    _assert_shell_screen(response.text)
    assert "MISSINGUSDT" in response.text
    assert "Direction: N/A" in response.text
    assert "Next step: N/A" in response.text
    assert "NEARUSDT" in response.text
    assert "Direction: Short" in response.text
    assert "Grade: B" in response.text
    assert "Score: 62" in response.text
    assert "Status: Monitoring" in response.text
    assert "Next step: Waiting for stronger confirmation" in response.text
    assert "failed_stage" not in response.text


def test_blocked_returns_target_integrity_blocked_rows(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_near_row(), _blocked_row()])

    response = service.response_for("/blocked")

    _assert_shell_screen(response.text)
    assert response.text.startswith(f"{SCREEN_HEADER} Integrity Desk")
    assert "TARGETUSDT" in response.text
    assert "Direction: Long" in response.text
    assert "Safety check: Clean target path is too compressed." in response.text
    assert "Status: Triggered" in response.text
    assert "target_integrity" not in response.text


def test_blocked_returns_clean_none_message_when_no_blocked_rows(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_near_row()])

    response = service.response_for("/blocked")

    _assert_shell_screen(response.text)
    assert "No safety blocks in the latest scan." in response.text
    assert "TARGETUSDT" not in response.text


def test_alerts_screen_lists_latest_alert_records_with_integrity_and_risk(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row()])

    response = service.response_for("/alerts")

    _assert_shell_screen(response.text)
    assert response.text.startswith(f"{SCREEN_HEADER} Alert Desk")
    assert "Run: run-46c" in response.text
    assert "ALERTUSDT" in response.text
    assert "Direction: Long" in response.text
    assert "Grade: A" in response.text
    assert "Score: 91" in response.text
    assert "Symbol: ALERTUSDT" in response.text
    assert "Delivery: Test mode" in response.text
    assert "Safety check: Passed" in response.text
    assert "Risk note:" in response.text
    assert "Manual review only. No execution controls." in response.text
    assert "delivery dry_run" not in response.text
    assert "integrity valid" not in response.text
    assert "{" not in response.text
    assert "}" not in response.text


def test_watchlists_screen_uses_active_public_watchlist_store(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_near_row(), _blocked_row(), _valid_row()])

    response = service.response_for("/watchlists")

    _assert_shell_screen(response.text)
    assert response.text.startswith("🐺🟠 WATCHLISTS")
    assert response.text.count("None right now.") == 3
    assert "The wolf is stalking liquidity." in response.text
    assert "No forced trades." in response.text
    assert "NEARUSDT" not in response.text
    assert "TARGETUSDT" not in response.text
    assert "Preset lists:" not in response.text


def test_integrity_screen_runs_read_only_audit_summary(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row()])

    response = service.response_for("/integrity")

    _assert_shell_screen(response.text)
    assert response.text.startswith(f"{SCREEN_HEADER} Integrity Desk")
    assert "Safety status: Clean" in response.text
    assert "Alerts reviewed: 1" in response.text
    assert "Invalid alerts: 0" in response.text
    assert "Missing safety data: 0" in response.text
    assert "Findings:" in response.text
    assert "None" in response.text
    assert "Rejected or incomplete setups are not alertable." in response.text
    assert "manifest" not in response.text.lower()


def test_config_screen_redacts_secrets_and_raw_chat_ids(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    response = service.response_for(
        "/config",
        admin_config=TelegramAdminConfig(
            admin_enabled=True,
            dry_run=False,
            bot_token="secret-token",
            admin_chat_id="123456789",
            public_channel_id="public-channel",
            wolf_briefing_channel_id="wolf-public-channel",
            signal_channel_invite_link="https://t.me/+config-private-invite",
            vip_channel_id="vip-channel",
        ),
    )

    _assert_shell_screen(response.text)
    assert response.text.startswith(f"{SCREEN_HEADER} Configuration Desk")
    assert "Manual mode: Active" in response.text
    assert "Execution: Disabled" in response.text
    assert "Command UI: Enabled" in response.text
    assert "Admin reports: Enabled" in response.text
    assert "Test mode: Inactive" in response.text
    assert "Bot token: Hidden" in response.text
    assert "Chat ID: Hidden" in response.text
    assert "Signal channel invite: configured" in response.text
    assert "secret-token" not in response.text
    assert "123456789" not in response.text
    assert "public-channel" not in response.text
    assert "wolf-public-channel" not in response.text
    assert "https://t.me/+config-private-invite" not in response.text
    assert "vip-channel" not in response.text


def test_telegram_admin_config_splits_command_ui_from_admin_reports() -> None:
    config = TelegramAdminConfig.from_settings(
        Settings(
            _env_file=None,
            telegram_admin_enabled=True,
            telegram_commands_enabled=True,
            telegram_admin_reports_enabled=False,
            telegram_dry_run=False,
            telegram_bot_token="secret-token",
            telegram_admin_chat_id="admin-chat",
            telegram_wolf_briefing_enabled=True,
            telegram_wolf_briefing_public_enabled=False,
            telegram_wolf_briefing_channel_publish_enabled=True,
            telegram_wolf_briefing_channel_id="wolf-public-channel",
            candle_craft_donate_usdt_ton_address="TEST_USDT_TON_ADDRESS",
            candle_craft_donate_ton_address="TEST_TON_ADDRESS",
            candle_craft_donate_btc_address="TEST_BTC_ADDRESS",
            candle_craft_donate_url="https://donate.example.test/candlecraft",
            telegram_signal_channel_invite_link="https://t.me/+test-private-invite",
        )
    )

    assert config.admin_enabled is True
    assert config.command_ui_enabled is True
    assert config.admin_report_enabled is False
    assert config.wolf_briefing_enabled is True
    assert config.wolf_briefing_public_enabled is False
    assert config.wolf_briefing_channel_publish_enabled is True
    assert config.wolf_briefing_channel_id == "wolf-public-channel"
    assert config.wolf_briefing_publish_channel_id == "wolf-public-channel"
    assert config.dry_run is False
    assert config.donate_usdt_ton_address == "TEST_USDT_TON_ADDRESS"
    assert config.donate_ton_address == "TEST_TON_ADDRESS"
    assert config.donate_btc_address == "TEST_BTC_ADDRESS"
    assert config.donate_url == "https://donate.example.test/candlecraft"
    assert config.signal_channel_invite_link == "https://t.me/+test-private-invite"


def test_public_start_response_uses_public_copy_and_optional_logo(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    response = service.public_response_for(
        "/start",
        public_config=TelegramAdminConfig(public_logo_url="https://cdn.example.test/candle-logo.png"),
    )

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_public_menu_only(response.reply_markup)
    _assert_no_execution_buttons(response.reply_markup)
    assert response.text.startswith(f"{SCREEN_HEADER} Candle Craft Intelligence")
    assert response.text == _expected_public_start_text()
    assert "Welcome to the Moon Trip signal desk" in response.text
    assert "No random signals." in response.text
    assert "Use the buttons below to access the signal channel and bot info." in response.text
    assert "System Desk" not in response.text
    assert "Integrity Desk" not in response.text
    assert "Configuration Desk" not in response.text
    assert response.photo_path is None
    assert response.photo_url == "https://cdn.example.test/candle-logo.png"


def test_public_start_uses_local_logo_path_when_it_exists(tmp_path) -> None:
    logo_path = _write_local_logo(tmp_path)
    service = TelegramAdminCommandService(project_root=tmp_path)
    config = TelegramAdminConfig.from_settings(
        Settings(
            _env_file=None,
            candle_craft_public_logo_path=str(Path("assets") / "telegram" / "welcome.png"),
            candle_craft_public_logo_url="https://cdn.example.test/candle-logo.png",
        )
    )

    response = service.public_response_for("/start", public_config=config)

    _assert_shell_screen(response.text)
    _assert_public_full_menu(response.reply_markup)
    assert response.text == _expected_public_start_text()
    assert response.photo_path == logo_path
    assert response.photo_url is None


def test_public_start_falls_back_to_url_when_local_logo_path_is_missing(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    config = TelegramAdminConfig.from_settings(
        Settings(
            _env_file=None,
            candle_craft_public_logo_path=str(Path("assets") / "telegram" / "missing.png"),
            candle_craft_public_logo_url="https://cdn.example.test/candle-logo.png",
        )
    )

    response = service.public_response_for("/start", public_config=config)

    assert response.photo_path is None
    assert response.photo_url == "https://cdn.example.test/candle-logo.png"


def test_public_start_works_when_logo_url_is_missing(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    response = service.public_response_for("/start", public_config=TelegramAdminConfig())

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_public_menu_only(response.reply_markup)
    assert response.text == _expected_public_start_text()
    assert response.photo_path is None
    assert response.photo_url is None


def test_public_start_ignores_invalid_local_logo_path_without_crashing(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    response = service.public_response_for(
        "/start",
        public_config=TelegramAdminConfig(public_logo_path="bad\0path"),
    )

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_public_full_menu(response.reply_markup)
    assert response.text == _expected_public_start_text()
    assert response.photo_path is None
    assert response.photo_url is None


def test_public_menu_has_only_public_buttons_and_no_logo_when_missing(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    response = service.public_response_for("/menu", public_config=TelegramAdminConfig())

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_public_full_menu(response.reply_markup)
    _assert_no_execution_buttons(response.reply_markup)
    assert "Your market-structure command center." not in response.text
    assert response.text == _expected_public_start_text()
    assert "System Desk" not in response.text
    assert "Integrity Desk" not in response.text
    assert "Configuration Desk" not in response.text
    assert response.photo_path is None
    assert response.photo_url is None


def test_public_start_menu_and_about_include_join_button_when_invite_link_configured(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    invite_link = "https://t.me/+test-private-invite"
    config = TelegramAdminConfig(signal_channel_invite_link=invite_link)

    for command in ("/start", "/menu", "/about"):
        response = service.public_response_for(command, public_config=config)

        _assert_shell_screen(response.text)
        _assert_public_screen_safe(response.text)
        _assert_join_signal_channel_button(response.reply_markup, invite_link=invite_link)
        _assert_no_execution_buttons(response.reply_markup)
        assert JOIN_SIGNAL_CHANNEL_BUTTON_LABEL in _button_labels(response.reply_markup)
        assert "Join the private Candle Craft signal channel for live watchlists and lifecycle updates." in response.text
        assert "Signal channel invite link is not configured yet." not in response.text
        assert invite_link not in response.text


def test_public_start_menu_and_about_hide_join_button_when_invite_link_missing(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    config = TelegramAdminConfig()

    for command in ("/start", "/menu", "/about"):
        response = service.public_response_for(command, public_config=config)

        _assert_shell_screen(response.text)
        _assert_public_screen_safe(response.text)
        _assert_no_join_signal_channel_button(response.reply_markup)
        _assert_no_execution_buttons(response.reply_markup)
        assert "Signal channel invite link is not configured yet." in response.text


def test_public_lastscan_shows_summary_without_admin_internals(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row(), _blocked_row(), _rejected_row()])

    response = service.public_response_for("/lastscan")

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_public_menu_only(response.reply_markup)
    assert response.text.startswith(f"{SCREEN_HEADER} Last Scan")
    assert "Latest Candle Craft market intelligence." in response.text
    assert "Last scan: 2026-06-01T12:00:00+00:00" in response.text
    assert "Symbols scanned: 4" in response.text
    assert "Confirmed setups: 1" in response.text
    assert "Near-miss candidates: 1" in response.text
    assert "Market Climate: Mixed" in response.text
    assert "The engine only promotes setups that pass the filters." in response.text
    assert "ALERTUSDT" not in response.text
    assert "TARGETUSDT" not in response.text


def test_public_active_signals_only_include_confirmed_signal_rows(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row(), _blocked_row(), _rejected_row()])
    db_path = tmp_path / "scan_runs" / "candle_craft.db"
    _insert_runtime_attempt(
        db_path,
        signal_id="sig-alert",
        symbol="ALERTUSDT",
        alert_type="SIGNAL_CONFIRMED",
        setup_quality_score="91",
        rr_planned="3",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="112",
        tp2="120",
        tp3="130",
    )
    _insert_runtime_attempt(db_path, signal_id="sig-watch", symbol="NEARUSDT", alert_type="WATCHLIST", entry_low="90", entry_high="91")
    _insert_runtime_attempt(
        db_path,
        signal_id="sig-blocked",
        symbol="TARGETUSDT",
        alert_type="SIGNAL_CONFIRMED",
        status="blocked",
        entry_low="80",
        entry_high="81",
    )

    response = service.public_response_for("/signals")

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_inline_markup(response.reply_markup)
    assert _button_labels(response.reply_markup) == ["ALERTUSDT"]
    assert _callback_data_values(response.reply_markup) == ["public:signal:ALERTUSDT"]
    _assert_no_execution_buttons(response.reply_markup)
    assert response.text.startswith(f"{SCREEN_HEADER} Active Signals")
    assert "Current active signal records." in response.text
    assert "Select a symbol for details." in response.text
    assert "Manual execution only." not in response.text
    assert "Active signals: 1" in response.text
    assert "Symbol: ALERTUSDT" not in response.text
    assert "NEARUSDT" not in response.text
    assert "TARGETUSDT" not in response.text
    assert "REJECTUSDT" not in response.text


def test_public_active_signals_empty_state_excludes_watch_only_rows(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_near_row(), _blocked_row(), _rejected_row()])

    response = service.public_response_for("/signals")

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    assert "No active confirmed signals right now." in response.text
    assert "The engine is waiting for clean structure." in response.text
    assert "NEARUSDT" not in response.text
    assert "TARGETUSDT" not in response.text
    assert "REJECTUSDT" not in response.text


def test_public_watchlists_use_active_public_watchlist_store(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row(), _blocked_row(), _rejected_row()])

    response = service.public_response_for("/watchlists")

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_public_watchlist_controls(response.reply_markup)
    assert response.text.startswith("🐺🟠 WATCHLISTS")
    assert response.text.count("None right now.") == 3
    assert "The wolf is stalking liquidity." in response.text
    assert "No forced trades." in response.text
    assert "ALERTUSDT" not in response.text
    assert "NEARUSDT" not in response.text
    assert "scan_runs" not in response.text


def test_public_watchlist_empty_state_uses_safe_local_data_copy(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _blocked_row(), _rejected_row()])

    response = service.public_response_for("/watchlist")

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_public_watchlist_controls(response.reply_markup)
    assert response.text.count("None right now.") == 3
    assert "The wolf is stalking liquidity." in response.text
    assert "No forced trades." in response.text
    assert "ALERTUSDT" not in response.text
    assert "TARGETUSDT" not in response.text
    assert "REJECTUSDT" not in response.text


def test_public_social_missing_links_use_configured_empty_states(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    config = TelegramAdminConfig()

    social = service.public_response_for("/social", public_config=config)

    _assert_shell_screen(social.text)
    _assert_public_screen_safe(social.text)
    assert "Official Candle Craft links: N/A" in social.text
    assert "X / Twitter:\nN/A" not in social.text
    assert "Telegram:\nN/A" not in social.text
    assert "Only trust official Candle Craft links." in social.text


def test_public_donate_screen_shows_support_instructions_and_hides_missing_buttons(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    config = TelegramAdminConfig()

    donate = service.public_response_for("/donate", public_config=config)

    _assert_shell_screen(donate.text)
    _assert_public_screen_safe(donate.text)
    _assert_public_donate_copy_markup(donate.reply_markup)
    _assert_no_execution_buttons(donate.reply_markup)
    assert donate.text.startswith(f"{SCREEN_HEADER} Donate")
    assert "Support Candle Craft Intelligence" in donate.text
    assert "Voluntary donations help support:" in donate.text
    assert "• infrastructure" in donate.text
    assert "• research tooling" in donate.text
    assert "Only donate voluntarily." in donate.text
    assert "Never send funds expecting guaranteed profits, managed trading, or private execution." in donate.text
    assert "Wallets:" in donate.text
    assert "Address:" not in donate.text
    assert "N/A" in donate.text
    assert "Donation link:" not in donate.text
    assert "📋 USDT on TON" not in _button_labels(donate.reply_markup)
    assert "📋 TON" not in _button_labels(donate.reply_markup)
    assert "📋 BTC" not in _button_labels(donate.reply_markup)
    assert "Open Donation Page" not in _button_labels(donate.reply_markup)


def test_public_social_and_donate_render_configured_copy_buttons_without_raw_addresses(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    config = TelegramAdminConfig.from_settings(
        Settings(
            _env_file=None,
            candle_craft_public_logo_url="https://cdn.example.test/logo.png",
            candle_craft_x_url="https://x.example.test/candlecraft",
            candle_craft_telegram_url="https://t.me/example_candlecraft",
            candle_craft_donate_usdt_ton_address="TEST_USDT_TON_ADDRESS",
            candle_craft_donate_ton_address="TEST_TON_ADDRESS",
            candle_craft_donate_btc_address="TEST_BTC_ADDRESS",
            candle_craft_donate_url="https://donate.example.test/candlecraft",
        )
    )

    social = service.public_response_for("/social", public_config=config)
    donate = service.public_response_for("/donate", public_config=config)

    _assert_shell_screen(social.text)
    _assert_shell_screen(donate.text)
    assert "https://x.example.test/candlecraft" in social.text
    assert "https://t.me/example_candlecraft" in social.text
    assert "TEST_USDT_TON_ADDRESS" not in donate.text
    assert "TEST_TON_ADDRESS" not in donate.text
    assert "TEST_BTC_ADDRESS" not in donate.text
    assert "https://donate.example.test/candlecraft" not in donate.text
    _assert_public_donate_copy_markup(
        donate.reply_markup,
        usdt_ton="TEST_USDT_TON_ADDRESS",
        ton="TEST_TON_ADDRESS",
        btc="TEST_BTC_ADDRESS",
    )


def test_public_donate_fallback_messages_return_copy_ready_addresses(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    config = TelegramAdminConfig(
        donate_usdt_ton_address="TEST_USDT_TON_ADDRESS",
        donate_ton_address="TEST_TON_ADDRESS",
        donate_btc_address="TEST_BTC_ADDRESS",
    )
    cases = (
        (
            "/donate_usdt_ton",
            "USDT on TON",
            "TEST_USDT_TON_ADDRESS",
            "TON",
            "Send only USDT on TON to this address.",
        ),
        (
            "/donate_ton",
            "TON",
            "TEST_TON_ADDRESS",
            "TON",
            "Send only TON to this address.",
        ),
        (
            "/donate_btc",
            "BTC",
            "TEST_BTC_ADDRESS",
            "Bitcoin",
            "Send only BTC to this address.",
        ),
    )

    for command, title, address, network, send_warning in cases:
        response = service.public_response_for(command, public_config=config)

        assert response.text == _expected_public_donate_address_text(
            title,
            address,
            network=network,
            send_warning=send_warning,
        )
        _assert_public_menu_only(response.reply_markup)
        _assert_no_execution_buttons(response.reply_markup)


def test_public_donate_fallback_messages_return_not_configured_when_missing(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    config = TelegramAdminConfig()

    for command in ("/donate_usdt_ton", "/donate_ton", "/donate_btc"):
        response = service.public_response_for(command, public_config=config)

        assert response.text == "Not configured yet."
        _assert_public_menu_only(response.reply_markup)
        _assert_no_execution_buttons(response.reply_markup)


def test_public_links_and_logo_load_from_settings_and_render(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    config = TelegramAdminConfig.from_settings(
        Settings(
            _env_file=None,
            candle_craft_public_logo_url="https://cdn.example.test/logo.png",
            candle_craft_x_url="https://x.example.test/candlecraft",
            candle_craft_telegram_url="https://t.me/example_candlecraft",
            candle_craft_donate_url="https://donate.example.test/candlecraft",
        )
    )

    start = service.public_response_for("/start", public_config=config)

    assert start.photo_path is None
    assert start.photo_url == "https://cdn.example.test/logo.png"
    assert start.text == _expected_public_start_text()


def test_public_help_uses_button_guidance_instead_of_slash_command_wording(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    response = service.public_response_for("/help")

    _assert_public_screen_safe(response.text)
    assert response.text == _expected_public_help_text()
    assert "Last Scan" not in response.text
    assert "Active Signals" not in response.text
    assert "Watchlists" not in response.text
    assert "/lastscan" not in response.text
    assert "/signals" not in response.text
    assert "/watchlist" not in response.text
    _assert_public_menu_only(response.reply_markup)


def test_command_menu_cleanup_calls_telegram_safely_and_does_not_print_token(capsys) -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": True})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
    try:
        exit_code = clear_menu_script.main(
            [],
            settings=Settings(_env_file=None, telegram_bot_token="secret-token"),
            http_client=client,
        )
    finally:
        asyncio.run(client.aclose())

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "native_command_menu_status=cleared" in output
    assert "delete_commands_status=cleared" in output
    assert "menu_button_status=default" in output
    assert "secret-token" not in output
    assert [request.url.path for request in requests] == [
        "/botsecret-token/deleteMyCommands",
        "/botsecret-token/setChatMenuButton",
    ]
    assert json.loads(requests[0].content.decode("utf-8")) == {}
    assert json.loads(requests[1].content.decode("utf-8")) == {"menu_button": {"type": "default"}}


def test_command_menu_cleanup_redacts_token_on_telegram_error(capsys) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": False, "description": "Bad token secret-token"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
    try:
        exit_code = clear_menu_script.main(
            [],
            settings=Settings(_env_file=None, telegram_bot_token="secret-token"),
            http_client=client,
        )
    finally:
        asyncio.run(client.aclose())

    output = capsys.readouterr().out
    assert exit_code == 1
    assert "native_command_menu_status=failed" in output
    assert "[REDACTED]" in output
    assert "secret-token" not in output


def test_httpx_command_transport_uploads_local_public_logo_as_photo(tmp_path) -> None:
    logo_path = _write_local_logo(tmp_path)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": {"message_id": 202, "date": 1}})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.test")
    transport = HttpxTelegramAdminCommandTransport(http_client=client, api_base_url="https://telegram.test")
    try:
        results = asyncio.run(
            transport.send_message(
                bot_token="secret-token",
                chat_id="public-chat",
                message=_expected_public_start_text(),
                reply_markup={"inline_keyboard": [[{"text": "📡 Last Scan", "callback_data": "public:lastscan"}]]},
                photo_path=logo_path,
            )
        )
    finally:
        asyncio.run(client.aclose())

    assert results[0]["status"] == "sent"
    assert requests[0].url.path == "/botsecret-token/sendPhoto"
    body = requests[0].content
    assert b'name="photo"; filename="welcome.png"' in body
    assert b"name=\"caption\"" in body
    assert b"name=\"reply_markup\"" in body


def test_every_public_screen_has_brand_header_footer_and_no_execution_buttons(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row()])
    config = TelegramAdminConfig()

    for command in ("/start", "/menu", "/lastscan", "/signals", "/watchlist", "/watchlists", "/social", "/help", "/donate"):
        response = service.public_response_for(command, public_config=config)
        if command == "/help":
            assert response.text == _expected_public_help_text()
            assert response.text.endswith(SCREEN_FOOTER)
            _assert_public_menu_only(response.reply_markup)
            _assert_no_execution_buttons(response.reply_markup)
            continue
        _assert_shell_screen(response.text)
        assert response.text.startswith(f"{SCREEN_HEADER} ")
        if command in {"/watchlist", "/watchlists"}:
            assert response.text.endswith(WATCHLIST_DASHBOARD_FOOTER)
            _assert_public_watchlist_controls(response.reply_markup)
        elif command == "/donate":
            assert response.text.endswith(SCREEN_FOOTER)
            _assert_public_donate_copy_markup(response.reply_markup)
        else:
            assert response.text.endswith(SCREEN_FOOTER)
            _assert_public_menu_only(response.reply_markup)
        _assert_no_execution_buttons(response.reply_markup)


def test_public_admin_reserved_response_does_not_expose_admin_data(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row()])

    status_response = service.public_response_for("/status")
    _assert_shell_screen(status_response.text)
    _assert_public_menu_only(status_response.reply_markup)
    _assert_no_execution_buttons(status_response.reply_markup)
    assert status_response.text.startswith(f"{SCREEN_HEADER} Status")
    assert "Candle Craft public signal desk status." in status_response.text
    assert "System Desk" not in status_response.text
    assert "Integrity Desk" not in status_response.text
    assert "Configuration Desk" not in status_response.text
    assert "ALERTUSDT" not in status_response.text
    assert "run-46c" not in status_response.text

    watchlists_response = service.public_response_for("/watchlists")
    _assert_shell_screen(watchlists_response.text)
    _assert_public_watchlist_controls(watchlists_response.reply_markup)
    _assert_no_execution_buttons(watchlists_response.reply_markup)
    assert watchlists_response.text.startswith("🐺🟠 WATCHLISTS")
    assert "No forced trades." in watchlists_response.text
    assert "The wolf is stalking liquidity." in watchlists_response.text
    assert watchlists_response.text.count("None right now.") == 3
    assert "System Desk" not in watchlists_response.text
    assert "Integrity Desk" not in watchlists_response.text
    assert "Configuration Desk" not in watchlists_response.text
    assert "ALERTUSDT" not in watchlists_response.text
    assert "run-46c" not in watchlists_response.text

    for command in ("/alerts", "/audit", "/config"):
        response = service.public_response_for(command)
        _assert_shell_screen(response.text)
        _assert_public_menu_only(response.reply_markup)
        _assert_no_execution_buttons(response.reply_markup)
        assert response.text.startswith(f"{SCREEN_HEADER} Candle Craft Intelligence")
        assert "That signal desk view is not available here." in response.text
        assert "Use the buttons below to enter the signal desk." in response.text
        assert "admin" not in response.text.lower()
        assert "System Desk" not in response.text
        assert "Integrity Desk" not in response.text
        assert "Configuration Desk" not in response.text
        assert "ALERTUSDT" not in response.text
        assert "run-46c" not in response.text


def test_public_start_and_menu_updates_route_to_public_ui(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row()])
    audit_path = tmp_path / "audit.jsonl"
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_logo_url="https://cdn.example.test/candle-logo.png",
            ),
            command_service=service,
            transport=transport,
            audit_path=audit_path,
            state_path=tmp_path / "state.json",
            updates=(
                _update(20, "public-chat", "/start"),
                _update(21, "public-chat", "/menu"),
            ),
        )
    )

    assert result.delivery_status == "sent_public"
    assert result.sent_count == 2
    cleanup_calls = _cleanup_send_calls(transport)
    screen_calls = _screen_send_calls(transport)
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0]["reply_markup"] == {"remove_keyboard": True}
    assert len(screen_calls) == 2
    assert screen_calls[0]["photo_path"] is None
    assert screen_calls[0]["photo_url"] == "https://cdn.example.test/candle-logo.png"
    assert screen_calls[1]["photo_path"] is None
    assert screen_calls[1]["photo_url"] is None
    for call in screen_calls:
        assert call["chat_id"] == "public-chat"
        assert "Your AI-powered signal engine is online." in call["message"]
        assert "Welcome to the Moon Trip signal desk" in call["message"]
        assert "System Desk" not in call["message"]
        assert "Integrity Desk" not in call["message"]
        assert "Configuration Desk" not in call["message"]
        _assert_public_screen_safe(call["message"])
        _assert_public_full_menu(call["reply_markup"])
        _assert_no_execution_buttons(call["reply_markup"])
    records = _read_jsonl(audit_path)
    assert [record["response_type"] for record in records] == ["public_menu", "public_menu"]
    assert [record["is_admin"] for record in records] == [False, False]
    serialized = json.dumps(records)
    assert "public-chat" not in serialized
    assert "secret-token" not in serialized


def test_public_and_admin_start_updates_receive_configured_join_button(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row()])
    audit_path = tmp_path / "audit.jsonl"
    transport = FakeCommandTransport()
    invite_link = "https://t.me/+route-private-invite"

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                signal_channel_invite_link=invite_link,
            ),
            command_service=service,
            transport=transport,
            audit_path=audit_path,
            state_path=tmp_path / "state.json",
            updates=(
                _update(22, "public-chat", "/start"),
                _update(23, "admin-chat", "/start"),
            ),
        )
    )

    assert result.delivery_status == "sent_admin"
    assert result.sent_count == 2
    screen_calls = _screen_send_calls(transport)
    assert [call["chat_id"] for call in screen_calls] == ["public-chat", "admin-chat"]
    _assert_join_signal_channel_button(screen_calls[0]["reply_markup"], invite_link=invite_link)
    _assert_join_signal_channel_button(screen_calls[1]["reply_markup"], invite_link=invite_link)
    assert "System Desk" not in screen_calls[0]["message"]
    assert "Configuration Desk" not in screen_calls[0]["message"]
    assert "System Desk" in screen_calls[1]["message"]
    assert invite_link not in screen_calls[0]["message"]
    assert invite_link not in screen_calls[1]["message"]
    serialized = json.dumps(_read_jsonl(audit_path))
    assert invite_link not in serialized
    assert "secret-token" not in serialized
    assert "public-chat" not in serialized
    assert "admin-chat" not in serialized


def test_public_start_sends_local_logo_path_when_configured_file_exists(tmp_path) -> None:
    logo_path = _write_local_logo(tmp_path)
    service = TelegramAdminCommandService(project_root=tmp_path)
    audit_path = tmp_path / "audit.jsonl"
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_logo_path=str(Path("assets") / "telegram" / "welcome.png"),
                public_logo_url="https://cdn.example.test/candle-logo.png",
            ),
            command_service=service,
            transport=transport,
            audit_path=audit_path,
            state_path=tmp_path / "state.json",
            updates=(_update(23, "public-chat", "/start"),),
        )
    )

    assert result.delivery_status == "sent_public"
    assert result.sent_count == 1
    cleanup_calls = _cleanup_send_calls(transport)
    screen_calls = _screen_send_calls(transport)
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0]["reply_markup"] == {"remove_keyboard": True}
    assert len(screen_calls) == 1
    assert screen_calls[0]["photo_path"] == logo_path
    assert screen_calls[0]["photo_url"] is None
    assert screen_calls[0]["message"] == _expected_public_start_text()
    _assert_public_full_menu(screen_calls[0]["reply_markup"])
    records = _read_jsonl(audit_path)
    assert records[0]["delivery_status"] == "sent_public"
    serialized = json.dumps(records)
    assert "secret-token" not in serialized
    assert "public-chat" not in serialized


def test_public_start_falls_back_to_text_when_local_logo_send_fails(tmp_path) -> None:
    logo_path = _write_local_logo(tmp_path)
    service = TelegramAdminCommandService(project_root=tmp_path)
    audit_path = tmp_path / "audit.jsonl"
    transport = FakeCommandTransport(fail_photo_send_with="sendPhoto unavailable for secret-token public-chat")

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_logo_path=str(Path("assets") / "telegram" / "welcome.png"),
                public_logo_url="https://cdn.example.test/candle-logo.png",
            ),
            command_service=service,
            transport=transport,
            audit_path=audit_path,
            state_path=tmp_path / "state.json",
            updates=(_update(24, "public-chat", "/start"),),
        )
    )

    assert result.delivery_status == "sent_public"
    assert result.sent_count == 1
    cleanup_calls = _cleanup_send_calls(transport)
    screen_calls = _screen_send_calls(transport)
    assert len(cleanup_calls) == 1
    assert len(screen_calls) == 2
    assert screen_calls[0]["photo_path"] == logo_path
    assert screen_calls[0]["photo_url"] is None
    assert screen_calls[1]["photo_path"] is None
    assert screen_calls[1]["photo_url"] is None
    assert screen_calls[1]["message"] == _expected_public_start_text()
    _assert_public_full_menu(screen_calls[1]["reply_markup"])
    records = _read_jsonl(audit_path)
    assert records[0]["delivery_status"] == "sent_public"
    serialized = json.dumps(records)
    assert "secret-token" not in serialized
    assert "public-chat" not in serialized


def test_public_start_falls_back_to_text_when_logo_send_is_unavailable(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    audit_path = tmp_path / "audit.jsonl"
    transport = FakeCommandTransport(fail_photo_send_with="sendPhoto unavailable for secret-token public-chat")

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_logo_url="https://cdn.example.test/candle-logo.png",
            ),
            command_service=service,
            transport=transport,
            audit_path=audit_path,
            state_path=tmp_path / "state.json",
            updates=(_update(22, "public-chat", "/start"),),
        )
    )

    assert result.delivery_status == "sent_public"
    assert result.sent_count == 1
    cleanup_calls = _cleanup_send_calls(transport)
    screen_calls = _screen_send_calls(transport)
    assert len(cleanup_calls) == 1
    assert len(screen_calls) == 2
    assert screen_calls[0]["photo_path"] is None
    assert screen_calls[0]["photo_url"] == "https://cdn.example.test/candle-logo.png"
    assert screen_calls[1]["photo_path"] is None
    assert screen_calls[1]["photo_url"] is None
    assert screen_calls[1]["message"] == _expected_public_start_text()
    _assert_public_full_menu(screen_calls[1]["reply_markup"])
    records = _read_jsonl(audit_path)
    assert records[0]["delivery_status"] == "sent_public"
    serialized = json.dumps(records)
    assert "secret-token" not in serialized
    assert "public-chat" not in serialized


def test_public_user_cannot_access_admin_only_commands(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row()])
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(
                _update(30, "public-chat", "/status"),
                _update(31, "public-chat", "/audit"),
                _update(32, "public-chat", "/config"),
            ),
        )
    )

    assert result.delivery_status == "sent_public"
    assert result.sent_count == 3
    assert len(transport.send_calls) == 3
    status_call = transport.send_calls[0]
    assert "Candle Craft public signal desk status." in status_call["message"]
    assert "System Desk" not in status_call["message"]
    assert "Integrity Desk" not in status_call["message"]
    assert "Configuration Desk" not in status_call["message"]
    assert "ALERTUSDT" not in status_call["message"]
    assert "run-46c" not in status_call["message"]
    _assert_public_menu_only(status_call["reply_markup"])
    _assert_no_execution_buttons(status_call["reply_markup"])
    for call in transport.send_calls[1:]:
        assert "That signal desk view is not available here." in call["message"]
        assert "Use the buttons below to enter the signal desk." in call["message"]
        assert "admin" not in call["message"].lower()
        assert "System Desk" not in call["message"]
        assert "Integrity Desk" not in call["message"]
        assert "Configuration Desk" not in call["message"]
        assert "ALERTUSDT" not in call["message"]
        assert "run-46c" not in call["message"]
        _assert_public_menu_only(call["reply_markup"])
        _assert_no_execution_buttons(call["reply_markup"])


def test_public_callbacks_route_to_public_screens(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row()])
    transport = FakeCommandTransport()
    updates = tuple(
        _callback_update(update_id, "public-chat", callback_data)
        for update_id, callback_data in enumerate(PUBLIC_CALLBACK_COMMANDS, start=40)
    )

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=updates,
            limit=len(updates),
        )
    )

    assert result.delivery_status == "sent_public"
    assert result.sent_count == len(PUBLIC_CALLBACK_COMMANDS)
    assert [call["callback_query_id"] for call in transport.answer_callback_calls] == [
        f"callback-{update_id}" for update_id in range(40, 40 + len(PUBLIC_CALLBACK_COMMANDS))
    ]
    cleanup_calls = _cleanup_send_calls(transport)
    screen_calls = _screen_send_calls(transport)
    assert len(cleanup_calls) == 1
    assert len(screen_calls) == len(PUBLIC_CALLBACK_COMMANDS)

    disabled_count = 12
    for call in screen_calls[:disabled_count]:
        assert "This public dashboard section is currently disabled for launch." in call["message"]
        assert "Use the main menu to join the signal channel" in call["message"]
        _assert_public_menu_only(call["reply_markup"])
        assert _callback_data_values(call["reply_markup"]) == ["public:menu"]
        _assert_public_screen_safe(call["message"])
        assert "System Desk" not in call["message"]
        assert "Integrity Desk" not in call["message"]

    assert "Social" in screen_calls[disabled_count]["message"]
    assert screen_calls[disabled_count + 1]["message"] == _expected_public_help_text()
    assert screen_calls[disabled_count + 2]["message"] == _expected_public_help_text()
    assert "Donate" in screen_calls[disabled_count + 3]["message"]
    assert screen_calls[disabled_count + 4]["message"] == "Not configured yet."
    assert screen_calls[disabled_count + 5]["message"] == "Not configured yet."
    assert screen_calls[disabled_count + 6]["message"] == "Not configured yet."
    assert "Welcome to the Moon Trip signal desk" in screen_calls[disabled_count + 7]["message"]
    _assert_public_donate_copy_markup(screen_calls[disabled_count + 3]["reply_markup"])
    _assert_no_execution_buttons(screen_calls[disabled_count + 3]["reply_markup"])
    for call in screen_calls[disabled_count + 4 : disabled_count + 7]:
        _assert_public_menu_only(call["reply_markup"])
        assert _callback_data_values(call["reply_markup"]) == ["public:menu"]
        _assert_no_execution_buttons(call["reply_markup"])
    _assert_public_full_menu(screen_calls[disabled_count + 7]["reply_markup"])
    records = _read_jsonl(tmp_path / "audit.jsonl")
    assert [record["command"] for record in records] == list(PUBLIC_CALLBACK_COMMANDS.values())
    assert all(record["is_admin"] is False for record in records)
    serialized = json.dumps(records)
    assert "secret-token" not in serialized
    assert "public-chat" not in serialized


def test_public_users_can_access_configured_donation_copy_buttons(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                donate_usdt_ton_address="TEST_USDT_TON_ADDRESS",
                donate_ton_address="TEST_TON_ADDRESS",
                donate_btc_address="TEST_BTC_ADDRESS",
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=(_callback_update(50, "public-chat", "public:donate"),),
        )
    )

    assert result.delivery_status == "sent_public"
    assert result.sent_count == 1
    screen_calls = _screen_send_calls(transport)
    assert len(screen_calls) == 1
    assert screen_calls[0]["chat_id"] == "public-chat"
    assert "Support Candle Craft Intelligence" in screen_calls[0]["message"]
    assert "Only donate voluntarily." in screen_calls[0]["message"]
    assert "Never send funds expecting guaranteed profits, managed trading, or private execution." in screen_calls[0]["message"]
    assert "TEST_USDT_TON_ADDRESS" not in screen_calls[0]["message"]
    assert "TEST_TON_ADDRESS" not in screen_calls[0]["message"]
    assert "TEST_BTC_ADDRESS" not in screen_calls[0]["message"]
    _assert_public_donate_copy_markup(
        screen_calls[0]["reply_markup"],
        usdt_ton="TEST_USDT_TON_ADDRESS",
        ton="TEST_TON_ADDRESS",
        btc="TEST_BTC_ADDRESS",
    )
    _assert_no_execution_buttons(screen_calls[0]["reply_markup"])
    assert "System Desk" not in screen_calls[0]["message"]
    assert "Integrity Desk" not in screen_calls[0]["message"]
    assert "Configuration Desk" not in screen_calls[0]["message"]
    records = _read_jsonl(tmp_path / "audit.jsonl")
    assert records[0]["command"] == "/donate"
    assert records[0]["is_admin"] is False
    serialized = json.dumps(records)
    assert "secret-token" not in serialized
    assert "public-chat" not in serialized


def test_admin_callbacks_route_to_admin_screens(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row()])
    transport = FakeCommandTransport()
    updates = tuple(
        _callback_update(update_id, "admin-chat", callback_data)
        for update_id, callback_data in enumerate(ADMIN_CALLBACK_COMMANDS, start=60)
    )

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                wolf_briefing_enabled=True,
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=updates,
        )
    )

    assert result.delivery_status == "sent_admin"
    assert result.sent_count == len(ADMIN_CALLBACK_COMMANDS)
    assert [call["callback_query_id"] for call in transport.answer_callback_calls] == [
        f"callback-{update_id}" for update_id in range(60, 60 + len(ADMIN_CALLBACK_COMMANDS))
    ]
    cleanup_calls = _cleanup_send_calls(transport)
    screen_calls = _screen_send_calls(transport)
    assert len(cleanup_calls) == 1
    assert len(screen_calls) == len(ADMIN_CALLBACK_COMMANDS)
    assert "WOLF BRIEFING PREVIEW" in screen_calls[0]["message"]
    assert "System Desk" in screen_calls[1]["message"]
    assert "Alert Desk" in screen_calls[2]["message"]
    assert "WATCHLISTS" in screen_calls[3]["message"]
    assert "Integrity Desk" in screen_calls[4]["message"]
    assert "Configuration Desk" in screen_calls[5]["message"]
    assert "Command Guide" in screen_calls[6]["message"]
    assert "Candle Craft Intelligence" in screen_calls[7]["message"]
    assert WOLF_BRIEFING_PUBLISH_BUTTON_LABEL in _button_labels(screen_calls[0]["reply_markup"])
    assert _callback_data_values(screen_calls[0]["reply_markup"]) == [
        "admin:wolf_publish",
        "admin:wolf_refresh",
        "admin:wolf_cancel",
    ]
    for call in (screen_calls[1], screen_calls[2], screen_calls[4], screen_calls[5], screen_calls[6]):
        _assert_admin_menu_only(call["reply_markup"])
        assert _callback_data_values(call["reply_markup"]) == ["admin:menu"]
    _assert_admin_watchlist_controls(screen_calls[3]["reply_markup"])
    _assert_admin_full_menu(screen_calls[7]["reply_markup"])
    records = _read_jsonl(tmp_path / "audit.jsonl")
    assert [record["command"] for record in records] == list(ADMIN_CALLBACK_COMMANDS.values())
    assert all(record["is_admin"] is True for record in records)
    serialized = json.dumps(records)
    assert "secret-token" not in serialized
    assert "admin-chat" not in serialized


def test_public_user_cannot_access_admin_callbacks(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row()])
    transport = FakeCommandTransport()
    updates = tuple(
        _callback_update(update_id, "public-chat", callback_data)
        for update_id, callback_data in enumerate(ADMIN_CALLBACK_COMMANDS, start=80)
    )

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            command_service=service,
            transport=transport,
            audit_path=tmp_path / "audit.jsonl",
            state_path=tmp_path / "state.json",
            updates=updates,
        )
    )

    assert result.delivery_status == "sent_public"
    assert result.sent_count == len(ADMIN_CALLBACK_COMMANDS)
    assert len(transport.answer_callback_calls) == len(ADMIN_CALLBACK_COMMANDS)
    assert _cleanup_send_calls(transport) == []
    screen_calls = _screen_send_calls(transport)
    assert len(screen_calls) == len(ADMIN_CALLBACK_COMMANDS)
    for call in screen_calls:
        assert "Candle Craft public signal desk status." in call["message"]
        assert "admin" not in call["message"].lower()
        assert "System Desk" not in call["message"]
        assert "Integrity Desk" not in call["message"]
        assert "Configuration Desk" not in call["message"]
        assert "ALERTUSDT" not in call["message"]
        _assert_public_menu_only(call["reply_markup"])
        _assert_no_execution_buttons(call["reply_markup"])
    records = _read_jsonl(tmp_path / "audit.jsonl")
    assert all(record["command"] == "/status" for record in records)
    assert all(record["is_admin"] is False for record in records)
    serialized = json.dumps(records)
    assert "secret-token" not in serialized
    assert "public-chat" not in serialized


def test_non_admin_chat_id_receives_public_lastscan_without_admin_data(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row()])
    audit_path = tmp_path / "scan_runs" / "admin_commands" / "commands.jsonl"
    transport = FakeCommandTransport()

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            command_service=service,
            transport=transport,
            audit_path=audit_path,
            state_path=tmp_path / "scan_runs" / "admin_commands" / "state.json",
            updates=(_update(1, "stranger-chat", "/lastscan"),),
        )
    )

    assert result.delivery_status == "sent_public"
    assert len(transport.send_calls) == 1
    assert transport.send_calls[0]["chat_id"] == "stranger-chat"
    assert transport.send_calls[0]["photo_url"] is None
    _assert_public_menu_only(transport.send_calls[0]["reply_markup"])
    _assert_no_execution_buttons(transport.send_calls[0]["reply_markup"])
    assert "Last Scan" in transport.send_calls[0]["message"]
    assert "System Desk" not in transport.send_calls[0]["message"]
    assert "Integrity Desk" not in transport.send_calls[0]["message"]
    assert "Configuration Desk" not in transport.send_calls[0]["message"]
    assert "VALIDUSDT" not in transport.send_calls[0]["message"]
    assert "scan_runs/latest_scan.json" not in transport.send_calls[0]["message"]
    records = _read_jsonl(audit_path)
    assert records[0]["delivery_status"] == "sent_public"
    assert records[0]["is_admin"] is False
    serialized = json.dumps(records)
    assert "stranger-chat" not in serialized
    assert "secret-token" not in serialized


def test_disabled_telegram_config_skips_network_safely(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row()])
    transport = FakeCommandTransport(fail_get=True)

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(admin_enabled=False, dry_run=True),
            command_service=service,
            transport=transport,
            state_path=tmp_path / "state.json",
            audit_path=tmp_path / "audit.jsonl",
        )
    )

    assert result.delivery_status == "skipped_disabled"
    assert transport.get_calls == []
    assert transport.send_calls == []


def test_dry_run_command_processing_does_not_call_live_send(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row()])
    transport = FakeCommandTransport()
    audit_path = tmp_path / "audit.jsonl"

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
            ),
            command_service=service,
            transport=transport,
            audit_path=audit_path,
            state_path=tmp_path / "state.json",
            updates=(_update(2, "admin-chat", "/status"),),
            dry_run=True,
        )
    )

    assert result.delivery_status == "dry_run"
    assert transport.send_calls == []
    records = _read_jsonl(audit_path)
    assert records[0]["delivery_status"] == "dry_run"


def test_enabled_admin_command_uses_fake_client_and_sends_exactly_one_reply(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row()])
    transport = FakeCommandTransport(updates=(_update(3, "admin-chat", "/status"),))

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
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

    assert result.delivery_status == "sent_admin"
    assert len(transport.get_calls) == 1
    assert len(transport.send_calls) == 1
    assert transport.send_calls[0]["chat_id"] == "admin-chat"
    assert "System Desk" in transport.send_calls[0]["message"]
    assert "Run: run-46c" in transport.send_calls[0]["message"]
    assert transport.send_calls[0]["reply_markup"]["inline_keyboard"][0][0]["text"] == "↩ Back to Menu"
    assert transport.send_calls[0]["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == "admin:menu"


def test_command_processor_works_when_admin_reports_are_disabled(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row()])
    transport = FakeCommandTransport(updates=(_update(33, "admin-chat", "/status"),))
    config = TelegramAdminConfig(
        admin_enabled=False,
        commands_enabled=True,
        admin_reports_enabled=False,
        dry_run=False,
        bot_token="secret-token",
        admin_chat_id="admin-chat",
    )

    result = asyncio.run(
        process_telegram_admin_commands(
            config=config,
            command_service=service,
            transport=transport,
            state_path=tmp_path / "state.json",
            audit_path=tmp_path / "audit.jsonl",
        )
    )

    assert config.command_ui_enabled is True
    assert config.admin_report_enabled is False
    assert result.delivery_status == "sent_admin"
    assert len(transport.get_calls) == 1
    assert len(transport.send_calls) == 1
    assert transport.send_calls[0]["chat_id"] == "admin-chat"
    assert "System Desk" in transport.send_calls[0]["message"]
    serialized = json.dumps(_read_jsonl(tmp_path / "audit.jsonl"))
    assert "secret-token" not in serialized


def test_public_and_vip_channel_ids_do_not_receive_command_ui(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row()])
    transport = FakeCommandTransport()
    audit_path = tmp_path / "audit.jsonl"

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                public_channel_id="public-channel",
                vip_channel_id="vip-channel",
            ),
            command_service=service,
            transport=transport,
            state_path=tmp_path / "state.json",
            audit_path=audit_path,
            updates=(
                _update(4, "public-channel", "/status"),
                _update(5, "vip-channel", "/near"),
                _update(6, "public-channel", "/watchlists"),
                {"update_id": 7, "message": {"chat": {"id": "group-chat", "type": "group"}, "text": "/watchlists"}},
                {
                    "update_id": 8,
                    "message": {"chat": {"id": "supergroup-chat", "type": "supergroup"}, "text": "/watchlists"},
                },
                {"update_id": 9, "channel_post": {"chat": {"id": "channel-chat", "type": "channel"}, "text": "/watchlists"}},
            ),
        )
    )

    assert result.delivery_status == "ignored_unauthorized"
    assert result.sent_count == 0
    assert transport.send_calls == []
    assert not audit_path.exists()


def test_update_offset_prevents_duplicate_replies_for_same_update_id(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row()])
    transport = FakeCommandTransport()
    state_path = tmp_path / "state.json"
    audit_path = tmp_path / "audit.jsonl"
    config = TelegramAdminConfig(
        admin_enabled=True,
        dry_run=False,
        bot_token="secret-token",
        admin_chat_id="admin-chat",
    )
    updates = (_update(6, "admin-chat", "/status"),)

    first = asyncio.run(
        process_telegram_admin_commands(
            config=config,
            command_service=service,
            transport=transport,
            state_path=state_path,
            audit_path=audit_path,
            updates=updates,
        )
    )
    second = asyncio.run(
        process_telegram_admin_commands(
            config=config,
            command_service=service,
            transport=transport,
            state_path=state_path,
            audit_path=audit_path,
            updates=updates,
        )
    )

    assert first.sent_count == 1
    assert second.processed_count == 0
    assert len(transport.send_calls) == 1


def test_command_audit_records_are_written_safely_and_redact_secrets(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row()])
    audit_path = tmp_path / "audit.jsonl"
    invite_link = "https://t.me/+audit-private-invite"
    transport = FakeCommandTransport(
        fail_send_with=f"Telegram rejected secret-token for admin-chat and {invite_link}"
    )

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig(
                admin_enabled=True,
                dry_run=False,
                bot_token="secret-token",
                admin_chat_id="admin-chat",
                signal_channel_invite_link=invite_link,
            ),
            command_service=service,
            transport=transport,
            audit_path=audit_path,
            state_path=tmp_path / "state.json",
            updates=(_update(7, "admin-chat", "/status"),),
        )
    )

    assert result.delivery_status == "failed"
    records = _read_jsonl(audit_path)
    assert records[0]["command"] == "/status"
    assert records[0]["is_admin"] is True
    assert records[0]["response_type"] == "status"
    assert records[0]["delivery_status"] == "failed"
    assert "[REDACTED]" in records[0]["error_message"]
    serialized = json.dumps(records)
    assert "secret-token" not in serialized
    assert "admin-chat" not in serialized
    assert invite_link not in serialized


def test_script_dry_run_disabled_skips_network_and_prints_preview(tmp_path, capsys) -> None:
    transport = FakeCommandTransport(fail_get=True)

    exit_code = process_script.main(
        [
            "--once",
            "--dry-run",
            "--show-preview",
            "--state-path",
            str(tmp_path / "state.json"),
            "--audit-path",
            str(tmp_path / "audit.jsonl"),
        ],
        settings=Settings(
            _env_file=None,
            telegram_admin_enabled=False,
            telegram_commands_enabled=False,
            telegram_admin_reports_enabled=False,
            telegram_dry_run=True,
        ),
        transport=transport,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "processor_status=skipped_disabled" in output
    assert "processed_count=0" in output
    assert "preview_1=skipped_disabled" in output
    assert transport.get_calls == []
    assert transport.send_calls == []


def test_command_ui_runs_when_legacy_admin_flag_is_disabled(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row()])
    transport = FakeCommandTransport(updates=(_update(41, "admin-chat", "/status"),))

    result = asyncio.run(
        process_telegram_admin_commands(
            config=TelegramAdminConfig.from_settings(
                Settings(
                    _env_file=None,
                    telegram_admin_enabled=False,
                    telegram_commands_enabled=True,
                    telegram_admin_reports_enabled=False,
                    telegram_dry_run=False,
                    telegram_bot_token="secret-token",
                    telegram_admin_chat_id="admin-chat",
                )
            ),
            command_service=service,
            transport=transport,
            state_path=tmp_path / "state.json",
            audit_path=tmp_path / "audit.jsonl",
        )
    )

    assert result.delivery_status == "sent_admin"
    assert len(transport.get_calls) == 1
    assert len(transport.send_calls) == 1
    assert "System Desk" in transport.send_calls[0]["message"]
