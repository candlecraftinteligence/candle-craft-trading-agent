from __future__ import annotations

import asyncio
import json
import logging
import math
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

from app.context.models import ContextStatus
from app.microstructure.reconnect import FeedRecovery, close_connection
from app.microstructure.websocket_protocol import is_control_message as _is_control_message
from app.microstructure.websocket_protocol import matches_usdt_perpetual, UnverifiedPayloadError
from app.microstructure.agg_trade import (
    AggTradePayloadError,
    WrongContractTypeError,
    parse_binance_agg_trade,
)
from app.microstructure.aggregator import SymbolFlowAggregator
from app.microstructure.models import MicrostructureFlowSnapshot


BINANCE_USDM_AGG_TRADE_URL = "wss://fstream.binance.com/market/stream"
BTC_FLOW_SYMBOL = "BTCUSDT"
DEFAULT_MAX_SYMBOLS = 100
DEFAULT_STALE_AFTER_SECONDS = 5.0
DEFAULT_RECONNECT_BASE_SECONDS = 1.0
DEFAULT_RECONNECT_MAX_SECONDS = 30.0
DEFAULT_WEBSOCKET_MAX_QUEUE = 32
DEFAULT_WEBSOCKET_MAX_MESSAGE_BYTES = 64 * 1024

logger = logging.getLogger(__name__)


class FlowWebSocketConnection(Protocol):
    async def recv(self) -> str | bytes | Mapping[str, Any]: ...

    async def send(self, payload: str) -> Any: ...

    async def close(self) -> Any: ...


class FlowWebSocketTransport(Protocol):
    async def connect(self) -> FlowWebSocketConnection: ...


class BinanceWebSocketTransport:
    """Bounded public-market WebSocket transport; no credentials are used."""

    def __init__(
        self,
        *,
        url: str = BINANCE_USDM_AGG_TRADE_URL,
        open_timeout_seconds: float = 10.0,
        close_timeout_seconds: float = 5.0,
        max_queue: int = DEFAULT_WEBSOCKET_MAX_QUEUE,
        max_message_bytes: int = DEFAULT_WEBSOCKET_MAX_MESSAGE_BYTES,
        ping_interval_seconds: float | None = None,
        ping_timeout_seconds: float | None = None,
    ) -> None:
        if not all(math.isfinite(v) and v > 0 for v in (open_timeout_seconds, close_timeout_seconds)):
            raise ValueError("WebSocket timeouts must be greater than zero")
        if max_queue < 1 or max_message_bytes < 1:
            raise ValueError("WebSocket queue and message limits must be positive")
        if ping_interval_seconds is not None and (not math.isfinite(ping_interval_seconds) or ping_interval_seconds <= 0):
            raise ValueError("WebSocket ping interval must be greater than zero")
        if ping_timeout_seconds is not None and (not math.isfinite(ping_timeout_seconds) or ping_timeout_seconds <= 0):
            raise ValueError("WebSocket ping timeout must be greater than zero")
        self.url = url
        self.open_timeout_seconds = float(open_timeout_seconds)
        self.close_timeout_seconds = float(close_timeout_seconds)
        self.max_queue = int(max_queue)
        self.max_message_bytes = int(max_message_bytes)
        self.ping_interval_seconds = ping_interval_seconds
        self.ping_timeout_seconds = ping_timeout_seconds

    async def connect(self) -> FlowWebSocketConnection:
        from websockets.asyncio.client import connect

        return await connect(
            self.url,
            open_timeout=self.open_timeout_seconds,
            close_timeout=self.close_timeout_seconds,
            ping_interval=self.ping_interval_seconds,
            ping_timeout=self.ping_timeout_seconds,
            max_queue=self.max_queue,
            max_size=self.max_message_bytes,
        )


class MicrostructureFlowService:
    """Long-lived aggregate-trade flow service shared by scanner iterations."""

    def __init__(
        self,
        *,
        transport: FlowWebSocketTransport | None = None,
        stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
        max_symbols: int = DEFAULT_MAX_SYMBOLS,
        reconnect_base_seconds: float = DEFAULT_RECONNECT_BASE_SECONDS,
        reconnect_max_seconds: float = DEFAULT_RECONNECT_MAX_SECONDS,
        allow_legacy_missing_stream_type: bool = False,
        clock: Any | None = None,
        sleep: Any | None = None,
        monotonic: Any | None = None,
        jitter: Any | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("microstructure stale threshold must be greater than zero")
        if max_symbols < 1 or max_symbols > 1024:
            raise ValueError("microstructure max_symbols must be between 1 and 1024")
        if reconnect_base_seconds < 0 or reconnect_max_seconds < reconnect_base_seconds:
            raise ValueError("microstructure reconnect delays are invalid")
        self.transport = transport or BinanceWebSocketTransport(
            ping_interval_seconds=20.0, ping_timeout_seconds=20.0,
        )
        self.stale_after_seconds = float(stale_after_seconds)
        self.max_symbols = int(max_symbols)
        self.reconnect_base_seconds = float(reconnect_base_seconds)
        self.reconnect_max_seconds = float(reconnect_max_seconds)
        self.allow_legacy_missing_stream_type = bool(allow_legacy_missing_stream_type)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or asyncio.sleep
        self.logger = log or logger
        self._lifecycle_lock = asyncio.Lock()
        self._recovery = FeedRecovery(
            "Microstructure", self.reconnect_base_seconds, self.reconnect_max_seconds,
            monotonic=monotonic, jitter=jitter, log=self.logger,
        )
        self._aggregators: dict[str, SymbolFlowAggregator] = {}
        self._desired_symbols: set[str] = set()
        self._active_symbols: set[str] = set()
        self._overflow_symbols: dict[str, str] = {}
        self._connection: FlowWebSocketConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._state_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._request_id = 0
        self._ever_connected = False
        self._connection_count = 0
        self._disconnect_count = 0
        self._malformed_event_count = 0
        self._wrong_contract_event_count = 0
        self._ignored_message_count = 0
        self._last_error: str | None = None
        self._payload_failure = False

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def subscribed_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._desired_symbols))

    async def start(self, symbols: Sequence[str] = ()) -> None:
        async with self._lifecycle_lock:
            await self.reconcile_symbols(symbols)
            if self.running:
                return
            self._stop_event.clear()
            self._task = asyncio.create_task(self._run_forever(), name="microstructure-flow-service")

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._stop_event.set()
            task, connection = self._task, self._connection
            self._invalidate_stream()
            try:
                if task is not None:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
                elif connection is not None:
                    await close_connection(connection)
            finally:
                self._task = None
                self._recovery.stopped()

    def _invalidate_stream(self) -> None:
        self._connection = None
        self._active_symbols.clear()
        for aggregator in self._aggregators.values():
            aggregator.mark_disconnected(self.clock())

    async def reconcile_symbols(self, symbols: Sequence[str]) -> tuple[str, ...]:
        requested = {_normalize_symbol(symbol) for symbol in symbols}
        requested.add(BTC_FLOW_SYMBOL)
        selected, overflow = _bounded_symbols(requested, maximum=self.max_symbols)
        overflow_reason = f"subscription_limit_exceeded:max_symbols={self.max_symbols}"
        async with self._state_lock:
            previous = set(self._desired_symbols)
            additions = selected - previous
            removals = previous - selected
            self._desired_symbols = set(selected)
            self._overflow_symbols = {symbol: overflow_reason for symbol in overflow}
            for symbol in additions:
                aggregator = SymbolFlowAggregator(
                    symbol,
                    stale_after_seconds=self.stale_after_seconds,
                )
                if self._connection is not None:
                    aggregator.mark_connected(self.clock(), reconnect=False)
                self._aggregators[symbol] = aggregator
            for symbol in removals:
                self._aggregators.pop(symbol, None)
            connection = self._connection
        if connection is not None:
            await self._send_subscription_diff(
                connection,
                additions=additions,
                removals=removals,
            )
        if additions or removals or overflow:
            self.logger.info(
                "Microstructure subscriptions reconciled additions=%s removals=%s active=%s overflow=%s.",
                len(additions),
                len(removals),
                len(selected),
                len(overflow),
            )
        return tuple(sorted(selected))

    def snapshot(self, symbol: str) -> MicrostructureFlowSnapshot:
        normalized = _normalize_symbol(symbol)
        overflow_reason = self._overflow_symbols.get(normalized)
        if overflow_reason is not None:
            return MicrostructureFlowSnapshot.unavailable(
                symbol=normalized,
                reason=overflow_reason,
            )
        aggregator = self._aggregators.get(normalized)
        if aggregator is None:
            reason = "service_not_started" if not self.running else "symbol_not_subscribed"
            return MicrostructureFlowSnapshot.unavailable(symbol=normalized, reason=reason)
        try:
            return aggregator.snapshot(as_of=self.clock())
        except Exception as exc:
            self.logger.warning(
                "Microstructure snapshot failed safely for symbol=%s: %s",
                normalized,
                _clean_reason(exc),
            )
            return MicrostructureFlowSnapshot.unavailable(
                symbol=normalized,
                reason=f"snapshot_error:{type(exc).__name__}",
                status=ContextStatus.ERROR,
            )

    def health(self) -> dict[str, Any]:
        return {
            **self._recovery.health(),
            "running": self.running,
            "connected": self._connection is not None,
            "desired_symbol_count": len(self._desired_symbols),
            "active_symbol_count": len(self._active_symbols),
            "overflow_symbol_count": len(self._overflow_symbols),
            "connection_count": self._connection_count,
            "disconnect_count": self._disconnect_count,
            "malformed_event_count": self._malformed_event_count,
            "wrong_contract_event_count": self._wrong_contract_event_count,
            "ignored_message_count": self._ignored_message_count,
            "last_error": self._last_error,
        }

    async def _run_forever(self) -> None:
        self._recovery.start_session()
        while not self._stop_event.is_set():
            connection: FlowWebSocketConnection | None = None
            phase, delay = "connect", 0.0
            self._payload_failure = False
            try:
                connection = await self.transport.connect()
                self._recovery.connected()
                phase = "subscribe"
                reconnect = self._ever_connected
                self._ever_connected = True
                self._connection_count += 1
                self._connection = connection
                now = self.clock()
                for aggregator in self._aggregators.values():
                    aggregator.mark_connected(now, reconnect=reconnect)
                self._active_symbols.clear()
                await self._send_subscription_diff(connection, additions=set(self._desired_symbols), removals=set())
                self._last_error = None
                phase = "receive"
                while not self._stop_event.is_set():
                    payload = await connection.recv()
                    accepted = self._handle_payload(payload)
                    if self._payload_failure:
                        raise UnverifiedPayloadError("public_stream_payload_unverified")
                    if accepted:
                        self._recovery.market_event()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._disconnect_count += 1
                self._last_error = f"{type(exc).__name__}:{_clean_reason(exc)}"
                self._invalidate_stream()
                delay = self._recovery.failed(exc, phase)
            finally:
                self._invalidate_stream()
                if connection is not None:
                    await close_connection(connection)
            if self._stop_event.is_set():
                break
            if delay > 0:
                await self.sleep(delay)
            else:
                # A zero-delay injected test policy must still yield ownership.
                await asyncio.sleep(0)

    def _reject_unverified_payload(self) -> None:
        if self._connection is not None:
            self._payload_failure = True
            self._invalidate_stream()

    def _handle_payload(self, payload: str | bytes | Mapping[str, Any]) -> bool:
        if _is_control_message(payload):
            self._ignored_message_count += 1
            return False
        try:
            event = parse_binance_agg_trade(
                payload,
                allow_legacy_missing_stream_type=self.allow_legacy_missing_stream_type,
            )
        except WrongContractTypeError:
            self._wrong_contract_event_count += 1
            return False
        except AggTradePayloadError:
            self._malformed_event_count += 1
            self._reject_unverified_payload()
            return False
        if not matches_usdt_perpetual(event.symbol):
            self._wrong_contract_event_count += 1
            return False
        aggregator = self._aggregators.get(event.symbol)
        if aggregator is None or event.symbol not in self._desired_symbols:
            self._ignored_message_count += 1
            return False
        accepted = aggregator.ingest(event)
        return accepted and 0 <= (self.clock() - event.event_time).total_seconds() <= self.stale_after_seconds

    async def _send_subscription_diff(
        self,
        connection: FlowWebSocketConnection,
        *,
        additions: set[str],
        removals: set[str],
    ) -> None:
        async with self._send_lock:
            if connection is not self._connection:
                return
            if removals:
                await connection.send(self._subscription_message("UNSUBSCRIBE", removals))
                self._active_symbols.difference_update(removals)
            if additions:
                await connection.send(self._subscription_message("SUBSCRIBE", additions))
                self._active_symbols.update(additions)

    def _subscription_message(self, method: str, symbols: set[str]) -> str:
        self._request_id += 1
        return json.dumps(
            {
                "method": method,
                "params": [f"{symbol.lower()}@aggTrade" for symbol in sorted(symbols)],
                "id": self._request_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


def _bounded_symbols(symbols: set[str], *, maximum: int) -> tuple[set[str], set[str]]:
    ordered = [BTC_FLOW_SYMBOL, *sorted(symbol for symbol in symbols if symbol != BTC_FLOW_SYMBOL)]
    selected = set(ordered[:maximum])
    return selected, set(ordered[maximum:])


def _normalize_symbol(value: Any) -> str:
    normalized = str(value).strip().upper()
    if not normalized:
        raise ValueError("microstructure subscription symbol must not be blank")
    return normalized


def _clean_reason(exc: Exception) -> str:
    return str(exc).strip().replace("\n", " ")[:300] or type(exc).__name__
