from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from app.alerts.telegram import DEFAULT_TELEGRAM_TIMEOUT, TELEGRAM_API_BASE_URL
from app.data.dtos import NA
from app.telegram_admin.client import TelegramAdminConfig


@dataclass(frozen=True)
class TelegramNativeCommandMenuCleanupResult:
    status: str
    delete_commands_status: str = NA
    menu_button_status: str = NA
    error_message: str = NA

    @property
    def failed(self) -> bool:
        return self.status == "failed"


async def clear_telegram_native_command_menu(
    *,
    config: TelegramAdminConfig,
    http_client: httpx.AsyncClient | None = None,
    api_base_url: str = TELEGRAM_API_BASE_URL,
    timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
    dry_run: bool = False,
) -> TelegramNativeCommandMenuCleanupResult:
    """Clear Telegram's slash-command menu while leaving chat controls untouched."""

    if not config.bot_token:
        return TelegramNativeCommandMenuCleanupResult(status="skipped_missing_credentials")
    if dry_run:
        return TelegramNativeCommandMenuCleanupResult(status="dry_run")

    close_client = http_client is None
    client = http_client or httpx.AsyncClient(base_url=api_base_url, timeout=timeout)
    try:
        delete_status, delete_error = await _post_bot_api_method(
            client,
            bot_token=config.bot_token,
            method="deleteMyCommands",
            payload={},
            config=config,
        )
        if delete_status != "cleared":
            return TelegramNativeCommandMenuCleanupResult(
                status="failed",
                delete_commands_status=delete_status,
                menu_button_status=NA,
                error_message=delete_error,
            )

        menu_status, menu_error = await _post_bot_api_method(
            client,
            bot_token=config.bot_token,
            method="setChatMenuButton",
            payload={"menu_button": {"type": "default"}},
            config=config,
        )
        if menu_status != "cleared":
            return TelegramNativeCommandMenuCleanupResult(
                status="failed",
                delete_commands_status=delete_status,
                menu_button_status=menu_status,
                error_message=menu_error,
            )
    finally:
        if close_client:
            await client.aclose()

    return TelegramNativeCommandMenuCleanupResult(
        status="cleared",
        delete_commands_status="cleared",
        menu_button_status="default",
    )


async def _post_bot_api_method(
    client: httpx.AsyncClient,
    *,
    bot_token: str,
    method: str,
    payload: dict[str, Any],
    config: TelegramAdminConfig,
) -> tuple[str, str]:
    try:
        response = await client.post(f"/bot{bot_token}/{method}", json=payload)
    except httpx.TimeoutException:
        return "failed", "Telegram command menu cleanup request timed out."
    except httpx.HTTPError as exc:
        return "failed", _sanitize_error(exc, config)

    if response.status_code != 200:
        return "failed", f"Telegram returned HTTP {response.status_code}."

    try:
        body = response.json()
    except ValueError:
        return "failed", "Telegram command menu cleanup response could not be read."

    if not isinstance(body, dict) or body.get("ok") is not True:
        description = body.get("description") if isinstance(body, dict) else None
        detail = str(description).strip() if description else "Telegram did not confirm command menu cleanup."
        return "failed", _sanitize_error(detail, config)

    return "cleared", NA


def _sanitize_error(value: Any, config: TelegramAdminConfig) -> str:
    text = str(value or "").strip()
    if not text:
        return NA
    for secret in (
        config.bot_token,
        config.admin_chat_id,
        config.public_chat_id,
        config.public_channel_id,
        config.vip_channel_id,
    ):
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    return text


__all__ = [
    "TelegramNativeCommandMenuCleanupResult",
    "clear_telegram_native_command_menu",
]
