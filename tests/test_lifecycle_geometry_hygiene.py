from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import telegram_alert_decision_for_symbol
from app.data.dtos import NA
from app.lifecycle.hygiene import (
    QUARANTINE_REASON_CODE,
    LifecycleHygieneError,
    apply_invalid_lifecycle_geometry_quarantine,
    audit_invalid_lifecycle_geometry,
)
from app.lifecycle.models import (
    SetupLifecycleOutcomeProgress,
    SetupLifecycleRecord,
    SetupLifecycleState,
    SetupTransitionReason,
    SetupTransitionResult,
)
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerSymbolResult
from app.storage.database import SCHEMA_VERSION, open_initialized_database
from scripts.repair_lifecycle_hygiene import main


NOW = "2026-07-21T09:00:00Z"


def _repository(connection: sqlite3.Connection) -> SQLiteSetupLifecycleRepository:
    repository = SQLiteSetupLifecycleRepository()
    repository.connection = connection
    return repository


def _record(
    lifecycle_id: str,
    symbol: str,
    state: SetupLifecycleState,
    *,
    direction: str = "n/a",
    invalid_long_order: bool = False,
    actionability: str = "NOT_A_GRADE_CANDIDATE",
) -> SetupLifecycleRecord:
    levels = {
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "117",
        "tp3": "124",
    }
    if direction == "short":
        levels = {
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "107",
            "tp1": "92",
            "tp2": "85",
            "tp3": "78",
        }
    if direction == "n/a":
        levels = {key: NA for key in levels}
    if invalid_long_order:
        levels["stop_loss"] = "103"
    return SetupLifecycleRecord(
        lifecycle_id=lifecycle_id,
        symbol=symbol,
        mode="scalp",
        direction=direction,
        current_state=state,
        first_seen_at=NOW,
        last_seen_at=NOW,
        last_transition_at=NOW,
        failed_gate=NA,
        actionability_state=actionability,
        readiness_score=50,
        quality_score=50,
        invalidation_reason="A close beyond the stored stop invalidates the plan.",
        invalidation_logic="A close beyond the stored stop invalidates the plan.",
        rr="3.2",
        setup_identity=f"{symbol}|scalp|{direction}|legacy",
        **levels,
    )


def _insert_attempt(
    connection: sqlite3.Connection,
    lifecycle_id: str,
    *,
    status: str,
    sent_at: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO telegram_alert_attempts (
            signal_id, symbol, direction, previous_state, new_state, alert_type,
            lifecycle_state, sent_at, attempted_at, telegram_status, message_hash
        ) VALUES (?, 'BADUSDT', 'long', 'N/A', 'TRIGGERED', 'WATCHLIST',
                  'TRIGGERED', ?, 'N/A', ?, ?)
        """,
        (lifecycle_id, sent_at, status, f"hash-{lifecycle_id}-{status}"),
    )


def _insert_malformed_progress(
    repository: SQLiteSetupLifecycleRepository,
    record: SetupLifecycleRecord,
    *,
    diagnostic: str = "invalid_stored_plan_geometry:unsupported_direction",
    entry_at: str | None = None,
) -> None:
    repository.upsert_outcome_progress(
        SetupLifecycleOutcomeProgress(
            lifecycle_id=record.lifecycle_id,
            plan_identity=canonical_plan_identity(record),
            symbol=record.symbol,
            mode=record.mode,
            direction=record.direction,
            execution_timeframe="15m",
            entry_at=entry_at,
            integrity_status="Failed",
            diagnostic=diagnostic,
            first_evaluated_at=NOW,
            last_evaluated_at=NOW,
        )
    )


def _logical_rows(connection: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
    return tuple(
        tuple(row[column] for column in columns)
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
    )


def _fixture_connection(tmp_path: Path) -> sqlite3.Connection:
    return open_initialized_database(tmp_path / "geometry.sqlite")


def test_six_legacy_active_records_are_detected_and_legally_quarantined(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        records = (
            _record("b3ce0902d8ea4900b1c466b65b02dcf7", "NEARUSDT", SetupLifecycleState.STALKING),
            _record("985ec9432371493b8d78f112bbe17863", "BZUSDT", SetupLifecycleState.TRIGGERED),
            _record("8833d88e94d340c4af3f43cf5776e1c7", "ESPORTSUSDT", SetupLifecycleState.TRIGGERED),
            _record("43b07bc896954ebda8e7cca3390667ad", "LINKUSDT", SetupLifecycleState.TRIGGERED),
            _record("793e9d4f50724bf59f3efcabe6fd724f", "MUUSDT", SetupLifecycleState.TRIGGERED),
            _record("a9b4aafabded4fe7ac1910216ee137ac", "XRPUSDT", SetupLifecycleState.TRIGGERED),
        )
        for record in records:
            repository.upsert_record(record)
        connection.commit()
        original_geometry = connection.execute(
            "SELECT lifecycle_id, direction, entry_low, entry_high, stop_loss, tp1, tp2, tp3, invalidation_logic "
            "FROM setup_lifecycle_records ORDER BY lifecycle_id"
        ).fetchall()

        plan = audit_invalid_lifecycle_geometry(connection)

        assert plan.schema_version == SCHEMA_VERSION == 16
        assert {item.lifecycle_id for item in plan.safe_to_quarantine} == {record.lifecycle_id for record in records}
        proposed = {item.symbol: item.proposed_state for item in plan.safe_to_quarantine}
        assert proposed["NEARUSDT"] == SetupLifecycleState.REJECTED.value
        assert {proposed[symbol] for symbol in proposed if symbol != "NEARUSDT"} == {
            SetupLifecycleState.INVALIDATED.value
        }

        applied = apply_invalid_lifecycle_geometry_quarantine(connection, plan, now=NOW)

        assert applied.applied_count == 6
        assert connection.execute("SELECT COUNT(*) FROM setup_lifecycle_events").fetchone()[0] == 6
        states = dict(
            connection.execute("SELECT symbol, current_state FROM setup_lifecycle_records").fetchall()
        )
        assert states["NEARUSDT"] == SetupLifecycleState.REJECTED.value
        assert all(states[symbol] == SetupLifecycleState.INVALIDATED.value for symbol in states if symbol != "NEARUSDT")
        assert connection.execute(
            "SELECT lifecycle_id, direction, entry_low, entry_high, stop_loss, tp1, tp2, tp3, invalidation_logic "
            "FROM setup_lifecycle_records ORDER BY lifecycle_id"
        ).fetchall() == original_geometry
        assert connection.execute(
            "SELECT COUNT(*) FROM setup_lifecycle_outcome_progress"
        ).fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM setup_outcome_analytics").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM telegram_alert_attempts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM public_alert_events").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM public_alert_delivery_parts").fetchone()[0] == 0
        assert connection.execute("SELECT COUNT(*) FROM scan_runs").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    finally:
        connection.close()


def test_hygiene_preserves_valid_and_historical_rows_and_escalates_dependencies(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        valid_long = _record("valid-long", "VALIDLONG", SetupLifecycleState.REJECTED, direction="long")
        valid_short = _record("valid-short", "VALIDSHORT", SetupLifecycleState.REJECTED, direction="short")
        terminal = _record("terminal", "HISTORY", SetupLifecycleState.REJECTED)
        quarantined = _record("quarantined", "QUAR", SetupLifecycleState.INVALIDATED)
        quarantined = quarantined.model_copy(update={"failed_gate": QUARANTINE_REASON_CODE})
        sent = _record("sent", "SENTUSDT", SetupLifecycleState.TRIGGERED)
        reserved = _record("reserved", "RESERVED", SetupLifecycleState.TRIGGERED)
        public_event = _record("public-event", "EVENTUSDT", SetupLifecycleState.TRIGGERED)
        executing = _record("executing", "EXECUTING", SetupLifecycleState.EXECUTING)
        managing = _record("managing", "MANAGING", SetupLifecycleState.MANAGING)
        for record in (valid_long, valid_short, terminal, quarantined, sent, reserved, public_event, executing, managing):
            repository.upsert_record(record)
        _insert_malformed_progress(repository, terminal)
        _insert_attempt(connection, sent.lifecycle_id, status="sent", sent_at=NOW)
        _insert_attempt(connection, reserved.lifecycle_id, status="reserved")
        connection.execute(
            """
            INSERT INTO public_alert_events (
                canonical_plan_id, event_type, event_key, symbol, side, status, delivery_state
            ) VALUES ('unmatchable-public-plan', 'INITIAL_WATCHLIST', 'eventusdt-legacy',
                      'EVENTUSDT', 'long', 'sent', 'SENT')
            """
        )
        connection.commit()
        terminal_before = _logical_rows(connection, "setup_lifecycle_records")
        progress_before = _logical_rows(connection, "setup_lifecycle_outcome_progress")

        plan = audit_invalid_lifecycle_geometry(connection)
        classes = {item.lifecycle_id: item for item in plan.items}

        assert "valid-long" not in classes
        assert "valid-short" not in classes
        assert classes[terminal.lifecycle_id].classification == "historical_preserve"
        assert classes[quarantined.lifecycle_id].reasons == (
            "non_outcome_eligible_preserve_unchanged",
            "already_quarantined",
        )
        assert classes[sent.lifecycle_id].classification == "requires_manual_review"
        assert "confirmed_public_delivery" in classes[sent.lifecycle_id].reasons
        assert classes[reserved.lifecycle_id].classification == "requires_manual_review"
        assert "public_delivery_reservation_or_uncertainty" in classes[reserved.lifecycle_id].reasons
        assert classes[public_event.lifecycle_id].classification == "requires_manual_review"
        assert "confirmed_public_delivery" in classes[public_event.lifecycle_id].reasons
        assert classes[executing.lifecycle_id].classification == "requires_manual_review"
        assert "execution_or_managing_state" in classes[executing.lifecycle_id].reasons
        assert classes[managing.lifecycle_id].classification == "requires_manual_review"
        assert "execution_or_managing_state" in classes[managing.lifecycle_id].reasons

        apply_invalid_lifecycle_geometry_quarantine(connection, plan, now=NOW)

        assert _logical_rows(connection, "setup_lifecycle_records") == terminal_before
        assert _logical_rows(connection, "setup_lifecycle_outcome_progress") == progress_before
    finally:
        connection.close()


def test_hygiene_dry_run_and_repeated_apply_are_idempotent(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "geometry.sqlite"
    connection = open_initialized_database(db_path)
    try:
        _repository(connection).upsert_record(
            _record("repeat", "REPEAT", SetupLifecycleState.STALKING)
        )
        connection.commit()
        before = _logical_rows(connection, "setup_lifecycle_records")
    finally:
        connection.close()

    assert main(["--database-path", str(db_path)]) == 0
    first_dry_run = json.loads(capsys.readouterr().out)
    assert main(["--database-path", str(db_path)]) == 0
    second_dry_run = json.loads(capsys.readouterr().out)
    assert first_dry_run["counts"]["safe_to_quarantine"] == 1
    assert first_dry_run["applied_count"] == 0
    assert second_dry_run["fingerprint"] == first_dry_run["fingerprint"]
    assert second_dry_run["applied_count"] == 0

    with sqlite3.connect(db_path) as verify:
        verify.row_factory = sqlite3.Row
        assert _logical_rows(verify, "setup_lifecycle_records") == before

    assert main(
        [
            "--database-path",
            str(db_path),
            "--apply",
            "--confirm",
            QUARANTINE_REASON_CODE,
            "--no-backup",
        ]
    ) == 0
    first_apply = json.loads(capsys.readouterr().out)
    assert first_apply["applied_count"] == 1
    assert main(
        [
            "--database-path",
            str(db_path),
            "--apply",
            "--confirm",
            QUARANTINE_REASON_CODE,
            "--no-backup",
        ]
    ) == 0
    second_apply = json.loads(capsys.readouterr().out)
    assert second_apply["applied_count"] == 0
    assert second_apply["counts"]["malformed_records"] == 1


def test_hygiene_rejects_stale_plans_without_partial_changes(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record("stale", "STALE", SetupLifecycleState.STALKING)
        repository.upsert_record(record)
        connection.commit()
        plan = audit_invalid_lifecycle_geometry(connection)
        connection.execute(
            "UPDATE setup_lifecycle_records SET current_state = 'TRIGGERED' WHERE lifecycle_id = ?",
            (record.lifecycle_id,),
        )
        connection.commit()

        with pytest.raises(LifecycleHygieneError, match="stale"):
            apply_invalid_lifecycle_geometry_quarantine(connection, plan, now=NOW)

        assert connection.execute(
            "SELECT current_state FROM setup_lifecycle_records WHERE lifecycle_id = ?",
            (record.lifecycle_id,),
        ).fetchone()[0] == SetupLifecycleState.TRIGGERED.value
        assert connection.execute("SELECT COUNT(*) FROM setup_lifecycle_events").fetchone()[0] == 0
    finally:
        connection.close()


def test_hygiene_apply_rolls_back_every_record_on_unexpected_failure(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        first = _record("rollback-first", "ROLLONE", SetupLifecycleState.STALKING)
        second = _record("rollback-second", "ROLLTWO", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(first)
        repository.upsert_record(second)
        connection.commit()
        plan = audit_invalid_lifecycle_geometry(connection)
        connection.execute(
            """
            CREATE TRIGGER fail_second_quarantine_event
            BEFORE INSERT ON setup_lifecycle_events
            WHEN NEW.lifecycle_id = 'rollback-second'
            BEGIN
                SELECT RAISE(ABORT, 'simulated event failure');
            END
            """
        )
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError, match="simulated event failure"):
            apply_invalid_lifecycle_geometry_quarantine(connection, plan, now=NOW)

        states = dict(connection.execute("SELECT lifecycle_id, current_state FROM setup_lifecycle_records"))
        assert states == {
            first.lifecycle_id: SetupLifecycleState.STALKING.value,
            second.lifecycle_id: SetupLifecycleState.TRIGGERED.value,
        }
        assert connection.execute("SELECT COUNT(*) FROM setup_lifecycle_events").fetchone()[0] == 0
    finally:
        connection.close()


def test_active_invalid_long_order_is_detected_without_inventing_geometry(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        record = _record(
            "bad-long-order",
            "BADLONG",
            SetupLifecycleState.TRIGGERED,
            direction="long",
            invalid_long_order=True,
        )
        _repository(connection).upsert_record(record)
        connection.commit()

        plan = audit_invalid_lifecycle_geometry(connection)

        assert plan.safe_to_quarantine[0].geometry_failure == "invalid_long_level_order"
        assert plan.safe_to_quarantine[0].proposed_state == SetupLifecycleState.INVALIDATED.value
        assert connection.execute(
            "SELECT direction, stop_loss FROM setup_lifecycle_records WHERE lifecycle_id = ?",
            (record.lifecycle_id,),
        ).fetchone()[:2] == ("long", "103")
    finally:
        connection.close()


def test_malformed_lifecycle_is_not_eligible_for_telegram_reservation_or_delivery() -> None:
    lifecycle = _record("telegram-block", "TELBLOCK", SetupLifecycleState.TRIGGERED)
    transition = SetupTransitionResult(
        lifecycle_id=lifecycle.lifecycle_id,
        symbol=lifecycle.symbol,
        from_state=SetupLifecycleState.STALKING,
        to_state=SetupLifecycleState.TRIGGERED,
        reason=SetupTransitionReason.LEGACY_INVALID_STORED_PLAN_GEOMETRY,
        transitioned=True,
        record=lifecycle,
    )
    symbol = ScannerSymbolResult(
        symbol=lifecycle.symbol,
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        lifecycle_state=lifecycle,
        lifecycle_transition=transition,
    )

    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is False
    assert decision.reason == "invalid_stored_plan_geometry:unsupported_direction:n/a"
    assert decision.alert_type is None
    assert decision.message is None
