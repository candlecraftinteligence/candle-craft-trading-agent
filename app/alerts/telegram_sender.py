from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.alerts.telegram import DEFAULT_TELEGRAM_TIMEOUT, TELEGRAM_API_BASE_URL, send_telegram_messages
from app.alerts.telegram_routing import (
    TelegramDestination,
    TelegramMessageType,
    can_send_to_destination,
    log_blocked_telegram_route,
    normalize_destination,
    normalize_message_type,
)
from app.core.config import Settings
from app.data.dtos import NA

logger = logging.getLogger(__name__)

PUBLIC_DESTINATION_MISSING_WARNING = "Public Telegram destination missing. Public lifecycle alerts will not be sent."
PUBLIC_DESTINATION_FALLBACK_WARNING = (
    "Public Telegram destination missing. Using TELEGRAM_CHAT_ID as local/manual fallback for public lifecycle alerts."
)


@dataclass(frozen=True)
class TelegramSendResult:
    status: str
    detail: str
    telegram_results: tuple[dict[str, Any], ...] = ()
    error_message: str = NA

    @property
    def sent(self) -> bool:
        return self.status == "sent"


@dataclass(frozen=True)
class PublicWatchlistSendGuard:
    event_key: str
    reservation_id: int
    event_id: int


@dataclass(frozen=True)
class TelegramPublicDestination:
    chat_id: str | None
    source: str
    warning: str = NA


class TelegramSender:
    def __init__(
        self,
        *,
        bot_token: str | None,
        chat_id: str | None,
        signals_enabled: bool,
        dry_run: bool = True,
        local_manual_mode: bool = True,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = TELEGRAM_API_BASE_URL,
        timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
        destination: TelegramDestination | str = TelegramDestination.UNKNOWN,
    ) -> None:
        self._bot_token = _clean_optional(bot_token)
        self._chat_id = _clean_optional(chat_id)
        self._signals_enabled = bool(signals_enabled)
        self._dry_run = bool(dry_run)
        self._local_manual_mode = bool(local_manual_mode)
        self._http_client = http_client
        self._api_base_url = api_base_url
        self._timeout = timeout
        self._destination = normalize_destination(destination)

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = TELEGRAM_API_BASE_URL,
        timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
    ) -> TelegramSender:
        destination = resolve_public_signal_destination(settings)
        if destination.warning != NA and getattr(settings, "telegram_signals_enabled", False):
            logger.warning(destination.warning)
        destination_type = (
            TelegramDestination.SIGNAL_CHANNEL
            if destination.source == "TELEGRAM_PUBLIC_CHANNEL_ID"
            else TelegramDestination.PUBLIC_CHAT
            if destination.source in {"TELEGRAM_PUBLIC_CHAT_ID", "TELEGRAM_CHAT_ID"}
            else TelegramDestination.UNKNOWN
        )
        return cls(
            bot_token=settings.telegram_bot_token,
            chat_id=destination.chat_id,
            signals_enabled=settings.telegram_signals_enabled,
            dry_run=settings.telegram_dry_run,
            local_manual_mode=settings.local_manual_mode,
            http_client=http_client,
            api_base_url=api_base_url,
            timeout=timeout,
            destination=destination_type,
        )

    async def send_text(
        self,
        text: str,
        *,
        message_type: TelegramMessageType | str = TelegramMessageType.UNKNOWN,
        public_watchlist_guard: PublicWatchlistSendGuard | None = None,
    ) -> TelegramSendResult:
        if not self._local_manual_mode:
            logger.warning("Telegram signal delivery skipped because LOCAL_MANUAL_MODE is false.")
            return TelegramSendResult(
                status="skipped",
                detail="Telegram signal delivery skipped because LOCAL_MANUAL_MODE is false.",
                error_message="local_manual_mode_disabled",
            )
        if not self._signals_enabled:
            logger.info("Telegram signal formatted but sending is disabled.")
            return TelegramSendResult(
                status="skipped",
                detail="Telegram sending is disabled by TELEGRAM_SIGNALS_ENABLED=false.",
                error_message="telegram_sending_disabled",
            )
        if self._dry_run:
            logger.info("Telegram signal delivery skipped because TELEGRAM_DRY_RUN is true.")
            return TelegramSendResult(
                status="skipped",
                detail="Telegram dry-run is enabled; no Telegram message was sent.",
                error_message="telegram_dry_run_enabled",
            )
        if not self._bot_token or not self._chat_id:
            logger.warning("Telegram signal delivery skipped because credentials are missing.")
            return TelegramSendResult(
                status="skipped",
                detail="Telegram credentials are missing; no Telegram message was sent.",
                error_message="missing_telegram_credentials",
            )

        normalized_message_type = normalize_message_type(message_type)
        if (
            normalized_message_type == TelegramMessageType.PUBLIC_WATCHLIST
            and not _valid_public_watchlist_guard(public_watchlist_guard)
        ):
            logger.warning("Public watchlist Telegram send blocked because reservation metadata is missing.")
            return TelegramSendResult(
                status="skipped",
                detail="Public WATCHLIST Telegram send requires a public_alert_events reservation.",
                error_message="public_watchlist_missing_required_reservation",
            )

        route_decision = can_send_to_destination(self._destination, normalized_message_type)
        if not route_decision.allowed:
            log_blocked_telegram_route(route_decision)
            return TelegramSendResult(
                status="skipped",
                detail="Telegram message blocked by signal-channel routing guard.",
                error_message=route_decision.reason,
            )

        results = await send_telegram_messages(
            bot_token=self._bot_token,
            chat_id=self._chat_id,
            message=text,
            http_client=self._http_client,
            api_base_url=self._api_base_url,
            timeout=self._timeout,
        )
        sent = bool(results) and all(result.get("status") == "sent" for result in results)
        error = _first_error(results)
        return TelegramSendResult(
            status="sent" if sent else "failed",
            detail="Telegram signal sent." if sent else "Telegram signal delivery failed.",
            telegram_results=tuple(dict(result) for result in results),
            error_message=error,
        )

    def send_message(
        self,
        text: str,
        *,
        message_type: TelegramMessageType | str = TelegramMessageType.UNKNOWN,
    ) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.send_text(text, message_type=message_type)).sent
        raise RuntimeError("send_message cannot be called from a running event loop; use send_text instead.")


def _valid_public_watchlist_guard(guard: PublicWatchlistSendGuard | None) -> bool:
    if guard is None:
        return False
    return bool(str(guard.event_key).strip()) and guard.reservation_id > 0 and guard.event_id > 0


def resolve_public_signal_destination(settings: Settings) -> TelegramPublicDestination:
    public_chat_id = _clean_optional(getattr(settings, "telegram_public_chat_id", None))
    if public_chat_id:
        return TelegramPublicDestination(chat_id=public_chat_id, source="TELEGRAM_PUBLIC_CHAT_ID")

    public_channel_id = _clean_optional(getattr(settings, "telegram_public_channel_id", None))
    if public_channel_id:
        return TelegramPublicDestination(chat_id=public_channel_id, source="TELEGRAM_PUBLIC_CHANNEL_ID")

    legacy_chat_id = _clean_optional(getattr(settings, "telegram_chat_id", None))
    if legacy_chat_id and bool(getattr(settings, "local_manual_mode", True)):
        return TelegramPublicDestination(
            chat_id=legacy_chat_id,
            source="TELEGRAM_CHAT_ID",
            warning=PUBLIC_DESTINATION_FALLBACK_WARNING,
        )

    return TelegramPublicDestination(chat_id=None, source="missing", warning=PUBLIC_DESTINATION_MISSING_WARNING)


def _clean_optional(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text if text else None


def _first_error(results: tuple[dict[str, Any], ...]) -> str:
    for result in results:
        error = result.get("error")
        if error:
            return str(error)
    return NA


__all__ = [
    "PUBLIC_DESTINATION_FALLBACK_WARNING",
    "PUBLIC_DESTINATION_MISSING_WARNING",
    "PublicWatchlistSendGuard",
    "TelegramPublicDestination",
    "TelegramSendResult",
    "TelegramSender",
    "resolve_public_signal_destination",
]
