from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.agents.trade_idea import create_trade_idea
from app.analytics.setup_quality import SetupQualityGrade, SetupQualityResult, SetupQualityState
from app.alerts.telegram_lifecycle import (
    PUBLIC_WATCHLIST_REGIME_PENDING_GATE_CODES,
    SQLiteTelegramAlertAttemptRepository,
    TelegramAlertType,
    TelegramEligibilityContext,
    TelegramLifecycleDeliveryService,
    WatchlistCandleSnapshot,
    classify_failed_gate_code,
    _public_signal_gate_result,
    _public_watchlist_candidate_from_symbol,
    _public_watchlist_gate_result,
    _signal_id,
    _stop_touched,
    _target_touched,
    telegram_alert_decision_for_symbol,
    telegram_signal_message_from_symbol,
)
from app.alerts.telegram_routing import TelegramMessageType
from app.alerts.telegram_sender import TelegramSendResult, TelegramSender
from app.core.config import Settings as AppSettings
from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import FOOTER, HEADER_PREFIX, format_telegram_signal_message
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState, SetupTransitionReason, SetupTransitionResult
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import apply_lifecycle_to_run_result
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from app.storage.models import TelegramAlertAttemptRecord
from app.telegram_admin.active_watchlists import load_active_public_signals


def run(coro):
    return asyncio.run(coro)


class FakeSender:
    def __init__(self, status: str = "sent") -> None:
        self.status = status
        self.messages: list[str] = []
        self.calls: list[dict[str, object]] = []

    async def send_text(self, text: str, **kwargs: object) -> TelegramSendResult:
        self.messages.append(text)
        self.calls.append(kwargs)
        return TelegramSendResult(status=self.status, detail=f"{self.status}.")


def Settings(*args, **kwargs):
    kwargs.setdefault("telegram_public_watchlist_terminal_updates_enabled", True)
    return AppSettings(*args, **kwargs)


def _config() -> ScannerRunConfig:
    return ScannerRunConfig(
        symbols=("BTCUSDT",),
        exchange="binance",
        account_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
    )


def _record(
    state: SetupLifecycleState,
    *,
    previous: SetupLifecycleState | None = None,
    signal_id: str = "sig-001",
) -> SetupLifecycleRecord:
    return SetupLifecycleRecord(
        lifecycle_id=signal_id,
        symbol="BTCUSDT",
        mode="swing",
        direction="long",
        current_state=state,
        previous_state=previous,
        first_seen_at="2026-06-02T00:00:00+00:00",
        last_seen_at="2026-06-02T00:00:00+00:00",
        last_transition_at="2026-06-02T00:00:00+00:00",
        failed_gate=NA,
        readiness_score=90,
        quality_score=88,
        edge_score=NA,
        regime_state=NA,
        action_label=NA,
        invalidation_reason="Invalid if price accepts below 95.",
    )


def _stored_plan_record(
    state: SetupLifecycleState,
    *,
    signal_id: str,
    direction: str = "long",
) -> SetupLifecycleRecord:
    short = direction.lower() == "short"
    return _record(state, signal_id=signal_id).model_copy(
        update={
            "direction": direction,
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "105" if short else "95",
            "tp1": "95" if short else "110",
            "tp2": "90" if short else "115",
            "tp3": "85" if short else "120",
            "rr": "3",
            "invalidation_reason": "Invalid if price accepts above 105." if short else "Invalid if price accepts below 95.",
            "invalidation_logic": "Invalid if price accepts above 105." if short else "Invalid if price accepts below 95.",
        }
    )


def _transition(
    state: SetupLifecycleState,
    *,
    previous: SetupLifecycleState | None = None,
    transitioned: bool = True,
    signal_id: str = "sig-001",
) -> SetupTransitionResult:
    record = _record(state, previous=previous, signal_id=signal_id)
    return SetupTransitionResult(
        lifecycle_id=signal_id,
        symbol="BTCUSDT",
        from_state=previous,
        to_state=state,
        reason=SetupTransitionReason.READINESS_IMPROVED,
        transitioned=transitioned,
        record=record,
    )


def _diagnostics(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "mode": "swing",
        "bias": "long",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop": Decimal("95"),
        "tp1": Decimal("110"),
        "tp2": Decimal("115"),
        "tp3": Decimal("120"),
        "rr_to_tp2": Decimal("3"),
        "invalidation": "Invalid if price accepts below 95.",
        "structure_reason": "Sweep and reclaim into valid pullback.",
        "confirmation_needed": "5m BOS/CHoCH.",
        "htf_2d_trend": "bullish",
        "selected_zone_type": "OB valid",
        "volume_profile_source": "12h",
        "funding_context": "normal",
        "oi_context": "rising",
        "derivatives_supports_trade": True,
    }
    data.update(overrides)
    return data


def _public_ready_watchlist_diagnostics(**overrides: object) -> dict[str, object]:
    data = _diagnostics(
        first_failed_gate="missing_confirmation",
        gates_passed=("sweep", "wick_reclaim", "bos_choch", "pullback_zone", "ob_fvg", "target_integrity", "rr"),
        gates_failed=("missing_confirmation",),
        execution_sweep_status="passed",
        confirmation_structure_shift_status="passed",
        regime_state="TRENDING",
        regime_compatibility_label="Supportive",
        regime_compatibility_reason="Regime supports the planned side.",
        pullback_failure_reason="Setup is structurally valid and waiting for confirmation.",
        next_required_conditions=(
            "Price must trade into the Limit Zone.",
            "Limit Zone must hold after the pullback.",
            "Confirmation must print before activation.",
        ),
    )
    data.update(overrides)
    return data


def _trade_idea(**overrides: object):
    data: dict[str, object] = {
        "symbol": "BTCUSDT",
        "exchange": "binance",
        "market_type": "perpetual",
        "direction": "long",
        "timeframe": "15m",
        "setup_type": "liquidity_grab_pullback_swing",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop_loss": Decimal("95"),
        "take_profit_targets": (Decimal("110"), Decimal("115"), Decimal("120")),
        "invalidation": "Invalid if price accepts below 95.",
        "opportunity_score": Decimal("88"),
        "opportunity_grade": "A",
        "opportunity_decision": "alert_candidate",
        "risk_approved": True,
        "best_rr": Decimal("3"),
        "technical_summary": "Sweep and reclaim into valid pullback.",
        "derivatives_summary": "Funding normal while open interest is rising.",
        "confirmed_facts": ("LTF BOS/CHoCH confirmed.",),
        "cancel_condition": "Cancel if price accepts below the reclaim zone before trigger.",
    }
    data.update(overrides)
    return create_trade_idea(data)


def _setup_quality(
    quality_state: SetupQualityState = SetupQualityState.HIGH_QUALITY_TRADE,
    *,
    quality_score: int = 88,
) -> SetupQualityResult:
    grade = SetupQualityGrade.A if quality_state != SetupQualityState.REJECTED_NO_EDGE else SetupQualityGrade.REJECT
    return SetupQualityResult(
        quality_state=quality_state,
        quality_grade=grade,
        quality_score=quality_score,
        tradeability_score=quality_score,
        profitability_edge_score=quality_score,
        execution_risk_score=max(0, 100 - quality_score),
        strongest_factors=("structure",),
        weakest_factors=(),
        decision_reason="Synthetic Phase 42B setup quality.",
        action_label="Valid setup" if quality_state in {
            SetupQualityState.HIGH_QUALITY_TRADE,
            SetupQualityState.VALID_BUT_LOWER_QUALITY,
        } else "Watch or reject",
    )


def _setup_quality_with_grade(
    grade: SetupQualityGrade,
    *,
    quality_state: SetupQualityState = SetupQualityState.HIGH_QUALITY_TRADE,
    quality_score: int = 88,
) -> SetupQualityResult:
    return _setup_quality(quality_state, quality_score=quality_score).model_copy(update={"quality_grade": grade})


def _symbol(
    state: SetupLifecycleState,
    *,
    previous: SetupLifecycleState | None = None,
    transitioned: bool = True,
    diagnostics: dict[str, object] | None = None,
    signal_id: str = "sig-001",
    trade_idea=NA,
    status: ScannerPipelineStatus | None = None,
    rejection_reason: str | None = None,
    rejection_reasons: tuple[str, ...] = (),
    technical_score: object = Decimal("70"),
    setup_quality: SetupQualityResult | None = None,
) -> ScannerSymbolResult:
    transition = _transition(state, previous=previous, transitioned=transitioned, signal_id=signal_id)
    if trade_idea == NA and state in {SetupLifecycleState.CONFIRMED, SetupLifecycleState.EXECUTING, SetupLifecycleState.MANAGING}:
        trade_idea = _trade_idea()
    return ScannerSymbolResult(
        symbol="BTCUSDT",
        status=status
        or (ScannerPipelineStatus.IDEA_CREATED
        if state not in {SetupLifecycleState.REJECTED, SetupLifecycleState.INVALIDATED}
        else ScannerPipelineStatus.SCANNED_NO_SETUP),
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        rejection_reason=rejection_reason,
        rejection_reasons=rejection_reasons,
        technical_score=technical_score,
        valid_strategy_modes=("swing",) if state != SetupLifecycleState.REJECTED else (),
        rejected_strategy_modes=("swing",) if state == SetupLifecycleState.REJECTED else (),
        strategy_diagnostics={"swing": diagnostics or _diagnostics()},
        trade_idea=None if trade_idea == NA else trade_idea,
        setup_quality=setup_quality or _setup_quality(),
        lifecycle_state=transition.record,
        lifecycle_transition=transition,
    )


def _research_settings(
    *,
    enabled: bool = True,
    to_public: bool = True,
    min_quality: int = 60,
    min_readiness: int = 50,
    cooldown_minutes: int = 1440,
    max_per_scan: int = 5,
) -> AppSettings:
    return Settings(
        _env_file=None,
        telegram_signals_enabled=True,
        telegram_research_watch_enabled=enabled,
        telegram_research_watch_to_public=to_public,
        telegram_research_min_quality=min_quality,
        telegram_research_min_readiness=min_readiness,
        telegram_research_alert_cooldown_minutes=cooldown_minutes,
        telegram_research_max_per_scan=max_per_scan,
    )


def _research_symbol(
    *,
    symbol: str = "FILUSDT",
    quality_score: int = 70,
    missing_trade_map: bool = True,
    next_trigger: str = "Wait for failed gate to clear / 5m BOS/CHoCH.",
    signal_id: str = "research-src",
) -> ScannerSymbolResult:
    diagnostics = _public_ready_watchlist_diagnostics(
        first_failed_gate="regime_compatibility",
        gates_failed=("regime_compatibility",),
        display_bucket="near_miss",
        readiness_score=55,
        next_trigger_needed=next_trigger,
        action_label="Wait for cleaner regime",
        regime_compatibility_label="Hostile",
        regime_compatibility_reason="Setup rejected by regime weakness; scalp compatibility Hostile.",
        setup_fingerprint=f"{symbol}-fingerprint",
    )
    if missing_trade_map:
        diagnostics.update(
            {
                "entry_low": NA,
                "entry_high": NA,
                "entry_zone": NA,
                "watch_zone": NA,
                "stop": NA,
                "tp1": NA,
                "tp2": NA,
                "tp3": NA,
                "rr_to_tp2": NA,
            }
        )
    base = _symbol(
        SetupLifecycleState.REJECTED,
        diagnostics=diagnostics,
        signal_id=signal_id,
        trade_idea=None,
        status=ScannerPipelineStatus.REJECTED_BY_REGIME,
        rejection_reason="Setup rejected by regime weakness; scalp compatibility Hostile; volatility/execution suitability weak.",
        rejection_reasons=("Setup rejected by regime weakness; scalp compatibility Hostile.",),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=quality_score),
    )
    return base.model_copy(
        update={
            "symbol": symbol,
            "status_history": (ScannerPipelineStatus.REJECTED_BY_REGIME,),
            "valid_strategy_modes": (),
            "rejected_strategy_modes": ("swing",),
            "lifecycle_state": None,
            "lifecycle_transition": None,
            "regime_state": "HIGH_VOLATILITY",
            "regime_confidence_score": 9,
            "regime_compatibility_label": "Hostile",
            "regime_blocked": True,
            "regime_penalty": 10,
        }
    )


def _run_result(symbol_result: ScannerSymbolResult) -> ScannerRunResult:
    return ScannerRunResult(
        config=_config(),
        results=(symbol_result,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=1,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )


def _direct_a_grade_limit_hit_symbol(
    *,
    signal_id: str = "sig-a-grade-limit",
    grade: SetupQualityGrade = SetupQualityGrade.A_PLUS,
) -> ScannerSymbolResult:
    diagnostics = _diagnostics(
        first_failed_gate="limit_zone_not_touched",
        gates_failed=("limit_zone_not_touched",),
        quality_grade=grade.value,
    )
    symbol = _symbol(
        SetupLifecycleState.EXECUTING,
        previous=SetupLifecycleState.A_GRADE_WATCH,
        diagnostics=diagnostics,
        signal_id=signal_id,
        trade_idea=None,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        setup_quality=_setup_quality_with_grade(grade, quality_score=92 if grade == SetupQualityGrade.A_PLUS else 88),
    )
    assert symbol.lifecycle_transition is not None
    transition = symbol.lifecycle_transition.model_copy(update={"reason": SetupTransitionReason.ENTRY_ZONE_TOUCHED})
    return symbol.model_copy(
        update={
            "lifecycle_transition": transition,
            "valid_strategy_modes": (),
            "rejected_strategy_modes": ("swing",),
            "status_history": (ScannerPipelineStatus.SCANNED_NO_SETUP,),
        }
    )


def _empty_run_result() -> ScannerRunResult:
    return ScannerRunResult(
        config=_config(),
        results=(),
        scanned_symbols=0,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )


def test_a_grade_watch_waiting_state_can_send_public_watchlist_when_candidate_complete() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.A_GRADE_WATCH,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(first_failed_gate="limit_zone_not_touched", gates_failed=("limit_zone_not_touched",)),
            setup_quality=_setup_quality_with_grade(SetupQualityGrade.A_PLUS, quality_score=92),
            trade_idea=None,
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        )
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST


def test_direct_a_grade_limit_hit_is_suppressed_without_public_signal(tmp_path: Path) -> None:
    db_path = tmp_path / "telegram.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(), sender=sender)
    result = _run_result(_direct_a_grade_limit_hit_symbol(signal_id="sig-limit-hit"))

    first = run(service.deliver_for_run(result, scan_run_id="run-limit-1"))
    second = run(service.deliver_for_run(result, scan_run_id="run-limit-2"))

    assert first.sent == 0
    assert second.sent == 0
    assert sender.messages == []


def test_direct_a_grade_limit_hit_does_not_use_quality_bypass() -> None:
    decision = telegram_alert_decision_for_symbol(
        _direct_a_grade_limit_hit_symbol(signal_id="sig-b-limit", grade=SetupQualityGrade.B_PLUS)
    )

    assert decision.eligible is False
    assert decision.reason == "lifecycle_state_not_eligible"


def _store_lifecycle_record(db_path: Path, record: SetupLifecycleRecord) -> None:
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)


def _with_lifecycle_direction(symbol_result: ScannerSymbolResult, direction: str) -> ScannerSymbolResult:
    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_transition is not None
    record = symbol_result.lifecycle_state.model_copy(update={"direction": direction})
    transition = symbol_result.lifecycle_transition.model_copy(update={"record": record})
    return symbol_result.model_copy(update={"lifecycle_state": record, "lifecycle_transition": transition})


def _with_lifecycle_fields(symbol_result: ScannerSymbolResult, **updates: object) -> ScannerSymbolResult:
    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_transition is not None
    record = symbol_result.lifecycle_state.model_copy(update=updates)
    transition = symbol_result.lifecycle_transition.model_copy(update={"record": record})
    return symbol_result.model_copy(update={"lifecycle_state": record, "lifecycle_transition": transition})


def _assert_target_integrity_blocked(decision, *fields: str) -> None:
    assert decision.eligible is False
    assert "target_integrity_failed" in decision.reason
    for field in fields:
        assert field in decision.reason


_DEFAULT_PLAN_LEVEL = object()


def _seed_prior_active_alert(
    db_path: Path,
    *,
    signal_id: str,
    alert_type: TelegramAlertType = TelegramAlertType.SIGNAL_CONFIRMED,
    status: str = "sent",
    symbol: str = "BTCUSDT",
    direction: str = "long",
    sent_at: str | None = None,
    price_level: object = _DEFAULT_PLAN_LEVEL,
    entry_low: object = _DEFAULT_PLAN_LEVEL,
    entry_high: object = _DEFAULT_PLAN_LEVEL,
    stop_loss: object = _DEFAULT_PLAN_LEVEL,
    tp1: object = _DEFAULT_PLAN_LEVEL,
    tp2: object = _DEFAULT_PLAN_LEVEL,
    tp3: object = _DEFAULT_PLAN_LEVEL,
) -> None:
    short = direction.lower() == "short"
    stored_entry_low = entry_low if entry_low is not _DEFAULT_PLAN_LEVEL else Decimal("100")
    stored_entry_high = entry_high if entry_high is not _DEFAULT_PLAN_LEVEL else Decimal("102")
    stored_stop_loss = stop_loss if stop_loss is not _DEFAULT_PLAN_LEVEL else Decimal("105" if short else "95")
    stored_tp1 = tp1 if tp1 is not _DEFAULT_PLAN_LEVEL else Decimal("95" if short else "110")
    stored_tp2 = tp2 if tp2 is not _DEFAULT_PLAN_LEVEL else Decimal("90" if short else "115")
    stored_tp3 = tp3 if tp3 is not _DEFAULT_PLAN_LEVEL else Decimal("85" if short else "120")
    stored_price_level = (
        price_level
        if price_level is not _DEFAULT_PLAN_LEVEL
        else f"{stored_entry_low}-{stored_entry_high}"
    )
    stored_sent_at = sent_at or datetime.now(UTC).isoformat().replace("+00:00", "Z")
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        repository.insert_attempt(
            TelegramAlertAttemptRecord(
                signal_id=signal_id,
                symbol=symbol,
                direction=direction,
                previous_state=NA,
                new_state="WATCHLISTED" if alert_type == TelegramAlertType.WATCHLIST else "CONFIRMED",
                alert_type=alert_type.value,
                lifecycle_state="WATCHLISTED" if alert_type == TelegramAlertType.WATCHLIST else "CONFIRMED",
                sent_at=stored_sent_at,
                telegram_status=status,
                message_hash=f"{signal_id}-active",
                attempted_alert_type=alert_type.value,
                setup_quality_score="B+",
                price_level=stored_price_level,
                entry_low=stored_entry_low,
                entry_high=stored_entry_high,
                stop_loss=stored_stop_loss,
                tp1=stored_tp1,
                tp2=stored_tp2,
                tp3=stored_tp3,
                first_seen_at=stored_sent_at,
            )
        )


def _telegram_attempt_rows(db_path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT signal_id, alert_type, telegram_status
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()


def _attempt_timestamp_rows(db_path: Path) -> list[tuple[str, str | None, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT telegram_status, sent_at, attempted_at
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()


def _research_attempt_rows(db_path: Path) -> list[tuple[str, str, str | None, str, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT alert_type, telegram_status, sent_at, attempted_at, blocked_reason
            FROM telegram_alert_attempts
            WHERE alert_type = 'RESEARCH_WATCH'
            ORDER BY id
            """
        ).fetchall()


def _insert_attempt_record(
    db_path: Path,
    *,
    signal_id: str,
    alert_type: str,
    status: str,
    attempted_alert_type: str | None = None,
    sent_at: str | None = "2026-06-07T00:00:00Z",
) -> None:
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        repository.insert_attempt(
            TelegramAlertAttemptRecord(
                signal_id=signal_id,
                symbol="BTCUSDT",
                direction="long",
                previous_state=NA,
                new_state="WATCHLISTED",
                alert_type=alert_type,
                lifecycle_state="WATCHLISTED",
                sent_at=sent_at,
                telegram_status=status,
                message_hash=f"hash-{signal_id}-{alert_type}",
                attempted_alert_type=attempted_alert_type or alert_type,
                setup_quality_score="B+",
                rr_planned="3",
                entry_low="100",
                entry_high="102",
                stop_loss="95",
                tp1="110",
                tp2="115",
                tp3="120",
            )
        )


def _insert_research_attempt_record(
    db_path: Path,
    *,
    symbol: str = "LINKUSDT",
    status: str = "sent",
    sent_at: str | None = "2026-06-07T00:00:00+00:00",
    signal_id: str = "research-link",
) -> None:
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        repository.insert_attempt(
            TelegramAlertAttemptRecord(
                signal_id=signal_id,
                symbol=symbol,
                direction=NA,
                previous_state=NA,
                new_state=TelegramAlertType.RESEARCH_WATCH.value,
                alert_type=TelegramAlertType.RESEARCH_WATCH.value,
                lifecycle_state=NA,
                sent_at=sent_at,
                attempted_at=sent_at or "2026-06-07T00:00:00+00:00",
                telegram_status=status,
                message_hash=f"research-hash-{signal_id}-{status}",
                attempted_alert_type=TelegramAlertType.RESEARCH_WATCH.value,
                setup_quality_score="70",
                rr_planned=NA,
                blocked_reason=NA,
                error_message=NA,
            )
        )


def test_prior_public_delivery_ignores_blocked_skipped_and_failed_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "prior-status.db"
    for status in ("blocked", "skipped", "failed"):
        _insert_attempt_record(
            db_path,
            signal_id=f"sig-{status}",
            alert_type=f"WATCHLIST_{status.upper()}",
            status=status,
            attempted_alert_type=TelegramAlertType.WATCHLIST.value,
        )

    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        for status in ("blocked", "skipped", "failed"):
            assert repository.get_prior_public_alert(signal_ids=(f"sig-{status}",)) is None
        assert repository.has_prior_public_alert_for_symbol(symbol="BTCUSDT") is False


def test_blocked_skipped_and_failed_attempt_writes_do_not_populate_sent_at(tmp_path: Path) -> None:
    db_path = tmp_path / "attempt-timestamps.db"
    for status in ("blocked", "skipped", "failed"):
        _insert_attempt_record(
            db_path,
            signal_id=f"sig-{status}",
            alert_type=f"WATCHLIST_{status.upper()}",
            status=status,
            attempted_alert_type=TelegramAlertType.WATCHLIST.value,
        )

    rows = _attempt_timestamp_rows(db_path)

    assert {row[0] for row in rows} == {"blocked", "skipped", "failed"}
    assert all(row[1] is None for row in rows)
    assert all(row[2] == "2026-06-07T00:00:00Z" for row in rows)


def test_sent_attempt_writes_delivery_sent_at(tmp_path: Path) -> None:
    db_path = tmp_path / "sent-timestamp.db"
    _insert_attempt_record(
        db_path,
        signal_id="sig-sent",
        alert_type=TelegramAlertType.WATCHLIST.value,
        status="sent",
    )

    assert _attempt_timestamp_rows(db_path) == [("sent", "2026-06-07T00:00:00Z", "2026-06-07T00:00:00Z")]


def test_research_watch_sends_enabled_regime_blocked_near_miss_without_official_alert(tmp_path: Path) -> None:
    db_path = tmp_path / "research-watch.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_research_settings(enabled=True, to_public=True),
        sender=sender,
    )

    summary = run(service.deliver_for_run(_run_result(_research_symbol()), scan_run_id="research-001"))

    assert summary.sent == 1
    assert len(sender.messages) == 1
    message = sender.messages[0]
    assert f"{HEADER_PREFIX} Research Watch — FILUSDT" in message
    assert "Quality: 70" in message
    assert "Readiness: 55" in message
    assert "Regime: High Volatility" in message
    assert "Regime fit: Hostile" in message
    assert "Confidence: 9/10" in message
    assert "Trade map:\nN/A — waiting for clean confirmation." in message
    assert "SCALP SIGNAL" not in message
    assert "WATCHLIST" not in message
    assert "CONFIRMED" not in message
    assert "LIMIT HIT" not in message
    rows = _telegram_attempt_rows(db_path)
    assert (rows[0][1], rows[0][2]) == (TelegramAlertType.RESEARCH_WATCH.value, "sent")
    assert not any(row[1] in {TelegramAlertType.WATCHLIST.value, TelegramAlertType.SIGNAL_CONFIRMED.value} for row in rows)


def test_research_watch_does_not_send_when_disabled_or_public_delivery_disabled(tmp_path: Path) -> None:
    cases = (
        ("disabled.db", _research_settings(enabled=False, to_public=True), "skipped", "research_watch_disabled"),
        (
            "public-disabled.db",
            _research_settings(enabled=True, to_public=False),
            "blocked",
            "research_watch_public_delivery_disabled",
        ),
    )

    for filename, settings, expected_status, expected_reason in cases:
        db_path = tmp_path / filename
        sender = FakeSender()
        service = TelegramLifecycleDeliveryService(database_path=db_path, settings=settings, sender=sender)

        summary = run(service.deliver_for_run(_run_result(_research_symbol()), scan_run_id=filename))

        assert getattr(summary, expected_status) == 1
        assert sender.messages == []
        rows = _research_attempt_rows(db_path)
        assert len(rows) == 1
        assert rows[0][0] == TelegramAlertType.RESEARCH_WATCH.value
        assert rows[0][1] == expected_status
        assert rows[0][2] is None
        assert rows[0][4] == expected_reason


def test_research_watch_respects_quality_and_readiness_thresholds(tmp_path: Path) -> None:
    cases = (
        ("quality.db", _research_symbol(quality_score=59), _research_settings(min_quality=60, min_readiness=0)),
        ("readiness.db", _research_symbol(), _research_settings(min_quality=0, min_readiness=56)),
    )

    for filename, symbol_result, settings in cases:
        db_path = tmp_path / filename
        sender = FakeSender()
        service = TelegramLifecycleDeliveryService(database_path=db_path, settings=settings, sender=sender)

        summary = run(service.deliver_for_run(_run_result(symbol_result), scan_run_id=filename))

        assert summary.attempted == 0
        assert sender.messages == []
        assert _research_attempt_rows(db_path) == []


def test_research_watch_duplicate_skips_inside_cooldown_and_resends_after_cooldown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "research-cooldown.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_research_settings(),
        sender=sender,
    )
    times = iter(
        (
            "2026-06-07T00:00:00+00:00",
            "2026-06-07T00:05:00+00:00",
            "2026-06-07T23:59:00+00:00",
            "2026-06-08T00:01:00+00:00",
        )
    )
    monkeypatch.setattr("app.alerts.telegram_lifecycle.now_utc_iso", lambda: next(times))
    result = _run_result(_research_symbol(symbol="LINKUSDT"))

    first = run(service.deliver_for_run(result, scan_run_id="research-1"))
    second = run(service.deliver_for_run(result, scan_run_id="research-2"))
    third = run(service.deliver_for_run(result, scan_run_id="research-3"))
    fourth = run(service.deliver_for_run(result, scan_run_id="research-4"))

    assert first.sent == 1
    assert second.skipped == 1
    assert third.skipped == 1
    assert fourth.sent == 1
    assert len(sender.messages) == 2
    rows = _research_attempt_rows(db_path)
    assert [row[1] for row in rows] == ["sent", "skipped", "sent"]
    assert rows[1][2] is None
    assert rows[1][4] == "research_watch_cooldown_active"


def test_research_watch_cooldown_uses_config_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "research-cooldown-override.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_research_settings(cooldown_minutes=10),
        sender=sender,
    )
    times = iter(
        (
            "2026-06-07T00:00:00+00:00",
            "2026-06-07T00:05:00+00:00",
            "2026-06-07T00:11:00+00:00",
        )
    )
    monkeypatch.setattr("app.alerts.telegram_lifecycle.now_utc_iso", lambda: next(times))
    result = _run_result(_research_symbol(symbol="LINKUSDT"))

    first = run(service.deliver_for_run(result, scan_run_id="research-override-1"))
    second = run(service.deliver_for_run(result, scan_run_id="research-override-2"))
    third = run(service.deliver_for_run(result, scan_run_id="research-override-3"))

    assert first.sent == 1
    assert second.skipped == 1
    assert third.sent == 1
    assert len(sender.messages) == 2


def test_research_watch_cooldown_normalizes_perp_suffix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "research-cooldown-symbol-normalized.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_research_settings(),
        sender=sender,
    )
    times = iter(("2026-06-07T00:00:00+00:00", "2026-06-07T00:05:00+00:00"))
    monkeypatch.setattr("app.alerts.telegram_lifecycle.now_utc_iso", lambda: next(times))

    first = run(service.deliver_for_run(_run_result(_research_symbol(symbol="LINKUSDT")), scan_run_id="research-link"))
    second = run(
        service.deliver_for_run(
            _run_result(_research_symbol(symbol="LINKUSDT.P")),
            scan_run_id="research-link-perp",
        )
    )

    assert first.sent == 1
    assert second.skipped == 1
    assert len(sender.messages) == 1
    rows = _research_attempt_rows(db_path)
    assert [row[1] for row in rows] == ["sent", "skipped"]
    assert rows[1][2] is None
    assert rows[1][4] == "research_watch_cooldown_active"


@pytest.mark.parametrize("status", ("blocked", "skipped", "failed"))
def test_research_watch_cooldown_ignores_unsent_rows(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
) -> None:
    db_path = tmp_path / f"research-cooldown-{status}.db"
    _insert_research_attempt_record(
        db_path,
        symbol="LINKUSDT",
        status=status,
        sent_at="2026-06-07T00:00:00+00:00",
        signal_id=f"research-link-{status}",
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_research_settings(),
        sender=sender,
    )
    monkeypatch.setattr("app.alerts.telegram_lifecycle.now_utc_iso", lambda: "2026-06-07T00:05:00+00:00")

    summary = run(service.deliver_for_run(_run_result(_research_symbol(symbol="LINKUSDT")), scan_run_id="research-after-unsent"))

    assert summary.sent == 1
    assert len(sender.messages) == 1


def test_research_watch_cooldown_only_sent_rows_suppress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "research-cooldown-sent-only.db"
    _insert_research_attempt_record(db_path, symbol="LINKUSDT", status="sent", signal_id="research-link-sent")
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_research_settings(),
        sender=sender,
    )
    monkeypatch.setattr("app.alerts.telegram_lifecycle.now_utc_iso", lambda: "2026-06-07T00:05:00+00:00")

    summary = run(service.deliver_for_run(_run_result(_research_symbol(symbol="LINKUSDT")), scan_run_id="research-after-sent"))

    assert summary.skipped == 1
    assert sender.messages == []
    rows = _research_attempt_rows(db_path)
    assert rows[-1][1] == "skipped"
    assert rows[-1][2] is None
    assert rows[-1][4] == "research_watch_cooldown_active"


def test_research_watch_cooldown_skips_do_not_consume_send_cap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_path = tmp_path / "research-cooldown-cap.db"
    _insert_research_attempt_record(db_path, symbol="LINKUSDT", status="sent", signal_id="research-link-sent")
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_research_settings(max_per_scan=1),
        sender=sender,
    )
    times = iter(("2026-06-07T00:05:00+00:00", "2026-06-07T00:05:01+00:00"))
    monkeypatch.setattr("app.alerts.telegram_lifecycle.now_utc_iso", lambda: next(times))
    result = ScannerRunResult(
        config=_config(),
        results=(
            _research_symbol(symbol="LINKUSDT", quality_score=90, signal_id="research-link"),
            _research_symbol(symbol="ETHUSDT", quality_score=80, signal_id="research-eth"),
        ),
        scanned_symbols=2,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )

    summary = run(service.deliver_for_run(result, scan_run_id="research-cap-after-cooldown"))

    assert summary.skipped == 1
    assert summary.sent == 1
    assert len(sender.messages) == 1
    assert "ETHUSDT" in sender.messages[0]
    assert "LINKUSDT" not in sender.messages[0]


def test_research_watch_sent_at_is_populated_only_for_delivery_success(tmp_path: Path) -> None:
    sent_db = tmp_path / "research-sent-at.db"
    sent_sender = FakeSender(status="sent")
    sent_service = TelegramLifecycleDeliveryService(
        database_path=sent_db,
        settings=_research_settings(),
        sender=sent_sender,
    )
    run(sent_service.deliver_for_run(_run_result(_research_symbol()), scan_run_id="sent"))
    sent_row = _research_attempt_rows(sent_db)[0]

    failed_db = tmp_path / "research-failed-at.db"
    failed_sender = FakeSender(status="failed")
    failed_service = TelegramLifecycleDeliveryService(
        database_path=failed_db,
        settings=_research_settings(),
        sender=failed_sender,
    )
    run(failed_service.deliver_for_run(_run_result(_research_symbol()), scan_run_id="failed"))
    failed_row = _research_attempt_rows(failed_db)[0]

    assert sent_row[1] == "sent"
    assert sent_row[2] == sent_row[3]
    assert failed_row[1] == "failed"
    assert failed_row[2] is None


def test_research_watch_respects_per_scan_cap_and_quality_sort(tmp_path: Path) -> None:
    db_path = tmp_path / "research-cap.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_research_settings(max_per_scan=2),
        sender=sender,
    )
    result = ScannerRunResult(
        config=_config(),
        results=(
            _research_symbol(symbol="LOWUSDT", quality_score=61, signal_id="research-low"),
            _research_symbol(symbol="HIGHUSDT", quality_score=80, signal_id="research-high"),
            _research_symbol(symbol="MIDUSDT", quality_score=70, signal_id="research-mid"),
        ),
        scanned_symbols=3,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )

    summary = run(service.deliver_for_run(result, scan_run_id="research-cap"))

    assert summary.sent == 2
    assert "HIGHUSDT" in sender.messages[0]
    assert "MIDUSDT" in sender.messages[1]
    assert all("LOWUSDT" not in message for message in sender.messages)


def test_research_watch_valid_trade_map_renders_but_remains_research_watch(tmp_path: Path) -> None:
    db_path = tmp_path / "research-valid-map.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=_research_settings(),
        sender=sender,
    )

    summary = run(
        service.deliver_for_run(
            _run_result(_research_symbol(missing_trade_map=False)),
            scan_run_id="research-map",
        )
    )

    assert summary.sent == 1
    message = sender.messages[0]
    assert f"{HEADER_PREFIX} Research Watch — FILUSDT" in message
    assert "Trade map:\nDirection: LONG" in message
    assert "Entry Zone: 100 – 102" in message
    assert "TP1: 110" in message
    assert "SCALP SIGNAL" not in message
    assert "CONFIRMED" not in message


@pytest.mark.parametrize(
    ("symbol_result", "blocked_alert_types"),
    (
        (
            _symbol(SetupLifecycleState.TP_HIT, previous=SetupLifecycleState.MANAGING, diagnostics=_diagnostics(outcome_status="tp1_hit")).model_copy(
                update={"current_price": Decimal("110")}
            ),
            {TelegramAlertType.TP1_HIT.value},
        ),
        (
            _symbol(SetupLifecycleState.SL_HIT, previous=SetupLifecycleState.MANAGING).model_copy(
                update={"current_price": Decimal("95")}
            ),
            {TelegramAlertType.SL_HIT.value},
        ),
        (
            _symbol(SetupLifecycleState.INVALIDATED, previous=SetupLifecycleState.CONFIRMED),
            {TelegramAlertType.INVALIDATED.value},
        ),
    ),
)
def test_research_watch_prior_does_not_unlock_tp_sl_or_invalidated_updates(
    tmp_path: Path,
    symbol_result: ScannerSymbolResult,
    blocked_alert_types: set[str],
) -> None:
    db_path = tmp_path / f"{next(iter(blocked_alert_types)).lower()}-research-prior.db"
    assert symbol_result.lifecycle_state is not None
    _seed_prior_active_alert(
        db_path,
        signal_id=symbol_result.lifecycle_state.lifecycle_id,
        alert_type=TelegramAlertType.RESEARCH_WATCH,
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )

    summary = run(service.deliver_for_run(_run_result(symbol_result), scan_run_id="terminal-research-prior"))

    assert summary.sent == 0
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        sent_outcomes = connection.execute(
            """
            SELECT alert_type
            FROM telegram_alert_attempts
            WHERE telegram_status = 'sent'
            """
        ).fetchall()
    assert not any(row[0] in blocked_alert_types for row in sent_outcomes)


def test_future_valid_watchlist_alert_is_not_suppressed_by_blocked_attempt(tmp_path: Path) -> None:
    db_path = tmp_path / "blocked-then-valid.db"
    signal_id = "sig-blocked-then-valid"
    _insert_attempt_record(
        db_path,
        signal_id=signal_id,
        alert_type="WATCHLIST_BLOCKED_abc123",
        status="blocked",
        attempted_alert_type=TelegramAlertType.WATCHLIST.value,
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(),
        signal_id=signal_id,
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="blocked-then-valid"))

    assert summary.sent == 1
    assert len(sender.messages) == 1
    rows = _telegram_attempt_rows(db_path)
    assert (signal_id, "WATCHLIST_BLOCKED_abc123", "blocked") in rows
    assert (signal_id, TelegramAlertType.WATCHLIST.value, "sent") in rows


def _soft_failed_confirmation_rows(db_path: Path) -> list[tuple[str, str, str, int]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT attempted_alert_type, telegram_status, blocked_reason, seen_count
            FROM telegram_alert_attempts
            WHERE attempted_alert_type = 'SOFT_FAILED_CONFIRMATION'
            ORDER BY id
            """
        ).fetchall()


def _outcome_scan_symbol(
    *,
    signal_id: str = "outcome-watch",
    symbol: str = "BTCUSDT",
    direction: str = "long",
    high: Decimal = Decimal("101"),
    low: Decimal = Decimal("99"),
    current_price: object = NA,
    price_symbol: str | None = None,
    price_stale: bool = False,
    diagnostics: dict[str, object] | None = None,
) -> ScannerSymbolResult:
    payload = dict(
        diagnostics
        or (
        _public_ready_watchlist_diagnostics()
        if direction == "long"
        else _public_ready_watchlist_diagnostics(
            bias="short",
            direction="short",
            entry_low=Decimal("100"),
            entry_high=Decimal("102"),
            stop=Decimal("105"),
            tp1=Decimal("95"),
            tp2=Decimal("90"),
            tp3=Decimal("85"),
            rr_to_tp2=Decimal("3"),
        )
    )
    )
    if current_price != NA:
        payload["current_price"] = current_price
    if price_symbol is not None:
        payload["current_price_symbol"] = price_symbol
    if price_stale:
        payload["current_price_stale"] = True
    return _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id=signal_id,
        diagnostics=payload,
    ).model_copy(
        update={
            "symbol": symbol,
            "lifecycle_state": None,
            "lifecycle_transition": None,
            "latest_high": high,
            "latest_low": low,
            "current_price": current_price,
        }
    )


def _watchlist_outcome_rows(db_path: Path) -> list[tuple[str, str, str, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT alert_type, telegram_status, attempted_alert_type, blocked_reason
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()


def _assert_transition_message_clean(message: str, *, signal_id: str, status: str) -> None:
    assert message.startswith(HEADER_PREFIX)
    assert message.endswith(FOOTER)
    assert "Signal ID:" not in message
    assert signal_id not in message
    assert "Status:" in message or status in message
    assert "Setup Type" not in message
    assert "Decimal(" not in message
    assert "{" not in message
    assert "}" not in message
    assert "\nTrue\n" not in message
    assert "\nFalse\n" not in message
    assert len([line for line in message.splitlines() if line.strip()]) <= 32


def test_confirmed_and_watchlist_lifecycle_states_are_eligible() -> None:
    confirmed = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.CONFIRMED, previous=SetupLifecycleState.TRIGGERED),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )
    watchlist = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.WATCHLISTED, diagnostics=_public_ready_watchlist_diagnostics())
    )

    assert confirmed.eligible is True
    assert confirmed.alert_type == "SIGNAL_CONFIRMED"
    assert watchlist.eligible is True
    assert watchlist.alert_type == "WATCHLIST"


def test_public_watchlist_rejects_regime_blocked_or_weak_fit() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="regime_blocked",
                gates_failed=("regime_blocked",),
                regime_state="HIGH_VOLATILITY",
                regime_compatibility_label="Weak",
                regime_compatibility_reason="Regime blocked the setup.",
                pullback_failure_reason="Regime blocked the setup.",
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_fatal_failed_gates=regime_blocked" in decision.reason


def test_regime_failed_gate_codes_are_fatal_for_public_watchlist() -> None:
    assert not PUBLIC_WATCHLIST_REGIME_PENDING_GATE_CODES
    for code in ("regime_blocked", "weak_regime_fit", "rejected_by_regime"):
        assert classify_failed_gate_code(code) == "FATAL_PUBLIC_WATCHLIST_GATE"


def test_public_watchlist_allows_exactly_timing_pending_failure() -> None:
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(
            first_failed_gate="limit_zone_hold_pending",
            gates_failed=("limit_zone_hold_pending",),
        ),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
    )
    message = telegram_signal_message_from_symbol(symbol)

    gate = _public_watchlist_gate_result(symbol, message, TelegramEligibilityContext())

    assert gate.allowed is True
    assert gate.allowed_missing_gate == "TIMING_CONFIRMATION_PENDING"
    assert gate.failed_gate_classes == ("TIMING_CONFIRMATION_PENDING",)


def test_public_watchlist_rejects_wait_for_rr_expansion() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="wait_for_rr_expansion_above_minimum",
                gates_failed=("wait_for_rr_expansion_above_minimum",),
                next_trigger_needed="Wait for RR expansion above minimum",
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_fatal_failed_gates=wait_for_rr_expansion_above_minimum" in decision.reason


def test_public_watchlist_rejects_trade_map_na() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="trade_map_na",
                gates_failed=("trade_map_na",),
                entry_low=NA,
                entry_high=NA,
                stop=NA,
                tp1=NA,
                rr_to_tp2=NA,
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_fatal_failed_gates=trade_map_na" in decision.reason
    assert "public_watchlist_missing_rr" in decision.reason
    assert "public_watchlist_missing_entry_zone" in decision.reason


def test_public_watchlist_blocks_missing_regime_data_gate_code() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="missing_regime_data",
                gates_failed=("missing_regime_data",),
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_missing_data_failed_gates=missing_regime_data" in decision.reason


def test_public_watchlist_blocks_unknown_gate_code() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="mystery_gate",
                gates_failed=("mystery_gate",),
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_unknown_failed_gates=mystery_gate" in decision.reason


def test_public_watchlist_blocks_malformed_diagnostics() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate={"bad": "gate"},
                gates_failed=({"bad": "gate"},),
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_malformed_failed_gate_diagnostics" in decision.reason


def test_public_watchlist_blocks_target_integrity_failure() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(tp1=Decimal("99")),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "target_integrity_failed" in decision.reason


def test_public_watchlist_blocks_terminal_lifecycle_states() -> None:
    for state in (
        SetupLifecycleState.INVALIDATED,
        SetupLifecycleState.EXPIRED,
        SetupLifecycleState.COOLDOWN,
        SetupLifecycleState.REJECTED,
    ):
        symbol = _symbol(state, previous=SetupLifecycleState.WATCHLISTED, diagnostics=_public_ready_watchlist_diagnostics())
        message = telegram_signal_message_from_symbol(symbol)

        gate = _public_watchlist_gate_result(symbol, message, TelegramEligibilityContext())

        assert gate.allowed is False
        assert any("public_watchlist_state_not_eligible" in reason for reason in gate.blocking_reasons)


def test_public_signal_gate_requires_confirmed_state() -> None:
    symbol = _symbol(SetupLifecycleState.TRIGGERED, previous=SetupLifecycleState.WATCHLISTED)
    message = telegram_signal_message_from_symbol(symbol)

    gate = _public_signal_gate_result(symbol, TelegramAlertType.SIGNAL_CONFIRMED, message)

    assert gate.allowed is False
    assert "public_signal_state_not_confirmed:triggered" in gate.blocking_reasons


def test_public_signal_gate_requires_core_trade_fields() -> None:
    symbol = _symbol(SetupLifecycleState.CONFIRMED, previous=SetupLifecycleState.TRIGGERED)
    message = replace(telegram_signal_message_from_symbol(symbol), tp1=NA)

    gate = _public_signal_gate_result(symbol, TelegramAlertType.SIGNAL_CONFIRMED, message)

    assert gate.allowed is False
    assert "missing_required_fields:tp1" in gate.blocking_reasons


def test_limit_hit_public_gate_requires_prior_public_emission() -> None:
    symbol = _symbol(SetupLifecycleState.MANAGING, previous=SetupLifecycleState.EXECUTING, signal_id="sig-limit-gate")
    message = telegram_signal_message_from_symbol(symbol)

    gate = _public_signal_gate_result(symbol, TelegramAlertType.LIMIT_HIT, message)

    assert gate.allowed is False
    assert "limit_hit_requires_prior_public_signal" in gate.blocking_reasons


def test_limit_hit_still_not_public_execution_eligible() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.MANAGING, previous=SetupLifecycleState.EXECUTING, signal_id="sig-limit-no-public"),
        previously_active_sent=False,
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.LIMIT_HIT
    assert "limit_hit_requires_prior_public_signal" in decision.reason


def test_watchlist_near_miss_routes_to_watchlist_not_confirmed() -> None:
    rejected = telegram_alert_decision_for_symbol(_symbol(SetupLifecycleState.REJECTED))
    no_setup = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )
    near_miss = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.STALKING,
            previous=SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )
    unchanged = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.CONFIRMED, transitioned=False)
    )

    assert rejected.eligible is False
    assert rejected.reason == "lifecycle_state_not_eligible"
    assert no_setup.eligible is False
    assert "core_status_blocked:scanned_no_setup" in no_setup.reason
    assert near_miss.eligible is True
    assert near_miss.alert_type == TelegramAlertType.WATCHLIST
    assert unchanged.eligible is False
    assert unchanged.reason == "unchanged_lifecycle_state"


def test_public_watchlist_requires_potential_rr() -> None:
    weak = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.STALKING, diagnostics=_public_ready_watchlist_diagnostics(rr_to_tp2=NA))
    )

    assert weak.eligible is False
    assert weak.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_missing_rr" in weak.reason


def test_public_watchlist_allows_rr_below_valid_min_when_potential_rr_above_public_min() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="rr_below_minimum",
                gates_failed=("rr_below_minimum",),
                rr_to_tp2=Decimal("2.6"),
            ),
            setup_quality=_setup_quality_with_grade(
                SetupQualityGrade.B_PLUS,
                quality_state=SetupQualityState.WATCHLIST_NEAR_MISS,
                quality_score=88,
            ),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert decision.message is not None
    assert decision.message.planned_rr == Decimal("2.6")


def test_public_watchlist_rejects_rr_below_public_min() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="rr_below_minimum",
                gates_failed=("rr_below_minimum",),
                rr_to_tp2=Decimal("2.49"),
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_rr_below_min:2.49<2.5" in decision.reason


def test_public_watchlist_rejects_rr_na() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="rr_below_minimum",
                gates_failed=("rr_below_minimum",),
                rr_to_tp2=NA,
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_missing_rr" in decision.reason


def test_public_watchlist_rejects_missing_entry_zone() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="rr_below_minimum",
                gates_failed=("rr_below_minimum",),
                rr_to_tp2=Decimal("2.6"),
                entry_low=NA,
                entry_high=NA,
                entry_zone=NA,
                watch_zone=NA,
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "entry_zone" in decision.reason


def test_public_watchlist_rejects_missing_invalidation() -> None:
    symbol = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="missing_stop",
                gates_failed=("missing_stop",),
                stop=NA,
                invalidation=NA,
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        ),
        invalidation_reason=NA,
        invalidation_logic=NA,
    )
    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "missing_invalidation" in decision.reason


def test_public_watchlist_allows_missing_confirmation_when_complete_plan_exists() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.STALKING,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="missing_confirmation_structure_shift",
                gates_failed=("missing_confirmation_structure_shift",),
                rr_to_tp2=Decimal("2.6"),
                confirmation_needed="Wait for 5m BOS/CHoCH before activation.",
            ),
            setup_quality=_setup_quality_with_grade(
                SetupQualityGrade.B_PLUS,
                quality_state=SetupQualityState.WATCHLIST_NEAR_MISS,
                quality_score=88,
            ),
        )
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST


def test_public_watchlist_rejects_missing_confirmation_without_trade_map() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.STALKING,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="missing_confirmation_structure_shift",
                gates_failed=("missing_confirmation_structure_shift",),
                entry_low=NA,
                entry_high=NA,
                stop=NA,
                tp1=NA,
                tp2=NA,
                tp3=NA,
                rr_to_tp2=NA,
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_missing_rr" in decision.reason
    assert "public_watchlist_missing_entry_zone" in decision.reason


def test_public_watchlist_blocks_no_ob_or_fvg_without_zone() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="no_ob_or_fvg_zone",
                gates_failed=("no_ob_or_fvg_zone",),
                entry_low=NA,
                entry_high=NA,
                stop=NA,
                tp1=NA,
                tp2=NA,
                tp3=NA,
                rr_to_tp2=NA,
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "no_ob_or_fvg_zone" in decision.reason
    assert "entry_zone" in decision.reason


def test_invalidated_transition_cannot_produce_tp2_hit_alert_type() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.INVALIDATED,
            previous=SetupLifecycleState.CONFIRMED,
            diagnostics=_diagnostics(outcome_status="tp2_hit", highest_tp_hit=2),
        ),
        previously_active_sent=True,
    )

    assert decision.alert_type == TelegramAlertType.INVALIDATED
    assert decision.alert_type != TelegramAlertType.TP2_HIT


def test_triggered_transition_cannot_produce_tp_hit_alert_type() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.TRIGGERED,
            diagnostics=_public_ready_watchlist_diagnostics(outcome_status="tp1_hit", highest_tp_hit=1),
        )
    )

    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert decision.alert_type not in {
        TelegramAlertType.TP1_HIT,
        TelegramAlertType.TP2_HIT,
        TelegramAlertType.TP3_HIT,
    }


def test_confirmed_to_stalking_transition_cannot_produce_sl_hit_alert_type() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.STALKING,
            previous=SetupLifecycleState.CONFIRMED,
            diagnostics=_public_ready_watchlist_diagnostics(outcome_status="sl_hit"),
        )
    )

    assert decision.alert_type != TelegramAlertType.SL_HIT


def test_public_watchlist_requires_b_plus_or_better() -> None:
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(),
        setup_quality=_setup_quality_with_grade(
            SetupQualityGrade.B,
            quality_state=SetupQualityState.WATCHLIST_NEAR_MISS,
            quality_score=70,
        ),
    )

    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "below_min_public_grade" in decision.reason


def test_grade_b_plus_passes_public_watchlist_gate_when_other_gates_pass() -> None:
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(),
        setup_quality=_setup_quality_with_grade(
            SetupQualityGrade.B_PLUS,
            quality_state=SetupQualityState.WATCHLIST_NEAR_MISS,
            quality_score=88,
        ),
    )

    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST


def test_grade_b_is_blocked_from_public_confirmed_signal() -> None:
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        setup_quality=_setup_quality_with_grade(SetupQualityGrade.B, quality_score=70),
        trade_idea=_trade_idea(opportunity_grade="B", opportunity_score=Decimal("88")),
    )

    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED
    assert "below_min_public_grade" in decision.reason


def test_public_confirmed_signal_allows_b_plus_and_a_family_grades() -> None:
    for grade in (
        SetupQualityGrade.B_PLUS,
        SetupQualityGrade.A_MINUS,
        SetupQualityGrade.A,
        SetupQualityGrade.A_PLUS,
    ):
        symbol = _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            setup_quality=_setup_quality_with_grade(grade),
            trade_idea=_trade_idea(opportunity_grade=grade.value, opportunity_score=Decimal("88")),
        )

        decision = telegram_alert_decision_for_symbol(symbol)

        assert decision.eligible is True
        assert decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED


def test_numeric_only_public_quality_score_maps_to_b_plus_gate() -> None:
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        setup_quality=_setup_quality_with_grade(SetupQualityGrade.NA, quality_score=76),
        trade_idea=_trade_idea(opportunity_grade="N/A", opportunity_score=Decimal("88")),
    )

    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED


def test_no_ob_fvg_watchlist_near_miss_without_plan_is_blocked() -> None:
    diagnostics = _diagnostics(
        entry_low=NA,
        entry_high=NA,
        stop=NA,
        tp1=NA,
        tp2=NA,
        tp3=NA,
        rr_to_tp2=NA,
        first_failed_gate="no_ob_or_fvg_zone",
        gates_passed=("sweep", "bos_choch"),
        gates_failed=("no_ob_or_fvg_zone",),
        execution_sweep_status="passed",
        confirmation_structure_shift_status="passed",
        pullback_failure_reason="No valid OB or FVG was found inside the 5m displacement impulse.",
        next_required_conditions=(
            "A valid OB or FVG must be found inside the displacement impulse.",
            "The OB/FVG zone must overlap the preferred fib pullback zone.",
            "RR and final quality gates must still pass after a valid zone is found.",
        ),
        mode="scalp",
    )

    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.REJECTED,
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            rejection_reason="No valid Liquidity-Grab Pullback setup.",
            rejection_reasons=("No valid Liquidity-Grab Pullback setup.",),
            diagnostics=diagnostics,
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "watchlist_missing_trackable_plan:all_plan_fields_na" in decision.reason


def test_watchlist_with_na_direction_is_blocked() -> None:
    symbol = _with_lifecycle_direction(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(bias=NA, direction=NA),
        ),
        NA,
    )

    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "watchlist_not_public_ready:missing_public_fields=direction" in decision.reason


def test_btc_bnb_style_na_heavy_watchlists_are_blocked() -> None:
    diagnostics = _public_ready_watchlist_diagnostics(
        entry_low=NA,
        entry_high=NA,
        stop=NA,
        tp1=NA,
        tp2=NA,
        tp3=NA,
        rr_to_tp2=NA,
    )

    for symbol_name in ("BTCUSDT", "BNBUSDT"):
        symbol = _symbol(
            SetupLifecycleState.WATCHLISTED,
            signal_id=f"{symbol_name.lower()}-na-watch",
            diagnostics=diagnostics,
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
        ).model_copy(update={"symbol": symbol_name})

        decision = telegram_alert_decision_for_symbol(symbol)

        assert decision.eligible is False
        assert decision.alert_type == TelegramAlertType.WATCHLIST
        assert "watchlist_missing_trackable_plan:all_plan_fields_na" in decision.reason


def test_action_watchlist_only_with_all_plan_fields_na_is_blocked() -> None:
    quality = _setup_quality(SetupQualityState.REJECTED_NO_EDGE, quality_score=41).model_copy(
        update={"action_label": "Watchlist only"}
    )

    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.REJECTED,
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            rejection_reason="No valid Liquidity-Grab Pullback setup.",
            diagnostics=_public_ready_watchlist_diagnostics(
                entry_low=NA,
                entry_high=NA,
                stop=NA,
                tp1=NA,
                tp2=NA,
                tp3=NA,
                rr_to_tp2=NA,
            ),
            setup_quality=quality,
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "watchlist_missing_trackable_plan:all_plan_fields_na" in decision.reason


def test_hype_style_public_ready_watchlist_sends_confirmation_pending_watchlist() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            signal_id="hype-watch",
            diagnostics=_public_ready_watchlist_diagnostics(
                entry_low=Decimal("71.407944"),
                entry_high=Decimal("71.675"),
                stop=Decimal("70.77363571"),
                tp1=Decimal("72.2"),
                tp2=Decimal("73.1"),
                tp3=Decimal("74.4"),
                rr_to_tp2=Decimal("2.9"),
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        ).model_copy(update={"symbol": "HYPEUSDT"})
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    text = format_telegram_signal_message(decision.alert_type, decision.message)
    assert "🐺🟠 WATCHLIST — HYPEUSDT" in text
    assert "The wolf is stalking this one." in text
    assert "Bias: LONG" in text
    assert "CONFIRMED SIGNAL" not in text
    assert "Zone: 71.41 \u2013 71.68" in text
    assert "Potential RR: 2.9R" in text
    assert "👀 What we want to see" in text
    assert "No confirmation = no trade." in text
    assert "71.407944" not in text
    assert "70.77" in text
    assert "70.77363571" not in text
    assert "WATCHLIST" in text
    assert "Invalid below/above:" in text


def _near_miss_watchlist_symbol(
    *,
    symbol: str = "ENAUSDT",
    side: str = "short",
    grade: str = "B+",
    score: int = 80,
    potential_rr: object = Decimal("2.6"),
    entry_low: object = Decimal("0.09528"),
    entry_high: object = Decimal("0.09599"),
    invalidation: object = Decimal("0.09751"),
    pending_reason: str = "confirmation_pending",
    signal_id: str = "near-miss-plan-complete",
    historical_rejection: bool = True,
) -> ScannerSymbolResult:
    short = side.lower() == "short"
    tp1 = Decimal("0.09300") if short else Decimal("0.09800")
    tp2 = Decimal("0.09150") if short else Decimal("0.10000")
    tp3 = Decimal("0.09000") if short else Decimal("0.10200")
    diagnostics = _public_ready_watchlist_diagnostics(
        bias=side,
        direction=side,
        grade=grade,
        readiness_score=score,
        potential_rr=potential_rr,
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        stop=invalidation,
        stop_loss=invalidation,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        invalidation_level=invalidation,
        first_failed_gate=pending_reason,
        pending_reason=pending_reason,
        pending_confirmation_reason=pending_reason,
        gates_failed=(pending_reason,),
        historical_rejection_reasons=("Previous scanner rejection was retained for audit.",) if historical_rejection else (),
        next_required_conditions=(
            "Price must trade into the Limit Zone.",
            "Limit Zone must hold after the pullback.",
            "Confirmation must print before activation.",
        ),
    )
    rejection_reason = "Historical research rejection retained for audit." if historical_rejection else None
    rejection_reasons = ("Historical scanner rejection retained for audit.",) if historical_rejection else ()
    if potential_rr == NA:
        diagnostics.update(
            {
                "potential_rr": NA,
                "rr": NA,
                "rr_to_tp1": NA,
                "rr_to_tp2": NA,
                "planned_rr": NA,
                "rr_planned": NA,
            }
        )
    if entry_low == NA or entry_high == NA:
        diagnostics.update(
            {
                "entry_low": NA,
                "entry_high": NA,
                "entry_zone_low": NA,
                "entry_zone_high": NA,
                "entry_zone": NA,
                "watch_zone": NA,
                "limit_zone": NA,
                "pullback_zone": NA,
                "zone": NA,
            }
        )
    if invalidation == NA:
        diagnostics.update(
            {
                "stop": NA,
                "stop_loss": NA,
                "invalidation": NA,
                "invalidation_level": NA,
                "invalid_below": NA,
                "invalid_above": NA,
                "protective_stop": NA,
                "atr_stop": NA,
            }
        )
    result = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=diagnostics,
        signal_id=signal_id,
        trade_idea=None,
        status=ScannerPipelineStatus.REJECTED_BY_SCORING,
        rejection_reason=rejection_reason,
        rejection_reasons=rejection_reasons,
        setup_quality=_setup_quality_with_grade(
            SetupQualityGrade.REJECT,
            quality_state=SetupQualityState.WATCHLIST_NEAR_MISS,
            quality_score=score,
        ),
    ).model_copy(update={"symbol": symbol})
    return _with_lifecycle_fields(
        result,
        direction=side,
        rr=NA if potential_rr == NA else str(potential_rr),
        entry_low=entry_low,
        entry_high=entry_high,
        stop_loss=invalidation,
        invalidation_reason=NA if invalidation == NA else f"Invalid if price accepts beyond {invalidation}.",
        invalidation_logic=NA if invalidation == NA else f"Invalid if price accepts beyond {invalidation}.",
    )


def test_near_miss_maps_rr_alias_into_public_watchlist_candidate() -> None:
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(
            potential_rr=NA,
            rr=NA,
            rr_to_tp1=NA,
            rr_to_tp2=NA,
            planned_rr=Decimal("2.6"),
            watchlist_grade="B+",
        ),
        trade_idea=None,
    )

    candidate = _public_watchlist_candidate_from_symbol(symbol)

    assert candidate.potential_rr == Decimal("2.6")


def test_near_miss_maps_entry_zone_alias_into_public_watchlist_candidate() -> None:
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(
            entry_low=NA,
            entry_high=NA,
            entry_zone=NA,
            watch_zone=NA,
            limit_zone=NA,
            pullback_zone_low=Decimal("99"),
            pullback_zone_high=Decimal("101"),
            watchlist_grade="B+",
        ),
        trade_idea=None,
    )

    candidate = _public_watchlist_candidate_from_symbol(symbol)

    assert candidate.entry_zone_low == Decimal("99")
    assert candidate.entry_zone_high == Decimal("101")


def test_near_miss_maps_invalidation_alias_into_public_watchlist_candidate() -> None:
    symbol = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                stop=NA,
                stop_loss=NA,
                invalidation=NA,
                invalidation_level=NA,
                protective_stop=Decimal("95"),
                watchlist_grade="B+",
            ),
            trade_idea=None,
        ),
        stop_loss=NA,
        invalidation_reason=NA,
        invalidation_logic=NA,
    )

    candidate = _public_watchlist_candidate_from_symbol(symbol)

    assert candidate.stop_loss == Decimal("95")


def test_near_miss_with_b_plus_rr_zone_stop_creates_public_watchlist_trade_idea(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / "b-plus-watch.db", settings=Settings(), sender=sender)

    summary = run(service.deliver_for_run(_run_result(_near_miss_watchlist_symbol(grade="B+")), scan_run_id="b-plus-watch"))

    assert summary.public_watchlist_audit.public_watchlist_bridge["public_watchlist_trade_ideas_created"] == 1
    assert summary.sent == 1
    assert sender.calls == [{"message_type": TelegramMessageType.PUBLIC_WATCHLIST}]


def test_near_miss_with_a_minus_rr_zone_stop_creates_public_watchlist_trade_idea(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / "a-minus-watch.db", settings=Settings(), sender=sender)

    summary = run(service.deliver_for_run(_run_result(_near_miss_watchlist_symbol(grade="A-")), scan_run_id="a-minus-watch"))

    assert summary.public_watchlist_audit.public_watchlist_bridge["public_watchlist_trade_ideas_created"] == 1
    assert summary.sent == 1


def test_near_miss_with_missing_rr_does_not_create_trade_idea(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / "missing-rr.db", settings=Settings(), sender=sender)

    summary = run(
        service.deliver_for_run(
            _run_result(_near_miss_watchlist_symbol(symbol="RENDERUSDT", grade="A-", potential_rr=NA)),
            scan_run_id="missing-rr",
        )
    )

    assert summary.sent == 0
    assert summary.public_watchlist_audit.public_watchlist_bridge["public_watchlist_trade_ideas_created"] == 0
    assert summary.public_watchlist_audit.blocked_by_reason["missing_rr"] == 1
    assert sender.messages == []


def test_near_miss_with_missing_entry_zone_does_not_create_trade_idea(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / "missing-entry.db", settings=Settings(), sender=sender)

    summary = run(
        service.deliver_for_run(
            _run_result(_near_miss_watchlist_symbol(symbol="RENDERUSDT", grade="A-", entry_low=NA, entry_high=NA)),
            scan_run_id="missing-entry",
        )
    )

    assert summary.sent == 0
    assert summary.public_watchlist_audit.blocked_by_reason["missing_entry_zone"] == 1
    assert sender.messages == []


def test_near_miss_with_missing_invalidation_does_not_create_trade_idea(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / "missing-invalidation.db", settings=Settings(), sender=sender)

    summary = run(
        service.deliver_for_run(
            _run_result(_near_miss_watchlist_symbol(symbol="RENDERUSDT", grade="A-", invalidation=NA)),
            scan_run_id="missing-invalidation",
        )
    )

    assert summary.sent == 0
    assert summary.public_watchlist_audit.blocked_by_reason["missing_invalidation"] == 1
    assert sender.messages == []


def test_historical_rejection_reasons_do_not_block_plan_complete_watchlist_candidate() -> None:
    decision = telegram_alert_decision_for_symbol(_near_miss_watchlist_symbol())

    assert decision.eligible is True
    assert "rejection_reason_present" not in decision.reason
    assert "rejection_reasons_present" not in decision.reason


def test_active_structural_breakdown_blocks_watchlist_candidate() -> None:
    symbol = _near_miss_watchlist_symbol(historical_rejection=False).model_copy(
        update={
            "strategy_diagnostics": {
                "swing": _public_ready_watchlist_diagnostics(
                    bias="short",
                    direction="short",
                    grade="B+",
                    potential_rr=Decimal("2.6"),
                    entry_zone_low=Decimal("0.09528"),
                    entry_zone_high=Decimal("0.09599"),
                    stop=Decimal("0.09751"),
                    stop_loss=Decimal("0.09751"),
                    invalidation_level=Decimal("0.09751"),
                    first_failed_gate="confirmation_pending",
                    gates_failed=("confirmation_pending",),
                    active_failed_gate="structural_breakdown",
                )
            }
        }
    )

    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is False
    assert "public_watchlist_active_rejection:structural_breakdown" in decision.reason


def test_entry_window_expired_blocks_watchlist_candidate() -> None:
    test_entry_window_expired_late_pullback_and_target_inside_chop_remain_blocked("entry_window_expired")


def test_target_inside_chop_blocks_watchlist_candidate() -> None:
    test_entry_window_expired_late_pullback_and_target_inside_chop_remain_blocked("target_inside_chop")


def test_rr_below_minimum_allowed_when_candidate_rr_above_public_min() -> None:
    decision = telegram_alert_decision_for_symbol(
        _near_miss_watchlist_symbol(pending_reason="rr_below_minimum", potential_rr=Decimal("2.6"))
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST


def test_rr_below_public_min_blocks_watchlist_candidate() -> None:
    decision = telegram_alert_decision_for_symbol(_near_miss_watchlist_symbol(potential_rr=Decimal("2.49")))

    assert decision.eligible is False
    assert "public_watchlist_rr_below_min:2.49<2.5" in decision.reason


def test_admin_draft_delivery_disabled_does_not_block_public_watchlist_trade_idea(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / "admin-disabled-watch.db", settings=Settings(), sender=sender)

    summary = run(service.deliver_for_run(_run_result(_near_miss_watchlist_symbol()), scan_run_id="admin-disabled-watch"))

    assert summary.sent == 1
    assert "WATCHLIST — ENAUSDT" in sender.messages[0]


def test_live_scanner_alerts_true_creates_public_watchlist_alert(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / "live-enabled.db", settings=Settings(), sender=sender)

    summary = run(service.deliver_for_run(_run_result(_near_miss_watchlist_symbol()), scan_run_id="live-enabled"))

    assert summary.public_watchlist_audit.public_watchlist_bridge["public_watchlist_alerts_created"] == 1
    assert summary.sent == 1


def test_live_scanner_alerts_false_does_not_send_public_watchlist(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=tmp_path / "live-disabled.db",
        settings=Settings(telegram_public_watchlist_enabled=False),
        sender=sender,
    )

    summary = run(service.deliver_for_run(_run_result(_near_miss_watchlist_symbol()), scan_run_id="live-disabled"))

    assert summary.sent == 0
    assert sender.messages == []


def test_public_watchlist_old_wolf_formatter_shape() -> None:
    decision = telegram_alert_decision_for_symbol(_near_miss_watchlist_symbol())

    assert decision.message is not None
    text = format_telegram_signal_message(decision.alert_type, decision.message)
    assert "🐺🟠 WATCHLIST — ENAUSDT" in text
    assert "The wolf is stalking this one." in text
    assert "Bias: SHORT" in text
    assert "Status: WATCHLIST" in text
    assert "Quality: B+" in text
    assert "Potential RR: 2.6R" in text
    assert "Price must trade into the Limit Zone." in text
    assert "Zone: 0.09528 – 0.09599" in text
    assert "Invalid below/above: 0.09751" in text
    assert "No confirmation = no trade." in text
    assert text.endswith(FOOTER)


def test_false_confirmed_fix_unchanged(tmp_path: Path) -> None:
    test_runtime_like_homeusdt_false_confirmed_creates_no_signal_confirmed_attempt(tmp_path)


def test_runtime_like_incomplete_render_near_miss_does_not_send_and_diagnoses_missing_fields(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / "render-incomplete.db", settings=Settings(), sender=sender)
    symbol = _near_miss_watchlist_symbol(
        symbol="RENDERUSDT",
        grade="A-",
        potential_rr=NA,
        entry_low=NA,
        entry_high=NA,
        invalidation=NA,
        signal_id="render-incomplete",
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="render-incomplete"))

    assert summary.sent == 0
    bridge = summary.public_watchlist_audit.public_watchlist_bridge
    assert bridge["near_miss_seen"] == 1
    assert bridge["near_miss_with_rr"] == 0
    assert bridge["near_miss_with_entry_zone"] == 0
    assert bridge["near_miss_with_invalidation"] == 0
    assert bridge["public_watchlist_trade_ideas_created"] == 0
    assert summary.public_watchlist_audit.blocked_by_reason["missing_rr"] == 1
    assert summary.public_watchlist_audit.blocked_by_reason["missing_entry_zone"] == 1
    assert summary.public_watchlist_audit.blocked_by_reason["missing_invalidation"] == 1
    assert sender.messages == []


def test_near_miss_with_only_missing_confirmation_can_publish_as_watchlist() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.STALKING,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="missing_confirmation_structure_shift",
                gates_failed=("missing_confirmation_structure_shift",),
                confirmation_bos_choch_reason="bullish BOS/CHoCH confirmed by candle close above a previous LTF swing high.",
            ),
        )
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST


def test_near_miss_below_rr_is_rejected_from_public_watchlist() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="missing_confirmation",
                gates_failed=("missing_confirmation",),
                rr_to_tp2=Decimal("1.99"),
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_rr_below_min:1.99<2.5" in decision.reason


def test_near_miss_missing_stop_is_rejected_from_public_watchlist() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="missing_stop",
                gates_failed=("missing_stop",),
                stop=NA,
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "public_watchlist_fatal_failed_gates=missing_stop" in decision.reason


def test_public_watchlist_allows_rr_exactly_2_5() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="missing_confirmation",
                gates_failed=("missing_confirmation",),
                rr_to_tp2=Decimal("2.5"),
            ),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert decision.message is not None
    assert decision.message.planned_rr == Decimal("2.5")


def test_public_watchlist_uses_candidate_grade_not_final_reject_grade() -> None:
    quality = _setup_quality_with_grade(
        SetupQualityGrade.REJECT,
        quality_state=SetupQualityState.REJECTED_NO_EDGE,
        quality_score=40,
    )
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B+"),
        status=ScannerPipelineStatus.REJECTED_BY_SCORING,
        setup_quality=quality,
    )

    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert decision.message is not None
    assert decision.message.quality == "B+"


def test_public_watchlist_uses_candidate_rr_zone_stop_fields_for_data_health() -> None:
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(
            watchlist_grade="B+",
            potential_rr=Decimal("2.7"),
            entry_zone_low=Decimal("100"),
            entry_zone_high=Decimal("102"),
            stop_loss=Decimal("95"),
        ),
        setup_quality=_setup_quality_with_grade(
            SetupQualityGrade.REJECT,
            quality_state=SetupQualityState.REJECTED_NO_EDGE,
            quality_score=40,
        ),
    ).model_copy(
        update={
            "status": ScannerPipelineStatus.REJECTED_BY_SCORING,
            "missing_data": ("generic_market_context",),
            "unverified_data": ("generic_derivatives_context",),
            "strategy_missing_data": ("strat_optional_context",),
        }
    )

    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is True
    assert "data_health" not in decision.reason


def test_public_watchlist_not_missing_data_when_required_candidate_fields_exist() -> None:
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B+", rr_to_tp2=Decimal("2.6")),
    ).model_copy(update={"missing_data": ("current_price: N/A",)})

    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is True
    assert "missing_data" not in decision.reason


def test_scanned_no_setup_without_candidate_does_not_create_watchlist_attempt(tmp_path: Path) -> None:
    db_path = tmp_path / "no-candidate.db"
    symbol = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            diagnostics=_public_ready_watchlist_diagnostics(
                entry_low=NA,
                entry_high=NA,
                entry_zone=NA,
                watch_zone=NA,
                rr_to_tp2=NA,
                stop=NA,
                invalidation=NA,
            ),
        ),
        entry_low=NA,
        entry_high=NA,
        stop_loss=NA,
        invalidation_reason=NA,
        invalidation_logic=NA,
    )
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(), sender=FakeSender())

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="no-candidate"))

    assert summary.sent == 0
    assert summary.public_watchlist_audit.blocked_before_attempt == 1
    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts WHERE attempted_alert_type = 'WATCHLIST'"
        ).fetchone()[0]
    assert count == 0


def test_rejected_by_scoring_with_complete_b_plus_candidate_can_be_watchlist() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B+", rr_to_tp2=Decimal("2.8")),
            status=ScannerPipelineStatus.REJECTED_BY_SCORING,
            setup_quality=_setup_quality_with_grade(
                SetupQualityGrade.REJECT,
                quality_state=SetupQualityState.REJECTED_NO_EDGE,
                quality_score=40,
            ),
        )
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST


def test_rejected_by_scoring_without_complete_candidate_is_blocked_before_attempt(tmp_path: Path) -> None:
    db_path = tmp_path / "rejected-incomplete.db"
    symbol = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(entry_low=NA, entry_high=NA, watch_zone=NA),
            status=ScannerPipelineStatus.REJECTED_BY_SCORING,
        ),
        entry_low=NA,
        entry_high=NA,
    )
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(), sender=FakeSender())

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="rejected-incomplete"))

    assert summary.public_watchlist_audit.blocked_before_attempt == 1
    with sqlite3.connect(db_path) as connection:
        count = connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts WHERE attempted_alert_type = 'WATCHLIST'"
        ).fetchone()[0]
    assert count == 0


def test_first_seen_triggered_b_plus_complete_plan_sends_pre_confirmation_watchlist(tmp_path: Path) -> None:
    db_path = tmp_path / "first-seen-triggered.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(), sender=sender)
    symbol = _symbol(
        SetupLifecycleState.TRIGGERED,
        previous=SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(
            watchlist_grade="B+",
            first_failed_gate="missing_confirmation",
            gates_failed=("missing_confirmation",),
            rr_to_tp2=Decimal("2.81"),
        ),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="first-seen-triggered"))

    assert summary.sent == 1
    assert summary.public_watchlist_audit.eligible_first_seen_triggered_pre_confirmation == 1
    assert "Status: LIMIT ZONE HIT — WAITING CONFIRMATION" in sender.messages[0]
    assert "No confirmation = no trade." in sender.messages[0]
    assert "CONFIRMED SIGNAL" not in sender.messages[0]


def test_first_seen_triggered_reject_grade_does_not_send_watchlist(tmp_path: Path) -> None:
    db_path = tmp_path / "first-seen-reject.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(), sender=sender)
    symbol = _symbol(
        SetupLifecycleState.TRIGGERED,
        diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="Reject"),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="first-seen-reject"))

    assert summary.sent == 0
    assert sender.messages == []


def test_first_seen_triggered_missing_zone_does_not_send_watchlist(tmp_path: Path) -> None:
    db_path = tmp_path / "first-seen-missing-zone.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(), sender=sender)
    symbol = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.TRIGGERED,
            diagnostics=_public_ready_watchlist_diagnostics(entry_low=NA, entry_high=NA, watch_zone=NA),
        ),
        entry_low=NA,
        entry_high=NA,
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="first-seen-missing-zone"))

    assert summary.sent == 0
    assert summary.public_watchlist_audit.blocked_before_attempt == 1
    assert sender.messages == []


def test_first_seen_triggered_missing_stop_does_not_send_watchlist(tmp_path: Path) -> None:
    db_path = tmp_path / "first-seen-missing-stop.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(), sender=sender)
    symbol = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.TRIGGERED,
            diagnostics=_public_ready_watchlist_diagnostics(stop=NA, invalidation=NA),
        ),
        stop_loss=NA,
        invalidation_reason=NA,
        invalidation_logic=NA,
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="first-seen-missing-stop"))

    assert summary.sent == 0
    assert summary.public_watchlist_audit.blocked_before_attempt == 1
    assert sender.messages == []


def test_triggered_confirmed_or_executing_not_sent_as_normal_watchlist() -> None:
    triggered = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.TRIGGERED,
            diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B+"),
        )
    )
    confirmed = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B+"),
        )
    )
    executing = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.EXECUTING,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B+"),
        )
    )

    assert triggered.alert_type == TelegramAlertType.WATCHLIST
    assert triggered.message is not None
    assert triggered.message.watchlist_status == "LIMIT_ZONE_HIT_WAITING_CONFIRMATION"
    assert confirmed.alert_type == TelegramAlertType.SIGNAL_CONFIRMED
    assert executing.alert_type != TelegramAlertType.WATCHLIST


def test_confirmed_not_sent_as_watchlist() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_public_ready_watchlist_diagnostics(action_label="Watchlist only"),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    assert decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED
    assert decision.alert_type != TelegramAlertType.WATCHLIST


def test_confirmed_signal_route_not_dependent_on_prior_watchlist_alert(tmp_path: Path) -> None:
    db_path = tmp_path / "confirmed-no-prior-watch.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(), sender=sender)
    symbol = _symbol(SetupLifecycleState.CONFIRMED, previous=SetupLifecycleState.TRIGGERED)

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="confirmed-no-prior-watch"))

    assert summary.sent == 1
    assert "CONFIRMED" in sender.messages[0]
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT attempted_alert_type FROM telegram_alert_attempts WHERE telegram_status = 'sent'"
        ).fetchone()
    assert row == (TelegramAlertType.SIGNAL_CONFIRMED.value,)


def test_confirmed_signal_creates_send_attempt_when_valid_and_telegram_enabled(tmp_path: Path) -> None:
    db_path = tmp_path / "confirmed-stale-rejection-send.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(),
        sender=sender,
        min_rr=Decimal("2.5"),
    )
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id="dydx-confirmed",
        rejection_reason="Opportunity score was below scanner minimum on a previous scan.",
        diagnostics=_diagnostics(rr_to_tp2=Decimal("2.91")),
        trade_idea=_trade_idea(symbol="DYDXUSDT", best_rr=Decimal("2.91")),
    ).model_copy(update={"symbol": "DYDXUSDT"})

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="confirmed-stale-rejection"))

    assert summary.sent == 1
    assert "CONFIRMED" in sender.messages[0]
    assert "DYDXUSDT" in sender.messages[0]
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT symbol, attempted_alert_type, telegram_status, blocked_reason, error_message
            FROM telegram_alert_attempts
            WHERE signal_id = ?
            """,
            ("dydx-confirmed",),
        ).fetchone()
    assert row == ("DYDXUSDT", TelegramAlertType.SIGNAL_CONFIRMED.value, "sent", "N/A", "N/A")


def _attempt_count(db_path: Path, attempted_alert_type: str = TelegramAlertType.SIGNAL_CONFIRMED.value) -> int:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts WHERE attempted_alert_type = ?",
            (attempted_alert_type,),
        ).fetchone()[0]


def test_signal_confirmed_attempt_not_created_for_rejected_by_scoring(tmp_path: Path) -> None:
    db_path = tmp_path / "rejected-confirmed.db"
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(),
        sender=FakeSender(),
        min_score_for_idea=Decimal("80"),
    )
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        status=ScannerPipelineStatus.REJECTED_BY_SCORING,
        signal_id="rejected-confirmed",
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="rejected-confirmed"))

    assert summary.sent == 0
    assert summary.blocked == 0
    assert _attempt_count(db_path) == 0
    assert summary.confirmed_alert_audit.blocked_before_attempt_by_reason["rejected_by_scoring"] == 1


def test_signal_confirmed_attempt_not_created_for_trade_idea_missing(tmp_path: Path) -> None:
    db_path = tmp_path / "missing-trade-idea.db"
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(),
        sender=FakeSender(),
        min_score_for_idea=Decimal("80"),
    )
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        trade_idea=None,
        signal_id="missing-trade-idea",
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="missing-trade-idea"))

    assert summary.sent == 0
    assert summary.blocked == 0
    assert _attempt_count(db_path) == 0
    assert summary.confirmed_alert_audit.blocked_before_attempt_by_reason["trade_idea_missing"] == 1


def test_signal_confirmed_attempt_not_created_for_watchlist_near_miss(tmp_path: Path) -> None:
    db_path = tmp_path / "near-miss-confirmed.db"
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(),
        sender=FakeSender(),
        min_score_for_idea=Decimal("80"),
    )
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        signal_id="near-miss-confirmed",
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="near-miss-confirmed"))

    assert summary.sent == 0
    assert summary.blocked == 0
    assert _attempt_count(db_path) == 0
    assert summary.confirmed_alert_audit.blocked_before_attempt_by_reason["watchlist_near_miss_not_confirmed"] == 1


def test_signal_confirmed_attempt_created_for_true_confirmed_candidate(tmp_path: Path) -> None:
    db_path = tmp_path / "true-confirmed.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(),
        sender=sender,
        min_score_for_idea=Decimal("80"),
    )
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        diagnostics=_diagnostics(rr_to_tp2=Decimal("3.1")),
        trade_idea=_trade_idea(best_rr=Decimal("3.1"), opportunity_score=Decimal("88")),
        signal_id="true-confirmed",
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="true-confirmed"))

    assert summary.sent == 1
    assert summary.confirmed_alert_audit.confirmed_candidates_seen == 1
    assert summary.confirmed_alert_audit.confirmed_prefilter_passed == 1
    assert summary.confirmed_alert_audit.signal_confirmed_attempts_created == 1
    assert summary.confirmed_alert_audit.signal_confirmed_sent == 1
    assert _attempt_count(db_path) == 1
    assert "CONFIRMED" in sender.messages[0]


def test_true_confirmed_candidate_not_blocked_by_historical_rejection_reason(tmp_path: Path) -> None:
    db_path = tmp_path / "historical-rejection-confirmed.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(),
        sender=sender,
        min_score_for_idea=Decimal("80"),
    )
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        rejection_reason="Technical score was below 50 on a previous scan.",
        diagnostics=_diagnostics(rr_to_tp2=Decimal("3.1")),
        trade_idea=_trade_idea(best_rr=Decimal("3.1"), opportunity_score=Decimal("88")),
        signal_id="historical-rejection-confirmed",
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="historical-rejection-confirmed"))

    assert summary.sent == 1
    assert summary.confirmed_alert_audit.confirmed_prefilter_passed == 1
    assert summary.confirmed_alert_audit.blocked_before_attempt_by_reason == {}
    assert _attempt_count(db_path) == 1


def test_true_confirmed_candidate_still_blocked_by_active_rejection_reason(tmp_path: Path) -> None:
    db_path = tmp_path / "active-rejection-confirmed.db"
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(),
        sender=FakeSender(),
        min_score_for_idea=Decimal("80"),
    )
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        diagnostics=_diagnostics(active_rejection_reason="technical_score_is_below_50", rr_to_tp2=Decimal("3.1")),
        trade_idea=_trade_idea(best_rr=Decimal("3.1"), opportunity_score=Decimal("88")),
        signal_id="active-rejection-confirmed",
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="active-rejection-confirmed"))

    assert summary.sent == 0
    assert summary.blocked == 0
    assert _attempt_count(db_path) == 0
    assert summary.confirmed_alert_audit.blocked_before_attempt_by_reason["active_rejection_reason"] == 1


def test_runtime_like_homeusdt_false_confirmed_creates_no_signal_confirmed_attempt(tmp_path: Path) -> None:
    db_path = tmp_path / "homeusdt-false-confirmed.db"
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(),
        sender=FakeSender(),
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )
    rejection_text = "Technical score is below 50.; Opportunity score 76 is below scanner minimum 80."
    symbol = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(
                active_failed_gate="scoring",
                active_rejection_reason="technical_score_is_below_50",
                active_invalidation_reason=rejection_text,
                first_failed_gate="scoring",
                rr_to_tp2=Decimal("2.91"),
                opportunity_score=Decimal("76"),
                invalidation=rejection_text,
            ),
            status=ScannerPipelineStatus.REJECTED_BY_SCORING,
            trade_idea=None,
            technical_score=Decimal("30"),
            setup_quality=_setup_quality_with_grade(
                SetupQualityGrade.B,
                quality_state=SetupQualityState.WATCHLIST_NEAR_MISS,
                quality_score=76,
            ),
            signal_id="homeusdt-false-confirmed",
        ),
        rr="2.91",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="117",
        tp3="124",
        invalidation_reason=rejection_text,
    ).model_copy(update={"symbol": "HOMEUSDT"})

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="homeusdt-runtime"))

    assert summary.sent == 0
    assert summary.blocked == 0
    assert _attempt_count(db_path) == 0
    reasons = summary.confirmed_alert_audit.blocked_before_attempt_by_reason
    assert reasons["rejected_by_scoring"] == 1
    assert reasons["failed_confirmation_gate_scoring"] == 1
    assert reasons["trade_idea_missing"] == 1


def test_order_execution_not_called_for_confirmed_signal(tmp_path: Path) -> None:
    test_confirmed_signal_does_not_call_order_execution(tmp_path)


def test_confirmed_signal_does_not_call_order_execution(tmp_path: Path) -> None:
    db_path = tmp_path / "confirmed-no-orders.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None, order_execution_enabled=False),
        sender=sender,
    )
    symbol = _symbol(SetupLifecycleState.CONFIRMED, previous=SetupLifecycleState.TRIGGERED, signal_id="confirmed-no-orders")

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="confirmed-no-orders"))

    assert summary.sent == 1
    assert sender.calls == [{"message_type": TelegramMessageType.PUBLIC_SIGNAL}]
    source = Path("app/alerts/telegram_lifecycle.py").read_text(encoding="utf-8").lower()
    for forbidden in ("execute_order", "place_order", "create_order"):
        assert forbidden not in source


def test_watch_state_b_plus_complete_plan_sends_old_watchlist(tmp_path: Path) -> None:
    db_path = tmp_path / "watch-old-card.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(), sender=sender)
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B+", rr_to_tp2=Decimal("2.6")),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="watch-old-card"))

    assert summary.sent == 1
    assert "Status: WATCHLIST" in sender.messages[0]
    assert "Price must trade into the Limit Zone." in sender.messages[0]
    assert "LIMIT ZONE HIT" not in sender.messages[0]


def test_stalking_state_b_plus_complete_plan_sends_old_watchlist(tmp_path: Path) -> None:
    db_path = tmp_path / "stalking-old-card.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(), sender=sender)
    symbol = _symbol(
        SetupLifecycleState.STALKING,
        previous=SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B+", rr_to_tp2=Decimal("2.6")),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="stalking-old-card"))

    assert summary.sent == 1
    assert "Status: WATCHLIST" in sender.messages[0]
    assert "We let the market come to us." in sender.messages[0]


def test_grade_b_remains_blocked() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B"),
        )
    )

    assert decision.eligible is False
    assert "below_min_public_grade" in decision.reason


def test_rr_below_2_5_remains_blocked() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B+", rr_to_tp2=Decimal("2.49")),
        )
    )

    assert decision.eligible is False
    assert "public_watchlist_rr_below_min:2.49<2.5" in decision.reason


@pytest.mark.parametrize("failed_gate", ("entry_window_expired", "late_pullback", "target_inside_chop"))
def test_entry_window_expired_late_pullback_and_target_inside_chop_remain_blocked(failed_gate: str) -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(
                watchlist_grade="B+",
                first_failed_gate=failed_gate,
                gates_failed=(failed_gate,),
            ),
        )
    )

    assert decision.eligible is False
    assert failed_gate in decision.reason


def test_entry_window_expired_remains_blocked() -> None:
    test_entry_window_expired_late_pullback_and_target_inside_chop_remain_blocked("entry_window_expired")


def test_late_pullback_remains_blocked() -> None:
    test_entry_window_expired_late_pullback_and_target_inside_chop_remain_blocked("late_pullback")


def test_target_inside_chop_remains_blocked() -> None:
    test_entry_window_expired_late_pullback_and_target_inside_chop_remain_blocked("target_inside_chop")


def test_research_watch_still_blocked_from_public(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=tmp_path / "research-not-public.db",
        settings=_research_settings(enabled=True, to_public=True),
        sender=sender,
    )

    summary = run(service.deliver_for_run(_run_result(_research_symbol(missing_trade_map=False)), scan_run_id="research"))

    assert summary.sent == 1
    assert sender.calls[0]["message_type"] == TelegramMessageType.RESEARCH_WATCH
    assert "WATCHLIST —" not in sender.messages[0]


def test_public_watchlist_rules_unchanged() -> None:
    below_grade = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B", rr_to_tp2=Decimal("2.6")),
        )
    )
    below_rr = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B+", rr_to_tp2=Decimal("2.49")),
        )
    )
    valid = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(watchlist_grade="B+", rr_to_tp2=Decimal("2.5")),
        )
    )

    assert below_grade.eligible is False
    assert "below_min_public_grade" in below_grade.reason
    assert below_rr.eligible is False
    assert "public_watchlist_rr_below_min:2.49<2.5" in below_rr.reason
    assert valid.eligible is True
    assert valid.alert_type == TelegramAlertType.WATCHLIST


def test_confirmed_signal_gates_unchanged() -> None:
    test_confirmed_signal_gates_are_unchanged()


def test_order_execution_not_called_for_public_watchlist(tmp_path: Path) -> None:
    test_public_watchlist_does_not_call_order_execution(tmp_path)


def test_rejected_action_watchlist_only_is_not_public_below_min_grade() -> None:
    quality = _setup_quality(SetupQualityState.REJECTED_NO_EDGE, quality_score=41).model_copy(
        update={"action_label": "Watchlist only"}
    )

    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.REJECTED,
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            rejection_reason="No valid Liquidity-Grab Pullback setup.",
            diagnostics=_diagnostics(first_failed_gate="no_ob_or_fvg_zone", rr_to_tp2=NA),
            setup_quality=quality,
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert "below_min_public_grade" in decision.reason


def test_fallback_signal_id_is_stable_for_same_watchlist_candidate() -> None:
    diagnostics = _diagnostics(
        entry_low=NA,
        entry_high=NA,
        stop=NA,
        tp1=NA,
        tp2=NA,
        tp3=NA,
        rr_to_tp2=NA,
        first_failed_gate="no_ob_or_fvg_zone",
        pullback_failure_reason="No valid OB or FVG was found inside the 5m displacement impulse.",
        mode="scalp",
        sweep_level=Decimal("0.16406"),
    )
    first = _symbol(
        SetupLifecycleState.REJECTED,
        diagnostics=diagnostics,
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
    ).model_copy(update={"lifecycle_state": None, "lifecycle_transition": None, "current_price": Decimal("0.17000")})
    repeated = first.model_copy(update={"current_price": Decimal("0.17123")})

    assert _signal_id(first) == _signal_id(repeated)


def test_updates_require_prior_active_telegram_alert() -> None:
    invalidated_without_prior = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.INVALIDATED, previous=SetupLifecycleState.CONFIRMED),
        previously_active_sent=False,
    )
    invalidated_with_prior = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.INVALIDATED, previous=SetupLifecycleState.CONFIRMED),
        previously_active_sent=True,
    )
    expired_with_prior = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.EXPIRED, previous=SetupLifecycleState.STALKING),
        previously_active_sent=True,
    )
    cooldown_with_prior = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.COOLDOWN, previous=SetupLifecycleState.TRIGGERED),
        previously_active_sent=True,
    )
    tp2 = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.TP_HIT,
            previous=SetupLifecycleState.MANAGING,
            diagnostics=_diagnostics(outcome_status="tp2_hit"),
        ),
        previously_active_sent=True,
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert invalidated_without_prior.eligible is False
    assert invalidated_without_prior.reason == "terminal_update_no_prior_public_alert"
    assert invalidated_with_prior.eligible is True
    assert invalidated_with_prior.alert_type == "INVALIDATED"
    assert expired_with_prior.eligible is True
    assert expired_with_prior.alert_type == "EXPIRED"
    assert cooldown_with_prior.eligible is True
    assert cooldown_with_prior.alert_type == "NO_LONGER_TRACKING"
    assert tp2.eligible is True
    assert tp2.alert_type == "TP2_HIT"


def test_duplicate_confirmed_signal_is_not_sent_twice(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    result = _run_result(_symbol(SetupLifecycleState.CONFIRMED, previous=SetupLifecycleState.TRIGGERED))

    first = run(service.deliver_for_run(result, scan_run_id="run-001"))
    second = run(service.deliver_for_run(result, scan_run_id="run-001"))

    assert first.sent == 1
    assert second.duplicate == 1
    assert len(sender.messages) == 1
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts(signal_id="sig-001")
    assert len(attempts) == 1
    assert attempts[0].alert_type == "SIGNAL_CONFIRMED"
    assert attempts[0].scan_run_id == "run-001"
    assert attempts[0].seen_count == 1


def test_watchlist_to_confirmed_sends_signal_confirmed_once(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )

    watchlist = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.WATCHLISTED,
                    signal_id="sig-life",
                    diagnostics=_public_ready_watchlist_diagnostics(),
                )
            ),
            scan_run_id="run-watch",
        )
    )
    confirmed = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.CONFIRMED,
                    previous=SetupLifecycleState.TRIGGERED,
                    signal_id="sig-life",
                )
            ),
            scan_run_id="run-confirm",
        )
    )
    duplicate_confirmed = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.CONFIRMED,
                    previous=SetupLifecycleState.TRIGGERED,
                    signal_id="sig-life",
                )
            ),
            scan_run_id="run-confirm",
        )
    )

    assert watchlist.sent == 1
    assert confirmed.sent == 1
    assert duplicate_confirmed.duplicate == 1
    assert "WATCHLIST — BTCUSDT" in sender.messages[0]
    assert "WATCHLIST UPGRADED — BTCUSDT" in sender.messages[1]


def test_public_watchlist_does_not_promote_to_executing(tmp_path: Path) -> None:
    db_path = tmp_path / "watchlist-no-executing.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    symbol = _symbol(
        SetupLifecycleState.A_GRADE_WATCH,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id="watch-no-exec",
        diagnostics=_public_ready_watchlist_diagnostics(),
        setup_quality=_setup_quality_with_grade(
            SetupQualityGrade.A,
            quality_state=SetupQualityState.WATCHLIST_NEAR_MISS,
            quality_score=88,
        ),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="watch-no-exec"))

    assert summary.sent == 1
    assert symbol.lifecycle_transition is not None
    assert symbol.lifecycle_transition.to_state == SetupLifecycleState.A_GRADE_WATCH
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT attempted_alert_type, new_state FROM telegram_alert_attempts WHERE signal_id = ?",
            ("watch-no-exec",),
        ).fetchone()
    assert row == (TelegramAlertType.WATCHLIST.value, SetupLifecycleState.A_GRADE_WATCH.value)
    assert "EXECUTING" not in sender.messages[0]


def test_public_watchlist_does_not_create_active_signal_base(tmp_path: Path) -> None:
    db_path = tmp_path / "watchlist-not-active-signal.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="watch-not-signal",
        diagnostics=_public_ready_watchlist_diagnostics(),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="watch-not-signal"))
    active = load_active_public_signals(project_root=tmp_path, database_path=db_path, limit=10)

    assert summary.sent == 1
    assert active.total == 0
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts(signal_id="watch-not-signal")
    assert [attempt.alert_type for attempt in attempts] == [TelegramAlertType.WATCHLIST.value]


def test_public_watchlist_does_not_call_order_execution(tmp_path: Path) -> None:
    db_path = tmp_path / "watchlist-no-orders.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None, order_execution_enabled=False),
        sender=sender,
    )
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="watch-no-orders",
        diagnostics=_public_ready_watchlist_diagnostics(
            first_failed_gate="missing_confirmation",
            gates_failed=("missing_confirmation",),
        ),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="watch-no-orders"))

    assert summary.sent == 1
    assert sender.calls == [{"message_type": TelegramMessageType.PUBLIC_WATCHLIST}]
    source = Path("app/alerts/telegram_lifecycle.py").read_text(encoding="utf-8").lower()
    for forbidden in ("execute_order", "place_order", "create_order"):
        assert forbidden not in source


def test_public_watchlist_dedupes_by_setup_id(tmp_path: Path) -> None:
    db_path = tmp_path / "watchlist-dedupe.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="watch-dedupe",
        diagnostics=_public_ready_watchlist_diagnostics(),
    )

    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="watch-dedupe-1"))
    second = run(service.deliver_for_run(_run_result(symbol), scan_run_id="watch-dedupe-2"))

    assert first.sent == 1
    assert second.duplicate == 1
    assert len(sender.messages) == 1
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts(signal_id="watch-dedupe")
    assert [attempt.alert_type for attempt in attempts] == [TelegramAlertType.WATCHLIST.value]


def test_public_watchlist_respects_symbol_side_plan_cooldown(tmp_path: Path) -> None:
    db_path = tmp_path / "watchlist-cooldown.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None, public_watchlist_cooldown_hours=24),
        sender=sender,
    )
    diagnostics = _public_ready_watchlist_diagnostics(
        first_failed_gate="missing_confirmation",
        gates_failed=("missing_confirmation",),
    )
    first_symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="watch-cooldown-1",
        diagnostics=diagnostics,
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
    )
    repeated_symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="watch-cooldown-2",
        diagnostics=diagnostics,
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
    )

    first = run(service.deliver_for_run(_run_result(first_symbol), scan_run_id="watch-cooldown-1"))
    second = run(service.deliver_for_run(_run_result(repeated_symbol), scan_run_id="watch-cooldown-2"))

    assert first.sent == 1
    assert second.skipped == 1
    assert len(sender.messages) == 1
    assert second.deliveries[0].error_message == "public_watchlist_cooldown_active"


def test_public_watchlist_routing_creates_send_attempt_or_skip_reason(tmp_path: Path) -> None:
    db_path = tmp_path / "watchlist-max-skip.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None, public_watchlist_max_per_scan=0),
        sender=sender,
    )
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="watch-max-skip",
        diagnostics=_public_ready_watchlist_diagnostics(
            first_failed_gate="rr_below_minimum",
            gates_failed=("rr_below_minimum",),
            rr_to_tp2=Decimal("2.6"),
        ),
        setup_quality=_setup_quality_with_grade(
            SetupQualityGrade.B_PLUS,
            quality_state=SetupQualityState.WATCHLIST_NEAR_MISS,
            quality_score=88,
        ),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="watch-max-skip"))

    assert summary.skipped == 1
    assert summary.sent == 0
    assert summary.public_watchlist_audit.candidates_considered == 1
    assert summary.public_watchlist_audit.eligible == 1
    assert summary.public_watchlist_audit.sent == 0
    assert summary.public_watchlist_audit.skipped_by_reason == {"public_watchlist_max_per_scan_reached": 1}
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts(signal_id="watch-max-skip")
    assert len(attempts) == 1
    assert attempts[0].telegram_status == "skipped"
    assert attempts[0].attempted_alert_type == TelegramAlertType.WATCHLIST.value
    assert attempts[0].blocked_reason == "public_watchlist_max_per_scan_reached"
    assert sender.messages == []


def test_confirmed_signal_gates_are_unchanged() -> None:
    blocked = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(rr_to_tp2=Decimal("2.9")),
            trade_idea=_trade_idea(best_rr=Decimal("2.9")),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )
    allowed = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.CONFIRMED, previous=SetupLifecycleState.TRIGGERED),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert blocked.eligible is False
    assert blocked.alert_type == TelegramAlertType.SIGNAL_CONFIRMED
    assert "planned_rr_below_min" in blocked.reason
    assert allowed.eligible is True
    assert allowed.alert_type == TelegramAlertType.SIGNAL_CONFIRMED


def test_confirmed_signal_rr_gate_unchanged() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(rr_to_tp2=Decimal("2.6")),
            trade_idea=_trade_idea(best_rr=Decimal("2.6")),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED
    assert "planned_rr_below_min:2.6<3" in decision.reason


def test_confirmed_signal_quality_gates_unchanged() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            setup_quality=_setup_quality_with_grade(SetupQualityGrade.B, quality_score=70),
            trade_idea=_trade_idea(opportunity_grade="B", opportunity_score=Decimal("88")),
        )
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED
    assert "below_min_public_grade" in decision.reason


def test_phase42k_watchlist_transition_matrix_preserves_original_id_rows_and_dedupes(
    tmp_path: Path,
) -> None:
    cases = (
        (
            "confirmed",
            TelegramAlertType.SIGNAL_CONFIRMED,
            "CONFIRMED",
            lambda signal_id: _symbol(
                SetupLifecycleState.CONFIRMED,
                previous=SetupLifecycleState.TRIGGERED,
                signal_id=signal_id,
            ),
        ),
        (
            "invalidated",
            TelegramAlertType.INVALIDATED,
            "INVALIDATED",
            lambda signal_id: _symbol(
                SetupLifecycleState.INVALIDATED,
                previous=SetupLifecycleState.WATCHLISTED,
                signal_id=signal_id,
            ),
        ),
        (
            "expired",
            TelegramAlertType.EXPIRED,
            "EXPIRED",
            lambda signal_id: _symbol(
                SetupLifecycleState.EXPIRED,
                previous=SetupLifecycleState.STALKING,
                signal_id=signal_id,
            ),
        ),
        (
            "no-longer-tracking",
            TelegramAlertType.NO_LONGER_TRACKING,
            "NO LONGER TRACKING",
            lambda signal_id: _symbol(
                SetupLifecycleState.COOLDOWN,
                previous=SetupLifecycleState.TRIGGERED,
                signal_id=signal_id,
            ),
        ),
    )
    for name, expected_alert_type, expected_status, make_transition in cases:
        db_path = tmp_path / f"{name}.db"
        signal_id = f"phase42k-{name}"
        sender = FakeSender()
        service = TelegramLifecycleDeliveryService(
            database_path=db_path,
            settings=Settings(_env_file=None),
            sender=sender,
            min_rr=Decimal("3"),
            min_score_for_idea=Decimal("80"),
        )

        watchlist = run(
            service.deliver_for_run(
                _run_result(
                    _symbol(
                        SetupLifecycleState.WATCHLISTED,
                        signal_id=signal_id,
                        diagnostics=_public_ready_watchlist_diagnostics(),
                    )
                ),
                scan_run_id=f"phase42k-watch-{name}",
            )
        )
        transition_symbol = make_transition(signal_id)
        transitioned = run(
            service.deliver_for_run(
                _run_result(transition_symbol),
                scan_run_id=f"phase42k-transition-{name}",
            )
        )
        duplicate = run(
            service.deliver_for_run(
                _run_result(transition_symbol),
                scan_run_id=f"phase42k-transition-repeat-{name}",
            )
        )

        assert watchlist.sent == 1
        assert transitioned.sent == 1
        assert duplicate.duplicate == 1
        assert len(sender.messages) == 2
        assert "WATCHLIST — BTCUSDT" in sender.messages[0]
        assert "WATCHLIST — BTCUSDT" not in sender.messages[1]
        if expected_alert_type == TelegramAlertType.NO_LONGER_TRACKING:
            assert "CONFIRMED SIGNAL" not in sender.messages[1]
        _assert_transition_message_clean(sender.messages[1], signal_id=signal_id, status=expected_status)
        assert _telegram_attempt_rows(db_path) == [
            (signal_id, TelegramAlertType.WATCHLIST.value, "sent"),
            (signal_id, expected_alert_type.value, "sent"),
        ]


def test_phase42l_reconciles_soft_failed_confirmation_gate_variants_after_grace_threshold(
    tmp_path: Path,
) -> None:
    cases = (
        ("target_expansion", "soft_failed_confirmation:target_expansion"),
        ("target_integrity", "soft_failed_confirmation:target_expansion"),
        ("rr_below_min", "soft_failed_confirmation:rr_below_min"),
    )
    for failed_gate, expected_soft_reason in cases:
        db_path = tmp_path / f"{failed_gate}.db"
        signal_id = f"phase42l-{failed_gate}"
        _seed_prior_active_alert(
            db_path,
            signal_id=signal_id,
            alert_type=TelegramAlertType.WATCHLIST,
            symbol="AAVEUSDT",
            direction="long",
        )
        _store_lifecycle_record(
            db_path,
            SetupLifecycleRecord(
                lifecycle_id=signal_id,
                symbol="AAVEUSDT",
                mode="scalp",
                direction="long",
                current_state=SetupLifecycleState.CONFIRMED,
                previous_state=SetupLifecycleState.TRIGGERED,
                first_seen_at="2026-06-03T00:00:00+00:00",
                last_seen_at="2026-06-03T00:05:00+00:00",
                last_transition_at="2026-06-03T00:00:00+00:00",
                failed_gate=failed_gate,
                invalidation_reason="Setup rejected because RR below the configured minimum.",
            ),
        )
        sender = FakeSender()
        service = TelegramLifecycleDeliveryService(
            database_path=db_path,
            settings=Settings(_env_file=None),
            sender=sender,
            min_rr=Decimal("3"),
            min_score_for_idea=Decimal("80"),
        )

        first = run(service.deliver_for_run(_empty_run_result(), scan_run_id=f"phase42l-{failed_gate}-1"))
        second = run(service.deliver_for_run(_empty_run_result(), scan_run_id=f"phase42l-{failed_gate}-2"))
        third = run(service.deliver_for_run(_empty_run_result(), scan_run_id=f"phase42l-{failed_gate}-3"))
        duplicate = run(
            service.deliver_for_run(_empty_run_result(), scan_run_id=f"phase42l-{failed_gate}-4")
        )

        assert first.sent == 0
        assert second.sent == 0
        assert third.sent == 1
        assert duplicate.sent == 0
        assert duplicate.duplicate == 0
        assert len(sender.messages) == 1
        _assert_transition_message_clean(
            sender.messages[0],
            signal_id=signal_id,
            status="NO LONGER TRACKING",
        )
        assert "The wolf walks away." in sender.messages[0]
        assert "CONFIRMED SIGNAL" not in sender.messages[0]
        soft_rows = _soft_failed_confirmation_rows(db_path)
        assert len(soft_rows) == 1
        assert soft_rows[0][0] == "SOFT_FAILED_CONFIRMATION"
        assert soft_rows[0][1] == "skipped"
        assert soft_rows[0][2] == expected_soft_reason
        assert soft_rows[0][3] == 3
        attempt_rows = _telegram_attempt_rows(db_path)
        assert attempt_rows[0] == (signal_id, TelegramAlertType.WATCHLIST.value, "sent")
        assert attempt_rows[1][0] == signal_id
        assert attempt_rows[1][1].startswith("SOFT_FAILED_CONFIRMATION_")
        assert attempt_rows[1][2] == "skipped"
        assert attempt_rows[2] == (signal_id, TelegramAlertType.NO_LONGER_TRACKING.value, "sent")


def test_phase42l_soft_blocker_observations_do_not_block_later_valid_confirmation(tmp_path: Path) -> None:
    db_path = tmp_path / "soft-then-confirmed.db"
    signal_id = "phase42l-soft-then-confirmed"
    _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
    _store_lifecycle_record(
        db_path,
        SetupLifecycleRecord(
            lifecycle_id=signal_id,
            symbol="BTCUSDT",
            mode="swing",
            direction="long",
            current_state=SetupLifecycleState.CONFIRMED,
            previous_state=SetupLifecycleState.TRIGGERED,
            first_seen_at="2026-06-03T00:00:00+00:00",
            last_seen_at="2026-06-03T00:05:00+00:00",
            last_transition_at="2026-06-03T00:00:00+00:00",
            failed_gate="regime_compatibility",
            action_label="Wait for cleaner regime",
            invalidation_reason="Setup rejected by regime weakness.",
        ),
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )

    first = run(service.deliver_for_run(_empty_run_result(), scan_run_id="soft-1"))
    second = run(service.deliver_for_run(_empty_run_result(), scan_run_id="soft-2"))
    confirmed = _symbol(SetupLifecycleState.CONFIRMED, transitioned=False, signal_id=signal_id)
    assert confirmed.lifecycle_state is not None
    _store_lifecycle_record(db_path, confirmed.lifecycle_state)
    third = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="confirmed-3"))

    assert first.sent == 0
    assert second.sent == 0
    assert third.sent == 1
    assert len(sender.messages) == 1
    assert "WATCHLIST UPGRADED — BTCUSDT" in sender.messages[0]
    assert "Signal ID:" not in sender.messages[0]
    soft_rows = _soft_failed_confirmation_rows(db_path)
    assert len(soft_rows) == 1
    assert soft_rows[0][3] == 2
    assert _telegram_attempt_rows(db_path)[-1] == (
        signal_id,
        TelegramAlertType.SIGNAL_CONFIRMED.value,
        "sent",
    )


def test_phase42l_wrong_side_targets_terminalize_without_soft_grace(tmp_path: Path) -> None:
    db_path = tmp_path / "wrong-side-target.db"
    signal_id = "phase42l-wrong-side-target"
    _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )
    confirmed = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id=signal_id,
        diagnostics=_diagnostics(tp1=Decimal("90")),
    )

    summary = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="wrong-target-1"))

    assert summary.sent == 1
    assert len(sender.messages) == 1
    assert "WATCHLIST INVALIDATED — BTCUSDT" in sender.messages[0]
    assert "Signal ID:" not in sender.messages[0]
    assert _soft_failed_confirmation_rows(db_path) == []
    assert _telegram_attempt_rows(db_path) == [
        (signal_id, TelegramAlertType.WATCHLIST.value, "sent"),
        (signal_id, TelegramAlertType.NO_LONGER_TRACKING.value, "sent"),
    ]


def test_watchlist_to_confirmed_with_regime_failed_gate_sends_no_longer_tracking(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_prior_active_alert(
        db_path,
        signal_id="sig-regime-watch",
        alert_type=TelegramAlertType.WATCHLIST,
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )
    confirmed = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            signal_id="sig-regime-watch",
        ),
        failed_gate="regime_compatibility",
        invalidation_reason="Setup rejected by regime weakness: penalty 15; scalp compatibility Weak.",
    )

    first = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-regime-1"))
    second = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-regime-2"))
    third = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-regime-3"))
    duplicate = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-regime-repeat"))

    assert first.sent == 0
    assert second.sent == 0
    assert third.sent == 1
    assert duplicate.duplicate == 1
    assert len(sender.messages) == 1
    assert "CONFIRMED SIGNAL" not in sender.messages[0]
    assert "WATCHLIST INVALIDATED — BTCUSDT" in sender.messages[0]
    assert "Signal ID:" not in sender.messages[0]
    assert "The wolf walks away." in sender.messages[0]
    assert "penalty 15" not in sender.messages[0]
    assert "scalp compatibility Weak" not in sender.messages[0]
    assert sender.messages[0].startswith(HEADER_PREFIX)
    assert sender.messages[0].endswith(FOOTER)
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT alert_type, telegram_status FROM telegram_alert_attempts ORDER BY id").fetchall()
    assert rows[0] == (TelegramAlertType.WATCHLIST.value, "sent")
    assert rows[1][0].startswith("SOFT_FAILED_CONFIRMATION_")
    assert rows[1][1] == "skipped"
    assert rows[2] == (TelegramAlertType.NO_LONGER_TRACKING.value, "sent")


def test_mstr_style_confirmed_regime_rejection_sends_no_longer_tracking(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_prior_active_alert(
        db_path,
        signal_id="mstr-watch",
        alert_type=TelegramAlertType.WATCHLIST,
        symbol="MSTRUSDT",
        direction="long",
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )
    confirmed = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=None,
            signal_id="mstr-terminal-life",
            diagnostics=_diagnostics(
                bias="long",
                direction="long",
                reason="Setup rejected by regime weakness: penalty 15; scalp compatibility Weak.",
            ),
        ).model_copy(update={"symbol": "MSTRUSDT"}),
        failed_gate="regime_compatibility",
        invalidation_reason="Wait for cleaner regime. Setup rejected by regime weakness: penalty 15.",
    )

    first = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-mstr-1"))
    second = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-mstr-2"))
    third = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-mstr-3"))

    assert first.sent == 0
    assert second.sent == 0
    assert third.sent == 1
    assert len(sender.messages) == 1
    assert "WATCHLIST INVALIDATED — MSTRUSDT" in sender.messages[0]
    assert "Bias was: LONG" in sender.messages[0]
    assert "Signal ID:" not in sender.messages[0]
    assert "The wolf walks away." in sender.messages[0]
    assert "SIGNAL CONFIRMED" not in sender.messages[0]
    assert "penalty 15" not in sender.messages[0]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT signal_id, alert_type, direction FROM telegram_alert_attempts ORDER BY id").fetchall()
    assert rows[0] == ("mstr-watch", TelegramAlertType.WATCHLIST.value, "long")
    assert rows[1][0] == "mstr-watch"
    assert rows[1][1].startswith("SOFT_FAILED_CONFIRMATION_")
    assert rows[1][2] == "long"
    assert rows[2] == ("mstr-watch", TelegramAlertType.NO_LONGER_TRACKING.value, "long")


def test_watchlist_to_confirmed_with_rr_guard_failure_sends_no_longer_tracking(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_prior_active_alert(
        db_path,
        signal_id="sig-low-rr-watch",
        alert_type=TelegramAlertType.WATCHLIST,
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )
    confirmed = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id="sig-low-rr-watch",
        diagnostics=_diagnostics(rr_to_tp2=Decimal("2.79")),
        trade_idea=_trade_idea(best_rr=Decimal("2.79")),
    )

    first = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-low-rr-1"))
    second = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-low-rr-2"))
    third = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-low-rr-3"))

    assert first.sent == 0
    assert second.sent == 0
    assert third.sent == 1
    assert len(sender.messages) == 1
    assert "WATCHLIST INVALIDATED — BTCUSDT" in sender.messages[0]
    assert "The wolf walks away." in sender.messages[0]
    assert "SIGNAL CONFIRMED" not in sender.messages[0]


def test_watchlist_to_confirmed_with_structural_failure_sends_invalidation(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_prior_active_alert(
        db_path,
        signal_id="sig-structure-watch",
        alert_type=TelegramAlertType.WATCHLIST,
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    confirmed = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            signal_id="sig-structure-watch",
        ),
        failed_gate="structural_breakdown",
        invalidation_reason="Structure broke before confirmation.",
    )

    summary = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-structure"))

    assert summary.sent == 1
    assert len(sender.messages) == 1
    assert "WATCHLIST INVALIDATED — BTCUSDT" in sender.messages[0]
    assert "Status: INVALIDATED" in sender.messages[0]
    assert "Signal ID:" not in sender.messages[0]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT alert_type, telegram_status FROM telegram_alert_attempts ORDER BY id").fetchall()
    assert rows == [
        (TelegramAlertType.WATCHLIST.value, "sent"),
        (TelegramAlertType.INVALIDATED.value, "sent"),
    ]


def test_sent_watchlist_reconciliation_mstr_current_confirmed_failed_gate_sends_no_longer_tracking(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_prior_active_alert(
        db_path,
        signal_id="mstr-watch",
        alert_type=TelegramAlertType.WATCHLIST,
        symbol="MSTRUSDT",
        direction="long",
    )
    _store_lifecycle_record(
        db_path,
        SetupLifecycleRecord(
            lifecycle_id="mstr-watch",
            symbol="MSTRUSDT",
            mode="scalp",
            direction="long",
            current_state=SetupLifecycleState.CONFIRMED,
            previous_state=None,
            first_seen_at="2026-06-03T09:11:16+00:00",
            last_seen_at="2026-06-03T09:48:45+00:00",
            last_transition_at="2026-06-03T09:11:16+00:00",
            failed_gate="regime_compatibility",
            action_label="Wait for cleaner regime",
            invalidation_reason="Setup rejected by regime weakness: penalty 15; scalp compatibility Weak.",
        ),
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )

    first = run(service.deliver_for_run(_empty_run_result(), scan_run_id="run-mstr-reconcile-1"))
    second = run(service.deliver_for_run(_empty_run_result(), scan_run_id="run-mstr-reconcile-2"))
    third = run(service.deliver_for_run(_empty_run_result(), scan_run_id="run-mstr-reconcile-3"))
    repeated = run(service.deliver_for_run(_empty_run_result(), scan_run_id="run-mstr-repeat"))

    assert first.sent == 0
    assert second.sent == 0
    assert third.sent == 1
    assert repeated.sent == 0
    assert len(sender.messages) == 1
    message = sender.messages[0]
    assert message.startswith(HEADER_PREFIX)
    assert message.endswith(FOOTER)
    assert "WATCHLIST INVALIDATED — MSTRUSDT" in message
    assert "Bias was: LONG" in message
    assert "Signal ID:" not in message
    assert "The wolf walks away." in message
    assert "SIGNAL CONFIRMED" not in message
    assert "penalty 15" not in message
    assert "scalp compatibility Weak" not in message
    assert "Decimal(" not in message
    assert "{" not in message
    assert "}" not in message
    assert "Setup Type" not in message
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT signal_id, alert_type, telegram_status, direction FROM telegram_alert_attempts ORDER BY id"
        ).fetchall()
    assert rows[0] == ("mstr-watch", TelegramAlertType.WATCHLIST.value, "sent", "long")
    assert rows[1][0] == "mstr-watch"
    assert rows[1][1].startswith("SOFT_FAILED_CONFIRMATION_")
    assert rows[1][2:] == ("skipped", "long")
    assert rows[2] == ("mstr-watch", TelegramAlertType.NO_LONGER_TRACKING.value, "sent", "long")


def test_sent_watchlist_reconciliation_current_terminal_states_send_updates(tmp_path: Path) -> None:
    cases = (
        (SetupLifecycleState.INVALIDATED, TelegramAlertType.INVALIDATED, "Status: INVALIDATED"),
        (SetupLifecycleState.EXPIRED, TelegramAlertType.EXPIRED, "WATCHLIST INVALIDATED"),
        (SetupLifecycleState.COOLDOWN, TelegramAlertType.NO_LONGER_TRACKING, "WATCHLIST INVALIDATED"),
        (SetupLifecycleState.COOLED_DOWN, TelegramAlertType.NO_LONGER_TRACKING, "WATCHLIST INVALIDATED"),
    )
    for state, expected_alert_type, expected_status in cases:
        db_path = tmp_path / f"{state.value.lower()}.db"
        signal_id = f"watch-{state.value.lower()}"
        _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
        _store_lifecycle_record(
            db_path,
            _record(state, previous=SetupLifecycleState.WATCHLISTED, signal_id=signal_id),
        )
        sender = FakeSender()
        service = TelegramLifecycleDeliveryService(
            database_path=db_path,
            settings=Settings(_env_file=None),
            sender=sender,
        )

        summary = run(service.deliver_for_run(_empty_run_result(), scan_run_id=f"run-{state.value.lower()}"))

        assert summary.sent == 1
        assert len(sender.messages) == 1
        assert expected_status in sender.messages[0]
        assert "Signal ID:" not in sender.messages[0]
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute("SELECT alert_type FROM telegram_alert_attempts ORDER BY id").fetchall()
        assert rows == [(TelegramAlertType.WATCHLIST.value,), (expected_alert_type.value,)]


def test_sent_watchlist_reconciliation_confirmed_with_eligibility_pass_sends_signal_confirmed_once(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candle_craft.db"
    signal_id = "sig-reconcile-confirm"
    _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
    symbol = _symbol(SetupLifecycleState.CONFIRMED, transitioned=False, signal_id=signal_id)
    assert symbol.lifecycle_state is not None
    _store_lifecycle_record(db_path, symbol.lifecycle_state)
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="run-confirm-reconcile"))
    repeated = run(service.deliver_for_run(_run_result(symbol), scan_run_id="run-confirm-repeat"))

    assert summary.sent == 1
    assert repeated.duplicate == 1
    assert len(sender.messages) == 1
    assert "WATCHLIST UPGRADED — BTCUSDT" in sender.messages[0]
    assert "Signal ID:" not in sender.messages[0]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT alert_type FROM telegram_alert_attempts ORDER BY id").fetchall()
    assert rows == [(TelegramAlertType.WATCHLIST.value,), (TelegramAlertType.SIGNAL_CONFIRMED.value,)]


def test_sent_watchlist_reconciliation_confirmed_with_eligibility_fail_sends_no_longer_tracking(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "candle_craft.db"
    signal_id = "sig-reconcile-low-rr"
    _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
    symbol = _symbol(
        SetupLifecycleState.CONFIRMED,
        transitioned=False,
        signal_id=signal_id,
        diagnostics=_diagnostics(rr_to_tp2=Decimal("2.4")),
        trade_idea=_trade_idea(best_rr=Decimal("2.4")),
    )
    assert symbol.lifecycle_state is not None
    _store_lifecycle_record(db_path, symbol.lifecycle_state)
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )

    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="run-low-rr-reconcile-1"))
    second = run(service.deliver_for_run(_run_result(symbol), scan_run_id="run-low-rr-reconcile-2"))
    third = run(service.deliver_for_run(_run_result(symbol), scan_run_id="run-low-rr-reconcile-3"))

    assert first.sent == 0
    assert second.sent == 0
    assert third.sent == 1
    assert len(sender.messages) == 1
    assert "WATCHLIST INVALIDATED — BTCUSDT" in sender.messages[0]
    assert "The wolf walks away." in sender.messages[0]
    assert "SIGNAL CONFIRMED" not in sender.messages[0]


def test_sent_watchlist_reconciliation_current_watch_states_send_nothing_without_failure(
    tmp_path: Path,
) -> None:
    for state in (SetupLifecycleState.WATCHLISTED, SetupLifecycleState.STALKING, SetupLifecycleState.TRIGGERED):
        db_path = tmp_path / f"{state.value.lower()}.db"
        signal_id = f"watch-{state.value.lower()}"
        _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
        _store_lifecycle_record(db_path, _record(state, signal_id=signal_id))
        sender = FakeSender()
        service = TelegramLifecycleDeliveryService(
            database_path=db_path,
            settings=Settings(_env_file=None),
            sender=sender,
        )

        summary = run(service.deliver_for_run(_empty_run_result(), scan_run_id=f"run-{state.value.lower()}"))

        assert summary.sent == 0
        assert summary.blocked == 0
        assert sender.messages == []
        with sqlite3.connect(db_path) as connection:
            rows = connection.execute("SELECT alert_type FROM telegram_alert_attempts ORDER BY id").fetchall()
        assert rows == [(TelegramAlertType.WATCHLIST.value,)]


def test_sent_watchlist_older_than_48h_records_expiry_audit_without_send(tmp_path: Path) -> None:
    db_path = tmp_path / "expired-watch.db"
    old_sent_at = (datetime.now(UTC) - timedelta(hours=49)).isoformat().replace("+00:00", "Z")
    _seed_prior_active_alert(
        db_path,
        signal_id="stale-watch",
        alert_type=TelegramAlertType.WATCHLIST,
        sent_at=old_sent_at,
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )

    summary = run(service.deliver_for_run(_empty_run_result(), scan_run_id="run-expiry"))

    assert summary.skipped == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT attempted_alert_type, telegram_status, blocked_reason
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()
    assert rows[0] == (TelegramAlertType.WATCHLIST.value, "sent", NA)
    assert rows[1] == ("WATCHLIST_EXPIRY", "skipped", "watchlist_expired_48h")


def test_sent_watchlist_reconciliation_symbol_fallback_requires_one_active_watchlist(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "fallback.db"
    _seed_prior_active_alert(db_path, signal_id="fallback-watch", alert_type=TelegramAlertType.WATCHLIST)
    _store_lifecycle_record(
        db_path,
        _record(
            SetupLifecycleState.INVALIDATED,
            previous=SetupLifecycleState.WATCHLISTED,
            signal_id="different-life-id",
        ),
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )

    summary = run(service.deliver_for_run(_empty_run_result(), scan_run_id="run-fallback"))

    assert summary.sent == 1
    assert "Signal ID:" not in sender.messages[0]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT signal_id, alert_type FROM telegram_alert_attempts ORDER BY id").fetchall()
    assert rows == [
        ("fallback-watch", TelegramAlertType.WATCHLIST.value),
        ("fallback-watch", TelegramAlertType.INVALIDATED.value),
    ]


def test_sent_watchlist_reconciliation_symbol_fallback_blocks_when_ambiguous(tmp_path: Path) -> None:
    db_path = tmp_path / "ambiguous.db"
    _seed_prior_active_alert(db_path, signal_id="watch-one", alert_type=TelegramAlertType.WATCHLIST)
    _seed_prior_active_alert(db_path, signal_id="watch-two", alert_type=TelegramAlertType.WATCHLIST)
    _store_lifecycle_record(
        db_path,
        _record(
            SetupLifecycleState.INVALIDATED,
            previous=SetupLifecycleState.WATCHLISTED,
            signal_id="ambiguous-life",
        ),
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )

    summary = run(service.deliver_for_run(_empty_run_result(), scan_run_id="run-ambiguous"))

    assert summary.blocked == 2
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        reasons = connection.execute(
            "SELECT blocked_reason FROM telegram_alert_attempts WHERE telegram_status = 'blocked' ORDER BY id"
        ).fetchall()
    assert reasons == [
        ("sent_watchlist_reconciliation_ambiguous",),
        ("sent_watchlist_reconciliation_ambiguous",),
    ]


def test_sent_watchlist_reconciliation_no_lifecycle_match_blocks_safely(tmp_path: Path) -> None:
    db_path = tmp_path / "no-match.db"
    _seed_prior_active_alert(db_path, signal_id="orphan-watch", alert_type=TelegramAlertType.WATCHLIST)
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )

    summary = run(service.deliver_for_run(_empty_run_result(), scan_run_id="run-no-match"))

    assert summary.blocked == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT attempted_alert_type, blocked_reason FROM telegram_alert_attempts WHERE telegram_status = 'blocked'"
        ).fetchone()
    assert row == ("SENT_WATCHLIST_RECONCILIATION", "sent_watchlist_reconciliation_no_lifecycle_match")


def test_sent_watchlist_reconciliation_exact_match_wins_and_uses_original_direction(tmp_path: Path) -> None:
    db_path = tmp_path / "exact.db"
    _seed_prior_active_alert(
        db_path,
        signal_id="exact-watch",
        alert_type=TelegramAlertType.WATCHLIST,
        direction="long",
    )
    _seed_prior_active_alert(
        db_path,
        signal_id="other-watch",
        alert_type=TelegramAlertType.WATCHLIST,
        direction="long",
    )
    _store_lifecycle_record(
        db_path,
        _record(
            SetupLifecycleState.INVALIDATED,
            previous=SetupLifecycleState.WATCHLISTED,
            signal_id="exact-watch",
        ).model_copy(update={"direction": NA}),
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )

    summary = run(service.deliver_for_run(_empty_run_result(), scan_run_id="run-exact"))

    assert summary.sent == 1
    assert summary.blocked == 1
    assert len(sender.messages) == 1
    assert "WATCHLIST INVALIDATED — BTCUSDT" in sender.messages[0]
    assert "Signal ID:" not in sender.messages[0]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT signal_id, alert_type, direction, blocked_reason FROM telegram_alert_attempts ORDER BY id"
        ).fetchall()
    assert rows[:3] == [
        ("exact-watch", TelegramAlertType.WATCHLIST.value, "long", "N/A"),
        ("other-watch", TelegramAlertType.WATCHLIST.value, "long", "N/A"),
        ("exact-watch", TelegramAlertType.INVALIDATED.value, "long", "N/A"),
    ]
    assert rows[3][0] == "other-watch"
    assert rows[3][1].startswith("NO_LONGER_TRACKING_BLOCKED_")
    assert rows[3][2] == "long"
    assert rows[3][3] == "sent_watchlist_reconciliation_ambiguous"


def test_watchlist_to_invalidated_sends_invalidation_once(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )

    watchlist = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.WATCHLISTED,
                    signal_id="sig-invalidates",
                    diagnostics=_public_ready_watchlist_diagnostics(),
                )
            ),
            scan_run_id="run-watch",
        )
    )
    invalidated = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.INVALIDATED,
                    previous=SetupLifecycleState.WATCHLISTED,
                    signal_id="sig-invalidates",
                )
            ),
            scan_run_id="run-invalid",
        )
    )
    duplicate_invalidated = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.INVALIDATED,
                    previous=SetupLifecycleState.WATCHLISTED,
                    signal_id="sig-invalidates",
                )
            ),
            scan_run_id="run-invalid",
        )
    )

    assert watchlist.sent == 1
    assert invalidated.sent == 1
    assert duplicate_invalidated.duplicate == 1
    assert "WATCHLIST — BTCUSDT" in sender.messages[0]
    assert "WATCHLIST INVALIDATED — BTCUSDT" in sender.messages[1]
    assert sender.messages[1].startswith(HEADER_PREFIX)
    assert sender.messages[1].endswith(FOOTER)
    assert "Status: INVALIDATED" in sender.messages[1]
    assert "Signal ID:" not in sender.messages[1]
    assert "The wolf walks away." in sender.messages[1]
    assert "Setup Type" not in sender.messages[1]
    assert "Decimal(" not in sender.messages[1]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT alert_type, telegram_status, previous_state, new_state
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()
    assert rows == [
        (TelegramAlertType.WATCHLIST.value, "sent", NA, "WATCHLISTED"),
        (TelegramAlertType.INVALIDATED.value, "sent", "WATCHLISTED", "INVALIDATED"),
    ]


def test_watchlist_to_expired_sends_expired_once(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )

    watchlist = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.WATCHLISTED,
                    signal_id="sig-expires",
                    diagnostics=_public_ready_watchlist_diagnostics(),
                )
            ),
            scan_run_id="run-watch",
        )
    )
    expired = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.EXPIRED,
                    previous=SetupLifecycleState.STALKING,
                    signal_id="sig-expires",
                )
            ),
            scan_run_id="run-expired",
        )
    )
    duplicate_expired = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.EXPIRED,
                    previous=SetupLifecycleState.STALKING,
                    signal_id="sig-expires",
                )
            ),
            scan_run_id="run-expired",
        )
    )

    assert watchlist.sent == 1
    assert expired.sent == 1
    assert duplicate_expired.duplicate == 1
    assert len(sender.messages) == 2
    assert "WATCHLIST INVALIDATED — BTCUSDT" in sender.messages[1]
    assert "Signal ID:" not in sender.messages[1]
    assert "The wolf walks away." in sender.messages[1]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT alert_type, telegram_status FROM telegram_alert_attempts ORDER BY id").fetchall()
    assert rows == [
        (TelegramAlertType.WATCHLIST.value, "sent"),
        (TelegramAlertType.EXPIRED.value, "sent"),
    ]


def test_watchlist_to_cooldown_sends_no_longer_tracking_once(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )

    watchlist = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.WATCHLISTED,
                    signal_id="sig-cooldown",
                    diagnostics=_public_ready_watchlist_diagnostics(),
                )
            ),
            scan_run_id="run-watch",
        )
    )
    cooldown = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.COOLDOWN,
                    previous=SetupLifecycleState.TRIGGERED,
                    signal_id="sig-cooldown",
                )
            ),
            scan_run_id="run-cooldown",
        )
    )
    duplicate_cooldown = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.COOLDOWN,
                    previous=SetupLifecycleState.TRIGGERED,
                    signal_id="sig-cooldown",
                )
            ),
            scan_run_id="run-cooldown",
        )
    )

    assert watchlist.sent == 1
    assert cooldown.sent == 1
    assert duplicate_cooldown.duplicate == 1
    assert len(sender.messages) == 2
    assert "WATCHLIST INVALIDATED — BTCUSDT" in sender.messages[1]
    assert "Signal ID:" not in sender.messages[1]
    assert "The wolf walks away." in sender.messages[1]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT alert_type, telegram_status FROM telegram_alert_attempts ORDER BY id").fetchall()
    assert rows == [
        (TelegramAlertType.WATCHLIST.value, "sent"),
        (TelegramAlertType.NO_LONGER_TRACKING.value, "sent"),
    ]


def test_blocked_watchlist_to_invalidated_does_not_send_terminal_update(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    blocked_watchlist = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="blocked-terminal",
        diagnostics=_public_ready_watchlist_diagnostics(
            entry_low=NA,
            entry_high=NA,
            stop=NA,
            tp1=NA,
            tp2=NA,
            tp3=NA,
            rr_to_tp2=NA,
        ),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
    )
    invalidated = _symbol(
        SetupLifecycleState.INVALIDATED,
        previous=SetupLifecycleState.WATCHLISTED,
        signal_id="blocked-terminal",
    )

    blocked = run(service.deliver_for_run(_run_result(blocked_watchlist), scan_run_id="run-blocked"))
    terminal = run(service.deliver_for_run(_run_result(invalidated), scan_run_id="run-invalid"))

    assert blocked.blocked == 0
    assert blocked.public_watchlist_audit.blocked_before_attempt == 1
    assert terminal.blocked == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT attempted_alert_type, telegram_status, blocked_reason
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()
    assert rows == [
        (TelegramAlertType.INVALIDATED.value, "blocked", "terminal_update_no_prior_public_alert"),
    ]


def test_skipped_watchlist_to_invalidated_does_not_count_as_public_prior(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_prior_active_alert(
        db_path,
        signal_id="skipped-terminal",
        alert_type=TelegramAlertType.WATCHLIST,
        status="skipped",
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )

    terminal = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.INVALIDATED,
                    previous=SetupLifecycleState.WATCHLISTED,
                    signal_id="skipped-terminal",
                )
            ),
            scan_run_id="run-invalid",
        )
    )

    assert terminal.blocked == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT attempted_alert_type, telegram_status, blocked_reason FROM telegram_alert_attempts ORDER BY id"
        ).fetchall()
    assert rows == [
        (TelegramAlertType.WATCHLIST.value, "skipped", NA),
        (TelegramAlertType.INVALIDATED.value, "blocked", "terminal_update_no_prior_public_alert"),
    ]


def test_terminal_update_uses_original_fallback_signal_id_when_lifecycle_id_differs(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    diagnostics = _public_ready_watchlist_diagnostics()
    fallback_source = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="old-life",
        diagnostics=diagnostics,
    ).model_copy(update={"lifecycle_state": None, "lifecycle_transition": None})
    original_signal_id = _signal_id(fallback_source)
    _seed_prior_active_alert(
        db_path,
        signal_id=original_signal_id,
        alert_type=TelegramAlertType.WATCHLIST,
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )

    terminal = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.INVALIDATED,
                    previous=SetupLifecycleState.TRIGGERED,
                    signal_id="new-life",
                    diagnostics=diagnostics,
                )
            ),
            scan_run_id="run-invalid",
        )
    )

    assert terminal.sent == 1
    assert len(sender.messages) == 1
    assert "Signal ID:" not in sender.messages[0]
    assert original_signal_id not in sender.messages[0]
    assert "new-life" not in sender.messages[0]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT signal_id, alert_type FROM telegram_alert_attempts ORDER BY id").fetchall()
    assert rows == [
        (original_signal_id, TelegramAlertType.WATCHLIST.value),
        (original_signal_id, TelegramAlertType.INVALIDATED.value),
    ]


def test_terminal_update_symbol_fallback_matches_single_active_watchlist_with_original_direction(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_prior_active_alert(
        db_path,
        signal_id="original-symbol-watch",
        alert_type=TelegramAlertType.WATCHLIST,
        symbol="BTCUSDT",
        direction="short",
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    terminal = _with_lifecycle_direction(
        _symbol(
            SetupLifecycleState.INVALIDATED,
            previous=SetupLifecycleState.WATCHLISTED,
            signal_id="terminal-symbol-life",
            diagnostics=_public_ready_watchlist_diagnostics(bias=NA, direction=NA),
        ),
        NA,
    )

    summary = run(service.deliver_for_run(_run_result(terminal), scan_run_id="run-invalid"))

    assert summary.sent == 1
    assert len(sender.messages) == 1
    assert "WATCHLIST INVALIDATED — BTCUSDT" in sender.messages[0]
    assert "Bias was: SHORT" in sender.messages[0]
    assert "Signal ID:" not in sender.messages[0]
    assert "terminal-symbol-life" not in sender.messages[0]
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT signal_id, alert_type, direction FROM telegram_alert_attempts ORDER BY id"
        ).fetchall()
    assert rows == [
        ("original-symbol-watch", TelegramAlertType.WATCHLIST.value, "short"),
        ("original-symbol-watch", TelegramAlertType.INVALIDATED.value, "short"),
    ]


def test_terminal_update_symbol_fallback_blocks_ambiguous_active_watchlists(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_prior_active_alert(
        db_path,
        signal_id="ambiguous-long-watch",
        alert_type=TelegramAlertType.WATCHLIST,
        symbol="BTCUSDT",
        direction="long",
    )
    _seed_prior_active_alert(
        db_path,
        signal_id="ambiguous-short-watch",
        alert_type=TelegramAlertType.WATCHLIST,
        symbol="BTCUSDT",
        direction="short",
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    terminal = _with_lifecycle_direction(
        _symbol(
            SetupLifecycleState.INVALIDATED,
            previous=SetupLifecycleState.WATCHLISTED,
            signal_id="terminal-ambiguous-life",
            diagnostics=_public_ready_watchlist_diagnostics(bias=NA, direction=NA),
        ),
        NA,
    )

    summary = run(service.deliver_for_run(_run_result(terminal), scan_run_id="run-invalid"))

    assert summary.blocked == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT attempted_alert_type, telegram_status, blocked_reason
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()
    assert rows[-1] == (
        TelegramAlertType.INVALIDATED.value,
        "blocked",
        "terminal_update_identity_ambiguous",
    )


def test_repeated_unmatched_terminal_update_compacts_seen_count(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    terminal = _symbol(
        SetupLifecycleState.INVALIDATED,
        previous=SetupLifecycleState.WATCHLISTED,
        signal_id="terminal-no-prior-repeat",
    )

    first = run(service.deliver_for_run(_run_result(terminal), scan_run_id="run-invalid-1"))
    second = run(service.deliver_for_run(_run_result(terminal), scan_run_id="run-invalid-2"))

    assert first.blocked == 1
    assert second.blocked_repeat == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT attempted_alert_type, telegram_status, blocked_reason, seen_count, last_scan_run_id
            FROM telegram_alert_attempts
            """
        ).fetchall()
    assert rows == [
        (
            TelegramAlertType.INVALIDATED.value,
            "blocked",
            "terminal_update_no_prior_public_alert",
            2,
            "run-invalid-2",
        )
    ]


def test_missing_telegram_credentials_skip_safely_and_persist_attempt(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = TelegramSender(bot_token=None, chat_id=None, signals_enabled=True)
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )

    summary = run(
        service.deliver_for_run(
            _run_result(_symbol(SetupLifecycleState.CONFIRMED, previous=SetupLifecycleState.TRIGGERED)),
            scan_run_id="run-001",
        )
    )
    repeated = run(
        service.deliver_for_run(
            _run_result(_symbol(SetupLifecycleState.CONFIRMED, previous=SetupLifecycleState.TRIGGERED)),
            scan_run_id="run-002",
        )
    )

    assert summary.skipped == 1
    assert repeated.duplicate == 1
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT telegram_status, error_message, seen_count, scan_run_id, last_scan_run_id FROM telegram_alert_attempts"
        ).fetchall()
    assert rows == [("skipped", "missing_telegram_credentials", 2, "run-001", "run-002")]


def test_blocked_incomplete_watchlist_persists_without_telegram_send(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="blocked-watch",
        diagnostics=_public_ready_watchlist_diagnostics(
            entry_low=NA,
            entry_high=NA,
            stop=NA,
            tp1=NA,
            tp2=NA,
            tp3=NA,
            rr_to_tp2=NA,
        ),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="run-blocked"))

    assert summary.blocked == 0
    assert summary.public_watchlist_audit.blocked_before_attempt == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM telegram_alert_attempts").fetchone()[0]
    assert count == 0


def test_below_min_public_grade_is_audited_without_telegram_send(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(),
        signal_id="below-grade-watch",
        setup_quality=_setup_quality_with_grade(
            SetupQualityGrade.B,
            quality_state=SetupQualityState.WATCHLIST_NEAR_MISS,
            quality_score=70,
        ),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="run-below-grade"))

    assert summary.blocked == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT telegram_status, attempted_alert_type, blocked_reason, scan_run_id
            FROM telegram_alert_attempts
            """
        ).fetchone()
    assert row[0] == "blocked"
    assert row[1] == TelegramAlertType.WATCHLIST.value
    assert "below_min_public_grade" in row[2]
    assert row[3] == "run-below-grade"


def test_blocked_target_integrity_persists_invalid_target_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="blocked-targets",
        diagnostics=_public_ready_watchlist_diagnostics(tp1=Decimal("99")),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="run-targets"))

    assert summary.blocked == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT telegram_status, attempted_alert_type, blocked_reason, invalid_target_fields, scan_run_id
            FROM telegram_alert_attempts
            """
        ).fetchone()
    assert row[0] == "blocked"
    assert row[1] == TelegramAlertType.WATCHLIST.value
    assert "target_integrity_failed" in row[2]
    assert "tp1" in row[3]
    assert row[4] == "run-targets"


def test_blocked_incomplete_watchlist_does_not_spam(tmp_path: Path, monkeypatch) -> None:
    timestamps = iter(
        (
            "2026-06-02T00:00:00+00:00",
            "2026-06-02T00:00:01+00:00",
            "2026-06-02T00:00:02+00:00",
            "2026-06-02T00:00:03+00:00",
            "2026-06-02T00:00:04+00:00",
            "2026-06-02T00:00:05+00:00",
        )
    )
    monkeypatch.setattr("app.alerts.telegram_lifecycle.now_utc_iso", lambda: next(timestamps))
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="blocked-watch",
        diagnostics=_public_ready_watchlist_diagnostics(
            entry_low=NA,
            entry_high=NA,
            stop=NA,
            tp1=NA,
            tp2=NA,
            tp3=NA,
            rr_to_tp2=NA,
        ),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
    )

    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="run-blocked"))
    second = run(service.deliver_for_run(_run_result(symbol), scan_run_id="run-blocked-repeat"))

    assert first.blocked == 0
    assert second.blocked == 0
    assert first.public_watchlist_audit.blocked_before_attempt == 1
    assert second.public_watchlist_audit.blocked_before_attempt == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM telegram_alert_attempts").fetchone()[0]
    assert count == 0


def test_blocked_watchlist_different_reason_creates_separate_audit_record(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    missing_plan = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="blocked-watch",
        diagnostics=_public_ready_watchlist_diagnostics(
            entry_low=NA,
            entry_high=NA,
            stop=NA,
            tp1=NA,
            tp2=NA,
            tp3=NA,
            rr_to_tp2=NA,
        ),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
    )
    missing_direction = _with_lifecycle_direction(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            signal_id="blocked-watch",
            diagnostics=_public_ready_watchlist_diagnostics(bias=NA, direction=NA),
        ),
        NA,
    )

    first = run(service.deliver_for_run(_run_result(missing_plan), scan_run_id="run-plan"))
    second = run(service.deliver_for_run(_run_result(missing_direction), scan_run_id="run-direction"))

    assert first.blocked == 0
    assert second.blocked == 0
    assert first.public_watchlist_audit.blocked_before_attempt == 1
    assert second.public_watchlist_audit.blocked_before_attempt == 1
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM telegram_alert_attempts").fetchone()[0]
    assert count == 0


def test_blocked_watchlist_different_signal_id_creates_separate_audit_record(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    diagnostics = _public_ready_watchlist_diagnostics(
        entry_low=NA,
        entry_high=NA,
        stop=NA,
        tp1=NA,
        tp2=NA,
        tp3=NA,
        rr_to_tp2=NA,
    )

    first = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.WATCHLISTED,
                    signal_id="blocked-watch-one",
                    diagnostics=diagnostics,
                    setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
                )
            ),
            scan_run_id="run-one",
        )
    )
    second = run(
        service.deliver_for_run(
            _run_result(
                _symbol(
                    SetupLifecycleState.WATCHLISTED,
                    signal_id="blocked-watch-two",
                    diagnostics=diagnostics,
                    setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
                )
            ),
            scan_run_id="run-two",
        )
    )

    assert first.blocked == 0
    assert second.blocked == 0
    assert first.public_watchlist_audit.blocked_before_attempt == 1
    assert second.public_watchlist_audit.blocked_before_attempt == 1
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM telegram_alert_attempts").fetchone()[0]
    assert count == 0


def test_blocked_different_alert_type_creates_separate_audit_record(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )
    watchlist = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="blocked-mixed",
        diagnostics=_public_ready_watchlist_diagnostics(
            entry_low=NA,
            entry_high=NA,
            stop=NA,
            tp1=NA,
            tp2=NA,
            tp3=NA,
            rr_to_tp2=NA,
        ),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
    )
    confirmed = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        signal_id="blocked-mixed",
        diagnostics=_diagnostics(rr_to_tp2=Decimal("2.79")),
        trade_idea=_trade_idea(best_rr=Decimal("2.79")),
    )

    watch_summary = run(service.deliver_for_run(_run_result(watchlist), scan_run_id="run-watch"))
    confirmed_summary = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-confirmed"))

    assert watch_summary.blocked == 0
    assert watch_summary.public_watchlist_audit.blocked_before_attempt == 1
    assert confirmed_summary.blocked == 0
    assert confirmed_summary.confirmed_alert_audit.blocked_before_attempt_by_reason == {"rr_below_min": 1}
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT attempted_alert_type, telegram_status, seen_count FROM telegram_alert_attempts ORDER BY id"
        ).fetchall()
    assert rows == []


def test_sent_watchlist_and_blocked_watchlist_remain_separate_audit_records(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    sent_watchlist = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="watch-separate",
        diagnostics=_public_ready_watchlist_diagnostics(),
    )
    blocked_watchlist = _symbol(
        SetupLifecycleState.WATCHLISTED,
        signal_id="watch-separate",
        diagnostics=_public_ready_watchlist_diagnostics(
            entry_low=NA,
            entry_high=NA,
            stop=NA,
            tp1=NA,
            tp2=NA,
            tp3=NA,
            rr_to_tp2=NA,
        ),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
    )

    sent = run(service.deliver_for_run(_run_result(sent_watchlist), scan_run_id="run-sent"))
    blocked = run(service.deliver_for_run(_run_result(blocked_watchlist), scan_run_id="run-blocked"))

    assert sent.sent == 1
    assert blocked.blocked == 0
    assert blocked.public_watchlist_audit.blocked_before_attempt == 1
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT attempted_alert_type, telegram_status, seen_count FROM telegram_alert_attempts ORDER BY id"
        ).fetchall()
    assert rows == [
        (TelegramAlertType.WATCHLIST.value, "sent", 1),
    ]


def test_confirmed_alert_is_blocked_when_planned_rr_is_below_min_rr() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(rr_to_tp2=Decimal("2.79181174")),
            trade_idea=_trade_idea(best_rr=Decimal("2.79181174")),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert "planned_rr_below_min" in decision.reason


def test_long_confirmed_blocks_when_stop_is_above_entry() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(stop=Decimal("103")),
            trade_idea=_trade_idea(stop_loss=Decimal("103")),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    _assert_target_integrity_blocked(decision, "stop_loss")


def test_long_confirmed_blocks_when_any_target_is_below_entry() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(tp2=Decimal("99")),
            trade_idea=_trade_idea(take_profit_targets=(Decimal("110"), Decimal("99"), Decimal("120"))),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    _assert_target_integrity_blocked(decision, "tp2")


def test_long_confirmed_blocks_when_targets_are_not_in_ascending_order() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(tp1=Decimal("112"), tp2=Decimal("111"), tp3=Decimal("120")),
            trade_idea=_trade_idea(take_profit_targets=(Decimal("112"), Decimal("111"), Decimal("120"))),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    _assert_target_integrity_blocked(decision, "tp_order")


def test_short_confirmed_blocks_when_stop_is_below_entry() -> None:
    symbol = _with_lifecycle_direction(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(
                bias="short",
                direction="short",
                stop=Decimal("95"),
                tp1=Decimal("90"),
                tp2=Decimal("85"),
                tp3=Decimal("80"),
            ),
            trade_idea=_trade_idea(
                direction="short",
                stop_loss=Decimal("95"),
                take_profit_targets=(Decimal("90"), Decimal("85"), Decimal("80")),
            ),
        ),
        "short",
    )

    decision = telegram_alert_decision_for_symbol(
        symbol,
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    _assert_target_integrity_blocked(decision, "stop_loss")


def test_short_confirmed_blocks_when_any_target_is_above_entry() -> None:
    symbol = _with_lifecycle_direction(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(
                bias="short",
                direction="short",
                stop=Decimal("105"),
                tp1=Decimal("90"),
                tp2=Decimal("85"),
                tp3=Decimal("103"),
            ),
            trade_idea=_trade_idea(
                direction="short",
                stop_loss=Decimal("105"),
                take_profit_targets=(Decimal("90"), Decimal("85"), Decimal("103")),
            ),
        ),
        "short",
    )

    decision = telegram_alert_decision_for_symbol(
        symbol,
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    _assert_target_integrity_blocked(decision, "tp3")


def test_short_confirmed_blocks_when_targets_are_not_in_descending_order() -> None:
    symbol = _with_lifecycle_direction(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(
                bias="short",
                direction="short",
                stop=Decimal("105"),
                tp1=Decimal("90"),
                tp2=Decimal("91"),
                tp3=Decimal("80"),
            ),
            trade_idea=_trade_idea(
                direction="short",
                stop_loss=Decimal("105"),
                take_profit_targets=(Decimal("90"), Decimal("91"), Decimal("80")),
            ),
        ),
        "short",
    )

    decision = telegram_alert_decision_for_symbol(
        symbol,
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    _assert_target_integrity_blocked(decision, "tp_order")


def test_watchlist_blocks_wrong_side_targets() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(tp1=Decimal("99")),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    _assert_target_integrity_blocked(decision, "tp1")
    assert decision.alert_type == TelegramAlertType.WATCHLIST


def test_watchlist_blocks_non_monotonic_targets() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(tp1=Decimal("110"), tp2=Decimal("109"), tp3=Decimal("120")),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        )
    )

    _assert_target_integrity_blocked(decision, "tp_order")
    assert decision.alert_type == TelegramAlertType.WATCHLIST


def test_confirmed_alert_is_blocked_when_opportunity_score_below_minimum() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            trade_idea=_trade_idea(opportunity_score=Decimal("79")),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert "opportunity_score_below_min" in decision.reason


def test_confirmed_alert_is_blocked_when_technical_score_below_threshold() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            technical_score=Decimal("49"),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert "technical_score_below_min" in decision.reason


def test_confirmed_signal_not_blocked_by_stale_rejection_reason() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            rejection_reason="Opportunity score is below scanner minimum.",
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED
    assert "rejection_reason_present" not in decision.reason


def test_confirmed_signal_not_blocked_by_historical_rejection_reasons() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            rejection_reasons=("Technical score is below 50.", "Opportunity score is below scanner minimum."),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED
    assert "rejection_reasons_present" not in decision.reason


def test_confirmed_signal_blocks_active_rejection_reason() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(active_rejection_reason="structural_breakdown"),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert decision.alert_type == TelegramAlertType.SIGNAL_CONFIRMED
    assert "confirmed_active_rejection_reason" in decision.reason
    assert "rejection_reason_present" not in decision.reason


def test_confirmed_alert_is_blocked_when_trade_idea_missing() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            trade_idea=None,
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert "trade_idea_missing" in decision.reason


def test_confirmed_signal_blocks_active_invalidation() -> None:
    bad_text = "Technical score is below 50.; Opportunity score 79.00000000 is below scanner minimum 80."
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(invalidation=bad_text),
            trade_idea=_trade_idea(invalidation=bad_text),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert "confirmed_active_invalidation" in decision.reason
    assert "invalidation_contains_rejection_reason" in decision.reason


def test_confirmed_alert_is_blocked_when_invalidation_is_rejection_text() -> None:
    test_confirmed_signal_blocks_active_invalidation()


def test_confirmed_signal_blocks_reject_grade() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            setup_quality=_setup_quality(SetupQualityState.REJECTED_NO_EDGE, quality_score=40),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert "confirmed_grade_below_min" in decision.reason


def test_confirmed_signal_blocks_rr_below_min() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(rr_to_tp2=Decimal("2.49")),
            trade_idea=_trade_idea(best_rr=Decimal("2.49")),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("2.5"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert "confirmed_rr_below_min:2.49<2.5" in decision.reason


def test_confirmed_signal_blocks_missing_entry_zone() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(entry_low=NA, entry_high=NA, entry_zone=NA, watch_zone=NA, entry=NA),
            trade_idea=_trade_idea(entry_low=None, entry_high=None),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert "confirmed_missing_entry_zone" in decision.reason


def test_confirmed_signal_blocks_missing_stop() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(stop=NA, stop_loss=NA),
            trade_idea=_trade_idea(stop_loss=None),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert "confirmed_missing_stop" in decision.reason


def test_allousdt_style_contradictory_confirmed_alert_is_blocked() -> None:
    rejection_text = "Technical score is below 50.; Opportunity score 79.00000000 is below scanner minimum 80."
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(rr_to_tp2=Decimal("2.79181174"), invalidation=rejection_text),
            trade_idea=_trade_idea(
                best_rr=Decimal("2.79181174"),
                opportunity_score=Decimal("79"),
                invalidation=rejection_text,
            ),
            technical_score=Decimal("49"),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert "planned_rr_below_min:2.79181174<3" in decision.reason
    assert "opportunity_score_below_min:79<80" in decision.reason
    assert "technical_score_below_min:49<50" in decision.reason
    assert "invalidation_contains_rejection_reason" in decision.reason


def test_no_setup_lifecycle_watchlist_does_not_send_watchlist_alert() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            rejection_reason="No valid Liquidity-Grab Pullback setup.",
            setup_quality=_setup_quality(SetupQualityState.REJECTED_NO_EDGE, quality_score=40),
        )
    )

    assert decision.eligible is False
    assert "below_min_public_grade" in decision.reason


def test_confluence_from_raw_derivatives_context_is_public_text() -> None:
    symbol_result = _symbol(
        SetupLifecycleState.CONFIRMED,
        previous=SetupLifecycleState.TRIGGERED,
        diagnostics=_diagnostics(
            funding_context={
                "funding_rate": Decimal("0.00010000"),
                "funding_status": "normal",
                "funding_extreme": False,
            },
            oi_context={
                "open_interest": Decimal("123456.789"),
                "oi_direction": "falling",
            },
            derivatives_supports_trade=True,
            volume_profile_source="estimated_from_candles",
        ),
    )

    message = telegram_signal_message_from_symbol(symbol_result)
    text = format_telegram_signal_message(TelegramAlertType.SIGNAL_CONFIRMED, message)

    assert "🧠 Why this setup matters" in text
    assert "Technical context:" in text
    for forbidden in ("Decimal(", "{", "}", "true", "false", "funding_rate:", "open_interest:"):
        assert forbidden not in text


def test_blocked_confirmed_alert_prefilter_records_summary_without_attempt(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
        min_rr=Decimal("3"),
        min_score_for_idea=Decimal("80"),
    )
    result = _run_result(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_diagnostics(rr_to_tp2=Decimal("2.79")),
            trade_idea=_trade_idea(best_rr=Decimal("2.79")),
        )
    )

    summary = run(service.deliver_for_run(result, scan_run_id="run-001"))

    assert summary.blocked == 0
    assert summary.confirmed_alert_audit.confirmed_candidates_seen == 1
    assert summary.confirmed_alert_audit.confirmed_prefilter_passed == 0
    assert summary.confirmed_alert_audit.signal_confirmed_attempts_created == 0
    assert summary.confirmed_alert_audit.blocked_before_attempt_by_reason == {"rr_below_min": 1}
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM telegram_alert_attempts").fetchone()[0]
    assert count == 0


def test_each_alert_type_is_not_sent_twice(tmp_path: Path) -> None:
    cases = (
        (
            TelegramAlertType.WATCHLIST,
            _symbol(
                SetupLifecycleState.WATCHLISTED,
                signal_id="sig-watchlist",
                diagnostics=_public_ready_watchlist_diagnostics(),
            ),
            False,
        ),
        (
            TelegramAlertType.SIGNAL_CONFIRMED,
            _symbol(SetupLifecycleState.CONFIRMED, previous=SetupLifecycleState.TRIGGERED, signal_id="sig-confirmed"),
            False,
        ),
        (
            TelegramAlertType.LIMIT_HIT,
            _symbol(SetupLifecycleState.MANAGING, previous=SetupLifecycleState.EXECUTING, signal_id="sig-limit"),
            True,
        ),
        (
            TelegramAlertType.TP1_HIT,
            _symbol(
                SetupLifecycleState.TP_HIT,
                previous=SetupLifecycleState.MANAGING,
                diagnostics=_diagnostics(outcome_status="tp1_hit"),
                signal_id="sig-tp1",
            ),
            True,
        ),
        (
            TelegramAlertType.TP2_HIT,
            _symbol(
                SetupLifecycleState.TP_HIT,
                previous=SetupLifecycleState.MANAGING,
                diagnostics=_diagnostics(outcome_status="tp2_hit"),
                signal_id="sig-tp2",
            ),
            True,
        ),
        (
            TelegramAlertType.TP3_HIT,
            _symbol(
                SetupLifecycleState.TP_HIT,
                previous=SetupLifecycleState.MANAGING,
                diagnostics=_diagnostics(outcome_status="tp3_hit"),
                signal_id="sig-tp3",
            ),
            True,
        ),
        (
            TelegramAlertType.SL_HIT,
            _symbol(SetupLifecycleState.SL_HIT, previous=SetupLifecycleState.MANAGING, signal_id="sig-sl"),
            True,
        ),
        (
            TelegramAlertType.INVALIDATED,
            _symbol(SetupLifecycleState.INVALIDATED, previous=SetupLifecycleState.CONFIRMED, signal_id="sig-invalidated"),
            True,
        ),
        (
            TelegramAlertType.NO_LONGER_TRACKING,
            _symbol(SetupLifecycleState.COOLDOWN, previous=SetupLifecycleState.TRIGGERED, signal_id="sig-cooldown"),
            True,
        ),
    )

    for alert_type, symbol_result, needs_prior_active in cases:
        db_path = tmp_path / f"{alert_type.value.lower()}.db"
        sender = FakeSender()
        signal_id = symbol_result.lifecycle_state.lifecycle_id
        if needs_prior_active:
            _seed_prior_active_alert(db_path, signal_id=signal_id)
        if alert_type in {TelegramAlertType.TP1_HIT, TelegramAlertType.TP2_HIT, TelegramAlertType.TP3_HIT}:
            tp_price = {
                TelegramAlertType.TP1_HIT: Decimal("110"),
                TelegramAlertType.TP2_HIT: Decimal("115"),
                TelegramAlertType.TP3_HIT: Decimal("120"),
            }[alert_type]
            symbol_result = symbol_result.model_copy(update={"current_price": tp_price})
        if alert_type == TelegramAlertType.SL_HIT:
            symbol_result = symbol_result.model_copy(update={"current_price": Decimal("95")})
        service = TelegramLifecycleDeliveryService(
            database_path=db_path,
            settings=Settings(_env_file=None),
            sender=sender,
            min_rr=Decimal("3"),
            min_score_for_idea=Decimal("80"),
        )

        first = run(service.deliver_for_run(_run_result(symbol_result), scan_run_id="run-001"))
        second = run(service.deliver_for_run(_run_result(symbol_result), scan_run_id="run-001"))

        assert first.sent == 1, alert_type
        assert second.duplicate == 1, alert_type
        assert len(sender.messages) == 1, alert_type


def test_sent_watchlist_limit_zone_touch_requires_prior_public_signal(tmp_path: Path) -> None:
    db_path = tmp_path / "limit-hit.db"
    signal_id = "watch-limit-hit"
    _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )
    symbol = _outcome_scan_symbol(signal_id=signal_id, high=Decimal("101"), low=Decimal("99"))

    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="limit-1"))
    second = run(service.deliver_for_run(_run_result(symbol), scan_run_id="limit-2"))

    assert first.sent == 0
    assert second.sent == 0
    assert sender.messages == []
    rows = _watchlist_outcome_rows(db_path)
    assert any(row[3] == "outcome_tracking_limit_hit_requires_prior_public_signal" for row in rows)


def test_limit_hit_decision_is_allowed_only_after_prior_public_signal() -> None:
    signal_id = "watch-limit-hit-confirmed"
    prior = TelegramAlertAttemptRecord(
        signal_id=signal_id,
        symbol="BTCUSDT",
        direction="long",
        previous_state="TRIGGERED",
        new_state="CONFIRMED",
        alert_type=TelegramAlertType.SIGNAL_CONFIRMED.value,
        lifecycle_state="CONFIRMED",
        sent_at="2026-06-02T00:00:00Z",
        telegram_status="sent",
        message_hash="hash",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="115",
        tp3="120",
    )
    symbol = _symbol(
        SetupLifecycleState.MANAGING,
        previous=SetupLifecycleState.EXECUTING,
        signal_id=signal_id,
        diagnostics=_diagnostics(entry_low=Decimal("200"), entry_high=Decimal("202"), stop=Decimal("190")),
    )

    decision = telegram_alert_decision_for_symbol(
        symbol,
        previously_active_sent=True,
        prior_public_alert=prior,
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.LIMIT_HIT
    assert decision.message is not None
    assert decision.message.entry_low == "100"
    assert decision.message.entry_high == "102"


def test_watchlist_long_stored_entry_zone_touch_sends_limit_hit_once_and_becomes_active(tmp_path: Path) -> None:
    db_path = tmp_path / "lifecycle-long-limit.db"
    signal_id = "life-watch-long"
    _store_lifecycle_record(db_path, _stored_plan_record(SetupLifecycleState.WATCHLISTED, signal_id=signal_id))
    _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
    scan = _outcome_scan_symbol(
        signal_id=signal_id,
        high=Decimal("101"),
        low=Decimal("101"),
        diagnostics=_public_ready_watchlist_diagnostics(
            entry_low=Decimal("200"),
            entry_high=Decimal("202"),
            stop=Decimal("190"),
            tp1=Decimal("210"),
            tp2=Decimal("215"),
            tp3=Decimal("220"),
            rr_to_tp2=Decimal("3"),
        ),
    )

    first_run = apply_lifecycle_to_run_result(
        _run_result(scan),
        database_path=db_path,
        scan_run_id="touch-1",
        now="2026-06-02T00:05:00+00:00",
    )
    first_symbol = first_run.results[0]
    assert first_symbol.lifecycle_transition is not None
    assert first_symbol.lifecycle_transition.from_state == SetupLifecycleState.WATCHLISTED
    assert first_symbol.lifecycle_transition.to_state == SetupLifecycleState.TRIGGERED
    assert first_symbol.lifecycle_transition.reason == SetupTransitionReason.ENTRY_ZONE_TOUCHED
    assert first_symbol.lifecycle_state is not None
    assert first_symbol.lifecycle_state.entry_low == "100"
    assert first_symbol.lifecycle_state.entry_high == "102"

    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )
    first_delivery = run(service.deliver_for_run(first_run, scan_run_id="touch-1"))

    second_run = apply_lifecycle_to_run_result(
        _run_result(scan),
        database_path=db_path,
        scan_run_id="touch-2",
        now="2026-06-02T00:10:00+00:00",
    )
    second_delivery = run(service.deliver_for_run(second_run, scan_run_id="touch-2"))

    assert first_delivery.sent == 0
    assert second_delivery.sent == 0
    assert sender.messages == []
    rows = _watchlist_outcome_rows(db_path)
    assert any(row[3] == "outcome_tracking_limit_hit_requires_prior_public_signal" for row in rows)

    active = load_active_public_signals(project_root=tmp_path, database_path=db_path, limit=10)
    assert active.total == 0

    with sqlite3.connect(db_path) as connection:
        row_count = connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0]
    assert row_count == 1


def test_watchlist_short_stored_entry_zone_touch_sends_limit_hit_once(tmp_path: Path) -> None:
    db_path = tmp_path / "lifecycle-short-limit.db"
    signal_id = "life-watch-short"
    _store_lifecycle_record(
        db_path,
        _stored_plan_record(SetupLifecycleState.STALKING, signal_id=signal_id, direction="short"),
    )
    _seed_prior_active_alert(
        db_path,
        signal_id=signal_id,
        alert_type=TelegramAlertType.WATCHLIST,
        direction="short",
    )
    scan = _outcome_scan_symbol(
        signal_id=signal_id,
        direction="short",
        high=Decimal("101"),
        low=Decimal("101"),
        diagnostics=_public_ready_watchlist_diagnostics(
            bias="short",
            direction="short",
            entry_low=Decimal("200"),
            entry_high=Decimal("202"),
            stop=Decimal("210"),
            tp1=Decimal("190"),
            tp2=Decimal("185"),
            tp3=Decimal("180"),
            rr_to_tp2=Decimal("3"),
        ),
    )

    lifecycle_run = apply_lifecycle_to_run_result(
        _run_result(scan),
        database_path=db_path,
        scan_run_id="short-touch",
        now="2026-06-02T00:05:00+00:00",
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )
    delivery = run(service.deliver_for_run(lifecycle_run, scan_run_id="short-touch"))

    assert delivery.sent == 0
    assert sender.messages == []
    rows = _watchlist_outcome_rows(db_path)
    assert not any(row[0] == TelegramAlertType.LIMIT_HIT.value and row[1] == "sent" for row in rows)


def test_watchlist_does_not_send_tp_or_sl_before_limit_hit(tmp_path: Path) -> None:
    db_path = tmp_path / "no-limit-no-outcome.db"
    signal_id = "watch-no-limit"
    _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )
    symbol = _outcome_scan_symbol(signal_id=signal_id, high=Decimal("111"), low=Decimal("105"))

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="no-limit"))

    assert summary.sent == 0
    assert sender.messages == []
    rows = _watchlist_outcome_rows(db_path)
    assert not any(row[0] in {TelegramAlertType.TP1_HIT.value, TelegramAlertType.SL_HIT.value} for row in rows)
    assert any(row[3] == "outcome_tracking_not_limit_hit_yet" for row in rows)


def test_watchlist_same_candle_entry_and_target_sends_only_limit_and_audits_ambiguity(tmp_path: Path) -> None:
    db_path = tmp_path / "same-candle.db"
    signal_id = "watch-same-candle"
    _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )
    symbol = _outcome_scan_symbol(signal_id=signal_id, high=Decimal("111"), low=Decimal("99"))

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="same-candle"))

    assert summary.sent == 0
    assert sender.messages == []
    rows = _watchlist_outcome_rows(db_path)
    assert not any(row[0] == TelegramAlertType.TP1_HIT.value for row in rows)
    assert any(row[3] == "outcome_tracking_limit_hit_requires_prior_public_signal" for row in rows)


def test_watchlist_target_touch_rules_are_direction_aware() -> None:
    short_above_tp1 = WatchlistCandleSnapshot(high=Decimal("0.359"), low=Decimal("0.356"))
    short_exact_tp1 = WatchlistCandleSnapshot(high=Decimal("0.359"), low=Decimal("0.3526"))
    short_below_tp1 = WatchlistCandleSnapshot(high=Decimal("0.359"), low=Decimal("0.351"))
    long_below_tp1 = WatchlistCandleSnapshot(high=Decimal("109.99"), low=Decimal("103"))
    long_exact_tp1 = WatchlistCandleSnapshot(high=Decimal("110"), low=Decimal("103"))

    assert not _target_touched(short_above_tp1, side="short", target=Decimal("0.3526"))
    assert _target_touched(short_exact_tp1, side="short", target=Decimal("0.3526"))
    assert _target_touched(short_below_tp1, side="short", target=Decimal("0.3526"))
    assert not _target_touched(long_below_tp1, side="long", target=Decimal("110"))
    assert _target_touched(long_exact_tp1, side="long", target=Decimal("110"))


def test_watchlist_stop_touch_rules_are_direction_aware() -> None:
    long_above_sl = WatchlistCandleSnapshot(high=Decimal("101"), low=Decimal("95.01"))
    long_exact_sl = WatchlistCandleSnapshot(high=Decimal("101"), low=Decimal("95"))
    short_below_sl = WatchlistCandleSnapshot(high=Decimal("104.99"), low=Decimal("100"))
    short_exact_sl = WatchlistCandleSnapshot(high=Decimal("105"), low=Decimal("100"))

    assert not _stop_touched(long_above_sl, side="long", stop_loss=Decimal("95"))
    assert _stop_touched(long_exact_sl, side="long", stop_loss=Decimal("95"))
    assert not _stop_touched(short_below_sl, side="short", stop_loss=Decimal("105"))
    assert _stop_touched(short_exact_sl, side="short", stop_loss=Decimal("105"))


def _watchlist_service_after_limit_hit(
    db_path: Path,
    *,
    signal_id: str,
    direction: str = "long",
    symbol: str = "BTCUSDT",
) -> tuple[FakeSender, TelegramLifecycleDeliveryService]:
    _seed_prior_active_alert(
        db_path,
        signal_id=signal_id,
        alert_type=TelegramAlertType.WATCHLIST,
        symbol=symbol,
        direction=direction,
    )
    _seed_prior_active_alert(
        db_path,
        signal_id=signal_id,
        alert_type=TelegramAlertType.SIGNAL_CONFIRMED,
        symbol=symbol,
        direction=direction,
    )
    sender = FakeSender()
    _seed_prior_active_alert(
        db_path,
        signal_id=signal_id,
        alert_type=TelegramAlertType.LIMIT_HIT,
        symbol=symbol,
        direction=direction,
    )
    sender.messages.append(
        format_telegram_signal_message(
            TelegramAlertType.LIMIT_HIT,
            telegram_signal_message_from_symbol(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    symbol=symbol,
                    direction=direction,
                    high=Decimal("101"),
                    low=Decimal("99"),
                )
            ),
        )
    )
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )
    return sender, service


def test_short_tp_does_not_trigger_when_current_price_is_above_tp(tmp_path: Path) -> None:
    db_path = tmp_path / "short-tp-current-above.db"
    signal_id = "short-tp-current-above"
    sender, service = _watchlist_service_after_limit_hit(db_path, signal_id=signal_id, direction="short")

    attempt = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    direction="short",
                    high=Decimal("99"),
                    low=Decimal("94"),
                    current_price=Decimal("96"),
                )
            ),
            scan_run_id="short-tp-current-above",
        )
    )

    assert attempt.sent == 0
    assert len(sender.messages) == 1
    rows = _watchlist_outcome_rows(db_path)
    assert any(
        row[1] == "blocked"
        and row[2] == TelegramAlertType.TP1_HIT.value
        and "tp_sl_price_condition_false" in row[3]
        for row in rows
    )


def test_long_tp_does_not_trigger_when_current_price_is_below_tp(tmp_path: Path) -> None:
    db_path = tmp_path / "long-tp-current-below.db"
    signal_id = "long-tp-current-below"
    sender, service = _watchlist_service_after_limit_hit(db_path, signal_id=signal_id)

    attempt = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("111"),
                    low=Decimal("103"),
                    current_price=Decimal("109"),
                )
            ),
            scan_run_id="long-tp-current-below",
        )
    )

    assert attempt.sent == 0
    assert len(sender.messages) == 1
    rows = _watchlist_outcome_rows(db_path)
    assert any(
        row[1] == "blocked"
        and row[2] == TelegramAlertType.TP1_HIT.value
        and "tp_sl_price_condition_false" in row[3]
        for row in rows
    )


def test_short_sl_does_not_trigger_when_current_price_is_below_sl(tmp_path: Path) -> None:
    db_path = tmp_path / "short-sl-current-below.db"
    signal_id = "short-sl-current-below"
    sender, service = _watchlist_service_after_limit_hit(db_path, signal_id=signal_id, direction="short")

    attempt = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    direction="short",
                    high=Decimal("106"),
                    low=Decimal("100"),
                    current_price=Decimal("104"),
                )
            ),
            scan_run_id="short-sl-current-below",
        )
    )

    assert attempt.sent == 0
    assert len(sender.messages) == 1
    rows = _watchlist_outcome_rows(db_path)
    assert any(
        row[1] == "blocked"
        and row[2] == TelegramAlertType.SL_HIT.value
        and "tp_sl_price_condition_false" in row[3]
        for row in rows
    )


def test_long_sl_does_not_trigger_when_current_price_is_above_sl(tmp_path: Path) -> None:
    db_path = tmp_path / "long-sl-current-above.db"
    signal_id = "long-sl-current-above"
    sender, service = _watchlist_service_after_limit_hit(db_path, signal_id=signal_id)

    attempt = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("101"),
                    low=Decimal("94"),
                    current_price=Decimal("96"),
                )
            ),
            scan_run_id="long-sl-current-above",
        )
    )

    assert attempt.sent == 0
    assert len(sender.messages) == 1
    rows = _watchlist_outcome_rows(db_path)
    assert any(
        row[1] == "blocked"
        and row[2] == TelegramAlertType.SL_HIT.value
        and "tp_sl_price_condition_false" in row[3]
        for row in rows
    )


def test_watchlist_current_price_at_tp_cannot_send_tp_before_entry_zone_touched(tmp_path: Path) -> None:
    db_path = tmp_path / "watchlist-tp-before-entry.db"
    signal_id = "watchlist-tp-before-entry"
    _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )

    attempt = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("111"),
                    low=Decimal("105"),
                    current_price=Decimal("111"),
                )
            ),
            scan_run_id="watchlist-tp-before-entry",
        )
    )

    assert attempt.sent == 0
    assert sender.messages == []
    rows = _watchlist_outcome_rows(db_path)
    assert not any(row[2] == TelegramAlertType.TP1_HIT.value and row[1] == "sent" for row in rows)
    assert any(row[3] == "outcome_tracking_not_limit_hit_yet" for row in rows)


def test_tp_sl_symbol_mismatch_blocks_watchlist_alert(tmp_path: Path) -> None:
    db_path = tmp_path / "tp-symbol-mismatch.db"
    signal_id = "opus-symbol-mismatch"
    sender, service = _watchlist_service_after_limit_hit(
        db_path,
        signal_id=signal_id,
        symbol="OPUSDT",
    )

    attempt = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    symbol="OPUSDT",
                    high=Decimal("111"),
                    low=Decimal("103"),
                    current_price=Decimal("110"),
                    price_symbol="BTCUSDT",
                )
            ),
            scan_run_id="tp-symbol-mismatch",
        )
    )

    assert attempt.sent == 0
    assert len(sender.messages) == 1
    rows = _watchlist_outcome_rows(db_path)
    assert any(
        row[1] == "blocked"
        and row[2] == TelegramAlertType.TP1_HIT.value
        and "tp_sl_symbol_mismatch" in row[3]
        for row in rows
    )


def test_missing_or_stale_live_price_blocks_watchlist_tp_alert(tmp_path: Path) -> None:
    missing_db_path = tmp_path / "missing-live-price.db"
    missing_signal_id = "missing-live-price"
    missing_sender, missing_service = _watchlist_service_after_limit_hit(missing_db_path, signal_id=missing_signal_id)

    missing = run(
        missing_service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=missing_signal_id,
                    high=Decimal("111"),
                    low=Decimal("103"),
                )
            ),
            scan_run_id="missing-live-price",
        )
    )

    stale_db_path = tmp_path / "stale-live-price.db"
    stale_signal_id = "stale-live-price"
    stale_sender, stale_service = _watchlist_service_after_limit_hit(stale_db_path, signal_id=stale_signal_id)
    stale = run(
        stale_service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=stale_signal_id,
                    high=Decimal("111"),
                    low=Decimal("103"),
                    current_price=Decimal("110"),
                    price_stale=True,
                )
            ),
            scan_run_id="stale-live-price",
        )
    )

    assert missing.sent == 0
    assert stale.sent == 0
    assert len(missing_sender.messages) == 1
    assert len(stale_sender.messages) == 1
    missing_rows = _watchlist_outcome_rows(missing_db_path)
    stale_rows = _watchlist_outcome_rows(stale_db_path)
    assert any(row[1] == "blocked" and row[2] == TelegramAlertType.TP1_HIT.value and "tp_sl_missing_live_price" in row[3] for row in missing_rows)
    assert any(row[1] == "blocked" and row[2] == TelegramAlertType.TP1_HIT.value and "tp_sl_stale_live_price" in row[3] for row in stale_rows)


def test_valid_short_tp_triggers_only_when_current_price_is_at_or_below_tp(tmp_path: Path) -> None:
    db_path = tmp_path / "valid-short-tp-current.db"
    signal_id = "valid-short-tp-current"
    sender, service = _watchlist_service_after_limit_hit(db_path, signal_id=signal_id, direction="short")

    attempt = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    direction="short",
                    high=Decimal("99"),
                    low=Decimal("95"),
                    current_price=Decimal("95"),
                )
            ),
            scan_run_id="valid-short-tp-current",
        )
    )

    assert attempt.sent == 1
    assert len(sender.messages) == 2
    assert "TAKE PROFIT HIT" in sender.messages[1]
    rows = _watchlist_outcome_rows(db_path)
    assert any(row[0] == TelegramAlertType.TP1_HIT.value and row[1] == "sent" for row in rows)


def test_valid_long_tp_triggers_only_when_current_price_is_at_or_above_tp(tmp_path: Path) -> None:
    db_path = tmp_path / "valid-long-tp-current.db"
    signal_id = "valid-long-tp-current"
    sender, service = _watchlist_service_after_limit_hit(db_path, signal_id=signal_id)

    attempt = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("110"),
                    low=Decimal("103"),
                    current_price=Decimal("110"),
                )
            ),
            scan_run_id="valid-long-tp-current",
        )
    )

    assert attempt.sent == 1
    assert len(sender.messages) == 2
    assert "TAKE PROFIT HIT" in sender.messages[1]
    rows = _watchlist_outcome_rows(db_path)
    assert any(row[0] == TelegramAlertType.TP1_HIT.value and row[1] == "sent" for row in rows)


def test_short_watchlist_uses_stored_tp1_not_recalculated_current_target(tmp_path: Path) -> None:
    db_path = tmp_path / "ondo-short-target.db"
    signal_id = "ondo-watch-short"
    stored_plan = {
        "entry_low": Decimal("0.3589"),
        "entry_high": Decimal("0.36066"),
        "stop_loss": Decimal("0.36521"),
        "tp1": Decimal("0.3526"),
        "tp2": Decimal("0.34854"),
        "tp3": Decimal("0.3448"),
    }
    current_recalculated = _public_ready_watchlist_diagnostics(
        bias="short",
        direction="short",
        entry_low=Decimal("0.3589"),
        entry_high=Decimal("0.36066"),
        stop=Decimal("0.36521"),
        tp1=Decimal("0.356"),
        tp2=Decimal("0.35"),
        tp3=Decimal("0.345"),
        rr_to_tp2=Decimal("3"),
    )
    _seed_prior_active_alert(
        db_path,
        signal_id=signal_id,
        symbol="ONDOUSDT",
        alert_type=TelegramAlertType.WATCHLIST,
        direction="short",
        **stored_plan,
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )

    run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    symbol="ONDOUSDT",
                    direction="short",
                    high=Decimal("0.36"),
                    low=Decimal("0.359"),
                    diagnostics=current_recalculated,
                )
            ),
            scan_run_id="ondo-limit",
        )
    )
    tp_attempt = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    symbol="ONDOUSDT",
                    direction="short",
                    high=Decimal("0.359"),
                    low=Decimal("0.356"),
                    current_price=Decimal("0.356"),
                    diagnostics=current_recalculated,
                )
            ),
            scan_run_id="ondo-current-above-stored-tp1",
        )
    )

    assert tp_attempt.sent == 0
    assert not any("TP1 HIT" in message for message in sender.messages)
    rows = _watchlist_outcome_rows(db_path)
    assert not any(row[0] == TelegramAlertType.TP1_HIT.value and row[1] == "sent" for row in rows)


def test_long_watchlist_tracks_tp_sequence_after_limit_hit(tmp_path: Path) -> None:
    db_path = tmp_path / "long-tps.db"
    signal_id = "watch-long-tps"
    sender, service = _watchlist_service_after_limit_hit(db_path, signal_id=signal_id)
    tp1 = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("110"),
                    low=Decimal("103"),
                    current_price=Decimal("110"),
                )
            ),
            scan_run_id="tp1",
        )
    )
    tp2 = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("116"),
                    low=Decimal("103"),
                    current_price=Decimal("116"),
                )
            ),
            scan_run_id="tp2",
        )
    )
    tp3 = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("121"),
                    low=Decimal("103"),
                    current_price=Decimal("121"),
                )
            ),
            scan_run_id="tp3",
        )
    )
    repeat = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("121"),
                    low=Decimal("103"),
                    current_price=Decimal("121"),
                )
            ),
            scan_run_id="tp3-repeat",
        )
    )

    assert (tp1.sent, tp2.sent, tp3.sent, repeat.sent) == (1, 1, 1, 0)
    assert "TAKE PROFIT HIT — BTCUSDT" in sender.messages[1]
    assert "TAKE PROFIT HIT — BTCUSDT" in sender.messages[2]
    assert "TAKE PROFIT HIT — BTCUSDT" in sender.messages[3]
    assert "Full target sequence completed." in sender.messages[3]
    rows = [row[0] for row in _watchlist_outcome_rows(db_path)]
    assert rows.count(TelegramAlertType.TP1_HIT.value) == 1
    assert rows.count(TelegramAlertType.TP2_HIT.value) == 1
    assert rows.count(TelegramAlertType.TP3_HIT.value) == 1


def test_long_watchlist_tracks_sl_after_limit_hit_even_when_terminal_updates_disabled(tmp_path: Path) -> None:
    db_path = tmp_path / "long-sl.db"
    signal_id = "watch-long-sl"
    sender, service = _watchlist_service_after_limit_hit(db_path, signal_id=signal_id)
    sl = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("101"),
                    low=Decimal("94"),
                    current_price=Decimal("94"),
                )
            ),
            scan_run_id="sl",
        )
    )
    repeat = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("101"),
                    low=Decimal("94"),
                    current_price=Decimal("94"),
                )
            ),
            scan_run_id="sl-repeat",
        )
    )

    assert sl.sent == 1
    assert repeat.sent == 0
    assert "STOP HIT — BTCUSDT" in sender.messages[1]
    assert "Small controlled losses protect us for the next A-grade opportunity." in sender.messages[1]


def test_short_watchlist_tracks_tp_and_sl_rules_after_limit_hit(tmp_path: Path) -> None:
    db_path = tmp_path / "short-tp.db"
    tp_signal = "watch-short-tp"
    sender, service = _watchlist_service_after_limit_hit(db_path, signal_id=tp_signal, direction="short")
    tp = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=tp_signal,
                    direction="short",
                    high=Decimal("99"),
                    low=Decimal("94"),
                    current_price=Decimal("94"),
                )
            ),
            scan_run_id="short-tp1",
        )
    )

    sl_db_path = tmp_path / "short-sl.db"
    sl_signal = "watch-short-sl"
    sl_sender, sl_service = _watchlist_service_after_limit_hit(sl_db_path, signal_id=sl_signal, direction="short")
    sl = run(
        sl_service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=sl_signal,
                    direction="short",
                    high=Decimal("106"),
                    low=Decimal("100"),
                    current_price=Decimal("106"),
                )
            ),
            scan_run_id="short-sl",
        )
    )

    assert tp.sent == 1
    assert sl.sent == 1
    assert any("TAKE PROFIT HIT" in message for message in sender.messages)
    assert any("STOP HIT" in message for message in sl_sender.messages)


def test_missing_targets_block_limit_and_sl_tracking(tmp_path: Path) -> None:
    db_path = tmp_path / "missing-targets.db"
    signal_id = "watch-missing-targets"
    diagnostics = _public_ready_watchlist_diagnostics(tp1=NA, tp2=NA, tp3=NA)
    _seed_prior_active_alert(
        db_path,
        signal_id=signal_id,
        alert_type=TelegramAlertType.WATCHLIST,
        tp1=NA,
        tp2=NA,
        tp3=NA,
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )

    limit = run(
        service.deliver_for_run(
            _run_result(_outcome_scan_symbol(signal_id=signal_id, diagnostics=diagnostics)),
            scan_run_id="missing-targets-limit",
        )
    )
    sl = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("101"),
                    low=Decimal("94"),
                    current_price=Decimal("94"),
                    diagnostics=diagnostics,
                )
            ),
            scan_run_id="missing-targets-sl",
        )
    )

    assert limit.sent == 0
    assert sl.sent == 0
    assert sender.messages == []
    rows = _watchlist_outcome_rows(db_path)
    assert not any(row[0] == TelegramAlertType.LIMIT_HIT.value and row[1] == "sent" for row in rows)
    assert not any(row[0] in {TelegramAlertType.TP1_HIT.value, TelegramAlertType.TP2_HIT.value} for row in rows)


def test_missing_entry_blocks_watchlist_outcome_tracking_and_compacts(tmp_path: Path) -> None:
    db_path = tmp_path / "missing-entry.db"
    signal_id = "watch-missing-entry"
    diagnostics = _public_ready_watchlist_diagnostics(entry_low=NA, entry_high=NA, watch_zone=NA, entry_zone=NA)
    _seed_prior_active_alert(
        db_path,
        signal_id=signal_id,
        alert_type=TelegramAlertType.WATCHLIST,
        price_level=NA,
        entry_low=NA,
        entry_high=NA,
    )
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )
    symbol = _outcome_scan_symbol(signal_id=signal_id, diagnostics=diagnostics)

    first = run(service.deliver_for_run(_run_result(symbol), scan_run_id="missing-entry-1"))
    second = run(service.deliver_for_run(_run_result(symbol), scan_run_id="missing-entry-2"))

    assert first.sent == 0
    assert second.sent == 0
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT blocked_reason, seen_count
            FROM telegram_alert_attempts
            WHERE attempted_alert_type = 'WATCHLIST_OUTCOME_TRACKING'
            """
        ).fetchone()
    assert row == ("outcome_tracking_missing_entry", 2)


def test_watchlist_terminal_updates_are_suppressed_by_default_but_internal_record_remains(tmp_path: Path) -> None:
    db_path = tmp_path / "terminal-suppressed.db"
    signal_id = "watch-terminal-suppressed"
    _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
    terminal_symbol = _symbol(
        SetupLifecycleState.INVALIDATED,
        previous=SetupLifecycleState.WATCHLISTED,
        signal_id=signal_id,
    )
    assert terminal_symbol.lifecycle_state is not None
    _store_lifecycle_record(db_path, terminal_symbol.lifecycle_state)
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=AppSettings(_env_file=None),
        sender=sender,
    )

    summary = run(service.deliver_for_run(_run_result(terminal_symbol), scan_run_id="terminal-suppressed"))

    assert summary.sent == 0
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        attempt = connection.execute(
            """
            SELECT attempted_alert_type, telegram_status, blocked_reason
            FROM telegram_alert_attempts
            WHERE attempted_alert_type = 'INVALIDATED'
            """
        ).fetchone()
    assert attempt == (TelegramAlertType.INVALIDATED.value, "skipped", "public_watchlist_terminal_updates_disabled")
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        record = repository.get_record_by_lifecycle_id(signal_id)
    assert record is not None
    assert record.current_state == SetupLifecycleState.INVALIDATED
