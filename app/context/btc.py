from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from app.agents.technical_structure import (
    TechnicalAnalysisStatus,
    TechnicalStructureAgent,
    TechnicalStructureResult,
)
from app.analytics.derivatives_enrichment import DerivativesEnrichmentInput, enrich_derivatives
from app.context.models import BtcContextPayload, ContextStatus, ContextValue
from app.data.candle_integrity import normalize_utc_timestamp, timeframe_duration, validate_candle_sequence
from app.data.dtos import NA

BTC_SYMBOL = "BTCUSDT"
BTC_CONTEXT_TIMEFRAMES = ("12h", "2h", "15m")
BTC_CONTEXT_SOURCE_PREFIX = "internal_market_data"
BTC_CANDLE_FRESHNESS_INTERVALS = 2
FUNDING_FRESHNESS_SECONDS = 24 * 60 * 60
OPEN_INTEREST_FRESHNESS_SECONDS = 15 * 60


def build_internal_btc_context(
    *,
    candles_by_timeframe: Mapping[str, Sequence[Any]],
    generated_at: datetime,
    technical_agent: TechnicalStructureAgent,
    exchange: str,
    funding: Any | None = None,
    open_interest: Any | None = None,
    open_interest_history: Sequence[Any] | None = None,
    unavailable_reasons: Mapping[str, str] | None = None,
) -> ContextValue:
    generated_utc = normalize_utc_timestamp(generated_at, field_name="btc_context_generated_at")
    source = f"{BTC_CONTEXT_SOURCE_PREFIX}:{exchange}/technical_structure_agent"
    reasons = dict(unavailable_reasons or {})
    analyses: dict[str, tuple[TechnicalStructureResult, datetime, float, ContextStatus]] = {}

    for timeframe in BTC_CONTEXT_TIMEFRAMES:
        candles = tuple(candles_by_timeframe.get(timeframe, ()))
        if not candles:
            continue
        try:
            timeline = validate_candle_sequence(candles, timeframe=timeframe)
            observed_at = timeline[-1].close_timestamp
            age_seconds = _age_seconds(generated_utc, observed_at)
            status = _freshness_status(
                age_seconds,
                stale_after_seconds=(
                    timeframe_duration(timeframe).total_seconds()
                    * BTC_CANDLE_FRESHNESS_INTERVALS
                ),
            )
            analyses[timeframe] = (
                technical_agent.analyze(candles, timeframe=timeframe),
                observed_at,
                age_seconds,
                status,
            )
        except Exception as exc:
            reasons.setdefault(timeframe, f"BTC {timeframe} analysis failed: {_clean_reason(exc)}")

    bias_12h = _trend_component(
        analyses.get("12h"),
        source=source,
        label="12h bias",
        unavailable_reason=reasons.get("12h"),
    )
    structure_2h = _trend_component(
        analyses.get("2h"),
        source=source,
        label="2h structure",
        unavailable_reason=reasons.get("2h"),
    )
    execution_15m = _trend_component(
        analyses.get("15m"),
        source=source,
        label="15m execution direction",
        unavailable_reason=reasons.get("15m"),
    )
    atr_15m, atr_pct_15m = _atr_components(
        analyses.get("15m"),
        candles=tuple(candles_by_timeframe.get("15m", ())),
        source=source,
        unavailable_reason=reasons.get("15m"),
    )
    funding_rate = _market_data_component(
        raw=funding,
        value_fields=("funding_rate", "rate", "fundingRate"),
        timestamp_fields=("timestamp", "funding_time", "fundingTime"),
        source=f"{BTC_CONTEXT_SOURCE_PREFIX}:{exchange}/funding_rate",
        generated_at=generated_utc,
        stale_after_seconds=FUNDING_FRESHNESS_SECONDS,
        label="BTC funding rate",
    )
    open_interest_value = _market_data_component(
        raw=open_interest,
        value_fields=("open_interest", "current_open_interest", "openInterest", "sumOpenInterest"),
        timestamp_fields=("timestamp", "time"),
        source=f"{BTC_CONTEXT_SOURCE_PREFIX}:{exchange}/open_interest",
        generated_at=generated_utc,
        stale_after_seconds=OPEN_INTEREST_FRESHNESS_SECONDS,
        label="BTC open interest",
    )
    open_interest_change = _open_interest_change_component(
        open_interest=open_interest,
        open_interest_history=open_interest_history,
        generated_at=generated_utc,
        exchange=exchange,
    )

    payload = BtcContextPayload(
        bias_12h=bias_12h,
        structure_2h=structure_2h,
        execution_15m=execution_15m,
        atr_15m=atr_15m,
        atr_pct_15m=atr_pct_15m,
        funding_rate=funding_rate,
        open_interest=open_interest_value,
        open_interest_change_pct=open_interest_change,
    )
    components = (
        bias_12h,
        structure_2h,
        execution_15m,
        atr_15m,
        atr_pct_15m,
        funding_rate,
        open_interest_value,
        open_interest_change,
    )
    usable = tuple(component for component in components if component.usable_for_research)
    verified = tuple(component for component in usable if component.status == ContextStatus.VERIFIED)
    stale = tuple(component for component in usable if component.status == ContextStatus.STALE)
    errors = tuple(component for component in components if component.status == ContextStatus.ERROR)
    unavailable = tuple(component for component in components if component.status == ContextStatus.UNAVAILABLE)

    if verified:
        status = ContextStatus.VERIFIED
    elif stale:
        status = ContextStatus.STALE
    elif errors:
        status = ContextStatus.ERROR
    else:
        status = ContextStatus.UNAVAILABLE
    observed_values = tuple(
        component.observed_at for component in usable if component.observed_at is not None
    )
    observed_at = max(observed_values) if observed_values else None
    reason_parts: list[str] = []
    if unavailable:
        reason_parts.append(f"{len(unavailable)} component(s) unavailable")
    if stale:
        reason_parts.append(f"{len(stale)} component(s) stale")
    if errors:
        reason_parts.append(f"{len(errors)} component(s) errored")
    if not usable:
        reason_parts.append("no usable BTC context components")
    return ContextValue(
        value=payload,
        source=source,
        observed_at=observed_at,
        age_seconds=_age_seconds(generated_utc, observed_at) if observed_at is not None else None,
        status=status,
        reason="; ".join(reason_parts) or None,
    )


def _trend_component(
    analysis_record: tuple[TechnicalStructureResult, datetime, float, ContextStatus] | None,
    *,
    source: str,
    label: str,
    unavailable_reason: str | None,
) -> ContextValue:
    if analysis_record is None:
        return ContextValue.unavailable(
            source=source,
            reason=unavailable_reason or f"{label} candles unavailable",
        )
    analysis, observed_at, age_seconds, freshness_status = analysis_record
    if not analysis.is_valid or analysis.trend_context == NA:
        status = (
            ContextStatus.ERROR
            if analysis.analysis_status == TechnicalAnalysisStatus.DATA_ERROR
            else ContextStatus.UNAVAILABLE
        )
        return ContextValue(
            source=source,
            observed_at=observed_at,
            age_seconds=age_seconds,
            status=status,
            reason="; ".join(analysis.errors) or f"{label} unavailable",
        )
    return ContextValue(
        value=analysis.trend_context,
        source=source,
        observed_at=observed_at,
        age_seconds=age_seconds,
        status=freshness_status,
        reason=_stale_reason(label, freshness_status),
    )


def _atr_components(
    analysis_record: tuple[TechnicalStructureResult, datetime, float, ContextStatus] | None,
    *,
    candles: Sequence[Any],
    source: str,
    unavailable_reason: str | None,
) -> tuple[ContextValue, ContextValue]:
    if analysis_record is None:
        reason = unavailable_reason or "15m candles unavailable for ATR"
        unavailable = ContextValue.unavailable(source=source, reason=reason)
        return unavailable, unavailable.model_copy()
    analysis, observed_at, age_seconds, freshness_status = analysis_record
    if not analysis.is_valid or analysis.atr == NA:
        reason = "; ".join(analysis.errors) or "15m ATR unavailable"
        status = (
            ContextStatus.ERROR
            if analysis.analysis_status == TechnicalAnalysisStatus.DATA_ERROR
            else ContextStatus.UNAVAILABLE
        )
        unavailable = ContextValue(
            source=source,
            observed_at=observed_at,
            age_seconds=age_seconds,
            status=status,
            reason=reason,
        )
        return unavailable, unavailable.model_copy()
    atr = _decimal(analysis.atr)
    latest_close = _decimal(_field(candles[-1], "close")) if candles else None
    atr_value = ContextValue(
        value=atr,
        source=source,
        observed_at=observed_at,
        age_seconds=age_seconds,
        status=freshness_status,
        reason=_stale_reason("15m ATR", freshness_status),
    )
    if atr is None or latest_close is None or latest_close <= 0:
        atr_pct = ContextValue.unavailable(
            source=source,
            reason="15m normalized ATR unavailable because latest close is missing or invalid",
        )
    else:
        atr_pct = ContextValue(
            value=(atr / latest_close * Decimal("100")).quantize(Decimal("0.00000001")),
            source=source,
            observed_at=observed_at,
            age_seconds=age_seconds,
            status=freshness_status,
            reason=_stale_reason("15m normalized ATR", freshness_status),
        )
    return atr_value, atr_pct


def _market_data_component(
    *,
    raw: Any | None,
    value_fields: Sequence[str],
    timestamp_fields: Sequence[str],
    source: str,
    generated_at: datetime,
    stale_after_seconds: float,
    label: str,
) -> ContextValue:
    value = _first_decimal(raw, value_fields)
    if value is None:
        return ContextValue.unavailable(source=source, reason=f"{label} unavailable")
    observed_at = _observation_time(raw, timestamp_fields, fallback=generated_at)
    age_seconds = _age_seconds(generated_at, observed_at)
    status = _freshness_status(age_seconds, stale_after_seconds=stale_after_seconds)
    return ContextValue(
        value=value,
        source=source,
        observed_at=observed_at,
        age_seconds=age_seconds,
        status=status,
        reason=_stale_reason(label, status),
    )


def _open_interest_change_component(
    *,
    open_interest: Any | None,
    open_interest_history: Sequence[Any] | None,
    generated_at: datetime,
    exchange: str,
) -> ContextValue:
    source = f"{BTC_CONTEXT_SOURCE_PREFIX}:{exchange}/derivatives_enrichment"
    current = _first_decimal(
        open_interest,
        ("open_interest", "current_open_interest", "openInterest", "sumOpenInterest"),
    )
    history = tuple(open_interest_history or ())
    previous = (
        _first_decimal(
            history[-2],
            ("open_interest", "current_open_interest", "openInterest", "sumOpenInterest"),
        )
        if len(history) >= 2
        else None
    )
    if current is None or previous is None:
        return ContextValue.unavailable(
            source=source,
            reason="BTC open-interest change unavailable because two verified observations are required",
        )
    result = enrich_derivatives(
        DerivativesEnrichmentInput(
            symbol=BTC_SYMBOL,
            exchange=exchange,
            current_open_interest=current,
            previous_open_interest=previous,
            open_interest_history=history,
            source=source,
        )
    )
    if result.open_interest_change_pct == NA:
        return ContextValue.unavailable(
            source=source,
            reason="BTC open-interest change could not be normalized",
        )
    observation_raw = history[-1] if history else open_interest
    observed_at = _observation_time(observation_raw, ("timestamp", "time"), fallback=generated_at)
    age_seconds = _age_seconds(generated_at, observed_at)
    status = _freshness_status(
        age_seconds,
        stale_after_seconds=OPEN_INTEREST_FRESHNESS_SECONDS,
    )
    return ContextValue(
        value=result.open_interest_change_pct,
        source=source,
        observed_at=observed_at,
        age_seconds=age_seconds,
        status=status,
        reason=_stale_reason("BTC open-interest change", status),
    )


def _freshness_status(age_seconds: float, *, stale_after_seconds: float) -> ContextStatus:
    return (
        ContextStatus.VERIFIED
        if age_seconds <= stale_after_seconds
        else ContextStatus.STALE
    )


def _stale_reason(label: str, status: ContextStatus) -> str | None:
    if status != ContextStatus.STALE:
        return None
    return f"{label} observation exceeds its freshness window"


def _observation_time(raw: Any, fields: Sequence[str], *, fallback: datetime) -> datetime:
    for field in fields:
        parsed = _epoch_datetime(_field(raw, field))
        if parsed is not None and parsed <= fallback + timedelta(minutes=5):
            return parsed
    return fallback


def _epoch_datetime(value: Any) -> datetime | None:
    if value in (None, "", NA):
        return None
    try:
        epoch = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if epoch > Decimal("100000000000"):
        epoch /= Decimal("1000")
    try:
        return datetime.fromtimestamp(float(epoch), tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def _first_decimal(raw: Any, fields: Sequence[str]) -> Decimal | None:
    for field in fields:
        value = _decimal(_field(raw, field))
        if value is not None:
            return value
    return None


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "", NA) or isinstance(value, bool):
        return None
    try:
        normalized = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return normalized if normalized.is_finite() else None


def _field(value: Any, name: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _age_seconds(now: datetime, observed_at: datetime | None) -> float:
    if observed_at is None:
        return 0.0
    return max((now - observed_at).total_seconds(), 0.0)


def _clean_reason(exc: Exception) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


__all__ = [
    "BTC_CONTEXT_TIMEFRAMES",
    "BTC_SYMBOL",
    "build_internal_btc_context",
]
