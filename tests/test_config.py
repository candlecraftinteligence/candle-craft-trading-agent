from __future__ import annotations

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
    assert settings.local_manual_mode is True
    assert settings.order_execution_enabled is False


def test_log_level_is_normalized() -> None:
    settings = Settings(_env_file=None, log_level="warning")

    assert settings.log_level == "WARNING"


def test_order_execution_enabled_fails_safely() -> None:
    with pytest.raises(ValueError, match="ORDER_EXECUTION_ENABLED must remain false"):
        Settings(_env_file=None, order_execution_enabled=True)
