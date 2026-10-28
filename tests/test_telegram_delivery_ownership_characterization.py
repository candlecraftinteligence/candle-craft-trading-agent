"""B0 observations of existing behavior, including blockers; no production fixes.

All databases are synthetic pytest temporaries. HTTP uses MockTransport only.
These assertions describe the audited baseline, not the desired future contract.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading

import httpx
import pytest

import app.alerts.telegram_outbox_protocol as protocol
from app.alerts.telegram_lifecycle import (
    PublicWatchlistReservationResult,
    SQLiteTelegramAlertAttemptRepository,
    _deliver_committed_public_alert_intent,
)
from app.alerts.telegram_outbox import (
    FAILED_FINAL,
    IN_FLIGHT,
    POLICY_DISABLED,
    SENT,
    SKIPPED_DRY_RUN,
    UNCERTAIN,
    SQLitePublicTelegramOutbox,
    list_outbox_parts,
)
from app.alerts.telegram_routing import TelegramMessageType
from app.alerts.telegram_sender import TelegramSender
from app.alerts.telegram_sender_adapter import TelegramSenderTransport
from app.formatters.telegram_signal_formatter import TelegramAlertType
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.storage.database import connect_database
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY
from tests.test_p0b3b1_telegram_outbox_protocol import NOW
from tests.test_telegram_sender_adapter import CHAT, TEXT, provider_success, seed


@pytest.fixture(autouse=True)
def deny_real_http(monkeypatch):
    async def deny_async(*args, **kwargs):
        pytest.fail("B0 attempted real async HTTP")

    def deny_sync(*args, **kwargs):
        pytest.fail("B0 attempted real sync HTTP")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_sync)


def make_sender(client, **changes):
    options = dict(
        bot_token="synthetic-b0-token", chat_id=CHAT, signals_enabled=True,
        dry_run=False, local_manual_mode=True, http_client=client,
        api_base_url="https://telegram.invalid", destination="public_chat",
    )
    options.update(changes)
    return TelegramSender(**options)


def deliver_current(repository, sender, event_id, reservation_id):
    return _deliver_committed_public_alert_intent(
        repository=repository, sender=sender,
        reservation=PublicWatchlistReservationResult(
            granted=True, event_key="plan-p0b3b1|initial_watchlist",
            event_id=event_id, reservation_id=reservation_id,
        ),
        symbol="BTCUSDT", signal_id="signal-p0b3b1", alert_type=TelegramAlertType.WATCHLIST,
        message_type=TelegramMessageType.PUBLIC_WATCHLIST,
        message_text=TEXT, message_hash="hash-p0b3b1",
    )


REFUSALS = [
    ({"signals_enabled": False}, POLICY_DISABLED, "telegram_sending_disabled"),
    ({"local_manual_mode": False}, POLICY_DISABLED, "local_manual_mode_disabled"),
    ({"dry_run": True}, SKIPPED_DRY_RUN, "telegram_dry_run_enabled"),
    ({"bot_token": None}, FAILED_FINAL, "missing_telegram_credentials"),
]


@pytest.mark.parametrize("changes,expected,reason", REFUSALS)
def test_current_preclaim_refusal_terminalizes_and_reenable_cannot_claim(tmp_path, changes, expected, reason):
    path = tmp_path / "preclaim.db"
    event_id, reservation_id = seed(path)
    calls = []

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request)), base_url="https://telegram.invalid") as client:
            with SQLiteTelegramAlertAttemptRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
                result = await deliver_current(repository, make_sender(client, **changes), event_id, reservation_id)
                assert result.status in {"skipped", "failed"}
                row = repository._connection.execute(
                    "SELECT delivery_state, last_error_category, attempt_count FROM public_alert_events WHERE id = ?",
                    (event_id,),
                ).fetchone()
                assert tuple(row) == (expected, reason, 0)
                # Re-enabling policy cannot undo the durable terminal state.
                again = await deliver_current(repository, make_sender(client), event_id, reservation_id)
                assert again.status != "sent"
                assert list_outbox_parts(repository._connection, event_id=event_id)[0].delivery_state == expected
        assert calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("changes,_expected,reason", REFUSALS)
def test_adapter_refusal_after_claim_is_final_even_after_reenable(tmp_path, changes, _expected, reason):
    path = tmp_path / "postclaim.db"
    event_id, reservation_id = seed(path)
    calls = []

    async def scenario():
        worker = protocol.TelegramOutboxWorker()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request)), base_url="https://telegram.invalid") as client:
                kwargs = dict(worker=worker, database_path=path, event_id=event_id,
                              reservation_id=reservation_id, expected_identity=SYNTHETIC_IDENTITY, now=NOW)
                result = await protocol.deliver_single_reserved_part(
                    **kwargs, transport=TelegramSenderTransport(make_sender(client, **changes), message_type="public_watchlist"),
                )
                assert result.durable_receipt.delivery_state == FAILED_FINAL
                assert result.transport_outcome.error_category == f"sender_preflight:{reason}"
                again = await protocol.deliver_single_reserved_part(
                    **kwargs, transport=TelegramSenderTransport(make_sender(client), message_type="public_watchlist"),
                )
                assert again.status == "blocked"
        finally:
            await worker.aclose()
        assert calls == []

    asyncio.run(scenario())


@pytest.mark.parametrize("attribute,value", [("_signals_enabled", False), ("_dry_run", True), ("_bot_token", None)])
def test_second_sender_preflight_refusal_is_uncertain_without_http(tmp_path, attribute, value):
    path = tmp_path / "late-refusal.db"
    event_id, reservation_id = seed(path)
    calls = []

    async def scenario():
        worker = protocol.TelegramOutboxWorker()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request: calls.append(request)), base_url="https://telegram.invalid") as client:
                sender = make_sender(client)
                original = sender.preflight
                checks = []

                def change_after_first_check(**kwargs):
                    blocked = original(**kwargs)
                    checks.append(blocked)
                    if len(checks) == 1:
                        setattr(sender, attribute, value)
                    return blocked

                sender.preflight = change_after_first_check
                result = await protocol.deliver_single_reserved_part(
                    worker=worker, database_path=path, event_id=event_id, reservation_id=reservation_id,
                    expected_identity=SYNTHETIC_IDENTITY, now=NOW,
                    transport=TelegramSenderTransport(sender, message_type="public_watchlist"),
                )
                assert len(checks) == 2 and checks[0] is None and checks[1] is not None
                assert result.durable_receipt.delivery_state == UNCERTAIN
                assert result.transport_outcome.error_category == "adapter_expected_one_part_result"
        finally:
            await worker.aclose()
        assert calls == []

    asyncio.run(scenario())


def test_current_transport_retains_both_outer_connections_on_event_loop(tmp_path):
    path = tmp_path / "outer-owners.db"
    event_id, reservation_id = seed(path)
    observations = []

    async def scenario():
        loop_thread = threading.get_ident()
        with SQLiteTelegramAlertAttemptRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as lifecycle:
                def handler(request):
                    observations.append((threading.get_ident(), repository._connection.in_transaction,
                                         lifecycle._connection.in_transaction))
                    assert repository._connection.execute("SELECT 1").fetchone()[0] == 1
                    assert lifecycle._connection.execute("SELECT 1").fetchone()[0] == 1
                    return httpx.Response(200, json=provider_success())

                async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                    assert (await deliver_current(repository, make_sender(client), event_id, reservation_id)).status == "sent"
        assert observations == [(loop_thread, False, False)]
        assert repository.connection is None and lifecycle.connection is None

    asyncio.run(scenario())


def test_outer_write_lock_blocks_fresh_connection_until_owner_rolls_back(tmp_path):
    path = tmp_path / "lock.db"
    event_id, reservation_id = seed(path)
    with SQLiteTelegramAlertAttemptRepository(path, expected_identity=SYNTHETIC_IDENTITY) as outer:
        # Open the independent owner before acquiring the write lock.
        with connect_database(path, busy_timeout_ms=1) as independent:
            outer._connection.execute("UPDATE public_alert_events SET updated_at = updated_at WHERE id = ?", (event_id,))
            assert outer._connection.in_transaction
            with pytest.raises(sqlite3.OperationalError, match="locked"):
                SQLitePublicTelegramOutbox(independent).claim(event_id=event_id, reservation_id=reservation_id, now=NOW)
            outer._connection.rollback()
            assert SQLitePublicTelegramOutbox(independent).claim(
                event_id=event_id, reservation_id=reservation_id, now=NOW,
            ).claim is not None


def test_protocol_connections_close_on_worker_before_mock_http(tmp_path, monkeypatch):
    path = tmp_path / "worker-owners.db"
    event_id, reservation_id = seed(path)
    observations = []
    original = protocol.open_repository_database

    class ObservedConnection:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def close(self):
            self.connection.close()
            observations.append(("close", threading.get_ident()))

    def open_observed(*args, **kwargs):
        connection = original(*args, **kwargs)
        observations.append(("open", threading.get_ident()))
        return ObservedConnection(connection)

    monkeypatch.setattr(protocol, "open_repository_database", open_observed)

    async def scenario():
        loop_thread = threading.get_ident()
        worker = protocol.TelegramOutboxWorker()
        try:
            def handler(request):
                assert [kind for kind, _ in observations] == ["open", "close"]
                observations.append(("http", threading.get_ident()))
                return httpx.Response(200, json=provider_success())

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                result = await protocol.deliver_single_reserved_part(
                    worker=worker, database_path=path, event_id=event_id, reservation_id=reservation_id,
                    expected_identity=SYNTHETIC_IDENTITY, now=NOW,
                    transport=TelegramSenderTransport(make_sender(client), message_type="public_watchlist"),
                )
                assert result.status == "sent" and result.durable_receipt.all_parts_sent
        finally:
            await worker.aclose()
        assert [kind for kind, _ in observations] == ["open", "close", "http", "open", "close"]
        assert observations[2][1] == loop_thread
        assert len({owner for kind, owner in observations if kind != "http"}) == 1
        assert observations[0][1] != loop_thread

    asyncio.run(scenario())


@pytest.mark.parametrize("completed", [False, True], ids=["active-claim", "confirmed-sent"])
def test_no_send_terminalizer_currently_overwrites_active_or_completed_event(tmp_path, completed):
    path = tmp_path / "terminalizer.db"
    event_id, reservation_id = seed(path)
    receipt = protocol.stage_1_outbox_preparation(
        path, event_id=event_id, reservation_id=reservation_id, expected_identity=SYNTHETIC_IDENTITY, now=NOW,
    )
    if completed:
        protocol.stage_3_outbox_persistence(
            path, receipt=receipt, expected_identity=SYNTHETIC_IDENTITY, now=NOW,
            transport_outcome=protocol.TelegramTransportOutcome(
                delivery_state=SENT, http_status=200, telegram_message_id="123", telegram_chat_id=CHAT,
            ),
        )
    with connect_database(path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        outbox.mark_terminal_without_send(
            event_id=event_id, reservation_id=reservation_id, state=POLICY_DISABLED, reason="synthetic-stale-preflight",
        )
        event = connection.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
        assert event[0] == POLICY_DISABLED
        assert list_outbox_parts(connection, event_id=event_id)[0].delivery_state == (SENT if completed else IN_FLIGHT)
        # No HTTP in this test: synthetic success demonstrates state overwrite only.


def test_current_transport_cancellation_leaves_in_flight_until_stale_recovery(tmp_path):
    path = tmp_path / "cancel-current.db"
    event_id, reservation_id = seed(path)
    calls = []

    async def scenario():
        entered = asyncio.Event()

        async def handler(request):
            calls.append(request)
            entered.set()
            await asyncio.Future()

        with SQLiteTelegramAlertAttemptRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                task = asyncio.create_task(deliver_current(repository, make_sender(client), event_id, reservation_id))
                await asyncio.wait_for(entered.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            row = repository._connection.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
            assert row[0] == IN_FLIGHT
            outbox = SQLitePublicTelegramOutbox(repository._connection)
            assert outbox.recover_stale_in_flight(event_id=event_id, now="2099-01-01T00:00:00Z")
            assert outbox.claim(event_id=event_id, reservation_id=reservation_id, now="2099-01-01T00:00:01Z").state == UNCERTAIN
        assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["epoch-binding", "canonical-pointer"])
def test_fault_injected_authority_drift_after_claim_is_not_rechecked_before_http(tmp_path, fault):
    """Out-of-contract mutation proves the boundary; normal epoch/bind APIs are immutable."""
    path = tmp_path / "authority-drift.db"
    event_id, reservation_id = seed(path)
    calls = []

    async def scenario():
        worker = protocol.TelegramOutboxWorker()
        try:
            def handler(request):
                calls.append(request)
                return httpx.Response(200, json=provider_success())

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                adapter = TelegramSenderTransport(make_sender(client), message_type="public_watchlist")

                async def inject_drift_then_send(request):
                    with connect_database(path) as connection:
                        if fault == "epoch-binding":
                            connection.execute("UPDATE runtime_epochs SET generation_binding = 'synthetic-invalidated'")
                        else:
                            connection.execute("UPDATE public_alert_events SET canonical_reservation_attempt_id = NULL WHERE id = ?", (event_id,))
                    return await adapter(request)

                result = await protocol.deliver_single_reserved_part(
                    worker=worker, database_path=path, event_id=event_id, reservation_id=reservation_id,
                    expected_identity=SYNTHETIC_IDENTITY, now=NOW, transport=inject_drift_then_send,
                )
                assert result.transport_outcome.delivery_state == SENT
                assert result.status == "unresolved_persistence" and result.durable_receipt is None
        finally:
            await worker.aclose()
        assert len(calls) == 1

    asyncio.run(scenario())


def test_fault_injected_destination_change_after_adapter_check_can_reach_http(tmp_path):
    path = tmp_path / "destination-drift.db"
    event_id, reservation_id = seed(path)
    calls = []

    async def scenario():
        worker = protocol.TelegramOutboxWorker()
        try:
            def handler(request):
                calls.append(request)
                body = provider_success()
                body["result"]["chat"]["id"] = -1009999999999
                return httpx.Response(200, json=body)

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                sender = make_sender(client)
                original = sender.preflight

                def mutate_after_destination_validation(**kwargs):
                    result = original(**kwargs)
                    sender._chat_id = "-1009999999999"
                    return result

                sender.preflight = mutate_after_destination_validation
                result = await protocol.deliver_single_reserved_part(
                    worker=worker, database_path=path, event_id=event_id, reservation_id=reservation_id,
                    expected_identity=SYNTHETIC_IDENTITY, now=NOW,
                    transport=TelegramSenderTransport(sender, message_type="public_watchlist"),
                )
                # Correctly rejecting the acknowledgment cannot undo the wrong public effect.
                assert result.transport_outcome.delivery_state == UNCERTAIN
                assert result.durable_receipt.delivery_state == UNCERTAIN
        finally:
            await worker.aclose()
        assert len(calls) == 1
        assert json.loads(calls[0].content)["chat_id"] == "-1009999999999"

    asyncio.run(scenario())
