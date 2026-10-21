"""Immutable source evidence for existing open-interest, funding, and account-ratio reads.

Evidence is captured at the adapter response boundary and carried beside the legacy
DTO or Decimal. It does not change those legacy values, and it does not decide
whether a setup is eligible.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.data.candle_batch_evidence import default_capture_clock
from app.data.candle_integrity import normalize_utc_timestamp
from app.data.dtos import NA, FundingDTO, OpenInterestDTO

OUTPUT_QUANT = Decimal("0.00000001")

QUALITY_VERIFIED = "verified"
QUALITY_UNVERIFIED = "unverified"
QUALITY_MISSING = "missing"
QUALITY_CONFLICT = "conflict"

ACQUISITION_MEANING = "successful_adapter_response_boundary"
CACHE_DELIVERY_MEANING = "cache_read_is_not_acquisition"

UNIT_UNKNOWN = "unknown"
UNIT_BASE_QUANTITY = "base_asset_quantity"
UNIT_QUOTE_NOTIONAL = "quote_notional"
UNIT_FUNDING_FRACTION = "funding_fraction"
UNIT_ACCOUNT_RATIO = "account_population_ratio"

COUNT_OPEN_CONTRACT = "open_contract_quantity"
COUNT_BOTH_SIDES = "both_sides"
COUNT_SINGLE_SIDE = "single_side"
COUNT_NOT_APPLICABLE = "not_applicable"
COUNT_UNKNOWN = "unknown"

MULTIPLIER_UNKNOWN = "unknown"
MULTIPLIER_NOT_APPLICABLE = "not_applicable"
MULTIPLIER_UNVERIFIED = "unverified"

ENDPOINT_BINANCE_OPEN_INTEREST = "/fapi/v1/openInterest"
ENDPOINT_BINANCE_OPEN_INTEREST_HIST = "/futures/data/openInterestHist"
ENDPOINT_BINANCE_FUNDING = "/fapi/v1/fundingRate"
ENDPOINT_BINANCE_ACCOUNT_RATIO = "/futures/data/globalLongShortAccountRatio"
ENDPOINT_BINANCE_EXCHANGE_INFO = "/fapi/v1/exchangeInfo"
ENDPOINT_BYBIT_OPEN_INTEREST = "/v5/market/open-interest"
ENDPOINT_BYBIT_FUNDING = "/v5/market/funding/history"
ENDPOINT_BYBIT_ACCOUNT_RATIO = "/v5/market/account-ratio"
ENDPOINT_BYBIT_INSTRUMENT = "/v5/market/instruments-info"

RATE_SETTLED_FUNDING = "settled_funding"
RATE_ACCOUNT_POPULATION = "account_population"
RATE_OPEN_INTEREST = "open_interest"

REASON_LEGACY_FILE_CACHE = "legacy_cache_entry_lacks_acquisition_observation"
REASON_UNINSTRUMENTED_CACHE = "uninstrumented_cache_entry_lacks_acquisition_observation"
REASON_ADAPTER_WITHOUT_EVIDENCE = "adapter_has_no_source_evidence"
REASON_FAST_MODE_SKIPPED = "fast_mode_skipped"
REASON_EVIDENCE_REPRESENTATION_FAILED = "evidence_representation_failed"


@dataclass(frozen=True)
class TimeFact:
    role: str
    meaning: str
    at: datetime | None
    raw: str
    quality: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class QuantityFact:
    raw_field: str
    raw_value: str
    numeric_value: Decimal | None
    unit: str
    unit_asset: str
    counting_convention: str
    multiplier: str
    multiplier_applicability: str
    transformation: str
    quality: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class RequestIdentity:
    """Identity known from the request and the metric payload, before instrument metadata."""

    exchange: str
    requested_symbol: str
    returned_symbol: str | None
    symbol_provenance: str
    market_category: str
    category_provenance: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class AcquisitionFact:
    acquired_at: datetime | None
    meaning: str
    quality: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class CacheFact:
    delivery_kind: str
    delivered_at: datetime | None
    original_acquired_at: datetime | None
    file_rehydrated: bool
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class DerivativesObservation:
    metric: str
    exchange: str
    endpoint: str
    request_params: tuple[tuple[str, str], ...]
    identity: RequestIdentity
    quantities: tuple[QuantityFact, ...]
    metric_time: TimeFact
    response_time: TimeFact
    acquisition: AcquisitionFact
    series_period: str
    rate_kind: str
    semantic_quality: str
    reason_codes: tuple[str, ...]
    raw_retained: bool


@dataclass(frozen=True)
class DerivativesEvidenceBatch:
    metric: str
    endpoint: str
    request_params: tuple[tuple[str, str], ...]
    observations: tuple[DerivativesObservation, ...]
    acquisition: AcquisitionFact
    response_time: TimeFact
    cache: CacheFact
    semantic_quality: str
    reason_codes: tuple[str, ...]


@dataclass(frozen=True)
class MetricEvidenceDelivery:
    value: Any
    evidence: DerivativesEvidenceBatch


def sample_capture_clock(clock: Callable[[], datetime] | None) -> datetime:
    """Sample the injected adapter clock. Naive values follow the existing UTC convention."""

    sampled = (clock or default_capture_clock)()
    return normalize_utc_timestamp(sampled, field_name="adapter_acquisition")


def freeze_params(params: Mapping[str, Any] | None) -> tuple[tuple[str, str], ...]:
    if not params:
        return ()
    return tuple((str(key), str(value)) for key, value in sorted(params.items(), key=lambda item: str(item[0])))


def direct_cache_fact(acquired_at: datetime | None) -> CacheFact:
    return CacheFact(
        delivery_kind="not_applicable",
        delivered_at=None,
        original_acquired_at=acquired_at,
        file_rehydrated=False,
        reason_codes=(),
    )


def acquisition_fact(acquired_at: datetime | None, *, quality: str, reason_codes: tuple[str, ...] = ()) -> AcquisitionFact:
    return AcquisitionFact(
        acquired_at=acquired_at,
        meaning=ACQUISITION_MEANING,
        quality=quality,
        reason_codes=reason_codes,
    )


def missing_time(role: str, meaning: str, reason: str) -> TimeFact:
    return TimeFact(
        role=role,
        meaning=meaning,
        at=None,
        raw=NA,
        quality=QUALITY_MISSING,
        reason_codes=(reason,),
    )


def parse_time(value: Any, *, role: str, meaning: str) -> TimeFact:
    if value is None or value == "" or value == NA:
        return missing_time(role, meaning, "missing_metric_timestamp" if role == "metric" else "missing_exchange_response_time")
    raw = str(value)
    malformed = "malformed_metric_timestamp" if role == "metric" else "malformed_exchange_response_time"
    if isinstance(value, bool):
        return TimeFact(role, meaning, None, raw, QUALITY_UNVERIFIED, (malformed,))
    try:
        parsed = normalize_utc_timestamp(value, field_name=role)
    except (TypeError, ValueError, OverflowError, OSError):
        return TimeFact(role, meaning, None, raw, QUALITY_UNVERIFIED, (malformed,))
    return TimeFact(role, meaning, parsed, raw, QUALITY_VERIFIED, ())


def parse_quantity(
    value: Any,
    *,
    raw_field: str,
    unit: str,
    counting_convention: str,
    allow_negative: bool = False,
) -> QuantityFact:
    raw = NA if value is None else str(value)
    reasons: list[str] = []
    numeric: Decimal | None = None
    quality = QUALITY_VERIFIED
    if value is None or value == "" or value == NA:
        return QuantityFact(
            raw_field=raw_field,
            raw_value=NA,
            numeric_value=None,
            unit=unit,
            unit_asset=NA,
            counting_convention=counting_convention,
            multiplier=NA,
            multiplier_applicability=MULTIPLIER_UNKNOWN,
            transformation="none",
            quality=QUALITY_MISSING,
            reason_codes=("missing_quantity",),
        )
    if isinstance(value, bool) or not isinstance(value, (int, str, Decimal)):
        reasons.append("malformed_quantity")
        quality = QUALITY_UNVERIFIED
    else:
        try:
            numeric = Decimal(str(value))
        except (InvalidOperation, ValueError):
            numeric = None
            reasons.append("malformed_quantity")
            quality = QUALITY_UNVERIFIED
        else:
            if not numeric.is_finite():
                numeric = None
                reasons.append("non_finite_quantity")
                quality = QUALITY_UNVERIFIED
            elif numeric < 0 and not allow_negative:
                reasons.append("negative_open_interest")
                quality = QUALITY_UNVERIFIED
    if unit == UNIT_UNKNOWN and quality == QUALITY_VERIFIED:
        quality = QUALITY_UNVERIFIED
        reasons.append("unit_unknown")
    return QuantityFact(
        raw_field=raw_field,
        raw_value=raw,
        numeric_value=numeric,
        unit=unit,
        unit_asset=NA,
        counting_convention=counting_convention,
        multiplier=NA,
        multiplier_applicability=MULTIPLIER_UNKNOWN if unit in {UNIT_UNKNOWN, UNIT_BASE_QUANTITY} else MULTIPLIER_NOT_APPLICABLE,
        transformation="identity" if numeric is not None else "none",
        quality=quality,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _identity(
    *,
    exchange: str,
    requested_symbol: str,
    returned_symbol: str | None,
    market_category: str,
    category_from_response: str | None,
) -> RequestIdentity:
    reasons: list[str] = []
    requested = requested_symbol.strip().upper()
    returned = returned_symbol.strip().upper() if isinstance(returned_symbol, str) and returned_symbol.strip() else None
    if returned is None:
        provenance = "request_context"
        reasons.append("identity_from_request_context")
    elif returned != requested:
        provenance = "conflict"
        reasons.append("symbol_conflict")
    else:
        provenance = "response"
    if category_from_response is None:
        category_provenance = "request_context"
        category = market_category
        reasons.append("category_from_request_context")
    else:
        category = category_from_response.strip().lower()
        if category != market_category:
            category_provenance = "conflict"
            reasons.append("category_mismatch")
        else:
            category_provenance = "response"
    return RequestIdentity(
        exchange=exchange,
        requested_symbol=requested,
        returned_symbol=returned,
        symbol_provenance=provenance,
        market_category=category,
        category_provenance=category_provenance,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def _observation_quality(identity: RequestIdentity, quantities: Sequence[QuantityFact], metric_time: TimeFact) -> tuple[str, tuple[str, ...]]:
    reasons = list(identity.reason_codes)
    if "symbol_conflict" in reasons or "category_mismatch" in reasons:
        return QUALITY_CONFLICT, tuple(dict.fromkeys(reasons))
    for quantity in quantities:
        reasons.extend(quantity.reason_codes)
        if quantity.quality == QUALITY_CONFLICT:
            return QUALITY_CONFLICT, tuple(dict.fromkeys(reasons))
    if metric_time.quality != QUALITY_VERIFIED:
        reasons.extend(metric_time.reason_codes)
    primary = next((item for item in quantities if item.unit != UNIT_QUOTE_NOTIONAL), None)
    if primary is None or primary.quality != QUALITY_VERIFIED:
        return QUALITY_UNVERIFIED, tuple(dict.fromkeys(reasons))
    if metric_time.quality != QUALITY_VERIFIED:
        return QUALITY_UNVERIFIED, tuple(dict.fromkeys(reasons))
    return QUALITY_VERIFIED, tuple(dict.fromkeys(reasons))


def _batch(
    *,
    metric: str,
    endpoint: str,
    params: tuple[tuple[str, str], ...],
    observations: tuple[DerivativesObservation, ...],
    acquisition: AcquisitionFact,
    response_time: TimeFact,
    extra_reasons: tuple[str, ...] = (),
) -> DerivativesEvidenceBatch:
    reasons = list(extra_reasons) + list(acquisition.reason_codes) + list(response_time.reason_codes)
    quality = QUALITY_VERIFIED if observations and acquisition.quality == QUALITY_VERIFIED else QUALITY_UNVERIFIED
    if not observations:
        quality = QUALITY_MISSING
        reasons.append("missing_observations")
    if any(item.semantic_quality == QUALITY_CONFLICT for item in observations):
        quality = QUALITY_CONFLICT
    elif any(item.semantic_quality != QUALITY_VERIFIED for item in observations):
        quality = QUALITY_UNVERIFIED if observations else QUALITY_MISSING
    return DerivativesEvidenceBatch(
        metric=metric,
        endpoint=endpoint,
        request_params=params,
        observations=observations,
        acquisition=acquisition,
        response_time=response_time,
        cache=direct_cache_fact(acquisition.acquired_at),
        semantic_quality=quality,
        reason_codes=tuple(dict.fromkeys(reasons)),
    )


def unavailable_batch(
    *,
    metric: str,
    endpoint: str,
    reason: str,
    exchange: str = NA,
    requested_symbol: str = NA,
) -> DerivativesEvidenceBatch:
    acquisition = acquisition_fact(None, quality=QUALITY_MISSING, reason_codes=("missing_acquisition_timestamp", reason))
    response = missing_time("response", "exchange_response", "missing_exchange_response_time")
    return DerivativesEvidenceBatch(
        metric=metric,
        endpoint=endpoint,
        request_params=(),
        observations=(),
        acquisition=acquisition,
        response_time=response,
        cache=direct_cache_fact(None),
        semantic_quality=QUALITY_MISSING,
        reason_codes=(reason, "missing_acquisition_timestamp"),
    )


def capture_acquisition(capture_clock: Callable[[], datetime] | None) -> AcquisitionFact:
    """Stamp acquisition only for a caller that has a successful response in hand.

    A failing clock does not raise and does not fall through to another clock.
    Cancellation and process-level interruptions are not evidence failures.
    """

    try:
        return acquisition_fact(sample_capture_clock(capture_clock), quality=QUALITY_VERIFIED)
    except Exception:
        return acquisition_fact(
            None,
            quality=QUALITY_MISSING,
            reason_codes=("missing_acquisition_timestamp", "acquisition_capture_failed"),
        )


def _resolve_acquisition(
    capture_clock: Callable[[], datetime] | None,
    acquisition: AcquisitionFact | None,
) -> AcquisitionFact:
    """Pure description never samples a clock, even when the caller supplied one."""

    del capture_clock
    if acquisition is not None:
        return acquisition
    return acquisition_fact(
        None,
        quality=QUALITY_MISSING,
        reason_codes=("missing_acquisition_timestamp", "description_without_acquisition_fact"),
    )


def describe_binance_open_interest(
    *,
    requested_symbol: str,
    payload: Any,
    request_params: Mapping[str, Any],
    capture_clock: Callable[[], datetime] | None,
    acquisition: AcquisitionFact | None = None,
) -> DerivativesEvidenceBatch:
    acquired = _resolve_acquisition(capture_clock, acquisition)
    params = freeze_params(request_params)
    data = payload if isinstance(payload, Mapping) else {}
    returned = data.get("symbol") if isinstance(data.get("symbol"), str) else None
    identity = _identity(
        exchange="binance_futures",
        requested_symbol=requested_symbol,
        returned_symbol=returned,
        market_category="usds_margined",
        category_from_response=None,
    )
    quantity = parse_quantity(
        data.get("openInterest"),
        raw_field="openInterest",
        unit=UNIT_UNKNOWN,
        counting_convention=COUNT_OPEN_CONTRACT,
    )
    metric_time = _row_metric_time(data, ("time",), ("timestamp",), "open_interest_snapshot_time")
    response_time = missing_time("response", "exchange_response", "missing_exchange_response_time")
    semantic, reasons = _observation_quality(identity, (quantity,), metric_time)
    observation = DerivativesObservation(
        metric="open_interest",
        exchange="binance_futures",
        endpoint=ENDPOINT_BINANCE_OPEN_INTEREST,
        request_params=params,
        identity=identity,
        quantities=(quantity,),
        metric_time=metric_time,
        response_time=response_time,
        acquisition=acquired,
        series_period=NA,
        rate_kind=RATE_OPEN_INTEREST,
        semantic_quality=semantic,
        reason_codes=reasons,
        raw_retained=True,
    )
    return _batch(
        metric="open_interest",
        endpoint=ENDPOINT_BINANCE_OPEN_INTEREST,
        params=params,
        observations=(observation,),
        acquisition=acquired,
        response_time=response_time,
    )


def describe_binance_open_interest_history(
    *,
    requested_symbol: str,
    payload: Any,
    request_params: Mapping[str, Any],
    capture_clock: Callable[[], datetime] | None,
    acquisition: AcquisitionFact | None = None,
) -> DerivativesEvidenceBatch:
    acquired = _resolve_acquisition(capture_clock, acquisition)
    params = freeze_params(request_params)
    period = str(request_params.get("period") or NA)
    response_time = missing_time("response", "exchange_response", "missing_exchange_response_time")
    rows = payload if isinstance(payload, list) else []
    observations: list[DerivativesObservation] = []
    for raw in rows:
        data = raw if isinstance(raw, Mapping) else {}
        returned = data.get("symbol") if isinstance(data.get("symbol"), str) else None
        identity = _identity(
            exchange="binance_futures",
            requested_symbol=requested_symbol,
            returned_symbol=returned,
            market_category="usds_margined",
            category_from_response=None,
        )
        quantity = parse_quantity(
            data.get("sumOpenInterest"),
            raw_field="sumOpenInterest",
            unit=UNIT_UNKNOWN,
            counting_convention=COUNT_OPEN_CONTRACT,
        )
        notional = parse_quantity(
            data.get("sumOpenInterestValue"),
            raw_field="sumOpenInterestValue",
            unit=UNIT_QUOTE_NOTIONAL,
            counting_convention=COUNT_NOT_APPLICABLE,
        )
        metric_time = _row_metric_time(data, ("timestamp",), ("time",), "open_interest_period_time")
        semantic, reasons = _observation_quality(identity, (quantity, notional), metric_time)
        observations.append(
            DerivativesObservation(
                metric="open_interest",
                exchange="binance_futures",
                endpoint=ENDPOINT_BINANCE_OPEN_INTEREST_HIST,
                request_params=params,
                identity=identity,
                quantities=(quantity, notional),
                metric_time=metric_time,
                response_time=response_time,
                acquisition=acquired,
                series_period=period,
                rate_kind=RATE_OPEN_INTEREST,
                semantic_quality=semantic,
                reason_codes=reasons,
                raw_retained=True,
            )
        )
    return _batch(
        metric="open_interest_history",
        endpoint=ENDPOINT_BINANCE_OPEN_INTEREST_HIST,
        params=params,
        observations=tuple(observations),
        acquisition=acquired,
        response_time=response_time,
    )


def describe_binance_funding_history(
    *,
    requested_symbol: str,
    payload: Any,
    request_params: Mapping[str, Any],
    capture_clock: Callable[[], datetime] | None,
    acquisition: AcquisitionFact | None = None,
) -> DerivativesEvidenceBatch:
    return _funding_rows(
        exchange="binance_futures",
        endpoint=ENDPOINT_BINANCE_FUNDING,
        requested_symbol=requested_symbol,
        rows=payload if isinstance(payload, list) else [],
        request_params=request_params,
        capture_clock=capture_clock,
        rate_field="fundingRate",
        time_field="fundingTime",
        market_category="usds_margined",
        category_field=None,
        response_time_field=None,
        acquisition=acquisition,
    )


def describe_bybit_open_interest(
    *,
    requested_symbol: str,
    payload: Any,
    request_params: Mapping[str, Any],
    capture_clock: Callable[[], datetime] | None,
    acquisition: AcquisitionFact | None = None,
) -> DerivativesEvidenceBatch:
    acquired = _resolve_acquisition(capture_clock, acquisition)
    params = freeze_params(request_params)
    envelope = payload if isinstance(payload, Mapping) else {}
    result = envelope.get("result") if isinstance(envelope.get("result"), Mapping) else {}
    response_time = parse_time(envelope.get("time"), role="response", meaning="exchange_response_time")
    category = result.get("category") if isinstance(result.get("category"), str) else None
    result_symbol = result.get("symbol") if isinstance(result.get("symbol"), str) else None
    period = str(request_params.get("intervalTime") or NA)
    rows = result.get("list") if isinstance(result.get("list"), list) else []
    observations: list[DerivativesObservation] = []
    for raw in rows:
        data = raw if isinstance(raw, Mapping) else {}
        row_symbol = data.get("symbol") if isinstance(data.get("symbol"), str) else result_symbol
        identity = _identity(
            exchange="bybit_linear",
            requested_symbol=requested_symbol,
            returned_symbol=row_symbol,
            market_category="linear",
            category_from_response=category,
        )
        both = parse_quantity(
            data.get("openInterest"),
            raw_field="openInterest",
            unit=UNIT_UNKNOWN,
            counting_convention=COUNT_BOTH_SIDES,
        )
        quantities = [both]
        if "singleOpenInterest" in data:
            quantities.append(
                parse_quantity(
                    data.get("singleOpenInterest"),
                    raw_field="singleOpenInterest",
                    unit=UNIT_UNKNOWN,
                    counting_convention=COUNT_SINGLE_SIDE,
                )
            )
        metric_time = _row_metric_time(data, ("timestamp",), ("time",), "open_interest_sample_time")
        semantic, reasons = _observation_quality(identity, quantities, metric_time)
        observations.append(
            DerivativesObservation(
                metric="open_interest",
                exchange="bybit_linear",
                endpoint=ENDPOINT_BYBIT_OPEN_INTEREST,
                request_params=params,
                identity=identity,
                quantities=tuple(quantities),
                metric_time=metric_time,
                response_time=response_time,
                acquisition=acquired,
                series_period=period,
                rate_kind=RATE_OPEN_INTEREST,
                semantic_quality=semantic,
                reason_codes=reasons,
                raw_retained=True,
            )
        )
    return _batch(
        metric="open_interest",
        endpoint=ENDPOINT_BYBIT_OPEN_INTEREST,
        params=params,
        observations=tuple(observations),
        acquisition=acquired,
        response_time=response_time,
    )


def describe_bybit_funding_history(
    *,
    requested_symbol: str,
    payload: Any,
    request_params: Mapping[str, Any],
    capture_clock: Callable[[], datetime] | None,
    acquisition: AcquisitionFact | None = None,
) -> DerivativesEvidenceBatch:
    envelope = payload if isinstance(payload, Mapping) else {}
    result = envelope.get("result") if isinstance(envelope.get("result"), Mapping) else {}
    rows = result.get("list") if isinstance(result.get("list"), list) else []
    return _funding_rows(
        exchange="bybit_linear",
        endpoint=ENDPOINT_BYBIT_FUNDING,
        requested_symbol=requested_symbol,
        rows=rows,
        request_params=request_params,
        capture_clock=capture_clock,
        rate_field="fundingRate",
        time_field="fundingRateTimestamp",
        market_category="linear",
        category_field=result.get("category") if isinstance(result.get("category"), str) else None,
        response_time_field=envelope.get("time"),
        acquisition=acquisition,
    )


def describe_binance_account_ratio(
    *,
    requested_symbol: str,
    payload: Any,
    request_params: Mapping[str, Any],
    capture_clock: Callable[[], datetime] | None,
    acquisition: AcquisitionFact | None = None,
) -> DerivativesEvidenceBatch:
    rows = payload if isinstance(payload, list) else []
    return _account_ratio_rows(
        exchange="binance_futures",
        endpoint=ENDPOINT_BINANCE_ACCOUNT_RATIO,
        requested_symbol=requested_symbol,
        rows=rows,
        request_params=request_params,
        capture_clock=capture_clock,
        market_category="usds_margined",
        category=None,
        response_time_raw=None,
        ratio_field="longShortRatio",
        long_field="longAccount",
        short_field="shortAccount",
        acquisition=acquisition,
    )


def describe_bybit_account_ratio(
    *,
    requested_symbol: str,
    payload: Any,
    request_params: Mapping[str, Any],
    capture_clock: Callable[[], datetime] | None,
    acquisition: AcquisitionFact | None = None,
) -> DerivativesEvidenceBatch:
    envelope = payload if isinstance(payload, Mapping) else {}
    result = envelope.get("result") if isinstance(envelope.get("result"), Mapping) else {}
    rows = result.get("list") if isinstance(result.get("list"), list) else []
    return _account_ratio_rows(
        exchange="bybit_linear",
        endpoint=ENDPOINT_BYBIT_ACCOUNT_RATIO,
        requested_symbol=requested_symbol,
        rows=rows,
        request_params=request_params,
        capture_clock=capture_clock,
        market_category="linear",
        category=result.get("category") if isinstance(result.get("category"), str) else None,
        response_time_raw=envelope.get("time"),
        ratio_field=None,
        long_field="buyRatio",
        short_field="sellRatio",
        acquisition=acquisition,
    )


def _funding_rows(
    *,
    exchange: str,
    endpoint: str,
    requested_symbol: str,
    rows: Sequence[Any],
    request_params: Mapping[str, Any],
    capture_clock: Callable[[], datetime] | None,
    rate_field: str,
    time_field: str,
    market_category: str,
    category_field: str | None,
    response_time_field: Any,
    acquisition: AcquisitionFact | None = None,
) -> DerivativesEvidenceBatch:
    acquired = _resolve_acquisition(capture_clock, acquisition)
    params = freeze_params(request_params)
    response_time = (
        parse_time(response_time_field, role="response", meaning="exchange_response_time")
        if response_time_field is not None
        else missing_time("response", "exchange_response", "missing_exchange_response_time")
    )
    observations: list[DerivativesObservation] = []
    for raw in rows:
        data = raw if isinstance(raw, Mapping) else {}
        returned = data.get("symbol") if isinstance(data.get("symbol"), str) else None
        identity = _identity(
            exchange=exchange,
            requested_symbol=requested_symbol,
            returned_symbol=returned,
            market_category=market_category,
            category_from_response=category_field,
        )
        rate = parse_quantity(
            data.get(rate_field),
            raw_field=rate_field,
            unit=UNIT_FUNDING_FRACTION,
            counting_convention=COUNT_NOT_APPLICABLE,
            allow_negative=True,
        )
        rate = replace(rate, multiplier_applicability=MULTIPLIER_NOT_APPLICABLE)
        if rate.numeric_value is not None and abs(rate.numeric_value) >= 1:
            rate = replace(
                rate,
                quality=QUALITY_UNVERIFIED,
                reason_codes=tuple(dict.fromkeys((*rate.reason_codes, "unexpected_funding_magnitude"))),
            )
        metric_time = _row_metric_time(data, (time_field,), ("fundingTime", "fundingRateTimestamp"), "funding_settlement_time")
        subtype = _supplied_funding_subtype(data)
        semantic, reasons = _observation_quality(identity, (rate,), metric_time)
        rate_kind, semantic, reasons = _apply_funding_subtype(subtype, semantic, reasons)
        observations.append(
            DerivativesObservation(
                metric="funding",
                exchange=exchange,
                endpoint=endpoint,
                request_params=params,
                identity=identity,
                quantities=(rate,),
                metric_time=metric_time,
                response_time=response_time,
                acquisition=acquired,
                series_period=NA,
                rate_kind=rate_kind,
                semantic_quality=semantic,
                reason_codes=reasons,
                raw_retained=True,
            )
        )
    return _batch(
        metric="funding",
        endpoint=endpoint,
        params=params,
        observations=tuple(observations),
        acquisition=acquired,
        response_time=response_time,
        extra_reasons=_funding_batch_reasons(observations),
    )


def _account_ratio_rows(
    *,
    exchange: str,
    endpoint: str,
    requested_symbol: str,
    rows: Sequence[Any],
    request_params: Mapping[str, Any],
    capture_clock: Callable[[], datetime] | None,
    market_category: str,
    category: str | None,
    response_time_raw: Any,
    ratio_field: str | None,
    long_field: str,
    short_field: str,
    acquisition: AcquisitionFact | None = None,
) -> DerivativesEvidenceBatch:
    acquired = _resolve_acquisition(capture_clock, acquisition)
    params = freeze_params(request_params)
    period = str(request_params.get("period") or NA)
    response_time = (
        parse_time(response_time_raw, role="response", meaning="exchange_response_time")
        if response_time_raw is not None
        else missing_time("response", "exchange_response", "missing_exchange_response_time")
    )
    observations: list[DerivativesObservation] = []
    for raw in rows:
        data = raw if isinstance(raw, Mapping) else {}
        returned = data.get("symbol") if isinstance(data.get("symbol"), str) else None
        identity = _identity(
            exchange=exchange,
            requested_symbol=requested_symbol,
            returned_symbol=returned,
            market_category=market_category,
            category_from_response=category,
        )
        long_part = parse_quantity(
            data.get(long_field),
            raw_field=long_field,
            unit=UNIT_ACCOUNT_RATIO,
            counting_convention=COUNT_NOT_APPLICABLE,
        )
        short_part = parse_quantity(
            data.get(short_field),
            raw_field=short_field,
            unit=UNIT_ACCOUNT_RATIO,
            counting_convention=COUNT_NOT_APPLICABLE,
        )
        ratio, ratio_reasons = _account_ratio_quantity(
            data.get(ratio_field) if ratio_field else None,
            ratio_field=ratio_field,
            long_part=long_part,
            short_part=short_part,
        )
        metric_time = _row_metric_time(data, ("timestamp",), ("time",), "account_ratio_period_time")
        reasons = tuple(
            dict.fromkeys(
                (
                    *identity.reason_codes,
                    *ratio.reason_codes,
                    *ratio_reasons,
                    *metric_time.reason_codes,
                    RATE_ACCOUNT_POPULATION,
                )
            )
        )
        if "symbol_conflict" in identity.reason_codes or "category_mismatch" in identity.reason_codes or ratio.quality == QUALITY_CONFLICT:
            semantic = QUALITY_CONFLICT
        elif ratio.quality != QUALITY_VERIFIED or metric_time.quality != QUALITY_VERIFIED:
            semantic = QUALITY_UNVERIFIED
        else:
            semantic = QUALITY_VERIFIED
        observations.append(
            DerivativesObservation(
                metric="account_ratio",
                exchange=exchange,
                endpoint=endpoint,
                request_params=params,
                identity=identity,
                quantities=(long_part, short_part, ratio),
                metric_time=metric_time,
                response_time=response_time,
                acquisition=acquired,
                series_period=period,
                rate_kind=RATE_ACCOUNT_POPULATION,
                semantic_quality=semantic,
                reason_codes=reasons,
                raw_retained=True,
            )
        )
    return _batch(
        metric="account_ratio",
        endpoint=endpoint,
        params=params,
        observations=tuple(observations),
        acquisition=acquired,
        response_time=response_time,
        extra_reasons=(RATE_ACCOUNT_POPULATION,),
    )


def _account_ratio_quantity(
    raw_ratio: Any,
    *,
    ratio_field: str | None,
    long_part: QuantityFact,
    short_part: QuantityFact,
) -> tuple[QuantityFact, tuple[str, ...]]:
    """Validate holder shares before trusting a supplied or derived quotient.

    Sum tolerance is one fractional unit in the last place of the coarser share.
    An integer-form string is exact as written and does not grant a tolerance of 1.
    A supplied quotient must overlap the half-unit interval implied by each
    component. That is source rounding, not a trading threshold.
    """

    reasons: list[str] = []
    reasons.extend(_share_domain_reasons(long_part))
    reasons.extend(_share_domain_reasons(short_part))
    shares_present = long_part.numeric_value is not None and short_part.numeric_value is not None
    if shares_present and "invalid_account_ratio" not in reasons and not _shares_sum_consistent(long_part, short_part):
        reasons.append("account_share_inconsistent")
    short_value = short_part.numeric_value
    if short_value is None or short_value == 0 or long_part.numeric_value is None:
        reasons.append("invalid_denominator")
    explicit = ratio_field is not None and raw_ratio not in (None, "", NA)
    if explicit and "invalid_denominator" not in reasons and "invalid_account_ratio" not in reasons and "account_share_inconsistent" not in reasons:
        parsed = parse_quantity(
            raw_ratio,
            raw_field=ratio_field or "longShortRatio",
            unit=UNIT_ACCOUNT_RATIO,
            counting_convention=COUNT_NOT_APPLICABLE,
        )
        if parsed.numeric_value is None or parsed.numeric_value <= 0:
            reasons.append("invalid_account_ratio")
        else:
            quotient_reason = _quotient_reason(
                parsed.numeric_value,
                long_part,
                short_part,
                str(raw_ratio),
            )
            if quotient_reason:
                reasons.append(quotient_reason)
            elif not reasons:
                return replace(parsed, multiplier_applicability=MULTIPLIER_NOT_APPLICABLE), ()
    if not explicit and not reasons and short_value not in (None, 0) and long_part.numeric_value is not None:
        value = (long_part.numeric_value / short_value).quantize(OUTPUT_QUANT)
        return QuantityFact(
            raw_field="buyRatio/sellRatio",
            raw_value=format(value, "f"),
            numeric_value=value,
            unit=UNIT_ACCOUNT_RATIO,
            unit_asset=NA,
            counting_convention=COUNT_NOT_APPLICABLE,
            multiplier=NA,
            multiplier_applicability=MULTIPLIER_NOT_APPLICABLE,
            transformation="long_account_over_short_account",
            quality=QUALITY_VERIFIED,
            reason_codes=(),
        ), ()
    raw_text = NA if raw_ratio in (None, "") else str(raw_ratio)
    quality = QUALITY_CONFLICT if "ratio_component_conflict" in reasons else QUALITY_UNVERIFIED
    return QuantityFact(
        raw_field=ratio_field or "buyRatio/sellRatio",
        raw_value=raw_text,
        numeric_value=None,
        unit=UNIT_ACCOUNT_RATIO,
        unit_asset=NA,
        counting_convention=COUNT_NOT_APPLICABLE,
        multiplier=NA,
        multiplier_applicability=MULTIPLIER_NOT_APPLICABLE,
        transformation="none",
        quality=quality,
        reason_codes=tuple(dict.fromkeys(reasons or ("invalid_account_ratio",))),
    ), tuple(dict.fromkeys(reasons or ("invalid_account_ratio",)))


def _share_domain_reasons(part: QuantityFact) -> tuple[str, ...]:
    if "non_finite_quantity" in part.reason_codes:
        return ("non_finite_quantity", "invalid_account_ratio")
    if "malformed_quantity" in part.reason_codes:
        return ("malformed_quantity", "invalid_account_ratio")
    if part.quality == QUALITY_MISSING or part.numeric_value is None:
        return ()
    value = part.numeric_value
    if not value.is_finite() or value < 0 or value > 1:
        return ("invalid_account_ratio",)
    return ()


def _shares_sum_consistent(long_part: QuantityFact, short_part: QuantityFact) -> bool:
    if long_part.numeric_value is None or short_part.numeric_value is None:
        return False
    return _within_share_precision(
        long_part.numeric_value + short_part.numeric_value,
        Decimal(1),
        long_part.raw_value,
        short_part.raw_value,
    )


def _within_share_precision(left: Decimal, right: Decimal, *raw_values: str) -> bool:
    quantum = max(_stated_quantum(raw) for raw in raw_values)
    return abs(left - right) <= quantum


def _stated_quantum(raw: str) -> Decimal:
    """Fractional unit in the last place. Integer text is exact and contributes no slack."""

    try:
        value = Decimal(str(raw).strip())
    except (InvalidOperation, ValueError):
        return Decimal(0)
    if not value.is_finite():
        return Decimal(0)
    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int) or exponent >= 0:
        return Decimal(0)
    return Decimal(10) ** exponent


def _quotient_reason(
    reported: Decimal,
    long_part: QuantityFact,
    short_part: QuantityFact,
    ratio_raw: str,
) -> str:
    """Return a reason when component uncertainty cannot admit the reported quotient."""

    long_value = long_part.numeric_value
    short_value = short_part.numeric_value
    if long_value is None or short_value is None:
        return "invalid_denominator"
    low_short = short_value - (_stated_quantum(short_part.raw_value) / 2)
    high_short = short_value + (_stated_quantum(short_part.raw_value) / 2)
    low_long = long_value - (_stated_quantum(long_part.raw_value) / 2)
    high_long = long_value + (_stated_quantum(long_part.raw_value) / 2)
    if low_short <= 0 or low_long < 0 or high_short <= 0:
        return "account_share_precision_unjustified"
    lower = low_long / high_short
    upper = high_long / low_short
    half_ratio = _stated_quantum(ratio_raw) / 2
    if reported + half_ratio < lower or reported - half_ratio > upper:
        return "ratio_component_conflict"
    return ""


_FUNDING_ROW_IDENTITY_CONFLICTS = frozenset(
    {
        "symbol_conflict",
        "category_mismatch",
        "identity_conflict",
        "request_context_identity_conflict",
        "settlement_mismatch",
        "contract_mismatch",
        "unsupported_inverse",
    }
)


def _apply_funding_subtype(
    subtype: str | None,
    semantic: str,
    reasons: tuple[str, ...],
) -> tuple[str, str, tuple[str, ...]]:
    """Map a documented funding subtype without treating an unknown token as ordinary."""

    if subtype is None:
        return RATE_SETTLED_FUNDING, semantic, tuple(dict.fromkeys((*reasons, RATE_SETTLED_FUNDING)))
    token = subtype.casefold()
    if token == "regular":
        return (
            RATE_SETTLED_FUNDING,
            semantic,
            tuple(dict.fromkeys((*reasons, RATE_SETTLED_FUNDING, "regular_funding"))),
        )
    if token == "special":
        return subtype, QUALITY_UNVERIFIED, tuple(dict.fromkeys((*reasons, "unsupported_funding_subtype")))
    return subtype, QUALITY_UNVERIFIED, tuple(dict.fromkeys((*reasons, "unexpected_funding_subtype")))


def _funding_batch_reasons(observations: Sequence[DerivativesObservation]) -> tuple[str, ...]:
    if not observations:
        return ()
    local = [
        item
        for item in observations
        if not any(code in _FUNDING_ROW_IDENTITY_CONFLICTS for code in item.reason_codes)
    ]
    pool = local or list(observations)
    reasons: list[str] = []
    if all(item.rate_kind == RATE_SETTLED_FUNDING for item in pool):
        reasons.append(RATE_SETTLED_FUNDING)
    if any("unsupported_funding_subtype" in item.reason_codes for item in pool):
        reasons.append("unsupported_funding_subtype")
    if any("unexpected_funding_subtype" in item.reason_codes for item in pool):
        reasons.append("unexpected_funding_subtype")
    return tuple(reasons)


def _supplied_funding_subtype(data: Mapping[str, Any]) -> str | None:
    """Return a supplied rateType or fundingRateType token. Absence means ordinary history."""

    for key in ("rateType", "fundingRateType"):
        if key not in data:
            continue
        value = data.get(key)
        if value in (None, "", NA):
            continue
        return str(value).strip()[:64]
    return None


def _row_metric_time(
    data: Mapping[str, Any],
    primary: Sequence[str],
    alternates: Sequence[str],
    meaning: str,
) -> TimeFact:
    ambiguous = _ambiguous_metric_time(data, primary, alternates)
    if ambiguous is not None:
        return replace(ambiguous, meaning=meaning)
    field = primary[0] if primary else ""
    return parse_time(data.get(field), role="metric", meaning=meaning)


def _ambiguous_metric_time(data: Mapping[str, Any], primary: Sequence[str], alternates: Sequence[str]) -> TimeFact | None:
    present = [name for name in (*primary, *alternates) if name in data and data.get(name) not in (None, "", NA)]
    if len(present) < 2:
        return None
    parsed: list[datetime] = []
    for name in present:
        fact = parse_time(data.get(name), role="metric", meaning="ambiguous")
        if fact.at is None:
            return TimeFact("metric", "ambiguous", None, str(data.get(name)), QUALITY_UNVERIFIED, ("ambiguous_metric_timestamp",))
        parsed.append(fact.at)
    if len(set(parsed)) > 1:
        return TimeFact("metric", "ambiguous", None, NA, QUALITY_UNVERIFIED, ("ambiguous_metric_timestamp",))
    return None


def apply_cache_delivery(
    batch: DerivativesEvidenceBatch | None,
    *,
    delivery_kind: str,
    delivered_at: datetime | None,
    file_rehydrated: bool,
    legacy_value: Any,
    metric: str,
    endpoint: str,
) -> DerivativesEvidenceBatch:
    """Preserve original acquisition. A cache read stamps delivery time only."""

    if batch is None or file_rehydrated:
        reason = REASON_LEGACY_FILE_CACHE if file_rehydrated else REASON_UNINSTRUMENTED_CACHE
        unverified = _unverified_from_legacy(legacy_value, metric=metric, endpoint=endpoint, reason=reason)
        return replace(
            unverified,
            cache=CacheFact(
                delivery_kind=delivery_kind,
                delivered_at=delivered_at,
                original_acquired_at=None,
                file_rehydrated=file_rehydrated,
                reason_codes=(reason, "cache_delivery_is_not_acquisition"),
            ),
        )
    cache = CacheFact(
        delivery_kind=delivery_kind,
        delivered_at=delivered_at,
        original_acquired_at=batch.acquisition.acquired_at,
        file_rehydrated=False,
        reason_codes=("cache_delivery_is_not_acquisition",),
    )
    return replace(batch, cache=cache)


def _unverified_from_legacy(value: Any, *, metric: str, endpoint: str, reason: str) -> DerivativesEvidenceBatch:
    acquisition = acquisition_fact(None, quality=QUALITY_MISSING, reason_codes=("missing_acquisition_timestamp", reason))
    response = missing_time("response", "exchange_response", "missing_exchange_response_time")
    observations: list[DerivativesObservation] = []
    items: Sequence[Any]
    if isinstance(value, list | tuple):
        items = value
    elif value is None:
        items = ()
    else:
        items = (value,)
    for item in items:
        metric_time, quantity = _legacy_fact(item)
        identity = RequestIdentity(
            exchange=getattr(item, "exchange", NA) if not isinstance(item, (Decimal, str, int)) else NA,
            requested_symbol=getattr(item, "symbol", NA) if not isinstance(item, (Decimal, str, int)) else NA,
            returned_symbol=None,
            symbol_provenance="legacy_value",
            market_category=NA,
            category_provenance="missing",
            reason_codes=(reason,),
        )
        observations.append(
            DerivativesObservation(
                metric=metric,
                exchange=identity.exchange,
                endpoint=endpoint,
                request_params=(),
                identity=identity,
                quantities=(quantity,),
                metric_time=metric_time,
                response_time=response,
                acquisition=acquisition,
                series_period=NA,
                rate_kind=NA,
                semantic_quality=QUALITY_UNVERIFIED,
                reason_codes=(reason, "missing_acquisition_timestamp"),
                raw_retained=False,
            )
        )
    return DerivativesEvidenceBatch(
        metric=metric,
        endpoint=endpoint,
        request_params=(),
        observations=tuple(observations),
        acquisition=acquisition,
        response_time=response,
        cache=direct_cache_fact(None),
        semantic_quality=QUALITY_UNVERIFIED,
        reason_codes=(reason, "missing_acquisition_timestamp"),
    )


def _legacy_fact(item: Any) -> tuple[TimeFact, QuantityFact]:
    if isinstance(item, OpenInterestDTO):
        metric_time = parse_time(item.timestamp, role="metric", meaning="dto_timestamp_not_reacquired")
        quantity = parse_quantity(item.open_interest, raw_field="open_interest", unit=UNIT_UNKNOWN, counting_convention=COUNT_UNKNOWN)
        return metric_time, quantity
    if isinstance(item, FundingDTO):
        metric_time = parse_time(item.timestamp, role="metric", meaning="dto_timestamp_not_reacquired")
        quantity = parse_quantity(
            item.funding_rate,
            raw_field="funding_rate",
            unit=UNIT_FUNDING_FRACTION,
            counting_convention=COUNT_NOT_APPLICABLE,
            allow_negative=True,
        )
        return metric_time, quantity
    if isinstance(item, Decimal):
        return missing_time("metric", "missing", "missing_metric_timestamp"), parse_quantity(
            item,
            raw_field="legacy_decimal",
            unit=UNIT_UNKNOWN,
            counting_convention=COUNT_UNKNOWN,
            allow_negative=True,
        )
    return missing_time("metric", "missing", "missing_metric_timestamp"), parse_quantity(
        None,
        raw_field="legacy",
        unit=UNIT_UNKNOWN,
        counting_convention=COUNT_UNKNOWN,
    )


def represent_delivery(value: Any, evidence: DerivativesEvidenceBatch) -> MetricEvidenceDelivery:
    return MetricEvidenceDelivery(value=value, evidence=evidence)


__all__ = [
    "ACQUISITION_MEANING",
    "COUNT_BOTH_SIDES",
    "COUNT_OPEN_CONTRACT",
    "COUNT_SINGLE_SIDE",
    "ENDPOINT_BINANCE_ACCOUNT_RATIO",
    "ENDPOINT_BINANCE_EXCHANGE_INFO",
    "ENDPOINT_BINANCE_FUNDING",
    "ENDPOINT_BINANCE_OPEN_INTEREST",
    "ENDPOINT_BINANCE_OPEN_INTEREST_HIST",
    "ENDPOINT_BYBIT_ACCOUNT_RATIO",
    "ENDPOINT_BYBIT_FUNDING",
    "ENDPOINT_BYBIT_INSTRUMENT",
    "ENDPOINT_BYBIT_OPEN_INTEREST",
    "QUALITY_CONFLICT",
    "QUALITY_MISSING",
    "QUALITY_UNVERIFIED",
    "QUALITY_VERIFIED",
    "RATE_SETTLED_FUNDING",
    "REASON_ADAPTER_WITHOUT_EVIDENCE",
    "REASON_FAST_MODE_SKIPPED",
    "REASON_LEGACY_FILE_CACHE",
    "UNIT_BASE_QUANTITY",
    "UNIT_FUNDING_FRACTION",
    "UNIT_QUOTE_NOTIONAL",
    "UNIT_UNKNOWN",
    "AcquisitionFact",
    "DerivativesEvidenceBatch",
    "DerivativesObservation",
    "MetricEvidenceDelivery",
    "QuantityFact",
    "apply_cache_delivery",
    "describe_binance_account_ratio",
    "describe_binance_funding_history",
    "describe_binance_open_interest",
    "describe_binance_open_interest_history",
    "describe_bybit_account_ratio",
    "describe_bybit_funding_history",
    "describe_bybit_open_interest",
    "parse_quantity",
    "represent_delivery",
    "sample_capture_clock",
    "unavailable_batch",
]
