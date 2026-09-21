from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import (
    PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
    PublicWatchlistPlanIdentity,
    SQLiteTelegramAlertAttemptRepository,
    TelegramLifecycleDeliveryService,
    _insert_public_alert_event,
)
from app.alerts.telegram_outbox import (
    PENDING,
    SENT,
    UNCERTAIN,
    SQLitePublicTelegramOutbox,
    persist_intent_parts,
)
from app.core.config import Settings
from app.data.candle_batch_evidence import (
    CACHE_KIND_HIT,
    observe_adapter_normalized_batch,
    observe_cache_delivery,
    observe_closed_subset,
)
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import apply_lifecycle_to_run_result
from app.pipeline.scanner_runner import ScannerSymbolResult
from app.runtime_epoch.authority import initialize_runtime_epoch, load_active_runtime_epoch, require_active_runtime_epoch
from app.runtime_epoch.errors import (
    RuntimeEpochConfigurationError,
    RuntimeEpochError,
    RuntimeEpochOriginError,
    RuntimeEpochOwnershipError,
)
from app.runtime_epoch.models import (
    ORIGIN_KIND_LIVE_FRESH,
    RUNTIME_EPOCH_CONTRACT_VERSION,
    RuntimeEpochIdentity,
)
from app.runtime_epoch.origin import (
    decision_cutoff_from_symbol_result,
    evaluate_symbol_origin,
    evaluation_completed_at_from_symbol_result,
    origin_kind_from_symbol_result,
    producer_acquisition_from_symbol_result,
    register_operational_run,
)
from app.runtime_epoch.ownership import decide_public_effect, require_lifecycle_public_intent
from app.runtime_epoch.startup import (
    inspect_operational_database,
    open_repository_database,
    require_operational_runtime,
)
from app.runtime_epoch.watch_state import (
    canonical_legacy_watch_state_path,
    load_or_initialize_operational_watch_payload,
    operational_watch_state_path,
    require_operational_watch_payload,
)
from app.storage.database import (
    SCHEMA_VERSION,
    UnsupportedSchemaVersionError,
    identify_schema_version,
    open_initialized_database,
)
from app.storage.models import TelegramAlertAttemptRecord
from app.telegram_admin.draft_router import build_admin_drafts, format_admin_scan_report
from app.telegram_admin.signal_detail import load_active_signal_detail

from test_lifecycle_outcomes import (
    _baseline as _outcome_baseline,
    _entry as _outcome_entry,
    _evaluate as _evaluate_outcomes,
    _record as _outcome_record,
)
from test_telegram_admin_draft_router import _run_result as _admin_run_result
from test_telegram_admin_draft_router import _valid_result
from test_telegram_lifecycle_delivery_phase42 import (
    FakeSender,
    _public_v1_symbol,
    _research_settings,
    _research_symbol,
    _run_result,
    _symbol,
    run,
)
from tests.fixtures.genuine_v25 import create_genuine_v25_database
from tests.runtime_epoch_support import (
    SYNTHETIC_CUTOFF_AT,
    SYNTHETIC_EPOCH_ID,
    SYNTHETIC_GENERATION_BINDING,
    SYNTHETIC_IDENTITY,
    SYNTHETIC_RELEASE_SHA,
    grant_synthetic_origin,
    seed_legacy_attempt,
    seed_legacy_lifecycle,
    seed_legacy_public_event,
)
from tests.test_prospective_runtime_epoch_isolation import NOW, _settings

pytestmark = pytest.mark.no_auto_epoch

CUTOFF_EPOCH_ID = "cci-test-epoch-cutoff-20260301"
CUTOFF_AT = "2026-03-01T12:00:00Z"
CUTOFF_IDENTITY = RuntimeEpochIdentity(
    epoch_id=CUTOFF_EPOCH_ID,
    cutoff_at=CUTOFF_AT,
    contract_version=RUNTIME_EPOCH_CONTRACT_VERSION,
    reviewed_release_sha=SYNTHETIC_RELEASE_SHA,
    generation_binding="boundary-cutoff-proof",
)
PRE_EPOCH = datetime(2026, 3, 1, 11, 0, tzinfo=UTC)
POST_EPOCH = datetime(2026, 3, 1, 13, 0, tzinfo=UTC)
DECISION = datetime(2026, 3, 1, 12, 5, tzinfo=UTC)
PROCESS_NOW = "2026-03-01T13:30:00Z"
PROCESS_DT = datetime(2026, 3, 1, 13, 30, tzinfo=UTC)


def _candles(anchor: datetime) -> tuple[dict[str, object], dict[str, object]]:
    first = anchor - timedelta(minutes=5)
    return (
        {
            "timestamp": int(first.timestamp() * 1000),
            "open": Decimal("100"),
            "high": Decimal("101"),
            "low": Decimal("99"),
            "close": Decimal("100.5"),
            "volume": Decimal("10"),
        },
        {
            "timestamp": int(anchor.timestamp() * 1000),
            "open": Decimal("100.5"),
            "high": Decimal("102"),
            "low": Decimal("100"),
            "close": Decimal("101"),
            "volume": Decimal("11"),
        },
    )


def _live_adapter_delivery(*, acquired_at: datetime, symbol: str = "BTCUSDT"):
    candles = _candles(acquired_at)
    return observe_adapter_normalized_batch(
        candles,
        requested_symbol=symbol,
        requested_interval="5m",
        requested_limit=len(candles),
        effective_symbol=symbol,
        effective_interval="5m",
        effective_limit=len(candles),
        endpoint_path="/v5/market/kline",
        adapter_class_label="BoundaryTestAdapter",
        capture_clock=lambda: acquired_at,
        http_client_injected=True,
        base_url_label="test",
    )


def _bootstrap(path: Path, identity: RuntimeEpochIdentity = SYNTHETIC_IDENTITY) -> Path:
    with open_initialized_database(path) as connection:
        initialize_runtime_epoch(connection, identity, activated_at=identity.cutoff_at)
        connection.commit()
    return path


def _register_run(
    path: Path,
    run_id: str,
    registered_at: str = "2026-03-01T13:00:00Z",
) -> None:
    with open_initialized_database(path) as connection:
        register_operational_run(
            connection,
            run_id=run_id,
            registered_at=registered_at,
            epoch=require_active_runtime_epoch(connection),
        )
        connection.commit()


def _schema_snapshot(path: Path) -> dict[str, object]:
    stat = path.stat()
    with sqlite3.connect(path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        indexes = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        epoch_count = 0
        if "runtime_epochs" in tables:
            epoch_count = int(connection.execute("SELECT COUNT(*) FROM runtime_epochs").fetchone()[0])
    return {
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
        "version": version,
        "tables": tables,
        "indexes": indexes,
        "epoch_count": epoch_count,
    }


def _fresh_symbol(
    *,
    origin_kind: str = "live_scan",
    decision: datetime | None = DECISION,
    delivery=None,
    symbol: str = "BTCUSDT",
    signal_id: str = "boundary-fresh",
    evaluation_completed_at: datetime | None = None,
) -> ScannerSymbolResult:
    return _symbol(SetupLifecycleState.WATCHLISTED, signal_id=signal_id).model_copy(
        update={
            "symbol": symbol,
            "evaluation_origin_kind": origin_kind,
            "lifecycle_decision_timestamp": decision,
            "lifecycle_execution_batch_delivery": delivery,
            "evaluation_completed_at": evaluation_completed_at,
        }
    )


def _plan(
    symbol: str = "BTCUSDT",
    plan_id: str = "owned-plan",
    side: str = "long",
) -> PublicWatchlistPlanIdentity:
    return PublicWatchlistPlanIdentity(
        plan_id=plan_id,
        symbol=symbol,
        side=side,
        setup_family="sweep_bos",
        source_modes=("swing",),
        raw_entry_low="100",
        raw_entry_high="102",
        raw_invalidation="95",
        normalized_entry_low="100",
        normalized_entry_high="102",
        normalized_invalidation="95",
    )


def _owned_record(
    origin_id: str,
    *,
    lifecycle_id: str,
    symbol: str = "BTCUSDT",
    direction: str = "long",
) -> SetupLifecycleRecord:
    return SetupLifecycleRecord(
        lifecycle_id=lifecycle_id,
        symbol=symbol,
        mode="swing",
        direction=direction,
        current_state=SetupLifecycleState.ACTIONABLE_A_GRADE,
        first_seen_at=NOW,
        last_seen_at=NOW,
        last_transition_at=NOW,
        runtime_epoch_id=SYNTHETIC_EPOCH_ID,
        creation_origin_id=origin_id,
        is_current=True,
        entry_low="100",
        entry_high="102",
        stop_loss="95",
    )


def _seed_legacy_part(connection: sqlite3.Connection, *, event_id: int, event_key: str, state: str = PENDING) -> int:
    connection.execute(
        """
        INSERT INTO public_alert_delivery_parts (
            public_alert_event_id, event_key, part_index, part_count,
            payload_text, payload_hash, delivery_state, created_at, updated_at
        ) VALUES (?, ?, 1, 1, 'legacy', 'legacy-hash', ?, ?, ?)
        """,
        (event_id, event_key, state, NOW, NOW),
    )
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _granted_origin_count(connection: sqlite3.Connection) -> int:
    return int(
        connection.execute(
            "SELECT COUNT(*) FROM runtime_operational_origins WHERE status = 'granted'"
        ).fetchone()[0]
    )


def test_r01_missing_evidence_does_not_grant_live_fresh(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r01.db", CUTOFF_IDENTITY)
    _register_run(db_path, "r01-run")
    _register_run(db_path, "r01-live")
    missing = _fresh_symbol(origin_kind="unspecified", decision=None, delivery=None)
    assert origin_kind_from_symbol_result(missing) == "unspecified"
    assert decision_cutoff_from_symbol_result(missing) is None
    assert producer_acquisition_from_symbol_result(missing) is None
    apply_lifecycle_to_run_result(
        _run_result(missing),
        database_path=db_path,
        scan_run_id="r01-run",
        now=PROCESS_NOW,
        expected_identity=CUTOFF_IDENTITY,
    )
    live_without_times = _fresh_symbol(origin_kind="live_scan", decision=None, delivery=None, signal_id="r01-live")
    apply_lifecycle_to_run_result(
        _run_result(live_without_times),
        database_path=db_path,
        scan_run_id="r01-live",
        now=PROCESS_NOW,
        expected_identity=CUTOFF_IDENTITY,
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        assert _granted_origin_count(connection) == 0
        assert connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0] == 0
        blocked = connection.execute(
            "SELECT block_reason FROM runtime_operational_origins WHERE symbol = 'BTCUSDT' ORDER BY rowid"
        ).fetchall()
        reasons = " ".join(str(row["block_reason"] or "") for row in blocked)
        assert "evaluation_kind_not_live:unspecified" in reasons or "evaluation_completed_at_missing" in reasons


def test_r02_pre_epoch_adapter_post_epoch_cache_does_not_grant_live_fresh(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r02.db", CUTOFF_IDENTITY)
    _register_run(db_path, "r02-run")
    acquired = _live_adapter_delivery(acquired_at=PRE_EPOCH)
    cached = observe_cache_delivery(
        acquired.returned_sequence,
        delivery_kind=CACHE_KIND_HIT,
        capture_clock=lambda: POST_EPOCH,
        cache_bookkeeping_created_at=PRE_EPOCH.timestamp(),
        cache_expires_at=POST_EPOCH.timestamp() + 60,
        cache_enabled=True,
        upstream=acquired,
        upstream_unavailable_reason=None,
        symbol="BTCUSDT",
        interval="5m",
        limit=len(acquired.returned_sequence),
    )
    selected = observe_closed_subset(
        cached,
        cached.returned_sequence[:1],
        logical_cutoff=POST_EPOCH,
        capture_clock=lambda: POST_EPOCH,
    )
    result = _fresh_symbol(
        origin_kind="live_scan",
        decision=PROCESS_DT,
        delivery=selected,
        signal_id="r02-cache",
        evaluation_completed_at=PROCESS_DT,
    )
    producer = producer_acquisition_from_symbol_result(result)
    assert producer is not None
    assert producer.startswith("2026-03-01T11:00:00")
    apply_lifecycle_to_run_result(
        _run_result(result),
        database_path=db_path,
        scan_run_id="r02-run",
        now=PROCESS_NOW,
        expected_identity=CUTOFF_IDENTITY,
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        assert _granted_origin_count(connection) == 0
        origin = connection.execute(
            "SELECT status, block_reason FROM runtime_operational_origins WHERE symbol = 'BTCUSDT'"
        ).fetchone()
        assert origin is not None
        assert origin["status"] == "blocked"
        assert "producer_observation_not_after_epoch_cutoff" in str(origin["block_reason"])


def test_r03_fresh_post_epoch_producer_can_grant_origin(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r03.db", CUTOFF_IDENTITY)
    _register_run(db_path, "r03-run")
    delivery = _live_adapter_delivery(acquired_at=POST_EPOCH)
    result = _fresh_symbol(
        origin_kind="live_scan",
        decision=DECISION,
        delivery=delivery,
        signal_id="r03-fresh",
        evaluation_completed_at=PROCESS_DT,
    )
    apply_lifecycle_to_run_result(
        _run_result(result),
        database_path=db_path,
        scan_run_id="r03-run",
        now=PROCESS_NOW,
        expected_identity=CUTOFF_IDENTITY,
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        origin = connection.execute(
            "SELECT status, origin_kind, producer_observed_at FROM runtime_operational_origins WHERE symbol = 'BTCUSDT'"
        ).fetchone()
        assert origin is not None
        assert origin["status"] == "granted"
        assert origin["origin_kind"] == ORIGIN_KIND_LIVE_FRESH
        assert str(origin["producer_observed_at"]).startswith("2026-03-01T13:00:00")


def test_r04_lost_provenance_does_not_default_to_live() -> None:
    live = _fresh_symbol(
        origin_kind="live_scan",
        decision=DECISION,
        delivery=_live_adapter_delivery(acquired_at=POST_EPOCH),
    )
    payload = live.model_dump(mode="json")
    assert "evaluation_origin_kind" not in payload
    restored = ScannerSymbolResult.model_validate(payload)
    assert restored.evaluation_origin_kind == "unspecified"
    assert origin_kind_from_symbol_result(restored) == "unspecified"
    omitted = _symbol(SetupLifecycleState.WATCHLISTED, signal_id="r04-omitted")
    assert omitted.evaluation_origin_kind == "unspecified"


def test_r05_btc_origin_cannot_create_eth_lifecycle(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r05.db")
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        origin = grant_synthetic_origin(repository.connection, symbol="BTCUSDT", run_id="r05", now=NOW)
        eth = _owned_record(origin.origin_id, lifecycle_id="life-eth", symbol="ETHUSDT")
        with pytest.raises(RuntimeEpochOriginError, match="symbol"):
            repository.upsert_record(eth)
        assert repository.get_record_by_lifecycle_id("life-eth") is None


def test_r06_missing_lifecycle_cannot_create_public_intent(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r06.db")
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        with pytest.raises(RuntimeEpochOwnershipError, match="persisted originating lifecycle"):
            require_lifecycle_public_intent(repository._connection, origin_lifecycle_id="missing-life")
        event, created = _insert_public_alert_event(
            repository,
            plan=_plan(),
            event_key="r06:missing",
            event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="missing-life",
        )
        assert event is None
        assert created is False
        assert repository._connection.execute("SELECT COUNT(*) FROM public_alert_events").fetchone()[0] == 0


def test_r07_foreign_and_legacy_lifecycle_cannot_create_public_intent(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r07.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="life-legacy",
            symbol="BTCUSDT",
            current_state=SetupLifecycleState.ACTIONABLE_A_GRADE.value,
        )
        connection.commit()
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        with pytest.raises(RuntimeEpochOwnershipError):
            require_lifecycle_public_intent(repository._connection, origin_lifecycle_id="life-legacy")
        event, created = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="legacy-plan"),
            event_key="r07:legacy",
            event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-legacy",
        )
        assert event is None
        assert created is False
        assert repository._connection.execute("SELECT COUNT(*) FROM public_alert_events").fetchone()[0] == 0


def test_r08_owned_lifecycle_can_create_public_intent(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r08.db")
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id="r08", now=NOW)
        lifecycle.upsert_record(_owned_record(origin.origin_id, lifecycle_id="life-r08"))
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        event, created = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-r08-plan"),
            event_key="r08:owned",
            event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-r08",
        )
        assert created is True
        assert event is not None
        row = repository._connection.execute(
            "SELECT runtime_epoch_id, origin_lifecycle_id FROM public_alert_events WHERE event_key = ?",
            ("r08:owned",),
        ).fetchone()
        assert tuple(row) == (SYNTHETIC_EPOCH_ID, "life-r08")


def test_r09_forged_event_lifecycle_chain_is_rejected(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r09.db")
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id="r09", now=NOW)
        lifecycle.upsert_record(_owned_record(origin.origin_id, lifecycle_id="life-r09"))
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        event, created = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-r09-plan"),
            event_key="r09:owned",
            event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-r09",
        )
        assert created is True
        assert event is not None
        repository._connection.execute(
            "UPDATE public_alert_events SET origin_lifecycle_id = ? WHERE event_key = ?",
            ("missing-life", "r09:owned"),
        )
        forged = repository._connection.execute(
            "SELECT * FROM public_alert_events WHERE event_key = ?",
            ("r09:owned",),
        ).fetchone()
        assert forged["runtime_epoch_id"] == SYNTHETIC_EPOCH_ID
        decision = decide_public_effect(repository._connection, forged)
        assert decision.allowed is False
        assert decision.send is False
        assert decision.mutate is False
        before = dict(forged)
        claimed = SQLitePublicTelegramOutbox(repository._connection).claim(
            event_id=int(forged["id"]),
            reservation_id=1,
            now=NOW,
        )
        assert claimed.claim is None
        after = dict(
            repository._connection.execute(
                "SELECT * FROM public_alert_events WHERE event_key = ?",
                ("r09:owned",),
            ).fetchone()
        )
        assert after["delivery_state"] == before["delivery_state"]
        assert after["origin_lifecycle_id"] == "missing-life"


def test_r10_mark_part_in_flight_cannot_mutate_legacy_part(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r10.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        event_id = seed_legacy_public_event(
            connection,
            event_key="r10:legacy",
            status="RESERVED",
            delivery_state=PENDING,
        )
        part_id = _seed_legacy_part(connection, event_id=event_id, event_key="r10:legacy")
        connection.commit()
        before = dict(
            connection.execute("SELECT * FROM public_alert_delivery_parts WHERE id = ?", (part_id,)).fetchone()
        )
        mutated = SQLitePublicTelegramOutbox(connection).mark_part_in_flight(
            part_id=part_id, attempt_id="forged", now=NOW
        )
        after = dict(
            connection.execute("SELECT * FROM public_alert_delivery_parts WHERE id = ?", (part_id,)).fetchone()
        )
        assert mutated is False
        assert after == before


def test_r11_watchlist_reservation_cannot_mutate_legacy_attempt(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r11.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_legacy_public_event(connection, event_key="r11:legacy", status="RESERVED", delivery_state=PENDING)
        attempt_id = seed_legacy_attempt(
            connection,
            signal_id="r11-legacy",
            event_key="r11:legacy",
            telegram_status="pending",
            delivery_state=PENDING,
        )
        connection.commit()
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        before = dict(
            repository._connection.execute(
                "SELECT * FROM telegram_alert_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        )
        mutated = repository.mark_public_watchlist_reservation_result(
            attempt_id=attempt_id,
            status="sent",
            sent_at=NOW,
            message_hash="forged",
            error_message="N/A",
            dedupe_status="N/A",
            dedupe_reason="N/A",
        )
        after = dict(
            repository._connection.execute(
                "SELECT * FROM telegram_alert_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        )
        assert mutated is False
        assert after == before


def test_r12_replace_attempt_cannot_reown_legacy_attempt(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r12.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_legacy_public_event(connection, event_key="r12:legacy", status="RESERVED", delivery_state=PENDING)
        attempt_id = seed_legacy_attempt(
            connection,
            signal_id="r12-legacy",
            event_key="r12:legacy",
            telegram_status="pending",
            delivery_state=PENDING,
        )
        connection.commit()
    replacement = TelegramAlertAttemptRecord(
        signal_id="forged-owned",
        symbol="BTCUSDT",
        direction="long",
        previous_state="WATCHLISTED",
        new_state="WATCHLISTED",
        alert_type="WATCHLIST",
        lifecycle_state="WATCHLISTED",
        sent_at=None,
        telegram_status="pending",
        message_hash="forged",
        public_watchlist_event_key="r12:owned",
        attempted_at=NOW,
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        before = dict(
            repository._connection.execute(
                "SELECT * FROM telegram_alert_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        )
        mutated = repository.replace_attempt_with_reservation(attempt_id=attempt_id, record=replacement)
        after = dict(
            repository._connection.execute(
                "SELECT * FROM telegram_alert_attempts WHERE id = ?",
                (attempt_id,),
            ).fetchone()
        )
        assert mutated is False
        assert after == before


def test_r13_remaining_direct_delivery_mutations_are_guarded(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r13.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        event_id = seed_legacy_public_event(
            connection,
            event_key="r13:legacy",
            status="RESERVED",
            delivery_state=PENDING,
        )
        part_id = _seed_legacy_part(connection, event_id=event_id, event_key="r13:legacy")
        attempt_id = seed_legacy_attempt(
            connection,
            signal_id="r13-legacy",
            event_key="r13:legacy",
            telegram_status="pending",
            delivery_state=PENDING,
        )
        connection.commit()
        event_before = dict(
            connection.execute("SELECT * FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        )
        part_before = dict(
            connection.execute("SELECT * FROM public_alert_delivery_parts WHERE id = ?", (part_id,)).fetchone()
        )
        attempt_before = dict(
            connection.execute("SELECT * FROM telegram_alert_attempts WHERE id = ?", (attempt_id,)).fetchone()
        )
        outbox = SQLitePublicTelegramOutbox(connection)
        assert outbox.recover_stale_in_flight(event_id=event_id, now=NOW) is False
        claimed = outbox.claim(event_id=event_id, reservation_id=attempt_id, now=NOW)
        assert claimed.claim is None
        with pytest.raises(RuntimeEpochOwnershipError):
            persist_intent_parts(
                connection,
                event_id=event_id,
                event_key="r13:legacy",
                message_text="forged",
                message_hash="forged",
            )
        result_state = outbox.record_part_result(
            event_id=event_id,
            reservation_id=attempt_id,
            part_id=part_id,
            attempt_id="forged",
            result={"delivery_state": SENT, "message_id": 1},
            now=NOW,
        )
        assert result_state != SENT
        outbox.mark_terminal_without_send(
            event_id=event_id,
            reservation_id=attempt_id,
            state="FAILED_FINAL",
            reason="forged",
            now=NOW,
        )
        outbox.mark_uncertain_after_persistence_failure(
            event_id=event_id,
            reservation_id=attempt_id,
            detail="forged",
            now=NOW,
        )
        event_after = dict(
            connection.execute("SELECT * FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        )
        part_after = dict(
            connection.execute("SELECT * FROM public_alert_delivery_parts WHERE id = ?", (part_id,)).fetchone()
        )
        attempt_after = dict(
            connection.execute("SELECT * FROM telegram_alert_attempts WHERE id = ?", (attempt_id,)).fetchone()
        )
        assert event_after == event_before
        assert part_after == part_before
        assert attempt_after == attempt_before
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        inserted = repository.insert_attempt(
            TelegramAlertAttemptRecord(
                signal_id="r13-insert",
                symbol="BTCUSDT",
                direction="long",
                previous_state="WATCHLISTED",
                new_state="WATCHLISTED",
                alert_type="WATCHLIST",
                lifecycle_state="WATCHLISTED",
                sent_at=None,
                telegram_status="pending",
                message_hash="forged",
                public_watchlist_event_key="r13:legacy",
                attempted_at=NOW,
            )
        )
        compacted = repository.compact_repeated_attempt(
            TelegramAlertAttemptRecord(
                signal_id="r13-legacy",
                symbol="BTCUSDT",
                direction="long",
                previous_state="WATCHLISTED",
                new_state="WATCHLISTED",
                alert_type="WATCHLIST",
                lifecycle_state="WATCHLISTED",
                sent_at=None,
                telegram_status="blocked",
                message_hash="legacy-hash",
                public_watchlist_event_key="r13:legacy",
                blocked_reason="x",
                error_message="x",
                last_seen_at=NOW,
            )
        )
        assert inserted is False
        assert compacted is False
        assert (
            repository._connection.execute(
                "SELECT COUNT(*) FROM telegram_alert_attempts WHERE signal_id = 'r13-insert'"
            ).fetchone()[0]
            == 0
        )


def test_r14_operational_open_does_not_migrate_genuine_v25(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = create_genuine_v25_database(tmp_path / "r14.db")
    before = _schema_snapshot(db_path)
    monkeypatch.setenv("RUNTIME_EPOCH_ID", SYNTHETIC_EPOCH_ID)
    with pytest.raises(RuntimeEpochError):
        inspect_operational_database(db_path, expected_identity=SYNTHETIC_IDENTITY)
    with pytest.raises(RuntimeEpochError):
        open_repository_database(db_path, expected_identity=SYNTHETIC_IDENTITY)
    with pytest.raises(RuntimeEpochError):
        require_operational_runtime(database_path=db_path, expected_identity=SYNTHETIC_IDENTITY)
    with pytest.raises(RuntimeEpochError):
        with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY):
            pass
    with pytest.raises(RuntimeEpochError):
        run(
            TelegramLifecycleDeliveryService(
                database_path=db_path,
                settings=_settings(),
                sender=FakeSender(),
                expected_identity=SYNTHETIC_IDENTITY,
            ).deliver_for_run(_run_result(_public_v1_symbol()), scan_run_id="r14")
        )
    after = _schema_snapshot(db_path)
    assert after["version"] == 25
    assert after["version"] == before["version"]
    assert after["epoch_count"] == 0
    assert "runtime_epochs" not in after["tables"]


def test_r15_operational_open_does_not_create_missing_db(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "missing.db"
    monkeypatch.setenv("RUNTIME_EPOCH_ID", SYNTHETIC_EPOCH_ID)
    with pytest.raises(RuntimeEpochConfigurationError):
        inspect_operational_database(db_path, expected_identity=SYNTHETIC_IDENTITY)
    with pytest.raises(RuntimeEpochConfigurationError):
        open_repository_database(db_path, expected_identity=SYNTHETIC_IDENTITY)
    with pytest.raises(RuntimeEpochError):
        with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY):
            pass
    assert not db_path.exists()


def test_r16_v26_without_epoch_fails_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "r16.db"
    with open_initialized_database(db_path) as connection:
        assert identify_schema_version(connection) == SCHEMA_VERSION
        assert load_active_runtime_epoch(connection) is None
    before = _schema_snapshot(db_path)
    assert before["version"] == 26
    monkeypatch.setenv("RUNTIME_EPOCH_ID", SYNTHETIC_EPOCH_ID)
    with pytest.raises(RuntimeEpochError):
        inspect_operational_database(db_path, expected_identity=SYNTHETIC_IDENTITY)
    with pytest.raises(RuntimeEpochError):
        open_repository_database(db_path, expected_identity=SYNTHETIC_IDENTITY)
    after = _schema_snapshot(db_path)
    assert after["version"] == 26
    assert after["epoch_count"] == 0


def test_r17_wrong_expected_epoch_fails_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _bootstrap(tmp_path / "r17.db")
    before = _schema_snapshot(db_path)
    monkeypatch.setenv("RUNTIME_EPOCH_ID", "epoch-other")
    other_identity = RuntimeEpochIdentity(
        epoch_id="epoch-other",
        cutoff_at=SYNTHETIC_CUTOFF_AT,
        contract_version=RUNTIME_EPOCH_CONTRACT_VERSION,
        reviewed_release_sha=SYNTHETIC_RELEASE_SHA,
        generation_binding=SYNTHETIC_GENERATION_BINDING,
    )
    with pytest.raises(RuntimeEpochError):
        inspect_operational_database(db_path, expected_identity=other_identity)
    with pytest.raises(RuntimeEpochError):
        open_repository_database(db_path, expected_identity=other_identity)
    after = _schema_snapshot(db_path)
    assert after["epoch_count"] == before["epoch_count"]
    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT epoch_id FROM runtime_epochs").fetchone()
        assert row[0] == SYNTHETIC_EPOCH_ID


def test_r18_wrong_db_and_unsupported_schema_fail_before_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    selected = _bootstrap(tmp_path / "selected.db")
    other = _bootstrap(tmp_path / "other.db")
    monkeypatch.setenv("RUNTIME_EPOCH_ID", SYNTHETIC_EPOCH_ID)
    foreign_identity = RuntimeEpochIdentity(
        epoch_id="not-the-selected-epoch",
        cutoff_at=SYNTHETIC_CUTOFF_AT,
        contract_version=RUNTIME_EPOCH_CONTRACT_VERSION,
        reviewed_release_sha=SYNTHETIC_RELEASE_SHA,
        generation_binding=SYNTHETIC_GENERATION_BINDING,
    )
    with pytest.raises(RuntimeEpochError):
        inspect_operational_database(other, expected_identity=foreign_identity)
    unsupported = tmp_path / "unsupported.db"
    with sqlite3.connect(unsupported) as connection:
        connection.execute("CREATE TABLE dummy (id INTEGER)")
        connection.execute("PRAGMA user_version = 24")
        connection.commit()
    before = _schema_snapshot(unsupported)
    with pytest.raises(RuntimeEpochError):
        inspect_operational_database(unsupported, expected_identity=SYNTHETIC_IDENTITY)
    with pytest.raises(RuntimeEpochError):
        open_repository_database(unsupported, expected_identity=SYNTHETIC_IDENTITY)
    after = _schema_snapshot(unsupported)
    assert after["version"] == 24
    assert after["version"] == before["version"]
    future = tmp_path / "future.db"
    with sqlite3.connect(future) as connection:
        connection.execute("CREATE TABLE dummy (id INTEGER)")
        connection.execute("PRAGMA user_version = 99")
        connection.commit()
    with pytest.raises((RuntimeEpochError, UnsupportedSchemaVersionError)):
        inspect_operational_database(future, expected_identity=SYNTHETIC_IDENTITY)
    assert selected.exists()


def test_r19_untagged_watch_payload_fails_closed(tmp_path: Path) -> None:
    base_dir = tmp_path / "scan_runs"
    path = operational_watch_state_path(SYNTHETIC_EPOCH_ID, base_dir=base_dir)
    original = b'{"runtime_epoch_id": null, "symbols": {"BTCUSDT": {"ready": true}}}'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)
    with pytest.raises(RuntimeEpochConfigurationError):
        require_operational_watch_payload(json.loads(original.decode("utf-8")), SYNTHETIC_EPOCH_ID)
    with pytest.raises(RuntimeEpochConfigurationError):
        load_or_initialize_operational_watch_payload(path, SYNTHETIC_EPOCH_ID, base_dir=base_dir)
    assert path.read_bytes() == original


def test_r20_mismatched_watch_epoch_fails_closed(tmp_path: Path) -> None:
    base_dir = tmp_path / "scan_runs"
    path = operational_watch_state_path(SYNTHETIC_EPOCH_ID, base_dir=base_dir)
    original = json.dumps({"runtime_epoch_id": "epoch-other", "symbols": {}}).encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(original)
    with pytest.raises(RuntimeEpochConfigurationError):
        load_or_initialize_operational_watch_payload(path, SYNTHETIC_EPOCH_ID, base_dir=base_dir)
    assert path.read_bytes() == original


def test_r21_missing_watch_file_creates_empty_current_epoch_state(tmp_path: Path) -> None:
    base_dir = tmp_path / "scan_runs"
    legacy = canonical_legacy_watch_state_path(base_dir)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(
        json.dumps({"runtime_epoch_id": None, "symbols": {"ETHUSDT": {"mode": "swing"}}}),
        encoding="utf-8",
    )
    path = operational_watch_state_path(SYNTHETIC_EPOCH_ID, base_dir=base_dir)
    payload = load_or_initialize_operational_watch_payload(path, SYNTHETIC_EPOCH_ID, base_dir=base_dir)
    assert payload["runtime_epoch_id"] == SYNTHETIC_EPOCH_ID
    assert "ETHUSDT" not in payload.get("symbols", {})
    assert json.loads(legacy.read_text(encoding="utf-8"))["symbols"]["ETHUSDT"]["mode"] == "swing"


def test_r22_research_watch_cannot_bypass_operational_ownership(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r22.db")
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_research_settings(),
        sender=sender,
        expected_identity=SYNTHETIC_IDENTITY,
    )
    summary = run(service.deliver_for_run(_run_result(_research_symbol()), scan_run_id="r22"))
    assert sender.messages == []
    assert sender.calls == []
    assert summary.sent == 0
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT telegram_status, blocked_reason FROM telegram_alert_attempts"
        ).fetchall()
    assert rows
    assert all(status != "sent" for status, _reason in rows)
    assert any("research_watch_operational_ownership_required" in str(reason) for _status, reason in rows)


def test_r23_active_signal_detail_excludes_legacy_state(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r23.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_legacy_attempt(
            connection,
            signal_id="legacy-confirmed-signal",
            event_key="r23:legacy",
            telegram_status="sent",
            delivery_state=SENT,
        )
        connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET alert_type = 'SIGNAL_CONFIRMED', attempted_alert_type = 'SIGNAL_CONFIRMED',
                new_state = 'CONFIRMED', lifecycle_state = 'CONFIRMED'
            WHERE signal_id = 'legacy-confirmed-signal'
            """
        )
        connection.commit()
    result = load_active_signal_detail(
        project_root=tmp_path,
        database_path=db_path,
        selector="BTCUSDT",
    )
    assert result.detail is None


def test_r24_admin_draft_operational_send_requires_ownership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = _bootstrap(tmp_path / "r24.db")
    monkeypatch.setenv("RUNTIME_EPOCH_ID", SYNTHETIC_EPOCH_ID)
    result = _admin_run_result((_valid_result(),))
    drafts = build_admin_drafts(
        result,
        delivery_status="dry_run",
        created_at=NOW,
        database_path=db_path,
    )
    assert "valid_setup" not in {draft.draft_type for draft in drafts}
    report = format_admin_scan_report(result, database_path=db_path)
    assert "VALIDUSDT" not in report.split("Valid Setups", 1)[1].split("Near Misses", 1)[0]


def test_r25_genuine_v25_migration_preserves_legacy_null_membership(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_prospective_runtime_epoch_isolation import test_t20_v25_upgrade_preserves_rows_and_rolls_back_failure

    test_t20_v25_upgrade_preserves_rows_and_rolls_back_failure(tmp_path, monkeypatch)


def test_r26_injected_v26_failure_preserves_pre_operational_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.test_prospective_runtime_epoch_isolation import test_t20_v25_upgrade_preserves_rows_and_rolls_back_failure

    test_t20_v25_upgrade_preserves_rows_and_rolls_back_failure(tmp_path, monkeypatch)


def _seed_owned_root_event(
    connection: sqlite3.Connection,
    *,
    lifecycle_id: str,
    symbol: str = "BTCUSDT",
    side: str = "long",
    plan_id: str | None = None,
) -> int:
    plan = plan_id if plan_id is not None else "N/A"
    event_key = f"{lifecycle_id}|initial_watchlist|{side.lower()}"
    existing = connection.execute(
        "SELECT id FROM public_alert_events WHERE event_key = ?",
        (event_key,),
    ).fetchone()
    if existing is not None:
        return int(existing[0])
    connection.execute(
        """
        INSERT INTO public_alert_events (
            canonical_plan_id, event_type, event_key, symbol, side, setup_family,
            status, reserved_at, created_at, updated_at, runtime_epoch_id, origin_lifecycle_id
        ) VALUES (?, 'initial_watchlist', ?, ?, ?, 'N/A', 'RESERVED', ?, ?, ?, ?, ?)
        """,
        (plan, event_key, symbol.upper(), side.lower(), NOW, NOW, NOW, SYNTHETIC_EPOCH_ID, lifecycle_id),
    )
    return int(connection.execute("SELECT last_insert_rowid()").fetchone()[0])


def _attempt_record(
    *,
    signal_id: str,
    event_key: str = "N/A",
    status: str = "pending",
    delivery_state: str = "N/A",
    blocked_reason: str = "N/A",
    alert_type: str = "WATCHLIST",
    symbol: str = "BTCUSDT",
    direction: str = "long",
) -> TelegramAlertAttemptRecord:
    return TelegramAlertAttemptRecord(
        signal_id=signal_id,
        symbol=symbol,
        direction=direction,
        previous_state="WATCHLISTED",
        new_state="WATCHLISTED",
        alert_type=alert_type,
        lifecycle_state="WATCHLISTED",
        sent_at=None,
        telegram_status=status,
        message_hash="hash",
        public_watchlist_event_key=event_key,
        attempted_at=NOW,
        delivery_state=delivery_state,
        blocked_reason=blocked_reason,
        error_message=blocked_reason,
    )


def test_r27_owned_fresh_path_can_claim_outbox_and_fake_send(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r27.db")
    delivery = _live_adapter_delivery(acquired_at=datetime.fromisoformat(NOW.replace("Z", "+00:00")))
    producer = producer_acquisition_from_symbol_result(
        _fresh_symbol(decision=datetime.fromisoformat(NOW.replace("Z", "+00:00")), delivery=delivery)
    )
    with open_initialized_database(db_path) as connection:
        register_operational_run(connection, run_id="r27-origin", registered_at=NOW)
        origin = evaluate_symbol_origin(
            connection,
            run_id="r27-origin",
            symbol="BTCUSDT",
            evaluation_kind="live_scan",
            evaluation_completed_at=NOW,
            decision_cutoff_at=NOW,
            producer_observed_at=producer,
        )
        assert origin.granted is True
        connection.commit()

    def _owned(record: SetupLifecycleRecord) -> SetupLifecycleRecord:
        return record.model_copy(
            update={
                "runtime_epoch_id": SYNTHETIC_EPOCH_ID,
                "creation_origin_id": origin.origin_id,
            }
        )

    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        record = _owned(_outcome_record(lifecycle_id="fresh-progress", mode="swing"))
        repository.upsert_record(record)
        candles = [_outcome_baseline("long")]
        primed = _evaluate_outcomes(repository, record, candles)
        owned_primed = _owned(primed.record)
        repository.upsert_record(owned_primed)
        candles.append(_outcome_entry(1, "long"))
        outcome = _evaluate_outcomes(repository, owned_primed, candles)
        owned_record = _owned(outcome.record)
        repository.upsert_record(owned_record)
        stamped_transitions = tuple(
            item.model_copy(update={"record": _owned(item.record)}) if item.record is not None else item
            for item in outcome.transitions
        )
        last_transition = stamped_transitions[-1] if stamped_transitions else outcome.last_transition
        if last_transition is not None and last_transition.record is not None:
            last_transition = last_transition.model_copy(update={"record": _owned(last_transition.record)})
        _seed_owned_root_event(repository.connection, lifecycle_id="fresh-progress")
    batch = _symbol(
        SetupLifecycleState.MANAGING,
        previous=SetupLifecycleState.EXECUTING,
        signal_id="fresh-progress",
    ).model_copy(
        update={
            "lifecycle_state": owned_record,
            "lifecycle_transition": last_transition,
            "lifecycle_transitions": stamped_transitions,
            "lifecycle_outcome_progress": outcome.progress,
        }
    )
    sender = FakeSender()
    summary = run(
        TelegramLifecycleDeliveryService(
            database_path=db_path,
            settings=_settings(),
            sender=sender,
            min_rr=Decimal("3"),
            min_score_for_idea=Decimal("88"),
            expected_identity=SYNTHETIC_IDENTITY,
        ).deliver_for_run(_run_result(batch), scan_run_id="r27")
    )
    assert summary.sent == 1
    assert sender.messages
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        event = connection.execute(
            "SELECT runtime_epoch_id, origin_lifecycle_id FROM public_alert_events WHERE runtime_epoch_id IS NOT NULL"
        ).fetchone()
    assert event is not None
    assert event["runtime_epoch_id"] == SYNTHETIC_EPOCH_ID
    assert event["origin_lifecycle_id"] == "fresh-progress"


def test_r28_global_legacy_sent_event_key_remains_consumed(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r28.db")
    event_key = "global:consumed"
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_legacy_public_event(connection, event_key=event_key, status="SENT", delivery_state=SENT)
        connection.commit()
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id="r28", now=NOW)
        lifecycle.upsert_record(_owned_record(origin.origin_id, lifecycle_id="life-r28"))
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        event, created = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-r28-plan"),
            event_key=event_key,
            event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-r28",
        )
        assert created is False
        assert event is not None
        assert event.status == "SENT"
        count = repository._connection.execute(
            "SELECT COUNT(*) FROM public_alert_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()[0]
        assert count == 1


def test_r29_legacy_uncertain_remains_unchanged_and_non_auto_retryable(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r29.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        event_id = seed_legacy_public_event(
            connection,
            event_key="r29:uncertain",
            status="RESERVED",
            delivery_state=UNCERTAIN,
        )
        part_id = _seed_legacy_part(
            connection,
            event_id=event_id,
            event_key="r29:uncertain",
            state=UNCERTAIN,
        )
        attempt_id = seed_legacy_attempt(
            connection,
            signal_id="r29-uncertain",
            event_key="r29:uncertain",
            telegram_status="uncertain",
            delivery_state=UNCERTAIN,
        )
        connection.commit()
        event_before = dict(
            connection.execute("SELECT * FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        )
        part_before = dict(
            connection.execute("SELECT * FROM public_alert_delivery_parts WHERE id = ?", (part_id,)).fetchone()
        )
        outbox = SQLitePublicTelegramOutbox(connection)
        claimed = outbox.claim(event_id=event_id, reservation_id=attempt_id, now=NOW)
        mutated = outbox.mark_part_in_flight(part_id=part_id, attempt_id="retry", now=NOW)
        recovered = outbox.recover_stale_in_flight(event_id=event_id, now=NOW)
        event_after = dict(
            connection.execute("SELECT * FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        )
        part_after = dict(
            connection.execute("SELECT * FROM public_alert_delivery_parts WHERE id = ?", (part_id,)).fetchone()
        )
        assert claimed.claim is None
        assert "uncertain" in claimed.state.lower() or claimed.reason != "N/A"
        assert mutated is False
        assert recovered is False
        assert event_after == event_before
        assert part_after == part_before
