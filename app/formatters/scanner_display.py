from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.analytics.near_miss_intelligence import NearMissIntelligence, build_near_miss_intelligence
from app.analytics.pullback_intelligence import PullbackIntelligenceResult, build_pullback_intelligence
from app.analytics.setup_quality import SetupQualityResult, SetupQualityState
from app.analytics.target_intelligence import TargetIntelligenceResult
from app.core.minimum_rr import hard_mode_minimum_rr
from app.data.dtos import NA
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunResult, ScannerSymbolResult

DisplayMode = Literal["compact", "normal", "full"]
DisplayBucket = Literal["valid", "near_miss", "no_setup", "data_issue"]
DisplayStatus = Literal["valid_setup", "near_miss", "no_setup", "data_issue", "scan_error"]
ReadinessLabel = Literal["VALID SETUP", "HOT WATCH", "WATCH", "REJECTED", "DATA ISSUE"]

BULLET = "\u2022"
CHECK = "\u2705"
CROSS = "\u274c"
PIN = "\U0001F4CD"
BRAIN = "\U0001F9E0"
TARGET = "\U0001F3AF"
CHART = "\U0001F4CA"
GREEN_CIRCLE = "\U0001F7E2"
YELLOW_CIRCLE = "\U0001F7E1"
RED_CIRCLE = "\U0001F534"
WHITE_CIRCLE = "\u26aa"
SWORDS = "\u2694\ufe0f"
DASH = "\u2014"
ARROW = "\u2192"
CARD_RULE = "\u2501" * 22
FOOTER = f"{SWORDS} Candle Craft | Signal. Structure. Execution."

MODE_ORDER = ("challenge", "swing", "scalp")
SETUP_PROGRESS_TOTAL = 4
DEFAULT_MAX_DISPLAY_RESULTS = 10
BUCKET_ORDER: dict[DisplayBucket, int] = {
    "valid": 0,
    "near_miss": 1,
    "no_setup": 2,
    "data_issue": 3,
}
EARLY_CORE_GATES = {
    "missing_confirmed_sweep",
    "missing_confirmation_structure_shift",
    "no_execution_candles",
    "missing_confirmation_candles",
    "not_enough_candles",
    "atr_unavailable",
}
DATA_INCOMPLETE_GATES = {
    "no_execution_candles",
    "missing_confirmation_candles",
    "not_enough_candles",
    "atr_unavailable",
}
PULLBACK_FAIL_GATES = {
    "no_ob_or_fvg_zone",
    "pullback_too_deep",
    "pullback_beyond_786",
    "wick_sweep_reclaim",
    "body_acceptance_failure",
    "structural_breakdown",
    "no_displacement_candle",
    "challenge_limit_entry_missing",
}
RR_FAIL_GATES = {
    "missing_rr",
    "missing_target",
    "rr_below_minimum",
    "challenge_rr_below_3",
    "rr_too_low",
}
FINAL_CONFLUENCE_FAIL_GATES = {
    "trust_meter_below_minimum",
    "challenge_trust_below_85",
    "entry_window_expired",
    "challenge_illiquid_token",
    "challenge_btc_abnormal",
    "challenge_event_window",
    "btc_volatility_guard",
    "btc_d_guard",
    "event_guard",
    "derivatives_conflict",
    "funding_oi_guard",
    "regime_compatibility",
}
HOT_WATCH_GATES = (
    RR_FAIL_GATES
    | {
        "trust_meter_below_minimum",
        "challenge_trust_below_85",
        "quality_filter",
    }
)
CONTEXT_REJECTION_GATES = {
    "challenge_illiquid_token",
    "challenge_btc_abnormal",
    "challenge_event_window",
    "btc_volatility_guard",
    "btc_d_guard",
    "event_guard",
    "derivatives_conflict",
    "funding_oi_guard",
}
LATE_FAILURE_GATES = (
    PULLBACK_FAIL_GATES
    | RR_FAIL_GATES
    | FINAL_CONFLUENCE_FAIL_GATES
    | {"missing_displacement_impulse", "missing_stop", "target_integrity"}
)
STAGE_ORDER = {
    "data": 0,
    "sweep": 1,
    "structure": 2,
    "pullback": 3,
    "ob_fvg": 4,
    "rr": 5,
    "final": 6,
    "target_integrity": 7,
    "valid": 8,
}
QUALITY_STATE_ORDER = {
    SetupQualityState.HIGH_QUALITY_TRADE: 0,
    SetupQualityState.VALID_BUT_LOWER_QUALITY: 1,
    SetupQualityState.WATCHLIST_NEAR_MISS: 2,
    SetupQualityState.REJECTED_NO_EDGE: 3,
    SetupQualityState.DATA_ISSUE: 4,
}
READINESS_LABEL_ORDER: dict[ReadinessLabel, int] = {
    "VALID SETUP": 0,
    "HOT WATCH": 1,
    "WATCH": 2,
    "REJECTED": 3,
    "DATA ISSUE": 4,
}


@dataclass(frozen=True)
class ProgressItem:
    label: str
    passed: bool
    evaluated: bool = True


@dataclass(frozen=True)
class SetupReadiness:
    readiness_score: int
    readiness_label: ReadinessLabel
    next_trigger_needed: str
    priority_rank_reason: str


@dataclass(frozen=True)
class SymbolDisplay:
    display_status: DisplayStatus
    display_status_label: str
    display_bucket: DisplayBucket
    display_bucket_label: str
    display_priority_score: int
    display_reason: str
    hidden_by_default: bool
    failed_stage: str
    setup_progress_total: int
    setup_progress_passed: int
    passed_checks: tuple[str, ...]
    failed_checks: tuple[str, ...]
    short_reason: str
    action_label: str
    progress_items: tuple[ProgressItem, ...]
    failed_gate: str
    near_miss_intelligence: NearMissIntelligence | None
    readiness_score: int
    readiness_label: ReadinessLabel
    next_trigger_needed: str
    priority_rank_reason: str


@dataclass(frozen=True)
class RankedSymbolDisplay:
    symbol_result: ScannerSymbolResult
    display: SymbolDisplay
    display_rank: int
    original_index: int


def build_symbol_display(symbol_result: ScannerSymbolResult) -> SymbolDisplay:
    diagnostics = representative_strategy_diagnostics(symbol_result)
    failed_gate = _failed_gate(symbol_result, diagnostics)
    progress_items = _progress_items(symbol_result, diagnostics, failed_gate)
    passed_checks = tuple(item.label for item in progress_items if item.passed)
    failed_checks = tuple(
        item.label
        for item in progress_items
        if not item.passed and (item.evaluated or _core_checks_passed(progress_items))
    )
    display_status = _display_status(symbol_result, diagnostics, failed_gate, progress_items)
    display_bucket = _display_bucket(display_status)
    setup_progress_passed = sum(1 for item in progress_items if item.passed)
    short_reason = _short_reason(symbol_result, diagnostics, display_status)
    near_miss_intelligence = _near_miss_intelligence(symbol_result, diagnostics, failed_gate, short_reason)
    action_label = _action_label(symbol_result, display_bucket, near_miss_intelligence)
    readiness = _setup_readiness(
        symbol_result,
        diagnostics=diagnostics,
        display_status=display_status,
        display_bucket=display_bucket,
        failed_gate=failed_gate,
        progress_items=progress_items,
        setup_progress_passed=setup_progress_passed,
    )
    return SymbolDisplay(
        display_status=display_status,
        display_status_label=_display_status_label(display_status),
        display_bucket=display_bucket,
        display_bucket_label=_display_status_label(display_status),
        display_priority_score=_display_priority_score(symbol_result, diagnostics, display_status, failed_gate),
        display_reason=short_reason,
        hidden_by_default=display_bucket == "no_setup",
        failed_stage=_failed_stage(symbol_result, failed_gate, display_status),
        setup_progress_total=SETUP_PROGRESS_TOTAL,
        setup_progress_passed=setup_progress_passed,
        passed_checks=passed_checks,
        failed_checks=failed_checks,
        short_reason=short_reason,
        action_label=action_label,
        progress_items=progress_items,
        failed_gate=failed_gate,
        near_miss_intelligence=near_miss_intelligence,
        readiness_score=readiness.readiness_score,
        readiness_label=readiness.readiness_label,
        next_trigger_needed=readiness.next_trigger_needed,
        priority_rank_reason=readiness.priority_rank_reason,
    )


def display_fields(symbol_result: ScannerSymbolResult, *, display_rank: int | None = None) -> dict[str, Any]:
    display = build_symbol_display(symbol_result)
    diagnostics = representative_strategy_diagnostics(symbol_result)
    pullback_intelligence = _pullback_intelligence(symbol_result, diagnostics)
    target_intelligence = _target_intelligence(symbol_result, diagnostics)
    lifecycle_integrity = _lifecycle_integrity_fields(symbol_result, display)
    return {
        "display_rank": display_rank,
        "display_bucket": display.display_bucket,
        "display_priority_score": display.display_priority_score,
        "display_reason": display.display_reason,
        "hidden_by_default": display.hidden_by_default,
        "failed_stage": display.failed_stage,
        "display_status": display.display_status,
        "display_status_label": display.display_status_label,
        "setup_progress_total": display.setup_progress_total,
        "setup_progress_passed": display.setup_progress_passed,
        "passed_checks": list(display.passed_checks),
        "failed_checks": list(display.failed_checks),
        "short_reason": display.short_reason,
        "action_label": display.action_label,
        "readiness_score": display.readiness_score,
        "readiness_label": display.readiness_label,
        "next_trigger_needed": display.next_trigger_needed,
        "priority_rank_reason": display.priority_rank_reason,
        "lifecycle_current_state": symbol_result.lifecycle_state.current_state.value
        if symbol_result.lifecycle_state is not None
        else NA,
        "lifecycle_previous_state": _state_value(symbol_result.lifecycle_state.previous_state)
        if symbol_result.lifecycle_state is not None
        else NA,
        **lifecycle_integrity,
        "near_miss_intelligence": display.near_miss_intelligence.model_dump(mode="json")
        if display.near_miss_intelligence is not None
        else None,
        "pullback_intelligence": pullback_intelligence.model_dump(mode="json")
        if pullback_intelligence is not None
        else None,
        "target_intelligence": target_intelligence.model_dump(mode="json")
        if target_intelligence is not None
        else None,
        "target_quality_grade": _enum_value(target_intelligence.target_quality_grade)
        if target_intelligence is not None
        else NA,
        "target_failure_type": _enum_value(target_intelligence.target_failure_type)
        if target_intelligence is not None
        else NA,
        "rr_compression_reason": _display(target_intelligence.rr_compression_reason)
        if target_intelligence is not None
        else NA,
        "wick_close_structure": pullback_intelligence.wick_close_structure.model_dump(mode="json")
        if pullback_intelligence is not None
        else None,
        "acceptance_status": _display(pullback_intelligence.acceptance_status) if pullback_intelligence is not None else NA,
        "reclaim_strength": _display(pullback_intelligence.reclaim_strength) if pullback_intelligence is not None else NA,
        "body_acceptance_ratio": _display(pullback_intelligence.body_acceptance_ratio)
        if pullback_intelligence is not None
        else NA,
    }


def rank_scan_results(
    results: Sequence[ScannerSymbolResult],
    *,
    rank_results: bool = True,
) -> tuple[RankedSymbolDisplay, ...]:
    ranked_input = [
        RankedSymbolDisplay(
            symbol_result=symbol_result,
            display=build_symbol_display(symbol_result),
            display_rank=0,
            original_index=index,
        )
        for index, symbol_result in enumerate(results)
    ]
    if rank_results:
        ranked_input.sort(
            key=lambda item: (
                *_ranking_priority(item),
                item.original_index,
            )
        )
    return tuple(
        RankedSymbolDisplay(
            symbol_result=item.symbol_result,
            display=item.display,
            display_rank=index + 1,
            original_index=item.original_index,
        )
        for index, item in enumerate(ranked_input)
    )


def filter_ranked_results(
    ranked_results: Sequence[RankedSymbolDisplay],
    *,
    show_no_setups: bool = False,
    bucket_filter: set[DisplayBucket] | None = None,
    max_display_results: int | None = DEFAULT_MAX_DISPLAY_RESULTS,
) -> tuple[RankedSymbolDisplay, ...]:
    filtered = []
    for item in ranked_results:
        bucket = item.display.display_bucket
        if bucket_filter is not None and bucket not in bucket_filter:
            continue
        if bucket_filter is None and bucket == "no_setup" and not show_no_setups:
            continue
        filtered.append(item)

    if max_display_results is None:
        return tuple(filtered)
    return tuple(filtered[: max(0, max_display_results)])


def format_scan_dashboard(
    result: ScannerRunResult,
    *,
    ranked_results: Sequence[RankedSymbolDisplay] | None = None,
    visible_results: Sequence[RankedSymbolDisplay] | None = None,
) -> str:
    ranked = tuple(ranked_results) if ranked_results is not None else rank_scan_results(result.results)
    visible = tuple(visible_results) if visible_results is not None else filter_ranked_results(ranked)
    counts = {
        "valid": 0,
        "near_miss": 0,
        "no_setup": 0,
        "data_issue": 0,
    }
    visible_counts = {
        "valid": 0,
        "near_miss": 0,
        "no_setup": 0,
        "data_issue": 0,
    }
    for item in ranked:
        counts[item.display.display_bucket] += 1
    for item in visible:
        visible_counts[item.display.display_bucket] += 1

    config = result.config
    hidden_no_setups = max(0, counts["no_setup"] - visible_counts["no_setup"])
    scan_errors = sum(1 for item in ranked if item.display.display_status == "scan_error")
    completed = max(0, result.scanned_symbols - scan_errors)
    cache_stats = result.cache_stats or {}
    data_warning_count = _optional_data_warning_count(result)
    runtime = result.runtime_stats
    runtime_populated = bool(
        runtime.total_runtime_seconds
        or runtime.completed_symbols
        or runtime.skipped_symbols
        or runtime.errored_symbols
        or runtime.timeout_count
        or runtime.global_timeout_hit
    )
    completed_count = runtime.completed_symbols if runtime_populated else completed
    skipped_errored_count = runtime.skipped_errored_symbols if runtime_populated else scan_errors
    slowest = (
        f"{runtime.slowest_symbol} ({_seconds_text(runtime.slowest_symbol_seconds)})"
        if runtime.slowest_symbol != NA
        else NA
    )
    return "\n".join(
        (
            "Candle Craft Scanner",
            f"Strategy: {_strategy_title(_display(config.strategy_name))}",
            (
                "Timeframes: "
                f"{_timeframe_label(config.htf_timeframe)} {ARROW} "
                f"{_timeframe_label(config.bias_timeframe)} {ARROW} "
                f"{_timeframe_label(config.execution_timeframe)} {ARROW} "
                f"{_timeframe_label(config.confirmation_timeframe)}"
            ),
            f"Symbols scanned: {result.scanned_symbols}",
            f"Completed: {completed_count}",
            f"Runtime: {_seconds_text(runtime.total_runtime_seconds)}",
            f"Average per symbol: {_seconds_text(runtime.average_seconds_per_symbol)}",
            f"Slowest symbol: {slowest}",
            f"Timeouts: {runtime.timeout_count}",
            f"Skipped/errored: {skipped_errored_count}",
            f"No setup: {counts['no_setup']}",
            f"Near miss: {counts['near_miss']}",
            f"Trade ideas: {counts['valid']}",
            f"Scan errors: {scan_errors}",
            f"Data warnings: {data_warning_count} optional endpoint warnings.",
            f"Cache hits: {int(cache_stats.get('hits', 0) or 0)}",
            f"Cache misses: {int(cache_stats.get('misses', 0) or 0)}",
            "",
            *_market_regime_lines(result),
            "",
            *_performance_memory_dashboard_lines(result),
            "",
            *_symbol_health_dashboard_lines(result),
            "",
            f"{GREEN_CIRCLE} Valid setups: {counts['valid']}",
            f"{YELLOW_CIRCLE} Near misses: {counts['near_miss']}",
            f"{WHITE_CIRCLE} Hidden rejected/no-setup symbols: {hidden_no_setups}",
            f"{RED_CIRCLE} Data issues: {counts['data_issue']}",
        )
    )


def _symbol_health_dashboard_lines(result: ScannerRunResult) -> tuple[str, ...]:
    health = result.symbol_health
    if not isinstance(health, Mapping) or not health or not health.get("enabled"):
        return ()
    return (
        "Symbol Health",
        f"Prioritized symbols: {_display(health.get('prioritized_symbols'))}",
        f"Cooldown symbols: {_display(health.get('cooldown_symbols'))}",
        f"Timeout strikes this run: {_display(health.get('timeout_strikes_this_run'))}",
        f"Slowest symbols: {_slow_symbol_text(health.get('slowest_symbols'))}",
        f"Skipped due to cooldown: {_display(health.get('skipped_due_to_cooldown'))}",
    )


def _slow_symbol_text(values: Any) -> str:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return NA
    items: list[str] = []
    for value in values:
        if not isinstance(value, Mapping):
            continue
        symbol = _display(value.get("symbol"))
        runtime = _display(value.get("runtime_sec"))
        if symbol != NA and runtime != NA:
            items.append(f"{symbol} {_seconds_text(runtime)}")
    return "[" + ", ".join(items) + "]" if items else "[]"


def format_symbol_compact_line(symbol_result: ScannerSymbolResult, *, rank: int | None = None) -> str:
    diagnostics = representative_strategy_diagnostics(symbol_result)
    display = build_symbol_display(symbol_result)
    quality = symbol_result.setup_quality
    status_text = display.display_status_label.split(" ", 1)[1]
    rank_text = f"#{rank} " if rank is not None else ""
    if _quality_evaluated(quality):
        line = (
            f"{rank_text}{symbol_result.symbol} {DASH} {quality.quality_state.value} {DASH} "
            f"{quality.quality_grade.value} {DASH} {quality.quality_score} {DASH} {quality.action_label}"
        )
        historical = _historical_edge_compact(symbol_result)
        regime_warning = _first_regime_warning(symbol_result)
        parts = [line]
        lifecycle = _lifecycle_compact(symbol_result)
        if lifecycle != NA:
            parts.append(lifecycle)
        if regime_warning != NA:
            parts.append(f"Regime: {regime_warning}")
        if historical != NA:
            parts.append(historical)
        return " | ".join(parts)
    parts = [
        f"{rank_text}{display.display_status_label.split(' ', 1)[0]} {symbol_result.symbol} {DASH} {status_text}",
        f"Readiness {display.readiness_score}/100 {display.readiness_label}",
        f"Modes {_mode_summary(symbol_result)}",
        _execution_summary(diagnostics),
        f"Progress {display.setup_progress_passed}/{display.setup_progress_total}",
    ]
    lifecycle = _lifecycle_compact(symbol_result)
    if lifecycle != NA:
        parts.append(lifecycle)
    if display.display_bucket != "valid" and display.failed_gate != NA:
        parts.append(f"Gate: {display.failed_gate}")
    parts.append(display.display_reason)
    parts.append(display.action_label)
    historical = _historical_edge_compact(symbol_result)
    if historical != NA:
        parts.append(historical)
    regime_warning = _first_regime_warning(symbol_result)
    if regime_warning != NA:
        parts.append(f"Regime: {regime_warning}")
    return " | ".join(parts)


def format_symbol_card(
    symbol_result: ScannerSymbolResult,
    *,
    include_diagnostics: bool = False,
    rank: int | None = None,
    show_near_miss_plan: bool = True,
) -> str:
    diagnostics = representative_strategy_diagnostics(symbol_result)
    display = build_symbol_display(symbol_result)
    if display.display_bucket == "near_miss" and show_near_miss_plan:
        return _format_near_miss_card(
            symbol_result,
            display=display,
            include_diagnostics=include_diagnostics,
            rank=rank,
        )

    icon, status_text = display.display_status_label.split(" ", 1)
    rank_text = f"#{rank} " if rank is not None else ""
    lines = [
        CARD_RULE,
        f"{rank_text}{icon} {symbol_result.symbol} {DASH} {status_text}",
        CARD_RULE,
        "",
        f"{BULLET} Bucket: {display.display_bucket_label}",
        *_readiness_summary_lines(display),
        *_lifecycle_card_lines(symbol_result),
        f"{BULLET} Mode(s): {_mode_summary(symbol_result)}",
        f"{BULLET} HTF/Bias/Execution: {_execution_summary(diagnostics)}",
        *(_card_failed_gate_lines(display)),
        *_quality_summary_lines(symbol_result.setup_quality),
        *_historical_edge_lines(symbol_result),
        "",
        f"{PIN} Context",
        f"{BULLET} 2D HTF: {_title_value(diagnostics.get('htf_2d_trend'))}",
        f"{BULLET} 12H Bias: {_title_value(diagnostics.get('mtf_12h_trend'))}",
        f"{BULLET} {_volume_profile_text(symbol_result)}",
        f"{BULLET} {_derivatives_context_text(symbol_result)}",
        *_symbol_regime_warning_lines(symbol_result),
        "",
        f"{CHECK} Passed",
        *_passed_lines(symbol_result, diagnostics, display),
        "",
        f"{CROSS} Failed",
        *_failed_lines(display),
        *_optional_pullback_intelligence_lines(symbol_result, diagnostics, display.display_bucket),
        *_optional_target_intelligence_lines(symbol_result, diagnostics),
        "",
        f"{CHART} Setup Progress: {display.setup_progress_passed}/{display.setup_progress_total}",
        *_progress_lines(display),
        "",
        f"{BRAIN} Reason",
        display.short_reason,
        "",
        f"{TARGET} Action",
        display.action_label,
    ]

    if include_diagnostics:
        lines.extend(("", "Diagnostics", *_diagnostic_lines(symbol_result, diagnostics)))

    lines.extend(("", FOOTER))
    return "\n".join(lines)


def _format_near_miss_card(
    symbol_result: ScannerSymbolResult,
    *,
    display: SymbolDisplay,
    include_diagnostics: bool,
    rank: int | None,
) -> str:
    diagnostics = representative_strategy_diagnostics(symbol_result)
    icon, status_text = display.display_status_label.split(" ", 1)
    rank_text = f"#{rank} " if rank is not None else ""
    intelligence = display.near_miss_intelligence
    status = intelligence.watchlist_status if intelligence is not None else display.action_label
    failed_gate = intelligence.primary_failed_gate if intelligence is not None else display.failed_gate
    reason = intelligence.short_reason if intelligence is not None else display.short_reason
    activation_hint = intelligence.activation_hint if intelligence is not None else NA
    invalidation_hint = intelligence.invalidation_hint if intelligence is not None else NA
    action = intelligence.action_label if intelligence is not None else display.action_label
    conditions = intelligence.next_required_conditions if intelligence is not None else (NA,)

    lines = [
        CARD_RULE,
        f"{rank_text}{icon} {symbol_result.symbol} {DASH} {status_text}",
        CARD_RULE,
        "",
        f"Status: {status}",
        f"Readiness score: {display.readiness_score}/100",
        f"Readiness label: {display.readiness_label}",
        *_lifecycle_card_lines(symbol_result),
        f"Next trigger needed: {display.next_trigger_needed}",
        f"Failed gate: {failed_gate}",
        f"Reason: {reason}",
        *_quality_summary_lines(symbol_result.setup_quality),
        *_optional_pullback_intelligence_lines(symbol_result, diagnostics, display.display_bucket),
        *_optional_target_intelligence_lines(symbol_result, diagnostics),
        *_historical_edge_lines(symbol_result),
        "",
        "Needs next:",
        *_numbered_condition_lines(conditions),
        "",
        f"Activation hint: {activation_hint}",
        f"Invalidation hint: {invalidation_hint}",
        f"Action: {action}",
    ]

    if include_diagnostics:
        lines.extend(
            (
                "",
                "Near-miss diagnostics",
                f"{BULLET} Quality note: {intelligence.quality_note if intelligence is not None else NA}",
                f"{BULLET} Progress: {display.setup_progress_passed}/{display.setup_progress_total}",
                f"{BULLET} Passed checks: {_sequence_text(display.passed_checks)}",
                f"{BULLET} Failed checks: {_sequence_text(display.failed_checks)}",
                "",
                "Diagnostics",
                *_diagnostic_lines(symbol_result, diagnostics),
            )
        )

    lines.extend(("", FOOTER))
    return "\n".join(lines)


def format_pullback_intelligence_block(symbol_result: ScannerSymbolResult) -> str:
    diagnostics = representative_strategy_diagnostics(symbol_result)
    intelligence = _pullback_intelligence(symbol_result, diagnostics)
    if intelligence is None:
        return "Pullback Intelligence\nN/A"
    return "\n".join(("Pullback Intelligence", *_pullback_intelligence_lines(intelligence)))


def format_target_intelligence_block(symbol_result: ScannerSymbolResult) -> str:
    diagnostics = representative_strategy_diagnostics(symbol_result)
    intelligence = _target_intelligence(symbol_result, diagnostics)
    if intelligence is None:
        return "Target Intelligence\nN/A"
    return "\n".join(("Target Intelligence", *_target_intelligence_lines(intelligence)))


def _numbered_condition_lines(conditions: Sequence[str]) -> list[str]:
    values = [condition for condition in conditions if _display(condition) != NA]
    if not values:
        values = [NA]
    return [f"{index}. {condition}" for index, condition in enumerate(values, start=1)]


def representative_strategy_diagnostics(symbol_result: ScannerSymbolResult) -> Mapping[str, Any]:
    if symbol_result.rejection_stage == "target_integrity":
        for diagnostics in symbol_result.strategy_diagnostics.values():
            if isinstance(diagnostics, Mapping) and _diagnostics_failed_gate(diagnostics) == "target_integrity":
                return diagnostics
    for mode in symbol_result.valid_strategy_modes:
        diagnostics = symbol_result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for mode in symbol_result.rejected_strategy_modes:
        diagnostics = symbol_result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for mode in MODE_ORDER:
        diagnostics = symbol_result.strategy_diagnostics.get(mode)
        if isinstance(diagnostics, Mapping):
            return diagnostics
    for diagnostics in symbol_result.strategy_diagnostics.values():
        if isinstance(diagnostics, Mapping):
            return diagnostics
    return {}


def _diagnostics_failed_gate(diagnostics: Mapping[str, Any]) -> str:
    failed_gate = _display(diagnostics.get("first_failed_gate"))
    if failed_gate != NA:
        return failed_gate
    gates_failed = _sequence_values(diagnostics.get("gates_failed"))
    if gates_failed:
        return gates_failed[0]
    return NA


def _display_status(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    failed_gate: str,
    progress_items: tuple[ProgressItem, ...],
) -> DisplayStatus:
    if _trade_idea_created(symbol_result):
        return "valid_setup"
    if symbol_result.status in (ScannerPipelineStatus.SCAN_ERROR, ScannerPipelineStatus.FAILED) or symbol_result.error_message:
        return "scan_error"
    if _data_incomplete(symbol_result, diagnostics, failed_gate):
        return "data_issue"
    if _near_miss_eligible(diagnostics, failed_gate, progress_items):
        return "near_miss"
    return "no_setup"


def _near_miss_eligible(
    diagnostics: Mapping[str, Any],
    failed_gate: str,
    progress_items: tuple[ProgressItem, ...],
) -> bool:
    if failed_gate == "target_integrity":
        return True
    if not _core_checks_passed(progress_items):
        return False
    if failed_gate == NA or failed_gate in EARLY_CORE_GATES:
        return False
    gates_failed = set(_sequence_values(diagnostics.get("gates_failed")))
    if failed_gate not in LATE_FAILURE_GATES and not bool(gates_failed & LATE_FAILURE_GATES):
        return False
    if failed_gate == "no_ob_or_fvg_zone" or "no_ob_or_fvg_zone" in gates_failed:
        return True
    return _has_valid_pullback_or_calculated_rr_failure(diagnostics, failed_gate, gates_failed)


def _has_valid_pullback_or_calculated_rr_failure(
    diagnostics: Mapping[str, Any],
    failed_gate: str,
    gates_failed: set[str],
) -> bool:
    gates_passed = set(_sequence_values(diagnostics.get("gates_passed")))
    pullback_status = _display(diagnostics.get("pullback_zone_status"))
    if pullback_status in ("valid", "passed") or "pullback_zone" in gates_passed:
        return True

    rr_value = _display(diagnostics.get("rr_to_tp2"))
    rr_failed = failed_gate in RR_FAIL_GATES or bool(gates_failed & RR_FAIL_GATES)
    return rr_failed and rr_value != NA


def _display_bucket(display_status: DisplayStatus) -> DisplayBucket:
    if display_status == "valid_setup":
        return "valid"
    if display_status in ("data_issue", "scan_error"):
        return "data_issue"
    return display_status


def _trade_idea_created(symbol_result: ScannerSymbolResult) -> bool:
    if symbol_result.regime_blocked:
        return False
    return (
        symbol_result.trade_idea is not None
        or ScannerPipelineStatus.IDEA_CREATED in symbol_result.status_history
        or ScannerPipelineStatus.ALERT_DRY_RUN_CREATED in symbol_result.status_history
        or ScannerPipelineStatus.JOURNAL_ENTRY_CREATED in symbol_result.status_history
    )


def _data_incomplete(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    failed_gate: str,
) -> bool:
    if symbol_result.error_message:
        return True
    if failed_gate in DATA_INCOMPLETE_GATES:
        return True
    if diagnostics.get("error"):
        return True
    return symbol_result.current_price == NA and symbol_result.latest_close == NA and failed_gate in (NA, "current_price")


def _progress_items(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    failed_gate: str,
) -> tuple[ProgressItem, ...]:
    gates_passed = set(_sequence_values(diagnostics.get("gates_passed")))
    gates_failed = set(_sequence_values(diagnostics.get("gates_failed")))
    execution_tf = _display(diagnostics.get("execution_timeframe"))
    confirmation_tf = _display(diagnostics.get("confirmation_timeframe"))
    if execution_tf == NA:
        execution_tf = "15m"
    if confirmation_tf == NA:
        confirmation_tf = "5m"

    sweep_status = _display(diagnostics.get("execution_sweep_status"))
    confirmation_status = _display(diagnostics.get("confirmation_structure_shift_status"))
    pullback_status = _display(diagnostics.get("pullback_zone_status"))
    rr_value = _display(diagnostics.get("rr_to_tp2"))
    rr_failed = failed_gate in RR_FAIL_GATES or bool(gates_failed & RR_FAIL_GATES)
    pullback_failed = failed_gate in PULLBACK_FAIL_GATES or pullback_status == "failed"

    sweep_passed = sweep_status == "passed" or "sweep" in gates_passed
    confirmation_passed = confirmation_status == "passed" or "bos_choch" in gates_passed
    pullback_passed = pullback_status in ("valid", "passed") or "pullback_zone" in gates_passed
    rr_passed = (rr_value != NA and not rr_failed) or "rr" in gates_passed

    valid = _trade_idea_created(symbol_result)
    core_passed = sweep_passed and confirmation_passed
    return (
        ProgressItem(f"{execution_tf} sweep", sweep_passed, valid or sweep_status not in (NA, "not_evaluated")),
        ProgressItem(
            f"{confirmation_tf} BOS/CHoCH",
            confirmation_passed,
            valid or confirmation_status not in (NA, "not_evaluated"),
        ),
        ProgressItem(
            "Pullback zone",
            pullback_passed,
            valid or pullback_failed or pullback_status != NA or core_passed,
        ),
        ProgressItem(
            "RR",
            rr_passed,
            valid or rr_failed or rr_value != NA or core_passed,
        ),
    )


def _core_checks_passed(progress_items: tuple[ProgressItem, ...]) -> bool:
    return len(progress_items) >= 2 and progress_items[0].passed and progress_items[1].passed


def _display_status_label(display_status: DisplayStatus) -> str:
    labels = {
        "valid_setup": f"{GREEN_CIRCLE} VALID SETUP",
        "near_miss": f"{YELLOW_CIRCLE} NEAR MISS",
        "no_setup": f"{WHITE_CIRCLE} REJECTED",
        "data_issue": f"{RED_CIRCLE} DATA ISSUE",
        "scan_error": f"{RED_CIRCLE} DATA ISSUE",
    }
    return labels[display_status]


def _short_reason(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    display_status: DisplayStatus,
) -> str:
    if display_status == "valid_setup":
        return "Trade idea created after scanner gates passed."
    if display_status == "scan_error":
        if symbol_result.error_message:
            return f"scan_error: {symbol_result.error_message}"
        return "scan_error"

    failed_gate = _failed_gate(symbol_result, diagnostics)
    if failed_gate == "target_integrity":
        reason = _target_integrity_reason(symbol_result, diagnostics)
        if reason != NA:
            return reason
    if failed_gate in ("missing_confirmation_structure_shift", "missing_confirmation_candles"):
        reason = _display(diagnostics.get("confirmation_bos_choch_reason"))
        if reason != NA:
            return reason

    for key in (
        "regime_compatibility_reason",
        "pullback_failure_reason",
        "derivatives_conflict_reason",
        "confirmation_bos_choch_reason",
    ):
        reason = _display(diagnostics.get(key))
        if reason != NA:
            return reason

    for key in ("rr_diagnostics", "trust_meter_diagnostics"):
        reason = _clean_diagnostic_sentence(diagnostics.get(key))
        if reason != NA and reason.lower().startswith("failed"):
            return reason

    hard_rejections = _sequence_values(diagnostics.get("hard_rejection_reasons"))
    if hard_rejections:
        return hard_rejections[0]
    if symbol_result.rejection_reasons:
        return "; ".join(symbol_result.rejection_reasons)
    if symbol_result.rejection_reason:
        return symbol_result.rejection_reason
    if symbol_result.error_message:
        return symbol_result.error_message
    if display_status == "data_issue":
        return "Required market data is missing or unavailable."
    return "No valid setup."


def _near_miss_intelligence(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    failed_gate: str,
    short_reason: str,
) -> NearMissIntelligence | None:
    if symbol_result.near_miss_intelligence is not None:
        return symbol_result.near_miss_intelligence
    if failed_gate == NA:
        return None
    return build_near_miss_intelligence(
        failed_gate=failed_gate,
        short_reason=short_reason,
        diagnostics=diagnostics,
    )


def _pullback_intelligence(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
) -> PullbackIntelligenceResult | None:
    if symbol_result.pullback_intelligence is not None:
        return symbol_result.pullback_intelligence
    payload = diagnostics.get("pullback_intelligence")
    if isinstance(payload, PullbackIntelligenceResult):
        return payload
    if isinstance(payload, Mapping):
        return PullbackIntelligenceResult.model_validate(payload)
    if diagnostics:
        return build_pullback_intelligence(diagnostics)
    return None


def _target_intelligence(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
) -> TargetIntelligenceResult | None:
    if symbol_result.target_intelligence is not None:
        return symbol_result.target_intelligence
    payload = diagnostics.get("target_intelligence")
    if isinstance(payload, TargetIntelligenceResult):
        return payload
    if isinstance(payload, Mapping):
        return TargetIntelligenceResult.model_validate(payload)
    return None


def _optional_pullback_intelligence_lines(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    display_bucket: DisplayBucket,
) -> tuple[str, ...]:
    if display_bucket == "valid":
        return ()
    intelligence = _pullback_intelligence(symbol_result, diagnostics)
    if intelligence is None:
        return ()
    return ("", "Pullback Intelligence", *_pullback_intelligence_lines(intelligence))


def _optional_target_intelligence_lines(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
) -> tuple[str, ...]:
    intelligence = _target_intelligence(symbol_result, diagnostics)
    if intelligence is None or not _target_has_visible_data(intelligence):
        return ()
    return ("", "Target Intelligence", *_target_intelligence_lines(intelligence))


def _target_has_visible_data(intelligence: TargetIntelligenceResult) -> bool:
    return any(
        _display(value) != NA
        for value in (
            intelligence.tp1_candidate,
            intelligence.tp2_candidate,
            intelligence.rr_to_tp1,
            intelligence.rr_to_tp2,
            intelligence.target_failure_type,
        )
    ) and _enum_value(intelligence.target_failure_type) != "DATA_INCOMPLETE"


def _target_intelligence_lines(intelligence: TargetIntelligenceResult) -> tuple[str, ...]:
    return (
        f"- TP1 candidate: {_price_display(intelligence.tp1_candidate)}",
        f"- TP2 candidate: {_price_display(intelligence.tp2_candidate)}",
        f"- Clean path: {_display(intelligence.clean_path_distance)}",
        f"- RR to TP1: {_display(intelligence.rr_to_tp1)}",
        f"- RR to TP2: {_display(intelligence.rr_to_tp2)}",
        f"- Target quality: {_enum_value(intelligence.target_quality_grade)}",
        f"- Failure: {_enum_value(intelligence.target_failure_type)}",
        f"- Next condition: {_display(intelligence.next_target_condition)}",
    )


def _pullback_intelligence_lines(intelligence: PullbackIntelligenceResult) -> tuple[str, ...]:
    failure_type = _enum_value(intelligence.pullback_failure_type)
    grade = _enum_value(intelligence.pullback_quality_grade)
    return (
        f"- Failure type: {failure_type}",
        f"- Grade: {grade}",
        f"- Depth: {_display(intelligence.pullback_depth_ratio)}",
        f"- Fib status: {_display(intelligence.fib_zone_status)}",
        f"- OB/FVG: {_display(intelligence.ob_fvg_status)}",
        f"- Displacement: {_display(intelligence.displacement_strength)}",
        f"- Candles since BOS: {_display(intelligence.candles_since_bos)}",
        f"- Freshness: {_freshness_label(intelligence.freshness_score)}",
        f"- RR potential: {_score_label(intelligence.rr_potential_score)}",
        f"- Structure risk: {_risk_label(intelligence.structure_risk_score)}",
        f"- Next condition: {_display(intelligence.next_pullback_condition)}",
        "",
        "Wick/Close Structure",
        f"- Wick depth: {_display(intelligence.wick_depth_ratio)}",
        f"- Close depth: {_display(intelligence.close_depth_ratio)}",
        f"- Acceptance: {_display(intelligence.acceptance_status)}",
        f"- Reclaim: {_display(intelligence.reclaim_strength)}",
        f"- Candles below zone: {_display(intelligence.candles_below_fib_zone)}",
        f"- Structural status: {_display(intelligence.structural_reclaim_status)}",
    )


def _score_label(value: object) -> str:
    text = _display(value)
    if text == NA:
        return NA
    try:
        score = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if score >= 75:
        return "high"
    if score >= 50:
        return "moderate"
    return "low"


def _freshness_label(value: object) -> str:
    text = _display(value)
    if text == NA:
        return NA
    try:
        score = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if score >= 75:
        return "strong"
    if score >= 50:
        return "acceptable"
    return "weak"


def _risk_label(value: object) -> str:
    text = _display(value)
    if text == NA:
        return NA
    try:
        score = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if score >= 75:
        return "high"
    if score >= 50:
        return "elevated"
    return "low"


def _enum_value(value: object) -> str:
    return _display(getattr(value, "value", value))


def _action_label(
    symbol_result: ScannerSymbolResult,
    display_bucket: DisplayBucket,
    intelligence: NearMissIntelligence | None = None,
) -> str:
    if _target_integrity_blocked(symbol_result, representative_strategy_diagnostics(symbol_result)):
        return "Wait for target expansion"
    quality = symbol_result.setup_quality
    if _quality_evaluated(quality):
        return quality.action_label
    if intelligence is not None and intelligence.action_label != NA:
        return intelligence.action_label
    if display_bucket == "valid":
        return "Trade idea created"
    if display_bucket == "near_miss":
        return "Watchlist only"
    if display_bucket == "data_issue":
        return "Data insufficient"
    return "Rejected"


def _setup_readiness(
    symbol_result: ScannerSymbolResult,
    *,
    diagnostics: Mapping[str, Any],
    display_status: DisplayStatus,
    display_bucket: DisplayBucket,
    failed_gate: str,
    progress_items: tuple[ProgressItem, ...],
    setup_progress_passed: int,
) -> SetupReadiness:
    score = _readiness_score(symbol_result, diagnostics, display_status, failed_gate, progress_items)
    label = _readiness_label(
        symbol_result,
        diagnostics=diagnostics,
        display_status=display_status,
        display_bucket=display_bucket,
        failed_gate=failed_gate,
        progress_items=progress_items,
        setup_progress_passed=setup_progress_passed,
        readiness_score=score,
    )
    next_trigger = _next_trigger_needed(
        symbol_result,
        diagnostics=diagnostics,
        display_status=display_status,
        failed_gate=failed_gate,
        readiness_label=label,
    )
    return SetupReadiness(
        readiness_score=score,
        readiness_label=label,
        next_trigger_needed=next_trigger,
        priority_rank_reason=_priority_rank_reason(
            label=label,
            score=score,
            next_trigger=next_trigger,
            failed_gate=failed_gate,
            progress_passed=setup_progress_passed,
            diagnostics=diagnostics,
        ),
    )


def _readiness_score(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    display_status: DisplayStatus,
    failed_gate: str,
    progress_items: tuple[ProgressItem, ...],
) -> int:
    score = Decimal("0")
    expected_trend = _expected_trend(_setup_bias(diagnostics))
    score += _trend_alignment_points(diagnostics.get("htf_2d_trend"), expected_trend, Decimal("10"))
    score += _trend_alignment_points(diagnostics.get("mtf_12h_trend"), expected_trend, Decimal("10"))

    if len(progress_items) > 0 and progress_items[0].passed:
        score += Decimal("15")
    if len(progress_items) > 1 and progress_items[1].passed:
        score += Decimal("15")
    if len(progress_items) > 2 and progress_items[2].passed:
        score += Decimal("15")
    score += _rr_readiness_points(diagnostics, failed_gate)
    score += _derivatives_readiness_points(symbol_result, diagnostics)
    score += _volume_profile_readiness_points(symbol_result)
    score += _data_certainty_points(symbol_result, diagnostics, failed_gate)

    quality = symbol_result.setup_quality
    if _quality_evaluated(quality):
        if quality.quality_state in (
            SetupQualityState.HIGH_QUALITY_TRADE,
            SetupQualityState.VALID_BUT_LOWER_QUALITY,
        ):
            score = max(score, Decimal(min(100, max(75, quality.quality_score))))
        elif quality.quality_state == SetupQualityState.WATCHLIST_NEAR_MISS:
            score = max(score, Decimal(min(89, max(45, quality.quality_score))))
        elif quality.quality_state == SetupQualityState.DATA_ISSUE:
            score = min(score, Decimal("25"))

    if display_status == "valid_setup":
        score = max(score, Decimal("90"))
    if display_status in ("data_issue", "scan_error"):
        score = min(score, Decimal("25"))
    if _critical_data_issue(symbol_result, diagnostics, failed_gate):
        score = min(score, Decimal("30"))
    if _severe_derivatives_conflict(symbol_result, diagnostics, failed_gate):
        score = min(score, Decimal("55"))
    if symbol_result.regime_penalty:
        score -= Decimal(min(25, symbol_result.regime_penalty))
    if symbol_result.regime_blocked:
        score = min(score, Decimal("55"))

    return _bounded_int(score)


def _readiness_label(
    symbol_result: ScannerSymbolResult,
    *,
    diagnostics: Mapping[str, Any],
    display_status: DisplayStatus,
    display_bucket: DisplayBucket,
    failed_gate: str,
    progress_items: tuple[ProgressItem, ...],
    setup_progress_passed: int,
    readiness_score: int,
) -> ReadinessLabel:
    quality = symbol_result.setup_quality
    if display_status in ("data_issue", "scan_error") or _critical_data_issue(symbol_result, diagnostics, failed_gate):
        return "DATA ISSUE"
    if _quality_evaluated(quality) and quality.quality_state == SetupQualityState.DATA_ISSUE:
        return "DATA ISSUE"
    if _trade_idea_created(symbol_result) or (
        _quality_evaluated(quality)
        and quality.quality_state
        in (SetupQualityState.HIGH_QUALITY_TRADE, SetupQualityState.VALID_BUT_LOWER_QUALITY)
    ):
        return "VALID SETUP"
    if _severe_derivatives_conflict(symbol_result, diagnostics, failed_gate):
        return "REJECTED"
    if _hot_watch_eligible(
        diagnostics=diagnostics,
        failed_gate=failed_gate,
        progress_items=progress_items,
        setup_progress_passed=setup_progress_passed,
    ):
        return "HOT WATCH"
    if _quality_evaluated(quality) and quality.quality_state == SetupQualityState.WATCHLIST_NEAR_MISS:
        return "WATCH"
    if display_bucket == "near_miss":
        return "WATCH"
    if _watch_eligible(progress_items, failed_gate, readiness_score):
        return "WATCH"
    return "REJECTED"


def _hot_watch_eligible(
    *,
    diagnostics: Mapping[str, Any],
    failed_gate: str,
    progress_items: tuple[ProgressItem, ...],
    setup_progress_passed: int,
) -> bool:
    if not _core_checks_passed(progress_items):
        return False
    if setup_progress_passed < 3:
        return False
    if failed_gate == NA or failed_gate in EARLY_CORE_GATES or failed_gate in CONTEXT_REJECTION_GATES:
        return False
    gates_failed = set(_sequence_values(diagnostics.get("gates_failed")))
    if not gates_failed and failed_gate != NA:
        gates_failed = {failed_gate}
    if len(gates_failed) != 1:
        return False
    if not gates_failed <= HOT_WATCH_GATES:
        return False
    return not _severe_derivatives_conflict_from_diagnostics(diagnostics, failed_gate)


def _watch_eligible(
    progress_items: tuple[ProgressItem, ...],
    failed_gate: str,
    readiness_score: int,
) -> bool:
    if failed_gate in CONTEXT_REJECTION_GATES:
        return False
    if failed_gate in DATA_INCOMPLETE_GATES:
        return False
    if len(progress_items) >= 2 and progress_items[0].passed:
        return True
    return readiness_score >= 45 and failed_gate not in {"missing_confirmed_sweep"}


def _next_trigger_needed(
    symbol_result: ScannerSymbolResult,
    *,
    diagnostics: Mapping[str, Any],
    display_status: DisplayStatus,
    failed_gate: str,
    readiness_label: ReadinessLabel,
) -> str:
    if readiness_label == "DATA ISSUE" or display_status in ("data_issue", "scan_error"):
        return "Avoid: data unreliable"
    if failed_gate == "target_integrity":
        intelligence = _target_intelligence(symbol_result, diagnostics)
        if intelligence is not None and _display(intelligence.next_target_condition) != NA:
            return _display(intelligence.next_target_condition)
        return "Wait for target expansion"
    if _severe_derivatives_conflict(symbol_result, diagnostics, failed_gate):
        return "Avoid: derivatives conflict"
    if readiness_label == "VALID SETUP":
        return "No trigger needed; setup is already valid"
    if failed_gate == "missing_confirmed_sweep":
        return "Wait for new sweep"
    if failed_gate in ("missing_confirmation_structure_shift", "missing_confirmation_candles"):
        return "Wait for 5m BOS/CHoCH"
    if failed_gate in PULLBACK_FAIL_GATES or failed_gate in {"missing_displacement_impulse", "missing_stop"}:
        return "Wait for clean OB/FVG pullback"
    if failed_gate in RR_FAIL_GATES:
        return "Wait for RR expansion above minimum"
    if failed_gate in {"trust_meter_below_minimum", "challenge_trust_below_85", "quality_filter"}:
        return "Wait for final quality gate to improve"
    if failed_gate in CONTEXT_REJECTION_GATES:
        return "Avoid: derivatives conflict" if failed_gate in {"derivatives_conflict", "funding_oi_guard"} else "Avoid: context guard active"
    if failed_gate == "risk":
        return "Avoid: risk gate failed"
    return "Wait for failed gate to clear"


def _priority_rank_reason(
    *,
    label: ReadinessLabel,
    score: int,
    next_trigger: str,
    failed_gate: str,
    progress_passed: int,
    diagnostics: Mapping[str, Any],
) -> str:
    return (
        f"{label}: score {score}/100; progress {progress_passed}/{SETUP_PROGRESS_TOTAL}; "
        f"2D {_title_value(diagnostics.get('htf_2d_trend'))}; "
        f"12H {_title_value(diagnostics.get('mtf_12h_trend'))}; "
        f"failed gate {_display(failed_gate)}; next {next_trigger}."
    )


def _readiness_summary_lines(display: SymbolDisplay) -> tuple[str, ...]:
    return (
        f"{BULLET} Readiness score: {display.readiness_score}/100",
        f"{BULLET} Readiness label: {display.readiness_label}",
        f"{BULLET} Next trigger needed: {display.next_trigger_needed}",
    )


def _ranking_priority(item: RankedSymbolDisplay) -> tuple[int, int, int, int, int, int, int]:
    quality = item.symbol_result.setup_quality
    readiness_order = READINESS_LABEL_ORDER[item.display.readiness_label]
    bucket_order = BUCKET_ORDER[item.display.display_bucket]
    if _quality_evaluated(quality):
        return (
            bucket_order,
            readiness_order,
            -item.display.readiness_score,
            QUALITY_STATE_ORDER[quality.quality_state],
            -quality.quality_score,
            -item.display.display_priority_score,
            0,
        )
    return (
        bucket_order,
        readiness_order,
        -item.display.readiness_score,
        -item.display.display_priority_score,
        0,
        0,
        0,
    )


def _quality_summary_lines(quality: SetupQualityResult) -> tuple[str, ...]:
    if not _quality_evaluated(quality):
        return ()
    return (
        f"{BULLET} Quality: {quality.quality_state.value} | Grade: {quality.quality_grade.value} | Score: {quality.quality_score}",
        f"{BULLET} Edge: {quality.profitability_edge_score}",
        f"{BULLET} Risk: {quality.execution_risk_score} (lower is better)",
        f"{BULLET} Action: {quality.action_label}",
        f"{BULLET} Strongest: {_sequence_text(quality.strongest_factors)}",
        f"{BULLET} Weakest: {_sequence_text(quality.weakest_factors)}",
        f"{BULLET} Reason: {quality.decision_reason}",
    )


def _historical_edge_lines(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    memory = symbol_result.performance_memory
    if isinstance(memory, Mapping) and memory:
        adjustments = memory.get("memory_adjustments")
        if not isinstance(adjustments, Mapping):
            adjustments = {}
        samples = _first_non_na_text(memory.get("historical_samples"), memory.get("samples"))
        confidence = _display(memory.get("confidence_bucket"))
        avg_r = _first_non_na_text(memory.get("average_r"), memory.get("historical_expectancy"))
        tp1 = _display(memory.get("tp1_rate"))
        tp2 = _display(memory.get("tp2_rate"))
        warning = _display(memory.get("historical_warning"))
        lines = [
            f"{BULLET} Performance Memory: samples {samples} | confidence {confidence} | "
            f"Avg R {_r_text(avg_r)} | TP1 {_percent_text(tp1)} | TP2 {_percent_text(tp2)}",
            f"{BULLET} Similar setup performance: {_display(memory.get('similar_setup_performance'))}",
            f"{BULLET} Regime compatibility: {_display(memory.get('regime_compatibility'))}",
            f"{BULLET} Symbol historical quality: {_display(memory.get('symbol_historical_quality'))}",
        ]
        if warning != NA:
            lines.append(f"{BULLET} Confidence note: {warning}")
        edge_adjustment = _display(adjustments.get("edge_score_adjustment"))
        if edge_adjustment != NA:
            lines.append(f"{BULLET} Memory adjustment: edge {edge_adjustment}")
        return tuple(lines)

    summary = symbol_result.historical_match_summary
    if not isinstance(summary, Mapping) or not summary:
        return ()
    metrics = summary.get("expectancy_metrics")
    if not isinstance(metrics, Mapping):
        metrics = {}
    sample = _first_non_na_text(summary.get("matching_sample_size"), metrics.get("fills"))
    expectancy = _display(metrics.get("expectancy"))
    tp1 = _display(metrics.get("tp1_hit_rate"))
    tp2 = _display(metrics.get("tp2_hit_rate"))
    label = _display(summary.get("confidence_label"))
    line = (
        f"{BULLET} Historical edge: {label} | expectancy {_r_text(expectancy)} | "
        f"sample {sample} | TP1 {_percent_text(tp1)} | TP2 {_percent_text(tp2)}"
    )
    warning = _display(summary.get("warning"))
    if label == "LOW SAMPLE" and warning != NA:
        return (line, f"{BULLET} Historical warning: {warning}")
    return (line,)


def _historical_edge_compact(symbol_result: ScannerSymbolResult) -> str:
    memory = symbol_result.performance_memory
    if isinstance(memory, Mapping) and memory:
        confidence = _display(memory.get("confidence_bucket"))
        sample = _display(memory.get("historical_samples"))
        expectancy = _first_non_na_text(memory.get("average_r"), memory.get("historical_expectancy"))
        return f"Memory {confidence} exp {_r_text(expectancy)} sample {sample}"

    summary = symbol_result.historical_match_summary
    if not isinstance(summary, Mapping) or not summary:
        return NA
    metrics = summary.get("expectancy_metrics")
    if not isinstance(metrics, Mapping):
        metrics = {}
    label = _display(summary.get("confidence_label"))
    sample = _first_non_na_text(summary.get("matching_sample_size"), metrics.get("fills"))
    expectancy = _display(metrics.get("expectancy"))
    return f"Historical {label} exp {_r_text(expectancy)} sample {sample}"


def _r_text(value: object) -> str:
    text = _display(value)
    return text if text == NA else f"{text} R"


def _percent_text(value: object) -> str:
    text = _display(value)
    return text if text == NA else f"{text}%"


def _quality_evaluated(quality: SetupQualityResult) -> bool:
    return bool(getattr(quality, "is_evaluated", False))


def _passed_lines(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    display: SymbolDisplay,
) -> list[str]:
    lines = [f"{BULLET} {_passed_line_text(item)}" for item in display.progress_items if item.passed]
    if symbol_result.derivatives_score != NA:
        lines.append(f"{BULLET} Context score: {_display(symbol_result.derivatives_score)}")
    if not lines:
        return [f"{BULLET} None yet."]
    return lines


def _passed_line_text(item: ProgressItem) -> str:
    if item.label.endswith("sweep"):
        return f"{item.label} detected"
    if item.label.endswith("BOS/CHoCH"):
        return f"{item.label} confirmed"
    if item.label == "Pullback zone":
        return "Pullback zone valid"
    return "RR valid"


def _failed_lines(display: SymbolDisplay) -> list[str]:
    lines = [f"{BULLET} {item.label}: failed" for item in display.progress_items if not item.passed and item.evaluated]
    if display.failed_gate != NA:
        lines.append(f"{BULLET} Gate: {display.failed_gate}")
    if not lines:
        return [f"{BULLET} None."]
    return lines


def _progress_lines(display: SymbolDisplay) -> list[str]:
    lines: list[str] = []
    for item in display.progress_items:
        marker = CHECK if item.passed else (CROSS if item.evaluated else WHITE_CIRCLE)
        suffix = "" if item.passed or item.evaluated else " N/A"
        lines.append(f"{marker} {item.label}{suffix}")
    return lines


def _diagnostic_lines(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> list[str]:
    return [
        f"{BULLET} Scanner status: {symbol_result.status.value}",
        f"{BULLET} Rejection stage: {_display(symbol_result.rejection_stage)}",
        f"{BULLET} Failed gate: {_failed_gate(symbol_result, diagnostics)}",
        f"{BULLET} Passed gates: {_sequence_text(diagnostics.get('gates_passed'))}",
        f"{BULLET} Failed gates: {_sequence_text(diagnostics.get('gates_failed'))}",
        f"{BULLET} Hard rejections: {_sequence_text(diagnostics.get('hard_rejection_reasons'))}",
        f"{BULLET} Missing data: {_sequence_text(symbol_result.strategy_missing_data or symbol_result.missing_data)}",
        f"{BULLET} Unverified data: {_sequence_text(symbol_result.strategy_unverified_data or symbol_result.unverified_data)}",
        f"{BULLET} Regime warnings: {_sequence_text(symbol_result.regime_warnings)}",
    ]


def _card_failed_gate_lines(display: SymbolDisplay) -> tuple[str, ...]:
    return (f"{BULLET} Failed gate: {display.failed_gate}",)


def _mode_summary(symbol_result: ScannerSymbolResult) -> str:
    valid_modes = _ordered_modes(symbol_result.valid_strategy_modes)
    rejected_modes = _ordered_modes(symbol_result.rejected_strategy_modes)
    if valid_modes and rejected_modes:
        return f"valid {', '.join(valid_modes)}; rejected {', '.join(rejected_modes)}"
    if valid_modes:
        return f"valid {', '.join(valid_modes)}"
    if rejected_modes:
        return f"rejected {', '.join(rejected_modes)}"
    if symbol_result.strategy_results:
        return ", ".join(_ordered_modes(symbol_result.strategy_results.keys()))
    return NA


def _ordered_modes(values: Sequence[str] | Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        values = tuple(values) if hasattr(values, "__iter__") else ()
    cleaned = tuple(str(value) for value in values if str(value))
    return tuple(sorted(cleaned, key=lambda value: (MODE_ORDER.index(value) if value in MODE_ORDER else 99, value)))


def _execution_summary(diagnostics: Mapping[str, Any]) -> str:
    execution_tf = _display(diagnostics.get("execution_timeframe"))
    confirmation_tf = _display(diagnostics.get("confirmation_timeframe"))
    if execution_tf == NA:
        execution_tf = "15m"
    if confirmation_tf == NA:
        confirmation_tf = "5m"
    return " | ".join(
        (
            f"2D HTF {_title_value(diagnostics.get('htf_2d_trend'))}",
            f"12H Bias {_title_value(diagnostics.get('mtf_12h_trend'))}",
            f"{execution_tf} sweep {_summary_status(diagnostics.get('execution_sweep_status'))}",
            f"{confirmation_tf} BOS/CHoCH {_summary_status(diagnostics.get('confirmation_structure_shift_status'))}",
        )
    )


def _summary_status(value: object) -> str:
    text = _display(value)
    if text == "not_evaluated":
        return "not evaluated"
    return text


def _setup_bias(diagnostics: Mapping[str, Any]) -> str:
    bias = _display(diagnostics.get("bias")).lower()
    if bias in ("long", "short"):
        return bias
    for key in ("sweep_diagnostics", "bos_choch_diagnostics"):
        text = _display(diagnostics.get(key)).lower()
        if "bullish" in text:
            return "long"
        if "bearish" in text:
            return "short"
    return NA


def _expected_trend(bias: str) -> str:
    if bias == "long":
        return "bullish"
    if bias == "short":
        return "bearish"
    return NA


def _trend_alignment_points(value: object, expected_trend: str, maximum: Decimal) -> Decimal:
    trend = _display(value).lower()
    if trend == NA.lower() or expected_trend == NA:
        return Decimal("0")
    if trend == expected_trend:
        return maximum
    if trend in ("neutral", "range", "ranging", "sideways"):
        return maximum * Decimal("0.5")
    return Decimal("0")


def _rr_readiness_points(
    diagnostics: Mapping[str, Any],
    failed_gate: str,
) -> Decimal:
    gates_passed = set(_sequence_values(diagnostics.get("gates_passed")))
    gates_failed = set(_sequence_values(diagnostics.get("gates_failed")))
    rr_failed = failed_gate in RR_FAIL_GATES or bool(gates_failed & RR_FAIL_GATES)
    rr = _numeric(diagnostics.get("rr_to_tp2"))
    if rr == 0 and "rr" in gates_passed and not rr_failed:
        return Decimal("15")
    if rr <= 0:
        return Decimal("0")
    required = _required_rr(diagnostics, failed_gate)
    ratio = min(rr / required, Decimal("1"))
    return ratio * Decimal("15")


def _required_rr(diagnostics: Mapping[str, Any], failed_gate: str) -> Decimal:
    effective_minimum_rr = _numeric(diagnostics.get("effective_minimum_rr"))
    if effective_minimum_rr > 0:
        return effective_minimum_rr
    mode = _display(diagnostics.get("mode")).lower()
    resolved_mode = (
        "challenge"
        if failed_gate.startswith("challenge_")
        else mode if mode in {"challenge", "scalp", "swing"} else "swing"
    )
    return hard_mode_minimum_rr(resolved_mode)


def _derivatives_readiness_points(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
) -> Decimal:
    if _severe_derivatives_conflict(symbol_result, diagnostics, _failed_gate(symbol_result, diagnostics)):
        return Decimal("0")
    support = _display(diagnostics.get("derivatives_supports_trade"))
    if support == "True":
        score = Decimal("6")
    elif support == "False":
        score = Decimal("2")
    else:
        score = Decimal("5")

    context_score = _numeric(symbol_result.derivatives_score)
    if context_score == 0:
        context_score = _numeric(diagnostics.get("derivatives_score"))
    if context_score >= Decimal("70"):
        score += Decimal("2")
    elif context_score >= Decimal("40"):
        score += Decimal("1")
    return _bounded_decimal(score, Decimal("8"))


def _volume_profile_readiness_points(symbol_result: ScannerSymbolResult) -> Decimal:
    score = Decimal("0")
    if symbol_result.poc != NA:
        score += Decimal("3")
    if symbol_result.value_area_high != NA and symbol_result.value_area_low != NA:
        score += Decimal("2")
    return score


def _data_certainty_points(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    failed_gate: str,
) -> Decimal:
    if _critical_data_issue(symbol_result, diagnostics, failed_gate):
        return Decimal("0")

    score = Decimal("7")
    missing_items = (
        *symbol_result.missing_data,
        *symbol_result.strategy_missing_data,
        *symbol_result.derivatives_missing_data,
        *_sequence_values(diagnostics.get("missing_data")),
    )
    unverified_items = (
        *symbol_result.unverified_data,
        *symbol_result.strategy_unverified_data,
        *symbol_result.derivatives_unverified_data,
        *_sequence_values(diagnostics.get("unverified_data")),
    )
    warning_items = (*symbol_result.derivatives_warnings, *symbol_result.volume_profile_warnings)
    for item in _unique_strings(missing_items):
        score -= Decimal("0.5") if _optional_missing_item(item) else Decimal("1")
    score -= Decimal(len(_unique_strings(unverified_items)) * 2)
    score -= Decimal(min(4, len(_unique_strings(warning_items)))) * Decimal("0.5")
    return _bounded_decimal(score, Decimal("7"))


def _critical_data_issue(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    failed_gate: str,
) -> bool:
    if symbol_result.error_message:
        return True
    if diagnostics.get("error"):
        return True
    if failed_gate in DATA_INCOMPLETE_GATES or failed_gate in {"current_price", "scanner_error"}:
        return True
    critical_prefixes = (
        "candles:",
        "candles_15m:",
        "candles_5m:",
        "execution_candles:",
        "confirmation_candles:",
        "current_price:",
        "latest_close:",
    )
    values = (
        *symbol_result.missing_data,
        *symbol_result.strategy_missing_data,
        *_sequence_values(diagnostics.get("missing_data")),
    )
    return any(str(item).startswith(critical_prefixes) for item in values)


def _optional_missing_item(value: str) -> bool:
    return value.startswith(("cvd:", "liquidation_data:", "btc", "event", "sector"))


def _severe_derivatives_conflict(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    failed_gate: str,
) -> bool:
    if _severe_derivatives_conflict_from_diagnostics(diagnostics, failed_gate):
        return True
    support = _display(diagnostics.get("derivatives_supports_trade"))
    funding_status = _first_non_na_text(
        symbol_result.funding_status,
        _nested_value(diagnostics, "funding_context", "funding_status"),
    )
    crowding = _first_non_na_text(symbol_result.crowding_risk, diagnostics.get("crowding_risk"))
    if support == "False" and crowding == "high":
        return True
    if support == "False" and funding_status in ("extreme_positive", "extreme_negative"):
        return True
    return False


def _severe_derivatives_conflict_from_diagnostics(
    diagnostics: Mapping[str, Any],
    failed_gate: str,
) -> bool:
    if failed_gate in {"derivatives_conflict", "funding_oi_guard"}:
        return True
    if _display(diagnostics.get("derivatives_conflict_reason")) != NA:
        return True
    support = _display(diagnostics.get("derivatives_supports_trade"))
    funding_status = _display(_nested_value(diagnostics, "funding_context", "funding_status"))
    crowding = _display(diagnostics.get("crowding_risk"))
    if support == "False" and crowding == "high":
        return True
    if support == "False" and funding_status in ("extreme_positive", "extreme_negative"):
        return True
    return False


def _nested_value(diagnostics: Mapping[str, Any], key: str, nested_key: str) -> object:
    value = diagnostics.get(key)
    if isinstance(value, Mapping):
        return value.get(nested_key, NA)
    return NA


def _first_non_na_text(*values: object) -> str:
    for value in values:
        text = _display(value)
        if text != NA:
            return text
    return NA


def _bounded_decimal(value: Decimal, maximum: Decimal) -> Decimal:
    return min(maximum, max(Decimal("0"), value))


def _bounded_int(value: Decimal) -> int:
    return int(_bounded_decimal(value, Decimal("100")).to_integral_value(rounding="ROUND_HALF_UP"))


def _display_priority_score(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    display_status: DisplayStatus,
    failed_gate: str,
) -> int:
    display_bucket = _display_bucket(display_status)
    score = (len(BUCKET_ORDER) - 1 - BUCKET_ORDER[display_bucket]) * 100_000
    if _trade_idea_created(symbol_result):
        score += 20_000
    score += int(_best_setup_score(symbol_result, diagnostics) * Decimal("100"))
    score += min(int(_best_rr(symbol_result, diagnostics) * Decimal("100")), 2_000)
    score += int(_numeric(symbol_result.technical_score) * Decimal("20"))
    score += int(_numeric(symbol_result.derivatives_score) * Decimal("10"))
    score += _derivatives_support_score(diagnostics)
    score += max(0, 20 - _failed_gate_count(diagnostics, failed_gate)) * 50
    score += _stage_rank(failed_gate, display_status) * 500
    return score


def _best_setup_score(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> Decimal:
    score_result = symbol_result.score_result
    return max(
        _numeric(diagnostics.get("trust_percentage")),
        _numeric(diagnostics.get("trust_score")),
        _numeric(getattr(score_result, "total_score", NA) if score_result is not None else NA),
        _numeric(symbol_result.technical_score),
    )


def _best_rr(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> Decimal:
    risk_decision = symbol_result.risk_decision
    return max(
        _numeric(diagnostics.get("rr_to_tp2")),
        _numeric(getattr(risk_decision, "best_risk_reward_ratio", NA) if risk_decision is not None else NA),
    )


def _derivatives_support_score(diagnostics: Mapping[str, Any]) -> int:
    support = _display(diagnostics.get("derivatives_supports_trade"))
    conflict = _display(diagnostics.get("derivatives_conflict_reason"))
    score = 0
    if support == "True":
        score += 500
    elif support == "False":
        score -= 750
    if conflict != NA:
        score -= 750
    return score


def _failed_gate_count(diagnostics: Mapping[str, Any], failed_gate: str) -> int:
    gates_failed = _sequence_values(diagnostics.get("gates_failed"))
    if gates_failed:
        return len(gates_failed)
    return 1 if failed_gate != NA else 0


def _failed_stage(
    symbol_result: ScannerSymbolResult,
    failed_gate: str,
    display_status: DisplayStatus,
) -> str:
    if display_status == "valid_setup":
        return NA
    if failed_gate != NA:
        return _stage_name(failed_gate, display_status)
    if symbol_result.rejection_stage != NA:
        return _display(symbol_result.rejection_stage)
    return NA


def _stage_rank(failed_gate: str, display_status: DisplayStatus) -> int:
    return STAGE_ORDER.get(_stage_name(failed_gate, display_status), 0)


def _stage_name(failed_gate: str, display_status: DisplayStatus) -> str:
    if display_status == "valid_setup":
        return "valid"
    if display_status == "data_issue" or failed_gate in DATA_INCOMPLETE_GATES:
        return "data"
    if failed_gate in ("missing_confirmed_sweep",):
        return "sweep"
    if failed_gate in ("missing_confirmation_structure_shift",):
        return "structure"
    if failed_gate in ("no_ob_or_fvg_zone",):
        return "ob_fvg"
    if failed_gate in PULLBACK_FAIL_GATES or failed_gate in ("missing_displacement_impulse", "missing_stop"):
        return "pullback"
    if failed_gate in RR_FAIL_GATES:
        return "rr"
    if failed_gate == "target_integrity":
        return "target_integrity"
    if failed_gate in FINAL_CONFLUENCE_FAIL_GATES:
        return "final"
    return "data" if failed_gate == NA else "pullback"


def _numeric(value: object) -> Decimal:
    text = _display(value)
    if text == NA:
        return Decimal("0")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal("0")


def _volume_profile_text(symbol_result: ScannerSymbolResult) -> str:
    return (
        f"Volume Profile: POC {_price_display(symbol_result.poc)} | "
        f"VAH {_price_display(symbol_result.value_area_high)} | "
        f"VAL {_price_display(symbol_result.value_area_low)}"
    )


def _derivatives_context_text(symbol_result: ScannerSymbolResult) -> str:
    return (
        "Derivatives: "
        f"Funding {_funding_summary(symbol_result)} | "
        f"OI {_title_value(symbol_result.oi_direction).lower() if symbol_result.oi_direction != NA else NA} | "
        f"Crowding {_title_value(symbol_result.crowding_risk).lower() if symbol_result.crowding_risk != NA else NA}"
    )


def _funding_summary(symbol_result: ScannerSymbolResult) -> str:
    status = _display(symbol_result.funding_status)
    if status != NA:
        return _funding_status_display(status)
    direction = _display(symbol_result.funding_direction)
    if direction != NA:
        return direction
    rate = symbol_result.funding_rate
    if isinstance(rate, Decimal):
        if rate > 0:
            return "positive"
        if rate < 0:
            return "negative"
        return "neutral"
    return NA


def _funding_status_display(status: str) -> str:
    if status in ("elevated_positive", "elevated_negative"):
        return "elevated"
    if status in ("extreme_positive", "extreme_negative"):
        return "extreme"
    return status


def _optional_data_warning_count(result: ScannerRunResult) -> int:
    return sum(len(symbol_result.derivatives_warnings) for symbol_result in result.results)


def _performance_memory_dashboard_lines(result: ScannerRunResult) -> tuple[str, ...]:
    summary = result.performance_memory_summary
    if not isinstance(summary, Mapping) or not summary:
        return ("Performance Memory", "Performance memory confidence too low.")
    if summary.get("enabled") is False:
        return ("Performance Memory", "Performance memory disabled.")
    return (
        "Performance Memory",
        f"Samples: {_display(summary.get('total_historical_samples'))}",
        f"Confidence: {_display(summary.get('memory_confidence_level'))}",
        f"Avg expectancy: {_summary_expectancy(result)}",
        f"Strongest regime: {_display(summary.get('best_regime_historically'))}",
        f"Weakest regime: {_display(summary.get('worst_regime_historically'))}",
        f"Historical TP1: {_summary_rate(result, 'tp1_rate')}",
        f"Historical TP2: {_summary_rate(result, 'tp2_rate')}",
    )


def _summary_expectancy(result: ScannerRunResult) -> str:
    values = [
        _numeric(memory.get("average_r"))
        for memory in (symbol_result.performance_memory for symbol_result in result.results)
        if isinstance(memory, Mapping) and _display(memory.get("average_r")) != NA
    ]
    if not values:
        return NA
    average = sum(values, Decimal("0")) / Decimal(len(values))
    sign = "+" if average > 0 else ""
    return f"{sign}{_display(average.quantize(Decimal('0.1')))}R"


def _summary_rate(result: ScannerRunResult, key: str) -> str:
    values = [
        _numeric(memory.get(key))
        for memory in (symbol_result.performance_memory for symbol_result in result.results)
        if isinstance(memory, Mapping) and _display(memory.get(key)) != NA
    ]
    if not values:
        return NA
    average = sum(values, Decimal("0")) / Decimal(len(values))
    return _percent_text(average.quantize(Decimal("0.1")))


def _market_regime_lines(result: ScannerRunResult) -> tuple[str, ...]:
    regime = result.market_regime
    adjustment = regime.adjustment
    if not regime.enabled:
        return ("Market Climate", "Market climate filter disabled")
    return (
        "Market Climate",
        f"State: {regime.state.value}",
        f"Confidence: {regime.confidence_score}",
        f"Risk: {regime.risk_level.value}",
        f"Challenge compatibility: {_compatibility_text(regime, 'challenge')}",
        f"Swing compatibility: {_compatibility_text(regime, 'swing')}",
        f"Scalp compatibility: {_compatibility_text(regime, 'scalp')}",
        (
            "Trade permission: "
            f"Scalp {_yes_no(adjustment.allow_scalps)} | "
            f"Swing {_yes_no(adjustment.allow_swings)} | "
            f"Challenge {_yes_no(adjustment.allow_challenge)}"
        ),
        f"Risk multiplier: {_display(adjustment.risk_multiplier)}x",
        f"Notes: {_notes_text((*regime.environment_notes, adjustment.explanation))}",
    )


def _symbol_regime_warning_lines(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    warning = _first_regime_warning(symbol_result)
    if warning == NA:
        return ()
    return (f"{BULLET} Regime: {warning}",)


def _lifecycle_compact(symbol_result: ScannerSymbolResult) -> str:
    record = symbol_result.lifecycle_state
    if record is None:
        return NA
    return f"Lifecycle {record.current_state.value}"


def _lifecycle_card_lines(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    record = symbol_result.lifecycle_state
    if record is None:
        return ()
    transition = symbol_result.lifecycle_transition
    from_state = (
        transition.from_state.value
        if transition is not None and transition.from_state is not None
        else _state_value(record.previous_state)
    )
    to_state = transition.to_state.value if transition is not None else record.current_state.value
    reason = transition.reason.value if transition is not None else NA
    return (
        "",
        "Lifecycle:",
        f"- State: {record.current_state.value}",
        f"- Previous: {_state_value(record.previous_state)}",
        f"- Transition: {from_state} {ARROW} {to_state}",
        f"- Reason: {reason}",
        f"- First seen: {record.first_seen_at}",
        f"- Last updated: {record.last_seen_at}",
    )


def _state_value(value: object) -> str:
    if value is None:
        return NA
    return _display(getattr(value, "value", value))


def _target_integrity_blocked(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
) -> bool:
    if _failed_gate(symbol_result, diagnostics) == "target_integrity":
        return True
    return _display(diagnostics.get("target_integrity_status")).lower() == "blocked"


def _target_integrity_reason(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
) -> str:
    for value in (
        diagnostics.get("target_integrity_reason"),
        diagnostics.get("target_integrity_warning"),
        _target_rr_reason(symbol_result, diagnostics),
    ):
        text = _display(value)
        if text != NA:
            return text
    return "Target integrity guard blocked alert creation."


def _target_rr_reason(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
) -> str:
    intelligence = _target_intelligence(symbol_result, diagnostics)
    if intelligence is None:
        return NA
    for value in (intelligence.rr_compression_reason, intelligence.next_target_condition):
        text = _display(value)
        if text != NA:
            return text
    return NA


def _lifecycle_integrity_fields(symbol_result: ScannerSymbolResult, display: SymbolDisplay) -> dict[str, Any]:
    lifecycle = symbol_result.lifecycle_state
    if lifecycle is None:
        return {
            "lifecycle_integrity_status": NA,
            "lifecycle_integrity_warning": NA,
            "current_scan_gate_valid": True,
        }
    state = _state_value(lifecycle.current_state)
    degraded_display = display.display_status in {"no_setup", "near_miss"} or (
        symbol_result.status
        in {
            ScannerPipelineStatus.REJECTED_BY_SCORING,
            ScannerPipelineStatus.REJECTED_BY_TECHNICAL,
            ScannerPipelineStatus.REJECTED_BY_RISK,
            ScannerPipelineStatus.REJECTED_BY_DERIVATIVES,
            ScannerPipelineStatus.REJECTED_BY_REGIME,
        }
    )
    degraded_stage = display.failed_stage in {"pullback", "structure", "ob_fvg", "rr", "scoring", "target_integrity"}
    if state in {"CONFIRMED", "EXECUTING", "TRIGGERED"} and (degraded_display or degraded_stage):
        return {
            "lifecycle_integrity_status": "STALE_OR_DEGRADED",
            "lifecycle_integrity_warning": (
                f"Lifecycle state {state} conflicts with current scan status "
                f"{display.display_status} at stage {display.failed_stage}; current gates are not valid."
            ),
            "current_scan_gate_valid": False,
        }
    return {
        "lifecycle_integrity_status": "OK",
        "lifecycle_integrity_warning": NA,
        "current_scan_gate_valid": True,
    }


def _first_regime_warning(symbol_result: ScannerSymbolResult) -> str:
    return symbol_result.regime_warnings[0] if symbol_result.regime_warnings else NA


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _compatibility_text(regime: Any, mode: str) -> str:
    item = regime.compatibility_scores.get(mode)
    if item is None:
        return NA
    return f"{item.label} ({item.score})"


def _notes_text(values: Sequence[str]) -> str:
    notes = []
    for value in values:
        text = _display(value)
        if text != NA and text not in notes:
            notes.append(text)
    return "; ".join(notes) if notes else NA


def _failed_gate(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    failed_gate = _display(diagnostics.get("first_failed_gate"))
    if failed_gate != NA:
        return failed_gate
    gates_failed = _sequence_values(diagnostics.get("gates_failed"))
    if gates_failed:
        return gates_failed[0]
    if symbol_result.rejection_stage != NA:
        return _display(symbol_result.rejection_stage)
    return NA


def _compact_result_text(display_status: DisplayStatus) -> str:
    if display_status == "valid_setup":
        return "Trade idea created."
    if display_status == "data_issue":
        return "No valid setup. Data incomplete."
    return "No valid setup."


def _strategy_title(value: str) -> str:
    if value == NA:
        return NA
    return value.replace("_", " ").title()


def _timeframe_label(value: object) -> str:
    text = _display(value)
    if text == NA:
        return NA
    lowered = text.lower()
    if lowered.endswith("d") or lowered.endswith("h"):
        return lowered.upper()
    return lowered


def _title_value(value: object) -> str:
    text = _display(value)
    if text == NA or text == "Unverified":
        return text
    return text.replace("_", " ").title()


def _price_display(value: object) -> str:
    text = _display(value)
    if text == NA:
        return NA
    try:
        decimal = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    normalized = format(decimal, "f")
    if "." in normalized:
        whole, fractional = normalized.split(".", 1)
        fractional = fractional.rstrip("0")
    else:
        whole, fractional = normalized, ""
    sign = "-" if whole.startswith("-") else ""
    whole = whole.lstrip("-")
    grouped = f"{int(whole):,}" if whole else "0"
    return f"{sign}{grouped}.{fractional}" if fractional else f"{sign}{grouped}"


def _seconds_text(value: object) -> str:
    text = _display(value)
    if text == NA:
        return NA
    try:
        seconds = Decimal(text)
    except (InvalidOperation, ValueError):
        return text
    if seconds == 0:
        return "0s"
    if seconds < Decimal("1"):
        return f"{seconds:.3f}".rstrip("0").rstrip(".") + "s"
    return f"{seconds:.1f}".rstrip("0").rstrip(".") + "s"


def _clean_diagnostic_sentence(value: object) -> str:
    text = _display(value)
    if text == NA:
        return NA
    return text.removeprefix("failed: ").removeprefix("passed: ")


def _sequence_text(values: Any) -> str:
    items = _sequence_values(values)
    return ", ".join(items) if items else NA


def _sequence_values(values: Any) -> tuple[str, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
        return ()
    return tuple(_display(value) for value in values if _display(value) != NA)


def _unique_strings(values: Sequence[str]) -> tuple[str, ...]:
    output: list[str] = []
    for value in values:
        text = _display(value)
        if text != NA and text not in output:
            output.append(text)
    return tuple(output)


def _display(value: object) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


__all__ = [
    "DEFAULT_MAX_DISPLAY_RESULTS",
    "DisplayBucket",
    "DisplayMode",
    "DisplayStatus",
    "FOOTER",
    "ReadinessLabel",
    "RankedSymbolDisplay",
    "SetupReadiness",
    "SymbolDisplay",
    "build_symbol_display",
    "display_fields",
    "filter_ranked_results",
    "format_pullback_intelligence_block",
    "format_scan_dashboard",
    "format_symbol_card",
    "format_symbol_compact_line",
    "format_target_intelligence_block",
    "rank_scan_results",
    "representative_strategy_diagnostics",
]
