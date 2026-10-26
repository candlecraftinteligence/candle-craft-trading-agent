"""Independent formulas with evidence-specific missingness; no composite trading score."""
from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from app.cmc.models import EvidenceState as S, Feature, Observation

FEATURE_VERSION = "1"
FEATURE_NAMES = (
    "CmcGlobalMarketRisk", "CmcDerivativesLeverage", "CmcFundingDispersion",
    "CmcOpenInterestContext", "CmcBasisContext", "CmcLiquidationPressure",
    "CmcLiquidationImbalance", "CmcBtcDominanceContext", "CmcAltcoinRotation",
    "CmcSectorRelativeStrength", "CmcDataConfidence", "CmcFundingContext", "CmcMarketCapRank",
    "CmcRegimeCompatibility",
)


def build_features(observations: tuple[Observation, ...], *, as_of: datetime,
                   cmc_id: int | None = None, instrument_key: str | None = None) -> tuple[Feature, ...]:
    causal = [o for o in observations if o.status_at(as_of) == S.VERIFIED]

    def select(metric, *, scope=None, asset=False, contract=False):
        candidates = [o for o in causal if o.metric == metric and (scope is None or o.scope == scope)
                      and (not asset or cmc_id is not None and o.cmc_id == cmc_id)
                      and (not contract or o.instrument is not None and o.instrument.key == instrument_key)]
        # Multiple same-time values with differing provenance are ambiguous, not averaged away.
        if not candidates:
            return None
        newest = max(o.source_at for o in candidates)
        candidates = [o for o in candidates if o.source_at == newest]
        if len({(o.value, o.units, o.scope) for o in candidates}) != 1:
            return None
        return sorted(candidates, key=lambda o: o.id)[0]

    def feature(name, inputs, formula, units, reason, *, scope="global"):
        inputs = tuple(inputs)
        valid = bool(inputs) and all(o is not None and o.value is not None for o in inputs)
        if valid and max(o.source_at for o in inputs) - min(o.source_at for o in inputs) > timedelta(seconds=300):
            valid = False
        value = formula(*(o.value for o in inputs)) if valid else None
        return Feature(name=name, value=value, units=units,
            state=S.VERIFIED if valid and value is not None else S.MISSING,
            evidence_ids=tuple(o.id for o in inputs if o is not None),
            input_timestamps=tuple(o.source_at for o in inputs if o is not None),
            acquired_timestamps=tuple(o.acquired_at for o in inputs if o is not None),
            reason=reason if valid else "N/A: required equivalent fresh causal inputs unavailable or misaligned",
            scope=scope, cmc_id=cmc_id, instrument_key=instrument_key,
            decision_relevance="experimental policy evidence; separate owner activation required")

    market = select("market_cap_change_24h", scope="global")
    basis = select("basis", contract=True)
    funding = select("funding_rate", contract=True)
    oi = select("open_interest", contract=True)
    result = [
        feature("CmcGlobalMarketRisk", (market,), lambda x: -x, "percent", "negative 24h total market-cap change"),
        feature("CmcBtcDominanceContext", (select("btc_dominance", scope="global"),), lambda x:x, "percent", "BTC share of market cap"),
        feature("CmcAltcoinRotation", (select("altcoin_index", scope="global"),), lambda x:x/100, "fraction", "CMC altcoin-season index / 100"),
        feature("CmcBasisContext", (basis,), lambda x:x, "fraction", "(reported contract price - index) / index", scope="contract"),
        feature("CmcFundingContext", (funding,), lambda x:x, "fraction/interval", "verified per-interval funding; not annualized", scope="contract"),
        feature("CmcOpenInterestContext", (oi,), lambda x:x, oi.units if oi else "N/A", "exact perpetual OI; no aggregate substitution", scope="contract"),
        feature("CmcMarketCapRank", (select("market_cap_rank", asset=True),), lambda x:x, "rank", "CMC rank at observed timestamp", scope="asset"),
    ]
    # Leverage context is turnover-normalized exchange OI, not an estimate of traders' leverage.
    exchange_id = oi.exchange_id if oi else None
    exchange_oi = [o for o in causal if o.metric == "open_interest_usd" and o.scope == "exchange_all_contracts" and o.exchange_id == exchange_id]
    exchange_volume = [o for o in causal if o.metric == "derivatives_volume_24h" and o.scope == "exchange_all_contracts" and o.exchange_id == exchange_id]
    newest = lambda xs: max(xs, key=lambda o:(o.source_at,o.id)) if xs else None
    result.append(feature("CmcDerivativesLeverage", (newest(exchange_oi), newest(exchange_volume)),
        lambda a,b: a/b if b > 0 else None, "OI/daily_volume", "exchange aggregate OI / 24h derivatives volume", scope="exchange_all_contracts"))
    rates = [o for o in causal if o.metric == "funding_rate" and cmc_id is not None and o.cmc_id == cmc_id
             and o.scope == "contract" and o.instrument is not None and o.instrument.quote == "USDT"
             and funding is not None and o.funding_interval_seconds == funding.funding_interval_seconds
             and o.rate_semantics == funding.rate_semantics and o.units == funding.units]
    by_exchange = {}
    for rate in sorted(rates, key=lambda o:(o.source_at,o.acquired_at,o.id)):
        by_exchange[rate.exchange_id] = rate
    inputs = tuple(by_exchange.values()) if len(by_exchange) >= 2 else (None,)
    result.append(feature("CmcFundingDispersion", inputs, lambda *xs:max(xs)-min(xs), "fraction/interval", "range of equivalent USDT perpetual funding across at least two exchanges", scope="cross_exchange"))
    liq_1h = select("liquidation_total_1h", scope="global_all_contracts")
    liq_24h = select("liquidation_total_24h", scope="global_all_contracts")
    long = select("liquidation_long_1h", scope="global_all_contracts")
    short = select("liquidation_short_1h", scope="global_all_contracts")
    result.extend((
        feature("CmcLiquidationPressure", (liq_1h,liq_24h), lambda a,b:a*24/b if b>0 else None,
                "hourly_vs_daily_rate", "24 * global rolling 1h liquidations / rolling 24h liquidations", scope="global_all_contracts"),
        feature("CmcLiquidationImbalance", (long,short), lambda a,b:(a-b)/(a+b) if a+b>0 else None,
                "fraction", "(long - short) / total; aggregate stress, never a local sweep", scope="global_all_contracts"),
    ))
    memberships = [o for o in causal if o.metric.startswith("sector_member:") and cmc_id is not None and o.cmc_id == cmc_id]
    sectors = sorted({o.scope for o in memberships})
    sector_input = select("sector_return_24h:" + sectors[0].split(":",1)[1], scope=sectors[0]) if len(sectors) == 1 else None
    membership = newest([o for o in memberships if o.scope == sectors[0]]) if len(sectors) == 1 else None
    result.append(feature("CmcSectorRelativeStrength", (sector_input, market, membership), lambda a,b,c:a-b,
        "percentage_points", "unambiguous observed sector 24h return minus total market-cap change; membership must be causal", scope="sector"))
    covered = sum(f.state == S.VERIFIED for f in result)
    refs = tuple(o.id for o in causal)
    result.append(Feature(name="CmcDataConfidence", value=Decimal(covered)/len(result) if refs else None,
        units="coverage_fraction", state=S.VERIFIED if refs else S.MISSING, evidence_ids=refs,
        input_timestamps=tuple(o.source_at for o in causal), acquired_timestamps=tuple(o.acquired_at for o in causal),
        reason="verified feature coverage; not a profitability probability", cmc_id=cmc_id, instrument_key=instrument_key))
    return tuple(result)


def regime_compatibility(risk: Feature, *, regime_state: str, direction: str,
                         regime_at: datetime, regime_ref: str, as_of: datetime) -> Feature:
    """Research-only directional compatibility with an already-computed CCI regime."""
    valid=(risk.state==S.VERIFIED and risk.value is not None and risk.input_timestamps
        and regime_state in {"RISK_OFF","RISK_ON","TREND_EXPANSION","TREND_PULLBACK","RANGE_COMPRESSION",
                             "HIGH_VOLATILITY","LOW_VOLATILITY","CHOP","MIXED","TRANSITION"}
        and direction in {"long","short"} and regime_at.tzinfo is not None and regime_at<=as_of
        and (as_of-regime_at).total_seconds()<=300 and regime_ref)
    value=None
    if valid:
        value=Decimal(0)
        if regime_state=="RISK_OFF" and risk.value>3:
            value=Decimal(-1 if direction=="long" else 1)
        elif regime_state=="RISK_ON" and risk.value<-3:
            value=Decimal(1 if direction=="long" else -1)
    return Feature(name="CmcRegimeCompatibility",value=value,units="directional_classification",
        state=S.VERIFIED if valid else S.MISSING,evidence_ids=(*risk.evidence_ids,regime_ref) if valid else (),
        input_timestamps=(*risk.input_timestamps,regime_at) if valid else (),
        acquired_timestamps=(*risk.acquired_timestamps,regime_at) if valid else (),
        reason="Direction compatibility for CCI RISK_ON/OFF plus >3% matching independent market-cap movement; 0 is neutral",
        decision_relevance="research only; never changes CCI regime or lifecycle")
