from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.context.models import ContextStatus
from app.data.exceptions import ExchangeRateLimitError, ExchangeTimeoutError
from app.microstructure.order_book import (
    BinanceDepthEvent,
    BookIngestOutcome,
    OrderBookPayloadError,
    SynchronizedLocalOrderBook,
    WrongOrderBookContractTypeError,
    parse_binance_depth_event,
    parse_binance_depth_snapshot,
)
from app.microstructure.order_book_service import (
    BinanceDepthSnapshotClient,
    OrderBookLiquidityService,
)


NOW = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
NOW_MS = int(NOW.timestamp() * 1000)


def _event_payload(
    *,
    symbol: str = "BTCUSDT",
    first: int = 100,
    final: int = 101,
    previous: int = 99,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
    stream_type: Any = 1,
    event_ms: int = NOW_MS,
) -> dict[str, Any]:
    return {
        "e": "depthUpdate",
        "E": event_ms,
        "T": event_ms - 1,
        "s": symbol,
        "U": first,
        "u": final,
        "pu": previous,
        "b": bids or [],
        "a": asks or [],
        "ps": symbol,
        "st": stream_type,
    }


def _snapshot_payload(
    *,
    update_id: int = 100,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
) -> dict[str, Any]:
    return {
        "lastUpdateId": update_id,
        "E": NOW_MS - 10,
        "T": NOW_MS - 11,
        "bids": bids
        or [
            ["99.99", "1"],
            ["99.90", "2"],
            ["99.75", "3"],
            ["99.50", "4"],
            ["99.00", "5"],
        ],
        "asks": asks
        or [
            ["100.01", "1"],
            ["100.10", "2"],
            ["100.25", "3"],
            ["100.50", "4"],
            ["101.00", "5"],
        ],
    }


def _book(*, buffer_size: int = 256, stale_after: float = 5.0) -> SynchronizedLocalOrderBook:
    book = SynchronizedLocalOrderBook(
        "BTCUSDT",
        stale_after_seconds=stale_after,
        event_buffer_size=buffer_size,
    )
    book.mark_connected(reconnect=False)
    return book


def _synchronized_book(
    *,
    snapshot_payload: dict[str, Any] | None = None,
    bridge_payload: dict[str, Any] | None = None,
) -> SynchronizedLocalOrderBook:
    book = _book()
    outcome = book.ingest(
        parse_binance_depth_event(bridge_payload or _event_payload()),
        received_at=NOW,
    )
    assert outcome == BookIngestOutcome.BUFFERED
    outcome = book.install_snapshot(
        parse_binance_depth_snapshot(snapshot_payload or _snapshot_payload()),
        received_at=NOW,
    )
    assert outcome == BookIngestOutcome.SYNCHRONIZED
    return book


def test_valid_usdm_depth_event_parses_decimal_levels_and_combined_wrapper() -> None:
    payload = _event_payload(
        bids=[["99.5", "2.25"]],
        asks=[["100.5", "3.75"]],
    )
    event = parse_binance_depth_event({"stream": "btcusdt@depth@500ms", "data": payload})
    assert event.symbol == "BTCUSDT"
    assert event.pair_symbol == "BTCUSDT"
    assert event.first_update_id == 100
    assert event.final_update_id == 101
    assert event.previous_final_update_id == 99
    assert event.bids[0].price == Decimal("99.5")
    assert event.bids[0].quantity == Decimal("2.25")


def test_coin_m_and_malformed_stream_types_are_rejected() -> None:
    with pytest.raises(WrongOrderBookContractTypeError):
        parse_binance_depth_event(_event_payload(stream_type=2))
    for value in (None, "1", True, 3):
        with pytest.raises(OrderBookPayloadError):
            parse_binance_depth_event(_event_payload(stream_type=value))


def test_snapshot_parses_decimal_sides_and_rejects_bad_shapes() -> None:
    snapshot = parse_binance_depth_snapshot(_snapshot_payload())
    assert snapshot.last_update_id == 100
    assert isinstance(snapshot.bids[0].price, Decimal)
    assert isinstance(snapshot.asks[0].quantity, Decimal)
    with pytest.raises(OrderBookPayloadError):
        parse_binance_depth_snapshot({**_snapshot_payload(), "bids": [["99"]]})


def test_quantity_zero_removes_and_nonzero_inserts_or_updates() -> None:
    book = _synchronized_book()
    event = parse_binance_depth_event(
        _event_payload(
            first=102,
            final=103,
            previous=101,
            bids=[["99.99", "0"], ["99.80", "7"]],
            asks=[["100.01", "9"]],
        )
    )
    assert book.ingest(event, received_at=NOW + timedelta(milliseconds=500)) == BookIngestOutcome.APPLIED
    assert Decimal("99.99") not in book.bids
    assert book.bids[Decimal("99.80")] == Decimal("7")
    assert book.asks[Decimal("100.01")] == Decimal("9")


def test_buffer_discards_stale_event_then_accepts_exact_official_bridge() -> None:
    book = _book()
    stale = parse_binance_depth_event(_event_payload(first=95, final=99, previous=94))
    bridge = parse_binance_depth_event(_event_payload(first=99, final=101, previous=98))
    book.ingest(stale, received_at=NOW - timedelta(seconds=1))
    book.ingest(bridge, received_at=NOW)
    outcome = book.install_snapshot(parse_binance_depth_snapshot(_snapshot_payload()), received_at=NOW)
    assert outcome == BookIngestOutcome.SYNCHRONIZED
    assert book.synchronized is True
    assert book.last_update_id == 101
    assert book.buffered_event_count == 0


def test_invalid_initial_bridge_fails_closed_and_requests_resync() -> None:
    book = _book()
    book.ingest(
        parse_binance_depth_event(_event_payload(first=101, final=102, previous=100)),
        received_at=NOW,
    )
    outcome = book.install_snapshot(parse_binance_depth_snapshot(_snapshot_payload()), received_at=NOW)
    assert outcome == BookIngestOutcome.NEEDS_RESYNC
    assert book.synchronized is False
    assert book.needs_resync is True
    assert book.bids == {}
    assert book.snapshot(as_of=NOW).status != ContextStatus.VERIFIED


def test_correct_subsequent_pu_applies_but_gap_invalidates_all_trusted_levels() -> None:
    book = _synchronized_book()
    valid = parse_binance_depth_event(
        _event_payload(first=102, final=104, previous=101, bids=[["99.80", "2"]])
    )
    assert book.ingest(valid, received_at=NOW) == BookIngestOutcome.APPLIED
    gap = parse_binance_depth_event(
        _event_payload(first=106, final=107, previous=105, bids=[["99.70", "2"]])
    )
    assert book.ingest(gap, received_at=NOW) == BookIngestOutcome.NEEDS_RESYNC
    assert book.synchronized is False
    assert book.bids == {}
    assert book.asks == {}
    assert book.gap_count == 1
    snapshot = book.snapshot(as_of=NOW)
    assert snapshot.status == ContextStatus.STALE
    assert snapshot.reason == "sequence_gap"


def test_duplicate_and_out_of_order_old_events_are_ignored_before_pu_check() -> None:
    book = _synchronized_book()
    duplicate = parse_binance_depth_event(
        _event_payload(first=100, final=101, previous=1, bids=[["99.99", "999"]])
    )
    old = parse_binance_depth_event(
        _event_payload(first=90, final=95, previous=1, bids=[["99.99", "888"]])
    )
    assert book.ingest(duplicate, received_at=NOW) == BookIngestOutcome.IGNORED_OLD
    assert book.ingest(old, received_at=NOW) == BookIngestOutcome.IGNORED_OLD
    assert book.bids[Decimal("99.99")] == Decimal("1")
    assert book.duplicate_event_count == 1
    assert book.out_of_order_event_count == 1
    assert book.synchronized is True


def test_reconnect_never_reuses_previous_synchronization() -> None:
    book = _synchronized_book()
    book.mark_disconnected()
    assert book.synchronized is False
    assert book.bids == {}
    assert book.snapshot(as_of=NOW).reason == "stream_disconnected"
    book.mark_connected(reconnect=True)
    assert book.snapshot(as_of=NOW).reason == "resyncing"


def test_event_buffer_is_hard_bounded_and_overflow_never_verifies() -> None:
    book = _book(buffer_size=2)
    assert book.ingest(parse_binance_depth_event(_event_payload(final=101)), received_at=NOW) == BookIngestOutcome.BUFFERED
    assert book.ingest(parse_binance_depth_event(_event_payload(final=102)), received_at=NOW) == BookIngestOutcome.BUFFERED
    assert book.ingest(parse_binance_depth_event(_event_payload(final=103)), received_at=NOW) == BookIngestOutcome.BUFFER_OVERFLOW
    assert book.buffered_event_count == 0
    assert book.buffer_overflow_count == 1
    assert book.snapshot(as_of=NOW).status != ContextStatus.VERIFIED


def test_crossed_snapshot_and_crossed_live_update_are_never_verified() -> None:
    crossed = _snapshot_payload(bids=[["101", "1"]], asks=[["100", "1"]])
    book = _book()
    book.ingest(parse_binance_depth_event(_event_payload()), received_at=NOW)
    assert book.install_snapshot(parse_binance_depth_snapshot(crossed), received_at=NOW) == BookIngestOutcome.NEEDS_RESYNC
    assert book.snapshot(as_of=NOW).status != ContextStatus.VERIFIED

    live = _synchronized_book()
    crossing = parse_binance_depth_event(
        _event_payload(first=102, final=102, previous=101, bids=[["101.50", "1"]])
    )
    assert live.ingest(crossing, received_at=NOW) == BookIngestOutcome.NEEDS_RESYNC
    assert live.snapshot(as_of=NOW).status != ContextStatus.VERIFIED


def test_reference_prices_all_bands_imbalance_and_concentrations_are_exact() -> None:
    snapshot = _synchronized_book().snapshot(as_of=NOW + timedelta(seconds=1))
    assert snapshot.status == ContextStatus.VERIFIED
    assert snapshot.best_bid == Decimal("99.99")
    assert snapshot.best_ask == Decimal("100.01")
    assert snapshot.mid_price == Decimal("100.00")
    assert snapshot.spread_absolute == Decimal("0.02")
    assert snapshot.spread_bps == Decimal("2.0000")

    expected = {
        "10bps": (Decimal("299.79000000"), Decimal("300.21000000"), Decimal("-0.00070000")),
        "25bps": (Decimal("599.04000000"), Decimal("600.96000000"), Decimal("-0.00160000")),
        "50bps": (Decimal("997.04000000"), Decimal("1002.96000000"), Decimal("-0.00296000")),
        "100bps": (Decimal("1492.04000000"), Decimal("1507.96000000"), Decimal("-0.00530667")),
    }
    for label, (bid_quote, ask_quote, imbalance) in expected.items():
        band = snapshot.bands[label]
        assert band.bid_quote_notional == bid_quote
        assert band.ask_quote_notional == ask_quote
        assert band.depth_imbalance == imbalance
        assert band.bid_coverage_complete is True
        assert band.ask_coverage_complete is True
    assert snapshot.furthest_bid_distance_bps == Decimal("100.0000")
    assert snapshot.furthest_ask_distance_bps == Decimal("100.0000")
    assert snapshot.largest_bid_level is not None
    assert snapshot.largest_bid_level.price == Decimal("99.00")
    assert snapshot.largest_bid_level.quote_notional == Decimal("495.00000000")
    assert snapshot.largest_bid_level.distance_bps == Decimal("100.0000")
    assert snapshot.largest_bid_level.share_of_observed_band == Decimal("0.33176054")
    assert snapshot.largest_ask_level is not None
    assert snapshot.largest_ask_level.price == Decimal("101.00")
    assert snapshot.largest_ask_level.quote_notional == Decimal("505.00000000")


@pytest.mark.parametrize(
    ("bid_qty", "ask_qty", "expected_sign"),
    [
        ("10", "1", 1),
        ("1", "10", -1),
        ("1", "0.9998000199980002", 0),
    ],
)
def test_production_like_bid_heavy_ask_heavy_and_balanced_books(
    bid_qty: str,
    ask_qty: str,
    expected_sign: int,
) -> None:
    payload = _snapshot_payload(
        bids=[["99.99", bid_qty], ["99.00", "1"]],
        asks=[["100.01", ask_qty], ["101.00", "1"]],
    )
    imbalance = _synchronized_book(snapshot_payload=payload).snapshot(as_of=NOW).bands[
        "10bps"
    ].depth_imbalance
    assert imbalance is not None
    assert (imbalance > 0) - (imbalance < 0) == expected_sign


def test_zero_total_depth_denominator_is_safe() -> None:
    payload = _snapshot_payload(bids=[["99", "1"]], asks=[["101", "1"]])
    snapshot = _synchronized_book(snapshot_payload=payload).snapshot(as_of=NOW)
    band = snapshot.bands["10bps"]
    assert band.bid_quote_notional == Decimal("0E-8")
    assert band.ask_quote_notional == Decimal("0E-8")
    assert band.depth_imbalance is None


def test_incomplete_far_side_coverage_is_explicit_not_silently_complete() -> None:
    payload = _snapshot_payload(
        bids=[["99.99", "1"], ["99.40", "2"]],
        asks=[["100.01", "1"], ["100.62", "2"]],
    )
    snapshot = _synchronized_book(snapshot_payload=payload).snapshot(as_of=NOW)
    assert snapshot.status == ContextStatus.VERIFIED
    assert snapshot.verified is True
    assert snapshot.reason == "insufficient_book_coverage"
    assert snapshot.bands["50bps"].bid_coverage_complete is True
    assert snapshot.bands["50bps"].ask_coverage_complete is True
    assert snapshot.bands["100bps"].bid_coverage_complete is False
    assert snapshot.bands["100bps"].ask_coverage_complete is False
    below = snapshot.liquidity_below_context()
    above = snapshot.liquidity_above_context()
    assert below is not None and below["bands"]["100bps"]["coverage_complete"] is False
    assert above is not None and above["bands"]["100bps"]["coverage_complete"] is False


def test_liquidity_below_maps_only_to_bids_and_above_only_to_asks() -> None:
    snapshot = _synchronized_book().snapshot(as_of=NOW)
    below = snapshot.liquidity_below_context()
    above = snapshot.liquidity_above_context()
    assert below is not None and above is not None
    assert below["usage"] == "research_only"
    assert below["side"] == "bid"
    assert above["side"] == "ask"
    assert below["bands"]["10bps"]["quote_notional"] == Decimal("299.79000000")
    assert above["bands"]["10bps"]["quote_notional"] == Decimal("300.21000000")
    assert "ask_quote_notional" not in below["bands"]["10bps"]
    assert "bid_quote_notional" not in above["bands"]["10bps"]


def test_stale_context_is_unverified_and_contains_no_trusted_depth_values() -> None:
    snapshot = _synchronized_book().snapshot(as_of=NOW + timedelta(seconds=10))
    assert snapshot.status == ContextStatus.STALE
    below = snapshot.liquidity_below_context()
    assert below is not None
    assert below["status"] == "STALE"
    assert "bands" not in below
    assert "largest_level" not in below


def test_compact_snapshot_is_under_budget_and_contains_no_raw_depth() -> None:
    snapshot = _synchronized_book().snapshot(as_of=NOW)
    serialized = snapshot.model_dump_json()
    assert len(serialized.encode("utf-8")) <= 4096
    for forbidden in ('"bids"', '"asks"', '"data"', '"raw"'):
        assert forbidden not in serialized
    assert '"bands"' in serialized


class _StaticSnapshotClient:
    def __init__(self, payload: Mapping[str, Any] | None = None, error: Exception | None = None):
        self.payload = payload or _snapshot_payload()
        self.error = error
        self.calls: list[tuple[str, int]] = []
        self.closed = False

    async def fetch(self, symbol: str, limit: int) -> Mapping[str, Any]:
        self.calls.append((symbol, limit))
        if self.error is not None:
            raise self.error
        return self.payload

    async def aclose(self) -> None:
        self.closed = True


class _IdleConnection:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.closed = False
        self._never = asyncio.Event()

    async def recv(self) -> str:
        await self._never.wait()
        raise AssertionError("unreachable")

    async def send(self, payload: str) -> None:
        self.sent.append(payload)

    async def close(self) -> None:
        self.closed = True


class _StaticTransport:
    def __init__(self, connection: _IdleConnection):
        self.connection = connection
        self.max_queue = 32

    async def connect(self) -> _IdleConnection:
        return self.connection


def test_service_bootstrap_failure_is_bounded_and_shutdown_is_clean() -> None:
    asyncio.run(_assert_service_bootstrap_failure_is_bounded_and_shutdown_is_clean())


async def _assert_service_bootstrap_failure_is_bounded_and_shutdown_is_clean() -> None:
    connection = _IdleConnection()
    client = _StaticSnapshotClient(error=ExchangeTimeoutError("timeout"))
    service = OrderBookLiquidityService(
        transport=_StaticTransport(connection),
        snapshot_client=client,
        bootstrap_attempts=2,
        bootstrap_min_interval_seconds=0,
        bootstrap_backoff_seconds=0,
        bootstrap_jitter_seconds=0,
        reconnect_base_seconds=0,
    )
    await service.start(["ETHUSDT"])
    for _ in range(100):
        if service.health()["snapshot_failure_count"] >= 2:
            break
        await asyncio.sleep(0)
    assert len(client.calls) == 4  # BTC and ETH, two bounded attempts each.
    assert service.snapshot("BTCUSDT").reason == "snapshot_failed"
    assert service.snapshot("ETHUSDT").status == ContextStatus.ERROR
    await service.stop()
    assert connection.closed is True
    assert client.closed is True
    assert service.running is False


def test_service_subscription_is_bounded_prioritized_and_uses_configured_speed() -> None:
    asyncio.run(_assert_service_subscription_is_bounded_prioritized_and_uses_configured_speed())


async def _assert_service_subscription_is_bounded_prioritized_and_uses_configured_speed() -> None:
    connection = _IdleConnection()
    service = OrderBookLiquidityService(
        transport=_StaticTransport(connection),
        snapshot_client=_StaticSnapshotClient(),
        max_symbols=2,
        update_speed="500ms",
        bootstrap_min_interval_seconds=0,
        bootstrap_jitter_seconds=0,
    )
    await service.start(["ZZZUSDT", "AAAUSDT"])
    for _ in range(100):
        if connection.sent:
            break
        await asyncio.sleep(0)
    assert service.subscribed_symbols == ("AAAUSDT", "BTCUSDT")
    request = json.loads(connection.sent[0])
    assert request["params"] == ["aaausdt@depth@500ms", "btcusdt@depth@500ms"]
    overflow = service.snapshot("ZZZUSDT")
    assert overflow.status == ContextStatus.UNAVAILABLE
    assert overflow.reason == "subscription_limit_exceeded:max_symbols=2"
    await service.stop()


def test_snapshot_http_client_handles_success_rate_limit_timeout_and_bad_json() -> None:
    asyncio.run(_assert_snapshot_http_client_handles_success_rate_limit_timeout_and_bad_json())


async def _assert_snapshot_http_client_handles_success_rate_limit_timeout_and_bad_json() -> None:
    success_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, json=_snapshot_payload(), request=request)
    )
    async with httpx.AsyncClient(
        base_url="https://fapi.binance.com",
        transport=success_transport,
    ) as http_client:
        client = BinanceDepthSnapshotClient(http_client=http_client)
        payload = await client.fetch("BTCUSDT", 500)
        assert payload["lastUpdateId"] == 100

    limited_transport = httpx.MockTransport(
        lambda request: httpx.Response(
            429,
            headers={"Retry-After": "3"},
            request=request,
        )
    )
    async with httpx.AsyncClient(base_url="https://fapi.binance.com", transport=limited_transport) as http_client:
        client = BinanceDepthSnapshotClient(http_client=http_client)
        with pytest.raises(ExchangeRateLimitError) as caught:
            await client.fetch("BTCUSDT", 500)
        assert caught.value.retry_after == 3

    async def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    async with httpx.AsyncClient(
        base_url="https://fapi.binance.com",
        transport=httpx.MockTransport(timeout_handler),
    ) as http_client:
        client = BinanceDepthSnapshotClient(http_client=http_client)
        with pytest.raises(ExchangeTimeoutError):
            await client.fetch("BTCUSDT", 500)

    bad_transport = httpx.MockTransport(
        lambda request: httpx.Response(200, text="not-json", request=request)
    )
    async with httpx.AsyncClient(base_url="https://fapi.binance.com", transport=bad_transport) as http_client:
        client = BinanceDepthSnapshotClient(http_client=http_client)
        with pytest.raises(Exception, match="malformed JSON"):
            await client.fetch("BTCUSDT", 500)


def test_service_configuration_exposes_current_weight_and_hard_bounds() -> None:
    service = OrderBookLiquidityService(snapshot_client=_StaticSnapshotClient())
    assert service.update_speed == "500ms"
    assert service.snapshot_limit == 500
    assert service.snapshot_request_weight == 10
    assert service.event_buffer_size == 256
    assert service.bootstrap_concurrency == 2
    assert service.transport.max_message_bytes == 1024 * 1024
    assert service.transport.max_queue == 4
    assert service.health()["websocket_max_message_bytes"] == 1024 * 1024
    assert service.health()["websocket_queue_bound"] == 4
    with pytest.raises(ValueError, match="max_symbols"):
        OrderBookLiquidityService(snapshot_client=_StaticSnapshotClient(), max_symbols=101)
    with pytest.raises(ValueError, match="update_speed"):
        OrderBookLiquidityService(snapshot_client=_StaticSnapshotClient(), update_speed="42ms")


def test_all_core_fixtures_are_network_free(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_network(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("external network call attempted")

    monkeypatch.setattr(httpx.AsyncClient, "get", forbidden_network)
    assert _synchronized_book().snapshot(as_of=NOW).status == ContextStatus.VERIFIED
