from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal


TradeDirection = Literal["long", "short"]


class TradePlanFailure(str, Enum):
    UNSUPPORTED_DIRECTION = "unsupported_direction"
    MISSING_COMPONENT = "missing_required_plan_component"
    MALFORMED_NUMERIC = "malformed_numeric"
    NON_FINITE_NUMERIC = "non_finite_numeric"
    NON_POSITIVE_PRICE = "non_positive_price"
    ENTRY_ZONE_INVALID = "entry_zone_invalid"
    ENTRY_REFERENCE_OUTSIDE_ZONE = "entry_reference_outside_zone"
    STOP_INSIDE_ENTRY_ZONE = "stop_inside_entry_zone"
    STOP_WRONG_SIDE = "stop_wrong_side"
    TARGET_INSIDE_ENTRY_ZONE = "target_inside_entry_zone"
    TARGET_WRONG_SIDE = "target_wrong_side"
    DUPLICATE_TARGETS = "duplicate_targets"
    TARGET_ORDER_INVALID = "target_order_invalid"
    ZERO_RISK = "zero_risk"
    NEGATIVE_RISK = "negative_risk"
    ZERO_REWARD = "zero_reward"
    NEGATIVE_REWARD = "negative_reward"
    RR_BELOW_MINIMUM = "rr_below_minimum"


@dataclass(frozen=True)
class TradePlanIntegrityResult:
    valid: bool
    reason: str = "N/A"
    direction: TradeDirection | None = None
    entry_low: Decimal | None = None
    entry_high: Decimal | None = None
    entry_reference: Decimal | None = None
    stop_loss: Decimal | None = None
    tp1: Decimal | None = None
    tp2: Decimal | None = None
    tp3: Decimal | None = None
    risk_distance: Decimal | None = None
    reward_distance: Decimal | None = None
    rr: Decimal | None = None
    rr_target: str = "tp2"
    entry_reference_type: str = "N/A"


class _PlanInputError(ValueError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def validate_trade_plan(
    *,
    direction: Any,
    entry_low: Any,
    entry_high: Any,
    entry_reference: Any,
    stop_loss: Any,
    tp1: Any = None,
    tp2: Any = None,
    tp3: Any = None,
    minimum_rr: Any = None,
    require_all_targets: bool = True,
    entry_reference_type: str = "N/A",
) -> TradePlanIntegrityResult:
    side = str(direction).strip().lower()
    if side not in {"long", "short"}:
        return _failed(TradePlanFailure.UNSUPPORTED_DIRECTION)

    try:
        low = _required_decimal(entry_low, "entry_low")
        high = _required_decimal(entry_high, "entry_high")
        entry = _required_decimal(entry_reference, "entry_reference")
        stop = _required_decimal(stop_loss, "stop_loss")
        target_values = (
            _optional_decimal(tp1, "tp1", required=require_all_targets),
            _optional_decimal(tp2, "tp2", required=require_all_targets),
            _optional_decimal(tp3, "tp3", required=require_all_targets),
        )
        minimum = _optional_decimal(minimum_rr, "minimum_rr", required=False)
    except _PlanInputError as exc:
        return _failed(exc.reason)

    named_prices = {
        "entry_low": low,
        "entry_high": high,
        "entry_reference": entry,
        "stop_loss": stop,
        **{
            f"tp{index}": target
            for index, target in enumerate(target_values, start=1)
            if target is not None
        },
    }
    for name, price in named_prices.items():
        if price <= 0:
            return _failed(f"{TradePlanFailure.NON_POSITIVE_PRICE.value}:{name}")

    if low > high:
        return _failed(TradePlanFailure.ENTRY_ZONE_INVALID)
    if not low <= entry <= high:
        return _failed(TradePlanFailure.ENTRY_REFERENCE_OUTSIDE_ZONE)

    signed_risk = entry - stop if side == "long" else stop - entry
    if signed_risk == 0:
        return _failed(TradePlanFailure.ZERO_RISK)

    if side == "long":
        if low <= stop <= high:
            return _failed(TradePlanFailure.STOP_INSIDE_ENTRY_ZONE)
        if stop >= high:
            return _failed(TradePlanFailure.STOP_WRONG_SIDE)
        risk = entry - stop
    else:
        if low <= stop <= high:
            return _failed(TradePlanFailure.STOP_INSIDE_ENTRY_ZONE)
        if stop <= low:
            return _failed(TradePlanFailure.STOP_WRONG_SIDE)
        risk = stop - entry

    if risk == 0:
        return _failed(TradePlanFailure.ZERO_RISK)
    if risk < 0:
        return _failed(TradePlanFailure.NEGATIVE_RISK)

    rr_target = target_values[1]
    reward: Decimal | None = None
    if rr_target is not None:
        reward = rr_target - entry if side == "long" else entry - rr_target
        if reward == 0:
            return _failed(TradePlanFailure.ZERO_REWARD)
        if reward < 0:
            return _failed(TradePlanFailure.NEGATIVE_REWARD)

    present_targets = tuple(
        (index, target)
        for index, target in enumerate(target_values, start=1)
        if target is not None
    )
    for index, target in present_targets:
        if low <= target <= high:
            return _failed(f"tp{index}_{TradePlanFailure.TARGET_INSIDE_ENTRY_ZONE.value}")
        if side == "long" and target < low:
            return _failed(f"tp{index}_{TradePlanFailure.TARGET_WRONG_SIDE.value}")
        if side == "short" and target > high:
            return _failed(f"tp{index}_{TradePlanFailure.TARGET_WRONG_SIDE.value}")

    targets = tuple(target for _index, target in present_targets)
    if len(set(targets)) != len(targets):
        return _failed(TradePlanFailure.DUPLICATE_TARGETS)
    if len(targets) > 1:
        ordered = all(
            current < following if side == "long" else current > following
            for current, following in zip(targets, targets[1:], strict=False)
        )
        if not ordered:
            return _failed(TradePlanFailure.TARGET_ORDER_INVALID)

    rr: Decimal | None = None
    if reward is not None:
        rr = reward / risk
        if minimum is not None and rr < minimum:
            return _result(
                valid=False,
                reason=TradePlanFailure.RR_BELOW_MINIMUM.value,
                side=side,
                low=low,
                high=high,
                entry=entry,
                stop=stop,
                targets=target_values,
                risk=risk,
                reward=reward,
                rr=rr,
                entry_reference_type=entry_reference_type,
            )
    elif minimum is not None:
        return _failed(f"{TradePlanFailure.MISSING_COMPONENT.value}:tp2")

    return _result(
        valid=True,
        reason="N/A",
        side=side,
        low=low,
        high=high,
        entry=entry,
        stop=stop,
        targets=target_values,
        risk=risk,
        reward=reward,
        rr=rr,
        entry_reference_type=entry_reference_type,
    )


def _required_decimal(value: Any, name: str) -> Decimal:
    parsed = _optional_decimal(value, name, required=True)
    assert parsed is not None
    return parsed


def _optional_decimal(value: Any, name: str, *, required: bool) -> Decimal | None:
    if value is None or str(value).strip() in {"", "N/A"}:
        if required:
            raise _PlanInputError(f"{TradePlanFailure.MISSING_COMPONENT.value}:{name}")
        return None
    try:
        parsed = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise _PlanInputError(f"{TradePlanFailure.MALFORMED_NUMERIC.value}:{name}") from None
    if not parsed.is_finite():
        raise _PlanInputError(f"{TradePlanFailure.NON_FINITE_NUMERIC.value}:{name}")
    return parsed


def _failed(reason: TradePlanFailure | str) -> TradePlanIntegrityResult:
    return TradePlanIntegrityResult(
        valid=False,
        reason=reason.value if isinstance(reason, TradePlanFailure) else reason,
    )


def _result(
    *,
    valid: bool,
    reason: str,
    side: str,
    low: Decimal,
    high: Decimal,
    entry: Decimal,
    stop: Decimal,
    targets: tuple[Decimal | None, Decimal | None, Decimal | None],
    risk: Decimal,
    reward: Decimal | None,
    rr: Decimal | None,
    entry_reference_type: str,
) -> TradePlanIntegrityResult:
    return TradePlanIntegrityResult(
        valid=valid,
        reason=reason,
        direction=side,  # type: ignore[arg-type]
        entry_low=low,
        entry_high=high,
        entry_reference=entry,
        stop_loss=stop,
        tp1=targets[0],
        tp2=targets[1],
        tp3=targets[2],
        risk_distance=risk,
        reward_distance=reward,
        rr=rr,
        entry_reference_type=entry_reference_type,
    )
