"""Finite optional acquisition before the scanner freezes its decision clock."""
from __future__ import annotations

import asyncio
import time


async def bounded_prefetch(symbols, acquire, *, timeout=20.0, workers=4, maximum=200):
    symbols = tuple(dict.fromkeys(symbols))
    selected = symbols[:maximum]
    pending = iter(selected)
    values, failures = {}, {}
    started = time.monotonic()

    async def worker():
        for symbol in pending:
            try:
                values[symbol] = await acquire(symbol)
            except Exception as exc:
                failures[symbol] = type(exc).__name__

    tasks = [asyncio.create_task(worker()) for _ in range(min(workers, len(selected)))]
    try:
        async with asyncio.timeout(max(0.0, timeout)):
            await asyncio.gather(*tasks)
    except TimeoutError:
        pass
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return values, {
        "requested": len(symbols), "acquired": len(values),
        "withheld": {s: failures.get(s, "prefetch_deadline_or_symbol_bound")
                     for s in symbols if s not in values},
        "workers": min(workers, len(selected)), "maximum_symbols": maximum,
        "deadline_seconds": timeout, "duration_seconds": round(time.monotonic()-started, 3),
    }
