from __future__ import annotations

import copy
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.analytics.evidence_contract import UNAVAILABLE, evidence_contract_payload
from app.analytics.outcome_ownership import (
    STATUS_AMBIGUOUS_CONTEXT,
    STATUS_MISSING_LEGACY,
    dumps_outcome_ownership_report,
    project_outcome_ownership,
)
from app.data.dtos import NA
from app.lifecycle.economic_identity import latch_economic_identities, proven_progress_plan_version_id
from app.lifecycle.models import (
    SetupLifecycleOutcomeProgress,
    SetupLifecycleRecord,
    SetupLifecycleState,
)
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.storage.database import SCHEMA_VERSION, open_initialized_database

BASE = datetime(2026, 1, 1, tzinfo=UTC)
TIMEFRAME = "5m"
INVALIDATION = "Closed structure beyond the stored stop invalidates the plan."
COMPARE_PROGRESS_COLUMNS = (
    "lifecycle_id",
    "plan_identity",
    "plan_version_id",
    "symbol",
    "mode",
    "direction",
    "execution_timeframe",
    "tracking_start_at",
    "evaluation_cursor_open_at",
    "evaluation_cursor_close_at",
    "entry_at",
    "tp1_at",
    "tp2_at",
    "tp3_at",
    "stop_at",
    "invalidated_at",
    "outcome_at",
    "terminal_outcome",
    "integrity_status",
    "diagnostic",
    "metadata_json",
    "first_evaluated_at",
    "last_evaluated_at",
)


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
):
    index = len(candles) - 1
    decision = decision_timestamp if decision_timestamp is not None else _close(index)
    processed = evaluated_at if evaluated_at is not None else _decision(index)
    return evaluate_closed_candle_outcomes(
        record,
        execution_candles=candles,
        execution_timeframe=TIMEFRAME,
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


def _causal_fields(row: dict[str, object]) -> dict[str, object]:
    return {name: row[name] for name in COMPARE_PROGRESS_COLUMNS}


def _project(record, progress, **provenance):
    values = {
        "synthetic": True,
        "label": "p3b2a-synthetic",
        "source_namespace": "synthetic-p3b2a",
        "coverage_complete": False,
    }
    values.update(provenance)
    return project_outcome_ownership(
        lifecycle_records=[record],
        progress_rows=[progress] if progress is not None else [],
        provenance=values,
    )


def _interpretable(payload: dict[str, object]) -> list[dict[str, object]]:
    return [item for item in payload["plan_interpretations"] if item.get("interpretable")]


def test_schema_is_v23_with_nullable_eligibility_cutoff(tmp_path: Path) -> None:
    path = tmp_path / "schema.sqlite"
    with open_initialized_database(path) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(setup_lifecycle_outcome_progress)")
        }
        nullability = {
            str(row[1]): (int(row[3]), row[4])
            for row in connection.execute("PRAGMA table_info(setup_lifecycle_outcome_progress)")
            if str(row[1]) == "last_eligibility_decision_at"
        }
        unique_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'setup_lifecycle_outcome_progress'"
        ).fetchone()[0]
    assert version == SCHEMA_VERSION == 23
    assert "tracking_start_at" in columns
    assert "plan_version_id" in columns
    assert "last_eligibility_decision_at" in columns
    assert nullability["last_eligibility_decision_at"] == (0, None)
    assert "decision_timestamp" not in columns
    assert "evaluation_id" not in columns
    assert "UNIQUE(lifecycle_id, plan_identity)" in unique_sql


def test_nonempty_tracking_start_is_anchor_not_complete(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "anchor.db") as repository:
        repository.upsert_record(record)
        result = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
        )
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
        sql = _sql_progress(repository, record.lifecycle_id)[0]
    payload = _project(record, progress, coverage_complete=True, as_of=_decision(9))
    context = payload["evaluations"][0]["evaluation_context"]
    interpretation = _interpretable(payload)[0]
    assert sql["tracking_start_at"] is not None
    assert result.progress.tracking_start_at == sql["tracking_start_at"]
    assert context["anchor_present"] is True
    assert context["anchor_normalizable"] is True
    assert context["complete"] is False
    assert context["caller_coverage_complete"] is True
    assert context["caller_coverage_complete_is_row_provenance"] is False
    assert context["durable_decision_timestamp"] == sql["last_eligibility_decision_at"]
    assert context["durable_decision_timestamp_status"] == "known"
    assert context["last_eligibility_decision_at_status"] == "known"
    assert "as_of" not in context
    assert payload["provenance"]["as_of"] == _decision(9)
    assert payload["provenance"]["as_of_is_report_timestamp_not_evaluator_cutoff"] is True
    assert payload["durable_evaluation_context"]["established"] is False
    assert interpretation["evaluation_context_complete"] is False
    assert interpretation["evaluation_context"]["complete"] is False
    assert interpretation["plan_version_id"] == record.plan_version_id
    assert interpretation["milestones"]["entry_at"] == result.progress.entry_at


def test_invalid_timestamp_does_not_repair_or_complete() -> None:
    record = _latched()
    original = {
        "lifecycle_id": record.lifecycle_id,
        "plan_identity": canonical_plan_identity(record),
        "plan_version_id": record.plan_version_id,
        "symbol": "BTCUSDT",
        "tracking_start_at": "not-a-timestamp",
        "execution_timeframe": "5m",
        "first_evaluated_at": _decision(0),
        "last_evaluated_at": _decision(0),
        "terminal_outcome": NA,
    }
    snapshot = copy.deepcopy(original)
    payload = _project(record, original, source_namespace="invalid-ts")
    context = payload["evaluations"][0]["evaluation_context"]
    assert original == snapshot
    assert payload["evaluations"][0]["attribution"]["plan_version_id"] == record.plan_version_id
    assert context["anchor_present"] is True
    assert context["anchor_normalizable"] is False
    assert context["complete"] is False
    assert context["supplied_snapshot_anchor"] is False
    assert {"field": "tracking_start_at", "reason": "not_normalizable_utc_timestamp"} in context[
        "context_conflicts"
    ]
    assert _interpretable(payload) == []
    assert payload["plan_interpretations"][0]["integrity_status"] == STATUS_AMBIGUOUS_CONTEXT
    assert payload["plan_interpretations"][0]["retained_raw_labels"]


def test_contradictory_cursor_is_retained_not_repaired() -> None:
    record = _latched()
    payload = _project(
        record,
        {
            "lifecycle_id": record.lifecycle_id,
            "plan_identity": canonical_plan_identity(record),
            "plan_version_id": record.plan_version_id,
            "symbol": "BTCUSDT",
            "tracking_start_at": BASE.isoformat(),
            "evaluation_cursor_open_at": _decision(2),
            "evaluation_cursor_close_at": _decision(1),
            "execution_timeframe": "5m",
            "first_evaluated_at": _decision(0),
            "last_evaluated_at": _decision(2),
            "terminal_outcome": NA,
        },
        source_namespace="cursor-conflict",
    )
    context = payload["evaluations"][0]["evaluation_context"]
    assert {"field": "evaluation_cursor", "reason": "cursor_close_before_open"} in context[
        "context_conflicts"
    ]
    assert context["complete"] is False
    assert _interpretable(payload) == []
    assert payload["evaluations"][0]["attribution"]["plan_version_id"] == record.plan_version_id


def test_caller_coverage_and_stuffed_cutoff_do_not_mint_row_provenance() -> None:
    record = _latched()
    payload = _project(
        record,
        {
            "lifecycle_id": record.lifecycle_id,
            "plan_identity": canonical_plan_identity(record),
            "plan_version_id": record.plan_version_id,
            "symbol": "BTCUSDT",
            "tracking_start_at": BASE.isoformat(),
            "decision_timestamp": _decision(4),
            "as_of": _decision(4),
            "execution_timeframe": "5m",
            "first_evaluated_at": _decision(0),
            "last_evaluated_at": _decision(0),
            "terminal_outcome": NA,
        },
        coverage_complete=True,
        as_of=_decision(8),
        source_namespace="caller-assertion",
    )
    context = payload["evaluations"][0]["evaluation_context"]
    assert context["complete"] is False
    assert context["caller_coverage_complete"] is True
    assert context["durable_decision_timestamp"] is None
    assert payload["durable_evaluation_context"]["established"] is False
    assert _interpretable(payload)[0]["evaluation_context_complete"] is False


def test_missing_attribution_keeps_context_diagnostics() -> None:
    record = _record()
    payload = _project(
        record,
        {
            "lifecycle_id": record.lifecycle_id,
            "plan_identity": canonical_plan_identity(record),
            "symbol": "BTCUSDT",
            "tracking_start_at": BASE.isoformat(),
            "execution_timeframe": "5m",
            "first_evaluated_at": _decision(0),
            "last_evaluated_at": _decision(0),
            "terminal_outcome": NA,
        },
        source_namespace="unattributed",
    )
    evaluation = payload["evaluations"][0]
    assert evaluation["integrity_status"] == STATUS_MISSING_LEGACY
    assert evaluation["evaluation_context"]["anchor_present"] is True
    assert evaluation["evaluation_context"]["complete"] is False
    assert payload["plan_interpretations"] == []


def test_distinct_as_of_same_closed_prefix_is_not_reconstructable(tmp_path: Path) -> None:
    record = _latched()
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    processed_at = (BASE + timedelta(hours=3)).isoformat()
    cutoff_a = _close(1) + timedelta(minutes=1)
    cutoff_b = _close(1) + timedelta(minutes=3)
    assert _close(1) < cutoff_a < cutoff_b < _close(2)

    def _run(path: Path, cutoff: datetime) -> dict[str, object]:
        with SQLiteSetupLifecycleRepository(path) as repository:
            repository.upsert_record(record)
            result = _evaluate(
                repository,
                record,
                candles,
                decision_timestamp=cutoff,
                evaluated_at=processed_at,
                scan_run_id=None,
            )
            progress = _causal_fields(_sql_progress(repository, record.lifecycle_id)[0])
            events = [
                (event.reason.value, event.scan_run_id, event.notes)
                for event in repository.list_events(lifecycle_id=record.lifecycle_id)
            ]
            scan_runs = repository._connection.execute("SELECT run_id FROM scan_runs").fetchall()
            sql = _sql_progress(repository, record.lifecycle_id)[0]
        return {
            "progress": progress,
            "sql": sql,
            "events": events,
            "scan_runs": scan_runs,
            "entry_at": result.progress.entry_at,
        }

    first = _run(tmp_path / "asof-a.db", cutoff_a)
    second = _run(tmp_path / "asof-b.db", cutoff_b)
    assert first["entry_at"] is not None
    assert first["progress"] == second["progress"]
    assert first["events"] == second["events"]
    assert first["scan_runs"] == second["scan_runs"] == []
    assert "decision_timestamp" not in first["progress"]
    notes = json.loads(first["events"][0][2])
    assert "decision_timestamp" not in notes
    first_cutoff = first["sql"]["last_eligibility_decision_at"]
    second_cutoff = second["sql"]["last_eligibility_decision_at"]
    assert first_cutoff == cutoff_a.isoformat()
    assert second_cutoff == cutoff_b.isoformat()
    assert first_cutoff != second_cutoff
    payload = _project(record, first["progress"])
    assert payload["evaluations"][0]["evaluation_context"]["complete"] is False
    assert payload["durable_evaluation_context"]["established"] is False


def test_later_processing_clock_does_not_admit_future_candles(tmp_path: Path) -> None:
    record = _latched()
    candles = [
        _candle(0, high="99", low="95"),
        _candle(1, high="103", low="99"),
        _candle(2, high="111", low="103"),
    ]
    cutoff = _close(1)
    later = (BASE + timedelta(hours=6)).isoformat()
    with SQLiteSetupLifecycleRepository(tmp_path / "cutoff.db") as repository:
        repository.upsert_record(record)
        exact = _evaluate(
            repository,
            record,
            candles,
            decision_timestamp=cutoff,
            evaluated_at=later,
            scan_run_id="scan-late",
        )
        row = _sql_progress(repository, record.lifecycle_id)[0]
    assert exact.progress.entry_at == _decision(1)
    assert exact.progress.tp1_at is None
    assert exact.progress.evaluation_cursor_close_at == cutoff.isoformat()
    assert exact.progress.last_evaluated_at == later
    assert exact.progress.first_evaluated_at == later
    assert row["last_evaluated_at"] == later
    assert row["evaluation_cursor_close_at"] == cutoff.isoformat()


def test_exact_close_is_eligible_and_unclosed_next_candle_is_not(tmp_path: Path) -> None:
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    with SQLiteSetupLifecycleRepository(tmp_path / "exact-close.db") as repository:
        eligible_record = _latched(lifecycle_id="life-exact")
        repository.upsert_record(eligible_record)
        eligible = _evaluate(
            repository,
            eligible_record,
            candles,
            decision_timestamp=_close(1),
            evaluated_at=_decision(1),
        )
    with SQLiteSetupLifecycleRepository(tmp_path / "before-close.db") as repository:
        early_record = _latched(lifecycle_id="life-open")
        repository.upsert_record(early_record)
        too_early = _evaluate(
            repository,
            early_record,
            candles,
            decision_timestamp=_close(1) - timedelta(seconds=1),
            evaluated_at=_decision(1),
        )
    assert eligible.progress.entry_at == _decision(1)
    assert too_early.progress.entry_at is None
    assert too_early.progress.evaluation_cursor_close_at == _close(0).isoformat()


def test_material_plan_boundary_uses_first_evaluated_at_without_changing_policy(
    tmp_path: Path,
) -> None:
    first = _latched()
    later = BASE + timedelta(hours=2)
    with SQLiteSetupLifecycleRepository(tmp_path / "material.db") as repository:
        repository.upsert_record(first)
        _evaluate(repository, first, [_candle(0, high="99", low="95")])
        replacement = first.model_copy(
            update={
                "current_state": SetupLifecycleState.ACTIONABLE_A_GRADE,
                "entry_low": "200",
                "entry_high": "202",
                "stop_loss": "190",
                "tp1": "210",
                "tp2": "220",
                "tp3": "230",
                "invalidation_logic": "Closed structure below 190 invalidates the replacement.",
                "last_seen_at": later.isoformat(),
                "last_transition_at": later.isoformat(),
            }
        )
        repository.upsert_record(replacement)
        second = _evaluate(
            repository,
            replacement,
            [_candle(0, high="199", low="195"), _candle(1, high="199", low="195")],
            decision_timestamp=_close(1),
            evaluated_at=later.isoformat(),
        )
        rows = _sql_progress(repository, first.lifecycle_id)
    metadata = json.loads(second.progress.metadata_json)
    assert len(rows) == 2
    assert metadata["tracking_boundary_source"] == "material_plan_first_evaluated_at_after_confirmation"
    assert metadata["tracking_boundary_timestamp"] == later.isoformat()
    assert second.progress.first_evaluated_at == later.isoformat()
    payload = _project(replacement, second.progress)
    context = payload["evaluations"][0]["evaluation_context"]
    assert context["tracking_boundary_source"] == "material_plan_first_evaluated_at_after_confirmation"
    assert context["start_boundary_provenance"] == "producer_metadata"
    assert context["complete"] is False


def test_reload_and_later_scan_id_continue_one_window(tmp_path: Path) -> None:
    db_path = tmp_path / "reload.db"
    record = _latched()
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        first = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
            scan_run_id="scan-1",
        )
        start = first.progress.tracking_start_at
        plan_id = first.progress.plan_version_id
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        loaded = repository.get_record_by_lifecycle_id(record.lifecycle_id)
        continued = _evaluate(
            repository,
            loaded,
            [
                _candle(0, high="99", low="95"),
                _candle(1, high="103", low="99"),
                _candle(2, high="111", low="103"),
            ],
            scan_run_id="scan-2",
        )
        none_pass = _evaluate(
            repository,
            continued.record,
            [
                _candle(0, high="99", low="95"),
                _candle(1, high="103", low="99"),
                _candle(2, high="111", low="103"),
            ],
            scan_run_id=None,
        )
        rows = _sql_progress(repository, record.lifecycle_id)
        events = repository.list_events(lifecycle_id=record.lifecycle_id)
    assert len(rows) == 1
    assert continued.progress.tracking_start_at == start
    assert none_pass.progress.tracking_start_at == start
    assert continued.progress.plan_version_id == plan_id == record.plan_version_id
    assert continued.progress.tp1_at is not None
    assert none_pass.processed_candles == 0
    assert {"scan-1", "scan-2"} <= {event.scan_run_id for event in events}


def test_raw_upsert_can_replace_start_but_ordinary_pass_does_not_drift(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "upsert.db") as repository:
        repository.upsert_record(record)
        first = _evaluate(repository, record, [_candle(0, high="99", low="95")])
        start = first.progress.tracking_start_at
        second = _evaluate(
            repository,
            first.record,
            [_candle(0, high="99", low="95"), _candle(1, high="99", low="95")],
        )
        assert second.progress.tracking_start_at == start
        mutated = second.progress.model_copy(update={"tracking_start_at": _decision(9)})
        repository.upsert_outcome_progress(mutated)
        replaced = _sql_progress(repository, record.lifecycle_id)[0]
    assert replaced["tracking_start_at"] == _decision(9)
    assert replaced["tracking_start_at"] != start


def test_noop_and_failed_pass_do_not_add_evaluated_coverage(tmp_path: Path) -> None:
    record = _latched()
    candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
    with SQLiteSetupLifecycleRepository(tmp_path / "noop.db") as repository:
        repository.upsert_record(record)
        first = _evaluate(repository, record, candles)
        metadata_before = json.loads(first.progress.metadata_json)
        noop = _evaluate(
            repository,
            first.record,
            candles,
            evaluated_at=(BASE + timedelta(hours=1)).isoformat(),
        )
        failed = _evaluate(
            repository,
            noop.record,
            [_candle(0, high="99", low="95"), _candle(2, high="111", low="103")],
            evaluated_at=(BASE + timedelta(hours=2)).isoformat(),
        )
        metadata_noop = json.loads(noop.progress.metadata_json)
        metadata_failed = json.loads(failed.progress.metadata_json)
    assert noop.processed_candles == 0
    assert metadata_noop["processed_candle_count"] == metadata_before["processed_candle_count"]
    assert noop.progress.evaluation_cursor_open_at == first.progress.evaluation_cursor_open_at
    assert failed.progress.integrity_status == "Unverified"
    assert "continuity_gap" in failed.progress.diagnostic
    assert failed.progress.evaluation_cursor_open_at == first.progress.evaluation_cursor_open_at
    assert metadata_failed["processed_candle_count"] == metadata_before["processed_candle_count"]
    payload = _project(record, failed.progress)
    assert payload["evaluations"][0]["evaluation_context"]["complete"] is False


def test_terminal_shortcut_without_start_keeps_attribution_and_raw_terminal(
    tmp_path: Path,
) -> None:
    record = _latched(current_state=SetupLifecycleState.INVALIDATED)
    with SQLiteSetupLifecycleRepository(tmp_path / "terminal.db") as repository:
        repository.upsert_record(record)
        result = _evaluate(repository, record, [_candle(0, high="99", low="95")])
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
        sql = _sql_progress(repository, record.lifecycle_id)[0]
        again = _evaluate(repository, result.record, [_candle(0, high="99", low="95")])
    assert sql["tracking_start_at"] is None
    assert sql["plan_version_id"] == record.plan_version_id
    assert sql["terminal_outcome"] == SetupLifecycleState.INVALIDATED.value
    assert progress.integrity_status == "Unverified"
    assert progress.diagnostic == "terminal_state_preceded_canonical_outcome_cursor"
    assert again.progress.terminal_outcome == progress.terminal_outcome
    payload = _project(record, progress)
    evaluation = payload["evaluations"][0]
    assert evaluation["attribution"]["plan_version_id"] == record.plan_version_id
    assert evaluation["evaluation_context"]["start_boundary_provenance"] == "absent"
    assert evaluation["evaluation_context"]["complete"] is False
    assert evaluation["raw_labels"]["progress_terminal_outcome"] == "INVALIDATED"
    assert _interpretable(payload) == []


def test_terminalizing_verified_partial_progress_retains_integrity_and_cursor(
    tmp_path: Path,
) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "partial-terminal.db") as repository:
        repository.upsert_record(record)
        opened = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
        )
        assert opened.progress.integrity_status == "Verified"
        assert opened.progress.evaluation_cursor_open_at is not None
        terminal_record = opened.record.model_copy(
            update={
                "current_state": SetupLifecycleState.EXPIRED,
                "last_transition_at": _decision(3),
            }
        )
        repository.upsert_record(terminal_record)
        closed = _evaluate(repository, terminal_record, [_candle(0, high="99", low="95")])
        sql = _sql_progress(repository, record.lifecycle_id)[0]
    assert closed.progress.tracking_start_at == opened.progress.tracking_start_at
    assert closed.progress.evaluation_cursor_open_at == opened.progress.evaluation_cursor_open_at
    assert closed.progress.integrity_status == "Verified"
    assert closed.progress.terminal_outcome == SetupLifecycleState.EXPIRED.value
    assert sql["plan_version_id"] == record.plan_version_id
    payload = _project(terminal_record, closed.progress)
    assert payload["evaluations"][0]["raw_labels"]["progress_terminal_outcome"] == "EXPIRED"
    assert payload["evaluations"][0]["evaluation_context"]["complete"] is False
    assert _interpretable(payload)[0]["progress_terminal_outcome"] == "EXPIRED"


def test_v19_cursor_backfill_is_unproven_origin_not_prospective_proof(tmp_path: Path) -> None:
    db_path = tmp_path / "v19.db"
    record = _latched()
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        primed = _evaluate(repository, record, [_candle(0, high="99", low="95")])
        cursor = primed.progress.evaluation_cursor_open_at
        repository._connection.execute(
            "UPDATE setup_lifecycle_outcome_progress SET tracking_start_at = NULL, metadata_json = ?",
            ('{"source":"v19-legacy"}',),
        )
        repository._connection.execute("PRAGMA user_version = 18")
        repository._connection.commit()
    with open_initialized_database(db_path):
        pass
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        raw = connection.execute("SELECT * FROM setup_lifecycle_outcome_progress").fetchone()
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == SCHEMA_VERSION == 23
    assert raw["tracking_start_at"] == cursor
    assert raw["evaluation_cursor_open_at"] == cursor
    metadata = json.loads(raw["metadata_json"])
    assert "tracking_boundary_source" not in metadata
    payload = _project(record, dict(raw))
    context = payload["evaluations"][0]["evaluation_context"]
    assert context["anchor_present"] is True
    assert context["start_boundary_provenance"] == "unproven"
    assert context["complete"] is False
    assert raw["plan_version_id"] == record.plan_version_id


def test_legacy_unfilled_cursor_adoption_is_distinct_from_v19_backfill(tmp_path: Path) -> None:
    record = _latched(lifecycle_id="legacy-unfilled")
    consumed_cursor = BASE + timedelta(minutes=15)
    progress = SetupLifecycleOutcomeProgress(
        lifecycle_id=record.lifecycle_id,
        plan_identity=canonical_plan_identity(record),
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        execution_timeframe=TIMEFRAME,
        tracking_start_at=consumed_cursor.isoformat(),
        evaluation_cursor_open_at=consumed_cursor.isoformat(),
        evaluation_cursor_close_at=(consumed_cursor + timedelta(minutes=5)).isoformat(),
        integrity_status="Verified",
        metadata_json=json.dumps({"source": "legacy-v19-runtime"}),
        first_evaluated_at=BASE.isoformat(),
        last_evaluated_at=(consumed_cursor + timedelta(minutes=5)).isoformat(),
        plan_version_id=record.plan_version_id,
    )
    consumed = [
        _candle(0, high="99", low="95"),
        _candle(1, high="103", low="99"),
        _candle(2, high="99", low="95"),
        _candle(3, high="99", low="95"),
    ]
    with SQLiteSetupLifecycleRepository(tmp_path / "legacy-unfilled.db") as repository:
        repository.upsert_record(record)
        repository.upsert_outcome_progress(progress)
        adopted = _evaluate(repository, record, consumed)
        metadata = json.loads(adopted.progress.metadata_json)
    assert metadata["entry_causality_migration"] == "legacy_unfilled_cursor_adopted_prospectively"
    payload = _project(record, adopted.progress)
    context = payload["evaluations"][0]["evaluation_context"]
    assert context["start_boundary_provenance"] == "producer_metadata"
    assert context["complete"] is False


def test_same_plan_across_generations_and_distinct_plans_stay_separate(tmp_path: Path) -> None:
    first = _latched(lifecycle_id="gen-a")
    second = _latched(lifecycle_id="gen-b")
    other = _latched(lifecycle_id="gen-c", tp3="140")
    assert first.plan_version_id == second.plan_version_id
    assert first.plan_version_id != other.plan_version_id
    with SQLiteSetupLifecycleRepository(tmp_path / "gens.db") as repository:
        repository.upsert_record(first)
        _evaluate(repository, first, [_candle(0, high="99", low="95")])
        repository.supersede_record(first.lifecycle_id)
        repository.upsert_record(second)
        _evaluate(repository, second, [_candle(0, high="99", low="95")])
        repository.supersede_record(second.lifecycle_id)
        repository.upsert_record(other)
        _evaluate(repository, other, [_candle(0, high="99", low="95")])
        rows = list(repository.list_outcome_progress())
    payload = project_outcome_ownership(
        lifecycle_records=[first, second, other],
        progress_rows=rows,
        provenance={
            "synthetic": True,
            "source_namespace": "generations",
            "coverage_complete": False,
        },
    )
    assert len(rows) == 3
    assert payload["plan_identity_inventory"]["count"] == 2
    same_plan = [
        item
        for item in payload["plan_interpretations"]
        if item["plan_version_id"] == first.plan_version_id
    ]
    assert same_plan[0]["interpretable"] is False
    assert same_plan[0]["integrity_status"] == STATUS_AMBIGUOUS_CONTEXT
    other_interp = _interpretable(payload)
    assert {item["plan_version_id"] for item in other_interp} == {other.plan_version_id}
    for evaluation in payload["evaluations"]:
        assert evaluation["evaluation_context"]["complete"] is False


def test_namespaces_stay_distinct_and_unspecified_is_not_a_runtime_namespace() -> None:
    record = _latched()
    progress = {
        "lifecycle_id": record.lifecycle_id,
        "plan_identity": canonical_plan_identity(record),
        "plan_version_id": record.plan_version_id,
        "symbol": "BTCUSDT",
        "tracking_start_at": BASE.isoformat(),
        "execution_timeframe": "5m",
        "first_evaluated_at": _decision(0),
        "last_evaluated_at": _decision(0),
        "terminal_outcome": NA,
    }
    live = {**progress, "source_namespace": "live-monitoring"}
    replay = {**progress, "source_namespace": "replay-run-a", "tp1_at": _decision(2)}
    mixed = project_outcome_ownership(
        lifecycle_records=[
            {**record.model_dump(mode="json"), "source_namespace": "live-monitoring"},
            {**record.model_dump(mode="json"), "source_namespace": "replay-run-a"},
        ],
        progress_rows=[live, replay],
        provenance={"synthetic": True, "coverage_complete": False},
    )
    absent = project_outcome_ownership(
        lifecycle_records=[record],
        progress_rows=[progress],
        provenance={"synthetic": True, "coverage_complete": False},
    )
    namespaces = {item["source_namespace"] for item in mixed["evaluations"]}
    assert namespaces == {"live-monitoring", "replay-run-a"}
    assert len(_interpretable(mixed)) == 2
    assert absent["provenance"]["default_namespace"] == "unspecified"
    assert absent["evaluations"][0]["evaluation_context"]["source_namespace"] == "unspecified"
    assert absent["evaluations"][0]["evaluation_context"]["complete"] is False


def test_duplicate_and_permuted_inputs_cannot_manufacture_completeness() -> None:
    record = _latched()
    progress = {
        "lifecycle_id": record.lifecycle_id,
        "plan_identity": canonical_plan_identity(record),
        "plan_version_id": record.plan_version_id,
        "symbol": "BTCUSDT",
        "tracking_start_at": BASE.isoformat(),
        "execution_timeframe": "5m",
        "first_evaluated_at": _decision(0),
        "last_evaluated_at": _decision(0),
        "terminal_outcome": NA,
    }
    first = project_outcome_ownership(
        lifecycle_records=[record, record],
        progress_rows=[progress, copy.deepcopy(progress)],
        provenance={"synthetic": True, "source_namespace": "dup", "coverage_complete": True},
    )
    second = project_outcome_ownership(
        lifecycle_records=[record],
        progress_rows=[copy.deepcopy(progress)],
        event_rows=[],
        provenance={"synthetic": True, "source_namespace": "dup", "coverage_complete": True},
    )
    assert dumps_outcome_ownership_report(first) == dumps_outcome_ownership_report(second)
    assert first["evaluations"][0]["evaluation_context"]["complete"] is False
    assert first["counts"]["raw_source_rows"]["outcome_progress"] == 1


def test_projection_is_read_only_deterministic_and_does_not_open_a_database(
    tmp_path: Path,
) -> None:
    record = _latched()
    db_path = tmp_path / "readonly.db"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
        )
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
        before = _sql_progress(repository, record.lifecycle_id)
        dumped = progress.model_dump(mode="json")
        dumped["plan_version_id"] = progress.plan_version_id
        snapshot = copy.deepcopy(dumped)
        source = Path("app/analytics/outcome_ownership.py").read_text(encoding="utf-8")
        first = project_outcome_ownership(
            lifecycle_records=[record],
            progress_rows=[dumped],
            provenance={"synthetic": True, "source_namespace": "ro", "coverage_complete": False},
        )
        second = project_outcome_ownership(
            lifecycle_records=[record],
            progress_rows=[dumped],
            provenance={"synthetic": True, "source_namespace": "ro", "coverage_complete": False},
        )
        after = _sql_progress(repository, record.lifecycle_id)
    assert dumped == snapshot
    assert before == after
    assert dumps_outcome_ownership_report(first) == dumps_outcome_ownership_report(second)
    assert "open_initialized_database" not in source
    assert "evaluate_closed_candle_outcomes" not in source
    assert "closed_candles_as_of" not in source
    assert first["schema_version_unchanged"] is True
    assert first["canonical_outcome_per_plan_version"]["established"] is False
    assert first["unavailable_metrics"]["unique_trade_count"]["status"] == UNAVAILABLE
    assert evidence_contract_payload()["unique_trade_count"]["status"] == UNAVAILABLE
    assert proven_progress_plan_version_id(record) == record.plan_version_id


def test_scan_run_id_is_optional_event_association_not_evaluation_owner(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "scan-id.db") as repository:
        repository.upsert_record(record)
        _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
            scan_run_id="ghost-run",
        )
        events = repository.list_events(lifecycle_id=record.lifecycle_id)
        scan_rows = repository._connection.execute("SELECT run_id FROM scan_runs").fetchall()
        fk_sql = repository._connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'setup_lifecycle_events'"
        ).fetchone()[0]
        rows = _sql_progress(repository, record.lifecycle_id)
    assert any(event.scan_run_id == "ghost-run" for event in events)
    assert scan_rows == []
    assert "REFERENCES scan_runs" not in fk_sql
    assert len(rows) == 1
    assert "scan_run_id" not in rows[0]
    payload = _project(record, rows[0])
    assert payload["evaluations"][0]["evaluation_context"]["complete"] is False




