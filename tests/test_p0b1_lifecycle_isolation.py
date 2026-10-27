"""P0B1 ownership and equivalence; every database and frame is synthetic DEV data."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import tracemalloc
import weakref
from contextvars import ContextVar
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial

import pytest
from websockets.sync.server import serve

from app.alerts.telegram_lifecycle import telegram_alert_decision_for_symbol
from app.core.phase_profile import PhaseProfile, current_observations
from app.data.candle_batch_evidence import observe_adapter_normalized_batch
from app.lifecycle import service
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.microstructure.reconnect import FeedRecovery
from app.microstructure.service import BinanceWebSocketTransport
from app.research.durable_source_replay import capture
from app.runtime.lifecycle_isolation import LifecycleWorker, LifecycleWorkerClosed, LifecycleWorkerSaturated
from app.runtime.loop_lag import LoopLagMonitor
from app.runtime_epoch.errors import RuntimeEpochError, RuntimeEpochOriginError
from app.runtime_epoch.origin import register_operational_run
from app.storage.database import StorageError
from app.watch_supervisor import WatchFailureDisposition, classify_watch_exception
from scripts import run_scan
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY, bootstrap_operational_test_database
from tests.test_f04_durable_source_replay import _candle, _capture_ids, _latched, _own, _replay
from tests.test_lifecycle import _a_grade_watch_symbol, _confirmed_candidate_symbol, _near_miss_symbol, _scan_result

pytestmark = pytest.mark.no_auto_epoch
NOW = datetime(2026, 4, 1, 0, 20, tzinfo=UTC)
RUN_ID = "p0b1-synthetic-run"


def _database(path):
    bootstrap_operational_test_database(path)
    with closing(sqlite3.connect(path)) as connection:
        register_operational_run(connection, run_id=RUN_ID, registered_at="2026-03-01T00:00:00Z")
        connection.commit()
    return path


def _input(symbol=None):
    symbol = symbol or _near_miss_symbol()
    candles = (_candle(0, high="101", low="99"), _candle(1, high="103", low="100"))
    delivery = observe_adapter_normalized_batch(
        candles, requested_symbol=symbol.symbol, requested_interval="5m", requested_limit=2,
        effective_symbol=symbol.symbol, effective_interval="5m", effective_limit=2,
        endpoint_path="/synthetic/fixture", adapter_class_label="p0b1-loopback-fixture",
        capture_clock=lambda: NOW, occurrence_factory=lambda: "p0b1-fixture-" + symbol.symbol,
        http_client_injected=True,
    )
    symbol = symbol.model_copy(update={
        "evaluation_origin_kind": "live_scan", "evaluation_completed_at": NOW,
        "lifecycle_decision_timestamp": NOW, "lifecycle_execution_timeframe": "5m",
        "lifecycle_execution_candles": candles, "lifecycle_execution_batch_delivery": delivery,
    })
    return _scan_result(symbol)


def _apply(path, result, **kwargs):
    return service.apply_lifecycle_to_run_result(
        result, database_path=path, scan_run_id=RUN_ID, now=NOW.isoformat(),
        expected_identity=SYNTHETIC_IDENTITY, **kwargs,
    )


def _snapshot(path):
    with closing(sqlite3.connect(path)) as connection:
        tables = connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name").fetchall()
        return {**{name: sorted(connection.execute('SELECT * FROM "' + name + '"').fetchall(), key=repr)
                   for (name,) in tables},
                "schema": connection.execute("SELECT * FROM sqlite_master ORDER BY name").fetchall(),
                "version": connection.execute("PRAGMA user_version").fetchall()}


def _track_connections(monkeypatch, *, gate_at=None, entered=None, release=None, fail_commit=False):
    """Real sqlite connections retain check_same_thread and normal epoch validation."""
    calls = []
    original = sqlite3.connect

    class Tracked(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            calls.append((id(self), "open", threading.get_ident()))

        def execute(self, sql, *args, **kwargs):
            calls.append((id(self), sql, threading.get_ident()))
            result = super().execute(sql, *args, **kwargs)
            if gate_at == "transaction" and sql == "BEGIN IMMEDIATE":
                entered.set()
                assert release.wait(10), "transaction test barrier timed out"
            return result

        def commit(self):
            calls.append((id(self), "commit", threading.get_ident()))
            if gate_at == "commit" and self.in_transaction:
                entered.set()
                assert release.wait(10), "commit test barrier timed out"
            if fail_commit and self.in_transaction:
                raise sqlite3.OperationalError("synthetic commit failure")
            return super().commit()

        def rollback(self):
            calls.append((id(self), "rollback", threading.get_ident()))
            return super().rollback()

        def close(self):
            calls.append((id(self), "close", threading.get_ident()))
            return super().close()

    def connect(*args, **kwargs):
        return original(*args, **{**kwargs, "factory": Tracked})

    monkeypatch.setattr(sqlite3, "connect", connect)
    return calls


def test_entire_connection_transaction_monitoring_and_capture_exit_owned_by_worker(tmp_path, monkeypatch):
    path = _database(tmp_path / "ownership.sqlite")
    calls = _track_connections(monkeypatch)
    monitored = []
    original = service.monitor_tracking_obligations

    def monitor(*args, **kwargs):
        monitored.append(threading.get_ident())
        assert args[0].connection.in_transaction
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "monitor_tracking_obligations", monitor)
    original_exit = capture.observe_repository_exit
    exited = []

    def observe(connection, **kwargs):
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
        exited.append(threading.get_ident())
        return original_exit(connection, **kwargs)

    monkeypatch.setattr(capture, "observe_repository_exit", observe)

    async def scenario():
        worker = LifecycleWorker()
        try:
            updated = await worker.call(_apply, path, _input())
            assert updated.results[0].lifecycle_state is not None
            assert worker.compact_snapshot()["COMMIT_OR_ROLLBACK_RESULT"] == "COMMITTED"
        finally:
            await worker.aclose()
        assert worker.shutdown_report == {"IN_FLIGHT_AT_SHUTDOWN": 0, "joined": True, "thread_alive": False}

    asyncio.run(scenario())
    assert len({thread for _, _, thread in calls}) == 1
    owner = calls[0][2]
    assert owner != threading.get_ident()
    assert monitored == exited == [owner]
    assert sum(op == "open" for _, op, _ in calls) == sum(op == "close" for _, op, _ in calls)
    assert any(op == "BEGIN IMMEDIATE" for _, op, _ in calls)
    assert any(op.startswith("SAVEPOINT lifecycle_symbol") for _, op, _ in calls)


@pytest.mark.parametrize("kind", ["unregistered", "epoch"])
def test_registered_run_and_epoch_checks_fail_closed(tmp_path, kind):
    path = _database(tmp_path / "rejected.sqlite")
    before = _snapshot(path)

    def rejected():
        return service.apply_lifecycle_to_run_result(
            _input(), database_path=path, scan_run_id="missing" if kind == "unregistered" else RUN_ID,
            expected_identity=(SYNTHETIC_IDENTITY if kind == "unregistered" else
                               replace(SYNTHETIC_IDENTITY, epoch_id="wrong")),
        )

    async def scenario():
        worker = LifecycleWorker()
        try:
            with pytest.raises(RuntimeEpochOriginError if kind == "unregistered" else RuntimeEpochError) as raised:
                await worker.call(rejected)
            assert classify_watch_exception(raised.value) == WatchFailureDisposition.FATAL
        finally:
            await worker.aclose()

    asyncio.run(scenario())
    assert _snapshot(path) == before


@pytest.mark.parametrize("symbol_factory", [_near_miss_symbol, _a_grade_watch_symbol, _confirmed_candidate_symbol])
@pytest.mark.parametrize("owner_present", [False, True])
def test_sync_baseline_equals_worker_output_all_tables_public_eligibility_and_replay(tmp_path, monkeypatch, symbol_factory, owner_present):
    original = _database(tmp_path / "baseline.sqlite")
    isolated = tmp_path / "worker.sqlite"
    if owner_present:
        _own(original, _latched())
    # Freeze UUIDs, capture clocks and SQLite CURRENT_TIMESTAMP defaults.
    # Default created_at columns are wall-clock observations, not economic identity.
    original_connect = sqlite3.connect

    def fixed_clock_connect(*args, **kwargs):
        connection = original_connect(*args, **kwargs)
        connection.create_function("current_timestamp", 0, lambda: NOW.strftime("%Y-%m-%d %H:%M:%S"))
        return connection

    monkeypatch.setattr(sqlite3, "connect", fixed_clock_connect)
    monkeypatch.setattr(service, "new_setup_generation_id", lambda **kwargs: "generation-" + kwargs["symbol"])
    monkeypatch.setattr(service, "associate_execution_handoff",
                        partial(service.associate_execution_handoff, capture_clock=lambda: NOW))
    import app.runtime_epoch.origin as origin
    from uuid import UUID
    monkeypatch.setattr(origin, "uuid4", lambda: UUID(int=1))
    with closing(sqlite3.connect(original)) as source, closing(sqlite3.connect(isolated)) as target:
        source.backup(target)
    result = _input(symbol_factory())
    expected = _apply(original, result)

    async def scenario():
        worker = LifecycleWorker()
        try:
            actual = await worker.call(_apply, isolated, result)
            assert actual == expected
            assert actual.scanner_process_summary["status"] == "SUCCESS"
            assert actual.results[0].lifecycle_state is not None
            assert telegram_alert_decision_for_symbol(actual.results[0]) == telegram_alert_decision_for_symbol(expected.results[0])
            once = _snapshot(isolated)
            replayed = await worker.call(_apply, isolated, result)
            assert replayed == _apply(original, result)
            after = _snapshot(isolated)
            # Baseline UPSERT advances SQLite's internal AUTOINCREMENT sequence.
            assert {k: v for k, v in after.items() if k != "sqlite_sequence"} == {
                k: v for k, v in once.items() if k != "sqlite_sequence"
            }, "same run replay duplicated persisted ownership or activation"
        finally:
            await worker.aclose()

    asyncio.run(scenario())
    assert _snapshot(isolated) == _snapshot(original)
    assert result.results[0].lifecycle_state is None


def test_symbol_savepoint_discards_failed_symbol_and_preserves_next(tmp_path, monkeypatch):
    path = _database(tmp_path / "savepoints.sqlite")
    original = service.SetupLifecycleService._apply_to_symbol_result_with_meta

    def fail_first(self, symbol_result, **kwargs):
        updated = original(self, symbol_result, **kwargs)
        if symbol_result.symbol == "BTCUSDT":
            raise ValueError("synthetic symbol failure after write")
        return updated

    monkeypatch.setattr(service.SetupLifecycleService, "_apply_to_symbol_result_with_meta", fail_first)
    result = _input().model_copy(update={"results": (_input().results[0], _input(_near_miss_symbol("ETHUSDT")).results[0])})

    async def scenario():
        worker = LifecycleWorker()
        try:
            updated = await worker.call(_apply, path, result)
            assert updated.scanner_process_summary["status"] == "PARTIAL"
            assert updated.scanner_process_summary["failed_symbols"] == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert not repository.list_records_for_symbol(symbol="BTCUSDT")
        assert repository.list_records_for_symbol(symbol="ETHUSDT")


@pytest.mark.parametrize("gate_at", ["transaction", "commit"])
def test_cancellation_fences_real_transaction_and_second_invocation_until_close(tmp_path, monkeypatch, gate_at):
    path = _database(tmp_path / "cancel.sqlite")
    entered, release = threading.Event(), threading.Event()
    calls = _track_connections(monkeypatch, gate_at=gate_at, entered=entered, release=release)

    async def scenario():
        worker = LifecycleWorker()
        task = asyncio.create_task(worker.call(_apply, path, _input()))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()  # A second cancellation cannot break the fence.
            second = asyncio.create_task(worker.call(lambda: "second"))
            await asyncio.sleep(0)
            assert not task.done() and not second.done()
            assert worker.compact_snapshot()["inflight"] == 1
            with pytest.raises(LifecycleWorkerSaturated):
                await worker.call(lambda: "third")
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert await second == "second"
            assert worker.compact_snapshot()["cancelled_after_admission"] == 1
            assert worker.compact_snapshot()["LIFECYCLE_WORKER_QUEUE_WAIT"] > 0
            assert worker.compact_snapshot()["LIFECYCLE_COMPLETION_DELAY"] >= 0
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await worker.aclose()

    asyncio.run(scenario())
    operations = [op for _, op, _ in calls]
    assert "commit" in operations and "close" in operations
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0] == 1
    # A newly constructed worker after interrupted awaiting reuses durable ownership.
    async def restart():
        worker = LifecycleWorker()
        try:
            await worker.call(_apply, path, _input())
        finally:
            await worker.aclose()
    asyncio.run(restart())
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0] == 1


def test_cancel_before_admission_does_not_execute_and_shutdown_joins_active_work():
    entered, release = threading.Event(), threading.Event()
    executions = []

    def first():
        entered.set()
        assert release.wait(10)
        executions.append("first")

    async def scenario():
        worker = LifecycleWorker()
        task = asyncio.create_task(worker.call(first))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            second = asyncio.create_task(worker.call(executions.append, "second"))
            await asyncio.sleep(0)
            second.cancel()
            with pytest.raises(asyncio.CancelledError):
                await second
            never_started = asyncio.create_task(worker.call(executions.append, "never"))
            never_started.cancel()
            with pytest.raises(asyncio.CancelledError):
                await never_started
            closing = asyncio.create_task(worker.aclose())
            await asyncio.sleep(0)
            closing.cancel()
            await asyncio.sleep(0)
            closing.cancel()
            assert not closing.done() and worker._thread.is_alive()
            release.set()
            await task
            with pytest.raises(asyncio.CancelledError):
                await closing
            await worker.aclose()
            assert worker.shutdown_report == {"IN_FLIGHT_AT_SHUTDOWN": 1, "joined": True, "thread_alive": False}
            with pytest.raises(LifecycleWorkerClosed):
                await worker.call(lambda: None)
        finally:
            release.set()
            await asyncio.gather(task, return_exceptions=True)
            await worker.aclose()

    asyncio.run(scenario())
    assert executions == ["first"]


@pytest.mark.parametrize("failure", ["transaction", "commit", "fatal_after_cancel", "locked_after_cancel", "epoch_after_cancel"])
def test_failures_rollback_propagate_and_allow_controlled_restart(tmp_path, monkeypatch, failure):
    path = _database(tmp_path / "failure.sqlite")
    before = _snapshot(path)
    entered, release = threading.Event(), threading.Event()
    if failure == "commit":
        _track_connections(monkeypatch, fail_commit=True)
    else:
        def fail(*args, **kwargs):
            if failure.endswith("after_cancel"):
                entered.set()
                assert release.wait(10)
                if failure == "epoch_after_cancel":
                    raise RuntimeEpochError("synthetic fatal epoch mismatch")
                if failure == "locked_after_cancel":
                    try:
                        raise sqlite3.OperationalError("database is locked")
                    except sqlite3.OperationalError as exc:
                        raise StorageError("synthetic locked wrapper") from exc
                raise sqlite3.DatabaseError("database disk image is malformed")
            raise RuntimeError("synthetic transaction failure")
        monkeypatch.setattr(service, "monitor_tracking_obligations", fail)

    async def scenario():
        worker = LifecycleWorker()
        task = asyncio.create_task(worker.call(_apply, path, _input()))
        try:
            if failure.endswith("after_cancel"):
                assert await asyncio.to_thread(entered.wait, 5)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
                release.set()
            expected_error = (RuntimeEpochError if failure == "epoch_after_cancel" else
                              StorageError if failure == "locked_after_cancel" else
                              RuntimeError if failure == "transaction" else sqlite3.Error)
            with pytest.raises(expected_error) as raised:
                await task
            if failure in {"fatal_after_cancel", "epoch_after_cancel"}:
                assert classify_watch_exception(raised.value) == WatchFailureDisposition.FATAL
            if failure == "locked_after_cancel":
                assert classify_watch_exception(raised.value) == WatchFailureDisposition.RECOVERABLE
            snapshot = worker.compact_snapshot()
            assert snapshot["COMMIT_OR_ROLLBACK_RESULT"] == ("COMMIT_FAILED_ROLLED_BACK" if failure == "commit" else "ROLLED_BACK")
            assert snapshot["connection_closed"] is True
            assert await worker.call(lambda: "worker survived") == "worker survived"
        finally:
            release.set()
            await worker.aclose()
        replacement = LifecycleWorker()
        try:
            assert await replacement.call(lambda: "restart") == "restart"
        finally:
            await replacement.aclose()

    asyncio.run(scenario())
    assert _snapshot(path) == before


@pytest.mark.parametrize("error", [SystemExit("synthetic fatal exit"), KeyboardInterrupt()])
def test_worker_base_exception_is_received_and_shutdown_is_controlled(error):
    def fail():
        raise error

    async def scenario():
        worker = LifecycleWorker()
        try:
            with pytest.raises(type(error)):
                await worker.call(fail)
        finally:
            await worker.aclose()
        assert not worker._thread.is_alive()
    asyncio.run(scenario())


def test_one_shot_shutdown_cancellation_cannot_mask_fatal_epoch_error(monkeypatch):
    from types import SimpleNamespace

    original_close = LifecycleWorker.aclose

    def fail(*args, **kwargs):
        raise RuntimeEpochError("synthetic epoch mismatch")

    async def close_then_cancel(self):
        await original_close(self)
        raise asyncio.CancelledError()

    monkeypatch.setattr(run_scan, "_lifecycle_enabled", lambda args: True)
    monkeypatch.setattr(run_scan, "apply_lifecycle_to_run_result", fail)
    monkeypatch.setattr(LifecycleWorker, "aclose", close_then_cancel)

    async def scenario():
        with pytest.raises(SystemExit) as raised:
            await run_scan._apply_lifecycle_isolated(SimpleNamespace(database_path="unused"), _input())
        assert isinstance(raised.value.__cause__, RuntimeEpochError)
        assert classify_watch_exception(raised.value) == WatchFailureDisposition.FATAL

    asyncio.run(scenario())


def test_synthetic_loop_lag_wall_time_and_memory_measurements(tmp_path, monkeypatch):
    paths = [_database(tmp_path / name) for name in ("sync.sqlite", "worker.sqlite")]
    original = service.monitor_tracking_obligations

    def slow_monitor(*args, **kwargs):
        # Deliberate DEV I/O stall, not a concurrency-order assertion or market claim.
        time.sleep(0.6)
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "monitor_tracking_obligations", slow_monitor)

    async def measure(path, isolated):
        profile = PhaseProfile()
        monitor = LoopLagMonitor(interval=0.02)
        profile.loop_lag = monitor
        monitor.bind(profile)
        await monitor.start()
        await asyncio.sleep(0.04)
        started = time.perf_counter()
        if isolated:
            worker = LifecycleWorker()
            try:
                await profile.acall("lifecycle", worker.call, _apply, path, _input())
                metrics = worker.compact_snapshot()
            finally:
                await worker.aclose()
        else:
            profile.call("lifecycle", _apply, path, _input())
            metrics = {}
        wall = time.perf_counter() - started
        await asyncio.sleep(0.04)
        await monitor.stop()
        return {"wall_seconds": round(wall, 6), "lag_seconds": round(monitor.max_lag, 6),
                "samples": monitor.samples, "worker": metrics}

    before = asyncio.run(measure(paths[0], False))
    after = asyncio.run(measure(paths[1], True))

    async def memory():
        payload = bytearray(500_000)
        tracemalloc.start()
        worker = LifecycleWorker()
        try:
            assert await worker.call(id, payload) == id(payload)
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
            await worker.aclose()

    growth = asyncio.run(memory())
    assert growth < 400_000, "worker copied the pre-existing payload"
    assert after["samples"] > before["samples"]
    print("P0B1_DEV_MEASUREMENTS=" + json.dumps({"before": before, "after": after,
          "python_allocation_peak_bytes": growth, "production_performance_claim": False}, sort_keys=True))


def test_context_capture_lineage_cleanup_and_profile_remain_owned(tmp_path, monkeypatch):
    path = _database(tmp_path / "capture.sqlite")
    _own(path, _latched())
    evidence = tmp_path / "evidence.sqlite"
    capture.reset_capture_process_state()
    marker = ContextVar("p0b1-test-marker", default="missing")
    original = service.monitor_tracking_obligations

    def inspect_context(*args, **kwargs):
        assert marker.get() == "source-context"
        assert current_observations(boundary="worker") == {}
        return original(*args, **kwargs)

    monkeypatch.setattr(service, "monitor_tracking_obligations", inspect_context)

    async def scenario():
        worker = LifecycleWorker()
        profile = PhaseProfile()
        profile.lifecycle_worker = worker
        token = marker.set("source-context")
        try:
            with profile.activate(), capture.use_capture(capture.CaptureConfig(True, evidence, (path,))):
                await profile.acall("lifecycle", worker.call, _apply, path, _input())
            row = profile.snapshot(boundary="complete")["phases"]["lifecycle"]
            assert row["calls"] == 1 and row["active_calls"] == 0
            assert row["measurement_status"] == "OBSERVED"
        finally:
            marker.reset(token)
            await worker.aclose()

    try:
        asyncio.run(scenario())
        ids = _capture_ids(evidence)
        assert ids and not capture._sessions
        for index, capture_id in enumerate(ids):
            report = _replay(evidence, capture_id, tmp_path / f"replay-{index}")
            assert report["status"] == "REPLAY_MATCH", report
            assert report["claims"]["authenticated_venue_evidence"] is False
            assert report["claims"]["operational_persistence"] is True
    finally:
        capture.reset_capture_process_state()


def test_source_capture_flush_is_inside_completion_fence(tmp_path, monkeypatch):
    path = _database(tmp_path / "flush.sqlite")
    entered, release = threading.Event(), threading.Event()
    original = capture.observe_repository_exit

    def flush(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(capture, "observe_repository_exit", flush)

    async def scenario():
        worker = LifecycleWorker()
        task = asyncio.create_task(worker.call(_apply, path, _input()))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await worker.aclose()

    asyncio.run(scenario())


def test_loopback_receive_progresses_during_real_sqlite_transaction(tmp_path, monkeypatch):
    path = _database(tmp_path / "loopback.sqlite")
    entered, processed = threading.Event(), threading.Event()
    _track_connections(monkeypatch, gate_at="transaction", entered=entered, release=processed)
    recovery = FeedRecovery("p0b1-fixture", 0.1, 1.0)

    def provider(connection):
        assert entered.wait(5)
        connection.send("synthetic unverified fixture frame")
        assert connection.recv(timeout=5) == "fixture complete"

    async def scenario(port):
        connection = await BinanceWebSocketTransport(url=f"ws://127.0.0.1:{port}").connect()
        worker = LifecycleWorker()

        async def receive():
            frame = await connection.recv()
            recovery.note_receive()
            assert frame == "synthetic unverified fixture frame"
            assert worker.compact_snapshot()["inflight"] == 1
            processed.set()
            await connection.send("fixture complete")

        receiver = asyncio.create_task(receive())
        try:
            await worker.call(_apply, path, _input())
            await asyncio.wait_for(receiver, 5)
        finally:
            processed.set()
            await worker.aclose()
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            await connection.close()

    with serve(provider, "127.0.0.1", 0, close_timeout=1) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            asyncio.run(scenario(server.socket.getsockname()[1]))
        finally:
            server.shutdown()
            thread.join(timeout=5)
    assert recovery.receive_count == 1
    # Wire receipt does not authenticate a Binance event or create a confirmation.
    assert recovery.accepted_events == 0


def test_payload_is_borrowed_without_retention_and_open_connections_are_refused(tmp_path):
    async def scenario():
        worker = LifecycleWorker()
        payload = _input()
        reference = weakref.ref(payload)
        connection = sqlite3.connect(tmp_path / "unused.sqlite")
        try:
            with pytest.raises(TypeError, match="worker-owned"):
                await worker.call(lambda value: value, connection)
            assert await worker.call(id, payload) == id(payload)
            del payload
            assert reference() is None
            assert len(json.dumps(worker.compact_snapshot())) < 1600
        finally:
            connection.close()
            await worker.aclose()

    asyncio.run(scenario())
