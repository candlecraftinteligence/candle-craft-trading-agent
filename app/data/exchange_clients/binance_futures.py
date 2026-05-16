from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import httpx

from app.data.dtos import CandleDTO, FundingDTO, OpenInterestDTO, TickerDTO
from app.data.exceptions import ExchangeResponseError
from app.data.exchange_clients.base import PublicHTTPExchangeClient
from app.data.normalizers.binance import (
    normalize_binance_funding,
    normalize_binance_funding_history,
    normalize_binance_klines,
    normalize_binance_long_short_ratio,
    normalize_binance_open_interest,
    normalize_binance_open_interest_history,
    normalize_binance_ticker,
)


class BinanceFuturesClient(PublicHTTPExchangeClient):
    BASE_URL = "https://fapi.binance.com"

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 0.25,
        retry_max_delay: float = 2.0,
    ) -> None:
        super().__init__(
            exchange_name="binance_futures",
            base_url=base_url,
            http_client=http_client,
            timeout=timeout,
            retry_attempts=retry_attempts,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[CandleDTO]:
        _validate_limit(limit, maximum=1500)
        normalized_symbol = symbol.upper()
        payload = await self._get_json(
            "/fapi/v1/klines",
            params={"symbol": normalized_symbol, "interval": interval, "limit": limit},
        )
        return normalize_binance_klines(normalized_symbol, interval, payload)

    async def get_ticker(self, symbol: str) -> TickerDTO:
        normalized_symbol = symbol.upper()
        payload = await self._get_json(
            "/fapi/v1/ticker/24hr",
            params={"symbol": normalized_symbol},
        )
        return normalize_binance_ticker(normalized_symbol, payload)

    async def get_24h_tickers(self) -> list[Mapping[str, Any]]:
        payload = await self._get_json("/fapi/v1/ticker/24hr")
        if not isinstance(payload, list):
            raise ExchangeResponseError("Expected list response at binance.tickers_24h")
        return payload

    async def get_funding_rate(self, symbol: str) -> FundingDTO:
        normalized_symbol = symbol.upper()
        history = await self.get_funding_rate_history(normalized_symbol, limit=1)
        if not history:
            payload = await self._get_json(
                "/fapi/v1/fundingRate",
                params={"symbol": normalized_symbol, "limit": 1},
            )
            return normalize_binance_funding(normalized_symbol, payload)
        return history[-1]

    async def get_funding_rate_history(self, symbol: str, limit: int = 100) -> list[FundingDTO]:
        _validate_limit(limit, maximum=1000)
        normalized_symbol = symbol.upper()
        payload = await self._get_json(
            "/fapi/v1/fundingRate",
            params={"symbol": normalized_symbol, "limit": limit},
        )
        return normalize_binance_funding_history(normalized_symbol, payload)

    async def get_open_interest(self, symbol: str) -> OpenInterestDTO:
        normalized_symbol = symbol.upper()
        payload = await self._get_json(
            "/fapi/v1/openInterest",
            params={"symbol": normalized_symbol},
        )
        return normalize_binance_open_interest(normalized_symbol, payload)

    async def get_open_interest_history(
        self,
        symbol: str,
        limit: int = 30,
        period: str = "5m",
    ) -> list[OpenInterestDTO]:
        _validate_limit(limit, maximum=500)
        normalized_symbol = symbol.upper()
        payload = await self._get_json(
            "/futures/data/openInterestHist",
            params={"symbol": normalized_symbol, "period": period, "limit": limit},
        )
        return normalize_binance_open_interest_history(normalized_symbol, payload)

    async def get_long_short_ratio(self, symbol: str, limit: int = 1, period: str = "5m") -> Decimal:
        _validate_limit(limit, maximum=500)
        normalized_symbol = symbol.upper()
        payload = await self._get_json(
            "/futures/data/globalLongShortAccountRatio",
            params={"symbol": normalized_symbol, "period": period, "limit": limit},
        )
        return normalize_binance_long_short_ratio(normalized_symbol, payload)


def _validate_limit(limit: int, *, maximum: int) -> None:
    if limit < 1 or limit > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")
