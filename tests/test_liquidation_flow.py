from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.context.models import ContextStatus
from app.microstructure import (
    BINANCE_ALL_MARKET_LIQUIDATION_STREAM,
    MAX_RETAINED_LIQUIDATION_BUCKETS,
    LiquidatedPositionSide,
    LiquidationAcceleration,
    LiquidationFlowService,
    LiquidationPayloadError,
    SymbolLiquidationAggregator,
    WrongLiquidationContractTypeError,
    parse_binance_liquidation,
)


BASE = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _payload(
    trade_time: datetime,
    *,
    symbol: str = "BTCUSDT",
    side: str = "SELL",
    original_quantity: str = "1",
    order_price: str = "99",
    average_price: str = "100",
    last_filled_quantity: str | None = None,
    accumulated_filled_quantity: str | None = None,
    event_time: datetime | None = None,
    stream_type: int | None = 1,
    pair_symbol: str | None = None,
) -> dict[str, Any]:
    filled = accumulated_filled_quantity or original_quantity
    payload: dict[str, Any] = {
        "e": "forceOrder",
        "E": _milliseconds(event_time or trade_time),
        "o": {
            "s": symbol,
            "S": side,
            "o": "LIMIT",
            "f": "IOC",
            "q": original_quantity,
            "p": order_price,
            "ap": average_price,
            "X": "FILLED",
            "l": last_filled_quantity or filled,
            "z": filled,
            "T": _milliseconds(trade_time),
        },
        "ps": pair_symbol or symbol,
    }
    if stream_type is not None:
        payload["st"] = stream_type
    return payload


def _event(trade_time: datetime, **overrides: Any):
    return parse_binance_liquidation(_payload(trade_time, **overrides))


def _aggregator(
    *,
    connected_at: datetime = BASE,
    stale_after_seconds: float = 120.0,
    max_dedupe_fingerprints: int = 128,
) -> SymbolLiquidationAggregator:
    aggregator = SymbolLiquidationAggregator(
        "BTCUSDT",
        stale_after_seconds=stale_after_seconds,
        max_dedupe_fingerprints=max_dedupe_fingerprints,
    )
    aggregator.mark_connected(connected_at)
    return aggregator


def _ingest(
    aggregator: SymbolLiquidationAggregator,
    trade_time: datetime,
    *,
    received_at: datetime | None = None,
    **overrides: Any,
) -> bool:
    return aggregator.ingest(
        _event(trade_time, **overrides),
        received_at=received_at or trade_time,
    )


def _fifteen_minute_replay(
    *,
    side: str,
    paired_balanced: bool = False,
) -> tuple[SymbolLiquidationAggregator, Any]:
    aggregator = _aggregator()
    for minute in range(15):
        quantity = str(minute + 1)
        at = BASE + timedelta(minutes=minute, seconds=30)
        _ingest(
            aggregator,
            at,
            side=side,
            original_quantity=quantity,
            average_price="100",
        )
        if paired_balanced:
            _ingest(
                aggregator,
                at + timedelta(seconds=1),
                side="BUY" if side == "SELL" else "SELL",
                original_quantity=quantity,
                average_price="100",
            )
    return aggregator, aggregator.snapshot(as_of=BASE + timedelta(minutes=15))


def test_current_binance_payload_contract_is_parsed_from_combined_stream() -> None:
    raw = {
        "stream": "!forceOrder@arr",
        "data": _payload(
            BASE,
            original_quantity="0.014",
            order_price="9910",
            average_price="9908.5",
        ),
    }

    event = parse_binance_liquidation(json.dumps(raw))

    assert event.symbol == "BTCUSDT"
    assert event.pair_symbol == "BTCUSDT"
    assert event.order_side == "SELL"
    assert event.order_type == "LIMIT"
    assert event.time_in_force == "IOC"
    assert event.original_quantity == Decimal("0.014")
    assert event.order_price == Decimal("9910")
    assert event.average_price == Decimal("9908.5")
    assert event.order_status == "FILLED"
    assert event.last_filled_quantity == Decimal("0.014")
    assert event.accumulated_filled_quantity == Decimal("0.014")
    assert event.event_time == BASE
    assert event.trade_time == BASE
    assert event.stream_type == 1


@pytest.mark.parametrize(
    ("order_side", "expected_position_side"),
    [
        ("SELL", LiquidatedPositionSide.LONG),
        ("BUY", LiquidatedPositionSide.SHORT),
    ],
)
def test_forced_order_side_maps_to_liquidated_position_side(
    order_side: str,
    expected_position_side: LiquidatedPositionSide,
) -> None:
    event = _event(BASE, side=order_side)

    assert event.order_side == order_side
    assert event.liquidated_position_side == expected_position_side


def test_notional_uses_accumulated_execution_quantity_times_average_price() -> None:
    event = _event(
        BASE,
        original_quantity="9",
        order_price="900",
        average_price="123.45",
        accumulated_filled_quantity="2",
        last_filled_quantity="0.5",
    )

    assert event.quote_notional == Decimal("246.90")


@pytest.mark.parametrize("average_price", [None, "", "0", "NaN"])
def test_missing_or_unusable_execution_price_never_contributes_zero(
    average_price: str | None,
) -> None:
    payload = _payload(BASE)
    if average_price is None:
        payload["o"].pop("ap")
    else:
        payload["o"]["ap"] = average_price

    with pytest.raises(LiquidationPayloadError):
        parse_binance_liquidation(payload)


def test_non_usdm_and_undiscriminated_all_market_events_are_rejected() -> None:
    with pytest.raises(WrongLiquidationContractTypeError, match="COIN-M"):
        parse_binance_liquidation(_payload(BASE, stream_type=2))
    with pytest.raises(LiquidationPayloadError, match="st"):
        parse_binance_liquidation(_payload(BASE, stream_type=None))


def test_exact_one_five_and_fifteen_minute_windows_and_metrics() -> None:
    aggregator = _aggregator()
    _ingest(
        aggregator,
        BASE + timedelta(seconds=59),
        side="SELL",
        original_quantity="1",
    )
    _ingest(
        aggregator,
        BASE + timedelta(minutes=10, seconds=59),
        side="BUY",
        original_quantity="2",
    )
    _ingest(
        aggregator,
        BASE + timedelta(minutes=14, seconds=59),
        side="SELL",
        original_quantity="3",
    )

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))
    one = snapshot.windows["1m"]
    five = snapshot.windows["5m"]
    fifteen = snapshot.windows["15m"]

    assert snapshot.status == ContextStatus.VERIFIED
    assert one.window_start == BASE + timedelta(minutes=14)
    assert one.long_liquidation_quote == Decimal("300.00000000")
    assert one.short_liquidation_quote == Decimal("0E-8")
    assert one.total_liquidation_quote == Decimal("300.00000000")
    assert one.event_count == one.long_event_count == 1
    assert one.short_event_count == 0
    assert one.largest_long_liquidation == Decimal("300.00000000")
    assert one.largest_short_liquidation is None
    assert one.liquidation_imbalance == Decimal("-1.00000000")
    assert one.liquidation_quote_per_minute == Decimal("300.00000000")
    assert one.liquidation_event_count_per_minute == Decimal("1.00000000")
    assert one.largest_event_share_of_total == Decimal("1.00000000")

    assert five.long_liquidation_quote == Decimal("300.00000000")
    assert five.short_liquidation_quote == Decimal("200.00000000")
    assert five.total_liquidation_quote == Decimal("500.00000000")
    assert five.event_count == 2
    assert five.largest_long_liquidation == Decimal("300.00000000")
    assert five.largest_short_liquidation == Decimal("200.00000000")
    assert five.liquidation_imbalance == Decimal("-0.20000000")
    assert five.liquidation_quote_per_minute == Decimal("100.00000000")
    assert five.liquidation_event_count_per_minute == Decimal("0.40000000")
    assert five.largest_event_share_of_total == Decimal("0.60000000")

    assert fifteen.long_liquidation_quote == Decimal("400.00000000")
    assert fifteen.short_liquidation_quote == Decimal("200.00000000")
    assert fifteen.total_liquidation_quote == Decimal("600.00000000")
    assert fifteen.event_count == 3
    assert fifteen.long_event_count == 2
    assert fifteen.short_event_count == 1
    assert fifteen.liquidation_imbalance == Decimal("-0.33333333")


def test_order_trade_time_not_event_or_receipt_time_selects_utc_bucket() -> None:
    aggregator = _aggregator(stale_after_seconds=3600)
    trade_time = BASE + timedelta(minutes=14, seconds=59)
    event = _event(
        trade_time,
        event_time=BASE + timedelta(minutes=16),
        original_quantity="2",
    )

    accepted = aggregator.ingest(
        event,
        received_at=BASE + timedelta(minutes=15),
    )
    one = aggregator.snapshot(as_of=BASE + timedelta(minutes=15)).windows["1m"]

    assert accepted is True
    assert one.event_count == 1
    assert one.total_liquidation_quote == Decimal("200.00000000")


def test_connected_silent_market_is_verified_zero_after_full_coverage() -> None:
    snapshot = _aggregator().snapshot(as_of=BASE + timedelta(minutes=15))

    assert snapshot.status == ContextStatus.VERIFIED
    for key, minutes in (("1m", 1), ("5m", 5), ("15m", 15)):
        window = snapshot.windows[key]
        assert window.status == ContextStatus.VERIFIED
        assert window.coverage_complete is True
        assert window.coverage_seconds == minutes * 60
        assert window.long_liquidation_quote == Decimal("0E-8")
        assert window.short_liquidation_quote == Decimal("0E-8")
        assert window.total_liquidation_quote == Decimal("0E-8")
        assert window.event_count == 0
        assert window.liquidation_imbalance is None


def test_disconnected_window_is_not_verified_zero() -> None:
    aggregator = _aggregator()
    aggregator.mark_disconnected(BASE + timedelta(minutes=14))

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))

    assert snapshot.status == ContextStatus.UNAVAILABLE
    assert snapshot.reason == "stream_disconnected"
    assert snapshot.windows["15m"].total_liquidation_quote is None
    assert snapshot.windows["15m"].event_count is None


def test_warmup_validates_each_window_only_after_complete_coverage() -> None:
    aggregator = _aggregator(connected_at=BASE + timedelta(minutes=10))

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))

    assert snapshot.status == ContextStatus.UNAVAILABLE
    assert snapshot.reason == "insufficient_window_coverage"
    assert snapshot.windows["1m"].status == ContextStatus.VERIFIED
    assert snapshot.windows["5m"].status == ContextStatus.VERIFIED
    assert snapshot.windows["15m"].status == ContextStatus.UNAVAILABLE
    assert snapshot.windows["15m"].coverage_seconds == 300
    assert snapshot.windows["15m"].total_liquidation_quote is None


def test_delayed_event_marks_coverage_stale_instead_of_fabricating_zero() -> None:
    aggregator = _aggregator(stale_after_seconds=5)

    accepted = _ingest(
        aggregator,
        BASE + timedelta(minutes=14, seconds=50),
        received_at=BASE + timedelta(minutes=15),
    )
    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))

    assert accepted is False
    assert snapshot.status == ContextStatus.STALE
    assert snapshot.reason == "stale_event_lag"
    assert snapshot.stale_event_count == 1
    assert snapshot.accepted_event_count == 0
    assert snapshot.windows["15m"].total_liquidation_quote is None


def test_reconnect_resets_coverage_and_exact_duplicate_is_not_double_counted() -> None:
    aggregator = _aggregator(stale_after_seconds=5)
    event_time = BASE + timedelta(minutes=4, seconds=59)
    event = _event(event_time, original_quantity="2")
    assert aggregator.ingest(event, received_at=event_time) is True
    aggregator.mark_disconnected(BASE + timedelta(minutes=5))
    aggregator.mark_connected(BASE + timedelta(minutes=5, seconds=1), reconnect=True)

    assert aggregator.ingest(event, received_at=BASE + timedelta(minutes=15)) is False
    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))

    assert snapshot.status == ContextStatus.UNAVAILABLE
    assert snapshot.reason == "connection_reconnect_in_window"
    assert snapshot.accepted_event_count == 1
    assert snapshot.duplicate_event_count == 1
    assert snapshot.stale_event_count == 0
    assert snapshot.reconnect_count == 1
    assert snapshot.disconnect_count == 1


def test_acceleration_compares_recent_rates_to_complete_prior_baselines() -> None:
    aggregator = _aggregator()
    for minute in range(15):
        quantity = "5" if minute == 14 else "1"
        _ingest(
            aggregator,
            BASE + timedelta(minutes=minute, seconds=30),
            original_quantity=quantity,
        )

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))
    one = snapshot.windows["1m"].acceleration
    five = snapshot.windows["5m"].acceleration

    assert one is not None
    assert one.status == LiquidationAcceleration.INCREASING
    assert one.recent_quote_per_minute == Decimal("500.00000000")
    assert one.prior_quote_per_minute == Decimal("100.00000000")
    assert one.recent_vs_prior_ratio == Decimal("5.00000000")
    assert five is not None
    assert five.status == LiquidationAcceleration.INCREASING
    assert five.recent_quote_per_minute == Decimal("180.00000000")
    assert five.prior_quote_per_minute == Decimal("100.00000000")
    assert five.recent_vs_prior_ratio == Decimal("1.80000000")


def test_acceleration_is_insufficient_without_prior_coverage() -> None:
    aggregator = _aggregator(connected_at=BASE + timedelta(minutes=10))
    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))

    one = snapshot.windows["1m"].acceleration
    five = snapshot.windows["5m"].acceleration
    assert one is not None and one.status == LiquidationAcceleration.STABLE
    assert five is not None and five.status == LiquidationAcceleration.INSUFFICIENT_DATA


def test_production_like_long_short_balanced_and_burst_scenarios_are_exact() -> None:
    _long_state, long_cascade = _fifteen_minute_replay(side="SELL")
    _short_state, short_cascade = _fifteen_minute_replay(side="BUY")
    _balanced_state, balanced = _fifteen_minute_replay(
        side="SELL",
        paired_balanced=True,
    )

    long_window = long_cascade.windows["15m"]
    short_window = short_cascade.windows["15m"]
    balanced_window = balanced.windows["15m"]
    assert long_window.long_liquidation_quote == Decimal("12000.00000000")
    assert long_window.short_liquidation_quote == Decimal("0E-8")
    assert long_window.event_count == 15
    assert long_window.liquidation_imbalance == Decimal("-1.00000000")
    assert short_window.short_liquidation_quote == Decimal("12000.00000000")
    assert short_window.long_liquidation_quote == Decimal("0E-8")
    assert short_window.event_count == 15
    assert short_window.liquidation_imbalance == Decimal("1.00000000")
    assert balanced_window.long_liquidation_quote == Decimal("12000.00000000")
    assert balanced_window.short_liquidation_quote == Decimal("12000.00000000")
    assert balanced_window.total_liquidation_quote == Decimal("24000.00000000")
    assert balanced_window.event_count == 30
    assert balanced_window.liquidation_imbalance == Decimal("0E-8")

    burst = _aggregator()
    for minute in range(15):
        _ingest(
            burst,
            BASE + timedelta(minutes=minute, seconds=30),
            original_quantity="10" if minute == 14 else "1",
        )
    burst_snapshot = burst.snapshot(as_of=BASE + timedelta(minutes=15))
    assert burst_snapshot.windows["1m"].total_liquidation_quote == Decimal("1000.00000000")
    assert (
        burst_snapshot.windows["1m"].acceleration.status
        == LiquidationAcceleration.INCREASING
    )


def test_disconnect_during_cascade_prevents_verified_partial_totals() -> None:
    aggregator = _aggregator()
    for minute in range(7):
        _ingest(
            aggregator,
            BASE + timedelta(minutes=minute, seconds=30),
            original_quantity=str(minute + 1),
        )
    aggregator.mark_disconnected(BASE + timedelta(minutes=7))

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))

    assert snapshot.status == ContextStatus.UNAVAILABLE
    assert snapshot.windows["15m"].total_liquidation_quote is None
    assert snapshot.accepted_event_count == 7


def test_summary_serialization_and_state_are_deterministic_and_bounded() -> None:
    aggregator, first = _fifteen_minute_replay(side="SELL")
    second = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))
    serialized = first.model_dump_json()

    assert first.liquidation_summary == second.liquidation_summary
    assert first.liquidation_summary == (
        "15m observed liquidations: long 12.00K USDT; short 0.00 USDT; "
        "imbalance -1.0000; 1m rate 1.50K USDT/min; 5m activity INCREASING."
    )
    assert len(serialized.encode("utf-8")) < 8 * 1024
    assert '"raw"' not in serialized
    assert "original_quantity" not in serialized
    assert "average_price" not in serialized
    assert "order_side" not in serialized


def test_bucket_and_dedupe_memory_are_strictly_bounded_without_raw_events() -> None:
    aggregator = _aggregator(
        stale_after_seconds=3600,
        max_dedupe_fingerprints=8,
    )
    for minute in range(40):
        _ingest(
            aggregator,
            BASE + timedelta(minutes=minute, seconds=30),
            original_quantity=str(minute + 1),
        )

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=40))

    assert aggregator.retained_bucket_count == MAX_RETAINED_LIQUIDATION_BUCKETS == 16
    assert aggregator.dedupe_fingerprint_count == 8
    assert snapshot.retained_bucket_count == 16
    assert snapshot.dedupe_fingerprint_count == 8
    assert not hasattr(aggregator, "raw_events")
    assert not hasattr(aggregator, "event_history")


class _FakeConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self.incoming: asyncio.Queue[Any] = asyncio.Queue()

    async def recv(self) -> Any:
        value = await self.incoming.get()
        if isinstance(value, BaseException):
            raise value
        return value

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


class _FakeTransport:
    def __init__(self, *connections: _FakeConnection, error: Exception | None = None) -> None:
        self.connections = list(connections)
        self.error = error
        self.calls = 0

    async def connect(self) -> _FakeConnection:
        self.calls += 1
        if self.error is not None:
            raise self.error
        if not self.connections:
            raise RuntimeError("no fake connection available")
        return self.connections.pop(0)


async def _wait_until(predicate: Any) -> None:
    for _ in range(100):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def test_service_uses_one_all_market_subscription_and_reconciles_retention_only() -> None:
    async def scenario() -> None:
        connection = _FakeConnection()
        service = LiquidationFlowService(
            transport=_FakeTransport(connection),
            max_symbols=3,
            clock=lambda: BASE,
        )
        await service.start(["ETHUSDT", "SOLUSDT"])
        await _wait_until(lambda: len(connection.sent) == 1)

        subscribe = json.loads(connection.sent[0])
        assert subscribe["method"] == "SUBSCRIBE"
        assert subscribe["params"] == [BINANCE_ALL_MARKET_LIQUIDATION_STREAM]
        assert service.tracked_symbols == ("BTCUSDT", "ETHUSDT", "SOLUSDT")

        await service.reconcile_symbols(["ADAUSDT"])
        assert service.tracked_symbols == ("ADAUSDT", "BTCUSDT")
        assert len(connection.sent) == 1
        await service.stop()
        assert connection.closed is True

    asyncio.run(scenario())


def test_btc_is_always_retained_and_overflow_is_explicit() -> None:
    async def scenario() -> None:
        service = LiquidationFlowService(
            transport=_FakeTransport(error=RuntimeError("offline fake")),
            max_symbols=1,
            clock=lambda: BASE,
        )
        selected = await service.reconcile_symbols(["ETHUSDT", "SOLUSDT"])

        assert selected == ("BTCUSDT",)
        assert service.snapshot("BTCUSDT").reason == "stream_disconnected"
        overflow = service.snapshot("ETHUSDT")
        assert overflow.status == ContextStatus.UNAVAILABLE
        assert overflow.reason == "retention_limit_exceeded:max_symbols=1"

    asyncio.run(scenario())


def test_malformed_wrong_contract_and_untracked_events_are_quarantined() -> None:
    async def scenario() -> None:
        service = LiquidationFlowService(clock=lambda: BASE)
        await service.reconcile_symbols(["BTCUSDT"])
        service._handle_payload("not-json")
        service._handle_payload(_payload(BASE, average_price="0"))
        service._handle_payload(_payload(BASE, stream_type=2))
        service._handle_payload(_payload(BASE, symbol="ETHUSDT"))
        service._handle_payload(_payload(BASE))

        health = service.health()
        assert health["malformed_event_count"] == 2
        assert health["wrong_contract_event_count"] == 1
        assert health["untracked_symbol_event_count"] == 1
        assert service.snapshot("BTCUSDT").accepted_event_count == 1

    asyncio.run(scenario())


def test_transport_failure_is_isolated_with_exact_error_reason() -> None:
    async def scenario() -> None:
        service = LiquidationFlowService(
            transport=_FakeTransport(error=ConnectionError("synthetic outage")),
            reconnect_base_seconds=60,
            reconnect_max_seconds=60,
            clock=lambda: BASE,
        )
        await service.start(["ETHUSDT"])
        await _wait_until(lambda: service.health()["disconnect_count"] == 1)

        snapshot = service.snapshot("ETHUSDT")
        assert service.running is True
        assert snapshot.status == ContextStatus.ERROR
        assert snapshot.reason == "stream_error:ConnectionError:synthetic outage"
        assert service.health()["last_error"] == "ConnectionError:synthetic outage"
        await service.stop()

    asyncio.run(scenario())
