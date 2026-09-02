from __future__ import annotations

from types import SimpleNamespace

from app.analytics.public_signal_quality import (
    MIN_PUBLIC_SIGNAL_GRADE,
    MIN_PUBLIC_SETUP_QUALITY_SCORE,
    canonical_public_setup_quality_decision,
    grade_from_score,
    grade_rank,
    public_quality_decision,
)


def test_public_quality_rank_supports_full_grade_ladder() -> None:
    ordered = ["No trade", "Reject", "C", "B-", "B", "B+", "A-", "A", "A+"]

    assert [grade_rank(grade) for grade in ordered] == sorted(grade_rank(grade) for grade in ordered)
    assert MIN_PUBLIC_SIGNAL_GRADE == "A"


def test_grade_b_does_not_pass_min_public_signal_grade() -> None:
    decision = public_quality_decision(grade_candidates=("B",))

    assert decision.passed is False
    assert decision.reason == "below_min_public_grade"
    assert decision.grade == "B"


def test_grade_a_family_pass_min_public_signal_grade() -> None:
    for grade in ("A", "A+"):
        assert public_quality_decision(grade_candidates=(grade,)).passed is True


def test_explicit_grade_b_is_not_upgraded_by_numeric_score() -> None:
    decision = public_quality_decision(grade_candidates=("B",), score_candidates=(95,))

    assert decision.passed is False
    assert decision.grade == "B"
    assert decision.source == "grade"


def test_numeric_only_scores_map_to_public_grade_ranks() -> None:
    assert grade_from_score(74) == "B"
    assert public_quality_decision(score_candidates=(74,)).passed is False
    assert grade_from_score(75) == "B+"
    assert public_quality_decision(score_candidates=(75,)).passed is False
    assert grade_from_score(85) == "A"
    assert public_quality_decision(score_candidates=(85,)).passed is True

def test_grade_b_plus_and_a_minus_do_not_pass_public_signal_grade() -> None:
    for grade in ("B+", "A-"):
        decision = public_quality_decision(grade_candidates=(grade,))

        assert decision.passed is False
        assert decision.reason == "below_min_public_grade"


def _canonical_record(
    score: int = 88,
    grade: str = "A",
    *,
    evaluated: bool = True,
):
    return SimpleNamespace(
        setup_quality=SimpleNamespace(
            is_evaluated=evaluated,
            quality_score=score,
            quality_grade=grade,
        )
    )


def test_canonical_public_setup_quality_contract_matrix() -> None:
    assert MIN_PUBLIC_SETUP_QUALITY_SCORE == 88
    for score, grade, expected in (
        (81, "A-", False),
        (87, "A", False),
        (88, "A", True),
        (90, "A+", True),
    ):
        decision = canonical_public_setup_quality_decision(
            _canonical_record(score, grade)
        )
        assert decision.passed is expected


def test_canonical_quality_missing_fails_closed_without_metric_substitution() -> None:
    record = SimpleNamespace(
        setup_quality=SimpleNamespace(
            is_evaluated=False,
            quality_score=0,
            quality_grade="N/A",
        ),
        opportunity_score=99,
        readiness_score=99,
        technical_score=99,
        trade_idea=SimpleNamespace(grade="A+", confidence_score=99),
        diagnostics={"quality_grade": "A+", "quality_score": 99},
    )

    decision = canonical_public_setup_quality_decision(record)

    assert decision.passed is False
    assert "public_setup_quality_score_missing" in decision.blockers
    assert "public_setup_quality_grade_missing" in decision.blockers


def test_grade_cannot_rescue_numeric_quality_below_88() -> None:
    decision = canonical_public_setup_quality_decision(
        _canonical_record(81, "A+")
    )

    assert decision.passed is False
    assert decision.blockers[0] == "public_setup_quality_score_below_min:81<88"


def test_trade_idea_and_diagnostic_grades_cannot_rescue_canonical_81() -> None:
    record = _canonical_record(81, "A-")
    record.trade_idea = SimpleNamespace(grade="A+", confidence_score=99)
    record.diagnostics = {"quality_grade": "A+", "quality_score": 99}

    decision = canonical_public_setup_quality_decision(record)

    assert decision.passed is False
    assert "public_setup_quality_score_below_min:81<88" in decision.blockers
    assert "public_setup_quality_grade_below_min:A-<A" in decision.blockers
