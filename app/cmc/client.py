"""Async GET-only REST client. No MCP dependency or credential-bearing diagnostics."""
from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import sqlite3
import time
from collections import OrderedDict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Callable

import httpx
from pydantic import Field, SecretStr

from app.cmc.endpoints import CONTRACTS, Endpoint, reserved_credits, rows
from app.cmc.models import EvidenceState, Frozen, stable_id
from app.cmc.storage import Sidecar


class Fetch(Frozen):
    endpoint: Endpoint
    acquired_at: datetime
    state: EvidenceState
    data: Any = None
    raw: Any = None
    raw_text: str | None = Field(default=None, repr=False)
    raw_ref: str = "N/A"
    error: str | None = None
    credits_reported: int | None = None
    authenticated: bool = False
    pagination_complete: bool | None = None
    request_params: dict[str, Any] = Field(default_factory=dict)


class CmcClient:
    ROOT = "https://pro-api.coinmarketcap.com"

    def __init__(self, *, store: Sidecar, api_key: SecretStr | None = None,
                 keyless: bool = False, http_client: httpx.AsyncClient | None = None,
                 timeout: float = 3, attempts: int = 3, concurrency: int = 2,
                 daily_credits: int = 100, monthly_credits: int = 3000,
                 max_entries: int = 16, max_body_bytes: int = 1024 * 1024,
                 clock: Callable = lambda: datetime.now(UTC), monotonic: Callable = time.monotonic,
                 sleep: Callable = asyncio.sleep):
        if not 1 <= concurrency <= 8 or not 1 <= attempts <= 5 or timeout <= 0 or max_entries < 1:
            raise ValueError("invalid CMC resource limits")
        self.store, self._key, self.keyless = store, api_key, keyless
        self._http = http_client or httpx.AsyncClient(follow_redirects=False)
        self._owns_http = http_client is None
        self.timeout, self.attempts = timeout, attempts
        self.daily, self.monthly = daily_credits, monthly_credits
        self.max_entries, self.max_body = max_entries, max_body_bytes
        self.clock, self.monotonic, self.sleep = clock, monotonic, sleep
        self._semaphore = asyncio.Semaphore(concurrency)
        self._cache = OrderedDict()
        self._inflight = {}
        self._failures, self._open_until = 0, 0.0
        self.capabilities: dict[str, str] = {}

    async def aclose(self):
        tasks = tuple(self._inflight.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self._owns_http:
            await self._http.aclose()

    async def get(self, endpoint: Endpoint, params: dict | None = None, *, refresh: bool = False) -> Fetch:
        endpoint = Endpoint(endpoint)  # Disallows arbitrary hosts / endpoints.
        params = dict(params or {})
        self._validate_params(endpoint, params)
        key = stable_id((endpoint, params))
        cached = self._cache.get(key)
        if not refresh and cached and self.monotonic() < cached[0]:
            self._cache.move_to_end(key)
            return copy.deepcopy(cached[1])
        if key in self._inflight:
            return copy.deepcopy(await asyncio.shield(self._inflight[key]))
        if len(self._inflight) >= self.max_entries:
            return self._error(endpoint, "inflight_budget", EvidenceState.UNAVAILABLE)
        task = asyncio.create_task(self._fetch_cached(endpoint, params, key))
        self._inflight[key] = task
        return copy.deepcopy(await asyncio.shield(task))

    async def _fetch_cached(self,endpoint,params,key):
        try:
            result = await self._fetch(endpoint,params)
            ttl = CONTRACTS[endpoint].ttl if result.error is None else 60
            self._cache[key] = (self.monotonic() + ttl, result)
            while len(self._cache) > self.max_entries:
                self._cache.popitem(last=False)
            return result
        finally:
            self._inflight.pop(key,None)

    async def pages(self, endpoint: Endpoint, params: dict | None = None, *, max_pages: int = 4):
        if not CONTRACTS[endpoint].paginated or not 1 <= max_pages <= 20:
            raise ValueError("invalid pagination request")
        params = dict(params or {})
        limit = int(params.get("limit", 100))
        start = int(params.get("start", 1))
        seen = set()
        for page in range(max_pages):
            result = await self.get(endpoint, {**params, "start": start + page * limit, "limit": limit})
            if result.error:
                yield result
                return
            try:
                items = rows(endpoint, result.data)
            except ValueError:
                yield result.model_copy(update={"state":EvidenceState.ERROR,"error":"pagination_contract"})
                return
            fingerprint = stable_id(items)
            if fingerprint in seen:
                yield result.model_copy(update={"state":EvidenceState.ERROR,"error":"repeated_pagination_page"})
                return
            seen.add(fingerprint)
            complete=len(items) < limit or (isinstance(result.data, dict) and result.data.get("has_more") is False)
            yield result.model_copy(update={"pagination_complete":True if complete else False if page+1==max_pages else None})
            if complete:
                return

    def _validate_params(self, endpoint, params):
        if params.get("convert", "USD") != "USD" or "convert_id" in params:
            raise ValueError("CMC integration supports a single USD conversion")
        if any("key" in str(key).lower() for key in params):
            raise ValueError("credentials belong only in the auth header")
        if "limit" in params and not 1 <= int(params["limit"]) <= CONTRACTS[endpoint].page_max:
            raise ValueError("page limit outside official contract")
        if int(params.get("start", 1)) < 1:
            raise ValueError("start must be 1-based")
        if endpoint == Endpoint.HISTORICAL_LISTINGS and "date" not in params:
            raise ValueError("historical listings require an explicit date")
        if endpoint == Endpoint.EXCHANGE_PAIRS and not (params.get("exchange_id") or params.get("exchange_slug")):
            raise ValueError("exchange identity required")
        if endpoint == Endpoint.CRYPTO_PAIRS and not params.get("crypto_id"):
            raise ValueError("numeric crypto_id required")
        if endpoint == Endpoint.CATEGORY and not params.get("id"):
            raise ValueError("category id required")

    def _error(self, endpoint, reason, state=EvidenceState.ERROR):
        return Fetch(endpoint=endpoint, acquired_at=self.clock(), state=state, error=reason)

    async def _fetch(self, endpoint, params):
        if not self._key and not (self.keyless and CONTRACTS[endpoint].keyless):
            self.capabilities[endpoint.value] = "UNAVAILABLE:credential_or_keyless_route"
            return self._error(endpoint, "credential_or_keyless_route", EvidenceState.UNAVAILABLE)
        if self.monotonic() < self._open_until:
            return self._error(endpoint, "circuit_open", EvidenceState.UNAVAILABLE)
        authenticated = bool(self._key)
        headers = {"Accept": "application/json"}
        if self._key:
            headers["X-CMC_PRO_API_KEY"] = self._key.get_secret_value()
        url = self.ROOT + ("" if authenticated else "/public-api") + endpoint.value
        result = self._error(endpoint, "attempts_exhausted")
        async with self._semaphore:
            for attempt in range(self.attempts):
                estimate = reserved_credits(endpoint, params)
                try:
                    allowed = await asyncio.to_thread(self.store.reserve, self.clock(), estimate,
                        daily=self.daily, monthly=self.monthly)
                    if not allowed:
                        return self._error(endpoint, "credit_budget", EvidenceState.UNAVAILABLE)
                    response_code, response_headers, body = await asyncio.wait_for(
                        self._request(url, headers, params), timeout=self.timeout)
                    if response_code in (401, 403):
                        result = self._error(endpoint, f"http_{response_code}_entitlement", EvidenceState.UNAVAILABLE)
                        break
                    if response_code == 429 or response_code >= 500:
                        result = self._error(endpoint, f"http_{response_code}")
                        delay = min(2, max(0, float(response_headers.get("retry-after", 0))))
                    elif response_code != 200:
                        result = self._error(endpoint, f"http_{response_code}")
                        break
                    else:
                        payload = json.loads(body, parse_float=Decimal)
                        status = payload.get("status") if isinstance(payload, dict) else None
                        if not isinstance(status, dict) or str(status.get("error_code")) != "0" or "data" not in payload:
                            code = str(status.get("error_code")) if isinstance(status, dict) else "malformed"
                            # Do not copy arbitrary provider error text into logs/evidence.
                            safe_code = code if code.isdigit() else "malformed"
                            result = self._error(endpoint, "envelope_" + safe_code)
                            break
                        acquired = self.clock()
                        reported = status.get("credit_count")
                        reported = int(reported) if str(reported).isdigit() else None
                        if reported is not None and reported > estimate:
                            await asyncio.to_thread(self.store.reserve, acquired, reported - estimate,
                                daily=2**63-1, monthly=2**63-1,record_request=False)
                        result = Fetch(endpoint=endpoint, acquired_at=acquired,
                            state=EvidenceState.UNVERIFIED, data=payload["data"], raw=payload,
                            raw_text=body.decode("utf-8"),raw_ref=hashlib.sha256(body).hexdigest(),
                            credits_reported=reported, authenticated=authenticated, request_params=params)
                        self._failures = 0
                        self.capabilities[endpoint.value] = "FETCHED_AUTHENTICATED" if authenticated else "FETCHED_KEYLESS"
                        return result
                except (httpx.HTTPError, TimeoutError, ValueError, TypeError, OSError, sqlite3.Error):
                    result = self._error(endpoint, "network_or_response_contract")
                    delay = 0
                if attempt + 1 < self.attempts:
                    await self.sleep(max(delay, min(2, 0.25 * (2 ** attempt))))
        self._failures += 1
        if self._failures >= 3:
            self._open_until = self.monotonic() + 60
        self.capabilities[endpoint.value] = result.state.value + ":" + str(result.error)
        return result

    async def _request(self, url, headers, params):
        async with self._http.stream("GET", url, headers=headers, params=params, timeout=self.timeout) as response:
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > self.max_body:
                    raise ValueError("response body budget")
            return response.status_code, response.headers, bytes(body)
