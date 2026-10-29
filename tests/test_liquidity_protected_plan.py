"""Deterministic structural-stop and final public economics contracts."""
from decimal import Decimal as D

import pytest

from app.analytics.structural_liquidity import LiquidityLevel, confirmed_levels, protected_stop
from app.analytics.pullback_zones import analyze_pullback_zone
from app.core.trade_plan_integrity import validate_public_trade_plan
from app.agents.risk_manager import RiskManagerAgent, RiskManagerInput
from app.lifecycle.eligibility import has_valid_rr
from app.core.rejection_category import rejection_category
from tests.test_selective_fib_mtf_v2 import _pullback


@pytest.mark.parametrize("direction", ["long", "short"])
def test_stop_protects_local_liquidity_with_outward_tick_and_bounded_distance(direction):
    long = direction == "long"
    edge = D("100")
    levels = [LiquidityLevel(D(price), "below" if long else "above", "4h", i, i)
              for i, price in enumerate(("99.5", "99.5", "90") if long else ("100.5", "100.5", "110"))]
    stop = protected_stop(direction=direction, entry=D("105") if long else D("95"),
        invalidation=edge, atr=D("1"), tick_size=D(".07"), spread=D(".15"), levels=levels)
    assert stop.liquidity_boundary == (D("99.5") if long else D("100.5"))
    assert stop.price < D("99.35") if long else stop.price > D("100.65")
    assert stop.price % D(".07") == 0 and stop.buffer == D(".15")
    assert stop.distant_levels_ignored == 1 and len(stop.included_levels) == 2


def test_only_confirmed_closed_swings_enter_liquidity_and_swept_targets_are_identified():
    bars = [dict(timestamp=i, high=D(110), low=D(low)) for i, low in enumerate([105,104,100,104,105,103,99])]
    levels = confirmed_levels(bars, timeframe="4h")
    assert any(level.price == 100 and level.swept for level in levels)
    assert not any(level.price == 99 for level in levels)  # two right neighbours are absent


@pytest.mark.parametrize("direction", ["long", "short"])
def test_worst_entry_and_friction_are_used_and_tp3_cannot_rescue_tp2(direction):
    long = direction == "long"
    values = dict(direction=direction, entry_low=D("100") if long else D("98"),
        entry_high=D("102") if long else D("100"), stop_loss=D("95") if long else D("105"),
        tp1=D("110") if long else D("90"), tp2=D("115") if long else D("85"),
        tp3=D("150") if long else D("50"))
    plan = validate_public_trade_plan(**values)
    assert not plan.valid and plan.reason == "rr_below_minimum"
    assert plan.entry_reference == (D("102") if long else D("98"))
    values["tp2"] = D("123") if long else D("77")
    assert validate_public_trade_plan(**values).valid
    assert not validate_public_trade_plan(**values, execution_cost=D(".1")).valid
    assert not validate_public_trade_plan(**values, minimum_rr=D("4")).valid


def test_asserted_rr_cannot_overrule_actual_tp2_economics():
    bad = dict(direction="long", entry_low="100", entry_high="102", stop_loss="95",
               tp1="110", tp2="115", tp3="150", rr="10")
    assert not has_valid_rr(bad)
    assert not has_valid_rr({**bad, "tp2": "N/A"})


def test_larger_stop_reduces_size_with_constant_monetary_risk_and_matching_net_rr():
    def decision(stop):
        return RiskManagerAgent().analyze(RiskManagerInput(account_equity=D("10000"), risk_per_trade_pct=D("1"),
            direction="long", entry_price=D("100"), stop_loss=stop,
            take_profit_targets=(D("120"), D("140"), D("160")),
            execution_cost=D(".2"), minimum_tp2_rr=D("3"), data_quality_score=D("80"),
            invalidation_reason="A close beyond the stored stop invalidates this plan."))
    narrow, wide = decision(D("95")), decision(D("90"))
    assert narrow.approved and wide.approved
    assert wide.position_sizing.risk_amount == narrow.position_sizing.risk_amount == D("100")
    assert wide.position_sizing.position_size < narrow.position_sizing.position_size
    assert wide.position_sizing.position_size*wide.position_sizing.risk_per_unit <= D("100")
    canonical = validate_public_trade_plan(direction="long", entry_low=100, entry_high=100,
        stop_loss=90, tp1=120, tp2=140, tp3=160, execution_cost=D(".2"))
    assert abs(wide.risk_reward[1].risk_reward_ratio-canonical.rr) <= D(".00000001")


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("ratio", ["0.618", "0.750", "0.786"])
def test_each_level_produces_a_liquidity_protected_net_three_r_plan(ratio, direction):
    plan = analyze_pullback_zone(_pullback(ratio, direction))
    assert plan.valid, plan.pullback_failure_reason
    canonical = validate_public_trade_plan(direction=direction, entry_low=plan.entry_low,
        entry_high=plan.entry_high, stop_loss=plan.stop, tp1=plan.tp1, tp2=plan.tp2,
        tp3=plan.tp3, execution_cost=plan.execution_cost)
    assert canonical.valid and canonical.rr >= D("3")
    assert abs(canonical.rr-plan.rr_to_tp2) <= D(".00000001")
    assert plan.tp2_source == "opposing_structural_liquidity"
    assert plan.stop_evidence["price"] == plan.stop and plan.execution_cost > 0


def test_missing_or_near_opposing_liquidity_is_rejected_without_target_expansion():
    missing = analyze_pullback_zone(_pullback(user_resistance_levels=None))
    assert not missing.valid and missing.first_failed_gate == "missing_opposing_liquidity"
    weak = analyze_pullback_zone(_pullback(user_resistance_levels=[D("130"), D("140"), D("150")]))
    assert not weak.valid and weak.first_failed_gate == "rr_below_minimum"
    assert weak.tp2 == D("140")


@pytest.mark.parametrize("gate,category", [
    ("missing_confirmed_sweep", "NO_VALID_SETUP"),
    ("entry_confirmation_missing", "MISSING_CONFIRMATION"),
    ("rr_below_minimum", "INVALID_RISK_REWARD"),
    ("instrument_precision_unverified", "DATA_UNAVAILABLE"),
    ("stale_mandatory_candles", "DATA_STALE"),
])
def test_rejections_do_not_conflate_market_and_operational_results(gate, category):
    assert rejection_category(gate) == category
    assert rejection_category(gate, valid=True) == "N/A"
    assert rejection_category(gate, provider_failure=True) == "PROVIDER_FAILURE"
    assert rejection_category(gate, internal_error=True) == "INTERNAL_ERROR"


def test_publication_revalidates_worst_entry_instead_of_trusting_reported_rr(tmp_path):
    import asyncio
    from tests.test_triggered_confirmed_telegram_delivery import (
        FakeSender, _public_setup_symbol, _service, _run_result, _with_lifecycle_fields,
    )
    sender = FakeSender()
    symbol = _with_lifecycle_fields(_public_setup_symbol(signal_id="forged-rr"), tp2="115", tp3="120", rr="10")
    result = asyncio.run(_service(tmp_path/"bad-rr.db", sender).deliver_for_run(_run_result(symbol), scan_run_id="forged"))
    assert result.sent == 0 and sender.messages == []
    assert "confirmed_economic_plan:rr_below_minimum" in result.deliveries[0].error_message


def test_simulated_contact_cannot_supply_missing_structural_confirmation(tmp_path):
    import asyncio
    import json
    from tests.test_triggered_confirmed_telegram_delivery import (
        FakeSender, _generated_entry_batch, _service, _run_result,
    )
    path = tmp_path/"contact.db"
    symbol = _generated_entry_batch(path)
    metadata = json.loads(symbol.lifecycle_outcome_progress.metadata_json)
    assert metadata["execution_semantics"] == "simulated_price_contact"
    assert metadata["user_execution_confirmed"] == metadata["exchange_fill_verified"] == "false"
    symbol = symbol.model_copy(update={"strategy_diagnostics": {"swing": {
        **symbol.strategy_diagnostics["swing"], "confirmation_structure_shift_status": "N/A"}}})
    sender = FakeSender()
    result = asyncio.run(_service(path, sender).deliver_for_run(_run_result(symbol), scan_run_id="no-confirmation"))
    assert result.sent == 0 and sender.messages == []
    assert any("simulated_contact_not_structural_confirmation" in (item.error_message or "") for item in result.deliveries)


def test_target_assessment_uses_final_plan_and_revalidates_claims():
    from dataclasses import replace
    from app.analytics.target_intelligence import build_target_intelligence
    from app.core.trade_plan_integrity import validate_public_trade_plan
    plan = validate_public_trade_plan(direction="long", entry_low=D("100"), entry_high=D("102"),
        stop_loss=D("95"),tp1=D("110"),tp2=D("125"),tp3=D("130"),execution_cost=D(".1"))
    assert plan.valid
    # An unrelated closer candidate must not replace the actual structural TP2.
    result = build_target_intelligence(economic_plan=plan,nearest_resistance=D("103"))
    assert result.tp2_candidate == plan.tp2 and result.rr_to_tp2 == plan.rr
    assert result.target_failure_type == "N/A"
    assert result.target_confidence == "N/A"  # no invented confidence score
    assert result.rr_projections[1].rr == plan.rr
    bad = replace(plan,tp2=D("115"),rr=D("10"),valid=True)
    rejected = build_target_intelligence(economic_plan=bad)
    assert rejected.target_quality_grade.value == "Reject"
    assert rejected.target_failure_type.value == "RR_BELOW_MINIMUM"
