from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, field_validator

from app.analytics.derivatives_enrichment import DerivativesEnrichmentResult
from app.analytics.pullback_zones import (
    FibAlignmentResult,
    PullbackZone,
    PullbackZoneInput,
    PullbackZoneResult,
    analyze_pullback_zone,
    calculate_fib_alignment,
)
from app.data.dtos import NA, MaybeDecimal, MaybeInt

DecimalLike = Decimal | int | str
Direction = Literal["bullish", "bearish", "N/A"]
TradeBias = Literal["long", "short", "N/A"]
ShiftKind = Literal["BOS", "CHoCH", "N/A"]
SetupStatus = Literal["Pending", "Filled", "TP1", "TP2", "TP3", "Closed", "Rejected"]
TrustGrade = Literal["A", "B", "No trade"]
RiskTier = Literal["conservative", "base", "aggressive", "no_trade"]
TrendLabel = Literal["bullish", "bearish", "neutral", "N/A"]

OUTPUT_QUANT = Decimal("0.00000001")
SWEEP_ATR_MULTIPLIER = Decimal("0.35")
DEFAULT_ATR_PERIOD = 14
VOLUME_CONFIRMATION_MULTIPLIER = Decimal("1.5")
BASE_MIN_RR = Decimal("2.5")
CHALLENGE_MIN_RR = Decimal("3.0")
TICK_SIZE = Decimal("0.00000001")
RISK_WARNING = (
    "This is not financial advice. Pullback ideas are conditional and must be invalidated at the stop."
)


class LiquidityGrabMode(str, Enum):
    challenge = "challenge"
    swing = "swing"
    scalp = "scalp"


class LiquiditySweepSignal(BaseModel):
    is_present: bool = False
    direction: Direction = NA
    candle_index: MaybeInt = NA
    swing_index: MaybeInt = NA
    swing_level: MaybeDecimal = NA
    wick_price: MaybeDecimal = NA
    magnitude: MaybeDecimal = NA
    magnitude_atr: MaybeDecimal = NA
    confluence: str = NA
    reason: str = "No confirmed liquidity sweep detected."

    model_config = ConfigDict(frozen=True)


class StructureShiftSignal(BaseModel):
    is_present: bool = False
    kind: ShiftKind = NA
    direction: Direction = NA
    candle_index: MaybeInt = NA
    swing_index: MaybeInt = NA
    level: MaybeDecimal = NA
    close: MaybeDecimal = NA
    reason: str = "No BOS/CHoCH detected."

    model_config = ConfigDict(frozen=True)


class OrderBlockZone(BaseModel):
    is_present: bool = False
    direction: Direction = NA
    candle_index: MaybeInt = NA
    low: MaybeDecimal = NA
    high: MaybeDecimal = NA
    midpoint: MaybeDecimal = NA
    body_low: MaybeDecimal = NA
    body_high: MaybeDecimal = NA
    wick_low: MaybeDecimal = NA
    wick_high: MaybeDecimal = NA
    freshness_status: str = NA
    reason: str = "Order block is N/A because no qualifying candle was found."

    model_config = ConfigDict(frozen=True)


class FairValueGapZone(BaseModel):
    is_present: bool = False
    direction: Direction = NA
    candle_index: MaybeInt = NA
    low: MaybeDecimal = NA
    high: MaybeDecimal = NA
    midpoint: MaybeDecimal = NA
    fill_low: MaybeDecimal = NA
    fill_high: MaybeDecimal = NA
    freshness_status: str = NA
    reason: str = "FVG is N/A because no valid imbalance was found."

    model_config = ConfigDict(frozen=True)


class MomentumConfirmation(BaseModel):
    is_confirmed: bool = False
    volume_status: Literal["confirmed", "not_confirmed", "N/A"] = NA
    volume_ratio: MaybeDecimal = NA
    average_volume: MaybeDecimal = NA
    sweep_volume: MaybeDecimal = NA
    delta_status: str = NA
    cvd: str = NA
    reason: str = "Volume/delta confirmation is N/A because required data is missing."

    model_config = ConfigDict(frozen=True)


class TrustMeterResult(BaseModel):
    score: int = 0
    percentage: int = 0
    grade: TrustGrade = "No trade"
    risk_tier: RiskTier = "no_trade"
    components: dict[str, int] = {}
    reason: str = "Trust Meter is below the trade threshold."

    model_config = ConfigDict(frozen=True)


class StrategyGateViolation(BaseModel):
    code: str
    message: str
    severity: Literal["hard_rejection"] = "hard_rejection"

    model_config = ConfigDict(frozen=True)


class StrategyGateResult(BaseModel):
    passed: bool = False
    violations: tuple[StrategyGateViolation, ...] = ()

    model_config = ConfigDict(frozen=True)


class RotationContext(BaseModel):
    sector_rotation: str = NA
    narrative: str = NA
    key_play: str = NA

    model_config = ConfigDict(frozen=True)


class LiquidityGrabSetup(BaseModel):
    mode: LiquidityGrabMode
    is_valid: bool = False
    status: SetupStatus = "Rejected"
    bias: TradeBias = NA
    timeframe: str = NA
    trend: TrendLabel = NA
    htf_timeframe: str = NA
    bias_timeframe: str = NA
    execution_timeframe: str = NA
    confirmation_timeframe: str = NA
    htf_2d_context_source: str = NA
    candles_2d_count: int = 0
    candles_12h_count: int = 0
    candles_15m_count: int = 0
    candles_5m_count: int = 0
    htf_2d_trend: TrendLabel = NA
    mtf_12h_trend: TrendLabel = NA
    ltf_confirmation_timeframe: str = NA
    ltf_confirmation_status: str = NA
    execution_sweep_status: str = NA
    confirmation_structure_shift_status: str = NA
    confirmation_bos_choch_reason: str = NA
    first_failed_gate: str = NA
    current_price: MaybeDecimal = NA
    poc: MaybeDecimal = NA
    volume_profile_source: str = NA
    poc_diagnostics: str = NA
    sweep: LiquiditySweepSignal = LiquiditySweepSignal()
    structure_shift: StructureShiftSignal = StructureShiftSignal()
    order_block: OrderBlockZone = OrderBlockZone()
    fair_value_gap: FairValueGapZone = FairValueGapZone()
    fib_alignment: FibAlignmentResult = FibAlignmentResult()
    pullback_zone: PullbackZoneResult = PullbackZoneResult()
    pullback_zone_status: str = NA
    pullback_calculation_timeframe: str = NA
    pullback_sweep_candle_index: MaybeInt = NA
    pullback_bos_choch_candle_index: MaybeInt = NA
    displacement_start_index: MaybeInt = NA
    displacement_end_index: MaybeInt = NA
    selected_zone_type: str = NA
    ob_zone: PullbackZone = PullbackZone(zone_type="OB")
    fvg_zone: PullbackZone = PullbackZone(zone_type="FVG")
    fib_382: MaybeDecimal = NA
    fib_618: MaybeDecimal = NA
    fib_65: MaybeDecimal = NA
    fib_786: MaybeDecimal = NA
    pullback_depth_ratio: MaybeDecimal = NA
    pullback_failure_reason: str = NA
    atr_stop_buffer: MaybeDecimal = NA
    momentum: MomentumConfirmation = MomentumConfirmation()
    trust_meter: TrustMeterResult = TrustMeterResult()
    gate_result: StrategyGateResult = StrategyGateResult()
    entry_low: MaybeDecimal = NA
    entry_high: MaybeDecimal = NA
    entry: MaybeDecimal = NA
    entry_source: str = NA
    stop: MaybeDecimal = NA
    tp1: MaybeDecimal = NA
    tp2: MaybeDecimal = NA
    tp3: MaybeDecimal = NA
    rr_to_tp2: MaybeDecimal = NA
    invalidation: str = NA
    risk_warning: str = RISK_WARNING
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()
    rotation: RotationContext = RotationContext()
    strategy_diagnostics: str = NA
    gates_passed: tuple[str, ...] = ()
    gates_failed: tuple[str, ...] = ()
    hard_rejection_reasons: tuple[str, ...] = ()
    sweep_diagnostics: str = NA
    structure_shift_diagnostics: str = NA
    ob_fvg_diagnostics: str = NA
    pullback_zone_diagnostics: str = NA
    fib_diagnostics: str = NA
    momentum_diagnostics: str = NA
    rr_diagnostics: str = NA
    trust_meter_diagnostics: str = NA
    derivatives_supports_trade: bool | Literal["N/A"] = NA
    derivatives_conflict_reason: str = NA
    funding_context: Any = NA
    oi_context: Any = NA
    crowding_risk: str = NA
    squeeze_risk: str = NA

    model_config = ConfigDict(frozen=True)

    def diagnostics_summary(self, symbol: str) -> str:
        return _format_setup_diagnostics(symbol, self)


class StrategyFormattedOutput(BaseModel):
    challenge_setup: str
    swing_setup: str
    scalp_setup: str
    full_text: str

    model_config = ConfigDict(frozen=True)


class LiquidityGrabResult(BaseModel):
    symbol: str
    requested_mode: LiquidityGrabMode
    challenge: LiquidityGrabSetup
    swing: LiquidityGrabSetup
    scalp: LiquidityGrabSetup
    formatted_output: StrategyFormattedOutput
    missing_data: tuple[str, ...] = ()
    unverified_data: tuple[str, ...] = ()
    strategy_diagnostics: str = NA
    gates_passed: tuple[str, ...] = ()
    gates_failed: tuple[str, ...] = ()
    hard_rejection_reasons: tuple[str, ...] = ()
    sweep_diagnostics: str = NA
    structure_shift_diagnostics: str = NA
    ob_fvg_diagnostics: str = NA
    pullback_zone_diagnostics: str = NA
    fib_diagnostics: str = NA
    momentum_diagnostics: str = NA
    rr_diagnostics: str = NA
    trust_meter_diagnostics: str = NA
    challenge_diagnostics: str = NA
    swing_diagnostics: str = NA
    scalp_diagnostics: str = NA
    safety_note: str = "Dry-run strategy analysis only. No exchange order or private API access is used."

    model_config = ConfigDict(frozen=True)

    def formatted_diagnostics(self, mode: LiquidityGrabMode | str | None = None) -> str:
        selected = self.requested_mode if mode is None else LiquidityGrabMode(mode)
        if selected == LiquidityGrabMode.challenge:
            return self.challenge_diagnostics
        if selected == LiquidityGrabMode.scalp:
            return self.scalp_diagnostics
        return self.swing_diagnostics


class LiquidityGrabInput(BaseModel):
    symbol: str
    mode: LiquidityGrabMode = LiquidityGrabMode.swing
    htf_timeframe: str = "2d"
    bias_timeframe: str = "12h"
    execution_timeframe: str = "15m"
    confirmation_timeframe: str = "5m"
    candles_2d: Sequence[Any] | None = None
    candles_12h: Sequence[Any] | None = None
    candles_6h: Sequence[Any] | None = None
    candles_4h: Sequence[Any] | None = None
    candles_1h: Sequence[Any] | None = None
    candles_15m: Sequence[Any] | None = None
    candles_5m: Sequence[Any] | None = None
    current_price: Decimal | None = None
    user_support_levels: Sequence[Any] | Any | None = None
    user_resistance_levels: Sequence[Any] | Any | None = None
    poc: Any | None = None
    value_area_high: Any | None = None
    value_area_low: Any | None = None
    volume_profile_source: str = NA
    volume_profile_warnings: Sequence[Any] | Any | None = None
    liquidity_below: Sequence[Any] | Any | None = None
    liquidity_above: Sequence[Any] | Any | None = None
    orderflow_summary: Any | None = None
    funding: Any | None = None
    open_interest: Any | None = None
    derivatives_enrichment: DerivativesEnrichmentResult | None = None
    cvd: Any | None = None
    liquidation_data: Any | None = None
    btc_context: Any | None = None
    btc_d_context: Any | None = None
    event_risk_context: Any | None = None
    weekend_filter: Any | None = None
    sector_rotation: Any | None = None
    narrative: Any | None = None
    aggressive_toggle: bool = False
    token_classification: Any | None = None
    htf_2d_context_source: str = NA

    model_config = ConfigDict(frozen=True)

    @field_validator("current_price")
    @classmethod
    def _decimal_is_finite(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("current_price must be finite")
        return value

    @field_validator("htf_timeframe", "bias_timeframe", "execution_timeframe", "confirmation_timeframe")
    @classmethod
    def _timeframe_not_blank(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("timeframe must not be blank")
        return normalized


@dataclass(frozen=True)
class _Candle:
    index: int
    timestamp: MaybeInt
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: MaybeDecimal


@dataclass(frozen=True)
class _SwingPoint:
    kind: Literal["high", "low"]
    index: int
    confirmed_at_index: int
    price: Decimal


@dataclass(frozen=True)
class _SelectedCandles:
    timeframe: str
    candles: tuple[_Candle, ...]


class LiquidityGrabEngine:
    """Deterministic liquidity-grab pullback analysis.

    The engine only analyzes already-supplied candles and context. It does not
    call exchanges, use private API data, send alerts, or place orders. CHoCH is
    intentionally simple: after a sweep, a close beyond the opposite LTF swing is
    labeled CHoCH when the nearest prior trend context was opposite the break;
    otherwise it is labeled BOS.
    """

    def __init__(self, *, atr_period: int = DEFAULT_ATR_PERIOD, swing_lookback: int = 2) -> None:
        if atr_period < 1:
            raise ValueError("atr_period must be at least 1")
        if swing_lookback < 1:
            raise ValueError("swing_lookback must be at least 1")
        self.atr_period = atr_period
        self.swing_lookback = swing_lookback

    def analyze(
        self,
        strategy_input: LiquidityGrabInput | Mapping[str, Any] | None = None,
        **overrides: Any,
    ) -> LiquidityGrabResult:
        data = _normalize_input(strategy_input, overrides)
        normalized = _normalize_all_timeframes(data)
        missing_data = _missing_context(data, normalized)
        unverified_data = _unverified_context(data)
        rotation = RotationContext(
            sector_rotation=_context_text(data.sector_rotation),
            narrative=_context_text(data.narrative),
            key_play=_key_play(data),
        )

        challenge = self._analyze_mode(
            data,
            normalized,
            LiquidityGrabMode.challenge,
            missing_data,
            unverified_data,
            rotation,
        )
        swing = self._analyze_mode(
            data,
            normalized,
            LiquidityGrabMode.swing,
            missing_data,
            unverified_data,
            rotation,
        )
        scalp = self._analyze_mode(
            data,
            normalized,
            LiquidityGrabMode.scalp,
            missing_data,
            unverified_data,
            rotation,
        )
        challenge = _with_setup_diagnostics(data.symbol, challenge)
        swing = _with_setup_diagnostics(data.symbol, swing)
        scalp = _with_setup_diagnostics(data.symbol, scalp)
        requested_setup = _setup_for_mode(data.mode, challenge, swing, scalp)
        formatted = _format_result(data.symbol, data, challenge, swing, scalp)
        return LiquidityGrabResult(
            symbol=data.symbol,
            requested_mode=data.mode,
            challenge=challenge,
            swing=swing,
            scalp=scalp,
            formatted_output=formatted,
            missing_data=missing_data,
            unverified_data=unverified_data,
            strategy_diagnostics=requested_setup.strategy_diagnostics,
            gates_passed=requested_setup.gates_passed,
            gates_failed=requested_setup.gates_failed,
            hard_rejection_reasons=requested_setup.hard_rejection_reasons,
            sweep_diagnostics=requested_setup.sweep_diagnostics,
            structure_shift_diagnostics=requested_setup.structure_shift_diagnostics,
            ob_fvg_diagnostics=requested_setup.ob_fvg_diagnostics,
            pullback_zone_diagnostics=requested_setup.pullback_zone_diagnostics,
            fib_diagnostics=requested_setup.fib_diagnostics,
            momentum_diagnostics=requested_setup.momentum_diagnostics,
            rr_diagnostics=requested_setup.rr_diagnostics,
            trust_meter_diagnostics=requested_setup.trust_meter_diagnostics,
            challenge_diagnostics=challenge.strategy_diagnostics,
            swing_diagnostics=swing.strategy_diagnostics,
            scalp_diagnostics=scalp.strategy_diagnostics,
        )

    def _analyze_mode(
        self,
        data: LiquidityGrabInput,
        normalized: Mapping[str, tuple[_Candle, ...]],
        mode: LiquidityGrabMode,
        missing_data: tuple[str, ...],
        unverified_data: tuple[str, ...],
        rotation: RotationContext,
    ) -> LiquidityGrabSetup:
        execution = _select_execution_candles(normalized, data)
        confirmation = _select_confirmation_candles(normalized, data)
        context_fields = _timeframe_context_fields(data, normalized, execution, confirmation)
        if execution is None:
            return _rejected_setup(
                mode,
                "no_execution_candles",
                "15m execution candles missing.",
                missing_data,
                unverified_data,
                rotation,
                context_fields=context_fields,
            )

        candles = execution.candles
        if confirmation is None:
            return _rejected_setup(
                mode,
                "missing_confirmation_candles",
                "5m confirmation candles missing.",
                missing_data,
                unverified_data,
                rotation,
                timeframe=execution.timeframe,
                current_price=_current_price(data, candles),
                context_fields=context_fields,
            )

        confirmation_candles = confirmation.candles
        if len(candles) < self.atr_period + self.swing_lookback + 3:
            return _rejected_setup(
                mode,
                "not_enough_candles",
                f"Not enough candles for {mode.value} mode.",
                missing_data,
                unverified_data,
                rotation,
                timeframe=execution.timeframe,
                current_price=_current_price(data, candles),
                context_fields=context_fields,
            )

        atr = _calculate_atr_from_normalized(candles, self.atr_period)
        if atr == NA or atr <= 0:
            return _rejected_setup(
                mode,
                "atr_unavailable",
                "ATR is N/A; sweep magnitude cannot be validated.",
                missing_data,
                unverified_data,
                rotation,
                timeframe=execution.timeframe,
                current_price=_current_price(data, candles),
                context_fields=context_fields,
            )

        sweep = detect_liquidity_sweep(
            candles,
            atr_period=self.atr_period,
            lookback=self.swing_lookback,
            liquidity_below=data.liquidity_below,
            liquidity_above=data.liquidity_above,
        )
        if not sweep.is_present:
            return _rejected_setup(
                mode,
                "missing_confirmed_sweep",
                "Confirmed liquidity sweep is required.",
                missing_data,
                unverified_data,
                rotation,
                timeframe=execution.timeframe,
                current_price=_current_price(data, candles),
                sweep=sweep,
                context_fields=context_fields,
            )

        confirmation_sweep = _sweep_for_confirmation_candles(sweep, candles, confirmation_candles)
        structure_shift = detect_structure_shift(
            confirmation_candles,
            sweep=confirmation_sweep,
            lookback=self.swing_lookback,
        )
        if not structure_shift.is_present:
            structure_shift = _confirmation_failure_shift(structure_shift)
            context_fields = _timeframe_context_fields(
                data,
                normalized,
                execution,
                confirmation,
                structure_shift=structure_shift,
            )
            return _rejected_setup(
                mode,
                "missing_confirmation_structure_shift",
                structure_shift.reason,
                missing_data,
                unverified_data,
                rotation,
                timeframe=execution.timeframe,
                current_price=_current_price(data, candles),
                sweep=sweep,
                structure_shift=structure_shift,
                context_fields=context_fields,
            )
        context_fields = _timeframe_context_fields(
            data,
            normalized,
            execution,
            confirmation,
            structure_shift=structure_shift,
        )

        direction: Direction = structure_shift.direction
        bias: TradeBias = "long" if direction == "bullish" else "short"
        trade_direction: TradeBias = "long" if direction == "bullish" else "short"
        pullback_zone = analyze_pullback_zone(
            PullbackZoneInput(
                symbol=data.symbol,
                direction=trade_direction,
                execution_timeframe=execution.timeframe,
                confirmation_timeframe=confirmation.timeframe,
                calculation_timeframe=confirmation.timeframe,
                candles_15m=candles,
                candles_5m=confirmation_candles,
                sweep_candle_index=confirmation_sweep.candle_index,
                bos_choch_candle_index=structure_shift.candle_index,
                latest_price=_current_price(data, confirmation_candles),
                atr_15m=atr,
                tick_size=TICK_SIZE,
                aggressive_toggle=data.aggressive_toggle,
                minimum_rr=CHALLENGE_MIN_RR if mode == LiquidityGrabMode.challenge else BASE_MIN_RR,
                poc=data.poc if not _is_missing(data.poc) else NA,
                value_area_high=data.value_area_high if not _is_missing(data.value_area_high) else NA,
                value_area_low=data.value_area_low if not _is_missing(data.value_area_low) else NA,
                liquidity_below=data.liquidity_below,
                liquidity_above=data.liquidity_above,
                user_support_levels=data.user_support_levels,
                user_resistance_levels=data.user_resistance_levels,
            )
        )
        order_block = _order_block_from_pullback_zone(pullback_zone.ob_zone, direction)
        fair_value_gap = _fair_value_gap_from_pullback_zone(pullback_zone.fvg_zone, direction)
        fib_alignment = pullback_zone.fib_alignment
        entry_source = pullback_zone.selected_zone_type
        if not pullback_zone.valid:
            return _rejected_setup(
                mode,
                pullback_zone.first_failed_gate if pullback_zone.first_failed_gate != NA else "no_ob_or_fvg_zone",
                pullback_zone.pullback_failure_reason
                if pullback_zone.pullback_failure_reason != NA
                else "Required OB/FVG pullback zone with fib alignment is missing.",
                missing_data,
                unverified_data,
                rotation,
                timeframe=execution.timeframe,
                current_price=_current_price(data, candles),
                sweep=sweep,
                structure_shift=structure_shift,
                order_block=order_block,
                fair_value_gap=fair_value_gap,
                fib_alignment=fib_alignment,
                pullback_zone=pullback_zone,
                entry_low=pullback_zone.entry_low,
                entry_high=pullback_zone.entry_high,
                entry=pullback_zone.entry,
                entry_source=entry_source,
                stop=pullback_zone.stop,
                tp1=pullback_zone.tp1,
                tp2=pullback_zone.tp2,
                tp3=pullback_zone.tp3,
                rr_to_tp2=pullback_zone.rr_to_tp2,
                context_fields=context_fields,
            )

        entry_low = _decimal_from(pullback_zone.entry_low, "pullback_zone.entry_low")
        entry_high = _decimal_from(pullback_zone.entry_high, "pullback_zone.entry_high")
        entry = _decimal_from(pullback_zone.entry, "pullback_zone.entry")
        stop = _decimal_from(pullback_zone.stop, "pullback_zone.stop")
        tp1 = _decimal_from(pullback_zone.tp1, "pullback_zone.tp1")
        tp2 = _decimal_from(pullback_zone.tp2, "pullback_zone.tp2")
        tp3: MaybeDecimal = pullback_zone.tp3
        rr_to_tp2 = pullback_zone.rr_to_tp2
        momentum = confirm_momentum(candles, int(sweep.candle_index), cvd=data.cvd)
        trend = _trend_for_mode(normalized, mode)
        trust_meter = _trust_meter(
            sweep=sweep,
            structure_shift=structure_shift,
            order_block=order_block,
            fair_value_gap=fair_value_gap,
            fib_alignment=fib_alignment,
            momentum=momentum,
            trend=trend,
            direction=direction,
            data=data,
            rr_to_tp2=rr_to_tp2,
        )
        derivatives_fields = _derivatives_strategy_fields(data, direction)
        gate_result = _gate_result(
            data=data,
            mode=mode,
            direction=direction,
            rr_to_tp2=rr_to_tp2,
            trust_meter=trust_meter,
            entry_source=entry_source,
            selected=confirmation,
            candles=confirmation_candles,
            structure_shift=structure_shift,
            entry_low=entry_low,
            entry_high=entry_high,
            fib_alignment=fib_alignment,
        )
        status: SetupStatus = "Rejected"
        if gate_result.passed:
            status = _entry_status(
                confirmation.timeframe,
                mode,
                confirmation_candles,
                int(structure_shift.candle_index),
                direction,
                entry_low,
                entry_high,
            )

        return LiquidityGrabSetup(
            mode=mode,
            is_valid=gate_result.passed,
            status=status if gate_result.passed else "Rejected",
            bias=bias,
            timeframe=execution.timeframe,
            trend=trend,
            current_price=_current_price(data, candles),
            sweep=sweep,
            structure_shift=structure_shift,
            order_block=order_block,
            fair_value_gap=fair_value_gap,
            fib_alignment=fib_alignment,
            pullback_zone=pullback_zone,
            pullback_zone_status=pullback_zone.pullback_zone_status,
            pullback_calculation_timeframe=pullback_zone.calculation_timeframe,
            pullback_sweep_candle_index=pullback_zone.sweep_candle_index,
            pullback_bos_choch_candle_index=pullback_zone.bos_choch_candle_index,
            displacement_start_index=pullback_zone.displacement_start_index,
            displacement_end_index=pullback_zone.displacement_end_index,
            selected_zone_type=pullback_zone.selected_zone_type,
            ob_zone=pullback_zone.ob_zone,
            fvg_zone=pullback_zone.fvg_zone,
            fib_382=pullback_zone.fib_382,
            fib_618=pullback_zone.fib_618,
            fib_65=pullback_zone.fib_65,
            fib_786=pullback_zone.fib_786,
            pullback_depth_ratio=pullback_zone.pullback_depth_ratio,
            pullback_failure_reason=pullback_zone.pullback_failure_reason,
            atr_stop_buffer=pullback_zone.atr_stop_buffer,
            momentum=momentum,
            trust_meter=trust_meter,
            gate_result=gate_result,
            entry_low=_quantize(entry_low),
            entry_high=_quantize(entry_high),
            entry=_quantize(entry),
            entry_source=entry_source,
            stop=_quantize(stop),
            tp1=_quantize(tp1),
            tp2=_quantize(tp2),
            tp3=NA if tp3 == NA else _quantize(tp3),
            rr_to_tp2=rr_to_tp2,
            invalidation=_invalidation(direction, stop),
            missing_data=missing_data,
            unverified_data=unverified_data,
            rotation=rotation,
            **derivatives_fields,
            **context_fields,
        )


def analyze_liquidity_grab_pullback(
    strategy_input: LiquidityGrabInput | Mapping[str, Any] | None = None,
    **overrides: Any,
) -> LiquidityGrabResult:
    return LiquidityGrabEngine().analyze(strategy_input, **overrides)


def calculate_atr(candles: Sequence[Any], period: int = DEFAULT_ATR_PERIOD) -> MaybeDecimal:
    normalized, errors = _normalize_candles(candles, "candles")
    if errors:
        raise ValueError(errors[0])
    return _calculate_atr_from_normalized(normalized, period)


def detect_liquidity_sweep(
    candles: Sequence[Any],
    *,
    direction: Direction | None = None,
    atr_period: int = DEFAULT_ATR_PERIOD,
    lookback: int = 2,
    liquidity_below: Any | None = None,
    liquidity_above: Any | None = None,
) -> LiquiditySweepSignal:
    normalized, errors = _normalize_candles(candles, "candles")
    if errors:
        raise ValueError(errors[0])
    if len(normalized) < atr_period + lookback + 2:
        return LiquiditySweepSignal(reason="No sweep: not enough candles for ATR and confirmed swing validation.")

    swings = _detect_swings(normalized, lookback)
    swing_highs = tuple(point for point in swings if point.kind == "high")
    swing_lows = tuple(point for point in swings if point.kind == "low")
    latest_signal = LiquiditySweepSignal()

    for candle in normalized:
        if candle.index < atr_period + lookback:
            continue
        atr = _calculate_atr_from_normalized(normalized[: candle.index], atr_period)
        if atr == NA or atr <= 0:
            continue
        threshold = atr * SWEEP_ATR_MULTIPLIER
        previous_low = _last_confirmed_before(swing_lows, candle.index)
        previous_high = _last_confirmed_before(swing_highs, candle.index)

        if direction in (None, "bullish") and previous_low is not None:
            magnitude = previous_low.price - candle.low
            if magnitude >= threshold and candle.close > previous_low.price:
                latest_signal = LiquiditySweepSignal(
                    is_present=True,
                    direction="bullish",
                    candle_index=candle.index,
                    swing_index=previous_low.index,
                    swing_level=_quantize(previous_low.price),
                    wick_price=_quantize(candle.low),
                    magnitude=_quantize(magnitude),
                    magnitude_atr=_quantize(magnitude / atr),
                    confluence=_sweep_confluence("bullish", previous_low.price, swing_lows, atr, liquidity_below),
                    reason="Wick swept below a confirmed swing low by at least 0.35 ATR and closed back above it.",
                )
        if direction in (None, "bearish") and previous_high is not None:
            magnitude = candle.high - previous_high.price
            if magnitude >= threshold and candle.close < previous_high.price:
                latest_signal = LiquiditySweepSignal(
                    is_present=True,
                    direction="bearish",
                    candle_index=candle.index,
                    swing_index=previous_high.index,
                    swing_level=_quantize(previous_high.price),
                    wick_price=_quantize(candle.high),
                    magnitude=_quantize(magnitude),
                    magnitude_atr=_quantize(magnitude / atr),
                    confluence=_sweep_confluence("bearish", previous_high.price, swing_highs, atr, liquidity_above),
                    reason="Wick swept above a confirmed swing high by at least 0.35 ATR and closed back below it.",
                )

    return latest_signal


def detect_structure_shift(
    candles: Sequence[Any],
    *,
    sweep: LiquiditySweepSignal | None = None,
    direction: Direction | None = None,
    lookback: int = 2,
) -> StructureShiftSignal:
    normalized, errors = _normalize_candles(candles, "candles")
    if errors:
        raise ValueError(errors[0])
    if len(normalized) < lookback * 2 + 2:
        return StructureShiftSignal(reason="No BOS/CHoCH: not enough candles for confirmed swing validation.")

    if sweep is not None and sweep.is_present:
        break_direction: Direction = sweep.direction
        start_index = int(sweep.candle_index) + 1
    else:
        break_direction = direction or NA
        start_index = lookback * 2 + 1
    if break_direction not in ("bullish", "bearish"):
        return StructureShiftSignal(reason="No BOS/CHoCH: direction is N/A.")

    swings = _detect_swings(normalized, lookback)
    swing_highs = tuple(point for point in swings if point.kind == "high")
    swing_lows = tuple(point for point in swings if point.kind == "low")
    prior_trend = _trend_before_index(normalized, max(0, start_index - 1))

    for candle in normalized[start_index:]:
        if break_direction == "bullish":
            previous_high = _last_confirmed_before(swing_highs, candle.index)
            if previous_high is not None and candle.close > previous_high.price:
                kind: ShiftKind = "CHoCH" if prior_trend == "bearish" else "BOS"
                return StructureShiftSignal(
                    is_present=True,
                    kind=kind,
                    direction="bullish",
                    candle_index=candle.index,
                    swing_index=previous_high.index,
                    level=_quantize(previous_high.price),
                    close=_quantize(candle.close),
                    reason="Bullish BOS/CHoCH confirmed by candle close above a previous LTF swing high.",
                )
        if break_direction == "bearish":
            previous_low = _last_confirmed_before(swing_lows, candle.index)
            if previous_low is not None and candle.close < previous_low.price:
                kind = "CHoCH" if prior_trend == "bullish" else "BOS"
                return StructureShiftSignal(
                    is_present=True,
                    kind=kind,
                    direction="bearish",
                    candle_index=candle.index,
                    swing_index=previous_low.index,
                    level=_quantize(previous_low.price),
                    close=_quantize(candle.close),
                    reason="Bearish BOS/CHoCH confirmed by candle close below a previous LTF swing low.",
                )

    return StructureShiftSignal(reason="No BOS/CHoCH close beyond the required LTF swing.")


def detect_fair_value_gap(
    candles: Sequence[Any],
    direction: Direction,
    *,
    start_index: int = 2,
    end_index: int | None = None,
) -> FairValueGapZone:
    normalized, errors = _normalize_candles(candles, "candles")
    if errors:
        raise ValueError(errors[0])
    if direction not in ("bullish", "bearish"):
        return FairValueGapZone(reason="FVG is N/A because direction is N/A.")
    if len(normalized) < 3:
        return FairValueGapZone()

    final_index = len(normalized) - 1 if end_index is None else min(end_index, len(normalized) - 1)
    first_index = max(2, start_index)
    latest = FairValueGapZone()
    for index in range(first_index, final_index + 1):
        left = normalized[index - 2]
        current = normalized[index]
        if direction == "bullish" and left.high < current.low:
            low = left.high
            high = current.low
            midpoint = (low + high) / Decimal("2")
            latest = FairValueGapZone(
                is_present=True,
                direction="bullish",
                candle_index=index,
                low=_quantize(low),
                high=_quantize(high),
                midpoint=_quantize(midpoint),
                fill_low=_quantize(low),
                fill_high=_quantize(midpoint),
                reason="Bullish FVG found where candle i-2 high is below candle i low.",
            )
        if direction == "bearish" and left.low > current.high:
            low = current.low
            high = left.low
            midpoint = (low + high) / Decimal("2")
            latest = FairValueGapZone(
                is_present=True,
                direction="bearish",
                candle_index=index,
                low=_quantize(low),
                high=_quantize(high),
                midpoint=_quantize(midpoint),
                fill_low=_quantize(midpoint),
                fill_high=_quantize(high),
                reason="Bearish FVG found where candle i-2 low is above candle i high.",
            )
    return latest


def detect_order_block(
    candles: Sequence[Any],
    direction: Direction,
    *,
    bos_index: int,
    sweep_index: int = 0,
) -> OrderBlockZone:
    normalized, errors = _normalize_candles(candles, "candles")
    if errors:
        raise ValueError(errors[0])
    if direction not in ("bullish", "bearish"):
        return OrderBlockZone(reason="Order block is N/A because direction is N/A.")
    if bos_index <= 0 or bos_index > len(normalized):
        return OrderBlockZone(reason="Order block is N/A because BOS index is outside the candle range.")

    start = min(bos_index - 1, len(normalized) - 1)
    floor = max(0, sweep_index)
    for index in range(start, floor - 1, -1):
        candle = normalized[index]
        if direction == "bullish" and candle.close < candle.open:
            return _order_block_from_candle(candle, direction)
        if direction == "bearish" and candle.close > candle.open:
            return _order_block_from_candle(candle, direction)
    return OrderBlockZone()


def confirm_momentum(
    candles: Sequence[Any],
    sweep_index: int,
    *,
    cvd: Any | None = None,
    window: int = 20,
) -> MomentumConfirmation:
    normalized, errors = _normalize_candles(candles, "candles")
    if errors:
        raise ValueError(errors[0])
    if sweep_index < 0 or sweep_index >= len(normalized):
        return MomentumConfirmation(reason="Volume/delta confirmation is N/A because sweep index is invalid.")
    if sweep_index < window:
        return MomentumConfirmation(cvd=_context_text(cvd), reason="Volume confirmation is N/A because fewer than 20 prior bars exist.")

    sample = normalized[sweep_index - window : sweep_index]
    sweep = normalized[sweep_index]
    if sweep.volume == NA or any(candle.volume == NA for candle in sample):
        return MomentumConfirmation(
            cvd=_context_text(cvd),
            delta_status="provided" if not _is_missing(cvd) else NA,
            reason="Volume confirmation is N/A because volume data is missing.",
        )

    volumes = tuple(candle.volume for candle in sample if candle.volume != NA)
    average = sum(volumes) / Decimal(window)
    if average <= 0:
        return MomentumConfirmation(cvd=_context_text(cvd), reason="Volume confirmation is N/A because average volume is zero.")
    assert sweep.volume != NA
    ratio = sweep.volume / average
    cvd_text = _context_text(cvd)
    if ratio >= VOLUME_CONFIRMATION_MULTIPLIER:
        return MomentumConfirmation(
            is_confirmed=True,
            volume_status="confirmed",
            volume_ratio=_quantize(ratio),
            average_volume=_quantize(average),
            sweep_volume=_quantize(sweep.volume),
            delta_status="provided" if cvd_text != NA else NA,
            cvd=cvd_text,
            reason="Sweep candle volume is at least 1.5x the 20-bar average.",
        )
    return MomentumConfirmation(
        volume_status="not_confirmed",
        volume_ratio=_quantize(ratio),
        average_volume=_quantize(average),
        sweep_volume=_quantize(sweep.volume),
        delta_status="provided" if cvd_text != NA else NA,
        cvd=cvd_text,
        reason="Sweep candle volume is below 1.5x the 20-bar average.",
    )


def _normalize_input(
    strategy_input: LiquidityGrabInput | Mapping[str, Any] | None,
    overrides: Mapping[str, Any],
) -> LiquidityGrabInput:
    if strategy_input is None:
        raw: dict[str, Any] = dict(overrides)
    elif isinstance(strategy_input, LiquidityGrabInput):
        raw = strategy_input.model_dump()
        raw.update(overrides)
    else:
        raw = dict(strategy_input)
        raw.update(overrides)
    return LiquidityGrabInput.model_validate(raw)


def _normalize_all_timeframes(data: LiquidityGrabInput) -> dict[str, tuple[_Candle, ...]]:
    output: dict[str, tuple[_Candle, ...]] = {}
    for timeframe, raw in (
        ("2d", data.candles_2d),
        ("12h", data.candles_12h),
        ("6h", data.candles_6h),
        ("4h", data.candles_4h),
        ("1h", data.candles_1h),
        ("15m", data.candles_15m),
        ("5m", data.candles_5m),
    ):
        if raw is None:
            continue
        candles, errors = _normalize_candles(raw, f"candles_{timeframe}")
        if errors:
            raise ValueError(errors[0])
        output[timeframe] = candles
    return output


def _normalize_candles(candles: Sequence[Any], label: str) -> tuple[tuple[_Candle, ...], tuple[str, ...]]:
    if isinstance(candles, (str, bytes)) or not isinstance(candles, Sequence):
        return (), (f"Malformed {label}: expected a sequence of candle objects.",)

    normalized: list[_Candle] = []
    errors: list[str] = []
    for index, candle in enumerate(candles):
        required: dict[str, Decimal] = {}
        for field in ("open", "high", "low", "close"):
            value = _get_field(candle, field)
            if _is_missing(value):
                errors.append(f"Missing required OHLC field {label}[{index}].{field}.")
                continue
            try:
                required[field] = _decimal_from(value, f"{label}[{index}].{field}")
            except ValueError as exc:
                errors.append(str(exc))

        if len(required) != 4:
            continue
        if required["high"] < required["low"]:
            errors.append(f"Malformed candle {label}[{index}]: high is lower than low.")
            continue
        if required["high"] < max(required["open"], required["close"]):
            errors.append(f"Malformed candle {label}[{index}]: high is below open or close.")
            continue
        if required["low"] > min(required["open"], required["close"]):
            errors.append(f"Malformed candle {label}[{index}]: low is above open or close.")
            continue

        volume_value = _get_field(candle, "volume")
        try:
            volume = NA if _is_missing(volume_value) else _decimal_from(volume_value, f"{label}[{index}].volume")
        except ValueError as exc:
            errors.append(str(exc))
            continue
        if volume != NA and volume < 0:
            errors.append(f"Malformed candle {label}[{index}].volume: volume cannot be negative.")
            continue

        normalized.append(
            _Candle(
                index=index,
                timestamp=_normalize_timestamp(_get_field(candle, "timestamp")),
                open=required["open"],
                high=required["high"],
                low=required["low"],
                close=required["close"],
                volume=volume,
            )
        )
    return tuple(normalized), tuple(errors)


def _calculate_atr_from_normalized(candles: Sequence[_Candle], period: int = DEFAULT_ATR_PERIOD) -> MaybeDecimal:
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


def _detect_swings(candles: Sequence[_Candle], lookback: int) -> tuple[_SwingPoint, ...]:
    points: list[_SwingPoint] = []
    for index in range(lookback, len(candles) - lookback):
        current = candles[index]
        left = candles[index - lookback : index]
        right = candles[index + 1 : index + lookback + 1]
        if all(current.high > candle.high for candle in (*left, *right)):
            points.append(_SwingPoint("high", current.index, current.index + lookback, current.high))
        if all(current.low < candle.low for candle in (*left, *right)):
            points.append(_SwingPoint("low", current.index, current.index + lookback, current.low))
    return tuple(points)


def _last_confirmed_before(points: Sequence[_SwingPoint], latest_index: int) -> _SwingPoint | None:
    candidates = [point for point in points if point.confirmed_at_index < latest_index]
    return candidates[-1] if candidates else None


def _sweep_confluence(
    direction: Direction,
    level: Decimal,
    points: Sequence[_SwingPoint],
    atr: Decimal,
    provided_liquidity: Any | None,
) -> str:
    if not _is_missing(provided_liquidity):
        levels = _extract_levels(provided_liquidity)
        if any(abs(candidate - level) <= atr * Decimal("0.20") for candidate in levels):
            return "Verified HTF/user liquidity"
        return "Unverified"

    nearby = [point for point in points if abs(point.price - level) <= atr * Decimal("0.10")]
    if len(nearby) >= 2:
        return "Verified equal lows" if direction == "bullish" else "Verified equal highs"
    return NA


def _order_block_from_candle(candle: _Candle, direction: Direction) -> OrderBlockZone:
    low = min(candle.open, candle.close)
    high = max(candle.open, candle.close)
    midpoint = (low + high) / Decimal("2")
    return OrderBlockZone(
        is_present=True,
        direction=direction,
        candle_index=candle.index,
        low=_quantize(low),
        high=_quantize(high),
        midpoint=_quantize(midpoint),
        body_low=_quantize(low),
        body_high=_quantize(high),
        wick_low=_quantize(candle.low),
        wick_high=_quantize(candle.high),
        freshness_status=NA,
        reason="Order block is the last opposite-color candle body before displacement/BOS.",
    )


def _order_block_from_pullback_zone(zone: PullbackZone, direction: Direction) -> OrderBlockZone:
    if not zone.is_present:
        return OrderBlockZone(reason=zone.reason if zone.reason != NA else OrderBlockZone().reason)
    low = zone.body_low if zone.body_low != NA else zone.low
    high = zone.body_high if zone.body_high != NA else zone.high
    return OrderBlockZone(
        is_present=True,
        direction=direction,
        candle_index=zone.creation_index,
        low=low,
        high=high,
        midpoint=zone.midpoint,
        body_low=zone.body_low,
        body_high=zone.body_high,
        wick_low=zone.wick_low,
        wick_high=zone.wick_high,
        freshness_status=zone.freshness_status,
        reason=zone.reason,
    )


def _fair_value_gap_from_pullback_zone(zone: PullbackZone, direction: Direction) -> FairValueGapZone:
    if not zone.is_present:
        return FairValueGapZone(reason=zone.reason if zone.reason != NA else FairValueGapZone().reason)
    return FairValueGapZone(
        is_present=True,
        direction=direction,
        candle_index=zone.creation_index,
        low=zone.low,
        high=zone.high,
        midpoint=zone.midpoint,
        fill_low=zone.fill_low,
        fill_high=zone.fill_high,
        freshness_status=zone.freshness_status,
        reason=zone.reason,
    )


def _entry_zone(
    *,
    direction: Direction,
    sweep: LiquiditySweepSignal,
    structure_shift: StructureShiftSignal,
    order_block: OrderBlockZone,
    fair_value_gap: FairValueGapZone,
    aggressive_toggle: bool,
) -> tuple[Decimal, Decimal, Decimal, str, FibAlignmentResult] | None:
    sweep_price = _sweep_wick_price(sweep)
    bos_price = _decimal_from(structure_shift.close, "structure_shift.close")
    preferred_low, preferred_high = _fib_price_zone(direction, sweep_price, bos_price, aggressive_toggle)
    source_zones: list[tuple[str, Decimal, Decimal]] = []
    if order_block.is_present and order_block.low != NA and order_block.high != NA:
        source_zones.append(("OB body/midpoint", _decimal_from(order_block.low, "ob.low"), _decimal_from(order_block.high, "ob.high")))
    if fair_value_gap.is_present and fair_value_gap.fill_low != NA and fair_value_gap.fill_high != NA:
        source_zones.append(
            (
                "FVG 50-100% fill",
                _decimal_from(fair_value_gap.fill_low, "fvg.fill_low"),
                _decimal_from(fair_value_gap.fill_high, "fvg.fill_high"),
            )
        )

    for source, low, high in source_zones:
        zone_low = max(min(low, high), min(preferred_low, preferred_high))
        zone_high = min(max(low, high), max(preferred_low, preferred_high))
        if zone_low <= zone_high:
            entry = (zone_low + zone_high) / Decimal("2")
            fib_alignment = calculate_fib_alignment(
                direction=direction,
                sweep_price=sweep_price,
                bos_price=bos_price,
                entry_price=entry,
                aggressive_toggle=aggressive_toggle,
            )
            return zone_low, zone_high, entry, source, fib_alignment
    return None


def _fib_price_zone(
    direction: Direction,
    sweep_price: Decimal,
    bos_price: Decimal,
    aggressive_toggle: bool,
) -> tuple[Decimal, Decimal]:
    impulse = abs(bos_price - sweep_price)
    max_retrace = Decimal("0.65") if aggressive_toggle else Decimal("0.618")
    if direction == "bullish":
        low = bos_price - max_retrace * impulse
        high = bos_price - Decimal("0.382") * impulse
    else:
        low = bos_price + Decimal("0.382") * impulse
        high = bos_price + max_retrace * impulse
    return min(low, high), max(low, high)


def _deepest_pullback_after_bos(
    candles: Sequence[_Candle],
    direction: Direction,
    bos_index: int,
    sweep_index: int,
    structure_shift: StructureShiftSignal,
) -> Decimal:
    if bos_index + 1 >= len(candles):
        return _decimal_from(structure_shift.close, "structure_shift.close")
    sample = candles[bos_index + 1 :]
    if direction == "bullish":
        return min(candle.low for candle in sample)
    return max(candle.high for candle in sample)


def _pullback_ratio(
    direction: Direction,
    sweep: Decimal,
    bos: Decimal,
    deepest_pullback: DecimalLike | None,
) -> Decimal | None:
    if deepest_pullback is None:
        return None
    value = _decimal_from(deepest_pullback, "deepest_pullback")
    if Decimal("0") <= value <= Decimal("1"):
        return value
    impulse = abs(bos - sweep)
    if impulse <= 0:
        return None
    if direction == "bullish":
        return (bos - value) / impulse
    return (value - bos) / impulse


def _stop_price(
    direction: Direction,
    sweep: LiquiditySweepSignal,
    order_block: OrderBlockZone,
    atr: Decimal,
    entry: Decimal,
) -> MaybeDecimal:
    sweep_price = _sweep_wick_price(sweep)
    atr_buffer = atr * Decimal("0.15")
    if direction == "bullish":
        sweep_stop = sweep_price - atr_buffer
        ob_stop = _decimal_from(order_block.low, "order_block.low") - TICK_SIZE if order_block.is_present else sweep_stop
        stop = min(sweep_stop, ob_stop)
        return _quantize(stop) if stop < entry else NA
    sweep_stop = sweep_price + atr_buffer
    ob_stop = _decimal_from(order_block.high, "order_block.high") + TICK_SIZE if order_block.is_present else sweep_stop
    stop = max(sweep_stop, ob_stop)
    return _quantize(stop) if stop > entry else NA


def _targets(
    data: LiquidityGrabInput,
    direction: Direction,
    entry: Decimal,
    stop: Decimal,
    normalized: Mapping[str, tuple[_Candle, ...]],
) -> tuple[Decimal, Decimal, Decimal | Literal["N/A"]]:
    risk = abs(entry - stop)
    if direction == "bullish":
        opposing = sorted(level for level in _extract_levels(data.user_resistance_levels) + _extract_levels(data.liquidity_above) if level > entry)
        tp1 = opposing[0] if opposing else entry + Decimal("2") * risk
        next_levels = [level for level in opposing if level > tp1]
        tp2 = next_levels[0] if next_levels else entry + Decimal("3") * risk
        tp3: Decimal | Literal["N/A"] = entry + Decimal("4") * risk if _trend_for_targets(normalized, direction) else NA
        return tp1, tp2, tp3

    opposing = sorted(
        (level for level in _extract_levels(data.user_support_levels) + _extract_levels(data.liquidity_below) if level < entry),
        reverse=True,
    )
    tp1 = opposing[0] if opposing else entry - Decimal("2") * risk
    next_levels = [level for level in opposing if level < tp1]
    tp2 = next_levels[0] if next_levels else entry - Decimal("3") * risk
    tp3 = entry - Decimal("4") * risk if _trend_for_targets(normalized, direction) else NA
    return tp1, tp2, tp3


def _risk_reward(direction: Direction, entry: Decimal, stop: Decimal, target: Decimal) -> MaybeDecimal:
    risk = abs(entry - stop)
    if risk <= 0:
        return NA
    if direction == "bullish" and target <= entry:
        return NA
    if direction == "bearish" and target >= entry:
        return NA
    return _quantize(abs(target - entry) / risk)


def _trust_meter(
    *,
    sweep: LiquiditySweepSignal,
    structure_shift: StructureShiftSignal,
    order_block: OrderBlockZone,
    fair_value_gap: FairValueGapZone,
    fib_alignment: FibAlignmentResult,
    momentum: MomentumConfirmation,
    trend: TrendLabel,
    direction: Direction,
    data: LiquidityGrabInput,
    rr_to_tp2: MaybeDecimal,
) -> TrustMeterResult:
    components = {
        "sweep_magnitude": _score_sweep_magnitude(sweep),
        "clean_bos_choch": 2 if structure_shift.is_present else 0,
        "ob_fvg_quality": (1 if order_block.is_present else 0) + (1 if fair_value_gap.is_present else 0),
        "fib_alignment": 1 if fib_alignment.is_aligned else 0,
        "volume_delta_confirmation": _score_momentum(momentum),
        "htf_bias_alignment": _score_trend_alignment(trend, direction),
        "btc_context": _score_btc_context(data, direction),
    }
    score = min(12, sum(components.values()))
    percentage = min(100, 10 * score + 20)
    if percentage >= 85:
        grade: TrustGrade = "A"
    elif percentage >= 75:
        grade = "B"
    else:
        grade = "No trade"

    risk_tier: RiskTier = "no_trade"
    rr = Decimal("0") if rr_to_tp2 == NA else _decimal_from(rr_to_tp2, "rr_to_tp2")
    if data.aggressive_toggle and score >= 9 and rr >= Decimal("3.5"):
        risk_tier = "aggressive"
    elif score >= 8 and rr >= Decimal("3.0"):
        risk_tier = "conservative"
    elif score >= 7 and rr >= BASE_MIN_RR:
        risk_tier = "base"

    return TrustMeterResult(
        score=score,
        percentage=percentage,
        grade=grade,
        risk_tier=risk_tier,
        components=components,
        reason="Trust Meter met the minimum threshold." if grade != "No trade" else "Trust Meter is below 75%.",
    )


def _score_sweep_magnitude(sweep: LiquiditySweepSignal) -> int:
    if sweep.magnitude_atr == NA:
        return 0
    magnitude_atr = _decimal_from(sweep.magnitude_atr, "sweep.magnitude_atr")
    if magnitude_atr >= Decimal("0.75"):
        return 2
    if magnitude_atr >= SWEEP_ATR_MULTIPLIER:
        return 1
    return 0


def _score_momentum(momentum: MomentumConfirmation) -> int:
    if momentum.is_confirmed:
        return 2
    if momentum.delta_status != NA or momentum.cvd != NA:
        return 1
    return 0


def _score_trend_alignment(trend: TrendLabel, direction: Direction) -> int:
    if direction == "bullish" and trend == "bullish":
        return 2
    if direction == "bearish" and trend == "bearish":
        return 2
    if trend == NA:
        return 0
    return 0


def _score_btc_context(data: LiquidityGrabInput, direction: Direction) -> int:
    if _is_missing(data.btc_context) and _is_missing(data.btc_d_context):
        return 0
    if _btc_abnormal(data.btc_context):
        return 0
    if direction == "bullish" and _btc_d_breaking_up(data.btc_d_context) and _is_alt_symbol(data.symbol):
        return 0
    if direction == "bearish" and _btc_d_breaking_down(data.btc_d_context) and _is_alt_symbol(data.symbol):
        return 0
    return 1


def _gate_result(
    *,
    data: LiquidityGrabInput,
    mode: LiquidityGrabMode,
    direction: Direction,
    rr_to_tp2: MaybeDecimal,
    trust_meter: TrustMeterResult,
    entry_source: str,
    selected: _SelectedCandles,
    candles: Sequence[_Candle],
    structure_shift: StructureShiftSignal,
    entry_low: Decimal,
    entry_high: Decimal,
    fib_alignment: FibAlignmentResult,
) -> StrategyGateResult:
    violations: list[StrategyGateViolation] = []

    if rr_to_tp2 == NA:
        violations.append(_violation("missing_rr", "RR to TP2 is N/A."))
    else:
        minimum_rr = CHALLENGE_MIN_RR if mode == LiquidityGrabMode.challenge else BASE_MIN_RR
        if _decimal_from(rr_to_tp2, "rr_to_tp2") < minimum_rr:
            violations.append(_violation("rr_below_minimum", f"RR to TP2 is below {minimum_rr}."))

    if trust_meter.grade == "No trade":
        violations.append(_violation("trust_meter_below_minimum", "Trust Meter is below 75%."))

    if fib_alignment.rejected_deeper_than_786:
        violations.append(_violation("pullback_beyond_786", "Pullback tagged beyond 0.786 before entry."))

    if mode in (LiquidityGrabMode.challenge, LiquidityGrabMode.scalp):
        max_bars = 12 if selected.timeframe == "5m" else 6
        bars_since_bos = len(candles) - 1 - int(structure_shift.candle_index)
        if bars_since_bos > max_bars and not _zone_touched_after_bos(
            candles,
            int(structure_shift.candle_index),
            direction,
            entry_low,
            entry_high,
            max_bars,
        ):
            violations.append(_violation("entry_window_expired", f"Entry was not valid within {max_bars} bars after BOS."))

    guard_violations = _risk_guard_violations(data, direction)
    violations.extend(guard_violations)

    if mode == LiquidityGrabMode.challenge:
        if trust_meter.percentage < 85:
            violations.append(_violation("challenge_trust_below_85", "Challenge mode requires Trust Meter >= 85%."))
        if rr_to_tp2 != NA and _decimal_from(rr_to_tp2, "rr_to_tp2") < CHALLENGE_MIN_RR:
            violations.append(_violation("challenge_rr_below_3", "Challenge mode requires RR >= 3.0."))
        if entry_source == NA:
            violations.append(_violation("challenge_limit_entry_missing", "Challenge mode requires limit pullback entry."))
        if _is_meme_or_illiquid(data):
            violations.append(_violation("challenge_illiquid_token", "Challenge mode rejects meme or illiquid tokens."))
        if _btc_abnormal(data.btc_context):
            violations.append(_violation("challenge_btc_abnormal", "Challenge mode rejects abnormal BTC context."))
        if _event_active(data.event_risk_context):
            violations.append(_violation("challenge_event_window", "Challenge mode rejects active major event windows."))

    return StrategyGateResult(passed=not violations, violations=tuple(_unique_violations(violations)))


def _risk_guard_violations(data: LiquidityGrabInput, direction: Direction) -> tuple[StrategyGateViolation, ...]:
    violations: list[StrategyGateViolation] = []
    if _btc_abnormal(data.btc_context) and _is_small_or_mid_cap(data):
        violations.append(_violation("btc_volatility_guard", "BTC abnormal volatility rejects small/mid-cap setup."))
    if _is_alt_symbol(data.symbol):
        if direction == "bullish" and _btc_d_breaking_up(data.btc_d_context):
            violations.append(_violation("btc_d_guard", "BTC.D breaking up intraday rejects alt longs."))
        if direction == "bearish" and _btc_d_breaking_down(data.btc_d_context):
            violations.append(_violation("btc_d_guard", "BTC.D breaking down intraday rejects alt shorts."))
    if _event_active(data.event_risk_context):
        violations.append(_violation("event_guard", "Major scheduled event window is active within +/- 30 minutes."))
    derivatives_conflict = _derivatives_conflict_against_trade(data, direction)
    if derivatives_conflict != NA:
        violations.append(_violation("derivatives_conflict", derivatives_conflict))
    if _funding_oi_against_trade(data, direction):
        violations.append(_violation("funding_oi_guard", "Extreme funding plus rising OI is directly against the trade without absorption."))
    return tuple(violations)


def _derivatives_strategy_fields(data: LiquidityGrabInput, direction: Direction) -> dict[str, Any]:
    result = _derivatives_enrichment_result(data)
    if result is None or direction not in ("bullish", "bearish"):
        return {
            "derivatives_supports_trade": NA,
            "derivatives_conflict_reason": NA,
            "funding_context": NA,
            "oi_context": NA,
            "crowding_risk": NA,
            "squeeze_risk": NA,
        }

    supports = result.supports_long if direction == "bullish" else result.supports_short
    conflict_reason = _derivatives_conflict_against_trade(data, direction)
    return {
        "derivatives_supports_trade": supports,
        "derivatives_conflict_reason": conflict_reason,
        "funding_context": result.funding_context.model_dump(),
        "oi_context": result.oi_context.model_dump(),
        "crowding_risk": result.crowding_risk,
        "squeeze_risk": result.squeeze_risk,
    }


def _derivatives_conflict_against_trade(data: LiquidityGrabInput, direction: Direction) -> str:
    result = _derivatives_enrichment_result(data)
    if result is None or direction not in ("bullish", "bearish") or _has_clear_absorption(data):
        return NA

    if direction == "bullish":
        severe = (
            result.funding_status == "extreme_positive"
            and result.oi_direction == "rising"
        ) or (
            result.crowding_risk == "high"
            and result.crowding_context.risk_direction == "long"
        ) or result.squeeze_risk == "long_squeeze_risk"
        if severe:
            return (
                "Severe derivatives conflict against long: extreme positive funding or crowded longs "
                "with rising OI and no clear absorption."
            )

    if direction == "bearish":
        severe = (
            result.funding_status == "extreme_negative"
            and result.oi_direction == "rising"
        ) or (
            result.crowding_risk == "high"
            and result.crowding_context.risk_direction == "short"
        ) or result.squeeze_risk == "short_squeeze_risk"
        if severe:
            return (
                "Severe derivatives conflict against short: extreme negative funding or crowded shorts "
                "with rising OI and no clear absorption."
            )

    return NA


def _derivatives_enrichment_result(data: LiquidityGrabInput) -> DerivativesEnrichmentResult | None:
    value = data.derivatives_enrichment
    if value is None:
        return None
    if isinstance(value, DerivativesEnrichmentResult):
        return value
    try:
        return DerivativesEnrichmentResult.model_validate(value)
    except ValueError:
        return None


def _has_clear_absorption(data: LiquidityGrabInput) -> bool:
    return _context_has(data.orderflow_summary, "absorption", "absorbed") or _context_has(
        data.liquidation_data,
        "absorption",
        "absorbed",
    )


def _entry_status(
    timeframe: str,
    mode: LiquidityGrabMode,
    candles: Sequence[_Candle],
    bos_index: int,
    direction: Direction,
    entry_low: Decimal,
    entry_high: Decimal,
) -> SetupStatus:
    max_bars = 12 if timeframe == "5m" else 6
    if mode == LiquidityGrabMode.swing:
        max_bars = max(1, len(candles) - bos_index - 1)
    return "Filled" if _zone_touched_after_bos(candles, bos_index, direction, entry_low, entry_high, max_bars) else "Pending"


def _zone_touched_after_bos(
    candles: Sequence[_Candle],
    bos_index: int,
    direction: Direction,
    entry_low: Decimal,
    entry_high: Decimal,
    max_bars: int,
) -> bool:
    sample = candles[bos_index + 1 : bos_index + 1 + max_bars]
    for candle in sample:
        if direction == "bullish" and candle.low <= entry_high and candle.high >= entry_low:
            return True
        if direction == "bearish" and candle.high >= entry_low and candle.low <= entry_high:
            return True
    return False


def _risk_guard_status(data: LiquidityGrabInput, direction: Direction) -> dict[str, str]:
    return {
        "btc_context": NA if _is_missing(data.btc_context) else ("Rejected" if _btc_abnormal(data.btc_context) else "Supportive"),
        "btc_d_context": NA
        if _is_missing(data.btc_d_context)
        else ("Rejected" if (direction == "bullish" and _btc_d_breaking_up(data.btc_d_context)) or (direction == "bearish" and _btc_d_breaking_down(data.btc_d_context)) else "Supportive"),
        "event_context": NA if _is_missing(data.event_risk_context) else ("Rejected" if _event_active(data.event_risk_context) else "Clear"),
    }


def _rejected_setup(
    mode: LiquidityGrabMode,
    code: str,
    message: str,
    missing_data: tuple[str, ...],
    unverified_data: tuple[str, ...],
    rotation: RotationContext,
    *,
    timeframe: str = NA,
    current_price: MaybeDecimal = NA,
    sweep: LiquiditySweepSignal | None = None,
    structure_shift: StructureShiftSignal | None = None,
    order_block: OrderBlockZone | None = None,
    fair_value_gap: FairValueGapZone | None = None,
    fib_alignment: FibAlignmentResult | None = None,
    pullback_zone: PullbackZoneResult | None = None,
    entry_low: MaybeDecimal = NA,
    entry_high: MaybeDecimal = NA,
    entry: MaybeDecimal = NA,
    entry_source: str = NA,
    stop: MaybeDecimal = NA,
    tp1: MaybeDecimal = NA,
    tp2: MaybeDecimal = NA,
    tp3: MaybeDecimal = NA,
    rr_to_tp2: MaybeDecimal = NA,
    context_fields: Mapping[str, Any] | None = None,
) -> LiquidityGrabSetup:
    context_fields = context_fields or {}
    pullback_zone = pullback_zone or PullbackZoneResult()
    return LiquidityGrabSetup(
        mode=mode,
        timeframe=timeframe,
        current_price=current_price,
        sweep=sweep or LiquiditySweepSignal(),
        structure_shift=structure_shift or StructureShiftSignal(),
        order_block=order_block or OrderBlockZone(),
        fair_value_gap=fair_value_gap or FairValueGapZone(),
        fib_alignment=fib_alignment or FibAlignmentResult(),
        pullback_zone=pullback_zone,
        pullback_zone_status=pullback_zone.pullback_zone_status,
        pullback_calculation_timeframe=pullback_zone.calculation_timeframe,
        pullback_sweep_candle_index=pullback_zone.sweep_candle_index,
        pullback_bos_choch_candle_index=pullback_zone.bos_choch_candle_index,
        displacement_start_index=pullback_zone.displacement_start_index,
        displacement_end_index=pullback_zone.displacement_end_index,
        selected_zone_type=pullback_zone.selected_zone_type,
        ob_zone=pullback_zone.ob_zone,
        fvg_zone=pullback_zone.fvg_zone,
        fib_382=pullback_zone.fib_382,
        fib_618=pullback_zone.fib_618,
        fib_65=pullback_zone.fib_65,
        fib_786=pullback_zone.fib_786,
        pullback_depth_ratio=pullback_zone.pullback_depth_ratio,
        pullback_failure_reason=pullback_zone.pullback_failure_reason,
        atr_stop_buffer=pullback_zone.atr_stop_buffer,
        entry_low=entry_low,
        entry_high=entry_high,
        entry=entry,
        entry_source=entry_source,
        stop=stop,
        tp1=tp1,
        tp2=tp2,
        tp3=tp3,
        rr_to_tp2=rr_to_tp2,
        gate_result=StrategyGateResult(passed=False, violations=(_violation(code, message),)),
        missing_data=missing_data,
        unverified_data=unverified_data,
        rotation=rotation,
        **context_fields,
    )


def _setup_for_mode(
    mode: LiquidityGrabMode,
    challenge: LiquidityGrabSetup,
    swing: LiquidityGrabSetup,
    scalp: LiquidityGrabSetup,
) -> LiquidityGrabSetup:
    if mode == LiquidityGrabMode.challenge:
        return challenge
    if mode == LiquidityGrabMode.scalp:
        return scalp
    return swing


def _with_setup_diagnostics(symbol: str, setup: LiquidityGrabSetup) -> LiquidityGrabSetup:
    fields = _setup_diagnostic_fields(setup)
    diagnosed = setup.model_copy(update=fields)
    return diagnosed.model_copy(update={"strategy_diagnostics": _format_setup_diagnostics(symbol, diagnosed)})


def _setup_diagnostic_fields(setup: LiquidityGrabSetup) -> dict[str, Any]:
    gates_failed = tuple(violation.code for violation in setup.gate_result.violations)
    hard_rejection_reasons = tuple(violation.message for violation in setup.gate_result.violations)
    return {
        "first_failed_gate": gates_failed[0] if gates_failed else NA,
        "gates_passed": _gates_passed(setup, gates_failed),
        "gates_failed": gates_failed,
        "hard_rejection_reasons": hard_rejection_reasons,
        "execution_sweep_status": _execution_sweep_status(setup),
        "confirmation_structure_shift_status": _confirmation_structure_shift_status(setup),
        "confirmation_bos_choch_reason": _confirmation_bos_choch_reason(setup),
        "pullback_zone_status": setup.pullback_zone.pullback_zone_status,
        "pullback_calculation_timeframe": setup.pullback_zone.calculation_timeframe,
        "pullback_sweep_candle_index": setup.pullback_zone.sweep_candle_index,
        "pullback_bos_choch_candle_index": setup.pullback_zone.bos_choch_candle_index,
        "displacement_start_index": setup.pullback_zone.displacement_start_index,
        "displacement_end_index": setup.pullback_zone.displacement_end_index,
        "selected_zone_type": setup.pullback_zone.selected_zone_type,
        "ob_zone": setup.pullback_zone.ob_zone,
        "fvg_zone": setup.pullback_zone.fvg_zone,
        "fib_382": setup.pullback_zone.fib_382,
        "fib_618": setup.pullback_zone.fib_618,
        "fib_65": setup.pullback_zone.fib_65,
        "fib_786": setup.pullback_zone.fib_786,
        "pullback_depth_ratio": setup.pullback_zone.pullback_depth_ratio,
        "pullback_failure_reason": setup.pullback_zone.pullback_failure_reason,
        "atr_stop_buffer": setup.pullback_zone.atr_stop_buffer,
        "sweep_diagnostics": _sweep_diagnostics(setup.sweep),
        "structure_shift_diagnostics": _structure_shift_diagnostics(setup.sweep, setup.structure_shift),
        "ob_fvg_diagnostics": _ob_fvg_diagnostics(setup.structure_shift, setup.order_block, setup.fair_value_gap),
        "pullback_zone_diagnostics": _pullback_zone_diagnostics(setup),
        "fib_diagnostics": _fib_diagnostics(setup),
        "momentum_diagnostics": _momentum_diagnostics(setup),
        "rr_diagnostics": _rr_diagnostics(setup),
        "trust_meter_diagnostics": _trust_meter_diagnostics(setup),
        "derivatives_supports_trade": setup.derivatives_supports_trade,
        "derivatives_conflict_reason": setup.derivatives_conflict_reason,
        "funding_context": setup.funding_context,
        "oi_context": setup.oi_context,
        "crowding_risk": setup.crowding_risk,
        "squeeze_risk": setup.squeeze_risk,
        "poc_diagnostics": setup.poc_diagnostics
        if setup.poc_diagnostics != NA
        else _setup_poc_diagnostics(setup),
    }


def _execution_sweep_status(setup: LiquidityGrabSetup) -> str:
    if setup.execution_timeframe == NA:
        return NA
    return "passed" if setup.sweep.is_present else "failed"


def _confirmation_structure_shift_status(setup: LiquidityGrabSetup) -> str:
    if setup.confirmation_timeframe == NA:
        return NA
    if not setup.sweep.is_present:
        return "not_evaluated"
    return "passed" if setup.structure_shift.is_present else "failed"


def _confirmation_bos_choch_reason(setup: LiquidityGrabSetup) -> str:
    if setup.confirmation_timeframe == NA:
        for violation in setup.gate_result.violations:
            if violation.code in ("missing_confirmation_structure_shift", "missing_confirmation_candles"):
                return violation.message
        return "5m confirmation candles missing."
    if not setup.sweep.is_present:
        return "N/A because 15m execution sweep failed."
    return setup.structure_shift.reason


def _gates_passed(setup: LiquidityGrabSetup, gates_failed: tuple[str, ...]) -> tuple[str, ...]:
    passed: list[str] = []
    if setup.sweep.is_present:
        passed.append("sweep")
    if setup.structure_shift.is_present:
        passed.append("bos_choch")
    if setup.order_block.is_present or setup.fair_value_gap.is_present:
        passed.append("ob_fvg")
    if setup.pullback_zone.valid:
        passed.append("pullback_zone")
    if setup.fib_alignment.is_aligned and not setup.fib_alignment.rejected_deeper_than_786:
        passed.append("fib_alignment")
    if setup.momentum.is_confirmed:
        passed.append("volume_confirmation")
    if setup.rr_to_tp2 != NA and not any(code in gates_failed for code in ("missing_rr", "rr_below_minimum", "challenge_rr_below_3")):
        passed.append("rr")
    if setup.trust_meter.grade != "No trade" and not any(
        code in gates_failed for code in ("trust_meter_below_minimum", "challenge_trust_below_85")
    ):
        passed.append("trust_meter")
    if setup.gate_result.passed:
        passed.append("final_strategy_gates")
    return tuple(passed)


def _sweep_diagnostics(sweep: LiquiditySweepSignal) -> str:
    if not sweep.is_present:
        reason = sweep.reason
        if reason == LiquiditySweepSignal().reason:
            reason = "No candle swept prior swing by at least 0.35 ATR and closed back inside."
        return f"failed: {reason}"
    return (
        f"passed: {sweep.direction} sweep at candle {_display(sweep.candle_index)}; "
        f"magnitude {_display(sweep.magnitude)} ({_display(sweep.magnitude_atr)} ATR)."
    )


def _structure_shift_diagnostics(sweep: LiquiditySweepSignal, structure_shift: StructureShiftSignal) -> str:
    if not sweep.is_present:
        return "N/A because sweep failed."
    if not structure_shift.is_present:
        return f"failed: {structure_shift.reason}"
    return (
        f"passed: {structure_shift.kind} {structure_shift.direction} at {_display(structure_shift.level)} "
        f"from candle close {_display(structure_shift.close)}."
    )


def _ob_fvg_diagnostics(
    structure_shift: StructureShiftSignal,
    order_block: OrderBlockZone,
    fair_value_gap: FairValueGapZone,
) -> str:
    if not structure_shift.is_present:
        return "N/A because BOS/CHoCH failed."
    ob_status = "found" if order_block.is_present else "missing"
    fvg_status = "found" if fair_value_gap.is_present else "missing"
    if not order_block.is_present and not fair_value_gap.is_present:
        return "failed: OB missing; FVG missing."
    return f"passed: OB {ob_status}; FVG {fvg_status}."


def _pullback_zone_diagnostics(setup: LiquidityGrabSetup) -> str:
    if not setup.sweep.is_present:
        return "N/A because sweep failed."
    if not setup.structure_shift.is_present:
        return "N/A because BOS/CHoCH failed."
    status = setup.pullback_zone.pullback_zone_status
    source = setup.pullback_zone.calculation_timeframe
    source_text = f" on {source}" if source != NA else ""
    if status == "valid":
        return f"valid{source_text}: {setup.pullback_zone.selected_zone_type} overlaps fib; RR {_display(setup.pullback_zone.rr_to_tp2)}."
    reason = setup.pullback_zone.pullback_failure_reason
    if reason == NA:
        reason = "Required OB/FVG pullback zone with fib alignment is missing."
    return f"failed{source_text}: {reason}"


def _fib_diagnostics(setup: LiquidityGrabSetup) -> str:
    if not setup.structure_shift.is_present:
        return "N/A."
    if setup.pullback_zone.first_failed_gate == "pullback_too_deep":
        return f"failed: {setup.pullback_zone.pullback_failure_reason}"
    if not setup.order_block.is_present and not setup.fair_value_gap.is_present:
        return "N/A because OB/FVG failed."
    if setup.fib_alignment.is_aligned and not setup.fib_alignment.rejected_deeper_than_786:
        return f"passed: {setup.fib_alignment.reason}"
    return f"failed: {setup.fib_alignment.reason}"


def _momentum_diagnostics(setup: LiquidityGrabSetup) -> str:
    if not setup.sweep.is_present or not setup.structure_shift.is_present:
        return "N/A."
    if setup.momentum.is_confirmed:
        return f"passed: {setup.momentum.reason}"
    if setup.momentum.volume_status == "not_confirmed":
        return f"failed: {setup.momentum.reason}"
    return f"N/A: {setup.momentum.reason}"


def _rr_diagnostics(setup: LiquidityGrabSetup) -> str:
    rr_messages = [
        violation.message
        for violation in setup.gate_result.violations
        if violation.code in ("missing_rr", "rr_below_minimum", "challenge_rr_below_3", "rr_too_low")
    ]
    if setup.rr_to_tp2 == NA:
        if rr_messages:
            return f"failed: {'; '.join(rr_messages)}"
        return "N/A."
    if rr_messages:
        return f"failed: RR to TP2 {_display(setup.rr_to_tp2)}. {'; '.join(rr_messages)}"
    return f"passed: RR to TP2 {_display(setup.rr_to_tp2)}."


def _trust_meter_diagnostics(setup: LiquidityGrabSetup) -> str:
    trust_messages = [
        violation.message
        for violation in setup.gate_result.violations
        if violation.code in ("trust_meter_below_minimum", "challenge_trust_below_85")
    ]
    if setup.trust_meter.grade == "No trade":
        if setup.trust_meter.percentage == 0 and not trust_messages:
            return "N/A."
        message = "; ".join(trust_messages) if trust_messages else setup.trust_meter.reason
        return f"failed: Trust Meter {setup.trust_meter.percentage}% ({setup.trust_meter.grade}). {message}"
    return f"passed: Trust Meter {setup.trust_meter.percentage}% ({setup.trust_meter.grade})."


def _setup_poc_diagnostics(setup: LiquidityGrabSetup) -> str:
    if setup.poc != NA:
        return "POC available from estimated candle volume profile."
    return "POC N/A because volume data missing/insufficient."


def _format_setup_diagnostics(symbol: str, setup: LiquidityGrabSetup) -> str:
    sweep_status, sweep_reason = _split_diagnostic(setup.sweep_diagnostics)
    lines = [
        f"Liquidity-Grab Diagnostics - {symbol}",
        f"Mode: {setup.mode.value}",
        f"{setup.htf_timeframe.upper()} HTF context: {_context_source_text(setup.htf_2d_context_source)}; candles={setup.candles_2d_count}; trend={setup.htf_2d_trend}",
        f"{setup.bias_timeframe.upper()} bias: {'direct' if setup.candles_12h_count > 0 else NA}; candles={setup.candles_12h_count}; trend={setup.mtf_12h_trend}",
        f"POC: {_display(setup.poc)}; source={_display(setup.volume_profile_source)}; {setup.poc_diagnostics}",
        f"{setup.execution_timeframe} execution sweep: {setup.execution_sweep_status}",
        f"{setup.confirmation_timeframe} confirmation BOS/CHoCH: {setup.confirmation_structure_shift_status}",
        f"Sweep: {sweep_status}",
    ]
    if sweep_reason:
        lines.append(f"Reason: {sweep_reason}")
    lines.extend(
        (
            f"{setup.confirmation_timeframe} BOS/CHoCH: {_diagnostic_detail(setup.structure_shift_diagnostics)}",
            f"Pullback Zone: {_diagnostic_detail(setup.pullback_zone_diagnostics)}",
            f"Pullback source: {_display(setup.pullback_calculation_timeframe)}; sweep index {_display(setup.pullback_sweep_candle_index)}; BOS/CHoCH index {_display(setup.pullback_bos_choch_candle_index)}",
            f"OB: {_pullback_zone_text(setup.ob_zone)}",
            f"FVG: {_pullback_zone_text(setup.fvg_zone)}",
            f"Fib: {_display(setup.fib_alignment.status)}",
            f"RR: {_display(setup.rr_to_tp2)}",
            f"OB/FVG: {_diagnostic_detail(setup.ob_fvg_diagnostics)}",
            f"Fib alignment: {_diagnostic_detail(setup.fib_diagnostics)}",
            f"Momentum: {_diagnostic_detail(setup.momentum_diagnostics)}",
            f"RR: {_diagnostic_detail(setup.rr_diagnostics)}",
            f"Trust Meter: {_diagnostic_detail(setup.trust_meter_diagnostics)}",
            f"Derivatives support: {_display(setup.derivatives_supports_trade)}",
            f"Derivatives conflict: {_display(setup.derivatives_conflict_reason)}",
            f"Crowding risk: {_display(setup.crowding_risk)}",
            f"Squeeze risk: {_display(setup.squeeze_risk)}",
            f"First failed gate: {setup.first_failed_gate}",
            f"Final decision: {'Valid setup.' if setup.is_valid else 'No valid setup.'}",
        )
    )
    return "\n".join(lines)


def _split_diagnostic(value: str) -> tuple[str, str]:
    if ": " not in value:
        return value, ""
    status, reason = value.split(": ", 1)
    return status, reason


def _diagnostic_detail(value: str) -> str:
    status, reason = _split_diagnostic(value)
    if reason:
        return f"{status} - {reason}"
    return status


def _format_result(
    symbol: str,
    data: LiquidityGrabInput,
    challenge: LiquidityGrabSetup,
    swing: LiquidityGrabSetup,
    scalp: LiquidityGrabSetup,
) -> StrategyFormattedOutput:
    challenge_text = _format_challenge(symbol, data, challenge)
    swing_text = _format_swing(symbol, data, swing)
    scalp_text = _format_scalp(symbol, data, scalp)
    full_text = "\n\n".join(
        (
            "🟢 Challenge Setup\n" + challenge_text,
            "🔵 Swing Setup\n" + swing_text,
            "🔴 Scalp Setup\n" + scalp_text,
            "⚔️ Candle Craft | Signal. Structure. Execution.",
        )
    )
    return StrategyFormattedOutput(
        challenge_setup="No valid challenge setup." if not challenge.is_valid else challenge_text,
        swing_setup=swing_text,
        scalp_setup=scalp_text,
        full_text=full_text,
    )


def _format_challenge(symbol: str, data: LiquidityGrabInput, setup: LiquidityGrabSetup) -> str:
    if not setup.is_valid:
        return "No valid challenge setup."
    common = _common_structure(data, setup, challenge=True)
    return "\n".join(
        (
            "1) HTF Structure (2D)",
            f"• Current price: [{_display(setup.current_price)}].",
            f"• Trend: [{setup.trend}].",
            f"• Key levels: Support [{_levels_text(data.user_support_levels)}], Resistance [{_levels_text(data.user_resistance_levels)}].",
            "",
            "2) Orderflow + Derivatives",
            *common["orderflow"],
            "",
            "3) Trade Map (Challenge Rules)",
            *common["trade_map"],
            "• Risk %: 5% (Challenge rules).",
            f"• Trust Meter: [{setup.trust_meter.grade} + {setup.trust_meter.percentage}%].",
            f"👉 {symbol} = Challenge {setup.trust_meter.grade}. Status: {setup.status}.",
            *_rotation_lines(setup.rotation),
        )
    )


def _format_swing(symbol: str, data: LiquidityGrabInput, setup: LiquidityGrabSetup) -> str:
    if not setup.is_valid:
        return "No valid swing setup."
    common = _common_structure(data, setup, challenge=False)
    return "\n".join(
        (
            "1) HTF Structure (2D)",
            f"• Current price: [{_display(setup.current_price)}].",
            f"• Trend: [{setup.trend}].",
            f"• Key levels: Support [{_levels_text(data.user_support_levels)}], Resistance [{_levels_text(data.user_resistance_levels)}].",
            "",
            "2) Orderflow + Derivatives",
            *common["orderflow"],
            "",
            "3) Trade Map",
            *common["trade_map"],
            f"• Trust Meter: [{setup.trust_meter.grade} + {setup.trust_meter.percentage}%].",
            f"👉 {symbol} = Swing {setup.trust_meter.grade}.",
            *_rotation_lines(setup.rotation),
        )
    )


def _format_scalp(symbol: str, data: LiquidityGrabInput, setup: LiquidityGrabSetup) -> str:
    if not setup.is_valid:
        return "No valid scalp setup."
    return "\n".join(
        (
            "1) HTF/MTF Bias (12H/4H)",
            f"• Price: [{_display(setup.current_price)}].",
            f"• Trend: [{setup.trend}].",
            f"• Sweep targets: [{_sweep_targets_text(data)}].",
            "",
            "2) LTF (15m Execution)",
            f"• BOS/CHoCH: [{setup.structure_shift.kind} at {_display(setup.structure_shift.level)}].",
            f"• OB/FVG alignment: [{setup.entry_source}].",
            f"• Liquidity: Below [{_levels_text(data.liquidity_below)}], Above [{_levels_text(data.liquidity_above)}].",
            f"• Volume/Delta: [{setup.momentum.volume_status} / {setup.momentum.delta_status}].",
            "",
            "3) Derivatives",
            f"• OI: [{_context_text(data.open_interest)}].",
            f"• Funding: [{_context_text(data.funding)}].",
            f"• CVD: [{_context_text(data.cvd)}].",
            f"• Liqs: [{_context_text(data.liquidation_data)}].",
            "",
            "4) Trade Map",
            f"• Bias: [{setup.bias}].",
            f"• Sweep Zone: [{_display(setup.sweep.wick_price)} -> {_display(setup.sweep.swing_level)}].",
            f"• Pullback Zone: [{setup.pullback_zone_status} | OB/FVG: {setup.selected_zone_type} | Fib: {_display(setup.fib_alignment.status)} | RR: {_display(setup.rr_to_tp2)}].",
            f"• Entry: [{_zone_text(setup.entry_low, setup.entry_high)}].",
            f"• Stop: [{_display(setup.stop)}].",
            f"• TPs: [TP1 {_display(setup.tp1)}], [TP2 {_display(setup.tp2)}], [TP3 opt {_display(setup.tp3)}].",
            f"• RR: [{_display(setup.rr_to_tp2)}].",
            f"• Trust Meter: [{setup.trust_meter.grade} + {setup.trust_meter.percentage}%].",
            f"👉 {symbol} = Scalp {setup.trust_meter.grade}.",
            *_rotation_lines(setup.rotation),
        )
    )


def _common_structure(data: LiquidityGrabInput, setup: LiquidityGrabSetup, *, challenge: bool) -> dict[str, list[str]]:
    return {
        "orderflow": [
            f"• POC: [{_context_text(data.poc)}].",
            f"• Liquidity: Below [{_levels_text(data.liquidity_below)}], Above [{_levels_text(data.liquidity_above)}].",
            f"• Orderflow: [{_context_text(data.orderflow_summary)}].",
            f"• OI/Funding/CVD: [{_context_text(data.open_interest)} / {_context_text(data.funding)} / {_context_text(data.cvd)}].",
        ],
        "trade_map": [
            f"• Bias: [{setup.bias}].",
            f"• Sweep Zone: [{_display(setup.sweep.wick_price)} -> {_display(setup.sweep.swing_level)}].",
            f"• Entry: [{_zone_text(setup.entry_low, setup.entry_high)}].",
            f"• Stop: [{_display(setup.stop)}].",
            f"• TPs: [TP1 {_display(setup.tp1)}], [TP2 {_display(setup.tp2)}], [TP3 opt {_display(setup.tp3)}].",
            f"• RR: [{_display(setup.rr_to_tp2)}].",
        ],
    }


def _rotation_lines(rotation: RotationContext) -> tuple[str, str, str]:
    return (
        f"• Sector rotation: [{rotation.sector_rotation}].",
        f"• Narrative: [{rotation.narrative}].",
        f"• Key play: [{rotation.key_play}].",
    )


def _select_execution_candles(
    normalized: Mapping[str, tuple[_Candle, ...]],
    data: LiquidityGrabInput,
) -> _SelectedCandles | None:
    timeframe = data.execution_timeframe
    candles = normalized.get(timeframe)
    if candles:
        return _SelectedCandles(timeframe, candles)
    return None


def _select_confirmation_candles(
    normalized: Mapping[str, tuple[_Candle, ...]],
    data: LiquidityGrabInput,
) -> _SelectedCandles | None:
    timeframe = data.confirmation_timeframe
    candles = normalized.get(timeframe)
    if candles:
        return _SelectedCandles(timeframe, candles)
    return None


def _sweep_for_confirmation_candles(
    sweep: LiquiditySweepSignal,
    execution_candles: Sequence[_Candle],
    confirmation_candles: Sequence[_Candle],
) -> LiquiditySweepSignal:
    mapped_index = _map_candle_index(
        int(sweep.candle_index),
        source_candles=execution_candles,
        target_candles=confirmation_candles,
    )
    return sweep.model_copy(update={"candle_index": mapped_index})


def _map_candle_index(
    source_index: int,
    *,
    source_candles: Sequence[_Candle],
    target_candles: Sequence[_Candle],
) -> int:
    if not target_candles:
        return 0
    if not source_candles:
        return min(max(source_index, 0), len(target_candles) - 1)

    clamped_source_index = min(max(source_index, 0), len(source_candles) - 1)
    source_timestamp = source_candles[clamped_source_index].timestamp
    if source_timestamp != NA:
        candidates = [
            candle.index
            for candle in target_candles
            if candle.timestamp != NA and int(candle.timestamp) <= int(source_timestamp)
        ]
        if candidates:
            return min(max(candidates[-1], 0), len(target_candles) - 1)
        timestamped = [candle.index for candle in target_candles if candle.timestamp != NA]
        if timestamped:
            return min(max(timestamped[0] - 1, 0), len(target_candles) - 1)

    ratio = Decimal(clamped_source_index + 1) / Decimal(len(source_candles))
    mapped = int((ratio * Decimal(len(target_candles))).to_integral_value(rounding="ROUND_FLOOR")) - 1
    return min(max(mapped, 0), len(target_candles) - 1)


def _confirmation_failure_shift(structure_shift: StructureShiftSignal) -> StructureShiftSignal:
    reason = structure_shift.reason
    if reason in (StructureShiftSignal().reason, "No BOS/CHoCH close beyond the required LTF swing."):
        reason = "No 5m BOS/CHoCH close beyond the required LTF swing."
    return structure_shift.model_copy(update={"reason": reason})


def _trend_for_mode(normalized: Mapping[str, tuple[_Candle, ...]], mode: LiquidityGrabMode) -> TrendLabel:
    if mode == LiquidityGrabMode.scalp:
        for timeframe in ("12h", "4h", "6h", "1h"):
            if normalized.get(timeframe):
                return _simple_trend(normalized[timeframe])
    else:
        for timeframe in ("12h", "2d", "4h", "6h", "1h"):
            if normalized.get(timeframe):
                return _simple_trend(normalized[timeframe])
    return NA


def _timeframe_context_fields(
    data: LiquidityGrabInput,
    normalized: Mapping[str, tuple[_Candle, ...]],
    execution: _SelectedCandles | None,
    confirmation: _SelectedCandles | None,
    *,
    structure_shift: StructureShiftSignal | None = None,
) -> dict[str, Any]:
    candles_2d = normalized.get("2d", ())
    candles_12h = normalized.get("12h", ())
    candles_15m = normalized.get("15m", ())
    candles_5m = normalized.get("5m", ())
    source = data.htf_2d_context_source if candles_2d and data.htf_2d_context_source != NA else NA
    confirmation_timeframe = confirmation.timeframe if confirmation is not None else NA
    if confirmation is None:
        ltf_status = NA
    elif structure_shift is not None and structure_shift.is_present:
        ltf_status = "confirmed"
    else:
        ltf_status = "missing"

    return {
        "htf_timeframe": data.htf_timeframe,
        "bias_timeframe": data.bias_timeframe,
        "execution_timeframe": execution.timeframe if execution is not None else NA,
        "confirmation_timeframe": confirmation_timeframe,
        "htf_2d_context_source": source,
        "poc": data.poc if not _is_missing(data.poc) else NA,
        "volume_profile_source": _context_text(data.volume_profile_source),
        "poc_diagnostics": _poc_context_diagnostics(data),
        "candles_2d_count": len(candles_2d),
        "candles_12h_count": len(candles_12h),
        "candles_15m_count": len(candles_15m),
        "candles_5m_count": len(candles_5m),
        "htf_2d_trend": _simple_trend(candles_2d) if candles_2d else NA,
        "mtf_12h_trend": _simple_trend(candles_12h) if candles_12h else NA,
        "ltf_confirmation_timeframe": confirmation_timeframe,
        "ltf_confirmation_status": ltf_status,
    }


def _trend_for_targets(normalized: Mapping[str, tuple[_Candle, ...]], direction: Direction) -> bool:
    trend = _trend_for_mode(normalized, LiquidityGrabMode.swing)
    return (direction == "bullish" and trend == "bullish") or (direction == "bearish" and trend == "bearish")


def _simple_trend(candles: Sequence[_Candle]) -> TrendLabel:
    if len(candles) < 2:
        return NA
    first = candles[0].close
    latest = candles[-1].close
    if latest > first:
        return "bullish"
    if latest < first:
        return "bearish"
    return "neutral"


def _trend_before_index(candles: Sequence[_Candle], index: int) -> TrendLabel:
    sample = candles[: max(0, index) + 1]
    return _simple_trend(sample[-20:] if len(sample) >= 20 else sample)


def _current_price(data: LiquidityGrabInput, candles: Sequence[_Candle]) -> MaybeDecimal:
    if data.current_price is not None:
        return _quantize(data.current_price)
    if candles:
        return _quantize(candles[-1].close)
    return NA


def _missing_context(data: LiquidityGrabInput, normalized: Mapping[str, tuple[_Candle, ...]]) -> tuple[str, ...]:
    missing: list[str] = []
    for field in (
        "poc",
        "liquidity_below",
        "liquidity_above",
        "orderflow_summary",
        "funding",
        "open_interest",
        "cvd",
        "liquidation_data",
        "btc_context",
        "btc_d_context",
        "event_risk_context",
        "weekend_filter",
        "sector_rotation",
        "narrative",
    ):
        if _is_missing(getattr(data, field)):
            missing.append(f"{field}: N/A")
    for timeframe in ("2d", "12h", "15m", "5m"):
        if not normalized.get(timeframe):
            missing.append(f"candles_{timeframe}: N/A")
    if not normalized:
        missing.append("candles: N/A")
    return tuple(missing)


def _unverified_context(data: LiquidityGrabInput) -> tuple[str, ...]:
    unverified: list[str] = []
    for field in (
        "poc",
        "liquidity_below",
        "liquidity_above",
        "orderflow_summary",
        "funding",
        "open_interest",
        "cvd",
        "liquidation_data",
        "btc_context",
        "btc_d_context",
        "event_risk_context",
    ):
        text = _context_text(getattr(data, field))
        if text != NA and "unverified" in text.lower():
            unverified.append(f"{field}: Unverified")
    return tuple(unverified)


def _key_play(data: LiquidityGrabInput) -> str:
    if not _is_missing(data.narrative):
        return _context_text(data.narrative)
    if not _is_missing(data.sector_rotation):
        return _context_text(data.sector_rotation)
    return NA


def _poc_context_diagnostics(data: LiquidityGrabInput) -> str:
    if not _is_missing(data.poc):
        return "POC available from estimated candle volume profile."
    warning_text = _context_text(data.volume_profile_warnings)
    if warning_text != NA:
        return f"POC N/A because volume data missing/insufficient: {warning_text}"
    return "POC N/A because volume data missing/insufficient."


def _sweep_wick_price(sweep: LiquiditySweepSignal) -> Decimal:
    return _decimal_from(sweep.wick_price, "sweep.wick_price")


def _bos_impulse_price(candle: _Candle, direction: Direction) -> Decimal:
    if direction == "bullish":
        return max(candle.high, candle.close)
    return min(candle.low, candle.close)


def _invalidation(direction: Direction, stop: Decimal) -> str:
    if direction == "bullish":
        return f"Invalid if price accepts below {_display(stop)}."
    return f"Invalid if price accepts above {_display(stop)}."


def _violation(code: str, message: str) -> StrategyGateViolation:
    return StrategyGateViolation(code=code, message=message)


def _unique_violations(violations: Sequence[StrategyGateViolation]) -> tuple[StrategyGateViolation, ...]:
    output: list[StrategyGateViolation] = []
    seen: set[str] = set()
    for violation in violations:
        key = f"{violation.code}:{violation.message}"
        if key not in seen:
            seen.add(key)
            output.append(violation)
    return tuple(output)


def _extract_levels(value: Any | None) -> list[Decimal]:
    if _is_missing(value):
        return []
    if isinstance(value, Mapping):
        levels: list[Decimal] = []
        for key in ("levels", "prices", "support", "resistance", "below", "above"):
            if key in value:
                levels.extend(_extract_levels(value[key]))
        if not levels:
            for item in value.values():
                try:
                    levels.append(_decimal_from(item, "level"))
                except ValueError:
                    continue
        return levels
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        levels = []
        for item in value:
            levels.extend(_extract_levels(item))
        return levels
    try:
        return [_decimal_from(value, "level")]
    except ValueError:
        return []


def _levels_text(value: Any | None) -> str:
    levels = _extract_levels(value)
    if levels:
        return ", ".join(_display(_quantize(level)) for level in levels)
    return _context_text(value)


def _sweep_targets_text(data: LiquidityGrabInput) -> str:
    below = _levels_text(data.liquidity_below)
    above = _levels_text(data.liquidity_above)
    return f"Below {below}, Above {above}"


def _zone_text(low: MaybeDecimal, high: MaybeDecimal) -> str:
    if low == NA or high == NA:
        return NA
    return f"{_display(low)} - {_display(high)}"


def _pullback_zone_text(zone: PullbackZone) -> str:
    if not zone.is_present or zone.low == NA or zone.high == NA:
        return NA
    return f"{zone.zone_type} {_display(zone.low)} - {_display(zone.high)} ({zone.freshness_status})"


def _display(value: Any) -> str:
    if value == NA or value is None:
        return NA
    if isinstance(value, Decimal):
        text = format(value.quantize(OUTPUT_QUANT), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _context_source_text(value: str) -> str:
    if value == "synthetic_from_1d":
        return "synthetic from 1D"
    return value if value else NA


def _context_text(value: Any | None) -> str:
    if _is_missing(value):
        return NA
    if isinstance(value, Mapping):
        for key in ("summary", "status", "context", "value", "description"):
            if key in value and not _is_missing(value[key]):
                return str(value[key])
        return ", ".join(f"{key}={item}" for key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if not value:
            return NA
        return ", ".join(str(item) for item in value)
    return str(value).strip() or NA


def _context_has(value: Any | None, *phrases: str) -> bool:
    if _is_missing(value):
        return False
    if isinstance(value, Mapping):
        for phrase in phrases:
            if bool(value.get(phrase)):
                return True
        return any(_context_has(item, *phrases) for item in value.values())
    text = _context_text(value).lower()
    return any(phrase.replace("_", " ") in text or phrase in text for phrase in phrases)


def _btc_abnormal(value: Any | None) -> bool:
    return _context_has(value, "abnormal_volatility", "abnormal volatility", "high volatility", "volatile")


def _btc_d_breaking_up(value: Any | None) -> bool:
    return _context_has(value, "breaking_up_intraday", "breaking up intraday", "btc.d up", "dominance up")


def _btc_d_breaking_down(value: Any | None) -> bool:
    return _context_has(value, "breaking_down_intraday", "breaking down intraday", "btc.d down", "dominance down")


def _event_active(value: Any | None) -> bool:
    return _context_has(value, "active", "within_30_minutes", "within 30 minutes", "major scheduled news")


def _is_alt_symbol(symbol: str) -> bool:
    upper = symbol.upper()
    return not (upper.startswith("BTC") or upper.startswith("ETH"))


def _is_small_or_mid_cap(data: LiquidityGrabInput) -> bool:
    text = f"{_context_text(data.token_classification)} {_context_text(data.narrative)} {_context_text(data.sector_rotation)}".lower()
    return "small" in text or "mid" in text or "meme" in text or "illiquid" in text


def _is_meme_or_illiquid(data: LiquidityGrabInput) -> bool:
    text = f"{_context_text(data.token_classification)} {_context_text(data.narrative)} {_context_text(data.sector_rotation)}".lower()
    return "meme" in text or "illiquid" in text


def _funding_oi_against_trade(data: LiquidityGrabInput, direction: Direction) -> bool:
    funding_text = _context_text(data.funding).lower()
    oi_text = _context_text(data.open_interest).lower()
    orderflow_text = _context_text(data.orderflow_summary).lower()
    rising_oi = "rising" in oi_text or "increasing" in oi_text or _context_has(data.open_interest, "rising", "increasing")
    absorption = "absorption" in orderflow_text
    if not rising_oi or absorption:
        return False
    if direction == "bullish":
        return ("extreme" in funding_text and "positive" in funding_text) or _funding_extreme_signed(data.funding, positive=True)
    if direction == "bearish":
        return ("extreme" in funding_text and "negative" in funding_text) or _funding_extreme_signed(data.funding, positive=False)
    return False


def _funding_extreme_signed(value: Any | None, *, positive: bool) -> bool:
    if _is_missing(value):
        return False
    if isinstance(value, Mapping):
        rate = value.get("funding_rate", value.get("rate"))
    else:
        rate = value
    try:
        decimal = _decimal_from(rate, "funding")
    except ValueError:
        return False
    if positive:
        return decimal >= Decimal("0.001")
    return decimal <= Decimal("-0.001")


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
        raise ValueError(f"Malformed liquidity-grab data at {path}: invalid decimal {value!r}.") from exc
    if not decimal.is_finite():
        raise ValueError(f"Malformed liquidity-grab data at {path}: invalid decimal {value!r}.")
    return decimal


def _is_missing(value: Any) -> bool:
    return value is None or value == "" or value == NA


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


__all__ = [
    "FairValueGapZone",
    "FibAlignmentResult",
    "LiquidityGrabEngine",
    "LiquidityGrabInput",
    "LiquidityGrabMode",
    "LiquidityGrabResult",
    "LiquidityGrabSetup",
    "LiquiditySweepSignal",
    "MomentumConfirmation",
    "OrderBlockZone",
    "RotationContext",
    "StrategyFormattedOutput",
    "StrategyGateResult",
    "StructureShiftSignal",
    "TrustMeterResult",
    "analyze_liquidity_grab_pullback",
    "calculate_atr",
    "calculate_fib_alignment",
    "confirm_momentum",
    "detect_fair_value_gap",
    "detect_liquidity_sweep",
    "detect_order_block",
    "detect_structure_shift",
]
