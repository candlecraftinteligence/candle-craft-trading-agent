"""B0A atomic no-send fencing; synthetic SQLite and fake transport only."""
from __future__ import annotations

import asyncio
import multiprocessing as mp
import sqlite3

import httpx
import pytest

from app.alerts.telegram_outbox import (
    FAILED_FINAL, IN_FLIGHT, PENDING, POLICY_DISABLED, RETRYABLE, SENT,
    SKIPPED_DRY_RUN, UNCERTAIN, SQLitePublicTelegramOutbox, list_outbox_parts,
)
from app.alerts.telegram_lifecycle import SQLiteTelegramAlertAttemptRepository
from app.storage.database import connect_database
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY
from tests.test_telegram_delivery_ownership_characterization import (
    deliver_current, make_sender, seed,
)
from tests.test_telegram_outbox_recovery import NOW, _seed_intent


@pytest.fixture(autouse=True)
def deny_real_http(monkeypatch):
    def deny(*args, **kwargs):
        pytest.fail("B0A attempted real HTTP")

    async def deny_async(*args, **kwargs):
        deny()

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)


def snapshot(connection):
    return tuple(
        tuple(tuple(row) for row in connection.execute(f"SELECT * FROM {table} ORDER BY id"))
        for table in ("public_alert_events", "public_alert_delivery_parts", "telegram_alert_attempts")
    )


def terminalize(outbox, event_id, reservation_id, **kwargs):
    return outbox.mark_terminal_without_send(
        event_id=event_id, reservation_id=reservation_id,
        state=kwargs.pop("state", POLICY_DISABLED), reason="synthetic-refusal", now=NOW, **kwargs,
    )


def claim(outbox, event_id, reservation_id, attempt="attempt-1", now=NOW):
    result = outbox.claim(event_id=event_id, reservation_id=reservation_id, attempt_id=attempt, now=now)
    assert result.claim is not None
    return result.claim


def result(outbox, claimed, part, state):
    assert outbox.mark_part_in_flight(part_id=part.id, attempt_id=claimed.attempt_id, now=NOW)
    return outbox.record_part_result(
        event_id=claimed.event_id, reservation_id=claimed.reservation_id,
        part_id=part.id, attempt_id=claimed.attempt_id, now=NOW,
        result={"delivery_state": state, "http_status": 200 if state == SENT else 429,
                "message_id": "durable-123" if state == SENT else None,
                "chat_id": "test-chat" if state == SENT else None, "retry_after": 5},
    )


@pytest.mark.parametrize("terminal", [POLICY_DISABLED, SKIPPED_DRY_RUN, FAILED_FINAL])
def test_pending_terminalization_is_atomic_terminal_and_idempotent(tmp_path, terminal):
    path = tmp_path / "pending.db"
    event, reservation = _seed_intent(path, message="part one\npart two\npart three", max_message_length=10)
    with connect_database(path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        assert terminalize(outbox, event, reservation, state=terminal)
        first = snapshot(connection)
        assert {part.delivery_state for part in list_outbox_parts(connection, event_id=event)} == {terminal}
        assert not terminalize(outbox, event, reservation, state=terminal)
        assert snapshot(connection) == first
        assert outbox.claim(event_id=event, reservation_id=reservation, now=NOW).claim is None
        assert connection.execute("SELECT attempt_count FROM public_alert_events").fetchone()[0] == 0
    # Pure state operations invoke HTTP zero times; no transport is constructed.


@pytest.mark.parametrize("state", [IN_FLIGHT, SENT, UNCERTAIN, FAILED_FINAL, POLICY_DISABLED, SKIPPED_DRY_RUN])
def test_exact_active_or_terminal_attempt_cannot_be_overwritten(tmp_path, state):
    path = tmp_path / "protected.db"
    event, reservation = _seed_intent(path)
    with connect_database(path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        if state in {IN_FLIGHT, SENT, UNCERTAIN}:
            claimed = claim(outbox, event, reservation)
            if state != IN_FLIGHT:
                result(outbox, claimed, claimed.parts[0], state)
        else:
            assert terminalize(outbox, event, reservation, state=state)
        before = snapshot(connection)
        assert not terminalize(outbox, event, reservation, expected_state=state,
                               expected_attempt_id="attempt-1", expected_attempt_count=1)
        assert snapshot(connection) == before


@pytest.mark.parametrize("terminal", [POLICY_DISABLED, SKIPPED_DRY_RUN, FAILED_FINAL])
def test_rejected_retry_requires_exact_attempt_and_preserves_attempt_budget(tmp_path, terminal):
    path = tmp_path / "retry.db"
    event, reservation = _seed_intent(path)
    with connect_database(path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        first = claim(outbox, event, reservation)
        assert result(outbox, first, first.parts[0], RETRYABLE) == RETRYABLE
        second = claim(outbox, event, reservation, "attempt-2", "2026-07-16T10:00:06Z")
        assert result(outbox, second, second.parts[0], RETRYABLE) == RETRYABLE
        before = snapshot(connection)
        for authority in ({}, {"expected_state": RETRYABLE, "expected_attempt_id": "attempt-1", "expected_attempt_count": 1},
                          {"expected_state": RETRYABLE, "expected_attempt_id": "attempt-2", "expected_attempt_count": 1}):
            assert not terminalize(outbox, event, reservation, **authority)
            assert snapshot(connection) == before
        assert terminalize(outbox, event, reservation, state=terminal, expected_state=RETRYABLE,
                           expected_attempt_id="attempt-2", expected_attempt_count=2)
        assert list_outbox_parts(connection, event_id=event)[0].attempt_count == 2
        assert connection.execute("SELECT attempt_count, max_attempts FROM public_alert_events").fetchone()[:] == (2, 3)
        assert outbox.claim(event_id=event, reservation_id=reservation, now="2099-01-01T00:00:00Z").claim is None


FAULTS = [
    "UPDATE public_alert_events SET canonical_reservation_attempt_id = NULL",
    "UPDATE public_alert_events SET runtime_epoch_id = 'foreign'",
    "UPDATE telegram_alert_attempts SET public_watchlist_event_key = 'foreign'",
    "UPDATE telegram_alert_attempts SET delivery_state = 'IN_FLIGHT'",
    "UPDATE telegram_alert_attempts SET delivery_attempt_id = 'foreign'",
    "UPDATE telegram_alert_attempts SET delivery_attempt_count = 1",
    "UPDATE public_alert_events SET attempt_id = 'foreign'",
    "UPDATE public_alert_events SET claimed_at = 'prior-claim'",
    "UPDATE public_alert_events SET uncertain_at = 'prior-uncertainty'",
    "UPDATE public_alert_events SET telegram_message_id = 'prior-success'",
    "UPDATE public_alert_delivery_parts SET telegram_message_id = 'prior-success'",
    "UPDATE public_alert_delivery_parts SET delivery_state = 'IN_FLIGHT'",
    "UPDATE public_alert_delivery_parts SET delivery_state = 'SENT'",
    "UPDATE public_alert_delivery_parts SET delivery_state = 'UNCERTAIN'",
    "UPDATE public_alert_delivery_parts SET event_key = 'foreign'",
    "UPDATE public_alert_delivery_parts SET part_count = 2",
    "UPDATE public_alert_delivery_parts SET attempt_count = 1",
    "DELETE FROM public_alert_delivery_parts",
]


@pytest.mark.parametrize("fault", FAULTS)
def test_missing_or_contradictory_evidence_fails_closed_without_writes(tmp_path, fault):
    path = tmp_path / "fault.db"
    event, reservation = _seed_intent(path)
    with connect_database(path) as connection:
        connection.execute(fault)
        connection.commit()
        before = snapshot(connection)
        assert not terminalize(SQLitePublicTelegramOutbox(connection), event, reservation)
        assert snapshot(connection) == before


def test_wrong_reservation_or_missing_event_cannot_mutate(tmp_path):
    path = tmp_path / "mismatch.db"
    event, reservation = _seed_intent(path)
    with connect_database(path) as connection:
        before = snapshot(connection)
        outbox = SQLitePublicTelegramOutbox(connection)
        assert not terminalize(outbox, event, reservation + 100)
        assert not terminalize(outbox, event + 100, reservation)
        assert snapshot(connection) == before


def test_partial_multipart_success_and_receipt_survive_refusal_and_restart(tmp_path):
    path = tmp_path / "partial.db"
    event, reservation = _seed_intent(path, message="first part\nsecond part\nthird part", max_message_length=12)
    with connect_database(path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        claimed = claim(outbox, event, reservation)
        assert len(claimed.parts) > 1
        assert result(outbox, claimed, claimed.parts[0], SENT) == IN_FLIGHT
        assert result(outbox, claimed, claimed.parts[1], RETRYABLE) == RETRYABLE
        before = snapshot(connection)
    with connect_database(path) as connection:
        assert not terminalize(SQLitePublicTelegramOutbox(connection), event, reservation,
                               expected_state=RETRYABLE, expected_attempt_id=claimed.attempt_id, expected_attempt_count=1)
        assert snapshot(connection) == before
        assert list_outbox_parts(connection, event_id=event)[0].telegram_message_id == "durable-123"


def _process_contender(path, event, reservation, action, barrier, ordered, terminal_done, stage, queue):
    try:
        with connect_database(path) as connection:
            outbox = SQLitePublicTelegramOutbox(connection)
            barrier.wait(timeout=20)
            if action == "terminal" and ordered:
                if not ordered.wait(timeout=20):
                    raise TimeoutError("claim commit barrier")
            if action == "claim":
                claimed = outbox.claim(event_id=event, reservation_id=reservation, now=NOW, attempt_id="process-attempt").claim
                # A fake HTTP boundary, only after both event and part authority.
                requests = []

                def fake_http(request):
                    requests.append(request)
                    if stage == "active-http":
                        ordered.set()
                        if not terminal_done.wait(timeout=20):
                            raise TimeoutError("terminal refusal barrier")
                    return httpx.Response(200, json={"ok": True, "result": {"message_id": "process-receipt"}})

                if claimed and outbox.mark_part_in_flight(part_id=claimed.parts[0].id, attempt_id=claimed.attempt_id, now=NOW):
                    with httpx.Client(transport=httpx.MockTransport(fake_http)) as client:
                        response = client.post("https://telegram.invalid/sendMessage", json={"text": claimed.parts[0].payload_text})
                    assert response.json()["ok"]
                    outbox.record_part_result(event_id=event, reservation_id=reservation,
                        part_id=claimed.parts[0].id, attempt_id=claimed.attempt_id, now=NOW,
                        result={"delivery_state": SENT, "message_id": "process-receipt", "chat_id": "test-chat"})
                if ordered:
                    ordered.set()
                queue.put((action, claimed is not None, len(requests)))
            else:
                changed = terminalize(outbox, event, reservation)
                terminal_done.set()
                queue.put((action, changed, 0))
    except BaseException as exc:
        queue.put(("error", repr(exc), 0))
        raise


@pytest.mark.parametrize("stage", ["simultaneous", "active-http", "after-send-commit"])
def test_independent_spawn_process_claim_vs_terminalization(tmp_path, stage):
    path = tmp_path / "process.db"
    event, reservation = _seed_intent(path)
    ctx = mp.get_context("spawn")
    barrier, queue = ctx.Barrier(2), ctx.Queue()
    ordered = ctx.Event() if stage != "simultaneous" else None
    terminal_done = ctx.Event()
    processes = [ctx.Process(target=_process_contender, args=(str(path), event, reservation, action, barrier, ordered, terminal_done, stage, queue))
                 for action in ("claim", "terminal")]
    try:
        for process in processes:
            process.start()
        results = [queue.get(timeout=40) for _ in processes]
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        assert all(action != "error" for action, _, _ in results)
        assert sum(int(won) for _, won, _ in results) == 1
        http_calls = sum(calls for _, _, calls in results)
        with connect_database(path) as connection:
            state = connection.execute("SELECT delivery_state FROM public_alert_events").fetchone()[0]
            assert state == (SENT if http_calls == 1 else POLICY_DISABLED)
            assert {part.delivery_state for part in list_outbox_parts(connection, event_id=event)} == {state}
            if ordered:
                assert http_calls == 1 and state == SENT
                assert list_outbox_parts(connection, event_id=event)[0].telegram_message_id == "process-receipt"
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        queue.close()
        queue.join_thread()


def test_cancelled_http_then_restart_cannot_be_terminalized_or_replayed(tmp_path):
    path = tmp_path / "cancel.db"
    event, reservation = seed(path)
    calls = []

    async def scenario():
        entered = asyncio.Event()

        async def http(request):
            calls.append(request)
            entered.set()
            await asyncio.Future()

        with SQLiteTelegramAlertAttemptRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            async with httpx.AsyncClient(transport=httpx.MockTransport(http), base_url="https://telegram.invalid") as client:
                task = asyncio.create_task(deliver_current(repository, make_sender(client), event, reservation))
                await asyncio.wait_for(entered.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(scenario())
    with connect_database(path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        before = snapshot(connection)
        assert not terminalize(outbox, event, reservation)
        assert snapshot(connection) == before
        assert outbox.recover_stale_in_flight(event_id=event, now="2099-01-01T00:00:00Z")
        uncertain = snapshot(connection)
        assert not terminalize(outbox, event, reservation)
        assert snapshot(connection) == uncertain
        assert outbox.claim(event_id=event, reservation_id=reservation, now="2099-01-01T00:00:01Z").state == UNCERTAIN
    assert len(calls) == 1


@pytest.mark.parametrize("table", ["public_alert_delivery_parts", "telegram_alert_attempts"])
def test_transaction_rolls_back_all_projections_on_part_or_reservation_failure(tmp_path, table):
    path = tmp_path / "rollback.db"
    event, reservation = _seed_intent(path)
    with connect_database(path) as connection:
        connection.execute(f"CREATE TRIGGER synthetic_failure BEFORE UPDATE ON {table} BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        connection.commit()
        before = snapshot(connection)
        with pytest.raises(sqlite3.IntegrityError, match="synthetic failure"):
            terminalize(SQLitePublicTelegramOutbox(connection), event, reservation)
        assert snapshot(connection) == before


@pytest.mark.parametrize("race", [False, True], ids=["legitimate-retry-refusal", "stale-preflight"])
def test_existing_caller_supplies_observed_attempt_before_preflight(tmp_path, race):
    path = tmp_path / "caller.db"
    event, reservation = seed(path)

    def reject_attempt():
        with connect_database(path) as connection:
            outbox = SQLitePublicTelegramOutbox(connection)
            claimed = claim(outbox, event, reservation)
            assert result(outbox, claimed, claimed.parts[0], RETRYABLE) == RETRYABLE

    if not race:
        reject_attempt()
    calls = []

    async def scenario():
        with SQLiteTelegramAlertAttemptRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request))) as client:
                sender = make_sender(client, signals_enabled=False)
                original = sender.preflight

                def preflight(**kwargs):
                    if race:
                        reject_attempt()
                    return original(**kwargs)

                sender.preflight = preflight
                await deliver_current(repository, sender, event, reservation)

    asyncio.run(scenario())
    with connect_database(path) as connection:
        assert connection.execute("SELECT delivery_state FROM public_alert_events").fetchone()[0] == (RETRYABLE if race else POLICY_DISABLED)
    assert len(calls) == 0
