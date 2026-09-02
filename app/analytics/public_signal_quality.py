from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.dtos import NA

MIN_PUBLIC_SIGNAL_GRADE = "A"
MIN_PUBLIC_SETUP_QUALITY_SCORE = Decimal("88")

GRADE_RANKS: dict[str, int] = {
    "no_trade": 0,
    "no trade": 0,
    "reject": 1,
    "rejected": 1,
    "c": 2,
    "b-": 3,
    "b": 4,
    "b+": 5,
    "a-": 6,
    "a": 7,
    "a+": 8,
}

SCORE_GRADE_BANDS: tuple[tuple[Decimal, str], ...] = (
    (Decimal("90"), "A+"),
    (Decimal("85"), "A"),
    (Decimal("80"), "A-"),
    (Decimal("75"), "B+"),
    (Decimal("65"), "B"),
    (Decimal("55"), "B-"),
    (Decimal("50"), "C"),
    (Decimal("0"), "Reject"),
)


@dataclass(frozen=True)
class PublicSignalQualityDecision:
    passed: bool
    reason: str
    grade: str = NA
    rank: int | None = None
    source: str = NA


@dataclass(frozen=True)
class CanonicalPublicSetupQualityDecision:
    passed: bool
    blockers: tuple[str, ...]
    score: Decimal | None = None
    grade: str = NA
    min_score: Decimal = MIN_PUBLIC_SETUP_QUALITY_SCORE
    min_grade: str = MIN_PUBLIC_SIGNAL_GRADE


def canonical_public_setup_quality_score(record: Any) -> Decimal | None:
    """Return the only setup-quality score allowed to back a public claim.

    A public score exists only when the scanner's SetupQualityResult was genuinely
    evaluated. Opportunity, readiness, technical, confidence, and trust metrics are
    deliberately not fallback sources for this value.
    """

    quality = getattr(record, "setup_quality", None)
    if quality is None or getattr(quality, "is_evaluated", False) is not True:
        return None
    return _decimal_or_none(getattr(quality, "quality_score", None))


def canonical_public_setup_quality_grade(record: Any) -> str:
    quality = getattr(record, "setup_quality", None)
    if quality is None or getattr(quality, "is_evaluated", False) is not True:
        return NA
    grade = getattr(quality, "quality_grade", None)
    if hasattr(grade, "value"):
        grade = grade.value
    return normalize_grade(grade)


def canonical_public_setup_quality_decision(
    record: Any,
    *,
    min_score: Decimal | str | int | float = MIN_PUBLIC_SETUP_QUALITY_SCORE,
    min_grade: str = MIN_PUBLIC_SIGNAL_GRADE,
) -> CanonicalPublicSetupQualityDecision:
    minimum_score = _decimal_or_none(min_score)
    if minimum_score is None:
        raise ValueError(f"Unsupported minimum public setup-quality score: {min_score!r}")
    minimum_rank = grade_rank(min_grade)
    if minimum_rank is None:
        raise ValueError(f"Unsupported minimum public setup-quality grade: {min_grade!r}")

    score = canonical_public_setup_quality_score(record)
    grade = canonical_public_setup_quality_grade(record)
    blockers: list[str] = []
    if score is None:
        blockers.append("public_setup_quality_score_missing")
    elif score < minimum_score:
        blockers.append(
            f"public_setup_quality_score_below_min:{_display(score)}<{_display(minimum_score)}"
        )

    rank = grade_rank(grade)
    if rank is None:
        blockers.append("public_setup_quality_grade_missing")
    elif rank < minimum_rank:
        blockers.append(f"public_setup_quality_grade_below_min:{grade}<{normalize_grade(min_grade)}")

    return CanonicalPublicSetupQualityDecision(
        passed=not blockers,
        blockers=tuple(blockers),
        score=score,
        grade=grade,
        min_score=minimum_score,
        min_grade=normalize_grade(min_grade),
    )


def grade_rank(value: Any) -> int | None:
    grade = normalize_grade(value)
    if grade == NA:
        return None
    return GRADE_RANKS.get(_grade_key(grade))


def normalize_grade(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    key = _grade_key(text)
    labels = {
        "a+": "A+",
        "a": "A",
        "a-": "A-",
        "b+": "B+",
        "b": "B",
        "b-": "B-",
        "c": "C",
        "reject": "Reject",
        "rejected": "Reject",
        "no_trade": "No trade",
        "no trade": "No trade",
    }
    return labels.get(key, NA)


def grade_from_score(value: Any) -> str:
    score = _decimal_or_none(value)
    if score is None:
        return NA
    score = min(Decimal("100"), max(Decimal("0"), score))
    for threshold, grade in SCORE_GRADE_BANDS:
        if score >= threshold:
            return grade
    return "Reject"


def public_quality_decision(
    *,
    grade_candidates: Sequence[Any] = (),
    score_candidates: Sequence[Any] = (),
    min_grade: str = MIN_PUBLIC_SIGNAL_GRADE,
) -> PublicSignalQualityDecision:
    min_rank = grade_rank(min_grade)
    if min_rank is None:
        raise ValueError(f"Unsupported minimum public signal grade: {min_grade!r}")

    for grade_candidate in grade_candidates:
        grade = normalize_grade(grade_candidate)
        rank = grade_rank(grade)
        if rank is None:
            continue
        if rank >= min_rank:
            return PublicSignalQualityDecision(True, "public_grade_passed", grade=grade, rank=rank, source="grade")
        return PublicSignalQualityDecision(
            False,
            "below_min_public_grade",
            grade=grade,
            rank=rank,
            source="grade",
        )

    for score_candidate in score_candidates:
        grade = grade_from_score(score_candidate)
        rank = grade_rank(grade)
        if rank is None:
            continue
        if rank >= min_rank:
            return PublicSignalQualityDecision(True, "public_score_passed", grade=grade, rank=rank, source="score")
        return PublicSignalQualityDecision(
            False,
            "below_min_public_grade",
            grade=grade,
            rank=rank,
            source="score",
        )

    return PublicSignalQualityDecision(False, "below_min_public_grade", grade=NA, rank=None, source=NA)


def public_quality_passes(
    *,
    grade_candidates: Sequence[Any] = (),
    score_candidates: Sequence[Any] = (),
    min_grade: str = MIN_PUBLIC_SIGNAL_GRADE,
) -> bool:
    return public_quality_decision(
        grade_candidates=grade_candidates,
        score_candidates=score_candidates,
        min_grade=min_grade,
    ).passed


def _grade_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    return text.lower().strip().replace("_", " ").replace("-", "-")


def _decimal_or_none(value: Any) -> Decimal | None:
    text = _display(value)
    if text == NA:
        return None
    try:
        number = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _display(value: Any) -> str:
    if value is None or value == "" or value == NA:
        return NA
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, bool):
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    text = " ".join(str(value).split())
    return text if text else NA


__all__ = [
    "CanonicalPublicSetupQualityDecision",
    "GRADE_RANKS",
    "MIN_PUBLIC_SIGNAL_GRADE",
    "MIN_PUBLIC_SETUP_QUALITY_SCORE",
    "PublicSignalQualityDecision",
    "canonical_public_setup_quality_decision",
    "canonical_public_setup_quality_grade",
    "canonical_public_setup_quality_score",
    "grade_from_score",
    "grade_rank",
    "normalize_grade",
    "public_quality_decision",
    "public_quality_passes",
]
