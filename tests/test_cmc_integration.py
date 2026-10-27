import asyncio
import json
from datetime import datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

import app.cmc.integration as integration
from app.alerts.telegram_lifecycle import TelegramLifecycleDeliveryService, telegram_alert_decision_for_symbol
from app.cmc.client import CmcClient
from app.cmc.collection import Collector, IdentityManifest, Semantics
from app.cmc.identity import AssetBinding
from app.cmc.models import EvidenceState as S, Mode, Snapshot
from app.cmc.normalize import normalize_context
from app.cmc.policy import evaluate
from app.cmc.runtime import Service, service_for
from app.cmc.storage import Sidecar
from app.core.config import Settings
from app.lifecycle.models import SetupLifecycleState
from app.pipeline.scanner_runner import ScannerRunner
from tests.fixtures.cmc import AT, approval, candidate, feature, fetched, pair
from app.cmc.endpoints import Endpoint
from test_scanner_runner import FakeExchangeClient, _config, _trend_candles_with_valid_setup
from test_telegram_lifecycle_delivery_phase42 import FakeSender, _run_result, _symbol, _with_lifecycle_fields


def stress_snapshot():
    q={"symbol":"USD","crypto_id":2781,"last_updated":AT.isoformat()}
    for suffix,total in (("1h",100),("4h",200),("24h",400)):
        q.update({f"total_liquidations_{suffix}":total,f"long_liquidations_{suffix}":total*Decimal("0.9"),
                  f"short_liquidations_{suffix}":total*Decimal("0.1")})
    return Snapshot(observations=normalize_context(fetched(Endpoint.LIQUIDATIONS,{"quotes":[q]})),batch_id="synthetic:stress")


class FrozenNow(datetime):
    @classmethod
    def now(cls,tz=None): return AT


def setup_symbol():
    return _with_lifecycle_fields(_symbol(SetupLifecycleState.CONFIRMED,previous=SetupLifecycleState.TRIGGERED),
        setup_id="synthetic:setup",plan_version_id="synthetic:plan",direction="long")


@pytest.mark.parametrize("mode",list(Mode))
def test_actual_public_delivery_modes_preserve_lifecycle_and_dedup(tmp_path,monkeypatch,mode):
    async def scenario():
        fake=SimpleNamespace(store=Sidecar(tmp_path/"cmc_e2e.sqlite"),snapshot=stress_snapshot())
        async def service(_): return fake
        monkeypatch.setattr(integration,"service_for",service)
        monkeypatch.setattr(integration,"datetime",FrozenNow)
        activation_file=tmp_path/"activation.json"
        activation_file.write_text(json.dumps([approval("liquidation_stress").model_dump(mode="json")]),encoding="utf-8")
        settings=Settings(_env_file=None,cmc_mode=mode.value,cmc_activation_manifest=activation_file)
        symbol=setup_symbol()
        before=symbol.model_dump(mode="json")
        result=_run_result(symbol)
        result=result.model_copy(update={"config":result.config.model_copy(update={"decision_timestamp":AT,"cmc_mode":mode.value})})
        sender=FakeSender()
        delivery=TelegramLifecycleDeliveryService(database_path=tmp_path/"public.db",settings=settings,sender=sender)
        first=await delivery.deliver_for_run(result,scan_run_id="synthetic:first")
        second=await delivery.deliver_for_run(result,scan_run_id="synthetic:repeat")
        assert first.sent==(0 if mode==Mode.DECISION else 1)
        assert second.sent==0
        assert len(sender.messages)==(0 if mode==Mode.DECISION else 1)
        assert symbol.model_dump(mode="json")==before
        if mode in {Mode.SHADOW,Mode.DECISION}:
            records=fake.store.causal("policy_batch",AT)
            assert records and any(batch[0]["evaluation"]["hypothetical_veto"] for batch in records)
    asyncio.run(scenario())


def test_policy_persistence_failure_retains_canonical_decision(tmp_path,monkeypatch):
    async def scenario():
        class FailedStore:
            def put(self,*args,**kwargs): raise OSError("synthetic disk unavailable")
        fake=SimpleNamespace(store=FailedStore(),snapshot=stress_snapshot())
        symbol=setup_symbol()
        result=_run_result(symbol).model_copy(update={"config":_run_result(symbol).config.model_copy(update={"decision_timestamp":AT})})
        settings=Settings(_env_file=None,cmc_mode="decision")
        output=await integration.evaluate_delivery_run(result,settings,service=fake,candidate_builder=lambda _:candidate())
        assert output.results[0].cmc_evaluation is None
        assert output.model_dump()==result.model_dump()
    asyncio.run(scenario())


def test_veto_cannot_attach_to_another_plan_or_followup(monkeypatch):
    monkeypatch.setattr(integration,"datetime",FrozenNow)
    symbol=setup_symbol()
    evaluation=evaluate(candidate(),(feature(),),mode=Mode.DECISION,as_of=AT,activations=(approval(),))
    symbol=symbol.model_copy(update={"cmc_evaluation":evaluation})
    assert integration.veto_applies(symbol,alert_type="SIGNAL_CONFIRMED",previously_public=False)
    for alert in ("TP1_HIT","SL_HIT","LIMIT_HIT","INVALIDATED","EXPIRED"):
        assert not integration.veto_applies(symbol,alert_type=alert,previously_public=False)
    assert not integration.veto_applies(symbol,alert_type="SIGNAL_CONFIRMED",previously_public=True)
    other=symbol.model_copy(update={"lifecycle_state":symbol.lifecycle_state.model_copy(update={"plan_version_id":"different"})})
    assert not integration.veto_applies(other,alert_type="SIGNAL_CONFIRMED",previously_public=False)


def test_off_service_has_no_files_or_provider_activity(tmp_path):
    async def scenario():
        settings=Settings(_env_file=None,cmc_sidecar_path=tmp_path/"cmc_off.sqlite")
        assert await service_for(settings) is None
        assert not settings.cmc_sidecar_path.exists()
    asyncio.run(scenario())


def test_background_schedule_coalesces_and_outage_does_not_block(tmp_path):
    async def scenario():
        calls=0
        class SlowCollector:
            client=SimpleNamespace(aclose=lambda:asyncio.sleep(0))
            async def collect(self):
                nonlocal calls
                calls+=1
                await asyncio.sleep(60)
        service=Service(SlowCollector(),Sidecar(tmp_path/"cmc_background.sqlite"))
        for _ in range(10): service.schedule()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert calls==1 and service.snapshot.observations==()
        await asyncio.wait_for(service.close(),1)
    asyncio.run(scenario())


def test_all_scanner_modes_preserve_canonical_results_with_absent_provider(monkeypatch):
    async def scenario():
        import app.cmc.runtime as runtime
        async def no_provider(): return None
        monkeypatch.setattr(runtime,"begin_collection",no_provider)
        outputs=[]
        for mode in Mode:
            client=FakeExchangeClient({"BTCUSDT":_trend_candles_with_valid_setup()},candle_as_of=AT)
            result=await ScannerRunner(exchange_client=client,clock=lambda:AT).run(
                _config(["BTCUSDT"],enable_strategy_output=False,decision_timestamp=AT,cmc_mode=mode.value))
            item=result.results[0].model_dump(mode="json")
            item.pop("runtime_seconds")
            outputs.append(item)
        assert all(item==outputs[0] for item in outputs)
    asyncio.run(scenario())


def test_fixture_collection_persists_raw_normalized_identity_and_usage(tmp_path):
    async def scenario():
        data={
            Endpoint.LISTINGS:[{"id":1,"cmc_rank":1,"last_updated":AT.isoformat(),"quote":[{"id":2781,"symbol":"USD","percent_change_24h":1,"last_updated":AT.isoformat()}]}],
            Endpoint.MAP:[{"id":1,"slug":"bitcoin","is_active":1}],
            Endpoint.GLOBAL:{"btc_dominance":60,"eth_dominance":15,"last_updated":AT.isoformat(),"quote":{"USD":{"last_updated":AT.isoformat(),"total_market_cap":1000000,"total_market_cap_yesterday_percentage_change":1}}},
            Endpoint.FEAR_GREED:{"value":40,"update_time":AT.isoformat()},
            Endpoint.ALTCOIN:{"altcoin_index":75,"snapshot_time":AT.isoformat()},
            Endpoint.EXCHANGES:{"exchanges":[{"exchange_id":270,"exchange_slug":"binance","quotes":[]}]}, Endpoint.EXCHANGE_PAIRS:{"exchange_id":270,"exchange_slug":"binance","market_pairs":[pair()]},
            Endpoint.LIQUIDATIONS:{"quotes":[]}, Endpoint.EXCHANGE_LIQUIDATIONS:{"exchanges":[]},
            Endpoint.CRYPTO_LIQUIDATIONS:{"cryptocurrencies":[]}, Endpoint.CATEGORIES:[],
        }
        def handler(request):
            endpoint=Endpoint(request.url.path)
            return httpx.Response(200,json={"status":{"error_code":0,"credit_count":1},"data":data[endpoint]})
        class Metadata:
            async def snapshot(self): return {"symbols":[{"symbol":"BTCUSDT","baseAsset":"BTC","quoteAsset":"USDT","marginAsset":"USDT","status":"TRADING","contractType":"PERPETUAL"}]}
        manifest=IdentityManifest(bindings=(AssetBinding(cmc_id=1,cmc_slug="bitcoin",binance_base="BTC",approved=True,
            evidence_ref="synthetic:review",approved_at=AT-timedelta(days=1),
            contract_symbol="BTCUSDT", exchange_id=270, market_id=100, owner_approval_ref="synthetic:owner",
            acquired_at=AT-timedelta(days=2), reviewed_at=AT-timedelta(days=1), expires_at=AT+timedelta(days=1)),),semantics=(Semantics(market_id=100,source_contract_ref="synthetic:contract",
                oi_units="base",oi_counting_convention="open_contract_quantity",funding_semantics="settled_funding",funding_interval_seconds=28800,
                verified_at=AT-timedelta(seconds=1),expires_at=AT+timedelta(days=1)),))
        store=Sidecar(tmp_path/"cmc_collection.sqlite")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            from pydantic import SecretStr
            client=CmcClient(store=store,api_key=SecretStr("synthetic-fixture-key"),http_client=http,clock=lambda:AT)
            snapshot=await Collector(client,store,manifest=manifest,metadata_provider=Metadata()).collect()
            assert not snapshot.errors
            assert any(o.metric=="open_interest" and o.status_at(AT)==S.VERIFIED for o in snapshot.observations)
            assert store.causal("raw",AT) and store.causal("mapping",AT) and store.causal("collection_batch",AT)
            restored=Snapshot.model_validate(store.causal("snapshot",AT)[0])
            assert restored==snapshot
            await client.aclose()
    asyncio.run(scenario())


def test_provider_outage_after_veto_does_not_leave_a_permanent_gate(tmp_path,monkeypatch):
    async def scenario():
        fake=SimpleNamespace(store=Sidecar(tmp_path/"cmc_recovery.sqlite"),snapshot=stress_snapshot())
        async def service(_): return fake
        monkeypatch.setattr(integration,"service_for",service)
        monkeypatch.setattr(integration,"datetime",FrozenNow)
        path=tmp_path/"rules.json"
        path.write_text(json.dumps([approval("liquidation_stress").model_dump(mode="json")]),encoding="utf-8")
        settings=Settings(_env_file=None,cmc_mode="decision",cmc_activation_manifest=path)
        symbol=setup_symbol()
        result=_run_result(symbol)
        result=result.model_copy(update={"config":result.config.model_copy(update={"decision_timestamp":AT})})
        sender=FakeSender()
        delivery=TelegramLifecycleDeliveryService(database_path=tmp_path/"public.db",settings=settings,sender=sender)
        assert (await delivery.deliver_for_run(result,scan_run_id="synthetic:veto")).sent==0
        fake.snapshot=Snapshot(errors=("synthetic:provider_outage",))
        recovered=await delivery.deliver_for_run(result,scan_run_id="synthetic:outage")
        assert recovered.sent==1 and len(sender.messages)==1
    asyncio.run(scenario())


def test_mode_serialization_and_key_safety():
    settings=Settings(_env_file=None,cmc_api_key="synthetic-fixture-key")
    assert settings.cmc_mode=="off" and not settings.cmc_keyless
    assert "synthetic-fixture-key" not in repr(settings)
    assert "synthetic-fixture-key" not in settings.model_dump_json()
    assert "cmc_mode" not in _config(["BTCUSDT"]).model_dump()
    assert _config(["BTCUSDT"],cmc_mode="shadow").model_dump()["cmc_mode"]=="shadow"


def test_full_candidate_batch_stays_within_sidecar_payload_budget(tmp_path,monkeypatch):
    async def scenario():
        from tests.fixtures.cmc import obs
        snapshot=stress_snapshot()
        snapshot=snapshot.model_copy(update={"observations":(*snapshot.observations,
            *(obs("unused",str(i)) for i in range(1500)))})
        fake=SimpleNamespace(store=Sidecar(tmp_path/"cmc_bounded_policy.sqlite"),snapshot=snapshot)
        monkeypatch.setattr(integration,"datetime",FrozenNow)
        symbol=setup_symbol()
        result=_run_result(symbol)
        result=result.model_copy(update={"results":tuple(symbol.model_copy(update={"symbol":f"SYNTHETIC{i}"}) for i in range(100)),
            "config":result.config.model_copy(update={"decision_timestamp":AT})})
        output=await integration.evaluate_delivery_run(result,Settings(_env_file=None,cmc_mode="shadow"),service=fake,
            candidate_builder=lambda s:candidate(symbol=s.symbol,setup_id=s.symbol,economic_plan_id=s.symbol))
        assert all(r.cmc_evaluation and r.cmc_evaluation.hypothetical_veto for r in output.results)
        persisted=fake.store.causal("policy_batch",AT)[0]
        assert len(persisted)==100 and len(json.dumps(persisted).encode())<2*1024*1024
    asyncio.run(scenario())
