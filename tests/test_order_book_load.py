from __future__ import annotations

from scripts.benchmark_order_book_liquidity import run_synthetic_order_book_benchmark


def test_synthetic_order_book_load_reports_bounds_without_fragile_timing_assertions() -> None:
    report = run_synthetic_order_book_benchmark(
        symbol_count=12,
        levels_per_side=500,
        updates_per_symbol=4,
    )
    assert report["maintained_symbols"] == 12
    assert report["levels_per_side"] == 500
    assert report["depth_updates_applied"] == 48
    assert report["per_symbol_initialized_book_memory_bytes"] > 0
    assert report["maintained_books_memory_bytes"] >= (
        report["per_symbol_initialized_book_memory_bytes"] * 12
    )
    assert report["configured_maximum_100_symbol_memory_bytes"] > 0
    assert report["snapshot_latency_median_ms"] >= 0
    assert report["snapshot_latency_p95_ms"] >= 0
    assert report["representative_serialized_snapshot_bytes"] <= 4096
    assert report["websocket_max_message_bytes"] == 1024 * 1024
    assert report["transport_queue_bound_frames"] == 4
    assert report["theoretical_raw_transport_queue_bytes"] == 4 * 1024 * 1024
    assert report["approximate_books_plus_raw_transport_bytes"] == (
        report["maintained_books_memory_bytes"] + 4 * 1024 * 1024
    )
    assert report["per_symbol_snapshot_event_buffer_bound"] == 256
    assert report["external_network_calls"] == 0
