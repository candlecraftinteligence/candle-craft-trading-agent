from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from app.data.derivatives_instrument import InstrumentMetadata, parse_bybit_linear_instrument
from app.data.derivatives_source_evidence import (
    ENDPOINT_BYBIT_ACCOUNT_RATIO,
    ENDPOINT_BYBIT_FUNDING,
    ENDPOINT_BYBIT_INSTRUMENT,
    ENDPOINT_BYBIT_OPEN_INTEREST,
    REASON_EVIDENCE_REPRESENTATION_FAILED,
    MetricEvidenceDelivery,
    capture_acquisition,
    describe_bybit_account_ratio,
    describe_bybit_funding_history,
    describe_bybit_open_interest,
    represent_delivery,
    unavailable_batch,
)
from app.data.dtos import CandleDTO, FundingDTO, OpenInterestDTO, TickerDTO
from app.data.exceptions import ExchangeRateLimitError, ExchangeResponseError
from app.data.exchange_clients.base import PublicHTTPExchangeClient
from app.data.normalizers.bybit import (
    normalize_bybit_funding,
    normalize_bybit_funding_history,
    normalize_bybit_klines,
    normalize_bybit_long_short_ratio,
    normalize_bybit_open_interest,
    normalize_bybit_open_interest_history,
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
        return (await self.get_funding_rate_with_evidence(symbol)).value

    async def get_funding_rate_with_evidence(
        self,
        symbol: str,
        *,
        capture_clock: Callable[[], datetime] | None = None,
    ) -> MetricEvidenceDelivery:
        history = await self.get_funding_rate_history_with_evidence(symbol, limit=1, capture_clock=capture_clock)
        if history.value:
            return represent_delivery(history.value[-1], history.evidence)
        normalized_symbol = symbol.upper()
        params = {"category": self.CATEGORY, "symbol": normalized_symbol, "limit": 1}
        payload = await self._get_json(ENDPOINT_BYBIT_FUNDING, params=params, validate=self._ensure_success)
        acquired = capture_acquisition(capture_clock)
        value = normalize_bybit_funding(normalized_symbol, payload)
        evidence = _describe_safely(
            lambda: describe_bybit_funding_history(
                requested_symbol=normalized_symbol,
                payload=payload,
                request_params=params,
                capture_clock=None,
                acquisition=acquired,
            ),
            metric="funding",
            endpoint=ENDPOINT_BYBIT_FUNDING,
        )
        return represent_delivery(value, evidence)

    async def get_funding_rate_history(self, symbol: str, limit: int = 100) -> list[FundingDTO]:
        return (await self.get_funding_rate_history_with_evidence(symbol, limit=limit)).value

    async def get_funding_rate_history_with_evidence(
        self,
        symbol: str,
        limit: int = 100,
        *,
        capture_clock: Callable[[], datetime] | None = None,
    ) -> MetricEvidenceDelivery:
        _validate_limit(limit, maximum=200)
        normalized_symbol = symbol.upper()
        params = {"category": self.CATEGORY, "symbol": normalized_symbol, "limit": limit}
        payload = await self._get_json(ENDPOINT_BYBIT_FUNDING, params=params, validate=self._ensure_success)
        acquired = capture_acquisition(capture_clock)
        value = normalize_bybit_funding_history(normalized_symbol, payload)
        evidence = _describe_safely(
            lambda: describe_bybit_funding_history(
                requested_symbol=normalized_symbol,
                payload=payload,
                request_params=params,
                capture_clock=None,
                acquisition=acquired,
            ),
            metric="funding",
            endpoint=ENDPOINT_BYBIT_FUNDING,
        )
        return represent_delivery(value, evidence)

    async def get_open_interest(self, symbol: str) -> OpenInterestDTO:
        return (await self.get_open_interest_with_evidence(symbol)).value

    async def get_open_interest_with_evidence(
        self,
        symbol: str,
        *,
        capture_clock: Callable[[], datetime] | None = None,
    ) -> MetricEvidenceDelivery:
        history = await self.get_open_interest_history_with_evidence(symbol, limit=1, capture_clock=capture_clock)
        if history.value:
            return represent_delivery(history.value[-1], history.evidence)
        normalized_symbol = symbol.upper()
        params = {
            "category": self.CATEGORY,
            "symbol": normalized_symbol,
            "intervalTime": self.open_interest_interval,
            "limit": 1,
        }
        payload = await self._get_json(ENDPOINT_BYBIT_OPEN_INTEREST, params=params, validate=self._ensure_success)
        acquired = capture_acquisition(capture_clock)
        value = normalize_bybit_open_interest(normalized_symbol, payload)
        evidence = _describe_safely(
            lambda: describe_bybit_open_interest(
                requested_symbol=normalized_symbol,
                payload=payload,
                request_params=params,
                capture_clock=None,
                acquisition=acquired,
            ),
            metric="open_interest",
            endpoint=ENDPOINT_BYBIT_OPEN_INTEREST,
        )
        return represent_delivery(value, evidence)

    async def get_open_interest_history(self, symbol: str, limit: int = 30) -> list[OpenInterestDTO]:
        return (await self.get_open_interest_history_with_evidence(symbol, limit=limit)).value

    async def get_open_interest_history_with_evidence(
        self,
        symbol: str,
        limit: int = 30,
        *,
        capture_clock: Callable[[], datetime] | None = None,
    ) -> MetricEvidenceDelivery:
        _validate_limit(limit, maximum=200)
        normalized_symbol = symbol.upper()
        params = {
            "category": self.CATEGORY,
            "symbol": normalized_symbol,
            "intervalTime": self.open_interest_interval,
            "limit": limit,
        }
        payload = await self._get_json(ENDPOINT_BYBIT_OPEN_INTEREST, params=params, validate=self._ensure_success)
        acquired = capture_acquisition(capture_clock)
        value = normalize_bybit_open_interest_history(normalized_symbol, payload)
        evidence = _describe_safely(
            lambda: describe_bybit_open_interest(
                requested_symbol=normalized_symbol,
                payload=payload,
                request_params=params,
                capture_clock=None,
                acquisition=acquired,
            ),
            metric="open_interest",
            endpoint=ENDPOINT_BYBIT_OPEN_INTEREST,
        )
        return represent_delivery(value, evidence)

    async def get_long_short_ratio(self, symbol: str, limit: int = 1, period: str = "5min") -> Decimal:
        return (await self.get_long_short_ratio_with_evidence(symbol, limit=limit, period=period)).value

    async def get_long_short_ratio_with_evidence(
        self,
        symbol: str,
        limit: int = 1,
        period: str = "5min",
        *,
        capture_clock: Callable[[], datetime] | None = None,
    ) -> MetricEvidenceDelivery:
        _validate_limit(limit, maximum=500)
        normalized_symbol = symbol.upper()
        params = {"category": self.CATEGORY, "symbol": normalized_symbol, "period": period, "limit": limit}
        payload = await self._get_json(ENDPOINT_BYBIT_ACCOUNT_RATIO, params=params, validate=self._ensure_success)
        acquired = capture_acquisition(capture_clock)
        value = normalize_bybit_long_short_ratio(normalized_symbol, payload)
        evidence = _describe_safely(
            lambda: describe_bybit_account_ratio(
                requested_symbol=normalized_symbol,
                payload=payload,
                request_params=params,
                capture_clock=None,
                acquisition=acquired,
            ),
            metric="account_ratio",
            endpoint=ENDPOINT_BYBIT_ACCOUNT_RATIO,
        )
        return represent_delivery(value, evidence)

    async def get_price_precision(self, symbol: str):
        from app.data.instrument_precision import parse_precision
        payload = await self._get_json(ENDPOINT_BYBIT_INSTRUMENT,
            params={"category": self.CATEGORY, "symbol": symbol.upper()}, validate=self._ensure_success)
        return parse_precision(payload, exchange="bybit", symbol=symbol.upper())

    async def get_instrument_metadata(
        self,
        symbol: str,
        *,
        capture_clock: Callable[[], datetime] | None = None,
    ) -> InstrumentMetadata:
        normalized_symbol = symbol.upper()
        params = {"category": self.CATEGORY, "symbol": normalized_symbol}
        payload = await self._get_json(ENDPOINT_BYBIT_INSTRUMENT, params=params, validate=self._ensure_success)
        return parse_bybit_linear_instrument(
            payload,
            requested_symbol=normalized_symbol,
            acquisition=capture_acquisition(capture_clock),
        )

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


def _describe_safely(build, *, metric: str, endpoint: str):
    try:
        return build()
    except Exception:
        return unavailable_batch(metric=metric, endpoint=endpoint, reason=REASON_EVIDENCE_REPRESENTATION_FAILED)


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
