from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.watch_retry_policy import watch_backoff_eligibility
from app.watch_supervisor import schedule_after_iteration
from scripts import run_scan
from tests.test_watch_session import _bootstrap
from tests.test_watch_mode import SequenceWatchRunner


def _summary(*, complete=True, unresolved=10, **changes):
    report = {"contract": "cci-public-provenance-diagnostics-v1", "status": "OBSERVED",
              "coverage_complete": complete, "unresolved": unresolved,
              "inspected": 10 if complete else 256, "inspection_limit": 256}
    report.update(changes)
    return SimpleNamespace(failed=0, deliveries=(), provenance_diagnostics=report)


def _execution(summary=None):
    summary = summary or _summary()
    return SimpleNamespace(telegram_delivery_summary=summary,
                           telegram_outbox_status=run_scan._telegram_outbox_status_summary(summary))


def test_real_functions_keep_provenance_partial_without_540_second_start_regression():
    execution = _execution()
    phases = {"scanner": "SUCCESS", "telegram_outbox": run_scan._telegram_outbox_phase_status(execution.telegram_outbox_status)}
    assert run_scan._iteration_status_from_phases(phases) == "PARTIAL"
    failures, backoff, policy = run_scan._watch_retry_policy(execution, phases, 10, True)
    assert failures == backoff == 0
    assert policy["retry_backoff_eligible"] is False
    assert policy["retry_backoff_reason"] == "PROVENANCE_ONLY_DIAGNOSTIC"
    decision = schedule_after_iteration(scheduled_start_monotonic=0, actual_start_monotonic=0,
                                        finished_monotonic=240, interval_seconds=300, backoff_seconds=backoff)
    assert decision.next_scheduled_monotonic == 300


def test_successfully_observed_inspection_budget_is_diagnostic_only():
    execution = _execution(_summary(complete=False, unresolved=0))
    decision = watch_backoff_eligibility({"scanner": "SUCCESS", "telegram_outbox": "PARTIAL"}, execution.telegram_outbox_status, execution.telegram_delivery_summary)
    assert decision.eligible is False


@pytest.mark.parametrize("changes", [{"status": "UNAVAILABLE"}, {"status": "ERROR"}, {"contract": "unknown"},
                                    {"coverage_complete": False, "inspected": 1}, {"unresolved": True},
                                    {"error": "OperationalError"}])
def test_unavailable_failed_or_malformed_inspection_retains_backoff(changes):
    execution = _execution(_summary(**changes))
    decision = watch_backoff_eligibility({"telegram_outbox": "PARTIAL"}, execution.telegram_outbox_status, execution.telegram_delivery_summary)
    assert decision.eligible is True


@pytest.mark.parametrize("key", ["failed", "retryable", "uncertain", "failed_final"])
@pytest.mark.parametrize("value", [1, None, "0", False, -1])
def test_actual_or_unverified_failure_counters_cannot_be_exempted(key, value):
    execution = _execution()
    execution.telegram_outbox_status[key] = value
    assert watch_backoff_eligibility({"telegram_outbox": "PARTIAL"}, execution.telegram_outbox_status, execution.telegram_delivery_summary).eligible is True


def test_missing_summary_or_defaulted_original_source_is_not_proof_of_zero():
    execution = _execution()
    assert watch_backoff_eligibility({"telegram_outbox": "PARTIAL"}, execution.telegram_outbox_status, None).eligible is True
    missing = SimpleNamespace(deliveries=(), provenance_diagnostics=_summary().provenance_diagnostics)
    counts = run_scan._telegram_outbox_status_summary(missing)
    assert counts["failed"] == 0
    assert watch_backoff_eligibility({"telegram_outbox": "PARTIAL"}, counts, missing).eligible is True


@pytest.mark.parametrize("phase", ["scanner", "lifecycle", "owner_monitoring", "scan_storage", "scan_manifest", "watch_output"])
def test_other_operational_failures_preserve_existing_streak_and_backoff(phase):
    execution = _execution()
    phases = {"telegram_outbox": "PARTIAL", phase: "FAILED"}
    streak, backoff, policy = run_scan._watch_retry_policy(execution, phases, 3, True)
    assert streak == 4 and backoff == run_scan.failure_backoff_seconds(4)
    assert policy["retry_backoff_eligible"] is True
    stopped = run_scan._watch_retry_policy(execution, phases, 3, False)
    assert stopped[0] == 4 and stopped[1] == 0


@pytest.mark.parametrize("status,detail", [("uncertain", "unknown"), ("skipped", "RETRYABLE"), ("unknown", "N/A")])
def test_unaccounted_raw_delivery_failure_cannot_hide_behind_zero_counters(status, detail):
    execution = _execution()
    execution.telegram_delivery_summary.deliveries = (SimpleNamespace(status=status, detail=detail),)
    assert watch_backoff_eligibility({"telegram_outbox": "PARTIAL"}, execution.telegram_outbox_status, execution.telegram_delivery_summary).eligible is True


@pytest.mark.parametrize("failure_phase", ["watch_output", "scan_manifest", "scan_storage"])
def test_real_watch_keeps_repeated_partial_status_at_cadence_and_recognizes_tail_failure(tmp_path, monkeypatch, failure_phase):
    path, args = _bootstrap(tmp_path, monkeypatch)
    args[args.index("--watch-max-iterations") + 1] = "4"
    args[-1] = "300"
    SequenceWatchRunner.results_by_call *= 4
    original_attempt = run_scan._attempt_watch_scan_iteration
    async def provenance_only(*args, **kwargs):
        watchlist, execution, error = await original_attempt(*args, **kwargs)
        assert error is None
        summary = _summary()
        execution = replace(execution, telegram_delivery_summary=summary,
                            telegram_outbox_status=run_scan._telegram_outbox_status_summary(summary),
                            phase_statuses={**execution.phase_statuses, "telegram_outbox": "PARTIAL"})
        return watchlist, execution, None
    monkeypatch.setattr(run_scan, "_attempt_watch_scan_iteration", provenance_only)
    sleeps = []
    async def no_wait(seconds):
        sleeps.append(seconds)
    monkeypatch.setattr(run_scan.asyncio, "sleep", no_wait)
    writes = [0]
    target = {"watch_output": "append_watch_output", "scan_manifest": "_append_scan_run_manifest", "scan_storage": "_store_watch_iteration_scan_run"}[failure_phase]
    original_tail = getattr(run_scan, target)
    def failing_tail(*args, **kwargs):
        writes[0] += 1
        if writes[0] == 3:
            raise OSError("synthetic tail failure after diagnostic cycles")
        return original_tail(*args, **kwargs)
    monkeypatch.setattr(run_scan, target, failing_tail)
    extra = ["--store-scan"] if failure_phase == "scan_storage" else []
    asyncio.run(run_scan.main(args + ["--watch-output-file", str(tmp_path / "output.jsonl")] + extra))
    rows = [json.loads(line) for line in Path(f"{path}.watch-observations.jsonl").read_text().splitlines()]
    finals = [row for row in rows if row.get("record_kind") == "watch_iteration_finalization_observation"]
    assert [row["status"] for row in finals] == ["PARTIAL"] * 4
    assert [row["consecutive_failure_count"] for row in finals] == [0, 0, 1, 0]
    assert [row["retry_backoff_eligible"] for row in finals] == [False, False, True, False]
    assert [row["selected_backoff_seconds"] for row in finals] == [0, 0, 5, 0]
    assert finals[2]["phase_statuses"][failure_phase] == "FAILED"
    assert finals[-1]["retry_backoff_reason"] == "PROVENANCE_ONLY_DIAGNOSTIC"
    assert all(row["consecutive_failure_scope"] == "backoff_eligible_operational_iterations" for row in finals)
    assert len(sleeps) == 3
