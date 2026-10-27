import asyncio
from collections import Counter
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from app.cmc.client import CmcClient
from app.cmc.collection import Collector
from app.cmc.endpoints import Endpoint as E
from app.cmc.profiles import CollectionProfile, Family, profile_for
from app.cmc.storage import Sidecar
from tests.fixtures.cmc import AT

pytestmark = pytest.mark.no_auto_epoch


def test_endpoint_schedules_cache_restart_and_outage_preserve_original_acquisition(tmp_path):
    async def scenario():
        at, seconds = AT, 0
        calls = Counter()
        down = False
        def handler(request):
            endpoint = E(request.url.path)
            calls[endpoint] += 1
            if down: return httpx.Response(503)
            data = ({"last_updated":at.isoformat(), "btc_dominance":60, "eth_dominance":15,
                "quote":{"USD":{"last_updated":at.isoformat(), "total_market_cap":100,
                    "total_market_cap_yesterday_percentage_change":-4}}} if endpoint == E.GLOBAL else
                {"value":30, "update_time":at.isoformat()})
            return httpx.Response(200, json={"status":{"error_code":0,"credit_count":1}, "data":data})
        profile = CollectionProfile(name="CUSTOM", families=(Family(endpoint=E.GLOBAL,seconds=600),
            Family(endpoint=E.FEAR_GREED,seconds=3600)))
        path = tmp_path / "cmc_schedules.sqlite"
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            def client():
                return CmcClient(store=Sidecar(path), api_key=SecretStr("synthetic"), http_client=http,
                    clock=lambda:at, monotonic=lambda:seconds, attempts=1)
            c = client()
            collector = Collector(c,c.store,profile=profile)
            first = await collector.collect()
            for seconds in (60,120,300,599):
                at = AT+timedelta(seconds=seconds)
                reused = await collector.collect()
                assert reused.observations == first.observations
            assert calls == {E.GLOBAL:1,E.FEAR_GREED:1}
            seconds = 600
            at = AT+timedelta(seconds=seconds)
            await collector.collect()
            assert calls == {E.GLOBAL:2,E.FEAR_GREED:1}
            await c.aclose()
            # Restart restores the completed family, not an invented new acquisition.
            c = client()
            collector = Collector(c,c.store,profile=profile)
            await collector.collect()
            assert calls == {E.GLOBAL:2,E.FEAR_GREED:1}
            down = True
            seconds = 1200
            at = AT+timedelta(seconds=seconds)
            outage = await collector.collect()
            assert outage.errors and all(o.acquired_at < at for o in outage.observations)
            assert all(o.status_at(at).value != "VERIFIED" for o in outage.observations if o.metric == "btc_dominance")
            await c.aclose()
    asyncio.run(scenario())


def test_partial_pagination_stays_explicit_on_cache_hit(tmp_path):
    async def scenario():
        profile = CollectionProfile(name="CUSTOM",families=(Family(endpoint=E.LISTINGS,seconds=3600,limit=1),))
        def handler(request):
            return httpx.Response(200,json={"status":{"error_code":0,"credit_count":1},
                "data":[{"id":1,"cmc_rank":1,"last_updated":AT.isoformat(),"quote":[]}]})
        store = Sidecar(tmp_path/"cmc_partial.sqlite")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = CmcClient(store=store,keyless=True,http_client=http,clock=lambda:AT)
            collector = Collector(client,store,profile=profile)
            for _ in range(2):
                assert any("page_budget_partial" in e for e in (await collector.collect()).errors)
            await client.aclose()
    asyncio.run(scenario())


def test_restored_family_cache_cannot_exceed_profile_entry_budget(tmp_path):
    async def scenario():
        from tests.fixtures.cmc import fetched
        store = Sidecar(tmp_path/"cmc_family_budget.sqlite")
        for index in range(20):
            store.put("collection_family",{"request_key":str(index),
                "fetches":[fetched(E.GLOBAL,{}).model_dump(mode="json")]},acquired_at=AT)
        profile = CollectionProfile(name="CUSTOM",cache_entries=16,
            families=(Family(endpoint=E.GLOBAL,seconds=600),))
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:httpx.Response(200,
                json={"status":{"error_code":0,"credit_count":1},"data":{}}))) as http:
            c = CmcClient(store=store,keyless=True,http_client=http,clock=lambda:AT)
            collector = Collector(c,store,profile=profile)
            await collector.collect()
            assert len(collector._families) == 16
            await c.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("reported,charged", [(0,0),(1,1),(4,4),(None,1)])
def test_actual_credit_reconciliation_and_restart(tmp_path,reported,charged):
    store = Sidecar(tmp_path/"cmc_receipt.sqlite")
    assert store.reserve(AT,1,daily=3,monthly=3)
    store.reconcile(AT,1,reported)
    store = Sidecar(store.path)
    row = store.credit_summary(AT)["d:2026-01-01"]
    assert row["reserved_credits"] == charged
    assert row["LOCAL_RESERVED_CREDITS"] == 1
    assert row["PROVIDER_REPORTED_CREDITS"] == (reported or 0)
    assert row["UNKNOWN_USAGE"] == (1 if reported is None else 0)
    assert row["FAILED_ATTEMPT_RESERVATIONS"] == 0
    assert store.reserve(AT,1,daily=3,monthly=3) == (charged < 3)


def test_failures_and_month_boundary_remain_charged_to_original_buckets(tmp_path):
    store = Sidecar(tmp_path/"cmc_month.sqlite")
    at = AT.replace(day=31,hour=23,minute=59)
    assert store.reserve(at,2,daily=5,monthly=5)
    # An unanswered reservation remains failed/unknown, not a zero-cost request.
    assert store.credit_summary(at)["m:2026-01"]["FAILED_ATTEMPT_RESERVATIONS"] == 2
    store.reconcile(at,2,3)
    later = at+timedelta(minutes=1)
    assert not store.usage(later)
    assert store.reserve(later,5,daily=5,monthly=5)
    assert not store.reserve(later,1,daily=5,monthly=5)
    assert store.usage(at)["m:2026-01"]["reserved_credits"] == 3


def test_legacy_usage_stays_unknown_and_naive_accounting_is_rejected(tmp_path):
    store = Sidecar(tmp_path/"cmc_legacy.sqlite")
    with store.connect() as db:
        db.execute("INSERT INTO usage VALUES('d:2026-01-01',5,5)")
    assert store.credit_summary(AT)["d:2026-01-01"]["UNKNOWN_USAGE"] == 5
    assert store.reserve(AT,1,daily=10,monthly=10)
    store.reconcile(AT,1,1)
    assert store.credit_summary(AT)["d:2026-01-01"]["UNKNOWN_USAGE"] == 5
    with pytest.raises(ValueError): store.reserve(AT.replace(tzinfo=None),1,daily=10,monthly=10)
    with pytest.raises(ValueError): store.reconcile(AT.replace(tzinfo=None),1,1)
    with pytest.raises(ValueError): store.usage(AT.replace(tzinfo=None))


def test_all_profiles_and_custom_resource_bounds(tmp_path):
    research = profile_for()
    focused = profile_for("DECISION_FOCUSED")
    assert research.family(E.CRYPTO_LIQUIDATIONS) and not focused.family(E.CRYPTO_LIQUIDATIONS)
    assert focused.family(E.GLOBAL).seconds == 600 and focused.family(E.MAP).seconds == 86400
    assert profile_for("LOW_CREDIT").family(E.GLOBAL).seconds == 900
    path = tmp_path/"custom.json"
    custom = focused.model_copy(update={"name":"CUSTOM"})
    path.write_text(custom.model_dump_json())
    assert profile_for("CUSTOM",custom_path=path) == custom
    for change in ({"max_body_bytes":2**30},{"concurrency":100},{"max_observations":100000},
                   {"requests_per_minute":51},{"families":focused.families*2}):
        with pytest.raises(ValueError): CollectionProfile.model_validate({**focused.model_dump(),**change})
    with pytest.raises(ValueError): profile_for("CUSTOM")


def test_request_rate_cap_includes_retries(tmp_path):
    async def scenario():
        seconds = 0
        calls = []
        async def sleep(delay):
            nonlocal seconds
            seconds += delay
        def handler(request):
            calls.append(seconds)
            return httpx.Response(503)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            c = CmcClient(store=Sidecar(tmp_path/"cmc_rate.sqlite"),keyless=True,http_client=http,
                clock=lambda:AT,monotonic=lambda:seconds,sleep=sleep,requests_per_minute=2)
            await c.get(E.GLOBAL)
            assert len(calls) == 3 and calls[2]-calls[0] >= 60
            assert c.store.credit_summary(AT)["d:2026-01-01"]["FAILED_ATTEMPT_RESERVATIONS"] == 3
            await c.aclose()
    asyncio.run(scenario())


def test_failed_metadata_refresh_cannot_renew_expired_identity(tmp_path):
    async def scenario():
        from app.cmc.collection import IdentityManifest
        from test_cmc_identity_review import approved
        from tests.fixtures.cmc import pair
        profile = CollectionProfile(name="CUSTOM",families=tuple(Family(endpoint=e,seconds=86400)
            for e in (E.MAP,E.EXCHANGES,E.EXCHANGE_PAIRS)))
        data = {E.MAP:[{"id":1,"slug":"bitcoin","is_active":1}],
            E.EXCHANGES:{"exchanges":[{"exchange_id":270,"exchange_slug":"binance"}]},
            E.EXCHANGE_PAIRS:{"exchange_id":270,"exchange_slug":"binance","market_pairs":[pair()]}}
        def handler(request):
            return httpx.Response(200,json={"status":{"error_code":0,"credit_count":1},
                "data":data[E(request.url.path)]})
        class BrokenMetadata:
            async def snapshot(self): return {"symbols":None}
        old = AT-timedelta(days=2)
        metadata = {"BTCUSDT":{"symbol":"BTCUSDT","baseAsset":"BTC","quoteAsset":"USDT",
            "marginAsset":"USDT","contractType":"PERPETUAL","status":"TRADING"}}
        store = Sidecar(tmp_path/"cmc_metadata.sqlite")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            c = CmcClient(store=store,api_key=SecretStr("synthetic"),http_client=http,clock=lambda:AT)
            collector = Collector(c,store,profile=profile,manifest=IdentityManifest(bindings=(approved(),)),
                metadata_provider=BrokenMetadata())
            collector._metadata = (metadata,old)
            snapshot = await collector.collect()
            assert snapshot.errors and all(o.instrument is None for o in snapshot.observations)
            assert collector._metadata == (metadata,old)
            await c.aclose()
    asyncio.run(scenario())
