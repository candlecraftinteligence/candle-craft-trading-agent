"""A fixed scan decision keeps pre-decision precision through cache refreshes."""
import asyncio
from datetime import datetime, timedelta
from decimal import Decimal as D

import httpx
import pytest

import app.data.exchange_clients.binance_futures as binance_module
from app.alerts.telegram_lifecycle import TelegramLifecycleDeliveryService
from app.data.exchange_clients import BinanceFuturesClient
from app.cache.market_data_cache import MarketDataCache
from app.runtime_epoch.origin import producer_acquisition_from_symbol_result
from app.lifecycle.service import apply_lifecycle_to_run_result
from app.pipeline.scanner_runner import ScannerRunner
from app.storage.repositories import store_scan_result
from tests.runtime_epoch_support import (
    SYNTHETIC_IDENTITY, bootstrap_operational_test_database, register_synthetic_scan_run,
)
from tests.test_integrated_signal_product import AT, ProductMarket
from tests.test_scanner_runner import _config
from tests.test_triggered_confirmed_telegram_delivery import FakeSender, _settings

pytestmark = pytest.mark.no_auto_epoch


def _scan(monkeypatch, direction, ratio, *, next_bar=False, invalid=None, warm_context=False):
    now = [AT - timedelta(seconds=1)]
    monkeypatch.setattr(binance_module, "default_capture_clock", lambda: now[0])

    class RefreshingMarket(ProductMarket):
        expired = False
        precision_lookups = 0
        exchange_info_requests = 0

        def response(self, request):
            if request.url.path == "/fapi/v1/exchangeInfo":
                self.exchange_info_requests += 1
            if request.url.path == "/fapi/v1/klines" and not self.expired:
                self.expired = True
                _expires, payload, acquired = self._exchange_info_snapshot
                self._exchange_info_snapshot = (0, payload, acquired)
                now[0] = self.as_of + timedelta(seconds=1)
            return super().response(request)

        async def get_price_precision(self, symbol):
            self.precision_lookups += 1
            return await super().get_price_precision(symbol)

        async def get_price_precision_snapshot(self, symbols):
            if invalid == "fetch_failure":
                raise TimeoutError("synthetic snapshot timeout")
            snapshot = await super().get_price_precision_snapshot(symbols)
            precision = snapshot["BTCUSDT"]
            changes = {
                "stale": {"received_at": self.as_of - timedelta(seconds=301)},
                "future": {"received_at": self.as_of + timedelta(seconds=1)},
                "identity": {"symbol": "ETHUSDT"},
            }
            if invalid in changes:
                snapshot["BTCUSDT"] = precision.model_copy(update=changes[invalid])
            elif invalid == "missing":
                snapshot = {}
            return snapshot

    client = RefreshingMarket(ratio, direction, next_bar=next_bar, room=True)
    now[0] = client.as_of - timedelta(seconds=1)
    config = _config(
        ["BTCUSDT"], strategy_version="selective-fib-mtf-v2", htf_timeframe="1d",
        structure_timeframe="4h", global_context_enabled=warm_context, decision_timestamp=None,
        btc_d_context_enabled=False, macro_event_context_enabled=False,
    )

    cache = MarketDataCache(enabled=True, now=lambda: client.as_of.timestamp()) if warm_context else None

    async def scan():
        try:
            return await ScannerRunner(
                exchange_client=client, market_data_cache=cache, clock=lambda: client.as_of,
                capture_clock=lambda: now[0],
            ).run(config)
        finally:
            await client.http.aclose()
            if cache is not None:
                await cache.aclose()

    return asyncio.run(scan()), client


@pytest.mark.parametrize("warm_context", [False, True])
@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("ratio", [D(".618"), D(".750"), D(".786")])
def test_refresh_during_real_scan_preserves_plan_risk_lifecycle_and_fake_delivery(
    tmp_path, monkeypatch, direction, ratio, warm_context,
):
    db = bootstrap_operational_test_database(tmp_path / "precision-product.db")
    sender = FakeSender()
    for index, next_bar in enumerate((False, True)):
        result, client = _scan(monkeypatch, direction, ratio, next_bar=next_bar, warm_context=warm_context)
        symbol = result.results[0]
        assert symbol.trade_idea is not None
        assert symbol.risk_decision.approved
        assert producer_acquisition_from_symbol_result(symbol) is not None
        assert client.precision_lookups == 0  # no post-cutoff precision request
        assert client.exchange_info_requests == 2  # preload plus optional metadata refresh
        assert "price_precision_snapshot" not in result.config.model_dump()
        for mode in symbol.valid_strategy_modes:
            diagnostic = symbol.strategy_diagnostics[mode]
            assert datetime.fromisoformat(diagnostic["instrument_precision"]["received_at"]) < result.config.decision_timestamp
            assert diagnostic["selective_fib"]["execution_eligible"]
            assert D(diagnostic["rr_to_tp2"]) >= 3
        run_id = f"precision-{index}"
        register_synthetic_scan_run(db, run_id)
        lifecycle = apply_lifecycle_to_run_result(
            result, database_path=db, scan_run_id=run_id,
            now=result.config.decision_timestamp.isoformat(), expected_identity=SYNTHETIC_IDENTITY,
        )
        expected = "CONFIRMED" if next_bar else "TRIGGERED"
        assert lifecycle.results[0].lifecycle_state.current_state.value == expected
        store_scan_result(db, lifecycle, run_id=f"stored-{index}", expected_identity=SYNTHETIC_IDENTITY)
        service = TelegramLifecycleDeliveryService(
            database_path=db, settings=_settings(), sender=sender,
            min_rr=result.config.min_rr, min_score_for_idea=result.config.min_score_for_idea,
            expected_identity=SYNTHETIC_IDENTITY,
        )
        delivery = asyncio.run(service.deliver_for_run(lifecycle, scan_run_id=run_id))
        assert delivery.sent == int(next_bar)
    repeated = asyncio.run(service.deliver_for_run(lifecycle, scan_run_id=run_id))
    assert repeated.sent == 0 and len(sender.messages) == 1
    assert "Risk warning:" in sender.messages[0] and "Invalidation:" in sender.messages[0]
    assert direction.upper() in sender.messages[0]


@pytest.mark.parametrize("invalid", ["stale", "future", "identity", "missing", "fetch_failure"])
def test_invalid_or_failed_snapshot_cannot_be_replaced_after_cutoff(monkeypatch, invalid):
    result, client = _scan(monkeypatch, "long", D(".618"), invalid=invalid)
    assert result.results[0].trade_idea is None
    assert not result.results[0].valid_strategy_modes
    assert client.precision_lookups == 0
    assert all(d["first_failed_gate"] == "instrument_precision_unverified"
               for d in result.results[0].strategy_diagnostics.values())


def test_bulk_precision_is_one_acquisition_and_isolates_malformed_contract(monkeypatch):
    calls = []
    monkeypatch.setattr(binance_module, "default_capture_clock", lambda: AT)

    def response(request):
        calls.append(request)
        return httpx.Response(200, json={"symbols": [
            {"symbol": "BTCUSDT", "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.10"}]},
            {"symbol": "BADUSDT", "filters": [{"filterType": "PRICE_FILTER", "tickSize": "bad"}]},
        ]})

    async def snapshot():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response), base_url="https://fixture.test") as http:
            client = BinanceFuturesClient(http_client=http)
            return await client.get_price_precision_snapshot(("BTCUSDT", "BADUSDT", "ABSENTUSDT"))

    values = asyncio.run(snapshot())
    assert len(calls) == 1
    assert values["BTCUSDT"].verified_for("binance", "BTCUSDT", AT)
    assert values["BADUSDT"] is None and values["ABSENTUSDT"] is None
