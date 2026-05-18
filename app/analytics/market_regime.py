from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data.dtos import NA, MaybeDecimal

OUTPUT_QUANT = Decimal("0.00000001")
MIN_REQUIRED_CANDLES = 40
RECENT_RANGE_WINDOW = 20
BASELINE_RANGE_WINDOW = 60
ATR_WINDOW = 14
EMA_FAST_PERIOD = 21
EMA_SLOW_PERIOD = 50


class RegimeState(str, Enum):
    TREND_EXPANSION = "TREND_EXPANSION"
    CHOP = "CHOP"
    COMPRESSION = "COMPRESSION"
    PANIC_VOLATILITY = "PANIC_VOLATILITY"
    LOW_VOL_DRIFT = "LOW_VOL_DRIFT"
    MIXED = "MIXED"
    DATA_INCOMPLETE = "DATA_INCOMPLETE"


class RegimeRiskLevel(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    EXTREME = "EXTREME"
    NA = "N/A"


class RegimeAdjustment(BaseModel):
    allow_scalps: bool = True
    allow_swings: bool = True
    allow_challenge: bool = True
    min_quality_score_adjustment: int = Field(default=0, ge=0)
    min_rr_adjustment: Decimal = Decimal("0")
    risk_multiplier: Decimal = Decimal("1")
    explanation: str = NA

    model_config = ConfigDict(frozen=True)

    @field_validator("min_rr_adjustment", "risk_multiplier", mode="before")
    @classmethod
    def _normalize_decimal(cls, value: Any) -> Decimal:
        if _is_missing(value):
            return Decimal("0")
        return _quantize(_decimal_from(value, "regime adjustment"))


class MarketRegimeInput(BaseModel):
    btc_candles: tuple[Any, ...] = ()
    eth_candles: tuple[Any, ...] = ()
    total_proxy_candles: tuple[Any, ...] = ()
    total2_proxy_candles: tuple[Any, ...] = ()
    scanned_symbols: int = 0
    bullish_bias_pct: MaybeDecimal = NA
    bearish_bias_pct: MaybeDecimal = NA
    valid_sweep_pct: MaybeDecimal = NA
    confirmation_pct: MaybeDecimal = NA
    failed_confirmation_pct: MaybeDecimal = NA
    volatility_expansion_vs_average: MaybeDecimal = NA
    risk_mode: Literal["conservative", "balanced", "aggressive"] = "balanced"
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
        mode="before",
    )
    @classmethod
    def _normalize_maybe_decimal(cls, value: Any) -> MaybeDecimal:
        if _is_missing(value):
            return NA
        return _quantize(_decimal_from(value, "market regime input"))

    @field_validator("missing_data", "unverified_data", mode="before")
    @classmethod
    def _normalize_string_tuple(cls, value: Any) -> tuple[str, ...]:
        return _sequence_values(value)


class MarketRegimeResult(BaseModel):
    enabled: bool = True
    state: RegimeState
    risk_level: RegimeRiskLevel
    adjustment: RegimeAdjustment
    metrics: dict[str, Any] = Field(default_factory=dict)
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


class _Candle(BaseModel):
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal

    model_config = ConfigDict(frozen=True)


class _MarketMetrics(BaseModel):
    symbol: str
    candle_count: int
    latest_close: MaybeDecimal = NA
    ema_slope_pct: MaybeDecimal = NA
    trend_direction: Literal["bullish", "bearish", "neutral"] = "neutral"
    drift_direction: Literal["bullish", "bearish", "neutral"] = "neutral"
    atr_pct: MaybeDecimal = NA
    atr_vs_average: MaybeDecimal = NA
    realized_range_pct: MaybeDecimal = NA
    range_vs_average: MaybeDecimal = NA
    missing_data: tuple[str, ...] = ()

    model_config = ConfigDict(frozen=True)


def evaluate_market_regime(regime_input: MarketRegimeInput | Mapping[str, Any] | None = None, **overrides: Any) -> MarketRegimeResult:
    data = _normalize_input(regime_input, overrides)
    btc = _market_metrics("BTCUSDT", data.btc_candles)
    eth = _market_metrics("ETHUSDT", data.eth_candles)
    missing_data = _unique_strings((*data.missing_data, *btc.missing_data, *eth.missing_data, *_proxy_missing_data(data)))
    metrics = _metrics_payload(data, btc, eth)

    if btc.missing_data or eth.missing_data:
        return MarketRegimeResult(
            state=RegimeState.DATA_INCOMPLETE,
            risk_level=RegimeRiskLevel.NA,
            adjustment=_adjustment_for_state(RegimeState.DATA_INCOMPLETE, data.risk_mode),
            metrics=metrics,
            missing_data=missing_data,
            unverified_data=data.unverified_data,
            warnings=("Required BTC/ETH candle data is incomplete; market regime is N/A.",),
        )

    state = _classify_state(data, btc, eth)
    risk_level = _risk_level_for_state(state)
    adjustment = _adjustment_for_state(state, data.risk_mode)
    warnings = _warnings_for_state(state, risk_level, adjustment)
    return MarketRegimeResult(
        state=state,
        risk_level=risk_level,
        adjustment=adjustment,
        metrics=metrics,
        missing_data=missing_data,
        unverified_data=data.unverified_data,
        warnings=warnings,
    )


def default_market_regime_result() -> MarketRegimeResult:
    return MarketRegimeResult(
        state=RegimeState.DATA_INCOMPLETE,
        risk_level=RegimeRiskLevel.NA,
        adjustment=_adjustment_for_state(RegimeState.DATA_INCOMPLETE, "balanced"),
        metrics={},
        missing_data=("market_regime: N/A",),
        warnings=("Market regime has not been evaluated.",),
    )


def disabled_market_regime_result() -> MarketRegimeResult:
    return MarketRegimeResult(
        enabled=False,
        state=RegimeState.DATA_INCOMPLETE,
        risk_level=RegimeRiskLevel.NA,
        adjustment=RegimeAdjustment(
            allow_scalps=True,
            allow_swings=True,
            allow_challenge=True,
            min_quality_score_adjustment=0,
            min_rr_adjustment=Decimal("0"),
            risk_multiplier=Decimal("1"),
            explanation="Market regime filter disabled.",
        ),
        metrics={},
        missing_data=("market_regime: N/A",),
        warnings=("Market regime filter disabled.",),
    )


def _normalize_input(
    regime_input: MarketRegimeInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> MarketRegimeInput:
    if regime_input is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(regime_input, MarketRegimeInput):
        raw = regime_input.model_dump()
        raw.update(overrides)
    else:
        raw = dict(regime_input)
        raw.update(overrides)
    return MarketRegimeInput.model_validate(raw)


def _market_metrics(symbol: str, raw_candles: Sequence[Any]) -> _MarketMetrics:
    candles = _normalized_candles(raw_candles)
    missing_data: list[str] = []
    if len(candles) < MIN_REQUIRED_CANDLES:
        missing_data.append(f"{symbol}_candles: N/A")
        return _MarketMetrics(symbol=symbol, candle_count=len(candles), missing_data=tuple(missing_data))

    closes = tuple(candle.close for candle in candles)
    ranges = tuple(max(candle.high - candle.low, Decimal("0")) for candle in candles)
    true_ranges = _true_ranges(candles)
    latest_close = closes[-1]
    recent_atr = _average(true_ranges[-ATR_WINDOW:])
    baseline_atr = _average(true_ranges[-BASELINE_RANGE_WINDOW:-ATR_WINDOW]) or _average(true_ranges[:-ATR_WINDOW])
    recent_range = _recent_range_pct(candles[-RECENT_RANGE_WINDOW:], latest_close)
    baseline_range = _baseline_range_pct(candles, latest_close)
    ema_fast = _ema_series(closes, EMA_FAST_PERIOD)
    ema_slow = _ema_series(closes, EMA_SLOW_PERIOD)
    slope_anchor_index = max(0, len(ema_fast) - 11)
    ema_slope_pct = (
        _quantize((ema_fast[-1] - ema_fast[slope_anchor_index]) / abs(latest_close) * Decimal("100"))
        if latest_close != 0
        else NA
    )
    trend_direction = _trend_direction(
        close=latest_close,
        ema_fast=ema_fast[-1],
        ema_slow=ema_slow[-1],
        ema_slope_pct=ema_slope_pct,
    )
    drift_direction = _drift_direction(close=latest_close, ema_slow=ema_slow[-1], ema_slope_pct=ema_slope_pct)

    atr_pct = _quantize(recent_atr / abs(latest_close) * Decimal("100")) if latest_close != 0 else NA
    return _MarketMetrics(
        symbol=symbol,
        candle_count=len(candles),
        latest_close=_quantize(latest_close),
        ema_slope_pct=ema_slope_pct,
        trend_direction=trend_direction,
        drift_direction=drift_direction,
        atr_pct=atr_pct,
        atr_vs_average=_ratio_or_na(recent_atr, baseline_atr),
        realized_range_pct=recent_range,
        range_vs_average=_ratio_or_na(recent_range, baseline_range),
        missing_data=(),
    )


def _classify_state(data: MarketRegimeInput, btc: _MarketMetrics, eth: _MarketMetrics) -> RegimeState:
    volatility_ratio = _max_decimal(
        btc.range_vs_average,
        eth.range_vs_average,
        btc.atr_vs_average,
        eth.atr_vs_average,
        data.volatility_expansion_vs_average,
    )
    range_pct = _max_decimal(btc.realized_range_pct, eth.realized_range_pct)
    atr_pct = _max_decimal(btc.atr_pct, eth.atr_pct)
    alignment = _market_alignment(btc, eth)
    drift_alignment = _market_drift_alignment(btc, eth)

    if volatility_ratio >= Decimal("2.25") or (volatility_ratio >= Decimal("1.90") and range_pct >= Decimal("7")):
        return RegimeState.PANIC_VOLATILITY
    if volatility_ratio <= Decimal("0.65") and atr_pct <= Decimal("1.25") and range_pct <= Decimal("4"):
        return RegimeState.COMPRESSION
    if alignment in ("bullish", "bearish") and _breadth_supports(data, alignment) and Decimal("0.75") <= volatility_ratio <= Decimal("1.85"):
        return RegimeState.TREND_EXPANSION
    if _chop_detected(data, btc, eth, volatility_ratio):
        return RegimeState.CHOP
    if volatility_ratio <= Decimal("0.90") and drift_alignment in ("bullish", "bearish"):
        return RegimeState.LOW_VOL_DRIFT
    return RegimeState.MIXED


def _risk_level_for_state(state: RegimeState) -> RegimeRiskLevel:
    return {
        RegimeState.TREND_EXPANSION: RegimeRiskLevel.LOW,
        RegimeState.LOW_VOL_DRIFT: RegimeRiskLevel.MEDIUM,
        RegimeState.COMPRESSION: RegimeRiskLevel.MEDIUM,
        RegimeState.MIXED: RegimeRiskLevel.MEDIUM,
        RegimeState.CHOP: RegimeRiskLevel.HIGH,
        RegimeState.PANIC_VOLATILITY: RegimeRiskLevel.EXTREME,
        RegimeState.DATA_INCOMPLETE: RegimeRiskLevel.NA,
    }[state]


def _adjustment_for_state(state: RegimeState, risk_mode: str) -> RegimeAdjustment:
    if state == RegimeState.TREND_EXPANSION:
        adjustment = RegimeAdjustment(explanation="Trend expansion detected; market context supports normal filtering.")
    elif state == RegimeState.PANIC_VOLATILITY:
        adjustment = RegimeAdjustment(
            allow_scalps=False,
            allow_swings=True,
            allow_challenge=False,
            min_quality_score_adjustment=12,
            min_rr_adjustment=Decimal("0.5"),
            risk_multiplier=Decimal("0.5"),
            explanation="Panic volatility detected; require cleaner structure and larger RR.",
        )
    elif state == RegimeState.CHOP:
        adjustment = RegimeAdjustment(
            allow_scalps=False,
            allow_swings=True,
            allow_challenge=False,
            min_quality_score_adjustment=10,
            min_rr_adjustment=Decimal("0.25"),
            risk_multiplier=Decimal("0.5"),
            explanation="Choppy regime; sweep failures are more likely.",
        )
    elif state == RegimeState.COMPRESSION:
        adjustment = RegimeAdjustment(
            allow_scalps=False,
            allow_swings=True,
            allow_challenge=False,
            min_quality_score_adjustment=8,
            min_rr_adjustment=Decimal("0.25"),
            risk_multiplier=Decimal("0.65"),
            explanation="Compression detected; require expansion before trusting follow-through.",
        )
    elif state == RegimeState.LOW_VOL_DRIFT:
        adjustment = RegimeAdjustment(
            allow_scalps=False,
            allow_swings=True,
            allow_challenge=True,
            min_quality_score_adjustment=5,
            min_rr_adjustment=Decimal("0.25"),
            risk_multiplier=Decimal("0.75"),
            explanation="Low-volatility drift detected; prioritize cleaner swing structure.",
        )
    elif state == RegimeState.MIXED:
        adjustment = RegimeAdjustment(
            allow_scalps=False,
            allow_swings=True,
            allow_challenge=False,
            min_quality_score_adjustment=8,
            min_rr_adjustment=Decimal("0.25"),
            risk_multiplier=Decimal("0.65"),
            explanation="Mixed BTC/ETH or breadth context; require stronger confirmation.",
        )
    else:
        adjustment = RegimeAdjustment(
            explanation="Market regime is N/A because required candle data is incomplete.",
        )

    if risk_mode == "conservative":
        return adjustment.model_copy(
            update={
                "allow_scalps": adjustment.allow_scalps and state == RegimeState.TREND_EXPANSION,
                "allow_challenge": adjustment.allow_challenge and state in (RegimeState.TREND_EXPANSION, RegimeState.LOW_VOL_DRIFT),
                "min_quality_score_adjustment": adjustment.min_quality_score_adjustment + (0 if state == RegimeState.DATA_INCOMPLETE else 5),
                "min_rr_adjustment": _quantize(adjustment.min_rr_adjustment + (Decimal("0") if state == RegimeState.DATA_INCOMPLETE else Decimal("0.25"))),
                "risk_multiplier": min(adjustment.risk_multiplier, Decimal("0.75")) if state != RegimeState.DATA_INCOMPLETE else adjustment.risk_multiplier,
            }
        )
    if risk_mode == "aggressive" and state not in (RegimeState.PANIC_VOLATILITY, RegimeState.DATA_INCOMPLETE):
        return adjustment.model_copy(
            update={
                "allow_challenge": adjustment.allow_challenge or state in (RegimeState.COMPRESSION, RegimeState.LOW_VOL_DRIFT),
                "min_quality_score_adjustment": max(0, adjustment.min_quality_score_adjustment - 3),
                "min_rr_adjustment": max(Decimal("0"), _quantize(adjustment.min_rr_adjustment - Decimal("0.25"))),
                "risk_multiplier": min(Decimal("1"), _quantize(adjustment.risk_multiplier + Decimal("0.15"))),
            }
        )
    return adjustment


def _warnings_for_state(
    state: RegimeState,
    risk_level: RegimeRiskLevel,
    adjustment: RegimeAdjustment,
) -> tuple[str, ...]:
    if risk_level in (RegimeRiskLevel.HIGH, RegimeRiskLevel.EXTREME):
        return (f"{state.value} regime risk is {risk_level.value}: {adjustment.explanation}",)
    if state in (RegimeState.COMPRESSION, RegimeState.MIXED, RegimeState.LOW_VOL_DRIFT):
        return (adjustment.explanation,)
    return ()


def _metrics_payload(data: MarketRegimeInput, btc: _MarketMetrics, eth: _MarketMetrics) -> dict[str, Any]:
    return {
        "btc": btc.model_dump(mode="json"),
        "eth": eth.model_dump(mode="json"),
        "total_proxy": "N/A" if not data.total_proxy_candles else "available",
        "total2_proxy": "N/A" if not data.total2_proxy_candles else "available",
        "scan_breadth": {
            "scanned_symbols": data.scanned_symbols,
            "bullish_bias_pct": data.bullish_bias_pct,
            "bearish_bias_pct": data.bearish_bias_pct,
            "valid_sweep_pct": data.valid_sweep_pct,
            "confirmation_pct": data.confirmation_pct,
            "failed_confirmation_pct": data.failed_confirmation_pct,
        },
        "volatility_expansion_vs_average": data.volatility_expansion_vs_average,
    }


def _proxy_missing_data(data: MarketRegimeInput) -> tuple[str, ...]:
    missing = []
    if not data.total_proxy_candles:
        missing.append("TOTAL_proxy: N/A")
    if not data.total2_proxy_candles:
        missing.append("TOTAL2_proxy: N/A")
    return tuple(missing)


def _chop_detected(data: MarketRegimeInput, btc: _MarketMetrics, eth: _MarketMetrics, volatility_ratio: Decimal) -> bool:
    failed_confirmation = _decimal_or_zero(data.failed_confirmation_pct)
    valid_sweeps = _decimal_or_zero(data.valid_sweep_pct)
    confirmation = _decimal_or_zero(data.confirmation_pct)
    breadth_diff = abs(_decimal_or_zero(data.bullish_bias_pct) - _decimal_or_zero(data.bearish_bias_pct))
    breadth_mixed = (
        data.bullish_bias_pct != NA
        and data.bearish_bias_pct != NA
        and Decimal("30") <= _decimal_or_zero(data.bullish_bias_pct) <= Decimal("70")
        and Decimal("30") <= _decimal_or_zero(data.bearish_bias_pct) <= Decimal("70")
        and breadth_diff <= Decimal("20")
    )
    failed_follow_through = valid_sweeps >= Decimal("20") and (
        failed_confirmation >= Decimal("45") or confirmation <= Decimal("35")
    )
    mixed_major_trends = _market_alignment(btc, eth) == "mixed"
    return (failed_follow_through or breadth_mixed or mixed_major_trends) and Decimal("0.65") <= volatility_ratio <= Decimal("1.85")


def _market_alignment(btc: _MarketMetrics, eth: _MarketMetrics) -> Literal["bullish", "bearish", "mixed", "neutral"]:
    directions = (btc.trend_direction, eth.trend_direction)
    if directions == ("bullish", "bullish"):
        return "bullish"
    if directions == ("bearish", "bearish"):
        return "bearish"
    if "bullish" in directions and "bearish" in directions:
        return "mixed"
    return "neutral"


def _market_drift_alignment(btc: _MarketMetrics, eth: _MarketMetrics) -> Literal["bullish", "bearish", "mixed", "neutral"]:
    directions = (btc.drift_direction, eth.drift_direction)
    if directions == ("bullish", "bullish"):
        return "bullish"
    if directions == ("bearish", "bearish"):
        return "bearish"
    if "bullish" in directions and "bearish" in directions:
        return "mixed"
    return "neutral"


def _breadth_supports(data: MarketRegimeInput, alignment: str) -> bool:
    if alignment == "bullish":
        return data.bullish_bias_pct == NA or _decimal_or_zero(data.bullish_bias_pct) >= Decimal("55")
    if alignment == "bearish":
        return data.bearish_bias_pct == NA or _decimal_or_zero(data.bearish_bias_pct) >= Decimal("55")
    return False


def _normalized_candles(raw_candles: Sequence[Any]) -> tuple[_Candle, ...]:
    candles: list[_Candle] = []
    for item in raw_candles:
        try:
            candle = _Candle(
                open=_decimal_from(_field(item, "open"), "candle.open"),
                high=_decimal_from(_field(item, "high"), "candle.high"),
                low=_decimal_from(_field(item, "low"), "candle.low"),
                close=_decimal_from(_field(item, "close"), "candle.close"),
            )
        except ValueError:
            continue
        candles.append(candle)
    return tuple(candles)


def _true_ranges(candles: Sequence[_Candle]) -> tuple[Decimal, ...]:
    if not candles:
        return ()
    ranges = [max(candles[0].high - candles[0].low, Decimal("0"))]
    previous_close = candles[0].close
    for candle in candles[1:]:
        ranges.append(
            max(
                candle.high - candle.low,
                abs(candle.high - previous_close),
                abs(candle.low - previous_close),
                Decimal("0"),
            )
        )
        previous_close = candle.close
    return tuple(ranges)


def _recent_range_pct(candles: Sequence[_Candle], latest_close: Decimal) -> MaybeDecimal:
    if not candles or latest_close == 0:
        return NA
    high = max(candle.high for candle in candles)
    low = min(candle.low for candle in candles)
    return _quantize((high - low) / abs(latest_close) * Decimal("100"))


def _baseline_range_pct(candles: Sequence[_Candle], latest_close: Decimal) -> MaybeDecimal:
    if len(candles) <= RECENT_RANGE_WINDOW or latest_close == 0:
        return NA
    baseline = candles[-(BASELINE_RANGE_WINDOW + RECENT_RANGE_WINDOW) : -RECENT_RANGE_WINDOW]
    if len(baseline) < RECENT_RANGE_WINDOW:
        baseline = candles[:-RECENT_RANGE_WINDOW]
    windows: list[Decimal] = []
    for index in range(0, len(baseline) - RECENT_RANGE_WINDOW + 1, max(1, RECENT_RANGE_WINDOW // 2)):
        window = baseline[index : index + RECENT_RANGE_WINDOW]
        value = _recent_range_pct(window, latest_close)
        if value != NA:
            windows.append(value)
    if not windows:
        return NA
    return _average(windows)


def _ema_series(values: Sequence[Decimal], period: int) -> tuple[Decimal, ...]:
    if not values:
        return ()
    multiplier = Decimal("2") / Decimal(period + 1)
    ema = values[0]
    output = [ema]
    for value in values[1:]:
        ema = (value - ema) * multiplier + ema
        output.append(ema)
    return tuple(output)


def _trend_direction(
    *,
    close: Decimal,
    ema_fast: Decimal,
    ema_slow: Decimal,
    ema_slope_pct: MaybeDecimal,
) -> Literal["bullish", "bearish", "neutral"]:
    slope = _decimal_or_zero(ema_slope_pct)
    if close > ema_slow and ema_fast > ema_slow and slope >= Decimal("0.12"):
        return "bullish"
    if close < ema_slow and ema_fast < ema_slow and slope <= Decimal("-0.12"):
        return "bearish"
    return "neutral"


def _drift_direction(
    *,
    close: Decimal,
    ema_slow: Decimal,
    ema_slope_pct: MaybeDecimal,
) -> Literal["bullish", "bearish", "neutral"]:
    slope = _decimal_or_zero(ema_slope_pct)
    if close >= ema_slow and slope >= Decimal("0.03"):
        return "bullish"
    if close <= ema_slow and slope <= Decimal("-0.03"):
        return "bearish"
    return "neutral"


def _ratio_or_na(numerator: MaybeDecimal, denominator: MaybeDecimal) -> MaybeDecimal:
    if numerator == NA or denominator == NA or denominator == 0:
        return NA
    return _quantize(numerator / denominator)


def _max_decimal(*values: Any) -> Decimal:
    decimals = [_decimal_or_zero(value) for value in values if value != NA and value is not None]
    return max(decimals) if decimals else Decimal("0")


def _average(values: Sequence[Decimal]) -> MaybeDecimal:
    numeric = [value for value in values if value != NA]
    if not numeric:
        return NA
    return _quantize(sum(numeric, Decimal("0")) / Decimal(len(numeric)))


def _field(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _decimal_or_zero(value: Any) -> Decimal:
    if _is_missing(value):
        return Decimal("0")
    try:
        return _decimal_from(value, "regime decimal")
    except ValueError:
        return Decimal("0")


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


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
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


__all__ = [
    "MarketRegimeInput",
    "MarketRegimeResult",
    "RegimeAdjustment",
    "RegimeRiskLevel",
    "RegimeState",
    "default_market_regime_result",
    "disabled_market_regime_result",
    "evaluate_market_regime",
]
