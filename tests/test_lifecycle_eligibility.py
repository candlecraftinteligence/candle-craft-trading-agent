from __future__ import annotations

from decimal import Decimal

from app.lifecycle.eligibility import (
    active_signal_eligible,
    has_valid_rr,
    is_numeric_trade_value,
    is_terminal_state,
    public_watchlist_eligible,
)


def _watch_record(**overrides):
    record = {
        "symbol": "BTCUSDT",
        "current_state": "WATCHLISTED",
        "archived_at": None,
        "direction": "long",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "115",
        "tp3": "120",
        "rr": "3",
        "quality_grade_current": "B+",
        "failed_gate": "N/A",
        "rejection_reason": "N/A",
        "blocked_reason": "N/A",
        "invalidation_reason": "N/A",
    }
    record.update(overrides)
    return record


def test_numeric_trade_value_rejects_na_nan_and_non_numeric_values() -> None:
    assert is_numeric_trade_value("100.25") is True
    assert is_numeric_trade_value(None) is False
    assert is_numeric_trade_value("") is False
    assert is_numeric_trade_value("N/A") is False
    assert is_numeric_trade_value("nan") is False
    assert is_numeric_trade_value("not-a-number") is False


def test_public_watchlist_eligible_requires_complete_public_trade_map() -> None:
    assert public_watchlist_eligible(_watch_record()) is True
    assert public_watchlist_eligible(_watch_record(direction="n/a")) is False
    assert public_watchlist_eligible(_watch_record(entry_low="N/A")) is False
    assert public_watchlist_eligible(_watch_record(entry_high="N/A")) is False
    assert public_watchlist_eligible(_watch_record(stop_loss="N/A")) is False
    assert public_watchlist_eligible(_watch_record(tp1="N/A")) is False


def test_public_watchlist_eligible_rejects_quality_rr_regime_and_no_edge_blockers() -> None:
    assert public_watchlist_eligible(_watch_record(quality_grade_current="Reject")) is False
    assert public_watchlist_eligible(_watch_record(quality_grade_current="N/A")) is False
    assert public_watchlist_eligible(_watch_record(rr="2.9")) is False
    assert has_valid_rr(_watch_record(rr="2.9"), Decimal("3")) is False
    assert public_watchlist_eligible(_watch_record(failed_gate="rejected_by_regime")) is False
    assert public_watchlist_eligible(_watch_record(rejection_reason="rejected_no_edge")) is False
    assert public_watchlist_eligible(_watch_record(rejection_reason="scanned_no_setup")) is False


def test_public_watchlist_eligible_rejects_terminal_or_archived_states() -> None:
    for state in ("INVALIDATED", "COOLDOWN", "ARCHIVED", "REJECTED", "EXPIRED", "TP1_HIT", "TP2_HIT", "SL_HIT"):
        assert is_terminal_state(state) is True
        assert public_watchlist_eligible(_watch_record(current_state=state)) is False
    assert public_watchlist_eligible(_watch_record(archived_at="2026-06-07T00:00:00Z")) is False


def test_active_signal_eligible_accepts_only_active_lifecycle_states() -> None:
    active = _watch_record(current_state="CONFIRMED")
    assert active_signal_eligible(active) is True
    assert active_signal_eligible(_watch_record(current_state="LIMIT_HIT")) is True
    assert active_signal_eligible(_watch_record(current_state="MANAGING")) is True
    assert active_signal_eligible(_watch_record(current_state="WATCHLISTED")) is False
    assert active_signal_eligible(_watch_record(current_state="INVALIDATED")) is False
