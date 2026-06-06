from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "Candle Craft Trading Agent"
    environment: Literal["local", "test", "staging", "production"] = "local"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"
    database_url: str = "postgresql+psycopg://candle@localhost:5432/candle_craft"
    sql_echo: bool = False
    telegram_admin_enabled: bool = False
    telegram_commands_enabled: bool | None = None
    telegram_admin_reports_enabled: bool | None = None
    telegram_dry_run: bool = True
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_admin_chat_id: str | None = None
    telegram_public_chat_id: str | None = None
    telegram_public_channel_id: str | None = None
    telegram_signal_channel_invite_link: str | None = None
    telegram_vip_channel_id: str | None = None
    telegram_signals_enabled: bool = False
    telegram_public_ui_enabled: bool | None = None
    telegram_watchlist_outcome_tracking_enabled: bool = True
    telegram_public_watchlist_terminal_updates_enabled: bool = False
    telegram_wolf_briefing_enabled: bool = False
    telegram_wolf_briefing_public_enabled: bool = False
    telegram_wolf_briefing_channel_publish_enabled: bool = False
    telegram_wolf_briefing_channel_id: str | None = None
    candle_craft_public_logo_path: str | None = None
    candle_craft_public_logo_url: str | None = None
    candle_craft_x_url: str | None = None
    candle_craft_telegram_url: str | None = None
    candle_craft_donate_usdt_ton_address: str | None = None
    candle_craft_donate_ton_address: str | None = None
    candle_craft_donate_btc_address: str | None = None
    candle_craft_donate_url: str | None = None
    local_manual_mode: bool = True
    order_execution_enabled: bool = False
    scanner_confirmation_cycles: int = 2
    scanner_setup_merge_tolerance_pct: float = 0.5

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    @field_validator("log_level", mode="before")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        return str(value).upper()

    @field_validator("scanner_confirmation_cycles")
    @classmethod
    def validate_scanner_confirmation_cycles(cls, value: int) -> int:
        return max(1, int(value))

    @field_validator("scanner_setup_merge_tolerance_pct")
    @classmethod
    def validate_scanner_setup_merge_tolerance_pct(cls, value: float) -> float:
        return max(0.0, float(value))

    @model_validator(mode="after")
    def enforce_manual_only_phase(self) -> Settings:
        if self.order_execution_enabled:
            raise ValueError("ORDER_EXECUTION_ENABLED must remain false for manual Telegram signal delivery mode.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
