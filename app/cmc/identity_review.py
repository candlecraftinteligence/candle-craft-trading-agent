"""Offline exact-identity proposal from hash-checked captures, never owner approval."""
import json
from datetime import timedelta
from pathlib import Path

from app.cmc.authenticated_validation import analyze_capture, load_sources
from app.cmc.collection import IdentityManifest
from app.cmc.identity import AssetBinding, binding_status
from app.cmc.models import utc


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
