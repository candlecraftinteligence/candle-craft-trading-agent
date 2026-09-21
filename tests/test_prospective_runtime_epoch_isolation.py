"""PROSPECTIVE_RUNTIME_EPOCH_ISOLATION: synthetic T01-T22 proof.

DEV-only temp databases, mocked senders, and deterministic clocks.
No Runtime filesystem, live Telegram, exchange calls, or scanner watch loops.
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import (
    TelegramLifecycleDeliveryService,
    telegram_alert_decision_for_symbol,
)
from app.alerts.telegram_outbox import (
    IN_FLIGHT,
    PENDING,
    RETRYABLE,
    UNCERTAIN,
    SQLitePublicTelegramOutbox,
)
from app.analytics.setup_quality import SetupQualityGrade
from app.analytics.symbol_health import SymbolHealthRecord, build_symbol_priority_plan
from app.core.config import Settings
from app.lifecycle.hygiene import LifecycleHygieneError, _apply_item, audit_invalid_lifecycle_geometry
from app.lifecycle.identity import new_setup_generation_id
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import SetupLifecycleService, apply_lifecycle_to_run_result
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from app.runtime_epoch.authority import initialize_runtime_epoch, load_active_runtime_epoch
from app.runtime_epoch.cohort import (
    COHORT_CURRENT_EPOCH_OPERATIONAL,
    COHORT_LEGACY_OR_UNATTRIBUTED,
    classify_lifecycle_cohort,
    classify_public_cohort,
)
from app.runtime_epoch.errors import (
    RuntimeEpochConfigurationError,
    RuntimeEpochError,
    RuntimeEpochIdentityCollisionError,
    RuntimeEpochOwnershipError,
)
from app.runtime_epoch.models import RUNTIME_EPOCH_CONTRACT_VERSION, RuntimeEpochIdentity
from app.runtime_epoch.ownership import (
    legacy_cooldown_veto,
    require_current_epoch_lifecycle_id,
)
from app.runtime_epoch.startup import require_operational_runtime
from app.storage.database import (
    SCHEMA_VERSION,
    identify_schema_version,
    initialize_database,
    open_initialized_database,
)
from app.watch_mode import WatchModeError, WatchState, save_watch_state, seed_watch_state_from_run_payload
from app.watch_supervisor import WatchFailureDisposition, classify_watch_exception
from app.runtime_epoch.watch_state import canonical_legacy_watch_state_path
import app.storage.database as database_module

from test_lifecycle_outcomes import _baseline, _candle, _record as _outcome_record, _target
from test_telegram_lifecycle_delivery_phase42 import (
    FakeSender,
    _direct_a_grade_limit_hit_symbol,
    _public_v1_symbol,
    _run_result,
    _run_result_many,
    _symbol,
    run,
)
from test_triggered_confirmed_telegram_delivery import _generated_entry_batch, _service
from tests.runtime_epoch_support import (
    SYNTHETIC_CUTOFF_AT,
    SYNTHETIC_EPOCH_ID,
    SYNTHETIC_IDENTITY,
    SYNTHETIC_NOW,
    assert_legacy_sent_consumption_frozen,
    bootstrap_operational_test_database,
    grant_synthetic_origin,
    seed_legacy_attempt,
    seed_legacy_lifecycle,
    seed_legacy_public_event,
    snapshot_tables,
)

NOW = "2026-09-20T12:00:00Z"


def _settings() -> Settings:
    return Settings(
        telegram_dry_run=True,
        telegram_signals_enabled=False,
        local_manual_mode=True,
        order_execution_enabled=False,
        runtime_epoch_id=SYNTHETIC_EPOCH_ID,
    )


def _row(connection: sqlite3.Connection, lifecycle_id: str) -> sqlite3.Row:
    row = connection.execute(
        "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
        (lifecycle_id,),
    ).fetchone()
    assert row is not None
    return row


def test_t01_legacy_actionable_cannot_create_initial_public_signal(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t01.db")
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-actionable",
            symbol="BTCUSDT",
            current_state=SetupLifecycleState.ACTIONABLE_A_GRADE.value,
        )
        before = snapshot_tables(connection)
        connection.commit()
    sender = FakeSender()
    symbol = _public_v1_symbol(signal_id="legacy-actionable")
    service = TelegramLifecycleDeliveryService(
        database_path=db_path, settings=_settings(), sender=sender
    )
    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="t01"))
    assert summary.sent == 0
    assert sender.messages == []
    assert sender.calls == []
    with open_initialized_database(db_path) as connection:
        after = snapshot_tables(connection)
        row = _row(connection, "legacy-actionable")
    assert dict(row)["runtime_epoch_id"] is None
    assert after["setup_lifecycle_records"] == before["setup_lifecycle_records"]
    assert after["public_alert_events"] == before["public_alert_events"]


def test_t02_legacy_confirmed_cannot_resume_limit_or_fill(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t02.db")
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-confirmed",
            symbol="ETHUSDT",
            current_state=SetupLifecycleState.CONFIRMED.value,
        )
        before = dict(_row(connection, "legacy-confirmed"))
        connection.commit()
    sender = FakeSender()
    symbol = _direct_a_grade_limit_hit_symbol(signal_id="legacy-confirmed")
    service = TelegramLifecycleDeliveryService(
        database_path=db_path, settings=_settings(), sender=sender
    )
    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="t02"))
    assert summary.sent == 0
    assert sender.calls == []
    with open_initialized_database(db_path) as connection:
        after = dict(_row(connection, "legacy-confirmed"))
        progress = connection.execute(
            "SELECT COUNT(*) FROM setup_lifecycle_outcome_progress WHERE lifecycle_id = ?",
            ("legacy-confirmed",),
        ).fetchone()[0]
    assert after == before
    assert progress == 0


def test_t03_legacy_managing_cannot_write_tp_sl_progress(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t03.db")
    record = _outcome_record(state=SetupLifecycleState.MANAGING)
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id=record.lifecycle_id,
            symbol=record.symbol,
            mode=record.mode,
            direction=record.direction,
            current_state=SetupLifecycleState.MANAGING.value,
            tp1="110",
            tp2="120",
            tp3="130",
        )
        before = snapshot_tables(connection)
        connection.commit()
    candles = (_baseline("long"), _target(1, "long", 1), _target(2, "long", 2), _target(3, "long", 3))
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        stored = repository.get_record_by_lifecycle_id(record.lifecycle_id)
        assert stored is not None
        result = evaluate_closed_candle_outcomes(
            stored,
            execution_candles=candles,
            execution_timeframe="5m",
            decision_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            evaluated_at=NOW,
            repository=repository,
            scan_run_id="t03",
        )
    assert result.progress is None
    with open_initialized_database(db_path) as connection:
        after = snapshot_tables(connection)
        assert after["setup_lifecycle_records"] == before["setup_lifecycle_records"]
        assert after["setup_lifecycle_outcome_progress"] == before["setup_lifecycle_outcome_progress"]
        assert after["public_alert_events"] == before["public_alert_events"]


def test_t04_legacy_triggered_cannot_progress(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t04.db")
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-triggered",
            symbol="SOLUSDT",
            current_state=SetupLifecycleState.TRIGGERED.value,
        )
        before = dict(_row(connection, "legacy-triggered"))
        connection.commit()
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        stored = repository.get_record_by_lifecycle_id("legacy-triggered")
        result = evaluate_closed_candle_outcomes(
            stored,
            execution_candles=(_baseline("long"), _candle(1, high="103", low="99")),
            execution_timeframe="5m",
            decision_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            evaluated_at=NOW,
            repository=repository,
        )
    assert result.record.current_state == SetupLifecycleState.TRIGGERED
    with open_initialized_database(db_path) as connection:
        assert dict(_row(connection, "legacy-triggered")) == before


def test_t05_terminal_legacy_is_not_backfilled(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t05.db")
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-tp",
            symbol="ADAUSDT",
            current_state=SetupLifecycleState.TP_HIT.value,
        )
        before = snapshot_tables(connection)
        connection.commit()
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        stored = repository.get_record_by_lifecycle_id("legacy-tp")
        evaluate_closed_candle_outcomes(
            stored,
            execution_candles=(_baseline("long"), _target(1, "long", 3)),
            execution_timeframe="5m",
            decision_timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            evaluated_at=NOW,
            repository=repository,
        )
    with open_initialized_database(db_path) as connection:
        assert snapshot_tables(connection) == before
        progress = connection.execute(
            "SELECT COUNT(*) FROM setup_lifecycle_outcome_progress WHERE lifecycle_id = 'legacy-tp'"
        ).fetchone()[0]
    assert progress == 0


def test_t06_legacy_sent_event_key_remains_consumed(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t06.db")
    event_key = "legacy-plan|initial_watchlist"
    with open_initialized_database(db_path) as connection:
        event_id = seed_legacy_public_event(connection, event_key=event_key, delivery_state="SENT", status="SENT")
        seed_legacy_attempt(connection, signal_id="legacy-sent", event_key=event_key)
        before = snapshot_tables(connection)
        original_attempt_ids = tuple(
            int(row[0]) for row in connection.execute("SELECT id FROM telegram_alert_attempts").fetchall()
        )
        connection.commit()
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path, settings=_settings(), sender=sender
    )
    symbol = _public_v1_symbol(signal_id="fresh-same-key")
    run(service.deliver_for_run(_run_result(symbol), scan_run_id="t06"))
    with open_initialized_database(db_path) as connection:
        row = connection.execute(
            "SELECT status, delivery_state, payload_text FROM public_alert_events WHERE id = ?",
            (event_id,),
        ).fetchone()
        count = connection.execute(
            "SELECT COUNT(*) FROM public_alert_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()[0]
        after = snapshot_tables(connection)
        if original_attempt_ids:
            placeholders = ",".join("?" for _ in original_attempt_ids)
            extra_attempts = connection.execute(
                f"""
                SELECT telegram_status, delivery_state
                FROM telegram_alert_attempts
                WHERE id NOT IN ({placeholders})
                """,
                original_attempt_ids,
            ).fetchall()
        else:
            extra_attempts = connection.execute(
                "SELECT telegram_status, delivery_state FROM telegram_alert_attempts"
            ).fetchall()
    assert tuple(row) == ("SENT", "SENT", "legacy payload")
    assert count == 1
    assert sender.calls == []
    assert_legacy_sent_consumption_frozen(
        before=before,
        after=after,
        extra_attempts=tuple(tuple(item) for item in extra_attempts),
    )


@pytest.mark.parametrize("delivery_state", [PENDING, RETRYABLE, IN_FLIGHT, UNCERTAIN])
def test_t07_legacy_pending_states_are_not_claimed_or_rewritten(
    tmp_path: Path, delivery_state: str
) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / f"t07-{delivery_state}.db")
    event_key = f"legacy-pending|{delivery_state}"
    with open_initialized_database(db_path) as connection:
        event_id = seed_legacy_public_event(
            connection,
            event_key=event_key,
            status="RESERVED",
            delivery_state=delivery_state,
        )
        attempt_id = seed_legacy_attempt(
            connection,
            signal_id=f"legacy-{delivery_state}",
            event_key=event_key,
            telegram_status="pending",
            delivery_state=delivery_state,
        )
        before = dict(
            connection.execute("SELECT * FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        )
        connection.commit()
        outbox = SQLitePublicTelegramOutbox(connection)
        recovered = outbox.recover_stale_in_flight(event_id=event_id, now=NOW)
        claimed = outbox.claim(event_id=event_id, reservation_id=attempt_id, now=NOW)
        after = dict(
            connection.execute("SELECT * FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        )
    assert recovered is False
    assert claimed.claim is None
    assert after == before


def test_t08_fresh_setup_can_coexist_with_legacy_current_row(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t08.db")
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-current",
            symbol="BTCUSDT",
            mode="swing",
            direction="long",
            current_state=SetupLifecycleState.ACTIONABLE_A_GRADE.value,
        )
        origin = grant_synthetic_origin(connection, symbol="BTCUSDT", run_id="t08-run", now=NOW)
        connection.commit()
    fresh = SetupLifecycleRecord(
        lifecycle_id="fresh-current",
        symbol="BTCUSDT",
        mode="swing",
        direction="long",
        current_state=SetupLifecycleState.WATCHLISTED,
        first_seen_at=NOW,
        last_seen_at=NOW,
        last_transition_at=NOW,
        runtime_epoch_id=SYNTHETIC_EPOCH_ID,
        creation_origin_id=origin.origin_id,
        is_current=True,
    )
    duplicate = fresh.model_copy(update={"lifecycle_id": "fresh-duplicate"})
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(fresh)
        with pytest.raises(sqlite3.IntegrityError):
            repository.upsert_record(duplicate)
        legacy = repository.get_record_by_lifecycle_id("legacy-current")
        current = repository.get_record(symbol="BTCUSDT", mode="swing", direction="long")
    assert legacy is not None
    assert legacy.is_current is True
    assert legacy.runtime_epoch_id is None
    assert current is not None
    assert current.lifecycle_id == "fresh-current"


def test_t09_legacy_identity_collision_is_rejected(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t09.db")
    anchor = "execution_sweep|15m|1789987200000"
    lifecycle_id = new_setup_generation_id(
        symbol="BTCUSDT", mode="swing", direction="long", structural_anchor=anchor
    )
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id=lifecycle_id,
            symbol="BTCUSDT",
            mode="swing",
            direction="long",
            current_state=SetupLifecycleState.ACTIONABLE_A_GRADE.value,
            structural_anchor=anchor,
        )
        origin = grant_synthetic_origin(connection, symbol="BTCUSDT", run_id="t09-run", now=NOW)
        before = dict(_row(connection, lifecycle_id))
        connection.commit()
    colliding = SetupLifecycleRecord(
        lifecycle_id=lifecycle_id,
        symbol="BTCUSDT",
        mode="swing",
        direction="long",
        current_state=SetupLifecycleState.WATCHLISTED,
        first_seen_at=NOW,
        last_seen_at=NOW,
        last_transition_at=NOW,
        structural_anchor=anchor,
        runtime_epoch_id=SYNTHETIC_EPOCH_ID,
        creation_origin_id=origin.origin_id,
        is_current=True,
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        with pytest.raises(RuntimeEpochIdentityCollisionError):
            repository.upsert_record(colliding)
    with open_initialized_database(db_path) as connection:
        after = dict(_row(connection, lifecycle_id))
        assert after["runtime_epoch_id"] is None
        assert after == before


def test_t10_fresh_valid_setup_reaches_fake_sender(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t10.db")
    sender = FakeSender()
    batch = _generated_entry_batch(db_path, lifecycle_id="fresh-progress")
    assert batch.lifecycle_state is not None
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        stored = repository.get_record_by_lifecycle_id("fresh-progress")
    assert stored is not None
    assert stored.runtime_epoch_id == SYNTHETIC_EPOCH_ID
    assert stored.current_state == SetupLifecycleState.MANAGING
    summary = run(_service(db_path, sender).deliver_for_run(_run_result(batch), scan_run_id="t10"))
    assert summary.sent == 1
    assert sender.messages
    with open_initialized_database(db_path) as connection:
        event = connection.execute(
            "SELECT runtime_epoch_id, origin_lifecycle_id FROM public_alert_events WHERE runtime_epoch_id IS NOT NULL"
        ).fetchone()
    assert event is not None
    assert event["runtime_epoch_id"] == SYNTHETIC_EPOCH_ID
    assert event["origin_lifecycle_id"] == "fresh-progress"


def test_t11_quality_gates_are_unchanged() -> None:
    decision = telegram_alert_decision_for_symbol(
        _direct_a_grade_limit_hit_symbol(
            signal_id="sig-b-limit",
            grade=SetupQualityGrade.B_PLUS,
        )
    )
    assert decision.eligible is False
    assert decision.reason == "lifecycle_state_not_eligible"


def test_t12_legacy_cooldown_still_vetoes_then_expires(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t12.db")
    active_until = "2026-09-21T00:00:00Z"
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-cool",
            symbol="BTCUSDT",
            mode="swing",
            direction="long",
            current_state=SetupLifecycleState.COOLDOWN.value,
            cooldown_until=active_until,
        )
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-other",
            symbol="ETHUSDT",
            mode="swing",
            direction="long",
            current_state=SetupLifecycleState.COOLDOWN.value,
            cooldown_until=active_until,
        )
        connection.execute(
            """
            INSERT INTO setup_lifecycle_records (
                lifecycle_id, symbol, mode, direction, current_state, previous_state,
                first_seen_at, last_seen_at, last_transition_at, is_current, cooldown_until
            ) VALUES ('legacy-bad-timer', 'SOLUSDT', 'swing', 'long', 'COOLDOWN', 'N/A',
                      ?, ?, ?, 1, 'not-a-timestamp')
            """,
            (NOW, NOW, NOW),
        )
        connection.commit()
        assert (
            legacy_cooldown_veto(
                connection, symbol="BTCUSDT", mode="swing", direction="long", now=NOW
            )
            == "legacy_cooldown_active"
        )
        assert (
            legacy_cooldown_veto(
                connection, symbol="BTCUSDT", mode="swing", direction="long", now=active_until
            )
            is None
        )
        assert (
            legacy_cooldown_veto(
                connection, symbol="SOLUSDT", mode="swing", direction="long", now=NOW
            )
            == "legacy_cooldown_unparseable"
        )
        assert (
            legacy_cooldown_veto(
                connection, symbol="ETHUSDT", mode="swing", direction="long", now=NOW
            )
            == "legacy_cooldown_active"
        )
        cool = _row(connection, "legacy-cool")
        other = _row(connection, "legacy-other")
        bad = _row(connection, "legacy-bad-timer")
    assert cool["cooldown_until"] == active_until
    assert other["cooldown_until"] == active_until
    assert bad["cooldown_until"] == "not-a-timestamp"


def test_t13_legacy_active_state_does_not_grant_health_exemption(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t13.db")
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-health",
            symbol="BTCUSDT",
            current_state=SetupLifecycleState.MANAGING.value,
        )
        connection.commit()
    cooldown_until = "2099-01-01T00:00:00Z"
    health = {
        "BTCUSDT": SymbolHealthRecord(
            symbol="BTCUSDT",
            current_health_score=20,
            cooldown_until=cooldown_until,
        )
    }
    from scripts.run_scan import _lifecycle_states_for_symbols

    args = type("Args", (), {"database_path": db_path})()
    states = _lifecycle_states_for_symbols(args, ("BTCUSDT",))
    assert "BTCUSDT" not in states
    plan = build_symbol_priority_plan(("BTCUSDT",), health, lifecycle_states=states, now=NOW)
    assert "BTCUSDT" in plan.skipped_symbols
    decision = plan.priority_by_symbol()["BTCUSDT"]
    assert decision.skipped_due_to_cooldown is True
    assert decision.cooldown_exempted is False


def test_t14_resumed_and_mixed_run_admit_only_fresh_symbols(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t14.db")
    resumed = _symbol(SetupLifecycleState.WATCHLISTED, signal_id="old-resume").model_copy(
        update={
            "symbol": "OLDUSDT",
            "evaluation_origin_kind": "resumed_payload",
            "lifecycle_decision_timestamp": datetime.fromisoformat(NOW.replace("Z", "+00:00")),
        }
    )
    fresh = resumed.model_copy(update={"symbol": "NEWUSDT", "evaluation_origin_kind": "live_scan"})
    result = apply_lifecycle_to_run_result(
        _run_result_many(resumed, fresh),
        database_path=db_path,
        scan_run_id="t14-run",
        now=NOW,
    )
    with open_initialized_database(db_path) as connection:
        old_origin = connection.execute(
            "SELECT status FROM runtime_operational_origins WHERE symbol = 'OLDUSDT'"
        ).fetchone()
        new_origin = connection.execute(
            "SELECT status FROM runtime_operational_origins WHERE symbol = 'NEWUSDT'"
        ).fetchone()
        old_life = connection.execute(
            "SELECT COUNT(*) FROM setup_lifecycle_records WHERE symbol = 'OLDUSDT'"
        ).fetchone()[0]
    assert old_origin["status"] == "blocked"
    assert new_origin["status"] == "granted"
    assert old_life == 0


def test_t15_timing_contract_fail_closed(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t15.db")
    equal_cutoff = datetime.fromisoformat(SYNTHETIC_CUTOFF_AT.replace("Z", "+00:00"))
    stale = _symbol(SetupLifecycleState.WATCHLISTED, signal_id="stale-cutoff").model_copy(
        update={
            "evaluation_origin_kind": "live_scan",
            "lifecycle_decision_timestamp": equal_cutoff,
            "evaluation_completed_at": datetime.fromisoformat(NOW.replace("Z", "+00:00")),
        }
    )
    result = apply_lifecycle_to_run_result(
        _run_result(stale),
        database_path=db_path,
        scan_run_id="t15-run",
        now=NOW,
    )
    del result
    with open_initialized_database(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0]
        origin = connection.execute(
            "SELECT status, block_reason FROM runtime_operational_origins WHERE symbol = 'BTCUSDT'"
        ).fetchone()
    assert count == 0
    assert origin is not None
    assert origin["status"] == "blocked"
    assert "decision_cutoff_not_after_epoch_cutoff" in str(origin["block_reason"])


def test_t16_reopen_preserves_epoch_and_excludes_legacy(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t16.db")
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-reopen",
            symbol="BTCUSDT",
            current_state=SetupLifecycleState.ACTIONABLE_A_GRADE.value,
        )
        connection.commit()
    with open_initialized_database(db_path) as connection:
        epoch = load_active_runtime_epoch(connection)
        assert epoch is not None
        assert epoch.epoch_id == SYNTHETIC_EPOCH_ID
        assert epoch.cutoff_at == SYNTHETIC_CUTOFF_AT
        records = SQLiteSetupLifecycleRepository(db_path)
    with records as repository:
        operational = repository.get_records_for_states((SetupLifecycleState.ACTIONABLE_A_GRADE,))
        legacy = repository.get_record_by_lifecycle_id("legacy-reopen")
    assert operational == ()
    assert legacy is not None
    assert legacy.runtime_epoch_id is None


@pytest.mark.no_auto_epoch
def test_t17_missing_epoch_blocks_before_writes(tmp_path: Path) -> None:
    db_path = tmp_path / "t17.db"
    with open_initialized_database(db_path) as connection:
        assert load_active_runtime_epoch(connection) is None
        assert identify_schema_version(connection) == SCHEMA_VERSION
    with pytest.raises(RuntimeEpochConfigurationError):
        require_operational_runtime(database_path=db_path, expected_identity=SYNTHETIC_IDENTITY)
    missing = tmp_path / "missing.db"
    with pytest.raises(RuntimeEpochConfigurationError):
        require_operational_runtime(database_path=missing, expected_identity=SYNTHETIC_IDENTITY)
    with pytest.raises(RuntimeEpochConfigurationError):
        with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY):
            pass


def test_t18_crash_and_concurrent_init_leave_no_unowned_live_state(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t18.db")
    with open_initialized_database(db_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        from app.runtime_epoch.origin import register_operational_run

        register_operational_run(connection, run_id="crash-run", registered_at=NOW)
        connection.execute("ROLLBACK")
        origins = connection.execute("SELECT COUNT(*) FROM runtime_operational_origins").fetchone()[0]
        lives = connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0]
        events = connection.execute("SELECT COUNT(*) FROM public_alert_events").fetchone()[0]
    assert origins == 0
    assert lives == 0
    assert events == 0

    errors: list[BaseException] = []

    def _init(identity: RuntimeEpochIdentity) -> None:
        try:
            with open_initialized_database(db_path) as connection:
                initialize_runtime_epoch(connection, identity, activated_at=SYNTHETIC_CUTOFF_AT)
        except BaseException as exc:  # noqa: BLE001 - collect worker failures
            errors.append(exc)

    other = RuntimeEpochIdentity(
        epoch_id="other-epoch",
        cutoff_at="2001-01-01T00:00:00Z",
        contract_version=RUNTIME_EPOCH_CONTRACT_VERSION,
        reviewed_release_sha="abc",
        generation_binding="other",
    )
    workers = [
        threading.Thread(target=_init, args=(SYNTHETIC_IDENTITY,)),
        threading.Thread(target=_init, args=(other,)),
    ]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    with open_initialized_database(db_path) as connection:
        epoch = load_active_runtime_epoch(connection)
    assert epoch is not None
    assert epoch.epoch_id == SYNTHETIC_EPOCH_ID
    assert any(isinstance(item, RuntimeEpochConfigurationError) for item in errors) or errors == []


def test_t19_direct_and_recoverable_paths_cannot_bypass(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t19.db")
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-direct",
            symbol="BTCUSDT",
            current_state=SetupLifecycleState.MANAGING.value,
        )
        connection.commit()
        with pytest.raises(RuntimeEpochOwnershipError):
            require_current_epoch_lifecycle_id(connection, "legacy-direct")
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        with pytest.raises(RuntimeEpochError):
            repository.upsert_record(
                repository.get_record_by_lifecycle_id("legacy-direct")
            )
    wrapped = SystemExit("epoch missing")
    wrapped.__cause__ = RuntimeEpochConfigurationError("missing")
    assert classify_watch_exception(wrapped) == WatchFailureDisposition.FATAL
    assert classify_watch_exception(RuntimeEpochOwnershipError("legacy")) == WatchFailureDisposition.FATAL


@pytest.mark.no_auto_epoch
def test_t20_v25_upgrade_preserves_rows_and_rolls_back_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.fixtures.genuine_v25 import create_genuine_v25_database

    db_path = create_genuine_v25_database(tmp_path / "t20.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="v25-row",
            symbol="BTCUSDT",
            current_state=SetupLifecycleState.ACTIONABLE_A_GRADE.value,
        )
        seed_legacy_public_event(
            connection,
            event_key="v25-plan|initial_watchlist",
            status="SENT",
            delivery_state="SENT",
        )
        connection.commit()
        before_life = dict(_row(connection, "v25-row"))
        before_event = dict(
            connection.execute(
                "SELECT * FROM public_alert_events WHERE event_key = ?",
                ("v25-plan|initial_watchlist",),
            ).fetchone()
        )
        assert "runtime_epoch_id" not in before_life
        assert "creation_origin_id" not in before_life
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "ux_lifecycle_records_current_symbol_mode_direction" in names

    with open_initialized_database(db_path) as connection:
        assert identify_schema_version(connection) == 26
        after = dict(_row(connection, "v25-row"))
        assert after["runtime_epoch_id"] is None
        assert after["creation_origin_id"] is None
        assert after["lifecycle_id"] == before_life["lifecycle_id"]
        assert after["current_state"] == before_life["current_state"]
        after_event = dict(
            connection.execute(
                "SELECT * FROM public_alert_events WHERE event_key = ?",
                ("v25-plan|initial_watchlist",),
            ).fetchone()
        )
        assert after_event["runtime_epoch_id"] is None
        assert after_event.get("canonical_reservation_attempt_id") is None
        assert after_event["event_key"] == before_event["event_key"]
        assert after_event["status"] == before_event["status"]
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "ux_lifecycle_records_current_symbol_mode_direction" not in names
        assert "ux_lifecycle_records_legacy_current_symbol_mode_direction" in names
        assert "ux_lifecycle_records_epoch_current_symbol_mode_direction" in names
        assert load_active_runtime_epoch(connection) is None

    fail_path = create_genuine_v25_database(tmp_path / "t20-fail.db")
    with sqlite3.connect(fail_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="fail-row",
            symbol="ETHUSDT",
            current_state=SetupLifecycleState.WATCHLISTED.value,
        )
        connection.commit()

    def boom(connection: sqlite3.Connection) -> None:
        connection.execute("DROP INDEX IF EXISTS ux_lifecycle_records_current_symbol_mode_direction")
        raise RuntimeError("injected v26 migration failure")

    monkeypatch.setattr(database_module, "_ensure_lifecycle_epoch_current_indexes", boom)
    with pytest.raises(Exception):
        open_initialized_database(fail_path)
    with sqlite3.connect(fail_path) as connection:
        connection.row_factory = sqlite3.Row
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        assert version == 25
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'")
        }
        assert "ux_lifecycle_records_current_symbol_mode_direction" in names
        epoch_tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name LIKE 'runtime_epoch%'"
            )
        }
        assert epoch_tables == set()
        assert load_active_runtime_epoch(connection) is None
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(setup_lifecycle_records)")
        }
        assert "runtime_epoch_id" not in columns



def test_t21_cohort_labels_are_honest(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t21.db")
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-cohort",
            symbol="BTCUSDT",
            current_state=SetupLifecycleState.ACTIONABLE_A_GRADE.value,
            last_seen_at="2099-01-01T00:00:00Z",
        )
        origin = grant_synthetic_origin(connection, symbol="ETHUSDT", run_id="t21-run", now=NOW)
        connection.commit()
    fresh = SetupLifecycleRecord(
        lifecycle_id="fresh-cohort",
        symbol="ETHUSDT",
        mode="swing",
        direction="long",
        current_state=SetupLifecycleState.WATCHLISTED,
        first_seen_at=NOW,
        last_seen_at=NOW,
        last_transition_at=NOW,
        runtime_epoch_id=SYNTHETIC_EPOCH_ID,
        creation_origin_id=origin.origin_id,
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(fresh)
        operational = repository.get_records_for_states((SetupLifecycleState.WATCHLISTED, SetupLifecycleState.ACTIONABLE_A_GRADE))
    assert [item.lifecycle_id for item in operational] == ["fresh-cohort"]
    with open_initialized_database(db_path) as connection:
        legacy = _row(connection, "legacy-cohort")
        current = _row(connection, "fresh-cohort")
        assert classify_lifecycle_cohort(connection, legacy) == COHORT_LEGACY_OR_UNATTRIBUTED
        assert classify_lifecycle_cohort(connection, current) == COHORT_CURRENT_EPOCH_OPERATIONAL
        assert "ADMITTED" not in classify_lifecycle_cohort(connection, current)
        assert "EXPECTANCY" not in classify_lifecycle_cohort(connection, current)
        forensic = connection.execute(
            "SELECT lifecycle_id FROM setup_lifecycle_records WHERE runtime_epoch_id IS NULL"
        ).fetchall()
    assert [row[0] for row in forensic] == ["legacy-cohort"]


def test_t22_legacy_evidence_is_frozen(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "t22.db")
    canonical = Path("scan_runs") / "watch_state.json"
    before = canonical.read_bytes() if canonical.exists() else None
    with pytest.raises(WatchModeError):
        save_watch_state(canonical, WatchState(runtime_epoch_id=SYNTHETIC_EPOCH_ID))
    after = canonical.read_bytes() if canonical.exists() else None
    assert after == before
    with pytest.raises(WatchModeError):
        seed_watch_state_from_run_payload(
            WatchState(runtime_epoch_id=SYNTHETIC_EPOCH_ID),
            {"results": [{"symbol": "BTCUSDT", "display_status": "near_miss"}]},
            ("BTCUSDT",),
        )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        with pytest.raises(RuntimeEpochOwnershipError):
            repository.reset()
    with open_initialized_database(db_path) as connection:
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="legacy-hygiene",
            symbol="BTCUSDT",
            current_state=SetupLifecycleState.WATCHLISTED.value,
            entry_low="N/A",
            entry_high="N/A",
            stop_loss="N/A",
        )
        before = dict(_row(connection, "legacy-hygiene"))
        plan = audit_invalid_lifecycle_geometry(connection)
        if plan.items:
            with pytest.raises(LifecycleHygieneError):
                _apply_item(connection, plan.items[0], plan.items[0], NOW)
        after = dict(_row(connection, "legacy-hygiene"))
        assert after == before
