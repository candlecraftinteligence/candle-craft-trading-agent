"""P0B3B2A synthetic contract, actual sender HTTP, and durable protocol tests.

Every HTTP integration uses MockTransport and asserts invocation counts. Real
httpx transports are blocked for this module. No settings or credentials are read.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest

from app.alerts.telegram_outbox import FAILED_FINAL, RETRYABLE, SENT, UNCERTAIN, list_outbox_parts
from app.alerts.telegram_outbox_protocol import (
    TelegramOutboxWorker,
    TelegramTransportRequest,
    deliver_single_reserved_part,
    normalize_transport_outcome,
)
from app.alerts.telegram_routing import TelegramMessageType
from app.alerts.telegram_sender import TelegramSender, TelegramSendResult
from app.alerts.telegram_sender_adapter import TelegramSenderTransport, adapt_telegram_send_result
from app.runtime_epoch.errors import RuntimeEpochError
from app.storage.database import connect_database
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY, bootstrap_operational_test_database
from tests.test_p0b3b1_telegram_outbox_protocol import NOW, _seed_canonical_event

CHAT = "-1001234567890"
TEXT = "Synthetic reserved part; invalidation: test boundary; risk: loss possible."


@pytest.fixture(autouse=True)
def deny_real_http(monkeypatch):
    async def deny_async(*args, **kwargs):
        pytest.fail("A real async HTTP transport was invoked")

    def deny_sync(*args, **kwargs):
        pytest.fail("A real sync HTTP transport was invoked")

    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny_async)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny_sync)


def request(**changes):
    base = TelegramTransportRequest(
        event_id=1, reservation_id=2, event_key="plan|initial_watchlist", attempt_id="attempt-1",
        part_id=3, part_index=1, part_count=1, payload_text=TEXT,
        payload_hash=hashlib.sha256(TEXT.encode()).hexdigest(), destination_chat_id=CHAT,
        destination_kind="public_chat", message_hash="reserved-message-hash",
    )
    return replace(base, **changes)


def success(**changes):
    raw = dict(
        status="sent", delivery_state=SENT, part_number=1, total_parts=1, http_status=200,
        message_id=123, chat_id=int(CHAT), sent_at=NOW, rate_limited=False,
        retry_after=None, error_category=None, error=None,
    )
    raw.update(changes)
    return TelegramSendResult("sent", "synthetic", (raw,), delivery_state=SENT)


def sender(client, **changes):
    options = dict(
        bot_token="synthetic-test-token", chat_id=CHAT, signals_enabled=True, dry_run=False,
        local_manual_mode=True, http_client=client, api_base_url="https://telegram.invalid",
        destination="public_chat",
    )
    options.update(changes)
    return TelegramSender(**options)


def provider_success(message_id=123):
    return {"ok": True, "result": {"message_id": message_id, "chat": {"id": int(CHAT)}, "date": 1784196000}}


def test_verified_success_is_detached_and_preserves_optional_timestamp():
    result = success()
    outcome = adapt_telegram_send_result(result, request=request())
    assert outcome.delivery_state == SENT
    assert outcome.http_status == 200
    assert outcome.telegram_message_id == "123"
    assert outcome.telegram_chat_id == CHAT
    assert outcome.sent_at == NOW
    assert normalize_transport_outcome(outcome) == outcome
    result.telegram_results[0]["message_id"] = 999
    assert outcome.telegram_message_id == "123"
    assert adapt_telegram_send_result(success(sent_at=None), request=request()).sent_at is None


@pytest.mark.parametrize("field,value", [
    ("http_status", None), ("http_status", "200"), ("http_status", 200.0), ("http_status", True),
    ("http_status", 201), ("http_status", 429), ("http_status", 500), ("http_status", 0),
    ("message_id", None), ("message_id", 0), ("message_id", -1), ("message_id", True),
    ("message_id", 1.0), ("message_id", "123"), ("message_id", "N/A"),
    ("chat_id", None), ("chat_id", 0), ("chat_id", True), ("chat_id", 123), ("chat_id", CHAT),
    ("part_number", None), ("part_number", True), ("part_number", 1.0), ("part_number", "1"),
    ("part_number", 2), ("total_parts", 2), ("total_parts", True),
    ("rate_limited", True), ("rate_limited", None), ("rate_limited", 0),
    ("retry_after", 0), ("retry_after", 10), ("error", "failure"), ("error_category", "failure"),
    ("sent_at", "N/A"), ("sent_at", "2026-10-28T10:00:00"), ("sent_at", 123),
    ("status", "failed"), ("delivery_state", RETRYABLE), ("delivery_state", "sent"),
])
def test_success_requires_strict_consistent_provider_evidence(field, value):
    outcome = adapt_telegram_send_result(success(**{field: value}), request=request())
    assert outcome.delivery_state == UNCERTAIN
    assert outcome.telegram_message_id is None


@pytest.mark.parametrize("field", ["http_status", "message_id", "chat_id", "part_number", "total_parts", "rate_limited"])
def test_missing_required_success_evidence(field):
    result = success()
    del result.telegram_results[0][field]
    assert adapt_telegram_send_result(result, request=request()).delivery_state == UNCERTAIN


@pytest.mark.parametrize("result", [
    None, True, {}, "sent", TelegramSendResult("sent", "summary only", delivery_state=SENT),
    replace(success(), status="failed"), replace(success(), delivery_state=RETRYABLE),
    replace(success(), error_message="failure"), replace(success(), telegram_results=(None,)),
    replace(success(), telegram_results=[success().telegram_results[0]]),
    replace(success(), telegram_results=success().telegram_results * 2),
    TelegramSendResult("skipped", "untrusted skip", error_message="telegram_dry_run_enabled", delivery_state="SKIPPED_DRY_RUN"),
])
def test_malformed_aggregate_or_unobserved_skip_is_uncertain(result):
    assert adapt_telegram_send_result(result, request=request()).delivery_state == UNCERTAIN


@pytest.mark.parametrize("raw,expected", [
    ({"delivery_state": RETRYABLE, "error_category": "connect_failure_before_transmission"}, RETRYABLE),
    ({"delivery_state": RETRYABLE, "http_status": 429, "error_category": "telegram_rate_limited", "rate_limited": True, "retry_after": 7.0}, RETRYABLE),
    ({"delivery_state": FAILED_FINAL, "http_status": 403, "error_category": "telegram_permanent_rejection"}, FAILED_FINAL),
    ({"delivery_state": FAILED_FINAL, "http_status": 408, "error_category": "telegram_permanent_rejection"}, UNCERTAIN),
    ({"delivery_state": RETRYABLE, "http_status": 503, "error_category": "telegram_server_rejection"}, UNCERTAIN),
    ({"delivery_state": FAILED_FINAL, "http_status": 500}, UNCERTAIN),
    ({"delivery_state": RETRYABLE, "http_status": 429, "error_category": "telegram_api_rejection:telegram_rate_limited", "rate_limited": True}, UNCERTAIN),
    ({"delivery_state": RETRYABLE, "http_status": 200, "error_category": "connect_failure_before_transmission"}, UNCERTAIN),
    ({"delivery_state": RETRYABLE}, UNCERTAIN),
    ({"delivery_state": "unknown"}, UNCERTAIN),
    ({"delivery_state": UNCERTAIN, "http_status": 200}, UNCERTAIN),
    ({"delivery_state": RETRYABLE, "error_category": "connect_failure_before_transmission", "message_id": 123}, UNCERTAIN),
    ({"delivery_state": RETRYABLE, "error_category": "connect_failure_before_transmission", "error": None}, UNCERTAIN),
])
def test_failure_evidence_controls_retry_eligibility(raw, expected):
    record = dict(status="failed", delivery_state=UNCERTAIN, part_number=1, total_parts=1,
                  http_status=None, rate_limited=False, retry_after=None, error="synthetic failure")
    record.update(raw)
    result = TelegramSendResult("failed", "synthetic", (record,), "synthetic failure", record["delivery_state"])
    outcome = adapt_telegram_send_result(result, request=request())
    assert outcome.delivery_state == expected
    assert outcome.telegram_message_id is None


@pytest.mark.parametrize("delay", [-1, True, "7", float("nan"), float("inf"), -float("inf"), 10**400])
def test_invalid_rate_limit_backoff_never_authorizes_retry(delay):
    raw = dict(status="failed", delivery_state=RETRYABLE, part_number=1, total_parts=1,
               http_status=429, rate_limited=True, retry_after=delay,
               error_category="telegram_rate_limited", error="limited")
    result = TelegramSendResult("failed", "synthetic", (raw,), "limited", RETRYABLE)
    assert adapt_telegram_send_result(result, request=request()).delivery_state == UNCERTAIN


@pytest.mark.parametrize("status,body,expected", [
    (200, provider_success(), SENT), (200, provider_success(0), UNCERTAIN),
    (200, provider_success(True), UNCERTAIN), (200, provider_success("123"), UNCERTAIN),
    (200, {"ok": True, "result": {}}, UNCERTAIN), (200, {}, UNCERTAIN), (200, [], UNCERTAIN),
    (200, {"ok": False}, UNCERTAIN), (200, {"ok": "true", "result": {"message_id": 1}}, UNCERTAIN),
    (200, {"ok": False, "error_code": 429, "parameters": {"retry_after": 8}}, UNCERTAIN),
    (201, provider_success(), UNCERTAIN), (302, {}, UNCERTAIN), (408, {}, UNCERTAIN),
    (400, {"ok": False}, FAILED_FINAL), (401, {"ok": False}, FAILED_FINAL),
    (403, {"ok": False}, FAILED_FINAL),
    (429, {"ok": False, "parameters": {"retry_after": 8}}, RETRYABLE),
    (500, {}, UNCERTAIN), (502, {}, UNCERTAIN), (503, {}, UNCERTAIN), (599, {}, UNCERTAIN),
])
def test_actual_sender_http_responses_are_adapted_once(status, body, expected):
    calls = []

    def handler(http_request):
        calls.append(http_request)
        assert http_request.url.host == "telegram.invalid"
        assert json.loads(http_request.content) == {"chat_id": CHAT, "text": TEXT, "disable_web_page_preview": True}
        return httpx.Response(status, json=body)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
            transport = TelegramSenderTransport(sender(client), message_type=TelegramMessageType.PUBLIC_WATCHLIST)
            outcome = await transport(request())
            assert outcome.delivery_state == expected
            assert len(calls) == 1
            if expected == RETRYABLE:
                assert outcome.rate_limited is True
                assert outcome.retry_after == 8
            assert "synthetic-test-token" not in repr(outcome)

    asyncio.run(scenario())


@pytest.mark.parametrize("exception,expected", [
    (httpx.ConnectTimeout, RETRYABLE), (httpx.ConnectError, RETRYABLE), (httpx.PoolTimeout, RETRYABLE),
    (httpx.ReadTimeout, UNCERTAIN), (httpx.WriteTimeout, UNCERTAIN), (httpx.ReadError, UNCERTAIN),
    (httpx.WriteError, UNCERTAIN), (httpx.TimeoutException, UNCERTAIN),
    (httpx.RemoteProtocolError, UNCERTAIN), (RuntimeError, UNCERTAIN),
])
def test_actual_transport_exceptions_do_not_retry_or_leak(exception, expected):
    calls = []

    def handler(http_request):
        calls.append(http_request)
        raise exception("sensitive URL /botSYNTHETIC/sendMessage")

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
            outcome = await TelegramSenderTransport(sender(client), message_type="public_signal")(request())
            assert outcome.delivery_state == expected
            assert "SYNTHETIC" not in repr(outcome)
            assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("changes,reason", [
    ({"signals_enabled": False}, "telegram_sending_disabled"),
    ({"dry_run": True}, "telegram_dry_run_enabled"),
    ({"local_manual_mode": False}, "local_manual_mode_disabled"),
    ({"bot_token": None}, "missing_telegram_credentials"),
])
def test_observed_preflight_confirms_zero_transmission(changes, reason):
    calls = []

    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(200, json=provider_success())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
            real_sender = sender(client, **changes)
            real_sender.send_part = AsyncMock(wraps=real_sender.send_part)
            outcome = await TelegramSenderTransport(real_sender, message_type="public_watchlist")(request())
            assert outcome.delivery_state == FAILED_FINAL
            assert outcome.error_category == f"sender_preflight:{reason}"
            assert outcome.http_status is None
            assert outcome.telegram_message_id is None
            real_sender.send_part.assert_not_awaited()
            assert len(calls) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("changes", [
    {"event_id": 0}, {"reservation_id": True}, {"part_id": -1}, {"part_index": 0},
    {"part_index": 2}, {"part_count": True}, {"attempt_id": "N/A"}, {"event_key": ""},
    {"message_hash": "N/A"}, {"payload_hash": "bad"}, {"payload_text": "changed"},
    {"payload_text": "x" * 4097}, {"payload_text": "\ud800"},
    {"destination_chat_id": "other"}, {"destination_kind": "signal_channel"},
    {"destination_kind": "admin_chat"},
])
def test_invalid_or_cross_destination_request_stops_before_sender(changes):
    real_sender = sender(None)
    real_sender.send_part = AsyncMock()
    outcome = asyncio.run(TelegramSenderTransport(real_sender, message_type="public_watchlist")(request(**changes)))
    assert outcome.delivery_state == FAILED_FINAL
    real_sender.send_part.assert_not_awaited()


@pytest.mark.parametrize("message_type", ["welcome", "diagnostics", "unknown", "made_up"])
def test_signal_channel_policy_cannot_be_bypassed(message_type):
    real_sender = sender(None, destination="signal_channel")
    real_sender.send_part = AsyncMock()
    outcome = asyncio.run(TelegramSenderTransport(real_sender, message_type=message_type)(request(destination_kind="signal_channel")))
    assert outcome.delivery_state == FAILED_FINAL
    real_sender.send_part.assert_not_awaited()


def test_bad_preflight_and_late_skip_never_claim_non_transmission():
    real_sender = sender(None)
    real_sender.preflight = lambda **kwargs: success()
    real_sender.send_part = AsyncMock()
    outcome = asyncio.run(TelegramSenderTransport(real_sender, message_type="public_signal")(request()))
    assert outcome.delivery_state == UNCERTAIN
    real_sender.send_part.assert_not_awaited()
    real_sender.preflight = lambda **kwargs: None
    real_sender.send_part = AsyncMock(return_value=TelegramSendResult("skipped", "late skip", delivery_state="POLICY_DISABLED"))
    outcome = asyncio.run(TelegramSenderTransport(real_sender, message_type="public_signal")(request()))
    assert outcome.delivery_state == UNCERTAIN
    assert real_sender.send_part.await_count == 1


def test_malformed_json_and_invalid_provider_date_are_uncertain():
    async def scenario():
        for response in (httpx.Response(200, content=b"not-json"),
                         httpx.Response(200, json={"ok": True, "result": {"message_id": 1, "date": 10**100}})):
            calls = []

            def handler(http_request):
                calls.append(http_request)
                return response

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                outcome = await TelegramSenderTransport(sender(client), message_type="public_signal")(request())
                assert outcome.delivery_state == UNCERTAIN
                assert len(calls) == 1

    asyncio.run(scenario())


def seed(db_path: Path, *, multipart=False, bind_reservation=True):
    # Shared synthetic fixture creation only; every scenario/assertion below is new.
    bootstrap_operational_test_database(db_path)
    event_id, reservation_id = _seed_canonical_event(
        db_path, message="A" * 80 if multipart else TEXT,
        max_message_length=40 if multipart else 4096, bind_reservation=bind_reservation,
    )
    with connect_database(db_path) as connection:
        connection.execute("UPDATE public_alert_events SET destination_chat_id = ? WHERE id = ?", (CHAT, event_id))
        connection.commit()
    return event_id, reservation_id


@pytest.mark.parametrize("mode,expected", [("success", SENT), ("5xx", UNCERTAIN), ("timeout", UNCERTAIN), ("malformed", UNCERTAIN)])
def test_durable_protocol_blocks_duplicate_http_after_terminal_outcome(tmp_path, mode, expected):
    db_path = tmp_path / "adapter.db"
    event_id, reservation_id = seed(db_path)
    calls = []

    def handler(http_request):
        calls.append(http_request)
        if mode == "timeout":
            raise httpx.ReadTimeout("synthetic")
        if mode == "5xx":
            return httpx.Response(502, json={})
        return httpx.Response(200, json=provider_success() if mode == "success" else {})

    async def scenario():
        worker = TelegramOutboxWorker()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                transport = TelegramSenderTransport(sender(client), message_type="public_watchlist")
                kwargs = dict(worker=worker, database_path=db_path, event_id=event_id, reservation_id=reservation_id,
                              transport=transport, expected_identity=SYNTHETIC_IDENTITY, now=NOW)
                result = await deliver_single_reserved_part(**kwargs)
                assert result.durable_receipt.delivery_state == expected
                assert result.durable_receipt.all_parts_sent is (expected == SENT)
                again = await deliver_single_reserved_part(**kwargs)
                assert again.status in {"blocked", "skipped"}
                assert len(calls) == 1
                with connect_database(db_path) as connection:
                    assert list_outbox_parts(connection, event_id=event_id)[0].delivery_state == expected
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_multipart_success_then_uncertainty_blocks_replay_and_next_part(tmp_path):
    db_path = tmp_path / "multipart.db"
    event_id, reservation_id = seed(db_path, multipart=True)
    calls = []

    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(200, json=provider_success()) if len(calls) == 1 else httpx.Response(503, json={})

    async def scenario():
        worker = TelegramOutboxWorker()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                transport = TelegramSenderTransport(sender(client), message_type="public_watchlist")
                kwargs = dict(worker=worker, database_path=db_path, event_id=event_id, reservation_id=reservation_id,
                              transport=transport, expected_identity=SYNTHETIC_IDENTITY, now=NOW)
                first = await deliver_single_reserved_part(**kwargs)
                # Durable receipt reports the aggregate event state for multipart.
                assert first.transport_outcome.delivery_state == SENT
                assert first.durable_receipt.delivery_state == "IN_FLIGHT"
                assert not first.durable_receipt.all_parts_sent
                assert len(calls) == 1
                attempt_id = first.reservation_receipt.attempt_id
                replay = await deliver_single_reserved_part(**kwargs, attempt_id=attempt_id)
                assert replay.status == "blocked"
                assert len(calls) == 1
                second = await deliver_single_reserved_part(**kwargs, part_index=2, attempt_id=attempt_id)
                assert second.durable_receipt.delivery_state == UNCERTAIN
                blocked = await deliver_single_reserved_part(**kwargs, part_index=2, attempt_id=attempt_id)
                assert blocked.status == "blocked"
                assert len(calls) == 2
                assert all(json.loads(call.content)["text"] == "A" * 40 for call in calls)
                with connect_database(db_path) as connection:
                    parts = list_outbox_parts(connection, event_id=event_id)
                    assert [part.delivery_state for part in parts] == [SENT, UNCERTAIN]
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_fatal_runtime_identity_mismatch_propagates_before_adapter(tmp_path):
    db_path = tmp_path / "fatal-epoch.db"
    event_id, reservation_id = seed(db_path)
    real_sender = sender(None)
    real_sender.send_part = AsyncMock()
    adapter = AsyncMock(wraps=TelegramSenderTransport(real_sender, message_type="public_watchlist"))

    async def scenario():
        worker = TelegramOutboxWorker()
        try:
            with pytest.raises(RuntimeEpochError):
                await deliver_single_reserved_part(
                    worker=worker, database_path=db_path, event_id=event_id, reservation_id=reservation_id,
                    transport=adapter, expected_identity=replace(SYNTHETIC_IDENTITY, epoch_id="foreign"), now=NOW,
                )
            adapter.assert_not_awaited()
            real_sender.send_part.assert_not_awaited()
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_rate_limit_retry_requires_durable_admission_and_respects_attempt_limit(tmp_path):
    db_path = tmp_path / "retry.db"
    event_id, reservation_id = seed(db_path)
    calls = []

    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(429, json={"ok": False, "parameters": {"retry_after": 60}})

    async def scenario():
        worker = TelegramOutboxWorker()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                transport = TelegramSenderTransport(sender(client), message_type="public_watchlist")
                kwargs = dict(worker=worker, database_path=db_path, event_id=event_id, reservation_id=reservation_id,
                              transport=transport, expected_identity=SYNTHETIC_IDENTITY)
                first = await deliver_single_reserved_part(**kwargs, now=NOW)
                assert first.durable_receipt.delivery_state == RETRYABLE
                assert len(calls) == 1
                early = await deliver_single_reserved_part(**kwargs, now=NOW)
                assert early.status == "skipped"
                assert len(calls) == 1
                for index, later in enumerate(("2026-07-16T10:01:00Z", "2026-07-16T10:02:00Z"), start=2):
                    result = await deliver_single_reserved_part(**kwargs, now=later)
                    assert result.transport_outcome.delivery_state == RETRYABLE
                    assert len(calls) == index
                exhausted = await deliver_single_reserved_part(**kwargs, now="2026-07-16T10:03:00Z")
                assert exhausted.status in {"blocked", "skipped"}
                assert len(calls) == 3
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_reserved_second_part_is_sent_once_with_exact_guard_and_no_splitting():
    calls = []

    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(200, json=provider_success())

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
            real_sender = sender(client)
            real_sender.send_part = AsyncMock(wraps=real_sender.send_part)
            real_sender.send_text = AsyncMock()
            part_request = request(part_index=2, part_count=3)
            result = await TelegramSenderTransport(real_sender, message_type="public_watchlist")(part_request)
            assert result.delivery_state == SENT
            assert len(calls) == 1
            assert real_sender.send_part.await_count == 1
            kwargs = real_sender.send_part.call_args.kwargs
            assert (kwargs["part_number"], kwargs["total_parts"]) == (2, 3)
            guard = kwargs["public_watchlist_guard"]
            assert (guard.event_id, guard.reservation_id, guard.event_key, guard.attempt_id) == (
                part_request.event_id, part_request.reservation_id, part_request.event_key, part_request.attempt_id,
            )
            real_sender.send_text.assert_not_awaited()

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["missing_canonical", "wrong_reservation", "wrong_epoch"])
def test_authority_failure_invokes_neither_adapter_nor_http(tmp_path, failure):
    db_path = tmp_path / "authority.db"
    event_id, reservation_id = seed(db_path, bind_reservation=failure != "missing_canonical")
    if failure == "wrong_epoch":
        with connect_database(db_path) as connection:
            connection.execute("UPDATE public_alert_events SET runtime_epoch_id = 'other-epoch' WHERE id = ?", (event_id,))
            connection.commit()
    real_sender = sender(None)
    real_sender.send_part = AsyncMock()
    adapter = AsyncMock(wraps=TelegramSenderTransport(real_sender, message_type="public_watchlist"))

    async def scenario():
        worker = TelegramOutboxWorker()
        try:
            result = await deliver_single_reserved_part(
                worker=worker, database_path=db_path, event_id=event_id,
                reservation_id=reservation_id + 100 if failure == "wrong_reservation" else reservation_id,
                transport=adapter, expected_identity=SYNTHETIC_IDENTITY, now=NOW,
            )
            assert result.status in {"blocked", "skipped"}
            adapter.assert_not_awaited()
            real_sender.send_part.assert_not_awaited()
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_transport_cancellation_is_durably_uncertain_and_not_replayed(tmp_path):
    db_path = tmp_path / "cancel.db"
    event_id, reservation_id = seed(db_path)
    calls = []

    async def scenario():
        started = asyncio.Event()

        async def handler(http_request):
            calls.append(http_request)
            started.set()
            await asyncio.Event().wait()

        worker = TelegramOutboxWorker()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                transport = TelegramSenderTransport(sender(client), message_type="public_watchlist")
                kwargs = dict(worker=worker, database_path=db_path, event_id=event_id, reservation_id=reservation_id,
                              transport=transport, expected_identity=SYNTHETIC_IDENTITY, now=NOW)
                task = asyncio.create_task(deliver_single_reserved_part(**kwargs))
                await asyncio.wait_for(started.wait(), timeout=5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
                with connect_database(db_path) as connection:
                    assert list_outbox_parts(connection, event_id=event_id)[0].delivery_state == UNCERTAIN
                assert (await deliver_single_reserved_part(**kwargs)).status == "blocked"
                assert len(calls) == 1
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_two_protocol_workers_can_invoke_actual_sender_only_once(tmp_path):
    db_path = tmp_path / "race.db"
    event_id, reservation_id = seed(db_path)
    calls = []

    async def scenario():
        entered = asyncio.Event()
        release = asyncio.Event()

        async def handler(http_request):
            calls.append(http_request)
            entered.set()
            await release.wait()
            return httpx.Response(200, json=provider_success())

        first_worker, second_worker = TelegramOutboxWorker(), TelegramOutboxWorker()
        first_task = None
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                transport = TelegramSenderTransport(sender(client), message_type="public_watchlist")
                kwargs = dict(database_path=db_path, event_id=event_id, reservation_id=reservation_id,
                              transport=transport, expected_identity=SYNTHETIC_IDENTITY, now=NOW)
                first_task = asyncio.create_task(deliver_single_reserved_part(worker=first_worker, **kwargs))
                await asyncio.wait_for(entered.wait(), timeout=5)
                loser = await deliver_single_reserved_part(worker=second_worker, **kwargs)
                assert loser.status in {"blocked", "skipped"}
                assert len(calls) == 1
                release.set()
                winner = await first_task
                assert winner.durable_receipt.delivery_state == SENT
                assert len(calls) == 1
        finally:
            release.set()
            if first_task is not None and not first_task.done():
                await first_task
            await first_worker.aclose()
            await second_worker.aclose()

    asyncio.run(scenario())


def test_all_multipart_acknowledgments_required_before_event_sent(tmp_path):
    db_path = tmp_path / "multipart-sent.db"
    event_id, reservation_id = seed(db_path, multipart=True)
    calls = []

    def handler(http_request):
        calls.append(http_request)
        return httpx.Response(200, json=provider_success(100 + len(calls)))

    async def scenario():
        worker = TelegramOutboxWorker()
        try:
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://telegram.invalid") as client:
                transport = TelegramSenderTransport(sender(client), message_type="public_watchlist")
                kwargs = dict(worker=worker, database_path=db_path, event_id=event_id, reservation_id=reservation_id,
                              transport=transport, expected_identity=SYNTHETIC_IDENTITY, now=NOW)
                first = await deliver_single_reserved_part(**kwargs)
                assert first.transport_outcome.telegram_message_id == "101"
                assert not first.durable_receipt.all_parts_sent
                second = await deliver_single_reserved_part(
                    **kwargs, part_index=2, attempt_id=first.reservation_receipt.attempt_id,
                )
                assert second.transport_outcome.telegram_message_id == "102"
                assert second.durable_receipt.delivery_state == SENT
                assert second.durable_receipt.all_parts_sent
                assert len(calls) == 2
                with connect_database(db_path) as connection:
                    parts = list_outbox_parts(connection, event_id=event_id)
                    assert [part.delivery_state for part in parts] == [SENT, SENT]
                    event = connection.execute("SELECT delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
                    assert event[0] == SENT
        finally:
            await worker.aclose()

    asyncio.run(scenario())


def test_preflight_terminal_reason_is_persisted_without_http(tmp_path):
    db_path = tmp_path / "dry-run.db"
    event_id, reservation_id = seed(db_path)
    real_sender = sender(None, dry_run=True)
    real_sender.send_part = AsyncMock()

    async def scenario():
        worker = TelegramOutboxWorker()
        try:
            transport = TelegramSenderTransport(real_sender, message_type="public_watchlist")
            kwargs = dict(worker=worker, database_path=db_path, event_id=event_id, reservation_id=reservation_id,
                          transport=transport, expected_identity=SYNTHETIC_IDENTITY, now=NOW)
            result = await deliver_single_reserved_part(**kwargs)
            assert result.durable_receipt.delivery_state == FAILED_FINAL
            assert result.transport_outcome.error_category == "sender_preflight:telegram_dry_run_enabled"
            assert (await deliver_single_reserved_part(**kwargs)).status == "blocked"
            real_sender.send_part.assert_not_awaited()
            with connect_database(db_path) as connection:
                row = connection.execute("SELECT last_error_category, delivery_state FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
                assert tuple(row) == ("sender_preflight:telegram_dry_run_enabled", FAILED_FINAL)
        finally:
            await worker.aclose()

    asyncio.run(scenario())
