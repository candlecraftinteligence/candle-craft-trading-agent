from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import blake2b

from app.context.models import ContextStatus
from app.data.candle_integrity import normalize_utc_timestamp
from app.microstructure.liquidation import BinanceLiquidationOrder, LiquidatedPositionSide
from app.microstructure.liquidation_models import (
    LIQUIDATION_SOURCE,
    LiquidationAcceleration,
    LiquidationAccelerationSnapshot,
    LiquidationFlowSnapshot,
    LiquidationWindowSnapshot,
)


LIQUIDATION_WINDOWS_MINUTES = (1, 5, 15)
MAX_RETAINED_LIQUIDATION_BUCKETS = max(LIQUIDATION_WINDOWS_MINUTES) + 1
DEFAULT_MAX_DEDUPE_FINGERPRINTS = 128
ZERO = Decimal("0")
MONEY_QUANT = Decimal("0.00000001")
RATIO_QUANT = Decimal("0.00000001")
RATE_QUANT = Decimal("0.00000001")


@dataclass(slots=True)
class MinuteLiquidationBucket:
    bucket_start: datetime
    long_quote: Decimal = ZERO
    short_quote: Decimal = ZERO
    event_count: int = 0
    long_event_count: int = 0
    short_event_count: int = 0
    largest_long: Decimal = ZERO
    largest_short: Decimal = ZERO

    @property
    def total_quote(self) -> Decimal:
        return self.long_quote + self.short_quote

    def apply(self, event: BinanceLiquidationOrder) -> None:
        notional = event.quote_notional
        if event.liquidated_position_side == LiquidatedPositionSide.LONG:
            self.long_quote += notional
            self.long_event_count += 1
            self.largest_long = max(self.largest_long, notional)
        else:
            self.short_quote += notional
            self.short_event_count += 1
            self.largest_short = max(self.largest_short, notional)
        self.event_count += 1


class SymbolLiquidationAggregator:
    """Aggregate force-order observations without retaining raw events."""

    def __init__(
        self,
        symbol: str,
        *,
        stale_after_seconds: float,
        max_retained_buckets: int = MAX_RETAINED_LIQUIDATION_BUCKETS,
        max_dedupe_fingerprints: int = DEFAULT_MAX_DEDUPE_FINGERPRINTS,
    ) -> None:
        normalized = str(symbol).strip().upper()
        if not normalized:
            raise ValueError("liquidation symbol must not be blank")
        if stale_after_seconds <= 0:
            raise ValueError("liquidation stale threshold must be greater than zero")
        if max_retained_buckets < MAX_RETAINED_LIQUIDATION_BUCKETS:
            raise ValueError(
                f"max_retained_buckets must be at least {MAX_RETAINED_LIQUIDATION_BUCKETS}"
            )
        if max_dedupe_fingerprints < 1:
            raise ValueError("max_dedupe_fingerprints must be positive")
        self.symbol = normalized
        self.stale_after_seconds = float(stale_after_seconds)
        self.max_retained_buckets = int(max_retained_buckets)
        self.max_dedupe_fingerprints = int(max_dedupe_fingerprints)
        self._buckets: dict[datetime, MinuteLiquidationBucket] = {}
        self._dedupe_order: deque[bytes] = deque()
        self._dedupe_seen: set[bytes] = set()
        self._connected = False
        self._coverage_start: datetime | None = None
        self._coverage_reason = "insufficient_window_coverage"
        self._last_state_change: datetime | None = None
        self._accepted_event_count = 0
        self._duplicate_event_count = 0
        self._stale_event_count = 0
        self._gap_count = 0
        self._reconnect_count = 0

    @property
    def retained_bucket_count(self) -> int:
        return len(self._buckets)

    @property
    def dedupe_fingerprint_count(self) -> int:
        return len(self._dedupe_order)

    def mark_connected(self, at: datetime, *, reconnect: bool = False) -> None:
        connected_at = normalize_utc_timestamp(at, field_name="liquidation_connected_at")
        if reconnect:
            self._reconnect_count += 1
            self._coverage_reason = "connection_reconnect_in_window"
        elif self._coverage_start is None:
            self._coverage_reason = "insufficient_window_coverage"
        self._coverage_start = connected_at
        self._last_state_change = connected_at
        self._connected = True

    def mark_disconnected(self, at: datetime) -> None:
        disconnected_at = normalize_utc_timestamp(at, field_name="liquidation_disconnected_at")
        if self._connected:
            self._gap_count += 1
        self._connected = False
        self._coverage_start = None
        self._coverage_reason = "connection_gap_in_window"
        self._last_state_change = disconnected_at

    def ingest(self, event: BinanceLiquidationOrder, *, received_at: datetime) -> bool:
        if event.symbol != self.symbol:
            raise ValueError(
                f"forceOrder symbol {event.symbol} does not match aggregator {self.symbol}"
            )
        received = normalize_utc_timestamp(
            received_at,
            field_name="liquidation_received_at",
        )
        fingerprint = _event_fingerprint(event)
        if fingerprint in self._dedupe_seen:
            self._duplicate_event_count += 1
            return False

        lag_seconds = (received - event.trade_time).total_seconds()
        if lag_seconds > self.stale_after_seconds:
            self._stale_event_count += 1
            self._coverage_start = received
            self._coverage_reason = "stale_event_lag"
            self._last_state_change = received
            return False

        bucket_start = _minute_start(event.trade_time)
        bucket = self._buckets.get(bucket_start)
        if bucket is None:
            bucket = MinuteLiquidationBucket(bucket_start=bucket_start)
            self._buckets[bucket_start] = bucket
        bucket.apply(event)
        self._remember_fingerprint(fingerprint)
        self._accepted_event_count += 1
        self._prune_buckets()
        return True

    def snapshot(self, *, as_of: datetime) -> LiquidationFlowSnapshot:
        now = normalize_utc_timestamp(as_of, field_name="liquidation_snapshot_at")
        windows = {
            f"{minutes}m": self._window_snapshot(minutes=minutes, as_of=now)
            for minutes in LIQUIDATION_WINDOWS_MINUTES
        }
        canonical = windows["15m"]
        if self._connected:
            observed_at = now
            age_seconds = 0.0
        else:
            observed_at = self._last_state_change
            age_seconds = (
                max((now - observed_at).total_seconds(), 0.0)
                if observed_at is not None
                else None
            )
        return LiquidationFlowSnapshot(
            symbol=self.symbol,
            source=LIQUIDATION_SOURCE,
            observed_at=observed_at,
            age_seconds=age_seconds,
            status=canonical.status,
            reason=canonical.reason,
            windows=windows,
            liquidation_summary=_liquidation_summary(
                windows,
                status=canonical.status,
                reason=canonical.reason,
            ),
            retained_bucket_count=len(self._buckets),
            max_retained_bucket_count=self.max_retained_buckets,
            dedupe_fingerprint_count=len(self._dedupe_order),
            max_dedupe_fingerprint_count=self.max_dedupe_fingerprints,
            accepted_event_count=self._accepted_event_count,
            duplicate_event_count=self._duplicate_event_count,
            stale_event_count=self._stale_event_count,
            reconnect_count=self._reconnect_count,
            disconnect_count=self._gap_count,
        )

    def _window_snapshot(self, *, minutes: int, as_of: datetime) -> LiquidationWindowSnapshot:
        window_end = _minute_start(as_of)
        window_start = window_end - timedelta(minutes=minutes)
        coverage_seconds = self._coverage_seconds(window_start, window_end)
        coverage_complete = bool(
            self._connected
            and self._coverage_start is not None
            and self._coverage_start <= window_start
        )
        if not self._connected:
            status = ContextStatus.UNAVAILABLE
            reason = "stream_disconnected"
        elif not coverage_complete:
            status = (
                ContextStatus.STALE
                if self._coverage_reason == "stale_event_lag"
                else ContextStatus.UNAVAILABLE
            )
            reason = self._coverage_reason
        else:
            status = ContextStatus.VERIFIED
            reason = None

        if not coverage_complete:
            return LiquidationWindowSnapshot(
                window_minutes=minutes,
                window_start=window_start,
                window_end=window_end,
                coverage_seconds=coverage_seconds,
                coverage_complete=False,
                status=status,
                reason=reason,
                acceleration=self._acceleration_snapshot(
                    minutes=minutes,
                    window_start=window_start,
                    recent_quote=None,
                ),
            )

        selected = self._buckets_between(window_start, window_end)
        long_quote = sum((bucket.long_quote for bucket in selected), ZERO)
        short_quote = sum((bucket.short_quote for bucket in selected), ZERO)
        total_quote = long_quote + short_quote
        event_count = sum(bucket.event_count for bucket in selected)
        largest_long = max((bucket.largest_long for bucket in selected), default=ZERO)
        largest_short = max((bucket.largest_short for bucket in selected), default=ZERO)
        largest = max(largest_long, largest_short)
        imbalance = (short_quote - long_quote) / total_quote if total_quote > 0 else None
        return LiquidationWindowSnapshot(
            window_minutes=minutes,
            window_start=window_start,
            window_end=window_end,
            coverage_seconds=coverage_seconds,
            coverage_complete=True,
            status=status,
            reason=reason,
            long_liquidation_quote=_money(long_quote),
            short_liquidation_quote=_money(short_quote),
            total_liquidation_quote=_money(total_quote),
            event_count=event_count,
            long_event_count=sum(bucket.long_event_count for bucket in selected),
            short_event_count=sum(bucket.short_event_count for bucket in selected),
            largest_long_liquidation=_money(largest_long) if largest_long > 0 else None,
            largest_short_liquidation=_money(largest_short) if largest_short > 0 else None,
            liquidation_imbalance=_ratio(imbalance),
            liquidation_quote_per_minute=_rate(total_quote / Decimal(minutes)),
            liquidation_event_count_per_minute=_rate(
                Decimal(event_count) / Decimal(minutes)
            ),
            largest_event_share_of_total=_ratio(largest / total_quote)
            if total_quote > 0
            else None,
            acceleration=self._acceleration_snapshot(
                minutes=minutes,
                window_start=window_start,
                recent_quote=total_quote,
            ),
        )

    def _acceleration_snapshot(
        self,
        *,
        minutes: int,
        window_start: datetime,
        recent_quote: Decimal | None,
    ) -> LiquidationAccelerationSnapshot | None:
        prior_minutes = {1: 4, 5: 10}.get(minutes)
        if prior_minutes is None:
            return None
        prior_start = window_start - timedelta(minutes=prior_minutes)
        history_complete = bool(
            self._connected
            and self._coverage_start is not None
            and self._coverage_start <= prior_start
        )
        if recent_quote is None or not history_complete:
            return LiquidationAccelerationSnapshot(
                status=LiquidationAcceleration.INSUFFICIENT_DATA,
                recent_minutes=minutes,
                prior_minutes=prior_minutes,
                reason="insufficient_prior_window_coverage",
            )
        prior_quote = sum(
            (bucket.total_quote for bucket in self._buckets_between(prior_start, window_start)),
            ZERO,
        )
        recent_rate = recent_quote / Decimal(minutes)
        prior_rate = prior_quote / Decimal(prior_minutes)
        if recent_rate > prior_rate:
            label = LiquidationAcceleration.INCREASING
        elif recent_rate < prior_rate:
            label = LiquidationAcceleration.DECREASING
        else:
            label = LiquidationAcceleration.STABLE
        ratio = recent_rate / prior_rate if prior_rate > 0 else None
        reason = "prior_rate_zero" if prior_rate == 0 and recent_rate > 0 else None
        return LiquidationAccelerationSnapshot(
            status=label,
            recent_minutes=minutes,
            prior_minutes=prior_minutes,
            recent_quote_per_minute=_rate(recent_rate),
            prior_quote_per_minute=_rate(prior_rate),
            recent_vs_prior_ratio=_ratio(ratio),
            reason=reason,
        )

    def _coverage_seconds(self, window_start: datetime, window_end: datetime) -> int:
        if self._coverage_start is None:
            return 0
        covered_start = max(window_start, self._coverage_start)
        return max(
            0,
            min(
                int((window_end - covered_start).total_seconds()),
                int((window_end - window_start).total_seconds()),
            ),
        )

    def _buckets_between(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> list[MinuteLiquidationBucket]:
        return [
            self._buckets[start]
            for start in sorted(self._buckets)
            if window_start <= start < window_end
        ]

    def _remember_fingerprint(self, fingerprint: bytes) -> None:
        if len(self._dedupe_order) >= self.max_dedupe_fingerprints:
            expired = self._dedupe_order.popleft()
            self._dedupe_seen.discard(expired)
        self._dedupe_order.append(fingerprint)
        self._dedupe_seen.add(fingerprint)

    def _prune_buckets(self) -> None:
        if len(self._buckets) <= self.max_retained_buckets:
            return
        for bucket_start in sorted(self._buckets)[: -self.max_retained_buckets]:
            del self._buckets[bucket_start]


def _event_fingerprint(event: BinanceLiquidationOrder) -> bytes:
    canonical = "\x1f".join(
        (
            str(event.stream_type),
            event.symbol,
            event.pair_symbol,
            event.order_side,
            event.order_type,
            event.time_in_force,
            str(event.original_quantity),
            str(event.order_price),
            str(event.average_price),
            event.order_status,
            str(event.last_filled_quantity),
            str(event.accumulated_filled_quantity),
            event.event_time.isoformat(),
            event.trade_time.isoformat(),
        )
    )
    return blake2b(canonical.encode("utf-8"), digest_size=16).digest()


def _minute_start(value: datetime) -> datetime:
    normalized = normalize_utc_timestamp(value, field_name="liquidation_trade_time")
    return normalized.astimezone(UTC).replace(second=0, microsecond=0)


def _money(value: Decimal | None) -> Decimal | None:
    return value.quantize(MONEY_QUANT) if value is not None else None


def _ratio(value: Decimal | None) -> Decimal | None:
    return value.quantize(RATIO_QUANT) if value is not None else None


def _rate(value: Decimal | None) -> Decimal | None:
    return value.quantize(RATE_QUANT) if value is not None else None


def _liquidation_summary(
    windows: dict[str, LiquidationWindowSnapshot],
    *,
    status: ContextStatus,
    reason: str | None,
) -> str:
    canonical = windows["15m"]
    if status != ContextStatus.VERIFIED or canonical.total_liquidation_quote is None:
        return f"Liquidation flow unavailable: {reason or canonical.reason or 'unknown_reason'}."
    one_minute = windows["1m"]
    five_minute = windows["5m"]
    imbalance = (
        f"{canonical.liquidation_imbalance:+.4f}"
        if canonical.liquidation_imbalance is not None
        else "N/A"
    )
    acceleration = (
        five_minute.acceleration.status.value
        if five_minute.acceleration is not None
        else LiquidationAcceleration.INSUFFICIENT_DATA.value
    )
    return (
        f"15m observed liquidations: long {_compact(canonical.long_liquidation_quote)} USDT; "
        f"short {_compact(canonical.short_liquidation_quote)} USDT; imbalance {imbalance}; "
        f"1m rate {_compact(one_minute.liquidation_quote_per_minute)} USDT/min; "
        f"5m activity {acceleration}."
    )


def _compact(value: Decimal | None) -> str:
    if value is None:
        return "N/A"
    absolute = abs(value)
    divisor = Decimal("1")
    suffix = ""
    if absolute >= Decimal("1000000000"):
        divisor, suffix = Decimal("1000000000"), "B"
    elif absolute >= Decimal("1000000"):
        divisor, suffix = Decimal("1000000"), "M"
    elif absolute >= Decimal("1000"):
        divisor, suffix = Decimal("1000"), "K"
    return f"{(value / divisor).quantize(Decimal('0.01'))}{suffix}"
