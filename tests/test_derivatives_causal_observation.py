"""P4 causal shadow observation. Fixtures are synthetic and offline."""

from __future__ import annotations

import asyncio
import json
from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from app.analytics.derivatives_causal_observation import (
    BOUNDARY_DESCRIPTION,
    OBSERVATION_MODE,
    OBSERVATION_ROLE,
    POLICY_VERSION,
    RESEARCH_CUTOFF_ROLE,
    STRATEGY_CUTOFF_ROLE,
    assess_frozen_bundle,
    build_synthetic_causal_fixture,
    build_verified_coverage_fixture,
    capture_observation_boundary,
    freeze_derivatives_bundle,
    measure_causal_evaluation_overhead,
    replay_dev_causal_shadow_artifact,
    study_from_records,
    study_from_symbol_results,
    write_dev_causal_shadow_artifact,
)
from app.analytics.derivatives_source_truth import (
    evaluate_derivatives_source_truth,
    evidence_visible_at_cutoff,
)
from app.cache.market_data_cache import CachedMarketDataClient, MarketDataCache
from app.data.derivatives_instrument import parse_binance_usds_instrument
from app.data.derivatives_source_evidence import (
    QUALITY_VERIFIED,
    UNIT_BASE_QUANTITY,
    TimeFact,
    apply_cache_delivery,
    capture_acquisition,
    describe_binance_account_ratio,
    describe_binance_funding_history,
    describe_binance_open_interest,
    describe_binance_open_interest_history,
    unavailable_batch,
)
from app.data.dtos import NA
from app.data.exchange_clients import BinanceFuturesClient, BybitLinearClient
from app.data.exchange_clients.base import ExchangeHTTPError
from app.lifecycle.service import observation_from_symbol_result
from app.pipeline.scanner_runner import ScannerRunner
from app.storage.database import SCHEMA_VERSION, connect_database, identify_schema_version, initialize_database
from tests.test_derivatives_source_truth import (
    CUTOFF,
    FUNDING_AT,
    PREVIOUS,
    STALE_FUNDING_AT,
    acquired,
    assert_cannot_confirm,
    binance_instrument,
    binance_instrument_payload,
    describe_binance_case,
    fixed_clock,
    funding_payload,
    millis,
    oi_history_payload,
    ratio_payload,
)
from tests.test_scanner_runner import FakeExchangeClient, SpyAlertAgent, _clean_target_intelligence, _config, _strategy_pullback_candles

pytestmark = pytest.mark.filterwarnings("ignore::DeprecationWarning")


def run(coro: Any) -> Any:
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean_targets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.scanner_runner.build_target_intelligence",
        lambda *args, **kwargs: _clean_target_intelligence(),
    )


def _oi_batch(metric_at: datetime, acquired_at: datetime, quantity: str = "110"):
    return describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": quantity, "time": millis(metric_at)},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(acquired_at),
    )


def _freeze(
    evidence: dict[str, Any],
    *,
    strategy: datetime,
    observed: datetime,
    instrument: Any = None,
    scan_mode: str = "full",
    symbol: str = "BTCUSDT",
    venue: str = "binance",
    retain_inputs: bool = True,
) -> Any:
    return freeze_derivatives_bundle(
        evidence,
        instrument=instrument,
        strategy_cutoff=strategy,
        observation=capture_observation_boundary(fixed_clock(observed)),
        symbol=symbol,
        requested_venue=venue,
        scan_mode=scan_mode,
        iteration_index=0,
        synthetic=True,
        assumptions=("synthetic fixture",),
        retain_inputs=retain_inputs,
        code_revision="synthetic-test",
    )


def _claim(record: Any, role: str, metric: str) -> Any:
    assessment = record.strategy_assessment if role == STRATEGY_CUTOFF_ROLE else record.research_assessment
    return next(item for item in assessment.availability if item.metric == metric)


def _population(study_dict: dict[str, Any], venue: str, scan_mode: str) -> dict[str, Any]:
    key = f"requested_venue={venue}|scan_mode={scan_mode}"
    return next(item for item in study_dict["populations"] if item["population"] == key)


def _rate(population: dict[str, Any], name: str, role: str) -> dict[str, Any]:
    return next(item for item in population["rates"] if item["name"] == name and item["cutoff_role"] == role)


def _direct(evidence: dict[str, Any], instrument: Any, cutoff: datetime) -> Any:
    return evaluate_derivatives_source_truth(
        decision_cutoff=cutoff,
        instrument=instrument,
        open_interest=evidence.get("open_interest"),
        open_interest_history=evidence.get("open_interest_history"),
        funding=evidence.get("funding_rate"),
        funding_history=evidence.get("funding_history"),
        account_ratio=evidence.get("long_short_ratio"),
    )


def _assert_same_evaluator(evidence: dict[str, Any], instrument: Any, strategy: datetime, observed: datetime) -> Any:
    bundle = _freeze(evidence, strategy=strategy, observed=observed, instrument=instrument)
    record = assess_frozen_bundle(bundle)
    assert record.strategy_assessment.evaluation == _direct(evidence, instrument, strategy)
    assert record.research_assessment.status == "assessed"
    assert record.research_assessment.evaluation == _direct(evidence, instrument, observed)
    assert record.validated_confirmation is False
    assert record.strategy_decision_cutoff == strategy.isoformat()
    assert record.research_observation_cutoff == observed.isoformat()
    assert record.strategy_assessment.evaluator_parameter == "decision_cutoff"
    assert record.strategy_assessment.cutoff_role == STRATEGY_CUTOFF_ROLE
    assert record.research_assessment.cutoff_role == RESEARCH_CUTOFF_ROLE
    return record


def _public_run(result: Any) -> dict[str, Any]:
    payload = result.model_dump(mode="json")
    payload.pop("runtime_stats", None)
    payload.pop("cache_stats", None)
    config = payload.get("config")
    if isinstance(config, dict):
        config.pop("derivatives_source_truth_shadow", None)
    for item in payload["results"]:
        item.pop("runtime_seconds", None)
    return payload


def _assert_cache_stats(off: Any, on: Any, *, extra_misses: int = 0) -> None:
    """Shadow may start one cancelled instrument lookup. It must not retain an extra entry."""

    left = dict(off.cache_stats)
    right = dict(on.cache_stats)
    assert right["entries"] == left["entries"]
    assert right["writes"] == left["writes"]
    assert right["errors"] == left["errors"]
    assert right["expired"] == left["expired"]
    assert right["hits"] == left["hits"]
    assert right["enabled"] == left["enabled"]
    assert right["misses"] - left["misses"] == extra_misses


def _scan(shadow: bool, **overrides: Any) -> Any:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF)
    runner = ScannerRunner(
        exchange_client=client,
        alert_agent=SpyAlertAgent(),
        clock=fixed_clock(CUTOFF),
        capture_clock=fixed_clock(CUTOFF + timedelta(seconds=5)),
    )
    config = _config(
        ["BTCUSDT"],
        decision_timestamp=CUTOFF,
        derivatives_source_truth_shadow=shadow,
        **overrides,
    )
    return client, run(runner.run(config))


def test_source_before_strategy_cutoff_is_rejected_until_the_later_observation() -> None:
    study = build_synthetic_causal_fixture()
    hero = study.attempts[0].record
    assert hero is not None
    strategy_at = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
    acquired_at = strategy_at + timedelta(seconds=5)
    observed_at = strategy_at + timedelta(seconds=6)
    metric_at = strategy_at - timedelta(minutes=1)
    assert hero.strategy_decision_cutoff == strategy_at.isoformat()
    assert hero.research_observation_cutoff == observed_at.isoformat()
    assert hero.strategy_decision_cutoff != hero.research_observation_cutoff
    assert hero.observation_role == OBSERVATION_ROLE
    assert hero.observation_boundary.description == BOUNDARY_DESCRIPTION
    assert hero.observation_boundary.meaning == OBSERVATION_MODE
    assert hero.policy_version == POLICY_VERSION
    assert hero.evidence_exchange == "binance_futures"
    assert hero.requested_venue == "binance"
    assert hero.acquisition_span.simultaneous_exchange_snapshot is False
    assert hero.acquisition_span.latest_metric_at == metric_at.isoformat()
    assert hero.acquisition_span.latest_acquired_at == acquired_at.isoformat()
    strategy = hero.strategy_assessment.evaluation
    research = hero.research_assessment.evaluation
    assert strategy is not None and research is not None
    assert strategy.classification == "acquired_after_cutoff"
    assert "acquisition_after_cutoff" in strategy.reason_codes
    assert research.classification == "eligible_no_conflict_detected"
    assert "acquisition_after_cutoff" not in research.reason_codes
    assert strategy.oi_change_pct == NA
    assert research.oi_change_pct == "10.00000000"
    assert research.oi_comparison_interval_ms == 300_000
    assert research.oi_interval_label == "300000ms"
    assert_cannot_confirm(strategy)
    assert_cannot_confirm(research)
    assert hero.validated_confirmation is False
    assert hero.mode == "shadow"
    assert hero.enforcement == "not_active"
    assert hero.trading_performance_claim is False
    assert "production_strategy_correction" in hero.strategy_assessment.does_not_establish
    assert "retroactive_strategy_availability" in hero.research_assessment.does_not_establish
    oi_strategy = _claim(hero, STRATEGY_CUTOFF_ROLE, "open_interest")
    oi_research = _claim(hero, RESEARCH_CUTOFF_ROLE, "open_interest")
    assert oi_strategy.collector_acquired_before_cutoff == "false"
    assert oi_research.collector_acquired_before_cutoff == "true"
    assert oi_strategy.cache_retained_before_cutoff == "not_recorded"
    assert "ttl_is_source_freshness" in oi_strategy.does_not_establish
    instrument_strategy = _claim(hero, STRATEGY_CUTOFF_ROLE, "instrument_metadata")
    assert instrument_strategy.collector_acquired_before_cutoff == "false"
    assert instrument_strategy.consumer_delivered_before_cutoff == "not_recorded"
    assert "synthetic_binance_funding_interval_ms" in hero.assumptions
    assert hero.synthetic is True


def test_cutoff_equality_and_one_microsecond_use_the_existing_reason() -> None:
    metric = CUTOFF - timedelta(minutes=1)
    observed = CUTOFF + timedelta(seconds=10)
    equal = assess_frozen_bundle(
        _freeze({"open_interest": _oi_batch(metric, observed)}, strategy=CUTOFF, observed=observed)
    )
    late = assess_frozen_bundle(
        _freeze(
            {"open_interest": _oi_batch(metric, observed + timedelta(microseconds=1))},
            strategy=CUTOFF,
            observed=observed,
        )
    )
    exact_strategy = assess_frozen_bundle(
        _freeze({"open_interest": _oi_batch(metric, CUTOFF)}, strategy=CUTOFF, observed=observed)
    )
    one_microsecond = assess_frozen_bundle(
        _freeze(
            {"open_interest": _oi_batch(metric, CUTOFF + timedelta(microseconds=1))},
            strategy=CUTOFF,
            observed=observed,
        )
    )
    assert "acquisition_after_cutoff" not in equal.research_assessment.evaluation.reason_codes
    assert "acquisition_after_cutoff" in late.research_assessment.evaluation.reason_codes
    assert "acquisition_after_cutoff" not in exact_strategy.strategy_assessment.evaluation.reason_codes
    assert "acquisition_after_cutoff" in one_microsecond.strategy_assessment.evaluation.reason_codes
    assert exact_strategy.strategy_decision_cutoff == CUTOFF.isoformat()


def test_bad_timestamps_and_impossible_chronology_stay_unverified() -> None:
    missing = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": "110"},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(CUTOFF),
    )
    malformed = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "BTCUSDT", "openInterest": "110", "time": "2026-01-15T12:00:00+99:00"},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(CUTOFF),
    )
    ambiguous = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={
            "symbol": "BTCUSDT",
            "openInterest": "110",
            "time": millis(CUTOFF),
            "timestamp": millis(CUTOFF - timedelta(minutes=1)),
        },
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(CUTOFF),
    )
    assert "missing_metric_timestamp" in missing.observations[0].reason_codes
    assert "malformed_metric_timestamp" in malformed.observations[0].reason_codes
    assert "ambiguous_metric_timestamp" in ambiguous.observations[0].reason_codes
    for batch in (missing, malformed, ambiguous):
        record = assess_frozen_bundle(_freeze({"open_interest": batch}, strategy=CUTOFF, observed=CUTOFF + timedelta(seconds=1)))
        assert_cannot_confirm(record.strategy_assessment.evaluation)
        assert_cannot_confirm(record.research_assessment.evaluation)

    base = _oi_batch(CUTOFF - timedelta(minutes=1), CUTOFF)
    observation = base.observations[0]
    after_acquisition = replace(
        observation,
        metric_time=TimeFact("metric", "open_interest_snapshot_time", CUTOFF + timedelta(seconds=1), "late", QUALITY_VERIFIED, ()),
    )
    response_late = replace(
        observation,
        response_time=TimeFact("response", "exchange_response", CUTOFF + timedelta(seconds=2), "late-response", QUALITY_VERIFIED, ()),
    )
    metric_after_response = replace(
        observation,
        metric_time=TimeFact("metric", "open_interest_snapshot_time", CUTOFF - timedelta(seconds=1), "after-response", QUALITY_VERIFIED, ()),
        response_time=TimeFact("response", "exchange_response", CUTOFF - timedelta(seconds=2), "response", QUALITY_VERIFIED, ()),
    )
    after_acquisition_batch = replace(base, observations=(after_acquisition,))
    response_batch = replace(base, observations=(response_late,), response_time=response_late.response_time)
    metric_response_batch = replace(base, observations=(metric_after_response,), response_time=metric_after_response.response_time)
    observed = CUTOFF + timedelta(minutes=1)
    after_record = _assert_same_evaluator({"open_interest": after_acquisition_batch}, None, CUTOFF, observed)
    response_record = _assert_same_evaluator({"open_interest": response_batch}, None, CUTOFF, observed)
    metric_response_record = _assert_same_evaluator({"open_interest": metric_response_batch}, None, CUTOFF, observed)
    assert "metric_after_acquisition" in after_record.strategy_assessment.evaluation.reason_codes
    assert "response_after_acquisition" in response_record.strategy_assessment.evaluation.reason_codes
    assert "metric_after_response" in metric_response_record.strategy_assessment.evaluation.reason_codes


def test_cache_delivery_chronology_does_not_rewrite_the_strategy_cutoff() -> None:
    metric = CUTOFF - timedelta(minutes=1)
    acquired_at = CUTOFF - timedelta(seconds=30)
    observed = CUTOFF + timedelta(seconds=10)
    delivered_late = observed + timedelta(seconds=1)
    base = _oi_batch(metric, acquired_at)
    late_delivery = apply_cache_delivery(
        base,
        delivery_kind="hit",
        delivered_at=delivered_late,
        file_rehydrated=False,
        legacy_value=None,
        metric="open_interest",
        endpoint="/fapi/v1/openInterest",
    )
    early_delivery = apply_cache_delivery(
        base,
        delivery_kind="hit",
        delivered_at=acquired_at - timedelta(seconds=1),
        file_rehydrated=False,
        legacy_value=None,
        metric="open_interest",
        endpoint="/fapi/v1/openInterest",
    )
    late_record = _assert_same_evaluator({"open_interest": late_delivery}, binance_instrument(acquired_at), CUTOFF, observed)
    early_record = _assert_same_evaluator({"open_interest": early_delivery}, None, CUTOFF, observed)
    late_claim = _claim(late_record, RESEARCH_CUTOFF_ROLE, "open_interest")
    strategy_claim = _claim(late_record, STRATEGY_CUTOFF_ROLE, "open_interest")
    assert late_claim.consumer_delivered_before_cutoff == "false"
    assert "delivery_after_observation" in late_claim.reason_codes
    assert strategy_claim.collector_acquired_before_cutoff == "true"
    assert strategy_claim.consumer_delivered_before_cutoff == "false"
    assert "consumer_delivery_after_strategy_cutoff" in strategy_claim.reason_codes
    assert strategy_claim.later_cache_retrieval_of_earlier_acquisition == "true"
    assert "strategy_consumed_before_cutoff" in strategy_claim.does_not_establish
    assert "delivery_before_acquisition" in early_record.strategy_assessment.evaluation.reason_codes
    study = study_from_records((late_record,), synthetic=True)
    population = _population(study.to_dict(), "binance", "full")
    research_admissible = _rate(population, "causally_admissible_observations", RESEARCH_CUTOFF_ROLE)
    strategy_admissible = _rate(population, "causally_admissible_observations", STRATEGY_CUTOFF_ROLE)
    assert research_admissible["numerator"] == 0
    assert strategy_admissible["denominator"] == research_admissible["denominator"]
    assert strategy_admissible["cutoff_role"] != research_admissible["cutoff_role"]


def test_metadata_after_the_strategy_cutoff_cannot_upgrade_units() -> None:
    metric_acquired = CUTOFF - timedelta(seconds=30)
    instrument_acquired = CUTOFF + timedelta(seconds=5)
    observed = CUTOFF + timedelta(seconds=6)
    current = CUTOFF - timedelta(minutes=1)
    previous = current - timedelta(minutes=5)
    acquisition = acquired(metric_acquired)
    evidence = {
        "open_interest": describe_binance_open_interest(
            requested_symbol="BTCUSDT",
            payload={"symbol": "BTCUSDT", "openInterest": "110", "time": millis(current)},
            request_params={"symbol": "BTCUSDT"},
            capture_clock=None,
            acquisition=acquisition,
        ),
        "open_interest_history": describe_binance_open_interest_history(
            requested_symbol="BTCUSDT",
            payload=oi_history_payload([(previous, "100", None), (current, "110", None)]),
            request_params={"symbol": "BTCUSDT", "period": "5m"},
            capture_clock=None,
            acquisition=acquisition,
        ),
        "funding_history": describe_binance_funding_history(
            requested_symbol="BTCUSDT",
            payload=funding_payload("0.0001", FUNDING_AT),
            request_params={"symbol": "BTCUSDT"},
            capture_clock=None,
            acquisition=acquisition,
        ),
        "long_short_ratio": describe_binance_account_ratio(
            requested_symbol="BTCUSDT",
            payload=ratio_payload(current),
            request_params={"symbol": "BTCUSDT", "period": "5m"},
            capture_clock=None,
            acquisition=acquisition,
        ),
    }
    instrument = replace(
        binance_instrument(metric_acquired),
        acquisition=acquired(instrument_acquired),
        funding_interval_ms=8 * 60 * 60 * 1000,
    )
    record = _assert_same_evaluator(evidence, instrument, CUTOFF, observed)
    strategy_visible = evidence_visible_at_cutoff(
        decision_cutoff=CUTOFF,
        instrument=instrument,
        open_interest=evidence["open_interest"],
        open_interest_history=evidence["open_interest_history"],
    )
    research_visible = evidence_visible_at_cutoff(
        decision_cutoff=observed,
        instrument=instrument,
        open_interest=evidence["open_interest"],
        open_interest_history=evidence["open_interest_history"],
    )
    assert "instrument_acquired_after_cutoff" in strategy_visible["instrument"].reason_codes
    assert strategy_visible["open_interest"].observations[0].quantities[0].unit != UNIT_BASE_QUANTITY
    assert "instrument_acquired_after_cutoff" not in research_visible["instrument"].reason_codes
    assert research_visible["open_interest"].observations[0].quantities[0].unit == UNIT_BASE_QUANTITY
    assert record.strategy_assessment.evaluation.classification != "eligible_no_conflict_detected"
    assert record.research_assessment.evaluation.classification == "eligible_no_conflict_detected"
    assert record.strategy_decision_cutoff == CUTOFF.isoformat()


def test_clock_failure_regression_and_frozen_bundle_do_not_invent_a_cutoff(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden() -> datetime:
        raise AssertionError("fallback clock")

    monkeypatch.setattr("app.data.derivatives_source_evidence.default_capture_clock", forbidden)
    failed = capture_observation_boundary(None)
    raised = capture_observation_boundary(lambda: (_ for _ in ()).throw(RuntimeError("clock down")))
    invalid = capture_observation_boundary(lambda: "not-a-timestamp")  # type: ignore[arg-type, return-value]
    naive = capture_observation_boundary(lambda: datetime(2026, 1, 15, 12, 0, 6))
    assert failed.quality != QUALITY_VERIFIED
    assert "observation_clock_failed" in failed.reason_codes
    assert "observation_clock_failed" in raised.reason_codes
    assert "observation_clock_failed" in invalid.reason_codes
    assert naive.cutoff == datetime(2026, 1, 15, 12, 0, 6, tzinfo=UTC)

    bundle = _freeze({"open_interest": _oi_batch(CUTOFF - timedelta(minutes=1), CUTOFF)}, strategy=CUTOFF, observed=CUTOFF)
    regressed = freeze_derivatives_bundle(
        {"open_interest": _oi_batch(CUTOFF - timedelta(minutes=1), CUTOFF - timedelta(minutes=2))},
        instrument=None,
        strategy_cutoff=CUTOFF,
        observation=capture_observation_boundary(fixed_clock(CUTOFF - timedelta(seconds=1))),
        symbol="BTCUSDT",
        requested_venue="binance",
        scan_mode="full",
        iteration_index=0,
        synthetic=True,
        retain_inputs=True,
        code_revision="synthetic-test",
    )
    monkeypatch.setattr(
        "app.analytics.derivatives_causal_observation.capture_observation_boundary",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("assess sampled a clock")),
    )
    record = assess_frozen_bundle(regressed)
    assert record.research_assessment.status == "unavailable"
    assert record.research_assessment.evaluation is None
    assert "observation_clock_regression" in record.research_assessment.reason_codes
    assert record.strategy_decision_cutoff == CUTOFF.isoformat()
    assert record.research_observation_cutoff == NA
    assert record.strategy_assessment.evaluation is not None
    source = {"open_interest": bundle.evidence_map()["open_interest"]}
    frozen = _freeze(source, strategy=CUTOFF, observed=CUTOFF + timedelta(seconds=1))
    source["open_interest"] = _oi_batch(CUTOFF, CUTOFF, quantity="999")
    assert frozen.evidence_map()["open_interest"].observations[0].quantities[0].raw_value == "110"


def test_historical_cutoffs_do_not_become_admissible_at_replay_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    historical_strategy = datetime(2020, 1, 1, tzinfo=UTC)
    historical_observation = datetime(2020, 1, 1, 0, 1, tzinfo=UTC)
    metric = datetime(2019, 12, 31, tzinfo=UTC)
    acquired_today = datetime(2026, 10, 21, tzinfo=UTC)
    record = assess_frozen_bundle(
        _freeze(
            {"open_interest": _oi_batch(metric, acquired_today)},
            strategy=historical_strategy,
            observed=historical_observation,
        )
    )
    assert "acquisition_after_cutoff" in record.strategy_assessment.evaluation.reason_codes
    assert "acquisition_after_cutoff" in record.research_assessment.evaluation.reason_codes
    study = study_from_records((record,), synthetic=True, code_revision="synthetic-test")
    target = tmp_path / "historical.json"
    write_dev_causal_shadow_artifact(target, study)
    monkeypatch.setattr(
        "app.analytics.derivatives_causal_observation.time.time",
        lambda: (_ for _ in ()).throw(AssertionError("replay wall clock")),
    )
    replayed = replay_dev_causal_shadow_artifact(target)
    replay_record = replayed.attempts[0].record
    assert replay_record is not None
    assert replay_record.strategy_decision_cutoff == historical_strategy.isoformat()
    assert replay_record.research_observation_cutoff == historical_observation.isoformat()
    assert replay_record.strategy_assessment.evaluation.classification == record.strategy_assessment.evaluation.classification
    assert "acquisition_after_cutoff" in replay_record.research_assessment.evaluation.reason_codes


def test_cache_provenance_matrix_keeps_acquisition_and_isolates_keys(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        symbol = request.url.params.get("symbol", "BTCUSDT")
        calls.append((request.url.path, symbol + ":" + request.url.params.get("period", "")))
        if request.url.path == "/fapi/v1/openInterest":
            quantity = "110" if symbol == "BTCUSDT" else "7"
            return httpx.Response(200, json={"symbol": symbol, "openInterest": quantity, "time": millis(CUTOFF - timedelta(minutes=1))})
        if request.url.path == "/futures/data/openInterestHist":
            return httpx.Response(
                200,
                json=oi_history_payload([(PREVIOUS, "100", None), (CUTOFF - timedelta(minutes=1), "110", None)]),
            )
        if request.url.path == "/fapi/v1/exchangeInfo":
            return httpx.Response(200, json=binance_instrument_payload(symbol))
        if request.url.path == "/v5/market/open-interest":
            return httpx.Response(
                200,
                json={
                    "retCode": 0,
                    "retMsg": "OK",
                    "time": millis(CUTOFF),
                    "result": {
                        "category": "linear",
                        "symbol": symbol,
                        "list": [{"openInterest": "80", "timestamp": millis(CUTOFF - timedelta(minutes=1))}],
                    },
                },
            )
        return httpx.Response(404, json={"missing": request.url.path})

    async def scenario() -> dict[str, Any]:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://market.test")
        binance = BinanceFuturesClient(http_client=http, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)
        bybit = BybitLinearClient(http_client=http, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)
        shared = MarketDataCache(now=lambda: 1_000_000.0)
        binance_cached = CachedMarketDataClient(binance, shared)
        bybit_cached = CachedMarketDataClient(bybit, shared)
        direct_client = CachedMarketDataClient(binance, MarketDataCache(enabled=False))
        direct = await direct_client.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF - timedelta(seconds=20)))
        first = await binance_cached.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF - timedelta(seconds=30)))
        second = await binance_cached.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF + timedelta(seconds=5)))
        eth = await binance_cached.get_open_interest_with_evidence("ETHUSDT", capture_clock=fixed_clock(CUTOFF - timedelta(seconds=10)))
        bybit_oi = await bybit_cached.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF - timedelta(seconds=15)))
        history_5m = await binance_cached.get_open_interest_history_with_evidence(
            "BTCUSDT", limit=2, period="5m", capture_clock=fixed_clock(CUTOFF - timedelta(seconds=25))
        )
        history_15m = await binance_cached.get_open_interest_history_with_evidence(
            "BTCUSDT", limit=2, period="15m", capture_clock=fixed_clock(CUTOFF - timedelta(seconds=24))
        )
        instrument_first = await binance_cached.get_instrument_metadata("BTCUSDT", capture_clock=fixed_clock(CUTOFF - timedelta(seconds=12)))
        instrument_second = await binance_cached.get_instrument_metadata("BTCUSDT", capture_clock=fixed_clock(CUTOFF + timedelta(hours=1)))
        legacy_cache = MarketDataCache(now=lambda: 1_000_000.0)
        legacy_client = CachedMarketDataClient(binance, legacy_cache)
        legacy_value = await legacy_client.get_open_interest("BTCUSDT")
        before_legacy_calls = len(calls)
        legacy_hit = await legacy_client.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        after_legacy_calls = len(calls)
        samples = {"count": 0}

        def failing_delivery_clock() -> datetime:
            samples["count"] += 1
            if samples["count"] > 1:
                raise RuntimeError("delivery clock failed")
            return CUTOFF - timedelta(seconds=40)

        delivery_cache = MarketDataCache(now=lambda: 2_000_000.0)
        delivery_client = CachedMarketDataClient(binance, delivery_cache)
        before_delivery_calls = len(calls)
        failed_delivery = await delivery_client.get_open_interest_with_evidence("BTCUSDT", capture_clock=failing_delivery_clock)
        recovered = await delivery_client.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF + timedelta(seconds=2)))
        after_delivery_calls = len(calls)
        file_cache = MarketDataCache(file_path=tmp_path / "oi.json", now=lambda: 3_000_000.0)
        file_client = CachedMarketDataClient(binance, file_cache)
        await file_client.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF - timedelta(seconds=50)))
        before_reload = len(calls)
        reloaded = CachedMarketDataClient(
            binance,
            MarketDataCache(file_path=tmp_path / "oi.json", now=lambda: 3_000_010.0),
        )
        restored = await reloaded.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF + timedelta(hours=2)))
        after_reload = len(calls)
        await binance.aclose()
        return {
            "direct": direct,
            "first": first,
            "second": second,
            "eth": eth,
            "bybit": bybit_oi,
            "history_5m": history_5m,
            "history_15m": history_15m,
            "instrument_first": instrument_first,
            "instrument_second": instrument_second,
            "legacy_value": legacy_value,
            "legacy_hit": legacy_hit,
            "legacy_calls": (before_legacy_calls, after_legacy_calls),
            "failed_delivery": failed_delivery,
            "recovered": recovered,
            "delivery_calls": (before_delivery_calls, after_delivery_calls),
            "restored": restored,
            "reload_calls": (before_reload, after_reload),
        }

    found = run(scenario())
    assert found["direct"].evidence.cache.delivery_kind == "not_applicable"
    assert found["direct"].evidence.acquisition.acquired_at == CUTOFF - timedelta(seconds=20)
    direct_record = assess_frozen_bundle(
        _freeze({"open_interest": found["direct"].evidence}, strategy=CUTOFF, observed=CUTOFF + timedelta(seconds=1))
    )
    direct_claim = _claim(direct_record, STRATEGY_CUTOFF_ROLE, "open_interest")
    assert direct_claim.consumer_delivered_before_cutoff == "true"
    assert "direct_adapter_response_is_not_cache_delivery" in direct_claim.reason_codes
    assert found["first"].evidence.cache.delivery_kind == "miss"
    assert found["second"].evidence.cache.delivery_kind == "hit"
    assert found["second"].evidence.acquisition.acquired_at == CUTOFF - timedelta(seconds=30)
    assert found["second"].evidence.cache.original_acquired_at == CUTOFF - timedelta(seconds=30)
    assert found["second"].evidence.cache.delivered_at == CUTOFF + timedelta(seconds=5)
    assert found["second"].value.open_interest == Decimal("110")
    hit_record = assess_frozen_bundle(
        _freeze(
            {"open_interest": found["second"].evidence},
            strategy=CUTOFF,
            observed=CUTOFF + timedelta(seconds=6),
            instrument=found["instrument_first"],
        )
    )
    hit_claim = _claim(hit_record, STRATEGY_CUTOFF_ROLE, "open_interest")
    assert hit_claim.collector_acquired_before_cutoff == "true"
    assert hit_claim.consumer_delivered_before_cutoff == "false"
    assert hit_claim.later_cache_retrieval_of_earlier_acquisition == "true"
    assert hit_claim.cache_retained_before_cutoff == "not_recorded"
    assert "cache_retention_instant_not_on_evidence_clock" in hit_claim.reason_codes
    assert "strategy_consumed_before_cutoff" in hit_claim.does_not_establish
    assert found["eth"].value.open_interest == Decimal("7")
    assert found["eth"].evidence.observations[0].identity.requested_symbol == "ETHUSDT"
    assert found["bybit"].evidence.observations[0].exchange == "bybit_linear"
    assert found["bybit"].value.open_interest == Decimal("80")
    assert found["history_5m"].evidence.request_params != found["history_15m"].evidence.request_params
    assert found["instrument_second"].acquisition.acquired_at == found["instrument_first"].acquisition.acquired_at
    instrument_record = assess_frozen_bundle(
        _freeze({}, strategy=CUTOFF, observed=CUTOFF + timedelta(seconds=1), instrument=found["instrument_second"])
    )
    instrument_claim = _claim(instrument_record, STRATEGY_CUTOFF_ROLE, "instrument_metadata")
    assert instrument_claim.consumer_delivered_before_cutoff == "not_recorded"
    assert instrument_claim.cache_retained_before_cutoff == "not_recorded"
    assert found["legacy_hit"].value.open_interest == found["legacy_value"].open_interest
    assert found["legacy_calls"][0] == found["legacy_calls"][1]
    assert "uninstrumented_cache_entry_lacks_acquisition_observation" in found["legacy_hit"].evidence.reason_codes
    assert found["legacy_hit"].evidence.acquisition.acquired_at is None
    legacy_record = assess_frozen_bundle(
        _freeze({"open_interest": found["legacy_hit"].evidence}, strategy=CUTOFF, observed=CUTOFF + timedelta(seconds=1))
    )
    legacy_claim = _claim(legacy_record, RESEARCH_CUTOFF_ROLE, "open_interest")
    assert legacy_claim.collector_acquired_before_cutoff == "unverified"
    assert "cache_value_does_not_restore_acquisition" in legacy_claim.reason_codes
    assert found["failed_delivery"].value.open_interest == Decimal("110")
    assert found["failed_delivery"].evidence.cache.delivered_at is None
    assert found["failed_delivery"].evidence.acquisition.acquired_at == CUTOFF - timedelta(seconds=40)
    assert found["delivery_calls"][1] == found["delivery_calls"][0] + 1
    assert found["recovered"].evidence.acquisition.acquired_at == CUTOFF - timedelta(seconds=40)
    assert found["recovered"].evidence.cache.delivered_at == CUTOFF + timedelta(seconds=2)
    failed_record = assess_frozen_bundle(
        _freeze({"open_interest": found["failed_delivery"].evidence}, strategy=CUTOFF, observed=CUTOFF + timedelta(seconds=1))
    )
    assert "missing_cache_delivery_capture" in _claim(failed_record, STRATEGY_CUTOFF_ROLE, "open_interest").reason_codes
    assert found["restored"].value.open_interest == Decimal("110")
    assert found["restored"].evidence.acquisition.acquired_at is None
    assert "legacy_cache_entry_lacks_acquisition_observation" in found["restored"].evidence.reason_codes
    assert found["reload_calls"][0] == found["reload_calls"][1]


def test_semantic_cases_stay_on_the_existing_evaluator() -> None:
    observed = CUTOFF + timedelta(seconds=1)
    notional = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=[
            {"symbol": "BTCUSDT", "sumOpenInterestValue": "100", "timestamp": millis(PREVIOUS)},
            {"symbol": "BTCUSDT", "sumOpenInterestValue": "110", "timestamp": millis(CUTOFF)},
        ],
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    notional_record = _assert_same_evaluator(
        {"open_interest_history": notional},
        binance_instrument(),
        CUTOFF,
        observed,
    )
    assert notional_record.strategy_assessment.evaluation.oi_change_pct == NA
    assert "notional_not_position_growth" in notional_record.strategy_assessment.evaluation.reason_codes

    unknown = _assert_same_evaluator({"open_interest": _oi_batch(CUTOFF, CUTOFF)}, None, CUTOFF, observed)
    assert unknown.strategy_assessment.evaluation.classification == "unverified"
    assert "unit_unverified" in unknown.strategy_assessment.evaluation.reason_codes or unknown.strategy_assessment.evaluation.oi_change_pct == NA

    conflict_payload = oi_history_payload([(PREVIOUS, "100", None), (CUTOFF, "110", None), (CUTOFF, "999", None)])
    reversed_payload = list(reversed(conflict_payload))
    conflict = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=conflict_payload,
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    reversed_conflict = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=reversed_payload,
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    conflict_record = _assert_same_evaluator({"open_interest_history": conflict}, binance_instrument(), CUTOFF, observed)
    reversed_record = _assert_same_evaluator({"open_interest_history": reversed_conflict}, binance_instrument(), CUTOFF, observed)
    assert conflict_record.strategy_assessment.evaluation.oi_change_pct == NA
    assert "duplicate_conflict" in conflict_record.strategy_assessment.evaluation.reason_codes
    assert reversed_record.strategy_assessment.evaluation == conflict_record.strategy_assessment.evaluation

    wrong_symbol = describe_binance_open_interest(
        requested_symbol="BTCUSDT",
        payload={"symbol": "ETHUSDT", "openInterest": "110", "time": millis(CUTOFF)},
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    identity = _assert_same_evaluator({"open_interest": wrong_symbol}, binance_instrument(), CUTOFF, observed)
    assert "symbol_conflict" in identity.strategy_assessment.evaluation.reason_codes

    funding = describe_binance_funding_history(
        requested_symbol="BTCUSDT",
        payload=funding_payload("0.0001", FUNDING_AT),
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(),
    )
    special = replace(funding, observations=(replace(funding.observations[0], rate_kind="predicted_funding"),))
    special_record = _assert_same_evaluator({"funding_history": special}, binance_instrument(), CUTOFF, observed)
    assert "unexpected_funding_subtype" in special_record.strategy_assessment.evaluation.reason_codes
    parsed = binance_instrument()
    assert parsed.funding_interval_ms is None
    cadence = _assert_same_evaluator({"funding_history": funding}, parsed, CUTOFF, observed)
    assert "funding_cadence_unknown" in cadence.strategy_assessment.evaluation.reason_codes

    stale_funding = describe_binance_funding_history(
        requested_symbol="BTCUSDT",
        payload=funding_payload("0.0001", STALE_FUNDING_AT),
        request_params={"symbol": "BTCUSDT"},
        capture_clock=None,
        acquisition=acquired(CUTOFF),
    )
    stale_delivered = apply_cache_delivery(
        stale_funding,
        delivery_kind="hit",
        delivered_at=CUTOFF,
        file_rehydrated=False,
        legacy_value=None,
        metric="funding",
        endpoint="/fapi/v1/fundingRate",
    )
    stale_record = _assert_same_evaluator(
        {"funding_history": stale_delivered},
        replace(binance_instrument(), funding_interval_ms=8 * 60 * 60 * 1000),
        CUTOFF,
        observed,
    )
    assert "stale_for_current_context" in stale_record.strategy_assessment.evaluation.reason_codes
    assert _claim(stale_record, STRATEGY_CUTOFF_ROLE, "funding_history").consumer_delivered_before_cutoff == "true"

    invalid_ratio = describe_binance_account_ratio(
        requested_symbol="BTCUSDT",
        payload=[{"symbol": "BTCUSDT", "longAccount": "0.4", "shortAccount": "0", "timestamp": millis(CUTOFF)}],
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(),
    )
    base_evidence = {
        "open_interest": _oi_batch(CUTOFF, CUTOFF),
        "open_interest_history": describe_binance_open_interest_history(
            requested_symbol="BTCUSDT",
            payload=oi_history_payload([(PREVIOUS, "100", None), (CUTOFF, "110", None)]),
            request_params={"symbol": "BTCUSDT", "period": "5m"},
            capture_clock=None,
            acquisition=acquired(),
        ),
        "funding_history": funding,
    }
    clean = _assert_same_evaluator(base_evidence, binance_instrument(), CUTOFF, observed)
    invalid = _assert_same_evaluator({**base_evidence, "long_short_ratio": invalid_ratio}, binance_instrument(), CUTOFF, observed)
    assert invalid.strategy_assessment.evaluation.oi_change_pct == clean.strategy_assessment.evaluation.oi_change_pct
    assert "invalid_denominator" in invalid.strategy_assessment.evaluation.reason_codes
    zero = describe_binance_case(history=[(PREVIOUS, "0", None), (CUTOFF, "110", None)])
    assert "zero_baseline" in zero.reason_codes
    assert zero.oi_change_pct == NA
    price = evaluate_derivatives_source_truth(
        decision_cutoff=CUTOFF,
        price_change_pct=Decimal("10"),
        price_quality="verified",
    )
    price_record = assess_frozen_bundle(
        _freeze({}, strategy=CUTOFF, observed=observed),
        strategy_evaluation=price,
    )
    assert "price_only_not_derivatives_support" in price.reason_codes
    assert price_record.validated_confirmation is False
    assert price_record.research_assessment.evaluation.validated_confirmation is False


def test_study_denominators_replay_and_shadow_constants(tmp_path: Path) -> None:
    study = build_synthetic_causal_fixture()
    payload = study.to_dict()
    assert payload["synthetic"] is True
    assert payload["mode"] == "shadow"
    assert payload["enforcement"] == "not_active"
    assert payload["validated_confirmation"] is False
    assert payload["trading_performance_claim"] is False
    assert payload["percentages_are_not_additive"] is True
    assert payload["replay_complete"] is True
    assert payload["complete_verified_coverage"] is False
    full = _population(payload, "binance", "full")
    fast = _population(payload, "binance", "fast")
    assert full["attempt_disposition_sum"] == 2
    assert full["metric_opportunities"] == 10
    assert full["metric_disposition_sum"] == 10
    assert _rate(full, "attempt_disposition_assessed_both", "not_cutoff_specific")["numerator"] == 1
    assert _rate(full, "attempt_disposition_unassessed", "not_cutoff_specific")["numerator"] == 1
    assert _rate(full, "metric_disposition_received", "not_cutoff_specific") == {
        **_rate(full, "metric_disposition_received", "not_cutoff_specific"),
        "numerator": 5,
        "denominator": 10,
    }
    assert _rate(full, "causally_admissible_observations", STRATEGY_CUTOFF_ROLE)["numerator"] == 0
    assert _rate(full, "causally_admissible_observations", RESEARCH_CUTOFF_ROLE)["value"] == "1.00000000"
    assert _rate(full, "fresh_observations", STRATEGY_CUTOFF_ROLE)["value"] == NA
    assert _rate(fast, "metric_disposition_skipped", "not_cutoff_specific")["numerator"] == 3
    assert _rate(fast, "metric_disposition_received", "not_cutoff_specific")["numerator"] == 2
    assert all(item["percentages_are_not_additive"] is True for item in full["overlapping_reason_counts"])
    assert any(item["name"] == "acquisition_after_cutoff" for item in full["overlapping_reason_counts"])
    empty = study_from_records(()).to_dict()
    assert empty["populations"][0]["rates"][0]["value"] == NA
    assert empty["complete_verified_coverage"] is False
    verified = build_verified_coverage_fixture()
    verified_payload = verified.to_dict()
    assert verified_payload["complete_verified_coverage"] is True
    assert verified_payload["replay_complete"] is True
    target = tmp_path / "causal.json"
    write_dev_causal_shadow_artifact(target, verified)
    replayed = replay_dev_causal_shadow_artifact(target)
    assert replayed.attempts[0].record.strategy_assessment.evaluation == verified.attempts[0].record.strategy_assessment.evaluation
    assert replayed.attempts[0].record.research_assessment.evaluation == verified.attempts[0].record.research_assessment.evaluation
    hostile = replace(
        verified.attempts[0].record,
        mode="live",
        enforcement="active",
        validated_confirmation=True,
        trading_performance_claim=True,
        observation_role="strategy_decision",
        policy_version="other",
    )
    assert hostile.mode == "shadow"
    assert hostile.enforcement == "not_active"
    assert hostile.validated_confirmation is False
    assert hostile.trading_performance_claim is False
    assert hostile.observation_role == OBSERVATION_ROLE
    assert hostile.policy_version == POLICY_VERSION
    raw = json.loads(target.read_text(encoding="utf-8"))
    raw["mode"] = "live"
    raw["validated_confirmation"] = True
    raw["trading_performance_claim"] = True
    target.write_text(json.dumps(raw), encoding="utf-8")
    coerced = replay_dev_causal_shadow_artifact(target)
    assert coerced.mode == "shadow"
    assert coerced.validated_confirmation is False
    assert coerced.trading_performance_claim is False
    diagnostic = study_from_records(
        (assess_frozen_bundle(_freeze({"open_interest": _oi_batch(CUTOFF, CUTOFF)}, strategy=CUTOFF, observed=CUTOFF, retain_inputs=False)),),
        synthetic=True,
    )
    assert diagnostic.to_dict()["replay_complete"] is False
    assert diagnostic.to_dict()["complete_verified_coverage"] is False
    diagnostic_path = tmp_path / "diagnostic.json"
    write_dev_causal_shadow_artifact(diagnostic_path, diagnostic)
    with pytest.raises(ValueError, match="artifact_incomplete"):
        replay_dev_causal_shadow_artifact(diagnostic_path)
    raw["attempts"][0]["inputs"] = None
    raw["replay_complete"] = True
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="artifact_incomplete"):
        replay_dev_causal_shadow_artifact(broken)
    with pytest.raises(ValueError):
        write_dev_causal_shadow_artifact(tmp_path / "causal.txt", verified)
    rows = [
        {"symbol": "BTCUSDT", "sumOpenInterest": str(index + 1), "timestamp": millis(CUTOFF - timedelta(minutes=5 * index))}
        for index in range(40)
    ]
    wide = describe_binance_open_interest_history(
        requested_symbol="BTCUSDT",
        payload=rows,
        request_params={"symbol": "BTCUSDT", "period": "5m"},
        capture_clock=None,
        acquisition=acquired(CUTOFF),
    )
    wide_record = assess_frozen_bundle(
        _freeze({"open_interest_history": wide}, strategy=CUTOFF, observed=CUTOFF + timedelta(seconds=1), instrument=binance_instrument())
    )
    assert wide_record.strategy_assessment.evaluation.lineage_omitted_count > 0
    wide_study = study_from_records((wide_record,), synthetic=True, code_revision="synthetic-test")
    wide_path = tmp_path / "wide.json"
    write_dev_causal_shadow_artifact(wide_path, wide_study)
    wide_replay = replay_dev_causal_shadow_artifact(wide_path)
    assert wide_replay.attempts[0].record.strategy_assessment.evaluation.lineage_omitted_count > 0
    assert wide_replay.to_dict()["replay_complete"] is True


def test_overhead_measurement_is_in_process_and_bounded() -> None:
    measured = measure_causal_evaluation_overhead(samples=5)
    assert measured["samples"] == 5
    assert measured["network_requests"] == 0
    assert measured["instrument_fetches"] == 0
    assert measured["background_tasks"] == 0
    assert measured["metric_slots"] == 5
    assert measured["observation_count"] > 0
    assert measured["artifact_bytes"] < 2_000_000
    assert measured["tracemalloc_peak_bytes"] < 50_000_000
    assert set(measured["p3_evaluate_microseconds"]) == {"min", "p50", "max", "samples"}
    assert set(measured["p4_assess_microseconds"]) == {"min", "p50", "max", "samples"}


def test_shadow_on_preserves_production_decisions_for_direct_cached_and_fast_paths() -> None:
    for overrides in ({"cache_enabled": False}, {"cache_enabled": True}, {"fast_mode": True, "cache_enabled": True}):
        _off_client, off = _scan(False, **overrides)
        _on_client, on = _scan(True, **overrides)
        assert _public_run(off) == _public_run(on)
        _assert_cache_stats(off, on)
        assert off.results[0].model_dump(mode="json")["runtime_seconds"] is not None
        assert "derivatives_causal_observation" not in on.results[0].model_dump()
        assert "derivatives_source_truth_shadow" not in on.results[0].model_dump()
        assert asdict(observation_from_symbol_result(off.results[0])) == asdict(observation_from_symbol_result(on.results[0]))
        assert off.results[0].status_history == on.results[0].status_history
        assert off.results[0].lifecycle_decision_timestamp == on.results[0].lifecycle_decision_timestamp
        assert on.config.decision_timestamp == CUTOFF
        if overrides.get("fast_mode"):
            causal = on.results[0].derivatives_causal_observation
            assert causal is not None
            assert causal.scan_mode == "fast"
            assert causal.validated_confirmation is False
            assert "fast_mode_skipped" in on.results[0].derivatives_source_truth_shadow.reason_codes
        else:
            assert on.results[0].derivatives_causal_observation is not None
            assert on.results[0].derivatives_causal_observation.bundle.retain_inputs is False
            study = study_from_symbol_results(
                on.results,
                requested_venue="binance",
                scan_mode="full",
                shadow_requested=True,
            )
            assert study.to_dict()["replay_complete"] is False
            assert study.to_dict()["complete_verified_coverage"] is False


def test_partial_metadata_and_evaluator_failures_stay_outside_production_gates(monkeypatch: pytest.MonkeyPatch) -> None:
    off = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, open_interest=NA, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF),
        ).run(_config(["BTCUSDT"], decision_timestamp=CUTOFF, derivatives_source_truth_shadow=False))
    )
    on = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, open_interest=NA, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF),
        ).run(_config(["BTCUSDT"], decision_timestamp=CUTOFF, derivatives_source_truth_shadow=True))
    )
    assert _public_run(off) == _public_run(on)
    _assert_cache_stats(off, on)
    assert any(item.startswith("open_interest") for item in on.results[0].missing_data)
    assert "source_fetch_failed" in on.results[0].derivatives_source_truth_shadow.reason_codes

    class FailingInstrument(FakeExchangeClient):
        async def get_instrument_metadata(self, symbol: str, *, capture_clock=None) -> Any:
            raise RuntimeError("metadata down")

    off_meta = run(
        ScannerRunner(
            exchange_client=FailingInstrument({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF),
        ).run(_config(["BTCUSDT"], decision_timestamp=CUTOFF))
    )
    on_meta = run(
        ScannerRunner(
            exchange_client=FailingInstrument({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF),
        ).run(_config(["BTCUSDT"], decision_timestamp=CUTOFF, derivatives_source_truth_shadow=True))
    )
    assert _public_run(off_meta) == _public_run(on_meta)
    _assert_cache_stats(off_meta, on_meta)
    causal_meta = on_meta.results[0].derivatives_causal_observation
    assert causal_meta.bundle.instrument_attempt == "withheld"
    assert "instrument_lookup_withheld" in causal_meta.bundle.instrument.reason_codes
    assert causal_meta.bundle.instrument.acquisition.acquired_at is None
    assert not any("instrument" in item for item in on_meta.results[0].missing_data)

    def boom(**_kwargs: Any) -> Any:
        raise RuntimeError("research evaluator failed")

    monkeypatch.setattr("app.analytics.derivatives_causal_observation.evaluate_derivatives_source_truth", boom)
    baseline = _scan(False)[1]
    failed = _scan(True)[1]
    assert _public_run(baseline) == _public_run(failed)
    _assert_cache_stats(baseline, failed)
    causal = failed.results[0].derivatives_causal_observation
    assert causal.research_assessment.status == "unavailable"
    assert causal.strategy_assessment.evaluation.classification != "evaluation_error"
    assert causal.validated_confirmation is False


def test_slow_shadow_metadata_cannot_consume_the_symbol_deadline() -> None:
    class SlowInstrument(FakeExchangeClient):
        def __init__(self) -> None:
            super().__init__({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF)
            self.instrument_calls = 0

        async def get_instrument_metadata(self, symbol: str, *, capture_clock=None) -> Any:
            self.instrument_calls += 1
            await asyncio.sleep(30)
            raise AssertionError("shadow instrument lookup ran to completion")

    off_client = SlowInstrument()
    on_client = SlowInstrument()
    off = run(
        ScannerRunner(
            exchange_client=off_client,
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF),
        ).run(_config(["BTCUSDT"], decision_timestamp=CUTOFF, symbol_timeout_sec=2, request_timeout_sec=10))
    )
    on = run(
        ScannerRunner(
            exchange_client=on_client,
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF),
        ).run(
            _config(
                ["BTCUSDT"],
                decision_timestamp=CUTOFF,
                derivatives_source_truth_shadow=True,
                symbol_timeout_sec=2,
                request_timeout_sec=10,
            )
        )
    )
    assert off.results[0].status.value != "scan_error"
    assert on.results[0].status == off.results[0].status
    assert on.results[0].status_history == off.results[0].status_history
    assert off_client.instrument_calls == 0
    assert on_client.instrument_calls == 0
    assert off_client.requested_klines == on_client.requested_klines
    causal = on.results[0].derivatives_causal_observation
    assert causal.bundle.instrument_attempt == "withheld"
    assert "instrument_lookup_withheld" in causal.bundle.instrument.reason_codes
    assert causal.bundle.instrument.acquisition.acquired_at is None
    assert not any("instrument" in item for item in on.results[0].missing_data)
    assert _public_run(off) == _public_run(on)
    _assert_cache_stats(off, on)


def test_symbol_deadline_does_not_collect_shadow_instrument_metadata() -> None:
    class CountingInstrument(FakeExchangeClient):
        def __init__(self) -> None:
            super().__init__({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF)
            self.instrument_calls = 0

        async def get_instrument_metadata(self, symbol: str, *, capture_clock=None) -> Any:
            self.instrument_calls += 1
            return parse_binance_usds_instrument(
                binance_instrument_payload(symbol),
                requested_symbol=symbol,
                acquisition=capture_acquisition(capture_clock),
            )

    client = CountingInstrument()
    result = run(
        ScannerRunner(
            exchange_client=client,
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF + timedelta(seconds=1)),
        ).run(
            _config(
                ["BTCUSDT"],
                decision_timestamp=CUTOFF,
                derivatives_source_truth_shadow=True,
                symbol_timeout_sec=30,
            )
        )
    )
    assert client.instrument_calls == 0
    causal = result.results[0].derivatives_causal_observation
    assert causal.bundle.instrument_attempt == "withheld"
    assert causal.strategy_assessment.evaluation is not None
    assert causal.research_assessment.status == "assessed"
    assert causal.validated_confirmation is False


def test_cancellation_propagates_and_does_not_leak_the_shadow_task() -> None:
    class SlowTicker(FakeExchangeClient):
        async def get_ticker(self, symbol: str) -> dict[str, Decimal | str | int]:
            await asyncio.sleep(30)
            return await super().get_ticker(symbol)

        async def get_instrument_metadata(self, symbol: str, *, capture_clock=None) -> Any:
            await asyncio.sleep(30)
            raise AssertionError("cancelled instrument lookup resumed")

    async def scenario(shadow: bool) -> None:
        runner = ScannerRunner(exchange_client=SlowTicker({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF))
        config = _config(["BTCUSDT"], decision_timestamp=CUTOFF, derivatives_source_truth_shadow=shadow, symbol_timeout_sec=30)
        task = asyncio.create_task(
            runner._fetch_optional_market_data(
                runner.exchange_client,
                "BTCUSDT",
                config,
                iteration_index=0,
                symbol_deadline=asyncio.get_running_loop().time() + 30,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        leaked = [item for item in asyncio.all_tasks() if item.get_name() == "cci-shadow-instrument" and not item.done()]
        assert leaked == []

    run(asyncio.wait_for(scenario(False), timeout=2))
    run(asyncio.wait_for(scenario(True), timeout=2))


def test_failed_attempts_remain_in_the_study_denominator() -> None:
    candles = {"BTCUSDT": _strategy_pullback_candles(), "ETHUSDT": _strategy_pullback_candles()}
    result = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient(candles, failing_symbols={"ETHUSDT"}, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF),
        ).run(_config(["ETHUSDT", "BTCUSDT"], decision_timestamp=CUTOFF, derivatives_source_truth_shadow=True))
    )
    study = study_from_symbol_results(result.results, requested_venue="binance", scan_mode="full", shadow_requested=True)
    payload = study.to_dict()
    population = _population(payload, "binance", "full")
    assert len(study.attempts) == 2
    assert study.attempts[0].assessed is False
    assert study.attempts[0].unassessed_reason == "errored"
    assert study.attempts[1].assessed is True
    assert population["attempt_disposition_sum"] == 2
    assert population["metric_opportunities"] == 10
    assert _rate(population, "attempt_disposition_unassessed", "not_cutoff_specific")["numerator"] == 1
    off = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
        ).run(_config(["BTCUSDT"], decision_timestamp=CUTOFF))
    )
    silent = study_from_symbol_results(off.results, requested_venue="binance", scan_mode="full", shadow_requested=False)
    assert silent.attempts[0].unassessed_reason == "shadow_not_requested"
    assert silent.to_dict()["populations"][0]["metric_opportunities"] == 5


def test_transport_errors_raise_and_global_context_does_not_move_the_decision_cutoff() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "down"})

    async def failing() -> None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://fapi.binance.com")
        client = BinanceFuturesClient(http_client=http, retry_attempts=1, retry_base_delay=0, retry_max_delay=0)
        try:
            await client.get_open_interest_with_evidence("BTCUSDT", capture_clock=fixed_clock(CUTOFF))
        finally:
            await client.aclose()

    with pytest.raises(ExchangeHTTPError):
        run(failing())

    result = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF),
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF + timedelta(seconds=30)),
        ).run(
            _config(
                ["BTCUSDT"],
                decision_timestamp=CUTOFF,
                derivatives_source_truth_shadow=True,
                global_context_enabled=True,
                btc_context_enabled=True,
                btc_d_context_enabled=False,
            )
        )
    )
    causal = result.results[0].derivatives_causal_observation
    assert result.config.decision_timestamp == CUTOFF
    assert causal.strategy_decision_cutoff == CUTOFF.isoformat()
    assert causal.research_observation_cutoff != causal.strategy_decision_cutoff


def test_schema_and_scanner_do_not_gain_a_shadow_writer(tmp_path: Path) -> None:
    assert SCHEMA_VERSION == 27
    connection = connect_database(tmp_path / "p4-causal.sqlite")
    try:
        initialize_database(connection)
        assert identify_schema_version(connection) == 27
        script = "\n".join(row[0] for row in connection.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"))
    finally:
        connection.close()
    assert "derivatives_causal_observation" not in script
    assert "derivatives_source_truth" not in script
    scanner_source = Path("app/pipeline/scanner_runner.py").read_text(encoding="utf-8")
    assert "write_dev_shadow_artifact" not in scanner_source
    assert "write_dev_causal_shadow_artifact" not in scanner_source
    unavailable = unavailable_batch(metric="open_interest", endpoint="open_interest", requested_symbol="BTCUSDT", reason="adapter_has_no_source_evidence")
    record = assess_frozen_bundle(_freeze({"open_interest": unavailable}, strategy=CUTOFF, observed=CUTOFF + timedelta(seconds=1)))
    assert record.strategy_assessment.evaluation.classification != "eligible_no_conflict_detected"
    assert record.validated_confirmation is False
