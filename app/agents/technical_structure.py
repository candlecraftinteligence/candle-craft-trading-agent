from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.data.dtos import NA, MaybeDecimal, MaybeInt

DecimalLike = Decimal | int | str
SignalDirection = Literal["bullish", "bearish", "N/A"]
TrendContext = Literal["bullish", "bearish", "neutral", "N/A"]
RangePosition = Literal["lower", "middle", "upper", "N/A"]
SwingKind = Literal["high", "low"]

OUTPUT_QUANT = Decimal("0.00000001")


class SwingPoint(BaseModel):
    kind: SwingKind
    index: int
    confirmed_at_index: int
    timestamp: MaybeInt = NA
    price: Decimal

    model_config = ConfigDict(frozen=True)


class SweepSignal(BaseModel):
    is_present: bool = False
    direction: SignalDirection = NA
    candle_index: MaybeInt = NA
    swing_index: MaybeInt = NA
    level: MaybeDecimal = NA
    reason: str = "No liquidity sweep detected."

    model_config = ConfigDict(frozen=True)


class BosSignal(BaseModel):
    is_present: bool = False
    direction: SignalDirection = NA
    candle_index: MaybeInt = NA
    swing_index: MaybeInt = NA
    level: MaybeDecimal = NA
    reason: str = "No break of structure detected."

    model_config = ConfigDict(frozen=True)


class ChochSignal(BaseModel):
    is_present: bool = False
    direction: SignalDirection = NA
    candle_index: MaybeInt = NA
    swing_index: MaybeInt = NA
    level: MaybeDecimal = NA
    prior_context: TrendContext = NA
    reason: str = "No change of character detected."

    model_config = ConfigDict(frozen=True)


class VolumeAnomalySignal(BaseModel):
    is_present: bool = False
    status: Literal["confirmed", "none", "N/A"] = NA
    z_score: MaybeDecimal = NA
    threshold: Decimal = Decimal("2.0")
    reason: str = "Volume anomaly is N/A because volume data is missing."

    model_config = ConfigDict(frozen=True)


class TechnicalStructureResult(BaseModel):
    is_valid: bool
    data_quality: Literal["valid", "invalid"]
    errors: tuple[str, ...] = ()
    candle_count: int = 0
    lookback: int
    atr: MaybeDecimal = NA
    ema_50: MaybeDecimal = NA
    ema_200: MaybeDecimal = NA
    volume_z_score: MaybeDecimal = NA
    trend_context: TrendContext = NA
    recent_range_high: MaybeDecimal = NA
    recent_range_low: MaybeDecimal = NA
    nearest_support: MaybeDecimal = NA
    nearest_resistance: MaybeDecimal = NA
    range_position: RangePosition = NA
    swing_points: tuple[SwingPoint, ...] = ()
    swing_highs: tuple[SwingPoint, ...] = ()
    swing_lows: tuple[SwingPoint, ...] = ()
    sweep: SweepSignal = SweepSignal()
    bos: BosSignal = BosSignal()
    choch: ChochSignal = ChochSignal()
    volume_anomaly: VolumeAnomalySignal = VolumeAnomalySignal()
    structure_score: int = 0

    model_config = ConfigDict(frozen=True)


@dataclass(frozen=True)
class _Candle:
    index: int
    timestamp: MaybeInt
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: MaybeDecimal


class TechnicalStructureAgent:
    """Deterministic technical structure analysis for already-collected OHLCV candles.

    CHoCH is intentionally simple: a bullish CHoCH requires the prior candle's EMA
    context to be bearish and the latest close to break a previously confirmed
    swing high. A bearish CHoCH is the inverse. The agent does not produce trade
    recommendations or order instructions.
    """

    def __init__(
        self,
        *,
        lookback: int = 2,
        atr_period: int = 14,
        ema_fast_period: int = 50,
        ema_slow_period: int = 200,
        range_window: int = 50,
        volume_zscore_window: int = 20,
        volume_zscore_threshold: DecimalLike = Decimal("2.0"),
    ) -> None:
        if lookback < 1:
            raise ValueError("lookback must be at least 1")
        if atr_period < 1:
            raise ValueError("atr_period must be at least 1")
        if ema_fast_period < 1:
            raise ValueError("ema_fast_period must be at least 1")
        if ema_slow_period < ema_fast_period:
            raise ValueError("ema_slow_period must be greater than or equal to ema_fast_period")
        if range_window < 1:
            raise ValueError("range_window must be at least 1")
        if volume_zscore_window < 2:
            raise ValueError("volume_zscore_window must be at least 2")

        self.lookback = lookback
        self.atr_period = atr_period
        self.ema_fast_period = ema_fast_period
        self.ema_slow_period = ema_slow_period
        self.range_window = range_window
        self.volume_zscore_window = volume_zscore_window
        self.volume_zscore_threshold = _decimal_from(volume_zscore_threshold, "volume_zscore_threshold")

    @property
    def min_required_candles(self) -> int:
        return max(
            self.atr_period + 1,
            self.ema_slow_period,
            self.lookback * 2 + 2,
            self.range_window,
        )

    def analyze(self, candles: Sequence[Any]) -> TechnicalStructureResult:
        normalized, errors = _normalize_candles(candles)
        if errors:
            return self._invalid_result(candle_count=len(candles), errors=errors)

        candle_count = len(normalized)
        if candle_count < self.min_required_candles:
            return self._invalid_result(
                candle_count=candle_count,
                errors=(
                    f"Not enough candles: received {candle_count}, "
                    f"required at least {self.min_required_candles}.",
                ),
            )

        closes = tuple(candle.close for candle in normalized)
        atr = calculate_atr(normalized, self.atr_period)
        ema_fast_series = calculate_ema_series(closes, self.ema_fast_period)
        ema_slow_series = calculate_ema_series(closes, self.ema_slow_period)
        ema_fast = ema_fast_series[-1]
        ema_slow = ema_slow_series[-1]
        trend_context = _trend_context(normalized[-1].close, ema_fast, ema_slow)
        prior_context = _trend_context(normalized[-2].close, ema_fast_series[-2], ema_slow_series[-2])

        volume_z_score = calculate_volume_z_score(
            tuple(candle.volume for candle in normalized),
            self.volume_zscore_window,
        )
        volume_anomaly = _detect_volume_anomaly(volume_z_score, self.volume_zscore_threshold)

        swing_points = _detect_swings(normalized, self.lookback)
        swing_highs = tuple(point for point in swing_points if point.kind == "high")
        swing_lows = tuple(point for point in swing_points if point.kind == "low")
        latest_index = candle_count - 1
        previous_high = _last_confirmed_before(swing_highs, latest_index)
        previous_low = _last_confirmed_before(swing_lows, latest_index)

        recent_range_high, recent_range_low, range_position = _detect_recent_range(
            normalized,
            self.range_window,
        )
        nearest_support, nearest_resistance = _nearest_levels(
            swing_highs,
            swing_lows,
            normalized[-1].close,
            latest_index,
        )

        sweep = _detect_sweep(normalized[-1], previous_high, previous_low)
        bos = _detect_bos(normalized[-1], previous_high, previous_low)
        choch = _detect_choch(normalized[-1], previous_high, previous_low, prior_context)
        score = _score_structure(
            trend_context=trend_context,
            sweep=sweep,
            bos=bos,
            choch=choch,
            volume_anomaly=volume_anomaly,
            range_position=range_position,
        )

        return TechnicalStructureResult(
            is_valid=True,
            data_quality="valid",
            candle_count=candle_count,
            lookback=self.lookback,
            atr=atr,
            ema_50=ema_fast,
            ema_200=ema_slow,
            volume_z_score=volume_z_score,
            trend_context=trend_context,
            recent_range_high=recent_range_high,
            recent_range_low=recent_range_low,
            nearest_support=nearest_support,
            nearest_resistance=nearest_resistance,
            range_position=range_position,
            swing_points=swing_points,
            swing_highs=swing_highs,
            swing_lows=swing_lows,
            sweep=sweep,
            bos=bos,
            choch=choch,
            volume_anomaly=volume_anomaly,
            structure_score=score,
        )

    def _invalid_result(self, *, candle_count: int, errors: tuple[str, ...]) -> TechnicalStructureResult:
        return TechnicalStructureResult(
            is_valid=False,
            data_quality="invalid",
            errors=errors,
            candle_count=candle_count,
            lookback=self.lookback,
        )


def calculate_ema(values: Sequence[DecimalLike], period: int) -> MaybeDecimal:
    decimals = tuple(_decimal_from(value, f"values[{index}]") for index, value in enumerate(values))
    series = calculate_ema_series(decimals, period)
    return series[-1] if series else NA


def calculate_ema_series(values: Sequence[Decimal], period: int) -> tuple[MaybeDecimal, ...]:
    if period < 1:
        raise ValueError("period must be at least 1")
    if len(values) < period:
        return tuple(NA for _ in values)

    multiplier = Decimal("2") / Decimal(period + 1)
    output: list[MaybeDecimal] = [NA for _ in range(period - 1)]
    ema = sum(values[:period]) / Decimal(period)
    output.append(_quantize(ema))

    for value in values[period:]:
        ema = (value - ema) * multiplier + ema
        output.append(_quantize(ema))

    return tuple(output)


def calculate_atr(candles: Sequence[_Candle], period: int = 14) -> MaybeDecimal:
    if period < 1:
        raise ValueError("period must be at least 1")
    if len(candles) < period + 1:
        return NA

    true_ranges: list[Decimal] = []
    for index in range(1, len(candles)):
        current = candles[index]
        previous = candles[index - 1]
        true_ranges.append(
            max(
                current.high - current.low,
                abs(current.high - previous.close),
                abs(current.low - previous.close),
            )
        )

    if len(true_ranges) < period:
        return NA
    return _quantize(sum(true_ranges[-period:]) / Decimal(period))


def calculate_volume_z_score(volumes: Sequence[MaybeDecimal], window: int = 20) -> MaybeDecimal:
    if window < 2:
        raise ValueError("window must be at least 2")
    if len(volumes) < window:
        return NA

    sample = volumes[-window:]
    if any(volume == NA for volume in sample):
        return NA

    decimal_sample = tuple(volume for volume in sample if volume != NA)
    mean = sum(decimal_sample) / Decimal(window)
    variance = sum((volume - mean) ** 2 for volume in decimal_sample) / Decimal(window)
    if variance == 0:
        return Decimal("0.00000000")

    with localcontext() as context:
        context.prec = 28
        z_score = (decimal_sample[-1] - mean) / variance.sqrt()
    return _quantize(z_score)


def _normalize_candles(candles: Sequence[Any]) -> tuple[tuple[_Candle, ...], tuple[str, ...]]:
    if isinstance(candles, (str, bytes)) or not isinstance(candles, Sequence):
        return (), ("Malformed candle data: expected a sequence of candle objects.",)

    normalized: list[_Candle] = []
    errors: list[str] = []

    for index, candle in enumerate(candles):
        required: dict[str, Decimal] = {}
        for field in ("open", "high", "low", "close"):
            value = _get_field(candle, field)
            if _is_missing(value):
                errors.append(f"Missing required OHLC field candles[{index}].{field}.")
                continue
            try:
                required[field] = _decimal_from(value, f"candles[{index}].{field}")
            except ValueError as exc:
                errors.append(str(exc))

        if len(required) != 4:
            continue
        if required["high"] < required["low"]:
            errors.append(f"Malformed candle candles[{index}]: high is lower than low.")
            continue

        timestamp = _get_field(candle, "timestamp")
        volume = _get_field(candle, "volume")
        try:
            normalized_volume = NA if _is_missing(volume) else _decimal_from(volume, f"candles[{index}].volume")
        except ValueError as exc:
            errors.append(str(exc))
            continue

        if normalized_volume != NA and normalized_volume < 0:
            errors.append(f"Malformed candle candles[{index}].volume: volume cannot be negative.")
            continue

        normalized.append(
            _Candle(
                index=index,
                timestamp=_normalize_timestamp(timestamp),
                open=required["open"],
                high=required["high"],
                low=required["low"],
                close=required["close"],
                volume=normalized_volume,
            )
        )

    return tuple(normalized), tuple(errors)


def _detect_swings(candles: Sequence[_Candle], lookback: int) -> tuple[SwingPoint, ...]:
    points: list[SwingPoint] = []
    for index in range(lookback, len(candles) - lookback):
        current = candles[index]
        left = candles[index - lookback : index]
        right = candles[index + 1 : index + lookback + 1]
        if all(current.high > candle.high for candle in (*left, *right)):
            points.append(
                SwingPoint(
                    kind="high",
                    index=current.index,
                    confirmed_at_index=current.index + lookback,
                    timestamp=current.timestamp,
                    price=current.high,
                )
            )
        if all(current.low < candle.low for candle in (*left, *right)):
            points.append(
                SwingPoint(
                    kind="low",
                    index=current.index,
                    confirmed_at_index=current.index + lookback,
                    timestamp=current.timestamp,
                    price=current.low,
                )
            )
    return tuple(points)


def _detect_recent_range(candles: Sequence[_Candle], window: int) -> tuple[MaybeDecimal, MaybeDecimal, RangePosition]:
    sample = candles[-window:]
    range_high = max(candle.high for candle in sample)
    range_low = min(candle.low for candle in sample)
    if range_high == range_low:
        return _quantize(range_high), _quantize(range_low), NA

    latest_close = candles[-1].close
    position = (latest_close - range_low) / (range_high - range_low)
    if position <= Decimal("0.25"):
        range_position: RangePosition = "lower"
    elif position >= Decimal("0.75"):
        range_position = "upper"
    else:
        range_position = "middle"
    return _quantize(range_high), _quantize(range_low), range_position


def _nearest_levels(
    swing_highs: Sequence[SwingPoint],
    swing_lows: Sequence[SwingPoint],
    close: Decimal,
    latest_index: int,
) -> tuple[MaybeDecimal, MaybeDecimal]:
    confirmed_highs = [point for point in swing_highs if point.confirmed_at_index < latest_index]
    confirmed_lows = [point for point in swing_lows if point.confirmed_at_index < latest_index]
    supports = [point.price for point in confirmed_lows if point.price <= close]
    resistances = [point.price for point in confirmed_highs if point.price >= close]
    support = _quantize(max(supports)) if supports else NA
    resistance = _quantize(min(resistances)) if resistances else NA
    return support, resistance


def _detect_sweep(
    latest: _Candle,
    previous_high: SwingPoint | None,
    previous_low: SwingPoint | None,
) -> SweepSignal:
    if previous_low and latest.low < previous_low.price and latest.close > previous_low.price:
        return SweepSignal(
            is_present=True,
            direction="bullish",
            candle_index=latest.index,
            swing_index=previous_low.index,
            level=previous_low.price,
            reason="Latest candle took a previous swing low and closed back above it.",
        )
    if previous_high and latest.high > previous_high.price and latest.close < previous_high.price:
        return SweepSignal(
            is_present=True,
            direction="bearish",
            candle_index=latest.index,
            swing_index=previous_high.index,
            level=previous_high.price,
            reason="Latest candle took a previous swing high and closed back below it.",
        )
    return SweepSignal()


def _detect_bos(
    latest: _Candle,
    previous_high: SwingPoint | None,
    previous_low: SwingPoint | None,
) -> BosSignal:
    if previous_high and latest.close > previous_high.price:
        return BosSignal(
            is_present=True,
            direction="bullish",
            candle_index=latest.index,
            swing_index=previous_high.index,
            level=previous_high.price,
            reason="Latest candle closed above a previously confirmed swing high.",
        )
    if previous_low and latest.close < previous_low.price:
        return BosSignal(
            is_present=True,
            direction="bearish",
            candle_index=latest.index,
            swing_index=previous_low.index,
            level=previous_low.price,
            reason="Latest candle closed below a previously confirmed swing low.",
        )
    return BosSignal()


def _detect_choch(
    latest: _Candle,
    previous_high: SwingPoint | None,
    previous_low: SwingPoint | None,
    prior_context: TrendContext,
) -> ChochSignal:
    if prior_context == "bearish" and previous_high and latest.close > previous_high.price:
        return ChochSignal(
            is_present=True,
            direction="bullish",
            candle_index=latest.index,
            swing_index=previous_high.index,
            level=previous_high.price,
            prior_context=prior_context,
            reason="Prior context was bearish and latest close broke a previous structure high.",
        )
    if prior_context == "bullish" and previous_low and latest.close < previous_low.price:
        return ChochSignal(
            is_present=True,
            direction="bearish",
            candle_index=latest.index,
            swing_index=previous_low.index,
            level=previous_low.price,
            prior_context=prior_context,
            reason="Prior context was bullish and latest close broke a previous structure low.",
        )
    return ChochSignal(prior_context=prior_context)


def _detect_volume_anomaly(z_score: MaybeDecimal, threshold: Decimal) -> VolumeAnomalySignal:
    if z_score == NA:
        return VolumeAnomalySignal(threshold=threshold)
    if z_score >= threshold:
        return VolumeAnomalySignal(
            is_present=True,
            status="confirmed",
            z_score=z_score,
            threshold=threshold,
            reason="Latest volume z-score met or exceeded the anomaly threshold.",
        )
    return VolumeAnomalySignal(
        status="none",
        z_score=z_score,
        threshold=threshold,
        reason="Latest volume z-score did not meet the anomaly threshold.",
    )


def _score_structure(
    *,
    trend_context: TrendContext,
    sweep: SweepSignal,
    bos: BosSignal,
    choch: ChochSignal,
    volume_anomaly: VolumeAnomalySignal,
    range_position: RangePosition,
) -> int:
    score = 10
    if trend_context in ("bullish", "bearish"):
        score += 20
    if sweep.is_present:
        score += 20
    if bos.is_present or choch.is_present:
        score += 25
    if volume_anomaly.is_present:
        score += 15
    if range_position in ("lower", "upper"):
        score += 10
    return min(score, 100)


def _last_confirmed_before(points: Sequence[SwingPoint], latest_index: int) -> SwingPoint | None:
    candidates = [point for point in points if point.confirmed_at_index < latest_index]
    return candidates[-1] if candidates else None


def _trend_context(close: Decimal, ema_fast: MaybeDecimal, ema_slow: MaybeDecimal) -> TrendContext:
    if ema_fast == NA or ema_slow == NA:
        return NA
    if close > ema_fast > ema_slow:
        return "bullish"
    if close < ema_fast < ema_slow:
        return "bearish"
    return "neutral"


def _get_field(candle: Any, field: str) -> Any:
    if isinstance(candle, Mapping):
        return candle.get(field)
    return getattr(candle, field, None)


def _normalize_timestamp(value: Any) -> MaybeInt:
    if _is_missing(value):
        return NA
    try:
        return int(value)
    except (TypeError, ValueError):
        return NA


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Malformed candle data at {path}: invalid decimal {value!r}.") from exc
    if not decimal.is_finite():
        raise ValueError(f"Malformed candle data at {path}: invalid decimal {value!r}.")
    return decimal


def _is_missing(value: Any) -> bool:
    return value is None or value == ""


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)
