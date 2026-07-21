from __future__ import annotations

from decimal import Decimal

from app.lifecycle.eligibility import (
    ResearchWatchEligibilityConfig,
    active_signal_eligible,
    has_valid_rr,
    is_internal_touch_state,
    is_public_active_state,
    is_public_signal_eligible_state,
    is_numeric_trade_value,
    is_terminal_state,
    public_watchlist_eligible,
    research_watch_eligible,
    requires_existing_public_signal_for_update,
)


def _watch_record(**overrides):
    record = {
        "symbol": "BTCUSDT",
        "current_state": "ACTIONABLE_A_GRADE",
        "archived_at": None,
        "direction": "long",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "115",
        "tp3": "120",
        "rr": "3",
        "quality_grade_current": "A",
        "quality_score": "88",
        "actionability_state": "A_GRADE_ACTIONABLE",
        "failed_gate": "N/A",
        "rejection_reason": "N/A",
        "blocked_reason": "N/A",
        "invalidation_reason": "A close beyond the stored stop invalidates this plan.",
        "invalidation_logic": "A close beyond the stored stop invalidates this plan.",
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
    assert public_watchlist_eligible(_watch_record(quality_grade_current="N/A", quality_score="N/A")) is False
    assert public_watchlist_eligible(_watch_record(rr="2.9")) is False
    assert public_watchlist_eligible(_watch_record(rr="2.4")) is False
    assert has_valid_rr(_watch_record(rr="2.9"), Decimal("3")) is False
    assert public_watchlist_eligible(_watch_record(failed_gate="rejected_by_regime")) is False
    assert public_watchlist_eligible(_watch_record(rejection_reason="rejected_no_edge")) is False
    assert public_watchlist_eligible(_watch_record(rejection_reason="scanned_no_setup")) is False


def test_public_watchlist_eligible_blocks_rr_below_public_minimum_gate() -> None:
    record = _watch_record(rr="2.6", failed_gate="rr_below_minimum")

    assert public_watchlist_eligible(record) is False


def test_public_watchlist_eligible_blocks_first_seen_triggered_confirmation_pending() -> None:
    record = _watch_record(current_state="TRIGGERED", actionability_state="N/A", failed_gate="missing_confirmation")

    assert public_watchlist_eligible(record) is False


def test_public_watchlist_eligible_rejects_rr_below_public_minimum_gate() -> None:
    record = _watch_record(rr="2.49", failed_gate="rr_below_minimum")

    assert public_watchlist_eligible(record) is False


def test_public_watchlist_eligible_rejects_terminal_or_archived_states() -> None:
    for state in ("INVALIDATED", "COOLDOWN", "ARCHIVED", "REJECTED", "EXPIRED", "TP1_HIT", "TP2_HIT", "SL_HIT"):
        assert is_terminal_state(state) is True
        assert public_watchlist_eligible(_watch_record(current_state=state)) is False
    assert public_watchlist_eligible(_watch_record(archived_at="2026-06-07T00:00:00Z")) is False


def test_active_signal_eligible_accepts_only_active_lifecycle_states() -> None:
    active = _watch_record(current_state="CONFIRMED")
    assert active_signal_eligible(active) is True
    assert active_signal_eligible(_watch_record(current_state="LIMIT_HIT")) is False
    assert active_signal_eligible(_watch_record(current_state="limit_zone_hit")) is False
    assert active_signal_eligible(_watch_record(current_state="MANAGING")) is True
    assert active_signal_eligible(_watch_record(current_state="WATCHLISTED")) is False
    assert active_signal_eligible(_watch_record(current_state="INVALIDATED")) is False


def test_public_signal_state_helpers_keep_limit_hit_internal() -> None:
    assert is_public_signal_eligible_state("CONFIRMED") is True
    assert is_public_signal_eligible_state("LIMIT_HIT") is False
    assert is_public_active_state("limit_hit") is False
    assert is_public_active_state("limit_zone_hit") is False
    assert is_internal_touch_state("ENTRY_ZONE_TOUCHED") is True
    assert is_internal_touch_state("limit_zone_hit") is True
    assert requires_existing_public_signal_for_update("limit_hit") is True


def _research_record(**overrides):
    record = {
        "symbol": "FILUSDT",
        "status": "rejected_by_regime",
        "display_bucket": "near_miss",
        "setup_quality_score": "70",
        "readiness_score": "55",
        "next_trigger_needed": "Wait for failed gate to clear / 5m BOS/CHoCH.",
        "regime_state": "HIGH_VOLATILITY",
        "regime_compatibility_label": "Hostile",
        "regime_confidence": "9",
        "rejection_reason": "Setup rejected by regime weakness; scalp compatibility Hostile.",
        "archived_at": None,
        "cooldown_until": None,
        "current_state": "N/A",
    }
    record.update(overrides)
    return record


def test_research_watch_accepts_regime_blocked_near_miss_without_public_watchlist_promotion() -> None:
    record = _research_record()

    assert research_watch_eligible(record) is True
    assert public_watchlist_eligible(record) is False


def test_research_watch_respects_quality_readiness_and_terminal_guards() -> None:
    config = ResearchWatchEligibilityConfig(min_quality=60, min_readiness=50)

    assert research_watch_eligible(_research_record(setup_quality_score="59"), config) is False
    assert research_watch_eligible(_research_record(readiness_score="49"), config) is False
    assert research_watch_eligible(_research_record(current_state="INVALIDATED"), config) is False
    assert research_watch_eligible(_research_record(cooldown_until="2999-01-01T00:00:00+00:00"), config) is False
    assert research_watch_eligible(_research_record(rejection_reason="No valid setup."), config) is False
