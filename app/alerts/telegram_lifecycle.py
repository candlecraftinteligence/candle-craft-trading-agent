from __future__ import annotations

import hashlib
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.alerts.telegram_sender import TelegramSender
from app.core.config import Settings
from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import (
    TelegramAlertType,
    TelegramSignalMessage,
    format_telegram_signal_message,
)
from app.lifecycle.models import SetupLifecycleState, SetupTransitionResult
from app.lifecycle.state_machine import now_utc_iso
from app.pipeline.scanner_runner import ScannerRunResult, ScannerSymbolResult
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
    duplicate: int = 0
    ineligible: int = 0
    deliveries: tuple[TelegramLifecycleDelivery, ...] = ()


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

    def has_prior_active_alert(self, *, signal_id: str) -> bool:
        placeholders = ",".join("?" for _ in PRIOR_ACTIVE_ALERT_TYPES)
        row = self._connection.execute(
            f"""
            SELECT 1 FROM telegram_alert_attempts
            WHERE signal_id = ?
              AND alert_type IN ({placeholders})
              AND telegram_status IN ('sent', 'skipped')
            LIMIT 1
            """,
            (_identity(signal_id), *sorted(PRIOR_ACTIVE_ALERT_TYPES)),
        ).fetchone()
        return row is not None

    def insert_attempt(self, record: TelegramAlertAttemptRecord) -> bool:
        try:
            self._connection.execute(
                """
                INSERT INTO telegram_alert_attempts (
                    signal_id, symbol, direction, previous_state, new_state,
                    alert_type, lifecycle_state, sent_at, telegram_status,
                    message_hash, scan_run_id, attempted_alert_type, setup_quality_score,
                    rr_planned, min_rr, opportunity_score, min_score_for_idea,
                    technical_score, price_level, blocked_reason, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    _text(record.blocked_reason),
                    _text(record.error_message),
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

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

        with SQLiteTelegramAlertAttemptRepository(self.database_path) as repository:
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
                deliveries.append(delivery)
                if delivery.status == "duplicate":
                    duplicate += 1
                elif delivery.status == "sent":
                    sent += 1
                elif delivery.status == "failed":
                    failed += 1
                elif delivery.status == "blocked":
                    blocked += 1
                else:
                    skipped += 1

        return TelegramLifecycleDeliverySummary(
            attempted=sent + skipped + failed + blocked,
            sent=sent,
            skipped=skipped,
            failed=failed,
            blocked=blocked,
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
        decision = telegram_alert_decision_for_symbol(
            symbol_result,
            previously_active_sent=repository.has_prior_active_alert(
                signal_id=_signal_id(symbol_result),
            ),
            eligibility_context=eligibility_context,
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

        signal_id = _signal_id(symbol_result)
        if repository.has_attempt(signal_id=signal_id, alert_type=decision.alert_type):
            return TelegramLifecycleDelivery(
                symbol=symbol_result.symbol,
                signal_id=signal_id,
                alert_type=decision.alert_type.value,
                status="duplicate",
                detail="Duplicate Telegram alert prevented.",
            )

        message_text = format_telegram_signal_message(decision.alert_type, decision.message)
        message_hash = hashlib.sha256(message_text.encode("utf-8")).hexdigest()
        send_result = await self.sender.send_text(message_text)
        transition = decision.lifecycle_transition
        previous_state = transition.from_state.value if transition and transition.from_state else NA
        new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
        record = TelegramAlertAttemptRecord(
            signal_id=signal_id,
            symbol=symbol_result.symbol,
            direction=decision.message.direction,
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
            rr_planned=_text(decision.message.planned_rr),
            min_rr=_text((eligibility_context or TelegramEligibilityContext()).min_rr),
            opportunity_score=_opportunity_score_text(symbol_result),
            min_score_for_idea=_text((eligibility_context or TelegramEligibilityContext()).min_score_for_idea),
            technical_score=_technical_score_text(symbol_result),
            price_level=_price_level_for_alert(decision.alert_type, decision.message),
            blocked_reason=NA,
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


def telegram_alert_decision_for_symbol(
    symbol_result: ScannerSymbolResult,
    *,
    previously_active_sent: bool = False,
    eligibility_context: TelegramEligibilityContext | None = None,
) -> TelegramAlertDecision:
    transition = symbol_result.lifecycle_transition
    if transition is None:
        return TelegramAlertDecision(False, "missing_lifecycle_transition")
    if not transition.transitioned:
        return TelegramAlertDecision(False, "unchanged_lifecycle_state", lifecycle_transition=transition)
    alert_type = _alert_type_for_transition(symbol_result, transition)
    if alert_type is None:
        return TelegramAlertDecision(False, "lifecycle_state_not_eligible", lifecycle_transition=transition)
    if _requires_prior_active_alert(alert_type) and not previously_active_sent:
        return TelegramAlertDecision(False, "missing_prior_active_telegram_alert", lifecycle_transition=transition)

    message = telegram_signal_message_from_symbol(symbol_result)
    context = eligibility_context or TelegramEligibilityContext()
    blockers = _defensive_delivery_blockers(symbol_result, alert_type, message, context)
    if blockers:
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
                getattr(lifecycle, "invalidation_reason", NA),
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


def _alert_type_for_transition(
    symbol_result: ScannerSymbolResult,
    transition: SetupTransitionResult,
) -> TelegramAlertType | None:
    state = transition.to_state
    if state in WATCH_ALERT_STATES:
        return TelegramAlertType.WATCHLIST
    if state in SIGNAL_ALERT_STATES:
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


def _missing_required_fields(alert_type: TelegramAlertType, message: TelegramSignalMessage) -> tuple[str, ...]:
    required: list[tuple[str, Any]] = [
        ("signal_id", message.signal_id),
        ("symbol", message.symbol),
        ("direction", message.direction),
    ]
    if alert_type in {TelegramAlertType.WATCHLIST, TelegramAlertType.SIGNAL_CONFIRMED, TelegramAlertType.LIMIT_HIT}:
        required.extend(
            [
                ("entry_low", message.entry_low),
                ("entry_high", message.entry_high),
                ("stop_loss", message.stop_loss),
                ("planned_rr", message.planned_rr),
            ]
        )
    if alert_type in {TelegramAlertType.WATCHLIST, TelegramAlertType.SIGNAL_CONFIRMED, TelegramAlertType.INVALIDATED}:
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
        blockers.extend(_core_status_blockers(symbol_result))
        quality_state = _setup_quality_state_key(symbol_result)
        if quality_state in WATCHLIST_BLOCKED_QUALITY_STATE_KEYS:
            blockers.append(f"setup_quality_blocked:{quality_state}")
        if _text(symbol_result.rejection_reason) != NA:
            blockers.append("rejection_reason_present")
        if any(_text(reason) != NA for reason in symbol_result.rejection_reasons):
            blockers.append("rejection_reasons_present")
        return tuple(dict.fromkeys(blockers))

    if alert_type != TelegramAlertType.SIGNAL_CONFIRMED:
        return ()

    blockers: list[str] = []
    blockers.extend(_core_status_blockers(symbol_result))

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

    return tuple(dict.fromkeys(blockers))


def _core_status_blockers(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    blockers: list[str] = []
    for status_key in _status_keys(symbol_result):
        if status_key in CONFIRMED_REJECTED_STATUS_KEYS:
            blockers.append(f"core_status_blocked:{status_key}")
    return tuple(dict.fromkeys(blockers))


def _status_keys(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    values: list[Any] = [getattr(symbol_result.status, "value", symbol_result.status)]
    values.extend(getattr(status, "value", status) for status in symbol_result.status_history)
    return tuple(dict.fromkeys(_status_key(value) for value in values if _status_key(value)))


def _setup_quality_state_key(symbol_result: ScannerSymbolResult) -> str:
    quality_state = getattr(symbol_result.setup_quality, "quality_state", NA)
    return _status_key(getattr(quality_state, "value", quality_state))


def _persist_blocked_decision(decision: TelegramAlertDecision) -> bool:
    return decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED and decision.reason.startswith("blocked:")


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
    if repository.has_attempt(signal_id=signal_id, alert_type=blocked_alert_type):
        return TelegramLifecycleDelivery(
            symbol=symbol_result.symbol,
            signal_id=signal_id,
            alert_type=decision.alert_type.value,
            status="duplicate",
            detail="Duplicate blocked Telegram alert attempt prevented.",
            error_message=decision.reason,
        )

    transition = decision.lifecycle_transition
    previous_state = transition.from_state.value if transition and transition.from_state else NA
    new_state = transition.to_state.value if transition else _lifecycle_state_text(symbol_result)
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
        sent_at=now_utc_iso(),
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
        blocked_reason=decision.reason,
        error_message=decision.reason,
    )
    inserted = repository.insert_attempt(record)
    return TelegramLifecycleDelivery(
        symbol=symbol_result.symbol,
        signal_id=signal_id,
        alert_type=decision.alert_type.value,
        status="blocked" if inserted else "duplicate",
        detail="Telegram confirmed alert blocked by defensive eligibility guard.",
        message_hash=message_hash,
        error_message=decision.reason,
    )


def _blocked_alert_type(alert_type: TelegramAlertType, reason: str) -> str:
    digest = hashlib.sha256(reason.encode("utf-8")).hexdigest()[:12]
    return f"{alert_type.value}_BLOCKED_{digest}"


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


def _signal_id(symbol_result: ScannerSymbolResult) -> str:
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is not None and _text(lifecycle.lifecycle_id) != NA:
        return lifecycle.lifecycle_id
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
        getattr(lifecycle, "mode", NA),
        diagnostics.get("mode"),
    )
    stable_parts = (
        symbol_result.symbol,
        direction,
        mode,
        _first_non_na(_field(setup, "entry_low"), diagnostics.get("entry_low"), _level_field(getattr(trade_idea, "entry_zone", None), "low")),
        _first_non_na(_field(setup, "entry_high"), diagnostics.get("entry_high"), _level_field(getattr(trade_idea, "entry_zone", None), "high")),
        _first_non_na(_field(setup, "stop"), diagnostics.get("stop"), _level_field(getattr(trade_idea, "stop_loss", None), "price")),
        _first_non_na(_field(setup, "tp1"), diagnostics.get("tp1"), _take_profit(trade_idea, 1)),
        _first_non_na(_field(setup, "tp2"), diagnostics.get("tp2"), _take_profit(trade_idea, 2)),
        _first_non_na(_field(setup, "tp3"), diagnostics.get("tp3"), _take_profit(trade_idea, 3)),
        _first_non_na(getattr(trade_idea, "invalidation", NA) if trade_idea is not None else NA, _field(setup, "invalidation"), diagnostics.get("invalidation")),
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
    if alert_type == TelegramAlertType.TP1_HIT:
        return _text(message.tp1)
    if alert_type == TelegramAlertType.TP2_HIT:
        return _text(message.tp2)
    if alert_type == TelegramAlertType.TP3_HIT:
        return _text(message.tp3)
    if alert_type == TelegramAlertType.SL_HIT:
        return _text(message.stop_loss)
    if alert_type == TelegramAlertType.LIMIT_HIT:
        return f"{_text(message.entry_low)}-{_text(message.entry_high)}"
    return _text(message.price_level)


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
    stop = _text(stop_loss)
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
    low = _text(entry_low)
    high = _text(entry_high)
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
        blocked_reason=row["blocked_reason"],
        error_message=row["error_message"],
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
