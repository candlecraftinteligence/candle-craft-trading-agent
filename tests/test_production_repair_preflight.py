"""Preflight budgets and error receipts through native clients; synthetic HTTP only."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.cmc.client import CmcClient as NativeClient
from app.cmc.validation_capture import Capture as NativeCapture, ValidationSettings
from app.cmc.endpoints import Endpoint
from scripts import cci_v2_repair_preflight as preflight

pytestmark = pytest.mark.no_auto_epoch


@pytest.mark.parametrize("status,identity,error", [
    (200, {"id": "xrp-xrp", "symbol": "XRP", "is_active": True}, None),
    (403, {}, "http_403"),
    (200, {"id": "wrong", "symbol": "XRP", "is_active": True}, "identity_response_contract"),
])
def test_preflight_retains_sanitized_bounded_native_evidence(tmp_path, monkeypatch, status, identity, error):
    async def exercise():
        def cmc(request):
            assert request.headers.get("X-CMC_PRO_API_KEY") == "SYNTHETIC-PREFLIGHT-KEY"
            return httpx.Response(200, json={"status": {"error_code": 0,
                "credit_count": 0 if request.url.path in {Endpoint.KEY_INFO.value, Endpoint.MAP.value} else 1},
                "data": {"fixture": True}})
        cmc_http = httpx.AsyncClient(transport=httpx.MockTransport(cmc))
        native_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(200, json={"synthetic": True})))
        paprika_http = httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
            httpx.Response(status, json=identity)))
        monkeypatch.setattr(preflight, "load_local_settings", lambda _: ValidationSettings(api_key="SYNTHETIC-PREFLIGHT-KEY"))
        monkeypatch.setattr(preflight, "CmcClient", lambda **kw: NativeClient(**kw, http_client=cmc_http))
        monkeypatch.setattr(preflight, "Capture", lambda root, client, **kw:
            NativeCapture(root, client, native_http=native_http, **kw))
        monkeypatch.setattr(preflight.httpx, "AsyncClient", lambda **_: paprika_http)
        args = SimpleNamespace(env_file=tmp_path/"unused.env", ledger=tmp_path/"cmc_preflight.sqlite",
            source=tmp_path/"captured", credit_cap=8)
        try:
            result = await preflight.capture(args)
        finally:
            await cmc_http.aclose()
            await native_http.aclose()
            await paprika_http.aclose()
        assert len(result["sources"]) == 10
        assert result["sources"][-1]["error"] == error
        assert result["native_request_attempts_this_session"] == 3
        assert sum(v["reserved_credits"] for k,v in result["usage"].items() if k.startswith("d:")) == 4
        for path in args.source.glob("*.json"):
            assert "SYNTHETIC-PREFLIGHT-KEY" not in path.read_text(encoding="utf-8")
        assert result["credit_cap"] == 8
    asyncio.run(exercise())
