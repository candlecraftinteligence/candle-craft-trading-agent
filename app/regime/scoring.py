from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from app.data.dtos import NA
from app.regime.models import (
    MODE_ORDER,
    MarketRegimeResult,
    RegimeAdjustment,
    RegimeCompatibility,
    RegimeRiskLevel,
    RegimeState,
    RegimeStrictness,
    compatibility_label,
    confidence_band,
)

OUTPUT_QUANT = Decimal("0.00000001")

STRICTNESS_ALLOWED_THRESHOLD = {
    RegimeStrictness.LOW: 36,
    RegimeStrictness.NORMAL: 46,
    RegimeStrictness.HIGH: 58,
}
STRICTNESS_PENALTY_MULTIPLIER = {
    RegimeStrictness.LOW: Decimal("0.75"),
    RegimeStrictness.NORMAL: Decimal("1.0"),
    RegimeStrictness.HIGH: Decimal("1.25"),
}

STATE_RISK = {
    RegimeState.TREND_EXPANSION: RegimeRiskLevel.LOW,
    RegimeState.TREND_PULLBACK: RegimeRiskLevel.MEDIUM,
    RegimeState.RANGE_COMPRESSION: RegimeRiskLevel.MEDIUM,
    RegimeState.HIGH_VOLATILITY: RegimeRiskLevel.EXTREME,
    RegimeState.LOW_VOLATILITY: RegimeRiskLevel.MEDIUM,
    RegimeState.CHOP: RegimeRiskLevel.HIGH,
    RegimeState.RISK_OFF: RegimeRiskLevel.HIGH,
    RegimeState.RISK_ON: RegimeRiskLevel.LOW,
    RegimeState.MIXED: RegimeRiskLevel.MEDIUM,
    RegimeState.TRANSITION: RegimeRiskLevel.MEDIUM,
}

STATE_MODE_BASE = {
    RegimeState.TREND_EXPANSION: {"challenge": 78, "swing": 86, "scalp": 70},
    RegimeState.TREND_PULLBACK: {"challenge": 66, "swing": 78, "scalp": 64},
    RegimeState.RANGE_COMPRESSION: {"challenge": 38, "swing": 58, "scalp": 50},
    RegimeState.HIGH_VOLATILITY: {"challenge": 28, "swing": 54, "scalp": 34},
    RegimeState.LOW_VOLATILITY: {"challenge": 52, "swing": 62, "scalp": 42},
    RegimeState.CHOP: {"challenge": 24, "swing": 46, "scalp": 32},
    RegimeState.RISK_OFF: {"challenge": 34, "swing": 56, "scalp": 38},
    RegimeState.RISK_ON: {"challenge": 68, "swing": 76, "scalp": 64},
    RegimeState.MIXED: {"challenge": 40, "swing": 54, "scalp": 44},
    RegimeState.TRANSITION: {"challenge": 42, "swing": 58, "scalp": 46},
}


def risk_level_for_state(state: RegimeState, *, required_data_missing: bool = False) -> RegimeRiskLevel:
    if required_data_missing:
        return RegimeRiskLevel.NA
    return STATE_RISK.get(state, RegimeRiskLevel.MEDIUM)


def score_confidence(
    *,
    state: RegimeState,
    evidence: Mapping[str, Any],
    strictness: RegimeStrictness,
    required_data_missing: bool,
) -> tuple[int, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if required_data_missing:
        notes = (
            "Required BTC/ETH candle data is incomplete; regime stays cautious/neutral.",
        )
        return 45, notes, (), ("required BTC/ETH context missing",)

    score = Decimal("50")
    notes: list[str] = []
    boosts: list[str] = []
    penalties: list[str] = []
    penalty_multiplier = STRICTNESS_PENALTY_MULTIPLIER[strictness]

    alignment = str(evidence.get("directional_alignment", NA))
    volatility = str(evidence.get("volatility_profile", NA))
    follow_through = _decimal(evidence.get("follow_through_pct"))
    failed_confirmation = _decimal(evidence.get("failed_confirmation_pct"))
    htf_agreement = _decimal(evidence.get("htf_agreement_pct"))
    htf_conflict = _decimal(evidence.get("htf_conflict_pct"))
    rr_quality = _decimal(evidence.get("average_rr"))
    setup_density = _decimal(evidence.get("setup_density_pct"))
    rejection_clustering = _decimal(evidence.get("rejection_clustering_pct"))
    breadth_strength = _decimal(evidence.get("breadth_strength_pct"))

    if alignment in {"bullish", "bearish"}:
        score += Decimal("8")
        boosts.append("directional alignment boost")
    elif alignment == "mixed":
        penalty = Decimal("12") * penalty_multiplier
        score -= penalty
        penalties.append("mixed directional penalty")

    if volatility == "stable_expansion":
        score += Decimal("8")
        boosts.append("stable volatility expansion boost")
    elif volatility == "compressed":
        penalty = Decimal("8") * penalty_multiplier
        score -= penalty
        penalties.append("compressed volatility penalty")
    elif volatility == "unstable":
        penalty = Decimal("16") * penalty_multiplier
        score -= penalty
        penalties.append("unstable volatility penalty")

    if htf_agreement >= Decimal("65"):
        score += Decimal("8")
        boosts.append("HTF alignment boost")
    elif htf_conflict >= Decimal("35"):
        penalty = Decimal("10") * penalty_multiplier
        score -= penalty
        penalties.append("HTF conflict penalty")

    if breadth_strength >= Decimal("60"):
        score += Decimal("7")
        boosts.append("broad participation boost")
    elif Decimal("0") < breadth_strength < Decimal("45"):
        penalty = Decimal("5") * penalty_multiplier
        score -= penalty
        penalties.append("weak participation penalty")

    if rr_quality >= Decimal("3"):
        score += Decimal("6")
        boosts.append("RR expansion boost")
    elif rr_quality != Decimal("0") and rr_quality < Decimal("2.5"):
        penalty = Decimal("7") * penalty_multiplier
        score -= penalty
        penalties.append("low-quality RR expansion penalty")

    if follow_through >= Decimal("55"):
        score += Decimal("5")
        boosts.append("follow-through boost")
    elif failed_confirmation >= Decimal("45"):
        penalty = Decimal("10") * penalty_multiplier
        score -= penalty
        penalties.append("weak follow-through penalty")

    if rejection_clustering >= Decimal("45"):
        penalty = Decimal("10") * penalty_multiplier
        score -= penalty
        penalties.append("excessive rejection clustering penalty")

    if setup_density >= Decimal("25"):
        score += Decimal("3")
        boosts.append("setup density support")
    elif setup_density != Decimal("0") and setup_density < Decimal("8"):
        penalty = Decimal("4") * penalty_multiplier
        score -= penalty
        penalties.append("low setup density penalty")

    state_bias = {
        RegimeState.TREND_EXPANSION: Decimal("10"),
        RegimeState.RISK_ON: Decimal("8"),
        RegimeState.TREND_PULLBACK: Decimal("2"),
        RegimeState.RANGE_COMPRESSION: Decimal("-2"),
        RegimeState.LOW_VOLATILITY: Decimal("-4"),
        RegimeState.TRANSITION: Decimal("-5"),
        RegimeState.MIXED: Decimal("-8"),
        RegimeState.RISK_OFF: Decimal("-12"),
        RegimeState.CHOP: Decimal("-18"),
        RegimeState.HIGH_VOLATILITY: Decimal("-20"),
    }.get(state, Decimal("0"))
    if state_bias < 0:
        score += state_bias * penalty_multiplier
    else:
        score += state_bias

    missing_optional = int(evidence.get("missing_optional_count") or 0)
    if missing_optional:
        score -= Decimal(min(10, missing_optional * 2))
        penalties.append("optional regime context unavailable")

    score_int = _bounded_int(score)
    if missing_optional and score_int > 70:
        score_int = 70
    if state in (RegimeState.CHOP, RegimeState.HIGH_VOLATILITY) and score_int > 50:
        score_int = 50
    if state == RegimeState.RANGE_COMPRESSION and score_int > 68:
        score_int = 68
    notes.extend(_state_notes(state))
    notes.extend(_evidence_notes(evidence))
    return score_int, _unique(notes), _unique(boosts), _unique(penalties)


def score_compatibility(
    *,
    state: RegimeState,
    confidence_score: int,
    evidence: Mapping[str, Any],
    strictness: RegimeStrictness,
) -> dict[str, RegimeCompatibility]:
    compatibilities: dict[str, RegimeCompatibility] = {}
    volatility_scores = _volatility_suitability(state, evidence)
    trend_scores = _trend_suitability(state, evidence)
    execution_scores = _execution_suitability(state, evidence)
    threshold = STRICTNESS_ALLOWED_THRESHOLD[strictness]
    for mode in MODE_ORDER:
        regime_base = STATE_MODE_BASE.get(state, STATE_MODE_BASE[RegimeState.MIXED])[mode]
        volatility = volatility_scores[mode]
        trend = trend_scores[mode]
        execution = execution_scores[mode]
        score = _bounded_int(
            Decimal(regime_base) * Decimal("0.35")
            + Decimal(volatility) * Decimal("0.20")
            + Decimal(trend) * Decimal("0.25")
            + Decimal(execution) * Decimal("0.20")
        )
        notes = _compatibility_notes(mode, state, evidence, volatility, trend, execution)
        allowed = score >= threshold
        risk_multiplier = _mode_risk_multiplier(score, state, strictness)
        compatibilities[mode] = RegimeCompatibility(
            mode=mode,
            score=score,
            label=compatibility_label(score),
            allowed=allowed,
            regime_compatibility=regime_base,
            volatility_suitability=volatility,
            trend_suitability=trend,
            execution_quality_suitability=execution,
            risk_multiplier=risk_multiplier,
            confidence_adjustment=_confidence_adjustment(score),
            notes=notes,
        )
    return compatibilities


def build_adjustment(
    *,
    state: RegimeState,
    confidence_score: int,
    compatibility: Mapping[str, RegimeCompatibility],
    strictness: RegimeStrictness,
    notes: Sequence[str],
) -> RegimeAdjustment:
    # Regime compatibility and state remain diagnostic-only for the live decision path.
    # Keep compatibility/gating metadata for display but do not alter live RR, risk,
    # setup quality, or candidate ranking.
    compatibility_scores = {mode: item.score for mode, item in compatibility.items()}
    min_quality_adjustment = 0
    min_rr_adjustment = Decimal("0")
    risk_multiplier = Decimal("1")

    explanation = _adjustment_explanation(state, confidence_score, notes)
    return RegimeAdjustment(
        allow_scalps=_allowed(compatibility, "scalp"),
        allow_swings=_allowed(compatibility, "swing"),
        allow_challenge=_allowed(compatibility, "challenge"),
        min_quality_score_adjustment=min(30, min_quality_adjustment),
        min_rr_adjustment=min_rr_adjustment,
        risk_multiplier=risk_multiplier.quantize(OUTPUT_QUANT),
        readiness_score_adjustment=0,
        edge_score_adjustment=0,
        trust_score_adjustment=0,
        portfolio_confidence_adjustment=0,
        regime_penalty=0,
        compatibility_scores=compatibility_scores,
        explanation=explanation,
    )


def default_adjustment() -> RegimeAdjustment:
    return RegimeAdjustment(
        explanation="Market climate is N/A because required candle data is incomplete.",
    )


def disabled_adjustment() -> RegimeAdjustment:
    return RegimeAdjustment(
        allow_scalps=True,
        allow_swings=True,
        allow_challenge=True,
        min_quality_score_adjustment=0,
        min_rr_adjustment=Decimal("0"),
        risk_multiplier=Decimal("1"),
        explanation="Market climate filter disabled.",
    )


def default_result() -> MarketRegimeResult:
    adjustment = default_adjustment()
    return MarketRegimeResult(
        state=RegimeState.MIXED,
        risk_level=RegimeRiskLevel.NA,
        confidence_score=45,
        confidence_band=confidence_band(45),
        adjustment=adjustment,
        missing_data=("market_regime: N/A",),
        warnings=("Market climate has not been evaluated.",),
        environment_notes=("Missing regime context remains cautious/neutral.",),
    )


def disabled_result() -> MarketRegimeResult:
    adjustment = disabled_adjustment()
    return MarketRegimeResult(
        enabled=False,
        state=RegimeState.MIXED,
        risk_level=RegimeRiskLevel.NA,
        confidence_score=50,
        confidence_band=confidence_band(50),
        adjustment=adjustment,
        missing_data=("market_regime: N/A",),
        warnings=("Market climate filter disabled.",),
        environment_notes=("Market climate filter disabled.",),
    )


def _volatility_suitability(state: RegimeState, evidence: Mapping[str, Any]) -> dict[str, int]:
    profile = str(evidence.get("volatility_profile", NA))
    if state == RegimeState.HIGH_VOLATILITY or profile == "unstable":
        return {"challenge": 25, "swing": 58, "scalp": 32}
    if state == RegimeState.RANGE_COMPRESSION or profile == "compressed":
        return {"challenge": 42, "swing": 60, "scalp": 44}
    if state == RegimeState.LOW_VOLATILITY:
        return {"challenge": 52, "swing": 62, "scalp": 38}
    if profile == "stable_expansion":
        return {"challenge": 75, "swing": 82, "scalp": 68}
    return {"challenge": 58, "swing": 64, "scalp": 56}


def _trend_suitability(state: RegimeState, evidence: Mapping[str, Any]) -> dict[str, int]:
    alignment = str(evidence.get("directional_alignment", NA))
    htf_conflict = _decimal(evidence.get("htf_conflict_pct"))
    if state in (RegimeState.TREND_EXPANSION, RegimeState.RISK_ON) and alignment in {"bullish", "bearish"}:
        return {"challenge": 78, "swing": 86, "scalp": 68}
    if state == RegimeState.TREND_PULLBACK:
        return {"challenge": 66, "swing": 78, "scalp": 62}
    if alignment == "mixed" or htf_conflict >= Decimal("35"):
        return {"challenge": 28, "swing": 48, "scalp": 36}
    if state == RegimeState.RISK_OFF:
        return {"challenge": 38, "swing": 58, "scalp": 42}
    return {"challenge": 54, "swing": 58, "scalp": 52}


def _execution_suitability(state: RegimeState, evidence: Mapping[str, Any]) -> dict[str, int]:
    follow_through = _decimal(evidence.get("follow_through_pct"))
    failed = _decimal(evidence.get("failed_confirmation_pct"))
    rejection = _decimal(evidence.get("rejection_clustering_pct"))
    rr = _decimal(evidence.get("average_rr"))
    base = {"challenge": 58, "swing": 62, "scalp": 56}
    if follow_through >= Decimal("55"):
        base = {mode: value + 12 for mode, value in base.items()}
    if rr >= Decimal("3"):
        base = {mode: value + 6 for mode, value in base.items()}
    if failed >= Decimal("45"):
        base = {"challenge": base["challenge"] - 18, "swing": base["swing"] - 10, "scalp": base["scalp"] - 15}
    if rejection >= Decimal("45"):
        base = {"challenge": base["challenge"] - 18, "swing": base["swing"] - 12, "scalp": base["scalp"] - 18}
    if state in (RegimeState.CHOP, RegimeState.HIGH_VOLATILITY):
        base = {"challenge": base["challenge"] - 12, "swing": base["swing"] - 5, "scalp": base["scalp"] - 10}
    return {mode: _bounded_int(Decimal(value)) for mode, value in base.items()}


def _mode_risk_multiplier(score: int, state: RegimeState, strictness: RegimeStrictness) -> Decimal:
    if score <= 30:
        value = Decimal("0.35")
    elif score <= 50:
        value = Decimal("0.60")
    elif score <= 70:
        value = Decimal("0.80")
    else:
        value = Decimal("1")
    if strictness == RegimeStrictness.HIGH and value < Decimal("1"):
        value -= Decimal("0.10")
    if state == RegimeState.HIGH_VOLATILITY:
        value = min(value, Decimal("0.50"))
    return max(Decimal("0"), value).quantize(OUTPUT_QUANT)


def _confidence_adjustment(score: int) -> int:
    if score >= 86:
        return 8
    if score >= 71:
        return 4
    if score >= 51:
        return 0
    if score >= 31:
        return -6
    return -12


def _regime_penalty(confidence_score: int, strictness: RegimeStrictness) -> int:
    if confidence_score >= 71:
        base = 0
    elif confidence_score >= 51:
        base = 3
    elif confidence_score >= 31:
        base = 8
    else:
        base = 15
    multiplier = STRICTNESS_PENALTY_MULTIPLIER[strictness]
    return int((Decimal(base) * multiplier).to_integral_value(rounding="ROUND_HALF_UP"))


def _allowed(compatibility: Mapping[str, RegimeCompatibility], mode: str) -> bool:
    item = compatibility.get(mode)
    return True if item is None else item.allowed


def _adjustment_explanation(state: RegimeState, confidence_score: int, notes: Sequence[str]) -> str:
    band = confidence_band(confidence_score).value
    note = next((item for item in notes if item), NA)
    if note == NA:
        note = "Weighted regime context applied."
    prefix = {
        RegimeState.HIGH_VOLATILITY: "Panic volatility detected",
        RegimeState.CHOP: "Choppy regime",
        RegimeState.RANGE_COMPRESSION: "Compression detected",
        RegimeState.LOW_VOLATILITY: "Low-volatility drift detected",
        RegimeState.MIXED: "Mixed BTC/ETH or breadth context",
    }.get(state, f"{state.value} environment")
    return f"{prefix} is {band} ({confidence_score}/100). {note}"


def _state_notes(state: RegimeState) -> tuple[str, ...]:
    return {
        RegimeState.TREND_EXPANSION: ("Trend expansion favors continuation when setup gates already pass.",),
        RegimeState.TREND_PULLBACK: ("Trend pullback is acceptable but needs clean execution follow-through.",),
        RegimeState.RANGE_COMPRESSION: ("Compressed volatility can reduce follow-through until expansion returns.",),
        RegimeState.HIGH_VOLATILITY: ("High volatility increases execution risk and reduces allowed risk.",),
        RegimeState.LOW_VOLATILITY: ("Low volatility can starve RR expansion and scalp follow-through.",),
        RegimeState.CHOP: ("Choppy conditions increase sweep failure and false-positive risk.",),
        RegimeState.RISK_OFF: ("Risk-off context requires defensive sizing and cleaner confirmation.",),
        RegimeState.RISK_ON: ("Risk-on context supports participation when structure confirms.",),
        RegimeState.MIXED: ("Mixed context remains cautious/neutral.",),
        RegimeState.TRANSITION: ("Transition context needs confirmation before high-confidence classification.",),
    }.get(state, ())


def _evidence_notes(evidence: Mapping[str, Any]) -> tuple[str, ...]:
    notes: list[str] = []
    if evidence.get("directional_alignment") == "mixed":
        notes.append("BTC/ETH directional conflict")
    if evidence.get("volatility_profile") == "compressed":
        notes.append("compressed volatility")
    if evidence.get("volatility_profile") == "unstable":
        notes.append("unstable volatility")
    if _decimal(evidence.get("failed_confirmation_pct")) >= Decimal("45"):
        notes.append("weak follow-through")
    if _decimal(evidence.get("average_rr")) != Decimal("0") and _decimal(evidence.get("average_rr")) < Decimal("2.5"):
        notes.append("low-quality RR expansion")
    if _decimal(evidence.get("rejection_clustering_pct")) >= Decimal("45"):
        notes.append("excessive rejection clustering")
    return tuple(notes)


def _compatibility_notes(
    mode: str,
    state: RegimeState,
    evidence: Mapping[str, Any],
    volatility: int,
    trend: int,
    execution: int,
) -> tuple[str, ...]:
    notes: list[str] = []
    if volatility <= 45:
        notes.append(f"{mode} volatility suitability is weak")
    if trend <= 45:
        notes.append(f"{mode} trend suitability is weak")
    if execution <= 45:
        notes.append(f"{mode} execution quality suitability is weak")
    if state == RegimeState.CHOP and mode == "challenge":
        notes.append("challenge mode is least compatible with chop")
    if state == RegimeState.RANGE_COMPRESSION and mode == "scalp":
        notes.append("range compression reduces scalp follow-through")
    if state == RegimeState.TREND_EXPANSION and mode == "swing":
        notes.append("trend expansion favors swing continuation")
    if evidence.get("directional_alignment") == "mixed":
        notes.append("directional inputs are mixed")
    return _unique(notes)


def _decimal(value: Any) -> Decimal:
    if value in (None, "", NA):
        return Decimal("0")
    try:
        return Decimal(str(value))
    except Exception:
        return Decimal("0")


def _bounded_int(value: Decimal) -> int:
    return int(max(Decimal("0"), min(Decimal("100"), value)).to_integral_value(rounding="ROUND_HALF_UP"))


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        text = str(value).strip()
        if text and text != NA and text not in output:
            output.append(text)
    return tuple(output)
