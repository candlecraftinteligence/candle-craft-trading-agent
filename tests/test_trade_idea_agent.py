from __future__ import annotations

from decimal import Decimal

from app.agents.trade_idea import TradeIdeaResult, create_trade_idea
from app.data.dtos import NA


def _base_idea(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "symbol": "BTCUSDT",
        "exchange": "Binance",
        "market_type": "perpetual",
        "direction": "long",
        "timeframe": "1h",
        "setup_type": "liquidity_sweep_reclaim",
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop_loss": Decimal("95"),
        "take_profit_targets": (Decimal("112"), Decimal("120")),
        "invalidation": "Price closes below the reclaimed range low.",
        "opportunity_score": Decimal("88"),
        "opportunity_grade": "A",
        "opportunity_decision": "alert_candidate",
        "risk_approved": True,
        "best_rr": Decimal("3.5"),
        "technical_summary": "Bullish sweep and reclaim at support",
        "derivatives_summary": "Open interest confirms participation without crowding",
        "confirmed_facts": ("Range low reclaimed",),
        "missing_data": (),
        "unverified_data": (),
        "cancel_condition": "Cancel if price accepts below the entry zone before trigger.",
    }
    data.update(overrides)
    return data


def _idea(**overrides: object) -> TradeIdeaResult:
    return create_trade_idea(_base_idea(**overrides))


def _has_violation(result: TradeIdeaResult, code: str) -> bool:
    return any(violation.code == code for violation in result.quality_gate_result.violations)


def test_creates_valid_long_trade_idea() -> None:
    result = _idea()

    assert result.quality_gate_result.passed is True
    assert result.status == "conditional"
    assert result.direction == "long"
    assert result.entry_zone.low == Decimal("100.00000000")
    assert result.entry_zone.high == Decimal("102.00000000")
    assert result.stop_loss.price == Decimal("95.00000000")
    assert result.invalidation == "Price closes below the reclaimed range low."
    assert result.take_profits[0].price == Decimal("112.00000000")
    assert result.best_rr == Decimal("3.50000000")
    assert result.confidence_score == Decimal("88.00000000")
    assert "Technical context: Bullish sweep and reclaim at support." in result.reason_for_trade
    assert result.risk_warning.startswith("This is not financial advice.")


def test_creates_valid_short_trade_idea() -> None:
    result = _idea(
        direction="short",
        entry_low=Decimal("98"),
        entry_high=Decimal("100"),
        stop_loss=Decimal("105"),
        take_profit_targets=(Decimal("90"), Decimal("84")),
        setup_type="bearish_breakdown_retest",
        invalidation="Price closes back above the breakdown level.",
        technical_summary="Bearish retest below lost support",
    )

    assert result.quality_gate_result.passed is True
    assert result.direction == "short"
    assert result.stop_loss.price == Decimal("105.00000000")
    assert result.take_profits[1].price == Decimal("84.00000000")


def test_rejects_score_below_80() -> None:
    result = _idea(opportunity_score=Decimal("79.99"))

    assert result.status == "rejected"
    assert result.quality_gate_result.passed is False
    assert _has_violation(result, "score_below_minimum")


def test_rejects_opportunity_decision_reject() -> None:
    result = _idea(opportunity_decision="reject")

    assert result.status == "rejected"
    assert _has_violation(result, "opportunity_rejected")


def test_rejects_risk_not_approved() -> None:
    result = _idea(risk_approved=False)

    assert result.status == "rejected"
    assert _has_violation(result, "risk_not_approved")


def test_rejects_missing_invalidation() -> None:
    result = _idea(invalidation=None)

    assert result.status == "rejected"
    assert result.invalidation == NA
    assert "invalidation: N/A" in result.missing_data
    assert _has_violation(result, "missing_invalidation")


def test_rejects_missing_stop_loss() -> None:
    result = _idea(stop_loss=None)

    assert result.status == "rejected"
    assert result.stop_loss.price == NA
    assert "stop_loss: N/A" in result.missing_data
    assert _has_violation(result, "missing_stop_loss")


def test_rejects_missing_entry_zone() -> None:
    result = _idea(entry_low=None)

    assert result.status == "rejected"
    assert result.entry_zone.low == NA
    assert "entry_zone: N/A" in result.missing_data
    assert _has_violation(result, "missing_entry_zone")


def test_rejects_no_take_profits() -> None:
    result = _idea(take_profit_targets=())

    assert result.status == "rejected"
    assert result.take_profits == ()
    assert "take_profit_targets: N/A" in result.missing_data
    assert _has_violation(result, "missing_take_profit_targets")


def test_rejects_rr_below_2() -> None:
    result = _idea(best_rr=Decimal("1.99"))

    assert result.status == "rejected"
    assert _has_violation(result, "risk_reward_below_minimum")


def test_status_conditional_by_default() -> None:
    result = _idea()

    assert result.status == "conditional"


def test_status_active_only_when_entry_triggered_true() -> None:
    result = _idea(entry_triggered=True)

    assert result.status == "active"


def test_missing_technical_summary_marked_na() -> None:
    result = _idea(technical_summary=None)

    assert result.quality_gate_result.passed is True
    assert result.context.technical_summary == NA
    assert "Technical context: N/A." in result.reason_for_trade
    assert "technical_summary: N/A" in result.missing_data


def test_missing_derivatives_summary_marked_na() -> None:
    result = _idea(derivatives_summary=None)

    assert result.quality_gate_result.passed is True
    assert result.context.derivatives_summary == NA
    assert "Derivatives context: N/A." in result.reason_for_trade
    assert "derivatives_summary: N/A" in result.missing_data


def test_unverified_data_preserved() -> None:
    result = _idea(unverified_data=("funding: Unverified", "open_interest: Unverified"))

    assert result.unverified_data == ("funding: Unverified", "open_interest: Unverified")


def test_leverage_risk_warning_included() -> None:
    result = _idea(leverage=Decimal("11"))

    assert "Leverage increases liquidation risk" in result.risk_warning
    assert "High leverage risk." in result.risk_warning


def test_dangerous_leverage_warning_included() -> None:
    result = _idea(leverage=Decimal("51"))

    assert "Dangerous leverage risk." in result.risk_warning


def test_rejects_trade_idea_target_inside_entry_zone() -> None:
    result = _idea(
        take_profit_targets=(Decimal("101"), Decimal("112"), Decimal("120")),
    )

    assert result.status == "rejected"
    assert _has_violation(result, "trade_plan_integrity_failed")


def test_rejects_trade_idea_stop_inside_entry_zone() -> None:
    result = _idea(stop_loss=Decimal("101"))

    assert result.status == "rejected"
    assert _has_violation(result, "trade_plan_integrity_failed")
