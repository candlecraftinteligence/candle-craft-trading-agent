from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.data.dtos import NA
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunResult, ScannerSymbolResult

DisplayMode = Literal["compact", "normal", "full"]
DisplayBucket = Literal["valid", "near_miss", "no_setup", "data_issue"]
DisplayStatus = Literal["valid_setup", "near_miss", "no_setup", "data_issue", "scan_error"]

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
}
LATE_FAILURE_GATES = (
    PULLBACK_FAIL_GATES
    | RR_FAIL_GATES
    | FINAL_CONFLUENCE_FAIL_GATES
    | {"missing_displacement_impulse", "missing_stop"}
)
STAGE_ORDER = {
    "data": 0,
    "sweep": 1,
    "structure": 2,
    "pullback": 3,
    "ob_fvg": 4,
    "rr": 5,
    "final": 6,
    "valid": 7,
}


@dataclass(frozen=True)
class ProgressItem:
    label: str
    passed: bool
    evaluated: bool = True


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
        action_label=_action_label(display_bucket),
        progress_items=progress_items,
        failed_gate=failed_gate,
    )


def display_fields(symbol_result: ScannerSymbolResult, *, display_rank: int | None = None) -> dict[str, Any]:
    display = build_symbol_display(symbol_result)
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
                BUCKET_ORDER[item.display.display_bucket],
                -item.display.display_priority_score,
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
            f"Completed: {completed}",
            f"No setup: {counts['no_setup']}",
            f"Near miss: {counts['near_miss']}",
            f"Trade ideas: {counts['valid']}",
            f"Scan errors: {scan_errors}",
            f"Data warnings: {data_warning_count} optional endpoint warnings.",
            f"Cache hits: {int(cache_stats.get('hits', 0) or 0)}",
            f"Cache misses: {int(cache_stats.get('misses', 0) or 0)}",
            "",
            f"{GREEN_CIRCLE} Valid setups: {counts['valid']}",
            f"{YELLOW_CIRCLE} Near misses: {counts['near_miss']}",
            f"{WHITE_CIRCLE} Hidden rejected/no-setup symbols: {hidden_no_setups}",
            f"{RED_CIRCLE} Data issues: {counts['data_issue']}",
        )
    )


def format_symbol_compact_line(symbol_result: ScannerSymbolResult, *, rank: int | None = None) -> str:
    diagnostics = representative_strategy_diagnostics(symbol_result)
    display = build_symbol_display(symbol_result)
    status_text = display.display_status_label.split(" ", 1)[1]
    rank_text = f"#{rank} " if rank is not None else ""
    parts = [
        f"{rank_text}{display.display_status_label.split(' ', 1)[0]} {symbol_result.symbol} {DASH} {status_text}",
        f"Modes {_mode_summary(symbol_result)}",
        _execution_summary(diagnostics),
        f"Progress {display.setup_progress_passed}/{display.setup_progress_total}",
    ]
    if display.display_bucket != "valid" and display.failed_gate != NA:
        parts.append(f"Gate: {display.failed_gate}")
    parts.append(display.display_reason)
    parts.append(display.action_label)
    return " | ".join(parts)


def format_symbol_card(
    symbol_result: ScannerSymbolResult,
    *,
    include_diagnostics: bool = False,
    rank: int | None = None,
) -> str:
    diagnostics = representative_strategy_diagnostics(symbol_result)
    display = build_symbol_display(symbol_result)
    icon, status_text = display.display_status_label.split(" ", 1)
    rank_text = f"#{rank} " if rank is not None else ""
    lines = [
        CARD_RULE,
        f"{rank_text}{icon} {symbol_result.symbol} {DASH} {status_text}",
        CARD_RULE,
        "",
        f"{BULLET} Bucket: {display.display_bucket_label}",
        f"{BULLET} Mode(s): {_mode_summary(symbol_result)}",
        f"{BULLET} HTF/Bias/Execution: {_execution_summary(diagnostics)}",
        *(_card_failed_gate_lines(display)),
        "",
        f"{PIN} Context",
        f"{BULLET} 2D HTF: {_title_value(diagnostics.get('htf_2d_trend'))}",
        f"{BULLET} 12H Bias: {_title_value(diagnostics.get('mtf_12h_trend'))}",
        f"{BULLET} {_volume_profile_text(symbol_result)}",
        f"{BULLET} {_derivatives_context_text(symbol_result)}",
        "",
        f"{CHECK} Passed",
        *_passed_lines(symbol_result, diagnostics, display),
        "",
        f"{CROSS} Failed",
        *_failed_lines(display),
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


def representative_strategy_diagnostics(symbol_result: ScannerSymbolResult) -> Mapping[str, Any]:
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
    if not _core_checks_passed(progress_items):
        return False
    if failed_gate == NA or failed_gate in EARLY_CORE_GATES:
        return False
    gates_failed = set(_sequence_values(diagnostics.get("gates_failed")))
    if failed_gate not in LATE_FAILURE_GATES and not bool(gates_failed & LATE_FAILURE_GATES):
        return False
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
    if failed_gate in ("missing_confirmation_structure_shift", "missing_confirmation_candles"):
        reason = _display(diagnostics.get("confirmation_bos_choch_reason"))
        if reason != NA:
            return reason

    for key in (
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


def _action_label(display_bucket: DisplayBucket) -> str:
    if display_bucket == "valid":
        return "Trade idea created"
    if display_bucket == "near_miss":
        return "Watchlist only"
    if display_bucket == "data_issue":
        return "Data insufficient"
    return "Rejected"


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
    ]


def _card_failed_gate_lines(display: SymbolDisplay) -> tuple[str, ...]:
    if display.display_bucket == "valid" or display.failed_gate == NA:
        return ()
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
    "RankedSymbolDisplay",
    "SymbolDisplay",
    "build_symbol_display",
    "display_fields",
    "filter_ranked_results",
    "format_scan_dashboard",
    "format_symbol_card",
    "format_symbol_compact_line",
    "rank_scan_results",
    "representative_strategy_diagnostics",
]
