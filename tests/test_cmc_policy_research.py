from datetime import timedelta
from decimal import Decimal

import pytest

from app.cmc.models import EvidenceState as S, Mode
from app.cmc.policy import Action, evaluate, rank
from app.cmc.research import Outcome, ResearchRow, compare_policies
from scripts.cmc_intelligence import demo
from tests.fixtures.cmc import AT, approval, candidate, feature

pytestmark=pytest.mark.no_auto_epoch


@pytest.mark.parametrize("mode",list(Mode))
def test_all_modes_and_explicit_rule_enforcement(mode):
    evaluation=evaluate(candidate(),(feature(),),mode=mode,as_of=AT,activations=(approval(),))
    assert evaluation.vetoed == (mode==Mode.DECISION)
    assert evaluation.hypothetical_veto == (mode in {Mode.SHADOW,Mode.DECISION})
    assert evaluation.final_eligible == (mode!=Mode.DECISION)
    assert evaluation.candidate.setup_id=="synthetic:setup"


@pytest.mark.parametrize("changes",[{"enabled":False},{"version":"obsolete"},{"owner_approval_ref":""},
    {"source_contract_ref":"N/A"},{"out_of_sample_or_prospective_ref":"PENDING"},{"specification_hash":"changed"},
    {"no_material_negative_regression":False},{"expires_at":AT},{"approved_at":AT+timedelta(seconds=1)}])
def test_invalid_or_incomplete_approval_cannot_enforce(changes):
    result=evaluate(candidate(),(feature(),),mode=Mode.DECISION,as_of=AT,activations=(approval(**changes),))
    assert result.hypothetical_veto and not result.vetoed and result.final_eligible


@pytest.mark.parametrize("status",[s for s in S if s!=S.VERIFIED])
def test_optional_missing_stale_and_unreliable_data_never_veto(status):
    result=evaluate(candidate(),(feature(state=status),),mode=Mode.DECISION,as_of=AT,activations=(approval(),))
    assert not result.vetoed
    assert result.recommendations[0].action==Action.INSUFFICIENT_EVIDENCE


def test_future_stale_and_duplicate_feature_evidence_cannot_enforce():
    variants=((feature(input_timestamps=(AT+timedelta(seconds=1),)),),
              (feature(acquired_timestamps=(AT+timedelta(seconds=1),)),),
              (feature(input_timestamps=(AT-timedelta(seconds=301),)),), (feature(),feature()))
    for features in variants:
        assert not evaluate(candidate(),features,mode=Mode.DECISION,as_of=AT,activations=(approval(),)).vetoed


@pytest.mark.parametrize("changes",[{"cci_eligible":False},{"is_new_signal":False},{"setup_id":"N/A"},{"economic_plan_id":"N/A"},{"direction":"N/A"}])
def test_no_promotion_or_historical_followup_selection(changes):
    c=candidate(**changes)
    result=evaluate(c,(feature(),),mode=Mode.DECISION,as_of=AT,activations=(approval(),))
    assert result.final_eligible==c.cci_eligible and not result.recommendations and not result.vetoed


def test_liquidation_veto_is_direction_specific():
    features=(feature("CmcLiquidationPressure",6),feature("CmcLiquidationImbalance","0.8"))
    assert evaluate(candidate(),features,mode=Mode.DECISION,as_of=AT,activations=(approval("liquidation_stress"),)).vetoed
    assert not evaluate(candidate(direction="short"),features,mode=Mode.DECISION,as_of=AT,activations=(approval("liquidation_stress"),)).vetoed


def test_priority_changes_only_break_canonical_ties():
    features=(feature("CmcSectorRelativeStrength",3),feature("CmcGlobalMarketRisk",-1),feature("CmcBasisContext","0.001"))
    a=evaluate(candidate(symbol="ZZZ",canonical_rank=(Decimal(-88),)),features,mode=Mode.DECISION,as_of=AT,activations=(approval("sector_support"),))
    b=evaluate(candidate(symbol="AAA",economic_plan_id="other",canonical_rank=(Decimal(-88),)),(),mode=Mode.DECISION,as_of=AT)
    assert a.priority_delta==1 and rank((b,a))==(a,b)
    better=b.model_copy(update={"candidate":b.candidate.model_copy(update={"canonical_rank":(Decimal(-89),)})})
    assert rank((a,better))==(better,a)
    shadow=evaluate(a.candidate,features,mode=Mode.SHADOW,as_of=AT,activations=(approval("sector_support"),))
    assert shadow.priority_delta==0 and shadow.hypothetical_priority_delta==1


def test_positive_support_does_not_change_eligibility_or_scores():
    features=(feature(value=1),feature("CmcBasisContext","0.001"))
    result=evaluate(candidate(),features,mode=Mode.SHADOW,as_of=AT)
    assert any(r.action==Action.CONFIRMED_SUPPORT for r in result.recommendations)
    assert result.candidate==candidate() and result.priority_delta==0 and result.final_eligible


def test_synthetic_comparison_counts_vetoed_winners_and_losers():
    report=demo()
    assert report["synthetic_fixture_only"] and not report["improvement_proven"]
    assert report["policies"]["CMC_SHADOW_POLICY"]["prevented_losing_trades"]==1
    assert report["policies"]["CMC_SHADOW_POLICY"]["rejected_winning_trades"]==1
    assert report["veto_recommendations"]==2


def test_research_rejects_lookahead_ownership_and_double_counting():
    evaluation=evaluate(candidate(),(feature(),),mode=Mode.SHADOW,as_of=AT)
    outcome=Outcome(economic_plan_id=candidate().economic_plan_id,attribution_ref="N/A")
    row=ResearchRow(selection_group="scan",evaluation=evaluation,outcome=outcome)
    assert compare_policies((row,))["policies"]["CCI_BASELINE"]["net_expectancy_r"]=="N/A"
    with pytest.raises(ValueError): compare_policies((row,row))
    with pytest.raises(ValueError): compare_policies((row.model_copy(update={"outcome":outcome.model_copy(update={"economic_plan_id":"wrong"})}),))
    future=evaluation.recommendations[0].model_copy(update={"data_timestamps":(AT+timedelta(seconds=1),)})
    with pytest.raises(ValueError): compare_policies((row.model_copy(update={"evaluation":evaluation.model_copy(update={"recommendations":(future,)})}),))
