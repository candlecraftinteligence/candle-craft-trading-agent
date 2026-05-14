from __future__ import annotations

import asyncio
from pprint import pprint

from app.data.exchange_clients import BinanceFuturesClient, BybitLinearClient


def _dump(value: object) -> object:
    if isinstance(value, list):
        return [item.model_dump(mode="json") for item in value]
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    return value


async def _fetch_exchange(name: str, client: object) -> None:
    print(f"\n{name}")
    candles = await client.get_klines("BTCUSDT", "1m", 3)
    ticker = await client.get_ticker("BTCUSDT")
    funding = await client.get_funding_rate("BTCUSDT")
    open_interest = await client.get_open_interest("BTCUSDT")

    pprint(
        {
            "candles": _dump(candles),
            "ticker": _dump(ticker),
            "funding": _dump(funding),
            "open_interest": _dump(open_interest),
        }
    )


async def main() -> None:
    async with BinanceFuturesClient() as binance, BybitLinearClient() as bybit:
        await _fetch_exchange("binance_futures", binance)
        await _fetch_exchange("bybit_linear", bybit)


if __name__ == "__main__":
    asyncio.run(main())
