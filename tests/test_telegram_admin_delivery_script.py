from __future__ import annotations

from app.core.config import Settings
from scripts import test_telegram_admin_delivery


class FakeAdminTransport:
    def __init__(self, *, status: str = "sent") -> None:
        self.status = status
        self.calls: list[dict[str, str]] = []

    async def send_message(self, *, bot_token: str, chat_id: str, message: str):
        self.calls.append({"bot_token": bot_token, "chat_id": chat_id, "message": message})
        if self.status == "sent":
            return (
                {
                    "status": "sent",
                    "part_number": 1,
                    "total_parts": 1,
                    "message_id": 44,
                    "chat_id": chat_id,
                    "sent_at": "2026-06-01T12:00:00Z",
                },
            )
        return (
            {
                "status": "failed",
                "part_number": 1,
                "total_parts": 1,
                "error": f"failure for {bot_token} {chat_id}",
            },
        )


def _settings(
    *,
    admin_enabled: bool = True,
    admin_reports_enabled: bool | None = None,
    commands_enabled: bool | None = None,
    dry_run: bool = False,
    bot_token: str | None = "secret-token",
    admin_chat_id: str | None = "admin-chat",
) -> Settings:
    return Settings(
        _env_file=None,
        telegram_admin_enabled=admin_enabled,
        telegram_admin_reports_enabled=admin_reports_enabled,
        telegram_commands_enabled=commands_enabled,
        telegram_dry_run=dry_run,
        telegram_bot_token=bot_token,
        telegram_admin_chat_id=admin_chat_id,
    )


def test_smoke_script_defaults_to_dry_run_and_does_not_call_network(capsys) -> None:
    transport = FakeAdminTransport()

    exit_code = test_telegram_admin_delivery.main(
        ["--message", "Candle Craft admin delivery dry-run test"],
        settings=_settings(admin_enabled=True, dry_run=False),
        transport=transport,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert transport.calls == []
    assert "admin_reports_enabled=true" in captured.out
    assert "legacy_admin_enabled=true" in captured.out
    assert "dry_run=true" in captured.out
    assert "delivery_status=dry_run" in captured.out
    assert "secret-token" not in captured.out


def test_smoke_script_skipped_disabled_exits_zero(capsys) -> None:
    transport = FakeAdminTransport()

    exit_code = test_telegram_admin_delivery.main(
        ["--message", "Candle Craft admin delivery test"],
        settings=_settings(admin_enabled=False, dry_run=True, bot_token=None, admin_chat_id=None),
        transport=transport,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert transport.calls == []
    assert "admin_reports_enabled=false" in captured.out
    assert "legacy_admin_enabled=false" in captured.out
    assert "delivery_status=skipped_disabled" in captured.out


def test_smoke_script_force_live_uses_fake_admin_transport(capsys) -> None:
    transport = FakeAdminTransport()

    exit_code = test_telegram_admin_delivery.main(
        ["--message", "Candle Craft admin delivery test", "--force-live"],
        settings=_settings(admin_enabled=True, dry_run=False),
        transport=transport,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert len(transport.calls) == 1
    assert transport.calls[0]["chat_id"] == "admin-chat"
    assert transport.calls[0]["message"] == "Candle Craft admin delivery test"
    assert "dry_run=false" in captured.out
    assert "delivery_status=sent_admin" in captured.out
    assert "secret-token" not in captured.out


def test_smoke_script_force_live_respects_admin_reports_flag(capsys) -> None:
    transport = FakeAdminTransport()

    exit_code = test_telegram_admin_delivery.main(
        ["--message", "Candle Craft admin delivery test", "--force-live"],
        settings=_settings(admin_enabled=True, admin_reports_enabled=False, dry_run=False),
        transport=transport,
    )

    captured = capsys.readouterr()
    assert exit_code == 0
    assert transport.calls == []
    assert "admin_reports_enabled=false" in captured.out
    assert "delivery_status=skipped_disabled" in captured.out
    assert "secret-token" not in captured.out


def test_smoke_script_live_failure_with_credentials_exits_nonzero(capsys) -> None:
    transport = FakeAdminTransport(status="failed")

    exit_code = test_telegram_admin_delivery.main(
        ["--message", "Candle Craft admin delivery test", "--force-live"],
        settings=_settings(admin_enabled=True, dry_run=False),
        transport=transport,
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert len(transport.calls) == 1
    assert "delivery_status=failed" in captured.out
    assert "secret-token" not in captured.out
