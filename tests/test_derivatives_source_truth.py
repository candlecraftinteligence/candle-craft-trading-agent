from __future__ import annotations

import asyncio
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.analytics.derivatives_enrichment import enrich_derivatives
from app.analytics.derivatives_source_truth import (
    evaluate_derivatives_source_truth,
    liquidation_coverage_label,
    native_position_change_pct,
    quote_notional,
    write_dev_shadow_artifact,
)
from app.cache.market_data_cache import CachedMarketDataClient, MarketDataCache
from app.data.candle_batch_evidence import (
    observe_adapter_normalized_batch,
    observe_empty_execution,
    unavailable_delivery,
)
from app.data.derivatives_instrument import parse_binance_usds_instrument, parse_bybit_linear_instrument
from app.data.derivatives_source_evidence import (
    RATE_SETTLED_FUNDING,
    UNIT_UNKNOWN,
    capture_acquisition,
    describe_binance_account_ratio,
    describe_binance_funding_history,
    describe_binance_open_interest,
    describe_binance_open_interest_history,
    describe_bybit_account_ratio,
    describe_bybit_funding_history,
    describe_bybit_open_interest,
    represent_delivery,
)
from app.data.dtos import NA
from app.data.exchange_clients import BinanceFuturesClient, BybitLinearClient
from app.data.normalizers.binance import (
    normalize_binance_funding,
    normalize_binance_long_short_ratio,
    normalize_binance_open_interest,
    normalize_binance_open_interest_history,
)
from app.lifecycle.economic_identity import mint_plan_version_id
from app.lifecycle.service import observation_from_symbol_result
from app.microstructure.liquidation_aggregator import SymbolLiquidationAggregator
from app.pipeline.scanner_runner import (
    ScannerRunner,
    _derivatives_enrichment_input,
    _derivatives_rejection,
    _previous_open_interest_from_history,
)
from app.runtime_epoch.origin import producer_acquisition_from_delivery, producer_acquisition_from_symbol_result
from app.runtime_epoch.time_contract import comparable_utc
from app.storage.database import SCHEMA_VERSION, connect_database, identify_schema_version, initialize_database
from tests.test_scanner_runner import FakeExchangeClient, SpyAlertAgent, _clean_target_intelligence, _config, _strategy_pullback_candles

CUTOFF = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
PREVIOUS = CUTOFF - timedelta(minutes=5)
IRREGULAR_PREVIOUS = CUTOFF - timedelta(minutes=7)
FUNDING_AT = CUTOFF - timedelta(hours=1)
STALE_FUNDING_AT = CUTOFF - timedelta(days=2)


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def fixed_clock(moment: datetime):
    def clock() -> datetime:
        return moment

    return clock


def assert_cannot_confirm(result) -> None:
    assert result.validated_confirmation is False
    assert result.enforcement == "not_active"
    assert result.mode == "shadow"
    assert result.supports_long is not True
    assert result.supports_short is not True
    assert result.supports_long != "true"
    assert result.supports_short != "true"


def binance_instrument_payload(symbol: str = "BTCUSDT", **overrides: Any) -> dict[str, Any]:
    row = {
        "symbol": symbol,
        "baseAsset": "BTC" if symbol == "BTCUSDT" else symbol.replace("USDT", ""),
        "quoteAsset": "USDT",
        "marginAsset": "USDT",
        "contractType": "PERPETUAL",
        "fundingIntervalHours": 8,
    }
    row.update(overrides)
    return {"symbols": [row]}


def acquired(moment: datetime = CUTOFF):
    return capture_acquisition(fixed_clock(moment))


def binance_instrument(moment: datetime = CUTOFF, **overrides: Any):
    symbol = str(overrides.get("symbol", "BTCUSDT"))
    return parse_binance_usds_instrument(
        binance_instrument_payload(symbol, **{key: value for key, value in overrides.items() if key != "symbol"}),
        requested_symbol=symbol,
        acquisition=acquired(moment),
    )


def oi_history_payload(rows: list[tuple[datetime, str, str | None]]) -> list[dict[str, Any]]:
    payload = []
    for moment, quantity, notional in rows:
        row = {"symbol": "BTCUSDT", "sumOpenInterest": quantity, "timestamp": millis(moment)}
        if notional is not None:
            row["sumOpenInterestValue"] = notional
        payload.append(row)
    return payload


def funding_payload(rate: str, moment: datetime) -> list[dict[str, Any]]:
    return [{"symbol": "BTCUSDT", "fundingRate": rate, "fundingTime": millis(moment)}]


def ratio_payload(moment: datetime, ratio: str = "1.10000000", long_account: str = "0.55", short_account: str = "0.50") -> list[dict[str, Any]]:
    return [
        {
            "symbol": "BTCUSDT",
            "longShortRatio": ratio,
            "longAccount": long_account,
            "shortAccount": short_account,
            "timestamp": millis(moment),
        }
    ]


_DEFAULT_INSTRUMENT = object()


def describe_binance_case(
    *,
    current: tuple[datetime, str] = (CUTOFF, "110"),
    history: list[tuple[datetime, str, str | None]] | None = None,
    funding_rate: str = "0.0001",
    funding_at: datetime = FUNDING_AT,
    ratio_at: datetime = CUTOFF,
    acquisition_at: datetime = CUTOFF,
    instrument: Any = _DEFAULT_INSTRUMENT,
    include_ratio: bool = True,
):
    if history is None:
        history = [(PREVIOUS, "100", "10000000"), (current[0], current[1], "11000000")]
    acquisition = acquired(acquisition_at)
    current_batch = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": current[1], "time": millis(current[0])},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquisition,
    )
    history_batch = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=oi_history_payload(history),
        request_params={"symbol": "BTCUSDT", "period": "5m", "limit": 30},
        capture_clock=None,
        acquisition=acquisition,
    )
    funding_batch = describe_binance_funding_history(
        requested_symbol="BTCUSDT",
        payload=funding_payload(funding_rate, funding_at),
        request_params={"symbol": "BTCUSDT", "limit": 1},
        capture_clock=None,
        acquisition=acquisition,
    )
    ratio_batch = (
        describe_binance_account_ratio(
            requested_symbol="BTCUSDT",
            payload=ratio_payload(ratio_at),
            request_params={"symbol": "BTCUSDT", "period": "5m", "limit": 1},
            capture_clock=None,
            acquisition=acquisition,
        )
        if include_ratio
        else None
    )
    return evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=None if instrument is None else binance_instrument(acquisition_at) if instrument is _DEFAULT_INSTRUMENT else instrument,
        open_interest=current_batch,
        open_interest_history=history_batch,
        funding_history=funding_batch,
        account_ratio=ratio_batch,
    )


class _Router:
    def __init__(self, routes: dict[str, Any]) -> None:
        self.routes = routes
        self.calls: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append((request.url.path, dict(request.url.params)))
        payload = self.routes[request.url.path]
        if callable(payload):
            payload = payload(request)
        return httpx.Response(200, json=payload)


def test_native_quantity_change_ignores_mark_price() -> None:
    unchanged, reasons = native_position_change_pct(Decimal("2.5"), Decimal("2.5"))
    increased, increased_reasons = native_position_change_pct(Decimal("110"), Decimal("100"))
    assert unchanged == Decimal("0.00000000")
    assert reasons == ()
    assert increased == Decimal("10.00000000")
    assert increased_reasons == ()
    assert quote_notional(Decimal("2.5"), Decimal("100")) == Decimal("250.00000000")
    assert quote_notional(Decimal("2.5"), Decimal("110")) == Decimal("275.00000000")

    flat = describe_binance_case(
        current=(CUTOFF, "2.5"),
        history=[(PREVIOUS, "2.5", "250"), (CUTOFF, "2.5", "275")],
    )
    assert_cannot_confirm(flat)
    assert flat.classification == "eligible_no_conflict_detected"
    assert flat.oi_change_pct == "0.00000000"
    assert flat.oi_change_context == "current"
    assert flat.supports_long == "eligible_no_conflict_detected"
    assert flat.supports_short == "eligible_no_conflict_detected"


def test_valid_native_increase_and_exact_funding_fraction() -> None:
    result = describe_binance_case()
    assert_cannot_confirm(result)
    assert result.classification == "eligible_no_conflict_detected"
    assert result.oi_change_pct == "10.00000000"
    assert result.oi_comparison_interval_ms == 300_000
    assert result.oi_interval_label == "300000ms"
    assert result.funding_label == RATE_SETTLED_FUNDING
    assert result.funding_rate == "0.00010000"
    assert result.funding_percent == "0.01000000"
    assert result.funding_basis_points == "1.00000000"
    assert result.funding_cadence_ms == 8 * 60 * 60 * 1000
    assert result.account_ratio == "1.10000000"


def test_unknown_unit_without_instrument_is_unverified() -> None:
    result = describe_binance_case(instrument=None)
    assert_cannot_confirm(result)
    assert result.oi_change_pct == NA
    assert "unit_unverified" in result.reason_codes
    assert result.classification == "unverified"


def test_binance_and_bybit_clients_preserve_legacy_values_and_source_semantics() -> None:
    binance_routes = {
        "/fapi/v1/openInterest": {"symbol": "BTCUSDT", "openInterest": "110", "time": millis(CUTOFF)},
        "/futures/data/openInterestHist": oi_history_payload(
            [(PREVIOUS, "100", "10000000"), (CUTOFF, "110", "12100000")]
        ),
        "/fapi/v1/fundingRate": funding_payload("0.0001", FUNDING_AT),
        "/futures/data/globalLongShortAccountRatio": ratio_payload(CUTOFF),
        "/fapi/v1/exchangeInfo": binance_instrument_payload(),
    }
    binance_router = _Router(binance_routes)
    bybit_oi = {
        "retCode": 0,
        "retMsg": "OK",
        "time": millis(CUTOFF),
        "result": {
            "category": "linear",
            "symbol": "BTCUSDT",
            "list": [
                {"openInterest": "100", "singleOpenInterest": "50", "timestamp": millis(PREVIOUS)},
                {"openInterest": "110", "singleOpenInterest": "40", "timestamp": millis(CUTOFF)},
            ],
        },
    }
    bybit_routes = {
        "/v5/market/open-interest": bybit_oi,
        "/v5/market/funding/history": {
            "retCode": 0,
            "retMsg": "OK",
            "time": millis(CUTOFF),
            "result": {
                "category": "linear",
                "list": [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": millis(FUNDING_AT)}],
            },
        },
        "/v5/market/account-ratio": {
            "retCode": 0,
            "retMsg": "OK",
            "time": millis(CUTOFF),
            "result": {
                "category": "linear",
                "list": [{"symbol": "BTCUSDT", "buyRatio": "0.55", "sellRatio": "0.50", "timestamp": millis(CUTOFF)}],
            },
        },
        "/v5/market/instruments-info": {
            "retCode": 0,
            "retMsg": "OK",
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "LinearPerpetual",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "deliveryTime": "0",
                        "fundingInterval": "480",
                        "lotSizeFilter": {"qtyStep": "0.001", "minOrderQty": "0.001"},
                    }
                ],
            },
        },
    }
    bybit_router = _Router(bybit_routes)

    async def scenario() -> None:
        binance = BinanceFuturesClient(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(binance_router), base_url="https://binance.test"),
            retry_attempts=1,
            retry_base_delay=0,
            retry_max_delay=0,
        )
        bybit = BybitLinearClient(
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(bybit_router), base_url="https://bybit.test"),
            retry_attempts=1,
            retry_base_delay=0,
            retry_max_delay=0,
        )
        binance_oi = await binance.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        binance_history = await binance.get_open_interest_history_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        binance_funding = await binance.get_funding_rate_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        binance_ratio = await binance.get_long_short_ratio_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        binance_meta = await binance.get_instrument_metadata("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        bybit_oi = await bybit.get_open_interest_history_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        bybit_funding = await bybit.get_funding_rate_history_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        bybit_ratio = await bybit.get_long_short_ratio_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        bybit_meta = await bybit.get_instrument_metadata("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        await binance.aclose()
        await bybit.aclose()
        return {
            "binance_oi": binance_oi,
            "binance_history": binance_history,
            "binance_funding": binance_funding,
            "binance_ratio": binance_ratio,
            "binance_meta": binance_meta,
            "bybit_oi": bybit_oi,
            "bybit_funding": bybit_funding,
            "bybit_ratio": bybit_ratio,
            "bybit_meta": bybit_meta,
        }

    found = run(scenario())
    assert found["binance_oi"].value.unit == NA
    assert found["binance_oi"].value.open_interest == Decimal("110")
    assert found["binance_history"].value[-1].notional_value == Decimal("12100000")
    assert found["binance_history"].value[-1].unit == NA
    assert found["binance_oi"].evidence.observations[0].quantities[0].unit == UNIT_UNKNOWN
    assert found["binance_oi"].evidence.observations[0].acquisition.acquired_at == CUTOFF
    assert found["binance_oi"].evidence.observations[0].acquisition.meaning == "successful_adapter_response_boundary"
    assert found["binance_funding"].value.funding_rate == Decimal("0.0001")
    assert found["binance_funding"].evidence.observations[0].rate_kind == RATE_SETTLED_FUNDING
    assert found["binance_funding"].evidence.observations[0].metric_time.meaning == "funding_settlement_time"
    assert isinstance(found["binance_ratio"].value, Decimal)
    assert found["binance_ratio"].evidence.observations[0].metric_time.at == CUTOFF
    assert not hasattr(found["binance_ratio"].value, "timestamp")
    assert found["binance_meta"].base_asset == "BTC"
    assert found["binance_meta"].multiplier_applicability == "not_applicable"
    quantities = found["bybit_oi"].evidence.observations[-1].quantities
    assert [item.raw_field for item in quantities] == ["openInterest", "singleOpenInterest"]
    assert [item.counting_convention for item in quantities] == ["both_sides", "single_side"]
    assert quantities[0].numeric_value == Decimal("110")
    assert quantities[1].numeric_value == Decimal("40")
    assert found["bybit_oi"].evidence.observations[-1].metric_time.at == CUTOFF
    assert found["bybit_oi"].evidence.observations[-1].response_time.at == CUTOFF
    assert found["bybit_funding"].evidence.observations[0].metric_time.meaning == "funding_settlement_time"
    assert found["bybit_ratio"].evidence.observations[0].metric_time.at == CUTOFF
    assert found["bybit_meta"].funding_interval_ms == 480 * 60 * 1000
    assert found["bybit_meta"].multiplier_applicability == "not_applicable"
    assert any(path == "/fapi/v1/openInterest" and params.get("symbol") == "BTCUSDT" for path, params in binance_router.calls)
    assert any(path == "/v5/market/open-interest" and params.get("category") == "linear" for path, params in bybit_router.calls)
    legacy_ratio = normalize_binance_long_short_ratio("BTCUSDT", ratio_payload(CUTOFF))
    assert legacy_ratio == Decimal("1.10000000")
    assert found["binance_ratio"].value == legacy_ratio


def test_bybit_row_timestamp_is_not_replaced_by_envelope_time() -> None:
    batch = describe_bybit_open_interest(
        requested_symbol="BTCUSDT",
        payload={
            "time": millis(CUTOFF),
            "result": {"category": "linear", "symbol": "BTCUSDT", "list": [{"openInterest": "1"}]},
        },
        request_params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min"},
        capture_clock=None,
        acquisition=acquired(),
    )
    assert batch.observations[0].metric_time.at is None
    assert "missing_metric_timestamp" in batch.observations[0].metric_time.reason_codes
    assert batch.observations[0].response_time.at == CUTOFF


def test_wrong_identity_inverse_and_multiplier_stay_unverified() -> None:
    wrong_symbol = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "ETHUSDT", "openInterest": "110", "time": millis(CUTOFF)},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    assert wrong_symbol.observations[0].identity.returned_symbol == "ETHUSDT"
    assert "symbol_conflict" in wrong_symbol.observations[0].identity.reason_codes
    history = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=oi_history_payload([(PREVIOUS, "100", None), (CUTOFF, "110", None)]),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    conflicted = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=wrong_symbol,
        open_interest_history=history,
        funding_history=describe_binance_funding_history(
            requested_symbol="BTCUSDT",
            payload=funding_payload("0.0001", FUNDING_AT),
            request_params={"symbol": "BTCUSDT"},
            capture_clock=None,
            acquisition=acquired(),
        ),
    )
    assert_cannot_confirm(conflicted)
    assert conflicted.oi_change_pct == NA
    assert "symbol_conflict" in conflicted.reason_codes

    inverse = describe_binance_case(
        instrument=binance_instrument(marginAsset="USD", contractType="PERPETUAL")
    )
    assert_cannot_confirm(inverse)
    assert "unsupported_inverse" in inverse.reason_codes
    assert inverse.oi_change_pct == NA

    dated = binance_instrument(contractType="CURRENT_QUARTER")
    assert dated.quality == "unverified"
    assert "missing_expiry" in dated.reason_codes
    dated_result = describe_binance_case(instrument=dated)
    assert dated_result.oi_change_pct == NA

    scaled_instrument = binance_instrument(contractSize="1000")
    assert "multiplier_unverified" in scaled_instrument.reason_codes
    scaled = describe_binance_case(instrument=scaled_instrument)
    assert_cannot_confirm(scaled)
    assert scaled.oi_change_pct == NA
    assert "multiplier_unverified" in scaled.reason_codes
    shib = parse_binance_usds_instrument(
        binance_instrument_payload("1000SHIBUSDT", baseAsset="1000SHIB"),
        requested_symbol="1000SHIBUSDT",
        acquisition=acquired(),
    )
    assert shib.base_asset == "1000SHIB"
    assert shib.multiplier_applicability == "not_applicable"
    shib_current = describe_binance_open_interest(
        requested_symbol="1000SHIBUSDT",
        payload={"symbol": "1000SHIBUSDT", "openInterest": "1100", "time": millis(CUTOFF)},
        request_params={"symbol": "1000SHIBUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    shib_history = describe_binance_open_interest_history(
        requested_symbol="1000SHIBUSDT",
        payload=[
            {"symbol": "1000SHIBUSDT", "sumOpenInterest": "1000", "timestamp": millis(PREVIOUS)},
            {"symbol": "1000SHIBUSDT", "sumOpenInterest": "1100", "timestamp": millis(CUTOFF)},
        ],
        request_params={"symbol": "1000SHIBUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    shib_funding = describe_binance_funding_history(
        requested_symbol="1000SHIBUSDT",
        payload=[{"symbol": "1000SHIBUSDT", "fundingRate": "0.0001", "fundingTime": millis(FUNDING_AT)}],
        request_params={"symbol": "1000SHIBUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    shib_result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=shib,
        open_interest=shib_current,
        open_interest_history=shib_history,
        funding_history=shib_funding,
    )
    assert shib_result.oi_change_pct == "10.00000000"
    assert shib_result.classification == "eligible_no_conflict_detected"
    assert_cannot_confirm(shib_result)

    bybit_inverse = parse_bybit_linear_instrument(
        {
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "BTCUSD",
                        "contractType": "InversePerpetual",
                        "baseCoin": "BTC",
                        "quoteCoin": "USD",
                        "settleCoin": "BTC",
                        "deliveryTime": "0",
                    }
                ],
            }
        },
        requested_symbol="BTCUSD",
        acquisition=acquired(),
    )
    assert bybit_inverse.quality == "unverified"
    assert "unsupported_inverse" in bybit_inverse.reason_codes
    mismatched_settlement = parse_bybit_linear_instrument(
        {
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "LinearPerpetual",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USD",
                        "deliveryTime": "0",
                        "fundingInterval": "480",
                    }
                ],
            }
        },
        requested_symbol="BTCUSDT",
        acquisition=acquired(),
    )
    assert "settlement_asset_mismatch" in mismatched_settlement.reason_codes
    wrong_venue = describe_binance_case(instrument=parse_bybit_linear_instrument(
        {
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "LinearPerpetual",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "deliveryTime": "0",
                        "fundingInterval": "480",
                    }
                ],
            }
        },
        requested_symbol="BTCUSDT",
        acquisition=acquired(),
    ))
    assert_cannot_confirm(wrong_venue)
    assert wrong_venue.oi_change_pct == NA
    assert "venue_mismatch" in wrong_venue.reason_codes


def test_notional_only_growth_is_not_position_growth() -> None:
    history = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=[
            {"symbol": "BTCUSDT", "sumOpenInterestValue": "100", "timestamp": millis(PREVIOUS)},
            {"symbol": "BTCUSDT", "sumOpenInterestValue": "110", "timestamp": millis(CUTOFF)},
        ],
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest_history=history,
    )
    assert_cannot_confirm(result)
    assert result.oi_change_pct == NA
    assert "notional_not_position_growth" in result.reason_codes


def test_bybit_single_side_is_preserved_and_not_substituted() -> None:
    batch = describe_bybit_open_interest(
        requested_symbol="BTCUSDT",
        payload={
            "time": millis(CUTOFF),
            "result": {
                "category": "linear",
                "symbol": "BTCUSDT",
                "list": [
                    {"openInterest": "100", "singleOpenInterest": "50", "timestamp": millis(PREVIOUS)},
                    {"openInterest": "110", "singleOpenInterest": "40", "timestamp": millis(CUTOFF)},
                ],
            },
        },
        request_params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min"},
        capture_clock=None,
        acquisition=acquired(),
    )
    instrument = parse_bybit_linear_instrument(
        {
            "result": {
                "category": "linear",
                "list": [
                    {
                        "symbol": "BTCUSDT",
                        "contractType": "LinearPerpetual",
                        "baseCoin": "BTC",
                        "quoteCoin": "USDT",
                        "settleCoin": "USDT",
                        "deliveryTime": "0",
                        "fundingInterval": "480",
                    }
                ],
            }
        },
        requested_symbol="BTCUSDT",
        acquisition=acquired(),
    )
    funding = describe_bybit_funding_history(
        requested_symbol="BTCUSDT",
        payload={
            "time": millis(CUTOFF),
            "result": {"category": "linear", "list": [{"symbol": "BTCUSDT", "fundingRate": "0.0001", "fundingRateTimestamp": millis(FUNDING_AT)}]},
        },
        request_params={"category": "linear", "symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    ratio = describe_bybit_account_ratio(
        requested_symbol="BTCUSDT",
        payload={
            "time": millis(CUTOFF),
            "result": {"category": "linear", "list": [{"symbol": "BTCUSDT", "buyRatio": "0.55", "sellRatio": "0.50", "timestamp": millis(CUTOFF)}]},
        },
        request_params={"category": "linear", "symbol": "BTCUSDT", "period": "5min"},
        capture_clock=None,
        acquisition=acquired(),
    )
    result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=instrument,
        open_interest=batch,
        funding_history=funding,
        account_ratio=ratio,
    )
    assert result.oi_change_pct == "10.00000000"
    assert "duplicate_conflict" not in result.reason_codes
    assert result.classification == "eligible_no_conflict_detected"
    assert_cannot_confirm(result)


def test_temporal_boundaries_do_not_fabricate_or_clamp() -> None:
    before = describe_binance_case(current=(CUTOFF - timedelta(seconds=1), "110"), acquisition_at=CUTOFF - timedelta(seconds=1))
    equal = describe_binance_case()
    metric_after = describe_binance_case(current=(CUTOFF + timedelta(seconds=1), "110"))
    acquired_after = describe_binance_case(acquisition_at=CUTOFF + timedelta(seconds=1))
    historical_row_fetched_late = describe_binance_case(
        current=(CUTOFF - timedelta(minutes=30), "110"),
        history=[(CUTOFF - timedelta(minutes=35), "100", None), (CUTOFF - timedelta(minutes=30), "110", None)],
        funding_at=CUTOFF - timedelta(hours=2),
        ratio_at=CUTOFF - timedelta(minutes=30),
        acquisition_at=CUTOFF + timedelta(seconds=1),
    )
    assert before.classification == "eligible_no_conflict_detected"
    assert equal.classification == "eligible_no_conflict_detected"
    assert "metric_after_cutoff" in metric_after.reason_codes
    assert metric_after.classification != "eligible_no_conflict_detected"
    assert acquired_after.classification == "acquired_after_cutoff"
    assert historical_row_fetched_late.classification == "acquired_after_cutoff"
    for result in (before, equal, metric_after, acquired_after, historical_row_fetched_late):
        assert_cannot_confirm(result)

    missing_time = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": "110"},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    assert missing_time.observations[0].metric_time.at is None
    assert missing_time.observations[0].metric_time.raw == NA

    response_before_metric = describe_bybit_open_interest(
        requested_symbol="BTCUSDT",
        payload={
            "time": millis(PREVIOUS),
            "result": {"category": "linear", "symbol": "BTCUSDT", "list": [{"openInterest": "110", "timestamp": millis(CUTOFF)}]},
        },
        request_params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min"},
        capture_clock=None,
        acquisition=acquired(),
    )
    late_response = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=response_before_metric,
    )
    assert "metric_after_response" in late_response.reason_codes
    assert_cannot_confirm(late_response)


def test_oi_pairing_is_deterministic_and_does_not_use_history_index() -> None:
    unordered = (
        {"open_interest": Decimal("50"), "timestamp": millis(CUTOFF - timedelta(hours=1))},
        {"open_interest": Decimal("110"), "timestamp": millis(CUTOFF)},
        {"open_interest": Decimal("100"), "timestamp": millis(IRREGULAR_PREVIOUS)},
    )
    assert _previous_open_interest_from_history(unordered) == Decimal("110")
    paired = describe_binance_case(
        current=(CUTOFF, "110"),
        history=[
            (CUTOFF, "110", None),
            (CUTOFF - timedelta(hours=1), "50", None),
            (IRREGULAR_PREVIOUS, "100", None),
        ],
    )
    assert paired.oi_change_pct == "10.00000000"
    assert paired.oi_comparison_interval_ms == 420_000
    assert paired.oi_interval_label == "420000ms"
    assert "5m" not in paired.oi_interval_label

    conflict = describe_binance_case(
        history=[
            (PREVIOUS, "100", None),
            (CUTOFF, "110", None),
            (CUTOFF, "999", None),
        ]
    )
    assert conflict.oi_change_pct == NA
    assert "duplicate_conflict" in conflict.reason_codes
    assert_cannot_confirm(conflict)

    duplicates = describe_binance_case(
        history=[(PREVIOUS, "100", None), (CUTOFF, "110", None), (CUTOFF, "110", None)]
    )
    assert duplicates.oi_change_pct == "10.00000000"
    assert "duplicate_conflict" not in duplicates.reason_codes

    zero = describe_binance_case(history=[(PREVIOUS, "0", None), (CUTOFF, "110", None)])
    negative = describe_binance_case(current=(CUTOFF, "-1"))
    non_finite = describe_binance_case(current=(CUTOFF, "NaN"))
    infinite = describe_binance_case(current=(CUTOFF, "Infinity"))
    boolean = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": True, "time": millis(CUTOFF)},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    assert "zero_baseline" in zero.reason_codes
    assert "negative_open_interest" in negative.reason_codes
    assert "non_finite_quantity" in non_finite.reason_codes or "malformed_quantity" in non_finite.reason_codes
    assert "non_finite_quantity" in infinite.reason_codes or "malformed_quantity" in infinite.reason_codes
    assert "malformed_quantity" in boolean.observations[0].reason_codes
    for result in (zero, negative, non_finite, infinite):
        assert result.oi_change_pct == NA
        assert_cannot_confirm(result)

    other_symbol = describe_binance_open_interest_history(
        requested_symbol="ETHUSDT",
        payload=[
            {"symbol": "ETHUSDT", "sumOpenInterest": "1", "timestamp": millis(PREVIOUS)},
            {"symbol": "ETHUSDT", "sumOpenInterest": "2", "timestamp": millis(CUTOFF)},
        ],
        request_params={"symbol": "ETHUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    current = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": "110", "time": millis(CUTOFF)},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    crossed = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=current,
        open_interest_history=other_symbol,
        funding_history=describe_binance_funding_history(
            requested_symbol="BTCUSDT",
            payload=funding_payload("0.0001", FUNDING_AT),
            request_params={"symbol": "BTCUSDT"},
            capture_clock=None,
            acquisition=acquired(),
        ),
    )
    assert "cross_instrument_not_paired" in crossed.reason_codes
    assert crossed.oi_change_pct == NA


def test_funding_and_account_ratio_semantics() -> None:
    stale = describe_binance_case(funding_at=STALE_FUNDING_AT)
    assert "stale_for_current_context" in stale.reason_codes
    assert stale.classification == "unverified"
    assert_cannot_confirm(stale)

    no_cadence = describe_binance_case(instrument=binance_instrument())
    instrument = parse_binance_usds_instrument(
        {"symbols": [{"symbol": "BTCUSDT", "baseAsset": "BTC", "quoteAsset": "USDT", "marginAsset": "USDT", "contractType": "PERPETUAL"}]},
        requested_symbol="BTCUSDT",
        acquisition=acquired(),
    )
    unknown_cadence = describe_binance_case(instrument=instrument)
    assert "funding_cadence_unknown" in unknown_cadence.reason_codes
    assert "freshness_unjustified" in unknown_cadence.reason_codes
    assert unknown_cadence.classification != "eligible_no_conflict_detected"
    assert_cannot_confirm(unknown_cadence)
    del no_cadence

    huge = describe_binance_case(funding_rate="1.5")
    assert "unexpected_funding_magnitude" in huge.reason_codes
    assert huge.funding_percent == NA
    assert "0.015" not in huge.funding_percent
    assert_cannot_confirm(huge)

    base = describe_binance_funding_history(
        requested_symbol="BTCUSDT",
        payload=funding_payload("0.0001", FUNDING_AT),
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    predicted = replace(base, observations=(replace(base.observations[0], rate_kind="predicted_funding"),))
    predicted_result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        funding_history=predicted,
    )
    assert "unexpected_funding_subtype" in predicted_result.reason_codes
    assert predicted_result.funding_label != RATE_SETTLED_FUNDING
    assert_cannot_confirm(predicted_result)

    invalid = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=[{"symbol": "BTCUSDT", "longAccount": "0.4", "shortAccount": "0", "timestamp": millis(CUTOFF)}],
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    conflict = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=[{"symbol": "BTCUSDT", "longShortRatio": "9", "longAccount": "0.7", "shortAccount": "0.3", "timestamp": millis(CUTOFF)}],
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    invalid_result = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF, account_ratio=invalid)
    conflict_result = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF, account_ratio=conflict)
    assert "invalid_denominator" in invalid_result.reason_codes
    assert invalid_result.account_ratio == NA
    assert "ratio_component_conflict" in conflict_result.reason_codes
    assert conflict_result.account_ratio == NA
    assert_cannot_confirm(invalid_result)
    assert_cannot_confirm(conflict_result)


def test_price_only_legacy_support_cannot_become_shadow_confirmation() -> None:
    legacy = enrich_derivatives(
        symbol="BTCUSDT",
        exchange="binance_futures",
        latest_price=Decimal("110"),
        current_funding_rate=NA,
        current_open_interest=NA,
        previous_open_interest=NA,
        candles_15m=({"close": Decimal("100")}, {"close": Decimal("110")}),
        long_short_ratio=NA,
    )
    assert legacy.supports_long is True
    assert legacy.supports_short is True
    shadow = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        price_change_pct=Decimal("10"),
        price_quality="verified",
    )
    assert "price_only_not_derivatives_support" in shadow.reason_codes
    assert shadow.supports_long != "true"
    assert shadow.supports_long is not True
    assert_cannot_confirm(shadow)


def test_real_enrichment_engine_ignores_stale_and_future_timestamps() -> None:
    stale = datetime(2020, 1, 1, tzinfo=UTC)
    future = datetime(2030, 1, 1, tzinfo=UTC)
    funding = normalize_binance_funding("BTCUSDT", funding_payload("0.01", stale))
    current = normalize_binance_open_interest(
        "BTCUSDT",
        {"symbol": "BTCUSDT", "openInterest": "110", "time": millis(future)},
    )
    history = normalize_binance_open_interest_history(
        "BTCUSDT",
        oi_history_payload([(stale, "100", "100"), (future, "110", "130")]),
    )
    assert current.unit == NA
    assert history[-1].notional_value == Decimal("130")
    optional = SimpleNamespace(
        funding=funding,
        open_interest=current,
        previous_open_interest=_previous_open_interest_from_history(history),
        funding_history=(funding,),
        open_interest_history=history,
        long_short_ratio=Decimal("1.2"),
        liquidation_data=None,
        warnings=(),
    )
    produced = _derivatives_enrichment_input(
        symbol="BTCUSDT",
        exchange="binance_futures",
        candles=(),
        current_price=Decimal("100"),
        optional_data=optional,  # type: ignore[arg-type]
        interval="15m",
    )
    assert "timestamp" not in type(produced).model_fields
    assert produced.current_funding_rate == Decimal("0.01")
    assert produced.current_open_interest == Decimal("110")
    enriched = enrich_derivatives(produced)
    rejection = _derivatives_rejection("long", enriched)
    assert enriched.funding_status == "extreme_positive"
    assert enriched.oi_direction == "rising"
    assert rejection is not None
    evidence_current = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": "110", "time": millis(future)},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(CUTOFF + timedelta(seconds=1)),
    )
    evidence_history = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=oi_history_payload([(stale, "100", "100"), (future, "110", "130")]),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(CUTOFF + timedelta(seconds=1)),
    )
    evidence_funding = describe_binance_funding_history(
        requested_symbol="BTCUSDT",
        payload=funding_payload("0.01", stale),
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(CUTOFF + timedelta(seconds=1)),
    )
    shadow = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(CUTOFF + timedelta(seconds=1)),
        open_interest=evidence_current,
        open_interest_history=evidence_history,
        funding_history=evidence_funding,
    )
    assert shadow.classification == "acquired_after_cutoff"
    assert_cannot_confirm(shadow)


def test_liquidation_coverage_labels_do_not_become_zero_activity() -> None:
    start = CUTOFF
    disconnected = SymbolLiquidationAggregator("BTCUSDT", stale_after_seconds=30)
    unavailable = disconnected.snapshot(as_of=start)
    covered = SymbolLiquidationAggregator("BTCUSDT", stale_after_seconds=30)
    covered.mark_connected(start - timedelta(minutes=20))
    empty = covered.snapshot(as_of=start)
    assert liquidation_coverage_label(unavailable) == "unavailable_not_zero"
    assert liquidation_coverage_label(empty) == "covered_empty_feed_window_not_exchange_wide_zero"
    assert empty.usage == "research_only"
    labeled = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF, liquidation_snapshot=empty)
    assert labeled.liquidation_coverage == "covered_empty_feed_window_not_exchange_wide_zero"
    assert_cannot_confirm(labeled)


def test_cache_preserves_original_acquisition_and_does_not_rehydrate_it(tmp_path: Path) -> None:
    state = {"now": CUTOFF}
    fetches = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        fetches["count"] += 1
        return httpx.Response(200, json={"symbol": "BTCUSDT", "openInterest": "110", "time": millis(CUTOFF)})

    async def scenario() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://binance.test")
        inner = BinanceFuturesClient(http_client=http_client, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)
        cache = MarketDataCache(file_path=tmp_path / "cache.json", now=lambda: 1_000_000.0)
        client = CachedMarketDataClient(inner, cache)
        first = await client.get_open_interest_with_evidence("BTCUSDT", capture_clock=lambda: state["now"])
        state["now"] = CUTOFF + timedelta(seconds=5)
        second = await client.get_open_interest_with_evidence("BTCUSDT", capture_clock=lambda: state["now"])
        disabled = CachedMarketDataClient(inner, MarketDataCache(enabled=False))
        state["now"] = CUTOFF
        direct = await disabled.get_open_interest_with_evidence("BTCUSDT", capture_clock=lambda: state["now"])
        expiring = MarketDataCache(ttl_seconds=30, now=lambda: 0.0)
        expiring_client = CachedMarketDataClient(inner, expiring)
        await expiring_client.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        expiring._now = lambda: 31.0
        refetched = await expiring_client.get_open_interest_with_evidence(
            "BTCUSDT",
            capture_clock=fixed_clock(CUTOFF + timedelta(seconds=40)),
        )
        await inner.aclose()
        return first, second, direct, refetched

    first, second, direct, refetched = run(scenario())
    assert first.evidence.cache.delivery_kind == "miss"
    assert second.evidence.cache.delivery_kind == "hit"
    assert second.evidence.acquisition.acquired_at == CUTOFF
    assert second.evidence.cache.original_acquired_at == CUTOFF
    assert second.evidence.cache.delivered_at == CUTOFF + timedelta(seconds=5)
    assert second.value.open_interest == first.value.open_interest
    early = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF - timedelta(seconds=1), open_interest=second.evidence)
    late = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF + timedelta(minutes=1), open_interest=second.evidence)
    assert "acquisition_after_cutoff" in early.reason_codes
    assert "acquisition_after_cutoff" not in late.reason_codes
    assert_cannot_confirm(early)
    assert_cannot_confirm(late)
    assert direct.evidence.cache.delivery_kind == "not_applicable"
    assert direct.evidence.acquisition.acquired_at == CUTOFF
    assert refetched.evidence.cache.delivery_kind == "expiry_refetch"
    assert refetched.evidence.acquisition.acquired_at == CUTOFF + timedelta(seconds=40)

    async def rehydrate() -> Any:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://binance.test")
        inner = BinanceFuturesClient(http_client=http_client, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)
        loaded = CachedMarketDataClient(inner, MarketDataCache(file_path=tmp_path / "cache.json", now=lambda: 1_000_010.0))
        result = await loaded.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF + timedelta(hours=1)))
        await inner.aclose()
        return result

    restored = run(rehydrate())
    assert restored.value.open_interest == Decimal("110")
    assert restored.evidence.acquisition.acquired_at is None
    assert "legacy_cache_entry_lacks_acquisition_observation" in restored.evidence.reason_codes
    assert fetches["count"] >= 1


def test_retry_stamps_only_the_successful_response_and_calls_keep_local_clocks() -> None:
    attempts = {"count": 0}
    clock_samples: list[datetime] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(500, json={"error": "temporary"})
        symbol = request.url.params.get("symbol", "BTCUSDT")
        return httpx.Response(200, json={"symbol": symbol, "openInterest": "110", "time": millis(CUTOFF)})

    async def scenario() -> None:
        http_client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://binance.test")
        client = BinanceFuturesClient(http_client=http_client, retry_attempts=2, retry_base_delay=0, retry_max_delay=0)

        def clock() -> datetime:
            moment = CUTOFF
            clock_samples.append(moment)
            return moment

        delivery = await client.get_open_interest_with_evidence("BTCUSDT", capture_clock=clock)

        async def one(symbol: str, moment: datetime):
            return await client.get_open_interest_with_evidence(symbol, capture_clock=fixed_clock(moment))

        eth_at = CUTOFF + timedelta(seconds=2)
        btc_at = CUTOFF + timedelta(seconds=4)
        eth, btc = await asyncio.gather(one("ETHUSDT", eth_at), one("BTCUSDT", btc_at))
        await client.aclose()
        return delivery, eth, btc

    delivery, eth, btc = run(scenario())
    assert attempts["count"] >= 2
    assert clock_samples == [CUTOFF]
    assert delivery.evidence.acquisition.acquired_at == CUTOFF
    assert eth.evidence.acquisition.acquired_at == CUTOFF + timedelta(seconds=2)
    assert btc.evidence.acquisition.acquired_at == CUTOFF + timedelta(seconds=4)
    assert eth.evidence.observations[0].identity.requested_symbol == "ETHUSDT"
    assert btc.evidence.observations[0].identity.requested_symbol == "BTCUSDT"


def test_malformed_numbers_do_not_confirm() -> None:
    malformed = describe_binance_case(current=(CUTOFF, "not-a-number"))
    assert "malformed_quantity" in malformed.reason_codes
    assert malformed.oi_change_pct == NA
    assert_cannot_confirm(malformed)
    boolean_change, reasons = native_position_change_pct(True, Decimal("1"))  # type: ignore[arg-type]
    assert boolean_change is None
    assert reasons == ("malformed_quantity",)


def test_evaluation_error_is_not_a_fallback_confirmation() -> None:
    class Boom:
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError("boom")

    result = evaluate_derivatives_source_truth(decision_cutoff=CUTOFF, instrument=Boom())  # type: ignore[arg-type]
    assert result.classification == "evaluation_error"
    assert result.evaluation_error == "RuntimeError"
    assert_cannot_confirm(result)


def test_dev_artifact_writer_is_explicit_json_only(tmp_path: Path) -> None:
    result = describe_binance_case()
    target = tmp_path / "shadow.json"
    write_dev_shadow_artifact(target, result)
    assert target.read_text(encoding="utf-8").startswith("{")
    with pytest.raises(ValueError):
        write_dev_shadow_artifact(tmp_path / "shadow.txt", result)
    scanner_source = Path("app/pipeline/scanner_runner.py").read_text(encoding="utf-8")
    assert "write_dev_shadow_artifact" not in scanner_source


def _decision_payload(result) -> dict[str, Any]:
    symbol = result.results[0]
    return {
        "status": symbol.status.value,
        "rejection_reason": symbol.rejection_reason,
        "missing_data": symbol.missing_data,
        "unverified_data": symbol.unverified_data,
        "funding_rate": str(symbol.funding_rate),
        "open_interest": str(symbol.open_interest),
        "final_quality_grade": symbol.final_quality_grade,
        "regime_state": symbol.regime_state,
        "regime_blocked": symbol.regime_blocked,
        "enrichment": None if symbol.derivatives_enrichment is None else symbol.derivatives_enrichment.model_dump(mode="json"),
        "score": None if symbol.score_result is None else symbol.score_result.model_dump(mode="json"),
        "risk": None if symbol.risk_decision is None else symbol.risk_decision.model_dump(mode="json"),
        "quality": symbol.setup_quality.model_dump(mode="json"),
        "trade_ideas": result.trade_ideas_created,
        "alerts": result.dry_run_alerts_created,
        "journal": result.journal_entries_created,
        "regime_enabled": result.market_regime.enabled,
        "regime_name": result.market_regime.state.value,
        "shadow_in_dump": "derivatives_source_truth_shadow" in symbol.model_dump(),
    }


def test_shadow_flag_preserves_public_scanner_decisions(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.scanner_runner.build_target_intelligence",
        lambda *args, **kwargs: _clean_target_intelligence(),
    )
    acquired_at = CUTOFF + timedelta(seconds=5)

    class EvidenceClient(FakeExchangeClient):
        def __init__(self) -> None:
            super().__init__({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF)
            self.evidence_calls = 0

        async def get_open_interest_with_evidence(self, symbol: str, *, capture_clock=None):
            self.evidence_calls += 1
            value = await self.get_open_interest(symbol)
            moment = capture_clock() if capture_clock is not None else acquired_at
            evidence = describe_binance_open_interest(
                requested_symbol=symbol,
                payload={"symbol": symbol, "openInterest": str(self.open_interest), "time": millis(CUTOFF - timedelta(minutes=1))},
                request_params={"symbol": symbol},
                capture_clock=None,
                acquisition=capture_acquisition(fixed_clock(moment)),
            )
            return represent_delivery(value, evidence)

    off_client = EvidenceClient()
    on_client = EvidenceClient()
    off = run(
        ScannerRunner(exchange_client=off_client, alert_agent=SpyAlertAgent(), capture_clock=fixed_clock(acquired_at)).run(
            _config(["BTCUSDT"], decision_timestamp=CUTOFF, derivatives_source_truth_shadow=False)
        )
    )
    on = run(
        ScannerRunner(exchange_client=on_client, alert_agent=SpyAlertAgent(), capture_clock=fixed_clock(acquired_at)).run(
            _config(["BTCUSDT"], decision_timestamp=CUTOFF, derivatives_source_truth_shadow=True, fast_mode=False)
        )
    )
    assert off_client.evidence_calls == 0
    assert on_client.evidence_calls == 1
    assert _decision_payload(off) == _decision_payload(on)
    assert off.results[0].derivatives_source_truth_shadow is None
    shadow = on.results[0].derivatives_source_truth_shadow
    assert shadow is not None
    assert shadow.classification == "acquired_after_cutoff"
    assert_cannot_confirm(shadow)
    assert "derivatives_source_truth_shadow" not in on.results[0].model_dump()


def test_fast_mode_and_evaluation_errors_do_not_change_the_legacy_scan(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.scanner_runner.build_target_intelligence",
        lambda *args, **kwargs: _clean_target_intelligence(),
    )
    off = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
        ).run(_config(["BTCUSDT"], decision_timestamp=CUTOFF, fast_mode=True))
    )
    fast = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
        ).run(_config(["BTCUSDT"], decision_timestamp=CUTOFF, fast_mode=True, derivatives_source_truth_shadow=True))
    )
    assert _decision_payload(off) == _decision_payload(fast)
    assert fast.results[0].derivatives_source_truth_shadow is not None
    assert "fast_mode_skipped" in fast.results[0].derivatives_source_truth_shadow.reason_codes
    assert_cannot_confirm(fast.results[0].derivatives_source_truth_shadow)

    def boom(**kwargs: Any):
        raise RuntimeError("shadow failed")

    monkeypatch.setattr("app.pipeline.scanner_runner.evaluate_derivatives_source_truth", boom)
    baseline = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
        ).run(_config(["BTCUSDT"], decision_timestamp=CUTOFF))
    )
    errored = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
        ).run(_config(["BTCUSDT"], decision_timestamp=CUTOFF, derivatives_source_truth_shadow=True))
    )
    assert _decision_payload(baseline) == _decision_payload(errored)
    shadow = errored.results[0].derivatives_source_truth_shadow
    assert shadow is not None
    assert shadow.classification == "evaluation_error"
    assert_cannot_confirm(shadow)


def test_schema_identity_and_producer_lineage_stay_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    assert SCHEMA_VERSION == 27
    connection = connect_database(tmp_path / "synthetic-source-truth.sqlite")
    try:
        initialize_database(connection)
        assert identify_schema_version(connection) == 27
        script = "\n".join(row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
        assert "derivatives_source_truth" not in script
    finally:
        connection.close()

    first = mint_plan_version_id(
        setup_id="setup-1",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="120",
        tp3="130",
        invalidation="94",
    )
    second = mint_plan_version_id(
        setup_id="setup-1",
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="120",
        tp3="130",
        invalidation="94",
    )
    assert first.available is True
    assert first.identity == second.identity

    monkeypatch.setattr(
        "app.pipeline.scanner_runner.build_target_intelligence",
        lambda *args, **kwargs: _clean_target_intelligence(),
    )
    scanned = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
        ).run(_config(["BTCUSDT"], decision_timestamp=CUTOFF))
    )
    symbol = scanned.results[0]
    shadowed = symbol.model_copy(update={"derivatives_source_truth_shadow": describe_binance_case()})
    assert symbol.model_dump(mode="json") == shadowed.model_dump(mode="json")
    assert asdict(observation_from_symbol_result(symbol)) == asdict(observation_from_symbol_result(shadowed))

    when = CUTOFF
    adapter = observe_adapter_normalized_batch(
        (),
        requested_symbol="BTCUSDT",
        requested_interval="15m",
        requested_limit=1,
        effective_symbol="BTCUSDT",
        effective_interval="15m",
        effective_limit=1,
        endpoint_path="/fapi/v1/klines",
        adapter_class_label="BinanceFuturesClient",
        capture_clock=fixed_clock(when),
        http_client_injected=True,
    )
    symbol_result = SimpleNamespace(
        lifecycle_execution_batch_delivery=adapter,
        lifecycle_batch_handoff=None,
        derivatives_observed_at=datetime(2020, 1, 1, tzinfo=UTC),
    )
    assert producer_acquisition_from_symbol_result(symbol_result) == comparable_utc(when)
    empty_with_parent = observe_empty_execution(
        execution_timeframe="15m",
        logical_cutoff=when,
        capture_clock=fixed_clock(when + timedelta(minutes=5)),
        parent=adapter,
    )
    assert producer_acquisition_from_delivery(empty_with_parent) == comparable_utc(when)
    assert producer_acquisition_from_delivery(observe_empty_execution(
        execution_timeframe="15m",
        logical_cutoff=when,
        capture_clock=fixed_clock(when),
        parent=None,
    )) is None
    assert producer_acquisition_from_delivery(unavailable_delivery((), reason="missing", capture_clock=fixed_clock(when))) is None
    assert producer_acquisition_from_delivery(SimpleNamespace(adapter=None, parent=None, cache=SimpleNamespace(upstream_acquisition=None))) is None
    assert producer_acquisition_from_symbol_result(SimpleNamespace(
        lifecycle_execution_batch_delivery=None,
        lifecycle_batch_handoff=None,
    )) is None
    handoff = SimpleNamespace(delivery=adapter)
    assert producer_acquisition_from_symbol_result(SimpleNamespace(
        lifecycle_execution_batch_delivery=None,
        lifecycle_batch_handoff=handoff,
    )) == comparable_utc(when)


def test_current_and_history_endpoints_pair_only_when_compatible() -> None:
    compatible = describe_binance_case()
    assert compatible.oi_change_context == "current"
    foreign = describe_bybit_open_interest(
        requested_symbol="BTCUSDT",
        payload={
            "time": millis(CUTOFF),
            "result": {"category": "linear", "symbol": "BTCUSDT", "list": [{"openInterest": "90", "timestamp": millis(PREVIOUS)}]},
        },
        request_params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min"},
        capture_clock=None,
        acquisition=acquired(),
    )
    current = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": "110", "time": millis(CUTOFF)},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    mixed = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=current,
        open_interest_history=foreign,
    )
    assert mixed.oi_change_pct == NA
    assert "cross_instrument_not_paired" in mixed.reason_codes or "endpoint_incompatible" in mixed.reason_codes
    assert_cannot_confirm(mixed)
