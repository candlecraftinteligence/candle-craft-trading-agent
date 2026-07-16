from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from enum import Enum
from statistics import median
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.analytics.edge_analytics import (
    DEFAULT_EDGE_MIN_SAMPLE,
    EdgeAnalyticsRecord,
    EdgeAnalyticsReport,
    EdgeConditionKey,
    ExpectancyMetrics,
    build_edge_analytics_report,
    condition_key_from_diagnostics,
)
from app.data.candle_integrity import closed_candles_as_of, validate_candle_sequence
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
    MISSED_ENTRY = "missed_entry"
    NOT_FILLED = "missed_entry"


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
    replay_candles: int = 300
    same_candle_policy: Literal["conservative", "optimistic"] = "conservative"
    max_hold_candles: int | None = None
    max_fill_candles: int | None = None
    max_setups: int | None = None
    edge_min_sample: int = DEFAULT_EDGE_MIN_SAMPLE
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

    @field_validator("max_hold_candles", "max_fill_candles", "max_setups")
    @classmethod
    def _optional_positive(cls, value: int | None) -> int | None:
        if value is not None and value < 1:
            raise ValueError("replay limits must be at least 1")
        return value

    @field_validator("edge_min_sample")
    @classmethod
    def _edge_min_sample_positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("edge_min_sample must be at least 1")
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
    condition_key: EdgeConditionKey = Field(default_factory=EdgeConditionKey)

    model_config = ConfigDict(frozen=True)


class ReplayTradeResult(BaseModel):
    symbol: str
    mode: LiquidityGrabMode
    direction: ReplayDirection
    candidate: ReplaySetupCandidate
    outcome: ReplayOutcome
    entry: MaybeDecimal = NA
    stop: MaybeDecimal = NA
    tp1: MaybeDecimal = NA
    tp2: MaybeDecimal = NA
    tp3: MaybeDecimal = NA
    filled: bool = False
    entry_filled: bool = False
    sl_hit: bool = False
    tp1_hit: bool = False
    tp2_hit: bool = False
    tp3_hit: bool = False
    fill_index: MaybeInt = NA
    fill_timestamp: MaybeInt = NA
    exit_index: MaybeInt = NA
    exit_timestamp: MaybeInt = NA
    exit_price: MaybeDecimal = NA
    highest_tp_hit: int = 0
    max_favorable_excursion: Decimal = Decimal("0")
    max_adverse_excursion: Decimal = Decimal("0")
    time_to_entry: MaybeInt = NA
    time_to_tp1: MaybeInt = NA
    time_to_final_outcome: MaybeInt = NA
    time_unit: Literal["candles"] = "candles"
    r_multiple: Decimal = Decimal("0")
    final_r_multiple: Decimal = Decimal("0")
    candles_held: int = 0
    failure_reason: str = NA

    model_config = ConfigDict(frozen=True)


class ReplayStats(BaseModel):
    total_setups: int = 0
    filled_trades: int = 0
    missed_entries: int = 0
    win_rate: MaybeDecimal = NA
    tp1_rate: MaybeDecimal = NA
    tp2_rate: MaybeDecimal = NA
    average_r: MaybeDecimal = NA
    median_r: MaybeDecimal = NA
    max_drawdown_r: MaybeDecimal = NA
    best_r: MaybeDecimal = NA
    worst_r: MaybeDecimal = NA
    max_loss_streak: int = 0
    max_win_streak: int = 0
    expectancy_r: MaybeDecimal = NA
    profit_factor: MaybeDecimal = NA
    average_time_in_trade: MaybeDecimal = NA
    average_hold_time: MaybeDecimal = NA
    most_common_rejection_reason: str = NA
    rejected_setup_count: int = 0
    near_miss_count: int = 0

    model_config = ConfigDict(frozen=True)


class ReplaySymbolResult(BaseModel):
    symbol: str
    historical_candles: int
    trades: tuple[ReplayTradeResult, ...] = ()
    stats: ReplayStats = Field(default_factory=ReplayStats)
    per_mode_stats: dict[str, ReplayStats] = Field(default_factory=dict)
    rejection_reasons: tuple[str, ...] = ()
    replay_edge: str = NA
    sample_size: Literal["low_sample_size", "medium_sample_size", "usable_sample_size"] = "low_sample_size"
    sample_size_warning: str = "low_sample_size"
    main_failure_reason: str = NA
    quality_note: str = NA
    edge_analytics: EdgeAnalyticsReport = Field(default_factory=EdgeAnalyticsReport)
    expectancy_metrics: ExpectancyMetrics = Field(default_factory=ExpectancyMetrics)
    confidence_label: str = NA
    data_notes: tuple[str, ...] = DEFAULT_DATA_NOTES

    model_config = ConfigDict(frozen=True)


class ReplaySummary(BaseModel):
    strategy: str = "Liquidity Grab Pullback"
    symbols_tested: int = 0
    historical_candles: int = 0
    stats: ReplayStats = Field(default_factory=ReplayStats)
    per_mode_stats: dict[str, ReplayStats] = Field(default_factory=dict)
    best_performing_symbol: str = NA
    worst_performing_symbol: str = NA
    replay_edge: str = NA
    sample_size: Literal["low_sample_size", "medium_sample_size", "usable_sample_size"] = "low_sample_size"
    sample_size_warning: str = "low_sample_size"
    symbols: tuple[ReplaySymbolResult, ...] = ()
    edge_analytics: EdgeAnalyticsReport = Field(default_factory=EdgeAnalyticsReport)
    expectancy_metrics: ExpectancyMetrics = Field(default_factory=ExpectancyMetrics)
    confidence_label: str = NA
    data_notes: tuple[str, ...] = DEFAULT_DATA_NOTES
    safety_note: str = (
        "Completed-candle diagnostic replay only. No orders, private exchange calls, Telegram sends, "
        "withdrawals, transfers, or live alerts are used."
    )

    model_config = ConfigDict(frozen=True)


@dataclass(frozen=True)
class _ReplayCandle:
    index: int
    timestamp: MaybeInt
    close_timestamp: int
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
        setups_used = 0
        for symbol, timeframe_candles in candles_by_symbol.items():
            symbol_config = replay_config
            if replay_config.max_setups is not None:
                remaining_setups = replay_config.max_setups - setups_used
                if remaining_setups <= 0:
                    symbol_config = replay_config.model_copy(update={"max_setups": 1})
                    symbol_results.append(
                        _empty_symbol_result(
                            symbol,
                            timeframe_candles,
                            symbol_config,
                            extra_data_notes=("backtest_max_setups reached",),
                        )
                    )
                    continue
                symbol_config = replay_config.model_copy(update={"max_setups": remaining_setups})
            symbol_results.append(
                self.replay_symbol(
                    symbol,
                    timeframe_candles,
                    symbol_config,
                    timeframe_context=(timeframe_context_by_symbol or {}).get(symbol, {}),
                    extra_data_notes=(data_notes_by_symbol or {}).get(symbol, ()),
                )
            )
            setups_used += symbol_results[-1].stats.total_setups

        all_trades = tuple(trade for result in symbol_results for trade in result.trades)
        rejection_reasons = tuple(reason for result in symbol_results for reason in result.rejection_reasons)
        stats = _stats_for_trades(
            all_trades,
            rejected_setup_count=sum(result.stats.rejected_setup_count for result in symbol_results),
            near_miss_count=sum(result.stats.near_miss_count for result in symbol_results),
            rejection_reasons=rejection_reasons,
        )
        data_notes = _unique_strings(
            (*DEFAULT_DATA_NOTES, *(note for result in symbol_results for note in result.data_notes))
        )
        sample_size = _sample_size(stats.filled_trades)
        edge = _edge_classification(stats)
        per_mode_stats = _per_mode_stats(all_trades)
        best_symbol, worst_symbol = _best_worst_symbol(symbol_results)
        edge_analytics = _edge_analytics_for_trades(
            all_trades,
            min_sample=replay_config.edge_min_sample,
        )
        return ReplaySummary(
            symbols_tested=len(symbol_results),
            historical_candles=sum(result.historical_candles for result in symbol_results),
            stats=stats,
            per_mode_stats=per_mode_stats,
            best_performing_symbol=best_symbol,
            worst_performing_symbol=worst_symbol,
            replay_edge=edge,
            sample_size=sample_size,
            sample_size_warning=sample_size,
            symbols=tuple(symbol_results),
            edge_analytics=edge_analytics,
            expectancy_metrics=edge_analytics.expectancy_metrics,
            confidence_label=edge_analytics.confidence_label,
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
        for timeframe, candles in candles_by_timeframe.items():
            if candles:
                validate_candle_sequence(
                    candles,
                    timeframe=timeframe,
                    require_continuity=True,
                )
        execution_timeframe = replay_config.execution_timeframe
        raw_execution = tuple(candles_by_timeframe.get(execution_timeframe, ()))
        raw_execution = raw_execution[-replay_config.replay_candles :]
        execution_candles = _normalize_candles(raw_execution, timeframe=execution_timeframe)
        if not execution_candles:
            stats = ReplayStats()
            edge_analytics = _edge_analytics_for_trades((), min_sample=replay_config.edge_min_sample)
            return ReplaySymbolResult(
                symbol=symbol,
                historical_candles=0,
                stats=stats,
                per_mode_stats=_per_mode_stats(()),
                replay_edge=NA,
                sample_size=_sample_size(0),
                sample_size_warning=_sample_size(0),
                main_failure_reason="execution candles N/A",
                quality_note="Replay unavailable because execution candles are N/A.",
                edge_analytics=edge_analytics,
                expectancy_metrics=edge_analytics.expectancy_metrics,
                confidence_label=edge_analytics.confidence_label,
                data_notes=_unique_strings((*DEFAULT_DATA_NOTES, *extra_data_notes, f"candles_{execution_timeframe}: N/A")),
            )

        trades: list[ReplayTradeResult] = []
        seen_candidates: set[str] = set()
        rejected_keys: set[str] = set()
        near_miss_keys: set[str] = set()
        rejection_reasons: list[str] = []
        timeframe_context = timeframe_context or {}

        for end_index, current_candle in enumerate(execution_candles):
            if replay_config.max_setups is not None and len(trades) >= replay_config.max_setups:
                break
            execution_prefix = raw_execution[: end_index + 1]
            candles_prefix = _prefix_by_timeframe(
                candles_by_timeframe,
                execution_timeframe=execution_timeframe,
                decision_timestamp=current_candle.close_timestamp,
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
                    rejection_key = _rejection_key(mode, setup, end_index)
                    if rejection_key not in rejected_keys:
                        rejection_reasons.append(_setup_rejection_reason(setup))
                    rejected_keys.add(rejection_key)
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
            rejection_reasons=tuple(rejection_reasons),
        )
        edge = _edge_classification(stats)
        sample_size = _sample_size(stats.filled_trades)
        edge_analytics = _edge_analytics_for_trades(
            tuple(trades),
            min_sample=replay_config.edge_min_sample,
        )
        return ReplaySymbolResult(
            symbol=symbol,
            historical_candles=len(execution_candles),
            trades=tuple(trades),
            stats=stats,
            per_mode_stats=_per_mode_stats(tuple(trades)),
            rejection_reasons=tuple(rejection_reasons),
            replay_edge=edge,
            sample_size=sample_size,
            sample_size_warning=sample_size,
            main_failure_reason=_main_failure_reason(tuple(trades)),
            quality_note=_quality_note(edge, stats),
            edge_analytics=edge_analytics,
            expectancy_metrics=edge_analytics.expectancy_metrics,
            confidence_label=edge_analytics.confidence_label,
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


def format_replay_summary(
    summary: ReplaySummary,
    *,
    summary_only: bool = False,
    include_setup_diagnostics: bool = False,
) -> str:
    sample_warning = (
        ("Sample size too small for reliable conclusion.",)
        if summary.sample_size == "low_sample_size"
        else ()
    )
    lines = [
        "Candle Craft Replay Backtest",
        f"Strategy: {summary.strategy}",
        "Safety: Completed-candle historical replay only. No orders, private API calls, Telegram sends, transfers, or withdrawals.",
        *sample_warning,
        "",
        "Key statistics",
        f"Symbols tested: {summary.symbols_tested}",
        f"Historical candles: {summary.historical_candles}",
        f"Setups found: {summary.stats.total_setups}",
        f"Filled setups: {summary.stats.filled_trades}",
        f"Missed entries: {summary.stats.missed_entries}",
        f"Win rate to TP1: {_display_percent(summary.stats.tp1_rate)}",
        f"Win rate to TP2: {_display_percent(summary.stats.tp2_rate)}",
        f"Average R: {_display_r(summary.stats.average_r)}",
        f"Median R: {_display_r(summary.stats.median_r)}",
        f"Max drawdown in R: {_display_r(summary.stats.max_drawdown_r)}",
        f"Best setup: {_setup_label(_best_trade(summary))}",
        f"Worst setup: {_setup_label(_worst_trade(summary))}",
        f"Average hold time: {_display_candles(summary.stats.average_hold_time)}",
        f"Most common rejection reason: {summary.stats.most_common_rejection_reason}",
        f"Best-performing symbol: {summary.best_performing_symbol}",
        f"Worst-performing symbol: {summary.worst_performing_symbol}",
        f"Replay edge: {summary.replay_edge}",
        f"Sample size warning: {summary.sample_size_warning}",
        f"Historical confidence: {summary.confidence_label}",
    ]

    lines.extend(("", "Edge analytics"))
    if summary.edge_analytics.strongest_conditions:
        lines.append(f"Top historical edge: {_edge_condition_line(summary.edge_analytics.strongest_conditions[0])}")
    else:
        lines.append("Top historical edge: N/A")
    if summary.edge_analytics.weakest_conditions:
        lines.append(f"Weakest historical condition: {_edge_condition_line(summary.edge_analytics.weakest_conditions[0])}")
    else:
        lines.append("Weakest historical condition: N/A")
    lines.append(f"Low-sample / unstable conditions: {len(summary.edge_analytics.unstable_conditions)}")

    if summary_only:
        return "\n".join(lines)

    lines.extend(("", "Mode split"))
    for mode in LiquidityGrabMode:
        stats = summary.per_mode_stats.get(mode.value, ReplayStats())
        lines.append(
            (
                f"- {mode.value.title()}: setups {stats.total_setups}, filled {stats.filled_trades}, "
                f"missed {stats.missed_entries}, TP1 {_display_percent(stats.tp1_rate)}, "
                f"TP2 {_display_percent(stats.tp2_rate)}, avg {_display_r(stats.average_r)}"
            )
        )

    for symbol in summary.symbols:
        best, worst = _best_worst(symbol.trades)
        lines.extend(
            (
                "",
                symbol.symbol,
                f"- Setups: {symbol.stats.total_setups}",
                f"- Filled: {symbol.stats.filled_trades}",
                f"- Missed entries: {symbol.stats.missed_entries}",
                f"- Win rate to TP1: {_display_percent(symbol.stats.tp1_rate)}",
                f"- Win rate to TP2: {_display_percent(symbol.stats.tp2_rate)}",
                f"- Avg R: {_display_r(symbol.stats.average_r)}",
                f"- Median R: {_display_r(symbol.stats.median_r)}",
                f"- Max drawdown: {_display_r(symbol.stats.max_drawdown_r)}",
                f"- Best trade: {_display_r(best)}",
                f"- Worst trade: {_display_r(worst)}",
                f"- Average hold time: {_display_candles(symbol.stats.average_hold_time)}",
                f"- Sample size warning: {symbol.sample_size_warning}",
                f"- Main failure reason: {symbol.main_failure_reason}",
                f"- Quality note: {symbol.quality_note}",
            )
        )

    if include_setup_diagnostics:
        lines.extend(("", "Setup diagnostics"))
        trades = tuple(trade for result in summary.symbols for trade in result.trades)
        if not trades:
            lines.append("- N/A")
        for trade in trades:
            lines.append(
                (
                    f"- {trade.symbol} {trade.mode.value} {trade.direction.value}: "
                    f"detected {trade.candidate.detected_at_index}, outcome {trade.outcome.value}, "
                    f"filled {_bool_text(trade.filled)}, final {_display_r(trade.final_r_multiple)}, "
                    f"MFE {_display_r(trade.max_favorable_excursion)}, "
                    f"MAE {_display_r(trade.max_adverse_excursion)}"
                )
            )

        lines.extend(("", "Condition breakdowns"))
        if not summary.edge_analytics.condition_groups:
            lines.append("- N/A")
        for condition in summary.edge_analytics.condition_groups:
            lines.append(f"- {_edge_condition_line(condition)}")

    lines.extend(("", f"Data notes: {_sequence_text(summary.data_notes)}"))
    return "\n".join(lines)


def backtest_json_payload(summary: ReplaySummary) -> dict[str, object]:
    return {
        "backtest_summary": _backtest_summary_payload(summary),
        "edge_analytics": _json_ready(summary.edge_analytics.model_dump(mode="json")),
        "expectancy_metrics": _json_ready(summary.expectancy_metrics.model_dump(mode="json")),
        "confidence_label": summary.confidence_label,
        "per_symbol_stats": {
            result.symbol: _stats_payload(result.stats)
            for result in summary.symbols
        },
        "per_mode_stats": {
            mode: _stats_payload(stats)
            for mode, stats in summary.per_mode_stats.items()
        },
        "individual_setup_results": [
            _trade_payload(trade)
            for result in summary.symbols
            for trade in result.trades
        ],
    }


def _backtest_summary_payload(summary: ReplaySummary) -> dict[str, object]:
    best_trade = _best_trade(summary)
    worst_trade = _worst_trade(summary)
    payload = {
        "strategy": summary.strategy,
        "symbols_tested": summary.symbols_tested,
        "historical_candles": summary.historical_candles,
        "setups_found": summary.stats.total_setups,
        "filled_setups": summary.stats.filled_trades,
        "missed_entries": summary.stats.missed_entries,
        "win_rate_to_tp1": summary.stats.tp1_rate,
        "win_rate_to_tp2": summary.stats.tp2_rate,
        "average_r": summary.stats.average_r,
        "median_r": summary.stats.median_r,
        "max_drawdown_r": summary.stats.max_drawdown_r,
        "best_setup": _setup_label(best_trade),
        "worst_setup": _setup_label(worst_trade),
        "average_hold_time": summary.stats.average_hold_time,
        "time_unit": "candles",
        "most_common_rejection_reason": summary.stats.most_common_rejection_reason,
        "best_performing_symbol": summary.best_performing_symbol,
        "worst_performing_symbol": summary.worst_performing_symbol,
        "sample_size": summary.sample_size,
        "sample_size_warning": summary.sample_size_warning,
        "expectancy_metrics": summary.expectancy_metrics,
        "confidence_label": summary.confidence_label,
        "sample_size_note": (
            "Sample size too small for reliable conclusion."
            if summary.sample_size == "low_sample_size"
            else "Sample size is sufficient for this diagnostic report."
        ),
        "safety_note": summary.safety_note,
        "data_notes": list(summary.data_notes),
    }
    return _json_ready(payload)


def _stats_payload(stats: ReplayStats) -> dict[str, object]:
    return _json_ready(stats.model_dump(mode="json"))


def _trade_payload(trade: ReplayTradeResult) -> dict[str, object]:
    return _json_ready(
        {
            "symbol": trade.symbol,
            "mode": trade.mode.value,
            "direction": trade.direction.value,
            "outcome": trade.outcome.value,
            "entry": trade.entry,
            "stop": trade.stop,
            "tp1": trade.tp1,
            "tp2": trade.tp2,
            "tp3": trade.tp3,
            "entry_filled": trade.entry_filled,
            "sl_hit": trade.sl_hit,
            "tp1_hit": trade.tp1_hit,
            "tp2_hit": trade.tp2_hit,
            "tp3_hit": trade.tp3_hit,
            "max_favorable_excursion": trade.max_favorable_excursion,
            "max_adverse_excursion": trade.max_adverse_excursion,
            "time_to_entry": trade.time_to_entry,
            "time_to_tp1": trade.time_to_tp1,
            "time_to_final_outcome": trade.time_to_final_outcome,
            "time_unit": trade.time_unit,
            "final_r_multiple": trade.final_r_multiple,
            "detected_at_index": trade.candidate.detected_at_index,
            "detected_at_timestamp": trade.candidate.detected_at_timestamp,
            "fill_index": trade.fill_index,
            "exit_index": trade.exit_index,
            "failure_reason": trade.failure_reason,
            "invalidation": trade.candidate.invalidation,
            "risk_warning": trade.candidate.risk_warning,
            "condition_key": trade.candidate.condition_key.model_dump(mode="json"),
        }
    )


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return _display(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, BaseModel):
        return _json_ready(value.model_dump(mode="json"))
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_ready(item) for item in value]
    return value


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
    decision_timestamp: int,
    execution_prefix: Sequence[Any],
) -> dict[str, tuple[Any, ...]]:
    output: dict[str, tuple[Any, ...]] = {}
    for timeframe, candles in candles_by_timeframe.items():
        raw = tuple(candles)
        if timeframe == execution_timeframe:
            output[timeframe] = tuple(execution_prefix)
            continue
        output[timeframe] = _slice_until(
            raw,
            decision_timestamp,
            timeframe=timeframe,
        )
    if execution_timeframe not in output:
        output[execution_timeframe] = tuple(execution_prefix)
    return output


def _slice_until(
    candles: Sequence[Any],
    decision_timestamp: int,
    *,
    timeframe: str,
) -> tuple[Any, ...]:
    if not candles:
        return ()
    return closed_candles_as_of(
        candles,
        timeframe=timeframe,
        decision_timestamp=decision_timestamp,
        minimum_closed_history=0,
    ).candles


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
        condition_key=condition_key_from_diagnostics(
            symbol=symbol,
            mode=mode.value,
            diagnostics=_setup_condition_diagnostics(setup),
            readiness_score=setup.trust_meter.percentage,
        ),
    )


def _setup_condition_diagnostics(setup: LiquidityGrabSetup) -> dict[str, Any]:
    return {
        "bias": setup.bias,
        "mode": setup.mode.value,
        "htf_2d_trend": setup.htf_2d_trend,
        "mtf_12h_trend": setup.mtf_12h_trend,
        "trend": setup.trend,
        "derivatives_supports_trade": setup.derivatives_supports_trade,
        "derivatives_conflict_reason": setup.derivatives_conflict_reason,
        "funding_context": setup.funding_context,
        "oi_context": setup.oi_context,
        "crowding_risk": setup.crowding_risk,
        "poc": setup.poc,
        "entry": setup.entry,
        "entry_low": setup.entry_low,
        "entry_high": setup.entry_high,
        "rr_to_tp2": setup.rr_to_tp2,
        "trust_percentage": setup.trust_meter.percentage,
        "execution_sweep_status": setup.execution_sweep_status,
        "sweep_magnitude_atr": setup.sweep.magnitude_atr,
        "pullback_zone_status": setup.pullback_zone_status,
        "selected_zone_type": setup.selected_zone_type,
        "ob_zone": setup.ob_zone.model_dump(),
        "fvg_zone": setup.fvg_zone.model_dump(),
        "fib_alignment_status": setup.fib_alignment.status,
        "gates_passed": setup.gates_passed,
        "gates_failed": setup.gates_failed,
        "sweep_diagnostics": setup.sweep_diagnostics,
        "bos_choch_diagnostics": setup.structure_shift_diagnostics,
    }


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
    time_to_tp1: MaybeInt = NA
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
                time_to_final_outcome=index - candidate.detected_at_index,
                failure_reason="Invalidation touched before the limit entry filled.",
            )
        if not entry_touched:
            continue

        fill_index = index
        fill_timestamp = candle.timestamp
        break
    else:
        expiry_candle = candles[max_fill_index] if candles else None
        return _result(
            candidate,
            ReplayOutcome.NOT_FILLED,
            r_multiple=Decimal("0"),
            exit_candle=expiry_candle,
            time_to_final_outcome=max_fill_index - candidate.detected_at_index if candles else NA,
            failure_reason=f"Limit entry did not fill within {_fill_window(candidate, config)} candle(s).",
        )

    if fill_index == NA:
        expiry_candle = candles[max_fill_index] if candles else None
        return _result(
            candidate,
            ReplayOutcome.NOT_FILLED,
            r_multiple=Decimal("0"),
            exit_candle=expiry_candle,
            time_to_final_outcome=max_fill_index - candidate.detected_at_index if candles else NA,
            failure_reason=f"Limit entry did not fill within {_fill_window(candidate, config)} candle(s).",
        )

    last_index = min(len(candles) - 1, int(fill_index) + max_hold_candles)
    final_target = _final_target_number(candidate)
    for index in range(int(fill_index), last_index + 1):
        candle = candles[index]
        stop_touched = _price_touched(candle, candidate.stop)
        new_highest_tp = max(highest_tp, _highest_target_touched(candidate, candle))
        target_touched = new_highest_tp > highest_tp

        if stop_touched and target_touched and config.same_candle_policy == "conservative":
            if highest_tp == 0:
                return _result(
                    candidate,
                    ReplayOutcome.STOPPED,
                    filled=True,
                    fill_index=fill_index,
                    fill_timestamp=fill_timestamp,
                    exit_candle=candle,
                    exit_price=candidate.stop,
                    highest_tp_hit=0,
                    r_multiple=Decimal("-1"),
                    candles_held=index - int(fill_index),
                    time_to_entry=int(fill_index) - candidate.detected_at_index,
                    time_to_tp1=time_to_tp1,
                    time_to_final_outcome=index - candidate.detected_at_index,
                    excursion_candles=candles[int(fill_index) : index + 1],
                    failure_reason=_failure_reason_for(ReplayOutcome.STOPPED),
                )
            return _target_result(
                candidate,
                highest_tp,
                fill_index=fill_index,
                fill_timestamp=fill_timestamp,
                exit_candle=candle,
                candles_held=index - int(fill_index),
                time_to_tp1=time_to_tp1,
                excursion_candles=candles[int(fill_index) : index + 1],
            )

        if target_touched:
            highest_tp = new_highest_tp
            if highest_tp >= 1 and time_to_tp1 == NA:
                time_to_tp1 = index - int(fill_index)
            if highest_tp >= final_target:
                return _target_result(
                    candidate,
                    highest_tp,
                    fill_index=fill_index,
                    fill_timestamp=fill_timestamp,
                    exit_candle=candle,
                    candles_held=index - int(fill_index),
                    time_to_tp1=time_to_tp1,
                    excursion_candles=candles[int(fill_index) : index + 1],
                )

        if stop_touched:
            if highest_tp > 0:
                return _target_result(
                    candidate,
                    highest_tp,
                    fill_index=fill_index,
                    fill_timestamp=fill_timestamp,
                    exit_candle=candle,
                    candles_held=index - int(fill_index),
                    time_to_tp1=time_to_tp1,
                    excursion_candles=candles[int(fill_index) : index + 1],
                )
            return _result(
                candidate,
                ReplayOutcome.STOPPED,
                filled=True,
                fill_index=fill_index,
                fill_timestamp=fill_timestamp,
                exit_candle=candle,
                exit_price=candidate.stop,
                highest_tp_hit=0,
                r_multiple=Decimal("-1"),
                candles_held=index - int(fill_index),
                time_to_entry=int(fill_index) - candidate.detected_at_index,
                time_to_tp1=time_to_tp1,
                time_to_final_outcome=index - candidate.detected_at_index,
                excursion_candles=candles[int(fill_index) : index + 1],
                failure_reason=_failure_reason_for(ReplayOutcome.STOPPED),
            )

    expiry_candle = candles[last_index]
    if highest_tp > 0:
        return _target_result(
            candidate,
            highest_tp,
            fill_index=fill_index,
            fill_timestamp=fill_timestamp,
            exit_candle=expiry_candle,
            candles_held=last_index - int(fill_index),
            time_to_tp1=time_to_tp1,
            excursion_candles=candles[int(fill_index) : last_index + 1],
        )

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
        time_to_entry=int(fill_index) - candidate.detected_at_index,
        time_to_tp1=time_to_tp1,
        time_to_final_outcome=last_index - candidate.detected_at_index,
        excursion_candles=candles[int(fill_index) : last_index + 1],
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


def _target_result(
    candidate: ReplaySetupCandidate,
    target_number: int,
    *,
    fill_index: int,
    fill_timestamp: MaybeInt,
    exit_candle: _ReplayCandle,
    candles_held: int,
    time_to_tp1: MaybeInt,
    excursion_candles: Sequence[_ReplayCandle],
) -> ReplayTradeResult:
    risk = _risk(candidate)
    target = _target_for(candidate, target_number)
    return _result(
        candidate,
        _tp_outcome(target_number),
        filled=True,
        fill_index=fill_index,
        fill_timestamp=fill_timestamp,
        exit_candle=exit_candle,
        exit_price=target,
        highest_tp_hit=target_number,
        r_multiple=_r_for_target(candidate, target_number, risk),
        candles_held=candles_held,
        time_to_entry=fill_index - candidate.detected_at_index,
        time_to_tp1=time_to_tp1,
        time_to_final_outcome=exit_candle.index - candidate.detected_at_index,
        excursion_candles=excursion_candles,
        failure_reason=_failure_reason_for(_tp_outcome(target_number)),
    )


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
    time_to_entry: MaybeInt = NA,
    time_to_tp1: MaybeInt = NA,
    time_to_final_outcome: MaybeInt = NA,
    excursion_candles: Sequence[_ReplayCandle] = (),
    failure_reason: str = NA,
) -> ReplayTradeResult:
    mfe, mae = _excursions(candidate, excursion_candles)
    return ReplayTradeResult(
        symbol=candidate.symbol,
        mode=candidate.mode,
        direction=candidate.direction,
        candidate=candidate,
        outcome=outcome,
        entry=candidate.entry,
        stop=candidate.stop,
        tp1=candidate.tp1,
        tp2=candidate.tp2,
        tp3=candidate.tp3,
        filled=filled,
        entry_filled=filled,
        sl_hit=outcome == ReplayOutcome.STOPPED,
        tp1_hit=highest_tp_hit >= 1,
        tp2_hit=highest_tp_hit >= 2,
        tp3_hit=highest_tp_hit >= 3,
        fill_index=fill_index,
        fill_timestamp=fill_timestamp,
        exit_index=exit_candle.index if exit_candle is not None else NA,
        exit_timestamp=exit_candle.timestamp if exit_candle is not None else NA,
        exit_price=NA if exit_price == NA else _quantize(_decimal_from(exit_price, "exit_price")),
        highest_tp_hit=highest_tp_hit,
        max_favorable_excursion=mfe,
        max_adverse_excursion=mae,
        time_to_entry=time_to_entry,
        time_to_tp1=time_to_tp1,
        time_to_final_outcome=time_to_final_outcome,
        r_multiple=_quantize(r_multiple),
        final_r_multiple=_quantize(r_multiple),
        candles_held=candles_held,
        failure_reason=failure_reason,
    )


def _stats_for_trades(
    trades: Sequence[ReplayTradeResult],
    *,
    rejected_setup_count: int,
    near_miss_count: int,
    rejection_reasons: Sequence[str] = (),
) -> ReplayStats:
    filled = tuple(trade for trade in trades if trade.filled)
    r_values = tuple(trade.r_multiple for trade in filled)
    wins = tuple(value for value in r_values if value > 0)
    losses = tuple(value for value in r_values if value < 0)
    tp1_hits = sum(1 for trade in filled if trade.highest_tp_hit >= 1)
    tp2_hits = sum(1 for trade in filled if trade.highest_tp_hit >= 2)
    missed_entries = sum(1 for trade in trades if trade.outcome == ReplayOutcome.NOT_FILLED)
    profit_sum = sum(wins, Decimal("0"))
    loss_sum = abs(sum(losses, Decimal("0")))
    average_r = _mean(r_values)
    common_rejection_reason = Counter(rejection_reasons).most_common(1)
    return ReplayStats(
        total_setups=len(trades),
        filled_trades=len(filled),
        missed_entries=missed_entries,
        win_rate=_rate(len(wins), len(filled)),
        tp1_rate=_rate(tp1_hits, len(filled)),
        tp2_rate=_rate(tp2_hits, len(filled)),
        average_r=average_r,
        median_r=NA if not r_values else _quantize(Decimal(str(median(r_values)))),
        max_drawdown_r=_max_drawdown_r(r_values),
        best_r=NA if not r_values else max(r_values),
        worst_r=NA if not r_values else min(r_values),
        max_loss_streak=_max_streak(r_values, positive=False),
        max_win_streak=_max_streak(r_values, positive=True),
        expectancy_r=average_r,
        profit_factor=NA if loss_sum == 0 else _quantize(profit_sum / loss_sum),
        average_time_in_trade=_mean(tuple(Decimal(trade.candles_held) for trade in filled)),
        average_hold_time=_mean(tuple(Decimal(trade.candles_held) for trade in filled)),
        most_common_rejection_reason=common_rejection_reason[0][0] if common_rejection_reason else NA,
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
            candidate.entry,
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


def _final_target_number(candidate: ReplaySetupCandidate) -> int:
    return 3 if candidate.tp3 != NA else 2


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


def _excursions(candidate: ReplaySetupCandidate, candles: Sequence[_ReplayCandle]) -> tuple[Decimal, Decimal]:
    risk = _risk(candidate)
    if risk <= 0 or not candles:
        return Decimal("0.00000000"), Decimal("0.00000000")

    favorable_values: list[Decimal] = []
    adverse_values: list[Decimal] = []
    for candle in candles:
        if candidate.direction == ReplayDirection.LONG:
            favorable_values.append((candle.high - candidate.entry) / risk)
            adverse_values.append((candle.low - candidate.entry) / risk)
        else:
            favorable_values.append((candidate.entry - candle.low) / risk)
            adverse_values.append((candidate.entry - candle.high) / risk)
    max_favorable = max(Decimal("0"), max(favorable_values))
    max_adverse = min(Decimal("0"), min(adverse_values))
    return _quantize(max_favorable), _quantize(max_adverse)


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


def _best_trade(summary: ReplaySummary) -> ReplayTradeResult | None:
    filled = [trade for result in summary.symbols for trade in result.trades if trade.filled]
    if not filled:
        return None
    return max(filled, key=lambda trade: trade.r_multiple)


def _worst_trade(summary: ReplaySummary) -> ReplayTradeResult | None:
    filled = [trade for result in summary.symbols for trade in result.trades if trade.filled]
    if not filled:
        return None
    return min(filled, key=lambda trade: trade.r_multiple)


def _setup_label(trade: ReplayTradeResult | None) -> str:
    if trade is None:
        return NA
    return f"{trade.symbol} {trade.mode.value} {trade.direction.value} {_display_r(trade.r_multiple)}"


def _edge_condition_line(condition: Any) -> str:
    key = condition.condition_key
    metrics = condition.expectancy_metrics
    return (
        f"{key.symbol} {key.mode} | {condition.confidence_label} | "
        f"expectancy {_display_r(metrics.expectancy)} | sample {metrics.fills} | "
        f"TP1 {_display_percent(metrics.tp1_hit_rate)} | TP2 {_display_percent(metrics.tp2_hit_rate)} | "
        f"score {_display(condition.edge_score)} | "
        f"HTF {key.htf_direction_alignment}, derivatives {key.derivatives_state}, "
        f"VP {key.volume_profile_alignment}, RR {key.rr_bucket}, readiness {key.readiness_score_bucket}"
    )


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


def _max_drawdown_r(values: Sequence[Decimal]) -> MaybeDecimal:
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


def _per_mode_stats(trades: Sequence[ReplayTradeResult]) -> dict[str, ReplayStats]:
    return {
        mode.value: _stats_for_trades(
            tuple(trade for trade in trades if trade.mode == mode),
            rejected_setup_count=0,
            near_miss_count=0,
        )
        for mode in LiquidityGrabMode
    }


def _edge_analytics_for_trades(
    trades: Sequence[ReplayTradeResult],
    *,
    min_sample: int,
) -> EdgeAnalyticsReport:
    return build_edge_analytics_report(
        tuple(
            EdgeAnalyticsRecord(
                condition_key=trade.candidate.condition_key,
                filled=trade.filled,
                tp1_hit=trade.tp1_hit,
                tp2_hit=trade.tp2_hit,
                r_multiple=trade.r_multiple if trade.filled else NA,
                candles_held=trade.candles_held,
            )
            for trade in trades
        ),
        min_sample=min_sample,
    )


def _best_worst_symbol(symbol_results: Sequence[ReplaySymbolResult]) -> tuple[str, str]:
    scored = [
        (result.symbol, _decimal_from(result.stats.average_r, "average_r"))
        for result in symbol_results
        if result.stats.average_r != NA and result.stats.filled_trades > 0
    ]
    if not scored:
        return NA, NA
    best = max(scored, key=lambda item: item[1])[0]
    worst = min(scored, key=lambda item: item[1])[0]
    return best, worst


def _setup_rejection_reason(setup: LiquidityGrabSetup) -> str:
    if setup.first_failed_gate != NA:
        return str(setup.first_failed_gate)
    if setup.hard_rejection_reasons:
        return str(setup.hard_rejection_reasons[0])
    if setup.pullback_failure_reason != NA:
        return str(setup.pullback_failure_reason)
    if setup.confirmation_bos_choch_reason != NA:
        return str(setup.confirmation_bos_choch_reason)
    if setup.gates_failed:
        return str(setup.gates_failed[0])
    return "Rejected setup"


def _empty_symbol_result(
    symbol: str,
    candles_by_timeframe: Mapping[str, Sequence[Any]],
    config: ReplayConfig,
    *,
    extra_data_notes: Sequence[str] = (),
) -> ReplaySymbolResult:
    raw_execution = tuple(candles_by_timeframe.get(config.execution_timeframe, ()))
    raw_execution = raw_execution[-config.replay_candles :]
    edge_analytics = _edge_analytics_for_trades((), min_sample=config.edge_min_sample)
    return ReplaySymbolResult(
        symbol=symbol,
        historical_candles=len(raw_execution),
        stats=ReplayStats(),
        per_mode_stats=_per_mode_stats(()),
        replay_edge=NA,
        sample_size=_sample_size(0),
        sample_size_warning=_sample_size(0),
        main_failure_reason="backtest_max_setups reached",
        quality_note="Replay skipped after the configured setup cap was reached.",
        edge_analytics=edge_analytics,
        expectancy_metrics=edge_analytics.expectancy_metrics,
        confidence_label=edge_analytics.confidence_label,
        data_notes=_unique_strings((*DEFAULT_DATA_NOTES, *extra_data_notes)),
    )


def _normalize_candles(candles: Sequence[Any], *, timeframe: str) -> tuple[_ReplayCandle, ...]:
    output: list[_ReplayCandle] = []
    timeline = validate_candle_sequence(candles, timeframe=timeframe, require_continuity=True)
    for index, causal in enumerate(timeline):
        candle = causal.source
        output.append(
            _ReplayCandle(
                index=index,
                timestamp=causal.open_timestamp_ms,
                close_timestamp=causal.close_timestamp_ms,
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


def _display_candles(value: object) -> str:
    text = _display(value)
    return text if text == NA else f"{text} candles"


def _bool_text(value: bool) -> str:
    return "yes" if value else "no"


def _sequence_text(values: Sequence[str]) -> str:
    return ", ".join(values) if values else NA


__all__ = [
    "EdgeAnalyticsReport",
    "EdgeConditionKey",
    "ExpectancyMetrics",
    "ReplayConfig",
    "ReplayDirection",
    "ReplayOutcome",
    "ReplaySetupCandidate",
    "ReplayStats",
    "ReplaySummary",
    "ReplaySymbolResult",
    "ReplayTradeResult",
    "StrategyReplayEngine",
    "backtest_json_payload",
    "format_replay_summary",
    "replay_liquidity_grab_pullback",
]
