"""One worker owns the complete synchronous lifecycle call, including cleanup.

Admission permits one operation and one waiting caller. The caller retains
admission through a completion receipt, even across repeated cancellation.
No SQLite connection is handed between threads, and no result is available
before repository exit and source-capture cleanup have returned.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping
import concurrent.futures
from contextlib import contextmanager
from contextvars import copy_context
from dataclasses import dataclass
import queue
import sqlite3
import threading
import time
from typing import Any

from app.core.phase_profile import suspend_phase_profile
from app.lifecycle.transaction_receipt import (
    note_repository_completion,
    observe_transaction,
)
from app.watch_supervisor import FatalWatchIterationError


class LifecycleWorkerClosed(FatalWatchIterationError):
    """No work is admitted once shutdown has started."""


class LifecycleWorkerSaturated(FatalWatchIterationError):
    """A second waiting payload is refused."""


def _is_forbidden_db_object(value: Any) -> bool:
    if isinstance(value, (sqlite3.Connection, sqlite3.Cursor)):
        return True
    cls = type(value)
    module_name = getattr(cls, "__module__", "") or ""
    type_name = getattr(cls, "__name__", "") or ""
    if "repository" in type_name.lower() and (
        module_name.startswith("app.storage")
        or module_name.startswith("app.lifecycle")
        or hasattr(cls, "connection")
        or hasattr(cls, "_connection")
    ):
        return True
    return False


def _contains_forbidden_db_object(value: Any, *, _depth: int = 0) -> bool:
    if _is_forbidden_db_object(value):
        return True
    if _depth >= 3 or isinstance(value, (str, bytes, bytearray)):
        return False
    if isinstance(value, Mapping):
        return any(
            _contains_forbidden_db_object(k, _depth=_depth + 1)
            or _contains_forbidden_db_object(v, _depth=_depth + 1)
            for k, v in value.items()
        )
    if isinstance(value, (tuple, list, set, frozenset)):
        return any(_contains_forbidden_db_object(item, _depth=_depth + 1) for item in value)
    return False


class _ObservedScanStorageConnection:
    """Worker-thread wrapper that records commit/rollback/close receipts for scan_storage."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection
        self._committed = False
        self._commit_failed = False
        self._rolled_back = False
        self._closed = False

    def commit(self) -> None:
        try:
            self._connection.commit()
        except BaseException:
            self._commit_failed = True
            try:
                self._connection.rollback()
            except sqlite3.Error:
                pass
            else:
                self._rolled_back = True
            raise
        else:
            self._committed = True

    def rollback(self) -> None:
        self._connection.rollback()
        self._rolled_back = True

    def close(self) -> None:
        try:
            if not self._committed and not self._rolled_back:
                try:
                    if getattr(self._connection, "in_transaction", False):
                        self._connection.rollback()
                        self._rolled_back = True
                except sqlite3.Error:
                    pass
        finally:
            try:
                self._connection.close()
                self._closed = True
            finally:
                note_repository_completion(
                    committed=self._committed,
                    commit_failed=self._commit_failed,
                    rolled_back=self._rolled_back,
                    closed=self._closed,
                )

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


@contextmanager
def _observe_scan_storage_connection(*, enabled: bool):
    if not enabled:
        yield
        return
    import app.runtime_epoch.startup as startup_mod
    import app.storage.repositories as repositories_mod

    worker_ident = threading.get_ident()
    orig_startup_open = startup_mod.open_operational_database
    orig_repo_open = repositories_mod.open_initialized_database

    def _wrapped_startup_open(*args: Any, **kwargs: Any) -> Any:
        try:
            opened = orig_startup_open(*args, **kwargs)
        except BaseException:
            if threading.get_ident() == worker_ident:
                note_repository_completion(
                    committed=False,
                    commit_failed=False,
                    rolled_back=True,
                    closed=True,
                )
            raise
        if threading.get_ident() == worker_ident:
            if isinstance(opened, sqlite3.Connection):
                return _ObservedScanStorageConnection(opened)
            if (
                isinstance(opened, tuple)
                and len(opened) == 2
                and isinstance(opened[0], sqlite3.Connection)
            ):
                return (_ObservedScanStorageConnection(opened[0]), opened[1])
        return opened

    def _wrapped_repo_open(*args: Any, **kwargs: Any) -> Any:
        try:
            conn = orig_repo_open(*args, **kwargs)
        except BaseException:
            if threading.get_ident() == worker_ident:
                note_repository_completion(
                    committed=False,
                    commit_failed=False,
                    rolled_back=True,
                    closed=True,
                )
            raise
        if threading.get_ident() == worker_ident and isinstance(conn, sqlite3.Connection):
            return _ObservedScanStorageConnection(conn)
        return conn

    startup_mod.open_operational_database = _wrapped_startup_open
    repositories_mod.open_initialized_database = _wrapped_repo_open
    try:
        yield
    finally:
        startup_mod.open_operational_database = orig_startup_open
        repositories_mod.open_initialized_database = orig_repo_open


@dataclass(frozen=True)
class _Receipt:
    value: Any
    error: BaseException | None
    started: float
    finished: float
    transaction_result: str
    connection_closed: bool | None


async def _fenced_wait(future):
    """Delay every cancellation until completion, without cancelling the owner."""
    cancelled = None
    while True:
        try:
            return await asyncio.shield(future), cancelled
        except (asyncio.CancelledError, KeyboardInterrupt) as exc:
            if future.cancelled():
                raise RuntimeError("lifecycle completion owner was cancelled") from exc
            cancelled = cancelled or exc


class LifecycleWorker:
    def __init__(self) -> None:
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._admission = asyncio.Lock()
        self._waiters = 0
        self._closed = False
        self._inflight = False
        self._closing: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue_wait = 0.0
        self._duration = 0.0
        self._completion_delay = 0.0
        self._completed = 0
        self._failed = 0
        self._cancelled = 0
        self._rejected = 0
        self._transaction_result = "NOT_OBSERVED"
        self._connection_closed: bool | None = None
        self._continuation_read_duration = 0.0
        self._continuation_write_duration = 0.0
        self._continuation_queue_wait = 0.0
        self._continuation_completion_delay = 0.0
        self._continuation_transaction_result = "NOT_OBSERVED"
        self._scan_storage_duration = 0.0
        self._scan_storage_queue_wait = 0.0
        self._scan_storage_completion_delay = 0.0
        self._scan_storage_transaction_result = "NOT_OBSERVED"
        self._scan_storage_connection_closed: bool | None = None
        self._scan_storage_completed = 0
        self._scan_storage_failed = 0
        self._scan_storage_cancelled = 0
        self._scan_storage_rejected = 0
        self.shutdown_report: dict[str, Any] = {}
        self._thread = threading.Thread(target=self._run, name="cci-p0b1-lifecycle", daemon=False)
        self._thread.start()

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("lifecycle worker belongs to a different event loop")

    async def call(self, fn: Callable[..., Any], /, *args: Any, stage: str | None = None, **kwargs: Any) -> Any:
        self._bind_loop()
        if self._closed:
            raise LifecycleWorkerClosed("lifecycle worker is closed")
        if _contains_forbidden_db_object(args) or _contains_forbidden_db_object(kwargs):
            raise TypeError("lifecycle worker requires worker-owned connections")
        # A pending cancellation must be delivered even when admission is free.
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError()
        queued = time.perf_counter()
        waiting = self._admission.locked()
        if waiting:
            if self._waiters >= 1:
                if stage == "scan_storage":
                    self._scan_storage_rejected += 1
                else:
                    self._rejected += 1
                raise LifecycleWorkerSaturated("one lifecycle operation and one waiter are permitted")
            self._waiters += 1
        try:
            await self._admission.acquire()
        finally:
            if waiting:
                self._waiters -= 1
        try:
            if self._closed:
                raise LifecycleWorkerClosed("lifecycle worker is closed")
            context = copy_context()
            completion: concurrent.futures.Future[_Receipt] = concurrent.futures.Future()
            self._queue.put_nowait((context, fn, args, kwargs, stage, completion))
            self._inflight = True
            receipt, cancelled = await _fenced_wait(asyncio.wrap_future(completion))
            observed = time.perf_counter()
            if stage == "scan_storage":
                self._scan_storage_duration = max(
                    self._scan_storage_duration,
                    receipt.finished - receipt.started,
                )
                self._scan_storage_queue_wait = max(
                    self._scan_storage_queue_wait,
                    receipt.started - queued,
                )
                self._scan_storage_completion_delay = max(
                    self._scan_storage_completion_delay,
                    observed - receipt.finished,
                )
                self._scan_storage_transaction_result = receipt.transaction_result
                self._scan_storage_connection_closed = receipt.connection_closed
                if receipt.error is None:
                    self._scan_storage_completed += 1
                else:
                    self._scan_storage_failed += 1
                if cancelled is not None:
                    self._scan_storage_cancelled += 1
            elif stage == "continuation_read":
                self._continuation_read_duration = max(self._continuation_read_duration, receipt.finished - receipt.started)
                self._continuation_queue_wait = max(self._continuation_queue_wait, receipt.started - queued)
                self._continuation_completion_delay = max(self._continuation_completion_delay, observed - receipt.finished)
                self._completed += 1
            elif stage == "continuation_write":
                self._continuation_write_duration = max(self._continuation_write_duration, receipt.finished - receipt.started)
                self._continuation_queue_wait = max(self._continuation_queue_wait, receipt.started - queued)
                self._continuation_completion_delay = max(self._continuation_completion_delay, observed - receipt.finished)
                self._continuation_transaction_result = receipt.transaction_result
                self._connection_closed = receipt.connection_closed
                self._completed += 1
            else:
                self._queue_wait = max(self._queue_wait, receipt.started - queued)
                self._duration = max(self._duration, receipt.finished - receipt.started)
                self._completion_delay = max(self._completion_delay, observed - receipt.finished)
                self._completed += 1
                self._transaction_result = receipt.transaction_result
                self._connection_closed = receipt.connection_closed
            if stage != "scan_storage":
                if cancelled is not None:
                    self._cancelled += 1
                if receipt.error is not None:
                    self._failed += 1
            if receipt.error is not None:
                # Fatal epoch/SQLite failures must remain visible after cancellation.
                if cancelled is not None:
                    receipt.error._cci_lifecycle_interruption = cancelled
                raise receipt.error
            if cancelled is not None:
                raise cancelled
            return receipt.value
        finally:
            self._inflight = False
            self._admission.release()

    async def aclose(self) -> None:
        """Stop admission, wait for the caller's receipt, then join the thread."""
        self._bind_loop()
        if self._closing is None:
            self._closed = True
            self.shutdown_report = {"IN_FLIGHT_AT_SHUTDOWN": int(self._inflight)}
            self._closing = asyncio.create_task(self._shutdown(), name="cci-p0b1-lifecycle-shutdown")
        _, cancelled = await _fenced_wait(self._closing)
        if cancelled is not None:
            raise cancelled

    async def _shutdown(self) -> None:
        async with self._admission:
            self._queue.put_nowait(None)
            # Only the join is delegated; transaction ownership stays in our thread.
            await asyncio.to_thread(self._thread.join)
            self.shutdown_report.update({"joined": True, "thread_alive": self._thread.is_alive()})

    def scan_storage_snapshot(self) -> dict[str, Any]:
        return {
            "contract": "cci-scan-storage-isolation-v1",
            "worker": "cci-p0b1-lifecycle",
            "SCAN_STORAGE_WORKER_QUEUE_WAIT": round(self._scan_storage_queue_wait, 6),
            "SCAN_STORAGE_WORKER_DURATION": round(self._scan_storage_duration, 6),
            "SCAN_STORAGE_COMPLETION_DELAY": round(self._scan_storage_completion_delay, 6),
            "SCAN_STORAGE_TRANSACTION_RESULT": self._scan_storage_transaction_result,
            "scan_storage_connection_closed": self._scan_storage_connection_closed,
            "scan_storage_completed": self._scan_storage_completed,
            "scan_storage_failed": self._scan_storage_failed,
            "scan_storage_cancelled_after_admission": self._scan_storage_cancelled,
            "scan_storage_backpressure_rejections": self._scan_storage_rejected,
        }

    def compact_snapshot(self) -> dict[str, Any]:
        # Only the owning event loop mutates these bounded observations.
        return {
            "contract": "cci-lifecycle-worker-v1", "max_in_flight": 1, "max_waiters": 1,
            "inflight": int(self._inflight), "waiters": self._waiters,
            "LIFECYCLE_WORKER_QUEUE_WAIT": round(self._queue_wait, 6),
            "LIFECYCLE_WORKER_DURATION": round(self._duration, 6),
            "LIFECYCLE_COMPLETION_DELAY": round(self._completion_delay, 6),
            "COMMIT_OR_ROLLBACK_RESULT": self._transaction_result,
            "connection_closed": self._connection_closed,
            "completed": self._completed, "failed": self._failed,
            "cancelled_after_admission": self._cancelled, "backpressure_rejections": self._rejected,
            "CONTINUATION_READ_WORKER_DURATION": round(self._continuation_read_duration, 6),
            "CONTINUATION_WRITE_WORKER_DURATION": round(self._continuation_write_duration, 6),
            "CONTINUATION_WORKER_QUEUE_WAIT": round(self._continuation_queue_wait, 6),
            "CONTINUATION_TRANSACTION_RESULT": self._continuation_transaction_result,
            "CONTINUATION_COMPLETION_DELAY": round(self._continuation_completion_delay, 6),
            "SCAN_STORAGE_WORKER_QUEUE_WAIT": round(self._scan_storage_queue_wait, 6),
            "SCAN_STORAGE_WORKER_DURATION": round(self._scan_storage_duration, 6),
            "SCAN_STORAGE_COMPLETION_DELAY": round(self._scan_storage_completion_delay, 6),
            "SCAN_STORAGE_TRANSACTION_RESULT": self._scan_storage_transaction_result,
            "scan_storage_connection_closed": self._scan_storage_connection_closed,
            "scan_storage_completed": self._scan_storage_completed,
            "scan_storage_failed": self._scan_storage_failed,
            "scan_storage_cancelled_after_admission": self._scan_storage_cancelled,
            "scan_storage_backpressure_rejections": self._scan_storage_rejected,
            "IN_FLIGHT_AT_SHUTDOWN": self.shutdown_report.get("IN_FLIGHT_AT_SHUTDOWN", int(self._inflight)),
            "shutdown": dict(self.shutdown_report),
        }

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                context, fn, args, kwargs, stage, completion = item
                item = None
                receipt = context.run(self._execute, fn, args, kwargs, stage)
                del context, fn, args, kwargs, stage
                completion.set_result(receipt)
                # Do not retain the previous scan while the next scan is built.
                del completion, receipt
            finally:
                item = None
                self._queue.task_done()

    @staticmethod
    def _execute(fn, args, kwargs, stage: str | None = None) -> _Receipt:
        started = time.perf_counter()
        value = None
        error = None
        with (
            suspend_phase_profile(),
            observe_transaction() as transaction,
            _observe_scan_storage_connection(enabled=(stage == "scan_storage")),
        ):
            try:
                value = fn(*args, **kwargs)
                if _contains_forbidden_db_object(value):
                    if isinstance(value, sqlite3.Connection):
                        value.close()
                    elif isinstance(getattr(value, "connection", None), sqlite3.Connection):
                        value.connection.close()
                    value = None
                    raise TypeError("lifecycle worker cannot return an open connection")
                if stage == "scan_storage" and not isinstance(value, str):
                    value = None
                    raise TypeError("scan_storage worker call must return detached scan_run_id str")
            except BaseException as exc:
                # Transport fatal exceptions as data, never as an unobserved Task.
                error = exc
        return _Receipt(value, error, started, time.perf_counter(),
                        transaction.outcome, transaction.connection_closed)
