"""Deterministic synthetic markets through the real scanner; no invented gates."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D
import asyncio
import sqlite3

import pytest
import httpx

from app.analytics.selective_fibonacci import SELECTIVE_VERSION, ALLOWED_TARGETS
from app.core.trade_plan_integrity import validate_public_trade_plan
from app.data.dtos import NA
from app.data.exchange_clients import BinanceFuturesClient
from app.pipeline.scanner_runner import ScannerRunner
from tests.test_liquidity_grab_pullback import _full_bullish_setup_candles
from tests.test_scanner_runner import _config, _time_aligned_candles, _INTERVAL_MS
from app.lifecycle.service import apply_lifecycle_to_run_result
from app.storage.repositories import store_scan_result
from tests.runtime_epoch_support import bootstrap_operational_test_database, register_synthetic_scan_run, SYNTHETIC_IDENTITY

AT = datetime(2026, 10, 27, 12, tzinfo=UTC)
pytestmark = pytest.mark.no_auto_epoch


def _cmc_membership():
    from tests.test_cmc_u100_provider import _row, _binding, _fetch
    from tests.test_strict_market_cap_universe import _ticker, _contract
    from app.cmc.collection import IdentityManifest
    from app.cmc.universe import normalize_rankings
    from app.universe.symbol_universe import build_symbol_universe_from_market_caps, CMC_MARKET_CAP_SOURCE
    rows=[_row(1,1,"BTC"),*[_row(100+i,2+i,f"FIXTURE{i}") for i in range(99)]]
    bindings=[]
    for row in rows:
        row["last_updated"]=AT.isoformat()
        row["quote"][0]["last_updated"]=AT.isoformat()
        bindings.append(_binding(row).model_copy(update={"acquired_at":AT-timedelta(days=1),
            "reviewed_at":AT-timedelta(hours=2),"approved_at":AT-timedelta(hours=1),"expires_at":AT+timedelta(days=1)}))
    snapshot=normalize_rankings(_fetch(rows,acquired_at=AT,raw={"status":{"timestamp":AT.isoformat()}}),
        IdentityManifest(bindings=tuple(bindings)),as_of=AT)
    universe=build_symbol_universe_from_market_caps([_ticker("BTC")],snapshot.assets,exchange_info={"symbols":[_contract("BTC")]},
        universe_size=100,generated_at=AT.isoformat(),ranking_source=CMC_MARKET_CAP_SOURCE,verified_mappings=snapshot.mappings)
    assert universe.resolved_symbols == ("BTCUSDT",)
    assert universe.diagnostics["potential_member_count"] == 100
    return universe


def _market(ratio, direction, *, room=False):
    pattern = _full_bullish_setup_candles()
    level = D("114")-D("29")*ratio
    pattern[33].update(open=level+D(".2"), close=level-D(".2"), low=level-D(".4"), high=level+D(".4"))
    # No pre-contact mitigation: displacement then a causal closed reaction.
    pattern[35]["low"] = level+D(".5")
    pattern += [dict(timestamp=36, open=D("108"), high=D("113"), low=level-D(".1"), close=D("111"), volume=D("200"))]
    execution = [dict(timestamp=i, open=D("100"), high=D("105"), low=D("95"), close=D("100"), volume=D("100")) for i in range(184)]
    execution += [{**c, "timestamp":184+i} for i,c in enumerate(pattern)]
    higher = [dict(timestamp=i, open=D("80")+D(i)/10, close=D("80")+D(i)/10+D(".05"),
                   low=D("79")+D(i)/10, high=D("81")+D(i)/10, volume=D("100")) for i in range(220)]
    for index, high in ((130,"240"),(140,"210"),(150,"180")) if room else ((130,"210"),(140,"180"),(150,"150")):
        higher[index]["high"] = D(high)
    frames = {"15m": execution, "4h": execution, "1d": higher, "12h": higher}
    if direction == "short":
        frames = {tf:[{**c,"open":300-c["open"],"close":300-c["close"],"low":300-c["high"],"high":300-c["low"]} for c in rows] for tf,rows in frames.items()}
    return {tf:[{**c, **{key:c[key]+1000 for key in ("open","close","high","low")}} for c in rows] for tf,rows in frames.items()}


class ProductMarket(BinanceFuturesClient):
    def __init__(self, ratio, direction, *, next_bar=False, room=False, scenario="valid"):
        self.frames = _market(ratio, direction,room=room)
        self.scenario=scenario
        self.direction=direction
        self.as_of=AT+timedelta(minutes=15) if next_bar else AT
        self.next_bar=next_bar
        if scenario == "before_contact":
            self.as_of=AT-timedelta(minutes=15)
        if next_bar:
            previous=self.frames["15m"][-1]
            self.frames["15m"]=[*self.frames["15m"],{**previous,"timestamp":previous["timestamp"]+1}]
        if scenario == "wick_only":
            bar=self.frames["15m"][-1]
            bar["open"]=bar["close"]+(D("1") if direction == "long" else D("-1"))
        if scenario == "invalidated":
            bar=self.frames["15m"][-1]
            bar.update(close=D("1080") if direction == "long" else D("1220"),
                low=D("1079") if direction == "long" else bar["low"],
                high=D("1221") if direction == "short" else bar["high"])
        if scenario == "weak_volume":
            self.frames["15m"][-7]["volume"]=D("100")
        if scenario == "structure_unconfirmed":
            for bar in self.frames["4h"][-2:]:
                bar["close"]=D("1100") if direction == "long" else D("1200")
        if scenario == "low_rr":
            for tf in ("1d","12h"):
                for index,level in ((130,125),(140,115),(150,110)):
                    self.frames[tf][index]["high" if direction == "long" else "low"]=D(1000+level if direction == "long" else 1300-level)
        self.requested_klines=[]
        self.http=httpx.AsyncClient(transport=httpx.MockTransport(self.response),base_url="https://fixture.test")
        super().__init__(http_client=self.http,retry_attempts=1)

    def response(self, request):
        if request.url.path == "/fapi/v1/klines":
            if self.scenario == "provider_failure":
                return httpx.Response(503,json={"msg":"synthetic structural provider outage"})
            symbol,interval=request.url.params["symbol"],request.url.params["interval"]
            self.requested_klines.append((symbol,interval))
            anchor=self.as_of if self.next_bar and interval == "15m" else AT
            rows=_time_aligned_candles(self.frames[interval][-int(request.url.params["limit"]):],interval,as_of=anchor)
            rows=[c for c in rows if c["timestamp"]+_INTERVAL_MS[interval] <= int(self.as_of.timestamp()*1000)]
            if self.scenario == "stale_daily" and interval == "1d":
                rows=rows[:-1]
            return httpx.Response(200,json=[[c["timestamp"],*[str(c[k]) for k in ("open","high","low","close","volume")],
                c["timestamp"]+_INTERVAL_MS[interval]-1] for c in rows])
        if request.url.path == "/fapi/v1/ticker/24hr":
            return httpx.Response(200,json={"symbol":"BTCUSDT","lastPrice":str(self.frames["15m"][-1]["close"]),
                "priceChangePercent":"1","quoteVolume":"100000000","volume":"100000","closeTime":int(self.as_of.timestamp()*1000)})
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200,json={"symbols":[{"symbol":"BTCUSDT","pair":"BTCUSDT","baseAsset":"BTC","quoteAsset":"USDT",
                "marginAsset":"USDT","contractType":"PERPETUAL","status":"TRADING","filters":[{"filterType":"PRICE_FILTER","tickSize":"0.001"}]}]})
        # Explicit optional outage, not synthetic numerical confirmation.
        return httpx.Response(503,json={"msg":"synthetic optional provider outage"})


def _scan(ratio, direction, *, next_bar=False, room=False, scenario="valid", universe=None, **config_overrides):
    client=ProductMarket(ratio,direction,next_bar=next_bar,room=room,scenario=scenario)
    config=_config(list(universe.resolved_symbols) if universe else ["BTCUSDT"], strategy_version=SELECTIVE_VERSION, htf_timeframe="1d", structure_timeframe="4h",
                   global_context_enabled=False, decision_timestamp=client.as_of, **config_overrides)
    async def scan():
        try:
            await client.get_instrument_metadata("BTCUSDT",capture_clock=lambda:client.as_of-timedelta(seconds=1))
            return await ScannerRunner(exchange_client=client,capture_clock=lambda:client.as_of).run(config)
        finally:
            await client.http.aclose()
    result=asyncio.run(scan())
    return result,client


@pytest.mark.parametrize("direction",["long","short"])
@pytest.mark.parametrize("ratio",ALLOWED_TARGETS)
def test_real_scanner_builds_qualified_plan(ratio,direction):
    result,client=_scan(ratio,direction)
    symbol=result.results[0]
    assert symbol.trade_idea is not None, {mode:{k:v for k,v in diag.items() if k in {
        "first_failed_gate","pullback_failure_reason","structure_layer_status","structure_layer_direction",
        "structure_layer_reason","selected_fib_ratio","selective_fib_rejection_reasons","rr_to_tp2","target_integrity_reason"}}
        for mode,diag in symbol.strategy_diagnostics.items()}
    assert symbol.valid_strategy_modes
    assert {tf for _symbol,tf in client.requested_klines} == {"1d","12h","4h","15m"}
    assert symbol.risk_decision.approved
    for mode in symbol.valid_strategy_modes:
        setup=getattr(symbol.strategy_results[mode],mode)
        plan=setup.pullback_zone
        assert symbol.trade_idea.execution_cost == plan.execution_cost
        assert plan.selective_fib.selected_ratio == ratio
        assert plan.selective_fib.level_touched and plan.selective_fib.execution_eligible
        assert setup.status == "Entry confirmed"
        assert (plan.stop < D("1085") if direction == "long" else plan.stop > D("1215"))
        validated=validate_public_trade_plan(direction=direction,entry_low=plan.entry_low,entry_high=plan.entry_high,
            stop_loss=plan.stop,tp1=plan.tp1,tp2=plan.tp2,tp3=plan.tp3,execution_cost=plan.execution_cost)
        assert validated.valid and validated.rr >= 3
        assert abs(validated.rr-plan.rr_to_tp2) < D(".00000001")
        from app.formatters.telegram_signal_formatter import format_rr
        assert f"TP2 RR {format_rr(plan.rr_to_tp2)}" in symbol.alert_result.formatted_message
    assert all(metric["status"] == "MISSING" for metric in symbol.derivatives_supporting_context["metrics"].values())
    assert symbol.derivatives_supporting_context["validated_confirmation"] is False


@pytest.mark.parametrize("direction",["long","short"])
@pytest.mark.parametrize("ratio",ALLOWED_TARGETS)
def test_real_plan_persists_and_reaches_fake_transport_without_duplicate(tmp_path,ratio,direction):
    from tests.test_triggered_confirmed_telegram_delivery import _settings, FakeSender
    from app.alerts.telegram_lifecycle import TelegramLifecycleDeliveryService
    universe=_cmc_membership()
    result,_client=_scan(ratio,direction,room=True,universe=universe)
    db=bootstrap_operational_test_database(tmp_path/"product.db")
    sender=FakeSender()
    def service():
        return TelegramLifecycleDeliveryService(database_path=db,settings=_settings(),sender=sender,
            min_rr=result.config.min_rr,min_score_for_idea=result.config.min_score_for_idea,
            expected_identity=SYNTHETIC_IDENTITY)
    register_synthetic_scan_run(db,"product-1")
    lifecycle=apply_lifecycle_to_run_result(result,database_path=db,scan_run_id="product-1",
        now=AT.isoformat(),expected_identity=SYNTHETIC_IDENTITY)
    symbol=lifecycle.results[0]
    assert symbol.lifecycle_state is not None
    assert symbol.lifecycle_state.current_state.value == "TRIGGERED"
    first_pending=asyncio.run(service().deliver_for_run(lifecycle,scan_run_id="product-1"))
    assert first_pending.sent == 0 and not sender.messages
    next_result,_client=_scan(ratio,direction,next_bar=True,room=True,universe=universe)
    register_synthetic_scan_run(db,"product-2")
    lifecycle=apply_lifecycle_to_run_result(next_result,database_path=db,scan_run_id="product-2",
        now=(AT+timedelta(minutes=15)).isoformat(),expected_identity=SYNTHETIC_IDENTITY)
    symbol=lifecycle.results[0]
    assert symbol.lifecycle_state.current_state.value == "CONFIRMED", (symbol.lifecycle_state, symbol.lifecycle_transition)
    store_scan_result(db,lifecycle,run_id="product-stored",expected_identity=SYNTHETIC_IDENTITY)
    first=asyncio.run(service().deliver_for_run(lifecycle,scan_run_id="product-2"))
    assert first.sent == 1, [(d.status,d.error_message,d.reason if hasattr(d,"reason") else None) for d in first.deliveries]
    repeat=asyncio.run(service().deliver_for_run(lifecycle,scan_run_id="product-2"))
    assert repeat.sent == 0 and len(sender.messages)==1
    text=sender.messages[0]
    for expected in ("BTCUSDT", direction.upper(), "SIGNAL CONFIRMED", "ENTRY", "SL", "TP1", "TP2", "TP3",
                     "TP2 RR", "Invalidation:", "hard stop on price contact", "Market bias:", "Funding N/A (Missing)",
                     "exchange fill are Unverified", "Risk warning:",f"Fib {ratio:.3f}"):
        assert expected in text
    assert "Filled" not in text and "SECURED" not in text
    from app.storage.scan_payloads import load_logical_scan_payload
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM scan_runs WHERE run_id='product-stored'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM symbol_results WHERE run_id='product-stored'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM public_alert_events WHERE lower(event_type)='signal_confirmed'").fetchone()[0] == 1
        assert connection.execute("SELECT COUNT(*) FROM telegram_alert_attempts WHERE alert_type='SIGNAL_CONFIRMED' AND telegram_status='sent'").fetchone()[0] == 1
        restored=load_logical_scan_payload(connection,"product-stored")
        assert restored["results"][0]["trade_idea"]["execution_cost"] == str(symbol.trade_idea.execution_cost)


@pytest.mark.parametrize("direction",["long","short"])
@pytest.mark.parametrize("scenario",["wick_only","invalidated","weak_volume","structure_unconfirmed","low_rr","stale_daily","provider_failure"])
def test_real_scanner_rejects_invalid_and_unconfirmed_markets(scenario,direction):
    result,_client=_scan(D(".786"),direction,scenario=scenario)
    symbol=result.results[0]
    assert symbol.trade_idea is None
    assert not symbol.valid_strategy_modes
    assert symbol.rejection_category != NA
    if scenario == "stale_daily": assert symbol.rejection_category == "DATA_STALE"
    if scenario == "provider_failure": assert symbol.rejection_category == "PROVIDER_FAILURE"


def test_configured_stricter_rr_is_preserved():
    result,_client=_scan(D(".618"),"long",min_rr=D("100"))
    assert result.results[0].trade_idea is None


def test_real_watch_waits_for_contact_before_trigger_and_confirmation(tmp_path):
    db=bootstrap_operational_test_database(tmp_path/"watch-progression.db")
    result,_client=_scan(D(".750"),"long",scenario="before_contact",room=True)
    assert result.results[0].trade_idea is None
    register_synthetic_scan_run(db,"watch-before")
    watching=apply_lifecycle_to_run_result(result,database_path=db,scan_run_id="watch-before",
        now=(AT-timedelta(minutes=15)).isoformat(),expected_identity=SYNTHETIC_IDENTITY)
    assert watching.results[0].lifecycle_state.current_state.value in {"WATCH","STALKING"}, watching.results[0].lifecycle_state
    owner=watching.results[0].lifecycle_state
    assert owner.confirmation_count == 0
    assert all(not getattr(result.results[0].strategy_results[mode],mode).pullback_zone.selective_fib.execution_eligible
               for mode in result.results[0].rejected_strategy_modes)
    for index,next_bar in enumerate((False,True)):
        run_id=f"watch-after-{index}"
        register_synthetic_scan_run(db,run_id)
        result,_client=_scan(D(".750"),"long",room=True,next_bar=next_bar)
        advanced=apply_lifecycle_to_run_result(result,database_path=db,scan_run_id=run_id,
            now=(AT+timedelta(minutes=15*index)).isoformat(),expected_identity=SYNTHETIC_IDENTITY)
        assert advanced.results[0].lifecycle_state.current_state.value == ("CONFIRMED" if next_bar else "TRIGGERED")
        assert advanced.results[0].lifecycle_state.lifecycle_id == owner.lifecycle_id
