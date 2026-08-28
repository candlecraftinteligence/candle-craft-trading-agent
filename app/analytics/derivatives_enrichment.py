from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data.dtos import NA, MaybeDecimal, MaybeInt

DecimalLike = Decimal | int | str
FundingStatus = Literal[
    "normal",
    "elevated_positive",
    "extreme_positive",
    "elevated_negative",
    "extreme_negative",
    "N/A",
]
OiDirection = Literal["rising", "falling", "flat", "N/A"]
PriceDirection = Literal["up", "down", "flat", "N/A"]
PriceOiRelationship = Literal[
    "long_building_or_breakout_participation",
    "short_covering_or_weak_participation",
    "short_building_or_long_trap_risk",
    "long_unwind_or_deleveraging",
    "neutral_or_no_clear_positioning",
    "N/A",
]
CrowdingRisk = Literal["low", "medium", "high", "N/A"]
SqueezeRisk = Literal["long_squeeze_risk", "short_squeeze_risk", "balanced", "N/A"]
DirectionalSupport = bool | Literal["N/A"]

OUTPUT_QUANT = Decimal("0.00000001")
DEFAULT_SOURCE = "public_futures_market_data"


class FundingContext(BaseModel):
    funding_rate: MaybeDecimal = NA
    funding_status: FundingStatus = NA
    funding_extreme: bool | Literal["N/A"] = NA
    direction: Literal["positive", "negative", "neutral", "N/A"] = NA
    history_sample_size: int = 0
    reason: str = "Funding is N/A because funding data is missing."

    model_config = ConfigDict(frozen=True)


class OpenInterestContext(BaseModel):
    open_interest: MaybeDecimal = NA
    previous_open_interest: MaybeDecimal = NA
    open_interest_change_pct: MaybeDecimal = NA
    oi_direction: OiDirection = NA
    history_sample_size: int = 0
    reason: str = "Open interest is N/A because current or previous OI is missing."

    model_config = ConfigDict(frozen=True)


class PriceOiContext(BaseModel):
    price_direction: PriceDirection = NA
    oi_direction: OiDirection = NA
    price_oi_relationship: PriceOiRelationship = NA
    price_change_pct: MaybeDecimal = NA
    reason: str = "Price/OI relationship is N/A because required data is missing."

    model_config = ConfigDict(frozen=True)


class CrowdingRiskContext(BaseModel):
    crowding_risk: CrowdingRisk = NA
    risk_direction: Literal["long", "short", "both", "none", "N/A"] = NA
    reason: str = "Crowding risk is N/A because required data is missing."

    model_config = ConfigDict(frozen=True)


class SqueezeRiskContext(BaseModel):
    squeeze_risk: SqueezeRisk = NA
    reason: str = "Squeeze risk is N/A because required data is missing."

    model_config = ConfigDict(frozen=True)


class DerivativesEnrichmentInput(BaseModel):
    symbol: str
    exchange: str
    latest_price: MaybeDecimal = NA
    current_funding_rate: MaybeDecimal = NA
    current_open_interest: MaybeDecimal = NA
    previous_open_interest: MaybeDecimal = NA
    candles_15m: Sequence[Any] | None = None
    funding_history: Sequence[Any] | None = None
    open_interest_history: Sequence[Any] | None = None
    long_short_ratio: MaybeDecimal = NA
    liquidation_data: Any | None = None
    source: str = DEFAULT_SOURCE
    warnings: Sequence[str] = ()

    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    @field_validator(
        "latest_price",
        "current_funding_rate",
        "current_open_interest",
        "previous_open_interest",
        "long_short_ratio",
        mode="before",
    )
    @classmethod
    def _normalize_maybe_decimal(cls, value: Any) -> Any:
        if _is_missing(value):
            return NA
        return _decimal_from(value, "derivatives_enrichment_input")

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator("exchange")
    @classmethod
    def _normalize_exchange(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("exchange must not be blank")
        return normalized


class DerivativesEnrichmentResult(BaseModel):
    symbol: str
    exchange: str
    source: str = DEFAULT_SOURCE
    funding_rate: MaybeDecimal = NA
    funding_status: FundingStatus = NA
    funding_extreme: bool | Literal["N/A"] = NA
    open_interest: MaybeDecimal = NA
    open_interest_change_pct: MaybeDecimal = NA
    oi_direction: OiDirection = NA
    price_direction: PriceDirection = NA
    price_oi_relationship: PriceOiRelationship = NA
    long_short_ratio: MaybeDecimal = NA
    crowding_risk: CrowdingRisk = NA
    squeeze_risk: SqueezeRisk = NA
    derivatives_score: MaybeInt = NA
    supports_long: DirectionalSupport = NA
    supports_short: DirectionalSupport = NA
    warnings: tuple[str, ...] = ()
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()
    funding_context: FundingContext = Field(default_factory=FundingContext)
    oi_context: OpenInterestContext = Field(default_factory=OpenInterestContext)
    price_oi_context: PriceOiContext = Field(default_factory=PriceOiContext)
    crowding_context: CrowdingRiskContext = Field(default_factory=CrowdingRiskContext)
    squeeze_context: SqueezeRiskContext = Field(default_factory=SqueezeRiskContext)

    model_config = ConfigDict(frozen=True)


class DerivativesEnrichmentEngine:
    """Deterministic derivatives enrichment from public futures market data only."""

    def __init__(
        self,
        *,
        funding_elevated_threshold: DecimalLike = Decimal("0.0005"),
        funding_extreme_threshold: DecimalLike = Decimal("0.0010"),
        oi_flat_threshold_pct: DecimalLike = Decimal("0.10"),
        oi_aggressive_threshold_pct: DecimalLike = Decimal("5.00"),
        price_flat_threshold_pct: DecimalLike = Decimal("0.10"),
        long_ratio_imbalance: DecimalLike = Decimal("1.50"),
        short_ratio_imbalance: DecimalLike = Decimal("0.67"),
        extreme_long_ratio: DecimalLike = Decimal("1.80"),
        extreme_short_ratio: DecimalLike = Decimal("0.55"),
    ) -> None:
        self.funding_elevated_threshold = _positive_decimal(
            funding_elevated_threshold,
            "funding_elevated_threshold",
        )
        self.funding_extreme_threshold = _positive_decimal(
            funding_extreme_threshold,
            "funding_extreme_threshold",
        )
        if self.funding_extreme_threshold < self.funding_elevated_threshold:
            raise ValueError("funding_extreme_threshold must be >= funding_elevated_threshold")

        self.oi_flat_threshold_pct = _non_negative_decimal(oi_flat_threshold_pct, "oi_flat_threshold_pct")
        self.oi_aggressive_threshold_pct = _non_negative_decimal(
            oi_aggressive_threshold_pct,
            "oi_aggressive_threshold_pct",
        )
        self.price_flat_threshold_pct = _non_negative_decimal(price_flat_threshold_pct, "price_flat_threshold_pct")
        self.long_ratio_imbalance = _positive_decimal(long_ratio_imbalance, "long_ratio_imbalance")
        self.short_ratio_imbalance = _positive_decimal(short_ratio_imbalance, "short_ratio_imbalance")
        self.extreme_long_ratio = _positive_decimal(extreme_long_ratio, "extreme_long_ratio")
        self.extreme_short_ratio = _positive_decimal(extreme_short_ratio, "extreme_short_ratio")

    def analyze(
        self,
        enrichment_input: DerivativesEnrichmentInput | Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> DerivativesEnrichmentResult:
        data = _normalize_input(enrichment_input, overrides)
        warnings = list(_clean_strings(data.warnings))
        funding_history_values, funding_history_warnings = _history_decimal_values(
            data.funding_history,
            ("funding_rate", "current_funding_rate", "rate", "fundingRate"),
            "funding_history",
        )
        oi_history_values, oi_history_warnings = _history_decimal_values(
            data.open_interest_history,
            ("open_interest", "current_open_interest", "oi", "openInterest", "sumOpenInterest"),
            "open_interest_history",
        )
        warnings.extend(funding_history_warnings)
        warnings.extend(oi_history_warnings)

        previous_open_interest = data.previous_open_interest
        if previous_open_interest == NA and len(oi_history_values) >= 2:
            previous_open_interest = oi_history_values[-2]

        current_open_interest = data.current_open_interest
        if current_open_interest == NA and oi_history_values:
            current_open_interest = oi_history_values[-1]

        current_funding_rate = data.current_funding_rate
        if current_funding_rate == NA and funding_history_values:
            current_funding_rate = funding_history_values[-1]

        funding = self._funding_context(current_funding_rate, len(funding_history_values))
        oi = self._open_interest_context(current_open_interest, previous_open_interest, len(oi_history_values))
        price_change_pct, price_direction = self._price_context(data.latest_price, data.candles_15m)
        price_oi = self._price_oi_context(price_change_pct, price_direction, oi.oi_direction)
        long_short_ratio, ratio_warnings = _normalize_long_short_ratio(data.long_short_ratio)
        warnings.extend(ratio_warnings)
        crowding = self._crowding_context(funding, oi, long_short_ratio)
        squeeze = self._squeeze_context(funding, oi, price_oi, long_short_ratio)
        missing_data = _missing_data(
            funding=funding,
            oi=oi,
            price_oi=price_oi,
            long_short_ratio=long_short_ratio,
            liquidation_data=data.liquidation_data,
        )
        unverified_data = _unverified_data(
            warnings=warnings,
            liquidation_data=data.liquidation_data,
        )
        score = _score_derivatives_context(
            funding=funding,
            oi=oi,
            price_oi=price_oi,
            long_short_ratio=long_short_ratio,
            funding_history_size=len(funding_history_values),
            oi_history_size=len(oi_history_values),
            warnings=warnings,
        )
        supports_long, supports_short = self._directional_support(
            funding=funding,
            oi=oi,
            price_oi=price_oi,
            crowding=crowding,
            squeeze=squeeze,
        )

        return DerivativesEnrichmentResult(
            symbol=data.symbol,
            exchange=data.exchange,
            source=data.source,
            funding_rate=funding.funding_rate,
            funding_status=funding.funding_status,
            funding_extreme=funding.funding_extreme,
            open_interest=oi.open_interest,
            open_interest_change_pct=oi.open_interest_change_pct,
            oi_direction=oi.oi_direction,
            price_direction=price_oi.price_direction,
            price_oi_relationship=price_oi.price_oi_relationship,
            long_short_ratio=long_short_ratio,
            crowding_risk=crowding.crowding_risk,
            squeeze_risk=squeeze.squeeze_risk,
            derivatives_score=score,
            supports_long=supports_long,
            supports_short=supports_short,
            warnings=_unique_strings(warnings),
            missing_data=missing_data,
            unverified_data=unverified_data,
            funding_context=funding,
            oi_context=oi,
            price_oi_context=price_oi,
            crowding_context=crowding,
            squeeze_context=squeeze,
        )

    def _funding_context(self, funding_rate: MaybeDecimal, history_sample_size: int) -> FundingContext:
        if funding_rate == NA:
            return FundingContext(history_sample_size=history_sample_size)

        if funding_rate > 0:
            direction: Literal["positive", "negative", "neutral", "N/A"] = "positive"
        elif funding_rate < 0:
            direction = "negative"
        else:
            direction = "neutral"

        absolute_rate = abs(funding_rate)
        if absolute_rate >= self.funding_extreme_threshold and direction == "positive":
            status: FundingStatus = "extreme_positive"
        elif absolute_rate >= self.funding_extreme_threshold and direction == "negative":
            status = "extreme_negative"
        elif absolute_rate >= self.funding_elevated_threshold and direction == "positive":
            status = "elevated_positive"
        elif absolute_rate >= self.funding_elevated_threshold and direction == "negative":
            status = "elevated_negative"
        else:
            status = "normal"

        return FundingContext(
            funding_rate=_quantize(funding_rate),
            funding_status=status,
            funding_extreme=status in ("extreme_positive", "extreme_negative"),
            direction=direction,
            history_sample_size=history_sample_size,
            reason="Funding status was classified from the public funding rate.",
        )

    def _open_interest_context(
        self,
        current_open_interest: MaybeDecimal,
        previous_open_interest: MaybeDecimal,
        history_sample_size: int,
    ) -> OpenInterestContext:
        if current_open_interest == NA or previous_open_interest == NA:
            return OpenInterestContext(
                open_interest=current_open_interest,
                previous_open_interest=previous_open_interest,
                history_sample_size=history_sample_size,
            )
        if previous_open_interest == 0:
            return OpenInterestContext(
                open_interest=_quantize(current_open_interest),
                previous_open_interest=_quantize(previous_open_interest),
                history_sample_size=history_sample_size,
                reason="Open interest change is N/A because previous OI is zero.",
            )

        change_pct = (current_open_interest - previous_open_interest) / abs(previous_open_interest) * Decimal("100")
        if change_pct > self.oi_flat_threshold_pct:
            direction: OiDirection = "rising"
        elif change_pct < -self.oi_flat_threshold_pct:
            direction = "falling"
        else:
            direction = "flat"

        return OpenInterestContext(
            open_interest=_quantize(current_open_interest),
            previous_open_interest=_quantize(previous_open_interest),
            open_interest_change_pct=_quantize(change_pct),
            oi_direction=direction,
            history_sample_size=history_sample_size,
            reason="Open interest change percentage was calculated from current and previous public OI.",
        )

    def _price_context(
        self,
        latest_price: MaybeDecimal,
        candles_15m: Sequence[Any] | None,
    ) -> tuple[MaybeDecimal, PriceDirection]:
        candles = tuple(candles_15m or ())
        if len(candles) < 2:
            return NA, NA

        first_close = _decimal_field(candles[0], ("close",))
        last_close = latest_price if latest_price != NA else _decimal_field(candles[-1], ("close",))
        if first_close == NA or last_close == NA or first_close == 0:
            return NA, NA

        change_pct = (last_close - first_close) / abs(first_close) * Decimal("100")
        if change_pct > self.price_flat_threshold_pct:
            direction: PriceDirection = "up"
        elif change_pct < -self.price_flat_threshold_pct:
            direction = "down"
        else:
            direction = "flat"
        return _quantize(change_pct), direction

    def _price_oi_context(
        self,
        price_change_pct: MaybeDecimal,
        price_direction: PriceDirection,
        oi_direction: OiDirection,
    ) -> PriceOiContext:
        if price_change_pct == NA or price_direction == NA or oi_direction == NA:
            return PriceOiContext(price_change_pct=price_change_pct, price_direction=price_direction, oi_direction=oi_direction)
        if price_direction == "up" and oi_direction == "rising":
            relationship: PriceOiRelationship = "long_building_or_breakout_participation"
        elif price_direction == "up" and oi_direction == "falling":
            relationship = "short_covering_or_weak_participation"
        elif price_direction == "down" and oi_direction == "rising":
            relationship = "short_building_or_long_trap_risk"
        elif price_direction == "down" and oi_direction == "falling":
            relationship = "long_unwind_or_deleveraging"
        else:
            relationship = "neutral_or_no_clear_positioning"
        return PriceOiContext(
            price_change_pct=price_change_pct,
            price_direction=price_direction,
            oi_direction=oi_direction,
            price_oi_relationship=relationship,
            reason="Price/OI relationship matched a deterministic public-data rule.",
        )

    def _crowding_context(
        self,
        funding: FundingContext,
        oi: OpenInterestContext,
        long_short_ratio: MaybeDecimal,
    ) -> CrowdingRiskContext:
        if funding.funding_status == NA and long_short_ratio == NA and oi.open_interest_change_pct == NA:
            return CrowdingRiskContext()

        oi_expanding = oi.oi_direction == "rising"
        oi_aggressive = (
            oi.open_interest_change_pct != NA
            and abs(oi.open_interest_change_pct) >= self.oi_aggressive_threshold_pct
        )
        long_imbalance = long_short_ratio != NA and long_short_ratio >= self.long_ratio_imbalance
        short_imbalance = long_short_ratio != NA and long_short_ratio <= self.short_ratio_imbalance
        extreme_long_imbalance = long_short_ratio != NA and long_short_ratio >= self.extreme_long_ratio
        extreme_short_imbalance = long_short_ratio != NA and long_short_ratio <= self.extreme_short_ratio

        if (
            funding.funding_status == "extreme_positive"
            and oi_expanding
            and (long_imbalance or long_short_ratio == NA)
        ) or (extreme_long_imbalance and oi_aggressive):
            return CrowdingRiskContext(
                crowding_risk="high",
                risk_direction="long",
                reason="High long crowding risk from extreme positive funding, OI expansion, and long imbalance.",
            )
        if (
            funding.funding_status == "extreme_negative"
            and oi_expanding
            and (short_imbalance or long_short_ratio == NA)
        ) or (extreme_short_imbalance and oi_aggressive):
            return CrowdingRiskContext(
                crowding_risk="high",
                risk_direction="short",
                reason="High short crowding risk from extreme negative funding, OI expansion, and short imbalance.",
            )

        if funding.funding_status in ("elevated_positive", "extreme_positive") or long_imbalance:
            return CrowdingRiskContext(
                crowding_risk="medium" if oi_expanding else "low",
                risk_direction="long" if oi_expanding else "none",
                reason="Long crowding inputs are present but not severe.",
            )
        if funding.funding_status in ("elevated_negative", "extreme_negative") or short_imbalance:
            return CrowdingRiskContext(
                crowding_risk="medium" if oi_expanding else "low",
                risk_direction="short" if oi_expanding else "none",
                reason="Short crowding inputs are present but not severe.",
            )

        return CrowdingRiskContext(
            crowding_risk="low",
            risk_direction="none",
            reason="No severe funding, OI, or long/short imbalance crowding rule was triggered.",
        )

    def _squeeze_context(
        self,
        funding: FundingContext,
        oi: OpenInterestContext,
        price_oi: PriceOiContext,
        long_short_ratio: MaybeDecimal,
    ) -> SqueezeRiskContext:
        if funding.funding_status == NA and oi.oi_direction == NA and price_oi.price_direction == NA:
            return SqueezeRiskContext()

        oi_expanding = oi.oi_direction == "rising"
        long_imbalance = long_short_ratio == NA or long_short_ratio >= self.long_ratio_imbalance
        short_imbalance = long_short_ratio == NA or long_short_ratio <= self.short_ratio_imbalance

        if funding.funding_status == "extreme_positive" and oi_expanding and long_imbalance:
            if price_oi.price_direction in ("down", "flat", NA):
                return SqueezeRiskContext(
                    squeeze_risk="long_squeeze_risk",
                    reason="Long squeeze risk from extreme positive funding, expanding OI, and weak price response.",
                )
            return SqueezeRiskContext(
                squeeze_risk="long_squeeze_risk",
                reason="Long squeeze risk from crowded long positioning despite rising price.",
            )
        if funding.funding_status == "extreme_negative" and oi_expanding and short_imbalance:
            if price_oi.price_direction in ("up", "flat", NA):
                return SqueezeRiskContext(
                    squeeze_risk="short_squeeze_risk",
                    reason="Short squeeze risk from extreme negative funding, expanding OI, and strong price response.",
                )
            return SqueezeRiskContext(
                squeeze_risk="short_squeeze_risk",
                reason="Short squeeze risk from crowded short positioning despite falling price.",
            )

        return SqueezeRiskContext(
            squeeze_risk="balanced",
            reason="No deterministic squeeze-risk rule was triggered.",
        )

    def _directional_support(
        self,
        *,
        funding: FundingContext,
        oi: OpenInterestContext,
        price_oi: PriceOiContext,
        crowding: CrowdingRiskContext,
        squeeze: SqueezeRiskContext,
    ) -> tuple[DirectionalSupport, DirectionalSupport]:
        if funding.funding_status == NA and oi.oi_direction == NA and price_oi.price_direction == NA:
            return NA, NA

        long_conflict = (
            (funding.funding_status == "extreme_positive" and oi.oi_direction == "rising")
            or (crowding.risk_direction == "long" and crowding.crowding_risk == "high")
            or squeeze.squeeze_risk == "long_squeeze_risk"
        )
        short_conflict = (
            (funding.funding_status == "extreme_negative" and oi.oi_direction == "rising")
            or (crowding.risk_direction == "short" and crowding.crowding_risk == "high")
            or squeeze.squeeze_risk == "short_squeeze_risk"
        )

        supports_long: DirectionalSupport = not long_conflict
        supports_short: DirectionalSupport = not short_conflict
        if price_oi.price_oi_relationship == "long_building_or_breakout_participation":
            supports_long = supports_long and True
        if price_oi.price_oi_relationship == "short_building_or_long_trap_risk":
            supports_short = supports_short and True
        return supports_long, supports_short


def enrich_derivatives(
    enrichment_input: DerivativesEnrichmentInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> DerivativesEnrichmentResult:
    return DerivativesEnrichmentEngine().analyze(enrichment_input, **overrides)


def _normalize_input(
    enrichment_input: DerivativesEnrichmentInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> DerivativesEnrichmentInput:
    if enrichment_input is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(enrichment_input, DerivativesEnrichmentInput):
        raw = enrichment_input.model_dump()
        raw.update(overrides)
    else:
        raw = dict(enrichment_input)
        raw.update(overrides)
    return DerivativesEnrichmentInput.model_validate(raw)


def _history_decimal_values(
    values: Sequence[Any] | None,
    aliases: Sequence[str],
    label: str,
) -> tuple[list[Decimal], list[str]]:
    if values is None:
        return [], []
    output: list[Decimal] = []
    warnings: list[str] = []
    for index, item in enumerate(values):
        if isinstance(item, (Decimal, int, str)):
            try:
                output.append(_decimal_from(item, f"{label}[{index}]"))
                continue
            except ValueError:
                warnings.append(f"{label}[{index}] could not be verified.")
                continue
        value = _decimal_field(item, aliases)
        if value == NA:
            warnings.append(f"{label}[{index}] could not be verified.")
            continue
        output.append(value)
    return output, warnings


def _normalize_long_short_ratio(value: Any) -> tuple[MaybeDecimal, tuple[str, ...]]:
    if _is_missing(value):
        return NA, ()
    if isinstance(value, Mapping):
        raw = _field(value, "long_short_ratio")
        if _is_missing(raw):
            raw = _field(value, "longShortRatio")
        if _is_missing(raw):
            raw = _field(value, "ratio")
        if _is_missing(raw):
            buy = _decimal_field(value, ("buyRatio", "longAccount", "longRatio"))
            sell = _decimal_field(value, ("sellRatio", "shortAccount", "shortRatio"))
            if buy != NA and sell != NA and sell != 0:
                return _quantize(buy / sell), ()
        value = raw
    try:
        ratio = _decimal_from(value, "long_short_ratio")
    except ValueError:
        return NA, ("long_short_ratio is Unverified because it was malformed.",)
    if ratio <= 0:
        return NA, ("long_short_ratio is Unverified because it was not positive.",)
    return _quantize(ratio), ()


def _missing_data(
    *,
    funding: FundingContext,
    oi: OpenInterestContext,
    price_oi: PriceOiContext,
    long_short_ratio: MaybeDecimal,
    liquidation_data: Any | None,
) -> tuple[str, ...]:
    missing: list[str] = []
    if funding.funding_rate == NA:
        missing.append("funding_rate: N/A")
    if oi.open_interest == NA:
        missing.append("open_interest: N/A")
    if oi.previous_open_interest == NA:
        missing.append("previous_open_interest: N/A")
    if oi.open_interest_change_pct == NA:
        missing.append("open_interest_change_pct: N/A")
    if price_oi.price_direction == NA:
        missing.append("price_direction: N/A")
    if price_oi.price_oi_relationship == NA:
        missing.append("price_oi_relationship: N/A")
    if long_short_ratio == NA:
        missing.append("long_short_ratio: N/A")
    if _is_missing(liquidation_data):
        missing.append("liquidation_data: N/A")
    return _unique_strings(missing)


def _unverified_data(*, warnings: Sequence[str], liquidation_data: Any | None) -> tuple[str, ...]:
    if _verified_research_context(liquidation_data):
        liquidation_data = None
    unverified: list[str] = []
    if warnings:
        unverified.append("derivatives: Unverified")
    if not _is_missing(liquidation_data):
        unverified.append("liquidation_data: Unverified")
    return _unique_strings(unverified)


def _verified_research_context(value: Any | None) -> bool:
    return bool(
        isinstance(value, Mapping)
        and value.get("usage") == "research_only"
        and value.get("status") == "VERIFIED"
    )


def _score_derivatives_context(
    *,
    funding: FundingContext,
    oi: OpenInterestContext,
    price_oi: PriceOiContext,
    long_short_ratio: MaybeDecimal,
    funding_history_size: int,
    oi_history_size: int,
    warnings: Sequence[str],
) -> int:
    score = 0
    if funding.funding_rate != NA:
        score += 20
    if oi.open_interest_change_pct != NA:
        score += 25
    if price_oi.price_direction != NA:
        score += 15
    if price_oi.price_oi_relationship != NA:
        score += 15
    if long_short_ratio != NA:
        score += 10
    if funding_history_size >= 2:
        score += 5
    if oi_history_size >= 2:
        score += 5
    if not warnings and score >= 70:
        score += 5
    if warnings:
        score -= min(10, len(warnings) * 2)
    return max(0, min(100, score))


def _decimal_field(source: Any | None, names: Sequence[str]) -> MaybeDecimal:
    value = None
    for name in names:
        value = _field(source, name)
        if not _is_missing(value):
            break
    else:
        return NA
    try:
        return _decimal_from(value, names[0])
    except ValueError:
        return NA


def _field(source: Any | None, name: str) -> Any:
    if source is None:
        return None
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _clean_strings(values: Sequence[str] | str | None) -> tuple[str, ...]:
    if values is None:
        return ()
    if isinstance(values, str):
        values = (values,)
    return tuple(value.strip() for value in values if value and value.strip())


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        cleaned = value.strip()
        if cleaned and cleaned not in output:
            output.append(cleaned)
    return tuple(output)


def _positive_decimal(value: Any, path: str) -> Decimal:
    decimal = _decimal_from(value, path)
    if decimal <= 0:
        raise ValueError(f"{path} must be positive")
    return decimal


def _non_negative_decimal(value: Any, path: str) -> Decimal:
    decimal = _decimal_from(value, path)
    if decimal < 0:
        raise ValueError(f"{path} cannot be negative")
    return decimal


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Malformed derivatives enrichment data at {path}: invalid decimal {value!r}.") from exc
    if not decimal.is_finite():
        raise ValueError(f"Malformed derivatives enrichment data at {path}: invalid decimal {value!r}.")
    return decimal


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


__all__ = [
    "CrowdingRiskContext",
    "DerivativesEnrichmentEngine",
    "DerivativesEnrichmentInput",
    "DerivativesEnrichmentResult",
    "FundingContext",
    "OpenInterestContext",
    "PriceOiContext",
    "SqueezeRiskContext",
    "enrich_derivatives",
]
