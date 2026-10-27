"""P0B2 scanner ordering tests: Stage 1 read -> Stage 2 fetch -> Stage 3 write -> Public delivery -> Lease release."""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle import service
from app.lifecycle import owner_monitoring
from app.runtime.lifecycle_isolation import LifecycleWorker
from app.watch_session import WatchSession
from scripts import run_scan
from tests.test_watch_mode import _patch_watch_runtime, _watch_args
from tests.test_p5a_monitoring_binding_query_repair import _quiet_runtime


@pytest.mark.parametrize("cancel", [False, True])
def test_watch_continuation_fences_public_delivery_next_iteration_and_lease(tmp_path, monkeypatch, cancel):
    """Items 22 & 24: Continuation write finishes before public delivery, next iteration, and lease release."""
    _quiet_runtime(monkeypatch, tmp_path)
    path = _patch_watch_runtime(tmp_path, monkeypatch, symbols=("BTCUSDT",))

    stage3_entered = threading.Event()
    stage3_release = threading.Event()
    stage3_finished = threading.Event()

    original_stage3_write = owner_monitoring.stage_3_continuation_write
    original_enter = WatchSession.__enter__
    original_release = WatchSession._release
    sessions = []
    order = []
    loop_thread = threading.get_ident()

    def wrapped_stage3_write(*args, **kwargs):
        is_worker = threading.current_thread().name == "cci-p0b1-lifecycle"
        assert is_worker, "Stage 3 continuation write must execute on the worker thread"
        order.append("stage3_pending")
        stage3_entered.set()
        assert stage3_release.wait(10), "Stage 3 barrier timed out"
        result = original_stage3_write(*args, **kwargs)
        order.append("stage3_closed")
        stage3_finished.set()
        return result

    def session_enter(self):
        result = original_enter(self)
        sessions.append(self)
        return result

    def session_release(self):
        if stage3_entered.is_set():
            assert stage3_finished.is_set(), "Session lease must not be released before Stage 3 finishes"
        order.append("lease_released")
        return original_release(self)

    async def publish(*args, **kwargs):
        assert stage3_finished.is_set(), "Public delivery must not run before Stage 3 write is closed"
        order.append("public_delivery_boundary")
        return None

    monkeypatch.setattr(owner_monitoring, "stage_3_continuation_write", wrapped_stage3_write)
    monkeypatch.setattr(WatchSession, "__enter__", session_enter)
    monkeypatch.setattr(WatchSession, "_release", session_release)
    monkeypatch.setattr(run_scan, "_deliver_telegram_manual_signals_if_enabled", publish)
    monkeypatch.setattr(run_scan, "_telegram_lifecycle_public_delivery_enabled", lambda args: True)

    async def scenario():
        task = asyncio.create_task(run_scan.main(_watch_args(path, universe_size=1)))
        try:
            assert await asyncio.to_thread(stage3_entered.wait, 5), "Stage 3 was not entered"
            assert sessions and sessions[0]._lease_acquired
            assert "stage3_pending" in order
            assert "public_delivery_boundary" not in order

            if cancel:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)

            assert not task.done(), "Task must not complete while Stage 3 is fenced"
            assert "public_delivery_boundary" not in order
            stage3_release.set()
            await task
        finally:
            stage3_release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert not sessions[0]._lease_acquired
    assert order[-1] == "lease_released"
    assert "stage3_pending" in order
    assert "stage3_closed" in order
    assert order.index("stage3_pending") < order.index("stage3_closed")

    if cancel:
        # On cancellation, public delivery was skipped, but Stage 3 closed cleanly before lease release
        assert "public_delivery_boundary" not in order
        assert order.index("stage3_closed") < order.index("lease_released")
    else:
        # In normal execution, Stage 3 closed before public delivery, which ran before lease release
        assert "public_delivery_boundary" in order
        assert order.index("stage3_closed") < order.index("public_delivery_boundary") < order.index("lease_released")

    # Worker thread must be cleanly joined
    assert not any(thread.name == "cci-p0b1-lifecycle" for thread in threading.enumerate())


def test_shared_worker_between_lifecycle_and_continuation_in_watch(tmp_path, monkeypatch):
    """Item 5: The exact same serialized worker domain is reused for lifecycle and continuation."""
    _quiet_runtime(monkeypatch, tmp_path)
    path = _patch_watch_runtime(tmp_path, monkeypatch, symbols=("BTCUSDT",))

    lifecycle_workers = []
    continuation_workers = []

    original_apply = service.apply_lifecycle_to_run_result
    original_continue = run_scan._continue_owned_plan_monitoring

    def tracking_apply(*args, **kwargs):
        # The worker is executing this synchronous call
        lifecycle_workers.append(threading.current_thread().name)
        return original_apply(*args, **kwargs)

    async def tracking_continue(*args, **kwargs):
        worker = kwargs.get("worker")
        continuation_workers.append(worker)
        return await original_continue(*args, **kwargs)

    monkeypatch.setattr(run_scan, "apply_lifecycle_to_run_result", tracking_apply)
    monkeypatch.setattr(run_scan, "_continue_owned_plan_monitoring", tracking_continue)

    async def scenario():
        task = asyncio.create_task(run_scan.main(_watch_args(path, universe_size=1)))
        await task

    asyncio.run(scenario())

    assert len(continuation_workers) >= 1
    shared_worker = continuation_workers[0]
    assert isinstance(shared_worker, LifecycleWorker)
    assert shared_worker._thread.name == "cci-p0b1-lifecycle"
    assert "cci-p0b1-lifecycle" in lifecycle_workers
    # After watch completion, worker thread is stopped
    assert not shared_worker._thread.is_alive()
