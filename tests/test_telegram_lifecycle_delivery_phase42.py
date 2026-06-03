from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal
from pathlib import Path

from app.agents.trade_idea import create_trade_idea
from app.analytics.setup_quality import SetupQualityGrade, SetupQualityResult, SetupQualityState
from app.alerts.telegram_lifecycle import (
    SQLiteTelegramAlertAttemptRepository,
    TelegramAlertType,
    TelegramEligibilityContext,
    TelegramLifecycleDeliveryService,
    _signal_id,
    telegram_alert_decision_for_symbol,
    telegram_signal_message_from_symbol,
)
from app.alerts.telegram_sender import TelegramSendResult, TelegramSender
from app.core.config import Settings
from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import FOOTER, HEADER_PREFIX, format_telegram_signal_message
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


def _public_ready_watchlist_diagnostics(**overrides: object) -> dict[str, object]:
    data = _diagnostics(
        first_failed_gate="no_ob_or_fvg_zone",
        gates_passed=("sweep", "bos_choch"),
        gates_failed=("no_ob_or_fvg_zone",),
        execution_sweep_status="passed",
        confirmation_structure_shift_status="passed",
        pullback_failure_reason="No valid OB or FVG was found inside the 5m displacement impulse.",
        next_required_conditions=(
            "A valid OB/FVG zone must form inside the displacement impulse.",
            "The OB/FVG zone must overlap the preferred fib pullback zone.",
            "RR and final quality gates must pass before confirmation.",
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


def _with_lifecycle_direction(symbol_result: ScannerSymbolResult, direction: str) -> ScannerSymbolResult:
    assert symbol_result.lifecycle_state is not None
    assert symbol_result.lifecycle_transition is not None
    record = symbol_result.lifecycle_state.model_copy(update={"direction": direction})
    transition = symbol_result.lifecycle_transition.model_copy(update={"record": record})
    return symbol_result.model_copy(update={"lifecycle_state": record, "lifecycle_transition": transition})


def _assert_target_integrity_blocked(decision, *fields: str) -> None:
    assert decision.eligible is False
    assert "target_integrity_failed" in decision.reason
    for field in fields:
        assert field in decision.reason


def _seed_prior_active_alert(
    db_path: Path,
    *,
    signal_id: str,
    alert_type: TelegramAlertType = TelegramAlertType.SIGNAL_CONFIRMED,
    status: str = "sent",
    symbol: str = "BTCUSDT",
    direction: str = "long",
) -> None:
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
                sent_at="2026-06-02T00:00:00+00:00",
                telegram_status=status,
                message_hash=f"{signal_id}-active",
                attempted_alert_type=alert_type.value,
            )
        )


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
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_public_ready_watchlist_diagnostics(),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=72),
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


def test_watchlist_alert_allows_na_rr_when_setup_is_not_fully_formed() -> None:
    weak = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.STALKING, diagnostics=_public_ready_watchlist_diagnostics(rr_to_tp2=NA))
    )

    assert weak.eligible is True
    assert weak.alert_type == TelegramAlertType.WATCHLIST
    assert weak.message is not None
    assert weak.message.planned_rr == NA


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


def test_hype_style_public_ready_watchlist_sends_watchlist_not_confirmed() -> None:
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
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=72),
        ).model_copy(update={"symbol": "HYPEUSDT"})
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert decision.message is not None
    text = format_telegram_signal_message(decision.alert_type, decision.message)
    assert "CANDLE CRAFT WATCHLIST" in text
    assert "HYPEUSDT | long" in text
    assert "CANDLE CRAFT SIGNAL CONFIRMED" not in text
    assert "Watch Zone:\n71.41 \u2013 71.68" in text
    assert "Potential Targets:" in text
    assert "TP2: 73.1" in text
    assert "Planned RR: 2.9R \u2014 watchlist only, final RR must improve to \u22653R before confirmation." in text
    assert "71.407944" not in text
    assert "70.77" in text
    assert "70.77363571" not in text
    assert "final RR" in text or "Final RR" in text
    assert "Watchlist invalidates if" in text
    assert "System:\nWatchlist only. No active signal yet." in text


def test_watchlist_context_avoids_awkward_raw_confirmation_wording() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.STALKING,
            diagnostics=_public_ready_watchlist_diagnostics(
                first_failed_gate="missing_confirmation_structure_shift",
                confirmation_bos_choch_reason="bullish BOS/CHoCH confirmed by candle close above a previous LTF swing high.",
            ),
        )
    )

    assert decision.eligible is True
    assert decision.message is not None
    text = format_telegram_signal_message(decision.alert_type, decision.message)
    context = text.split("Current Context:\n", 1)[1].split("\n\nNeeds Next:", 1)[0]
    assert "because bullish BOS/CHoCH confirmed" not in context
    assert "fresh LTF BOS/CHoCH confirmation is still required" in context


def test_rr_below_min_watchlist_never_routes_to_signal_confirmed() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.CONFIRMED,
            previous=SetupLifecycleState.TRIGGERED,
            diagnostics=_public_ready_watchlist_diagnostics(rr_to_tp2=Decimal("2.9")),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=72),
        ),
        eligibility_context=TelegramEligibilityContext(min_rr=Decimal("3"), min_score_for_idea=Decimal("80")),
    )

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST
    assert decision.message is not None
    text = format_telegram_signal_message(decision.alert_type, decision.message)
    assert "CANDLE CRAFT SIGNAL CONFIRMED" not in text
    assert "Final RR" in text


def test_action_watchlist_only_sends_watchlist_not_confirmed() -> None:
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

    assert decision.eligible is True
    assert decision.alert_type == TelegramAlertType.WATCHLIST


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
    assert "CANDLE CRAFT WATCHLIST" in sender.messages[0]
    assert "CANDLE CRAFT SIGNAL CONFIRMED" in sender.messages[1]


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
    assert "CANDLE CRAFT WATCHLIST" in sender.messages[0]
    assert "CANDLE CRAFT INVALIDATION" in sender.messages[1]
    assert sender.messages[1].startswith(HEADER_PREFIX)
    assert sender.messages[1].endswith(FOOTER)
    assert "Status:\nINVALIDATED" in sender.messages[1]
    assert "Signal ID:\nsig-invalidates" in sender.messages[1]
    assert "System:\nWatchlist removed from active tracking." in sender.messages[1]
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
    assert "Status:\nEXPIRED" in sender.messages[1]
    assert "Signal ID:\nsig-expires" in sender.messages[1]
    assert "Watchlist expired because it did not confirm within the valid tracking window." in sender.messages[1]
    assert "System:\nWatchlist expired. No active signal." in sender.messages[1]
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
    assert "Status:\nNO LONGER TRACKING" in sender.messages[1]
    assert "Signal ID:\nsig-cooldown" in sender.messages[1]
    assert "Watchlist removed because the setup entered cooldown before confirmation." in sender.messages[1]
    assert "System:\nWatchlist removed from active tracking." in sender.messages[1]
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

    assert blocked.blocked == 1
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
    assert rows[0][0] == TelegramAlertType.WATCHLIST.value
    assert rows[0][1] == "blocked"
    assert "watchlist_missing_trackable_plan" in rows[0][2]
    assert rows[1][0] == TelegramAlertType.INVALIDATED.value
    assert rows[1][1] == "blocked"
    assert rows[1][2] == "terminal_update_no_prior_public_alert"


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
    assert f"Signal ID:\n{original_signal_id}" in sender.messages[0]
    assert "Signal ID:\nnew-life" not in sender.messages[0]
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
    assert "BTCUSDT | short" in sender.messages[0]
    assert "Signal ID:\noriginal-symbol-watch" in sender.messages[0]
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
    assert "watchlist_missing_trackable_plan" in row[2]
    assert row[3] == "run-blocked"


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
        setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=72),
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

    assert first.blocked == 1
    assert second.blocked_repeat == 1
    assert sender.messages == []
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            """
            SELECT seen_count, first_seen_at, last_seen_at, scan_run_id, last_scan_run_id, last_error_message
            FROM telegram_alert_attempts
            """
        ).fetchall()
    assert len(rows) == 1
    assert rows[0][0] == 2
    assert rows[0][1] != NA
    assert rows[0][2] != NA
    assert rows[0][1] != rows[0][2]
    assert rows[0][3] == "run-blocked"
    assert rows[0][4] == "run-blocked-repeat"
    assert "watchlist_missing_trackable_plan" in rows[0][5]


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

    assert first.blocked == 1
    assert second.blocked == 1
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT blocked_reason, seen_count FROM telegram_alert_attempts ORDER BY id"
        ).fetchall()
    assert len(rows) == 2
    assert "watchlist_missing_trackable_plan" in rows[0][0]
    assert "watchlist_not_public_ready:missing_public_fields=direction" in rows[1][0]
    assert rows[0][1] == 1
    assert rows[1][1] == 1


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

    assert first.blocked == 1
    assert second.blocked == 1
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute("SELECT signal_id, seen_count FROM telegram_alert_attempts").fetchall()
    assert {row[0] for row in rows} == {"blocked-watch-one", "blocked-watch-two"}
    assert {row[1] for row in rows} == {1}


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

    assert watch_summary.blocked == 1
    assert confirmed_summary.blocked == 1
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT attempted_alert_type, telegram_status, seen_count FROM telegram_alert_attempts ORDER BY id"
        ).fetchall()
    assert rows == [
        (TelegramAlertType.WATCHLIST.value, "blocked", 1),
        (TelegramAlertType.SIGNAL_CONFIRMED.value, "blocked", 1),
    ]


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
    assert blocked.blocked == 1
    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT attempted_alert_type, telegram_status, seen_count FROM telegram_alert_attempts ORDER BY id"
        ).fetchall()
    assert rows == [
        (TelegramAlertType.WATCHLIST.value, "sent", 1),
        (TelegramAlertType.WATCHLIST.value, "blocked", 1),
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
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=72),
        )
    )

    _assert_target_integrity_blocked(decision, "tp1")
    assert decision.alert_type == TelegramAlertType.WATCHLIST


def test_watchlist_blocks_non_monotonic_targets() -> None:
    decision = telegram_alert_decision_for_symbol(
        _symbol(
            SetupLifecycleState.WATCHLISTED,
            diagnostics=_public_ready_watchlist_diagnostics(tp1=Decimal("110"), tp2=Decimal("109"), tp3=Decimal("120")),
            setup_quality=_setup_quality(SetupQualityState.WATCHLIST_NEAR_MISS, quality_score=72),
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
    assert "core_status_blocked:scanned_no_setup" in decision.reason
    assert "setup_quality_blocked:rejected_no_edge" in decision.reason
    assert "rejection_reason_present" in decision.reason


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

    assert "Confluence:" in text
    assert "funding is normal while open interest is falling" in text
    assert "Volume is candle-estimated." in text
    for forbidden in ("Decimal(", "{", "}", "true", "false", "funding_rate:", "open_interest:"):
        assert forbidden not in text


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
