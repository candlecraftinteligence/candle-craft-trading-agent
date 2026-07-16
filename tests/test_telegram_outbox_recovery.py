from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
import pytest

from app.alerts.telegram import send_telegram_message_part
from app.alerts.telegram_outbox import (
    FAILED_FINAL,
    IN_FLIGHT,
    PENDING,
    RETRYABLE,
    SENT,
    SKIPPED_DRY_RUN,
    UNCERTAIN,
    SQLitePublicTelegramOutbox,
    persist_intent_parts,
)
from app.storage.database import connect_database, open_initialized_database

NOW = "2026-07-16T10:00:00Z"
EVENT_KEY = "plan-1|initial_watchlist"


def run(coro):
    return asyncio.run(coro)


def _seed_intent(
    db_path: Path,
    *,
    message: str = "compact setup",
    max_attempts: int = 3,
    max_message_length: int = 4096,
) -> tuple[int, int]:
    with open_initialized_database(db_path) as connection:
        cursor = connection.execute(
            """
            INSERT INTO public_alert_events (
                canonical_plan_id, event_type, event_key, symbol, side, status,
                reserved_at, delivery_state, payload_text, message_hash,
                destination_chat_id, destination_kind, max_attempts, created_at, updated_at
            ) VALUES (?, 'initial_watchlist', ?, 'BTCUSDT', 'long', 'RESERVED',
                      ?, ?, ?, 'hash-1', 'test-chat', 'public_chat', ?, ?, ?)
            """,
            ("plan-1", EVENT_KEY, NOW, PENDING, message, max_attempts, NOW, NOW),
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
                'signal-1', 'BTCUSDT', 'long', 'WATCHLISTED', 'WATCHLIST', 'WATCHLISTED',
                NULL, ?, 'pending', 'hash-1', 'WATCHLIST', 'plan-1', ?,
                'initial_watchlist', ?, 1
            )
            """,
            (NOW, EVENT_KEY, PENDING),
        )
        attempt_id = int(cursor.lastrowid)
        parts = persist_intent_parts(
            connection,
            event_id=event_id,
            event_key=EVENT_KEY,
            message_text=message,
            message_hash="hash-1",
            destination_chat_id="test-chat",
            destination_kind="public_chat",
            max_attempts=max_attempts,
            max_message_length=max_message_length,
        )
        connection.execute(
            "UPDATE telegram_alert_attempts SET delivery_part_count = ? WHERE id = ?",
            (len(parts), attempt_id),
        )
        connection.commit()
    return event_id, attempt_id


def _event(connection: sqlite3.Connection, event_id: int) -> sqlite3.Row:
    row = connection.execute("SELECT * FROM public_alert_events WHERE id = ?", (event_id,)).fetchone()
    assert row is not None
    return row


def test_pending_intent_is_committed_and_safely_claimable_after_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "pending-restart.db"
    event_id, attempt_id = _seed_intent(db_path)

    with connect_database(db_path) as connection:
        assert _event(connection, event_id)["delivery_state"] == PENDING
        claim = SQLitePublicTelegramOutbox(connection).claim(
            event_id=event_id,
            reservation_id=attempt_id,
            now=NOW,
            attempt_id="attempt-1",
            owner="worker-1",
        )

    assert claim.claim is not None
    assert claim.claim.attempt_id == "attempt-1"
    assert claim.claim.attempt_count == 1


def test_two_processes_atomically_claim_one_intent(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent-claim.db"
    event_id, attempt_id = _seed_intent(db_path)

    def claim_once(index: int):
        with connect_database(db_path) as connection:
            return SQLitePublicTelegramOutbox(connection).claim(
                event_id=event_id,
                reservation_id=attempt_id,
                now=NOW,
                attempt_id=f"attempt-{index}",
                owner=f"worker-{index}",
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim_once, (1, 2)))

    assert sum(result.claim is not None for result in results) == 1
    assert {result.state for result in results} <= {IN_FLIGHT}
    with connect_database(db_path) as connection:
        row = _event(connection, event_id)
        assert row["delivery_state"] == IN_FLIGHT
        assert row["attempt_count"] == 1


def test_nonexpired_in_flight_cannot_be_reclaimed(tmp_path: Path) -> None:
    db_path = tmp_path / "active-lease.db"
    event_id, attempt_id = _seed_intent(db_path)
    with connect_database(db_path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        first = outbox.claim(
            event_id=event_id, reservation_id=attempt_id, now=NOW,
            attempt_id="attempt-1", lease_seconds=60,
        )
        second = outbox.claim(
            event_id=event_id, reservation_id=attempt_id,
            now="2026-07-16T10:00:30Z", attempt_id="attempt-2",
        )

    assert first.claim is not None
    assert second.claim is None
    assert second.state == IN_FLIGHT


def test_stale_in_flight_becomes_uncertain_idempotently(tmp_path: Path) -> None:
    db_path = tmp_path / "stale.db"
    event_id, attempt_id = _seed_intent(db_path)
    with connect_database(db_path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        claim = outbox.claim(
            event_id=event_id, reservation_id=attempt_id, now=NOW,
            attempt_id="attempt-1", lease_seconds=1,
        )
        assert claim.claim is not None
        assert outbox.recover_stale_in_flight(
            event_id=event_id, now="2026-07-16T10:00:02Z"
        ) is True
        assert outbox.recover_stale_in_flight(
            event_id=event_id, now="2026-07-16T10:00:03Z"
        ) is False
        row = _event(connection, event_id)
        blocked = outbox.claim(
            event_id=event_id, reservation_id=attempt_id,
            now="2026-07-16T10:01:00Z", attempt_id="attempt-2",
        )

    assert row["delivery_state"] == UNCERTAIN
    assert blocked.claim is None
    assert blocked.state == UNCERTAIN


def test_confirmed_success_persists_message_and_destination_ids(tmp_path: Path) -> None:
    db_path = tmp_path / "sent.db"
    event_id, attempt_id = _seed_intent(db_path)
    with connect_database(db_path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        claim = outbox.claim(
            event_id=event_id, reservation_id=attempt_id, now=NOW,
            attempt_id="attempt-success",
        ).claim
        assert claim is not None
        part = claim.parts[0]
        assert outbox.mark_part_in_flight(part_id=part.id, attempt_id=claim.attempt_id)
        state = outbox.record_part_result(
            event_id=event_id,
            reservation_id=attempt_id,
            part_id=part.id,
            attempt_id=claim.attempt_id,
            now="2026-07-16T10:00:01Z",
            result={
                "delivery_state": SENT,
                "message_id": 901,
                "chat_id": -100123,
                "sent_at": "2026-07-16T10:00:01Z",
                "http_status": 200,
            },
        )
        event = _event(connection, event_id)
        attempt = connection.execute(
            "SELECT * FROM telegram_alert_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()

    assert state == SENT
    assert event["delivery_state"] == SENT
    assert event["telegram_message_id"] == "901"
    assert event["telegram_chat_id"] == "-100123"
    assert attempt["telegram_status"] == "sent"
    assert attempt["telegram_message_id"] == "901"


def test_retryable_failure_respects_next_retry_and_only_retries_unsent_part(tmp_path: Path) -> None:
    db_path = tmp_path / "multipart-retry.db"
    event_id, attempt_id = _seed_intent(
        db_path, message="AAAAA BBBBB CCCCC", max_message_length=5
    )
    with connect_database(db_path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        first = outbox.claim(
            event_id=event_id, reservation_id=attempt_id, now=NOW,
            attempt_id="attempt-1",
        ).claim
        assert first is not None
        assert len(first.parts) == 3
        part1, part2, _part3 = first.parts
        assert outbox.mark_part_in_flight(part_id=part1.id, attempt_id=first.attempt_id)
        assert outbox.record_part_result(
            event_id=event_id, reservation_id=attempt_id, part_id=part1.id,
            attempt_id=first.attempt_id, now=NOW,
            result={"delivery_state": SENT, "message_id": 1, "chat_id": "chat"},
        ) == IN_FLIGHT
        assert outbox.mark_part_in_flight(part_id=part2.id, attempt_id=first.attempt_id)
        assert outbox.record_part_result(
            event_id=event_id, reservation_id=attempt_id, part_id=part2.id,
            attempt_id=first.attempt_id, now=NOW,
            result={
                "delivery_state": RETRYABLE,
                "retry_after": 17,
                "error_category": "telegram_rate_limited",
                "error": "rate limited",
                "http_status": 429,
            },
        ) == RETRYABLE
        early = outbox.claim(
            event_id=event_id, reservation_id=attempt_id,
            now="2026-07-16T10:00:16Z", attempt_id="attempt-early",
        )
        retry = outbox.claim(
            event_id=event_id, reservation_id=attempt_id,
            now="2026-07-16T10:00:17Z", attempt_id="attempt-2",
        ).claim

    assert early.claim is None
    assert early.reason == "retry_not_due"
    assert retry is not None
    assert part1.id not in {part.id for part in retry.parts}
    assert part2.id in {part.id for part in retry.parts}


def test_ambiguous_multipart_part_stops_continuation_and_preserves_prior_sent_part(tmp_path: Path) -> None:
    db_path = tmp_path / "multipart-uncertain.db"
    event_id, attempt_id = _seed_intent(
        db_path, message="AAAAA BBBBB CCCCC", max_message_length=5
    )
    with connect_database(db_path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        claim = outbox.claim(
            event_id=event_id, reservation_id=attempt_id, now=NOW,
            attempt_id="attempt-1",
        ).claim
        assert claim is not None
        part1, part2, _part3 = claim.parts
        assert outbox.mark_part_in_flight(part_id=part1.id, attempt_id=claim.attempt_id)
        outbox.record_part_result(
            event_id=event_id, reservation_id=attempt_id, part_id=part1.id,
            attempt_id=claim.attempt_id,
            result={"delivery_state": SENT, "message_id": 10, "chat_id": "chat"},
        )
        assert outbox.mark_part_in_flight(part_id=part2.id, attempt_id=claim.attempt_id)
        assert outbox.record_part_result(
            event_id=event_id, reservation_id=attempt_id, part_id=part2.id,
            attempt_id=claim.attempt_id,
            result={
                "delivery_state": UNCERTAIN,
                "error_category": "read_timeout",
                "error": "acceptance unknown",
            },
        ) == UNCERTAIN
        parts = connection.execute(
            "SELECT part_index, delivery_state FROM public_alert_delivery_parts ORDER BY part_index"
        ).fetchall()
        blocked = outbox.claim(
            event_id=event_id, reservation_id=attempt_id,
            now="2026-07-16T11:00:00Z", attempt_id="attempt-2",
        )

    assert parts[0][1] == SENT
    assert parts[1][1] == UNCERTAIN
    assert blocked.claim is None
    assert blocked.state == UNCERTAIN


def test_retry_limit_exhaustion_becomes_failed_final(tmp_path: Path) -> None:
    db_path = tmp_path / "retry-limit.db"
    event_id, attempt_id = _seed_intent(db_path, max_attempts=2)
    with connect_database(db_path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        first = outbox.claim(
            event_id=event_id, reservation_id=attempt_id, now=NOW,
            attempt_id="attempt-1",
        ).claim
        assert first is not None
        part = first.parts[0]
        assert outbox.mark_part_in_flight(part_id=part.id, attempt_id=first.attempt_id)
        assert outbox.record_part_result(
            event_id=event_id, reservation_id=attempt_id, part_id=part.id,
            attempt_id=first.attempt_id, now=NOW,
            result={"delivery_state": RETRYABLE, "error_category": "known_5xx", "error": "500"},
        ) == RETRYABLE
        second = outbox.claim(
            event_id=event_id, reservation_id=attempt_id,
            now="2026-07-16T10:00:05Z", attempt_id="attempt-2",
        ).claim
        assert second is not None
        part = second.parts[0]
        assert outbox.mark_part_in_flight(part_id=part.id, attempt_id=second.attempt_id)
        state = outbox.record_part_result(
            event_id=event_id, reservation_id=attempt_id, part_id=part.id,
            attempt_id=second.attempt_id, now="2026-07-16T10:00:05Z",
            result={"delivery_state": RETRYABLE, "error_category": "known_5xx", "error": "500"},
        )
        row = _event(connection, event_id)

    assert state == FAILED_FINAL
    assert row["delivery_state"] == FAILED_FINAL
    assert row["last_error_category"] == "retry_limit_exhausted"


def test_dry_run_terminal_state_never_claims_or_becomes_sent(tmp_path: Path) -> None:
    db_path = tmp_path / "dry-run.db"
    event_id, attempt_id = _seed_intent(db_path)
    with connect_database(db_path) as connection:
        outbox = SQLitePublicTelegramOutbox(connection)
        outbox.mark_terminal_without_send(
            event_id=event_id,
            reservation_id=attempt_id,
            state=SKIPPED_DRY_RUN,
            reason="telegram_dry_run_enabled",
            now=NOW,
        )
        blocked = outbox.claim(
            event_id=event_id, reservation_id=attempt_id,
            now="2026-07-17T10:00:00Z", attempt_id="live-attempt",
        )
        event = _event(connection, event_id)

    assert event["delivery_state"] == SKIPPED_DRY_RUN
    assert event["sent_at"] is None
    assert blocked.claim is None
    assert blocked.state == SKIPPED_DRY_RUN


@pytest.mark.parametrize(
    ("handler", "expected_state", "expected_category"),
    [
        (
            lambda request: (_ for _ in ()).throw(httpx.ConnectError("connect failed", request=request)),
            RETRYABLE,
            "connect_failure_before_transmission",
        ),
        (
            lambda request: httpx.Response(
                429,
                json={"ok": False, "error_code": 429, "parameters": {"retry_after": 12}},
            ),
            RETRYABLE,
            "telegram_rate_limited",
        ),
        (
            lambda request: httpx.Response(503, json={"ok": False, "error_code": 503}),
            RETRYABLE,
            "telegram_server_rejection",
        ),
        (
            lambda request: httpx.Response(400, json={"ok": False, "error_code": 400}),
            FAILED_FINAL,
            "telegram_permanent_rejection",
        ),
        (
            lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("read timeout", request=request)),
            UNCERTAIN,
            "transport_outcome_uncertain",
        ),
        (
            lambda request: (_ for _ in ()).throw(httpx.WriteError("write reset", request=request)),
            UNCERTAIN,
            "transport_outcome_uncertain",
        ),
        (
            lambda request: httpx.Response(200, content=b"not-json"),
            UNCERTAIN,
            "malformed_success_response",
        ),
        (
            lambda request: httpx.Response(200, json={"ok": True, "result": {}}),
            UNCERTAIN,
            "incomplete_success_response",
        ),
    ],
)
def test_transport_failure_classification(handler, expected_state: str, expected_category: str) -> None:
    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://telegram.test"
    )
    try:
        result = run(
            send_telegram_message_part(
                bot_token="test-token",
                chat_id="test-chat",
                message="setup",
                http_client=client,
                api_base_url="https://telegram.test",
            )
        )
    finally:
        run(client.aclose())

    assert result["delivery_state"] == expected_state
    assert result["error_category"] == expected_category
    if result["http_status"] == 429:
        assert result["retry_after"] == 12


def test_confirmed_transport_success_requires_and_returns_message_id() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "ok": True,
                "result": {"message_id": 77, "date": 1784196000, "chat": {"id": -10077}},
            },
        )

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://telegram.test"
    )
    try:
        result = run(
            send_telegram_message_part(
                bot_token="test-token",
                chat_id="test-chat",
                message="setup",
                http_client=client,
                api_base_url="https://telegram.test",
            )
        )
    finally:
        run(client.aclose())

    assert result["delivery_state"] == SENT
    assert result["message_id"] == 77
    assert result["chat_id"] == -10077


def test_outbox_diagnostics_expose_state_retry_and_age_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "diagnostics.db"
    event_id, _attempt_id = _seed_intent(db_path)
    with connect_database(db_path) as connection:
        diagnostic = SQLitePublicTelegramOutbox(connection).diagnostics()[0]

    assert diagnostic["id"] == event_id
    assert diagnostic["delivery_state"] == PENDING
    assert diagnostic["attempt_count"] == 0
    assert "next_retry_at" in diagnostic
    assert "last_error_category" in diagnostic
    assert "age_seconds" in diagnostic
