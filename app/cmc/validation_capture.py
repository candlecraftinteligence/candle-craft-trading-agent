"""Explicit DEV-only source capture. Never imported by the scanner runtime."""
from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values
from pydantic import Field, SecretStr

from app.cmc.client import CmcClient
from app.cmc.endpoints import Endpoint
from app.cmc.models import Frozen, encoded
from app.cmc.storage import Sidecar

BINANCE_PATHS = frozenset({
    "/fapi/v1/exchangeInfo", "/fapi/v1/premiumIndex", "/fapi/v1/fundingInfo",
    "/fapi/v1/openInterest", "/fapi/v2/ticker/price", "/fapi/v1/fundingRate",
})


class ValidationSettings(Frozen):
    api_key: SecretStr = Field(exclude=True, repr=False)
    daily_credits: int = Field(default=100, ge=0)
    monthly_credits: int = Field(default=3000, ge=0)


def load_local_settings(path: Path) -> ValidationSettings:
    """Read the named file in memory; never call load_dotenv or mutate os.environ."""
    if path.stat().st_size > 256 * 1024:
        raise ValueError("credential file exceeds size budget")
    values = dotenv_values(path, interpolate=False)
    key = values.get("CMC_API_KEY") or ""
    if not key or any(c in key for c in "\r\n"):
        raise ValueError("CMC credential missing or malformed")
    return ValidationSettings(api_key=SecretStr(key),
        daily_credits=int(values.get("CMC_DAILY_CREDITS") or 100),
        monthly_credits=int(values.get("CMC_MONTHLY_CREDITS") or 3000))


def quota_summary(data):
    """Keep plan limits/usage, never account identifiers or an echoed credential."""
    result = {}
    if not isinstance(data, dict):
        return result
    allowed = {"credit_limit_daily", "credit_limit_monthly", "credit_limit_monthly_reset",
               "rate_limit_minute", "credits_used", "credits_left"}
    for group in ("plan", "usage"):
        block = data.get(group, {})
        if not isinstance(block, dict):
            continue
        result[group] = {k: v for k, v in block.items() if k in allowed}
        for period in ("current_day", "current_month"):
            if isinstance(block.get(period), dict):
                result[group][period] = {k: v for k, v in block[period].items() if k in allowed}
    return result


class Capture:
    def __init__(self, root: Path, cmc: CmcClient, *, native_http=None, max_native_requests=24):
        self.root, self.cmc = root, cmc
        root.mkdir(parents=True, exist_ok=True)
        index_path = root / "index.json"
        self.index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {
            "started_at": datetime.now(UTC).isoformat(), "sources": []}
        self._http = native_http or httpx.AsyncClient(timeout=6, follow_redirects=False)
        self._owns_http = native_http is None
        self.max_native_requests = max_native_requests
        self.native_attempts = 0

    def save(self):
        self.index["updated_at"] = datetime.now(UTC).isoformat()
        self.index["usage"] = self.cmc.store.usage(datetime.now(UTC))
        (self.root / "index.json").write_text(encoded(self.index) + "\n", encoding="utf-8")

    def _persist(self, record, body: bytes):
        # The caller supplies only public response bodies; headers are never written.
        name = f"{len(self.index['sources'])}_{record['provider'].lower()}.json"
        (self.root / name).write_bytes(body)
        record.update(file=name, sha256=hashlib.sha256(body).hexdigest())

    async def cmc_get(self, endpoint: Endpoint, params=None, *, refresh=False):
        result = await self.cmc.get(endpoint, params or {}, refresh=refresh)
        record = {"provider": "CoinMarketCap", "endpoint": endpoint.value, "params": params or {},
            "acquired_at": result.acquired_at.isoformat(), "error": result.error,
            "authenticated": result.authenticated, "credits_reported": result.credits_reported}
        if result.raw_text is not None:
            if self.cmc._key and self.cmc._key.get_secret_value() in result.raw_text:
                raise ValueError("credential-bearing response rejected")
            if endpoint == Endpoint.KEY_INFO:
                record.update(quota_summary(result.data))
            else:
                self._persist(record, result.raw_text.encode("utf-8"))
        self.index["sources"].append(record)
        self.save()
        return result

    async def native_get(self, path: str, params=None):
        if path not in BINANCE_PATHS:
            raise ValueError("public validation endpoint not allowed")
        params = dict(params or {})
        if set(params) - {"symbol", "limit"}:
            raise ValueError("public validation parameter not allowed")
        record = {"provider": "Binance", "endpoint": path, "params": params, "error": None}
        payload = None
        for attempt in range(2):
            if self.native_attempts >= self.max_native_requests:
                record["error"] = "native_request_budget"
                break
            self.native_attempts += 1
            try:
                body, code = await asyncio.wait_for(self._native_request(path, params), timeout=7)
                if code != 200:
                    record["error"] = f"http_{code}"
                    if code not in {418, 429} and code < 500:
                        break
                else:
                    payload = json.loads(body)
                    record["acquired_at"] = datetime.now(UTC).isoformat()
                    self._persist(record, body)
                    record["error"] = None
                    break
            except (httpx.HTTPError, ValueError, TimeoutError):
                record["error"] = "network_or_response_contract"
            if attempt == 0:
                await asyncio.sleep(1)
        record.setdefault("acquired_at", datetime.now(UTC).isoformat())
        self.index["sources"].append(record)
        self.index["native_request_attempts_this_session"] = self.native_attempts
        self.save()
        return payload

    async def _native_request(self, path, params):
        async with self._http.stream("GET", "https://fapi.binance.com" + path, params=params) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError("native response body budget")
            return bytes(body), response.status_code

    async def close(self):
        await self.cmc.aclose()
        if self._owns_http:
            await self._http.aclose()
