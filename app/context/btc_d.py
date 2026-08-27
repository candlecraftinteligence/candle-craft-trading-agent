from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, field_validator

from app.context.models import BtcDominancePayload, ContextStatus, ContextValue
from app.data.candle_integrity import normalize_utc_timestamp
from app.data.exceptions import (
    ExchangeHTTPError,
    ExchangeMalformedJSONError,
    ExchangeNetworkError,
    ExchangeRateLimitError,
    ExchangeResponseError,
    ExchangeTimeoutError,
)
from app.data.retry import retry_async

COINPAPRIKA_BASE_URL = "https://api.coinpaprika.com/v1"
COINPAPRIKA_BTC_D_SOURCE = "coinpaprika:/v1/global"
BTC_D_USER_AGENT = "candle-craft-trading-agent/global-context"
DEFAULT_BTC_D_CACHE_TTL_SECONDS = 300
DEFAULT_BTC_D_FRESH_SECONDS = 600
DEFAULT_BTC_D_MAX_STALE_SECONDS = 3600
DEFAULT_BTC_D_REQUEST_TIMEOUT_SECONDS = 5.0
DEFAULT_BTC_D_RETRY_ATTEMPTS = 3
DEFAULT_BTC_D_RETRY_BASE_DELAY_SECONDS = 0.25
DEFAULT_BTC_D_RETRY_MAX_DELAY_SECONDS = 2.0

logger = logging.getLogger(__name__)


class BtcDominanceObservation(BaseModel):
    btc_dominance_pct: Decimal
    observed_at: datetime
    source: str

    model_config = ConfigDict(frozen=True)

    @field_validator("btc_dominance_pct", mode="before")
    @classmethod
    def _valid_dominance(cls, value: Any) -> Decimal:
        if value is None or value == "" or isinstance(value, bool):
            raise ValueError("bitcoin_dominance_percentage is missing")
        try:
            normalized = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise ValueError("bitcoin_dominance_percentage is not numeric") from exc
        if not normalized.is_finite() or normalized < 0 or normalized > 100:
            raise ValueError("bitcoin_dominance_percentage must be between 0 and 100")
        return normalized

    @field_validator("observed_at", mode="before")
    @classmethod
    def _normalize_observed_at(cls, value: Any) -> datetime:
        return normalize_utc_timestamp(value, field_name="btc_d_observed_at")


class BtcDominanceProvider(Protocol):
    source: str

    async def get_snapshot(self) -> BtcDominanceObservation:
        ...


class CoinPaprikaBtcDominanceProvider:
    source = COINPAPRIKA_BTC_D_SOURCE

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = COINPAPRIKA_BASE_URL,
        timeout_seconds: float = DEFAULT_BTC_D_REQUEST_TIMEOUT_SECONDS,
        retry_attempts: int = DEFAULT_BTC_D_RETRY_ATTEMPTS,
        retry_base_delay_seconds: float = DEFAULT_BTC_D_RETRY_BASE_DELAY_SECONDS,
        retry_max_delay_seconds: float = DEFAULT_BTC_D_RETRY_MAX_DELAY_SECONDS,
        log: logging.Logger | None = None,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("BTC.D request timeout must be greater than zero")
        if retry_attempts < 1:
            raise ValueError("BTC.D retry attempts must be at least one")
        self._http_client = http_client
        self._base_url = base_url.rstrip("/")
        self._timeout_seconds = float(timeout_seconds)
        self._retry_attempts = retry_attempts
        self._retry_base_delay_seconds = max(float(retry_base_delay_seconds), 0.0)
        self._retry_max_delay_seconds = max(float(retry_max_delay_seconds), 0.0)
        self._logger = log or logger
        self._retry_events: list[dict[str, Any]] = []

    async def get_snapshot(self) -> BtcDominanceObservation:
        payload = await retry_async(
            self._get_once,
            attempts=self._retry_attempts,
            base_delay=self._retry_base_delay_seconds,
            max_delay=self._retry_max_delay_seconds,
            logger=self._logger,
            operation_name="coinpaprika GET /global",
            on_retry_event=lambda event: self._retry_events.append(dict(event)),
        )
        if not isinstance(payload, dict):
            raise ExchangeResponseError("CoinPaprika BTC.D response must be an object")
        try:
            last_updated = Decimal(str(payload["last_updated"]))
            observed_at = datetime.fromtimestamp(float(last_updated), tz=UTC)
            return BtcDominanceObservation(
                btc_dominance_pct=payload.get("bitcoin_dominance_percentage"),
                observed_at=observed_at,
                source=self.source,
            )
        except KeyError as exc:
            raise ExchangeResponseError("CoinPaprika BTC.D response is missing last_updated") from exc
        except (InvalidOperation, TypeError, ValueError, OSError, OverflowError) as exc:
            raise ExchangeResponseError(f"CoinPaprika BTC.D response is malformed: {exc}") from exc

    async def _get_once(self) -> dict[str, Any]:
        owns_client = self._http_client is None
        client = self._http_client or httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._timeout_seconds,
            headers={"User-Agent": BTC_D_USER_AGENT},
        )
        try:
            try:
                response = await client.get("/global")
            except httpx.TimeoutException as exc:
                raise ExchangeTimeoutError("CoinPaprika BTC.D request timed out") from exc
            except httpx.TransportError as exc:
                raise ExchangeNetworkError("CoinPaprika BTC.D request failed") from exc
        finally:
            if owns_client:
                await client.aclose()

        if response.status_code == 429:
            raise ExchangeRateLimitError(
                "CoinPaprika BTC.D rate limit response: HTTP 429",
                status_code=429,
                retry_after=_retry_after(response),
            )
        if not 200 <= response.status_code < 300:
            raise ExchangeHTTPError(
                f"CoinPaprika BTC.D non-200 response: HTTP {response.status_code}",
                status_code=response.status_code,
                response_body=response.text[:500],
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExchangeMalformedJSONError("CoinPaprika BTC.D returned malformed JSON") from exc
        if not isinstance(payload, dict):
            raise ExchangeResponseError("CoinPaprika BTC.D response must be an object")
        return payload

    def retry_events(self) -> tuple[dict[str, Any], ...]:
        return tuple(self._retry_events)


class BtcDominanceContextService:
    def __init__(
        self,
        provider: BtcDominanceProvider,
        *,
        cache_ttl_seconds: int = DEFAULT_BTC_D_CACHE_TTL_SECONDS,
        fresh_seconds: int = DEFAULT_BTC_D_FRESH_SECONDS,
        max_stale_seconds: int = DEFAULT_BTC_D_MAX_STALE_SECONDS,
        clock: Any | None = None,
    ) -> None:
        if cache_ttl_seconds < 0:
            raise ValueError("BTC.D cache TTL must be zero or greater")
        if fresh_seconds < 0:
            raise ValueError("BTC.D freshness window must be zero or greater")
        if max_stale_seconds < fresh_seconds:
            raise ValueError("BTC.D max stale window must be at least the freshness window")
        self.provider = provider
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self.fresh_seconds = int(fresh_seconds)
        self.max_stale_seconds = int(max_stale_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))
        self._cached_observation: BtcDominanceObservation | None = None
        self._cached_at: datetime | None = None
        self._lock = asyncio.Lock()
        self.provider_calls = 0

    async def get_context(self) -> ContextValue:
        async with self._lock:
            return await self._get_context_locked()

    async def _get_context_locked(self) -> ContextValue:
        now = normalize_utc_timestamp(self.clock(), field_name="btc_d_context_clock")
        if self._cache_is_fresh(now):
            return self._context_from_observation(
                self._cached_observation,
                now=now,
                cache_hit=True,
            )

        stale_candidate = self._usable_stale_candidate(now)
        try:
            self.provider_calls += 1
            observation = await self.provider.get_snapshot()
        except Exception as exc:
            reason = f"BTC.D provider unavailable: {_clean_reason(exc)}"
            if stale_candidate is not None:
                return self._context_from_observation(
                    stale_candidate,
                    now=now,
                    cache_hit=True,
                    force_stale_reason=reason,
                )
            return ContextValue.unavailable(
                source=_provider_source(self.provider),
                reason=reason,
            )

        self._cached_observation = observation
        self._cached_at = now
        return self._context_from_observation(observation, now=now, cache_hit=False)

    def _cache_is_fresh(self, now: datetime) -> bool:
        if self._cached_observation is None or self._cached_at is None:
            return False
        return (now - self._cached_at).total_seconds() <= self.cache_ttl_seconds

    def _usable_stale_candidate(self, now: datetime) -> BtcDominanceObservation | None:
        observation = self._cached_observation
        if observation is None:
            return None
        age_seconds = max((now - observation.observed_at).total_seconds(), 0.0)
        return observation if age_seconds <= self.max_stale_seconds else None

    def _context_from_observation(
        self,
        observation: BtcDominanceObservation | None,
        *,
        now: datetime,
        cache_hit: bool,
        force_stale_reason: str | None = None,
    ) -> ContextValue:
        if observation is None:
            return ContextValue.unavailable(
                source=_provider_source(self.provider),
                reason="BTC.D observation unavailable",
            )
        age_seconds = max((now - observation.observed_at).total_seconds(), 0.0)
        if force_stale_reason is not None:
            status = ContextStatus.STALE
            reason = force_stale_reason
        elif age_seconds <= self.fresh_seconds:
            status = ContextStatus.VERIFIED
            reason = None
        elif age_seconds <= self.max_stale_seconds:
            status = ContextStatus.STALE
            reason = "BTC.D observation exceeds its freshness window"
        else:
            status = ContextStatus.UNAVAILABLE
            reason = "BTC.D observation exceeds the maximum stale tolerance"
        return ContextValue(
            value=BtcDominancePayload(btc_dominance_pct=observation.btc_dominance_pct),
            source=observation.source,
            observed_at=observation.observed_at,
            age_seconds=age_seconds,
            status=status,
            reason=reason,
            cache_hit=cache_hit,
        )


def _provider_source(provider: BtcDominanceProvider) -> str:
    source = getattr(provider, "source", None)
    return str(source).strip() if source else provider.__class__.__name__


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return max(float(raw), 0.0)
    except ValueError:
        return None


def _clean_reason(exc: Exception) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


__all__ = [
    "BTC_D_USER_AGENT",
    "BtcDominanceContextService",
    "BtcDominanceObservation",
    "BtcDominanceProvider",
    "COINPAPRIKA_BTC_D_SOURCE",
    "CoinPaprikaBtcDominanceProvider",
    "DEFAULT_BTC_D_CACHE_TTL_SECONDS",
    "DEFAULT_BTC_D_FRESH_SECONDS",
    "DEFAULT_BTC_D_MAX_STALE_SECONDS",
    "DEFAULT_BTC_D_REQUEST_TIMEOUT_SECONDS",
]
