"""Shared bounded acquisition, independent of per-symbol critical processing."""
from __future__ import annotations

import asyncio
import json
from collections import OrderedDict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from app.cmc.client import CmcClient, Fetch
from app.cmc.endpoints import Endpoint, rows
from app.cmc.identity import AssetBinding, match_instrument
from app.cmc.models import EvidenceState as S, Frozen, Instrument, Snapshot, stable_id
from app.cmc.normalize import normalize_context, normalize_pair, observation
from app.cmc.storage import Sidecar
from app.cmc.profiles import CollectionProfile
from app.data.exchange_clients import BinanceFuturesClient


class Semantics(Frozen):
    market_id: int = Field(gt=0)
    source_contract_ref: str
    oi_units: Literal["base", "USD"] | None = None
    oi_counting_convention: Literal["open_contract_quantity"] | None = None
    funding_semantics: Literal["settled_funding", "indicative_funding"] | None = None
    index_price_units: Literal["USD"] | None = None
    funding_interval_seconds: int | None = Field(default=None, gt=0, le=86400)
    verified_at: datetime
    expires_at: datetime


class IdentityManifest(Frozen):
    bindings: tuple[AssetBinding, ...] = ()
    semantics: tuple[Semantics, ...] = ()
    category_ids: tuple[str, ...] = ()
    external_instruments: tuple["ReviewedInstrument", ...] = ()

    @model_validator(mode="after")
    def unique_bindings(self):
        for attr in ("cmc_id", "binance_base", "contract_symbol", "market_id"):
            values = [getattr(b, attr) for b in self.bindings if getattr(b, attr) is not None]
            if len(values) != len(set(values)):
                raise ValueError("conflicting or ambiguous asset bindings")
        return self


class ReviewedInstrument(Frozen):
    instrument: Instrument
    approved: bool = False
    native_contract_ref: str
    approved_at: datetime
    expires_at: datetime


IdentityManifest.model_rebuild()


def load_manifest(path: Path | None) -> IdentityManifest:
    if path is None:
        return IdentityManifest()
    if path.stat().st_size > 256 * 1024:
        raise ValueError("identity manifest budget")
    manifest = IdentityManifest.model_validate_json(path.read_text(encoding="utf-8"))
    if len(manifest.bindings) > 100 or len(manifest.semantics) > 400 or len(manifest.category_ids) > 4 or len(manifest.external_instruments)>100:
        raise ValueError("identity manifest item budget")
    if len({b.cmc_id for b in manifest.bindings}) != len(manifest.bindings):
        raise ValueError("duplicate asset binding")
    if len({b.binance_base for b in manifest.bindings}) != len(manifest.bindings):
        raise ValueError("ambiguous exchange asset binding")
    if len({r.instrument.cmc_id for r in manifest.external_instruments})>4:
        raise ValueError("cross-exchange acquisition budget is four assets")
    return manifest


class BinanceMetadataProvider(BinanceFuturesClient):
    async def snapshot(self):
        # Reuses existing read-only timeout / 429 / retry implementation for one bulk request.
        return await self._get_json("/fapi/v1/exchangeInfo")


class Collector:
    def __init__(self, client: CmcClient, store: Sidecar, *, manifest=IdentityManifest(),
                 metadata_provider=None, max_pages=2, max_observations=2500, profile: CollectionProfile | None = None):
        self.client, self.store, self.manifest = client, store, manifest
        self.metadata_provider = metadata_provider
        self.profile = profile
        self.max_pages, self.max_observations = max_pages, profile.max_observations if profile else max_observations
        self._families = OrderedDict()
        self._restored = False
        self._metadata = ({}, None)
        self._retry_at = {}

    async def collect(self) -> Snapshot:
        requests = (
            (Endpoint.LISTINGS, {"limit":100, "sort":"market_cap"}),
            (Endpoint.MAP, {"limit":1000}),
            (Endpoint.GLOBAL, {}), (Endpoint.FEAR_GREED, {}), (Endpoint.ALTCOIN, {}),
            (Endpoint.EXCHANGES, {"limit":100}),
            (Endpoint.EXCHANGE_PAIRS, {"exchange_slug":"binance", "category":"perpetual", "limit":250}),
            (Endpoint.LIQUIDATIONS, {}), (Endpoint.EXCHANGE_LIQUIDATIONS, {"limit":100}),
            (Endpoint.CRYPTO_LIQUIDATIONS, {"limit":100}), (Endpoint.CATEGORIES, {"limit":100}),
            *((Endpoint.CATEGORY, {"id":category,"limit":100}) for category in self.manifest.category_ids),
            *((Endpoint.CRYPTO_PAIRS,{"crypto_id":asset,"category":"perpetual","limit":250})
              for asset in sorted({r.instrument.cmc_id for r in self.manifest.external_instruments if r.approved})),
        )
        # Sequential endpoint families, with only client-bounded concurrent callers. This
        # avoids a burst of requests on the resource-constrained scanner host.
        fetched, errors = [], []
        if self.profile and not self._restored:
            saved = await asyncio.to_thread(self.store.causal, "collection_family", self.client.clock(), limit=64)
            for row in saved:
                key = row["request_key"]
                if key not in self._families:
                    self._families[key] = tuple(Fetch.model_validate(f) for f in row["fetches"])
                if len(self._families) >= self.profile.cache_entries:
                    break
            self._restored = True
        for endpoint, params in requests:
            family = self.profile.family(endpoint) if self.profile else None
            if self.profile and family is None:
                continue
            if family and family.limit is not None:
                params = {**params, "limit": family.limit}
            if endpoint == Endpoint.MAP and self.manifest.bindings:
                # Symbol is only a bounded acquisition filter. Exact reviewed
                # numeric IDs, slugs and independent metadata still own identity.
                params = {**params, "symbol": ",".join(b.binance_base for b in self.manifest.bindings)}
            key = stable_id((endpoint, params))
            previous = self._families.get(key, ())
            if previous:
                self._families.move_to_end(key)
            now = self.client.clock()
            if family and now < self._retry_at.get(key, now):
                fetched.extend(previous)
                errors.append(endpoint.value+":retry_backoff_stale_fallback")
                continue
            if (family and previous and all(not f.error for f in previous)
                    and all(0 <= (now-f.acquired_at).total_seconds() < family.seconds for f in previous)):
                fetched.extend(previous)
                if any(f.pagination_complete is False for f in previous):
                    errors.append(endpoint.value+":page_budget_partial")
                continue
            start = len(fetched)
            try:
                if endpoint in {Endpoint.LISTINGS, Endpoint.MAP, Endpoint.EXCHANGE_PAIRS, Endpoint.CRYPTO_PAIRS}:
                    async for result in self.client.pages(endpoint, params, max_pages=family.max_pages if family else self.max_pages):
                        fetched.append(result)
                        if result.error:
                            errors.append(endpoint.value + ":" + result.error)
                        elif result.pagination_complete is False:
                            errors.append(endpoint.value+":page_budget_partial")
                else:
                    result = await self.client.get(endpoint, params)
                    fetched.append(result)
                    if result.error:
                        errors.append(endpoint.value + ":" + result.error)
            except Exception as exc:
                reason="response_contract:"+type(exc).__name__
                errors.append(endpoint.value+":"+reason)
                fetched.append(Fetch(endpoint=endpoint,acquired_at=self.client.clock(),state=S.ERROR,error=reason))
            current = tuple(fetched[start:])
            if family and current:
                if any(f.error for f in current):
                    self._retry_at[key] = now + timedelta(seconds=min(300, family.seconds))
                if any(f.error for f in current) and previous:
                    # Outage keeps the last acquired evidence, which still expires
                    # at source time. Failed attempts never refresh that timestamp.
                    fetched[start:] = previous
                elif not any(f.error for f in current):
                    self._families[key] = current
                    while len(self._families) > self.profile.cache_entries:
                        self._families.popitem(last=False)
                    try:
                        await asyncio.to_thread(self.store.put, "collection_family", {
                            "request_key":key, "fetches":[f.model_dump(mode="json") for f in current]},
                            acquired_at=self.client.clock())
                    except ValueError:
                        errors.append(endpoint.value+":restart_cache_payload_budget")
        metadata, metadata_at = self._metadata
        if (self.manifest.bindings and (metadata_at is None or (self.client.clock()-metadata_at).total_seconds() >= 3600)
                and any(f.endpoint in {Endpoint.EXCHANGE_PAIRS,Endpoint.CRYPTO_PAIRS} and not f.error for f in fetched)):
            provider = self.metadata_provider or BinanceMetadataProvider(timeout=3, retry_attempts=2)
            try:
                payload = await asyncio.wait_for(provider.snapshot(), timeout=8)
                refreshed_at = self.client.clock()
                symbols = payload.get("symbols", [])
                if not isinstance(symbols, list) or len(symbols) > 10000:
                    raise ValueError("metadata item budget")
                refreshed = {}
                for s in symbols:
                    if not isinstance(s,dict) or not isinstance(s.get("symbol"),str):
                        continue
                    key=s["symbol"]
                    refreshed[key]=s if key not in refreshed else {}
                await asyncio.to_thread(self.store.put, "exchange_metadata", payload, acquired_at=refreshed_at)
                metadata, metadata_at = refreshed, refreshed_at
                self._metadata = (metadata, metadata_at)
            except Exception as exc:
                errors.append("exchange_metadata:" + type(exc).__name__)
            finally:
                if self.metadata_provider is None:
                    await provider.aclose()
        assets = {}
        asset_times = {}
        for result in fetched:
            if result.endpoint == Endpoint.MAP and not result.error:
                for asset in result.data:
                    if not isinstance(asset,dict) or not isinstance(asset.get("id"),int):
                        errors.append("invalid_cmc_id_map_row")
                        continue
                    if asset.get("id") in assets:
                        errors.append("ambiguous_cmc_id_map")
                        assets[asset["id"]] = {}
                    else:
                        assets[asset["id"]] = asset
                        asset_times[asset["id"]] = result.acquired_at
        bindings = {b.cmc_id:b for b in self.manifest.bindings}
        registry, registry_times = {}, {}
        for result in fetched:
            if result.endpoint==Endpoint.EXCHANGES and not result.error:
                for exchange in result.data.get("exchanges",[]):
                    key=exchange.get("exchange_slug")
                    registry[key]=exchange if key not in registry else {}
                    registry_times[key]=result.acquired_at
        observations = []
        for result in fetched:
            if result.raw is not None:
                await asyncio.to_thread(self.store.put, "raw", {
                    "payload":result.raw_text if result.raw_text is not None else result.raw},
                    acquired_at=result.acquired_at, identity=result.raw_ref)
            if result.endpoint in {Endpoint.EXCHANGE_PAIRS,Endpoint.CRYPTO_PAIRS} and not result.error:
                exchange = {k:result.data.get(k) for k in ("exchange_id", "exchange_slug")}
                for pair in rows(result.endpoint, result.data):
                    binding = bindings.get(pair.get("market_pair_base", {}).get("crypto_id"))
                    pair_exchange=pair.get("exchange",exchange)
                    if binding is None:
                        continue  # raw evidence retained; never manufacture a symbol mapping
                    instrument = None
                    if metadata_at is not None and pair_exchange.get("exchange_slug")=="binance":
                        mapped = match_instrument(pair, pair_exchange, metadata.get(binding.binance_base + "USDT", {}),
                            binding, assets.get(binding.cmc_id, {}), as_of=self.client.clock(),
                            metadata_at=metadata_at, acquired_at=self.client.clock(),
                            map_acquired_at=asset_times.get(binding.cmc_id, self.client.clock()),
                            exchange_registry=registry.get("binance",{}),exchange_registry_acquired_at=registry_times.get("binance",self.client.clock()),
                            require_owner_approval=True)
                        await asyncio.to_thread(self.store.put, "mapping", mapped, acquired_at=self.client.clock())
                        instrument = mapped.instrument
                        if instrument:
                            observations.append(observation(result.model_copy(update={"acquired_at":self.client.clock()}),
                                "instrument_identity", 1, units="exact_contract_identity", source=result.acquired_at,
                                scope="identity", cmc_id=instrument.cmc_id, exchange_id=instrument.exchange_id,
                                instrument=instrument, max_age=86400,
                                reason="Acquired identity metadata only; does not refresh any market metric"))
                    else:
                        instrument=self._external_instrument(pair,pair_exchange,assets,asset_times,registry,registry_times)
                    specs = [s for s in self.manifest.semantics if s.market_id == pair.get("market_id")
                             and s.source_contract_ref and s.verified_at.tzinfo is not None
                             and s.expires_at.tzinfo is not None and s.verified_at <= result.acquired_at < s.expires_at]
                    semantics = specs[0] if len(specs) == 1 else None
                    observations.extend(normalize_pair(result, pair, instrument,
                        oi_units=semantics.oi_units if semantics else None,
                        funding_interval_seconds=semantics.funding_interval_seconds if semantics else None,
                        funding_semantics=semantics.funding_semantics if semantics else None,
                        oi_counting_convention=semantics.oi_counting_convention if semantics else None,
                        index_price_units=semantics.index_price_units if semantics else None))
            else:
                observations.extend(normalize_context(result))
            if len(observations) > self.max_observations:
                raise ValueError("normalized observation budget exceeded")
        acquired = self.client.clock()
        snapshot = Snapshot(observations=tuple(observations), errors=tuple(errors),
            batch_id=stable_id((tuple((f.endpoint,f.raw_ref,f.acquired_at) for f in fetched),
                               tuple(o.id for o in observations), tuple(errors))))
        await asyncio.to_thread(self.store.put, "snapshot", snapshot, acquired_at=acquired,
            identity=snapshot.batch_id)
        await asyncio.to_thread(self.store.put, "collection_batch", {
            "batch_id":snapshot.batch_id, "capabilities":self.client.capabilities,
            "profile":self.profile.name if self.profile else "LEGACY",
            "coverage":"PARTIAL" if errors else "REQUESTED_FAMILIES_ONLY",
            "usage":self.store.credit_summary(acquired), "errors":errors,
            "sources":[{"endpoint":f.endpoint.value,"acquired_at":f.acquired_at.isoformat(),"raw_ref":f.raw_ref,
                        "authenticated":f.authenticated,"credits_reported":f.credits_reported,
                        "pagination_complete":f.pagination_complete} for f in fetched],
            "observation_count":len(observations)}, acquired_at=acquired)
        return snapshot

    def _external_instrument(self,pair,exchange,assets,asset_times,registry,registry_times):
        """External venue metadata must be separately supplied and owner-reviewed."""
        matches=[r for r in self.manifest.external_instruments if r.instrument.market_id==pair.get("market_id")
                 and r.instrument.exchange_id==exchange.get("exchange_id")]
        if len(matches)!=1:
            return None
        reviewed=matches[0]
        i=reviewed.instrument
        now=self.client.clock()
        if (not reviewed.approved or not reviewed.native_contract_ref or reviewed.approved_at.tzinfo is None
                or reviewed.expires_at.tzinfo is None or not reviewed.approved_at<=now<reviewed.expires_at
                or pair.get("category")!="perpetual" or pair.get("outlier_detected") is not False or pair.get("exclusions")
                or exchange.get("exchange_slug")!=i.exchange or registry.get(i.exchange,{}).get("exchange_id")!=i.exchange_id
                or pair.get("market_pair_base",{}).get("crypto_id")!=i.cmc_id
                or pair.get("market_pair_base",{}).get("exchange_symbol")!=i.base
                or pair.get("market_pair_quote",{}).get("crypto_id")!=i.quote_id
                or pair.get("market_pair_quote",{}).get("exchange_symbol")!=i.quote
                or not assets.get(i.cmc_id,{}).get("is_active")):
            return None
        return i.model_copy(update={"map_acquired_at":asset_times[i.cmc_id],
            "binding_approved_at":max(i.binding_approved_at,reviewed.approved_at),
            "exchange_registry_acquired_at":registry_times[i.exchange],"exchange_registry_ref":stable_id(registry[i.exchange])})
