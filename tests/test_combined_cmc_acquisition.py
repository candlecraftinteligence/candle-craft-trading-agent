"""Deterministic synthetic source contracts, never real instrument approvals."""
import asyncio
import json
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from app.cmc.client import CmcClient
from app.cmc.collection import Collector, IdentityManifest
from app.cmc.endpoints import Endpoint as E
from app.cmc.identity_review import review_universe
from app.cmc.profiles import CollectionProfile, Family
from app.cmc.acquisition import validate_family
from app.cmc.storage import Sidecar
from tests.fixtures.cmc import AT, fetched, pair
from tests.test_cmc_identity_review import approved

pytestmark = pytest.mark.no_auto_epoch


def universe_fixture(count=55):
    bases = [f"FIXTURE{i:03d}" for i in range(count-1)] + ["币安人生"]
    metadata, assets, pairs = [], [], []
    for i, base in enumerate(bases, 1):
        metadata.append({"symbol":base+"USDT", "baseAsset":base,"quoteAsset":"USDT",
            "marginAsset":"USDT","contractType":"PERPETUAL","status":"TRADING"})
        assets.append({"id":i,"slug":f"synthetic-slug-{i}","symbol":base,"is_active":1})
        p = pair()
        p.update(market_id=10000+i, market_pair_symbol=base+"/USDT",
            market_pair_base={"crypto_id":i,"exchange_symbol":base,"symbol":base})
        pairs.append(p)
    return metadata, assets, pairs


def pages_fixture():
    metadata, assets, pairs = universe_fixture()
    maps = [fetched(E.MAP,assets,pagination_complete=True)]
    pages = [fetched(E.EXCHANGE_PAIRS,{"exchange_id":270,"exchange_slug":"binance",
        "market_pairs":pairs[start:start+15]},
        pagination_complete=True if start == 45 else None).model_copy(update={"raw_ref":f"synthetic:page-{start}"})
        for start in range(0,55,15)]
    return metadata, maps, pages


def test_all_55_exact_candidates_unicode_and_slugs_preserved_without_approval():
    metadata, maps, pages = pages_fixture()
    result = review_universe(metadata,map_pages=maps,pair_pages=pages,as_of=AT)
    assert len(result) == 55 and all(r["candidate"] is not None for r in result)
    assert {r["candidate"]["cmc_id"] for r in result} == set(range(1,56))
    assert result[-1]["symbol"] == "币安人生USDT"
    assert result[-1]["candidate"]["cmc_slug"] == "synthetic-slug-55"
    assert all(not r["approved"] and r["binding_state"] == "UNAPPROVED" for r in result)
    assert all(set(r["semantic_verification"].values()) == {"Unverified"} for r in result)


@pytest.mark.parametrize("fault", ["partial", "duplicate", "exchange", "stale", "future"])
def test_partial_colliding_or_stale_families_never_create_candidates(fault):
    metadata, maps, pages = pages_fixture()
    if fault == "partial": pages[-1] = pages[-1].model_copy(update={"pagination_complete":False})
    if fault == "duplicate": pages[1].data["market_pairs"][0]["market_id"] = pages[0].data["market_pairs"][0]["market_id"]
    if fault == "exchange": pages[-1].data["exchange_id"] = 999
    if fault == "stale": maps = [maps[0].model_copy(update={"acquired_at":AT-timedelta(days=2)})]
    if fault == "future": maps = [maps[0].model_copy(update={"acquired_at":AT+timedelta(seconds=1)})]
    result = review_universe(metadata,map_pages=maps,pair_pages=pages,as_of=AT)
    assert all(r["candidate"] is None and not r["approved"] for r in result)


def test_multipliers_exclusions_and_ticker_similarity_stay_withheld():
    metadata, maps, pages = pages_fixture()
    for i, base in enumerate(("1000LUNC", "1000PEPE", "1000SHIB", "ARB")):
        metadata[i].update(symbol=base+"USDT",baseAsset=base)
        pages[0].data["market_pairs"][i]["market_pair_base"]["exchange_symbol"] = base
    pages[0].data["market_pairs"][3]["exclusions"] = ["volume"]
    result = review_universe(metadata,map_pages=maps,pair_pages=pages,as_of=AT)
    assert all("multiplier_equivalence_unverified" in r["withholding_reasons"] for r in result[:3])
    assert "provider_exclusions_or_outlier" in result[3]["withholding_reasons"]
    # Removing the multiplier is never a fallback join.
    pages[0].data["market_pairs"][0]["market_pair_base"]["exchange_symbol"] = "LUNC"
    assert review_universe(metadata,map_pages=maps,pair_pages=pages,as_of=AT)[0]["candidate"] is None


@pytest.mark.parametrize("base,multiplier", [("1000LUNC",1),("1000PEPE",1),("1000SHIB",1),("BTC",1000)])
def test_identity_owner_approval_cannot_establish_multiplier_semantics(base,multiplier):
    from tests.test_cmc_evidence import _mapping
    binding = approved(binance_base=base,contract_symbol=base+"USDT",contract_multiplier=multiplier)
    p = pair()
    p["market_pair_base"]["exchange_symbol"] = base
    native = {"symbol":base+"USDT","baseAsset":base,"quoteAsset":"USDT","marginAsset":"USDT",
        "contractType":"PERPETUAL","status":"TRADING"}
    result = _mapping(binding=binding,pair=p,metadata=native,require_owner_approval=True)
    assert result.instrument is None and result.state.value == "UNVERIFIED"
    assert "multiplier" in result.reason


def test_collector_uses_shared_unfiltered_map_and_four_pair_pages(tmp_path):
    async def scenario():
        metadata, assets, pairs = universe_fixture()
        calls = []
        def handler(request):
            e = E(request.url.path)
            calls.append((e,dict(request.url.params)))
            if e == E.MAP:
                assert "symbol" not in request.url.params
                assert request.url.params["sort"] == "cmc_rank" and request.url.params["aux"] == "is_active"
                data = assets
            else:
                start = int(request.url.params["start"])-1
                data = {"exchange_id":270,"exchange_slug":"binance","market_pairs":pairs[start:start+15]}
            return httpx.Response(200,json={"status":{"error_code":0,"credit_count":0 if e == E.MAP else 1},"data":data})
        manifest = IdentityManifest(bindings=tuple(approved(cmc_id=a["id"],cmc_slug=a["slug"],
            binance_base=m["baseAsset"],contract_symbol=m["symbol"],market_id=p["market_id"],approved=False,
            approved_at=None,reviewed_at=None,owner_approval_ref=None) for m,a,p in zip(metadata,assets,pairs)))
        class Metadata:
            async def snapshot(self): return {"symbols":metadata}
        store = Sidecar(tmp_path/"cmc_unicode.sqlite")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = CmcClient(store=store,http_client=http,api_key=SecretStr("synthetic"),clock=lambda:AT)
            profile = CollectionProfile(name="CUSTOM",families=(Family(endpoint=E.MAP,seconds=86400,limit=1000),
                Family(endpoint=E.EXCHANGE_PAIRS,seconds=14400,limit=15,max_pages=4)))
            snapshot = await Collector(client,store,manifest=manifest,profile=profile,metadata_provider=Metadata()).collect()
            assert len(calls) == 5 and len([c for c in calls if c[0] == E.MAP]) == 1
            assert not any("partial" in e for e in snapshot.errors)
            assert all(o.instrument is None for o in snapshot.observations)
            assert not any(b.approved for b in manifest.bindings)
            await client.aclose()
    asyncio.run(scenario())


def test_provider_declared_total_prevents_short_page_from_being_marked_complete(tmp_path):
    async def scenario():
        def handler(request):
            return httpx.Response(200,json={"status":{"error_code":0,"credit_count":1},"data":{
                "exchange_id":270,"exchange_slug":"binance","num_market_pairs":820,"market_pairs":[pair()]}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = CmcClient(store=Sidecar(tmp_path/"cmc_declared.sqlite"),http_client=http,
                api_key=SecretStr("synthetic"),clock=lambda:AT)
            result = [p async for p in client.pages(E.EXCHANGE_PAIRS,{"exchange_slug":"binance","limit":250},max_pages=4)]
            assert len(result) == 1 and result[0].error == "pagination_count_conflict"
            assert result[0].pagination_complete is not True
            await client.aclose()
    asyncio.run(scenario())


def test_bounded_map_can_prove_exact_requested_ids_without_claiming_full_catalog_coverage():
    _, maps, _ = pages_fixture()
    partial = [maps[0].model_copy(update={"pagination_complete":False})]
    assert not validate_family(partial)
    assert validate_family(partial,required_ids=set(range(1,56)))
    assert not validate_family(partial,required_ids={999999})
    assert not validate_family([*partial,*partial],required_ids={1})


@pytest.mark.parametrize("missing", [False,True])
def test_partial_map_target_coverage_is_explicit_and_never_approves_identities(tmp_path,missing):
    async def scenario():
        metadata,assets,pairs = universe_fixture()
        def handler(request):
            e = E(request.url.path)
            if e == E.MAP:
                data = [*assets, *({"id":100+i,"slug":f"extra-{i}","is_active":1} for i in range(5))]
                if missing: data[-6] = {"id":999999,"slug":"unrelated","is_active":1}
            else:
                data = {"exchange_id":270,"exchange_slug":"binance","market_pairs":pairs}
            # A full single map page is globally partial, but can contain every
            # exact requested ID. Pair pagination is independently complete.
            return httpx.Response(200,json={"status":{"error_code":0,"credit_count":0 if e == E.MAP else 1},"data":data})
        store = Sidecar(tmp_path/"cmc_target_ids.sqlite")
        manifest = IdentityManifest(bindings=tuple(approved(cmc_id=a["id"],cmc_slug=a["slug"],
            binance_base=m["baseAsset"],contract_symbol=m["symbol"],market_id=p["market_id"],approved=False,
            approved_at=None,reviewed_at=None,owner_approval_ref=None) for m,a,p in zip(metadata,assets,pairs)))
        class Metadata:
            async def snapshot(self): return {"symbols":metadata}
        profile = CollectionProfile(name="CUSTOM",families=(Family(endpoint=E.MAP,seconds=86400,limit=60),
            Family(endpoint=E.EXCHANGE_PAIRS,seconds=14400,limit=100)))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = CmcClient(store=store,http_client=http,api_key=SecretStr("synthetic"),clock=lambda:AT)
            for collector in (Collector(client,store,manifest=manifest,profile=profile,metadata_provider=Metadata()),
                              Collector(client,store,manifest=manifest,profile=profile,metadata_provider=Metadata())):
                snapshot = await collector.collect()
                coverage = store.causal("collection_batch",AT,limit=1)[0]["map_identity_coverage"]
                assert coverage["provider_pagination_complete"] is False
                assert coverage["requested_ids_complete"] is (not missing)
                assert coverage["missing_ids"] == ([55] if missing else [])
                assert all(o.instrument is None for o in snapshot.observations)
                assert (any("incomplete_or_ambiguous_family_withheld" in e for e in snapshot.errors)) is missing
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("fault", ["duplicate", "short_has_more", "budget", "repeated"])
def test_pagination_failure_contracts_are_explicit(tmp_path,fault):
    async def scenario():
        def handler(request):
            start = int(request.url.params["start"])
            item = {**pair(),"market_id":1 if fault in {"duplicate","repeated"} else start}
            data = {"exchange_id":270,"exchange_slug":"binance","market_pairs":[item],"has_more":True}
            if fault == "duplicate" and start == 1: data["market_pairs"].append({**item,"market_id":2})
            return httpx.Response(200,json={"status":{"error_code":0,"credit_count":1},"data":data})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = CmcClient(store=Sidecar(tmp_path/"cmc_pages.sqlite"),http_client=http,
                api_key=SecretStr("synthetic"),clock=lambda:AT,daily_credits=0 if fault == "budget" else 100)
            result = [p async for p in client.pages(E.EXCHANGE_PAIRS,{"exchange_slug":"binance","limit":2},max_pages=4)]
            assert result[-1].error or result[-1].pagination_complete is False
            assert not any(p.pagination_complete is True for p in result)
            await client.aclose()
    asyncio.run(scenario())
