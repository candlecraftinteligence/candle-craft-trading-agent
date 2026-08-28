from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any

from app.context.models import ContextStatus
from app.data.candle_integrity import normalize_utc_timestamp
from app.microstructure.order_book_models import (
    LiquidityBandSnapshot,
    OrderBookLiquiditySnapshot,
    VisibleLevelConcentration,
)


DEFAULT_DEPTH_BANDS_BPS = (10, 25, 50, 100)
MONEY_QUANT = Decimal("0.00000001")
RATIO_QUANT = Decimal("0.00000001")
BPS_QUANT = Decimal("0.0001")
TEN_THOUSAND = Decimal("10000")


class OrderBookPayloadError(ValueError):
    pass


class WrongOrderBookContractTypeError(OrderBookPayloadError):
    pass


class BookIngestOutcome(str, Enum):
    BUFFERED = "BUFFERED"
    SYNCHRONIZED = "SYNCHRONIZED"
    APPLIED = "APPLIED"
    IGNORED_OLD = "IGNORED_OLD"
    NEEDS_RESYNC = "NEEDS_RESYNC"
    BUFFER_OVERFLOW = "BUFFER_OVERFLOW"


@dataclass(frozen=True, slots=True)
class DepthLevelUpdate:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class BinanceDepthEvent:
    symbol: str
    pair_symbol: str
    event_time: datetime
    transaction_time: datetime
    first_update_id: int
    final_update_id: int
    previous_final_update_id: int
    bids: tuple[DepthLevelUpdate, ...]
    asks: tuple[DepthLevelUpdate, ...]


@dataclass(frozen=True, slots=True)
class BinanceDepthSnapshot:
    last_update_id: int
    event_time: datetime
    transaction_time: datetime
    bids: tuple[DepthLevelUpdate, ...]
    asks: tuple[DepthLevelUpdate, ...]


@dataclass(frozen=True, slots=True)
class _BufferedDepthEvent:
    event: BinanceDepthEvent
    received_at: datetime


def parse_binance_depth_event(payload: str | bytes | Mapping[str, Any]) -> BinanceDepthEvent:
    decoded = _decode_mapping(payload, label="depth event")
    if isinstance(decoded.get("data"), Mapping):
        decoded = decoded["data"]
    if decoded.get("e") != "depthUpdate":
        raise OrderBookPayloadError("depth event type must be depthUpdate")

    stream_type = decoded.get("st")
    if isinstance(stream_type, bool) or not isinstance(stream_type, int):
        raise OrderBookPayloadError("depth event st must be integer 1 for USD-M")
    if stream_type != 1:
        if stream_type == 2:
            raise WrongOrderBookContractTypeError("COIN-M depth event rejected")
        raise OrderBookPayloadError("depth event st must be 1 for USD-M")

    symbol = _required_text(decoded, "s").upper()
    pair_symbol = _required_text(decoded, "ps").upper()
    first_update_id = _required_non_negative_int(decoded, "U")
    final_update_id = _required_non_negative_int(decoded, "u")
    previous_final_update_id = _required_non_negative_int(decoded, "pu")
    if first_update_id > final_update_id:
        raise OrderBookPayloadError("depth event U cannot exceed u")
    return BinanceDepthEvent(
        symbol=symbol,
        pair_symbol=pair_symbol,
        event_time=_timestamp_from_ms(decoded.get("E"), field_name="E"),
        transaction_time=_timestamp_from_ms(decoded.get("T"), field_name="T"),
        first_update_id=first_update_id,
        final_update_id=final_update_id,
        previous_final_update_id=previous_final_update_id,
        bids=_parse_levels(decoded.get("b"), label="b"),
        asks=_parse_levels(decoded.get("a"), label="a"),
    )


def parse_binance_depth_snapshot(payload: Mapping[str, Any]) -> BinanceDepthSnapshot:
    if not isinstance(payload, Mapping):
        raise OrderBookPayloadError("depth snapshot must be an object")
    return BinanceDepthSnapshot(
        last_update_id=_required_non_negative_int(payload, "lastUpdateId"),
        event_time=_timestamp_from_ms(payload.get("E"), field_name="E"),
        transaction_time=_timestamp_from_ms(payload.get("T"), field_name="T"),
        bids=_parse_levels(payload.get("bids"), label="bids"),
        asks=_parse_levels(payload.get("asks"), label="asks"),
    )


class SynchronizedLocalOrderBook:
    """Finite, fail-closed USD-M local book initialized from one REST snapshot."""

    def __init__(
        self,
        symbol: str,
        *,
        stale_after_seconds: float,
        event_buffer_size: int,
        bands_bps: Sequence[int] = DEFAULT_DEPTH_BANDS_BPS,
    ) -> None:
        normalized = str(symbol).strip().upper()
        if not normalized:
            raise ValueError("order-book symbol must not be blank")
        if stale_after_seconds <= 0:
            raise ValueError("order-book stale threshold must be greater than zero")
        if event_buffer_size < 1:
            raise ValueError("order-book event buffer size must be positive")
        normalized_bands = tuple(sorted({int(value) for value in bands_bps}))
        if not normalized_bands or any(value <= 0 for value in normalized_bands):
            raise ValueError("order-book bands must contain positive basis-point values")

        self.symbol = normalized
        self.stale_after_seconds = float(stale_after_seconds)
        self.event_buffer_size = int(event_buffer_size)
        self.bands_bps = normalized_bands
        self.bids: dict[Decimal, Decimal] = {}
        self.asks: dict[Decimal, Decimal] = {}
        self.last_update_id: int | None = None
        self.last_transaction_time: datetime | None = None
        self.last_event_time: datetime | None = None
        self.snapshot_time: datetime | None = None
        self.last_valid_update: datetime | None = None
        self.synchronized = False
        self.stream_connected = False
        self.reason = "warming_up"
        self.resync_count = 0
        self.gap_count = 0
        self.duplicate_event_count = 0
        self.out_of_order_event_count = 0
        self.buffer_overflow_count = 0
        self._buffer: list[_BufferedDepthEvent] = []
        self._snapshot_installed = False
        self._needs_resync = True
        self._coverage_bid_floor: Decimal | None = None
        self._coverage_ask_ceiling: Decimal | None = None

    @property
    def buffered_event_count(self) -> int:
        return len(self._buffer)

    @property
    def needs_resync(self) -> bool:
        return self._needs_resync

    def mark_connected(self, *, reconnect: bool) -> None:
        self.stream_connected = True
        self._clear_trusted_book()
        self._buffer.clear()
        self.reason = "resyncing" if reconnect else "snapshot_pending"
        self._needs_resync = True

    def mark_disconnected(self) -> None:
        self.stream_connected = False
        self._clear_trusted_book()
        self._buffer.clear()
        self.reason = "stream_disconnected"
        self._needs_resync = True

    def mark_bootstrap_started(self) -> None:
        self.resync_count += 1
        self.reason = "snapshot_pending" if self.resync_count == 1 else "resyncing"

    def mark_snapshot_failed(self) -> None:
        self._clear_trusted_book()
        self.reason = "snapshot_failed"
        self._needs_resync = False

    def ingest(
        self,
        event: BinanceDepthEvent,
        *,
        received_at: datetime,
    ) -> BookIngestOutcome:
        received = normalize_utc_timestamp(received_at, field_name="order_book_received_at")
        if event.symbol != self.symbol:
            raise ValueError(f"depth event symbol {event.symbol} does not match {self.symbol}")

        if self.synchronized:
            return self._apply_live_event(event, received_at=received)

        if len(self._buffer) >= self.event_buffer_size:
            self.buffer_overflow_count += 1
            self._invalidate("buffer_overflow", gap=False)
            return BookIngestOutcome.BUFFER_OVERFLOW
        self._buffer.append(_BufferedDepthEvent(event=event, received_at=received))
        if not self._snapshot_installed:
            return BookIngestOutcome.BUFFERED
        return self._reconcile_buffer()

    def install_snapshot(
        self,
        snapshot: BinanceDepthSnapshot,
        *,
        received_at: datetime,
    ) -> BookIngestOutcome:
        snapshot_received = normalize_utc_timestamp(
            received_at,
            field_name="order_book_snapshot_received_at",
        )
        bids = {level.price: level.quantity for level in snapshot.bids if level.quantity > 0}
        asks = {level.price: level.quantity for level in snapshot.asks if level.quantity > 0}
        if not bids or not asks or max(bids) >= min(asks):
            self._invalidate("invalid_snapshot_book", gap=False)
            return BookIngestOutcome.NEEDS_RESYNC

        self.bids = bids
        self.asks = asks
        self.last_update_id = snapshot.last_update_id
        self.last_transaction_time = snapshot.transaction_time
        self.last_event_time = snapshot.event_time
        self.snapshot_time = snapshot_received
        self.last_valid_update = None
        self.synchronized = False
        self._snapshot_installed = True
        self._needs_resync = False
        self._coverage_bid_floor = min(bids)
        self._coverage_ask_ceiling = max(asks)
        self.reason = "synchronizing"
        return self._reconcile_buffer()

    def snapshot(self, *, as_of: datetime) -> OrderBookLiquiditySnapshot:
        now = normalize_utc_timestamp(as_of, field_name="order_book_snapshot_at")
        observed_at = self.last_event_time
        age_seconds = (
            max((now - observed_at).total_seconds(), 0.0) if observed_at is not None else None
        )
        if not self.synchronized:
            status = (
                ContextStatus.STALE
                if self.reason in {"sequence_gap", "crossed_book"} and observed_at is not None
                else ContextStatus.ERROR
                if self.reason in {"snapshot_failed", "invalid_snapshot_book"}
                else ContextStatus.UNAVAILABLE
            )
            return OrderBookLiquiditySnapshot.unavailable(
                symbol=self.symbol,
                reason=self.reason,
                status=status,
                observed_at=observed_at,
                age_seconds=age_seconds,
                resync_count=self.resync_count,
                gap_count=self.gap_count,
                duplicate_event_count=self.duplicate_event_count,
                out_of_order_event_count=self.out_of_order_event_count,
                buffer_overflow_count=self.buffer_overflow_count,
            )

        best_bid = max(self.bids)
        best_ask = min(self.asks)
        if best_bid >= best_ask:
            self._invalidate("crossed_book", gap=False)
            return self.snapshot(as_of=now)
        mid = (best_bid + best_ask) / Decimal("2")
        spread = best_ask - best_bid
        spread_bps = spread / mid * TEN_THOUSAND
        furthest_bid = self._distance_bps(mid, self._coverage_bid_floor, side="bid")
        furthest_ask = self._distance_bps(mid, self._coverage_ask_ceiling, side="ask")

        bands: dict[str, LiquidityBandSnapshot] = {}
        observed_by_band: dict[int, tuple[Decimal, Decimal]] = {}
        for band_bps in self.bands_bps:
            band = Decimal(band_bps)
            bid_floor = mid * (Decimal("1") - band / TEN_THOUSAND)
            ask_ceiling = mid * (Decimal("1") + band / TEN_THOUSAND)
            bid_quote = sum(
                (price * quantity for price, quantity in self.bids.items() if price >= bid_floor),
                Decimal("0"),
            )
            ask_quote = sum(
                (price * quantity for price, quantity in self.asks.items() if price <= ask_ceiling),
                Decimal("0"),
            )
            total = bid_quote + ask_quote
            imbalance = (bid_quote - ask_quote) / total if total > 0 else None
            observed_by_band[band_bps] = (bid_quote, ask_quote)
            bands[f"{band_bps}bps"] = LiquidityBandSnapshot(
                band_bps=band_bps,
                bid_quote_notional=_money(bid_quote),
                ask_quote_notional=_money(ask_quote),
                depth_imbalance=_ratio(imbalance),
                bid_coverage_complete=furthest_bid is not None and furthest_bid >= band,
                ask_coverage_complete=furthest_ask is not None and furthest_ask >= band,
            )

        outer_band = self.bands_bps[-1]
        outer_bid_quote, outer_ask_quote = observed_by_band[outer_band]
        outer = Decimal(outer_band)
        bid_floor = mid * (Decimal("1") - outer / TEN_THOUSAND)
        ask_ceiling = mid * (Decimal("1") + outer / TEN_THOUSAND)
        largest_bid = _largest_concentration(
            ((price, quantity) for price, quantity in self.bids.items() if price >= bid_floor),
            mid=mid,
            side="bid",
            band_bps=outer_band,
            observed_band_quote=outer_bid_quote,
        )
        largest_ask = _largest_concentration(
            ((price, quantity) for price, quantity in self.asks.items() if price <= ask_ceiling),
            mid=mid,
            side="ask",
            band_bps=outer_band,
            observed_band_quote=outer_ask_quote,
        )
        complete = all(
            band.bid_coverage_complete and band.ask_coverage_complete for band in bands.values()
        )
        status = (
            ContextStatus.STALE
            if age_seconds is None or age_seconds > self.stale_after_seconds
            else ContextStatus.VERIFIED
        )
        reason = "stale_book" if status == ContextStatus.STALE else None
        if status == ContextStatus.VERIFIED and not complete:
            reason = "insufficient_book_coverage"
        return OrderBookLiquiditySnapshot(
            symbol=self.symbol,
            status=status,
            reason=reason,
            observed_at=observed_at,
            age_seconds=age_seconds,
            synchronized=True,
            last_update_id=self.last_update_id,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid,
            spread_absolute=spread,
            spread_bps=_bps(spread_bps),
            bands=bands,
            furthest_bid_distance_bps=_bps(furthest_bid),
            furthest_ask_distance_bps=_bps(furthest_ask),
            largest_bid_level=largest_bid,
            largest_ask_level=largest_ask,
            level_count_bid=len(self.bids),
            level_count_ask=len(self.asks),
            resync_count=self.resync_count,
            gap_count=self.gap_count,
            duplicate_event_count=self.duplicate_event_count,
            out_of_order_event_count=self.out_of_order_event_count,
            buffer_overflow_count=self.buffer_overflow_count,
        )

    def _reconcile_buffer(self) -> BookIngestOutcome:
        if not self._snapshot_installed or self.last_update_id is None:
            return BookIngestOutcome.BUFFERED
        self._buffer = [
            item for item in self._buffer if item.event.final_update_id >= self.last_update_id
        ]
        if not self._buffer:
            return BookIngestOutcome.BUFFERED

        bridge = self._buffer[0]
        event = bridge.event
        if not (
            event.first_update_id <= self.last_update_id <= event.final_update_id
        ):
            self._invalidate("invalid_initial_bridge", gap=True)
            return BookIngestOutcome.NEEDS_RESYNC

        self._apply_levels(event, received_at=bridge.received_at)
        self.synchronized = True
        self.reason = None
        self._needs_resync = False
        remaining = self._buffer[1:]
        self._buffer.clear()
        for item in remaining:
            outcome = self._apply_live_event(item.event, received_at=item.received_at)
            if outcome == BookIngestOutcome.NEEDS_RESYNC:
                return outcome
        return BookIngestOutcome.SYNCHRONIZED

    def _apply_live_event(
        self,
        event: BinanceDepthEvent,
        *,
        received_at: datetime,
    ) -> BookIngestOutcome:
        current_update_id = self.last_update_id
        if current_update_id is None:
            self._invalidate("missing_update_id", gap=True)
            return BookIngestOutcome.NEEDS_RESYNC
        if event.final_update_id <= current_update_id:
            if event.final_update_id == current_update_id:
                self.duplicate_event_count += 1
            else:
                self.out_of_order_event_count += 1
            return BookIngestOutcome.IGNORED_OLD
        if event.previous_final_update_id != current_update_id:
            self._invalidate("sequence_gap", gap=True)
            return BookIngestOutcome.NEEDS_RESYNC
        self._apply_levels(event, received_at=received_at)
        if not self.bids or not self.asks or max(self.bids) >= min(self.asks):
            self._invalidate("crossed_book", gap=False)
            return BookIngestOutcome.NEEDS_RESYNC
        return BookIngestOutcome.APPLIED

    def _apply_levels(self, event: BinanceDepthEvent, *, received_at: datetime) -> None:
        _apply_side(self.bids, event.bids)
        _apply_side(self.asks, event.asks)
        self.last_update_id = event.final_update_id
        self.last_event_time = event.event_time
        self.last_transaction_time = event.transaction_time
        self.last_valid_update = received_at

    def _invalidate(self, reason: str, *, gap: bool) -> None:
        if gap:
            self.gap_count += 1
        self._clear_trusted_book(preserve_observation=True)
        self._buffer.clear()
        self.reason = reason
        self._needs_resync = True

    def _clear_trusted_book(self, *, preserve_observation: bool = False) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_update_id = None
        self.snapshot_time = None
        self.last_valid_update = None
        self.synchronized = False
        self._snapshot_installed = False
        self._coverage_bid_floor = None
        self._coverage_ask_ceiling = None
        if not preserve_observation:
            self.last_event_time = None
            self.last_transaction_time = None

    @staticmethod
    def _distance_bps(
        mid: Decimal,
        boundary: Decimal | None,
        *,
        side: str,
    ) -> Decimal | None:
        if boundary is None or mid <= 0:
            return None
        distance = mid - boundary if side == "bid" else boundary - mid
        return max(distance / mid * TEN_THOUSAND, Decimal("0"))


def _apply_side(book: dict[Decimal, Decimal], updates: Sequence[DepthLevelUpdate]) -> None:
    for level in updates:
        if level.quantity == 0:
            book.pop(level.price, None)
        else:
            book[level.price] = level.quantity


def _largest_concentration(
    levels: Any,
    *,
    mid: Decimal,
    side: str,
    band_bps: int,
    observed_band_quote: Decimal,
) -> VisibleLevelConcentration | None:
    candidates = [(price, quantity, price * quantity) for price, quantity in levels]
    if not candidates:
        return None
    price, _quantity, quote = max(candidates, key=lambda item: (item[2], item[0]))
    raw_distance = mid - price if side == "bid" else price - mid
    share = quote / observed_band_quote if observed_band_quote > 0 else None
    return VisibleLevelConcentration(
        price=price,
        quote_notional=_money(quote),
        distance_bps=_bps(raw_distance / mid * TEN_THOUSAND) or Decimal("0"),
        share_of_observed_band=_ratio(share),
        band_bps=band_bps,
    )


def _decode_mapping(payload: str | bytes | Mapping[str, Any], *, label: str) -> Mapping[str, Any]:
    if isinstance(payload, Mapping):
        return payload
    if isinstance(payload, bytes):
        try:
            payload = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise OrderBookPayloadError(f"{label} is not valid UTF-8") from exc
    if not isinstance(payload, str):
        raise OrderBookPayloadError(f"{label} must be JSON or an object")
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise OrderBookPayloadError(f"{label} is not valid JSON") from exc
    if not isinstance(decoded, Mapping):
        raise OrderBookPayloadError(f"{label} must decode to an object")
    return decoded


def _parse_levels(value: Any, *, label: str) -> tuple[DepthLevelUpdate, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise OrderBookPayloadError(f"{label} must be an array")
    levels: list[DepthLevelUpdate] = []
    for index, item in enumerate(value):
        if not isinstance(item, Sequence) or isinstance(item, (str, bytes)) or len(item) != 2:
            raise OrderBookPayloadError(f"{label}[{index}] must be [price, quantity]")
        price = _decimal(item[0], field_name=f"{label}[{index}].price")
        quantity = _decimal(item[1], field_name=f"{label}[{index}].quantity")
        if price <= 0:
            raise OrderBookPayloadError(f"{label}[{index}] price must be positive")
        if quantity < 0:
            raise OrderBookPayloadError(f"{label}[{index}] quantity cannot be negative")
        levels.append(DepthLevelUpdate(price=price, quantity=quantity))
    return tuple(levels)


def _decimal(value: Any, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise OrderBookPayloadError(f"{field_name} must be decimal-compatible")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise OrderBookPayloadError(f"{field_name} must be decimal-compatible") from exc
    if not decimal_value.is_finite():
        raise OrderBookPayloadError(f"{field_name} must be finite")
    return decimal_value


def _required_text(payload: Mapping[str, Any], field_name: str) -> str:
    value = payload.get(field_name)
    text = str(value).strip() if value is not None else ""
    if not text:
        raise OrderBookPayloadError(f"depth event {field_name} is required")
    return text


def _required_non_negative_int(payload: Mapping[str, Any], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OrderBookPayloadError(f"{field_name} must be a non-negative integer")
    return value


def _timestamp_from_ms(value: Any, *, field_name: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise OrderBookPayloadError(f"{field_name} must be a non-negative millisecond timestamp")
    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    except (OSError, OverflowError, ValueError) as exc:
        raise OrderBookPayloadError(f"{field_name} timestamp is out of range") from exc


def _money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT)


def _ratio(value: Decimal | None) -> Decimal | None:
    return value.quantize(RATIO_QUANT) if value is not None else None


def _bps(value: Decimal | None) -> Decimal | None:
    return value.quantize(BPS_QUANT) if value is not None else None
