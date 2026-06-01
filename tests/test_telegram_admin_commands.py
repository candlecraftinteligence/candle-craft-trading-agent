from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.core.config import Settings
from app.telegram_admin import TelegramAdminCommandService, TelegramAdminConfig, process_telegram_admin_commands
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
        self.send_calls: list[dict[str, str]] = []

    async def get_updates(self, *, bot_token: str, offset: int | None, limit: int, timeout: int):
        self.get_calls.append({"bot_token": bot_token, "offset": offset, "limit": limit, "timeout": timeout})
        if self.fail_get:
            raise AssertionError("getUpdates should not be called")
        return self.updates

    async def send_message(self, *, bot_token: str, chat_id: str, message: str):
        self.send_calls.append({"bot_token": bot_token, "chat_id": chat_id, "message": message})
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


def _update(update_id: int, chat_id: str, text: str) -> dict[str, Any]:
    return {"update_id": update_id, "message": {"chat": {"id": chat_id}, "text": text}}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_start_response_contains_admin_desk_welcome_and_command_list(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    response = service.response_for("/start")

    assert "Candle Craft Intelligence admin desk" in response.text
    for command in ("/start", "/help", "/status", "/lastscan", "/near", "/blocked"):
        assert command in response.text


def test_help_response_lists_commands_and_safety_note(tmp_path) -> None:
    service = TelegramAdminCommandService(project_root=tmp_path)

    response = service.response_for("/help")

    assert "/status - latest scan metadata" in response.text
    assert "No public/VIP posting" in response.text
    assert "No execution enabled" in response.text


def test_status_loads_latest_manifest_and_formats_core_counts(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row(), _near_row(), _blocked_row()])

    response = service.response_for("/status")

    assert "Latest run_id: run-46c" in response.text
    assert "Universe: manual test universe (manual)" in response.text
    assert "Market regime: MIXED" in response.text
    assert "Symbols scanned: 3" in response.text
    assert "Valid setups: 1" in response.text
    assert "Near misses: 1" in response.text
    assert "Target-integrity blocked: 1" in response.text
    assert "Latest scan path: scan_runs/latest_scan.json" in response.text
    assert "No execution enabled" in response.text


def test_lastscan_summarizes_latest_scan_without_raw_json_dump(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_valid_row(), _near_row()])

    response = service.response_for("/lastscan")

    assert "Candle Craft Latest Scan" in response.text
    assert "Top Valid Setups" in response.text
    assert "VALIDUSDT | side long | grade A | score 88" in response.text
    assert "{" not in response.text
    assert "}" not in response.text


def test_lastscan_uses_near_misses_when_no_valid_setups(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_near_row()])

    response = service.response_for("/lastscan")

    assert "Top Near Misses" in response.text
    assert "NEARUSDT" in response.text


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

    assert "MISSINGUSDT | side N/A | grade N/A | score N/A | failed_stage rr" in response.text
    assert "next N/A | lifecycle N/A" in response.text
    assert "NEARUSDT | side short | grade B | score 62 | failed_stage final" in response.text


def test_blocked_returns_target_integrity_blocked_rows(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_near_row(), _blocked_row()])

    response = service.response_for("/blocked")

    assert "TARGETUSDT | side long" in response.text
    assert "failure RR_BELOW_MINIMUM" in response.text
    assert "reason Clean target path is too compressed." in response.text
    assert "state TRIGGERED | lifecycle STALE_OR_DEGRADED" in response.text


def test_blocked_returns_clean_none_message_when_no_blocked_rows(tmp_path) -> None:
    service = _write_artifacts(tmp_path, rows=[_near_row()])

    response = service.response_for("/blocked")

    assert "No target-integrity blocks in latest scan." in response.text
    assert "TARGETUSDT" not in response.text


def test_non_admin_chat_id_cannot_access_scan_data(tmp_path) -> None:
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

    assert result.delivery_status == "ignored_unauthorized"
    assert transport.send_calls == []
    records = _read_jsonl(audit_path)
    assert records[0]["delivery_status"] == "ignored_unauthorized"
    serialized = json.dumps(records)
    assert "VALIDUSDT" not in serialized
    assert "run-46c" not in serialized
    assert "stranger-chat" not in serialized


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
    assert "Latest run_id: run-46c" in transport.send_calls[0]["message"]


def test_public_and_vip_channel_ids_are_ignored(tmp_path) -> None:
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

    assert result.delivery_status == "ignored_unauthorized"
    assert transport.send_calls == []
    records = _read_jsonl(audit_path)
    assert [record["delivery_status"] for record in records] == ["ignored_unauthorized", "ignored_unauthorized"]
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
