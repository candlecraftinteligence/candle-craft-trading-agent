"""Fourth bounded repair proofs R91–R122 for PROSPECTIVE_RUNTIME_EPOCH_ISOLATION.

DEV-only temp databases, mocked senders, and deterministic clocks.
No Runtime filesystem, live Telegram, exchange calls, or scanner watch loops.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import (
    PUBLIC_SIGNAL_MIN_RR,
    PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
    SQLiteTelegramAlertAttemptRepository,
    TelegramLifecycleDeliveryService,
    _insert_public_alert_event,
)
from app.alerts.telegram_outbox import PENDING, SENT, SQLitePublicTelegramOutbox, persist_intent_parts
from app.core.config import Settings
from app.core.minimum_rr import DEFAULT_CONFIGURED_MINIMUM_RR
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.pipeline.scanner_runner import ScannerRunner
from app.runtime_epoch.authority import initialize_runtime_epoch, load_active_runtime_epoch
from app.runtime_epoch.origin import register_operational_scan_run
from app.storage.database import (
    SCHEMA_VERSION,
    identify_schema_version,
    initialize_database,
    open_initialized_database,
)
from app.storage.database import _telegram_alert_attempts_has_unconditional_signal_alert_unique
import app.storage.database as database_module
from app.telegram_admin.signal_detail import load_active_signal_detail
from scripts import run_scan
from test_telegram_lifecycle_delivery_phase42 import FakeSender
from tests.fixtures.genuine_v25 import create_genuine_v25_database
from tests.runtime_epoch_support import (
    SYNTHETIC_CUTOFF_AT,
    SYNTHETIC_EPOCH_ID,
    SYNTHETIC_GENERATION_BINDING,
    SYNTHETIC_IDENTITY,
    SYNTHETIC_RELEASE_SHA,
    bootstrap_operational_test_database,
    grant_synthetic_origin,
    snapshot_tables,
)
from tests.test_prospective_runtime_epoch_isolation import NOW
from tests.test_prospective_runtime_epoch_isolation_boundaries import (
    _attempt_record,
    _bootstrap,
    _owned_record,
    _plan,
)
from tests.test_prospective_runtime_epoch_isolation_repair2 import (
    FakeAdapterExchangeClient,
    _owned_event_reservation,
    _public_min_rr_pullback_candles,
    _row,
)
from tests.test_scanner_runner import _strategy_pullback_candles

pytestmark = pytest.mark.no_auto_epoch

OWNED_RATIONALE = "OWNED_RATIONALE_MARKER"
OWNED_INVALIDATION = "OWNED_INVALIDATION_MARKER"
OWNED_FACT = "OWNED_FACT_MARKER"
OWNED_GATE = "OWNED_GATE_MARKER"
FOREIGN_RATIONALE = "FOREIGN_SHORT_RATIONALE_MARKER"
FOREIGN_INVALIDATION = "FOREIGN_SETUP_INVALIDATION_MARKER"
FOREIGN_FACT = "FOREIGN_LEGACY_FACT_MARKER"
FOREIGN_GATE = "FOREIGN_CANDIDATE_GATE_MARKER"
FOREIGN_QUALITY = "FOREIGN_QUALITY_MARKER"


def _fresh_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _operational_fields(detail) -> tuple[object, ...]:
    return (
        detail.quality,
        detail.why_it_matters,
        detail.invalid_if,
        tuple(detail.confirmed_facts),
        tuple(detail.confirmed_gates),
        detail.rr,
        detail.entry_low,
        detail.entry_high,
        detail.stop_loss,
        detail.tp1,
        detail.tp2,
        detail.tp3,
        detail.bias,
    )


def _joined_detail_text(detail) -> str:
    parts = [
        str(detail.quality),
        str(detail.why_it_matters),
        str(detail.invalid_if),
        str(detail.lifecycle),
        *(str(item) for item in detail.confirmed_facts),
        *(str(item) for item in detail.confirmed_gates),
    ]
    return " ".join(parts)


def _insert_scan_run(connection: sqlite3.Connection, *, run_id: str, symbol: str, now: str) -> None:
    connection.execute(
        """
        INSERT OR IGNORE INTO scan_runs (
            run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
            strategy, timeframes_json, market_regime, runtime_stats_json,
            command_preset, command_used, total_valid_setups, near_misses,
            rejected, data_issues, data_issues_json, raw_payload_json
        ) VALUES (?, ?, 'binance', 'manual', 1, ?, 'liquidity_grab_pullback', '{}',
                  'mixed', '{}', 'repair4', 'test', 0, 0, 0, 0, '[]', '{}')
        """,
        (run_id, now, json.dumps([symbol])),
    )


def _insert_symbol_result(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    symbol: str,
    raw_result: dict[str, object],
    status: str = "valid_setup",
    quality: str = "A",
) -> None:
    connection.execute(
        """
        INSERT INTO symbol_results (
            run_id, symbol, status, display_bucket, readiness_score,
            setup_quality_score, edge_score, failed_gate, rejection_reason,
            next_trigger_needed, action_label, regime_state, derivatives_context_json,
            volume_profile_context_json, pullback_status, portfolio_decision,
            raw_result_json
        ) VALUES (?, ?, ?, ?, 80, ?, 'N/A', 'N/A', 'N/A', 'N/A', 'watch', 'mixed',
                  '{}', '{}', 'N/A', 'N/A', ?)
        """,
        (run_id, symbol, status, status, quality, json.dumps(raw_result)),
    )


def _insert_candidate(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    symbol: str,
    direction: str,
    raw_candidate: dict[str, object],
    quality: str = "A",
) -> None:
    connection.execute(
        """
        INSERT INTO setup_candidates (
            run_id, symbol, mode, direction, entry, stop, tp1, tp2, tp3, rr,
            invalidation, quality_grade, trust_meter, risk_warning, raw_candidate_json
        ) VALUES (?, ?, 'swing', ?, '100-102', '95', '110', '120', '130', '3.5',
                  ?, ?, 'N/A', 'N/A', ?)
        """,
        (
            run_id,
            symbol,
            direction,
            str(raw_candidate.get("invalidation", "N/A")),
            quality,
            json.dumps(raw_candidate),
        ),
    )


def _owned_raw_result(*, lifecycle_id: str, direction: str = "long") -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "direction": direction,
        "lifecycle_id": lifecycle_id,
        "reason_for_trade": OWNED_RATIONALE,
        "invalidation": OWNED_INVALIDATION,
        "setup_quality": {"quality_grade": "A", "decision_reason": OWNED_GATE},
        "trade_idea": {
            "direction": direction,
            "grade": "A",
            "reason_for_trade": OWNED_RATIONALE,
            "invalidation": OWNED_INVALIDATION,
            "confirmed_facts": [OWNED_FACT],
        },
        "confirmed_facts": [OWNED_FACT],
    }


def _seed_positive_active_detail(db_path: Path, *, label: str) -> tuple[int, str, str]:
    fresh = _fresh_now()
    lifecycle_id = f"life-{label}"
    event_key = f"{label}:owned"
    run_id = f"{label}-origin"
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id=run_id, now=fresh)
        lifecycle.upsert_record(
            _owned_record(origin.origin_id, lifecycle_id=lifecycle_id).model_copy(
                update={
                    "current_state": SetupLifecycleState.CONFIRMED,
                    "first_seen_at": fresh,
                    "last_seen_at": fresh,
                    "last_transition_at": fresh,
                    "tp1": "110",
                    "tp2": "120",
                    "tp3": "130",
                    "rr": "3.5",
                    "quality_grade_current": "A",
                    "quality_score": 90,
                    "invalidation_reason": OWNED_INVALIDATION,
                }
            )
        )
        _insert_scan_run(lifecycle.connection, run_id=run_id, symbol="BTCUSDT", now=fresh)
        _insert_symbol_result(
            lifecycle.connection,
            run_id=run_id,
            symbol="BTCUSDT",
            raw_result=_owned_raw_result(lifecycle_id=lifecycle_id),
        )
        _insert_candidate(
            lifecycle.connection,
            run_id=run_id,
            symbol="BTCUSDT",
            direction="long",
            raw_candidate={
                "reason_for_trade": OWNED_RATIONALE,
                "invalidation": OWNED_INVALIDATION,
                "quality_grade": "A",
                "confirmed_facts": [OWNED_FACT],
                "lifecycle_id": lifecycle_id,
            },
        )
        lifecycle.connection.commit()
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        event, created = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id=f"{lifecycle_id}-plan"),
            event_key=event_key,
            event_type="signal_confirmed",
            status="RESERVED",
            reserved_at=fresh,
            origin_lifecycle_id=lifecycle_id,
        )
        assert created is True
        assert event is not None
        inserted = repository.insert_attempt(
            replace(
                _attempt_record(
                    signal_id=lifecycle_id,
                    event_key=event_key,
                    status="pending",
                    delivery_state=PENDING,
                    alert_type="SIGNAL_CONFIRMED",
                ),
                public_watchlist_plan_id=f"{lifecycle_id}-plan",
                public_alert_event_type="signal_confirmed",
                scan_run_id=run_id,
                last_scan_run_id=run_id,
                new_state="CONFIRMED",
                lifecycle_state="CONFIRMED",
                setup_quality_score="A",
                quality_grade="A",
                rr_planned="3.5",
                min_rr="3",
                entry_low="100",
                entry_high="102",
                stop_loss="95",
                tp1="110",
                tp2="120",
                tp3="130",
                attempted_alert_type="SIGNAL_CONFIRMED",
            )
        )
        assert inserted is True
        reservation_id = int(
            repository._connection.execute(
                "SELECT id FROM telegram_alert_attempts WHERE public_watchlist_event_key = ?",
                (event_key,),
            ).fetchone()[0]
        )
        persist_intent_parts(
            repository._connection,
            event_id=int(event.id),
            event_key=event_key,
            message_text="owned payload",
            message_hash=f"hash-{label}",
        )
        repository._connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET telegram_status = 'sent', delivery_state = ?, sent_at = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (SENT, fresh, fresh, reservation_id),
        )
        repository._connection.commit()
    return reservation_id, event_key, run_id


def _load_detail(tmp_path: Path, db_path: Path):
    result = load_active_signal_detail(project_root=tmp_path, database_path=db_path, selector="BTCUSDT")
    return result.detail


def _snapshot_attempt(connection: sqlite3.Connection, attempt_id: int) -> dict[str, object]:
    row = connection.execute("SELECT * FROM telegram_alert_attempts WHERE id = ?", (attempt_id,)).fetchone()
    assert row is not None
    return dict(row)


def _register_current_run(db_path: Path, run_id: str) -> None:
    register_operational_scan_run(
        db_path,
        run_id=run_id,
        registered_at=_fresh_now(),
        expected_identity=SYNTHETIC_IDENTITY,
    )


def _audit_record(*, signal_id: str, event_key: str, status: str, run_id: str, blocked_reason: str = "diagnostic"):
    return replace(
        _attempt_record(
            signal_id=signal_id,
            event_key=event_key,
            status=status,
            blocked_reason=blocked_reason,
        ),
        scan_run_id=run_id,
        last_scan_run_id=run_id,
        error_message=blocked_reason,
    )


def _insert_legacy_audit(
    connection: sqlite3.Connection,
    *,
    signal_id: str,
    status: str,
    event_key: str | None,
    blocked_reason: str = "diagnostic",
) -> int:
    cursor = connection.execute(
        """
        INSERT INTO telegram_alert_attempts (
            signal_id, symbol, direction, previous_state, new_state, alert_type,
            lifecycle_state, attempted_at, telegram_status, message_hash,
            attempted_alert_type, blocked_reason, error_message, first_seen_at,
            last_seen_at, seen_count, last_scan_run_id, last_error_message,
            public_watchlist_event_key, public_alert_event_type
        ) VALUES (
            ?, 'BTCUSDT', 'long', 'WATCHLISTED', 'WATCHLISTED', 'WATCHLIST',
            'WATCHLISTED', '2026-01-01T00:00:00Z', ?, 'legacy-hash', 'WATCHLIST',
            ?, ?, '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 4, NULL, ?,
            ?, 'initial_watchlist'
        )
        """,
        (
            signal_id,
            status,
            blocked_reason,
            blocked_reason,
            blocked_reason,
            event_key if event_key is not None else "N/A",
        ),
    )
    return int(cursor.lastrowid)


def test_r91_positive_active_detail_exists_from_owned_chain(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r91.db")
    reservation_id, _event_key, _run_id = _seed_positive_active_detail(db_path, label="r91")
    detail = _load_detail(tmp_path, db_path)
    assert detail is not None
    assert detail.symbol == "BTCUSDT"
    assert str(detail.bias).upper() == "LONG"
    assert detail.quality not in {None, "", "N/A"}
    assert "3.5" in str(detail.rr) or str(detail.rr) != "N/A"
    assert str(detail.entry_low) != "N/A"
    assert OWNED_RATIONALE in _joined_detail_text(detail)
    assert OWNED_INVALIDATION in _joined_detail_text(detail)
    with sqlite3.connect(db_path) as connection:
        canonical = connection.execute(
            "SELECT canonical_reservation_attempt_id FROM public_alert_events WHERE event_key = 'r91:owned'"
        ).fetchone()
    assert canonical is not None
    assert int(canonical[0]) == reservation_id


def test_r92_later_opposite_direction_same_symbol_result_cannot_change_active_detail(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r92.db")
    _seed_positive_active_detail(db_path, label="r92")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id="r92-foreign-short", symbol="BTCUSDT", now=_fresh_now())
        _insert_symbol_result(
            connection,
            run_id="r92-foreign-short",
            symbol="BTCUSDT",
            quality="Reject",
            raw_result={
                "direction": "short",
                "reason_for_trade": FOREIGN_RATIONALE,
                "invalidation": FOREIGN_INVALIDATION,
                "setup_quality": {"quality_grade": "Reject", "decision_reason": FOREIGN_GATE},
                "trade_idea": {
                    "direction": "short",
                    "grade": "Reject",
                    "reason_for_trade": FOREIGN_RATIONALE,
                    "invalidation": FOREIGN_INVALIDATION,
                    "confirmed_facts": [FOREIGN_FACT],
                },
            },
        )
        connection.commit()
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    assert _operational_fields(after) == before
    assert FOREIGN_RATIONALE not in _joined_detail_text(after)
    assert FOREIGN_QUALITY not in _joined_detail_text(after)


def test_r93_later_different_setup_same_symbol_result_cannot_change_active_detail(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r93.db")
    _seed_positive_active_detail(db_path, label="r93")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id="r93-foreign-setup", symbol="BTCUSDT", now=_fresh_now())
        _insert_symbol_result(
            connection,
            run_id="r93-foreign-setup",
            symbol="BTCUSDT",
            raw_result={
                "direction": "long",
                "lifecycle_id": "life-r93-other",
                "setup_id": "setup-foreign",
                "reason_for_trade": FOREIGN_RATIONALE,
                "invalidation": FOREIGN_INVALIDATION,
                "setup_quality": {"quality_grade": "B", "decision_reason": FOREIGN_GATE},
                "trade_idea": {"grade": "B", "reason_for_trade": FOREIGN_RATIONALE, "invalidation": FOREIGN_INVALIDATION},
            },
        )
        connection.commit()
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    assert _operational_fields(after) == before
    assert FOREIGN_INVALIDATION not in _joined_detail_text(after)
    assert FOREIGN_GATE not in _joined_detail_text(after)


def test_r94_legacy_same_symbol_result_cannot_enrich_active_detail(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r94.db")
    _seed_positive_active_detail(db_path, label="r94")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id="r94-legacy-result", symbol="BTCUSDT", now="2026-01-01T00:00:00Z")
        _insert_symbol_result(
            connection,
            run_id="r94-legacy-result",
            symbol="BTCUSDT",
            raw_result={
                "direction": "long",
                "reason_for_trade": FOREIGN_FACT,
                "invalidation": FOREIGN_INVALIDATION,
                "confirmed_facts": [FOREIGN_FACT],
                "setup_quality": {"quality_grade": "C", "decision_reason": FOREIGN_GATE},
            },
        )
        connection.commit()
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    assert _operational_fields(after) == before
    assert FOREIGN_FACT not in _joined_detail_text(after)


def test_r95_foreign_same_symbol_candidate_cannot_enrich_active_detail(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r95.db")
    _seed_positive_active_detail(db_path, label="r95")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    before = _operational_fields(baseline)
    with sqlite3.connect(db_path) as connection:
        _insert_scan_run(connection, run_id="r95-foreign-candidate", symbol="BTCUSDT", now=_fresh_now())
        _insert_candidate(
            connection,
            run_id="r95-foreign-candidate",
            symbol="BTCUSDT",
            direction="long",
            quality="Reject",
            raw_candidate={
                "reason_for_trade": FOREIGN_GATE,
                "invalidation": FOREIGN_INVALIDATION,
                "quality_grade": FOREIGN_QUALITY,
                "confirmed_facts": [FOREIGN_FACT],
                "lifecycle_id": "life-foreign",
            },
        )
        connection.commit()
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    assert _operational_fields(after) == before
    assert FOREIGN_QUALITY not in _joined_detail_text(after)
    assert FOREIGN_GATE not in _joined_detail_text(after)


def test_r96_active_detail_uses_exact_canonical_reservation_attempt(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r96.db")
    reservation_id, event_key, run_id = _seed_positive_active_detail(db_path, label="r96")
    baseline = _load_detail(tmp_path, db_path)
    assert baseline is not None
    _register_current_run(db_path, "r96-current")
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert repository.insert_attempt(
            _audit_record(
                signal_id="life-r96",
                event_key=event_key,
                status="blocked",
                run_id="r96-current",
                blocked_reason=FOREIGN_QUALITY,
            ),
            audit_only=True,
        )
        later_id = int(
            repository._connection.execute("SELECT MAX(id) FROM telegram_alert_attempts").fetchone()[0]
        )
        assert later_id != reservation_id
        repository._connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET setup_quality_score = ?, quality_grade = ?, rr_planned = '9.9'
            WHERE id = ?
            """,
            (FOREIGN_QUALITY, FOREIGN_QUALITY, later_id),
        )
        repository._connection.commit()
    after = _load_detail(tmp_path, db_path)
    assert after is not None
    assert _operational_fields(after) == _operational_fields(baseline)
    assert FOREIGN_QUALITY not in _joined_detail_text(after)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        event = connection.execute(
            "SELECT canonical_reservation_attempt_id FROM public_alert_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
    assert int(event["canonical_reservation_attempt_id"]) == reservation_id


def _assert_legacy_audit_frozen(tmp_path: Path, *, label: str, status: str, keyed: bool) -> None:
    db_path = _bootstrap(tmp_path / f"{label}.db")
    _register_current_run(db_path, f"{label}-run")
    event_key = f"{label}:legacy" if keyed else None
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        legacy_id = _insert_legacy_audit(
            connection,
            signal_id=f"{label}-legacy",
            status=status,
            event_key=event_key,
        )
        before = _snapshot_attempt(connection, legacy_id)
        connection.commit()
    incoming_key = event_key if event_key is not None else "N/A"
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        inserted = repository.insert_attempt(
            _audit_record(
                signal_id=f"{label}-legacy",
                event_key=incoming_key,
                status=status,
                run_id=f"{label}-run",
            )
        )
        after = _snapshot_attempt(repository._connection, legacy_id)
        extra = repository._connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts WHERE signal_id = ?",
            (f"{label}-legacy",),
        ).fetchone()[0]
    assert inserted is True
    assert after == before
    assert extra >= 2


def test_r97_keyed_legacy_blocked_audit_unchanged(tmp_path: Path) -> None:
    _assert_legacy_audit_frozen(tmp_path, label="r97", status="blocked", keyed=True)


def test_r98_keyed_legacy_skipped_audit_unchanged(tmp_path: Path) -> None:
    _assert_legacy_audit_frozen(tmp_path, label="r98", status="skipped", keyed=True)


def test_r99_unkeyed_legacy_blocked_audit_unchanged(tmp_path: Path) -> None:
    _assert_legacy_audit_frozen(tmp_path, label="r99", status="blocked", keyed=False)


def test_r100_unkeyed_legacy_skipped_audit_unchanged(tmp_path: Path) -> None:
    _assert_legacy_audit_frozen(tmp_path, label="r100", status="skipped", keyed=False)


def test_r101_current_epoch_audit_compaction_only_against_current_epoch(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r101.db")
    _register_current_run(db_path, "r101-run")
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert repository.insert_attempt(
            _audit_record(signal_id="r101-now", event_key="r101:now", status="blocked", run_id="r101-run")
        )
        first_id = int(
            repository._connection.execute(
                "SELECT id FROM telegram_alert_attempts WHERE signal_id = 'r101-now'"
            ).fetchone()[0]
        )
        compacted = repository.compact_repeated_attempt(
            _audit_record(signal_id="r101-now", event_key="r101:now", status="blocked", run_id="r101-run")
        )
        after = _snapshot_attempt(repository._connection, first_id)
        count = repository._connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts WHERE signal_id = 'r101-now'"
        ).fetchone()[0]
    assert compacted is True
    assert int(after["seen_count"]) >= 2
    assert after["last_scan_run_id"] == "r101-run"
    assert count == 1


def test_r102_canonical_reservation_is_never_compacted_as_audit(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r102.db")
    event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r102", event_key="r102:owned", signal_id="r102-owned", run_id="r102"
    )
    _register_current_run(db_path, "r102-run")
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        before = _snapshot_attempt(repository._connection, reservation_id)
        assert repository.insert_attempt(
            _audit_record(
                signal_id="r102-owned",
                event_key="r102:owned",
                status="blocked",
                run_id="r102-run",
            )
        )
        compacted = repository.compact_repeated_attempt(
            _audit_record(
                signal_id="r102-owned",
                event_key="r102:owned",
                status="blocked",
                run_id="r102-run",
            )
        )
        after = _snapshot_attempt(repository._connection, reservation_id)
        event_after = _row(repository._connection, "public_alert_events", event_id)
    assert after == before
    assert after["telegram_status"] == "pending"
    assert int(event_after["canonical_reservation_attempt_id"]) == reservation_id
    assert compacted is True


def _replacement_base(signal_id: str, event_key: str, lifecycle_id: str, **fields):
    return replace(
        _attempt_record(
            signal_id=signal_id,
            event_key=event_key,
            status="pending",
            delivery_state=PENDING,
        ),
        public_watchlist_plan_id=f"{lifecycle_id}-plan",
        public_alert_event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
        **fields,
    )


def test_r103_replacement_contradictory_actual_alert_type_rejected(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r103.db")
    _event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r103", event_key="r103:owned", signal_id="r103-owned", run_id="r103"
    )
    replacement = _replacement_base("r103-owned", "r103:owned", "life-r103", alert_type="TP1_HIT")
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        before = _snapshot_attempt(repository._connection, reservation_id)
        event_before = _row(repository._connection, "public_alert_events", _event_id)
        assert repository.replace_attempt_with_reservation(attempt_id=reservation_id, record=replacement) is False
        after = _snapshot_attempt(repository._connection, reservation_id)
        event_after = _row(repository._connection, "public_alert_events", _event_id)
    assert after == before
    assert event_after == event_before


def test_r104_replacement_contradictory_attempted_alert_type_rejected(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r104.db")
    _event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r104", event_key="r104:owned", signal_id="r104-owned", run_id="r104"
    )
    replacement = _replacement_base(
        "r104-owned",
        "r104:owned",
        "life-r104",
        attempted_alert_type="TP1_HIT",
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        before = _snapshot_attempt(repository._connection, reservation_id)
        assert repository.replace_attempt_with_reservation(attempt_id=reservation_id, record=replacement) is False
        after = _snapshot_attempt(repository._connection, reservation_id)
    assert after == before


def test_r105_replacement_contradictory_entry_stop_targets_rejected(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r105.db")
    _event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r105", event_key="r105:owned", signal_id="r105-owned", run_id="r105"
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE setup_lifecycle_records
            SET tp1 = '110', tp2 = '120', tp3 = '130', rr = '3.5'
            WHERE lifecycle_id = 'life-r105'
            """
        )
        connection.commit()
    attacks = (
        {"entry_low": "999", "entry_high": "102", "stop_loss": "95"},
        {"entry_low": "100", "entry_high": "102", "stop_loss": "1"},
        {"entry_low": "100", "entry_high": "102", "stop_loss": "95", "tp1": "999", "tp2": "120", "tp3": "130"},
        {"entry_low": "100", "entry_high": "102", "stop_loss": "95", "tp1": "110", "tp2": "120", "tp3": "130", "rr_planned": "9.9"},
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        before = _snapshot_attempt(repository._connection, reservation_id)
        for fields in attacks:
            replacement = _replacement_base("r105-owned", "r105:owned", "life-r105", **fields)
            assert repository.replace_attempt_with_reservation(attempt_id=reservation_id, record=replacement) is False
            after = _snapshot_attempt(repository._connection, reservation_id)
            assert after == before


def test_r106_valid_semantically_equivalent_replacement_succeeds(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r106.db")
    event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r106", event_key="r106:owned", signal_id="r106-owned", run_id="r106"
    )
    _register_current_run(db_path, "r106-next")
    replacement = _replacement_base(
        "r106-owned",
        "r106:owned",
        "life-r106",
        entry_low="100.00",
        entry_high="102.0",
        stop_loss="95.00",
        attempted_alert_type="WATCHLIST",
        scan_run_id="r106-next",
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert repository.replace_attempt_with_reservation(attempt_id=reservation_id, record=replacement) is True
        after = _snapshot_attempt(repository._connection, reservation_id)
        event_after = _row(repository._connection, "public_alert_events", event_id)
        claimed = SQLitePublicTelegramOutbox(repository._connection).claim(
            event_id=event_id,
            reservation_id=reservation_id,
            now=NOW,
        )
    assert after["entry_low"] == "100.00"
    assert int(event_after["canonical_reservation_attempt_id"]) == reservation_id
    assert claimed.claim is not None


def test_r107_malformed_canonical_reservation_cannot_be_claimed(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r107.db")
    event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r107", event_key="r107:owned", signal_id="r107-owned", run_id="r107"
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute(
            "UPDATE telegram_alert_attempts SET alert_type = 'TP1_HIT' WHERE id = ?",
            (reservation_id,),
        )
        before = _snapshot_attempt(connection, reservation_id)
        event_before = dict(
            connection.execute("SELECT * FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        )
        connection.commit()
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        claimed = SQLitePublicTelegramOutbox(repository._connection).claim(
            event_id=event_id,
            reservation_id=reservation_id,
            now=NOW,
        )
        after = _snapshot_attempt(repository._connection, reservation_id)
        event_after = _row(repository._connection, "public_alert_events", event_id)
    assert claimed.claim is None
    assert after["alert_type"] == before["alert_type"] == "TP1_HIT"
    assert event_after["delivery_state"] == event_before["delivery_state"]
    assert int(event_after["canonical_reservation_attempt_id"]) == reservation_id


def _v25_insert_attempt(connection: sqlite3.Connection, *, signal_id: str, status: str = "blocked") -> int:
    cursor = connection.execute(
        """
        INSERT INTO telegram_alert_attempts (
            signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
            telegram_status, message_hash, attempted_alert_type, blocked_reason,
            error_message, first_seen_at, last_seen_at, seen_count
        ) VALUES (?, 'BTCUSDT', 'long', 'WATCHLISTED', 'WATCHLIST', 'WATCHLISTED',
                  ?, 'v25-hash', 'WATCHLIST', 'diagnostic', 'diagnostic',
                  '2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 3)
        """,
        (signal_id, status),
    )
    return int(cursor.lastrowid)


def _assert_no_unconditional_signal_alert_unique(connection: sqlite3.Connection) -> None:
    assert _telegram_alert_attempts_has_unconditional_signal_alert_unique(connection) is False
    sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='telegram_alert_attempts'"
    ).fetchone()[0]
    squeezed = "".join(str(sql).split())
    assert "UNIQUE(signal_id,alert_type)" not in squeezed
    names = {
        row[1]
        for row in connection.execute("PRAGMA index_list('telegram_alert_attempts')")
    }
    assert "ux_telegram_alert_attempts_signal_alert_operational" in names


def _coexistence_on_db(db_path: Path, *, signal_id: str, event_key: str, run_id: str, audit_first: bool) -> None:
    with open_initialized_database(db_path) as connection:
        if load_active_runtime_epoch(connection) is None:
            initialize_runtime_epoch(connection, SYNTHETIC_IDENTITY, activated_at=SYNTHETIC_CUTOFF_AT)
        connection.commit()
    if audit_first:
        _register_current_run(db_path, f"{run_id}-audit")
        with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            assert repository.insert_attempt(
                _audit_record(
                    signal_id=signal_id,
                    event_key="N/A",
                    status="blocked",
                    run_id=f"{run_id}-audit",
                )
            )
    _event_id, reservation_id, _part = _owned_event_reservation(
        db_path,
        lifecycle_id=f"life-{signal_id}",
        event_key=event_key,
        signal_id=signal_id,
        run_id=run_id,
    )
    if not audit_first:
        _register_current_run(db_path, f"{run_id}-audit")
        with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            assert repository.insert_attempt(
                _audit_record(
                    signal_id=signal_id,
                    event_key=event_key,
                    status="blocked",
                    run_id=f"{run_id}-audit",
                )
            )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        statuses = {
            str(row["telegram_status"])
            for row in connection.execute(
                "SELECT telegram_status FROM telegram_alert_attempts WHERE signal_id = ? AND alert_type = 'WATCHLIST'",
                (signal_id,),
            )
        }
        canonical = connection.execute(
            "SELECT canonical_reservation_attempt_id FROM public_alert_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()
    assert "blocked" in statuses
    assert "pending" in statuses
    assert canonical is not None
    assert int(canonical[0]) == reservation_id


def test_r108_genuine_v25_canonical_first_then_audit(tmp_path: Path) -> None:
    db_path = create_genuine_v25_database(tmp_path / "r108.db")
    with open_initialized_database(db_path) as connection:
        assert identify_schema_version(connection) == SCHEMA_VERSION
        _assert_no_unconditional_signal_alert_unique(connection)
    _coexistence_on_db(db_path, signal_id="r108-owned", event_key="r108:owned", run_id="r108", audit_first=False)


def test_r109_genuine_v25_audit_first_then_canonical(tmp_path: Path) -> None:
    db_path = create_genuine_v25_database(tmp_path / "r109.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        legacy_id = _v25_insert_attempt(connection, signal_id="r109-owned", status="blocked")
        before = dict(
            connection.execute("SELECT * FROM telegram_alert_attempts WHERE id = ?", (legacy_id,)).fetchone()
        )
        connection.commit()
    with open_initialized_database(db_path) as connection:
        after = dict(
            connection.execute("SELECT * FROM telegram_alert_attempts WHERE id = ?", (legacy_id,)).fetchone()
        )
        assert after["id"] == before["id"] == legacy_id
        assert after["signal_id"] == before["signal_id"]
        assert after["telegram_status"] == "blocked"
        assert after["seen_count"] == before["seen_count"]
        _assert_no_unconditional_signal_alert_unique(connection)
    _coexistence_on_db(db_path, signal_id="r109-owned", event_key="r109:owned", run_id="r109", audit_first=True)


def test_r110_fresh_v26_equivalent_coexistence(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r110.db")
    with sqlite3.connect(db_path) as connection:
        _assert_no_unconditional_signal_alert_unique(connection)
    _coexistence_on_db(db_path, signal_id="r110-owned", event_key="r110:owned", run_id="r110", audit_first=False)
    db_path_b = _bootstrap(tmp_path / "r110b.db")
    _coexistence_on_db(db_path_b, signal_id="r110b-owned", event_key="r110b:owned", run_id="r110b", audit_first=True)


def test_r111_migrated_v25_has_no_obsolete_unconditional_unique(tmp_path: Path) -> None:
    db_path = create_genuine_v25_database(tmp_path / "r111.db")
    with sqlite3.connect(db_path) as connection:
        assert _telegram_alert_attempts_has_unconditional_signal_alert_unique(connection) is True
        _v25_insert_attempt(connection, signal_id="r111-keep")
        connection.commit()
    with open_initialized_database(db_path) as connection:
        _assert_no_unconditional_signal_alert_unique(connection)
        indexes = connection.execute("PRAGMA index_list('telegram_alert_attempts')").fetchall()
        for index in indexes:
            origin = str(index[3]).lower() if len(index) > 3 and index[3] is not None else ""
            if origin != "u":
                continue
            columns = [
                str(row[2])
                for row in connection.execute(f'PRAGMA index_info("{index[1]}")').fetchall()
            ]
            assert columns != ["signal_id", "alert_type"]


def test_r112_repeated_migration_is_semantically_idempotent(tmp_path: Path) -> None:
    db_path = create_genuine_v25_database(tmp_path / "r112.db")
    with sqlite3.connect(db_path) as connection:
        attempt_id = _v25_insert_attempt(connection, signal_id="r112-keep")
        connection.commit()
    with open_initialized_database(db_path) as connection:
        first = snapshot_tables(connection)
        first_attempt = dict(
            connection.execute("SELECT * FROM telegram_alert_attempts WHERE id = ?", (attempt_id,)).fetchone()
        )
        assert identify_schema_version(connection) == SCHEMA_VERSION
        _assert_no_unconditional_signal_alert_unique(connection)
        initialize_database(connection)
        second = snapshot_tables(connection)
        second_attempt = dict(
            connection.execute("SELECT * FROM telegram_alert_attempts WHERE id = ?", (attempt_id,)).fetchone()
        )
        assert identify_schema_version(connection) == SCHEMA_VERSION
        _assert_no_unconditional_signal_alert_unique(connection)
        assert load_active_runtime_epoch(connection) is None
    assert first["telegram_alert_attempts"] == second["telegram_alert_attempts"]
    assert first_attempt == second_attempt
    assert first_attempt["id"] == attempt_id


def test_r113_migration_failure_preserves_rows_and_retryability(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = create_genuine_v25_database(tmp_path / "r113.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        attempt_id = _v25_insert_attempt(connection, signal_id="r113-keep")
        before = dict(
            connection.execute("SELECT * FROM telegram_alert_attempts WHERE id = ?", (attempt_id,)).fetchone()
        )
        connection.commit()

    def boom(connection: sqlite3.Connection) -> None:
        raise RuntimeError("injected v25 attempt-table rebuild failure")

    monkeypatch.setattr(database_module, "_copy_telegram_alert_attempts_pre_v26_unique_rows", boom)
    with pytest.raises(Exception):
        open_initialized_database(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        assert identify_schema_version(connection) == 25
        surviving = dict(
            connection.execute("SELECT * FROM telegram_alert_attempts WHERE id = ?", (attempt_id,)).fetchone()
        )
        assert surviving["id"] == before["id"] == attempt_id
        assert surviving["signal_id"] == before["signal_id"]
        assert surviving["telegram_status"] == before["telegram_status"]
        assert _telegram_alert_attempts_has_unconditional_signal_alert_unique(connection) is True
        names = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "telegram_alert_attempts" in names
        assert load_active_runtime_epoch(connection) is None
    monkeypatch.undo()
    with open_initialized_database(db_path) as connection:
        assert identify_schema_version(connection) == SCHEMA_VERSION
        retried = dict(
            connection.execute("SELECT * FROM telegram_alert_attempts WHERE id = ?", (attempt_id,)).fetchone()
        )
        assert retried["id"] == attempt_id
        assert retried["signal_id"] == "r113-keep"
        _assert_no_unconditional_signal_alert_unique(connection)
        assert load_active_runtime_epoch(connection) is None


def test_r114_post_migration_integrity_checks_pass(tmp_path: Path) -> None:
    db_path = create_genuine_v25_database(tmp_path / "r114.db")
    with sqlite3.connect(db_path) as connection:
        _v25_insert_attempt(connection, signal_id="r114-keep")
        connection.commit()
    with open_initialized_database(db_path) as connection:
        fk = connection.execute("PRAGMA foreign_key_check").fetchall()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
        quick = connection.execute("PRAGMA quick_check").fetchone()[0]
        _assert_no_unconditional_signal_alert_unique(connection)
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='telegram_alert_attempts'"
        ).fetchone()[0]
    assert fk == []
    assert str(integrity).lower() == "ok"
    assert str(quick).lower() == "ok"
    assert "UNIQUE(signal_id,alert_type)" not in "".join(str(sql).split())


def _identity_cli(db_path: Path) -> list[str]:
    return [
        "--database-path",
        str(db_path),
        "--runtime-epoch-id",
        SYNTHETIC_EPOCH_ID,
        "--runtime-epoch-cutoff-at",
        SYNTHETIC_CUTOFF_AT,
        "--runtime-reviewed-release-sha",
        SYNTHETIC_RELEASE_SHA,
        "--runtime-generation-binding",
        SYNTHETIC_GENERATION_BINDING,
        "--lifecycle",
        "--store-scan",
        "--telegram-manual-signals",
        "--no-cache",
        "--candle-limit",
        "220",
    ]


def _patch_production_orchestration(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    candles_by_symbol: dict[str, list],
    sender: FakeSender,
):
    clock = {"now": datetime(2026, 3, 1, 14, 0, tzinfo=UTC)}

    def _tick() -> datetime:
        current = clock["now"]
        clock["now"] = current + timedelta(seconds=5)
        return current

    def _timestamp() -> str:
        return _tick().strftime("%Y-%m-%dT%H:%M:%SZ")

    client = FakeAdapterExchangeClient(candles_by_symbol, failing_timeframes={"2d"})

    def _runner(cache=None, **_kwargs):
        return ScannerRunner(
            exchange_client=client,
            market_data_cache=cache,
            clock=_tick,
            capture_clock=_tick,
        )

    original_init = TelegramLifecycleDeliveryService.__init__

    def _init(self, *args, **kwargs):
        kwargs["sender"] = sender
        settings = kwargs.get("settings") or Settings(
            _env_file=None,
            telegram_dry_run=True,
            telegram_signals_enabled=False,
            local_manual_mode=True,
            order_execution_enabled=False,
        )
        kwargs["settings"] = settings.model_copy(update={"telegram_public_watchlist_enabled": True})
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(run_scan, "_scanner_runner", _runner)
    monkeypatch.setattr(run_scan, "_watch_iteration_timestamp", _timestamp)
    monkeypatch.setattr("app.lifecycle.service.now_utc_iso", _timestamp)
    monkeypatch.setattr(TelegramLifecycleDeliveryService, "__init__", _init)
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", tmp_path / "watch_state.json")
    monkeypatch.setattr(run_scan, "WATCH_STATE_CANONICAL_BASE_DIR", tmp_path)
    return clock


def test_r115_production_orchestration_registers_run_before_acquisition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "r115.db")
    sender = FakeSender()
    _patch_production_orchestration(
        monkeypatch,
        tmp_path,
        candles_by_symbol={"BTCUSDT": _public_min_rr_pullback_candles()},
        sender=sender,
    )
    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", *_identity_cli(db_path)]))
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        runs = connection.execute(
            "SELECT run_id, registered_at FROM runtime_operational_runs ORDER BY registered_at ASC"
        ).fetchall()
        origins = connection.execute(
            "SELECT run_id, symbol, evaluation_completed_at FROM runtime_operational_origins"
        ).fetchall()
    assert runs
    assert origins
    registered_at = runs[0]["registered_at"]
    evaluation = origins[0]["evaluation_completed_at"]
    assert registered_at
    assert evaluation
    assert datetime.fromisoformat(registered_at.replace("Z", "+00:00")) < datetime.fromisoformat(
        evaluation.replace("Z", "+00:00")
    )


def test_r116_production_orchestration_multi_symbol_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "r116.db")
    sender = FakeSender()
    candles = _strategy_pullback_candles()
    _patch_production_orchestration(
        monkeypatch,
        tmp_path,
        candles_by_symbol={"BTCUSDT": candles, "ETHUSDT": candles},
        sender=sender,
    )
    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "ETHUSDT", *_identity_cli(db_path)]))
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        run_row = connection.execute(
            "SELECT run_id, registered_at FROM runtime_operational_runs ORDER BY registered_at ASC"
        ).fetchone()
        origins = list(
            connection.execute(
                """
                SELECT symbol, run_id, status, evaluation_completed_at
                FROM runtime_operational_origins
                WHERE run_id = ?
                ORDER BY evaluation_completed_at ASC, symbol ASC
                """,
                (run_row["run_id"],),
            )
        )
        lives = list(
            connection.execute(
                "SELECT symbol, last_seen_at, creation_origin_id FROM setup_lifecycle_records"
            )
        )
    assert run_row is not None
    assert {row["symbol"] for row in origins} == {"BTCUSDT", "ETHUSDT"}
    assert all(row["status"] == "granted" for row in origins)
    t0 = datetime.fromisoformat(run_row["registered_at"].replace("Z", "+00:00"))
    t1 = datetime.fromisoformat(origins[0]["evaluation_completed_at"].replace("Z", "+00:00"))
    t2 = datetime.fromisoformat(origins[1]["evaluation_completed_at"].replace("Z", "+00:00"))
    t3_values = [
        datetime.fromisoformat(str(row["last_seen_at"]).replace("Z", "+00:00"))
        for row in lives
        if row["last_seen_at"]
    ]
    assert t3_values
    t3 = min(t3_values)
    assert t0 < t1 < t2 < t3
    origin_ids = {row["creation_origin_id"] for row in lives}
    assert None not in origin_ids


def test_r117_production_orchestration_valid_3r_path_reaches_fake_sender(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "r117.db")
    sender = FakeSender()
    _patch_production_orchestration(
        monkeypatch,
        tmp_path,
        candles_by_symbol={"BTCUSDT": _public_min_rr_pullback_candles()},
        sender=sender,
    )
    argv = ["--symbols", "BTCUSDT", *_identity_cli(db_path)]
    asyncio.run(run_scan.main(argv))
    asyncio.run(run_scan.main(argv))
    assert sender.calls
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        life = connection.execute(
            "SELECT current_state, confirmation_count FROM setup_lifecycle_records WHERE symbol = 'BTCUSDT'"
        ).fetchone()
        event = connection.execute(
            "SELECT canonical_reservation_attempt_id FROM public_alert_events WHERE runtime_epoch_id IS NOT NULL"
        ).fetchone()
    assert life is not None
    assert life["current_state"] == SetupLifecycleState.CONFIRMED.value
    assert int(life["confirmation_count"]) >= 2
    assert event is not None
    assert event["canonical_reservation_attempt_id"] is not None


def test_r118_removing_production_registration_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "r118.db")
    sender = FakeSender()
    _patch_production_orchestration(
        monkeypatch,
        tmp_path,
        candles_by_symbol={"BTCUSDT": _public_min_rr_pullback_candles()},
        sender=sender,
    )
    monkeypatch.setattr(run_scan, "_register_operational_scan_run_before_acquisition", lambda *args, **kwargs: None)
    with pytest.raises(SystemExit):
        asyncio.run(run_scan.main(["--symbols", "BTCUSDT", *_identity_cli(db_path)]))
    with sqlite3.connect(db_path) as connection:
        runs = connection.execute("SELECT COUNT(*) FROM runtime_operational_runs").fetchone()[0]
        origins = connection.execute("SELECT COUNT(*) FROM runtime_operational_origins").fetchone()[0]
        lives = connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0]
        events = connection.execute("SELECT COUNT(*) FROM public_alert_events").fetchone()[0]
    assert runs == 0
    assert origins == 0
    assert lives == 0
    assert events == 0
    assert sender.calls == []


def test_r119_canonical_266r_remains_blocked_by_public_3r(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_prospective_runtime_epoch_isolation_repair2 import (
        test_r58_insufficient_public_rr_pipeline_does_not_send,
    )

    assert PUBLIC_SIGNAL_MIN_RR == Decimal("3")
    test_r58_insufficient_public_rr_pipeline_does_not_send(tmp_path, monkeypatch)


def test_r120_legacy_sent_global_event_key_consumption_remains_intact(tmp_path: Path) -> None:
    from tests.test_prospective_runtime_epoch_isolation_repair3 import (
        test_r88_legacy_sent_event_key_remains_consumed,
    )

    test_r88_legacy_sent_event_key_remains_consumed(tmp_path)


def test_r121_legacy_uncertain_remains_frozen(tmp_path: Path) -> None:
    from tests.test_prospective_runtime_epoch_isolation_repair3 import test_r89_legacy_uncertain_untouched

    test_r89_legacy_uncertain_untouched(tmp_path)


def test_r122_strategy_economic_regression_unchanged() -> None:
    assert PUBLIC_SIGNAL_MIN_RR == Decimal("3")
    assert DEFAULT_CONFIGURED_MINIMUM_RR == Decimal("2.5")
