"""Bounded, read-only acquisition tests; no external network or runtime database."""
import asyncio
from datetime import UTC, datetime
from decimal import Decimal as D

import httpx
import pytest

from app.cache.market_data_cache import CachedMarketDataClient, MarketDataCache, CANDLE_PERIOD_SECONDS
from app.data.exchange_clients import BinanceFuturesClient
from app.data.exceptions import ExchangeRateLimitError, ExchangeResponseError

pytestmark = pytest.mark.no_auto_epoch


class ClosedClient:
    exchange_name = "binance_futures"
    def __init__(self, clock): self.clock, self.calls, self.lag = clock, 0, 0
    async def get_klines(self, symbol, interval, limit):
        self.calls += 1
        period = CANDLE_PERIOD_SECONDS[interval]
        opened = int(self.clock[0]//period)*period-period-self.lag*period
        return [dict(symbol=symbol, interval=interval, timestamp=opened*1000,
                     open=D(100), high=D(101), low=D(99), close=D(100), volume=D(10))]


@pytest.mark.parametrize("interval", ["1d", "12h", "4h", "15m"])
def test_closed_candles_reused_until_exact_next_boundary(interval):
    async def scenario():
        clock = [1700000000.125]
        client = ClosedClient(clock)
        cache = MarketDataCache(now=lambda: clock[0])
        cached = CachedMarketDataClient(client, cache)
        await cached.get_klines("BTCUSDT", interval, 100)
        period = CANDLE_PERIOD_SECONDS[interval]
        boundary = (int(clock[0]//period)+1)*period
        clock[0] = boundary-.001
        await cached.get_klines("BTCUSDT", interval, 100)
        assert client.calls == 1
        clock[0] = boundary
        await cached.get_klines("BTCUSDT", interval, 100)
        assert client.calls == 2
        await cache.aclose()
    asyncio.run(scenario())


def test_delayed_latest_close_is_not_pinned_until_tomorrow():
    async def scenario():
        clock = [1700000000.0]
        client = ClosedClient(clock)
        client.lag = 1
        cache = MarketDataCache(now=lambda: clock[0])
        cached = CachedMarketDataClient(client, cache)
        first = await cached.get_klines("BTCUSDT", "1d", 100)
        clock[0] += 1
        client.lag = 0
        current = await cached.get_klines("BTCUSDT", "1d", 100)
        assert client.calls == 2 and current[0]["timestamp"] > first[0]["timestamp"]
        await cache.aclose()
    asyncio.run(scenario())


def test_identical_requests_coalesce_and_cancelled_waiter_does_not_cancel_shared_source():
    async def scenario():
        cache = MarketDataCache()
        gate, started, calls = asyncio.Event(), asyncio.Event(), []
        async def fetch():
            calls.append(1)
            started.set()
            await gate.wait()
            return {"price": D("100")}
        kwargs = dict(data_type="ticker", key_parts={"symbol": "BTCUSDT"}, fetch=fetch,
                      bind_upstream=lambda: ("original-source", "original-acquired-at"))
        first = asyncio.create_task(cache.get_or_fetch_with_local_delivery(**kwargs))
        await started.wait()
        second = asyncio.create_task(cache.get_or_fetch_with_local_delivery(**kwargs))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError): await first
        gate.set()
        result = await second
        assert calls == [1] and result.retained_upstream == kwargs["bind_upstream"]()
        result.value["price"] = D(999)
        cached = await cache.get_or_fetch_with_local_delivery(**kwargs)
        assert cached.value["price"] == 100 and cached.retained_upstream == result.retained_upstream
        await cache.aclose()
    asyncio.run(scenario())


def test_cache_shutdown_joins_inflight_fetches_and_memory_is_bounded(tmp_path):
    async def scenario():
        cache = MarketDataCache(max_entries=2)
        started, cancelled = asyncio.Event(), []
        async def forever():
            started.set()
            try: await asyncio.Event().wait()
            finally: cancelled.append(True)
        pending = asyncio.create_task(cache.get_or_fetch(data_type="candles", key_parts={"id": 0}, fetch=forever))
        await started.wait()
        await cache.aclose()
        with pytest.raises(asyncio.CancelledError): await pending
        assert cancelled == [True] and not cache._inflight
        for index in range(3):
            async def fetch(): return D(index)
            await cache.get_or_fetch_with_local_delivery(data_type="ticker", key_parts={"id": index},
                                                        fetch=fetch, bind_upstream=lambda: "source")
        assert cache.stats()["entries"] == cache.capture_association_size() == 2
    asyncio.run(scenario())
    path = tmp_path/"oversized.json"
    path.write_text("x"*1025)
    cache = MarketDataCache(file_path=path, max_file_bytes=1024)
    assert cache.errors == 1 and cache.stats()["entries"] == 0


def test_bulk_exchange_metadata_is_shared_without_refreshing_original_acquisition():
    async def scenario():
        at = datetime(2026, 10, 29, 10, tzinfo=UTC)
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"symbols": [dict(symbol=symbol, baseAsset=base,
                quoteAsset="USDT", marginAsset="USDT", contractType="PERPETUAL", fundingIntervalHours=8,
                filters=[{"filterType": "PRICE_FILTER", "tickSize": tick}])
                for symbol, base, tick in (("BTCUSDT", "BTC", ".10"), ("ETHUSDT", "ETH", ".01"))]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://exchange.test") as http:
            client = BinanceFuturesClient(http_client=http)
            metadata = await client.get_instrument_metadata("BTCUSDT", capture_clock=lambda: at)
            a, b = await asyncio.gather(client.get_price_precision("BTCUSDT"), client.get_price_precision("ETHUSDT"))
            payload = await client.get_exchange_info()
            payload["symbols"].clear()
            assert (await client.get_price_precision("BTCUSDT")).tick_size == D(".10")
            assert len(calls) == 1 and not calls[0].url.params
            assert a.received_at == b.received_at == at
            assert metadata.acquisition.acquired_at == at
            await client.aclose()
    asyncio.run(scenario())


def test_rate_limit_cooldown_prevents_new_symbol_requests_until_recovery():
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, headers={"Retry-After": "60"}) if len(calls) == 1 else httpx.Response(200, json={"ok": True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://exchange.test") as http:
            client = BinanceFuturesClient(http_client=http, retry_attempts=1)
            with pytest.raises(ExchangeRateLimitError): await client._get_json("/fapi/v1/openInterest", params={"symbol": "BTCUSDT"})
            with pytest.raises(ExchangeRateLimitError): await client._get_json("/fapi/v1/openInterest", params={"symbol": "ETHUSDT"})
            assert len(calls) == 1
            client._rate_limit_until = 0  # deterministic expiry, no real wait
            assert await client._get_json("/fapi/v1/openInterest") == {"ok": True}
            assert len(calls) == 2
    asyncio.run(scenario())


def test_http_concurrency_and_response_memory_are_bounded():
    async def scenario():
        active, peak = [0], [0]
        async def handler(request):
            active[0] += 1
            peak[0] = max(peak[0], active[0])
            await asyncio.sleep(.001)
            active[0] -= 1
            return httpx.Response(200, json={"ok": True})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://exchange.test") as http:
            client = BinanceFuturesClient(http_client=http, retry_attempts=1)
            await asyncio.gather(*(client._get_json("/futures/data/openInterestHist", params={"symbol": f"C{i}"}) for i in range(10)))
            assert peak[0] <= 2
            client._max_response_bytes = 2
            with pytest.raises(ExchangeResponseError): await client._get_json("/fapi/v1/ticker/price")
    asyncio.run(scenario())
