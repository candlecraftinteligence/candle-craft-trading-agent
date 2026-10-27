"""P0A: file persistence can leave the scanner loop; SQLite ownership cannot."""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import threading
import time
import tracemalloc
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from websockets.exceptions import ConnectionClosed
from websockets.frames import Close
from websockets.sync.server import serve

from app.cmc.runtime import COLLECTION_DEADLINE_SECONDS, Service
from app.core.minimum_rr import DEFAULT_CONFIGURED_MINIMUM_RR
from app.core.phase_profile import PhaseProfile, profiled
from app.microstructure.reconnect import FeedRecovery
from app.microstructure.service import BinanceWebSocketTransport
from app.pipeline.scanner_runner import ScannerRunConfig, ScannerRunResult
from app.runtime.disk_isolation import (
    DEFERRED_SQLITE_OPERATIONS,
    SAFE_OFFLOAD_OPERATIONS,
    DiskWorkerSaturated,
    SerializedDiskWorker,
)
from app.runtime.loop_lag import LoopLagMonitor
from app.storage.database import SCHEMA_VERSION
from app.watch_mode import WatchModeError, WatchState, load_watch_state, save_watch_state
import scripts.run_scan as run_scan

pytestmark = pytest.mark.no_auto_epoch


class _Client:
    async def aclose(self) -> None:
        return None


def test_loop_lag_contract_is_bounded_and_distinct_from_wall_clock() -> None:
    monitor = LoopLagMonitor(interval=0.05)
    monitor.record_sample(0.4, phase="scan_manifest")
    monitor.record_sample(0.1, phase="scanner")
    monitor.note_induced_block("scan_storage", 1.25)
    snapshot = monitor.compact_snapshot()
    assert snapshot["contract"] == "cci-loop-lag-v1"
    assert snapshot["sampled_scheduling_lag_seconds_max"] == 0.4
    assert snapshot["sampled_scheduling_lag_seconds_last"] == 0.1
    assert snapshot["induced_synchronous_block_seconds_max"] == 1.25
    assert snapshot["phase_sampled_lag_seconds_max"]["scan_manifest"] == 0.4
    assert snapshot["distinct_from"] == "phase_wall_clock"
    assert "token" not in json.dumps(snapshot).lower()
    assert len(json.dumps(snapshot).encode()) < 1500


def test_loop_lag_monitor_yields_when_asyncio_sleep_does_not(monkeypatch: pytest.MonkeyPatch) -> None:
    """A no-op sleep must not turn the sampler into a synchronous spin."""

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_sleep)

    async def scenario() -> None:
        monitor = LoopLagMonitor(interval=0.05)
        await monitor.start()
        loop = asyncio.get_running_loop()
        gate: asyncio.Future[None] = loop.create_future()
        loop.call_later(0.2, gate.set_result, None)
        await gate
        assert monitor.samples >= 1
        await monitor.stop()

    box: dict[str, object] = {}

    def run() -> None:
        try:
            asyncio.run(scenario())
        except BaseException as exc:
            box["error"] = exc
        else:
            box["ok"] = True

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(2)
    assert not thread.is_alive()
    assert box.get("error") is None
    assert box.get("ok") is True


def test_synchronous_phase_records_induced_block_only_when_a_monitor_is_attached() -> None:
    profile = PhaseProfile()
    bare = profile.snapshot(boundary="bare")
    assert "event_loop" not in bare
    assert len(json.dumps(bare).encode()) < 7000
    monitor = LoopLagMonitor(interval=0.05)
    profile.loop_lag = monitor
    profile.call("lifecycle", lambda: time.sleep(0.05))
    induced = profile.snapshot(boundary="blocked")["event_loop"]
    assert induced["induced_synchronous_block_seconds_max"] >= 0.04
    assert induced["distinct_from"] == "phase_wall_clock"


def test_offload_allowlist_excludes_sqlite_owned_phases() -> None:
    text = inspect.getsource(run_scan._run_watch_mode)
    assert SAFE_OFFLOAD_OPERATIONS == frozenset({
        "watch_state_persistence", "scan_manifest", "watch_output",
    })
    assert "lifecycle" in DEFERRED_SQLITE_OPERATIONS
    assert "scan_storage" in DEFERRED_SQLITE_OPERATIONS
    assert "telegram_outbox" in DEFERRED_SQLITE_OPERATIONS
    for phase in SAFE_OFFLOAD_OPERATIONS:
        assert f'"{phase}"' in text
    assert 'profile.call("scan_storage"' in text
    assert 'profile.call("watch_state_persistence"' not in text
    assert 'profile.call("scan_manifest"' not in text
    assert 'profile.call("watch_output"' not in text
    for function in (
        run_scan._apply_lifecycle_if_enabled,
        run_scan._store_watch_iteration_scan_run,
        run_scan._deliver_telegram_manual_signals_if_enabled,
    ):
        assert "SerializedDiskWorker" not in inspect.getsource(function)
    assert "check_same_thread" not in Path("app/storage/database.py").read_text(encoding="utf-8")
    assert SCHEMA_VERSION == 27
    assert DEFAULT_CONFIGURED_MINIMUM_RR == Decimal("2.5")


def test_lifecycle_apply_stays_on_the_calling_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[int] = []

    def apply(result, **kwargs):
        del kwargs
        seen.append(threading.get_ident())
        return result

    monkeypatch.setattr(run_scan, "_lifecycle_enabled", lambda args: True)
    monkeypatch.setattr(run_scan, "apply_lifecycle_to_run_result", apply)
    args = SimpleNamespace(database_path="unused-fixture", runtime_identity=None)
    assert run_scan._apply_lifecycle_if_enabled(args, {"result": True}, scan_run_id="fixture") == {"result": True}
    assert seen == [threading.get_ident()]


def test_worker_refuses_an_open_sqlite_connection(tmp_path: Path) -> None:
    async def scenario() -> None:
        worker = SerializedDiskWorker()
        connection = sqlite3.connect(tmp_path / "unused.db")
        try:
            with pytest.raises(RuntimeError, match="open SQLite connection"):
                await worker.call("test", lambda conn: conn, connection)
            assert worker.metrics.compact()["observed_max_executing"] == 0
        finally:
            connection.close()
            await worker.aclose()

    asyncio.run(scenario())


def test_worker_runs_off_the_loop_and_propagates_exceptions(tmp_path: Path) -> None:
    path = tmp_path / "after.txt"

    async def scenario() -> None:
        worker = SerializedDiskWorker()
        seen: dict[str, object] = {}

        def identify() -> None:
            seen["name"] = threading.current_thread().name
            seen["ident"] = threading.get_ident()

        def explode() -> None:
            raise RuntimeError("disk failed")

        try:
            await worker.call("test", identify)
            with pytest.raises(RuntimeError, match="disk failed"):
                await worker.call("test", explode)
            await worker.call("test", path.write_text, "ok", encoding="utf-8")
        finally:
            await worker.aclose()
        assert seen["name"] == "cci-p0a-disk"
        assert seen["ident"] != threading.get_ident()
        assert path.read_text(encoding="utf-8") == "ok"
        metrics = worker.metrics.compact()
        assert metrics["failed"] == 1
        assert metrics["observed_max_executing"] == 1
        assert worker.shutdown_report["awaited_to_completion"] is True
        assert worker.shutdown_report["thread_alive"] is False

    asyncio.run(scenario())


def test_backpressure_rejects_a_third_payload_and_serializes_execution() -> None:
    async def scenario() -> None:
        worker = SerializedDiskWorker()
        entered = threading.Event()
        release = threading.Event()
        second_entered = threading.Event()
        active = {"current": 0, "max": 0}
        gate = threading.Lock()

        def first() -> None:
            with gate:
                active["current"] += 1
                active["max"] = max(active["max"], active["current"])
            entered.set()
            assert release.wait(5)
            with gate:
                active["current"] -= 1

        def second() -> None:
            with gate:
                active["current"] += 1
                active["max"] = max(active["max"], active["current"])
            second_entered.set()
            time.sleep(0.02)
            with gate:
                active["current"] -= 1

        try:
            first_task = asyncio.create_task(worker.call("test", first))
            for _ in range(100):
                if entered.is_set():
                    break
                await asyncio.sleep(0.01)
            assert entered.is_set()
            second_task = asyncio.create_task(worker.call("test", second))
            for _ in range(100):
                if worker._waiters == 1:
                    break
                await asyncio.sleep(0.01)
            assert worker._waiters == 1
            assert not second_entered.is_set()
            with pytest.raises(DiskWorkerSaturated):
                await worker.call("test", lambda: None)
            await asyncio.sleep(0.05)
            release.set()
            await first_task
            await second_task
        finally:
            release.set()
            await worker.aclose()
        assert active["max"] == 1
        metrics = worker.metrics.compact()
        assert metrics["observed_max_executing"] == 1
        assert metrics["backpressure_rejections"] == 1
        assert metrics["admissions_waited"] >= 1
        assert metrics["queue_wait_seconds_max"] >= 0.04

    asyncio.run(scenario())


def test_cancelled_caller_does_not_imply_the_write_finished(tmp_path: Path) -> None:
    path = tmp_path / "state.txt"
    started = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()
    order: list[str] = []

    def first(target: Path) -> None:
        started.set()
        assert release.wait(5)
        order.append("first")
        target.write_text("done", encoding="utf-8")

    def second(target: Path) -> None:
        order.append("second")
        second_entered.set()
        target.write_text("second", encoding="utf-8")

    async def runner(worker: SerializedDiskWorker) -> tuple[str, bool]:
        try:
            await worker.call("watch_state_persistence", first, path)
            return "success", False
        except asyncio.CancelledError:
            pending = not path.exists()
            await worker.settle_after_cancellation()
            return "cancelled", pending

    async def scenario() -> None:
        worker = SerializedDiskWorker()
        try:
            task = asyncio.create_task(runner(worker))
            for _ in range(100):
                if started.is_set():
                    break
                await asyncio.sleep(0.01)
            assert started.is_set()
            task.cancel()
            await asyncio.sleep(0.05)
            assert not path.exists()
            other = asyncio.create_task(worker.call("watch_state_persistence", second, path))
            await asyncio.sleep(0.05)
            assert not second_entered.is_set()
            release.set()
            status, pending = await task
            await other
        finally:
            release.set()
            await worker.aclose()
        assert status == "cancelled"
        assert pending is True
        assert order == ["first", "second"]
        metrics = worker.metrics.compact()
        assert metrics["caller_cancelled"] >= 1
        assert metrics["finished_after_cancel"] >= 1
        assert metrics["observed_max_executing"] == 1
        assert metrics["completed"] == 1
        assert worker.shutdown_report["inflight_at_shutdown_request"] == 0
        assert worker.shutdown_report["awaited_to_completion"] is True

    asyncio.run(scenario())


def test_restart_after_shutdown_can_write_immediately(tmp_path: Path) -> None:
    path = tmp_path / "state.txt"

    async def scenario() -> None:
        first = SerializedDiskWorker()
        await first.call("test", path.write_text, "one", encoding="utf-8")
        await first.aclose()
        assert not first._thread.is_alive()
        second = SerializedDiskWorker()
        try:
            await second.call("test", path.write_text, "two", encoding="utf-8")
        finally:
            await second.aclose()

    asyncio.run(scenario())
    assert path.read_text(encoding="utf-8") == "two"


def test_watch_state_atomicity_epoch_guard_and_temp_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "watch_state.json"
    original = WatchState(updated_at="2026-10-27T00:00:00+00:00", runtime_epoch_id="epoch-a")
    replacement = original.model_copy(update={"updated_at": "2026-10-27T00:00:01+00:00"})
    mismatched = original.model_copy(update={
        "updated_at": "2026-10-27T00:00:02+00:00",
        "runtime_epoch_id": "epoch-b",
    })

    async def scenario() -> None:
        worker = SerializedDiskWorker()
        try:
            same: list[int] = []

            def remember(target: Path, state: WatchState, expected_updated_at: str | None = None) -> None:
                del target, expected_updated_at
                same.append(id(state))

            await worker.call("watch_state_persistence", remember, path, original)
            assert same == [id(original)]
            await worker.call(
                "watch_state_persistence", save_watch_state, path, original,
            )
            assert load_watch_state(path).runtime_epoch_id == "epoch-a"
            with pytest.raises(WatchModeError, match="epoch"):
                await worker.call(
                    "watch_state_persistence",
                    save_watch_state,
                    path,
                    mismatched,
                    expected_updated_at=original.updated_at,
                    epoch_guard=lambda: run_scan._reject_mismatched_watch_epoch(mismatched, "epoch-a"),
                )
            assert load_watch_state(path).runtime_epoch_id == "epoch-a"
            real_write = Path.write_text

            def fail_temp(self: Path, *args: object, **kwargs: object) -> int:
                if self.name.startswith("."):
                    raise OSError("synthetic disk failure")
                return real_write(self, *args, **kwargs)

            monkeypatch.setattr(Path, "write_text", fail_temp)
            with pytest.raises(WatchModeError, match="could not write watch state"):
                await worker.call(
                    "watch_state_persistence",
                    save_watch_state,
                    path,
                    replacement,
                    expected_updated_at=original.updated_at,
                )
        finally:
            await worker.aclose()

    asyncio.run(scenario())
    assert load_watch_state(path).updated_at == original.updated_at
    assert list(tmp_path.glob(".watch_state.json.*.tmp")) == []


def test_manifest_append_order_has_one_receipt_per_call(tmp_path: Path) -> None:
    config = ScannerRunConfig.model_validate({
        "symbols": ["BTCUSDT"],
        "exchange": "binance",
        "account_equity": Decimal("10000"),
        "risk_per_trade_pct": Decimal("1"),
    })
    result = ScannerRunResult(
        config=config,
        results=(),
        scanned_symbols=0,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )
    watchlist = SimpleNamespace(
        source_label="synthetic",
        universe=SimpleNamespace(mode="manual", label="synthetic"),
    )
    manifest = tmp_path / "manifest.jsonl"
    nightly = tmp_path / "nightly.json"

    async def scenario() -> None:
        worker = SerializedDiskWorker()
        profile = PhaseProfile()
        try:
            with profile.activate():
                for run_id in ("run-a", "run-b"):
                    row = await run_scan._isolated_disk_call(
                        profile,
                        worker,
                        "scan_manifest",
                        run_scan._append_scan_run_manifest,
                        result,
                        watchlist=watchlist,
                        ranked_results=(),
                        manifest_path=manifest,
                        nightly_history_path=nightly,
                        run_id=run_id,
                    )
                    assert row["run_id"] == run_id
        finally:
            await worker.aclose()
        assert profile.snapshot(boundary="done")["phases"]["scan_manifest"]["calls"] == 2

    asyncio.run(scenario())
    lines = manifest.read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["run_id"] for line in lines] == ["run-a", "run-b"]
    assert len(lines) == len(set(lines))
    nightly_runs = json.loads(nightly.read_text(encoding="utf-8"))["runs"]
    assert [row["run_id"] for row in nightly_runs] == ["run-a", "run-b"]


def test_profiled_disk_function_is_not_measured_twice_from_the_worker() -> None:
    @profiled("watch_output")
    def wrapped(value: int) -> int:
        return value

    async def scenario() -> None:
        worker = SerializedDiskWorker()
        profile = PhaseProfile()
        try:
            with profile.activate():
                assert await run_scan._isolated_disk_call(
                    profile, worker, "watch_output", wrapped, 7,
                ) == 7
        finally:
            await worker.aclose()
        assert profile.snapshot(boundary="done")["phases"]["watch_output"]["calls"] == 1

    asyncio.run(scenario())


def test_worker_does_not_copy_the_caller_payload() -> None:
    payload = bytearray(1_000_000)

    async def scenario() -> int:
        worker = SerializedDiskWorker()
        tracemalloc.start()
        try:
            before = tracemalloc.get_traced_memory()[1]
            assert await worker.call("test", lambda buf: len(buf), payload) == 1_000_000
            return tracemalloc.get_traced_memory()[1] - before
        finally:
            tracemalloc.stop()
            await worker.aclose()

    growth = asyncio.run(scenario())
    assert growth < 400_000


def test_loopback_frame_progresses_during_offloaded_disk_work(tmp_path: Path) -> None:
    path = tmp_path / "offloaded.txt"
    started = threading.Event()
    processed = threading.Event()
    order: list[str] = []

    def provider(connection) -> None:
        assert started.wait(5)
        connection.send("synthetic fixture frame")
        order.append("wire_frame_sent")
        assert connection.recv(timeout=5) == "fixture complete"

    def offloaded(target: Path) -> None:
        order.append("worker_entered")
        started.set()
        assert processed.wait(5)
        order.append("worker_returned")
        target.write_text("durable", encoding="utf-8")

    async def scenario(port: int) -> None:
        connection = await BinanceWebSocketTransport(url=f"ws://127.0.0.1:{port}").connect()
        worker = SerializedDiskWorker()

        async def receive() -> None:
            assert await connection.recv() == "synthetic fixture frame"
            order.append("frame_processed")
            processed.set()

        receiver = asyncio.create_task(receive())
        try:
            await asyncio.sleep(0)
            await worker.call("watch_state_persistence", offloaded, path)
            await asyncio.wait_for(receiver, 5)
            await connection.send("fixture complete")
        finally:
            receiver.cancel()
            await asyncio.gather(receiver, return_exceptions=True)
            await connection.close()
            await worker.aclose()

    with serve(provider, "127.0.0.1", 0, close_timeout=1) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            asyncio.run(scenario(server.socket.getsockname()[1]))
        finally:
            server.shutdown()
            thread.join(timeout=5)
        assert not thread.is_alive()
    assert order.index("frame_processed") < order.index("worker_returned")
    assert path.read_text(encoding="utf-8") == "durable"


def test_receive_health_counts_frames_and_heartbeat_timeouts_without_payload_text() -> None:
    recovery = FeedRecovery(
        "OrderBook", 0.01, 0.02, jitter=lambda _bound: 0.0, monotonic=lambda: 10.0,
    )
    recovery.note_receive()
    health = recovery.health()
    assert health["receive_count"] == 1
    assert health["last_receive_age_seconds"] == 0.0
    assert health["heartbeat_timeout_count"] == 0
    recovery.failed(ConnectionClosed(None, Close(1011, "keepalive ping timeout")), "receive")
    failed = recovery.health()
    assert failed["heartbeat_timeout_count"] == 1
    assert failed["last_failure"]["category"] == "heartbeat_timeout"
    assert "keepalive" not in json.dumps(failed)


def test_cmc_deadline_remains_30_and_timeout_does_not_join_sidecar_work(monkeypatch: pytest.MonkeyPatch) -> None:
    assert COLLECTION_DEADLINE_SECONDS == 30
    monkeypatch.setattr("app.cmc.runtime.COLLECTION_DEADLINE_SECONDS", 0.5)
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    class Collector:
        def __init__(self) -> None:
            self.client = _Client()

        async def collect(self) -> str:
            await asyncio.to_thread(self._block)
            return "replaced"

        def _block(self) -> None:
            started.set()
            release.wait(5)
            finished.set()

    service = Service(Collector(), store=None)
    previous = service.snapshot

    async def scenario() -> None:
        # Observe the sidecar before the loop closes. asyncio.run joins the
        # default executor on shutdown, which would hide a still-running write.
        await service._refresh()
        assert started.is_set()
        assert service.snapshot is previous
        acquisition = service.last_acquisition
        assert acquisition["outcome"] == "TimeoutError"
        assert acquisition["deadline_seconds"] == 0.5
        assert acquisition["duration_seconds"] < 2.0
        assert acquisition["deadline_extended"] is False
        assert finished.is_set() is False
        release.set()
        assert finished.wait(2)

    try:
        asyncio.run(scenario())
    finally:
        release.set()


def test_cmc_completed_acquisition_records_duration_without_extending_deadline() -> None:
    class Collector:
        def __init__(self) -> None:
            self.client = _Client()

        async def collect(self) -> str:
            await asyncio.sleep(0.01)
            return "snap"

    service = Service(Collector(), store=None)
    asyncio.run(service._refresh())
    assert service.snapshot == "snap"
    assert service.last_acquisition["outcome"] == "completed"
    assert service.last_acquisition["deadline_seconds"] == 30
    assert service.last_acquisition["deadline_extended"] is False
    assert service.last_acquisition["duration_seconds"] >= 0
