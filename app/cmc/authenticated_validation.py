"""Offline analysis of bounded, hash-verified authenticated validation captures."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.cmc.client import Fetch
from app.cmc.comparison import compare
from app.cmc.endpoints import Endpoint
from app.cmc.identity import AssetBinding, match_instrument
from app.cmc.models import EvidenceState as S, Observation, number, stable_id, utc
from app.cmc.native import native_observations
from app.cmc.normalize import normalize_context, normalize_pair
from app.data.derivatives_source_evidence import acquisition_fact, describe_binance_open_interest, describe_binance_funding_history


def load_sources(root: Path):
    root = root.resolve()
    index_path = root / "index.json"
    if index_path.stat().st_size > 512 * 1024:
        raise ValueError("capture index budget exceeded")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    records = index.get("sources", [])
    if not records or len(records) > 128:
        raise ValueError("invalid source count")
    sources = []
    for record in records:
        at = utc(record.get("acquired_at"))
        if at is None:
            raise ValueError("source acquisition timestamp missing")
        data = None
        if record.get("file"):
            path = (root / record["file"]).resolve()
            if path.parent != root or path.suffix != ".json" or path.stat().st_size > 8 * 1024 * 1024:
                raise ValueError("invalid captured source path/size")
            body = path.read_bytes()
            if hashlib.sha256(body).hexdigest() != record.get("sha256"):
                raise ValueError("captured source hash mismatch")
            data = json.loads(body, parse_float=Decimal)
        sources.append((record, data))
    return index, sources


def as_fetch(record, payload):
    status = payload.get("status") if isinstance(payload, dict) else None
    error = record.get("error")
    if not error and (not isinstance(status, dict) or str(status.get("error_code")) != "0"):
        error = "captured_envelope_contract"
    return Fetch(endpoint=Endpoint(record["endpoint"]), acquired_at=utc(record["acquired_at"]),
        state=S.ERROR if error else S.UNVERIFIED, error=error,
        data=payload.get("data") if isinstance(payload, dict) else None,
        raw=payload, raw_ref=record.get("sha256", "N/A"),
        authenticated=record.get("authenticated", False), request_params=record.get("params", {}))


def _latest(sources, provider, endpoint, symbol=None):
    items = [(r, p) for r, p in sources if r["provider"] == provider and r["endpoint"] == endpoint
             and p is not None and not r.get("error") and (symbol is None or r.get("params", {}).get("symbol") == symbol)]
    return max(items, key=lambda x: utc(x[0]["acquired_at"])) if items else (None, None)


def _native_time(value):
    parsed = number(value)
    try:
        return datetime.fromtimestamp(float(parsed / 1000), UTC) if parsed is not None else None
    except (ValueError, OverflowError, OSError):
        return None


def _observation_record(value, as_of):
    result = value.model_dump(mode="json")
    result["state_at_validation_cutoff"] = value.status_at(as_of).value
    return result


def analyze_capture(root: Path, binding_path: Path):
    index, sources = load_sources(root)
    if binding_path.stat().st_size > 128 * 1024:
        raise ValueError("binding review budget exceeded")
    review = json.loads(binding_path.read_text(encoding="utf-8"))
    if review.get("validation_only") is not True:
        raise ValueError("validation-only identity review required")
    bindings = tuple(AssetBinding.model_validate(b) for b in review.get("bindings", []))
    if not 1 <= len(bindings) <= 10 or len({b.cmc_id for b in bindings}) != len(bindings):
        raise ValueError("invalid reviewed binding set")
    cutoff = max(utc(r["acquired_at"]) for r, _ in sources)
    map_r, map_p = _latest(sources, "CoinMarketCap", Endpoint.MAP.value)
    registry_r, registry_p = _latest(sources, "CoinMarketCap", Endpoint.EXCHANGES.value)
    pairs_r, pairs_p = _latest(sources, "CoinMarketCap", Endpoint.EXCHANGE_PAIRS.value)
    meta_r, meta_p = _latest(sources, "Binance", "/fapi/v1/exchangeInfo")
    if any(r is None for r in (map_r, registry_r, pairs_r, meta_r)):
        raise ValueError("identity source set incomplete")
    assets = map_p["data"]
    registries = [r for r in registry_p["data"].get("exchanges", []) if r.get("exchange_slug") == "binance"]
    if len(registries) != 1:
        raise ValueError("Binance exchange registry ambiguous")
    exchange = {k: pairs_p["data"].get(k) for k in ("exchange_id", "exchange_slug")}
    pair_fetch = as_fetch(pairs_r, pairs_p)
    contexts, context_sources = [], []
    for record, payload in sources:
        if record["provider"] == "CoinMarketCap" and payload is not None:
            fetch = as_fetch(record, payload)
            values = normalize_context(fetch)
            contexts.extend(values)
            context_sources.append({"endpoint": record["endpoint"], "raw_ref": record.get("sha256"),
                "observations": [_observation_record(o, cutoff) for o in values]})
    mappings, comparisons, derivatives = [], [], []
    for binding in bindings:
        asset_rows = [a for a in assets if a.get("id") == binding.cmc_id and a.get("slug") == binding.cmc_slug]
        pair_rows = [p for p in pairs_p["data"].get("market_pairs", [])
            if p.get("market_pair_base", {}).get("crypto_id") == binding.cmc_id
            and p.get("market_pair_quote", {}).get("crypto_id") == binding.quote_id and p.get("category") == "perpetual"]
        metadata = [m for m in meta_p.get("symbols", []) if m.get("symbol") == binding.binance_base + "USDT"]
        if len(asset_rows) != 1 or len(pair_rows) != 1 or len(metadata) != 1:
            mappings.append({"cmc_id": binding.cmc_id, "state": "AMBIGUOUS", "reason": "non-unique asset/pair/native metadata"})
            continue
        pair = pair_rows[0]
        mapped = match_instrument(pair, exchange, metadata[0], binding, asset_rows[0], as_of=cutoff,
            metadata_at=utc(meta_r["acquired_at"]), acquired_at=cutoff,
            map_acquired_at=utc(map_r["acquired_at"]), exchange_registry=registries[0],
            exchange_registry_acquired_at=utc(registry_r["acquired_at"]))
        mapping = mapped.model_dump(mode="json")
        mapping["symbol_collisions_in_map"] = sum(a.get("symbol") == asset_rows[0].get("symbol") for a in assets)
        cross = [(r, p) for r, p in sources if r["provider"] == "CoinMarketCap"
                 and r["endpoint"] == Endpoint.CRYPTO_PAIRS.value and p is not None
                 and r.get("params", {}).get("crypto_id") == binding.cmc_id]
        mapping["cross_endpoint_market_ids"] = sorted({p.get("market_id") for _, payload in cross
            for p in payload["data"].get("market_pairs", [])
            if p.get("exchange", {}).get("exchange_id") == exchange["exchange_id"]
            and p.get("market_pair_quote", {}).get("crypto_id") == binding.quote_id})
        mappings.append(mapping)
        i = mapped.instrument
        if i is None:
            continue
        # Official schema establishes conversion-currency notional, but neither
        # counting convention nor funding interval/kind/index currency. Do not infer them.
        observations = normalize_pair(pair_fetch, pair, i, oi_units="USD")
        native = []
        for endpoint, label, describe in (("/fapi/v1/openInterest", "open_interest", describe_binance_open_interest),
                ("/fapi/v1/fundingRate", "funding_rate", describe_binance_funding_history)):
            record, payload = _latest(sources, "Binance", endpoint, i.symbol)
            if record:
                batch = describe(requested_symbol=i.symbol, payload=payload,
                    request_params=record.get("params", {}), capture_clock=None,
                    acquisition=acquisition_fact(utc(record["acquired_at"]), quality="verified"))
                native.extend(native_observations({label: batch}, i))
        price_r, price_p = _latest(sources, "Binance", "/fapi/v2/ticker/price", i.symbol)
        if price_r and price_p.get("symbol") == i.symbol:
            value = number(price_p.get("price"))
            fields = dict(provider="Binance", endpoint=price_r["endpoint"], metric="price", value=value,
                raw_value=str(price_p.get("price")), units="USDT", native_units="USDT",
                source_at=_native_time(price_p.get("time")), acquired_at=utc(price_r["acquired_at"]),
                state=S.VERIFIED if value is not None and value > 0 else S.UNVERIFIED,
                instrument=i, cmc_id=i.cmc_id, exchange_id=i.exchange_id, scope="contract", provenance=price_r["sha256"])
            native.append(Observation(id=stable_id(fields), **fields))
        for left in observations:
            right_rows = [n for n in native if n.metric == left.metric]
            if not right_rows:
                continue
            right = max(right_rows, key=lambda n: n.source_at or n.acquired_at)
            comparisons.append({"symbol": i.symbol, "metric": left.metric,
                "classification": compare(left, right, as_of=cutoff).value,
                "cmc_state": left.status_at(cutoff).value, "native_state": right.status_at(cutoff).value,
                "cmc_units": left.units, "native_units": right.units,
                "source_skew_seconds": str(abs((left.source_at-right.source_at).total_seconds())) if left.source_at and right.source_at else None,
                "cmc_evidence": left.id, "native_evidence": right.id})
        info_r, info_p = _latest(sources, "Binance", "/fapi/v1/fundingInfo")
        infos = [row for row in (info_p or []) if row.get("symbol") == i.symbol]
        q = pair.get("exchange_reported_quotes", [{}])[0]
        normalized_quotes = [row for row in pair.get("quotes", []) if row.get("convert_id") == 2781]
        normalized_price = number(normalized_quotes[0].get("price")) if len(normalized_quotes) == 1 else None
        index_price = number(q.get("index_price"))
        alternative = (normalized_price-index_price)/index_price if normalized_price is not None and index_price and index_price > 0 else None
        derivatives.append({"symbol": i.symbol, "market_id": i.market_id,
            "observations": [_observation_record(o, cutoff) for o in observations],
            "native_observations": [_observation_record(o, cutoff) for o in native],
            "native_funding_interval_hours_reported": infos[0].get("fundingIntervalHours") if len(infos) == 1 else None,
            "native_interval_provider_timestamp": infos[0].get("updateTime") if len(infos) == 1 else None,
            "native_interval_acquired_at": info_r.get("acquired_at") if info_r else None,
            "cmc_funding_interval_seconds": None,
            "basis_diagnostics": {"provider_raw_basis": q.get("index_basis"),
                "normalized_price_minus_index_fraction": str(alternative) if alternative is not None else None,
                "verified_formula_or_sign": False,
                "reason": "Reported and normalized prices differ; index currency and provider basis convention are not established."}})
    routes = {}
    for record, payload in sources:
        if record["provider"] != "CoinMarketCap":
            continue
        routes.setdefault(record["endpoint"], []).append(record)
    observations = [o for row in derivatives for o in row["observations"]]
    return {"schema_version": "cmc-authenticated-validation/1", "validation_only": True,
        "validation_cutoff": cutoff.isoformat(), "authenticated_transport_verified": all(
            r.get("authenticated") and not r.get("error") for records in routes.values() for r in records),
        "routes": {endpoint: {"requests": len(records), "errors": [r["error"] for r in records if r.get("error")],
            "reported_credits": sum(r.get("credits_reported") or 0 for r in records)} for endpoint, records in sorted(routes.items())},
        "distinct_authenticated_routes": len(routes), "raw_source_count": sum(bool(r.get("file")) for r, _ in sources),
        "source_refs": [{k: r[k] for k in ("provider", "endpoint", "params", "acquired_at", "sha256", "original_raw_sha256", "representation", "error") if k in r} for r, _ in sources],
        "mappings": mappings, "derivatives": derivatives, "comparisons": comparisons,
        "context_sources": context_sources, "derivatives_state_counts": dict(Counter(o["state_at_validation_cutoff"] for o in observations)),
        "comparison_counts": dict(Counter(c["classification"] for c in comparisons)),
        "usage": index.get("usage", {}), "reported_validation_credits": sum(r.get("credits_reported") or 0 for r, _ in sources),
        "configured_budget": index.get("configured_budget", {}),
        "validation_credit_cap": index.get("validation_credit_cap"),
        "prior_probe_reserved": index.get("prior_probe_reserved", {}),
        "quota_observations": [r for r, _ in sources if r["endpoint"] == Endpoint.KEY_INFO.value],
        "production_rules_activated": False, "scanner_started": False, "trading_decisions_modified": False,
        "empirical_expectancy_validated": False}
