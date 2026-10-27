"""Synthetic scanner fixtures prove publication, continuation and lease ordering."""

from __future__ import annotations

import asyncio
import sqlite3
import threading

import pytest

from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle import service
from app.storage.database import StorageError
from app.watch_session import WatchSession
from scripts import run_scan
from tests.test_watch_mode import _patch_watch_runtime, _watch_args
from tests.test_p5a_monitoring_binding_query_repair import _quiet_runtime


@pytest.mark.parametrize("cancel,failure", [(False, None), (True, None), (True, "locked"), (True, "wrapped_busy")])
def test_watch_lifecycle_fences_publication_continuation_next_iteration_and_lease(tmp_path, monkeypatch, cancel, failure):
    _quiet_runtime(monkeypatch, tmp_path)
    path = _patch_watch_runtime(tmp_path, monkeypatch, symbols=("BTCUSDT",))
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_exit = SQLiteSetupLifecycleRepository.__exit__
    original_enter = WatchSession.__enter__
    original_release = WatchSession._release
    original_monitor = run_scan._continue_owned_plan_monitoring
    sessions, order = [], []
    loop_thread = threading.get_ident()
    if failure is not None:
        def recoverable_failure(*args, **kwargs):
            try:
                raise sqlite3.OperationalError("database is locked")
            except sqlite3.OperationalError as exc:
                if failure == "wrapped_busy":
                    raise StorageError("synthetic busy wrapper") from exc
                raise
        monkeypatch.setattr(service, "monitor_tracking_obligations", recoverable_failure)

    def repository_exit(self, *args):
        is_worker = threading.current_thread().name == "cci-p0b1-lifecycle"
        if is_worker:
            assert self.connection.in_transaction
            order.append("transaction_pending")
            entered.set()
            assert release.wait(10), "scanner test barrier timed out"
        result = original_exit(self, *args)
        if is_worker:
            assert self.connection is None
            order.append("transaction_closed")
            finished.set()
        return result

    def session_enter(self):
        result = original_enter(self)
        sessions.append(self)
        return result

    def session_release(self):
        assert finished.is_set()
        order.append("lease_released")
        return original_release(self)

    async def continuation(*args, **kwargs):
        assert finished.is_set() and threading.get_ident() == loop_thread
        order.append("continuation")
        return await original_monitor(*args, **kwargs)

    async def publish(*args, **kwargs):
        assert finished.is_set()
        order.append("public_delivery_boundary")
        return None

    monkeypatch.setattr(SQLiteSetupLifecycleRepository, "__exit__", repository_exit)
    monkeypatch.setattr(WatchSession, "__enter__", session_enter)
    monkeypatch.setattr(WatchSession, "_release", session_release)
    monkeypatch.setattr(run_scan, "_continue_owned_plan_monitoring", continuation)
    monkeypatch.setattr(run_scan, "_deliver_telegram_manual_signals_if_enabled", publish)
    # Enable only the injected publication boundary; no Telegram client is used.
    monkeypatch.setattr(run_scan, "_telegram_lifecycle_public_delivery_enabled", lambda args: True)

    async def scenario():
        task = asyncio.create_task(run_scan.main(_watch_args(path, universe_size=1)))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert sessions and sessions[0]._lease_acquired
            assert order == ["transaction_pending"]
            if cancel:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
            assert not task.done() and sessions[0]._lease_acquired
            assert "public_delivery_boundary" not in order and "continuation" not in order
            release.set()
            await task
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert not sessions[0]._lease_acquired
    shutdown = sessions[0].state["lifecycle_isolation_shutdown"]
    assert shutdown["shutdown"] == {"IN_FLIGHT_AT_SHUTDOWN": 0, "joined": True, "thread_alive": False}
    assert shutdown["COMMIT_OR_ROLLBACK_RESULT"] == ("ROLLED_BACK" if failure else "COMMITTED")
    assert shutdown["failed"] == (1 if failure else 0)
    assert shutdown["connection_closed"] is True
    assert order[-1] == "lease_released"
    if cancel:
        assert order == ["transaction_pending", "transaction_closed", "lease_released"]
    else:
        assert order.index("transaction_closed") < order.index("continuation") < order.index("public_delivery_boundary")
    assert not any(thread.name == "cci-p0b1-lifecycle" for thread in threading.enumerate())
