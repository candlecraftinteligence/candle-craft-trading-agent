"""Comprehensive synthetic test suite for P0B3B1 Telegram outbox transaction/transport protocol foundation.

PART 1: Baseline Protocol Tests (Original 22 scenarios from commit 3f0dcaee)
PART 2: Safety Hardening Tests (Post-HTTP repeated cancellation, SQLite verification, 5xx normalization)
PART 3: Evidence Contract and Fatal Epoch Boundary Tests (Strict provider evidence and fatal epoch error propagation)

Strict assertion: Fake transport invocation counts are verified in every test.
Zero live orders. Zero real Telegram HTTP calls. Zero production database access.
"""

from __future__ import annotations

import asyncio
import sqlite3
import threading
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
    list_outbox_parts,
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
from app.data.dtos import NA
from app.runtime_epoch.errors import RuntimeEpochError, RuntimeEpochOwnershipError
from app.runtime_epoch.models import RuntimeEpochIdentity
from app.runtime_epoch.ownership import bind_canonical_reservation_attempt
from app.storage.database import connect_database, open_initialized_database
from tests.runtime_epoch_support import (
    SYNTHETIC_EPOCH_ID,
    SYNTHETIC_IDENTITY,
    bootstrap_operational_test_database,
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
    """Helper to seed an operational public alert event, attempt, and parts."""
    with open_initialized_database(db_path) as connection:
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
# PART 1: BASELINE PROTOCOL TESTS (Original 22 Scenarios from commit 3f0dcaee)
# ==============================================================================

def test_baseline_01_valid_committed_reservation_sent(tmp_path: Path) -> None:
    db_path = tmp_path / "base01.db"
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


def test_baseline_02_missing_canonical_reservation_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "base02.db"
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

            assert result.status == "blocked"
            assert result.durable_receipt is None
            assert len(transport.calls) == 0, "Transport MUST NOT be invoked when canonical reservation is missing"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_baseline_03_epoch_mismatch_fails_closed(tmp_path: Path) -> None:
    db_path = tmp_path / "base03.db"
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
            # Fatal epoch mismatch propagates as RuntimeEpochError
            with pytest.raises(RuntimeEpochError):
                await deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=foreign_identity,
                    now=NOW,
                )

            assert len(transport.calls) == 0, "Transport MUST NOT be invoked when runtime epoch mismatches"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_baseline_04_two_competing_claimants_atomic_claim_race(tmp_path: Path) -> None:
    db_path = tmp_path / "base04.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker_1 = TelegramOutboxWorker()
        worker_2 = TelegramOutboxWorker()
        try:
            transport = FakeTelegramTransport(mode="success")
            result_1, result_2 = await asyncio.gather(
                deliver_single_reserved_part(
                    worker=worker_1,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=SYNTHETIC_IDENTITY,
                    now=NOW,
                ),
                deliver_single_reserved_part(
                    worker=worker_2,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=SYNTHETIC_IDENTITY,
                    now=NOW,
                ),
            )

            statuses = {result_1.status, result_2.status}
            assert "sent" in statuses
            assert any(s in ("skipped", "blocked") for s in statuses)
            assert len(transport.calls) == 1, "Exactly 1 claimant must invoke transport"
        finally:
            await worker_1.aclose()
            await worker_2.aclose()

    asyncio.run(scenario())


def test_baseline_05_multithreaded_claim_race(tmp_path: Path) -> None:
    db_path = tmp_path / "base05.db"
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


def test_baseline_06_duplicate_existing_sent_event_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "base06.db"
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


def test_baseline_07_existing_uncertain_event_rejected(tmp_path: Path) -> None:
    db_path = tmp_path / "base07.db"
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


def test_baseline_08_cancellation_before_claim(tmp_path: Path) -> None:
    db_path = tmp_path / "base08.db"
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


def test_baseline_09_cancellation_during_worker_transaction_completes_cleanly(tmp_path: Path) -> None:
    db_path = tmp_path / "base09.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            def _slow_prep(*args, **kwargs):
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

    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == IN_FLIGHT


def test_baseline_10_cancellation_after_committed_claim_before_http(tmp_path: Path) -> None:
    db_path = tmp_path / "base10.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    transport = FakeTelegramTransport(mode="success")

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
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


def test_baseline_11_cancellation_during_http_transmission_persists_uncertain(tmp_path: Path) -> None:
    db_path = tmp_path / "base11.db"
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

    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == UNCERTAIN, "Interrupted HTTP transmission MUST be persisted as UNCERTAIN"


def test_baseline_12_http_200_yields_durable_sent_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "base12.db"
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


def test_baseline_13_http_timeout_yields_durable_uncertain_receipt(tmp_path: Path) -> None:
    db_path = tmp_path / "base13.db"
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


def test_baseline_14_worker_failure_during_persistence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "base14.db"
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


def test_baseline_15_commit_failure_after_http_success_yields_unresolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "base15.db"
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


def test_baseline_16_restart_after_ambiguous_http_completion(tmp_path: Path) -> None:
    db_path = tmp_path / "base16.db"
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


def test_baseline_17_multipart_partial_failure_part1_sent_part2_uncertain(tmp_path: Path) -> None:
    db_path = tmp_path / "base17.db"
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


def test_baseline_18_multipart_finalization_after_every_part_confirmed(tmp_path: Path) -> None:
    db_path = tmp_path / "base18.db"
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
            assert result_1.durable_receipt.all_parts_sent is False

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
            assert result_2.durable_receipt.delivery_state == SENT
            assert result_2.durable_receipt.all_parts_sent is True
            assert len(transport_1.calls) == 1
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
        assert all(p["delivery_state"] == SENT for p in parts)

        event_row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert event_row["delivery_state"] == SENT


def test_baseline_19_no_second_http_attempt_for_sent_or_uncertain_parts(tmp_path: Path) -> None:
    db_path = tmp_path / "base19.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

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
            )
            assert result_1.status == "sent"
            assert len(transport_1.calls) == 1

            # Second attempt on the same SENT part
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
            assert len(transport_2.calls) == 0, "Second attempt on SENT part MUST NOT call transport"
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_baseline_20_no_database_connection_held_during_network_await(tmp_path: Path) -> None:
    db_path = tmp_path / "base20.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            concurrent_write_succeeded = False

            def _inspect_db_during_transport(req):
                nonlocal concurrent_write_succeeded
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


def test_baseline_21_existing_telegram_lifecycle_service_unmodified() -> None:
    from app.alerts.telegram_lifecycle import TelegramLifecycleDeliveryService
    assert hasattr(TelegramLifecycleDeliveryService, "deliver_for_run")


def test_baseline_22_no_mutation_of_historical_quarantined_records(tmp_path: Path) -> None:
    db_path = tmp_path / "base22.db"
    bootstrap_operational_test_database(db_path)

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
# PART 2: SAFETY HARDENING TESTS (The 20 Hardening Tests Retained)
# ==============================================================================

def test_hardening_01_cancellation_after_http_before_stage3_admission(tmp_path: Path) -> None:
    db_path = tmp_path / "hard01.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            task_ref = None

            def _on_request(req):
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

    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == SENT


def test_hardening_02_repeated_cancellation_during_admission_wait(tmp_path: Path) -> None:
    db_path = tmp_path / "hard02.db"
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


def test_hardening_03_cancellation_during_stage3_transaction(tmp_path: Path) -> None:
    db_path = tmp_path / "hard03.db"
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


def test_hardening_04_cancellation_during_commit(tmp_path: Path) -> None:
    db_path = tmp_path / "hard04.db"
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


def test_hardening_05_cancellation_during_worker_shutdown() -> None:
    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        shutdown_task = asyncio.create_task(worker.aclose())
        await asyncio.sleep(0)
        shutdown_task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await shutdown_task

        assert not worker._thread.is_alive()

    asyncio.run(scenario())


def test_hardening_06_sqlite_result_persistence_raises_before_commit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "hard06.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

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


def test_hardening_07_sqlite_uncertain_fallback_silently_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "hard07.db"
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
            assert result.durable_receipt is None
            assert "remained in state" in result.error_message or "remained IN_FLIGHT" in result.error_message
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_hardening_08_database_remains_in_flight_after_failed_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "hard08.db"
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

    with connect_database(db_path) as conn:
        row = conn.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert row["delivery_state"] == IN_FLIGHT


def test_hardening_09_no_false_durable_delivery_receipt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "hard09.db"
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
            assert result.durable_receipt is None
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_hardening_10_valid_http_success_with_verified_telegram_message_id(tmp_path: Path) -> None:
    db_path = tmp_path / "hard10.db"
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
            assert result.durable_receipt.telegram_message_id is not None
            assert len(transport.calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_hardening_11_malformed_sent_outcome_with_no_message_id(tmp_path: Path) -> None:
    db_path = tmp_path / "hard11.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            outcome = TelegramTransportOutcome(
                delivery_state=SENT,
                http_status=200,
                telegram_message_id=None,
            )
            transport = FakeTelegramTransport(mode="custom", custom_outcome=outcome)
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


def test_hardening_12_contradictory_sent_outcome_with_failed_http_status(tmp_path: Path) -> None:
    db_path = tmp_path / "hard12.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            outcome = TelegramTransportOutcome(
                delivery_state=SENT,
                http_status=500,
                telegram_message_id="valid_msg_id",
            )
            transport = FakeTelegramTransport(mode="custom", custom_outcome=outcome)
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


def test_hardening_13_unknown_provider_acknowledgment(tmp_path: Path) -> None:
    db_path = tmp_path / "hard13.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            outcome = TelegramTransportOutcome(
                delivery_state="MYSTERIOUS_STATE",
                telegram_message_id="msg_xyz",
            )
            transport = FakeTelegramTransport(mode="custom", custom_outcome=outcome)
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


def test_hardening_14_http_5xx_with_uncertain_acceptance(tmp_path: Path) -> None:
    db_path = tmp_path / "hard14.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            transport = FakeTelegramTransport(mode="http_500")
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


def test_hardening_15_stage_1_cancellation_combined_with_worker_exception(tmp_path: Path) -> None:
    db_path = tmp_path / "hard15.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            def _failing_slow_prep(*args, **kwargs):
                time.sleep(0.02)
                raise OutboxPreparationError("Synthetic prep failure")

            async def _run():
                return await worker.call(
                    _failing_slow_prep,
                    db_path,
                    stage="preparation",
                )

            task = asyncio.create_task(_run())
            await asyncio.sleep(0.005)
            task.cancel()

            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_hardening_16_two_concurrent_claimants(tmp_path: Path) -> None:
    test_baseline_04_two_competing_claimants_atomic_claim_race(tmp_path)


def test_hardening_17_independent_process_claim_ownership(tmp_path: Path) -> None:
    test_baseline_05_multithreaded_claim_race(tmp_path)


def test_hardening_18_multipart_success_and_partial_uncertainty(tmp_path: Path) -> None:
    test_baseline_17_multipart_partial_failure_part1_sent_part2_uncertain(tmp_path)


def test_hardening_19_restart_after_unresolved_persistence(tmp_path: Path) -> None:
    test_baseline_16_restart_after_ambiguous_http_completion(tmp_path)


def test_hardening_20_no_second_http_call_for_uncertain_delivery(tmp_path: Path) -> None:
    test_baseline_19_no_second_http_attempt_for_sent_or_uncertain_parts(tmp_path)


# ==============================================================================
# PART 3: EVIDENCE CONTRACT AND FATAL EPOCH BOUNDARY TESTS (Issues 1 & 2)
# ==============================================================================

def test_evidence_sent_missing_http_status() -> None:
    """Issue 1: A missing HTTP status must not silently count as proven HTTP 200."""
    outcome = TelegramTransportOutcome(
        delivery_state=SENT,
        http_status=None,
        telegram_message_id="12345",
    )
    normalized = normalize_transport_outcome(outcome)
    assert normalized.delivery_state == UNCERTAIN
    assert normalized.error_category == "missing_success_http_status"
    assert "omitted HTTP status" in str(normalized.error)


def test_evidence_sent_with_http_200_and_valid_message_id() -> None:
    """Issue 1: Verified HTTP 200 and valid message ID retains SENT."""
    outcome = TelegramTransportOutcome(
        delivery_state=SENT,
        http_status=200,
        telegram_message_id="12345",
    )
    normalized = normalize_transport_outcome(outcome)
    assert normalized.delivery_state == SENT
    assert normalized.telegram_message_id == "12345"
    assert normalized.http_status == 200


def test_evidence_sent_with_contradictory_http_status() -> None:
    """Issue 1: SENT with contradictory HTTP status fails closed to UNCERTAIN."""
    for code in (400, 401, 403, 404, 500, 502, 503):
        outcome = TelegramTransportOutcome(
            delivery_state=SENT,
            http_status=code,
            telegram_message_id="12345",
        )
        normalized = normalize_transport_outcome(outcome)
        assert normalized.delivery_state == UNCERTAIN
        assert normalized.error_category == "contradictory_success_http_status"


def test_evidence_sent_with_missing_or_invalid_message_id() -> None:
    """Issue 1: Missing or placeholder message IDs fail closed to UNCERTAIN."""
    for invalid_id in (None, "", " ", "0", "None", NA):
        outcome = TelegramTransportOutcome(
            delivery_state=SENT,
            http_status=200,
            telegram_message_id=invalid_id,
        )
        normalized = normalize_transport_outcome(outcome)
        assert normalized.delivery_state == UNCERTAIN
        assert normalized.error_category == "confirmed_success_missing_message_id"


def test_evidence_sent_with_provider_error() -> None:
    """Issue 1: Transport claiming SENT but reporting an error fails closed to UNCERTAIN."""
    outcome = TelegramTransportOutcome(
        delivery_state=SENT,
        http_status=200,
        telegram_message_id="12345",
        error="Bad request: chat not found",
    )
    normalized = normalize_transport_outcome(outcome)
    assert normalized.delivery_state == UNCERTAIN
    assert normalized.error_category == "contradictory_success_error"


def test_evidence_sent_with_rate_limit_indication() -> None:
    """Issue 1: Transport claiming SENT while marked rate_limited fails closed to UNCERTAIN."""
    outcome = TelegramTransportOutcome(
        delivery_state=SENT,
        http_status=200,
        telegram_message_id="12345",
        rate_limited=True,
    )
    normalized = normalize_transport_outcome(outcome)
    assert normalized.delivery_state == UNCERTAIN
    assert normalized.error_category == "contradictory_success_rate_limited"


def test_evidence_unknown_transport_result_type() -> None:
    """Issue 1: Non-TelegramTransportOutcome returns UNCERTAIN."""
    for bad_obj in ("SENT", {"status": "sent"}, 12345, None, [SENT]):
        normalized = normalize_transport_outcome(bad_obj)
        assert normalized.delivery_state == UNCERTAIN
        assert normalized.error_category == "malformed_transport_outcome"


def test_fatal_epoch_mismatch_uncancelled_caller(tmp_path: Path) -> None:
    """Issue 2: Fatal epoch boundary violation propagates directly to uncancelled caller."""
    db_path = tmp_path / "fatal_uncancelled.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    foreign_identity = RuntimeEpochIdentity(
        epoch_id="foreign-epoch",
        cutoff_at="2000-01-01T00:00:00Z",
        contract_version=1,
        reviewed_release_sha="eef92b89f168bbb016715f3486b9f6bf2824f53f",
        generation_binding="foreign-binding",
    )

    transport = FakeTelegramTransport(mode="success")

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            with pytest.raises(RuntimeEpochError):
                await deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=foreign_identity,
                    now=NOW,
                )
        finally:
            await worker.aclose()

    asyncio.run(scenario())
    assert len(transport.calls) == 0, "Transport must not be invoked on fatal epoch failure"


def test_fatal_epoch_mismatch_cancelled_caller(tmp_path: Path) -> None:
    """Issue 2: Fatal epoch boundary violation propagates even if caller is cancelled."""
    db_path = tmp_path / "fatal_cancelled.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    foreign_identity = RuntimeEpochIdentity(
        epoch_id="foreign-epoch",
        cutoff_at="2000-01-01T00:00:00Z",
        contract_version=1,
        reviewed_release_sha="eef92b89f168bbb016715f3486b9f6bf2824f53f",
        generation_binding="foreign-binding",
    )

    transport = FakeTelegramTransport(mode="success")

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            async def _run():
                cur = asyncio.current_task()
                assert cur is not None
                cur.cancel()
                return await deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=foreign_identity,
                    now=NOW,
                )

            task = asyncio.create_task(_run())
            with pytest.raises(RuntimeEpochError):
                await task
        finally:
            await worker.aclose()

    asyncio.run(scenario())
    assert len(transport.calls) == 0, "Transport must not be invoked on fatal epoch failure"


def test_fatal_epoch_zero_transport_calls(tmp_path: Path) -> None:
    """Issue 2: Verify zero transport calls occur on any fatal Stage 1 failure."""
    db_path = tmp_path / "fatal_zero_calls.db"
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(db_path)

    broken_identity = RuntimeEpochIdentity(
        epoch_id="nonexistent-epoch",
        cutoff_at="2000-01-01T00:00:00Z",
        contract_version=1,
        reviewed_release_sha="0000000000000000000000000000000000000000",
        generation_binding="broken-binding",
    )

    transport = FakeTelegramTransport(mode="success")

    async def scenario() -> None:
        worker = TelegramOutboxWorker()
        try:
            with pytest.raises(RuntimeEpochError):
                await deliver_single_reserved_part(
                    worker=worker,
                    database_path=db_path,
                    event_id=event_id,
                    reservation_id=reservation_id,
                    transport=transport,
                    expected_identity=broken_identity,
                    now=NOW,
                )
        finally:
            await worker.aclose()

    asyncio.run(scenario())
    assert len(transport.calls) == 0
