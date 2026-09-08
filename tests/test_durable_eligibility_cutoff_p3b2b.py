from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.analytics.evidence_contract import UNAVAILABLE, evidence_contract_payload
from app.analytics.outcome_ownership import (
    ELIGIBILITY_CUTOFF_KNOWN,
    dumps_outcome_ownership_report,
    project_outcome_ownership,
)
from app.data.candle_integrity import normalize_utc_timestamp
from app.data.dtos import NA
from app.lifecycle.economic_identity import latch_economic_identities, proven_progress_plan_version_id
from app.lifecycle.models import SetupLifecycleOutcomeProgress, SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import SetupLifecycleService
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerSymbolResult
from app.storage.database import SCHEMA_VERSION, open_initialized_database

BASE = datetime(2026, 1, 1, tzinfo=UTC)
TIMEFRAME = "5m"
INVALIDATION = "Closed structure beyond the stored stop invalidates the plan."
PROCESSED_AT = (BASE + timedelta(hours=3)).isoformat()


def _candle(index: int, *, high: str, low: str) -> dict[str, object]:
    opened = BASE + timedelta(minutes=5 * index)
    return {
        "timestamp": int(opened.timestamp() * 1000),
        "open": Decimal(low),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(high),
        "volume": Decimal("10"),
    }


def _close(index: int) -> datetime:
    return BASE + timedelta(minutes=5 * (index + 1))


def _decision(index: int) -> str:
    return _close(index).isoformat()


def _record(*, lifecycle_id: str = "life-1", **updates) -> SetupLifecycleRecord:
    values = {
        "lifecycle_id": lifecycle_id,
        "symbol": "BTCUSDT",
        "mode": "challenge",
        "direction": "long",
        "current_state": SetupLifecycleState.ACTIONABLE_A_GRADE,
        "first_seen_at": BASE.isoformat(),
        "last_seen_at": BASE.isoformat(),
        "last_transition_at": BASE.isoformat(),
        "confirmed_at": BASE.isoformat(),
        "invalidation_reason": INVALIDATION,
        "invalidation_logic": INVALIDATION,
        "setup_identity": f"{lifecycle_id}-setup",
        "structural_anchor": "sweep-a",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "90",
        "tp1": "110",
        "tp2": "120",
        "tp3": "130",
    }
    values.update(updates)
    return SetupLifecycleRecord(**values)


def _latched(**updates) -> SetupLifecycleRecord:
    return latch_economic_identities(
        _record(**updates),
        instrument_venue="binance",
        plan_locked=True,
    )


def _evaluate(
    repository,
    record,
    candles,
    *,
    decision_timestamp=None,
    evaluated_at=None,
    scan_run_id=None,
    execution_timeframe=TIMEFRAME,
):
    index = max(len(candles) - 1, 0)
    decision = decision_timestamp if decision_timestamp is not None else _close(index)
    processed = evaluated_at if evaluated_at is not None else PROCESSED_AT
    return evaluate_closed_candle_outcomes(
        record,
        execution_candles=candles,
        execution_timeframe=execution_timeframe,
        decision_timestamp=decision,
        evaluated_at=processed if isinstance(processed, str) else processed.isoformat(),
        repository=repository,
        scan_run_id=scan_run_id,
    )


def _sql_progress(repository, lifecycle_id: str) -> list[dict[str, object]]:
    cursor = repository._connection.execute(
        """
        SELECT * FROM setup_lifecycle_outcome_progress
        WHERE lifecycle_id = ?
        ORDER BY id
        """,
        (lifecycle_id,),
    )
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _project(record, progress, **provenance):
    values = {
        "synthetic": True,
        "label": "p3b2b-synthetic",
        "source_namespace": "synthetic-p3b2b",
        "coverage_complete": False,
    }
    values.update(provenance)
    return project_outcome_ownership(
        lifecycle_records=[record],
        progress_rows=[progress] if progress is not None else [],
        provenance=values,
    )


def test_effective_cutoff_survives_write_and_reload_distinct_from_processing_time(tmp_path: Path) -> None:
    record = _latched()
    cutoff = _close(1) + timedelta(minutes=1)
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    with SQLiteSetupLifecycleRepository(tmp_path / "roundtrip.db") as repository:
        repository.upsert_record(record)
        result = _evaluate(repository, record, candles, decision_timestamp=cutoff)
        row = _sql_progress(repository, record.lifecycle_id)[0]
        loaded = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
    assert result.progress.last_evaluated_at == PROCESSED_AT
    assert row["last_evaluated_at"] == PROCESSED_AT
    assert row["last_eligibility_decision_at"] == cutoff.isoformat()
    assert loaded.last_eligibility_decision_at == cutoff.isoformat()
    assert loaded.eligibility_cutoff_observed is False
    assert "last_eligibility_decision_at" not in loaded.model_dump()
    assert normalize_utc_timestamp(row["last_eligibility_decision_at"], field_name="cutoff") == cutoff


def test_two_inter_close_cutoffs_remain_distinct_after_reload(tmp_path: Path) -> None:
    record = _latched()
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    cutoff_a = _close(1) + timedelta(minutes=1)
    cutoff_b = _close(1) + timedelta(minutes=3)
    assert _close(1) < cutoff_a < cutoff_b < _close(2)

    def _run(path: Path, cutoff: datetime) -> dict[str, object]:
        with SQLiteSetupLifecycleRepository(path) as repository:
            repository.upsert_record(record)
            result = _evaluate(repository, record, candles, decision_timestamp=cutoff)
            row = _sql_progress(repository, record.lifecycle_id)[0]
        return {
            "entry_at": result.progress.entry_at,
            "cursor": row["evaluation_cursor_close_at"],
            "cutoff": row["last_eligibility_decision_at"],
            "evaluated_at": row["last_evaluated_at"],
        }

    first = _run(tmp_path / "a.db", cutoff_a)
    second = _run(tmp_path / "b.db", cutoff_b)
    assert first["entry_at"] == second["entry_at"] is not None
    assert first["cursor"] == second["cursor"]
    assert first["evaluated_at"] == second["evaluated_at"] == PROCESSED_AT
    assert first["cutoff"] == cutoff_a.isoformat()
    assert second["cutoff"] == cutoff_b.isoformat()


def test_exact_close_included_and_future_candle_excluded(tmp_path: Path) -> None:
    candles = [
        _candle(0, high="99", low="95"),
        _candle(1, high="103", low="99"),
        _candle(2, high="111", low="103"),
    ]
    with SQLiteSetupLifecycleRepository(tmp_path / "exact.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        exact = _evaluate(repository, record, candles, decision_timestamp=_close(1))
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert exact.progress.entry_at == _decision(1)
    assert exact.progress.tp1_at is None
    assert row["last_eligibility_decision_at"] == _close(1).isoformat()

    with SQLiteSetupLifecycleRepository(tmp_path / "early.db") as repository:
        early_record = _latched(lifecycle_id="life-early")
        repository.upsert_record(early_record)
        early = _evaluate(
            repository,
            early_record,
            candles,
            decision_timestamp=_close(1) - timedelta(seconds=1),
        )
        early_row = _sql_progress(repository, early_record.lifecycle_id)[0]
    assert early.progress.entry_at is None
    assert early_row["last_eligibility_decision_at"] == (_close(1) - timedelta(seconds=1)).isoformat()


def test_service_fallback_now_is_recorded_without_source_provenance(tmp_path: Path) -> None:
    db_path = tmp_path / "fallback.db"
    record = _latched()
    now = (BASE + timedelta(hours=4)).replace(microsecond=0).isoformat()
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        strategy_diagnostics={
            "challenge": {
                "mode": "challenge",
                "bias": "long",
                "invalidation": record.invalidation_logic,
            }
        },
        valid_strategy_modes=("challenge",),
        lifecycle_execution_candles=(_candle(0, high="99", low="95"), _candle(1, high="103", low="99")),
        lifecycle_execution_timeframe=TIMEFRAME,
    )
    assert symbol_result.lifecycle_decision_timestamp is None
    assert "lifecycle_decision_timestamp" not in symbol_result.model_dump()
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        service = SetupLifecycleService(db_path)
        updated = service.apply_to_symbol_result(
            symbol_result,
            repository=repository,
            scan_run_id="scan-fallback",
            now=now,
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    expected = normalize_utc_timestamp(now, field_name="decision_timestamp").isoformat()
    assert row["last_eligibility_decision_at"] == expected
    assert row["last_evaluated_at"] == now
    assert updated.lifecycle_outcome_progress is not None
    dumped = updated.lifecycle_outcome_progress.model_dump()
    assert "last_eligibility_decision_at" not in dumped
    assert "eligibility_cutoff_observed" not in dumped


def test_microsecond_precision_round_trip(tmp_path: Path) -> None:
    record = _latched()
    cutoff = _close(1) + timedelta(microseconds=123456)
    with SQLiteSetupLifecycleRepository(tmp_path / "precision.db") as repository:
        repository.upsert_record(record)
        _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
            decision_timestamp=cutoff,
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
        loaded = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
    assert row["last_eligibility_decision_at"] == cutoff.isoformat()
    assert loaded.last_eligibility_decision_at == cutoff.isoformat()
    assert normalize_utc_timestamp(loaded.last_eligibility_decision_at, field_name="cutoff") == cutoff


def test_first_write_before_filter_stays_null_until_application(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "first.db") as repository:
        repository.upsert_record(record)
        missing = _evaluate(repository, record, [])
        missing_row = _sql_progress(repository, record.lifecycle_id)[0]
        applied = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95")],
            decision_timestamp=_close(0),
        )
        applied_row = _sql_progress(repository, record.lifecycle_id)[0]
    assert missing.progress.diagnostic == "missing_execution_candle_history"
    assert missing_row["last_eligibility_decision_at"] is None
    assert applied_row["last_eligibility_decision_at"] == _close(0).isoformat()
    assert applied.progress.eligibility_cutoff_observed is True


def test_cursor_advancement_records_input_not_last_close_as_coverage(tmp_path: Path) -> None:
    record = _latched()
    cutoff = _close(1) + timedelta(minutes=2)
    with SQLiteSetupLifecycleRepository(tmp_path / "advance.db") as repository:
        repository.upsert_record(record)
        result = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
            decision_timestamp=cutoff,
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert result.processed_candles > 0
    assert row["evaluation_cursor_close_at"] == _close(1).isoformat()
    assert row["last_eligibility_decision_at"] == cutoff.isoformat()
    assert row["last_eligibility_decision_at"] != row["evaluation_cursor_close_at"]


def test_same_cutoff_repeat_and_newer_cutoff_noop(tmp_path: Path) -> None:
    record = _latched()
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    cutoff_a = _close(1) + timedelta(minutes=1)
    cutoff_b = _close(1) + timedelta(minutes=3)
    with SQLiteSetupLifecycleRepository(tmp_path / "noop.db") as repository:
        repository.upsert_record(record)
        first = _evaluate(repository, record, candles, decision_timestamp=cutoff_a)
        cursor = first.progress.evaluation_cursor_open_at
        repeat = _evaluate(repository, first.record, candles, decision_timestamp=cutoff_a)
        noop = _evaluate(repository, repeat.record, candles, decision_timestamp=cutoff_b)
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert repeat.processed_candles == 0
    assert noop.processed_candles == 0
    assert noop.progress.evaluation_cursor_open_at == cursor
    assert row["last_eligibility_decision_at"] == cutoff_b.isoformat()


def test_waiting_for_first_eligible_candle_records_cutoff(tmp_path: Path) -> None:
    record = _latched(confirmed_at=(BASE + timedelta(minutes=1)).isoformat())
    with SQLiteSetupLifecycleRepository(tmp_path / "wait.db") as repository:
        repository.upsert_record(record)
        waiting = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95")],
            decision_timestamp=_close(0),
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert waiting.processed_candles == 0
    assert waiting.progress.entry_at is None
    assert waiting.progress.integrity_status == "Verified"
    assert row["last_eligibility_decision_at"] == _close(0).isoformat()


def test_stale_history_after_filter_records_cutoff(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "stale.db") as repository:
        repository.upsert_record(record)
        first = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
            decision_timestamp=_close(1),
        )
        stale = _evaluate(
            repository,
            first.record,
            [_candle(0, high="99", low="95")],
            decision_timestamp=_close(0),
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert "stale_execution_candle_history" in stale.progress.diagnostic
    assert row["last_eligibility_decision_at"] == _close(0).isoformat()
    assert stale.progress.evaluation_cursor_open_at == first.progress.evaluation_cursor_open_at


def test_continuity_gap_does_not_record_unreturned_filter(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "gap.db") as repository:
        repository.upsert_record(record)
        first = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95")],
            decision_timestamp=_close(0),
        )
        prior = _sql_progress(repository, record.lifecycle_id)[0]["last_eligibility_decision_at"]
        gapped = _evaluate(
            repository,
            first.record,
            [_candle(0, high="99", low="95"), _candle(2, high="103", low="99")],
            decision_timestamp=_close(2),
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert "continuity_gap" in gapped.progress.diagnostic
    assert row["last_eligibility_decision_at"] == prior == _close(0).isoformat()


def test_timeframe_mismatch_before_filter_preserves_cutoff(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "tf.db") as repository:
        repository.upsert_record(record)
        first = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95")],
            decision_timestamp=_close(0),
        )
        mismatched = _evaluate(
            repository,
            first.record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
            decision_timestamp=_close(1),
            execution_timeframe="15m",
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert "execution_timeframe_changed" in mismatched.progress.diagnostic
    assert row["last_eligibility_decision_at"] == _close(0).isoformat()


def test_empty_prefix_after_filter_records_cutoff(tmp_path: Path) -> None:
    record = _latched()
    cutoff = BASE + timedelta(minutes=1)
    with SQLiteSetupLifecycleRepository(tmp_path / "empty.db") as repository:
        repository.upsert_record(record)
        empty = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95")],
            decision_timestamp=cutoff,
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert empty.progress.diagnostic == "no_closed_execution_candles_at_decision_boundary"
    assert row["last_eligibility_decision_at"] == cutoff.isoformat()


def test_rejected_upsert_does_not_orphan_cutoff(tmp_path: Path) -> None:
    record = _latched()
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    with SQLiteSetupLifecycleRepository(tmp_path / "rollback.db") as repository:
        repository.upsert_record(record)
        first = _evaluate(repository, record, candles, decision_timestamp=_close(1))
        stored = _sql_progress(repository, record.lifecycle_id)[0]["last_eligibility_decision_at"]

        def _boom(progress: SetupLifecycleOutcomeProgress) -> None:
            raise RuntimeError("reject progress write")

        repository.upsert_outcome_progress = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="reject progress write"):
            _evaluate(
                repository,
                first.record,
                candles,
                decision_timestamp=_close(1) + timedelta(minutes=2),
            )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert row["last_eligibility_decision_at"] == stored == _close(1).isoformat()


def test_atomic_rollback_restores_prior_cutoff(tmp_path: Path) -> None:
    record = _latched()
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    with SQLiteSetupLifecycleRepository(tmp_path / "tx.db") as repository:
        repository.upsert_record(record)
        _evaluate(repository, record, candles, decision_timestamp=_close(1))
        repository._connection.commit()
        prior = _sql_progress(repository, record.lifecycle_id)[0]["last_eligibility_decision_at"]
        repository._connection.execute("BEGIN")
        _evaluate(
            repository,
            record,
            candles,
            decision_timestamp=_close(1) + timedelta(minutes=2),
        )
        repository._connection.rollback()
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert prior == _close(1).isoformat()
    assert row["last_eligibility_decision_at"] == prior


def test_terminal_shortcut_does_not_invent_filter_input(tmp_path: Path) -> None:
    fresh = _latched(lifecycle_id="life-term-fresh", current_state=SetupLifecycleState.INVALIDATED)
    with SQLiteSetupLifecycleRepository(tmp_path / "term-fresh.db") as repository:
        repository.upsert_record(fresh)
        _evaluate(repository, fresh, [_candle(0, high="99", low="95")], decision_timestamp=_close(0))
        row = _sql_progress(repository, fresh.lifecycle_id)[0]
    assert row["terminal_outcome"] == SetupLifecycleState.INVALIDATED.value
    assert row["last_eligibility_decision_at"] is None

    live = _latched(lifecycle_id="life-term-existing")
    with SQLiteSetupLifecycleRepository(tmp_path / "term-existing.db") as repository:
        repository.upsert_record(live)
        first = _evaluate(
            repository,
            live,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
            decision_timestamp=_close(1) + timedelta(minutes=1),
        )
        terminal_record = first.record.model_copy(
            update={"current_state": SetupLifecycleState.EXPIRED}
        )
        repository.upsert_record(terminal_record)
        _evaluate(
            repository,
            terminal_record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
            decision_timestamp=_close(2),
        )
        row = _sql_progress(repository, live.lifecycle_id)[0]
    assert row["terminal_outcome"] == SetupLifecycleState.EXPIRED.value
    assert row["last_eligibility_decision_at"] == (_close(1) + timedelta(minutes=1)).isoformat()


def test_normal_tp_terminal_before_prefix_exhausted_is_not_coverage(tmp_path: Path) -> None:
    record = _latched()
    candles = [
        _candle(0, high="99", low="95"),
        _candle(1, high="103", low="99"),
        _candle(2, high="111", low="103"),
        _candle(3, high="121", low="111"),
        _candle(4, high="131", low="121"),
        _candle(5, high="132", low="130"),
    ]
    cutoff = _close(5)
    with SQLiteSetupLifecycleRepository(tmp_path / "tp3.db") as repository:
        repository.upsert_record(record)
        result = _evaluate(repository, record, candles, decision_timestamp=cutoff)
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert result.progress.terminal_outcome == SetupLifecycleState.TP_HIT.value
    assert result.progress.outcome_at == _decision(4)
    assert result.processed_candles < len(candles)
    assert row["last_eligibility_decision_at"] == cutoff.isoformat()
    payload = _project(record, result.progress)
    assert payload["evaluations"][0]["evaluation_context"]["complete"] is False
    assert payload["durable_evaluation_context"]["established"] is False


def test_decreasing_cutoff_stores_accepted_input_not_max(tmp_path: Path) -> None:
    record = _latched()
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    later = _close(1) + timedelta(minutes=3)
    earlier = _close(1) + timedelta(minutes=1)
    with SQLiteSetupLifecycleRepository(tmp_path / "decreasing.db") as repository:
        repository.upsert_record(record)
        _evaluate(repository, record, candles, decision_timestamp=later)
        _evaluate(repository, record, candles, decision_timestamp=earlier)
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert row["last_eligibility_decision_at"] == earlier.isoformat()


def test_omitted_witness_and_stale_object_cannot_roll_back_newer_cutoff(tmp_path: Path) -> None:
    record = _latched()
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    first_cutoff = _close(1) + timedelta(minutes=1)
    second_cutoff = _close(1) + timedelta(minutes=3)
    with SQLiteSetupLifecycleRepository(tmp_path / "stale-obj.db") as repository:
        repository.upsert_record(record)
        first = _evaluate(repository, record, candles, decision_timestamp=first_cutoff)
        stale = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
        _evaluate(repository, first.record, candles, decision_timestamp=second_cutoff)
        mutated = stale.model_copy(
            update={
                "last_evaluated_at": PROCESSED_AT,
                "last_eligibility_decision_at": first_cutoff.isoformat(),
                "eligibility_cutoff_observed": False,
            }
        )
        repository.upsert_outcome_progress(mutated)
        row = _sql_progress(repository, record.lifecycle_id)[0]
        loaded = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
        repository.upsert_outcome_progress(loaded)
        after_reload = _sql_progress(repository, record.lifecycle_id)[0]
    assert row["last_eligibility_decision_at"] == second_cutoff.isoformat()
    assert after_reload["last_eligibility_decision_at"] == second_cutoff.isoformat()


def test_later_or_absent_scan_run_id_does_not_split_or_reset_cutoff(tmp_path: Path) -> None:
    record = _latched()
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    cutoff = _close(1) + timedelta(minutes=1)
    newer = _close(1) + timedelta(minutes=2)
    with SQLiteSetupLifecycleRepository(tmp_path / "scan.db") as repository:
        repository.upsert_record(record)
        first = _evaluate(repository, record, candles, decision_timestamp=cutoff, scan_run_id="scan-1")
        continued = _evaluate(
            repository,
            first.record,
            candles,
            decision_timestamp=newer,
            scan_run_id="scan-2",
        )
        none_pass = _evaluate(
            repository,
            continued.record,
            candles,
            decision_timestamp=newer,
            scan_run_id=None,
        )
        rows = _sql_progress(repository, record.lifecycle_id)
    assert len(rows) == 1
    assert rows[0]["last_eligibility_decision_at"] == newer.isoformat()
    assert none_pass.processed_candles == 0


def test_material_replacement_binds_cutoff_to_new_economics(tmp_path: Path) -> None:
    first = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "replace.db") as repository:
        repository.upsert_record(first)
        entered = _evaluate(
            repository,
            first,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
            decision_timestamp=_close(1),
        )
        old_identity = entered.progress.plan_identity
        old_cutoff = _sql_progress(repository, first.lifecycle_id)[0]["last_eligibility_decision_at"]
        replacement = entered.record.model_copy(
            update={
                "current_state": SetupLifecycleState.ACTIONABLE_A_GRADE,
                "entry_low": "200",
                "entry_high": "202",
                "stop_loss": "190",
                "tp1": "210",
                "tp2": "220",
                "tp3": "230",
                "invalidation_logic": "Closed structure below 190 invalidates the replacement plan.",
                "invalidation_reason": "Closed structure below 190 invalidates the replacement plan.",
            }
        )
        replacement = latch_economic_identities(
            replacement,
            instrument_venue="binance",
            plan_locked=True,
            previous=entered.record,
        )
        repository.upsert_record(replacement)
        second_cutoff = _close(0) + timedelta(minutes=1)
        second = _evaluate(
            repository,
            replacement,
            [_candle(0, high="199", low="195")],
            decision_timestamp=second_cutoff,
            evaluated_at=(BASE + timedelta(hours=2)).isoformat(),
        )
        rows = {row["plan_identity"]: row for row in _sql_progress(repository, first.lifecycle_id)}
    new_identity = canonical_plan_identity(replacement)
    assert new_identity != old_identity
    assert rows[old_identity]["last_eligibility_decision_at"] == old_cutoff
    assert rows[old_identity]["plan_version_id"] == first.plan_version_id
    assert rows[new_identity]["last_eligibility_decision_at"] == second_cutoff.isoformat()
    assert rows[new_identity]["plan_version_id"] is None
    assert proven_progress_plan_version_id(replacement) is None
    assert second.progress.plan_identity == new_identity


def test_shared_plan_version_keeps_distinct_physical_rows(tmp_path: Path) -> None:
    first = _latched(lifecycle_id="life-a")
    second = _latched(lifecycle_id="life-b")
    assert first.plan_version_id == second.plan_version_id
    cutoff = _close(0)
    with SQLiteSetupLifecycleRepository(tmp_path / "shared.db") as repository:
        repository.upsert_record(first)
        _evaluate(repository, first, [_candle(0, high="99", low="95")], decision_timestamp=cutoff)
        repository.supersede_record(first.lifecycle_id)
        repository.upsert_record(second)
        _evaluate(repository, second, [_candle(0, high="99", low="95")], decision_timestamp=cutoff)
        unique_sql = repository._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'setup_lifecycle_outcome_progress'"
        ).fetchone()[0]
        rows = repository._connection.execute(
            "SELECT lifecycle_id, plan_version_id, last_eligibility_decision_at FROM setup_lifecycle_outcome_progress"
        ).fetchall()
    assert "UNIQUE(lifecycle_id, plan_identity)" in unique_sql
    assert len(rows) == 2
    assert {row[0] for row in rows} == {"life-a", "life-b"}
    assert {row[1] for row in rows} == {first.plan_version_id}
    assert {row[2] for row in rows} == {cutoff.isoformat()}


def test_p3a_distinguishes_known_null_and_old_schema_without_completeness(tmp_path: Path) -> None:
    record = _latched()
    cutoff = _close(1) + timedelta(minutes=1)
    with SQLiteSetupLifecycleRepository(tmp_path / "p3a.db") as repository:
        repository.upsert_record(record)
        result = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
            decision_timestamp=cutoff,
        )
        known = _project(record, result.progress)
    context = known["evaluations"][0]["evaluation_context"]
    assert context["last_eligibility_decision_at"] == cutoff.isoformat()
    assert context["last_eligibility_decision_at_status"] == ELIGIBILITY_CUTOFF_KNOWN
    assert context["durable_decision_timestamp"] == cutoff.isoformat()
    assert context["complete"] is False
    assert known["durable_evaluation_context"]["established"] is False
    assert known["canonical_outcome_per_plan_version"]["established"] is False

    null_payload = _project(
        record,
        {
            "lifecycle_id": record.lifecycle_id,
            "plan_identity": canonical_plan_identity(record),
            "plan_version_id": record.plan_version_id,
            "symbol": "BTCUSDT",
            "tracking_start_at": BASE.isoformat(),
            "execution_timeframe": "5m",
            "first_evaluated_at": _decision(0),
            "last_evaluated_at": _decision(0),
            "last_eligibility_decision_at": None,
            "terminal_outcome": NA,
        },
    )
    null_context = null_payload["evaluations"][0]["evaluation_context"]
    assert null_context["durable_decision_timestamp"] is None
    assert null_context["durable_decision_timestamp_status"] == UNAVAILABLE
    assert null_context["complete"] is False

    old_schema = _project(
        record,
        {
            "lifecycle_id": record.lifecycle_id,
            "plan_identity": canonical_plan_identity(record),
            "plan_version_id": record.plan_version_id,
            "symbol": "BTCUSDT",
            "tracking_start_at": BASE.isoformat(),
            "decision_timestamp": cutoff.isoformat(),
            "execution_timeframe": "5m",
            "first_evaluated_at": _decision(0),
            "last_evaluated_at": _decision(0),
            "terminal_outcome": NA,
        },
    )
    old_context = old_schema["evaluations"][0]["evaluation_context"]
    assert old_context["durable_decision_timestamp"] is None
    assert old_context["last_eligibility_decision_at"] is None
    assert old_context["complete"] is False
    artifact = json.loads(dumps_outcome_ownership_report(known))
    assert artifact["durable_evaluation_context"]["established"] is False


def test_p3a_projection_does_not_mutate_or_open_database(tmp_path: Path) -> None:
    record = _latched()
    path = tmp_path / "readonly.db"
    with SQLiteSetupLifecycleRepository(path) as repository:
        repository.upsert_record(record)
        result = _evaluate(repository, record, [_candle(0, high="99", low="95")])
        before = _sql_progress(repository, record.lifecycle_id)[0]
        schema_before = repository._connection.execute("PRAGMA user_version").fetchone()[0]
        _project(record, result.progress)
        after = _sql_progress(repository, record.lifecycle_id)[0]
        schema_after = repository._connection.execute("PRAGMA user_version").fetchone()[0]
    assert before == after
    assert schema_before == schema_after == SCHEMA_VERSION == 24
    source = Path("app/analytics/outcome_ownership.py").read_text(encoding="utf-8")
    assert "sqlite3" not in source
    assert "open_initialized_database" not in source


def test_migrated_legacy_row_reload_does_not_populate_cutoff(tmp_path: Path) -> None:
    from tests.test_storage_database import _create_schema_v22_outcome_progress_fixture

    db_path = tmp_path / "legacy-reload.db"
    _create_schema_v22_outcome_progress_fixture(db_path)
    with open_initialized_database(db_path):
        pass
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        loaded = repository.get_outcome_progress(
            lifecycle_id="v22-active-null",
            plan_identity="plan-v22-active-null",
        )
        assert loaded is not None
        assert loaded.last_eligibility_decision_at is None
        assert loaded.plan_version_id is None
        repository.upsert_outcome_progress(loaded)
        attributed = repository.get_outcome_progress(
            lifecycle_id="v22-active-attributed",
            plan_identity="plan-v22-active-attr",
        )
        assert attributed is not None
        assert attributed.last_eligibility_decision_at is None
        assert attributed.plan_version_id == "plan-version-active"
        repository.upsert_outcome_progress(attributed)
        rows = repository._connection.execute(
            """
            SELECT lifecycle_id, plan_version_id, last_eligibility_decision_at
            FROM setup_lifecycle_outcome_progress
            ORDER BY lifecycle_id
            """
        ).fetchall()
    assert {row[2] for row in rows} == {None}
    by_id = {row[0]: row[1] for row in rows}
    assert by_id["v22-active-null"] is None
    assert by_id["v22-active-attributed"] == "plan-version-active"
    assert by_id["v22-terminal-null"] is None
    assert by_id["v22-terminal-attributed"] == "plan-version-terminal"

def test_public_health_memory_do_not_consume_cutoff_field() -> None:
    from app.alerts import telegram_lifecycle
    from app.analytics import performance_memory, symbol_health
    from app.research import queries

    banned = "last_eligibility_decision_at"
    for module in (telegram_lifecycle, symbol_health, performance_memory, queries):
        assert banned not in inspect.getsource(module)
    assert evidence_contract_payload()["outcome_ownership"]["feeds_operational_decisions"] is False
    assert evidence_contract_payload()["unique_trade_count"]["status"] == UNAVAILABLE
    assert SCHEMA_VERSION == 24
