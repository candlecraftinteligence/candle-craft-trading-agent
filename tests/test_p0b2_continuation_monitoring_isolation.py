"""P0B2 continuation monitoring isolation and equivalence verification.

Every database and frame is synthetic DEV data; no production or Runtime PC access.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import time
import tracemalloc
import weakref
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from websockets.sync.server import serve

from app.core.phase_profile import PhaseProfile, current_observations
from app.data.exceptions import ExchangeClientError
from app.lifecycle import owner_monitoring
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.outcomes import (
    OUTCOME_ELIGIBLE_STATES,
    TERMINAL_OUTCOME_STATES,
)
from app.lifecycle.owner_monitoring import (
    GAP_EXCHANGE_MARKET_DATA,
    GAP_MARKET_UNSUPPORTED,
    GAP_REQUIRED_CLOSED_CANDLE,
    OwnerMonitoringResult,
    SymbolMonitoringEvidence,
    evidence_key,
    monitor_obligations_with_market_data,
    stage_1_continuation_read,
    stage_3_continuation_write,
)
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.microstructure.reconnect import FeedRecovery
from app.microstructure.service import BinanceWebSocketTransport
from app.research.durable_source_replay import capture
from app.runtime.lifecycle_isolation import (
    LifecycleWorker,
    LifecycleWorkerClosed,
    LifecycleWorkerSaturated,
)
from app.runtime.loop_lag import LoopLagMonitor
from app.runtime_epoch.errors import (
    RuntimeEpochConfigurationError,
    RuntimeEpochError,
    RuntimeEpochOriginError,
)
from app.runtime_epoch.origin import register_operational_run
from app.storage.database import StorageError
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY, bootstrap_operational_test_database
from tests.test_p5a_active_owner_monitoring import (
    TIMEFRAME,
    _continuation_candles,
    _decision,
    _latched,
    _progress,
    _seed_entered,
)

NOW = datetime(2026, 4, 1, 0, 20, tzinfo=UTC)
RUN_ID = "p0b2-synthetic-run"


def _repo(path, **kwargs):
    kwargs.setdefault('expected_identity', SYNTHETIC_IDENTITY)
    return SQLiteSetupLifecycleRepository(path, **kwargs)


def _database(path: Path) -> Path:
    bootstrap_operational_test_database(path)
    with closing(sqlite3.connect(path)) as connection:
        register_operational_run(connection, run_id=RUN_ID, registered_at="2026-03-01T00:00:00Z")
        connection.commit()
    return path


def _track_connections(monkeypatch, *, gate_at=None, entered=None, release=None, fail_commit=False):
    calls = []
    original = sqlite3.connect

    class Tracked(sqlite3.Connection):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._had_immediate = False
            calls.append((id(self), "open", threading.get_ident()))

        def execute(self, sql, *args, **kwargs):
            calls.append((id(self), sql, threading.get_ident()))
            if "BEGIN IMMEDIATE" in str(sql):
                self._had_immediate = True
            result = super().execute(sql, *args, **kwargs)
            if gate_at == "transaction" and sql == "BEGIN IMMEDIATE":
                if entered is not None:
                    entered.set()
                if release is not None:
                    assert release.wait(10), "transaction test barrier timed out"
            return result

        def commit(self):
            calls.append((id(self), "commit", threading.get_ident()))
            if gate_at == "commit" and self.in_transaction:
                if entered is not None:
                    entered.set()
                if release is not None:
                    assert release.wait(10), "commit test barrier timed out"
            if fail_commit and self._had_immediate and self.in_transaction:
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


def test_stage_1_and_stage_3_worker_ownership_and_no_conn_across_fetch(tmp_path, monkeypatch):
    """Items 1, 2, 3, 4, 5: Worker owns Stage 1 & 3, Stage 2 is async on loop, no conn across await."""
    path = _database(tmp_path / "ownership.sqlite")
    with _repo(path) as repository:
        owner = _seed_entered(repository, symbol="BTCUSDT")

    loop_thread = threading.get_ident()
    fetch_thread = None
    fetch_started = asyncio.Event()

    calls = _track_connections(monkeypatch)

    async def fetch(symbol, timeframe, limit):
        nonlocal fetch_thread
        fetch_thread = threading.get_ident()
        # Assert that during Stage 2 fetch, NO SQLite connection is in_transaction
        # And any opened connection before fetch has already been closed
        opens_before = [item for item in calls if item[1] == "open"]
        closes_before = [item for item in calls if item[1] == "close"]
        assert len(opens_before) > 0, "Stage 1 must have opened at least one connection"
        assert len(opens_before) == len(closes_before), "Every Stage 1 connection must be closed before fetch"
        fetch_started.set()
        await asyncio.sleep(0.01)
        return tuple(_continuation_candles())

    async def scenario():
        worker = LifecycleWorker()
        try:
            result = await monitor_obligations_with_market_data(
                path,
                evidence_by_key={},
                fetch_candles=fetch,
                execution_timeframe=TIMEFRAME,
                candle_limit=10,
                evaluated_at=_decision(2),
                scan_run_id=RUN_ID,
                expected_identity=SYNTHETIC_IDENTITY,
                worker=worker,
            )
            return result, worker.compact_snapshot()
        finally:
            await worker.aclose()

    result, snapshot = asyncio.run(scenario())

    # Verify Stage 2 executed on loop thread
    assert fetch_thread == loop_thread

    # Verify Stage 1 and Stage 3 opened connections on worker thread
    opens = [item for item in calls if item[1] == "open"]
    closes = [item for item in calls if item[1] == "close"]
    assert len(opens) == len(closes), "Every opened connection must be cleanly closed"
    worker_threads = {item[2] for item in opens}
    assert len(worker_threads) == 1, "All SQLite operations must occur on a single worker thread"
    stage_worker_thread = worker_threads.pop()
    assert stage_worker_thread != loop_thread, "Stage 1 and Stage 3 must NOT run on the event loop thread"

    # Verify metrics captured
    assert snapshot["CONTINUATION_READ_WORKER_DURATION"] > 0
    assert snapshot["CONTINUATION_WRITE_WORKER_DURATION"] > 0
    assert snapshot["CONTINUATION_TRANSACTION_RESULT"] == "COMMITTED"
    assert snapshot["connection_closed"] is True


def test_runtime_epoch_mismatch_fails_closed(tmp_path):
    """Item 6: Runtime epoch mismatch fails closed."""
    path = _database(tmp_path / "epoch_mismatch.sqlite")
    with _repo(path) as repository:
        _seed_entered(repository, symbol="BTCUSDT")

    mismatched_identity = replace(SYNTHETIC_IDENTITY, epoch_id="wrong-epoch-id")

    async def scenario():
        with pytest.raises((RuntimeEpochConfigurationError, RuntimeEpochError)):
            await monitor_obligations_with_market_data(
                path,
                evidence_by_key={},
                fetch_candles=None,
                execution_timeframe=TIMEFRAME,
                candle_limit=10,
                evaluated_at=_decision(2),
                scan_run_id=RUN_ID,
                expected_identity=mismatched_identity,
            )

    asyncio.run(scenario())


def test_registered_operational_run_checks_valid(tmp_path):
    """Item 7: Registered operational run checks remain valid when require_registered_run is True."""
    path = _database(tmp_path / "run_reg.sqlite")
    with _repo(path) as repository:
        _seed_entered(repository, symbol="BTCUSDT")

    async def scenario():
        # 1. Unregistered run fails closed with RuntimeEpochOriginError
        with pytest.raises(RuntimeEpochOriginError, match="not registered"):
            await monitor_obligations_with_market_data(
                path,
                evidence_by_key={},
                fetch_candles=None,
                execution_timeframe=TIMEFRAME,
                candle_limit=10,
                evaluated_at=_decision(2),
                scan_run_id="unregistered-run-id",
                expected_identity=SYNTHETIC_IDENTITY,
                require_registered_run=True,
            )

        # 2. Registered run succeeds
        result = await monitor_obligations_with_market_data(
            path,
            evidence_by_key={},
            fetch_candles=None,
            execution_timeframe=TIMEFRAME,
            candle_limit=10,
            evaluated_at=_decision(2),
            scan_run_id=RUN_ID,
            expected_identity=SYNTHETIC_IDENTITY,
            require_registered_run=True,
        )
        assert len(result.obligation_lifecycle_ids) == 1

    asyncio.run(scenario())


def test_deterministic_equivalence_with_baseline(tmp_path):
    """Items 8, 9, 10, 11: Selection, cursor lag, plan IDs and outcomes match baseline."""
    sync_path = _database(tmp_path / "sync.sqlite")
    worker_path = _database(tmp_path / "worker.sqlite")

    with _repo(sync_path) as r1:
        owner1 = _seed_entered(r1, symbol="ETHUSDT")
    with _repo(worker_path) as r2:
        owner2 = _seed_entered(r2, symbol="ETHUSDT", lifecycle_id=owner1.lifecycle_id)

    candles = tuple(_continuation_candles())

    async def fetch(*args):
        return candles

    async def scenario():
        # Run isolated
        result_isolated = await monitor_obligations_with_market_data(
            worker_path,
            evidence_by_key={},
            fetch_candles=fetch,
            execution_timeframe=TIMEFRAME,
            candle_limit=10,
            evaluated_at=_decision(2),
            scan_run_id=RUN_ID,
            expected_identity=SYNTHETIC_IDENTITY,
        )
        return result_isolated

    isolated_result = asyncio.run(scenario())

    # Directly run baseline on sync_path
    with _repo(sync_path) as repository:
        obligations = owner_monitoring.select_tracking_obligations(repository)
        baseline_result = owner_monitoring.monitor_tracking_obligations(
            repository,
            evidence_by_key={
                evidence_key("ETHUSDT", TIMEFRAME): SymbolMonitoringEvidence(
                    candles=candles,
                    execution_timeframe=TIMEFRAME,
                    decision_timestamp=_decision(2),
                    lineage="fetched_without_delivery_envelope",
                )
            },
            evaluated_at=_decision(2),
            scan_run_id=RUN_ID,
            default_timeframe=TIMEFRAME,
            record_missing_evidence=True,
        )

    # Compare exact fields
    assert isolated_result.evaluated_lifecycle_ids == baseline_result.evaluated_lifecycle_ids
    assert isolated_result.obligation_lifecycle_ids == baseline_result.obligation_lifecycle_ids
    assert isolated_result.verified_lifecycle_ids == baseline_result.verified_lifecycle_ids
    assert isolated_result.uncovered_symbols == baseline_result.uncovered_symbols
    assert isolated_result.gap_lifecycle_ids == baseline_result.gap_lifecycle_ids
    assert isolated_result.errors == baseline_result.errors
    assert len(isolated_result.lags) == len(baseline_result.lags)
    assert isolated_result.lags[0].as_dict() == baseline_result.lags[0].as_dict()

    # Compare persisted progress rows
    with _repo(worker_path) as r_w, _repo(sync_path) as r_s:
        pw = _progress(r_w, owner1.lifecycle_id)
        ps = _progress(r_s, owner1.lifecycle_id)
        assert pw.terminal_outcome == ps.terminal_outcome
        assert pw.evaluation_cursor_close_at == ps.evaluation_cursor_close_at
        assert pw.plan_version_id == ps.plan_version_id
        assert pw.integrity_status == ps.integrity_status


def test_stale_obligation_protection_superseded_and_terminal(tmp_path):
    """Item 12: Superseded and terminal owners do not gain unauthorized updates."""
    path = _database(tmp_path / "stale.sqlite")
    with _repo(path) as repository:
        owner = _seed_entered(repository, symbol="BTCUSDT")

    # Hook fetch to mutate the plan into a terminal state while fetch is happening (between Stage 1 and Stage 3)
    fetch_barrier = asyncio.Event()

    async def fetch(symbol, timeframe, limit):
        # Between Stage 1 and Stage 3, another transaction supersedes or makes the owner terminal
        with closing(sqlite3.connect(path)) as conn:
            conn.execute(
                "UPDATE setup_lifecycle_records SET current_state = 'SL_HIT' WHERE lifecycle_id = ?",
                (owner.lifecycle_id,),
            )
            conn.execute(
                "UPDATE setup_lifecycle_outcome_progress SET terminal_outcome = 'SL_HIT' WHERE lifecycle_id = ?",
                (owner.lifecycle_id,),
            )
            conn.commit()
        fetch_barrier.set()
        return tuple(_continuation_candles())

    async def scenario():
        result = await monitor_obligations_with_market_data(
            path,
            evidence_by_key={},
            fetch_candles=fetch,
            execution_timeframe=TIMEFRAME,
            candle_limit=10,
            evaluated_at=_decision(2),
            scan_run_id=RUN_ID,
            expected_identity=SYNTHETIC_IDENTITY,
        )
        return result

    result = asyncio.run(scenario())
    assert fetch_barrier.is_set()

    # The stale obligation was blocked and not evaluated in Stage 3
    assert owner.lifecycle_id not in result.evaluated_lifecycle_ids

    # Persisted row retained its SL_HIT disposition without corruption
    with _repo(path) as repository:
        progress = _progress(repository, owner.lifecycle_id)
        assert progress.terminal_outcome == "SL_HIT"


def test_missing_candles_and_failures_classification(tmp_path):
    """Items 13, 14, 15: Missing candles remain missing evidence; provider failures classified."""
    path = _database(tmp_path / "failures.sqlite")
    with _repo(path) as repository:
        owner1 = _seed_entered(repository, lifecycle_id="life-gap", symbol="BTCUSDT")
        owner2 = _seed_entered(repository, lifecycle_id="life-unsupp", symbol="OLDUSDT")
        owner3 = _seed_entered(repository, lifecycle_id="life-err", symbol="FAILUSDT")

    async def fetch(symbol, timeframe, limit):
        if symbol == "BTCUSDT":
            # Missing candles -> empty tuple
            return ()
        if symbol == "OLDUSDT":
            # Unsupported / delisted market error
            raise ExchangeClientError("Binance: symbol OLDUSDT is delisted or unknown symbol")
        if symbol == "FAILUSDT":
            # Unexpected adapter or programming error
            raise ValueError("unexpected bug in client adapter")
        return ()

    async def scenario():
        result = await monitor_obligations_with_market_data(
            path,
            evidence_by_key={},
            fetch_candles=fetch,
            execution_timeframe=TIMEFRAME,
            candle_limit=10,
            evaluated_at=_decision(2),
            scan_run_id=RUN_ID,
            expected_identity=SYNTHETIC_IDENTITY,
            record_missing_evidence=True,
        )
        return result

    result = asyncio.run(scenario())

    # BTCUSDT -> gap_reason GAP_REQUIRED_CLOSED_CANDLE
    assert "life-gap" in result.gap_lifecycle_ids

    # OLDUSDT -> gap_reason GAP_MARKET_UNSUPPORTED
    assert "life-unsupp" in result.gap_lifecycle_ids

    # FAILUSDT -> unexpected error reported in errors, NOT persisted as gap
    error_symbols = [e["symbol"] for e in result.errors]
    assert "FAILUSDT" in error_symbols
    assert "life-err" not in result.gap_lifecycle_ids


def test_skip_keys_and_skip_lifecycle_ids_remain_correct(tmp_path):
    """Item 16: Existing skip_keys and skip_lifecycle_ids remain correct."""
    path = _database(tmp_path / "skip.sqlite")
    with _repo(path) as repository:
        owner1 = _seed_entered(repository, lifecycle_id="life-skip-key", symbol="BTCUSDT")
        owner2 = _seed_entered(repository, lifecycle_id="life-skip-id", symbol="ETHUSDT")

    fetched_symbols = []

    async def fetch(symbol, timeframe, limit):
        fetched_symbols.append(symbol)
        return tuple(_continuation_candles())

    async def scenario():
        result = await monitor_obligations_with_market_data(
            path,
            evidence_by_key={},
            fetch_candles=fetch,
            execution_timeframe=TIMEFRAME,
            candle_limit=10,
            evaluated_at=_decision(2),
            scan_run_id=RUN_ID,
            expected_identity=SYNTHETIC_IDENTITY,
            skip_keys=(evidence_key("BTCUSDT", TIMEFRAME),),
            skip_lifecycle_ids=("life-skip-id",),
        )
        return result

    result = asyncio.run(scenario())

    # BTCUSDT was skipped in Stage 1/2, so fetch was not called for it
    assert "BTCUSDT" not in fetched_symbols
    assert "ETHUSDT" in fetched_symbols

    # ETHUSDT was fetched, but skipped in Stage 3 by skip_lifecycle_ids
    assert "life-skip-id" in result.skipped_lifecycle_ids
    assert "life-skip-id" not in result.evaluated_lifecycle_ids


def test_cancellation_fenced_stage_1(tmp_path, monkeypatch):
    """Item 17: Cancellation during Stage 1 is fenced."""
    path = _database(tmp_path / "cancel_s1.sqlite")
    with _repo(path) as repository:
        _seed_entered(repository, symbol="BTCUSDT")

    entered, release = threading.Event(), threading.Event()
    original_select = owner_monitoring.select_tracking_obligations

    def gated_select(repository):
        entered.set()
        assert release.wait(10), "stage 1 barrier timed out"
        return original_select(repository)

    monkeypatch.setattr(owner_monitoring, "select_tracking_obligations", gated_select)

    async def scenario():
        worker = LifecycleWorker()
        task = asyncio.create_task(
            monitor_obligations_with_market_data(
                path,
                evidence_by_key={},
                fetch_candles=None,
                execution_timeframe=TIMEFRAME,
                candle_limit=10,
                evaluated_at=_decision(2),
                scan_run_id=RUN_ID,
                expected_identity=SYNTHETIC_IDENTITY,
                worker=worker,
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0.01)
            # Cancellation must NOT abort work mid-query
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await worker.aclose()

    asyncio.run(scenario())


def test_cancellation_during_stage_2_does_not_write_synthetic_gap(tmp_path):
    """Item 18: Cancellation during Stage 2 does not write a synthetic gap."""
    path = _database(tmp_path / "cancel_s2.sqlite")
    with _repo(path) as repository:
        owner = _seed_entered(repository, symbol="BTCUSDT")
        progress_before = _progress(repository, owner.lifecycle_id)

    fetch_entered = asyncio.Event()

    async def hanging_fetch(symbol, timeframe, limit):
        fetch_entered.set()
        await asyncio.sleep(10)
        return ()

    async def scenario():
        task = asyncio.create_task(
            monitor_obligations_with_market_data(
                path,
                evidence_by_key={},
                fetch_candles=hanging_fetch,
                execution_timeframe=TIMEFRAME,
                candle_limit=10,
                evaluated_at=_decision(2),
                scan_run_id=RUN_ID,
                expected_identity=SYNTHETIC_IDENTITY,
                record_missing_evidence=True,
            )
        )
        await fetch_entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    # Verify that Stage 3 write NEVER executed: no synthetic gap was persisted!
    with _repo(path) as repository:
        progress_after = _progress(repository, owner.lifecycle_id)
        assert progress_before == progress_after


def test_cancellation_fenced_stage_3(tmp_path, monkeypatch):
    """Item 19: Cancellation during Stage 3 is fenced."""
    path = _database(tmp_path / "cancel_s3.sqlite")
    with _repo(path) as repository:
        _seed_entered(repository, symbol="BTCUSDT")

    entered, release = threading.Event(), threading.Event()
    original_monitor = owner_monitoring.monitor_tracking_obligations

    def gated_monitor(*args, **kwargs):
        entered.set()
        assert release.wait(10), "stage 3 barrier timed out"
        return original_monitor(*args, **kwargs)

    monkeypatch.setattr(owner_monitoring, "monitor_tracking_obligations", gated_monitor)

    async def scenario():
        worker = LifecycleWorker()
        task = asyncio.create_task(
            monitor_obligations_with_market_data(
                path,
                evidence_by_key={},
                fetch_candles=None,
                execution_timeframe=TIMEFRAME,
                candle_limit=10,
                evaluated_at=_decision(2),
                scan_run_id=RUN_ID,
                expected_identity=SYNTHETIC_IDENTITY,
                worker=worker,
            )
        )
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0.01)
            # Cancellation must NOT abort transaction mid-write
            assert not task.done()
            release.set()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            release.set()
            await worker.aclose()

    asyncio.run(scenario())


def test_commit_failure_causes_rollback(tmp_path, monkeypatch):
    """Item 20: Failure during commit causes the required rollback behavior."""
    path = _database(tmp_path / "commit_fail.sqlite")
    with _repo(path) as repository:
        owner = _seed_entered(repository, symbol="BTCUSDT")
        progress_before = _progress(repository, owner.lifecycle_id)

    # Inject failure on commit in Stage 3
    calls = _track_connections(monkeypatch, fail_commit=True)

    async def scenario():
        worker = LifecycleWorker()
        try:
            with pytest.raises(sqlite3.OperationalError, match="synthetic commit failure"):
                await monitor_obligations_with_market_data(
                    path,
                    evidence_by_key={},
                    fetch_candles=None,
                    execution_timeframe=TIMEFRAME,
                    candle_limit=10,
                    evaluated_at=_decision(2),
                    scan_run_id=RUN_ID,
                    expected_identity=SYNTHETIC_IDENTITY,
                    worker=worker,
                )
            snapshot = worker.compact_snapshot()
            assert snapshot["CONTINUATION_TRANSACTION_RESULT"] == "COMMIT_FAILED_ROLLED_BACK"
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    # Verify rollback happened: database state untouched
    with _repo(path) as repository:
        progress_after = _progress(repository, owner.lifecycle_id)
        assert progress_before == progress_after


def test_no_continuation_overlaps_and_saturation_rejection(tmp_path):
    """Item 21: Bounded admission refuses a second waiting caller."""
    path = _database(tmp_path / "saturation.sqlite")

    async def scenario():
        worker = LifecycleWorker()
        entered, release = threading.Event(), threading.Event()

        def slow_read(*args, **kwargs):
            entered.set()
            assert release.wait(10)
            return (), {}, {}

        task1 = asyncio.create_task(worker.call(slow_read))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            # One waiter permitted
            task2 = asyncio.create_task(worker.call(lambda: "waiter1"))
            await asyncio.sleep(0.01)
            assert worker.compact_snapshot()["waiters"] == 1

            # Second waiter is rejected with LifecycleWorkerSaturated
            with pytest.raises(LifecycleWorkerSaturated):
                await worker.call(lambda: "waiter2")

            release.set()
            await task1
            assert await task2 == "waiter1"
        finally:
            release.set()
            await worker.aclose()

    asyncio.run(scenario())


def test_shutdown_waits_for_continuation_completion(tmp_path):
    """Item 23: Scanner stop waits for worker completion."""
    async def scenario():
        worker = LifecycleWorker()
        entered, release = threading.Event(), threading.Event()

        def in_flight():
            entered.set()
            time.sleep(0.05)
            return "done"

        task = asyncio.create_task(worker.call(in_flight))
        assert await asyncio.to_thread(entered.wait, 5)
        # Call aclose while task is in flight
        await worker.aclose()
        assert task.done()
        assert await task == "done"
        assert worker.shutdown_report["joined"] is True
        assert worker.shutdown_report["thread_alive"] is False

    asyncio.run(scenario())


def test_loopback_websocket_receiver_progresses_during_slow_continuation_work(tmp_path, monkeypatch):
    """Item 21: Loopback websocket receiver makes progress concurrently while continuation SQLite work executes."""
    from websockets.sync.server import serve
    path = _database(tmp_path / "ws_concurrency.sqlite")
    with _repo(path) as repository:
        _seed_entered(repository, symbol="BTCUSDT")

    entered, processed = threading.Event(), threading.Event()
    # Gate Stage 3 write transaction
    _track_connections(monkeypatch, gate_at="transaction", entered=entered, release=processed)
    recovery = FeedRecovery("p0b2-loopback", 0.05, 1.0)

    def provider(server_connection):
        assert entered.wait(5), "Worker did not reach Stage 3 transaction in time"
        server_connection.send("synthetic unverified fixture frame")
        assert server_connection.recv(timeout=5) == "fixture complete"

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
            async def run_monitoring():
                async def fetch(symbol, timeframe, limit):
                    return tuple(_continuation_candles())

                return await monitor_obligations_with_market_data(
                    path,
                    evidence_by_key={},
                    fetch_candles=fetch,
                    execution_timeframe=TIMEFRAME,
                    candle_limit=10,
                    evaluated_at=_decision(2),
                    scan_run_id=RUN_ID,
                    expected_identity=SYNTHETIC_IDENTITY,
                    worker=worker,
                )

            monitoring_task = asyncio.create_task(run_monitoring())
            await asyncio.wait_for(receiver, 5)
            await asyncio.wait_for(monitoring_task, 5)
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
            thread.join(timeout=2)


def test_synthetic_continuation_loop_lag_wall_time_and_memory(tmp_path, monkeypatch):
    """Dev performance benchmark: measures loop lag, worker duration, queue wait and memory."""
    paths = [_database(tmp_path / name) for name in ("sync.sqlite", "isolated.sqlite")]
    for p in paths:
        with _repo(p) as repository:
            _seed_entered(repository, symbol="BTCUSDT")

    original_stage1 = owner_monitoring.stage_1_continuation_read

    def slow_stage1(*args, **kwargs):
        # 0.5 s synthetic I/O stall
        time.sleep(0.5)
        return original_stage1(*args, **kwargs)

    monkeypatch.setattr(owner_monitoring, "stage_1_continuation_read", slow_stage1)

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
            profile.lifecycle_worker = worker
            try:
                await profile.acall(
                    "owner_monitoring",
                    monitor_obligations_with_market_data,
                    path,
                    evidence_by_key={},
                    fetch_candles=None,
                    execution_timeframe=TIMEFRAME,
                    candle_limit=10,
                    evaluated_at=_decision(2),
                    scan_run_id=RUN_ID,
                    expected_identity=SYNTHETIC_IDENTITY,
                    worker=worker,
                )
                snapshot = profile.snapshot(boundary="complete")
            finally:
                await worker.aclose()
        else:
            # Synchronous stall on event loop
            def sync_wrapper():
                time.sleep(0.5)

            profile.call("owner_monitoring", sync_wrapper)
            snapshot = profile.snapshot(boundary="complete")

        wall = time.perf_counter() - started
        await asyncio.sleep(0.04)
        await monitor.stop()
        return {
            "wall_seconds": round(wall, 6),
            "lag_seconds": round(monitor.max_lag, 6),
            "samples": monitor.samples,
            "snapshot": snapshot,
        }

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
    assert growth < 400_000, "worker copied payload instead of borrowing reference"
    assert after["samples"] > before["samples"]
    assert after["lag_seconds"] < before["lag_seconds"]

    iso_metrics = after["snapshot"]["continuation_isolation"]
    assert iso_metrics["CONTINUATION_READ_WORKER_DURATION"] > 0
    assert iso_metrics["CONTINUATION_TRANSACTION_RESULT"] == "COMMITTED"

    print("\nP0B2_DEV_MEASUREMENTS=" + json.dumps({
        "before": before,
        "after": after,
        "python_allocation_peak_bytes": growth,
        "production_performance_claim": False,
    }, sort_keys=True))
