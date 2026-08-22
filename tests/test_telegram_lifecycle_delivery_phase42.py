from __future__ import annotations

# Phase 51 replaced obsolete automated public-WATCH/setup-only delivery contracts with
# tests/test_triggered_confirmed_telegram_delivery.py. Applicable Phase 42 guard,
# persistence, transport, and helper coverage remains below.

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.agents.trade_idea import create_trade_idea
from app.analytics.setup_quality import SetupQualityGrade, SetupQualityResult, SetupQualityState
from app.alerts.telegram_lifecycle import (
    CONFIRMED_PUBLIC_DELIVERY_POLICY_DISABLED_REASON,
    PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
    PUBLIC_WATCHLIST_MISSING_REQUIRED_RESERVATION_REASON,
    PUBLIC_WATCHLIST_REGIME_PENDING_GATE_CODES,
    RESEARCH_WATCH_SETUP_ONLY_POLICY_DISABLED_REASON,
    PublicWatchlistReservationResult,
    SQLiteTelegramAlertAttemptRepository,
    TelegramAlertType,
    TelegramEligibilityContext,
    TelegramLifecycleDeliveryService,
    WatchlistCandleSnapshot,
    classify_failed_gate_code,
    reserve_public_watchlist_event,
    _public_signal_gate_result,
    _public_watchlist_candidate_from_symbol,
    _public_watchlist_canonical_plan,
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
        self.public_watchlist_guards: list[object] = []

    async def send_text(self, text: str, **kwargs: object) -> TelegramSendResult:
        return await self.send_part(text, part_number=1, total_parts=1, **kwargs)

    async def send_part(
        self,
        text: str,
        *,
        part_number: int,
        total_parts: int,
        **kwargs: object,
    ) -> TelegramSendResult:
        guard = kwargs.pop("public_watchlist_guard", None)
        if guard is not None:
            self.public_watchlist_guards.append(guard)
        self.messages.append(text)
        self.calls.append(kwargs)
        sent = self.status == "sent"
        transport = {
            "status": "sent" if sent else "failed",
            "delivery_state": "SENT" if sent else "RETRYABLE",
            "part_number": part_number,
            "total_parts": total_parts,
            "message_id": len(self.messages) if sent else None,
            "chat_id": "fake-public-chat" if sent else None,
            "error_category": None if sent else "fake_known_rejection",
            "error": None if sent else "fake_known_rejection",
        }
        return TelegramSendResult(
            status=self.status,
            detail=f"{self.status}.",
            telegram_results=(transport,),
            error_message=NA if sent else "fake_known_rejection",
            delivery_state=transport["delivery_state"],
        )


class LegacyResearchWatchDeliveryService(TelegramLifecycleDeliveryService):
    @property
    def setup_only_public_delivery(self) -> bool:
        return False



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
        invalidation_logic="Invalid if price accepts below 95.",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="115",
        tp3="120",

        setup_identity=f"BTCUSDT|swing|long|{signal_id}",
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
            "stop_loss": "95" if direction == "long" else "105",
            "tp1": "110" if direction == "long" else "95",
            "tp2": "115" if direction == "long" else "90",
            "tp3": "120" if direction == "long" else "85",
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
        "confirmation_needed": "15m BOS/CHoCH.",
        "confirmation_timeframe": "15m",
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
    next_trigger: str = "Wait for failed gate to clear / 15m BOS/CHoCH.",
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


def _run_result_many(*symbol_results: ScannerSymbolResult) -> ScannerRunResult:
    return ScannerRunResult(
        config=_config(),
        results=tuple(symbol_results),
        scanned_symbols=len(symbol_results),
        failed_symbols=0,
        trade_ideas_created=len(symbol_results),
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












def test_public_v1_blocks_rejected_state() -> None:
    decision = telegram_alert_decision_for_symbol(
        _public_v1_symbol(
            state=SetupLifecycleState.REJECTED,
            quality_score=95,
            rr=Decimal("4"),
            technical_score=Decimal("99"),
            opportunity_score=Decimal("99"),
        )
    )

    assert decision.eligible is False
    assert "public_block_non_public_terminal_state" in decision.reason or decision.reason == "lifecycle_state_not_eligible"














def test_public_v1_target_caution_terminal_states_block() -> None:
    for state in (
        SetupLifecycleState.REJECTED,
        SetupLifecycleState.COOLDOWN,
        SetupLifecycleState.INVALIDATED,
        SetupLifecycleState.EXPIRED,
    ):
        symbol = _public_target_caution_symbol(state=state, signal_id=f"target-caution-{state.value.lower()}")
        message = telegram_signal_message_from_symbol(symbol)
        gate = _public_watchlist_gate_result(symbol, message, TelegramEligibilityContext())

        assert gate.allowed is False, state
        assert any(
            reason == "public_block_non_public_terminal_state"
            or "public_watchlist_state_not_eligible" in reason
            or "public_watchlist_terminal_state" in reason
            for reason in gate.blocking_reasons
        ), state



























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


def _public_v1_symbol(
    *,
    symbol: str = "BTCUSDT",
    signal_id: str = "public-v1",
    state: SetupLifecycleState = SetupLifecycleState.ACTIONABLE_A_GRADE,
    previous: SetupLifecycleState | None = SetupLifecycleState.TRIGGERED,
    actionability_state: str = "A_GRADE_ACTIONABLE",
    grade: SetupQualityGrade = SetupQualityGrade.A,
    quality_score: int = 88,
    rr: Decimal = Decimal("3.0"),
    technical_score: Decimal = Decimal("95"),
    opportunity_score: Decimal = Decimal("95"),
    direction: str = "long",
    diagnostics: dict[str, object] | None = None,
) -> ScannerSymbolResult:
    short = direction.lower() == "short"
    default_entry_low = Decimal("100")
    default_entry_high = Decimal("102")
    default_stop = Decimal("105") if short else Decimal("95")
    default_tp1 = Decimal("95") if short else Decimal("110")
    default_tp2 = Decimal("90") if short else Decimal("115")
    default_tp3 = Decimal("85") if short else Decimal("120")
    public_diagnostics = _public_ready_watchlist_diagnostics(
        watchlist_grade=grade.value,
        quality_grade=grade.value,
        setup_quality_score=Decimal(str(quality_score)),
        quality_score=Decimal(str(quality_score)),
        rr_to_tp2=rr,
        entry_low=default_entry_low,
        entry_high=default_entry_high,
        stop=default_stop,
        stop_loss=default_stop,
        tp1=default_tp1,
        tp2=default_tp2,
        tp3=default_tp3,
        opportunity_score=opportunity_score,
        bias=direction,
        direction=direction,
        target_integrity_status="passed",
        target_integrity_failed=False,
    )
    if diagnostics is not None:
        public_diagnostics.update(diagnostics)
    result = _symbol(
        state,
        previous=previous,
        diagnostics=public_diagnostics,
        signal_id=signal_id,
        setup_quality=_setup_quality_with_grade(grade, quality_score=quality_score),
        technical_score=technical_score,
        trade_idea=None,
    ).model_copy(update={"symbol": symbol})
    assert result.lifecycle_state is not None
    assert result.lifecycle_transition is not None
    record = result.lifecycle_state.model_copy(
        update={
            "symbol": symbol,
            "direction": direction,
            "actionability_state": actionability_state,
            "quality_score": quality_score,
            "quality_grade_current": grade.value,
            "candidate_quality_grade": grade.value,
            "final_quality_grade": grade.value,
            "rr": str(rr),
            "entry_low": public_diagnostics.get("entry_low", "100"),
            "entry_high": public_diagnostics.get("entry_high", "102"),
            "stop_loss": public_diagnostics.get("stop_loss", public_diagnostics.get("stop", "95" if direction == "long" else "105")),
            "tp1": public_diagnostics.get("tp1", "110" if direction == "long" else "95"),
            "tp2": public_diagnostics.get("tp2", "115" if direction == "long" else "90"),
            "tp3": public_diagnostics.get("tp3", "120" if direction == "long" else "85"),
            "target_integrity_status": public_diagnostics.get("target_integrity_status", "passed"),
            "target_failure": public_diagnostics.get("target_failure", NA),
            "target_failure_severity": public_diagnostics.get("target_failure_severity", NA),
            "target_warning_reason": public_diagnostics.get("target_warning_reason", NA),
            "final_failed_gate": public_diagnostics.get("final_failed_gate", NA),
            "final_block_reason": public_diagnostics.get("final_block_reason", NA),
        }
    )
    transition = result.lifecycle_transition.model_copy(update={"symbol": symbol, "record": record})
    return result.model_copy(update={"lifecycle_state": record, "lifecycle_transition": transition})


def _target_caution_diagnostics(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "first_failed_gate": "target_inside_chop",
        "gates_failed": ("target_inside_chop",),
        "target_integrity_status": "warning",
        "target_failure": "TARGET_INSIDE_CHOP",
        "target_failure_severity": "target_caution_actionable",
        "target_warning_reason": "TARGET_INSIDE_CHOP",
    }
    data.update(overrides)
    return data


def _public_target_caution_symbol(
    *,
    grade: SetupQualityGrade = SetupQualityGrade.A,
    quality_score: int = 88,
    rr: Decimal = Decimal("2.8"),
    state: SetupLifecycleState = SetupLifecycleState.ACTIONABLE_A_GRADE,
    symbol: str = "BTCUSDT",
    signal_id: str = "target-caution",
    confirmation_count: int = 2,
    required_confirmation_cycles: int = 2,
    diagnostics: dict[str, object] | None = None,
    **overrides: object,
) -> ScannerSymbolResult:
    caution_diagnostics = _target_caution_diagnostics(**(diagnostics or {}))
    symbol_result = _public_v1_symbol(
        symbol=symbol,
        signal_id=signal_id,
        state=state,
        actionability_state="A_GRADE_ACTIONABLE_TARGET_CAUTION",
        grade=grade,
        quality_score=quality_score,
        rr=rr,
        technical_score=Decimal("95"),
        opportunity_score=Decimal("95"),
        diagnostics=caution_diagnostics,
    )
    lifecycle_updates: dict[str, object] = {
        "actionability_state": "A_GRADE_ACTIONABLE_TARGET_CAUTION",
        "confirmation_count": confirmation_count,
        "required_confirmation_cycles": required_confirmation_cycles,
        "failed_gate": caution_diagnostics.get("first_failed_gate", NA),
        "target_integrity_status": caution_diagnostics.get("target_integrity_status", "warning"),
        "target_failure": caution_diagnostics.get("target_failure", "TARGET_INSIDE_CHOP"),
        "target_failure_severity": caution_diagnostics.get("target_failure_severity", "target_caution_actionable"),
        "target_warning_reason": caution_diagnostics.get("target_warning_reason", "TARGET_INSIDE_CHOP"),
    }
    lifecycle_updates.update(overrides)
    return _with_lifecycle_fields(symbol_result, **lifecycle_updates)

def _public_v1_block_reasons(db_path: Path) -> list[tuple[str, str, str]]:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            """
            SELECT symbol, telegram_status, blocked_reason
            FROM telegram_alert_attempts
            WHERE attempted_alert_type = 'WATCHLIST'
            ORDER BY id
            """
        ).fetchall()

def _assert_target_integrity_blocked(decision, *fields: str) -> None:
    assert decision.eligible is False
    if decision.reason.startswith("invalid_stored_plan_geometry:"):
        assert decision.alert_type is None
        return
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


def _sent_at_ago(*, hours: int = 0, minutes: int = 0) -> str:
    return (datetime.now(UTC) - timedelta(hours=hours, minutes=minutes)).isoformat().replace("+00:00", "Z")


def _seed_public_cap_history(db_path: Path, count: int) -> None:
    start = datetime.now(UTC) - timedelta(hours=23)
    for index in range(count):
        sent_at = (start + timedelta(minutes=90 * index)).isoformat().replace("+00:00", "Z")
        _seed_prior_active_alert(
            db_path,
            signal_id=f"prior-cap-{index}",
            alert_type=TelegramAlertType.WATCHLIST,
            symbol=f"CAP{index}USDT",
            sent_at=sent_at,
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


def _assert_research_watch_policy_disabled(
    summary,
    sender: FakeSender,
    db_path: Path,
    *,
    expected_count: int = 1,
    expected_persisted_count: int | None = None,
) -> None:
    assert summary.sent == 0
    assert summary.failed == 0
    assert summary.skipped == expected_count
    assert sender.messages == []
    assert sender.calls == []
    policy_rows = [
        row
        for row in _research_attempt_rows(db_path)
        if row[4] == RESEARCH_WATCH_SETUP_ONLY_POLICY_DISABLED_REASON
    ]
    assert len(policy_rows) == (expected_persisted_count or expected_count)
    assert all(row[1] == "skipped" and row[2] is None for row in policy_rows)


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
        service = LegacyResearchWatchDeliveryService(database_path=db_path, settings=settings, sender=sender)

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
        service = LegacyResearchWatchDeliveryService(database_path=db_path, settings=settings, sender=sender)

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
    service = LegacyResearchWatchDeliveryService(
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
    service = LegacyResearchWatchDeliveryService(
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
    service = LegacyResearchWatchDeliveryService(
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
    service = LegacyResearchWatchDeliveryService(
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
    service = LegacyResearchWatchDeliveryService(
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
    service = LegacyResearchWatchDeliveryService(
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
    sent_service = LegacyResearchWatchDeliveryService(
        database_path=sent_db,
        settings=_research_settings(),
        sender=sent_sender,
    )
    run(sent_service.deliver_for_run(_run_result(_research_symbol()), scan_run_id="sent"))
    sent_row = _research_attempt_rows(sent_db)[0]

    failed_db = tmp_path / "research-failed-at.db"
    failed_sender = FakeSender(status="failed")
    failed_service = LegacyResearchWatchDeliveryService(
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
    service = LegacyResearchWatchDeliveryService(
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
    service = LegacyResearchWatchDeliveryService(
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






def test_regime_failed_gate_codes_are_fatal_for_public_watchlist() -> None:
    assert not PUBLIC_WATCHLIST_REGIME_PENDING_GATE_CODES
    for code in ("regime_blocked", "weak_regime_fit", "rejected_by_regime"):
        assert classify_failed_gate_code(code) == "FATAL_PUBLIC_WATCHLIST_GATE"


def test_public_watchlist_blocks_timing_pending_until_actionable_state() -> None:
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

    assert gate.allowed is False
    assert "public_block_non_actionable_state" in gate.blocking_reasons
    assert gate.allowed_missing_gate == "TIMING_CONFIRMATION_PENDING"
    assert gate.failed_gate_classes == ("TIMING_CONFIRMATION_PENDING",)












def test_public_watchlist_blocks_target_integrity_failure() -> None:
    symbol = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=_public_ready_watchlist_diagnostics(tp1=Decimal("99")),
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
    )
    lifecycle = symbol.lifecycle_state.model_copy(update={"tp1": "99"})
    transition = symbol.lifecycle_transition.model_copy(update={"record": lifecycle})
    decision = telegram_alert_decision_for_symbol(
        symbol.model_copy(update={"lifecycle_state": lifecycle, "lifecycle_transition": transition})
    )

    assert decision.eligible is False
    assert decision.alert_type is None
    assert decision.reason == "invalid_stored_plan_geometry:invalid_long_level_order"







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












def test_public_watchlist_rejects_missing_entry_zone() -> None:
    symbol = _with_lifecycle_fields(
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
        ),
        entry_low=NA,
        entry_high=NA,
    )
    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is False
    assert decision.alert_type is None
    assert decision.reason == "invalid_stored_plan_geometry:missing_entry_low"


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
    assert decision.alert_type is None
    assert decision.reason == "invalid_stored_plan_geometry:missing_invalidation"








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




def test_confirmed_to_stalking_transition_cannot_produce_sl_hit_alert_type() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.STALKING,
            previous=SetupLifecycleState.CONFIRMED,
            diagnostics=_public_ready_watchlist_diagnostics(outcome_status="sl_hit"),
        )
    )

    assert decision.alert_type != TelegramAlertType.SL_HIT






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




def test_public_confirmed_signal_allows_a_family_grades() -> None:
    for grade in (SetupQualityGrade.A, SetupQualityGrade.A_PLUS):
        symbol = _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            setup_quality=_setup_quality_with_grade(grade),
            trade_idea=_trade_idea(opportunity_grade=grade.value, opportunity_score=Decimal("88")),
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
        pullback_failure_reason="No valid OB or FVG was found inside the 15m displacement impulse.",
        next_required_conditions=(
            "A valid OB or FVG must be found inside the displacement impulse.",
            "The OB/FVG zone must overlap the preferred fib pullback zone.",
            "RR and final quality gates must still pass after a valid zone is found.",
        ),
        mode="scalp",
    )

    symbol = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.REJECTED,
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            rejection_reason="No valid Liquidity-Grab Pullback setup.",
            rejection_reasons=("No valid Liquidity-Grab Pullback setup.",),
            diagnostics=diagnostics,
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=41),
        ),
        entry_low=NA,
        entry_high=NA,
        stop_loss=NA,
        tp1=NA,
        tp2=NA,
        tp3=NA,
    )
    decision = telegram_alert_decision_for_symbol(symbol)

    assert decision.eligible is False
    assert decision.alert_type is None
    assert decision.reason == "invalid_stored_plan_geometry:missing_entry_low"


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
    assert decision.alert_type is None
    assert decision.reason == "invalid_stored_plan_geometry:unsupported_direction:N/A"


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
        symbol = _with_lifecycle_fields(
            symbol, entry_low=NA, entry_high=NA, stop_loss=NA, tp1=NA, tp2=NA, tp3=NA
        )

        decision = telegram_alert_decision_for_symbol(symbol)

        assert decision.eligible is False
        assert decision.alert_type is None
        assert decision.reason == "invalid_stored_plan_geometry:missing_entry_low"


def test_action_watchlist_only_with_all_plan_fields_na_is_blocked() -> None:
    quality = _setup_quality(SetupQualityState.REJECTED_NO_EDGE, quality_score=41).model_copy(
        update={"action_label": "Watchlist only"}
    )

    decision = telegram_alert_decision_for_symbol(
        _with_lifecycle_fields(
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
            ),
            entry_low=NA,
            entry_high=NA,
            stop_loss=NA,
            tp1=NA,
            tp2=NA,
            tp3=NA,
        )
    )

    assert decision.eligible is False
    assert decision.alert_type is None
    assert decision.reason == "invalid_stored_plan_geometry:missing_entry_low"




def test_public_signal_context_extracts_specific_diagnostics_for_message() -> None:
    symbol_result = _public_v1_symbol(
        symbol="CTXUSDT",
        signal_id="ctx-watch",
        rr=Decimal("3.25"),
        diagnostics={
            "mode": "scalp",
            "source_modes": ("scalp",),
            "gates_passed": ("sweep", "wick_reclaim", "bos_choch", "ob_fvg", "fib", "target_integrity"),
            "execution_sweep_status": "passed",
            "reclaim_detected": True,
            "confirmation_structure_shift_status": "passed",
            "selected_zone_type": "FVG",
            "fib_alignment_status": "aligned",
            "target_integrity_status": "passed",
            "target_warning_reason": NA,
            "structure_reason": "Setup quality does not provide enough deterministic edge.",
        },
    )

    message = telegram_signal_message_from_symbol(symbol_result)
    text = format_telegram_signal_message(TelegramAlertType.WATCHLIST, message)

    assert message.signal_context is not None
    assert message.signal_context.primary_mode == "swing"
    assert "CTXUSDT · LONG · SWING" in text
    assert "Downside liquidity was swept" in text
    assert "Downside liquidity was swept and reclaimed." in text
    assert "15m structure shifted bullish" in text
    assert "FVG + fib reaction zone" in text
    assert "fib reaction zone" in text
    assert "clean RR path" not in text
    assert "Invalid if price" in text and "below 95" in text
    assert "Setup quality does not provide enough deterministic edge" not in text

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
    tp1: object = _DEFAULT_PLAN_LEVEL,
    historical_rejection: bool = True,
) -> ScannerSymbolResult:
    short = side.lower() == "short"
    tp1 = (Decimal("0.09300") if short else Decimal("0.09800")) if tp1 is _DEFAULT_PLAN_LEVEL else tp1
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
    if tp1 == NA:
        diagnostics.update(
            {
                "tp1": NA,
                "target_1": NA,
                "take_profit_1": NA,
                "first_target": NA,
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
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
    )



def _runtime_public_watchlist_snapshot(
    *,
    symbol: str = "INTUSDT",
    state: SetupLifecycleState = SetupLifecycleState.ACTIONABLE_A_GRADE,
    side: str = "long",
    grade: str = "A",
    potential_rr: object = Decimal("3.1"),
    entry_low: object = Decimal("125.70246"),
    entry_high: object = Decimal("125.74"),
    invalidation: object = Decimal("125.452"),
    failed_gate: str = NA,
    signal_id: str = "int-runtime-watchlist",
    status: ScannerPipelineStatus = ScannerPipelineStatus.IDEA_CREATED,
    quality_score: int = 88,
    strategy_mode: str = "swing",
    extra_diagnostics: dict[str, object] | None = None,
) -> ScannerSymbolResult:
    short = side.lower() == "short"
    tp1 = Decimal("124.8") if short else Decimal("126.5")
    tp2 = Decimal("124.0") if short else Decimal("127.2")
    tp3 = Decimal("123.2") if short else Decimal("128.0")
    diagnostics = _public_ready_watchlist_diagnostics(
        bias=side,
        direction=side,
        mode=strategy_mode,
        grade=grade,
        watchlist_grade=grade,
        quality_label="HIGH_QUALITY_TRADE",
        quality_state="HIGH_QUALITY_TRADE",
        potential_rr=potential_rr,
        rr=potential_rr,
        rr_to_tp2=potential_rr,
        planned_rr=potential_rr,
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        entry_low=entry_low,
        entry_high=entry_high,
        stop=invalidation,
        stop_loss=invalidation,
        invalidation_level=invalidation,
        invalidation=invalidation,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        first_failed_gate=failed_gate,
        failed_gate=failed_gate,
        gates_failed=() if failed_gate == NA else (failed_gate,),
        pending_reason=failed_gate,
        pending_confirmation_reason=failed_gate,
        final_failed_gate=failed_gate,
        final_block_reason=NA if failed_gate == NA else f"Blocked by {failed_gate}.",
        technical_score=Decimal("95"),
        opportunity_score=Decimal("95"),
        trust_meter=Decimal("95"),
    )
    if extra_diagnostics:
        diagnostics.update(extra_diagnostics)
    result = _symbol(
        state,
        diagnostics=diagnostics,
        signal_id=signal_id,
        trade_idea=None,
        status=status,
        rejection_reason="Technical score is below confirmed minimum 50." if status == ScannerPipelineStatus.REJECTED_BY_SCORING else None,
        rejection_reasons=("Opportunity score is below scanner confirmed minimum 80.",) if status == ScannerPipelineStatus.REJECTED_BY_SCORING else (),
        technical_score=Decimal("95"),
        setup_quality=_setup_quality_with_grade(
            SetupQualityGrade.A,
            quality_state=SetupQualityState.HIGH_QUALITY_TRADE,
            quality_score=quality_score,
        ),
    ).model_copy(update={"symbol": symbol})
    assert result.lifecycle_state is not None
    record = result.lifecycle_state.model_copy(
        update={
            "current_state": state,
            "direction": side,
            "mode": strategy_mode,
            "actionability_state": "A_GRADE_ACTIONABLE",
            "quality_score": quality_score,
            "quality_grade_current": grade,
            "candidate_quality_grade": grade,
            "final_quality_grade": grade,
            "failed_gate": failed_gate,
            "final_failed_gate": failed_gate,
            "final_block_reason": NA if failed_gate == NA else f"Blocked by {failed_gate}.",
            "rr": NA if potential_rr == NA else str(potential_rr),
            "entry_low": entry_low,
            "entry_high": entry_high,
            "stop_loss": invalidation,
            "tp1": diagnostics.get("tp1", tp1),
            "tp2": diagnostics.get("tp2", tp2),
            "tp3": diagnostics.get("tp3", tp3),
            "invalidation_reason": NA if invalidation == NA else f"Invalid if price accepts beyond {invalidation}.",
            "invalidation_logic": NA if invalidation == NA else f"Invalid if price accepts beyond {invalidation}.",
        }
    )
    transition = result.lifecycle_transition.model_copy(update={"symbol": symbol, "record": record}) if result.lifecycle_transition else None
    return result.model_copy(
        update={
            "lifecycle_state": record,
            "lifecycle_transition": transition,
            "status_history": (status,),
            "valid_strategy_modes": (),
            "rejected_strategy_modes": (strategy_mode,),
            "strategy_diagnostics": {strategy_mode: diagnostics},
        }
    )


def _deliver_public_watchlist_snapshot(tmp_path: Path, symbol_result: ScannerSymbolResult, run_id: str):
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / f"{run_id}.db", settings=Settings(), sender=sender)
    summary = run(service.deliver_for_run(_run_result(symbol_result), scan_run_id=run_id))
    return sender, summary


def _why_section(text: str) -> str:
    marker = "🧠 Edge"
    assert marker in text
    return text.split(marker, 1)[1].strip().split("\n\n⚠️ Execution", 1)[0].strip()


def _reason_watchlist_symbol(
    *,
    symbol: str,
    failed_gate: str,
    extra_diagnostics: dict[str, object],
    side: str = "long",
) -> ScannerSymbolResult:
    diagnostics = _public_ready_watchlist_diagnostics(
        bias=side,
        direction=side,
        grade="B+",
        potential_rr=Decimal("2.7"),
        rr=Decimal("2.7"),
        rr_to_tp2=Decimal("2.7"),
        planned_rr=Decimal("2.7"),
        first_failed_gate=failed_gate,
        failed_gate=failed_gate,
        gates_failed=(failed_gate,),
        pending_reason=failed_gate,
        pending_confirmation_reason=failed_gate,
    )
    diagnostics.update(extra_diagnostics)
    result = _symbol(
        SetupLifecycleState.WATCHLISTED,
        diagnostics=diagnostics,
        signal_id=f"{symbol.lower()}-reason",
        trade_idea=None,
        status=ScannerPipelineStatus.REJECTED_BY_SCORING,
        setup_quality=_setup_quality_with_grade(
            SetupQualityGrade.B_PLUS,
            quality_state=SetupQualityState.WATCHLIST_NEAR_MISS,
            quality_score=82,
        ),
    ).model_copy(update={"symbol": symbol})
    return _with_lifecycle_fields(
        result,
        direction=side,
        rr="2.7",
        entry_low=Decimal("100"),
        entry_high=Decimal("102"),
        stop_loss=Decimal("95") if side == "long" else Decimal("105"),
        invalidation_reason="Invalid if price accepts beyond the planned invalidation level.",
        invalidation_logic="Invalid if price accepts beyond the planned invalidation level.",
    )

def _simple_signal_text_for(symbol_result: ScannerSymbolResult) -> str:
    decision = telegram_alert_decision_for_symbol(symbol_result)
    assert decision.message is not None
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    return format_telegram_signal_message(decision.alert_type, decision.message)


def _assert_suppressed_follow_up(db_path: Path, attempted_alert_type: TelegramAlertType) -> None:
    rows = _watchlist_outcome_rows(db_path)
    assert not any(row[1] == "sent" and row[2] == attempted_alert_type.value for row in rows)
    assert any(
        row[2] == attempted_alert_type.value
        and row[3] in {
            "public_watchlist_follow_up_updates_disabled",
            "outcome_tracking_limit_hit_requires_prior_public_signal",
        }
        for row in rows
    )


def _assert_no_sent_follow_up_attempt(db_path: Path, attempted_alert_type: TelegramAlertType) -> None:
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT attempted_alert_type, telegram_status, blocked_reason
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()
    assert not any(row[0] == attempted_alert_type.value and row[1] == "sent" for row in rows)

def _contextless_reason_overrides(*, failed_gate: str) -> dict[str, object]:
    return {
        "first_failed_gate": failed_gate,
        "failed_gate": failed_gate,
        "gates_failed": (failed_gate,),
        "pending_reason": failed_gate,
        "pending_confirmation_reason": failed_gate,
        "next_required_conditions": (),
        "confirmation_needed": NA,
        "structure_reason": NA,
        "selected_zone_type": NA,
        "ob_fvg_status": NA,
        "ob_fvg_diagnostics": NA,
        "execution_sweep_status": NA,
        "sweep_status": NA,
        "liquidity_sweep_detected": False,
        "sweep_detected": False,
        "reclaim_detected": False,
        "wick_reclaim_status": NA,
        "reclaim_status": NA,
        "confirmation_structure_shift_status": NA,
        "bos_choch_status": NA,
        "bos_detected": False,
        "choch_detected": False,
        "mss_detected": False,
        "structure_shift_detected": False,
        "gates_passed": ("target_integrity", "rr"),
    }




@pytest.mark.parametrize(
    ("case_name", "symbol_result"),
    (
        ("below-b-plus", _near_miss_watchlist_symbol(grade="B", score=79, historical_rejection=False)),
        ("rr-below-min", _near_miss_watchlist_symbol(potential_rr=Decimal("2.49"), historical_rejection=False)),
        ("missing-entry", _near_miss_watchlist_symbol(entry_low=NA, entry_high=NA, historical_rejection=False)),
        ("missing-stop", _near_miss_watchlist_symbol(invalidation=NA, historical_rejection=False)),
        ("missing-tp1", _near_miss_watchlist_symbol(tp1=NA, historical_rejection=False)),
    ),
)
def test_simple_public_signal_blocks_weak_or_malformed_watchlist_candidates(
    tmp_path: Path,
    case_name: str,
    symbol_result: ScannerSymbolResult,
) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / f"{case_name}.db", settings=Settings(), sender=sender)

    summary = run(service.deliver_for_run(_run_result(symbol_result), scan_run_id=case_name))

    assert summary.sent == 0
    assert sender.messages == []






def test_limit_zone_hit_update_does_not_send_public_telegram_message(tmp_path: Path) -> None:
    db_path = tmp_path / "limit-follow-up-suppressed.db"
    signal_id = "limit-follow-up-suppressed"
    _seed_prior_active_alert(db_path, signal_id=signal_id, alert_type=TelegramAlertType.WATCHLIST)
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(), sender=sender)

    summary = run(
        service.deliver_for_run(
            _run_result(_outcome_scan_symbol(signal_id=signal_id, high=Decimal("101"), low=Decimal("99"))),
            scan_run_id="limit-follow-up-suppressed",
        )
    )

    assert summary.sent == 0
    assert sender.messages == []
    rows = _watchlist_outcome_rows(db_path)
    assert not any(row[1] == "sent" and row[2] == TelegramAlertType.LIMIT_HIT.value for row in rows)


def test_tp_hit_update_does_not_send_public_telegram_message(tmp_path: Path) -> None:
    db_path = tmp_path / "tp-follow-up-suppressed.db"
    signal_id = "tp-follow-up-suppressed"
    sender, service = _watchlist_service_after_limit_hit(db_path, signal_id=signal_id)

    summary = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("110"),
                    low=Decimal("103"),
                    current_price=Decimal("110"),
                )
            ),
            scan_run_id="tp-follow-up-suppressed",
        )
    )

    assert summary.sent == 0
    assert len(sender.messages) == 1
    assert not any("TAKE PROFIT HIT" in message for message in sender.messages)
    _assert_suppressed_follow_up(db_path, TelegramAlertType.TP1_HIT)


def test_sl_hit_update_does_not_send_public_telegram_message(tmp_path: Path) -> None:
    db_path = tmp_path / "sl-follow-up-suppressed.db"
    signal_id = "sl-follow-up-suppressed"
    sender, service = _watchlist_service_after_limit_hit(db_path, signal_id=signal_id)

    summary = run(
        service.deliver_for_run(
            _run_result(
                _outcome_scan_symbol(
                    signal_id=signal_id,
                    high=Decimal("101"),
                    low=Decimal("94"),
                    current_price=Decimal("94"),
                )
            ),
            scan_run_id="sl-follow-up-suppressed",
        )
    )

    assert summary.sent == 0
    assert len(sender.messages) == 1
    assert not any("STOP HIT" in message for message in sender.messages)
    _assert_suppressed_follow_up(db_path, TelegramAlertType.SL_HIT)


def test_simple_public_signal_requires_actionable_lifecycle_status(tmp_path: Path) -> None:
    sender, summary = _deliver_public_watchlist_snapshot(
        tmp_path,
        _runtime_public_watchlist_snapshot(state=SetupLifecycleState.STALKING, signal_id="stalking-simple-signal"),
        "stalking-simple-signal",
    )

    assert summary.sent == 0
    assert sender.messages == []













def test_confirmed_scoring_failure_blocks_b_plus_public_watchlist(tmp_path: Path) -> None:
    sender, summary = _deliver_public_watchlist_snapshot(
        tmp_path,
        _runtime_public_watchlist_snapshot(failed_gate="scoring"),
        "scoring-watchlist",
    )

    assert summary.sent == 0
    assert sender.messages == []


def test_confirmed_technical_min_failure_blocks_public_watchlist(tmp_path: Path) -> None:
    sender, summary = _deliver_public_watchlist_snapshot(
        tmp_path,
        _runtime_public_watchlist_snapshot(failed_gate="technical_score_below_confirmed_minimum"),
        "technical-watchlist",
    )

    assert summary.sent == 0
    assert sender.messages == []


def test_confirmed_opportunity_min_failure_blocks_public_watchlist(tmp_path: Path) -> None:
    sender, summary = _deliver_public_watchlist_snapshot(
        tmp_path,
        _runtime_public_watchlist_snapshot(failed_gate="opportunity_score_below_confirmed_minimum"),
        "opportunity-watchlist",
    )

    assert summary.sent == 0
    assert sender.messages == []


def test_trust_meter_below_confirmed_min_blocks_public_watchlist(tmp_path: Path) -> None:
    sender, summary = _deliver_public_watchlist_snapshot(
        tmp_path,
        _runtime_public_watchlist_snapshot(failed_gate="trust_meter_below_confirmed_minimum"),
        "trust-watchlist",
    )

    assert summary.sent == 0
    assert sender.messages == []




def test_rejected_core_state_from_confirmed_scoring_is_blocked_from_public_watchlist(tmp_path: Path) -> None:
    symbol = _runtime_public_watchlist_snapshot(state=SetupLifecycleState.REJECTED, failed_gate="scoring")
    sender, summary = _deliver_public_watchlist_snapshot(tmp_path, symbol, "rejected-scoring-watchlist")

    assert symbol.lifecycle_state is not None
    assert symbol.lifecycle_state.current_state == SetupLifecycleState.REJECTED
    assert summary.sent == 0
    assert sender.messages == []


def test_rejected_core_state_with_structural_breakdown_remains_blocked(tmp_path: Path) -> None:
    sender, summary = _deliver_public_watchlist_snapshot(
        tmp_path,
        _runtime_public_watchlist_snapshot(state=SetupLifecycleState.REJECTED, failed_gate="structural_breakdown"),
        "rejected-structural-block",
    )
    assert summary.sent == 0
    assert sender.messages == []













def _public_watchlist_attempt_records(db_path: Path) -> list[dict[str, object]]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT signal_id, alert_type, telegram_status, attempted_alert_type,
                   public_watchlist_plan_id, public_watchlist_event_key, blocked_reason, error_message,
                   dedupe_reason, seen_count
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _public_alert_event_records(db_path: Path) -> list[dict[str, object]]:
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, canonical_plan_id, event_type, event_key, symbol, side, setup_family,
                   normalized_zone_low, normalized_zone_high, normalized_invalidation,
                   raw_entry_low, raw_entry_high, raw_stop_loss, status, matched_prior_alert_id,
                   matched_prior_event_id, failure_reason
            FROM public_alert_events
            ORDER BY id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _sent_public_alert_event_records(db_path: Path) -> list[dict[str, object]]:
    return [row for row in _public_alert_event_records(db_path) if row["status"] == "SENT"]


def _blocked_public_alert_event_records(db_path: Path) -> list[dict[str, object]]:
    return [row for row in _public_alert_event_records(db_path) if row["status"] == "BLOCKED"]


def _sent_public_watchlist_records(db_path: Path) -> list[dict[str, object]]:
    return [
        row
        for row in _public_watchlist_attempt_records(db_path)
        if row["alert_type"] == TelegramAlertType.WATCHLIST.value and row["telegram_status"] == "sent"
    ]


def _duplicate_public_watchlist_records(db_path: Path) -> list[dict[str, object]]:
    return [
        row
        for row in _public_watchlist_attempt_records(db_path)
        if row["attempted_alert_type"] == TelegramAlertType.WATCHLIST.value
        and row["telegram_status"] == "skipped"
        and row["error_message"] == "duplicate_successful_public_watchlist_event"
    ]


def _pepe_watchlist_snapshot(*, signal_id: str, invalidation: object = Decimal("0.00268872")) -> ScannerSymbolResult:
    return _runtime_public_watchlist_snapshot(
        symbol="1000PEPEUSDT",
        side="long",
        grade="A",
        potential_rr=Decimal("3.2"),
        entry_low=Decimal("0.00270433"),
        entry_high=Decimal("0.00270820"),
        invalidation=invalidation,
        signal_id=signal_id,
        extra_diagnostics={
            "tick_size": Decimal("0.00000001"),
            "sweep_structure_id": "pepe-sweep-1",
            "bos_structure_id": "pepe-bos-1",
            "pullback_zone_id": "pepe-zone-1",
        },
    )


def _syn_watchlist_snapshot(*, signal_id: str, invalidation: object = Decimal("0.35994")) -> ScannerSymbolResult:
    return _runtime_public_watchlist_snapshot(
        symbol="SYNUSDT",
        side="long",
        grade="A",
        potential_rr=Decimal("3.2"),
        entry_low=Decimal("0.39115"),
        entry_high=Decimal("0.39218"),
        invalidation=invalidation,
        signal_id=signal_id,
        extra_diagnostics={"tick_size": Decimal("0.00001")},
    )


def _eth_watchlist_snapshot(*, signal_id: str, invalidation: object = Decimal("1257.60")) -> ScannerSymbolResult:
    return _runtime_public_watchlist_snapshot(
        symbol="ETHUSDT",
        side="short",
        grade="A",
        potential_rr=Decimal("3.2"),
        entry_low=Decimal("1234.05"),
        entry_high=Decimal("1239.6"),
        invalidation=invalidation,
        signal_id=signal_id,
        extra_diagnostics={"tick_size": Decimal("0.01")},
    )


def _skhynix_watchlist_snapshot(*, signal_id: str, structure_suffix: str = "1") -> ScannerSymbolResult:
    return _runtime_public_watchlist_snapshot(
        symbol="SKHYNIXUSDT",
        side="short",
        grade="A",
        potential_rr=Decimal("2.6"),
        entry_low=Decimal("1744.16"),
        entry_high=Decimal("1754.74"),
        invalidation=Decimal("1779.61"),
        signal_id=signal_id,
        extra_diagnostics={
            "tick_size": Decimal("0.01"),
            "sweep_structure_id": f"skhynix-sweep-{structure_suffix}",
            "bos_structure_id": f"skhynix-bos-{structure_suffix}",
            "pullback_zone_id": f"skhynix-zone-{structure_suffix}",
        },
    )


def _xplus_watchlist_snapshot(*, signal_id: str, structure_suffix: str = "1") -> ScannerSymbolResult:
    return _runtime_public_watchlist_snapshot(
        symbol="XPLUSDT",
        side="short",
        grade="A",
        potential_rr=Decimal("3.2"),
        entry_low=Decimal("0.10393"),
        entry_high=Decimal("0.10448"),
        invalidation=Decimal("0.10661"),
        signal_id=signal_id,
        extra_diagnostics={
            "tick_size": Decimal("0.00001"),
            "tp1": Decimal("0.10300"),
            "tp2": Decimal("0.10220"),
            "tp3": Decimal("0.10150"),
            "sweep_structure_id": f"xplus-sweep-{structure_suffix}",
            "bos_structure_id": f"xplus-bos-{structure_suffix}",
            "pullback_zone_id": f"xplus-zone-{structure_suffix}",
        },
    )























































def test_concurrent_public_watchlist_reservations_allow_one_sender(tmp_path: Path) -> None:
    db_path = tmp_path / "concurrent-public-watchlist-reservation.db"
    symbol_result = _syn_watchlist_snapshot(signal_id="concurrent-watchlist")
    message = telegram_signal_message_from_symbol(symbol_result)
    plan = _public_watchlist_canonical_plan(symbol_result, message)

    def reserve_once(index: int):
        attempted_at = f"2026-06-25T18:5{index}:00+00:00"
        record = TelegramAlertAttemptRecord(
            signal_id="concurrent-watchlist",
            symbol=symbol_result.symbol,
            direction=message.direction,
            previous_state=NA,
            new_state="WATCHLISTED",
            alert_type=TelegramAlertType.WATCHLIST.value,
            lifecycle_state="WATCHLISTED",
            sent_at=None,
            attempted_at=attempted_at,
            telegram_status="reserved",
            message_hash=f"concurrent-hash-{index}",
            scan_run_id=f"concurrent-{index}",
            attempted_alert_type=TelegramAlertType.WATCHLIST.value,
            setup_quality_score="A",
            rr_planned="2.8",
            min_rr="2.5",
            opportunity_score="80",
            min_score_for_idea="80",
            technical_score="49",
            price_level="0.39115-0.39218",
            entry_low=plan.raw_entry_low,
            entry_high=plan.raw_entry_high,
            stop_loss=plan.raw_invalidation,
            tp1="0.41",
            tp2="0.43",
            tp3="0.45",
            public_watchlist_plan_id=plan.plan_id,
            public_watchlist_event_key=f"{plan.plan_id}|{PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE}",
            public_alert_event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
            normalized_entry_zone_low=plan.normalized_entry_low,
            normalized_entry_zone_high=plan.normalized_entry_high,
            normalized_invalidation=plan.normalized_invalidation,
            dedupe_status="reserved",
        )
        with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
            return reserve_public_watchlist_event(
                repository,
                symbol=symbol_result.symbol,
                side=message.direction,
                canonical_plan_id=plan.plan_id,
                event_type=PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE,
                payload={"canonical_plan": plan, "reservation_record": record},
            )

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reserve_once, (1, 2)))

    assert sum(1 for result in results if result.granted) == 1
    assert sum(1 for result in results if not result.granted) == 1
    events = _public_alert_event_records(db_path)
    assert len(events) == 1
    assert events[0]["status"] == "RESERVED"
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        attempts = repository.list_attempts(signal_id="concurrent-watchlist")
    assert len(attempts) == 1
    assert attempts[0].telegram_status == "reserved"


































def test_missing_zone_remains_blocked(tmp_path: Path) -> None:
    sender, summary = _deliver_public_watchlist_snapshot(
        tmp_path,
        _runtime_public_watchlist_snapshot(entry_low=NA, entry_high=NA),
        "missing-zone-block",
    )

    assert summary.sent == 0
    assert sender.messages == []
    assert summary.public_watchlist_audit.public_watchlist_reconciliation_audit["blocked_by_missing_zone"] == 1


def test_missing_invalidation_remains_blocked(tmp_path: Path) -> None:
    sender, summary = _deliver_public_watchlist_snapshot(
        tmp_path,
        _runtime_public_watchlist_snapshot(invalidation=NA),
        "missing-invalidation-block",
    )

    assert summary.sent == 0
    assert sender.messages == []
    assert summary.public_watchlist_audit.public_watchlist_reconciliation_audit["blocked_by_missing_invalidation"] == 1






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
    symbol = _with_lifecycle_fields(symbol, entry_low=NA, entry_high=NA)

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


def test_near_miss_with_b_plus_rr_zone_stop_stays_internal(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / "b-plus-watch.db", settings=Settings(), sender=sender)

    summary = run(service.deliver_for_run(_run_result(_near_miss_watchlist_symbol(grade="B+")), scan_run_id="b-plus-watch"))

    assert summary.public_watchlist_audit.public_watchlist_bridge["public_watchlist_trade_ideas_created"] == 0
    assert summary.sent == 0
    assert sender.messages == []


def test_near_miss_with_a_minus_rr_zone_stop_stays_internal(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=tmp_path / "a-minus-watch.db", settings=Settings(), sender=sender)

    summary = run(service.deliver_for_run(_run_result(_near_miss_watchlist_symbol(grade="A-")), scan_run_id="a-minus-watch"))

    assert summary.public_watchlist_audit.public_watchlist_bridge["public_watchlist_trade_ideas_created"] == 0
    assert summary.sent == 0
    assert sender.messages == []


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
    assert summary.public_watchlist_audit.blocked_by_reason["missing_invalidation"] >= 1
    assert sender.messages == []


















def test_live_scanner_alerts_false_does_not_send_public_watchlist(tmp_path: Path) -> None:
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=tmp_path / "live-disabled.db",
        settings=Settings(telegram_public_watchlist_enabled=False),
        sender=sender,
    )

    summary = run(service.deliver_for_run(_run_result(_public_v1_symbol(symbol="ENAUSDT", direction="short", rr=Decimal("3.5"))), scan_run_id="live-disabled"))

    assert summary.sent == 0
    assert sender.messages == []






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
    assert summary.public_watchlist_audit.blocked_by_reason["missing_invalidation"] >= 1
    assert sender.messages == []















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








def _attempt_count(db_path: Path, attempted_alert_type: str = TelegramAlertType.SIGNAL_CONFIRMED.value) -> int:
    with sqlite3.connect(db_path) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts WHERE attempted_alert_type = ?",
            (attempted_alert_type,),
        ).fetchone()[0]


def _assert_confirmed_public_delivery_policy_disabled(
    summary,
    outbound_messages,
    db_path: Path,
) -> None:
    audit = summary.confirmed_alert_audit
    assert summary.sent == 0
    assert summary.failed == 0
    assert summary.duplicate == 0
    assert audit.public_signal_policy == "setup_only"
    assert audit.confirmed_candidates_seen == 1
    assert audit.confirmed_prefilter_passed == 0
    assert audit.confirmed_policy_disabled == 1
    assert audit.signal_confirmed_attempts_created == 0
    assert audit.signal_confirmed_sent == 0
    assert audit.policy_disabled_by_reason == {
        CONFIRMED_PUBLIC_DELIVERY_POLICY_DISABLED_REASON: 1
    }
    assert audit.blocked_before_attempt_by_reason == {}
    assert list(outbound_messages) == []
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT attempted_alert_type, telegram_status
            FROM telegram_alert_attempts
            """
        ).fetchall()
    assert not any(
        row[0] == TelegramAlertType.SIGNAL_CONFIRMED.value
        and row[1] in {"sent", "failed", "duplicate"}
        for row in rows
    )





































def test_confirmed_signal_gates_unchanged() -> None:
    test_confirmed_signal_gates_are_unchanged()








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
        pullback_failure_reason="No valid OB or FVG was found inside the 15m displacement impulse.",
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







def test_mstr_style_confirmed_regime_rejection_remains_internal_and_silent(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_prior_active_alert(db_path, signal_id="mstr-watch", alert_type=TelegramAlertType.WATCHLIST, symbol="MSTRUSDT", direction="long")
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(_env_file=None), sender=sender)
    confirmed = _with_lifecycle_fields(
        _symbol(SetupLifecycleState.CONFIRMED, previous=None, signal_id="mstr-terminal-life", diagnostics=_diagnostics(bias="long", direction="long")).model_copy(update={"symbol": "MSTRUSDT"}),
        failed_gate="regime_compatibility",
        invalidation_reason="Wait for cleaner regime. Setup rejected by regime weakness: penalty 15.",
    )

    first = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-mstr-1"))
    second = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-mstr-2"))
    third = run(service.deliver_for_run(_run_result(confirmed), scan_run_id="run-mstr-3"))

    assert first.sent == 0
    assert second.sent == 0
    assert third.sent == 0
    assert sender.messages == []
    _assert_no_sent_follow_up_attempt(db_path, TelegramAlertType.NO_LONGER_TRACKING)







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













def test_terminal_update_symbol_fallback_matches_single_active_watchlist_with_original_direction(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    _seed_prior_active_alert(db_path, signal_id="original-symbol-watch", alert_type=TelegramAlertType.WATCHLIST, symbol="BTCUSDT", direction="short")
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(database_path=db_path, settings=Settings(_env_file=None), sender=sender)
    terminal = _with_lifecycle_direction(
        _symbol(SetupLifecycleState.INVALIDATED, previous=SetupLifecycleState.WATCHLISTED, signal_id="terminal-symbol-life", diagnostics=_public_ready_watchlist_diagnostics(bias=NA, direction=NA)),
        NA,
    )

    summary = run(service.deliver_for_run(_run_result(terminal), scan_run_id="run-invalid"))

    assert summary.sent == 0
    assert sender.messages == []
    _assert_no_sent_follow_up_attempt(db_path, TelegramAlertType.INVALIDATED)


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

    assert summary.blocked == 2
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT attempted_alert_type, telegram_status, blocked_reason
            FROM telegram_alert_attempts
            WHERE telegram_status = 'blocked'
            ORDER BY id
            """
        ).fetchall()
    assert rows == [
        ("SENT_WATCHLIST_RECONCILIATION", "blocked", "sent_watchlist_reconciliation_ambiguous"),
        ("SENT_WATCHLIST_RECONCILIATION", "blocked", "sent_watchlist_reconciliation_ambiguous"),
    ]






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




def test_blocked_target_integrity_persists_invalid_target_fields(tmp_path: Path) -> None:
    db_path = tmp_path / "candle_craft.db"
    sender = FakeSender()
    service = TelegramLifecycleDeliveryService(
        database_path=db_path,
        settings=Settings(_env_file=None),
        sender=sender,
    )
    symbol = _with_lifecycle_fields(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            signal_id="blocked-targets",
            diagnostics=_public_ready_watchlist_diagnostics(tp1=Decimal("99")),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
        ),
        tp1="99",
    )

    summary = run(service.deliver_for_run(_run_result(symbol), scan_run_id="run-targets"))

    assert summary.ineligible == 1
    assert summary.blocked == 0
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM telegram_alert_attempts").fetchone()[0]
    assert count == 0


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
        _with_lifecycle_fields(
            _symbol(
                SetupLifecycleState.CONFIRMED,
                previous=SetupLifecycleState.TRIGGERED,
                diagnostics=_diagnostics(stop=Decimal("103")),
                trade_idea=_trade_idea(stop_loss=Decimal("103")),
            ),
            stop_loss="103",
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    _assert_target_integrity_blocked(decision, "stop_loss")


def test_long_confirmed_blocks_when_any_target_is_below_entry() -> None:
    decision = telegram_alert_decision_for_symbol(
        _with_lifecycle_fields(
            _symbol(
                SetupLifecycleState.CONFIRMED,
                previous=SetupLifecycleState.TRIGGERED,
                diagnostics=_diagnostics(tp2=Decimal("99")),
                trade_idea=_trade_idea(take_profit_targets=(Decimal("110"), Decimal("99"), Decimal("120"))),
            ),
            tp2="99",
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    _assert_target_integrity_blocked(decision, "tp2")


def test_long_confirmed_blocks_when_targets_are_not_in_ascending_order() -> None:
    decision = telegram_alert_decision_for_symbol(
        _with_lifecycle_fields(
            _symbol(
                SetupLifecycleState.CONFIRMED,
                previous=SetupLifecycleState.TRIGGERED,
                diagnostics=_diagnostics(tp1=Decimal("112"), tp2=Decimal("111"), tp3=Decimal("120")),
                trade_idea=_trade_idea(take_profit_targets=(Decimal("112"), Decimal("111"), Decimal("120"))),
            ),
            tp1="112",
            tp2="111",
            tp3="120",
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
        _with_lifecycle_fields(
            _symbol(
                SetupLifecycleState.WATCHLISTED,
                diagnostics=_public_ready_watchlist_diagnostics(tp1=Decimal("99")),
                setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
            ),
            tp1="99",
        )
    )

    _assert_target_integrity_blocked(decision, "tp1")
    assert decision.alert_type is None


def test_watchlist_blocks_non_monotonic_targets() -> None:
    decision = telegram_alert_decision_for_symbol(
        _with_lifecycle_fields(
            _symbol(
                SetupLifecycleState.WATCHLISTED,
                diagnostics=_public_ready_watchlist_diagnostics(tp1=Decimal("110"), tp2=Decimal("109"), tp3=Decimal("120")),
                setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=88),
            ),
            tp1="110",
            tp2="109",
            tp3="120",
        )
    )

    _assert_target_integrity_blocked(decision, "tp_order")
    assert decision.alert_type is None


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
        _with_lifecycle_fields(
            _symbol(
                SetupLifecycleState.CONFIRMED,
                previous=SetupLifecycleState.TRIGGERED,
                diagnostics=_diagnostics(entry_low=NA, entry_high=NA, entry_zone=NA, watch_zone=NA, entry=NA),
                trade_idea=_trade_idea(entry_low=None, entry_high=None),
            ),
            entry_low=NA,
            entry_high=NA,
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert decision.reason == "invalid_stored_plan_geometry:missing_entry_low"


def test_confirmed_signal_blocks_missing_stop() -> None:
    decision = telegram_alert_decision_for_symbol(
        _with_lifecycle_fields(
            _symbol(
                SetupLifecycleState.CONFIRMED,
                previous=SetupLifecycleState.TRIGGERED,
                diagnostics=_diagnostics(stop=NA, stop_loss=NA),
                trade_idea=_trade_idea(stop_loss=None),
            ),
            stop_loss=NA,
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert decision.reason == "invalid_stored_plan_geometry:missing_stop_loss"


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

    assert "🧠 Edge" in text
    assert "🧠 Edge" in text
    for forbidden in ("Decimal(", "{", "}", "true", "false", "funding_rate:", "open_interest:"):
        assert forbidden not in text







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
        rr_planned="3",
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
    assert decision.message.planned_rr == "3"






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

    assert attempt.sent == 0
    assert len(sender.messages) == 1
    assert not any("TAKE PROFIT HIT" in message for message in sender.messages)
    _assert_suppressed_follow_up(db_path, TelegramAlertType.TP1_HIT)


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

    assert attempt.sent == 0
    assert len(sender.messages) == 1
    assert not any("TAKE PROFIT HIT" in message for message in sender.messages)
    _assert_suppressed_follow_up(db_path, TelegramAlertType.TP1_HIT)


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

    assert (tp1.sent, tp2.sent, tp3.sent, repeat.sent) == (0, 0, 0, 0)
    assert len(sender.messages) == 1
    assert not any("TAKE PROFIT HIT" in message for message in sender.messages)
    rows = _watchlist_outcome_rows(db_path)
    assert not any(row[1] == "sent" and row[2] in {TelegramAlertType.TP1_HIT.value, TelegramAlertType.TP2_HIT.value, TelegramAlertType.TP3_HIT.value} for row in rows)
    assert any(row[1] == "skipped" and row[3] == "public_watchlist_follow_up_updates_disabled" for row in rows)


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

    assert sl.sent == 0
    assert repeat.sent == 0
    assert len(sender.messages) == 1
    assert not any("STOP HIT" in message for message in sender.messages)
    _assert_suppressed_follow_up(db_path, TelegramAlertType.SL_HIT)


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

    assert tp.sent == 0
    assert sl.sent == 0
    assert len(sender.messages) == 1
    assert len(sl_sender.messages) == 1
    assert not any("TAKE PROFIT HIT" in message for message in sender.messages)
    assert not any("STOP HIT" in message for message in sl_sender.messages)
    _assert_suppressed_follow_up(db_path, TelegramAlertType.TP1_HIT)
    _assert_suppressed_follow_up(sl_db_path, TelegramAlertType.SL_HIT)


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
