"""Monitoring coverage reports retain owners even when their evaluation fails."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.lifecycle import owner_monitoring
from app.lifecycle.owner_monitoring import monitor_tracking_obligations
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.monitoring_diagnostics import summarize_monitoring_passes
from app.lifecycle.models import SetupLifecycleState
from app.storage.scan_payloads import load_logical_scan_payload
from scripts import run_scan
from tests.test_p5a_active_owner_monitoring import (
    TIMEFRAME, _continuation_candles, _decision, _latched, _progress, _rejected_result, _seed_entered,
)
from tests.test_p5a_monitoring_binding_query_repair import _KlineClient, _quiet_runtime
from tests.test_watch_mode import SequenceWatchRunner, _bootstrap_watch_main


def test_evaluator_failure_stays_in_owner_denominator(tmp_path, monkeypatch):
    path = tmp_path / "owners.db"
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)
        before = _progress(repository, owner.lifecycle_id)

        def fail(*args, **kwargs):
            raise RuntimeError("evaluation failed")

        monkeypatch.setattr(owner_monitoring, "evaluate_closed_candle_outcomes", fail)
        result = monitor_tracking_obligations(
            repository,
            evidence_by_key={owner_monitoring.evidence_key(owner.symbol, TIMEFRAME):
                owner_monitoring.SymbolMonitoringEvidence(
                    candles=tuple(_continuation_candles()), execution_timeframe=TIMEFRAME,
                    decision_timestamp=_decision(2),
                )},
            evaluated_at=_decision(2), scan_run_id="synthetic-failure",
        )
        after = _progress(repository, owner.lifecycle_id)
    report = result.as_dict()
    assert report["obligation_count"] == 1
    assert report["owner_dispositions"][0]["disposition"] == "ERROR"
    assert report["verified_evaluation_count"] == 0
    assert report["all_obligations_verified"] is False
    assert before == after


@pytest.mark.parametrize("watch", [False, True])
def test_real_scan_persists_out_of_discovery_monitoring(tmp_path, monkeypatch, watch):
    _quiet_runtime(monkeypatch, tmp_path)
    path, _ = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository, symbol="CAKEUSDT")
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [
        (_rejected_result("BTCUSDT", tuple(_continuation_candles()), mode="scalp", bias=None),)
    ]
    client = _KlineClient()
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: client)
    monkeypatch.setattr(run_scan, "_watch_iteration_timestamp", lambda: _decision(2))
    output = tmp_path / "scan.json"
    args = ["--symbols", "BTCUSDT", "--database-path", str(path), "--lifecycle",
            "--execution-timeframe", TIMEFRAME, "--output-json", str(output), "--store-scan"]
    if watch:
        args += ["--watch", "--watch-max-iterations", "1", "--watch-interval-sec", "0.01"]
    asyncio.run(run_scan.main(args))
    report = json.loads(output.read_text(encoding="utf-8"))["scanner_process_summary"]["owner_monitoring"]
    assert client.calls == ["CAKEUSDT"]
    assert report["obligation_count"] == 1
    assert report["evaluated_lifecycle_ids"] == [owner.lifecycle_id]
    assert report["uncovered_symbols"] == []
    assert report["verified_evaluation_count"] == 1
    assert report["historical_completeness"] == "Unverified"
    assert len(report["passes"]) == 2
    manifest = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert manifest["owner_monitoring"] == report
    with sqlite3.connect(path) as connection:
        run_id = connection.execute("SELECT run_id FROM scan_runs").fetchone()[0]
        stored = load_logical_scan_payload(connection, run_id)
    assert stored["scanner_process_summary"]["owner_monitoring"] == report


def test_shared_symbol_gap_and_terminal_owners_are_counted_honestly(tmp_path):
    with SQLiteSetupLifecycleRepository(tmp_path / "gaps.db") as repository:
        first = _seed_entered(repository, lifecycle_id="scalp", mode="scalp")
        second = _seed_entered(repository, lifecycle_id="swing", mode="swing")
        repository.upsert_record(_latched(
            lifecycle_id="terminal", state=SetupLifecycleState.SL_HIT, is_current=False,
        ))
        before = [_progress(repository, record.lifecycle_id) for record in (first, second)]
        result = monitor_tracking_obligations(
            repository, evidence_by_key={}, evaluated_at=_decision(2),
            scan_run_id="synthetic-missing", record_missing_evidence=True,
        )
        after = [_progress(repository, record.lifecycle_id) for record in (first, second)]
    report = result.as_dict()
    assert report["obligation_count"] == 2
    assert report["disposition_counts"] == {"EVIDENCE_GAP": 2}
    assert report["all_obligations_verified"] is False
    assert report["evaluated_lifecycle_ids"] == []
    for old, new in zip(before, after):
        assert new.evaluation_cursor_close_at == old.evaluation_cursor_close_at
        assert new.plan_version_id == old.plan_version_id
        assert new.terminal_outcome == old.terminal_outcome


def test_fetch_failure_preserves_owner_identity_and_denominator(tmp_path):
    path = tmp_path / "fetch.db"
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)

    async def fail(*args):
        raise RuntimeError("adapter failure")

    report = asyncio.run(owner_monitoring.monitor_obligations_with_market_data(
        path, evidence_by_key={}, fetch_candles=fail, execution_timeframe=TIMEFRAME,
        candle_limit=50, evaluated_at=_decision(2), scan_run_id="synthetic-fetch",
    )).as_dict()
    assert report["obligation_count"] == 1
    row = report["owner_dispositions"][0]
    assert row["lifecycle_id"] == owner.lifecycle_id
    assert row["plan_version_id"] == owner.plan_version_id
    assert row["disposition"] == "ERROR"
    assert "adapter failure" in row["diagnostic"]


def test_retry_diagnostics_keep_prior_errors_and_do_not_double_count():
    first = {
        "obligation_lifecycle_ids": ["owner", "sibling"],
        "errors": [{"lifecycle_id": "owner", "symbol": "CAKEUSDT", "detail": "failed once"}],
        "verified_lifecycle_ids": ["sibling"], "evaluated_lifecycle_ids": ["sibling"],
        "lags": [{"lifecycle_id": item, "symbol": "CAKEUSDT"} for item in ("owner", "sibling")],
    }
    second = {
        "obligation_lifecycle_ids": ["owner", "sibling"],
        "skipped_lifecycle_ids": ["sibling"],
        "verified_lifecycle_ids": ["owner"], "evaluated_lifecycle_ids": ["owner"],
        "lags": [{"lifecycle_id": "owner", "symbol": "CAKEUSDT"}],
    }
    report = summarize_monitoring_passes([first, second], retain_passes=True)
    assert report["obligation_count"] == 2
    assert report["verified_evaluation_count"] == 2
    assert report["errors"] == first["errors"]
    assert report["all_obligations_verified"] is False
    assert report["uncovered_symbols"] == []
    assert len(report["passes"]) == 2
    assert summarize_monitoring_passes([])["all_obligations_verified"] is None


def test_gap_persistence_failure_is_an_error_not_verified_coverage(tmp_path, monkeypatch):
    with SQLiteSetupLifecycleRepository(tmp_path / "write-failure.db") as repository:
        owner = _seed_entered(repository)
        before = _progress(repository, owner.lifecycle_id)

        def fail(*args, **kwargs):
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(owner_monitoring, "record_closed_candle_evidence_gap", fail)
        report = monitor_tracking_obligations(
            repository, evidence_by_key={}, evaluated_at=_decision(2),
            scan_run_id="synthetic-write-failure", record_missing_evidence=True,
        ).as_dict()
        assert _progress(repository, owner.lifecycle_id) == before
    assert report["disposition_counts"] == {"ERROR": 1}
    assert report["all_obligations_verified"] is False


def test_discovery_failure_retains_monitoring_report_in_failed_manifest(tmp_path, monkeypatch):
    from tests.test_p5a_monitoring_failure_visibility import _discovery_exit, _summary

    _quiet_runtime(monkeypatch, tmp_path)
    path, _ = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: _KlineClient())
    monkeypatch.setattr(run_scan, "_watch_iteration_timestamp", lambda: _decision(2))
    discovery = _discovery_exit()
    args = run_scan.parse_args(["--symbols", "BTCUSDT", "--lifecycle", "--database-path", str(path),
                               "--execution-timeframe", TIMEFRAME])
    asyncio.run(run_scan._surface_monitoring_during_discovery_failure(args, discovery))
    startup = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert startup["record_kind"] == "discovery_failure_monitoring_diagnostic"
    assert startup["scan_completion_record"] is False
    assert startup["owner_monitoring"]["evaluated_lifecycle_ids"] == [owner.lifecycle_id]
    summary = _summary(discovery)
    from types import SimpleNamespace
    run_scan._record_failed_watch_iteration(
        args, summary, watchlist=SimpleNamespace(universe=None, source_label="synthetic"),
    )
    row = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["status"] == "FAILED"
    assert row["owner_monitoring"]["evaluated_lifecycle_ids"] == [owner.lifecycle_id]
    assert row["watch_supervisor"]["owner_monitoring"] == row["owner_monitoring"]


@pytest.mark.parametrize("continuation_raises", [False, True])
@pytest.mark.parametrize("watch", [False, True])
def test_monitoring_failure_has_durable_partial_diagnostics(
    tmp_path, monkeypatch, continuation_raises, watch,
):
    _quiet_runtime(monkeypatch, tmp_path)
    path, _ = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)
        before = _progress(repository, owner.lifecycle_id)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [
        (_rejected_result("BTCUSDT", tuple(_continuation_candles()), mode="scalp", bias=None),)
    ]
    if continuation_raises:
        async def fail(*args, **kwargs):
            raise RuntimeError("synthetic continuation failure")
        monkeypatch.setattr(run_scan, "_continue_owned_plan_monitoring", fail)
    else:
        monkeypatch.setattr(run_scan, "_owner_monitoring_client",
                            lambda args: _KlineClient(error=RuntimeError("synthetic adapter failure")))
    monkeypatch.setattr(run_scan, "_watch_iteration_timestamp", lambda: _decision(2))

    async def forbid_delivery(*args, **kwargs):
        raise AssertionError("one-shot failure must exit before delivery")

    if not watch:
        monkeypatch.setattr(run_scan, "_deliver_telegram_manual_signals_if_enabled", forbid_delivery)
    args = ["--symbols", "BTCUSDT", "--database-path", str(path), "--lifecycle",
            "--execution-timeframe", TIMEFRAME]
    if watch:
        args += ["--watch", "--watch-max-iterations", "1", "--watch-interval-sec", "0.01"]
        asyncio.run(run_scan.main(args))
    else:
        with pytest.raises(SystemExit, match="synthetic"):
            asyncio.run(run_scan.main(args))
    row = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    report = row["owner_monitoring"]
    assert report["obligation_count"] == 1
    assert report["all_obligations_verified"] is False
    assert row.get("status", row.get("watch_supervisor", {}).get("status")) == "PARTIAL"
    if continuation_raises:
        assert report["continuation_status"] == "ERROR"
        assert "synthetic continuation failure" in report["continuation_error"]
    else:
        assert report["owner_dispositions"][0]["disposition"] == "ERROR"
        assert "synthetic adapter failure" in report["errors"][0]["detail"]
    with SQLiteSetupLifecycleRepository(path) as repository:
        assert _progress(repository, owner.lifecycle_id) == before


def test_failed_diagnostic_write_keeps_original_error(tmp_path, monkeypatch):
    from tests.test_p5a_active_owner_monitoring import _run

    monkeypatch.setattr(run_scan, "SCAN_RUN_MANIFEST_PATH", tmp_path)
    result = _run(_rejected_result("BTCUSDT", (), mode="scalp", bias=None))
    failure = RuntimeError("original monitoring failure")
    result = run_scan._retain_owner_monitoring_failure(result, failure)
    assert result.scanner_process_summary["owner_monitoring"]["obligation_count"] is None
    with pytest.raises(SystemExit) as raised:
        run_scan._persist_one_shot_monitoring_failure(result, exc=failure)
    assert "original monitoring failure" in str(raised.value)
    assert "owner_monitoring_diagnostic_persistence" in str(raised.value)


@pytest.mark.parametrize("startup", [False, True])
def test_discovery_and_monitoring_exception_persist_unknown_coverage(tmp_path, monkeypatch, startup):
    from types import SimpleNamespace
    from tests.test_p5a_monitoring_failure_visibility import _discovery_exit, _summary

    _quiet_runtime(monkeypatch, tmp_path)

    async def fail(*args, **kwargs):
        raise sqlite3.OperationalError("synthetic monitoring database unavailable")

    monkeypatch.setattr(run_scan, "_continue_owned_plan_monitoring", fail)
    discovery = _discovery_exit()
    asyncio.run(run_scan._surface_monitoring_during_discovery_failure(
        SimpleNamespace(), discovery, publish_on_system_exit=startup,
    ))
    if not startup:
        run_scan._record_failed_watch_iteration(
            SimpleNamespace(watch_output_file=None), _summary(discovery),
            watchlist=SimpleNamespace(universe=None, source_label="synthetic"),
        )
    row = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["status"] == "FAILED"
    report = row["owner_monitoring"]
    assert report["obligation_count"] is None
    assert report["owner_accounting_status"] == "N/A"
    assert report["all_obligations_verified"] is False
    assert report["continuation_status"] == "ERROR"
    assert "synthetic monitoring database unavailable" in report["continuation_error"]
    assert any("universe_error" in item for item in row.get("errors", row.get("watch_supervisor", {}).get("errors", ())))
