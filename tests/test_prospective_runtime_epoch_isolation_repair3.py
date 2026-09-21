"""Third bounded repair proofs R61–R90 for PROSPECTIVE_RUNTIME_EPOCH_ISOLATION.

DEV-only temp databases, mocked senders, and deterministic clocks.
No Runtime filesystem, live Telegram, exchange calls, or scanner watch loops.
"""

from __future__ import annotations

import asyncio
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
from app.alerts.telegram_outbox import (
    IN_FLIGHT,
    PENDING,
    SENT,
    SKIPPED_DRY_RUN,
    UNCERTAIN,
    SQLitePublicTelegramOutbox,
)
from app.core.minimum_rr import DEFAULT_CONFIGURED_MINIMUM_RR
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import apply_lifecycle_to_run_result
from app.storage.database import open_initialized_database
from app.pipeline.scanner_runner import ScannerRunner
from app.runtime_epoch.authority import require_active_runtime_epoch
from app.runtime_epoch.errors import RuntimeEpochOriginError
from app.runtime_epoch.origin import (
    evaluation_completed_at_from_symbol_result,
    producer_acquisition_from_symbol_result,
    register_operational_scan_run,
)
from app.runtime_epoch.ownership import decide_public_effect, public_ownership_chain_reason
from app.core.config import Settings
from app.telegram_admin import TelegramAdminConfig, route_admin_scan_report
from app.telegram_admin.signal_detail import load_active_signal_detail
from test_telegram_admin_draft_router import FakeAdminTransport, _run_result as _admin_run_result, _valid_result
from test_telegram_lifecycle_delivery_phase42 import FakeSender, _public_v1_symbol, _run_result, _run_result_many, run
from tests.runtime_epoch_support import (
    SYNTHETIC_IDENTITY,
    assert_legacy_sent_consumption_frozen,
    bootstrap_operational_test_database,
    grant_synthetic_origin,
    seed_legacy_attempt,
    seed_legacy_public_event,
    snapshot_tables,
)
from tests.test_prospective_runtime_epoch_isolation import NOW, _settings
from tests.test_prospective_runtime_epoch_isolation_boundaries import (
    CUTOFF_IDENTITY,
    PROCESS_DT,
    _attempt_record,
    _bootstrap,
    _fresh_symbol,
    _live_adapter_delivery,
    _owned_record,
    _plan,
)
from tests.test_prospective_runtime_epoch_isolation_repair2 import (
    FakeAdapterExchangeClient,
    _owned_event_reservation,
    _producer_lifecycle_then_deliver,
    _public_min_rr_pullback_candles,
    _row,
)
from tests.test_scanner_runner import _config, _strategy_pullback_candles, run as run_scanner

pytestmark = pytest.mark.no_auto_epoch


def _audit_record(signal_id: str, event_key: str):
    return _attempt_record(
        signal_id=signal_id,
        event_key=event_key,
        status="blocked",
        blocked_reason="diagnostic",
    )


def test_r61_operational_run_is_registered_before_producer(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r61.db", CUTOFF_IDENTITY)
    register_operational_scan_run(
        db_path,
        run_id="r61-run",
        registered_at="2026-03-01T13:59:00Z",
        expected_identity=CUTOFF_IDENTITY,
    )
    clock_now = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    scanned = run_scanner(
        ScannerRunner(
            exchange_client=FakeAdapterExchangeClient(
                {"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"}
            ),
            clock=lambda: clock_now,
            capture_clock=lambda: clock_now,
        ).run(_config(["BTCUSDT"]))
    )
    symbol = scanned.results[0]
    evaluation = evaluation_completed_at_from_symbol_result(symbol)
    producer = producer_acquisition_from_symbol_result(symbol)
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        run_row = connection.execute(
            "SELECT registered_at FROM runtime_operational_runs WHERE run_id = 'r61-run'"
        ).fetchone()
    assert run_row is not None
    assert run_row["registered_at"] == "2026-03-01T13:59:00Z"
    assert evaluation is not None
    assert producer is not None
    assert datetime.fromisoformat(run_row["registered_at"].replace("Z", "+00:00")) < datetime.fromisoformat(
        evaluation.replace("Z", "+00:00")
    )
    assert datetime.fromisoformat(run_row["registered_at"].replace("Z", "+00:00")) < datetime.fromisoformat(
        producer.replace("Z", "+00:00")
    )


def test_r62_fresh_evaluation_consumed_later_remains_eligible(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r62.db", CUTOFF_IDENTITY)
    register_operational_scan_run(
        db_path,
        run_id="r62-run",
        registered_at="2026-03-01T13:00:00Z",
        expected_identity=CUTOFF_IDENTITY,
    )
    symbol = _fresh_symbol(
        evaluation_completed_at=PROCESS_DT,
        decision=PROCESS_DT,
        delivery=_live_adapter_delivery(acquired_at=PROCESS_DT),
        signal_id="r62",
    )
    apply_lifecycle_to_run_result(
        _run_result(symbol),
        database_path=db_path,
        scan_run_id="r62-run",
        now="2026-03-01T13:30:01Z",
        expected_identity=CUTOFF_IDENTITY,
    )
    with sqlite3.connect(db_path) as connection:
        origin = connection.execute(
            "SELECT status FROM runtime_operational_origins WHERE run_id = 'r62-run' AND symbol = 'BTCUSDT'"
        ).fetchone()
    assert origin is not None
    assert origin[0] == "granted"


def test_r63_multi_symbol_earlier_evaluation_not_rejected(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r63.db", CUTOFF_IDENTITY)
    register_operational_scan_run(
        db_path,
        run_id="r63-run",
        registered_at="2026-03-01T13:00:00Z",
        expected_identity=CUTOFF_IDENTITY,
    )
    btc_at = datetime(2026, 3, 1, 13, 10, tzinfo=UTC)
    eth_at = datetime(2026, 3, 1, 13, 20, tzinfo=UTC)
    btc = _fresh_symbol(
        evaluation_completed_at=btc_at,
        decision=btc_at,
        delivery=_live_adapter_delivery(acquired_at=btc_at),
        signal_id="r63-btc",
        symbol="BTCUSDT",
    )
    eth = _fresh_symbol(
        evaluation_completed_at=eth_at,
        decision=eth_at,
        delivery=_live_adapter_delivery(acquired_at=eth_at, symbol="ETHUSDT"),
        signal_id="r63-eth",
        symbol="ETHUSDT",
    )
    apply_lifecycle_to_run_result(
        _run_result_many(btc, eth),
        database_path=db_path,
        scan_run_id="r63-run",
        now="2026-03-01T13:30:00Z",
        expected_identity=CUTOFF_IDENTITY,
    )
    with sqlite3.connect(db_path) as connection:
        statuses = {
            str(row[0]): str(row[1])
            for row in connection.execute(
                "SELECT symbol, status FROM runtime_operational_origins WHERE run_id = 'r63-run'"
            )
        }
    assert statuses["BTCUSDT"] == "granted"
    assert statuses["ETHUSDT"] == "granted"


def test_r64_missing_run_registration_blocks_and_does_not_create(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r64.db", CUTOFF_IDENTITY)
    with pytest.raises(RuntimeEpochOriginError):
        apply_lifecycle_to_run_result(
            _run_result(
                _fresh_symbol(
                    evaluation_completed_at=PROCESS_DT,
                    decision=PROCESS_DT,
                    delivery=_live_adapter_delivery(acquired_at=PROCESS_DT),
                    signal_id="r64",
                )
            ),
            database_path=db_path,
            scan_run_id="r64-missing",
            now="2026-03-01T13:30:01Z",
            expected_identity=CUTOFF_IDENTITY,
        )
    with sqlite3.connect(db_path) as connection:
        runs = connection.execute(
            "SELECT COUNT(*) FROM runtime_operational_runs WHERE run_id = 'r64-missing'"
        ).fetchone()[0]
        origins = connection.execute("SELECT COUNT(*) FROM runtime_operational_origins").fetchone()[0]
    assert runs == 0
    assert origins == 0


def test_r65_keyed_audit_cannot_be_canonical_reservation(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r65.db")
    event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r65", event_key="r65:owned", signal_id="r65-owned", run_id="r65"
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert repository.insert_attempt(_audit_record("r65-audit", "r65:owned"), audit_only=True) is True
        audit_id = int(
            repository._connection.execute(
                "SELECT id FROM telegram_alert_attempts WHERE signal_id = 'r65-audit'"
            ).fetchone()[0]
        )
        event = repository._connection.execute(
            "SELECT canonical_reservation_attempt_id FROM public_alert_events WHERE id = ?",
            (event_id,),
        ).fetchone()
    assert event is not None
    assert int(event[0]) == reservation_id
    assert audit_id != reservation_id


def test_r66_claim_rejects_keyed_audit_unchanged(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r66.db")
    event_id, _res, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r66", event_key="r66:owned", signal_id="r66-owned", run_id="r66"
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert repository.insert_attempt(_audit_record("r66-audit", "r66:owned"), audit_only=True)
        audit_id = int(
            repository._connection.execute(
                "SELECT id FROM telegram_alert_attempts WHERE signal_id = 'r66-audit'"
            ).fetchone()[0]
        )
        before = _row(repository._connection, "telegram_alert_attempts", audit_id)
        event_before = _row(repository._connection, "public_alert_events", event_id)
        claimed = SQLitePublicTelegramOutbox(repository._connection).claim(
            event_id=event_id, reservation_id=audit_id, now=NOW
        )
        after = _row(repository._connection, "telegram_alert_attempts", audit_id)
        event_after = _row(repository._connection, "public_alert_events", event_id)
    assert claimed.claim is None
    assert after == before
    assert event_after == event_before


def test_r67_stale_recovery_updates_only_canonical_reservation(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r67.db")
    event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r67", event_key="r67:owned", signal_id="r67-owned", run_id="r67"
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert repository.insert_attempt(_audit_record("r67-audit", "r67:owned"), audit_only=True)
        audit_id = int(
            repository._connection.execute(
                "SELECT id FROM telegram_alert_attempts WHERE signal_id = 'r67-audit'"
            ).fetchone()[0]
        )
        claimed = SQLitePublicTelegramOutbox(repository._connection).claim(
            event_id=event_id, reservation_id=reservation_id, now=NOW
        )
        assert claimed.claim is not None
        repository._connection.execute(
            "UPDATE public_alert_events SET lease_expires_at = ? WHERE id = ?",
            ("2026-01-01T00:00:00Z", event_id),
        )
        before_audit = _row(repository._connection, "telegram_alert_attempts", audit_id)
        recovered = SQLitePublicTelegramOutbox(repository._connection).recover_stale_in_flight(
            event_id=event_id, now=NOW
        )
        after_audit = _row(repository._connection, "telegram_alert_attempts", audit_id)
        after_res = _row(repository._connection, "telegram_alert_attempts", reservation_id)
    assert recovered is True
    assert after_audit == before_audit
    assert after_audit["telegram_status"] == "blocked"
    assert after_res["telegram_status"] == "uncertain"
    assert after_res["delivery_state"] == UNCERTAIN


def _reject_noncanonical(method_name: str, tmp_path: Path, label: str) -> None:
    db_path = _bootstrap(tmp_path / f"{label}.db")
    event_id, _res, part_id = _owned_event_reservation(
        db_path,
        lifecycle_id=f"life-{label}",
        event_key=f"{label}:owned",
        signal_id=f"{label}-owned",
        run_id=label,
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert repository.insert_attempt(_audit_record(f"{label}-audit", f"{label}:owned"), audit_only=True)
        audit_id = int(
            repository._connection.execute(
                "SELECT id FROM telegram_alert_attempts WHERE signal_id = ?",
                (f"{label}-audit",),
            ).fetchone()[0]
        )
        before = _row(repository._connection, "telegram_alert_attempts", audit_id)
        event_before = _row(repository._connection, "public_alert_events", event_id)
        outbox = SQLitePublicTelegramOutbox(repository._connection)
        if method_name == "record_part_result":
            outbox.record_part_result(
                event_id=event_id,
                reservation_id=audit_id,
                part_id=part_id,
                attempt_id="forged",
                result={"delivery_state": SENT, "message_id": "1", "sent_at": NOW},
                now=NOW,
            )
        elif method_name == "mark_terminal_without_send":
            outbox.mark_terminal_without_send(
                event_id=event_id,
                reservation_id=audit_id,
                state=SKIPPED_DRY_RUN,
                reason="forged",
                now=NOW,
            )
        elif method_name == "mark_uncertain_after_persistence_failure":
            outbox.mark_uncertain_after_persistence_failure(
                event_id=event_id,
                reservation_id=audit_id,
                detail="forged",
                now=NOW,
            )
        else:
            repository.mark_public_watchlist_reservation_result(
                attempt_id=audit_id,
                status="sent",
                sent_at=NOW,
                message_hash="x",
                error_message="N/A",
                dedupe_status="N/A",
                dedupe_reason="N/A",
            )
        after = _row(repository._connection, "telegram_alert_attempts", audit_id)
        event_after = _row(repository._connection, "public_alert_events", event_id)
    assert after == before
    assert event_after == event_before


def test_r68_record_part_result_rejects_audit(tmp_path: Path) -> None:
    _reject_noncanonical("record_part_result", tmp_path, "r68")


def test_r69_mark_terminal_rejects_audit(tmp_path: Path) -> None:
    _reject_noncanonical("mark_terminal_without_send", tmp_path, "r69")


def test_r70_mark_uncertain_rejects_audit(tmp_path: Path) -> None:
    _reject_noncanonical("mark_uncertain_after_persistence_failure", tmp_path, "r70")


def test_r71_mark_watchlist_result_rejects_audit(tmp_path: Path) -> None:
    _reject_noncanonical("mark_public_watchlist_reservation_result", tmp_path, "r71")


def test_r72_replace_rejects_opposite_direction(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r72.db")
    _event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r72", event_key="r72:owned", signal_id="r72-owned", run_id="r72"
    )
    replacement = replace(
        _attempt_record(
            signal_id="r72-owned",
            event_key="r72:owned",
            status="pending",
            delivery_state=PENDING,
            direction="short",
        ),
        public_watchlist_plan_id="life-r72-plan",
        public_alert_event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        before = _row(repository._connection, "telegram_alert_attempts", reservation_id)
        assert repository.replace_attempt_with_reservation(attempt_id=reservation_id, record=replacement) is False
        after = _row(repository._connection, "telegram_alert_attempts", reservation_id)
    assert after == before
    assert after["direction"] == "long"


def test_r73_replace_rejects_incompatible_family_or_plan(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r73.db")
    _event_id, reservation_id, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r73", event_key="r73:owned", signal_id="r73-owned", run_id="r73"
    )
    family = replace(
        _attempt_record(
            signal_id="r73-owned",
            event_key="r73:owned",
            status="pending",
            delivery_state=PENDING,
            alert_type="TP1_HIT",
        ),
        public_watchlist_plan_id="life-r73-plan",
        public_alert_event_type="tp1_hit",
    )
    plan = replace(
        _attempt_record(
            signal_id="r73-owned",
            event_key="r73:owned",
            status="pending",
            delivery_state=PENDING,
        ),
        public_watchlist_plan_id="other-plan",
        public_alert_event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        before = _row(repository._connection, "telegram_alert_attempts", reservation_id)
        assert repository.replace_attempt_with_reservation(attempt_id=reservation_id, record=family) is False
        assert repository.replace_attempt_with_reservation(attempt_id=reservation_id, record=plan) is False
        after = _row(repository._connection, "telegram_alert_attempts", reservation_id)
    assert after == before


def test_r74_long_lifecycle_cannot_authorize_short_root(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r74.db")
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id="r74", now=NOW)
        lifecycle.upsert_record(_owned_record(origin.origin_id, lifecycle_id="life-r74", direction="long"))
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        event, created = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-r74-plan", side="short"),
            event_key="r74:short-root",
            event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-r74",
        )
    assert created is False
    assert event is None


def test_r75_matching_short_root_child_cannot_bypass_long_lifecycle(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r75.db")
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id="r75", now=NOW)
        lifecycle.upsert_record(_owned_record(origin.origin_id, lifecycle_id="life-r75", direction="long"))
        epoch = require_active_runtime_epoch(lifecycle.connection)
        lifecycle.connection.execute(
            """
            INSERT INTO public_alert_events (
                canonical_plan_id, event_type, event_key, symbol, side, status,
                created_at, updated_at, runtime_epoch_id, origin_lifecycle_id
            ) VALUES (?, 'initial_watchlist', 'r75:short-root', 'BTCUSDT', 'short', 'RESERVED', ?, ?, ?, ?)
            """,
            ("life-r75-plan", NOW, NOW, epoch.epoch_id, "life-r75"),
        )
        root_id = int(lifecycle.connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        lifecycle.connection.execute(
            """
            INSERT INTO public_alert_events (
                canonical_plan_id, event_type, event_key, symbol, side, status,
                created_at, updated_at, runtime_epoch_id, origin_lifecycle_id, origin_root_event_id
            ) VALUES (?, 'tp1_hit', 'r75:short-child', 'BTCUSDT', 'short', 'RESERVED', ?, ?, ?, ?, ?)
            """,
            ("life-r75-plan", NOW, NOW, epoch.epoch_id, "life-r75", root_id),
        )
        lifecycle.connection.commit()
        child = lifecycle.connection.execute(
            "SELECT * FROM public_alert_events WHERE event_key = 'r75:short-child'"
        ).fetchone()
        decision = decide_public_effect(lifecycle.connection, child, epoch=epoch)
        reason = public_ownership_chain_reason(lifecycle.connection, child, epoch=epoch)
    assert decision.allowed is False
    assert reason == "public_event_direction_mismatch"


def test_r76_unkeyed_insert_cannot_create_sent(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r76.db")
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert (
            repository.insert_attempt(
                _attempt_record(signal_id="r76-sent", event_key="N/A", status="sent", delivery_state=SENT)
            )
            is False
        )
        count = repository._connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts WHERE signal_id = 'r76-sent'"
        ).fetchone()[0]
    assert count == 0


def test_r77_unkeyed_insert_cannot_create_operational_states(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r77.db")
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        for status, delivery in (
            ("pending", PENDING),
            ("retryable", "RETRYABLE"),
            ("in_flight", IN_FLIGHT),
            ("uncertain", UNCERTAIN),
        ):
            assert (
                repository.insert_attempt(
                    _attempt_record(
                        signal_id=f"r77-{status}",
                        event_key="N/A",
                        status=status,
                        delivery_state=delivery,
                    )
                )
                is False
            )
        count = repository._connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts WHERE signal_id LIKE 'r77-%'"
        ).fetchone()[0]
    assert count == 0


def test_r78_explicit_audit_insert_cannot_be_claimed_or_sent(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r78.db")
    event_id, _res, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r78", event_key="r78:owned", signal_id="r78-owned", run_id="r78"
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert repository.insert_attempt(
            _attempt_record(signal_id="r78-audit", event_key="N/A", status="blocked"),
            audit_only=True,
        )
        audit_id = int(
            repository._connection.execute(
                "SELECT id FROM telegram_alert_attempts WHERE signal_id = 'r78-audit'"
            ).fetchone()[0]
        )
        before = _row(repository._connection, "telegram_alert_attempts", audit_id)
        claimed = SQLitePublicTelegramOutbox(repository._connection).claim(
            event_id=event_id, reservation_id=audit_id, now=NOW
        )
        sent = repository.insert_attempt(
            _attempt_record(signal_id="r78-audit-sent", event_key="N/A", status="sent", delivery_state=SENT),
            audit_only=True,
        )
        after = _row(repository._connection, "telegram_alert_attempts", audit_id)
    assert claimed.claim is None
    assert sent is False
    assert after == before
    assert after["telegram_status"] == "blocked"


def _admin_config() -> TelegramAdminConfig:
    return TelegramAdminConfig(
        admin_enabled=True,
        dry_run=False,
        bot_token="secret-token",
        admin_chat_id="admin-chat",
    )


def test_r79_admin_route_without_context_cannot_send_valid_setups(tmp_path: Path) -> None:
    transport = FakeAdminTransport()
    routed = asyncio.run(
        route_admin_scan_report(
            _admin_run_result((_valid_result(),), run_id="r79"),
            config=_admin_config(),
            transport=transport,
            drafts_dir=tmp_path,
        )
    )
    assert routed.delivery_status == "sent_admin"
    assert transport.calls
    valid_section = transport.calls[0]["message"].split("Valid Setups", 1)[1].split("Near Misses", 1)[0]
    assert "VALIDUSDT" not in valid_section


def test_r80_admin_route_wrong_direction_cannot_recommend(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r80.db")
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="VALIDUSDT", run_id="r80", now=NOW)
        lifecycle.upsert_record(
            _owned_record(origin.origin_id, lifecycle_id="life-r80", symbol="VALIDUSDT", direction="long").model_copy(
                update={"setup_id": "setup-A", "setup_identity": "VALIDUSDT|swing|long"}
            )
        )
    short_record = _owned_record(
        "foreign", lifecycle_id="life-r80-short", symbol="VALIDUSDT", direction="short"
    ).model_copy(update={"setup_id": "setup-B", "setup_identity": "VALIDUSDT|swing|short"})
    short_result = _valid_result().model_copy(
        update={
            "lifecycle_state": short_record,
            "strategy_diagnostics": {
                "swing": {
                    **_valid_result().strategy_diagnostics["swing"],
                    "bias": "short",
                }
            },
        }
    )
    transport = FakeAdminTransport()
    asyncio.run(
        route_admin_scan_report(
            _admin_run_result((short_result,), run_id="r80"),
            config=_admin_config(),
            transport=transport,
            drafts_dir=tmp_path,
            database_path=db_path,
            expected_identity=SYNTHETIC_IDENTITY,
        )
    )
    assert transport.calls
    valid_section = transport.calls[0]["message"].split("Valid Setups", 1)[1].split("Near Misses", 1)[0]
    assert "VALIDUSDT" not in valid_section


def test_r81_admin_route_exact_owned_setup_still_recommends(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r81.db")
    record = _owned_record("pending", lifecycle_id="life-r81", symbol="VALIDUSDT", direction="long").model_copy(
        update={"setup_id": "setup-A", "setup_identity": "VALIDUSDT|swing|long"}
    )
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="VALIDUSDT", run_id="r81", now=NOW)
        owned = record.model_copy(update={"creation_origin_id": origin.origin_id})
        lifecycle.upsert_record(owned)
    owned_result = _valid_result().model_copy(update={"lifecycle_state": owned})
    transport = FakeAdminTransport()
    routed = asyncio.run(
        route_admin_scan_report(
            _admin_run_result((owned_result,), run_id="r81"),
            config=_admin_config(),
            transport=transport,
            drafts_dir=tmp_path,
            database_path=db_path,
            expected_identity=SYNTHETIC_IDENTITY,
        )
    )
    assert routed.delivery_status == "sent_admin"
    assert transport.calls
    assert "VALIDUSDT | long" in transport.calls[0]["message"]


def _seed_sent_active_attempt(
    db_path: Path,
    *,
    lifecycle_id: str,
    event_key: str,
    signal_id: str,
    run_id: str,
    attempt_direction: str | None = None,
    attempt_plan_id: str | None = None,
) -> int:
    _event_id, reservation_id, _part = _owned_event_reservation(
        db_path,
        lifecycle_id=lifecycle_id,
        event_key=event_key,
        signal_id=signal_id,
        run_id=run_id,
    )
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET telegram_status = 'sent', delivery_state = ?, sent_at = ?,
                alert_type = 'SIGNAL_CONFIRMED', attempted_alert_type = 'SIGNAL_CONFIRMED',
                new_state = 'CONFIRMED', lifecycle_state = 'CONFIRMED',
                direction = ?, public_watchlist_plan_id = COALESCE(?, public_watchlist_plan_id)
            WHERE id = ?
            """,
            (SENT, NOW, attempt_direction or "long", attempt_plan_id, reservation_id),
        )
        connection.commit()
    return reservation_id


def test_r82_active_detail_mismatched_attempt_direction_is_none(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r82.db")
    _seed_sent_active_attempt(
        db_path,
        lifecycle_id="life-r82",
        event_key="r82:owned",
        signal_id="r82-owned",
        run_id="r82",
        attempt_direction="short",
    )
    result = load_active_signal_detail(project_root=tmp_path, database_path=db_path, selector="BTCUSDT")
    assert result.detail is None


def test_r83_active_detail_unrelated_plan_is_none(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r83.db")
    _seed_sent_active_attempt(
        db_path,
        lifecycle_id="life-r83-owned",
        event_key="r83:owned",
        signal_id="r83-owned",
        run_id="r83",
        attempt_plan_id="unrelated-plan",
    )
    result = load_active_signal_detail(project_root=tmp_path, database_path=db_path, selector="BTCUSDT")
    assert result.detail is None


def test_r84_active_detail_uses_event_origin_lifecycle_id(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r84.db")
    _seed_sent_active_attempt(
        db_path,
        lifecycle_id="life-r84-event",
        event_key="r84:owned",
        signal_id="r84-owned",
        run_id="r84",
    )
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        foreign = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id="r84-geo", now=NOW)
        lifecycle.upsert_record(
            _owned_record(foreign.origin_id, lifecycle_id="life-r84-geometry", direction="long").model_copy(
                update={"is_current": False, "entry_low": "1", "entry_high": "2", "stop_loss": "0.5"}
            )
        )
    result = load_active_signal_detail(project_root=tmp_path, database_path=db_path, selector="BTCUSDT")
    if result.detail is not None:
        assert "life-r84-geometry" not in (result.detail.lifecycle or "")


def test_r85_real_positive_path_with_advancing_clock(tmp_path: Path) -> None:
    (
        first,
        _applied_first,
        applied,
        summary,
        sender,
        origin,
        life,
        event,
        _attempt,
        _part,
        db_path,
    ) = _producer_lifecycle_then_deliver(
        tmp_path,
        db_name="r85.db",
        candles=_public_min_rr_pullback_candles(),
        run_prefix="r85",
    )
    evaluation = evaluation_completed_at_from_symbol_result(first.results[0])
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        registered = connection.execute(
            "SELECT registered_at FROM runtime_operational_runs WHERE run_id = 'r85-run-1'"
        ).fetchone()["registered_at"]
    assert datetime.fromisoformat(registered.replace("Z", "+00:00")) < datetime.fromisoformat(
        evaluation.replace("Z", "+00:00")
    )
    assert origin is not None
    assert life is not None
    assert applied.results[0].lifecycle_state.current_state == SetupLifecycleState.CONFIRMED
    assert summary.sent == 1
    assert sender.calls
    assert event is not None


def test_r86_multi_symbol_producer_preserves_earlier_origin(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r86.db", CUTOFF_IDENTITY)
    register_operational_scan_run(
        db_path,
        run_id="r86-run",
        registered_at="2026-03-01T13:59:00Z",
        expected_identity=CUTOFF_IDENTITY,
    )
    clock = {"now": datetime(2026, 3, 1, 14, 0, tzinfo=UTC)}

    def _clock():
        current = clock["now"]
        clock["now"] = current + timedelta(seconds=5)
        return current

    candles = _strategy_pullback_candles()
    scanned = run_scanner(
        ScannerRunner(
            exchange_client=FakeAdapterExchangeClient(
                {"BTCUSDT": candles, "ETHUSDT": candles},
                failing_timeframes={"2d"},
            ),
            clock=_clock,
            capture_clock=_clock,
        ).run(_config(["BTCUSDT", "ETHUSDT"]))
    )
    apply_lifecycle_to_run_result(
        scanned,
        database_path=db_path,
        scan_run_id="r86-run",
        now="2026-03-01T14:30:00Z",
        expected_identity=CUTOFF_IDENTITY,
    )
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT symbol, status FROM runtime_operational_origins WHERE run_id = 'r86-run'"
        ).fetchall()
    statuses = {str(row[0]): str(row[1]) for row in rows}
    assert statuses["BTCUSDT"] == "granted"
    assert statuses["ETHUSDT"] == "granted"


def test_r87_canonical_266r_negative_still_blocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.test_prospective_runtime_epoch_isolation_repair2 import (
        test_r58_insufficient_public_rr_pipeline_does_not_send,
    )

    assert PUBLIC_SIGNAL_MIN_RR == Decimal("3")
    assert DEFAULT_CONFIGURED_MINIMUM_RR == Decimal("2.5")
    test_r58_insufficient_public_rr_pipeline_does_not_send(tmp_path, monkeypatch)


def test_r88_legacy_sent_event_key_remains_consumed(tmp_path: Path) -> None:
    db_path = bootstrap_operational_test_database(tmp_path / "r88.db")
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
        database_path=db_path,
        settings=_settings(),
        sender=sender,
        expected_identity=SYNTHETIC_IDENTITY,
    )
    run(service.deliver_for_run(_run_result(_public_v1_symbol(signal_id="fresh-same-key")), scan_run_id="r88"))
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


def test_r89_legacy_uncertain_untouched(tmp_path: Path) -> None:
    from tests.test_prospective_runtime_epoch_isolation_repair2 import (
        test_r60_legacy_uncertain_and_reservation_unchanged_under_borrowed_event_attacks,
    )

    test_r60_legacy_uncertain_and_reservation_unchanged_under_borrowed_event_attacks(tmp_path)


def test_r90_strategy_gates_unchanged() -> None:
    assert PUBLIC_SIGNAL_MIN_RR == Decimal("3")
    assert DEFAULT_CONFIGURED_MINIMUM_RR == Decimal("2.5")
