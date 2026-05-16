from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_CEILING
from enum import Enum
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.data.dtos import NA, MaybeDecimal, MaybeInt
from app.strategies.liquidity_grab_pullback import (
    LiquidityGrabEngine,
    LiquidityGrabMode,
    LiquidityGrabResult,
    LiquidityGrabSetup,
)

OUTPUT_QUANT = Decimal("0.00000001")
PERCENT_QUANT = Decimal("0.01")
DEFAULT_REPLAY_STRATEGY = "liquidity_grab_pullback"
DEFAULT_DATA_NOTES = (
    "derivatives: N/A",
    "funding: N/A",
    "open_interest: N/A",
    "cvd: N/A",
    "liquidation_data: N/A",
)


class ReplayOutcome(str, Enum):
    TP1_HIT = "tp1_hit"
    TP2_HIT = "tp2_hit"
    TP3_HIT = "tp3_hit"
    STOPPED = "stopped"
    EXPIRED = "expired"
    INVALIDATED = "invalidated"
    NOT_FILLED = "not_filled"


class ReplayDirection(str, Enum):
    LONG = "long"
    SHORT = "short"


class ReplayConfig(BaseModel):
    strategy_name: str = DEFAULT_REPLAY_STRATEGY
    modes: tuple[LiquidityGrabMode, ...] = (LiquidityGrabMode.swing,)
    execution_timeframe: str = "15m"
    confirmation_timeframe: str = "5m"
    htf_timeframe: str = "2d"
    bias_timeframe: str = "12h"
    replay_candles: int = 1000
    same_candle_policy: Literal["conservative", "optimistic"] = "conservative"
    max_hold_candles: int | None = None
    max_fill_candles: int | None = None
    aggressive_toggle: bool = False

    model_config = ConfigDict(frozen=True)

    @field_validator("strategy_name")
    @classmethod
    def _strategy_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized != DEFAULT_REPLAY_STRATEGY:
            raise ValueError(f"unsupported replay strategy: {value!r}")
        return normalized

    @field_validator("modes", mode="before")
    @classmethod
    def _normalize_modes(cls, value: Any) -> Any:
        if value is None:
            return (LiquidityGrabMode.swing,)
        if isinstance(value, str):
            return (value,)
        if isinstance(value, Sequence):
            return tuple(value)
        return value

    @field_validator("execution_timeframe", "confirmation_timeframe", "htf_timeframe", "bias_timeframe")
    @classmethod
    def _timeframe_not_blank(cls, value: str) -> str:
        normalized = value.strip().lower()
        if not normalized:
            raise ValueError("timeframe must not be blank")
        return normalized

    @field_validator("replay_candles")
    @classmethod
    def _replay_candles_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("replay_candles must be at least 1")
        return value

    @field_validator("max_hold_candles", "max_fill_candles")
    @classmethod
    def _optional_positive(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("replay limits must be at least 1")
        return value

    @model_validator(mode="after")
    def _at_least_one_mode(self) -> ReplayConfig:
        if not self.modes:
            raise ValueError("modes must include at least one strategy mode")
        return self


class ReplaySetupCandidate(BaseModel):
    symbol: str
    mode: LiquidityGrabMode
    direction: ReplayDirection
    detected_at_index: int
    detected_at_timestamp: MaybeInt = NA
    entry: Decimal
    entry_low: Decimal
    entry_high: Decimal
    stop: Decimal
    tp1: Decimal
    tp2: Decimal
    tp3: MaybeDecimal = NA
    rr_to_tp2: MaybeDecimal = NA
    sweep_candle_index: MaybeInt = NA
    bos_choch_candle_index: MaybeInt = NA
    pullback_calculation_timeframe: str = NA
    selected_zone_type: str = NA
    trust_grade: str = NA
    trust_percentage: int = 0
    invalidation: str = NA
    risk_warning: str = NA

    model_config = ConfigDict(frozen=True)


class ReplayTradeResult(BaseModel):
    symbol: str
    mode: LiquidityGrabMode
    direction: ReplayDirection
    candidate: ReplaySetupCandidate
    outcome: ReplayOutcome
    filled: bool = False
    fill_index: MaybeInt = NA
    fill_timestamp: MaybeInt = NA
    exit_index: MaybeInt = NA
    exit_timestamp: MaybeInt = NA
    exit_price: MaybeDecimal = NA
    highest_tp_hit: int = 0
    r_multiple: Decimal = Decimal("0")
    candles_held: int = 0
    failure_reason: str = NA

    model_config = ConfigDict(frozen=True)


class ReplayStats(BaseModel):
    total_setups: int = 0
    filled_trades: int = 0
    win_rate: MaybeDecimal = NA
    tp1_rate: MaybeDecimal = NA
    tp2_rate: MaybeDecimal = NA
    average_r: MaybeDecimal = NA
    median_r: MaybeDecimal = NA
    max_loss_streak: int = 0
    max_win_streak: int = 0
    expectancy_r: MaybeDecimal = NA
    profit_factor: MaybeDecimal = NA
    average_time_in_trade: MaybeDecimal = NA
    rejected_setup_count: int = 0
    near_miss_count: int = 0

    model_config = ConfigDict(frozen=True)


class ReplaySymbolResult(BaseModel):
    symbol: str
    historical_candles: int
    trades: tuple[ReplayTradeResult, ...] = ()
    stats: ReplayStats = Field(default_factory=ReplayStats)
    replay_edge: str = NA
    sample_size: Literal["low_sample_size", "medium_sample_size", "usable_sample_size"] = "low_sample_size"
    sample_size_warning: str = "low_sample_size"
    main_failure_reason: str = NA
    quality_note: str = NA
    data_notes: tuple[str, ...] = DEFAULT_DATA_NOTES

    model_config = ConfigDict(frozen=True)


class ReplaySummary(BaseModel):
    strategy: str = "Liquidity Grab Pullback"
    symbols_tested: int = 0
    historical_candles: int = 0
    stats: ReplayStats = Field(default_factory=ReplayStats)
    replay_edge: str = NA
    sample_size: Literal["low_sample_size", "medium_sample_size", "usable_sample_size"] = "low_sample_size"
    sample_size_warning: str = "low_sample_size"
    symbols: tuple[ReplaySymbolResult, ...] = ()
    data_notes: tuple[str, ...] = DEFAULT_DATA_NOTES
    safety_note: str = "Diagnostic replay only. No orders, private exchange calls, transfers, or live alerts are used."

    model_config = ConfigDict(frozen=True)


@dataclass(frozen=True)
class _ReplayCandle:
    index: int
    timestamp: MaybeInt
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: MaybeDecimal


class StrategyReplayEngine:
    """Deterministic historical replay for existing strategy setup rules."""

    def __init__(self, *, strategy_engine: LiquidityGrabEngine | None = None) -> None:
        self.strategy_engine = strategy_engine or LiquidityGrabEngine()

    def run(
        self,
        candles_by_symbol: Mapping[str, Mapping[str, Sequence[Any]]],
        config: ReplayConfig | Mapping[str, Any] | None = None,
        *,
        timeframe_context_by_symbol: Mapping[str, Mapping[str, Any]] | None = None,
        data_notes_by_symbol: Mapping[str, Sequence[str]] | None = None,
    ) -> ReplaySummary:
        replay_config = config if isinstance(config, ReplayConfig) else ReplayConfig.model_validate(config or {})
        symbol_results = []
        for symbol, timeframe_candles in candles_by_symbol.items():
            symbol_results.append(
                self.replay_symbol(
                    symbol,
                    timeframe_candles,
                    replay_config,
                    timeframe_context=(timeframe_context_by_symbol or {}).get(symbol, {}),
                    extra_data_notes=(data_notes_by_symbol or {}).get(symbol, ()),
                )
            )

        all_trades = tuple(trade for result in symbol_results for trade in result.trades)
        stats = _stats_for_trades(
            all_trades,
            rejected_setup_count=sum(result.stats.rejected_setup_count for result in symbol_results),
            near_miss_count=sum(result.stats.near_miss_count for result in symbol_results),
        )
        data_notes = _unique_strings(
            (*DEFAULT_DATA_NOTES, *(note for result in symbol_results for note in result.data_notes))
        )
        sample_size = _sample_size(stats.filled_trades)
        edge = _edge_classification(stats)
        return ReplaySummary(
            symbols_tested=len(symbol_results),
            historical_candles=sum(result.historical_candles for result in symbol_results),
            stats=stats,
            replay_edge=edge,
            sample_size=sample_size,
            sample_size_warning=sample_size,
            symbols=tuple(symbol_results),
            data_notes=data_notes,
        )

    def replay_symbol(
        self,
        symbol: str,
        candles_by_timeframe: Mapping[str, Sequence[Any]],
        config: ReplayConfig | Mapping[str, Any] | None = None,
        *,
        timeframe_context: Mapping[str, Any] | None = None,
        extra_data_notes: Sequence[str] = (),
    ) -> ReplaySymbolResult:
        replay_config = config if isinstance(config, ReplayConfig) else ReplayConfig.model_validate(config or {})
        execution_timeframe = replay_config.execution_timeframe
        raw_execution = tuple(candles_by_timeframe.get(execution_timeframe, ()))
        raw_execution = raw_execution[-replay_config.replay_candles :]
        execution_candles = _normalize_candles(raw_execution)
        if not execution_candles:
            stats = ReplayStats()
            return ReplaySymbolResult(
                symbol=symbol,
                historical_candles=0,
                stats=stats,
                replay_edge=NA,
                sample_size=_sample_size(0),
                sample_size_warning=_sample_size(0),
                main_failure_reason="execution candles N/A",
                quality_note="Replay unavailable because execution candles are N/A.",
                data_notes=_unique_strings((*DEFAULT_DATA_NOTES, *extra_data_notes, f"candles_{execution_timeframe}: N/A")),
            )

        trades: list[ReplayTradeResult] = []
        seen_candidates: set[str] = set()
        rejected_keys: set[str] = set()
        near_miss_keys: set[str] = set()
        timeframe_context = timeframe_context or {}

        for end_index, current_candle in enumerate(execution_candles):
            execution_prefix = raw_execution[: end_index + 1]
            candles_prefix = _prefix_by_timeframe(
                candles_by_timeframe,
                execution_timeframe=execution_timeframe,
                execution_prefix_count=end_index + 1,
                execution_total_count=len(raw_execution),
                current_timestamp=current_candle.timestamp,
                execution_prefix=execution_prefix,
            )
            base_input = _strategy_input(
                symbol=symbol,
                candles_by_timeframe=candles_prefix,
                config=replay_config,
                current_price=current_candle.close,
                timeframe_context=timeframe_context,
            )

            for mode in replay_config.modes:
                result = self.strategy_engine.analyze({**base_input, "mode": mode})
                setup = _setup_for_mode(result, mode)
                if _is_replay_near_miss(setup):
                    near_miss_keys.add(_rejection_key(mode, setup, end_index))
                if _is_replay_rejection(setup):
                    rejected_keys.add(_rejection_key(mode, setup, end_index))
                if not _is_valid_replay_setup(setup):
                    continue

                candidate = _candidate_from_setup(
                    symbol=symbol,
                    mode=mode,
                    setup=setup,
                    detected_at_index=end_index,
                    detected_at_timestamp=current_candle.timestamp,
                )
                key = _candidate_key(candidate)
                if key in seen_candidates:
                    continue
                seen_candidates.add(key)
                trades.append(_simulate_trade(candidate, execution_candles, replay_config))

        stats = _stats_for_trades(
            tuple(trades),
            rejected_setup_count=len(rejected_keys),
            near_miss_count=len(near_miss_keys),
        )
        edge = _edge_classification(stats)
        sample_size = _sample_size(stats.filled_trades)
        return ReplaySymbolResult(
            symbol=symbol,
            historical_candles=len(execution_candles),
            trades=tuple(trades),
            stats=stats,
            replay_edge=edge,
            sample_size=sample_size,
            sample_size_warning=sample_size,
            main_failure_reason=_main_failure_reason(tuple(trades)),
            quality_note=_quality_note(edge, stats),
            data_notes=_unique_strings((*DEFAULT_DATA_NOTES, *extra_data_notes)),
        )


def replay_liquidity_grab_pullback(
    candles_by_symbol: Mapping[str, Mapping[str, Sequence[Any]]],
    config: ReplayConfig | Mapping[str, Any] | None = None,
    *,
    timeframe_context_by_symbol: Mapping[str, Mapping[str, Any]] | None = None,
    data_notes_by_symbol: Mapping[str, Sequence[str]] | None = None,
) -> ReplaySummary:
    return StrategyReplayEngine().run(
        candles_by_symbol,
        config,
        timeframe_context_by_symbol=timeframe_context_by_symbol,
        data_notes_by_symbol=data_notes_by_symbol,
    )


def format_replay_summary(summary: ReplaySummary) -> str:
    lines = [
        "Candle Craft Replay",
        f"Strategy: {summary.strategy}",
        f"Symbols tested: {summary.symbols_tested}",
        f"Historical candles: {summary.historical_candles}",
        f"Setups found: {summary.stats.total_setups}",
        f"Filled trades: {summary.stats.filled_trades}",
        f"Win rate: {_display_percent(summary.stats.win_rate)}",
        f"TP1 rate: {_display_percent(summary.stats.tp1_rate)}",
        f"TP2 rate: {_display_percent(summary.stats.tp2_rate)}",
        f"Expectancy: {_display_r(summary.stats.expectancy_r)}",
        f"Profit factor: {_display(summary.stats.profit_factor)}",
        f"Max loss streak: {summary.stats.max_loss_streak}",
        f"Replay edge: {summary.replay_edge}",
        f"Sample size warning: {summary.sample_size_warning}",
        f"Data notes: {_sequence_text(summary.data_notes)}",
    ]
    for symbol in summary.symbols:
        best, worst = _best_worst(symbol.trades)
        lines.extend(
            (
                "",
                symbol.symbol,
                f"- Setups: {symbol.stats.total_setups}",
                f"- Filled: {symbol.stats.filled_trades}",
                f"- Win rate: {_display_percent(symbol.stats.win_rate)}",
                f"- Avg R: {_display_r(symbol.stats.average_r)}",
                f"- Best trade: {_display_r(best)}",
                f"- Worst trade: {_display_r(worst)}",
                f"- Replay edge: {symbol.replay_edge}",
                f"- Historical expectancy R: {_display(symbol.stats.expectancy_r)}",
                f"- Recent win rate: {_display_percent(symbol.stats.win_rate)}",
                f"- Sample size warning: {symbol.sample_size_warning}",
                f"- Main failure reason: {symbol.main_failure_reason}",
                f"- Quality note: {symbol.quality_note}",
            )
        )
    return "\n".join(lines)


def _strategy_input(
    *,
    symbol: str,
    candles_by_timeframe: Mapping[str, Sequence[Any]],
    config: ReplayConfig,
    current_price: Decimal,
    timeframe_context: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "htf_timeframe": config.htf_timeframe,
        "bias_timeframe": config.bias_timeframe,
        "execution_timeframe": config.execution_timeframe,
        "confirmation_timeframe": config.confirmation_timeframe,
        "candles_2d": candles_by_timeframe.get("2d"),
        "candles_12h": candles_by_timeframe.get("12h"),
        "candles_6h": candles_by_timeframe.get("6h"),
        "candles_4h": candles_by_timeframe.get("4h"),
        "candles_1h": candles_by_timeframe.get("1h"),
        "candles_15m": candles_by_timeframe.get("15m"),
        "candles_5m": candles_by_timeframe.get("5m"),
        "current_price": current_price,
        "funding": None,
        "open_interest": None,
        "cvd": None,
        "liquidation_data": None,
        "aggressive_toggle": config.aggressive_toggle,
        "htf_2d_context_source": timeframe_context.get("htf_2d_context_source", NA),
    }


def _prefix_by_timeframe(
    candles_by_timeframe: Mapping[str, Sequence[Any]],
    *,
    execution_timeframe: str,
    execution_prefix_count: int,
    execution_total_count: int,
    current_timestamp: MaybeInt,
    execution_prefix: Sequence[Any],
) -> dict[str, tuple[Any, ...]]:
    output: dict[str, tuple[Any, ...]] = {}
    for timeframe, candles in candles_by_timeframe.items():
        raw = tuple(candles)
        if timeframe == execution_timeframe:
            output[timeframe] = tuple(execution_prefix)
            continue
        output[timeframe] = _slice_until(raw, current_timestamp, execution_prefix_count, execution_total_count)
    if execution_timeframe not in output:
        output[execution_timeframe] = tuple(execution_prefix)
    return output


def _slice_until(
    candles: Sequence[Any],
    current_timestamp: MaybeInt,
    execution_prefix_count: int,
    execution_total_count: int,
) -> tuple[Any, ...]:
    if not candles:
        return ()
    normalized = _normalize_candles(candles)
    if current_timestamp != NA and any(candle.timestamp != NA for candle in normalized):
        sliced = tuple(raw for raw, candle in zip(candles, normalized) if candle.timestamp != NA and int(candle.timestamp) <= int(current_timestamp))
        return sliced
    ratio = Decimal(execution_prefix_count) / Decimal(max(execution_total_count, 1))
    end = int((Decimal(len(candles)) * ratio).to_integral_value(rounding=ROUND_CEILING))
    return tuple(candles[: max(1, min(end, len(candles)))])


def _setup_for_mode(result: LiquidityGrabResult, mode: LiquidityGrabMode | str) -> LiquidityGrabSetup:
    selected = LiquidityGrabMode(mode)
    if selected == LiquidityGrabMode.challenge:
        return result.challenge
    if selected == LiquidityGrabMode.scalp:
        return result.scalp
    return result.swing


def _is_valid_replay_setup(setup: LiquidityGrabSetup) -> bool:
    return setup.is_valid and setup.trust_meter.grade in ("A", "B")


def _candidate_from_setup(
    *,
    symbol: str,
    mode: LiquidityGrabMode,
    setup: LiquidityGrabSetup,
    detected_at_index: int,
    detected_at_timestamp: MaybeInt,
) -> ReplaySetupCandidate:
    required = {
        "entry": setup.entry,
        "entry_low": setup.entry_low,
        "entry_high": setup.entry_high,
        "stop": setup.stop,
        "tp1": setup.tp1,
        "tp2": setup.tp2,
    }
    missing = [name for name, value in required.items() if value == NA]
    if missing:
        raise ValueError(f"valid replay setup missing required levels: {', '.join(missing)}")
    if setup.bias not in ("long", "short"):
        raise ValueError("valid replay setup has N/A direction")
    return ReplaySetupCandidate(
        symbol=symbol,
        mode=mode,
        direction=ReplayDirection.LONG if setup.bias == "long" else ReplayDirection.SHORT,
        detected_at_index=detected_at_index,
        detected_at_timestamp=detected_at_timestamp,
        entry=_quantize(_decimal_from(setup.entry, "entry")),
        entry_low=_quantize(_decimal_from(setup.entry_low, "entry_low")),
        entry_high=_quantize(_decimal_from(setup.entry_high, "entry_high")),
        stop=_quantize(_decimal_from(setup.stop, "stop")),
        tp1=_quantize(_decimal_from(setup.tp1, "tp1")),
        tp2=_quantize(_decimal_from(setup.tp2, "tp2")),
        tp3=NA if setup.tp3 == NA else _quantize(_decimal_from(setup.tp3, "tp3")),
        rr_to_tp2=setup.rr_to_tp2,
        sweep_candle_index=setup.pullback_sweep_candle_index,
        bos_choch_candle_index=setup.pullback_bos_choch_candle_index,
        pullback_calculation_timeframe=setup.pullback_calculation_timeframe,
        selected_zone_type=setup.selected_zone_type,
        trust_grade=setup.trust_meter.grade,
        trust_percentage=setup.trust_meter.percentage,
        invalidation=setup.invalidation,
        risk_warning=setup.risk_warning,
    )


def _simulate_trade(
    candidate: ReplaySetupCandidate,
    candles: Sequence[_ReplayCandle],
    config: ReplayConfig,
) -> ReplayTradeResult:
    risk = _risk(candidate)
    max_fill_index = min(
        len(candles) - 1,
        candidate.detected_at_index + _fill_window(candidate, config),
    )
    max_hold_candles = _max_hold_candles(candidate.mode, config.execution_timeframe, config)
    highest_tp = 0
    fill_index: MaybeInt = NA
    fill_timestamp: MaybeInt = NA

    for index in range(candidate.detected_at_index + 1, max_fill_index + 1):
        candle = candles[index]
        entry_touched = _price_touched(candle, candidate.entry)
        stop_touched = _price_touched(candle, candidate.stop)
        if not entry_touched and stop_touched:
            return _result(
                candidate,
                ReplayOutcome.INVALIDATED,
                r_multiple=Decimal("0"),
                exit_candle=candle,
                exit_price=candidate.stop,
                failure_reason="Invalidation touched before the limit entry filled.",
            )
        if not entry_touched:
            continue

        fill_index = index
        fill_timestamp = candle.timestamp
        same_candle = _evaluate_exit_candle(candidate, candle, highest_tp, config.same_candle_policy)
        if same_candle is not None:
            outcome, highest_tp, exit_price, r_multiple = same_candle
            return _result(
                candidate,
                outcome,
                filled=True,
                fill_index=fill_index,
                fill_timestamp=fill_timestamp,
                exit_candle=candle,
                exit_price=exit_price,
                highest_tp_hit=highest_tp,
                r_multiple=r_multiple,
                candles_held=0,
                failure_reason=_failure_reason_for(outcome),
            )
        break
    else:
        return _result(
            candidate,
            ReplayOutcome.NOT_FILLED,
            r_multiple=Decimal("0"),
            failure_reason=f"Limit entry did not fill within {_fill_window(candidate, config)} candle(s).",
        )

    if fill_index == NA:
        return _result(
            candidate,
            ReplayOutcome.NOT_FILLED,
            r_multiple=Decimal("0"),
            failure_reason=f"Limit entry did not fill within {_fill_window(candidate, config)} candle(s).",
        )

    last_index = min(len(candles) - 1, int(fill_index) + max_hold_candles)
    for index in range(int(fill_index) + 1, last_index + 1):
        candle = candles[index]
        exit_result = _evaluate_exit_candle(candidate, candle, highest_tp, config.same_candle_policy)
        if exit_result is None:
            continue
        outcome, highest_tp, exit_price, r_multiple = exit_result
        if outcome == ReplayOutcome.STOPPED and highest_tp > 0:
            outcome = _tp_outcome(highest_tp)
            exit_price = _target_for(candidate, highest_tp)
            r_multiple = _r_for_target(candidate, highest_tp, risk)
        return _result(
            candidate,
            outcome,
            filled=True,
            fill_index=fill_index,
            fill_timestamp=fill_timestamp,
            exit_candle=candle,
            exit_price=exit_price,
            highest_tp_hit=highest_tp,
            r_multiple=r_multiple,
            candles_held=index - int(fill_index),
            failure_reason=_failure_reason_for(outcome),
        )

    expiry_candle = candles[last_index]
    r_multiple = _mark_to_market_r(candidate, expiry_candle.close, risk)
    return _result(
        candidate,
        ReplayOutcome.EXPIRED,
        filled=True,
        fill_index=fill_index,
        fill_timestamp=fill_timestamp,
        exit_candle=expiry_candle,
        exit_price=expiry_candle.close,
        highest_tp_hit=highest_tp,
        r_multiple=r_multiple,
        candles_held=last_index - int(fill_index),
        failure_reason=f"No TP/SL hit within {max_hold_candles} candle(s) after fill.",
    )


def _evaluate_exit_candle(
    candidate: ReplaySetupCandidate,
    candle: _ReplayCandle,
    current_highest_tp: int,
    same_candle_policy: Literal["conservative", "optimistic"],
) -> tuple[ReplayOutcome, int, Decimal, Decimal] | None:
    risk = _risk(candidate)
    stop_touched = _price_touched(candle, candidate.stop)
    highest_tp = max(current_highest_tp, _highest_target_touched(candidate, candle))
    target_touched = highest_tp > current_highest_tp
    if stop_touched and target_touched and same_candle_policy == "conservative":
        if current_highest_tp > 0:
            return (
                _tp_outcome(current_highest_tp),
                current_highest_tp,
                _target_for(candidate, current_highest_tp),
                _r_for_target(candidate, current_highest_tp, risk),
            )
        return (ReplayOutcome.STOPPED, 0, candidate.stop, Decimal("-1"))
    if target_touched:
        return (_tp_outcome(highest_tp), highest_tp, _target_for(candidate, highest_tp), _r_for_target(candidate, highest_tp, risk))
    if stop_touched:
        return (ReplayOutcome.STOPPED, current_highest_tp, candidate.stop, Decimal("-1"))
    return None


def _result(
    candidate: ReplaySetupCandidate,
    outcome: ReplayOutcome,
    *,
    filled: bool = False,
    fill_index: MaybeInt = NA,
    fill_timestamp: MaybeInt = NA,
    exit_candle: _ReplayCandle | None = None,
    exit_price: MaybeDecimal = NA,
    highest_tp_hit: int = 0,
    r_multiple: Decimal = Decimal("0"),
    candles_held: int = 0,
    failure_reason: str = NA,
) -> ReplayTradeResult:
    return ReplayTradeResult(
        symbol=candidate.symbol,
        mode=candidate.mode,
        direction=candidate.direction,
        candidate=candidate,
        outcome=outcome,
        filled=filled,
        fill_index=fill_index,
        fill_timestamp=fill_timestamp,
        exit_index=exit_candle.index if exit_candle is not None else NA,
        exit_timestamp=exit_candle.timestamp if exit_candle is not None else NA,
        exit_price=NA if exit_price == NA else _quantize(_decimal_from(exit_price, "exit_price")),
        highest_tp_hit=highest_tp_hit,
        r_multiple=_quantize(r_multiple),
        candles_held=candles_held,
        failure_reason=failure_reason,
    )


def _stats_for_trades(
    trades: Sequence[ReplayTradeResult],
    *,
    rejected_setup_count: int,
    near_miss_count: int,
) -> ReplayStats:
    filled = tuple(trade for trade in trades if trade.filled)
    r_values = tuple(trade.r_multiple for trade in filled)
    wins = tuple(value for value in r_values if value > 0)
    losses = tuple(value for value in r_values if value < 0)
    tp1_hits = sum(1 for trade in filled if trade.highest_tp_hit >= 1)
    tp2_hits = sum(1 for trade in filled if trade.highest_tp_hit >= 2)
    profit_sum = sum(wins, Decimal("0"))
    loss_sum = abs(sum(losses, Decimal("0")))
    average_r = _mean(r_values)
    return ReplayStats(
        total_setups=len(trades),
        filled_trades=len(filled),
        win_rate=_rate(len(wins), len(filled)),
        tp1_rate=_rate(tp1_hits, len(filled)),
        tp2_rate=_rate(tp2_hits, len(filled)),
        average_r=average_r,
        median_r=NA if not r_values else _quantize(Decimal(str(median(r_values)))),
        max_loss_streak=_max_streak(r_values, positive=False),
        max_win_streak=_max_streak(r_values, positive=True),
        expectancy_r=average_r,
        profit_factor=NA if loss_sum == 0 else _quantize(profit_sum / loss_sum),
        average_time_in_trade=_mean(tuple(Decimal(trade.candles_held) for trade in filled)),
        rejected_setup_count=rejected_setup_count,
        near_miss_count=near_miss_count,
    )


def _is_replay_rejection(setup: LiquidityGrabSetup) -> bool:
    if setup.is_valid:
        return False
    if setup.sweep.is_present:
        return True
    if setup.confirmation_structure_shift_status == "passed":
        return True
    return bool(setup.gates_passed)


def _is_replay_near_miss(setup: LiquidityGrabSetup) -> bool:
    if setup.is_valid:
        return False
    return setup.confirmation_structure_shift_status == "passed" or "bos_choch" in setup.gates_passed


def _rejection_key(mode: LiquidityGrabMode, setup: LiquidityGrabSetup, fallback_index: int) -> str:
    return ":".join(
        str(part)
        for part in (
            mode.value,
            setup.first_failed_gate,
            setup.sweep.candle_index,
            setup.structure_shift.candle_index,
            setup.entry,
            fallback_index if setup.sweep.candle_index == NA and setup.structure_shift.candle_index == NA else "",
        )
    )


def _candidate_key(candidate: ReplaySetupCandidate) -> str:
    return ":".join(
        str(part)
        for part in (
            candidate.symbol,
            candidate.mode.value,
            candidate.direction.value,
            candidate.pullback_calculation_timeframe,
            candidate.sweep_candle_index,
            candidate.bos_choch_candle_index,
            candidate.entry,
            candidate.stop,
            candidate.tp2,
        )
    )


def _fill_window(candidate: ReplaySetupCandidate, config: ReplayConfig) -> int:
    if config.max_fill_candles is not None:
        return config.max_fill_candles
    if candidate.mode in (LiquidityGrabMode.challenge, LiquidityGrabMode.scalp):
        return 12 if config.confirmation_timeframe == "5m" else 6
    return _max_hold_candles(candidate.mode, config.execution_timeframe, config)


def _max_hold_candles(mode: LiquidityGrabMode, execution_timeframe: str, config: ReplayConfig) -> int:
    if config.max_hold_candles is not None:
        return config.max_hold_candles
    if mode in (LiquidityGrabMode.challenge, LiquidityGrabMode.scalp):
        return 48
    if execution_timeframe in ("1h", "4h"):
        return 80
    return 80


def _highest_target_touched(candidate: ReplaySetupCandidate, candle: _ReplayCandle) -> int:
    targets = [(1, candidate.tp1), (2, candidate.tp2)]
    if candidate.tp3 != NA:
        targets.append((3, _decimal_from(candidate.tp3, "tp3")))
    highest = 0
    for number, price in targets:
        if _price_touched(candle, price):
            highest = number
    return highest


def _target_for(candidate: ReplaySetupCandidate, number: int) -> Decimal:
    if number == 1:
        return candidate.tp1
    if number == 2:
        return candidate.tp2
    if candidate.tp3 != NA:
        return _decimal_from(candidate.tp3, "tp3")
    return candidate.tp2


def _tp_outcome(number: int) -> ReplayOutcome:
    if number >= 3:
        return ReplayOutcome.TP3_HIT
    if number == 2:
        return ReplayOutcome.TP2_HIT
    return ReplayOutcome.TP1_HIT


def _r_for_target(candidate: ReplaySetupCandidate, number: int, risk: Decimal) -> Decimal:
    return _mark_to_market_r(candidate, _target_for(candidate, number), risk)


def _mark_to_market_r(candidate: ReplaySetupCandidate, price: Decimal, risk: Decimal) -> Decimal:
    if risk <= 0:
        return Decimal("0")
    if candidate.direction == ReplayDirection.LONG:
        return _quantize((price - candidate.entry) / risk)
    return _quantize((candidate.entry - price) / risk)


def _risk(candidate: ReplaySetupCandidate) -> Decimal:
    return abs(candidate.entry - candidate.stop)


def _price_touched(candle: _ReplayCandle, price: Decimal) -> bool:
    return candle.low <= price <= candle.high


def _sample_size(filled_trades: int) -> Literal["low_sample_size", "medium_sample_size", "usable_sample_size"]:
    if filled_trades < 10:
        return "low_sample_size"
    if filled_trades < 30:
        return "medium_sample_size"
    return "usable_sample_size"


def _edge_classification(stats: ReplayStats) -> str:
    if stats.filled_trades == 0 or stats.expectancy_r == NA or stats.win_rate == NA:
        return NA
    sample = _sample_size(stats.filled_trades)
    expectancy = _decimal_from(stats.expectancy_r, "expectancy_r")
    win_rate = _decimal_from(stats.win_rate, "win_rate")
    if expectancy > Decimal("0.35") and win_rate > Decimal("45") and sample in ("medium_sample_size", "usable_sample_size"):
        return "strong"
    if expectancy <= 0 and sample in ("medium_sample_size", "usable_sample_size"):
        return "weak"
    return "mixed"


def _quality_note(edge: str, stats: ReplayStats) -> str:
    if edge == "strong":
        return "Replay shows positive expectancy with enough filled trades to treat the sample as useful."
    if edge == "weak":
        return "Replay expectancy is non-positive on a medium or usable sample."
    if edge == "mixed":
        if stats.filled_trades < 10:
            return "Replay sample is too small for confidence."
        return "Replay edge is positive but not strong enough for a strong classification."
    return "Replay edge is N/A because no filled trades were available."


def _main_failure_reason(trades: Sequence[ReplayTradeResult]) -> str:
    failures = [
        trade.failure_reason
        for trade in trades
        if trade.failure_reason != NA and trade.outcome not in (ReplayOutcome.TP1_HIT, ReplayOutcome.TP2_HIT, ReplayOutcome.TP3_HIT)
    ]
    if not failures:
        return NA
    return Counter(failures).most_common(1)[0][0]


def _best_worst(trades: Sequence[ReplayTradeResult]) -> tuple[MaybeDecimal, MaybeDecimal]:
    filled = [trade.r_multiple for trade in trades if trade.filled]
    if not filled:
        return NA, NA
    return max(filled), min(filled)


def _failure_reason_for(outcome: ReplayOutcome) -> str:
    if outcome == ReplayOutcome.STOPPED:
        return "Stop loss touched before a higher target resolved."
    if outcome == ReplayOutcome.EXPIRED:
        return "Trade expired before TP/SL resolution."
    if outcome == ReplayOutcome.INVALIDATED:
        return "Setup invalidated before fill."
    if outcome == ReplayOutcome.NOT_FILLED:
        return "Limit entry did not fill."
    return NA


def _rate(numerator: int, denominator: int) -> MaybeDecimal:
    if denominator == 0:
        return NA
    return ((Decimal(numerator) / Decimal(denominator)) * Decimal("100")).quantize(PERCENT_QUANT)


def _mean(values: Sequence[Decimal]) -> MaybeDecimal:
    if not values:
        return NA
    return _quantize(sum(values, Decimal("0")) / Decimal(len(values)))


def _max_streak(values: Sequence[Decimal], *, positive: bool) -> int:
    best = 0
    current = 0
    for value in values:
        matches = value > 0 if positive else value < 0
        if matches:
            current += 1
            best = max(best, current)
        else:
            current = 0
    return best


def _normalize_candles(candles: Sequence[Any]) -> tuple[_ReplayCandle, ...]:
    output: list[_ReplayCandle] = []
    for index, candle in enumerate(candles):
        output.append(
            _ReplayCandle(
                index=index,
                timestamp=_normalize_timestamp(_field(candle, "timestamp")),
                open=_decimal_from(_field(candle, "open"), "open"),
                high=_decimal_from(_field(candle, "high"), "high"),
                low=_decimal_from(_field(candle, "low"), "low"),
                close=_decimal_from(_field(candle, "close"), "close"),
                volume=_normalize_optional_decimal(_field(candle, "volume")),
            )
        )
    return tuple(output)


def _field(source: Any, name: str) -> Any:
    if isinstance(source, Mapping):
        return source.get(name)
    return getattr(source, name, None)


def _normalize_timestamp(value: Any) -> MaybeInt:
    if value is None or value == "" or value == NA:
        return NA
    try:
        return int(value)
    except (TypeError, ValueError):
        return NA


def _normalize_optional_decimal(value: Any) -> MaybeDecimal:
    if value is None or value == "" or value == NA:
        return NA
    return _quantize(_decimal_from(value, "optional_decimal"))


def _decimal_from(value: Any, path: str) -> Decimal:
    try:
        decimal = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"Malformed replay data at {path}: invalid decimal {value!r}.") from exc
    if not decimal.is_finite():
        raise ValueError(f"Malformed replay data at {path}: invalid decimal {value!r}.")
    return decimal


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(OUTPUT_QUANT)


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        cleaned = str(value).strip()
        if cleaned and cleaned not in output:
            output.append(cleaned)
    return tuple(output)


def _display(value: object) -> str:
    if value == NA or value is None:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


def _display_percent(value: object) -> str:
    text = _display(value)
    return text if text == NA else f"{text}%"


def _display_r(value: object) -> str:
    text = _display(value)
    return text if text == NA else f"{text} R"


def _sequence_text(values: Sequence[str]) -> str:
    return ", ".join(values) if values else NA


__all__ = [
    "ReplayConfig",
    "ReplayDirection",
    "ReplayOutcome",
    "ReplaySetupCandidate",
    "ReplayStats",
    "ReplaySummary",
    "ReplaySymbolResult",
    "ReplayTradeResult",
    "StrategyReplayEngine",
    "format_replay_summary",
    "replay_liquidity_grab_pullback",
]
