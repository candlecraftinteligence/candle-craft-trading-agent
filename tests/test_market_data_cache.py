from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from app.cache.market_data_cache import CachedMarketDataClient, MarketDataCache


def run(coro: Any) -> Any:
    return asyncio.run(coro)


class CountingPublicClient:
    exchange_name = "mock_exchange"

    def __init__(self) -> None:
        self.kline_calls = 0
        self.ticker_calls = 0
        self.fail_next = False

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int | str]]:
        self.kline_calls += 1
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("temporary public kline failure")
        return [
            {
                "symbol": symbol,
                "timestamp": 1,
                "interval": interval,
                "open": Decimal("100"),
                "high": Decimal("101"),
                "low": Decimal("99"),
                "close": Decimal(str(100 + self.kline_calls)),
                "volume": Decimal("10"),
            }
            for _ in range(limit)
        ]

    async def get_ticker(self, symbol: str) -> dict[str, Decimal | str]:
        self.ticker_calls += 1
        return {"symbol": symbol, "last_price": Decimal(str(100 + self.ticker_calls))}

    async def get_funding_rate(self, symbol: str) -> dict[str, Decimal | int | str]:
        return {"symbol": symbol, "funding_rate": Decimal("0.0001"), "timestamp": 1}

    async def get_open_interest(self, symbol: str) -> dict[str, Decimal | int | str]:
        return {"symbol": symbol, "open_interest": Decimal("100"), "timestamp": 1}


def test_cache_hit_and_miss_behavior() -> None:
    client = CountingPublicClient()
    cache = MarketDataCache()
    cached = CachedMarketDataClient(client, cache)

    first = run(cached.get_klines("btcusdt", "15m", 2))
    second = run(cached.get_klines("BTCUSDT", "15m", 2))

    assert first == second
    assert client.kline_calls == 1
    assert cache.stats()["misses"] == 1
    assert cache.stats()["hits"] == 1


def test_cache_ttl_expiration_refetches() -> None:
    current_time = 1000.0

    def now() -> float:
        return current_time

    client = CountingPublicClient()
    cache = MarketDataCache(ttl_seconds=1, now=now)
    cached = CachedMarketDataClient(client, cache)

    first = run(cached.get_ticker("BTCUSDT"))
    current_time = 1002.0
    second = run(cached.get_ticker("BTCUSDT"))

    assert first != second
    assert client.ticker_calls == 2
    assert cache.stats()["expired"] == 1
    assert cache.stats()["misses"] == 2


def test_no_cache_disables_storage_and_stats() -> None:
    client = CountingPublicClient()
    cache = MarketDataCache(enabled=False)
    cached = CachedMarketDataClient(client, cache)

    run(cached.get_klines("BTCUSDT", "15m", 1))
    run(cached.get_klines("BTCUSDT", "15m", 1))

    assert client.kline_calls == 2
    assert cache.stats()["hits"] == 0
    assert cache.stats()["misses"] == 0
    assert cache.stats()["entries"] == 0


def test_file_cache_read_write(tmp_path) -> None:
    cache_path = tmp_path / "market_cache.json"
    first_client = CountingPublicClient()
    first_cache = MarketDataCache(file_path=cache_path)
    first_cached = CachedMarketDataClient(first_client, first_cache)
    first = run(first_cached.get_klines("BTCUSDT", "15m", 1))

    second_client = CountingPublicClient()
    second_cache = MarketDataCache(file_path=cache_path)
    second_cached = CachedMarketDataClient(second_client, second_cache)
    second = run(second_cached.get_klines("BTCUSDT", "15m", 1))

    assert cache_path.exists()
    assert second == first
    assert first_client.kline_calls == 1
    assert second_client.kline_calls == 0
    assert second_cache.stats()["hits"] == 1


def test_cache_failures_do_not_poison_future_fetches() -> None:
    client = CountingPublicClient()
    client.fail_next = True
    cache = MarketDataCache()
    cached = CachedMarketDataClient(client, cache)

    try:
        run(cached.get_klines("BTCUSDT", "15m", 1))
    except RuntimeError as exc:
        assert str(exc) == "temporary public kline failure"
    result = run(cached.get_klines("BTCUSDT", "15m", 1))

    assert result
    assert client.kline_calls == 2
    assert cache.stats()["misses"] == 2
    assert cache.stats()["entries"] == 1
