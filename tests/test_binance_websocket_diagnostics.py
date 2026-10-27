from __future__ import annotations

import asyncio
import socket
import ssl
import json
from datetime import UTC, datetime

import httpx

import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.http11 import Response
from websockets.asyncio.server import serve

from scripts.diagnose_binance_websocket import failure, network_stages, PUBLIC_URL, MARKET_URL
from scripts.diagnose_binance_websocket import probe_stream, PUBLIC_STREAMS
from tests.test_order_book_liquidity import _event_payload, _snapshot_payload


@pytest.mark.parametrize("exc,phase,expected", [
    (socket.gaierror("sensitive"), "opening_handshake", "dns_failure"),
    (ssl.SSLCertVerificationError("sensitive"), "tls", "tls_certificate_failure"),
    (ssl.SSLError("sensitive"), "tls", "tls_failure"),
    (TimeoutError("sensitive"), "opening_handshake", "opening_handshake_timeout"),
    (TimeoutError("sensitive"), "receive", "receive_timeout"),
    (ConnectionClosedError(None, None), "receive", "transport_lost_without_close_frame"),
    (InvalidStatus(Response(403, "Forbidden", Headers(), b"sensitive")), "opening_handshake", "http_upgrade_rejected"),
])
def test_diagnostics_classify_without_exposing_response_or_exception_text(exc, phase, expected) -> None:
    result = failure(exc, phase)
    assert result["category"] == expected
    assert "sensitive" not in str(result)


def test_dns_failure_does_not_continue_network_stages(monkeypatch) -> None:
    async def scenario() -> None:
        async def dns(*args, **kwargs):
            raise socket.gaierror("synthetic offline DNS")
        monkeypatch.setattr(asyncio.get_running_loop(), "getaddrinfo", dns)
        result = await network_stages()
        assert result["dns"]["category"] == "dns_failure"
        assert "tcp" not in result
        assert "tls" not in result
    asyncio.run(scenario())


def test_probe_uses_only_routed_public_market_data() -> None:
    assert PUBLIC_URL == "wss://fstream.binance.com/public/stream"
    assert MARKET_URL == "wss://fstream.binance.com/market/stream"


def test_probe_receives_parses_and_synchronizes_real_loopback_frames() -> None:
    async def scenario() -> None:
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        async def provider(connection):
            request = json.loads(await connection.recv())
            assert request["method"] == "SUBSCRIBE"
            await connection.send('{"result":null,"id":1}')
            await connection.send(json.dumps({"stream": PUBLIC_STREAMS[0], "data": _event_payload(event_ms=now_ms)}))
            request = json.loads(await connection.recv())
            assert request["method"] == "UNSUBSCRIBE"
            await connection.wait_closed()

        snapshot = _snapshot_payload()
        snapshot.update(E=now_ms, T=now_ms)
        async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=snapshot),
        )) as client:
            async with serve(provider, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]
                result = await probe_stream(f"ws://127.0.0.1:{port}", PUBLIC_STREAMS,
                    seconds=0.05, direct=True, client=client, instruments={"BTCUSDT"})
                assert "error" not in result
                assert result["http_status"] == 101
                assert result["subscription_ack"]
                assert result["valid"] == {"depthUpdate": 1}
                assert result["order_book_synchronized_during_probe"]
                assert result["book_status_at_end"] == "VERIFIED"
                assert result["stale"] == result["unverified"] == 0
                assert result["final_close_code"] == 1000
    asyncio.run(scenario())
