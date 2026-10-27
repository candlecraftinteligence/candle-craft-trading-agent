"""Synthetic regression fixtures only; no market-performance claims."""
from asyncio import run
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.analytics.pullback_zones import PullbackZone, analyze_pullback_zone
from app.analytics.selective_fibonacci import (
    ALLOWED_TARGETS, LEGACY_VERSION, SELECTIVE_VERSION, select_target, target_price,
)
from app.data.instrument_precision import InstrumentPrecision, parse_precision
from app.pipeline.scanner_runner import ScannerRunConfig, ScannerRunner, _strategy_diagnostics_for_setup
from app.strategies.context_fields import htf_value
from app.strategies.liquidity_grab_pullback import LiquidityGrabEngine, LiquidityGrabInput, LiquidityGrabSetup
from app.strategies.selective_timeframes import closed_selective_timeframes
from app.storage.run_provenance import collect_run_provenance
from app.backtesting.strategy_replay import ReplayConfig, StrategyReplayEngine, _strategy_input, _setup_condition_diagnostics
from app.analytics.edge_analytics import condition_key_from_diagnostics
from app.analytics.near_miss_intelligence import build_near_miss_intelligence
from app.formatters.telegram_signal_formatter import (
    SignalEdgeEvidence, TelegramSignalMessage, format_premium_public_signal_message,
)
from app.data.exchange_clients import BinanceFuturesClient, BybitLinearClient
from app.data.exceptions import ExchangeRateLimitError, ExchangeTimeoutError, ExchangeMalformedJSONError
from tests.test_pullback_zones import _bullish_valid_candles, _input
from tests.test_scanner_runner import FakeExchangeClient, _flat_candles, _config

D = Decimal
AT = datetime(2026, 10, 27, 12, tzinfo=UTC)


def _bar(index=0, *, open="130", close="132", low="129", high="133"):
    return SimpleNamespace(timestamp=index, open=D(open), close=D(close), low=D(low), high=D(high))


def _zone(ratio, direction="long", *, freshness="fresh", kind="OB"):
    start, end = (D("100"), D("200")) if direction == "long" else (D("200"), D("100"))
    price = target_price(direction, start, end, D(ratio))
    return PullbackZone(is_present=True, zone_type=kind, direction=direction,
                        low=price-D(".01"), high=price+D(".01"), freshness_status=freshness)


def _selection(ratio="0.618", direction="long", **overrides):
    start, end = (D("100"), D("200")) if direction == "long" else (D("200"), D("100"))
    bars = [_bar(0), _bar(1), _bar(2, open="140", close="142", low="139", high="143"),
            _bar(3, open="142", close="145", low="141", high="146")]
    if direction == "short":
        bars = [SimpleNamespace(timestamp=c.timestamp, open=300-c.open, close=300-c.close,
                                low=300-c.high, high=300-c.low) for c in bars]
    args = dict(direction=direction, start=start, end=end, zones=[_zone(ratio, direction)],
                tick_size=D(".01"), precision_source="/fapi/v1/exchangeInfo", timeframe="15m",
                candles=bars, sweep_index=0, bos_index=1, structure_confirmed=True,
                canonical_confirmation=True)
    args.update(overrides)
    return select_target(**args)


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("ratio", ALLOWED_TARGETS)
def test_decimal_targets_and_actual_intersection(direction, ratio):
    _, evidence = _selection(str(ratio), direction)
    expected = D("200") - D("100")*ratio if direction == "long" else D("100")+D("100")*ratio
    assert evidence.calculated_price == expected
    assert evidence.selected_ratio == ratio
    assert evidence.original_zone_low <= evidence.intersection_low <= evidence.original_zone_high
    assert evidence.intersection_low == evidence.intersection_high == expected
    assert evidence.strategy_version == SELECTIVE_VERSION
    assert evidence.direction == direction and evidence.timeframe == "15m"
    assert evidence.execution_eligible is (ratio != D("0.786"))


@pytest.mark.parametrize("ratio", ["0.382", "0.500", "0.650"])
@pytest.mark.parametrize("direction", ["long", "short"])
def test_old_ratios_cannot_be_new_entry_targets(ratio, direction):
    with pytest.raises(ValueError, match="unsupported_selective"):
        target_price(direction, D("100"), D("200"), D(ratio))
    price = D("200")-D("100")*D(ratio) if direction == "long" else D("100")+D("100")*D(ratio)
    zone = PullbackZone(is_present=True, zone_type="OB", direction=direction,
                        low=price-D(".001"), high=price+D(".001"), freshness_status="fresh")
    _, evidence = _selection(direction=direction, zones=[zone])
    assert not evidence.execution_eligible
    assert evidence.selected_ratio == "N/A"


@pytest.mark.parametrize("kind", ["OB", "FVG", "OB_FVG_OVERLAP"])
def test_real_structural_confluence(kind):
    _, evidence = _selection(zones=[_zone("0.618", kind=kind)])
    assert evidence.execution_eligible
    assert evidence.structural_confluence == (kind, "fresh")


def test_selects_structural_overlap_before_distance_and_shallower_on_tie():
    _, evidence = _selection(zones=[_zone("0.618"), _zone("0.750", kind="OB_FVG_OVERLAP")])
    assert evidence.selected_ratio == D("0.750")
    _, evidence = _selection(zones=[_zone("0.750"), _zone("0.618")])
    assert evidence.selected_ratio == D("0.618")


@pytest.mark.parametrize("override,reason", [
    ({"structure_confirmed": False}, "deep_structure_unconfirmed"),
    ({"canonical_confirmation": False}, "deep_canonical_confirmation_missing"),
    ({"zones": [_zone("0.750", freshness="partially_mitigated")]}, "deep_zone_not_fresh"),
    ({"candles": [_bar(0), _bar(1)]}, "missing_structural_acceptance"),
])
def test_deep_0750_requires_complete_structural_acceptance(override, reason):
    _, evidence = _selection("0.750", **override)
    assert not evidence.execution_eligible and reason in evidence.rejection_reasons


@pytest.mark.parametrize("direction", ["long", "short"])
def test_0786_weak_and_strong_reclaim_stay_watch_only(direction):
    boundary = D("121.4")
    bars = [_bar(0), _bar(1), _bar(2, open="122", close="123", low="121", high="124"),
            _bar(3, open="123", close="125", low="122", high="126")]
    if direction == "short":
        bars = [SimpleNamespace(timestamp=c.timestamp, open=300-c.open, close=300-c.close,
                                low=300-c.high, high=300-c.low) for c in bars]
    _, strong = _selection("0.786", direction, candles=bars)
    _, weak = _selection("0.786", direction, candles=bars[:3])
    assert strong.reclaim_status == "strong_reclaim"
    assert weak.reclaim_status == "weak_or_missing_reclaim"
    assert not strong.execution_eligible and not weak.execution_eligible
    assert "deep_0786_watch_only_lifecycle_stop_contract" in strong.rejection_reasons


def test_precision_is_verified_and_tolerance_cannot_expand_zone():
    _, evidence = _selection(tick_size=None)
    assert evidence.rejection_reasons == ("instrument_precision_unverified",)
    # A nearest tick inside the zone cannot rescue a mathematical target outside it.
    zone = _zone("0.618").model_copy(update={"low": D("138.201"), "high": D("138.21")})
    _, evidence = _selection(zones=[zone])
    assert not evidence.execution_eligible
    _, evidence = _selection(tick_size=D("3"), zones=[_zone("0.618").model_copy(update={"low": D("137"), "high": D("140")})])
    assert "precision_exceeds_policy_tolerance" in evidence.rejection_reasons
    assert evidence.price_tolerance == D(".01")


def _pullback(ratio="0.618", direction="long", **overrides):
    candles = _bullish_valid_candles()
    price = D("145")-D("45")*D(ratio)
    candles[7].update(open=price+D(".2"), close=price-D(".2"), low=price-D(".4"), high=price+D(".4"))
    candles[9]["low"] = price-D(".4")
    candles.extend([dict(timestamp=10, open=D("130"), close=D("132"), low=D("129"), high=D("133"), volume=D("100")),
                    dict(timestamp=11, open=D("132"), close=D("135"), low=D("131"), high=D("136"), volume=D("100"))])
    if direction == "short":
        candles = [{**c, "open": 250-c["open"], "close": 250-c["close"],
                    "low": 250-c["high"], "high": 250-c["low"]} for c in candles]
    data = _input(candles, direction=direction).model_copy(update=dict(
        strategy_version=SELECTIVE_VERSION, precision_source="/fapi/v1/exchangeInfo",
        structure_confirmed=True, canonical_confirmation=True, confirmation_timeframe="15m",
        calculation_timeframe="15m", **overrides))
    return data


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("ratio", ["0.618", "0.750", "0.786"])
def test_pullback_plan_integration_at_all_targets(ratio, direction):
    result = analyze_pullback_zone(_pullback(ratio, direction))
    if ratio == "0.786":
        assert not result.valid and "deep_0786_watch_only_lifecycle_stop_contract" in result.selective_fib.rejection_reasons
    else:
        assert result.valid, result.pullback_failure_reason
        assert result.trade_plan_integrity == "PASS"
        assert result.rr_to_tp2 >= D("2.5")
        assert result.entry == result.selective_fib.execution_price
        assert result.entry_low == result.entry_high == result.entry


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("close,gate", [("108", "body_acceptance_failure"), ("99", "structural_breakdown")])
def test_invalidated_deep_setups_remain_rejected(direction, close, gate):
    data = _pullback("0.750", direction)
    candle = dict(timestamp=12, open=D("112"), close=D(close), low=min(D("98"), D(close)), high=D("113"), volume=D("100"))
    if direction == "short":
        candle = {**candle, "open": 250-candle["open"], "close": 250-candle["close"],
                  "low": 250-candle["high"], "high": 250-candle["low"]}
    bars = list(data.candles_15m)+[candle]
    result = analyze_pullback_zone(data.model_copy(update={"candles_15m": bars, "candles_5m": bars, "latest_price": candle["close"]}))
    assert not result.valid and result.first_failed_gate == gate
    assert not result.selective_fib.execution_eligible


def test_rr_and_trade_plan_gates_not_lowered():
    result = analyze_pullback_zone(_pullback(minimum_rr=D("100")))
    assert not result.valid and result.first_failed_gate == "rr_below_minimum"
    assert "rr_below_minimum" in result.selective_fib.rejection_reasons
    # A support/resistance target beyond TP2 is not repaired to pass RR.
    result = analyze_pullback_zone(_pullback(user_resistance_levels=[D("999")]))
    assert not result.valid and result.first_failed_gate == "trade_plan_integrity"


class GenuineCandleClient(FakeExchangeClient):
    async def get_klines(self, symbol, interval, limit):
        rows = await super().get_klines(symbol, interval, limit)
        return [{**row, "exchange": "binance", "symbol": symbol, "interval": interval} for row in rows]

    async def get_price_precision(self, symbol):
        return InstrumentPrecision(exchange="binance", symbol=symbol, tick_size=D(".01"),
            source_endpoint="/fapi/v1/exchangeInfo", received_at=datetime.now(UTC))


def _new_config(**kwargs):
    return _config(["BTCUSDT"], strategy_version=SELECTIVE_VERSION, htf_timeframe="1d",
                   decision_timestamp=AT, **kwargs)


def _genuine_input():
    client = GenuineCandleClient({"BTCUSDT": _flat_candles()}, candle_as_of=AT)
    bars = {tf: run(client.get_klines("BTCUSDT", tf, 80)) for tf in ("1d", "12h", "2h", "15m")}
    return LiquidityGrabInput(symbol="BTCUSDT", strategy_version=SELECTIVE_VERSION,
        exchange="binance", decision_timestamp=AT, htf_timeframe="1d", candles_1d=bars["1d"],
        candles_12h=bars["12h"], structure_candles=bars["2h"], structure_analysis_required=True,
        candles_15m=bars["15m"])


def test_genuine_hierarchy_closed_data_and_no_2d_alias():
    data = _genuine_input()
    output = closed_selective_timeframes(data)
    assert tuple(output) == ("1d", "12h", "2h", "15m")
    result = LiquidityGrabEngine().analyze(data).swing
    assert result.htf_timeframe == "1d" and result.candles_1d_count == 80
    assert result.candles_2d_count == 0 and result.htf_2d_trend == "N/A"
    assert "candles_2d: N/A" not in result.missing_data
    assert result.strategy_version == SELECTIVE_VERSION


@pytest.mark.parametrize("field", ["candles_1d", "candles_12h", "structure_candles", "candles_15m"])
def test_missing_or_stale_timeframes_fail_closed(field):
    data = _genuine_input()
    for value in (None, getattr(data, field)[:-1]):
        with pytest.raises(ValueError):
            closed_selective_timeframes(data.model_copy(update={field: value}))


def test_incomplete_daily_candle_never_enters_context_and_mislabeled_source_rejected():
    data = _genuine_input()
    today = dict(data.candles_1d[-1], timestamp=int(AT.replace(hour=0).timestamp()*1000), close=D("1000000"), high=D("1000001"))
    output = closed_selective_timeframes(data.model_copy(update={"candles_1d": [*data.candles_1d, today]}))
    assert today not in output["1d"] and output["1d"] == tuple(data.candles_1d)
    wrong = [{**c, "interval": "2d"} for c in data.candles_1d]
    with pytest.raises(ValueError, match="source_identity"):
        closed_selective_timeframes(data.model_copy(update={"candles_1d": wrong}))
    shifted = [{**c, "timestamp": c["timestamp"]+60_000} for c in data.candles_1d]
    with pytest.raises(ValueError, match="utc_alignment"):
        closed_selective_timeframes(data.model_copy(update={"candles_1d": shifted}))


def test_scanner_acquires_only_required_native_timeframes():
    client = GenuineCandleClient({"BTCUSDT": _flat_candles()}, candle_as_of=AT)
    result = run(ScannerRunner(exchange_client=client).run(_new_config()))
    assert {tf for _, tf in client.requested_klines} == {"1d", "12h", "2h", "15m"}
    diag = result.results[0].strategy_diagnostics["swing"]
    assert diag["strategy_version"] == SELECTIVE_VERSION
    assert diag["htf_1d_context_source"] == "exchange_closed_1d"
    assert diag["candles_2d_count"] == 0
    assert not result.results[0].valid_strategy_modes


def test_new_defaults_explicit_legacy_and_prospective_version():
    config = ScannerRunConfig(symbols=["BTCUSDT"], exchange="binance", account_equity=10000, risk_per_trade_pct=1)
    assert config.strategy_version == SELECTIVE_VERSION and config.htf_timeframe == "1d"
    # Configuration construction must not block existing lifecycle owners.
    invalid = ScannerRunConfig(**{**config.model_dump(), "htf_timeframe": "2d"})
    assert invalid.htf_timeframe == "2d"
    with pytest.raises(ValueError, match="hierarchy_mismatch"):
        closed_selective_timeframes(_genuine_input().model_copy(update={"htf_timeframe": "2d"}))
    legacy = _config(["BTCUSDT"])
    assert legacy.strategy_version == LEGACY_VERSION and legacy.htf_timeframe == "2d"
    assert htf_value({"htf_timeframe": "2d", "htf_2d_trend": "bearish", "htf_1d_trend": "bullish"}) == "bearish"
    assert htf_value({"htf_timeframe": "1d", "htf_2d_trend": "bearish"}) == "N/A"
    provenance = collect_run_provenance(scanner_config=config)
    assert provenance["versions"]["strategy_version"] == {"status": "captured", "value": SELECTIVE_VERSION}


@pytest.mark.parametrize("exchange,payload", [
    ("binance", {"symbols": [{"symbol": "BTCUSDT", "filters": [{"filterType": "PRICE_FILTER", "tickSize": "0.10"}]}]}),
    ("bybit", {"retCode": 0, "result": {"category": "linear", "list": [{"symbol": "BTCUSDT", "priceFilter": {"tickSize": "0.10"}}]}}),
])
def test_exchange_precision_source_identity_freshness(exchange, payload):
    value = parse_precision(payload, exchange=exchange, symbol="BTCUSDT")
    assert value.tick_size == D(".10") and value.verified_for(exchange, "BTCUSDT", value.received_at)
    assert not value.verified_for(exchange, "ETHUSDT", value.received_at)
    assert not value.verified_for(exchange, "BTCUSDT", value.received_at+timedelta(seconds=301))
    with pytest.raises(ValueError):
        parse_precision(payload, exchange=exchange, symbol="OTHER")


def test_public_formatter_uses_selected_target_and_keeps_risk_and_invalidation():
    message = TelegramSignalMessage(symbol="BTCUSDT", direction="long", entry_low=D("138.2"),
        entry_high=D("138.2"), stop_loss=D("99"), tp1=D("220"), tp2=D("250"), tp3=D("300"),
        edge_evidence=SignalEdgeEvidence(strategy_version=SELECTIVE_VERSION,
            selected_fib_ratio=D(".618"), selected_zone_type="OB", fib_aligned=True))
    output = format_premium_public_signal_message(message)
    assert "Fib 0.618" in output
    assert "Risk warning" in output and "SL 99" in output
    assert "0.382" not in output


def test_deep_watch_explanation_cannot_offer_automatic_activation():
    _, evidence = _selection("0.786")
    note = build_near_miss_intelligence(failed_gate=evidence.rejection_reasons[0], diagnostics={
        "strategy_version": SELECTIVE_VERSION, "selective_fib": evidence.model_dump(mode="json"),
        "execution_sweep_status": "passed", "confirmation_structure_shift_status": "passed"})
    assert "WATCH-only" in note.activation_hint and "cannot activate" in note.activation_hint
    assert "invalidates" in note.invalidation_hint


@pytest.mark.parametrize("client_type", [BinanceFuturesClient, BybitLinearClient])
def test_public_precision_client_uses_exact_exchange_filter(client_type):
    requests = []
    def handler(request):
        requests.append(request)
        if client_type is BinanceFuturesClient:
            return httpx.Response(200, json={"symbols": [{"symbol": "BTCUSDT", "filters": [
                {"filterType": "LOT_SIZE", "stepSize": "999"},
                {"filterType": "PRICE_FILTER", "tickSize": "0.10"}]}]})
        return httpx.Response(200, json={"retCode": 0, "result": {"category": "linear", "list": [
            {"symbol": "BTCUSDT", "priceFilter": {"tickSize": "0.10"}, "lotSizeFilter": {"qtyStep": "999"}}]}})
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://exchange.test") as transport:
            client = client_type(http_client=transport)
            return await client.get_price_precision("BTCUSDT")
    value = run(exercise())
    assert value.tick_size == D(".10")
    assert all(request.method == "GET" and "signature" not in request.url.params for request in requests)


@pytest.mark.parametrize("failure", ["rate_limit", "timeout", "bad_json", "bad_identity"])
def test_precision_failures_are_rejected(failure):
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        if failure == "rate_limit":
            return httpx.Response(429, headers={"Retry-After": "0"})
        if failure == "bad_json":
            return httpx.Response(200, content=b"invalid")
        return httpx.Response(200, json={"symbols": []})
    async def exercise():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://exchange.test") as transport:
            client = BinanceFuturesClient(http_client=transport, retry_attempts=1)
            return await client.get_price_precision("BTCUSDT")
    expected = {"rate_limit": ExchangeRateLimitError, "timeout": ExchangeTimeoutError,
                "bad_json": ExchangeMalformedJSONError, "bad_identity": ValueError}[failure]
    with pytest.raises(expected):
        run(exercise())


def test_replay_version_and_precision_are_causal_and_separate_from_legacy():
    assert ReplayConfig().strategy_version == LEGACY_VERSION
    assert ReplayConfig().htf_timeframe == "2d"
    data = _genuine_input()
    candles = {"1d": data.candles_1d, "12h": data.candles_12h,
               "2h": data.structure_candles, "15m": data.candles_15m}
    config = ReplayConfig(strategy_version=SELECTIVE_VERSION, htf_timeframe="1d", replay_candles=80)
    future = InstrumentPrecision(exchange="binance", symbol="BTCUSDT", tick_size=D(".01"),
        source_endpoint="/fapi/v1/exchangeInfo", received_at=AT+timedelta(seconds=1))
    context = dict(exchange="binance", decision_timestamp=AT, instrument_precision=future)
    invalid = _strategy_input(symbol="BTCUSDT", candles_by_timeframe=candles, config=config,
                              current_price=D("100"), timeframe_context=context)
    assert invalid["tick_size"] == "N/A" and invalid["precision_source"] == "N/A"
    context["instrument_precision"] = future.model_copy(update={"received_at": AT-timedelta(seconds=1)})
    valid = _strategy_input(symbol="BTCUSDT", candles_by_timeframe=candles, config=config,
                            current_price=D("100"), timeframe_context=context)
    assert valid["tick_size"] == D(".01") and valid["strategy_version"] == SELECTIVE_VERSION
    summary = StrategyReplayEngine().run({"BTCUSDT": candles}, config,
        timeframe_context_by_symbol={"BTCUSDT": {"exchange": "binance"}})
    assert summary.strategy_version == SELECTIVE_VERSION and summary.stats.total_setups == 0


@pytest.mark.parametrize("version,timeframe,expected", [
    (SELECTIVE_VERSION, "1d", "aligned"), (LEGACY_VERSION, "2d", "conflicting"),
])
def test_replay_conditions_use_the_named_htf_without_relabeling(version, timeframe, expected):
    setup = LiquidityGrabSetup(mode="swing", bias="long", strategy_version=version,
        htf_timeframe=timeframe, htf_1d_trend="bullish", htf_2d_trend="bearish")
    diagnostics = _setup_condition_diagnostics(setup)
    condition = condition_key_from_diagnostics(symbol="BTCUSDT", mode="swing", diagnostics=diagnostics)
    assert condition.htf_direction_alignment == expected
