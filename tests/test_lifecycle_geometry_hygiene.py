from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import app.lifecycle.hygiene as hygiene
from app.alerts.public_identity import canonical_public_event_key
from app.alerts.telegram_lifecycle import telegram_alert_decision_for_symbol
from app.data.dtos import NA
from app.lifecycle.hygiene import (
    DependencyOwnership,
    LifecycleHygieneError,
    apply_invalid_lifecycle_geometry_quarantine,
    audit_invalid_lifecycle_geometry,
    build_geometry_hygiene_manifest,
)
from app.lifecycle.models import (
    SetupLifecycleOutcomeProgress,
    SetupLifecycleRecord,
    SetupLifecycleState,
    SetupTransitionReason,
    SetupTransitionResult,
)
from app.lifecycle.outcome_policy import canonical_plan_identity, stored_plan_geometry_failure
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


def _valid_historical_plan(record: SetupLifecycleRecord) -> SetupLifecycleRecord:
    return record.model_copy(
        update={
            "direction": "short",
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "107",
            "tp1": "92",
            "tp2": "85",
            "tp3": "78",
            "setup_identity": f"{record.symbol}|scalp|short|historical",
        }
    )


def _insert_current_progress(
    repository: SQLiteSetupLifecycleRepository,
    record: SetupLifecycleRecord,
    *,
    plan_identity: str | None = None,
    diagnostic: str | None = None,
    integrity_status: str = "Failed",
    terminal_outcome: str = NA,
    **timestamps: str | None,
) -> None:
    failure = stored_plan_geometry_failure(record)
    assert failure is not None
    repository.upsert_outcome_progress(
        SetupLifecycleOutcomeProgress(
            lifecycle_id=record.lifecycle_id,
            plan_identity=plan_identity or canonical_plan_identity(record),
            symbol=record.symbol,
            mode=record.mode,
            direction=record.direction,
            execution_timeframe="15m",
            integrity_status=integrity_status,
            diagnostic=diagnostic or f"invalid_stored_plan_geometry:{failure}",
            terminal_outcome=terminal_outcome,
            first_evaluated_at=NOW,
            last_evaluated_at=NOW,
            **timestamps,
        )
    )


def _insert_historical_terminal_progress(
    repository: SQLiteSetupLifecycleRepository,
    record: SetupLifecycleRecord,
) -> str:
    historical = _valid_historical_plan(record)
    historical_identity = canonical_plan_identity(historical)
    repository.upsert_outcome_progress(
        SetupLifecycleOutcomeProgress(
            lifecycle_id=record.lifecycle_id,
            plan_identity=historical_identity,
            symbol=record.symbol,
            mode=historical.mode,
            direction=historical.direction,
            execution_timeframe="15m",
            entry_at="2026-07-20T10:00:00Z",
            invalidated_at="2026-07-20T11:00:00Z",
            outcome_at="2026-07-20T11:00:00Z",
            terminal_outcome=SetupLifecycleState.INVALIDATED.value,
            integrity_status="Verified",
            diagnostic="historical_terminal_outcome",
            first_evaluated_at="2026-07-20T09:00:00Z",
            last_evaluated_at="2026-07-20T11:00:00Z",
        )
    )
    return historical_identity


def _insert_attempt(
    connection: sqlite3.Connection,
    record: SetupLifecycleRecord,
    *,
    status: str,
    delivery_state: str = NA,
    plan_identity: str = NA,
    event_type: str = NA,
    event_key: str = NA,
    signal_id: str | None = None,
    sent_at: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO telegram_alert_attempts (
            signal_id, symbol, direction, previous_state, new_state, alert_type,
            lifecycle_state, sent_at, attempted_at, telegram_status, message_hash,
            delivery_state, public_watchlist_plan_id, public_watchlist_event_key,
            public_alert_event_type
        ) VALUES (?, ?, 'n/a', 'N/A', ?, 'WATCHLIST', ?, ?, ?, ?, ?,
                  ?, ?, ?, ?)
        """,
        (
            signal_id or record.lifecycle_id,
            record.symbol,
            record.current_state.value,
            record.current_state.value,
            sent_at,
            NOW,
            status,
            f"hash-{record.lifecycle_id}-{status}",
            delivery_state,
            plan_identity,
            event_key,
            event_type,
        ),
    )


def _insert_public_event(
    connection: sqlite3.Connection,
    record: SetupLifecycleRecord,
    *,
    plan_identity: str,
    status: str,
    delivery_state: str,
    event_type: str = "initial_watchlist",
    event_key: str | None = None,
    setup_family: str = "liquidity_grab_pullback",
    sent_at: str | None = None,
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO public_alert_events (
            canonical_plan_id, event_type, event_key, symbol, side, setup_family,
            status, delivery_state, sent_at
        ) VALUES (?, ?, ?, ?, 'short', ?, ?, ?, ?)
        """,
        (
            plan_identity,
            event_type,
            event_key or canonical_public_event_key(plan_identity, event_type),
            record.symbol,
            setup_family,
            status,
            delivery_state,
            sent_at,
        ),
    )
    return int(cursor.lastrowid)


def _insert_delivery_part(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    event_key: str,
    delivery_state: str,
    sent_at: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO public_alert_delivery_parts (
            public_alert_event_id, event_key, part_index, part_count,
            payload_text, payload_hash, delivery_state, sent_at
        ) VALUES (?, ?, 1, 1, 'payload', 'payload-hash', ?, ?)
        """,
        (event_id, event_key, delivery_state, sent_at),
    )


def _logical_rows(connection: sqlite3.Connection, table: str) -> tuple[tuple[object, ...], ...]:
    columns = [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")]
    return tuple(
        tuple(row[column] for column in columns)
        for row in connection.execute(f"SELECT * FROM {table} ORDER BY rowid")
    )


def _table_snapshot(
    connection: sqlite3.Connection,
    tables: tuple[str, ...],
) -> dict[str, tuple[tuple[object, ...], ...]]:
    return {table: _logical_rows(connection, table) for table in tables}


def _fixture_connection(tmp_path: Path, name: str = "geometry.sqlite") -> sqlite3.Connection:
    return open_initialized_database(tmp_path / name)


def test_historical_terminal_progress_does_not_block_distinct_current_malformed_plan(
    tmp_path: Path,
) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record("xrp-current", "XRPUSDT", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        _insert_current_progress(repository, record)
        historical_identity = _insert_historical_terminal_progress(repository, record)
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).safe_to_quarantine[0]

        assert item.plan_identity == canonical_plan_identity(record)
        assert item.plan_identity != historical_identity
        assert item.dependency_ownership == (
            DependencyOwnership.CURRENT_PLAN_IDENTITY.value,
            DependencyOwnership.HISTORICAL_NON_CURRENT_PLAN_IDENTITY.value,
        )
        assert item.reasons == ()
        assert item.proposed_state == SetupLifecycleState.INVALIDATED.value
    finally:
        connection.close()


@pytest.mark.parametrize(
    "timestamp_column",
    ("entry_at", "tp1_at", "tp2_at", "tp3_at", "stop_at", "invalidated_at", "outcome_at"),
)
def test_current_plan_market_progress_blocks(
    tmp_path: Path,
    timestamp_column: str,
) -> None:
    connection = _fixture_connection(tmp_path, f"{timestamp_column}.sqlite")
    try:
        repository = _repository(connection)
        record = _record(f"progress-{timestamp_column}", "PROGRESS", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        _insert_current_progress(repository, record, **{timestamp_column: NOW})
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).requires_manual_review[0]

        assert "current_plan_market_progress" in item.reasons
    finally:
        connection.close()


def test_current_plan_terminal_outcome_blocks(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record("terminal-current", "TERMCUR", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        _insert_current_progress(
            repository,
            record,
            terminal_outcome=SetupLifecycleState.INVALIDATED.value,
        )
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).requires_manual_review[0]

        assert "current_plan_terminal_outcome" in item.reasons
    finally:
        connection.close()


def test_verified_current_plan_integrity_blocks(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record("verified-current", "VERIFIED", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        _insert_current_progress(repository, record, integrity_status="Verified")
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).requires_manual_review[0]

        assert "current_plan_verified_integrity" in item.reasons
    finally:
        connection.close()


@pytest.mark.parametrize("identity_case", ("missing", "mismatched", "malformed"))
def test_missing_mismatched_or_malformed_current_plan_identity_blocks(
    tmp_path: Path,
    identity_case: str,
) -> None:
    connection = _fixture_connection(tmp_path, f"{identity_case}.sqlite")
    try:
        repository = _repository(connection)
        record = _record(f"identity-{identity_case}", "IDENTITY", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        if identity_case == "mismatched":
            _insert_historical_terminal_progress(repository, record)
        elif identity_case == "malformed":
            _insert_current_progress(repository, record, plan_identity="malformed-plan")
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).requires_manual_review[0]

        assert "missing_current_plan_outcome_progress" in item.reasons
        if identity_case == "malformed":
            assert "ambiguous_or_malformed_outcome_plan_identity" in item.reasons
            assert DependencyOwnership.AMBIGUOUS_UNPROVEN.value in item.dependency_ownership
    finally:
        connection.close()


def test_non_geometry_only_current_progress_blocks(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record("wrong-diagnostic", "WRONGDIAG", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        _insert_current_progress(repository, record, diagnostic="missing_execution_timeframe")
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).requires_manual_review[0]

        assert "current_plan_progress_not_failed_geometry_only" in item.reasons
    finally:
        connection.close()


def test_direct_current_plan_sent_attempt_blocks(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record("direct-sent", "DIRECT", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        _insert_current_progress(repository, record)
        plan_identity = canonical_plan_identity(record)
        event_type = "initial_watchlist"
        _insert_attempt(
            connection,
            record,
            status="sent",
            delivery_state="SENT",
            plan_identity=plan_identity,
            event_type=event_type,
            event_key=canonical_public_event_key(plan_identity, event_type),
            sent_at=NOW,
        )
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).requires_manual_review[0]

        assert "confirmed_public_delivery" in item.reasons
        assert (
            DependencyOwnership.DIRECT_CURRENT_PLAN_PUBLIC_DELIVERY.value
            in item.dependency_ownership
        )
    finally:
        connection.close()


@pytest.mark.parametrize(
    ("status", "delivery_state"),
    (
        ("RESERVED", "PENDING"),
        ("FAILED", "RETRYABLE"),
        ("RESERVED", "UNCERTAIN"),
    ),
)
def test_reserved_retryable_and_uncertain_direct_delivery_blocks(
    tmp_path: Path,
    status: str,
    delivery_state: str,
) -> None:
    connection = _fixture_connection(tmp_path, f"{delivery_state}.sqlite")
    try:
        repository = _repository(connection)
        record = _record(f"delivery-{delivery_state}", "DELIVERY", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        _insert_current_progress(repository, record)
        _insert_public_event(
            connection,
            record,
            plan_identity=canonical_plan_identity(record),
            status=status,
            delivery_state=delivery_state,
        )
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).requires_manual_review[0]

        assert "public_delivery_reservation_or_uncertainty" in item.reasons
    finally:
        connection.close()


def test_direct_current_plan_retryable_delivery_part_blocks(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record("part-retry", "PARTRETRY", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        _insert_current_progress(repository, record)
        plan_identity = canonical_plan_identity(record)
        event_key = canonical_public_event_key(plan_identity, "initial_watchlist")
        event_id = _insert_public_event(
            connection,
            record,
            plan_identity=plan_identity,
            status="FAILED",
            delivery_state="FAILED_FINAL",
            event_key=event_key,
        )
        _insert_delivery_part(
            connection,
            event_id=event_id,
            event_key=event_key,
            delivery_state="RETRYABLE",
        )
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).requires_manual_review[0]

        assert "public_delivery_reservation_or_uncertainty" in item.reasons
    finally:
        connection.close()


def test_lifecycle_only_delivery_with_missing_plan_identity_fails_closed(
    tmp_path: Path,
) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record("ambiguous-delivery", "AMBIG", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        _insert_current_progress(repository, record)
        _insert_attempt(connection, record, status="sent", sent_at=NOW)
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).requires_manual_review[0]

        assert "ambiguous_public_delivery_identity" in item.reasons
        assert DependencyOwnership.AMBIGUOUS_UNPROVEN.value in item.dependency_ownership
    finally:
        connection.close()


def test_same_symbol_different_plan_xrp_delivery_does_not_block(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record("xrp-malformed", "XRPUSDT", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        _insert_current_progress(repository, record)
        xrp_plan_id = "XRPUSDT|short|e8e2f7760b3797881ed7"
        xrp_event_key = canonical_public_event_key(xrp_plan_id, "initial_watchlist")
        _insert_public_event(
            connection,
            record,
            plan_identity=xrp_plan_id,
            event_key=xrp_event_key,
            status="SENT",
            delivery_state="SENT",
            sent_at=NOW,
        )
        _insert_attempt(
            connection,
            record,
            signal_id="XRPUSDT-WATCH-e8e2f7760b3797881ed7",
            status="sent",
            delivery_state="SENT",
            plan_identity=xrp_plan_id,
            event_type="initial_watchlist",
            event_key=xrp_event_key,
            sent_at=NOW,
        )
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).safe_to_quarantine[0]

        assert "confirmed_public_delivery" not in item.reasons
        assert item.plan_identity != xrp_plan_id
        assert (
            DependencyOwnership.HISTORICAL_NON_CURRENT_PLAN_IDENTITY.value
            in item.dependency_ownership
        )
        assert (
            DependencyOwnership.DIRECT_CURRENT_PLAN_PUBLIC_DELIVERY.value
            not in item.dependency_ownership
        )
    finally:
        connection.close()


def test_legal_and_illegal_quarantine_transitions_are_distinguished(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record("legal-transition", "LEGAL", SetupLifecycleState.STALKING)
        repository.upsert_record(record)
        _insert_current_progress(repository, record)
        connection.commit()

        legal = audit_invalid_lifecycle_geometry(connection).safe_to_quarantine[0]
        assert legal.proposed_state == SetupLifecycleState.REJECTED.value

        monkeypatch.setitem(
            hygiene.SAFE_QUARANTINE_TRANSITIONS,
            SetupLifecycleState.STALKING.value,
            SetupLifecycleState.INVALIDATED,
        )
        illegal = audit_invalid_lifecycle_geometry(connection).requires_manual_review[0]
        assert illegal.reasons == ("unexpected_illegal_quarantine_transition",)
    finally:
        connection.close()


@pytest.mark.parametrize(
    "state",
    (
        SetupLifecycleState.EXECUTING,
        SetupLifecycleState.MANAGING,
        SetupLifecycleState.TP_HIT,
        SetupLifecycleState.SL_HIT,
    ),
)
def test_execution_and_terminal_market_states_block_automatic_quarantine(
    tmp_path: Path,
    state: SetupLifecycleState,
) -> None:
    connection = _fixture_connection(tmp_path, f"{state.value}.sqlite")
    try:
        repository = _repository(connection)
        record = _record(f"state-{state.value}", "STATEBLOCK", state)
        repository.upsert_record(record)
        if state in {SetupLifecycleState.EXECUTING, SetupLifecycleState.MANAGING}:
            _insert_current_progress(repository, record)
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).requires_manual_review[0]

        assert item.reasons == ("execution_or_terminal_market_state",)
    finally:
        connection.close()


def test_synthetic_manifest_apply_preserves_all_historical_tables(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    historical_tables = (
        "setup_lifecycle_outcome_progress",
        "setup_outcome_analytics",
        "telegram_alert_attempts",
        "public_alert_events",
        "public_alert_delivery_parts",
    )
    try:
        repository = _repository(connection)
        record = _record("controlled-apply", "XRPUSDT", SetupLifecycleState.TRIGGERED)
        repository.upsert_record(record)
        _insert_current_progress(repository, record)
        _insert_historical_terminal_progress(repository, record)
        connection.execute(
            """
            INSERT INTO setup_outcome_analytics (
                lifecycle_id, symbol, first_seen_at, final_outcome
            ) VALUES (?, ?, ?, 'INVALIDATED')
            """,
            (record.lifecycle_id, record.symbol, NOW),
        )
        historical_plan = "XRPUSDT|short|e8e2f7760b3797881ed7"
        historical_event_key = canonical_public_event_key(
            historical_plan,
            "initial_watchlist",
        )
        event_id = _insert_public_event(
            connection,
            record,
            plan_identity=historical_plan,
            event_key=historical_event_key,
            status="SENT",
            delivery_state="SENT",
            sent_at=NOW,
        )
        _insert_delivery_part(
            connection,
            event_id=event_id,
            event_key=historical_event_key,
            delivery_state="SENT",
            sent_at=NOW,
        )
        _insert_attempt(
            connection,
            record,
            signal_id="XRPUSDT-WATCH-e8e2f7760b3797881ed7",
            status="sent",
            delivery_state="SENT",
            plan_identity=historical_plan,
            event_type="initial_watchlist",
            event_key=historical_event_key,
            sent_at=NOW,
        )
        connection.commit()
        before = _table_snapshot(connection, historical_tables)
        plan = audit_invalid_lifecycle_geometry(connection)
        manifest = build_geometry_hygiene_manifest(plan)

        applied = apply_invalid_lifecycle_geometry_quarantine(
            connection,
            plan,
            manifest=manifest,
            now=NOW,
        )

        assert applied.applied_count == 1
        assert _table_snapshot(connection, historical_tables) == before
        assert connection.execute(
            "SELECT current_state FROM setup_lifecycle_records WHERE lifecycle_id = ?",
            (record.lifecycle_id,),
        ).fetchone()[0] == SetupLifecycleState.INVALIDATED.value
        assert connection.execute("SELECT COUNT(*) FROM setup_lifecycle_events").fetchone()[0] == 1
    finally:
        connection.close()


def test_dry_run_produces_no_database_writes(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "dry-run.sqlite"
    connection = open_initialized_database(db_path)
    tables = (
        "setup_lifecycle_records",
        "setup_lifecycle_events",
        "setup_lifecycle_outcome_progress",
        "setup_outcome_analytics",
        "telegram_alert_attempts",
        "public_alert_events",
        "public_alert_delivery_parts",
    )
    try:
        repository = _repository(connection)
        record = _record("dry-run", "DRYRUN", SetupLifecycleState.STALKING)
        repository.upsert_record(record)
        _insert_current_progress(repository, record)
        connection.commit()
        before = _table_snapshot(connection, tables)
    finally:
        connection.close()

    assert main(["--database-path", str(db_path)]) == 0
    payload = json.loads(capsys.readouterr().out)

    with sqlite3.connect(db_path) as verify:
        verify.row_factory = sqlite3.Row
        assert _table_snapshot(verify, tables) == before
    assert payload["counts"]["safe_to_quarantine"] == 1
    assert payload["applied_count"] == 0
    assert payload["manifest_template"]["items"][0]["lifecycle_id"] == record.lifecycle_id


def test_manifest_mismatch_aborts_without_partial_application(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        first = _record("manifest-first", "MANONE", SetupLifecycleState.STALKING)
        second = _record("manifest-second", "MANTWO", SetupLifecycleState.TRIGGERED)
        for record in (first, second):
            repository.upsert_record(record)
            _insert_current_progress(repository, record)
        connection.commit()
        plan = audit_invalid_lifecycle_geometry(connection)
        manifest = build_geometry_hygiene_manifest(plan).as_dict()
        manifest["items"][1]["current_plan_identity"] = "plan-" + ("0" * 64)

        with pytest.raises(LifecycleHygieneError, match="mismatch"):
            apply_invalid_lifecycle_geometry_quarantine(
                connection,
                plan,
                manifest=manifest,
                now=NOW,
            )

        assert dict(
            connection.execute("SELECT lifecycle_id, current_state FROM setup_lifecycle_records")
        ) == {
            first.lifecycle_id: SetupLifecycleState.STALKING.value,
            second.lifecycle_id: SetupLifecycleState.TRIGGERED.value,
        }
        assert connection.execute("SELECT COUNT(*) FROM setup_lifecycle_events").fetchone()[0] == 0
    finally:
        connection.close()


def test_hygiene_rejects_stale_plan_without_partial_changes(tmp_path: Path) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record("stale", "STALE", SetupLifecycleState.STALKING)
        repository.upsert_record(record)
        _insert_current_progress(repository, record)
        connection.commit()
        plan = audit_invalid_lifecycle_geometry(connection)
        manifest = build_geometry_hygiene_manifest(plan)
        connection.execute(
            "UPDATE setup_lifecycle_records SET current_state = 'TRIGGERED' WHERE lifecycle_id = ?",
            (record.lifecycle_id,),
        )
        connection.commit()

        with pytest.raises(LifecycleHygieneError, match="stale"):
            apply_invalid_lifecycle_geometry_quarantine(
                connection,
                plan,
                manifest=manifest,
                now=NOW,
            )

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
        for record in (first, second):
            repository.upsert_record(record)
            _insert_current_progress(repository, record)
        connection.commit()
        plan = audit_invalid_lifecycle_geometry(connection)
        manifest = build_geometry_hygiene_manifest(plan)
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
            apply_invalid_lifecycle_geometry_quarantine(
                connection,
                plan,
                manifest=manifest,
                now=NOW,
            )

        assert dict(
            connection.execute("SELECT lifecycle_id, current_state FROM setup_lifecycle_records")
        ) == {
            first.lifecycle_id: SetupLifecycleState.STALKING.value,
            second.lifecycle_id: SetupLifecycleState.TRIGGERED.value,
        }
        assert connection.execute("SELECT COUNT(*) FROM setup_lifecycle_events").fetchone()[0] == 0
    finally:
        connection.close()


def test_active_invalid_long_order_is_classified_without_inventing_geometry(
    tmp_path: Path,
) -> None:
    connection = _fixture_connection(tmp_path)
    try:
        repository = _repository(connection)
        record = _record(
            "bad-long-order",
            "BADLONG",
            SetupLifecycleState.TRIGGERED,
            direction="long",
            invalid_long_order=True,
        )
        repository.upsert_record(record)
        _insert_current_progress(repository, record)
        connection.commit()

        item = audit_invalid_lifecycle_geometry(connection).safe_to_quarantine[0]

        assert item.geometry_failure == "invalid_long_level_order"
        assert item.proposed_state == SetupLifecycleState.INVALIDATED.value
        assert connection.execute(
            "SELECT direction, stop_loss FROM setup_lifecycle_records WHERE lifecycle_id = ?",
            (record.lifecycle_id,),
        ).fetchone()[:2] == ("long", "103")
    finally:
        connection.close()


def test_current_unsupported_na_geometry_remains_fail_closed_in_normal_lifecycle() -> None:
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
