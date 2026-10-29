"""CMC rankings adapted to the existing conservative U100 qualification contract.

Only reviewed numeric-ID bindings can join Binance instruments. A ticker match,
sector tag, or a recently acquired stale response never establishes membership.
This module does not generate setups or activate CMC derivatives decisions.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from app.cmc.client import CmcClient, Fetch
from app.cmc.collection import IdentityManifest
from app.cmc.endpoints import Endpoint
from app.cmc.identity import IdentityStatus, binding_status
from app.cmc.models import utc
from app.universe.canonical_qualification import EXCLUSION_CATEGORIES, VerifiedContractMapping
from app.universe.symbol_universe import UniverseResolutionError

MAX_RANKING_AGE = timedelta(hours=1)
# A 500-row snapshot costs up to two credits. Reuse it for 30 minutes: 96
# credits/day and at most 2,976 in 31 days, within the existing 100/3,000 guard.
# Original source/acquisition times and current binding approval still govern.
RANKING_REFRESH_INTERVAL = timedelta(minutes=30)
# These describe representations, not protocol sectors (e.g. liquid-staking).
REPRESENTATIONS = {
    "stablecoin": "stablecoin", "asset-backed-stablecoin": "stablecoin",
    "wrapped-tokens": "wrapped_or_bridged", "wrapped-token": "wrapped_or_bridged",
    "liquid-staking-token": "liquid_staked_or_restaked",
    "liquid-restaking-token": "liquid_staked_or_restaked",
    "leveraged-token": "leveraged_product", "tokenized-stock": "tokenized_security",
}


@dataclass(frozen=True)
class RankingSnapshot:
    assets: tuple[Mapping[str, Any], ...]
    mappings: Mapping[str, VerifiedContractMapping]
    acquired_at: datetime
    source_at: datetime
    raw_ref: str


def normalize_rankings(fetch: Fetch, manifest: IdentityManifest, *, as_of: datetime) -> RankingSnapshot:
    """Normalize v3 list quotes and v1/v2 USD objects without inventing identity."""
    def fail(reason: str):
        raise UniverseResolutionError(f"universe_error: CMC market-cap source {reason}")

    if fetch.error:
        fail(f"unavailable ({fetch.error})")
    if fetch.endpoint != Endpoint.LISTINGS or as_of.tzinfo is None:
        fail("returned invalid endpoint or decision time")
    source = utc((fetch.raw or {}).get("status", {}).get("timestamp"))
    if source is None or not source <= fetch.acquired_at <= as_of:
        fail("returned missing or future source/acquisition time")
    if as_of - source > MAX_RANKING_AGE or as_of - fetch.acquired_at > MAX_RANKING_AGE:
        fail("returned stale ranking")
    if not isinstance(fetch.data, list) or not fetch.data or len(fetch.data) > 500:
        fail("returned malformed, empty, or oversized ranking")

    bindings = {b.cmc_id: b for b in manifest.bindings
                if binding_status(b, as_of) == IdentityStatus.OWNER_APPROVED}
    assets, mappings = [], {}
    for row in fetch.data:
        if not isinstance(row, Mapping):
            fail("returned malformed asset")
        cmc_id = row.get("id")
        if type(cmc_id) is not int or cmc_id <= 0:
            fail("returned invalid numeric identity")
        provider_id = f"cmc:{cmc_id}"
        quote = row.get("quote")
        if isinstance(quote, list):
            usd = [q for q in quote if isinstance(q, Mapping) and q.get("symbol") == "USD" and q.get("id") == 2781]
            if len(usd) != 1:
                fail("returned ambiguous USD quote")
            usd = usd[0]
        elif isinstance(quote, Mapping) and isinstance(quote.get("USD"), Mapping):
            usd = quote["USD"]
        else:
            fail("returned missing USD quote")
        updated = utc(row.get("last_updated"))
        quote_updated = utc(usd.get("last_updated"))
        if updated is None or quote_updated is None or max(updated, quote_updated) > as_of:
            fail("returned missing or future asset time")
        # Keep the older source time. Fresh acquisition cannot refresh old quotes.
        updated = min(updated, quote_updated, source)
        tags = row.get("tags", [])
        if not isinstance(tags, list):
            fail("returned malformed classification metadata")
        represented = {REPRESENTATIONS[tag] for tag in tags if isinstance(tag, str) and tag in REPRESENTATIONS}
        category = next(iter(represented)) if len(represented) == 1 else None
        binding = bindings.get(cmc_id)
        symbol = row.get("symbol")
        if binding is not None and row.get("slug") == binding.cmc_slug and isinstance(symbol, str):
            reviewed = binding.qualification_class
            if reviewed in {"genuine", *EXCLUSION_CATEGORIES}:
                category = reviewed if not represented or represented == {reviewed} else None
            base = symbol if binding.contract_multiplier == 1 else f"{binding.contract_multiplier}{symbol}"
            if base == binding.binance_base and category == "genuine":
                mappings[provider_id] = VerifiedContractMapping(
                    provider_id, symbol, binding.contract_symbol, base, binding.contract_multiplier)
        # A conflicting representation is withheld, even when a reviewed binding exists.
        assets.append({"id": provider_id, "symbol": symbol, "name": row.get("name"),
                       "rank": row.get("cmc_rank"), "last_updated": updated.isoformat(),
                       "quotes": {"USD": {"market_cap": usd.get("market_cap")}},
                       **({"qualification_class": category} if category else {})})
    return RankingSnapshot(tuple(assets), mappings, fetch.acquired_at, source, fetch.raw_ref)


async def fetch_rankings(*, as_of: datetime | None = None) -> RankingSnapshot:
    from app.cmc.runtime import service_for
    from app.core.config import get_settings

    service = await service_for(get_settings(), discovery=True)
    if service is None:
        raise UniverseResolutionError("universe_error: CMC market-cap service unavailable")
    decision = as_of or datetime.now(UTC)
    if decision.tzinfo is None:
        raise UniverseResolutionError("universe_error: CMC market-cap source invalid decision time")
    previous = getattr(service, "universe_fetch", None)
    if previous is not None:
        source = utc((previous.raw or {}).get("status", {}).get("timestamp"))
        if (source is not None and source <= previous.acquired_at <= decision
                and decision-previous.acquired_at < RANKING_REFRESH_INTERVAL
                and decision-source <= MAX_RANKING_AGE):
            # Re-normalize to recheck binding expiry/approval at every delivery.
            return normalize_rankings(previous, service.collector.manifest, as_of=decision)
    client: CmcClient = service.collector.client
    # One bounded bulk request; enough headroom for excluded representations.
    # Incomplete qualification still fails closed. Never fetch farther to backfill.
    fetch = await client.get(Endpoint.LISTINGS, {"start": 1, "limit": 500,
                            "convert": "USD", "sort": "market_cap", "sort_dir": "desc"})
    snapshot = normalize_rankings(fetch, service.collector.manifest, as_of=decision if as_of else datetime.now(UTC))
    service.universe_fetch = fetch
    return snapshot
