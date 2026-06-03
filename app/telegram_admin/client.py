from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

from app.alerts.telegram import DEFAULT_TELEGRAM_TIMEOUT, TELEGRAM_API_BASE_URL, send_telegram_messages
from app.data.dtos import NA

TelegramAdminDeliveryStatus = Literal[
    "dry_run",
    "sent_admin",
    "skipped_disabled",
    "skipped_missing_credentials",
    "failed",
]

SAFE_TELEGRAM_RESULT_KEYS = (
    "status",
    "part_number",
    "total_parts",
    "http_status",
    "rate_limited",
    "error",
    "message_id",
    "chat_id",
    "sent_at",
)


@dataclass(frozen=True)
class TelegramAdminConfig:
    admin_enabled: bool = False
    commands_enabled: bool | None = None
    admin_reports_enabled: bool | None = None
    dry_run: bool = True
    bot_token: str | None = None
    admin_chat_id: str | None = None
    public_channel_id: str | None = None
    vip_channel_id: str | None = None
    public_logo_url: str | None = None
    x_url: str | None = None
    telegram_url: str | None = None
    donate_url: str | None = None
    timeout: float = DEFAULT_TELEGRAM_TIMEOUT

    @classmethod
    def from_settings(cls, settings: Any) -> TelegramAdminConfig:
        legacy_admin_enabled = bool(getattr(settings, "telegram_admin_enabled", False))
        return cls(
            admin_enabled=legacy_admin_enabled,
            commands_enabled=_enabled_with_legacy_fallback(
                getattr(settings, "telegram_commands_enabled", None),
                legacy_admin_enabled,
            ),
            admin_reports_enabled=_enabled_with_legacy_fallback(
                getattr(settings, "telegram_admin_reports_enabled", None),
                legacy_admin_enabled,
            ),
            dry_run=bool(getattr(settings, "telegram_dry_run", True)),
            bot_token=_clean_optional(getattr(settings, "telegram_bot_token", None)),
            admin_chat_id=_clean_optional(getattr(settings, "telegram_admin_chat_id", None)),
            public_channel_id=_clean_optional(getattr(settings, "telegram_public_channel_id", None)),
            vip_channel_id=_clean_optional(getattr(settings, "telegram_vip_channel_id", None)),
            public_logo_url=_clean_optional(getattr(settings, "candle_craft_public_logo_url", None)),
            x_url=_clean_optional(getattr(settings, "candle_craft_x_url", None)),
            telegram_url=_clean_optional(getattr(settings, "candle_craft_telegram_url", None)),
            donate_url=_clean_optional(getattr(settings, "candle_craft_donate_url", None)),
        )

    @property
    def has_admin_credentials(self) -> bool:
        return bool(self.bot_token and self.admin_chat_id)

    @property
    def command_ui_enabled(self) -> bool:
        return self.admin_enabled if self.commands_enabled is None else bool(self.commands_enabled)

    @property
    def admin_report_enabled(self) -> bool:
        return self.admin_enabled if self.admin_reports_enabled is None else bool(self.admin_reports_enabled)


@dataclass(frozen=True)
class TelegramAdminDelivery:
    status: TelegramAdminDeliveryStatus
    detail: str
    error_message: str = NA
    telegram_results: tuple[dict[str, Any], ...] = ()

    @property
    def warning(self) -> str:
        if self.status in {"failed", "skipped_missing_credentials"}:
            return self.detail
        return NA


class TelegramAdminTransport(Protocol):
    async def send_message(self, *, bot_token: str, chat_id: str, message: str) -> tuple[Mapping[str, Any], ...]:
        """Send one admin report message through Telegram."""


class HttpxTelegramAdminTransport:
    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = TELEGRAM_API_BASE_URL,
        timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
    ) -> None:
        self._http_client = http_client
        self._api_base_url = api_base_url
        self._timeout = timeout

    async def send_message(self, *, bot_token: str, chat_id: str, message: str) -> tuple[Mapping[str, Any], ...]:
        return await send_telegram_messages(
            bot_token=bot_token,
            chat_id=chat_id,
            message=message,
            http_client=self._http_client,
            api_base_url=self._api_base_url,
            timeout=self._timeout,
        )


class TelegramAdminClient:
    """Admin-only Telegram sender for scan reports.

    This client never targets public or VIP channel IDs. It sends one compact
    report to the configured admin chat when explicitly enabled and not in
    dry-run mode.
    """

    def __init__(
        self,
        config: TelegramAdminConfig,
        *,
        transport: TelegramAdminTransport | None = None,
    ) -> None:
        self._config = config
        self._transport = transport or HttpxTelegramAdminTransport(timeout=config.timeout)

    async def send_admin_report(self, message: str) -> TelegramAdminDelivery:
        if not self._config.admin_report_enabled:
            return TelegramAdminDelivery(
                status="skipped_disabled",
                detail="Telegram admin scan reports are disabled; local draft artifact persisted.",
            )
        if self._config.dry_run:
            return TelegramAdminDelivery(
                status="dry_run",
                detail="Telegram admin dry-run: no network send attempted.",
            )
        if not self._config.has_admin_credentials:
            return TelegramAdminDelivery(
                status="skipped_missing_credentials",
                detail=(
                    "Missing TELEGRAM_BOT_TOKEN or TELEGRAM_ADMIN_CHAT_ID for Telegram admin report; "
                    "local draft artifact persisted instead."
                ),
                error_message="missing_telegram_admin_credentials",
            )

        try:
            raw_results = await self._transport.send_message(
                bot_token=self._config.bot_token or "",
                chat_id=self._config.admin_chat_id or "",
                message=message,
            )
        except Exception as exc:
            return TelegramAdminDelivery(
                status="failed",
                detail="Telegram admin report failed safely; scanner persistence remains intact.",
                error_message=_sanitize_error(exc, self._config),
            )

        sanitized_results = tuple(_sanitize_result(result, self._config) for result in raw_results)
        sent = bool(sanitized_results) and all(result.get("status") == "sent" for result in sanitized_results)
        if sent:
            return TelegramAdminDelivery(
                status="sent_admin",
                detail="Telegram admin report sent to admin chat.",
                telegram_results=sanitized_results,
            )

        error = _first_error(sanitized_results)
        return TelegramAdminDelivery(
            status="failed",
            detail="Telegram admin report failed safely; scanner persistence remains intact.",
            error_message=error,
            telegram_results=sanitized_results,
        )


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _enabled_with_legacy_fallback(value: Any, fallback: bool) -> bool:
    if value is None:
        return fallback
    return bool(value)


def _sanitize_result(result: Mapping[str, Any], config: TelegramAdminConfig) -> dict[str, Any]:
    sanitized = {key: result[key] for key in SAFE_TELEGRAM_RESULT_KEYS if key in result}
    if "error" in sanitized:
        sanitized["error"] = _sanitize_error(sanitized.get("error"), config)
    return sanitized


def _sanitize_error(value: Any, config: TelegramAdminConfig) -> str:
    text = str(value or "").strip()
    if not text:
        return NA
    for secret in (config.bot_token, config.admin_chat_id, config.public_channel_id, config.vip_channel_id):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def _first_error(results: tuple[dict[str, Any], ...]) -> str:
    for result in results:
        error = str(result.get("error") or "").strip()
        if error:
            return error
    return "telegram_admin_delivery_failed"


ADMIN_TELEGRAM_DELIVERY_STATUSES = (
    "dry_run",
    "sent_admin",
    "skipped_disabled",
    "skipped_missing_credentials",
    "failed",
)


__all__ = [
    "ADMIN_TELEGRAM_DELIVERY_STATUSES",
    "HttpxTelegramAdminTransport",
    "TelegramAdminClient",
    "TelegramAdminConfig",
    "TelegramAdminDelivery",
    "TelegramAdminDeliveryStatus",
    "TelegramAdminTransport",
]
