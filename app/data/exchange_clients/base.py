from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from app.data.dtos import CandleDTO, FundingDTO, OpenInterestDTO, TickerDTO
from app.data.exceptions import (
    ExchangeMalformedJSONError,
    ExchangeHTTPError,
    ExchangeNetworkError,
    ExchangeRateLimitError,
    ExchangeTimeoutError,
)
from app.data.retry import retry_async


class BaseExchangeClient(ABC):
    @abstractmethod
    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[CandleDTO]:
        raise NotImplementedError

    @abstractmethod
    async def get_ticker(self, symbol: str) -> TickerDTO:
        raise NotImplementedError

    @abstractmethod
    async def get_funding_rate(self, symbol: str) -> FundingDTO:
        raise NotImplementedError

    @abstractmethod
    async def get_open_interest(self, symbol: str) -> OpenInterestDTO:
        raise NotImplementedError


class PublicHTTPExchangeClient(BaseExchangeClient):
    def __init__(
        self,
        *,
        exchange_name: str,
        base_url: str,
        http_client: httpx.AsyncClient | None = None,
        timeout: float = 10.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 0.25,
        retry_max_delay: float = 2.0,
    ) -> None:
        self.exchange_name = exchange_name
        self.base_url = base_url.rstrip("/")
        self._owns_client = http_client is None
        self._http_client = http_client or httpx.AsyncClient(
            base_url=self.base_url,
            timeout=timeout,
        )
        self._retry_attempts = retry_attempts
        self._retry_base_delay = retry_base_delay
        self._retry_max_delay = retry_max_delay
        self._logger = logging.getLogger(f"{__name__}.{exchange_name}")

    async def __aenter__(self) -> PublicHTTPExchangeClient:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._http_client.aclose()

    async def _get_json(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
        validate: Callable[[Any], None] | None = None,
    ) -> Any:
        async def operation() -> Any:
            response = await self._send_once(path, params=params)
            payload = self._decode_json(response, path)
            if validate is not None:
                validate(payload)
            return payload

        return await retry_async(
            operation,
            attempts=self._retry_attempts,
            base_delay=self._retry_base_delay,
            max_delay=self._retry_max_delay,
            logger=self._logger,
            operation_name=f"{self.exchange_name} GET {path}",
        )

    async def _send_once(
        self,
        path: str,
        *,
        params: Mapping[str, Any] | None = None,
    ) -> httpx.Response:
        try:
            response = await self._http_client.get(path, params=params)
        except httpx.TimeoutException as exc:
            raise ExchangeTimeoutError(f"{self.exchange_name} request timed out: {path}") from exc
        except httpx.TransportError as exc:
            raise ExchangeNetworkError(f"{self.exchange_name} request failed: {path}") from exc

        if response.status_code in {418, 429}:
            raise ExchangeRateLimitError(
                f"{self.exchange_name} rate limit response: HTTP {response.status_code}",
                status_code=response.status_code,
                retry_after=self._parse_retry_after(response),
            )

        if not 200 <= response.status_code < 300:
            raise ExchangeHTTPError(
                f"{self.exchange_name} non-200 response: HTTP {response.status_code}",
                status_code=response.status_code,
                response_body=response.text[:500],
            )

        return response

    def _decode_json(self, response: httpx.Response, path: str) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise ExchangeMalformedJSONError(
                f"{self.exchange_name} returned malformed JSON for {path}"
            ) from exc

    @staticmethod
    def _parse_retry_after(response: httpx.Response) -> float | None:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        try:
            return max(float(value), 0.0)
        except ValueError:
            return None
