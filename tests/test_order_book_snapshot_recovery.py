from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any

from app.context.models import ContextStatus
from app.data.exceptions import ExchangeRateLimitError, ExchangeTimeoutError
from app.microstructure.order_book_service import OrderBookLiquidityService


NOW = datetime(2026, 8, 28, 12, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


def _snapshot_payload(*, update_id: int = 100) -> dict[str, Any]:
    return {
        "lastUpdateId": update_id,
        "E": NOW_MS - 10,
        "T": NOW_MS - 11,
        "bids": [["99.99", "1"], ["99.00", "2"]],
        "asks": [["100.01", "1"], ["101.00", "2"]],
    }


def _bridge_payload(*, symbol: str = "BTCUSDT") -> dict[str, Any]:
    return {
        "e": "depthUpdate",
        "E": NOW_MS,
        "T": NOW_MS - 1,
        "s": symbol,
        "U": 100,
        "u": 101,
        "pu": 99,
        "b": [["99.99", "1.5"]],
        "a": [["100.01", "1.5"]],
        "ps": symbol,
        "st": 1,
    }


class _SequenceSnapshotClient:
    def __init__(self, outcomes: list[Mapping[str, Any] | Exception]) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    async def fetch(self, symbol: str, limit: int) -> Mapping[str, Any]:
        self.calls.append((symbol, limit))
        outcome = self.outcomes.pop(0) if self.outcomes else _snapshot_payload()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed = True


class _PerSymbolSnapshotClient:
    def __init__(
        self,
        outcomes: Mapping[str, list[Mapping[str, Any] | Exception]],
    ) -> None:
        self.outcomes = {symbol: list(items) for symbol, items in outcomes.items()}
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    async def fetch(self, symbol: str, limit: int) -> Mapping[str, Any]:
        self.calls.append((symbol, limit))
        choices = self.outcomes.setdefault(symbol, [])
        outcome = choices.pop(0) if choices else _snapshot_payload()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def aclose(self) -> None:
        self.closed = True


class _IdleConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._incoming: asyncio.Queue[str | Exception] = asyncio.Queue()

    async def recv(self) -> str:
        item = await self._incoming.get()
        if isinstance(item, Exception):
            raise item
        return item

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True

    def disconnect(self) -> None:
        self._incoming.put_nowait(RuntimeError("test disconnect"))


class _StaticTransport:
    def __init__(self, connection: _IdleConnection) -> None:
        self.connection = connection
        self.max_queue = 32
        self.connect_count = 0

    async def connect(self) -> _IdleConnection:
        self.connect_count += 1
        return self.connection


class _RecordingSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)


class _GateSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []
        self.started = asyncio.Event()
        self._release = asyncio.Event()

    async def __call__(self, delay: float) -> None:
        self.delays.append(delay)
        self.started.set()
        await self._release.wait()
        self._release.clear()
        self.started.clear()

    def release(self) -> None:
        self._release.set()


async def _wait_for(predicate: Callable[[], bool], *, turns: int = 500) -> None:
    for _ in range(turns):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition did not become true")


def _service(
    *,
    client: Any,
    connection: _IdleConnection,
    sleep: Any = asyncio.sleep,
    attempts: int = 1,
    reconnect_base: float = 1.0,
    reconnect_max: float = 4.0,
) -> tuple[OrderBookLiquidityService, _StaticTransport]:
    transport = _StaticTransport(connection)
    return (
        OrderBookLiquidityService(
            transport=transport,
            snapshot_client=client,
            bootstrap_attempts=attempts,
            bootstrap_min_interval_seconds=0,
            bootstrap_backoff_seconds=0,
            bootstrap_jitter_seconds=0,
            reconnect_base_seconds=reconnect_base,
            reconnect_max_seconds=reconnect_max,
            clock=lambda: NOW,
            sleep=sleep,
            jitter=lambda _maximum: 0.0,
        ),
        transport,
    )


def test_successful_bootstrap_cycle_does_not_schedule_recovery() -> None:
    asyncio.run(_assert_successful_bootstrap_cycle_does_not_schedule_recovery())


async def _assert_successful_bootstrap_cycle_does_not_schedule_recovery() -> None:
    connection = _IdleConnection()
    sleep = _RecordingSleep()
    client = _SequenceSnapshotClient([_snapshot_payload()])
    service, _ = _service(client=client, connection=connection, sleep=sleep)
    await service.start([])
    await _wait_for(lambda: len(client.calls) == 1)
    await _wait_for(lambda: service.health()["active_bootstrap_count"] == 0)
    assert len(client.calls) == 1
    assert sleep.delays == []
    assert service.health()["snapshot_recovery_scheduled_count"] == 0
    assert service.health()["snapshot_recovery_attempt_count"] == 0
    await service.stop()


def test_quiet_symbol_recovers_without_buffer_overflow_or_ws_reconnect() -> None:
    asyncio.run(_assert_quiet_symbol_recovers_without_buffer_overflow_or_ws_reconnect())


async def _assert_quiet_symbol_recovers_without_buffer_overflow_or_ws_reconnect() -> None:
    connection = _IdleConnection()
    sleep = _GateSleep()
    client = _SequenceSnapshotClient(
        [ExchangeTimeoutError("temporary outage"), _snapshot_payload()]
    )
    service, transport = _service(client=client, connection=connection, sleep=sleep)
    await service.start([])
    await _wait_for(lambda: sleep.started.is_set())

    failed = service.snapshot("BTCUSDT")
    assert failed.status == ContextStatus.ERROR
    assert failed.reason == "snapshot_failed"
    assert failed.synchronized is False
    assert failed.best_bid is None
    assert failed.best_ask is None
    assert len(client.calls) == 1
    assert transport.connect_count == 1
    assert service.health()["active_bootstrap_count"] == 1
    assert service.health()["snapshot_recovery_scheduled_count"] == 1

    sleep.release()
    await _wait_for(lambda: len(client.calls) == 2)
    await _wait_for(lambda: service.health()["active_bootstrap_count"] == 0)
    assert transport.connect_count == 1
    assert service.snapshot("BTCUSDT").status == ContextStatus.UNAVAILABLE

    service._handle_payload(_bridge_payload())
    recovered = service.snapshot("BTCUSDT")
    assert recovered.status == ContextStatus.VERIFIED
    assert recovered.synchronized is True
    assert recovered.liquidity_below_context()["usage"] == "research_only"
    assert recovered.liquidity_above_context()["usage"] == "research_only"
    assert service.health()["snapshot_recovery_attempt_count"] == 1
    await service.stop()


def test_exhausted_cycle_marks_failure_and_stop_cancels_pending_recovery() -> None:
    asyncio.run(_assert_exhausted_cycle_marks_failure_and_stop_cancels_pending_recovery())


async def _assert_exhausted_cycle_marks_failure_and_stop_cancels_pending_recovery() -> None:
    connection = _IdleConnection()
    sleep = _GateSleep()
    client = _SequenceSnapshotClient(
        [ExchangeTimeoutError("one"), ExchangeTimeoutError("two")]
    )
    service, _ = _service(client=client, connection=connection, sleep=sleep, attempts=2)
    await service.start([])
    await _wait_for(lambda: sleep.started.is_set())
    assert len(client.calls) == 2
    assert service.snapshot("BTCUSDT").reason == "snapshot_failed"
    assert service.health()["snapshot_failure_count"] == 1
    assert service.health()["snapshot_recovery_scheduled_count"] == 1
    assert service.health()["active_bootstrap_count"] == 1
    await service.stop()
    assert service.health()["active_bootstrap_count"] == 0
    assert len(client.calls) == 2
    assert client.closed is True


def test_repeated_failures_use_bounded_exponential_recovery() -> None:
    asyncio.run(_assert_repeated_failures_use_bounded_exponential_recovery())


async def _assert_repeated_failures_use_bounded_exponential_recovery() -> None:
    connection = _IdleConnection()
    sleep = _RecordingSleep()
    client = _SequenceSnapshotClient(
        [
            ExchangeTimeoutError("one"),
            ExchangeTimeoutError("two"),
            ExchangeTimeoutError("three"),
            ExchangeTimeoutError("four"),
            _snapshot_payload(),
        ]
    )
    service, _ = _service(client=client, connection=connection, sleep=sleep)
    await service.start([])
    await _wait_for(lambda: len(client.calls) == 5)
    await _wait_for(lambda: service.health()["active_bootstrap_count"] == 0)
    assert len(client.calls) == 5
    assert sleep.delays == [1.0, 2.0, 4.0, 4.0]
    assert all(delay > 0 for delay in sleep.delays)
    assert service.health()["snapshot_failure_count"] == 4
    assert service.health()["snapshot_recovery_scheduled_count"] == 4
    assert service.health()["snapshot_recovery_attempt_count"] == 4
    await service.stop()


def test_snapshot_recovery_respects_final_429_retry_after() -> None:
    asyncio.run(_assert_snapshot_recovery_respects_final_429_retry_after())


async def _assert_snapshot_recovery_respects_final_429_retry_after() -> None:
    connection = _IdleConnection()
    sleep = _RecordingSleep()
    client = _SequenceSnapshotClient(
        [
            ExchangeRateLimitError("limited", status_code=429, retry_after=7),
            _snapshot_payload(),
        ]
    )
    service, _ = _service(client=client, connection=connection, sleep=sleep)
    await service.start([])
    await _wait_for(lambda: len(client.calls) == 2)
    await _wait_for(lambda: service.health()["active_bootstrap_count"] == 0)
    assert len(client.calls) == 2
    assert sleep.delays == [7.0]
    await service.stop()


def test_snapshot_recovery_still_uses_shared_minimum_request_interval() -> None:
    asyncio.run(_assert_snapshot_recovery_still_uses_shared_minimum_request_interval())


async def _assert_snapshot_recovery_still_uses_shared_minimum_request_interval() -> None:
    connection = _IdleConnection()
    transport = _StaticTransport(connection)
    sleep = _RecordingSleep()
    client = _SequenceSnapshotClient(
        [ExchangeTimeoutError("temporary"), _snapshot_payload()]
    )
    service = OrderBookLiquidityService(
        transport=transport,
        snapshot_client=client,
        bootstrap_attempts=1,
        bootstrap_min_interval_seconds=0.5,
        bootstrap_backoff_seconds=0,
        bootstrap_jitter_seconds=0,
        reconnect_base_seconds=1,
        reconnect_max_seconds=4,
        clock=lambda: NOW,
        monotonic=lambda: 0.0,
        sleep=sleep,
        jitter=lambda _maximum: 0.0,
    )
    await service.start([])
    await _wait_for(lambda: len(client.calls) == 2)
    await _wait_for(lambda: service.health()["active_bootstrap_count"] == 0)
    assert sleep.delays == [1.0, 0.5]
    assert service.health()["snapshot_request_count"] == 2
    assert service.health()["snapshot_recovery_attempt_count"] == 1
    await service.stop()


def test_symbol_unsubscribe_cancels_pending_recovery() -> None:
    asyncio.run(_assert_symbol_unsubscribe_cancels_pending_recovery())


async def _assert_symbol_unsubscribe_cancels_pending_recovery() -> None:
    connection = _IdleConnection()
    sleep = _GateSleep()
    client = _PerSymbolSnapshotClient(
        {
            "BTCUSDT": [_snapshot_payload()],
            "ETHUSDT": [ExchangeTimeoutError("temporary")],
        }
    )
    service, _ = _service(client=client, connection=connection, sleep=sleep)
    await service.start(["ETHUSDT"])
    await _wait_for(lambda: sleep.started.is_set())
    eth_calls = len([call for call in client.calls if call[0] == "ETHUSDT"])
    assert eth_calls == 1
    await service.reconcile_symbols([])
    await _wait_for(lambda: service.health()["active_bootstrap_count"] == 0)
    sleep.release()
    await asyncio.sleep(0)
    assert len([call for call in client.calls if call[0] == "ETHUSDT"]) == 1
    assert service.snapshot("ETHUSDT").reason == "symbol_not_subscribed"
    await service.stop()


def test_ws_disconnect_cancels_recovery_without_stale_snapshot_request() -> None:
    asyncio.run(_assert_ws_disconnect_cancels_recovery_without_stale_snapshot_request())


async def _assert_ws_disconnect_cancels_recovery_without_stale_snapshot_request() -> None:
    connection = _IdleConnection()
    sleep = _GateSleep()
    client = _SequenceSnapshotClient([ExchangeTimeoutError("temporary")])
    service, transport = _service(
        client=client,
        connection=connection,
        sleep=sleep,
        reconnect_base=10,
        reconnect_max=10,
    )
    await service.start([])
    await _wait_for(lambda: sleep.started.is_set())
    assert len(client.calls) == 1
    connection.disconnect()
    await _wait_for(
        lambda: not service.health()["connected"]
        and service.health()["active_bootstrap_count"] == 0
    )
    await _wait_for(lambda: service.snapshot("BTCUSDT").reason == "stream_disconnected")
    assert len(client.calls) == 1
    assert transport.connect_count == 1
    assert service.snapshot("BTCUSDT").reason == "stream_disconnected"
    await service.stop()


def test_repeated_schedule_requests_keep_one_task_per_symbol() -> None:
    asyncio.run(_assert_repeated_schedule_requests_keep_one_task_per_symbol())


async def _assert_repeated_schedule_requests_keep_one_task_per_symbol() -> None:
    connection = _IdleConnection()
    sleep = _GateSleep()
    client = _SequenceSnapshotClient([ExchangeTimeoutError("temporary")])
    service, _ = _service(client=client, connection=connection, sleep=sleep)
    await service.start([])
    await _wait_for(lambda: sleep.started.is_set())
    original = service._bootstrap_tasks["BTCUSDT"]
    for _ in range(20):
        service._schedule_bootstrap("BTCUSDT")
    assert service.health()["active_bootstrap_count"] == 1
    assert service._bootstrap_tasks["BTCUSDT"] is original
    assert len(client.calls) == 1
    await service.stop()
