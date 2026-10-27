"""Bounded inclusive elapsed-call observations, independent of business status."""

from __future__ import annotations

import inspect
import math
import time
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from typing import Any, Callable

from app.data.dtos import NA

PHASES = (
    "resource_observation", "start_receipt", "universe", "priority_queue",
    "scanner", "lifecycle", "owner_monitoring", "telegram_outbox",
    "symbol_health", "portfolio_selection", "json_output", "scan_core",
    "watch_state_persistence", "scan_storage", "scan_manifest",
    "watch_output", "admin_report", "console_emit", "failure_recording",
)
_current: ContextVar[PhaseProfile | None] = ContextVar("cci_phase_profile", default=None)


class PhaseProfile:
    def __init__(self, clock: Callable[[], float] | None = None) -> None:
        self._clock = clock or time.perf_counter
        self._rows = {name: {"calls": 0, "active_calls": 0, "raised_calls": 0, "unavailable_calls": 0,
                             "observed_seconds": 0.0} for name in PHASES}
        self.resources: dict[str, Any] = {}
        self.loop_lag: Any = None
        self.disk_worker: Any = None
        self.lifecycle_worker: Any = None
        self._phase_stack: list[str] = []
        self._started = self._read_clock()
        try:
            info = time.get_clock_info("perf_counter")
            self.clock_info = {"api": "time.perf_counter", "implementation": info.implementation,
                               "resolution_seconds": info.resolution, "monotonic": info.monotonic,
                               "adjustable": info.adjustable, "injected_clock": clock is not None}
        except Exception:
            self.clock_info = {"api": "time.perf_counter", "status": "Unverified",
                               "resolution_seconds": NA, "injected_clock": clock is not None}

    def _read_clock(self) -> float | None:
        try:
            value = self._clock()
            return float(value) if not isinstance(value, bool) and math.isfinite(value) else None
        except Exception:
            return None

    @contextmanager
    def activate(self):
        token = _current.set(self)
        try:
            yield self
        finally:
            _current.reset(token)

    @contextmanager
    def measure(self, name: str):
        if name not in self._rows:
            raise ValueError("unknown phase name")
        row = self._rows[name]
        start = self._read_clock()
        row["calls"] += 1
        row["active_calls"] += 1
        self._phase_stack.append(name)
        try:
            yield
        except BaseException:
            row["raised_calls"] += 1
            raise
        finally:
            self._phase_stack.pop()
            row["active_calls"] -= 1
            end = self._read_clock()
            if start is None or end is None or end < start or not math.isfinite(end - start):
                row["unavailable_calls"] += 1
            else:
                row["observed_seconds"] += end - start

    @property
    def active_phase(self) -> str | None:
        return self._phase_stack[-1] if self._phase_stack else None

    def call(self, name: str, function: Callable, *args: Any, **kwargs: Any) -> Any:
        monitor = self.loop_lag
        started = self._read_clock() if monitor is not None else None
        try:
            with self.measure(name):
                return function(*args, **kwargs)
        finally:
            if monitor is not None and started is not None:
                ended = self._read_clock()
                if ended is not None and ended >= started and math.isfinite(ended - started):
                    monitor.note_induced_block(name, ended - started)

    async def acall(self, name: str, function: Callable, *args: Any, **kwargs: Any) -> Any:
        with self.measure(name):
            return await function(*args, **kwargs)

    def snapshot(self, *, boundary: str) -> dict[str, Any]:
        end = self._read_clock()
        total = end - self._started if end is not None and self._started is not None and end >= self._started and math.isfinite(end - self._started) else NA
        rows = {}
        for name, value in self._rows.items():
            rows[name] = {
                **value,
                "elapsed_seconds": value["observed_seconds"] if value["calls"] and not value["unavailable_calls"] and not value["active_calls"] else NA,
                "measurement_status": "NOT_OBSERVED" if not value["calls"] else
                    "Unverified" if value["unavailable_calls"] else "IN_PROGRESS" if value["active_calls"] else "OBSERVED",
            }
        report: dict[str, Any] = {"contract": "cci-phase-profile-v1", "boundary": boundary,
                "clock": dict(self.clock_info), "elapsed_seconds": total, "phases": rows,
                "scope": "inclusive_elapsed_calls_may_overlap_do_not_sum",
                "coverage": "instrumented_boundaries_only", "business_success_inferred": False}
        if self.loop_lag is not None:
            report["event_loop"] = self.loop_lag.compact_snapshot()
        if self.disk_worker is not None:
            report["disk_isolation"] = self.disk_worker.compact_snapshot()
        if self.lifecycle_worker is not None:
            snapshot = self.lifecycle_worker.compact_snapshot()
            report["lifecycle_isolation"] = snapshot
            report["lifecycle_isolation"]["LOOP_LAG_DURING_LIFECYCLE"] = (
                self.loop_lag.compact_snapshot()["phase_sampled_lag_seconds_max"].get("lifecycle", NA)
                if self.loop_lag is not None else NA
            )
            report["continuation_isolation"] = {
                "CONTINUATION_READ_WORKER_DURATION": snapshot.get("CONTINUATION_READ_WORKER_DURATION", 0.0),
                "CONTINUATION_WRITE_WORKER_DURATION": snapshot.get("CONTINUATION_WRITE_WORKER_DURATION", 0.0),
                "CONTINUATION_WORKER_QUEUE_WAIT": snapshot.get("CONTINUATION_WORKER_QUEUE_WAIT", 0.0),
                "CONTINUATION_TRANSACTION_RESULT": snapshot.get("CONTINUATION_TRANSACTION_RESULT", "NOT_OBSERVED"),
                "CONTINUATION_COMPLETION_DELAY": snapshot.get("CONTINUATION_COMPLETION_DELAY", 0.0),
                "LOOP_LAG_DURING_MONITORING": (
                    self.loop_lag.compact_snapshot()["phase_sampled_lag_seconds_max"].get("owner_monitoring", NA)
                    if self.loop_lag is not None else NA
                ),
                "IN_FLIGHT_AT_SHUTDOWN": snapshot.get("IN_FLIGHT_AT_SHUTDOWN", 0),
            }
        return report


@contextmanager
def suspend_phase_profile():
    """Keep a copied worker context from mutating the loop-owned profile."""
    token = _current.set(None)
    try:
        yield
    finally:
        _current.reset(token)


def current_observations(*, boundary: str) -> dict[str, Any]:
    profile = _current.get()
    if profile is None:
        return {}
    return {"phase_profile": profile.snapshot(boundary=boundary),
            "resource_observation": dict(profile.resources)}


def profiled(name: str):
    """No active iteration means no sampling. Context follows awaited child tasks."""
    if name not in PHASES:
        raise ValueError("unknown phase name")
    def decorate(function: Callable):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def async_wrapper(*args: Any, **kwargs: Any):
                profile = _current.get()
                if profile is None:
                    return await function(*args, **kwargs)
                return await profile.acall(name, function, *args, **kwargs)
            return async_wrapper
        @wraps(function)
        def wrapper(*args: Any, **kwargs: Any):
            profile = _current.get()
            if profile is None:
                return function(*args, **kwargs)
            return profile.call(name, function, *args, **kwargs)
        return wrapper
    return decorate
