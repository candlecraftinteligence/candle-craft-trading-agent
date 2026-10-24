"""OS lease and session observations on synthetic paths, never a scanner process."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import watch_session as observations
from app.watch_session import CONTRACT, MAX_RECEIPT_BYTES, WatchSession, WatchSessionError, append_session_observation
from app.watch_supervisor import schedule_after_iteration
from scripts import run_scan
from tests.test_watch_mode import SequenceWatchRunner, _bootstrap_watch_main
from tests.test_p5a_monitoring_binding_query_repair import _quiet_runtime
from tests.test_p5a_active_owner_monitoring import _rejected_result, _continuation_candles

START = "2026-10-23T10:00:00+00:00"
END = "2026-10-23T10:20:00+00:00"


def _read(session):
    return json.loads(session.receipt_path.read_text(encoding="utf-8"))


def test_same_database_contends_without_overwriting_or_unlocking_owner(tmp_path):
    path = tmp_path / "runtime.db"
    with WatchSession(path, interval_seconds=300) as first:
        before = first.receipt_path.read_bytes()
        for equivalent in (path, tmp_path / "unused" / ".." / "runtime.db"):
            with pytest.raises(WatchSessionError, match="lease unavailable"):
                with WatchSession(equivalent, interval_seconds=300):
                    pytest.fail("a competing watch must not start")
        assert first.receipt_path.read_bytes() == before
        with WatchSession(tmp_path / "other.db", interval_seconds=300):
            pass
    assert not path.exists()  # The module never opens/creates a database.
    assert first.lock_path.exists()
    with WatchSession(path, interval_seconds=300):
        pass


@pytest.mark.parametrize("invalid", [b"unknown", b"{}", b"x" * (MAX_RECEIPT_BYTES + 1)], ids=["unknown", "empty-object", "oversize"])
def test_unknown_corrupt_and_oversize_receipts_are_never_overwritten(tmp_path, invalid):
    session = WatchSession(tmp_path / "runtime.db", interval_seconds=300)
    session.receipt_path.write_bytes(invalid)
    with pytest.raises(WatchSessionError, match="refusing overwrite"):
        with session:
            pytest.fail("unknown receipt cannot authorize startup")
    assert session.receipt_path.read_bytes() == invalid


def test_unknown_lock_header_is_never_replaced(tmp_path):
    session = WatchSession(tmp_path / "runtime.db", interval_seconds=300)
    session.lock_path.write_bytes(b"unknown protected bytes")
    with pytest.raises(WatchSessionError, match="header"):
        with session:
            pass
    assert session.lock_path.read_bytes() == b"unknown protected bytes"


def test_empty_reserved_marker_and_simultaneous_first_starts_have_one_owner(tmp_path):
    for index in range(5):
        path = tmp_path / f"runtime-{index}.db"
        marker = Path(f"{path}.watch.lock")
        marker.write_bytes(b"")
        gate, loser_seen = threading.Barrier(2), threading.Event()
        def contend(_):
            gate.wait(timeout=10)
            try:
                with WatchSession(path, interval_seconds=300):
                    assert loser_seen.wait(timeout=10)
                    return "owned"
            except WatchSessionError:
                loser_seen.set()
                return "blocked"
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sorted(pool.map(contend, (0, 1))) == ["blocked", "owned"]
        with WatchSession(path, interval_seconds=300):
            pass


def test_failed_replace_releases_lease_and_preserves_prior_receipt(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    with WatchSession(path, interval_seconds=300) as session:
        pass
    before = session.receipt_path.read_bytes()
    def fail(*args):
        raise OSError("synthetic replacement failure")
    with monkeypatch.context() as local:
        local.setattr(observations.os, "replace", fail)
        with pytest.raises(OSError, match="synthetic replacement"):
            with WatchSession(path, interval_seconds=300):
                pass
    assert session.receipt_path.read_bytes() == before
    assert list(tmp_path.glob("*.tmp")) == []
    with WatchSession(path, interval_seconds=300):
        pass


def test_shutdown_write_failure_reports_both_errors_and_releases(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    with monkeypatch.context() as local:
        with pytest.raises(WatchSessionError, match="original failure.*shutdown observation failed"):
            with WatchSession(path, interval_seconds=300) as session:
                local.setattr(session, "_write", lambda: (_ for _ in ()).throw(OSError("receipt write failure")))
                raise RuntimeError("original failure")
    assert _read(session)["shutdown_finalization_observed_at"] == "N/A"
    with WatchSession(path, interval_seconds=300):
        pass


def _fail_close(session):
    original = session._handle
    class Handle:
        def __getattr__(self, name):
            return getattr(original, name)
        def close(self):
            original.close()
            raise OSError("synthetic descriptor close failure")
    session._handle = Handle()


def test_release_error_cannot_replace_original_and_shutdown_write_failure(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    with pytest.raises(WatchSessionError) as failure:
        with WatchSession(path, interval_seconds=300) as session:
            _fail_close(session)
            monkeypatch.setattr(session, "_write", lambda: (_ for _ in ()).throw(OSError("synthetic shutdown write failure")))
            raise RuntimeError("original scanner failure")
    detail = str(failure.value)
    assert all(text in detail for text in ("original scanner failure", "shutdown write failure", "descriptor close failure"))
    assert session._handle is None
    with WatchSession(path, interval_seconds=300):
        pass


def test_release_error_cannot_replace_startup_failure(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    def fail_prior(session):
        _fail_close(session)
        raise RuntimeError("original startup failure")
    with monkeypatch.context() as local:
        local.setattr(WatchSession, "_read_prior", fail_prior)
        with pytest.raises(WatchSessionError, match="original startup failure.*descriptor close failure"):
            with WatchSession(path, interval_seconds=300):
                pass
    with WatchSession(path, interval_seconds=300):
        pass


def test_temporary_cleanup_error_preserves_replace_failure(tmp_path, monkeypatch):
    path = tmp_path / "runtime.db"
    original_unlink = Path.unlink
    def fail_unlink(temporary, *args, **kwargs):
        if temporary.name.endswith(".tmp"):
            raise OSError("synthetic temporary cleanup failure")
        return original_unlink(temporary, *args, **kwargs)
    with monkeypatch.context() as local:
        local.setattr(observations.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("original replace failure")))
        local.setattr(Path, "unlink", fail_unlink)
        with pytest.raises(WatchSessionError, match="original replace failure.*temporary cleanup failure"):
            with WatchSession(path, interval_seconds=300):
                pass
    with WatchSession(path, interval_seconds=300):
        pass


def test_unowned_temporary_path_collision_is_never_deleted(tmp_path, monkeypatch):
    session = WatchSession(tmp_path / "runtime.db", interval_seconds=300)
    collision = session.receipt_path.with_name(f"{session.receipt_path.name}.{'0' * 32}.tmp")
    collision.write_bytes(b"unknown protected bytes")
    monkeypatch.setattr(observations, "uuid4", lambda: SimpleNamespace(hex="0" * 32))
    with pytest.raises(FileExistsError):
        with session:
            pass
    assert collision.read_bytes() == b"unknown protected bytes"


def test_completion_scope_and_nonrecursive_prior_observation_are_bounded(tmp_path):
    path = tmp_path / "runtime.db"
    rows = []
    for index in range(20):
        with WatchSession(path, interval_seconds=300, on_observation=rows.append, now=lambda: START) as session:
            session.start_iteration(iteration=1, actual_start=START, scheduled_start=START)
            schedule = schedule_after_iteration(scheduled_start_monotonic=0, actual_start_monotonic=0,
                                                finished_monotonic=650, interval_seconds=300)
            session.finish_iteration(status="PARTIAL", phases={"scanner": "SUCCESS", "scan_storage": "FAILED"},
                                     schedule=schedule, iteration_id=f"synthetic-{index}")
    receipt = _read(session)
    assert len(session.receipt_path.read_bytes()) < MAX_RECEIPT_BYTES
    assert "previous_session" not in receipt["previous_session"]
    latest = receipt["latest_iteration"]
    assert latest["completion_observed"] is True and latest["status"] == "PARTIAL"
    assert latest["duration_seconds"] == 650 and latest["missed_interval_count"] == 2
    assert "before_receipt_write" in latest["duration_scope"]
    assert len([row for row in rows if row["record_kind"] == "watch_iteration_finalization_observation"]) == 20


def test_negative_cross_session_clock_is_unavailable_not_zero(tmp_path):
    path = tmp_path / "runtime.db"
    with WatchSession(path, interval_seconds=300, now=lambda: END):
        pass
    rows = []
    with WatchSession(path, interval_seconds=300, on_observation=rows.append, now=lambda: START):
        pass
    boundary = rows[0]
    assert boundary["wall_clock_elapsed_seconds"] == "N/A"
    assert boundary["missed_interval_count"] == "N/A" and boundary["cause"] == "Unverified"


@pytest.mark.parametrize("tail", [b"unknown", b'{"contract":"unknown"}\n'])
def test_unknown_or_incomplete_journal_is_not_repaired(tmp_path, tail):
    path = tmp_path / "runtime.db"
    journal = Path(f"{path}.watch-observations.jsonl")
    journal.write_bytes(tail)
    with pytest.raises(WatchSessionError, match="refusing append"):
        with WatchSession(path, interval_seconds=300,
                          on_observation=lambda row: append_session_observation(path, row)):
            pass
    assert journal.read_bytes() == tail


def _helper(path, ready):
    code = """
import os, sys
from pathlib import Path
from app.watch_session import WatchSession
with WatchSession(sys.argv[1], interval_seconds=300) as session:
    session.start_iteration(iteration=1, actual_start='2026-10-23T10:00:00+00:00', scheduled_start='2026-10-23T10:00:00+00:00')
    Path(sys.argv[2]).write_text('ready', encoding='ascii')
    sys.stdin.readline()
    os._exit(23)
"""
    return subprocess.Popen([sys.executable, "-c", code, str(path), str(ready)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


def test_abrupt_helper_exit_releases_os_lease_and_exposes_missing_completion(tmp_path):
    path, ready = tmp_path / "runtime.db", tmp_path / "ready"
    child = _helper(path, ready)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and child.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists(), child.communicate(timeout=5)
        with pytest.raises(WatchSessionError, match="lease unavailable"):
            with WatchSession(path, interval_seconds=300):
                pass
        child.stdin.write("exit\n")
        child.stdin.flush()
        child.wait(timeout=10)
        assert child.returncode == 23
        rows = []
        with WatchSession(path, interval_seconds=300, on_observation=rows.append):
            pass
        boundary = rows[0]
        assert boundary["prior_shutdown_unobserved"] is True
        assert boundary["prior_iteration_completion_unobserved"] is True
        assert boundary["cause"] == "Unverified" and boundary["missed_interval_count"] == "N/A"
    finally:
        if child.poll() is None:
            child.kill()
        child.communicate(timeout=10)


def _bootstrap(tmp_path, monkeypatch):
    _quiet_runtime(monkeypatch, tmp_path)
    path, _ = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_rejected_result("BTCUSDT", tuple(_continuation_candles()), mode="scalp", bias=None),)]
    args = ["--symbols", "BTCUSDT", "--database-path", str(path), "--watch", "--watch-max-iterations", "1", "--watch-interval-sec", "0.01"]
    return path, args


def test_real_main_finishes_receipt_and_keeps_scan_manifest_population(tmp_path, monkeypatch):
    path, args = _bootstrap(tmp_path, monkeypatch)
    asyncio.run(run_scan.main(args))
    receipt = json.loads(Path(f"{path}.watch-session.json").read_text(encoding="utf-8"))
    journal = [json.loads(line) for line in Path(f"{path}.watch-observations.jsonl").read_text(encoding="utf-8").splitlines()]
    assert journal[0]["contract"] == CONTRACT
    assert journal[-1]["record_kind"] == "watch_iteration_finalization_observation"
    assert receipt["latest_iteration"]["completion_observed"] is True
    assert receipt["shutdown_outcome"] == "COMPLETED"
    manifest = [json.loads(line) for line in (tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(manifest) == 1
    assert manifest[0]["watch_supervisor"]["watch_session_id"] == receipt["session_id"]


def test_duplicate_main_fails_before_reset_or_discovery(tmp_path, monkeypatch):
    path, args = _bootstrap(tmp_path, monkeypatch)
    def forbid(*args, **kwargs):
        pytest.fail("a duplicate watch reached state-changing startup")
    monkeypatch.setattr(run_scan, "_reset_lifecycle_state", forbid)
    monkeypatch.setattr(run_scan, "_prepare_operational_runtime_if_needed", forbid)
    with WatchSession(path, interval_seconds=300):
        with pytest.raises(WatchSessionError, match="lease unavailable"):
            asyncio.run(run_scan.main(args + ["--reset-lifecycle", "--list-presets"]))


@pytest.mark.parametrize("cancel", [False, True])
def test_startup_failure_and_cancellation_are_distinct_and_release_lease(tmp_path, monkeypatch, cancel):
    path, args = _bootstrap(tmp_path, monkeypatch)
    async def fail(*args, **kwargs):
        raise asyncio.CancelledError() if cancel else RuntimeError("synthetic discovery failure")
    monkeypatch.setattr(run_scan, "_resolve_watchlist_for_args", fail)
    with pytest.raises(asyncio.CancelledError if cancel else RuntimeError):
        asyncio.run(run_scan.main(args))
    receipt = json.loads(Path(f"{path}.watch-session.json").read_text(encoding="utf-8"))
    assert receipt["shutdown_outcome"] == ("CANCELLED" if cancel else "STARTUP_FAILED")
    with WatchSession(path, interval_seconds=300):
        pass


def test_cancellation_swallowed_by_watch_loop_still_records_cancelled(tmp_path, monkeypatch):
    path, args = _bootstrap(tmp_path, monkeypatch)
    async def cancel(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(run_scan, "_attempt_watch_scan_iteration", cancel)
    asyncio.run(run_scan.main(args))
    receipt = json.loads(Path(f"{path}.watch-session.json").read_text(encoding="utf-8"))
    assert receipt["shutdown_outcome"] == "CANCELLED"
    assert receipt["latest_iteration"]["completion_observed"] is False


def test_reporting_tail_is_inside_finalization_duration_and_missed_slots(tmp_path, monkeypatch):
    path, args = _bootstrap(tmp_path, monkeypatch)
    args[-1] = "300"
    offset = [0.0]
    original_clock = time.monotonic
    monkeypatch.setattr(run_scan.time, "monotonic", lambda: original_clock() + offset[0])
    async def slow_report(*args, **kwargs):
        offset[0] += 650.0
    monkeypatch.setattr(run_scan, "_route_admin_report", slow_report)
    asyncio.run(run_scan.main(args))
    receipt = json.loads(Path(f"{path}.watch-session.json").read_text(encoding="utf-8"))
    latest = receipt["latest_iteration"]
    assert latest["duration_seconds"] >= 650
    assert latest["missed_interval_count"] == 2
    manifest = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert manifest["watch_supervisor"]["duration_seconds"] < 650
    assert "before_manifest_reporting" in manifest["watch_supervisor"]["duration_scope"]


def test_reporting_tail_failure_is_partial_in_final_receipt_and_console(tmp_path, monkeypatch, capsys):
    path, args = _bootstrap(tmp_path, monkeypatch)
    def fail(*args, **kwargs):
        raise OSError("synthetic watch output failure")
    monkeypatch.setattr(run_scan, "append_watch_output", fail)
    asyncio.run(run_scan.main(args + ["--watch-output-file", str(tmp_path / "output.jsonl")]))
    receipt = json.loads(Path(f"{path}.watch-session.json").read_text(encoding="utf-8"))
    latest = receipt["latest_iteration"]
    assert latest["phase_statuses"]["watch_output"] == "FAILED"
    assert latest["status"] == "PARTIAL" and latest["completion_observed"] is True
    assert latest["consecutive_failure_count"] == 1
    assert latest["next_attempt_requested"] is False and latest["sleep_seconds_at_observation"] == "N/A"
    assert "PARTIAL" in capsys.readouterr().out


def test_repeated_reporting_failures_keep_consecutive_failure_policy(tmp_path, monkeypatch):
    path, args = _bootstrap(tmp_path, monkeypatch)
    args[args.index("--watch-max-iterations") + 1] = "2"
    SequenceWatchRunner.results_by_call *= 2
    def fail(*args, **kwargs):
        raise OSError("synthetic watch output failure")
    monkeypatch.setattr(run_scan, "append_watch_output", fail)
    monkeypatch.setattr(run_scan, "failure_backoff_seconds", lambda streak: streak / 1000)
    asyncio.run(run_scan.main(args + ["--watch-output-file", str(tmp_path / "output.jsonl")]))
    rows = [json.loads(line) for line in Path(f"{path}.watch-observations.jsonl").read_text(encoding="utf-8").splitlines()]
    finalized = [row for row in rows if row.get("record_kind") == "watch_iteration_finalization_observation"]
    assert [row["status"] for row in finalized] == ["PARTIAL", "PARTIAL"]
    assert [row["consecutive_failure_count"] for row in finalized] == [1, 2]
    assert finalized[0]["selected_backoff_seconds"] == 0.001
    assert finalized[-1]["next_attempt_requested"] is False


def test_service_startup_and_cleanup_failures_preserve_both_and_try_every_stop(tmp_path, monkeypatch):
    path, args = _bootstrap(tmp_path, monkeypatch)
    calls = []
    class Service:
        def __init__(self, name, *, fail_start=False, fail_stop=False):
            self.name, self.fail_start, self.fail_stop = name, fail_start, fail_stop
        async def start(self, symbols):
            calls.append(f"start:{self.name}")
            if self.fail_start:
                raise RuntimeError("synthetic startup failure")
        async def stop(self):
            calls.append(f"stop:{self.name}")
            with pytest.raises(WatchSessionError, match="lease unavailable"):
                with WatchSession(path, interval_seconds=300):
                    pass
            if self.fail_stop:
                raise OSError("synthetic cleanup failure")
    monkeypatch.setattr(run_scan, "_microstructure_flow_service_for_config", lambda config: Service("flow", fail_stop=True))
    monkeypatch.setattr(run_scan, "_order_book_liquidity_service_for_config", lambda config: Service("book", fail_start=True))
    monkeypatch.setattr(run_scan, "_liquidation_flow_service_for_config", lambda config: Service("liquidation"))
    with pytest.raises(RuntimeError, match="synthetic startup failure.*synthetic cleanup failure"):
        asyncio.run(run_scan.main(args))
    assert calls == ["start:flow", "start:book", "stop:flow", "stop:book", "stop:liquidation"]
    receipt = json.loads(Path(f"{path}.watch-session.json").read_text(encoding="utf-8"))
    assert receipt["shutdown_outcome"] == "STARTUP_FAILED"
    with WatchSession(path, interval_seconds=300):
        pass
