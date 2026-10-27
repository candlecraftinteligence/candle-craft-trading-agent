"""Offline watch orchestration regressions. No scanner/provider or Telegram network."""
import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.alerts.telegram_lifecycle import TelegramLifecycleDeliveryService
from app.cmc import integration, runtime
from app.cmc.models import Snapshot
from app.cmc.normalize import normalize_context
from app.cmc.endpoints import Endpoint
from app.cmc.storage import Sidecar
from app.core.config import Settings
from app.pipeline.scanner_runner import ScannerRunner
from scripts import run_scan
from test_cmc_integration import FrozenNow, setup_symbol, stress_snapshot
from test_scanner_runner import FakeExchangeClient
from test_telegram_lifecycle_delivery_phase42 import FakeSender
from test_watch_mode import _bootstrap_watch_main, _noop_admin_report
from tests.fixtures.cmc import AT, fetched
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY


@pytest.mark.parametrize("case", ["healthy", "recorded_context", "stale", "empty_identity", "missing_key", "credit_exhausted",
    "storage_unavailable", "malformed_activation", "off"])
def test_real_watch_runner_preserves_cutoff_through_public_boundary(tmp_path, monkeypatch, case):
    async def scenario():
        db, _ = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
        store = Sidecar(tmp_path / "cmc_watch.sqlite")
        analysis_at = AT
        global_context = normalize_context(fetched(Endpoint.GLOBAL, {"last_updated":AT.isoformat(),
            "quote":{"USD":{"last_updated":AT.isoformat(), "total_market_cap":100,
                "total_market_cap_yesterday_percentage_change":-4}}}))
        snapshot = stress_snapshot().model_copy(update={"observations":(*stress_snapshot().observations,*global_context)})
        if case == "recorded_context":
            from pathlib import Path
            from app.cmc.authenticated_validation import as_fetch, load_sources
            from app.cmc.models import utc
            _, sources = load_sources(Path(__file__).parent/"fixtures/cmc_authenticated")
            analysis_at = max(utc(r["acquired_at"]) for r,_ in sources)
            observations = []
            for endpoint in (Endpoint.GLOBAL,Endpoint.LIQUIDATIONS):
                r,p = max(((r,p) for r,p in sources if r["endpoint"] == endpoint.value),
                    key=lambda value:value[0]["acquired_at"])
                observations.extend(normalize_context(as_fetch(r,p)))
            snapshot = Snapshot(observations=tuple(observations),batch_id="recorded:authenticated-global-context")
        if case == "stale":
            snapshot = snapshot.model_copy(update={"observations":tuple(o.model_copy(update={
                "source_at":AT-timedelta(seconds=400)}) for o in snapshot.observations)})
        if case in {"missing_key", "credit_exhausted"}:
            from app.cmc.client import CmcClient
            from app.cmc.collection import Collector
            from app.cmc.profiles import profile_for
            from pydantic import SecretStr
            import httpx
            provider_calls = []
            def forbidden(request):
                provider_calls.append(request.url.path)
                assert case == "credit_exhausted" and request.url.path == Endpoint.MAP.value
                return httpx.Response(200,json={"status":{"error_code":0,"credit_count":0},"data":[]})
            async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as http:
                client = CmcClient(store=store,http_client=http,clock=lambda:AT,
                    api_key=SecretStr("synthetic") if case == "credit_exhausted" else None,
                    daily_credits=0,monthly_credits=0)
                snapshot = await Collector(client,store,profile=profile_for("DECISION_FOCUSED")).collect()
                assert snapshot.errors and all(o.status_at(AT).value != "VERIFIED" for o in snapshot.observations)
                assert provider_calls == ([Endpoint.MAP.value] if case == "credit_exhausted" else [])
                await client.aclose()
        if case == "empty_identity":
            assert all(o.instrument is None for o in snapshot.observations)
        class FailedStore:
            def put(self,*a,**k): raise OSError("synthetic storage outage")
        service = SimpleNamespace(store=FailedStore() if case == "storage_unavailable" else store, snapshot=snapshot)
        async def ready(*args, **kwargs): return service
        monkeypatch.setattr(runtime, "begin_collection", ready)
        monkeypatch.setattr(integration, "service_for", ready)
        class SelectionNow(FrozenNow):
            @classmethod
            def now(cls,tz=None): return analysis_at+timedelta(seconds=2)
        monkeypatch.setattr(integration, "datetime", SelectionNow)
        for name in ("GLOBAL_CONTEXT_ENABLED", "MACRO_EVENT_CONTEXT_ENABLED", "BTC_D_CONTEXT_ENABLED"):
            monkeypatch.setenv(name, "false")
        mode = "off" if case == "off" else "shadow"
        monkeypatch.setenv("CMC_MODE", mode)
        monkeypatch.setattr(run_scan, "_route_admin_report", _noop_admin_report)
        sender = FakeSender()
        seen = []
        class Runner(ScannerRunner):
            async def _scan_symbol(self, *args, **kwargs): return setup_symbol()
        monkeypatch.setattr(run_scan, "_scanner_runner_with_context", lambda *a, **k:
            Runner(exchange_client=FakeExchangeClient({}), clock=lambda: analysis_at))
        class Delivery(TelegramLifecycleDeliveryService):
            async def deliver_for_run(self, result, **kwargs):
                seen.append(result)
                return await super().deliver_for_run(result, **kwargs)
        activation = tmp_path/"activation.json"
        if case == "malformed_activation": activation.write_text("{broken")
        settings = Settings(_env_file=None, cmc_mode=mode,
            cmc_activation_manifest=activation if case == "malformed_activation" else None)
        monkeypatch.setattr(run_scan, "_telegram_manual_signal_settings", lambda: settings)
        monkeypatch.setattr(run_scan, "TelegramLifecycleDeliveryService", lambda **kwargs:
            Delivery(database_path=db, settings=settings, sender=sender))
        await run_scan.main(["--symbols", "BTCUSDT", "--watch", "--watch-max-iterations", "2", "--watch-interval-sec", "0.01",
            "--database-path", str(db), "--disable-lifecycle", "--disable-regime-filter", "--no-cache",
            "--runtime-epoch-id", SYNTHETIC_IDENTITY.epoch_id,
            "--runtime-epoch-cutoff-at", SYNTHETIC_IDENTITY.cutoff_at,
            "--runtime-reviewed-release-sha", SYNTHETIC_IDENTITY.reviewed_release_sha,
            "--runtime-generation-binding", SYNTHETIC_IDENTITY.generation_binding,
            "--telegram-signals"])
        assert len(seen) == 2 and all(r.config.decision_timestamp == analysis_at for r in seen)
        records = store.causal("policy_batch", analysis_at+timedelta(seconds=2))
        if case in {"off", "storage_unavailable", "malformed_activation"}:
            assert not records
        else:
            first = next(batch[0] for batch in records if batch[0]["evaluation"]["recommendations"])
            assert first["evaluation"]["hypothetical_veto"] == (case in {"healthy","empty_identity"})
            assert first["context"]["cci_original_decision_at"] == analysis_at.isoformat()
            assert first["context"]["cmc_selection_at"] == (analysis_at+timedelta(seconds=2)).isoformat()
            if case == "recorded_context":
                assert all(next(f for f in first["features"] if f["name"] == name)["state"] == "VERIFIED"
                    for name in ("CmcGlobalMarketRisk","CmcLiquidationPressure","CmcLiquidationImbalance"))
            if case in {"healthy","empty_identity"}:
                adverse = next(r for r in first["evaluation"]["recommendations"] if r["rule_id"] == "adverse_regime")
                assert adverse["action"] == "PRIORITY_DECREASE" and not adverse["enforced"]
            assert not any(r["enforced"] for r in first["evaluation"]["recommendations"])
        assert all(r.results[0].lifecycle_state.plan_version_id == "synthetic:plan" for r in seen)
        assert len(sender.messages) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["later_acquisition", "later_source", "missing_cutoff", "stale"])
def test_original_cutoff_cannot_receive_later_evidence(tmp_path, change):
    async def scenario():
        from test_telegram_lifecycle_delivery_phase42 import _run_result
        from tests.fixtures.cmc import candidate
        snapshot = stress_snapshot()
        updates = {"acquired_at": AT + timedelta(seconds=1)} if change == "later_acquisition" else (
            {"source_at": AT + timedelta(seconds=1), "acquired_at": AT + timedelta(seconds=1)}
            if change == "later_source" else {"source_at": AT - timedelta(seconds=400)})
        if change != "missing_cutoff":
            snapshot = snapshot.model_copy(update={"observations": tuple(
                o.model_copy(update=updates) for o in snapshot.observations)})
        result = _run_result(setup_symbol())
        result = result.model_copy(update={"config": result.config.model_copy(update={
            "decision_timestamp": None if change == "missing_cutoff" else AT})})
        service = SimpleNamespace(snapshot=snapshot, store=Sidecar(tmp_path / "cmc_causal.sqlite"))
        output = await integration.evaluate_delivery_run(result, Settings(_env_file=None, cmc_mode="shadow"),
            service=service, candidate_builder=lambda _: candidate(), decision_at=AT+timedelta(seconds=2))
        value = output.results[0].cmc_evaluation
        assert value is None or not value.hypothetical_veto
    asyncio.run(scenario())


def test_resumed_analysis_cannot_inherit_later_run_regime(tmp_path):
    async def scenario():
        from test_telegram_lifecycle_delivery_phase42 import _run_result
        from tests.fixtures.cmc import candidate
        old = AT-timedelta(seconds=60)
        symbol = setup_symbol().model_copy(update={"evaluation_origin_kind":"resumed_payload",
            "lifecycle_decision_timestamp":old})
        result = _run_result(symbol)
        result = result.model_copy(update={"config":result.config.model_copy(update={"decision_timestamp":AT}),
            "market_regime":result.market_regime.model_copy(update={"enabled":True,"missing_data":(),"unverified_data":()})})
        snapshot = stress_snapshot().model_copy(update={"observations":tuple(o.model_copy(update={
            "source_at":old,"acquired_at":old}) for o in stress_snapshot().observations)})
        store = Sidecar(tmp_path/"cmc_resumed.sqlite")
        output = await integration.evaluate_delivery_run(result,Settings(_env_file=None,cmc_mode="shadow"),
            service=SimpleNamespace(store=store,snapshot=snapshot),candidate_builder=lambda _:candidate(),decision_at=AT)
        assert output.results[0].cmc_evaluation.candidate.original_decision_at == old
        record = store.causal("policy_batch",AT+timedelta(days=365))[0][0]
        assert record["context"]["market_regime_status"] == "UNVERIFIED"
        assert not any(f["name"] == "CmcRegimeCompatibility" for f in record["features"])
    asyncio.run(scenario())
