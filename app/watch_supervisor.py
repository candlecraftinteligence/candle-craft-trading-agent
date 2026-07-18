from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from enum import Enum

from pydantic import ValidationError

from app.storage.database import StorageError, UnsupportedSchemaVersionError
from app.universe.symbol_universe import UniverseResolutionError


DEFAULT_FAILURE_BACKOFF_BASE_SEC = 5.0
DEFAULT_FAILURE_BACKOFF_MAX_SEC = 300.0


class WatchIterationStatus(str, Enum):
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    FATAL = "FATAL"


class WatchFailureDisposition(str, Enum):
    RECOVERABLE = "RECOVERABLE"
    FATAL = "FATAL"


class FatalWatchIterationError(RuntimeError):
    """Raised when continuing watch mode could violate a runtime invariant."""


class RecoverableWatchIterationError(RuntimeError):
    """Raised when a future supervised iteration may safely retry."""


@dataclass(frozen=True)
class WatchScheduleDecision:
    scheduled_start_monotonic: float
    actual_start_monotonic: float
    finished_monotonic: float
    next_scheduled_monotonic: float
    duration_seconds: float
    sleep_seconds: float
    cadence_lag_seconds: float
    overrun_seconds: float
    missed_interval_count: int


def failure_backoff_seconds(
    consecutive_failures: int,
    *,
    base_seconds: float = DEFAULT_FAILURE_BACKOFF_BASE_SEC,
    maximum_seconds: float = DEFAULT_FAILURE_BACKOFF_MAX_SEC,
) -> float:
    if consecutive_failures <= 0:
        return 0.0
    exponent = min(consecutive_failures - 1, 30)
    return round(min(float(maximum_seconds), float(base_seconds) * (2**exponent)), 3)


def schedule_after_iteration(
    *,
    scheduled_start_monotonic: float,
    actual_start_monotonic: float,
    finished_monotonic: float,
    interval_seconds: float,
    backoff_seconds: float = 0.0,
) -> WatchScheduleDecision:
    interval = float(interval_seconds)
    if interval <= 0:
        raise ValueError("watch interval must be greater than zero")

    scheduled = float(scheduled_start_monotonic)
    actual = float(actual_start_monotonic)
    finished = max(float(finished_monotonic), actual)
    duration = max(0.0, finished - actual)
    cadence_lag = max(0.0, actual - scheduled)
    first_next_start = scheduled + interval
    overrun = max(0.0, finished - first_next_start)
    missed_intervals = 0
    cadence_next = first_next_start

    if overrun > 0:
        elapsed_intervals = max(0.0, (finished - scheduled) / interval)
        nearest_boundary = round(elapsed_intervals)
        if math.isclose(elapsed_intervals, nearest_boundary, rel_tol=0.0, abs_tol=1e-9):
            next_multiplier = max(1, int(nearest_boundary))
        else:
            next_multiplier = max(1, math.floor(elapsed_intervals) + 1)
        cadence_next = scheduled + (next_multiplier * interval)
        missed_intervals = max(0, next_multiplier - 1)

    retry_not_before = finished + max(0.0, float(backoff_seconds))
    next_scheduled = max(cadence_next, retry_not_before)
    sleep_seconds = max(0.0, next_scheduled - finished)
    return WatchScheduleDecision(
        scheduled_start_monotonic=scheduled,
        actual_start_monotonic=actual,
        finished_monotonic=finished,
        next_scheduled_monotonic=next_scheduled,
        duration_seconds=_seconds(duration),
        sleep_seconds=_seconds(sleep_seconds),
        cadence_lag_seconds=_seconds(cadence_lag),
        overrun_seconds=_seconds(overrun),
        missed_interval_count=missed_intervals,
    )


def classify_watch_exception(exc: Exception | SystemExit) -> WatchFailureDisposition:
    if isinstance(exc, SystemExit):
        if exc.__cause__ is not None and isinstance(exc.__cause__, Exception):
            return classify_watch_exception(exc.__cause__)
        return WatchFailureDisposition.FATAL
    if isinstance(exc, (FatalWatchIterationError, UnsupportedSchemaVersionError)):
        return WatchFailureDisposition.FATAL
    if isinstance(exc, RecoverableWatchIterationError):
        return WatchFailureDisposition.RECOVERABLE
    if isinstance(exc, StorageError):
        cause = _root_cause(exc)
        if _is_transient_sqlite_error(cause) or isinstance(cause, OSError):
            return WatchFailureDisposition.RECOVERABLE
        return WatchFailureDisposition.FATAL
    if isinstance(exc, sqlite3.Error):
        return (
            WatchFailureDisposition.RECOVERABLE
            if _is_transient_sqlite_error(exc)
            else WatchFailureDisposition.FATAL
        )
    if isinstance(exc, (AssertionError, TypeError, ValidationError)):
        return WatchFailureDisposition.FATAL
    if isinstance(exc, (UniverseResolutionError, OSError, TimeoutError)):
        return WatchFailureDisposition.RECOVERABLE
    return WatchFailureDisposition.RECOVERABLE


def _is_transient_sqlite_error(exc: BaseException) -> bool:
    if not isinstance(exc, sqlite3.OperationalError):
        return False
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _root_cause(exc: BaseException) -> BaseException:
    current = exc
    seen: set[int] = set()
    while current.__cause__ is not None and id(current) not in seen:
        seen.add(id(current))
        current = current.__cause__
    return current


def _seconds(value: float) -> float:
    return round(max(0.0, value), 3)


__all__ = [
    "DEFAULT_FAILURE_BACKOFF_BASE_SEC",
    "DEFAULT_FAILURE_BACKOFF_MAX_SEC",
    "FatalWatchIterationError",
    "RecoverableWatchIterationError",
    "WatchFailureDisposition",
    "WatchIterationStatus",
    "WatchScheduleDecision",
    "classify_watch_exception",
    "failure_backoff_seconds",
    "schedule_after_iteration",
]
