"""Deterministic shadow admissibility for derivatives source evidence.

The evaluator cannot emit a validated derivatives confirmation. Production
eligibility, scores, and rejection stay on the legacy numeric path.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
import json

from app.analytics.derivatives_enrichment import DerivativesEnrichmentEngine
from app.data.candle_integrity import normalize_utc_timestamp
from app.data.derivatives_instrument import InstrumentMetadata, bind_instrument_batch
from app.data.derivatives_source_evidence import (
    COUNT_BOTH_SIDES,
    COUNT_OPEN_CONTRACT,
    COUNT_SINGLE_SIDE,
    ENDPOINT_BINANCE_OPEN_INTEREST,
    ENDPOINT_BINANCE_OPEN_INTEREST_HIST,
    QUALITY_CONFLICT,
    QUALITY_UNVERIFIED,
    QUALITY_VERIFIED,
    RATE_SETTLED_FUNDING,
    UNIT_BASE_QUANTITY,
    UNIT_FUNDING_FRACTION,
    UNIT_QUOTE_NOTIONAL,
    DerivativesEvidenceBatch,
    DerivativesObservation,
    QuantityFact,
)
from app.data.dtos import NA

OUTPUT_QUANT = Decimal("0.00000001")
SHADOW_FRESHNESS_POLICY = "cadence_or_verified_period_v1"
ENFORCEMENT = "not_active"

_PERIOD_MS = {
    "5m": 300_000,
    "5min": 300_000,
    "15m": 900_000,
    "15min": 900_000,
    "30m": 1_800_000,
    "30min": 1_800_000,
    "1h": 3_600_000,
    "60": 3_600_000,
    "2h": 7_200_000,
    "4h": 14_400_000,
    "6h": 21_600_000,
    "12h": 43_200_000,
    "1d": 86_400_000,
}
_BINANCE_QUANTITY_FIELDS = frozenset(
    {
        (ENDPOINT_BINANCE_OPEN_INTEREST, "openInterest"),
        (ENDPOINT_BINANCE_OPEN_INTEREST_HIST, "sumOpenInterest"),
    }
)
_PRIMARY_COUNTS = frozenset({COUNT_OPEN_CONTRACT, COUNT_BOTH_SIDES})
_SUPPORT_STATES = frozenset({NA, "unverified", "false", "eligible_no_conflict_detected"})
_LINEAGE_LIMIT = 32
_IDENTITY_FAILURES = frozenset(
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


@dataclass(frozen=True)
class SourceLineageFact:
    role: str
    exchange: str
    endpoint: str
    requested_symbol: str
    returned_symbol: str | None
    market_category: str
    contract_type: str | None
    settlement_asset: str | None
    base_asset: str | None
    raw_field: str
    raw_value: str
    unit: str
    counting_convention: str
    transformation: str
    metric_time: str | None
    response_time: str | None
    acquired_at: str | None
    cache_delivered_at: str | None
    reason_codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "exchange": self.exchange,
            "endpoint": self.endpoint,
            "requested_symbol": self.requested_symbol,
            "returned_symbol": self.returned_symbol,
            "market_category": self.market_category,
            "contract_type": self.contract_type,
            "settlement_asset": self.settlement_asset,
            "base_asset": self.base_asset,
            "raw_field": self.raw_field,
            "raw_value": self.raw_value,
            "unit": self.unit,
            "counting_convention": self.counting_convention,
            "transformation": self.transformation,
            "metric_time": self.metric_time,
            "response_time": self.response_time,
            "acquired_at": self.acquired_at,
            "cache_delivered_at": self.cache_delivered_at,
            "reason_codes": list(self.reason_codes),
        }


@dataclass(frozen=True)
class DerivativesSourceTruthResult:
    mode: str = "shadow"
    enforcement: str = ENFORCEMENT
    validated_confirmation: bool = False
    classification: str = "not_available"
    decision_cutoff: str = NA
    oi_change_pct: str = NA
    oi_comparison_interval_ms: int | None = None
    oi_interval_label: str = NA
    oi_change_context: str = "not_admissible"
    funding_rate: str = NA
    funding_label: str = NA
    funding_percent: str = NA
    funding_basis_points: str = NA
    funding_cadence_ms: int | None = None
    account_ratio: str = NA
    supports_long: str = NA
    supports_short: str = NA
    reason_codes: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    evaluation_error: str | None = None
    liquidation_coverage: str = "not_collected"
    freshness_policy: str = SHADOW_FRESHNESS_POLICY
    lineage: tuple[SourceLineageFact, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", "shadow")
        object.__setattr__(self, "enforcement", ENFORCEMENT)
        object.__setattr__(self, "validated_confirmation", False)
        if self.supports_long not in _SUPPORT_STATES:
            object.__setattr__(self, "supports_long", "unverified")
        if self.supports_short not in _SUPPORT_STATES:
            object.__setattr__(self, "supports_short", "unverified")
        if len(self.lineage) > _LINEAGE_LIMIT:
            object.__setattr__(self, "lineage", self.lineage[:_LINEAGE_LIMIT])

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "enforcement": self.enforcement,
            "validated_confirmation": self.validated_confirmation,
            "classification": self.classification,
            "decision_cutoff": self.decision_cutoff,
            "oi_change_pct": self.oi_change_pct,
            "oi_comparison_interval_ms": self.oi_comparison_interval_ms,
            "oi_interval_label": self.oi_interval_label,
            "oi_change_context": self.oi_change_context,
            "funding_rate": self.funding_rate,
            "funding_label": self.funding_label,
            "funding_percent": self.funding_percent,
            "funding_basis_points": self.funding_basis_points,
            "funding_cadence_ms": self.funding_cadence_ms,
            "account_ratio": self.account_ratio,
            "supports_long": self.supports_long,
            "supports_short": self.supports_short,
            "reason_codes": list(self.reason_codes),
            "dependencies": list(self.dependencies),
            "exclusions": list(self.exclusions),
            "evaluation_error": self.evaluation_error,
            "liquidation_coverage": self.liquidation_coverage,
            "freshness_policy": self.freshness_policy,
            "lineage": [item.to_dict() for item in self.lineage],
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> DerivativesSourceTruthResult:
        """Supported deserialization. Shadow constants cannot be restored as live."""

        data = dict(payload)
        lineage_payload = data.get("lineage") or ()
        facts: list[SourceLineageFact] = []
        if isinstance(lineage_payload, Sequence) and not isinstance(lineage_payload, (str, bytes)):
            for item in lineage_payload:
                if isinstance(item, SourceLineageFact):
                    facts.append(item)
                elif isinstance(item, Mapping):
                    facts.append(
                        SourceLineageFact(
                            role=str(item.get("role") or "input"),
                            exchange=str(item.get("exchange") or NA),
                            endpoint=str(item.get("endpoint") or NA),
                            requested_symbol=str(item.get("requested_symbol") or NA),
                            returned_symbol=item.get("returned_symbol") if item.get("returned_symbol") is None else str(item.get("returned_symbol")),
                            market_category=str(item.get("market_category") or NA),
                            contract_type=item.get("contract_type"),
                            settlement_asset=item.get("settlement_asset"),
                            base_asset=item.get("base_asset"),
                            raw_field=str(item.get("raw_field") or NA),
                            raw_value=str(item.get("raw_value") or NA),
                            unit=str(item.get("unit") or NA),
                            counting_convention=str(item.get("counting_convention") or NA),
                            transformation=str(item.get("transformation") or NA),
                            metric_time=item.get("metric_time"),
                            response_time=item.get("response_time"),
                            acquired_at=item.get("acquired_at"),
                            cache_delivered_at=item.get("cache_delivered_at"),
                            reason_codes=tuple(str(code) for code in item.get("reason_codes") or ()),
                        )
                    )
        allowed = {
            "classification",
            "decision_cutoff",
            "oi_change_pct",
            "oi_comparison_interval_ms",
            "oi_interval_label",
            "oi_change_context",
            "funding_rate",
            "funding_label",
            "funding_percent",
            "funding_basis_points",
            "funding_cadence_ms",
            "account_ratio",
            "supports_long",
            "supports_short",
            "reason_codes",
            "dependencies",
            "exclusions",
            "evaluation_error",
            "liquidation_coverage",
            "freshness_policy",
        }
        kwargs: dict[str, Any] = {key: data[key] for key in allowed if key in data}
        for key in ("reason_codes", "dependencies", "exclusions"):
            if key in kwargs and not isinstance(kwargs[key], tuple):
                kwargs[key] = tuple(kwargs[key])
        kwargs["lineage"] = tuple(facts)
        return cls(**kwargs)


@dataclass(frozen=True)
class _OiPoint:
    observation: DerivativesObservation
    quantity: QuantityFact
    cache_reasons: tuple[str, ...] = ()


@dataclass(frozen=True)
class OpenInterestPairSelection:
    change_pct: Decimal | None
    interval_ms: int | None
    interval_label: str
    context: str
    reason_codes: tuple[str, ...]
    exclusions: tuple[str, ...]
    current_quantity: Decimal | None = None
    previous_quantity: Decimal | None = None


def native_position_change_pct(current: Decimal, previous: Decimal) -> tuple[Decimal | None, tuple[str, ...]]:
    """Percent change of native quantity. Price and notional are not inputs."""

    if isinstance(current, bool) or isinstance(previous, bool):
        return None, ("malformed_quantity",)
    if not isinstance(current, Decimal) or not isinstance(previous, Decimal):
        return None, ("malformed_quantity",)
    if not current.is_finite() or not previous.is_finite():
        return None, ("non_finite_quantity",)
    if current < 0 or previous < 0:
        return None, ("negative_open_interest",)
    if previous == 0:
        return None, ("zero_baseline",)
    change = (Decimal(100) * (current - previous) / previous).quantize(OUTPUT_QUANT)
    return change, ()


def quote_notional(quantity: Decimal, mark_price: Decimal) -> Decimal:
    return (quantity * mark_price).quantize(OUTPUT_QUANT)


def liquidation_coverage_label(snapshot: Any) -> str:
    """Label existing coverage. An empty covered window is not exchange-wide zero."""

    if snapshot is None:
        return "not_collected"
    status = getattr(snapshot, "status", None)
    status_value = str(getattr(status, "value", status) or "")
    reason = str(getattr(snapshot, "reason", "") or "")
    if status_value.upper() == "UNAVAILABLE" or reason in {"stream_disconnected", "no_valid_events"}:
        return "unavailable_not_zero"
    windows = getattr(snapshot, "windows", None) or {}
    for window in windows.values():
        if getattr(window, "coverage_complete", False) and getattr(window, "event_count", None) == 0:
            return "covered_empty_feed_window_not_exchange_wide_zero"
    usage = getattr(snapshot, "usage", None)
    if usage == "research_only":
        return "research_only_not_a_signal"
    return "not_collected"


def write_dev_shadow_artifact(path: Path | str, result: DerivativesSourceTruthResult) -> None:
    """Write an explicit DEV review artifact. The scanner and Runtime must not call this."""

    target = Path(path)
    if target.suffix.lower() != ".json":
        raise ValueError("DEV shadow artifacts must be explicit JSON files")
    target.write_text(json.dumps(result.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def evaluate_derivatives_source_truth(
    *,
    decision_cutoff: datetime | None,
    instrument: InstrumentMetadata | None = None,
    open_interest: DerivativesEvidenceBatch | None = None,
    open_interest_history: DerivativesEvidenceBatch | None = None,
    funding: DerivativesEvidenceBatch | None = None,
    funding_history: DerivativesEvidenceBatch | None = None,
    account_ratio: DerivativesEvidenceBatch | None = None,
    price_change_pct: Decimal | None = None,
    price_quality: str = "missing",
    liquidation_snapshot: Any = None,
) -> DerivativesSourceTruthResult:
    try:
        visible_instrument = _instrument_visible_at_cutoff(instrument, decision_cutoff)
        return _evaluate(
            decision_cutoff=decision_cutoff,
            instrument=visible_instrument,
            open_interest=bind_instrument_batch(open_interest, visible_instrument),
            open_interest_history=bind_instrument_batch(open_interest_history, visible_instrument),
            funding=bind_instrument_batch(funding, visible_instrument),
            funding_history=bind_instrument_batch(funding_history, visible_instrument),
            account_ratio=bind_instrument_batch(account_ratio, visible_instrument),
            price_change_pct=price_change_pct,
            price_quality=price_quality,
            liquidation_snapshot=liquidation_snapshot,
        )
    except Exception as exc:
        return _error_result(
            type(exc).__name__,
            liquidation_coverage_label(liquidation_snapshot),
            lineage=_safe_input_lineage(
                instrument=None,
                batches=(
                    ("input_open_interest", open_interest),
                    ("input_open_interest_history", open_interest_history),
                    ("input_funding", funding),
                    ("input_funding_history", funding_history),
                    ("input_account_ratio", account_ratio),
                ),
            ),
        )


def select_open_interest_pair(
    points: Sequence[_OiPoint],
    *,
    cutoff: datetime | None,
    period_ms: int | None,
) -> OpenInterestPairSelection:
    exclusions: list[str] = []
    timed: list[_OiPoint] = []
    for point in points:
        if point.observation.metric_time.at is None or point.observation.metric_time.quality != QUALITY_VERIFIED:
            exclusions.append(_exclusion(point, "missing_or_unverified_metric_timestamp"))
            continue
        timed.append(point)
    if not timed:
        return OpenInterestPairSelection(
            None, None, NA, "not_admissible", ("missing_open_interest",), tuple(exclusions)
        )
    groups: dict[datetime, list[_OiPoint]] = {}
    for point in timed:
        moment = point.observation.metric_time.at
        if moment is not None:
            groups.setdefault(moment, []).append(point)
    newest_at = max(groups)
    current, collapse_reasons, collapse_exclusions = _collapse_timestamp_group(
        _chosen_series_points(groups[newest_at])
    )
    exclusions.extend(collapse_exclusions)
    if current is None:
        return OpenInterestPairSelection(
            None,
            None,
            NA,
            "not_admissible",
            collapse_reasons,
            tuple(dict.fromkeys(exclusions)),
        )
    current_reasons = _point_block_reasons(current, cutoff)
    earlier = [
        point
        for moment, grouped in groups.items()
        if moment < newest_at
        for point in grouped
        if _series_compatible(current, point)
    ]
    earlier.sort(key=lambda point: point.observation.metric_time.at or datetime.min.replace(tzinfo=newest_at.tzinfo), reverse=True)
    if not earlier:
        prior = [
            point
            for point in timed
            if point.observation.metric_time.at is not None and point.observation.metric_time.at < newest_at
        ]
        reasons = ["missing_previous_open_interest", *current_reasons]
        cross_instrument = [
            point
            for point in prior
            if point.observation.exchange != current.observation.exchange
            or point.observation.identity.requested_symbol != current.observation.identity.requested_symbol
        ]
        same_instrument = [point for point in prior if point not in cross_instrument]
        if cross_instrument:
            reasons.append("cross_instrument_not_paired")
        if same_instrument:
            reasons.append("endpoint_incompatible")
        return OpenInterestPairSelection(
            None,
            None,
            NA,
            "not_admissible",
            tuple(reasons),
            tuple(dict.fromkeys(exclusions)),
            current_quantity=current.quantity.numeric_value,
        )
    previous_at = earlier[0].observation.metric_time.at
    previous_group = [point for point in earlier if point.observation.metric_time.at == previous_at]
    previous, previous_collapse_reasons, previous_exclusions = _collapse_timestamp_group(previous_group)
    exclusions.extend(previous_exclusions)
    if previous is None:
        return OpenInterestPairSelection(
            None,
            None,
            NA,
            "not_admissible",
            tuple(dict.fromkeys((*previous_collapse_reasons, *current_reasons))),
            tuple(dict.fromkeys(exclusions)),
            current_quantity=current.quantity.numeric_value,
        )
    if current_reasons:
        exclusions.append(_exclusion(current, ",".join(current_reasons)))
        return OpenInterestPairSelection(
            None,
            None,
            NA,
            "not_admissible",
            current_reasons,
            tuple(dict.fromkeys(exclusions)),
            current_quantity=current.quantity.numeric_value,
        )
    previous_reasons = _point_block_reasons(previous, cutoff)
    if previous_reasons:
        exclusions.append(_exclusion(previous, ",".join(previous_reasons)))
        return OpenInterestPairSelection(
            None,
            None,
            NA,
            "not_admissible",
            previous_reasons,
            tuple(dict.fromkeys(exclusions)),
            current_quantity=current.quantity.numeric_value,
        )
    assert current.observation.metric_time.at is not None
    assert previous.observation.metric_time.at is not None
    assert current.quantity.numeric_value is not None
    assert previous.quantity.numeric_value is not None
    interval_ms = int((current.observation.metric_time.at - previous.observation.metric_time.at).total_seconds() * 1000)
    if interval_ms <= 0:
        return OpenInterestPairSelection(
            None, None, NA, "not_admissible", ("non_positive_comparison_interval",), tuple(exclusions)
        )
    change, change_reasons = native_position_change_pct(current.quantity.numeric_value, previous.quantity.numeric_value)
    if change_reasons:
        return OpenInterestPairSelection(
            None,
            interval_ms,
            f"{interval_ms}ms",
            "not_admissible",
            change_reasons,
            tuple(exclusions),
            current.quantity.numeric_value,
            previous.quantity.numeric_value,
        )
    context, freshness_reasons = _freshness(current.observation.metric_time.at, cutoff, period_ms)
    return OpenInterestPairSelection(
        change,
        interval_ms,
        f"{interval_ms}ms",
        context,
        freshness_reasons,
        tuple(dict.fromkeys(exclusions)),
        current.quantity.numeric_value,
        previous.quantity.numeric_value,
    )


def _evaluate(
    *,
    decision_cutoff: datetime | None,
    instrument: InstrumentMetadata | None,
    open_interest: DerivativesEvidenceBatch | None,
    open_interest_history: DerivativesEvidenceBatch | None,
    funding: DerivativesEvidenceBatch | None,
    funding_history: DerivativesEvidenceBatch | None,
    account_ratio: DerivativesEvidenceBatch | None,
    price_change_pct: Decimal | None,
    price_quality: str,
    liquidation_snapshot: Any,
) -> DerivativesSourceTruthResult:
    reasons: list[str] = []
    exclusions: list[str] = []
    for batch in (open_interest, open_interest_history, funding, funding_history, account_ratio):
        if batch is not None:
            reasons.extend(batch.reason_codes)
    cutoff = _coerce_cutoff(decision_cutoff)
    if cutoff is None:
        reasons.append("missing_decision_cutoff")
    oi_points = _oi_points(open_interest) + _oi_points(open_interest_history)
    quantity_points = [point for point in oi_points if point.quantity.unit != UNIT_QUOTE_NOTIONAL]
    has_notional = any(
        quantity.unit == UNIT_QUOTE_NOTIONAL and quantity.numeric_value is not None
        for batch in (open_interest, open_interest_history)
        if batch is not None
        for observation in batch.observations
        for quantity in observation.quantities
    )
    usable_native = any(point.quantity.numeric_value is not None for point in quantity_points)
    if has_notional and not usable_native:
        reasons.append("notional_not_position_growth")
    if any(_single_side_only(batch) for batch in (open_interest, open_interest_history)):
        reasons.append("single_side_not_substituted")
    period_ms = _series_period_ms(open_interest_history) or _series_period_ms(open_interest)
    oi_pair = select_open_interest_pair(quantity_points, cutoff=cutoff, period_ms=period_ms)
    reasons.extend(oi_pair.reason_codes)
    exclusions.extend(oi_pair.exclusions)
    funding_obs, funding_reasons = _select_funding(funding, funding_history, cutoff, instrument)
    reasons.extend(funding_reasons)
    ratio_obs, ratio_reasons = _select_ratio(account_ratio, cutoff)
    reasons.extend(ratio_reasons)
    funding_rate = funding_obs.quantities[0].numeric_value if funding_obs and funding_obs.quantities else None
    funding_texts = (NA, NA, NA)
    funding_label = NA
    if funding_obs is not None and funding_rate is not None and not funding_reasons:
        funding_label = RATE_SETTLED_FUNDING
        fraction, percent, bps, magnitude_reasons = _funding_texts(funding_rate)
        funding_texts = (fraction, percent, bps)
        reasons.extend(magnitude_reasons)
        if magnitude_reasons:
            funding_label = "unverified"
            funding_rate = None
    elif funding_obs is not None:
        funding_label = "unverified"
    cadence = instrument.funding_interval_ms if instrument is not None else None
    if funding_obs is not None and cadence is None:
        reasons.append("funding_cadence_unknown")
    funding_fresh = False
    if funding_obs is not None and funding_rate is not None and not _blocking(tuple(funding_reasons)):
        fresh_context, fresh_reasons = _freshness(
            funding_obs.metric_time.at,
            cutoff,
            cadence,
        )
        reasons.extend(fresh_reasons)
        funding_fresh = fresh_context == "current"
        if cadence is None:
            funding_fresh = False
    price_only = price_quality == "verified" and funding_obs is None and not quantity_points
    if price_only:
        reasons.append("price_only_not_derivatives_support")
    oi_ready = (
        oi_pair.change_pct is not None
        and oi_pair.context == "current"
        and not _blocking(oi_pair.reason_codes)
    )
    funding_ready = funding_fresh and funding_rate is not None and funding_label == RATE_SETTLED_FUNDING
    long_conflict = False
    short_conflict = False
    ratio_value = None
    if ratio_obs is not None and not ratio_reasons:
        ratio_value = _ratio_value(ratio_obs)
    if funding_ready and oi_ready and funding_rate is not None and oi_pair.current_quantity is not None and oi_pair.previous_quantity is not None:
        long_conflict, short_conflict = _existing_conflicts(
            funding_rate=funding_rate,
            current_oi=oi_pair.current_quantity,
            previous_oi=oi_pair.previous_quantity,
            ratio=ratio_value,
            price_change_pct=price_change_pct if price_quality == "verified" else None,
        )
    conflict = long_conflict or short_conflict
    eligible = funding_ready and oi_ready and not conflict and "price_only_not_derivatives_support" not in reasons
    classification = _classification(tuple(reasons), eligible=eligible, conflict=conflict)
    if classification == "eligible_no_conflict_detected":
        supports_long = "eligible_no_conflict_detected"
        supports_short = "eligible_no_conflict_detected"
    elif classification == "conflict_detected":
        supports_long = "false" if long_conflict else "eligible_no_conflict_detected"
        supports_short = "false" if short_conflict else "eligible_no_conflict_detected"
    elif classification in {"unverified", "acquired_after_cutoff"}:
        supports_long = "unverified"
        supports_short = "unverified"
    else:
        supports_long = NA
        supports_short = NA
    if supports_long == "true" or supports_short == "true":
        raise RuntimeError("shadow support must not be true")
    dependencies = (
        "combined_conclusion_requires_valid_settled_funding",
        "combined_conclusion_requires_valid_comparable_oi_pair",
        "account_ratio_used_only_when_admissible",
        "price_used_only_when_admissible",
        "absence_of_conflict_is_not_validated_confirmation",
        "validated_confirmation_not_emitted",
    )
    return DerivativesSourceTruthResult(
        validated_confirmation=False,
        classification=classification,
        decision_cutoff=cutoff.isoformat() if cutoff is not None else NA,
        oi_change_pct=_dec_text(oi_pair.change_pct) if oi_pair.context != "not_admissible" else NA,
        oi_comparison_interval_ms=oi_pair.interval_ms,
        oi_interval_label=oi_pair.interval_label,
        oi_change_context=oi_pair.context,
        funding_rate=funding_texts[0] if funding_ready or funding_label == RATE_SETTLED_FUNDING else NA,
        funding_label=funding_label,
        funding_percent=funding_texts[1] if funding_label == RATE_SETTLED_FUNDING else NA,
        funding_basis_points=funding_texts[2] if funding_label == RATE_SETTLED_FUNDING else NA,
        funding_cadence_ms=cadence,
        account_ratio=_dec_text(ratio_value) if ratio_value is not None and not ratio_reasons else NA,
        supports_long=supports_long,
        supports_short=supports_short,
        reason_codes=tuple(dict.fromkeys(reasons)),
        dependencies=dependencies,
        exclusions=tuple(dict.fromkeys(exclusions)),
        liquidation_coverage=liquidation_coverage_label(liquidation_snapshot),
        lineage=_bounded_lineage(
            _result_lineage(
                instrument=instrument,
                oi_pair=oi_pair,
                funding_obs=funding_obs,
                ratio_obs=ratio_obs,
                batches=(
                    ("open_interest", open_interest),
                    ("open_interest_history", open_interest_history),
                    ("funding", funding),
                    ("funding_history", funding_history),
                    ("account_ratio", account_ratio),
                ),
            )
        ),
    )


def _existing_conflicts(
    *,
    funding_rate: Decimal,
    current_oi: Decimal,
    previous_oi: Decimal,
    ratio: Decimal | None,
    price_change_pct: Decimal | None,
) -> tuple[bool, bool]:
    engine = DerivativesEnrichmentEngine()
    funding_ctx = engine._funding_context(funding_rate, 1)
    oi_ctx = engine._open_interest_context(current_oi, previous_oi, 2)
    long_conflict = funding_ctx.funding_status == "extreme_positive" and oi_ctx.oi_direction == "rising"
    short_conflict = funding_ctx.funding_status == "extreme_negative" and oi_ctx.oi_direction == "rising"
    if ratio is not None:
        crowding = engine._crowding_context(funding_ctx, oi_ctx, ratio)
        long_conflict = long_conflict or (crowding.risk_direction == "long" and crowding.crowding_risk == "high")
        short_conflict = short_conflict or (crowding.risk_direction == "short" and crowding.crowding_risk == "high")
        if price_change_pct is not None:
            price_change, price_direction = engine._price_context(None, _price_candles(price_change_pct))
            if price_direction == NA:
                price_direction = _direction_from_change(engine, price_change_pct)
                price_change = price_change_pct
            price_oi = engine._price_oi_context(price_change, price_direction, oi_ctx.oi_direction)
            squeeze = engine._squeeze_context(funding_ctx, oi_ctx, price_oi, ratio)
            long_conflict = long_conflict or squeeze.squeeze_risk == "long_squeeze_risk"
            short_conflict = short_conflict or squeeze.squeeze_risk == "short_squeeze_risk"
    return long_conflict, short_conflict


def _price_candles(change_pct: Decimal) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    start = Decimal("100")
    end = start * (Decimal(1) + (change_pct / Decimal(100)))
    return ({"close": start}, {"close": end})


def _direction_from_change(engine: DerivativesEnrichmentEngine, change_pct: Decimal) -> str:
    if abs(change_pct) <= engine.price_flat_threshold_pct:
        return "flat"
    return "up" if change_pct > 0 else "down"


def _oi_points(batch: DerivativesEvidenceBatch | None) -> list[_OiPoint]:
    if batch is None:
        return []
    cache_reasons = _cache_chronology(batch)
    points: list[_OiPoint] = []
    for observation in batch.observations:
        primary = _chosen_quantity(observation)
        if primary is not None:
            points.append(_OiPoint(observation, primary, cache_reasons))
    return points


def _chosen_quantity(observation: DerivativesObservation) -> QuantityFact | None:
    quantities = [item for item in observation.quantities if item.unit != UNIT_QUOTE_NOTIONAL]
    for count in (COUNT_OPEN_CONTRACT, COUNT_BOTH_SIDES):
        for quantity in quantities:
            if quantity.counting_convention == count:
                return quantity
    return None


def _single_side_only(batch: DerivativesEvidenceBatch | None) -> bool:
    if batch is None:
        return False
    for observation in batch.observations:
        counts = {quantity.counting_convention for quantity in observation.quantities}
        if COUNT_SINGLE_SIDE in counts and not counts & _PRIMARY_COUNTS:
            return True
    return False


def _series_compatible(left: _OiPoint, right: _OiPoint) -> bool:
    if left.quantity.counting_convention != right.quantity.counting_convention:
        return False
    if left.observation.exchange != right.observation.exchange:
        return False
    if left.observation.identity.requested_symbol != right.observation.identity.requested_symbol:
        return False
    if (
        left.quantity.unit == UNIT_BASE_QUANTITY
        and right.quantity.unit == UNIT_BASE_QUANTITY
        and left.quantity.unit_asset != right.quantity.unit_asset
    ):
        return False
    same_endpoint = left.observation.endpoint == right.observation.endpoint and left.quantity.raw_field == right.quantity.raw_field
    if same_endpoint:
        return True
    pair = {
        (left.observation.endpoint, left.quantity.raw_field),
        (right.observation.endpoint, right.quantity.raw_field),
    }
    return pair <= _BINANCE_QUANTITY_FIELDS and len(pair) == 2


def _point_block_reasons(point: _OiPoint, cutoff: datetime | None) -> tuple[str, ...]:
    reasons: list[str] = []
    observation = point.observation
    if observation.semantic_quality == QUALITY_CONFLICT or "symbol_conflict" in observation.reason_codes:
        reasons.append("symbol_conflict")
    if "category_mismatch" in observation.reason_codes or "venue_mismatch" in observation.reason_codes:
        reasons.append("identity_conflict")
    if observation.semantic_quality != QUALITY_VERIFIED or point.quantity.unit != UNIT_BASE_QUANTITY:
        reasons.append("unit_unverified")
    if point.quantity.numeric_value is None:
        reasons.append("malformed_quantity")
    elif point.quantity.numeric_value < 0:
        reasons.append("negative_open_interest")
    if "unsupported_inverse" in observation.reason_codes:
        reasons.append("unsupported_inverse")
    if "multiplier_unverified" in observation.reason_codes:
        reasons.append("multiplier_unverified")
    reasons.extend(point.cache_reasons)
    reasons.extend(_temporal_reasons(observation, cutoff))
    return tuple(dict.fromkeys(reasons))


def _temporal_reasons(observation: DerivativesObservation, cutoff: datetime | None) -> tuple[str, ...]:
    reasons: list[str] = []
    metric = observation.metric_time.at
    acquired = observation.acquisition.acquired_at
    if observation.metric_time.quality != QUALITY_VERIFIED or metric is None:
        reasons.append("missing_metric_timestamp")
    if observation.acquisition.quality != QUALITY_VERIFIED or acquired is None:
        reasons.append("missing_acquisition_timestamp")
    if metric is not None and acquired is not None and metric > acquired:
        reasons.append("metric_after_acquisition")
    response = observation.response_time.at
    if observation.response_time.quality == QUALITY_UNVERIFIED:
        reasons.append("malformed_exchange_response_time")
    if response is not None and metric is not None and metric > response:
        reasons.append("metric_after_response")
    if response is not None and acquired is not None and response > acquired:
        reasons.append("response_after_acquisition")
    if cutoff is None:
        reasons.append("missing_decision_cutoff")
    else:
        if metric is not None and metric > cutoff:
            reasons.append("metric_after_cutoff")
        if acquired is not None and acquired > cutoff:
            reasons.append("acquisition_after_cutoff")
    return tuple(dict.fromkeys(reasons))


def _select_funding(
    current: DerivativesEvidenceBatch | None,
    history: DerivativesEvidenceBatch | None,
    cutoff: datetime | None,
    instrument: InstrumentMetadata | None,
) -> tuple[DerivativesObservation | None, tuple[str, ...]]:
    del instrument
    owned = {id(item): batch for batch in (history, current) if batch is not None for item in batch.observations}
    observations = [item for batch in (history, current) if batch is not None for item in batch.observations]
    if not observations:
        return None, ("missing_funding",)
    timed = [item for item in observations if item.metric_time.at is not None and item.metric_time.quality == QUALITY_VERIFIED]
    if not timed:
        return None, ("missing_metric_timestamp",)
    newest_at = max(item.metric_time.at for item in timed if item.metric_time.at is not None)
    newest = [item for item in timed if item.metric_time.at == newest_at]
    if _funding_conflict(newest):
        return None, ("duplicate_conflict",)
    if _observation_provenance_conflict(newest):
        return None, ("conflicting_provenance",)
    chosen = sorted(newest, key=lambda item: (item.endpoint, item.quantities[0].raw_value if item.quantities else ""))[0]
    if chosen.rate_kind != RATE_SETTLED_FUNDING or "unexpected_funding_subtype" in chosen.reason_codes:
        return None, ("unexpected_funding_subtype",)
    reasons = list(_temporal_reasons(chosen, cutoff))
    reasons.extend(_cache_chronology(owned.get(id(chosen))))
    reasons.extend(code for code in chosen.reason_codes if code in _IDENTITY_FAILURES)
    quantity = chosen.quantities[0] if chosen.quantities else None
    if quantity is None or quantity.unit != UNIT_FUNDING_FRACTION or quantity.numeric_value is None:
        reasons.append("malformed_quantity")
    elif abs(quantity.numeric_value) >= 1:
        reasons.append("unexpected_funding_magnitude")
    if chosen.semantic_quality == QUALITY_CONFLICT and "symbol_conflict" not in reasons:
        reasons.append("symbol_conflict")
    if chosen.semantic_quality != QUALITY_VERIFIED and not reasons:
        reasons.append("unverified_funding_semantics")
    if reasons:
        return chosen, tuple(dict.fromkeys(reasons))
    return chosen, ()


def _select_ratio(
    batch: DerivativesEvidenceBatch | None,
    cutoff: datetime | None,
) -> tuple[DerivativesObservation | None, tuple[str, ...]]:
    if batch is None or not batch.observations:
        reasons = ("missing_account_ratio",)
        if batch is not None and "fast_mode_skipped" in batch.reason_codes:
            reasons = ("fast_mode_skipped", "missing_account_ratio")
        return None, reasons
    timed = [item for item in batch.observations if item.metric_time.at is not None]
    if not timed:
        return None, ("missing_metric_timestamp",)
    newest_at = max(item.metric_time.at for item in timed if item.metric_time.at is not None)
    newest = [item for item in timed if item.metric_time.at == newest_at]
    if _observation_provenance_conflict(newest):
        return None, ("conflicting_provenance",)
    values = {_ratio_value(item) for item in newest}
    if len(values) > 1:
        return None, ("duplicate_conflict",)
    chosen = sorted(newest, key=lambda item: item.endpoint)[0]
    identity = tuple(code for code in chosen.reason_codes if code in _IDENTITY_FAILURES)
    ratio_failures = tuple(
        code
        for code in chosen.reason_codes
        if code
        in {
            "invalid_denominator",
            "ratio_component_conflict",
            "invalid_account_ratio",
            "account_share_inconsistent",
        }
    )
    if chosen.semantic_quality != QUALITY_VERIFIED or identity or ratio_failures:
        reasons = identity or ratio_failures or ("invalid_account_ratio",)
        reasons = tuple(dict.fromkeys((*reasons, *_cache_chronology(batch), *_temporal_reasons(chosen, cutoff))))
        return None, reasons
    reasons = list(_temporal_reasons(chosen, cutoff))
    reasons.extend(_cache_chronology(batch))
    if _ratio_value(chosen) is None:
        reasons.append("invalid_account_ratio")
    period_ms = _period_ms(chosen.series_period)
    if not reasons and chosen.metric_time.at is not None:
        _context, fresh_reasons = _freshness(chosen.metric_time.at, cutoff, period_ms)
        reasons.extend(fresh_reasons)
    return (None, tuple(dict.fromkeys(reasons))) if reasons else (chosen, ())


def _ratio_value(observation: DerivativesObservation) -> Decimal | None:
    for quantity in observation.quantities:
        if quantity.raw_field in {"longShortRatio", "buyRatio/sellRatio"} and quantity.quality == QUALITY_VERIFIED:
            return quantity.numeric_value
    return None


def _funding_conflict(observations: Sequence[DerivativesObservation]) -> bool:
    values = []
    for observation in observations:
        if not observation.quantities or observation.quantities[0].numeric_value is None:
            return True
        values.append(observation.quantities[0].numeric_value)
    return len(set(values)) > 1


def _freshness(metric_at: datetime | None, cutoff: datetime | None, allowance_ms: int | None) -> tuple[str, tuple[str, ...]]:
    if metric_at is None or cutoff is None:
        return "not_admissible", ()
    if allowance_ms is None:
        return "historical_only", ("freshness_unjustified",)
    age_ms = int((cutoff - metric_at).total_seconds() * 1000)
    if age_ms < 0:
        return "not_admissible", ("metric_after_cutoff",)
    if age_ms > allowance_ms:
        return "historical_only", ("stale_for_current_context",)
    return "current", ()


def _classification(reasons: tuple[str, ...], *, eligible: bool, conflict: bool) -> str:
    if "acquisition_after_cutoff" in reasons:
        return "acquired_after_cutoff"
    if conflict and eligible is False and "duplicate_conflict" not in reasons and _has_admissible_conflict(reasons):
        return "conflict_detected"
    if conflict:
        return "conflict_detected"
    if eligible:
        return "eligible_no_conflict_detected"
    unverified_markers = {
        "unit_unverified",
        "freshness_unjustified",
        "stale_for_current_context",
        "unexpected_funding_magnitude",
        "malformed_quantity",
        "malformed_metric_timestamp",
        "missing_metric_timestamp",
        "missing_acquisition_timestamp",
        "metric_after_cutoff",
        "metric_after_acquisition",
        "metric_after_response",
        "negative_open_interest",
        "zero_baseline",
        "unsupported_inverse",
        "multiplier_unverified",
        "duplicate_conflict",
        "symbol_conflict",
        "identity_conflict",
        "notional_not_position_growth",
        "single_side_not_substituted",
        "cross_instrument_not_paired",
        "invalid_denominator",
        "invalid_account_ratio",
        "ratio_component_conflict",
        "non_finite_quantity",
        "ambiguous_metric_timestamp",
        "endpoint_incompatible",
        "response_after_acquisition",
        "delivery_before_acquisition",
        "malformed_exchange_response_time",
        "conflicting_provenance",
        "request_context_identity_conflict",
        "account_share_inconsistent",
        "settlement_mismatch",
        "contract_mismatch",
        "unexpected_funding_subtype",
        "description_without_acquisition_fact",
        "acquisition_capture_failed",
    }
    if any(reason in unverified_markers for reason in reasons):
        return "unverified"
    return "not_available"


def _has_admissible_conflict(reasons: tuple[str, ...]) -> bool:
    del reasons
    return True


def _blocking(reasons: tuple[str, ...]) -> bool:
    return any(
        reason
        not in {"freshness_unjustified", "stale_for_current_context", "funding_cadence_unknown", RATE_SETTLED_FUNDING, "account_population"}
        for reason in reasons
    )


def _series_period_ms(batch: DerivativesEvidenceBatch | None) -> int | None:
    if batch is None or not batch.observations:
        return None
    return _period_ms(batch.observations[0].series_period)


def _period_ms(period: str) -> int | None:
    return _PERIOD_MS.get(str(period).strip().lower())


def _chosen_series_points(points: Sequence[_OiPoint]) -> list[_OiPoint]:
    """Keep one instrument series at a timestamp.

    Another instrument observed at the same time is not a value conflict, and a
    conflict inside the chosen series does not fall through to that other series.
    """

    primary = [point for point in points if point.quantity.counting_convention in _PRIMARY_COUNTS]
    pool = primary or list(points)
    if not pool:
        return []
    anchor = sorted(pool, key=_point_sort)[0]
    key = _series_partition(anchor)
    return [point for point in pool if _series_partition(point) == key]


def _collapse_timestamp_group(
    points: Sequence[_OiPoint],
) -> tuple[_OiPoint | None, tuple[str, ...], tuple[str, ...]]:
    """Collapse one timestamp. Conflicts exclude the whole group."""

    if not points:
        return None, ("missing_open_interest",), ()
    partitions: dict[tuple[Any, ...], list[_OiPoint]] = {}
    for point in points:
        partitions.setdefault(_series_partition(point), []).append(point)
    exclusions = tuple(_exclusion(point, "duplicate_conflict") for point in points)
    if len(partitions) != 1:
        return None, ("duplicate_conflict",), exclusions
    group = next(iter(partitions.values()))
    chronology = _group_chronology(group)
    if chronology:
        return None, chronology, tuple(_exclusion(point, ",".join(chronology)) for point in group)
    if _values_conflict(group):
        return None, ("duplicate_conflict",), exclusions
    if _provenance_conflict(group):
        return None, ("conflicting_provenance",), tuple(_exclusion(point, "conflicting_provenance") for point in group)
    return sorted(group, key=_point_sort)[0], (), ()


def _series_partition(point: _OiPoint) -> tuple[Any, ...]:
    quantity = point.quantity
    identity = point.observation.identity
    return (
        point.observation.exchange,
        identity.requested_symbol,
        identity.returned_symbol,
        quantity.counting_convention,
        quantity.unit,
        quantity.unit_asset,
    )


def _values_conflict(points: Sequence[_OiPoint]) -> bool:
    values: list[Decimal] = []
    for point in points:
        if point.quantity.numeric_value is None:
            return True
        values.append(point.quantity.numeric_value)
    return len(set(values)) > 1


def _provenance_conflict(points: Sequence[_OiPoint]) -> bool:
    if len(points) <= 1:
        return False
    if len({point.observation.acquisition.acquired_at for point in points}) > 1:
        return True
    if len({point.observation.acquisition.quality for point in points}) > 1:
        return True
    if len({point.observation.response_time.at for point in points}) > 1:
        return True
    endpoints = {(point.observation.endpoint, point.quantity.raw_field) for point in points}
    if len(endpoints) == 1:
        return False
    return not (endpoints <= _BINANCE_QUANTITY_FIELDS and len(endpoints) == 2)


def _group_chronology(points: Sequence[_OiPoint]) -> tuple[str, ...]:
    reasons: list[str] = []
    for point in points:
        reasons.extend(point.cache_reasons)
        response = point.observation.response_time.at
        acquired = point.observation.acquisition.acquired_at
        if response is not None and acquired is not None and response > acquired:
            reasons.append("response_after_acquisition")
        if point.observation.response_time.quality == QUALITY_UNVERIFIED:
            reasons.append("malformed_exchange_response_time")
    return tuple(dict.fromkeys(reasons))


def _observation_provenance_conflict(observations: Sequence[DerivativesObservation]) -> bool:
    if len(observations) <= 1:
        return False
    if len({item.acquisition.acquired_at for item in observations}) > 1:
        return True
    if len({item.response_time.at for item in observations}) > 1:
        return True
    return False


def _cache_chronology(batch: DerivativesEvidenceBatch | None) -> tuple[str, ...]:
    if batch is None:
        return ()
    delivered = batch.cache.delivered_at
    acquired = batch.acquisition.acquired_at
    if delivered is not None and acquired is not None and delivered < acquired:
        return ("delivery_before_acquisition",)
    original = batch.cache.original_acquired_at
    if delivered is not None and original is not None and delivered < original:
        return ("delivery_before_acquisition",)
    return ()


def _duplicate_conflict(points: Sequence[_OiPoint]) -> bool:
    grouped: dict[tuple[str, str, str, str], list[Decimal]] = {}
    for point in points:
        if point.quantity.numeric_value is None:
            return True
        key = (
            point.observation.exchange,
            point.observation.identity.requested_symbol,
            point.quantity.counting_convention,
            point.quantity.unit_asset,
        )
        grouped.setdefault(key, []).append(point.quantity.numeric_value)
    return any(len(set(values)) > 1 for values in grouped.values())


def _point_sort(point: _OiPoint) -> tuple[str, str, str]:
    return (point.observation.endpoint, point.quantity.raw_field, point.quantity.raw_value)


def _exclusion(point: _OiPoint, reason: str) -> str:
    stamp = point.observation.metric_time.raw
    return f"{point.observation.endpoint}:{point.quantity.raw_field}:{stamp}:{reason}"


def _instrument_visible_at_cutoff(
    instrument: InstrumentMetadata | None,
    decision_cutoff: datetime | None,
) -> InstrumentMetadata | None:
    if instrument is None:
        return None
    reasons = list(instrument.reason_codes)
    acquired = instrument.acquisition.acquired_at
    cutoff = _coerce_cutoff(decision_cutoff)
    if instrument.acquisition.quality != QUALITY_VERIFIED or acquired is None:
        reasons.append("missing_acquisition_timestamp")
        return replace(instrument, quality=QUALITY_UNVERIFIED, reason_codes=tuple(dict.fromkeys(reasons)))
    if cutoff is not None and acquired > cutoff:
        reasons.append("instrument_acquired_after_cutoff")
        return replace(instrument, quality=QUALITY_UNVERIFIED, reason_codes=tuple(dict.fromkeys(reasons)))
    return instrument


def _coerce_cutoff(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return normalize_utc_timestamp(value, field_name="decision_cutoff")


def _funding_texts(rate: Decimal) -> tuple[str, str, str, tuple[str, ...]]:
    if abs(rate) >= 1:
        return NA, NA, NA, ("unexpected_funding_magnitude",)
    fraction = rate.quantize(OUTPUT_QUANT)
    percent = (rate * Decimal(100)).quantize(OUTPUT_QUANT)
    basis_points = (rate * Decimal(10000)).quantize(OUTPUT_QUANT)
    return format(fraction, "f"), format(percent, "f"), format(basis_points, "f"), ()


def _dec_text(value: Decimal | None) -> str:
    if value is None:
        return NA
    return format(value.quantize(OUTPUT_QUANT), "f")


def _error_result(
    error_name: str,
    liquidation_coverage: str,
    lineage: tuple[SourceLineageFact, ...] = (),
) -> DerivativesSourceTruthResult:
    return DerivativesSourceTruthResult(
        validated_confirmation=False,
        classification="evaluation_error",
        supports_long="unverified",
        supports_short="unverified",
        reason_codes=("evaluation_error",),
        dependencies=("validated_confirmation_not_emitted",),
        evaluation_error=error_name,
        liquidation_coverage=liquidation_coverage,
        lineage=lineage,
    )


def _result_lineage(
    *,
    instrument: InstrumentMetadata | None,
    oi_pair: OpenInterestPairSelection,
    funding_obs: DerivativesObservation | None,
    ratio_obs: DerivativesObservation | None,
    batches: Sequence[tuple[str, DerivativesEvidenceBatch | None]],
) -> tuple[SourceLineageFact, ...]:
    del funding_obs, ratio_obs
    facts: list[SourceLineageFact] = []
    instrument_fact = _instrument_lineage(instrument)
    if instrument_fact is not None:
        facts.append(instrument_fact)
    for role, batch in batches:
        if batch is None:
            continue
        for observation in batch.observations:
            fact_role = role
            if observation.metric == "open_interest":
                fact_role = "excluded_oi" if oi_pair.change_pct is None else "selected_current_oi"
            facts.append(_lineage_fact(fact_role, observation, batch, instrument))
    return tuple(facts)


def _safe_input_lineage(
    *,
    instrument: InstrumentMetadata | None,
    batches: Sequence[tuple[str, DerivativesEvidenceBatch | None]],
) -> tuple[SourceLineageFact, ...]:
    try:
        facts: list[SourceLineageFact] = []
        instrument_fact = _instrument_lineage(instrument)
        if instrument_fact is not None:
            facts.append(instrument_fact)
        for role, batch in batches:
            if batch is None:
                continue
            for observation in getattr(batch, "observations", ()):
                facts.append(_lineage_fact(role, observation, batch, None))
        return _bounded_lineage(facts)
    except Exception:
        return ()


def _bounded_lineage(facts: Sequence[SourceLineageFact]) -> tuple[SourceLineageFact, ...]:
    ordered = tuple(
        sorted(
            facts,
            key=lambda item: (
                item.role,
                item.endpoint,
                item.metric_time or "",
                item.raw_field,
                item.raw_value,
            ),
        )
    )
    return ordered[:_LINEAGE_LIMIT]


def _instrument_lineage(instrument: InstrumentMetadata | None) -> SourceLineageFact | None:
    if instrument is None:
        return None
    try:
        acquired = instrument.acquisition.acquired_at
        return SourceLineageFact(
            role="instrument",
            exchange=instrument.exchange,
            endpoint=instrument.source_endpoint,
            requested_symbol=instrument.requested_symbol,
            returned_symbol=instrument.symbol,
            market_category=instrument.market_category,
            contract_type=instrument.contract_type,
            settlement_asset=instrument.settlement_asset,
            base_asset=instrument.base_asset,
            raw_field="instrument",
            raw_value=instrument.symbol or NA,
            unit=NA,
            counting_convention=NA,
            transformation="instrument_metadata",
            metric_time=None,
            response_time=None,
            acquired_at=acquired.isoformat() if acquired is not None else None,
            cache_delivered_at=None,
            reason_codes=tuple(instrument.reason_codes),
        )
    except Exception:
        return None


def _lineage_fact(
    role: str,
    observation: DerivativesObservation,
    batch: DerivativesEvidenceBatch | None,
    instrument: InstrumentMetadata | None,
) -> SourceLineageFact:
    quantity = next((item for item in observation.quantities if item.unit != UNIT_QUOTE_NOTIONAL), None)
    if quantity is None and observation.quantities:
        quantity = observation.quantities[0]
    delivered = None
    if batch is not None and batch.cache.delivered_at is not None:
        delivered = batch.cache.delivered_at.isoformat()
    return SourceLineageFact(
        role=role,
        exchange=observation.exchange,
        endpoint=observation.endpoint,
        requested_symbol=observation.identity.requested_symbol,
        returned_symbol=observation.identity.returned_symbol,
        market_category=observation.identity.market_category,
        contract_type=getattr(instrument, "contract_type", None) if instrument is not None else None,
        settlement_asset=getattr(instrument, "settlement_asset", None) if instrument is not None else None,
        base_asset=getattr(instrument, "base_asset", None) if instrument is not None else None,
        raw_field=quantity.raw_field if quantity is not None else NA,
        raw_value=quantity.raw_value if quantity is not None else NA,
        unit=quantity.unit if quantity is not None else NA,
        counting_convention=quantity.counting_convention if quantity is not None else NA,
        transformation=quantity.transformation if quantity is not None else NA,
        metric_time=observation.metric_time.at.isoformat() if observation.metric_time.at is not None else None,
        response_time=observation.response_time.at.isoformat() if observation.response_time.at is not None else None,
        acquired_at=observation.acquisition.acquired_at.isoformat() if observation.acquisition.acquired_at is not None else None,
        cache_delivered_at=delivered,
        reason_codes=tuple(observation.reason_codes),
    )


def evidence_map_reasons(evidence: Mapping[str, Any] | None) -> tuple[str, ...]:
    if not evidence:
        return ()
    reasons: list[str] = []
    for value in evidence.values():
        codes = getattr(value, "reason_codes", ())
        reasons.extend(str(code) for code in codes)
    return tuple(dict.fromkeys(reasons))


__all__ = [
    "ENFORCEMENT",
    "SHADOW_FRESHNESS_POLICY",
    "DerivativesSourceTruthResult",
    "OpenInterestPairSelection",
    "evaluate_derivatives_source_truth",
    "liquidation_coverage_label",
    "native_position_change_pct",
    "quote_notional",
    "select_open_interest_pair",
    "write_dev_shadow_artifact",
]
