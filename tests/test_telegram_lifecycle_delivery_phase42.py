from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal
from pathlib import Path

from app.agents.trade_idea import create_trade_idea
from app.alerts.telegram_lifecycle import (
    SQLiteTelegramAlertAttemptRepository,
    TelegramAlertType,
    TelegramEligibilityContext,
    TelegramLifecycleDeliveryService,
    telegram_alert_decision_for_symbol,
)
from app.alerts.telegram_sender import TelegramSendResult, TelegramSender
from app.core.config import Settings
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState, SetupTransitionReason, SetupTransitionResult
from app.storage.models import TelegramAlertAttemptRecord
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult


def run(coro):
    return asyncio.run(coro)


class FakeSender:
    def __init__(self, status: str = "sent") -> None:
        self.status = status
        self.messages: list[str] = []

    async def send_text(self, text: str) -> TelegramSendResult:
        self.messages.append(text)
        return TelegramSendResult(status=self.status, detail=f"{self.status}.")


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
        lifecycle_state=transition.record,
        lifecycle_transition=transition,
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


def _seed_prior_active_alert(db_path: Path, *, signal_id: str) -> None:
    with SQLiteTelegramAlertAttemptRepository(db_path) as repository:
        repository.insert_attempt(
            TelegramAlertAttemptRecord(
                signal_id=signal_id,
                symbol="BTCUSDT",
                direction="long",
                previous_state=NA,
                new_state="CONFIRMED",
                alert_type=TelegramAlertType.SIGNAL_CONFIRMED.value,
                lifecycle_state="CONFIRMED",
                sent_at="2026-06-02T00:00:00+00:00",
                telegram_status="sent",
                message_hash=f"{signal_id}-active",
                attempted_alert_type=TelegramAlertType.SIGNAL_CONFIRMED.value,
            )
        )


def test_confirmed_and_watchlist_lifecycle_states_are_eligible() -> None:
    confirmed = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.CONFIRMED, previous=SetupLifecycleState.TRIGGERED),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )
    watchlist = telegram_alert_decision_for_symbol(_symbol(SetupLifecycleState.WATCHLISTED))

    assert confirmed.eligible is True
    assert confirmed.alert_type == "SIGNAL_CONFIRMED"
    assert watchlist.eligible is True
    assert watchlist.alert_type == "WATCHLIST"


def test_near_miss_rejected_and_unchanged_states_are_not_eligible() -> None:
    rejected = telegram_alert_decision_for_symbol(_symbol(SetupLifecycleState.REJECTED))
    unchanged = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.CONFIRMED, transitioned=False)
    )

    assert rejected.eligible is False
    assert rejected.reason == "lifecycle_state_not_eligible"
    assert unchanged.eligible is False
    assert unchanged.reason == "unchanged_lifecycle_state"


def test_weak_watch_state_missing_required_fields_is_not_eligible() -> None:
    weak = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.STALKING, diagnostics=_diagnostics(rr_to_tp2=NA))
    )

    assert weak.eligible is False
    assert weak.reason.startswith("missing_required_fields")


def test_updates_require_prior_active_telegram_alert() -> None:
    invalidated_without_prior = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.INVALIDATED, previous=SetupLifecycleState.CONFIRMED),
        previously_active_sent=False,
    )
    invalidated_with_prior = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.INVALIDATED, previous=SetupLifecycleState.CONFIRMED),
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
    assert invalidated_without_prior.reason == "missing_prior_active_telegram_alert"
    assert invalidated_with_prior.eligible is True
    assert invalidated_with_prior.alert_type == "INVALIDATED"
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

    assert summary.skipped == 1
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT telegram_status, error_message FROM telegram_alert_attempts"
        ).fetchone()
    assert row == ("skipped", "missing_telegram_credentials")


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


def test_confirmed_alert_is_blocked_when_rejection_fields_are_present() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            rejection_reason="Opportunity score is below scanner minimum.",
            rejection_reasons=("Technical score is below 50.",),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is False
    assert "rejection_reason_present" in decision.reason
    assert "rejection_reasons_present" in decision.reason


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


def test_confirmed_alert_is_blocked_when_invalidation_is_rejection_text() -> None:
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
    assert "invalidation_contains_rejection_reason" in decision.reason


def test_blocked_confirmed_alert_persists_safe_research_metadata(tmp_path: Path) -> None:
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

    assert summary.blocked == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT telegram_status, attempted_alert_type, rr_planned, min_rr, min_score_for_idea, blocked_reason
            FROM telegram_alert_attempts
            """
        ).fetchone()
    assert row[0] == "blocked"
    assert row[1] == TelegramAlertType.SIGNAL_CONFIRMED.value
    assert row[2] == "2.79"
    assert row[3] == "3"
    assert row[4] == "80"
    assert "planned_rr_below_min" in row[5]


def test_each_alert_type_is_not_sent_twice(tmp_path: Path) -> None:
    cases = (
        (
            TelegramAlertType.WATCHLIST,
            _symbol(SetupLifecycleState.WATCHLISTED, signal_id="sig-watchlist"),
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
    )

    for alert_type, symbol_result, needs_prior_active in cases:
        db_path = tmp_path / f"{alert_type.value.lower()}.db"
        sender = FakeSender()
        signal_id = symbol_result.lifecycle_state.lifecycle_id
        if needs_prior_active:
            _seed_prior_active_alert(db_path, signal_id=signal_id)
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
