from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest
from websockets.exceptions import ConnectionClosedError

from app.context.models import ContextStatus
from app.microstructure.liquidation_service import LiquidationFlowService
from app.microstructure.order_book import BookIngestOutcome, parse_binance_depth_event, parse_binance_depth_snapshot
from app.microstructure.order_book_service import OrderBookLiquidityService
from app.microstructure.reconnect import FeedRecovery
from app.microstructure.service import MicrostructureFlowService
from tests.test_order_book_liquidity import NOW, _book, _event_payload, _snapshot_payload


def test_backoff_survives_handshakes_and_requires_sustained_market_progress() -> None:
    now = [0.0]
    backoff = FeedRecovery("test", 1, 4, monotonic=lambda: now[0], jitter=lambda bound: bound)
    delays = []
    for _ in range(6):
        backoff.connected()
        now[0] += 1
        backoff.market_event()
        delays.append(backoff.failed(ConnectionError("synthetic"), "receive"))
    assert delays == [1, 2, 4, 4, 4, 4]
    backoff.connected()
    now[0] += 61
    # Time or another handshake without an accepted market event doesn't reset.
    assert backoff.failed(ConnectionError("synthetic"), "receive") == 4
    backoff.market_event()
    assert backoff.failed(ConnectionError("synthetic"), "receive") == 1


def test_backoff_jitter_is_bounded_even_at_ceiling() -> None:
    low = FeedRecovery("test", 1, 4, jitter=lambda _: 0)
    high = FeedRecovery("test", 1, 4, jitter=lambda bound: bound)
    assert [low.failed(ConnectionError("synthetic"), "receive") for _ in range(4)] == [0.8, 1.6, 3.2, 3.2]
    assert [high.failed(ConnectionError("synthetic"), "receive") for _ in range(4)] == [1, 2, 4, 4]


@pytest.mark.parametrize("bids,asks", [
    ([["99", "0"]], []), ([], [["101", "0"]]), ([["102", "1"]], []),
])
def test_initial_bridge_cannot_publish_empty_or_crossed_book(bids: Any, asks: Any) -> None:
    book = _book()
    book.ingest(parse_binance_depth_event(_event_payload(bids=bids, asks=asks)), received_at=NOW)
    result = book.install_snapshot(parse_binance_depth_snapshot(_snapshot_payload(
        bids=[["99", "1"]], asks=[["101", "1"]],
    )), received_at=NOW)
    assert result == BookIngestOutcome.NEEDS_RESYNC
    assert not book.synchronized
    assert book.needs_resync
    assert book.snapshot(as_of=NOW).status != ContextStatus.VERIFIED


def test_initial_bridge_remains_strict_and_stale_event_cannot_verify() -> None:
    book = _book()
    book.ingest(parse_binance_depth_event(_event_payload(first=101, final=102, previous=100)), received_at=NOW)
    assert book.install_snapshot(parse_binance_depth_snapshot(_snapshot_payload()), received_at=NOW) == BookIngestOutcome.NEEDS_RESYNC
    book.mark_connected(reconnect=True)
    book.ingest(parse_binance_depth_event(_event_payload()), received_at=NOW)
    book.install_snapshot(parse_binance_depth_snapshot(_snapshot_payload()), received_at=NOW)
    assert book.snapshot(as_of=NOW + timedelta(seconds=6)).status == ContextStatus.STALE


class _SnapshotClient:
    async def fetch(self, symbol: str, limit: int) -> Any:
        await asyncio.Future()

    async def aclose(self) -> None:
        pass


class _FlapConnection:
    def __init__(self, transport: Any) -> None:
        self.transport = transport

    async def send(self, payload: str) -> None:
        pass

    async def recv(self) -> Any:
        raise ConnectionClosedError(None, None)

    async def close(self) -> None:
        # Fail closed before cleanup can suspend the loop.
        service = self.transport.service
        assert not service.health()["connected"]
        if isinstance(service, OrderBookLiquidityService):
            assert all(not book.stream_connected for book in service._books.values())
        else:
            assert all(not agg._connected for agg in service._aggregators.values())
        self.transport.open_count -= 1


@pytest.mark.parametrize("kind", [MicrostructureFlowService, LiquidationFlowService, OrderBookLiquidityService])
@pytest.mark.parametrize("failure_mode", ["receive", "handshake"])
def test_flapping_receive_backoff_cancellation_and_single_socket(kind: Any, failure_mode: str) -> None:
    async def scenario() -> None:
        class Transport:
            calls = 0
            open_count = 0
            service: Any = None

            async def connect(self) -> Any:
                self.calls += 1
                if failure_mode == "handshake":
                    raise TimeoutError("synthetic handshake timeout")
                self.open_count += 1
                assert self.open_count == 1
                return _FlapConnection(self)

        transport = Transport()
        delays = []
        waiting = asyncio.Event()

        async def sleep(delay: float) -> None:
            delays.append(delay)
            if len(delays) == 4:
                waiting.set()
                await asyncio.Future()

        kwargs = {"snapshot_client": _SnapshotClient()} if kind is OrderBookLiquidityService else {}
        service = kind(transport=transport, clock=lambda: NOW, monotonic=lambda: 0,
                       reconnect_max_seconds=4, sleep=sleep, jitter=lambda bound: bound, **kwargs)
        transport.service = service
        await service.start(["BTCUSDT"])
        task = service._task
        await service.start(["BTCUSDT"])
        assert service._task is task
        await asyncio.wait_for(waiting.wait(), timeout=2)
        assert delays == [1, 2, 4, 4]
        assert transport.calls == 4
        assert service.snapshot("BTCUSDT").status != ContextStatus.VERIFIED
        await asyncio.wait_for(service.stop(), timeout=2)
        assert not service.running
        assert transport.open_count == 0
        assert service._connection is None
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [MicrostructureFlowService, LiquidationFlowService, OrderBookLiquidityService])
def test_subscription_rejection_is_an_error_and_ack_is_not_market_progress(kind: Any) -> None:
    kwargs = {"snapshot_client": _SnapshotClient()} if kind is OrderBookLiquidityService else {}
    service = kind(**kwargs)
    assert service._handle_payload('{"id":1,"result":null}') is False
    with pytest.raises(ConnectionError, match="binance_subscription_rejected:code=2") as caught:
        service._handle_payload('{"id":1,"code":2,"msg":"sensitive response"}')
    assert "sensitive" not in str(caught.value)


@pytest.mark.parametrize("kind", [MicrostructureFlowService, LiquidationFlowService, OrderBookLiquidityService])
def test_shutdown_invalidates_before_close_and_cannot_start_duplicate(kind: Any) -> None:
    async def scenario() -> None:
        closing = asyncio.Event()
        release = asyncio.Event()

        class Connection:
            async def send(self, payload):
                pass
            async def recv(self):
                await asyncio.Future()
            async def close(self):
                closing.set()
                await release.wait()

        class Transport:
            calls = 0
            async def connect(self):
                self.calls += 1
                return Connection()

        transport = Transport()
        kwargs = {"snapshot_client": _SnapshotClient()} if kind is OrderBookLiquidityService else {}
        service = kind(transport=transport, **kwargs)
        await service.start(["BTCUSDT"])
        await asyncio.sleep(0)
        assert service.health()["connected"]
        stop = asyncio.create_task(service.stop())
        await asyncio.wait_for(closing.wait(), 1)
        assert not service.health()["connected"]
        assert service.snapshot("BTCUSDT").status != ContextStatus.VERIFIED
        if kind is OrderBookLiquidityService:
            assert all(not book.stream_connected for book in service._books.values())
        else:
            assert all(not agg._connected for agg in service._aggregators.values())
        restart = asyncio.create_task(service.start(["BTCUSDT"]))
        await asyncio.sleep(0)
        assert not restart.done()
        assert transport.calls == 1
        release.set()
        await asyncio.wait_for(stop, 1)
        await asyncio.wait_for(restart, 1)
        await asyncio.sleep(0)
        assert transport.calls == 2
        await asyncio.wait_for(service.stop(), 1)
    asyncio.run(scenario())
