"""Narrowly bounded three-stage Telegram outbox transaction/transport protocol foundation.

Stage 1: Worker-Owned Preparation / Claim -> CommittedPartReservationReceipt
Stage 2: Async Injected Transport (Event Loop) -> TelegramTransportOutcome
Stage 3: Worker-Owned Result Persistence -> DurableDeliveryReceipt

Zero SQLite connections or transactions escape worker boundaries or span external network awaits.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import queue
import sqlite3
import threading
import time
from collections.abc import Awaitable, Callable
from contextvars import copy_context
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.alerts.telegram_outbox import (
    FAILED_FINAL,
    IN_FLIGHT,
    PENDING,
    RETRYABLE,
    SENT,
    SKIPPED_DRY_RUN,
    UNCERTAIN,
    SQLitePublicTelegramOutbox,
    list_outbox_parts,
)
from app.data.dtos import NA
from app.runtime_epoch.authority import load_active_runtime_epoch, require_active_runtime_epoch
from app.runtime_epoch.errors import RuntimeEpochError, RuntimeEpochOwnershipError
from app.runtime_epoch.models import RuntimeEpochIdentity
from app.runtime_epoch.ownership import (
    decide_public_effect,
    require_canonical_reservation_for_public_effect,
    require_event_part_association,
    require_event_reservation_association,
)
from app.runtime_epoch.startup import open_repository_database


class TelegramOutboxProtocolError(RuntimeError):
    """Base error for Telegram outbox protocol failures."""


class OutboxPreparationError(TelegramOutboxProtocolError):
    """Raised when Stage 1 preparation fails validation."""


class OutboxClaimRejected(TelegramOutboxProtocolError):
    """Raised when outbox atomic claim is not granted."""


class SupersededClaimError(TelegramOutboxProtocolError):
    """Raised when a claim attempt ID has been superseded before persistence."""


class PersistenceCommitFailureError(TelegramOutboxProtocolError):
    """Raised when database commit fails after HTTP transmission."""


class TelegramOutboxWorkerClosed(RuntimeError):
    """No work is admitted once the outbox worker has closed."""


class TelegramOutboxWorkerSaturated(RuntimeError):
    """A second waiting payload is refused by the bounded worker."""


@dataclass(frozen=True)
class CommittedPartReservationReceipt:
    """Detached, immutable receipt produced by worker-owned preparation."""

    event_id: int
    reservation_id: int
    event_key: str
    attempt_id: str
    part_id: int
    part_index: int
    part_count: int
    payload_text: str
    payload_hash: str
    destination_chat_id: str
    destination_kind: str
    message_hash: str
    attempt_count: int
    committed_at: str


@dataclass(frozen=True)
class TelegramTransportRequest:
    """Detached request payload delivered to external async transport on the event loop."""

    event_id: int
    reservation_id: int
    event_key: str
    attempt_id: str
    part_id: int
    part_index: int
    part_count: int
    payload_text: str
    payload_hash: str
    destination_chat_id: str
    destination_kind: str
    message_hash: str


@dataclass(frozen=True)
class TelegramTransportOutcome:
    """Outcome classification returned by external async transport."""

    delivery_state: str  # SENT, RETRYABLE, UNCERTAIN, FAILED_FINAL
    http_status: int | None = None
    telegram_message_id: str | None = None
    telegram_chat_id: str | None = None
    sent_at: str | None = None
    error_category: str | None = None
    error: str | None = None
    rate_limited: bool = False
    retry_after: float | None = None


@dataclass(frozen=True)
class DurableDeliveryReceipt:
    """Detached, immutable completion receipt confirming durable SQLite persistence."""

    event_id: int
    reservation_id: int
    part_id: int
    attempt_id: str
    delivery_state: str
    telegram_message_id: str | None = None
    persisted_at: str = ""
    all_parts_sent: bool = False
    transaction_result: str = "COMMITTED"


@dataclass(frozen=True)
class TelegramDeliveryProtocolResult:
    """Final outcome of the three-stage protocol run for one reserved part."""

    status: str  # "sent", "retryable", "uncertain", "failed", "skipped", "unresolved_persistence"
    reservation_receipt: CommittedPartReservationReceipt | None = None
    transport_outcome: TelegramTransportOutcome | None = None
    durable_receipt: DurableDeliveryReceipt | None = None
    error_message: str = NA
    detail: str = ""


@dataclass(frozen=True)
class _WorkerReceipt:
    value: Any
    error: BaseException | None
    started: float
    finished: float
    transaction_result: str
    connection_closed: bool | None


async def _fenced_wait(future: asyncio.Future[Any]) -> tuple[Any, BaseException | None]:
    """Delay cooperative cancellation until completion, without cancelling the worker task."""
    cancelled: BaseException | None = None
    while True:
        try:
            return await asyncio.shield(future), cancelled
        except (asyncio.CancelledError, KeyboardInterrupt) as exc:
            if future.cancelled():
                raise RuntimeError("telegram outbox completion owner was cancelled") from exc
            cancelled = cancelled or exc


class TelegramOutboxWorker:
    """Bounded, serialized worker thread owning SQLite preparation and persistence transactions.

    Permits at most one in-flight operation and one waiting caller.
    Never accepts or returns live SQLite connections.
    """

    def __init__(self) -> None:
        self._queue: queue.Queue[Any] = queue.Queue(maxsize=1)
        self._admission = asyncio.Lock()
        self._waiters = 0
        self._closed = False
        self._inflight = False
        self._closing: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._run, name="cci-telegram-outbox", daemon=False)
        self._completed = 0
        self._failed = 0
        self._cancelled = 0
        self._rejected = 0
        self._thread.start()

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("telegram outbox worker belongs to a different event loop")

    async def call(self, fn: Callable[..., Any], /, *args: Any, stage: str | None = None, **kwargs: Any) -> Any:
        self._bind_loop()
        if self._closed:
            raise TelegramOutboxWorkerClosed("telegram outbox worker is closed")
        if any(isinstance(value, sqlite3.Connection) for value in (*args, *kwargs.values())):
            raise TypeError("telegram outbox worker requires worker-owned connections")

        task = asyncio.current_task()
        uncancel_count = 0
        if stage == "persistence" and task is not None:
            while task.cancelling():
                task.uncancel()
                uncancel_count += 1
        elif task is not None and task.cancelling():
            raise asyncio.CancelledError()

        waiting = self._admission.locked()
        if waiting:
            if self._waiters >= 1:
                self._rejected += 1
                raise TelegramOutboxWorkerSaturated("one telegram outbox operation and one waiter are permitted")
            self._waiters += 1

        lock_cancelled: BaseException | None = None
        try:
            if stage == "persistence":
                acquire_task = asyncio.create_task(self._admission.acquire())
                _, lock_cancelled = await _fenced_wait(acquire_task)
                if lock_cancelled is not None and task is not None:
                    while task.cancelling():
                        task.uncancel()
                        uncancel_count += 1
            else:
                await self._admission.acquire()
        finally:
            if waiting:
                self._waiters -= 1

        try:
            if self._closed:
                raise TelegramOutboxWorkerClosed("telegram outbox worker is closed")
            context = copy_context()
            completion: concurrent.futures.Future[_WorkerReceipt] = concurrent.futures.Future()
            self._queue.put_nowait((context, fn, args, kwargs, completion))
            self._inflight = True
            receipt, cancelled = await _fenced_wait(asyncio.wrap_future(completion))
            self._completed += 1
            if cancelled is not None:
                self._cancelled += 1
            if lock_cancelled is not None and cancelled is None:
                cancelled = lock_cancelled

            if receipt.error is not None:
                self._failed += 1
                if stage == "preparation" and (cancelled is not None or uncancel_count > 0):
                    if isinstance(receipt.error, (RuntimeEpochOwnershipError, RuntimeEpochError, sqlite3.DatabaseError)):
                        raise receipt.error from (cancelled or asyncio.CancelledError())
                    raise (cancelled or asyncio.CancelledError()) from receipt.error
                raise receipt.error

            if stage == "persistence":
                if (uncancel_count > 0 or cancelled is not None) and task is not None:
                    task.cancel()
                return receipt.value

            if uncancel_count > 0 and cancelled is None:
                cancelled = asyncio.CancelledError()
            if cancelled is not None:
                raise cancelled
            return receipt.value
        finally:
            self._inflight = False
            self._admission.release()

    async def aclose(self) -> None:
        """Stop admission, flush remaining task, and join the worker thread."""
        self._bind_loop()
        if self._closing is None:
            self._closed = True
            self._closing = asyncio.create_task(self._shutdown(), name="cci-telegram-outbox-shutdown")
        _, cancelled = await _fenced_wait(self._closing)
        if cancelled is not None:
            raise cancelled

    async def _shutdown(self) -> None:
        async with self._admission:
            self._queue.put_nowait(None)
            await asyncio.to_thread(self._thread.join)

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is None:
                    return
                context, fn, args, kwargs, completion = item
                started = time.perf_counter()
                value = None
                error: BaseException | None = None
                try:
                    value = context.run(fn, *args, **kwargs)
                except BaseException as exc:
                    error = exc
                finished = time.perf_counter()
                completion.set_result(
                    _WorkerReceipt(
                        value=value,
                        error=error,
                        started=started,
                        finished=finished,
                        transaction_result="FAILED" if error else "COMMITTED",
                        connection_closed=True,
                    )
                )
            finally:
                self._queue.task_done()


def _now_utc_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def stage_1_outbox_preparation(
    database_path: Path | str,
    *,
    event_id: int,
    reservation_id: int,
    expected_identity: RuntimeEpochIdentity | None = None,
    now: str | None = None,
    owner: str = "p0b3b1-worker",
    lease_seconds: int = 300,
    part_index: int = 1,
    attempt_id: str | None = None,
) -> CommittedPartReservationReceipt:
    """Stage 1: Worker-owned preparation, validation, atomic claim, and IN_FLIGHT transition.

    Runs synchronously on the dedicated outbox worker thread.
    Opens, validates, claims, marks IN_FLIGHT, commits, and closes SQLite.
    Returns only detached, immutable receipt data.
    """
    timestamp = now or _now_utc_iso()
    connection = open_repository_database(database_path, expected_identity=expected_identity)
    try:
        epoch = load_active_runtime_epoch(connection)
        if epoch is None:
            raise RuntimeEpochOwnershipError("active_runtime_epoch_missing")

        require_event_reservation_association(connection, event_id=int(event_id), reservation_id=int(reservation_id))
        require_canonical_reservation_for_public_effect(connection, event_id=int(event_id))

        event_row = connection.execute(
            "SELECT * FROM public_alert_events WHERE id = ?",
            (int(event_id),),
        ).fetchone()
        if event_row is None:
            raise OutboxPreparationError(f"public_alert_event_{event_id}_not_found")

        decision = decide_public_effect(connection, event_row)
        if not decision.allowed:
            raise OutboxPreparationError(f"public_effect_disallowed:{decision.reason}")

        current_delivery_state = str(event_row["delivery_state"] or "").strip().upper()
        if current_delivery_state == SENT:
            raise OutboxPreparationError("event_already_sent")
        if current_delivery_state == UNCERTAIN:
            raise OutboxPreparationError("event_delivery_uncertain")
        if current_delivery_state not in ("PENDING", "RETRYABLE", "IN_FLIGHT"):
            raise OutboxPreparationError(f"event_state_not_claimable:{current_delivery_state.lower()}")

        outbox = SQLitePublicTelegramOutbox(connection)
        if attempt_id is not None and current_delivery_state == IN_FLIGHT:
            if str(event_row["attempt_id"] or "") != attempt_id:
                raise SupersededClaimError("claim_attempt_mismatch")
            parts = list_outbox_parts(connection, event_id=int(event_id))
            part = next((p for p in parts if p.part_index == int(part_index)), None)
            if part is None:
                raise OutboxPreparationError(f"part_index_{part_index}_not_found_in_claim")
            if part.delivery_state == SENT:
                raise OutboxPreparationError(f"part_{part_index}_already_sent")

            in_flight_marked = outbox.mark_part_in_flight(part_id=part.id, attempt_id=attempt_id)
            if not in_flight_marked:
                outbox.mark_uncertain_after_persistence_failure(
                    event_id=int(event_id),
                    reservation_id=int(reservation_id),
                    detail="Telegram part could not enter IN_FLIGHT before transport",
                )
                raise OutboxPreparationError("part_in_flight_commit_failed")

            connection.commit()

            return CommittedPartReservationReceipt(
                event_id=int(event_id),
                reservation_id=int(reservation_id),
                event_key=str(event_row["event_key"]),
                attempt_id=str(attempt_id),
                part_id=int(part.id),
                part_index=int(part.part_index),
                part_count=int(part.part_count),
                payload_text=str(part.payload_text),
                payload_hash=str(part.payload_hash),
                destination_chat_id=str(event_row["destination_chat_id"] or NA),
                destination_kind=str(event_row["destination_kind"] or NA),
                message_hash=str(event_row["message_hash"] or NA),
                attempt_count=int(event_row["attempt_count"] or 1),
                committed_at=timestamp,
            )

        claim_result = outbox.claim(
            event_id=int(event_id),
            reservation_id=int(reservation_id),
            now=timestamp,
            owner=owner,
            lease_seconds=lease_seconds,
        )
        if claim_result.claim is None:
            raise OutboxClaimRejected(f"outbox_claim_rejected:{claim_result.reason}")

        claim = claim_result.claim
        part = next((p for p in claim.parts if p.part_index == int(part_index)), None)
        if part is None:
            raise OutboxPreparationError(f"part_index_{part_index}_not_found_in_claim")
        if part.delivery_state == SENT:
            raise OutboxPreparationError(f"part_{part_index}_already_sent")

        in_flight_marked = outbox.mark_part_in_flight(part_id=part.id, attempt_id=claim.attempt_id)
        if not in_flight_marked:
            outbox.mark_uncertain_after_persistence_failure(
                event_id=int(event_id),
                reservation_id=int(reservation_id),
                detail="Telegram part could not enter IN_FLIGHT before transport",
            )
            raise OutboxPreparationError("part_in_flight_commit_failed")

        connection.commit()

        return CommittedPartReservationReceipt(
            event_id=int(event_id),
            reservation_id=int(reservation_id),
            event_key=str(claim.event_key),
            attempt_id=str(claim.attempt_id),
            part_id=int(part.id),
            part_index=int(part.part_index),
            part_count=int(part.part_count),
            payload_text=str(part.payload_text),
            payload_hash=str(part.payload_hash),
            destination_chat_id=str(event_row["destination_chat_id"] or NA),
            destination_kind=str(event_row["destination_kind"] or NA),
            message_hash=str(event_row["message_hash"] or NA),
            attempt_count=int(claim.attempt_count),
            committed_at=timestamp,
        )
    finally:
        connection.close()


def normalize_transport_outcome(outcome: Any) -> TelegramTransportOutcome:
    """Validate and normalize external transport outcome against Telegram provider evidence contracts."""
    if not isinstance(outcome, TelegramTransportOutcome):
        return TelegramTransportOutcome(
            delivery_state=UNCERTAIN,
            error_category="malformed_transport_outcome",
            error=f"Transport returned unexpected type: {type(outcome).__name__}",
        )

    state = str(outcome.delivery_state or "").strip().upper()
    if state not in {SENT, RETRYABLE, UNCERTAIN, FAILED_FINAL}:
        return TelegramTransportOutcome(
            delivery_state=UNCERTAIN,
            error_category="unknown_delivery_state",
            error=f"Unrecognized transport state: {outcome.delivery_state}",
        )

    if state == SENT:
        # A SENT outcome requires independently supported Telegram acknowledgment evidence
        msg_id = str(outcome.telegram_message_id or "").strip()
        if not msg_id or msg_id in (NA, "None", "0"):
            return TelegramTransportOutcome(
                delivery_state=UNCERTAIN,
                http_status=outcome.http_status,
                telegram_chat_id=outcome.telegram_chat_id,
                sent_at=outcome.sent_at,
                error_category="confirmed_success_missing_message_id",
                error="Telegram success response omitted a valid message ID; acceptance is uncertain.",
            )
        if outcome.http_status is not None and outcome.http_status != 200:
            return TelegramTransportOutcome(
                delivery_state=UNCERTAIN,
                http_status=outcome.http_status,
                telegram_message_id=msg_id,
                telegram_chat_id=outcome.telegram_chat_id,
                sent_at=outcome.sent_at,
                error_category="contradictory_success_http_status",
                error=f"Transport claimed SENT with non-200 HTTP status: {outcome.http_status}",
            )
        if outcome.error not in (None, "", NA):
            return TelegramTransportOutcome(
                delivery_state=UNCERTAIN,
                http_status=outcome.http_status,
                telegram_message_id=msg_id,
                telegram_chat_id=outcome.telegram_chat_id,
                sent_at=outcome.sent_at,
                error_category="contradictory_success_error",
                error=f"Transport claimed SENT but reported error: {outcome.error}",
            )
        if outcome.rate_limited:
            return TelegramTransportOutcome(
                delivery_state=UNCERTAIN,
                http_status=outcome.http_status,
                telegram_message_id=msg_id,
                telegram_chat_id=outcome.telegram_chat_id,
                sent_at=outcome.sent_at,
                error_category="contradictory_success_rate_limited",
                error="Transport claimed SENT while marked rate_limited",
            )
        return outcome

    if state == RETRYABLE:
        # HTTP 5xx or server timeout after transmission may have been accepted upstream.
        # Fail closed to UNCERTAIN to prevent duplicate public transmissions.
        if outcome.http_status is not None and 500 <= outcome.http_status <= 599:
            return TelegramTransportOutcome(
                delivery_state=UNCERTAIN,
                http_status=outcome.http_status,
                telegram_chat_id=outcome.telegram_chat_id,
                sent_at=outcome.sent_at,
                error_category="http_5xx_uncertain_acceptance",
                error=outcome.error or f"Telegram gateway returned HTTP {outcome.http_status}; acceptance is uncertain",
                retry_after=outcome.retry_after,
            )
        return outcome

    return outcome


async def stage_2_async_transport(
    receipt: CommittedPartReservationReceipt,
    transport: Callable[[TelegramTransportRequest], Awaitable[TelegramTransportOutcome]],
) -> TelegramTransportOutcome:
    """Stage 2: External asynchronous transport executed on the event loop.

    Zero SQLite connections or transactions are open during this execution.
    Handles exceptions and classifies unconfirmed responses strictly as UNCERTAIN.
    """
    request = TelegramTransportRequest(
        event_id=receipt.event_id,
        reservation_id=receipt.reservation_id,
        event_key=receipt.event_key,
        attempt_id=receipt.attempt_id,
        part_id=receipt.part_id,
        part_index=receipt.part_index,
        part_count=receipt.part_count,
        payload_text=receipt.payload_text,
        payload_hash=receipt.payload_hash,
        destination_chat_id=receipt.destination_chat_id,
        destination_kind=receipt.destination_kind,
        message_hash=receipt.message_hash,
    )
    try:
        raw_outcome = await transport(request)
        return normalize_transport_outcome(raw_outcome)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        return TelegramTransportOutcome(
            delivery_state=UNCERTAIN,
            error_category="transport_exception_after_attempt_started",
            error=f"{type(exc).__name__}: {str(exc)}",
        )


def stage_3_outbox_persistence(
    database_path: Path | str,
    *,
    receipt: CommittedPartReservationReceipt,
    transport_outcome: TelegramTransportOutcome,
    expected_identity: RuntimeEpochIdentity | None = None,
    now: str | None = None,
) -> DurableDeliveryReceipt:
    """Stage 3: Worker-owned persistence of transport outcome.

    Opens a fresh connection, validates exact attempt ID and associations,
    records the part outcome via low-level primitives, commits, and closes.
    """
    timestamp = now or _now_utc_iso()
    connection = open_repository_database(database_path, expected_identity=expected_identity)
    try:
        require_active_runtime_epoch(connection)
        require_event_reservation_association(
            connection, event_id=receipt.event_id, reservation_id=receipt.reservation_id
        )
        require_canonical_reservation_for_public_effect(connection, event_id=receipt.event_id)
        require_event_part_association(connection, event_id=receipt.event_id, part_id=receipt.part_id)

        event_row = connection.execute(
            "SELECT attempt_id, delivery_state FROM public_alert_events WHERE id = ?",
            (receipt.event_id,),
        ).fetchone()
        if event_row is None:
            raise SupersededClaimError("event_not_found_at_persistence")
        if str(event_row["attempt_id"] or "") != receipt.attempt_id:
            raise SupersededClaimError(
                f"claim_superseded: event_attempt={event_row['attempt_id']} != receipt={receipt.attempt_id}"
            )

        part_row = connection.execute(
            "SELECT attempt_id, delivery_state FROM public_alert_delivery_parts WHERE id = ?",
            (receipt.part_id,),
        ).fetchone()
        if part_row is None:
            raise SupersededClaimError("part_not_found_at_persistence")
        if str(part_row["attempt_id"] or "") != receipt.attempt_id:
            raise SupersededClaimError(
                f"part_claim_superseded: part_attempt={part_row['attempt_id']} != receipt={receipt.attempt_id}"
            )

        outbox = SQLitePublicTelegramOutbox(connection)
        result_payload: dict[str, Any] = {
            "delivery_state": transport_outcome.delivery_state,
            "message_id": transport_outcome.telegram_message_id,
            "chat_id": transport_outcome.telegram_chat_id,
            "sent_at": transport_outcome.sent_at or timestamp,
            "error_category": transport_outcome.error_category,
            "error": transport_outcome.error,
            "http_status": transport_outcome.http_status,
            "rate_limited": transport_outcome.rate_limited,
            "retry_after": transport_outcome.retry_after,
        }

        try:
            state = outbox.record_part_result(
                event_id=receipt.event_id,
                reservation_id=receipt.reservation_id,
                part_id=receipt.part_id,
                attempt_id=receipt.attempt_id,
                result=result_payload,
                now=timestamp,
            )
            connection.commit()
        except sqlite3.Error as exc:
            try:
                outbox.mark_uncertain_after_persistence_failure(
                    event_id=receipt.event_id,
                    reservation_id=receipt.reservation_id,
                    detail=f"Persistence error: {type(exc).__name__}: {str(exc)}",
                )
                check_ev = connection.execute(
                    "SELECT delivery_state FROM public_alert_events WHERE id = ?",
                    (receipt.event_id,),
                ).fetchone()
                if check_ev is not None and str(check_ev["delivery_state"] or "").strip().upper() == UNCERTAIN:
                    connection.execute(
                        """
                        UPDATE public_alert_delivery_parts
                        SET delivery_state = ?, updated_at = ?
                        WHERE id = ? AND attempt_id = ?
                        """,
                        (UNCERTAIN, timestamp, receipt.part_id, receipt.attempt_id),
                    )
                connection.commit()
                state = UNCERTAIN
            except BaseException as fatal_exc:
                raise PersistenceCommitFailureError(
                    f"Failed to record transport result or uncertainty fallback: {type(fatal_exc).__name__}"
                ) from fatal_exc

        # MANDATORY COMMIT VERIFICATION (Issue 2)
        # Independently verify that the requested state was actually committed in SQLite.
        verified_event = connection.execute(
            "SELECT id, canonical_reservation_attempt_id, attempt_id, delivery_state FROM public_alert_events WHERE id = ?",
            (receipt.event_id,),
        ).fetchone()
        if verified_event is None:
            raise PersistenceCommitFailureError(f"Event {receipt.event_id} missing after persistence commit")
        if int(verified_event["id"]) != receipt.event_id:
            raise PersistenceCommitFailureError("Event ID mismatch after persistence commit")
        if verified_event["canonical_reservation_attempt_id"] != receipt.reservation_id:
            raise PersistenceCommitFailureError("Reservation ID mismatch after persistence commit")

        verified_part = connection.execute(
            "SELECT id, public_alert_event_id, attempt_id, delivery_state FROM public_alert_delivery_parts WHERE id = ?",
            (receipt.part_id,),
        ).fetchone()
        if verified_part is None:
            raise PersistenceCommitFailureError(f"Part {receipt.part_id} missing after persistence commit")
        if int(verified_part["public_alert_event_id"]) != receipt.event_id:
            raise PersistenceCommitFailureError("Part event association mismatch after persistence commit")
        if str(verified_part["attempt_id"] or "") != receipt.attempt_id:
            raise PersistenceCommitFailureError("Part attempt ID mismatch after persistence commit")

        actual_part_state = str(verified_part["delivery_state"] or "").strip().upper()
        actual_event_state = str(verified_event["delivery_state"] or "").strip().upper()

        if actual_part_state == IN_FLIGHT and state != IN_FLIGHT:
            raise PersistenceCommitFailureError(
                f"Part {receipt.part_id} remained IN_FLIGHT after attempted persistence to {state}"
            )
        if state == UNCERTAIN and (actual_event_state != UNCERTAIN or actual_part_state != UNCERTAIN):
            raise PersistenceCommitFailureError(
                f"Event {receipt.event_id} remained in state {actual_event_state} after fallback to UNCERTAIN"
            )

        parts = list_outbox_parts(connection, event_id=receipt.event_id)
        all_sent = bool(parts) and all(p.delivery_state == SENT for p in parts)

        return DurableDeliveryReceipt(
            event_id=receipt.event_id,
            reservation_id=receipt.reservation_id,
            part_id=receipt.part_id,
            attempt_id=receipt.attempt_id,
            delivery_state=state,
            telegram_message_id=transport_outcome.telegram_message_id,
            persisted_at=timestamp,
            all_parts_sent=all_sent,
            transaction_result="COMMITTED",
        )
    finally:
        connection.close()


async def deliver_single_reserved_part(
    *,
    worker: TelegramOutboxWorker,
    database_path: Path | str,
    event_id: int,
    reservation_id: int,
    transport: Callable[[TelegramTransportRequest], Awaitable[TelegramTransportOutcome]],
    expected_identity: RuntimeEpochIdentity | None = None,
    now: str | None = None,
    part_index: int = 1,
    attempt_id: str | None = None,
) -> TelegramDeliveryProtocolResult:
    """Execute the three-stage protocol pipeline for one committed canonical public alert part.

    Stage 1: Preparation (Worker) -> Stage 2: Transport (Async Event Loop) -> Stage 3: Persistence (Worker).
    Fenced against cancellation so uncertain transport outcomes are durably recorded before cancelling.
    """
    timestamp = now or _now_utc_iso()

    # Stage 1: Preparation
    try:
        receipt = await worker.call(
            stage_1_outbox_preparation,
            database_path,
            event_id=event_id,
            reservation_id=reservation_id,
            expected_identity=expected_identity,
            now=timestamp,
            part_index=part_index,
            attempt_id=attempt_id,
            stage="preparation",
        )
    except (OutboxClaimRejected, OutboxPreparationError, RuntimeEpochOwnershipError, RuntimeEpochError) as exc:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError() from exc
        status_label = "skipped" if isinstance(exc, OutboxClaimRejected) else "blocked"
        return TelegramDeliveryProtocolResult(
            status=status_label,
            error_message=str(exc),
            detail=f"Outbox preparation stopped by authority: {type(exc).__name__}",
        )
    except Exception as exc:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise asyncio.CancelledError() from exc
        return TelegramDeliveryProtocolResult(
            status="failed",
            error_message=f"{type(exc).__name__}: {str(exc)}",
            detail="Unexpected error during outbox preparation",
        )

    # Cancellation fence before transport
    task = asyncio.current_task()
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()

    # Stage 2: Transport
    pending_cancellation: BaseException | None = None
    try:
        transport_outcome = await stage_2_async_transport(receipt, transport)
    except (asyncio.CancelledError, KeyboardInterrupt) as cancel_exc:
        pending_cancellation = cancel_exc
        transport_outcome = TelegramTransportOutcome(
            delivery_state=UNCERTAIN,
            error_category="cancelled_during_transport",
            error="Async transport cancelled during transmission; outcome is uncertain",
        )

    # Stage 3: Persistence (Fenced via worker)
    try:
        durable_receipt = await worker.call(
            stage_3_outbox_persistence,
            database_path,
            receipt=receipt,
            transport_outcome=transport_outcome,
            expected_identity=expected_identity,
            now=timestamp,
            stage="persistence",
        )
    except PersistenceCommitFailureError as commit_err:
        return TelegramDeliveryProtocolResult(
            status="unresolved_persistence",
            reservation_receipt=receipt,
            transport_outcome=transport_outcome,
            durable_receipt=None,
            error_message=str(commit_err),
            detail="Database commit failed after HTTP transmission; durable receipt could not be confirmed",
        )
    except Exception as exc:
        return TelegramDeliveryProtocolResult(
            status="unresolved_persistence",
            reservation_receipt=receipt,
            transport_outcome=transport_outcome,
            durable_receipt=None,
            error_message=f"{type(exc).__name__}: {str(exc)}",
            detail="Persistence worker failure after HTTP transmission",
        )

    task = asyncio.current_task()
    if pending_cancellation is not None:
        raise pending_cancellation
    if task is not None and task.cancelling():
        raise asyncio.CancelledError()

    status_label = durable_receipt.delivery_state.lower()
    return TelegramDeliveryProtocolResult(
        status=status_label,
        reservation_receipt=receipt,
        transport_outcome=transport_outcome,
        durable_receipt=durable_receipt,
        detail=f"Telegram outbox protocol completed in state {durable_receipt.delivery_state}",
    )
