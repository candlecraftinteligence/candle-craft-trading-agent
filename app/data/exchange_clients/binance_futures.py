from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime
from decimal import Decimal
from typing import Any

import httpx

from app.data.candle_batch_evidence import (
    CandleBatchDelivery,
    default_capture_clock,
    new_capture_occurrence_token,
    observe_adapter_normalized_batch,
    unavailable_delivery,
)
from app.data.derivatives_instrument import InstrumentMetadata, parse_binance_usds_instrument
from app.data.derivatives_source_evidence import (
    ENDPOINT_BINANCE_ACCOUNT_RATIO,
    ENDPOINT_BINANCE_EXCHANGE_INFO,
    ENDPOINT_BINANCE_FUNDING,
    ENDPOINT_BINANCE_OPEN_INTEREST,
    ENDPOINT_BINANCE_OPEN_INTEREST_HIST,
    REASON_EVIDENCE_REPRESENTATION_FAILED,
    MetricEvidenceDelivery,
    capture_acquisition,
    describe_binance_account_ratio,
    describe_binance_funding_history,
    describe_binance_open_interest,
    describe_binance_open_interest_history,
    represent_delivery,
    unavailable_batch,
)
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
    _cci_klines_delivery_capture = True

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
        delivery = await self._get_klines_delivery(symbol, interval, limit)
        return list(delivery.returned_sequence)

    async def get_klines_with_delivery(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        capture_clock: Callable[[], datetime] | None = None,
        occurrence_factory: Callable[[], str] | None = None,
    ) -> CandleBatchDelivery:
        return await self._get_klines_delivery(
            symbol,
            interval,
            limit,
            capture_clock=capture_clock,
            occurrence_factory=occurrence_factory,
        )

    async def _get_klines_delivery(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        capture_clock: Callable[[], datetime] | None = None,
        occurrence_factory: Callable[[], str] | None = None,
    ) -> CandleBatchDelivery:
        safe_limit = _clamp_limit(limit, maximum=1500)
        normalized_symbol = symbol.upper()
        payload = await self._get_json(
            "/fapi/v1/klines",
            params={"symbol": normalized_symbol, "interval": interval, "limit": safe_limit},
        )
        candles = normalize_binance_klines(normalized_symbol, interval, payload)
        clock = capture_clock or default_capture_clock
        token_factory = occurrence_factory or new_capture_occurrence_token
        try:
            return observe_adapter_normalized_batch(
                candles,
                requested_symbol=symbol,
                requested_interval=interval,
                requested_limit=limit,
                effective_symbol=normalized_symbol,
                effective_interval=interval,
                effective_limit=safe_limit,
                endpoint_path="/fapi/v1/klines",
                adapter_class_label=f"{type(self).__module__}.{type(self).__qualname__}",
                capture_clock=clock,
                occurrence_factory=token_factory,
                http_client_injected=not self._owns_client,
                base_url_label=self.base_url,
            )
        except Exception:
            return unavailable_delivery(
                candles,
                reason="capture_representation_failed",
                capture_clock=clock,
                occurrence_factory=token_factory,
            )

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

    async def get_exchange_info(self) -> Mapping[str, Any]:
        payload = await self._get_json("/fapi/v1/exchangeInfo")
        if not isinstance(payload, Mapping):
            raise ExchangeResponseError("Expected object response at binance.exchange_info")
        return payload

    async def get_funding_rate(self, symbol: str) -> FundingDTO:
        return (await self.get_funding_rate_with_evidence(symbol)).value

    async def get_funding_rate_with_evidence(
        self,
        symbol: str,
        *,
        capture_clock: Callable[[], datetime] | None = None,
    ) -> MetricEvidenceDelivery:
        normalized_symbol = symbol.upper()
        history = await self.get_funding_rate_history_with_evidence(
            normalized_symbol,
            limit=1,
            capture_clock=capture_clock,
        )
        if history.value:
            return represent_delivery(history.value[-1], history.evidence)
        params = {"symbol": normalized_symbol, "limit": 1}
        payload = await self._get_json(ENDPOINT_BINANCE_FUNDING, params=params)
        acquired = capture_acquisition(capture_clock)
        value = normalize_binance_funding(normalized_symbol, payload)
        evidence = _describe_safely(
            lambda: describe_binance_funding_history(
                requested_symbol=normalized_symbol,
                payload=payload,
                request_params=params,
                capture_clock=None,
                acquisition=acquired,
            ),
            metric="funding",
            endpoint=ENDPOINT_BINANCE_FUNDING,
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
        _validate_limit(limit, maximum=1000)
        normalized_symbol = symbol.upper()
        params = {"symbol": normalized_symbol, "limit": limit}
        payload = await self._get_json(ENDPOINT_BINANCE_FUNDING, params=params)
        acquired = capture_acquisition(capture_clock)
        value = normalize_binance_funding_history(normalized_symbol, payload)
        evidence = _describe_safely(
            lambda: describe_binance_funding_history(
                requested_symbol=normalized_symbol,
                payload=payload,
                request_params=params,
                capture_clock=None,
                acquisition=acquired,
            ),
            metric="funding",
            endpoint=ENDPOINT_BINANCE_FUNDING,
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
        normalized_symbol = symbol.upper()
        params = {"symbol": normalized_symbol}
        payload = await self._get_json(ENDPOINT_BINANCE_OPEN_INTEREST, params=params)
        acquired = capture_acquisition(capture_clock)
        value = normalize_binance_open_interest(normalized_symbol, payload)
        evidence = _describe_safely(
            lambda: describe_binance_open_interest(
                requested_symbol=normalized_symbol,
                payload=payload,
                request_params=params,
                capture_clock=None,
                acquisition=acquired,
            ),
            metric="open_interest",
            endpoint=ENDPOINT_BINANCE_OPEN_INTEREST,
        )
        return represent_delivery(value, evidence)

    async def get_open_interest_history(
        self,
        symbol: str,
        limit: int = 30,
        period: str = "5m",
    ) -> list[OpenInterestDTO]:
        return (await self.get_open_interest_history_with_evidence(symbol, limit=limit, period=period)).value

    async def get_open_interest_history_with_evidence(
        self,
        symbol: str,
        limit: int = 30,
        period: str = "5m",
        *,
        capture_clock: Callable[[], datetime] | None = None,
    ) -> MetricEvidenceDelivery:
        _validate_limit(limit, maximum=500)
        normalized_symbol = symbol.upper()
        params = {"symbol": normalized_symbol, "period": period, "limit": limit}
        payload = await self._get_json(ENDPOINT_BINANCE_OPEN_INTEREST_HIST, params=params)
        acquired = capture_acquisition(capture_clock)
        value = normalize_binance_open_interest_history(normalized_symbol, payload)
        evidence = _describe_safely(
            lambda: describe_binance_open_interest_history(
                requested_symbol=normalized_symbol,
                payload=payload,
                request_params=params,
                capture_clock=None,
                acquisition=acquired,
            ),
            metric="open_interest_history",
            endpoint=ENDPOINT_BINANCE_OPEN_INTEREST_HIST,
        )
        return represent_delivery(value, evidence)

    async def get_long_short_ratio(self, symbol: str, limit: int = 1, period: str = "5m") -> Decimal:
        return (await self.get_long_short_ratio_with_evidence(symbol, limit=limit, period=period)).value

    async def get_long_short_ratio_with_evidence(
        self,
        symbol: str,
        limit: int = 1,
        period: str = "5m",
        *,
        capture_clock: Callable[[], datetime] | None = None,
    ) -> MetricEvidenceDelivery:
        _validate_limit(limit, maximum=500)
        normalized_symbol = symbol.upper()
        params = {"symbol": normalized_symbol, "period": period, "limit": limit}
        payload = await self._get_json(ENDPOINT_BINANCE_ACCOUNT_RATIO, params=params)
        acquired = capture_acquisition(capture_clock)
        value = normalize_binance_long_short_ratio(normalized_symbol, payload)
        evidence = _describe_safely(
            lambda: describe_binance_account_ratio(
                requested_symbol=normalized_symbol,
                payload=payload,
                request_params=params,
                capture_clock=None,
                acquisition=acquired,
            ),
            metric="account_ratio",
            endpoint=ENDPOINT_BINANCE_ACCOUNT_RATIO,
        )
        return represent_delivery(value, evidence)

    async def get_instrument_metadata(
        self,
        symbol: str,
        *,
        capture_clock: Callable[[], datetime] | None = None,
    ) -> InstrumentMetadata:
        normalized_symbol = symbol.upper()
        payload = await self._get_json(ENDPOINT_BINANCE_EXCHANGE_INFO, params={"symbol": normalized_symbol})
        return parse_binance_usds_instrument(
            payload,
            requested_symbol=normalized_symbol,
            acquisition=capture_acquisition(capture_clock),
        )


def _describe_safely(build, *, metric: str, endpoint: str):
    try:
        return build()
    except Exception:
        return unavailable_batch(metric=metric, endpoint=endpoint, reason=REASON_EVIDENCE_REPRESENTATION_FAILED)


def _validate_limit(limit: int, *, maximum: int) -> None:
    if limit < 1 or limit > maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")


def _clamp_limit(limit: int, *, maximum: int) -> int:
    return min(max(limit, 1), maximum)
