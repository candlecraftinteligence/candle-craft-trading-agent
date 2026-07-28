from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from app.data.dtos import NA

WATCHLIST_ONLY = "Watchlist only"
WAIT_FOR_CONFIRMATION = "Wait for confirmation"
REJECTED = "Rejected"
DATA_INSUFFICIENT = "Data insufficient"

RR_GATES = {
    "missing_rr",
    "missing_target",
    "rr_below_minimum",
    "challenge_rr_below_3",
    "rr_too_low",
}
DEEP_PULLBACK_GATES = {"pullback_too_deep", "pullback_beyond_786"}
WICK_RECLAIM_GATES = {"wick_sweep_reclaim"}
BODY_ACCEPTANCE_GATES = {"body_acceptance_failure", "structural_breakdown"}
OB_FVG_GATES = {"no_ob_or_fvg_zone", "challenge_limit_entry_missing"}
CONFIRMATION_GATES = {"missing_confirmation_structure_shift"}
EARLY_REJECTION_GATES = {"missing_confirmed_sweep"}
DATA_GATES = {
    "no_execution_candles",
    "missing_confirmation_candles",
    "not_enough_candles",
    "atr_unavailable",
}
FINAL_QUALITY_GATES = {
    "trust_meter_below_minimum",
    "challenge_trust_below_85",
}
STALE_CONTEXT_GATES = {
    "entry_window_expired",
    "missing_displacement_impulse",
    "no_displacement_candle",
    "missing_stop",
}
CONTEXT_REJECTION_GATES = {
    "challenge_illiquid_token",
    "challenge_btc_abnormal",
    "challenge_event_window",
    "btc_volatility_guard",
    "btc_d_guard",
    "event_guard",
    "derivatives_conflict",
    "funding_oi_guard",
}


class NearMissIntelligence(BaseModel):
    primary_failed_gate: str = NA
    short_reason: str = NA
    watchlist_status: str = NA
    next_required_conditions: tuple[str, ...] = (NA,)
    activation_hint: str = NA
    invalidation_hint: str = NA
    quality_note: str = NA
    action_label: str = NA

    model_config = ConfigDict(frozen=True)

    @field_validator(
        "primary_failed_gate",
        "short_reason",
        "watchlist_status",
        "activation_hint",
        "invalidation_hint",
        "quality_note",
        "action_label",
        mode="before",
    )
    @classmethod
    def _string_or_na(cls, value: Any) -> str:
        text = _display(value)
        return text if text else NA

    @field_validator("next_required_conditions", mode="before")
    @classmethod
    def _conditions_or_na(cls, value: Any) -> tuple[str, ...]:
        values = _sequence_values(value)
        return values if values else (NA,)


def build_near_miss_intelligence(
    *,
    failed_gate: Any,
    short_reason: Any = NA,
    diagnostics: Mapping[str, Any] | None = None,
) -> NearMissIntelligence | None:
    """Build a read-only action plan from existing scanner diagnostics.

    The helper never creates levels, targets, trade ideas, alerts, or execution
    instructions. It only explains what existing strategy diagnostics say is
    missing before a setup can be reconsidered.
    """

    gate = _display(failed_gate)
    if gate == NA:
        return None
    if gate in EARLY_REJECTION_GATES:
        return None

    data = diagnostics or {}
    sweep_passed = _sweep_passed(data)
    confirmation_passed = _confirmation_passed(data)
    confirmation_timeframe = _confirmation_timeframe(data)
    core_passed = sweep_passed and confirmation_passed
    reason = _reason_for_gate(gate, short_reason, data)

    if gate in RR_GATES:
        return NearMissIntelligence(
            primary_failed_gate=gate,
            short_reason=reason,
            watchlist_status=WATCHLIST_ONLY if core_passed else REJECTED,
            next_required_conditions=(
                "Better pullback entry must improve entry-to-stop distance.",
                "TP2 distance must widen without inventing a target.",
                "A cleaner opposing liquidity target must be visible before activation.",
            ),
            activation_hint="RR must improve to the required minimum before this setup can become valid.",
            invalidation_hint=_structure_invalidation_hint(core_passed),
            quality_note="Structure is close, but current reward does not compensate for risk.",
            action_label=WATCHLIST_ONLY if core_passed else REJECTED,
        )

    if gate in DEEP_PULLBACK_GATES:
        return NearMissIntelligence(
            primary_failed_gate=gate,
            short_reason=reason,
            watchlist_status=REJECTED,
            next_required_conditions=(
                "Do not use this pullback leg.",
                "A completely new liquidity sweep must form.",
                "A new BOS/CHoCH must confirm after that sweep.",
            ),
            activation_hint="This setup cannot activate from the current pullback; a new sweep and BOS/CHoCH are required.",
            invalidation_hint="Current idea is invalidated by the deep pullback beyond 0.786.",
            quality_note="Pullback tagged beyond 0.786; intent is weak.",
            action_label=REJECTED,
        )

    if gate in WICK_RECLAIM_GATES:
        return NearMissIntelligence(
            primary_failed_gate=gate,
            short_reason=reason,
            watchlist_status=WATCHLIST_ONLY if core_passed else REJECTED,
            next_required_conditions=(
                "Reclaim strength must improve after the wick sweep.",
                "BOS/CHoCH structure must remain intact.",
                "OB/FVG, RR, and final quality gates must still pass.",
            ),
            activation_hint="A wick reclaim is watch-only until reclaim quality and all standard gates pass.",
            invalidation_hint=_structure_invalidation_hint(core_passed),
            quality_note="The deep move is a wick sweep, not body acceptance, but reclaim quality is still weak.",
            action_label=WATCHLIST_ONLY if core_passed else REJECTED,
        )

    if gate in BODY_ACCEPTANCE_GATES:
        return NearMissIntelligence(
            primary_failed_gate=gate,
            short_reason=reason,
            watchlist_status=REJECTED,
            next_required_conditions=(
                "Do not confirm this pullback after body acceptance beyond 0.786.",
                "Wait for a fresh liquidity sweep.",
                "Require a new BOS/CHoCH before evaluating another pullback.",
            ),
            activation_hint="Body acceptance beyond the invalidation zone blocks activation from this structure.",
            invalidation_hint="Current idea is invalidated by body acceptance beyond 0.786 or structural breakdown.",
            quality_note="Body-close acceptance is materially weaker than a wick-only sweep.",
            action_label=REJECTED,
        )

    if gate in OB_FVG_GATES:
        if core_passed:
            return NearMissIntelligence(
                primary_failed_gate=gate,
                short_reason=reason,
                watchlist_status=WATCHLIST_ONLY,
                next_required_conditions=(
                    "A valid OB or FVG must be found inside the displacement impulse.",
                    "The OB/FVG zone must overlap the preferred fib pullback zone.",
                    "RR and final quality gates must still pass after a valid zone is found.",
                ),
                activation_hint="A valid OB/FVG zone inside the displacement impulse is required before this setup can become valid.",
                invalidation_hint=_structure_invalidation_hint(core_passed),
                quality_note="Structure progressed, but there is no actionable execution zone yet.",
                action_label=WATCHLIST_ONLY,
            )
        return NearMissIntelligence(
            primary_failed_gate=gate,
            short_reason=reason,
            watchlist_status=REJECTED,
            next_required_conditions=(
                "15m sweep must pass first.",
                f"{confirmation_timeframe} BOS/CHoCH must confirm after the sweep.",
                "Then a valid OB/FVG must appear inside the displacement impulse.",
            ),
            activation_hint="Core sweep and BOS/CHoCH gates must pass before OB/FVG quality matters.",
            invalidation_hint="No watchlist activation until the missing core structure is rebuilt.",
            quality_note="The execution zone is missing before the setup has enough confirmed structure.",
            action_label=REJECTED,
        )

    if gate in CONFIRMATION_GATES:
        return NearMissIntelligence(
            primary_failed_gate=gate,
            short_reason=reason,
            watchlist_status=WAIT_FOR_CONFIRMATION,
            next_required_conditions=(
                f"Wait for a {confirmation_timeframe} BOS/CHoCH close beyond the required LTF swing.",
                "Keep the 15m sweep context intact before confirmation.",
                "Only reassess pullback, OB/FVG, fib, RR, and risk after confirmation.",
            ),
            activation_hint=f"A {confirmation_timeframe} BOS/CHoCH confirmation must print before this can become interesting.",
            invalidation_hint="Invalidated if price reverses through the sweep context before confirmation or the setup times out.",
            quality_note="The idea is unconfirmed; no pullback or RR plan is valid yet.",
            action_label=WAIT_FOR_CONFIRMATION,
        )

    if gate in FINAL_QUALITY_GATES:
        status = WATCHLIST_ONLY if core_passed else REJECTED
        return NearMissIntelligence(
            primary_failed_gate=gate,
            short_reason=reason,
            watchlist_status=status,
            next_required_conditions=(
                "Trust Meter or final confluence must reach the required threshold.",
                "Existing sweep, BOS/CHoCH, pullback, and RR gates must remain valid.",
                "No new hard rejection may appear before reassessment.",
            ),
            activation_hint="Final quality must improve to the required threshold before a valid setup can be created.",
            invalidation_hint=_structure_invalidation_hint(core_passed),
            quality_note="The structure is close, but final confluence is not strong enough.",
            action_label=status,
        )

    if gate in STALE_CONTEXT_GATES:
        return NearMissIntelligence(
            primary_failed_gate=gate,
            short_reason=reason,
            watchlist_status=REJECTED,
            next_required_conditions=(
                "Do not activate this stale or incomplete leg.",
                "Wait for a fresh liquidity sweep.",
                "Require a new BOS/CHoCH and pullback map after the fresh sweep.",
            ),
            activation_hint="A fresh structure sequence is required before this can become valid.",
            invalidation_hint="Current context is invalid for activation.",
            quality_note="The setup context is stale or incomplete.",
            action_label=REJECTED,
        )

    if gate in CONTEXT_REJECTION_GATES:
        return NearMissIntelligence(
            primary_failed_gate=gate,
            short_reason=reason,
            watchlist_status=REJECTED,
            next_required_conditions=(
                "Hard context rejection must clear.",
                "Strategy structure must remain valid after context clears.",
                "Risk and final quality gates must be reassessed from current data.",
            ),
            activation_hint="Context rejection must clear before any setup can be reconsidered.",
            invalidation_hint="Do not activate while the hard context rejection remains active.",
            quality_note="Context is not supportive enough for a valid setup.",
            action_label=REJECTED,
        )

    if gate in DATA_GATES:
        return NearMissIntelligence(
            primary_failed_gate=gate,
            short_reason=reason,
            watchlist_status=DATA_INSUFFICIENT,
            next_required_conditions=(
                "Required public market data must become available.",
                "Scanner diagnostics must no longer mark required fields as N/A.",
                "Strategy gates must be evaluated again with complete data.",
            ),
            activation_hint="Data must be available before setup validity can be evaluated.",
            invalidation_hint="N/A",
            quality_note="The scanner cannot evaluate setup quality from missing data.",
            action_label=DATA_INSUFFICIENT,
        )

    return NearMissIntelligence(
        primary_failed_gate=gate,
        short_reason=reason,
        watchlist_status=REJECTED,
        next_required_conditions=(
            "Failed gate must clear on fresh scanner data.",
            "All prior strategy gates must remain valid.",
            "Risk and trade-idea quality gates must still pass before any valid setup exists.",
        ),
        activation_hint="The failed gate must clear before the setup can become valid.",
        invalidation_hint=_structure_invalidation_hint(core_passed),
        quality_note="Current diagnostics do not justify a valid setup.",
        action_label=REJECTED,
    )


def _reason_for_gate(gate: str, short_reason: Any, diagnostics: Mapping[str, Any]) -> str:
    if gate in RR_GATES:
        rr_diagnostic = _clean_diagnostic_sentence(diagnostics.get("rr_diagnostics"))
        if rr_diagnostic != NA and "rr" in rr_diagnostic.lower():
            return rr_diagnostic
        reason = _display(short_reason)
        if reason != NA and "rr" in reason.lower():
            return reason
        return "RR to TP2 is below the required minimum."

    if gate in DEEP_PULLBACK_GATES:
        reason = _first_text(
            diagnostics.get("pullback_failure_reason"),
            diagnostics.get("fib_diagnostics"),
            short_reason,
        )
        if reason != NA:
            return reason
        return "Pullback tagged beyond 0.786 and intent is weak."

    if gate in OB_FVG_GATES:
        reason = _first_text(
            diagnostics.get("pullback_failure_reason"),
            diagnostics.get("ob_fvg_diagnostics"),
            short_reason,
        )
        if reason != NA:
            return _clean_diagnostic_sentence(reason)
        return "No valid OB/FVG was found inside the displacement impulse."

    if gate in CONFIRMATION_GATES:
        reason = _first_text(diagnostics.get("confirmation_bos_choch_reason"), short_reason)
        if reason != NA:
            return reason
        return f"{_confirmation_timeframe(diagnostics)} BOS/CHoCH confirmation is still missing."

    if gate in FINAL_QUALITY_GATES:
        reason = _first_text(diagnostics.get("trust_meter_diagnostics"), short_reason)
        if reason != NA:
            return _clean_diagnostic_sentence(reason)
        return "Final quality or Trust Meter is below the required threshold."

    reason = _display(short_reason)
    if reason != NA:
        return reason

    hard_rejections = _sequence_values(diagnostics.get("hard_rejection_reasons"))
    if hard_rejections:
        return hard_rejections[0]
    return "No valid setup."


def _confirmation_timeframe(diagnostics: Mapping[str, Any]) -> str:
    timeframe = _display(diagnostics.get("confirmation_timeframe"))
    return timeframe if timeframe != NA else "15m"


def _sweep_passed(diagnostics: Mapping[str, Any]) -> bool:
    return _display(diagnostics.get("execution_sweep_status")) == "passed" or "sweep" in _sequence_values(
        diagnostics.get("gates_passed")
    )


def _confirmation_passed(diagnostics: Mapping[str, Any]) -> bool:
    return _display(diagnostics.get("confirmation_structure_shift_status")) == "passed" or "bos_choch" in _sequence_values(
        diagnostics.get("gates_passed")
    )


def _structure_invalidation_hint(core_passed: bool) -> str:
    if core_passed:
        return "Invalidated if the sweep/BOS/CHoCH context fails, expires, or price invalidates the strategy structure."
    return "Invalidated until the missing sweep and BOS/CHoCH structure is rebuilt."


def _first_text(*values: Any) -> str:
    for value in values:
        text = _clean_diagnostic_sentence(value)
        if text != NA:
            return text
    return NA


def _clean_diagnostic_sentence(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    return text.removeprefix("failed: ").removeprefix("passed: ")


def _sequence_values(values: Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    output: list[str] = []
    for value in values:
        text = _display(value)
        if text != NA:
            output.append(text)
    return tuple(output)


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


__all__ = [
    "DATA_INSUFFICIENT",
    "REJECTED",
    "WAIT_FOR_CONFIRMATION",
    "WATCHLIST_ONLY",
    "NearMissIntelligence",
    "build_near_miss_intelligence",
]
