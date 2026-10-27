"""Bounded event-loop scheduling lag. Durations use a monotonic clock."""

from __future__ import annotations

import asyncio
import math
import time
from datetime import datetime, timezone
from typing import Any, Callable


class LoopLagMonitor:
    """Sample how late the loop runs a timer. This is not phase wall-clock time.

    Samples keep only maxima. Phase names are bounded. Nothing here is logged.
    """

    def __init__(self, *, interval: float = 0.5, clock: Callable[[], float] | None = None) -> None:
        if isinstance(interval, bool) or not isinstance(interval, (int, float)):
            raise ValueError("loop lag interval must be a finite number of seconds")
        if not math.isfinite(interval) or interval < 0.01 or interval > 5:
            raise ValueError("loop lag interval must be between 0.01 and 5 seconds")
        self.interval = float(interval)
        self._clock = clock or time.monotonic
        self._task: asyncio.Task[None] | None = None
        self._profile: Any = None
        self.samples = 0
        self.max_lag = 0.0
        self.last_lag = 0.0
        self.max_induced = 0.0
        self._phase_lag: dict[str, float] = {}
        self._phase_induced: dict[str, float] = {}
        self.started_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    def bind(self, profile: Any) -> None:
        self._profile = profile

    def note_induced_block(self, phase: str, seconds: float) -> None:
        """A synchronous call occupied the loop thread for ``seconds``."""
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            return
        if not math.isfinite(seconds) or seconds <= 0:
            return
        self.max_induced = max(self.max_induced, float(seconds))
        self._remember(self._phase_induced, phase, float(seconds))

    def record_sample(self, lag_seconds: float, *, phase: str | None = None) -> None:
        if isinstance(lag_seconds, bool) or not isinstance(lag_seconds, (int, float)):
            return
        if not math.isfinite(lag_seconds):
            return
        lag = max(0.0, float(lag_seconds))
        self.samples += 1
        self.last_lag = lag
        if lag > self.max_lag:
            self.max_lag = lag
        if phase:
            self._remember(self._phase_lag, phase, lag)

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._task = asyncio.create_task(self._sample(), name="cci-p0a-loop-lag")

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is None:
            return
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    def compact_snapshot(self) -> dict[str, Any]:
        return {
            "contract": "cci-loop-lag-v1",
            "samples": self.samples,
            "sampled_scheduling_lag_seconds_max": round(self.max_lag, 3),
            "sampled_scheduling_lag_seconds_last": round(self.last_lag, 3),
            "induced_synchronous_block_seconds_max": round(self.max_induced, 3),
            "phase_sampled_lag_seconds_max": {
                key: round(value, 3) for key, value in sorted(self._phase_lag.items()) if value > 0
            },
            "phase_induced_block_seconds_max": {
                key: round(value, 3) for key, value in sorted(self._phase_induced.items()) if value > 0
            },
            "sample_interval_seconds": self.interval,
            "clock": "monotonic",
            "observed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "started_at": self.started_at,
            "distinct_from": "phase_wall_clock",
        }

    async def _sample(self) -> None:
        # call_later, not asyncio.sleep. Watch tests replace sleep with a
        # coroutine that finishes inline; awaiting that never returns to the
        # loop and would stall every other task.
        loop = asyncio.get_running_loop()
        interval = self.interval
        next_due = self._clock() + interval
        try:
            while True:
                await _wait_on_loop_clock(loop, interval)
                now = self._clock()
                self.record_sample(now - next_due, phase=self._active_phase())
                next_due = now + interval
        except asyncio.CancelledError:
            return

    def _active_phase(self) -> str | None:
        profile = self._profile
        if profile is None:
            return None
        phase = getattr(profile, "active_phase", None)
        return phase if isinstance(phase, str) else None

    @staticmethod
    def _remember(store: dict[str, float], phase: str, seconds: float) -> None:
        if not isinstance(phase, str) or not phase or len(phase) > 64:
            return
        current = store.get(phase)
        if current is None and len(store) >= 24:
            return
        if current is None or seconds > current:
            store[phase] = seconds


async def _wait_on_loop_clock(loop: asyncio.AbstractEventLoop, delay: float) -> None:
    future: asyncio.Future[None] = loop.create_future()
    handle = loop.call_later(delay, _release_timer, future)
    try:
        await future
    finally:
        handle.cancel()


def _release_timer(future: asyncio.Future[None]) -> None:
    if not future.done():
        future.set_result(None)
