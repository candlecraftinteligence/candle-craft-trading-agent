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
    assert settings.telegram_public_signal_policy == "lifecycle"
    assert settings.telegram_signal_channel_invite_link is None
    assert settings.telegram_public_watchlist_enabled is False
    assert settings.public_watchlist_min_grade == "A"
    assert settings.public_watchlist_min_score == 88
    assert str(settings.public_watchlist_min_rr) == "3.0"
    assert settings.public_watchlist_max_per_scan == 1
    assert settings.public_watchlist_max_per_24h == 15
    assert settings.public_watchlist_max_per_60m == 3
    assert settings.public_watchlist_cooldown_hours == 2
    assert settings.public_watchlist_dedupe_across_modes is True
    assert settings.public_watchlist_require_plan is True
    assert settings.public_watchlist_require_entry_zone is True
    assert settings.public_watchlist_require_invalidation is True
    assert settings.telegram_research_watch_enabled is False
    assert settings.telegram_research_watch_to_public is False
    assert settings.telegram_research_min_quality == 60
    assert settings.telegram_research_min_readiness == 50
    assert settings.telegram_research_alert_cooldown_minutes == 1440
    assert settings.telegram_research_max_per_scan == 5
    assert settings.telegram_wolf_briefing_enabled is False
    assert settings.telegram_wolf_briefing_public_enabled is False
    assert settings.telegram_wolf_briefing_channel_publish_enabled is False
    assert settings.telegram_wolf_briefing_channel_id is None
    assert settings.candle_craft_donate_usdt_ton_address is None
    assert settings.candle_craft_donate_ton_address is None
    assert settings.candle_craft_donate_btc_address is None
    assert settings.candle_craft_donate_url is None
    assert settings.global_context_enabled is True
    assert settings.btc_context_enabled is True
    assert settings.btc_d_context_enabled is True
    assert settings.btc_d_cache_ttl_sec == 300
    assert settings.btc_d_request_timeout_sec == 5.0
    assert settings.local_manual_mode is True
    assert settings.order_execution_enabled is False


def test_log_level_is_normalized() -> None:
    settings = Settings(_env_file=None, log_level="warning")

    assert settings.log_level == "WARNING"


@pytest.mark.parametrize(("raw_value", "expected"), [("true", True), ("false", False)])
def test_telegram_dry_run_loads_from_canonical_environment_setting(
    monkeypatch, raw_value: str, expected: bool
) -> None:
    monkeypatch.setenv("TELEGRAM_DRY_RUN", raw_value)

    settings = Settings(_env_file=None)

    assert settings.telegram_dry_run is expected


def test_order_execution_enabled_fails_safely() -> None:
    with pytest.raises(ValueError, match="ORDER_EXECUTION_ENABLED must remain false"):
        Settings(_env_file=None, order_execution_enabled=True)


def test_unsupported_public_signal_policy_fails_safely() -> None:
    with pytest.raises(ValueError, match="lifecycle"):
        Settings(_env_file=None, telegram_public_signal_policy="confirmed_updates")


def test_legacy_setup_only_policy_maps_to_lifecycle() -> None:
    settings = Settings(_env_file=None, telegram_public_signal_policy="setup_only")

    assert settings.telegram_public_signal_policy == "lifecycle"


def test_signal_channel_invite_link_loads_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_SIGNAL_CHANNEL_INVITE_LINK", "https://t.me/+test-private-invite")

    settings = Settings(_env_file=None)

    assert settings.telegram_signal_channel_invite_link == "https://t.me/+test-private-invite"


def test_missing_signal_channel_invite_link_does_not_crash(monkeypatch) -> None:
    monkeypatch.delenv("TELEGRAM_SIGNAL_CHANNEL_INVITE_LINK", raising=False)

    settings = Settings(_env_file=None)

    assert settings.telegram_signal_channel_invite_link is None


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


def test_env_example_contains_signal_channel_invite_placeholder_only() -> None:
    env_example = Path(__file__).resolve().parents[1] / ".env.example"
    values: dict[str, str] = {}
    for line in env_example.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key] = value

    assert values["TELEGRAM_SIGNAL_CHANNEL_INVITE_LINK"] == (
        "https://t.me/+replace-with-your-private-invite-link"
    )


def test_global_context_settings_load_without_secrets(monkeypatch) -> None:
    monkeypatch.setenv("GLOBAL_CONTEXT_ENABLED", "false")
    monkeypatch.setenv("BTC_CONTEXT_ENABLED", "false")
    monkeypatch.setenv("BTC_D_CONTEXT_ENABLED", "false")
    monkeypatch.setenv("BTC_D_CACHE_TTL_SEC", "90")
    monkeypatch.setenv("BTC_D_REQUEST_TIMEOUT_SEC", "2.5")

    settings = Settings(_env_file=None)

    assert settings.global_context_enabled is False
    assert settings.btc_context_enabled is False
    assert settings.btc_d_context_enabled is False
    assert settings.btc_d_cache_ttl_sec == 90
    assert settings.btc_d_request_timeout_sec == 2.5
