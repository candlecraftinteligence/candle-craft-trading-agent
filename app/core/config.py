from __future__ import annotations

from decimal import Decimal
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
    telegram_public_signal_policy: Literal["lifecycle"] = "lifecycle"
    telegram_public_ui_enabled: bool | None = None
    telegram_watchlist_outcome_tracking_enabled: bool = True
    telegram_public_watchlist_terminal_updates_enabled: bool = False
    telegram_public_watchlist_enabled: bool = False
    public_watchlist_min_grade: str = "A"
    public_watchlist_min_score: int = 88
    public_watchlist_min_rr: Decimal = Decimal("3.0")
    public_watchlist_max_per_scan: int = 1
    public_watchlist_max_per_24h: int = 15
    public_watchlist_max_per_60m: int = 3
    public_watchlist_cooldown_hours: int = 2
    public_watchlist_symbol_whitelist: str = ""
    public_watchlist_dedupe_across_modes: bool = True
    public_watchlist_require_plan: bool = True
    public_watchlist_require_entry_zone: bool = True
    public_watchlist_require_invalidation: bool = True
    telegram_research_watch_enabled: bool = False
    telegram_research_watch_to_public: bool = False
    telegram_research_min_quality: int = 60
    telegram_research_min_readiness: int = 50
    telegram_research_alert_cooldown_minutes: int = 1440
    telegram_research_max_per_scan: int = 5
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
    global_context_enabled: bool = True
    btc_context_enabled: bool = True
    btc_d_context_enabled: bool = True
    btc_d_cache_ttl_sec: int = 300
    btc_d_request_timeout_sec: float = 5.0
    microstructure_flow_enabled: bool = False
    microstructure_flow_stale_sec: float = 5.0
    microstructure_flow_max_symbols: int = 100
    liquidation_flow_enabled: bool = False
    liquidation_flow_stale_sec: float = 30.0
    liquidation_flow_max_symbols: int = 100

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

    @field_validator("telegram_public_signal_policy", mode="before")
    @classmethod
    def normalize_telegram_public_signal_policy(cls, value: str) -> str:
        normalized = str(value).strip().lower().replace("-", "_")
        if normalized == "setup_only":
            return "lifecycle"
        return normalized

    @field_validator("scanner_confirmation_cycles")
    @classmethod
    def validate_scanner_confirmation_cycles(cls, value: int) -> int:
        return max(1, int(value))

    @field_validator("scanner_setup_merge_tolerance_pct")
    @classmethod
    def validate_scanner_setup_merge_tolerance_pct(cls, value: float) -> float:
        return max(0.0, float(value))

    @field_validator("btc_d_cache_ttl_sec")
    @classmethod
    def validate_btc_d_cache_ttl(cls, value: int) -> int:
        return max(0, int(value))

    @field_validator("btc_d_request_timeout_sec")
    @classmethod
    def validate_btc_d_request_timeout(cls, value: float) -> float:
        normalized = float(value)
        if normalized <= 0:
            raise ValueError("BTC_D_REQUEST_TIMEOUT_SEC must be greater than zero")
        return normalized

    @field_validator("microstructure_flow_stale_sec")
    @classmethod
    def validate_microstructure_flow_stale_sec(cls, value: float) -> float:
        normalized = float(value)
        if normalized <= 0:
            raise ValueError("MICROSTRUCTURE_FLOW_STALE_SEC must be greater than zero")
        return normalized

    @field_validator("microstructure_flow_max_symbols")
    @classmethod
    def validate_microstructure_flow_max_symbols(cls, value: int) -> int:
        normalized = int(value)
        if normalized < 1 or normalized > 1024:
            raise ValueError("MICROSTRUCTURE_FLOW_MAX_SYMBOLS must be between 1 and 1024")
        return normalized

    @field_validator("liquidation_flow_stale_sec")
    @classmethod
    def validate_liquidation_flow_stale_sec(cls, value: float) -> float:
        normalized = float(value)
        if normalized <= 0:
            raise ValueError("LIQUIDATION_FLOW_STALE_SEC must be greater than zero")
        return normalized

    @field_validator("liquidation_flow_max_symbols")
    @classmethod
    def validate_liquidation_flow_max_symbols(cls, value: int) -> int:
        normalized = int(value)
        if normalized < 1 or normalized > 1024:
            raise ValueError("LIQUIDATION_FLOW_MAX_SYMBOLS must be between 1 and 1024")
        return normalized

    @field_validator(
        "telegram_research_min_quality",
        "telegram_research_min_readiness",
        "telegram_research_alert_cooldown_minutes",
        "telegram_research_max_per_scan",
        "public_watchlist_min_score",
        "public_watchlist_max_per_scan",
        "public_watchlist_max_per_24h",
        "public_watchlist_max_per_60m",
        "public_watchlist_cooldown_hours",
    )
    @classmethod
    def validate_telegram_research_non_negative(cls, value: int) -> int:
        return max(0, int(value))

    @field_validator("public_watchlist_min_rr")
    @classmethod
    def validate_public_watchlist_min_rr(cls, value: Decimal) -> Decimal:
        return max(Decimal("0"), Decimal(value))

    @field_validator("public_watchlist_min_grade", mode="before")
    @classmethod
    def normalize_public_watchlist_min_grade(cls, value: str) -> str:
        return str(value).strip().upper()

    @model_validator(mode="after")
    def enforce_manual_only_phase(self) -> Settings:
        if self.order_execution_enabled:
            raise ValueError("ORDER_EXECUTION_ENABLED must remain false for manual Telegram signal delivery mode.")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
