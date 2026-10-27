from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from collections.abc import Mapping
from typing import Any

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.http11 import Response
from websockets.datastructures import Headers

from app.microstructure.liquidation_service import LiquidationFlowService
from app.microstructure.order_book import parse_binance_depth_event
from app.microstructure.order_book_service import (
    ORDER_BOOK_WEBSOCKET_MAX_MESSAGE_BYTES,
    ORDER_BOOK_WEBSOCKET_MAX_QUEUE,
    OrderBookLiquidityService,
)
from app.microstructure.service import (
    DEFAULT_WEBSOCKET_MAX_MESSAGE_BYTES,
    DEFAULT_WEBSOCKET_MAX_QUEUE,
    BinanceWebSocketTransport,
    MicrostructureFlowService,
)


class _UnusedSnapshotClient:
    async def fetch(self, symbol: str, limit: int) -> Mapping[str, Any]:
        raise AssertionError(f"unexpected snapshot request for {symbol} limit={limit}")

    async def aclose(self) -> None:
        return None


def test_service_transports_keep_workload_specific_finite_bounds() -> None:
    generic = BinanceWebSocketTransport()
    microstructure = MicrostructureFlowService()
    liquidation = LiquidationFlowService()
    order_book = OrderBookLiquidityService(snapshot_client=_UnusedSnapshotClient())

    assert DEFAULT_WEBSOCKET_MAX_MESSAGE_BYTES == 64 * 1024
    assert DEFAULT_WEBSOCKET_MAX_QUEUE == 32
    assert generic.max_message_bytes == DEFAULT_WEBSOCKET_MAX_MESSAGE_BYTES
    assert generic.max_queue == DEFAULT_WEBSOCKET_MAX_QUEUE
    assert generic.open_timeout_seconds == 10

    assert isinstance(microstructure.transport, BinanceWebSocketTransport)
    assert microstructure.transport.max_message_bytes == 64 * 1024
    assert microstructure.transport.max_queue == 32
    assert microstructure.transport.ping_interval_seconds == 20
    assert microstructure.transport.ping_timeout_seconds == 20

    assert isinstance(liquidation.transport, BinanceWebSocketTransport)
    assert liquidation.transport.max_message_bytes == 64 * 1024
    assert liquidation.transport.max_queue == 32

    assert isinstance(order_book.transport, BinanceWebSocketTransport)
    assert order_book.transport.max_message_bytes == 1024 * 1024
    assert order_book.transport.max_queue == 4
    assert order_book.transport.open_timeout_seconds == 20
    assert order_book.transport.max_message_bytes is not None
    assert order_book.transport.max_queue is not None


def test_order_book_transport_forwards_explicit_bounds_to_websockets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}
    expected_connection = object()

    async def fake_connect(url: str, **kwargs: Any) -> object:
        captured["url"] = url
        captured.update(kwargs)
        return expected_connection

    monkeypatch.setattr("websockets.asyncio.client.connect", fake_connect)
    service = OrderBookLiquidityService(snapshot_client=_UnusedSnapshotClient())

    connection = asyncio.run(service.transport.connect())

    assert connection is expected_connection
    assert captured["max_size"] == ORDER_BOOK_WEBSOCKET_MAX_MESSAGE_BYTES
    assert captured["max_queue"] == ORDER_BOOK_WEBSOCKET_MAX_QUEUE
    assert captured["open_timeout"] == 20
    assert captured["max_size"] is not None
    assert captured["max_queue"] is not None


def test_order_book_transport_accepts_valid_depth_message_above_64_kib() -> None:
    asyncio.run(_assert_order_book_transport_accepts_valid_depth_message_above_64_kib())


async def _assert_order_book_transport_accepts_valid_depth_message_above_64_kib() -> None:
    payload = _depth_message_larger_than(64 * 1024)
    assert 64 * 1024 < len(payload.encode("utf-8")) < ORDER_BOOK_WEBSOCKET_MAX_MESSAGE_BYTES

    transport = BinanceWebSocketTransport(
        max_message_bytes=ORDER_BOOK_WEBSOCKET_MAX_MESSAGE_BYTES,
        max_queue=ORDER_BOOK_WEBSOCKET_MAX_QUEUE,
    )
    received = await _receive_from_loopback(payload, transport=transport)

    assert received == payload
    event = parse_binance_depth_event(received)
    assert event.symbol == "BTCUSDT"
    assert len(event.bids) > 1


def test_order_book_transport_rejects_message_above_hard_maximum() -> None:
    asyncio.run(_assert_order_book_transport_rejects_message_above_hard_maximum())


async def _assert_order_book_transport_rejects_message_above_hard_maximum() -> None:
    payload = _depth_message_larger_than(ORDER_BOOK_WEBSOCKET_MAX_MESSAGE_BYTES)
    transport = BinanceWebSocketTransport(
        max_message_bytes=ORDER_BOOK_WEBSOCKET_MAX_MESSAGE_BYTES,
        max_queue=ORDER_BOOK_WEBSOCKET_MAX_QUEUE,
    )

    with pytest.raises(ConnectionClosedError) as caught:
        await _receive_from_loopback(payload, transport=transport)

    assert caught.value.sent is not None
    assert caught.value.sent.code == 1009


async def _receive_from_loopback(
    payload: str,
    *,
    transport: BinanceWebSocketTransport,
) -> str | bytes:
    async def send_once(connection: Any) -> None:
        await connection.send(payload)
        await connection.wait_closed()

    async with serve(send_once, "127.0.0.1", 0, compression=None) as server:
        port = server.sockets[0].getsockname()[1]
        transport.url = f"ws://127.0.0.1:{port}"
        connection = await transport.connect()
        try:
            return await connection.recv()
        finally:
            await connection.close()


def _depth_message_larger_than(minimum_bytes: int) -> str:
    level_count = max(1, minimum_bytes // 24)
    while True:
        payload = json.dumps(
            {
                "e": "depthUpdate",
                "E": 1_788_177_600_000,
                "T": 1_788_177_599_999,
                "s": "BTCUSDT",
                "U": 100,
                "u": 101,
                "pu": 99,
                "b": [
                    [str(99_000_000 - index), "1.00000000"]
                    for index in range(level_count)
                ],
                "a": [["100000000", "1.00000000"]],
                "ps": "BTCUSDT",
                "st": 1,
            },
            separators=(",", ":"),
            sort_keys=True,
        )
        if len(payload.encode("utf-8")) > minimum_bytes:
            return payload
        level_count += 256


def test_wrong_path_reports_http_upgrade_rejection() -> None:
    async def scenario() -> None:
        async def reject(connection: Any, request: Any) -> Response:
            return Response(404, "Not Found", Headers(), b"")

        async with serve(lambda connection: connection.wait_closed(), "127.0.0.1", 0,
                         process_request=reject) as server:
            port = server.sockets[0].getsockname()[1]
            transport = BinanceWebSocketTransport(url=f"ws://127.0.0.1:{port}/incorrect")
            with pytest.raises(InvalidStatus) as caught:
                await transport.connect()
            assert caught.value.response.status_code == 404
    asyncio.run(scenario())


def test_handshake_timeout_is_bounded() -> None:
    async def scenario() -> None:
        async def stall(connection: Any, request: Any) -> None:
            await asyncio.sleep(0.2)

        async with serve(lambda connection: connection.wait_closed(), "127.0.0.1", 0,
                         process_request=stall) as server:
            port = server.sockets[0].getsockname()[1]
            transport = BinanceWebSocketTransport(url=f"ws://127.0.0.1:{port}", open_timeout_seconds=0.03)
            with pytest.raises(TimeoutError):
                await transport.connect()
    asyncio.run(scenario())


def test_valid_upgrade_ping_pong_frames_and_provider_close() -> None:
    async def scenario() -> None:
        async def provider(connection: Any) -> None:
            # Real protocol responds automatically to Ping while waiting for data.
            await connection.recv()
            await connection.send('{"diagnostic":"fixture"}')
            await connection.close(code=1012, reason="test provider restart")

        async with serve(provider, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            connection = await BinanceWebSocketTransport(url=f"ws://127.0.0.1:{port}").connect()
            try:
                assert connection.response.status_code == 101
                assert connection.subprotocol is None
                pong = await connection.ping(b"cci-test")
                assert await asyncio.wait_for(pong, 1) >= 0
                await connection.send("fixture trigger")
                assert json.loads(await connection.recv())["diagnostic"] == "fixture"
                with pytest.raises(ConnectionClosedError) as caught:
                    await connection.recv()
                assert caught.value.rcvd.code == 1012
            finally:
                await connection.close()
    asyncio.run(scenario())


def test_missing_pong_closes_connection_and_classifies_heartbeat_timeout() -> None:
    from app.microstructure.websocket_protocol import failure_facts

    async def scenario() -> None:
        ping_received = asyncio.Event()

        async def silent_peer(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            try:
                request = (await reader.readuntil(b"\r\n\r\n")).decode("ascii")
                key = next(line.split(":", 1)[1].strip() for line in request.splitlines()
                           if line.lower().startswith("sec-websocket-key:"))
                accept = base64.b64encode(hashlib.sha1(
                    (key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")
                ).digest()).decode("ascii")
                writer.write(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\nSec-WebSocket-Accept: " + accept + "\r\n\r\n").encode("ascii"))
                await writer.drain()
                frame = await reader.read(512)
                assert frame[0] & 0x0F == 9
                ping_received.set()
                # Deliberately withhold Pong and the close reply; this is a loopback fixture.
                await reader.read()
            finally:
                writer.close()
                await writer.wait_closed()

        async with await asyncio.start_server(silent_peer, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            connection = await BinanceWebSocketTransport(
                url=f"ws://127.0.0.1:{port}", ping_interval_seconds=0.02,
                ping_timeout_seconds=0.02, close_timeout_seconds=0.05,
            ).connect()
            try:
                with pytest.raises(ConnectionClosedError) as caught:
                    await asyncio.wait_for(connection.recv(), timeout=2)
                assert ping_received.is_set()
                assert caught.value.sent.code == 1011
                assert failure_facts(caught.value, "receive")["category"] == "heartbeat_timeout"
            finally:
                await connection.close()
    asyncio.run(scenario())
