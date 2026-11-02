"""Deterministic public-data replay; no sockets, runtime DB or delivery paths."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.context.models import ContextStatus
from app.data.exchange_clients import binance_futures
from app.data.exchange_clients.binance_futures import BinanceFuturesClient
from app.microstructure.liquidation_service import LiquidationFlowService
from app.microstructure.order_book_service import OrderBookLiquidityService
from app.microstructure.service import MicrostructureFlowService
from app.pipeline.scanner_runner import ScannerRunner
from tests.test_liquidation_flow import _payload as liquidation
from tests.test_microstructure_flow import _payload as trade
from tests.test_order_book_liquidity import NOW, _event_payload as depth, _snapshot_payload
from tests.test_production_repair_streams import UNICODE, metadata

pytestmark = pytest.mark.no_auto_epoch
SYMBOL = "BTCUSDT"


class SimulatedTime:
    def __init__(self):
        self.seconds = 0.0
        self.waiters = []

    def __call__(self):
        return NOW + timedelta(seconds=self.seconds)

    async def sleep(self, delay):
        future = asyncio.get_running_loop().create_future()
        self.waiters.append((self.seconds + delay, future))
        try:
            await future
        finally:
            self.waiters = [(t, f) for t, f in self.waiters if f is not future]

    async def advance(self, seconds):
        self.seconds = seconds
        for deadline, future in tuple(self.waiters):
            if deadline <= seconds and not future.done():
                future.set_result(None)
        await settle()


async def settle():
    for _ in range(30):
        await asyncio.sleep(0)


async def collectors(clock, symbols=(SYMBOL,)):
    services = (MicrostructureFlowService(clock=clock), OrderBookLiquidityService(clock=clock),
                LiquidationFlowService(clock=clock))
    for service in services:
        await service.reconcile_symbols(symbols)
    return services


def runner_for(client, services, clock):
    return ScannerRunner(exchange_client=client, microstructure_flow_service=services[0],
        order_book_liquidity_service=services[1], liquidation_flow_service=services[2],
        clock=clock, capture_clock=clock)


def test_scanner_refresh_propagates_real_cache_renewal_to_all_long_lived_services(monkeypatch):
    async def exercise():
        clock, requests = SimulatedTime(), []

        def respond(request):
            requests.append(clock.seconds)
            return httpx.Response(200, json=metadata(SYMBOL))

        monkeypatch.setattr(binance_futures, "time", SimpleNamespace(monotonic=lambda: clock.seconds))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://example.test") as http:
            client = BinanceFuturesClient(http_client=http)
            if hasattr(client, "_stream_metadata_refresher"):
                client._stream_metadata_refresher.sleep = clock.sleep
                client._stream_metadata_refresher.monotonic = lambda: clock.seconds
            services = await collectors(clock)
            for service in services:
                service.transport = MemoryTransport()
            services[1].snapshot_client = ReplayDepthClient(clock)
            runner = runner_for(client, services, clock)
            config = SimpleNamespace(exchange="binance", request_timeout_sec=3, max_retries=0)
            try:
                await runner._install_stream_metadata(client, config)
                assert all(s.evidence.metadata_at == NOW for s in services)
                for service in services:
                    await service.start([SYMBOL])
                await settle()
                assert all(s.health()["subscription_confirmed"] for s in services)
                assert services[0]._handle_payload(trade(1, clock()))
                assert services[1]._handle_payload(depth())
                assert services[2]._handle_payload(liquidation(clock()))
                assert services[1].snapshot(SYMBOL).synchronized
                # The production scanner asks again just before cache expiry.
                await clock.advance(299)
                await runner._install_stream_metadata(client, config)
                assert all(s.evidence.identity_reason(SYMBOL) is None for s in services)
                assert services[0]._handle_payload(trade(2, clock()))
                assert services[1]._handle_payload(depth(first=102, final=103, previous=101,
                    event_ms=int(clock().timestamp() * 1000)))
                assert services[2]._handle_payload(liquidation(clock()))
                await clock.advance(301)
                _, acquired = await client.get_stream_contract_metadata(capture_clock=clock)
                assert len(requests) >= 2  # The upstream really renewed its cache.
                # Production retained NOW in all three services until the next scan.
                reasons = [s.evidence.identity_reason(SYMBOL) for s in services]
                assert all(reason is None for reason in reasons), reasons
                # These are CURRENT market events for the correct contract.
                assert services[0]._handle_payload(trade(3, clock()))
                assert services[1]._handle_payload(depth(first=104, final=105, previous=103,
                    event_ms=int(clock().timestamp() * 1000)))
                assert services[2]._handle_payload(liquidation(clock()))
                assert all(s.evidence.metadata_at == acquired for s in services)
            finally:
                await client.aclose()
                await asyncio.gather(*(s.stop() for s in services))

    asyncio.run(exercise())


class MemoryConnection:
    """An acknowledged in-memory transport, never a network connection."""
    def __init__(self):
        self.messages = asyncio.Queue()
        self.sent = []
        self.closed = False

    async def send(self, message):
        decoded = json.loads(message)
        self.sent.append(decoded)
        await self.messages.put({"id": decoded["id"], "result": None})

    async def recv(self):
        return await self.messages.get()

    async def close(self):
        self.closed = True


class MemoryTransport:
    def __init__(self):
        self.connection = MemoryConnection()
        self.connects = 0

    async def connect(self):
        self.connects += 1
        return self.connection


class ReplayDepthClient:
    def __init__(self, clock):
        self.clock = clock
        self.requests = []

    async def fetch(self, symbol, limit):
        self.requests.append((symbol, self.clock.seconds))
        result = _snapshot_payload(update_id=100 + int(self.clock.seconds))
        stamp = int(self.clock().timestamp() * 1000)
        result.update(E=stamp - 10, T=stamp - 11)
        return result

    async def aclose(self):
        pass


class StreamReplay:
    def __init__(self, monkeypatch):
        self.clock = SimulatedTime()
        self.requests = []
        self.fail = False
        self.response_gate = None
        self.payload = metadata(SYMBOL, UNICODE)
        self.transports = [MemoryTransport() for _ in range(3)]
        self.depth_client = ReplayDepthClient(self.clock)
        self.services = (
            MicrostructureFlowService(clock=self.clock, transport=self.transports[0]),
            OrderBookLiquidityService(clock=self.clock, transport=self.transports[1],
                snapshot_client=self.depth_client, bootstrap_min_interval_seconds=0),
            LiquidationFlowService(clock=self.clock, transport=self.transports[2]),
        )
        self.http = httpx.AsyncClient(transport=httpx.MockTransport(self.respond), base_url="https://example.test")
        self.client = BinanceFuturesClient(http_client=self.http, retry_attempts=1)
        refresher = self.client._stream_metadata_refresher
        refresher.sleep, refresher.monotonic = self.clock.sleep, lambda: self.clock.seconds
        monkeypatch.setattr(binance_futures, "time", SimpleNamespace(monotonic=lambda: self.clock.seconds))
        self.config = SimpleNamespace(exchange="binance", request_timeout_sec=3)

    async def respond(self, request):
        assert request.url.path == "/fapi/v1/exchangeInfo"
        self.requests.append(self.clock.seconds)
        if self.response_gate is not None:
            await self.response_gate.wait()
        if self.fail:
            raise httpx.ReadTimeout("simulated public metadata outage")
        return httpx.Response(200, json=self.payload)

    async def scanner_iteration(self):
        # Each production iteration builds a new runner with the SAME client
        # and service owners; only the admission maintainer survives the scan.
        runner = runner_for(self.client, self.services, self.clock)
        await runner._install_stream_metadata(self.client, self.config)
        return runner

    async def start(self):
        for service in self.services:
            await service.reconcile_symbols([SYMBOL, UNICODE])
        await self.scanner_iteration()
        for service in self.services:
            await service.start([SYMBOL, UNICODE])
        await settle()
        assert all(s.health()["subscription_confirmed"] for s in self.services)

    async def tick(self, second, *, liquidations=True):
        await self.clock.advance(second)
        for symbol in (SYMBOL, UNICODE):
            await self.transports[0].connection.messages.put(trade(second + 1, self.clock(), symbol=symbol))
            await self.transports[1].connection.messages.put(depth(symbol=symbol, first=100 + second,
                final=101 + second, previous=99 if second == 0 else 100 + second,
                event_ms=int(self.clock().timestamp() * 1000), bids=[["99.99", str(second + 1)]]))
        if liquidations and second % 60 == 0:
            await self.transports[2].connection.messages.put(liquidation(self.clock()))
        await settle()

    async def close(self):
        await self.client.aclose()
        await asyncio.gather(*(s.stop() for s in self.services))
        await self.http.aclose()


def test_thirty_minutes_of_continuous_admission_across_five_minute_scans(monkeypatch):
    async def exercise():
        replay = StreamReplay(monkeypatch)
        try:
            await replay.start()
            frozen = None
            frozen_dump = None
            for second in range(1801):
                await replay.tick(second)
                if second % 300 == 0:
                    runner = await replay.scanner_iteration()
                for symbol in (SYMBOL, UNICODE):
                    for service in replay.services:
                        assert service.evidence.identity_reason(symbol) is None
                    flow, book, liq = (s.snapshot(symbol) for s in replay.services)
                    assert book.status == ContextStatus.VERIFIED and book.synchronized
                    assert all(b.bid_coverage_complete and b.ask_coverage_complete for b in book.bands.values())
                    assert book.observed_at == replay.clock()
                    assert flow.accepted_event_count == second + 1
                    assert flow.gap_count == 0
                    assert (flow.status == ContextStatus.VERIFIED) is (second >= 900)
                    assert (liq.status == ContextStatus.VERIFIED) is (second >= 900)
                    if second >= 900:
                        assert flow.windows["15m"].coverage_complete
                        assert flow.windows["15m"].aggregate_event_count == 900
                        assert liq.windows["15m"].coverage_complete
                        assert liq.windows["15m"].event_count == (15 if symbol == SYMBOL else 0)
                        if symbol == UNICODE:
                            assert liq.windows["15m"].total_liquidation_quote == 0
                if second == 900:
                    frozen = runner._supporting_snapshot(replay.services[1], SYMBOL, None)
                    frozen_dump = frozen.model_dump(mode="json")
                if frozen is not None:
                    assert frozen.model_dump(mode="json") == frozen_dump
                    assert frozen.observed_at == NOW + timedelta(seconds=900)
            assert replay.requests == list(range(0, 1801, 240))
            assert len(replay.depth_client.requests) == 2  # no healthy resyncs
            for service, transport in zip(replay.services, replay.transports):
                health = service.health()
                assert health["metadata_refresh"]["renewals"] == 8
                assert health["metadata_refresh"]["consecutive_failures"] == 0
                assert health["metadata_acquired_at"] == (NOW + timedelta(seconds=1680)).isoformat()
                assert health["connection_count"] == transport.connects == 1
                assert health["disconnect_count"] == 0
                assert health["acknowledged_symbol_count"] == 2
                assert len(transport.connection.sent) == 1
            assert replay.services[2].health()["per_symbol"][UNICODE].get("accepted_events", 0) == 0
            assert replay.services[0].health()["per_symbol"][SYMBOL]["accepted_events"] == 1801
        finally:
            await replay.close()
        assert replay.client._stream_metadata_refresher._task is None
        assert all(t.connection.closed for t in replay.transports)

    asyncio.run(exercise())


def test_outage_expires_admission_and_recovery_requires_a_complete_new_window(monkeypatch):
    async def exercise():
        replay = StreamReplay(monkeypatch)
        try:
            await replay.start()
            for second in range(961):
                await replay.tick(second, liquidations=False)
            assert all(s.snapshot(SYMBOL).status == ContextStatus.VERIFIED for s in replay.services)
            replay.fail = True
            # Last genuine acquisition was 960; the failed renewal is at 1200.
            for second in range(961, 1262):
                await replay.tick(second, liquidations=False)
            assert all(s.snapshot(SYMBOL).status == ContextStatus.UNVERIFIED for s in replay.services)
            assert replay.services[2].snapshot(UNICODE).windows.get("15m") is None
            assert not replay.services[1]._books[SYMBOL].synchronized
            assert replay.services[0].health()["per_symbol"][SYMBOL]["accepted_events"] == 1261
            replay.fail = False
            # Retry backoff is bounded at 30 seconds; the next attempt is 1265.
            for second in range(1262, 1266):
                await replay.tick(second, liquidations=False)
            recovered_at = replay.services[0].evidence.metadata_at
            assert recovered_at == NOW + timedelta(seconds=1265)
            assert replay.services[1].snapshot(SYMBOL).synchronized
            assert len(replay.depth_client.requests) == 4  # exactly one resync per symbol
            assert replay.services[0].snapshot(SYMBOL).status != ContextStatus.VERIFIED
            assert replay.services[2].snapshot(UNICODE).windows["15m"].event_count is None
            # The canonical window uses completed minutes: [1320, 2220).
            for second in range(1266, 2221):
                await replay.tick(second, liquidations=False)
                flow = replay.services[0].snapshot(SYMBOL)
                quiet = replay.services[2].snapshot(UNICODE)
                assert (flow.status == ContextStatus.VERIFIED) is (second >= 2220)
                assert (quiet.status == ContextStatus.VERIFIED) is (second >= 2220)
            assert quiet.windows["15m"].event_count == 0
            assert flow.windows["15m"].aggregate_event_count == 900
            assert all(t.connects == 1 and len(t.connection.sent) == 1 for t in replay.transports)
            assert replay.services[0].health()["metadata_refresh"]["consecutive_failures"] == 0
        finally:
            await replay.close()

    asyncio.run(exercise())


def test_quiet_liquidations_cannot_reuse_coverage_across_unobserved_metadata_expiry():
    async def exercise():
        clock = SimulatedTime()
        service = LiquidationFlowService(clock=clock)
        await service.reconcile_symbols([SYMBOL])
        service.evidence.set_metadata(metadata(SYMBOL), clock())
        service._connection = object()
        service._connection_count = 1
        service.evidence.requested(1, "SUBSCRIBE", {SYMBOL})
        service._handle_payload({"id": 1, "result": None})
        # No market events or assessments during this gap; an all-market stream
        # cannot prove an observed zero if its identity admission had expired.
        await clock.advance(901)
        service.evidence.set_metadata(metadata(SYMBOL), clock())
        snapshot = service.snapshot(SYMBOL)
        assert snapshot.status != ContextStatus.VERIFIED
        assert snapshot.windows["15m"].event_count is None

    asyncio.run(exercise())
