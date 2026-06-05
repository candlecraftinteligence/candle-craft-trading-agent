from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import FOOTER, HEADER_PREFIX, RANGE_DASH, format_direction, format_price, format_symbol

EM_DASH = "\u2014"
ARROW = " \u2192 "
UNVERIFIED = "Unverified"


@dataclass(frozen=True)
class TelegramSignalDetail:
    symbol: Any = NA
    bias: Any = NA
    status: Any = NA
    quality: Any = NA
    lifecycle: Any = NA
    entry_low: Any = NA
    entry_high: Any = NA
    stop_loss: Any = NA
    tp1: Any = NA
    tp2: Any = NA
    tp3: Any = NA
    why_it_matters: Any = NA
    invalid_if: Any = NA
    confirmed_facts: tuple[Any, ...] = ()
    confirmed_gates: tuple[Any, ...] = ()
    lifecycle_reason: Any = NA


def format_signal_detail(detail: TelegramSignalDetail, *, detail_type: str = "SIGNAL") -> str:
    title = "WATCHLIST DETAIL" if _status_key(detail_type) == "watchlist" else "SIGNAL DETAIL"
    return "\n".join(
        (
            f"{HEADER_PREFIX} {format_symbol(detail.symbol)} {EM_DASH} {title}",
            "",
            f"Bias: {_bias_display(detail.bias)}",
            f"Status: {_status_display(detail.status)}",
            f"Quality: {_text(detail.quality)}",
            f"Lifecycle: {_lifecycle_display(detail.lifecycle)}",
            "",
            "\U0001F3AF Trade Map",
            f"Entry: {_entry_text(detail.entry_low, detail.entry_high)}",
            f"Stop: {_price_or_text(detail.stop_loss)}",
            f"TP1: {_price_or_text(detail.tp1)}",
            f"TP2: {_price_or_text(detail.tp2)}",
            f"TP3: {_price_or_text(detail.tp3)}",
            "",
            "\U0001F9E0 Why it matters",
            _public_text(detail.why_it_matters),
            "",
            "\U0001F6AB Invalid if",
            _public_text(detail.invalid_if),
            "",
            FOOTER,
        )
    )


def format_signal_detail_lifecycle(detail: TelegramSignalDetail) -> str:
    reason = _public_text(detail.lifecycle_reason)
    return "\n".join(
        (
            f"{HEADER_PREFIX} {format_symbol(detail.symbol)} {EM_DASH} LIFECYCLE",
            "",
            _lifecycle_display(detail.lifecycle),
            "",
            f"Latest reason: {reason}",
            "",
            FOOTER,
        )
    )


def format_signal_detail_why_valid(detail: TelegramSignalDetail) -> str:
    facts = _facts_text(detail.confirmed_facts)
    gates = _facts_text(detail.confirmed_gates)
    return "\n".join(
        (
            f"{HEADER_PREFIX} {format_symbol(detail.symbol)} {EM_DASH} WHY VALID?",
            "",
            "Confirmed facts",
            facts,
            "",
            "Confirmed gates",
            gates,
            "",
            FOOTER,
        )
    )


def lifecycle_chain_text(values: Sequence[Any]) -> str:
    states: list[str] = []
    for value in values:
        text = _state_text(value)
        if text != NA and text not in states:
            states.append(text)
    return ARROW.join(states) if states else NA


def _entry_text(low: Any, high: Any) -> str:
    low_text = _price_or_text(low)
    high_text = _price_or_text(high)
    if low_text == NA and high_text == NA:
        return NA
    if high_text == NA or low_text == high_text:
        return low_text
    if low_text == NA:
        return high_text
    return f"{low_text} {RANGE_DASH} {high_text}"


def _price_or_text(value: Any) -> str:
    text = _text(value)
    if text == UNVERIFIED:
        return UNVERIFIED
    return format_price(value)


def _facts_text(values: Sequence[Any]) -> str:
    lines: list[str] = []
    for value in values:
        text = _public_text(value)
        if text != NA and text not in lines:
            lines.append(text)
        if len(lines) == 4:
            break
    return "\n".join(lines) if lines else NA


def _lifecycle_display(value: Any) -> str:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray, Mapping)):
        return lifecycle_chain_text(value)
    text = _text(value)
    if text == UNVERIFIED:
        return UNVERIFIED
    if ARROW in text:
        return ARROW.join(_state_text(part) for part in text.split(ARROW) if _state_text(part) != NA) or NA
    return _state_text(text)


def _state_text(value: Any) -> str:
    text = _text(value)
    if text in {NA, UNVERIFIED}:
        return text
    normalized = text.replace("-", "_").replace(" ", "_").strip("_")
    if not normalized:
        return NA
    return " ".join(part for part in normalized.upper().split("_") if part)


def _status_display(value: Any) -> str:
    text = _text(value)
    if text in {NA, UNVERIFIED}:
        return text
    return _state_text(text)


def _bias_display(value: Any) -> str:
    text = _text(value)
    if text in {NA, UNVERIFIED}:
        return text
    return format_direction(value)


def _public_text(value: Any) -> str:
    text = _text(value)
    if text in {NA, UNVERIFIED}:
        return text
    lowered = text.lower()
    if (
        "decimal(" in lowered
        or "strategy_diagnostics" in lowered
        or "raw_result" in lowered
        or "first_failed_gate" in lowered
        or "{" in text
        or "}" in text
        or lowered in {"true", "false"}
    ):
        return NA
    return _short_text(text)


def _short_text(value: str, max_length: int = 180) -> str:
    text = " ".join(value.split()).strip()
    if not text:
        return NA
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3].rstrip()}..."


def _text(value: Any) -> str:
    if value is None or value == "" or value == NA:
        return NA
    if isinstance(value, bool):
        return NA
    if isinstance(value, Mapping):
        return NA
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return NA
    enum_value = getattr(value, "value", None)
    if isinstance(enum_value, str):
        value = enum_value
    text = " ".join(str(value).split())
    if not text or text.upper() == NA:
        return NA
    if "unverified" in _status_key(text):
        return UNVERIFIED
    return text


def _status_key(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


__all__ = [
    "TelegramSignalDetail",
    "format_signal_detail",
    "format_signal_detail_lifecycle",
    "format_signal_detail_why_valid",
    "lifecycle_chain_text",
]
