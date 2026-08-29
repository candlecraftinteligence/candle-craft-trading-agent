from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final

from app.context.macro_events import (
    MacroEvent,
    MacroEventRiskSnapshot,
    MacroSourceHealth,
    MacroSourceStatus,
    MacroVerificationStatus,
    build_macro_event_snapshot,
    dedupe_macro_events,
    strict_aware_utc,
)
from app.context.macro_providers import (
    BeaCalendarProvider,
    BlsCalendarProvider,
    FederalReserveCalendarProvider,
    MacroCalendarProvider,
    MacroProviderObservation,
)


DEFAULT_MACRO_CACHE_TTL_SECONDS: Final = 6 * 60 * 60
DEFAULT_MACRO_MAX_STALE_SECONDS: Final = 48 * 60 * 60
DEFAULT_MACRO_SNAPSHOT_LOOKBACK_SECONDS: Final = 24 * 60 * 60
DEFAULT_MACRO_SNAPSHOT_LOOKAHEAD_SECONDS: Final = 7 * 24 * 60 * 60

logger = logging.getLogger(__name__)


class MacroCalendarService:
    """Aggregate official schedules once per scan and reuse them across symbols."""

    def __init__(
        self,
        providers: Sequence[MacroCalendarProvider],
        *,
        cache_ttl_seconds: int = DEFAULT_MACRO_CACHE_TTL_SECONDS,
        max_stale_seconds: int = DEFAULT_MACRO_MAX_STALE_SECONDS,
        snapshot_lookback_seconds: int = DEFAULT_MACRO_SNAPSHOT_LOOKBACK_SECONDS,
        snapshot_lookahead_seconds: int = DEFAULT_MACRO_SNAPSHOT_LOOKAHEAD_SECONDS,
        clock: Callable[[], datetime] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        if not providers:
            raise ValueError("macro calendar service requires at least one provider")
        if cache_ttl_seconds < 0:
            raise ValueError("macro calendar cache TTL must be zero or greater")
        if max_stale_seconds < cache_ttl_seconds:
            raise ValueError("macro maximum stale tolerance must be at least the cache TTL")
        if snapshot_lookback_seconds < 0 or snapshot_lookahead_seconds < 0:
            raise ValueError("macro snapshot horizons must be zero or greater")
        sources = tuple(str(provider.source).strip() for provider in providers)
        if len(set(sources)) != len(sources):
            raise ValueError("macro calendar provider sources must be unique")
        self.providers = tuple(providers)
        self.cache_ttl_seconds = int(cache_ttl_seconds)
        self.max_stale_seconds = int(max_stale_seconds)
        self.snapshot_lookback_seconds = int(snapshot_lookback_seconds)
        self.snapshot_lookahead_seconds = int(snapshot_lookahead_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.logger = log or logger
        self._cached: dict[str, MacroProviderObservation] = {}
        self._lock = asyncio.Lock()
        self.provider_calls: dict[str, int] = {source: 0 for source in sources}

    async def snapshot(self, *, as_of: datetime) -> MacroEventRiskSnapshot:
        decision_time = strict_aware_utc(as_of, field_name="macro_snapshot_as_of")
        async with self._lock:
            fetch_time = strict_aware_utc(
                self.clock(), field_name="macro_calendar_service_clock"
            )
            provider_results = await asyncio.gather(
                *(
                    self._provider_result(provider, fetch_time=fetch_time)
                    for provider in self.providers
                )
            )
            events = tuple(
                event
                for result in provider_results
                for event in result.events
            )
            relevant_events = _relevant_events(
                dedupe_macro_events(events),
                as_of=decision_time,
                lookback_seconds=self.snapshot_lookback_seconds,
                lookahead_seconds=self.snapshot_lookahead_seconds,
            )
            return build_macro_event_snapshot(
                generated_at=decision_time,
                events=relevant_events,
                sources=tuple(result.health for result in provider_results),
                provider_requests=sum(result.health.request_count for result in provider_results),
                cache_hit=all(result.health.cache_hit for result in provider_results),
            )

    async def _provider_result(
        self,
        provider: MacroCalendarProvider,
        *,
        fetch_time: datetime,
    ) -> _ProviderResult:
        source = str(provider.source).strip()
        cached = self._cached.get(source)
        if cached is not None and _age_seconds(fetch_time, cached.fetched_at) <= self.cache_ttl_seconds:
            return _ProviderResult(
                events=cached.events,
                health=_health_from_observation(cached, cache_hit=True, request_count=0),
            )
        self.provider_calls[source] += 1
        try:
            observation = await provider.fetch()
            if observation.source != source:
                raise RuntimeError(
                    f"macro provider source mismatch: expected {source}, got {observation.source}"
                )
        except Exception as exc:
            reason = f"{source} provider unavailable: {_clean_reason(exc)}"
            if cached is not None and _age_seconds(fetch_time, cached.fetched_at) <= self.max_stale_seconds:
                stale_events = tuple(
                    event.model_copy(
                        update={"verification_status": MacroVerificationStatus.UNVERIFIED}
                    )
                    for event in cached.events
                )
                return _ProviderResult(
                    events=stale_events,
                    health=MacroSourceHealth(
                        source=source,
                        source_url=str(provider.source_url),
                        status=MacroSourceStatus.STALE,
                        verification=MacroVerificationStatus.UNVERIFIED,
                        fetched_at=cached.fetched_at,
                        cache_hit=True,
                        event_count=len(stale_events),
                        request_count=1,
                        reason=reason,
                        calendar_version=cached.calendar_version,
                    ),
                )
            return _ProviderResult(
                events=(),
                health=MacroSourceHealth(
                    source=source,
                    source_url=str(provider.source_url),
                    status=MacroSourceStatus.UNAVAILABLE,
                    verification=MacroVerificationStatus.UNVERIFIED,
                    fetched_at=cached.fetched_at if cached is not None else None,
                    event_count=0,
                    request_count=1,
                    reason=(
                        f"{reason}; cached calendar exceeds maximum stale tolerance"
                        if cached is not None
                        else reason
                    ),
                    calendar_version=(cached.calendar_version if cached is not None else None),
                ),
            )
        self._cached[source] = observation
        return _ProviderResult(
            events=observation.events,
            health=_health_from_observation(
                observation,
                cache_hit=False,
                request_count=observation.request_count,
            ),
        )


class _ProviderResult:
    def __init__(
        self,
        *,
        events: Sequence[MacroEvent],
        health: MacroSourceHealth,
    ) -> None:
        self.events = tuple(events)
        self.health = health


def default_macro_calendar_service(
    *,
    timeout_seconds: float,
    cache_ttl_seconds: int = DEFAULT_MACRO_CACHE_TTL_SECONDS,
    max_stale_seconds: int = DEFAULT_MACRO_MAX_STALE_SECONDS,
    clock: Callable[[], datetime] | None = None,
    log: logging.Logger | None = None,
) -> MacroCalendarService:
    provider_clock = clock or (lambda: datetime.now(UTC))
    providers: tuple[MacroCalendarProvider, ...] = (
        BlsCalendarProvider(
            timeout_seconds=timeout_seconds,
            clock=provider_clock,
            log=log,
        ),
        FederalReserveCalendarProvider(
            timeout_seconds=timeout_seconds,
            clock=provider_clock,
            log=log,
        ),
        BeaCalendarProvider(
            timeout_seconds=timeout_seconds,
            clock=provider_clock,
            log=log,
        ),
    )
    return MacroCalendarService(
        providers,
        cache_ttl_seconds=cache_ttl_seconds,
        max_stale_seconds=max_stale_seconds,
        clock=provider_clock,
        log=log,
    )


def _health_from_observation(
    observation: MacroProviderObservation,
    *,
    cache_hit: bool,
    request_count: int,
) -> MacroSourceHealth:
    return MacroSourceHealth(
        source=observation.source,
        source_url=observation.source_url,
        status=observation.status,
        verification=MacroVerificationStatus.VERIFIED,
        fetched_at=observation.fetched_at,
        cache_hit=cache_hit,
        event_count=len(observation.events),
        request_count=request_count,
        reason=observation.reason,
        calendar_version=observation.calendar_version,
    )


def _relevant_events(
    events: Sequence[MacroEvent],
    *,
    as_of: datetime,
    lookback_seconds: int,
    lookahead_seconds: int,
) -> tuple[MacroEvent, ...]:
    ordered = tuple(sorted(events, key=lambda item: (item.scheduled_at_utc, item.event_id)))
    if not ordered:
        return ()
    earliest = as_of - timedelta(seconds=lookback_seconds)
    latest = as_of + timedelta(seconds=lookahead_seconds)
    selected: dict[str, MacroEvent] = {
        event.event_id: event
        for event in ordered
        if earliest <= event.scheduled_at_utc <= latest
    }
    upcoming = next((event for event in ordered if event.scheduled_at_utc >= as_of), None)
    recent = next(
        (event for event in reversed(ordered) if event.scheduled_at_utc < as_of),
        None,
    )
    for event in (upcoming, recent):
        if event is not None:
            selected[event.event_id] = event
    return tuple(
        sorted(selected.values(), key=lambda item: (item.scheduled_at_utc, item.event_id))
    )


def _age_seconds(now: datetime, observed_at: datetime) -> float:
    return max((now - observed_at).total_seconds(), 0.0)


def _clean_reason(exc: BaseException) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


__all__ = [
    "DEFAULT_MACRO_CACHE_TTL_SECONDS",
    "DEFAULT_MACRO_MAX_STALE_SECONDS",
    "DEFAULT_MACRO_SNAPSHOT_LOOKAHEAD_SECONDS",
    "DEFAULT_MACRO_SNAPSHOT_LOOKBACK_SECONDS",
    "MacroCalendarService",
    "default_macro_calendar_service",
]
