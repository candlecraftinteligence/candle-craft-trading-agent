from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any

from app.context.models import ContextStatus
from app.microstructure.liquidation import (
    LiquidationPayloadError,
    WrongLiquidationContractTypeError,
    parse_binance_liquidation,
)
from app.microstructure.liquidation_aggregator import SymbolLiquidationAggregator
from app.microstructure.liquidation_models import LiquidationFlowSnapshot
from app.microstructure.service import (
    BINANCE_USDM_AGG_TRADE_URL,
    BinanceWebSocketTransport,
    FlowWebSocketConnection,
    FlowWebSocketTransport,
)


BINANCE_ALL_MARKET_LIQUIDATION_STREAM = "!forceOrder@arr"
BTC_LIQUIDATION_SYMBOL = "BTCUSDT"
DEFAULT_LIQUIDATION_STALE_AFTER_SECONDS = 30.0
DEFAULT_LIQUIDATION_MAX_SYMBOLS = 100
DEFAULT_RECONNECT_BASE_SECONDS = 1.0
DEFAULT_RECONNECT_MAX_SECONDS = 30.0

logger = logging.getLogger(__name__)


class LiquidationFlowService:
    """Watch-owned all-market Binance liquidation observation service."""

    def __init__(
        self,
        *,
        transport: FlowWebSocketTransport | None = None,
        stale_after_seconds: float = DEFAULT_LIQUIDATION_STALE_AFTER_SECONDS,
        max_symbols: int = DEFAULT_LIQUIDATION_MAX_SYMBOLS,
        reconnect_base_seconds: float = DEFAULT_RECONNECT_BASE_SECONDS,
        reconnect_max_seconds: float = DEFAULT_RECONNECT_MAX_SECONDS,
        clock: Any | None = None,
        sleep: Any | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("liquidation stale threshold must be greater than zero")
        if max_symbols < 1 or max_symbols > 1024:
            raise ValueError("liquidation max_symbols must be between 1 and 1024")
        if reconnect_base_seconds < 0 or reconnect_max_seconds < reconnect_base_seconds:
            raise ValueError("liquidation reconnect delays are invalid")
        self.transport = transport or BinanceWebSocketTransport(
            url=BINANCE_USDM_AGG_TRADE_URL,
            ping_interval_seconds=20.0,
            ping_timeout_seconds=20.0,
        )
        self.stale_after_seconds = float(stale_after_seconds)
        self.max_symbols = int(max_symbols)
        self.reconnect_base_seconds = float(reconnect_base_seconds)
        self.reconnect_max_seconds = float(reconnect_max_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.sleep = sleep or asyncio.sleep
        self.logger = log or logger
        self._aggregators: dict[str, SymbolLiquidationAggregator] = {}
        self._desired_symbols: set[str] = set()
        self._overflow_symbols: dict[str, str] = {}
        self._connection: FlowWebSocketConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._state_lock = asyncio.Lock()
        self._ever_connected = False
        self._connection_count = 0
        self._disconnect_count = 0
        self._malformed_event_count = 0
        self._wrong_contract_event_count = 0
        self._untracked_symbol_event_count = 0
        self._ignored_message_count = 0
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def tracked_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._desired_symbols))

    async def start(self, symbols: Sequence[str] = ()) -> None:
        await self.reconcile_symbols(symbols)
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(
            self._run_forever(),
            name="liquidation-flow-service",
        )

    async def stop(self) -> None:
        self._stop_event.set()
        task = self._task
        self._task = None
        connection = self._connection
        if connection is not None:
            await _safe_close(connection)
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._connection = None

    async def reconcile_symbols(self, symbols: Sequence[str]) -> tuple[str, ...]:
        requested = {_normalize_symbol(symbol) for symbol in symbols}
        requested.add(BTC_LIQUIDATION_SYMBOL)
        selected, overflow = _bounded_symbols(requested, maximum=self.max_symbols)
        overflow_reason = f"retention_limit_exceeded:max_symbols={self.max_symbols}"
        async with self._state_lock:
            previous = set(self._desired_symbols)
            additions = selected - previous
            removals = previous - selected
            self._desired_symbols = set(selected)
            self._overflow_symbols = {symbol: overflow_reason for symbol in overflow}
            for symbol in additions:
                aggregator = SymbolLiquidationAggregator(
                    symbol,
                    stale_after_seconds=self.stale_after_seconds,
                )
                if self._connection is not None:
                    aggregator.mark_connected(self.clock(), reconnect=False)
                self._aggregators[symbol] = aggregator
            for symbol in removals:
                self._aggregators.pop(symbol, None)
        if additions or removals or overflow:
            self.logger.info(
                "Liquidation retention reconciled additions=%s removals=%s tracked=%s overflow=%s.",
                len(additions),
                len(removals),
                len(selected),
                len(overflow),
            )
        return tuple(sorted(selected))

    def snapshot(self, symbol: str) -> LiquidationFlowSnapshot:
        normalized = _normalize_symbol(symbol)
        overflow_reason = self._overflow_symbols.get(normalized)
        if overflow_reason is not None:
            return LiquidationFlowSnapshot.unavailable(
                symbol=normalized,
                reason=overflow_reason,
            )
        aggregator = self._aggregators.get(normalized)
        if aggregator is None:
            reason = "service_not_started" if not self.running else "symbol_not_tracked"
            return LiquidationFlowSnapshot.unavailable(symbol=normalized, reason=reason)
        try:
            snapshot = aggregator.snapshot(as_of=self.clock())
            updates: dict[str, Any] = {
                "malformed_event_count": self._malformed_event_count,
                "wrong_contract_event_count": self._wrong_contract_event_count,
                "untracked_symbol_event_count": self._untracked_symbol_event_count,
                "connection_count": self._connection_count,
                "disconnect_count": self._disconnect_count,
            }
            if snapshot.status != ContextStatus.VERIFIED and self._last_error:
                reason = f"stream_error:{self._last_error}"
                updates.update(
                    status=ContextStatus.ERROR,
                    reason=reason,
                    liquidation_summary=f"Liquidation flow unavailable: {reason}.",
                )
            return snapshot.model_copy(update=updates)
        except Exception as exc:
            self.logger.warning(
                "Liquidation snapshot failed safely for symbol=%s: %s",
                normalized,
                _clean_reason(exc),
            )
            return LiquidationFlowSnapshot.unavailable(
                symbol=normalized,
                reason=f"snapshot_error:{type(exc).__name__}",
                status=ContextStatus.ERROR,
            )

    def health(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "connected": self._connection is not None,
            "tracked_symbol_count": len(self._desired_symbols),
            "overflow_symbol_count": len(self._overflow_symbols),
            "connection_count": self._connection_count,
            "disconnect_count": self._disconnect_count,
            "malformed_event_count": self._malformed_event_count,
            "wrong_contract_event_count": self._wrong_contract_event_count,
            "untracked_symbol_event_count": self._untracked_symbol_event_count,
            "ignored_message_count": self._ignored_message_count,
            "last_error": self._last_error,
        }

    async def _run_forever(self) -> None:
        failure_count = 0
        while not self._stop_event.is_set():
            connection: FlowWebSocketConnection | None = None
            try:
                connection = await self.transport.connect()
                reconnect = self._ever_connected
                self._ever_connected = True
                self._connection_count += 1
                self._connection = connection
                now = self.clock()
                for aggregator in self._aggregators.values():
                    aggregator.mark_connected(now, reconnect=reconnect)
                await connection.send(
                    json.dumps(
                        {
                            "method": "SUBSCRIBE",
                            "params": [BINANCE_ALL_MARKET_LIQUIDATION_STREAM],
                            "id": self._connection_count,
                        },
                        separators=(",", ":"),
                        sort_keys=True,
                    )
                )
                failure_count = 0
                self._last_error = None
                self.logger.info(
                    "Liquidation WebSocket connected tracked_symbols=%s reconnect=%s.",
                    len(self._desired_symbols),
                    reconnect,
                )
                while not self._stop_event.is_set():
                    payload = await connection.recv()
                    self._handle_payload(payload)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                failure_count += 1
                self._disconnect_count += 1
                self._last_error = f"{type(exc).__name__}:{_clean_reason(exc)}"
                self.logger.warning(
                    "Liquidation WebSocket disconnected attempt=%s error=%s.",
                    failure_count,
                    self._last_error,
                )
            finally:
                if self._connection is connection:
                    self._connection = None
                if connection is not None:
                    await _safe_close(connection)
                now = self.clock()
                for aggregator in self._aggregators.values():
                    aggregator.mark_disconnected(now)
            if self._stop_event.is_set():
                break
            delay = min(
                self.reconnect_max_seconds,
                self.reconnect_base_seconds
                * (2 ** min(max(failure_count - 1, 0), 10)),
            )
            if delay > 0:
                await self.sleep(delay)

    def _handle_payload(self, payload: str | bytes | Mapping[str, Any]) -> None:
        decoded = _decoded_mapping(payload)
        if decoded is not None and _is_control_message(decoded):
            self._ignored_message_count += 1
            return
        if decoded is not None:
            event_payload = decoded.get("data") if isinstance(decoded.get("data"), Mapping) else decoded
            if isinstance(event_payload, Mapping) and event_payload.get("e") == "serverShutdown":
                raise ConnectionError("binance_server_shutdown")
        try:
            event = parse_binance_liquidation(payload)
        except WrongLiquidationContractTypeError:
            self._wrong_contract_event_count += 1
            return
        except LiquidationPayloadError:
            self._malformed_event_count += 1
            return
        aggregator = self._aggregators.get(event.symbol)
        if aggregator is None or event.symbol not in self._desired_symbols:
            self._untracked_symbol_event_count += 1
            return
        aggregator.ingest(event, received_at=self.clock())


def _bounded_symbols(symbols: set[str], *, maximum: int) -> tuple[set[str], set[str]]:
    ordered = [
        BTC_LIQUIDATION_SYMBOL,
        *sorted(symbol for symbol in symbols if symbol != BTC_LIQUIDATION_SYMBOL),
    ]
    selected = set(ordered[:maximum])
    return selected, set(ordered[maximum:])


def _normalize_symbol(value: Any) -> str:
    normalized = str(value).strip().upper()
    if not normalized:
        raise ValueError("liquidation tracked symbol must not be blank")
    return normalized


def _decoded_mapping(payload: str | bytes | Mapping[str, Any]) -> Mapping[str, Any] | None:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(payload, str):
        return None
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, Mapping) else None


def _is_control_message(payload: Mapping[str, Any]) -> bool:
    return "id" in payload and "e" not in payload and "data" not in payload


async def _safe_close(connection: FlowWebSocketConnection) -> None:
    try:
        await connection.close()
    except Exception:
        return


def _clean_reason(exc: Exception) -> str:
    return str(exc).strip().replace("\n", " ")[:300] or type(exc).__name__
