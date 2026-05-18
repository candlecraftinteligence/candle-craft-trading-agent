from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from app.data.dtos import NA, MaybeDecimal
from app.regime.models import (
    MarketRegimeInput,
    MarketRegimeResult,
    RegimeRiskLevel,
    RegimeState,
    RegimeStrictness,
    confidence_band,
)
from app.regime.scoring import (
    build_adjustment,
    default_result,
    disabled_result,
    risk_level_for_state,
    score_compatibility,
    score_confidence,
)

OUTPUT_QUANT = Decimal("0.00000001")
MIN_REQUIRED_CANDLES = 40
RECENT_RANGE_WINDOW = 20
BASELINE_RANGE_WINDOW = 60
ATR_WINDOW = 14
EMA_FAST_PERIOD = 21
EMA_SLOW_PERIOD = 50


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


def evaluate_market_regime(
    regime_input: MarketRegimeInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> MarketRegimeResult:
    data = _normalize_input(regime_input, overrides)
    btc = _market_metrics("BTCUSDT", data.btc_candles)
    eth = _market_metrics("ETHUSDT", data.eth_candles)
    missing_data = _unique_strings((*data.missing_data, *btc.missing_data, *eth.missing_data, *_proxy_missing_data(data)))
    required_data_missing = bool(btc.missing_data or eth.missing_data)
    evidence = _evidence(data, btc, eth)
    state = RegimeState.MIXED if required_data_missing else _classify_state(data, btc, eth, evidence)
    confidence_score, notes, boosts, penalties = score_confidence(
        state=state,
        evidence=evidence,
        strictness=data.strictness,
        required_data_missing=required_data_missing,
    )
    compatibility = score_compatibility(
        state=state,
        confidence_score=confidence_score,
        evidence=evidence,
        strictness=data.strictness,
    )
    adjustment = build_adjustment(
        state=state,
        confidence_score=confidence_score,
        compatibility=compatibility,
        strictness=data.strictness,
        notes=notes,
    )
    risk_level = risk_level_for_state(state, required_data_missing=required_data_missing)
    warnings = _warnings_for_state(state, risk_level, adjustment.explanation, required_data_missing)
    return MarketRegimeResult(
        state=state,
        risk_level=risk_level,
        confidence_score=confidence_score,
        confidence_band=confidence_band(confidence_score),
        strictness=data.strictness,
        compatibility_scores=compatibility,
        adjustment=adjustment,
        metrics=_metrics_payload(data, btc, eth, evidence),
        missing_data=missing_data,
        unverified_data=data.unverified_data,
        warnings=warnings,
        environment_notes=notes,
        boosts=boosts,
        penalties=penalties,
    )


def default_market_regime_result() -> MarketRegimeResult:
    return default_result()


def disabled_market_regime_result() -> MarketRegimeResult:
    return disabled_result()


def _normalize_input(
    regime_input: MarketRegimeInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> MarketRegimeInput:
    if regime_input is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(regime_input, MarketRegimeInput):
        if not overrides:
            return regime_input
        raw = regime_input.model_dump()
        raw.update(overrides)
    else:
        raw = dict(regime_input)
        raw.update(overrides)
    if "strictness" not in raw and "risk_mode" in raw:
        raw["strictness"] = raw["risk_mode"]
    return MarketRegimeInput.model_validate(raw)


def _market_metrics(symbol: str, raw_candles: Sequence[Any]) -> _MarketMetrics:
    candles = _normalized_candles(raw_candles)
    missing_data: list[str] = []
    if len(candles) < MIN_REQUIRED_CANDLES:
        missing_data.append(f"{symbol}_candles: N/A")
        return _MarketMetrics(symbol=symbol, candle_count=len(candles), missing_data=tuple(missing_data))

    closes = tuple(candle.close for candle in candles)
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


def _evidence(data: MarketRegimeInput, btc: _MarketMetrics, eth: _MarketMetrics) -> dict[str, Any]:
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
    breadth_side, breadth_strength = _breadth_state(data)
    htf_agreement = _first_decimal(data.htf_agreement_pct, _derived_htf_agreement(alignment, breadth_side))
    htf_conflict = _first_decimal(data.htf_conflict_pct, _derived_htf_conflict(alignment, breadth_side))
    confirmation = _decimal_or_zero(data.confirmation_pct)
    failed_confirmation = _decimal_or_zero(data.failed_confirmation_pct)
    valid_sweeps = _decimal_or_zero(data.valid_sweep_pct)
    setup_density = _decimal_or_zero(data.setup_density_pct)
    rejection_clustering = _decimal_or_zero(data.rejection_clustering_pct)
    if setup_density == 0 and data.scanned_symbols > 0:
        setup_density = confirmation
    if rejection_clustering == 0 and failed_confirmation > 0:
        rejection_clustering = failed_confirmation

    volatility_profile = _volatility_profile(volatility_ratio, atr_pct, range_pct)
    missing_optional = sum(
        1
        for value in (
            data.btc_d_context,
            data.usdt_d_context,
            data.average_rr,
            data.setup_density_pct,
            data.rejection_clustering_pct,
        )
        if _display(value) == NA
    )
    return {
        "volatility_ratio": volatility_ratio,
        "range_pct": range_pct,
        "atr_pct": atr_pct,
        "volatility_profile": volatility_profile,
        "directional_alignment": alignment,
        "drift_alignment": drift_alignment,
        "breadth_side": breadth_side,
        "breadth_strength_pct": breadth_strength,
        "valid_sweep_pct": valid_sweeps,
        "follow_through_pct": confirmation,
        "failed_confirmation_pct": failed_confirmation,
        "htf_agreement_pct": htf_agreement,
        "htf_conflict_pct": htf_conflict,
        "average_rr": _decimal_or_zero(data.average_rr),
        "setup_density_pct": setup_density,
        "rejection_clustering_pct": rejection_clustering,
        "btc_d_context": data.btc_d_context,
        "usdt_d_context": data.usdt_d_context,
        "missing_optional_count": missing_optional,
    }


def _classify_state(
    data: MarketRegimeInput,
    btc: _MarketMetrics,
    eth: _MarketMetrics,
    evidence: Mapping[str, Any],
) -> RegimeState:
    volatility_ratio = _decimal_or_zero(evidence.get("volatility_ratio"))
    range_pct = _decimal_or_zero(evidence.get("range_pct"))
    atr_pct = _decimal_or_zero(evidence.get("atr_pct"))
    alignment = str(evidence.get("directional_alignment", NA))
    drift_alignment = str(evidence.get("drift_alignment", NA))
    breadth_side = str(evidence.get("breadth_side", NA))
    breadth_strength = _decimal_or_zero(evidence.get("breadth_strength_pct"))
    confirmation = _decimal_or_zero(evidence.get("follow_through_pct"))
    failed_confirmation = _decimal_or_zero(evidence.get("failed_confirmation_pct"))
    valid_sweeps = _decimal_or_zero(evidence.get("valid_sweep_pct"))
    htf_agreement = _decimal_or_zero(evidence.get("htf_agreement_pct"))
    htf_conflict = _decimal_or_zero(evidence.get("htf_conflict_pct"))
    rr = _decimal_or_zero(evidence.get("average_rr"))
    rejection_cluster = _decimal_or_zero(evidence.get("rejection_clustering_pct"))

    scores = {state: Decimal("0") for state in RegimeState if state.name == state.value}
    if volatility_ratio >= Decimal("2.25") or (volatility_ratio >= Decimal("1.90") and range_pct >= Decimal("7")):
        scores[RegimeState.HIGH_VOLATILITY] += Decimal("80")
    if volatility_ratio <= Decimal("0.65") and atr_pct <= Decimal("1.25") and range_pct <= Decimal("4"):
        scores[RegimeState.RANGE_COMPRESSION] += Decimal("78")
    if alignment in ("bullish", "bearish"):
        scores[RegimeState.TREND_EXPANSION] += Decimal("35")
        scores[RegimeState.TREND_PULLBACK] += Decimal("24")
        if htf_agreement >= Decimal("60"):
            scores[RegimeState.TREND_EXPANSION] += Decimal("20")
        if Decimal("0.75") <= volatility_ratio <= Decimal("1.85"):
            scores[RegimeState.TREND_EXPANSION] += Decimal("18")
        if confirmation >= Decimal("50"):
            scores[RegimeState.TREND_EXPANSION] += Decimal("12")
        if rr >= Decimal("3"):
            scores[RegimeState.TREND_EXPANSION] += Decimal("8")
        if valid_sweeps >= Decimal("20") and confirmation < Decimal("55"):
            scores[RegimeState.TREND_PULLBACK] += Decimal("18")
        if volatility_ratio <= Decimal("1.10"):
            scores[RegimeState.TREND_PULLBACK] += Decimal("10")
    if alignment == "bullish" and breadth_side == "bullish" and breadth_strength >= Decimal("60"):
        scores[RegimeState.RISK_ON] += Decimal("58")
    if alignment == "bearish" and breadth_side == "bearish" and breadth_strength >= Decimal("60"):
        scores[RegimeState.RISK_OFF] += Decimal("58")
    if alignment == "mixed" or htf_conflict >= Decimal("35"):
        scores[RegimeState.CHOP] += Decimal("42")
        scores[RegimeState.MIXED] += Decimal("20")
    if valid_sweeps >= Decimal("20") and (failed_confirmation >= Decimal("45") or confirmation <= Decimal("35")):
        scores[RegimeState.CHOP] += Decimal("35")
    if rejection_cluster >= Decimal("45"):
        scores[RegimeState.CHOP] += Decimal("20")
    if breadth_side == "mixed":
        scores[RegimeState.CHOP] += Decimal("20")
        scores[RegimeState.MIXED] += Decimal("18")
    if volatility_ratio <= Decimal("0.90") and drift_alignment in ("bullish", "bearish"):
        scores[RegimeState.LOW_VOLATILITY] += Decimal("45")
    if alignment == "neutral" and drift_alignment in ("bullish", "bearish"):
        scores[RegimeState.TRANSITION] += Decimal("35")
    if htf_conflict > Decimal("20") and htf_agreement > Decimal("20"):
        scores[RegimeState.TRANSITION] += Decimal("25")
    scores[RegimeState.MIXED] += Decimal("10")

    ordered = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    top_state, top_score = ordered[0]
    runner_up_score = ordered[1][1] if len(ordered) > 1 else Decimal("0")
    if top_score < Decimal("35"):
        return RegimeState.MIXED
    if top_state == RegimeState.RISK_ON and scores[RegimeState.TREND_EXPANSION] >= Decimal("70"):
        return RegimeState.TREND_EXPANSION
    if top_state == RegimeState.RISK_OFF and volatility_ratio >= Decimal("1.70"):
        return RegimeState.RISK_OFF
    if top_score - runner_up_score <= Decimal("8") and top_state not in (
        RegimeState.HIGH_VOLATILITY,
        RegimeState.RANGE_COMPRESSION,
        RegimeState.CHOP,
    ):
        return RegimeState.TRANSITION
    return top_state


def _warnings_for_state(
    state: RegimeState,
    risk_level: RegimeRiskLevel,
    explanation: str,
    required_data_missing: bool,
) -> tuple[str, ...]:
    if required_data_missing:
        return ("Required BTC/ETH candle data is incomplete; market regime is cautious/neutral.",)
    if risk_level in (RegimeRiskLevel.HIGH, RegimeRiskLevel.EXTREME):
        return (f"{state.value} regime risk is {risk_level.value}: {explanation}",)
    if state in (RegimeState.RANGE_COMPRESSION, RegimeState.MIXED, RegimeState.LOW_VOLATILITY, RegimeState.TRANSITION):
        return (explanation,)
    return ()


def _metrics_payload(
    data: MarketRegimeInput,
    btc: _MarketMetrics,
    eth: _MarketMetrics,
    evidence: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "btc": btc.model_dump(mode="json"),
        "eth": eth.model_dump(mode="json"),
        "total_proxy": "N/A" if not data.total_proxy_candles else "available",
        "total2_proxy": "N/A" if not data.total2_proxy_candles else "available",
        "btc_d_context": data.btc_d_context,
        "usdt_d_context": data.usdt_d_context,
        "scan_breadth": {
            "scanned_symbols": data.scanned_symbols,
            "bullish_bias_pct": data.bullish_bias_pct,
            "bearish_bias_pct": data.bearish_bias_pct,
            "valid_sweep_pct": data.valid_sweep_pct,
            "confirmation_pct": data.confirmation_pct,
            "failed_confirmation_pct": data.failed_confirmation_pct,
            "setup_density_pct": data.setup_density_pct,
            "rejection_clustering_pct": data.rejection_clustering_pct,
        },
        "environment_evidence": _json_evidence(evidence),
    }


def _json_evidence(evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {key: _display(value) for key, value in evidence.items()}


def _proxy_missing_data(data: MarketRegimeInput) -> tuple[str, ...]:
    missing = []
    if not data.total_proxy_candles:
        missing.append("TOTAL_proxy: N/A")
    if not data.total2_proxy_candles:
        missing.append("TOTAL2_proxy: N/A")
    if data.btc_d_context == NA:
        missing.append("BTC.D_context: N/A")
    if data.usdt_d_context == NA:
        missing.append("USDT.D_context: N/A")
    return tuple(missing)


def _volatility_profile(volatility_ratio: Decimal, atr_pct: Decimal, range_pct: Decimal) -> str:
    if volatility_ratio >= Decimal("2.0") or (volatility_ratio >= Decimal("1.65") and range_pct >= Decimal("7")):
        return "unstable"
    if volatility_ratio <= Decimal("0.70") and atr_pct <= Decimal("1.35") and range_pct <= Decimal("4.25"):
        return "compressed"
    if Decimal("0.80") <= volatility_ratio <= Decimal("1.85"):
        return "stable_expansion"
    if volatility_ratio < Decimal("0.95"):
        return "low"
    return "neutral"


def _breadth_state(data: MarketRegimeInput) -> tuple[str, Decimal]:
    bullish = _decimal_or_zero(data.bullish_bias_pct)
    bearish = _decimal_or_zero(data.bearish_bias_pct)
    broad = _decimal_or_zero(data.broad_participation_pct)
    if broad > 0:
        return "broad", broad
    if bullish >= Decimal("55") and bullish - bearish >= Decimal("15"):
        return "bullish", bullish
    if bearish >= Decimal("55") and bearish - bullish >= Decimal("15"):
        return "bearish", bearish
    if bullish >= Decimal("30") and bearish >= Decimal("30") and abs(bullish - bearish) <= Decimal("20"):
        return "mixed", max(bullish, bearish)
    return NA, Decimal("0")


def _derived_htf_agreement(alignment: str, breadth_side: str) -> Decimal:
    if alignment in ("bullish", "bearish") and breadth_side in (alignment, NA, "broad"):
        return Decimal("60")
    if alignment in ("bullish", "bearish"):
        return Decimal("45")
    return Decimal("0")


def _derived_htf_conflict(alignment: str, breadth_side: str) -> Decimal:
    if alignment == "mixed":
        return Decimal("60")
    if alignment in ("bullish", "bearish") and breadth_side not in (alignment, NA, "broad"):
        return Decimal("45")
    return Decimal("0")


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


def _first_decimal(*values: Any) -> Decimal:
    for value in values:
        if value == NA or value is None:
            continue
        return _decimal_or_zero(value)
    return Decimal("0")


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
    if value in (None, "", NA):
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
    if value is None or value == "" or value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value).strip() or NA


__all__ = [
    "MarketRegimeInput",
    "MarketRegimeResult",
    "RegimeRiskLevel",
    "RegimeState",
    "RegimeStrictness",
    "default_market_regime_result",
    "disabled_market_regime_result",
    "evaluate_market_regime",
]
