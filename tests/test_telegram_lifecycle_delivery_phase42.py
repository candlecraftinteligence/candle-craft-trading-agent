from __future__ import annotations

import asyncio
import sqlite3
from decimal import Decimal
from pathlib import Path

from app.alerts.telegram_lifecycle import (
    SQLiteTelegramAlertAttemptRepository,
    TelegramLifecycleDeliveryService,
    telegram_alert_decision_for_symbol,
)
from app.alerts.telegram_sender import TelegramSendResult, TelegramSender
from app.core.config import Settings
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState, SetupTransitionReason, SetupTransitionResult
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


def _symbol(
    state: SetupLifecycleState,
    *,
    previous: SetupLifecycleState | None = None,
    transitioned: bool = True,
    diagnostics: dict[str, object] | None = None,
    signal_id: str = "sig-001",
) -> ScannerSymbolResult:
    transition = _transition(state, previous=previous, transitioned=transitioned, signal_id=signal_id)
    return ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED
        if state not in {SetupLifecycleState.REJECTED, SetupLifecycleState.INVALIDATED}
        else ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        valid_strategy_modes=("swing",) if state != SetupLifecycleState.REJECTED else (),
        rejected_strategy_modes=("swing",) if state == SetupLifecycleState.REJECTED else (),
        strategy_diagnostics={"swing": diagnostics or _diagnostics()},
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


def test_confirmed_and_watchlist_lifecycle_states_are_eligible() -> None:
    confirmed = telegram_alert_decision_for_symbol(
        _symbol(SetupLifecycleState.CONFIRMED, previous=SetupLifecycleState.TRIGGERED)
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
