from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings


def test_default_settings_are_safe() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "Candle Craft Trading Agent"
    assert settings.environment == "local"
    assert settings.log_level == "INFO"
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.telegram_admin_enabled is False
    assert settings.telegram_commands_enabled is None
    assert settings.telegram_admin_reports_enabled is None
    assert settings.telegram_dry_run is True
    assert settings.telegram_signals_enabled is False
    assert settings.candle_craft_donate_usdt_ton_address is None
    assert settings.candle_craft_donate_ton_address is None
    assert settings.candle_craft_donate_btc_address is None
    assert settings.candle_craft_donate_url is None
    assert settings.local_manual_mode is True
    assert settings.order_execution_enabled is False


def test_log_level_is_normalized() -> None:
    settings = Settings(_env_file=None, log_level="warning")

    assert settings.log_level == "WARNING"


def test_order_execution_enabled_fails_safely() -> None:
    with pytest.raises(ValueError, match="ORDER_EXECUTION_ENABLED must remain false"):
        Settings(_env_file=None, order_execution_enabled=True)


def test_env_example_contains_donation_placeholders_only() -> None:
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    values: dict[str, str] = {}
    for line in env_example.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value

    for key in (
        "CANDLE_CRAFT_DONATE_USDT_TON_ADDRESS",
        "CANDLE_CRAFT_DONATE_TON_ADDRESS",
        "CANDLE_CRAFT_DONATE_BTC_ADDRESS",
        "CANDLE_CRAFT_DONATE_URL",
    ):
        assert values[key] == ""
