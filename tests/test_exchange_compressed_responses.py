from __future__ import annotations

import asyncio
import gzip
import json
import zlib
from collections.abc import AsyncIterator

import httpx
import pytest

from app.data.exceptions import ExchangeHTTPError, ExchangeRateLimitError, ExchangeResponseError
from app.data.exchange_clients import BinanceFuturesClient, BybitLinearClient


class _WireStream(httpx.AsyncByteStream):
    """Supply encoded wire bytes, without HTTPX pre-decoding a mock response."""

    def __init__(self, body: bytes) -> None:
        self.body = body
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.body), 7):
            yield self.body[start:start + 7]

    async def aclose(self) -> None:
        self.closed = True


@pytest.mark.parametrize("client_type", [BinanceFuturesClient, BybitLinearClient])
@pytest.mark.parametrize("encoding", ["gzip", "deflate", None])
def test_streamed_json_is_decoded_once_and_body_headers_match(client_type, encoding) -> None:
    async def scenario() -> None:
        payload = {"symbols": [{"symbol": "BTCUSDT"}], "fixture": "x" * 200}
        decoded = json.dumps(payload).encode()
        encoded = gzip.compress(decoded) if encoding == "gzip" else zlib.compress(decoded) if encoding else decoded
        stream = _WireStream(encoded)

        def handler(request: httpx.Request) -> httpx.Response:
            headers = {"Content-Length": str(len(encoded)), "Content-Type": "application/json",
                       "X-MBX-USED-WEIGHT-1M": "1"}
            if encoding:
                headers["Content-Encoding"] = encoding
            return httpx.Response(200, headers=headers, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://exchange.test") as http:
            client = client_type(http_client=http, retry_base_delay=0, retry_max_delay=0)
            response = await client._send_once("/fixture")
            assert response.json() == payload
            assert response.content == decoded
            assert "Content-Encoding" not in response.headers
            assert int(response.headers["Content-Length"]) == len(decoded)
            assert response.headers["Content-Type"] == "application/json"
            assert response.headers["X-MBX-USED-WEIGHT-1M"] == "1"
            assert response.request.url.path == "/fixture"
        assert stream.closed

    asyncio.run(scenario())


def test_decoded_response_budget_rejects_compressed_expansion_and_closes_stream() -> None:
    async def scenario() -> None:
        decoded = json.dumps({"fixture": "x" * 4096}).encode()
        encoded = gzip.compress(decoded)
        assert len(encoded) < 256 < len(decoded)
        stream = _WireStream(encoded)

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, headers={"Content-Encoding": "gzip", "Content-Length": str(len(encoded))},
                                  stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://exchange.test") as http:
            client = BinanceFuturesClient(http_client=http, retry_attempts=3, retry_base_delay=0, retry_max_delay=0)
            client._max_response_bytes = 256
            with pytest.raises(ExchangeResponseError, match="byte budget"):
                await client._get_json("/fixture")
            assert len(client.retry_events()) == 1
            assert client.retry_events()[0]["will_retry"] is False
        assert stream.closed

    asyncio.run(scenario())


def test_malformed_content_encoding_is_a_terminal_response_error() -> None:
    async def scenario() -> None:
        streams: list[_WireStream] = []

        def handler(request: httpx.Request) -> httpx.Response:
            stream = _WireStream(b"not a gzip stream")
            streams.append(stream)
            return httpx.Response(200, headers={"Content-Encoding": "gzip"}, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://exchange.test") as http:
            client = BinanceFuturesClient(http_client=http, retry_attempts=3, retry_base_delay=0, retry_max_delay=0)
            with pytest.raises(ExchangeResponseError, match="content encoding") as error:
                await client._get_json("/fixture")
            assert isinstance(error.value.__cause__, httpx.DecodingError)
            assert len(streams) == 1
            assert client.retry_events()[0]["error_type"] == "ExchangeResponseError"
            assert client.retry_events()[0]["will_retry"] is False
        assert all(stream.closed for stream in streams)

    asyncio.run(scenario())


@pytest.mark.parametrize("status", [418, 429, 503])
def test_compressed_error_responses_preserve_retry_policy(status) -> None:
    async def scenario() -> None:
        streams: list[_WireStream] = []

        def handler(request: httpx.Request) -> httpx.Response:
            stream = _WireStream(gzip.compress(b'{"error":"fixture unavailable"}'))
            streams.append(stream)
            return httpx.Response(status, headers={"Content-Encoding": "gzip", "Retry-After": "0"}, stream=stream)

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://exchange.test") as http:
            client = BinanceFuturesClient(http_client=http, retry_attempts=3, retry_base_delay=0, retry_max_delay=0)
            expected = ExchangeRateLimitError if status in {418, 429} else ExchangeHTTPError
            with pytest.raises(expected) as error:
                await client._get_json("/fixture")
            assert error.value.status_code == status
            assert len(streams) == 3
            assert [r["will_retry"] for r in client.retry_events()] == [True, True, False]
        assert all(stream.closed for stream in streams)

    asyncio.run(scenario())
