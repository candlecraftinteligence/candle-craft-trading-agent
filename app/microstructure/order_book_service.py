from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx

from app.context.models import ContextStatus
from app.data.exceptions import (
    ExchangeHTTPError,
    ExchangeMalformedJSONError,
    ExchangeNetworkError,
    ExchangeRateLimitError,
    ExchangeTimeoutError,
)
from app.microstructure.order_book import (
    BookIngestOutcome,
    OrderBookPayloadError,
    SynchronizedLocalOrderBook,
    WrongOrderBookContractTypeError,
    parse_binance_depth_event,
    parse_binance_depth_snapshot,
)
from app.microstructure.order_book_models import OrderBookLiquiditySnapshot
from app.microstructure.service import (
    BinanceWebSocketTransport,
    FlowWebSocketConnection,
    FlowWebSocketTransport,
)


BINANCE_USDM_PUBLIC_STREAM_URL = "wss://fstream.binance.com/public/stream"
BINANCE_USDM_REST_URL = "https://fapi.binance.com"
BTC_ORDER_BOOK_SYMBOL = "BTCUSDT"
ORDER_BOOK_HARD_MAX_SYMBOLS = 100
DEFAULT_ORDER_BOOK_MAX_SYMBOLS = 100
DEFAULT_ORDER_BOOK_STALE_AFTER_SECONDS = 5.0
DEFAULT_ORDER_BOOK_UPDATE_SPEED = "500ms"
DEFAULT_ORDER_BOOK_SNAPSHOT_LIMIT = 500
DEFAULT_ORDER_BOOK_EVENT_BUFFER_SIZE = 256
DEFAULT_ORDER_BOOK_BOOTSTRAP_CONCURRENCY = 2
DEFAULT_ORDER_BOOK_BOOTSTRAP_MIN_INTERVAL_SECONDS = 0.5
DEFAULT_ORDER_BOOK_BOOTSTRAP_ATTEMPTS = 3
DEFAULT_ORDER_BOOK_BOOTSTRAP_BACKOFF_SECONDS = 1.0
DEFAULT_ORDER_BOOK_BOOTSTRAP_JITTER_SECONDS = 0.1
DEFAULT_RECONNECT_BASE_SECONDS = 1.0
DEFAULT_RECONNECT_MAX_SECONDS = 30.0
ORDER_BOOK_WEBSOCKET_MAX_MESSAGE_BYTES = 1024 * 1024
ORDER_BOOK_WEBSOCKET_MAX_QUEUE = 4
VALID_UPDATE_SPEEDS = frozenset({"100ms", "250ms", "500ms"})
VALID_SNAPSHOT_LIMITS = frozenset({5, 10, 20, 50, 100, 500, 1000})
SNAPSHOT_REQUEST_WEIGHTS = {5: 2, 10: 2, 20: 2, 50: 2, 100: 5, 500: 10, 1000: 20}

logger = logging.getLogger(__name__)


class DepthSnapshotClient(Protocol):
    async def fetch(self, symbol: str, limit: int) -> Mapping[str, Any]: ...

    async def aclose(self) -> None: ...


class BinanceDepthSnapshotClient:
    """Public, credential-free Binance depth client with explicit failure classes."""

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient | None = None,
        base_url: str = BINANCE_USDM_REST_URL,
        timeout_seconds: float = 10.0,
    ) -> None:
        if timeout_seconds <= 0:
            raise ValueError("depth snapshot timeout must be greater than zero")
        self._owns_client = http_client is None
        self._client = http_client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
        )

    async def fetch(self, symbol: str, limit: int) -> Mapping[str, Any]:
        if limit not in VALID_SNAPSHOT_LIMITS:
            raise ValueError("invalid Binance depth snapshot limit")
        try:
            response = await self._client.get(
                "/fapi/v1/depth",
                params={"symbol": symbol.upper(), "limit": limit},
            )
        except httpx.TimeoutException as exc:
            raise ExchangeTimeoutError("Binance depth snapshot timed out") from exc
        except httpx.TransportError as exc:
            raise ExchangeNetworkError("Binance depth snapshot transport failed") from exc
        if response.status_code in {418, 429}:
            raise ExchangeRateLimitError(
                f"Binance depth snapshot rate limited: HTTP {response.status_code}",
                status_code=response.status_code,
                retry_after=_retry_after(response),
            )
        if not 200 <= response.status_code < 300:
            raise ExchangeHTTPError(
                f"Binance depth snapshot failed: HTTP {response.status_code}",
                status_code=response.status_code,
                response_body=response.text[:500],
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise ExchangeMalformedJSONError("Binance depth snapshot returned malformed JSON") from exc
        if not isinstance(payload, Mapping):
            raise ExchangeMalformedJSONError("Binance depth snapshot must be an object")
        return payload

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


class OrderBookLiquidityService:
    """Watch-owned synchronized Binance USD-M visible-liquidity service."""

    def __init__(
        self,
        *,
        transport: FlowWebSocketTransport | None = None,
        snapshot_client: DepthSnapshotClient | None = None,
        stale_after_seconds: float = DEFAULT_ORDER_BOOK_STALE_AFTER_SECONDS,
        max_symbols: int = DEFAULT_ORDER_BOOK_MAX_SYMBOLS,
        update_speed: str = DEFAULT_ORDER_BOOK_UPDATE_SPEED,
        snapshot_limit: int = DEFAULT_ORDER_BOOK_SNAPSHOT_LIMIT,
        event_buffer_size: int = DEFAULT_ORDER_BOOK_EVENT_BUFFER_SIZE,
        bootstrap_concurrency: int = DEFAULT_ORDER_BOOK_BOOTSTRAP_CONCURRENCY,
        bootstrap_min_interval_seconds: float = DEFAULT_ORDER_BOOK_BOOTSTRAP_MIN_INTERVAL_SECONDS,
        bootstrap_attempts: int = DEFAULT_ORDER_BOOK_BOOTSTRAP_ATTEMPTS,
        bootstrap_backoff_seconds: float = DEFAULT_ORDER_BOOK_BOOTSTRAP_BACKOFF_SECONDS,
        bootstrap_jitter_seconds: float = DEFAULT_ORDER_BOOK_BOOTSTRAP_JITTER_SECONDS,
        reconnect_base_seconds: float = DEFAULT_RECONNECT_BASE_SECONDS,
        reconnect_max_seconds: float = DEFAULT_RECONNECT_MAX_SECONDS,
        clock: Any | None = None,
        monotonic: Any | None = None,
        sleep: Any | None = None,
        jitter: Any | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        if stale_after_seconds <= 0:
            raise ValueError("order-book stale threshold must be greater than zero")
        if max_symbols < 1 or max_symbols > ORDER_BOOK_HARD_MAX_SYMBOLS:
            raise ValueError(
                f"order-book max_symbols must be between 1 and {ORDER_BOOK_HARD_MAX_SYMBOLS}"
            )
        if update_speed not in VALID_UPDATE_SPEEDS:
            raise ValueError("order-book update_speed must be 100ms, 250ms, or 500ms")
        if snapshot_limit not in VALID_SNAPSHOT_LIMITS:
            raise ValueError("order-book snapshot_limit is not supported by Binance")
        if event_buffer_size < 1 or event_buffer_size > 4096:
            raise ValueError("order-book event_buffer_size must be between 1 and 4096")
        if bootstrap_concurrency < 1 or bootstrap_concurrency > 8:
            raise ValueError("order-book bootstrap_concurrency must be between 1 and 8")
        if bootstrap_min_interval_seconds < 0:
            raise ValueError("order-book bootstrap interval cannot be negative")
        if bootstrap_attempts < 1 or bootstrap_attempts > 5:
            raise ValueError("order-book bootstrap_attempts must be between 1 and 5")
        if bootstrap_backoff_seconds < 0 or bootstrap_jitter_seconds < 0:
            raise ValueError("order-book bootstrap backoff and jitter cannot be negative")
        if reconnect_base_seconds < 0 or reconnect_max_seconds < reconnect_base_seconds:
            raise ValueError("order-book reconnect delays are invalid")

        self.transport = transport or BinanceWebSocketTransport(
            url=BINANCE_USDM_PUBLIC_STREAM_URL,
            max_message_bytes=ORDER_BOOK_WEBSOCKET_MAX_MESSAGE_BYTES,
            max_queue=ORDER_BOOK_WEBSOCKET_MAX_QUEUE,
            ping_interval_seconds=20.0,
            ping_timeout_seconds=20.0,
        )
        self.snapshot_client = snapshot_client or BinanceDepthSnapshotClient()
        self.stale_after_seconds = float(stale_after_seconds)
        self.max_symbols = int(max_symbols)
        self.update_speed = update_speed
        self.snapshot_limit = int(snapshot_limit)
        self.snapshot_request_weight = SNAPSHOT_REQUEST_WEIGHTS[self.snapshot_limit]
        self.event_buffer_size = int(event_buffer_size)
        self.bootstrap_concurrency = int(bootstrap_concurrency)
        self.bootstrap_min_interval_seconds = float(bootstrap_min_interval_seconds)
        self.bootstrap_attempts = int(bootstrap_attempts)
        self.bootstrap_backoff_seconds = float(bootstrap_backoff_seconds)
        self.bootstrap_jitter_seconds = float(bootstrap_jitter_seconds)
        self.reconnect_base_seconds = float(reconnect_base_seconds)
        self.reconnect_max_seconds = float(reconnect_max_seconds)
        self.clock = clock or (lambda: datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.sleep = sleep or asyncio.sleep
        self.jitter = jitter or (lambda maximum: random.uniform(0.0, maximum))
        self.logger = log or logger

        self._books: dict[str, SynchronizedLocalOrderBook] = {}
        self._desired_symbols: set[str] = set()
        self._active_symbols: set[str] = set()
        self._overflow_symbols: dict[str, str] = {}
        self._connection: FlowWebSocketConnection | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._send_lock = asyncio.Lock()
        self._rate_lock = asyncio.Lock()
        self._bootstrap_semaphore = asyncio.Semaphore(self.bootstrap_concurrency)
        self._bootstrap_tasks: dict[str, asyncio.Task[None]] = {}
        self._next_snapshot_start = 0.0
        self._request_id = 0
        self._ever_connected = False
        self._connection_count = 0
        self._disconnect_count = 0
        self._malformed_event_count = 0
        self._wrong_contract_event_count = 0
        self._ignored_message_count = 0
        self._snapshot_failure_count = 0
        self._snapshot_request_count = 0
        self._snapshot_recovery_scheduled_count = 0
        self._snapshot_recovery_attempt_count = 0
        self._last_error: str | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def subscribed_symbols(self) -> tuple[str, ...]:
        return tuple(sorted(self._desired_symbols))

    async def start(self, symbols: Sequence[str] = ()) -> None:
        await self.reconcile_symbols(symbols)
        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_forever(), name="order-book-liquidity-service")

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
        await self._cancel_bootstraps()
        self._connection = None
        self._active_symbols.clear()
        try:
            await self.snapshot_client.aclose()
        except Exception:
            pass

    async def reconcile_symbols(self, symbols: Sequence[str]) -> tuple[str, ...]:
        requested = {_normalize_symbol(symbol) for symbol in symbols}
        requested.add(BTC_ORDER_BOOK_SYMBOL)
        selected, overflow = _bounded_symbols(requested, maximum=self.max_symbols)
        previous = set(self._desired_symbols)
        additions = selected - previous
        removals = previous - selected
        self._desired_symbols = set(selected)
        self._overflow_symbols = {
            symbol: f"subscription_limit_exceeded:max_symbols={self.max_symbols}"
            for symbol in overflow
        }
        for symbol in additions:
            self._books[symbol] = SynchronizedLocalOrderBook(
                symbol,
                stale_after_seconds=self.stale_after_seconds,
                event_buffer_size=self.event_buffer_size,
            )
        for symbol in removals:
            task = self._bootstrap_tasks.pop(symbol, None)
            if task is not None:
                task.cancel()
            self._books.pop(symbol, None)

        connection = self._connection
        if connection is not None:
            for symbol in additions:
                self._books[symbol].mark_connected(reconnect=False)
            await self._send_subscription_diff(connection, additions=additions, removals=removals)
            for symbol in sorted(additions):
                self._schedule_bootstrap(symbol)
        if additions or removals or overflow:
            self.logger.info(
                "Order-book subscriptions reconciled additions=%s removals=%s active=%s overflow=%s.",
                len(additions),
                len(removals),
                len(selected),
                len(overflow),
            )
        return tuple(sorted(selected))

    def snapshot(self, symbol: str) -> OrderBookLiquiditySnapshot:
        normalized = _normalize_symbol(symbol)
        overflow_reason = self._overflow_symbols.get(normalized)
        if overflow_reason is not None:
            return OrderBookLiquiditySnapshot.unavailable(
                symbol=normalized,
                reason=overflow_reason,
            )
        book = self._books.get(normalized)
        if book is None:
            reason = "service_not_started" if not self.running else "symbol_not_subscribed"
            return OrderBookLiquiditySnapshot.unavailable(symbol=normalized, reason=reason)
        try:
            return book.snapshot(as_of=self.clock())
        except Exception as exc:
            self.logger.warning(
                "Order-book snapshot failed safely for symbol=%s: %s",
                normalized,
                _clean_reason(exc),
            )
            return OrderBookLiquiditySnapshot.unavailable(
                symbol=normalized,
                reason=f"snapshot_error:{type(exc).__name__}",
                status=ContextStatus.ERROR,
                resync_count=book.resync_count,
                gap_count=book.gap_count,
                buffer_overflow_count=book.buffer_overflow_count,
            )

    def health(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "connected": self._connection is not None,
            "desired_symbol_count": len(self._desired_symbols),
            "active_symbol_count": len(self._active_symbols),
            "overflow_symbol_count": len(self._overflow_symbols),
            "active_bootstrap_count": len(self._bootstrap_tasks),
            "connection_count": self._connection_count,
            "disconnect_count": self._disconnect_count,
            "malformed_event_count": self._malformed_event_count,
            "wrong_contract_event_count": self._wrong_contract_event_count,
            "ignored_message_count": self._ignored_message_count,
            "snapshot_request_count": self._snapshot_request_count,
            "snapshot_failure_count": self._snapshot_failure_count,
            "snapshot_recovery_scheduled_count": self._snapshot_recovery_scheduled_count,
            "snapshot_recovery_attempt_count": self._snapshot_recovery_attempt_count,
            "snapshot_limit": self.snapshot_limit,
            "snapshot_request_weight": self.snapshot_request_weight,
            "update_speed": self.update_speed,
            "event_buffer_size": self.event_buffer_size,
            "websocket_max_message_bytes": getattr(
                self.transport,
                "max_message_bytes",
                None,
            ),
            "websocket_queue_bound": getattr(self.transport, "max_queue", None),
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
                self._active_symbols.clear()
                for book in self._books.values():
                    book.mark_connected(reconnect=reconnect)
                await self._send_subscription_diff(
                    connection,
                    additions=set(self._desired_symbols),
                    removals=set(),
                )
                for symbol in sorted(self._desired_symbols):
                    self._schedule_bootstrap(symbol)
                failure_count = 0
                self._last_error = None
                self.logger.info(
                    "Order-book WebSocket connected symbols=%s reconnect=%s speed=%s "
                    "max_message_bytes=%s max_queue=%s.",
                    len(self._desired_symbols),
                    reconnect,
                    self.update_speed,
                    getattr(self.transport, "max_message_bytes", None),
                    getattr(self.transport, "max_queue", None),
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
                    "Order-book WebSocket disconnected attempt=%s error=%s.",
                    failure_count,
                    self._last_error,
                )
            finally:
                if self._connection is connection:
                    self._connection = None
                self._active_symbols.clear()
                await self._cancel_bootstraps()
                if connection is not None:
                    await _safe_close(connection)
                for book in self._books.values():
                    book.mark_disconnected()
            if self._stop_event.is_set():
                break
            delay = min(
                self.reconnect_max_seconds,
                self.reconnect_base_seconds * (2 ** min(max(failure_count - 1, 0), 10)),
            )
            if delay > 0:
                await self.sleep(delay)

    def _handle_payload(self, payload: str | bytes | Mapping[str, Any]) -> None:
        if _is_control_message(payload):
            self._ignored_message_count += 1
            return
        try:
            event = parse_binance_depth_event(payload)
        except WrongOrderBookContractTypeError:
            self._wrong_contract_event_count += 1
            return
        except OrderBookPayloadError:
            self._malformed_event_count += 1
            return
        book = self._books.get(event.symbol)
        if book is None or event.symbol not in self._desired_symbols:
            self._ignored_message_count += 1
            return
        outcome = book.ingest(event, received_at=self.clock())
        if outcome in {
            BookIngestOutcome.NEEDS_RESYNC,
            BookIngestOutcome.BUFFER_OVERFLOW,
        }:
            self._schedule_bootstrap(event.symbol)

    def _schedule_bootstrap(self, symbol: str) -> None:
        if self._connection is None or self._stop_event.is_set():
            return
        current = self._bootstrap_tasks.get(symbol)
        if current is not None and not current.done():
            return
        book = self._books.get(symbol)
        if book is None or symbol not in self._desired_symbols:
            return
        book.mark_bootstrap_started()
        task = asyncio.create_task(
            self._bootstrap_symbol(symbol),
            name=f"order-book-bootstrap-{symbol.lower()}",
        )
        self._bootstrap_tasks[symbol] = task
        task.add_done_callback(lambda completed, key=symbol: self._bootstrap_done(key, completed))

    def _bootstrap_done(self, symbol: str, task: asyncio.Task[None]) -> None:
        if self._bootstrap_tasks.get(symbol) is task:
            self._bootstrap_tasks.pop(symbol, None)
        if task.cancelled():
            return
        try:
            task.result()
        except Exception as exc:
            self._last_error = f"bootstrap_task:{type(exc).__name__}:{_clean_reason(exc)}"
        book = self._books.get(symbol)
        if (
            book is not None
            and book.needs_resync
            and self._connection is not None
            and not self._stop_event.is_set()
        ):
            self._schedule_bootstrap(symbol)

    async def _bootstrap_symbol(self, symbol: str) -> None:
        book = self._books.get(symbol)
        connection = self._connection
        if book is None or connection is None:
            return
        recovery_cycle = 0
        while self._bootstrap_context_active(symbol, book=book, connection=connection):
            last_error: Exception | None = None
            for attempt in range(1, self.bootstrap_attempts + 1):
                if not self._bootstrap_context_active(symbol, book=book, connection=connection):
                    return
                try:
                    async with self._bootstrap_semaphore:
                        await self._wait_for_snapshot_slot()
                        if not self._bootstrap_context_active(
                            symbol,
                            book=book,
                            connection=connection,
                        ):
                            return
                        self._snapshot_request_count += 1
                        payload = await self.snapshot_client.fetch(symbol, self.snapshot_limit)
                    if not self._bootstrap_context_active(
                        symbol,
                        book=book,
                        connection=connection,
                    ):
                        return
                    snapshot = parse_binance_depth_snapshot(payload)
                    outcome = book.install_snapshot(snapshot, received_at=self.clock())
                    if outcome != BookIngestOutcome.NEEDS_RESYNC:
                        return
                    last_error = RuntimeError(book.reason)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    last_error = exc
                if attempt < self.bootstrap_attempts:
                    retry_after = max(
                        float(getattr(last_error, "retry_after", 0) or 0),
                        0.0,
                    )
                    exponential = self.bootstrap_backoff_seconds * (2 ** (attempt - 1))
                    delay = max(exponential, retry_after) + self.jitter(
                        self.bootstrap_jitter_seconds
                    )
                    if delay > 0:
                        await self.sleep(delay)

            self._snapshot_failure_count += 1
            book.mark_snapshot_failed()
            if last_error is not None:
                self._last_error = f"snapshot_failed:{symbol}:{type(last_error).__name__}"

            recovery_cycle += 1
            self._snapshot_recovery_scheduled_count += 1
            retry_after = max(float(getattr(last_error, "retry_after", 0) or 0), 0.0)
            recovery_base = max(
                DEFAULT_RECONNECT_BASE_SECONDS,
                self.reconnect_base_seconds,
                self.bootstrap_backoff_seconds,
            )
            recovery_ceiling = max(recovery_base, self.reconnect_max_seconds)
            exponential = recovery_base * (2 ** min(recovery_cycle - 1, 10))
            delay = max(min(exponential, recovery_ceiling), retry_after) + self.jitter(
                self.bootstrap_jitter_seconds
            )
            await self.sleep(delay)
            if not self._bootstrap_context_active(symbol, book=book, connection=connection):
                return
            self._snapshot_recovery_attempt_count += 1
            book.mark_bootstrap_started()

    def _bootstrap_context_active(
        self,
        symbol: str,
        *,
        book: SynchronizedLocalOrderBook,
        connection: FlowWebSocketConnection,
    ) -> bool:
        return (
            self._connection is connection
            and not self._stop_event.is_set()
            and symbol in self._desired_symbols
            and self._books.get(symbol) is book
        )

    async def _wait_for_snapshot_slot(self) -> None:
        async with self._rate_lock:
            now = float(self.monotonic())
            delay = max(self._next_snapshot_start - now, 0.0)
            if delay > 0:
                await self.sleep(delay)
            started = float(self.monotonic())
            self._next_snapshot_start = started + self.bootstrap_min_interval_seconds

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
                "params": [
                    f"{symbol.lower()}@depth@{self.update_speed}" for symbol in sorted(symbols)
                ],
                "id": self._request_id,
            },
            separators=(",", ":"),
            sort_keys=True,
        )

    async def _cancel_bootstraps(self) -> None:
        tasks = tuple(self._bootstrap_tasks.values())
        self._bootstrap_tasks.clear()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


def _bounded_symbols(symbols: set[str], *, maximum: int) -> tuple[set[str], set[str]]:
    ordered = [
        BTC_ORDER_BOOK_SYMBOL,
        *sorted(symbol for symbol in symbols if symbol != BTC_ORDER_BOOK_SYMBOL),
    ]
    selected = set(ordered[:maximum])
    return selected, set(ordered[maximum:])


def _normalize_symbol(value: Any) -> str:
    normalized = str(value).strip().upper()
    if not normalized:
        raise ValueError("order-book subscription symbol must not be blank")
    return normalized


def _is_control_message(payload: str | bytes | Mapping[str, Any]) -> bool:
    decoded: Any = payload
    if isinstance(payload, bytes):
        try:
            decoded = payload.decode("utf-8")
        except UnicodeDecodeError:
            return False
    if isinstance(decoded, str):
        try:
            decoded = json.loads(decoded)
        except json.JSONDecodeError:
            return False
    return isinstance(decoded, Mapping) and "id" in decoded and "e" not in decoded and "data" not in decoded


def _retry_after(response: httpx.Response) -> float | None:
    value = response.headers.get("Retry-After")
    if value is None:
        return None
    try:
        return max(float(value), 0.0)
    except ValueError:
        return None


async def _safe_close(connection: FlowWebSocketConnection) -> None:
    try:
        await connection.close()
    except Exception:
        return


def _clean_reason(exc: Exception) -> str:
    return str(exc).strip().replace("\n", " ")[:300] or type(exc).__name__
