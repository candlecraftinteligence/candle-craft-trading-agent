"""Synthetic evidence only; no provider calls, production state or Telegram."""
import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D

import httpx
import pytest

from app.analytics.derivatives_source_truth import evaluate_derivatives_source_truth
from app.analytics.selective_fibonacci import SELECTIVE_VERSION
from app.data.dtos import NA
from app.data.funding_cadence import attach_funding_cadence
from app.pipeline.causal_prefetch import bounded_prefetch
from app.pipeline.scanner_runner import ScannerRunner
from tests.test_derivatives_source_truth import CUTOFF, FUNDING_AT, acquired, binance_instrument, funding_payload, millis
from app.data.derivatives_source_evidence import describe_binance_funding_history
from tests.test_integrated_signal_product import ProductMarket, AT
from tests.test_scanner_runner import _config

pytestmark = pytest.mark.no_auto_epoch


def funding(at=CUTOFF, rate="0.0001", **row):
    payload = funding_payload(rate, FUNDING_AT)
    payload[0].update(row)
    return describe_binance_funding_history(requested_symbol="BTCUSDT", payload=payload,
        request_params={"symbol": "BTCUSDT", "limit": 3}, acquisition=acquired(at), capture_clock=None)


def test_independent_reads_of_identical_settled_event_are_not_provenance_conflicts():
    one, two = funding(CUTOFF-timedelta(seconds=2)), funding(CUTOFF-timedelta(seconds=1))
    result = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF,
        instrument=replace(binance_instrument(), funding_interval_ms=28800000), funding=one, funding_history=two)
    assert D(result.funding_rate) == D(".0001")
    assert "conflicting_provenance" not in result.reason_codes


@pytest.mark.parametrize("row", [{"fundingRate": ".0002"}, {"rateType": "Special"}, {"rateType": "predicted"}])
def test_conflicting_settled_or_predicted_funding_is_still_withheld(row):
    result = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF,
        instrument=replace(binance_instrument(), funding_interval_ms=28800000),
        funding=funding(CUTOFF-timedelta(seconds=2)), funding_history=funding(**row))
    assert result.funding_rate == NA


def test_explicit_and_scheduled_cadence_keep_source_provenance_and_conflicts():
    instrument = binance_instrument()
    schedule = {"symbol": "BTCUSDT", "time": millis(CUTOFF), "nextFundingTime": millis(FUNDING_AT+timedelta(hours=8))}
    result = attach_funding_cadence(instrument, {"funding_rate": funding()}, (CUTOFF, [], [schedule]))
    assert result.funding_interval_ms == 28800000
    assert result.funding_cadence_source_at == CUTOFF
    assert "premiumIndex" in result.funding_cadence_source
    conflict = attach_funding_cadence(instrument, {"funding_rate": funding()},
        (CUTOFF, [{"symbol": "BTCUSDT", "fundingIntervalHours": 4}], [schedule]))
    assert conflict.funding_interval_ms is None
    missing = attach_funding_cadence(instrument, {}, (CUTOFF, [], []))
    assert missing.funding_interval_ms is None
    assert missing.funding_cadence_source == "funding_cadence_not_reported"


@pytest.mark.parametrize("snapshot", ["bad", (None, [], []), (CUTOFF, {}, [])])
def test_bad_schedule_envelope_is_optional_and_never_invents_cadence(snapshot):
    result = attach_funding_cadence(binance_instrument(), {"funding_rate": funding()}, snapshot)
    assert result.funding_interval_ms is None
    assert result.funding_cadence_source == "malformed_funding_schedule"


def test_withheld_observations_keep_source_times_and_specific_admission_reasons():
    from app.analytics.derivatives_context import assess_native_context
    assessment, context = assess_native_context(evidence={"funding_rate": funding(CUTOFF+timedelta(seconds=1))},
        instrument=replace(binance_instrument(), funding_interval_ms=28800000),
        strategy_cutoff=CUTOFF, observed_at=CUTOFF+timedelta(seconds=2))
    withheld = context["strategy_inputs"]["funding_rate"]
    assert withheld["value"] == NA
    assert withheld["withheld_sources"][0]["acquired_at"] == (CUTOFF+timedelta(seconds=1)).isoformat()
    assert "acquisition_after_cutoff" in withheld["withheld_reason_counts"]
    assert context["metrics"]["funding_rate"]["status"] == "VERIFIED"


def test_prefetch_deadline_cancels_workers_and_never_fabricates_data():
    async def exercise():
        active, peak = 0, 0
        async def acquire(symbol):
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            try:
                if symbol != "fast":
                    await asyncio.Event().wait()
                return symbol
            finally:
                active -= 1
        values, diagnostics = await bounded_prefetch(["fast", "slow", "later"], acquire, timeout=.2, workers=2)
        assert values == {"fast": "fast"}
        assert active == 0 and peak == 2
        assert set(diagnostics["withheld"]) == {"slow", "later"}
    asyncio.run(exercise())


class NativeMarket(ProductMarket):
    funding_rate = ".0001"
    def response(self, request):
        p = request.url.path
        body = None
        if p == "/fapi/v1/fundingRate":
            body = [{"symbol": "BTCUSDT", "fundingRate": self.funding_rate, "fundingTime": millis(AT-timedelta(hours=1)), "rateType": "Regular"}]
        elif p == "/fapi/v1/fundingInfo":
            body = [{"symbol": "BTCUSDT", "fundingIntervalHours": 8}]
        elif p == "/fapi/v1/premiumIndex":
            body = [{"symbol": "BTCUSDT", "time": millis(AT), "nextFundingTime": millis(AT+timedelta(hours=7))}]
        elif p == "/fapi/v1/openInterest":
            body = {"symbol": "BTCUSDT", "openInterest": "101", "time": millis(AT)}
        elif p.endswith("openInterestHist"):
            body = [{"symbol": "BTCUSDT", "sumOpenInterest": value, "sumOpenInterestValue": str(D(value)*1100), "timestamp": millis(at)}
                for at, value in ((AT-timedelta(minutes=10), "99"), (AT-timedelta(minutes=5), "100"))]
        elif p.endswith("globalLongShortAccountRatio"):
            body = [{"symbol": "BTCUSDT", "longShortRatio": "1", "longAccount": ".5", "shortAccount": ".5", "timestamp": millis(AT-timedelta(seconds=1))}]
        return httpx.Response(200, json=body) if body is not None else super().response(request)


@pytest.mark.parametrize("rate,expect_plan", [(".0001", True), (".002", False)])
def test_real_scanner_prefetch_admits_native_values_and_preserves_derivatives_guard(rate, expect_plan):
    async def exercise():
        market = NativeMarket(D(".786"), "long")
        market.funding_rate = rate
        config = _config(["BTCUSDT"], strategy_version=SELECTIVE_VERSION, htf_timeframe="1d",
            structure_timeframe="4h", global_context_enabled=False, decision_timestamp=None)
        try:
            await market.get_instrument_metadata("BTCUSDT", capture_clock=lambda: AT)
            result = await ScannerRunner(exchange_client=market, clock=lambda: AT+timedelta(seconds=1),
                capture_clock=lambda: AT).run(config)
        finally:
            await market.http.aclose()
        symbol = result.results[0]
        assert result.runtime_stats.derivatives_prefetch["acquired"] == 1
        assert symbol.funding_rate == D(rate), symbol.derivatives_supporting_context
        assert symbol.open_interest == D("101"), str(symbol.derivatives_supporting_context["metrics"]["oi_change_pct"])
        assert symbol.derivatives_enrichment.open_interest_change_pct == D("1")
        assert symbol.derivatives_supporting_context["strategy_cutoff"] == (AT+timedelta(seconds=1)).isoformat()
        assert result.config.decision_timestamp == AT+timedelta(seconds=1)
        assert (symbol.trade_idea is not None) is expect_plan, symbol.rejection_reason
        if not expect_plan:
            assert symbol.derivatives_enrichment.funding_status == "extreme_positive"
            assert symbol.derivatives_enrichment.oi_direction == "rising"
    asyncio.run(exercise())


def test_supporting_flow_refreshes_at_final_assessment_without_moving_structural_clock():
    from app.context.models import ContextStatus
    from app.microstructure.models import MicrostructureFlowSnapshot
    class Flow:
        calls = 0
        def snapshot(self, symbol):
            self.calls += 1
            at = AT-timedelta(seconds=10) if self.calls == 1 else AT
            return MicrostructureFlowSnapshot.unavailable(symbol=symbol, reason="synthetic_fixture").model_copy(
                update={"status": ContextStatus.VERIFIED, "observed_at": at, "age_seconds": (AT-at).total_seconds()})
    async def exercise():
        market, flow = NativeMarket(D(".786"), "long"), Flow()
        config = _config(["BTCUSDT"], strategy_version=SELECTIVE_VERSION, htf_timeframe="1d",
            structure_timeframe="4h", global_context_enabled=False, decision_timestamp=AT,
            microstructure_flow_enabled=True)
        try:
            await market.get_instrument_metadata("BTCUSDT", capture_clock=lambda: AT)
            result = await ScannerRunner(exchange_client=market, microstructure_flow_service=flow,
                clock=lambda: AT, capture_clock=lambda: AT).run(config)
        finally:
            await market.http.aclose()
        context = result.results[0].derivatives_supporting_context
        assert flow.calls >= 3
        assert context["streams"]["aggregate_flow"]["status"] == "VERIFIED"
        assert context["streams"]["aggregate_flow"]["source_at"] == AT.isoformat()
        assert result.config.decision_timestamp == AT
    asyncio.run(exercise())
