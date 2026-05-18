from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data.dtos import NA, MaybeDecimal, MaybeInt

OUTPUT_QUANT = Decimal("0.00000001")
DEFAULT_REQUIRED_RR = Decimal("2.5")
IDEAL_MIN_DEPTH = Decimal("0.382")
IDEAL_MAX_DEPTH = Decimal("0.618")
HARD_INVALIDATION_DEPTH = Decimal("0.786")

DATA_INCOMPLETE_GATES = {
    "no_execution_candles",
    "missing_confirmation_candles",
    "not_enough_candles",
    "atr_unavailable",
    "current_price",
    "scanner_error",
}
RR_GATES = {"missing_rr", "missing_target", "rr_below_minimum", "challenge_rr_below_3", "rr_too_low"}
NO_OB_FVG_GATES = {"no_ob_or_fvg_zone", "challenge_limit_entry_missing"}
WEAK_DISPLACEMENT_GATES = {"missing_displacement_impulse", "no_displacement_candle", "missing_stop"}
LATE_PULLBACK_GATES = {"entry_window_expired"}


class PullbackFailureType(str, Enum):
    TOO_DEEP = "TOO_DEEP"
    TOO_SHALLOW = "TOO_SHALLOW"
    NO_OB_FVG = "NO_OB_FVG"
    FIB_MISALIGNMENT = "FIB_MISALIGNMENT"
    LATE_PULLBACK = "LATE_PULLBACK"
    WEAK_DISPLACEMENT = "WEAK_DISPLACEMENT"
    OPPOSING_STRUCTURE_BLOCK = "OPPOSING_STRUCTURE_BLOCK"
    RR_COMPRESSION = "RR_COMPRESSION"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"


class PullbackQualityGrade(str, Enum):
    A = "A"
    B = "B"
    C = "C"
    REJECT = "REJECT"
    NA = "N/A"


class PullbackProjection(BaseModel):
    next_pullback_condition: str = NA
    can_reactivate_same_structure: bool | Literal["N/A"] = NA
    fresh_lifecycle_required: bool | Literal["N/A"] = NA
    lifecycle_action: Literal["NONE", "WATCHLIST", "DO_NOT_CONFIRM", "INVALIDATE", "DATA_WAIT", "N/A"] = NA
    rationale: str = NA

    model_config = ConfigDict(frozen=True)


class PullbackIntelligenceInput(BaseModel):
    symbol: str = NA
    mode: str = NA
    direction: str = NA
    pullback_zone_status: str = NA
    first_failed_gate: str = NA
    gates_passed: tuple[str, ...] = ()
    gates_failed: tuple[str, ...] = ()
    hard_rejection_reasons: tuple[str, ...] = ()
    pullback_failure_reason: str = NA
    pullback_depth_ratio: MaybeDecimal = NA
    fib_alignment_status: str = NA
    selected_zone_type: str = NA
    ob_zone: dict[str, Any] = Field(default_factory=dict)
    fvg_zone: dict[str, Any] = Field(default_factory=dict)
    sweep_magnitude_atr: MaybeDecimal = NA
    pullback_sweep_candle_index: MaybeInt = NA
    pullback_bos_choch_candle_index: MaybeInt = NA
    displacement_start_index: MaybeInt = NA
    displacement_end_index: MaybeInt = NA
    candles_15m_count: int = 0
    candles_5m_count: int = 0
    pullback_calculation_timeframe: str = NA
    rr_to_tp2: MaybeDecimal = NA
    required_rr: Decimal = DEFAULT_REQUIRED_RR
    execution_sweep_status: str = NA
    confirmation_structure_shift_status: str = NA
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)

    @field_validator(
        "symbol",
        "mode",
        "direction",
        "pullback_zone_status",
        "first_failed_gate",
        "pullback_failure_reason",
        "fib_alignment_status",
        "selected_zone_type",
        "pullback_calculation_timeframe",
        "execution_sweep_status",
        "confirmation_structure_shift_status",
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
        mode="before",
    )
    @classmethod
    def _normalize_tuple(cls, value: Any) -> tuple[str, ...]:
        return _sequence_values(value)

    @field_validator("pullback_depth_ratio", "sweep_magnitude_atr", "rr_to_tp2", mode="before")
    @classmethod
    def _normalize_optional_decimal(cls, value: Any) -> Any:
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
        "pullback_sweep_candle_index",
        "pullback_bos_choch_candle_index",
        "displacement_start_index",
        "displacement_end_index",
        mode="before",
    )
    @classmethod
    def _normalize_optional_int(cls, value: Any) -> Any:
        if _is_missing(value):
            return NA
        try:
            return int(value)
        except (TypeError, ValueError):
            return NA

    @field_validator("ob_zone", "fvg_zone", mode="before")
    @classmethod
    def _normalize_zone(cls, value: Any) -> dict[str, Any]:
        if hasattr(value, "model_dump"):
            return dict(value.model_dump())
        return dict(value) if isinstance(value, Mapping) else {}


class PullbackIntelligenceResult(BaseModel):
    is_diagnostic_only: bool = True
    pullback_depth_ratio: MaybeDecimal = NA
    fib_zone_status: str = NA
    ob_fvg_status: str = NA
    displacement_strength: str = NA
    candles_since_bos: MaybeInt = NA
    freshness_score: MaybeInt = NA
    rr_potential_score: MaybeInt = NA
    structure_risk_score: MaybeInt = NA
    pullback_quality_grade: PullbackQualityGrade = PullbackQualityGrade.NA
    pullback_failure_type: PullbackFailureType | Literal["N/A"] = NA
    next_pullback_condition: str = NA
    explanation: str = NA
    projection: PullbackProjection = Field(default_factory=PullbackProjection)
    warnings: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


def build_pullback_intelligence(
    input_data: PullbackIntelligenceInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> PullbackIntelligenceResult:
    payload: dict[str, Any] = {}
    if input_data is not None:
        if isinstance(input_data, PullbackIntelligenceInput):
            payload.update(input_data.model_dump())
        else:
            payload.update(dict(input_data))
    payload.update(overrides)
    data = PullbackIntelligenceInput.model_validate(payload)

    failure_type = _failure_type(data)
    fib_status = _fib_status(data)
    ob_fvg_status = _ob_fvg_status(data)
    displacement_strength = _displacement_strength(data)
    candles_since_bos = _candles_since_bos(data)
    freshness_score = _freshness_score(candles_since_bos)
    rr_score = _rr_potential_score(data, failure_type)
    risk_score = _structure_risk_score(data, failure_type)
    grade = _quality_grade(
        data=data,
        failure_type=failure_type,
        freshness_score=freshness_score,
        rr_potential_score=rr_score,
        structure_risk_score=risk_score,
        ob_fvg_status=ob_fvg_status,
        fib_status=fib_status,
    )
    projection = _projection(data, failure_type)
    return PullbackIntelligenceResult(
        pullback_depth_ratio=data.pullback_depth_ratio,
        fib_zone_status=fib_status,
        ob_fvg_status=ob_fvg_status,
        displacement_strength=displacement_strength,
        candles_since_bos=candles_since_bos,
        freshness_score=freshness_score,
        rr_potential_score=rr_score,
        structure_risk_score=risk_score,
        pullback_quality_grade=grade,
        pullback_failure_type=failure_type,
        next_pullback_condition=projection.next_pullback_condition,
        explanation=_explanation(data, failure_type),
        projection=projection,
        warnings=_warnings(data, failure_type),
    )


def _failure_type(data: PullbackIntelligenceInput) -> PullbackFailureType | Literal["N/A"]:
    gate = data.first_failed_gate
    gates_failed = set(data.gates_failed)
    reason = data.pullback_failure_reason.lower()

    if gate in DATA_INCOMPLETE_GATES or bool(gates_failed & DATA_INCOMPLETE_GATES) or _critical_missing_data(data):
        return PullbackFailureType.DATA_INCOMPLETE
    if data.pullback_depth_ratio != NA and data.pullback_depth_ratio > HARD_INVALIDATION_DEPTH:
        return PullbackFailureType.TOO_DEEP
    if gate in {"pullback_too_deep", "pullback_beyond_786"} or "beyond 0.786" in reason:
        return PullbackFailureType.TOO_DEEP
    if gate in RR_GATES or bool(gates_failed & RR_GATES):
        return PullbackFailureType.RR_COMPRESSION
    if gate in NO_OB_FVG_GATES or bool(gates_failed & NO_OB_FVG_GATES):
        return PullbackFailureType.NO_OB_FVG
    if gate in LATE_PULLBACK_GATES or bool(gates_failed & LATE_PULLBACK_GATES):
        return PullbackFailureType.LATE_PULLBACK
    if gate in WEAK_DISPLACEMENT_GATES or bool(gates_failed & WEAK_DISPLACEMENT_GATES):
        return PullbackFailureType.WEAK_DISPLACEMENT
    if "opposing" in reason and ("block" in reason or "structure" in reason or "liquidity" in reason):
        return PullbackFailureType.OPPOSING_STRUCTURE_BLOCK
    if _fib_status(data) == "failed":
        return PullbackFailureType.FIB_MISALIGNMENT
    if data.pullback_depth_ratio != NA and data.pullback_depth_ratio < IDEAL_MIN_DEPTH and data.pullback_zone_status != "valid":
        return PullbackFailureType.TOO_SHALLOW
    return NA


def _fib_status(data: PullbackIntelligenceInput) -> str:
    status = data.fib_alignment_status.lower()
    gate = data.first_failed_gate
    if gate in {"pullback_too_deep", "pullback_beyond_786"}:
        return "failed"
    if status in {"aligned", "aligned_aggressive_0_65", "valid", "passed"}:
        return "aligned"
    if status in {"failed", "failed_outside_preferred_zone", "failed_no_overlap", "pullback_too_deep"}:
        return "failed"
    if data.pullback_depth_ratio != NA:
        if IDEAL_MIN_DEPTH <= data.pullback_depth_ratio <= IDEAL_MAX_DEPTH:
            return "aligned"
        if data.pullback_depth_ratio > HARD_INVALIDATION_DEPTH:
            return "failed"
        return "imperfect"
    return NA


def _ob_fvg_status(data: PullbackIntelligenceInput) -> str:
    selected = data.selected_zone_type
    if selected != NA:
        return "present"
    if _zone_present(data.ob_zone) or _zone_present(data.fvg_zone):
        return "present"
    if data.first_failed_gate in NO_OB_FVG_GATES or "no valid ob or fvg" in data.pullback_failure_reason.lower():
        return "missing"
    return NA


def _displacement_strength(data: PullbackIntelligenceInput) -> str:
    if data.first_failed_gate in WEAK_DISPLACEMENT_GATES:
        return "weak"
    magnitude = data.sweep_magnitude_atr
    if magnitude != NA:
        if magnitude >= Decimal("0.75"):
            return "strong"
        if magnitude >= Decimal("0.35"):
            return "adequate"
        return "weak"
    if data.displacement_start_index != NA and data.displacement_end_index != NA:
        span = max(0, int(data.displacement_end_index) - int(data.displacement_start_index))
        if span <= 0:
            return "weak"
        if span <= 5:
            return "strong"
        if span <= 12:
            return "adequate"
        return "weak"
    return NA


def _candles_since_bos(data: PullbackIntelligenceInput) -> MaybeInt:
    bos_index = data.pullback_bos_choch_candle_index
    if bos_index == NA:
        return NA
    count = data.candles_5m_count if data.pullback_calculation_timeframe == "5m" else data.candles_15m_count
    if count <= 0:
        count = max(data.candles_5m_count, data.candles_15m_count)
    if count <= 0:
        return NA
    return max(0, count - 1 - int(bos_index))


def _freshness_score(candles_since_bos: MaybeInt) -> MaybeInt:
    if candles_since_bos == NA:
        return NA
    value = int(candles_since_bos)
    if value <= 5:
        return 90
    if value <= 12:
        return 70
    if value <= 24:
        return 45
    return 20


def _rr_potential_score(
    data: PullbackIntelligenceInput,
    failure_type: PullbackFailureType | Literal["N/A"],
) -> MaybeInt:
    if data.rr_to_tp2 == NA:
        return 20 if failure_type == PullbackFailureType.RR_COMPRESSION else NA
    rr = data.rr_to_tp2
    required = data.required_rr
    if rr >= required * Decimal("1.20"):
        return 90
    if rr >= required:
        return 75
    if rr >= required * Decimal("0.80"):
        return 45
    return 20


def _structure_risk_score(
    data: PullbackIntelligenceInput,
    failure_type: PullbackFailureType | Literal["N/A"],
) -> MaybeInt:
    if failure_type == PullbackFailureType.DATA_INCOMPLETE:
        return NA
    if failure_type == PullbackFailureType.TOO_DEEP:
        return 95
    if failure_type == PullbackFailureType.OPPOSING_STRUCTURE_BLOCK:
        return 90
    if failure_type == PullbackFailureType.WEAK_DISPLACEMENT:
        return 80
    if failure_type == PullbackFailureType.LATE_PULLBACK:
        return 75
    if failure_type == PullbackFailureType.NO_OB_FVG:
        return 70
    if failure_type == PullbackFailureType.RR_COMPRESSION:
        return 60
    if data.pullback_depth_ratio == NA:
        return NA
    depth = data.pullback_depth_ratio
    if IDEAL_MIN_DEPTH <= depth <= IDEAL_MAX_DEPTH:
        return 20
    if depth <= Decimal("0.65"):
        return 35
    if depth <= HARD_INVALIDATION_DEPTH:
        return 55
    return 95


def _quality_grade(
    *,
    data: PullbackIntelligenceInput,
    failure_type: PullbackFailureType | Literal["N/A"],
    freshness_score: MaybeInt,
    rr_potential_score: MaybeInt,
    structure_risk_score: MaybeInt,
    ob_fvg_status: str,
    fib_status: str,
) -> PullbackQualityGrade:
    if failure_type == PullbackFailureType.DATA_INCOMPLETE or _critical_missing_data(data):
        return PullbackQualityGrade.NA
    if failure_type in {
        PullbackFailureType.TOO_DEEP,
        PullbackFailureType.OPPOSING_STRUCTURE_BLOCK,
        PullbackFailureType.WEAK_DISPLACEMENT,
        PullbackFailureType.LATE_PULLBACK,
    }:
        return PullbackQualityGrade.REJECT
    if failure_type in {PullbackFailureType.NO_OB_FVG, PullbackFailureType.RR_COMPRESSION, PullbackFailureType.TOO_SHALLOW}:
        return PullbackQualityGrade.C
    if failure_type == PullbackFailureType.FIB_MISALIGNMENT:
        return PullbackQualityGrade.REJECT if data.pullback_zone_status == "failed" else PullbackQualityGrade.C
    if data.pullback_zone_status not in {"valid", "passed"}:
        return PullbackQualityGrade.NA

    freshness = _score_or_zero(freshness_score)
    rr = _score_or_zero(rr_potential_score)
    risk = _score_or_default(structure_risk_score, 60)
    if ob_fvg_status == "present" and fib_status == "aligned" and freshness >= 70 and rr >= 75 and risk <= 30:
        return PullbackQualityGrade.A
    if ob_fvg_status == "present" and fib_status in {"aligned", "imperfect"} and rr >= 45 and risk <= 55:
        return PullbackQualityGrade.B
    return PullbackQualityGrade.C


def _projection(
    data: PullbackIntelligenceInput,
    failure_type: PullbackFailureType | Literal["N/A"],
) -> PullbackProjection:
    if failure_type == PullbackFailureType.TOO_DEEP:
        return PullbackProjection(
            next_pullback_condition="fresh sweep + BOS required",
            can_reactivate_same_structure=False,
            fresh_lifecycle_required=True,
            lifecycle_action="INVALIDATE",
            rationale=(
                "Pullback exceeded 0.786, so intent weakened before entry. "
                "Do not reactivate from the same structure."
            ),
        )
    if failure_type == PullbackFailureType.NO_OB_FVG:
        return PullbackProjection(
            next_pullback_condition="valid OB/FVG inside displacement required",
            can_reactivate_same_structure=True,
            fresh_lifecycle_required=False,
            lifecycle_action="WATCHLIST",
            rationale="Sweep and BOS/CHoCH may remain useful, but activation requires a clean execution zone.",
        )
    if failure_type == PullbackFailureType.RR_COMPRESSION:
        return PullbackProjection(
            next_pullback_condition="better entry, wider clean TP2, or new structure required",
            can_reactivate_same_structure=True,
            fresh_lifecycle_required=False,
            lifecycle_action="DO_NOT_CONFIRM",
            rationale="Target distance is not worth the current risk.",
        )
    if failure_type == PullbackFailureType.DATA_INCOMPLETE:
        return PullbackProjection(
            next_pullback_condition="complete public pullback data required",
            can_reactivate_same_structure=NA,
            fresh_lifecycle_required=NA,
            lifecycle_action="DATA_WAIT",
            rationale="Pullback quality cannot be evaluated while required data is N/A.",
        )
    if failure_type == PullbackFailureType.TOO_SHALLOW:
        return PullbackProjection(
            next_pullback_condition="wait for pullback into 0.382-0.618 with valid OB/FVG",
            can_reactivate_same_structure=True,
            fresh_lifecycle_required=False,
            lifecycle_action="WATCHLIST",
            rationale="Pullback has not reached the preferred execution depth.",
        )
    if failure_type == PullbackFailureType.FIB_MISALIGNMENT:
        return PullbackProjection(
            next_pullback_condition="OB/FVG must align with the preferred fib zone",
            can_reactivate_same_structure=True,
            fresh_lifecycle_required=False,
            lifecycle_action="DO_NOT_CONFIRM",
            rationale="Execution zone is outside the preferred fib retracement area.",
        )
    if failure_type == PullbackFailureType.LATE_PULLBACK:
        return PullbackProjection(
            next_pullback_condition="fresh structure required before activation",
            can_reactivate_same_structure=False,
            fresh_lifecycle_required=True,
            lifecycle_action="INVALIDATE",
            rationale="The pullback context is stale.",
        )
    if failure_type == PullbackFailureType.WEAK_DISPLACEMENT:
        return PullbackProjection(
            next_pullback_condition="stronger displacement candle required",
            can_reactivate_same_structure=False,
            fresh_lifecycle_required=True,
            lifecycle_action="INVALIDATE",
            rationale="Displacement is too weak or incomplete to anchor a pullback.",
        )
    if failure_type == PullbackFailureType.OPPOSING_STRUCTURE_BLOCK:
        return PullbackProjection(
            next_pullback_condition="clean opposing structure must clear or new structure required",
            can_reactivate_same_structure=False,
            fresh_lifecycle_required=True,
            lifecycle_action="INVALIDATE",
            rationale="Opposing structure blocks clean continuation.",
        )
    return PullbackProjection(
        next_pullback_condition="maintain valid pullback, RR, and invalidation rules",
        can_reactivate_same_structure=True,
        fresh_lifecycle_required=False,
        lifecycle_action="NONE",
        rationale="Pullback diagnostics did not identify a hard pullback failure.",
    )


def _explanation(
    data: PullbackIntelligenceInput,
    failure_type: PullbackFailureType | Literal["N/A"],
) -> str:
    if failure_type == PullbackFailureType.TOO_DEEP:
        return "Pullback went beyond 0.786; intent weakened before entry and a new sweep plus BOS/CHoCH is required."
    if failure_type == PullbackFailureType.NO_OB_FVG:
        return "No clean OB/FVG execution zone was found inside the displacement; watch only until a valid zone exists."
    if failure_type == PullbackFailureType.RR_COMPRESSION:
        return "RR is compressed; target distance is not worth the current risk."
    if failure_type == PullbackFailureType.DATA_INCOMPLETE:
        return "Required pullback data is incomplete; mark the diagnostic as N/A."
    if failure_type == PullbackFailureType.TOO_SHALLOW:
        return "Pullback is too shallow for the preferred execution zone."
    if failure_type == PullbackFailureType.FIB_MISALIGNMENT:
        return "Pullback structure does not align with the preferred fib retracement zone."
    if failure_type == PullbackFailureType.LATE_PULLBACK:
        return "Pullback arrived too late for the existing structure."
    if failure_type == PullbackFailureType.WEAK_DISPLACEMENT:
        return "Displacement is too weak or incomplete to support a quality pullback."
    if failure_type == PullbackFailureType.OPPOSING_STRUCTURE_BLOCK:
        return "Opposing structure blocks clean continuation from this pullback."
    if data.pullback_zone_status in {"valid", "passed"}:
        return "Pullback remains valid under existing strategy gates."
    return NA


def _warnings(
    data: PullbackIntelligenceInput,
    failure_type: PullbackFailureType | Literal["N/A"],
) -> tuple[str, ...]:
    warnings: list[str] = []
    if failure_type != NA:
        warnings.append("Diagnostic only; does not loosen strategy gates or create a valid setup.")
    if failure_type == PullbackFailureType.TOO_DEEP:
        warnings.append("Same structure must not be reactivated after a >0.786 pullback.")
    if data.unverified_data:
        warnings.append("Some source data is Unverified.")
    return tuple(warnings)


def _critical_missing_data(data: PullbackIntelligenceInput) -> bool:
    critical_prefixes = ("candles:", "candles_15m:", "candles_5m:", "execution_candles:", "confirmation_candles:")
    return any(item.startswith(critical_prefixes) for item in data.missing_data)


def _zone_present(zone: Mapping[str, Any]) -> bool:
    value = zone.get("is_present")
    if isinstance(value, bool):
        return value
    return _display(value).lower() == "true"


def _score_or_zero(value: MaybeInt) -> int:
    return int(value) if value != NA else 0


def _score_or_default(value: MaybeInt, default: int) -> int:
    return int(value) if value != NA else default


def _sequence_values(values: Any) -> tuple[str, ...]:
    if values is None or isinstance(values, (str, bytes)):
        return ()
    if not isinstance(values, Sequence):
        return ()
    output: list[str] = []
    for value in values:
        text = _display(value)
        if text != NA:
            output.append(text)
    return tuple(output)


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


def _decimal_from(value: Any) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid pullback intelligence decimal: {value!r}") from exc
    if not decimal.is_finite():
        raise ValueError(f"Invalid pullback intelligence decimal: {value!r}")
    return decimal


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if isinstance(value, Enum):
        return str(value.value)
    if value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


__all__ = [
    "PullbackFailureType",
    "PullbackIntelligenceInput",
    "PullbackIntelligenceResult",
    "PullbackProjection",
    "PullbackQualityGrade",
    "build_pullback_intelligence",
]
