from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import Enum
from pathlib import Path
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.analytics.edge_analytics import EdgeConditionKey, condition_key_from_diagnostics
from app.analytics.setup_quality import SetupQualityResult, SetupQualityState
from app.data.dtos import NA, MaybeDecimal

OUTPUT_QUANT = Decimal("0.00000001")
PERCENT_QUANT = Decimal("0.01")
MEMORY_VERSION = 1
DEFAULT_MEMORY_PATH = Path("scan_runs/performance_memory.json")
INSUFFICIENT_SAMPLE_WARNING = "Performance memory confidence too low."
MEMORY_SAFETY_NOTE = (
    "Performance memory is deterministic historical evidence only. It does not predict, execute trades, "
    "or override scanner gates."
)
POSITIVE_ADJUSTMENT_CAP = 10
NEGATIVE_ADJUSTMENT_CAP = -15


class ConfidenceBucket(str, Enum):
    VERY_LOW = "VERY_LOW"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    VERY_HIGH = "VERY_HIGH"


CONFIDENCE_ORDER = {
    ConfidenceBucket.VERY_LOW: 0,
    ConfidenceBucket.LOW: 1,
    ConfidenceBucket.MEDIUM: 2,
    ConfidenceBucket.HIGH: 3,
    ConfidenceBucket.VERY_HIGH: 4,
}


class SetupFingerprint(BaseModel):
    direction: str = NA
    htf_bias: str = NA
    market_regime: str = NA
    derivatives_state: str = NA
    crowding: str = NA
    squeeze_risk: str = NA
    rr_bucket: str = NA
    pullback_quality: str = NA
    ob_fvg_quality: str = NA
    confirmation_strength: str = NA
    volatility_regime: str = NA
    symbol_category: str = NA
    mode: str = NA
    setup_type: str = NA

    model_config = ConfigDict(frozen=True)

    @field_validator("*", mode="before")
    @classmethod
    def _normalize_text(cls, value: Any) -> str:
        text = _display(value)
        if text == NA:
            return NA
        return text.strip().upper().replace(" ", "_")

    @property
    def signature(self) -> str:
        return "|".join(f"{field}={getattr(self, field)}" for field in type(self).model_fields)

    @property
    def label(self) -> str:
        parts = (
            self.direction,
            self.mode,
            self.htf_bias,
            self.market_regime,
            self.derivatives_state,
            self.crowding,
            self.pullback_quality,
            self.confirmation_strength,
        )
        cleaned = tuple(part for part in parts if part != NA)
        return " + ".join(cleaned) if cleaned else NA


class SetupPerformanceStats(BaseModel):
    fingerprint: SetupFingerprint = Field(default_factory=SetupFingerprint)
    total_occurrences: int = 0
    filled_occurrences: int = 0
    wins: int = 0
    losses: int = 0
    tp1_hits: int = 0
    tp2_hits: int = 0
    rejections: int = 0
    invalidations: int = 0
    tp1_rate: MaybeDecimal = NA
    tp2_rate: MaybeDecimal = NA
    average_r: MaybeDecimal = NA
    median_r: MaybeDecimal = NA
    max_drawdown: MaybeDecimal = NA
    average_hold_time: MaybeDecimal = NA
    rejection_frequency: MaybeDecimal = NA
    invalidation_frequency: MaybeDecimal = NA
    r_multiples: tuple[Decimal, ...] = ()
    hold_times: tuple[int, ...] = ()
    confidence_bucket: ConfidenceBucket = ConfidenceBucket.VERY_LOW
    data_quality: Literal["N/A", "insufficient_sample", "unverified", "verified"] = "insufficient_sample"

    model_config = ConfigDict(frozen=True)

    @field_validator(
        "total_occurrences",
        "filled_occurrences",
        "wins",
        "losses",
        "tp1_hits",
        "tp2_hits",
        "rejections",
        "invalidations",
    )
    @classmethod
    def _non_negative_int(cls, value: int) -> int:
        if value < 0:
            raise ValueError("performance counts must be zero or greater")
        return value

    @field_validator(
        "tp1_rate",
        "tp2_rate",
        "average_r",
        "median_r",
        "max_drawdown",
        "average_hold_time",
        "rejection_frequency",
        "invalidation_frequency",
        mode="before",
    )
    @classmethod
    def _normalize_maybe_decimal(cls, value: Any) -> MaybeDecimal:
        if _is_missing(value):
            return NA
        return _quantize(_decimal_from(value, "performance stat"))

    @field_validator("r_multiples", mode="before")
    @classmethod
    def _normalize_r_multiples(cls, value: Any) -> tuple[Decimal, ...]:
        if value is None:
            return ()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError("r_multiples must be a sequence")
        return tuple(_quantize(_decimal_from(item, "r_multiple")) for item in value)

    @field_validator("hold_times", mode="before")
    @classmethod
    def _normalize_hold_times(cls, value: Any) -> tuple[int, ...]:
        if value is None:
            return ()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError("hold_times must be a sequence")
        output = []
        for item in value:
            try:
                normalized = int(item)
            except (TypeError, ValueError) as exc:
                raise ValueError("hold time must be an integer") from exc
            if normalized < 0:
                raise ValueError("hold time must be zero or greater")
            output.append(normalized)
        return tuple(output)

    @model_validator(mode="after")
    def _validate_ranges(self) -> SetupPerformanceStats:
        if self.filled_occurrences > self.total_occurrences:
            raise ValueError("filled_occurrences cannot exceed total_occurrences")
        if self.wins + self.losses > self.filled_occurrences:
            raise ValueError("wins plus losses cannot exceed filled_occurrences")
        if self.tp1_hits > self.filled_occurrences or self.tp2_hits > self.filled_occurrences:
            raise ValueError("target hits cannot exceed filled_occurrences")
        if self.rejections > self.total_occurrences or self.invalidations > self.total_occurrences:
            raise ValueError("rejections or invalidations cannot exceed total_occurrences")
        for rate_name in ("tp1_rate", "tp2_rate", "rejection_frequency", "invalidation_frequency"):
            rate = getattr(self, rate_name)
            if rate != NA and (rate < 0 or rate > 100):
                raise ValueError(f"{rate_name} must be between 0 and 100")
        if len(self.r_multiples) != self.filled_occurrences:
            raise ValueError("r_multiples length must match filled_occurrences")
        if len(self.hold_times) > self.filled_occurrences:
            raise ValueError("hold_times cannot exceed filled_occurrences")
        return self


class SymbolPerformanceStats(SetupPerformanceStats):
    symbol: str = NA

    @field_validator("symbol", mode="before")
    @classmethod
    def _normalize_symbol(cls, value: Any) -> str:
        text = _display(value).upper()
        return text if text else NA


class RegimePerformanceStats(SetupPerformanceStats):
    regime: str = NA

    @field_validator("regime", mode="before")
    @classmethod
    def _normalize_regime(cls, value: Any) -> str:
        text = _display(value).upper()
        return text if text else NA


class PerformanceMemoryResult(BaseModel):
    enabled: bool = True
    fingerprint: SetupFingerprint = Field(default_factory=SetupFingerprint)
    historical_samples: int = 0
    tp1_rate: MaybeDecimal = NA
    tp2_rate: MaybeDecimal = NA
    average_r: MaybeDecimal = NA
    median_r: MaybeDecimal = NA
    max_drawdown: MaybeDecimal = NA
    average_hold_time: MaybeDecimal = NA
    confidence_bucket: ConfidenceBucket = ConfidenceBucket.VERY_LOW
    similar_setup_performance: str = "insufficient_sample"
    regime_compatibility: str = "unverified"
    symbol_historical_quality: str = "unverified"
    historical_expectancy: MaybeDecimal = NA
    memory_adjustments: dict[str, Any] = Field(default_factory=dict)
    historical_warning: str = INSUFFICIENT_SAMPLE_WARNING
    safety_note: str = MEMORY_SAFETY_NOTE

    model_config = ConfigDict(frozen=True)


class PerformanceMemoryStore(BaseModel):
    version: int = MEMORY_VERSION
    setup_stats: dict[str, SetupPerformanceStats] = Field(default_factory=dict)
    symbol_stats: dict[str, SymbolPerformanceStats] = Field(default_factory=dict)
    regime_stats: dict[str, RegimePerformanceStats] = Field(default_factory=dict)
    ingested_event_ids: tuple[str, ...] = ()
    rejected_memory_entries: int = 0
    load_warnings: tuple[str, ...] = ()
    safety_note: str = MEMORY_SAFETY_NOTE

    model_config = ConfigDict(frozen=True)


class ReplayIngestionResult(BaseModel):
    store: PerformanceMemoryStore
    events_seen: int = 0
    events_added: int = 0
    duplicates_ignored: int = 0
    skipped_outcomes: int = 0
    warnings: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


def confidence_bucket(samples: int) -> ConfidenceBucket:
    if samples < 10:
        return ConfidenceBucket.VERY_LOW
    if samples < 25:
        return ConfidenceBucket.LOW
    if samples < 75:
        return ConfidenceBucket.MEDIUM
    if samples < 200:
        return ConfidenceBucket.HIGH
    return ConfidenceBucket.VERY_HIGH


def confidence_meets_minimum(bucket: ConfidenceBucket | str, minimum: ConfidenceBucket | str) -> bool:
    normalized_bucket = _confidence_bucket(bucket)
    normalized_minimum = _confidence_bucket(minimum)
    return CONFIDENCE_ORDER[normalized_bucket] >= CONFIDENCE_ORDER[normalized_minimum]


def empty_performance_memory() -> PerformanceMemoryStore:
    return PerformanceMemoryStore()


def load_performance_memory(path: Path | str = DEFAULT_MEMORY_PATH) -> PerformanceMemoryStore:
    memory_path = Path(path)
    if not memory_path.exists():
        return empty_performance_memory()
    try:
        raw = json.loads(memory_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return PerformanceMemoryStore(load_warnings=(f"performance memory unavailable: {exc}",), rejected_memory_entries=1)
    if not isinstance(raw, Mapping):
        return PerformanceMemoryStore(load_warnings=("performance memory root is not an object",), rejected_memory_entries=1)

    rejected = 0
    warnings: list[str] = []
    setup_stats: dict[str, SetupPerformanceStats] = {}
    for key, value in _mapping_items(raw.get("setup_stats")):
        try:
            stats = SetupPerformanceStats.model_validate(value)
        except ValueError as exc:
            rejected += 1
            warnings.append(f"setup_stats[{key}] rejected: {_clean_error(exc)}")
            continue
        if key != stats.fingerprint.signature:
            rejected += 1
            warnings.append(f"setup_stats[{key}] rejected: signature mismatch")
            continue
        setup_stats[key] = stats

    symbol_stats: dict[str, SymbolPerformanceStats] = {}
    for key, value in _mapping_items(raw.get("symbol_stats")):
        try:
            stats = SymbolPerformanceStats.model_validate(value)
        except ValueError as exc:
            rejected += 1
            warnings.append(f"symbol_stats[{key}] rejected: {_clean_error(exc)}")
            continue
        symbol_stats[stats.symbol] = stats

    regime_stats: dict[str, RegimePerformanceStats] = {}
    for key, value in _mapping_items(raw.get("regime_stats")):
        try:
            stats = RegimePerformanceStats.model_validate(value)
        except ValueError as exc:
            rejected += 1
            warnings.append(f"regime_stats[{key}] rejected: {_clean_error(exc)}")
            continue
        regime_stats[stats.regime] = stats

    event_ids = tuple(str(item) for item in raw.get("ingested_event_ids", ()) if str(item).strip())
    version = _safe_int(raw.get("version"), MEMORY_VERSION)
    return PerformanceMemoryStore(
        version=version,
        setup_stats=setup_stats,
        symbol_stats=symbol_stats,
        regime_stats=regime_stats,
        ingested_event_ids=_unique_strings(event_ids),
        rejected_memory_entries=rejected + _safe_int(raw.get("rejected_memory_entries"), 0),
        load_warnings=tuple(warnings),
    )


def save_performance_memory(
    store: PerformanceMemoryStore,
    path: Path | str = DEFAULT_MEMORY_PATH,
) -> None:
    memory_path = Path(path)
    memory_path.parent.mkdir(parents=True, exist_ok=True)
    memory_path.write_text(json.dumps(store.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8")


def reset_performance_memory(path: Path | str = DEFAULT_MEMORY_PATH) -> PerformanceMemoryStore:
    store = empty_performance_memory()
    save_performance_memory(store, path)
    return store


def ingest_replay_summary(
    store: PerformanceMemoryStore,
    replay_summary: Any,
    *,
    source_id: str | None = None,
) -> ReplayIngestionResult:
    setup_stats = dict(store.setup_stats)
    symbol_stats = dict(store.symbol_stats)
    regime_stats = dict(store.regime_stats)
    ingested_ids = set(store.ingested_event_ids)
    added_ids: list[str] = []
    events_seen = 0
    duplicates = 0
    skipped = 0
    warnings: list[str] = []

    for symbol_result in getattr(replay_summary, "symbols", ()) or ():
        symbol = _display(getattr(symbol_result, "symbol", NA)).upper()
        rejected_count = _safe_int(_attr(getattr(symbol_result, "stats", None), "rejected_setup_count"), 0)
        if symbol != NA and rejected_count > 0:
            existing_symbol_stats = symbol_stats.get(symbol, _empty_symbol_stats(symbol))
            symbol_stats[symbol] = _rebuild_stats(
                existing_symbol_stats.model_copy(
                    update={
                        "total_occurrences": existing_symbol_stats.total_occurrences + rejected_count,
                        "rejections": existing_symbol_stats.rejections + rejected_count,
                    }
                )
            ).model_copy(update={"symbol": symbol})

        for trade in getattr(symbol_result, "trades", ()) or ():
            events_seen += 1
            event_id = _replay_event_id(trade, source_id=source_id)
            if event_id in ingested_ids:
                duplicates += 1
                continue
            outcome = _outcome_from_trade(trade)
            if outcome is None:
                skipped += 1
                warnings.append(f"Skipped replay outcome with invalid data for {symbol}.")
                continue
            fingerprint = setup_fingerprint_from_replay_trade(trade)
            key = fingerprint.signature
            setup_stats[key] = _stats_with_outcome(
                setup_stats.get(key, _empty_setup_stats(fingerprint)),
                outcome,
            )
            symbol_stats[symbol] = _stats_with_outcome(
                symbol_stats.get(symbol, _empty_symbol_stats(symbol)),
                outcome,
            ).model_copy(update={"symbol": symbol})

            regime = fingerprint.market_regime
            if regime != NA:
                regime_stats[regime] = _stats_with_outcome(
                    regime_stats.get(regime, _empty_regime_stats(regime)),
                    outcome,
                ).model_copy(update={"regime": regime})

            ingested_ids.add(event_id)
            added_ids.append(event_id)

    updated_store = PerformanceMemoryStore(
        version=MEMORY_VERSION,
        setup_stats=setup_stats,
        symbol_stats=symbol_stats,
        regime_stats=regime_stats,
        ingested_event_ids=_unique_strings((*store.ingested_event_ids, *added_ids)),
        rejected_memory_entries=store.rejected_memory_entries,
        load_warnings=store.load_warnings,
    )
    return ReplayIngestionResult(
        store=updated_store,
        events_seen=events_seen,
        events_added=len(added_ids),
        duplicates_ignored=duplicates,
        skipped_outcomes=skipped,
        warnings=tuple(warnings),
    )


def apply_performance_memory_to_result(
    scan_result: Any,
    store: PerformanceMemoryStore,
    *,
    enabled: bool = True,
    min_confidence: ConfidenceBucket | str = ConfidenceBucket.LOW,
) -> Any:
    if not enabled:
        updated = tuple(_with_disabled_memory(symbol_result) for symbol_result in getattr(scan_result, "results", ()))
        summary = _disabled_summary()
    else:
        updated = tuple(
            apply_performance_memory_to_symbol(
                symbol_result,
                store,
                market_regime=_market_regime_value(getattr(scan_result, "market_regime", None)),
                min_confidence=min_confidence,
            )
            for symbol_result in getattr(scan_result, "results", ())
        )
        summary = performance_memory_summary(store)
    try:
        return scan_result.model_copy(update={"results": updated, "performance_memory_summary": summary})
    except AttributeError:
        return scan_result


def apply_performance_memory_to_symbol(
    symbol_result: Any,
    store: PerformanceMemoryStore,
    *,
    market_regime: str = NA,
    min_confidence: ConfidenceBucket | str = ConfidenceBucket.LOW,
    ) -> Any:
    fingerprint = setup_fingerprint_from_scan(symbol_result, market_regime=market_regime)
    stats = store.setup_stats.get(fingerprint.signature) or _find_similar_setup_stats(store, fingerprint)
    symbol = _display(getattr(symbol_result, "symbol", NA)).upper()
    symbol_stats = store.symbol_stats.get(symbol)
    regime_stats = store.regime_stats.get(fingerprint.market_regime)
    result = performance_memory_result(
        fingerprint=fingerprint,
        stats=stats,
        symbol_stats=symbol_stats,
        regime_stats=regime_stats,
        min_confidence=min_confidence,
    )
    update: dict[str, Any] = {
        "performance_memory": result.model_dump(mode="json"),
        "historical_expectancy": result.historical_expectancy,
        "confidence_bucket": result.confidence_bucket.value,
        "memory_adjustments": result.memory_adjustments,
        "historical_warning": result.historical_warning,
        "expectancy_metrics": {
            "expectancy": result.historical_expectancy,
            "average_r": result.average_r,
            "median_r": result.median_r,
            "tp1_hit_rate": result.tp1_rate,
            "tp2_hit_rate": result.tp2_rate,
            "fills": stats.filled_occurrences if stats is not None else 0,
            "setups": result.historical_samples,
        },
        "historical_match_summary": {
            "matched": stats is not None,
            "condition_key": fingerprint.model_dump(mode="json"),
            "expectancy_metrics": {
                "expectancy": result.historical_expectancy,
                "average_r": result.average_r,
                "median_r": result.median_r,
                "tp1_hit_rate": result.tp1_rate,
                "tp2_hit_rate": result.tp2_rate,
                "fills": stats.filled_occurrences if stats is not None else 0,
                "setups": result.historical_samples,
            },
            "confidence_label": result.confidence_bucket.value,
            "edge_score": result.memory_adjustments.get("edge_score_adjustment", 0),
            "matching_sample_size": result.historical_samples,
            "match_group": fingerprint.signature if stats is not None else NA,
            "warning": result.historical_warning,
        },
    }
    adjusted_quality = _adjust_setup_quality(getattr(symbol_result, "setup_quality", None), result)
    if adjusted_quality is not None:
        update["setup_quality"] = adjusted_quality
    try:
        return symbol_result.model_copy(update=update)
    except AttributeError:
        return symbol_result


def performance_memory_result(
    *,
    fingerprint: SetupFingerprint,
    stats: SetupPerformanceStats | None,
    symbol_stats: SymbolPerformanceStats | None = None,
    regime_stats: RegimePerformanceStats | None = None,
    min_confidence: ConfidenceBucket | str = ConfidenceBucket.LOW,
) -> PerformanceMemoryResult:
    if stats is None:
        return PerformanceMemoryResult(
            fingerprint=fingerprint,
            memory_adjustments=_zero_adjustments("no_historical_samples"),
            historical_warning=INSUFFICIENT_SAMPLE_WARNING,
        )
    bucket = stats.confidence_bucket
    enough = confidence_meets_minimum(bucket, min_confidence) and bucket != ConfidenceBucket.VERY_LOW
    adjustment = _score_adjustment(stats, symbol_stats, regime_stats) if enough else 0
    warning = (
        "Historical memory evidence applied conservatively."
        if adjustment != 0
        else INSUFFICIENT_SAMPLE_WARNING
        if not enough
        else "Historical memory evidence is neutral."
    )
    return PerformanceMemoryResult(
        fingerprint=fingerprint,
        historical_samples=stats.total_occurrences,
        tp1_rate=stats.tp1_rate,
        tp2_rate=stats.tp2_rate,
        average_r=stats.average_r,
        median_r=stats.median_r,
        max_drawdown=stats.max_drawdown,
        average_hold_time=stats.average_hold_time,
        confidence_bucket=bucket,
        similar_setup_performance=_quality_label(stats),
        regime_compatibility=_quality_label(regime_stats) if regime_stats is not None else "unverified",
        symbol_historical_quality=_quality_label(symbol_stats) if symbol_stats is not None else "unverified",
        historical_expectancy=stats.average_r,
        memory_adjustments={
            "edge_score_adjustment": adjustment,
            "readiness_adjustment": _readiness_adjustment(adjustment),
            "portfolio_preference_adjustment": _portfolio_adjustment(adjustment, bucket),
            "positive_cap": POSITIVE_ADJUSTMENT_CAP,
            "negative_cap": NEGATIVE_ADJUSTMENT_CAP,
            "min_confidence_required": _confidence_bucket(min_confidence).value,
            "applied": adjustment != 0,
        },
        historical_warning=warning,
    )


def performance_memory_summary(store: PerformanceMemoryStore) -> dict[str, Any]:
    setup_values = tuple(store.setup_stats.values())
    symbol_values = tuple(store.symbol_stats.values())
    regime_values = tuple(store.regime_stats.values())
    total_samples = sum(stats.total_occurrences for stats in setup_values)
    strongest_setup = _best_stats(setup_values)
    weakest_setup = _worst_stats(setup_values)
    strongest_symbol = _best_stats(symbol_values)
    weakest_symbol = _worst_stats(symbol_values)
    best_regime = _best_stats(regime_values)
    worst_regime = _worst_stats(regime_values)
    return {
        "enabled": True,
        "best_performing_setup_type": _stats_label(strongest_setup),
        "weakest_setup_type": _stats_label(weakest_setup),
        "best_regime_historically": _regime_label(best_regime),
        "worst_regime_historically": _regime_label(worst_regime),
        "strongest_symbols": _symbol_label(strongest_symbol),
        "weakest_symbols": _symbol_label(weakest_symbol),
        "memory_confidence_level": confidence_bucket(total_samples).value,
        "total_historical_samples": total_samples,
        "rejected_memory_entries": store.rejected_memory_entries,
        "warnings": list(store.load_warnings),
        "safety_note": store.safety_note,
    }


def _disabled_summary() -> dict[str, Any]:
    return {
        "enabled": False,
        "best_performing_setup_type": NA,
        "weakest_setup_type": NA,
        "best_regime_historically": NA,
        "worst_regime_historically": NA,
        "strongest_symbols": NA,
        "weakest_symbols": NA,
        "memory_confidence_level": ConfidenceBucket.VERY_LOW.value,
        "total_historical_samples": 0,
        "rejected_memory_entries": 0,
        "warnings": ["Performance memory disabled."],
        "safety_note": MEMORY_SAFETY_NOTE,
    }


def setup_fingerprint_from_scan(symbol_result: Any, *, market_regime: str = NA) -> SetupFingerprint:
    diagnostics = _representative_diagnostics(symbol_result)
    condition_key = condition_key_from_diagnostics(
        symbol=_display(getattr(symbol_result, "symbol", NA)),
        mode=_first_non_na(diagnostics.get("mode"), _first_mode(symbol_result)),
        diagnostics=diagnostics,
        readiness_score=_first_non_na(diagnostics.get("trust_percentage"), _quality_score(symbol_result)),
    )
    return _fingerprint_from_condition_key(
        condition_key,
        direction=_direction_from(symbol_result, diagnostics),
        market_regime=market_regime,
        squeeze_risk=_first_non_na(getattr(symbol_result, "squeeze_risk", NA), diagnostics.get("squeeze_risk")),
        confirmation_strength=_confirmation_strength(diagnostics),
        volatility_regime=_first_non_na(diagnostics.get("volatility_regime"), diagnostics.get("atr_regime")),
        setup_type=_setup_type(symbol_result, diagnostics),
    )


def setup_fingerprint_from_replay_trade(trade: Any, *, market_regime: str = NA) -> SetupFingerprint:
    candidate = getattr(trade, "candidate", None)
    condition_key = getattr(candidate, "condition_key", None)
    if not isinstance(condition_key, EdgeConditionKey):
        condition_key = EdgeConditionKey.model_validate(_attr(candidate, "condition_key") or {})
    return _fingerprint_from_condition_key(
        condition_key,
        direction=_display(getattr(getattr(trade, "direction", None), "value", getattr(trade, "direction", NA))),
        market_regime=market_regime,
        squeeze_risk=NA,
        confirmation_strength=_confirmation_from_candidate(candidate),
        volatility_regime=NA,
        setup_type=f"liquidity_grab_pullback_{_display(getattr(getattr(trade, 'mode', None), 'value', getattr(trade, 'mode', NA)))}",
    )


def _fingerprint_from_condition_key(
    condition_key: EdgeConditionKey,
    *,
    direction: Any,
    market_regime: Any,
    squeeze_risk: Any,
    confirmation_strength: Any,
    volatility_regime: Any,
    setup_type: Any,
) -> SetupFingerprint:
    return SetupFingerprint(
        direction=direction,
        htf_bias=condition_key.htf_direction_alignment,
        market_regime=market_regime,
        derivatives_state=condition_key.derivatives_state,
        crowding=condition_key.crowding_state,
        squeeze_risk=squeeze_risk,
        rr_bucket=condition_key.rr_bucket,
        pullback_quality=condition_key.pullback_quality,
        ob_fvg_quality=condition_key.ob_fvg_quality,
        confirmation_strength=confirmation_strength,
        volatility_regime=volatility_regime,
        symbol_category=_symbol_category(condition_key.symbol),
        mode=condition_key.mode,
        setup_type=setup_type,
    )


def _stats_with_outcome(stats: SetupPerformanceStats, outcome: Mapping[str, Any]) -> SetupPerformanceStats:
    r_multiples = list(stats.r_multiples)
    hold_times = list(stats.hold_times)
    filled = bool(outcome["filled"])
    r_value = outcome["r_multiple"]
    if filled:
        r_multiples.append(r_value)
        hold_times.append(_safe_int(outcome.get("candles_held"), 0))
    updated = stats.model_copy(
        update={
            "total_occurrences": stats.total_occurrences + 1,
            "filled_occurrences": stats.filled_occurrences + (1 if filled else 0),
            "wins": stats.wins + (1 if filled and r_value > 0 else 0),
            "losses": stats.losses + (1 if filled and r_value < 0 else 0),
            "tp1_hits": stats.tp1_hits + (1 if filled and outcome["tp1_hit"] else 0),
            "tp2_hits": stats.tp2_hits + (1 if filled and outcome["tp2_hit"] else 0),
            "rejections": stats.rejections + (1 if outcome["rejected"] else 0),
            "invalidations": stats.invalidations + (1 if outcome["invalidated"] else 0),
            "r_multiples": tuple(r_multiples),
            "hold_times": tuple(hold_times),
        }
    )
    return _rebuild_stats(updated)


def _find_similar_setup_stats(
    store: PerformanceMemoryStore,
    fingerprint: SetupFingerprint,
) -> SetupPerformanceStats | None:
    candidates = [
        stats
        for stats in store.setup_stats.values()
        if _fingerprints_compatible(stats.fingerprint, fingerprint)
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda stats: (stats.total_occurrences, stats.filled_occurrences))


def _fingerprints_compatible(stored: SetupFingerprint, current: SetupFingerprint) -> bool:
    required_fields = (
        "direction",
        "htf_bias",
        "derivatives_state",
        "rr_bucket",
        "pullback_quality",
        "ob_fvg_quality",
        "confirmation_strength",
        "symbol_category",
        "mode",
        "setup_type",
    )
    optional_fields = ("market_regime", "squeeze_risk", "volatility_regime", "crowding")
    for field in required_fields:
        if getattr(stored, field) != getattr(current, field):
            return False
    for field in optional_fields:
        left = getattr(stored, field)
        right = getattr(current, field)
        if left != NA and right != NA and left != right:
            return False
    return True


def _rebuild_stats(stats: SetupPerformanceStats) -> SetupPerformanceStats:
    r_values = tuple(stats.r_multiples)
    hold_times = tuple(stats.hold_times)
    filled = stats.filled_occurrences
    total = stats.total_occurrences
    bucket = confidence_bucket(total)
    data_quality = (
        "verified"
        if bucket in (ConfidenceBucket.HIGH, ConfidenceBucket.VERY_HIGH)
        else "unverified"
        if bucket != ConfidenceBucket.VERY_LOW
        else "insufficient_sample"
    )
    rebuilt = stats.model_copy(
        update={
            "tp1_rate": _rate(stats.tp1_hits, filled),
            "tp2_rate": _rate(stats.tp2_hits, filled),
            "average_r": _mean(r_values),
            "median_r": NA if not r_values else _quantize(Decimal(str(median(r_values)))),
            "max_drawdown": _max_drawdown(r_values),
            "average_hold_time": _mean(tuple(Decimal(value) for value in hold_times)),
            "rejection_frequency": _rate(stats.rejections, total),
            "invalidation_frequency": _rate(stats.invalidations, total),
            "confidence_bucket": bucket,
            "data_quality": data_quality,
        }
    )
    return stats.__class__.model_validate(rebuilt.model_dump())


def _outcome_from_trade(trade: Any) -> dict[str, Any] | None:
    try:
        filled = bool(getattr(trade, "filled"))
        r_value = _quantize(_decimal_from(getattr(trade, "r_multiple"), "r_multiple"))
    except (AttributeError, ValueError):
        return None
    outcome_text = _display(getattr(getattr(trade, "outcome", None), "value", getattr(trade, "outcome", NA))).lower()
    return {
        "filled": filled,
        "tp1_hit": bool(getattr(trade, "tp1_hit", False)),
        "tp2_hit": bool(getattr(trade, "tp2_hit", False)),
        "r_multiple": r_value,
        "candles_held": _safe_int(getattr(trade, "candles_held", 0), 0),
        "invalidated": outcome_text == "invalidated",
        "rejected": outcome_text in {"missed_entry", "not_filled", "expired"},
    }


def _score_adjustment(
    stats: SetupPerformanceStats,
    symbol_stats: SymbolPerformanceStats | None,
    regime_stats: RegimePerformanceStats | None,
) -> int:
    expectancy = _decimal_or_zero(stats.average_r)
    tp1_rate = _decimal_or_zero(stats.tp1_rate)
    drawdown = _decimal_or_zero(stats.max_drawdown)
    raw = Decimal("0")
    if expectancy > 0:
        raw += min(Decimal("7"), expectancy * Decimal("4"))
    else:
        raw += max(Decimal("-10"), expectancy * Decimal("5"))
    if tp1_rate >= 60:
        raw += Decimal("2")
    elif tp1_rate < 40 and stats.filled_occurrences > 0:
        raw -= Decimal("3")
    if expectancy >= Decimal("1.5") and tp1_rate >= 60:
        raw += Decimal("1")
    if drawdown > Decimal("3"):
        raw -= Decimal("2")
    raw += _context_adjustment(symbol_stats, weight=Decimal("1.5"))
    raw += _context_adjustment(regime_stats, weight=Decimal("1.5"))
    bounded = min(Decimal(POSITIVE_ADJUSTMENT_CAP), max(Decimal(NEGATIVE_ADJUSTMENT_CAP), raw))
    return int(bounded.to_integral_value(rounding="ROUND_HALF_UP"))


def _context_adjustment(stats: SetupPerformanceStats | None, *, weight: Decimal) -> Decimal:
    if stats is None or stats.confidence_bucket == ConfidenceBucket.VERY_LOW or stats.average_r == NA:
        return Decimal("0")
    expectancy = _decimal_or_zero(stats.average_r)
    if expectancy > 0:
        return min(weight, expectancy)
    return max(-weight, expectancy)


def _adjust_setup_quality(
    quality: SetupQualityResult | None,
    memory_result: PerformanceMemoryResult,
) -> SetupQualityResult | None:
    if quality is None or not getattr(quality, "is_evaluated", False):
        return None
    adjustment = _safe_int(memory_result.memory_adjustments.get("edge_score_adjustment"), 0)
    if adjustment == 0:
        return quality
    edge_score = _bounded_score(quality.profitability_edge_score + adjustment)
    readiness_adjustment = _safe_int(memory_result.memory_adjustments.get("readiness_adjustment"), 0)
    quality_score = _bounded_score(quality.quality_score + readiness_adjustment)
    tradeability_score = _bounded_score(quality.tradeability_score + readiness_adjustment)
    strongest = quality.strongest_factors
    weakest = quality.weakest_factors
    if adjustment > 0:
        strongest = _unique_strings((*strongest, "positive performance memory"))
    else:
        weakest = _unique_strings((*weakest, "weak performance memory"))
    return quality.model_copy(
        update={
            "quality_score": quality_score,
            "tradeability_score": tradeability_score,
            "profitability_edge_score": edge_score,
            "strongest_factors": strongest,
            "weakest_factors": weakest,
            "decision_reason": f"{quality.decision_reason} Performance memory: {memory_result.historical_warning}",
        }
    )


def _with_disabled_memory(symbol_result: Any) -> Any:
    disabled = PerformanceMemoryResult(
        enabled=False,
        memory_adjustments=_zero_adjustments("disabled"),
        historical_warning="Performance memory disabled.",
    )
    try:
        return symbol_result.model_copy(
            update={
                "performance_memory": disabled.model_dump(mode="json"),
                "historical_expectancy": NA,
                "confidence_bucket": ConfidenceBucket.VERY_LOW.value,
                "memory_adjustments": disabled.memory_adjustments,
                "historical_warning": "Performance memory disabled.",
            }
        )
    except AttributeError:
        return symbol_result


def _zero_adjustments(reason: str) -> dict[str, Any]:
    return {
        "edge_score_adjustment": 0,
        "readiness_adjustment": 0,
        "portfolio_preference_adjustment": 0,
        "positive_cap": POSITIVE_ADJUSTMENT_CAP,
        "negative_cap": NEGATIVE_ADJUSTMENT_CAP,
        "applied": False,
        "reason": reason,
    }


def _readiness_adjustment(adjustment: int) -> int:
    if adjustment == 0:
        return 0
    return int(max(-5, min(5, round(adjustment / 2))))


def _portfolio_adjustment(adjustment: int, bucket: ConfidenceBucket) -> int:
    if bucket == ConfidenceBucket.VERY_LOW:
        return 0
    return int(max(-5, min(5, round(adjustment / 2))))


def _quality_label(stats: SetupPerformanceStats | None) -> str:
    if stats is None:
        return "unverified"
    if stats.confidence_bucket == ConfidenceBucket.VERY_LOW:
        return "insufficient_sample"
    expectancy = _decimal_or_zero(stats.average_r)
    if expectancy > Decimal("0.25"):
        return "historically_strong"
    if expectancy < Decimal("0"):
        return "historically_weak"
    return "mixed"


def _best_stats(values: Sequence[SetupPerformanceStats]) -> SetupPerformanceStats | None:
    candidates = tuple(item for item in values if item.average_r != NA and item.total_occurrences >= 10)
    if not candidates:
        return None
    return max(candidates, key=lambda item: (_decimal_or_zero(item.average_r), item.total_occurrences))


def _worst_stats(values: Sequence[SetupPerformanceStats]) -> SetupPerformanceStats | None:
    candidates = tuple(item for item in values if item.average_r != NA and item.total_occurrences >= 10)
    if not candidates:
        return None
    return min(candidates, key=lambda item: (_decimal_or_zero(item.average_r), -item.total_occurrences))


def _stats_label(stats: SetupPerformanceStats | None) -> str:
    if stats is None:
        return NA
    return f"{stats.fingerprint.label} ({_display(stats.average_r)}R, {stats.total_occurrences} samples)"


def _symbol_label(stats: SetupPerformanceStats | None) -> str:
    if stats is None:
        return NA
    return f"{_display(getattr(stats, 'symbol', NA))} ({_display(stats.average_r)}R, {stats.total_occurrences} samples)"


def _regime_label(stats: SetupPerformanceStats | None) -> str:
    if stats is None:
        return NA
    return f"{_display(getattr(stats, 'regime', NA))} ({_display(stats.average_r)}R, {stats.total_occurrences} samples)"


def _replay_event_id(trade: Any, *, source_id: str | None) -> str:
    candidate = getattr(trade, "candidate", None)
    parts = (
        source_id or "replay",
        _display(getattr(trade, "symbol", NA)),
        _display(getattr(getattr(trade, "mode", None), "value", getattr(trade, "mode", NA))),
        _display(getattr(getattr(trade, "direction", None), "value", getattr(trade, "direction", NA))),
        _display(_attr(candidate, "detected_at_timestamp")),
        _display(_attr(candidate, "detected_at_index")),
        _display(_attr(candidate, "entry")),
        _display(_attr(candidate, "stop")),
        _display(_attr(candidate, "tp2")),
        _display(getattr(getattr(trade, "outcome", None), "value", getattr(trade, "outcome", NA))),
    )
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


def _empty_setup_stats(fingerprint: SetupFingerprint) -> SetupPerformanceStats:
    return SetupPerformanceStats(fingerprint=fingerprint)


def _empty_symbol_stats(symbol: str) -> SymbolPerformanceStats:
    return SymbolPerformanceStats(symbol=symbol, fingerprint=SetupFingerprint(symbol_category=_symbol_category(symbol)))


def _empty_regime_stats(regime: str) -> RegimePerformanceStats:
    return RegimePerformanceStats(regime=regime, fingerprint=SetupFingerprint(market_regime=regime))


def _representative_diagnostics(symbol_result: Any) -> Mapping[str, Any]:
    diagnostics = getattr(symbol_result, "strategy_diagnostics", {}) or {}
    if not isinstance(diagnostics, Mapping):
        return {}
    for values_name in ("valid_strategy_modes", "rejected_strategy_modes"):
        for mode in getattr(symbol_result, values_name, ()) or ():
            item = diagnostics.get(str(mode))
            if isinstance(item, Mapping):
                return item
    for mode in ("challenge", "swing", "scalp"):
        item = diagnostics.get(mode)
        if isinstance(item, Mapping):
            return item
    for item in diagnostics.values():
        if isinstance(item, Mapping):
            return item
    return {}


def _first_mode(symbol_result: Any) -> str:
    for values_name in ("valid_strategy_modes", "rejected_strategy_modes"):
        values = getattr(symbol_result, values_name, ()) or ()
        if values:
            return _display(values[0])
    diagnostics = getattr(symbol_result, "strategy_diagnostics", {}) or {}
    if isinstance(diagnostics, Mapping) and diagnostics:
        return _display(next(iter(diagnostics.keys())))
    return NA


def _direction_from(symbol_result: Any, diagnostics: Mapping[str, Any]) -> str:
    trade_idea = getattr(symbol_result, "trade_idea", None)
    for value in (
        _attr(trade_idea, "direction"),
        diagnostics.get("direction"),
        diagnostics.get("bias"),
        diagnostics.get("setup_direction"),
    ):
        normalized = _direction_text(value)
        if normalized != NA:
            return normalized
    for key in ("sweep_diagnostics", "bos_choch_diagnostics", "structure_shift_diagnostics"):
        text = _display(diagnostics.get(key)).lower()
        if "bullish" in text:
            return "long"
        if "bearish" in text:
            return "short"
    return NA


def _confirmation_strength(diagnostics: Mapping[str, Any]) -> str:
    status = _display(diagnostics.get("confirmation_structure_shift_status"))
    if status != "passed":
        return "failed" if status == "failed" else NA
    trust = _optional_decimal(diagnostics.get("trust_percentage"))
    if trust != NA and trust >= Decimal("85"):
        return "strong_confirmation"
    if trust != NA and trust < Decimal("65"):
        return "weak_confirmation"
    return "confirmed"


def _confirmation_from_candidate(candidate: Any) -> str:
    trust = _optional_decimal(_attr(candidate, "trust_percentage"))
    if trust != NA and trust >= Decimal("85"):
        return "strong_confirmation"
    if trust != NA and trust < Decimal("65"):
        return "weak_confirmation"
    return "confirmed"


def _setup_type(symbol_result: Any, diagnostics: Mapping[str, Any]) -> str:
    trade_idea = getattr(symbol_result, "trade_idea", None)
    setup_type = _first_non_na(_attr(trade_idea, "setup_type"), diagnostics.get("setup_type"))
    if setup_type != NA:
        return setup_type
    mode = _first_non_na(diagnostics.get("mode"), _first_mode(symbol_result))
    return f"liquidity_grab_pullback_{mode}" if mode != NA else NA


def _symbol_category(symbol: str) -> str:
    base = _base_asset(symbol)
    if base in {"BTC", "WBTC"}:
        return "BTC_MAJOR"
    if base in {"ETH", "ETC", "LDO", "ENS"}:
        return "ETH_BETA"
    if base in {"SOL", "JUP", "JTO", "PYTH", "RAY"}:
        return "SOL_BETA"
    if base in {"DOGE", "SHIB", "PEPE", "WIF", "BONK", "FLOKI", "MEME", "BOME", "PNUT", "TRUMP"}:
        return "MEME"
    if base in {"TAO", "FET", "RNDR", "RENDER", "AIXBT", "VIRTUAL", "GRASS", "AI"}:
        return "AI"
    if base in {"ONDO", "PENDLE", "OM", "POLYX", "CFG"}:
        return "RWA"
    if base in {"UNI", "AAVE", "CRV", "MKR", "COMP", "SNX", "DYDX", "GMX", "CAKE", "RUNE", "ENA"}:
        return "DEFI"
    if base in {"ADA", "AVAX", "DOT", "NEAR", "ATOM", "SUI", "APT", "SEI", "INJ", "TON", "MATIC", "POL", "OP", "ARB"}:
        return "L1_L2"
    return "UNKNOWN"


def _base_asset(symbol: str) -> str:
    text = _display(symbol).upper()
    for prefix in ("1000000", "10000", "1000"):
        if text.startswith(prefix):
            text = text.removeprefix(prefix)
    for suffix in ("USDT", "USDC", "USD", "PERP"):
        if text.endswith(suffix):
            return text.removesuffix(suffix)
    return text


def _market_regime_value(market_regime: Any) -> str:
    if market_regime is None:
        return NA
    state = getattr(market_regime, "state", market_regime)
    value = _display(getattr(state, "value", state))
    return NA if value in {"DATA_INCOMPLETE", "N/A"} else value


def _quality_score(symbol_result: Any) -> Any:
    quality = getattr(symbol_result, "setup_quality", None)
    return _attr(quality, "quality_score")


def _bounded_score(value: int) -> int:
    return max(0, min(100, int(value)))


def _direction_text(value: Any) -> str:
    text = _display(value).lower()
    if text in {"long", "bullish"}:
        return "long"
    if text in {"short", "bearish"}:
        return "short"
    return NA


def _mapping_items(value: Any) -> tuple[tuple[str, Any], ...]:
    if not isinstance(value, Mapping):
        return ()
    return tuple((str(key), item) for key, item in value.items())


def _rate(numerator: int, denominator: int) -> MaybeDecimal:
    if denominator <= 0:
        return NA
    return (Decimal(numerator) / Decimal(denominator) * Decimal("100")).quantize(PERCENT_QUANT)


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
        return _quantize(_decimal_from(value, "optional decimal"))
    except ValueError:
        return NA


def _decimal_or_zero(value: Any) -> Decimal:
    decimal = _optional_decimal(value)
    return Decimal("0") if decimal == NA else decimal


def _confidence_bucket(value: ConfidenceBucket | str) -> ConfidenceBucket:
    if isinstance(value, ConfidenceBucket):
        return value
    text = _display(value).upper()
    return ConfidenceBucket.__members__.get(text, ConfidenceBucket.LOW)


def _first_non_na(*values: Any) -> Any:
    for value in values:
        if _display(value) != NA:
            return value
    return NA


def _attr(source: Any, name: str | None = None) -> Any:
    if source is None:
        return NA
    if name is None:
        return source
    if isinstance(source, Mapping):
        return source.get(name, NA)
    return getattr(source, name, NA)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _clean_error(exc: Exception) -> str:
    text = str(exc).splitlines()[0].strip()
    return text or exc.__class__.__name__


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        text = _display(value)
        if text != NA and text not in output:
            output.append(text)
    return tuple(output)


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid performance memory decimal at {path}: {value!r}") from exc
    if not decimal.is_finite():
        raise ValueError(f"Invalid performance memory decimal at {path}: {value!r}")
    return decimal


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _display(value: Any) -> str:
    if value is None or value == "" or value == NA:
        return NA
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value).strip() or NA


__all__ = [
    "DEFAULT_MEMORY_PATH",
    "INSUFFICIENT_SAMPLE_WARNING",
    "NEGATIVE_ADJUSTMENT_CAP",
    "POSITIVE_ADJUSTMENT_CAP",
    "ConfidenceBucket",
    "PerformanceMemoryResult",
    "PerformanceMemoryStore",
    "RegimePerformanceStats",
    "ReplayIngestionResult",
    "SetupFingerprint",
    "SetupPerformanceStats",
    "SymbolPerformanceStats",
    "apply_performance_memory_to_result",
    "apply_performance_memory_to_symbol",
    "confidence_bucket",
    "confidence_meets_minimum",
    "empty_performance_memory",
    "ingest_replay_summary",
    "load_performance_memory",
    "performance_memory_result",
    "performance_memory_summary",
    "reset_performance_memory",
    "save_performance_memory",
    "setup_fingerprint_from_replay_trade",
    "setup_fingerprint_from_scan",
]
