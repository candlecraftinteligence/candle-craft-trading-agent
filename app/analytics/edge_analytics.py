from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import Enum
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data.dtos import NA, MaybeDecimal

OUTPUT_QUANT = Decimal("0.00000001")
PERCENT_QUANT = Decimal("0.01")
DEFAULT_EDGE_MIN_SAMPLE = 20

ConfidenceLabel = Literal["HIGH CONFIDENCE", "MODERATE CONFIDENCE", "LOW SAMPLE", "NEGATIVE EDGE", "N/A"]

CONDITION_DIMENSIONS = (
    "symbol",
    "mode",
    "htf_direction_alignment",
    "derivatives_state",
    "volume_profile_alignment",
    "rr_bucket",
    "readiness_score_bucket",
    "sweep_quality",
    "pullback_quality",
    "ob_fvg_quality",
    "trend_alignment",
    "crowding_state",
)


class EdgeConditionKey(BaseModel):
    symbol: str = NA
    mode: str = NA
    htf_direction_alignment: str = NA
    derivatives_state: str = NA
    volume_profile_alignment: str = NA
    rr_bucket: str = NA
    readiness_score_bucket: str = NA
    sweep_quality: str = NA
    pullback_quality: str = NA
    ob_fvg_quality: str = NA
    trend_alignment: str = NA
    crowding_state: str = NA

    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        text = _display(value)
        return text if text else NA

    def signature(self) -> str:
        return "|".join(f"{dimension}={getattr(self, dimension)}" for dimension in CONDITION_DIMENSIONS)


class EdgeAnalyticsRecord(BaseModel):
    condition_key: EdgeConditionKey
    filled: bool = False
    tp1_hit: bool = False
    tp2_hit: bool = False
    r_multiple: MaybeDecimal = NA
    candles_held: int = 0

    model_config = ConfigDict(frozen=True)

    @field_validator("r_multiple", mode="before")
    @classmethod
    def _normalize_r(cls, value: Any) -> Any:
        if _is_missing(value):
            return NA
        return _quantize(_decimal_from(value, "r_multiple"))

    @field_validator("candles_held", mode="before")
    @classmethod
    def _normalize_candles_held(cls, value: Any) -> int:
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0


class ExpectancyMetrics(BaseModel):
    setups: int = 0
    fills: int = 0
    tp1_hit_rate: MaybeDecimal = NA
    tp2_hit_rate: MaybeDecimal = NA
    average_r: MaybeDecimal = NA
    median_r: MaybeDecimal = NA
    max_drawdown: MaybeDecimal = NA
    expectancy: MaybeDecimal = NA
    average_hold_time: MaybeDecimal = NA

    model_config = ConfigDict(frozen=True)


class EdgeConditionPerformance(BaseModel):
    group_by: str = "condition"
    group_value: str = NA
    condition_key: EdgeConditionKey = Field(default_factory=EdgeConditionKey)
    expectancy_metrics: ExpectancyMetrics = Field(default_factory=ExpectancyMetrics)
    confidence_label: ConfidenceLabel = NA
    edge_score: MaybeDecimal = NA
    sample_size: int = 0
    sample_warning: str = "LOW SAMPLE"
    rank_category: Literal["strongest", "weakest", "unstable", "unranked"] = "unranked"

    model_config = ConfigDict(frozen=True)


class EdgeAnalyticsReport(BaseModel):
    min_sample: int = DEFAULT_EDGE_MIN_SAMPLE
    total_records: int = 0
    total_groups: int = 0
    expectancy_metrics: ExpectancyMetrics = Field(default_factory=ExpectancyMetrics)
    confidence_label: ConfidenceLabel = NA
    strongest_conditions: tuple[EdgeConditionPerformance, ...] = ()
    weakest_conditions: tuple[EdgeConditionPerformance, ...] = ()
    unstable_conditions: tuple[EdgeConditionPerformance, ...] = ()
    condition_groups: tuple[EdgeConditionPerformance, ...] = ()
    dimension_breakdowns: dict[str, tuple[EdgeConditionPerformance, ...]] = Field(default_factory=dict)
    safety_note: str = (
        "Historical edge analytics are diagnostic only. Low-sample groups are marked weak and no "
        "profitability is fabricated."
    )

    model_config = ConfigDict(frozen=True)


class HistoricalMatchSummary(BaseModel):
    matched: bool = False
    condition_key: EdgeConditionKey = Field(default_factory=EdgeConditionKey)
    expectancy_metrics: ExpectancyMetrics = Field(default_factory=ExpectancyMetrics)
    confidence_label: ConfidenceLabel = "LOW SAMPLE"
    edge_score: MaybeDecimal = NA
    matching_sample_size: int = 0
    match_group: str = NA
    warning: str = "No historical condition match found."

    model_config = ConfigDict(frozen=True)


def build_edge_analytics_report(
    records: Sequence[EdgeAnalyticsRecord | Mapping[str, Any]],
    *,
    min_sample: int = DEFAULT_EDGE_MIN_SAMPLE,
) -> EdgeAnalyticsReport:
    normalized = tuple(_normalize_record(record) for record in records)
    safe_min_sample = max(1, int(min_sample))
    overall_metrics = expectancy_metrics(normalized)
    condition_groups = _condition_performances(normalized, min_sample=safe_min_sample)
    strongest = tuple(
        performance.model_copy(update={"rank_category": "strongest"})
        for performance in sorted(
            (
                item
                for item in condition_groups
                if item.confidence_label in ("HIGH CONFIDENCE", "MODERATE CONFIDENCE")
                and item.edge_score != NA
            ),
            key=lambda item: _decimal_or_floor(item.edge_score),
            reverse=True,
        )[:10]
    )
    weakest = tuple(
        performance.model_copy(update={"rank_category": "weakest"})
        for performance in sorted(
            (
                item
                for item in condition_groups
                if item.confidence_label == "NEGATIVE EDGE" and item.edge_score != NA
            ),
            key=lambda item: _decimal_or_floor(item.edge_score),
        )[:10]
    )
    unstable = tuple(
        performance.model_copy(update={"rank_category": "unstable"})
        for performance in sorted(
            (item for item in condition_groups if item.confidence_label == "LOW SAMPLE"),
            key=lambda item: (item.sample_size, item.expectancy_metrics.setups, item.group_value),
        )[:10]
    )

    return EdgeAnalyticsReport(
        min_sample=safe_min_sample,
        total_records=len(normalized),
        total_groups=len(condition_groups),
        expectancy_metrics=overall_metrics,
        confidence_label=confidence_label(overall_metrics, min_sample=safe_min_sample),
        strongest_conditions=strongest,
        weakest_conditions=weakest,
        unstable_conditions=unstable,
        condition_groups=condition_groups,
        dimension_breakdowns=_dimension_breakdowns(normalized, min_sample=safe_min_sample),
    )


def expectancy_metrics(records: Sequence[EdgeAnalyticsRecord | Mapping[str, Any]]) -> ExpectancyMetrics:
    normalized = tuple(_normalize_record(record) for record in records)
    filled = tuple(record for record in normalized if record.filled)
    r_values = tuple(
        _decimal_from(record.r_multiple, "r_multiple")
        for record in filled
        if record.r_multiple != NA
    )
    tp1_hits = sum(1 for record in filled if record.tp1_hit)
    tp2_hits = sum(1 for record in filled if record.tp2_hit)
    return ExpectancyMetrics(
        setups=len(normalized),
        fills=len(filled),
        tp1_hit_rate=_rate(tp1_hits, len(filled)),
        tp2_hit_rate=_rate(tp2_hits, len(filled)),
        average_r=_mean(r_values),
        median_r=NA if not r_values else _quantize(Decimal(str(median(r_values)))),
        max_drawdown=_max_drawdown(r_values),
        expectancy=_mean(r_values),
        average_hold_time=_mean(tuple(Decimal(record.candles_held) for record in filled)),
    )


def confidence_label(metrics: ExpectancyMetrics, *, min_sample: int = DEFAULT_EDGE_MIN_SAMPLE) -> ConfidenceLabel:
    if metrics.fills < max(1, int(min_sample)) or metrics.expectancy == NA:
        return "LOW SAMPLE"
    expectancy = _decimal_from(metrics.expectancy, "expectancy")
    tp1_rate = _decimal_or_zero(metrics.tp1_hit_rate)
    if expectancy <= 0:
        return "NEGATIVE EDGE"
    if expectancy >= Decimal("0.35") and tp1_rate >= Decimal("45"):
        return "HIGH CONFIDENCE"
    return "MODERATE CONFIDENCE"


def edge_score(metrics: ExpectancyMetrics, *, min_sample: int = DEFAULT_EDGE_MIN_SAMPLE) -> MaybeDecimal:
    if metrics.expectancy == NA or metrics.fills == 0:
        return NA

    label = confidence_label(metrics, min_sample=min_sample)
    expectancy = _decimal_from(metrics.expectancy, "expectancy")
    tp1_rate = _decimal_or_zero(metrics.tp1_hit_rate)
    tp2_rate = _decimal_or_zero(metrics.tp2_hit_rate)
    drawdown = _decimal_or_zero(metrics.max_drawdown)
    raw_score = (
        Decimal("50")
        + expectancy * Decimal("25")
        + tp1_rate * Decimal("0.20")
        + tp2_rate * Decimal("0.10")
        - drawdown * Decimal("5")
    )
    if label == "LOW SAMPLE":
        raw_score = min(raw_score, Decimal("20"))
    elif label == "NEGATIVE EDGE":
        raw_score = min(raw_score, Decimal("30"))
    return _quantize(min(Decimal("100"), max(Decimal("0"), raw_score)))


def condition_key_from_diagnostics(
    *,
    symbol: str,
    mode: str,
    diagnostics: Mapping[str, Any],
    readiness_score: Any = NA,
) -> EdgeConditionKey:
    bias = _first_non_na(diagnostics.get("bias"), _bias_from_diagnostics(diagnostics))
    return EdgeConditionKey(
        symbol=symbol,
        mode=mode,
        htf_direction_alignment=_direction_alignment(bias, diagnostics.get("htf_2d_trend")),
        derivatives_state=_derivatives_state(diagnostics),
        volume_profile_alignment=_volume_profile_alignment(bias, diagnostics),
        rr_bucket=_rr_bucket(diagnostics.get("rr_to_tp2")),
        readiness_score_bucket=_readiness_bucket(_first_non_na(readiness_score, diagnostics.get("trust_percentage"))),
        sweep_quality=_sweep_quality(diagnostics),
        pullback_quality=_pullback_quality(diagnostics),
        ob_fvg_quality=_ob_fvg_quality(diagnostics),
        trend_alignment=_trend_alignment(bias, diagnostics),
        crowding_state=_first_non_na(diagnostics.get("crowding_risk"), _nested_value(diagnostics, "crowding_context", "crowding_risk")),
    )


def match_historical_condition(
    report: EdgeAnalyticsReport,
    condition_key: EdgeConditionKey,
) -> HistoricalMatchSummary:
    group = next(
        (
            item
            for item in report.condition_groups
            if item.condition_key == condition_key
        ),
        None,
    )
    if group is None:
        return HistoricalMatchSummary(
            matched=False,
            condition_key=condition_key,
            confidence_label="LOW SAMPLE",
            warning="No historical condition sample matched this setup.",
        )

    warning = (
        "Historical sample is below the configured minimum; do not treat this as a reliable edge."
        if group.confidence_label == "LOW SAMPLE"
        else "Historical match found. Analytics are diagnostic only, not a trade guarantee."
    )
    return HistoricalMatchSummary(
        matched=True,
        condition_key=condition_key,
        expectancy_metrics=group.expectancy_metrics,
        confidence_label=group.confidence_label,
        edge_score=group.edge_score,
        matching_sample_size=group.sample_size,
        match_group=group.group_value,
        warning=warning,
    )


def _condition_performances(
    records: Sequence[EdgeAnalyticsRecord],
    *,
    min_sample: int,
) -> tuple[EdgeConditionPerformance, ...]:
    grouped: dict[str, list[EdgeAnalyticsRecord]] = defaultdict(list)
    keys_by_signature: dict[str, EdgeConditionKey] = {}
    for record in records:
        signature = record.condition_key.signature()
        grouped[signature].append(record)
        keys_by_signature[signature] = record.condition_key

    performances = [
        _performance(
            group_by="condition",
            group_value=signature,
            condition_key=keys_by_signature[signature],
            records=items,
            min_sample=min_sample,
        )
        for signature, items in grouped.items()
    ]
    return tuple(sorted(performances, key=lambda item: item.group_value))


def _dimension_breakdowns(
    records: Sequence[EdgeAnalyticsRecord],
    *,
    min_sample: int,
) -> dict[str, tuple[EdgeConditionPerformance, ...]]:
    output: dict[str, tuple[EdgeConditionPerformance, ...]] = {}
    for dimension in CONDITION_DIMENSIONS:
        grouped: dict[str, list[EdgeAnalyticsRecord]] = defaultdict(list)
        for record in records:
            grouped[getattr(record.condition_key, dimension)].append(record)
        performances = [
            _performance(
                group_by=dimension,
                group_value=value,
                condition_key=EdgeConditionKey(**{dimension: value}),
                records=items,
                min_sample=min_sample,
            )
            for value, items in grouped.items()
        ]
        output[dimension] = tuple(
            sorted(
                performances,
                key=lambda item: (
                    item.confidence_label == "LOW SAMPLE",
                    -_decimal_or_floor(item.edge_score),
                    item.group_value,
                ),
            )
        )
    return output


def _performance(
    *,
    group_by: str,
    group_value: str,
    condition_key: EdgeConditionKey,
    records: Sequence[EdgeAnalyticsRecord],
    min_sample: int,
) -> EdgeConditionPerformance:
    metrics = expectancy_metrics(records)
    label = confidence_label(metrics, min_sample=min_sample)
    sample_warning = (
        "LOW SAMPLE"
        if label == "LOW SAMPLE"
        else "NEGATIVE EDGE"
        if label == "NEGATIVE EDGE"
        else "OK"
    )
    return EdgeConditionPerformance(
        group_by=group_by,
        group_value=group_value,
        condition_key=condition_key,
        expectancy_metrics=metrics,
        confidence_label=label,
        edge_score=edge_score(metrics, min_sample=min_sample),
        sample_size=metrics.fills,
        sample_warning=sample_warning,
    )


def _direction_alignment(bias: Any, trend: Any) -> str:
    normalized_bias = _display(bias).lower()
    normalized_trend = _display(trend).lower()
    expected = _expected_trend(normalized_bias)
    if expected == NA or normalized_trend == NA.lower():
        return NA
    if normalized_trend == expected:
        return "aligned"
    if normalized_trend in ("neutral", "range", "ranging", "sideways"):
        return "neutral"
    return "conflicting"


def _trend_alignment(bias: Any, diagnostics: Mapping[str, Any]) -> str:
    values = tuple(
        value
        for value in (
            _direction_alignment(bias, diagnostics.get("htf_2d_trend")),
            _direction_alignment(bias, diagnostics.get("mtf_12h_trend")),
            _direction_alignment(bias, diagnostics.get("trend")),
        )
        if value != NA
    )
    if not values:
        return NA
    if all(value == "aligned" for value in values):
        return "aligned"
    if any(value == "conflicting" for value in values):
        return "conflicting"
    return "mixed"


def _expected_trend(bias: str) -> str:
    if bias == "long":
        return "bullish"
    if bias == "short":
        return "bearish"
    return NA


def _derivatives_state(diagnostics: Mapping[str, Any]) -> str:
    conflict = _display(diagnostics.get("derivatives_conflict_reason"))
    if conflict != NA:
        return "conflicting"
    support = diagnostics.get("derivatives_supports_trade")
    support_text = _display(support)
    if support is True or support_text == "True":
        return "supportive"
    if support is False or support_text == "False":
        return "mixed"
    funding = _first_non_na(_nested_value(diagnostics, "funding_context", "funding_status"), diagnostics.get("funding_status"))
    oi = _first_non_na(_nested_value(diagnostics, "oi_context", "oi_direction"), diagnostics.get("oi_direction"))
    if funding == NA and oi == NA:
        return NA
    return "mixed"


def _volume_profile_alignment(bias: Any, diagnostics: Mapping[str, Any]) -> str:
    poc = _optional_decimal(diagnostics.get("poc"))
    if poc == NA:
        return NA
    entry_low = _optional_decimal(diagnostics.get("entry_low"))
    entry_high = _optional_decimal(diagnostics.get("entry_high"))
    if entry_low != NA and entry_high != NA and entry_low <= poc <= entry_high:
        return "entry_overlaps_poc"

    value_area_low = _optional_decimal(diagnostics.get("value_area_low"))
    value_area_high = _optional_decimal(diagnostics.get("value_area_high"))
    entry = _optional_decimal(diagnostics.get("entry"))
    if entry != NA and value_area_low != NA and value_area_high != NA and value_area_low <= entry <= value_area_high:
        return "entry_inside_value_area"

    normalized_bias = _display(bias).lower()
    if entry != NA:
        if normalized_bias == "long" and entry >= poc:
            return "long_above_poc"
        if normalized_bias == "short" and entry <= poc:
            return "short_below_poc"
    return "volume_profile_available"


def _rr_bucket(value: Any) -> str:
    rr = _optional_decimal(value)
    if rr == NA:
        return NA
    if rr < Decimal("2"):
        return "rr_lt_2"
    if rr < Decimal("2.5"):
        return "rr_2_to_2_49"
    if rr < Decimal("3"):
        return "rr_2_5_to_2_99"
    if rr < Decimal("4"):
        return "rr_3_to_3_99"
    return "rr_4_plus"


def _readiness_bucket(value: Any) -> str:
    score = _optional_decimal(value)
    if score == NA:
        return NA
    if score < Decimal("50"):
        return "readiness_lt_50"
    if score < Decimal("70"):
        return "readiness_50_to_69"
    if score < Decimal("85"):
        return "readiness_70_to_84"
    return "readiness_85_plus"


def _sweep_quality(diagnostics: Mapping[str, Any]) -> str:
    status = _display(diagnostics.get("execution_sweep_status"))
    gates_passed = _sequence_values(diagnostics.get("gates_passed"))
    if status != "passed" and "sweep" not in gates_passed:
        return "failed" if status == "failed" else NA
    magnitude_atr = _optional_decimal(diagnostics.get("sweep_magnitude_atr"))
    if magnitude_atr == NA:
        return "confirmed"
    if magnitude_atr >= Decimal("1"):
        return "strong"
    if magnitude_atr >= Decimal("0.5"):
        return "solid"
    return "thin"


def _pullback_quality(diagnostics: Mapping[str, Any]) -> str:
    status = _display(diagnostics.get("pullback_zone_status"))
    gates_passed = _sequence_values(diagnostics.get("gates_passed"))
    fib = _display(diagnostics.get("fib_alignment_status"))
    if status in ("valid", "passed") or "pullback_zone" in gates_passed:
        if fib in ("aligned", "valid", "passed") or "fib_alignment" in gates_passed:
            return "clean"
        return "valid"
    if status == "failed":
        return "failed"
    return NA


def _ob_fvg_quality(diagnostics: Mapping[str, Any]) -> str:
    selected = _display(diagnostics.get("selected_zone_type"))
    if selected != NA:
        return f"{selected.lower()}_selected"
    ob_zone = diagnostics.get("ob_zone")
    fvg_zone = diagnostics.get("fvg_zone")
    if _zone_present(ob_zone):
        return "ob_available"
    if _zone_present(fvg_zone):
        return "fvg_available"
    if _display(diagnostics.get("pullback_zone_status")) == "failed":
        return "none"
    return NA


def _zone_present(value: Any) -> bool:
    if isinstance(value, Mapping):
        return bool(value.get("is_present"))
    return bool(getattr(value, "is_present", False))


def _bias_from_diagnostics(diagnostics: Mapping[str, Any]) -> str:
    for key in ("sweep_diagnostics", "bos_choch_diagnostics", "structure_shift_diagnostics"):
        text = _display(diagnostics.get(key)).lower()
        if "bullish" in text:
            return "long"
        if "bearish" in text:
            return "short"
    return NA


def _nested_value(diagnostics: Mapping[str, Any], key: str, nested_key: str) -> Any:
    value = diagnostics.get(key)
    if isinstance(value, Mapping):
        return value.get(nested_key, NA)
    return getattr(value, nested_key, NA)


def _normalize_record(record: EdgeAnalyticsRecord | Mapping[str, Any]) -> EdgeAnalyticsRecord:
    if isinstance(record, EdgeAnalyticsRecord):
        return record
    return EdgeAnalyticsRecord.model_validate(record)


def _rate(numerator: int, denominator: int) -> MaybeDecimal:
    if denominator == 0:
        return NA
    return ((Decimal(numerator) / Decimal(denominator)) * Decimal("100")).quantize(PERCENT_QUANT)


def _mean(values: Sequence[Decimal]) -> MaybeDecimal:
    if not values:
        return NA
    return _quantize(sum(values, Decimal("0")) / Decimal(len(values)))


def _max_drawdown(values: Sequence[Decimal]) -> MaybeDecimal:
    if not values:
        return NA
    equity = Decimal("0")
    peak = Decimal("0")
    max_drawdown = Decimal("0")
    for value in values:
        equity += value
        peak = max(peak, equity)
        max_drawdown = max(max_drawdown, peak - equity)
    return _quantize(max_drawdown)


def _optional_decimal(value: Any) -> MaybeDecimal:
    if _is_missing(value):
        return NA
    try:
        return _quantize(_decimal_from(value, "optional_decimal"))
    except ValueError:
        return NA


def _decimal_or_zero(value: Any) -> Decimal:
    decimal = _optional_decimal(value)
    return Decimal("0") if decimal == NA else decimal


def _decimal_or_floor(value: Any) -> Decimal:
    decimal = _optional_decimal(value)
    return Decimal("-999999") if decimal == NA else decimal


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid edge analytics decimal at {path}: {value!r}") from exc
    if not decimal.is_finite():
        raise ValueError(f"Invalid edge analytics decimal at {path}: {value!r}")
    return decimal


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _first_non_na(*values: Any) -> Any:
    for value in values:
        if _display(value) != NA:
            return value
    return NA


def _sequence_values(values: Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(_display(value) for value in values if _display(value) != NA)


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


def _display(value: object) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, Enum):
        return value.value
    return str(value)


__all__ = [
    "CONDITION_DIMENSIONS",
    "DEFAULT_EDGE_MIN_SAMPLE",
    "ConfidenceLabel",
    "EdgeAnalyticsRecord",
    "EdgeAnalyticsReport",
    "EdgeConditionKey",
    "EdgeConditionPerformance",
    "ExpectancyMetrics",
    "HistoricalMatchSummary",
    "build_edge_analytics_report",
    "condition_key_from_diagnostics",
    "confidence_label",
    "edge_score",
    "expectancy_metrics",
    "match_historical_condition",
]
