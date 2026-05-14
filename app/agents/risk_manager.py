from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.data.dtos import NA, MaybeDecimal

Direction = Literal["long", "short"]
DecisionStatus = Literal["approved", "rejected"]
LeverageRiskLevel = Literal["standard", "high", "extreme", "dangerous", "N/A"]
DataReliability = Literal["Verified", "Unverified", "N/A"]

OUTPUT_QUANT = Decimal("0.00000001")
MAX_RISK_PER_TRADE_PCT = Decimal("2")
MIN_RISK_REWARD_RATIO = Decimal("2.0")
MIN_DATA_QUALITY_SCORE = Decimal("60")
POSITION_SIZING_MESSAGE = "Position size should be based on stop-loss risk, not desired profit."
LIQUIDATION_MESSAGE = "Exact liquidation price requires exchange-specific margin model and position settings."
RISK_WARNING = (
    "Risk validation only. This does not place trades, and any approved setup can still fail at invalidation."
)


class RiskManagerInput(BaseModel):
    account_equity: Decimal
    risk_per_trade_pct: Decimal
    entry_price: Decimal
    stop_loss: Decimal
    take_profit_targets: tuple[Decimal, ...]
    direction: Direction
    leverage: Decimal | None = None
    max_daily_risk_pct: Decimal | None = None
    current_daily_loss_pct: Decimal | None = None
    data_quality_score: Decimal | None = None
    invalidation_reason: str | None = None

    model_config = ConfigDict(frozen=True)


class RiskRuleViolation(BaseModel):
    code: str
    message: str
    severity: Literal["hard_rejection"] = "hard_rejection"

    model_config = ConfigDict(frozen=True)


class PositionSizingResult(BaseModel):
    risk_amount: MaybeDecimal = NA
    risk_per_unit: MaybeDecimal = NA
    position_size: MaybeDecimal = NA
    notional_value: MaybeDecimal = NA
    message: str = POSITION_SIZING_MESSAGE

    model_config = ConfigDict(frozen=True)


class RiskRewardResult(BaseModel):
    take_profit: MaybeDecimal = NA
    reward: MaybeDecimal = NA
    risk_reward_ratio: MaybeDecimal = NA
    is_valid: bool = False
    reason: str = "Risk/reward is N/A because required price or stop data is missing."

    model_config = ConfigDict(frozen=True)


class LeverageRiskResult(BaseModel):
    leverage: MaybeDecimal = NA
    risk_level: LeverageRiskLevel = NA
    warning: str = (
        "Leverage is N/A because no leverage value was provided. "
        f"{POSITION_SIZING_MESSAGE}"
    )
    liquidation_distance: MaybeDecimal = NA
    liquidation_message: str = LIQUIDATION_MESSAGE
    position_sizing_message: str = POSITION_SIZING_MESSAGE

    model_config = ConfigDict(frozen=True)


class RiskDecision(BaseModel):
    approved: bool
    decision: DecisionStatus
    violations: tuple[RiskRuleViolation, ...] = ()
    position_sizing: PositionSizingResult = PositionSizingResult()
    risk_reward: tuple[RiskRewardResult, ...] = ()
    best_risk_reward_ratio: MaybeDecimal = NA
    leverage_risk: LeverageRiskResult = LeverageRiskResult()
    invalidation_reason: str = NA
    data_quality_score: MaybeDecimal = NA
    data_reliability: DataReliability = NA
    max_daily_risk_pct: MaybeDecimal = NA
    current_daily_loss_pct: MaybeDecimal = NA
    risk_warning: str = RISK_WARNING

    model_config = ConfigDict(frozen=True)


class RiskManagerAgent:
    """Deterministic risk validation for proposed trade setups.

    The agent calculates stop-loss-based sizing, risk/reward, leverage warnings,
    and hard risk rejections. It does not call exchanges, use private API data,
    calculate exchange-specific liquidation, place orders, or produce execution
    instructions.
    """

    def analyze(self, setup: RiskManagerInput | Mapping[str, Any] | None = None, **overrides: Any) -> RiskDecision:
        risk_input = _normalize_input(setup, overrides)
        violations = _hard_rule_violations(risk_input)
        position_sizing = _calculate_position_sizing(risk_input)
        risk_reward = _calculate_risk_reward(risk_input, position_sizing.risk_per_unit)
        best_rr = _best_risk_reward(risk_reward)

        if best_rr != NA and best_rr < MIN_RISK_REWARD_RATIO:
            violations.append(
                RiskRuleViolation(
                    code="risk_reward_below_minimum",
                    message="Best risk/reward ratio is below 2.0.",
                )
            )

        leverage_risk = _classify_leverage(risk_input.leverage)
        approved = not violations

        return RiskDecision(
            approved=approved,
            decision="approved" if approved else "rejected",
            violations=tuple(violations),
            position_sizing=position_sizing,
            risk_reward=risk_reward,
            best_risk_reward_ratio=best_rr,
            leverage_risk=leverage_risk,
            invalidation_reason=_normalized_invalidation(risk_input),
            data_quality_score=_optional_quantize(risk_input.data_quality_score),
            data_reliability=_data_reliability(risk_input.data_quality_score),
            max_daily_risk_pct=_optional_quantize(risk_input.max_daily_risk_pct),
            current_daily_loss_pct=_optional_quantize(risk_input.current_daily_loss_pct),
        )


def _normalize_input(setup: RiskManagerInput | Mapping[str, Any] | None, overrides: Mapping[str, Any]) -> RiskManagerInput:
    if setup is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(setup, RiskManagerInput):
        raw = setup.model_dump()
        raw.update(overrides)
    else:
        raw = dict(setup)
        raw.update(overrides)
    return RiskManagerInput.model_validate(raw)


def _hard_rule_violations(setup: RiskManagerInput) -> list[RiskRuleViolation]:
    violations: list[RiskRuleViolation] = []

    if setup.account_equity <= 0:
        violations.append(
            RiskRuleViolation(
                code="invalid_account_equity",
                message="Account equity must be greater than zero.",
            )
        )
    if setup.risk_per_trade_pct <= 0:
        violations.append(
            RiskRuleViolation(
                code="invalid_risk_per_trade",
                message="Risk per trade percentage must be greater than zero.",
            )
        )
    if setup.risk_per_trade_pct > MAX_RISK_PER_TRADE_PCT:
        violations.append(
            RiskRuleViolation(
                code="risk_per_trade_too_high",
                message="Risk per trade percentage must not exceed 2%.",
            )
        )
    if setup.entry_price <= 0:
        violations.append(
            RiskRuleViolation(
                code="invalid_entry_price",
                message="Entry price must be greater than zero.",
            )
        )
    if setup.stop_loss <= 0:
        violations.append(
            RiskRuleViolation(
                code="invalid_stop_loss",
                message="Stop loss must be greater than zero.",
            )
        )
    if not setup.take_profit_targets:
        violations.append(
            RiskRuleViolation(
                code="missing_take_profit_targets",
                message="At least one take profit target is required.",
            )
        )
    if _normalized_invalidation(setup) == NA:
        violations.append(
            RiskRuleViolation(
                code="missing_invalidation",
                message="A clear invalidation reason is required.",
            )
        )
    if setup.entry_price > 0 and setup.stop_loss > 0:
        if setup.direction == "long" and setup.stop_loss >= setup.entry_price:
            violations.append(
                RiskRuleViolation(
                    code="wrong_side_stop_loss",
                    message="Long setup stop loss must be below entry.",
                )
            )
        if setup.direction == "short" and setup.stop_loss <= setup.entry_price:
            violations.append(
                RiskRuleViolation(
                    code="wrong_side_stop_loss",
                    message="Short setup stop loss must be above entry.",
                )
            )
    if setup.data_quality_score is not None and setup.data_quality_score < MIN_DATA_QUALITY_SCORE:
        violations.append(
            RiskRuleViolation(
                code="low_data_quality",
                message="Data quality score is below 60 and should be treated as Unverified.",
            )
        )
    if (
        setup.max_daily_risk_pct is not None
        and setup.current_daily_loss_pct is not None
        and setup.current_daily_loss_pct > setup.max_daily_risk_pct
    ):
        violations.append(
            RiskRuleViolation(
                code="daily_risk_exceeded",
                message="Current daily loss percentage exceeds max daily risk percentage.",
            )
        )

    return violations


def _calculate_position_sizing(setup: RiskManagerInput) -> PositionSizingResult:
    risk_amount: MaybeDecimal = NA
    risk_per_unit = _risk_per_unit(setup)
    position_size: MaybeDecimal = NA
    notional_value: MaybeDecimal = NA

    if setup.account_equity > 0 and setup.risk_per_trade_pct > 0:
        risk_amount = _quantize(setup.account_equity * setup.risk_per_trade_pct / Decimal("100"))

    if risk_amount != NA and risk_per_unit != NA and risk_per_unit > 0:
        raw_position_size = risk_amount / risk_per_unit
        position_size = _quantize(raw_position_size)
        notional_value = _quantize(raw_position_size * setup.entry_price)

    return PositionSizingResult(
        risk_amount=risk_amount,
        risk_per_unit=_quantize(risk_per_unit) if risk_per_unit != NA else NA,
        position_size=position_size,
        notional_value=notional_value,
    )


def _calculate_risk_reward(setup: RiskManagerInput, risk_per_unit: MaybeDecimal) -> tuple[RiskRewardResult, ...]:
    results: list[RiskRewardResult] = []
    for target in setup.take_profit_targets:
        if risk_per_unit == NA or risk_per_unit <= 0:
            results.append(
                RiskRewardResult(
                    take_profit=_quantize(target),
                    reason="Risk/reward is N/A because stop loss is not on the valid side of entry.",
                )
            )
            continue

        reward = target - setup.entry_price if setup.direction == "long" else setup.entry_price - target
        ratio = reward / risk_per_unit
        quantized_ratio = _quantize(ratio)
        results.append(
            RiskRewardResult(
                take_profit=_quantize(target),
                reward=_quantize(reward),
                risk_reward_ratio=quantized_ratio,
                is_valid=ratio >= MIN_RISK_REWARD_RATIO,
                reason="Risk/reward was calculated from target reward divided by stop-loss risk per unit.",
            )
        )
    return tuple(results)


def _best_risk_reward(results: tuple[RiskRewardResult, ...]) -> MaybeDecimal:
    ratios = [result.risk_reward_ratio for result in results if result.risk_reward_ratio != NA]
    if not ratios:
        return NA
    return max(ratios)


def _classify_leverage(leverage: Decimal | None) -> LeverageRiskResult:
    if leverage is None:
        return LeverageRiskResult()

    quantized_leverage = _quantize(leverage)
    if leverage > Decimal("50"):
        risk_level: LeverageRiskLevel = "dangerous"
        level_warning = "Dangerous leverage risk: small price moves can cause severe losses or liquidation."
    elif leverage > Decimal("25"):
        risk_level = "extreme"
        level_warning = "Extreme leverage risk: liquidation risk and loss acceleration are very high."
    elif leverage > Decimal("10"):
        risk_level = "high"
        level_warning = "High leverage risk: losses and liquidation risk are amplified."
    else:
        risk_level = "standard"
        level_warning = "Leverage increases losses and liquidation risk even at lower levels."

    return LeverageRiskResult(
        leverage=quantized_leverage,
        risk_level=risk_level,
        warning=f"{level_warning} {POSITION_SIZING_MESSAGE}",
    )


def _risk_per_unit(setup: RiskManagerInput) -> MaybeDecimal:
    if setup.entry_price <= 0 or setup.stop_loss <= 0:
        return NA
    if setup.direction == "long":
        risk_per_unit = setup.entry_price - setup.stop_loss
    else:
        risk_per_unit = setup.stop_loss - setup.entry_price
    if risk_per_unit <= 0:
        return NA
    return risk_per_unit


def _normalized_invalidation(setup: RiskManagerInput) -> str:
    if setup.invalidation_reason is None or setup.invalidation_reason.strip() == "":
        return NA
    return setup.invalidation_reason.strip()


def _data_reliability(score: Decimal | None) -> DataReliability:
    if score is None:
        return NA
    if score < MIN_DATA_QUALITY_SCORE:
        return "Unverified"
    return "Verified"


def _optional_quantize(value: Decimal | None) -> MaybeDecimal:
    if value is None:
        return NA
    return _quantize(value)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


__all__ = [
    "LeverageRiskResult",
    "PositionSizingResult",
    "RiskDecision",
    "RiskManagerAgent",
    "RiskManagerInput",
    "RiskRewardResult",
    "RiskRuleViolation",
]
