"""Credential boundaries and replay of selected, real public provider responses."""
import asyncio
import json
import os
import shutil
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr

from app.cmc.authenticated_validation import analyze_capture, load_sources
from app.cmc.client import CmcClient
from app.cmc.endpoints import Endpoint
from app.cmc.storage import Sidecar
from app.cmc.validation_capture import Capture, load_local_settings
from tests.fixtures.cmc import AT

pytestmark = pytest.mark.no_auto_epoch
REAL = Path(__file__).parent / "fixtures" / "cmc_authenticated"


def test_local_credential_loading_never_exports_or_serializes(tmp_path, monkeypatch):
    monkeypatch.delenv("CMC_API_KEY", raising=False)
    path = tmp_path / "local.env"
    path.write_text('CMC_API_KEY="synthetic-${UNCHANGED}-$value"\n'
                    'CMC_DAILY_CREDITS=40\nCMC_MONTHLY_CREDITS=400\n', encoding="utf-8")
    before = dict(os.environ)
    settings = load_local_settings(path)
    assert dict(os.environ) == before
    assert settings.api_key.get_secret_value() == "synthetic-${UNCHANGED}-$value"
    assert (settings.daily_credits, settings.monthly_credits) == (40, 400)
    assert "synthetic" not in repr(settings) + settings.model_dump_json()


@pytest.mark.parametrize("body", ["", "CMC_API_KEY=", 'CMC_API_KEY="line1\nline2"'])
def test_missing_or_multiline_credential_fails_closed(tmp_path, body):
    path = tmp_path / "invalid.env"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ValueError, match="credential missing or malformed"):
        load_local_settings(path)


def test_capture_keeps_key_in_cmc_header_and_strips_account_fields(tmp_path):
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request)
            if request.url.host == "pro-api.coinmarketcap.com":
                assert request.headers["X-CMC_PRO_API_KEY"] == "synthetic-test-key"
                data = {"plan": {"account_id": "private", "credit_limit_monthly": 15000},
                        "usage": {"current_day": {"credits_used": 3, "key_id": "private"}}}
                return httpx.Response(200, json={"status": {"error_code": 0, "credit_count": 0}, "data": data})
            assert "X-CMC_PRO_API_KEY" not in request.headers
            return httpx.Response(200, json={"symbol": "BTCUSDT", "openInterest": "1", "time": 1})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = CmcClient(store=Sidecar(tmp_path / "cmc_capture.sqlite"),
                api_key=SecretStr("synthetic-test-key"), http_client=http, clock=lambda: AT)
            capture = Capture(tmp_path / "sources", client, native_http=http)
            await capture.cmc_get(Endpoint.KEY_INFO)
            await capture.native_get("/fapi/v1/openInterest", {"symbol": "BTCUSDT"})
            records = capture.index["sources"]
            assert "file" not in records[0]
            assert records[0]["plan"] == {"credit_limit_monthly": 15000}
            assert records[0]["usage"] == {"current_day": {"credits_used": 3}}
            _, sources = load_sources(capture.root)
            assert sources[1][1]["openInterest"] == "1"
            serialized = "".join(p.read_text() for p in capture.root.glob("*.json"))
            assert "synthetic-test-key" not in serialized and "private" not in serialized
            assert all(request.method == "GET" for request in calls)
            await capture.close()
    asyncio.run(scenario())


def test_echoed_credential_is_rejected_before_evidence_write(tmp_path):
    async def scenario():
        def handler(_):
            return httpx.Response(200, json={"status": {"error_code": 0},
                "data": {"echo": "synthetic-test-key"}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = CmcClient(store=Sidecar(tmp_path / "cmc_echo.sqlite"),
                api_key=SecretStr("synthetic-test-key"), http_client=http, clock=lambda: AT)
            capture = Capture(tmp_path / "sources", client, native_http=http)
            with pytest.raises(ValueError, match="credential-bearing response rejected"):
                await capture.cmc_get(Endpoint.GLOBAL)
            assert not list(capture.root.iterdir())
            await capture.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["429", "timeout", "json", "oversize"])
def test_native_failure_retries_and_budget_are_bounded(tmp_path, monkeypatch, failure):
    async def no_sleep(_): pass
    monkeypatch.setattr("app.cmc.validation_capture.asyncio.sleep", no_sleep)
    async def scenario():
        calls = 0
        def handler(request):
            nonlocal calls
            calls += 1
            if failure == "timeout": raise httpx.ReadTimeout("synthetic", request=request)
            if failure == "429": return httpx.Response(429)
            return httpx.Response(200, content=b"{" if failure == "json" else b"x" * (8*1024*1024+1))
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            client = CmcClient(store=Sidecar(tmp_path / "cmc_fail.sqlite"), http_client=http)
            capture = Capture(tmp_path / "sources", client, native_http=http, max_native_requests=2)
            assert await capture.native_get("/fapi/v1/openInterest") is None
            assert await capture.native_get("/fapi/v1/openInterest") is None
            assert calls == 2 and capture.index["sources"][-1]["error"] == "native_request_budget"
            with pytest.raises(ValueError, match="endpoint not allowed"):
                await capture.native_get("/fapi/v1/order")
            with pytest.raises(ValueError, match="parameter not allowed"):
                await capture.native_get("/fapi/v1/openInterest", {"api_key": "synthetic"})
            await capture.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("corruption", ["hash", "traversal", "acquisition"])
def test_offline_sources_reject_tampering(tmp_path, corruption):
    root = tmp_path / "capture"
    shutil.copytree(REAL, root)
    index_path = root / "index.json"
    index = json.loads(index_path.read_text())
    row = next(r for r in index["sources"] if r.get("file"))
    if corruption == "hash": row["sha256"] = "0" * 64
    if corruption == "traversal": row["file"] = "../outside.json"
    if corruption == "acquisition": row["acquired_at"] = "N/A"
    index_path.write_text(json.dumps(index))
    with pytest.raises(ValueError): load_sources(root)


def test_real_authenticated_contracts_keep_semantic_and_temporal_gaps():
    report = analyze_capture(REAL, REAL / "bindings.reviewed.json")
    assert report["authenticated_transport_verified"] is True
    assert report["distinct_authenticated_routes"] == 15
    assert report["reported_validation_credits"] == 20
    assert report["comparison_counts"] == {"UNVERIFIED": 7, "UNIT_MISMATCH": 2}
    assert report["derivatives_state_counts"] == {"VERIFIED": 3, "UNVERIFIED": 15}
    assert [m["instrument"]["market_id"] for m in report["mappings"]] == [47150, 47205, 67944]
    assert all(m["state"] == "VERIFIED" and m["symbol_collisions_in_map"] > 1 for m in report["mappings"])
    for row in report["derivatives"]:
        assert row["native_funding_interval_hours_reported"] == 8
        assert row["native_interval_provider_timestamp"] is None
        assert row["cmc_funding_interval_seconds"] is None
        assert not row["basis_diagnostics"]["verified_formula_or_sign"]
        native_oi = [o for o in row["native_observations"] if o["metric"] == "open_interest"]
        assert len(native_oi) == 1 and native_oi[0]["units"] == "base"
        assert native_oi[0]["state_at_validation_cutoff"] == "VERIFIED"
        funding = [o for o in row["native_observations"] if o["metric"] == "funding_rate"]
        assert len(funding) == 3 and all(o["state_at_validation_cutoff"] == "STALE" for o in funding)
    history = [c for c in report["context_sources"] if c["endpoint"] == Endpoint.HISTORICAL_LISTINGS.value]
    assert all(o["state"] == "UNVERIFIED" and "after requested" in o["reason"]
               for c in history for o in c["observations"])
    assert not report["production_rules_activated"] and not report["trading_decisions_modified"]


def test_replay_cli_does_not_read_credentials_or_use_network(tmp_path, monkeypatch, capsys):
    from scripts import cmc_authenticated_validation as cli
    def forbidden(*args, **kwargs): raise AssertionError("replay must remain offline")
    monkeypatch.setattr(cli, "load_local_settings", forbidden)
    monkeypatch.setattr(cli, "capture_sources", forbidden)
    output = tmp_path / "report.json"
    cli.main(["--replay", "--source", str(REAL), "--bindings", str(REAL / "bindings.reviewed.json"),
              "--env-file", str(tmp_path / "absent.env"), "--output", str(output)])
    assert json.loads(output.read_text())["distinct_authenticated_routes"] == 15
    assert '"reported_credits": 20' in capsys.readouterr().out


def test_historical_client_preserves_requested_day(tmp_path):
    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: httpx.Response(200,
                json={"status": {"error_code": 0}, "data": []}))) as http:
            client = CmcClient(store=Sidecar(tmp_path / "cmc_date.sqlite"), api_key=SecretStr("synthetic"),
                http_client=http, clock=lambda: AT)
            result = await client.get(Endpoint.HISTORICAL_LISTINGS, {"date": "2025-12-31", "limit": 3})
            assert result.request_params == {"date": "2025-12-31", "limit": 3}
            await client.aclose()
    asyncio.run(scenario())


def test_native_bridge_accepts_documented_category_but_rejects_identity_conflicts():
    from dataclasses import replace
    from app.cmc.native import native_observations
    from app.data.derivatives_source_evidence import acquisition_fact, describe_binance_open_interest
    from tests.fixtures.cmc import instrument
    batch = describe_binance_open_interest(requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": "10", "time": int(AT.timestamp()*1000)},
        request_params={"symbol": "BTCUSDT"}, capture_clock=None,
        acquisition=acquisition_fact(AT, quality="verified"))
    native = native_observations({"open_interest": batch}, instrument())
    assert len(native) == 1 and native[0].status_at(AT).value == "VERIFIED"
    for changes in ({"returned_symbol": "ETHUSDT"}, {"reason_codes": ("symbol_conflict",)},
                    {"returned_symbol": None}, {"reason_codes": ("unknown_identity_reason",)}):
        row = replace(batch.observations[0], identity=replace(batch.observations[0].identity, **changes))
        assert not native_observations({"open_interest": replace(batch, observations=(row,))}, instrument())
    row = batch.observations[0]
    quantity = replace(row.quantities[0], unit="quote_notional", quality="verified")
    row = replace(row, quantities=(quantity,))
    result = native_observations({"open_interest": replace(batch, observations=(row,))}, instrument())
    assert result[0].units == "USDT"
