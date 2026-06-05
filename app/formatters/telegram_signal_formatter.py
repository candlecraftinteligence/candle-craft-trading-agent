from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any

from app.data.dtos import NA

HEADER_PREFIX = "\U0001F43A\U0001F7E0"
FOOTER = "\U0001F43A Candle Craft | Signal. Structure. Execution."
BULLET = "\u2022"
RANGE_DASH = "\u2013"
EM_DASH = "\u2014"
GREATER_EQUAL = "\u2265"
DEFAULT_MIN_RR_DISPLAY = Decimal("2.7")


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
    NO_LONGER_TRACKING = "NO_LONGER_TRACKING"


PUBLIC_STATUS_BY_ALERT_TYPE = {
    TelegramAlertType.WATCHLIST: "WATCHLIST",
    TelegramAlertType.SIGNAL_CONFIRMED: "CONFIRMED",
    TelegramAlertType.LIMIT_HIT: "LIMIT ZONE HIT",
    TelegramAlertType.TP1_HIT: "TP1 HIT",
    TelegramAlertType.TP2_HIT: "TP2 HIT",
    TelegramAlertType.TP3_HIT: "TP3 HIT",
    TelegramAlertType.SL_HIT: "SL HIT",
    TelegramAlertType.INVALIDATED: "INVALIDATED",
    TelegramAlertType.EXPIRED: "EXPIRED",
    TelegramAlertType.NO_LONGER_TRACKING: "NO LONGER TRACKING",
}


@dataclass(frozen=True)
class TelegramSignalMessage:
    symbol: Any = NA
    direction: Any = NA
    signal_id: Any = NA
    watch_zone: Any = NA
    entry_low: Any = NA
    entry_high: Any = NA
    stop_loss: Any = NA
    tp1: Any = NA
    tp2: Any = NA
    tp3: Any = NA
    planned_rr: Any = NA
    current_context: Any = NA
    needs_next: tuple[Any, ...] = ()
    structure_reason: Any = NA
    confirmation_needed: Any = NA
    invalidation_reason: Any = NA
    watchlist_invalidation_reason: Any = NA
    confluence: Any = NA
    htf_bias: Any = NA
    ob_fvg_status: Any = NA
    volume_status: Any = NA
    derivatives_status: Any = NA
    price_level: Any = NA
    min_rr: Any = NA
    watchlist_outcome: bool = False


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
    if normalized == TelegramAlertType.NO_LONGER_TRACKING:
        return format_no_longer_tracking_update(message)
    raise ValueError(f"Unsupported Telegram alert type: {alert_type}")


def format_watchlist_alert(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT WATCHLIST",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "WATCHLIST",
        "",
        "Limit Zone:",
        _watch_zone(message),
        "",
        "Current Context:",
        _display(message.current_context),
        "",
        "Needs Next:",
        *_needs_next_lines(message),
        "",
        "Potential Plan:",
        f"Entry: {_entry_range(message)}",
        f"SL: {_price_display(message.stop_loss)}",
        "Potential Targets:",
        f"TP1: {_price_display(message.tp1)}",
        f"TP2: {_price_display(message.tp2)}",
        f"TP3: {_price_display(message.tp3)}",
        _watchlist_planned_rr_line(message),
        "",
        "Invalidation:",
        _first_display(message.watchlist_invalidation_reason, message.invalidation_reason),
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
        _price_display(message.stop_loss),
        "",
        "Take Profits:",
        f"TP1: {_price_display(message.tp1)}",
        f"TP2: {_price_display(message.tp2)}",
        f"TP3: {_price_display(message.tp3)}",
        "",
        "Planned RR:",
        _rr_with_unit(message.planned_rr),
        "",
        "Structure:",
        _display(message.structure_reason),
        "",
        "Confluence:",
        _display(message.confluence),
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
    if message.watchlist_outcome:
        return _join(
            f"{HEADER_PREFIX} CANDLE CRAFT UPDATE",
            f"{_display(message.symbol)} | {_display(message.direction)}",
            "",
            "Status:",
            "LIMIT ZONE HIT",
            "",
            "Signal ID:",
            _display(message.signal_id),
            "",
            "Update:",
            "Price has reached the planned watchlist Limit Zone.",
            "",
            "Limit Zone:",
            _watch_zone(message),
            "",
            "System:",
            "Watchlist tracking update. No order was placed by the system.",
            "",
            FOOTER,
        )
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT UPDATE",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "LIMIT ZONE HIT",
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
        f"SL: {_price_display(message.stop_loss)}",
        "",
        "Targets:",
        f"TP1: {_price_display(message.tp1)}",
        f"TP2: {_price_display(message.tp2)}",
        f"TP3: {_price_display(message.tp3)}",
        "",
        "System:",
        "Price alert only. No order was placed by the system.",
        "",
        FOOTER,
    )


def format_tp1_hit_update(message: TelegramSignalMessage) -> str:
    if message.watchlist_outcome:
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
            "First target reached from the watchlist plan.",
            "",
            "Price Level:",
            _price_display(message.tp1),
            "",
            "System:",
            "Watchlist tracking update. Manual trade management only.",
            "",
            FOOTER,
        )
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
        _price_display(message.tp1),
        "",
        "Current State:",
        "Setup remains active while structure holds.",
        "",
        "Next Targets:",
        f"TP2: {_price_display(message.tp2)}",
        f"TP3: {_price_display(message.tp3)}",
        "",
        "Research:",
        "Outcome saved for performance analysis.",
        "",
        FOOTER,
    )


def format_tp2_hit_update(message: TelegramSignalMessage) -> str:
    if message.watchlist_outcome:
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
            "Second target reached from the watchlist plan.",
            "",
            "Price Level:",
            _price_display(message.tp2),
            "",
            "System:",
            "Watchlist tracking update. Manual trade management only.",
            "",
            FOOTER,
        )
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
        _price_display(message.tp2),
        "",
        "Current State:",
        "Final target remains active while structure holds.",
        "",
        "Next Target:",
        f"TP3: {_price_display(message.tp3)}",
        "",
        "Research:",
        "Outcome saved for performance analysis.",
        "",
        FOOTER,
    )


def format_tp3_hit_update(message: TelegramSignalMessage) -> str:
    if message.watchlist_outcome:
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
            "Final target reached from the watchlist plan.",
            "",
            "Lifecycle:",
            "Watchlist outcome tracking completed.",
            "",
            "System:",
            "Watchlist tracking update. Manual trade management only.",
            "",
            FOOTER,
        )
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
        _price_display(message.tp3),
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
    if message.watchlist_outcome:
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
            "Stop level reached from the watchlist plan.",
            "",
            "Price Level:",
            _price_display(message.stop_loss),
            "",
            "Lifecycle:",
            "Watchlist outcome tracking closed.",
            "",
            "System:",
            "Watchlist tracking update. Manual trade management only.",
            "",
            FOOTER,
        )
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
        _price_display(message.stop_loss),
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
        "System:",
        "Watchlist removed from active tracking.",
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
        _display(message.invalidation_reason),
        "",
        "System:",
        "Watchlist expired. No active signal.",
        "",
        FOOTER,
    )


def format_no_longer_tracking_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} CANDLE CRAFT UPDATE",
        f"{_display(message.symbol)} | {_display(message.direction)}",
        "",
        "Status:",
        "NO LONGER TRACKING",
        "",
        "Signal ID:",
        _display(message.signal_id),
        "",
        "Reason:",
        _display(message.invalidation_reason),
        "",
        "System:",
        "Watchlist removed from active tracking.",
        "",
        FOOTER,
    )


def _entry_range(message: TelegramSignalMessage) -> str:
    return f"{_price_display(message.entry_low)} {RANGE_DASH} {_price_display(message.entry_high)}"


def _watch_zone(message: TelegramSignalMessage) -> str:
    watch_zone = _price_range_text(message.watch_zone)
    return watch_zone if watch_zone != NA else _entry_range(message)


def _needs_next_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    lines: list[str] = []
    values = message.needs_next
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        for value in values:
            text = _display(value)
            if text != NA and _chart_only_need(text):
                lines.append(text)
            if len(lines) == 3:
                break
    if not lines:
        lines.extend(_fallback_needs_next(message))
    return tuple(f"{index}. {line}" for index, line in enumerate(lines, start=1))


def _fallback_needs_next(message: TelegramSignalMessage) -> tuple[str, str, str]:
    side = _display(message.direction).lower()
    if side == "long":
        return (
            "Price must trade into the Limit Zone.",
            "Limit Zone must hold as support after the pullback.",
            "Bullish structure must remain valid above the invalidation level.",
        )
    if side == "short":
        return (
            "Price must trade into the Limit Zone.",
            "Limit Zone must hold as resistance after the pullback.",
            "Bearish structure must remain valid below the invalidation level.",
        )
    return (
        "Price must interact with the Limit Zone.",
        "Structure must remain valid.",
        "Invalidation level must hold.",
    )


def _chart_only_need(value: str) -> bool:
    text = value.lower()
    tokens = text.replace("/", " ").replace("-", " ").replace(".", " ").replace(",", " ").split()
    forbidden = (
        "trust meter",
        " risk/reward",
        "risk reward",
        "score",
        "scoring",
        "opportunity score",
        "quality score",
        "final confluence threshold",
        "scanner threshold",
        "grade",
        "hard rejection",
        "required threshold",
        "quality gate",
        "final quality",
        "core engine",
    )
    return "rr" not in tokens and not any(fragment in text for fragment in forbidden)


def _first_display(*values: Any) -> str:
    for value in values:
        text = _display(value)
        if text != NA:
            return text
    return NA


def _join(*lines: str) -> str:
    return "\n".join(lines)


def _display(value: Any) -> str:
    if value is None or value == "" or value == NA:
        return NA
    if isinstance(value, Mapping):
        return NA
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return NA
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, Decimal):
        text = format(value, "f")
        return text.rstrip("0").rstrip(".") if "." in text else text
    if isinstance(value, bool):
        return NA
    text = " ".join(str(value).split())
    return text if text else NA


def format_telegram_price(value: Any) -> str:
    return _price_display(value)


def format_telegram_rr(value: Any) -> str:
    return _rr_with_unit(value)


def _price_display(value: Any) -> str:
    number = _decimal_value(value)
    if number is None:
        return NA
    places = _price_decimal_places(number)
    quantum = Decimal(1).scaleb(-places)
    rounded = number.quantize(quantum, rounding=ROUND_HALF_UP)
    output = format(rounded, "f")
    return output.rstrip("0").rstrip(".") if "." in output else output


def _price_decimal_places(value: Decimal) -> int:
    magnitude = abs(value)
    if magnitude >= Decimal("1000"):
        return 2
    if magnitude >= Decimal("100"):
        return 2
    if magnitude >= Decimal("10"):
        return 2
    if magnitude >= Decimal("1"):
        return 4
    if magnitude >= Decimal("0.1"):
        return 5
    if magnitude >= Decimal("0.01"):
        return 5
    return 8


def _price_range_text(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    normalized = text.replace(RANGE_DASH, "-").replace(EM_DASH, "-")
    parts = [part.strip() for part in normalized.split("-")]
    if len(parts) != 2:
        return NA
    low = _price_display(parts[0])
    high = _price_display(parts[1])
    if low == NA or high == NA:
        return NA
    return f"{low} {RANGE_DASH} {high}"


def _decimal_value(value: Any) -> Decimal | None:
    if value is None or value == "" or value == NA:
        return None
    if isinstance(value, Mapping):
        return None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return None
    if hasattr(value, "value") and not isinstance(value, (str, int, float, bool, Decimal)):
        value = value.value
    if isinstance(value, bool):
        return None
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _watchlist_planned_rr_line(message: TelegramSignalMessage) -> str:
    rr = _rr_with_unit(message.planned_rr)
    if rr == NA:
        if _watchlist_has_trackable_price_level(message):
            return f"Planned RR: {NA} {EM_DASH} final RR must validate before confirmation."
        return f"Planned RR: {NA}"

    planned_rr = _decimal_value(message.planned_rr)
    min_rr_value = _decimal_value(message.min_rr) or DEFAULT_MIN_RR_DISPLAY
    min_rr = _rr_display(min_rr_value)
    if planned_rr is not None and min_rr_value is not None and planned_rr < min_rr_value:
        return (
            f"Planned RR: {rr} {EM_DASH} watchlist only, final RR must improve "
            f"to {GREATER_EQUAL}{min_rr}R before confirmation."
        )
    return f"Planned RR: {rr}"


def _watchlist_has_trackable_price_level(message: TelegramSignalMessage) -> bool:
    if _price_range_text(message.watch_zone) != NA:
        return True
    return any(
        _price_display(value) != NA
        for value in (
            message.entry_low,
            message.entry_high,
            message.stop_loss,
            message.tp1,
            message.tp2,
            message.tp3,
        )
    )


def _rr_display(value: Any) -> str:
    number = _decimal_value(value)
    if number is None:
        return NA
    rounded = number.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    output = format(rounded, "f")
    return output.rstrip("0").rstrip(".") if "." in output else output


def _rr_with_unit(value: Any) -> str:
    text = _rr_display(value)
    return NA if text == NA else f"{text}R"


__all__ = [
    "FOOTER",
    "HEADER_PREFIX",
    "PUBLIC_STATUS_BY_ALERT_TYPE",
    "TelegramAlertType",
    "TelegramSignalMessage",
    "format_expired_update",
    "format_invalidated_update",
    "format_limit_hit_update",
    "format_no_longer_tracking_update",
    "format_signal_confirmed_alert",
    "format_sl_hit_update",
    "format_telegram_signal_message",
    "format_telegram_price",
    "format_telegram_rr",
    "format_tp1_hit_update",
    "format_tp2_hit_update",
    "format_tp3_hit_update",
    "format_watchlist_alert",
]
