from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from app import watch_session as sessions
from app.core.phase_profile import PhaseProfile
from app.watch_session import WatchSession, WatchSessionError, append_session_observation
from app.watch_supervisor import schedule_after_iteration
from scripts import run_scan
from tests.test_watch_session import START, _bootstrap
from tests.test_watch_mode import SequenceWatchRunner


def _writer(path):
    return lambda row: append_session_observation(path, row)


def test_exhaustion_is_latched_across_smaller_rows_and_restart_without_deleting_history(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    warnings = []
    monkeypatch.setattr(sessions, "MAX_JOURNAL_BYTES", 5000)
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path), on_advisory=warnings.append) as session:
        journal = Path(f"{path}.watch-observations.jsonl")
        original = journal.read_bytes()
        session._observe({"database_id": session.database_id, "record_kind": "synthetic_large", "padding": "x" * 6000})
        before = journal.read_bytes()
        assert before.startswith(original)
        assert json.loads(before.splitlines()[-1])["record_kind"] == "watch_journal_budget_exhausted"
        session._observe({"database_id": session.database_id, "record_kind": "synthetic_small"})
        assert journal.read_bytes() == before
        assert session.state["observation_journal"]["omitted_record_count"] == 2
        session.start_iteration(iteration=1, actual_start=START, scheduled_start=START)
    first_omitted = session.state["observation_journal"]["first_omitted_at"]
    assert len(warnings) == 1
    for _ in range(3):
        with WatchSession(path, interval_seconds=300, on_observation=_writer(path), on_advisory=warnings.append) as next_session:
            status = next_session.state["observation_journal"]
            assert status["status"] == "BUDGET_EXHAUSTED" and status["coverage_complete"] is False
            assert status["first_omitted_at"] == first_omitted
            assert status["omitted_record_count"] >= 4
            assert len(next_session.receipt_path.read_bytes()) < sessions.MAX_RECEIPT_BYTES
        assert journal.read_bytes() == before
    assert len(warnings) == 4
    assert not path.exists()


@pytest.mark.parametrize("damage", ["torn", "unknown", "missing"])
def test_exhaustion_does_not_hide_journal_corruption_or_recreate_missing_history(tmp_path, monkeypatch, damage):
    path = tmp_path / "synthetic.db"
    monkeypatch.setattr(sessions, "MAX_JOURNAL_BYTES", 5000)
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as session:
        session._observe({"database_id": session.database_id, "padding": "x" * 6000})
        session.start_iteration(iteration=1, actual_start=START, scheduled_start=START)
    journal = Path(f"{path}.watch-observations.jsonl")
    if damage == "missing":
        journal.unlink()  # This test owns this synthetic fixture only.
    elif damage == "unknown":
        journal.write_bytes(b"unknown\n")
    else:
        with journal.open("ab") as handle:
            handle.write(b"torn")
    before = journal.read_bytes() if journal.exists() else None
    with pytest.raises(WatchSessionError, match="refusing append"):
        with WatchSession(path, interval_seconds=300, on_observation=_writer(path)):
            pass
    assert (journal.read_bytes() if journal.exists() else None) == before


def test_advisory_failure_cannot_mask_scan_failure_or_break_finalization(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    monkeypatch.setattr(sessions, "MAX_JOURNAL_BYTES", 5000)
    def fail(_):
        raise OSError("synthetic closed console")
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path), on_advisory=fail) as session:
        session._observe({"database_id": session.database_id, "padding": "x" * 6000})
        session.start_iteration(iteration=1, actual_start=START, scheduled_start=START)
        decision = schedule_after_iteration(scheduled_start_monotonic=0, actual_start_monotonic=0, finished_monotonic=1, interval_seconds=300)
        session.finish_iteration(status="FAILED", phases={"scanner": "FAILED"}, schedule=decision)
    assert session.state["latest_iteration"]["status"] == "FAILED"
    assert session.state["shutdown_outcome"] == "COMPLETED"
    assert session.state["observation_journal"]["coverage_complete"] is False


def test_real_main_profiles_reporting_tail_and_keeps_capacity_advisory_out_of_business_status(tmp_path, monkeypatch):
    path, args = _bootstrap(tmp_path, monkeypatch)
    offset = [0.0]
    monkeypatch.setattr(run_scan, "PhaseProfile", lambda: PhaseProfile(lambda: offset[0]))
    monkeypatch.setattr(run_scan, "observe_local_resources", lambda: {"capacity_approval": False, "system_disk": {"floor_observation": "BELOW_PLANNING_FLOOR"}})
    async def tail(*args, **kwargs):
        offset[0] += 12.5
    monkeypatch.setattr(run_scan, "_route_admin_report", tail)
    output = tmp_path / "scan.json"
    asyncio.run(run_scan.main(args + ["--save-run", str(output)]))
    receipt = json.loads(Path(f"{path}.watch-session.json").read_text())
    latest = receipt["latest_iteration"]
    assert latest["status"] == "SUCCESS" and latest["consecutive_failure_count"] == 0
    assert latest["resource_observation"]["capacity_approval"] is False
    profile = latest["phase_profile"]
    assert profile["phases"]["admin_report"]["elapsed_seconds"] == profile["elapsed_seconds"] == 12.5
    assert "before_final_observation_persistence" in profile["boundary"]
    assert profile["phases"]["scan_manifest"]["calls"] == 1
    assert profile["phases"]["scanner"]["calls"] == 1
    stored = json.loads(output.read_text())["scanner_process_summary"]["phase_profile"]
    assert stored["boundary"] == "before_final_scan_json_output"
    assert stored["phases"]["scan_core"]["measurement_status"] == "IN_PROGRESS"
    assert stored["phases"]["admin_report"]["elapsed_seconds"] == "N/A"
    manifest = json.loads((tmp_path / "manifest.jsonl").read_text().splitlines()[-1])
    assert manifest["watch_supervisor"]["phase_profile"]["phases"]["admin_report"]["elapsed_seconds"] == "N/A"


def test_unexpected_resource_failure_is_unavailable_and_scan_continues(tmp_path, monkeypatch):
    path, args = _bootstrap(tmp_path, monkeypatch)
    def broken():
        raise RuntimeError("synthetic telemetry fault")
    monkeypatch.setattr(run_scan, "observe_local_resources", broken)
    asyncio.run(run_scan.main(args))
    latest = json.loads(Path(f"{path}.watch-session.json").read_text())["latest_iteration"]
    assert latest["status"] == "SUCCESS"
    assert latest["resource_observation"]["status"] == "Unverified"
    assert latest["resource_observation"]["observed_at"] == "N/A"


@pytest.mark.parametrize("failure", [False, True])
def test_main_warns_once_for_success_or_failed_iterations_and_keeps_current_receipt(tmp_path, monkeypatch, capsys, failure):
    path, args = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(sessions, "MAX_JOURNAL_BYTES", 1000)
    args[args.index("--watch-max-iterations") + 1] = "2"
    SequenceWatchRunner.results_by_call *= 2
    monkeypatch.setattr(run_scan, "failure_backoff_seconds", lambda _: 0)
    if failure:
        async def failed(*args, **kwargs):
            return None, None, RuntimeError("synthetic recoverable failure")
        monkeypatch.setattr(run_scan, "_attempt_watch_scan_iteration", failed)
    asyncio.run(run_scan.main(args))
    receipt = json.loads(Path(f"{path}.watch-session.json").read_text())
    assert receipt["latest_iteration"]["completion_observed"] is True
    assert receipt["latest_iteration"]["status"] == ("FAILED" if failure else "SUCCESS")
    assert receipt["observation_journal"]["status"] == "BUDGET_EXHAUSTED"
    assert receipt["observation_journal"]["coverage_complete"] is False
    assert receipt["observation_journal"]["bytes"] <= 1000
    assert capsys.readouterr().out.count("journal budget exhausted") == 1


def test_main_warns_during_startup_even_when_discovery_then_fails(tmp_path, monkeypatch, capsys):
    path, args = _bootstrap(tmp_path, monkeypatch)
    monkeypatch.setattr(sessions, "MAX_JOURNAL_BYTES", 5000)
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as prior:
        prior._observe({"database_id": prior.database_id, "padding": "x" * 6000})
        prior.start_iteration(iteration=1, actual_start=START, scheduled_start=START)
    async def fail(*args, **kwargs):
        raise RuntimeError("synthetic startup discovery failure")
    monkeypatch.setattr(run_scan, "_resolve_watchlist_for_args", fail)
    with pytest.raises(RuntimeError, match="startup discovery failure"):
        asyncio.run(run_scan.main(args))
    assert capsys.readouterr().out.count("journal budget exhausted") == 1
    receipt = json.loads(Path(f"{path}.watch-session.json").read_text())
    assert receipt["shutdown_outcome"] == "STARTUP_FAILED"
    assert receipt["observation_journal"]["coverage_complete"] is False


def test_abrupt_helper_exit_after_marker_fsync_preserves_suspension_with_stale_receipt(tmp_path):
    path = tmp_path / "synthetic.db"
    # This child owns only a synthetic lease/journal; it never starts the scanner.
    code = """
import os, sys
from app import watch_session as sessions
from app.watch_session import WatchSession, append_session_observation
sessions.MAX_JOURNAL_BYTES = 5000
path = sys.argv[1]
with WatchSession(path, interval_seconds=300, on_observation=lambda row: append_session_observation(path, row)) as session:
    result = append_session_observation(path, {'database_id': session.database_id, 'padding': 'x' * 6000})
    assert result['terminal_marker_present'] is True
    os._exit(23)
"""
    child = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True, text=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    assert child.returncode == 23, child.stderr
    stale = json.loads(Path(f"{path}.watch-session.json").read_text())
    assert stale["observation_journal"]["status"] == "APPENDED"
    journal = Path(f"{path}.watch-observations.jsonl")
    before = journal.read_bytes()
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as recovered:
        assert recovered.state["observation_journal"]["coverage_complete"] is False
        assert recovered.state["observation_journal"]["status"] == "BUDGET_EXHAUSTED"
        assert recovered.state["previous_session"]["shutdown_finalization_observed_at"] == "N/A"
    assert journal.read_bytes() == before


def test_exact_admission_keeps_terminal_reserve_and_long_ordinary_tail_is_valid(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as session:
        journal = Path(f"{path}.watch-observations.jsonl")
        row = {"database_id": session.database_id, "record_kind": "synthetic", "padding": "x" * 2000}
        payload_bytes = len(json.dumps({"contract": sessions.CONTRACT, **row}, sort_keys=True, ensure_ascii=False).encode()) + 1
        cap = journal.stat().st_size + payload_bytes + sessions.JOURNAL_TERMINAL_RESERVE_BYTES
        monkeypatch.setattr(sessions, "MAX_JOURNAL_BYTES", cap)
        first = append_session_observation(path, row)
        assert first["row_appended"] is True
        assert journal.stat().st_size == cap - sessions.JOURNAL_TERMINAL_RESERVE_BYTES
        second = append_session_observation(path, {"database_id": session.database_id})
        assert second["status"] == "BUDGET_EXHAUSTED" and second["terminal_marker_present"] is True
        assert journal.stat().st_size <= cap


def test_legacy_near_cap_without_room_for_marker_remains_suspended(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as session:
        journal = Path(f"{path}.watch-observations.jsonl")
        size = journal.stat().st_size
        monkeypatch.setattr(sessions, "MAX_JOURNAL_BYTES", size + 10)
        result = append_session_observation(path, {"database_id": session.database_id})
        assert result["status"] == "BUDGET_EXHAUSTED" and result["terminal_marker_present"] is False
        assert journal.stat().st_size == size
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as restarted:
        assert restarted.state["observation_journal"]["status"] == "BUDGET_EXHAUSTED"
    assert journal.stat().st_size == size


def test_torn_terminal_and_failed_terminal_fsync_are_integrity_errors(tmp_path, monkeypatch):
    path = tmp_path / "synthetic.db"
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as session:
        monkeypatch.setattr(sessions, "MAX_JOURNAL_BYTES", 5000)
        def fail(_):
            raise OSError("synthetic marker fsync failure")
        with monkeypatch.context() as local:
            local.setattr(sessions.os, "fsync", fail)
            with pytest.raises(OSError, match="marker fsync"):
                append_session_observation(path, {"database_id": session.database_id, "padding": "x" * 6000})
    journal = Path(f"{path}.watch-observations.jsonl")
    journal.write_bytes(journal.read_bytes()[:-1])  # Owned synthetic torn marker.
    before = journal.read_bytes()
    with pytest.raises(WatchSessionError, match="refusing append"):
        with WatchSession(path, interval_seconds=300, on_observation=_writer(path)):
            pass
    assert journal.read_bytes() == before


def test_corrupt_budget_metadata_cannot_authorize_reset(tmp_path):
    path = tmp_path / "synthetic.db"
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as session:
        pass
    receipt = json.loads(session.receipt_path.read_text())
    receipt["observation_journal"]["omitted_record_count"] = True
    session.receipt_path.write_text(json.dumps(receipt))
    before = session.receipt_path.read_bytes()
    with pytest.raises(WatchSessionError, match="refusing overwrite"):
        with WatchSession(path, interval_seconds=300, on_observation=_writer(path)):
            pass
    assert session.receipt_path.read_bytes() == before


@pytest.mark.parametrize("restart", [False, True])
def test_established_nonexhausted_journal_is_never_recreated_when_missing(tmp_path, restart):
    path = tmp_path / "synthetic.db"
    journal = Path(f"{path}.watch-observations.jsonl")
    if restart:
        with WatchSession(path, interval_seconds=300, on_observation=_writer(path)):
            pass
        journal.unlink()  # Owned fixture simulates externally lost evidence.
        with pytest.raises(WatchSessionError, match="refusing append"):
            with WatchSession(path, interval_seconds=300, on_observation=_writer(path)):
                pass
    else:
        with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as session:
            journal.unlink()
            with pytest.raises(WatchSessionError, match="refusing append"):
                session._observe({"database_id": session.database_id, "record_kind": "synthetic"})
    assert not journal.exists()


def test_unconfigured_session_does_not_erase_established_history_knowledge(tmp_path):
    path = tmp_path / "synthetic.db"
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)):
        pass
    with WatchSession(path, interval_seconds=300) as unconfigured:
        assert unconfigured.state["journal_established"] is True
    Path(f"{path}.watch-observations.jsonl").unlink()
    with pytest.raises(WatchSessionError, match="refusing append"):
        with WatchSession(path, interval_seconds=300, on_observation=_writer(path)):
            pass


def test_first_configuration_after_known_unconfigured_session_initializes_journal(tmp_path):
    path = tmp_path / "synthetic.db"
    with WatchSession(path, interval_seconds=300) as unconfigured:
        assert unconfigured.state["journal_established"] is False
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as configured:
        assert configured.state["journal_established"] is True
        assert configured.state["observation_journal"]["status"] == "APPENDED"


@pytest.mark.parametrize("missing", [False, True])
def test_legacy_receipt_requires_existing_journal_without_silent_reset(tmp_path, missing):
    path = tmp_path / "synthetic.db"
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as session:
        pass
    legacy = json.loads(session.receipt_path.read_text())
    legacy.pop("journal_established")
    legacy.pop("observation_journal")
    session.receipt_path.write_text(json.dumps(legacy))
    journal = Path(f"{path}.watch-observations.jsonl")
    if missing:
        journal.unlink()
        with pytest.raises(WatchSessionError, match="refusing append"):
            with WatchSession(path, interval_seconds=300, on_observation=_writer(path)):
                pass
        assert not journal.exists()
    else:
        before = journal.read_bytes()
        with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as resumed:
            assert resumed.state["journal_established"] is True
        assert journal.read_bytes().startswith(before)


def test_contradictory_established_flag_cannot_recreate_history(tmp_path):
    path = tmp_path / "synthetic.db"
    with WatchSession(path, interval_seconds=300, on_observation=_writer(path)) as session:
        pass
    receipt = json.loads(session.receipt_path.read_text())
    receipt["journal_established"] = False
    session.receipt_path.write_text(json.dumps(receipt))
    Path(f"{path}.watch-observations.jsonl").unlink()
    before = session.receipt_path.read_bytes()
    with pytest.raises(WatchSessionError, match="refusing overwrite"):
        with WatchSession(path, interval_seconds=300, on_observation=_writer(path)):
            pass
    assert session.receipt_path.read_bytes() == before
