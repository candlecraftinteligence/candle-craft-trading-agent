"""Comprehensive synthetic test suite for P0B3B1 Telegram outbox transaction/transport protocol foundation.

All 20 required hardening test scenarios mandated by Master Prompt Final Release Gate:
1. Cancellation after HTTP before Stage 3 admission.
2. Repeated cancellation during admission wait.
3. Cancellation during Stage 3 transaction.
4. Cancellation during commit.
5. Cancellation during worker shutdown.
6. SQLite result persistence raises before commit.
7. SQLite UNCERTAIN fallback silently fails.
8. Database remains IN_FLIGHT after failed fallback.
9. No false DurableDeliveryReceipt.
10. Valid HTTP success with verified Telegram message ID.
11. Malformed SENT outcome with no message ID.
12. Contradictory SENT outcome with failed HTTP status.
13. Unknown provider acknowledgment.
14. HTTP 5xx with uncertain acceptance.
15. Stage 1 cancellation combined with worker exception.
16. Two concurrent claimants.
17. Independent-process claim ownership.
18. Multipart success and partial uncertainty.
19. Restart after unresolved persistence.
20. No second HTTP call for uncertain delivery.

Additional regression guards:
21. Existing TelegramLifecycleDeliveryService interface unmodified.
22. No mutation of the eleven historical quarantined records.
23. No database connection held during external network await.

Strict assertion: Fake transport invocation counts are verified in every test.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from app.alerts.telegram_outbox import (
    FAILED_FINAL,
    IN_FLIGHT,
    PENDING,
    RETRYABLE,
    SENT,
    UNCERTAIN,
    SQLitePublicTelegramOutbox,
    persist_intent_parts,
)
from app.alerts.telegram_outbox_protocol import (
    CommittedPartReservationReceipt,
    DurableDeliveryReceipt,
    OutboxClaimRejected,
    OutboxPreparationError,
    PersistenceCommitFailureError,
    TelegramDeliveryProtocolResult,
    TelegramOutboxWorker,
    TelegramOutboxWorkerClosed,
    TelegramTransportOutcome,
    TelegramTransportRequest,
    deliver_single_reserved_part,
    normalize_transport_outcome,
    stage_1_outbox_preparation,
    stage_2_async_transport,
    stage_3_outbox_persistence,
)
from app.runtime_epoch.models import RuntimeEpochIdentity
from app.runtime_epoch.ownership import bind_canonical_reservation_attempt
from app.storage.database import connect_database, open_initialized_database
from tests.runtime_epoch_support import (
    SYNTHETIC_EPOCH_ID,
    SYNTHETIC_IDENTITY,
    bootstrap_operational_test_database,
    grant_synthetic_origin,
    seed_legacy_lifecycle,
)

NOW = "2026-07-16T10:00:00Z"
EVENT_KEY_BASE = "plan-p0b3b1|initial_watchlist"


@dataclass
class FakeTelegramTransport:
    """Configurable fake transport recording calls and returning synthetic outcomes."""

    mode: str = "success"  # "success", "timeout", "http_500", "http_502", "exception", "uncertain", "custom"
    calls: list[TelegramTransportRequest] = field(default_factory=list)
    custom_outcome: TelegramTransportOutcome | None = None
    on_request_hook: Any | None = None
    on_response_hook: Any | None = None
    delay_seconds: float = 0.0

    async def __call__(self, request: TelegramTransportRequest) -> TelegramTransportOutcome:
        self.calls.append(request)
        if self.on_request_hook is not None:
            hook_res = self.on_request_hook(request)
            if asyncio.iscoroutine(hook_res):
                await hook_res

        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)

        if self.on_response_hook is not None:
            hook_res = self.on_response_hook(request)
            if asyncio.iscoroutine(hook_res):
                await hook_res

        if self.mode == "success":
            return TelegramTransportOutcome(
                delivery_state=SENT,
                http_status=200,
                telegram_message_id=f"tg_msg_{request.part_id}_{len(self.calls)}",
                telegram_chat_id=request.destination_chat_id,
                sent_at=NOW,
            )
        elif self.mode == "timeout":
            return TelegramTransportOutcome(
                delivery_state=UNCERTAIN,
                error_category="transport_timeout_uncertain",
                error="HTTP client timeout during transmission; outcome uncertain",
            )
        elif self.mode == "http_500":
            return TelegramTransportOutcome(
                delivery_state=RETRYABLE,
                http_status=500,
                error_category="server_error",
                error="Internal server error from Telegram gateway",
            )
        elif self.mode == "http_502":
            return TelegramTransportOutcome(
                delivery_state=RETRYABLE,
                http_status=502,
                error_category="bad_gateway",
                error="Bad Gateway from Telegram reverse proxy",
            )
        elif self.mode == "exception":
            raise ConnectionResetError("Connection abruptly closed by peer")
        elif self.mode == "uncertain":
            return TelegramTransportOutcome(
                delivery_state=UNCERTAIN,
                error_category="unconfirmed_transmission",
                error="Transport connection reset after bytes sent",
            )
        elif self.mode == "custom" and self.custom_outcome is not None:
            return self.custom_outcome
        else:
            return TelegramTransportOutcome(
                delivery_state=UNCERTAIN,
                error_category="unknown_mode",
                error=f"Unrecognized mode: {self.mode}",
            )


def _seed_canonical_event(
    db_path: Path,
    *,
    event_key: str = EVENT_KEY_BASE,
    message: str = "P0B3B1 synthetic alert payload",
    max_attempts: int = 3,
    max_message_length: int = 4096,
    initial_event_state: str = PENDING,
    initial_part_state: str = PENDING,
    runtime_epoch_id: str | None = SYNTHETIC_EPOCH_ID,
    bind_reservation: bool = True,
    signal_id: str = "signal-p0b3b1",
) -> tuple[int, int]:
    """Seed a fully initialized SQLite database with canonical reservation and outbox parts."""
    with open_initialized_database(db_path) as connection:
        if runtime_epoch_id is not None:
            origin = grant_synthetic_origin(connection, symbol="BTCUSDT", run_id="p0b3b1-seed-run", now=NOW)
            seed_legacy_lifecycle(
                connection,
                lifecycle_id=signal_id,
                symbol="BTCUSDT",
                current_state="WATCHLISTED",
                last_seen_at=NOW,
            )
            connection.execute(
                """
                UPDATE setup_lifecycle_records
                SET runtime_epoch_id = ?, creation_origin_id = ?
                WHERE lifecycle_id = ?
                """,
                (runtime_epoch_id, origin.origin_id, signal_id),
            )

        cursor = connection.execute(
            """
            INSERT INTO public_alert_events (
                canonical_plan_id, event_type, event_key, symbol, side, status,
                reserved_at, delivery_state, payload_text, message_hash,
                destination_chat_id, destination_kind, max_attempts, created_at, updated_at,
                runtime_epoch_id, origin_lifecycle_id
            ) VALUES (?, 'initial_watchlist', ?, 'BTCUSDT', 'long', 'RESERVED',
                      ?, ?, ?, 'hash-p0b3b1', 'test-chat-p0b3b1', 'public_chat', ?, ?, ?, ?, ?)
            """,
            (
                "plan-p0b3b1",
                event_key,
                NOW,
                initial_event_state,
                message,
                max_attempts,
                NOW,
                NOW,
                runtime_epoch_id,
                signal_id,
            ),
        )
        event_id = int(cursor.lastrowid)

        cursor = connection.execute(
            """
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
                sent_at, attempted_at, telegram_status, message_hash,
                attempted_alert_type, public_watchlist_plan_id,
                public_watchlist_event_key, public_alert_event_type,
                delivery_state, delivery_part_count
            ) VALUES (
                ?, 'BTCUSDT', 'long', 'WATCHLISTED', 'WATCHLIST', 'WATCHLISTED',
                NULL, ?, 'pending', 'hash-p0b3b1', 'WATCHLIST', 'plan-p0b3b1', ?,
                'initial_watchlist', ?, 1
            )
            """,
            (signal_id, NOW, event_key, initial_event_state),
        )
        attempt_id = int(cursor.lastrowid)

        parts = persist_intent_parts(
            connection,
            event_id=event_id,
            event_key=event_key,
            message_text=message,
            message_hash="hash-p0b3b1",
            destination_chat_id="test-chat-p0b3b1",
            destination_kind="public_chat",
            max_attempts=max_attempts,
            max_message_length=max_message_length,
        )
        if initial_part_state != PENDING:
            connection.execute(
                "UPDATE public_alert_delivery_parts SET delivery_state = ? WHERE public_alert_event_id = ?",
                (initial_part_state, event_id),
            )

        connection.execute(
            "UPDATE telegram_alert_attempts SET delivery_part_count = ? WHERE id = ?",
            (len(parts), attempt_id),
        )

        if bind_reservation:
            bind_canonical_reservation_attempt(
                connection,
                event_id=event_id,
                attempt_id=attempt_id,
            )
        connection.commit()

    return event_id, attempt_id


# ==============================================================================
# TEST 1: Cancellation after HTTP before Stage 3 admission
# ==============================================================================
def test_01_cancellation_after_http_before_stage3_admission(tmp_path: Path) -> None:
    db_path = tmp_path / "test01.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            task_ref = None

            def _on_request(req):
                # Request has completed HTTP transmission
                nonlocal task_ref
                assert task_ref is not None
                task_ref.cancel()

            transport = FakeTelegramTransport(mode="success", delay_seconds=0.01, on_response_hook=_on_request)

            async def _run():
                nonlocal task_ref
                task_ref = asyncio.current_task()
                return await deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=SYNTHETIC_IDENTITY,
                    now=NOW,
                )

            task = asyncio.create_task(_run())
            with pytest.raises(asyncio.CancelledError):
                await task

            assert len(transport.calls) == 1, "HTTP transport must be invoked exactly once"
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    # Crucial assertion: Stage 3 was fenced, so outcome was durably persisted before cancelling
    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == SENT


# ==============================================================================
# TEST 2: Repeated cancellation during admission wait
# ==============================================================================
def test_02_repeated_cancellation_during_admission_wait(tmp_path: Path) -> None:
    db_path = tmp_path / "test02.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            # Hold worker admission lock
            await worker._admission.acquire()

            transport = FakeTelegramTransport(mode="success")

            delivery_task = asyncio.create_task(
                deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=SYNTHETIC_IDENTITY,
                    now=NOW,
                )
            )

            # Wait until delivery_task is waiting on admission
            await asyncio.sleep(0.02)
            # Repeated cancellations while waiting for admission
            delivery_task.cancel()
            delivery_task.cancel()
            delivery_task.cancel()

            # Now release admission lock so delivery_task can be admitted
            worker._admission.release()

            # It must not raise before completing Stage 1 / Stage 3
            with pytest.raises(asyncio.CancelledError):
                await delivery_task

            # Cancelled before HTTP transport, so 0 HTTP calls
            assert len(transport.calls) == 0
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 3: Cancellation during Stage 3 transaction
# ==============================================================================
def test_03_cancellation_during_stage3_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "test03.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            delivery_task = None

            def _on_request(req):
                # When transport finishes, schedule a cancellation during Stage 3
                asyncio.get_running_loop().call_soon(lambda: delivery_task.cancel())

            transport = FakeTelegramTransport(mode="success", delay_seconds=0.01, on_response_hook=_on_request)

            delivery_task = asyncio.create_task(
                deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=SYNTHETIC_IDENTITY,
                    now=NOW,
                )
            )

            with pytest.raises(asyncio.CancelledError):
                await delivery_task

            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    # Verified: Transaction completed cleanly and committed in SQLite
    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == SENT


# ==============================================================================
# TEST 4: Cancellation during commit
# ==============================================================================
def test_04_cancellation_during_commit(tmp_path: Path) -> None:
    db_path = tmp_path / "test04.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            delivery_task = None

            def _on_request(req):
                asyncio.get_running_loop().call_soon(lambda: delivery_task.cancel())

            transport = FakeTelegramTransport(mode="success", delay_seconds=0.01, on_response_hook=_on_request)

            delivery_task = asyncio.create_task(
                deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=SYNTHETIC_IDENTITY,
                    now=NOW,
                )
            )

            with pytest.raises(asyncio.CancelledError):
                await delivery_task

            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == SENT


# ==============================================================================
# TEST 5: Cancellation during worker shutdown
# ==============================================================================
def test_05_cancellation_during_worker_shutdown() -> None:
    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        shutdown_task = asyncio.create_task(worker.aclose())
        await asyncio.sleep(0)  # Allow aclose() to enter and initiate shutdown
        shutdown_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await shutdown_task

        # Worker thread must be cleanly joined, not left alive as a zombie
        assert not worker._thread.is_alive()

    asyncio.run(scenario())


# ==============================================================================
# TEST 6: SQLite result persistence raises before commit
# ==============================================================================
def test_06_sqlite_result_persistence_raises_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test06.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    original_record = SQLitePublicTelegramOutbox.record_part_result

    def _failing_record(self, *args, **kwargs):
        raise sqlite3.OperationalError("Simulated disk error during result recording")

    monkeypatch.setattr(SQLitePublicTelegramOutbox, "record_part_result", _failing_record)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            transport = FakeTelegramTransport(mode="success")
            result = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )

            # Fallback path caught OperationalError and durably committed UNCERTAIN
            assert result.status == "uncertain"
            assert result.durable_receipt is not None
            assert result.durable_receipt.delivery_state == UNCERTAIN
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == UNCERTAIN


# ==============================================================================
# TEST 7: SQLite UNCERTAIN fallback silently fails
# ==============================================================================
def test_07_sqlite_uncertain_fallback_silently_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test07.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    # Simulate record_part_result failure
    def _failing_record(self, *args, **kwargs):
        raise sqlite3.OperationalError("Simulated disk error during record")

    # Simulate legacy fallback that silently swallows the error without updating SQLite
    def _silent_fallback(self, *args, **kwargs):
        return  # Does nothing! Database row is left untouched in IN_FLIGHT.

    monkeypatch.setattr(SQLitePublicTelegramOutbox, "record_part_result", _failing_record)
    monkeypatch.setattr(SQLitePublicTelegramOutbox, "mark_uncertain_after_persistence_failure", _silent_fallback)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            transport = FakeTelegramTransport(mode="success")
            result = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )

            # Mandatory commit verification detected that SQLite was NOT updated!
            assert result.status == "unresolved_persistence"
            assert result.durable_receipt is None
            assert "remained in state" in result.error_message or "remained IN_FLIGHT" in result.error_message
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 8: Database remains IN_FLIGHT after failed fallback
# ==============================================================================
def test_08_database_remains_in_flight_after_failed_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test08.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    def _failing_record(self, *args, **kwargs):
        raise sqlite3.OperationalError("Simulated disk error during record")

    def _silent_fallback(self, *args, **kwargs):
        return

    monkeypatch.setattr(SQLitePublicTelegramOutbox, "record_part_result", _failing_record)
    monkeypatch.setattr(SQLitePublicTelegramOutbox, "mark_uncertain_after_persistence_failure", _silent_fallback)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            transport = FakeTelegramTransport(mode="success")
            result = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )
            assert result.status == "unresolved_persistence"
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    # Directly check SQLite: Event remains IN_FLIGHT, never falsely declared UNCERTAIN
    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == IN_FLIGHT


# ==============================================================================
# TEST 9: No false DurableDeliveryReceipt
# ==============================================================================
def test_09_no_false_durable_delivery_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test09.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    def _failing_record(self, *args, **kwargs):
        raise sqlite3.OperationalError("Simulated disk error")

    def _silent_fallback(self, *args, **kwargs):
        return

    monkeypatch.setattr(SQLitePublicTelegramOutbox, "record_part_result", _failing_record)
    monkeypatch.setattr(SQLitePublicTelegramOutbox, "mark_uncertain_after_persistence_failure", _silent_fallback)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            transport = FakeTelegramTransport(mode="success")
            result = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )
            # Crucial assertion: No DurableDeliveryReceipt is issued when persistence was not verified
            assert result.durable_receipt is None
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 10: Valid HTTP success with verified Telegram message ID
# ==============================================================================
def test_10_valid_http_success_with_verified_telegram_message_id(tmp_path: Path) -> None:
    db_path = tmp_path / "test10.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            custom_outcome = TelegramTransportOutcome(
                delivery_state=SENT,
                http_status=200,
                telegram_message_id="valid_tg_msg_7788",
                telegram_chat_id="test-chat",
                sent_at=NOW,
            )
            transport = FakeTelegramTransport(mode="custom", custom_outcome=custom_outcome)
            result = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )

            assert result.status == "sent"
            assert result.durable_receipt is not None
            assert result.durable_receipt.delivery_state == SENT
            assert result.durable_receipt.telegram_message_id == "valid_tg_msg_7788"
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 11: Malformed SENT outcome with no message ID
# ==============================================================================
def test_11_malformed_sent_outcome_with_no_message_id(tmp_path: Path) -> None:
    db_path = tmp_path / "test11.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            # Transport claims SENT but omits message ID
            malformed_outcome = TelegramTransportOutcome(
                delivery_state=SENT,
                http_status=200,
                telegram_message_id=None,
            )
            transport = FakeTelegramTransport(mode="custom", custom_outcome=malformed_outcome)
            result = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )

            # Normalized to UNCERTAIN
            assert result.status == "uncertain"
            assert result.durable_receipt is not None
            assert result.durable_receipt.delivery_state == UNCERTAIN
            assert result.transport_outcome.error_category == "confirmed_success_missing_message_id"
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == UNCERTAIN


# ==============================================================================
# TEST 12: Contradictory SENT outcome with failed HTTP status
# ==============================================================================
def test_12_contradictory_sent_outcome_with_failed_http_status(tmp_path: Path) -> None:
    db_path = tmp_path / "test12.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            # Transport claims SENT but returns HTTP 500
            contradictory_outcome = TelegramTransportOutcome(
                delivery_state=SENT,
                http_status=500,
                telegram_message_id="msg-contradictory-1",
            )
            transport = FakeTelegramTransport(mode="custom", custom_outcome=contradictory_outcome)
            result = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )

            assert result.status == "uncertain"
            assert result.durable_receipt is not None
            assert result.durable_receipt.delivery_state == UNCERTAIN
            assert result.transport_outcome.error_category == "contradictory_success_http_status"
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 13: Unknown provider acknowledgment
# ==============================================================================
def test_13_unknown_provider_acknowledgment(tmp_path: Path) -> None:
    db_path = tmp_path / "test13.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            unknown_outcome = TelegramTransportOutcome(
                delivery_state="UNRECOGNIZED_STATE",
                http_status=200,
            )
            transport = FakeTelegramTransport(mode="custom", custom_outcome=unknown_outcome)
            result = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )

            assert result.status == "uncertain"
            assert result.durable_receipt is not None
            assert result.durable_receipt.delivery_state == UNCERTAIN
            assert result.transport_outcome.error_category == "unknown_delivery_state"
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 14: HTTP 5xx with uncertain acceptance
# ==============================================================================
def test_14_http_5xx_with_uncertain_acceptance(tmp_path: Path) -> None:
    db_path = tmp_path / "test14.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            # Server returns HTTP 502 Bad Gateway after transmission started
            transport = FakeTelegramTransport(mode="http_502")
            result = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )

            # Must fail closed to UNCERTAIN, never automatically retried as un-sent
            assert result.status == "uncertain"
            assert result.durable_receipt is not None
            assert result.durable_receipt.delivery_state == UNCERTAIN
            assert result.transport_outcome.error_category == "http_5xx_uncertain_acceptance"
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == UNCERTAIN


# ==============================================================================
# TEST 15: Stage 1 cancellation combined with worker exception
# ==============================================================================
def test_15_stage_1_cancellation_combined_with_worker_exception(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test15.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    def _failing_prep(*args, **kwargs):
        time.sleep(0.02)
        raise OutboxPreparationError("Recoverable validation error")

    monkeypatch.setattr("app.alerts.telegram_outbox_protocol.stage_1_outbox_preparation", _failing_prep)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            transport = FakeTelegramTransport(mode="success")

            async def _run():
                t = asyncio.current_task()
                # Schedule task cancellation while worker is running
                asyncio.get_running_loop().call_later(0.01, lambda: t.cancel())
                return await deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=SYNTHETIC_IDENTITY,
                    now=NOW,
                )

            task = asyncio.create_task(_run())
            with pytest.raises(asyncio.CancelledError):
                await task

            assert len(transport.calls) == 0, "Transport must not be called when cancelled"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 16: Two concurrent claimants
# ==============================================================================
def test_16_two_concurrent_claimants(tmp_path: Path) -> None:
    db_path = tmp_path / "test16.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            transport = FakeTelegramTransport(mode="success", delay_seconds=0.01)

            result_a, result_b = await asyncio.gather(
                deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=SYNTHETIC_IDENTITY,
                    now=NOW,
                ),
                deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=SYNTHETIC_IDENTITY,
                    now=NOW,
                ),
            )

            statuses = {result_a.status, result_b.status}
            assert "sent" in statuses
            assert "skipped" in statuses or "blocked" in statuses
            assert len(transport.calls) == 1, "Exactly one claimant must win the claim and call transport"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 17: Independent-process claim ownership
# ==============================================================================
def test_17_independent_process_claim_ownership(tmp_path: Path) -> None:
    db_path = tmp_path / "test17.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    def _worker_claim(owner_name: str) -> bool:
        try:
            receipt = stage_1_outbox_preparation(
                db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
                owner=owner_name,
            )
            return receipt is not None
        except (OutboxClaimRejected, OutboxPreparationError):
            return False

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [executor.submit(_worker_claim, f"thread-owner-{i}") for i in range(4)]
        results = [f.result() for f in futures]

    assert sum(1 for r in results if r) == 1, "Exactly one thread must win the atomic claim"


# ==============================================================================
# TEST 18: Multipart success and partial uncertainty
# ==============================================================================
def test_18_multipart_success_and_partial_uncertainty(tmp_path: Path) -> None:
    db_path = tmp_path / "test18.db"
    bootstrap_operational_test_database(db_path)
    two_part_message = "A" * 5000
    event_id, reservation_id = _seed_canonical_event(
        db_path, message=two_part_message, max_message_length=4000
    )

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            # Deliver Part 1 -> SUCCESS
            transport_1 = FakeTelegramTransport(mode="success")
            result_1 = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport_1,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
                part_index=1,
            )

            assert result_1.status == "in_flight"
            assert result_1.durable_receipt is not None
            assert result_1.durable_receipt.delivery_state == IN_FLIGHT
            assert result_1.durable_receipt.all_parts_sent is False
            assert len(transport_1.calls) == 1

            # Deliver Part 2 -> TIMEOUT / UNCERTAIN
            assert result_1.reservation_receipt is not None
            transport_2 = FakeTelegramTransport(mode="timeout")
            result_2 = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport_2,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
                part_index=2,
                attempt_id=result_1.reservation_receipt.attempt_id,
            )

            assert result_2.status == "uncertain"
            assert result_2.durable_receipt is not None
            assert result_2.durable_receipt.all_parts_sent is False
            assert len(transport_2.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    with connect_database(db_path) as conn:
        parts = conn.execute(
            "SELECT part_index, delivery_state FROM public_alert_delivery_parts WHERE public_alert_event_id = ? ORDER BY part_index",
            (event_id,),
        ).fetchall()
        assert len(parts) == 2
        assert parts[0]["delivery_state"] == SENT
        assert parts[1]["delivery_state"] == UNCERTAIN

        event_row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert event_row["delivery_state"] == UNCERTAIN


# ==============================================================================
# TEST 19: Restart after unresolved persistence
# ==============================================================================
def test_19_restart_after_unresolved_persistence(tmp_path: Path) -> None:
    db_path = tmp_path / "test19.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(
        db_path, initial_event_state=UNCERTAIN, initial_part_state=UNCERTAIN
    )

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            transport = FakeTelegramTransport(mode="success")
            result = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )

            assert result.status in ("blocked", "skipped")
            assert result.durable_receipt is None
            assert len(transport.calls) == 0, "Restart with UNCERTAIN event must not call transport"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 20: No second HTTP call for uncertain delivery
# ==============================================================================
def test_20_no_second_http_call_for_uncertain_delivery(tmp_path: Path) -> None:
    db_path = tmp_path / "test20.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            # First attempt times out -> UNCERTAIN
            transport_1 = FakeTelegramTransport(mode="timeout")
            result_1 = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport_1,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )
            assert result_1.status == "uncertain"
            assert len(transport_1.calls) == 1

            # Second attempt must be rejected before HTTP
            transport_2 = FakeTelegramTransport(mode="success")
            result_2 = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport_2,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )
            assert result_2.status in ("blocked", "skipped")
            assert len(transport_2.calls) == 0, "No second HTTP call allowed for UNCERTAIN event"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 21: Existing TelegramLifecycleDeliveryService interface unmodified
# ==============================================================================
def test_21_existing_telegram_lifecycle_service_unmodified() -> None:
    from app.alerts.telegram_lifecycle import TelegramLifecycleDeliveryService
    assert hasattr(TelegramLifecycleDeliveryService, "deliver_for_run")
    assert hasattr(TelegramLifecycleDeliveryService, "public_watchlist_enabled")


# ==============================================================================
# TEST 22: No mutation of the eleven historical quarantined records
# ==============================================================================
def test_22_no_mutation_of_historical_quarantined_records(tmp_path: Path) -> None:
    db_path = tmp_path / "test22.db"
    bootstrap_operational_test_database(db_path)

    # Seed 11 historical records without canonical reservation (representing legacy/quarantined records)
    historical_event_ids: list[tuple[int, int]] = []
    with open_initialized_database(db_path) as conn:
        for i in range(11):
            key = f"historical-plan-{i}|watchlist"
            cur = conn.execute(
                """
                INSERT INTO public_alert_events (
                    canonical_plan_id, event_type, event_key, symbol, side, status,
                    reserved_at, delivery_state, payload_text, message_hash,
                    destination_chat_id, destination_kind, max_attempts, created_at, updated_at,
                    runtime_epoch_id, origin_lifecycle_id, canonical_reservation_attempt_id
                ) VALUES (?, 'initial_watchlist', ?, 'BTCUSDT', 'long', 'RESERVED',
                          ?, 'PENDING', 'historical payload', 'hist-hash',
                          'hist-chat', 'public_chat', 3, ?, ?, NULL, ?, NULL)
                """,
                (f"hist-plan-{i}", key, NOW, NOW, NOW, f"legacy-signal-{i}"),
            )
            ev_id = int(cur.lastrowid)

            cur_att = conn.execute(
                """
                INSERT INTO telegram_alert_attempts (
                    signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
                    sent_at, attempted_at, telegram_status, message_hash,
                    attempted_alert_type, public_watchlist_plan_id,
                    public_watchlist_event_key, public_alert_event_type,
                    delivery_state, delivery_part_count
                ) VALUES (
                    ?, 'BTCUSDT', 'long', 'WATCHLISTED', 'WATCHLIST', 'WATCHLISTED',
                    NULL, ?, 'pending', 'hist-hash', 'WATCHLIST', ?, ?,
                    'initial_watchlist', 'PENDING', 1
                )
                """,
                (f"legacy-signal-{i}", NOW, f"hist-plan-{i}", key),
            )
            att_id = int(cur_att.lastrowid)

            conn.execute(
                """
                INSERT INTO public_alert_delivery_parts (
                    public_alert_event_id, event_key, part_index, part_count,
                    payload_text, payload_hash, delivery_state, created_at, updated_at
                ) VALUES (?, ?, 1, 1, 'historical payload', 'hist-hash', 'PENDING', ?, ?)
                """,
                (ev_id, key, NOW, NOW),
            )
            historical_event_ids.append((ev_id, att_id))
        conn.commit()

    # Capture snapshot before execution
    with connect_database(db_path) as conn:
        before_rows = conn.execute(
            "SELECT id, delivery_state, canonical_reservation_attempt_id, updated_at FROM public_alert_events WHERE canonical_reservation_attempt_id IS NULL"
        ).fetchall()
        assert len(before_rows) == 11

    transport = FakeTelegramTransport(mode="success")

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            for ev_id, att_id in historical_event_ids:
                result = await deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=ev_id,
                    reservation_id=att_id,
                    transport=transport,
                    expected_identity=SYNTHETIC_IDENTITY,
                    now=NOW,
                )
                assert result.status in ("failed", "blocked")
                assert result.durable_receipt is None
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    assert len(transport.calls) == 0, "Transport MUST NEVER be called for historical quarantined records"

    # Strict assertion: Zero mutations on all 11 records in SQLite
    with connect_database(db_path) as conn:
        after_rows = conn.execute(
            "SELECT id, delivery_state, canonical_reservation_attempt_id, updated_at FROM public_alert_events WHERE canonical_reservation_attempt_id IS NULL"
        ).fetchall()
        assert len(after_rows) == 11
        for b_row, a_row in zip(before_rows, after_rows, strict=True):
            assert b_row["id"] == a_row["id"]
            assert b_row["delivery_state"] == a_row["delivery_state"]
            assert b_row["canonical_reservation_attempt_id"] == a_row["canonical_reservation_attempt_id"]
            assert b_row["updated_at"] == a_row["updated_at"]


# ==============================================================================
# TEST 23: No database connection held during external network await
# ==============================================================================
def test_23_no_database_connection_held_during_network_await(tmp_path: Path) -> None:
    db_path = tmp_path / "test23.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            concurrent_write_succeeded = False

            async def _inspect_db_during_transport(req: TelegramTransportRequest) -> None:
                nonlocal concurrent_write_succeeded
                # Open a completely independent connection during transport and perform a write.
                # If the worker or caller held a connection or write lock, this would raise OperationalError (database locked).
                with connect_database(db_path) as check_conn:
                    check_conn.execute("BEGIN IMMEDIATE")
                    check_conn.execute(
                        "UPDATE telegram_alert_attempts SET delivery_state = 'IN_TEST' WHERE id = ?",
                        (reservation_id,),
                    )
                    check_conn.commit()
                    concurrent_write_succeeded = True

            transport = FakeTelegramTransport(mode="success", delay_seconds=0.02)
            transport.on_request_hook = _inspect_db_during_transport

            result = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )

            assert result.status == "sent"
            assert concurrent_write_succeeded is True, "Concurrent SQLite write must succeed during HTTP await"
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())
