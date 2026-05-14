from __future__ import annotations

from decimal import Decimal

from app.data.dtos import NA
from app.scoring.opportunity_scoring import OpportunityScoreResult, score_opportunity


def _base_candidate(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "technical_score": Decimal("100"),
        "derivatives_score": Decimal("100"),
        "risk_approved": True,
        "best_rr": Decimal("5.0"),
        "liquidity_score": Decimal("100"),
        "catalyst_score": Decimal("100"),
        "data_quality_score": Decimal("100"),
        "invalidation_present": True,
        "setup_location": "edge",
    }
    data.update(overrides)
    return data


def _score(**overrides: object) -> OpportunityScoreResult:
    return score_opportunity(_base_candidate(**overrides))


def _has_violation(result: OpportunityScoreResult, code: str) -> bool:
    return any(violation.code == code for violation in result.hard_filter_result.violations)


def test_a_plus_score_candidate() -> None:
    result = _score()

    assert result.total_score == Decimal("100.00000000")
    assert result.grade == "A+"
    assert result.decision == "high_quality_candidate"
    assert result.hard_filter_result.passed is True


def test_a_score_candidate() -> None:
    result = _score(
        technical_score=Decimal("90"),
        derivatives_score=Decimal("80"),
        liquidity_score=Decimal("80"),
        catalyst_score=Decimal("80"),
        best_rr=Decimal("3.0"),
        data_quality_score=Decimal("80"),
    )

    assert result.total_score == Decimal("83.00000000")
    assert result.grade == "A"
    assert result.decision == "alert_candidate"


def test_b_score_watchlist() -> None:
    result = _score(
        technical_score=Decimal("80"),
        derivatives_score=Decimal("70"),
        liquidity_score=Decimal("70"),
        catalyst_score=Decimal("50"),
        best_rr=Decimal("3.0"),
        data_quality_score=Decimal("80"),
    )

    assert result.total_score == Decimal("72.00000000")
    assert result.grade == "B"
    assert result.decision == "watchlist_only"


def test_reject_risk_not_approved() -> None:
    result = _score(risk_approved=False)

    assert result.grade == "Reject"
    assert result.decision == "reject"
    assert _has_violation(result, "risk_not_approved")


def test_reject_missing_invalidation() -> None:
    result = _score(invalidation_present=False)

    assert result.grade == "Reject"
    assert result.decision == "reject"
    assert _has_violation(result, "missing_invalidation")


def test_reject_risk_reward_below_2() -> None:
    result = _score(best_rr=Decimal("1.99"))

    assert result.grade == "Reject"
    assert result.decision == "reject"
    assert result.score_breakdown.risk_reward_tier == "rejected"
    assert _has_violation(result, "risk_reward_below_minimum")


def test_reject_low_data_quality() -> None:
    result = _score(data_quality_score=Decimal("59"))

    assert result.grade == "Reject"
    assert result.decision == "reject"
    assert "data_quality" in result.unverified_data
    assert _has_violation(result, "low_data_quality")


def test_reject_middle_of_range_setup() -> None:
    result = _score(setup_location="middle")

    assert result.grade == "Reject"
    assert result.decision == "reject"
    assert _has_violation(result, "middle_of_range_setup")


def test_reject_weak_technical_score() -> None:
    result = _score(technical_score=Decimal("49"))

    assert result.grade == "Reject"
    assert result.decision == "reject"
    assert _has_violation(result, "weak_technical_score")


def test_reject_weak_derivatives_score() -> None:
    result = _score(derivatives_score=Decimal("39"))

    assert result.grade == "Reject"
    assert result.decision == "reject"
    assert _has_violation(result, "weak_derivatives_score")


def test_liquidity_missing_marked_na() -> None:
    candidate = _base_candidate()
    del candidate["liquidity_score"]

    result = score_opportunity(candidate)

    assert result.score_breakdown.liquidity_score == NA
    assert result.score_breakdown.liquidity_effective_score == Decimal("50.00000000")
    assert result.score_breakdown.liquidity_points == Decimal("7.50000000")
    assert result.score_breakdown.liquidity_status == NA
    assert "liquidity" in result.missing_data


def test_catalyst_missing_marked_na() -> None:
    candidate = _base_candidate()
    del candidate["catalyst_score"]

    result = score_opportunity(candidate)

    assert result.score_breakdown.catalyst_score == NA
    assert result.score_breakdown.catalyst_effective_score == Decimal("0E-8")
    assert result.score_breakdown.catalyst_points == Decimal("0E-8")
    assert result.score_breakdown.catalyst_status == NA
    assert "catalyst" in result.missing_data


def test_data_quality_missing_marked_na() -> None:
    candidate = _base_candidate()
    del candidate["data_quality_score"]

    result = score_opportunity(candidate)

    assert result.score_breakdown.data_quality_score == NA
    assert result.score_breakdown.data_quality_effective_score == Decimal("50.00000000")
    assert result.score_breakdown.data_quality_points == Decimal("2.50000000")
    assert result.score_breakdown.data_quality_status == NA
    assert "data_quality" in result.missing_data
    assert _has_violation(result, "low_data_quality")


def test_risk_rejection_reasons_force_reject() -> None:
    result = _score(risk_rejection_reasons=("daily risk limit exceeded",))

    assert result.grade == "Reject"
    assert result.decision == "reject"
    assert _has_violation(result, "risk_manager_rejection")
    assert "daily risk limit exceeded" in result.rejection_reasons[0]


def test_score_weighting_calculation() -> None:
    result = _score(
        technical_score=Decimal("80"),
        derivatives_score=Decimal("60"),
        liquidity_score=Decimal("40"),
        catalyst_score=Decimal("20"),
        best_rr=Decimal("2.5"),
        data_quality_score=Decimal("80"),
    )

    assert result.score_breakdown.technical_points == Decimal("24.00000000")
    assert result.score_breakdown.derivatives_points == Decimal("12.00000000")
    assert result.score_breakdown.liquidity_points == Decimal("6.00000000")
    assert result.score_breakdown.catalyst_points == Decimal("3.00000000")
    assert result.score_breakdown.risk_reward_tier == "moderate"
    assert result.score_breakdown.risk_reward_points == Decimal("9.00000000")
    assert result.score_breakdown.data_quality_points == Decimal("4.00000000")
    assert result.total_score == Decimal("58.00000000")


def test_grade_boundaries() -> None:
    cases = (
        (
            "A+",
            Decimal("90.00000000"),
            _base_candidate(catalyst_score=Decimal("33.33333333")),
        ),
        (
            "A",
            Decimal("80.00000000"),
            _base_candidate(catalyst_score=Decimal("20"), best_rr=Decimal("2.0"), data_quality_score=Decimal("60")),
        ),
        (
            "B",
            Decimal("70.00000000"),
            _base_candidate(
                technical_score=Decimal("50"),
                derivatives_score=Decimal("40"),
                catalyst_score=Decimal("93.33333333"),
                data_quality_score=Decimal("60"),
            ),
        ),
        (
            "C",
            Decimal("60.00000000"),
            _base_candidate(
                technical_score=Decimal("50"),
                derivatives_score=Decimal("40"),
                catalyst_score=Decimal("66.66666667"),
                best_rr=Decimal("2.0"),
                data_quality_score=Decimal("60"),
            ),
        ),
        (
            "Reject",
            Decimal("59.00000000"),
            _base_candidate(
                technical_score=Decimal("50"),
                derivatives_score=Decimal("40"),
                catalyst_score=Decimal("60"),
                best_rr=Decimal("2.0"),
                data_quality_score=Decimal("60"),
            ),
        ),
    )

    for expected_grade, expected_score, candidate in cases:
        result = score_opportunity(candidate)

        assert result.total_score == expected_score
        assert result.grade == expected_grade


def test_decision_boundaries() -> None:
    high_quality = score_opportunity(_base_candidate(catalyst_score=Decimal("33.33333333")))
    alert = score_opportunity(
        _base_candidate(catalyst_score=Decimal("20"), best_rr=Decimal("2.0"), data_quality_score=Decimal("60"))
    )
    watchlist = score_opportunity(
        _base_candidate(
            technical_score=Decimal("50"),
            derivatives_score=Decimal("40"),
            catalyst_score=Decimal("93.33333333"),
            data_quality_score=Decimal("60"),
        )
    )
    reject = score_opportunity(
        _base_candidate(
            technical_score=Decimal("50"),
            derivatives_score=Decimal("40"),
            catalyst_score=Decimal("86.66666667"),
            data_quality_score=Decimal("60"),
        )
    )

    assert high_quality.decision == "high_quality_candidate"
    assert alert.decision == "alert_candidate"
    assert watchlist.decision == "watchlist_only"
    assert reject.total_score == Decimal("69.00000000")
    assert reject.decision == "reject"


def test_unverified_data_is_preserved() -> None:
    result = _score(unverified_data=("funding", "liquidity"))

    assert result.unverified_data == ("funding", "liquidity")
    assert result.score_breakdown.liquidity_status == "Unverified"
