from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import httpx

from app.agents.technical_structure import TechnicalStructureAgent
from app.context import (
    BtcDominanceContextService,
    BtcDominanceObservation,
    CoinPaprikaBtcDominanceProvider,
    ContextStatus,
    ContextValue,
    build_global_context_snapshot,
    build_internal_btc_context,
    build_weekend_context,
)


def run(coroutine):
    return asyncio.run(coroutine)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class FakeBtcDominanceProvider:
    source = "fake:btc_d"

    def __init__(self, observation: BtcDominanceObservation | None = None) -> None:
        self.observation = observation
        self.error: Exception | None = None
        self.calls = 0

    async def get_snapshot(self) -> BtcDominanceObservation:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if self.observation is None:
            raise RuntimeError("no fake observation")
        return self.observation


def _observation(at: datetime, dominance: str = "57.25") -> BtcDominanceObservation:
    return BtcDominanceObservation(
        btc_dominance_pct=Decimal(dominance),
        observed_at=at,
        source="fake:btc_d",
    )


def _candles(*, timeframe: str, count: int, decision_at: datetime) -> list[dict[str, object]]:
    duration = {
        "15m": timedelta(minutes=15),
        "2h": timedelta(hours=2),
        "12h": timedelta(hours=12),
    }[timeframe]
    start = decision_at - (duration * count)
    rows: list[dict[str, object]] = []
    for index in range(count):
        close = Decimal("100") + Decimal(index) / Decimal("10")
        rows.append(
            {
                "timestamp": int((start + duration * index).timestamp() * 1000),
                "open": close - Decimal("0.2"),
                "high": close + Decimal("1"),
                "low": close - Decimal("1"),
                "close": close,
                "volume": Decimal("1000") + index,
            }
        )
    return rows


def test_weekend_context_is_true_on_saturday_utc() -> None:
    context = build_weekend_context(datetime(2026, 8, 29, 12, tzinfo=UTC))

    assert context.status == ContextStatus.VERIFIED
    assert context.value.is_weekend is True
    assert context.value.utc_weekday == 5
    assert context.value.session_label == "weekend"


def test_weekend_context_is_true_on_sunday_utc() -> None:
    context = build_weekend_context(datetime(2026, 8, 30, 12, tzinfo=UTC))

    assert context.value.is_weekend is True
    assert context.value.utc_weekday == 6


def test_weekend_context_is_false_on_friday_utc() -> None:
    context = build_weekend_context(datetime(2026, 8, 28, 12, tzinfo=UTC))

    assert context.value.is_weekend is False
    assert context.value.utc_weekday == 4
    assert context.value.session_label == "europe"


def test_weekend_context_uses_utc_boundary_not_source_offset() -> None:
    friday = build_weekend_context(datetime.fromisoformat("2026-08-28T23:59:59+00:00"))
    saturday = build_weekend_context(datetime.fromisoformat("2026-08-29T02:00:00+02:00"))

    assert friday.value.is_weekend is False
    assert saturday.value.is_weekend is True
    assert saturday.observed_at == datetime(2026, 8, 29, 0, 0, tzinfo=UTC)


def test_internal_btc_context_uses_existing_verified_analysis() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    candles_by_timeframe = {
        timeframe: _candles(timeframe=timeframe, count=220, decision_at=now)
        for timeframe in ("12h", "2h", "15m")
    }
    epoch_ms = int(now.timestamp() * 1000)

    context = build_internal_btc_context(
        candles_by_timeframe=candles_by_timeframe,
        generated_at=now,
        technical_agent=TechnicalStructureAgent(),
        exchange="binance",
        funding={"funding_rate": "0.0001", "timestamp": epoch_ms},
        open_interest={"open_interest": "105", "timestamp": epoch_ms},
        open_interest_history=(
            {"open_interest": "100", "timestamp": epoch_ms - 300_000},
            {"open_interest": "105", "timestamp": epoch_ms},
        ),
    )

    assert context.status == ContextStatus.VERIFIED
    assert context.value.symbol == "BTCUSDT"
    assert context.value.bias_12h.value == "bullish"
    assert context.value.structure_2h.value == "bullish"
    assert context.value.execution_15m.value == "bullish"
    assert context.value.atr_15m.value > 0
    assert context.value.atr_pct_15m.value > 0
    assert context.value.funding_rate.value == Decimal("0.0001")
    assert context.value.open_interest.value == Decimal("105")
    assert context.value.open_interest_change_pct.value == Decimal("5.00000000")


def test_internal_btc_context_preserves_verified_components_when_optional_data_missing() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    context = build_internal_btc_context(
        candles_by_timeframe={
            timeframe: _candles(timeframe=timeframe, count=220, decision_at=now)
            for timeframe in ("12h", "2h", "15m")
        },
        generated_at=now,
        technical_agent=TechnicalStructureAgent(),
        exchange="binance",
        funding=None,
        open_interest={"open_interest": "105", "timestamp": int(now.timestamp())},
        open_interest_history=None,
    )

    assert context.status == ContextStatus.VERIFIED
    assert context.value.bias_12h.status == ContextStatus.VERIFIED
    assert context.value.funding_rate.status == ContextStatus.UNAVAILABLE
    assert context.value.open_interest.status == ContextStatus.VERIFIED
    assert context.value.open_interest_change_pct.status == ContextStatus.UNAVAILABLE
    assert "component(s) unavailable" in context.reason


def test_funding_timestamp_controls_observation_time_and_freshness() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    fresh_at = now - timedelta(hours=1)
    stale_at = now - timedelta(hours=25)

    fresh = build_internal_btc_context(
        candles_by_timeframe={},
        generated_at=now,
        technical_agent=TechnicalStructureAgent(),
        exchange="binance",
        funding={"funding_rate": "0.0001", "fundingTime": int(fresh_at.timestamp() * 1000)},
    ).value.funding_rate
    stale = build_internal_btc_context(
        candles_by_timeframe={},
        generated_at=now,
        technical_agent=TechnicalStructureAgent(),
        exchange="binance",
        funding={"funding_rate": "0.0001", "timestamp": int(stale_at.timestamp())},
    ).value.funding_rate

    assert fresh.observed_at == fresh_at
    assert fresh.age_seconds == 3600
    assert fresh.status == ContextStatus.VERIFIED
    assert stale.observed_at == stale_at
    assert stale.age_seconds == 25 * 60 * 60
    assert stale.status == ContextStatus.STALE


def test_funding_without_timestamp_never_invents_scan_time_or_verified_freshness() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)

    funding = build_internal_btc_context(
        candles_by_timeframe={},
        generated_at=now,
        technical_agent=TechnicalStructureAgent(),
        exchange="binance",
        funding={"funding_rate": "0.0001"},
    ).value.funding_rate

    assert funding.value == Decimal("0.0001")
    assert funding.observed_at is None
    assert funding.age_seconds is None
    assert funding.status == ContextStatus.UNAVAILABLE
    assert funding.status != ContextStatus.VERIFIED
    assert "timestamp unavailable" in funding.reason


def test_open_interest_timestamp_controls_normal_freshness() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    observed_at = now - timedelta(minutes=10)

    open_interest = build_internal_btc_context(
        candles_by_timeframe={},
        generated_at=now,
        technical_agent=TechnicalStructureAgent(),
        exchange="binance",
        open_interest={
            "open_interest": "105",
            "time": int(observed_at.timestamp() * 1000),
        },
    ).value.open_interest

    assert open_interest.value == Decimal("105")
    assert open_interest.observed_at == observed_at
    assert open_interest.age_seconds == 600
    assert open_interest.status == ContextStatus.VERIFIED


def test_open_interest_without_timestamp_has_unknown_non_verified_freshness() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)

    open_interest = build_internal_btc_context(
        candles_by_timeframe={},
        generated_at=now,
        technical_agent=TechnicalStructureAgent(),
        exchange="binance",
        open_interest={"open_interest": "105"},
    ).value.open_interest

    assert open_interest.value == Decimal("105")
    assert open_interest.observed_at is None
    assert open_interest.age_seconds is None
    assert open_interest.status == ContextStatus.UNAVAILABLE
    assert open_interest.status != ContextStatus.VERIFIED


def test_open_interest_change_uses_actual_timestamped_observations() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    previous_at = now - timedelta(minutes=5)

    change = build_internal_btc_context(
        candles_by_timeframe={},
        generated_at=now,
        technical_agent=TechnicalStructureAgent(),
        exchange="binance",
        open_interest={"open_interest": "105", "timestamp": int(now.timestamp() * 1000)},
        open_interest_history=(
            {"open_interest": "95", "timestamp": int((previous_at - timedelta(minutes=5)).timestamp() * 1000)},
            {"open_interest": "100", "timestamp": int(previous_at.timestamp() * 1000)},
            {"open_interest": "104", "timestamp": int(now.timestamp() * 1000)},
        ),
    ).value.open_interest_change_pct

    assert change.value == Decimal("5.00000000")
    assert change.observed_at == now
    assert change.age_seconds == 0
    assert change.status == ContextStatus.VERIFIED


def test_open_interest_change_without_history_timestamp_never_claims_freshness() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)

    change = build_internal_btc_context(
        candles_by_timeframe={},
        generated_at=now,
        technical_agent=TechnicalStructureAgent(),
        exchange="binance",
        open_interest={"open_interest": "105", "timestamp": int(now.timestamp() * 1000)},
        open_interest_history=(
            {"open_interest": "95"},
            {"open_interest": "100"},
            {"open_interest": "104"},
        ),
    ).value.open_interest_change_pct

    assert change.value == Decimal("5.00000000")
    assert change.observed_at is None
    assert change.age_seconds is None
    assert change.status == ContextStatus.UNAVAILABLE
    assert change.status != ContextStatus.VERIFIED
    assert "timestamps unavailable" in change.reason


def test_untimestamped_derivatives_do_not_change_verified_candle_components() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)

    context = build_internal_btc_context(
        candles_by_timeframe={
            timeframe: _candles(timeframe=timeframe, count=220, decision_at=now)
            for timeframe in ("12h", "2h", "15m")
        },
        generated_at=now,
        technical_agent=TechnicalStructureAgent(),
        exchange="binance",
        funding={"funding_rate": "0.0001"},
        open_interest={"open_interest": "105"},
    )

    assert context.status == ContextStatus.VERIFIED
    assert context.value.bias_12h.value == "bullish"
    assert context.value.structure_2h.value == "bullish"
    assert context.value.execution_15m.value == "bullish"
    assert context.value.atr_15m.value > 0
    assert context.value.atr_pct_15m.value > 0
    assert context.value.funding_rate.status == ContextStatus.UNAVAILABLE
    assert context.value.open_interest.status == ContextStatus.UNAVAILABLE
    assert "component(s) unavailable" in context.reason


def test_coinpaprika_provider_normalizes_valid_public_payload_without_network() -> None:
    observed_at = datetime(2026, 8, 27, 12, tzinfo=UTC)

    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/global"
        return httpx.Response(
            200,
            json={
                "bitcoin_dominance_percentage": 58.125,
                "last_updated": int(observed_at.timestamp()),
            },
        )

    client = httpx.AsyncClient(
        base_url="https://api.coinpaprika.com/v1",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = run(CoinPaprikaBtcDominanceProvider(http_client=client).get_snapshot())
    finally:
        run(client.aclose())

    assert result.btc_dominance_pct == Decimal("58.125")
    assert result.observed_at == observed_at
    assert result.source == "coinpaprika:/v1/global"


def test_coinpaprika_malformed_payload_becomes_unavailable_without_crash() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"last_updated": 1_800_000_000})

    client = httpx.AsyncClient(
        base_url="https://api.coinpaprika.com/v1",
        transport=httpx.MockTransport(handler),
    )
    clock = MutableClock(datetime(2027, 1, 15, tzinfo=UTC))
    service = BtcDominanceContextService(
        CoinPaprikaBtcDominanceProvider(http_client=client),
        clock=clock,
    )
    try:
        context = run(service.get_context())
    finally:
        run(client.aclose())

    assert context.status == ContextStatus.UNAVAILABLE
    assert context.value is None
    assert "malformed" in context.reason.lower()


def test_btc_d_timeout_without_cache_is_unavailable_never_zero() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    provider = FakeBtcDominanceProvider()
    provider.error = TimeoutError("mock timeout")
    service = BtcDominanceContextService(provider, clock=MutableClock(now))

    context = run(service.get_context())

    assert context.status == ContextStatus.UNAVAILABLE
    assert context.value is None
    assert provider.calls == 1
    assert "mock timeout" in context.reason


def test_btc_d_cache_many_consumers_make_one_provider_call() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    provider = FakeBtcDominanceProvider(_observation(now))
    service = BtcDominanceContextService(provider, clock=MutableClock(now))

    async def consume_twice():
        return await asyncio.gather(service.get_context(), service.get_context())

    contexts = run(consume_twice())

    assert provider.calls == 1
    assert contexts[0].value.btc_dominance_pct == Decimal("57.25")
    assert contexts[1].cache_hit is True


def test_btc_d_ttl_expiry_refreshes_observation() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    clock = MutableClock(now)
    provider = FakeBtcDominanceProvider(_observation(now, "57"))
    service = BtcDominanceContextService(
        provider,
        cache_ttl_seconds=60,
        fresh_seconds=120,
        max_stale_seconds=600,
        clock=clock,
    )
    first = run(service.get_context())
    clock.value += timedelta(seconds=61)
    provider.observation = _observation(clock.value, "58")

    second = run(service.get_context())

    assert first.value.btc_dominance_pct == Decimal("57")
    assert second.value.btc_dominance_pct == Decimal("58")
    assert second.cache_hit is False
    assert provider.calls == 2


def test_btc_d_recent_cache_is_explicitly_stale_when_refresh_fails() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    clock = MutableClock(now)
    provider = FakeBtcDominanceProvider(_observation(now, "57"))
    service = BtcDominanceContextService(
        provider,
        cache_ttl_seconds=30,
        fresh_seconds=60,
        max_stale_seconds=600,
        clock=clock,
    )
    run(service.get_context())
    clock.value += timedelta(seconds=31)
    provider.error = TimeoutError("refresh timeout")

    context = run(service.get_context())

    assert context.status == ContextStatus.STALE
    assert context.cache_hit is True
    assert context.value.btc_dominance_pct == Decimal("57")
    assert "refresh timeout" in context.reason


def test_btc_d_cache_is_not_reused_beyond_max_stale_tolerance() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    clock = MutableClock(now)
    provider = FakeBtcDominanceProvider(_observation(now, "57"))
    service = BtcDominanceContextService(
        provider,
        cache_ttl_seconds=30,
        fresh_seconds=60,
        max_stale_seconds=120,
        clock=clock,
    )
    run(service.get_context())
    clock.value += timedelta(seconds=121)
    provider.error = RuntimeError("provider down")

    context = run(service.get_context())

    assert context.status == ContextStatus.UNAVAILABLE
    assert context.value is None
    assert provider.calls == 2


def test_global_context_snapshot_is_compact_and_research_payload_is_marked() -> None:
    now = datetime(2026, 8, 27, 12, tzinfo=UTC)
    btc = build_internal_btc_context(
        candles_by_timeframe={
            timeframe: _candles(timeframe=timeframe, count=220, decision_at=now)
            for timeframe in ("12h", "2h", "15m")
        },
        generated_at=now,
        technical_agent=TechnicalStructureAgent(),
        exchange="binance",
    )
    btc_d = ContextValue(
        value={"btc_dominance_pct": Decimal("57.25")},
        source="fake:btc_d",
        observed_at=now,
        age_seconds=0,
        status=ContextStatus.VERIFIED,
    )
    snapshot = build_global_context_snapshot(
        generated_at=now,
        btc_context=btc,
        btc_d_context=btc_d,
        weekend_context=build_weekend_context(now),
    )

    serialized = json.dumps(snapshot.model_dump(mode="json"), separators=(",", ":"))

    assert len(serialized.encode("utf-8")) < 8_192
    assert snapshot.strategy_context()["btc_context"]["usage"] == "research_only"
    assert "candles" not in serialized
