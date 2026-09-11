"""PROSPECTIVE_BATCH_DELIVERY_CAPTURE: in-memory batch observation and handoff.

Synthetic fixtures and mocked HTTP only. No live exchange calls and no live or
existing scan database is read. Local capture is not venue authenticity,
possession at cutoff, admission, or a durable source identity.

Evidence labels below are test documentation, not application enums.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.cache.market_data_cache import CachedMarketDataClient, MarketDataCache
from app.data.candle_batch_evidence import (
    CACHE_KIND_EXPIRY_REFETCH,
    CACHE_KIND_HIT,
    CACHE_KIND_MISS,
    CACHE_KIND_NOT_APPLICABLE,
    DISPOSITION_CAPTURED,
    DISPOSITION_UNAVAILABLE,
    HANDOFF_MATCHED,
    HANDOFF_MISMATCHED,
    HANDOFF_UNAVAILABLE,
    LIMIT_FILE_CACHE_ACQUISITION,
    LIMIT_POSSESSION_UNAVAILABLE,
    LIMIT_UNINSTRUMENTED_INSERT,
    PRODUCER_ADAPTER_NORMALIZATION_RETURN,
    PRODUCER_CLIENT_RETURN,
    PRODUCER_EMPTY_EXECUTION,
    PRODUCER_FILTER_SELECTION,
    PRODUCER_TRANSFORM_2D,
    associate_execution_handoff,
    client_offers_same_path_klines_delivery,
    fetch_klines_with_delivery,
    observe_unsupported_client_return,
    snapshot_candle_batch,
)
from app.data.dtos import NA, CandleDTO
from app.data.exceptions import ExchangeMalformedJSONError
from app.data.exchange_clients import BinanceFuturesClient
from app.data.timeframes import resample_ohlcv_candles
from app.lifecycle.service import SetupLifecycleService
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunner, ScannerSymbolResult
from app.research.evaluation_policy import build_runtime_evaluation_policy
from app.research.source_evidence import (
    BINDING_STATUS_UNBOUND,
    SUPPORT_UNAVAILABLE,
    assess_source_evidence,
    build_source_evidence_descriptor,
)
from app.storage.database import SCHEMA_VERSION, open_initialized_database
from app.storage.repositories import store_scan_result
from tests.test_evaluation_semantics_contract import (
    EVIDENCE_MALFORMED,
    EVIDENCE_NORMAL_PRODUCER,
    EVIDENCE_SOURCE_ONLY,
    EVIDENCE_SUPPLIED_STATE,
    TIMEFRAME,
    _both_entry,
    _close,
    _decision,
    _no_touch,
    _record,
)
from tests.test_scanner_runner import FakeExchangeClient, _config, _flat_candles

EVIDENCE_MIXED = "mixed planted-record plus ordinary service evaluation"
REPO_ROOT = Path(__file__).resolve().parents[1]
DAY_MS = 86_400_000
FIFTEEN_MIN_MS = 15 * 60_000


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class ControllableDatetimeClock:
    def __init__(self, current: datetime) -> None:
        self.current = current
        self.samples: list[datetime] = []

    def __call__(self) -> datetime:
        self.samples.append(self.current)
        return self.current

    def advance(self, seconds: int | float) -> None:
        self.current = self.current + timedelta(seconds=seconds)


class CacheClock:
    def __init__(self, current: float = 1_000.0) -> None:
        self.current = current

    def __call__(self) -> float:
        return self.current


def _scan_config(**overrides: object):
    data: dict[str, object] = {
        "cache_enabled": False,
        "btc_context_enabled": False,
        "btc_d_context_enabled": False,
        "global_context_enabled": False,
        "market_regime_enabled": False,
        "macro_event_context_enabled": False,
        "enable_strategy_output": False,
        "interval": "15m",
        "execution_timeframe": "15m",
        "candle_limit": 40,
    }
    data.update(overrides)
    return _config(["BTCUSDT"], **data)


def _kline_row(open_ms: int, *, duration_ms: int, close: str = "100.0") -> list[Any]:
    return [
        open_ms,
        "100.0",
        "101.0",
        "99.0",
        close,
        "10.0",
        open_ms + duration_ms - 1,
        "1000.0",
        3,
    ]


def _rows_for_interval(interval: str, limit: int, *, close_value: str = "100.0") -> list[list[Any]]:
    durations = {
        "1m": 60_000,
        "5m": 300_000,
        "15m": FIFTEEN_MIN_MS,
        "1h": 3_600_000,
        "4h": 14_400_000,
        "12h": 43_200_000,
        "1d": DAY_MS,
    }
    duration = durations[interval]
    start = 1_700_000_000_000
    return [_kline_row(start + index * duration, duration_ms=duration, close=close_value) for index in range(limit)]


def _decision_after_rows(interval: str, count: int) -> datetime:
    durations = {
        "1m": 60_000,
        "15m": FIFTEEN_MIN_MS,
        "1d": DAY_MS,
    }
    duration = durations[interval]
    last_close_ms = 1_700_000_000_000 + (count - 1) * duration + duration - 1
    return datetime.fromtimestamp((last_close_ms + 1) / 1000, tz=UTC)


async def _binance(handler: httpx.MockTransport) -> BinanceFuturesClient:
    http_client = httpx.AsyncClient(transport=handler, base_url="https://binance.test")
    return BinanceFuturesClient(
        http_client=http_client,
        retry_attempts=3,
        retry_base_delay=0,
        retry_max_delay=0,
    )


def _interval_handler(*, close_by_symbol: dict[str, str] | None = None, requests: list[httpx.Request] | None = None):
    captured = requests if requests is not None else []
    closes = close_by_symbol or {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        assert request.url.path == "/fapi/v1/klines"
        symbol = request.url.params["symbol"]
        interval = request.url.params["interval"]
        limit = int(request.url.params["limit"])
        close = closes.get(symbol, "100.0")
        return httpx.Response(200, json=_rows_for_interval(interval, limit, close_value=close))

    return handler


def test_binance_mocked_http_records_normalized_batch_and_clamped_params() -> None:
    requests: list[httpx.Request] = []

    async def scenario() -> None:
        client = await _binance(httpx.MockTransport(_interval_handler(requests=requests)))
        clock = ControllableDatetimeClock(datetime(2026, 1, 1, tzinfo=UTC))
        delivery = await client.get_klines_with_delivery(
            "btcusdt",
            "1m",
            2000,
            capture_clock=clock,
        )
        listed = await client.get_klines("btcusdt", "1m", 0)
        await client._http_client.aclose()
        assert delivery.producer_boundary == PRODUCER_ADAPTER_NORMALIZATION_RETURN
        assert delivery.adapter is not None
        assert delivery.adapter.requested_limit == 2000
        assert delivery.adapter.effective_limit == 1500
        assert delivery.adapter.requested_symbol == "btcusdt"
        assert delivery.adapter.effective_symbol == "BTCUSDT"
        assert delivery.adapter.endpoint_path == "/fapi/v1/klines"
        assert delivery.adapter.observed_completed_at == datetime(2026, 1, 1, tzinfo=UTC)
        assert delivery.snapshot.record_count == 1500
        assert delivery.snapshot.records[0].values["open"] == Decimal("100.0")
        assert "raw_source" not in delivery.snapshot.records[0].values
        assert listed[0].raw_source is not None
        assert delivery.disposition == DISPOSITION_CAPTURED
        assert requests[0].url.params["limit"] == "1500"
        assert requests[1].url.params["limit"] == "1"

    run(scenario())
    assert EVIDENCE_NORMAL_PRODUCER


def test_cache_disabled_and_legacy_injected_client_preserve_data() -> None:
    class Legacy:
        exchange_name = "legacy"

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
            return [{"symbol": symbol, "timestamp": 1, "interval": interval, "open": Decimal("1"), "high": Decimal("2"), "low": Decimal("0"), "close": Decimal("1"), "volume": Decimal("1")}]

    clock = ControllableDatetimeClock(datetime(2026, 1, 2, tzinfo=UTC))
    cache = MarketDataCache(enabled=False)
    wrapped = CachedMarketDataClient(Legacy(), cache)
    delivery = run(
        wrapped.get_klines_with_delivery("aaa", "1m", 1, capture_clock=clock)
    )
    listed = run(wrapped.get_klines("aaa", "1m", 1))
    assert cache.stats()["misses"] == 0
    assert cache.stats()["entries"] == 0
    assert listed[0]["close"] == Decimal("1")
    assert delivery.cache is not None
    assert delivery.cache.delivery_kind == CACHE_KIND_NOT_APPLICABLE
    assert delivery.adapter is None
    assert delivery.producer_boundary == PRODUCER_CLIENT_RETURN
    assert not client_offers_same_path_klines_delivery(Legacy())
    assert EVIDENCE_NORMAL_PRODUCER


def test_miss_hit_expiry_refetch_distinct_occurrences_equal_ohlc() -> None:
    payload = [
        {
            "symbol": "BTCUSDT",
            "timestamp": 1,
            "interval": "15m",
            "open": Decimal("100"),
            "high": Decimal("101"),
            "low": Decimal("99"),
            "close": Decimal("100"),
            "volume": Decimal("10"),
        }
    ]

    class Client:
        exchange_name = "fixture"
        calls = 0

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
            type(self).calls += 1
            return list(payload)

    cache_clock = CacheClock(1_000.0)
    capture = ControllableDatetimeClock(datetime(2026, 1, 3, tzinfo=UTC))
    cache = MarketDataCache(ttl_seconds=10, now=cache_clock)
    wrapped = CachedMarketDataClient(Client(), cache)
    miss = run(wrapped.get_klines_with_delivery("BTCUSDT", "15m", 1, capture_clock=capture))
    capture.advance(1)
    hit = run(wrapped.get_klines_with_delivery("BTCUSDT", "15m", 1, capture_clock=capture))
    cache_clock.current = 1_020.0
    capture.advance(1)
    refetch = run(wrapped.get_klines_with_delivery("BTCUSDT", "15m", 1, capture_clock=capture))
    assert miss.cache is not None and hit.cache is not None and refetch.cache is not None
    assert miss.cache.delivery_kind == CACHE_KIND_MISS
    assert hit.cache.delivery_kind == CACHE_KIND_HIT
    assert refetch.cache.delivery_kind == CACHE_KIND_EXPIRY_REFETCH
    assert miss.snapshot.content_fingerprint == hit.snapshot.content_fingerprint == refetch.snapshot.content_fingerprint
    assert len({miss.occurrence_token, hit.occurrence_token, refetch.occurrence_token}) == 3
    assert Client.calls == 2
    assert EVIDENCE_NORMAL_PRODUCER


def test_cache_bookkeeping_remains_prefetch_and_counters_unchanged() -> None:
    cache_clock = CacheClock(5_000.0)

    class Advancing:
        exchange_name = "fixture"

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
            cache_clock.current += 25.0
            return [{"symbol": symbol, "timestamp": 1, "interval": interval, "open": Decimal("1"), "high": Decimal("1"), "low": Decimal("1"), "close": Decimal("1"), "volume": Decimal("1")}]

    capture = ControllableDatetimeClock(datetime(2026, 1, 4, tzinfo=UTC))
    cache = MarketDataCache(ttl_seconds=60, now=cache_clock)
    wrapped = CachedMarketDataClient(Advancing(), cache)
    delivery = run(wrapped.get_klines_with_delivery("BTCUSDT", "15m", 1, capture_clock=capture))
    assert delivery.cache is not None
    assert delivery.cache.cache_bookkeeping_created_at == 5_000.0
    assert delivery.cache.observed_completed_at == datetime(2026, 1, 4, tzinfo=UTC)
    assert cache.stats()["misses"] == 1
    assert cache.stats()["hits"] == 0
    assert EVIDENCE_NORMAL_PRODUCER


def test_process_hit_retains_upstream_file_reload_does_not(tmp_path: Path) -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []
        client = await _binance(httpx.MockTransport(_interval_handler(requests=requests)))
        cache_path = tmp_path / "candles.json"
        cache_clock = CacheClock(1_000.0)
        capture = ControllableDatetimeClock(datetime(2026, 1, 5, tzinfo=UTC))
        first_cache = MarketDataCache(file_path=cache_path, now=cache_clock, ttl_seconds=60)
        first = CachedMarketDataClient(client, first_cache)
        miss = await first.get_klines_with_delivery("BTCUSDT", "15m", 2, capture_clock=capture)
        capture.advance(1)
        hit = await first.get_klines_with_delivery("BTCUSDT", "15m", 2, capture_clock=capture)
        encoded = json.loads(cache_path.read_text(encoding="utf-8"))
        second_client = await _binance(httpx.MockTransport(_interval_handler()))
        restarted = MarketDataCache(file_path=cache_path, now=CacheClock(1_001.0), ttl_seconds=60)
        second = CachedMarketDataClient(second_client, restarted)
        capture.advance(1)
        reloaded = await second.get_klines_with_delivery("BTCUSDT", "15m", 2, capture_clock=capture)
        await client._http_client.aclose()
        await second_client._http_client.aclose()
        assert miss.adapter is not None
        assert hit.adapter is miss.adapter
        assert hit.cache is not None and hit.cache.delivery_kind == CACHE_KIND_HIT
        assert "occurrence" not in json.dumps(encoded)
        assert "capture" not in json.dumps(encoded).lower()
        assert reloaded.adapter is None
        assert reloaded.cache is not None
        assert reloaded.cache.upstream_unavailable_reason == LIMIT_FILE_CACHE_ACQUISITION
        assert reloaded.cache.delivery_kind == CACHE_KIND_HIT

    run(scenario())
    assert EVIDENCE_NORMAL_PRODUCER


def test_uninstrumented_insertion_hit_has_known_hit_unknown_acquisition() -> None:
    class Client:
        exchange_name = "fixture"

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
            return [{"symbol": symbol, "timestamp": 1, "interval": interval, "open": Decimal("1"), "high": Decimal("1"), "low": Decimal("1"), "close": Decimal("1"), "volume": Decimal("1")}]

    cache = MarketDataCache(ttl_seconds=60, now=CacheClock(1_000.0))
    wrapped = CachedMarketDataClient(Client(), cache)
    run(wrapped.get_klines("BTCUSDT", "15m", 1))
    delivery = run(
        wrapped.get_klines_with_delivery(
            "BTCUSDT",
            "15m",
            1,
            capture_clock=ControllableDatetimeClock(datetime(2026, 1, 6, tzinfo=UTC)),
        )
    )
    assert delivery.cache is not None
    assert delivery.cache.delivery_kind == CACHE_KIND_HIT
    assert delivery.adapter is None
    assert delivery.cache.upstream_unavailable_reason == LIMIT_UNINSTRUMENTED_INSERT
    assert EVIDENCE_NORMAL_PRODUCER


def test_two_symbols_interleaved_mixed_cache_paths() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []
        client = await _binance(
            httpx.MockTransport(_interval_handler(close_by_symbol={"BTCUSDT": "100.0", "ETHUSDT": "200.0"}, requests=requests))
        )
        cache_clock = CacheClock(1_000.0)
        capture = ControllableDatetimeClock(datetime(2026, 1, 7, tzinfo=UTC))
        cache = MarketDataCache(now=cache_clock, ttl_seconds=60)
        wrapped = CachedMarketDataClient(client, cache)
        btc_miss = await wrapped.get_klines_with_delivery("BTCUSDT", "15m", 1, capture_clock=capture)
        capture.advance(1)
        eth_miss = await wrapped.get_klines_with_delivery("ETHUSDT", "1h", 1, capture_clock=capture)
        capture.advance(1)
        btc_hit = await wrapped.get_klines_with_delivery("BTCUSDT", "15m", 1, capture_clock=capture)
        await client._http_client.aclose()
        assert btc_miss.snapshot.records[0].values["close"] == Decimal("100.0")
        assert eth_miss.snapshot.records[0].values["close"] == Decimal("200.0")
        assert btc_hit.cache is not None and btc_hit.cache.delivery_kind == CACHE_KIND_HIT
        assert btc_hit.adapter is btc_miss.adapter
        assert eth_miss.cache is not None and eth_miss.cache.delivery_kind == CACHE_KIND_MISS
        assert {item.url.params["symbol"] for item in requests} == {"BTCUSDT", "ETHUSDT"}

    run(scenario())
    assert EVIDENCE_NORMAL_PRODUCER


def test_concurrent_same_key_reversed_completion_keeps_per_caller_evidence() -> None:
    class Gated:
        exchange_name = "fixture"

        def __init__(self) -> None:
            self.release_first = asyncio.Event()
            self.second_started = asyncio.Event()

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
            if not self.second_started.is_set():
                self.second_started.set()
                await self.release_first.wait()
                close = Decimal("1")
            else:
                close = Decimal("9")
            return [{"symbol": symbol, "timestamp": 1, "interval": interval, "open": close, "high": close, "low": close, "close": close, "volume": Decimal("1")}]

    async def scenario() -> None:
        client = Gated()
        cache = MarketDataCache(now=CacheClock(1_000.0), ttl_seconds=60)
        wrapped = CachedMarketDataClient(client, cache)
        clock = ControllableDatetimeClock(datetime(2026, 1, 8, tzinfo=UTC))
        first_task = asyncio.create_task(wrapped.get_klines_with_delivery("AAAUSDT", "1m", 1, capture_clock=clock))
        await client.second_started.wait()
        second = await wrapped.get_klines_with_delivery("AAAUSDT", "1m", 1, capture_clock=clock)
        client.release_first.set()
        first = await first_task
        later = await wrapped.get_klines_with_delivery("AAAUSDT", "1m", 1, capture_clock=clock)
        assert first.snapshot.records[0].values["close"] == Decimal("1")
        assert second.snapshot.records[0].values["close"] == Decimal("9")
        assert first.occurrence_token != second.occurrence_token
        assert later.snapshot.records[0].values["close"] == Decimal("1")
        assert later.cache is not None and later.cache.delivery_kind == CACHE_KIND_HIT

    run(scenario())
    assert EVIDENCE_NORMAL_PRODUCER


def test_replacement_and_disabled_path_do_not_reuse_stale_upstream() -> None:
    payloads = [
        [{"symbol": "AAA", "timestamp": 1, "interval": "1m", "open": Decimal("1"), "high": Decimal("1"), "low": Decimal("1"), "close": Decimal("1"), "volume": Decimal("1")}],
        [{"symbol": "AAA", "timestamp": 1, "interval": "1m", "open": Decimal("2"), "high": Decimal("2"), "low": Decimal("2"), "close": Decimal("2"), "volume": Decimal("2")}],
    ]

    class Client:
        exchange_name = "fixture"
        calls = 0

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
            idx = min(type(self).calls, 1)
            type(self).calls += 1
            return list(payloads[idx])

    cache_clock = CacheClock(1_000.0)
    cache = MarketDataCache(now=cache_clock, ttl_seconds=5)
    wrapped = CachedMarketDataClient(Client(), cache)
    clock = ControllableDatetimeClock(datetime(2026, 1, 9, tzinfo=UTC))
    first = run(wrapped.get_klines_with_delivery("AAA", "1m", 1, capture_clock=clock))
    cache_clock.current = 1_010.0
    second = run(wrapped.get_klines_with_delivery("AAA", "1m", 1, capture_clock=clock))
    assert first.snapshot.records[0].values["close"] == Decimal("1")
    assert second.snapshot.records[0].values["close"] == Decimal("2")
    assert second.cache is not None and second.cache.delivery_kind == CACHE_KIND_EXPIRY_REFETCH
    disabled = CachedMarketDataClient(Client(), MarketDataCache(enabled=False))
    disabled_delivery = run(disabled.get_klines_with_delivery("AAA", "1m", 1, capture_clock=clock))
    assert disabled_delivery.cache is not None
    assert disabled_delivery.cache.delivery_kind == CACHE_KIND_NOT_APPLICABLE
    assert cache.capture_association_size() <= cache.stats()["entries"]
    assert EVIDENCE_NORMAL_PRODUCER


def test_identical_and_regressing_wall_clock_do_not_merge_or_prove_possession() -> None:
    clock = ControllableDatetimeClock(datetime(2026, 1, 10, tzinfo=UTC))

    class Client:
        exchange_name = "fixture"

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
            return [{"symbol": symbol, "timestamp": 1, "interval": interval, "open": Decimal("1"), "high": Decimal("1"), "low": Decimal("1"), "close": Decimal("1"), "volume": Decimal("1")}]

    cache = MarketDataCache(enabled=False)
    wrapped = CachedMarketDataClient(Client(), cache)
    first = run(wrapped.get_klines_with_delivery("AAA", "1m", 1, capture_clock=clock))
    clock.current = clock.current - timedelta(seconds=30)
    second = run(wrapped.get_klines_with_delivery("AAA", "1m", 1, capture_clock=clock))
    assert first.occurrence_token != second.occurrence_token
    assert second.observed_at is not None and first.observed_at is not None
    assert second.observed_at < first.observed_at
    assert LIMIT_POSSESSION_UNAVAILABLE in first.limitations
    cutoff = datetime(2026, 1, 11, tzinfo=UTC)
    handoff = associate_execution_handoff(
        first,
        execution_candles=first.returned_sequence,
        execution_timeframe="1m",
        logical_cutoff=cutoff,
    )
    assert handoff.disposition in {HANDOFF_MATCHED, HANDOFF_MISMATCHED, HANDOFF_UNAVAILABLE}
    assert LIMIT_POSSESSION_UNAVAILABLE in first.limitations
    assert EVIDENCE_NORMAL_PRODUCER


def test_both_2d_routes_and_incomplete_pairs() -> None:
    start = 1_700_000_000_000
    start = start - (start % DAY_MS)
    rows = []
    for index in range(40):
        open_ms = start + index * DAY_MS
        rows.append(_kline_row(open_ms, duration_ms=DAY_MS, close=str(100 + index)))
    incomplete = rows[:3]

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=rows)

        client = await _binance(httpx.MockTransport(handler))
        clock = ControllableDatetimeClock(datetime.fromtimestamp((start + 40 * DAY_MS) / 1000, tz=UTC))
        source = await client.get_klines_with_delivery("BTCUSDT", "1d", 40, capture_clock=clock)
        synthetic = resample_ohlcv_candles(
            source.returned_sequence,
            target_interval="2d",
            decision_timestamp=clock.current,
        )
        from app.data.candle_batch_evidence import observe_closed_subset, observe_synthetic_2d_resample

        closed_1d = observe_closed_subset(source, source.returned_sequence, logical_cutoff=clock.current, capture_clock=clock)
        transformed = observe_synthetic_2d_resample(
            closed_1d,
            synthetic,
            target_interval="2d",
            logical_cutoff=clock.current,
            capture_clock=clock,
        )
        incomplete_out = resample_ohlcv_candles(
            [
                CandleDTO(
                    exchange="binance_futures",
                    symbol="BTCUSDT",
                    timestamp=item[0],
                    interval="1d",
                    open=Decimal(item[1]),
                    high=Decimal(item[2]),
                    low=Decimal(item[3]),
                    close=Decimal(item[4]),
                    volume=Decimal(item[5]),
                    close_timestamp=item[6],
                )
                for item in incomplete
            ],
            target_interval="2d",
            decision_timestamp=clock.current,
        )
        await client._http_client.aclose()
        assert transformed.producer_boundary == PRODUCER_TRANSFORM_2D
        assert transformed.transform is not None
        assert transformed.transform.mapping_complete is True
        assert transformed.parent is closed_1d
        assert len(synthetic) >= 1
        assert len(incomplete_out) == 1
        assert all(candle.interval == "2d" for candle in synthetic)

    run(scenario())

    client = FakeExchangeClient({"BTCUSDT": _flat_candles()}, failing_timeframes={"2d"})
    primary_2d = run(
        ScannerRunner(exchange_client=client).run(
            _scan_config(
                interval="2d",
                execution_timeframe="2d",
                htf_timeframe="2d",
                decision_timestamp=_decision_after_rows("15m", 220),
            )
        )
    )
    strategy_2d = run(
        ScannerRunner(exchange_client=FakeExchangeClient({"BTCUSDT": _flat_candles()}, failing_timeframes=set())).run(
            _scan_config(
                enable_strategy_output=True,
                interval="1d",
                execution_timeframe="15m",
                htf_timeframe="2d",
                candle_limit=80,
                decision_timestamp=_decision_after_rows("15m", 220),
            )
        )
    )
    assert primary_2d.results[0].lifecycle_execution_batch_delivery is not None
    assert strategy_2d.results[0].lifecycle_execution_batch_delivery is not None
    assert EVIDENCE_NORMAL_PRODUCER


def test_closed_filter_and_execution_timeframe_selection() -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()}, failing_timeframes={"2d"})
    decision = datetime(2026, 1, 1, tzinfo=UTC)
    result = run(
        ScannerRunner(exchange_client=client, capture_clock=ControllableDatetimeClock(decision)).run(
            _scan_config(decision_timestamp=decision, enable_strategy_output=False)
        )
    )
    symbol = result.results[0]
    delivery = symbol.lifecycle_execution_batch_delivery
    assert delivery is not None
    assert delivery.producer_boundary == PRODUCER_FILTER_SELECTION
    assert delivery.selection is not None
    assert delivery.logical_cutoff == decision
    assert tuple(symbol.lifecycle_execution_candles or ()) == delivery.returned_sequence
    assert EVIDENCE_NORMAL_PRODUCER


def test_no_strategy_matching_and_mismatching_execution_timeframes() -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()}, failing_timeframes={"2d"})
    decision = datetime(2026, 1, 1, tzinfo=UTC)
    matched = run(
        ScannerRunner(exchange_client=client).run(
            _scan_config(decision_timestamp=decision, interval="15m", execution_timeframe="15m")
        )
    )
    mismatched = run(
        ScannerRunner(exchange_client=client).run(
            _scan_config(decision_timestamp=decision, interval="15m", execution_timeframe="1h")
        )
    )
    assert matched.results[0].lifecycle_execution_candles
    assert mismatched.results[0].lifecycle_execution_candles == ()
    assert mismatched.results[0].lifecycle_execution_batch_delivery is not None
    assert mismatched.results[0].lifecycle_execution_batch_delivery.producer_boundary == PRODUCER_EMPTY_EXECUTION
    assert EVIDENCE_NORMAL_PRODUCER


def test_ordinary_runner_to_lifecycle_handoff(tmp_path: Path) -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()}, failing_timeframes={"2d"})
    decision = datetime(2026, 1, 1, tzinfo=UTC)
    runner = ScannerRunner(exchange_client=client, capture_clock=ControllableDatetimeClock(decision + timedelta(seconds=5)))
    result = run(runner.run(_scan_config(decision_timestamp=decision)))
    db_path = tmp_path / "handoff.sqlite"
    updated = SetupLifecycleService(db_path).apply_to_run_result(result, scan_run_id="cap-1", now="2026-01-01T00:00:00Z")
    symbol = updated.results[0]
    handoff = symbol.lifecycle_batch_handoff
    assert handoff is not None
    assert handoff.disposition == HANDOFF_MATCHED
    assert handoff.execution_timeframe == "15m"
    assert handoff.logical_cutoff == decision
    dumped = symbol.model_dump()
    encoded = symbol.model_dump_json()
    assert "lifecycle_execution_candles" not in dumped
    assert "lifecycle_execution_batch_delivery" not in dumped
    assert "lifecycle_batch_handoff" not in dumped
    assert "lifecycle_execution_candles" not in encoded
    assert "lifecycle_execution_batch_delivery" not in encoded
    assert "lifecycle_batch_handoff" not in encoded
    store_scan_result(db_path, updated, inline_raw_payload=True)
    with open_initialized_database(db_path) as connection:
        raw = connection.execute("SELECT raw_payload_json FROM scan_runs").fetchone()[0]
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert "lifecycle_batch_handoff" not in raw
    assert user_version == SCHEMA_VERSION == 25
    assert symbol.status in {
        ScannerPipelineStatus.SCANNED_NO_SETUP,
        ScannerPipelineStatus.REJECTED_BY_TECHNICAL,
        ScannerPipelineStatus.REJECTED_BY_REGIME,
        ScannerPipelineStatus.IDEA_CREATED,
        ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        ScannerPipelineStatus.ALERT_DRY_RUN_CREATED,
    }
    assert EVIDENCE_NORMAL_PRODUCER


def test_direct_none_empty_not_run_and_failures_do_not_fabricate_evidence(tmp_path: Path) -> None:
    db_path = tmp_path / "direct.sqlite"
    service = SetupLifecycleService(db_path)
    direct = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        strategy_diagnostics={"swing": {"mode": "swing", "bias": "long", "invalidation": "x"}},
        valid_strategy_modes=("swing",),
        lifecycle_execution_candles=(_no_touch(0, "long"), _both_entry(1)),
        lifecycle_execution_timeframe=TIMEFRAME,
        lifecycle_decision_timestamp=_close(1),
    )
    with open_initialized_database(db_path):
        pass
    from app.lifecycle.repositories import SQLiteSetupLifecycleRepository

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(_record())
        updated = service.apply_to_symbol_result(
            direct,
            repository=repository,
            scan_run_id="direct",
            now=_decision(1),
        )
    assert updated.lifecycle_batch_handoff is not None
    assert updated.lifecycle_batch_handoff.disposition == HANDOFF_UNAVAILABLE
    none_result = ScannerSymbolResult(
        symbol="ETHUSDT",
        status=ScannerPipelineStatus.NOT_RUN,
        status_history=(ScannerPipelineStatus.NOT_RUN,),
        not_run_reason="skipped",
    )
    run_result = ScannerRunner(exchange_client=FakeExchangeClient({"BTCUSDT": _flat_candles()}, failing_symbols={"BTCUSDT"})).run
    failed = run(ScannerRunner(exchange_client=FakeExchangeClient({"BTCUSDT": _flat_candles()}, failing_symbols={"BTCUSDT"})).run(_scan_config()))
    assert failed.results[0].status == ScannerPipelineStatus.SCAN_ERROR
    assert failed.results[0].lifecycle_execution_candles is None
    assert none_result.lifecycle_execution_candles is None

    async def broken_json() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, text="not-json")

        client = await _binance(httpx.MockTransport(handler))
        with pytest.raises(ExchangeMalformedJSONError):
            await client.get_klines("BTCUSDT", "1m", 1)
        await client._http_client.aclose()

    run(broken_json())
    assert EVIDENCE_SUPPLIED_STATE
    assert EVIDENCE_NORMAL_PRODUCER
    assert EVIDENCE_MIXED


def test_snapshot_immutability_and_false_handoff_rejected() -> None:
    raw = _kline_row(1_700_000_000_000, duration_ms=60_000)
    raw_copy = list(raw)

    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=[raw_copy])

        client = await _binance(httpx.MockTransport(handler))
        delivery = await client.get_klines_with_delivery(
            "BTCUSDT",
            "1m",
            1,
            capture_clock=ControllableDatetimeClock(datetime(2026, 1, 12, tzinfo=UTC)),
        )
        await client._http_client.aclose()
        listed = list(delivery.returned_sequence)
        listed[0].raw_source[1] = "999"
        assert delivery.snapshot.records[0].values["open"] == Decimal("100.0")
        mapping = {
            "timestamp": 1,
            "open": Decimal("1"),
            "high": Decimal("2"),
            "low": Decimal("0"),
            "close": Decimal("1"),
            "volume": Decimal("1"),
            "interval": "1m",
        }
        snapshot = snapshot_candle_batch([mapping])
        mapping["close"] = Decimal("50")
        assert snapshot.records[0].values["close"] == Decimal("1")
        exported = snapshot.to_canonical_dict()
        exported["records"][0]["values"]["close"] = "50"
        assert snapshot.records[0].values["close"] == Decimal("1")
        mutated_seq = tuple(reversed(delivery.returned_sequence))
        handoff = associate_execution_handoff(
            delivery,
            execution_candles=mutated_seq if mutated_seq else (_no_touch(0, "long"),),
            execution_timeframe="5m",
            logical_cutoff=datetime(2026, 1, 12, tzinfo=UTC),
        )
        assert handoff.disposition == HANDOFF_MISMATCHED
        missing = associate_execution_handoff(
            delivery,
            execution_candles=None,
            execution_timeframe="1m",
            logical_cutoff=datetime(2026, 1, 12, tzinfo=UTC),
        )
        assert missing.disposition == HANDOFF_UNAVAILABLE

    run(scenario())
    assert EVIDENCE_MALFORMED
    assert EVIDENCE_NORMAL_PRODUCER


def test_capture_clock_failure_keeps_business_path() -> None:
    def boom() -> datetime:
        raise RuntimeError("clock failed")

    async def scenario() -> None:
        client = await _binance(httpx.MockTransport(_interval_handler()))
        candles = await client.get_klines("BTCUSDT", "1m", 1)
        delivery = await client.get_klines_with_delivery("BTCUSDT", "1m", 1, capture_clock=boom)
        await client._http_client.aclose()
        assert candles[0].close == Decimal("100.0")
        assert delivery.disposition == DISPOSITION_UNAVAILABLE
        assert delivery.unavailable_reason == "capture_representation_failed"
        assert delivery.returned_sequence

    run(scenario())
    assert EVIDENCE_NORMAL_PRODUCER


def test_source_descriptor_and_policy_remain_unbound_and_unused() -> None:
    descriptor = build_source_evidence_descriptor(
        evaluation_purpose="scanner_observation",
        input_origin="synthetic_fixture",
        delivery_path="cache_return_path",
        cache_delivery_kind="hit",
    )
    assessment = assess_source_evidence(descriptor)
    assert descriptor.binding_status == BINDING_STATUS_UNBOUND
    assert assessment.witnessed_acquisition_chain == SUPPORT_UNAVAILABLE
    assert assessment.possession_at_logical_cutoff == SUPPORT_UNAVAILABLE
    assert assessment.authentic_venue_provenance == SUPPORT_UNAVAILABLE
    first = build_runtime_evaluation_policy(execution_timeframe="15m")
    second = build_runtime_evaluation_policy(execution_timeframe="15m")
    assert first.policy_id == second.policy_id
    production_hits: list[str] = []
    for folder in (REPO_ROOT / "app", REPO_ROOT / "scripts"):
        for path in folder.rglob("*.py"):
            if path.name in {"source_evidence.py", "evaluation_policy.py"}:
                continue
            text = path.read_text(encoding="utf-8")
            if "research.source_evidence" in text or "from app.research import source_evidence" in text:
                production_hits.append(str(path.relative_to(REPO_ROOT)))
            if "research.evaluation_policy" in text or "from app.research import evaluation_policy" in text:
                production_hits.append(str(path.relative_to(REPO_ROOT)))
    assert production_hits == []
    assert "source_evidence" not in (REPO_ROOT / "app/research/__init__.py").read_text(encoding="utf-8")
    assert EVIDENCE_SOURCE_ONLY


def test_override_get_klines_is_not_bypassed() -> None:
    class Override(BinanceFuturesClient):
        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[CandleDTO]:
            return [
                CandleDTO(
                    exchange="override",
                    symbol=symbol.upper(),
                    timestamp=1,
                    interval=interval,
                    open=Decimal("7"),
                    high=Decimal("7"),
                    low=Decimal("7"),
                    close=Decimal("7"),
                    volume=Decimal("7"),
                )
            ]

    override = Override(
        http_client=httpx.AsyncClient(transport=httpx.MockTransport(lambda _request: httpx.Response(500)))
    )
    assert client_offers_same_path_klines_delivery(override) is False
    delivery = run(
        fetch_klines_with_delivery(
            override,
            "BTCUSDT",
            "1m",
            1,
            capture_clock=ControllableDatetimeClock(datetime(2026, 1, 13, tzinfo=UTC)),
        )
    )
    assert delivery.producer_boundary == PRODUCER_CLIENT_RETURN
    assert delivery.returned_sequence[0].close == Decimal("7")
    assert EVIDENCE_NORMAL_PRODUCER


def test_capture_includes_accepted_and_rejected_without_denominator() -> None:
    rejected = run(
        ScannerRunner(exchange_client=FakeExchangeClient({"BTCUSDT": _flat_candles()}, failing_timeframes={"2d"})).run(
            _scan_config(decision_timestamp=datetime(2026, 1, 1, tzinfo=UTC))
        )
    )
    accepted_like = run(
        ScannerRunner(exchange_client=FakeExchangeClient({"BTCUSDT": _flat_candles()}, failing_timeframes={"2d"})).run(
            _scan_config(decision_timestamp=datetime(2026, 1, 1, tzinfo=UTC), enable_strategy_output=True)
        )
    )
    assert rejected.results[0].lifecycle_execution_batch_delivery is not None
    assert accepted_like.results[0].lifecycle_execution_batch_delivery is not None
    assert rejected.results[0].status != ScannerPipelineStatus.NOT_RUN
    assert EVIDENCE_NORMAL_PRODUCER


def test_capture_association_is_bounded_by_retained_entries() -> None:
    class Client:
        exchange_name = "fixture"
        n = 0

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Any]]:
            type(self).n += 1
            close = Decimal(str(type(self).n))
            return [{"symbol": symbol, "timestamp": 1, "interval": interval, "open": close, "high": close, "low": close, "close": close, "volume": Decimal("1")}]

    cache_clock = CacheClock(1_000.0)
    cache = MarketDataCache(now=cache_clock, ttl_seconds=5)
    wrapped = CachedMarketDataClient(Client(), cache)
    clock = ControllableDatetimeClock(datetime(2026, 1, 14, tzinfo=UTC))
    run(wrapped.get_klines_with_delivery("AAA", "1m", 1, capture_clock=clock))
    run(wrapped.get_klines_with_delivery("AAA", "1m", 1, capture_clock=clock))
    cache_clock.current = 1_010.0
    run(wrapped.get_klines_with_delivery("AAA", "1m", 1, capture_clock=clock))
    run(wrapped.get_klines_with_delivery("BBB", "1m", 1, capture_clock=clock))
    assert cache.capture_association_size() <= cache.stats()["entries"]
    assert cache.capture_association_size() <= 2
    assert EVIDENCE_NORMAL_PRODUCER


def test_schema_and_source_contract_remain_v25_unbound(tmp_path: Path) -> None:
    path = tmp_path / "capture-schema.sqlite"
    with open_initialized_database(path) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        columns: set[str] = set()
        for table in tables:
            columns.update(row[1] for row in connection.execute(f"PRAGMA table_info({table})"))
    assert user_version == 25
    assert "candle_batch_evidence" not in tables
    assert "source_evidence" not in tables
    for forbidden in ("admission_id", "source_namespace", "capture_occurrence_id", "evaluation_policy_id"):
        assert forbidden not in columns
    assert inspect.isfunction(associate_execution_handoff)
    assert inspect.isfunction(observe_unsupported_client_return)
    assert deepcopy is not None
    assert EVIDENCE_SOURCE_ONLY
