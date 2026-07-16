from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data.dtos import NA, MaybeDecimal

OUTPUT_QUANT = Decimal("0.00000001")
MODE_ORDER = ("challenge", "swing", "scalp")


class RegimeState(str, Enum):
    TREND_EXPANSION = "TREND_EXPANSION"
    TREND_PULLBACK = "TREND_PULLBACK"
    RANGE_COMPRESSION = "RANGE_COMPRESSION"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOLATILITY = "LOW_VOLATILITY"
    CHOP = "CHOP"
    RISK_OFF = "RISK_OFF"
    RISK_ON = "RISK_ON"
    MIXED = "MIXED"
    TRANSITION = "TRANSITION"

    # Backward-compatible names from the Phase 31 implementation.
    COMPRESSION = "RANGE_COMPRESSION"
    PANIC_VOLATILITY = "HIGH_VOLATILITY"
    LOW_VOL_DRIFT = "LOW_VOLATILITY"
    DATA_INCOMPLETE = "MIXED"


class RegimeRiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXTREME = "EXTREME"
    NA = "N/A"


class RegimeStrictness(str, Enum):
    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"


class RegimeConfidenceBand(str, Enum):
    HOSTILE = "hostile"
    WEAK = "weak"
    ACCEPTABLE = "acceptable"
    FAVORABLE = "favorable"
    EXCEPTIONAL = "exceptional"


CompatibilityLabel = Literal["Hostile", "Weak", "Moderate", "Strong", "Exceptional"]


class RegimeCompatibility(BaseModel):
    mode: Literal["challenge", "swing", "scalp"]
    score: int = Field(ge=0, le=100)
    label: CompatibilityLabel
    allowed: bool = True
    regime_compatibility: int = Field(ge=0, le=100)
    volatility_suitability: int = Field(ge=0, le=100)
    trend_suitability: int = Field(ge=0, le=100)
    execution_quality_suitability: int = Field(ge=0, le=100)
    risk_multiplier: Decimal = Decimal("1")
    confidence_adjustment: int = 0
    notes: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)

    @field_validator("risk_multiplier", mode="before")
    @classmethod
    def _normalize_decimal(cls, value: Any) -> Decimal:
        if _is_missing(value):
            return Decimal("1")
        return _quantize(_decimal_from(value, "regime compatibility"))

    @field_validator("notes", mode="before")
    @classmethod
    def _normalize_notes(cls, value: Any) -> tuple[str, ...]:
        return _sequence_values(value)


class RegimeAdjustment(BaseModel):
    allow_scalps: bool = True
    allow_swings: bool = True
    allow_challenge: bool = True
    min_quality_score_adjustment: int = Field(default=0, ge=0)
    min_rr_adjustment: Decimal = Decimal("0")
    risk_multiplier: Decimal = Decimal("1")
    readiness_score_adjustment: int = 0
    edge_score_adjustment: int = 0
    trust_score_adjustment: int = 0
    portfolio_confidence_adjustment: int = 0
    regime_penalty: int = Field(default=0, ge=0, le=100)
    compatibility_scores: dict[str, int] = Field(default_factory=dict)
    explanation: str = NA

    model_config = ConfigDict(frozen=True)

    @field_validator("min_rr_adjustment", "risk_multiplier", mode="before")
    @classmethod
    def _normalize_decimal(cls, value: Any) -> Decimal:
        if _is_missing(value):
            return Decimal("0")
        return _quantize(_decimal_from(value, "regime adjustment"))

    @field_validator("compatibility_scores", mode="before")
    @classmethod
    def _normalize_compatibility_scores(cls, value: Any) -> dict[str, int]:
        if not isinstance(value, Mapping):
            return {}
        output: dict[str, int] = {}
        for key, item in value.items():
            try:
                output[str(key)] = max(0, min(100, int(Decimal(str(item)))))
            except (InvalidOperation, ValueError):
                continue
        return output


class MarketRegimeInput(BaseModel):
    btc_candles: tuple[Any, ...] = ()
    eth_candles: tuple[Any, ...] = ()
    total_proxy_candles: tuple[Any, ...] = ()
    total2_proxy_candles: tuple[Any, ...] = ()
    btc_d_context: str = NA
    usdt_d_context: str = NA
    scanned_symbols: int = 0
    bullish_bias_pct: MaybeDecimal = NA
    bearish_bias_pct: MaybeDecimal = NA
    valid_sweep_pct: MaybeDecimal = NA
    confirmation_pct: MaybeDecimal = NA
    failed_confirmation_pct: MaybeDecimal = NA
    volatility_expansion_vs_average: MaybeDecimal = NA
    htf_agreement_pct: MaybeDecimal = NA
    htf_conflict_pct: MaybeDecimal = NA
    average_rr: MaybeDecimal = NA
    setup_density_pct: MaybeDecimal = NA
    rejection_clustering_pct: MaybeDecimal = NA
    broad_participation_pct: MaybeDecimal = NA
    risk_mode: Literal["conservative", "balanced", "aggressive"] = "balanced"
    strictness: RegimeStrictness = RegimeStrictness.NORMAL
    candle_timeframe: str = "12h"
    decision_timestamp: datetime | None = None
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    @field_validator(
        "btc_candles",
        "eth_candles",
        "total_proxy_candles",
        "total2_proxy_candles",
        mode="before",
    )
    @classmethod
    def _normalize_candle_sequence(cls, value: Any) -> tuple[Any, ...]:
        if value is None:
            return ()
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            return tuple(value)
        return ()

    @field_validator(
        "bullish_bias_pct",
        "bearish_bias_pct",
        "valid_sweep_pct",
        "confirmation_pct",
        "failed_confirmation_pct",
        "volatility_expansion_vs_average",
        "htf_agreement_pct",
        "htf_conflict_pct",
        "average_rr",
        "setup_density_pct",
        "rejection_clustering_pct",
        "broad_participation_pct",
        mode="before",
    )
    @classmethod
    def _normalize_maybe_decimal(cls, value: Any) -> MaybeDecimal:
        if _is_missing(value):
            return NA
        return _quantize(_decimal_from(value, "market climate input"))

    @field_validator("btc_d_context", "usdt_d_context", mode="before")
    @classmethod
    def _normalize_context(cls, value: Any) -> str:
        return _display(value)

    @field_validator("strictness", mode="before")
    @classmethod
    def _normalize_strictness(cls, value: Any) -> RegimeStrictness:
        if isinstance(value, RegimeStrictness):
            return value
        if _is_missing(value):
            return RegimeStrictness.NORMAL
        text = str(value).strip().lower()
        if text in {"aggressive"}:
            return RegimeStrictness.LOW
        if text in {"balanced"}:
            return RegimeStrictness.NORMAL
        if text in {"conservative"}:
            return RegimeStrictness.HIGH
        return RegimeStrictness(text)

    @field_validator("missing_data", "unverified_data", mode="before")
    @classmethod
    def _normalize_string_tuple(cls, value: Any) -> tuple[str, ...]:
        return _sequence_values(value)


class MarketRegimeResult(BaseModel):
    enabled: bool = True
    state: RegimeState
    risk_level: RegimeRiskLevel
    confidence_score: int = Field(default=45, ge=0, le=100)
    confidence_band: RegimeConfidenceBand = RegimeConfidenceBand.WEAK
    strictness: RegimeStrictness = RegimeStrictness.NORMAL
    compatibility_scores: dict[str, RegimeCompatibility] = Field(default_factory=dict)
    adjustment: RegimeAdjustment
    metrics: dict[str, Any] = Field(default_factory=dict)
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    environment_notes: tuple[str, ...] = ()
    boosts: tuple[str, ...] = ()
    penalties: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)

    @field_validator("compatibility_scores", mode="before")
    @classmethod
    def _normalize_compatibilities(cls, value: Any) -> dict[str, RegimeCompatibility]:
        if not isinstance(value, Mapping):
            return {}
        output: dict[str, RegimeCompatibility] = {}
        for key, item in value.items():
            mode = str(key).lower()
            if mode not in MODE_ORDER:
                continue
            output[mode] = item if isinstance(item, RegimeCompatibility) else RegimeCompatibility.model_validate(item)
        return output

    @field_validator("missing_data", "unverified_data", "warnings", "environment_notes", "boosts", "penalties", mode="before")
    @classmethod
    def _normalize_tuples(cls, value: Any) -> tuple[str, ...]:
        return _sequence_values(value)


def confidence_band(score: int) -> RegimeConfidenceBand:
    if score <= 30:
        return RegimeConfidenceBand.HOSTILE
    if score <= 50:
        return RegimeConfidenceBand.WEAK
    if score <= 70:
        return RegimeConfidenceBand.ACCEPTABLE
    if score <= 85:
        return RegimeConfidenceBand.FAVORABLE
    return RegimeConfidenceBand.EXCEPTIONAL


def compatibility_label(score: int) -> CompatibilityLabel:
    if score <= 30:
        return "Hostile"
    if score <= 50:
        return "Weak"
    if score <= 70:
        return "Moderate"
    if score <= 85:
        return "Strong"
    return "Exceptional"


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Invalid decimal at {path}: {value!r}") from exc
    if not decimal.is_finite():
        raise ValueError(f"Invalid decimal at {path}: {value!r}")
    return decimal


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _sequence_values(values: Any) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    if not isinstance(values, Sequence):
        return ()
    output: list[str] = []
    for value in values:
        text = _display(value)
        if text != NA and text not in output:
            output.append(text)
    return tuple(output)


def _display(value: Any) -> str:
    if isinstance(value, Enum):
        value = value.value
    if _is_missing(value):
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value).strip() or NA


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA
