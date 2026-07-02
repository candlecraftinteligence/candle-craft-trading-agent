from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

import httpx

from .config import telegram_chat_id as configured_telegram_chat_id
from .config import telegram_token
from .models import TelegramSendResult

logger = logging.getLogger(__name__)

TELEGRAM_API_BASE_URL = "https://api.telegram.org"
DEFAULT_TELEGRAM_TIMEOUT = 10.0


class TelegramXHypeSender:
    def __init__(
        self,
        *,
        bot_token: str | None = None,
        chat_id: str | None = None,
        api_base_url: str = TELEGRAM_API_BASE_URL,
        timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
        client: httpx.Client | None = None,
        max_retries: int = 2,
        retry_delay_sec: float = 0.5,
        disable_web_page_preview: bool = False,
    ) -> None:
        self._bot_token = _clean(bot_token) or telegram_token()
        self._chat_id = _clean(chat_id) or configured_telegram_chat_id()
        self._api_base_url = api_base_url.rstrip("/")
        self._timeout = timeout
        self._client = client
        self._max_retries = max(0, int(max_retries))
        self._retry_delay_sec = max(0.0, float(retry_delay_sec))
        self._disable_web_page_preview = disable_web_page_preview

    def send_message(self, text: str, *, chat_id: str | None = None, dry_run: bool = False) -> TelegramSendResult:
        target_chat_id = _clean(chat_id) or self._chat_id
        if dry_run:
            print(text)
            return TelegramSendResult(status="dry_run", detail="Dry-run mode printed the Telegram message.")

        if not self._bot_token or not target_chat_id:
            logger.error("Telegram X hype send skipped because token or chat id is missing.")
            return TelegramSendResult(
                status="failed",
                detail="Telegram X hype credentials are missing.",
                error="missing_telegram_credentials",
            )
        if not text.strip():
            return TelegramSendResult(status="failed", detail="Telegram message is empty.", error="empty_message")

        close_client = self._client is None
        client = self._client or httpx.Client(base_url=self._api_base_url, timeout=self._timeout)
        try:
            return self._send_with_retries(client, target_chat_id, text)
        finally:
            if close_client:
                client.close()

    def _send_with_retries(self, client: httpx.Client, chat_id: str, text: str) -> TelegramSendResult:
        url = f"/bot{self._bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": self._disable_web_page_preview,
        }
        last_error = "unknown_telegram_error"
        attempts = self._max_retries + 1
        for attempt in range(1, attempts + 1):
            try:
                response = client.post(url, json=payload)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = exc.__class__.__name__
                logger.warning("Transient Telegram X hype send error", extra={"attempt": attempt, "error": last_error})
                if attempt < attempts:
                    time.sleep(self._retry_delay_sec)
                    continue
                return TelegramSendResult(
                    status="failed",
                    detail="Telegram X hype send failed after retryable network errors.",
                    error=last_error,
                )

            if response.status_code in {429, 500, 502, 503, 504} and attempt < attempts:
                last_error = f"http_{response.status_code}"
                logger.warning(
                    "Retryable Telegram X hype response",
                    extra={"attempt": attempt, "http_status": response.status_code},
                )
                time.sleep(self._retry_delay_sec)
                continue

            if response.status_code != 200:
                return TelegramSendResult(
                    status="failed",
                    detail=f"Telegram returned HTTP {response.status_code}.",
                    error=f"http_{response.status_code}",
                )

            try:
                body = response.json()
            except ValueError:
                return TelegramSendResult(
                    status="failed",
                    detail="Telegram returned malformed JSON.",
                    error="malformed_telegram_response",
                )
            if not isinstance(body, Mapping) or body.get("ok") is not True:
                return TelegramSendResult(
                    status="failed",
                    detail=_telegram_failure_detail(body),
                    error="telegram_response_not_ok",
                )
            return TelegramSendResult(
                status="sent",
                detail="Telegram X hype prompt sent.",
                telegram_message_id=_message_id(body),
            )

        return TelegramSendResult(status="failed", detail="Telegram send failed.", error=last_error)


def _message_id(body: Mapping[str, Any]) -> int | None:
    result = body.get("result")
    if not isinstance(result, Mapping):
        return None
    value = result.get("message_id")
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _telegram_failure_detail(body: Any) -> str:
    if isinstance(body, Mapping):
        description = body.get("description")
        if isinstance(description, str) and description.strip():
            return f"Telegram did not confirm success: {description.strip()}"
    return "Telegram did not confirm success."


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text if text else None
