from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.analytics.evidence_contract import UNAVAILABLE, evidence_contract_payload
from app.analytics.outcome_ownership import (
    STATUS_MISSING_LEGACY,
    project_outcome_ownership,
)
from app.lifecycle.economic_identity import (
    REASON_PLAN_VERSION_INVARIANT_VIOLATION,
    latch_economic_identities,
    proven_progress_plan_version_id,
)
from app.lifecycle.models import (
    SetupLifecycleOutcomeProgress,
    SetupLifecycleRecord,
    SetupLifecycleState,
    SetupTransitionReason,
    SetupTransitionResult,
)
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import _outcome_analytics_record
from app.storage.database import SCHEMA_VERSION, open_initialized_database

BASE = datetime(2026, 1, 1, tzinfo=UTC)
TIMEFRAME = "5m"
INVALIDATION = "Closed structure beyond the stored stop invalidates the plan."


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


def _decision(index: int) -> str:
    return (BASE + timedelta(minutes=5 * (index + 1))).isoformat()


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


def _evaluate(repository, record, candles, *, decision_index: int | None = None):
    index = len(candles) - 1 if decision_index is None else decision_index
    return evaluate_closed_candle_outcomes(
        record,
        execution_candles=candles,
        execution_timeframe=TIMEFRAME,
        decision_timestamp=_decision(index),
        evaluated_at=_decision(index),
        repository=repository,
        scan_run_id=f"scan-{index}",
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


def test_eligible_progress_stores_exact_p1_id_for_evaluated_plan(tmp_path: Path) -> None:
    record = _latched()
    expected = proven_progress_plan_version_id(record)
    assert expected is not None
    with SQLiteSetupLifecycleRepository(tmp_path / "eligible.db") as repository:
        repository.upsert_record(record)
        candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
        result = _evaluate(repository, record, candles)
        rows = _sql_progress(repository, record.lifecycle_id)
    assert len(rows) == 1
    assert rows[0]["plan_version_id"] == expected == record.plan_version_id
    assert result.progress is not None
    assert result.progress.plan_version_id == expected
    assert result.progress.entry_at is not None
    assert "plan_version_id" not in result.progress.model_dump(mode="json")


def test_missing_identity_stores_null_and_still_persists_outcome(tmp_path: Path) -> None:
    record = _record()
    assert record.plan_version_id is None
    with SQLiteSetupLifecycleRepository(tmp_path / "missing.db") as repository:
        repository.upsert_record(record)
        candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
        result = _evaluate(repository, record, candles)
        rows = _sql_progress(repository, record.lifecycle_id)
    assert len(rows) == 1
    assert rows[0]["plan_version_id"] is None
    assert result.progress is not None
    assert result.progress.entry_at is not None
    assert result.progress.plan_identity == canonical_plan_identity(record)


def test_invariant_conflicting_replacement_cannot_acquire_latched_id(tmp_path: Path) -> None:
    original = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "conflict.db") as repository:
        repository.upsert_record(original)
        candles = [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")]
        entered = _evaluate(repository, original, candles)
        original_row = _sql_progress(repository, original.lifecycle_id)[0]
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
        assert replacement.plan_version_id == original.plan_version_id
        assert REASON_PLAN_VERSION_INVARIANT_VIOLATION in (replacement.economic_identity_reason or "")
        assert proven_progress_plan_version_id(replacement) is None
        repository.upsert_record(replacement)
        _evaluate(repository, replacement, [_candle(0, high="199", low="195")])
        rows = _sql_progress(repository, original.lifecycle_id)
    assert len(rows) == 2
    by_identity = {row["plan_identity"]: row for row in rows}
    assert original_row["plan_identity"] in by_identity
    assert by_identity[original_row["plan_identity"]]["plan_version_id"] == original.plan_version_id
    new_identity = canonical_plan_identity(replacement)
    assert by_identity[new_identity]["plan_version_id"] is None


def test_later_rediscovery_does_not_relabel_existing_attribution(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "frozen.db") as repository:
        repository.upsert_record(record)
        first = _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
        )
        stored = first.progress.plan_version_id if first.progress is not None else None
        advertised = record.model_copy(update={"plan_version_id": "plan-version-advertised-y"})
        second = _evaluate(
            repository,
            advertised,
            [
                _candle(0, high="99", low="95"),
                _candle(1, high="103", low="99"),
                _candle(2, high="111", low="103"),
            ],
        )
        rows = _sql_progress(repository, record.lifecycle_id)
    assert len(rows) == 1
    assert rows[0]["plan_version_id"] == stored == record.plan_version_id
    assert second.progress is not None
    assert second.progress.plan_version_id == stored
    assert second.progress.tp1_at is not None


def test_repeat_upsert_preserves_attribution_and_physical_key(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "repeat.db") as repository:
        repository.upsert_record(record)
        candles = [_candle(0, high="99", low="95")]
        _evaluate(repository, record, candles)
        first_rows = _sql_progress(repository, record.lifecycle_id)
        _evaluate(repository, record, candles)
        second_rows = _sql_progress(repository, record.lifecycle_id)
    assert len(first_rows) == len(second_rows) == 1
    assert first_rows[0]["id"] == second_rows[0]["id"]
    assert first_rows[0]["plan_identity"] == second_rows[0]["plan_identity"]
    assert first_rows[0]["plan_version_id"] == second_rows[0]["plan_version_id"] == record.plan_version_id


def test_existing_null_is_not_backfilled_when_later_caller_supplies_id(tmp_path: Path) -> None:
    unlatched = _record()
    latched = _latched()
    assert canonical_plan_identity(unlatched) == canonical_plan_identity(latched)
    with SQLiteSetupLifecycleRepository(tmp_path / "nobackfill.db") as repository:
        repository.upsert_record(unlatched)
        _evaluate(repository, unlatched, [_candle(0, high="99", low="95")])
        assert _sql_progress(repository, unlatched.lifecycle_id)[0]["plan_version_id"] is None
        repository.upsert_record(latched)
        _evaluate(
            repository,
            latched,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
        )
        rows = _sql_progress(repository, unlatched.lifecycle_id)
    assert len(rows) == 1
    assert rows[0]["plan_version_id"] is None
    assert rows[0]["entry_at"] is not None


def test_reconstructed_unbound_progress_is_not_treated_as_prospective(tmp_path: Path) -> None:
    record = _latched()
    reconstructed = SetupLifecycleOutcomeProgress(
        lifecycle_id=record.lifecycle_id,
        plan_identity=canonical_plan_identity(record),
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        execution_timeframe=TIMEFRAME,
        first_evaluated_at=_decision(0),
        last_evaluated_at=_decision(0),
    )
    with SQLiteSetupLifecycleRepository(tmp_path / "reconstruct.db") as repository:
        repository.upsert_record(record)
        repository.upsert_outcome_progress(reconstructed)
        loaded = repository.get_outcome_progress(
            lifecycle_id=record.lifecycle_id,
            plan_identity=reconstructed.plan_identity,
        )
        _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
        )
        rows = _sql_progress(repository, record.lifecycle_id)
    assert reconstructed.plan_version_id is None
    assert loaded is not None
    assert loaded.plan_version_id is None
    assert rows[0]["plan_version_id"] is None


def test_reload_round_trip_continues_same_plan_without_scanner(tmp_path: Path) -> None:
    db_path = tmp_path / "reload.db"
    record = _latched()
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
        )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        loaded = repository.get_record_by_lifecycle_id(record.lifecycle_id)
        assert loaded is not None
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
        assert progress.plan_version_id == record.plan_version_id
        continued = _evaluate(
            repository,
            loaded,
            [
                _candle(0, high="99", low="95"),
                _candle(1, high="103", low="99"),
                _candle(2, high="111", low="103"),
            ],
        )
        rows = _sql_progress(repository, record.lifecycle_id)
    assert continued.progress is not None
    assert continued.progress.plan_version_id == record.plan_version_id
    assert continued.progress.tp1_at is not None
    assert rows[0]["plan_version_id"] == record.plan_version_id


def test_missing_incoming_id_does_not_erase_stored_association(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "preserve.db") as repository:
        repository.upsert_record(record)
        _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
        )
        stripped = record.model_copy(update={"plan_version_id": None, "economic_identity_reason": None})
        _evaluate(
            repository,
            stripped,
            [
                _candle(0, high="99", low="95"),
                _candle(1, high="103", low="99"),
                _candle(2, high="111", low="103"),
            ],
        )
        rows = _sql_progress(repository, record.lifecycle_id)
    assert len(rows) == 1
    assert rows[0]["plan_version_id"] == record.plan_version_id


def test_distinct_tp_ladders_remain_separate_under_existing_keys(tmp_path: Path) -> None:
    first = _latched(lifecycle_id="life-tp-a")
    second = _latched(lifecycle_id="life-tp-b", tp3="140")
    assert first.plan_version_id != second.plan_version_id
    assert canonical_plan_identity(first) != canonical_plan_identity(second)
    with SQLiteSetupLifecycleRepository(tmp_path / "ladders.db") as repository:
        repository.upsert_record(first)
        candles = [_candle(0, high="99", low="95")]
        _evaluate(repository, first, candles)
        repository.supersede_record(first.lifecycle_id)
        repository.upsert_record(second)
        _evaluate(repository, second, candles)
        rows = list(repository.list_outcome_progress())
    assert len(rows) == 2
    assert {row.plan_identity for row in rows} == {
        canonical_plan_identity(first),
        canonical_plan_identity(second),
    }
    assert {row.plan_version_id for row in rows} == {first.plan_version_id, second.plan_version_id}


def test_same_plan_version_across_generations_keeps_separate_rows(tmp_path: Path) -> None:
    first = _latched(lifecycle_id="gen-1")
    second = _latched(lifecycle_id="gen-2")
    assert first.plan_version_id == second.plan_version_id
    assert canonical_plan_identity(first) != canonical_plan_identity(second)
    with SQLiteSetupLifecycleRepository(tmp_path / "gens.db") as repository:
        repository.upsert_record(first)
        candles = [_candle(0, high="99", low="95")]
        _evaluate(repository, first, candles)
        repository.supersede_record(first.lifecycle_id)
        repository.upsert_record(second)
        _evaluate(repository, second, candles)
        rows = list(repository.list_outcome_progress())
    assert len(rows) == 2
    assert {row.lifecycle_id for row in rows} == {"gen-1", "gen-2"}
    assert {row.plan_version_id for row in rows} == {first.plan_version_id}


def test_terminal_shortcut_uses_same_attribution_policy(tmp_path: Path) -> None:
    record = _latched(current_state=SetupLifecycleState.TP_HIT)
    with SQLiteSetupLifecycleRepository(tmp_path / "terminal.db") as repository:
        repository.upsert_record(record)
        result = _evaluate(repository, record, [_candle(0, high="99", low="95")])
        rows = _sql_progress(repository, record.lifecycle_id)
    assert result.progress is not None
    assert result.progress.terminal_outcome == SetupLifecycleState.TP_HIT.value
    assert rows[0]["plan_version_id"] == record.plan_version_id


def test_cooldown_does_not_overwrite_terminal_attribution(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "cooldown.db") as repository:
        repository.upsert_record(record)
        candles = [
            _candle(0, high="99", low="95"),
            _candle(1, high="103", low="99"),
            _candle(2, high="111", low="103"),
            _candle(3, high="121", low="111"),
            _candle(4, high="131", low="121"),
        ]
        terminal = _evaluate(repository, record, candles)
        cooled = terminal.record.model_copy(update={"current_state": SetupLifecycleState.COOLDOWN})
        repository.upsert_record(cooled)
        _evaluate(repository, cooled, candles)
        rows = _sql_progress(repository, record.lifecycle_id)
    assert len(rows) == 1
    assert rows[0]["terminal_outcome"] == SetupLifecycleState.TP_HIT.value
    assert rows[0]["plan_version_id"] == record.plan_version_id


def test_analytics_payload_excludes_internal_plan_version_id(tmp_path: Path) -> None:
    record = _latched(current_state=SetupLifecycleState.TP_HIT)
    with SQLiteSetupLifecycleRepository(tmp_path / "analytics.db") as repository:
        repository.upsert_record(record)
        result = _evaluate(repository, record, [_candle(0, high="99", low="95")])
        transition = SetupTransitionResult(
            lifecycle_id=record.lifecycle_id,
            symbol=record.symbol,
            from_state=SetupLifecycleState.MANAGING,
            to_state=SetupLifecycleState.TP_HIT,
            reason=SetupTransitionReason.TAKE_PROFIT_HIT,
            transitioned=True,
            record=result.record,
        )
        analytics = _outcome_analytics_record(
            repository,
            transition,
            observation=object(),  # unused by the serializer; isolation is the dump
            outcome_progress=result.progress,
        )
    assert result.progress is not None
    dumped = result.progress.model_dump(mode="json")
    assert "plan_version_id" not in dumped
    assert analytics is not None
    payload = json.loads(analytics.raw_payload_json)
    assert "plan_version_id" not in (payload["outcome_progress"] or {})
    service_source = Path("app/lifecycle/service.py").read_text(encoding="utf-8")
    assert "outcome_progress.model_dump(mode=\"json\")" in service_source


def test_p3a_recognizes_persisted_progress_id_and_keeps_explicit_null_ambiguous(
    tmp_path: Path,
) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "p3a.db") as repository:
        repository.upsert_record(record)
        _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
        )
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
        verified = project_outcome_ownership(
            lifecycle_records=[repository.get_record_by_lifecycle_id(record.lifecycle_id)],
            progress_rows=[progress],
            provenance={"synthetic": True, "source_namespace": "p3b1", "coverage_complete": True},
        )

    unlatched = _record(lifecycle_id="life-null")
    latched_same_geometry = _latched(lifecycle_id="life-null")
    with SQLiteSetupLifecycleRepository(tmp_path / "p3a-null.db") as repository:
        repository.upsert_record(unlatched)
        _evaluate(repository, unlatched, [_candle(0, high="99", low="95")])
        repository.upsert_record(latched_same_geometry)
        explicit_null = project_outcome_ownership(
            lifecycle_records=[latched_same_geometry],
            progress_rows=list(repository.list_outcome_progress(lifecycle_id="life-null")),
            provenance={"synthetic": True, "source_namespace": "p3b1-null", "coverage_complete": True},
        )
        sql_rows = _sql_progress(repository, "life-null")
    assert verified["evaluations"][0]["attribution"]["plan_version_id"] == record.plan_version_id
    assert sql_rows[0]["plan_version_id"] is None
    assert explicit_null["evaluations"][0]["integrity_status"] == STATUS_MISSING_LEGACY
    assert explicit_null["evaluations"][0]["attribution"]["plan_version_id"] is None
    assert verified["canonical_outcome_per_plan_version"]["established"] is False
    assert evidence_contract_payload()["unique_trade_count"]["status"] == UNAVAILABLE


def test_event_notes_do_not_include_progress_plan_version(tmp_path: Path) -> None:
    record = _latched()
    with SQLiteSetupLifecycleRepository(tmp_path / "events.db") as repository:
        repository.upsert_record(record)
        _evaluate(
            repository,
            record,
            [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")],
        )
        notes = [
            json.loads(event.notes)
            for event in repository.list_events(lifecycle_id=record.lifecycle_id)
            if event.notes and event.notes.startswith("{")
        ]
    assert notes
    for payload in notes:
        assert "plan_version_id" not in payload
        assert "plan_identity" in payload


def test_runtime_consumers_do_not_import_progress_plan_version_helper() -> None:
    from app.alerts import telegram_lifecycle
    from app.analytics import performance_memory, symbol_health
    from app.research import queries

    banned = "proven_progress_plan_version_id"
    for module in (telegram_lifecycle, symbol_health, performance_memory, queries):
        assert banned not in inspect.getsource(module)
    assert SCHEMA_VERSION == 24
    assert evidence_contract_payload()["outcome_ownership"]["feeds_operational_decisions"] is False
    assert evidence_contract_payload()["outcome_ownership"]["canonical_outcome_per_plan_version"][
        "established"
    ] is False


def test_fresh_schema_progress_column_is_nullable_and_analytics_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "schema.sqlite"
    with open_initialized_database(path) as connection:
        progress = {
            str(row[1]): (int(row[3]), row[4])
            for row in connection.execute("PRAGMA table_info(setup_lifecycle_outcome_progress)")
        }
        analytics = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(setup_outcome_analytics)")
        }
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert user_version == SCHEMA_VERSION == 24
    assert "plan_version_id" in progress
    assert progress["plan_version_id"] == (0, None)
    assert "last_eligibility_decision_at" in progress
    assert progress["last_eligibility_decision_at"] == (0, None)
    assert "last_eligibility_prefix_evidence_json" in progress
    assert progress["last_eligibility_prefix_evidence_json"] == (0, None)
    assert "plan_version_id" not in analytics
    assert "last_eligibility_decision_at" not in analytics
    assert "last_eligibility_prefix_evidence_json" not in analytics
