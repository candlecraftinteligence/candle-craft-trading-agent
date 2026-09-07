from __future__ import annotations

import hashlib
import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.analytics.activation_accounting import project_activation_accounting
from app.analytics.evidence_baseline_audit import build_evidence_baseline, dumps_evidence_payload
from app.analytics.evidence_contract import CONTRACT_VERSION, UNAVAILABLE, UNSAFE, evidence_contract_payload
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState, SetupTransitionReason
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.state_machine import LifecycleObservation, evaluate_lifecycle_transition
from app.storage.database import SCHEMA_VERSION, open_initialized_database
from app.storage.models import WatchIterationMetadata
from app.storage.repositories import store_scan_result
from app.watch_mode import WatchActivation, build_watch_iteration_summary, current_result_is_valid_activation
from tests.test_storage_database import _scan_result


START = "2026-09-01T00:00:00Z"
CUTOFF = "2026-09-03T00:00:00Z"
BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _write_sql(path: Path, statements: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(statements)
        connection.commit()


def _accounting_schema(path: Path) -> None:
    _write_sql(
        path,
        """
        CREATE TABLE scan_runs (
            run_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            is_watch_iteration INTEGER NOT NULL DEFAULT 0,
            valid_activations INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE setup_lifecycle_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            lifecycle_id TEXT,
            timestamp TEXT NOT NULL,
            reason TEXT NOT NULL,
            scan_run_id TEXT,
            from_state TEXT,
            to_state TEXT
        );
        CREATE TABLE setup_lifecycle_records (
            lifecycle_id TEXT PRIMARY KEY,
            current_state TEXT,
            setup_id TEXT,
            plan_version_id TEXT,
            last_seen_at TEXT
        );
        """,
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


def _lifecycle_record(*, state: SetupLifecycleState = SetupLifecycleState.CONFIRMED) -> SetupLifecycleRecord:
    return SetupLifecycleRecord(
        lifecycle_id="life-1",
        symbol="BTCUSDT",
        mode="challenge",
        direction="long",
        current_state=state,
        first_seen_at=BASE.isoformat(),
        last_seen_at=BASE.isoformat(),
        last_transition_at=BASE.isoformat(),
        confirmed_at=BASE.isoformat(),
        confirmation_count=2,
        required_confirmation_cycles=2,
        invalidation_reason="Closed structure beyond the stored stop invalidates the plan.",
        invalidation_logic="Closed structure beyond the stored stop invalidates the plan.",
        setup_identity="BTCUSDT|challenge|long|100|102|90|stop",
        setup_id="setup-seed",
        plan_version_id="plan-version-seed",
        entry_low="100",
        entry_high="102",
        stop_loss="90",
        tp1="110",
        tp2="120",
        tp3="130",
    )


def _confirmed_observation(**overrides: object) -> LifecycleObservation:
    data: dict[str, object] = {
        "symbol": "BTCUSDT",
        "mode": "swing",
        "direction": "long",
        "readiness_score": 85,
        "readiness_label": "VALID SETUP",
        "quality_score": 82,
        "quality_grade": "B+",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "117",
        "tp3": "124",
        "rr": "3.2",
        "failed_gate": NA,
        "invalidation_reason": "Invalid if price accepts below 95.",
        "sweep_detected": True,
        "structure_shift_detected": True,
        "pullback_valid": True,
        "rr_valid": True,
        "valid_trade_idea": True,
        "core_status": "idea_created",
        "setup_quality_state": "high_quality_trade",
        "technical_score": "70",
        "opportunity_score": "88",
        "min_technical_score": "50",
        "min_opportunity_score": "80",
        "actionable_a_grade_candidate": True,
        "entry_filled": False,
    }
    data.update(overrides)
    return LifecycleObservation(**data)


def test_schema_version_is_unchanged() -> None:
    assert SCHEMA_VERSION == 21
    assert evidence_contract_payload()["activation_accounting"]["schema_version_unchanged"] is True
    assert CONTRACT_VERSION == "cci-evidence-contract-v2"


def test_watch_producer_is_alert_list_not_fill() -> None:
    result = _scan_result()
    summary = build_watch_iteration_summary(
        iteration=1,
        result=result,
        activations=(
            WatchActivation(
                symbol="BTCUSDT",
                mode="swing",
                message="alert",
                delivery_status="dry_run",
                delivery_detail="Dry run.",
            ),
        ),
        next_scan_seconds=None,
        scanned_at="2026-09-01T00:00:00+00:00",
    )
    assert summary.valid_activations == 1
    assert current_result_is_valid_activation(result.results[0]) is False
    assert SetupTransitionReason.ENTRY_ACTIVATED.value not in summary.model_dump_json()


def test_non_watch_store_keeps_default_zero_and_is_not_watch_scoped(tmp_path: Path) -> None:
    db_path = tmp_path / "scan.sqlite"
    run_id = store_scan_result(db_path, _scan_result())
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT is_watch_iteration, valid_activations FROM scan_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
    assert row[0] == 0
    assert row[1] == 0
    payload = build_evidence_baseline(
        db_path,
        start="2020-01-01T00:00:00Z",
        cutoff="2100-01-01T00:00:00Z",
    )
    assert payload["activation_accounting"]["watch_alert_activations"]["status"] == UNAVAILABLE
    assert payload["activation_accounting"]["watch_alert_activations"]["value"] is None
    assert payload["scan_run_counters"]["valid_activations"]["status"] == UNSAFE


def test_watch_store_preserves_alert_count(tmp_path: Path) -> None:
    db_path = tmp_path / "watch.sqlite"
    store_scan_result(
        db_path,
        _scan_result(),
        watch_iteration=WatchIterationMetadata(
            iteration_number=1,
            started_at="2026-09-01T00:00:00+00:00",
            completed_at="2026-09-01T00:01:00+00:00",
            symbols_requested=1,
            symbols_queued=1,
            symbols_completed=1,
            valid_activations=2,
            still_watching=0,
            rejected_no_edge=0,
            data_issues=0,
            runtime_sec=1.0,
        ),
    )
    payload = build_evidence_baseline(
        db_path,
        start="2020-01-01T00:00:00Z",
        cutoff="2100-01-01T00:00:00Z",
    )
    watch = payload["activation_accounting"]["watch_alert_activations"]
    assert watch["status"] == "available"
    assert watch["value"] == 2
    assert watch["watch_iterations_in_window"] == 1


def test_watch_complete_zero_is_available_zero(tmp_path: Path) -> None:
    path = tmp_path / "watch-zero.sqlite"
    _accounting_schema(path)
    _write_sql(
        path,
        """
        INSERT INTO scan_runs(run_id, timestamp, is_watch_iteration, valid_activations)
        VALUES ('w1', '2026-09-01T12:00:00+00:00', 1, 0);
        """,
    )
    payload = build_evidence_baseline(path, start=START, cutoff=CUTOFF)
    watch = payload["activation_accounting"]["watch_alert_activations"]
    assert watch["status"] == "available"
    assert watch["value"] == 0
    assert "complete zero" in watch["zero_semantics"].lower() or "produced no" in watch["zero_semantics"]


def test_trigger_and_confirmation_events_are_not_entry_activation(tmp_path: Path) -> None:
    path = tmp_path / "trigger.sqlite"
    _accounting_schema(path)
    _write_sql(
        path,
        f"""
        INSERT INTO scan_runs(run_id, timestamp, is_watch_iteration, valid_activations)
        VALUES ('s1', '2026-09-01T12:00:00+00:00', 0, 0);
        INSERT INTO setup_lifecycle_events(lifecycle_id, timestamp, reason, scan_run_id, from_state, to_state)
        VALUES
            ('life-1', '2026-09-01T12:00:00+00:00', '{SetupTransitionReason.STRUCTURE_SHIFT_CONFIRMED.value}', 's1', 'STALKING', 'TRIGGERED'),
            ('life-1', '2026-09-01T12:05:00+00:00', '{SetupTransitionReason.PULLBACK_RR_VALID.value}', 's1', 'TRIGGERED', 'CONFIRMED');
        """,
    )
    payload = build_evidence_baseline(path, start=START, cutoff=CUTOFF)
    accounting = payload["activation_accounting"]
    assert accounting["entry_activated_event_records"]["value"] == 0
    assert accounting["entry_zone_touched_event_records"]["value"] == 0
    assert accounting["entry_fill_simulated_event_records"]["value"] == 0
    assert accounting["fill_occurrence_count"]["status"] == UNAVAILABLE


def test_zone_touch_is_not_activation_or_fill(tmp_path: Path) -> None:
    path = tmp_path / "touch.sqlite"
    _accounting_schema(path)
    _write_sql(
        path,
        f"""
        INSERT INTO scan_runs(run_id, timestamp, is_watch_iteration, valid_activations)
        VALUES ('s1', '2026-09-01T12:00:00+00:00', 0, 0);
        INSERT INTO setup_lifecycle_events(lifecycle_id, timestamp, reason, scan_run_id, from_state, to_state)
        VALUES ('life-1', '2026-09-01T12:00:00+00:00', '{SetupTransitionReason.ENTRY_ZONE_TOUCHED.value}', 's1', 'WATCHLISTED', 'TRIGGERED');
        """,
    )
    payload = build_evidence_baseline(path, start=START, cutoff=CUTOFF)
    accounting = payload["activation_accounting"]
    assert accounting["entry_zone_touched_event_records"]["value"] == 1
    assert accounting["entry_activated_event_records"]["value"] == 0
    assert accounting["entry_fill_simulated_event_records"]["value"] == 0
    assert accounting["manual_fill_count"]["status"] == UNAVAILABLE


def test_entry_activated_event_records_count_structured_reason(tmp_path: Path) -> None:
    path = tmp_path / "activated.sqlite"
    _accounting_schema(path)
    _write_sql(
        path,
        f"""
        INSERT INTO scan_runs(run_id, timestamp, is_watch_iteration, valid_activations)
        VALUES ('s1', '2026-09-01T12:00:00+00:00', 0, 0);
        INSERT INTO setup_lifecycle_events(lifecycle_id, timestamp, reason, scan_run_id, from_state, to_state)
        VALUES
            ('life-1', '2026-09-01T12:00:00+00:00', '{SetupTransitionReason.ENTRY_ACTIVATED.value}', 's1', 'CONFIRMED', 'CONFIRMED'),
            ('life-1', '2026-09-01T12:00:01+00:00', '{SetupTransitionReason.ENTRY_FILL_SIMULATED.value}', 's1', 'CONFIRMED', 'EXECUTING'),
            ('life-2', '2026-09-01T13:00:00+00:00', '{SetupTransitionReason.ENTRY_ACTIVATED.value}', NULL, 'CONFIRMED', 'CONFIRMED'),
            ('life-1', '2026-09-03T00:00:00+00:00', '{SetupTransitionReason.ENTRY_ACTIVATED.value}', 's1', 'CONFIRMED', 'CONFIRMED');
        """,
    )
    payload = build_evidence_baseline(path, start=START, cutoff=CUTOFF)
    activated = payload["activation_accounting"]["entry_activated_event_records"]
    simulated = payload["activation_accounting"]["entry_fill_simulated_event_records"]
    assert activated["status"] == "available"
    assert activated["value"] == 2
    assert activated["records_missing_scan_run_id"] == 1
    assert simulated["value"] == 1
    assert simulated["simulated_versus_confirmed"] == "indistinguishable"
    assert payload["activation_accounting"]["unique_activation_occurrence_count"]["status"] == UNAVAILABLE


def test_executing_state_without_event_is_not_an_activation(tmp_path: Path) -> None:
    path = tmp_path / "executing.sqlite"
    _accounting_schema(path)
    _write_sql(
        path,
        """
        INSERT INTO scan_runs(run_id, timestamp, is_watch_iteration, valid_activations)
        VALUES ('s1', '2026-09-01T12:00:00+00:00', 0, 0);
        INSERT INTO setup_lifecycle_records(lifecycle_id, current_state, setup_id, plan_version_id, last_seen_at)
        VALUES ('life-1', 'EXECUTING', 'setup-seed', 'plan-version-seed', '2026-09-01T12:00:00+00:00');
        """,
    )
    payload = build_evidence_baseline(path, start=START, cutoff=CUTOFF)
    assert payload["activation_accounting"]["entry_activated_event_records"]["value"] == 0
    assert payload["activation_accounting"]["entry_fill_simulated_event_records"]["value"] == 0


def test_projection_is_idempotent_and_does_not_collapse_distinct_events() -> None:
    rows = (
        {
            "event_id": 1,
            "reason": SetupTransitionReason.ENTRY_ACTIVATED.value,
            "scan_run_id": "s1",
        },
        {
            "event_id": 2,
            "reason": SetupTransitionReason.ENTRY_ACTIVATED.value,
            "scan_run_id": "s1",
        },
    )
    columns = {"event_id", "reason", "scan_run_id", "timestamp"}
    first = project_activation_accounting(
        scan_rows=(),
        scan_columns={"valid_activations", "is_watch_iteration"},
        event_rows=rows,
        event_columns=columns,
        events_table_available=True,
    )
    second = project_activation_accounting(
        scan_rows=(),
        scan_columns={"valid_activations", "is_watch_iteration"},
        event_rows=rows,
        event_columns=columns,
        events_table_available=True,
    )
    assert first == second
    assert first["entry_activated_event_records"]["value"] == 2


def test_closed_candle_producer_counts_one_activation_on_retry(tmp_path: Path) -> None:
    db_path = tmp_path / "outcomes.sqlite"
    candles = [_candle(0, high="103", low="99")]
    evaluated_at = (BASE + timedelta(minutes=5)).isoformat()
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        record = _lifecycle_record(state=SetupLifecycleState.ACTIONABLE_A_GRADE)
        repository.upsert_record(record)
        first = evaluate_closed_candle_outcomes(
            record,
            execution_candles=candles,
            execution_timeframe="5m",
            decision_timestamp=evaluated_at,
            evaluated_at=evaluated_at,
            repository=repository,
            scan_run_id="scan-0",
        )
        assert first.progress is not None
        assert first.progress.entry_at is not None
        second = evaluate_closed_candle_outcomes(
            first.record,
            execution_candles=candles,
            execution_timeframe="5m",
            decision_timestamp=evaluated_at,
            evaluated_at=evaluated_at,
            repository=repository,
            scan_run_id="scan-0",
        )
        events = repository.list_events(lifecycle_id=record.lifecycle_id)
        activated = [event for event in events if event.reason == SetupTransitionReason.ENTRY_ACTIVATED]
        assert len(activated) == 1
        assert second.progress is not None
        assert second.progress.entry_at == first.progress.entry_at
        setup_id = first.record.setup_id
        plan_version_id = first.record.plan_version_id

    payload = build_evidence_baseline(
        db_path,
        start="2020-01-01T00:00:00Z",
        cutoff="2100-01-01T00:00:00Z",
    )
    assert payload["activation_accounting"]["entry_activated_event_records"]["value"] == 1
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT setup_id, plan_version_id FROM setup_lifecycle_records WHERE lifecycle_id = 'life-1'"
        ).fetchone()
    assert row[0] == setup_id == "setup-seed"
    assert row[1] == plan_version_id == "plan-version-seed"


def test_audit_is_read_only_with_activation_events(tmp_path: Path) -> None:
    path = tmp_path / "frozen.sqlite"
    _accounting_schema(path)
    _write_sql(
        path,
        f"""
        INSERT INTO scan_runs(run_id, timestamp, is_watch_iteration, valid_activations)
        VALUES ('s1', '2026-09-01T12:00:00+00:00', 1, 0);
        INSERT INTO setup_lifecycle_events(lifecycle_id, timestamp, reason, scan_run_id, from_state, to_state)
        VALUES ('life-1', '2026-09-01T12:00:00+00:00', '{SetupTransitionReason.ENTRY_ACTIVATED.value}', 's1', 'CONFIRMED', 'CONFIRMED');
        INSERT INTO setup_lifecycle_records(lifecycle_id, current_state, setup_id, plan_version_id, last_seen_at)
        VALUES ('life-1', 'MANAGING', 'setup-seed', 'plan-version-seed', '2026-09-01T12:00:00+00:00');
        """,
    )
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    first = dumps_evidence_payload(build_evidence_baseline(path, start=START, cutoff=CUTOFF))
    second = dumps_evidence_payload(build_evidence_baseline(path, start=START, cutoff=CUTOFF))
    assert first == second
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_confirmed_actionable_oscillation_is_no_longer_produced() -> None:
    """P2B regression of the P2A-characterized CONFIRMED/ACTIONABLE ping-pong.

    Former defect (compact pre-edit trace, identical confirmed-ready + A-grade
    observation, unfilled): CONFIRMED → ACTIONABLE_A_GRADE (ACTIONABLE_A_GRADE)
    → CONFIRMED (PULLBACK_RR_VALID) → ACTIONABLE_A_GRADE → ...
    Prospective repair: stay CONFIRMED; do not invent EXECUTING from that
    unfilled observation.
    """

    record = _lifecycle_record(state=SetupLifecycleState.CONFIRMED).model_copy(
        update={
            "mode": "swing",
            "stop_loss": "95",
            "tp2": "117",
            "tp3": "124",
            "confirmation_count": 2,
            "required_confirmation_cycles": 2,
            "invalidation_reason": "Invalid if price accepts below 95.",
            "invalidation_logic": "Invalid if price accepts below 95.",
            "setup_identity": "BTCUSDT|swing|long|100|102|95|Invalid if price accepts below 95.",
        }
    )
    observation = _confirmed_observation(actionability_state="A_GRADE_ACTIONABLE")
    first = evaluate_lifecycle_transition(
        record,
        observation,
        lifecycle_id=record.lifecycle_id,
        now="2026-09-01T12:00:00+00:00",
    )
    assert first.transitioned is False
    assert first.to_state == SetupLifecycleState.CONFIRMED
    assert first.reason == SetupTransitionReason.NO_CHANGE
    assert first.event is None
    assert first.record is not None
    assert first.record.current_state == SetupLifecycleState.CONFIRMED
    assert first.record.confirmation_count == 2
    assert first.record.confirmed_at == record.confirmed_at
    assert first.record.actionability_state == "A_GRADE_ACTIONABLE"

    second = evaluate_lifecycle_transition(
        first.record,
        observation,
        lifecycle_id=record.lifecycle_id,
        now="2026-09-01T12:05:00+00:00",
    )
    assert second.transitioned is False
    assert second.to_state == SetupLifecycleState.CONFIRMED
    assert second.reason == SetupTransitionReason.NO_CHANGE
    assert second.event is None
    assert second.record is not None
    assert second.record.current_state == SetupLifecycleState.CONFIRMED
    assert second.record.last_transition_at == first.record.last_transition_at


def test_accounting_module_is_not_an_operational_input() -> None:
    from app.alerts import telegram_lifecycle
    from app.lifecycle import service, state_machine
    from app.watch_mode import build_watch_iteration_summary as watch_summary

    banned = "activation_accounting"
    assert banned not in inspect.getsource(state_machine)
    assert banned not in inspect.getsource(service)
    assert banned not in inspect.getsource(telegram_lifecycle)
    assert banned not in inspect.getsource(watch_summary)
    assert SCHEMA_VERSION == 21


def test_fresh_database_stays_on_schema_v21(tmp_path: Path) -> None:
    path = tmp_path / "fresh.sqlite"
    with open_initialized_database(path) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(scan_runs)").fetchall()
        }
    assert user_version == 21
    assert "valid_activations" in columns
