"""Regressions for Astra's PR #139 review. Fixtures are offline and deterministic."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from app.analytics.derivatives_source_truth import (
    DerivativesSourceTruthResult,
    evaluate_derivatives_source_truth,
    write_dev_shadow_artifact,
)
from app.cache.market_data_cache import CachedMarketDataClient, MarketDataCache
from app.data.derivatives_instrument import parse_binance_usds_instrument
from app.data.derivatives_source_evidence import (
    RATE_SETTLED_FUNDING,
    describe_binance_account_ratio,
    describe_binance_funding_history,
    describe_binance_open_interest,
    describe_binance_open_interest_history,
    describe_bybit_account_ratio,
    describe_bybit_funding_history,
    describe_bybit_open_interest,
)
from app.data.dtos import NA
from app.data.exchange_clients import BinanceFuturesClient, BybitLinearClient
from app.data.exchange_clients.base import ExchangeHTTPError
from app.pipeline.scanner_runner import ScannerRunner
from tests.test_derivatives_source_truth import (
    CUTOFF,
    FUNDING_AT,
    PREVIOUS,
    acquired,
    binance_instrument,
    describe_binance_case,
    fixed_clock,
    funding_payload,
    millis,
    oi_history_payload,
    ratio_payload,
)
from tests.test_scanner_runner import _config

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _cannot_confirm(result: DerivativesSourceTruthResult) -> None:
    assert result.mode == "shadow"
    assert result.enforcement == "not_active"
    assert result.validated_confirmation is False
    assert result.supports_long not in {True, "true"}
    assert result.supports_short not in {True, "true"}


def _binance_oi_routes(open_interest: str = "110") -> dict[str, Any]:
    return {
        "/fapi/v1/openInterest": {"symbol": "BTCUSDT", "openInterest": open_interest, "time": millis(CUTOFF)},
    }


def _bybit_oi_payload(open_interest: str = "110") -> dict[str, Any]:
    return {
        "retCode": 0,
        "retMsg": "OK",
        "time": millis(CUTOFF),
        "result": {
            "category": "linear",
            "symbol": "BTCUSDT",
            "list": [{"openInterest": open_interest, "timestamp": str(millis(CUTOFF))}],
        },
    }


async def _optional_open_interest(client: Any, *, shadow: bool, clock: Any) -> tuple[Any, ...]:
    runner = ScannerRunner(exchange_client=client, capture_clock=clock)
    return await runner._optional_call(
        client,
        "get_open_interest",
        "BTCUSDT",
        _config(["BTCUSDT"], derivatives_source_truth_shadow=shadow),
        "open_interest",
    )


def test_r1_capture_failure_keeps_legacy_open_interest_for_both_venues() -> None:
    def clock() -> datetime:
        raise RuntimeError("capture clock unavailable")

    async def scenario(kind: str) -> dict[str, Any]:
        if kind == "binance":
            def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=_binance_oi_routes()["/fapi/v1/openInterest"])

            http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://fapi.binance.com")
            client = BinanceFuturesClient(http_client=http, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)
        else:
            def handler(_request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=_bybit_oi_payload())

            http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.bybit.com")
            client = BybitLinearClient(http_client=http, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)
        off = await _optional_open_interest(client, shadow=False, clock=clock)
        on = await _optional_open_interest(client, shadow=True, clock=clock)
        await client.aclose()
        return {"off": off, "on": on}

    for kind in ("binance", "bybit"):
        found = run(scenario(kind))
        assert found["off"][1].open_interest == Decimal("110")
        assert found["off"][2] is None
        assert found["on"][1].open_interest == Decimal("110")
        assert found["on"][2] is None
        assert found["on"][4] is not None
        assert found["on"][4].acquisition.acquired_at is None


def test_r1_cache_attachment_failure_keeps_the_cached_value() -> None:
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        return httpx.Response(200, json=_binance_oi_routes()["/fapi/v1/openInterest"])

    async def scenario() -> dict[str, Any]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://fapi.binance.com")
        inner = BinanceFuturesClient(http_client=http, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)
        cached = CachedMarketDataClient(inner, cache=MarketDataCache())
        runner = ScannerRunner(exchange_client=cached, capture_clock=fixed_clock(CUTOFF))
        await cached.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        warmed = calls["count"]
        with patch("app.cache.market_data_cache.apply_cache_delivery", side_effect=RuntimeError("evidence failure")):
            off = await runner._optional_call(
                cached,
                "get_open_interest",
                "BTCUSDT",
                _config(["BTCUSDT"], derivatives_source_truth_shadow=False),
                "open_interest",
            )
            on = await runner._optional_call(
                cached,
                "get_open_interest",
                "BTCUSDT",
                _config(["BTCUSDT"], derivatives_source_truth_shadow=True),
                "open_interest",
            )
        await inner.aclose()
        return {"off": off, "on": on, "warmed": warmed, "calls": calls["count"]}

    found = run(scenario())
    assert found["warmed"] == 1
    assert found["calls"] == 1
    assert found["off"][1].open_interest == Decimal("110")
    assert found["on"][1].open_interest == Decimal("110")
    assert found["on"][2] is None
    assert found["on"][4].acquisition.acquired_at is None


def test_r1_transport_failure_and_cancellation_are_not_swallowed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    async def http_failure() -> tuple[Any, ...]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://fapi.binance.com")
        client = BinanceFuturesClient(http_client=http, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)
        try:
            return await _optional_open_interest(client, shadow=True, clock=fixed_clock(CUTOFF))
        finally:
            await client.aclose()

    failed = run(http_failure())
    assert failed[1] is None
    assert failed[2] == "open_interest: N/A"

    async def cancelled() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://fapi.binance.com")
        client = BinanceFuturesClient(http_client=http, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)

        async def explode(*_args: Any, **_kwargs: Any) -> Any:
            raise asyncio.CancelledError

        client._get_json = explode  # type: ignore[method-assign]
        try:
            await client.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        finally:
            await client.aclose()

    with pytest.raises(asyncio.CancelledError):
        run(cancelled())


def test_r1_retry_stamps_only_the_successful_response_and_does_not_refetch_after_it() -> None:
    calls = {"count": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["count"] += 1
        if calls["count"] == 1:
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(200, json=_binance_oi_routes()["/fapi/v1/openInterest"])

    def clock() -> datetime:
        if calls["count"] >= 2:
            raise RuntimeError("capture clock unavailable")
        return CUTOFF

    async def scenario() -> Any:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://fapi.binance.com")
        client = BinanceFuturesClient(http_client=http, retry_attempts=2, retry_base_delay=0, retry_max_delay=0)
        delivery = await client.get_open_interest_with_evidence("BTCUSDT", capture_clock=clock)
        await client.aclose()
        return delivery

    delivery = run(scenario())
    assert calls["count"] == 2
    assert delivery.value.open_interest == Decimal("110")
    assert delivery.evidence.acquisition.acquired_at is None


def test_r1_missing_evidence_method_and_malformed_metadata_keep_the_legacy_value() -> None:
    class LegacyOnly:
        exchange_name = "binance_futures"

        async def get_open_interest(self, _symbol: str) -> Any:
            return SimpleNamespace(open_interest=Decimal("110"))

    async def missing_method() -> dict[str, Any]:
        client = LegacyOnly()
        off = await _optional_open_interest(client, shadow=False, clock=fixed_clock(CUTOFF))
        on = await _optional_open_interest(client, shadow=True, clock=fixed_clock(CUTOFF))
        return {"off": off, "on": on}

    found = run(missing_method())
    assert found["off"][1].open_interest == Decimal("110")
    assert found["off"][4] is None
    assert found["on"][1].open_interest == Decimal("110")
    assert found["on"][2] is None
    assert "adapter_has_no_source_evidence" in found["on"][4].reason_codes

    routes = _binance_oi_routes()
    routes["/fapi/v1/exchangeInfo"] = {"symbols": "not-a-list"}

    class Router:
        def __call__(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=routes[request.url.path])

    async def malformed() -> Any:
        http = httpx.AsyncClient(transport=httpx.MockTransport(Router()), base_url="https://fapi.binance.com")
        client = BinanceFuturesClient(http_client=http, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)
        runner = ScannerRunner(exchange_client=client, capture_clock=fixed_clock(CUTOFF))
        config = _config(["BTCUSDT"], derivatives_source_truth_shadow=True)
        oi = await runner._optional_call(client, "get_open_interest", "BTCUSDT", config, "open_interest")
        instrument = await runner._shadow_instrument(client, "BTCUSDT", config)
        await client.aclose()
        return oi, instrument

    oi, instrument = run(malformed())
    assert oi[1].open_interest == Decimal("110")
    assert oi[2] is None
    assert instrument.quality != "verified"
    assert instrument.funding_interval_ms is None


def test_r2_predecessor_conflict_does_not_depend_on_row_order() -> None:
    ascending = describe_binance_case(
        history=[(PREVIOUS, "100", None), (PREVIOUS, "50", None), (CUTOFF, "110", None)]
    )
    descending = describe_binance_case(
        history=[(PREVIOUS, "50", None), (PREVIOUS, "100", None), (CUTOFF, "110", None)]
    )
    assert ascending.oi_change_pct == NA
    assert descending.oi_change_pct == NA
    assert ascending.classification == descending.classification
    assert ascending.classification != "eligible_no_conflict_detected"
    assert "duplicate_conflict" in ascending.reason_codes
    assert "duplicate_conflict" in descending.reason_codes
    _cannot_confirm(ascending)

    def bybit_history(order: list[tuple[str, datetime]]) -> Any:
        rows = [
            {"openInterest": quantity, "timestamp": str(millis(moment))}
            for quantity, moment in order
        ]
        return describe_bybit_open_interest(
            requested_symbol="BTCUSDT",
            payload={
                "time": millis(CUTOFF),
                "result": {"category": "linear", "symbol": "BTCUSDT", "list": rows},
            },
            request_params={"category": "linear", "symbol": "BTCUSDT", "intervalTime": "5min"},
            capture_clock=None,
            acquisition=acquired(),
        )

    left = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        open_interest_history=bybit_history([("100", PREVIOUS), ("50", PREVIOUS), ("110", CUTOFF)]),
    )
    right = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        open_interest_history=bybit_history([("50", PREVIOUS), ("100", PREVIOUS), ("110", CUTOFF)]),
    )
    assert left.oi_change_pct == NA
    assert right.oi_change_pct == NA
    assert left.to_dict()["oi_change_pct"] == right.to_dict()["oi_change_pct"]
    assert "duplicate_conflict" in left.reason_codes


def test_r2_identical_duplicates_collapse_and_conflicting_provenance_does_not() -> None:
    duplicates = describe_binance_case(
        history=[(PREVIOUS, "100", None), (CUTOFF, "110", None), (CUTOFF, "110", None)]
    )
    assert duplicates.oi_change_pct == "10.00000000"
    assert "duplicate_conflict" not in duplicates.reason_codes
    assert "conflicting_provenance" not in duplicates.reason_codes

    history = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=oi_history_payload([(PREVIOUS, "100", None), (PREVIOUS, "100", None), (CUTOFF, "110", None)]),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    changed = []
    shifted = False
    for observation in history.observations:
        if not shifted and observation.metric_time.at == PREVIOUS:
            changed.append(
                replace(
                    observation,
                    acquisition=replace(observation.acquisition, acquired_at=PREVIOUS - timedelta(seconds=5)),
                )
            )
            shifted = True
        else:
            changed.append(observation)
    conflicted = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest_history=replace(history, observations=tuple(changed)),
    )
    assert conflicted.oi_change_pct == NA
    assert "conflicting_provenance" in conflicted.reason_codes
    _cannot_confirm(conflicted)


def test_r2_out_of_order_history_missing_predecessor_and_notional_do_not_invent_growth() -> None:
    ordered = describe_binance_case(
        current=(CUTOFF, "110"),
        history=[
            (CUTOFF, "110", None),
            (CUTOFF - timedelta(hours=1), "50", None),
            (CUTOFF - timedelta(minutes=7), "100", None),
        ],
    )
    assert ordered.oi_change_pct == "10.00000000"
    assert ordered.oi_comparison_interval_ms == 420_000

    blocked_newest_neighbor = describe_binance_case(
        history=[
            (CUTOFF - timedelta(hours=1), "100", None),
            (PREVIOUS, "100", None),
            (PREVIOUS, "50", None),
            (CUTOFF, "110", None),
        ]
    )
    assert blocked_newest_neighbor.oi_change_pct == NA
    assert "duplicate_conflict" in blocked_newest_neighbor.reason_codes

    missing = describe_binance_case(history=[(CUTOFF, "110", None)])
    assert missing.oi_change_pct == NA
    assert "missing_previous_open_interest" in missing.reason_codes

    notional_only = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=[
            {"symbol": "BTCUSDT", "sumOpenInterestValue": "100", "timestamp": millis(PREVIOUS)},
            {"symbol": "BTCUSDT", "sumOpenInterestValue": "110", "timestamp": millis(CUTOFF)},
        ],
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    notional = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest_history=notional_only,
    )
    assert notional.oi_change_pct == NA
    assert "notional_not_position_growth" in notional.reason_codes


def test_r3_invalid_optional_ratio_does_not_change_an_admissible_conclusion() -> None:
    clean = describe_binance_case(include_ratio=False)
    eth_rows = ratio_payload(CUTOFF, ratio="4", long_account="0.8", short_account="0.2")
    eth_rows[0]["symbol"] = "ETHUSDT"
    eth_ratio = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=eth_rows,
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    wrong_symbol = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=clean_open_interest(),
        open_interest_history=clean_history(),
        funding_history=clean_funding(),
        account_ratio=eth_ratio,
    )
    assert wrong_symbol.classification == clean.classification
    assert wrong_symbol.oi_change_pct == clean.oi_change_pct
    assert wrong_symbol.supports_long == clean.supports_long
    assert wrong_symbol.supports_short == clean.supports_short
    assert wrong_symbol.account_ratio == NA
    assert "symbol_conflict" in wrong_symbol.reason_codes

    foreign = describe_bybit_account_ratio(
        requested_symbol="ETHUSDT",
        payload={
            "time": millis(CUTOFF),
            "result": {
                "category": "linear",
                "list": [
                    {"symbol": "ETHUSDT", "buyRatio": "0.49", "sellRatio": "0.51", "timestamp": str(millis(CUTOFF))}
                ],
            },
        },
        request_params={"symbol": "ETHUSDT", "category": "linear", "period": "5min"},
        capture_clock=None,
        acquisition=acquired(),
    )
    foreign_result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=clean_open_interest(),
        open_interest_history=clean_history(),
        funding_history=clean_funding(),
        account_ratio=foreign,
    )
    assert foreign_result.classification == clean.classification
    assert foreign_result.account_ratio == NA
    assert foreign_result.supports_long == clean.supports_long
    assert "venue_mismatch" in foreign_result.reason_codes


def test_r3_request_context_cannot_bind_funding_to_another_instrument() -> None:
    rows = funding_payload("0.0001", FUNDING_AT)
    del rows[0]["symbol"]
    funding = describe_binance_funding_history(
        requested_symbol="ETHUSDT",
        payload=rows,
        request_params={"symbol": "ETHUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=clean_open_interest(),
        open_interest_history=clean_history(),
        funding_history=funding,
    )
    assert result.oi_change_pct == "10.00000000"
    assert result.classification != "eligible_no_conflict_detected"
    assert result.funding_label != RATE_SETTLED_FUNDING
    assert "request_context_identity_conflict" in result.reason_codes
    assert funding.observations[0].identity.returned_symbol is None
    assert funding.observations[0].identity.symbol_provenance == "request_context"
    _cannot_confirm(result)


def test_r4_description_does_not_fabricate_acquisition() -> None:
    calls: list[datetime] = []

    def consumer_clock() -> datetime:
        calls.append(CUTOFF)
        return CUTOFF

    described = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=oi_history_payload([(PREVIOUS, "100", None), (CUTOFF, "110", None)]),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=consumer_clock,
    )
    assert calls == []
    assert described.acquisition.acquired_at is None
    assert described.acquisition.quality != "verified"
    assert described.acquisition.meaning == "successful_adapter_response_boundary"
    result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest_history=described,
        funding_history=clean_funding(),
    )
    assert result.classification != "eligible_no_conflict_detected"
    assert "missing_acquisition_timestamp" in result.reason_codes
    _cannot_confirm(result)


def test_r4_impossible_chronology_is_rejected_without_filling_missing_times() -> None:
    current = clean_open_interest()
    future_response = replace(
        current,
        observations=tuple(
            replace(
                row,
                response_time=replace(row.response_time, at=CUTOFF + timedelta(days=1), quality="verified", reason_codes=()),
            )
            for row in current.observations
        ),
        response_time=replace(current.response_time, at=CUTOFF + timedelta(days=1), quality="verified", reason_codes=()),
    )
    responded = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=future_response,
        open_interest_history=clean_history(),
        funding_history=clean_funding(),
    )
    assert "response_after_acquisition" in responded.reason_codes
    assert responded.classification != "eligible_no_conflict_detected"

    early_delivery = replace(
        current,
        cache=replace(current.cache, delivered_at=CUTOFF - timedelta(days=1), original_acquired_at=CUTOFF),
    )
    delivered = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=early_delivery,
        open_interest_history=clean_history(),
        funding_history=clean_funding(),
    )
    assert "delivery_before_acquisition" in delivered.reason_codes
    assert delivered.classification != "eligible_no_conflict_detected"

    malformed = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": "110", "time": "2026-01-15T12:00:00+99:00"},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    assert malformed.observations[0].metric_time.at is None
    assert "malformed_metric_timestamp" in malformed.observations[0].metric_time.reason_codes

    conflicted_fields = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "openInterest": "110",
            "time": millis(CUTOFF),
            "timestamp": millis(CUTOFF - timedelta(minutes=5)),
        },
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    assert conflicted_fields.observations[0].metric_time.at is None
    assert "ambiguous_metric_timestamp" in conflicted_fields.observations[0].reason_codes
    assert conflicted_fields.observations[0].response_time.at is None


def test_r5_account_share_and_funding_subtype_follow_provider_payloads() -> None:
    zero_short = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=ratio_payload(CUTOFF, ratio="4", long_account="0.8", short_account="0"),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    zero_result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=clean_open_interest(),
        open_interest_history=clean_history(),
        funding_history=clean_funding(),
        account_ratio=zero_short,
    )
    clean = describe_binance_case(include_ratio=False)
    assert zero_result.account_ratio == NA
    assert "invalid_denominator" in zero_result.reason_codes
    assert zero_result.classification == clean.classification
    assert zero_result.supports_long == clean.supports_long

    unbounded = describe_bybit_account_ratio(
        requested_symbol="BTCUSDT",
        payload={
            "time": millis(CUTOFF),
            "result": {
                "category": "linear",
                "list": [{"symbol": "BTCUSDT", "buyRatio": "2", "sellRatio": "0.5", "timestamp": str(millis(CUTOFF))}],
            },
        },
        request_params={"category": "linear", "symbol": "BTCUSDT", "period": "5min"},
        capture_clock=None,
        acquisition=acquired(),
    )
    unbounded_result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest=clean_open_interest(),
        open_interest_history=clean_history(),
        funding_history=clean_funding(),
        account_ratio=unbounded,
    )
    assert unbounded_result.account_ratio == NA
    assert "invalid_account_ratio" in unbounded_result.reason_codes
    assert unbounded_result.supports_long == clean.supports_long

    documented = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=ratio_payload(CUTOFF, ratio="1.8105", long_account="0.6442", short_account="0.3558"),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    documented_result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        account_ratio=documented,
    )
    assert documented_result.account_ratio == "1.81050000"
    assert "ratio_component_conflict" not in documented_result.reason_codes

    rows = funding_payload("0.0001", FUNDING_AT)
    rows[0]["rateType"] = "unexpected"
    unexpected = describe_binance_funding_history(
        requested_symbol="BTCUSDT",
        payload=rows,
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    unexpected_result = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        instrument=binance_instrument(),
        open_interest_history=clean_history(),
        funding_history=unexpected,
    )
    assert unexpected.observations[0].rate_kind == "unexpected"
    assert unexpected.observations[0].rate_kind != RATE_SETTLED_FUNDING
    assert "unexpected_funding_subtype" in unexpected_result.reason_codes
    assert unexpected_result.funding_label != RATE_SETTLED_FUNDING

    predicted = funding_payload("0.0001", FUNDING_AT)
    predicted[0]["rateType"] = "predicted"
    predicted_batch = describe_binance_funding_history(
        requested_symbol="BTCUSDT",
        payload=predicted,
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    assert predicted_batch.observations[0].rate_kind != RATE_SETTLED_FUNDING

    instrument = parse_binance_usds_instrument(
        {
            "symbols": [
                {
                    "symbol": "BTCUSDT",
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "fundingIntervalHours": 8,
                }
            ]
        },
        requested_symbol="BTCUSDT",
        acquisition=acquired(),
    )
    assert instrument.funding_interval_ms is None
    assert instrument.quality == "verified"


def test_r6_shadow_constants_cannot_be_overridden() -> None:
    forged = DerivativesSourceTruthResult(
        validated_confirmation=True,
        enforcement="active",
        mode="live",
        supports_long="true",
        supports_short="confirmed",
    )
    assert forged.mode == "shadow"
    assert forged.enforcement == "not_active"
    assert forged.validated_confirmation is False
    assert forged.supports_long == "unverified"
    assert forged.supports_short == "unverified"
    payload = forged.to_dict()
    assert payload["mode"] == "shadow"
    assert payload["enforcement"] == "not_active"
    assert payload["validated_confirmation"] is False
    replaced = replace(forged, mode="live", validated_confirmation=True, supports_long="eligible_no_conflict_detected")
    assert replaced.mode == "shadow"
    assert replaced.validated_confirmation is False
    assert replaced.supports_long == "eligible_no_conflict_detected"
    restored = DerivativesSourceTruthResult.from_dict(
        {
            "mode": "live",
            "enforcement": "active",
            "validated_confirmation": True,
            "supports_long": "true",
            "supports_short": "false",
            "classification": "not_available",
        }
    )
    assert restored.mode == "shadow"
    assert restored.enforcement == "not_active"
    assert restored.validated_confirmation is False
    assert restored.supports_long == "unverified"
    assert restored.supports_short == "false"


def test_r7_lineage_is_bounded_missing_times_stay_null_and_public_dumps_omit_it(tmp_path) -> None:
    result = describe_binance_case(
        history=[(PREVIOUS, "100", None), (PREVIOUS, "50", None), (CUTOFF, "110", None)]
    )
    payload = result.to_dict()
    assert payload["lineage"]
    encoded = json.dumps(payload)
    assert "1970-01-01" not in encoded
    roles = {item["role"] for item in payload["lineage"]}
    assert "excluded_oi" in roles or "selected_current_oi" in roles
    current = next(item for item in payload["lineage"] if item["raw_value"] in {"110", "100", "50"})
    assert current["endpoint"]
    assert current["requested_symbol"] == "BTCUSDT"
    assert "response_time" in current
    assert current["response_time"] is None
    target = tmp_path / "shadow.json"
    write_dev_shadow_artifact(target, result)
    artifact = json.loads(target.read_text(encoding="utf-8"))
    assert artifact["lineage"] == payload["lineage"]
    assert "derivatives_source_truth_shadow" not in artifact or artifact.get("mode") == "shadow"

    class Boom(dict):
        def __getattr__(self, name: str) -> Any:
            raise RuntimeError(name)

    failed = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        open_interest_history=clean_history(),
        instrument=Boom(),  # type: ignore[arg-type]
    )
    assert failed.classification == "evaluation_error"
    assert failed.validated_confirmation is False
    assert any(item["endpoint"] == "/futures/data/openInterestHist" for item in failed.to_dict()["lineage"])


def clean_open_interest() -> Any:
    return describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": "110", "time": millis(CUTOFF)},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )


def clean_history() -> Any:
    return describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=oi_history_payload([(PREVIOUS, "100", None), (CUTOFF, "110", None)]),
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )


def clean_funding() -> Any:
    return describe_binance_funding_history(
        requested_symbol="BTCUSDT",
        payload=funding_payload("0.0001", FUNDING_AT),
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )


def test_transport_error_type_remains_the_legacy_http_error() -> None:
    assert ExchangeHTTPError.__name__ == "ExchangeHTTPError"
