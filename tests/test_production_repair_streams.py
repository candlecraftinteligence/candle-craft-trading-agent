"""Synthetic stream replay; never opens a socket or starts a production service."""
import asyncio
import json
from datetime import timedelta

import pytest

from app.context.models import ContextStatus
from app.microstructure.agg_trade import parse_binance_agg_trade, AggTradePayloadError
from app.microstructure.liquidation import parse_binance_liquidation
from app.microstructure.order_book import parse_binance_depth_event, parse_binance_depth_snapshot
from app.microstructure.service import MicrostructureFlowService
from app.microstructure.order_book_service import OrderBookLiquidityService
from app.microstructure.liquidation_service import LiquidationFlowService
from app.microstructure.stream_evidence import StreamEvidence
from tests.test_microstructure_flow import _payload as trade
from tests.test_order_book_liquidity import NOW, _event_payload as depth, _snapshot_payload
from tests.test_liquidation_flow import _payload as liquidation

pytestmark = pytest.mark.no_auto_epoch
UNICODE = "币安人生USDT"


def metadata(*symbols):
    return {"symbols": [{"symbol": s, "pair": s, "baseAsset": s[:-4],
        "quoteAsset": "USDT", "marginAsset": "USDT", "contractType": "PERPETUAL", "status": "TRADING"}
        for s in symbols]}


def test_unicode_literal_identity_survives_all_three_parsers_and_collectors():
    async def exercise():
        agg, book, liq = (MicrostructureFlowService(clock=lambda: NOW),
            OrderBookLiquidityService(clock=lambda: NOW), LiquidationFlowService(clock=lambda: NOW))
        try:
            for service in (agg, book, liq):
                service.evidence.set_metadata(metadata("BTCUSDT", UNICODE), NOW)
                await service.reconcile_symbols([UNICODE])
            a, b, c = trade(1, NOW, symbol=UNICODE), depth(symbol=UNICODE), liquidation(NOW, symbol=UNICODE)
            assert parse_binance_agg_trade(a).symbol == parse_binance_depth_event(b).symbol == parse_binance_liquidation(c).symbol == UNICODE
            agg._aggregators[UNICODE].mark_connected(NOW)
            liq._aggregators[UNICODE].mark_connected(NOW)
            book._books[UNICODE].mark_connected(reconnect=False)
            assert agg._handle_payload(a)
            assert book._handle_payload(b)
            book._books[UNICODE].install_snapshot(parse_binance_depth_snapshot(_snapshot_payload()), received_at=NOW)
            assert book.snapshot(UNICODE).synchronized
            assert liq._handle_payload(c)
            for service in (agg, book, liq):
                health = service.health()
                assert health["per_symbol"][UNICODE]["decoded_events"] == 1
                assert health["per_symbol"][UNICODE]["identity_status"] == "VERIFIED"
        finally:
            await book.snapshot_client.aclose()
    asyncio.run(exercise())


@pytest.mark.parametrize("symbol", ["btcUSDT", " BTCUSDT", "BTCUSDT ", "BТCUSDT", "ßUSDT"])
def test_normalized_or_confusable_events_never_alias_the_requested_contract(symbol):
    evidence = StreamEvidence(lambda: NOW)
    evidence.set_metadata(metadata("BTCUSDT"), NOW)
    assert not evidence.matches(symbol)
    if symbol != "BТCUSDT":
        with pytest.raises(AggTradePayloadError):
            parse_binance_agg_trade(trade(1, NOW, symbol=symbol))


def test_metadata_collision_wrong_contract_and_expiry_fail_closed_without_reconnect():
    current = NOW
    evidence = StreamEvidence(lambda: current)
    evidence.set_metadata(metadata("BTCUSDT", "ＢＴＣUSDT"), NOW)
    assert not evidence.matches("BTCUSDT")
    assert "collision" in evidence.identity_reason("BTCUSDT")
    current += timedelta(seconds=1)
    evidence.set_metadata(metadata(UNICODE), current)
    assert not evidence.matches(UNICODE, "BTCUSDT")
    assert not evidence.matches("BTCUSDT")
    current += timedelta(seconds=301)
    assert not evidence.matches(UNICODE)
    assert evidence.health({UNICODE})["metadata_status"] == "STALE"


@pytest.mark.parametrize("payload,acquired", [({"symbols": None}, NOW), (metadata(UNICODE), None),
    (metadata(UNICODE), "bad"), ({"symbols": []}, NOW)])
def test_bad_metadata_is_withheld_without_breaking_core_or_health(payload, acquired):
    evidence = StreamEvidence(lambda: NOW)
    evidence.set_metadata(payload, acquired)
    assert not evidence.matches(UNICODE)
    assert evidence.health({UNICODE})["metadata_status"] == "Unverified"


def test_missing_metadata_pair_does_not_prove_exact_perpetual_identity():
    payload = metadata(UNICODE)
    payload["symbols"][0].pop("pair")
    evidence = StreamEvidence(lambda: NOW)
    evidence.set_metadata(payload, NOW)
    assert not evidence.matches(UNICODE)


def test_subscription_ack_is_distinct_from_requested_and_restored_after_disconnect():
    evidence = StreamEvidence(lambda: NOW)
    evidence.requested(1, "SUBSCRIBE", {UNICODE})
    assert evidence.health({UNICODE})["acknowledged_symbol_count"] == 0
    assert not evidence.control({"id": 2, "result": None})
    assert evidence.control({"id": 1, "result": None})
    assert evidence.health({UNICODE})["acknowledged_symbol_count"] == 1
    evidence.reset()
    assert evidence.health({UNICODE})["acknowledged_symbol_count"] == 0
    evidence.requested(2, "SUBSCRIBE", {UNICODE})
    evidence.requested(3, "UNSUBSCRIBE", {UNICODE})
    assert evidence.control({"id": 3, "result": None})
    assert evidence.control({"id": 2, "result": None})
    assert UNICODE not in evidence.acknowledged
    for n in range(4, 200):
        evidence.requested(n, "SUBSCRIBE", {UNICODE})
    assert len(evidence.pending) <= 128 and len(evidence.history) == 32


def test_unsupported_liquidation_symbol_cannot_inherit_verified_connection_but_quiet_eligible_can():
    current = NOW
    async def exercise():
        nonlocal current
        service = LiquidationFlowService(clock=lambda: current)
        service.evidence.set_metadata(metadata("BTCUSDT", UNICODE), NOW)
        await service.reconcile_symbols([UNICODE, "UNKNOWNUSDT"])
        for aggregator in service._aggregators.values():
            aggregator.mark_connected(NOW)
        current += timedelta(seconds=61)
        quiet = service.snapshot(UNICODE)
        assert quiet.windows["1m"].event_count == 0
        assert quiet.windows["1m"].status == ContextStatus.VERIFIED
        unsupported = service.snapshot("UNKNOWNUSDT")
        assert unsupported.status == ContextStatus.UNVERIFIED
        assert unsupported.reason == "not_verified_binance_usdm_contract"
        assert service.health()["per_symbol"][UNICODE]["source_event_age_seconds"] is None
    asyncio.run(exercise())


def test_assessment_reports_depth_band_coverage_separately_from_synchronization():
    from app.microstructure.order_book_models import OrderBookLiquiditySnapshot, LiquidityBandSnapshot
    evidence = StreamEvidence(lambda: NOW)
    snapshot = OrderBookLiquiditySnapshot(symbol=UNICODE, status=ContextStatus.VERIFIED,
        synchronized=True, observed_at=NOW, age_seconds=0,
        bands={"100bps": LiquidityBandSnapshot(band_bps=100, bid_quote_notional=1,
            ask_quote_notional=1, bid_coverage_complete=False, ask_coverage_complete=True)})
    evidence.assessed(UNICODE, snapshot)
    assessed = evidence.health({UNICODE})["per_symbol"][UNICODE]["assessment"]
    assert assessed["synchronized"] is True and assessed["all_depth_bands_complete"] is False


def test_reconciliation_preserves_flow_warmup_and_real_id_gaps_require_new_coverage():
    current = NOW + timedelta(seconds=10)

    class Connection:
        def __init__(self):
            self.sent = []

        async def send(self, payload):
            self.sent.append(json.loads(payload))

    async def exercise():
        nonlocal current
        service = MicrostructureFlowService(clock=lambda: current)
        await service.reconcile_symbols(["BTCUSDT"])
        connection = Connection()
        service._connection = connection
        aggregator = service._aggregators["BTCUSDT"]
        aggregator.mark_connected(current)
        await service._send_subscription_diff(connection, additions={"BTCUSDT"}, removals=set())
        service._handle_payload({"id": connection.sent[0]["id"], "result": None})
        frozen = None
        for minute in range(17):
            current = NOW + timedelta(minutes=minute, seconds=10)
            service.evidence.set_metadata(metadata("BTCUSDT"), current)
            await service.reconcile_symbols(["BTCUSDT"])
            payload = trade(100 + minute, current - timedelta(seconds=1))
            # Underlying trade IDs are a different sequence from aggregate IDs.
            payload.update(f=1000 + minute * 10, l=1000 + minute * 10 + 3)
            assert service._handle_payload(payload)
            snapshot = service.snapshot("BTCUSDT")
            assert snapshot.gap_count == 0
            assert (snapshot.status == ContextStatus.VERIFIED) is (minute == 16)
            if minute == 5:
                frozen = snapshot
        assert frozen.status == ContextStatus.UNAVAILABLE
        assert frozen.windows["15m"].coverage_complete is False
        assert service._aggregators["BTCUSDT"] is aggregator
        assert len(connection.sent) == 1
        assert service.health()["acknowledged_symbol_count"] == 1

        # A missing aggregate ID resets the whole canonical coverage window.
        for minute in range(17, 34):
            current = NOW + timedelta(minutes=minute, seconds=10)
            service.evidence.set_metadata(metadata("BTCUSDT"), current)
            assert service._handle_payload(trade(101 + minute, current - timedelta(seconds=1)))
            snapshot = service.snapshot("BTCUSDT")
            assert snapshot.gap_count == 1
            if minute < 33:
                assert snapshot.status == ContextStatus.UNAVAILABLE
                assert snapshot.reason == "aggregate_trade_id_gap_in_window"
            else:
                assert snapshot.status == ContextStatus.VERIFIED
        current += timedelta(seconds=service.stale_after_seconds + 1)
        service.evidence.set_metadata(metadata("BTCUSDT"), current)
        assert service.snapshot("BTCUSDT").status == ContextStatus.STALE

    asyncio.run(exercise())


def test_quiet_liquidation_windows_require_matching_ack_and_full_observed_coverage():
    current = NOW

    async def exercise():
        nonlocal current
        service = LiquidationFlowService(clock=lambda: current)
        await service.reconcile_symbols(["BTCUSDT"])
        service._connection = object()  # Injected connection; no socket/service is started.
        service._connection_count = 1
        service.evidence.requested(1, "SUBSCRIBE", {"BTCUSDT"})
        service._handle_payload({"id": 2, "result": None})
        current += timedelta(minutes=16)
        service.evidence.set_metadata(metadata("BTCUSDT"), current)
        missing = service.snapshot("BTCUSDT")
        assert not service.health()["subscription_confirmed"]
        assert all(window.event_count is None for window in missing.windows.values())
        assert all(window.status != ContextStatus.VERIFIED for window in missing.windows.values())

        service._handle_payload({"id": 1, "result": None})
        connected_at = current
        for minute in range(16):
            current = connected_at + timedelta(minutes=minute)
            service.evidence.set_metadata(metadata("BTCUSDT"), current)
            await service.reconcile_symbols(["BTCUSDT"])
            snapshot = service.snapshot("BTCUSDT")
            assert (snapshot.status == ContextStatus.VERIFIED) is (minute == 15)
            canonical = snapshot.windows["15m"]
            assert canonical.event_count == (0 if minute == 15 else None)
            assert canonical.total_liquidation_quote == (0 if minute == 15 else None)
        assert service.health()["subscription_scope"] == "all_markets"
        assert service.health()["acknowledged_symbol_count"] == 1
        service._invalidate_stream()
        assert all(window.event_count is None for window in service.snapshot("BTCUSDT").windows.values())

    asyncio.run(exercise())


def test_frozen_book_assessment_does_not_freshen_or_mutate_after_new_depth():
    from tests.test_order_book_liquidity import _synchronized_book

    book = _synchronized_book()
    frozen = book.snapshot(as_of=NOW)
    assert frozen.status == ContextStatus.VERIFIED
    late = NOW + timedelta(seconds=6)
    assert book.snapshot(as_of=late).status == ContextStatus.STALE
    book.ingest(parse_binance_depth_event(depth(
        first=102, final=103, previous=101, event_ms=int(late.timestamp() * 1000),
        bids=[["99.99", "2"]],
    )), received_at=late)
    refreshed = book.snapshot(as_of=late)
    assert refreshed.status == ContextStatus.VERIFIED
    assert refreshed.observed_at == late
    assert frozen.observed_at == NOW
    assert frozen.bands["10bps"].bid_quote_notional != refreshed.bands["10bps"].bid_quote_notional
    book.mark_disconnected()
    assert book.snapshot(as_of=late).status != ContextStatus.VERIFIED
