from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import threading
import time
from typing import Any

import pytest
import websockets

from app.analytics.market_regime import default_market_regime_result
from app.core.phase_profile import PhaseProfile
from app.data.dtos import NA
from app.lifecycle.eligibility import has_valid_trade_map, public_watchlist_eligible
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerRuntimeStats,
    ScannerSymbolResult,
)
from app.runtime.lifecycle_isolation import (
    LifecycleWorker,
    LifecycleWorkerClosed,
    LifecycleWorkerSaturated,
)
from app.runtime.loop_lag import LoopLagMonitor
from app.runtime_epoch.authority import initialize_runtime_epoch
from app.runtime_epoch.errors import RuntimeEpochError
from app.runtime_epoch.models import (
    RUNTIME_EPOCH_CONTRACT_VERSION,
    RuntimeEpochIdentity,
)
from app.runtime_epoch.origin import register_operational_scan_run
from app.storage.database import (
    StorageError,
    open_initialized_database,
)
from app.universe.symbol_universe import manual_symbol_universe
from app.watch_mode import WatchIterationSummary, build_watch_iteration_summary
from app.watch_session import WatchSession
from app.watch_supervisor import WatchFailureDisposition, classify_watch_exception
import scripts.run_scan as run_scan
from tests.runtime_epoch_support import (
    SYNTHETIC_IDENTITY,
    SYNTHETIC_NOW,
    bootstrap_operational_test_database,
)
from tests.test_p5a_monitoring_binding_query_repair import _quiet_runtime
from tests.test_watch_mode import (
    _patch_watch_runtime,
    _rejected_symbol,
    _scanner_config,
    _watch_args,
)


def _epoch_identity(epoch_id: str | None = None) -> RuntimeEpochIdentity:
    if epoch_id is None:
        return SYNTHETIC_IDENTITY
    return RuntimeEpochIdentity(
        epoch_id=epoch_id,
        cutoff_at=SYNTHETIC_IDENTITY.cutoff_at,
        contract_version=RUNTIME_EPOCH_CONTRACT_VERSION,
        reviewed_release_sha=SYNTHETIC_IDENTITY.reviewed_release_sha,
        generation_binding=SYNTHETIC_IDENTITY.generation_binding,
    )


def _init_operational_db(
    db_path: Path, identity: RuntimeEpochIdentity | None = None
) -> RuntimeEpochIdentity:
    bootstrap_operational_test_database(db_path)
    return identity or SYNTHETIC_IDENTITY


def _make_watch_execution(
    db_path: Path,
    *,
    run_id: str = "run-p0b3-parity-001",
    identity: RuntimeEpochIdentity | None = None,
    symbols: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
) -> tuple[Any, run_scan.WatchScanExecution, WatchIterationSummary]:
    ident = _init_operational_db(db_path, identity)
    register_operational_scan_run(
        db_path,
        run_id=run_id,
        registered_at=SYNTHETIC_NOW,
        expected_identity=ident,
    )
    args = run_scan.parse_args(
        [
            "--symbols",
            *symbols,
            "--store-scan",
            "--database-path",
            str(db_path),
            "--disable-lifecycle",
            "--no-telegram-signals",
        ]
    )
    args.runtime_identity = ident
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    sym_results = tuple(_rejected_symbol(sym) for sym in symbols)
    config = _scanner_config(list(symbols))
    result = ScannerRunResult(
        config=config,
        results=sym_results,
        scanned_symbols=len(sym_results),
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
        resume_metadata={
            "run_id": run_id,
            "scan_run_id": run_id,
            "watch_mode": True,
            "watch_iteration": 1,
            "pending_symbols": [],
        },
        runtime_stats=ScannerRuntimeStats(
            total_runtime_seconds=1.25,
            average_seconds_per_symbol=0.625,
            slowest_symbol=symbols[0],
            slowest_symbol_seconds=0.75,
            timeout_count=0,
            completed_symbols=len(symbols),
            skipped_symbols=0,
            errored_symbols=0,
            global_timeout_hit=False,
        ),
        market_regime=default_market_regime_result(),
        symbol_health={
            sym: {
                "symbol": sym,
                "state": "healthy",
                "consecutive_timeout_count": 0,
                "last_status": "scanned_no_setup",
            }
            for sym in symbols
        },
    )
    ranked_results = run_scan.rank_scan_results(result.results, rank_results=True)
    execution = run_scan.WatchScanExecution(
        result=result,
        ranked_results=ranked_results,
        portfolio_selection=None,
        symbol_priority_plan=run_scan.empty_symbol_priority_plan(symbols),
        queued_symbols=symbols,
        storage_run_id=run_id,
        phase_statuses={"universe": "SUCCESS", "queue": "SUCCESS", "scanner": "SUCCESS"},
        recoverable_errors=(),
        telegram_outbox_status={},
        telegram_delivery_summary=None,
    )
    summary = build_watch_iteration_summary(
        iteration=1,
        result=result,
        activations=[],
        next_scan_seconds=60.0,
        scanned_at=now.isoformat(),
    ).model_copy(
        update={
            "iteration_id": run_id,
            "status": "SUCCESS",
            "database_storage_status": "PENDING",
            "duration_scope": "through_watch_state_before_primary_scan_storage",
        }
    )
    return args, execution, summary


def test_scan_storage_worker_keeps_event_loop_and_websocket_responsive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Slow worker-owned scan_storage must not block the event loop or WebSocket heartbeats."""
    db_path = tmp_path / "responsive.db"
    args, execution, summary = _make_watch_execution(db_path, run_id="run-responsive-001")

    real_store = run_scan.store_scan_result
    worker_threads: list[str] = []

    def _slow_worker_store(*s_args: Any, **s_kwargs: Any) -> str:
        worker_threads.append(threading.current_thread().name)
        time.sleep(0.18)
        return real_store(*s_args, **s_kwargs)

    monkeypatch.setattr(run_scan, "store_scan_result", _slow_worker_store)

    received_ws_frames: list[str] = []

    async def _ws_handler(ws: Any) -> None:
        for idx in range(8):
            await ws.send(f"tick-{idx}")
            await asyncio.sleep(0.025)

    heartbeat_gaps: list[float] = []
    stop_heartbeat = asyncio.Event()

    async def _heartbeat() -> None:
        last = time.perf_counter()
        while not stop_heartbeat.is_set():
            await asyncio.sleep(0.01)
            now = time.perf_counter()
            heartbeat_gaps.append(now - last)
            last = now

    async def _scenario() -> dict[str, Any]:
        worker = LifecycleWorker()
        loop_lag = LoopLagMonitor(interval=0.02)
        profile = run_scan.WatchPhaseProfile()
        profile.loop_lag = loop_lag
        profile.lifecycle_worker = worker
        loop_lag.bind(profile)

        await loop_lag.start()
        hb_task = asyncio.create_task(_heartbeat())
        try:
            async with websockets.serve(_ws_handler, "127.0.0.1", 0) as server:
                port = server.sockets[0].getsockname()[1]

                async def _ws_client() -> None:
                    async with websockets.connect(f"ws://127.0.0.1:{port}") as client:
                        async for message in client:
                            received_ws_frames.append(str(message))
                            if len(received_ws_frames) >= 8:
                                break

                ws_task = asyncio.create_task(_ws_client())
                snap_before = profile.snapshot(boundary="before_primary_scan_storage")
                assert snap_before["phases"]["scan_storage"]["measurement_status"] == "NOT_OBSERVED"
                assert snap_before["phases"]["scan_storage"]["calls"] == 0

                stored_run_id = await profile.acall(
                    "scan_storage",
                    run_scan._store_watch_iteration_scan_run_isolated,
                    args,
                    worker=worker,
                    execution=execution,
                    summary=summary,
                    started_at="2026-04-20T12:00:00+00:00",
                    completed_at="2026-04-20T12:00:02+00:00",
                    runtime_sec=1.25,
                    command_used="python scripts/run_scan.py --watch --store-scan",
                )
                await ws_task
                assert stored_run_id == "run-responsive-001"
        finally:
            stop_heartbeat.set()
            await hb_task
            await loop_lag.stop()
            await worker.aclose()
        return profile.snapshot(boundary="before_manifest_reporting_and_presentation")

    snap = asyncio.run(_scenario())
    assert worker_threads == ["cci-p0b1-lifecycle"]
    assert len(received_ws_frames) == 8
    assert max(heartbeat_gaps) < 0.09

    assert snap["phases"]["scan_storage"]["calls"] == 1
    assert snap["phases"]["scan_storage"]["measurement_status"] == "OBSERVED"
    assert "scan_storage" not in snap["event_loop"]["phase_induced_block_seconds_max"]

    iso = snap["scan_storage_isolation"]
    assert iso["SCAN_STORAGE_WORKER_DURATION"] >= 0.16
    assert iso["SCAN_STORAGE_TRANSACTION_RESULT"] == "COMMITTED"
    assert iso["scan_storage_connection_closed"] is True
    assert iso["scan_storage_completed"] == 1
    assert iso["scan_storage_failed"] == 0
    if iso["LOOP_LAG_DURING_SCAN_STORAGE"] != NA:
        assert iso["LOOP_LAG_DURING_SCAN_STORAGE"] < 0.09


def test_worker_thread_ownership_and_connection_argument_rejection(tmp_path: Path) -> None:
    """No sqlite3.Connection, Cursor, or repository may cross threads into or out of LifecycleWorker."""
    db_path = tmp_path / "ownership.db"
    _init_operational_db(db_path)

    async def _scenario() -> None:
        worker = LifecycleWorker()
        try:
            conn = sqlite3.connect(db_path)
            try:
                with pytest.raises(TypeError, match="worker-owned"):
                    await worker.call(lambda c: "bad", conn, stage="scan_storage")

                with pytest.raises(TypeError, match="worker-owned"):
                    await worker.call(lambda payload: "bad", {"nested": [conn]}, stage="scan_storage")

                cursor = conn.cursor()
                with pytest.raises(TypeError, match="worker-owned"):
                    await worker.call(lambda cur: "bad", cursor, stage="scan_storage")
            finally:
                conn.close()

            repo = SQLiteSetupLifecycleRepository(db_path)
            with pytest.raises(TypeError, match="worker-owned"):
                await worker.call(lambda r: "bad", repo, stage="scan_storage")

            with pytest.raises(TypeError, match="cannot return an open connection"):
                await worker.call(lambda: sqlite3.connect(db_path), stage="scan_storage")

            with pytest.raises(TypeError, match="detached scan_run_id str"):
                await worker.call(lambda: 12345, stage="scan_storage")
        finally:
            await worker.aclose()

    asyncio.run(_scenario())


def test_scan_storage_sync_and_isolated_parity_without_duplicate_writes(tmp_path: Path) -> None:
    """Worker-isolated scan_storage persists exact row/payload parity with the synchronous path."""
    sync_db = tmp_path / "sync.db"
    iso_db = tmp_path / "isolated.db"
    args_sync, exec_sync, sum_sync = _make_watch_execution(sync_db, run_id="run-parity-777")
    args_iso, exec_iso, sum_iso = _make_watch_execution(iso_db, run_id="run-parity-777")

    async def _scenario() -> tuple[str, str]:
        sync_run_id = await run_scan._store_watch_iteration_scan_run_isolated(
            args_sync,
            worker=None,
            execution=exec_sync,
            summary=sum_sync,
            started_at="2026-04-20T12:00:00+00:00",
            completed_at="2026-04-20T12:00:02+00:00",
            runtime_sec=1.25,
            command_used="python scripts/run_scan.py --watch --store-scan",
        )

        worker = LifecycleWorker()
        try:
            iso_run_id = await run_scan._store_watch_iteration_scan_run_isolated(
                args_iso,
                worker=worker,
                execution=exec_iso,
                summary=sum_iso,
                started_at="2026-04-20T12:00:00+00:00",
                completed_at="2026-04-20T12:00:02+00:00",
                runtime_sec=1.25,
                command_used="python scripts/run_scan.py --watch --store-scan",
            )
        finally:
            await worker.aclose()
        return sync_run_id, iso_run_id

    sync_run_id, iso_run_id = asyncio.run(_scenario())
    assert sync_run_id == "run-parity-777"
    assert iso_run_id == "run-parity-777"

    def _read_db_state(path: Path) -> dict[str, Any]:
        with sqlite3.connect(path) as conn:
            conn.row_factory = sqlite3.Row
            scan_runs = [dict(r) for r in conn.execute("SELECT * FROM scan_runs").fetchall()]
            symbol_results = [
                dict(r)
                for r in conn.execute("SELECT * FROM symbol_results ORDER BY symbol").fetchall()
            ]
            symbol_health = [
                dict(r)
                for r in conn.execute("SELECT * FROM symbol_health ORDER BY symbol").fetchall()
            ]
            return {
                "scan_runs": scan_runs,
                "symbol_results": symbol_results,
                "symbol_health": symbol_health,
            }

    sync_state = _read_db_state(sync_db)
    iso_state = _read_db_state(iso_db)

    assert len(sync_state["scan_runs"]) == 1
    assert len(iso_state["scan_runs"]) == 1
    assert sync_state["scan_runs"][0]["run_id"] == "run-parity-777"
    assert iso_state["scan_runs"][0]["run_id"] == "run-parity-777"
    assert json.loads(sync_state["scan_runs"][0]["raw_payload_json"]) == json.loads(
        iso_state["scan_runs"][0]["raw_payload_json"]
    )
    assert len(sync_state["symbol_results"]) == 2
    assert len(iso_state["symbol_results"]) == 2
    for row_a, row_b in zip(sync_state["symbol_results"], iso_state["symbol_results"], strict=True):
        row_a.pop("id", None)
        row_b.pop("id", None)
        assert row_a == row_b


def test_epoch_mismatch_fails_closed(tmp_path: Path) -> None:
    """Epoch mismatch inside worker-owned scan_storage must fail closed and never be swallowed."""
    db_path = tmp_path / "epoch_mismatch.db"
    args, execution, summary = _make_watch_execution(
        db_path,
        run_id="run-epoch-mismatch",
    )
    args.runtime_identity = _epoch_identity("epoch-stale-writer")

    async def _scenario() -> None:
        worker = LifecycleWorker()
        try:
            with pytest.raises((RuntimeEpochError, SystemExit)) as exc_info:
                await run_scan._store_watch_iteration_scan_run_isolated(
                    args,
                    worker=worker,
                    execution=execution,
                    summary=summary,
                    started_at="2026-04-20T12:00:00+00:00",
                    completed_at="2026-04-20T12:00:02+00:00",
                    runtime_sec=1.25,
                    command_used="python scripts/run_scan.py --watch --store-scan",
                )
            exc = exc_info.value
            assert classify_watch_exception(exc) == WatchFailureDisposition.FATAL

            snap = worker.scan_storage_snapshot()
            assert snap["SCAN_STORAGE_TRANSACTION_RESULT"] == "NOT_OBSERVED"
            assert snap["scan_storage_connection_closed"] is None
            assert snap["scan_storage_failed"] == 1
            assert snap["scan_storage_completed"] == 0
        finally:
            await worker.aclose()

    asyncio.run(_scenario())

    with sqlite3.connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0]
        assert count == 0


def test_no_global_function_replacement_during_scan_storage(tmp_path: Path) -> None:
    """Scan storage isolation must not monkey-patch global database opening functions."""
    import app.runtime_epoch.startup as startup_mod
    import app.storage.repositories as repositories_mod

    orig_startup_open = startup_mod.open_operational_database
    orig_repo_open = repositories_mod.open_initialized_database

    db_path = tmp_path / "no_patch.db"
    args, execution, summary = _make_watch_execution(db_path, run_id="run-no-patch-001")

    async def _scenario() -> None:
        worker = LifecycleWorker()
        try:
            stored_id = await run_scan._store_watch_iteration_scan_run_isolated(
                args,
                worker=worker,
                execution=execution,
                summary=summary,
                started_at="2026-04-20T12:00:00+00:00",
                completed_at="2026-04-20T12:00:02+00:00",
                runtime_sec=1.25,
                command_used="python scripts/run_scan.py --watch --store-scan",
            )
            assert stored_id == "run-no-patch-001"
            assert startup_mod.open_operational_database is orig_startup_open
            assert repositories_mod.open_initialized_database is orig_repo_open
            snap = worker.scan_storage_snapshot()
            assert snap["SCAN_STORAGE_TRANSACTION_RESULT"] == "COMMITTED"
            assert snap["scan_storage_connection_closed"] is True
            assert snap["scan_storage_completed"] == 1
        finally:
            await worker.aclose()

    asyncio.run(_scenario())
    assert startup_mod.open_operational_database is orig_startup_open
    assert repositories_mod.open_initialized_database is orig_repo_open


def test_cross_thread_database_access_during_scan_storage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent threads must be able to access the database without global locks or function patches."""
    import app.storage.repositories as repositories_mod

    db_path = tmp_path / "concurrent_access.db"
    args, execution, summary = _make_watch_execution(db_path, run_id="run-concurrent-001")

    concurrent_read_saw_table = threading.Event()
    worker_can_proceed = threading.Event()
    real_insert_scan_run = repositories_mod._insert_scan_run

    def _pausing_insert(conn: sqlite3.Connection, scan_record: Any) -> None:
        real_insert_scan_run(conn, scan_record)
        concurrent_read_saw_table.set()
        assert worker_can_proceed.wait(timeout=5.0)

    monkeypatch.setattr(repositories_mod, "_insert_scan_run", _pausing_insert)

    concurrent_thread_result: list[bool] = []

    def _concurrent_reader() -> None:
        if not concurrent_read_saw_table.wait(timeout=5.0):
            return
        try:
            with sqlite3.connect(db_path, timeout=5.0) as reader_conn:
                row = reader_conn.execute("SELECT COUNT(*) FROM runtime_epochs").fetchone()
                concurrent_thread_result.append(row is not None and row[0] >= 1)
        finally:
            worker_can_proceed.set()

    reader_thread = threading.Thread(target=_concurrent_reader, name="test-concurrent-reader")
    reader_thread.start()

    async def _scenario() -> None:
        worker = LifecycleWorker()
        try:
            stored_id = await run_scan._store_watch_iteration_scan_run_isolated(
                args,
                worker=worker,
                execution=execution,
                summary=summary,
                started_at="2026-04-20T12:00:00+00:00",
                completed_at="2026-04-20T12:00:02+00:00",
                runtime_sec=1.25,
                command_used="python scripts/run_scan.py --watch --store-scan",
            )
            assert stored_id == "run-concurrent-001"
            snap = worker.scan_storage_snapshot()
            assert snap["SCAN_STORAGE_TRANSACTION_RESULT"] == "COMMITTED"
            assert snap["scan_storage_connection_closed"] is True
            assert snap["scan_storage_completed"] == 1
        finally:
            await worker.aclose()

    asyncio.run(_scenario())
    reader_thread.join(timeout=5.0)
    assert concurrent_thread_result == [True]


def test_transaction_outcomes_and_closure_truthfulness(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Receipts truthfully reflect actual DB outcomes without inventing COMMITTED or connection_closed."""
    import app.storage.repositories as repositories_mod

    db_path = tmp_path / "truthful_receipts.db"
    args, execution, summary = _make_watch_execution(db_path, run_id="run-truth-001")

    # 1. Failure before transaction begins -> NO_TRANSACTION_OR_UNKNOWN, connection_closed=True
    def _failing_insert(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("synthetic insert failure")

    monkeypatch.setattr(repositories_mod, "_insert_scan_run", _failing_insert)

    async def _scenario_pre_transaction_failure() -> None:
        worker = LifecycleWorker()
        try:
            with pytest.raises(SystemExit) as exc_info:
                await run_scan._store_watch_iteration_scan_run_isolated(
                    args,
                    worker=worker,
                    execution=execution,
                    summary=summary,
                    started_at="2026-04-20T12:00:00+00:00",
                    completed_at="2026-04-20T12:00:02+00:00",
                    runtime_sec=1.25,
                    command_used="python scripts/run_scan.py --watch --store-scan",
                )
            assert isinstance(exc_info.value.__cause__, StorageError)
            snap = worker.scan_storage_snapshot()
            assert snap["SCAN_STORAGE_TRANSACTION_RESULT"] == "NO_TRANSACTION_OR_UNKNOWN"
            assert snap["scan_storage_connection_closed"] is True
            assert snap["scan_storage_failed"] == 1
        finally:
            await worker.aclose()

    asyncio.run(_scenario_pre_transaction_failure())
    monkeypatch.undo()

    # 2. Failure after transaction begins -> ROLLED_BACK, connection_closed=True
    def _failing_symbols(*_args: Any, **_kwargs: Any) -> None:
        raise sqlite3.OperationalError("synthetic symbol insert failure during transaction")

    monkeypatch.setattr(repositories_mod, "_insert_symbol_results", _failing_symbols)

    async def _scenario_in_transaction_failure() -> None:
        worker = LifecycleWorker()
        try:
            with pytest.raises(SystemExit) as exc_info:
                await run_scan._store_watch_iteration_scan_run_isolated(
                    args,
                    worker=worker,
                    execution=execution,
                    summary=summary,
                    started_at="2026-04-20T12:00:00+00:00",
                    completed_at="2026-04-20T12:00:02+00:00",
                    runtime_sec=1.25,
                    command_used="python scripts/run_scan.py --watch --store-scan",
                )
            assert isinstance(exc_info.value.__cause__, StorageError)
            snap = worker.scan_storage_snapshot()
            assert snap["SCAN_STORAGE_TRANSACTION_RESULT"] == "ROLLED_BACK"
            assert snap["scan_storage_connection_closed"] is True
            assert snap["scan_storage_failed"] == 1
        finally:
            await worker.aclose()

    asyncio.run(_scenario_in_transaction_failure())
    monkeypatch.undo()

    # 3. Commit failure -> COMMIT_FAILED_ROLLED_BACK, connection_closed=True
    import app.runtime_epoch.startup as startup_mod
    real_open = startup_mod.open_operational_database

    class _FailingCommitConn:
        def __init__(self, actual: sqlite3.Connection) -> None:
            self._actual = actual

        def commit(self) -> None:
            raise sqlite3.OperationalError("synthetic commit failure")

        def __getattr__(self, name: str) -> Any:
            return getattr(self._actual, name)

    def _open_failing_commit(*o_args: Any, **o_kwargs: Any) -> Any:
        conn, epoch = real_open(*o_args, **o_kwargs)
        return _FailingCommitConn(conn), epoch

    monkeypatch.setattr(startup_mod, "open_operational_database", _open_failing_commit)

    async def _scenario_commit_failure() -> None:
        worker = LifecycleWorker()
        try:
            with pytest.raises(SystemExit) as exc_info:
                await run_scan._store_watch_iteration_scan_run_isolated(
                    args,
                    worker=worker,
                    execution=execution,
                    summary=summary,
                    started_at="2026-04-20T12:00:00+00:00",
                    completed_at="2026-04-20T12:00:02+00:00",
                    runtime_sec=1.25,
                    command_used="python scripts/run_scan.py --watch --store-scan",
                )
            assert isinstance(exc_info.value.__cause__, StorageError)
            snap = worker.scan_storage_snapshot()
            assert snap["SCAN_STORAGE_TRANSACTION_RESULT"] == "COMMIT_FAILED_ROLLED_BACK"
            assert snap["scan_storage_connection_closed"] is True
            assert snap["scan_storage_failed"] == 1
        finally:
            await worker.aclose()

    asyncio.run(_scenario_commit_failure())
    monkeypatch.undo()


def test_watch_phase_profile_explicit_composition() -> None:
    """WatchPhaseProfile composes scan_storage telemetry without mutating PhaseProfile.snapshot."""
    worker = LifecycleWorker()
    try:
        profile = run_scan.WatchPhaseProfile()
        assert isinstance(profile, PhaseProfile)
        assert "snapshot" not in profile.__dict__
        profile.lifecycle_worker = worker
        snap = profile.snapshot(boundary="test_composition")
        assert "scan_storage_isolation" in snap
        assert snap["scan_storage_isolation"]["contract"] == "cci-scan-storage-isolation-v1"
    finally:
        asyncio.run(worker.aclose())


def test_cancellation_mid_commit_completes_and_closes_before_propagating(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Admitted scan_storage writes complete commit/rollback and close before cancellation propagates."""
    db_path = tmp_path / "cancel_commit.db"
    args, execution, summary = _make_watch_execution(db_path, run_id="run-cancel-commit-001")

    entered_store = threading.Event()
    release_store = threading.Event()
    real_store = run_scan.store_scan_result

    def _fenced_store(*s_args: Any, **s_kwargs: Any) -> str:
        entered_store.set()
        release_store.wait(timeout=5.0)
        return real_store(*s_args, **s_kwargs)

    monkeypatch.setattr(run_scan, "store_scan_result", _fenced_store)

    async def _scenario() -> None:
        worker = LifecycleWorker()
        try:
            call_task = asyncio.create_task(
                run_scan._store_watch_iteration_scan_run_isolated(
                    args,
                    worker=worker,
                    execution=execution,
                    summary=summary,
                    started_at="2026-04-20T12:00:00+00:00",
                    completed_at="2026-04-20T12:00:02+00:00",
                    runtime_sec=1.25,
                    command_used="python scripts/run_scan.py --watch --store-scan",
                )
            )
            while not entered_store.is_set():
                await asyncio.sleep(0.005)

            call_task.cancel()
            await asyncio.sleep(0.01)
            assert not call_task.done()

            release_store.set()
            with pytest.raises(asyncio.CancelledError):
                await call_task

            snap = worker.scan_storage_snapshot()
            assert snap["SCAN_STORAGE_TRANSACTION_RESULT"] == "COMMITTED"
            assert snap["scan_storage_connection_closed"] is True
            assert snap["scan_storage_completed"] == 1
            assert snap["scan_storage_cancelled_after_admission"] == 1
        finally:
            await worker.aclose()

    asyncio.run(_scenario())

    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT run_id FROM scan_runs").fetchall()
        assert [r[0] for r in rows] == ["run-cancel-commit-001"]


def test_competing_owner_and_worker_saturation_fail_closed(tmp_path: Path) -> None:
    """Competing SQLite writer and third queued caller behave deterministically and fail closed on saturation."""
    db_path = tmp_path / "competing.db"
    args, execution, summary = _make_watch_execution(db_path, run_id="run-competing-001")

    async def _scenario() -> None:
        locked_event = threading.Event()

        def _competing_writer() -> None:
            comp_conn = sqlite3.connect(db_path, timeout=5.0)
            try:
                comp_conn.execute("BEGIN IMMEDIATE")
                locked_event.set()
                time.sleep(0.08)
                comp_conn.rollback()
            finally:
                comp_conn.close()

        releaser = threading.Thread(target=_competing_writer, daemon=True)
        releaser.start()
        assert locked_event.wait(timeout=2.0)

        worker = LifecycleWorker()
        try:
            run_id = await run_scan._store_watch_iteration_scan_run_isolated(
                args,
                worker=worker,
                execution=execution,
                summary=summary,
                started_at="2026-04-20T12:00:00+00:00",
                completed_at="2026-04-20T12:00:02+00:00",
                runtime_sec=1.25,
                command_used="python scripts/run_scan.py --watch --store-scan",
            )
            assert run_id == "run-competing-001"
        finally:
            releaser.join(timeout=2.0)
            await worker.aclose()

        worker2 = LifecycleWorker()
        gate = threading.Event()
        entered = threading.Event()
        try:
            def _block() -> str:
                entered.set()
                gate.wait(timeout=5.0)
                return "run-1"

            t1 = asyncio.create_task(worker2.call(_block, stage="scan_storage"))
            while not entered.is_set():
                await asyncio.sleep(0.005)
            t2 = asyncio.create_task(worker2.call(lambda: "run-2", stage="scan_storage"))
            await asyncio.sleep(0.01)

            with pytest.raises(LifecycleWorkerSaturated):
                await worker2.call(lambda: "run-3", stage="scan_storage")

            gate.set()
            assert await t1 == "run-1"
            assert await t2 == "run-2"
            snap = worker2.scan_storage_snapshot()
            assert snap["scan_storage_backpressure_rejections"] == 1
        finally:
            gate.set()
            await worker2.aclose()

    asyncio.run(_scenario())


def test_telemetry_isolation_and_no_late_writes_after_shutdown_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """scan_storage telemetry never overwrites lifecycle/continuation metrics, and aclose fences lease release."""
    db_path = tmp_path / "telemetry_fence.db"
    args, execution, summary = _make_watch_execution(db_path, run_id="run-telemetry-001")

    async def _scenario() -> None:
        worker = LifecycleWorker()
        try:
            def _lifecycle_call() -> str:
                from app.lifecycle.transaction_receipt import note_repository_completion
                note_repository_completion(
                    committed=True, commit_failed=False, rolled_back=False, closed=True
                )
                return "lifecycle-ok"

            def _cont_write_call() -> str:
                from app.lifecycle.transaction_receipt import note_repository_completion
                note_repository_completion(
                    committed=False, commit_failed=False, rolled_back=True, closed=True
                )
                return "cont-rolled-back"

            await worker.call(_lifecycle_call)
            await worker.call(lambda: "cont-read-ok", stage="continuation_read")
            await worker.call(_cont_write_call, stage="continuation_write")

            with pytest.raises((RuntimeEpochError, SystemExit)):
                bad_args = run_scan.parse_args(["--store-scan", "--database-path", str(db_path)])
                bad_args.runtime_identity = _epoch_identity("wrong-epoch")
                await run_scan._store_watch_iteration_scan_run_isolated(
                    bad_args,
                    worker=worker,
                    execution=execution,
                    summary=summary,
                    started_at="2026-04-20T12:00:00+00:00",
                    completed_at="2026-04-20T12:00:02+00:00",
                    runtime_sec=1.25,
                    command_used="python scripts/run_scan.py --watch --store-scan",
                )

            compact = worker.compact_snapshot()
            assert compact["COMMIT_OR_ROLLBACK_RESULT"] == "COMMITTED"
            assert compact["CONTINUATION_TRANSACTION_RESULT"] == "ROLLED_BACK"
            assert compact["completed"] == 3
            assert compact["failed"] == 0

            assert compact["SCAN_STORAGE_TRANSACTION_RESULT"] == "NOT_OBSERVED"
            assert compact["scan_storage_completed"] == 0
            assert compact["scan_storage_failed"] == 1

            stored_id = await run_scan._store_watch_iteration_scan_run_isolated(
                args,
                worker=worker,
                execution=execution,
                summary=summary,
                started_at="2026-04-20T12:00:00+00:00",
                completed_at="2026-04-20T12:00:02+00:00",
                runtime_sec=1.25,
                command_used="python scripts/run_scan.py --watch --store-scan",
            )
            assert stored_id == "run-telemetry-001"
            compact_after = worker.compact_snapshot()
            assert compact_after["SCAN_STORAGE_TRANSACTION_RESULT"] == "COMMITTED"
            assert compact_after["scan_storage_completed"] == 1
            assert compact_after["scan_storage_failed"] == 1
            assert compact_after["CONTINUATION_TRANSACTION_RESULT"] == "ROLLED_BACK"
            assert compact_after["completed"] == 3
            assert compact_after["failed"] == 0
        finally:
            await worker.aclose()

        with pytest.raises(LifecycleWorkerClosed):
            await worker.call(lambda: "late-write", stage="scan_storage")

    asyncio.run(_scenario())


def test_watch_mode_end_to_end_scan_storage_worker_and_lease_fence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Full watch-mode iteration dispatches scan_storage onto cci-p0b1-lifecycle and fences lease release."""
    _quiet_runtime(monkeypatch, tmp_path)
    path = _patch_watch_runtime(tmp_path, monkeypatch, symbols=("BTCUSDT",))

    store_threads: list[str] = []
    order: list[str] = []
    sessions: list[WatchSession] = []
    real_store = run_scan.store_scan_result
    original_enter = WatchSession.__enter__
    original_release = WatchSession._release

    def _tracked_store(*s_args: Any, **s_kwargs: Any) -> str:
        store_threads.append(threading.current_thread().name)
        order.append("scan_storage_enter")
        res = real_store(*s_args, **s_kwargs)
        order.append("scan_storage_exit")
        return res

    def _session_enter(self: WatchSession) -> WatchSession:
        res = original_enter(self)
        sessions.append(self)
        return res

    def _session_release(self: WatchSession) -> None:
        order.append("lease_released")
        original_release(self)

    monkeypatch.setattr(run_scan, "store_scan_result", _tracked_store)
    monkeypatch.setattr(WatchSession, "__enter__", _session_enter)
    monkeypatch.setattr(WatchSession, "_release", _session_release)

    asyncio.run(run_scan.main(_watch_args(path, universe_size=1)))

    assert store_threads == ["cci-p0b1-lifecycle"]
    assert order == ["scan_storage_enter", "scan_storage_exit", "lease_released"]
    assert len(sessions) == 1
    scan_iso_shutdown = sessions[0].state["scan_storage_isolation_shutdown"]
    assert scan_iso_shutdown["SCAN_STORAGE_TRANSACTION_RESULT"] == "COMMITTED"
    assert scan_iso_shutdown["scan_storage_connection_closed"] is True
    assert scan_iso_shutdown["scan_storage_completed"] == 1
    assert scan_iso_shutdown["scan_storage_failed"] == 0
    lifecycle_shutdown = sessions[0].state["lifecycle_isolation_shutdown"]
    assert lifecycle_shutdown["COMMIT_OR_ROLLBACK_RESULT"] == "COMMITTED"
    assert lifecycle_shutdown["failed"] == 0


def test_watch_mode_recoverable_vs_fatal_storage_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """In watch mode, recoverable storage error records scan_storage=FAILED and status=PARTIAL; fatal error raises."""
    _quiet_runtime(monkeypatch, tmp_path)
    path = _patch_watch_runtime(tmp_path, monkeypatch, symbols=("BTCUSDT",))
    watch_out = tmp_path / "watch_out.jsonl"

    # 1. Recoverable error: transient SQLite lock -> records FAILED, status=PARTIAL
    def _recoverable_store(*_args: Any, **_kwargs: Any) -> str:
        exc = sqlite3.OperationalError("database is locked")
        raise StorageError("transient lock") from exc

    monkeypatch.setattr(run_scan, "store_scan_result", _recoverable_store)

    asyncio.run(
        run_scan.main(
            [
                *_watch_args(path, universe_size=1),
                "--watch-output-file",
                str(watch_out),
            ]
        )
    )
    rows = [json.loads(line) for line in watch_out.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(rows) == 1
    assert rows[0]["status"] == "PARTIAL"
    assert rows[0]["database_storage_status"] == "FAILED"
    assert rows[0]["phase_statuses"]["scan_storage"] == "FAILED"

    # 2. Fatal error: corrupt database -> raises SystemExit immediately
    def _fatal_store(*_args: Any, **_kwargs: Any) -> str:
        exc = sqlite3.DatabaseError("database disk image is malformed")
        raise StorageError("malformed database") from exc

    monkeypatch.setattr(run_scan, "store_scan_result", _fatal_store)

    with pytest.raises(SystemExit, match="malformed database"):
        asyncio.run(run_scan.main(_watch_args(path, universe_size=1)))


def test_one_shot_strict_storage_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One-shot mode strictly raises SystemExit on StorageError."""
    _quiet_runtime(monkeypatch, tmp_path)
    path = _patch_watch_runtime(tmp_path, monkeypatch, symbols=("BTCUSDT",))

    def _failing_store(*_args: Any, **_kwargs: Any) -> str:
        raise StorageError("synthetic one-shot storage failure")

    monkeypatch.setattr(run_scan, "store_scan_result", _failing_store)

    base_args = [
        "--symbols",
        "BTCUSDT",
        "--store-scan",
        "--database-path",
        str(path),
        "--disable-lifecycle",
        "--no-telegram-signals",
        "--no-cache",
    ]
    with pytest.raises(SystemExit, match="synthetic one-shot storage failure"):
        asyncio.run(run_scan.main(base_args))


def test_cancellation_during_recoverable_vs_fatal_scan_storage_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancellation during recoverable scan_storage raises CancelledError; fatal errors remain visible."""
    db_path = tmp_path / "cancel_errors.db"
    args, execution, summary = _make_watch_execution(db_path, run_id="run-cancel-err-001")

    entered = threading.Event()
    release = threading.Event()
    mode = {"kind": "recoverable"}

    def _failing_store(*_args: Any, **_kwargs: Any) -> str:
        entered.set()
        release.wait(timeout=5.0)
        if mode["kind"] == "recoverable":
            try:
                raise sqlite3.OperationalError("database is locked")
            except sqlite3.OperationalError as exc:
                raise StorageError("transient lock") from exc
        raise RuntimeEpochError("fatal epoch violation")

    monkeypatch.setattr(run_scan, "store_scan_result", _failing_store)

    async def _scenario() -> None:
        worker = LifecycleWorker()
        try:
            t_rec = asyncio.create_task(
                run_scan._store_watch_iteration_scan_run_isolated(
                    args,
                    worker=worker,
                    execution=execution,
                    summary=summary,
                    started_at="2026-04-20T12:00:00+00:00",
                    completed_at="2026-04-20T12:00:02+00:00",
                    runtime_sec=1.25,
                    command_used="python scripts/run_scan.py --watch --store-scan",
                )
            )
            while not entered.is_set():
                await asyncio.sleep(0.005)
            t_rec.cancel()
            await asyncio.sleep(0.01)
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await t_rec

            entered.clear()
            release.clear()
            mode["kind"] = "fatal"

            async def _invoke_fatal() -> BaseException | None:
                try:
                    await run_scan._store_watch_iteration_scan_run_isolated(
                        args,
                        worker=worker,
                        execution=execution,
                        summary=summary,
                        started_at="2026-04-20T12:00:00+00:00",
                        completed_at="2026-04-20T12:00:02+00:00",
                        runtime_sec=1.25,
                        command_used="python scripts/run_scan.py --watch --store-scan",
                    )
                except (SystemExit, RuntimeEpochError) as exc:
                    return exc
                return None

            t_fat = asyncio.create_task(_invoke_fatal())
            while not entered.is_set():
                await asyncio.sleep(0.005)
            t_fat.cancel()
            await asyncio.sleep(0.01)
            release.set()
            fatal_exc = await t_fat
            assert isinstance(fatal_exc, (SystemExit, RuntimeEpochError))
            assert "fatal epoch violation" in str(fatal_exc)
        finally:
            await worker.aclose()

    asyncio.run(_scenario())


def test_no_changes_in_telegram_or_trading_eligibility(tmp_path: Path) -> None:
    """Scan storage isolation must not mutate symbol trading eligibility, trade setups, or Telegram outbox state."""
    db_path = tmp_path / "telegram_eligibility.db"
    args, execution, summary = _make_watch_execution(
        db_path,
        run_id="run-tele-elig-001",
        symbols=("BTCUSDT", "ETHUSDT"),
    )

    sym_results_before = [s.model_dump() for s in execution.result.results]
    trade_maps_before = [has_valid_trade_map(s) for s in execution.result.results]
    eligibility_before = [public_watchlist_eligible(s) for s in execution.result.results]
    telegram_status_before = dict(execution.telegram_outbox_status)
    telegram_summary_before = execution.telegram_delivery_summary

    with sqlite3.connect(db_path) as conn:
        tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
        outbox_tables = [t for t in tables if "outbox" in t or "telegram" in t]
        outbox_counts_before = {
            t: conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in outbox_tables
        }

    async def _run() -> str:
        worker = LifecycleWorker()
        try:
            return await run_scan._store_watch_iteration_scan_run_isolated(
                args,
                worker=worker,
                execution=execution,
                summary=summary,
                started_at="2026-04-20T12:00:00+00:00",
                completed_at="2026-04-20T12:00:02+00:00",
                runtime_sec=1.25,
                command_used="python scripts/run_scan.py --watch --store-scan",
            )
        finally:
            await worker.aclose()

    run_id = asyncio.run(_run())
    assert run_id == "run-tele-elig-001"

    sym_results_after = [s.model_dump() for s in execution.result.results]
    trade_maps_after = [has_valid_trade_map(s) for s in execution.result.results]
    eligibility_after = [public_watchlist_eligible(s) for s in execution.result.results]
    assert sym_results_after == sym_results_before
    assert trade_maps_after == trade_maps_before
    assert eligibility_after == eligibility_before

    assert execution.telegram_outbox_status == telegram_status_before
    assert execution.telegram_delivery_summary == telegram_summary_before

    with sqlite3.connect(db_path) as conn:
        for t, count_before in outbox_counts_before.items():
            count_after = conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            assert count_after == count_before
