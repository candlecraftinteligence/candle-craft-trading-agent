"""One serialized disk worker for independently owned files.

The worker never receives an open SQLite connection. Admission is one running
operation plus one waiting caller. Cancelling the awaiting coroutine does not
mean the thread has finished writing: the admission lock stays held until the
thread's future completes.
"""

from __future__ import annotations

import asyncio
import queue
import sqlite3
import threading
import time
from collections.abc import Callable
from typing import Any

import concurrent.futures

SAFE_OFFLOAD_OPERATIONS = frozenset({
    "watch_state_persistence",
    "scan_manifest",
    "watch_output",
})
DEFERRED_SQLITE_OPERATIONS = frozenset({
    "lifecycle",
    "owner_monitoring",
    "telegram_outbox",
    "scan_storage",
    "session_receipt",
})


class DiskWorkerSaturated(RuntimeError):
    """A third caller would queue another payload. The call is refused."""


class DiskWorkerClosed(RuntimeError):
    """The worker has been shut down."""


class DiskIsolationMetrics:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.queue_wait_max = 0.0
        self.execution_max = 0.0
        self.completion_max = 0.0
        self.completed = 0
        self.caller_cancelled = 0
        self.finished_after_cancel = 0
        self.failed = 0
        self.backpressure_rejections = 0
        self.admissions_waited = 0
        self.executing = 0
        self.max_executing = 0
        self.inflight = 0
        self._execution_by_operation: dict[str, float] = {}

    def note_queue_wait(self, seconds: float) -> None:
        with self._lock:
            if seconds >= 0.001:
                self.admissions_waited += 1
                self.queue_wait_max = max(self.queue_wait_max, seconds)

    def begin_execute(self) -> None:
        with self._lock:
            self.executing += 1
            self.max_executing = max(self.max_executing, self.executing)

    def end_execute(self, operation: str, seconds: float) -> None:
        with self._lock:
            self.executing = max(0, self.executing - 1)
            if seconds > self.execution_max:
                self.execution_max = seconds
            self._remember(operation, seconds)

    def set_inflight(self, value: int) -> None:
        with self._lock:
            self.inflight = 1 if value else 0

    def note_completed(self, seconds: float) -> None:
        with self._lock:
            self.completed += 1
            self.completion_max = max(self.completion_max, seconds)

    def note_caller_cancelled(self) -> None:
        with self._lock:
            self.caller_cancelled += 1

    def note_finished_after_cancel(self, seconds: float) -> None:
        with self._lock:
            self.finished_after_cancel += 1
            self.completion_max = max(self.completion_max, seconds)

    def note_failed(self) -> None:
        with self._lock:
            self.failed += 1

    def note_backpressure(self) -> None:
        with self._lock:
            self.backpressure_rejections += 1

    def compact(self) -> dict[str, Any]:
        with self._lock:
            return {
                "contract": "cci-disk-worker-v1",
                "max_in_flight": 1,
                "max_waiters": 1,
                "observed_max_executing": self.max_executing,
                "inflight": self.inflight,
                "queue_wait_seconds_max": round(self.queue_wait_max, 3),
                "execution_seconds_max": round(self.execution_max, 3),
                "completion_seconds_max": round(self.completion_max, 3),
                "completed": self.completed,
                "caller_cancelled": self.caller_cancelled,
                "finished_after_cancel": self.finished_after_cancel,
                "failed": self.failed,
                "backpressure_rejections": self.backpressure_rejections,
                "admissions_waited": self.admissions_waited,
                "execution_seconds_max_by_operation": {
                    key: round(value, 3)
                    for key, value in sorted(self._execution_by_operation.items())
                },
            }

    def _remember(self, operation: str, seconds: float) -> None:
        if not isinstance(operation, str) or not operation or len(operation) > 64:
            operation = "other"
        current = self._execution_by_operation.get(operation)
        if current is None and len(self._execution_by_operation) >= 16:
            operation = "other"
            current = self._execution_by_operation.get(operation)
        if current is None or seconds > current:
            self._execution_by_operation[operation] = seconds


class SerializedDiskWorker:
    """Single non-daemon thread. The queue never holds more than one job."""

    def __init__(self) -> None:
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._thread = threading.Thread(target=self._run, name="cci-p0a-disk", daemon=False)
        self._thread.start()
        self._admission: asyncio.Lock | None = None
        self._waiters = 0
        self._closed = False
        self._inflight: concurrent.futures.Future[Any] | None = None
        self._fence: asyncio.Task[None] | None = None
        self.metrics = DiskIsolationMetrics()
        self.shutdown_report: dict[str, Any] = {}

    def compact_snapshot(self) -> dict[str, Any]:
        return self.metrics.compact()

    async def call(
        self,
        operation: str,
        fn: Callable[..., Any],
        /,
        *args: Any,
        epoch_guard: Callable[[], None] | None = None,
        **kwargs: Any,
    ) -> Any:
        if self._closed:
            raise DiskWorkerClosed("disk worker is closed")
        _reject_open_sqlite(args, kwargs)
        await self._acquire_admission()
        submitted = time.monotonic()
        loop = asyncio.get_running_loop()
        release_once = self._release_once()
        owned = True
        try:
            if self._closed:
                raise DiskWorkerClosed("disk worker is closed")
            future: concurrent.futures.Future[Any] = concurrent.futures.Future()
            self._inflight = future
            self.metrics.set_inflight(1)
            self._queue.put_nowait((operation, fn, args, kwargs, epoch_guard, future, loop, release_once))
            try:
                result = await asyncio.shield(asyncio.wrap_future(future))
            except (asyncio.CancelledError, KeyboardInterrupt):
                self.metrics.note_caller_cancelled()
                self._fence = asyncio.create_task(
                    self._finish_fenced(future, submitted),
                    name="cci-p0a-disk-fence",
                )
                owned = False
                raise
            self.metrics.note_completed(time.monotonic() - submitted)
            return result
        finally:
            if owned:
                release_once()

    async def settle(self) -> None:
        """Wait until any fenced or in-flight write has finished."""
        fence = self._fence
        if fence is not None and not fence.done():
            await asyncio.shield(fence)
        lock = self._ensure_lock()
        await lock.acquire()
        lock.release()

    async def settle_after_cancellation(self) -> None:
        """Finish the write even though this task was cancelled.

        Clearing the cancellation count lets the shutdown await run. The watch
        loop already treats cancellation as an orderly stop, so the count is
        not restored.
        """
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
        await self.settle()

    async def aclose(self) -> None:
        if self._closed:
            return
        inflight_at_shutdown = self.metrics.compact()["inflight"]
        self._closed = True
        await self.settle_after_cancellation()
        try:
            self._queue.put_nowait(None)
        except queue.Full:
            self.shutdown_report = {
                "inflight_at_shutdown_request": inflight_at_shutdown,
                "awaited_to_completion": False,
                "thread_alive": self._thread.is_alive(),
            }
            raise RuntimeError("disk worker could not accept its shutdown sentinel")
        await asyncio.shield(asyncio.to_thread(self._thread.join))
        self.shutdown_report = {
            "inflight_at_shutdown_request": inflight_at_shutdown,
            "awaited_to_completion": True,
            "thread_alive": self._thread.is_alive(),
        }

    async def _finish_fenced(self, future: concurrent.futures.Future[Any], submitted: float) -> None:
        try:
            await asyncio.wrap_future(future)
        except Exception:
            # The worker thread already counted the failure. The cancelled
            # caller must not observe a successful result.
            return
        else:
            self.metrics.note_finished_after_cancel(time.monotonic() - submitted)

    async def _acquire_admission(self) -> None:
        lock = self._ensure_lock()
        queued = time.monotonic()
        waiting = False
        if lock.locked():
            if self._waiters >= 1:
                self.metrics.note_backpressure()
                raise DiskWorkerSaturated(
                    "disk worker admits one in-flight operation and one waiting caller"
                )
            self._waiters += 1
            waiting = True
        try:
            await lock.acquire()
        finally:
            if waiting:
                self._waiters -= 1
        self.metrics.note_queue_wait(time.monotonic() - queued)

    def _release_once(self) -> Callable[[], None]:
        """Release admission exactly once, from the caller or the worker thread."""
        token = {"released": False}
        gate = threading.Lock()

        def release_once() -> None:
            with gate:
                if token["released"]:
                    return
                token["released"] = True
            self._inflight = None
            self.metrics.set_inflight(0)
            self._release_admission()

        return release_once

    def _release_admission(self) -> None:
        lock = self._admission
        if lock is not None and lock.locked():
            lock.release()

    def _ensure_lock(self) -> asyncio.Lock:
        if self._admission is None:
            self._admission = asyncio.Lock()
        return self._admission

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                operation, fn, args, kwargs, epoch_guard, future, loop, release_once = item
                self.metrics.begin_execute()
                started = time.monotonic()
                try:
                    if epoch_guard is not None:
                        epoch_guard()
                    result = fn(*args, **kwargs)
                except Exception as exc:
                    self.metrics.note_failed()
                    if not future.done():
                        future.set_exception(exc)
                except BaseException as exc:
                    self.metrics.note_failed()
                    if not future.done():
                        future.set_exception(RuntimeError(type(exc).__name__))
                else:
                    if not future.done():
                        future.set_result(result)
                finally:
                    self.metrics.end_execute(str(operation), time.monotonic() - started)
                    try:
                        loop.call_soon_threadsafe(release_once)
                    except RuntimeError:
                        release_once()
            finally:
                self._queue.task_done()


def _reject_open_sqlite(args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
    for value in (*args, *kwargs.values()):
        if isinstance(value, sqlite3.Connection):
            raise RuntimeError("P0A disk worker refuses an open SQLite connection")
