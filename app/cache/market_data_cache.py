from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import time
from collections.abc import Awaitable, Callable, Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel

from app.data.exchange_clients import BaseExchangeClient

T = TypeVar("T")

CACHE_FILE_VERSION = 1
CACHE_TYPE_FIELD = "__cache_type__"

DEFAULT_TTLS_SECONDS: dict[str, int] = {
    "candles": 60,
    "ticker": 15,
    "funding": 60,
    "funding_history": 60,
    "open_interest": 30,
    "open_interest_history": 30,
    "long_short_ratio": 60,
}


class MarketDataCache:
    """Cache successful public market-data responses for one scanner run.

    The cache intentionally stores only data returned through public market-data
    methods. It never stores exceptions, private/account data, orders, transfers,
    or any credential-bearing payloads.
    """

    def __init__(
        self,
        *,
        enabled: bool = True,
        ttl_seconds: int | None = None,
        file_path: Path | str | None = None,
        now: Callable[[], float] | None = None,
    ) -> None:
        self.enabled = enabled
        self.ttl_seconds = ttl_seconds
        self.file_path = Path(file_path) if file_path is not None else None
        self._now = now or time.time
        self._entries: dict[str, dict[str, Any]] = {}
        self.hits = 0
        self.misses = 0
        self.expired = 0
        self.writes = 0
        self.errors = 0
        if self.enabled and self.file_path is not None:
            self._load_file()

    @property
    def file_cache_enabled(self) -> bool:
        return self.file_path is not None and self.enabled

    def stats(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "file_cache_enabled": self.file_cache_enabled,
            "file_path": str(self.file_path) if self.file_path is not None else None,
            "hits": self.hits,
            "misses": self.misses,
            "expired": self.expired,
            "writes": self.writes,
            "errors": self.errors,
            "entries": len(self._entries),
        }

    async def get_or_fetch(
        self,
        *,
        data_type: str,
        key_parts: Mapping[str, Any],
        fetch: Callable[[], Awaitable[T]],
    ) -> T:
        if not self.enabled:
            return await fetch()

        cache_key = self.cache_key(data_type=data_type, key_parts=key_parts)
        now = self._now()
        entry = self._entries.get(cache_key)
        if entry is not None:
            expires_at = float(entry.get("expires_at", 0))
            if expires_at > now:
                self.hits += 1
                return _deserialize_value(entry.get("value"))

            self.expired += 1
            self._entries.pop(cache_key, None)

        self.misses += 1
        value = await fetch()
        self._entries[cache_key] = {
            "version": CACHE_FILE_VERSION,
            "data_type": data_type,
            "key": _json_safe_key(dict(key_parts)),
            "created_at": now,
            "expires_at": now + self._ttl_for(data_type),
            "value": _serialize_value(value),
        }
        self.writes += 1
        if self.file_path is not None:
            self._write_file()
        return value

    def cache_key(self, *, data_type: str, key_parts: Mapping[str, Any]) -> str:
        payload = {
            "data_type": data_type,
            "key": _json_safe_key(dict(key_parts)),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _ttl_for(self, data_type: str) -> int:
        if self.ttl_seconds is not None:
            return max(int(self.ttl_seconds), 0)
        return DEFAULT_TTLS_SECONDS.get(data_type, 60)

    def _load_file(self) -> None:
        if self.file_path is None or not self.file_path.exists():
            return
        try:
            payload = json.loads(self.file_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self.errors += 1
            return
        if not isinstance(payload, Mapping) or payload.get("version") != CACHE_FILE_VERSION:
            self.errors += 1
            return
        entries = payload.get("entries")
        if not isinstance(entries, Mapping):
            self.errors += 1
            return
        self._entries = {
            str(key): dict(value)
            for key, value in entries.items()
            if isinstance(value, Mapping) and value.get("version") == CACHE_FILE_VERSION
        }

    def _write_file(self) -> None:
        if self.file_path is None:
            return
        try:
            self.file_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.file_path.with_suffix(f"{self.file_path.suffix}.tmp")
            temp_path.write_text(
                json.dumps(
                    {
                        "version": CACHE_FILE_VERSION,
                        "entries": self._entries,
                    },
                    indent=2,
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            temp_path.replace(self.file_path)
        except OSError:
            self.errors += 1


class CachedMarketDataClient(BaseExchangeClient):
    """Read-only public market-data cache wrapper."""

    def __init__(self, client: BaseExchangeClient, cache: MarketDataCache) -> None:
        self.client = client
        self.cache = cache
        self.exchange_name = _exchange_name(client)

    async def get_klines(self, symbol: str, interval: str, limit: int) -> Any:
        normalized_symbol = symbol.upper()
        normalized_interval = interval.strip()
        return await self.cache.get_or_fetch(
            data_type="candles",
            key_parts={
                "exchange": self.exchange_name,
                "symbol": normalized_symbol,
                "endpoint": "get_klines",
                "interval": normalized_interval,
                "limit": limit,
                "params": {},
            },
            fetch=lambda: self.client.get_klines(normalized_symbol, interval, limit),
        )

    async def get_ticker(self, symbol: str) -> Any:
        normalized_symbol = symbol.upper()
        return await self.cache.get_or_fetch(
            data_type="ticker",
            key_parts={
                "exchange": self.exchange_name,
                "symbol": normalized_symbol,
                "endpoint": "get_ticker",
                "interval": None,
                "limit": None,
                "params": {},
            },
            fetch=lambda: self.client.get_ticker(normalized_symbol),
        )

    async def get_funding_rate(self, symbol: str) -> Any:
        normalized_symbol = symbol.upper()
        return await self.cache.get_or_fetch(
            data_type="funding",
            key_parts={
                "exchange": self.exchange_name,
                "symbol": normalized_symbol,
                "endpoint": "get_funding_rate",
                "interval": None,
                "limit": 1,
                "params": {},
            },
            fetch=lambda: self.client.get_funding_rate(normalized_symbol),
        )

    async def get_funding_rate_history(self, symbol: str, limit: int = 100) -> Any:
        normalized_symbol = symbol.upper()
        return await self.cache.get_or_fetch(
            data_type="funding_history",
            key_parts={
                "exchange": self.exchange_name,
                "symbol": normalized_symbol,
                "endpoint": "get_funding_rate_history",
                "interval": None,
                "limit": limit,
                "params": {},
            },
            fetch=lambda: _call_public_method(
                self.client,
                "get_funding_rate_history",
                normalized_symbol,
                {"limit": limit},
            ),
        )

    async def get_open_interest(self, symbol: str) -> Any:
        normalized_symbol = symbol.upper()
        return await self.cache.get_or_fetch(
            data_type="open_interest",
            key_parts={
                "exchange": self.exchange_name,
                "symbol": normalized_symbol,
                "endpoint": "get_open_interest",
                "interval": None,
                "limit": None,
                "params": {},
            },
            fetch=lambda: self.client.get_open_interest(normalized_symbol),
        )

    async def get_open_interest_history(self, symbol: str, limit: int = 30, period: str | None = None) -> Any:
        normalized_symbol = symbol.upper()
        default_period = _default_parameter(self.client, "get_open_interest_history", "period")
        effective_period = period if period is not None else default_period
        if effective_period is None:
            effective_period = getattr(self.client, "open_interest_interval", None)

        async def fetch() -> Any:
            params: dict[str, Any] = {"limit": limit}
            if period is not None:
                params["period"] = period
            return await _call_public_method(
                self.client,
                "get_open_interest_history",
                normalized_symbol,
                params,
            )

        return await self.cache.get_or_fetch(
            data_type="open_interest_history",
            key_parts={
                "exchange": self.exchange_name,
                "symbol": normalized_symbol,
                "endpoint": "get_open_interest_history",
                "interval": effective_period,
                "limit": limit,
                "params": {"period": effective_period},
            },
            fetch=fetch,
        )

    async def get_long_short_ratio(self, symbol: str, limit: int = 1, period: str | None = None) -> Any:
        normalized_symbol = symbol.upper()
        default_period = _default_parameter(self.client, "get_long_short_ratio", "period")
        effective_period = period if period is not None else default_period

        async def fetch() -> Any:
            params: dict[str, Any] = {"limit": limit}
            if period is not None:
                params["period"] = period
            return await _call_public_method(
                self.client,
                "get_long_short_ratio",
                normalized_symbol,
                params,
            )

        return await self.cache.get_or_fetch(
            data_type="long_short_ratio",
            key_parts={
                "exchange": self.exchange_name,
                "symbol": normalized_symbol,
                "endpoint": "get_long_short_ratio",
                "interval": effective_period,
                "limit": limit,
                "params": {"period": effective_period},
            },
            fetch=fetch,
        )

    async def aclose(self) -> None:
        close = getattr(self.client, "aclose", None)
        if callable(close):
            await close()

    def cache_stats(self) -> dict[str, Any]:
        return self.cache.stats()

    def retry_events(self) -> tuple[dict[str, Any], ...]:
        events = getattr(self.client, "retry_events", None)
        if callable(events):
            return tuple(events())
        return ()


def _exchange_name(client: BaseExchangeClient) -> str:
    value = getattr(client, "exchange_name", None)
    if value:
        return str(value)
    return client.__class__.__name__.lower()


def _default_parameter(client: BaseExchangeClient, method_name: str, parameter_name: str) -> Any | None:
    method = getattr(client, method_name, None)
    if not callable(method):
        return None
    try:
        parameter = inspect.signature(method).parameters.get(parameter_name)
    except (TypeError, ValueError):
        return None
    if parameter is None or parameter.default is inspect.Parameter.empty:
        return None
    return parameter.default


async def _call_public_method(
    client: BaseExchangeClient,
    method_name: str,
    symbol: str,
    params: Mapping[str, Any],
) -> Any:
    method = getattr(client, method_name)
    accepted = _accepted_kwargs(method, params)
    return await method(symbol, **accepted)


def _accepted_kwargs(method: Callable[..., Any], params: Mapping[str, Any]) -> dict[str, Any]:
    try:
        signature = inspect.signature(method)
    except (TypeError, ValueError):
        return dict(params)
    if any(parameter.kind == inspect.Parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return dict(params)
    return {
        name: value
        for name, value in params.items()
        if name in signature.parameters
    }


def _json_safe_key(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe_key(item) for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))}
    if isinstance(value, (list, tuple)):
        return [_json_safe_key(item) for item in value]
    return value


def _serialize_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return {
            CACHE_TYPE_FIELD: "pydantic",
            "class": f"{value.__class__.__module__}:{value.__class__.__qualname__}",
            "data": value.model_dump(mode="json"),
        }
    if isinstance(value, Decimal):
        return {CACHE_TYPE_FIELD: "decimal", "value": str(value)}
    if isinstance(value, tuple):
        return {CACHE_TYPE_FIELD: "tuple", "items": [_serialize_value(item) for item in value]}
    if isinstance(value, list):
        return {CACHE_TYPE_FIELD: "list", "items": [_serialize_value(item) for item in value]}
    if isinstance(value, Mapping):
        return {
            CACHE_TYPE_FIELD: "dict",
            "items": [[_serialize_value(key), _serialize_value(item)] for key, item in value.items()],
        }
    return value


def _deserialize_value(value: Any) -> Any:
    if isinstance(value, Mapping):
        cache_type = value.get(CACHE_TYPE_FIELD)
        if cache_type == "pydantic":
            class_path = value.get("class")
            data = value.get("data")
            model = _load_model_class(str(class_path)) if class_path else None
            if model is not None:
                return model.model_validate(data)
            return data
        if cache_type == "decimal":
            return Decimal(str(value.get("value")))
        if cache_type == "tuple":
            return tuple(_deserialize_value(item) for item in value.get("items", ()))
        if cache_type == "list":
            return [_deserialize_value(item) for item in value.get("items", ())]
        if cache_type == "dict":
            return {
                _deserialize_value(key): _deserialize_value(item)
                for key, item in value.get("items", ())
            }
    return value


def _load_model_class(class_path: str) -> type[BaseModel] | None:
    module_name, _, qualname = class_path.partition(":")
    if not module_name or not qualname:
        return None
    try:
        current: Any = importlib.import_module(module_name)
        for part in qualname.split("."):
            current = getattr(current, part)
    except (ImportError, AttributeError):
        return None
    if isinstance(current, type) and issubclass(current, BaseModel):
        return current
    return None
