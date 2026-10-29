"""Native source context is causal, optional and never a structural confirmation."""
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal as D
import asyncio

import pytest

from app.analytics.derivatives_context import assess_native_context, admitted_strategy_values, observed_stream_context, observed_spread
from app.analytics.derivatives_source_truth import ENFORCEMENT
from app.context.models import ContextStatus
from app.data.derivatives_source_evidence import describe_binance_open_interest, describe_binance_open_interest_history, describe_binance_funding_history
from app.data.dtos import NA
from app.microstructure.order_book_models import OrderBookLiquiditySnapshot
from tests.test_derivatives_source_truth import CUTOFF, PREVIOUS, FUNDING_AT, acquired, binance_instrument, millis, oi_history_payload, funding_payload

pytestmark = pytest.mark.no_auto_epoch


def _source(acquisition_at=CUTOFF):
    acquisition = acquired(acquisition_at)
    evidence = {
        "open_interest": describe_binance_open_interest(requested_symbol="BTCUSDT",
            payload={"symbol": "BTCUSDT", "openInterest": "110", "time": millis(CUTOFF)},
            request_params={"symbol": "BTCUSDT"}, acquisition=acquisition, capture_clock=None),
        "open_interest_history": describe_binance_open_interest_history(requested_symbol="BTCUSDT",
            payload=oi_history_payload([(PREVIOUS, "100", None), (CUTOFF, "110", None)]),
            request_params={"symbol": "BTCUSDT", "period": "5m", "limit": 30}, acquisition=acquisition, capture_clock=None),
        "funding_history": describe_binance_funding_history(requested_symbol="BTCUSDT",
            payload=funding_payload("0.0001", FUNDING_AT), request_params={"symbol": "BTCUSDT", "limit": 1}, acquisition=acquisition, capture_clock=None),
    }
    # Explicit synthetic cadence evidence, not a claim about every Binance contract.
    instrument = replace(binance_instrument(acquisition_at), funding_interval_ms=8*60*60*1000)
    return evidence, instrument


def test_native_context_reuses_verified_units_identity_cadence_and_acquisition():
    evidence, instrument = _source()
    assessment, context = assess_native_context(evidence=evidence, instrument=instrument,
        strategy_cutoff=CUTOFF, observed_at=CUTOFF)
    admitted = admitted_strategy_values(assessment)
    assert admitted["funding"]["funding_rate"] == D("0.0001")
    assert admitted["open_interest"]["open_interest"] == D("110")
    assert admitted["previous_open_interest"] == D("100")
    assert context["metrics"]["oi_change_pct"]["status"] == "VERIFIED"
    assert D(context["metrics"]["oi_change_pct"]["value"]) == 10
    assert context["metrics"]["funding_rate"]["sources"][0]["acquired_at"] == CUTOFF.isoformat()
    assert context["validated_confirmation"] is False and ENFORCEMENT == "not_active"


def test_later_supporting_observation_cannot_be_backdated_into_strategy():
    later = CUTOFF+timedelta(seconds=10)
    evidence, instrument = _source(later)
    assessment, context = assess_native_context(evidence=evidence, instrument=instrument,
        strategy_cutoff=CUTOFF, observed_at=later)
    assert admitted_strategy_values(assessment)["funding"] is None
    assert admitted_strategy_values(assessment)["open_interest"] is None
    assert context["strategy_cutoff"] == CUTOFF.isoformat()
    assert context["observed_at"] == later.isoformat()
    assert context["metrics"]["oi_change_pct"]["status"] == "VERIFIED"
    assert context["validated_confirmation"] is False


def test_missing_optional_derivatives_remain_missing_not_zero_or_confirmed():
    assessment, context = assess_native_context(evidence={}, instrument=None,
        strategy_cutoff=CUTOFF, observed_at=CUTOFF)
    assert all(metric["status"] == "MISSING" and metric["value"] == NA for metric in context["metrics"].values())
    assert admitted_strategy_values(assessment)["funding"] is None
    assert admitted_strategy_values(assessment)["open_interest"] is None
    assert admitted_strategy_values(assessment)["long_short_ratio"] == NA


def test_stale_native_data_cannot_be_promoted_by_fresh_assessment():
    evidence, instrument = _source()
    assessment, context = assess_native_context(evidence=evidence, instrument=instrument,
        strategy_cutoff=CUTOFF+timedelta(days=2), observed_at=CUTOFF+timedelta(days=2))
    assert all(metric["status"] != "VERIFIED" and metric["value"] == NA for metric in context["metrics"].values())
    assert admitted_strategy_values(assessment)["funding"] is None
    assert admitted_strategy_values(assessment)["open_interest"] is None


@pytest.mark.parametrize("case,expected", [("fresh", "VERIFIED"), ("stale", "STALE"),
    ("future", "UNVERIFIED"), ("unsynchronized", "UNVERIFIED"), ("wrong_symbol", "UNVERIFIED")])
def test_stream_context_rechecks_age_synchronization_and_identity(case, expected):
    at = CUTOFF-timedelta(seconds=10) if case == "stale" else CUTOFF+timedelta(seconds=1) if case == "future" else CUTOFF
    snapshot = OrderBookLiquiditySnapshot(symbol="ETHUSDT" if case == "wrong_symbol" else "BTCUSDT",
        status=ContextStatus.VERIFIED, synchronized=case != "unsynchronized", observed_at=at,
        best_bid=D("100"), best_ask=D("100.1"), spread_absolute=D(".1"))
    result = observed_stream_context(snapshot, as_of=CUTOFF, stale_seconds=5,
        limitation="Visible liquidity may cancel; not a guarantee.", expected_symbol="BTCUSDT")
    assert result["status"] == expected
    assert snapshot.usage == "research_only"
    if expected != "VERIFIED": assert "snapshot" not in result


def test_prospective_scanner_does_not_trust_numeric_only_adapter_derivatives():
    from app.pipeline.scanner_runner import ScannerRunner
    from tests.test_scanner_runner import FakeExchangeClient, _flat_candles, _config
    from app.analytics.selective_fibonacci import SELECTIVE_VERSION
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()})
    config = _config(["BTCUSDT"], strategy_version=SELECTIVE_VERSION, htf_timeframe="1d",
                     structure_timeframe="4h", decision_timestamp=CUTOFF)
    optional = asyncio.run(ScannerRunner(exchange_client=client, capture_clock=lambda: CUTOFF)._fetch_optional_market_data(client, "BTCUSDT", config))
    assert optional.funding is None and optional.open_interest is None
    assert optional.supporting_context["metrics"]["funding_rate"]["status"] == "MISSING"
    assert optional.derivatives_source_truth_requested is False


@pytest.mark.parametrize("status,expected", [(ContextStatus.UNAVAILABLE, "MISSING"), (ContextStatus.ERROR, "ERROR"), (ContextStatus.STALE, "STALE")])
def test_stream_availability_is_not_fabricated(status, expected):
    snapshot = OrderBookLiquiditySnapshot.unavailable(symbol="BTCUSDT", reason="disconnected", status=status)
    result = observed_stream_context(snapshot, as_of=CUTOFF, stale_seconds=5, limitation="Observed book only.")
    assert result["status"] == expected
    assert "snapshot" not in result


@pytest.mark.parametrize("age,expected", [(0, D(".1")), (6, NA), (-1, NA)])
def test_spread_cost_only_uses_fresh_causal_synchronized_book(age, expected):
    snapshot = OrderBookLiquiditySnapshot(symbol="BTCUSDT", status=ContextStatus.VERIFIED,
        synchronized=True, observed_at=CUTOFF-timedelta(seconds=age), spread_absolute=D(".1"))
    assert observed_spread(snapshot, symbol="BTCUSDT", cutoff=CUTOFF, stale_seconds=5) == expected
    assert observed_spread(snapshot.model_copy(update={"synchronized": False}), symbol="BTCUSDT", cutoff=CUTOFF, stale_seconds=5) == NA
    assert observed_spread(snapshot, symbol="ETHUSDT", cutoff=CUTOFF, stale_seconds=5) == NA
