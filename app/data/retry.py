from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

from app.data.exceptions import (
    ExchangeClientError,
    ExchangeHTTPError,
    ExchangeNetworkError,
    ExchangeRateLimitError,
    ExchangeTimeoutError,
)

T = TypeVar("T")


def is_retryable_exchange_error(exc: ExchangeClientError) -> bool:
    if isinstance(exc, (ExchangeNetworkError, ExchangeRateLimitError, ExchangeTimeoutError)):
        return True
    return isinstance(exc, ExchangeHTTPError) and exc.status_code >= 500


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    attempts: int,
    base_delay: float,
    max_delay: float,
    logger: logging.Logger,
    operation_name: str,
    on_retry_event: Callable[[dict[str, Any]], None] | None = None,
) -> T:
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    for attempt in range(1, attempts + 1):
        try:
            return await operation()
        except ExchangeClientError as exc:
            if not is_retryable_exchange_error(exc) or attempt == attempts:
                logger.debug("%s failed after attempt %s: %s", operation_name, attempt, exc)
                if on_retry_event is not None:
                    on_retry_event(
                        {
                            "operation": operation_name,
                            "attempt": attempt,
                            "attempts": attempts,
                            "will_retry": False,
                            "delay_seconds": 0,
                            "error": str(exc),
                            "error_type": exc.__class__.__name__,
                        }
                    )
                raise

            retry_after = getattr(exc, "retry_after", None)
            exponential_delay = min(max_delay, base_delay * (2 ** (attempt - 1)))
            delay = max(exponential_delay, retry_after or 0)
            if on_retry_event is not None:
                on_retry_event(
                    {
                        "operation": operation_name,
                        "attempt": attempt,
                        "attempts": attempts,
                        "will_retry": True,
                        "delay_seconds": delay,
                        "error": str(exc),
                        "error_type": exc.__class__.__name__,
                    }
                )
            logger.debug(
                "%s failed on attempt %s/%s: %s; retrying in %.2fs",
                operation_name,
                attempt,
                attempts,
                exc,
                delay,
            )
            if delay > 0:
                await asyncio.sleep(delay)

    raise RuntimeError("retry loop exited unexpectedly")
