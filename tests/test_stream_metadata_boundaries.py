"""Cache, concurrency, identity and shutdown boundaries of stream admission."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from app.context.models import ContextStatus
from app.data.exchange_clients import binance_futures
from app.data.exchange_clients.binance_futures import BinanceFuturesClient
from app.microstructure import metadata_refresh
from app.microstructure.stream_evidence import StreamEvidence
from tests.test_order_book_liquidity import NOW
from tests.test_production_repair_streams import UNICODE, metadata
from tests.test_stream_metadata_lifecycle import (
    SYMBOL, SimulatedTime, StreamReplay, settle,
)

pytestmark = pytest.mark.no_auto_epoch


def test_cache_refresh_boundary_is_single_flight_and_does_not_restamp_cached_reads(monkeypatch):
    async def exercise():
        clock, requests, gate = SimulatedTime(), [], asyncio.Event()

        async def respond(request):
            requests.append(clock.seconds)
            if len(requests) > 1:
                await gate.wait()
            return httpx.Response(200, json=metadata(SYMBOL))

        monkeypatch.setattr(binance_futures, "time", SimpleNamespace(monotonic=lambda: clock.seconds))
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), base_url="https://example.test") as http:
            client = BinanceFuturesClient(http_client=http)
            try:
                first, acquired = await client.get_stream_contract_metadata(capture_clock=clock, max_age_seconds=240)
                first["symbols"].clear()  # caller mutation cannot corrupt the cache
                await clock.advance(239.999)
                cached, cached_at = await client.get_stream_contract_metadata(capture_clock=clock, max_age_seconds=240)
                assert cached_at == acquired == NOW and cached["symbols"]
                assert requests == [0]
                await clock.advance(240)
                tasks = [asyncio.create_task(client.get_stream_contract_metadata(
                    capture_clock=clock, max_age_seconds=240)) for _ in range(50)]
                await settle()
                assert requests == [0, 240]
                await clock.advance(250)
                gate.set()
                results = await asyncio.gather(*tasks)
                assert all(at == NOW + timedelta(seconds=250) for _, at in results)
                assert requests == [0, 240]
                # UTC jumps must not make an expired/future source cache usable
                # merely because the monotonic TTL has not elapsed.
                await clock.advance(249)
                _, at = await client.get_stream_contract_metadata(capture_clock=clock, max_age_seconds=240)
                assert at == clock() and requests == [0, 240, 249]
            finally:
                await client.aclose()

    asyncio.run(exercise())


def test_duplicate_scanner_registration_and_failed_renewals_are_bounded(monkeypatch):
    async def exercise():
        replay = StreamReplay(monkeypatch)
        replay.fail = True
        try:
            await asyncio.gather(*(replay.scanner_iteration() for _ in range(50)))
            assert replay.requests == [0]
            assert all(s.evidence.identity_reason(SYMBOL) for s in replay.services)
            assert all(s.health()["metadata_refresh"]["consecutive_failures"] == 1 for s in replay.services)
            await replay.clock.advance(4.999)
            await asyncio.gather(*(replay.scanner_iteration() for _ in range(50)))
            assert replay.requests == [0]
            await replay.clock.advance(5)
            assert replay.requests == [0, 5]
            replay.fail = False
            await replay.clock.advance(15)
            assert replay.requests == [0, 5, 15]
            assert all(s.evidence.identity_reason(SYMBOL) is None for s in replay.services)
        finally:
            await replay.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("payload", [[], {"symbols": []}, {"symbols": None}])
def test_malformed_renewal_withholds_immediately_without_waiting_for_old_expiry(monkeypatch, payload):
    async def exercise():
        replay = StreamReplay(monkeypatch)
        try:
            await replay.start()
            await replay.tick(0)
            replay.payload = payload
            await replay.clock.advance(240)
            assert all(s.evidence.identity_reason(SYMBOL) for s in replay.services)
            assert all(s.snapshot(SYMBOL).status == ContextStatus.UNVERIFIED for s in replay.services)
            assert not replay.services[1]._books[SYMBOL].synchronized
            assert all(s.health()["metadata_refresh"]["consecutive_failures"] == 1 for s in replay.services)
            replay.payload = metadata(SYMBOL, UNICODE)
            await replay.tick(245)
            assert all(s.evidence.identity_reason(SYMBOL) is None for s in replay.services)
            assert replay.services[2].snapshot(UNICODE).status != ContextStatus.VERIFIED
            assert all(t.connects == 1 for t in replay.transports)
        finally:
            await replay.close()

    asyncio.run(exercise())


def test_delayed_response_cannot_bridge_a_real_expiry_gap(monkeypatch):
    async def exercise():
        replay = StreamReplay(monkeypatch)
        try:
            await replay.start()
            for second in range(240):
                await replay.tick(second)
            replay.response_gate = asyncio.Event()
            await replay.tick(240)
            assert replay.requests == [0, 240]
            for second in range(241, 301):
                await replay.tick(second)
            assert all(s.evidence.identity_reason(SYMBOL) is None for s in replay.services)
            assert replay.services[1].snapshot(SYMBOL).synchronized
            await replay.tick(301)
            assert all(s.evidence.identity_reason(SYMBOL) for s in replay.services)
            await replay.clock.advance(330)
            replay.response_gate.set()
            await settle()
            assert all(s.evidence.metadata_at == NOW + timedelta(seconds=330) for s in replay.services)
            assert all(s.evidence.identity_reason(SYMBOL) is None for s in replay.services)
            assert replay.services[2].snapshot(UNICODE).status != ContextStatus.VERIFIED
            assert replay.services[0].snapshot(SYMBOL).status != ContextStatus.VERIFIED
            assert replay.services[1].snapshot(SYMBOL).status != ContextStatus.VERIFIED
            await replay.tick(330)
            assert replay.services[1].snapshot(SYMBOL).synchronized
            assert all(t.connects == 1 for t in replay.transports)
        finally:
            await replay.close()

    asyncio.run(exercise())


def test_cancelling_a_scanner_waiter_does_not_cancel_the_owned_refresh(monkeypatch):
    async def exercise():
        replay = StreamReplay(monkeypatch)
        replay.response_gate = asyncio.Event()
        waiter = asyncio.create_task(replay.scanner_iteration())
        try:
            await settle()
            assert replay.requests == [0]
            waiter.cancel()
            with pytest.raises(asyncio.CancelledError):
                await waiter
            assert not replay.client._stream_metadata_refresher._task.done()
            replay.response_gate.set()
            await settle()
            assert all(s.evidence.identity_reason(SYMBOL) is None for s in replay.services)
            assert replay.requests == [0]
        finally:
            await replay.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("offset", [-31, 1])
def test_depth_bootstrap_rejects_expired_or_future_source_timestamps(monkeypatch, offset):
    async def exercise():
        replay = StreamReplay(monkeypatch)
        book = replay.services[1]
        book.bootstrap_attempts = 1
        book.sleep = replay.clock.sleep
        original_fetch = replay.depth_client.fetch

        async def fetch(symbol, limit):
            payload = await original_fetch(symbol, limit)
            stamp = int((replay.clock() + timedelta(seconds=offset)).timestamp() * 1000)
            payload.update(E=stamp, T=stamp)
            return payload

        replay.depth_client.fetch = fetch
        try:
            await replay.start()
            await replay.tick(0)
            assert book.health()["snapshot_failure_count"] == 2
            assert not book.snapshot(SYMBOL).synchronized
            assert book.snapshot(SYMBOL).status != ContextStatus.VERIFIED
            assert replay.services[0].evidence.identity_reason(SYMBOL) is None
        finally:
            await replay.close()

    asyncio.run(exercise())


def test_refresh_timeout_cancellation_and_restart_do_not_leak_tasks(monkeypatch):
    async def exercise():
        replay = StreamReplay(monkeypatch)
        try:
            await replay.start()
            replay.response_gate = asyncio.Event()
            await replay.clock.advance(240)
            task = replay.client._stream_metadata_refresher._task
            await replay.client.aclose()
            assert task.done() and task.cancelled()
            assert not replay.client._exchange_info_lock.locked()
            assert not replay.clock.waiters
            await replay.clock.advance(301)
            replay.response_gate = None
            await replay.scanner_iteration()
            assert replay.client._stream_metadata_refresher._task is not task
            assert all(s.evidence.metadata_at == replay.clock() for s in replay.services)
            assert all(t.connects == 1 for t in replay.transports)
            # A stuck HTTP response is bounded independently of scanner timing.
            monkeypatch.setattr(metadata_refresh, "METADATA_REFRESH_TIMEOUT_SECONDS", 0.01)
            replay.response_gate = asyncio.Event()
            await replay.clock.advance(541)
            await asyncio.sleep(0.03)
            assert replay.services[0].health()["metadata_refresh"]["last_error"] == "TimeoutError"
            assert not replay.client._exchange_info_lock.locked()
        finally:
            await replay.close()

    asyncio.run(exercise())


def test_identity_change_only_resets_affected_contract_coverage(monkeypatch):
    async def exercise():
        replay = StreamReplay(monkeypatch)
        try:
            await replay.start()
            for second in range(240):
                await replay.tick(second, liquidations=False)
            replay.payload = metadata(SYMBOL, UNICODE, "ＢＴＣUSDT")
            await replay.tick(240, liquidations=False)
            assert all("collision" in s.evidence.identity_reason(SYMBOL) for s in replay.services)
            assert all(s.evidence.identity_reason(UNICODE) is None for s in replay.services)
            replay.payload = metadata(SYMBOL, UNICODE)
            for second in range(241, 1381):
                await replay.tick(second, liquidations=False)
                flow, _, liq = replay.services
                if second >= 900:
                    assert flow.snapshot(UNICODE).status == liq.snapshot(UNICODE).status == ContextStatus.VERIFIED
                    assert (flow.snapshot(SYMBOL).status == ContextStatus.VERIFIED) is (second >= 1380)
                    assert (liq.snapshot(SYMBOL).status == ContextStatus.VERIFIED) is (second >= 1380)
            assert replay.services[1].snapshot(SYMBOL).synchronized
            assert len(replay.depth_client.requests) == 3
            assert all(t.connects == 1 and len(t.connection.sent) == 1 for t in replay.transports)
        finally:
            await replay.close()

    asyncio.run(exercise())


@pytest.mark.parametrize("offset,reason", [(1, "exchange_metadata_stale_or_future"),
    (-1, "metadata_acquisition_regressed")])
def test_invalid_acquisition_chronology_is_not_promoted(offset, reason):
    evidence = StreamEvidence(lambda: NOW)
    evidence.set_metadata(metadata(SYMBOL, UNICODE), NOW)
    evidence.set_metadata(metadata(SYMBOL, UNICODE), NOW + timedelta(seconds=offset))
    assert evidence.identity_reason(SYMBOL) == reason
    assert evidence.health({SYMBOL})["metadata_status"] != "VERIFIED"


def test_conflicting_identity_at_the_same_acquisition_is_not_a_fresh_snapshot():
    evidence = StreamEvidence(lambda: NOW)
    evidence.set_metadata(metadata(SYMBOL, UNICODE), NOW)
    evidence.set_metadata(metadata(SYMBOL), NOW)
    assert evidence.identity_reason(SYMBOL) == "conflicting_metadata_acquisition"


@pytest.mark.parametrize("offset", [-31, 1])
def test_stale_and_future_market_events_do_not_enter_collectors(monkeypatch, offset):
    from tests.test_microstructure_flow import _payload as trade
    from tests.test_liquidation_flow import _payload as liquidation
    from tests.test_order_book_liquidity import _event_payload as depth

    async def exercise():
        replay = StreamReplay(monkeypatch)
        try:
            await replay.start()
            event_at = replay.clock() + timedelta(seconds=offset)
            payloads = (trade(1, event_at), depth(event_ms=int(event_at.timestamp() * 1000)), liquidation(event_at))
            for service, payload in zip(replay.services, payloads):
                assert not service._handle_payload(payload)
                assert service.snapshot(SYMBOL).status != ContextStatus.VERIFIED
                assert service.health()["rejected_event_time_count"] == 1
            assert not replay.services[1]._books[SYMBOL].synchronized
        finally:
            await replay.close()

    asyncio.run(exercise())
