from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.data.dtos import NA
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunResult, ScannerSymbolResult

DisplayMode = Literal["compact", "normal", "full"]
DisplayStatus = Literal["valid_setup", "near_miss", "no_setup", "data_incomplete"]

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
    "rr_below_minimum",
    "challenge_rr_below_3",
    "rr_too_low",
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
    setup_progress_total: int
    setup_progress_passed: int
    passed_checks: tuple[str, ...]
    failed_checks: tuple[str, ...]
    short_reason: str
    action_label: str
    progress_items: tuple[ProgressItem, ...]
    failed_gate: str


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
    setup_progress_passed = sum(1 for item in progress_items if item.passed)
    return SymbolDisplay(
        display_status=display_status,
        display_status_label=_display_status_label(display_status),
        setup_progress_total=SETUP_PROGRESS_TOTAL,
        setup_progress_passed=setup_progress_passed,
        passed_checks=passed_checks,
        failed_checks=failed_checks,
        short_reason=_short_reason(symbol_result, diagnostics, display_status),
        action_label=_action_label(display_status),
        progress_items=progress_items,
        failed_gate=failed_gate,
    )


def display_fields(symbol_result: ScannerSymbolResult) -> dict[str, Any]:
    display = build_symbol_display(symbol_result)
    return {
        "display_status": display.display_status,
        "display_status_label": display.display_status_label,
        "setup_progress_total": display.setup_progress_total,
        "setup_progress_passed": display.setup_progress_passed,
        "passed_checks": list(display.passed_checks),
        "failed_checks": list(display.failed_checks),
        "short_reason": display.short_reason,
        "action_label": display.action_label,
    }


def format_scan_dashboard(result: ScannerRunResult) -> str:
    counts = {
        "valid_setup": 0,
        "near_miss": 0,
        "no_setup": 0,
        "data_incomplete": 0,
    }
    for symbol_result in result.results:
        counts[build_symbol_display(symbol_result).display_status] += 1

    config = result.config
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
            "",
            f"{GREEN_CIRCLE} Valid setups: {counts['valid_setup']}",
            f"{YELLOW_CIRCLE} Near misses: {counts['near_miss']}",
            f"{RED_CIRCLE} No setups: {counts['no_setup']}",
            f"{WHITE_CIRCLE} Data incomplete: {counts['data_incomplete']}",
        )
    )


def format_symbol_compact_line(symbol_result: ScannerSymbolResult) -> str:
    display = build_symbol_display(symbol_result)
    status_text = display.display_status_label.split(" ", 1)[1]
    parts = [
        f"{display.display_status_label.split(' ', 1)[0]} {symbol_result.symbol} {DASH} {status_text}",
        f"Progress {display.setup_progress_passed}/{display.setup_progress_total}",
    ]
    if display.failed_checks:
        parts.append(f"Failed: {', '.join(display.failed_checks)}")
    if display.failed_gate != NA:
        parts.append(f"Gate: {display.failed_gate}")
    parts.append(_compact_result_text(display.display_status))
    return " | ".join(parts)


def format_symbol_card(symbol_result: ScannerSymbolResult, *, include_diagnostics: bool = False) -> str:
    diagnostics = representative_strategy_diagnostics(symbol_result)
    display = build_symbol_display(symbol_result)
    icon, status_text = display.display_status_label.split(" ", 1)
    lines = [
        CARD_RULE,
        f"{icon} {symbol_result.symbol} {DASH} {status_text}",
        CARD_RULE,
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
    if _data_incomplete(symbol_result, diagnostics, failed_gate):
        return "data_incomplete"
    if _core_checks_passed(progress_items) and failed_gate not in (NA, *EARLY_CORE_GATES):
        return "near_miss"
    return "no_setup"


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
        "no_setup": f"{RED_CIRCLE} NO SETUP",
        "data_incomplete": f"{WHITE_CIRCLE} DATA INCOMPLETE",
    }
    return labels[display_status]


def _short_reason(
    symbol_result: ScannerSymbolResult,
    diagnostics: Mapping[str, Any],
    display_status: DisplayStatus,
) -> str:
    if display_status == "valid_setup":
        return "Trade idea created after scanner gates passed."

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
    if display_status == "data_incomplete":
        return "Required market data is missing or unavailable."
    return "No valid setup."


def _action_label(display_status: DisplayStatus) -> str:
    if display_status == "valid_setup":
        return "Trade idea created. Review invalidation and risk warning."
    if display_status == "data_incomplete":
        return "No trade idea. Required data is incomplete."
    return "No trade idea, no alert, no journal entry."


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
    if display_status == "data_incomplete":
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
    "DisplayMode",
    "DisplayStatus",
    "FOOTER",
    "SymbolDisplay",
    "build_symbol_display",
    "display_fields",
    "format_scan_dashboard",
    "format_symbol_card",
    "format_symbol_compact_line",
    "representative_strategy_diagnostics",
]
