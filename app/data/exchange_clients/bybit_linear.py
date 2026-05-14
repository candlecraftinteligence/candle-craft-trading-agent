from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx

from app.data.dtos import CandleDTO, FundingDTO, OpenInterestDTO, TickerDTO
from app.data.exceptions import ExchangeRateLimitError, ExchangeResponseError
from app.data.exchange_clients.base import PublicHTTPExchangeClient
from app.data.normalizers.bybit import (
    normalize_bybit_funding,
    normalize_bybit_klines,
    normalize_bybit_open_interest,
    normalize_bybit_ticker,
)


class BybitLinearClient(PublicHTTPExchangeClient):
    BASE_URL = "https://api.bybit.com"
    CATEGORY = "linear"

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = BASE_URL,
        timeout: float = 10.0,
        retry_attempts: int = 3,
        retry_base_delay: float = 0.25,
        retry_max_delay: float = 2.0,
        open_interest_interval: str = "5min",
    ) -> None:
        super().__init__(
            exchange_name="bybit_linear",
            base_url=base_url,
            http_client=http_client,
            timeout=timeout,
            retry_attempts=retry_attempts,
            retry_base_delay=retry_base_delay,
            retry_max_delay=retry_max_delay,
        )
        self.open_interest_interval = open_interest_interval

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[CandleDTO]:
        _validate_limit(limit, maximum=1000)
        normalized_symbol = symbol.upper()
        payload = await self._get_json(
            "/v5/market/kline",
            params={
                "category": self.CATEGORY,
                "symbol": normalized_symbol,
                "interval": _to_bybit_interval(interval),
                "limit": limit,
            },
            validate=self._ensure_success,
        )
        return normalize_bybit_klines(normalized_symbol, interval, payload)

    async def get_ticker(self, symbol: str) -> TickerDTO:
        normalized_symbol = symbol.upper()
        payload = await self._get_json(
            "/v5/market/tickers",
            params={"category": self.CATEGORY, "symbol": normalized_symbol},
            validate=self._ensure_success,
        )
        return normalize_bybit_ticker(normalized_symbol, payload)

    async def get_funding_rate(self, symbol: str) -> FundingDTO:
        normalized_symbol = symbol.upper()
        payload = await self._get_json(
            "/v5/market/funding/history",
            params={"category": self.CATEGORY, "symbol": normalized_symbol, "limit": 1},
            validate=self._ensure_success,
        )
        return normalize_bybit_funding(normalized_symbol, payload)

    async def get_open_interest(self, symbol: str) -> OpenInterestDTO:
        normalized_symbol = symbol.upper()
        payload = await self._get_json(
            "/v5/market/open-interest",
            params={
                "category": self.CATEGORY,
                "symbol": normalized_symbol,
                "intervalTime": self.open_interest_interval,
                "limit": 1,
            },
            validate=self._ensure_success,
        )
        return normalize_bybit_open_interest(normalized_symbol, payload)

    @staticmethod
    def _ensure_success(payload: Any) -> None:
        if not isinstance(payload, Mapping):
            raise ExchangeResponseError("Bybit response must be a JSON object")

        ret_code = payload.get("retCode")
        normalized_code = str(ret_code)
        if normalized_code == "0":
            return

        ret_msg = payload.get("retMsg", "Unknown Bybit API error")
        if normalized_code == "10006":
            raise ExchangeRateLimitError(f"Bybit rate limit response: retCode={ret_code} {ret_msg}")
        raise ExchangeResponseError(f"Bybit API error: retCode={ret_code} {ret_msg}")


def _validate_limit(limit: int, *, maximum: int) -> None:
    if limit < 1 or limit > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")


def _to_bybit_interval(interval: str) -> str:
    interval_map = {
        "1m": "1",
        "3m": "3",
        "5m": "5",
        "15m": "15",
        "30m": "30",
        "1h": "60",
        "2h": "120",
        "4h": "240",
        "6h": "360",
        "12h": "720",
        "1d": "D",
        "1w": "W",
        "1M": "M",
    }
    return interval_map.get(interval, interval)
