from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
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
    results: list[dict[str, Any]] = []
    for part_number, chunk in enumerate(chunks, start=1):
        result = await send_telegram_message_part(
            bot_token=bot_token,
            chat_id=chat_id,
            message=chunk,
            part_number=part_number,
            total_parts=len(chunks),
            http_client=http_client,
            api_base_url=api_base_url,
            timeout=timeout,
        )
        results.append(result)
        if result.get("delivery_state") != "SENT":
            break
    return tuple(results)


async def send_telegram_message_part(
    *,
    bot_token: str,
    chat_id: str,
    message: str,
    part_number: int = 1,
    total_parts: int = 1,
    http_client: httpx.AsyncClient | None = None,
    api_base_url: str = TELEGRAM_API_BASE_URL,
    timeout: float = DEFAULT_TELEGRAM_TIMEOUT,
) -> dict[str, Any]:
    """Send one durable outbox part and classify whether non-acceptance is proven."""

    close_client = http_client is None
    client = http_client or httpx.AsyncClient(base_url=api_base_url, timeout=timeout)
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
    try:
        try:
            response = await client.post(f"/bot{bot_token}/sendMessage", json=payload)
        except (httpx.ConnectTimeout, httpx.ConnectError, httpx.PoolTimeout):
            return _failure(
                part_number=part_number,
                total_parts=total_parts,
                error="Telegram connection failed before request transmission.",
                delivery_state="RETRYABLE",
                error_category="connect_failure_before_transmission",
            )
        except (httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.WriteError):
            return _failure(
                part_number=part_number,
                total_parts=total_parts,
                error="Telegram request timed out or failed after transmission; outcome is uncertain.",
                delivery_state="UNCERTAIN",
                error_category="transport_outcome_uncertain",
            )
        except httpx.TimeoutException:
            return _failure(
                part_number=part_number,
                total_parts=total_parts,
                error="Telegram request timed out with uncertain acceptance.",
                delivery_state="UNCERTAIN",
                error_category="transport_timeout_uncertain",
            )
        except httpx.HTTPError:
            return _failure(
                part_number=part_number,
                total_parts=total_parts,
                error="Telegram request failed after transmission; outcome is uncertain.",
                delivery_state="UNCERTAIN",
                error_category="transport_http_error_uncertain",
            )

        body = _response_body(response)
        if response.status_code != 200:
            state, category = _rejection_classification(response.status_code)
            return _failure(
                part_number=part_number,
                total_parts=total_parts,
                error=_http_error(response),
                delivery_state=state,
                error_category=category,
                http_status=response.status_code,
                rate_limited=response.status_code == 429,
                retry_after=_retry_after(response, body),
            )

        if body is None:
            return _failure(
                part_number=part_number,
                total_parts=total_parts,
                error="Malformed Telegram success response; acceptance is uncertain.",
                delivery_state="UNCERTAIN",
                error_category="malformed_success_response",
                http_status=response.status_code,
            )
        if body.get("ok") is not True:
            error_code = _int_or_none(body.get("error_code"))
            state, category = _rejection_classification(error_code or 400)
            return _failure(
                part_number=part_number,
                total_parts=total_parts,
                error=_malformed_body_error(body),
                delivery_state=state,
                error_category=f"telegram_api_rejection:{category}",
                http_status=error_code or response.status_code,
                rate_limited=error_code == 429,
                retry_after=_retry_after(response, body),
            )

        metadata = _success_metadata(body)
        if "message_id" not in metadata:
            return _failure(
                part_number=part_number,
                total_parts=total_parts,
                error="Telegram success response omitted the message ID; acceptance is uncertain.",
                delivery_state="UNCERTAIN",
                error_category="incomplete_success_response",
                http_status=response.status_code,
            )
        return {
            "status": "sent",
            "delivery_state": "SENT",
            "part_number": part_number,
            "total_parts": total_parts,
            "http_status": response.status_code,
            "rate_limited": False,
            "retry_after": None,
            "error_category": None,
            "error": None,
            **metadata,
        }
    finally:
        if close_client:
            await client.aclose()


def _failure(
    *,
    part_number: int,
    total_parts: int,
    error: str,
    delivery_state: str,
    error_category: str,
    http_status: int | None = None,
    rate_limited: bool = False,
    retry_after: float | None = None,
) -> dict[str, Any]:
    return {
        "status": "failed",
        "delivery_state": delivery_state,
        "part_number": part_number,
        "total_parts": total_parts,
        "http_status": http_status,
        "rate_limited": rate_limited,
        "retry_after": retry_after,
        "error_category": error_category,
        "error": error,
    }


def _response_body(response: httpx.Response) -> Mapping[str, Any] | None:
    try:
        body = response.json()
    except ValueError:
        return None
    return body if isinstance(body, Mapping) else None


def _rejection_classification(status_code: int) -> tuple[str, str]:
    if status_code == 429:
        return "RETRYABLE", "telegram_rate_limited"
    if 500 <= status_code <= 599:
        return "RETRYABLE", "telegram_server_rejection"
    return "FAILED_FINAL", "telegram_permanent_rejection"


def _retry_after(response: httpx.Response, body: Mapping[str, Any] | None) -> float | None:
    if body is not None:
        parameters = body.get("parameters")
        if isinstance(parameters, Mapping):
            parsed = _float_or_none(parameters.get("retry_after"))
            if parsed is not None:
                return parsed
    return _float_or_none(response.headers.get("Retry-After"))


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


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


def _success_metadata(body: Mapping[str, Any]) -> dict[str, Any]:
    result = body.get("result")
    if not isinstance(result, Mapping):
        return {}

    metadata: dict[str, Any] = {}
    message_id = result.get("message_id")
    if message_id is not None:
        metadata["message_id"] = message_id

    chat = result.get("chat")
    if isinstance(chat, Mapping):
        chat_id = chat.get("id")
        if chat_id is not None:
            metadata["chat_id"] = chat_id

    sent_at = _telegram_sent_at(result.get("date"))
    if sent_at is not None:
        metadata["sent_at"] = sent_at

    return metadata


def _telegram_sent_at(value: Any) -> str | None:
    try:
        timestamp = int(value)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, tz=UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_TELEGRAM_TIMEOUT",
    "TELEGRAM_API_BASE_URL",
    "send_telegram_message_part",
    "send_telegram_messages",
]
