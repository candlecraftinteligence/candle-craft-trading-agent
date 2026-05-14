from __future__ import annotations

from app.core.config import Settings


def test_default_settings_are_safe() -> None:
    settings = Settings(_env_file=None)

    assert settings.app_name == "Candle Craft Trading Agent"
    assert settings.environment == "local"
    assert settings.log_level == "INFO"
    assert settings.database_url.startswith("postgresql+psycopg://")


def test_log_level_is_normalized() -> None:
    settings = Settings(_env_file=None, log_level="warning")

    assert settings.log_level == "WARNING"
