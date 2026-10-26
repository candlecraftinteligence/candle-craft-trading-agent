from datetime import timedelta
from decimal import Decimal

import pytest

from app.cmc.comparison import compare
from app.cmc.endpoints import Endpoint
from app.cmc.features import build_features
from app.cmc.identity import AssetBinding, match_instrument
from app.cmc.models import Comparison as C, EvidenceState as S
from app.cmc.normalize import normalize_context, normalize_pair
from tests.fixtures.cmc import AT, fetched, instrument, obs, pair

pytestmark = pytest.mark.no_auto_epoch


@pytest.mark.parametrize("value,state",[(None,S.MISSING),("NaN",S.ERROR),("Infinity",S.ERROR),(True,S.ERROR),("-1",S.ERROR),("101",S.ERROR),("0",S.VERIFIED)])
def test_evidence_distinguishes_zero_absence_and_invalid(value,state):
    assert obs(value=value).state == state


def test_source_time_is_never_acquisition_time():
    value = obs().model_copy(update={"source_at":None})
    assert value.status_at(AT) == S.UNVERIFIED
    assert obs().status_at(AT+timedelta(seconds=301)) == S.STALE
    assert obs().status_at(AT-timedelta(seconds=1)) == S.UNAVAILABLE
    assert obs().model_copy(update={"source_at":AT+timedelta(seconds=1)}).status_at(AT) == S.UNVERIFIED
    assert obs(value=None).display == "N/A"
    assert value.model_copy(update={"state":S.UNVERIFIED}).display == "Unverified"


def test_future_identity_dependencies_block_causal_use():
    value = obs(instrument=instrument(metadata_acquired_at=AT+timedelta(seconds=1)))
    assert value.status_at(AT) == S.UNAVAILABLE
    assert instrument().key == instrument(metadata_acquired_at=AT+timedelta(seconds=1)).key


def _mapping(**changes):
    binding = AssetBinding(cmc_id=1,cmc_slug="bitcoin",binance_base="BTC",approved=True,
        evidence_ref="synthetic:asset-review",approved_at=AT-timedelta(days=1))
    args = dict(pair=pair(),exchange={"exchange_id":270,"exchange_slug":"binance"},
        metadata={"symbol":"BTCUSDT","baseAsset":"BTC","quoteAsset":"USDT","marginAsset":"USDT",
                  "contractType":"PERPETUAL","status":"TRADING"},binding=binding,
        asset={"id":1,"slug":"bitcoin","is_active":1},as_of=AT,metadata_at=AT,
        acquired_at=AT,map_acquired_at=AT,exchange_registry={"exchange_id":270,"exchange_slug":"binance"},exchange_registry_acquired_at=AT)
    return match_instrument(**{**args,**changes})


def test_exact_identity_mapping():
    result = _mapping()
    assert result.state == S.VERIFIED and result.instrument.cmc_id == 1


@pytest.mark.parametrize("section,key,value",[("pair","category","futures"),("pair","outlier_detected",True),
    ("metadata","contractType","CURRENT_QUARTER"),("metadata","status","BREAK"),
    ("metadata","marginAsset","BTC"),("metadata","baseAsset","BTC2"),("asset","id",999),
    ("asset","slug","another-bitcoin"),("exchange","exchange_slug","bybit")])
def test_symbol_collision_and_contract_mismatches_are_rejected(section,key,value):
    if section == "pair":
        changed=pair()
    elif section == "metadata":
        changed={"symbol":"BTCUSDT","baseAsset":"BTC","quoteAsset":"USDT","marginAsset":"USDT","contractType":"PERPETUAL","status":"TRADING"}
    elif section == "asset":
        changed={"id":1,"slug":"bitcoin","is_active":1}
    else:
        changed={"exchange_id":270,"exchange_slug":"binance"}
    changed[key]=value
    assert _mapping(**{section:changed}).state != S.VERIFIED


def test_unknown_futures_units_and_intervals_are_not_verified():
    data = {o.metric:o for o in normalize_pair(fetched(Endpoint.EXCHANGE_PAIRS),pair(),instrument())}
    assert data["open_interest"].state == S.UNVERIFIED
    assert data["funding_rate"].state == S.UNVERIFIED
    assert data["basis"].value == Decimal(1)/99
    assert data["provider_index_basis"].state == S.UNVERIFIED
    assert data["basis"].value != data["provider_index_basis"].value


def test_verified_reviewed_semantics_preserve_financial_decimals():
    observations=normalize_pair(fetched(Endpoint.EXCHANGE_PAIRS),pair(),instrument(),oi_units="base",
        oi_counting_convention="open_contract_quantity",funding_interval_seconds=28800,funding_semantics="settled_funding")
    data={o.metric:o for o in observations}
    assert data["open_interest"].state == data["funding_rate"].state == S.VERIFIED
    assert data["funding_rate"].value == Decimal("0.0001")
    assert data["funding_rate"].funding_interval_seconds == 28800


@pytest.mark.parametrize("change,classification",[({},C.CONSISTENT),({"value":Decimal("100")},C.MATERIAL_DIVERGENCE),
    ({"source_at":AT-timedelta(seconds=40)},C.TEMPORALLY_MISALIGNED),({"units":"USD"},C.UNIT_MISMATCH),
    ({"instrument":instrument(market_id=101)},C.IDENTITY_MISMATCH),({"scope":"global_all_contracts"},C.NOT_COMPARABLE),
    ({"state":S.UNVERIFIED},C.UNVERIFIED),({"counting_convention":"both_sides"},C.UNIT_MISMATCH)])
def test_comparison_requires_equivalent_identity_units_time_and_scope(change,classification):
    left=obs("open_interest","50",instrument=instrument(),scope="contract")
    right=left.model_copy(update={"provider":"Binance",**change})
    assert compare(left,right,as_of=AT) == classification


def test_altcoin_contract_and_global_liquidation_semantics():
    alt=normalize_context(fetched(Endpoint.ALTCOIN,{"altcoin_index":75,"snapshot_time":AT.isoformat()}))[0]
    assert alt.state == S.VERIFIED and alt.value == 75
    quote={"symbol":"USD","crypto_id":2781,"last_updated":AT.isoformat()}
    for suffix,total in (("1h",100),("4h",200),("24h",400)):
        quote.update({f"total_liquidations_{suffix}":total,f"long_liquidations_{suffix}":total*Decimal("0.9"),f"short_liquidations_{suffix}":total*Decimal("0.1")})
    observations=normalize_context(fetched(Endpoint.LIQUIDATIONS,{"quotes":[quote]}))
    features={f.name:f for f in build_features(observations,as_of=AT)}
    assert features["CmcLiquidationPressure"].value == 6
    assert features["CmcLiquidationImbalance"].value == Decimal("0.8")
    assert all(o.scope == "global_all_contracts" for o in observations)
    quote["total_liquidations_1h"]=999
    invalid=normalize_context(fetched(Endpoint.LIQUIDATIONS,{"quotes":[quote]}))
    assert {o.state for o in invalid if o.window_seconds==3600} == {S.UNVERIFIED}


def test_missing_features_never_receive_favorable_defaults():
    features=build_features((),as_of=AT)
    assert all(f.value is None and f.state==S.MISSING for f in features)
    stale=build_features((obs(),),as_of=AT+timedelta(seconds=301))
    assert all(f.value is None for f in stale)


def test_v3_quote_array_and_historical_quote_map_are_separate_contracts():
    v3=fetched(Endpoint.LISTINGS,[{"id":1,"cmc_rank":1,"last_updated":AT.isoformat(),
        "quote":[{"id":2781,"symbol":"USD","percent_change_24h":"2.5","last_updated":AT.isoformat()}]}])
    data={o.metric:o for o in normalize_context(v3)}
    assert data["market_cap_rank"].state==S.VERIFIED
    assert data["asset_return_24h"].value==Decimal("2.5")
    historic=fetched(Endpoint.HISTORICAL_LISTINGS,[{"id":1,"cmc_rank":1,"last_updated":AT.isoformat(),
        "quote":{"USD":{"percent_change_24h":"2.5","last_updated":AT.isoformat()}}}])
    assert [o.value for o in normalize_context(historic)]==[o.value for o in normalize_context(v3)]
    assert normalize_context(fetched(Endpoint.GLOBAL,[]))[0].state==S.ERROR


def test_cross_exchange_funding_dispersion_requires_equivalent_intervals_and_semantics():
    def rates(i,rate,interval=28800,semantics="settled_funding"):
        from app.cmc.normalize import observation
        return observation(fetched(),"funding_rate",rate,units="fraction/interval",source=AT,
            cmc_id=1,exchange_id=i.exchange_id,instrument=i,scope="contract",
            funding_interval_seconds=interval,rate_semantics=semantics)
    one=instrument()
    two=instrument(exchange="bybit",exchange_id=521,market_id=200)
    first=rates(one,"0.0001")
    second=rates(two,"0.0004")
    features=lambda values:{f.name:f for f in build_features(values,as_of=AT,cmc_id=1,instrument_key=one.key)}
    assert features((first,second))["CmcFundingDispersion"].value==Decimal("0.0003")
    assert features((first,rates(two,"0.0004",14400)))["CmcFundingDispersion"].state==S.MISSING
    assert features((first,rates(two,"0.0004",semantics="indicative_funding")))["CmcFundingDispersion"].state==S.MISSING


def test_regime_compatibility_is_explicit_research_context():
    from app.cmc.features import regime_compatibility
    from tests.fixtures.cmc import feature
    kwargs=dict(regime_state="RISK_OFF",regime_at=AT,regime_ref="synthetic:cci-regime",as_of=AT)
    assert regime_compatibility(feature("CmcGlobalMarketRisk",4),direction="long",**kwargs).value==-1
    assert regime_compatibility(feature("CmcGlobalMarketRisk",4),direction="short",**kwargs).value==1


def test_coverage_provenance_does_not_repeat_irrelevant_snapshot_rows():
    observations=(obs(),*(obs("unused",str(i)) for i in range(1500)))
    features={f.name:f for f in build_features(observations,as_of=AT)}
    assert features["CmcDataConfidence"].evidence_ids==(observations[0].id,)
