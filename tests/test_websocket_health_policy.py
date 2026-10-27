from __future__ import annotations

import asyncio
import logging
import socket
import ssl
from datetime import timedelta
from typing import Any

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidHandshake, InvalidStatus, ProxyError
from websockets.frames import Close
from websockets.http11 import Response

from app.microstructure.reconnect import FeedRecovery, close_connection
from app.microstructure.websocket_protocol import (
    ProviderShutdownError, SubscriptionRejectedError, failure_facts, is_control_message,
)
from app.microstructure.order_book_service import BinanceDepthSnapshotClient, OrderBookLiquidityService
from app.microstructure.service import MicrostructureFlowService
from app.microstructure.liquidation_service import LiquidationFlowService
from tests.test_websocket_recovery import _SnapshotClient
from tests.test_order_book_liquidity import NOW, _event_payload
from tests.test_microstructure_flow import _payload as _trade_payload
from tests.test_liquidation_flow import _payload as _liquidation_payload


def test_disconnect_logs_are_bounded_escalate_and_recover_only_on_sustained_frames(caplog) -> None:
    caplog.set_level(logging.INFO)
    clock = [0.0]
    recovery = FeedRecovery("fixture", 1, 30, monotonic=lambda: clock[0], jitter=lambda bound: bound)
    recovery.connected()
    assert recovery.health()["connection_state"] == "CONNECTED_UNVERIFIED"
    for i in range(50):
        clock[0] = float(i)
        recovery.failed(ConnectionClosedError(None, None), "receive")
        recovery.connected()
    disconnects = [r for r in caplog.records if "disconnected" in r.message]
    assert len(disconnects) == 2
    assert [r.levelno for r in disconnects] == [logging.INFO, logging.WARNING]
    assert len([r for r in caplog.records if "connected;" in r.message]) == 1
    assert recovery.health()["consecutive_connection_failures"] == 50
    assert recovery.health()["suppressed_disconnect_logs"] == 48
    recovery.market_event()
    assert recovery.failures == 50
    assert recovery.health()["connection_state"] == "RECEIVING_UNSTABLE"
    clock[0] = 110.0
    recovery.failed(TimeoutError(), "receive")
    assert len([r for r in caplog.records if "disconnected" in r.message]) == 3
    recovery.connected()
    clock[0] += 61
    recovery.market_event()
    assert recovery.failures == 0
    assert recovery.health()["connection_state"] == "RECEIVING"
    assert len([r for r in caplog.records if "recovered after" in r.message]) == 1
    recovery.market_event()
    assert len([r for r in caplog.records if "recovered after" in r.message]) == 1


@pytest.mark.parametrize("exc,phase,category", [
    (socket.gaierror("SENSITIVE"), "connect", "dns_failure"),
    (ConnectionRefusedError("SENSITIVE"), "connect", "tcp_failure"),
    (ConnectionResetError("SENSITIVE"), "receive", "network_failure"),
    (ProxyError("SENSITIVE"), "connect", "proxy_failure"),
    (ssl.SSLCertVerificationError("SENSITIVE"), "connect", "tls_certificate_failure"),
    (ssl.SSLError("SENSITIVE"), "connect", "tls_failure"),
    (InvalidStatus(Response(404, "Not Found", Headers(), b"SENSITIVE")), "connect", "upgrade_rejected"),
    (InvalidHandshake("SENSITIVE"), "connect", "protocol_failure"),
    (TimeoutError("SENSITIVE"), "connect", "handshake_timeout"),
    (TimeoutError("SENSITIVE"), "receive", "receive_timeout"),
    (ConnectionClosedError(None, None), "receive", "abrupt_close"),
    (ConnectionClosedError(Close(1012, "restart"), None), "receive", "peer_close"),
    (ConnectionClosedError(None, Close(1011, "keepalive ping timeout")), "receive", "heartbeat_timeout"),
    (SubscriptionRejectedError("SENSITIVE"), "receive", "subscription_rejected"),
    (ProviderShutdownError("SENSITIVE"), "receive", "provider_shutdown"),
])
def test_error_classification_is_explicit_and_does_not_log_raw_exception(exc, phase, category, caplog) -> None:
    caplog.set_level(logging.INFO)
    recovery = FeedRecovery("fixture", 1, 30)
    recovery.failed(exc, phase)
    assert recovery.health()["last_failure"]["category"] == category
    assert "SENSITIVE" not in caplog.text
    assert "SENSITIVE" not in str(failure_facts(exc, phase))
    if category in {"subscription_rejected", "tls_certificate_failure", "protocol_failure", "upgrade_rejected", "proxy_failure"}:
        assert caplog.records[-1].levelno == logging.WARNING


def test_new_critical_category_is_visible_immediately_during_rate_limited_outage(caplog) -> None:
    caplog.set_level(logging.INFO)
    recovery = FeedRecovery("fixture", 1, 30, monotonic=lambda: 0)
    for _ in range(3):
        recovery.failed(ConnectionError(), "connect")
    assert caplog.records[-1].levelno == logging.WARNING
    recovery.failed(ssl.SSLCertVerificationError(), "connect")
    assert "tls_certificate_failure" in caplog.records[-1].message
    count = len(caplog.records)
    for _ in range(50):
        recovery.failed(ssl.SSLCertVerificationError(), "connect")
    assert len(caplog.records) == count
    recovery.failed(SubscriptionRejectedError(), "receive")
    assert "subscription_rejected" in caplog.records[-1].message


@pytest.mark.parametrize("payload", [
    '{"code":2,"msg":"SENSITIVE"}',
    b'{"id":1,"error":{"code":2,"msg":"SENSITIVE"}}',
])
def test_control_errors_without_id_or_with_nested_error_cannot_be_ignored(payload) -> None:
    with pytest.raises(SubscriptionRejectedError, match="code=2") as caught:
        is_control_message(payload)
    assert "SENSITIVE" not in str(caught.value)


def test_owned_snapshot_client_reopens_after_stop_without_changing_public_request(monkeypatch) -> None:
    async def scenario() -> None:
        original = httpx.AsyncClient
        clients = []
        requests = []

        def response(request):
            requests.append(request)
            return httpx.Response(200, json={"fixture": True})

        def factory(**kwargs):
            client = original(transport=httpx.MockTransport(response), **kwargs)
            clients.append(client)
            return client

        monkeypatch.setattr("app.microstructure.order_book_service.httpx.AsyncClient", factory)
        client = BinanceDepthSnapshotClient()
        assert await client.fetch("BTCUSDT", 500) == {"fixture": True}
        await client.aclose()
        assert clients[0].is_closed
        assert await client.fetch("BTCUSDT", 500) == {"fixture": True}
        await client.aclose()
        assert len(clients) == 2 and all(c.is_closed for c in clients)
        assert all(r.url.host == "fapi.binance.com" and r.url.path == "/fapi/v1/depth" for r in requests)
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [MicrostructureFlowService, LiquidationFlowService, OrderBookLiquidityService])
def test_repeated_concurrent_lifecycle_operations_never_overlap_sockets(kind: Any) -> None:
    async def scenario() -> None:
        class Transport:
            calls = 0
            active = 0
            peak = 0
            async def connect(self):
                self.calls += 1
                self.active += 1
                self.peak = max(self.peak, self.active)
                transport = self
                class Connection:
                    closed = False
                    async def send(self, payload):
                        pass
                    async def recv(self):
                        await asyncio.Future()
                    async def close(self):
                        await asyncio.sleep(0)
                        if not self.closed:
                            transport.active -= 1
                            self.closed = True
                return Connection()

        transport = Transport()
        kwargs = {"snapshot_client": _SnapshotClient()} if kind is OrderBookLiquidityService else {}
        service = kind(transport=transport, **kwargs)
        for _ in range(5):
            await asyncio.gather(*(service.start(["BTCUSDT"]) for _ in range(10)))
            await asyncio.sleep(0)
            assert transport.active == 1
            await asyncio.gather(*(service.stop() for _ in range(10)))
            assert transport.active == 0
            assert not service.running
        assert transport.peak == 1
        assert transport.calls == 5
    asyncio.run(scenario())


def test_cancellation_while_closing_does_not_abandon_owned_socket() -> None:
    async def scenario() -> None:
        closing, release = asyncio.Event(), asyncio.Event()
        class Connection:
            closed = False
            async def close(self):
                closing.set()
                await release.wait()
                self.closed = True
        connection = Connection()
        owner = asyncio.create_task(close_connection(connection))
        await closing.wait()
        owner.cancel()
        await asyncio.sleep(0)
        assert not owner.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await owner
        assert connection.closed
    asyncio.run(scenario())


@pytest.mark.parametrize("base,maximum,reset", [(1, float("inf"), 60), (1, 30, float("nan")), (-1, 30, 60), (2, 1, 60)])
def test_recovery_limits_refuse_unbounded_or_invalid_configuration(base, maximum, reset) -> None:
    with pytest.raises(ValueError):
        FeedRecovery("fixture", base, maximum, reset_after=reset)


@pytest.mark.parametrize("kind", [MicrostructureFlowService, LiquidationFlowService, OrderBookLiquidityService])
@pytest.mark.parametrize("symbol", ["BTCUSDC", "BTCUSDT_261225"])
def test_non_target_contracts_cannot_become_usdt_perpetual_evidence(kind, symbol) -> None:
    async def scenario():
        kwargs = {"snapshot_client": _SnapshotClient()} if kind is OrderBookLiquidityService else {}
        service = kind(clock=lambda: NOW, **kwargs)
        await service.reconcile_symbols([symbol])
        if kind is MicrostructureFlowService:
            payload = _trade_payload(1, NOW, symbol=symbol)
        elif kind is LiquidationFlowService:
            payload = _liquidation_payload(NOW, symbol=symbol)
        else:
            payload = _event_payload(symbol=symbol)
        assert service._handle_payload(payload) is False
        assert service.health()["wrong_contract_event_count"] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [LiquidationFlowService, OrderBookLiquidityService])
def test_pair_symbol_mismatch_is_quarantined(kind) -> None:
    async def scenario():
        kwargs = {"snapshot_client": _SnapshotClient()} if kind is OrderBookLiquidityService else {}
        service = kind(clock=lambda: NOW, **kwargs)
        await service.reconcile_symbols(["BTCUSDT"])
        payload = _liquidation_payload(NOW) if kind is LiquidationFlowService else _event_payload()
        payload["ps"] = "ETHUSDT"
        assert service._handle_payload(payload) is False
        assert service.health()["wrong_contract_event_count"] == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [MicrostructureFlowService, LiquidationFlowService, OrderBookLiquidityService])
def test_stale_payload_is_not_recovery_progress(kind) -> None:
    async def scenario():
        kwargs = {"snapshot_client": _SnapshotClient()} if kind is OrderBookLiquidityService else {}
        service = kind(clock=lambda: NOW + timedelta(seconds=120), **kwargs)
        await service.reconcile_symbols(["BTCUSDT"])
        if kind is MicrostructureFlowService:
            payload = _trade_payload(1, NOW)
        elif kind is LiquidationFlowService:
            payload = _liquidation_payload(NOW)
        else:
            payload = _event_payload()
        assert service._handle_payload(payload) is False
        assert service.health()["accepted_market_event_count"] == 0
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [MicrostructureFlowService, LiquidationFlowService, OrderBookLiquidityService])
def test_zero_delay_injected_policy_still_yields_and_cancels(kind) -> None:
    async def scenario():
        class Transport:
            calls = 0
            async def connect(self):
                self.calls += 1
                raise ConnectionError("fixture offline")
        transport = Transport()
        kwargs = {"snapshot_client": _SnapshotClient()} if kind is OrderBookLiquidityService else {}
        service = kind(transport=transport, reconnect_base_seconds=0, reconnect_max_seconds=0, **kwargs)
        await service.start(["BTCUSDT"])
        for _ in range(5):
            await asyncio.sleep(0)
        await asyncio.wait_for(service.stop(), 1)
        assert transport.calls >= 1
        calls = transport.calls
        await asyncio.sleep(0)
        assert transport.calls == calls
        assert not service.running
    asyncio.run(scenario())
