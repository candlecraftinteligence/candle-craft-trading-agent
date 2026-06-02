from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum
from typing import Any

from app.data.dtos import NA

HEADER_PREFIX = "\U0001F43A\U0001F7E0"
FOOTER = "\U0001F43A Candle Craft | Signal. Structure. Execution."
BULLET = "\u2022"
RANGE_DASH = "\u2013"


class TelegramAlertType(str, Enum):
    WATCHLIST = "WATCHLIST"
    SIGNAL_CONFIRMED = "SIGNAL_CONFIRMED"
    LIMIT_HIT = "LIMIT_HIT"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    TP3_HIT = "TP3_HIT"
    SL_HIT = "SL_HIT"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"


PUBLIC_STATUS_BY_ALERT_TYPE = {
    TelegramAlertType.WATCHLIST: "WATCHLIST",
    TelegramAlertType.SIGNAL_CONFIRMED: "CONFIRMED",
    TelegramAlertType.LIMIT_HIT: "LIMIT HIT",
    TelegramAlertType.TP1_HIT: "TP1 HIT",
    TelegramAlertType.TP2_HIT: "TP2 HIT",
    TelegramAlertType.TP3_HIT: "TP3 HIT",
    TelegramAlertType.SL_HIT: "SL HIT",
    TelegramAlertType.INVALIDATED: "INVALIDATED",
    TelegramAlertType.EXPIRED: "EXPIRED",
}


@dataclass(frozen=True)
class TelegramSignalMessage:
    symbol: Any = NA
    direction: Any = NA
    signal_id: Any = NA
    entry_low: Any = NA
    entry_high: Any = NA
    stop_loss: Any = NA
    tp1: Any = NA
    tp2: Any = NA
    tp3: Any = NA
    planned_rr: Any = NA
    structure_reason: Any = NA
    confirmation_needed: Any = NA
    invalidation_reason: Any = NA
    htf_bias: Any = NA
    ob_fvg_status: Any = NA
    volume_status: Any = NA
    derivatives_status: Any = NA
    price_level: Any = NA


def format_telegram_signal_message(
    alert_type: TelegramAlertType | str,
    message: TelegramSignalMessage,
) -> str:
    normalized = alert_type if isinstance(alert_type, TelegramAlertType) else TelegramAlertType(str(alert_type))
    if normalized == TelegramAlertType.WATCHLIST:
        return format_watchlist_alert(message)
    if normalized == TelegramAlertType.SIGNAL_CONFIRMED:
        return format_signal_confirmed_alert(message)
    if normalized == TelegramAlertType.LIMIT_HIT:
        return format_limit_hit_update(message)
    if normalized == TelegramAlertType.TP1_HIT:
        return format_tp1_hit_update(message)
    if normalized == TelegramAlertType.TP2_HIT:
        return format_tp2_hit_update(message)
    if normalized == TelegramAlertType.TP3_HIT:
        return format_tp3_hit_update(message)
    if normalized == TelegramAlertType.SL_HIT:
        return format_sl_hit_update(message)
    if normalized == TelegramAlertType.INVALIDATED:
        return format_invalidated_update(message)
    if normalized == TelegramAlertType.EXPIRED:
        return format_expired_update(message)
    raise ValueError(f"Unsupported Telegram alert type: {alert_type}")


def format_watchlist_alert(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT WATCHLIST",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "WATCHLIST",
        "",
        "Watch Zone:",
        _entry_range(message),
        "",
        "Stop Loss:",
        _display(message.stop_loss),
        "",
        "Potential Targets:",
        f"TP1: {_display(message.tp1)}",
        f"TP2: {_display(message.tp2)}",
        f"TP3: {_display(message.tp3)}",
        "",
        "Planned RR:",
        f"{_display(message.planned_rr)}R",
        "",
        "Structure:",
        _display(message.structure_reason),
        "",
        "Confirmation Needed:",
        _display(message.confirmation_needed),
        "",
        "Invalidation:",
        _display(message.invalidation_reason),
        "",
        "Signal ID:",
        _display(message.signal_id),
        "",
        "System:",
        "Watchlist only. No active signal yet.",
        "",
        FOOTER,
    )


def format_signal_confirmed_alert(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT SIGNAL CONFIRMED",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "CONFIRMED",
        "",
        "Entry Zone:",
        _entry_range(message),
        "",
        "Stop Loss:",
        _display(message.stop_loss),
        "",
        "Take Profits:",
        f"TP1: {_display(message.tp1)}",
        f"TP2: {_display(message.tp2)}",
        f"TP3: {_display(message.tp3)}",
        "",
        "Planned RR:",
        f"{_display(message.planned_rr)}R",
        "",
        "Structure:",
        _display(message.structure_reason),
        "",
        "Confluence:",
        f"{BULLET} HTF Bias: {_display(message.htf_bias)}",
        f"{BULLET} OB/FVG: {_display(message.ob_fvg_status)}",
        f"{BULLET} Volume: {_display(message.volume_status)}",
        f"{BULLET} OI/Funding/CVD: {_display(message.derivatives_status)}",
        "",
        "Invalidation:",
        _display(message.invalidation_reason),
        "",
        "Signal ID:",
        _display(message.signal_id),
        "",
        "System:",
        "Alert only. Trade managed manually by Adam.",
        "",
        FOOTER,
    )


def format_limit_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT UPDATE",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "LIMIT HIT",
        "",
        "Signal ID:",
        _display(message.signal_id),
        "",
        "Entry Zone:",
        _entry_range(message),
        "",
        "Update:",
        "Price has reached the planned entry zone.",
        "",
        "Current State:",
        "Setup remains valid while price respects the entry structure and invalidation level.",
        "",
        "Risk Level:",
        f"SL: {_display(message.stop_loss)}",
        "",
        "Targets:",
        f"TP1: {_display(message.tp1)}",
        f"TP2: {_display(message.tp2)}",
        f"TP3: {_display(message.tp3)}",
        "",
        "System:",
        "Price alert only. No order was placed by the system.",
        "",
        FOOTER,
    )


def format_tp1_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT UPDATE",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "TP1 HIT",
        "",
        "Signal ID:",
        _display(message.signal_id),
        "",
        "Result:",
        "First target reached.",
        "",
        "Price Level:",
        _display(message.tp1),
        "",
        "Current State:",
        "Setup remains active while structure holds.",
        "",
        "Next Targets:",
        f"TP2: {_display(message.tp2)}",
        f"TP3: {_display(message.tp3)}",
        "",
        "Research:",
        "Outcome saved for performance analysis.",
        "",
        FOOTER,
    )


def format_tp2_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT UPDATE",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "TP2 HIT",
        "",
        "Signal ID:",
        _display(message.signal_id),
        "",
        "Result:",
        "Second target reached.",
        "",
        "Price Level:",
        _display(message.tp2),
        "",
        "Current State:",
        "Final target remains active while structure holds.",
        "",
        "Next Target:",
        f"TP3: {_display(message.tp3)}",
        "",
        "Research:",
        "Outcome saved for performance analysis.",
        "",
        FOOTER,
    )


def format_tp3_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT UPDATE",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "TP3 HIT",
        "",
        "Signal ID:",
        _display(message.signal_id),
        "",
        "Result:",
        "Final target reached.",
        "",
        "Price Level:",
        _display(message.tp3),
        "",
        "Lifecycle:",
        "Signal completed.",
        "",
        "Research:",
        "Outcome saved for performance analysis.",
        "",
        FOOTER,
    )


def format_sl_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT UPDATE",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "SL HIT",
        "",
        "Signal ID:",
        _display(message.signal_id),
        "",
        "Result:",
        "Stop level reached.",
        "",
        "Price Level:",
        _display(message.stop_loss),
        "",
        "Lifecycle:",
        "Signal closed.",
        "",
        "Research:",
        "Outcome saved for failure analysis.",
        "",
        FOOTER,
    )


def format_invalidated_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT INVALIDATION",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "INVALIDATED",
        "",
        "Signal ID:",
        _display(message.signal_id),
        "",
        "Reason:",
        _display(message.invalidation_reason),
        "",
        "Lifecycle:",
        "Setup removed from active signal tracking.",
        "",
        "Research:",
        "Invalidation saved for lifecycle and expectancy analysis.",
        "",
        FOOTER,
    )


def format_expired_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT UPDATE",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "EXPIRED",
        "",
        "Signal ID:",
        _display(message.signal_id),
        "",
        "Reason:",
        "Setup did not confirm within the valid lifecycle window.",
        "",
        "Lifecycle:",
        "Setup removed from active tracking.",
        "",
        "Research:",
        "Expiration saved for lifecycle and expectancy analysis.",
        "",
        FOOTER,
    )


def _entry_range(message: TelegramSignalMessage) -> str:
    return f"{_display(message.entry_low)} {RANGE_DASH} {_display(message.entry_high)}"


def _join(*lines: str) -> str:
    return "\n".join(lines)


def _display(value: Any) -> str:
    if value is None or value == "" or value == NA:
        return NA
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, bool):
        return "true" if value else "false"
    text = " ".join(str(value).split())
    return text if text else NA


__all__ = [
    "FOOTER",
    "HEADER_PREFIX",
    "PUBLIC_STATUS_BY_ALERT_TYPE",
    "TelegramAlertType",
    "TelegramSignalMessage",
    "format_expired_update",
    "format_invalidated_update",
    "format_limit_hit_update",
    "format_signal_confirmed_alert",
    "format_sl_hit_update",
    "format_telegram_signal_message",
    "format_tp1_hit_update",
    "format_tp2_hit_update",
    "format_tp3_hit_update",
    "format_watchlist_alert",
]
