from __future__ import annotations

import sqlite3
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerSymbolResult,
)
from app.storage.database import (
    UnsupportedSchemaVersionError,
    open_initialized_database,
)
from app.universe.symbol_universe import UniverseResolutionError
from app.watch_iteration import (
    not_run_symbol_result,
    queued_symbol_outcome_counts,
    queued_symbol_outcomes,
    scanner_phase_status,
    telegram_outbox_phase_status,
    telegram_outbox_status_summary,
)
from app.watch_mode import WatchModeError, WatchState, load_watch_state, save_watch_state
from app.watch_supervisor import (
    FatalWatchIterationError,
    WatchFailureDisposition,
    classify_watch_exception,
    failure_backoff_seconds,
    schedule_after_iteration,
)


def test_start_to_start_cadence_subtracts_iteration_duration() -> None:
    decision = schedule_after_iteration(
        scheduled_start_monotonic=1_000,
        actual_start_monotonic=1_000,
        finished_monotonic=1_090,
        interval_seconds=300,
    )

    assert decision.duration_seconds == 90
    assert decision.sleep_seconds == 210
    assert decision.next_scheduled_monotonic == 1_300
    assert decision.cadence_lag_seconds == 0
    assert decision.overrun_seconds == 0


def test_fast_iteration_preserves_original_cadence_anchor() -> None:
    first = schedule_after_iteration(
        scheduled_start_monotonic=100,
        actual_start_monotonic=100,
        finished_monotonic=101,
        interval_seconds=300,
    )
    second = schedule_after_iteration(
        scheduled_start_monotonic=first.next_scheduled_monotonic,
        actual_start_monotonic=first.next_scheduled_monotonic,
        finished_monotonic=first.next_scheduled_monotonic + 2,
        interval_seconds=300,
    )

    assert first.next_scheduled_monotonic == 400
    assert second.next_scheduled_monotonic == 700
    assert second.sleep_seconds == 298


def test_overrun_reanchors_without_overlap_or_catch_up_storm() -> None:
    decision = schedule_after_iteration(
        scheduled_start_monotonic=1_000,
        actual_start_monotonic=1_000,
        finished_monotonic=1_390,
        interval_seconds=300,
    )

    assert decision.overrun_seconds == 90
    assert decision.missed_interval_count == 1
    assert decision.next_scheduled_monotonic == 1_600
    assert decision.sleep_seconds == 210
    assert decision.next_scheduled_monotonic >= decision.finished_monotonic


def test_cadence_diagnostics_use_only_monotonic_inputs() -> None:
    decision = schedule_after_iteration(
        scheduled_start_monotonic=10,
        actual_start_monotonic=17,
        finished_monotonic=25,
        interval_seconds=10,
    )

    assert decision.cadence_lag_seconds == 7
    assert decision.overrun_seconds == 5
    assert decision.missed_interval_count == 1
    assert decision.next_scheduled_monotonic == 30


def test_failure_backoff_is_deterministic_bounded_and_resets() -> None:
    assert [failure_backoff_seconds(item) for item in range(1, 5)] == [5, 10, 20, 40]
    assert failure_backoff_seconds(20) == 300
    assert failure_backoff_seconds(0) == 0


def test_failure_backoff_combines_with_cadence_without_tight_loop() -> None:
    decision = schedule_after_iteration(
        scheduled_start_monotonic=0,
        actual_start_monotonic=0,
        finished_monotonic=2,
        interval_seconds=1,
        backoff_seconds=20,
    )

    assert decision.next_scheduled_monotonic == 22
    assert decision.sleep_seconds == 20


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (UniverseResolutionError("temporary universe failure"), WatchFailureDisposition.RECOVERABLE),
        (OSError("temporary filesystem failure"), WatchFailureDisposition.RECOVERABLE),
        (FatalWatchIterationError("unsafe invariant"), WatchFailureDisposition.FATAL),
        (AssertionError("accounting invariant"), WatchFailureDisposition.FATAL),
        (ValidationError.from_exception_data("invalid", []), WatchFailureDisposition.FATAL),
    ),
)
def test_failure_classification(error, expected) -> None:
    assert classify_watch_exception(error) == expected


def test_unsupported_schema_is_fatal_and_left_untouched(tmp_path) -> None:
    database_path = tmp_path / "newer.sqlite"
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA user_version = 17")
    connection.close()

    with pytest.raises(UnsupportedSchemaVersionError):
        open_initialized_database(database_path)

    verification = sqlite3.connect(database_path)
    try:
        assert verification.execute("PRAGMA user_version").fetchone()[0] == 17
        assert verification.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0] == 0
    finally:
        verification.close()


def test_every_queued_symbol_has_exactly_one_explicit_outcome() -> None:
    symbols = ("EVAL", "REJECT", "ERROR", "TIMEOUT", "LATER")
    results = (
        _symbol("EVAL", ScannerPipelineStatus.IDEA_CREATED),
        _symbol("REJECT", ScannerPipelineStatus.SCANNED_NO_SETUP),
        _symbol("ERROR", ScannerPipelineStatus.SCAN_ERROR),
        _symbol(
            "TIMEOUT",
            ScannerPipelineStatus.SCAN_ERROR,
            timed_out=True,
            timeout_status="symbol_timeout",
        ),
        not_run_symbol_result("LATER", reason="global_timeout_not_run"),
    )
    run_result = _run_result(symbols, results)

    assert queued_symbol_outcome_counts(run_result, symbols) == {
        "evaluated": 1,
        "rejected": 1,
        "errored": 1,
        "timed_out": 1,
        "not_run": 1,
    }
    outcomes = queued_symbol_outcomes(run_result, symbols)
    assert outcomes["LATER"]["reason_code"] == "global_timeout_not_run"
    assert outcomes["TIMEOUT"]["reason_code"] == "symbol_timeout"
    assert scanner_phase_status(run_result, symbols) == "PARTIAL"


def test_queue_accounting_rejects_missing_or_duplicate_outcomes() -> None:
    result = _run_result(
        ("ONE", "TWO"),
        (
            _symbol("ONE", ScannerPipelineStatus.SCANNED_NO_SETUP),
            _symbol("ONE", ScannerPipelineStatus.SCANNED_NO_SETUP),
        ),
    )

    with pytest.raises(AssertionError, match="duplicate outcomes"):
        queued_symbol_outcome_counts(result, ("ONE", "TWO"))


def test_telegram_retryable_and_uncertain_states_are_explicitly_partial() -> None:
    summary = SimpleNamespace(
        sent=1,
        skipped=0,
        duplicate=0,
        blocked=0,
        blocked_repeat=0,
        failed=0,
        deliveries=(
            SimpleNamespace(status="retryable", detail="RETRYABLE"),
            SimpleNamespace(status="uncertain", detail="UNCERTAIN_NO_RESEND"),
        ),
    )

    counts = telegram_outbox_status_summary(summary)
    assert counts["retryable"] == 1
    assert counts["uncertain"] == 1
    assert telegram_outbox_phase_status(counts) == "PARTIAL"


def test_watch_state_save_refuses_to_overwrite_newer_state(tmp_path) -> None:
    path = tmp_path / "watch_state.json"
    newer = WatchState(updated_at="2026-07-17T12:00:00+00:00")
    stale = WatchState(updated_at="2026-07-17T11:00:00+00:00")
    save_watch_state(path, newer)

    with pytest.raises(WatchModeError, match="newer watch state"):
        save_watch_state(
            path,
            stale,
            expected_updated_at="2026-07-17T11:00:00+00:00",
        )

    assert load_watch_state(path) == newer
    assert list(tmp_path.glob("*.tmp")) == []
    assert list(tmp_path.glob(".*.tmp")) == []


def _symbol(
    symbol: str,
    status: ScannerPipelineStatus,
    **updates,
) -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=status,
        status_history=(status,),
        **updates,
    )


def _run_result(
    symbols: tuple[str, ...],
    results: tuple[ScannerSymbolResult, ...],
) -> ScannerRunResult:
    return ScannerRunResult(
        config=ScannerRunConfig(
            symbols=list(symbols),
            exchange="binance",
            account_equity="10000",
            risk_per_trade_pct="1",
        ),
        results=results,
        scanned_symbols=sum(item.iteration_outcome != "not_run" for item in results),
        failed_symbols=sum(item.iteration_outcome in {"errored", "timed_out"} for item in results),
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )
