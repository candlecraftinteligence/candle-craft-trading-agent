"""Synthetic CMC acquisition and scanner injection with isolated sidecar state."""
import asyncio
from datetime import timedelta
from decimal import Decimal as D
from types import SimpleNamespace

import httpx
import pytest
from pydantic import SecretStr

from app.cmc.client import CmcClient
from app.cmc.collection import Collector
from app.cmc.endpoints import Endpoint, reserved_credits
from app.cmc.models import Snapshot, EvidenceState
from app.cmc.normalize import normalize_context
from app.cmc.profiles import profile_for, CollectionProfile, Family
from app.cmc.scanner_context import scanner_context
from app.cmc.storage import Sidecar
from app.core.config import Settings
from tests.fixtures.cmc import fetched
from tests.test_integrated_signal_product import _scan, AT
from tests.test_strict_market_cap_universe import _strict_resolution, _asset

pytestmark = pytest.mark.no_auto_epoch


def global_data(at=AT):
    return {"btc_dominance": 60, "eth_dominance": 15, "last_updated": at.isoformat(),
        "quote": {"USD": {"total_market_cap": "1000000", "total_market_cap_yesterday_percentage_change": "1", "last_updated": at.isoformat()}}}


def test_403_entitlement_is_not_reprobed_even_after_client_restart(tmp_path):
    async def exercise():
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            return httpx.Response(403, json={"status": {"error_code": 1006}})
        store = Sidecar(tmp_path/"cmc_denied.sqlite")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            for _ in range(2):
                client = CmcClient(store=store, api_key=SecretStr("SYNTHETIC-KEY"), http_client=http, clock=lambda: AT)
                for _ in range(2):
                    result = await client.get(Endpoint.LIQUIDATIONS, refresh=True)
                    assert result.error == "http_403_entitlement"
                await client.aclose()
        assert calls == 1
    asyncio.run(exercise())


def test_runtime_shadow_credit_profile_fits_500_daily_and_15000_monthly():
    profile = profile_for("RUNTIME_SHADOW")
    daily = sum((86400//f.seconds)*reserved_credits(f.endpoint, {"limit": f.limit} if f.limit else {})*f.max_pages
        for f in profile.families)
    assert daily == 433
    assert daily*30 == 12990 and 500-daily == 67
    settings = Settings(_env_file=None, cmc_mode="shadow", cmc_daily_credits=500,
        cmc_monthly_credits=15000, cmc_collection_profile="RUNTIME_SHADOW")
    assert settings.market_cap_provider == "coinpaprika"
    assert settings.order_execution_enabled is False


def test_collector_enriches_rejected_scanner_candidate_without_changing_decisions(tmp_path, monkeypatch):
    at = AT-timedelta(minutes=15)
    async def acquire():
        def handler(request):
            assert request.url.path == Endpoint.GLOBAL.value
            return httpx.Response(200, json={"status": {"error_code": 0, "credit_count": 1}, "data": global_data(at)})
        store = Sidecar(tmp_path/"cmc_injected.sqlite")
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = CmcClient(store=store, api_key=SecretStr("SYNTHETIC-KEY"), http_client=http, clock=lambda: at)
            collector = Collector(client, store, profile=CollectionProfile(name="CUSTOM",
                families=(Family(endpoint=Endpoint.GLOBAL, seconds=300),)))
            snapshot = await collector.collect()
            assert "identity_manifest_empty:asset_enrichment_unavailable" in snapshot.errors
            assert store.causal("snapshot", AT, limit=1)
            await client.aclose()
            return snapshot
    snapshot = asyncio.run(acquire())
    async def begin(_settings=None):
        return SimpleNamespace(snapshot=snapshot)
    monkeypatch.setattr("app.cmc.runtime.begin_collection", begin)
    baseline, _ = _scan(D(".750"), "long", scenario="before_contact", room=True)
    enriched, _ = _scan(D(".750"), "long", scenario="before_contact", room=True, cmc_mode="shadow")
    before, after = baseline.results[0], enriched.results[0]
    assert after.trade_idea is None and after.rejection_reason == before.rejection_reason
    assert after.strategy_diagnostics == before.strategy_diagnostics
    context = after.cmc_context
    assert context["setup_authority"] is False
    assert context["batch_id"] == snapshot.batch_id
    assert context["status_counts"]["VERIFIED"] == 4
    cap = next(o for o in context["observations"] if o["metric"] == "market_cap")
    assert cap["value"] == "1000000" and cap["source_at"] == at.isoformat()
    assert cap["provenance"] != "N/A"
    assert enriched.results[0].model_dump(mode="json")["cmc_context"] == context
    import json
    import sqlite3
    from app.storage.repositories import store_scan_result
    from app.storage.scan_payloads import load_logical_scan_payload
    from tests.runtime_epoch_support import bootstrap_operational_test_database, register_synthetic_scan_run, SYNTHETIC_IDENTITY
    database = bootstrap_operational_test_database(tmp_path/"synthetic_cmc_scan.db")
    register_synthetic_scan_run(database, "synthetic-cmc-stored")
    store_scan_result(database, enriched, run_id="synthetic-cmc-stored", expected_identity=SYNTHETIC_IDENTITY)
    with sqlite3.connect(database) as connection:
        restored = load_logical_scan_payload(connection, "synthetic-cmc-stored")
        assert restored["results"][0]["cmc_context"] == context
        row = connection.execute("SELECT raw_payload_format,runtime_stats_json FROM scan_runs WHERE run_id=?",
            ("synthetic-cmc-stored",)).fetchone()
        assert row[0] == "symbol_refs_v1"
        assert json.loads(row[1])["cmc_acquisition"]["mode"] == "shadow"


def test_cmc_observations_cannot_be_backdated_or_freshened_at_assessment():
    fetch = fetched(Endpoint.GLOBAL, global_data()).model_copy(update={"acquired_at": AT})
    snapshot = Snapshot(observations=normalize_context(fetch), batch_id="synthetic:cmc")
    late = scanner_context(snapshot, symbol="BTCUSDT", cutoff=AT-timedelta(seconds=1), assessed_at=AT)
    stale = scanner_context(snapshot, symbol="BTCUSDT", cutoff=AT, assessed_at=AT+timedelta(seconds=301))
    assert all(o["value"] == "N/A" for o in late["observations"])
    assert all(o["status"] == "STALE" and o["value"] == "N/A" for o in stale["observations"])


def test_xrp_provider_id_stays_bound_when_an_unrelated_xrp_ticker_appears():
    assets = [_asset(1, symbol="XRP", asset_id="xrp-xrp"),
              _asset(2, symbol="XRP", asset_id="synthetic-imposter-xrp")]
    result = _strict_resolution(assets, ["XRP"], universe_size=1)
    assert result.resolved_symbols == ("XRPUSDT",)
    assert result.diagnostics["qualified_membership"][0]["provider_id"] == "xrp-xrp"
