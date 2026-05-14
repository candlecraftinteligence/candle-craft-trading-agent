from __future__ import annotations

from decimal import Decimal

from app.agents.risk_manager import (
    LIQUIDATION_MESSAGE,
    RiskDecision,
    RiskManagerAgent,
    RiskManagerInput,
)
from app.data.dtos import NA


def _agent() -> RiskManagerAgent:
    return RiskManagerAgent()


def _base_long_setup(**overrides: object) -> RiskManagerInput:
    data = {
        "account_equity": Decimal("10000"),
        "risk_per_trade_pct": Decimal("1"),
        "entry_price": Decimal("100"),
        "stop_loss": Decimal("95"),
        "take_profit_targets": (Decimal("110"),),
        "direction": "long",
        "max_daily_risk_pct": Decimal("5"),
        "current_daily_loss_pct": Decimal("1"),
        "data_quality_score": Decimal("80"),
        "invalidation_reason": "Price closes below the invalidation level.",
    }
    data.update(overrides)
    return RiskManagerInput.model_validate(data)


def _base_short_setup(**overrides: object) -> RiskManagerInput:
    data = {
        "account_equity": Decimal("10000"),
        "risk_per_trade_pct": Decimal("1"),
        "entry_price": Decimal("100"),
        "stop_loss": Decimal("105"),
        "take_profit_targets": (Decimal("90"),),
        "direction": "short",
        "max_daily_risk_pct": Decimal("5"),
        "current_daily_loss_pct": Decimal("1"),
        "data_quality_score": Decimal("80"),
        "invalidation_reason": "Price closes above the invalidation level.",
    }
    data.update(overrides)
    return RiskManagerInput.model_validate(data)


def _has_violation(result: RiskDecision, code: str) -> bool:
    return any(violation.code == code for violation in result.violations)


def test_valid_long_setup_approved() -> None:
    result = _agent().analyze(_base_long_setup())

    assert result.approved is True
    assert result.decision == "approved"
    assert result.violations == ()
    assert result.invalidation_reason != NA
    assert result.risk_warning


def test_valid_short_setup_approved() -> None:
    result = _agent().analyze(_base_short_setup())

    assert result.approved is True
    assert result.decision == "approved"
    assert result.violations == ()
    assert result.best_risk_reward_ratio == Decimal("2.00000000")


def test_reject_missing_invalidation() -> None:
    result = _agent().analyze(_base_long_setup(invalidation_reason=None))

    assert result.approved is False
    assert _has_violation(result, "missing_invalidation")


def test_reject_wrong_side_stop_for_long() -> None:
    result = _agent().analyze(_base_long_setup(stop_loss=Decimal("101")))

    assert result.approved is False
    assert _has_violation(result, "wrong_side_stop_loss")
    assert result.position_sizing.risk_per_unit == NA


def test_reject_wrong_side_stop_for_short() -> None:
    result = _agent().analyze(_base_short_setup(stop_loss=Decimal("99")))

    assert result.approved is False
    assert _has_violation(result, "wrong_side_stop_loss")
    assert result.position_sizing.risk_per_unit == NA


def test_reject_risk_reward_below_2() -> None:
    result = _agent().analyze(_base_long_setup(take_profit_targets=(Decimal("104"),)))

    assert result.approved is False
    assert result.best_risk_reward_ratio == Decimal("0.80000000")
    assert _has_violation(result, "risk_reward_below_minimum")


def test_reject_excessive_risk_per_trade() -> None:
    result = _agent().analyze(_base_long_setup(risk_per_trade_pct=Decimal("2.1")))

    assert result.approved is False
    assert _has_violation(result, "risk_per_trade_too_high")


def test_reject_daily_risk_exceeded() -> None:
    result = _agent().analyze(
        _base_long_setup(max_daily_risk_pct=Decimal("3"), current_daily_loss_pct=Decimal("3.1"))
    )

    assert result.approved is False
    assert _has_violation(result, "daily_risk_exceeded")


def test_reject_low_data_quality() -> None:
    result = _agent().analyze(_base_long_setup(data_quality_score=Decimal("59")))

    assert result.approved is False
    assert result.data_reliability == "Unverified"
    assert _has_violation(result, "low_data_quality")


def test_position_size_calculation() -> None:
    result = _agent().analyze(_base_long_setup())

    assert result.position_sizing.risk_amount == Decimal("100.00000000")
    assert result.position_sizing.risk_per_unit == Decimal("5.00000000")
    assert result.position_sizing.position_size == Decimal("20.00000000")


def test_notional_value_calculation() -> None:
    result = _agent().analyze(_base_long_setup())

    assert result.position_sizing.notional_value == Decimal("2000.00000000")


def test_risk_reward_calculation_long() -> None:
    result = _agent().analyze(_base_long_setup(take_profit_targets=(Decimal("115"),)))

    assert result.risk_reward[0].reward == Decimal("15.00000000")
    assert result.risk_reward[0].risk_reward_ratio == Decimal("3.00000000")


def test_risk_reward_calculation_short() -> None:
    result = _agent().analyze(_base_short_setup(take_profit_targets=(Decimal("85"),)))

    assert result.risk_reward[0].reward == Decimal("15.00000000")
    assert result.risk_reward[0].risk_reward_ratio == Decimal("3.00000000")


def test_leverage_missing_returns_na() -> None:
    result = _agent().analyze(_base_long_setup(leverage=None))

    assert result.leverage_risk.leverage == NA
    assert result.leverage_risk.risk_level == NA
    assert "Position size should be based on stop-loss risk" in result.leverage_risk.warning


def test_leverage_high_risk_warning() -> None:
    result = _agent().analyze(_base_long_setup(leverage=Decimal("11")))

    assert result.leverage_risk.leverage == Decimal("11.00000000")
    assert result.leverage_risk.risk_level == "high"
    assert "High leverage risk" in result.leverage_risk.warning
    assert "stop-loss risk" in result.leverage_risk.warning


def test_leverage_extreme_risk_warning() -> None:
    result = _agent().analyze(_base_long_setup(leverage=Decimal("26")))

    assert result.leverage_risk.risk_level == "extreme"
    assert "Extreme leverage risk" in result.leverage_risk.warning


def test_leverage_dangerous_risk_warning() -> None:
    result = _agent().analyze(_base_long_setup(leverage=Decimal("51")))

    assert result.leverage_risk.risk_level == "dangerous"
    assert "Dangerous leverage risk" in result.leverage_risk.warning


def test_liquidation_placeholder_stays_na() -> None:
    result = _agent().analyze(_base_long_setup(leverage=Decimal("5")))

    assert result.leverage_risk.liquidation_distance == NA
    assert result.leverage_risk.liquidation_message == LIQUIDATION_MESSAGE
