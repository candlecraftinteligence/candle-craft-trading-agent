from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

import pytest

from app.microstructure.order_book import parse_binance_depth_snapshot
from app.microstructure.order_book_service import OrderBookLiquidityService


pytestmark = pytest.mark.no_auto_epoch
NOW = datetime(2026, 10, 26, 12, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


def _depth() -> dict[str, Any]:
    return {"e": "depthUpdate", "E": NOW_MS, "T": NOW_MS - 1, "s": "BTCUSDT",
            "U": 100, "u": 101, "pu": 99, "b": [["99", "2"]],
            "a": [["101", "2"]], "ps": "BTCUSDT", "st": 1}


class _Clock:
    value = 0.0

    def __call__(self) -> float:
        return self.value


class _NoSnapshots:
    async def fetch(self, symbol: str, limit: int) -> Mapping[str, Any]:
        raise AssertionError("connection tests must not request REST snapshots")

    async def aclose(self) -> None:
        pass


class _Connection:
    def __init__(self, messages=(), *, clock=None):
        self.messages = list(messages)
        self.clock = clock
        self.closed = False
        self.wait = asyncio.Event()

    async def recv(self):
        if not self.messages:
            await self.wait.wait()
            raise AssertionError("unreachable")
        elapsed, payload = self.messages.pop(0)
        if self.clock is not None:
            self.clock.value += elapsed
        if isinstance(payload, Exception):
            raise payload
        return payload

    async def send(self, payload: str) -> None:
        pass

    async def close(self) -> None:
        self.closed = True


class _Transport:
    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.calls = 0

    async def connect(self):
        self.calls += 1
        outcome = next(self.outcomes)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _service(transport, **kwargs):
    service = OrderBookLiquidityService(
        transport=transport, snapshot_client=_NoSnapshots(), clock=lambda: NOW, **kwargs,
    )
    # Exercise the connection owner with real books, without starting REST workers.
    service._schedule_bootstrap = lambda symbol: None
    return service


def test_flapping_connections_back_off_instead_of_resetting_on_each_open():
    async def scenario():
        connections = [_Connection([(0, ConnectionResetError("synthetic TCP loss"))])
                       for _ in range(7)]
        delays = []

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == len(connections):
                service._stop_event.set()

        service = _service(_Transport(connections), sleep=sleep)
        await service.reconcile_symbols(["BTCUSDT"])
        await service._run_forever()
        assert delays == [1, 2, 4, 8, 16, 30, 30]
        assert all(c.closed for c in connections)
        assert service.snapshot("BTCUSDT").synchronized is False

    asyncio.run(scenario())


def test_handshake_failures_use_bounded_backoff_and_report_the_phase(caplog):
    async def scenario():
        delays = []

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 3:
                service._stop_event.set()

        service = _service(_Transport([TimeoutError("opening handshake") for _ in range(3)]),
                           sleep=sleep)
        await service._run_forever()
        assert delays == [1, 2, 4]
        assert service.health()["consecutive_connection_failures"] == 3
        assert service.health()["reconnect_delay_seconds"] == 4

    asyncio.run(scenario())
    assert "phase=connect" in caplog.text
    assert "retry_in=4.0s" in caplog.text


@pytest.mark.parametrize("payload,expected", [
    (_depth(), [1, 1]),
    ({"id": 1, "result": None}, [1, 2]),
    ("malformed JSON", [1, 2]),
    ({**_depth(), "st": 2}, [1, 2]),
    ({**_depth(), "s": "ETHUSDT", "ps": "ETHUSDT"}, [1, 2]),
])
def test_only_sustained_subscribed_usdm_depth_resets_failure_backoff(payload, expected):
    async def scenario():
        clock = _Clock()
        first = _Connection([(0, ConnectionResetError("first loss"))], clock=clock)
        second = _Connection([(61, payload), (0, ConnectionResetError("second loss"))],
                             clock=clock)
        delays = []

        async def sleep(delay):
            delays.append(delay)
            if len(delays) == 2:
                service._stop_event.set()

        service = _service(_Transport([first, second]), sleep=sleep, monotonic=clock,
                           reconnect_reset_seconds=60)
        await service.reconcile_symbols(["BTCUSDT"])
        await service._run_forever()
        assert delays == expected

    asyncio.run(scenario())


def test_silent_connection_times_out_and_reconnects_without_external_traffic(caplog):
    async def scenario():
        connection = _Connection()
        delays = []

        async def sleep(delay):
            delays.append(delay)
            service._stop_event.set()

        service = _service(_Transport([connection]), sleep=sleep, receive_timeout_seconds=0.01)
        await service.reconcile_symbols(["BTCUSDT"])
        await asyncio.wait_for(service._run_forever(), timeout=5)
        assert delays == [1]
        assert connection.closed
        assert not service._books["BTCUSDT"].stream_connected
        assert service.health()["disconnect_count"] == 1
        assert service.health()["last_error"].startswith("TimeoutError:")

    asyncio.run(scenario())
    assert "phase=receive" in caplog.text


def test_disconnect_revokes_trusted_book_before_waiting_for_socket_close():
    async def scenario():
        close_started, allow_close = asyncio.Event(), asyncio.Event()

        class SynchronizedThenDrop(_Connection):
            async def recv(self):
                book = service._books["BTCUSDT"]
                service._handle_payload(_depth())
                book.install_snapshot(parse_binance_depth_snapshot({
                    "lastUpdateId": 100, "E": NOW_MS, "T": NOW_MS,
                    "bids": [["99", "1"]], "asks": [["101", "1"]],
                }), received_at=NOW)
                assert book.synchronized
                raise ConnectionResetError("synthetic TCP loss")

            async def close(self):
                close_started.set()
                await allow_close.wait()
                self.closed = True

        connection = SynchronizedThenDrop()
        service = _service(_Transport([connection]))
        await service.reconcile_symbols(["BTCUSDT"])
        task = asyncio.create_task(service._run_forever())
        try:
            await asyncio.wait_for(close_started.wait(), timeout=5)
            book = service._books["BTCUSDT"]
            assert not book.synchronized
            assert not book.stream_connected
            assert not book.bids and not book.asks
        finally:
            service._stop_event.set()
            allow_close.set()
            await asyncio.wait_for(task, timeout=5)

    asyncio.run(scenario())


def test_stop_cancels_reconnect_wait_without_opening_another_socket():
    async def scenario():
        waiting = asyncio.Event()

        async def sleep(delay):
            waiting.set()
            await asyncio.Event().wait()

        connection = _Connection([(0, ConnectionResetError("synthetic TCP loss"))])
        transport = _Transport([connection])
        service = _service(transport, sleep=sleep)
        await service.start(["BTCUSDT"])
        await asyncio.wait_for(waiting.wait(), timeout=5)
        await asyncio.wait_for(service.stop(), timeout=5)
        assert transport.calls == 1
        assert connection.closed
        assert not service.running

    asyncio.run(scenario())


@pytest.mark.parametrize("field", ["receive_timeout_seconds", "reconnect_reset_seconds"])
@pytest.mark.parametrize("value", [0, -1, float("inf"), float("nan")])
def test_connection_recovery_time_limits_must_be_positive_and_finite(field, value):
    with pytest.raises(ValueError):
        _service(_Transport([]), **{field: value})


def test_default_order_book_handshake_and_recovery_limits_are_explicit():
    service = OrderBookLiquidityService(snapshot_client=_NoSnapshots())
    assert service.transport.open_timeout_seconds == 20
    assert service.receive_timeout_seconds == 30
    assert service.reconnect_reset_seconds == 60
    assert service.transport.max_message_bytes == 1024 * 1024
    assert service.transport.max_queue == 4
    assert service.stale_after_seconds == 5
