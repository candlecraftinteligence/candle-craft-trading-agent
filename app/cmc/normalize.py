"""Endpoint-specific semantics. Unspecified OI units / funding intervals stay Unverified."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from app.cmc.client import Fetch
from app.cmc.endpoints import Endpoint
from app.cmc.models import EvidenceState as S, Instrument, Observation, number, stable_id, utc


def observation(fetch: Fetch, metric: str, raw, *, units: str, source=None, scope="global",
                cmc_id=None, exchange_id=None, instrument=None, verified=True, reason="",
                window_seconds=None, funding_interval_seconds=None, rate_semantics=None,
                counting_convention=None, max_age=300) -> Observation:
    value = number(raw)
    source_at = utc(source)
    state = S.VERIFIED if verified else S.UNVERIFIED
    if fetch.error:
        state = fetch.state
    elif raw is None:
        state = S.MISSING
    elif value is None:
        state = S.ERROR
    elif source_at is None or source_at > fetch.acquired_at:
        state = S.UNVERIFIED
        reason = reason or "missing/invalid/future provider metric timestamp"
    elif (fetch.acquired_at - source_at).total_seconds() > max_age:
        state = S.STALE
    if value is not None and (metric.startswith(("open_interest", "liquidation", "market_cap", "price", "index_price")) and value < 0):
        state, reason = S.ERROR, "negative monetary/position value"
    if value is not None and metric in {"btc_dominance", "eth_dominance", "fear_greed", "altcoin_index"} and not 0 <= value <= 100:
        state, reason = S.ERROR, "percentage/index outside 0..100"
    if value is not None and (metric in {"price","index_price"} and value<=0 or
            metric=="market_cap_rank" and (value<=0 or value!=value.to_integral_value())):
        state, reason = S.ERROR, "invalid price or market-cap rank"
    fields = dict(endpoint=fetch.endpoint.value, metric=metric, raw_value=None if raw is None else str(raw),
        raw_type=type(raw).__name__, value=value,
        units=units, native_units=units, source_at=source_at, acquired_at=fetch.acquired_at,
        state=state, reason=reason, cmc_id=cmc_id, exchange_id=exchange_id, instrument=instrument,
        scope=scope, provenance=fetch.raw_ref, window_seconds=window_seconds,
        funding_interval_seconds=funding_interval_seconds, max_age_seconds=max_age)
    fields.update(rate_semantics=rate_semantics, counting_convention=counting_convention)
    return Observation(id=stable_id(fields), **fields)


def _usd(quotes):
    return [q for q in quotes if isinstance(q, dict) and
            (q.get("convert_symbol") == "USD" or q.get("symbol") == "USD") and
            (q.get("convert_id", q.get("crypto_id")) == 2781)]


def normalize_pair(fetch: Fetch, pair: dict, instrument: Instrument | None = None,
                   *, oi_units: str | None = None, funding_interval_seconds: int | None = None,
                   funding_semantics: str | None = None, oi_counting_convention: str | None = None):
    quotes = _usd(pair.get("exchange_reported_quotes", []))
    if len(quotes) != 1:
        result = observation(fetch, "pair_quote", None, units="N/A", verified=False,
                            reason="missing or ambiguous USD exchange-reported quote")
        return (result.model_copy(update={"state":S.AMBIGUOUS if quotes else S.MISSING}),)
    q = quotes[0]
    identity = dict(cmc_id=pair.get("market_pair_base", {}).get("crypto_id"),
                    instrument=instrument, exchange_id=instrument.exchange_id if instrument else None,
                    scope="contract", source=q.get("last_updated"))
    trusted = instrument is not None and pair.get("outlier_detected") is False and not pair.get("exclusions")
    result = []
    for metric, units, verified in (
        ("price", "USD", trusted), ("index_price", "USD", trusted),
        ("open_interest", oi_units or "unspecified", trusted and oi_units in {"base", "USD"} and oi_counting_convention == "open_contract_quantity"),
        ("funding_rate", "fraction/interval", trusted and funding_interval_seconds is not None and funding_semantics in {"settled_funding", "indicative_funding"}),
        ("provider_index_basis", "unspecified", False),
    ):
        key = "index_basis" if metric == "provider_index_basis" else metric
        result.append(observation(fetch, metric, q.get(key), units=units, verified=verified,
            reason="" if verified else "identity/units/interval not independently verified",
            funding_interval_seconds=funding_interval_seconds if metric == "funding_rate" else None,
            rate_semantics=funding_semantics if metric == "funding_rate" else None,
            counting_convention=oi_counting_convention if metric == "open_interest" else None, **identity))
    price, index = number(q.get("price")), number(q.get("index_price"))
    basis = (price - index) / index if price is not None and index is not None and price > 0 and index > 0 else None
    result.append(observation(fetch, "basis", basis, units="fraction", verified=trusted,
        reason="(exchange reported price - index price) / index price; not annualized", **identity))
    return tuple(result)


def normalize_context(fetch: Fetch):
    try:
        return _normalize_context(fetch)
    except (AttributeError,TypeError,KeyError,ValueError):
        value = observation(fetch,"response_contract",None,units="N/A",verified=False,
                            reason="malformed endpoint response shape")
        return (value.model_copy(update={"state":S.ERROR}),)


def _normalize_context(fetch: Fetch):
    if fetch.error:
        return (observation(fetch, "provider_error", None, units="N/A", verified=False, reason=fetch.error),)
    data, result = fetch.data, []
    if fetch.endpoint == Endpoint.GLOBAL:
        for metric in ("btc_dominance", "eth_dominance"):
            result.append(observation(fetch, metric, data.get(metric), units="percent", source=data.get("last_updated")))
        q = data.get("quote", {}).get("USD", {})
        for metric, key, units in (("market_cap", "total_market_cap", "USD"),
                                   ("market_cap_change_24h", "total_market_cap_yesterday_percentage_change", "percent")):
            result.append(observation(fetch, metric, q.get(key), units=units, source=q.get("last_updated")))
    elif fetch.endpoint in {Endpoint.LISTINGS, Endpoint.HISTORICAL_LISTINGS}:
        for row in data:
            if fetch.endpoint == Endpoint.LISTINGS:
                quotes = row.get("quote", [])
                usd = [q for q in quotes if isinstance(q,dict) and q.get("id") == 2781 and q.get("symbol") == "USD"] if isinstance(quotes,list) else []
                q = usd[0] if len(usd) == 1 else {}
            else:
                q = row.get("quote", {}).get("USD", {})
            for metric, value, units in (("market_cap_rank", row.get("cmc_rank"), "rank"),
                    ("asset_return_24h", q.get("percent_change_24h"), "percent")):
                result.append(observation(fetch, metric, value, units=units, cmc_id=row.get("id"),
                    source=row.get("last_updated") if metric == "market_cap_rank" else q.get("last_updated"), scope="asset"))
    elif fetch.endpoint in {Endpoint.FEAR_GREED, Endpoint.ALTCOIN}:
        row = data[0] if isinstance(data, list) and len(data) == 1 else data
        if isinstance(row, dict):
            metric = "fear_greed" if fetch.endpoint == Endpoint.FEAR_GREED else "altcoin_index"
            result.append(observation(fetch, metric, row.get("value") if fetch.endpoint == Endpoint.FEAR_GREED else row.get("altcoin_index"), units="index/100",
                source=row.get("update_time", row.get("timestamp")) if fetch.endpoint == Endpoint.FEAR_GREED else row.get("snapshot_time"), max_age=86400))
    elif fetch.endpoint == Endpoint.EXCHANGES:
        for exchange in data.get("exchanges", []):
            for q in _usd(exchange.get("quotes", [])):
                for metric, key in (("open_interest_usd", "open_interest_usd"), ("derivatives_volume_24h", "derivative_volume_usd")):
                    result.append(observation(fetch, metric, q.get(key), units="USD", source=q.get("last_updated"),
                        scope="exchange_all_contracts", exchange_id=exchange.get("exchange_id")))
    elif fetch.endpoint in {Endpoint.LIQUIDATIONS, Endpoint.EXCHANGE_LIQUIDATIONS, Endpoint.CRYPTO_LIQUIDATIONS}:
        items = [data] if fetch.endpoint == Endpoint.LIQUIDATIONS else data.get(
            "exchanges" if fetch.endpoint == Endpoint.EXCHANGE_LIQUIDATIONS else "cryptocurrencies", [])
        scope = {Endpoint.LIQUIDATIONS:"global_all_contracts", Endpoint.EXCHANGE_LIQUIDATIONS:"exchange_all_contracts",
                 Endpoint.CRYPTO_LIQUIDATIONS:"asset_all_exchanges"}[fetch.endpoint]
        for row in items:
            for q in _usd(row.get("quotes", [])):
                for suffix, seconds in (("1h",3600), ("4h",14400), ("24h",86400)):
                    long, short, total = (number(q.get(f"{side}_liquidations_{suffix}")) for side in ("long", "short", "total"))
                    consistent = all(x is not None and x >= 0 for x in (long, short, total))
                    consistent = consistent and abs(long + short - total) <= max(Decimal("0.01"), total * Decimal("0.000001"))
                    for side, value in (("long",long), ("short",short), ("total",total)):
                        result.append(observation(fetch, f"liquidation_{side}_{suffix}", value, units="USD",
                            source=q.get("last_updated"), scope=scope, cmc_id=row.get("crypto_id"),
                            exchange_id=row.get("exchange_id"), window_seconds=seconds, verified=consistent,
                            reason="rolling aggregate; never proof of a local stop sweep"))
    elif fetch.endpoint == Endpoint.CATEGORIES:
        for row in data:
            result.append(observation(fetch, "sector_return_24h:" + str(row.get("id")), row.get("avg_price_change"),
                units="percent", source=row.get("last_updated"), scope="sector:" + str(row.get("id")), max_age=7200))
    elif fetch.endpoint == Endpoint.CATEGORY:
        # Membership itself is preserved in raw evidence; no timeless inferred membership.
        for row in data.get("coins", []):
            result.append(observation(fetch, "sector_member:" + str(data.get("id")), 1, units="membership",
                source=data.get("last_updated"), cmc_id=row.get("id"), scope="sector:" + str(data.get("id")), max_age=7200))
    return tuple(result)
