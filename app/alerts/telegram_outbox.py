from __future__ import annotations

import hashlib
import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Iterator, Mapping, Sequence

from app.alerts.templates import TELEGRAM_MAX_MESSAGE_LENGTH, split_message
from app.data.dtos import NA

PENDING = "PENDING"
IN_FLIGHT = "IN_FLIGHT"
SENT = "SENT"
RETRYABLE = "RETRYABLE"
UNCERTAIN = "UNCERTAIN"
FAILED_FINAL = "FAILED_FINAL"
SKIPPED_DRY_RUN = "SKIPPED_DRY_RUN"
POLICY_DISABLED = "POLICY_DISABLED"

AUTO_CLAIMABLE_STATES = frozenset({PENDING, RETRYABLE})
TERMINAL_STATES = frozenset({SENT, UNCERTAIN, FAILED_FINAL, SKIPPED_DRY_RUN, POLICY_DISABLED})
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_LEASE_SECONDS = 300
DEFAULT_BACKOFF_SECONDS = 5
MAX_BACKOFF_SECONDS = 300


@dataclass(frozen=True)
class TelegramOutboxPart:
    id: int
    event_id: int
    event_key: str
    part_index: int
    part_count: int
    payload_text: str
    payload_hash: str
    delivery_state: str
    attempt_count: int
    next_retry_at: str | None = None
    telegram_message_id: str | None = None
    telegram_chat_id: str | None = None


@dataclass(frozen=True)
class TelegramOutboxClaim:
    event_id: int
    reservation_id: int
    event_key: str
    attempt_id: str
    attempt_count: int
    parts: tuple[TelegramOutboxPart, ...]


@dataclass(frozen=True)
class TelegramOutboxClaimResult:
    claim: TelegramOutboxClaim | None
    state: str
    reason: str


def persist_intent_parts(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    event_key: str,
    message_text: str,
    message_hash: str,
    destination_chat_id: str = NA,
    destination_kind: str = NA,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    max_message_length: int = TELEGRAM_MAX_MESSAGE_LENGTH,
) -> tuple[TelegramOutboxPart, ...]:
    """Persist the exact payload and every Telegram part in the intent transaction."""

    chunks = split_message(message_text, max_message_length)
    now = _now_iso()
    connection.execute(
        """
        UPDATE public_alert_events
        SET delivery_state = ?, payload_text = ?, message_hash = ?,
            destination_chat_id = ?, destination_kind = ?, part_count = ?,
            max_attempts = ?, updated_at = ?
        WHERE id = ?
        """,
        (
            PENDING,
            message_text,
            message_hash,
            _text(destination_chat_id),
            _text(destination_kind),
            len(chunks),
            max(1, int(max_attempts)),
            now,
            int(event_id),
        ),
    )
    for part_index, chunk in enumerate(chunks, start=1):
        connection.execute(
            """
            INSERT OR IGNORE INTO public_alert_delivery_parts (
                public_alert_event_id, event_key, part_index, part_count,
                payload_text, payload_hash, delivery_state, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(event_id),
                event_key,
                part_index,
                len(chunks),
                chunk,
                hashlib.sha256(chunk.encode("utf-8")).hexdigest(),
                PENDING,
                now,
                now,
            ),
        )
    return list_outbox_parts(connection, event_id=event_id)


def list_outbox_parts(
    connection: sqlite3.Connection,
    *,
    event_id: int,
    states: Sequence[str] | None = None,
) -> tuple[TelegramOutboxPart, ...]:
    params: list[Any] = [int(event_id)]
    state_clause = ""
    if states:
        normalized = tuple(_state(state) for state in states)
        placeholders = ",".join("?" for _ in normalized)
        state_clause = f"AND delivery_state IN ({placeholders})"
        params.extend(normalized)
    rows = connection.execute(
        f"""
        SELECT * FROM public_alert_delivery_parts
        WHERE public_alert_event_id = ? {state_clause}
        ORDER BY part_index ASC
        """,
        params,
    ).fetchall()
    return tuple(_part_from_row(row) for row in rows)


class SQLitePublicTelegramOutbox:
    """Transactional state machine over the canonical public alert ledger."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    def recover_stale_in_flight(self, *, event_id: int, now: str | None = None) -> bool:
        with self._transaction():
            return self._recover_stale_locked(event_id=event_id, now=now or _now_iso())

    def claim(
        self,
        *,
        event_id: int,
        reservation_id: int,
        now: str | None = None,
        owner: str | None = None,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        attempt_id: str | None = None,
    ) -> TelegramOutboxClaimResult:
        timestamp = now or _now_iso()
        delivery_attempt_id = attempt_id or uuid.uuid4().hex
        lease_expires_at = _add_seconds(timestamp, max(1, int(lease_seconds)))
        with self._transaction():
            self._recover_stale_locked(event_id=event_id, now=timestamp)
            row = self.connection.execute(
                "SELECT * FROM public_alert_events WHERE id = ?", (int(event_id),)
            ).fetchone()
            if row is None:
                return TelegramOutboxClaimResult(None, FAILED_FINAL, "outbox_intent_missing")
            state = _state(row["delivery_state"])
            if state == RETRYABLE and not _retry_due(row["next_retry_at"], timestamp):
                return TelegramOutboxClaimResult(None, RETRYABLE, "retry_not_due")
            if state not in AUTO_CLAIMABLE_STATES:
                return TelegramOutboxClaimResult(None, state, f"outbox_not_claimable:{state.lower()}")
            attempt_count = int(row["attempt_count"] or 0)
            max_attempts = max(1, int(row["max_attempts"] or DEFAULT_MAX_ATTEMPTS))
            if attempt_count >= max_attempts:
                self._mark_event_locked(
                    event_id, reservation_id, FAILED_FINAL,
                    "retry_limit_exhausted", "Telegram delivery retry limit exhausted.", timestamp,
                )
                return TelegramOutboxClaimResult(None, FAILED_FINAL, "retry_limit_exhausted")
            cursor = self.connection.execute(
                """
                UPDATE public_alert_events
                SET delivery_state = ?, attempt_id = ?, claim_owner = ?, claimed_at = ?,
                    attempt_started_at = ?, lease_expires_at = ?, attempt_count = attempt_count + 1,
                    next_retry_at = NULL, last_error_category = ?, last_error_detail = ?, updated_at = ?
                WHERE id = ? AND delivery_state IN (?, ?)
                  AND (delivery_state != ? OR next_retry_at IS NULL OR datetime(next_retry_at) <= datetime(?))
                """,
                (
                    IN_FLIGHT, delivery_attempt_id, owner or uuid.uuid4().hex,
                    timestamp, timestamp, lease_expires_at, NA, NA, timestamp,
                    int(event_id), PENDING, RETRYABLE, RETRYABLE, timestamp,
                ),
            )
            if cursor.rowcount != 1:
                current = self.connection.execute(
                    "SELECT delivery_state FROM public_alert_events WHERE id = ?", (int(event_id),)
                ).fetchone()
                return TelegramOutboxClaimResult(
                    None, _state(current[0]) if current else FAILED_FINAL, "atomic_claim_lost"
                )
            self.connection.execute(
                """
                UPDATE telegram_alert_attempts
                SET telegram_status = 'in_flight', delivery_state = ?, delivery_attempt_id = ?,
                    delivery_attempt_count = delivery_attempt_count + 1,
                    delivery_next_retry_at = NULL, delivery_last_error_category = ?
                WHERE id = ?
                """,
                (IN_FLIGHT, delivery_attempt_id, NA, int(reservation_id)),
            )
            parts = list_outbox_parts(self.connection, event_id=event_id, states=(PENDING, RETRYABLE))
            if not parts:
                sent_parts = list_outbox_parts(self.connection, event_id=event_id, states=(SENT,))
                if sent_parts:
                    self._finalize_sent_locked(event_id, reservation_id, timestamp)
                    return TelegramOutboxClaimResult(None, SENT, "all_parts_already_sent")
                self._mark_event_locked(
                    event_id, reservation_id, FAILED_FINAL,
                    "outbox_parts_missing", "Committed Telegram intent has no delivery parts.", timestamp,
                )
                return TelegramOutboxClaimResult(None, FAILED_FINAL, "outbox_parts_missing")
            return TelegramOutboxClaimResult(
                TelegramOutboxClaim(
                    int(event_id), int(reservation_id), str(row["event_key"]),
                    delivery_attempt_id, attempt_count + 1, parts,
                ),
                IN_FLIGHT,
                NA,
            )

    def mark_part_in_flight(
        self, *, part_id: int, attempt_id: str, now: str | None = None
    ) -> bool:
        timestamp = now or _now_iso()
        with self._transaction():
            cursor = self.connection.execute(
                """
                UPDATE public_alert_delivery_parts
                SET delivery_state = ?, attempt_id = ?, attempt_count = attempt_count + 1,
                    attempt_started_at = ?, next_retry_at = NULL,
                    last_error_category = ?, last_error_detail = ?, updated_at = ?
                WHERE id = ? AND delivery_state IN (?, ?)
                """,
                (IN_FLIGHT, attempt_id, timestamp, NA, NA, timestamp, int(part_id), PENDING, RETRYABLE),
            )
            return cursor.rowcount == 1

    def record_part_result(
        self,
        *,
        event_id: int,
        reservation_id: int,
        part_id: int,
        attempt_id: str,
        result: Mapping[str, Any],
        now: str | None = None,
    ) -> str:
        timestamp = now or _now_iso()
        result_state = _state(result.get("delivery_state") or result.get("status"))
        if result_state not in {SENT, RETRYABLE, UNCERTAIN, FAILED_FINAL}:
            result_state = UNCERTAIN
        category = _text(result.get("error_category"))
        detail = _text(result.get("error"))
        http_status = _int_or_none(result.get("http_status"))
        retry_after = _float_or_none(result.get("retry_after"))
        message_id = _optional_text(result.get("message_id"))
        chat_id = _optional_text(result.get("chat_id"))
        if result_state == SENT and message_id is None:
            result_state = UNCERTAIN
            category = "confirmed_success_missing_message_id"
            detail = "Telegram success could not be proven without a message ID."
        sent_at = _optional_text(result.get("sent_at")) or (timestamp if result_state == SENT else None)
        with self._transaction():
            event = self.connection.execute(
                "SELECT attempt_count, max_attempts FROM public_alert_events WHERE id = ? AND attempt_id = ?",
                (int(event_id), attempt_id),
            ).fetchone()
            if event is None:
                return UNCERTAIN
            attempt_count = int(event["attempt_count"] or 0)
            if result_state == RETRYABLE and attempt_count >= max(1, int(event["max_attempts"] or 1)):
                result_state = FAILED_FINAL
                category = "retry_limit_exhausted"
                detail = "Telegram delivery retry limit exhausted."
            next_retry_at = None
            if result_state == RETRYABLE:
                delay = retry_after if retry_after is not None else _backoff_seconds(attempt_count)
                next_retry_at = _add_seconds(timestamp, delay)
            cursor = self.connection.execute(
                """
                UPDATE public_alert_delivery_parts
                SET delivery_state = ?, sent_at = ?, telegram_message_id = ?, telegram_chat_id = ?,
                    http_status = ?, retry_after_seconds = ?, next_retry_at = ?,
                    last_error_category = ?, last_error_detail = ?, updated_at = ?
                WHERE id = ? AND delivery_state = ? AND attempt_id = ?
                """,
                (
                    result_state, sent_at if result_state == SENT else None,
                    message_id if result_state == SENT else None,
                    chat_id if result_state == SENT else None,
                    http_status, retry_after, next_retry_at,
                    NA if result_state == SENT else category,
                    NA if result_state == SENT else detail,
                    timestamp, int(part_id), IN_FLIGHT, attempt_id,
                ),
            )
            if cursor.rowcount != 1:
                self._mark_event_locked(
                    event_id, reservation_id, UNCERTAIN,
                    "part_result_persistence_conflict",
                    "Telegram part result could not be matched to the active attempt.", timestamp,
                )
                return UNCERTAIN
            if result_state == SENT:
                remaining = self.connection.execute(
                    "SELECT COUNT(*) FROM public_alert_delivery_parts WHERE public_alert_event_id = ? AND delivery_state != ?",
                    (int(event_id), SENT),
                ).fetchone()[0]
                if int(remaining) == 0:
                    self._finalize_sent_locked(event_id, reservation_id, timestamp)
                    return SENT
                return IN_FLIGHT
            self._mark_event_locked(
                event_id, reservation_id, result_state, category, detail, timestamp, next_retry_at
            )
            return result_state

    def mark_terminal_without_send(
        self,
        *,
        event_id: int,
        reservation_id: int,
        state: str,
        reason: str,
        now: str | None = None,
    ) -> None:
        terminal = _state(state)
        if terminal not in {SKIPPED_DRY_RUN, POLICY_DISABLED, FAILED_FINAL}:
            raise ValueError(f"Unsupported no-send terminal state: {state}")
        timestamp = now or _now_iso()
        with self._transaction():
            self.connection.execute(
                """
                UPDATE public_alert_delivery_parts
                SET delivery_state = ?, last_error_category = ?, last_error_detail = ?, updated_at = ?
                WHERE public_alert_event_id = ? AND delivery_state = ?
                """,
                (terminal, reason, reason, timestamp, int(event_id), PENDING),
            )
            self._mark_event_locked(
                event_id, reservation_id, terminal, reason, reason, timestamp
            )

    def mark_uncertain_after_persistence_failure(
        self, *, event_id: int, reservation_id: int, detail: str, now: str | None = None
    ) -> None:
        try:
            with self._transaction():
                self._mark_event_locked(
                    event_id, reservation_id, UNCERTAIN,
                    "sent_persistence_failed", detail, now or _now_iso(),
                )
        except sqlite3.Error:
            return

    def diagnostics(self) -> tuple[dict[str, Any], ...]:
        rows = self.connection.execute(
            """
            SELECT id, event_key, canonical_plan_id, delivery_state, attempt_id,
                   attempt_count, max_attempts, last_error_category, next_retry_at,
                   telegram_message_id, telegram_chat_id, part_count, created_at,
                   claimed_at, attempt_started_at,
                   CAST((julianday('now') - julianday(created_at)) * 86400 AS INTEGER) AS age_seconds
            FROM public_alert_events ORDER BY id ASC
            """
        ).fetchall()
        return tuple(dict(row) for row in rows)

    def _recover_stale_locked(self, *, event_id: int, now: str) -> bool:
        cursor = self.connection.execute(
            """
            UPDATE public_alert_events
            SET delivery_state = ?, uncertain_at = COALESCE(uncertain_at, ?),
                last_error_category = ?, last_error_detail = ?, updated_at = ?
            WHERE id = ? AND delivery_state = ?
              AND (lease_expires_at IS NULL OR datetime(lease_expires_at) <= datetime(?))
            """,
            (
                UNCERTAIN, now, "stale_in_flight_acceptance_unknown",
                "A prior Telegram attempt started but did not reach a durable terminal result.",
                now, int(event_id), IN_FLIGHT, now,
            ),
        )
        if cursor.rowcount != 1:
            return False
        self.connection.execute(
            """
            UPDATE public_alert_delivery_parts
            SET delivery_state = ?, last_error_category = ?, last_error_detail = ?, updated_at = ?
            WHERE public_alert_event_id = ? AND delivery_state = ?
            """,
            (
                UNCERTAIN, "stale_in_flight_acceptance_unknown",
                "The process ended after the delivery attempt began.",
                now, int(event_id), IN_FLIGHT,
            ),
        )
        self.connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET telegram_status = 'uncertain', delivery_state = ?,
                delivery_last_error_category = ?, error_message = ?, last_error_message = ?
            WHERE public_watchlist_event_key = (
                SELECT event_key FROM public_alert_events WHERE id = ?
            )
            """,
            (
                UNCERTAIN, "stale_in_flight_acceptance_unknown",
                "stale_in_flight_acceptance_unknown", "stale_in_flight_acceptance_unknown",
                int(event_id),
            ),
        )
        return True

    def _mark_event_locked(
        self,
        event_id: int,
        reservation_id: int,
        state: str,
        error_category: str,
        error_detail: str,
        now: str,
        next_retry_at: str | None = None,
    ) -> None:
        normalized = _state(state)
        status = "SENT" if normalized == SENT else "FAILED" if normalized in TERMINAL_STATES else "RESERVED"
        self.connection.execute(
            """
            UPDATE public_alert_events
            SET status = ?, delivery_state = ?, next_retry_at = ?, failure_reason = ?,
                last_error_category = ?, last_error_detail = ?, lease_expires_at = NULL,
                uncertain_at = CASE WHEN ? = ? THEN COALESCE(uncertain_at, ?) ELSE uncertain_at END,
                completed_at = CASE WHEN ? IN (?, ?, ?, ?) THEN COALESCE(completed_at, ?) ELSE completed_at END,
                updated_at = ?
            WHERE id = ?
            """,
            (
                status, normalized, next_retry_at,
                NA if normalized == SENT else _text(error_detail),
                NA if normalized == SENT else _text(error_category),
                NA if normalized == SENT else _text(error_detail),
                normalized, UNCERTAIN, now, normalized,
                SENT, FAILED_FINAL, SKIPPED_DRY_RUN, POLICY_DISABLED, now, now, int(event_id),
            ),
        )
        self.connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET telegram_status = ?, delivery_state = ?, delivery_next_retry_at = ?,
                delivery_last_error_category = ?, blocked_reason = ?, error_message = ?,
                last_error_message = ?, last_seen_at = ?
            WHERE id = ?
            """,
            (
                normalized.lower(), normalized, next_retry_at,
                NA if normalized == SENT else _text(error_category),
                NA if normalized == SENT else _text(error_detail),
                NA if normalized == SENT else _text(error_detail),
                NA if normalized == SENT else _text(error_detail),
                now, int(reservation_id),
            ),
        )

    def _finalize_sent_locked(self, event_id: int, reservation_id: int, now: str) -> None:
        parts = list_outbox_parts(self.connection, event_id=event_id)
        if not parts or any(part.delivery_state != SENT for part in parts):
            raise sqlite3.IntegrityError("Cannot finalize Telegram outbox before every part is SENT.")
        rows = self.connection.execute(
            """
            SELECT telegram_message_id, telegram_chat_id, sent_at
            FROM public_alert_delivery_parts
            WHERE public_alert_event_id = ? ORDER BY part_index
            """,
            (int(event_id),),
        ).fetchall()
        message_ids = [str(row["telegram_message_id"]) for row in rows if row["telegram_message_id"]]
        chat_ids = [str(row["telegram_chat_id"]) for row in rows if row["telegram_chat_id"]]
        sent_times = [str(row["sent_at"]) for row in rows if row["sent_at"]]
        sent_at = max(sent_times) if sent_times else now
        message_id_summary = ",".join(message_ids) if message_ids else None
        chat_id = chat_ids[0] if chat_ids else None
        self.connection.execute(
            """
            UPDATE public_alert_events
            SET status = 'SENT', delivery_state = ?, sent_at = ?, completed_at = ?,
                telegram_message_id = ?, telegram_chat_id = ?, failure_reason = ?,
                last_error_category = ?, last_error_detail = ?, lease_expires_at = NULL,
                next_retry_at = NULL, updated_at = ?
            WHERE id = ?
            """,
            (SENT, sent_at, now, message_id_summary, chat_id, NA, NA, NA, now, int(event_id)),
        )
        self.connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET sent_at = ?, telegram_status = 'sent', delivery_state = ?,
                telegram_message_id = ?, telegram_chat_id = ?, blocked_reason = ?,
                error_message = ?, last_error_message = ?, delivery_last_error_category = ?,
                delivery_next_retry_at = NULL, last_seen_at = ?
            WHERE id = ?
            """,
            (sent_at, SENT, message_id_summary, chat_id, NA, NA, NA, NA, now, int(reservation_id)),
        )

    @contextmanager
    def _transaction(self) -> Iterator[None]:
        if self.connection.in_transaction:
            self.connection.commit()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()


def _part_from_row(row: sqlite3.Row) -> TelegramOutboxPart:
    return TelegramOutboxPart(
        id=int(row["id"]),
        event_id=int(row["public_alert_event_id"]),
        event_key=str(row["event_key"]),
        part_index=int(row["part_index"]),
        part_count=int(row["part_count"]),
        payload_text=str(row["payload_text"]),
        payload_hash=str(row["payload_hash"]),
        delivery_state=_state(row["delivery_state"]),
        attempt_count=int(row["attempt_count"] or 0),
        next_retry_at=row["next_retry_at"],
        telegram_message_id=row["telegram_message_id"],
        telegram_chat_id=row["telegram_chat_id"],
    )


def _state(value: Any) -> str:
    return str(value or "").strip().upper() or FAILED_FINAL


def _text(value: Any) -> str:
    text = str(value).strip() if value is not None else ""
    return text or NA


def _optional_text(value: Any) -> str | None:
    text = str(value).strip() if value is not None else ""
    return text or None


def _int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float_or_none(value: Any) -> float | None:
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)


def _add_seconds(value: str, seconds: float) -> str:
    return (_parse_iso(value) + timedelta(seconds=max(0.0, float(seconds)))).isoformat().replace("+00:00", "Z")


def _retry_due(next_retry_at: Any, now: str) -> bool:
    if not next_retry_at:
        return True
    try:
        return _parse_iso(str(next_retry_at)) <= _parse_iso(now)
    except ValueError:
        return False


def _backoff_seconds(attempt_count: int) -> float:
    exponent = max(0, int(attempt_count) - 1)
    return float(min(MAX_BACKOFF_SECONDS, DEFAULT_BACKOFF_SECONDS * (2**exponent)))


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "FAILED_FINAL",
    "IN_FLIGHT",
    "PENDING",
    "POLICY_DISABLED",
    "RETRYABLE",
    "SENT",
    "SKIPPED_DRY_RUN",
    "SQLitePublicTelegramOutbox",
    "TelegramOutboxClaim",
    "TelegramOutboxClaimResult",
    "TelegramOutboxPart",
    "UNCERTAIN",
    "list_outbox_parts",
    "persist_intent_parts",
]
