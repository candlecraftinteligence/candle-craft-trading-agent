from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import httpx

from app.alerts.telegram import DEFAULT_TELEGRAM_TIMEOUT, TELEGRAM_API_BASE_URL, send_telegram_messages
from app.core.config import Settings
from app.data.dtos import NA

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TelegramSendResult:
    status: str
    detail: str
    telegram_results: tuple[dict[str, Any], ...] = ()
    error_message: str = NA

    @property
    def sent(self) -> bool:
        return self.status == "sent"


class TelegramSender:
    def __init__(
        self,
        *,
        bot_token: str | None,
        chat_id: str | None,
        signals_enabled: bool,
        local_manual_mode: bool = True,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = TELEGRAM_API_BASE_URL,
        timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
    ) -> None:
        self._bot_token = _clean_optional(bot_token)
        self._chat_id = _clean_optional(chat_id)
        self._signals_enabled = bool(signals_enabled)
        self._local_manual_mode = bool(local_manual_mode)
        self._http_client = http_client
        self._api_base_url = api_base_url
        self._timeout = timeout

    @classmethod
    def from_settings(
        cls,
        settings: Settings,
        *,
        http_client: httpx.AsyncClient | None = None,
        api_base_url: str = TELEGRAM_API_BASE_URL,
        timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
    ) -> TelegramSender:
        return cls(
            bot_token=settings.telegram_bot_token,
            chat_id=settings.telegram_chat_id,
            signals_enabled=settings.telegram_signals_enabled,
            local_manual_mode=settings.local_manual_mode,
            http_client=http_client,
            api_base_url=api_base_url,
            timeout=timeout,
        )

    async def send_text(self, text: str) -> TelegramSendResult:
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
        if not self._bot_token or not self._chat_id:
            logger.warning("Telegram signal delivery skipped because credentials are missing.")
            return TelegramSendResult(
                status="skipped",
                detail="Telegram credentials are missing; no Telegram message was sent.",
                error_message="missing_telegram_credentials",
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

    def send_message(self, text: str) -> bool:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.send_text(text)).sent
        raise RuntimeError("send_message cannot be called from a running event loop; use send_text instead.")


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
    "TelegramSendResult",
    "TelegramSender",
]
