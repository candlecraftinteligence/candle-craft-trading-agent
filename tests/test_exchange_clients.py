from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.data.exceptions import ExchangeMalformedJSONError, ExchangeMissingFieldError
from app.data.exchange_clients import BinanceFuturesClient, BybitLinearClient


def run(coro: Any) -> Any:
    return asyncio.run(coro)


async def _binance_client(handler: httpx.MockTransport) -> BinanceFuturesClient:
    http_client = httpx.AsyncClient(transport=handler, base_url="https://binance.test")
    return BinanceFuturesClient(
        http_client=http_client,
        retry_attempts=3,
        retry_base_delay=0,
        retry_max_delay=0,
    )


async def _bybit_client(handler: httpx.MockTransport) -> BybitLinearClient:
    http_client = httpx.AsyncClient(transport=handler, base_url="https://bybit.test")
    return BybitLinearClient(
        http_client=http_client,
        retry_attempts=3,
        retry_base_delay=0,
        retry_max_delay=0,
    )


def test_binance_klines_mocked_response() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/fapi/v1/klines"
            assert request.url.params["symbol"] == "BTCUSDT"
            assert request.url.params["interval"] == "1m"
            return httpx.Response(
                200,
                json=[
                    [
                        1710000000000,
                        "100.0",
                        "110.0",
                        "95.0",
                        "105.0",
                        "12.5",
                        1710000059999,
                        "1312.5",
                        42,
                        "6.0",
                        "630.0",
                        "0",
                    ]
                ],
            )

        client = await _binance_client(httpx.MockTransport(handler))
        candles = await client.get_klines("btcusdt", "1m", 1)
        await client._http_client.aclose()

        assert len(candles) == 1
        assert candles[0].exchange == "binance_futures"
        assert candles[0].symbol == "BTCUSDT"
        assert candles[0].open == Decimal("100.0")
        assert candles[0].quote_volume == Decimal("1312.5")
        assert candles[0].trade_count == 42

    run(scenario())


def test_binance_ticker_mocked_response() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/fapi/v1/ticker/24hr"
            return httpx.Response(
                200,
                json={
                    "symbol": "BTCUSDT",
                    "priceChange": "250.0",
                    "priceChangePercent": "2.500",
                    "lastPrice": "10250.0",
                    "highPrice": "10300.0",
                    "lowPrice": "9900.0",
                    "volume": "123.4",
                    "quoteVolume": "1264999.0",
                    "closeTime": 1710000000000,
                },
            )

        client = await _binance_client(httpx.MockTransport(handler))
        ticker = await client.get_ticker("BTCUSDT")
        await client._http_client.aclose()

        assert ticker.exchange == "binance_futures"
        assert ticker.last_price == Decimal("10250.0")
        assert ticker.price_change_ratio_24h == Decimal("0.025")

    run(scenario())


def test_binance_funding_mocked_response() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/fapi/v1/fundingRate"
            assert request.url.params["limit"] == "1"
            return httpx.Response(
                200,
                json=[
                    {
                        "symbol": "BTCUSDT",
                        "fundingRate": "0.0001",
                        "fundingTime": 1710000000000,
                        "markPrice": "10000.1",
                    }
                ],
            )

        client = await _binance_client(httpx.MockTransport(handler))
        funding = await client.get_funding_rate("BTCUSDT")
        await client._http_client.aclose()

        assert funding.exchange == "binance_futures"
        assert funding.funding_rate == Decimal("0.0001")
        assert funding.mark_price == Decimal("10000.1")

    run(scenario())


def test_binance_open_interest_mocked_response() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/fapi/v1/openInterest"
            return httpx.Response(
                200,
                json={"symbol": "BTCUSDT", "openInterest": "10659.509", "time": 1710000000000},
            )

        client = await _binance_client(httpx.MockTransport(handler))
        open_interest = await client.get_open_interest("BTCUSDT")
        await client._http_client.aclose()

        assert open_interest.exchange == "binance_futures"
        assert open_interest.open_interest == Decimal("10659.509")
        assert open_interest.timestamp == 1710000000000

    run(scenario())


def test_bybit_klines_mocked_response() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v5/market/kline"
            assert request.url.params["category"] == "linear"
            assert request.url.params["interval"] == "1"
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "category": "linear",
                        "symbol": "BTCUSDT",
                        "list": [
                            ["1710000060000", "105.0", "112.0", "104.0", "110.0", "8.1", "891.0"],
                            ["1710000000000", "100.0", "106.0", "99.0", "105.0", "7.3", "766.5"],
                        ],
                    },
                    "time": 1710000100000,
                },
            )

        client = await _bybit_client(httpx.MockTransport(handler))
        candles = await client.get_klines("BTCUSDT", "1m", 2)
        await client._http_client.aclose()

        assert [candle.timestamp for candle in candles] == [1710000000000, 1710000060000]
        assert candles[0].exchange == "bybit_linear"
        assert candles[0].quote_volume == Decimal("766.5")

    run(scenario())


def test_bybit_ticker_mocked_response() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v5/market/tickers"
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "category": "linear",
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "lastPrice": "10100.0",
                                "indexPrice": "10095.0",
                                "markPrice": "10101.0",
                                "prevPrice24h": "10000.0",
                                "price24hPcnt": "0.01",
                                "highPrice24h": "10300.0",
                                "lowPrice24h": "9900.0",
                                "turnover24h": "1000000.0",
                                "volume24h": "100.0",
                                "bid1Price": "10099.5",
                                "ask1Price": "10100.5",
                            }
                        ],
                    },
                    "time": 1710000000000,
                },
            )

        client = await _bybit_client(httpx.MockTransport(handler))
        ticker = await client.get_ticker("BTCUSDT")
        await client._http_client.aclose()

        assert ticker.exchange == "bybit_linear"
        assert ticker.last_price == Decimal("10100.0")
        assert ticker.price_change_24h == Decimal("100.0")
        assert ticker.price_change_ratio_24h == Decimal("0.01")
        assert ticker.bid_price == Decimal("10099.5")

    run(scenario())


def test_bybit_funding_mocked_response() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v5/market/funding/history"
            assert request.url.params["limit"] == "1"
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "category": "linear",
                        "list": [
                            {
                                "symbol": "BTCUSDT",
                                "fundingRate": "0.0001",
                                "fundingRateTimestamp": "1710000000000",
                            }
                        ],
                    },
                    "time": 1710000000100,
                },
            )

        client = await _bybit_client(httpx.MockTransport(handler))
        funding = await client.get_funding_rate("BTCUSDT")
        await client._http_client.aclose()

        assert funding.exchange == "bybit_linear"
        assert funding.funding_rate == Decimal("0.0001")
        assert funding.timestamp == 1710000000000

    run(scenario())


def test_bybit_open_interest_mocked_response() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/v5/market/open-interest"
            assert request.url.params["intervalTime"] == "5min"
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "result": {
                        "symbol": "BTCUSDT",
                        "category": "linear",
                        "list": [{"openInterest": "12345.67", "timestamp": "1710000000000"}],
                    },
                    "time": 1710000000100,
                },
            )

        client = await _bybit_client(httpx.MockTransport(handler))
        open_interest = await client.get_open_interest("BTCUSDT")
        await client._http_client.aclose()

        assert open_interest.exchange == "bybit_linear"
        assert open_interest.open_interest == Decimal("12345.67")

    run(scenario())


def test_rate_limit_response_retries_before_success() -> None:
    async def scenario() -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls < 3:
                return httpx.Response(429, json={"msg": "rate limited"}, headers={"Retry-After": "0"})
            return httpx.Response(
                200,
                json={"symbol": "BTCUSDT", "openInterest": "100.0", "time": 1710000000000},
            )

        client = await _binance_client(httpx.MockTransport(handler))
        open_interest = await client.get_open_interest("BTCUSDT")
        await client._http_client.aclose()

        assert calls == 3
        assert open_interest.open_interest == Decimal("100.0")

    run(scenario())


def test_malformed_json_response_raises_clear_error() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=b"{not-json")

        client = await _binance_client(httpx.MockTransport(handler))
        with pytest.raises(ExchangeMalformedJSONError):
            await client.get_ticker("BTCUSDT")
        await client._http_client.aclose()

    run(scenario())


def test_missing_expected_field_raises_clear_error() -> None:
    async def scenario() -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"symbol": "BTCUSDT", "closeTime": 1710000000000})

        client = await _binance_client(httpx.MockTransport(handler))
        with pytest.raises(ExchangeMissingFieldError):
            await client.get_ticker("BTCUSDT")
        await client._http_client.aclose()

    run(scenario())
