from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.alerts.integrity_manifest import build_alert_integrity_manifest
from app.core.config import Settings
from app.telegram_admin import TelegramAdminCommandService, TelegramAdminConfig, process_telegram_admin_commands
from app.telegram_admin.commands import (
    ADMIN_MENU_BUTTON_ROWS,
    PUBLIC_MENU_BUTTON_ROWS,
    SCREEN_FOOTER,
    SCREEN_HEADER,
    normalize_admin_command,
)
from scripts import process_telegram_admin_commands as process_script


class FakeCommandTransport:
    def __init__(
        self,
        updates: tuple[Mapping[str, Any], ...] = (),
        *,
        fail_send_with: str | None = None,
        fail_get: bool = False,
    ) -> None:
        self.updates = updates
        self.fail_send_with = fail_send_with
        self.fail_get = fail_get
        self.get_calls: list[dict[str, Any]] = []
        self.send_calls: list[dict[str, Any]] = []

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
        photo_url=None,
    ):
        self.send_calls.append(
            {
                "bot_token": bot_token,
                "chat_id": chat_id,
                "message": message,
                "reply_markup": reply_markup,
                "photo_url": photo_url,
            }
        )
        if self.fail_send_with is not None:
            return ({"status": "failed", "error": self.fail_send_with},)
        return ({"status": "sent", "message_id": 101, "chat_id": chat_id},)


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


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _assert_shell_screen(text: str) -> None:
    assert text.startswith(SCREEN_HEADER)
    assert text.endswith(SCREEN_FOOTER)


def _keyboard_labels(reply_markup: Mapping[str, Any] | None) -> list[str]:
    if reply_markup is None:
        return []
    keyboard = reply_markup.get("keyboard")
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


def _assert_public_menu_only(reply_markup: Mapping[str, Any] | None) -> None:
    labels = _keyboard_labels(reply_markup)
    expected = [label for row in PUBLIC_MENU_BUTTON_ROWS for label in row]
    assert labels == expected
    admin_labels = {label for row in ADMIN_MENU_BUTTON_ROWS for label in row}
    assert admin_labels.isdisjoint(labels)


def _assert_no_execution_buttons(reply_markup: Mapping[str, Any] | None) -> None:
    labels = _keyboard_labels(reply_markup)
    forbidden = ("buy", "sell", "execute", "order", "withdraw", "transfer")
    for label in labels:
        assert not any(word in label.lower() for word in forbidden)


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
    assert response.reply_markup is not None
    assert response.reply_markup["keyboard"][0][0]["text"] == "📊 Status"
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
    assert "/audit" in response.text
    assert "View safety and duplicate checks." in response.text
    assert "No weak setup promotion." in response.text


def test_menu_button_labels_normalize_to_commands() -> None:
    assert normalize_admin_command("📊 Status") == "/status"
    assert normalize_admin_command("🚨 Alerts") == "/alerts"
    assert normalize_admin_command("👁 Watchlists") == "/watchlists"
    assert normalize_admin_command("🧾 Integrity") == "/integrity"
    assert normalize_admin_command("⚙️ Config") == "/config"
    assert normalize_admin_command("❓ Guide") == "/guide"
    assert normalize_admin_command("📡 Last Scan") == "/lastscan"
    assert normalize_admin_command("🔥 Active Signals") == "/signals"
    assert normalize_admin_command("👁 Watchlist Signals") == "/watchlist"
    assert normalize_admin_command("🌐 Social") == "/social"
    assert normalize_admin_command("❓ Help") == "/help"
    assert normalize_admin_command("🧡 Donate") == "/donate"


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
    assert "Market regime: Mixed" in response.text
    assert "Regime confidence: 72" in response.text
    assert "Symbols scanned: 3" in response.text
    assert "Confirmed setups: 1" in response.text
    assert "Watch candidates: 1" in response.text
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
    assert "Top Watch Candidates" in response.text
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


def test_watchlists_screen_lists_active_watch_candidates_and_presets(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_near_row(), _blocked_row(), _valid_row()])

    response = service.response_for("/watchlists")

    _assert_shell_screen(response.text)
    assert response.text.startswith(f"{SCREEN_HEADER} Watchlist Desk")
    assert "Watch candidates: 1" in response.text
    assert "Symbol: NEARUSDT" in response.text
    assert "Direction: Short" in response.text
    assert "Grade: B" in response.text
    assert "Score: 62" in response.text
    assert "Status: Monitoring" in response.text
    assert "TARGETUSDT" not in response.text
    assert "Majors: 5 symbols" in response.text
    assert "High-liquidity meme: 6 symbols" in response.text


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
            vip_channel_id="vip-channel",
        ),
    )

    _assert_shell_screen(response.text)
    assert response.text.startswith(f"{SCREEN_HEADER} Configuration Desk")
    assert "Manual mode: Active" in response.text
    assert "Execution: Disabled" in response.text
    assert "Telegram alerts: Enabled" in response.text
    assert "Test mode: Inactive" in response.text
    assert "Bot token: Hidden" in response.text
    assert "Chat ID: Hidden" in response.text
    assert "secret-token" not in response.text
    assert "123456789" not in response.text
    assert "public-channel" not in response.text
    assert "vip-channel" not in response.text


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
    assert "Welcome to the Candle Craft signal desk." in response.text
    assert "📡 Last Scan" in response.text
    assert "🔥 Active Signals" in response.text
    assert "👁 Watchlist Signals" in response.text
    assert "System Desk" not in response.text
    assert "Integrity Desk" not in response.text
    assert "Configuration Desk" not in response.text
    assert response.photo_url == "https://cdn.example.test/candle-logo.png"


def test_public_menu_has_only_public_buttons_and_no_logo_when_missing(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    response = service.public_response_for("/menu", public_config=TelegramAdminConfig())

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_public_menu_only(response.reply_markup)
    _assert_no_execution_buttons(response.reply_markup)
    assert "Your market-structure command center." not in response.text
    assert "System Desk" not in response.text
    assert "Integrity Desk" not in response.text
    assert "Configuration Desk" not in response.text
    assert response.photo_url is None


def test_public_lastscan_shows_summary_without_admin_internals(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row(), _blocked_row(), _rejected_row()])

    response = service.public_response_for("/lastscan")

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_public_menu_only(response.reply_markup)
    assert response.text.startswith(f"{SCREEN_HEADER} Last Scan")
    assert "Last run: run-46c" in response.text
    assert "Symbols scanned: 4" in response.text
    assert "Confirmed setups: 1" in response.text
    assert "Watchlist setups: 1" in response.text
    assert "Market regime: Mixed" in response.text
    assert "ALERTUSDT" not in response.text
    assert "TARGETUSDT" not in response.text


def test_public_active_signals_only_include_confirmed_signal_rows(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row(), _blocked_row(), _rejected_row()])

    response = service.public_response_for("/signals")

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_public_menu_only(response.reply_markup)
    assert response.text.startswith(f"{SCREEN_HEADER} Active Signals")
    assert "Confirmed Candle Craft setups." in response.text
    assert "Symbol: ALERTUSDT" in response.text
    assert "Direction: Long" in response.text
    assert "Entry: 100 - 102" in response.text
    assert "Stop: 95" in response.text
    assert "Targets: 112, 120" in response.text
    assert "Status: Confirmed setup" in response.text
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


def test_public_watchlist_signals_are_clearly_conditional(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row(), _blocked_row(), _rejected_row()])

    response = service.public_response_for("/watchlist")

    _assert_shell_screen(response.text)
    _assert_public_screen_safe(response.text)
    _assert_public_menu_only(response.reply_markup)
    assert response.text.startswith(f"{SCREEN_HEADER} Watchlist Signals")
    assert "Conditional setups being monitored." in response.text
    assert "Watchlist does not mean confirmed signal." in response.text
    assert "Symbol: NEARUSDT" in response.text
    assert "Direction: Short" in response.text
    assert "Grade: B" in response.text
    assert "Status: Monitoring" in response.text
    assert "Waiting for: Stronger confirmation" in response.text
    assert "Invalidation: N/A" in response.text
    assert "ALERTUSDT" not in response.text
    assert "TARGETUSDT" not in response.text
    assert "REJECTUSDT" not in response.text


def test_public_social_and_donate_missing_links_use_configured_empty_states(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    config = TelegramAdminConfig()

    social = service.public_response_for("/social", public_config=config)
    donate = service.public_response_for("/donate", public_config=config)

    _assert_shell_screen(social.text)
    _assert_shell_screen(donate.text)
    _assert_public_screen_safe(social.text)
    _assert_public_screen_safe(donate.text)
    assert "X / Twitter:\nN/A" in social.text
    assert "Telegram:\nN/A" in social.text
    assert "Donation link:\nNot configured yet" in donate.text


def test_public_links_and_logo_load_from_settings_and_render(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)
    config = TelegramAdminConfig.from_settings(
        Settings(
            candle_craft_public_logo_url="https://cdn.example.test/logo.png",
            candle_craft_x_url="https://x.example.test/candlecraft",
            candle_craft_telegram_url="https://t.me/example_candlecraft",
            candle_craft_donate_url="https://donate.example.test/candlecraft",
        )
    )

    start = service.public_response_for("/start", public_config=config)
    social = service.public_response_for("/social", public_config=config)
    donate = service.public_response_for("/donate", public_config=config)

    assert start.photo_url == "https://cdn.example.test/logo.png"
    assert "https://x.example.test/candlecraft" in social.text
    assert "https://t.me/example_candlecraft" in social.text
    assert "https://donate.example.test/candlecraft" in donate.text


def test_every_public_screen_has_brand_header_footer_and_no_execution_buttons(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row(), _near_row()])
    config = TelegramAdminConfig()

    for command in ("/start", "/menu", "/lastscan", "/signals", "/watchlist", "/social", "/help", "/donate"):
        response = service.public_response_for(command, public_config=config)
        _assert_shell_screen(response.text)
        assert response.text.startswith(f"{SCREEN_HEADER} ")
        assert response.text.endswith(SCREEN_FOOTER)
        _assert_public_menu_only(response.reply_markup)
        _assert_no_execution_buttons(response.reply_markup)


def test_public_admin_reserved_response_does_not_expose_admin_data(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_alert_row()])

    for command in ("/status", "/alerts", "/watchlists", "/audit", "/config"):
        response = service.public_response_for(command)
        _assert_shell_screen(response.text)
        _assert_public_menu_only(response.reply_markup)
        _assert_no_execution_buttons(response.reply_markup)
        assert response.text.startswith(f"{SCREEN_HEADER} Candle Craft Intelligence")
        assert "This command is reserved for the admin desk." in response.text
        assert "Use /menu to open the public signal menu." in response.text
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
    assert len(transport.send_calls) == 2
    assert transport.send_calls[0]["photo_url"] == "https://cdn.example.test/candle-logo.png"
    assert transport.send_calls[1]["photo_url"] is None
    for call in transport.send_calls:
        assert call["chat_id"] == "public-chat"
        assert "Welcome to the Candle Craft signal desk." in call["message"]
        assert "System Desk" not in call["message"]
        assert "Integrity Desk" not in call["message"]
        assert "Configuration Desk" not in call["message"]
        _assert_public_screen_safe(call["message"])
        _assert_public_menu_only(call["reply_markup"])
        _assert_no_execution_buttons(call["reply_markup"])
    records = _read_jsonl(audit_path)
    assert [record["response_type"] for record in records] == ["public_menu", "public_menu"]
    assert [record["is_admin"] for record in records] == [False, False]
    serialized = json.dumps(records)
    assert "public-chat" not in serialized
    assert "secret-token" not in serialized


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
    for call in transport.send_calls:
        assert "This command is reserved for the admin desk." in call["message"]
        assert "Use /menu to open the public signal menu." in call["message"]
        assert "System Desk" not in call["message"]
        assert "Integrity Desk" not in call["message"]
        assert "Configuration Desk" not in call["message"]
        assert "ALERTUSDT" not in call["message"]
        assert "run-46c" not in call["message"]
        _assert_public_menu_only(call["reply_markup"])
        _assert_no_execution_buttons(call["reply_markup"])


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
    assert transport.send_calls[0]["reply_markup"]["keyboard"][0][0]["text"] == "📊 Status"


def test_public_and_vip_channel_ids_receive_public_reserved_screen(tmp_path) -> None:
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
            ),
        )
    )

    assert result.delivery_status == "sent_public"
    assert len(transport.send_calls) == 2
    assert [call["chat_id"] for call in transport.send_calls] == ["public-channel", "vip-channel"]
    for call in transport.send_calls:
        assert "This command is reserved for the admin desk." in call["message"]
        assert "Use /menu to open the public signal menu." in call["message"]
        assert "System Desk" not in call["message"]
        assert "Integrity Desk" not in call["message"]
        assert "Configuration Desk" not in call["message"]
        assert "VALIDUSDT" not in call["message"]
        _assert_public_menu_only(call["reply_markup"])
        _assert_no_execution_buttons(call["reply_markup"])
    records = _read_jsonl(audit_path)
    assert [record["delivery_status"] for record in records] == ["sent_public", "sent_public"]
    assert [record["is_admin"] for record in records] == [False, False]
    serialized = json.dumps(records)
    assert "public-channel" not in serialized
    assert "vip-channel" not in serialized


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
    transport = FakeCommandTransport(fail_send_with="Telegram rejected secret-token for admin-chat")

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
        settings=Settings(telegram_admin_enabled=False, telegram_dry_run=True),
        transport=transport,
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "processor_status=skipped_disabled" in output
    assert "processed_count=0" in output
    assert "preview_1=skipped_disabled" in output
    assert transport.get_calls == []
    assert transport.send_calls == []
