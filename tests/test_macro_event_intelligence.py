from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from app.context.macro_calendar import MacroCalendarService
from app.context.macro_events import (
    EVENT_CATEGORY_BY_TYPE,
    RESEARCH_PRIORITY_BY_TYPE,
    MacroCalendarStatus,
    MacroEvent,
    MacroEventType,
    MacroSourceHealth,
    MacroSourceStatus,
    MacroVerificationStatus,
    MacroWindowPhase,
    ResearchPriority,
    build_event_id,
    build_macro_event_snapshot,
    normalize_event_type,
)
from app.context.macro_providers import (
    BEA_RELEASE_DATES_URL,
    BLS_CALENDAR_URL,
    FED_MONTH_URL_TEMPLATE,
    BeaCalendarProvider,
    BlsCalendarProvider,
    FederalReserveCalendarProvider,
    MacroProviderObservation,
    parse_bea_release_dates,
    parse_bls_ics,
    parse_federal_reserve_month_html,
)
from app.context.macro_scanner_enrichment import (
    apply_macro_event_context_to_symbol_result,
)
from app.core.confirmed_data_health import (
    classify_confirmed_data_health,
    confirmed_data_health_for_symbol,
)
from app.lifecycle.eligibility import public_watchlist_eligible
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerRunner,
    ScannerSymbolResult,
)
from app.storage.repositories import _storage_payload
from app.strategies.liquidity_grab_pullback import LiquidityGrabEngine


NOW = datetime(2026, 1, 15, 13, 0, tzinfo=UTC)

BLS_ICS_FIXTURE = """BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//U.S. Bureau of Labor Statistics//Release Calendar//EN
BEGIN:VEVENT
UID:cpi-2026-01
SEQUENCE:1
SUMMARY:Consumer Price Index for December 2025
DTSTART;TZID=America/New_York:20260115T083000
END:VEVENT
BEGIN:VEVENT
UID:ppi-2026-07
SUMMARY:Producer Price Index for June 2026
DTSTART;TZID=America/New_York:20260715T083000
END:VEVENT
BEGIN:VEVENT
UID:empsit-2026-01
SUMMARY:The Employment Situation for December 2025
DTSTART;TZID=America/New_York:20260109T083000
END:VEVENT
BEGIN:VEVENT
UID:jolts-2026-01
SUMMARY:Job Openings and Labor Turnover Survey for November 2025
DTSTART;TZID=America/New_York:20260120T100000
END:VEVENT
BEGIN:VEVENT
UID:eci-2026-01
SUMMARY:Employment Cost Index for Fourth Quarter 2025
DTSTART;TZID=America/New_York:20260130T083000
END:VEVENT
BEGIN:VEVENT
UID:other-2026-01
SUMMARY:Import and Export Price Indexes
DTSTART;TZID=America/New_York:20260116T083000
END:VEVENT
END:VCALENDAR
"""

FED_HTML_FIXTURE = """<!doctype html><html><body>
<div class="row cal-nojs__rowTitle"><div><h4>FOMC Meetings</h4></div></div>
<div class="row">
  <div class="col-xs-2">2:00 p.m.</div>
  <div class="col-xs-7"><p>FOMC Meeting</p><p>Two-day meeting</p></div>
  <div class="col-xs-3">16</div>
</div>
<div class="row">
  <div class="col-xs-2">2:30 p.m.</div>
  <div class="col-xs-7"><p>FOMC Press Conference</p></div>
  <div class="col-xs-3">16</div>
</div>
<div class="row">
  <div class="col-xs-2">2:00 p.m.</div>
  <div class="col-xs-7"><p>FOMC Minutes</p><p>Meeting of July 28-29</p></div>
  <div class="col-xs-3">19</div>
</div>
<div class="row cal-nojs__rowTitle"><div><h4>Speeches</h4></div></div>
<div class="row">
  <div class="col-xs-2">8:30 a.m.</div>
  <div class="col-xs-7"><p>Speech - Chair Example</p><p class="calendar__title">Economic Outlook</p></div>
  <div class="col-xs-3">3</div>
</div>
</body></html>"""

BEA_JSON_FIXTURE = {
    "file_last_updated": "2026-01-05 09:31:00",
    "Gross Domestic Product": {
        "release_dates": ["2026-01-29T13:30:00+00:00"],
    },
    "Personal Income and Outlays": {
        "release_dates": ["2026-01-29T13:30:00+00:00"],
    },
}


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def _event(
    event_type: MacroEventType,
    scheduled_at: datetime,
    *,
    source: str = "bls",
    source_event_id: str | None = None,
    institution: str | None = None,
    verification: MacroVerificationStatus = MacroVerificationStatus.VERIFIED,
) -> MacroEvent:
    event_name = event_type.value
    source_id = source_event_id or f"{event_name}|{scheduled_at.isoformat()}"
    return MacroEvent(
        event_id=build_event_id(
            source=source,
            source_event_id=source_id,
            normalized_event_type=event_type,
            source_event_name=event_name,
            scheduled_at_utc=scheduled_at,
        ),
        source=source,
        source_event_id=source_id,
        source_event_name=event_name,
        normalized_event_type=event_type,
        scheduled_at_utc=scheduled_at,
        source_timezone="America/New_York" if source != "bea" else "UTC",
        event_category=EVENT_CATEGORY_BY_TYPE[event_type],
        institution=institution or source.upper(),
        research_priority=RESEARCH_PRIORITY_BY_TYPE[event_type],
        verification_status=verification,
    )


def _health(
    source: str,
    *,
    status: MacroSourceStatus = MacroSourceStatus.VERIFIED,
    fetched_at: datetime = NOW,
    reason: str | None = None,
) -> MacroSourceHealth:
    verification = (
        MacroVerificationStatus.VERIFIED
        if status in (MacroSourceStatus.VERIFIED, MacroSourceStatus.PARTIAL)
        else MacroVerificationStatus.UNVERIFIED
    )
    return MacroSourceHealth(
        source=source,
        source_url=f"https://official.example/{source}",
        status=status,
        verification=verification,
        fetched_at=fetched_at,
        event_count=1 if status != MacroSourceStatus.UNAVAILABLE else 0,
        reason=reason,
        calendar_version="fixture-v1",
    )


def _observation(
    source: str,
    events: tuple[MacroEvent, ...],
    *,
    fetched_at: datetime,
    status: MacroSourceStatus = MacroSourceStatus.VERIFIED,
    version: str = "fixture-v1",
    request_count: int = 1,
) -> MacroProviderObservation:
    return MacroProviderObservation(
        source=source,
        source_url=f"https://official.example/{source}",
        fetched_at=fetched_at,
        events=events,
        status=status,
        calendar_version=version,
        request_count=request_count,
    )


class _FakeProvider:
    def __init__(self, source: str, outcomes: list[Any]) -> None:
        self.source = source
        self.source_url = f"https://official.example/{source}"
        self.outcomes = list(outcomes)
        self.calls = 0

    async def fetch(self) -> MacroProviderObservation:
        self.calls += 1
        index = min(self.calls - 1, len(self.outcomes) - 1)
        outcome = self.outcomes[index]
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome



def test_bls_calendar_valid_event_parse_and_explicit_normalization() -> None:
    events, warnings = parse_bls_ics(BLS_ICS_FIXTURE)
    by_id = {event.source_event_id: event for event in events}

    assert warnings == ()
    assert by_id["cpi-2026-01"].normalized_event_type == MacroEventType.US_CPI
    assert by_id["ppi-2026-07"].normalized_event_type == MacroEventType.US_PPI
    assert (
        by_id["empsit-2026-01"].normalized_event_type
        == MacroEventType.US_EMPLOYMENT_SITUATION
    )
    assert by_id["jolts-2026-01"].normalized_event_type == MacroEventType.US_JOLTS
    assert by_id["eci-2026-01"].normalized_event_type == MacroEventType.US_ECI
    assert (
        by_id["other-2026-01"].normalized_event_type
        == MacroEventType.OTHER_OFFICIAL_MACRO_EVENT
    )


def test_bls_timezone_conversion_covers_est_edt_and_dst_boundary() -> None:
    events, _ = parse_bls_ics(BLS_ICS_FIXTURE)
    by_id = {event.source_event_id: event for event in events}
    assert by_id["cpi-2026-01"].scheduled_at_utc == datetime(
        2026, 1, 15, 13, 30, tzinfo=UTC
    )
    assert by_id["ppi-2026-07"].scheduled_at_utc == datetime(
        2026, 7, 15, 12, 30, tzinfo=UTC
    )

    boundary = """BEGIN:VCALENDAR
BEGIN:VEVENT
UID:before-dst
SUMMARY:Consumer Price Index before DST
DTSTART;TZID=America/New_York:20260306T083000
END:VEVENT
BEGIN:VEVENT
UID:after-dst
SUMMARY:Consumer Price Index after DST
DTSTART;TZID=America/New_York:20260309T083000
END:VEVENT
END:VCALENDAR"""
    boundary_events, warnings = parse_bls_ics(boundary)
    assert warnings == ()
    assert tuple(event.scheduled_at_utc.hour for event in boundary_events) == (13, 12)


def test_naive_datetime_cannot_be_verified() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _event(MacroEventType.US_CPI, datetime(2026, 1, 15, 8, 30))

    events, warnings, _ = parse_bea_release_dates(
        {
            "Gross Domestic Product": {"release_dates": ["2026-01-29T08:30:00"]},
            "Personal Income and Outlays": {"release_dates": []},
        }
    )
    assert events == ()
    assert any("timezone-aware" in warning for warning in warnings)


def test_federal_reserve_statement_press_conference_minutes_and_speech_parse() -> None:
    events, warnings = parse_federal_reserve_month_html(
        FED_HTML_FIXTURE,
        year=2026,
        month_name="September",
        source_url="https://www.federalreserve.gov/newsevents/2026-september.htm",
    )
    by_type = {event.normalized_event_type: event for event in events}

    assert warnings == ()
    assert set(by_type) == {
        MacroEventType.FOMC_STATEMENT,
        MacroEventType.FOMC_PRESS_CONFERENCE,
        MacroEventType.FOMC_MINUTES,
        MacroEventType.FED_SPEECH,
    }
    assert by_type[MacroEventType.FOMC_STATEMENT].scheduled_at_utc == datetime(
        2026, 9, 16, 18, 0, tzinfo=UTC
    )
    assert by_type[MacroEventType.FOMC_PRESS_CONFERENCE].scheduled_at_utc == datetime(
        2026, 9, 16, 18, 30, tzinfo=UTC
    )
    assert by_type[MacroEventType.FOMC_MINUTES].research_priority == ResearchPriority.TIER_2
    assert by_type[MacroEventType.FED_SPEECH].speaker == "Chair Example"
    assert by_type[MacroEventType.FED_SPEECH].title == "Economic Outlook"
    assert by_type[MacroEventType.FED_SPEECH].research_priority == ResearchPriority.NEUTRAL


def test_bea_gdp_and_pce_are_separate_simultaneous_events() -> None:
    events, warnings, version = parse_bea_release_dates(BEA_JSON_FIXTURE)

    assert warnings == ()
    assert version == "file_last_updated:2026-01-05 09:31:00"
    assert tuple(event.normalized_event_type for event in events) == (
        MacroEventType.US_GDP,
        MacroEventType.US_PCE,
    )
    assert events[0].scheduled_at_utc == events[1].scheduled_at_utc
    snapshot = build_macro_event_snapshot(
        generated_at=NOW,
        events=events,
        sources=(_health("bea"),),
    )
    assert len(snapshot.events) == 2


def test_unknown_event_mapping_is_deterministic_not_arbitrary_substring_matching() -> None:
    assert (
        normalize_event_type("bls", "A CPI-like experimental table")
        == MacroEventType.OTHER_OFFICIAL_MACRO_EVENT
    )
    assert (
        normalize_event_type("bea", "GDP and Personal Income summary")
        == MacroEventType.OTHER_OFFICIAL_MACRO_EVENT
    )
    assert (
        normalize_event_type("federal_reserve", "Community development discussion")
        == MacroEventType.OTHER_OFFICIAL_MACRO_EVENT
    )


def test_official_providers_make_five_mocked_gets_per_full_refresh() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if str(request.url) == BLS_CALENDAR_URL:
            return httpx.Response(200, text=BLS_ICS_FIXTURE, request=request)
        if str(request.url) == BEA_RELEASE_DATES_URL:
            return httpx.Response(200, json=BEA_JSON_FIXTURE, request=request)
        if str(request.url).startswith("https://www.federalreserve.gov/newsevents/"):
            return httpx.Response(200, text=FED_HTML_FIXTURE, request=request)
        return httpx.Response(404, request=request)

    async def scenario() -> tuple[MacroProviderObservation, ...]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            clock = lambda: NOW
            providers = (
                BlsCalendarProvider(http_client=client, clock=clock),
                FederalReserveCalendarProvider(http_client=client, clock=clock),
                BeaCalendarProvider(http_client=client, clock=clock),
            )
            return tuple([await provider.fetch() for provider in providers])

    observations = _run(scenario())
    assert len(calls) == 5
    assert tuple(item.request_count for item in observations) == (1, 3, 1)
    assert all(item.status == MacroSourceStatus.VERIFIED for item in observations)
    expected_fed_urls = {
        FED_MONTH_URL_TEMPLATE.format(year=2025, month="december"),
        FED_MONTH_URL_TEMPLATE.format(year=2026, month="january"),
        FED_MONTH_URL_TEMPLATE.format(year=2026, month="february"),
    }
    assert expected_fed_urls.issubset(calls)


def test_nearest_proximity_counts_and_pre_event_window_are_exact() -> None:
    recent = _event(MacroEventType.US_PPI, NOW - timedelta(hours=2))
    cpi = _event(MacroEventType.US_CPI, NOW + timedelta(minutes=45))
    snapshot = build_macro_event_snapshot(
        generated_at=NOW,
        events=(cpi, recent),
        sources=(_health("bls"),),
    )

    assert snapshot.nearest_upcoming_event.normalized_event_type == MacroEventType.US_CPI
    assert snapshot.nearest_upcoming_event.seconds_until_event == 2700
    assert snapshot.nearest_upcoming_event.minutes_until_event == 45
    assert snapshot.nearest_recent_event.normalized_event_type == MacroEventType.US_PPI
    assert snapshot.nearest_recent_event.seconds_since_event == 7200
    assert snapshot.events_next_24h == snapshot.events_next_6h == snapshot.events_next_1h == 1
    assert snapshot.inside_event_window is True
    assert snapshot.window_phase == MacroWindowPhase.PRE_EVENT


def test_post_active_and_no_event_window_scenarios_are_exact() -> None:
    post = build_macro_event_snapshot(
        generated_at=NOW,
        events=(_event(MacroEventType.US_CPI, NOW - timedelta(minutes=15)),),
        sources=(_health("bls"),),
    )
    active = build_macro_event_snapshot(
        generated_at=NOW,
        events=(_event(MacroEventType.FOMC_STATEMENT, NOW),),
        sources=(_health("federal_reserve"),),
    )
    quiet = build_macro_event_snapshot(
        generated_at=NOW,
        events=(_event(MacroEventType.US_CPI, NOW + timedelta(hours=25)),),
        sources=(_health("bls"),),
    )

    assert post.window_phase == MacroWindowPhase.POST_EVENT
    assert post.nearest_recent_event.seconds_since_event == 900
    assert active.window_phase == MacroWindowPhase.ACTIVE_EVENT
    assert active.nearest_upcoming_event.seconds_until_event == 0
    assert quiet.events_next_24h == 0
    assert quiet.inside_event_window is False


def test_fomc_statement_and_press_conference_remain_separate_and_nearest_wins() -> None:
    statement = _event(
        MacroEventType.FOMC_STATEMENT,
        NOW + timedelta(minutes=20),
        source="federal_reserve",
    )
    press = _event(
        MacroEventType.FOMC_PRESS_CONFERENCE,
        NOW + timedelta(minutes=50),
        source="federal_reserve",
    )
    snapshot = build_macro_event_snapshot(
        generated_at=NOW,
        events=(press, statement),
        sources=(_health("federal_reserve"),),
    )

    assert len(snapshot.events) == 2
    assert snapshot.window_event.normalized_event_type == MacroEventType.FOMC_STATEMENT
    assert snapshot.window_event.minutes_until_event == 20


def test_simultaneous_distinct_bls_events_remain_and_exact_duplicate_is_removed() -> None:
    event_time = NOW + timedelta(hours=1)
    nfp = _event(MacroEventType.US_EMPLOYMENT_SITUATION, event_time)
    cpi = _event(MacroEventType.US_CPI, event_time)
    duplicate_nfp = nfp.model_copy(update={"event_id": "duplicate-id"})
    snapshot = build_macro_event_snapshot(
        generated_at=NOW,
        events=(nfp, cpi, duplicate_nfp),
        sources=(_health("bls"),),
    )

    assert len(snapshot.events) == 2
    assert {item.normalized_event_type for item in snapshot.events} == {
        MacroEventType.US_EMPLOYMENT_SITUATION,
        MacroEventType.US_CPI,
    }


@pytest.mark.parametrize("failed_source", ["bls", "federal_reserve", "bea"])
def test_one_provider_failure_does_not_discard_other_official_events(
    failed_source: str,
) -> None:
    sources = ("bls", "federal_reserve", "bea")
    providers = []
    for index, source in enumerate(sources):
        if source == failed_source:
            outcomes: list[Any] = [RuntimeError(f"synthetic {source} timeout")]
        else:
            event_type = (
                MacroEventType.US_CPI
                if source == "bls"
                else MacroEventType.FOMC_STATEMENT
                if source == "federal_reserve"
                else MacroEventType.US_GDP
            )
            outcomes = [
                _observation(
                    source,
                    (_event(event_type, NOW + timedelta(hours=index + 1), source=source),),
                    fetched_at=NOW,
                )
            ]
        providers.append(_FakeProvider(source, outcomes))

    snapshot = _run(MacroCalendarService(providers, clock=lambda: NOW).snapshot(as_of=NOW))
    assert snapshot.calendar_status == MacroCalendarStatus.PARTIAL
    assert len(snapshot.events) == 2
    assert next(item for item in snapshot.sources if item.source == failed_source).status == (
        MacroSourceStatus.UNAVAILABLE
    )


def test_all_sources_unavailable_returns_snapshot_and_scanner_can_continue() -> None:
    providers = [
        _FakeProvider(source, [RuntimeError("synthetic outage")])
        for source in ("bls", "federal_reserve", "bea")
    ]
    snapshot = _run(MacroCalendarService(providers, clock=lambda: NOW).snapshot(as_of=NOW))

    assert snapshot.calendar_status == MacroCalendarStatus.UNAVAILABLE
    assert snapshot.events == ()
    assert snapshot.symbol_context() is None


def test_recent_cache_becomes_stale_then_unavailable_beyond_tolerance() -> None:
    clock = [NOW]
    event = _event(MacroEventType.US_CPI, NOW + timedelta(hours=1))
    provider = _FakeProvider(
        "bls",
        [
            _observation("bls", (event,), fetched_at=NOW),
            RuntimeError("synthetic timeout"),
            RuntimeError("synthetic timeout"),
        ],
    )
    service = MacroCalendarService(
        (provider,),
        cache_ttl_seconds=60,
        max_stale_seconds=180,
        snapshot_lookahead_seconds=24 * 60 * 60,
        clock=lambda: clock[0],
    )

    verified = _run(service.snapshot(as_of=clock[0]))
    clock[0] = NOW + timedelta(seconds=90)
    stale = _run(service.snapshot(as_of=clock[0]))
    clock[0] = NOW + timedelta(seconds=181)
    unavailable = _run(service.snapshot(as_of=clock[0]))

    assert verified.calendar_status == MacroCalendarStatus.VERIFIED
    assert stale.calendar_status == MacroCalendarStatus.STALE
    assert stale.events[0].verification_status == MacroVerificationStatus.UNVERIFIED
    assert stale.sources[0].fetched_at == NOW
    assert unavailable.calendar_status == MacroCalendarStatus.UNAVAILABLE
    assert unavailable.events == ()


def test_cache_hit_refresh_after_ttl_and_calendar_revision_replaces_old_schedule() -> None:
    clock = [NOW]
    old = _event(
        MacroEventType.US_CPI,
        NOW + timedelta(hours=1),
        source_event_id="stable-cpi-release",
    )
    revised = _event(
        MacroEventType.US_CPI,
        NOW + timedelta(hours=2),
        source_event_id="stable-cpi-release",
    )
    provider = _FakeProvider(
        "bls",
        [
            _observation("bls", (old,), fetched_at=NOW, version="v1"),
            _observation(
                "bls",
                (revised,),
                fetched_at=NOW + timedelta(seconds=61),
                version="v2",
            ),
        ],
    )
    service = MacroCalendarService(
        (provider,),
        cache_ttl_seconds=60,
        max_stale_seconds=600,
        clock=lambda: clock[0],
    )

    first = _run(service.snapshot(as_of=clock[0]))
    clock[0] = NOW + timedelta(seconds=30)
    hit = _run(service.snapshot(as_of=clock[0]))
    clock[0] = NOW + timedelta(seconds=61)
    refreshed = _run(service.snapshot(as_of=clock[0]))

    assert provider.calls == 2
    assert first.cache_hit is False
    assert hit.cache_hit is True
    assert hit.provider_requests == 0
    assert refreshed.cache_hit is False
    assert refreshed.events == (revised,)
    assert old not in refreshed.events
    assert refreshed.sources[0].calendar_version == "v2"


def test_provider_contract_mismatch_is_isolated_as_unavailable() -> None:
    mismatch = _FakeProvider(
        "bls",
        [_observation("bea", (), fetched_at=NOW)],
    )
    valid = _FakeProvider(
        "bea",
        [
            _observation(
                "bea",
                (_event(MacroEventType.US_GDP, NOW + timedelta(hours=2), source="bea"),),
                fetched_at=NOW,
            )
        ],
    )
    snapshot = _run(MacroCalendarService((mismatch, valid), clock=lambda: NOW).snapshot(as_of=NOW))

    assert snapshot.calendar_status == MacroCalendarStatus.PARTIAL
    assert len(snapshot.events) == 1
    assert next(item for item in snapshot.sources if item.source == "bls").status == (
        MacroSourceStatus.UNAVAILABLE
    )


def _symbol_result(symbol: str = "BTCUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejection_reason="fixture_no_setup",
        missing_data=("event_risk_context: N/A", "narrative: N/A"),
        strategy_missing_data=("event_risk_context: N/A",),
    )


def test_verified_partial_unavailable_and_stale_data_health_semantics() -> None:
    event = _event(MacroEventType.US_CPI, NOW + timedelta(minutes=45))
    verified = build_macro_event_snapshot(
        generated_at=NOW,
        events=(event,),
        sources=(_health("bls"),),
    )
    partial = build_macro_event_snapshot(
        generated_at=NOW,
        events=(event,),
        sources=(
            _health("bls"),
            _health("bea", status=MacroSourceStatus.UNAVAILABLE, reason="timeout"),
        ),
    )
    unavailable = build_macro_event_snapshot(
        generated_at=NOW,
        events=(),
        sources=(
            _health("bls", status=MacroSourceStatus.UNAVAILABLE, reason="timeout"),
        ),
    )
    stale_event = event.model_copy(
        update={"verification_status": MacroVerificationStatus.UNVERIFIED}
    )
    stale = build_macro_event_snapshot(
        generated_at=NOW,
        events=(stale_event,),
        sources=(
            _health(
                "bls",
                status=MacroSourceStatus.STALE,
                fetched_at=NOW - timedelta(hours=1),
                reason="timeout",
            ),
        ),
    )

    verified_result = apply_macro_event_context_to_symbol_result(_symbol_result(), verified)
    partial_result = apply_macro_event_context_to_symbol_result(_symbol_result(), partial)
    unavailable_result = apply_macro_event_context_to_symbol_result(_symbol_result(), unavailable)
    stale_result = apply_macro_event_context_to_symbol_result(_symbol_result(), stale)

    assert verified_result.event_risk_context.status == MacroVerificationStatus.VERIFIED
    assert "event_risk_context" not in confirmed_data_health_for_symbol(verified_result).optional_missing
    assert partial_result.event_risk_context.status == MacroVerificationStatus.VERIFIED
    assert partial_result.event_risk_context.calendar_status == MacroCalendarStatus.PARTIAL
    assert partial_result.event_risk_context.degraded_sources == ("bea",)
    assert "event_risk_context" not in confirmed_data_health_for_symbol(partial_result).optional_unverified
    assert unavailable_result.event_risk_context is None
    assert "event_risk_context" in confirmed_data_health_for_symbol(unavailable_result).optional_missing
    assert stale_result.event_risk_context.status == MacroVerificationStatus.UNVERIFIED
    assert "event_risk_context" in confirmed_data_health_for_symbol(stale_result).optional_unverified
    assert confirmed_data_health_for_symbol(stale_result).blocked is False


def test_research_only_context_cannot_change_strategy_gates_grade_rr_or_geometry() -> None:
    engine = LiquidityGrabEngine()
    baseline = engine.analyze({"symbol": "DOGEUSDT", "mode": "challenge"})
    research = engine.analyze(
        {
            "symbol": "DOGEUSDT",
            "mode": "challenge",
            "event_risk_context": {
                "usage": "research_only",
                "status": "VERIFIED",
                "nearest_event": "US_CPI",
                "minutes_until_event": 20,
                "inside_event_window": True,
                "window_phase": "PRE_EVENT",
            },
        }
    )

    for mode in ("challenge", "swing", "scalp"):
        before = getattr(baseline, mode)
        after = getattr(research, mode)
        assert after.is_valid == before.is_valid
        assert after.status == before.status
        assert after.gates_passed == before.gates_passed
        assert after.gates_failed == before.gates_failed
        assert after.hard_rejection_reasons == before.hard_rejection_reasons
        assert after.trust_meter == before.trust_meter
        assert after.entry_low == before.entry_low
        assert after.entry_high == before.entry_high
        assert after.stop == before.stop
        assert after.tp1 == before.tp1
        assert after.tp2 == before.tp2
        assert after.tp3 == before.tp3
        assert after.rr_to_tp2 == before.rr_to_tp2


def test_post_decision_enrichment_changes_no_strategy_lifecycle_or_telegram_eligibility() -> None:
    before = _symbol_result()
    snapshot = build_macro_event_snapshot(
        generated_at=NOW,
        events=(_event(MacroEventType.US_CPI, NOW + timedelta(minutes=45)),),
        sources=(_health("bls"),),
    )
    eligibility_before = public_watchlist_eligible(before)
    after = apply_macro_event_context_to_symbol_result(before, snapshot)

    assert after.strategy_results == before.strategy_results
    assert after.score_result == before.score_result
    assert after.trade_idea == before.trade_idea
    assert after.lifecycle_state == before.lifecycle_state
    assert after.lifecycle_transition == before.lifecycle_transition
    assert after.valid_strategy_modes == before.valid_strategy_modes
    assert after.rejected_strategy_modes == before.rejected_strategy_modes
    assert public_watchlist_eligible(after) == eligibility_before


def test_unknown_future_data_health_field_still_fails_closed() -> None:
    report = classify_confirmed_data_health(
        missing_values=(("future_macro_magic: N/A",),)
    )

    assert report.required_missing == ("future_macro_magic",)
    assert report.blocked is True


class _NoMarketDataClient:
    cache_stats = None
    retry_events = None

    def __init__(self) -> None:
        self.calls = 0

    def __getattr__(self, name: str) -> Any:
        self.calls += 1
        raise AssertionError(f"macro context made a per-symbol market-data call: {name}")


class _StubScannerRunner(ScannerRunner):
    async def _scan_symbol(self, symbol_config, config, client, **kwargs):
        del config, client, kwargs
        return _symbol_result(symbol_config.symbol)


def _scanner_config(symbols: list[str]) -> ScannerRunConfig:
    return ScannerRunConfig(
        symbols=symbols,
        exchange="binance",
        interval="15m",
        cache_enabled=False,
        account_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        decision_timestamp=NOW,
        macro_event_context_enabled=True,
    )


def test_repeated_symbols_share_one_run_snapshot_and_add_no_provider_calls() -> None:
    event = _event(MacroEventType.US_CPI, NOW + timedelta(minutes=45))
    provider = _FakeProvider(
        "bls",
        [_observation("bls", (event,), fetched_at=NOW)],
    )
    service = MacroCalendarService((provider,), clock=lambda: NOW)
    market_client = _NoMarketDataClient()
    result = _run(
        _StubScannerRunner(
            exchange_client=market_client,
            macro_calendar_service=service,
        ).run(_scanner_config(["BTCUSDT", "ETHUSDT", "SOLUSDT"]))
    )

    assert provider.calls == 1
    assert market_client.calls == 0
    assert result.macro_event_snapshot is not None
    assert result.macro_event_snapshot.provider_requests == 1
    assert all(item.event_risk_context is not None for item in result.results)
    assert result.trade_ideas_created == 0
    assert result.dry_run_alerts_created == 0


def test_snapshot_and_per_symbol_persistence_are_compact_and_contain_no_raw_payloads() -> None:
    events = tuple(
        _event(event_type, NOW + timedelta(hours=index + 1), source=source)
        for index, (event_type, source) in enumerate(
            (
                (MacroEventType.US_CPI, "bls"),
                (MacroEventType.US_PPI, "bls"),
                (MacroEventType.US_EMPLOYMENT_SITUATION, "bls"),
                (MacroEventType.US_GDP, "bea"),
                (MacroEventType.US_PCE, "bea"),
                (MacroEventType.FOMC_STATEMENT, "federal_reserve"),
                (MacroEventType.FOMC_PRESS_CONFERENCE, "federal_reserve"),
            )
        )
    )
    snapshot = build_macro_event_snapshot(
        generated_at=NOW,
        events=events,
        sources=(_health("bls"), _health("federal_reserve"), _health("bea")),
        provider_requests=5,
    )
    results = tuple(
        apply_macro_event_context_to_symbol_result(_symbol_result(symbol), snapshot)
        for symbol in ("BTCUSDT", "ETHUSDT", "SOLUSDT")
    )
    run_result = ScannerRunResult(
        config=_scanner_config([item.symbol for item in results]),
        results=results,
        scanned_symbols=len(results),
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
        macro_event_snapshot=snapshot,
    )
    payload = _storage_payload(run_result, {}, None, None)
    snapshot_bytes = len(
        json.dumps(snapshot.model_dump(mode="json"), separators=(",", ":")).encode()
    )
    context_bytes = len(
        json.dumps(
            results[0].event_risk_context.model_dump(mode="json"),
            separators=(",", ":"),
        ).encode()
    )
    serialized = json.dumps(payload, separators=(",", ":"))

    assert snapshot_bytes < 20_000
    assert context_bytes < 1_000
    assert len(payload["macro_event_snapshot"]["events"]) == len(events)
    assert all("events" not in item["event_risk_context"] for item in payload["results"])
    assert "BEGIN:VCALENDAR" not in serialized
    assert "<!doctype html>" not in serialized.lower()

