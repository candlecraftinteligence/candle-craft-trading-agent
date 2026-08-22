from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.core.trade_plan_integrity import validate_trade_plan
from app.data.dtos import NA, MaybeDecimal

Direction = Literal["long", "short"]
OpportunityDecision = Literal["watchlist_only", "alert_candidate", "high_quality_candidate", "reject"]
TradeIdeaStatus = Literal["conditional", "active", "rejected"]

OUTPUT_QUANT = Decimal("0.00000001")
MIN_OPPORTUNITY_SCORE = Decimal("80")
MIN_RISK_REWARD_RATIO = Decimal("2.0")
BASE_RISK_WARNING = (
    "This is not financial advice. Position size must be based on stop-loss risk, not desired profit."
)
LEVERAGE_RISK_WARNING = (
    "Leverage increases liquidation risk. Exact liquidation price requires exchange-specific margin model and "
    "position settings."
)


class TradeIdeaInput(BaseModel):
    symbol: str
    exchange: str | None = None
    market_type: str | None = None
    direction: Direction
    timeframe: str
    setup_type: str
    entry_low: Decimal | None = None
    entry_high: Decimal | None = None
    entry_reference: Decimal | None = None
    stop_loss: Decimal | None = None
    take_profit_targets: tuple[Decimal, ...] = ()
    invalidation: str | None = None
    opportunity_score: Decimal
    opportunity_grade: str
    opportunity_decision: OpportunityDecision
    risk_approved: bool
    best_rr: Decimal
    technical_summary: str | None = None
    derivatives_summary: str | None = None
    confirmed_facts: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()
    cancel_condition: str | None = None
    leverage: Decimal | None = None
    entry_triggered: bool = False

    model_config = ConfigDict(frozen=True)

    @field_validator("opportunity_score", "best_rr")
    @classmethod
    def _decimal_is_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("decimal value must be finite")
        return value

    @field_validator("entry_low", "entry_high", "entry_reference", "stop_loss", "leverage")
    @classmethod
    def _optional_decimal_is_finite(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("decimal value must be finite")
        return value

    @field_validator("take_profit_targets", mode="before")
    @classmethod
    def _normalize_targets(cls, value: Any) -> Any:
        if value is None:
            return ()
        return value

    @field_validator("confirmed_facts", "missing_data", "unverified_data", mode="before")
    @classmethod
    def _normalize_string_tuple(cls, value: Any) -> Any:
        if value is None:
            return ()
        if isinstance(value, str):
            return (value,)
        return value


class TradeIdeaLevel(BaseModel):
    label: str
    price: MaybeDecimal = NA
    low: MaybeDecimal = NA
    high: MaybeDecimal = NA

    model_config = ConfigDict(frozen=True)


class TakeProfitLevel(BaseModel):
    target_number: int
    price: Decimal

    model_config = ConfigDict(frozen=True)


class TradeIdeaQualityGateViolation(BaseModel):
    code: str
    message: str
    severity: Literal["hard_rejection"] = "hard_rejection"

    model_config = ConfigDict(frozen=True)


class TradeIdeaQualityGate(BaseModel):
    passed: bool
    violations: tuple[TradeIdeaQualityGateViolation, ...] = ()

    model_config = ConfigDict(frozen=True)


class TradeIdeaContext(BaseModel):
    technical_summary: str = NA
    derivatives_summary: str = NA

    model_config = ConfigDict(frozen=True)


class TradeIdeaResult(BaseModel):
    symbol: str
    exchange: str
    market_type: str
    direction: Direction
    timeframe: str
    setup_type: str
    status: TradeIdeaStatus
    entry_zone: TradeIdeaLevel
    stop_loss: TradeIdeaLevel
    invalidation: str
    take_profits: tuple[TakeProfitLevel, ...]
    best_rr: Decimal
    confidence_score: Decimal
    grade: str
    reason_for_trade: str
    confirmed_facts: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()
    cancel_condition: str
    risk_warning: str
    quality_gate_result: TradeIdeaQualityGate
    context: TradeIdeaContext

    model_config = ConfigDict(frozen=True)


class TradeIdeaAgent:
    """Build structured trade idea objects from already-scored candidates only.

    The agent does not call exchanges, use private API access, send alerts, place
    orders, or execute trades. It rejects weak or incomplete setup data through
    deterministic hard gates before returning a structured idea.
    """

    def create(
        self,
        setup: TradeIdeaInput | Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> TradeIdeaResult:
        trade_input = _normalize_input(setup, overrides)
        quality_gate = _quality_gate_result(trade_input)
        context = TradeIdeaContext(
            technical_summary=_summary_or_na(trade_input.technical_summary),
            derivatives_summary=_summary_or_na(trade_input.derivatives_summary),
        )
        status = _status(trade_input, quality_gate)

        return TradeIdeaResult(
            symbol=trade_input.symbol,
            exchange=_optional_text(trade_input.exchange),
            market_type=_optional_text(trade_input.market_type),
            direction=trade_input.direction,
            timeframe=trade_input.timeframe,
            setup_type=trade_input.setup_type,
            status=status,
            entry_zone=_entry_zone(trade_input),
            stop_loss=_stop_loss_level(trade_input),
            invalidation=_optional_text(trade_input.invalidation),
            take_profits=_take_profits(trade_input.take_profit_targets),
            best_rr=_quantize(trade_input.best_rr),
            confidence_score=_quantize(trade_input.opportunity_score),
            grade=trade_input.opportunity_grade,
            reason_for_trade=_reason_for_trade(context),
            confirmed_facts=_clean_strings(trade_input.confirmed_facts),
            missing_data=_missing_data(trade_input),
            unverified_data=_clean_strings(trade_input.unverified_data),
            cancel_condition=_optional_text(trade_input.cancel_condition),
            risk_warning=_risk_warning(trade_input.leverage),
            quality_gate_result=quality_gate,
            context=context,
        )

    def analyze(
        self,
        setup: TradeIdeaInput | Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> TradeIdeaResult:
        return self.create(setup, **overrides)


def create_trade_idea(
    setup: TradeIdeaInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> TradeIdeaResult:
    return TradeIdeaAgent().create(setup, **overrides)


def _normalize_input(
    setup: TradeIdeaInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> TradeIdeaInput:
    if setup is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(setup, TradeIdeaInput):
        raw = setup.model_dump()
        raw.update(overrides)
    else:
        raw = dict(setup)
        raw.update(overrides)
    return TradeIdeaInput.model_validate(raw)


def _quality_gate_result(trade_input: TradeIdeaInput) -> TradeIdeaQualityGate:
    violations: list[TradeIdeaQualityGateViolation] = []

    if trade_input.opportunity_decision == "reject":
        violations.append(
            TradeIdeaQualityGateViolation(
                code="opportunity_rejected",
                message="Opportunity scoring rejected the candidate.",
            )
        )
    if trade_input.opportunity_score < MIN_OPPORTUNITY_SCORE:
        violations.append(
            TradeIdeaQualityGateViolation(
                code="score_below_minimum",
                message="Opportunity score is below 80.",
            )
        )
    if not trade_input.risk_approved:
        violations.append(
            TradeIdeaQualityGateViolation(
                code="risk_not_approved",
                message="Risk manager did not approve the setup.",
            )
        )
    if _optional_text(trade_input.invalidation) == NA:
        violations.append(
            TradeIdeaQualityGateViolation(
                code="missing_invalidation",
                message="A clear invalidation condition is required.",
            )
        )
    if trade_input.stop_loss is None:
        violations.append(
            TradeIdeaQualityGateViolation(
                code="missing_stop_loss",
                message="Stop loss is required.",
            )
        )
    if trade_input.entry_low is None or trade_input.entry_high is None:
        violations.append(
            TradeIdeaQualityGateViolation(
                code="missing_entry_zone",
                message="Entry zone low and high are required.",
            )
        )
    elif trade_input.entry_low > trade_input.entry_high:
        violations.append(
            TradeIdeaQualityGateViolation(
                code="invalid_entry_zone",
                message="Entry zone low must not be above entry zone high.",
            )
        )
    if not trade_input.take_profit_targets:
        violations.append(
            TradeIdeaQualityGateViolation(
                code="missing_take_profit_targets",
                message="At least one take profit target is required.",
            )
        )
    elif (
        trade_input.entry_low is not None
        and trade_input.entry_high is not None
        and trade_input.stop_loss is not None
    ):
        targets = trade_input.take_profit_targets
        entry_reference = trade_input.entry_reference
        if entry_reference is None:
            entry_reference = (
                trade_input.entry_low if trade_input.direction == "long" else trade_input.entry_high
            )
        plan_integrity = validate_trade_plan(
            direction=trade_input.direction,
            entry_low=trade_input.entry_low,
            entry_high=trade_input.entry_high,
            entry_reference=entry_reference,
            stop_loss=trade_input.stop_loss,
            tp1=targets[0] if len(targets) > 0 else None,
            tp2=targets[1] if len(targets) > 1 else None,
            tp3=targets[2] if len(targets) > 2 else None,
            require_all_targets=False,
            entry_reference_type="explicit_entry" if trade_input.entry_reference is not None else "favorable_zone_edge",
        )
        if not plan_integrity.valid:
            violations.append(
                TradeIdeaQualityGateViolation(
                    code="trade_plan_integrity_failed",
                    message=f"Trade plan integrity failed: {plan_integrity.reason}.",
                )
            )
    if trade_input.best_rr < MIN_RISK_REWARD_RATIO:
        violations.append(
            TradeIdeaQualityGateViolation(
                code="risk_reward_below_minimum",
                message="Best risk/reward ratio is below 2.0.",
            )
        )

    return TradeIdeaQualityGate(passed=not violations, violations=tuple(violations))


def _status(trade_input: TradeIdeaInput, quality_gate: TradeIdeaQualityGate) -> TradeIdeaStatus:
    if not quality_gate.passed:
        return "rejected"
    if trade_input.entry_triggered:
        return "active"
    return "conditional"


def _entry_zone(trade_input: TradeIdeaInput) -> TradeIdeaLevel:
    return TradeIdeaLevel(
        label="entry_zone",
        low=NA if trade_input.entry_low is None else _quantize(trade_input.entry_low),
        high=NA if trade_input.entry_high is None else _quantize(trade_input.entry_high),
    )


def _stop_loss_level(trade_input: TradeIdeaInput) -> TradeIdeaLevel:
    return TradeIdeaLevel(
        label="stop_loss",
        price=NA if trade_input.stop_loss is None else _quantize(trade_input.stop_loss),
    )


def _take_profits(targets: tuple[Decimal, ...]) -> tuple[TakeProfitLevel, ...]:
    return tuple(
        TakeProfitLevel(target_number=index, price=_quantize(target))
        for index, target in enumerate(targets, start=1)
    )


def _reason_for_trade(context: TradeIdeaContext) -> str:
    technical = _sentence_fragment(context.technical_summary)
    derivatives = _sentence_fragment(context.derivatives_summary)
    return f"Technical context: {technical} Derivatives context: {derivatives}"


def _risk_warning(leverage: Decimal | None) -> str:
    warnings = [BASE_RISK_WARNING]
    if leverage is None:
        return " ".join(warnings)

    warnings.append(LEVERAGE_RISK_WARNING)
    if leverage > Decimal("10"):
        warnings.append("High leverage risk.")
    if leverage > Decimal("25"):
        warnings.append("Extreme leverage risk.")
    if leverage > Decimal("50"):
        warnings.append("Dangerous leverage risk.")
    return " ".join(warnings)


def _missing_data(trade_input: TradeIdeaInput) -> tuple[str, ...]:
    missing = list(_clean_strings(trade_input.missing_data))
    if _summary_or_na(trade_input.technical_summary) == NA:
        missing.append("technical_summary: N/A")
    if _summary_or_na(trade_input.derivatives_summary) == NA:
        missing.append("derivatives_summary: N/A")
    if trade_input.entry_low is None or trade_input.entry_high is None:
        missing.append("entry_zone: N/A")
    if trade_input.stop_loss is None:
        missing.append("stop_loss: N/A")
    if not trade_input.take_profit_targets:
        missing.append("take_profit_targets: N/A")
    if _optional_text(trade_input.invalidation) == NA:
        missing.append("invalidation: N/A")
    return _unique(missing)


def _summary_or_na(value: str | None) -> str:
    return _optional_text(value)


def _optional_text(value: str | None) -> str:
    if value is None or value.strip() == "":
        return NA
    return value.strip()


def _sentence_fragment(value: str) -> str:
    if value.endswith((".", "!", "?")):
        return value
    return f"{value}."


def _clean_strings(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value.strip() for value in values if value.strip())


def _unique(values: list[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        if value not in output:
            output.append(value)
    return tuple(output)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


__all__ = [
    "TakeProfitLevel",
    "TradeIdeaAgent",
    "TradeIdeaContext",
    "TradeIdeaInput",
    "TradeIdeaLevel",
    "TradeIdeaQualityGate",
    "TradeIdeaQualityGateViolation",
    "TradeIdeaResult",
    "TradeIdeaStatus",
    "create_trade_idea",
]
