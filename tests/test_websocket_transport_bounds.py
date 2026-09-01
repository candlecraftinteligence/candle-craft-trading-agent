from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosedError

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

    assert isinstance(microstructure.transport, BinanceWebSocketTransport)
    assert microstructure.transport.max_message_bytes == 64 * 1024
    assert microstructure.transport.max_queue == 32

    assert isinstance(liquidation.transport, BinanceWebSocketTransport)
    assert liquidation.transport.max_message_bytes == 64 * 1024
    assert liquidation.transport.max_queue == 32

    assert isinstance(order_book.transport, BinanceWebSocketTransport)
    assert order_book.transport.max_message_bytes == 1024 * 1024
    assert order_book.transport.max_queue == 4
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
