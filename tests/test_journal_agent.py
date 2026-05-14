from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.agents.journal_agent import (
    JournalEntryResult,
    JournalStatus,
    create_journal_entry,
    summarize_performance,
    update_journal_entry,
)
from app.data.dtos import NA


def _base_entry(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "trade_idea_id": "idea-1",
        "alert_id": "alert-1",
        "symbol": "BTCUSDT",
        "exchange": "Binance",
        "direction": "long",
        "timeframe": "1h",
        "setup_type": "liquidity_sweep_reclaim",
        "status": "watching",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop_loss": Decimal("95"),
        "take_profit_targets": (Decimal("112"), Decimal("120")),
        "invalidation": "Price closes below the reclaimed range low.",
        "best_rr": Decimal("3.5"),
        "confidence_score": Decimal("88"),
        "grade": "A",
        "reason_for_trade": "Technical context confirmed with derivatives participation.",
        "confirmed_facts": ("Range low reclaimed",),
        "missing_data": ("funding: N/A",),
        "unverified_data": ("open_interest: Unverified",),
        "risk_warning": "This is not financial advice. Size from stop-loss risk only.",
        "screenshot_url": "https://example.test/chart.png",
        "notes": "Watch the reclaim retest.",
        "emotional_notes": "Patient entry only.",
    }
    data.update(overrides)
    return data


def _entry(**overrides: object) -> JournalEntryResult:
    return create_journal_entry(_base_entry(**overrides))


def test_create_valid_journal_entry() -> None:
    result = _entry()

    assert result.symbol == "BTCUSDT"
    assert result.status == JournalStatus.WATCHING
    assert result.entry_low == Decimal("100.00000000")
    assert result.entry_high == Decimal("102.00000000")
    assert result.stop_loss == Decimal("95.00000000")
    assert result.take_profit_targets == (Decimal("112.00000000"), Decimal("120.00000000"))
    assert result.best_rr == Decimal("3.50000000")
    assert result.confidence_score == Decimal("88.00000000")
    assert result.result_r == NA


def test_reject_missing_symbol() -> None:
    data = _base_entry()
    data.pop("symbol")

    with pytest.raises(ValidationError):
        create_journal_entry(data)


def test_reject_missing_direction() -> None:
    data = _base_entry()
    data.pop("direction")

    with pytest.raises(ValidationError):
        create_journal_entry(data)


def test_reject_missing_invalidation() -> None:
    with pytest.raises(ValidationError):
        _entry(invalidation="")


def test_missing_optional_data_marked_na() -> None:
    result = _entry(
        exchange=None,
        confirmed_facts=None,
        missing_data=None,
        unverified_data=None,
        screenshot_url=None,
        notes=None,
        emotional_notes=None,
    )

    assert result.exchange == NA
    assert result.confirmed_facts == (NA,)
    assert result.missing_data == (NA,)
    assert result.unverified_data == (NA,)
    assert result.screenshot_url == NA
    assert result.notes == NA
    assert result.emotional_notes == NA


def test_update_status() -> None:
    result = update_journal_entry(_entry(), {"status": "triggered"})

    assert result.status == JournalStatus.TRIGGERED
    assert result.setup_type == "liquidity_sweep_reclaim"
    assert result.invalidation == "Price closes below the reclaimed range low."


def test_update_result_r() -> None:
    result = update_journal_entry(_entry(), {"result_r": Decimal("1.25")})

    assert result.result_r == Decimal("1.25000000")


def test_update_notes() -> None:
    result = update_journal_entry(
        _entry(),
        {
            "notes": "Triggered cleanly and moved to TP1.",
            "emotional_notes": "No chase after trigger.",
            "screenshot_url": "https://example.test/after.png",
        },
    )

    assert result.notes == "Triggered cleanly and moved to TP1."
    assert result.emotional_notes == "No chase after trigger."
    assert result.screenshot_url == "https://example.test/after.png"


def test_performance_summary_total_entries() -> None:
    summary = summarize_performance([_entry(), _entry(symbol="ETHUSDT"), _entry(symbol="SOLUSDT")])

    assert summary.total_entries == 3


def test_performance_summary_win_rate() -> None:
    entries = [
        update_journal_entry(_entry(symbol="BTCUSDT"), {"result_r": Decimal("2")}),
        update_journal_entry(_entry(symbol="ETHUSDT"), {"result_r": Decimal("-1")}),
    ]

    summary = summarize_performance(entries)

    assert summary.win_count == 1
    assert summary.loss_count == 1
    assert summary.win_rate == Decimal("50.00000000")


def test_performance_summary_average_r() -> None:
    entries = [
        update_journal_entry(_entry(symbol="BTCUSDT"), {"result_r": Decimal("2")}),
        update_journal_entry(_entry(symbol="ETHUSDT"), {"result_r": Decimal("-1")}),
        update_journal_entry(_entry(symbol="SOLUSDT"), {"result_r": Decimal("0")}),
        _entry(symbol="XRPUSDT"),
    ]

    summary = summarize_performance(entries)

    assert summary.average_r == Decimal("0.33333333")
    assert summary.best_r == Decimal("2.00000000")
    assert summary.worst_r == Decimal("-1.00000000")


def test_best_setup_type() -> None:
    entries = [
        update_journal_entry(_entry(symbol="BTCUSDT", setup_type="breakout"), {"result_r": Decimal("2")}),
        update_journal_entry(_entry(symbol="ETHUSDT", setup_type="breakout"), {"result_r": Decimal("0")}),
        update_journal_entry(_entry(symbol="SOLUSDT", setup_type="failed_retest"), {"result_r": Decimal("-1")}),
    ]

    summary = summarize_performance(entries)

    assert summary.best_setup_type == "breakout"


def test_worst_setup_type() -> None:
    entries = [
        update_journal_entry(_entry(symbol="BTCUSDT", setup_type="breakout"), {"result_r": Decimal("2")}),
        update_journal_entry(_entry(symbol="ETHUSDT", setup_type="breakout"), {"result_r": Decimal("0")}),
        update_journal_entry(_entry(symbol="SOLUSDT", setup_type="failed_retest"), {"result_r": Decimal("-1")}),
    ]

    summary = summarize_performance(entries)

    assert summary.worst_setup_type == "failed_retest"


def test_unresolved_entries_excluded_from_win_rate() -> None:
    entries = [
        update_journal_entry(_entry(symbol="BTCUSDT"), {"result_r": Decimal("2")}),
        update_journal_entry(_entry(symbol="ETHUSDT"), {"result_r": Decimal("-1")}),
        update_journal_entry(_entry(symbol="SOLUSDT"), {"result_r": Decimal("0")}),
        _entry(symbol="XRPUSDT"),
    ]

    summary = summarize_performance(entries)

    assert summary.win_rate == Decimal("50.00000000")


def test_preserves_risk_warning() -> None:
    risk_warning = "This is not financial advice. Leverage increases liquidation risk."

    result = _entry(risk_warning=risk_warning)

    assert result.risk_warning == risk_warning


def test_preserves_unverified_data() -> None:
    result = _entry(unverified_data=("funding: Unverified", "open_interest: Unverified"))

    assert result.unverified_data == ("funding: Unverified", "open_interest: Unverified")
