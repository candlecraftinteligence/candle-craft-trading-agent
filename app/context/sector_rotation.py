from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import Enum
from statistics import median
from typing import Any, Final

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data.candle_integrity import (
    closed_candles_as_of,
    normalize_utc_timestamp,
    timeframe_duration,
)
from app.data.dtos import NA
from app.context.sector_taxonomy import (
    SECTOR_TAXONOMY_VERSION,
    SectorAssetType,
    SectorClassification,
    classify_sector,
)


SECTOR_ROTATION_SCHEMA_VERSION: Final = "cci_sector_rotation_v1"
SECTOR_USAGE: Final = "research_only"
SECTOR_RETURN_HORIZONS: Final = ("15m", "1h", "4h", "24h")
SECTOR_RANKING_HORIZON: Final = "4h"
DEFAULT_MIN_VERIFIED_CONSTITUENTS: Final = 3
DEFAULT_MIN_COVERAGE_PCT: Final = Decimal("60")
DEFAULT_BROAD_BREADTH_PCT: Final = Decimal("60")
DEFAULT_FRESHNESS_INTERVALS: Final = 2
OUTPUT_QUANT: Final = Decimal("0.00000001")


class SectorMemberStatus(str, Enum):
    VERIFIED = "VERIFIED"
    UNAVAILABLE = "UNAVAILABLE"
    UNCLASSIFIED = "UNCLASSIFIED"
    NON_DIRECTIONAL = "NON_DIRECTIONAL"
    BENCHMARK_ONLY = "BENCHMARK_ONLY"
    ERROR = "ERROR"


class SectorAggregateStatus(str, Enum):
    VERIFIED = "VERIFIED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class SectorSnapshotStatus(str, Enum):
    VERIFIED = "VERIFIED"
    PARTIAL = "PARTIAL"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    ERROR = "ERROR"


class SectorContextStatus(str, Enum):
    VERIFIED = "VERIFIED"
    STALE = "STALE"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    UNCLASSIFIED = "UNCLASSIFIED"
    NON_DIRECTIONAL = "NON_DIRECTIONAL"
    BENCHMARK_ONLY = "BENCHMARK_ONLY"
    UNAVAILABLE = "UNAVAILABLE"
    ERROR = "ERROR"


class SectorRotationState(str, Enum):
    OUTPERFORMING_BROAD = "OUTPERFORMING_BROAD"
    OUTPERFORMING_NARROW = "OUTPERFORMING_NARROW"
    UNDERPERFORMING_BROAD = "UNDERPERFORMING_BROAD"
    UNDERPERFORMING_NARROW = "UNDERPERFORMING_NARROW"
    MIXED = "MIXED"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class SectorMemberFeature(BaseModel):
    symbol: str
    sector: str
    taxonomy_version: str = SECTOR_TAXONOMY_VERSION
    asset_type: SectorAssetType
    status: SectorMemberStatus
    observed_at: datetime | None = None
    source_timeframe: str | None = None
    return_15m_pct: Decimal | None = None
    return_1h_pct: Decimal | None = None
    return_4h_pct: Decimal | None = None
    return_24h_pct: Decimal | None = None
    structure_state: str | None = None
    technical_valid: bool = False
    unavailable_reason: str | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _normalize_observed_at(cls, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        return normalize_utc_timestamp(value, field_name="sector_member_observed_at")

    def return_for(self, horizon: str) -> Decimal | None:
        return getattr(self, f"return_{horizon}_pct", None)


class SectorHorizonMetrics(BaseModel):
    horizon: str
    verified_count: int = Field(ge=0)
    coverage_pct: Decimal = Field(ge=0, le=100)
    equal_weight_return_pct: Decimal | None = None
    median_return_pct: Decimal | None = None
    positive_breadth_pct: Decimal | None = None
    negative_breadth_pct: Decimal | None = None
    relative_strength_vs_btc_pct_points: Decimal | None = None

    model_config = ConfigDict(frozen=True)


class SectorAggregate(BaseModel):
    sector: str
    status: SectorAggregateStatus
    constituent_count: int = Field(ge=0)
    verified_constituent_count: int = Field(ge=0)
    unavailable_constituent_count: int = Field(ge=0)
    coverage_pct: Decimal = Field(ge=0, le=100)
    observed_at: datetime | None = None
    observation_span_seconds: float | None = Field(default=None, ge=0)
    horizons: tuple[SectorHorizonMetrics, ...] = ()
    structure_verified_count: int = Field(default=0, ge=0)
    bullish_structure_pct: Decimal | None = None
    bearish_structure_pct: Decimal | None = None
    neutral_structure_pct: Decimal | None = None
    top_constituent_4h: str | None = None
    top_constituent_return_4h_pct: Decimal | None = None
    bottom_constituent_4h: str | None = None
    bottom_constituent_return_4h_pct: Decimal | None = None
    sector_rank: int | None = Field(default=None, ge=1)
    rotation_state: SectorRotationState = SectorRotationState.INSUFFICIENT_DATA

    model_config = ConfigDict(frozen=True)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _normalize_observed_at(cls, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        return normalize_utc_timestamp(value, field_name="sector_observed_at")

    def metrics_for(self, horizon: str) -> SectorHorizonMetrics | None:
        return next((item for item in self.horizons if item.horizon == horizon), None)


class SectorRotationSnapshot(BaseModel):
    schema_version: str = SECTOR_ROTATION_SCHEMA_VERSION
    taxonomy_version: str = SECTOR_TAXONOMY_VERSION
    usage: str = SECTOR_USAGE
    status: SectorSnapshotStatus
    generated_at: datetime
    observed_at: datetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    freshness_seconds: float = Field(gt=0)
    ranking_horizon: str = SECTOR_RANKING_HORIZON
    minimum_verified_constituents: int = Field(ge=1)
    minimum_coverage_pct: Decimal = Field(ge=0, le=100)
    intended_universe_count: int = Field(ge=0)
    directional_classified_count: int = Field(ge=0)
    unclassified_count: int = Field(ge=0)
    non_directional_count: int = Field(ge=0)
    benchmark_count: int = Field(ge=0)
    unavailable_member_count: int = Field(ge=0)
    benchmark: SectorMemberFeature | None = None
    sectors: tuple[SectorAggregate, ...] = ()
    verified_sector_count: int = Field(default=0, ge=0)
    ranked_sector_count: int = Field(default=0, ge=0)
    unavailable_reason: str | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("generated_at", "observed_at", mode="before")
    @classmethod
    def _normalize_timestamps(cls, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        return normalize_utc_timestamp(value, field_name="sector_snapshot_timestamp")

    def sector(self, name: str) -> SectorAggregate | None:
        return next((item for item in self.sectors if item.sector == name), None)

    @classmethod
    def error(
        cls,
        *,
        universe_symbols: Sequence[str],
        generated_at: datetime,
        reason: str,
        freshness_seconds: float = 1800.0,
    ) -> SectorRotationSnapshot:
        classifications = tuple(classify_sector(symbol) for symbol in universe_symbols)
        return cls(
            status=SectorSnapshotStatus.ERROR,
            generated_at=generated_at,
            freshness_seconds=freshness_seconds,
            minimum_verified_constituents=DEFAULT_MIN_VERIFIED_CONSTITUENTS,
            minimum_coverage_pct=DEFAULT_MIN_COVERAGE_PCT,
            intended_universe_count=len(classifications),
            directional_classified_count=_classification_count(
                classifications, SectorAssetType.DIRECTIONAL
            ),
            unclassified_count=_classification_count(
                classifications, SectorAssetType.UNCLASSIFIED
            ),
            non_directional_count=_classification_count(
                classifications, SectorAssetType.NON_DIRECTIONAL
            ),
            benchmark_count=_classification_count(
                classifications, SectorAssetType.BENCHMARK_ONLY
            ),
            unavailable_member_count=len(classifications),
            unavailable_reason=reason,
        )


class SectorRotationContext(BaseModel):
    schema_version: str = SECTOR_ROTATION_SCHEMA_VERSION
    usage: str = SECTOR_USAGE
    taxonomy_version: str = SECTOR_TAXONOMY_VERSION
    sector: str
    status: SectorContextStatus
    observed_at: datetime | None = None
    age_seconds: float | None = Field(default=None, ge=0)
    sector_rank: int | None = Field(default=None, ge=1)
    ranked_sector_count: int = Field(default=0, ge=0)
    constituent_count: int = Field(default=0, ge=0)
    verified_constituent_count: int = Field(default=0, ge=0)
    coverage_pct: Decimal | None = Field(default=None, ge=0, le=100)
    median_return_4h_pct: Decimal | None = None
    relative_strength_vs_btc_4h_pct_points: Decimal | None = None
    positive_breadth_4h_pct: Decimal | None = None
    bullish_structure_pct: Decimal | None = None
    rotation_state: SectorRotationState = SectorRotationState.INSUFFICIENT_DATA
    member_return_15m_pct: Decimal | None = None
    member_return_1h_pct: Decimal | None = None
    member_return_4h_pct: Decimal | None = None
    member_return_24h_pct: Decimal | None = None
    member_relative_return_vs_btc_4h_pct_points: Decimal | None = None
    member_structure_state: str | None = None
    reason: str | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("observed_at", mode="before")
    @classmethod
    def _normalize_observed_at(cls, value: Any) -> datetime | None:
        if value in (None, ""):
            return None
        return normalize_utc_timestamp(value, field_name="sector_context_observed_at")

    def display_text(self) -> str:
        if self.status not in (SectorContextStatus.VERIFIED, SectorContextStatus.STALE):
            return f"{self.sector} {self.status.value} (research-only)"
        metrics: list[str] = [self.sector]
        if self.sector_rank is not None and self.ranked_sector_count:
            metrics.append(f"rank {self.sector_rank}/{self.ranked_sector_count}")
        if self.median_return_4h_pct is not None:
            metrics.append(f"median 4h {_signed(self.median_return_4h_pct)}%")
        if self.relative_strength_vs_btc_4h_pct_points is not None:
            metrics.append(
                f"vs BTC {_signed(self.relative_strength_vs_btc_4h_pct_points)}pp"
            )
        if self.positive_breadth_4h_pct is not None:
            metrics.append(f"positive breadth {_plain(self.positive_breadth_4h_pct)}%")
        metrics.append("research-only")
        return ", ".join(metrics)


def build_sector_member_feature(
    *,
    symbol: str,
    candles: Sequence[Any],
    timeframe: str,
    decision_timestamp: datetime,
    structure_state: Any = None,
    technical_valid: bool = False,
    classification: SectorClassification | None = None,
) -> SectorMemberFeature:
    classification = classification or classify_sector(symbol)
    status_by_asset_type = {
        SectorAssetType.BENCHMARK_ONLY: SectorMemberStatus.BENCHMARK_ONLY,
        SectorAssetType.NON_DIRECTIONAL: SectorMemberStatus.NON_DIRECTIONAL,
        SectorAssetType.UNCLASSIFIED: SectorMemberStatus.UNCLASSIFIED,
    }
    if classification.asset_type != SectorAssetType.DIRECTIONAL and classification.asset_type != SectorAssetType.BENCHMARK_ONLY:
        return SectorMemberFeature(
            symbol=classification.symbol,
            sector=classification.primary_sector,
            asset_type=classification.asset_type,
            status=status_by_asset_type[classification.asset_type],
            unavailable_reason=classification.exclusion_reason,
        )
    try:
        normalized_timeframe = str(timeframe).strip().lower()
        source_duration = timeframe_duration(normalized_timeframe)
        window = closed_candles_as_of(
            candles,
            timeframe=normalized_timeframe,
            decision_timestamp=decision_timestamp,
            minimum_closed_history=1,
        )
        returns = _returns_from_closed_window(window.timeline, source_duration)
        usable_returns = tuple(value for value in returns.values() if value is not None)
        member_status = (
            SectorMemberStatus.BENCHMARK_ONLY
            if classification.asset_type == SectorAssetType.BENCHMARK_ONLY
            else SectorMemberStatus.VERIFIED
            if usable_returns
            else SectorMemberStatus.UNAVAILABLE
        )
        normalized_structure = str(structure_state).strip().lower()
        if normalized_structure not in ("bullish", "bearish", "neutral"):
            normalized_structure = None
        return SectorMemberFeature(
            symbol=classification.symbol,
            sector=classification.primary_sector,
            asset_type=classification.asset_type,
            status=member_status,
            observed_at=window.timeline[-1].close_timestamp,
            source_timeframe=normalized_timeframe,
            return_15m_pct=returns["15m"],
            return_1h_pct=returns["1h"],
            return_4h_pct=returns["4h"],
            return_24h_pct=returns["24h"],
            structure_state=normalized_structure,
            technical_valid=bool(technical_valid and normalized_structure is not None),
            unavailable_reason=None if usable_returns else "no_exact_supported_return_horizon",
        )
    except Exception as exc:
        return SectorMemberFeature(
            symbol=classification.symbol,
            sector=classification.primary_sector,
            asset_type=classification.asset_type,
            status=SectorMemberStatus.ERROR,
            source_timeframe=str(timeframe).strip().lower() or None,
            unavailable_reason=_clean_reason(exc),
        )


class SectorRotationEngine:
    def __init__(
        self,
        *,
        minimum_verified_constituents: int = DEFAULT_MIN_VERIFIED_CONSTITUENTS,
        minimum_coverage_pct: Decimal = DEFAULT_MIN_COVERAGE_PCT,
        broad_breadth_pct: Decimal = DEFAULT_BROAD_BREADTH_PCT,
        freshness_intervals: int = DEFAULT_FRESHNESS_INTERVALS,
    ) -> None:
        if minimum_verified_constituents < 1:
            raise ValueError("minimum_verified_constituents must be at least 1")
        if not Decimal("0") <= minimum_coverage_pct <= Decimal("100"):
            raise ValueError("minimum_coverage_pct must be between 0 and 100")
        if not Decimal("0") <= broad_breadth_pct <= Decimal("100"):
            raise ValueError("broad_breadth_pct must be between 0 and 100")
        if freshness_intervals < 1:
            raise ValueError("freshness_intervals must be at least 1")
        self.minimum_verified_constituents = minimum_verified_constituents
        self.minimum_coverage_pct = _quantize(minimum_coverage_pct)
        self.broad_breadth_pct = _quantize(broad_breadth_pct)
        self.freshness_intervals = freshness_intervals

    def build_snapshot(
        self,
        *,
        universe_symbols: Sequence[str],
        member_features: Sequence[SectorMemberFeature],
        generated_at: datetime,
    ) -> SectorRotationSnapshot:
        generated_utc = normalize_utc_timestamp(
            generated_at, field_name="sector_snapshot_generated_at"
        )
        classifications = tuple(classify_sector(symbol) for symbol in universe_symbols)
        feature_by_symbol = {feature.symbol: feature for feature in member_features}
        benchmark = next(
            (
                feature_by_symbol.get(classification.symbol)
                for classification in classifications
                if classification.asset_type == SectorAssetType.BENCHMARK_ONLY
            ),
            None,
        )
        benchmark_return = (
            benchmark.return_4h_pct
            if benchmark is not None
            and benchmark.status == SectorMemberStatus.BENCHMARK_ONLY
            else None
        )
        sectors = sorted(
            {
                classification.primary_sector
                for classification in classifications
                if classification.asset_type == SectorAssetType.DIRECTIONAL
            }
        )
        aggregates = tuple(
            self._aggregate_sector(
                sector=sector,
                intended_classifications=classifications,
                feature_by_symbol=feature_by_symbol,
                benchmark=benchmark,
            )
            for sector in sectors
        )
        ranked = self._rank_aggregates(aggregates, benchmark_return=benchmark_return)
        verified_sector_count = sum(
            1 for aggregate in ranked if aggregate.status == SectorAggregateStatus.VERIFIED
        )
        ranked_sector_count = sum(1 for aggregate in ranked if aggregate.sector_rank is not None)
        observed_values = tuple(
            aggregate.observed_at for aggregate in ranked if aggregate.observed_at is not None
        )
        observed_at = min(observed_values) if observed_values else None
        freshness_seconds = _snapshot_freshness_seconds(
            member_features, intervals=self.freshness_intervals
        )
        if verified_sector_count and benchmark_return is not None:
            status = SectorSnapshotStatus.VERIFIED
        elif verified_sector_count:
            status = SectorSnapshotStatus.PARTIAL
        else:
            status = SectorSnapshotStatus.INSUFFICIENT_DATA
        available_directional = sum(
            1
            for classification in classifications
            if classification.asset_type == SectorAssetType.DIRECTIONAL
            and feature_by_symbol.get(classification.symbol) is not None
            and feature_by_symbol[classification.symbol].status == SectorMemberStatus.VERIFIED
        )
        directional_count = _classification_count(
            classifications, SectorAssetType.DIRECTIONAL
        )
        return SectorRotationSnapshot(
            status=status,
            generated_at=generated_utc,
            observed_at=observed_at,
            age_seconds=_age_seconds(generated_utc, observed_at),
            freshness_seconds=freshness_seconds,
            minimum_verified_constituents=self.minimum_verified_constituents,
            minimum_coverage_pct=self.minimum_coverage_pct,
            intended_universe_count=len(classifications),
            directional_classified_count=directional_count,
            unclassified_count=_classification_count(
                classifications, SectorAssetType.UNCLASSIFIED
            ),
            non_directional_count=_classification_count(
                classifications, SectorAssetType.NON_DIRECTIONAL
            ),
            benchmark_count=_classification_count(
                classifications, SectorAssetType.BENCHMARK_ONLY
            ),
            unavailable_member_count=max(directional_count - available_directional, 0),
            benchmark=benchmark,
            sectors=ranked,
            verified_sector_count=verified_sector_count,
            ranked_sector_count=ranked_sector_count,
            unavailable_reason=(
                "btc_benchmark_4h_unavailable" if benchmark_return is None else None
            ),
        )

    def _aggregate_sector(
        self,
        *,
        sector: str,
        intended_classifications: Sequence[SectorClassification],
        feature_by_symbol: Mapping[str, SectorMemberFeature],
        benchmark: SectorMemberFeature | None,
    ) -> SectorAggregate:
        symbols = tuple(
            classification.symbol
            for classification in intended_classifications
            if classification.asset_type == SectorAssetType.DIRECTIONAL
            and classification.primary_sector == sector
        )
        features = tuple(
            feature_by_symbol[symbol]
            for symbol in symbols
            if symbol in feature_by_symbol
            and feature_by_symbol[symbol].status == SectorMemberStatus.VERIFIED
        )
        benchmark_returns = {
            horizon: benchmark.return_for(horizon) if benchmark is not None else None
            for horizon in SECTOR_RETURN_HORIZONS
        }
        horizons = tuple(
            _horizon_metrics(
                horizon=horizon,
                constituent_count=len(symbols),
                features=features,
                benchmark_return=benchmark_returns[horizon],
            )
            for horizon in SECTOR_RETURN_HORIZONS
        )
        ranking_metrics = next(
            item for item in horizons if item.horizon == SECTOR_RANKING_HORIZON
        )
        status = (
            SectorAggregateStatus.VERIFIED
            if ranking_metrics.verified_count >= self.minimum_verified_constituents
            and ranking_metrics.coverage_pct >= self.minimum_coverage_pct
            else SectorAggregateStatus.INSUFFICIENT_DATA
        )
        structure_features = tuple(
            feature
            for feature in features
            if feature.technical_valid
            and feature.structure_state in ("bullish", "bearish", "neutral")
        )
        bullish_count = sum(
            1 for feature in structure_features if feature.structure_state == "bullish"
        )
        bearish_count = sum(
            1 for feature in structure_features if feature.structure_state == "bearish"
        )
        neutral_count = sum(
            1 for feature in structure_features if feature.structure_state == "neutral"
        )
        observed_values = tuple(
            feature.observed_at
            for feature in features
            if feature.return_4h_pct is not None and feature.observed_at is not None
        )
        returns_4h = tuple(
            (feature.symbol, feature.return_4h_pct)
            for feature in features
            if feature.return_4h_pct is not None
        )
        top = max(returns_4h, key=lambda item: (item[1], item[0])) if returns_4h else None
        bottom = min(returns_4h, key=lambda item: (item[1], item[0])) if returns_4h else None
        return SectorAggregate(
            sector=sector,
            status=status,
            constituent_count=len(symbols),
            verified_constituent_count=ranking_metrics.verified_count,
            unavailable_constituent_count=max(
                len(symbols) - ranking_metrics.verified_count, 0
            ),
            coverage_pct=ranking_metrics.coverage_pct,
            observed_at=min(observed_values) if observed_values else None,
            observation_span_seconds=_observation_span_seconds(observed_values),
            horizons=horizons,
            structure_verified_count=len(structure_features),
            bullish_structure_pct=_percentage(bullish_count, len(structure_features)),
            bearish_structure_pct=_percentage(bearish_count, len(structure_features)),
            neutral_structure_pct=_percentage(neutral_count, len(structure_features)),
            top_constituent_4h=top[0] if top else None,
            top_constituent_return_4h_pct=top[1] if top else None,
            bottom_constituent_4h=bottom[0] if bottom else None,
            bottom_constituent_return_4h_pct=bottom[1] if bottom else None,
        )

    def _rank_aggregates(
        self,
        aggregates: Sequence[SectorAggregate],
        *,
        benchmark_return: Decimal | None,
    ) -> tuple[SectorAggregate, ...]:
        if benchmark_return is None:
            return tuple(aggregates)
        eligible = tuple(
            aggregate
            for aggregate in aggregates
            if aggregate.status == SectorAggregateStatus.VERIFIED
            and aggregate.metrics_for(SECTOR_RANKING_HORIZON) is not None
            and aggregate.metrics_for(SECTOR_RANKING_HORIZON).relative_strength_vs_btc_pct_points
            is not None
        )
        sorted_eligible = sorted(eligible, key=_ranking_key)
        rank_by_sector = {
            aggregate.sector: index for index, aggregate in enumerate(sorted_eligible, start=1)
        }
        return tuple(
            aggregate.model_copy(
                update={
                    "sector_rank": rank_by_sector.get(aggregate.sector),
                    "rotation_state": _rotation_state(
                        aggregate,
                        broad_breadth_pct=self.broad_breadth_pct,
                    ),
                }
            )
            for aggregate in aggregates
        )


def project_sector_context(
    *,
    symbol: str,
    snapshot: SectorRotationSnapshot,
    member_feature: SectorMemberFeature | None,
    as_of: datetime,
) -> SectorRotationContext:
    classification = classify_sector(symbol)
    base = {
        "sector": classification.primary_sector,
        "observed_at": None,
        "age_seconds": None,
        "ranked_sector_count": snapshot.ranked_sector_count,
    }
    if snapshot.status == SectorSnapshotStatus.ERROR:
        return SectorRotationContext(
            **base,
            status=SectorContextStatus.ERROR,
            reason=snapshot.unavailable_reason or "sector_engine_error",
        )
    if classification.asset_type == SectorAssetType.BENCHMARK_ONLY:
        return SectorRotationContext(
            **base,
            status=SectorContextStatus.BENCHMARK_ONLY,
            reason="btc_benchmark_only",
        )
    if classification.asset_type == SectorAssetType.NON_DIRECTIONAL:
        return SectorRotationContext(
            **base,
            status=SectorContextStatus.NON_DIRECTIONAL,
            reason=classification.exclusion_reason,
        )
    if classification.asset_type == SectorAssetType.UNCLASSIFIED:
        return SectorRotationContext(
            **base,
            status=SectorContextStatus.UNCLASSIFIED,
            reason="unclassified_asset",
        )
    aggregate = snapshot.sector(classification.primary_sector)
    if aggregate is None:
        return SectorRotationContext(
            **base,
            status=SectorContextStatus.UNAVAILABLE,
            reason="sector_not_present_in_snapshot",
        )
    ranking_metrics = aggregate.metrics_for(SECTOR_RANKING_HORIZON)
    observed_at = aggregate.observed_at
    age_seconds = _age_seconds(
        normalize_utc_timestamp(as_of, field_name="sector_context_as_of"), observed_at
    )
    context_status = (
        SectorContextStatus.INSUFFICIENT_DATA
        if aggregate.status == SectorAggregateStatus.INSUFFICIENT_DATA
        else SectorContextStatus.STALE
        if age_seconds is not None and age_seconds > snapshot.freshness_seconds
        else SectorContextStatus.VERIFIED
    )
    benchmark_return = snapshot.benchmark.return_4h_pct if snapshot.benchmark else None
    member_return = member_feature.return_4h_pct if member_feature is not None else None
    return SectorRotationContext(
        sector=classification.primary_sector,
        status=context_status,
        observed_at=observed_at,
        age_seconds=age_seconds,
        sector_rank=aggregate.sector_rank,
        ranked_sector_count=snapshot.ranked_sector_count,
        constituent_count=aggregate.constituent_count,
        verified_constituent_count=aggregate.verified_constituent_count,
        coverage_pct=aggregate.coverage_pct,
        median_return_4h_pct=(
            ranking_metrics.median_return_pct if ranking_metrics is not None else None
        ),
        relative_strength_vs_btc_4h_pct_points=(
            ranking_metrics.relative_strength_vs_btc_pct_points
            if ranking_metrics is not None
            else None
        ),
        positive_breadth_4h_pct=(
            ranking_metrics.positive_breadth_pct if ranking_metrics is not None else None
        ),
        bullish_structure_pct=aggregate.bullish_structure_pct,
        rotation_state=aggregate.rotation_state,
        member_return_15m_pct=(
            member_feature.return_15m_pct if member_feature is not None else None
        ),
        member_return_1h_pct=(
            member_feature.return_1h_pct if member_feature is not None else None
        ),
        member_return_4h_pct=member_return,
        member_return_24h_pct=(
            member_feature.return_24h_pct if member_feature is not None else None
        ),
        member_relative_return_vs_btc_4h_pct_points=(
            _quantize(member_return - benchmark_return)
            if member_return is not None and benchmark_return is not None
            else None
        ),
        member_structure_state=(
            member_feature.structure_state if member_feature is not None else None
        ),
        reason=(
            "sector_snapshot_stale"
            if context_status == SectorContextStatus.STALE
            else "insufficient_constituent_coverage"
            if context_status == SectorContextStatus.INSUFFICIENT_DATA
            else None
        ),
    )


def _returns_from_closed_window(
    timeline: Sequence[Any], source_duration: timedelta
) -> dict[str, Decimal | None]:
    result = {horizon: None for horizon in SECTOR_RETURN_HORIZONS}
    if not timeline:
        return result
    latest = timeline[-1]
    latest_close = _decimal(_field(latest.source, "close"))
    if latest_close is None or latest_close <= 0:
        return result
    by_close_timestamp = {item.close_timestamp: item for item in timeline}
    for horizon in SECTOR_RETURN_HORIZONS:
        horizon_duration = timeframe_duration(horizon)
        if horizon_duration.total_seconds() % source_duration.total_seconds() != 0:
            continue
        start = by_close_timestamp.get(latest.close_timestamp - horizon_duration)
        start_close = _decimal(_field(start.source, "close")) if start is not None else None
        if start_close is None or start_close <= 0:
            continue
        result[horizon] = _quantize(
            (latest_close / start_close - Decimal("1")) * Decimal("100")
        )
    return result


def _horizon_metrics(
    *,
    horizon: str,
    constituent_count: int,
    features: Sequence[SectorMemberFeature],
    benchmark_return: Decimal | None,
) -> SectorHorizonMetrics:
    returns = tuple(
        value
        for feature in features
        if (value := feature.return_for(horizon)) is not None
    )
    if not returns:
        return SectorHorizonMetrics(
            horizon=horizon,
            verified_count=0,
            coverage_pct=Decimal("0"),
        )
    equal_weight = _quantize(sum(returns, Decimal("0")) / Decimal(len(returns)))
    median_return = _quantize(Decimal(median(returns)))
    return SectorHorizonMetrics(
        horizon=horizon,
        verified_count=len(returns),
        coverage_pct=_percentage(len(returns), constituent_count) or Decimal("0"),
        equal_weight_return_pct=equal_weight,
        median_return_pct=median_return,
        positive_breadth_pct=_percentage(sum(1 for value in returns if value > 0), len(returns)),
        negative_breadth_pct=_percentage(sum(1 for value in returns if value < 0), len(returns)),
        relative_strength_vs_btc_pct_points=(
            _quantize(median_return - benchmark_return)
            if benchmark_return is not None
            else None
        ),
    )


def _ranking_key(aggregate: SectorAggregate) -> tuple[Any, ...]:
    metrics = aggregate.metrics_for(SECTOR_RANKING_HORIZON)
    assert metrics is not None
    return (
        -_sort_decimal(metrics.relative_strength_vs_btc_pct_points),
        -_sort_decimal(metrics.median_return_pct),
        -_sort_decimal(metrics.positive_breadth_pct),
        -_sort_decimal(aggregate.bullish_structure_pct),
        aggregate.sector,
    )


def _rotation_state(
    aggregate: SectorAggregate, *, broad_breadth_pct: Decimal
) -> SectorRotationState:
    if aggregate.status != SectorAggregateStatus.VERIFIED:
        return SectorRotationState.INSUFFICIENT_DATA
    metrics = aggregate.metrics_for(SECTOR_RANKING_HORIZON)
    if metrics is None or metrics.relative_strength_vs_btc_pct_points is None:
        return SectorRotationState.INSUFFICIENT_DATA
    relative = metrics.relative_strength_vs_btc_pct_points
    if relative > 0:
        return (
            SectorRotationState.OUTPERFORMING_BROAD
            if (metrics.positive_breadth_pct or Decimal("0")) >= broad_breadth_pct
            else SectorRotationState.OUTPERFORMING_NARROW
        )
    if relative < 0:
        return (
            SectorRotationState.UNDERPERFORMING_BROAD
            if (metrics.negative_breadth_pct or Decimal("0")) >= broad_breadth_pct
            else SectorRotationState.UNDERPERFORMING_NARROW
        )
    return SectorRotationState.MIXED


def _snapshot_freshness_seconds(
    features: Sequence[SectorMemberFeature], *, intervals: int
) -> float:
    durations = tuple(
        timeframe_duration(feature.source_timeframe).total_seconds()
        for feature in features
        if feature.source_timeframe
    )
    source_seconds = min(durations) if durations else 15 * 60
    return float(source_seconds * intervals)


def _observation_span_seconds(values: Sequence[datetime]) -> float | None:
    if not values:
        return None
    return round(max((max(values) - min(values)).total_seconds(), 0.0), 3)


def _age_seconds(now: datetime, observed_at: datetime | None) -> float | None:
    if observed_at is None:
        return None
    return round(max((now - observed_at).total_seconds(), 0.0), 3)


def _classification_count(
    classifications: Sequence[SectorClassification], asset_type: SectorAssetType
) -> int:
    return sum(1 for item in classifications if item.asset_type == asset_type)


def _percentage(numerator: int, denominator: int) -> Decimal | None:
    if denominator <= 0:
        return None
    return _quantize(Decimal(numerator) / Decimal(denominator) * Decimal("100"))


def _sort_decimal(value: Decimal | None) -> Decimal:
    return value if value is not None else Decimal("-Infinity")


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _decimal(value: Any) -> Decimal | None:
    if value in (None, "", NA) or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return result if result.is_finite() else None


def _field(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, Mapping):
        return value.get(name)
    return getattr(value, name, None)


def _clean_reason(exc: Exception) -> str:
    return " ".join(str(exc).split()) or exc.__class__.__name__


def _plain(value: Decimal) -> str:
    text = format(value, "f")
    return text.rstrip("0").rstrip(".") if "." in text else text


def _signed(value: Decimal) -> str:
    text = _plain(value)
    return text if value < 0 else f"+{text}"


__all__ = [
    "DEFAULT_BROAD_BREADTH_PCT",
    "DEFAULT_FRESHNESS_INTERVALS",
    "DEFAULT_MIN_COVERAGE_PCT",
    "DEFAULT_MIN_VERIFIED_CONSTITUENTS",
    "SECTOR_RANKING_HORIZON",
    "SECTOR_RETURN_HORIZONS",
    "SECTOR_ROTATION_SCHEMA_VERSION",
    "SECTOR_USAGE",
    "SectorAggregate",
    "SectorAggregateStatus",
    "SectorContextStatus",
    "SectorHorizonMetrics",
    "SectorMemberFeature",
    "SectorMemberStatus",
    "SectorRotationContext",
    "SectorRotationEngine",
    "SectorRotationSnapshot",
    "SectorRotationState",
    "SectorSnapshotStatus",
    "build_sector_member_feature",
    "project_sector_context",
]
