from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, Literal

from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display
from app.pipeline.scanner_runner import ScannerSymbolResult
from app.strategies.liquidity_grab_pullback import LiquidityGrabMode, LiquidityGrabResult, LiquidityGrabSetup

DiagnosticsLevel = Literal["summary", "normal", "full"]

BULLET = "\u2022"
DASH = "\u2014"
GREEN_CIRCLE = "\U0001F7E2"
POINT_RIGHT = "\U0001F449"
PIN = "\U0001F4CD"
BRAIN = "\U0001F9E0"
TARGET = "\U0001F3AF"
CHECK = "\u2705"
CROSS = "\u274c"
SWORDS = "\u2694\ufe0f"
FOOTER = f"{SWORDS} Candle Craft | Signal. Structure. Execution."
RISK_WARNING_FALLBACK = (
    "This is not financial advice. Pullback ideas are conditional and must be invalidated at the stop."
)
MODE_ORDER = ("challenge", "swing", "scalp")


def format_telegram_strategy_output(
    symbol_result: ScannerSymbolResult,
    *,
    diagnostics_level: DiagnosticsLevel = "normal",
    compact: bool = False,
) -> str:
    """Format scanner strategy results as Telegram-ready plain text.

    This function is intentionally side-effect free. It only reads scanner output
    and returns text; it does not call alert agents, Telegram transports, exchange
    clients, or any execution pathway.
    """

    modes = _valid_modes(symbol_result)
    if modes:
        return "\n\n".join(
            format_valid_setup_message(
                symbol_result,
                mode=mode,
                diagnostics_level=diagnostics_level,
                compact=compact,
            )
            for mode in modes
        )

    return format_no_setup_message(
        symbol_result,
        diagnostics_level=diagnostics_level,
        compact=compact,
    )


def format_valid_setup_message(
    symbol_result: ScannerSymbolResult,
    *,
    mode: str,
    setup: LiquidityGrabSetup | None = None,
    diagnostics_level: DiagnosticsLevel = "normal",
    compact: bool = False,
) -> str:
    setup = setup or _setup_from_symbol_result(symbol_result, mode)
    diagnostics = _diagnostics_for_mode(symbol_result, mode)
    mode_title = _mode_title(mode)
    symbol = symbol_result.symbol
    grade = _trust_grade(setup, diagnostics)
    trust_percentage = _trust_percentage(setup, diagnostics)
    status = _display(_setup_field(setup, diagnostics, "status"))
    if status == NA:
        status = "Pending"
    display = build_symbol_display(symbol_result)

    if compact:
        return "\n".join(
            (
                (
                    f"{symbol} {DASH} Valid Setup | {mode_title} {grade} {trust_percentage}% | "
                    f"Bias: {_trade_bias(setup, diagnostics)} | Entry: {_entry_text(setup, diagnostics)} | "
                    f"Stop: {_display(_setup_field(setup, diagnostics, 'stop'))} | "
                    f"RR: {_display(_setup_field(setup, diagnostics, 'rr_to_tp2'))} | Trade idea created."
                ),
                FOOTER,
            )
        )

    lines = [
        f"{symbol} {DASH} Valid Setup",
        "",
        f"{PIN} Bias",
        f"{BULLET} 2D HTF: {_display(_setup_field(setup, diagnostics, 'htf_2d_trend'))}",
        f"{BULLET} 12H Bias: {_display(_setup_field(setup, diagnostics, 'mtf_12h_trend'))}",
        "",
        f"{CHECK} Passed",
        *_telegram_passed_lines(display),
        "",
        "Orderflow",
        f"{BULLET} POC: {_display(_first_available(_setup_field(setup, diagnostics, 'poc'), symbol_result.poc))}",
        f"{BULLET} VAH/VAL: {_vah_val_text(symbol_result)}",
        f"{BULLET} Funding: {_funding_text(symbol_result)}",
        f"{BULLET} OI: {_oi_text(symbol_result)}",
        "",
        f"{TARGET} Trade Idea",
        f"{BULLET} Bias: {_trade_bias(setup, diagnostics)}",
        f"{BULLET} Entry: {_entry_text(setup, diagnostics)}",
        f"{BULLET} Stop: {_display(_setup_field(setup, diagnostics, 'stop'))}",
        f"{BULLET} RR: {_display(_setup_field(setup, diagnostics, 'rr_to_tp2'))}",
        f"{BULLET} Trust Meter: {grade} + {trust_percentage}%",
        f"{BULLET} Invalidation: {_display(_setup_field(setup, diagnostics, 'invalidation'))}",
        f"{BULLET} Risk warning: {_risk_warning(setup, diagnostics)}",
        "",
        f"{BRAIN} Final Result",
        f"Trade idea created. Status: {status}.",
    ]

    if diagnostics_level == "full":
        lines.extend(("", "4) Diagnostics", *_diagnostic_lines(symbol_result, diagnostics)))

    lines.extend(("", FOOTER))
    return "\n".join(lines)


def format_no_setup_message(
    symbol_result: ScannerSymbolResult,
    *,
    diagnostics_level: DiagnosticsLevel = "normal",
    compact: bool = False,
) -> str:
    return format_rejection_summary(
        symbol_result,
        diagnostics_level=diagnostics_level,
        compact=compact,
    )


def format_rejection_summary(
    symbol_result: ScannerSymbolResult,
    *,
    diagnostics_level: DiagnosticsLevel = "normal",
    compact: bool = False,
) -> str:
    diagnostics = _representative_diagnostics(symbol_result)
    symbol = symbol_result.symbol
    display = build_symbol_display(symbol_result)
    failed_gate = _failed_gate(symbol_result, diagnostics)
    reason = display.short_reason

    if compact:
        return "\n".join(
            (
                (
                    f"{symbol} {DASH} {_telegram_no_setup_title(display.display_status)} | "
                    f"Failed: {_telegram_failed_summary(display, failed_gate)} | "
                    f"Why: {reason} | No valid setup. No trade. Watching only."
                ),
                FOOTER,
            )
        )

    lines = [
        f"{symbol} {DASH} {_telegram_no_setup_title(display.display_status)}",
        "",
        f"{PIN} Bias",
        f"{BULLET} 2D HTF: {_display(diagnostics.get('htf_2d_trend'))}",
        f"{BULLET} 12H Bias: {_display(diagnostics.get('mtf_12h_trend'))}",
        "",
        f"{CHECK} Passed",
        *_telegram_passed_lines(display),
        "",
        f"{CROSS} Failed",
        *_telegram_failed_lines(display, failed_gate),
        "",
        f"{BRAIN} Why",
        reason,
        "",
        f"{TARGET} Action",
        "No valid setup. No trade. Watching only.",
    ]

    if diagnostics_level == "full":
        lines.extend(("", "Diagnostics", *_diagnostic_lines(symbol_result, diagnostics)))

    lines.extend(("", FOOTER))
    return "\n".join(lines)


def _telegram_no_setup_title(display_status: str) -> str:
    if display_status == "near_miss":
        return "Near Miss (No Valid Setup)"
    if display_status == "data_incomplete":
        return "Data Incomplete (No Valid Setup)"
    return "No Valid Setup"


def _telegram_passed_lines(display: Any) -> list[str]:
    values = [item.label for item in display.progress_items if item.passed]
    if not values:
        return [f"{BULLET} None yet."]
    return [f"{BULLET} {value}" for value in values]


def _telegram_failed_lines(display: Any, failed_gate: str) -> list[str]:
    values = [item.label for item in display.progress_items if not item.passed and item.evaluated]
    lines = [f"{BULLET} {value}" for value in values]
    if failed_gate != NA:
        lines.append(f"{BULLET} Gate: {failed_gate}")
    if not lines:
        return [f"{BULLET} None."]
    return lines


def _telegram_failed_summary(display: Any, failed_gate: str) -> str:
    failed = [item.label for item in display.progress_items if not item.passed and item.evaluated]
    if failed_gate != NA:
        failed.append(f"Gate: {failed_gate}")
    return ", ".join(failed) if failed else NA


def _valid_modes(symbol_result: ScannerSymbolResult) -> tuple[str, ...]:
    candidates: list[str] = []
    candidates.extend(symbol_result.valid_strategy_modes)

    for mode, result in symbol_result.strategy_results.items():
        setup = _setup_from_result(result, mode)
        if setup is not None and setup.is_valid and mode not in candidates:
            candidates.append(mode)

    for mode, diagnostics in symbol_result.strategy_diagnostics.items():
        if isinstance(diagnostics, Mapping) and diagnostics.get("is_valid") is True and mode not in candidates:
            candidates.append(mode)

    ordered = [mode for mode in MODE_ORDER if mode in candidates]
    ordered.extend(mode for mode in candidates if mode not in ordered)
    return tuple(ordered)


def _setup_from_symbol_result(symbol_result: ScannerSymbolResult, mode: str) -> LiquidityGrabSetup | None:
    result = symbol_result.strategy_results.get(mode)
    if result is None:
        return None
    return _setup_from_result(result, mode)


def _setup_from_result(result: LiquidityGrabResult, mode: str) -> LiquidityGrabSetup | None:
    try:
        selected = LiquidityGrabMode(mode)
    except ValueError:
        return None
    if selected == LiquidityGrabMode.challenge:
        return result.challenge
    if selected == LiquidityGrabMode.scalp:
        return result.scalp
    return result.swing


def _diagnostics_for_mode(symbol_result: ScannerSymbolResult, mode: str) -> Mapping[str, Any]:
    diagnostics = symbol_result.strategy_diagnostics.get(mode)
    if isinstance(diagnostics, Mapping):
        return diagnostics
    return {}


def _representative_diagnostics(symbol_result: ScannerSymbolResult) -> Mapping[str, Any]:
    for mode in symbol_result.rejected_strategy_modes:
        diagnostics = _diagnostics_for_mode(symbol_result, mode)
        if diagnostics:
            return diagnostics
    for mode in MODE_ORDER:
        diagnostics = _diagnostics_for_mode(symbol_result, mode)
        if diagnostics:
            return diagnostics
    for diagnostics in symbol_result.strategy_diagnostics.values():
        if isinstance(diagnostics, Mapping):
            return diagnostics
    return {}


def _setup_field(setup: LiquidityGrabSetup | None, diagnostics: Mapping[str, Any], name: str) -> Any:
    if setup is not None and hasattr(setup, name):
        value = getattr(setup, name)
        if value is not None and value != "":
            return value
    return diagnostics.get(name, NA)


def _current_price_text(
    symbol_result: ScannerSymbolResult,
    setup: LiquidityGrabSetup | None,
    diagnostics: Mapping[str, Any],
) -> str:
    return _display(
        _first_available(
            _setup_field(setup, diagnostics, "current_price"),
            symbol_result.current_price,
            symbol_result.latest_close,
        )
    )


def _key_context_text(
    symbol_result: ScannerSymbolResult,
    setup: LiquidityGrabSetup | None,
    diagnostics: Mapping[str, Any],
) -> str:
    parts: list[str] = []
    if symbol_result.nearest_support != NA:
        parts.append(f"support {_display(symbol_result.nearest_support)}")
    if symbol_result.nearest_resistance != NA:
        parts.append(f"resistance {_display(symbol_result.nearest_resistance)}")
    sweep_zone = _sweep_zone_text(setup, diagnostics)
    if sweep_zone != NA:
        parts.append(f"sweep {sweep_zone}")
    return "; ".join(parts) if parts else NA


def _vah_val_text(symbol_result: ScannerSymbolResult) -> str:
    high = _display(symbol_result.value_area_high)
    low = _display(symbol_result.value_area_low)
    if high == NA and low == NA:
        return NA
    return f"{high} / {low}"


def _funding_text(symbol_result: ScannerSymbolResult) -> str:
    rate = _display(symbol_result.funding_rate)
    status = _display(symbol_result.funding_status)
    direction = _display(symbol_result.funding_direction)
    parts = [part for part in (rate, status, direction) if part != NA]
    return " / ".join(parts) if parts else NA


def _oi_text(symbol_result: ScannerSymbolResult) -> str:
    value = _display(symbol_result.open_interest)
    change = _percentage_text(symbol_result.open_interest_change_pct)
    direction = _display(symbol_result.oi_direction)
    parts = [part for part in (value, change, direction) if part != NA]
    return " / ".join(parts) if parts else NA


def _trade_bias(setup: LiquidityGrabSetup | None, diagnostics: Mapping[str, Any]) -> str:
    return _display(_setup_field(setup, diagnostics, "bias"))


def _sweep_zone_text(setup: LiquidityGrabSetup | None, diagnostics: Mapping[str, Any]) -> str:
    if setup is not None:
        wick = _display(setup.sweep.wick_price)
        level = _display(setup.sweep.swing_level)
        if wick != NA and level != NA:
            return f"{wick} -> {level}"
    if diagnostics.get("sweep_zone") not in (None, "", NA):
        return _display(diagnostics.get("sweep_zone"))
    wick = _display(diagnostics.get("sweep_wick_price"))
    level = _display(diagnostics.get("sweep_swing_level"))
    if wick != NA and level != NA:
        return f"{wick} -> {level}"
    return NA


def _entry_text(setup: LiquidityGrabSetup | None, diagnostics: Mapping[str, Any]) -> str:
    entry_low = _display(_setup_field(setup, diagnostics, "entry_low"))
    entry_high = _display(_setup_field(setup, diagnostics, "entry_high"))
    entry = _display(_setup_field(setup, diagnostics, "entry"))
    if entry_low != NA and entry_high != NA:
        if entry_low == entry_high:
            return entry_low
        return f"{entry_low} - {entry_high}"
    return entry


def _tp_text(setup: LiquidityGrabSetup | None, diagnostics: Mapping[str, Any]) -> str:
    tp1 = _display(_setup_field(setup, diagnostics, "tp1"))
    tp2 = _display(_setup_field(setup, diagnostics, "tp2"))
    tp3 = _display(_setup_field(setup, diagnostics, "tp3"))
    return f"TP1 {tp1}, TP2 {tp2}, TP3 {tp3}"


def _trust_grade(setup: LiquidityGrabSetup | None, diagnostics: Mapping[str, Any]) -> str:
    if setup is not None:
        return _display(setup.trust_meter.grade)
    return _display(diagnostics.get("trust_grade"))


def _trust_percentage(setup: LiquidityGrabSetup | None, diagnostics: Mapping[str, Any]) -> str:
    if setup is not None:
        return _display(setup.trust_meter.percentage)
    return _display(diagnostics.get("trust_percentage"))


def _risk_warning(setup: LiquidityGrabSetup | None, diagnostics: Mapping[str, Any]) -> str:
    value = _setup_field(setup, diagnostics, "risk_warning")
    text = _display(value)
    if text == NA:
        return RISK_WARNING_FALLBACK
    return text


def _failed_gate(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    failed_gate = _display(diagnostics.get("first_failed_gate"))
    if failed_gate != NA:
        return failed_gate
    gates_failed = diagnostics.get("gates_failed")
    if _is_sequence(gates_failed) and gates_failed:
        return _display(gates_failed[0])
    if symbol_result.rejection_stage != NA:
        return _display(symbol_result.rejection_stage)
    return NA


def _clean_rejection_reason(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> str:
    for key in (
        "confirmation_bos_choch_reason",
        "pullback_failure_reason",
        "derivatives_conflict_reason",
    ):
        text = _display(diagnostics.get(key))
        if text != NA:
            return text

    hard_rejections = diagnostics.get("hard_rejection_reasons")
    if _is_sequence(hard_rejections) and hard_rejections:
        return _display(hard_rejections[0])

    if symbol_result.rejection_reasons:
        return "; ".join(symbol_result.rejection_reasons)
    if symbol_result.rejection_reason:
        return symbol_result.rejection_reason
    if symbol_result.error_message:
        return symbol_result.error_message
    return NA


def _diagnostic_lines(symbol_result: ScannerSymbolResult, diagnostics: Mapping[str, Any]) -> list[str]:
    return [
        f"{BULLET} Passed gates: {_sequence_text(diagnostics.get('gates_passed'))}",
        f"{BULLET} Failed gates: {_sequence_text(diagnostics.get('gates_failed'))}",
        f"{BULLET} Hard rejections: {_sequence_text(diagnostics.get('hard_rejection_reasons'))}",
        f"{BULLET} Missing data: {_sequence_text(symbol_result.strategy_missing_data or symbol_result.missing_data)}",
        f"{BULLET} Unverified data: {_sequence_text(symbol_result.strategy_unverified_data or symbol_result.unverified_data)}",
        f"{BULLET} Volume warnings: {_sequence_text(symbol_result.volume_profile_warnings)}",
    ]


def _first_available(*values: Any) -> Any:
    for value in values:
        if value is not None and value != "" and value != NA:
            return value
    return NA


def _mode_title(mode: str) -> str:
    return mode.replace("_", " ").title()


def _status_text(value: Any) -> str:
    text = _display(value)
    if text == "not_evaluated":
        return "not evaluated"
    return text


def _percentage_text(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    return f"{text}%"


def _sequence_text(values: Any) -> str:
    if not _is_sequence(values):
        return NA
    text = ", ".join(_display(value) for value in values if _display(value) != NA)
    return text if text else NA


def _is_sequence(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    return str(value)


__all__ = [
    "format_no_setup_message",
    "format_rejection_summary",
    "format_telegram_strategy_output",
    "format_valid_setup_message",
]
