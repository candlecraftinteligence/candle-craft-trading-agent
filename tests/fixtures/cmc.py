"""Synthetic CMC contracts, never captured or invented market history."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from app.cmc.client import Fetch
from app.cmc.endpoints import Endpoint
from app.cmc.models import EvidenceState as S, Feature, Instrument
from app.cmc.normalize import observation
from app.cmc.policy import Activation, Candidate, RULES

AT = datetime(2026,1,1,tzinfo=UTC)


def instrument(**updates):
    data = dict(cmc_id=1,market_id=100,exchange_id=270,exchange="binance",symbol="BTCUSDT",
        base="BTC",quote="USDT",quote_id=825,margin_asset="USDT",metadata_ref="synthetic:metadata",
        binding_ref="synthetic:binding",metadata_acquired_at=AT-timedelta(seconds=1),
        binding_approved_at=AT-timedelta(days=1),map_acquired_at=AT-timedelta(seconds=1))
    data.update(exchange_registry_acquired_at=AT-timedelta(seconds=1),exchange_registry_ref="synthetic:exchange-registry")
    return Instrument(**{**data,**updates})


def fetched(endpoint=Endpoint.GLOBAL,data=None,**updates):
    return Fetch(endpoint=endpoint,acquired_at=AT,state=S.UNVERIFIED,data=data,
        raw={"synthetic":True,"data":data},raw_ref="synthetic:raw",**updates)


def pair():
    return {"market_id":100,"market_pair_symbol":"BTC/USDT","category":"perpetual",
        "outlier_detected":False,"exclusions":None,
        "market_pair_base":{"crypto_id":1,"symbol":"BTC","exchange_symbol":"BTC"},
        "market_pair_quote":{"crypto_id":825,"symbol":"USDT","exchange_symbol":"USDT"},
        "exchange_reported_quotes":[{"convert_id":2781,"convert_symbol":"USD","price":"100",
            "index_price":"99","index_basis":"0.99","funding_rate":"0.0001","open_interest":"50",
            "last_updated":AT.isoformat()}]}


def obs(metric="btc_dominance",value="60",**updates):
    return observation(fetched(),metric,value,units="percent",source=AT,**updates)


def feature(name="CmcDataConsistency",value=-1,**updates):
    return Feature(**{**dict(name=name,value=Decimal(str(value)),units="classification",state=S.VERIFIED,
        evidence_ids=("synthetic:evidence",),input_timestamps=(AT,),acquired_timestamps=(AT,),reason="synthetic"),**updates})


def candidate(**updates):
    return Candidate(**{**dict(setup_id="synthetic:setup",economic_plan_id="synthetic:plan",symbol="BTCUSDT",
        direction="long",cci_eligible=True),**updates})


def approval(rule_id="data_divergence",**updates):
    rule = next(r for r in RULES if r.id == rule_id)
    return Activation(**{**dict(rule_id=rule.id,version=rule.version,specification_hash=rule.specification_hash,
        enabled=True,source_contract_ref="synthetic:test",causal_evaluation_ref="synthetic:test",
        out_of_sample_or_prospective_ref="synthetic:test",tests_ref="synthetic:test",hard_gate_compatibility_ref="synthetic:test",
        baseline_comparison_ref="synthetic:test",no_material_negative_regression=True,owner_approval_ref="synthetic:approval",
        approved_at=AT,expires_at=AT+timedelta(days=1)),**updates})
