from __future__ import annotations

from app.analytics.public_signal_quality import (
    MIN_PUBLIC_SIGNAL_GRADE,
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
