from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.alerts.telegram_sender import TelegramSender
from app.core.config import Settings
from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import (
    TelegramAlertType,
    TelegramSignalMessage,
    format_telegram_price,
    format_telegram_signal_message,
)
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState, SetupTransitionReason, SetupTransitionResult
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.state_machine import now_utc_iso
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunResult, ScannerSymbolResult
from app.storage.database import DEFAULT_DATABASE_PATH, StorageError, open_initialized_database
from app.storage.models import TelegramAlertAttemptRecord

logger = logging.getLogger(__name__)

WATCH_ALERT_STATES = {
    SetupLifecycleState.WATCHLISTED,
    SetupLifecycleState.STALKING,
    SetupLifecycleState.TRIGGERED,
}
SIGNAL_ALERT_STATES = {
    SetupLifecycleState.CONFIRMED,
    SetupLifecycleState.EXECUTING,
}
PRIOR_ACTIVE_ALERT_TYPES = {
    TelegramAlertType.WATCHLIST.value,
    TelegramAlertType.SIGNAL_CONFIRMED.value,
}
TERMINAL_UPDATE_ALERT_TYPES = {
    TelegramAlertType.INVALIDATED,
    TelegramAlertType.EXPIRED,
    TelegramAlertType.NO_LONGER_TRACKING,
}
TERMINAL_COMPLETION_ALERT_TYPES = {
    TelegramAlertType.INVALIDATED.value,
    TelegramAlertType.EXPIRED.value,
    TelegramAlertType.NO_LONGER_TRACKING.value,
    "COOLDOWN",
    TelegramAlertType.SL_HIT.value,
    TelegramAlertType.TP3_HIT.value,
}
WATCHLIST_OUTCOME_TERMINAL_ALERT_TYPES = {
    TelegramAlertType.TP3_HIT,
    TelegramAlertType.SL_HIT,
}
WATCHLIST_OUTCOME_TRACKING_ATTEMPT = "WATCHLIST_OUTCOME_TRACKING"
WATCHLIST_TERMINAL_SUPPRESSION_ATTEMPT = "WATCHLIST_TERMINAL_SUPPRESSION"
SENT_WATCHLIST_RECONCILIATION_ATTEMPT = "SENT_WATCHLIST_RECONCILIATION"
SENT_WATCHLIST_RECONCILIATION_NO_MATCH = "sent_watchlist_reconciliation_no_lifecycle_match"
SENT_WATCHLIST_RECONCILIATION_AMBIGUOUS = "sent_watchlist_reconciliation_ambiguous"
SOFT_FAILED_CONFIRMATION_ATTEMPT = "SOFT_FAILED_CONFIRMATION"
SOFT_FAILED_CONFIRMATION_MIN_OBSERVATIONS = 3
SOFT_FAILED_CONFIRMATION_REMOVAL_REASON = "Watchlist removed because final confirmation conditions did not improve."
TERMINAL_IDENTITY_BLOCK_REASONS = {
    "terminal_update_no_prior_public_alert",
    "terminal_update_identity_ambiguous",
    "terminal_update_identity_not_matched",
    "terminal_update_not_public_tracked",
    "terminal_update_not_terminal_state",
}
DEFAULT_CONFIRMED_MIN_RR = Decimal("3")
DEFAULT_MIN_TECHNICAL_SCORE = Decimal("50")
CONFIRMED_REJECTED_STATUS_KEYS = {
    "scan_error",
    "scanned_no_setup",
    "rejected_by_technical",
    "rejected_by_derivatives",
    "rejected_by_risk",
    "rejected_by_scoring",
    "rejected_by_regime",
    "failed",
    "near_miss",
    "no_setup",
    "rejected",
}
CONFIRMED_ALLOWED_QUALITY_STATE_KEYS = {
    "high_quality_trade",
    "valid_but_lower_quality",
}
WATCHLIST_BLOCKED_QUALITY_STATE_KEYS = {
    "data_issue",
    "rejected_no_edge",
}
WATCHLIST_HARD_STATUS_BLOCKERS = {
    "scan_error",
    "failed",
}
WATCHLIST_ACTION_KEYS = {
    "watchlist_only",
}
WATCHLIST_LIFECYCLE_ACTION_KEYS = {
    "watchlist",
}
WATCHLIST_STALE_OR_INCOMPLETE_GATES = {
    "entry_window_expired",
    "missing_displacement_impulse",
    "no_displacement_candle",
    "missing_stop",
    "pullback_too_deep",
    "pullback_beyond_786",
    "body_acceptance_failure",
    "structural_breakdown",
}
WATCHLIST_OB_FVG_GATES = {
    "no_ob_or_fvg_zone",
    "challenge_limit_entry_missing",
}
WATCHLIST_RR_GATES = {
    "missing_rr",
    "missing_target",
    "rr_below_minimum",
    "challenge_rr_below_3",
    "rr_too_low",
}
WATCHLIST_CONFIRMATION_GATES = {
    "missing_confirmation_structure_shift",
}
INVALIDATION_REJECTION_FRAGMENTS = (
    "technical score",
    "opportunity score",
    "scanner minimum",
    "below scanner",
    "below minimum",
    "rr below",
    "risk/reward below",
    "missing required",
    "missing field",
    "no valid setup",
    "no setup",
    "near miss",
    "rejected setup",
    "setup rejected",
    "failed gate",
    "gate failed",
    "hard rejection",
)


@dataclass(frozen=True)
class TelegramAlertDecision:
    eligible: bool
    reason: str
    alert_type: TelegramAlertType | None = None
    message: TelegramSignalMessage | None = None
    lifecycle_transition: SetupTransitionResult | None = None


@dataclass(frozen=True)
class TelegramEligibilityContext:
    min_rr: Decimal = DEFAULT_CONFIRMED_MIN_RR
    min_score_for_idea: Decimal | None = None
    min_technical_score: Decimal = DEFAULT_MIN_TECHNICAL_SCORE


@dataclass(frozen=True)
class TelegramLifecycleDelivery:
    symbol: str
    signal_id: str
    alert_type: str
    status: str
    detail: str
    message_hash: str = NA
    error_message: str = NA


@dataclass(frozen=True)
class TelegramLifecycleDeliverySummary:
    attempted: int = 0
    sent: int = 0
    skipped: int = 0
    failed: int = 0
    blocked: int = 0
    blocked_repeat: int = 0
    duplicate: int = 0
    ineligible: int = 0
    deliveries: tuple[TelegramLifecycleDelivery, ...] = ()


@dataclass(frozen=True)
class TerminalIdentityBridge:
    prior_alert: TelegramAlertAttemptRecord | None = None
    blocked_reason: str | None = None


@dataclass(frozen=True)
class SentWatchlistLifecycleMatch:
    record: SetupLifecycleRecord | None = None
    blocked_reason: str | None = None


@dataclass(frozen=True)
class SentWatchlistReconciliationOutcome:
    alert_type: TelegramAlertType
    message: TelegramSignalMessage
    symbol_result: ScannerSymbolResult


@dataclass(frozen=True)
class WatchlistCandleSnapshot:
    high: Decimal
    low: Decimal
    identity: str = NA


class SQLiteTelegramAlertAttemptRepository(AbstractContextManager["SQLiteTelegramAlertAttemptRepository"]):
    def __init__(self, database_path: Path | str = DEFAULT_DATABASE_PATH) -> None:
        self.database_path = Path(database_path)
        self.connection: sqlite3.Connection | None = None

    def __enter__(self) -> SQLiteTelegramAlertAttemptRepository:
        self.connection = open_initialized_database(self.database_path)
        return self

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if self.connection is None:
            return
        if exc_type is None:
            self.connection.commit()
        self.connection.close()
        self.connection = None

    def has_attempt(self, *, signal_id: str, alert_type: TelegramAlertType | str) -> bool:
        row = self._connection.execute(
            """
            SELECT 1 FROM telegram_alert_attempts
            WHERE signal_id = ? AND alert_type = ?
            LIMIT 1
            """,
            (_identity(signal_id), _alert_type_text(alert_type)),
        ).fetchone()
        return row is not None

    def get_attempt(
        self,
        *,
        signal_id: str,
        alert_type: TelegramAlertType | str,
    ) -> TelegramAlertAttemptRecord | None:
        row = self._connection.execute(
            """
            SELECT * FROM telegram_alert_attempts
            WHERE signal_id = ? AND alert_type = ?
            LIMIT 1
            """,
            (_identity(signal_id), _alert_type_text(alert_type)),
        ).fetchone()
        return _record_from_row(row) if row is not None else None

    def has_prior_active_alert(self, *, signal_id: str) -> bool:
        return self.get_prior_public_alert(signal_ids=(signal_id,)) is not None

    def get_prior_public_alert(
        self,
        *,
        signal_ids: Sequence[str],
        symbol: str | None = None,
        direction: str | None = None,
    ) -> TelegramAlertAttemptRecord | None:
        candidates = tuple(dict.fromkeys(_identity(signal_id) for signal_id in signal_ids if _identity(signal_id) != NA))
        if candidates:
            placeholders = ",".join("?" for _ in candidates)
            type_placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
            rows = self._connection.execute(
                f"""
                SELECT * FROM telegram_alert_attempts
                WHERE signal_id IN ({placeholders})
                  AND alert_type IN ({type_placeholders})
                  AND telegram_status = 'sent'
                ORDER BY id ASC
                """,
                (*candidates, *sorted(PRIOR_ACTIVE_ALERT_TYPES)),
            ).fetchall()
            records = tuple(_record_from_row(row) for row in rows)
            if records:
                return _preferred_prior_active_record(records, candidates)
        return None

    def list_active_prior_public_alerts(
        self,
        *,
        symbol: str,
        direction: str | None = None,
    ) -> tuple[TelegramAlertAttemptRecord, ...]:
        normalized_symbol = _symbol(symbol)
        if normalized_symbol == NA:
            return ()
        params: list[Any] = [
            normalized_symbol,
            *sorted(PRIOR_ACTIVE_ALERT_TYPES),
            *sorted(TERMINAL_COMPLETION_ALERT_TYPES),
        ]
        direction_clause = ""
        normalized_direction = _text(direction)
        if normalized_direction != NA:
            direction_clause = "AND prior.direction = ?"
            params.append(normalized_direction)
        type_placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
        terminal_placeholders = ",".join("?" for _ in TERMINAL_COMPLETION_ALERT_TYPES)
        rows = self._connection.execute(
            f"""
            SELECT prior.* FROM telegram_alert_attempts AS prior
            WHERE prior.symbol = ?
              AND prior.alert_type IN ({type_placeholders})
              AND prior.telegram_status = 'sent'
              AND NOT EXISTS (
                  SELECT 1 FROM telegram_alert_attempts AS terminal
                  WHERE terminal.signal_id = prior.signal_id
                    AND terminal.alert_type IN ({terminal_placeholders})
                    AND terminal.telegram_status = 'sent'
              )
              {direction_clause}
            ORDER BY prior.id ASC
            """,
            params,
        ).fetchall()
        return _unique_prior_public_records(tuple(_record_from_row(row) for row in rows))

    def list_prior_public_alerts(
        self,
        *,
        symbol: str,
        direction: str | None = None,
    ) -> tuple[TelegramAlertAttemptRecord, ...]:
        normalized_symbol = _symbol(symbol)
        if normalized_symbol == NA:
            return ()
        params: list[Any] = [normalized_symbol, *sorted(PRIOR_ACTIVE_ALERT_TYPES)]
        direction_clause = ""
        normalized_direction = _text(direction)
        if normalized_direction != NA:
            direction_clause = "AND direction = ?"
            params.append(normalized_direction)
        type_placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
        rows = self._connection.execute(
            f"""
            SELECT * FROM telegram_alert_attempts
            WHERE symbol = ?
              AND alert_type IN ({type_placeholders})
              AND telegram_status = 'sent'
              {direction_clause}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
        return _unique_prior_public_records(tuple(_record_from_row(row) for row in rows))

    def has_prior_public_alert_for_symbol(self, *, symbol: str) -> bool:
        type_placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
        row = self._connection.execute(
            f"""
            SELECT 1 FROM telegram_alert_attempts
            WHERE symbol = ?
              AND alert_type IN ({type_placeholders})
              AND telegram_status = 'sent'
            LIMIT 1
            """,
            (_symbol(symbol), *sorted(PRIOR_ACTIVE_ALERT_TYPES)),
        ).fetchone()
        return row is not None

    def has_sent_terminal_outcome(self, *, signal_id: str) -> bool:
        placeholders = ",".join("?" for _ in TERMINAL_COMPLETION_ALERT_TYPES)
        row = self._connection.execute(
            f"""
            SELECT 1 FROM telegram_alert_attempts
            WHERE signal_id = ?
              AND alert_type IN ({placeholders})
              AND telegram_status = 'sent'
            LIMIT 1
            """,
            (_identity(signal_id), *sorted(TERMINAL_COMPLETION_ALERT_TYPES)),
        ).fetchone()
        return row is not None

    def list_sent_watchlist_alerts(self) -> tuple[TelegramAlertAttemptRecord, ...]:
        terminal_placeholders = ",".join("?" for _ in TERMINAL_COMPLETION_ALERT_TYPES)
        rows = self._connection.execute(
            f"""
            SELECT watch.* FROM telegram_alert_attempts AS watch
            WHERE watch.alert_type = ?
              AND watch.telegram_status = 'sent'
              AND NOT EXISTS (
                  SELECT 1 FROM telegram_alert_attempts AS terminal
                  WHERE terminal.signal_id = watch.signal_id
                    AND terminal.alert_type IN ({terminal_placeholders})
                    AND terminal.telegram_status = 'sent'
              )
            ORDER BY watch.id ASC
            """,
            (TelegramAlertType.WATCHLIST.value, *sorted(TERMINAL_COMPLETION_ALERT_TYPES)),
        ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    def insert_attempt(self, record: TelegramAlertAttemptRecord) -> bool:
        first_seen_at = _text(record.first_seen_at)
        if first_seen_at == NA:
            first_seen_at = _text(record.sent_at)
        if first_seen_at == NA:
            first_seen_at = now_utc_iso()
        last_seen_at = _text(record.last_seen_at)
        if last_seen_at == NA:
            last_seen_at = _text(record.sent_at)
        if last_seen_at == NA:
            last_seen_at = now_utc_iso()
        last_error_message = _text(record.last_error_message)
        if last_error_message == NA:
            last_error_message = _text(record.error_message)
        try:
            self._connection.execute(
                """
                INSERT INTO telegram_alert_attempts (
                    signal_id, symbol, direction, previous_state, new_state,
                    alert_type, lifecycle_state, sent_at, telegram_status,
                    message_hash, scan_run_id, attempted_alert_type, setup_quality_score,
                    rr_planned, min_rr, opportunity_score, min_score_for_idea,
                    technical_score, price_level, entry_low, entry_high, stop_loss,
                    tp1, tp2, tp3, blocked_reason, error_message,
                    invalid_target_fields, first_seen_at, last_seen_at, seen_count, last_scan_run_id,
                    last_error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _identity(record.signal_id),
                    _symbol(record.symbol),
                    _text(record.direction),
                    _text(record.previous_state),
                    _text(record.new_state),
                    _text(record.alert_type),
                    _text(record.lifecycle_state),
                    _text(record.sent_at),
                    _text(record.telegram_status),
                    _text(record.message_hash),
                    record.scan_run_id,
                    _text(record.attempted_alert_type),
                    _text(record.setup_quality_score),
                    _text(record.rr_planned),
                    _text(record.min_rr),
                    _text(record.opportunity_score),
                    _text(record.min_score_for_idea),
                    _text(record.technical_score),
                    _text(record.price_level),
                    _text(record.entry_low),
                    _text(record.entry_high),
                    _text(record.stop_loss),
                    _text(record.tp1),
                    _text(record.tp2),
                    _text(record.tp3),
                    _text(record.blocked_reason),
                    _text(record.error_message),
                    _text(record.invalid_target_fields),
                    first_seen_at,
                    last_seen_at,
                    int(record.seen_count) if record.seen_count >= 1 else 1,
                    record.last_scan_run_id or record.scan_run_id,
                    last_error_message,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def compact_repeated_attempt(self, record: TelegramAlertAttemptRecord) -> bool:
        status = _text(record.telegram_status)
        if status not in {"blocked", "skipped"}:
            return False
        now = _text(record.last_seen_at if _text(record.last_seen_at) != NA else now_utc_iso())
        cursor = self._connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET
                first_seen_at = CASE
                    WHEN first_seen_at IS NULL OR first_seen_at = 'N/A'
                        THEN COALESCE(NULLIF(sent_at, 'N/A'), ?)
                    ELSE first_seen_at
                END,
                last_seen_at = ?,
                seen_count = CASE
                    WHEN seen_count IS NULL OR seen_count < 1 THEN 2
                    ELSE seen_count + 1
                END,
                last_scan_run_id = ?,
                last_error_message = ?
            WHERE signal_id = ?
              AND alert_type = ?
              AND telegram_status = ?
              AND blocked_reason = ?
              AND error_message = ?
            """,
            (
                now,
                now,
                record.last_scan_run_id,
                _text(record.last_error_message),
                _identity(record.signal_id),
                _text(record.alert_type),
                status,
                _text(record.blocked_reason),
                _text(record.error_message),
            ),
        )
        return cursor.rowcount > 0

    def compact_existing_attempt(
        self,
        *,
        signal_id: str,
        alert_type: TelegramAlertType | str,
        scan_run_id: str | None = None,
    ) -> TelegramAlertAttemptRecord | None:
        existing = self.get_attempt(signal_id=signal_id, alert_type=alert_type)
        if existing is None or existing.telegram_status not in {"blocked", "skipped"}:
            return existing
        now = now_utc_iso()
        record = TelegramAlertAttemptRecord(
            signal_id=existing.signal_id,
            symbol=existing.symbol,
            direction=existing.direction,
            previous_state=existing.previous_state,
            new_state=existing.new_state,
            alert_type=existing.alert_type,
            lifecycle_state=existing.lifecycle_state,
            sent_at=existing.sent_at,
            telegram_status=existing.telegram_status,
            message_hash=existing.message_hash,
            scan_run_id=existing.scan_run_id,
            attempted_alert_type=existing.attempted_alert_type,
            setup_quality_score=existing.setup_quality_score,
            rr_planned=existing.rr_planned,
            min_rr=existing.min_rr,
            opportunity_score=existing.opportunity_score,
            min_score_for_idea=existing.min_score_for_idea,
            technical_score=existing.technical_score,
            price_level=existing.price_level,
            entry_low=existing.entry_low,
            entry_high=existing.entry_high,
            stop_loss=existing.stop_loss,
            tp1=existing.tp1,
            tp2=existing.tp2,
            tp3=existing.tp3,
            blocked_reason=existing.blocked_reason,
            invalid_target_fields=existing.invalid_target_fields,
            error_message=existing.error_message,
            first_seen_at=existing.first_seen_at,
            last_seen_at=now,
            seen_count=existing.seen_count,
            last_scan_run_id=scan_run_id,
            last_error_message=existing.error_message,
            id=existing.id,
        )
        self.compact_repeated_attempt(record)
        return existing

    def list_attempts(self, *, signal_id: str | None = None) -> tuple[TelegramAlertAttemptRecord, ...]:
        params: list[Any] = []
        where = ""
        if signal_id is not None:
            where = "WHERE signal_id = ?"
            params.append(_identity(signal_id))
        rows = self._connection.execute(
            f"""
            SELECT * FROM telegram_alert_attempts
            {where}
            ORDER BY id ASC
            """,
            params,
        ).fetchall()
        return tuple(_record_from_row(row) for row in rows)

    @property
    def _connection(self) -> sqlite3.Connection:
        if self.connection is None:
            raise StorageError("Telegram alert attempt repository is not open.")
        return self.connection


class TelegramLifecycleDeliveryService:
    def __init__(
        self,
        *,
        database_path: Path | str = DEFAULT_DATABASE_PATH,
        settings: Settings | None = None,
        sender: TelegramSender | None = None,
        min_rr: Decimal | str | None = None,
        min_score_for_idea: Decimal | str | None = None,
        min_technical_score: Decimal | str = DEFAULT_MIN_TECHNICAL_SCORE,
    ) -> None:
        self.database_path = Path(database_path)
        self.settings = settings or Settings()
        self.sender = sender or TelegramSender.from_settings(self.settings)
        self.min_rr = _decimal_or_default(min_rr, DEFAULT_CONFIRMED_MIN_RR)
        self.min_score_for_idea = _decimal_or_none(min_score_for_idea)
        self.min_technical_score = _decimal_or_default(min_technical_score, DEFAULT_MIN_TECHNICAL_SCORE)

    @property
    def watchlist_outcome_tracking_enabled(self) -> bool:
        return bool(getattr(self.settings, "telegram_watchlist_outcome_tracking_enabled", True))

    @property
    def public_watchlist_terminal_updates_enabled(self) -> bool:
        return bool(getattr(self.settings, "telegram_public_watchlist_terminal_updates_enabled", False))

    async def deliver_for_run(
        self,
        result: ScannerRunResult,
        *,
        scan_run_id: str | None = None,
    ) -> TelegramLifecycleDeliverySummary:
        deliveries: list[TelegramLifecycleDelivery] = []
        ineligible = 0
        duplicate = 0
        sent = 0
        skipped = 0
        failed = 0
        blocked = 0
        blocked_repeat = 0
        current_run_attempts: set[tuple[str, str]] = set()
        current_run_identity_blocked_symbols: set[str] = set()

        def record_delivery(delivery: TelegramLifecycleDelivery) -> None:
            nonlocal duplicate, sent, skipped, failed, blocked, blocked_repeat
            deliveries.append(delivery)
            if delivery.status in {"sent", "failed", "duplicate", "blocked", "blocked_repeat", "skipped"}:
                current_run_attempts.add((delivery.signal_id, delivery.alert_type))
            if delivery.status in {"blocked", "blocked_repeat"} and delivery.error_message in TERMINAL_IDENTITY_BLOCK_REASONS:
                current_run_identity_blocked_symbols.add(_symbol(delivery.symbol))
            if delivery.status == "duplicate":
                duplicate += 1
            elif delivery.status == "sent":
                sent += 1
            elif delivery.status == "failed":
                failed += 1
            elif delivery.status == "blocked":
                blocked += 1
            elif delivery.status == "blocked_repeat":
                blocked_repeat += 1
            else:
                skipped += 1

        with SQLiteTelegramAlertAttemptRepository(self.database_path) as repository:
            with SQLiteSetupLifecycleRepository(self.database_path) as lifecycle_repository:
                prior_sent_watchlists = repository.list_sent_watchlist_alerts()
                min_score_for_idea = self.min_score_for_idea
                if min_score_for_idea is None:
                    min_score_for_idea = _decimal_or_none(result.config.min_score_for_idea)
                eligibility_context = TelegramEligibilityContext(
                    min_rr=self.min_rr,
                    min_score_for_idea=min_score_for_idea,
                    min_technical_score=self.min_technical_score,
                )
                for symbol_result in result.results:
                    delivery = await self.deliver_for_symbol(
                        symbol_result,
                        repository=repository,
                        scan_run_id=scan_run_id,
                        eligibility_context=eligibility_context,
                    )
                    if delivery is None:
                        ineligible += 1
                        continue
                    record_delivery(delivery)
                for delivery in await self.reconcile_sent_watchlists(
                    repository=repository,
                    lifecycle_repository=lifecycle_repository,
                    sent_watchlists=prior_sent_watchlists,
                    current_results=result.results,
                    scan_run_id=scan_run_id,
                    eligibility_context=eligibility_context,
                    current_run_attempts=frozenset(current_run_attempts),
                    current_run_identity_blocked_symbols=frozenset(current_run_identity_blocked_symbols),
                ):
                    record_delivery(delivery)

        return TelegramLifecycleDeliverySummary(
            attempted=sent + skipped + failed + blocked + blocked_repeat,
            sent=sent,
            skipped=skipped,
            failed=failed,
            blocked=blocked,
            blocked_repeat=blocked_repeat,
            duplicate=duplicate,
            ineligible=ineligible,
            deliveries=tuple(deliveries),
        )

    async def deliver_for_symbol(
        self,
        symbol_result: ScannerSymbolResult,
        *,
        repository: SQLiteTelegramAlertAttemptRepository,
        scan_run_id: str | None = None,
        eligibility_context: TelegramEligibilityContext | None = None,
    ) -> TelegramLifecycleDelivery | None:
        alert_type_hint = (
            _alert_type_for_transition(symbol_result, symbol_result.lifecycle_transition)
            if symbol_result.lifecycle_transition
            else None
        )
        terminal_bridge = (
            _terminal_identity_bridge(repository, symbol_result, alert_type_hint)
            if alert_type_hint in TERMINAL_UPDATE_ALERT_TYPES
            else _terminal_identity_bridge(
                repository,
                symbol_result,
                alert_type_hint,
                watchlist_only=True,
            )
            if alert_type_hint == TelegramAlertType.SIGNAL_CONFIRMED
            else TerminalIdentityBridge()
        )
        prior_active_alert = terminal_bridge.prior_alert
        previously_active_sent = (
            prior_active_alert is not None
            if alert_type_hint in TERMINAL_UPDATE_ALERT_TYPES
            else repository.has_prior_active_alert(signal_id=_signal_id(symbol_result))
        )
        decision = telegram_alert_decision_for_symbol(
            symbol_result,
            previously_active_sent=previously_active_sent,
            eligibility_context=eligibility_context,
            terminal_identity_failure_reason=terminal_bridge.blocked_reason,
            prior_public_alert=prior_active_alert,
        )
        if not decision.eligible or decision.alert_type is None or decision.message is None:
            if decision.alert_type is not None and decision.message is not None and _persist_blocked_decision(decision):
                return _persist_blocked_attempt(
                    repository,
                    symbol_result,
                    decision=decision,
                    scan_run_id=scan_run_id,
                    eligibility_context=eligibility_context or TelegramEligibilityContext(),
                )
            return None

        decision = _apply_soft_failed_confirmation_grace_to_decision(
            repository,
            symbol_result,
            decision=decision,
            prior_alert=prior_active_alert,
            scan_run_id=scan_run_id,
            eligibility_context=eligibility_context or TelegramEligibilityContext(),
        )
        if decision is None:
            return None
        if (
            decision.alert_type in TERMINAL_UPDATE_ALERT_TYPES
            and prior_active_alert is not None
            and prior_active_alert.alert_type == TelegramAlertType.WATCHLIST.value
            and not self.public_watchlist_terminal_updates_enabled
        ):
            return _persist_suppressed_watchlist_terminal_update(
                repository,
                prior_alert=prior_active_alert,
                symbol_result=symbol_result,
                alert_type=decision.alert_type,
                message=decision.message,
                scan_run_id=scan_run_id,
                eligibility_context=eligibility_context or TelegramEligibilityContext(),
            )

        signal_id = _signal_id(symbol_result)
        message = decision.message
        if decision.alert_type in TERMINAL_UPDATE_ALERT_TYPES and prior_active_alert is not None:
            signal_id = prior_active_alert.signal_id
            message = _message_with_prior_public_identity(message, prior_active_alert)
            if repository.has_sent_terminal_outcome(signal_id=signal_id) and not repository.has_attempt(
                signal_id=signal_id,
                alert_type=decision.alert_type,
            ):
                return TelegramLifecycleDelivery(
                    symbol=symbol_result.symbol,
                    signal_id=signal_id,
                    alert_type=decision.alert_type.value,
                    status="duplicate",
                    detail="Prior terminal Telegram lifecycle update already sent.",
                )
        elif decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED and prior_active_alert is not None:
            signal_id = prior_active_alert.signal_id
            message = _message_with_prior_public_identity(message, prior_active_alert)

        if repository.has_attempt(signal_id=signal_id, alert_type=decision.alert_type):
            repository.compact_existing_attempt(
                signal_id=signal_id,
                alert_type=decision.alert_type,
                scan_run_id=scan_run_id,
            )
            return TelegramLifecycleDelivery(
                symbol=symbol_result.symbol,
                signal_id=signal_id,
                alert_type=decision.alert_type.value,
                status="duplicate",
                detail="Duplicate Telegram alert prevented.",
            )

        message_text = format_telegram_signal_message(decision.alert_type, message)
        message_hash = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
        send_result = await self.sender.send_text(message_text)
        transition = decision.lifecycle_transition
        previous_state = transition.from_state.value if transition and transition.from_state else NA
        new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
        record = TelegramAlertAttemptRecord(
            signal_id=signal_id,
            symbol=symbol_result.symbol,
            direction=message.direction,
            previous_state=previous_state,
            new_state=new_state,
            alert_type=decision.alert_type.value,
            lifecycle_state=_lifecycle_state_text(symbol_result),
            sent_at=now_utc_iso(),
            telegram_status=send_result.status,
            message_hash=message_hash,
            scan_run_id=scan_run_id or _transition_scan_run_id(transition),
            attempted_alert_type=decision.alert_type.value,
            setup_quality_score=_quality_score(symbol_result),
            rr_planned=_text(message.planned_rr),
            min_rr=_text((eligibility_context or TelegramEligibilityContext()).min_rr),
            opportunity_score=_opportunity_score_text(symbol_result),
            min_score_for_idea=_text((eligibility_context or TelegramEligibilityContext()).min_score_for_idea),
            technical_score=_technical_score_text(symbol_result),
            price_level=_price_level_for_alert(decision.alert_type, message),
            **_message_level_metadata(message),
            blocked_reason=NA,
            invalid_target_fields=NA,
            error_message=send_result.error_message,
        )
        inserted = repository.insert_attempt(record)
        if not inserted:
            return TelegramLifecycleDelivery(
                symbol=symbol_result.symbol,
                signal_id=signal_id,
                alert_type=decision.alert_type.value,
                status="duplicate",
                detail="Duplicate Telegram alert prevented.",
                message_hash=message_hash,
            )

        logger.info(
            "Telegram signal alert attempt persisted: symbol=%s alert_type=%s status=%s message_hash=%s",
            symbol_result.symbol,
            decision.alert_type.value,
            send_result.status,
            message_hash,
        )
        return TelegramLifecycleDelivery(
            symbol=symbol_result.symbol,
            signal_id=signal_id,
            alert_type=decision.alert_type.value,
            status=send_result.status,
            detail=send_result.detail,
            message_hash=message_hash,
            error_message=send_result.error_message,
        )

    async def reconcile_sent_watchlists(
        self,
        *,
        repository: SQLiteTelegramAlertAttemptRepository,
        lifecycle_repository: SQLiteSetupLifecycleRepository,
        sent_watchlists: Sequence[TelegramAlertAttemptRecord],
        current_results: Sequence[ScannerSymbolResult],
        scan_run_id: str | None,
        eligibility_context: TelegramEligibilityContext,
        current_run_attempts: frozenset[tuple[str, str]],
        current_run_identity_blocked_symbols: frozenset[str],
    ) -> tuple[TelegramLifecycleDelivery, ...]:
        deliveries: list[TelegramLifecycleDelivery] = []
        snapshot_watchlists_by_symbol = _sent_watchlist_snapshot_by_symbol(sent_watchlists)
        for prior_alert in sent_watchlists:
            if prior_alert.alert_type != TelegramAlertType.WATCHLIST.value or prior_alert.telegram_status != "sent":
                continue
            if _symbol(prior_alert.symbol) in current_run_identity_blocked_symbols:
                continue
            if repository.has_sent_terminal_outcome(signal_id=prior_alert.signal_id):
                continue
            if _has_current_run_soft_failed_confirmation_observation(
                repository,
                signal_id=prior_alert.signal_id,
                scan_run_id=scan_run_id,
            ):
                continue
            if (prior_alert.signal_id, TelegramAlertType.WATCHLIST.value) in current_run_attempts:
                continue

            current_result_for_prior = _current_result_for_prior_watchlist(prior_alert, current_results)
            if self.watchlist_outcome_tracking_enabled and current_result_for_prior is not None:
                outcome_delivery = await self._send_watchlist_outcome_update(
                    repository,
                    prior_alert=prior_alert,
                    current_result=current_result_for_prior,
                    scan_run_id=scan_run_id,
                    eligibility_context=eligibility_context,
                )
                if outcome_delivery is not None:
                    deliveries.append(outcome_delivery)
                    if outcome_delivery.alert_type in {
                        alert_type.value for alert_type in WATCHLIST_OUTCOME_TERMINAL_ALERT_TYPES
                    }:
                        continue

            match = _match_sent_watchlist_lifecycle(
                prior_alert,
                lifecycle_repository=lifecycle_repository,
                current_results=current_results,
                snapshot_watchlists_by_symbol=snapshot_watchlists_by_symbol,
            )
            if match.blocked_reason is not None:
                deliveries.append(
                    _persist_sent_watchlist_reconciliation_block(
                        repository,
                        prior_alert,
                        reason=match.blocked_reason,
                        scan_run_id=scan_run_id,
                    )
                )
                continue
            if match.record is None:
                continue

            current_result = _current_result_for_lifecycle_record(match.record, prior_alert, current_results)
            if current_result is None:
                current_result = current_result_for_prior
            outcome = _sent_watchlist_reconciliation_outcome(
                prior_alert,
                match.record,
                current_result=current_result,
                eligibility_context=eligibility_context,
            )
            if outcome is None:
                continue

            outcome = _apply_soft_failed_confirmation_grace_to_reconciliation(
                repository,
                prior_alert=prior_alert,
                outcome=outcome,
                scan_run_id=scan_run_id,
                eligibility_context=eligibility_context,
            )
            if outcome is None:
                continue
            if (
                outcome.alert_type in TERMINAL_UPDATE_ALERT_TYPES
                and prior_alert.alert_type == TelegramAlertType.WATCHLIST.value
                and not self.public_watchlist_terminal_updates_enabled
            ):
                deliveries.append(
                    _persist_suppressed_watchlist_terminal_update(
                        repository,
                        prior_alert=prior_alert,
                        symbol_result=outcome.symbol_result,
                        alert_type=outcome.alert_type,
                        message=outcome.message,
                        scan_run_id=scan_run_id,
                        eligibility_context=eligibility_context,
                    )
                )
                continue

            signal_id = prior_alert.signal_id
            alert_type_text = outcome.alert_type.value
            if (signal_id, alert_type_text) in current_run_attempts and repository.has_attempt(
                signal_id=signal_id,
                alert_type=outcome.alert_type,
            ):
                continue
            if outcome.alert_type in TERMINAL_UPDATE_ALERT_TYPES and repository.has_sent_terminal_outcome(
                signal_id=signal_id
            ) and not repository.has_attempt(signal_id=signal_id, alert_type=outcome.alert_type):
                deliveries.append(
                    TelegramLifecycleDelivery(
                        symbol=prior_alert.symbol,
                        signal_id=signal_id,
                        alert_type=alert_type_text,
                        status="duplicate",
                        detail="Prior terminal Telegram lifecycle update already sent.",
                    )
                )
                continue
            if repository.has_attempt(signal_id=signal_id, alert_type=outcome.alert_type):
                repository.compact_existing_attempt(
                    signal_id=signal_id,
                    alert_type=outcome.alert_type,
                    scan_run_id=scan_run_id,
                )
                deliveries.append(
                    TelegramLifecycleDelivery(
                        symbol=prior_alert.symbol,
                        signal_id=signal_id,
                        alert_type=alert_type_text,
                        status="duplicate",
                        detail="Duplicate Telegram alert prevented.",
                    )
                )
                continue

            deliveries.append(
                await self._send_reconciliation_update(
                    repository,
                    prior_alert=prior_alert,
                    outcome=outcome,
                    scan_run_id=scan_run_id,
                    eligibility_context=eligibility_context,
                )
            )
        return tuple(deliveries)

    async def _send_watchlist_outcome_update(
        self,
        repository: SQLiteTelegramAlertAttemptRepository,
        *,
        prior_alert: TelegramAlertAttemptRecord,
        current_result: ScannerSymbolResult,
        scan_run_id: str | None,
        eligibility_context: TelegramEligibilityContext,
    ) -> TelegramLifecycleDelivery | None:
        outcome = _watchlist_outcome_for_current_result(
            repository,
            prior_alert=prior_alert,
            current_result=current_result,
            eligibility_context=eligibility_context,
            scan_run_id=scan_run_id,
        )
        if outcome is None:
            return None
        if isinstance(outcome, TelegramLifecycleDelivery):
            return outcome

        alert_type, message = outcome
        if repository.has_attempt(signal_id=prior_alert.signal_id, alert_type=alert_type):
            repository.compact_existing_attempt(
                signal_id=prior_alert.signal_id,
                alert_type=alert_type,
                scan_run_id=scan_run_id,
            )
            return TelegramLifecycleDelivery(
                symbol=prior_alert.symbol,
                signal_id=prior_alert.signal_id,
                alert_type=alert_type.value,
                status="duplicate",
                detail="Duplicate Telegram watchlist outcome update prevented.",
            )

        message_text = format_telegram_signal_message(alert_type, message)
        message_hash = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
        send_result = await self.sender.send_text(message_text)
        transition = current_result.lifecycle_transition
        previous_state = transition.from_state.value if transition and transition.from_state else prior_alert.new_state
        new_state = transition.to_state.value if transition else _lifecycle_state_text(current_result)
        record = TelegramAlertAttemptRecord(
            signal_id=prior_alert.signal_id,
            symbol=_symbol(message.symbol),
            direction=_text(message.direction),
            previous_state=previous_state,
            new_state=new_state,
            alert_type=alert_type.value,
            lifecycle_state=_lifecycle_state_text(current_result),
            sent_at=now_utc_iso(),
            telegram_status=send_result.status,
            message_hash=message_hash,
            scan_run_id=scan_run_id or _transition_scan_run_id(transition),
            attempted_alert_type=alert_type.value,
            setup_quality_score=_quality_score(current_result),
            rr_planned=_text(message.planned_rr),
            min_rr=_text(eligibility_context.min_rr),
            opportunity_score=_opportunity_score_text(current_result),
            min_score_for_idea=_text(eligibility_context.min_score_for_idea),
            technical_score=_technical_score_text(current_result),
            price_level=_price_level_for_alert(alert_type, message),
            **_message_level_metadata(message),
            blocked_reason=NA,
            invalid_target_fields=NA,
            error_message=send_result.error_message,
        )
        inserted = repository.insert_attempt(record)
        if not inserted:
            return TelegramLifecycleDelivery(
                symbol=prior_alert.symbol,
                signal_id=prior_alert.signal_id,
                alert_type=alert_type.value,
                status="duplicate",
                detail="Duplicate Telegram watchlist outcome update prevented.",
                message_hash=message_hash,
            )
        logger.info(
            "Telegram watchlist outcome persisted: symbol=%s alert_type=%s status=%s message_hash=%s",
            prior_alert.symbol,
            alert_type.value,
            send_result.status,
            message_hash,
        )
        return TelegramLifecycleDelivery(
            symbol=prior_alert.symbol,
            signal_id=prior_alert.signal_id,
            alert_type=alert_type.value,
            status=send_result.status,
            detail=send_result.detail,
            message_hash=message_hash,
            error_message=send_result.error_message,
        )

    async def _send_reconciliation_update(
        self,
        repository: SQLiteTelegramAlertAttemptRepository,
        *,
        prior_alert: TelegramAlertAttemptRecord,
        outcome: SentWatchlistReconciliationOutcome,
        scan_run_id: str | None,
        eligibility_context: TelegramEligibilityContext,
    ) -> TelegramLifecycleDelivery:
        message_text = format_telegram_signal_message(outcome.alert_type, outcome.message)
        message_hash = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
        send_result = await self.sender.send_text(message_text)
        transition = outcome.symbol_result.lifecycle_transition
        previous_state = transition.from_state.value if transition and transition.from_state else NA
        new_state = transition.to_state.value if transition else _lifecycle_state_text(outcome.symbol_result)
        record = TelegramAlertAttemptRecord(
            signal_id=prior_alert.signal_id,
            symbol=_symbol(outcome.message.symbol),
            direction=_text(outcome.message.direction),
            previous_state=previous_state,
            new_state=new_state,
            alert_type=outcome.alert_type.value,
            lifecycle_state=_lifecycle_state_text(outcome.symbol_result),
            sent_at=now_utc_iso(),
            telegram_status=send_result.status,
            message_hash=message_hash,
            scan_run_id=scan_run_id or _transition_scan_run_id(transition),
            attempted_alert_type=outcome.alert_type.value,
            setup_quality_score=_quality_score(outcome.symbol_result),
            rr_planned=_text(outcome.message.planned_rr),
            min_rr=_text(eligibility_context.min_rr),
            opportunity_score=_opportunity_score_text(outcome.symbol_result),
            min_score_for_idea=_text(eligibility_context.min_score_for_idea),
            technical_score=_technical_score_text(outcome.symbol_result),
            price_level=_price_level_for_alert(outcome.alert_type, outcome.message),
            **_message_level_metadata(outcome.message),
            blocked_reason=NA,
            invalid_target_fields=NA,
            error_message=send_result.error_message,
        )
        inserted = repository.insert_attempt(record)
        if not inserted:
            return TelegramLifecycleDelivery(
                symbol=prior_alert.symbol,
                signal_id=prior_alert.signal_id,
                alert_type=outcome.alert_type.value,
                status="duplicate",
                detail="Duplicate Telegram alert prevented.",
                message_hash=message_hash,
            )
        logger.info(
            "Telegram sent-watchlist reconciliation persisted: symbol=%s alert_type=%s status=%s message_hash=%s",
            prior_alert.symbol,
            outcome.alert_type.value,
            send_result.status,
            message_hash,
        )
        return TelegramLifecycleDelivery(
            symbol=prior_alert.symbol,
            signal_id=prior_alert.signal_id,
            alert_type=outcome.alert_type.value,
            status=send_result.status,
            detail=send_result.detail,
            message_hash=message_hash,
            error_message=send_result.error_message,
        )


def telegram_alert_decision_for_symbol(
    symbol_result: ScannerSymbolResult,
    *,
    previously_active_sent: bool = False,
    eligibility_context: TelegramEligibilityContext | None = None,
    terminal_identity_failure_reason: str | None = None,
    prior_public_alert: TelegramAlertAttemptRecord | None = None,
) -> TelegramAlertDecision:
    transition = symbol_result.lifecycle_transition
    if transition is None:
        return TelegramAlertDecision(False, "missing_lifecycle_transition")
    if not transition.transitioned:
        return TelegramAlertDecision(False, "unchanged_lifecycle_state", lifecycle_transition=transition)
    alert_type = _alert_type_for_transition(symbol_result, transition)
    if alert_type is None:
        return TelegramAlertDecision(False, "lifecycle_state_not_eligible", lifecycle_transition=transition)

    context = eligibility_context or TelegramEligibilityContext()
    message = _telegram_signal_message_for_alert(symbol_result, alert_type, context)
    if alert_type in TERMINAL_UPDATE_ALERT_TYPES and prior_public_alert is not None:
        message = _message_with_prior_public_identity(message, prior_public_alert)
    if _requires_prior_active_alert(alert_type) and not previously_active_sent:
        if alert_type in TERMINAL_UPDATE_ALERT_TYPES:
            reason = terminal_identity_failure_reason or "terminal_update_no_prior_public_alert"
        else:
            reason = "missing_prior_active_telegram_alert"
        return TelegramAlertDecision(
            False,
            reason,
            alert_type=alert_type if alert_type in TERMINAL_UPDATE_ALERT_TYPES else None,
            message=message if alert_type in TERMINAL_UPDATE_ALERT_TYPES else None,
            lifecycle_transition=transition,
        )

    blockers = _defensive_delivery_blockers(symbol_result, alert_type, message, context)
    if blockers:
        failed_confirmation = _failed_confirmation_terminal_decision(
            symbol_result,
            alert_type=alert_type,
            message=message,
            blockers=blockers,
            lifecycle_transition=transition,
            prior_public_alert=prior_public_alert,
            terminal_identity_failure_reason=terminal_identity_failure_reason,
        )
        if failed_confirmation is not None:
            return failed_confirmation
        return TelegramAlertDecision(
            False,
            "blocked:" + "; ".join(blockers),
            alert_type=alert_type,
            message=message,
            lifecycle_transition=transition,
        )
    missing = _missing_required_fields(alert_type, message)
    if missing:
        return TelegramAlertDecision(
            False,
            f"missing_required_fields:{','.join(missing)}",
            alert_type=alert_type,
            message=message,
            lifecycle_transition=transition,
        )
    return TelegramAlertDecision(True, "eligible", alert_type=alert_type, message=message, lifecycle_transition=transition)


def telegram_signal_message_from_symbol(symbol_result: ScannerSymbolResult) -> TelegramSignalMessage:
    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    lifecycle = symbol_result.lifecycle_state
    direction = _first_non_na(
        getattr(lifecycle, "direction", NA),
        getattr(trade_idea, "direction", NA) if trade_idea is not None else NA,
        _field(setup, "bias"),
        diagnostics.get("bias"),
        diagnostics.get("direction"),
    )
    return TelegramSignalMessage(
        symbol=symbol_result.symbol,
        direction=direction,
        signal_id=_signal_id(symbol_result),
        watch_zone=_watch_zone_text(symbol_result, diagnostics),
        entry_low=_first_non_na(
            _field(setup, "entry_low"),
            diagnostics.get("entry_low"),
            _level_field(getattr(trade_idea, "entry_zone", None), "low"),
            _level_field(getattr(trade_idea, "entry_zone", None), "price"),
            diagnostics.get("entry"),
        ),
        entry_high=_first_non_na(
            _field(setup, "entry_high"),
            diagnostics.get("entry_high"),
            _level_field(getattr(trade_idea, "entry_zone", None), "high"),
            _level_field(getattr(trade_idea, "entry_zone", None), "price"),
            diagnostics.get("entry"),
        ),
        stop_loss=_first_non_na(
            _field(setup, "stop"),
            diagnostics.get("stop"),
            _level_field(getattr(trade_idea, "stop_loss", None), "price"),
            diagnostics.get("stop_loss"),
        ),
        tp1=_first_non_na(_field(setup, "tp1"), diagnostics.get("tp1"), _take_profit(trade_idea, 1)),
        tp2=_first_non_na(_field(setup, "tp2"), diagnostics.get("tp2"), _take_profit(trade_idea, 2)),
        tp3=_first_non_na(_field(setup, "tp3"), diagnostics.get("tp3"), _take_profit(trade_idea, 3)),
        planned_rr=_first_non_na(
            _field(setup, "rr_to_tp2"),
            diagnostics.get("rr_to_tp2"),
            getattr(trade_idea, "best_rr", NA) if trade_idea is not None else NA,
        ),
        current_context=_watchlist_context_sentence(symbol_result, diagnostics),
        needs_next=_watchlist_needs_next(symbol_result, diagnostics),
        structure_reason=_first_non_na(
            getattr(trade_idea, "reason_for_trade", NA) if trade_idea is not None else NA,
            diagnostics.get("structure_reason"),
            diagnostics.get("pullback_failure_reason"),
            diagnostics.get("confirmation_bos_choch_reason"),
            symbol_result.rejection_reason,
        ),
        confirmation_needed=_first_non_na(
            diagnostics.get("confirmation_needed"),
            diagnostics.get("next_trigger_needed"),
            _confirmation_needed(diagnostics),
        ),
        invalidation_reason=_public_invalidation_sentence(
            direction=direction,
            stop_loss=_first_non_na(
                _field(setup, "stop"),
                diagnostics.get("stop"),
                _level_field(getattr(trade_idea, "stop_loss", None), "price"),
                diagnostics.get("stop_loss"),
            ),
            entry_low=_first_non_na(
                _field(setup, "entry_low"),
                diagnostics.get("entry_low"),
                _level_field(getattr(trade_idea, "entry_zone", None), "low"),
                _level_field(getattr(trade_idea, "entry_zone", None), "price"),
                diagnostics.get("entry"),
            ),
            entry_high=_first_non_na(
                _field(setup, "entry_high"),
                diagnostics.get("entry_high"),
                _level_field(getattr(trade_idea, "entry_zone", None), "high"),
                _level_field(getattr(trade_idea, "entry_zone", None), "price"),
                diagnostics.get("entry"),
            ),
            raw_invalidation=_first_non_na(
                getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA,
                _field(setup, "invalidation"),
                diagnostics.get("invalidation"),
                _near_miss_invalidation_hint(symbol_result, diagnostics),
                getattr(lifecycle, "invalidation_reason", NA),
            ),
        ),
        watchlist_invalidation_reason=_watchlist_invalidation_sentence(
            symbol_result,
            diagnostics,
            direction=direction,
            stop_loss=_first_non_na(
                _field(setup, "stop"),
                diagnostics.get("stop"),
                _level_field(getattr(trade_idea, "stop_loss", None), "price"),
                diagnostics.get("stop_loss"),
            ),
        ),
        confluence=_human_confluence_sentence(symbol_result, diagnostics),
        htf_bias=_first_non_na(diagnostics.get("htf_2d_trend"), diagnostics.get("mtf_12h_trend")),
        ob_fvg_status=_first_non_na(
            diagnostics.get("selected_zone_type"),
            diagnostics.get("ob_fvg_status"),
            diagnostics.get("ob_fvg_diagnostics"),
        ),
        volume_status=_first_non_na(
            diagnostics.get("volume_profile_source"),
            symbol_result.volume_profile_source,
        ),
        derivatives_status=_derivatives_status(symbol_result, diagnostics),
        price_level=_first_non_na(
            diagnostics.get("price_level"),
            diagnostics.get("exit_price"),
            symbol_result.current_price,
            symbol_result.latest_close,
        ),
    )


def _telegram_signal_message_for_alert(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    context: TelegramEligibilityContext,
) -> TelegramSignalMessage:
    message = replace(telegram_signal_message_from_symbol(symbol_result), min_rr=context.min_rr)
    if alert_type in TERMINAL_UPDATE_ALERT_TYPES:
        return replace(message, invalidation_reason=_terminal_update_reason(symbol_result, alert_type))
    return message


def _watch_zone_text(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    return _first_non_na(
        diagnostics.get("watch_zone"),
        diagnostics.get("entry_zone"),
        diagnostics.get("pullback_zone"),
        diagnostics.get("preferred_fib_pullback_zone"),
        diagnostics.get("fib_pullback_zone"),
    )


def _watchlist_context_sentence(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    failed_gate = _watch_failed_gate(symbol_result, diagnostics)
    reason = _watch_reason(symbol_result, diagnostics)
    structure = _watch_structure_context(symbol_result, diagnostics)
    planned_rr = _decimal_or_none(_first_non_na(diagnostics.get("rr_to_tp2"), diagnostics.get("planned_rr")))
    rr_missing = planned_rr is None
    rr_below_default = planned_rr is not None and planned_rr < DEFAULT_CONFIRMED_MIN_RR

    if failed_gate in WATCHLIST_STALE_OR_INCOMPLETE_GATES:
        sentence = (
            "The earlier structure is no longer clean enough for confirmation. "
            "A fresh liquidity sweep and new BOS/CHoCH are required before this candidate can become valid."
        )
    elif failed_gate in WATCHLIST_CONFIRMATION_GATES:
        sentence = (
            "Price has swept liquidity, but a fresh LTF BOS/CHoCH confirmation is still required "
            "before this candidate can become valid."
        )
    elif failed_gate in WATCHLIST_OB_FVG_GATES:
        sentence = (
            f"{structure}, but the setup is still waiting for a valid OB/FVG pullback zone "
            "before it can become a confirmed signal."
        )
    elif "fib" in failed_gate:
        sentence = f"{structure}, but fib alignment still needs confirmation before the setup can activate."
    elif failed_gate in WATCHLIST_RR_GATES or rr_below_default:
        sentence = f"{structure}, but final RR still needs validation before the setup can activate."
    elif _watchlist_has_public_plan_levels(diagnostics):
        sentence = (
            "Price has produced a trackable pullback map, but the setup remains watchlist-only until "
            "final RR, structure, and quality gates confirm."
        )
    elif reason != NA:
        sentence = f"{structure}, but the setup is not confirmed yet. {_clean_watch_text(reason)}"
    else:
        sentence = f"{structure}, but the setup is not confirmed yet."

    if not sentence.endswith((".", "!", "?")):
        sentence = f"{sentence}."
    if rr_missing and "RR still needs validation" not in sentence:
        sentence = f"{sentence} Final RR still needs validation after the entry zone forms."
    elif rr_below_default and "RR" not in sentence:
        sentence = f"{sentence} Final RR must improve to the configured minimum before confirmation."
    return sentence


def _watchlist_needs_next(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> tuple[str, ...]:
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    failed_gate = _watch_failed_gate(symbol_result, diagnostics)
    direction = _first_non_na(
        diagnostics.get("bias"),
        diagnostics.get("direction"),
        getattr(symbol_result.lifecycle_state, "direction", NA) if symbol_result.lifecycle_state is not None else NA,
    )
    if failed_gate in WATCHLIST_STALE_OR_INCOMPLETE_GATES:
        return (
            "A fresh BOS/CHoCH is needed if the current structure becomes stale.",
            "Pullback must remain inside the planned zone.",
            _directional_invalidation_condition(direction),
        )
    if failed_gate in WATCHLIST_CONFIRMATION_GATES:
        return (
            "Price must trade into the Limit Zone.",
            "Structure must continue to respect the sweep/BOS/CHoCH context.",
            _directional_invalidation_condition(direction),
        )
    if failed_gate in WATCHLIST_OB_FVG_GATES:
        return (
            "A valid OB/FVG zone must form inside the displacement impulse.",
            "Price must react from the planned Limit Zone.",
            _directional_invalidation_condition(direction),
        )

    candidates = (
        _field(intelligence, "next_required_conditions"),
        diagnostics.get("needs_next"),
        diagnostics.get("next_required_conditions"),
        diagnostics.get("next_conditions"),
        diagnostics.get("next_condition"),
        diagnostics.get("next_trigger_needed"),
    )
    lines: list[str] = []
    for candidate in candidates:
        for value in _sequence_or_single(candidate):
            text = _clean_watch_text(value)
            if text != NA and _chart_only_watch_condition(text) and text not in lines:
                lines.append(text)
            if len(lines) == 3:
                return tuple(lines)
    if lines:
        while len(lines) < 3:
            fallback = _fallback_watchlist_needs_next(direction)[len(lines)]
            if fallback not in lines:
                lines.append(fallback)
            else:
                break
        return tuple(lines[:3])
    return _fallback_watchlist_needs_next(direction)


def _fallback_watchlist_needs_next(direction: Any) -> tuple[str, str, str]:
    side = _status_key(direction)
    if side == "long":
        return (
            "Price must trade into the Limit Zone.",
            "Limit Zone must hold as support after the pullback.",
            "Bullish structure must remain valid above the invalidation level.",
        )
    if side == "short":
        return (
            "Price must trade into the Limit Zone.",
            "Limit Zone must hold as resistance after the pullback.",
            "Bearish structure must remain valid below the invalidation level.",
        )
    return (
        "Price must interact with the Limit Zone.",
        "Structure must remain valid.",
        "Invalidation level must hold.",
    )


def _directional_invalidation_condition(direction: Any) -> str:
    side = _status_key(direction)
    if side == "long":
        return "Price must not accept beyond the invalidation level."
    if side == "short":
        return "Price must not accept beyond the invalidation level."
    return "Invalidation level must hold."


def _chart_only_watch_condition(value: Any) -> bool:
    text = _text(value).lower()
    if text == NA.lower():
        return False
    tokens = text.replace("/", " ").replace("-", " ").replace(".", " ").replace(",", " ").split()
    forbidden = (
        "trust meter",
        "risk/reward",
        "risk reward",
        "score",
        "scoring",
        "opportunity score",
        "quality score",
        "final confluence threshold",
        "scanner threshold",
        "grade",
        "hard rejection",
        "required threshold",
        "quality gate",
        "final quality",
        "core engine",
    )
    return "rr" not in tokens and not any(fragment in text for fragment in forbidden)


def _watchlist_has_public_plan_levels(diagnostics: Mapping[str, Any]) -> bool:
    return (
        _numeric_pair_values(diagnostics.get("entry_low"), diagnostics.get("entry_high"))
        or _decimal_or_none(diagnostics.get("stop")) is not None
        or _decimal_or_none(diagnostics.get("stop_loss")) is not None
    )


def _append_rr_requirement(lines: Sequence[str], diagnostics: Mapping[str, Any]) -> tuple[str, ...]:
    output = [line for line in lines if _text(line) != NA]
    planned_rr = _decimal_or_none(_first_non_na(diagnostics.get("rr_to_tp2"), diagnostics.get("planned_rr")))
    if planned_rr is None or planned_rr >= DEFAULT_CONFIRMED_MIN_RR:
        return tuple(output[:3])
    requirement = "Final RR must improve to at least the configured minimum before confirmation."
    if any("rr" in line.lower() for line in output):
        return tuple(output[:3])
    if len(output) >= 3:
        output[2] = requirement
    else:
        output.append(requirement)
    return tuple(output[:3])


def _watchlist_invalidation_sentence(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    *,
    direction: Any,
    stop_loss: Any,
) -> str:
    stop = format_telegram_price(stop_loss)
    side = _status_key(direction)
    if stop != NA:
        side_word = "below" if side == "long" else "above" if side == "short" else "through"
        return (
            f"Watchlist invalidates if price accepts {side_word} {stop} or the sweep/BOS/CHoCH "
            "structure fails before a valid OB/FVG pullback forms."
        )

    raw = _clean_public_sentence(
        _first_non_na(
            _near_miss_invalidation_hint(symbol_result, diagnostics),
            diagnostics.get("watchlist_invalidation"),
            diagnostics.get("invalidation_hint"),
            getattr(symbol_result.lifecycle_state, "invalidation_reason", NA),
        )
    )
    if raw != NA:
        return _as_watchlist_invalidation(raw)
    return (
        "Watchlist invalidates if the sweep/BOS/CHoCH context fails, expires, or price breaks "
        "the structure that created the watchlist candidate."
    )


def _as_watchlist_invalidation(value: Any) -> str:
    text = _clean_watch_text(value)
    if text == NA:
        return NA
    key = text.lower()
    if key.startswith("watchlist invalidates"):
        return text
    if key.startswith("invalidated if"):
        return "Watchlist invalidates if" + text[len("Invalidated if") :]
    if key.startswith("invalidates if"):
        return "Watchlist invalidates if" + text[len("Invalidates if") :]
    if key.startswith("invalid if"):
        return "Watchlist invalidates if" + text[len("Invalid if") :]
    if key.startswith("signal invalidates if"):
        return "Watchlist invalidates if" + text[len("Signal invalidates if") :]
    return text


def _watch_structure_context(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    gates_passed = {_status_key(value) for value in _sequence_or_single(diagnostics.get("gates_passed"))}
    sweep = (
        bool(symbol_result.sweep_detected)
        or _status_key(diagnostics.get("execution_sweep_status")) == "passed"
        or "sweep" in gates_passed
    )
    confirmation = (
        bool(symbol_result.bos_detected or symbol_result.choch_detected)
        or _status_key(diagnostics.get("confirmation_structure_shift_status")) == "passed"
        or "bos_choch" in gates_passed
    )
    if sweep and confirmation:
        return "Price has swept liquidity and confirmed a LTF BOS/CHoCH"
    if sweep:
        return "Price has produced a sweep, while LTF BOS/CHoCH still needs confirmation"
    if confirmation:
        return "LTF structure has shifted, while the sweep context still needs confirmation"
    return "Core structure is still developing"


def _watch_failed_gate(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    return _status_key(
        _first_non_na(
            _field(intelligence, "primary_failed_gate"),
            diagnostics.get("first_failed_gate"),
            getattr(symbol_result.lifecycle_state, "failed_gate", NA),
            symbol_result.rejection_stage,
        )
    )


def _watch_reason(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    return _first_non_na(
        _field(intelligence, "short_reason"),
        diagnostics.get("pullback_failure_reason"),
        diagnostics.get("ob_fvg_diagnostics"),
        diagnostics.get("fib_diagnostics"),
        diagnostics.get("rr_diagnostics"),
        symbol_result.rejection_reason,
    )


def _near_miss_invalidation_hint(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    return _first_non_na(_field(intelligence, "invalidation_hint"), diagnostics.get("invalidation_hint"))


def _near_miss_intelligence(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> Any:
    intelligence = symbol_result.near_miss_intelligence
    if intelligence is not None:
        return intelligence
    payload = diagnostics.get("near_miss_intelligence")
    return payload if isinstance(payload, Mapping) else None


def _alert_type_for_transition(
    symbol_result: ScannerSymbolResult,
    transition: SetupTransitionResult,
) -> TelegramAlertType | None:
    state = transition.to_state
    if state in WATCH_ALERT_STATES:
        return TelegramAlertType.WATCHLIST
    if state in SIGNAL_ALERT_STATES:
        if _explicit_watchlist_candidate(symbol_result):
            return TelegramAlertType.WATCHLIST
        return TelegramAlertType.SIGNAL_CONFIRMED
    if state == SetupLifecycleState.MANAGING:
        return TelegramAlertType.LIMIT_HIT
    if state == SetupLifecycleState.TP_HIT:
        return _tp_alert_type(symbol_result)
    if state == SetupLifecycleState.SL_HIT:
        return TelegramAlertType.SL_HIT
    if state == SetupLifecycleState.INVALIDATED:
        return TelegramAlertType.INVALIDATED
    if state == SetupLifecycleState.EXPIRED:
        return TelegramAlertType.EXPIRED
    if state == SetupLifecycleState.COOLDOWN:
        return TelegramAlertType.NO_LONGER_TRACKING
    if _explicit_watchlist_candidate(symbol_result):
        return TelegramAlertType.WATCHLIST
    return None


def _tp_alert_type(symbol_result: ScannerSymbolResult) -> TelegramAlertType | None:
    diagnostics = _representative_diagnostics(symbol_result)
    for value in (
        diagnostics.get("telegram_alert_type"),
        diagnostics.get("outcome_status"),
        diagnostics.get("tp_hit"),
        diagnostics.get("highest_tp_hit"),
    ):
        text = _status_key(value)
        if text in {"tp3", "tp3_hit", "3"}:
            return TelegramAlertType.TP3_HIT
        if text in {"tp2", "tp2_hit", "2"}:
            return TelegramAlertType.TP2_HIT
        if text in {"tp1", "tp1_hit", "1"}:
            return TelegramAlertType.TP1_HIT
    return None


def _requires_prior_active_alert(alert_type: TelegramAlertType) -> bool:
    return alert_type not in {TelegramAlertType.WATCHLIST, TelegramAlertType.SIGNAL_CONFIRMED}


def _terminal_transition_blockers(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
) -> tuple[str, ...]:
    transition = symbol_result.lifecycle_transition
    if transition is None:
        return ("missing_lifecycle_transition",)
    previous = _previous_transition_state(symbol_result)
    if previous is None:
        return ("terminal_transition_missing_previous_state",)

    if alert_type == TelegramAlertType.INVALIDATED:
        allowed = WATCH_ALERT_STATES | SIGNAL_ALERT_STATES | {SetupLifecycleState.MANAGING}
    elif alert_type == TelegramAlertType.EXPIRED:
        allowed = WATCH_ALERT_STATES | SIGNAL_ALERT_STATES | {SetupLifecycleState.MANAGING}
    elif alert_type == TelegramAlertType.NO_LONGER_TRACKING:
        allowed = WATCH_ALERT_STATES | {SetupLifecycleState.INVALIDATED, SetupLifecycleState.EXPIRED}
    else:
        allowed = set()

    if previous not in allowed:
        return (f"terminal_transition_not_meaningful:{previous.value}->{transition.to_state.value}",)
    return ()


def _previous_transition_state(symbol_result: ScannerSymbolResult) -> SetupLifecycleState | None:
    transition = symbol_result.lifecycle_transition
    if transition is not None and transition.from_state is not None:
        return transition.from_state
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is not None:
        return lifecycle.previous_state
    return None


def _failed_confirmation_terminal_decision(
    symbol_result: ScannerSymbolResult,
    *,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage,
    blockers: Sequence[str],
    lifecycle_transition: SetupTransitionResult | None,
    prior_public_alert: TelegramAlertAttemptRecord | None,
    terminal_identity_failure_reason: str | None,
) -> TelegramAlertDecision | None:
    if alert_type != TelegramAlertType.SIGNAL_CONFIRMED:
        return None
    if not _failed_confirmation_evidence(symbol_result, blockers):
        return None

    terminal_alert_type = (
        TelegramAlertType.INVALIDATED
        if _failed_confirmation_is_structural_invalidation(symbol_result, blockers)
        else TelegramAlertType.NO_LONGER_TRACKING
    )
    failed_message = replace(
        message,
        invalidation_reason=_failed_confirmation_reason(symbol_result, blockers, terminal_alert_type),
    )
    if prior_public_alert is not None and prior_public_alert.alert_type == TelegramAlertType.WATCHLIST.value:
        return TelegramAlertDecision(
            True,
            "eligible_failed_confirmation_update",
            alert_type=terminal_alert_type,
            message=_message_with_prior_public_identity(failed_message, prior_public_alert),
            lifecycle_transition=lifecycle_transition,
        )

    if terminal_identity_failure_reason in {
        "terminal_update_identity_ambiguous",
        "terminal_update_identity_not_matched",
    }:
        return TelegramAlertDecision(
            False,
            terminal_identity_failure_reason,
            alert_type=terminal_alert_type,
            message=failed_message,
            lifecycle_transition=lifecycle_transition,
        )
    return None


def _failed_confirmation_core_blockers(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    blockers: list[str] = []
    lifecycle = symbol_result.lifecycle_state
    failed_gate = _status_key(getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA)
    if failed_gate:
        blockers.append(f"failed_confirmation_gate:{failed_gate}")

    text = _failed_confirmation_haystack(symbol_result)
    text_key = _status_key(text)
    if "rejected_by_regime" in text_key or "regime_compatibility" in text_key or "regime_weakness" in text_key:
        blockers.append("failed_confirmation_text:regime_compatibility")
    if "technical_score_below" in text_key or "low_technical" in text_key:
        blockers.append("failed_confirmation_text:technical_quality")
    if "opportunity_score_below" in text_key or "score_below" in text_key:
        blockers.append("failed_confirmation_text:final_score")
    if "rr_below" in text_key or "risk_reward_below" in text_key or "below_minimum" in text_key:
        blockers.append("failed_confirmation_text:final_rr")
    if "quality_gate_failed" in text_key:
        blockers.append("failed_confirmation_text:quality_gate")
    if "setup_rejected" in text_key or "not_public_ready" in text_key:
        blockers.append("failed_confirmation_text:setup_rejected")

    action_label = _status_key(getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA)
    if action_label and any(token in action_label for token in ("reject", "wait", "no_trade", "removed")):
        blockers.append(f"failed_confirmation_action:{action_label}")
    return tuple(dict.fromkeys(blockers))


def _failed_confirmation_evidence(
    symbol_result: ScannerSymbolResult,
    blockers: Sequence[str],
) -> bool:
    if any(str(blocker).startswith("failed_confirmation_") for blocker in blockers):
        return True
    if _text(getattr(symbol_result.lifecycle_state, "failed_gate", NA)) != NA:
        return True
    return bool(blockers)


def _failed_confirmation_is_structural_invalidation(
    symbol_result: ScannerSymbolResult,
    blockers: Sequence[str],
) -> bool:
    haystack = " ".join(
        (
            _failed_confirmation_haystack(symbol_result),
            " ".join(str(blocker) for blocker in blockers),
        )
    )
    key = _status_key(haystack)
    return any(
        token in key
        for token in (
            "structural_breakdown",
            "body_acceptance_failure",
            "pullback_too_deep",
            "pullback_beyond_786",
            "structure_failed",
            "structure_broke",
        )
    )


def _failed_confirmation_reason(
    symbol_result: ScannerSymbolResult,
    blockers: Sequence[str],
    alert_type: TelegramAlertType,
) -> str:
    if alert_type == TelegramAlertType.INVALIDATED:
        return "Watchlist invalidated because the sweep/BOS/CHoCH structure failed before confirmation."

    haystack = " ".join((_failed_confirmation_haystack(symbol_result), " ".join(str(blocker) for blocker in blockers)))
    key = _status_key(haystack)
    if "regime_compatibility" in key or "regime_weakness" in key or "rejected_by_regime" in key:
        return "Watchlist removed because market conditions failed before confirmation."
    if "rr_below" in key or "low_rr" in key or "risk_reward_below" in key:
        return "Watchlist removed because final RR or target quality failed before confirmation."
    if "target_integrity" in key:
        return "Watchlist removed because final RR or target quality failed before confirmation."
    if "technical" in key:
        return "Watchlist removed because technical quality failed before confirmation."
    if "opportunity" in key or "scoring" in key or "score_below" in key:
        return "Watchlist removed because final score requirements were not met before confirmation."
    if "quality_gate" in key or "setup_quality" in key:
        return "Watchlist removed because the setup failed final confirmation gates."
    if "rejected" in key or "not_public_ready" in key:
        return "Watchlist removed because the setup failed final confirmation gates."
    return "Watchlist removed because the setup failed final confirmation gates."


def _failed_confirmation_haystack(symbol_result: ScannerSymbolResult) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    transition = symbol_result.lifecycle_transition
    parts = [
        getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA,
        getattr(lifecycle, "invalidation_reason", NA) if lifecycle is not None else NA,
        getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA,
        getattr(transition, "notes", NA) if transition is not None else NA,
        symbol_result.rejection_reason,
        *symbol_result.rejection_reasons,
        diagnostics.get("reason"),
        diagnostics.get("invalidation_reason"),
        diagnostics.get("regime_reason"),
        diagnostics.get("regime_diagnostics"),
        diagnostics.get("first_failed_gate"),
        diagnostics.get("failed_gate"),
    ]
    return " ".join(_text(part) for part in parts if _text(part) != NA)


def _terminal_update_reason(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
) -> str:
    raw_reason = _terminal_raw_reason(symbol_result, alert_type)
    cleaned = _clean_terminal_reason(raw_reason)
    if alert_type == TelegramAlertType.INVALIDATED:
        if cleaned != NA:
            return _terminal_sentence("Watchlist invalidated because", cleaned)
        return "Watchlist is no longer valid according to the lifecycle engine."
    if alert_type == TelegramAlertType.EXPIRED:
        if cleaned != NA and _status_key(cleaned) != "setup_expired_before_completion":
            return _terminal_sentence("Watchlist expired because", cleaned)
        return "Watchlist expired because it did not confirm within the valid tracking window."
    if alert_type == TelegramAlertType.NO_LONGER_TRACKING:
        if cleaned != NA and "cooldown" not in cleaned.lower():
            return _terminal_sentence("Watchlist removed because", cleaned)
        return "Watchlist removed because the setup entered cooldown before confirmation."
    return "Watchlist is no longer valid according to the lifecycle engine."


def _terminal_raw_reason(symbol_result: ScannerSymbolResult, alert_type: TelegramAlertType) -> Any:
    diagnostics = _representative_diagnostics(symbol_result)
    transition = symbol_result.lifecycle_transition
    lifecycle = symbol_result.lifecycle_state
    if alert_type == TelegramAlertType.EXPIRED:
        return _first_non_na(
            diagnostics.get("terminal_reason"),
            diagnostics.get("expired_reason"),
            getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA,
            diagnostics.get("first_failed_gate"),
            diagnostics.get("failed_gate"),
        )
    if alert_type == TelegramAlertType.NO_LONGER_TRACKING:
        return _first_non_na(
            diagnostics.get("terminal_reason"),
            diagnostics.get("cooldown_reason"),
            getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA,
            getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA,
            diagnostics.get("first_failed_gate"),
            diagnostics.get("failed_gate"),
        )
    return _first_non_na(
        getattr(lifecycle, "invalidation_reason", NA) if lifecycle is not None else NA,
        getattr(transition, "notes", NA) if transition is not None else NA,
        getattr(getattr(transition, "reason", None), "value", NA) if transition is not None else NA,
        getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA,
        getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA,
        diagnostics.get("terminal_reason"),
        diagnostics.get("invalidation_reason"),
        diagnostics.get("pullback_failure_reason"),
        diagnostics.get("first_failed_gate"),
        diagnostics.get("failed_gate"),
    )


def _clean_terminal_reason(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return NA
    key = _status_key(text.rstrip(".!?"))
    mapped = {
        "setup_invalidated_by_current_structure_or_failed_gate": "the sweep/BOS/CHoCH structure failed before confirmation",
        "setup_expired_before_completion": "it did not confirm within the valid tracking window",
        "lifecycle_moved_into_cooldown": "the setup entered cooldown before confirmation",
        "entry_window_expired": "it did not confirm within the valid tracking window",
        "pullback_too_deep": "price accepted beyond the planned invalidation structure",
        "pullback_beyond_786": "price accepted beyond the planned invalidation structure",
        "body_acceptance_failure": "price accepted beyond the planned invalidation structure",
        "structural_breakdown": "the sweep/BOS/CHoCH structure failed before confirmation",
        "no_displacement_candle": "the required displacement structure failed before confirmation",
        "missing_displacement_impulse": "the required displacement structure failed before confirmation",
    }.get(key)
    if mapped is not None:
        return mapped
    lowered = text.lower()
    if (
        _looks_like_rejection_reason(text)
        or "decimal(" in lowered
        or "{" in text
        or "}" in text
        or lowered in {"true", "false"}
    ):
        return NA
    if key.startswith("invalid_if") or key.startswith("invalidates_if") or key.startswith("signal_invalidates_if"):
        return "price accepted beyond the planned invalidation structure"
    return _plain_label(text) if "_" in text and len(text.split()) == 1 else text


def _terminal_sentence(prefix: str, reason: str) -> str:
    cleaned = _clean_watch_text(reason)
    if cleaned == NA:
        return "Watchlist is no longer valid according to the lifecycle engine."
    lower = cleaned.lower()
    if lower.startswith(("watchlist ", "signal ", "setup ")):
        return cleaned
    return f"{prefix} {cleaned[:1].lower()}{cleaned[1:]}"


def _missing_required_fields(alert_type: TelegramAlertType, message: TelegramSignalMessage) -> tuple[str, ...]:
    required: list[tuple[str, Any]] = [
        ("signal_id", message.signal_id),
        ("symbol", message.symbol),
        ("direction", message.direction),
    ]
    if alert_type in {TelegramAlertType.SIGNAL_CONFIRMED, TelegramAlertType.LIMIT_HIT}:
        required.extend(
            [
                ("entry_low", message.entry_low),
                ("entry_high", message.entry_high),
                ("stop_loss", message.stop_loss),
                ("planned_rr", message.planned_rr),
            ]
        )
    if alert_type == TelegramAlertType.WATCHLIST:
        required.append(
            (
                "invalidation_reason",
                _first_non_na(message.watchlist_invalidation_reason, message.invalidation_reason),
            )
        )
    elif alert_type in {
        TelegramAlertType.SIGNAL_CONFIRMED,
        TelegramAlertType.INVALIDATED,
        TelegramAlertType.EXPIRED,
        TelegramAlertType.NO_LONGER_TRACKING,
    }:
        required.append(("invalidation_reason", message.invalidation_reason))
    if alert_type == TelegramAlertType.SIGNAL_CONFIRMED:
        required.extend((("tp1", message.tp1), ("tp2", message.tp2), ("tp3", message.tp3)))
    if alert_type == TelegramAlertType.TP1_HIT:
        required.append(("tp1", message.tp1))
    if alert_type == TelegramAlertType.TP2_HIT:
        required.append(("tp2", message.tp2))
    if alert_type == TelegramAlertType.TP3_HIT:
        required.append(("tp3", message.tp3))
    if alert_type == TelegramAlertType.SL_HIT:
        required.append(("stop_loss", message.stop_loss))
    return tuple(name for name, value in required if _text(value) == NA)


def _defensive_delivery_blockers(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage,
    context: TelegramEligibilityContext,
) -> tuple[str, ...]:
    if alert_type == TelegramAlertType.WATCHLIST:
        blockers: list[str] = []
        explicit_watchlist = _explicit_watchlist_candidate(symbol_result)
        blockers.extend(_watchlist_status_blockers(symbol_result, explicit_watchlist=explicit_watchlist))
        quality_state = _setup_quality_state_key(symbol_result)
        if quality_state == "data_issue":
            blockers.append(f"setup_quality_blocked:{quality_state}")
        elif quality_state in WATCHLIST_BLOCKED_QUALITY_STATE_KEYS and not explicit_watchlist:
            blockers.append(f"setup_quality_blocked:{quality_state}")
        if _text(symbol_result.rejection_reason) != NA and not explicit_watchlist:
            blockers.append("rejection_reason_present")
        if any(_text(reason) != NA for reason in symbol_result.rejection_reasons) and not explicit_watchlist:
            blockers.append("rejection_reasons_present")
        blockers.extend(_watchlist_public_readiness_blockers(symbol_result, message, context))
        blockers.extend(_target_integrity_blockers(symbol_result, alert_type, message))
        return tuple(dict.fromkeys(blockers))

    if alert_type in TERMINAL_UPDATE_ALERT_TYPES:
        blockers = list(_terminal_transition_blockers(symbol_result, alert_type))
        missing = _missing_required_fields(alert_type, message)
        if missing:
            blockers.append(f"missing_required_fields:{','.join(missing)}")
        return tuple(dict.fromkeys(blockers))

    if alert_type != TelegramAlertType.SIGNAL_CONFIRMED:
        return ()

    blockers: list[str] = []
    blockers.extend(_core_status_blockers(symbol_result))
    blockers.extend(_failed_confirmation_core_blockers(symbol_result))

    quality_state = _setup_quality_state_key(symbol_result)
    if quality_state not in CONFIRMED_ALLOWED_QUALITY_STATE_KEYS:
        blockers.append(f"setup_quality_not_confirmed:{quality_state or 'missing'}")

    if symbol_result.trade_idea is None:
        blockers.append("trade_idea_missing")
    else:
        quality_gate = getattr(symbol_result.trade_idea, "quality_gate_result", None)
        if quality_gate is not None and getattr(quality_gate, "passed", True) is not True:
            blockers.append("quality_gate_failed")

    if _text(symbol_result.rejection_reason) != NA:
        blockers.append("rejection_reason_present")
    if any(_text(reason) != NA for reason in symbol_result.rejection_reasons):
        blockers.append("rejection_reasons_present")

    planned_rr = _decimal_or_none(message.planned_rr)
    if planned_rr is None:
        blockers.append("planned_rr_missing_or_invalid")
    elif planned_rr < context.min_rr:
        blockers.append(f"planned_rr_below_min:{_text(planned_rr)}<{_text(context.min_rr)}")

    opportunity_score = _opportunity_score_decimal(symbol_result)
    if context.min_score_for_idea is not None:
        if opportunity_score is None:
            blockers.append("opportunity_score_missing")
        elif opportunity_score < context.min_score_for_idea:
            blockers.append(
                f"opportunity_score_below_min:{_text(opportunity_score)}<{_text(context.min_score_for_idea)}"
            )

    technical_score = _technical_score_decimal(symbol_result)
    if technical_score is not None and technical_score < context.min_technical_score:
        blockers.append(f"technical_score_below_min:{_text(technical_score)}<{_text(context.min_technical_score)}")

    missing = _missing_required_fields(alert_type, message)
    if missing:
        blockers.append(f"missing_required_fields:{','.join(missing)}")

    raw_invalidation = _raw_invalidation_text(symbol_result)
    if _text(message.invalidation_reason) == NA:
        blockers.append("invalidation_missing")
    if _looks_like_rejection_reason(raw_invalidation) or _looks_like_rejection_reason(message.invalidation_reason):
        blockers.append("invalidation_contains_rejection_reason")

    blockers.extend(_target_integrity_blockers(symbol_result, alert_type, message))

    return tuple(dict.fromkeys(blockers))


def _target_integrity_blockers(
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage,
) -> tuple[str, ...]:
    if alert_type not in {TelegramAlertType.WATCHLIST, TelegramAlertType.SIGNAL_CONFIRMED}:
        return ()
    side = _status_key(message.direction)
    if side not in {"long", "short"}:
        return ()

    confirmed = alert_type == TelegramAlertType.SIGNAL_CONFIRMED
    entry_reference = _entry_reference(symbol_result, message)
    stop_loss = _decimal_or_none(message.stop_loss)
    targets = (
        ("tp1", _decimal_or_none(message.tp1)),
        ("tp2", _decimal_or_none(message.tp2)),
        ("tp3", _decimal_or_none(message.tp3)),
    )
    invalid_fields: list[str] = []

    if confirmed and entry_reference is None:
        invalid_fields.append("entry_reference")
    if confirmed and stop_loss is None:
        invalid_fields.append("stop_loss")
    if confirmed:
        invalid_fields.extend(name for name, value in targets if value is None)

    if entry_reference is not None:
        if stop_loss is not None:
            if side == "long" and stop_loss >= entry_reference:
                invalid_fields.append("stop_loss")
            elif side == "short" and stop_loss <= entry_reference:
                invalid_fields.append("stop_loss")
        for name, target in targets:
            if target is None:
                continue
            if side == "long" and target <= entry_reference:
                invalid_fields.append(name)
            elif side == "short" and target >= entry_reference:
                invalid_fields.append(name)

    numeric_targets = tuple((name, value) for name, value in targets if value is not None)
    if len(numeric_targets) >= 2:
        for (_, left), (_, right) in zip(numeric_targets, numeric_targets[1:]):
            if side == "long" and left >= right:
                invalid_fields.append("tp_order")
                break
            if side == "short" and left <= right:
                invalid_fields.append("tp_order")
                break

    invalid = tuple(dict.fromkeys(invalid_fields))
    if not invalid:
        return ()
    return (f"target_integrity_failed:invalid_target_fields={','.join(invalid)}",)


def _entry_reference(symbol_result: ScannerSymbolResult, message: TelegramSignalMessage) -> Decimal | None:
    entry_pair = _decimal_pair_values(message.entry_low, message.entry_high)
    if entry_pair is not None:
        low, high = entry_pair
        return (low + high) / Decimal("2")

    watch_pair = _decimal_pair_text(message.watch_zone)
    if watch_pair is not None:
        low, high = watch_pair
        return (low + high) / Decimal("2")

    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    return _first_decimal(
        _field(setup, "entry"),
        _field(setup, "planned_entry"),
        _field(setup, "current_planned_entry"),
        diagnostics.get("entry"),
        diagnostics.get("entry_price"),
        diagnostics.get("planned_entry"),
        diagnostics.get("current_planned_entry"),
        diagnostics.get("limit_entry"),
        diagnostics.get("limit_price"),
        _level_field(getattr(trade_idea, "entry_zone", None), "price"),
    )


def _core_status_blockers(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    blockers: list[str] = []
    for status_key in _status_keys(symbol_result):
        if status_key in CONFIRMED_REJECTED_STATUS_KEYS:
            blockers.append(f"core_status_blocked:{status_key}")
    return tuple(dict.fromkeys(blockers))


def _watchlist_status_blockers(
    symbol_result: ScannerSymbolResult,
    *,
    explicit_watchlist: bool,
) -> tuple[str, ...]:
    blockers: list[str] = []
    for status_key in _status_keys(symbol_result):
        if status_key in WATCHLIST_HARD_STATUS_BLOCKERS:
            blockers.append(f"core_status_blocked:{status_key}")
        elif not explicit_watchlist and status_key in CONFIRMED_REJECTED_STATUS_KEYS:
            blockers.append(f"core_status_blocked:{status_key}")
    return tuple(dict.fromkeys(blockers))


def _watchlist_public_readiness_blockers(
    symbol_result: ScannerSymbolResult,
    message: TelegramSignalMessage,
    context: TelegramEligibilityContext,
) -> tuple[str, ...]:
    missing: list[str] = []
    if _text(message.symbol) == NA:
        missing.append("symbol")
    if _status_key(message.direction) not in {"long", "short"}:
        missing.append("direction")
    if _text(message.signal_id) == NA:
        missing.append("signal_id")

    if missing:
        return (f"watchlist_not_public_ready:missing_public_fields={','.join(missing)}",)

    blockers: list[str] = []
    missing_public = _watchlist_missing_public_sections(message)
    if missing_public:
        blockers.append(f"watchlist_not_public_ready:missing_public_fields={','.join(missing_public)}")

    if _watchlist_plan_all_na(message):
        blockers.append("watchlist_missing_trackable_plan:all_plan_fields_na")
    elif not _watchlist_has_tracking_anchor(symbol_result, message):
        blockers.append("watchlist_not_public_ready:no_useful_tracking_anchor")

    if _watchlist_is_mostly_na(message):
        blockers.append("watchlist_not_public_ready:mostly_na_message")

    planned_rr = _decimal_or_none(message.planned_rr)
    if planned_rr is not None and planned_rr < context.min_rr and not _watchlist_rr_warning_present(message):
        blockers.append("watchlist_not_public_ready:rr_warning_missing")

    return tuple(dict.fromkeys(blockers))


def _watchlist_missing_public_sections(message: TelegramSignalMessage) -> tuple[str, ...]:
    missing: list[str] = []
    context = _text(message.current_context)
    if context == NA or _looks_raw_or_generic_context(context):
        missing.append("current_context")
    needs_next = _usable_needs_next(message.needs_next)
    if not needs_next:
        missing.append("needs_next")
    invalidation = _text(_first_non_na(message.watchlist_invalidation_reason, message.invalidation_reason))
    if invalidation == NA or _looks_like_rejection_reason(invalidation):
        missing.append("invalidation")
    return tuple(missing)


def _watchlist_has_tracking_anchor(symbol_result: ScannerSymbolResult, message: TelegramSignalMessage) -> bool:
    if _numeric_pair_text(message.watch_zone):
        return True
    if _numeric_pair_values(message.entry_low, message.entry_high):
        return True
    if _decimal_or_none(message.stop_loss) is not None and _usable_needs_next(message.needs_next):
        return True

    diagnostics = _representative_diagnostics(symbol_result)
    trackable_level = _first_non_na(
        diagnostics.get("initial_sweep_level"),
        diagnostics.get("sweep_level"),
        diagnostics.get("swing_level"),
        diagnostics.get("ltf_swing_level"),
        diagnostics.get("price_level"),
    )
    return _decimal_or_none(trackable_level) is not None and _has_structural_tracking_context(message.current_context)


def _watchlist_plan_all_na(message: TelegramSignalMessage) -> bool:
    return (
        _decimal_or_none(message.entry_low) is None
        and _decimal_or_none(message.entry_high) is None
        and _decimal_or_none(message.stop_loss) is None
        and _decimal_or_none(message.tp1) is None
        and _decimal_or_none(message.tp2) is None
        and _decimal_or_none(message.tp3) is None
        and _decimal_or_none(message.planned_rr) is None
    )


def _watchlist_is_mostly_na(message: TelegramSignalMessage) -> bool:
    fields = (
        message.watch_zone,
        message.entry_low,
        message.entry_high,
        message.stop_loss,
        message.tp1,
        message.tp2,
        message.tp3,
        message.planned_rr,
    )
    na_count = 0
    for index, value in enumerate(fields):
        if index == 0:
            missing = _decimal_pair_text(value) is None
        else:
            missing = _decimal_or_none(value) is None
        if missing:
            na_count += 1
    return na_count >= 7


def _usable_needs_next(values: Sequence[Any]) -> tuple[str, ...]:
    usable: list[str] = []
    for value in values:
        text = _text(value)
        if text == NA:
            continue
        key = text.lower()
        if "waiting for the next lifecycle update" in key:
            continue
        if _looks_like_rejection_reason(text):
            continue
        if not _chart_only_watch_condition(text):
            continue
        usable.append(text)
    return tuple(usable)


def _watchlist_rr_warning_present(message: TelegramSignalMessage) -> bool:
    planned_rr = _decimal_or_none(message.planned_rr)
    min_rr = _decimal_or_none(message.min_rr) or DEFAULT_CONFIRMED_MIN_RR
    if planned_rr is not None and planned_rr < min_rr:
        return True
    haystack = " ".join(
        text
        for text in (
            _text(message.current_context),
            " ".join(_text(value) for value in message.needs_next if _text(value) != NA),
        )
        if text != NA
    ).lower()
    return "rr" in haystack and ("before confirmation" in haystack or "must" in haystack)


def _looks_raw_or_generic_context(value: Any) -> bool:
    text = _text(value)
    if text == NA:
        return True
    lowered = text.lower()
    return (
        text in {"Core structure is still developing.", "N/A"}
        or "{" in text
        or "}" in text
        or "decimal(" in lowered
        or lowered in {"true", "false"}
    )


def _has_structural_tracking_context(value: Any) -> bool:
    text = _text(value).lower()
    if text == NA.lower():
        return False
    return any(token in text for token in ("sweep", "bos", "choch", "structure", "pullback", "ob/fvg"))


def _numeric_pair_values(low: Any, high: Any) -> bool:
    return _decimal_or_none(low) is not None and _decimal_or_none(high) is not None


def _decimal_pair_values(low: Any, high: Any) -> tuple[Decimal, Decimal] | None:
    low_value = _decimal_or_none(low)
    high_value = _decimal_or_none(high)
    if low_value is None or high_value is None:
        return None
    return low_value, high_value


def _numeric_pair_text(value: Any) -> bool:
    return _decimal_pair_text(value) is not None


def _decimal_pair_text(value: Any) -> tuple[Decimal, Decimal] | None:
    text = _text(value)
    if text == NA:
        return None
    parts = text.replace("\u2013", "-").replace("\u2014", "-").split("-")
    if len(parts) != 2:
        return None
    low = _decimal_or_none(parts[0].strip())
    high = _decimal_or_none(parts[1].strip())
    if low is None or high is None:
        return None
    return low, high


def _status_keys(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    values: list[Any] = [getattr(symbol_result.status, "value", symbol_result.status)]
    values.extend(getattr(status, "value", status) for status in symbol_result.status_history)
    return tuple(dict.fromkeys(_status_key(value) for value in values if _status_key(value)))


def _setup_quality_state_key(symbol_result: ScannerSymbolResult) -> str:
    quality_state = getattr(symbol_result.setup_quality, "quality_state", NA)
    return _status_key(getattr(quality_state, "value", quality_state))


def _explicit_watchlist_candidate(symbol_result: ScannerSymbolResult) -> bool:
    diagnostics = _representative_diagnostics(symbol_result)
    lifecycle = symbol_result.lifecycle_state
    intelligence = _near_miss_intelligence(symbol_result, diagnostics)
    quality_state = _setup_quality_state_key(symbol_result)
    if quality_state == "watchlist_near_miss":
        return True

    action_values = (
        getattr(symbol_result.setup_quality, "action_label", NA),
        getattr(lifecycle, "action_label", NA) if lifecycle is not None else NA,
        _field(intelligence, "action_label"),
        _field(intelligence, "watchlist_status"),
        diagnostics.get("action_label"),
        diagnostics.get("watchlist_status"),
    )
    if any(_status_key(value) in WATCHLIST_ACTION_KEYS for value in action_values):
        return True

    pullback_payload = diagnostics.get("pullback_intelligence")
    lifecycle_action = _first_non_na(
        _field(_field(pullback_payload, "projection"), "lifecycle_action"),
        diagnostics.get("lifecycle_action"),
    )
    return _status_key(lifecycle_action) in WATCHLIST_LIFECYCLE_ACTION_KEYS


def _persist_blocked_decision(decision: TelegramAlertDecision) -> bool:
    if decision.alert_type in TERMINAL_UPDATE_ALERT_TYPES:
        return decision.reason in TERMINAL_IDENTITY_BLOCK_REASONS or decision.reason.startswith("blocked:")
    return decision.alert_type in {TelegramAlertType.WATCHLIST, TelegramAlertType.SIGNAL_CONFIRMED} and decision.reason.startswith(
        "blocked:"
    )


def _invalid_target_fields_from_reason(reason: str) -> str:
    marker = "invalid_target_fields="
    text = _text(reason)
    if marker not in text:
        return NA
    fields = text.split(marker, 1)[1].split(";", 1)[0].strip()
    return fields if fields else NA


def _persist_blocked_attempt(
    repository: SQLiteTelegramAlertAttemptRepository,
    symbol_result: ScannerSymbolResult,
    *,
    decision: TelegramAlertDecision,
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> TelegramLifecycleDelivery:
    assert decision.alert_type is not None
    assert decision.message is not None
    signal_id = _signal_id(symbol_result)
    blocked_alert_type = _blocked_alert_type(decision.alert_type, decision.reason)
    transition = decision.lifecycle_transition
    previous_state = transition.from_state.value if transition and transition.from_state else NA
    new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
    seen_at = now_utc_iso()
    message_hash = hashlib.sha256(
        f"{signal_id}|{decision.alert_type.value}|{decision.reason}".encode("utf-8")
    ).hexdigest()
    record = TelegramAlertAttemptRecord(
        signal_id=signal_id,
        symbol=symbol_result.symbol,
        direction=decision.message.direction,
        previous_state=previous_state,
        new_state=new_state,
        alert_type=blocked_alert_type,
        lifecycle_state=_lifecycle_state_text(symbol_result),
        sent_at=seen_at,
        telegram_status="blocked",
        message_hash=message_hash,
        scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        attempted_alert_type=decision.alert_type.value,
        setup_quality_score=_quality_score(symbol_result),
        rr_planned=_text(decision.message.planned_rr),
        min_rr=_text(eligibility_context.min_rr),
        opportunity_score=_opportunity_score_text(symbol_result),
        min_score_for_idea=_text(eligibility_context.min_score_for_idea),
        technical_score=_technical_score_text(symbol_result),
        price_level=_price_level_for_alert(decision.alert_type, decision.message),
        **_message_level_metadata(decision.message),
        blocked_reason=decision.reason,
        invalid_target_fields=_invalid_target_fields_from_reason(decision.reason),
        error_message=decision.reason,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        last_error_message=decision.reason,
    )
    inserted = repository.insert_attempt(record)
    if not inserted:
        compacted = repository.compact_repeated_attempt(record)
        status = "blocked_repeat" if compacted else "duplicate"
        detail = (
            "Repeated blocked Telegram alert attempt compacted."
            if compacted
            else "Duplicate blocked Telegram alert attempt prevented."
        )
        return TelegramLifecycleDelivery(
            symbol=symbol_result.symbol,
            signal_id=signal_id,
            alert_type=decision.alert_type.value,
            status=status,
            detail=detail,
            message_hash=message_hash,
            error_message=decision.reason,
        )
    return TelegramLifecycleDelivery(
        symbol=symbol_result.symbol,
        signal_id=signal_id,
        alert_type=decision.alert_type.value,
        status="blocked",
        detail=_blocked_delivery_detail(decision.alert_type),
        message_hash=message_hash,
        error_message=decision.reason,
    )


def _persist_sent_watchlist_reconciliation_block(
    repository: SQLiteTelegramAlertAttemptRepository,
    prior_alert: TelegramAlertAttemptRecord,
    *,
    reason: str,
    scan_run_id: str | None,
) -> TelegramLifecycleDelivery:
    seen_at = now_utc_iso()
    blocked_alert_type = _blocked_alert_type(TelegramAlertType.NO_LONGER_TRACKING, reason)
    message_hash = hashlib.sha256(
        f"{prior_alert.signal_id}|{SENT_WATCHLIST_RECONCILIATION_ATTEMPT}|{reason}".encode("utf-8")
    ).hexdigest()
    record = TelegramAlertAttemptRecord(
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
        previous_state=NA,
        new_state=NA,
        alert_type=blocked_alert_type,
        lifecycle_state=NA,
        sent_at=seen_at,
        telegram_status="blocked",
        message_hash=message_hash,
        scan_run_id=scan_run_id,
        attempted_alert_type=SENT_WATCHLIST_RECONCILIATION_ATTEMPT,
        setup_quality_score=NA,
        rr_planned=NA,
        min_rr=NA,
        opportunity_score=NA,
        min_score_for_idea=NA,
        technical_score=NA,
        price_level=NA,
        blocked_reason=reason,
        invalid_target_fields=NA,
        error_message=reason,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id,
        last_error_message=reason,
    )
    inserted = repository.insert_attempt(record)
    if not inserted:
        compacted = repository.compact_repeated_attempt(record)
        status = "blocked_repeat" if compacted else "duplicate"
        detail = (
            "Repeated sent-watchlist reconciliation block compacted."
            if compacted
            else "Duplicate sent-watchlist reconciliation block prevented."
        )
        return TelegramLifecycleDelivery(
            symbol=prior_alert.symbol,
            signal_id=prior_alert.signal_id,
            alert_type=SENT_WATCHLIST_RECONCILIATION_ATTEMPT,
            status=status,
            detail=detail,
            message_hash=message_hash,
            error_message=reason,
        )
    return TelegramLifecycleDelivery(
        symbol=prior_alert.symbol,
        signal_id=prior_alert.signal_id,
        alert_type=SENT_WATCHLIST_RECONCILIATION_ATTEMPT,
        status="blocked",
        detail="Sent watchlist reconciliation blocked by lifecycle identity guard.",
        message_hash=message_hash,
        error_message=reason,
    )


def _persist_watchlist_outcome_audit(
    repository: SQLiteTelegramAlertAttemptRepository,
    prior_alert: TelegramAlertAttemptRecord,
    *,
    reason: str,
    scan_run_id: str | None,
    symbol_result: ScannerSymbolResult | None = None,
    message: TelegramSignalMessage | None = None,
    price_level: str = NA,
) -> None:
    seen_at = now_utc_iso()
    alert_type = _watchlist_outcome_audit_alert_type(reason)
    transition = symbol_result.lifecycle_transition if symbol_result is not None else None
    previous_state = transition.from_state.value if transition and transition.from_state else NA
    new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result) if symbol_result else NA
    message_hash = hashlib.sha256(
        f"{prior_alert.signal_id}|{WATCHLIST_OUTCOME_TRACKING_ATTEMPT}|{reason}".encode("utf-8")
    ).hexdigest()
    record = TelegramAlertAttemptRecord(
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
        previous_state=previous_state,
        new_state=new_state,
        alert_type=alert_type,
        lifecycle_state=_lifecycle_state_text(symbol_result) if symbol_result is not None else NA,
        sent_at=seen_at,
        telegram_status="skipped",
        message_hash=message_hash,
        scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        attempted_alert_type=WATCHLIST_OUTCOME_TRACKING_ATTEMPT,
        setup_quality_score=_quality_score(symbol_result) if symbol_result is not None else NA,
        rr_planned=_text(message.planned_rr) if message is not None else NA,
        min_rr=_text(message.min_rr) if message is not None else NA,
        opportunity_score=_opportunity_score_text(symbol_result) if symbol_result is not None else NA,
        min_score_for_idea=NA,
        technical_score=_technical_score_text(symbol_result) if symbol_result is not None else NA,
        price_level=price_level,
        **_message_level_metadata(message),
        blocked_reason=reason,
        invalid_target_fields=NA,
        error_message=reason,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        last_error_message=reason,
    )
    if not repository.insert_attempt(record):
        repository.compact_repeated_attempt(record)


def _persist_suppressed_watchlist_terminal_update(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    prior_alert: TelegramAlertAttemptRecord,
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType,
    message: TelegramSignalMessage | None,
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> TelegramLifecycleDelivery:
    seen_at = now_utc_iso()
    reason = "public_watchlist_terminal_updates_disabled"
    skipped_alert_type = _watchlist_terminal_suppression_alert_type(alert_type)
    transition = symbol_result.lifecycle_transition
    previous_state = transition.from_state.value if transition and transition.from_state else NA
    new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
    message_hash = hashlib.sha256(
        f"{prior_alert.signal_id}|{WATCHLIST_TERMINAL_SUPPRESSION_ATTEMPT}|{alert_type.value}".encode("utf-8")
    ).hexdigest()
    record = TelegramAlertAttemptRecord(
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
        previous_state=previous_state,
        new_state=new_state,
        alert_type=skipped_alert_type,
        lifecycle_state=_lifecycle_state_text(symbol_result),
        sent_at=seen_at,
        telegram_status="skipped",
        message_hash=message_hash,
        scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        attempted_alert_type=alert_type.value,
        setup_quality_score=_quality_score(symbol_result),
        rr_planned=_text(message.planned_rr) if message is not None else NA,
        min_rr=_text(eligibility_context.min_rr),
        opportunity_score=_opportunity_score_text(symbol_result),
        min_score_for_idea=_text(eligibility_context.min_score_for_idea),
        technical_score=_technical_score_text(symbol_result),
        price_level=_price_level_for_alert(alert_type, message) if message is not None else NA,
        **_message_level_metadata(message),
        blocked_reason=reason,
        invalid_target_fields=NA,
        error_message=reason,
        first_seen_at=seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        last_error_message=reason,
    )
    inserted = repository.insert_attempt(record)
    status = "skipped"
    detail = "Public watchlist terminal update suppressed by configuration."
    if not inserted:
        compacted = repository.compact_repeated_attempt(record)
        status = "blocked_repeat" if compacted else "duplicate"
        detail = (
            "Repeated suppressed watchlist terminal update compacted."
            if compacted
            else "Duplicate suppressed watchlist terminal update prevented."
        )
    return TelegramLifecycleDelivery(
        symbol=prior_alert.symbol,
        signal_id=prior_alert.signal_id,
        alert_type=alert_type.value,
        status=status,
        detail=detail,
        message_hash=message_hash,
        error_message=reason,
    )


def _watchlist_outcome_for_current_result(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    prior_alert: TelegramAlertAttemptRecord,
    current_result: ScannerSymbolResult,
    eligibility_context: TelegramEligibilityContext,
    scan_run_id: str | None,
) -> tuple[TelegramAlertType, TelegramSignalMessage] | None:
    if prior_alert.alert_type != TelegramAlertType.WATCHLIST.value or prior_alert.telegram_status != "sent":
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_no_prior_public_watchlist",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
        )
        return None
    if repository.has_sent_terminal_outcome(signal_id=prior_alert.signal_id):
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_already_closed",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
        )
        return None

    message = _message_with_prior_public_identity(
        replace(
            telegram_signal_message_from_symbol(current_result),
            min_rr=eligibility_context.min_rr,
            watchlist_outcome=True,
        ),
        prior_alert,
    )
    side = _status_key(message.direction)
    limit_zone = _limit_zone_values(message)
    if side not in {"long", "short"} or limit_zone is None:
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_missing_entry",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
            message=message,
        )
        return None

    candle = _watchlist_candle_snapshot(current_result)
    if candle is None:
        return None

    stop_loss = _valid_watchlist_stop(message, limit_zone)
    if stop_loss is None:
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_missing_stop",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
            message=message,
        )

    targets, missing_targets = _valid_watchlist_targets(message, limit_zone)
    if missing_targets:
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_missing_targets",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
            message=message,
        )

    if not repository.has_attempt(signal_id=prior_alert.signal_id, alert_type=TelegramAlertType.LIMIT_HIT):
        if not _candle_touches_zone(candle, limit_zone):
            _persist_watchlist_outcome_audit(
                repository,
                prior_alert,
                reason="outcome_tracking_not_limit_hit_yet",
                scan_run_id=scan_run_id,
                symbol_result=current_result,
                message=message,
                price_level=_price_level_for_alert(TelegramAlertType.LIMIT_HIT, message),
            )
            return None
        if _same_candle_touches_post_limit_outcome(candle, side=side, stop_loss=stop_loss, targets=targets):
            _persist_watchlist_outcome_audit(
                repository,
                prior_alert,
                reason="outcome_tracking_same_candle_ambiguous",
                scan_run_id=scan_run_id,
                symbol_result=current_result,
                message=message,
                price_level=_price_level_for_alert(TelegramAlertType.LIMIT_HIT, message),
            )
        return TelegramAlertType.LIMIT_HIT, message

    if repository.has_attempt(signal_id=prior_alert.signal_id, alert_type=TelegramAlertType.SL_HIT) or repository.has_attempt(
        signal_id=prior_alert.signal_id,
        alert_type=TelegramAlertType.TP3_HIT,
    ):
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_already_closed",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
            message=message,
        )
        return None

    sl_hit = stop_loss is not None and _stop_touched(candle, side=side, stop_loss=stop_loss)
    next_tp = _next_touched_watchlist_tp(repository, prior_alert.signal_id, candle, side=side, targets=targets)
    if sl_hit and next_tp is not None:
        _persist_watchlist_outcome_audit(
            repository,
            prior_alert,
            reason="outcome_tracking_same_candle_ambiguous",
            scan_run_id=scan_run_id,
            symbol_result=current_result,
            message=message,
            price_level=_price_level_for_alert(next_tp, message),
        )
        return None
    if sl_hit:
        return TelegramAlertType.SL_HIT, message
    if next_tp is not None:
        return next_tp, message
    return None


def _apply_soft_failed_confirmation_grace_to_decision(
    repository: SQLiteTelegramAlertAttemptRepository,
    symbol_result: ScannerSymbolResult,
    *,
    decision: TelegramAlertDecision,
    prior_alert: TelegramAlertAttemptRecord | None,
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> TelegramAlertDecision | None:
    if (
        decision.alert_type != TelegramAlertType.NO_LONGER_TRACKING
        or decision.message is None
        or prior_alert is None
        or prior_alert.alert_type != TelegramAlertType.WATCHLIST.value
    ):
        return decision
    if _is_explicit_no_longer_tracking_state(symbol_result) and not _soft_failed_confirmation_lifecycle_evidence(
        symbol_result
    ):
        return decision
    if (
        _terminal_alert_type_for_lifecycle_state(_status_key(_lifecycle_state_text(symbol_result)))
        == TelegramAlertType.NO_LONGER_TRACKING
    ):
        blockers = _confirmed_guard_blockers(symbol_result, eligibility_context)
        if _soft_failed_confirmation_blocker_key(symbol_result, blockers) == NA:
            return decision
    else:
        blockers = _confirmed_guard_blockers(symbol_result, eligibility_context)

    blocker_key = _soft_failed_confirmation_blocker_key(symbol_result, blockers)
    if blocker_key == NA:
        return decision
    seen_count = _record_soft_failed_confirmation_observation(
        repository,
        prior_alert=prior_alert,
        symbol_result=symbol_result,
        blocker_key=blocker_key,
        blockers=blockers,
        scan_run_id=scan_run_id,
        eligibility_context=eligibility_context,
    )
    if seen_count < SOFT_FAILED_CONFIRMATION_MIN_OBSERVATIONS:
        return None
    return replace(
        decision,
        message=replace(
            decision.message,
            invalidation_reason=SOFT_FAILED_CONFIRMATION_REMOVAL_REASON,
        ),
    )


def _apply_soft_failed_confirmation_grace_to_reconciliation(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    prior_alert: TelegramAlertAttemptRecord,
    outcome: SentWatchlistReconciliationOutcome,
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> SentWatchlistReconciliationOutcome | None:
    if outcome.alert_type != TelegramAlertType.NO_LONGER_TRACKING:
        return outcome
    if _is_explicit_no_longer_tracking_state(
        outcome.symbol_result
    ) and not _soft_failed_confirmation_lifecycle_evidence(outcome.symbol_result):
        return outcome
    blockers = _confirmed_guard_blockers(outcome.symbol_result, eligibility_context)
    blocker_key = _soft_failed_confirmation_blocker_key(outcome.symbol_result, blockers)
    if blocker_key == NA:
        return outcome
    seen_count = _record_soft_failed_confirmation_observation(
        repository,
        prior_alert=prior_alert,
        symbol_result=outcome.symbol_result,
        blocker_key=blocker_key,
        blockers=blockers,
        scan_run_id=scan_run_id,
        eligibility_context=eligibility_context,
    )
    if seen_count < SOFT_FAILED_CONFIRMATION_MIN_OBSERVATIONS:
        return None
    return SentWatchlistReconciliationOutcome(
        alert_type=outcome.alert_type,
        message=replace(
            outcome.message,
            invalidation_reason=SOFT_FAILED_CONFIRMATION_REMOVAL_REASON,
        ),
        symbol_result=outcome.symbol_result,
    )


def _record_soft_failed_confirmation_observation(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    prior_alert: TelegramAlertAttemptRecord,
    symbol_result: ScannerSymbolResult,
    blocker_key: str,
    blockers: Sequence[str],
    scan_run_id: str | None,
    eligibility_context: TelegramEligibilityContext,
) -> int:
    alert_type = _soft_failed_confirmation_alert_type(blocker_key)
    existing = repository.get_attempt(signal_id=prior_alert.signal_id, alert_type=alert_type)
    seen_at = now_utc_iso()
    transition = symbol_result.lifecycle_transition
    previous_state = transition.from_state.value if transition and transition.from_state else NA
    new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
    reason = f"soft_failed_confirmation:{blocker_key}"
    message_hash = hashlib.sha256(
        f"{prior_alert.signal_id}|{SOFT_FAILED_CONFIRMATION_ATTEMPT}|{blocker_key}".encode("utf-8")
    ).hexdigest()
    message = telegram_signal_message_from_symbol(symbol_result)
    record = TelegramAlertAttemptRecord(
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
        previous_state=previous_state,
        new_state=new_state,
        alert_type=alert_type,
        lifecycle_state=_lifecycle_state_text(symbol_result),
        sent_at=seen_at,
        telegram_status="skipped",
        message_hash=message_hash,
        scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        attempted_alert_type=SOFT_FAILED_CONFIRMATION_ATTEMPT,
        setup_quality_score=_quality_score(symbol_result),
        rr_planned=_text(message.planned_rr),
        min_rr=_text(eligibility_context.min_rr),
        opportunity_score=_opportunity_score_text(symbol_result),
        min_score_for_idea=_text(eligibility_context.min_score_for_idea),
        technical_score=_technical_score_text(symbol_result),
        price_level=_text(message.price_level),
        blocked_reason=reason,
        invalid_target_fields=_invalid_target_fields_from_reason("; ".join(str(blocker) for blocker in blockers)),
        error_message=reason,
        first_seen_at=existing.first_seen_at if existing is not None else seen_at,
        last_seen_at=seen_at,
        last_scan_run_id=scan_run_id or _transition_scan_run_id(transition),
        last_error_message=reason,
    )
    if existing is None:
        inserted = repository.insert_attempt(record)
        if inserted:
            return 1
    repository.compact_repeated_attempt(record)
    updated = repository.get_attempt(signal_id=prior_alert.signal_id, alert_type=alert_type)
    return updated.seen_count if updated is not None else 1


def _has_current_run_soft_failed_confirmation_observation(
    repository: SQLiteTelegramAlertAttemptRepository,
    *,
    signal_id: str,
    scan_run_id: str | None,
) -> bool:
    if scan_run_id is None:
        return False
    return any(
        attempt.attempted_alert_type == SOFT_FAILED_CONFIRMATION_ATTEMPT
        and attempt.telegram_status == "skipped"
        and attempt.last_scan_run_id == scan_run_id
        for attempt in repository.list_attempts(signal_id=signal_id)
    )


def _confirmed_guard_blockers(
    symbol_result: ScannerSymbolResult,
    eligibility_context: TelegramEligibilityContext,
) -> tuple[str, ...]:
    message = replace(
        telegram_signal_message_from_symbol(symbol_result),
        min_rr=eligibility_context.min_rr,
    )
    blockers = (
        *_defensive_delivery_blockers(
            symbol_result,
            TelegramAlertType.SIGNAL_CONFIRMED,
            message,
            eligibility_context,
        ),
        *_failed_confirmation_core_blockers(symbol_result),
    )
    return tuple(dict.fromkeys(str(blocker) for blocker in blockers if _text(blocker) != NA))


def _soft_failed_confirmation_blocker_key(
    symbol_result: ScannerSymbolResult,
    blockers: Sequence[str],
) -> str:
    if not blockers or _hard_failed_confirmation_blockers(symbol_result, blockers):
        return NA
    lifecycle = symbol_result.lifecycle_state
    failed_gate = _status_key(getattr(lifecycle, "failed_gate", NA) if lifecycle is not None else NA)
    if "regime_compatibility" in failed_gate:
        return "regime_compatibility"
    if "rr_below" in failed_gate or "low_rr" in failed_gate:
        return "rr_below_min"
    if "target_expansion" in failed_gate or "target_integrity" in failed_gate:
        return "target_expansion"
    haystack = " ".join(
        (
            _failed_confirmation_haystack(symbol_result),
            " ".join(str(blocker) for blocker in blockers),
        )
    )
    key = _status_key(haystack)
    if "regime_compatibility" in key or "cleaner_regime" in key or "regime_weakness" in key:
        return "regime_compatibility"
    if "target_expansion" in key or "target_integrity" in key or "not_enough_room" in key:
        return "target_expansion"
    if "rr_below" in key or "planned_rr_below" in key or "low_rr" in key or "risk_reward_below" in key:
        return "rr_below_min"
    if "opportunity_score" in key or "score_below" in key or "scoring" in key:
        return "score_below_min"
    if "technical_score" in key or "technical_quality" in key:
        return "technical_score"
    if "quality_gate" in key or "final_quality" in key or "setup_quality" in key:
        return "quality_gate"
    if "trade_idea_missing" in key or "missing_required_fields" in key:
        return "incomplete_confirmation"
    return NA


def _is_explicit_no_longer_tracking_state(symbol_result: ScannerSymbolResult) -> bool:
    return (
        _terminal_alert_type_for_lifecycle_state(_status_key(_lifecycle_state_text(symbol_result)))
        == TelegramAlertType.NO_LONGER_TRACKING
    )


def _soft_failed_confirmation_lifecycle_evidence(symbol_result: ScannerSymbolResult) -> bool:
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is None:
        return False
    haystack = _status_key(
        " ".join(
            _text(value)
            for value in (
                lifecycle.failed_gate,
                lifecycle.action_label,
                lifecycle.invalidation_reason,
                _failed_confirmation_haystack(symbol_result),
            )
            if _text(value) != NA
        )
    )
    return any(
        token in haystack
        for token in (
            "regime_compatibility",
            "cleaner_regime",
            "target_expansion",
            "target_integrity",
            "rr_below",
            "low_rr",
            "score_below",
            "technical_score",
            "quality_gate",
        )
    )


def _hard_failed_confirmation_blockers(
    symbol_result: ScannerSymbolResult,
    blockers: Sequence[str],
) -> bool:
    if _failed_confirmation_is_structural_invalidation(symbol_result, blockers):
        return True
    invalid_target_fields = _invalid_target_fields_from_reason("; ".join(str(blocker) for blocker in blockers))
    if invalid_target_fields != NA and _target_integrity_has_numeric_terminal_failure(symbol_result):
        return True
    haystack = " ".join(
        (
            _failed_confirmation_haystack(symbol_result),
            " ".join(str(blocker) for blocker in blockers),
        )
    )
    key = _status_key(haystack)
    return any(
        token in key
        for token in (
            "accepted_beyond_invalidation",
            "acceptance_beyond_invalidation",
            "body_acceptance_failure",
            "invalidation_broken",
            "stop_broken",
            "wrong_side_target",
            "non_monotonic",
            "impossible_target",
        )
    )


def _target_integrity_has_numeric_terminal_failure(symbol_result: ScannerSymbolResult) -> bool:
    message = telegram_signal_message_from_symbol(symbol_result)
    side = _status_key(message.direction)
    if side not in {"long", "short"}:
        return False
    entry_reference = _entry_reference(symbol_result, message)
    if entry_reference is None:
        return False

    stop_loss = _decimal_or_none(message.stop_loss)
    if stop_loss is not None:
        if side == "long" and stop_loss >= entry_reference:
            return True
        if side == "short" and stop_loss <= entry_reference:
            return True

    targets = tuple(
        value
        for value in (
            _decimal_or_none(message.tp1),
            _decimal_or_none(message.tp2),
            _decimal_or_none(message.tp3),
        )
        if value is not None
    )
    for target in targets:
        if side == "long" and target <= entry_reference:
            return True
        if side == "short" and target >= entry_reference:
            return True
    for left, right in zip(targets, targets[1:]):
        if side == "long" and left >= right:
            return True
        if side == "short" and left <= right:
            return True
    return False


def _soft_failed_confirmation_alert_type(blocker_key: str) -> str:
    digest = hashlib.sha256(_status_key(blocker_key).encode("utf-8")).hexdigest()[:12]
    return f"{SOFT_FAILED_CONFIRMATION_ATTEMPT}_{digest}"


def _match_sent_watchlist_lifecycle(
    prior_alert: TelegramAlertAttemptRecord,
    *,
    lifecycle_repository: SQLiteSetupLifecycleRepository,
    current_results: Sequence[ScannerSymbolResult],
    snapshot_watchlists_by_symbol: Mapping[str, tuple[TelegramAlertAttemptRecord, ...]],
) -> SentWatchlistLifecycleMatch:
    exact = lifecycle_repository.get_record_by_lifecycle_id(prior_alert.signal_id)
    if exact is not None:
        return SentWatchlistLifecycleMatch(record=exact)

    current_exact = _current_lifecycle_records_for_signal_id(prior_alert.signal_id, current_results)
    if len(current_exact) == 1:
        return SentWatchlistLifecycleMatch(record=current_exact[0])
    if len(current_exact) > 1:
        return SentWatchlistLifecycleMatch(blocked_reason=SENT_WATCHLIST_RECONCILIATION_AMBIGUOUS)

    active_watchlists = snapshot_watchlists_by_symbol.get(_symbol(prior_alert.symbol), ())
    if len(active_watchlists) != 1:
        reason = (
            SENT_WATCHLIST_RECONCILIATION_NO_MATCH
            if not active_watchlists
            else SENT_WATCHLIST_RECONCILIATION_AMBIGUOUS
        )
        return SentWatchlistLifecycleMatch(blocked_reason=reason)
    if active_watchlists[0].signal_id != prior_alert.signal_id:
        return SentWatchlistLifecycleMatch(blocked_reason=SENT_WATCHLIST_RECONCILIATION_AMBIGUOUS)

    fallback_records = _fallback_lifecycle_records_for_prior_alert(lifecycle_repository, prior_alert)
    if len(fallback_records) == 1:
        return SentWatchlistLifecycleMatch(record=fallback_records[0])
    if len(fallback_records) > 1:
        return SentWatchlistLifecycleMatch(blocked_reason=SENT_WATCHLIST_RECONCILIATION_AMBIGUOUS)
    return SentWatchlistLifecycleMatch(blocked_reason=SENT_WATCHLIST_RECONCILIATION_NO_MATCH)


def _sent_watchlist_snapshot_by_symbol(
    sent_watchlists: Sequence[TelegramAlertAttemptRecord],
) -> dict[str, tuple[TelegramAlertAttemptRecord, ...]]:
    grouped: dict[str, list[TelegramAlertAttemptRecord]] = {}
    for prior_alert in sent_watchlists:
        if prior_alert.alert_type != TelegramAlertType.WATCHLIST.value or prior_alert.telegram_status != "sent":
            continue
        grouped.setdefault(_symbol(prior_alert.symbol), []).append(prior_alert)
    return {symbol: tuple(records) for symbol, records in grouped.items()}


def _current_result_for_prior_watchlist(
    prior_alert: TelegramAlertAttemptRecord,
    current_results: Sequence[ScannerSymbolResult],
) -> ScannerSymbolResult | None:
    exact_matches = tuple(
        symbol_result
        for symbol_result in current_results
        if prior_alert.signal_id in _signal_id_candidates(symbol_result)
    )
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        return None

    symbol_matches = [
        symbol_result
        for symbol_result in current_results
        if _symbol(symbol_result.symbol) == _symbol(prior_alert.symbol)
    ]
    prior_direction = _status_key(prior_alert.direction)
    if prior_direction in {"long", "short"}:
        symbol_matches = [
            symbol_result
            for symbol_result in symbol_matches
            if _status_key(telegram_signal_message_from_symbol(symbol_result).direction) in {prior_direction, ""}
        ]
    return symbol_matches[0] if len(symbol_matches) == 1 else None


def _watchlist_candle_snapshot(symbol_result: ScannerSymbolResult) -> WatchlistCandleSnapshot | None:
    diagnostics = _representative_diagnostics(symbol_result)
    for key in ("current_candle", "latest_candle", "candle"):
        snapshot = _candle_snapshot_from_value(diagnostics.get(key))
        if snapshot is not None:
            return snapshot

    for high_key, low_key in (
        ("candle_high", "candle_low"),
        ("latest_high", "latest_low"),
        ("current_high", "current_low"),
        ("high", "low"),
    ):
        snapshot = _candle_snapshot_from_high_low(diagnostics.get(high_key), diagnostics.get(low_key))
        if snapshot is not None:
            return snapshot

    snapshot = _candle_snapshot_from_high_low(
        getattr(symbol_result, "latest_high", NA),
        getattr(symbol_result, "latest_low", NA),
    )
    if snapshot is not None:
        return snapshot

    for key in ("candles_5m", "candles_15m", "candles_1h", "candles_4h", "candles_12h", "candles_2d", "candles"):
        values = diagnostics.get(key)
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray, Mapping)) or not values:
            continue
        snapshot = _candle_snapshot_from_value(values[-1])
        if snapshot is not None:
            return snapshot
    return None


def _candle_snapshot_from_value(value: Any) -> WatchlistCandleSnapshot | None:
    if isinstance(value, Mapping):
        high = _decimal_or_none(value.get("high"))
        low = _decimal_or_none(value.get("low"))
        identity = _first_non_na(
            value.get("timestamp"),
            value.get("opened_at"),
            value.get("open_time"),
            value.get("time"),
        )
    else:
        high = _decimal_or_none(getattr(value, "high", NA))
        low = _decimal_or_none(getattr(value, "low", NA))
        identity = _first_non_na(
            getattr(value, "timestamp", NA),
            getattr(value, "opened_at", NA),
            getattr(value, "open_time", NA),
            getattr(value, "time", NA),
        )
    if high is None or low is None:
        return None
    return WatchlistCandleSnapshot(high=max(high, low), low=min(high, low), identity=_text(identity))


def _candle_snapshot_from_high_low(high: Any, low: Any) -> WatchlistCandleSnapshot | None:
    high_value = _decimal_or_none(high)
    low_value = _decimal_or_none(low)
    if high_value is None or low_value is None:
        return None
    return WatchlistCandleSnapshot(high=max(high_value, low_value), low=min(high_value, low_value))


def _limit_zone_values(message: TelegramSignalMessage) -> tuple[Decimal, Decimal] | None:
    watch_pair = _decimal_pair_text(message.watch_zone)
    if watch_pair is not None:
        low, high = watch_pair
        return min(low, high), max(low, high)

    low = _decimal_or_none(message.entry_low)
    high = _decimal_or_none(message.entry_high)
    if low is not None and high is not None:
        return min(low, high), max(low, high)
    single = low if low is not None else high
    if single is not None:
        return single, single
    return None


def _candle_touches_zone(candle: WatchlistCandleSnapshot, zone: tuple[Decimal, Decimal]) -> bool:
    low, high = zone
    return candle.high >= low and candle.low <= high


def _valid_watchlist_stop(message: TelegramSignalMessage, zone: tuple[Decimal, Decimal]) -> Decimal | None:
    stop_loss = _decimal_or_none(message.stop_loss)
    if stop_loss is None:
        return None
    side = _status_key(message.direction)
    reference = (zone[0] + zone[1]) / Decimal("2")
    if side == "long" and stop_loss >= reference:
        return None
    if side == "short" and stop_loss <= reference:
        return None
    return stop_loss


def _valid_watchlist_targets(
    message: TelegramSignalMessage,
    zone: tuple[Decimal, Decimal],
) -> tuple[tuple[TelegramAlertType, Decimal], bool]:
    side = _status_key(message.direction)
    reference = (zone[0] + zone[1]) / Decimal("2")
    raw_targets = (
        (TelegramAlertType.TP1_HIT, _decimal_or_none(message.tp1)),
        (TelegramAlertType.TP2_HIT, _decimal_or_none(message.tp2)),
        (TelegramAlertType.TP3_HIT, _decimal_or_none(message.tp3)),
    )
    present = tuple((alert_type, target) for alert_type, target in raw_targets if target is not None)
    missing = len(present) != len(raw_targets)
    if not present or side not in {"long", "short"}:
        return (), True

    invalid = False
    for _alert_type, target in present:
        if side == "long" and target <= reference:
            invalid = True
        if side == "short" and target >= reference:
            invalid = True
    for (_, left), (_, right) in zip(present, present[1:]):
        if side == "long" and left >= right:
            invalid = True
        if side == "short" and left <= right:
            invalid = True
    return (() if invalid else present), missing or invalid


def _same_candle_touches_post_limit_outcome(
    candle: WatchlistCandleSnapshot,
    *,
    side: str,
    stop_loss: Decimal | None,
    targets: Sequence[tuple[TelegramAlertType, Decimal]],
) -> bool:
    if stop_loss is not None and _stop_touched(candle, side=side, stop_loss=stop_loss):
        return True
    return any(_target_touched(candle, side=side, target=target) for _alert_type, target in targets)


def _stop_touched(candle: WatchlistCandleSnapshot, *, side: str, stop_loss: Decimal) -> bool:
    if side == "long":
        return candle.low <= stop_loss
    if side == "short":
        return candle.high >= stop_loss
    return False


def _target_touched(candle: WatchlistCandleSnapshot, *, side: str, target: Decimal) -> bool:
    if side == "long":
        return candle.high >= target
    if side == "short":
        return candle.low <= target
    return False


def _next_touched_watchlist_tp(
    repository: SQLiteTelegramAlertAttemptRepository,
    signal_id: str,
    candle: WatchlistCandleSnapshot,
    *,
    side: str,
    targets: Sequence[tuple[TelegramAlertType, Decimal]],
) -> TelegramAlertType | None:
    for alert_type, target in targets:
        if repository.has_attempt(signal_id=signal_id, alert_type=alert_type):
            continue
        if _target_touched(candle, side=side, target=target):
            return alert_type
    return None


def _current_lifecycle_records_for_signal_id(
    signal_id: str,
    current_results: Sequence[ScannerSymbolResult],
) -> tuple[SetupLifecycleRecord, ...]:
    normalized = _identity(signal_id)
    if normalized == NA:
        return ()
    records: list[SetupLifecycleRecord] = []
    for symbol_result in current_results:
        lifecycle = symbol_result.lifecycle_state
        if lifecycle is None:
            continue
        candidates = _signal_id_candidates(symbol_result)
        if normalized in candidates or lifecycle.lifecycle_id == normalized:
            records.append(lifecycle)
    unique: dict[str, SetupLifecycleRecord] = {}
    for record in records:
        unique.setdefault(record.lifecycle_id, record)
    return tuple(unique.values())


def _fallback_lifecycle_records_for_prior_alert(
    lifecycle_repository: SQLiteSetupLifecycleRepository,
    prior_alert: TelegramAlertAttemptRecord,
) -> tuple[SetupLifecycleRecord, ...]:
    records = lifecycle_repository.list_records_for_symbol(symbol=prior_alert.symbol)
    direction = _status_key(prior_alert.direction)
    if direction not in {"long", "short"}:
        return records
    matched = tuple(
        record
        for record in records
        if _status_key(record.direction) in {direction, ""}
    )
    return matched


def _current_result_for_lifecycle_record(
    record: SetupLifecycleRecord,
    prior_alert: TelegramAlertAttemptRecord,
    current_results: Sequence[ScannerSymbolResult],
) -> ScannerSymbolResult | None:
    exact_matches = tuple(
        symbol_result
        for symbol_result in current_results
        if symbol_result.lifecycle_state is not None
        and (
            symbol_result.lifecycle_state.lifecycle_id == record.lifecycle_id
            or record.lifecycle_id in _signal_id_candidates(symbol_result)
            or prior_alert.signal_id in _signal_id_candidates(symbol_result)
        )
    )
    if len(exact_matches) == 1:
        return exact_matches[0]
    if len(exact_matches) > 1:
        return None

    symbol_matches = [
        symbol_result
        for symbol_result in current_results
        if _symbol(symbol_result.symbol) == _symbol(record.symbol)
    ]
    prior_direction = _status_key(prior_alert.direction)
    if prior_direction in {"long", "short"}:
        symbol_matches = [
            symbol_result
            for symbol_result in symbol_matches
            if _status_key(telegram_signal_message_from_symbol(symbol_result).direction) in {prior_direction, ""}
        ]
    return symbol_matches[0] if len(symbol_matches) == 1 else None


def _sent_watchlist_reconciliation_outcome(
    prior_alert: TelegramAlertAttemptRecord,
    record: SetupLifecycleRecord,
    *,
    current_result: ScannerSymbolResult | None,
    eligibility_context: TelegramEligibilityContext,
) -> SentWatchlistReconciliationOutcome | None:
    state_key = _lifecycle_record_state_key(record)
    symbol_result = _reconciliation_symbol_result(record, prior_alert, current_result=current_result)
    terminal_alert_type = _terminal_alert_type_for_lifecycle_state(state_key)
    if terminal_alert_type is not None:
        message = _message_with_prior_public_identity(
            _telegram_signal_message_for_alert(symbol_result, terminal_alert_type, eligibility_context),
            prior_alert,
        )
        return SentWatchlistReconciliationOutcome(
            alert_type=terminal_alert_type,
            message=message,
            symbol_result=symbol_result,
        )

    failed_blockers = _lifecycle_failed_confirmation_blockers(
        symbol_result,
        confirmed_state=state_key == "confirmed",
    )
    if state_key == "confirmed":
        if failed_blockers:
            return _failed_confirmed_reconciliation_outcome(
                prior_alert,
                symbol_result,
                blockers=failed_blockers,
                eligibility_context=eligibility_context,
            )
        if current_result is None:
            return None
        return _confirmed_reconciliation_outcome(
            prior_alert,
            symbol_result,
            eligibility_context=eligibility_context,
        )

    if state_key in {"watchlist", "watchlisted", "stalking", "triggered", "rejected"} and failed_blockers:
        return _failed_confirmed_reconciliation_outcome(
            prior_alert,
            symbol_result,
            blockers=failed_blockers,
            eligibility_context=eligibility_context,
        )
    return None


def _confirmed_reconciliation_outcome(
    prior_alert: TelegramAlertAttemptRecord,
    symbol_result: ScannerSymbolResult,
    *,
    eligibility_context: TelegramEligibilityContext,
) -> SentWatchlistReconciliationOutcome:
    confirmed_message = replace(
        telegram_signal_message_from_symbol(symbol_result),
        min_rr=eligibility_context.min_rr,
    )
    blockers = _defensive_delivery_blockers(
        symbol_result,
        TelegramAlertType.SIGNAL_CONFIRMED,
        confirmed_message,
        eligibility_context,
    )
    if blockers:
        return _failed_confirmed_reconciliation_outcome(
            prior_alert,
            symbol_result,
            blockers=blockers,
            eligibility_context=eligibility_context,
        )
    message = _message_with_prior_public_identity(confirmed_message, prior_alert)
    return SentWatchlistReconciliationOutcome(
        alert_type=TelegramAlertType.SIGNAL_CONFIRMED,
        message=message,
        symbol_result=symbol_result,
    )


def _failed_confirmed_reconciliation_outcome(
    prior_alert: TelegramAlertAttemptRecord,
    symbol_result: ScannerSymbolResult,
    *,
    blockers: Sequence[str],
    eligibility_context: TelegramEligibilityContext,
) -> SentWatchlistReconciliationOutcome:
    message = replace(
        telegram_signal_message_from_symbol(symbol_result),
        min_rr=eligibility_context.min_rr,
        invalidation_reason=_failed_confirmation_reason(
            symbol_result,
            blockers,
            TelegramAlertType.NO_LONGER_TRACKING,
        ),
    )
    return SentWatchlistReconciliationOutcome(
        alert_type=TelegramAlertType.NO_LONGER_TRACKING,
        message=_message_with_prior_public_identity(message, prior_alert),
        symbol_result=symbol_result,
    )


def _reconciliation_symbol_result(
    record: SetupLifecycleRecord,
    prior_alert: TelegramAlertAttemptRecord,
    *,
    current_result: ScannerSymbolResult | None,
) -> ScannerSymbolResult:
    transition = _reconciliation_transition(record)
    if current_result is not None:
        return current_result.model_copy(
            update={
                "symbol": record.symbol,
                "lifecycle_state": record,
                "lifecycle_transition": transition,
            }
        )

    direction = _first_non_na(record.direction, prior_alert.direction)
    mode = _first_non_na(record.mode, NA)
    diagnostics = {
        "mode": mode,
        "direction": direction,
        "bias": direction,
        "failed_gate": record.failed_gate,
        "first_failed_gate": record.failed_gate,
        "action_label": record.action_label,
        "invalidation_reason": record.invalidation_reason,
        "reason": record.invalidation_reason,
        "regime_reason": record.invalidation_reason,
    }
    strategy_diagnostics = {mode: diagnostics} if mode != NA else {"lifecycle": diagnostics}
    return ScannerSymbolResult(
        symbol=record.symbol,
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        strategy_diagnostics=strategy_diagnostics,
        valid_strategy_modes=(mode,) if mode != NA else (),
        technical_score=record.quality_score if record.quality_score else NA,
        lifecycle_state=record,
        lifecycle_transition=transition,
    )


def _reconciliation_transition(record: SetupLifecycleRecord) -> SetupTransitionResult:
    return SetupTransitionResult(
        lifecycle_id=record.lifecycle_id,
        symbol=record.symbol,
        from_state=record.previous_state,
        to_state=record.current_state,
        reason=SetupTransitionReason.NO_CHANGE,
        transitioned=True,
        record=record,
    )


def _terminal_alert_type_for_lifecycle_state(state_key: str) -> TelegramAlertType | None:
    if state_key == "invalidated":
        return TelegramAlertType.INVALIDATED
    if state_key == "expired":
        return TelegramAlertType.EXPIRED
    if state_key in {
        "cooldown",
        "cooled_down",
        "no_longer_tracking",
        "removed",
        "cancelled",
        "canceled",
    }:
        return TelegramAlertType.NO_LONGER_TRACKING
    return None


def _lifecycle_failed_confirmation_blockers(
    symbol_result: ScannerSymbolResult,
    *,
    confirmed_state: bool,
) -> tuple[str, ...]:
    blockers = _failed_confirmation_core_blockers(symbol_result)
    if confirmed_state:
        return blockers

    terminal_gate_keys = (
        "regime_compatibility",
        "target_integrity",
        "rr_below_min",
        "low_rr",
        "technical",
        "technical_score",
        "scoring",
        "opportunity_score",
        "quality_gate",
        "setup_rejected",
    )
    filtered: list[str] = []
    for blocker in blockers:
        key = _status_key(blocker)
        if key.startswith(("failed_confirmation_text", "failed_confirmation_action")):
            filtered.append(blocker)
        elif key.startswith("failed_confirmation_gate") and any(token in key for token in terminal_gate_keys):
            filtered.append(blocker)
    return tuple(dict.fromkeys(filtered))


def _lifecycle_record_state_key(record: SetupLifecycleRecord) -> str:
    return _status_key(record.current_state.value)


def _blocked_delivery_detail(alert_type: TelegramAlertType) -> str:
    if alert_type == TelegramAlertType.WATCHLIST:
        return "Telegram watchlist alert blocked by public readiness guard."
    if alert_type in TERMINAL_UPDATE_ALERT_TYPES:
        return "Telegram terminal lifecycle update blocked by public alert identity guard."
    return "Telegram confirmed alert blocked by defensive eligibility guard."


def _blocked_alert_type(alert_type: TelegramAlertType, reason: str) -> str:
    digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:12]
    return f"{alert_type.value}_BLOCKED_{digest}"


def _watchlist_outcome_audit_alert_type(reason: str) -> str:
    digest = hashlib.sha256(_status_key(reason).encode("utf-8")).hexdigest()[:12]
    return f"{WATCHLIST_OUTCOME_TRACKING_ATTEMPT}_{digest}"


def _watchlist_terminal_suppression_alert_type(alert_type: TelegramAlertType) -> str:
    digest = hashlib.sha256(alert_type.value.encode("utf-8")).hexdigest()[:12]
    return f"{WATCHLIST_TERMINAL_SUPPRESSION_ATTEMPT}_{digest}"


def _selected_setup(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> Any | None:
    mode = _first_non_na(
        symbol_result.valid_strategy_modes[0] if symbol_result.valid_strategy_modes else NA,
        diagnostics.get("mode"),
        getattr(symbol_result.lifecycle_state, "mode", NA),
    )
    if mode == NA:
        return None
    result = symbol_result.strategy_results.get(str(mode))
    if result is None:
        return None
    return getattr(result, str(mode), None)


def _representative_diagnostics(symbol_result: ScannerSymbolResult) -> Mapping[str, Any]:
    for mode in symbol_result.valid_strategy_modes:
        diagnostics = symbol_result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for mode in symbol_result.rejected_strategy_modes:
        diagnostics = symbol_result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is not None:
        diagnostics = symbol_result.strategy_diagnostics.get(lifecycle.mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for diagnostics in symbol_result.strategy_diagnostics.values():
        if isinstance(diagnostics, Mapping):
            return diagnostics
    return {}


def _terminal_identity_bridge(
    repository: SQLiteTelegramAlertAttemptRepository,
    symbol_result: ScannerSymbolResult,
    alert_type: TelegramAlertType | None,
    *,
    watchlist_only: bool = False,
) -> TerminalIdentityBridge:
    if alert_type not in TERMINAL_UPDATE_ALERT_TYPES and not watchlist_only:
        return TerminalIdentityBridge(blocked_reason="terminal_update_not_terminal_state")

    message = telegram_signal_message_from_symbol(symbol_result)
    exact = repository.get_prior_public_alert(
        signal_ids=_signal_id_candidates(symbol_result),
    )
    if exact is not None and (not watchlist_only or exact.alert_type == TelegramAlertType.WATCHLIST.value):
        return TerminalIdentityBridge(prior_alert=exact)

    active = repository.list_active_prior_public_alerts(symbol=symbol_result.symbol)
    if watchlist_only:
        active = tuple(record for record in active if record.alert_type == TelegramAlertType.WATCHLIST.value)
    if not active:
        inactive = repository.list_prior_public_alerts(symbol=symbol_result.symbol)
        if watchlist_only:
            inactive = tuple(
                record for record in inactive if record.alert_type == TelegramAlertType.WATCHLIST.value
            )
        if len(inactive) == 1:
            return TerminalIdentityBridge(prior_alert=inactive[0])
        if len(inactive) > 1:
            return TerminalIdentityBridge(blocked_reason="terminal_update_identity_ambiguous")
        reason = (
            "terminal_update_not_public_tracked"
            if repository.has_prior_public_alert_for_symbol(symbol=symbol_result.symbol)
            else "terminal_update_no_prior_public_alert"
        )
        return TerminalIdentityBridge(blocked_reason=reason)

    terminal_direction = _status_key(message.direction)
    if terminal_direction in {"long", "short"}:
        direction_matches = tuple(
            record for record in active if _status_key(record.direction) == terminal_direction
        )
        if len(direction_matches) == 1:
            return TerminalIdentityBridge(prior_alert=direction_matches[0])
        if len(direction_matches) > 1:
            return TerminalIdentityBridge(blocked_reason="terminal_update_identity_ambiguous")
        return TerminalIdentityBridge(blocked_reason="terminal_update_identity_not_matched")

    if len(active) == 1:
        return TerminalIdentityBridge(prior_alert=active[0])
    return TerminalIdentityBridge(blocked_reason="terminal_update_identity_ambiguous")


def _message_with_prior_public_identity(
    message: TelegramSignalMessage,
    prior_alert: TelegramAlertAttemptRecord,
) -> TelegramSignalMessage:
    return replace(
        message,
        signal_id=prior_alert.signal_id,
        symbol=prior_alert.symbol,
        direction=prior_alert.direction,
    )


def _preferred_prior_active_record(
    records: Sequence[TelegramAlertAttemptRecord],
    signal_id_priority: Sequence[str],
) -> TelegramAlertAttemptRecord:
    priority = {signal_id: index for index, signal_id in enumerate(signal_id_priority)}
    alert_priority = {
        TelegramAlertType.WATCHLIST.value: 0,
        TelegramAlertType.SIGNAL_CONFIRMED.value: 1,
    }
    return sorted(
        records,
        key=lambda record: (
            priority.get(record.signal_id, len(priority)),
            alert_priority.get(record.alert_type, len(alert_priority)),
            record.id or 0,
        ),
    )[0]


def _unique_prior_public_records(
    records: Sequence[TelegramAlertAttemptRecord],
) -> tuple[TelegramAlertAttemptRecord, ...]:
    grouped: dict[str, list[TelegramAlertAttemptRecord]] = {}
    for record in records:
        grouped.setdefault(record.signal_id, []).append(record)
    preferred = [
        _preferred_prior_active_record(tuple(group), (signal_id,))
        for signal_id, group in grouped.items()
    ]
    return tuple(sorted(preferred, key=lambda record: record.id or 0))


def _signal_id_candidates(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    lifecycle_id = _lifecycle_signal_id(symbol_result)
    fallback_id = _fallback_signal_id(symbol_result)
    return tuple(dict.fromkeys(value for value in (lifecycle_id, fallback_id) if _text(value) != NA))


def _signal_id(symbol_result: ScannerSymbolResult) -> str:
    lifecycle_id = _lifecycle_signal_id(symbol_result)
    if lifecycle_id != NA:
        return lifecycle_id
    return _fallback_signal_id(symbol_result)


def _lifecycle_signal_id(symbol_result: ScannerSymbolResult) -> str:
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is not None and _text(lifecycle.lifecycle_id) != NA:
        return lifecycle.lifecycle_id
    return NA


def _fallback_signal_id(symbol_result: ScannerSymbolResult) -> str:
    lifecycle = symbol_result.lifecycle_state
    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    direction = _first_non_na(
        getattr(trade_idea, "direction", NA) if trade_idea is not None else NA,
        getattr(lifecycle, "direction", NA),
        _field(setup, "bias"),
        diagnostics.get("bias"),
        diagnostics.get("direction"),
    )
    mode = _first_non_na(
        symbol_result.valid_strategy_modes[0] if symbol_result.valid_strategy_modes else NA,
        symbol_result.rejected_strategy_modes[0] if symbol_result.rejected_strategy_modes else NA,
        getattr(lifecycle, "mode", NA),
        diagnostics.get("mode"),
    )
    stable_parts = (
        symbol_result.symbol,
        direction,
        mode,
        _first_non_na(
            diagnostics.get("initial_sweep_level"),
            diagnostics.get("sweep_level"),
            diagnostics.get("swing_level"),
            diagnostics.get("ltf_swing_level"),
        ),
        _first_non_na(_field(setup, "entry_low"), diagnostics.get("entry_low"), _level_field(getattr(trade_idea, "entry_zone", None), "low")),
        _first_non_na(_field(setup, "entry_high"), diagnostics.get("entry_high"), _level_field(getattr(trade_idea, "entry_zone", None), "high")),
        _first_non_na(_field(setup, "stop"), diagnostics.get("stop"), _level_field(getattr(trade_idea, "stop_loss", None), "price")),
        _first_non_na(_field(setup, "tp1"), diagnostics.get("tp1"), _take_profit(trade_idea, 1)),
        _first_non_na(_field(setup, "tp2"), diagnostics.get("tp2"), _take_profit(trade_idea, 2)),
        _first_non_na(_field(setup, "tp3"), diagnostics.get("tp3"), _take_profit(trade_idea, 3)),
        _first_non_na(getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA, _field(setup, "invalidation"), diagnostics.get("invalidation")),
        _watch_failed_gate(symbol_result, diagnostics),
        _watch_reason(symbol_result, diagnostics),
        _first_non_na(
            diagnostics.get("lifecycle_source_timestamp"),
            diagnostics.get("setup_source_timestamp"),
            diagnostics.get("signal_source_timestamp"),
        ),
    )
    digest = hashlib.sha256("|".join(_text(part) for part in stable_parts).encode("utf-8")).hexdigest()[:20]
    return f"{symbol_result.symbol}-{digest}"


def _lifecycle_state_text(symbol_result: ScannerSymbolResult) -> str:
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is None:
        return NA
    return lifecycle.current_state.value


def _transition_scan_run_id(transition: SetupTransitionResult | None) -> str | None:
    if transition is None or transition.event is None:
        return None
    return transition.event.scan_run_id


def _quality_score(symbol_result: ScannerSymbolResult) -> str:
    return _text(getattr(symbol_result.setup_quality, "quality_score", NA))


def _opportunity_score_decimal(symbol_result: ScannerSymbolResult) -> Decimal | None:
    trade_idea = symbol_result.trade_idea
    diagnostics = _representative_diagnostics(symbol_result)
    score_result = symbol_result.score_result
    return _first_decimal(
        getattr(score_result, "total_score", NA) if score_result is not None else NA,
        getattr(trade_idea, "confidence_score", NA) if trade_idea is not None else NA,
        diagnostics.get("opportunity_score"),
        diagnostics.get("total_score"),
    )


def _opportunity_score_text(symbol_result: ScannerSymbolResult) -> str:
    score = _opportunity_score_decimal(symbol_result)
    return _text(score)


def _technical_score_decimal(symbol_result: ScannerSymbolResult) -> Decimal | None:
    return _decimal_or_none(symbol_result.technical_score)


def _technical_score_text(symbol_result: ScannerSymbolResult) -> str:
    score = _technical_score_decimal(symbol_result)
    return _text(score)


def _price_level_for_alert(alert_type: TelegramAlertType, message: TelegramSignalMessage) -> str:
    if alert_type == TelegramAlertType.WATCHLIST:
        zone = _limit_zone_values(message)
        if zone is not None:
            low, high = zone
            return f"{format_telegram_price(low)}-{format_telegram_price(high)}"
        return _entry_zone_text(message.entry_low, message.entry_high)
    if alert_type == TelegramAlertType.TP1_HIT:
        return _text(message.tp1)
    if alert_type == TelegramAlertType.TP2_HIT:
        return _text(message.tp2)
    if alert_type == TelegramAlertType.TP3_HIT:
        return _text(message.tp3)
    if alert_type == TelegramAlertType.SL_HIT:
        return _text(message.stop_loss)
    if alert_type == TelegramAlertType.LIMIT_HIT:
        zone = _limit_zone_values(message)
        if zone is not None:
            low, high = zone
            return f"{_text(low)}-{_text(high)}"
        return f"{_text(message.entry_low)}-{_text(message.entry_high)}"
    return _text(message.price_level)


def _message_level_metadata(message: TelegramSignalMessage | None) -> dict[str, str]:
    if message is None:
        return {}
    return {
        "entry_low": format_telegram_price(message.entry_low),
        "entry_high": format_telegram_price(message.entry_high),
        "stop_loss": format_telegram_price(message.stop_loss),
        "tp1": format_telegram_price(message.tp1),
        "tp2": format_telegram_price(message.tp2),
        "tp3": format_telegram_price(message.tp3),
    }


def _confirmation_needed(diagnostics: Mapping[str, Any]) -> str:
    failed_gate = _text(diagnostics.get("first_failed_gate"))
    if failed_gate == "missing_confirmation_structure_shift":
        return "5m BOS/CHoCH confirmation."
    if failed_gate != NA:
        return failed_gate
    return NA


def _public_invalidation_sentence(
    *,
    direction: Any,
    stop_loss: Any,
    entry_low: Any,
    entry_high: Any,
    raw_invalidation: Any,
) -> str:
    side = _status_key(direction)
    stop = format_telegram_price(stop_loss)
    if stop == NA:
        raw = _clean_public_sentence(raw_invalidation)
        return raw if raw != NA and not _looks_like_rejection_reason(raw) else NA
    entry = _entry_zone_text(entry_low, entry_high)
    if side == "long":
        zone = " and accepts below the entry reclaim zone" if entry != NA else ""
        return (
            f"Signal invalidates if price closes below {stop}{zone}, "
            "confirming that the bullish continuation structure has failed."
        )
    if side == "short":
        zone = " and accepts above the entry rejection zone" if entry != NA else ""
        return (
            f"Signal invalidates if price closes above {stop}{zone}, "
            "confirming that the bearish continuation structure has failed."
        )
    raw = _clean_public_sentence(raw_invalidation)
    return raw if raw != NA and not _looks_like_rejection_reason(raw) else NA


def _human_confluence_sentence(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    structure = _structure_confluence_text(diagnostics)
    ob_fvg = _ob_fvg_confluence_text(diagnostics)
    volume = _volume_confluence_text(symbol_result, diagnostics)
    derivatives = _derivatives_confluence_text(symbol_result, diagnostics)
    return " ".join(part for part in (structure, ob_fvg, volume, derivatives) if part != NA)


def _structure_confluence_text(diagnostics: Mapping[str, Any]) -> str:
    confirmation = _status_key(diagnostics.get("confirmation_structure_shift_status"))
    sweep = _status_key(diagnostics.get("execution_sweep_status"))
    if confirmation == "passed":
        return "Structure is confirmed by a clean LTF BOS/CHoCH."
    if sweep == "passed":
        return "Structure has a valid sweep, but LTF confirmation context is N/A."
    return "Structure confirmation is N/A."


def _ob_fvg_confluence_text(diagnostics: Mapping[str, Any]) -> str:
    raw = _status_key(_first_non_na(diagnostics.get("selected_zone_type"), diagnostics.get("ob_fvg_status")))
    if "fvg" in raw:
        return "Price is reacting from a valid FVG reaction."
    if "ob" in raw or "order_block" in raw:
        return "Price is reacting from a valid OB reaction."
    return "OB/FVG context is N/A."


def _volume_confluence_text(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    source = _status_key(_first_non_na(diagnostics.get("volume_profile_source"), symbol_result.volume_profile_source))
    if source == "estimated_from_candles":
        return "Volume is candle-estimated."
    if source != "":
        return f"Volume context uses {_plain_label(source)} data."
    return "Volume context is N/A."


def _derivatives_confluence_text(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    funding_source = _first_context_value(
        (diagnostics.get("funding_context"), ("funding_status", "status", "direction", "funding_direction", "severity")),
        (symbol_result.funding_status, ()),
    )
    oi_source = _first_context_value(
        (diagnostics.get("oi_context"), ("oi_direction", "direction", "open_interest_direction", "trend")),
        (symbol_result.oi_direction, ()),
    )
    funding = _funding_phrase(funding_source)
    oi = _oi_phrase(oi_source)
    if funding == NA and oi == NA:
        return "Derivatives context is N/A."
    if funding != NA and oi != NA:
        prefix = (
            "Derivatives are neutral"
            if funding == "funding is normal" and oi != "open interest is rising"
            else "Derivatives context"
        )
        return f"{prefix}: {funding} while {oi}, so follow-through still needs structure to hold."
    if funding != NA:
        return f"Derivatives context: {funding}."
    return f"Derivatives context: {oi}."


def _derivatives_status(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    parts = [
        _first_non_na(diagnostics.get("funding_context"), symbol_result.funding_status),
        _first_non_na(diagnostics.get("oi_context"), symbol_result.oi_direction),
        _first_non_na(diagnostics.get("derivatives_supports_trade"), symbol_result.price_oi_relationship),
    ]
    text = " / ".join(part for part in (_text(value) for value in parts) if part != NA)
    return text if text else NA


def _funding_phrase(value: Any) -> str:
    value = _context_value(value, "funding_status", "status", "direction", "funding_direction", "severity")
    key = _status_key(value)
    if key in {"", "na", "n_a"}:
        return NA
    if "normal" in key or "neutral" in key:
        return "funding is normal"
    if "positive" in key:
        return "funding is positive"
    if "negative" in key:
        return "funding is negative"
    if "elevated" in key:
        return "funding is elevated"
    if "extreme" in key:
        return "funding is extreme"
    return f"funding is {_plain_label(key)}"


def _oi_phrase(value: Any) -> str:
    value = _context_value(value, "oi_direction", "direction", "open_interest_direction", "trend")
    key = _status_key(value)
    if key in {"", "na", "n_a"}:
        return NA
    if key in {"falling", "decreasing", "down"}:
        return "open interest is falling"
    if key in {"rising", "increasing", "up"}:
        return "open interest is rising"
    if key in {"flat", "neutral", "stable"}:
        return "open interest is stable"
    return f"open interest is {_plain_label(key)}"


def _context_value(value: Any, *keys: str) -> Any:
    if hasattr(value, "model_dump") and not isinstance(value, Mapping):
        value = value.model_dump()
    if not isinstance(value, Mapping):
        return value
    for key in keys:
        candidate = value.get(key, NA)
        if _text(candidate) != NA:
            return candidate
    return NA


def _first_context_value(*candidates: tuple[Any, tuple[str, ...]]) -> Any:
    for value, keys in candidates:
        candidate = _context_value(value, *keys)
        if _text(candidate) != NA:
            return candidate
    return NA


def _entry_zone_text(entry_low: Any, entry_high: Any) -> str:
    low = format_telegram_price(entry_low)
    high = format_telegram_price(entry_high)
    if low == NA and high == NA:
        return NA
    if low == high or high == NA:
        return low
    if low == NA:
        return high
    return f"{low}-{high}"


def _raw_invalidation_text(symbol_result: ScannerSymbolResult) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    setup = _selected_setup(symbol_result, diagnostics)
    trade_idea = symbol_result.trade_idea
    lifecycle = symbol_result.lifecycle_state
    return _first_non_na(
        getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA,
        _field(setup, "invalidation"),
        diagnostics.get("invalidation"),
        getattr(lifecycle, "invalidation_reason", NA),
    )


def _looks_like_rejection_reason(value: Any) -> bool:
    text = _text(value).lower()
    if text == NA.lower():
        return False
    return any(fragment in text for fragment in INVALIDATION_REJECTION_FRAGMENTS)


def _clean_public_sentence(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return NA
    if _looks_like_rejection_reason(text):
        return NA
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _plain_label(value: Any) -> str:
    text = _status_key(value)
    return text.replace("_", " ") if text else NA


def _take_profit(trade_idea: Any | None, target_number: int) -> Any:
    targets = getattr(trade_idea, "take_profits", ()) if trade_idea is not None else ()
    if not isinstance(targets, Sequence) or isinstance(targets, (str, bytes)):
        return NA
    index = target_number - 1
    if index >= len(targets):
        return NA
    return getattr(targets[index], "price", NA)


def _level_field(level: Any | None, name: str) -> Any:
    if level is None:
        return NA
    return getattr(level, name, NA)


def _field(source: Any | None, name: str) -> Any:
    if source is None:
        return NA
    if isinstance(source, Mapping):
        return source.get(name, NA)
    return getattr(source, name, NA)


def _first_non_na(*values: Any) -> Any:
    for value in values:
        if _text(value) != NA:
            return value
    return NA


def _sequence_or_single(value: Any) -> tuple[Any, ...]:
    if value is None or value == NA:
        return ()
    if isinstance(value, Mapping):
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(value)
    return (value,)


def _clean_watch_text(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return NA
    return text if text.endswith((".", "!", "?")) else f"{text}."


def _lower_first(value: Any) -> str:
    text = _clean_watch_text(value)
    if text == NA:
        return NA
    return text[:1].lower() + text[1:]


def _record_from_row(row: sqlite3.Row) -> TelegramAlertAttemptRecord:
    return TelegramAlertAttemptRecord(
        id=int(row["id"]),
        signal_id=row["signal_id"],
        symbol=row["symbol"],
        direction=row["direction"],
        previous_state=row["previous_state"],
        new_state=row["new_state"],
        alert_type=row["alert_type"],
        lifecycle_state=row["lifecycle_state"],
        sent_at=row["sent_at"],
        telegram_status=row["telegram_status"],
        message_hash=row["message_hash"],
        scan_run_id=row["scan_run_id"],
        attempted_alert_type=row["attempted_alert_type"],
        setup_quality_score=row["setup_quality_score"],
        rr_planned=row["rr_planned"],
        min_rr=row["min_rr"],
        opportunity_score=row["opportunity_score"],
        min_score_for_idea=row["min_score_for_idea"],
        technical_score=row["technical_score"],
        price_level=row["price_level"],
        entry_low=row["entry_low"],
        entry_high=row["entry_high"],
        stop_loss=row["stop_loss"],
        tp1=row["tp1"],
        tp2=row["tp2"],
        tp3=row["tp3"],
        blocked_reason=row["blocked_reason"],
        invalid_target_fields=row["invalid_target_fields"],
        error_message=row["error_message"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        seen_count=int(row["seen_count"]),
        last_scan_run_id=row["last_scan_run_id"],
        last_error_message=row["last_error_message"],
    )


def _first_decimal(*values: Any) -> Decimal | None:
    for value in values:
        decimal = _decimal_or_none(value)
        if decimal is not None:
            return decimal
    return None


def _decimal_or_default(value: Any, default: Decimal) -> Decimal:
    parsed = _decimal_or_none(value)
    return parsed if parsed is not None else default


def _decimal_or_none(value: Any) -> Decimal | None:
    text = _text(value)
    if text == NA:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _alert_type_text(value: TelegramAlertType | str) -> str:
    return value.value if isinstance(value, TelegramAlertType) else _text(value)


def _status_key(value: Any) -> str:
    text = _text(value)
    if text == NA:
        return ""
    key = text.lower().strip().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _identity(value: Any) -> str:
    text = _text(value)
    return text if text != NA else NA


def _symbol(value: Any) -> str:
    text = _text(value)
    return text.upper() if text != NA else NA


def _text(value: Any) -> str:
    if value is None or value == "" or value == NA:
        return NA
    if isinstance(value, Mapping):
        return NA
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return NA
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, bool):
        return NA
    text = " ".join(str(value).split())
    return text if text else NA


__all__ = [
    "SQLiteTelegramAlertAttemptRepository",
    "TelegramAlertDecision",
    "TelegramAlertType",
    "TelegramEligibilityContext",
    "TelegramLifecycleDelivery",
    "TelegramLifecycleDeliveryService",
    "TelegramLifecycleDeliverySummary",
    "telegram_alert_decision_for_symbol",
    "telegram_signal_message_from_symbol",
]
