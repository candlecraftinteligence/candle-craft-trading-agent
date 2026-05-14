from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from app.alerts.templates import TELEGRAM_MAX_MESSAGE_LENGTH, split_message

TELEGRAM_API_BASE_URL = "https://api.telegram.org"
DEFAULT_TELEGRAM_TIMEOUT = 10.0


async def send_telegram_messages(
    *,
    bot_token: str,
    chat_id: str,
    message: str,
    http_client: httpx.AsyncClient | None = None,
    api_base_url: str = TELEGRAM_API_BASE_URL,
    timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
    max_message_length: int = TELEGRAM_MAX_MESSAGE_LENGTH,
) -> tuple[dict[str, Any], ...]:
    """Send a plain-text Telegram alert, splitting oversized messages safely."""

    chunks = split_message(message, max_message_length)
    close_client = http_client is None
    client = http_client or httpx.AsyncClient(base_url=api_base_url, timeout=timeout)
    url = f"/bot{bot_token}/sendMessage"
    results: list[dict[str, Any]] = []

    try:
        for part_number, chunk in enumerate(chunks, start=1):
            payload = {
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            try:
                response = await client.post(url, json=payload)
            except httpx.TimeoutException:
                results.append(
                    _failure(
                        part_number=part_number,
                        total_parts=len(chunks),
                        error="Telegram request timed out.",
                    )
                )
                break
            except httpx.HTTPError as exc:
                results.append(
                    _failure(
                        part_number=part_number,
                        total_parts=len(chunks),
                        error=f"Telegram request failed: {exc}",
                    )
                )
                break

            if response.status_code != 200:
                results.append(
                    _failure(
                        part_number=part_number,
                        total_parts=len(chunks),
                        error=_http_error(response),
                        http_status=response.status_code,
                        rate_limited=response.status_code == 429,
                    )
                )
                break

            try:
                body = response.json()
            except ValueError:
                results.append(
                    _failure(
                        part_number=part_number,
                        total_parts=len(chunks),
                        error="Malformed Telegram response.",
                        http_status=response.status_code,
                    )
                )
                break

            if not isinstance(body, Mapping) or body.get("ok") is not True:
                results.append(
                    _failure(
                        part_number=part_number,
                        total_parts=len(chunks),
                        error=_malformed_body_error(body),
                        http_status=response.status_code,
                    )
                )
                break

            results.append(
                {
                    "status": "sent",
                    "part_number": part_number,
                    "total_parts": len(chunks),
                    "http_status": response.status_code,
                    "rate_limited": False,
                    "error": None,
                }
            )
    finally:
        if close_client:
            await client.aclose()

    return tuple(results)


def _failure(
    *,
    part_number: int,
    total_parts: int,
    error: str,
    http_status: int | None = None,
    rate_limited: bool = False,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "part_number": part_number,
        "total_parts": total_parts,
        "http_status": http_status,
        "rate_limited": rate_limited,
        "error": error,
    }


def _http_error(response: httpx.Response) -> str:
    if response.status_code == 429:
        return "Telegram rate limited the alert request."
    return f"Telegram returned HTTP {response.status_code}."


def _malformed_body_error(body: Any) -> str:
    if isinstance(body, Mapping):
        description = body.get("description")
        if isinstance(description, str) and description.strip():
            return f"Telegram response did not confirm success: {description.strip()}"
    return "Malformed Telegram response."


__all__ = [
    "DEFAULT_TELEGRAM_TIMEOUT",
    "TELEGRAM_API_BASE_URL",
    "send_telegram_messages",
]
