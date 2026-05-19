from __future__ import annotations

from decimal import Decimal

from app.strategies import liquidity_grab_pullback as strategy


def test_phase40_does_not_change_strategy_gate_constants() -> None:
    assert strategy.SWEEP_ATR_MULTIPLIER == Decimal("0.35")
    assert strategy.VOLUME_CONFIRMATION_MULTIPLIER == Decimal("1.5")
    assert strategy.BASE_MIN_RR == Decimal("2.5")
    assert strategy.CHALLENGE_MIN_RR == Decimal("3.0")
    assert strategy.RISK_WARNING == (
        "This is not financial advice. Pullback ideas are conditional and must be invalidated at the stop."
    )
