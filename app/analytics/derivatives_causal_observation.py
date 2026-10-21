"""P4 causal shadow observation for derivatives evidence.

One frozen bundle is assessed twice by the existing pure evaluator. The
original strategy cutoff stays the strategy cutoff. The research cutoff is the
injected clock sample taken after derivative delivery and the instrument
attempt, and it is never written into a strategy-decision field. Neither
assessment can validate a confirmation or change a live decision.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import datetime, timedelta
from decimal import Decimal
import json
from pathlib import Path
import time
import tracemalloc
from typing import Any

from app.analytics.derivatives_source_truth import (
    ENFORCEMENT,
    SHADOW_FRESHNESS_POLICY,
    DerivativesSourceTruthResult,
    evaluate_derivatives_source_truth,
    evidence_visible_at_cutoff,
    observation_context_freshness,
    observation_temporal_reasons,
    series_period_allowance_ms,
    visible_selector_admission,
)
from app.data.candle_integrity import normalize_utc_timestamp
from app.data.derivatives_instrument import (
    InstrumentMetadata,
    parse_binance_usds_instrument,
    unverified_instrument,
)
from app.data.derivatives_source_evidence import (
    QUALITY_MISSING,
    QUALITY_VERIFIED,
    AcquisitionFact,
    CacheFact,
    DerivativesEvidenceBatch,
    DerivativesObservation,
    QuantityFact,
    RequestIdentity,
    TimeFact,
    capture_acquisition,
    describe_binance_account_ratio,
    describe_binance_funding_history,
    describe_binance_open_interest,
    describe_binance_open_interest_history,
    unavailable_batch,
)
from app.data.dtos import NA

POLICY_VERSION = "p4_causal_shadow_observation_v1"
STRATEGY_CUTOFF_ROLE = "original_strategy_decision_cutoff"
RESEARCH_CUTOFF_ROLE = "research_observation_cutoff"
OBSERVATION_ROLE = "later_research_observation"
BOUNDARY_DESCRIPTION = "completed_per_symbol_derivatives_bundle_after_delivery_and_instrument_attempt"
OBSERVATION_MODE = "injected_capture_clock_at_bundle_boundary"
EVALUATOR_PARAMETER = "decision_cutoff"

METRIC_SLOTS = (
    "open_interest",
    "open_interest_history",
    "funding_rate",
    "funding_history",
    "long_short_ratio",
)
_SLOT_ARGUMENT = {
    "open_interest": "open_interest",
    "open_interest_history": "open_interest_history",
    "funding_rate": "funding",
    "funding_history": "funding_history",
    "long_short_ratio": "account_ratio",
}
_BLOCKING_TEMPORAL = frozenset(
    {
        "missing_metric_timestamp",
        "malformed_metric_timestamp",
        "ambiguous_metric_timestamp",
        "missing_acquisition_timestamp",
        "description_without_acquisition_fact",
        "acquisition_capture_failed",
        "metric_after_cutoff",
        "acquisition_after_cutoff",
        "metric_after_acquisition",
        "metric_after_response",
        "response_after_acquisition",
        "delivery_before_acquisition",
        "malformed_exchange_response_time",
        "missing_decision_cutoff",
    }
)
_IDENTITY_CODES = frozenset(
    {
        "symbol_conflict",
        "venue_mismatch",
        "category_mismatch",
        "identity_conflict",
        "request_context_identity_conflict",
        "settlement_mismatch",
        "contract_mismatch",
        "unsupported_inverse",
    }
)
_CACHE_PROVENANCE_GAPS = frozenset(
    {
        "legacy_cache_entry_lacks_acquisition_observation",
        "uninstrumented_cache_entry_lacks_acquisition_observation",
    }
)
_OUTPUT_QUANT = Decimal("0.00000001")
_DOES_NOT_ESTABLISH = (
    "production_strategy_correction",
    "cache_insertion_before_cutoff",
    "consumer_visibility_from_upstream_acquisition",
    "ttl_is_source_freshness",
)
_RESEARCH_DOES_NOT = (
    "original_strategy_decision",
    "production_strategy_correction",
    "retroactive_strategy_availability",
)
_TYPES: dict[str, type[Any]] = {}


def _register(cls: type[Any]) -> type[Any]:
    _TYPES[cls.__name__] = cls
    return cls


@_register
@dataclass(frozen=True)
class ObservationBoundary:
    cutoff: datetime | None
    quality: str
    reason_codes: tuple[str, ...]
    meaning: str
    description: str


@_register
@dataclass(frozen=True)
class AcquisitionSpan:
    earliest_metric_at: str | None
    latest_metric_at: str | None
    earliest_response_at: str | None
    latest_response_at: str | None
    earliest_acquired_at: str | None
    latest_acquired_at: str | None
    earliest_cache_delivered_at: str | None
    latest_cache_delivered_at: str | None
    acquisition_span_ms: int | None
    simultaneous_exchange_snapshot: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "simultaneous_exchange_snapshot", False)


@dataclass(frozen=True)
class FrozenDerivativesBundle:
    """Immutable slot snapshot. Later writes to the source mapping are ignored."""

    slots: tuple[tuple[str, Any], ...]
    instrument: Any
    strategy_cutoff: datetime | None
    observation: ObservationBoundary
    symbol: str
    requested_venue: str
    evidence_exchange: str
    scan_mode: str
    iteration_index: int | None
    attempt_id: str
    instrument_attempt: str
    synthetic: bool = False
    assumptions: tuple[str, ...] = ()
    retain_inputs: bool = False
    code_revision: str = NA

    def evidence_map(self) -> dict[str, Any]:
        return dict(self.slots)


@_register
@dataclass(frozen=True)
class AvailabilityClaim:
    metric: str
    cutoff_role: str
    collector_acquired_before_cutoff: str
    cache_retained_before_cutoff: str
    consumer_delivered_before_cutoff: str
    later_cache_retrieval_of_earlier_acquisition: str
    delivery_kind: str
    endpoint: str
    request_params: tuple[tuple[str, str], ...]
    metric_times: tuple[str | None, ...]
    response_at: str | None
    acquired_at: str | None
    cache_delivered_at: str | None
    original_acquired_at: str | None
    file_rehydrated: bool
    reason_codes: tuple[str, ...]
    establishes: tuple[str, ...]
    does_not_establish: tuple[str, ...]


@dataclass(frozen=True)
class CutoffAssessment:
    cutoff_role: str
    cutoff: str
    status: str
    evaluator_parameter: str
    evaluation: DerivativesSourceTruthResult | None
    availability: tuple[AvailabilityClaim, ...]
    establishes: tuple[str, ...]
    does_not_establish: tuple[str, ...]
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CausalObservationRecord:
    attempt_id: str
    symbol: str
    requested_venue: str
    evidence_exchange: str
    scan_mode: str
    iteration_index: int | None
    observation_role: str
    strategy_decision_cutoff: str
    research_observation_cutoff: str
    strategy_evaluation_cutoff_matches: bool
    observation_boundary: ObservationBoundary
    strategy_assessment: CutoffAssessment
    research_assessment: CutoffAssessment
    acquisition_span: AcquisitionSpan
    assumptions: tuple[str, ...] = ()
    synthetic: bool = False
    policy_version: str = POLICY_VERSION
    evaluator_freshness_policy: str = SHADOW_FRESHNESS_POLICY
    code_revision: str = NA
    mode: str = "shadow"
    enforcement: str = ENFORCEMENT
    validated_confirmation: bool = False
    trading_performance_claim: bool = False
    bundle: FrozenDerivativesBundle | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", "shadow")
        object.__setattr__(self, "enforcement", ENFORCEMENT)
        object.__setattr__(self, "validated_confirmation", False)
        object.__setattr__(self, "trading_performance_claim", False)
        object.__setattr__(self, "policy_version", POLICY_VERSION)
        object.__setattr__(self, "evaluator_freshness_policy", SHADOW_FRESHNESS_POLICY)
        object.__setattr__(self, "observation_role", OBSERVATION_ROLE)


@dataclass(frozen=True)
class StudyAttempt:
    attempt_id: str
    symbol: str
    requested_venue: str
    scan_mode: str
    assessed: bool
    unassessed_reason: str | None
    record: CausalObservationRecord | None


@dataclass(frozen=True)
class CausalShadowStudy:
    attempts: tuple[StudyAttempt, ...]
    requested_venue: str
    synthetic: bool = False
    code_revision: str = NA
    shadow_requested: bool = True
    mode: str = "shadow"
    enforcement: str = ENFORCEMENT
    validated_confirmation: bool = False
    trading_performance_claim: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", "shadow")
        object.__setattr__(self, "enforcement", ENFORCEMENT)
        object.__setattr__(self, "validated_confirmation", False)
        object.__setattr__(self, "trading_performance_claim", False)


def capture_observation_boundary(clock: Callable[[], datetime] | None) -> ObservationBoundary:
    """Sample the injected capture clock once. A failure does not select another clock."""

    if clock is None:
        return _failed_boundary(("observation_clock_failed",))
    try:
        moment = normalize_utc_timestamp(clock(), field_name="research_observation_cutoff")
    except Exception:
        return _failed_boundary(("observation_clock_failed",))
    return ObservationBoundary(
        cutoff=moment,
        quality=QUALITY_VERIFIED,
        reason_codes=(),
        meaning=OBSERVATION_MODE,
        description=BOUNDARY_DESCRIPTION,
    )


def freeze_derivatives_bundle(
    evidence: Mapping[str, Any] | None,
    *,
    instrument: Any,
    strategy_cutoff: datetime | None,
    observation: ObservationBoundary,
    symbol: str,
    requested_venue: str,
    scan_mode: str,
    iteration_index: int | None,
    synthetic: bool = False,
    assumptions: tuple[str, ...] = (),
    retain_inputs: bool = False,
    code_revision: str = NA,
    attempt_id: str | None = None,
) -> FrozenDerivativesBundle:
    instrument = represented_instrument(instrument, exchange=requested_venue, symbol=symbol)
    slots = tuple((str(key), value) for key, value in (evidence or {}).items())
    exchange = _evidence_exchange(slots, instrument)
    strategy_text = _iso(strategy_cutoff) or NA
    identifier = attempt_id or (
        f"{requested_venue}:{symbol}:{_index_text(iteration_index)}:{scan_mode}:{strategy_text}"
    )
    return FrozenDerivativesBundle(
        slots=slots,
        instrument=instrument,
        strategy_cutoff=strategy_cutoff,
        observation=observation,
        symbol=symbol,
        requested_venue=requested_venue,
        evidence_exchange=exchange,
        scan_mode=scan_mode,
        iteration_index=iteration_index,
        attempt_id=identifier,
        instrument_attempt=_instrument_attempt(instrument),
        synthetic=synthetic,
        assumptions=tuple(assumptions),
        retain_inputs=retain_inputs,
        code_revision=code_revision,
    )


def assess_frozen_bundle(
    bundle: FrozenDerivativesBundle,
    *,
    strategy_evaluation: DerivativesSourceTruthResult | None = None,
    liquidation_snapshot: Any = None,
) -> CausalObservationRecord:
    """Evaluate the frozen bundle at both cutoffs. This function does not sample a clock."""

    if strategy_evaluation is None:
        strategy_evaluation = _evaluate_bundle(bundle, bundle.strategy_cutoff, liquidation_snapshot)
    strategy_dt, strategy_norm_reasons = _try_normalize(bundle.strategy_cutoff, "strategy_decision_cutoff")
    research_status, research_reasons, research_dt = _research_gate(bundle, strategy_dt, strategy_norm_reasons)
    research_evaluation = None
    if research_status == "assessed":
        research_evaluation = _evaluate_bundle(bundle, research_dt, liquidation_snapshot)
    return _assemble_record(
        bundle,
        strategy_evaluation=strategy_evaluation,
        strategy_dt=strategy_dt,
        research_status=research_status,
        research_reasons=research_reasons,
        research_dt=research_dt,
        research_evaluation=research_evaluation,
    )


def represented_instrument(instrument: Any, *, exchange: str, symbol: str) -> Any:
    """Keep a real instrument record. Replace a malformed one without copying its clock."""

    if instrument is None:
        return None
    if (
        isinstance(instrument, InstrumentMetadata)
        and _reason_codes_are_text(getattr(instrument, "reason_codes", ()))
        and isinstance(getattr(instrument, "acquisition", None), AcquisitionFact)
    ):
        return instrument
    reason = "instrument_representation_invalid"
    return unverified_instrument(
        exchange=exchange or NA,
        requested_symbol=symbol or NA,
        source_endpoint="get_instrument_metadata",
        reason=reason,
    )


def fallback_shadow_bundle(
    evidence: Mapping[str, Any] | None,
    *,
    symbol: str,
    requested_venue: str,
    strategy_cutoff: datetime | None,
    scan_mode: str,
    iteration_index: int | None,
    error_name: str,
) -> FrozenDerivativesBundle:
    """Build an explicit unavailable bundle after construction failed.

    Successfully fetched evidence batches are kept. The observation cutoff is
    missing rather than replaced with a new clock sample, and metadata is not
    fetched again.
    """

    safe = {
        str(key): value
        for key, value in (evidence or {}).items()
        if isinstance(value, DerivativesEvidenceBatch)
    }
    instrument = unverified_instrument(
        exchange=requested_venue or NA,
        requested_symbol=symbol or NA,
        source_endpoint="get_instrument_metadata",
        reason="shadow_bundle_construction_failed",
    )
    boundary = _failed_boundary(("shadow_bundle_construction_failed", error_name))
    cutoff = strategy_cutoff if isinstance(strategy_cutoff, datetime) else None
    try:
        return freeze_derivatives_bundle(
            safe,
            instrument=instrument,
            strategy_cutoff=cutoff,
            observation=boundary,
            symbol=symbol,
            requested_venue=requested_venue or NA,
            scan_mode=scan_mode,
            iteration_index=iteration_index,
        )
    except Exception:
        return FrozenDerivativesBundle(
            slots=tuple(safe.items()),
            instrument=instrument,
            strategy_cutoff=cutoff,
            observation=boundary,
            symbol=symbol,
            requested_venue=requested_venue or NA,
            evidence_exchange=requested_venue or NA,
            scan_mode=scan_mode,
            iteration_index=iteration_index,
            attempt_id=f"{requested_venue}:{symbol}:{_index_text(iteration_index)}:{scan_mode}:construction-failed",
            instrument_attempt="unavailable",
        )


def unavailable_causal_record(
    bundle: FrozenDerivativesBundle,
    *,
    strategy_evaluation: DerivativesSourceTruthResult,
    reason: str,
    error_name: str | None = None,
) -> CausalObservationRecord:
    reasons = tuple(dict.fromkeys(code for code in (reason, error_name) if code))
    return _assemble_record(
        bundle,
        strategy_evaluation=strategy_evaluation,
        strategy_dt=_try_normalize(bundle.strategy_cutoff, "strategy_decision_cutoff")[0],
        research_status="unavailable",
        research_reasons=reasons or ("evaluation_error",),
        research_dt=None,
        research_evaluation=None,
    )


def study_from_records(
    records: Sequence[CausalObservationRecord],
    *,
    synthetic: bool = False,
    code_revision: str = NA,
    shadow_requested: bool = True,
) -> CausalShadowStudy:
    attempts = tuple(
        StudyAttempt(
            attempt_id=record.attempt_id,
            symbol=record.symbol,
            requested_venue=record.requested_venue,
            scan_mode=record.scan_mode,
            assessed=True,
            unassessed_reason=None,
            record=record,
        )
        for record in records
    )
    venue = records[0].requested_venue if records else NA
    return CausalShadowStudy(
        attempts=attempts,
        requested_venue=venue,
        synthetic=synthetic or any(record.synthetic for record in records),
        code_revision=code_revision,
        shadow_requested=shadow_requested,
    )


def study_from_symbol_results(
    results: Sequence[Any],
    *,
    requested_venue: str,
    scan_mode: str,
    shadow_requested: bool,
    code_revision: str = NA,
    synthetic: bool = False,
) -> CausalShadowStudy:
    """Count every symbol result. Missing causal records stay unassessed."""

    attempts: list[StudyAttempt] = []
    for index, result in enumerate(results):
        symbol = str(getattr(result, "symbol", NA) or NA)
        record = getattr(result, "derivatives_causal_observation", None)
        if isinstance(record, CausalObservationRecord):
            attempts.append(
                StudyAttempt(
                    attempt_id=record.attempt_id,
                    symbol=record.symbol,
                    requested_venue=record.requested_venue,
                    scan_mode=record.scan_mode,
                    assessed=True,
                    unassessed_reason=None,
                    record=record,
                )
            )
            continue
        reason = _unassessed_reason(result, shadow_requested=shadow_requested)
        attempts.append(
            StudyAttempt(
                attempt_id=f"unassessed:{requested_venue}:{symbol}:{index}:{scan_mode}",
                symbol=symbol,
                requested_venue=requested_venue,
                scan_mode=scan_mode,
                assessed=False,
                unassessed_reason=reason,
                record=None,
            )
        )
    return CausalShadowStudy(
        attempts=tuple(attempts),
        requested_venue=requested_venue,
        synthetic=synthetic,
        code_revision=code_revision,
        shadow_requested=shadow_requested,
    )


def write_dev_causal_shadow_artifact(path: Path | str, study: CausalShadowStudy) -> None:
    """Explicit DEV JSON writer. The scanner and Runtime must not call this."""

    target = Path(path)
    if target.suffix.lower() != ".json":
        raise ValueError("DEV causal shadow artifacts must be explicit JSON files")
    target.write_text(json.dumps(study.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def replay_dev_causal_shadow_artifact(path: Path | str) -> CausalShadowStudy:
    """Recompute assessments from retained inputs and recorded cutoffs. Does not sample a clock."""

    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, Mapping) or not payload.get("replay_complete"):
        raise ValueError("artifact_incomplete")
    return _replay_payload(payload)


def measure_causal_evaluation_overhead(samples: int = 30) -> dict[str, Any]:
    """Time one fixed in-memory bundle. No network and no wall-clock pass/fail threshold."""

    if samples < 1:
        raise ValueError("samples must be positive")
    bundle = _hero_bundle()
    evidence = bundle.evidence_map()
    observations = sum(len(getattr(batch, "observations", ())) for batch in evidence.values())
    p3_times: list[int] = []
    p4_times: list[int] = []
    for _ in range(samples):
        started = time.perf_counter_ns()
        evaluate_derivatives_source_truth(
            decision_cutoff=bundle.strategy_cutoff,
            instrument=bundle.instrument,
            open_interest=evidence.get("open_interest"),
            open_interest_history=evidence.get("open_interest_history"),
            funding=evidence.get("funding_rate"),
            funding_history=evidence.get("funding_history"),
            account_ratio=evidence.get("long_short_ratio"),
        )
        p3_times.append((time.perf_counter_ns() - started) // 1000)
        started = time.perf_counter_ns()
        assess_frozen_bundle(bundle)
        p4_times.append((time.perf_counter_ns() - started) // 1000)
    tracemalloc.start()
    study = study_from_records((assess_frozen_bundle(bundle),), synthetic=True, code_revision="overhead-fixture")
    encoded = json.dumps(study.to_dict(), sort_keys=True).encode("utf-8")
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return {
        "workload": "synthetic_hero_bundle",
        "samples": samples,
        "observation_count": observations,
        "metric_slots": len(METRIC_SLOTS),
        "network_requests": 0,
        "instrument_fetches": 0,
        "background_tasks": 0,
        "p3_evaluate_microseconds": _time_summary(p3_times),
        "p4_assess_microseconds": _time_summary(p4_times),
        "artifact_bytes": len(encoded),
        "tracemalloc_peak_bytes": peak,
    }


def build_synthetic_causal_fixture() -> CausalShadowStudy:
    """Deterministic synthetic study: later observation, fast skip, and an unassessed attempt."""

    hero = assess_frozen_bundle(_hero_bundle())
    fast = assess_frozen_bundle(_fast_bundle())
    study = study_from_records((hero, fast), synthetic=True, code_revision="synthetic-fixture-not-a-release")
    unassessed = StudyAttempt(
        attempt_id="unassessed:binance:ETHUSDT:1:full",
        symbol="ETHUSDT",
        requested_venue="binance",
        scan_mode="full",
        assessed=False,
        unassessed_reason="scan_error",
        record=None,
    )
    return replace(study, attempts=(study.attempts[0], unassessed, study.attempts[1]))


def build_verified_coverage_fixture() -> CausalShadowStudy:
    """One synthetic attempt whose supplied inputs are admissible at both cutoffs."""

    record = assess_frozen_bundle(_verified_bundle())
    return study_from_records((record,), synthetic=True, code_revision="synthetic-fixture-not-a-release")


def _hero_bundle() -> FrozenDerivativesBundle:
    strategy = datetime(2026, 1, 15, 12, 0, tzinfo=_utc())
    current = strategy - timedelta(minutes=1)
    previous = current - timedelta(minutes=5)
    funding_at = strategy - timedelta(hours=1)
    acquired = strategy + timedelta(seconds=5)
    observed = strategy + timedelta(seconds=6)
    return _binance_bundle(
        strategy=strategy,
        current=current,
        previous=previous,
        funding_at=funding_at,
        ratio_at=current,
        acquired=acquired,
        observed=observed,
        scan_mode="full",
        iteration_index=0,
        symbol="BTCUSDT",
        fast_skips=(),
        assumptions=("synthetic_binance_funding_interval_ms",),
    )


def _fast_bundle() -> FrozenDerivativesBundle:
    strategy = datetime(2026, 1, 15, 12, 0, tzinfo=_utc())
    current = strategy - timedelta(minutes=1)
    acquired = strategy + timedelta(seconds=5)
    observed = strategy + timedelta(seconds=6)
    return _binance_bundle(
        strategy=strategy,
        current=current,
        previous=current - timedelta(minutes=5),
        funding_at=strategy - timedelta(hours=1),
        ratio_at=current,
        acquired=acquired,
        observed=observed,
        scan_mode="fast",
        iteration_index=2,
        symbol="BTCUSDT",
        fast_skips=("funding_history", "open_interest_history", "long_short_ratio"),
        assumptions=(),
        synthetic_cadence=False,
    )


def _verified_bundle() -> FrozenDerivativesBundle:
    strategy = datetime(2026, 1, 15, 12, 0, tzinfo=_utc())
    current = strategy - timedelta(minutes=1)
    # Acquisition follows the source measurement and still precedes the strategy cutoff.
    acquired = strategy - timedelta(seconds=30)
    observed = strategy + timedelta(seconds=1)
    return _binance_bundle(
        strategy=strategy,
        current=current,
        previous=current - timedelta(minutes=5),
        funding_at=strategy - timedelta(hours=1),
        ratio_at=current,
        acquired=acquired,
        observed=observed,
        scan_mode="full",
        iteration_index=0,
        symbol="BTCUSDT",
        fast_skips=(),
        assumptions=("synthetic_binance_funding_interval_ms",),
    )


def _binance_bundle(
    *,
    strategy: datetime,
    current: datetime,
    previous: datetime,
    funding_at: datetime,
    ratio_at: datetime,
    acquired: datetime,
    observed: datetime,
    scan_mode: str,
    iteration_index: int,
    symbol: str,
    fast_skips: tuple[str, ...],
    assumptions: tuple[str, ...],
    synthetic_cadence: bool = True,
) -> FrozenDerivativesBundle:
    acquisition = capture_acquisition(lambda: acquired)
    current_batch = describe_binance_open_interest(
        requested_symbol=symbol,
        payload={"symbol": symbol, "openInterest": "110", "time": _millis(current)},
        request_params={"symbol": symbol},
        capture_clock=None,
        acquisition=acquisition,
    )
    history_batch = describe_binance_open_interest_history(
        requested_symbol=symbol,
        payload=[
            {"symbol": symbol, "sumOpenInterest": "100", "sumOpenInterestValue": "10000000", "timestamp": _millis(previous)},
            {"symbol": symbol, "sumOpenInterest": "110", "sumOpenInterestValue": "11000000", "timestamp": _millis(current)},
        ],
        request_params={"symbol": symbol, "period": "5m", "limit": "2"},
        capture_clock=None,
        acquisition=acquisition,
    )
    funding_batch = describe_binance_funding_history(
        requested_symbol=symbol,
        payload=[{"symbol": symbol, "fundingRate": "0.0001", "fundingTime": _millis(funding_at)}],
        request_params={"symbol": symbol, "limit": "1"},
        capture_clock=None,
        acquisition=acquisition,
    )
    ratio_batch = describe_binance_account_ratio(
        requested_symbol=symbol,
        payload=[
            {
                "symbol": symbol,
                "longShortRatio": "1.22222222",
                "longAccount": "0.55",
                "shortAccount": "0.45",
                "timestamp": _millis(ratio_at),
            }
        ],
        request_params={"symbol": symbol, "period": "5m", "limit": "1"},
        capture_clock=None,
        acquisition=acquisition,
    )
    instrument = parse_binance_usds_instrument(
        {
            "symbols": [
                {
                    "symbol": symbol,
                    "baseAsset": "BTC",
                    "quoteAsset": "USDT",
                    "marginAsset": "USDT",
                    "contractType": "PERPETUAL",
                    "fundingIntervalHours": 8,
                }
            ]
        },
        requested_symbol=symbol,
        acquisition=acquisition,
    )
    if synthetic_cadence:
        instrument = replace(instrument, funding_interval_ms=8 * 60 * 60 * 1000)
    evidence: dict[str, Any] = {
        "open_interest": current_batch,
        "open_interest_history": history_batch,
        "funding_rate": funding_batch,
        "funding_history": describe_binance_funding_history(
            requested_symbol=symbol,
            payload=[{"symbol": symbol, "fundingRate": "0.0001", "fundingTime": _millis(funding_at)}],
            request_params={"symbol": symbol, "limit": "1"},
            capture_clock=None,
            acquisition=acquisition,
        ),
        "long_short_ratio": ratio_batch,
    }
    for label in fast_skips:
        evidence[label] = unavailable_batch(
            metric=label,
            endpoint=label,
            requested_symbol=symbol,
            reason="fast_mode_skipped",
        )
    boundary = capture_observation_boundary(lambda: observed)
    return freeze_derivatives_bundle(
        evidence,
        instrument=instrument,
        strategy_cutoff=strategy,
        observation=boundary,
        symbol=symbol,
        requested_venue="binance",
        scan_mode=scan_mode,
        iteration_index=iteration_index,
        synthetic=True,
        assumptions=assumptions,
        retain_inputs=True,
        code_revision="synthetic-fixture-not-a-release",
    )


def _failed_boundary(reasons: tuple[str, ...]) -> ObservationBoundary:
    return ObservationBoundary(
        cutoff=None,
        quality=QUALITY_MISSING,
        reason_codes=reasons,
        meaning=OBSERVATION_MODE,
        description=BOUNDARY_DESCRIPTION,
    )


def _evaluate_bundle(
    bundle: FrozenDerivativesBundle,
    cutoff: datetime | None,
    liquidation_snapshot: Any,
) -> DerivativesSourceTruthResult:
    evidence = bundle.evidence_map()
    return evaluate_derivatives_source_truth(
        decision_cutoff=cutoff,
        instrument=bundle.instrument,
        open_interest=_batch(evidence.get("open_interest")),
        open_interest_history=_batch(evidence.get("open_interest_history")),
        funding=_batch(evidence.get("funding_rate")),
        funding_history=_batch(evidence.get("funding_history")),
        account_ratio=_batch(evidence.get("long_short_ratio")),
        liquidation_snapshot=liquidation_snapshot,
    )


def _research_gate(
    bundle: FrozenDerivativesBundle,
    strategy_dt: datetime | None,
    strategy_norm_reasons: tuple[str, ...],
) -> tuple[str, tuple[str, ...], datetime | None]:
    if strategy_norm_reasons:
        return "unavailable", strategy_norm_reasons, None
    observation = bundle.observation
    if observation.quality != QUALITY_VERIFIED or observation.cutoff is None:
        reasons = observation.reason_codes or ("observation_clock_failed",)
        return "unavailable", tuple(reasons), None
    observed, observed_reasons = _try_normalize(observation.cutoff, "research_observation_cutoff")
    if observed_reasons or observed is None:
        return "unavailable", observed_reasons or ("observation_clock_failed",), None
    if strategy_dt is not None and observed < strategy_dt:
        return "unavailable", ("observation_clock_regression",), None
    return "assessed", (), observed


def _assemble_record(
    bundle: FrozenDerivativesBundle,
    *,
    strategy_evaluation: DerivativesSourceTruthResult,
    strategy_dt: datetime | None,
    research_status: str,
    research_reasons: tuple[str, ...],
    research_dt: datetime | None,
    research_evaluation: DerivativesSourceTruthResult | None,
) -> CausalObservationRecord:
    strategy_text = _iso(strategy_dt) or NA
    research_text = _iso(research_dt) if research_status == "assessed" else NA
    strategy_claims = _claims_for_cutoff(bundle, STRATEGY_CUTOFF_ROLE, strategy_dt)
    research_claims = (
        _claims_for_cutoff(bundle, RESEARCH_CUTOFF_ROLE, research_dt) if research_status == "assessed" else ()
    )
    strategy_assessment = CutoffAssessment(
        cutoff_role=STRATEGY_CUTOFF_ROLE,
        cutoff=strategy_text,
        status="assessed",
        evaluator_parameter=EVALUATOR_PARAMETER,
        evaluation=strategy_evaluation,
        availability=strategy_claims,
        establishes=("p3_admissibility_at_original_strategy_cutoff",),
        does_not_establish=_DOES_NOT_ESTABLISH,
        reason_codes=strategy_evaluation.reason_codes,
    )
    research_assessment = CutoffAssessment(
        cutoff_role=RESEARCH_CUTOFF_ROLE,
        cutoff=research_text,
        status=research_status,
        evaluator_parameter=EVALUATOR_PARAMETER,
        evaluation=research_evaluation,
        availability=research_claims,
        establishes=("later_research_observation",) if research_status == "assessed" else (),
        does_not_establish=_RESEARCH_DOES_NOT,
        reason_codes=research_evaluation.reason_codes if research_evaluation is not None else research_reasons,
    )
    return CausalObservationRecord(
        attempt_id=bundle.attempt_id,
        symbol=bundle.symbol,
        requested_venue=bundle.requested_venue,
        evidence_exchange=bundle.evidence_exchange,
        scan_mode=bundle.scan_mode,
        iteration_index=bundle.iteration_index,
        observation_role=OBSERVATION_ROLE,
        strategy_decision_cutoff=strategy_text,
        research_observation_cutoff=research_text,
        strategy_evaluation_cutoff_matches=strategy_evaluation.decision_cutoff == strategy_text,
        observation_boundary=bundle.observation,
        strategy_assessment=strategy_assessment,
        research_assessment=research_assessment,
        acquisition_span=_span(bundle),
        assumptions=bundle.assumptions,
        synthetic=bundle.synthetic,
        code_revision=bundle.code_revision,
        bundle=bundle,
    )


def _claims_for_cutoff(
    bundle: FrozenDerivativesBundle,
    cutoff_role: str,
    cutoff: datetime | None,
) -> tuple[AvailabilityClaim, ...]:
    claims = [
        _batch_claim(label, batch, cutoff_role, cutoff)
        for label, batch in bundle.slots
        if label in METRIC_SLOTS
    ]
    claims.append(_instrument_claim(bundle, cutoff_role, cutoff))
    return tuple(claims)


def _batch_claim(label: str, batch: Any, cutoff_role: str, cutoff: datetime | None) -> AvailabilityClaim:
    if not isinstance(batch, DerivativesEvidenceBatch):
        return _blank_claim(label, cutoff_role, ("evidence_not_a_derivatives_batch",))
    reasons: list[str] = list(batch.reason_codes)
    acquired = batch.acquisition.acquired_at
    acquisition_known = batch.acquisition.quality == QUALITY_VERIFIED and acquired is not None
    if batch.cache.file_rehydrated or any(code in _CACHE_PROVENANCE_GAPS for code in batch.reason_codes):
        collector = "unverified"
        reasons.append("cache_value_does_not_restore_acquisition")
    else:
        collector = _flag(acquisition_known, acquired, cutoff)
        if not acquisition_known:
            reasons.append("missing_acquisition_timestamp")
    retained = "not_recorded"
    reasons.append("cache_retention_instant_not_on_evidence_clock")
    consumer, consumer_reasons = _consumer_delivery(batch, cutoff, cutoff_role, acquisition_known, acquired)
    reasons.extend(consumer_reasons)
    later = _later_retrieval(batch)
    if later == "true":
        reasons.append("later_cache_retrieval_of_earlier_acquisition")
    establishes: list[str] = []
    does_not = list(_DOES_NOT_ESTABLISH)
    if collector == "true":
        establishes.append("collector_acquired_before_cutoff")
    if consumer == "true" and batch.cache.delivery_kind == "not_applicable":
        establishes.append("direct_adapter_response_at_or_before_cutoff")
    elif consumer == "true":
        establishes.append("cache_delivered_to_this_consumer_before_cutoff")
    if later == "true":
        establishes.append("later_cache_retrieval_of_earlier_acquisition")
        does_not.append("strategy_consumed_before_cutoff")
    if batch.cache.delivery_kind == "not_applicable":
        does_not.append("cache_delivery_occurred")
    response = batch.response_time.at
    return AvailabilityClaim(
        metric=label,
        cutoff_role=cutoff_role,
        collector_acquired_before_cutoff=collector,
        cache_retained_before_cutoff=retained,
        consumer_delivered_before_cutoff=consumer,
        later_cache_retrieval_of_earlier_acquisition=later,
        delivery_kind=batch.cache.delivery_kind,
        endpoint=batch.endpoint,
        request_params=tuple(batch.request_params),
        metric_times=tuple(_iso(item.metric_time.at) for item in batch.observations),
        response_at=_iso(response),
        acquired_at=_iso(acquired) if acquisition_known else None,
        cache_delivered_at=_iso(batch.cache.delivered_at),
        original_acquired_at=_iso(batch.cache.original_acquired_at),
        file_rehydrated=batch.cache.file_rehydrated,
        reason_codes=tuple(dict.fromkeys(reasons)),
        establishes=tuple(dict.fromkeys(establishes)),
        does_not_establish=tuple(dict.fromkeys(does_not)),
    )


def _instrument_claim(bundle: FrozenDerivativesBundle, cutoff_role: str, cutoff: datetime | None) -> AvailabilityClaim:
    instrument = bundle.instrument
    reasons = list(getattr(instrument, "reason_codes", ()) or ())
    reasons.append("instrument_consumer_delivery_not_recorded")
    acquired = getattr(getattr(instrument, "acquisition", None), "acquired_at", None)
    quality = getattr(getattr(instrument, "acquisition", None), "quality", QUALITY_MISSING)
    known = quality == QUALITY_VERIFIED and isinstance(acquired, datetime)
    if bundle.instrument_attempt in {"not_waited", "failed", "unavailable"} or not known:
        collector = "unverified"
    else:
        collector = _flag(True, acquired, cutoff)
    establishes = ["present_in_frozen_bundle"] if instrument is not None else []
    if collector == "true":
        establishes.append("collector_acquired_before_cutoff")
    return AvailabilityClaim(
        metric="instrument_metadata",
        cutoff_role=cutoff_role,
        collector_acquired_before_cutoff=collector,
        cache_retained_before_cutoff="not_recorded",
        consumer_delivered_before_cutoff="not_recorded",
        later_cache_retrieval_of_earlier_acquisition="not_applicable",
        delivery_kind="not_recorded",
        endpoint=str(getattr(instrument, "source_endpoint", NA) or NA),
        request_params=(),
        metric_times=(),
        response_at=None,
        acquired_at=_iso(acquired) if known else None,
        cache_delivered_at=None,
        original_acquired_at=None,
        file_rehydrated=False,
        reason_codes=tuple(dict.fromkeys(reasons)),
        establishes=tuple(establishes),
        does_not_establish=(
            "instrument_consumer_delivery_time",
            "cache_insertion_before_cutoff",
            "this_symbol_consumer_read_before_strategy_cutoff",
            *_DOES_NOT_ESTABLISH,
        ),
    )


def _blank_claim(label: str, cutoff_role: str, reasons: tuple[str, ...]) -> AvailabilityClaim:
    return AvailabilityClaim(
        metric=label,
        cutoff_role=cutoff_role,
        collector_acquired_before_cutoff="unverified",
        cache_retained_before_cutoff="not_recorded",
        consumer_delivered_before_cutoff="unverified",
        later_cache_retrieval_of_earlier_acquisition="unverified",
        delivery_kind=NA,
        endpoint=NA,
        request_params=(),
        metric_times=(),
        response_at=None,
        acquired_at=None,
        cache_delivered_at=None,
        original_acquired_at=None,
        file_rehydrated=False,
        reason_codes=reasons,
        establishes=(),
        does_not_establish=_DOES_NOT_ESTABLISH,
    )


def _consumer_delivery(
    batch: DerivativesEvidenceBatch,
    cutoff: datetime | None,
    cutoff_role: str,
    acquisition_known: bool,
    acquired: datetime | None,
) -> tuple[str, tuple[str, ...]]:
    kind = batch.cache.delivery_kind
    if kind == "not_applicable":
        flag = _flag(acquisition_known, acquired, cutoff)
        return flag, ("direct_adapter_response_is_not_cache_delivery",)
    delivered = batch.cache.delivered_at
    if delivered is None:
        return "unverified", ("missing_cache_delivery_capture",)
    if cutoff is None:
        return "unverified", ("missing_decision_cutoff",)
    if delivered <= cutoff:
        return "true", ("cache_delivery_is_not_acquisition",)
    if cutoff_role == RESEARCH_CUTOFF_ROLE:
        return "false", ("delivery_after_observation",)
    return "false", ("consumer_delivery_after_strategy_cutoff",)


def _later_retrieval(batch: DerivativesEvidenceBatch) -> str:
    if batch.cache.file_rehydrated or any(code in _CACHE_PROVENANCE_GAPS for code in batch.reason_codes):
        return "unverified"
    if batch.cache.delivery_kind != "hit":
        return "not_applicable"
    original = batch.cache.original_acquired_at
    delivered = batch.cache.delivered_at
    if original is None or delivered is None:
        return "unverified"
    return "true" if delivered > original else "false"


def _flag(known: bool, moment: datetime | None, cutoff: datetime | None) -> str:
    if not known or moment is None or cutoff is None:
        return "unverified"
    return "true" if moment <= cutoff else "false"


def _span(bundle: FrozenDerivativesBundle) -> AcquisitionSpan:
    metrics: list[datetime] = []
    responses: list[datetime] = []
    acquired: list[datetime] = []
    delivered: list[datetime] = []
    for _label, batch in bundle.slots:
        if not isinstance(batch, DerivativesEvidenceBatch):
            continue
        if batch.acquisition.quality == QUALITY_VERIFIED and batch.acquisition.acquired_at is not None:
            acquired.append(batch.acquisition.acquired_at)
        if batch.response_time.at is not None and batch.response_time.quality == QUALITY_VERIFIED:
            responses.append(batch.response_time.at)
        if batch.cache.delivered_at is not None:
            delivered.append(batch.cache.delivered_at)
        for observation in batch.observations:
            if observation.metric_time.at is not None and observation.metric_time.quality == QUALITY_VERIFIED:
                metrics.append(observation.metric_time.at)
    metric_early, metric_late, _metric_span = _range(metrics)
    response_early, response_late, _response_span = _range(responses)
    acquired_early, acquired_late, acquired_span = _range(acquired)
    delivered_early, delivered_late, _delivered_span = _range(delivered)
    return AcquisitionSpan(
        earliest_metric_at=metric_early,
        latest_metric_at=metric_late,
        earliest_response_at=response_early,
        latest_response_at=response_late,
        earliest_acquired_at=acquired_early,
        latest_acquired_at=acquired_late,
        earliest_cache_delivered_at=delivered_early,
        latest_cache_delivered_at=delivered_late,
        acquisition_span_ms=acquired_span,
        simultaneous_exchange_snapshot=False,
    )


def _range(moments: Sequence[datetime]) -> tuple[str | None, str | None, int | None]:
    if not moments:
        return None, None, None
    earliest = min(moments)
    latest = max(moments)
    return earliest.isoformat(), latest.isoformat(), int((latest - earliest).total_seconds() * 1000)


def _slot_disposition(batch: Any) -> str:
    if batch is None:
        return "failed"
    if not isinstance(batch, DerivativesEvidenceBatch):
        return "failed"
    reasons = set(batch.reason_codes)
    if "fast_mode_skipped" in reasons:
        return "skipped"
    if "adapter_has_no_source_evidence" in reasons:
        return "unsupported"
    if "source_fetch_failed" in reasons or "evidence_representation_failed" in reasons:
        return "failed"
    if not batch.observations:
        return "empty"
    return "received"


def _population(venue: str, scan_mode: str) -> str:
    return f"requested_venue={venue}|scan_mode={scan_mode}"


def _attempt_disposition(attempt: StudyAttempt) -> str:
    if not attempt.assessed or attempt.record is None:
        return "unassessed"
    record = attempt.record
    if record.research_assessment.status != "assessed":
        return "research_clock_unavailable"
    strategy_error = _classification(record.strategy_assessment) == "evaluation_error"
    research_error = _classification(record.research_assessment) == "evaluation_error"
    if strategy_error or research_error:
        return "evaluation_error"
    return "assessed_both"


def _classification(assessment: CutoffAssessment) -> str:
    if assessment.evaluation is None:
        return "not_available"
    return assessment.evaluation.classification


class _PopulationTally:
    def __init__(self) -> None:
        self.attempts = 0
        self.dispositions: Counter[str] = Counter()
        self.slot_dispositions: Counter[str] = Counter()
        self.received_rows = 0
        self.unique = 0
        self.verified: dict[str, int] = {STRATEGY_CUTOFF_ROLE: 0, RESEARCH_CUTOFF_ROLE: 0}
        self.admissible: dict[str, int] = {STRATEGY_CUTOFF_ROLE: 0, RESEARCH_CUTOFF_ROLE: 0}
        self.fresh: dict[str, int] = {STRATEGY_CUTOFF_ROLE: 0, RESEARCH_CUTOFF_ROLE: 0}
        self.admissible_oi: dict[str, int] = {STRATEGY_CUTOFF_ROLE: 0, RESEARCH_CUTOFF_ROLE: 0}
        self.consumer_delivered: dict[str, int] = {STRATEGY_CUTOFF_ROLE: 0, RESEARCH_CUTOFF_ROLE: 0}
        self.oi_opportunities: dict[str, int] = {STRATEGY_CUTOFF_ROLE: 0, RESEARCH_CUTOFF_ROLE: 0}
        self.oi_valid: dict[str, int] = {STRATEGY_CUTOFF_ROLE: 0, RESEARCH_CUTOFF_ROLE: 0}
        self.oi_verified_change: dict[str, int] = {STRATEGY_CUTOFF_ROLE: 0, RESEARCH_CUTOFF_ROLE: 0}
        self.overlap: Counter[tuple[str, str, str]] = Counter()
        self.research_unavailable = 0
        self.exclusions: Counter[str] = Counter()


def _compile(study: CausalShadowStudy) -> dict[str, Any]:
    tallies: dict[str, _PopulationTally] = defaultdict(_PopulationTally)
    if not study.attempts:
        return _empty_compile()
    for attempt in study.attempts:
        tally = tallies[_population(attempt.requested_venue, attempt.scan_mode)]
        tally.attempts += 1
        disposition = _attempt_disposition(attempt)
        tally.dispositions[disposition] += 1
        if disposition == "unassessed":
            for _slot in METRIC_SLOTS:
                tally.slot_dispositions["unassessed"] += 1
            tally.exclusions["unassessed_attempt"] += 1
            continue
        record = attempt.record
        assert record is not None and record.bundle is not None
        _tally_record(tally, record)
    populations = []
    for key in sorted(tallies):
        populations.append(_population_report(key, tallies[key]))
    replay_complete = all((not attempt.assessed) or _inputs_retained(attempt) for attempt in study.attempts)
    coverage = _coverage(study, tallies, replay_complete)
    return {
        "definitions": _DEFINITIONS,
        "populations": populations,
        "replay_complete": replay_complete,
        "complete_verified_coverage": coverage,
        "percentages_are_not_additive": True,
        "trading_performance_claim": False,
    }


def _tally_record(tally: _PopulationTally, record: CausalObservationRecord) -> None:
    bundle = record.bundle
    assert bundle is not None
    evidence = bundle.evidence_map()
    raw_rows: list[tuple[str, DerivativesObservation, DerivativesEvidenceBatch]] = []
    for slot in METRIC_SLOTS:
        batch = evidence.get(slot)
        disposition = _slot_disposition(batch) if slot in evidence or batch is not None else "failed"
        if slot not in evidence:
            disposition = "failed"
            tally.overlap[( "not_cutoff_specific", "missing_evidence_slot", "metric_slot")] += 1
        tally.slot_dispositions[disposition] += 1
        if disposition == "skipped":
            tally.overlap[("not_cutoff_specific", "fast_mode_skipped", "metric_slot")] += 1
        if disposition in {"skipped", "failed", "empty", "unsupported"} and slot == "long_short_ratio":
            tally.overlap[("not_cutoff_specific", "optional_data_unavailable", "metric_slot")] += 1
        if isinstance(batch, DerivativesEvidenceBatch) and any(code in _CACHE_PROVENANCE_GAPS for code in batch.reason_codes):
            tally.overlap[("not_cutoff_specific", "cache_without_acquisition_provenance", "metric_slot")] += 1
        if disposition == "received" and isinstance(batch, DerivativesEvidenceBatch):
            for observation in batch.observations:
                raw_rows.append((slot, observation, batch))
                if observation.metric_time.quality != QUALITY_VERIFIED or observation.metric_time.at is None:
                    tally.overlap[("not_cutoff_specific", "missing_source_timestamp", "observation")] += 1
    tally.received_rows += len(raw_rows)
    grouped: dict[tuple[Any, ...], list[tuple[str, DerivativesObservation, DerivativesEvidenceBatch]]] = {}
    for item in raw_rows:
        grouped.setdefault(_dedup_key(item[1]), []).append(item)
    tally.unique += len(grouped)
    if record.research_assessment.status != "assessed":
        tally.research_unavailable += 1
        tally.exclusions["research_assessment_unavailable"] += 1
        tally.overlap[(RESEARCH_CUTOFF_ROLE, "observation_clock_or_evaluation_error", "attempt")] += 1
    if _classification(record.strategy_assessment) == "evaluation_error":
        tally.overlap[(STRATEGY_CUTOFF_ROLE, "observation_clock_or_evaluation_error", "attempt")] += 1
    if _classification(record.research_assessment) == "evaluation_error":
        tally.overlap[(RESEARCH_CUTOFF_ROLE, "observation_clock_or_evaluation_error", "attempt")] += 1
    _tally_cutoff(tally, record, grouped, STRATEGY_CUTOFF_ROLE, record.strategy_assessment, bundle.strategy_cutoff)
    if record.research_assessment.status == "assessed":
        _tally_cutoff(
            tally,
            record,
            grouped,
            RESEARCH_CUTOFF_ROLE,
            record.research_assessment,
            record.bundle.observation.cutoff if record.bundle is not None else None,
        )


def _tally_cutoff(
    tally: _PopulationTally,
    record: CausalObservationRecord,
    grouped: Mapping[tuple[Any, ...], Sequence[tuple[str, DerivativesObservation, DerivativesEvidenceBatch]]],
    role: str,
    assessment: CutoffAssessment,
    cutoff: datetime | None,
) -> None:
    bundle = record.bundle
    assert bundle is not None
    visible = evidence_visible_at_cutoff(
        decision_cutoff=cutoff,
        instrument=bundle.instrument if isinstance(bundle.instrument, InstrumentMetadata) else None,
        open_interest=_batch(bundle.evidence_map().get("open_interest")),
        open_interest_history=_batch(bundle.evidence_map().get("open_interest_history")),
        funding=_batch(bundle.evidence_map().get("funding_rate")),
        funding_history=_batch(bundle.evidence_map().get("funding_history")),
        account_ratio=_batch(bundle.evidence_map().get("long_short_ratio")),
    )
    bound_by_id = {
        id(raw): bound
        for slot, argument in _SLOT_ARGUMENT.items()
        for raw, bound in zip(
            getattr(_batch(bundle.evidence_map().get(slot)), "observations", ()),
            getattr(visible.get(argument), "observations", ()),
            strict=False,
        )
    }
    claims = {claim.metric: claim for claim in assessment.availability}
    instrument = visible.get("instrument")
    allowance_funding = getattr(instrument, "funding_interval_ms", None) if getattr(instrument, "quality", None) == QUALITY_VERIFIED else None
    admission = visible_selector_admission(
        decision_cutoff=cutoff,
        instrument=instrument if isinstance(instrument, InstrumentMetadata) else None,
        open_interest=_batch(visible.get("open_interest")),
        open_interest_history=_batch(visible.get("open_interest_history")),
        funding=_batch(visible.get("funding")),
        funding_history=_batch(visible.get("funding_history")),
        account_ratio=_batch(visible.get("account_ratio")),
    )
    slot_by_bound_id = {
        id(observation): slot
        for slot, argument in _SLOT_ARGUMENT.items()
        for observation in getattr(_batch(visible.get(argument)), "observations", ())
    }
    verified = admissible = fresh = admissible_oi = delivered = 0
    for _key, items in grouped.items():
        bound_items = []
        for slot, raw, batch in items:
            bound = bound_by_id.get(id(raw), raw)
            bound_batch = visible.get(_SLOT_ARGUMENT.get(slot, ""), batch)
            bound_items.append((slot, bound, bound_batch if isinstance(bound_batch, DerivativesEvidenceBatch) else batch, claims.get(slot)))
        if all(item.semantic_quality == QUALITY_VERIFIED for _slot, item, _batch, _claim in bound_items):
            verified += 1
        else:
            tally.overlap[(role, "incomplete_or_unverified", "unique_observation")] += 1
        if any(_unknown_unit(item) for _slot, item, _batch, _claim in bound_items):
            tally.overlap[(role, "unknown_units", "unique_observation")] += 1
        if any(_has_identity(item) for _slot, item, _batch, _claim in bound_items):
            tally.overlap[(role, "instrument_identity_conflicts", "evidence_item")] += 1
        temporal = [
            observation_temporal_reasons(item, cutoff, batch)
            for _slot, item, batch, _claim in bound_items
        ]
        if any("acquisition_after_cutoff" in codes for codes in temporal):
            tally.overlap[(role, "acquisition_after_cutoff", "unique_observation")] += 1
        if any("missing_acquisition_timestamp" in codes or "acquisition_capture_failed" in codes for codes in temporal):
            tally.overlap[(role, "missing_acquisition_provenance", "unique_observation")] += 1
        temporally_clear = all(not any(code in _BLOCKING_TEMPORAL for code in codes) for codes in temporal)
        semantically_ok = all(item.semantic_quality == QUALITY_VERIFIED for _slot, item, _batch, _claim in bound_items)
        consumer_ok = all(claim is not None and claim.consumer_delivered_before_cutoff == "true" for _slot, _item, _batch, claim in bound_items)
        rejection_codes = [
            code
            for _slot, item, _batch, _claim in bound_items
            for code in admission.group_rejections.get(id(item), ())
        ]
        if rejection_codes:
            for code in dict.fromkeys(rejection_codes):
                tally.overlap[(role, code, "unique_observation")] += 1
        if consumer_ok:
            delivered += 1
        role_admissible = (
            temporally_clear
            and semantically_ok
            and not rejection_codes
            and (role != RESEARCH_CUTOFF_ROLE or consumer_ok)
        )
        if role_admissible:
            admissible += 1
            if any(item.metric == "open_interest" for _slot, item, _batch, _claim in bound_items):
                admissible_oi += 1
            if _group_fresh(bound_items, cutoff, allowance_funding):
                fresh += 1
    if instrument is not None and _has_identity_codes(getattr(instrument, "reason_codes", ())):
        tally.overlap[(role, "instrument_identity_conflicts", "evidence_item")] += 1
    if assessment.evaluation is not None and "funding_cadence_unknown" in assessment.evaluation.reason_codes:
        tally.overlap[(role, "funding_cadence_uncertainty", "attempt")] += 1
    tally.verified[role] += verified
    tally.admissible[role] += admissible
    tally.fresh[role] += fresh
    tally.admissible_oi[role] += admissible_oi
    tally.consumer_delivered[role] += delivered
    tally.oi_opportunities[role] += 1
    evaluation = assessment.evaluation
    pair_visible = role != RESEARCH_CUTOFF_ROLE or _research_selected_pair_delivered(
        admission.selected_oi_ids,
        claims,
        slot_by_bound_id,
    )
    if (
        pair_visible
        and evaluation is not None
        and evaluation.oi_change_pct != NA
        and evaluation.oi_change_context != "not_admissible"
    ):
        tally.oi_valid[role] += 1
        if "notional_not_position_growth" not in evaluation.reason_codes and "zero_baseline" not in evaluation.reason_codes:
            tally.oi_verified_change[role] += 1
    for items in grouped.values():
        bound_items = []
        for slot, raw, batch in items:
            bound = bound_by_id.get(id(raw), raw)
            bound_batch = visible.get(_SLOT_ARGUMENT.get(slot, ""), batch)
            bound_items.append((bound, bound_batch if isinstance(bound_batch, DerivativesEvidenceBatch) else batch))
        if any(
            "stale_for_current_context" in observation_context_freshness(item.metric_time.at, cutoff, _allowance(item, allowance_funding))[1]
            for item, _batch in bound_items
        ):
            tally.overlap[(role, "stale_measurements", "unique_observation")] += 1


def _research_selected_pair_delivered(
    selected_ids: tuple[int, ...],
    claims: Mapping[str, Any],
    slot_by_bound_id: Mapping[int, str],
) -> bool:
    """The selected current and previous OI observations must both be delivered."""

    if len(selected_ids) != 2:
        return False
    for obs_id in selected_ids:
        claim = claims.get(slot_by_bound_id.get(obs_id, ""))
        if claim is None or claim.consumer_delivered_before_cutoff != "true":
            return False
    return True


def _group_fresh(bound_items: Sequence[tuple[Any, ...]], cutoff: datetime | None, allowance_funding: int | None) -> bool:
    for _slot, item, _batch, _claim in bound_items:
        context, _reasons = observation_context_freshness(item.metric_time.at, cutoff, _allowance(item, allowance_funding))
        if context != "current":
            return False
    return True


def _allowance(observation: DerivativesObservation, funding_allowance: int | None) -> int | None:
    if observation.metric == "funding":
        return funding_allowance
    return series_period_allowance_ms(observation.series_period)


def _population_report(population: str, tally: _PopulationTally) -> dict[str, Any]:
    opportunities = tally.attempts * len(METRIC_SLOTS)
    rates: list[dict[str, Any]] = []
    rates.append(_rate(
        "study_attempts",
        tally.attempts,
        tally.attempts,
        "symbol_iteration",
        "not_cutoff_specific",
        population,
        (),
        "every targeted symbol iteration, including errors and skips",
    ))
    for name in ("unassessed", "research_clock_unavailable", "evaluation_error", "assessed_both"):
        rates.append(_rate(
            f"attempt_disposition_{name}",
            tally.dispositions[name],
            tally.attempts,
            "symbol_iteration",
            "not_cutoff_specific",
            population,
            (),
            "mutually exclusive attempt disposition",
            exclusive="attempt_disposition",
        ))
    for name in ("requested_received", "skipped", "unsupported", "failed", "empty", "unassessed"):
        label = "received" if name == "requested_received" else name
        rates.append(_rate(
            f"metric_disposition_{label}",
            tally.slot_dispositions[label],
            opportunities,
            "metric_slot",
            "not_cutoff_specific",
            population,
            (),
            "mutually exclusive metric-slot disposition",
            exclusive="metric_slot_disposition",
        ))
    rates.append(_rate("received_source_rows", tally.received_rows, tally.received_rows, "source_row", "not_cutoff_specific", population, ("skipped", "failed", "empty", "unsupported", "unassessed"), "raw rows in received slots before deduplication"))
    rates.append(_rate("unique_observations", tally.unique, tally.received_rows, "source_row_to_unique_observation", "not_cutoff_specific", population, (), "identity/time deduplication inside each attempt"))
    for role in (STRATEGY_CUTOFF_ROLE, RESEARCH_CUTOFF_ROLE):
        rule = (
            "shared_conflict_rules_on_every_retained_identity_time_group"
            if role == STRATEGY_CUTOFF_ROLE
            else "shared_conflict_rules_on_every_retained_identity_time_group_and_recorded_consumer_delivery"
        )
        exclusions = ("research_assessment_unavailable",) if role == RESEARCH_CUTOFF_ROLE and tally.research_unavailable else ()
        rates.append(_rate("semantically_verified_observations", tally.verified[role], tally.unique, "unique_observation", role, population, exclusions, "bound instrument visibility at this cutoff"))
        rates.append(_rate("causally_admissible_observations", tally.admissible[role], tally.unique, "unique_observation", role, population, exclusions, rule))
        rates.append(_rate("fresh_observations", tally.fresh[role], tally.admissible[role], "causally_admissible_unique_observation", role, population, exclusions, "fresh among the admissible population; empty admissible population is N/A"))
        rates.append(_rate("admissible_oi_observations", tally.admissible_oi[role], tally.unique, "unique_observation", role, population, exclusions, "open-interest observations inside the causal admission rule"))
        rates.append(_rate("consumer_delivered_before_cutoff_observations", tally.consumer_delivered[role], tally.unique, "unique_observation", role, population, exclusions, "recorded consumer delivery at or before the labeled cutoff"))
        rates.append(_rate("oi_pair_opportunities", tally.oi_opportunities[role], tally.oi_opportunities[role], "assessed_attempt", role, population, exclusions, "one pair opportunity per attempt assessed at this cutoff"))
        pair_rule = (
            "labeled P3 evaluation produced a native quantity change"
            if role == STRATEGY_CUTOFF_ROLE
            else "selected OI pair and its batches were delivered at or before the research observation, and the labeled evaluation produced a native quantity change"
        )
        verified_rule = (
            "labeled P3 native quantity change without notional substitution or zero baseline"
            if role == STRATEGY_CUTOFF_ROLE
            else "selected delivered OI pair whose labeled evaluation is a native quantity change without notional substitution or zero baseline"
        )
        rates.append(_rate("valid_oi_pairs", tally.oi_valid[role], tally.oi_opportunities[role], "oi_pair_opportunity", role, population, exclusions, pair_rule))
        rates.append(_rate("verified_oi_quantity_changes", tally.oi_verified_change[role], tally.oi_opportunities[role], "oi_pair_opportunity", role, population, exclusions, verified_rule))
    overlapping = [
        {
            "name": name,
            "count": count,
            "unit": unit,
            "cutoff_role": role,
            "population": population,
            "mutually_exclusive": False,
            "percentages_are_not_additive": True,
        }
        for (role, name, unit), count in sorted(tally.overlap.items())
    ]
    return {
        "population": population,
        "attempt_disposition_sum": sum(tally.dispositions.values()),
        "metric_disposition_sum": sum(tally.slot_dispositions.values()),
        "metric_opportunities": opportunities,
        "rates": rates,
        "overlapping_reason_counts": overlapping,
    }


def _coverage(study: CausalShadowStudy, tallies: Mapping[str, _PopulationTally], replay_complete: bool) -> bool:
    if not replay_complete or not study.attempts or any(not attempt.assessed for attempt in study.attempts):
        return False
    for tally in tallies.values():
        if tally.research_unavailable or tally.dispositions["evaluation_error"] or tally.dispositions["unassessed"]:
            return False
        if any(tally.slot_dispositions[name] for name in ("skipped", "failed", "empty", "unsupported", "unassessed")):
            return False
        if tally.unique == 0 or tally.verified[RESEARCH_CUTOFF_ROLE] != tally.unique:
            return False
        if tally.admissible[RESEARCH_CUTOFF_ROLE] != tally.unique or tally.admissible[STRATEGY_CUTOFF_ROLE] != tally.unique:
            return False
    return True


def _empty_compile() -> dict[str, Any]:
    return {
        "definitions": _DEFINITIONS,
        "populations": [
            {
                "population": "empty",
                "attempt_disposition_sum": 0,
                "metric_disposition_sum": 0,
                "metric_opportunities": 0,
                "rates": [
                    _rate(
                        "study_attempts",
                        0,
                        0,
                        "symbol_iteration",
                        "not_cutoff_specific",
                        "empty",
                        (),
                        "empty study",
                    )
                ],
                "overlapping_reason_counts": [],
            }
        ],
        "replay_complete": False,
        "complete_verified_coverage": False,
        "percentages_are_not_additive": True,
        "trading_performance_claim": False,
    }


def _rate(
    name: str,
    numerator: int,
    denominator: int,
    unit: str,
    cutoff_role: str,
    population: str,
    exclusions: tuple[str, ...],
    rule: str,
    exclusive: str | None = None,
) -> dict[str, Any]:
    value: str | None
    if denominator == 0:
        value = NA
    else:
        value = format((Decimal(numerator) / Decimal(denominator)).quantize(_OUTPUT_QUANT), "f")
    return {
        "name": name,
        "numerator": numerator,
        "denominator": denominator,
        "value": value,
        "unit": unit,
        "cutoff_role": cutoff_role,
        "population": population,
        "exclusions": list(exclusions),
        "rule": rule,
        "mutually_exclusive_within": exclusive,
    }


def _inputs_retained(attempt: StudyAttempt) -> bool:
    return bool(attempt.record and attempt.record.bundle and attempt.record.bundle.retain_inputs)


def _replay_payload(payload: Mapping[str, Any]) -> CausalShadowStudy:
    attempts: list[StudyAttempt] = []
    for item in payload.get("attempts") or ():
        if not isinstance(item, Mapping):
            continue
        if not item.get("assessed"):
            attempts.append(
                StudyAttempt(
                    attempt_id=str(item.get("attempt_id") or NA),
                    symbol=str(item.get("symbol") or NA),
                    requested_venue=str(item.get("requested_venue") or NA),
                    scan_mode=str(item.get("scan_mode") or NA),
                    assessed=False,
                    unassessed_reason=None if item.get("unassessed_reason") is None else str(item.get("unassessed_reason")),
                    record=None,
                )
            )
            continue
        inputs = item.get("inputs")
        if not isinstance(inputs, Mapping):
            raise ValueError("artifact_incomplete")
        record = assess_frozen_bundle(_bundle_from_inputs(_decode(inputs)))
        attempts.append(
            StudyAttempt(
                attempt_id=record.attempt_id,
                symbol=record.symbol,
                requested_venue=record.requested_venue,
                scan_mode=record.scan_mode,
                assessed=True,
                unassessed_reason=None,
                record=record,
            )
        )
    return CausalShadowStudy(
        attempts=tuple(attempts),
        requested_venue=str(payload.get("requested_venue") or NA),
        synthetic=bool(payload.get("synthetic")),
        code_revision=str(payload.get("code_revision") or NA),
        shadow_requested=bool(payload.get("shadow_requested", True)),
    )


def _bundle_from_inputs(decoded: Any) -> FrozenDerivativesBundle:
    if not isinstance(decoded, Mapping):
        raise ValueError("artifact_incomplete")
    evidence = {str(item["label"]): item["batch"] for item in decoded["slots"]}
    return freeze_derivatives_bundle(
        evidence,
        instrument=decoded["instrument"],
        strategy_cutoff=decoded["strategy_cutoff"],
        observation=decoded["observation"],
        symbol=str(decoded["symbol"]),
        requested_venue=str(decoded["requested_venue"]),
        scan_mode=str(decoded["scan_mode"]),
        iteration_index=decoded["iteration_index"],
        synthetic=bool(decoded["synthetic"]),
        assumptions=tuple(decoded.get("assumptions") or ()),
        retain_inputs=True,
        code_revision=str(decoded.get("code_revision") or NA),
        attempt_id=str(decoded["attempt_id"]),
    )


def _encode(value: Any) -> Any:
    if isinstance(value, datetime):
        return {"__utc__": value.isoformat()}
    if isinstance(value, Decimal):
        return {"__decimal__": format(value, "f")}
    if is_dataclass(value) and not isinstance(value, type):
        return {"__type__": type(value).__name__, **{item.name: _encode(getattr(value, item.name)) for item in fields(value)}}
    if isinstance(value, tuple | list):
        return [_encode(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _encode(item) for key, item in value.items()}
    return value


def _decode(value: Any) -> Any:
    if isinstance(value, list):
        return tuple(_decode(item) for item in value)
    if isinstance(value, dict):
        if set(value) == {"__utc__"}:
            return normalize_utc_timestamp(value["__utc__"], field_name="replay_timestamp")
        if set(value) == {"__decimal__"}:
            return Decimal(str(value["__decimal__"]))
        if "__type__" in value:
            cls = _TYPES[str(value["__type__"])]
            kwargs = {key: _decode(item) for key, item in value.items() if key != "__type__"}
            return cls(**kwargs)
        return {str(key): _decode(item) for key, item in value.items()}
    return value


def _dedup_key(observation: DerivativesObservation) -> tuple[Any, ...]:
    primary = observation.quantities[0] if observation.quantities else None
    acquired = observation.acquisition.acquired_at.isoformat() if observation.acquisition.acquired_at is not None else None
    return (
        observation.metric,
        observation.exchange,
        observation.endpoint,
        observation.identity.requested_symbol,
        observation.identity.returned_symbol,
        observation.metric_time.raw,
        observation.metric_time.quality,
        None if primary is None else primary.raw_field,
        None if primary is None else primary.raw_value,
        None if primary is None else primary.counting_convention,
        None if primary is None else primary.unit,
        None if primary is None else primary.unit_asset,
        observation.rate_kind,
        observation.subtype_declarations,
        acquired,
        observation.acquisition.quality,
    )


def _unknown_unit(observation: DerivativesObservation) -> bool:
    if "unit_unknown" in observation.reason_codes or "unit_unverified" in observation.reason_codes:
        return True
    return any(quantity.unit in {"unknown", NA} or "unit_unknown" in quantity.reason_codes for quantity in observation.quantities)


def _has_identity(observation: DerivativesObservation) -> bool:
    return any(code in _IDENTITY_CODES for code in observation.reason_codes)


def _has_identity_codes(codes: Any) -> bool:
    if not _reason_codes_are_text(codes):
        return False
    return any(code in _IDENTITY_CODES for code in codes)


def _reason_codes_are_text(codes: Any) -> bool:
    if isinstance(codes, (str, bytes)) or not isinstance(codes, Sequence):
        return False
    return all(isinstance(code, str) for code in codes)


def _evidence_exchange(slots: Sequence[tuple[str, Any]], instrument: Any) -> str:
    exchange = getattr(instrument, "exchange", None)
    if isinstance(exchange, str) and exchange and exchange != NA:
        return exchange
    for _label, batch in slots:
        observations = getattr(batch, "observations", ())
        if observations:
            return str(observations[0].exchange)
    return NA


def _instrument_attempt(instrument: Any) -> str:
    raw = getattr(instrument, "reason_codes", ())
    codes = raw if _reason_codes_are_text(raw) else ()
    if "instrument_lookup_withheld" in codes:
        return "withheld"
    if "instrument_lookup_not_waited" in codes:
        return "not_waited"
    if "instrument_lookup_failed" in codes:
        return "failed"
    if (
        instrument is None
        or "adapter_has_no_source_evidence" in codes
        or "instrument_representation_invalid" in codes
        or "shadow_bundle_construction_failed" in codes
    ):
        return "unavailable"
    return "completed"


def _unassessed_reason(result: Any, *, shadow_requested: bool) -> str:
    if not shadow_requested:
        return "shadow_not_requested"
    outcome = getattr(result, "iteration_outcome", None)
    if outcome:
        return str(outcome)
    status = getattr(result, "status", None)
    value = getattr(status, "value", status)
    return str(value or "result_has_no_causal_observation")


def _batch(value: Any) -> DerivativesEvidenceBatch | None:
    return value if isinstance(value, DerivativesEvidenceBatch) else None


def _try_normalize(value: datetime | None, field: str) -> tuple[datetime | None, tuple[str, ...]]:
    if value is None:
        return None, ()
    try:
        return normalize_utc_timestamp(value, field_name=field), ()
    except (TypeError, ValueError, OverflowError, OSError):
        return None, ("malformed_cutoff",)


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    try:
        return normalize_utc_timestamp(value, field_name="timestamp").isoformat()
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _index_text(value: int | None) -> str:
    return "none" if value is None else str(value)


def _millis(moment: datetime) -> int:
    return int(moment.timestamp() * 1000)


def _utc() -> Any:
    from datetime import UTC

    return UTC


def _time_summary(samples: Sequence[int]) -> dict[str, int]:
    ordered = sorted(samples)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        median = ordered[middle]
    else:
        median = (ordered[middle - 1] + ordered[middle]) // 2
    return {"min": ordered[0], "p50": median, "max": ordered[-1], "samples": len(ordered)}


def _public(value: Any) -> Any:
    if isinstance(value, tuple):
        return [_public(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _public(item) for key, item in value.items()}
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _claim_dict(claim: AvailabilityClaim) -> dict[str, Any]:
    return _public(
        {
            "metric": claim.metric,
            "cutoff_role": claim.cutoff_role,
            "collector_acquired_before_cutoff": claim.collector_acquired_before_cutoff,
            "cache_retained_before_cutoff": claim.cache_retained_before_cutoff,
            "consumer_delivered_before_cutoff": claim.consumer_delivered_before_cutoff,
            "later_cache_retrieval_of_earlier_acquisition": claim.later_cache_retrieval_of_earlier_acquisition,
            "delivery_kind": claim.delivery_kind,
            "endpoint": claim.endpoint,
            "request_params": claim.request_params,
            "metric_times": claim.metric_times,
            "response_at": claim.response_at,
            "acquired_at": claim.acquired_at,
            "cache_delivered_at": claim.cache_delivered_at,
            "original_acquired_at": claim.original_acquired_at,
            "file_rehydrated": claim.file_rehydrated,
            "reason_codes": claim.reason_codes,
            "establishes": claim.establishes,
            "does_not_establish": claim.does_not_establish,
        }
    )


def _assessment_dict(assessment: CutoffAssessment) -> dict[str, Any]:
    return {
        "cutoff_role": assessment.cutoff_role,
        "cutoff": assessment.cutoff,
        "status": assessment.status,
        "evaluator_parameter": assessment.evaluator_parameter,
        "evaluator_parameter_role": assessment.cutoff_role,
        "evaluation": None if assessment.evaluation is None else assessment.evaluation.to_dict(),
        "availability": [_claim_dict(item) for item in assessment.availability],
        "establishes": list(assessment.establishes),
        "does_not_establish": list(assessment.does_not_establish),
        "reason_codes": list(assessment.reason_codes),
    }


def _record_dict(record: CausalObservationRecord) -> dict[str, Any]:
    boundary = record.observation_boundary
    return {
        "attempt_id": record.attempt_id,
        "symbol": record.symbol,
        "requested_venue": record.requested_venue,
        "evidence_exchange": record.evidence_exchange,
        "scan_mode": record.scan_mode,
        "iteration_index": record.iteration_index,
        "observation_role": record.observation_role,
        "strategy_decision_cutoff": record.strategy_decision_cutoff,
        "research_observation_cutoff": record.research_observation_cutoff,
        "strategy_evaluation_cutoff_matches": record.strategy_evaluation_cutoff_matches,
        "observation_boundary": {
            "cutoff": _iso(boundary.cutoff),
            "quality": boundary.quality,
            "reason_codes": list(boundary.reason_codes),
            "meaning": boundary.meaning,
            "description": boundary.description,
        },
        "strategy_assessment": _assessment_dict(record.strategy_assessment),
        "research_assessment": _assessment_dict(record.research_assessment),
        "acquisition_span": {
            "earliest_metric_at": record.acquisition_span.earliest_metric_at,
            "latest_metric_at": record.acquisition_span.latest_metric_at,
            "earliest_response_at": record.acquisition_span.earliest_response_at,
            "latest_response_at": record.acquisition_span.latest_response_at,
            "earliest_acquired_at": record.acquisition_span.earliest_acquired_at,
            "latest_acquired_at": record.acquisition_span.latest_acquired_at,
            "earliest_cache_delivered_at": record.acquisition_span.earliest_cache_delivered_at,
            "latest_cache_delivered_at": record.acquisition_span.latest_cache_delivered_at,
            "acquisition_span_ms": record.acquisition_span.acquisition_span_ms,
            "simultaneous_exchange_snapshot": False,
        },
        "assumptions": list(record.assumptions),
        "synthetic": record.synthetic,
        "policy_version": record.policy_version,
        "evaluator_freshness_policy": record.evaluator_freshness_policy,
        "code_revision": record.code_revision,
        "mode": record.mode,
        "enforcement": record.enforcement,
        "validated_confirmation": record.validated_confirmation,
        "trading_performance_claim": record.trading_performance_claim,
    }


def _inputs_dict(bundle: FrozenDerivativesBundle) -> dict[str, Any]:
    return _encode(
        {
            "strategy_cutoff": bundle.strategy_cutoff,
            "observation": bundle.observation,
            "instrument": bundle.instrument,
            "slots": [{"label": label, "batch": batch} for label, batch in bundle.slots],
            "symbol": bundle.symbol,
            "requested_venue": bundle.requested_venue,
            "scan_mode": bundle.scan_mode,
            "iteration_index": bundle.iteration_index,
            "synthetic": bundle.synthetic,
            "assumptions": bundle.assumptions,
            "code_revision": bundle.code_revision,
            "attempt_id": bundle.attempt_id,
        }
    )


def _study_to_dict(study: CausalShadowStudy) -> dict[str, Any]:
    compiled = _compile(study)
    attempts = []
    for attempt in study.attempts:
        payload: dict[str, Any] = {
            "attempt_id": attempt.attempt_id,
            "symbol": attempt.symbol,
            "requested_venue": attempt.requested_venue,
            "scan_mode": attempt.scan_mode,
            "assessed": attempt.assessed,
            "unassessed_reason": attempt.unassessed_reason,
            "record": None if attempt.record is None else _record_dict(attempt.record),
            "inputs": None,
        }
        if attempt.record is not None and attempt.record.bundle is not None and attempt.record.bundle.retain_inputs:
            payload["inputs"] = _inputs_dict(attempt.record.bundle)
        attempts.append(payload)
    return {
        "artifact_kind": "p4_causal_shadow_study",
        "policy_version": POLICY_VERSION,
        "mode": "shadow",
        "enforcement": ENFORCEMENT,
        "validated_confirmation": False,
        "trading_performance_claim": False,
        "synthetic": study.synthetic,
        "code_revision": study.code_revision,
        "shadow_requested": study.shadow_requested,
        "requested_venue": study.requested_venue,
        "replay_complete": compiled["replay_complete"],
        "complete_verified_coverage": compiled["complete_verified_coverage"],
        "percentages_are_not_additive": True,
        "definitions": compiled["definitions"],
        "populations": compiled["populations"],
        "attempts": attempts,
    }


CausalShadowStudy.to_dict = _study_to_dict  # type: ignore[method-assign]


_DEFINITIONS = {
    "study_attempts": "Every targeted symbol-iteration, including errors, skips, and attempts with no final assessment.",
    "metric_opportunities": "Five scanner derivative slots per attempt: open_interest, open_interest_history, funding_rate, funding_history, long_short_ratio.",
    "metric_dispositions": "Mutually exclusive slot states: received, skipped, unsupported, failed, empty, unassessed.",
    "received_source_rows": "Raw observation rows in received slots before deduplication.",
    "unique_observations": "Rows collapsed inside one attempt when metric, venue, endpoint, symbol, metric time, primary raw value, counting convention, unit, subtype, and acquisition provenance match. Conflicting values and different series stay distinct. Totals sum per-attempt unique counts. Received-row denominators stay on the raw rows.",
    "semantically_verified_observations": "Unique observations whose instrument-bound semantics are verified at the labeled cutoff. A conflicting group can still contain individually verified rows.",
    "causally_admissible_observations": "Temporal and semantic gates, plus conflict validation of every retained identity and time group. Latest-pair selection stays on the existing selectors and does not define which older groups are checked. A duplicate_conflict, conflicting_provenance, or contradictory_funding_evidence mark excludes that group. Raw rows and unique observations stay in their denominators. Observations outside the conflicting group stay admissible. Research admission also requires recorded consumer delivery at or before the observation event. A later admissible observation does not make evidence available to the earlier strategy decision.",
    "fresh_observations": "Admissible observations whose existing cadence-or-period freshness helper returns current. The denominator is the admissible population. An empty admissible population is N/A.",
    "oi_pairs": "One opportunity per attempt assessed at that cutoff. Strategy-role valid and verified counts follow the labeled P3 evaluation. Research-role counts also require the selected current and previous observations, and the batches that carry them, to be delivered at or before the research observation. A verified quantity change is a native quantity result that is not notional growth and not a zero baseline.",
    "attempts_assessed_at_both_cutoffs": "Exclusive disposition assessed_both. Clock failures, evaluation errors, and unassessed attempts are separate dispositions and are not dropped.",
    "overlapping_reasons": "Reason counts are not a partition. Their percentages are not additive and are not required to sum to 100 percent.",
    "cache_retention": "P3 does not record a cache insertion instant on the evidence clock. Retention before a cutoff is not_recorded. TTL is not freshness and is not historical availability.",
    "replay": "Replay uses retained inputs and the recorded cutoffs. It does not sample a clock. Bounded lineage without retained inputs is not a complete replay record.",
}


for _cls in (
    TimeFact,
    QuantityFact,
    RequestIdentity,
    AcquisitionFact,
    CacheFact,
    DerivativesObservation,
    DerivativesEvidenceBatch,
    InstrumentMetadata,
):
    _register(_cls)


__all__ = [
    "BOUNDARY_DESCRIPTION",
    "METRIC_SLOTS",
    "OBSERVATION_ROLE",
    "POLICY_VERSION",
    "RESEARCH_CUTOFF_ROLE",
    "STRATEGY_CUTOFF_ROLE",
    "AvailabilityClaim",
    "CausalObservationRecord",
    "CausalShadowStudy",
    "CutoffAssessment",
    "FrozenDerivativesBundle",
    "ObservationBoundary",
    "assess_frozen_bundle",
    "build_synthetic_causal_fixture",
    "build_verified_coverage_fixture",
    "capture_observation_boundary",
    "freeze_derivatives_bundle",
    "measure_causal_evaluation_overhead",
    "replay_dev_causal_shadow_artifact",
    "study_from_records",
    "study_from_symbol_results",
    "unavailable_causal_record",
    "write_dev_causal_shadow_artifact",
]
