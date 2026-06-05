from __future__ import annotations

from decimal import Decimal

from app.analytics.setup_quality import CHALLENGE_REQUIRED_RR, SetupQualityState, validate_setup_quality
from app.data.dtos import NA


def _base_quality_input(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "symbol": "BTCUSDT",
        "setup_valid": True,
        "mode": "swing",
        "bias": "long",
        "rr_to_tp2": Decimal("3.2"),
        "required_rr": Decimal("2.5"),
        "sweep_passed": True,
        "confirmation_passed": True,
        "pullback_valid": True,
        "ob_or_fvg_valid": True,
        "fib_valid": True,
        "volume_confirmed": True,
        "htf_2d_trend": "bullish",
        "mtf_12h_trend": "bullish",
        "trend": "bullish",
        "trust_percentage": 90,
        "poc_available": True,
        "value_area_available": True,
        "derivatives_supports_trade": True,
        "derivatives_score": 88,
        "funding_status": "normal",
        "oi_direction": "rising",
        "price_oi_relationship": "long_building_or_breakout_participation",
        "crowding_risk": "low",
        "squeeze_risk": "balanced",
        "risk_approved": True,
        "best_rr": Decimal("3.2"),
        "leverage_risk_level": "standard",
        "data_quality_score": Decimal("90"),
        "stop_distance_pct": Decimal("1.5"),
        "gates_passed": ("sweep", "bos_choch", "pullback_zone", "fib_alignment", "rr", "trust_meter"),
    }
    data.update(overrides)
    return data


def test_high_quality_valid_setup_becomes_high_quality_trade() -> None:
    result = validate_setup_quality(_base_quality_input())

    assert result.quality_state == SetupQualityState.HIGH_QUALITY_TRADE
    assert result.quality_grade.value in {"A+", "A"}
    assert result.quality_score >= 85
    assert result.action_label == "Trade candidate"


def test_valid_but_weak_rr_and_context_is_lower_quality_or_rejected() -> None:
    result = validate_setup_quality(
        _base_quality_input(
            rr_to_tp2=Decimal("2.1"),
            best_rr=Decimal("2.1"),
            htf_2d_trend="bearish",
            mtf_12h_trend="neutral",
            trend="bearish",
            trust_percentage=76,
            poc_available=False,
        )
    )

    assert result.quality_state in {
        SetupQualityState.VALID_BUT_LOWER_QUALITY,
        SetupQualityState.REJECTED_NO_EDGE,
    }
    assert result.quality_state != SetupQualityState.HIGH_QUALITY_TRADE
    assert "marginal RR" in result.weakest_factors


def test_sweep_and_bos_passed_but_pullback_failed_becomes_watchlist() -> None:
    result = validate_setup_quality(
        _base_quality_input(
            setup_valid=False,
            pullback_valid=False,
            ob_or_fvg_valid=False,
            fib_valid=False,
            first_failed_gate="no_ob_or_fvg_zone",
            gates_failed=("no_ob_or_fvg_zone",),
            gates_passed=("sweep", "bos_choch"),
        )
    )

    assert result.quality_state == SetupQualityState.WATCHLIST_NEAR_MISS
    assert result.action_label == "Wait for cleaner pullback"


def test_missing_confirmation_waits_for_confirmation() -> None:
    result = validate_setup_quality(
        _base_quality_input(
            setup_valid=False,
            confirmation_passed=False,
            pullback_valid=False,
            ob_or_fvg_valid=False,
            fib_valid=False,
            rr_to_tp2="N/A",
            best_rr="N/A",
            first_failed_gate="missing_confirmation_structure_shift",
            gates_failed=("missing_confirmation_structure_shift",),
            gates_passed=("sweep",),
        )
    )

    assert result.quality_state == SetupQualityState.REJECTED_NO_EDGE
    assert result.action_label == "Wait for confirmation"
    assert result.decision_reason == "Sweep passed but 5m BOS/CHoCH confirmation is missing."


def test_rr_below_required_minimum_cannot_be_high_quality() -> None:
    result = validate_setup_quality(_base_quality_input(rr_to_tp2=Decimal("2.2"), best_rr=Decimal("2.2")))

    assert result.quality_state != SetupQualityState.HIGH_QUALITY_TRADE
    assert "marginal RR" in result.weakest_factors


def test_challenge_rr_269_fails_but_270_passes_quality_gate() -> None:
    below = validate_setup_quality(
        _base_quality_input(mode="challenge", required_rr=NA, rr_to_tp2=Decimal("2.69"), best_rr=Decimal("2.69"))
    )
    at_minimum = validate_setup_quality(
        _base_quality_input(mode="challenge", required_rr=NA, rr_to_tp2=Decimal("2.70"), best_rr=Decimal("2.70"))
    )

    assert CHALLENGE_REQUIRED_RR == Decimal("2.7")
    assert below.quality_state != SetupQualityState.HIGH_QUALITY_TRADE
    assert "marginal RR" in below.weakest_factors
    assert at_minimum.quality_state == SetupQualityState.HIGH_QUALITY_TRADE
    assert "RR meets threshold" in at_minimum.strongest_factors


def test_challenge_rr_270_still_fails_without_bos_choch() -> None:
    result = validate_setup_quality(
        _base_quality_input(
            mode="challenge",
            required_rr=NA,
            setup_valid=False,
            confirmation_passed=False,
            pullback_valid=False,
            ob_or_fvg_valid=False,
            fib_valid=False,
            rr_to_tp2=Decimal("2.70"),
            best_rr=Decimal("2.70"),
            first_failed_gate="missing_confirmation_structure_shift",
            gates_failed=("missing_confirmation_structure_shift",),
            gates_passed=("sweep",),
        )
    )

    assert result.quality_state == SetupQualityState.REJECTED_NO_EDGE
    assert result.action_label == "Wait for confirmation"


def test_challenge_rr_270_still_waitlists_failed_ob_fvg_or_fib_gate() -> None:
    ob_fvg_failed = validate_setup_quality(
        _base_quality_input(
            mode="challenge",
            required_rr=NA,
            setup_valid=False,
            pullback_valid=False,
            ob_or_fvg_valid=False,
            fib_valid=False,
            rr_to_tp2=Decimal("2.70"),
            best_rr=Decimal("2.70"),
            first_failed_gate="no_ob_or_fvg_zone",
            gates_failed=("no_ob_or_fvg_zone",),
            gates_passed=("sweep", "bos_choch"),
        )
    )
    fib_failed = validate_setup_quality(
        _base_quality_input(
            mode="challenge",
            required_rr=NA,
            setup_valid=False,
            fib_valid=False,
            rr_to_tp2=Decimal("2.70"),
            best_rr=Decimal("2.70"),
            first_failed_gate="pullback_beyond_786",
            gates_failed=("pullback_beyond_786",),
            gates_passed=("sweep", "bos_choch", "pullback_zone"),
        )
    )

    assert ob_fvg_failed.quality_state == SetupQualityState.WATCHLIST_NEAR_MISS
    assert fib_failed.quality_state == SetupQualityState.WATCHLIST_NEAR_MISS
    assert ob_fvg_failed.quality_state != SetupQualityState.HIGH_QUALITY_TRADE
    assert fib_failed.quality_state != SetupQualityState.HIGH_QUALITY_TRADE


def test_severe_derivatives_conflict_rejects_after_technical_gates_pass() -> None:
    result = validate_setup_quality(
        _base_quality_input(
            derivatives_supports_trade=False,
            funding_status="extreme_positive",
            crowding_risk="high",
            derivatives_score=35,
        )
    )

    assert result.quality_state == SetupQualityState.REJECTED_NO_EDGE
    assert result.action_label == "Reject — no edge"
    assert "severe derivatives conflict" in result.weakest_factors


def test_missing_optional_derivatives_reduces_confidence_without_auto_rejecting() -> None:
    result = validate_setup_quality(
        _base_quality_input(
            derivatives_supports_trade="N/A",
            derivatives_score="N/A",
            funding_status="N/A",
            oi_direction="N/A",
            price_oi_relationship="N/A",
            crowding_risk="N/A",
            squeeze_risk="N/A",
            derivatives_missing_data=(
                "funding_rate: N/A",
                "open_interest: N/A",
                "long_short_ratio: N/A",
            ),
        )
    )

    assert result.quality_state in {
        SetupQualityState.HIGH_QUALITY_TRADE,
        SetupQualityState.VALID_BUT_LOWER_QUALITY,
    }
    assert result.quality_state != SetupQualityState.REJECTED_NO_EDGE
    assert "mixed derivatives" in result.weakest_factors
