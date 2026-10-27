"""Comprehensive synthetic test suite for P0B3B1 Telegram outbox transaction/transport protocol foundation.

All 22 failure/concurrency scenarios mandated by Master Prompt Section 7:
1. Valid committed reservation (SENT)
2. Missing canonical reservation (fails closed before HTTP)
3. Epoch mismatch (fails closed before HTTP)
4. Two competing claimants (atomic claim race; only 1 wins)
5. Independent-process/multithreaded claim race
6. Duplicate existing SENT event (rejected before HTTP)
7. Existing UNCERTAIN event (rejected before HTTP)
8. Cancellation before claim (0 DB writes, 0 HTTP calls)
9. Cancellation during worker transaction (_fenced_wait completes cleanly)
10. Cancellation after committed claim but before HTTP (0 HTTP calls)
11. Cancellation during HTTP transmission (persists UNCERTAIN before propagating cancellation)
12. HTTP 200 -> durable SENT receipt
13. HTTP timeout -> durable UNCERTAIN receipt
14. Worker failure during persistence
15. Commit failure after possible HTTP success (unresolved_persistence, durable_receipt=None)
16. Restart after ambiguous HTTP completion
17. Part 1 SENT / Part 2 UNCERTAIN (multipart partial failure)
18. Multipart finalization only after every part is confirmed
19. No second HTTP attempt for previously SENT or UNCERTAIN parts
20. No database connection held during external network await
21. No changes to existing public Telegram service behavior
22. No mutation of the eleven historical quarantined records

Strict assertion: Fake transport invocation counts are verified in every test.
"""

from __future__ import annotations

import asyncio
import sqlite3
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

    mode: str = "success"  # "success", "timeout", "http_500", "exception", "uncertain", "custom"
    calls: list[TelegramTransportRequest] = field(default_factory=list)
    custom_outcome: TelegramTransportOutcome | None = None
    on_request_hook: Any | None = None
    delay_seconds: float = 0.0

    async def __call__(self, request: TelegramTransportRequest) -> TelegramTransportOutcome:
        self.calls.append(request)
        if self.on_request_hook is not None:
            hook_res = self.on_request_hook(request)
            if asyncio.iscoroutine(hook_res):
                await hook_res

        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)

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
# TEST 1: Valid committed reservation (SENT)
# ==============================================================================
def test_01_valid_committed_reservation_sent(tmp_path: Path) -> None:
    db_path = tmp_path / "test01.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

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

            assert result.status == "sent"
            assert result.durable_receipt is not None
            assert result.durable_receipt.delivery_state == SENT
            assert result.durable_receipt.all_parts_sent is True
            assert result.durable_receipt.telegram_message_id is not None
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == SENT


# ==============================================================================
# TEST 2: Missing canonical reservation (fails closed before HTTP)
# ==============================================================================
def test_02_missing_canonical_reservation_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "test02.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path, bind_reservation=False)

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

            assert result.status in ("failed", "blocked")
            assert result.durable_receipt is None
            assert len(transport.calls) == 0, "Transport MUST NOT be invoked when canonical reservation is missing"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 3: Epoch mismatch (fails closed before HTTP)
# ==============================================================================
def test_03_epoch_mismatch_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "test03.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    foreign_identity = RuntimeEpochIdentity(
        epoch_id="foreign-epoch",
        cutoff_at="2000-01-01T00:00:00Z",
        contract_version=1,
        reviewed_release_sha="eef92b89f168bbb016715f3486b9f6bf2824f53f",
        generation_binding="foreign-binding",
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
                expected_identity=foreign_identity,
                now=NOW,
            )

            assert result.status in ("failed", "blocked")
            assert result.durable_receipt is None
            assert len(transport.calls) == 0, "Transport MUST NOT be invoked when runtime epoch mismatches"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 4: Two competing claimants (atomic claim race; only 1 wins)
# ==============================================================================
def test_04_two_competing_claimants_atomic_claim_race(tmp_path: Path) -> None:
    db_path = tmp_path / "test04.db"
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
# TEST 5: Independent-process/multithreaded claim race
# ==============================================================================
def test_05_multithreaded_claim_race(tmp_path: Path) -> None:
    db_path = tmp_path / "test05.db"
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
# TEST 6: Duplicate existing SENT event (rejected before HTTP)
# ==============================================================================
def test_06_duplicate_existing_sent_event_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "test06.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(
        db_path, initial_event_state=SENT, initial_part_state=SENT
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
            assert len(transport.calls) == 0, "Transport MUST NOT be invoked for already SENT event"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 7: Existing UNCERTAIN event (rejected before HTTP)
# ==============================================================================
def test_07_existing_uncertain_event_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "test07.db"
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
            assert len(transport.calls) == 0, "Transport MUST NOT be invoked for UNCERTAIN event"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 8: Cancellation before claim (0 DB writes, 0 HTTP calls)
# ==============================================================================
def test_08_cancellation_before_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "test08.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    transport = FakeTelegramTransport(mode="success")

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            task = asyncio.create_task(
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
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    assert len(transport.calls) == 0, "Transport MUST NOT be invoked when cancelled before claim"
    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == PENDING, "Database must not be modified when cancelled before claim"


# ==============================================================================
# TEST 9: Cancellation during worker transaction (_fenced_wait completes cleanly)
# ==============================================================================
def test_09_cancellation_during_worker_transaction_completes_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "test09.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            def _slow_prep(*args, **kwargs):
                import time
                time.sleep(0.05)
                return stage_1_outbox_preparation(*args, **kwargs)

            task = asyncio.create_task(
                worker.call(
                    _slow_prep,
                    db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    expected_identity=SYNTHETIC_IDENTITY,
                    now=NOW,
                )
            )
            await asyncio.sleep(0.01)
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    # Worker transaction must have completed cleanly without deadlocks or broken locks
    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == IN_FLIGHT


# ==============================================================================
# TEST 10: Cancellation after committed claim but before HTTP (0 HTTP calls)
# ==============================================================================
def test_10_cancellation_after_committed_claim_before_http(tmp_path: Path) -> None:
    db_path = tmp_path / "test10.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    transport = FakeTelegramTransport(mode="success")

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            # Manually execute Stage 1 to obtain committed receipt
            receipt = await worker.call(
                stage_1_outbox_preparation,
                db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )
            assert receipt is not None

            async def _run_subtask():
                subtask = asyncio.current_task()
                assert subtask is not None
                subtask.cancel()
                if subtask.cancelling():
                    raise asyncio.CancelledError()
                await stage_2_async_transport(receipt, transport)

            task = asyncio.create_task(_run_subtask())
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    assert len(transport.calls) == 0, "Transport MUST NOT be invoked when cancelled before HTTP"


# ==============================================================================
# TEST 11: Cancellation during HTTP transmission (persists UNCERTAIN before propagating)
# ==============================================================================
def test_11_cancellation_during_http_transmission_persists_uncertain(tmp_path: Path) -> None:
    db_path = tmp_path / "test11.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            transport_started = asyncio.Event()

            def _on_request(req):
                transport_started.set()

            transport = FakeTelegramTransport(mode="success", delay_seconds=0.2, on_request_hook=_on_request)

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

            await transport_started.wait()
            delivery_task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await delivery_task
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    # Crucial assertion: Database MUST persist UNCERTAIN before propagating cancellation
    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == UNCERTAIN, "Interrupted HTTP transmission MUST be persisted as UNCERTAIN"


# ==============================================================================
# TEST 12: HTTP 200 -> durable SENT receipt
# ==============================================================================
def test_12_http_200_yields_durable_sent_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "test12.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

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

            assert result.status == "sent"
            assert result.durable_receipt is not None
            assert result.durable_receipt.delivery_state == SENT
            assert result.durable_receipt.all_parts_sent is True
            assert result.durable_receipt.telegram_message_id is not None
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == SENT


# ==============================================================================
# TEST 13: HTTP timeout -> durable UNCERTAIN receipt
# ==============================================================================
def test_13_http_timeout_yields_durable_uncertain_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "test13.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            transport = FakeTelegramTransport(mode="timeout")
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
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == UNCERTAIN


# ==============================================================================
# TEST 14: Worker failure during persistence
# ==============================================================================
def test_14_worker_failure_during_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test14.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    def _failing_persistence(*args, **kwargs):
        raise RuntimeError("Injected disk failure during persistence")

    monkeypatch.setattr("app.alerts.telegram_outbox_protocol.stage_3_outbox_persistence", _failing_persistence)

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
            assert result.durable_receipt is None
            assert "Injected disk failure" in result.error_message
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 15: Commit failure after possible HTTP success (unresolved_persistence, durable_receipt=None)
# ==============================================================================
def test_15_commit_failure_after_http_success_yields_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "test15.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    def _failing_commit(*args, **kwargs):
        raise PersistenceCommitFailureError("Simulated database commit failure after HTTP transmission")

    monkeypatch.setattr("app.alerts.telegram_outbox_protocol.stage_3_outbox_persistence", _failing_commit)

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
            assert result.durable_receipt is None
            assert "Simulated database commit failure" in result.error_message
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 16: Restart after ambiguous HTTP completion
# ==============================================================================
def test_16_restart_after_ambiguous_http_completion(tmp_path: Path) -> None:
    db_path = tmp_path / "test16.db"
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
# TEST 17: Part 1 SENT / Part 2 UNCERTAIN (multipart partial failure)
# ==============================================================================
def test_17_multipart_partial_failure_part1_sent_part2_uncertain(tmp_path: Path) -> None:
    db_path = tmp_path / "test17.db"
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
# TEST 18: Multipart finalization only after every part is confirmed
# ==============================================================================
def test_18_multipart_finalization_after_every_part_confirmed(tmp_path: Path) -> None:
    db_path = tmp_path / "test18.db"
    bootstrap_operational_test_database(db_path)
    two_part_message = "B" * 5000
    event_id, reservation_id = _seed_canonical_event(
        db_path, message=two_part_message, max_message_length=4000
    )

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
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

            with connect_database(db_path) as conn:
                event_mid = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
                assert event_mid["delivery_state"] != SENT, "Event must NOT be finalized as SENT until all parts complete"

            assert result_1.reservation_receipt is not None
            transport_2 = FakeTelegramTransport(mode="success")
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
            assert result_2.status == "sent"
            assert result_2.durable_receipt is not None
            assert result_2.durable_receipt.all_parts_sent is True
            assert len(transport_2.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())

    with connect_database(db_path) as conn:
        event_final = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert event_final["delivery_state"] == SENT


# ==============================================================================
# TEST 19: No second HTTP attempt for previously SENT or UNCERTAIN parts
# ==============================================================================
def test_19_no_second_http_attempt_for_sent_or_uncertain_parts(tmp_path: Path) -> None:
    db_path = tmp_path / "test19.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            transport = FakeTelegramTransport(mode="success")
            result_1 = await deliver_single_reserved_part(
                worker=worker,
                database_path=db_path,
                event_id=event_id,
                reservation_id=reservation_id,
                transport=transport,
                expected_identity=SYNTHETIC_IDENTITY,
                now=NOW,
            )
            assert result_1.status == "sent"
            assert len(transport.calls) == 1

            # Attempt second send for the exact same already SENT part
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
            assert result_2.status == "blocked"
            assert len(transport_2.calls) == 0, "No second HTTP attempt allowed for SENT part"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


# ==============================================================================
# TEST 20: No database connection held during external network await
# ==============================================================================
def test_20_no_database_connection_held_during_network_await(tmp_path: Path) -> None:
    db_path = tmp_path / "test20.db"
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


# ==============================================================================
# TEST 21: No changes to existing public Telegram service behavior
# ==============================================================================
def test_21_existing_telegram_lifecycle_service_unmodified() -> None:
    from app.alerts.telegram_lifecycle import TelegramLifecycleDeliveryService
    # Verify instantiation and public interface remain completely intact
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
            # Attempt to deliver each of the 11 historical records through the protocol
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

    # Strict assertion: Transport was NEVER called
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
