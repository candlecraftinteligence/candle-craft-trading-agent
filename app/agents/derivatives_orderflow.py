from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.data.dtos import NA, MaybeDecimal

DecimalLike = Decimal | int | str
FundingDirection = Literal["positive", "negative", "neutral", "N/A"]
FundingSeverity = Literal["normal", "elevated", "extreme", "N/A"]
OiDirection = Literal["increasing", "decreasing", "flat", "N/A"]
PriceDirection = Literal["up", "down", "flat", "N/A"]
RelationshipClassification = Literal[
    "new participation / trend participation",
    "short covering / weaker continuation",
    "new shorts / possible short crowding",
    "long liquidation / de-risking",
    "N/A",
]
VolumeConfirmation = Literal["no confirmation", "moderate confirmation", "strong confirmation", "N/A"]
DataQualityStatus = Literal["valid", "partial", "invalid"]
ReliabilityStatus = Literal["Verified", "Unverified"]

OUTPUT_QUANT = Decimal("0.00000001")


class FundingSignal(BaseModel):
    raw_funding_rate: MaybeDecimal = NA
    direction: FundingDirection = NA
    severity: FundingSeverity = NA
    z_score: MaybeDecimal = NA
    historical_sample_size: int = 0
    reason: str = "Funding is N/A because funding data is missing."

    model_config = ConfigDict(frozen=True)


class OpenInterestSignal(BaseModel):
    current_open_interest: MaybeDecimal = NA
    previous_open_interest: MaybeDecimal = NA
    oi_change_percentage: MaybeDecimal = NA
    direction: OiDirection = NA
    reason: str = "Open interest is N/A because current or previous OI is missing."

    model_config = ConfigDict(frozen=True)


class PriceOiRelationship(BaseModel):
    price_change_percentage: MaybeDecimal = NA
    price_direction: PriceDirection = NA
    oi_direction: OiDirection = NA
    classification: RelationshipClassification = NA
    reason: str = "Price/OI relationship is N/A because required data is missing."

    model_config = ConfigDict(frozen=True)


class CrowdingRiskSignal(BaseModel):
    crowded_long_risk: bool = False
    crowded_short_risk: bool = False
    risk_direction: Literal["long", "short", "none", "N/A"] = NA
    reason: str = "Crowding risk is N/A because required data is missing."

    model_config = ConfigDict(frozen=True)


class VolumeConfirmationSignal(BaseModel):
    volume_z_score: MaybeDecimal = NA
    confirmation: VolumeConfirmation = NA
    reason: str = "Volume confirmation is N/A because volume z-score is missing."

    model_config = ConfigDict(frozen=True)


class DerivativesRiskFlags(BaseModel):
    crowded_long_risk: bool = False
    crowded_short_risk: bool = False
    funding_extreme: bool = False
    oi_spike: bool = False
    missing_funding: bool = False
    missing_open_interest: bool = False
    missing_volume: bool = False
    conflicting_context: bool = False

    model_config = ConfigDict(frozen=True)


class DerivativesDataQuality(BaseModel):
    status: DataQualityStatus
    reliability: ReliabilityStatus
    missing_fields: tuple[str, ...] = ()
    unverified_fields: tuple[str, ...] = ()
    cvd: Literal["N/A"] = NA
    liquidation_heatmap: Literal["N/A"] = NA
    reason: str

    model_config = ConfigDict(frozen=True)


class DerivativesOrderflowResult(BaseModel):
    is_valid: bool
    data_quality: DerivativesDataQuality
    errors: tuple[str, ...] = ()
    funding: FundingSignal = FundingSignal()
    open_interest: OpenInterestSignal = OpenInterestSignal()
    price_oi_relationship: PriceOiRelationship = PriceOiRelationship()
    crowding_risk: CrowdingRiskSignal = CrowdingRiskSignal()
    volume_confirmation: VolumeConfirmationSignal = VolumeConfirmationSignal()
    risk_flags: DerivativesRiskFlags = DerivativesRiskFlags()
    active_risk_flags: tuple[str, ...] = ()
    derivatives_score: int = 0
    score_components: dict[str, int] = {}
    cvd: Literal["N/A"] = NA
    liquidation_heatmap: Literal["N/A"] = NA

    model_config = ConfigDict(frozen=True)


class DerivativesOrderflowAgent:
    """Deterministic derivatives context analysis for normalized public market data.

    The agent classifies funding, open interest, price/OI context, crowding risk,
    and volume confirmation. It does not call exchanges, use private API data,
    produce trade recommendations, or create order instructions.
    """

    def __init__(
        self,
        *,
        funding_elevated_threshold: DecimalLike = Decimal("0.0005"),
        funding_extreme_threshold: DecimalLike = Decimal("0.0010"),
        oi_flat_change_threshold_percentage: DecimalLike = Decimal("0.10"),
        oi_significant_change_threshold_percentage: DecimalLike = Decimal("2.00"),
        oi_spike_threshold_percentage: DecimalLike = Decimal("10.00"),
        price_flat_change_threshold_percentage: DecimalLike = Decimal("0.10"),
        volume_moderate_z_threshold: DecimalLike = Decimal("1.00"),
        volume_strong_z_threshold: DecimalLike = Decimal("2.00"),
    ) -> None:
        self.funding_elevated_threshold = _decimal_from(
            funding_elevated_threshold,
            "funding_elevated_threshold",
        )
        self.funding_extreme_threshold = _decimal_from(
            funding_extreme_threshold,
            "funding_extreme_threshold",
        )
        self.oi_flat_change_threshold_percentage = _decimal_from(
            oi_flat_change_threshold_percentage,
            "oi_flat_change_threshold_percentage",
        )
        self.oi_significant_change_threshold_percentage = _decimal_from(
            oi_significant_change_threshold_percentage,
            "oi_significant_change_threshold_percentage",
        )
        self.oi_spike_threshold_percentage = _decimal_from(
            oi_spike_threshold_percentage,
            "oi_spike_threshold_percentage",
        )
        self.price_flat_change_threshold_percentage = _decimal_from(
            price_flat_change_threshold_percentage,
            "price_flat_change_threshold_percentage",
        )
        self.volume_moderate_z_threshold = _decimal_from(
            volume_moderate_z_threshold,
            "volume_moderate_z_threshold",
        )
        self.volume_strong_z_threshold = _decimal_from(
            volume_strong_z_threshold,
            "volume_strong_z_threshold",
        )

        if self.funding_elevated_threshold <= 0:
            raise ValueError("funding_elevated_threshold must be positive")
        if self.funding_extreme_threshold < self.funding_elevated_threshold:
            raise ValueError("funding_extreme_threshold must be greater than or equal to elevated threshold")
        if self.oi_flat_change_threshold_percentage < 0:
            raise ValueError("oi_flat_change_threshold_percentage cannot be negative")
        if self.oi_significant_change_threshold_percentage < self.oi_flat_change_threshold_percentage:
            raise ValueError("oi_significant_change_threshold_percentage must be greater than or equal to flat threshold")
        if self.oi_spike_threshold_percentage < self.oi_significant_change_threshold_percentage:
            raise ValueError("oi_spike_threshold_percentage must be greater than or equal to significant threshold")
        if self.price_flat_change_threshold_percentage < 0:
            raise ValueError("price_flat_change_threshold_percentage cannot be negative")
        if self.volume_moderate_z_threshold < 0:
            raise ValueError("volume_moderate_z_threshold cannot be negative")
        if self.volume_strong_z_threshold < self.volume_moderate_z_threshold:
            raise ValueError("volume_strong_z_threshold must be greater than or equal to moderate threshold")

    def analyze(self, market_data: Any | None = None, **overrides: Any) -> DerivativesOrderflowResult:
        errors: list[str] = []
        price_change_percentage = _extract_price_change_percentage(market_data, overrides, errors)
        funding_rate = _extract_decimal(
            market_data,
            overrides,
            ("funding_rate", "current_funding_rate"),
            "funding_rate",
            errors,
        )
        current_open_interest = _extract_decimal(
            market_data,
            overrides,
            ("current_open_interest", "open_interest", "oi"),
            "current_open_interest",
            errors,
        )
        previous_open_interest = _extract_decimal(
            market_data,
            overrides,
            ("previous_open_interest", "previous_oi", "open_interest_previous"),
            "previous_open_interest",
            errors,
        )
        historical_funding_rates = _extract_value(
            market_data,
            overrides,
            ("historical_funding_rates", "funding_history", "historical_funding"),
        )
        volume_z_score = _extract_decimal(
            market_data,
            overrides,
            ("volume_z_score", "volume_zscore", "volume_z_score_24h"),
            "volume_z_score",
            errors,
        )

        funding = self._analyze_funding(funding_rate, historical_funding_rates, errors)
        open_interest = self._analyze_open_interest(current_open_interest, previous_open_interest)
        price_oi_relationship = self._classify_price_oi_relationship(price_change_percentage, open_interest)
        crowding_risk = self._analyze_crowding_risk(funding, open_interest, price_oi_relationship)
        volume_confirmation = self._classify_volume_confirmation(volume_z_score)
        risk_flags = self._build_risk_flags(funding, open_interest, price_oi_relationship, crowding_risk, volume_confirmation)
        data_quality = _build_data_quality(price_change_percentage, open_interest, funding, volume_confirmation, errors)

        if data_quality.status == "invalid":
            score_components = {
                "price_oi_clarity": 0,
                "funding_context": 0,
                "oi_change_significance": 0,
                "volume_confirmation": 0,
                "crowding_risk_adjustment": 0,
                "data_quality": 0,
            }
            score = 0
        else:
            score_components = self._score_components(
                funding=funding,
                open_interest=open_interest,
                relationship=price_oi_relationship,
                crowding_risk=crowding_risk,
                volume_confirmation=volume_confirmation,
                data_quality=data_quality,
            )
            score = min(sum(score_components.values()), 100)

        return DerivativesOrderflowResult(
            is_valid=data_quality.status == "valid",
            data_quality=data_quality,
            errors=tuple(errors),
            funding=funding,
            open_interest=open_interest,
            price_oi_relationship=price_oi_relationship,
            crowding_risk=crowding_risk,
            volume_confirmation=volume_confirmation,
            risk_flags=risk_flags,
            active_risk_flags=_active_risk_flags(risk_flags),
            derivatives_score=score,
            score_components=score_components,
        )

    def _analyze_funding(
        self,
        funding_rate: MaybeDecimal,
        historical_funding_rates: Any,
        errors: list[str],
    ) -> FundingSignal:
        if funding_rate == NA:
            return FundingSignal()

        direction: FundingDirection
        if funding_rate > 0:
            direction = "positive"
        elif funding_rate < 0:
            direction = "negative"
        else:
            direction = "neutral"

        absolute_rate = abs(funding_rate)
        if absolute_rate >= self.funding_extreme_threshold:
            severity: FundingSeverity = "extreme"
        elif absolute_rate >= self.funding_elevated_threshold:
            severity = "elevated"
        else:
            severity = "normal"

        z_score, sample_size = _calculate_z_score(funding_rate, historical_funding_rates, "historical_funding_rates", errors)
        return FundingSignal(
            raw_funding_rate=_quantize(funding_rate),
            direction=direction,
            severity=severity,
            z_score=z_score,
            historical_sample_size=sample_size,
            reason="Funding rate was classified deterministically from the normalized funding input.",
        )

    def _analyze_open_interest(
        self,
        current_open_interest: MaybeDecimal,
        previous_open_interest: MaybeDecimal,
    ) -> OpenInterestSignal:
        if current_open_interest == NA or previous_open_interest == NA:
            return OpenInterestSignal(
                current_open_interest=current_open_interest,
                previous_open_interest=previous_open_interest,
            )
        if previous_open_interest == 0:
            return OpenInterestSignal(
                current_open_interest=_quantize(current_open_interest),
                previous_open_interest=_quantize(previous_open_interest),
                reason="Open interest change is N/A because previous OI is zero.",
            )

        change_percentage = ((current_open_interest - previous_open_interest) / abs(previous_open_interest)) * Decimal("100")
        quantized_change = _quantize(change_percentage)
        if change_percentage > self.oi_flat_change_threshold_percentage:
            direction: OiDirection = "increasing"
        elif change_percentage < -self.oi_flat_change_threshold_percentage:
            direction = "decreasing"
        else:
            direction = "flat"

        return OpenInterestSignal(
            current_open_interest=_quantize(current_open_interest),
            previous_open_interest=_quantize(previous_open_interest),
            oi_change_percentage=quantized_change,
            direction=direction,
            reason="Open interest change percentage was calculated from current and previous OI.",
        )

    def _classify_price_oi_relationship(
        self,
        price_change_percentage: MaybeDecimal,
        open_interest: OpenInterestSignal,
    ) -> PriceOiRelationship:
        price_direction = self._price_direction(price_change_percentage)
        oi_direction = open_interest.direction
        if price_change_percentage == NA or oi_direction == NA:
            return PriceOiRelationship(
                price_change_percentage=price_change_percentage,
                price_direction=price_direction,
                oi_direction=oi_direction,
            )

        if price_direction == "up" and oi_direction == "increasing":
            classification: RelationshipClassification = "new participation / trend participation"
        elif price_direction == "up" and oi_direction == "decreasing":
            classification = "short covering / weaker continuation"
        elif price_direction == "down" and oi_direction == "increasing":
            classification = "new shorts / possible short crowding"
        elif price_direction == "down" and oi_direction == "decreasing":
            classification = "long liquidation / de-risking"
        else:
            return PriceOiRelationship(
                price_change_percentage=_quantize(price_change_percentage),
                price_direction=price_direction,
                oi_direction=oi_direction,
                reason="Price/OI relationship is N/A because price or OI is flat.",
            )

        return PriceOiRelationship(
            price_change_percentage=_quantize(price_change_percentage),
            price_direction=price_direction,
            oi_direction=oi_direction,
            classification=classification,
            reason="Price/OI relationship matched a deterministic classification rule.",
        )

    def _price_direction(self, price_change_percentage: MaybeDecimal) -> PriceDirection:
        if price_change_percentage == NA:
            return NA
        if price_change_percentage > self.price_flat_change_threshold_percentage:
            return "up"
        if price_change_percentage < -self.price_flat_change_threshold_percentage:
            return "down"
        return "flat"

    def _analyze_crowding_risk(
        self,
        funding: FundingSignal,
        open_interest: OpenInterestSignal,
        relationship: PriceOiRelationship,
    ) -> CrowdingRiskSignal:
        required_missing = funding.direction == NA or open_interest.direction == NA or relationship.price_direction == NA
        if required_missing:
            return CrowdingRiskSignal()

        funding_is_elevated = funding.severity in ("elevated", "extreme")
        if funding.direction == "positive" and funding_is_elevated:
            if open_interest.direction == "increasing" and relationship.price_direction == "up":
                return CrowdingRiskSignal(
                    crowded_long_risk=True,
                    risk_direction="long",
                    reason="Crowded long risk: elevated positive funding, rising OI, and rising price.",
                )
        if funding.direction == "negative" and funding_is_elevated:
            if open_interest.direction == "increasing" and relationship.price_direction == "down":
                return CrowdingRiskSignal(
                    crowded_short_risk=True,
                    risk_direction="short",
                    reason="Crowded short risk: elevated negative funding, rising OI, and falling price.",
                )

        return CrowdingRiskSignal(
            risk_direction="none",
            reason="No crowded long or crowded short risk rule was triggered.",
        )

    def _classify_volume_confirmation(self, volume_z_score: MaybeDecimal) -> VolumeConfirmationSignal:
        if volume_z_score == NA:
            return VolumeConfirmationSignal()
        if volume_z_score >= self.volume_strong_z_threshold:
            confirmation: VolumeConfirmation = "strong confirmation"
        elif volume_z_score >= self.volume_moderate_z_threshold:
            confirmation = "moderate confirmation"
        else:
            confirmation = "no confirmation"

        return VolumeConfirmationSignal(
            volume_z_score=_quantize(volume_z_score),
            confirmation=confirmation,
            reason="Volume confirmation was classified from the supplied volume z-score.",
        )

    def _build_risk_flags(
        self,
        funding: FundingSignal,
        open_interest: OpenInterestSignal,
        relationship: PriceOiRelationship,
        crowding_risk: CrowdingRiskSignal,
        volume_confirmation: VolumeConfirmationSignal,
    ) -> DerivativesRiskFlags:
        oi_spike = (
            open_interest.oi_change_percentage != NA
            and abs(open_interest.oi_change_percentage) >= self.oi_spike_threshold_percentage
        )
        conflicting_context = (
            relationship.price_direction == "up"
            and funding.direction == "negative"
            and funding.severity in ("elevated", "extreme")
        ) or (
            relationship.price_direction == "down"
            and funding.direction == "positive"
            and funding.severity in ("elevated", "extreme")
        )
        return DerivativesRiskFlags(
            crowded_long_risk=crowding_risk.crowded_long_risk,
            crowded_short_risk=crowding_risk.crowded_short_risk,
            funding_extreme=funding.severity == "extreme",
            oi_spike=oi_spike,
            missing_funding=funding.raw_funding_rate == NA,
            missing_open_interest=open_interest.current_open_interest == NA or open_interest.previous_open_interest == NA,
            missing_volume=volume_confirmation.volume_z_score == NA,
            conflicting_context=conflicting_context,
        )

    def _score_components(
        self,
        *,
        funding: FundingSignal,
        open_interest: OpenInterestSignal,
        relationship: PriceOiRelationship,
        crowding_risk: CrowdingRiskSignal,
        volume_confirmation: VolumeConfirmationSignal,
        data_quality: DerivativesDataQuality,
    ) -> dict[str, int]:
        price_oi_points = 25 if relationship.classification != NA else 0
        funding_points = 20 if funding.raw_funding_rate != NA else 0

        oi_points = 0
        if open_interest.oi_change_percentage != NA:
            oi_points = 10
            if abs(open_interest.oi_change_percentage) >= self.oi_significant_change_threshold_percentage:
                oi_points = 20

        if volume_confirmation.confirmation == "strong confirmation":
            volume_points = 15
        elif volume_confirmation.confirmation == "moderate confirmation":
            volume_points = 10
        else:
            volume_points = 0

        if crowding_risk.risk_direction == "none":
            crowding_points = 10
        elif crowding_risk.risk_direction == NA:
            crowding_points = 5
        else:
            crowding_points = 0

        if data_quality.status == "valid":
            data_quality_points = 10
        elif data_quality.status == "partial":
            data_quality_points = 5
        else:
            data_quality_points = 0

        return {
            "price_oi_clarity": price_oi_points,
            "funding_context": funding_points,
            "oi_change_significance": oi_points,
            "volume_confirmation": volume_points,
            "crowding_risk_adjustment": crowding_points,
            "data_quality": data_quality_points,
        }


def _extract_price_change_percentage(
    source: Any | None,
    overrides: Mapping[str, Any],
    errors: list[str],
) -> MaybeDecimal:
    percentage = _extract_decimal(
        source,
        overrides,
        (
            "price_change_percentage",
            "price_change_percent",
            "price_change_pct",
            "price_change_24h_percentage",
            "price_change_24h_pct",
        ),
        "price_change_percentage",
        errors,
    )
    if percentage != NA:
        return percentage

    ratio = _extract_decimal(
        source,
        overrides,
        ("price_change_ratio", "price_change_ratio_24h", "price_change_pcnt", "price24hPcnt"),
        "price_change_ratio",
        errors,
    )
    if ratio != NA:
        return ratio * Decimal("100")

    current_price = _extract_decimal(
        source,
        overrides,
        ("current_price", "last_price", "mark_price"),
        "current_price",
        errors,
    )
    previous_price = _extract_decimal(
        source,
        overrides,
        ("previous_price", "previous_close", "prev_price"),
        "previous_price",
        errors,
    )
    if current_price == NA or previous_price == NA:
        return NA
    if previous_price == 0:
        errors.append("Cannot calculate price change percentage because previous_price is zero.")
        return NA
    return ((current_price - previous_price) / abs(previous_price)) * Decimal("100")


def _extract_decimal(
    source: Any | None,
    overrides: Mapping[str, Any],
    aliases: Sequence[str],
    label: str,
    errors: list[str],
) -> MaybeDecimal:
    value = _extract_value(source, overrides, aliases)
    if _is_missing(value):
        return NA
    try:
        return _decimal_from(value, label)
    except ValueError as exc:
        errors.append(str(exc))
        return NA


def _extract_value(source: Any | None, overrides: Mapping[str, Any], aliases: Sequence[str]) -> Any:
    for alias in aliases:
        if alias in overrides:
            return overrides[alias]
    if source is None:
        return None
    for alias in aliases:
        if isinstance(source, Mapping) and alias in source:
            return source[alias]
        if hasattr(source, alias):
            return getattr(source, alias)
    return None


def _calculate_z_score(
    current_value: Decimal,
    historical_values: Any,
    label: str,
    errors: list[str],
) -> tuple[MaybeDecimal, int]:
    if historical_values is None:
        return NA, 0
    if isinstance(historical_values, (str, bytes)) or not isinstance(historical_values, Sequence):
        errors.append(f"Malformed {label}: expected a sequence of funding rates.")
        return NA, 0

    decimals: list[Decimal] = []
    for index, value in enumerate(historical_values):
        if _is_missing(value):
            errors.append(f"Malformed {label}[{index}]: funding history cannot contain missing values.")
            return NA, len(decimals)
        try:
            decimals.append(_decimal_from(value, f"{label}[{index}]"))
        except ValueError as exc:
            errors.append(str(exc))
            return NA, len(decimals)

    if len(decimals) < 2:
        return NA, len(decimals)

    sample_size = len(decimals)
    mean = sum(decimals) / Decimal(sample_size)
    variance = sum((value - mean) ** 2 for value in decimals) / Decimal(sample_size)
    if variance == 0:
        if current_value == mean:
            return Decimal("0.00000000"), sample_size
        return NA, sample_size

    with localcontext() as context:
        context.prec = 28
        z_score = (current_value - mean) / variance.sqrt()
    return _quantize(z_score), sample_size


def _build_data_quality(
    price_change_percentage: MaybeDecimal,
    open_interest: OpenInterestSignal,
    funding: FundingSignal,
    volume_confirmation: VolumeConfirmationSignal,
    errors: Sequence[str],
) -> DerivativesDataQuality:
    missing_fields: list[str] = []
    if price_change_percentage == NA:
        missing_fields.append("price_change_percentage")
    if open_interest.current_open_interest == NA:
        missing_fields.append("current_open_interest")
    if open_interest.previous_open_interest == NA:
        missing_fields.append("previous_open_interest")
    if funding.raw_funding_rate == NA:
        missing_fields.append("funding_rate")
    if volume_confirmation.volume_z_score == NA:
        missing_fields.append("volume_z_score")

    unverified_fields = ["CVD", "liquidation_heatmap"]
    if price_change_percentage == NA:
        status: DataQualityStatus = "invalid"
        reason = "Invalid derivatives context: price data is missing."
    elif open_interest.current_open_interest == NA or open_interest.previous_open_interest == NA:
        status = "partial"
        reason = "Partial derivatives context: one or more open interest fields are missing."
    else:
        status = "valid"
        reason = "Valid derivatives context: price and open interest data are available."

    reliability: ReliabilityStatus = "Verified"
    if errors or status != "valid" or funding.raw_funding_rate == NA or volume_confirmation.volume_z_score == NA:
        reliability = "Unverified"

    return DerivativesDataQuality(
        status=status,
        reliability=reliability,
        missing_fields=tuple(missing_fields),
        unverified_fields=tuple(unverified_fields),
        reason=reason,
    )


def _active_risk_flags(flags: DerivativesRiskFlags) -> tuple[str, ...]:
    output = []
    for name in (
        "crowded_long_risk",
        "crowded_short_risk",
        "funding_extreme",
        "oi_spike",
        "missing_funding",
        "missing_open_interest",
        "missing_volume",
        "conflicting_context",
    ):
        if getattr(flags, name):
            output.append(name)
    return tuple(output)


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Malformed derivatives data at {path}: invalid decimal {value!r}.") from exc
    if not decimal.is_finite():
        raise ValueError(f"Malformed derivatives data at {path}: invalid decimal {value!r}.")
    return decimal


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


__all__ = [
    "CrowdingRiskSignal",
    "DerivativesDataQuality",
    "DerivativesOrderflowAgent",
    "DerivativesOrderflowResult",
    "DerivativesRiskFlags",
    "FundingSignal",
    "OpenInterestSignal",
    "PriceOiRelationship",
    "VolumeConfirmationSignal",
]
