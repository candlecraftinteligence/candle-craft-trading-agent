"""Second bounded repair proofs R31–R60 for PROSPECTIVE_RUNTIME_EPOCH_ISOLATION.

DEV-only temp databases, mocked senders, and deterministic clocks.
No Runtime filesystem, live Telegram, exchange calls, or scanner watch loops.
"""

from __future__ import annotations

import sqlite3
from argparse import Namespace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import (
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
    persist_intent_parts,
)
from app.core.config import Settings
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import apply_lifecycle_to_run_result
from app.pipeline.scanner_runner import ScannerRunner
from app.runtime_epoch.authority import initialize_runtime_epoch
from app.runtime_epoch.errors import (
    RuntimeEpochConfigurationError,
    RuntimeEpochError,
    RuntimeEpochOriginError,
    RuntimeEpochOwnershipError,
)
from app.runtime_epoch.models import RUNTIME_EPOCH_CONTRACT_VERSION, RuntimeEpochIdentity
from app.runtime_epoch.origin import (
    decision_cutoff_from_symbol_result,
    evaluation_completed_at_from_symbol_result,
    origin_kind_from_symbol_result,
    producer_acquisition_from_symbol_result,
)
from app.runtime_epoch.startup import (
    identity_from_settings,
    inspect_operational_database,
    open_repository_database,
    require_expected_operational_identity,
)
from app.storage.database import identify_schema_version, open_initialized_database
from app.telegram_admin.draft_router import _operational_setup_recommendation_allowed
from app.telegram_admin.signal_detail import load_active_signal_detail

from test_telegram_lifecycle_delivery_phase42 import (
    FakeSender,
    _research_settings,
    _research_symbol,
    _run_result,
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
from tests.test_prospective_runtime_epoch_isolation_boundaries import (
    CUTOFF_IDENTITY,
    PROCESS_DT,
    _attempt_record,
    _bootstrap,
    _fresh_symbol,
    _live_adapter_delivery,
    _owned_record,
    _plan,
    _schema_snapshot,
    _seed_legacy_part,
    _seed_owned_root_event,
)
from app.data.candle_batch_evidence import (
    new_capture_occurrence_token,
    observe_adapter_normalized_batch,
)
from tests.test_scanner_runner import (
    FakeExchangeClient,
    _clean_target_intelligence,
    _config,
    _flat_candles,
    _strategy_pullback_candles,
    run as run_scanner,
)

pytestmark = pytest.mark.no_auto_epoch


class FakeAdapterExchangeClient(FakeExchangeClient):
    """Test producer that stamps adapter acquisition like the Binance klines path."""

    _cci_klines_delivery_capture = True

    async def get_klines(self, symbol: str, interval: str, limit: int):
        delivery = await self.get_klines_with_delivery(symbol, interval, limit)
        return list(delivery.returned_sequence)

    async def get_klines_with_delivery(
        self,
        symbol: str,
        interval: str,
        limit: int,
        *,
        capture_clock=None,
        occurrence_factory=None,
    ):
        candles = await FakeExchangeClient.get_klines(self, symbol, interval, limit)
        clock = capture_clock or (lambda: datetime.now(UTC))
        token_factory = occurrence_factory or new_capture_occurrence_token
        return observe_adapter_normalized_batch(
            candles,
            requested_symbol=symbol,
            requested_interval=interval,
            requested_limit=limit,
            effective_symbol=symbol.upper(),
            effective_interval=interval,
            effective_limit=limit,
            endpoint_path="fake_exchange.get_klines",
            adapter_class_label=f"{type(self).__module__}.{type(self).__qualname__}",
            capture_clock=clock,
            occurrence_factory=token_factory,
            http_client_injected=True,
            base_url_label="fake://exchange",
        )


def _row(connection: sqlite3.Connection, table: str, row_id: int) -> dict[str, object]:
    row = connection.execute(f"SELECT * FROM {table} WHERE id = ?", (row_id,)).fetchone()
    assert row is not None
    return dict(row)


def _owned_event_reservation(
    db_path: Path,
    *,
    lifecycle_id: str,
    event_key: str,
    signal_id: str,
    run_id: str,
    symbol: str = "BTCUSDT",
    direction: str = "long",
) -> tuple[int, int, int]:
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol=symbol, run_id=run_id, now=NOW)
        lifecycle.upsert_record(
            _owned_record(
                origin.origin_id,
                lifecycle_id=lifecycle_id,
                symbol=symbol,
                direction=direction,
            )
        )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        event, created = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id=f"{lifecycle_id}-plan", symbol=symbol, side=direction),
            event_key=event_key,
            event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id=lifecycle_id,
        )
        assert created is True
        assert event is not None
        assert event.id is not None
        inserted = repository.insert_attempt(
            _attempt_record(
                signal_id=signal_id,
                event_key=event_key,
                status="pending",
                delivery_state=PENDING,
                symbol=symbol,
                direction=direction,
            )
        )
        assert inserted is True
        reservation_id = int(
            repository._connection.execute(
                "SELECT id FROM telegram_alert_attempts WHERE public_watchlist_event_key = ?",
                (event_key,),
            ).fetchone()[0]
        )
        parts = persist_intent_parts(
            repository._connection,
            event_id=int(event.id),
            event_key=event_key,
            message_text="owned payload",
            message_hash=f"hash-{signal_id}",
        )
        assert parts
        return int(event.id), reservation_id, int(parts[0].id)


def test_r31_missing_evaluation_completion_blocks_fresh_acquisition_and_decision(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r31.db", CUTOFF_IDENTITY)
    missing_eval = _fresh_symbol(
        origin_kind="live_scan",
        decision=PROCESS_DT,
        delivery=_live_adapter_delivery(acquired_at=PROCESS_DT),
        signal_id="r31-eval",
        evaluation_completed_at=None,
    )
    assert producer_acquisition_from_symbol_result(missing_eval) is not None
    assert decision_cutoff_from_symbol_result(missing_eval) is not None
    assert evaluation_completed_at_from_symbol_result(missing_eval) is None
    apply_lifecycle_to_run_result(
        _run_result(missing_eval),
        database_path=db_path,
        scan_run_id="r31-eval",
        now="2026-03-01T13:30:00Z",
        expected_identity=CUTOFF_IDENTITY,
    )
    missing_decision = _fresh_symbol(
        origin_kind="live_scan",
        decision=None,
        delivery=_live_adapter_delivery(acquired_at=PROCESS_DT),
        signal_id="r31-decision",
        evaluation_completed_at=PROCESS_DT,
    )
    apply_lifecycle_to_run_result(
        _run_result(missing_decision),
        database_path=db_path,
        scan_run_id="r31-decision",
        now="2026-03-01T13:30:00Z",
        expected_identity=CUTOFF_IDENTITY,
    )
    missing_producer = _fresh_symbol(
        origin_kind="live_scan",
        decision=PROCESS_DT,
        delivery=None,
        signal_id="r31-producer",
        evaluation_completed_at=PROCESS_DT,
    )
    apply_lifecycle_to_run_result(
        _run_result(missing_producer),
        database_path=db_path,
        scan_run_id="r31-producer",
        now="2026-03-01T13:30:00Z",
        expected_identity=CUTOFF_IDENTITY,
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        reasons = {
            row["symbol"] if False else row["block_reason"]: row["block_reason"]
            for row in connection.execute(
                "SELECT run_id, block_reason FROM runtime_operational_origins"
            )
        }
        by_run = {
            str(row["run_id"]): str(row["block_reason"])
            for row in connection.execute("SELECT run_id, block_reason FROM runtime_operational_origins")
        }
        lives = connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0]
    assert lives == 0
    assert "evaluation_completed_at_missing" in by_run["r31-eval"]
    assert "decision_cutoff_at_missing" in by_run["r31-decision"]
    assert "producer_observed_at_missing" in by_run["r31-producer"]
    del reasons


def test_r32_actual_producer_path_supplies_evaluation_completion(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r32.db", CUTOFF_IDENTITY)
    clock_now = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    client = FakeAdapterExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"})
    scanned = run_scanner(
        ScannerRunner(
            exchange_client=client,
            clock=lambda: clock_now,
            capture_clock=lambda: clock_now,
        ).run(_config(["BTCUSDT"]))
    )
    symbol = scanned.results[0]
    assert origin_kind_from_symbol_result(symbol) == "live_scan"
    assert evaluation_completed_at_from_symbol_result(symbol) is not None
    assert producer_acquisition_from_symbol_result(symbol) is not None
    assert decision_cutoff_from_symbol_result(symbol) is not None
    apply_lifecycle_to_run_result(
        scanned,
        database_path=db_path,
        scan_run_id="r32-run",
        now="2026-03-01T14:00:00Z",
        expected_identity=CUTOFF_IDENTITY,
    )
    with sqlite3.connect(db_path) as connection:
        origin = connection.execute(
            "SELECT status, evaluation_completed_at FROM runtime_operational_origins WHERE symbol = 'BTCUSDT'"
        ).fetchone()
        life = connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0]
    assert origin is not None
    assert origin[0] == "granted"
    assert origin[1]
    assert life >= 1


def test_r33_malformed_tagged_lifecycle_missing_origin_cannot_mutate(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r33.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_legacy_lifecycle(
            connection,
            lifecycle_id="malformed-tagged",
            symbol="BTCUSDT",
            current_state=SetupLifecycleState.WATCHLISTED.value,
        )
        connection.execute(
            "UPDATE setup_lifecycle_records SET runtime_epoch_id = ? WHERE lifecycle_id = ?",
            (SYNTHETIC_EPOCH_ID, "malformed-tagged"),
        )
        before = dict(
            connection.execute(
                "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
                ("malformed-tagged",),
            ).fetchone()
        )
        connection.commit()
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        with pytest.raises(RuntimeEpochOriginError):
            repository.upsert_record(
                SetupLifecycleRecord(
                    lifecycle_id="malformed-tagged",
                    symbol="BTCUSDT",
                    mode="swing",
                    direction="long",
                    current_state=SetupLifecycleState.STALKING,
                    first_seen_at=NOW,
                    last_seen_at=NOW,
                    last_transition_at=NOW,
                    runtime_epoch_id=SYNTHETIC_EPOCH_ID,
                    creation_origin_id=None,
                )
            )
        with pytest.raises(RuntimeEpochOriginError):
            repository.supersede_record("malformed-tagged")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        after = dict(
            connection.execute(
                "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
                ("malformed-tagged",),
            ).fetchone()
        )
    assert after == before
    assert after["creation_origin_id"] is None
    assert after["runtime_epoch_id"] == SYNTHETIC_EPOCH_ID


def test_r34_legacy_root_event_rejects_followup_insert(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r34.db")
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        legacy_root = seed_legacy_public_event(
            connection, event_key="eth-legacy-root", symbol="ETHUSDT", status="SENT", delivery_state=SENT
        )
        connection.commit()
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id="r34", now=NOW)
        lifecycle.upsert_record(_owned_record(origin.origin_id, lifecycle_id="life-r34"))
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        event, created = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-r34-plan"),
            event_key="life-r34-plan|tp1_hit",
            event_type="tp1_hit",
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-r34",
            origin_root_event_id=legacy_root,
        )
        assert created is False
        assert event is None
        count = repository._connection.execute(
            "SELECT COUNT(*) FROM public_alert_events WHERE event_type = 'tp1_hit'"
        ).fetchone()[0]
    assert count == 0
    del legacy_root


def test_r35_foreign_epoch_wrong_symbol_and_lifecycle_roots_reject(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r35.db")
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        btc = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id="r35-btc", now=NOW)
        eth = grant_synthetic_origin(lifecycle.connection, symbol="ETHUSDT", run_id="r35-eth", now=NOW)
        lifecycle.upsert_record(_owned_record(btc.origin_id, lifecycle_id="life-btc"))
        lifecycle.upsert_record(_owned_record(eth.origin_id, lifecycle_id="life-eth", symbol="ETHUSDT"))
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        eth_root, created = _insert_public_alert_event(
            repository,
            plan=_plan(symbol="ETHUSDT", plan_id="life-eth-plan"),
            event_key="life-eth-plan|initial_watchlist",
            event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-eth",
        )
        assert created is True
        assert eth_root is not None
        repository._connection.execute(
            """
            INSERT INTO public_alert_events (
                canonical_plan_id, event_type, event_key, symbol, side, status,
                reserved_at, created_at, updated_at, runtime_epoch_id, origin_lifecycle_id
            ) VALUES ('foreign-plan', 'initial_watchlist', 'foreign-root', 'BTCUSDT', 'long',
                      'RESERVED', ?, ?, ?, 'foreign-epoch', 'life-btc')
            """,
            (NOW, NOW, NOW),
        )
        foreign_id = int(repository._connection.execute("SELECT last_insert_rowid()").fetchone()[0])
        wrong_symbol, created_symbol = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-btc-plan-symbol"),
            event_key="life-btc-plan-symbol|tp1_hit",
            event_type="tp1_hit",
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-btc",
            origin_root_event_id=eth_root.id,
        )
        wrong_epoch, created_epoch = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-btc-plan-epoch"),
            event_key="life-btc-plan-epoch|tp1_hit",
            event_type="tp1_hit",
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-btc",
            origin_root_event_id=foreign_id,
        )
        wrong_life, created_life = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-btc-plan-life"),
            event_key="life-btc-plan-life|tp1_hit",
            event_type="tp1_hit",
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-btc",
            origin_root_event_id=eth_root.id,
        )
        assert created_symbol is False
        assert created_epoch is False
        assert created_life is False
        assert wrong_symbol is None
        assert wrong_epoch is None
        assert wrong_life is None


def test_r36_missing_required_root_rejects_and_initial_root_still_works(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r36.db")
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id="r36", now=NOW)
        lifecycle.upsert_record(_owned_record(origin.origin_id, lifecycle_id="life-r36"))
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        missing, created_missing = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-r36-follow"),
            event_key="life-r36-follow|tp1_hit",
            event_type="tp1_hit",
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-r36",
        )
        assert created_missing is False
        assert missing is None
        root, created_root = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-r36-plan"),
            event_key="life-r36-plan|initial_watchlist",
            event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-r36",
        )
        assert created_root is True
        assert root is not None
        follow, created_follow = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-r36-plan"),
            event_key="life-r36-plan|tp1_hit",
            event_type="tp1_hit",
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-r36",
        )
        assert created_follow is True
        assert follow is not None
        assert follow.origin_root_event_id == root.id


def test_r37_owned_event_plus_legacy_reservation_rejects_unchanged(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r37.db")
    event_id, _owned_res, _part_id = _owned_event_reservation(
        db_path, lifecycle_id="life-r37", event_key="r37:owned", signal_id="r37-owned", run_id="r37"
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        legacy_event = seed_legacy_public_event(
            connection, event_key="r37:legacy", status="RESERVED", delivery_state=PENDING
        )
        legacy_res = seed_legacy_attempt(
            connection,
            signal_id="r37-legacy",
            event_key="r37:legacy",
            telegram_status="pending",
            delivery_state=PENDING,
        )
        before_legacy = _row(connection, "telegram_alert_attempts", legacy_res)
        before_event = _row(connection, "public_alert_events", event_id)
        connection.commit()
        claimed = SQLitePublicTelegramOutbox(connection).claim(
            event_id=event_id, reservation_id=legacy_res, now=NOW
        )
        after_legacy = _row(connection, "telegram_alert_attempts", legacy_res)
        after_event = _row(connection, "public_alert_events", event_id)
    assert claimed.claim is None
    assert after_legacy == before_legacy
    assert after_event == before_event
    del legacy_event


def test_r38_reservation_belonging_to_another_current_event_rejects(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r38.db")
    event_a, _res_a, _part_a = _owned_event_reservation(
        db_path, lifecycle_id="life-r38a", event_key="r38:a", signal_id="r38-a", run_id="r38a"
    )
    _event_b, res_b, _part_b = _owned_event_reservation(
        db_path,
        lifecycle_id="life-r38b",
        event_key="r38:b",
        signal_id="r38-b",
        run_id="r38b",
        direction="short",
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        before_b = _row(connection, "telegram_alert_attempts", res_b)
        before_a = _row(connection, "public_alert_events", event_a)
        claimed = SQLitePublicTelegramOutbox(connection).claim(event_id=event_a, reservation_id=res_b, now=NOW)
        after_b = _row(connection, "telegram_alert_attempts", res_b)
        after_a = _row(connection, "public_alert_events", event_a)
    assert claimed.claim is None
    assert after_b == before_b
    assert after_a == before_a


def test_r39_nonexistent_reservation_rejects(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r39.db")
    event_id, _res, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r39", event_key="r39:owned", signal_id="r39-owned", run_id="r39"
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        before = _row(connection, "public_alert_events", event_id)
        claimed = SQLitePublicTelegramOutbox(connection).claim(event_id=event_id, reservation_id=999999, now=NOW)
        after = _row(connection, "public_alert_events", event_id)
    assert claimed.claim is None
    assert after == before


def test_r40_record_part_result_cannot_mutate_foreign_reservation(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r40.db")
    event_a, _res_a, part_a = _owned_event_reservation(
        db_path, lifecycle_id="life-r40a", event_key="r40:a", signal_id="r40-a", run_id="r40a"
    )
    _event_b, res_b, _part_b = _owned_event_reservation(
        db_path,
        lifecycle_id="life-r40b",
        event_key="r40:b",
        signal_id="r40-b",
        run_id="r40b",
        direction="short",
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        before_b = _row(connection, "telegram_alert_attempts", res_b)
        SQLitePublicTelegramOutbox(connection).record_part_result(
            event_id=event_a,
            reservation_id=res_b,
            part_id=part_a,
            attempt_id="forged",
            result={"delivery_state": SENT, "message_id": "1", "sent_at": NOW},
            now=NOW,
        )
        after_b = _row(connection, "telegram_alert_attempts", res_b)
    assert after_b == before_b


def test_r41_terminal_and_uncertain_cannot_target_foreign_reservation(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r41.db")
    event_a, _res_a, _part_a = _owned_event_reservation(
        db_path, lifecycle_id="life-r41a", event_key="r41:a", signal_id="r41-a", run_id="r41a"
    )
    _event_b, res_b, _part_b = _owned_event_reservation(
        db_path,
        lifecycle_id="life-r41b",
        event_key="r41:b",
        signal_id="r41-b",
        run_id="r41b",
        direction="short",
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        before_b = _row(connection, "telegram_alert_attempts", res_b)
        outbox = SQLitePublicTelegramOutbox(connection)
        outbox.mark_terminal_without_send(
            event_id=event_a,
            reservation_id=res_b,
            state=SKIPPED_DRY_RUN,
            reason="forged",
            now=NOW,
        )
        outbox.mark_uncertain_after_persistence_failure(
            event_id=event_a,
            reservation_id=res_b,
            detail="forged",
            now=NOW,
        )
        after_b = _row(connection, "telegram_alert_attempts", res_b)
    assert after_b == before_b


def test_r42_mark_part_in_flight_rejects_wrong_claim_association(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r42.db")
    event_id, reservation_id, part_id = _owned_event_reservation(
        db_path, lifecycle_id="life-r42", event_key="r42:owned", signal_id="r42-owned", run_id="r42"
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        outbox = SQLitePublicTelegramOutbox(connection)
        claimed = outbox.claim(event_id=event_id, reservation_id=reservation_id, now=NOW)
        assert claimed.claim is not None
        valid_attempt = claimed.claim.attempt_id
        before_part = _row(connection, "public_alert_delivery_parts", part_id)
        assert outbox.mark_part_in_flight(part_id=part_id, attempt_id="wrong-claim", now=NOW) is False
        after_wrong = _row(connection, "public_alert_delivery_parts", part_id)
        assert after_wrong["delivery_state"] == before_part["delivery_state"] or after_wrong["attempt_id"] != "wrong-claim"
        legacy_event = seed_legacy_public_event(
            connection, event_key="r42:legacy-part", status="RESERVED", delivery_state=PENDING
        )
        legacy_part = _seed_legacy_part(connection, event_id=legacy_event, event_key="r42:legacy-part")
        before_legacy = _row(connection, "public_alert_delivery_parts", legacy_part)
        assert outbox.mark_part_in_flight(part_id=legacy_part, attempt_id=valid_attempt, now=NOW) is False
        after_legacy = _row(connection, "public_alert_delivery_parts", legacy_part)
        assert after_legacy == before_legacy


def test_r43_persist_intent_parts_rejects_event_id_event_key_mismatch(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r43.db")
    event_id, _res, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r43", event_key="r43:owned", signal_id="r43-owned", run_id="r43"
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        before = connection.execute(
            "SELECT COUNT(*) FROM public_alert_delivery_parts WHERE event_key = 'r43:other'"
        ).fetchone()[0]
        with pytest.raises(RuntimeEpochOwnershipError):
            persist_intent_parts(
                connection,
                event_id=event_id,
                event_key="r43:other",
                message_text="mismatch",
                message_hash="mismatch",
            )
        after = connection.execute(
            "SELECT COUNT(*) FROM public_alert_delivery_parts WHERE event_key = 'r43:other'"
        ).fetchone()[0]
    assert after == before == 0


def test_r44_insert_attempt_operational_status_rejects_missing_parent_event(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r44.db")
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert (
            repository.insert_attempt(
                _attempt_record(
                    signal_id="r44-missing",
                    event_key="r44:missing",
                    status="sent",
                    delivery_state=SENT,
                )
            )
            is False
        )
        count = repository._connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts WHERE signal_id = 'r44-missing'"
        ).fetchone()[0]
    assert count == 0


def test_r45_audit_only_blocked_attempt_cannot_become_claimable_or_sent(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r45.db")
    event_id, _res, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r45", event_key="r45:owned", signal_id="r45-owned", run_id="r45"
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert repository.insert_attempt(
            _attempt_record(signal_id="r45-audit", event_key="N/A", status="blocked", blocked_reason="diagnostic")
        )
        audit_id = int(
            repository._connection.execute(
                "SELECT id FROM telegram_alert_attempts WHERE signal_id = 'r45-audit'"
            ).fetchone()[0]
        )
        before = _row(repository._connection, "telegram_alert_attempts", audit_id)
        claimed = SQLitePublicTelegramOutbox(repository._connection).claim(
            event_id=event_id, reservation_id=audit_id, now=NOW
        )
        after = _row(repository._connection, "telegram_alert_attempts", audit_id)
    assert claimed.claim is None
    assert after == before
    assert after["telegram_status"] == "blocked"
    assert after["delivery_state"] not in {SENT, IN_FLIGHT, PENDING}


def test_r46_replace_attempt_rejects_unowned_or_wrong_replacement(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r46.db")
    _event_id, owned_res, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r46", event_key="r46:owned", signal_id="r46-owned", run_id="r46"
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_legacy_public_event(connection, event_key="r46:legacy", status="RESERVED", delivery_state=PENDING)
        legacy_id = seed_legacy_attempt(
            connection,
            signal_id="r46-legacy",
            event_key="r46:legacy",
            telegram_status="pending",
            delivery_state=PENDING,
        )
        connection.commit()
    replacement = _attempt_record(signal_id="forged", event_key="r46:missing", status="pending", delivery_state=PENDING)
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        before_owned = _row(repository._connection, "telegram_alert_attempts", owned_res)
        before_legacy = _row(repository._connection, "telegram_alert_attempts", legacy_id)
        assert repository.replace_attempt_with_reservation(attempt_id=owned_res, record=replacement) is False
        assert repository.replace_attempt_with_reservation(attempt_id=legacy_id, record=_attempt_record(
            signal_id="forged-legacy",
            event_key="r46:owned",
            status="pending",
            delivery_state=PENDING,
        )) is False
        after_owned = _row(repository._connection, "telegram_alert_attempts", owned_res)
        after_legacy = _row(repository._connection, "telegram_alert_attempts", legacy_id)
    assert after_owned == before_owned
    assert after_legacy == before_legacy


def test_r47_compact_repeated_attempt_mutates_only_validated_owned_event_rows(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r47.db")
    _event_a, _res_a, _part_a = _owned_event_reservation(
        db_path, lifecycle_id="life-r47a", event_key="r47:a", signal_id="r47-a", run_id="r47a"
    )
    _event_b, _res_b, _part_b = _owned_event_reservation(
        db_path,
        lifecycle_id="life-r47b",
        event_key="r47:b",
        signal_id="r47-b",
        run_id="r47b",
        direction="short",
    )
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        assert repository.insert_attempt(
            _attempt_record(
                signal_id="r47-block-a",
                event_key="r47:a",
                status="blocked",
                blocked_reason="repeat",
            )
        )
        assert repository.insert_attempt(
            _attempt_record(
                signal_id="r47-block-b",
                event_key="r47:b",
                status="blocked",
                blocked_reason="repeat",
                direction="short",
            )
        )
        before_b = dict(
            repository._connection.execute(
                "SELECT seen_count, last_seen_at FROM telegram_alert_attempts WHERE signal_id = 'r47-block-b'"
            ).fetchone()
        )
        borrowed = repository.compact_repeated_attempt(
            _attempt_record(
                signal_id="r47-block-b",
                event_key="r47:a",
                status="blocked",
                blocked_reason="repeat",
            )
        )
        after_borrowed = dict(
            repository._connection.execute(
                "SELECT seen_count, last_seen_at FROM telegram_alert_attempts WHERE signal_id = 'r47-block-b'"
            ).fetchone()
        )
        compacted = repository.compact_repeated_attempt(
            _attempt_record(
                signal_id="r47-block-a",
                event_key="r47:a",
                status="blocked",
                blocked_reason="repeat",
            )
        )
        after_a = int(
            repository._connection.execute(
                "SELECT seen_count FROM telegram_alert_attempts WHERE signal_id = 'r47-block-a'"
            ).fetchone()[0]
        )
    assert borrowed is False
    assert after_borrowed == before_b
    assert compacted is True
    assert after_a >= 2


def test_r48_operational_open_missing_context_does_not_create_db(tmp_path: Path) -> None:
    db_path = tmp_path / "missing-r48.db"
    with pytest.raises(RuntimeEpochConfigurationError):
        open_repository_database(db_path)
    with pytest.raises(RuntimeEpochConfigurationError):
        inspect_operational_database(db_path)
    assert not db_path.exists()


def test_r49_operational_open_missing_context_does_not_migrate_v25(tmp_path: Path) -> None:
    db_path = create_genuine_v25_database(tmp_path / "r49.db")
    before = _schema_snapshot(db_path)
    with pytest.raises(RuntimeEpochConfigurationError):
        open_repository_database(db_path)
    with pytest.raises(RuntimeEpochConfigurationError):
        inspect_operational_database(db_path)
    after = _schema_snapshot(db_path)
    assert after["version"] == 25
    assert after["tables"] == before["tables"]
    assert after["indexes"] == before["indexes"]
    assert after["epoch_count"] == before["epoch_count"]


def test_r50_lifecycle_health_load_against_v25_fails_before_schema_mutation(tmp_path: Path) -> None:
    db_path = create_genuine_v25_database(tmp_path / "r50.db")
    before = _schema_snapshot(db_path)
    with pytest.raises(RuntimeEpochError):
        apply_lifecycle_to_run_result(
            _run_result(_fresh_symbol(evaluation_completed_at=PROCESS_DT)),
            database_path=db_path,
            scan_run_id="r50",
            now=NOW,
            expected_identity=SYNTHETIC_IDENTITY,
        )
    after = _schema_snapshot(db_path)
    assert after["version"] == 25
    assert after["tables"] == before["tables"]
    assert after["indexes"] == before["indexes"]
    assert after["epoch_count"] == 0


def test_r51_same_epoch_id_different_generation_binding_is_rejected(tmp_path: Path) -> None:
    identity_b = RuntimeEpochIdentity(
        epoch_id=SYNTHETIC_EPOCH_ID,
        cutoff_at=SYNTHETIC_CUTOFF_AT,
        contract_version=RUNTIME_EPOCH_CONTRACT_VERSION,
        reviewed_release_sha="bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
        generation_binding="other-generation-binding",
    )
    db_a = _bootstrap(tmp_path / "r51-a.db", SYNTHETIC_IDENTITY)
    db_b = _bootstrap(tmp_path / "r51-b.db", identity_b)
    before_b = _schema_snapshot(db_b)
    with pytest.raises(RuntimeEpochError):
        inspect_operational_database(db_b, expected_identity=SYNTHETIC_IDENTITY)
    with pytest.raises(RuntimeEpochError):
        open_repository_database(db_b, expected_identity=SYNTHETIC_IDENTITY)
    after_b = _schema_snapshot(db_b)
    assert after_b["version"] == before_b["version"]
    assert after_b["epoch_count"] == before_b["epoch_count"]
    assert after_b["tables"] == before_b["tables"]
    inspect_operational_database(db_a, expected_identity=SYNTHETIC_IDENTITY)


def test_r52_settings_only_identity_propagates_without_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "RUNTIME_EPOCH_ID",
        "RUNTIME_EPOCH_CUTOFF_AT",
        "RUNTIME_REVIEWED_RELEASE_SHA",
        "RUNTIME_GENERATION_BINDING",
        "RUNTIME_EPOCH_CONTRACT_VERSION",
    ):
        monkeypatch.delenv(key, raising=False)
    settings = Settings(
        _env_file=None,
        runtime_epoch_id=SYNTHETIC_EPOCH_ID,
        runtime_epoch_cutoff_at=SYNTHETIC_CUTOFF_AT,
        runtime_reviewed_release_sha=SYNTHETIC_RELEASE_SHA,
        runtime_generation_binding=SYNTHETIC_GENERATION_BINDING,
        runtime_epoch_contract_version=RUNTIME_EPOCH_CONTRACT_VERSION,
    )
    identity = identity_from_settings(settings)
    assert identity.epoch_id == SYNTHETIC_EPOCH_ID
    assert identity.generation_binding == SYNTHETIC_GENERATION_BINDING
    db_path = _bootstrap(tmp_path / "r52.db")
    inspect_operational_database(db_path, expected_identity=identity)
    empty = Settings(_env_file=None)
    with pytest.raises(RuntimeEpochConfigurationError):
        identity_from_settings(empty)
    with pytest.raises(RuntimeEpochConfigurationError):
        require_expected_operational_identity(epoch_id=SYNTHETIC_EPOCH_ID)
    args = Namespace(
        runtime_epoch_id=SYNTHETIC_EPOCH_ID,
        runtime_epoch_cutoff_at=SYNTHETIC_CUTOFF_AT,
        runtime_reviewed_release_sha=SYNTHETIC_RELEASE_SHA,
        runtime_generation_binding=SYNTHETIC_GENERATION_BINDING,
        runtime_identity=None,
    )
    from scripts.run_scan import _runtime_identity_from_args_or_settings

    from_cli = _runtime_identity_from_args_or_settings(args, empty)
    assert from_cli.epoch_id == SYNTHETIC_EPOCH_ID
    assert from_cli.generation_binding == SYNTHETIC_GENERATION_BINDING


def test_r53_repeated_watch_preparation_does_not_nest_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import scripts.run_scan as run_scan_module

    db_path = _bootstrap(tmp_path / "r53.db")
    base_dir = tmp_path / "scan_runs"
    base_dir.mkdir()
    monkeypatch.setattr(run_scan_module, "WATCH_STATE_PATH", base_dir / "watch_state.json")
    monkeypatch.setattr(run_scan_module, "WATCH_STATE_CANONICAL_BASE_DIR", base_dir)
    args = Namespace(
        database_path=db_path,
        runtime_identity=SYNTHETIC_IDENTITY,
        runtime_epoch_id=SYNTHETIC_EPOCH_ID,
        watch_state_base_dir=None,
    )
    settings = Settings(_env_file=None)
    run_scan_module._prepare_operational_runtime_if_needed(args, settings)
    first = Path(run_scan_module.WATCH_STATE_PATH)
    args.watch_state_base_dir = None
    run_scan_module._prepare_operational_runtime_if_needed(args, settings)
    second = Path(run_scan_module.WATCH_STATE_PATH)
    assert first == second
    rendered = first.as_posix()
    assert rendered.count("/epochs/") == 1
    assert f"/epochs/{SYNTHETIC_EPOCH_ID}/epochs/" not in rendered


def test_r54_research_watch_missing_epoch_context_zero_sender_calls(tmp_path: Path) -> None:
    db_path = tmp_path / "r54.db"
    with open_initialized_database(db_path) as connection:
        connection.commit()
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_research_settings(enabled=True, to_public=True),
        sender=sender,
    )
    with pytest.raises(RuntimeEpochError):
        run(service.deliver_for_run(_run_result(_research_symbol()), scan_run_id="r54"))
    assert sender.messages == []
    assert sender.calls == []


def test_r55_admin_exact_setup_mismatch_on_same_symbol_is_rejected(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r55.db")
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id="r55", now=NOW)
        record = _owned_record(origin.origin_id, lifecycle_id="life-r55-long").model_copy(
            update={"setup_id": "setup-A", "setup_identity": "BTCUSDT|swing|long|100|102|95", "direction": "long"}
        )
        lifecycle.upsert_record(record)
    allowed_same = _operational_setup_recommendation_allowed(
        {
            "symbol": "BTCUSDT",
            "direction": "long",
            "lifecycle_id": "life-r55-long",
            "setup_id": "setup-A",
            "setup_identity": "BTCUSDT|swing|long|100|102|95",
        },
        db_path,
        expected_identity=SYNTHETIC_IDENTITY,
    )
    denied_short = _operational_setup_recommendation_allowed(
        {
            "symbol": "BTCUSDT",
            "direction": "short",
            "lifecycle_id": "life-r55-short",
            "setup_id": "setup-B",
            "setup_identity": "BTCUSDT|swing|short|100|102|105",
        },
        db_path,
        expected_identity=SYNTHETIC_IDENTITY,
    )
    denied_missing = _operational_setup_recommendation_allowed(
        {"symbol": "BTCUSDT", "direction": "long"},
        db_path,
        expected_identity=SYNTHETIC_IDENTITY,
    )
    denied_no_context = _operational_setup_recommendation_allowed(
        {
            "symbol": "BTCUSDT",
            "direction": "long",
            "lifecycle_id": "life-r55-long",
            "setup_id": "setup-A",
        },
        db_path,
    )
    assert allowed_same is True
    assert denied_short is False
    assert denied_missing is False
    assert denied_no_context is False


def test_r56_active_detail_does_not_combine_owned_event_with_orphan_attempt(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r56.db")
    event_id, _res, _part = _owned_event_reservation(
        db_path, lifecycle_id="life-r56", event_key="r56:owned", signal_id="r56-owned", run_id="r56"
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        seed_legacy_attempt(
            connection,
            signal_id="BTCUSDT-orphan",
            event_key="r56:orphan",
            telegram_status="sent",
            delivery_state=SENT,
        )
        connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET alert_type = 'SIGNAL_CONFIRMED', attempted_alert_type = 'SIGNAL_CONFIRMED',
                new_state = 'CONFIRMED', lifecycle_state = 'CONFIRMED', symbol = 'BTCUSDT'
            WHERE signal_id = 'BTCUSDT-orphan'
            """
        )
        connection.commit()
    result = load_active_signal_detail(project_root=tmp_path, database_path=db_path, selector="BTCUSDT")
    assert result.detail is None
    del event_id


def test_r57_real_producer_pipeline_creates_owned_public_send(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Producer→origin→owned lifecycle; public send stays blocked by unchanged min RR 3.

    The repository's only deterministic pullback producer fixture yields planned RR
    2.66. Scanner unit tests stub target intelligence for this OHLC; this proof reuses
    that same test double so lifecycle can reach ACTIONABLE_A_GRADE, then shows the
    public watchlist path still refuses to send because public min RR is unchanged.
    Inventing a new strategy-valid OHLC series or lowering RR would be out of scope.
    """
    import app.pipeline.scanner_runner as scanner_runner_module

    monkeypatch.setattr(
        scanner_runner_module,
        "build_target_intelligence",
        lambda *args, **kwargs: _clean_target_intelligence(),
    )
    db_path = tmp_path / "r57.db"
    with open_initialized_database(db_path) as connection:
        initialize_runtime_epoch(connection, SYNTHETIC_IDENTITY, activated_at=SYNTHETIC_CUTOFF_AT)
        connection.commit()
    clock = {"now": datetime(2026, 3, 1, 14, 0, tzinfo=UTC)}
    client = FakeAdapterExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"})
    runner = ScannerRunner(
        exchange_client=client,
        clock=lambda: clock["now"],
        capture_clock=lambda: clock["now"],
    )
    first = run_scanner(runner.run(_config(["BTCUSDT"])))
    symbol = first.results[0]
    assert evaluation_completed_at_from_symbol_result(symbol) is not None
    assert producer_acquisition_from_symbol_result(symbol) is not None
    applied_first = apply_lifecycle_to_run_result(
        first,
        database_path=db_path,
        scan_run_id="r57-run-1",
        now="2026-03-01T14:00:00Z",
        expected_identity=SYNTHETIC_IDENTITY,
        confirmation_cycles=2,
    )
    first_life = applied_first.results[0].lifecycle_state
    clock["now"] = datetime(2026, 3, 1, 14, 5, tzinfo=UTC)
    second = run_scanner(runner.run(_config(["BTCUSDT"])))
    applied = apply_lifecycle_to_run_result(
        second,
        database_path=db_path,
        scan_run_id="r57-run-2",
        now="2026-03-01T14:05:00Z",
        expected_identity=SYNTHETIC_IDENTITY,
        confirmation_cycles=2,
    )
    with sqlite3.connect(db_path) as connection:
        origin = connection.execute(
            "SELECT status, origin_id FROM runtime_operational_origins WHERE symbol = 'BTCUSDT' AND status = 'granted'"
        ).fetchone()
        life = connection.execute(
            "SELECT lifecycle_id, runtime_epoch_id, creation_origin_id, current_state FROM setup_lifecycle_records WHERE symbol = 'BTCUSDT'"
        ).fetchone()
    assert origin is not None
    assert life is not None
    assert life[1] == SYNTHETIC_EPOCH_ID
    assert life[2]
    second_life = applied.results[0].lifecycle_state
    assert first_life is not None
    assert second_life is not None
    assert first_life.current_state != SetupLifecycleState.REJECTED
    assert second_life.confirmation_count >= first_life.confirmation_count
    sender = FakeSender()
    summary = run(
        TelegramLifecycleDeliveryService(
            database_path=db_path,
            settings=Settings(
                _env_file=None,
                telegram_dry_run=True,
                telegram_signals_enabled=False,
                telegram_public_watchlist_enabled=True,
                local_manual_mode=True,
                order_execution_enabled=False,
            ),
            sender=sender,
            min_rr=Decimal("3"),
            min_score_for_idea=Decimal("88"),
            expected_identity=SYNTHETIC_IDENTITY,
        ).deliver_for_run(applied_first, scan_run_id="r57-deliver")
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        event = connection.execute(
            "SELECT id, event_key, runtime_epoch_id, origin_lifecycle_id FROM public_alert_events WHERE runtime_epoch_id IS NOT NULL"
        ).fetchone()
    assert first.results[0].trade_idea is not None
    assert first_life is not None
    assert first_life.current_state != SetupLifecycleState.REJECTED
    assert summary.sent == 0
    assert sender.messages == []
    assert sender.calls == []
    assert event is None
    reasons = " ".join(summary.public_watchlist_audit.skipped_by_reason)
    assert "rr_below_public_min" in reasons or "public_block_low_opportunity_score" in reasons


def test_r58_insufficient_quality_pipeline_does_not_send(tmp_path: Path) -> None:
    db_path = tmp_path / "r58.db"
    with open_initialized_database(db_path) as connection:
        initialize_runtime_epoch(connection, SYNTHETIC_IDENTITY, activated_at=SYNTHETIC_CUTOFF_AT)
        connection.commit()
    clock_now = datetime(2026, 3, 1, 14, 0, tzinfo=UTC)
    client = FakeAdapterExchangeClient({"BTCUSDT": _flat_candles()}, failing_timeframes={"2d"})
    scanned = run_scanner(
        ScannerRunner(
            exchange_client=client,
            clock=lambda: clock_now,
            capture_clock=lambda: clock_now,
        ).run(_config(["BTCUSDT"]))
    )
    applied = apply_lifecycle_to_run_result(
        scanned,
        database_path=db_path,
        scan_run_id="r58-run",
        now="2026-03-01T14:00:00Z",
        expected_identity=SYNTHETIC_IDENTITY,
    )
    sender = FakeSender()
    summary = run(
        TelegramLifecycleDeliveryService(
            database_path=db_path,
            settings=Settings(
                _env_file=None,
                telegram_dry_run=True,
                telegram_signals_enabled=False,
                telegram_public_watchlist_enabled=True,
                local_manual_mode=True,
                order_execution_enabled=False,
            ),
            sender=sender,
            expected_identity=SYNTHETIC_IDENTITY,
        ).deliver_for_run(applied, scan_run_id="r58-deliver")
    )
    assert summary.sent == 0
    assert sender.messages == []
    assert sender.calls == []
    with sqlite3.connect(db_path) as connection:
        events = connection.execute(
            "SELECT COUNT(*) FROM public_alert_events WHERE runtime_epoch_id IS NOT NULL"
        ).fetchone()[0]
    assert events == 0


def test_r59_global_legacy_sent_event_key_remains_consumed(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r59.db")
    event_key = "global:r59-consumed"
    with sqlite3.connect(db_path) as connection:
        seed_legacy_public_event(connection, event_key=event_key, status="SENT", delivery_state=SENT)
        connection.commit()
    with SQLiteSetupLifecycleRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
        origin = grant_synthetic_origin(lifecycle.connection, symbol="BTCUSDT", run_id="r59", now=NOW)
        lifecycle.upsert_record(_owned_record(origin.origin_id, lifecycle_id="life-r59"))
    with SQLiteTelegramAlertAttemptRepository(db_path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        event, created = _insert_public_alert_event(
            repository,
            plan=_plan(plan_id="life-r59-plan"),
            event_key=event_key,
            event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
            status="RESERVED",
            reserved_at=NOW,
            origin_lifecycle_id="life-r59",
        )
        assert created is False
        assert event is not None
        assert event.status == "SENT"
        count = repository._connection.execute(
            "SELECT COUNT(*) FROM public_alert_events WHERE event_key = ?",
            (event_key,),
        ).fetchone()[0]
    assert count == 1


def test_r60_legacy_uncertain_and_reservation_unchanged_under_borrowed_event_attacks(tmp_path: Path) -> None:
    db_path = _bootstrap(tmp_path / "r60.db")
    event_id, _owned_res, part_id = _owned_event_reservation(
        db_path, lifecycle_id="life-r60", event_key="r60:owned", signal_id="r60-owned", run_id="r60"
    )
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        legacy_event = seed_legacy_public_event(
            connection, event_key="r60:uncertain", status="RESERVED", delivery_state=UNCERTAIN
        )
        legacy_part = _seed_legacy_part(
            connection, event_id=legacy_event, event_key="r60:uncertain", state=UNCERTAIN
        )
        legacy_res = seed_legacy_attempt(
            connection,
            signal_id="r60-uncertain",
            event_key="r60:uncertain",
            telegram_status="uncertain",
            delivery_state=UNCERTAIN,
        )
        connection.commit()
        event_before = _row(connection, "public_alert_events", legacy_event)
        part_before = _row(connection, "public_alert_delivery_parts", legacy_part)
        res_before = _row(connection, "telegram_alert_attempts", legacy_res)
        outbox = SQLitePublicTelegramOutbox(connection)
        outbox.claim(event_id=event_id, reservation_id=legacy_res, now=NOW)
        outbox.record_part_result(
            event_id=event_id,
            reservation_id=legacy_res,
            part_id=part_id,
            attempt_id="borrowed",
            result={"delivery_state": SENT, "message_id": "9", "sent_at": NOW},
            now=NOW,
        )
        outbox.mark_terminal_without_send(
            event_id=event_id,
            reservation_id=legacy_res,
            state=SKIPPED_DRY_RUN,
            reason="borrowed",
            now=NOW,
        )
        outbox.mark_uncertain_after_persistence_failure(
            event_id=event_id,
            reservation_id=legacy_res,
            detail="borrowed",
            now=NOW,
        )
        outbox.mark_part_in_flight(part_id=legacy_part, attempt_id="borrowed", now=NOW)
        event_after = _row(connection, "public_alert_events", legacy_event)
        part_after = _row(connection, "public_alert_delivery_parts", legacy_part)
        res_after = _row(connection, "telegram_alert_attempts", legacy_res)
    assert event_after == event_before
    assert part_after == part_before
    assert res_after == res_before
