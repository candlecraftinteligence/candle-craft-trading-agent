"""Synthetic provenance evidence; no real transport or operational database."""

from __future__ import annotations

import asyncio
import json
import sqlite3

import pytest

from app.alerts import provenance_diagnostics as diagnostics
from app.alerts.telegram_lifecycle import SQLiteTelegramAlertAttemptRepository, TelegramLifecycleDeliverySummary
from app.storage.scan_payloads import load_logical_scan_payload
from app.storage.database import StorageError
from app.watch_iteration import telegram_outbox_phase_status, telegram_outbox_status_summary
from scripts import run_scan
from tests.test_telegram_outbox_recovery import _seed_intent, EVENT_KEY
from test_telegram_lifecycle_delivery_phase42 import FakeSender, _empty_run_result, _run_result
from test_triggered_confirmed_telegram_delivery import _service, _public_setup_symbol
from tests.test_watch_mode import SequenceWatchRunner, _bootstrap_watch_main
from tests.test_p5a_monitoring_binding_query_repair import _quiet_runtime
from tests.test_p5a_active_owner_monitoring import _rejected_result, _continuation_candles


@pytest.fixture(autouse=True)
def clean_warning_cache():
    diagnostics._WARNED.clear()


def _snapshot(connection):
    return {table: [tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id")]
            for table in ("public_alert_events", "telegram_alert_attempts", "public_alert_delivery_parts")}


def _collect(path, **kwargs):
    with SQLiteTelegramAlertAttemptRepository(path) as repository:
        before = _snapshot(repository._connection)
        report = diagnostics.collect_provenance_diagnostics(repository._connection, database_path=path, **kwargs)
        assert _snapshot(repository._connection) == before
    return report


@pytest.mark.parametrize("corruption,reason", [
    ("missing", "public_canonical_reservation_missing"),
    ("dangling", "public_reservation_missing"),
    ("event_key", "public_reservation_event_mismatch"),
    ("symbol", "public_reservation_symbol_mismatch"),
    ("family", "public_reservation_event_family_mismatch"),
    ("plan", "public_reservation_plan_mismatch"),
])
def test_malformed_canonical_is_read_only_and_attributed(tmp_path, corruption, reason):
    path = tmp_path / "malformed.db"
    event_id, attempt_id = _seed_intent(path)
    with sqlite3.connect(path) as connection:
        if corruption in {"missing", "dangling"}:
            connection.execute("UPDATE public_alert_events SET canonical_reservation_attempt_id = ? WHERE id = ?",
                               (None if corruption == "missing" else 999999, event_id))
        else:
            column, value = {
                "event_key": ("public_watchlist_event_key", "wrong-event"),
                "symbol": ("symbol", "ETHUSDT"),
                "family": ("public_alert_event_type", "tp1_hit"),
                "plan": ("public_watchlist_plan_id", "wrong-plan"),
            }[corruption]
            connection.execute(f"UPDATE telegram_alert_attempts SET {column} = ? WHERE id = ?", (value, attempt_id))
    report = _collect(path)
    assert report["coverage_complete"] is True
    assert report["unresolved"] == 1
    issue = report["details"][0]
    assert issue["event_key"] == EVENT_KEY
    assert issue["event_id"] == event_id
    assert issue["reservation_reason"] == reason
    assert issue["disposition"] == "UNRESOLVED_NO_SEND"
    assert issue["historical_http_delivery"] == "Unverified"
    assert "payload_text" not in issue and "destination_chat_id" not in issue


def test_ownership_denial_is_separate_from_missing_canonical(tmp_path):
    path = tmp_path / "legacy.db"
    _seed_intent(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE public_alert_events SET runtime_epoch_id = NULL, canonical_reservation_attempt_id = NULL")
    issue = _collect(path)["details"][0]
    assert issue["ownership_reason"] == "legacy_or_unattributed_public_event"
    assert issue["reservation_reason"] == "public_canonical_reservation_missing"


def test_valid_canonical_does_not_imply_historical_delivery(tmp_path):
    path = tmp_path / "valid.db"
    _seed_intent(path)
    report = _collect(path)
    assert report["inspected"] == 1
    assert report["unresolved"] == 0 and report["details"] == []
    assert report["historical_completeness"] == "Unverified"


def test_warning_repeats_are_suppressed_but_current_evidence_remains(tmp_path, caplog):
    path = tmp_path / "warnings.db"
    _seed_intent(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE public_alert_events SET canonical_reservation_attempt_id = NULL")
    first = _collect(path)
    second = _collect(path)
    assert first["warnings_emitted"] == 1
    assert second["warnings_emitted"] == 0 and second["warnings_suppressed"] == 1
    assert first["details"] == second["details"]
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE public_alert_events SET canonical_reservation_attempt_id = 999999")
    changed = _collect(path)
    assert changed["warnings_emitted"] == 1
    assert len([row for row in caplog.records if "Unresolved public provenance" in row.message]) == 2


def _add_unresolved(connection, count):
    columns = [row[1] for row in connection.execute("PRAGMA table_info(public_alert_events)") if row[1] != "id"]
    template = dict(zip(columns, connection.execute(f"SELECT {','.join(columns)} FROM public_alert_events LIMIT 1").fetchone()))
    for index in range(count):
        row = {**template, "event_key": f"synthetic-unresolved-{index}", "canonical_reservation_attempt_id": None}
        connection.execute(f"INSERT INTO public_alert_events ({','.join(columns)}) VALUES ({','.join('?' for _ in columns)})",
                           tuple(row[name] for name in columns))


def test_inspection_details_and_warning_growth_are_bounded(tmp_path, monkeypatch):
    path = tmp_path / "bounded.db"
    _seed_intent(path)
    with sqlite3.connect(path) as connection:
        _add_unresolved(connection, 70)
    monkeypatch.setattr(diagnostics, "WARNING_CACHE_LIMIT", 3)
    report = _collect(path, inspection_limit=65, detail_limit=2)
    assert report["inspected"] == 65  # exercises more than one SQL page
    assert report["coverage_complete"] is False
    assert report["unresolved"] == 64 and report["omitted_details"] == 62
    assert len(report["details"]) == 2
    assert report["warnings_emitted"] == diagnostics.WARNING_BUDGET
    assert len(diagnostics._WARNED) <= 3
    repeat = _collect(path, inspection_limit=65, detail_limit=2)
    assert repeat["warnings_emitted"] <= diagnostics.WARNING_BUDGET
    assert telegram_outbox_phase_status(telegram_outbox_status_summary(
        TelegramLifecycleDeliverySummary(provenance_diagnostics=report))) == "PARTIAL"


def test_unavailable_is_not_observed_zero():
    with sqlite3.connect(":memory:") as connection:
        connection.row_factory = sqlite3.Row
        report = diagnostics.collect_provenance_diagnostics(connection, database_path="synthetic.db")
    assert report["status"] == "UNAVAILABLE" and report["coverage_complete"] is False
    assert report["unresolved"] == "N/A"
    assert telegram_outbox_phase_status(telegram_outbox_status_summary(
        TelegramLifecycleDeliverySummary(provenance_diagnostics=report))) == "PARTIAL"


def test_newer_valid_recovery_is_not_starved_and_unresolved_events_are_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "recovery.db"
    _seed_intent(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE public_alert_events SET canonical_reservation_attempt_id = NULL")
        _add_unresolved(connection, 4)
        before = _snapshot(connection)
    sender = FakeSender()
    service = _service(path, sender)
    symbol = _public_setup_symbol(signal_id="new-valid-root")
    assert asyncio.run(service.deliver_for_run(_run_result(symbol))).sent == 1
    sender.messages.clear()
    with sqlite3.connect(path) as connection:
        event_id = connection.execute("SELECT MAX(id) FROM public_alert_events").fetchone()[0]
        connection.execute("UPDATE public_alert_events SET delivery_state='PENDING', status='RESERVED', attempt_count=0 WHERE id=?", (event_id,))
        connection.execute("UPDATE public_alert_delivery_parts SET delivery_state='PENDING' WHERE public_alert_event_id=?", (event_id,))
    original = diagnostics.collect_provenance_diagnostics
    monkeypatch.setattr("app.alerts.telegram_lifecycle.collect_provenance_diagnostics",
                        lambda connection, **kwargs: original(connection, **kwargs, inspection_limit=2))
    result = asyncio.run(_service(path, sender).deliver_for_run(_empty_run_result()))
    assert result.sent == 1 and len(sender.messages) == 1
    assert result.provenance_diagnostics["coverage_complete"] is False
    assert result.provenance_diagnostics["unresolved"] == 2
    repeated = asyncio.run(_service(path, sender).deliver_for_run(_empty_run_result()))
    assert repeated.sent == 0 and len(sender.messages) == 1
    with sqlite3.connect(path) as connection:
        after = _snapshot(connection)
    for table in before:
        assert after[table][:len(before[table])] == before[table]


def test_missing_confirmed_canonical_never_uses_event_key_fallback(tmp_path):
    path = tmp_path / "missing-root.db"
    sender = FakeSender()
    service = _service(path, sender)
    asyncio.run(service.deliver_for_run(_run_result(_public_setup_symbol(signal_id="missing-canonical"))))
    sender.messages.clear()
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE public_alert_events SET status='RESERVED', delivery_state='PENDING', canonical_reservation_attempt_id=NULL")
        connection.execute("UPDATE telegram_alert_attempts SET telegram_status='pending', delivery_state='PENDING'")
        before = _snapshot(connection)
    result = asyncio.run(_service(path, sender).deliver_for_run(_empty_run_result()))
    assert sender.messages == []
    assert result.attempted == 0 and result.sent == 0
    assert result.provenance_diagnostics["unresolved"] == 1
    with sqlite3.connect(path) as connection:
        assert _snapshot(connection) == before


@pytest.mark.parametrize("watch", [False, True])
def test_scan_retains_provenance_in_all_durable_outputs(tmp_path, monkeypatch, watch):
    _quiet_runtime(monkeypatch, tmp_path)
    path, _ = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_rejected_result("BTCUSDT", tuple(_continuation_candles()), mode="scalp", bias=None),)]
    report = {"status": "OBSERVED", "coverage_complete": True, "unresolved": 1,
              "details": [{"event_key": "synthetic-unresolved", "reservation_reason": "public_canonical_reservation_missing"}]}
    async def delivery(*args, **kwargs):
        return TelegramLifecycleDeliverySummary(provenance_diagnostics=report)
    monkeypatch.setattr(run_scan, "_deliver_telegram_manual_signals_if_enabled", delivery)
    output, watch_output = tmp_path / "scan.json", tmp_path / "watch.jsonl"
    args = ["--symbols", "BTCUSDT", "--database-path", str(path), "--telegram-live-alerts",
            "--output-json", str(output), "--store-scan"]
    if watch:
        args += ["--watch", "--watch-max-iterations", "1", "--watch-interval-sec", "0.01", "--watch-output-file", str(watch_output)]
    asyncio.run(run_scan.main(args))
    assert json.loads(output.read_text(encoding="utf-8"))["scanner_process_summary"]["telegram_provenance"] == report
    manifest = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert manifest["telegram_provenance"] == report and manifest["status"] == "PARTIAL"
    with sqlite3.connect(path) as connection:
        run_id = connection.execute("SELECT run_id FROM scan_runs").fetchone()[0]
        stored = load_logical_scan_payload(connection, run_id)
    assert stored["scanner_process_summary"]["telegram_provenance"] == report
    if watch:
        summary = json.loads(watch_output.read_text(encoding="utf-8").splitlines()[-1])
        assert summary["telegram_provenance"] == report and summary["phase_statuses"]["telegram_outbox"] == "PARTIAL"


def test_disabled_delivery_is_explicitly_not_requested():
    result = run_scan._retain_telegram_provenance(_empty_run_result(), None, requested=False)
    report = result.scanner_process_summary["telegram_provenance"]
    assert report["status"] == "NOT_REQUESTED" and report["unresolved"] == "N/A"
    assert report["coverage_complete"] is None
    assert telegram_outbox_phase_status(telegram_outbox_status_summary(None)) == "PARTIAL"


@pytest.mark.parametrize("event_type", ["invalidated", "expired", "no_longer_tracking"])
def test_diagnostic_families_do_not_expand_send_recovery(tmp_path, event_type):
    path = tmp_path / "diagnostic-family.db"
    _seed_intent(path)
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE public_alert_events SET event_type=?, canonical_reservation_attempt_id=NULL", (event_type,))
        before = _snapshot(connection)
    sender = FakeSender()
    result = asyncio.run(_service(path, sender).deliver_for_run(_empty_run_result()))
    assert result.provenance_diagnostics["details"][0]["event_type"] == event_type
    assert result.provenance_diagnostics["unresolved"] == 1
    assert result.attempted == 0 and sender.messages == []
    with sqlite3.connect(path) as connection:
        assert _snapshot(connection) == before


@pytest.mark.parametrize("watch", [False, True])
@pytest.mark.parametrize("fatal", [False, True])
def test_delivery_failure_persists_unavailable_coverage(tmp_path, monkeypatch, watch, fatal):
    _quiet_runtime(monkeypatch, tmp_path)
    path, _ = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_rejected_result("BTCUSDT", tuple(_continuation_candles()), mode="scalp", bias=None),)]
    async def fail(*args, **kwargs):
        raise (StorageError if fatal else RuntimeError)("synthetic delivery failure")
    monkeypatch.setattr(run_scan, "_deliver_telegram_manual_signals_if_enabled", fail)
    args = ["--symbols", "BTCUSDT", "--database-path", str(path), "--telegram-live-alerts"]
    if watch:
        args += ["--watch", "--watch-max-iterations", "1", "--watch-interval-sec", "0.01"]
        if fatal:
            with pytest.raises(StorageError, match="synthetic delivery failure"):
                asyncio.run(run_scan.main(args))
        else:
            asyncio.run(run_scan.main(args))
    else:
        with pytest.raises(StorageError if fatal else RuntimeError, match="synthetic delivery failure"):
            asyncio.run(run_scan.main(args))
    row = json.loads((tmp_path / "manifest.jsonl").read_text(encoding="utf-8").splitlines()[-1])
    assert row["status"] == ("FATAL" if watch and fatal else "PARTIAL")
    assert row["telegram_provenance"]["status"] == "UNAVAILABLE"
    assert row["telegram_provenance"]["unresolved"] == "N/A"
    if not watch:
        assert row["scan_completion_record"] is False
        assert "synthetic delivery failure" in row["errors"][0]


def test_failure_to_persist_diagnostics_preserves_original_failure(tmp_path, monkeypatch):
    blocked = tmp_path / "blocked"
    blocked.write_text("synthetic non-directory", encoding="utf-8")
    monkeypatch.setattr(run_scan, "SCAN_RUN_MANIFEST_PATH", blocked / "manifest.jsonl")
    with pytest.raises(SystemExit) as failure:
        run_scan._persist_one_shot_telegram_failure(_empty_run_result(), RuntimeError("original failure"))
    assert "original failure" in str(failure.value)
    assert "telegram_provenance_diagnostic_persistence" in str(failure.value)
