import asyncio
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.cmc.models import Snapshot
from app.cmc.normalize import normalize_context
from app.cmc.scanner_context import scanner_context
from app.cmc.endpoints import Endpoint
from app.cmc.runtime import Service
from tests.fixtures.cmc import AT, fetched, obs, instrument
from tests.test_integrated_signal_product import _scan

pytestmark = pytest.mark.no_auto_epoch


@pytest.mark.parametrize("failure", ["returns_none", "throws"])
@pytest.mark.parametrize("scenario", ["before_contact", "valid", "provider_failure"])
def test_collector_construction_failure_has_per_result_status_and_no_decision_effect(monkeypatch,failure,scenario):
    baseline,_ = _scan(Decimal(".750"),"long",scenario=scenario,room=True)
    async def broken(*args,**kwargs):
        if failure == "throws": raise ValueError("synthetic")
        return None
    monkeypatch.setattr("app.cmc.runtime.begin_collection",broken)
    result,_ = _scan(Decimal(".750"),"long",scenario=scenario,room=True,cmc_mode="shadow")
    a,b = baseline.results[0], result.results[0]
    assert b.cmc_context["acquisition_status"] == "COLLECTOR_FAILURE"
    assert b.cmc_context["identity_status"] == "NO_APPROVED_INSTRUMENT_MAPPING"
    assert b.cmc_context["observations"] == [] and b.cmc_context["setup_authority"] is False
    assert b.trade_idea == a.trade_idea and b.rejection_reason == a.rejection_reason
    assert b.strategy_diagnostics == a.strategy_diagnostics and b.status_history == a.status_history
    assert baseline.config.decision_timestamp == result.config.decision_timestamp


@pytest.mark.parametrize("errors,expected", [((),"PENDING_ACQUISITION"),
    (("endpoint:credit_budget",),"BUDGET_EXHAUSTED"),
    (("collector_acquisition_failure:TimeoutError",),"ACQUISITION_FAILURE")])
def test_unavailable_pending_budget_and_failure_are_distinct(errors,expected):
    result = scanner_context(Snapshot(errors=errors),symbol="BTCUSDT",cutoff=AT,assessed_at=AT)
    assert result["acquisition_status"] == expected
    assert result["context_status"] == "STALE_OR_UNAVAILABLE"


def test_global_only_contract_specific_and_stale_are_distinct_and_never_attach_other_contract():
    global_row = obs()
    own = obs(metric="price",instrument=instrument(),scope="contract")
    unrelated = obs(metric="price",instrument=instrument(symbol="ETHUSDT",base="ETH"),scope="contract")
    def context(snapshot,at=AT):
        return scanner_context(snapshot,symbol="BTCUSDT",cutoff=AT,assessed_at=at)
    global_only = context(Snapshot(observations=(global_row,unrelated),batch_id="synthetic"))
    assert global_only["context_status"] == "GLOBAL_ONLY_CONTEXT"
    assert len(global_only["observations"]) == 1
    both = context(Snapshot(observations=(global_row,own),batch_id="synthetic"))
    assert both["global_context_status"] == "VERIFIED_GLOBAL_CONTEXT"
    assert both["contract_context_status"] == "VERIFIED_CONTRACT_CONTEXT"
    stale = context(Snapshot(observations=(global_row,own),batch_id="synthetic"),AT+timedelta(seconds=301))
    assert stale["context_status"] == "STALE_OR_UNAVAILABLE"
    assert all(v["value"] == "N/A" for v in stale["observations"])


def test_refresh_failure_preserves_evidence_times_and_records_failure():
    async def exercise():
        class Broken:
            async def collect(self): raise RuntimeError("synthetic")
        service = Service(Broken(),None)
        service.snapshot = Snapshot(observations=(obs(),),batch_id="original")
        previous = service.snapshot
        await service._refresh()
        assert service.snapshot is previous
        assert service.snapshot.observations == (obs(),)
        assert service.last_acquisition["outcome"] == "RuntimeError"
        context = scanner_context(service.research_snapshot,symbol="BTCUSDT",cutoff=AT,assessed_at=AT)
        assert context["acquisition_status"] == "ACQUISITION_FAILURE"
    asyncio.run(exercise())


def test_exchange_aggregate_is_neither_global_nor_contract_context():
    exchange = obs(metric="open_interest_usd",scope="exchange_all_contracts",exchange_id=270)
    other = obs(metric="open_interest_usd",scope="exchange_all_contracts",exchange_id=999)
    context = scanner_context(Snapshot(observations=(exchange,other),batch_id="synthetic"),
        symbol="BTCUSDT",cutoff=AT,assessed_at=AT)
    assert context["exchange_context_status"] == "VERIFIED_EXCHANGE_AGGREGATE"
    assert context["global_context_status"] == context["contract_context_status"] == "STALE_OR_UNAVAILABLE"
    assert len(context["observations"]) == 1


def test_construction_failure_with_retained_snapshot_preserves_evidence_and_reports_failure(monkeypatch):
    from tests.test_integrated_signal_product import AT as SCAN_AT
    async def absent(*args,**kwargs): return None
    monkeypatch.setattr("app.cmc.runtime.begin_collection",absent)
    retained = Snapshot(observations=(obs().model_copy(update={
        "source_at":SCAN_AT-timedelta(minutes=15),"acquired_at":SCAN_AT-timedelta(minutes=15)}),),batch_id="retained")
    result,_ = _scan(Decimal(".750"),"long",scenario="before_contact",room=True,cmc_mode="shadow",cmc_snapshot=retained)
    context = result.results[0].cmc_context
    assert context["acquisition_status"] == "COLLECTOR_FAILURE" and context["batch_id"] == "retained"
    assert context["observations"][0]["acquired_at"] == (SCAN_AT-timedelta(minutes=15)).isoformat()
    assert retained.errors == ()
