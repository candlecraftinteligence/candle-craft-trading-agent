from __future__ import annotations

import copy
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.analytics.evidence_contract import UNAVAILABLE, evidence_contract_payload
from app.analytics.outcome_ownership import (
    dumps_outcome_ownership_report,
    project_outcome_ownership,
)
from app.data.candle_integrity import normalize_utc_timestamp
from app.data.dtos import NA
from app.lifecycle.economic_identity import latch_economic_identities, proven_progress_plan_version_id
from app.lifecycle.models import SetupLifecycleOutcomeProgress, SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.prefix_disposition_evidence import (
    DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES,
    DISPOSITION_NO_NEW_PENDING_CANDLES,
    DISPOSITION_PENDING_SUFFIX_EXHAUSTED,
    DISPOSITION_POLICY_TERMINAL,
    DISPOSITION_POST_FILTER_BLOCKED,
    DISPOSITION_PROCESSING_ABORTED,
    DISPOSITION_WAITING_FOR_TRACKING_START,
    PREFIX_EVIDENCE_CONTRACT_VERSION,
    PREFIX_EVIDENCE_STATUS_CONFLICTING,
    PREFIX_EVIDENCE_STATUS_KNOWN,
    PREFIX_EVIDENCE_STATUS_MALFORMED,
    diagnose_prefix_evidence,
    serialize_prefix_evidence,
)
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.storage.database import SCHEMA_VERSION, open_initialized_database

BASE = datetime(2026, 1, 1, tzinfo=UTC)
TIMEFRAME = "5m"
INVALIDATION = "Closed structure beyond the stored stop invalidates the plan."
PROCESSED_AT = (BASE + timedelta(hours=3)).isoformat()
BOOKKEEPING = {"id", "created_at", "updated_at"}


def _candle(index: int, *, high: str, low: str, close_timestamp=None) -> dict[str, object]:
    opened = BASE + timedelta(minutes=5 * index)
    payload = {
        "timestamp": int(opened.timestamp() * 1000),
        "open": Decimal(low),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(high),
        "volume": Decimal("10"),
    }
    if close_timestamp is not None:
        payload["close_timestamp"] = close_timestamp
    return payload


def _close(index: int) -> datetime:
    return BASE + timedelta(minutes=5 * (index + 1))


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


def _envelope(row: dict[str, object]) -> dict[str, object]:
    raw = row["last_eligibility_prefix_evidence_json"]
    assert raw is not None
    payload = json.loads(str(raw))
    assert isinstance(payload, dict)
    return payload


def _comparable(row: dict[str, object]) -> dict[str, object]:
    return {key: value for key, value in row.items() if key not in BOOKKEEPING}


def _project(record, progress, **provenance):
    values = {
        "synthetic": True,
        "label": "p3-prefix-synthetic",
        "source_namespace": "synthetic-p3-prefix",
        "coverage_complete": False,
    }
    values.update(provenance)
    return project_outcome_ownership(
        lifecycle_records=[record],
        progress_rows=[progress] if progress is not None else [],
        provenance=values,
    )


def _tp_prefix() -> list[dict[str, object]]:
    return [
        _candle(0, high="99", low="95"),
        _candle(1, high="103", low="99"),
        _candle(2, high="111", low="103"),
        _candle(3, high="121", low="111"),
        _candle(4, high="131", low="121"),
    ]


def test_qualifying_write_persists_bound_envelope_distinct_from_processing_time(tmp_path: Path) -> None:
    record = _latched()
    cutoff = _close(1) + timedelta(minutes=1)
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    db_path = tmp_path / "roundtrip.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        result = _evaluate(repository, record, candles, decision_timestamp=cutoff)
        row = _sql_progress(repository, record.lifecycle_id)[0]
        loaded = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
    envelope = _envelope(row)
    assert result.progress.last_evaluated_at == PROCESSED_AT
    assert row["last_evaluated_at"] == PROCESSED_AT
    assert row["last_eligibility_decision_at"] == cutoff.isoformat()
    assert loaded.last_eligibility_decision_at == cutoff.isoformat()
    assert loaded.last_eligibility_prefix_evidence_json == row["last_eligibility_prefix_evidence_json"]
    assert loaded.eligibility_cutoff_observed is False
    dumped = loaded.model_dump()
    assert "last_eligibility_decision_at" not in dumped
    assert "last_eligibility_prefix_evidence_json" not in dumped
    assert "eligibility_cutoff_observed" not in dumped
    assert envelope["contract_version"] == PREFIX_EVIDENCE_CONTRACT_VERSION
    assert envelope["lifecycle_id"] == record.lifecycle_id
    assert envelope["plan_identity"] == row["plan_identity"]
    assert envelope["applied_cutoff"] == cutoff.isoformat()
    assert envelope["execution_timeframe"] == TIMEFRAME
    assert envelope["supplied_window"]["count"] == 2
    assert envelope["pending_suffix"]["established"] is True
    assert envelope["pending_suffix"]["count"] == 2
    assert envelope["completed_work"]["count"] == 2
    assert envelope["disposition"] == DISPOSITION_PENDING_SUFFIX_EXHAUSTED
    assert envelope["pending_suffix_exhausted"] is True
    assert normalize_utc_timestamp(row["last_eligibility_decision_at"], field_name="cutoff") == cutoff

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        reopened = _sql_progress(repository, record.lifecycle_id)[0]
    assert reopened["last_eligibility_prefix_evidence_json"] == row["last_eligibility_prefix_evidence_json"]
    assert reopened["last_eligibility_decision_at"] == cutoff.isoformat()


def test_empty_returned_window_and_waiting_and_no_new_pending(tmp_path: Path) -> None:
    cutoff = BASE + timedelta(minutes=1)
    with SQLiteSetupLifecycleRepository(tmp_path / "empty.db") as repository:
        empty_record = _latched(lifecycle_id="life-empty")
        repository.upsert_record(empty_record)
        empty = _evaluate(
            repository,
            empty_record,
            [_candle(0, high="99", low="95")],
            decision_timestamp=cutoff,
        )
        empty_row = _sql_progress(repository, empty_record.lifecycle_id)[0]
    empty_envelope = _envelope(empty_row)
    assert empty.progress.diagnostic == "no_closed_execution_candles_at_decision_boundary"
    assert empty_row["last_eligibility_decision_at"] == cutoff.isoformat()
    assert empty_envelope["disposition"] == DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES
    assert empty_envelope["supplied_window"]["count"] == 0
    assert empty_envelope["supplied_window"]["first_open_at"] is None
    assert empty_envelope["pending_suffix"]["established"] is False
    assert empty_envelope["pending_suffix_exhausted"] is None

    with SQLiteSetupLifecycleRepository(tmp_path / "wait.db") as repository:
        waiting_record = _latched(
            lifecycle_id="life-wait",
            confirmed_at=(BASE + timedelta(minutes=1)).isoformat(),
        )
        repository.upsert_record(waiting_record)
        waiting = _evaluate(
            repository,
            waiting_record,
            [_candle(0, high="99", low="95")],
            decision_timestamp=_close(0),
        )
        waiting_row = _sql_progress(repository, waiting_record.lifecycle_id)[0]
    waiting_envelope = _envelope(waiting_row)
    assert waiting.processed_candles == 0
    assert waiting.progress.integrity_status == "Verified"
    assert waiting_envelope["disposition"] == DISPOSITION_WAITING_FOR_TRACKING_START
    assert waiting_envelope["pending_suffix"]["established"] is True
    assert waiting_envelope["pending_suffix"]["count"] == 0
    assert waiting_envelope["pending_suffix_exhausted"] is None

    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    cutoff_a = _close(1) + timedelta(minutes=1)
    cutoff_b = _close(1) + timedelta(minutes=3)
    with SQLiteSetupLifecycleRepository(tmp_path / "noop.db") as repository:
        record = _latched(lifecycle_id="life-noop")
        repository.upsert_record(record)
        first = _evaluate(repository, record, candles, decision_timestamp=cutoff_a)
        first_envelope = _envelope(_sql_progress(repository, record.lifecycle_id)[0])
        repeat = _evaluate(repository, first.record, candles, decision_timestamp=cutoff_a)
        repeat_row = _sql_progress(repository, record.lifecycle_id)[0]
        newer = _evaluate(repository, repeat.record, candles, decision_timestamp=cutoff_b)
        newer_row = _sql_progress(repository, record.lifecycle_id)[0]
    repeat_envelope = _envelope(repeat_row)
    newer_envelope = _envelope(newer_row)
    assert first_envelope["disposition"] == DISPOSITION_PENDING_SUFFIX_EXHAUSTED
    assert repeat.processed_candles == 0
    assert repeat_envelope["disposition"] == DISPOSITION_NO_NEW_PENDING_CANDLES
    assert repeat_envelope["completed_work"]["count"] == 0
    assert repeat_envelope["cursor_before"]["open_at"] == first.progress.evaluation_cursor_open_at
    assert repeat_envelope["cursor_after"]["open_at"] == first.progress.evaluation_cursor_open_at
    assert repeat_envelope["pending_suffix_exhausted"] is True
    assert newer_envelope["applied_cutoff"] == cutoff_b.isoformat()
    assert newer_row["last_eligibility_decision_at"] == cutoff_b.isoformat()
    assert newer_envelope["disposition"] == DISPOSITION_NO_NEW_PENDING_CANDLES


def test_terminal_at_end_versus_terminal_before_eligible_tail(tmp_path: Path) -> None:
    prefix = _tp_prefix()
    tail = _candle(5, high="132", low="130")
    cutoff = _close(5)
    with SQLiteSetupLifecycleRepository(tmp_path / "end.db") as repository:
        end_record = _latched(lifecycle_id="life-end")
        repository.upsert_record(end_record)
        end = _evaluate(repository, end_record, prefix, decision_timestamp=cutoff)
        end_row = _sql_progress(repository, end_record.lifecycle_id)[0]
        end_events = repository._connection.execute(
            """
            SELECT from_state, to_state, reason, timestamp, notes
            FROM setup_lifecycle_events WHERE lifecycle_id = ? ORDER BY event_id
            """,
            (end_record.lifecycle_id,),
        ).fetchall()
    with SQLiteSetupLifecycleRepository(tmp_path / "tail.db") as repository:
        tail_record = _latched(lifecycle_id="life-end")
        repository.upsert_record(tail_record)
        extra = _evaluate(repository, tail_record, prefix + [tail], decision_timestamp=cutoff)
        extra_row = _sql_progress(repository, tail_record.lifecycle_id)[0]
        extra_events = repository._connection.execute(
            """
            SELECT from_state, to_state, reason, timestamp, notes
            FROM setup_lifecycle_events WHERE lifecycle_id = ? ORDER BY event_id
            """,
            (tail_record.lifecycle_id,),
        ).fetchall()
    end_old = _comparable(end_row)
    extra_old = _comparable(extra_row)
    end_old.pop("last_eligibility_prefix_evidence_json")
    extra_old.pop("last_eligibility_prefix_evidence_json")
    assert end_old == extra_old
    assert end_events == extra_events
    assert end.progress.model_dump() == extra.progress.model_dump()
    assert end.progress.terminal_outcome == extra.progress.terminal_outcome == SetupLifecycleState.TP_HIT.value
    end_envelope = _envelope(end_row)
    extra_envelope = _envelope(extra_row)
    assert end_envelope["supplied_window"]["count"] == 5
    assert extra_envelope["supplied_window"]["count"] == 6
    assert end_envelope["pending_suffix"]["count"] == 5
    assert extra_envelope["pending_suffix"]["count"] == 6
    assert end_envelope["completed_work"]["count"] == extra_envelope["completed_work"]["count"] == 5
    assert end_envelope["disposition"] == extra_envelope["disposition"] == DISPOSITION_POLICY_TERMINAL
    assert end_envelope["pending_suffix_exhausted"] is True
    assert extra_envelope["pending_suffix_exhausted"] is False
    payload = _project(end_record, end.progress)
    assert payload["evaluations"][0]["evaluation_context"]["complete"] is False
    assert payload["durable_evaluation_context"]["established"] is False
    assert payload["canonical_outcome_per_plan_version"]["established"] is False
    assert payload["unavailable_metrics"]["unique_trade_count"]["status"] == UNAVAILABLE
    assert payload["unavailable_metrics"]["fill_occurrence_count"]["status"] == UNAVAILABLE


def test_tp_and_stop_terminals_preserve_existing_policy(tmp_path: Path) -> None:
    with SQLiteSetupLifecycleRepository(tmp_path / "tp-last.db") as repository:
        record = _latched(lifecycle_id="life-tp-last")
        repository.upsert_record(record)
        result = _evaluate(repository, record, _tp_prefix(), decision_timestamp=_close(4))
        row = _sql_progress(repository, record.lifecycle_id)[0]
    envelope = _envelope(row)
    assert result.progress.terminal_outcome == SetupLifecycleState.TP_HIT.value
    assert envelope["disposition"] == DISPOSITION_POLICY_TERMINAL
    assert envelope["pending_suffix_exhausted"] is True
    assert envelope["completed_work"]["last_close_at"] == _close(4).isoformat()

    stop_candles = [
        _candle(0, high="99", low="95"),
        _candle(1, high="103", low="99"),
        _candle(2, high="105", low="89"),
        _candle(3, high="106", low="100"),
    ]
    with SQLiteSetupLifecycleRepository(tmp_path / "sl.db") as repository:
        record = _latched(lifecycle_id="life-sl")
        repository.upsert_record(record)
        result = _evaluate(repository, record, stop_candles, decision_timestamp=_close(3))
        row = _sql_progress(repository, record.lifecycle_id)[0]
    envelope = _envelope(row)
    assert result.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert result.progress.entry_at == _close(1).isoformat()
    assert envelope["disposition"] == DISPOSITION_POLICY_TERMINAL
    assert envelope["pending_suffix"]["count"] == 4
    assert envelope["completed_work"]["count"] == 3
    assert envelope["pending_suffix_exhausted"] is False

    same_candle = [_candle(0, high="99", low="95"), _candle(1, high="103", low="89")]
    with SQLiteSetupLifecycleRepository(tmp_path / "same.db") as repository:
        record = _latched(lifecycle_id="life-same")
        repository.upsert_record(record)
        result = _evaluate(repository, record, same_candle, decision_timestamp=_close(1))
        row = _sql_progress(repository, record.lifecycle_id)[0]
    envelope = _envelope(row)
    assert result.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert envelope["disposition"] == DISPOSITION_POLICY_TERMINAL
    assert envelope["pending_suffix_exhausted"] is True


def test_post_filter_blocks_do_not_infer_completion(tmp_path: Path) -> None:
    first_candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    with SQLiteSetupLifecycleRepository(tmp_path / "stale.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        first = _evaluate(repository, record, first_candles, decision_timestamp=_close(1))
        stale = _evaluate(
            repository,
            first.record,
            [_candle(0, high="99", low="95")],
            decision_timestamp=_close(0),
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    envelope = _envelope(row)
    assert "stale_execution_candle_history" in stale.progress.diagnostic
    assert stale.processed_candles == 0
    assert envelope["disposition"] == DISPOSITION_POST_FILTER_BLOCKED
    assert envelope["pending_suffix"]["established"] is False
    assert envelope["completed_work"]["count"] == 0
    assert envelope["pending_suffix_exhausted"] is None
    assert envelope["cursor_after"]["open_at"] == first.progress.evaluation_cursor_open_at

    with SQLiteSetupLifecycleRepository(tmp_path / "gap.db") as repository:
        record = _latched(lifecycle_id="life-gap")
        repository.upsert_record(record)
        seeded = _evaluate(repository, record, first_candles, decision_timestamp=_close(1))
        gapped = _evaluate(
            repository,
            seeded.record,
            [_candle(4, high="111", low="103"), _candle(5, high="112", low="110")],
            decision_timestamp=_close(5),
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    envelope = _envelope(row)
    assert "expected_open=" in gapped.progress.diagnostic
    assert envelope["disposition"] == DISPOSITION_POST_FILTER_BLOCKED
    assert envelope["pending_suffix"]["established"] is True
    assert envelope["pending_suffix"]["count"] == 2
    assert envelope["completed_work"]["count"] == 0
    assert envelope["pending_suffix_exhausted"] is None

    with SQLiteSetupLifecycleRepository(tmp_path / "range.db") as repository:
        record = _latched(lifecycle_id="life-range")
        repository.upsert_record(record)
        failed = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="90", low="103")],
            decision_timestamp=_close(1),
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    envelope = _envelope(row)
    assert "invalid_ohlc_range" in failed.progress.diagnostic
    assert envelope["disposition"] == DISPOSITION_POST_FILTER_BLOCKED
    assert envelope["supplied_window"]["count"] == 2
    assert envelope["pending_suffix"]["established"] is False
    assert envelope["pending_suffix_exhausted"] is None


def test_processing_abort_fault_injection_does_not_count_attempted_as_completed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.lifecycle import outcomes as outcomes_module

    def _fail_managing(record, **kwargs):
        del kwargs
        return record, None, ()

    monkeypatch.setattr(outcomes_module, "_advance_to_managing", _fail_managing)
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    with SQLiteSetupLifecycleRepository(tmp_path / "abort.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        result = _evaluate(repository, record, candles, decision_timestamp=_close(1))
        row = _sql_progress(repository, record.lifecycle_id)[0]
    envelope = _envelope(row)
    assert "entry_state_progression_failed" in result.progress.diagnostic
    assert result.processed_candles == 2
    assert envelope["disposition"] == DISPOSITION_PROCESSING_ABORTED
    assert envelope["pending_suffix"]["count"] == 2
    assert envelope["completed_work"]["count"] == 1
    assert envelope["pending_suffix_exhausted"] is False
    assert envelope["completed_work"]["count"] != result.processed_candles


def test_pre_filter_paths_preserve_prior_tuple(tmp_path: Path) -> None:
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    with SQLiteSetupLifecycleRepository(tmp_path / "prefilter.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        first = _evaluate(repository, record, candles, decision_timestamp=_close(1) + timedelta(minutes=1))
        first_row = _sql_progress(repository, record.lifecycle_id)[0]
        mismatched = _evaluate(
            repository,
            first.record,
            candles,
            decision_timestamp=_close(1),
            execution_timeframe="15m",
        )
        mismatched_row = _sql_progress(repository, record.lifecycle_id)[0]
        gapped = _evaluate(
            repository,
            first.record,
            [_candle(0, high="99", low="95"), _candle(2, high="103", low="99")],
            decision_timestamp=_close(2),
        )
        gapped_row = _sql_progress(repository, record.lifecycle_id)[0]
    assert "execution_timeframe_changed" in mismatched.progress.diagnostic
    assert mismatched_row["last_eligibility_decision_at"] == first_row["last_eligibility_decision_at"]
    assert (
        mismatched_row["last_eligibility_prefix_evidence_json"]
        == first_row["last_eligibility_prefix_evidence_json"]
    )
    assert "continuity_gap" in gapped.progress.diagnostic
    assert gapped_row["last_eligibility_prefix_evidence_json"] == first_row["last_eligibility_prefix_evidence_json"]

    fresh = _latched(lifecycle_id="life-term-fresh", current_state=SetupLifecycleState.INVALIDATED)
    with SQLiteSetupLifecycleRepository(tmp_path / "term-fresh.db") as repository:
        repository.upsert_record(fresh)
        _evaluate(repository, fresh, candles, decision_timestamp=_close(0))
        row = _sql_progress(repository, fresh.lifecycle_id)[0]
    assert row["terminal_outcome"] == SetupLifecycleState.INVALIDATED.value
    assert row["last_eligibility_decision_at"] is None
    assert row["last_eligibility_prefix_evidence_json"] is None

    live = _latched(lifecycle_id="life-term-existing")
    with SQLiteSetupLifecycleRepository(tmp_path / "term-existing.db") as repository:
        repository.upsert_record(live)
        first = _evaluate(repository, live, candles, decision_timestamp=_close(1) + timedelta(minutes=1))
        prior = _sql_progress(repository, live.lifecycle_id)[0]
        terminal_record = first.record.model_copy(update={"current_state": SetupLifecycleState.EXPIRED})
        repository.upsert_record(terminal_record)
        _evaluate(repository, terminal_record, candles, decision_timestamp=_close(2))
        row = _sql_progress(repository, live.lifecycle_id)[0]
    assert row["terminal_outcome"] == SetupLifecycleState.EXPIRED.value
    assert row["last_eligibility_decision_at"] == prior["last_eligibility_decision_at"]
    assert row["last_eligibility_prefix_evidence_json"] == prior["last_eligibility_prefix_evidence_json"]
    payload = _project(terminal_record, row)
    assert payload["evaluations"][0]["evaluation_context"]["complete"] is False
    assert payload["evaluations"][0]["evaluation_context"]["pending_suffix_exhausted"] is True


def test_stale_reload_and_equal_cutoff_replacement(tmp_path: Path) -> None:
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    first_cutoff = _close(1) + timedelta(minutes=1)
    second_cutoff = _close(1) + timedelta(minutes=3)
    with SQLiteSetupLifecycleRepository(tmp_path / "stale-obj.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        first = _evaluate(repository, record, candles, decision_timestamp=first_cutoff)
        stale = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
        _evaluate(repository, first.record, candles, decision_timestamp=second_cutoff)
        newer = _sql_progress(repository, record.lifecycle_id)[0]
        mutated = stale.model_copy(
            update={
                "last_evaluated_at": PROCESSED_AT,
                "last_eligibility_decision_at": first_cutoff.isoformat(),
                "last_eligibility_prefix_evidence_json": stale.last_eligibility_prefix_evidence_json,
                "eligibility_cutoff_observed": False,
            }
        )
        repository.upsert_outcome_progress(mutated)
        after_stale = _sql_progress(repository, record.lifecycle_id)[0]
        loaded = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
        repository.upsert_outcome_progress(loaded)
        after_reload = _sql_progress(repository, record.lifecycle_id)[0]
        dumped = json.loads(json.dumps(loaded.model_dump()))
        reconstructed = SetupLifecycleOutcomeProgress(
            **{
                **dumped,
                "first_evaluated_at": loaded.first_evaluated_at,
                "last_evaluated_at": loaded.last_evaluated_at,
            }
        )
        repository.upsert_outcome_progress(reconstructed)
        after_dump = _sql_progress(repository, record.lifecycle_id)[0]
    assert after_stale["last_eligibility_decision_at"] == second_cutoff.isoformat()
    assert after_stale["last_eligibility_prefix_evidence_json"] == newer["last_eligibility_prefix_evidence_json"]
    assert after_reload["last_eligibility_prefix_evidence_json"] == newer["last_eligibility_prefix_evidence_json"]
    assert after_dump["last_eligibility_prefix_evidence_json"] == newer["last_eligibility_prefix_evidence_json"]

    equal_cutoff = _close(2)
    with SQLiteSetupLifecycleRepository(tmp_path / "equal.db") as repository:
        record = _latched(lifecycle_id="life-equal")
        repository.upsert_record(record)
        first = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
            decision_timestamp=equal_cutoff,
        )
        first_row = _sql_progress(repository, record.lifecycle_id)[0]
        second = _evaluate(
            repository,
            first.record,
            [
                _candle(0, high="99", low="95"),
                _candle(1, high="103", low="99"),
                _candle(2, high="104", low="100"),
            ],
            decision_timestamp=equal_cutoff,
        )
        second_row = _sql_progress(repository, record.lifecycle_id)[0]
        missing = first.progress.model_copy(
            update={
                "last_eligibility_decision_at": equal_cutoff.isoformat(),
                "last_eligibility_prefix_evidence_json": None,
                "eligibility_cutoff_observed": True,
                "last_evaluated_at": PROCESSED_AT,
            }
        )
        repository.upsert_outcome_progress(missing)
        missing_row = _sql_progress(repository, record.lifecycle_id)[0]
    assert first_row["last_eligibility_decision_at"] == second_row["last_eligibility_decision_at"] == equal_cutoff.isoformat()
    assert _envelope(first_row)["supplied_window"]["count"] == 2
    assert _envelope(second_row)["supplied_window"]["count"] == 3
    assert second.processed_candles == 1
    assert missing_row["last_eligibility_decision_at"] == equal_cutoff.isoformat()
    assert missing_row["last_eligibility_prefix_evidence_json"] is None


def test_scan_run_and_material_replacement_and_shared_plan_version(tmp_path: Path) -> None:
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    cutoff = _close(1) + timedelta(minutes=1)
    with SQLiteSetupLifecycleRepository(tmp_path / "scan.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        first = _evaluate(repository, record, candles, decision_timestamp=cutoff, scan_run_id="scan-1")
        continued = _evaluate(
            repository,
            first.record,
            candles,
            decision_timestamp=cutoff + timedelta(minutes=1),
            scan_run_id="scan-2",
        )
        none_pass = _evaluate(
            repository,
            continued.record,
            candles,
            decision_timestamp=cutoff + timedelta(minutes=1),
            scan_run_id=None,
        )
        rows = _sql_progress(repository, record.lifecycle_id)
    assert len(rows) == 1
    assert none_pass.processed_candles == 0
    assert _envelope(rows[0])["lifecycle_id"] == record.lifecycle_id

    first = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "replace.db") as repository:
        repository.upsert_record(first)
        entered = _evaluate(repository, first, candles, decision_timestamp=_close(1))
        old_identity = entered.progress.plan_identity
        old_row = {
            row["plan_identity"]: row for row in _sql_progress(repository, first.lifecycle_id)
        }[old_identity]
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
        _evaluate(
            repository,
            replacement,
            [_candle(0, high="199", low="195")],
            decision_timestamp=second_cutoff,
            evaluated_at=(BASE + timedelta(hours=2)).isoformat(),
        )
        rows = {row["plan_identity"]: row for row in _sql_progress(repository, first.lifecycle_id)}
    new_identity = canonical_plan_identity(replacement)
    assert new_identity != old_identity
    assert rows[old_identity]["last_eligibility_prefix_evidence_json"] == old_row["last_eligibility_prefix_evidence_json"]
    assert rows[new_identity]["plan_version_id"] is None
    assert proven_progress_plan_version_id(replacement) is None
    assert _envelope(rows[new_identity])["plan_identity"] == new_identity

    first = _latched(lifecycle_id="life-a")
    second = _latched(lifecycle_id="life-b")
    assert first.plan_version_id == second.plan_version_id
    with SQLiteSetupLifecycleRepository(tmp_path / "shared.db") as repository:
        repository.upsert_record(first)
        _evaluate(repository, first, [_candle(0, high="99", low="95")], decision_timestamp=_close(0))
        repository.supersede_record(first.lifecycle_id)
        repository.upsert_record(second)
        _evaluate(repository, second, [_candle(0, high="99", low="95")], decision_timestamp=_close(0))
        rows = repository._connection.execute(
            """
            SELECT lifecycle_id, last_eligibility_prefix_evidence_json
            FROM setup_lifecycle_outcome_progress ORDER BY lifecycle_id
            """
        ).fetchall()
    assert len(rows) == 2
    first_payload = json.loads(rows[0][1])
    second_payload = json.loads(rows[1][1])
    assert first_payload["lifecycle_id"] == "life-a"
    assert second_payload["lifecycle_id"] == "life-b"


def test_rejected_and_rolled_back_writes_leave_no_orphan_envelope(tmp_path: Path) -> None:
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    with SQLiteSetupLifecycleRepository(tmp_path / "rollback.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        first = _evaluate(repository, record, candles, decision_timestamp=_close(1))
        stored = _sql_progress(repository, record.lifecycle_id)[0]

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
    assert row["last_eligibility_prefix_evidence_json"] == stored["last_eligibility_prefix_evidence_json"]

    with SQLiteSetupLifecycleRepository(tmp_path / "tx.db") as repository:
        record = _latched(lifecycle_id="life-tx")
        repository.upsert_record(record)
        _evaluate(repository, record, candles, decision_timestamp=_close(1))
        repository._connection.commit()
        prior = _sql_progress(repository, record.lifecycle_id)[0]
        repository._connection.execute("BEGIN")
        _evaluate(repository, record, candles, decision_timestamp=_close(1) + timedelta(minutes=2))
        repository._connection.rollback()
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert row["last_eligibility_decision_at"] == prior["last_eligibility_decision_at"]
    assert row["last_eligibility_prefix_evidence_json"] == prior["last_eligibility_prefix_evidence_json"]


def test_p3a_diagnostics_and_stronger_claims_remain_false(tmp_path: Path) -> None:
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
        before = _sql_progress(repository, record.lifecycle_id)[0]
        _project(record, result.progress)
        after = _sql_progress(repository, record.lifecycle_id)[0]
        schema = repository._connection.execute("PRAGMA user_version").fetchone()[0]
    context = known["evaluations"][0]["evaluation_context"]
    assert before == after
    assert schema == SCHEMA_VERSION == 24
    assert context["last_eligibility_prefix_evidence_status"] == PREFIX_EVIDENCE_STATUS_KNOWN
    assert context["prefix_disposition"] == DISPOSITION_PENDING_SUFFIX_EXHAUSTED
    assert context["pending_suffix_exhausted"] is True
    assert context["complete"] is False
    assert known["durable_evaluation_context"]["established"] is False
    assert known["canonical_outcome_per_plan_version"]["established"] is False
    assert known["unavailable_metrics"]["unique_trade_count"]["status"] == UNAVAILABLE
    assert known["unavailable_metrics"]["fill_occurrence_count"]["status"] == UNAVAILABLE
    assert all(item.get("evaluation_context_complete") is not True for item in known.get("plan_interpretations", []))
    artifact = json.loads(dumps_outcome_ownership_report(known))
    assert artifact["durable_evaluation_context"]["established"] is False

    malformed = _project(
        record,
        {
            "lifecycle_id": record.lifecycle_id,
            "plan_identity": canonical_plan_identity(record),
            "plan_version_id": record.plan_version_id,
            "symbol": "BTCUSDT",
            "tracking_start_at": BASE.isoformat(),
            "execution_timeframe": "5m",
            "first_evaluated_at": _close(0).isoformat(),
            "last_evaluated_at": _close(0).isoformat(),
            "last_eligibility_decision_at": cutoff.isoformat(),
            "last_eligibility_prefix_evidence_json": "{not-json",
            "terminal_outcome": NA,
        },
    )
    malformed_context = malformed["evaluations"][0]["evaluation_context"]
    assert malformed_context["last_eligibility_prefix_evidence_status"] == PREFIX_EVIDENCE_STATUS_MALFORMED
    assert malformed_context["last_eligibility_prefix_evidence_raw"] == "{not-json"
    assert malformed_context["prefix_disposition"] is None
    assert malformed_context["complete"] is False

    conflicting = _project(
        record,
        {
            "lifecycle_id": record.lifecycle_id,
            "plan_identity": canonical_plan_identity(record),
            "plan_version_id": record.plan_version_id,
            "symbol": "BTCUSDT",
            "tracking_start_at": BASE.isoformat(),
            "execution_timeframe": "5m",
            "first_evaluated_at": _close(0).isoformat(),
            "last_evaluated_at": _close(0).isoformat(),
            "last_eligibility_decision_at": cutoff.isoformat(),
            "last_eligibility_prefix_evidence_json": json.dumps(
                {
                    "contract_version": PREFIX_EVIDENCE_CONTRACT_VERSION,
                    "lifecycle_id": "other-life",
                    "plan_identity": canonical_plan_identity(record),
                    "applied_cutoff": cutoff.isoformat(),
                    "execution_timeframe": "5m",
                    "tracking_start_at": BASE.isoformat(),
                    "cursor_before": None,
                    "supplied_window": {
                        "count": 0,
                        "first_open_at": None,
                        "last_open_at": None,
                        "last_close_at": None,
                    },
                    "pending_suffix": {
                        "established": False,
                        "count": None,
                        "first_open_at": None,
                        "last_open_at": None,
                        "last_close_at": None,
                        "expected_next_open_at": None,
                        "unknown_reason": "pending_rules_not_evaluated",
                    },
                    "completed_work": {"count": 0, "last_open_at": None, "last_close_at": None},
                    "cursor_after": None,
                    "disposition": DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES,
                    "pending_suffix_exhausted": None,
                },
                sort_keys=True,
                separators=(",", ":"),
            ),
            "terminal_outcome": NA,
        },
    )
    conflicting_context = conflicting["evaluations"][0]["evaluation_context"]
    assert conflicting_context["last_eligibility_prefix_evidence_status"] == PREFIX_EVIDENCE_STATUS_CONFLICTING
    assert conflicting_context["prefix_disposition"] is None
    assert conflicting_context["complete"] is False

    source = Path("app/analytics/outcome_ownership.py").read_text(encoding="utf-8")
    assert "sqlite3" not in source
    assert "open_initialized_database" not in source


def test_ordinary_dumps_and_runtime_consumers_do_not_take_prefix_envelope() -> None:
    import inspect

    from app.alerts import telegram_lifecycle
    from app.analytics import performance_memory, symbol_health
    from app.research import queries

    banned = "last_eligibility_prefix_evidence_json"
    for module in (telegram_lifecycle, symbol_health, performance_memory, queries):
        assert banned not in inspect.getsource(module)
    assert evidence_contract_payload()["outcome_ownership"]["feeds_operational_decisions"] is False
    assert evidence_contract_payload()["unique_trade_count"]["status"] == UNAVAILABLE
    assert evidence_contract_payload()["outcome_ownership"]["schema_version_unchanged"] is True
    assert SCHEMA_VERSION == 24


def test_exchange_close_convention_and_bounded_envelope_size(tmp_path: Path) -> None:
    explicit_close = _close(1) - timedelta(milliseconds=1)
    candles = [
        _candle(0, high="99", low="95"),
        _candle(1, high="103", low="99", close_timestamp=explicit_close),
    ]
    with SQLiteSetupLifecycleRepository(tmp_path / "close.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        _evaluate(repository, record, candles, decision_timestamp=explicit_close)
        row = _sql_progress(repository, record.lifecycle_id)[0]
    envelope = _envelope(row)
    assert envelope["supplied_window"]["last_close_at"] == explicit_close.isoformat()
    assert envelope["completed_work"]["last_close_at"] == explicit_close.isoformat()

    short = [_candle(index, high="99", low="95") for index in range(6)]
    long = [_candle(index, high="99", low="95") for index in range(200)]
    with SQLiteSetupLifecycleRepository(tmp_path / "short.db") as repository:
        record = _latched(lifecycle_id="life-short")
        repository.upsert_record(record)
        _evaluate(repository, record, short, decision_timestamp=_close(5))
        short_json = _sql_progress(repository, record.lifecycle_id)[0]["last_eligibility_prefix_evidence_json"]
    with SQLiteSetupLifecycleRepository(tmp_path / "long.db") as repository:
        record = _latched(lifecycle_id="life-long")
        repository.upsert_record(record)
        _evaluate(repository, record, long, decision_timestamp=_close(199))
        long_row = _sql_progress(repository, record.lifecycle_id)[0]
        long_json = long_row["last_eligibility_prefix_evidence_json"]
        _evaluate(repository, record, long, decision_timestamp=_close(199) + timedelta(minutes=1))
        second_json = _sql_progress(repository, record.lifecycle_id)[0]["last_eligibility_prefix_evidence_json"]
    assert _envelope(long_row)["supplied_window"]["count"] == 200
    assert abs(len(str(short_json)) - len(str(long_json))) < 80
    assert len(str(short_json)) < 1200
    assert len(str(long_json)) < 1200
    assert len(str(second_json)) < 1200
    assert json.loads(str(second_json))["disposition"] == DISPOSITION_NO_NEW_PENDING_CANDLES


def test_v23_to_v24_migration_adds_nullable_envelope_without_backfill(tmp_path: Path) -> None:
    from tests.test_storage_database import _create_schema_v23_outcome_progress_fixture

    db_path = tmp_path / "v23-prefix.db"
    _create_schema_v23_outcome_progress_fixture(db_path)
    with open_initialized_database(db_path):
        pass
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        version = repository._connection.execute("PRAGMA user_version").fetchone()[0]
        rows = repository._connection.execute(
            """
            SELECT lifecycle_id, last_eligibility_decision_at, last_eligibility_prefix_evidence_json,
                   plan_version_id
            FROM setup_lifecycle_outcome_progress ORDER BY lifecycle_id
            """
        ).fetchall()
        loaded = repository.get_outcome_progress(
            lifecycle_id="v23-active-cutoff",
            plan_identity="plan-v23-active-cutoff",
        )
        assert loaded is not None
        assert loaded.last_eligibility_prefix_evidence_json is None
        repository.upsert_outcome_progress(loaded)
        after = repository._connection.execute(
            """
            SELECT last_eligibility_decision_at, last_eligibility_prefix_evidence_json
            FROM setup_lifecycle_outcome_progress WHERE lifecycle_id = 'v23-active-cutoff'
            """
        ).fetchone()
    assert version == SCHEMA_VERSION == 24
    assert {row[2] for row in rows} == {None}
    assert {row[1] for row in rows if row[0] == "v23-active-cutoff"} == {"2026-09-01T10:00:00+00:00"}
    assert after[0] == "2026-09-01T10:00:00+00:00"
    assert after[1] is None
    payload = _project(
        _latched(lifecycle_id="v23-active-cutoff"),
        {
            "lifecycle_id": "v23-active-cutoff",
            "plan_identity": "plan-v23-active-cutoff",
            "symbol": "BTCUSDT",
            "tracking_start_at": "2026-09-01T09:30:00+00:00",
            "execution_timeframe": "15m",
            "first_evaluated_at": "2026-09-01T09:30:00+00:00",
            "last_evaluated_at": "2026-09-01T10:00:00+00:00",
            "last_eligibility_decision_at": "2026-09-01T10:00:00+00:00",
            "terminal_outcome": NA,
        },
    )
    assert payload["evaluations"][0]["evaluation_context"]["last_eligibility_prefix_evidence_status"] == "unavailable"
    assert payload["evaluations"][0]["evaluation_context"]["complete"] is False


def _open_at(index: int) -> str:
    return (BASE + timedelta(minutes=5 * index)).isoformat()


def _close_at(index: int) -> str:
    return _close(index).isoformat()


def _valid_prefix_envelope() -> dict[str, object]:
    return {
        "contract_version": PREFIX_EVIDENCE_CONTRACT_VERSION,
        "lifecycle_id": "life-1",
        "plan_identity": "plan-1",
        "applied_cutoff": _close_at(2),
        "execution_timeframe": "5m",
        "tracking_start_at": _open_at(0),
        "cursor_before": None,
        "supplied_window": {
            "count": 3,
            "first_open_at": _open_at(0),
            "last_open_at": _open_at(2),
            "last_close_at": _close_at(2),
        },
        "pending_suffix": {
            "established": True,
            "count": 3,
            "first_open_at": _open_at(0),
            "last_open_at": _open_at(2),
            "last_close_at": _close_at(2),
            "expected_next_open_at": _open_at(0),
            "unknown_reason": None,
        },
        "completed_work": {
            "count": 3,
            "last_open_at": _open_at(2),
            "last_close_at": _close_at(2),
        },
        "cursor_after": {"open_at": _open_at(2), "close_at": _close_at(2)},
        "disposition": DISPOSITION_PENDING_SUFFIX_EXHAUSTED,
        "pending_suffix_exhausted": True,
    }


def _diagnose_envelope(payload: dict[str, object], **overrides) -> dict[str, object]:
    return diagnose_prefix_evidence(
        raw_json=json.dumps(payload, sort_keys=True, separators=(",", ":")),
        lifecycle_id=str(overrides.get("lifecycle_id", payload["lifecycle_id"])),
        plan_identity=str(overrides.get("plan_identity", payload["plan_identity"])),
        applied_cutoff=str(overrides.get("applied_cutoff", payload["applied_cutoff"])),
        execution_timeframe=str(overrides.get("execution_timeframe", payload["execution_timeframe"])),
    )


def _assert_not_known(payload: dict[str, object]) -> dict[str, object]:
    diagnostic = _diagnose_envelope(payload)
    assert diagnostic["status"] != PREFIX_EVIDENCE_STATUS_KNOWN
    assert diagnostic["status"] in {
        PREFIX_EVIDENCE_STATUS_MALFORMED,
        PREFIX_EVIDENCE_STATUS_CONFLICTING,
    }
    assert diagnostic["disposition"] is None
    assert diagnostic["pending_suffix_exhausted"] is None
    assert serialize_prefix_evidence(payload) is None
    return diagnostic


def test_producer_valid_envelope_remains_known_and_serializable() -> None:
    payload = _valid_prefix_envelope()
    diagnostic = _diagnose_envelope(payload)
    encoded = serialize_prefix_evidence(payload)
    assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
    assert diagnostic["disposition"] == DISPOSITION_PENDING_SUFFIX_EXHAUSTED
    assert diagnostic["pending_suffix_exhausted"] is True
    assert encoded == json.dumps(payload, sort_keys=True, separators=(",", ":"))


def test_contradictory_prefix_envelopes_are_not_known() -> None:
    exhausted_incomplete = copy.deepcopy(_valid_prefix_envelope())
    exhausted_incomplete["completed_work"] = {
        "count": 1,
        "last_open_at": _open_at(0),
        "last_close_at": _close_at(0),
    }
    exhausted_incomplete["cursor_after"] = {"open_at": _open_at(0), "close_at": _close_at(0)}
    assert _assert_not_known(exhausted_incomplete)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    no_new_nonzero_pending = copy.deepcopy(_valid_prefix_envelope())
    no_new_nonzero_pending["disposition"] = DISPOSITION_NO_NEW_PENDING_CANDLES
    no_new_nonzero_pending["pending_suffix_exhausted"] = True
    no_new_nonzero_pending["completed_work"] = {
        "count": 0,
        "last_open_at": None,
        "last_close_at": None,
    }
    no_new_nonzero_pending["cursor_after"] = None
    assert _assert_not_known(no_new_nonzero_pending)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    aborted_exhausted = copy.deepcopy(_valid_prefix_envelope())
    aborted_exhausted["disposition"] = DISPOSITION_PROCESSING_ABORTED
    aborted_exhausted["pending_suffix_exhausted"] = True
    aborted_exhausted["completed_work"] = {
        "count": 1,
        "last_open_at": _open_at(0),
        "last_close_at": _close_at(0),
    }
    aborted_exhausted["cursor_after"] = {"open_at": _open_at(0), "close_at": _close_at(0)}
    assert _assert_not_known(aborted_exhausted)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    terminal_true_but_incomplete = copy.deepcopy(_valid_prefix_envelope())
    terminal_true_but_incomplete["disposition"] = DISPOSITION_POLICY_TERMINAL
    terminal_true_but_incomplete["pending_suffix_exhausted"] = True
    terminal_true_but_incomplete["completed_work"] = {
        "count": 2,
        "last_open_at": _open_at(1),
        "last_close_at": _close_at(1),
    }
    terminal_true_but_incomplete["cursor_after"] = {"open_at": _open_at(1), "close_at": _close_at(1)}
    assert _assert_not_known(terminal_true_but_incomplete)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    terminal_false_but_complete = copy.deepcopy(_valid_prefix_envelope())
    terminal_false_but_complete["disposition"] = DISPOSITION_POLICY_TERMINAL
    terminal_false_but_complete["pending_suffix_exhausted"] = False
    assert _assert_not_known(terminal_false_but_complete)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    empty_window_nonempty_w = {
        "contract_version": PREFIX_EVIDENCE_CONTRACT_VERSION,
        "lifecycle_id": "life-1",
        "plan_identity": "plan-1",
        "applied_cutoff": _close_at(0),
        "execution_timeframe": "5m",
        "tracking_start_at": None,
        "cursor_before": None,
        "supplied_window": {
            "count": 1,
            "first_open_at": _open_at(0),
            "last_open_at": _open_at(0),
            "last_close_at": _close_at(0),
        },
        "pending_suffix": {
            "established": False,
            "count": None,
            "first_open_at": None,
            "last_open_at": None,
            "last_close_at": None,
            "expected_next_open_at": None,
            "unknown_reason": "pending_rules_not_evaluated",
        },
        "completed_work": {"count": 0, "last_open_at": None, "last_close_at": None},
        "cursor_after": None,
        "disposition": DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES,
        "pending_suffix_exhausted": None,
    }
    assert _assert_not_known(empty_window_nonempty_w)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    waiting_exhausted = copy.deepcopy(_valid_prefix_envelope())
    waiting_exhausted["disposition"] = DISPOSITION_WAITING_FOR_TRACKING_START
    waiting_exhausted["pending_suffix_exhausted"] = True
    waiting_exhausted["pending_suffix"] = {
        "established": True,
        "count": 0,
        "first_open_at": None,
        "last_open_at": None,
        "last_close_at": None,
        "expected_next_open_at": _open_at(1),
        "unknown_reason": None,
    }
    waiting_exhausted["completed_work"] = {"count": 0, "last_open_at": None, "last_close_at": None}
    waiting_exhausted["cursor_after"] = None
    assert _assert_not_known(waiting_exhausted)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    blocked_exhausted = copy.deepcopy(_valid_prefix_envelope())
    blocked_exhausted["disposition"] = DISPOSITION_POST_FILTER_BLOCKED
    blocked_exhausted["pending_suffix_exhausted"] = True
    blocked_exhausted["pending_suffix"]["established"] = False
    blocked_exhausted["pending_suffix"]["count"] = None
    blocked_exhausted["pending_suffix"]["first_open_at"] = None
    blocked_exhausted["pending_suffix"]["last_open_at"] = None
    blocked_exhausted["pending_suffix"]["last_close_at"] = None
    blocked_exhausted["pending_suffix"]["unknown_reason"] = "pending_rules_not_evaluated"
    blocked_exhausted["completed_work"] = {"count": 0, "last_open_at": None, "last_close_at": None}
    blocked_exhausted["cursor_after"] = None
    assert _assert_not_known(blocked_exhausted)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    reversed_window = copy.deepcopy(_valid_prefix_envelope())
    reversed_window["supplied_window"]["first_open_at"] = _open_at(2)
    reversed_window["supplied_window"]["last_open_at"] = _open_at(0)
    reversed_window["supplied_window"]["last_close_at"] = _close_at(0)
    assert _assert_not_known(reversed_window)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    completed_after_pending = copy.deepcopy(_valid_prefix_envelope())
    completed_after_pending["completed_work"] = {
        "count": 3,
        "last_open_at": _open_at(5),
        "last_close_at": _close_at(5),
    }
    completed_after_pending["cursor_after"] = {"open_at": _open_at(5), "close_at": _close_at(5)}
    assert _assert_not_known(completed_after_pending)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    impossible_cursor = copy.deepcopy(_valid_prefix_envelope())
    impossible_cursor["cursor_after"] = {"open_at": _close_at(2), "close_at": _open_at(2)}
    assert _assert_not_known(impossible_cursor)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    half_cursor = copy.deepcopy(_valid_prefix_envelope())
    half_cursor["cursor_after"] = {"open_at": _open_at(2), "close_at": None}
    assert _assert_not_known(half_cursor)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    no_eligible_with_work = copy.deepcopy(_valid_prefix_envelope())
    no_eligible_with_work["disposition"] = DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES
    no_eligible_with_work["pending_suffix_exhausted"] = None
    assert _assert_not_known(no_eligible_with_work)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    no_eligible_exhausted_false = {
        "contract_version": PREFIX_EVIDENCE_CONTRACT_VERSION,
        "lifecycle_id": "life-1",
        "plan_identity": "plan-1",
        "applied_cutoff": _close_at(0),
        "execution_timeframe": "5m",
        "tracking_start_at": None,
        "cursor_before": None,
        "supplied_window": {
            "count": 0,
            "first_open_at": None,
            "last_open_at": None,
            "last_close_at": None,
        },
        "pending_suffix": {
            "established": False,
            "count": None,
            "first_open_at": None,
            "last_open_at": None,
            "last_close_at": None,
            "expected_next_open_at": None,
            "unknown_reason": "pending_rules_not_evaluated",
        },
        "completed_work": {"count": 0, "last_open_at": None, "last_close_at": None},
        "cursor_after": None,
        "disposition": DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES,
        "pending_suffix_exhausted": False,
    }
    assert _assert_not_known(no_eligible_exhausted_false)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    zero_work_exhausted = copy.deepcopy(_valid_prefix_envelope())
    zero_work_exhausted["disposition"] = DISPOSITION_PENDING_SUFFIX_EXHAUSTED
    zero_work_exhausted["pending_suffix_exhausted"] = True
    zero_work_exhausted["pending_suffix"] = {
        "established": True,
        "count": 0,
        "first_open_at": None,
        "last_open_at": None,
        "last_close_at": None,
        "expected_next_open_at": _open_at(3),
        "unknown_reason": None,
    }
    zero_work_exhausted["completed_work"] = {"count": 0, "last_open_at": None, "last_close_at": None}
    zero_work_exhausted["cursor_before"] = {"open_at": _open_at(2), "close_at": _close_at(2)}
    zero_work_exhausted["cursor_after"] = {"open_at": _open_at(2), "close_at": _close_at(2)}
    assert _assert_not_known(zero_work_exhausted)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    aborted_empty_pending = copy.deepcopy(zero_work_exhausted)
    aborted_empty_pending["disposition"] = DISPOSITION_PROCESSING_ABORTED
    aborted_empty_pending["pending_suffix_exhausted"] = False
    assert _assert_not_known(aborted_empty_pending)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    aborted_complete = copy.deepcopy(_valid_prefix_envelope())
    aborted_complete["disposition"] = DISPOSITION_PROCESSING_ABORTED
    aborted_complete["pending_suffix_exhausted"] = False
    assert _assert_not_known(aborted_complete)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    terminal_empty_pending = copy.deepcopy(zero_work_exhausted)
    terminal_empty_pending["disposition"] = DISPOSITION_POLICY_TERMINAL
    terminal_empty_pending["pending_suffix_exhausted"] = True
    assert _assert_not_known(terminal_empty_pending)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    waiting_unestablished = copy.deepcopy(_valid_prefix_envelope())
    waiting_unestablished["disposition"] = DISPOSITION_WAITING_FOR_TRACKING_START
    waiting_unestablished["pending_suffix_exhausted"] = None
    waiting_unestablished["pending_suffix"] = {
        "established": False,
        "count": None,
        "first_open_at": None,
        "last_open_at": None,
        "last_close_at": None,
        "expected_next_open_at": None,
        "unknown_reason": "pending_rules_not_evaluated",
    }
    waiting_unestablished["completed_work"] = {"count": 0, "last_open_at": None, "last_close_at": None}
    waiting_unestablished["cursor_after"] = None
    assert _assert_not_known(waiting_unestablished)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    waiting_exhausted_false = copy.deepcopy(_valid_prefix_envelope())
    waiting_exhausted_false["disposition"] = DISPOSITION_WAITING_FOR_TRACKING_START
    waiting_exhausted_false["pending_suffix_exhausted"] = False
    waiting_exhausted_false["pending_suffix"] = {
        "established": True,
        "count": 0,
        "first_open_at": None,
        "last_open_at": None,
        "last_close_at": None,
        "expected_next_open_at": _open_at(1),
        "unknown_reason": None,
    }
    waiting_exhausted_false["completed_work"] = {"count": 0, "last_open_at": None, "last_close_at": None}
    waiting_exhausted_false["cursor_after"] = None
    assert _assert_not_known(waiting_exhausted_false)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    blocked_with_completed = copy.deepcopy(_valid_prefix_envelope())
    blocked_with_completed["disposition"] = DISPOSITION_POST_FILTER_BLOCKED
    blocked_with_completed["pending_suffix_exhausted"] = None
    assert _assert_not_known(blocked_with_completed)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED

    blocked_exhausted_false = copy.deepcopy(_valid_prefix_envelope())
    blocked_exhausted_false["disposition"] = DISPOSITION_POST_FILTER_BLOCKED
    blocked_exhausted_false["pending_suffix_exhausted"] = False
    blocked_exhausted_false["pending_suffix"] = {
        "established": False,
        "count": None,
        "first_open_at": None,
        "last_open_at": None,
        "last_close_at": None,
        "expected_next_open_at": None,
        "unknown_reason": "pending_rules_not_evaluated",
    }
    blocked_exhausted_false["completed_work"] = {"count": 0, "last_open_at": None, "last_close_at": None}
    blocked_exhausted_false["cursor_after"] = None
    assert _assert_not_known(blocked_exhausted_false)["status"] == PREFIX_EVIDENCE_STATUS_MALFORMED


def test_legal_policy_terminal_and_unassessable_dispositions_remain_known() -> None:
    early_terminal = copy.deepcopy(_valid_prefix_envelope())
    early_terminal["disposition"] = DISPOSITION_POLICY_TERMINAL
    early_terminal["pending_suffix_exhausted"] = False
    early_terminal["completed_work"] = {
        "count": 2,
        "last_open_at": _open_at(1),
        "last_close_at": _close_at(1),
    }
    early_terminal["cursor_after"] = {"open_at": _open_at(1), "close_at": _close_at(1)}
    early = _diagnose_envelope(early_terminal)
    assert early["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
    assert early["pending_suffix_exhausted"] is False

    waiting = {
        "contract_version": PREFIX_EVIDENCE_CONTRACT_VERSION,
        "lifecycle_id": "life-1",
        "plan_identity": "plan-1",
        "applied_cutoff": _close_at(0),
        "execution_timeframe": "5m",
        "tracking_start_at": _open_at(1),
        "cursor_before": None,
        "supplied_window": {
            "count": 1,
            "first_open_at": _open_at(0),
            "last_open_at": _open_at(0),
            "last_close_at": _close_at(0),
        },
        "pending_suffix": {
            "established": True,
            "count": 0,
            "first_open_at": None,
            "last_open_at": None,
            "last_close_at": None,
            "expected_next_open_at": _open_at(1),
            "unknown_reason": None,
        },
        "completed_work": {"count": 0, "last_open_at": None, "last_close_at": None},
        "cursor_after": None,
        "disposition": DISPOSITION_WAITING_FOR_TRACKING_START,
        "pending_suffix_exhausted": None,
    }
    assert _diagnose_envelope(waiting)["status"] == PREFIX_EVIDENCE_STATUS_KNOWN

    no_eligible = copy.deepcopy(waiting)
    no_eligible["disposition"] = DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES
    no_eligible["supplied_window"] = {
        "count": 0,
        "first_open_at": None,
        "last_open_at": None,
        "last_close_at": None,
    }
    no_eligible["pending_suffix"] = {
        "established": False,
        "count": None,
        "first_open_at": None,
        "last_open_at": None,
        "last_close_at": None,
        "expected_next_open_at": None,
        "unknown_reason": "pending_rules_not_evaluated",
    }
    assert _diagnose_envelope(no_eligible)["status"] == PREFIX_EVIDENCE_STATUS_KNOWN

    no_new = copy.deepcopy(_valid_prefix_envelope())
    no_new["disposition"] = DISPOSITION_NO_NEW_PENDING_CANDLES
    no_new["pending_suffix_exhausted"] = True
    no_new["pending_suffix"] = {
        "established": True,
        "count": 0,
        "first_open_at": None,
        "last_open_at": None,
        "last_close_at": None,
        "expected_next_open_at": _open_at(3),
        "unknown_reason": None,
    }
    no_new["completed_work"] = {"count": 0, "last_open_at": None, "last_close_at": None}
    no_new["cursor_before"] = {"open_at": _open_at(2), "close_at": _close_at(2)}
    no_new["cursor_after"] = {"open_at": _open_at(2), "close_at": _close_at(2)}
    assert _diagnose_envelope(no_new)["status"] == PREFIX_EVIDENCE_STATUS_KNOWN

    aborted = copy.deepcopy(_valid_prefix_envelope())
    aborted["disposition"] = DISPOSITION_PROCESSING_ABORTED
    aborted["pending_suffix_exhausted"] = False
    aborted["completed_work"] = {
        "count": 1,
        "last_open_at": _open_at(0),
        "last_close_at": _close_at(0),
    }
    aborted["cursor_after"] = {"open_at": _open_at(0), "close_at": _close_at(0)}
    assert _diagnose_envelope(aborted)["status"] == PREFIX_EVIDENCE_STATUS_KNOWN

    blocked_unestablished = copy.deepcopy(_valid_prefix_envelope())
    blocked_unestablished["disposition"] = DISPOSITION_POST_FILTER_BLOCKED
    blocked_unestablished["pending_suffix_exhausted"] = None
    blocked_unestablished["pending_suffix"] = {
        "established": False,
        "count": None,
        "first_open_at": None,
        "last_open_at": None,
        "last_close_at": None,
        "expected_next_open_at": None,
        "unknown_reason": "pending_rules_not_evaluated",
    }
    blocked_unestablished["completed_work"] = {"count": 0, "last_open_at": None, "last_close_at": None}
    blocked_unestablished["cursor_after"] = None
    assert _diagnose_envelope(blocked_unestablished)["status"] == PREFIX_EVIDENCE_STATUS_KNOWN

    blocked_established = copy.deepcopy(blocked_unestablished)
    blocked_established["pending_suffix"] = {
        "established": True,
        "count": 2,
        "first_open_at": _open_at(1),
        "last_open_at": _open_at(2),
        "last_close_at": _close_at(2),
        "expected_next_open_at": _open_at(1),
        "unknown_reason": None,
    }
    blocked_established["cursor_before"] = {"open_at": _open_at(0), "close_at": _close_at(0)}
    blocked_established["cursor_after"] = {"open_at": _open_at(0), "close_at": _close_at(0)}
    assert _diagnose_envelope(blocked_established)["status"] == PREFIX_EVIDENCE_STATUS_KNOWN


def _diagnose_row(row: dict[str, object]) -> dict[str, object]:
    raw = row["last_eligibility_prefix_evidence_json"]
    cutoff = row["last_eligibility_decision_at"]
    return diagnose_prefix_evidence(
        raw_json=None if raw is None else str(raw),
        lifecycle_id=str(row["lifecycle_id"]),
        plan_identity=str(row["plan_identity"]),
        applied_cutoff=None if cutoff is None else str(cutoff),
        execution_timeframe=str(row["execution_timeframe"]),
    )


def test_producer_generated_dispositions_diagnose_known(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]

    with SQLiteSetupLifecycleRepository(tmp_path / "empty.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95")],
            decision_timestamp=BASE + timedelta(minutes=1),
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
        diagnostic = _diagnose_row(row)
        assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
        assert diagnostic["disposition"] == DISPOSITION_NO_ELIGIBLE_CLOSED_CANDLES
        assert diagnostic["pending_suffix_exhausted"] is None

    with SQLiteSetupLifecycleRepository(tmp_path / "wait.db") as repository:
        record = _latched(confirmed_at=(BASE + timedelta(minutes=1)).isoformat())
        repository.upsert_record(record)
        _evaluate(repository, record, [_candle(0, high="99", low="95")], decision_timestamp=_close(0))
        row = _sql_progress(repository, record.lifecycle_id)[0]
        diagnostic = _diagnose_row(row)
        assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
        assert diagnostic["disposition"] == DISPOSITION_WAITING_FOR_TRACKING_START
        assert diagnostic["pending_suffix_exhausted"] is None

    with SQLiteSetupLifecycleRepository(tmp_path / "exhausted.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        first = _evaluate(repository, record, first_candles, decision_timestamp=_close(1))
        row = _sql_progress(repository, record.lifecycle_id)[0]
        diagnostic = _diagnose_row(row)
        assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
        assert diagnostic["disposition"] == DISPOSITION_PENDING_SUFFIX_EXHAUSTED
        assert diagnostic["pending_suffix_exhausted"] is True
        _evaluate(repository, first.record, first_candles, decision_timestamp=_close(1))
        row = _sql_progress(repository, record.lifecycle_id)[0]
        diagnostic = _diagnose_row(row)
        assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
        assert diagnostic["disposition"] == DISPOSITION_NO_NEW_PENDING_CANDLES
        assert diagnostic["pending_suffix_exhausted"] is True

    with SQLiteSetupLifecycleRepository(tmp_path / "range.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="90", low="103")],
            decision_timestamp=_close(1),
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
        diagnostic = _diagnose_row(row)
        assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
        assert diagnostic["disposition"] == DISPOSITION_POST_FILTER_BLOCKED
        assert diagnostic["pending_suffix_exhausted"] is None

    with SQLiteSetupLifecycleRepository(tmp_path / "stale.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        first = _evaluate(repository, record, first_candles, decision_timestamp=_close(1))
        _evaluate(
            repository,
            first.record,
            [_candle(0, high="99", low="95")],
            decision_timestamp=_close(0),
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
        diagnostic = _diagnose_row(row)
        assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
        assert diagnostic["disposition"] == DISPOSITION_POST_FILTER_BLOCKED
        assert diagnostic["pending_suffix_exhausted"] is None

    with SQLiteSetupLifecycleRepository(tmp_path / "gap.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        first = _evaluate(repository, record, first_candles, decision_timestamp=_close(1))
        _evaluate(
            repository,
            first.record,
            [_candle(4, high="111", low="103"), _candle(5, high="112", low="110")],
            decision_timestamp=_close(5),
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
        diagnostic = _diagnose_row(row)
        assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
        assert diagnostic["disposition"] == DISPOSITION_POST_FILTER_BLOCKED
        assert diagnostic["pending_suffix_exhausted"] is None

    with SQLiteSetupLifecycleRepository(tmp_path / "terminal.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        _evaluate(repository, record, _tp_prefix(), decision_timestamp=_close(4))
        row = _sql_progress(repository, record.lifecycle_id)[0]
        diagnostic = _diagnose_row(row)
        assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
        assert diagnostic["disposition"] == DISPOSITION_POLICY_TERMINAL
        assert diagnostic["pending_suffix_exhausted"] is True

    with SQLiteSetupLifecycleRepository(tmp_path / "early.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        _evaluate(
            repository,
            record,
            _tp_prefix() + [_candle(5, high="132", low="130")],
            decision_timestamp=_close(5),
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
        diagnostic = _diagnose_row(row)
        assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
        assert diagnostic["disposition"] == DISPOSITION_POLICY_TERMINAL
        assert diagnostic["pending_suffix_exhausted"] is False

    with SQLiteSetupLifecycleRepository(tmp_path / "prefilter.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        seeded = _evaluate(repository, record, first_candles, decision_timestamp=_close(1) + timedelta(minutes=1))
        prior = _sql_progress(repository, record.lifecycle_id)[0]
        terminal_lifecycle = seeded.record.model_copy(update={"current_state": SetupLifecycleState.EXPIRED})
        repository.upsert_record(terminal_lifecycle)
        _evaluate(repository, terminal_lifecycle, first_candles, decision_timestamp=_close(2))
        preserved = _sql_progress(repository, record.lifecycle_id)[0]
        diagnostic = _diagnose_row(preserved)
        assert preserved["last_eligibility_prefix_evidence_json"] == prior["last_eligibility_prefix_evidence_json"]
        assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
        assert diagnostic["disposition"] == DISPOSITION_PENDING_SUFFIX_EXHAUSTED

    from app.lifecycle import outcomes as outcomes_module

    def _fail_managing(record, **kwargs):
        del kwargs
        return record, None, ()

    monkeypatch.setattr(outcomes_module, "_advance_to_managing", _fail_managing)
    with SQLiteSetupLifecycleRepository(tmp_path / "abort.db") as repository:
        record = _latched()
        repository.upsert_record(record)
        _evaluate(repository, record, first_candles, decision_timestamp=_close(1))
        row = _sql_progress(repository, record.lifecycle_id)[0]
        diagnostic = _diagnose_row(row)
        assert diagnostic["status"] == PREFIX_EVIDENCE_STATUS_KNOWN
        assert diagnostic["disposition"] == DISPOSITION_PROCESSING_ABORTED
        assert diagnostic["pending_suffix_exhausted"] is False
