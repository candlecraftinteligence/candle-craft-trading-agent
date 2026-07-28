from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data.dtos import NA, MaybeDecimal, MaybeInt

OUTPUT_QUANT = Decimal("0.00000001")
MAX_SCORE = Decimal("100")
DEFAULT_REQUIRED_RR = Decimal("2.5")
CHALLENGE_REQUIRED_RR = Decimal("3.0")
MIN_SCANNER_RR = Decimal("2.0")

STRUCTURE_WEIGHT = Decimal("25")
PULLBACK_WEIGHT = Decimal("20")
RR_WEIGHT = Decimal("20")
CONTEXT_WEIGHT = Decimal("15")
DERIVATIVES_WEIGHT = Decimal("10")
EXECUTION_RISK_WEIGHT = Decimal("10")

DATA_ISSUE_GATES = {
    "no_execution_candles",
    "missing_confirmation_candles",
    "not_enough_candles",
    "atr_unavailable",
    "current_price",
    "scanner_error",
}
CONFIRMATION_GATES = {"missing_confirmation_structure_shift"}
PULLBACK_GATES = {
    "no_ob_or_fvg_zone",
    "pullback_too_deep",
    "pullback_beyond_786",
    "wick_sweep_reclaim",
    "body_acceptance_failure",
    "structural_breakdown",
    "no_displacement_candle",
    "challenge_limit_entry_missing",
    "missing_displacement_impulse",
    "missing_stop",
    "entry_window_expired",
}
RR_GATES = {"missing_rr", "missing_target", "rr_below_minimum", "challenge_rr_below_3", "rr_too_low"}
TARGET_INTEGRITY_GATES = {
    "target_integrity",
    "target_integrity_failed",
    "target_order_invalid",
    "targets_not_monotonic",
    "invalid_tp_sequence",
}
FINAL_RR_REJECTION_GATES = RR_GATES | TARGET_INTEGRITY_GATES
FINAL_QUALITY_GATES = {
    "trust_meter_below_minimum",
    "challenge_trust_below_85",
    "challenge_illiquid_token",
    "challenge_btc_abnormal",
    "challenge_event_window",
    "btc_volatility_guard",
    "btc_d_guard",
    "event_guard",
    "derivatives_conflict",
    "funding_oi_guard",
    "quality_filter",
    *TARGET_INTEGRITY_GATES,
}
LATE_GATES = PULLBACK_GATES | RR_GATES | FINAL_QUALITY_GATES


class SetupQualityState(str, Enum):
    HIGH_QUALITY_TRADE = "HIGH_QUALITY_TRADE"
    VALID_BUT_LOWER_QUALITY = "VALID_BUT_LOWER_QUALITY"
    WATCHLIST_NEAR_MISS = "WATCHLIST_NEAR_MISS"
    REJECTED_NO_EDGE = "REJECTED_NO_EDGE"
    DATA_ISSUE = "DATA_ISSUE"


class SetupQualityGrade(str, Enum):
    A_PLUS = "A+"
    A = "A"
    A_MINUS = "A-"
    B_PLUS = "B+"
    B = "B"
    B_MINUS = "B-"
    C = "C"
    REJECT = "Reject"
    NO_TRADE = "No trade"
    NA = "N/A"


class SetupQualityFactor(BaseModel):
    name: str
    score: int
    weight: int
    contribution: Decimal
    status: Literal["strong", "neutral", "weak", "N/A"] = "neutral"
    note: str = NA

    model_config = ConfigDict(frozen=True)


class SetupQualityInput(BaseModel):
    """Normalized inputs for the post-strategy setup quality layer."""

    symbol: str = NA
    setup_valid: bool = False
    mode: str = NA
    bias: str = NA
    rr_to_tp2: MaybeDecimal = NA
    required_rr: Decimal = DEFAULT_REQUIRED_RR
    sweep_passed: bool = False
    confirmation_passed: bool = False
    confirmation_timeframe: str = "15m"
    pullback_valid: bool = False
    ob_or_fvg_valid: bool = False
    fib_valid: bool = False
    volume_confirmed: bool = False
    late_pullback: bool = False
    htf_2d_trend: str = NA
    mtf_12h_trend: str = NA
    trend: str = NA
    trust_percentage: MaybeInt = NA
    poc_available: bool = False
    value_area_available: bool = False
    derivatives_supports_trade: bool | Literal["N/A"] = NA
    derivatives_score: MaybeInt = NA
    funding_status: str = NA
    oi_direction: str = NA
    price_oi_relationship: str = NA
    crowding_risk: str = NA
    squeeze_risk: str = NA
    risk_approved: bool | Literal["N/A"] = NA
    best_rr: MaybeDecimal = NA
    leverage_risk_level: str = NA
    data_quality_score: MaybeDecimal = NA
    stop_distance_pct: MaybeDecimal = NA
    first_failed_gate: str = NA
    gates_passed: tuple[str, ...] = ()
    gates_failed: tuple[str, ...] = ()
    hard_rejection_reasons: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()
    derivatives_missing_data: tuple[str, ...] = ()
    derivatives_unverified_data: tuple[str, ...] = ()
    derivatives_warnings: tuple[str, ...] = ()
    rejection_reason: str = NA

    model_config = ConfigDict(frozen=True)

    @field_validator("rr_to_tp2", "best_rr", "data_quality_score", "stop_distance_pct", mode="before")
    @classmethod
    def _normalize_decimal(cls, value: Any) -> Any:
        if _is_missing(value):
            return NA
        return _quantize(_decimal_from(value))

    @field_validator("required_rr", mode="before")
    @classmethod
    def _normalize_required_rr(cls, value: Any) -> Decimal:
        if _is_missing(value):
            return DEFAULT_REQUIRED_RR
        return _quantize(_decimal_from(value))

    @field_validator(
        "symbol",
        "mode",
        "bias",
        "confirmation_timeframe",
        "htf_2d_trend",
        "mtf_12h_trend",
        "trend",
        "funding_status",
        "oi_direction",
        "price_oi_relationship",
        "crowding_risk",
        "squeeze_risk",
        "leverage_risk_level",
        "first_failed_gate",
        "rejection_reason",
        mode="before",
    )
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        text = _display(value)
        return text if text else NA

    @field_validator(
        "gates_passed",
        "gates_failed",
        "hard_rejection_reasons",
        "missing_data",
        "unverified_data",
        "derivatives_missing_data",
        "derivatives_unverified_data",
        "derivatives_warnings",
        mode="before",
    )
    @classmethod
    def _normalize_tuple(cls, value: Any) -> tuple[str, ...]:
        return _sequence_values(value)


class SetupQualityResult(BaseModel):
    """Post-strategy quality result.

    `execution_risk_score` is 0-100 where lower is better. The weighted quality
    score uses `100 - execution_risk_score` for the execution-risk component.
    """

    is_evaluated: bool = True
    quality_state: SetupQualityState
    quality_grade: SetupQualityGrade
    quality_score: int = Field(ge=0, le=100)
    tradeability_score: int = Field(ge=0, le=100)
    profitability_edge_score: int = Field(ge=0, le=100)
    execution_risk_score: int = Field(ge=0, le=100)
    strongest_factors: tuple[str, ...] = ()
    weakest_factors: tuple[str, ...] = ()
    decision_reason: str
    action_label: str
    factor_breakdown: tuple[SetupQualityFactor, ...] = ()
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


def default_setup_quality_result() -> SetupQualityResult:
    return SetupQualityResult(
        is_evaluated=False,
        quality_state=SetupQualityState.DATA_ISSUE,
        quality_grade=SetupQualityGrade.NA,
        quality_score=0,
        tradeability_score=0,
        profitability_edge_score=0,
        execution_risk_score=100,
        strongest_factors=(),
        weakest_factors=("setup quality not evaluated",),
        decision_reason="Setup quality has not been evaluated.",
        action_label="Reject — data issue",
        factor_breakdown=(),
    )


def validate_setup_quality(quality_input: SetupQualityInput | Mapping[str, Any] | None = None, **overrides: Any) -> SetupQualityResult:
    data = _normalize_input(quality_input, overrides)
    structure = _structure_score(data)
    pullback = _pullback_score(data)
    rr = _rr_score(data)
    context = _context_score(data)
    derivatives = _derivatives_score(data)
    execution_risk = _execution_risk_score(data)

    factors = (
        _factor("Structure quality", structure, STRUCTURE_WEIGHT, _structure_note(data)),
        _factor("Pullback quality", pullback, PULLBACK_WEIGHT, _pullback_note(data)),
        _factor("RR / profit potential", rr, RR_WEIGHT, _rr_note(data)),
        _factor("Context quality", context, CONTEXT_WEIGHT, _context_note(data)),
        _factor("Derivatives quality", derivatives, DERIVATIVES_WEIGHT, _derivatives_note(data)),
        _factor("Execution risk", MAX_SCORE - execution_risk, EXECUTION_RISK_WEIGHT, _execution_note(execution_risk)),
    )
    quality_score = _weighted_quality_score(factors)
    tradeability_score = _bounded_int(
        structure * Decimal("0.35")
        + pullback * Decimal("0.30")
        + rr * Decimal("0.20")
        + (MAX_SCORE - execution_risk) * Decimal("0.15")
    )
    profitability_edge_score = _bounded_int(
        rr * Decimal("0.35")
        + context * Decimal("0.30")
        + derivatives * Decimal("0.20")
        + structure * Decimal("0.15")
    )

    severe_derivatives = _severe_derivatives_conflict(data)
    required_data_issue = _required_data_issue(data)
    confirmation_missing = data.sweep_passed and not data.confirmation_passed and _failed_gate(data) in CONFIRMATION_GATES
    core_passed = data.sweep_passed and data.confirmation_passed
    rr_value = _best_rr_value(data)
    rr_meets_required = rr_value != NA and rr_value >= data.required_rr and not _final_rr_validation_failed(data)
    rr_below_scanner_minimum = rr_value == NA or rr_value < MIN_SCANNER_RR
    late_gate_failed = _failed_gate(data) in LATE_GATES or bool(set(data.gates_failed) & LATE_GATES)

    if required_data_issue:
        state = SetupQualityState.DATA_ISSUE
    elif not data.setup_valid:
        if core_passed and late_gate_failed:
            state = SetupQualityState.WATCHLIST_NEAR_MISS if quality_score >= 40 else SetupQualityState.REJECTED_NO_EDGE
        else:
            state = SetupQualityState.REJECTED_NO_EDGE
    elif severe_derivatives and core_passed:
        state = SetupQualityState.REJECTED_NO_EDGE
    elif rr_below_scanner_minimum:
        state = SetupQualityState.REJECTED_NO_EDGE
    elif _high_quality(data, quality_score, context, execution_risk, rr_meets_required):
        state = SetupQualityState.HIGH_QUALITY_TRADE
    elif quality_score >= 55:
        state = SetupQualityState.VALID_BUT_LOWER_QUALITY
    else:
        state = SetupQualityState.REJECTED_NO_EDGE

    grade = _grade(state, quality_score)
    strongest = _strongest_factors(data, factors)
    weakest = _weakest_factors(data, factors, execution_risk)
    action = _action_label(
        state,
        confirmation_missing=confirmation_missing,
        failed_gate=_failed_gate(data),
        weakest=weakest,
    )
    reason = _decision_reason(
        data,
        state,
        confirmation_missing=confirmation_missing,
        severe_derivatives=severe_derivatives,
        weakest=weakest,
    )

    return SetupQualityResult(
        quality_state=state,
        quality_grade=grade,
        quality_score=quality_score,
        tradeability_score=tradeability_score,
        profitability_edge_score=profitability_edge_score,
        execution_risk_score=_bounded_int(execution_risk),
        strongest_factors=strongest,
        weakest_factors=weakest,
        decision_reason=reason,
        action_label=action,
        factor_breakdown=factors,
        missing_data=_unique_strings((*data.missing_data, *data.derivatives_missing_data)),
        unverified_data=_unique_strings((*data.unverified_data, *data.derivatives_unverified_data, *data.derivatives_warnings)),
    )


def _normalize_input(
    quality_input: SetupQualityInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> SetupQualityInput:
    if quality_input is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(quality_input, SetupQualityInput):
        raw = quality_input.model_dump()
        raw.update(overrides)
    else:
        raw = dict(quality_input)
        raw.update(overrides)

    mode = _display(raw.get("mode")).lower()
    if "required_rr" not in raw or _is_missing(raw.get("required_rr")):
        raw["required_rr"] = CHALLENGE_REQUIRED_RR if mode == "challenge" else DEFAULT_REQUIRED_RR
    return SetupQualityInput.model_validate(raw)


def _structure_score(data: SetupQualityInput) -> Decimal:
    score = Decimal("0")
    if data.sweep_passed:
        score += Decimal("35")
    if data.confirmation_passed:
        score += Decimal("40")
    score += _alignment_score(data) * Decimal("15") / Decimal("100")
    if _failed_gate(data) not in {"missing_confirmed_sweep", "missing_confirmation_structure_shift"}:
        score += Decimal("10")
    return _bounded_decimal(score)


def _pullback_score(data: SetupQualityInput) -> Decimal:
    score = Decimal("0")
    if data.pullback_valid:
        score += Decimal("35")
    if data.ob_or_fvg_valid:
        score += Decimal("25")
    if data.fib_valid:
        score += Decimal("25")
    if not data.late_pullback and _failed_gate(data) not in {"entry_window_expired"}:
        score += Decimal("15")
    return _bounded_decimal(score)


def _rr_score(data: SetupQualityInput) -> Decimal:
    rr = _best_rr_value(data)
    if rr == NA:
        return Decimal("0")
    if rr < data.required_rr:
        return _bounded_decimal((rr / data.required_rr) * Decimal("55"))
    score = Decimal("60")
    if rr >= data.required_rr + Decimal("0.5"):
        score += Decimal("20")
    if rr >= Decimal("4.0"):
        score += Decimal("10")
    if rr >= Decimal("5.0"):
        score += Decimal("10")
    return _bounded_decimal(score)


def _context_score(data: SetupQualityInput) -> Decimal:
    score = _alignment_score(data) * Decimal("40") / Decimal("100")
    trust = _numeric_score(data.trust_percentage)
    if trust != NA:
        score += trust * Decimal("40") / Decimal("100")
    elif data.setup_valid:
        score += Decimal("25")
    if data.poc_available:
        score += Decimal("12")
    if data.value_area_available:
        score += Decimal("8")
    return _bounded_decimal(score)


def _derivatives_score(data: SetupQualityInput) -> Decimal:
    if _severe_derivatives_conflict(data):
        return Decimal("0")
    score = Decimal("0")
    if data.derivatives_supports_trade is True:
        score += Decimal("45")
    elif data.derivatives_supports_trade is False:
        score += Decimal("10")
    else:
        score += Decimal("25")

    derivative_context_score = _numeric_score(data.derivatives_score)
    if derivative_context_score != NA:
        score += derivative_context_score * Decimal("35") / Decimal("100")
    else:
        score += Decimal("15")

    if data.funding_status == "normal":
        score += Decimal("10")
    elif data.funding_status in ("elevated_positive", "elevated_negative"):
        score += Decimal("5")
    elif data.funding_status == NA:
        score += Decimal("3")

    if data.crowding_risk == "low":
        score += Decimal("10")
    elif data.crowding_risk == "medium":
        score += Decimal("5")
    elif data.crowding_risk == NA:
        score += Decimal("3")
    return _bounded_decimal(score)


def _execution_risk_score(data: SetupQualityInput) -> Decimal:
    risk = Decimal("10")
    if data.risk_approved is False:
        risk += Decimal("35")
    elif data.risk_approved == NA:
        risk += Decimal("5")
    if _best_rr_value(data) == NA:
        risk += Decimal("15")
    elif _best_rr_value(data) < data.required_rr:
        risk += Decimal("15")
    if not data.pullback_valid:
        risk += Decimal("15")
    if not data.confirmation_passed:
        risk += Decimal("20")
    if data.data_quality_score != NA and data.data_quality_score < Decimal("60"):
        risk += Decimal("15")
    risk += _leverage_penalty(data.leverage_risk_level)
    if data.stop_distance_pct != NA:
        if data.stop_distance_pct > Decimal("5"):
            risk += Decimal("15")
        elif data.stop_distance_pct > Decimal("3"):
            risk += Decimal("10")
    if _required_data_issue(data):
        risk += Decimal("35")
    optional_missing_count = len(
        [
            item
            for item in data.derivatives_missing_data
            if not item.startswith(("liquidation_data", "cvd", "btc", "event", "sector"))
        ]
    )
    risk += Decimal(min(10, optional_missing_count * 2))
    if data.unverified_data or data.derivatives_unverified_data or data.derivatives_warnings:
        risk += Decimal("5")
    return _bounded_decimal(risk)


def _factor(name: str, score: Decimal, weight: Decimal, note: str) -> SetupQualityFactor:
    bounded = _bounded_int(score)
    status: Literal["strong", "neutral", "weak", "N/A"]
    if note == NA:
        status = "N/A"
    elif bounded >= 75:
        status = "strong"
    elif bounded >= 50:
        status = "neutral"
    else:
        status = "weak"
    return SetupQualityFactor(
        name=name,
        score=bounded,
        weight=int(weight),
        contribution=_quantize(score / MAX_SCORE * weight),
        status=status,
        note=note,
    )


def _weighted_quality_score(factors: Sequence[SetupQualityFactor]) -> int:
    total = sum((factor.contribution for factor in factors), Decimal("0"))
    return _bounded_int(total)


def _high_quality(
    data: SetupQualityInput,
    quality_score: int,
    context_score: Decimal,
    execution_risk: Decimal,
    rr_meets_required: bool,
) -> bool:
    return (
        data.setup_valid
        and data.sweep_passed
        and data.confirmation_passed
        and data.pullback_valid
        and rr_meets_required
        and context_score >= Decimal("70")
        and not _severe_derivatives_conflict(data)
        and execution_risk <= Decimal("35")
        and quality_score >= 85
    )


def _required_data_issue(data: SetupQualityInput) -> bool:
    if _failed_gate(data) in DATA_ISSUE_GATES:
        return True
    if any(item.startswith("candles:") for item in data.missing_data):
        return True
    return False


def _severe_derivatives_conflict(data: SetupQualityInput) -> bool:
    if _failed_gate(data) in {"derivatives_conflict", "funding_oi_guard"}:
        return data.sweep_passed and data.confirmation_passed
    if data.derivatives_supports_trade is False and data.crowding_risk == "high":
        return data.sweep_passed and data.confirmation_passed
    if data.derivatives_supports_trade is False and data.funding_status in ("extreme_positive", "extreme_negative"):
        return data.sweep_passed and data.confirmation_passed
    return False


def _alignment_score(data: SetupQualityInput) -> Decimal:
    expected = _expected_trend(data.bias)
    if expected == NA:
        return Decimal("35")
    trends = [data.htf_2d_trend, data.mtf_12h_trend, data.trend]
    known = [trend for trend in trends if trend != NA and trend != "neutral"]
    if not known:
        return Decimal("45")
    aligned = sum(1 for trend in known if trend == expected)
    conflicted = sum(1 for trend in known if trend != expected)
    if aligned >= 2 and conflicted == 0:
        return Decimal("100")
    if aligned >= 1 and conflicted == 0:
        return Decimal("70")
    if aligned >= 1 and conflicted >= 1:
        return Decimal("45")
    return Decimal("10")


def _expected_trend(bias: str) -> str:
    if bias == "long":
        return "bullish"
    if bias == "short":
        return "bearish"
    return NA


def _best_rr_value(data: SetupQualityInput) -> MaybeDecimal:
    if data.rr_to_tp2 != NA:
        return data.rr_to_tp2
    return data.best_rr


def _grade(state: SetupQualityState, quality_score: int) -> SetupQualityGrade:
    if state == SetupQualityState.DATA_ISSUE:
        return SetupQualityGrade.NA
    if state == SetupQualityState.REJECTED_NO_EDGE:
        return SetupQualityGrade.REJECT
    if quality_score >= 90:
        return SetupQualityGrade.A_PLUS
    if quality_score >= 85:
        return SetupQualityGrade.A
    if quality_score >= 80:
        return SetupQualityGrade.A_MINUS
    if quality_score >= 75:
        return SetupQualityGrade.B_PLUS
    if quality_score >= 65:
        return SetupQualityGrade.B
    if quality_score >= 55:
        return SetupQualityGrade.B_MINUS
    if quality_score >= 50:
        return SetupQualityGrade.C
    return SetupQualityGrade.REJECT


def _confirmation_timeframe(data: SetupQualityInput) -> str:
    timeframe = _display(data.confirmation_timeframe)
    return timeframe if timeframe != NA else "15m"


def _strongest_factors(data: SetupQualityInput, factors: Sequence[SetupQualityFactor]) -> tuple[str, ...]:
    values: list[str] = []
    if data.sweep_passed:
        values.append("clean sweep")
    if data.confirmation_passed:
        values.append(f"clean {_confirmation_timeframe(data)} BOS/CHoCH")
    if data.pullback_valid:
        values.append("pullback zone valid")
    if _best_rr_value(data) != NA and _best_rr_value(data) >= data.required_rr and not _final_rr_validation_failed(data):
        values.append("RR meets threshold")
    if _alignment_score(data) >= Decimal("70"):
        values.append("context aligned")
    if data.derivatives_supports_trade is True and not _severe_derivatives_conflict(data):
        values.append("derivatives supportive")
    if not values:
        values.extend(f"{factor.name} {factor.score}/100" for factor in sorted(factors, key=lambda item: item.score, reverse=True)[:2])
    return _unique_strings(values[:4])


def _weakest_factors(
    data: SetupQualityInput,
    factors: Sequence[SetupQualityFactor],
    execution_risk: Decimal,
) -> tuple[str, ...]:
    values: list[str] = []
    rr = _best_rr_value(data)
    if not data.confirmation_passed and data.sweep_passed:
        values.append("confirmation missing")
    if not data.pullback_valid:
        values.append("pullback zone missing")
    if data.late_pullback or _failed_gate(data) == "entry_window_expired":
        values.append("late pullback")
    if rr == NA or rr < data.required_rr:
        values.append("marginal RR")
    elif _final_rr_validation_failed(data):
        values.append("final target/RR validation failed")
    if _alignment_score(data) < Decimal("50"):
        values.append("trend conflict")
    if _context_score(data) < Decimal("70"):
        values.append("weak context")
    if not data.poc_available:
        values.append("weak volume/POC alignment")
    if _severe_derivatives_conflict(data):
        values.append("severe derivatives conflict")
    elif _derivatives_score(data) < Decimal("70"):
        values.append("mixed derivatives")
    if data.stop_distance_pct != NA and data.stop_distance_pct > Decimal("3"):
        values.append("wide stop")
    if execution_risk > Decimal("35"):
        values.append("execution risk elevated")
    if not values:
        values.extend(f"{factor.name} {factor.score}/100" for factor in sorted(factors, key=lambda item: item.score)[:2])
    return _unique_strings(values[:5])


def _action_label(
    state: SetupQualityState,
    *,
    confirmation_missing: bool,
    failed_gate: str,
    weakest: Sequence[str],
) -> str:
    if state == SetupQualityState.DATA_ISSUE:
        return "Reject — data issue"
    if confirmation_missing:
        return "Wait for confirmation"
    if state == SetupQualityState.HIGH_QUALITY_TRADE:
        return "Trade candidate"
    if state == SetupQualityState.VALID_BUT_LOWER_QUALITY:
        if "pullback zone missing" in weakest or failed_gate in PULLBACK_GATES:
            return "Wait for cleaner pullback"
        return "Trade candidate"
    if state == SetupQualityState.WATCHLIST_NEAR_MISS:
        if failed_gate in PULLBACK_GATES or "pullback zone missing" in weakest or "marginal RR" in weakest:
            return "Wait for cleaner pullback"
        return "Watchlist only"
    return "Reject — no edge"


def _decision_reason(
    data: SetupQualityInput,
    state: SetupQualityState,
    *,
    confirmation_missing: bool,
    severe_derivatives: bool,
    weakest: Sequence[str],
) -> str:
    if state == SetupQualityState.DATA_ISSUE:
        return "Required market data is missing; validation is unreliable."
    if confirmation_missing:
        return f"Sweep passed but {_confirmation_timeframe(data)} BOS/CHoCH confirmation is missing."
    if state == SetupQualityState.HIGH_QUALITY_TRADE:
        return "Valid setup has clean structure, acceptable RR, supportive context, and manageable execution risk."
    if state == SetupQualityState.WATCHLIST_NEAR_MISS:
        return f"Sweep and {_confirmation_timeframe(data)} BOS/CHoCH passed, but later quality gates still need improvement."
    if severe_derivatives:
        return "Technical gates progressed, but derivatives conflict removes the edge."
    if state == SetupQualityState.VALID_BUT_LOWER_QUALITY:
        weakness_text = ", ".join(weakest[:3]) if weakest else "minor quality weaknesses"
        return f"Technically valid setup has quality weaknesses: {weakness_text}."
    return "Setup quality does not provide enough deterministic edge."


def _structure_note(data: SetupQualityInput) -> str:
    if data.sweep_passed and data.confirmation_passed:
        return f"Sweep and {_confirmation_timeframe(data)} BOS/CHoCH passed."
    if data.sweep_passed:
        return "Sweep passed but confirmation is missing."
    return "Core sweep/confirmation structure is incomplete."


def _pullback_note(data: SetupQualityInput) -> str:
    if data.pullback_valid:
        return "Pullback zone is valid."
    if _failed_gate(data) in PULLBACK_GATES:
        return "Pullback quality gate failed."
    return "Pullback zone is N/A or not validated."


def _rr_note(data: SetupQualityInput) -> str:
    rr = _best_rr_value(data)
    if rr == NA:
        return "RR is N/A."
    if rr >= data.required_rr:
        if _final_rr_validation_failed(data):
            return f"RR {rr} meets numeric threshold, but final target/RR validation failed."
        return f"RR {rr} meets required {data.required_rr}."
    return f"RR {rr} is below required {data.required_rr}."


def _context_note(data: SetupQualityInput) -> str:
    if _alignment_score(data) >= Decimal("70"):
        return "HTF and setup context are aligned."
    if _alignment_score(data) < Decimal("50"):
        return "Trend context conflicts with setup direction."
    return "Context is mixed or partially unavailable."


def _derivatives_note(data: SetupQualityInput) -> str:
    if _severe_derivatives_conflict(data):
        return "Severe derivatives conflict."
    if data.derivatives_supports_trade is True:
        return "Derivatives are supportive or not conflicting."
    if data.derivatives_supports_trade is False:
        return "Derivatives are mixed against the setup."
    return "Derivatives are N/A or partially unverified."


def _execution_note(execution_risk: Decimal) -> str:
    if execution_risk <= Decimal("25"):
        return "Execution risk is acceptable."
    if execution_risk <= Decimal("45"):
        return "Execution risk is moderate."
    return "Execution risk is elevated."


def _final_rr_validation_failed(data: SetupQualityInput) -> bool:
    gates = {_failed_gate(data), *data.gates_failed}
    return bool(gates & FINAL_RR_REJECTION_GATES)


def _failed_gate(data: SetupQualityInput) -> str:
    if data.first_failed_gate != NA:
        return data.first_failed_gate
    if data.gates_failed:
        return data.gates_failed[0]
    return NA


def _numeric_score(value: Any) -> MaybeDecimal:
    if _is_missing(value):
        return NA
    try:
        return _bounded_decimal(_decimal_from(value))
    except ValueError:
        return NA


def _leverage_penalty(value: str) -> Decimal:
    return {
        "standard": Decimal("0"),
        "high": Decimal("8"),
        "extreme": Decimal("15"),
        "dangerous": Decimal("25"),
    }.get(value, Decimal("0"))


def _bounded_decimal(value: Decimal) -> Decimal:
    return min(MAX_SCORE, max(Decimal("0"), value))


def _bounded_int(value: Decimal | int) -> int:
    decimal = value if isinstance(value, Decimal) else Decimal(value)
    return int(_bounded_decimal(decimal).to_integral_value(rounding="ROUND_HALF_UP"))


def _decimal_from(value: Any) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid setup quality decimal: {value!r}") from exc
    if not decimal.is_finite():
        raise ValueError(f"Invalid setup quality decimal: {value!r}")
    return decimal


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _sequence_values(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, Sequence):
        return ()
    output: list[str] = []
    for value in values:
        text = _display(value)
        if text != NA and text not in output:
            output.append(text)
    return tuple(output)


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        text = _display(value)
        if text != NA and text not in output:
            output.append(text)
    return tuple(output)


def _display(value: Any) -> str:
    if _is_missing(value):
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value).strip() or NA


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


__all__ = [
    "SetupQualityFactor",
    "SetupQualityGrade",
    "SetupQualityInput",
    "SetupQualityResult",
    "SetupQualityState",
    "default_setup_quality_result",
    "validate_setup_quality",
]
