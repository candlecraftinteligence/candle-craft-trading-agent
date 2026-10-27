from __future__ import annotations

import asyncio
import json
from datetime import timedelta

import pytest
from app.context.models import ContextStatus
from app.microstructure.liquidation_service import LiquidationFlowService
from app.microstructure.service import MicrostructureFlowService
from app.microstructure.order_book_service import OrderBookLiquidityService
from tests.test_websocket_recovery import _SnapshotClient
from tests.test_liquidation_flow import BASE


class _Connection:
    def __init__(self):
        self.incoming = asyncio.Queue()
        self.sent = []

    async def send(self, payload):
        self.sent.append(json.loads(payload))

    async def recv(self):
        return await self.incoming.get()

    async def close(self):
        pass


class _Transport:
    def __init__(self, connection):
        self.connection = connection

    async def connect(self):
        return self.connection


def test_quiet_socket_without_subscription_ack_cannot_create_verified_zero() -> None:
    async def scenario():
        now = [BASE]
        connection = _Connection()
        service = LiquidationFlowService(transport=_Transport(connection), clock=lambda: now[0])
        await service.start(["BTCUSDT"])
        try:
            for _ in range(10):
                await asyncio.sleep(0)
                if connection.sent:
                    break
            assert connection.sent[0]["method"] == "SUBSCRIBE"
            now[0] += timedelta(minutes=16)
            snapshot = service.snapshot("BTCUSDT")
            assert snapshot.status != ContextStatus.VERIFIED
            assert snapshot.strategy_context() is None
        finally:
            await service.stop()
    asyncio.run(scenario())


def test_only_matching_ack_starts_coverage_and_duplicate_ack_cannot_reset_it() -> None:
    async def scenario():
        now = [BASE]
        connection = _Connection()
        service = LiquidationFlowService(transport=_Transport(connection), clock=lambda: now[0])
        await service.start(["BTCUSDT"])
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            request_id = connection.sent[0]["id"]
            service._handle_payload({"result": None, "id": request_id + 1})
            service._handle_payload({"id": request_id})
            assert not service.health()["subscription_confirmed"]
            service._handle_payload({"result": None, "id": request_id})
            assert service.health()["subscription_confirmed"]
            assert service.health()["connection_state"] == "CONNECTED_UNVERIFIED"
            assert service.snapshot("BTCUSDT").status != ContextStatus.VERIFIED
            now[0] += timedelta(minutes=16)
            service._handle_payload({"result": None, "id": request_id})
            # This is an acknowledged, continuously connected quiet interval,
            # unlike an unacknowledged or disconnected/missing observation.
            assert service.snapshot("BTCUSDT").status == ContextStatus.VERIFIED
            await service.reconcile_symbols(["ETHUSDT"])
            assert service.snapshot("ETHUSDT").status != ContextStatus.VERIFIED
        finally:
            await service.stop()
        assert not service.health()["subscription_confirmed"]
        assert service.snapshot("BTCUSDT").status != ContextStatus.VERIFIED
    asyncio.run(scenario())


def test_missing_ack_timeout_invalidates_and_uses_owned_reconnect_backoff() -> None:
    async def scenario():
        connection = _Connection()
        waiting = asyncio.Event()
        async def sleep(delay):
            assert 0.8 <= delay <= 1
            waiting.set()
            await asyncio.Future()
        service = LiquidationFlowService(transport=_Transport(connection),
                                        subscription_timeout_seconds=0.01, sleep=sleep)
        await service.start(["BTCUSDT"])
        try:
            await asyncio.wait_for(waiting.wait(), 2)
            assert service.health()["last_failure"]["category"] == "subscription_timeout"
            assert service.health()["consecutive_connection_failures"] == 1
            assert not service.health()["subscription_confirmed"]
            assert service.snapshot("BTCUSDT").status != ContextStatus.VERIFIED
        finally:
            await service.stop()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", [MicrostructureFlowService, LiquidationFlowService, OrderBookLiquidityService])
def test_malformed_connected_stream_invalidates_evidence_and_recovers_with_backoff(kind) -> None:
    async def scenario():
        now = [BASE]
        connection = _Connection()
        waiting = asyncio.Event()
        async def sleep(delay):
            waiting.set()
            await asyncio.Future()
        kwargs = {"snapshot_client": _SnapshotClient()} if kind is OrderBookLiquidityService else {}
        service = kind(transport=_Transport(connection), clock=lambda: now[0], sleep=sleep, **kwargs)
        await service.start(["BTCUSDT"])
        try:
            for _ in range(10):
                await asyncio.sleep(0)
            if kind is LiquidationFlowService:
                connection.incoming.put_nowait({"result": None, "id": connection.sent[0]["id"]})
                for _ in range(10):
                    await asyncio.sleep(0)
                now[0] += timedelta(minutes=16)
                assert service.snapshot("BTCUSDT").status == ContextStatus.VERIFIED
            connection.incoming.put_nowait("malformed fixture JSON")
            await asyncio.wait_for(waiting.wait(), 2)
            assert not service.health()["connected"]
            assert service.snapshot("BTCUSDT").status != ContextStatus.VERIFIED
            assert service.health()["malformed_event_count"] == 1
            assert service.health()["last_failure"]["category"] == "protocol_failure"
        finally:
            await service.stop()
    asyncio.run(scenario())
