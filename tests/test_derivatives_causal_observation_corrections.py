"""Regressions for the P4 Astra review. Fixtures are synthetic and offline."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest

from app.analytics.derivatives_causal_observation import (
    RESEARCH_CUTOFF_ROLE,
    STRATEGY_CUTOFF_ROLE,
    assess_frozen_bundle,
    build_verified_coverage_fixture,
    capture_observation_boundary,
    fallback_shadow_bundle,
    freeze_derivatives_bundle,
    study_from_records,
    study_from_symbol_results,
)
from app.data.derivatives_instrument import parse_binance_usds_instrument
from app.data.derivatives_source_evidence import capture_acquisition
from app.data.dtos import NA
from app.pipeline.scanner_runner import ScannerRunner
from tests.test_derivatives_causal_observation import CUTOFF, fixed_clock
from tests.test_derivatives_source_truth import binance_instrument_payload
from tests.test_scanner_runner import (
    FakeExchangeClient,
    SpyAlertAgent,
    _clean_target_intelligence,
    _config,
    _strategy_pullback_candles,
)


def _report(bundle: Any) -> tuple[Any, dict[str, Any]]:
    record = assess_frozen_bundle(bundle)
    return record, study_from_records([record], synthetic=True).to_dict()


def _rate(payload: dict[str, Any], name: str, role: str = RESEARCH_CUTOFF_ROLE) -> dict[str, Any]:
    return next(
        item
        for item in payload["populations"][0]["rates"]
        if item["name"] == name and item["cutoff_role"] == role
    )


def _conflict(bundle: Any, placement: str) -> Any:
    evidence = bundle.evidence_map()
    batch = evidence["open_interest_history"]
    original = batch.observations[-1]
    quantity = replace(original.quantities[0], raw_value="999", numeric_value=Decimal("999"))
    conflict = replace(original, quantities=(quantity, *original.quantities[1:]))
    rows = list(batch.observations)
    if placement == "append":
        rows.append(conflict)
    elif placement == "prepend":
        rows.insert(0, conflict)
    else:
        rows.insert(1, conflict)
    evidence["open_interest_history"] = replace(batch, observations=tuple(rows))
    return replace(bundle, slots=tuple(evidence.items()))


def _deliver(bundle: Any, when: Any, labels: tuple[str, ...] = ("open_interest", "open_interest_history")) -> Any:
    evidence = bundle.evidence_map()
    for label in labels:
        batch = evidence[label]
        evidence[label] = replace(batch, cache=replace(batch.cache, delivery_kind="hit", delivered_at=when))
    return replace(bundle, slots=tuple(evidence.items()))


@pytest.fixture(autouse=True)
def _isolate_target_intelligence(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "app.pipeline.scanner_runner.build_target_intelligence",
        lambda *_args, **_kwargs: _clean_target_intelligence(),
    )


def _execute(client: FakeExchangeClient, shadow: bool, **overrides: Any) -> Any:
    return asyncio.run(
        ScannerRunner(
            exchange_client=client,
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF),
        ).run(
            _config(
                ["BTCUSDT"],
                decision_timestamp=CUTOFF,
                derivatives_source_truth_shadow=shadow,
                cache_enabled=False,
                **overrides,
            )
        )
    )


def test_conflicting_oi_group_is_not_complete_verified_coverage() -> None:
    bundle = build_verified_coverage_fixture().attempts[0].record.bundle
    _clean_record, clean = _report(bundle)
    assert clean["complete_verified_coverage"] is True
    assert _rate(clean, "verified_oi_quantity_changes")["numerator"] == 1
    clean_rows = _rate(clean, "received_source_rows", "not_cutoff_specific")["numerator"]

    numerators: list[int] = []
    for placement in ("append", "prepend", "middle"):
        record, payload = _report(_conflict(bundle, placement))
        evaluation = record.research_assessment.evaluation
        assert payload["complete_verified_coverage"] is False
        assert "duplicate_conflict" in evaluation.reason_codes
        assert "duplicate_conflict" in record.strategy_assessment.evaluation.reason_codes
        assert evaluation.funding_rate != NA
        admissible = _rate(payload, "causally_admissible_observations")
        assert 0 < admissible["numerator"] < admissible["denominator"]
        assert _rate(payload, "admissible_oi_observations")["numerator"] > 0
        assert _rate(payload, "received_source_rows", "not_cutoff_specific")["numerator"] == clean_rows + 1
        rejected = admissible["denominator"] - admissible["numerator"]
        overlap = sum(
            item["count"]
            for item in payload["populations"][0]["overlapping_reason_counts"]
            if item["name"] == "duplicate_conflict" and item["cutoff_role"] == RESEARCH_CUTOFF_ROLE
        )
        assert overlap == rejected
        assert rejected > 0
        numerators.append(admissible["numerator"])
    assert numerators[0] == numerators[1] == numerators[2]


def test_research_pair_requires_delivery_of_the_selected_oi_observations() -> None:
    bundle = build_verified_coverage_fixture().attempts[0].record.bundle
    clean_record, clean = _report(bundle)
    cases = {
        "late": bundle.observation.cutoff + timedelta(seconds=1),
        "missing": None,
        "equal": bundle.observation.cutoff,
        "before": bundle.observation.cutoff - timedelta(seconds=1),
    }
    expected = {"late": 0, "missing": 0, "equal": 1, "before": 1}
    for name, when in cases.items():
        record, payload = _report(_deliver(bundle, when))
        assert _rate(payload, "verified_oi_quantity_changes")["numerator"] == expected[name]
        assert _rate(payload, "valid_oi_pairs")["numerator"] == expected[name]
        assert record.research_assessment.evaluation.oi_change_pct == clean_record.research_assessment.evaluation.oi_change_pct
        assert record.strategy_assessment.evaluation.oi_change_pct == clean_record.strategy_assessment.evaluation.oi_change_pct
        assert record.research_assessment.evaluation.oi_change_pct != NA
        assert (
            _rate(payload, "verified_oi_quantity_changes", STRATEGY_CUTOFF_ROLE)["numerator"]
            == _rate(clean, "verified_oi_quantity_changes", STRATEGY_CUTOFF_ROLE)["numerator"]
        )
    partial, partial_payload = _report(
        _deliver(
            _deliver(bundle, bundle.observation.cutoff - timedelta(seconds=1), ("open_interest",)),
            bundle.observation.cutoff + timedelta(seconds=1),
            ("open_interest_history",),
        )
    )
    assert _rate(partial_payload, "verified_oi_quantity_changes")["numerator"] == 0
    assert partial.research_assessment.evaluation.oi_change_pct == clean_record.research_assessment.evaluation.oi_change_pct
    assert _rate(clean, "verified_oi_quantity_changes")["numerator"] == 1


def test_shared_connection_cannot_change_production_status() -> None:
    class LimitedClient(FakeExchangeClient):
        def __init__(self) -> None:
            super().__init__({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF)
            self.connection = asyncio.Semaphore(1)
            self.instrument_calls = 0

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int]]:
            async with self.connection:
                return await super().get_klines(symbol, interval, limit)

        async def get_instrument_metadata(self, symbol: str, *, capture_clock: Any = None) -> Any:
            self.instrument_calls += 1
            async with self.connection:
                await asyncio.Event().wait()

        async def get_funding_rate(self, symbol: str) -> dict[str, Decimal | str | int]:
            async with self.connection:
                return await super().get_funding_rate(symbol)

    off_client = LimitedClient()
    on_client = LimitedClient()
    off = _execute(off_client, False, symbol_timeout_sec=1, request_timeout_sec=5)
    on = _execute(on_client, True, symbol_timeout_sec=1, request_timeout_sec=5)
    assert on.results[0].status == off.results[0].status
    assert on.results[0].status.value != "scan_error"
    assert on_client.instrument_calls == 0
    assert off.config.decision_timestamp == on.config.decision_timestamp == CUTOFF
    causal = on.results[0].derivatives_causal_observation
    assert causal.bundle.instrument_attempt == "withheld"
    assert causal.bundle.instrument.acquisition.acquired_at is None


def test_global_context_does_not_collect_competing_metadata() -> None:
    class LimitedClient(FakeExchangeClient):
        def __init__(self) -> None:
            super().__init__({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF)
            self.connection = asyncio.Semaphore(1)
            self.instrument_calls = 0

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int]]:
            async with self.connection:
                return await super().get_klines(symbol, interval, limit)

        async def get_instrument_metadata(self, symbol: str, *, capture_clock: Any = None) -> Any:
            self.instrument_calls += 1
            async with self.connection:
                await asyncio.Event().wait()

        async def get_funding_rate(self, symbol: str) -> dict[str, Decimal | str | int]:
            async with self.connection:
                return await super().get_funding_rate(symbol)

    async def run_once(shadow: bool, client: LimitedClient) -> Any:
        runner = ScannerRunner(
            exchange_client=client,
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF),
        )
        return await asyncio.wait_for(
            runner.run(
                _config(
                    ["BTCUSDT"],
                    decision_timestamp=CUTOFF,
                    derivatives_source_truth_shadow=shadow,
                    cache_enabled=False,
                    global_context_enabled=True,
                    btc_d_context_enabled=False,
                    symbol_timeout_sec=5,
                    request_timeout_sec=5,
                )
            ),
            timeout=8,
        )

    off_client = LimitedClient()
    on_client = LimitedClient()
    off = asyncio.run(run_once(False, off_client))
    on = asyncio.run(run_once(True, on_client))
    assert on.results[0].status == off.results[0].status
    assert on.config.decision_timestamp == CUTOFF
    assert on_client.instrument_calls == 0
    assert off_client.instrument_calls == 0


def test_malformed_instrument_and_freeze_failure_preserve_production_status(monkeypatch: pytest.MonkeyPatch) -> None:
    class MalformedClient(FakeExchangeClient):
        def __init__(self) -> None:
            super().__init__({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF)
            self.instrument_calls = 0

        async def get_instrument_metadata(self, symbol: str, *, capture_clock: Any = None) -> Any:
            self.instrument_calls += 1
            return SimpleNamespace(reason_codes=17, acquired_at=CUTOFF)

    off = _execute(MalformedClient(), False)
    on_client = MalformedClient()
    on = _execute(on_client, True)
    assert on.results[0].status == off.results[0].status
    assert on.results[0].status.value != "scan_error"
    assert on_client.instrument_calls == 0
    causal = on.results[0].derivatives_causal_observation
    assert causal.validated_confirmation is False
    assert causal.bundle.instrument.acquisition.acquired_at is None

    direct = MalformedClient()

    async def fetch_direct() -> Any:
        runner = ScannerRunner(
            exchange_client=direct,
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF),
        )
        return await runner._fetch_optional_market_data(
            direct,
            "BTCUSDT",
            _config(["BTCUSDT"], decision_timestamp=CUTOFF, derivatives_source_truth_shadow=True, cache_enabled=False),
            iteration_index=0,
        )

    optional = asyncio.run(fetch_direct())
    assert optional.ticker is not None
    assert optional.funding is not None
    assert optional.open_interest is not None
    assert direct.instrument_calls == 1
    assert optional.derivatives_instrument.acquisition.acquired_at is None
    assert "instrument_representation_invalid" in optional.derivatives_instrument.reason_codes
    assert optional.derivatives_frozen_bundle.observation.cutoff == CUTOFF

    frozen = freeze_derivatives_bundle(
        {},
        instrument=SimpleNamespace(reason_codes=17, acquired_at=CUTOFF),
        strategy_cutoff=CUTOFF,
        observation=capture_observation_boundary(lambda: CUTOFF),
        symbol="BTCUSDT",
        requested_venue="binance",
        scan_mode="full",
        iteration_index=0,
    )
    assert frozen.instrument.acquisition.acquired_at is None
    assert frozen.instrument_attempt == "unavailable"

    def _boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("freeze broke")

    monkeypatch.setattr("app.pipeline.scanner_runner.freeze_derivatives_bundle", _boom)
    monkeypatch.setattr("app.analytics.derivatives_causal_observation.freeze_derivatives_bundle", _boom)
    broken_client = MalformedClient()
    broken = _execute(broken_client, True)
    assert broken.results[0].status == off.results[0].status
    broken_causal = broken.results[0].derivatives_causal_observation
    assert broken_causal.bundle.observation.cutoff is None
    assert "shadow_bundle_construction_failed" in broken_causal.bundle.instrument.reason_codes
    assert broken_causal.research_assessment.status == "unavailable"
    assert broken_causal.strategy_assessment.evaluation.classification != "evaluation_error"
    assert broken_client.instrument_calls == 0
    assert broken.results[0].missing_data == off.results[0].missing_data


def test_direct_lookup_is_reused_and_cancellation_still_propagates() -> None:
    class CountingClient(FakeExchangeClient):
        def __init__(self) -> None:
            super().__init__({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF)
            self.instrument_calls = 0

        async def get_instrument_metadata(self, symbol: str, *, capture_clock: Any = None) -> Any:
            self.instrument_calls += 1
            return parse_binance_usds_instrument(
                binance_instrument_payload(symbol),
                requested_symbol=symbol,
                acquisition=capture_acquisition(capture_clock),
            )

    client = CountingClient()

    async def fetch_once() -> Any:
        runner = ScannerRunner(
            exchange_client=client,
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF + timedelta(seconds=1)),
        )
        return await runner._fetch_optional_market_data(
            client,
            "BTCUSDT",
            _config(["BTCUSDT"], decision_timestamp=CUTOFF, derivatives_source_truth_shadow=True, cache_enabled=False),
            iteration_index=0,
        )

    optional = asyncio.run(fetch_once())
    record = assess_frozen_bundle(optional.derivatives_frozen_bundle)
    assert client.instrument_calls == 1
    assert record.bundle.instrument_attempt == "completed"
    assert record.strategy_assessment.evaluation is not None
    assert record.research_assessment.status == "assessed"

    class SlowInstrument(FakeExchangeClient):
        async def get_instrument_metadata(self, symbol: str, *, capture_clock: Any = None) -> Any:
            await asyncio.sleep(30)
            raise AssertionError("cancelled instrument lookup resumed")

    async def cancel_direct() -> None:
        slow = SlowInstrument({"BTCUSDT": _strategy_pullback_candles()}, candle_as_of=CUTOFF)
        runner = ScannerRunner(exchange_client=slow, alert_agent=SpyAlertAgent(), clock=fixed_clock(CUTOFF))
        task = asyncio.create_task(
            runner._fetch_optional_market_data(
                slow,
                "BTCUSDT",
                _config(["BTCUSDT"], decision_timestamp=CUTOFF, derivatives_source_truth_shadow=True),
                iteration_index=0,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        current = asyncio.current_task()
        leaked = [item for item in asyncio.all_tasks() if item is not current and not item.done()]
        assert leaked == []

    asyncio.run(cancel_direct())


def test_fallback_bundle_does_not_invent_an_observation_time() -> None:
    bundle = fallback_shadow_bundle(
        {},
        symbol="BTCUSDT",
        requested_venue="binance",
        strategy_cutoff=CUTOFF,
        scan_mode="full",
        iteration_index=0,
        error_name="RuntimeError",
    )
    assert bundle.observation.cutoff is None
    assert bundle.instrument.acquisition.acquired_at is None
    assert "shadow_bundle_construction_failed" in bundle.instrument.reason_codes
    assert bundle.strategy_cutoff == CUTOFF


def test_global_timeout_study_still_counts_every_targeted_symbol() -> None:
    class SlowClient(FakeExchangeClient):
        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int]]:
            await asyncio.Event().wait()

    client = SlowClient(
        {"BTCUSDT": _strategy_pullback_candles(), "ETHUSDT": _strategy_pullback_candles()},
        candle_as_of=CUTOFF,
    )
    result = asyncio.run(
        ScannerRunner(
            exchange_client=client,
            alert_agent=SpyAlertAgent(),
            clock=fixed_clock(CUTOFF),
            capture_clock=fixed_clock(CUTOFF),
        ).run(
            _config(
                ["BTCUSDT", "ETHUSDT"],
                decision_timestamp=CUTOFF,
                derivatives_source_truth_shadow=True,
                cache_enabled=False,
                scan_timeout_sec=1,
                symbol_timeout_sec=5,
                request_timeout_sec=5,
            )
        )
    )
    study = study_from_symbol_results(
        result.results,
        requested_venue="binance",
        scan_mode="full",
        shadow_requested=True,
    )
    assert len(study.attempts) == len(result.config.symbols)
