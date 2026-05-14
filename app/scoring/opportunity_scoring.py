from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.data.dtos import NA, MaybeDecimal

ScoreGrade = Literal["A+", "A", "B", "C", "Reject"]
DecisionLabel = Literal["watchlist_only", "alert_candidate", "high_quality_candidate", "reject"]
SetupLocation = Literal["edge", "middle", "breakout_retest", "unknown"]
DataStatus = Literal["Verified", "Unverified", "N/A"]
RiskRewardTier = Literal["rejected", "moderate", "strong", "excellent"]

OUTPUT_QUANT = Decimal("0.00000001")
MAX_COMPONENT_SCORE = Decimal("100")
MIN_RISK_REWARD_RATIO = Decimal("2.0")
MIN_DATA_QUALITY_SCORE = Decimal("60")
MIN_TECHNICAL_SCORE = Decimal("50")
MIN_DERIVATIVES_SCORE = Decimal("40")

TECHNICAL_WEIGHT = Decimal("30")
DERIVATIVES_WEIGHT = Decimal("20")
LIQUIDITY_WEIGHT = Decimal("15")
CATALYST_WEIGHT = Decimal("15")
RISK_REWARD_WEIGHT = Decimal("15")
DATA_QUALITY_WEIGHT = Decimal("5")

DEFAULT_LIQUIDITY_SCORE = Decimal("50")
DEFAULT_CATALYST_SCORE = Decimal("0")
DEFAULT_DATA_QUALITY_SCORE = Decimal("50")

SAFETY_NOTE = "Opportunity scoring only; no trade idea, order, or execution is created."


class OpportunityScoringInput(BaseModel):
    technical_score: Decimal
    derivatives_score: Decimal
    risk_approved: bool
    best_rr: Decimal
    liquidity_score: Decimal | None = None
    catalyst_score: Decimal | None = None
    data_quality_score: Decimal | None = None
    invalidation_present: bool
    setup_location: SetupLocation = "unknown"
    risk_rejection_reasons: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)

    @field_validator(
        "technical_score",
        "derivatives_score",
        "liquidity_score",
        "catalyst_score",
        "data_quality_score",
    )
    @classmethod
    def _score_is_in_range(cls, value: Decimal | None) -> Decimal | None:
        if value is None:
            return value
        if not value.is_finite():
            raise ValueError("score must be a finite decimal")
        if value < 0 or value > MAX_COMPONENT_SCORE:
            raise ValueError("score must be between 0 and 100")
        return value

    @field_validator("best_rr")
    @classmethod
    def _risk_reward_is_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("best_rr must be a finite decimal")
        return value

    @field_validator("risk_rejection_reasons", "missing_data", "unverified_data", mode="before")
    @classmethod
    def _normalize_string_tuple(cls, value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return value


class ScoringRuleViolation(BaseModel):
    code: str
    message: str
    severity: Literal["hard_rejection"] = "hard_rejection"

    model_config = ConfigDict(frozen=True)


class HardFilterResult(BaseModel):
    passed: bool
    violations: tuple[ScoringRuleViolation, ...] = ()

    model_config = ConfigDict(frozen=True)


class ScoreBreakdown(BaseModel):
    technical_score: Decimal
    technical_weight: Decimal = TECHNICAL_WEIGHT
    technical_points: Decimal
    technical_status: DataStatus = "Verified"
    derivatives_score: Decimal
    derivatives_weight: Decimal = DERIVATIVES_WEIGHT
    derivatives_points: Decimal
    derivatives_status: DataStatus = "Verified"
    liquidity_score: MaybeDecimal = NA
    liquidity_effective_score: Decimal = DEFAULT_LIQUIDITY_SCORE
    liquidity_weight: Decimal = LIQUIDITY_WEIGHT
    liquidity_points: Decimal
    liquidity_status: DataStatus = NA
    catalyst_score: MaybeDecimal = NA
    catalyst_effective_score: Decimal = DEFAULT_CATALYST_SCORE
    catalyst_weight: Decimal = CATALYST_WEIGHT
    catalyst_points: Decimal
    catalyst_status: DataStatus = NA
    best_rr: Decimal
    risk_reward_score: Decimal
    risk_reward_weight: Decimal = RISK_REWARD_WEIGHT
    risk_reward_points: Decimal
    risk_reward_tier: RiskRewardTier
    data_quality_score: MaybeDecimal = NA
    data_quality_effective_score: Decimal = DEFAULT_DATA_QUALITY_SCORE
    data_quality_weight: Decimal = DATA_QUALITY_WEIGHT
    data_quality_points: Decimal
    data_quality_status: DataStatus = NA

    model_config = ConfigDict(frozen=True)


class OpportunityScoreResult(BaseModel):
    total_score: Decimal
    grade: ScoreGrade
    decision: DecisionLabel
    score_breakdown: ScoreBreakdown
    hard_filter_result: HardFilterResult
    rejection_reasons: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class OpportunityScoringEngine:
    """Deterministic scoring for candidate setups only.

    The engine combines existing analysis outputs into a single score and grade.
    It does not call exchanges, use private API access, create trade ideas, place
    orders, or produce execution instructions.
    """

    def score(
        self,
        candidate: OpportunityScoringInput | Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> OpportunityScoreResult:
        scoring_input = _normalize_input(candidate, overrides)
        missing_data = _missing_data(scoring_input)
        unverified_data = _unverified_data(scoring_input)
        risk_reward_score, risk_reward_tier = _risk_reward_score(scoring_input.best_rr)

        liquidity_effective_score = (
            DEFAULT_LIQUIDITY_SCORE if scoring_input.liquidity_score is None else scoring_input.liquidity_score
        )
        catalyst_effective_score = (
            DEFAULT_CATALYST_SCORE if scoring_input.catalyst_score is None else scoring_input.catalyst_score
        )
        data_quality_effective_score = (
            DEFAULT_DATA_QUALITY_SCORE
            if scoring_input.data_quality_score is None
            else scoring_input.data_quality_score
        )

        breakdown = ScoreBreakdown(
            technical_score=_quantize(scoring_input.technical_score),
            technical_points=_weighted_points(scoring_input.technical_score, TECHNICAL_WEIGHT),
            technical_status=_status_for("technical", missing_data, unverified_data),
            derivatives_score=_quantize(scoring_input.derivatives_score),
            derivatives_points=_weighted_points(scoring_input.derivatives_score, DERIVATIVES_WEIGHT),
            derivatives_status=_status_for("derivatives", missing_data, unverified_data),
            liquidity_score=NA if scoring_input.liquidity_score is None else _quantize(scoring_input.liquidity_score),
            liquidity_effective_score=_quantize(liquidity_effective_score),
            liquidity_points=_weighted_points(liquidity_effective_score, LIQUIDITY_WEIGHT),
            liquidity_status=_status_for("liquidity", missing_data, unverified_data),
            catalyst_score=NA if scoring_input.catalyst_score is None else _quantize(scoring_input.catalyst_score),
            catalyst_effective_score=_quantize(catalyst_effective_score),
            catalyst_points=_weighted_points(catalyst_effective_score, CATALYST_WEIGHT),
            catalyst_status=_status_for("catalyst", missing_data, unverified_data),
            best_rr=_quantize(scoring_input.best_rr),
            risk_reward_score=risk_reward_score,
            risk_reward_points=_weighted_points(risk_reward_score, RISK_REWARD_WEIGHT),
            risk_reward_tier=risk_reward_tier,
            data_quality_score=NA
            if scoring_input.data_quality_score is None
            else _quantize(scoring_input.data_quality_score),
            data_quality_effective_score=_quantize(data_quality_effective_score),
            data_quality_points=_weighted_points(data_quality_effective_score, DATA_QUALITY_WEIGHT),
            data_quality_status=_status_for("data_quality", missing_data, unverified_data),
        )

        total_score = _quantize(
            breakdown.technical_points
            + breakdown.derivatives_points
            + breakdown.liquidity_points
            + breakdown.catalyst_points
            + breakdown.risk_reward_points
            + breakdown.data_quality_points
        )
        hard_filter_result = _hard_filter_result(scoring_input, data_quality_effective_score)

        grade = _grade(total_score, hard_filter_result)
        decision = _decision(total_score, hard_filter_result)
        rejection_reasons = _rejection_reasons(total_score, hard_filter_result, decision)

        return OpportunityScoreResult(
            total_score=total_score,
            grade=grade,
            decision=decision,
            score_breakdown=breakdown,
            hard_filter_result=hard_filter_result,
            rejection_reasons=rejection_reasons,
            missing_data=missing_data,
            unverified_data=unverified_data,
            notes=_notes(scoring_input, risk_reward_tier, missing_data, unverified_data),
        )


def score_opportunity(
    candidate: OpportunityScoringInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> OpportunityScoreResult:
    return OpportunityScoringEngine().score(candidate, **overrides)


def _normalize_input(
    candidate: OpportunityScoringInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> OpportunityScoringInput:
    if candidate is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(candidate, OpportunityScoringInput):
        raw = candidate.model_dump()
        raw.update(overrides)
    else:
        raw = dict(candidate)
        raw.update(overrides)
    return OpportunityScoringInput.model_validate(raw)


def _missing_data(scoring_input: OpportunityScoringInput) -> tuple[str, ...]:
    missing = list(_clean_strings(scoring_input.missing_data))
    if scoring_input.liquidity_score is None:
        missing.append("liquidity")
    if scoring_input.catalyst_score is None:
        missing.append("catalyst")
    if scoring_input.data_quality_score is None:
        missing.append("data_quality")
    return _unique(missing)


def _unverified_data(scoring_input: OpportunityScoringInput) -> tuple[str, ...]:
    unverified = list(_clean_strings(scoring_input.unverified_data))
    if scoring_input.data_quality_score is not None and scoring_input.data_quality_score < MIN_DATA_QUALITY_SCORE:
        unverified.append("data_quality")
    return _unique(unverified)


def _risk_reward_score(best_rr: Decimal) -> tuple[Decimal, RiskRewardTier]:
    if best_rr < MIN_RISK_REWARD_RATIO:
        return Decimal("0.00000000"), "rejected"
    if best_rr < Decimal("3.0"):
        return Decimal("60.00000000"), "moderate"
    if best_rr < Decimal("5.0"):
        return Decimal("80.00000000"), "strong"
    return Decimal("100.00000000"), "excellent"


def _hard_filter_result(
    scoring_input: OpportunityScoringInput,
    data_quality_effective_score: Decimal,
) -> HardFilterResult:
    violations: list[ScoringRuleViolation] = []

    if not scoring_input.risk_approved:
        violations.append(
            ScoringRuleViolation(
                code="risk_not_approved",
                message="Risk manager did not approve the setup.",
            )
        )
    if not scoring_input.invalidation_present:
        violations.append(
            ScoringRuleViolation(
                code="missing_invalidation",
                message="Setup is missing a clear invalidation condition.",
            )
        )
    if scoring_input.best_rr < MIN_RISK_REWARD_RATIO:
        violations.append(
            ScoringRuleViolation(
                code="risk_reward_below_minimum",
                message="Best risk/reward ratio is below 2.0.",
            )
        )
    if data_quality_effective_score < MIN_DATA_QUALITY_SCORE:
        message = "Data quality score is below 60 and should be treated as Unverified."
        if scoring_input.data_quality_score is None:
            message = "Data quality score is N/A; default score 50 is below the minimum 60."
        violations.append(
            ScoringRuleViolation(
                code="low_data_quality",
                message=message,
            )
        )
    if scoring_input.setup_location == "middle":
        violations.append(
            ScoringRuleViolation(
                code="middle_of_range_setup",
                message="Setup location is in the middle of the range.",
            )
        )
    if scoring_input.technical_score < MIN_TECHNICAL_SCORE:
        violations.append(
            ScoringRuleViolation(
                code="weak_technical_score",
                message="Technical score is below 50.",
            )
        )
    if scoring_input.derivatives_score < MIN_DERIVATIVES_SCORE:
        violations.append(
            ScoringRuleViolation(
                code="weak_derivatives_score",
                message="Derivatives/orderflow score is below 40.",
            )
        )

    for reason in _clean_strings(scoring_input.risk_rejection_reasons):
        violations.append(
            ScoringRuleViolation(
                code="risk_manager_rejection",
                message=f"Risk manager rejection reason: {reason}",
            )
        )

    return HardFilterResult(passed=not violations, violations=tuple(violations))


def _grade(total_score: Decimal, hard_filter_result: HardFilterResult) -> ScoreGrade:
    if not hard_filter_result.passed or total_score < Decimal("60"):
        return "Reject"
    if total_score >= Decimal("90"):
        return "A+"
    if total_score >= Decimal("80"):
        return "A"
    if total_score >= Decimal("70"):
        return "B"
    return "C"


def _decision(total_score: Decimal, hard_filter_result: HardFilterResult) -> DecisionLabel:
    if not hard_filter_result.passed:
        return "reject"
    if total_score >= Decimal("90"):
        return "high_quality_candidate"
    if total_score >= Decimal("80"):
        return "alert_candidate"
    if total_score >= Decimal("70"):
        return "watchlist_only"
    return "reject"


def _rejection_reasons(
    total_score: Decimal,
    hard_filter_result: HardFilterResult,
    decision: DecisionLabel,
) -> tuple[str, ...]:
    reasons = [violation.message for violation in hard_filter_result.violations]
    if decision == "reject" and hard_filter_result.passed:
        if total_score < Decimal("60"):
            reasons.append("Total score is below 60.")
        else:
            reasons.append("Total score is below the watchlist threshold of 70.")
    return tuple(reasons)


def _notes(
    scoring_input: OpportunityScoringInput,
    risk_reward_tier: RiskRewardTier,
    missing_data: tuple[str, ...],
    unverified_data: tuple[str, ...],
) -> tuple[str, ...]:
    notes = [SAFETY_NOTE, f"Risk/reward tier: {risk_reward_tier}."]
    if scoring_input.setup_location == "unknown":
        notes.append("Setup location is unknown.")
    if "liquidity" in missing_data:
        notes.append("Liquidity is N/A; default score 50 was used for weighting.")
    if "catalyst" in missing_data:
        notes.append("Catalyst is N/A; default score 0 was used for weighting.")
    if "data_quality" in missing_data:
        notes.append("Data quality is N/A; default score 50 was used for weighting.")
    if unverified_data:
        notes.append("Unverified data was preserved in the scoring output.")
    return tuple(notes)


def _status_for(
    component: str,
    missing_data: tuple[str, ...],
    unverified_data: tuple[str, ...],
) -> DataStatus:
    if component in missing_data:
        return NA
    if component in unverified_data:
        return "Unverified"
    return "Verified"


def _weighted_points(score: Decimal, weight: Decimal) -> Decimal:
    return _quantize(score / MAX_COMPONENT_SCORE * weight)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _clean_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value.strip())


def _unique(values: list[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        if value not in output:
            output.append(value)
    return tuple(output)


__all__ = [
    "HardFilterResult",
    "OpportunityScoreResult",
    "OpportunityScoringEngine",
    "OpportunityScoringInput",
    "ScoreBreakdown",
    "ScoreGrade",
    "ScoringRuleViolation",
    "score_opportunity",
]
