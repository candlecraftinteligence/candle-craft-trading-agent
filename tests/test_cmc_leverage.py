import pytest

from app.cmc.leverage import selection_leverage
from app.cmc.models import Mode
from app.cmc.policy import evaluate
from tests.fixtures.cmc import AT, candidate, feature

pytestmark = pytest.mark.no_auto_epoch


def test_leverage_counts_exact_eligible_ties_without_claiming_delivery_changes():
    features = (feature("CmcGlobalMarketRisk",4),feature("CmcLiquidationPressure",3))
    values = tuple(evaluate(candidate(symbol=s,direction=d,canonical_rank=(q,),cci_eligible=eligible,
        alert_type="SIGNAL_CONFIRMED"),features,mode=Mode.SHADOW,as_of=AT)
        for s,d,q,eligible in (("A","long",-88,True),("B","short",-88,True),
            ("C","long",-90,True),("D","long",-88,False)))
    report = selection_leverage(values)
    assert report["eligible_new_candidates"] == 3
    assert report["exact_rank_tie_groups"] == 1 and report["candidates_in_exact_rank_ties"] == 2
    assert report["ties_with_different_hypothetical_priorities"] == 1
    assert not report["actual_delivery_change_proven"] and report["economic_outcome"] == "N/A"
    assert all(v.priority_delta == 0 and not v.vetoed for v in values)


def test_empty_or_unique_ranks_cannot_demonstrate_selection_leverage():
    assert selection_leverage(())["exact_rank_tie_groups"] == 0
    values = tuple(evaluate(candidate(canonical_rank=(rank,)),(),mode=Mode.SHADOW,as_of=AT) for rank in (-88,-89))
    assert selection_leverage(values)["exact_rank_tie_groups"] == 0
