"""DEV-only loop responsiveness measurements. These are not Runtime timings."""

from __future__ import annotations

import asyncio
import threading
import time
import tracemalloc

import pytest

from app.runtime.disk_isolation import SerializedDiskWorker
from app.runtime.loop_lag import LoopLagMonitor

pytestmark = pytest.mark.no_auto_epoch


def test_dev_benchmark_offloaded_sleep_keeps_the_loop_runnable() -> None:
    """Simulated disk stall. Do not read this as a production cycle-time claim."""

    async def ticks_during(action) -> tuple[int, dict]:
        monitor = LoopLagMonitor(interval=0.05)
        await monitor.start()
        count = 0

        async def beat() -> None:
            nonlocal count
            while True:
                await asyncio.sleep(0.02)
                count += 1

        beater = asyncio.create_task(beat())
        await asyncio.sleep(0.05)
        before = count
        started = time.monotonic()
        await action()
        during = count - before
        wall = time.monotonic() - started
        await asyncio.sleep(0.05)
        beater.cancel()
        await asyncio.gather(beater, return_exceptions=True)
        snapshot = monitor.compact_snapshot()
        await monitor.stop()
        snapshot["wall_seconds"] = round(wall, 3)
        snapshot["beats"] = during
        return during, snapshot

    async def scenario() -> dict:
        worker = SerializedDiskWorker()
        try:
            blocking_beats, blocking = await ticks_during(lambda: _async_sleep_on_loop(0.6))
            offloaded_beats, offloaded = await ticks_during(
                lambda: worker.call("benchmark", time.sleep, 0.6)
            )
        finally:
            await worker.aclose()
        return {
            "blocking_beats": blocking_beats,
            "offloaded_beats": offloaded_beats,
            "blocking_lag": blocking["sampled_scheduling_lag_seconds_max"],
            "offloaded_lag": offloaded["sampled_scheduling_lag_seconds_max"],
            "offloaded_wall": offloaded["wall_seconds"],
            "queue_wait_max": worker.metrics.compact()["queue_wait_seconds_max"],
            "execution_max": worker.metrics.compact()["execution_seconds_max"],
        }

    measured = asyncio.run(scenario())
    assert measured["blocking_beats"] == 0, measured
    assert measured["offloaded_beats"] >= 5, measured
    assert measured["blocking_lag"] >= 0.4, measured
    assert measured["offloaded_lag"] < measured["blocking_lag"] * 0.5, measured
    assert measured["offloaded_wall"] >= 0.5, measured
    assert measured["offloaded_lag"] < measured["offloaded_wall"] * 0.5, measured
    assert measured["execution_max"] >= 0.5, measured


async def _async_sleep_on_loop(seconds: float) -> None:
    time.sleep(seconds)


def test_dev_benchmark_queue_wait_and_allocation_stay_bounded() -> None:
    payload = bytearray(500_000)
    entered = threading.Event()
    release = threading.Event()

    def hold() -> None:
        entered.set()
        assert release.wait(5)

    async def scenario() -> tuple[float, int]:
        worker = SerializedDiskWorker()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[1]
            holder = asyncio.create_task(worker.call("benchmark", hold))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            queued = time.monotonic()
            follower = asyncio.create_task(worker.call("benchmark", lambda buf: len(buf), payload))
            await asyncio.sleep(0.1)
            release.set()
            await holder
            assert await follower == 500_000
            waited = time.monotonic() - queued
            growth = tracemalloc.get_traced_memory()[1] - before
            return waited, growth
        finally:
            tracemalloc.stop()
            release.set()
            await worker.aclose()

    waited, growth = asyncio.run(scenario())
    assert waited >= 0.05
    assert growth < 400_000
