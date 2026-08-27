from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from app.context.models import ContextStatus
from app.data.candle_integrity import normalize_utc_timestamp
from app.microstructure.agg_trade import BinanceAggTrade
from app.microstructure.models import (
    MICROSTRUCTURE_SOURCE,
    FlowWindowSnapshot,
    MicrostructureFlowSnapshot,
    PriceCvdAlignment,
)


FLOW_WINDOWS_MINUTES = (1, 5, 15)
MAX_RETAINED_MINUTE_BUCKETS = max(FLOW_WINDOWS_MINUTES) + 1
ZERO = Decimal("0")
RATIO_QUANT = Decimal("0.00000001")
PERCENT_QUANT = Decimal("0.0001")
MONEY_QUANT = Decimal("0.00000001")


@dataclass(slots=True)
class MinuteFlowBucket:
    bucket_start: datetime
    aggressive_buy_base: Decimal = ZERO
    aggressive_sell_base: Decimal = ZERO
    aggressive_buy_quote: Decimal = ZERO
    aggressive_sell_quote: Decimal = ZERO
    normal_quote_notional: Decimal = ZERO
    normal_quantity_missing_events: int = 0
    aggregate_event_count: int = 0
    underlying_trade_count: int = 0
    first_aggregate_trade_id: int | None = None
    last_aggregate_trade_id: int | None = None
    first_trade_time: datetime | None = None
    last_trade_time: datetime | None = None
    first_price: Decimal | None = None
    last_price: Decimal | None = None
    rpi_event_count: int = 0

    @property
    def delta_base(self) -> Decimal:
        return self.aggressive_buy_base - self.aggressive_sell_base

    @property
    def delta_quote(self) -> Decimal:
        return self.aggressive_buy_quote - self.aggressive_sell_quote

    @property
    def total_quote(self) -> Decimal:
        return self.aggressive_buy_quote + self.aggressive_sell_quote

    def apply(self, event: BinanceAggTrade) -> None:
        quote_notional = event.quote_notional
        if event.aggressive_side == "BUY":
            self.aggressive_buy_base += event.quantity
            self.aggressive_buy_quote += quote_notional
        else:
            self.aggressive_sell_base += event.quantity
            self.aggressive_sell_quote += quote_notional
        if event.normal_quote_notional is None:
            self.normal_quantity_missing_events += 1
        else:
            self.normal_quote_notional += event.normal_quote_notional
            if event.normal_quantity != event.quantity:
                self.rpi_event_count += 1
        self.aggregate_event_count += 1
        self.underlying_trade_count += event.underlying_trade_count
        if self.first_aggregate_trade_id is None:
            self.first_aggregate_trade_id = event.aggregate_trade_id
            self.first_trade_time = event.trade_time
            self.first_price = event.price
        self.last_aggregate_trade_id = event.aggregate_trade_id
        self.last_trade_time = event.trade_time
        self.last_price = event.price


class SymbolFlowAggregator:
    """Incrementally aggregates one symbol without retaining raw trades."""

    def __init__(
        self,
        symbol: str,
        *,
        stale_after_seconds: float,
        max_retained_buckets: int = MAX_RETAINED_MINUTE_BUCKETS,
    ) -> None:
        normalized_symbol = str(symbol).strip().upper()
        if not normalized_symbol:
            raise ValueError("flow symbol must not be blank")
        if stale_after_seconds <= 0:
            raise ValueError("flow stale threshold must be greater than zero")
        if max_retained_buckets < MAX_RETAINED_MINUTE_BUCKETS:
            raise ValueError(
                f"max_retained_buckets must be at least {MAX_RETAINED_MINUTE_BUCKETS}"
            )
        self.symbol = normalized_symbol
        self.stale_after_seconds = float(stale_after_seconds)
        self.max_retained_buckets = int(max_retained_buckets)
        self._buckets: dict[datetime, MinuteFlowBucket] = {}
        self._connected = False
        self._coverage_start: datetime | None = None
        self._coverage_reason = "insufficient_window_coverage"
        self._last_aggregate_trade_id: int | None = None
        self._last_trade_time: datetime | None = None
        self._last_event_time: datetime | None = None
        self._accepted_event_count = 0
        self._duplicate_event_count = 0
        self._out_of_order_event_count = 0
        self._gap_count = 0
        self._reconnect_count = 0
        self._rpi_event_count = 0

    @property
    def retained_bucket_count(self) -> int:
        return len(self._buckets)

    def mark_connected(self, at: datetime, *, reconnect: bool = False) -> None:
        connected_at = normalize_utc_timestamp(at, field_name="microstructure_connected_at")
        if reconnect:
            self._reconnect_count += 1
            self._coverage_reason = "connection_reconnect_in_window"
        elif self._coverage_start is None:
            self._coverage_reason = "insufficient_window_coverage"
        self._coverage_start = connected_at
        self._connected = True

    def mark_disconnected(self, at: datetime) -> None:
        normalize_utc_timestamp(at, field_name="microstructure_disconnected_at")
        if self._connected:
            self._gap_count += 1
        self._connected = False
        self._coverage_start = None
        self._coverage_reason = "connection_gap_in_window"

    def ingest(self, event: BinanceAggTrade) -> bool:
        if event.symbol != self.symbol:
            raise ValueError(
                f"aggTrade symbol {event.symbol} does not match aggregator {self.symbol}"
            )
        previous_id = self._last_aggregate_trade_id
        if previous_id is not None and event.aggregate_trade_id <= previous_id:
            if event.aggregate_trade_id == previous_id:
                self._duplicate_event_count += 1
            else:
                self._out_of_order_event_count += 1
            return False
        if self._last_trade_time is not None and event.trade_time < self._last_trade_time:
            self._out_of_order_event_count += 1
            self._mark_uncertain_coverage(
                self._last_trade_time,
                reason="trade_time_regression_in_window",
            )
            return False
        if previous_id is not None and event.aggregate_trade_id > previous_id + 1:
            self._gap_count += 1
            self._mark_uncertain_coverage(
                event.trade_time,
                reason="aggregate_trade_id_gap_in_window",
            )

        bucket_start = _minute_start(event.trade_time)
        bucket = self._buckets.get(bucket_start)
        if bucket is None:
            bucket = MinuteFlowBucket(bucket_start=bucket_start)
            self._buckets[bucket_start] = bucket
        bucket.apply(event)
        self._last_aggregate_trade_id = event.aggregate_trade_id
        self._last_trade_time = event.trade_time
        self._last_event_time = event.event_time
        self._accepted_event_count += 1
        if event.normal_quantity is not None and event.normal_quantity != event.quantity:
            self._rpi_event_count += 1
        self._prune_buckets()
        return True

    def snapshot(self, *, as_of: datetime) -> MicrostructureFlowSnapshot:
        now = normalize_utc_timestamp(as_of, field_name="microstructure_snapshot_at")
        windows = {
            f"{minutes}m": self._window_snapshot(minutes=minutes, as_of=now)
            for minutes in FLOW_WINDOWS_MINUTES
        }
        age_seconds = (
            max((now - self._last_trade_time).total_seconds(), 0.0)
            if self._last_trade_time is not None
            else None
        )
        status, reason = self._snapshot_status(windows, age_seconds=age_seconds)
        return MicrostructureFlowSnapshot(
            symbol=self.symbol,
            source=MICROSTRUCTURE_SOURCE.format(symbol=self.symbol.lower()),
            observed_at=self._last_trade_time,
            age_seconds=age_seconds,
            status=status,
            reason=reason,
            windows=windows,
            orderflow_summary=_orderflow_summary(windows["15m"], status=status, reason=reason),
            retained_bucket_count=len(self._buckets),
            max_retained_bucket_count=self.max_retained_buckets,
            last_aggregate_trade_id=self._last_aggregate_trade_id,
            accepted_event_count=self._accepted_event_count,
            duplicate_event_count=self._duplicate_event_count,
            out_of_order_event_count=self._out_of_order_event_count,
            gap_count=self._gap_count,
            reconnect_count=self._reconnect_count,
            rpi_event_count=self._rpi_event_count,
        )

    def _mark_uncertain_coverage(self, at: datetime, *, reason: str) -> None:
        normalized = normalize_utc_timestamp(at, field_name="microstructure_gap_at")
        self._coverage_start = normalized
        self._coverage_reason = reason

    def _prune_buckets(self) -> None:
        if len(self._buckets) <= self.max_retained_buckets:
            return
        for bucket_start in sorted(self._buckets)[: -self.max_retained_buckets]:
            del self._buckets[bucket_start]

    def _window_snapshot(self, *, minutes: int, as_of: datetime) -> FlowWindowSnapshot:
        window_end = _minute_start(as_of)
        window_start = window_end - timedelta(minutes=minutes)
        coverage_seconds = self._coverage_seconds(window_start, window_end)
        coverage_complete = bool(
            self._connected
            and self._coverage_start is not None
            and self._coverage_start <= window_start
        )
        selected = [
            self._buckets[start]
            for start in sorted(self._buckets)
            if window_start <= start < window_end
        ]
        event_count = sum(bucket.aggregate_event_count for bucket in selected)
        reason: str | None = None
        if not coverage_complete:
            reason = self._coverage_reason
        elif event_count == 0:
            reason = "no_valid_events_in_window"

        if event_count == 0:
            return FlowWindowSnapshot(
                window_minutes=minutes,
                window_start=window_start,
                window_end=window_end,
                coverage_seconds=coverage_seconds,
                coverage_complete=coverage_complete,
                reason=reason,
            )

        buy_base = sum((bucket.aggressive_buy_base for bucket in selected), ZERO)
        sell_base = sum((bucket.aggressive_sell_base for bucket in selected), ZERO)
        buy_quote = sum((bucket.aggressive_buy_quote for bucket in selected), ZERO)
        sell_quote = sum((bucket.aggressive_sell_quote for bucket in selected), ZERO)
        delta_base = buy_base - sell_base
        delta_quote = buy_quote - sell_quote
        total_quote = buy_quote + sell_quote
        flow_imbalance = delta_quote / total_quote if total_quote > 0 else None
        buyer_aggression = buy_quote / total_quote * Decimal("100") if total_quote > 0 else None
        normal_complete = all(bucket.normal_quantity_missing_events == 0 for bucket in selected)
        normal_quote = (
            sum((bucket.normal_quote_notional for bucket in selected), ZERO)
            if normal_complete
            else None
        )
        rpi_quote = total_quote - normal_quote if normal_quote is not None else None
        first_bucket = selected[0]
        last_bucket = selected[-1]
        price_return = _price_return_pct(first_bucket.first_price, last_bucket.last_price)
        minute_deltas = [
            self._buckets[start].delta_quote if start in self._buckets else ZERO
            for start in _minute_range(window_start, minutes)
        ]
        rolling_cvd = sum(minute_deltas, ZERO)
        cvd_slope = _cumulative_path_slope(minute_deltas)
        alignment = classify_price_cvd_alignment(price_return, rolling_cvd)
        return FlowWindowSnapshot(
            window_minutes=minutes,
            window_start=window_start,
            window_end=window_end,
            coverage_seconds=coverage_seconds,
            coverage_complete=coverage_complete,
            reason=reason,
            aggressive_buy_base=_money(buy_base),
            aggressive_sell_base=_money(sell_base),
            aggressive_buy_quote=_money(buy_quote),
            aggressive_sell_quote=_money(sell_quote),
            delta_base=_money(delta_base),
            delta_quote=_money(delta_quote),
            total_quote=_money(total_quote),
            flow_imbalance_ratio=_ratio(flow_imbalance),
            buyer_aggression_pct=_percent(buyer_aggression),
            rolling_cvd_quote=_money(rolling_cvd),
            cvd_slope_quote_per_min=_money(cvd_slope),
            price_return_pct=_percent(price_return),
            price_cvd_alignment=alignment,
            normal_quote_notional=_money(normal_quote),
            rpi_quote_notional=_money(rpi_quote),
            aggregate_event_count=event_count,
            underlying_trade_count=sum(bucket.underlying_trade_count for bucket in selected),
        )

    def _coverage_seconds(self, window_start: datetime, window_end: datetime) -> int:
        if self._coverage_start is None:
            return 0
        covered_start = max(window_start, self._coverage_start)
        return max(0, min(int((window_end - covered_start).total_seconds()), int((window_end - window_start).total_seconds())))

    def _snapshot_status(
        self,
        windows: dict[str, FlowWindowSnapshot],
        *,
        age_seconds: float | None,
    ) -> tuple[ContextStatus, str | None]:
        if not self._connected:
            return ContextStatus.UNAVAILABLE, "stream_disconnected"
        if self._last_trade_time is None:
            return ContextStatus.UNAVAILABLE, "no_valid_events"
        if age_seconds is not None and age_seconds > self.stale_after_seconds:
            return ContextStatus.STALE, "last_valid_event_stale"
        canonical = windows["15m"]
        if not canonical.coverage_complete:
            return ContextStatus.UNAVAILABLE, canonical.reason or "insufficient_window_coverage"
        if canonical.aggregate_event_count == 0:
            return ContextStatus.UNAVAILABLE, "no_valid_events_in_window"
        return ContextStatus.VERIFIED, None


def classify_price_cvd_alignment(
    price_return_pct: Decimal | None,
    rolling_cvd_quote: Decimal | None,
) -> PriceCvdAlignment | None:
    if price_return_pct is None or rolling_cvd_quote is None:
        return None
    if price_return_pct > 0 and rolling_cvd_quote > 0:
        return PriceCvdAlignment.ALIGNED_UP
    if price_return_pct < 0 and rolling_cvd_quote < 0:
        return PriceCvdAlignment.ALIGNED_DOWN
    if price_return_pct > 0 and rolling_cvd_quote < 0:
        return PriceCvdAlignment.PRICE_UP_CVD_DOWN
    if price_return_pct < 0 and rolling_cvd_quote > 0:
        return PriceCvdAlignment.PRICE_DOWN_CVD_UP
    return PriceCvdAlignment.MIXED_FLAT


def _cumulative_path_slope(minute_deltas: list[Decimal]) -> Decimal:
    cumulative = ZERO
    path = [ZERO]
    for delta in minute_deltas:
        cumulative += delta
        path.append(cumulative)
    count = Decimal(len(path))
    xs = [Decimal(index) for index in range(len(path))]
    mean_x = sum(xs, ZERO) / count
    mean_y = sum(path, ZERO) / count
    numerator = sum(
        ((x - mean_x) * (y - mean_y) for x, y in zip(xs, path, strict=True)),
        ZERO,
    )
    denominator = sum(((x - mean_x) ** 2 for x in xs), ZERO)
    return numerator / denominator if denominator > 0 else ZERO


def _price_return_pct(first_price: Decimal | None, last_price: Decimal | None) -> Decimal | None:
    if first_price is None or last_price is None or first_price <= 0:
        return None
    return (last_price - first_price) / first_price * Decimal("100")


def _minute_start(value: datetime) -> datetime:
    normalized = normalize_utc_timestamp(value, field_name="microstructure_trade_time")
    return normalized.astimezone(UTC).replace(second=0, microsecond=0)


def _minute_range(start: datetime, count: int) -> tuple[datetime, ...]:
    return tuple(start + timedelta(minutes=index) for index in range(count))


def _money(value: Decimal | None) -> Decimal | None:
    return value.quantize(MONEY_QUANT) if value is not None else None


def _ratio(value: Decimal | None) -> Decimal | None:
    return value.quantize(RATIO_QUANT) if value is not None else None


def _percent(value: Decimal | None) -> Decimal | None:
    return value.quantize(PERCENT_QUANT) if value is not None else None


def _orderflow_summary(
    window: FlowWindowSnapshot,
    *,
    status: ContextStatus,
    reason: str | None,
) -> str:
    if status != ContextStatus.VERIFIED or window.delta_quote is None:
        return f"Orderflow unavailable: {reason or window.reason or 'unknown_reason'}."
    slope = window.cvd_slope_quote_per_min
    slope_label = "flat"
    if slope is not None and slope > 0:
        slope_label = "positive"
    elif slope is not None and slope < 0:
        slope_label = "negative"
    aggression = (
        f"{window.buyer_aggression_pct.quantize(Decimal('0.1'))}%"
        if window.buyer_aggression_pct is not None
        else "N/A"
    )
    alignment = window.price_cvd_alignment.value if window.price_cvd_alignment is not None else "N/A"
    return (
        f"15m delta {_signed_compact(window.delta_quote)} USDT; "
        f"buyer aggression {aggression}; CVD slope {slope_label}; "
        f"price/CVD {alignment}."
    )


def _signed_compact(value: Decimal) -> str:
    absolute = abs(value)
    divisor = Decimal("1")
    suffix = ""
    if absolute >= Decimal("1000000000"):
        divisor, suffix = Decimal("1000000000"), "B"
    elif absolute >= Decimal("1000000"):
        divisor, suffix = Decimal("1000000"), "M"
    elif absolute >= Decimal("1000"):
        divisor, suffix = Decimal("1000"), "K"
    scaled = (absolute / divisor).quantize(Decimal("0.01"))
    sign = "+" if value >= 0 else "-"
    return f"{sign}{scaled}{suffix}"
