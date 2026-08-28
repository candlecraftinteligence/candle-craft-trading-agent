from __future__ import annotations

import json
import statistics
import sys
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.microstructure.order_book import (  # noqa: E402
    SynchronizedLocalOrderBook,
    parse_binance_depth_event,
    parse_binance_depth_snapshot,
)


def run_synthetic_order_book_benchmark(
    *,
    symbol_count: int = 100,
    levels_per_side: int = 500,
    updates_per_symbol: int = 10,
) -> dict[str, Any]:
    if symbol_count < 1 or symbol_count > 100:
        raise ValueError("symbol_count must be between 1 and 100")
    if levels_per_side < 1 or levels_per_side > 1000:
        raise ValueError("levels_per_side must be between 1 and 1000")
    if updates_per_symbol < 0:
        raise ValueError("updates_per_symbol cannot be negative")

    observed_at = datetime(2026, 8, 28, 12, tzinfo=UTC)
    books: list[SynchronizedLocalOrderBook] = []
    for index in range(symbol_count):
        symbol = "BTCUSDT" if index == 0 else f"S{index:03d}USDT"
        books.append(
            _initialized_book(
                symbol,
                levels_per_side=levels_per_side,
                observed_at=observed_at,
            )
        )

    per_symbol_memory = _deep_size(books[0])
    maintained_memory = sum(_deep_size(book) for book in books)

    update_started = time.perf_counter()
    update_count = 0
    for update_number in range(updates_per_symbol):
        for book in books:
            previous = book.last_update_id or 0
            event = parse_binance_depth_event(
                _event_payload(
                    book.symbol,
                    first=previous + 1,
                    final=previous + 1,
                    previous=previous,
                    event_ms=_milliseconds(observed_at + timedelta(milliseconds=update_number + 1)),
                    bids=[["99.99", str(update_number + 1)]],
                )
            )
            book.ingest(event, received_at=observed_at)
            update_count += 1
    update_elapsed = max(time.perf_counter() - update_started, sys.float_info.epsilon)

    snapshot_latencies_ms: list[float] = []
    representative_bytes = 0
    for book in books:
        started = time.perf_counter()
        snapshot = book.snapshot(as_of=observed_at + timedelta(seconds=1))
        snapshot_latencies_ms.append((time.perf_counter() - started) * 1000)
        if not representative_bytes:
            representative_bytes = len(snapshot.model_dump_json().encode("utf-8"))

    ordered_latency = sorted(snapshot_latencies_ms)
    p95_index = min(len(ordered_latency) - 1, max(0, int(len(ordered_latency) * 0.95) - 1))
    return {
        "maintained_symbols": symbol_count,
        "levels_per_side": levels_per_side,
        "depth_updates_applied": update_count,
        "depth_update_rate_events_per_second": round(update_count / update_elapsed, 2),
        "per_symbol_initialized_book_memory_bytes": per_symbol_memory,
        "maintained_books_memory_bytes": maintained_memory,
        "configured_maximum_100_symbol_memory_bytes": round(
            maintained_memory / symbol_count * 100
        ),
        "snapshot_latency_median_ms": round(statistics.median(snapshot_latencies_ms), 4),
        "snapshot_latency_p95_ms": round(ordered_latency[p95_index], 4),
        "representative_serialized_snapshot_bytes": representative_bytes,
        "transport_queue_bound_messages": 32,
        "per_symbol_snapshot_event_buffer_bound": 256,
        "external_network_calls": 0,
    }


def _initialized_book(
    symbol: str,
    *,
    levels_per_side: int,
    observed_at: datetime,
) -> SynchronizedLocalOrderBook:
    book = SynchronizedLocalOrderBook(
        symbol,
        stale_after_seconds=5,
        event_buffer_size=256,
    )
    book.mark_connected(reconnect=False)
    bridge = parse_binance_depth_event(
        _event_payload(
            symbol,
            first=100,
            final=101,
            previous=99,
            event_ms=_milliseconds(observed_at),
        )
    )
    book.ingest(bridge, received_at=observed_at)
    snapshot = parse_binance_depth_snapshot(
        {
            "lastUpdateId": 100,
            "E": _milliseconds(observed_at - timedelta(milliseconds=2)),
            "T": _milliseconds(observed_at - timedelta(milliseconds=3)),
            "bids": [
                [str(Decimal("100") - Decimal(index + 1) / Decimal("100")), str((index % 9) + 1)]
                for index in range(levels_per_side)
            ],
            "asks": [
                [str(Decimal("100") + Decimal(index + 1) / Decimal("100")), str((index % 9) + 1)]
                for index in range(levels_per_side)
            ],
        }
    )
    book.install_snapshot(snapshot, received_at=observed_at)
    return book


def _event_payload(
    symbol: str,
    *,
    first: int,
    final: int,
    previous: int,
    event_ms: int,
    bids: list[list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "e": "depthUpdate",
        "E": event_ms,
        "T": event_ms,
        "s": symbol,
        "U": first,
        "u": final,
        "pu": previous,
        "b": bids or [],
        "a": [],
        "ps": symbol,
        "st": 1,
    }


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _deep_size(value: Any, seen: set[int] | None = None) -> int:
    visited = seen if seen is not None else set()
    identity = id(value)
    if identity in visited:
        return 0
    visited.add(identity)
    size = sys.getsizeof(value)
    if isinstance(value, Mapping):
        return size + sum(
            _deep_size(key, visited) + _deep_size(item, visited)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return size + sum(_deep_size(item, visited) for item in value)
    if hasattr(value, "__dict__"):
        size += _deep_size(vars(value), visited)
    slots = getattr(type(value), "__slots__", ())
    for slot in slots if isinstance(slots, tuple) else (slots,):
        if hasattr(value, slot):
            size += _deep_size(getattr(value, slot), visited)
    return size


def main() -> None:
    print(json.dumps(run_synthetic_order_book_benchmark(), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
