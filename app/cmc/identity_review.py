"""Offline exact-identity proposal from hash-checked captures, never owner approval."""
import json
from datetime import timedelta
from pathlib import Path

from app.cmc.authenticated_validation import analyze_capture, load_sources
from app.cmc.collection import IdentityManifest
from app.cmc.identity import AssetBinding, binding_status
from app.cmc.models import utc


def review_universe(metadata, *, map_pages, pair_pages, as_of, bindings=()):
    """Research candidates only. No identity or semantic approval is performed."""
    from app.cmc.acquisition import validate_family
    from app.cmc.endpoints import Endpoint, rows
    if as_of.tzinfo is None or not 1 <= len(metadata) <= 100:
        raise ValueError("bounded timezone-aware universe required")
    symbols = [m.get("symbol") for m in metadata]
    if any(not isinstance(s, str) or not s for s in symbols) or len(set(symbols)) != len(symbols):
        raise ValueError("duplicate or invalid native contract identity")
    complete = (all(f.endpoint == Endpoint.MAP for f in map_pages)
        and all(f.endpoint == Endpoint.EXCHANGE_PAIRS for f in pair_pages)
        and validate_family(map_pages) and validate_family(pair_pages))
    fresh = complete and all(f.acquired_at.tzinfo is not None and
        0 <= (as_of-f.acquired_at).total_seconds() <= 86400 for f in (*map_pages, *pair_pages))
    assets = {a["id"]:a for f in map_pages for a in rows(f.endpoint, f.data)} if fresh else {}
    pairs = [p for f in pair_pages for p in rows(f.endpoint, f.data)] if fresh else []
    result = []
    for native in metadata:
        symbol, base = native["symbol"], native.get("baseAsset")
        reasons = []
        if not complete: reasons.append("partial_or_ambiguous_shared_acquisition")
        elif not fresh: reasons.append("identity_sources_stale_or_future")
        if (native.get("contractType") != "PERPETUAL" or native.get("quoteAsset") != "USDT"
                or native.get("marginAsset") != "USDT" or native.get("status") != "TRADING"
                or not isinstance(base, str) or symbol != base+"USDT"):
            reasons.append("native_perpetual_identity_unverified")
        matches = [p for p in pairs if p.get("market_pair_base", {}).get("exchange_symbol") == base
            and p.get("market_pair_quote", {}).get("exchange_symbol") == "USDT"
            and p.get("market_pair_quote", {}).get("crypto_id") == 825 and p.get("category") == "perpetual"]
        candidate = None
        if len(matches) != 1:
            reasons.append("no_exact_pair" if not matches else "ambiguous_pair_collision")
        else:
            pair = matches[0]
            asset = assets.get(pair["market_pair_base"].get("crypto_id"), {})
            if not asset.get("slug") or not asset.get("is_active"):
                reasons.append("numeric_id_or_exact_slug_unverified")
            else:
                candidate = {"cmc_id":asset["id"], "cmc_slug":asset["slug"],
                    "market_id":pair["market_id"], "native_symbol":symbol, "native_base":base,
                    "provider_exclusions":pair.get("exclusions"),
                    "provider_source_times":[q.get("last_updated") for q in pair.get("exchange_reported_quotes",[])]}
            if pair.get("outlier_detected") is not False or pair.get("exclusions"):
                reasons.append("provider_exclusions_or_outlier")
        if symbol in {"1000LUNCUSDT", "1000PEPEUSDT", "1000SHIBUSDT"} or any(
                native.get(k, 1) != 1 for k in ("contractSize", "contractMultiplier")):
            reasons.append("multiplier_equivalence_unverified")
        if symbol == "ARBUSDT": reasons.append("ARB_exclusion_requires_separate_review")
        if symbol == "币安人生USDT": reasons.append("Unicode_exact_identity_requires_separate_review")
        current = [b for b in bindings if b.contract_symbol == symbol]
        state = binding_status(current[0], as_of).value if len(current) == 1 else "UNAPPROVED"
        result.append({"symbol":symbol, "native_identity":native, "candidate":candidate,
            "binding_state":state, "approved":False,
            "withholding_reasons":reasons or ["owner_exact_contract_approval_pending"],
            "source_evidence":[{"endpoint":f.endpoint.value,"raw_ref":f.raw_ref,
                "acquired_at":f.acquired_at.isoformat()} for f in (*map_pages, *pair_pages)],
            "required_approval":"Exact CMC ID/slug, native Binance perpetual, exchange/market IDs, expiry and owner reference",
            "semantic_verification":{"oi_counting_convention":"Unverified", "funding_interval":"Unverified",
                "funding_semantics":"Unverified", "index_currency":"Unverified"}})
    return result


def propose_identities(root: Path, *, as_of):
    review = analyze_capture(root, root / "bindings.reviewed.json")
    _, sources = load_sources(root)
    cutoff = utc(review["validation_cutoff"])
    if as_of.tzinfo is None or cutoff > as_of:
        raise ValueError("proposal requires causal timezone-aware evidence")
    assets = {b["cmc_id"]:(b["cmc_slug"], b["binance_base"]) for b in
        json.loads((root / "bindings.reviewed.json").read_text(encoding="utf-8"))["bindings"]}
    bindings, evidence = [], []
    for row in review["mappings"]:
        instrument = row.get("instrument")
        if row["state"] != "VERIFIED" or not instrument:
            continue
        asset_id = instrument["cmc_id"]
        if asset_id not in assets or row["cross_endpoint_market_ids"] != [instrument["market_id"]]:
            continue
        slug, base = assets[asset_id]
        refs = ["sha256:"+r["sha256"] for r, _ in sources if r.get("sha256") and r["endpoint"] in {
            "/v1/cryptocurrency/map", "/v5/exchange/derivatives/list", "/fapi/v1/exchangeInfo",
            "/v5/exchange/derivatives/market-pairs/list/latest",
            "/v5/cryptocurrency/derivatives/market-pairs/list/latest"}]
        binding = AssetBinding(cmc_id=asset_id, cmc_slug=slug, binance_base=base,
            contract_symbol=instrument["symbol"], exchange_id=instrument["exchange_id"],
            market_id=instrument["market_id"], evidence_ref=";".join(sorted(set(refs))),
            acquired_at=cutoff, reviewed_at=None, approved_at=None,
            expires_at=cutoff+timedelta(days=7), approved=False)
        bindings.append(binding)
        evidence.append({"symbol":instrument["symbol"], "historical_evidence_status":"EVIDENCE_VERIFIED",
            "authorization_status":binding_status(binding, as_of).value,
            "evidence_cutoff":cutoff.isoformat(), "review_at":"N/A: pending", "provenance_refs":sorted(set(refs)),
            "owner_approval":"N/A", "current_contract_metadata":"N/A: reacquire before rollout"})
    return IdentityManifest(bindings=tuple(bindings)), evidence
