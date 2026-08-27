from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.context.models import ContextStatus
from app.microstructure import (
    MAX_RETAINED_MINUTE_BUCKETS,
    AggTradePayloadError,
    MicrostructureFlowService,
    PriceCvdAlignment,
    SymbolFlowAggregator,
    WrongContractTypeError,
    classify_price_cvd_alignment,
    parse_binance_agg_trade,
)


BASE = datetime(2026, 8, 27, 12, 0, tzinfo=UTC)


def _milliseconds(value: datetime) -> int:
    return int(value.timestamp() * 1000)


def _payload(
    aggregate_id: int,
    trade_time: datetime,
    *,
    symbol: str = "BTCUSDT",
    price: str = "100",
    quantity: str = "1",
    normal_quantity: str | None = None,
    buyer_is_maker: bool = False,
    stream_type: int | None = 1,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "e": "aggTrade",
        "E": _milliseconds(trade_time),
        "s": symbol,
        "a": aggregate_id,
        "p": price,
        "q": quantity,
        "f": aggregate_id * 2,
        "l": aggregate_id * 2 + 1,
        "T": _milliseconds(trade_time),
        "m": buyer_is_maker,
    }
    if normal_quantity is not None:
        payload["nq"] = normal_quantity
    if stream_type is not None:
        payload["st"] = stream_type
    return payload


def _event(aggregate_id: int, trade_time: datetime, **overrides: Any):
    return parse_binance_agg_trade(_payload(aggregate_id, trade_time, **overrides))


def _aggregator(*, stale_after_seconds: float = 120.0) -> SymbolFlowAggregator:
    aggregator = SymbolFlowAggregator(
        "BTCUSDT",
        stale_after_seconds=stale_after_seconds,
    )
    aggregator.mark_connected(BASE)
    return aggregator


def _fifteen_minute_replay(
    *,
    side: str,
    descending_price: bool = False,
    paired_balanced: bool = False,
) -> tuple[SymbolFlowAggregator, Any]:
    aggregator = _aggregator()
    aggregate_id = 1
    for minute in range(15):
        price = str(115 - minute if descending_price else 100 + minute)
        trade_time = BASE + timedelta(minutes=minute, seconds=59)
        aggregator.ingest(
            _event(
                aggregate_id,
                trade_time,
                price=price,
                buyer_is_maker=side == "sell",
                normal_quantity="1",
            )
        )
        aggregate_id += 1
        if paired_balanced:
            aggregator.ingest(
                _event(
                    aggregate_id,
                    trade_time,
                    price=price,
                    buyer_is_maker=side != "sell",
                    normal_quantity="1",
                )
            )
            aggregate_id += 1
    return aggregator, aggregator.snapshot(as_of=BASE + timedelta(minutes=15))


@pytest.mark.parametrize(
    ("buyer_is_maker", "expected_side"),
    [(False, "BUY"), (True, "SELL")],
)
def test_aggressor_side_semantics_are_never_inverted(
    buyer_is_maker: bool,
    expected_side: str,
) -> None:
    event = _event(1, BASE, buyer_is_maker=buyer_is_maker)

    assert event.aggressive_side == expected_side


def test_quote_notional_is_decimal_price_times_total_quantity() -> None:
    event = _event(1, BASE, price="123.45", quantity="0.125", normal_quantity="0.100")

    assert event.quote_notional == Decimal("15.43125")
    assert event.normal_quote_notional == Decimal("12.34500")


def test_stream_type_is_strict_with_explicit_legacy_compatibility() -> None:
    with pytest.raises(WrongContractTypeError, match="COIN-M"):
        parse_binance_agg_trade(_payload(1, BASE, stream_type=2))
    with pytest.raises(AggTradePayloadError, match="missing required UM stream type"):
        parse_binance_agg_trade(_payload(1, BASE, stream_type=None))

    legacy = parse_binance_agg_trade(
        _payload(1, BASE, stream_type=None),
        allow_legacy_missing_stream_type=True,
    )
    assert legacy.stream_type is None


def test_rpi_q_and_nq_are_diagnostics_not_double_counted() -> None:
    aggregator = _aggregator()
    aggregator.ingest(
        _event(
            1,
            BASE + timedelta(minutes=14, seconds=59),
            price="100",
            quantity="2",
            normal_quantity="1.5",
        )
    )

    window = aggregator.snapshot(as_of=BASE + timedelta(minutes=15)).windows["15m"]

    assert window.total_quote == Decimal("200.00000000")
    assert window.normal_quote_notional == Decimal("150.00000000")
    assert window.rpi_quote_notional == Decimal("50.00000000")


def test_buy_sell_delta_and_flow_imbalance_aggregate_exactly() -> None:
    aggregator = _aggregator()
    at = BASE + timedelta(minutes=14, seconds=59)
    aggregator.ingest(_event(1, at, price="100", quantity="2", buyer_is_maker=False))
    aggregator.ingest(_event(2, at, price="100", quantity="1", buyer_is_maker=True))

    window = aggregator.snapshot(as_of=BASE + timedelta(minutes=15)).windows["1m"]

    assert window.aggressive_buy_base == Decimal("2.00000000")
    assert window.aggressive_sell_base == Decimal("1.00000000")
    assert window.aggressive_buy_quote == Decimal("200.00000000")
    assert window.aggressive_sell_quote == Decimal("100.00000000")
    assert window.delta_quote == Decimal("100.00000000")
    assert window.total_quote == Decimal("300.00000000")
    assert window.flow_imbalance_ratio == Decimal("0.33333333")
    assert window.buyer_aggression_pct == Decimal("66.6667")


def test_empty_window_is_unavailable_and_never_zero_volume_verified() -> None:
    snapshot = _aggregator().snapshot(as_of=BASE + timedelta(minutes=15))

    assert snapshot.status == ContextStatus.UNAVAILABLE
    assert snapshot.reason == "no_valid_events"
    assert snapshot.windows["15m"].delta_quote is None
    assert snapshot.windows["15m"].flow_imbalance_ratio is None


def test_trade_time_selects_exact_utc_minute_boundaries() -> None:
    aggregator = _aggregator()
    aggregator.ingest(
        _event(1, BASE + timedelta(minutes=13, seconds=59, milliseconds=999), price="100")
    )
    aggregator.ingest(_event(2, BASE + timedelta(minutes=14), price="200"))

    one_minute = aggregator.snapshot(as_of=BASE + timedelta(minutes=15)).windows["1m"]

    assert one_minute.window_start == BASE + timedelta(minutes=14)
    assert one_minute.aggregate_event_count == 1
    assert one_minute.aggressive_buy_quote == Decimal("200.00000000")


def test_five_and_fifteen_minute_windows_are_exact_complete_minutes() -> None:
    _aggregator_state, snapshot = _fifteen_minute_replay(side="buy")

    assert snapshot.status == ContextStatus.VERIFIED
    assert snapshot.windows["1m"].aggressive_buy_quote == Decimal("114.00000000")
    assert snapshot.windows["5m"].aggressive_buy_quote == Decimal("560.00000000")
    assert snapshot.windows["15m"].aggressive_buy_quote == Decimal("1605.00000000")
    assert snapshot.windows["5m"].aggregate_event_count == 5
    assert snapshot.windows["15m"].aggregate_event_count == 15
    assert snapshot.windows["15m"].coverage_seconds == 900


def test_warmup_does_not_pretend_partial_history_is_full_15m() -> None:
    aggregator = SymbolFlowAggregator("BTCUSDT", stale_after_seconds=120)
    aggregator.mark_connected(BASE + timedelta(minutes=10))
    aggregator.ingest(_event(1, BASE + timedelta(minutes=14, seconds=59)))

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))

    assert snapshot.status == ContextStatus.UNAVAILABLE
    assert snapshot.reason == "insufficient_window_coverage"
    assert snapshot.windows["15m"].coverage_complete is False
    assert snapshot.windows["15m"].coverage_seconds == 300


def test_duplicate_and_older_aggregate_ids_are_discarded_without_corruption() -> None:
    aggregator = _aggregator()
    at = BASE + timedelta(minutes=14, seconds=59)
    assert aggregator.ingest(_event(10, at, quantity="2")) is True
    assert aggregator.ingest(_event(10, at, quantity="99")) is False
    assert aggregator.ingest(_event(9, at - timedelta(seconds=1), quantity="99")) is False

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))

    assert snapshot.windows["15m"].aggressive_buy_base == Decimal("2.00000000")
    assert snapshot.accepted_event_count == 1
    assert snapshot.duplicate_event_count == 1
    assert snapshot.out_of_order_event_count == 1


def test_trade_time_regression_and_id_gap_compromise_coverage() -> None:
    aggregator = _aggregator()
    aggregator.ingest(_event(1, BASE + timedelta(minutes=1)))
    assert aggregator.ingest(_event(2, BASE + timedelta(seconds=59))) is False
    aggregator.ingest(_event(3, BASE + timedelta(minutes=14, seconds=59)))

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))

    assert snapshot.status == ContextStatus.UNAVAILABLE
    assert snapshot.reason == "aggregate_trade_id_gap_in_window"
    assert snapshot.out_of_order_event_count == 1
    assert snapshot.gap_count == 1


def test_disconnect_and_reconnect_preserve_dedupe_but_reset_coverage() -> None:
    aggregator = _aggregator()
    first = _event(1, BASE + timedelta(minutes=1))
    aggregator.ingest(first)
    aggregator.mark_disconnected(BASE + timedelta(minutes=5))
    aggregator.mark_connected(BASE + timedelta(minutes=5, seconds=1), reconnect=True)
    assert aggregator.ingest(first) is False
    aggregator.ingest(_event(2, BASE + timedelta(minutes=14, seconds=59)))

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))

    assert snapshot.status == ContextStatus.UNAVAILABLE
    assert snapshot.reason == "connection_reconnect_in_window"
    assert snapshot.reconnect_count == 1
    assert snapshot.gap_count == 1
    assert snapshot.duplicate_event_count == 1


def test_last_event_freshness_is_explicit() -> None:
    aggregator = _aggregator(stale_after_seconds=5)
    aggregator.ingest(_event(1, BASE + timedelta(minutes=14, seconds=50)))

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))

    assert snapshot.status == ContextStatus.STALE
    assert snapshot.reason == "last_valid_event_stale"
    assert snapshot.age_seconds == 10.0


@pytest.mark.parametrize(
    ("price_return", "cvd", "expected"),
    [
        ("1", "1", PriceCvdAlignment.ALIGNED_UP),
        ("-1", "-1", PriceCvdAlignment.ALIGNED_DOWN),
        ("1", "-1", PriceCvdAlignment.PRICE_UP_CVD_DOWN),
        ("-1", "1", PriceCvdAlignment.PRICE_DOWN_CVD_UP),
        ("0", "1", PriceCvdAlignment.MIXED_FLAT),
    ],
)
def test_price_cvd_alignment_is_deterministic(
    price_return: str,
    cvd: str,
    expected: PriceCvdAlignment,
) -> None:
    assert classify_price_cvd_alignment(Decimal(price_return), Decimal(cvd)) == expected


def test_production_like_buyer_seller_and_balanced_replays_have_exact_outputs() -> None:
    _buyer, buyer = _fifteen_minute_replay(side="buy")
    _seller, seller = _fifteen_minute_replay(side="sell")
    _balanced, balanced = _fifteen_minute_replay(side="buy", paired_balanced=True)

    assert buyer.windows["15m"].delta_quote == Decimal("1605.00000000")
    assert buyer.windows["15m"].flow_imbalance_ratio == Decimal("1.00000000")
    assert buyer.windows["15m"].buyer_aggression_pct == Decimal("100.0000")
    assert buyer.windows["15m"].rolling_cvd_quote == Decimal("1605.00000000")
    assert buyer.windows["15m"].cvd_slope_quote_per_min > 0
    assert seller.windows["15m"].delta_quote == Decimal("-1605.00000000")
    assert seller.windows["15m"].flow_imbalance_ratio == Decimal("-1.00000000")
    assert seller.windows["15m"].buyer_aggression_pct == Decimal("0.0000")
    assert seller.windows["15m"].cvd_slope_quote_per_min < 0
    assert balanced.windows["15m"].delta_quote == Decimal("0E-8")
    assert balanced.windows["15m"].flow_imbalance_ratio == Decimal("0E-8")
    assert balanced.windows["15m"].buyer_aggression_pct == Decimal("50.0000")
    assert balanced.windows["15m"].cvd_slope_quote_per_min == Decimal("0E-8")


def test_production_like_divergence_replays_are_factual() -> None:
    _rising, rising_price_falling_cvd = _fifteen_minute_replay(side="sell")
    _falling, falling_price_rising_cvd = _fifteen_minute_replay(
        side="buy",
        descending_price=True,
    )

    assert (
        rising_price_falling_cvd.windows["15m"].price_cvd_alignment
        == PriceCvdAlignment.PRICE_UP_CVD_DOWN
    )
    assert (
        falling_price_rising_cvd.windows["15m"].price_cvd_alignment
        == PriceCvdAlignment.PRICE_DOWN_CVD_UP
    )


def test_summary_and_snapshot_serialization_are_deterministic_and_bounded() -> None:
    aggregator, first = _fifteen_minute_replay(side="buy")
    second = aggregator.snapshot(as_of=BASE + timedelta(minutes=15))
    serialized = first.model_dump_json()

    assert first.orderflow_summary == second.orderflow_summary
    assert first.orderflow_summary == (
        "15m delta +1.60K USDT; buyer aggression 100.0%; "
        "CVD slope positive; price/CVD ALIGNED_UP."
    )
    assert len(serialized.encode("utf-8")) < 8 * 1024
    assert "buyer_is_maker" not in serialized
    assert '"raw"' not in serialized


def test_bucket_history_is_bounded_and_contains_no_raw_trade_objects() -> None:
    aggregator = _aggregator(stale_after_seconds=120)
    for minute in range(40):
        aggregator.ingest(
            _event(
                minute + 1,
                BASE + timedelta(minutes=minute, seconds=59),
                price=str(100 + minute),
            )
        )

    snapshot = aggregator.snapshot(as_of=BASE + timedelta(minutes=40))

    assert aggregator.retained_bucket_count == MAX_RETAINED_MINUTE_BUCKETS == 16
    assert snapshot.retained_bucket_count == 16
    assert not hasattr(aggregator, "raw_events")


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


def test_service_reconciles_subscriptions_and_always_includes_btc() -> None:
    async def scenario() -> None:
        connection = _FakeConnection()
        service = MicrostructureFlowService(
            transport=_FakeTransport(connection),
            max_symbols=3,
            clock=lambda: BASE,
        )
        await service.start(["ETHUSDT", "SOLUSDT"])
        await _wait_until(lambda: len(connection.sent) == 1)

        first = json.loads(connection.sent[0])
        assert first["method"] == "SUBSCRIBE"
        assert first["params"] == [
            "btcusdt@aggTrade",
            "ethusdt@aggTrade",
            "solusdt@aggTrade",
        ]
        assert service.subscribed_symbols == ("BTCUSDT", "ETHUSDT", "SOLUSDT")

        await service.reconcile_symbols(["ADAUSDT"])
        unsubscribe = json.loads(connection.sent[1])
        subscribe = json.loads(connection.sent[2])
        assert unsubscribe["method"] == "UNSUBSCRIBE"
        assert unsubscribe["params"] == ["ethusdt@aggTrade", "solusdt@aggTrade"]
        assert subscribe["method"] == "SUBSCRIBE"
        assert subscribe["params"] == ["adausdt@aggTrade"]
        assert service.subscribed_symbols == ("ADAUSDT", "BTCUSDT")
        await service.stop()
        assert connection.closed is True

    asyncio.run(scenario())


def test_many_symbols_use_deterministic_cap_and_explicit_overflow() -> None:
    async def scenario() -> None:
        service = MicrostructureFlowService(
            transport=_FakeTransport(error=RuntimeError("offline fake")),
            max_symbols=100,
            clock=lambda: BASE,
        )
        requested = [f"S{index:04d}USDT" for index in range(500)]
        selected = await service.reconcile_symbols(requested)

        assert len(selected) == 100
        assert selected[0] == "BTCUSDT"
        assert service.health()["desired_symbol_count"] == 100
        assert service.health()["overflow_symbol_count"] == 401
        overflow = service.snapshot("S0499USDT")
        assert overflow.status == ContextStatus.UNAVAILABLE
        assert overflow.reason == "subscription_limit_exceeded:max_symbols=100"

    asyncio.run(scenario())


def test_malformed_and_wrong_contract_events_are_quarantined() -> None:
    async def scenario() -> None:
        service = MicrostructureFlowService(clock=lambda: BASE)
        await service.reconcile_symbols(["BTCUSDT"])
        service._handle_payload("not-json")
        service._handle_payload(_payload(1, BASE, stream_type=2))
        service._handle_payload(_payload(1, BASE))

        health = service.health()
        assert health["malformed_event_count"] == 1
        assert health["wrong_contract_event_count"] == 1
        assert service.snapshot("BTCUSDT").accepted_event_count == 1

    asyncio.run(scenario())


def test_transport_failure_is_non_fatal_and_snapshot_is_unavailable() -> None:
    async def scenario() -> None:
        service = MicrostructureFlowService(
            transport=_FakeTransport(error=ConnectionError("synthetic outage")),
            reconnect_base_seconds=60,
            reconnect_max_seconds=60,
            clock=lambda: BASE,
        )
        await service.start(["ETHUSDT"])
        await _wait_until(lambda: service.health()["disconnect_count"] == 1)
        snapshot = service.snapshot("ETHUSDT")

        assert service.running is True
        assert snapshot.status == ContextStatus.UNAVAILABLE
        assert snapshot.reason == "stream_disconnected"
        assert service.health()["last_error"] == "ConnectionError:synthetic outage"
        await service.stop()

    asyncio.run(scenario())
