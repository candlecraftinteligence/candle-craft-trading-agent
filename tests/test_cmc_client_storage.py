import asyncio
import hashlib
import sqlite3
from datetime import timedelta

import httpx
import pytest
from pydantic import SecretStr

from app.cmc.client import CmcClient
from app.cmc.endpoints import Endpoint
from app.cmc.models import EvidenceState as S
from app.cmc.storage import Sidecar
from tests.fixtures.cmc import AT

pytestmark = pytest.mark.no_auto_epoch


def envelope(data=None,**status):
    return {"status":{"error_code":0,"credit_count":1,**status},"data":data if data is not None else {"fixture":True}}


def test_coalescing_cache_and_cancellation_do_not_duplicate_calls(tmp_path):
    async def scenario():
        calls=0
        async def handler(request):
            nonlocal calls
            calls+=1
            await asyncio.sleep(0.02)
            return httpx.Response(200,json=envelope())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client=CmcClient(store=Sidecar(tmp_path/"cmc_cache.sqlite"),keyless=True,http_client=http,clock=lambda:AT)
            results=await asyncio.gather(*(client.get(Endpoint.GLOBAL) for _ in range(10)))
            results[0].data["fixture"]=False
            cached=await client.get(Endpoint.GLOBAL)
            assert cached.data["fixture"] is True
            assert calls==1 and all(r.state==S.UNVERIFIED for r in results)
            assert client.store.usage(AT)["d:2026-01-01"]["requests"]==1
            await client.aclose()
    asyncio.run(scenario())


def test_cancelled_waiter_keeps_shared_request_and_cache(tmp_path):
    async def scenario():
        ready=asyncio.Event()
        calls=0
        async def handler(request):
            nonlocal calls
            calls+=1
            await ready.wait()
            return httpx.Response(200,json=envelope())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client=CmcClient(store=Sidecar(tmp_path/"cmc_cancel.sqlite"),keyless=True,http_client=http)
            first=asyncio.create_task(client.get(Endpoint.GLOBAL))
            await asyncio.sleep(0)
            second=asyncio.create_task(client.get(Endpoint.GLOBAL))
            first.cancel()
            with pytest.raises(asyncio.CancelledError): await first
            ready.set()
            assert not (await second).error
            assert not (await client.get(Endpoint.GLOBAL)).error and calls==1
            await client.aclose()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure",["429","500","timeout","invalid_json","envelope","oversize","denied"])
def test_network_failures_are_bounded_and_structured(tmp_path,failure):
    async def scenario():
        calls=0
        def handler(request):
            nonlocal calls
            calls+=1
            if failure=="timeout":
                raise httpx.ReadTimeout("synthetic",request=request)
            if failure in {"429","500"}:
                return httpx.Response(int(failure),headers={"Retry-After":"0"})
            if failure=="invalid_json":
                return httpx.Response(200,content=b"{")
            if failure=="envelope":
                return httpx.Response(200,json=envelope(error_code=1001))
            if failure=="oversize":
                return httpx.Response(200,content=b"x"*1000)
            return httpx.Response(403,content=b"do not log provider message")
        async def no_sleep(_): pass
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client=CmcClient(store=Sidecar(tmp_path/"cmc_fail.sqlite"),keyless=True,http_client=http,
                             clock=lambda:AT,sleep=no_sleep,max_body_bytes=100)
            result=await client.get(Endpoint.GLOBAL)
            assert result.error and result.state!=S.VERIFIED and calls<=3
            assert "provider message" not in str(result)
            await client.aclose()
    asyncio.run(scenario())


def test_authentication_header_is_not_in_results_or_errors(tmp_path):
    async def scenario():
        def handler(request):
            assert request.headers["X-CMC_PRO_API_KEY"]=="synthetic-fixture-key"
            assert "synthetic-fixture-key" not in str(request.url)
            return httpx.Response(403)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client=CmcClient(store=Sidecar(tmp_path/"cmc_auth.sqlite"),api_key=SecretStr("synthetic-fixture-key"),http_client=http)
            result=await client.get(Endpoint.EXCHANGES)
            assert "synthetic-fixture-key" not in result.model_dump_json()
            assert result.state==S.UNAVAILABLE
            await client.aclose()
    asyncio.run(scenario())


def test_credit_budgets_survive_restart_and_bound_retries(tmp_path):
    async def scenario():
        path=tmp_path/"cmc_budget.sqlite"
        def handler(request): return httpx.Response(200,json=envelope())
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            for index in range(3):
                client=CmcClient(store=Sidecar(path),keyless=True,http_client=http,clock=lambda:AT,daily_credits=2,monthly_credits=2)
                result=await client.get(Endpoint.GLOBAL)
                assert (result.error=="credit_budget") == (index==2)
                await client.aclose()
    asyncio.run(scenario())


def test_circuit_breaker_and_unavailable_credentials(tmp_path):
    async def scenario():
        calls=0
        def handler(request):
            nonlocal calls
            calls+=1
            return httpx.Response(400)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client=CmcClient(store=Sidecar(tmp_path/"cmc_circuit.sqlite"),http_client=http,keyless=True)
            assert (await client.get(Endpoint.EXCHANGES)).state==S.UNAVAILABLE
            assert calls==0
            for _ in range(3): await client.get(Endpoint.GLOBAL,refresh=True)
            assert (await client.get(Endpoint.GLOBAL,refresh=True)).error=="circuit_open"
            assert calls==3
            await client.aclose()
    asyncio.run(scenario())


def test_pagination_and_contract_parameters(tmp_path):
    async def scenario():
        starts=[]
        def handler(request):
            start=int(request.url.params["start"])
            starts.append(start)
            return httpx.Response(200,json=envelope([{"id":start},{"id":start+1}] if start<5 else []))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client=CmcClient(store=Sidecar(tmp_path/"cmc_pages.sqlite"),keyless=True,http_client=http)
            pages=[p async for p in client.pages(Endpoint.MAP,{"limit":2},max_pages=4)]
            assert len(pages)==3 and starts==[1,3,5]
            for endpoint,params in ((Endpoint.GLOBAL,{"convert":"BTC"}), (Endpoint.MAP,{"start":0}),
                    (Endpoint.HISTORICAL_LISTINGS,{}), (Endpoint.CRYPTO_PAIRS,{}), (Endpoint.MAP,{"limit":6000}),
                    (Endpoint.GLOBAL,{"CMC_PRO_API_KEY":"synthetic"})):
                with pytest.raises(ValueError): await client.get(endpoint,params)
            await client.aclose()
    asyncio.run(scenario())


def test_sidecar_idempotency_causal_join_and_budgets(tmp_path):
    path=tmp_path/"cmc_store.sqlite"
    store=Sidecar(path,max_records=3,max_bytes=128*1024)
    identity=store.put("observation",{"synthetic":1},acquired_at=AT,source_at=AT)
    assert store.put("observation",{"synthetic":1},acquired_at=AT,source_at=AT)==identity
    assert len(store.causal("observation",AT))==1
    with pytest.raises(ValueError): store.put("observation",{"conflict":True},acquired_at=AT,identity=identity)
    store.put("observation",{"synthetic":2},acquired_at=AT+timedelta(seconds=1),source_at=AT)
    store.put("observation",{"synthetic":3},acquired_at=AT,source_at=AT+timedelta(seconds=1))
    assert len(store.causal("observation",AT))==1
    for i in range(20):
        store.put("bounded",{"synthetic":i,"payload":"x"*4096},acquired_at=AT+timedelta(seconds=i))
    with store.connect() as db:
        assert db.execute("SELECT count(*) FROM evidence").fetchone()[0]<=3
        assert db.execute("SELECT records FROM storage_stats").fetchone()[0]<=3
    assert path.stat().st_size<=128*1024
    with pytest.raises(ValueError): store.put("large","x"*65536,acquired_at=AT)


def test_sidecar_refuses_foreign_databases(tmp_path):
    with pytest.raises(ValueError): Sidecar(tmp_path/"main_live_runtime.sqlite")
    path=tmp_path/"cmc_foreign.sqlite"
    with sqlite3.connect(path) as db: db.execute("CREATE TABLE unrelated(value TEXT)")
    before=hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError): Sidecar(path)
    assert hashlib.sha256(path.read_bytes()).hexdigest()==before
