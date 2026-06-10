from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any

from app.data.dtos import NA

HEADER_PREFIX = "\U0001F43A\U0001F7E0"
FOOTER = "Candle Craft | Signal. Structure. Execution."
BULLET = "\u2022"
RANGE_DASH = "\u2013"
EM_DASH = "\u2014"
DEFAULT_MIN_RR_DISPLAY = Decimal("3")


class TelegramAlertType(str, Enum):
    RESEARCH_WATCH = "RESEARCH_WATCH"
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
    TelegramAlertType.RESEARCH_WATCH: "RESEARCH WATCH",
    TelegramAlertType.WATCHLIST: "WATCHLIST",
    TelegramAlertType.SIGNAL_CONFIRMED: "CONFIRMED",
    TelegramAlertType.LIMIT_HIT: "ENTRY ZONE TOUCHED",
    TelegramAlertType.TP1_HIT: "TP1 HIT",
    TelegramAlertType.TP2_HIT: "TP2 HIT",
    TelegramAlertType.TP3_HIT: "TP3 HIT",
    TelegramAlertType.SL_HIT: "STOP HIT",
    TelegramAlertType.INVALIDATED: "INVALIDATED",
    TelegramAlertType.EXPIRED: "INVALIDATED",
    TelegramAlertType.NO_LONGER_TRACKING: "INVALIDATED",
}


@dataclass(frozen=True)
class TelegramSignalMessage:
    symbol: Any = NA
    direction: Any = NA
    signal_id: Any = NA
    mode: Any = NA
    quality: Any = NA
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
    readiness_score: Any = NA
    regime_state: Any = NA
    regime_compatibility_label: Any = NA
    regime_confidence: Any = NA
    watchlist_outcome: bool = False
    upgraded_from_watchlist: bool = False
    was_watchlist: bool = False


def format_telegram_signal_message(
    alert_type: TelegramAlertType | str,
    message: TelegramSignalMessage,
) -> str:
    normalized = alert_type if isinstance(alert_type, TelegramAlertType) else TelegramAlertType(str(alert_type))
    if normalized == TelegramAlertType.RESEARCH_WATCH:
        return format_research_watch_message(message)
    if normalized == TelegramAlertType.WATCHLIST:
        return format_premium_watchlist_message(message)
    if normalized == TelegramAlertType.SIGNAL_CONFIRMED:
        return format_premium_public_signal_message(message)
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


def format_research_watch_message(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} Research Watch {EM_DASH} {format_symbol(message.symbol)}",
        "",
        f"Quality: {_display(message.quality)}",
        f"Readiness: {_display(message.readiness_score)}",
        f"Regime: {_title_display(message.regime_state)}",
        f"Regime fit: {_title_display(message.regime_compatibility_label)}",
        f"Confidence: {_confidence_display(message.regime_confidence)}",
        "",
        "Why watching:",
        "Regime blocked the setup, but structure is close enough to monitor.",
        "",
        "Next trigger:",
        _research_next_trigger(message),
        "",
        "Trade map:",
        *_research_trade_map_lines(message),
        "",
        FOOTER,
    )


def format_premium_public_signal_message(message: TelegramSignalMessage) -> str:
    if message.upgraded_from_watchlist:
        return format_watchlist_upgraded_message(message)

    reason = safe_reason_text(message.structure_reason, message.confluence)
    return _join(
        f"{HEADER_PREFIX} {_signal_title(message)} {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "The wolf found liquidity.",
        "",
        f"Bias: {format_direction(message.direction)}",
        "Status: CONFIRMED",
        f"Quality: {_quality_display(message.quality)}",
        f"RR: {format_rr(message.planned_rr)}",
        "",
        "\U0001F3AF Trade Map",
        f"Entry Zone: {format_entry_zone(message)}",
        f"Stop: {format_price(message.stop_loss)}",
        *format_tp_lines(message),
        "",
        "\U0001F9E0 Why this setup matters",
        reason,
        f"Now we wait for execution inside the limit zone {EM_DASH} no chase.",
        "",
        "\U0001F6AB Invalid if",
        safe_invalidation_text(message),
        "",
        "\u26A0\ufe0f Manual execution only. Manage risk.",
        "",
        FOOTER,
    )


def format_premium_watchlist_message(message: TelegramSignalMessage) -> str:
    requirements = _confirmation_requirements(message)
    return _join(
        f"\U0001F7E1 CANDLE CRAFT WATCHLIST {EM_DASH} {format_symbol(message.symbol)}",
        "Near-miss setup \u2014 not triggered yet",
        "",
        _watchlist_pending_summary(message),
        "",
        f"Bias: {format_direction(message.direction)}",
        f"Setup: {_watchlist_setup_display(message.mode)}",
        "Status: WATCHLIST - NOT ACTIVE SIGNAL",
        f"Pending: {_watchlist_pending_label(message)}",
        f"Quality: {_quality_display(message.quality)}",
        f"Potential RR: {format_rr(message.planned_rr)}",
        "",
        "\U0001F3AF Trade Map",
        f"Entry/Limit Zone: {format_entry_zone(message)}",
        f"SL: {format_price(message.stop_loss)}",
        f"Invalidation: {safe_invalidation_text(message)}",
        *_watchlist_tp_lines(message),
        "",
        "Passed setup checks",
        "Liquidity sweep, reclaim, structure shift, pullback zone, target map, and RR are intact.",
        "",
        "Awaiting trigger/confirmation",
        requirements,
        "",
        "This is a watchlist idea, not an active signal. Trade only after trigger/confirmation.",
        "",
        FOOTER,
    )


def format_watchlist_upgraded_message(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} WATCHLIST UPGRADED {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "The wolf has confirmation.",
        "",
        "Previous state: WATCHLIST",
        "New state: CONFIRMED SIGNAL",
        f"Bias: {format_direction(message.direction)}",
        f"Quality: {_quality_display(message.quality)}",
        f"RR: {format_rr(message.planned_rr)}",
        "",
        "What changed:",
        safe_reason_text(message.structure_reason, message.confluence),
        "",
        "\U0001F3AF Trade Map",
        f"Entry Zone: {format_entry_zone(message)}",
        f"Stop: {format_price(message.stop_loss)}",
        *format_tp_lines(message),
        "",
        "\U0001F6AB Invalid if",
        safe_invalidation_text(message),
        "",
        "\u26A0\ufe0f Manual execution only. Manage risk.",
        "",
        "Now it becomes execution-ready.",
        "",
        FOOTER,
    )


def format_premium_lifecycle_update_message(
    alert_type: TelegramAlertType | str,
    message: TelegramSignalMessage,
) -> str:
    return format_telegram_signal_message(alert_type, message)


def format_limit_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} ENTRY ZONE TOUCHED {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "Entry zone touched.",
        "",
        "Status: AWAITING FOLLOW-THROUGH",
        f"Direction: {format_direction(message.direction)}",
        f"Quality: {_quality_display(message.quality)}",
        f"Entry Zone: {format_entry_zone(message)}",
        f"Invalidation: {safe_invalidation_text(message)}",
        "",
        "Use the existing published plan only.",
        "No confirmation = no chase.",
        "",
        FOOTER,
    )


def format_tp1_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} TAKE PROFIT HIT {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "First target secured.",
        "",
        f"Direction: {format_direction(message.direction)}",
        f"TP1: {format_price(message.tp1)}",
        "Status: PARTIAL WIN",
        "",
        "Nice execution from the zone.",
        "Risk should now be reduced according to your own plan.",
        "",
        "Next levels:",
        f"TP2: {format_price(message.tp2)}",
        f"TP3: {format_price(message.tp3)}",
        "",
        "The wolf eats step by step.",
        "",
        FOOTER,
    )


def format_tp2_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} TAKE PROFIT HIT {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "The move is developing cleanly.",
        "",
        f"Direction: {format_direction(message.direction)}",
        f"TP2: {format_price(message.tp2)}",
        "Status: STRONG FOLLOW-THROUGH",
        "",
        "Market respected the setup and expanded from our zone.",
        "",
        "Remaining target:",
        f"TP3: {format_price(message.tp3)}",
        "",
        "Discipline pays better than chasing.",
        "",
        FOOTER,
    )


def format_tp3_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} TAKE PROFIT HIT {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "Full target sequence completed.",
        "",
        f"Direction: {format_direction(message.direction)}",
        f"Final Target: {format_price(message.tp3)}",
        "Status: TRADE COMPLETE",
        "",
        "Clean setup. Clean execution. Clean finish.",
        "",
        "The wolf tracked it from liquidity to expansion.",
        "",
        FOOTER,
    )


def format_trade_complete_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} TRADE COMPLETE {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "Full target sequence completed.",
        "",
        f"Direction: {format_direction(message.direction)}",
        "Status: TRADE COMPLETE",
        "",
        "Clean setup. Clean execution. Clean finish.",
        "",
        "The wolf tracked it from liquidity to expansion.",
        "",
        FOOTER,
    )


def format_sl_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} STOP HIT {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "Setup invalidated.",
        "",
        f"Direction: {format_direction(message.direction)}",
        f"Stop: {format_price(message.stop_loss)}",
        "Status: CLOSED",
        "",
        "The market failed to hold the structure, so the idea is no longer valid.",
        "",
        "This is part of the process.",
        "Small controlled losses protect us for the next A-grade opportunity.",
        "",
        FOOTER,
    )


def format_invalidated_update(message: TelegramSignalMessage) -> str:
    if message.was_watchlist:
        return _format_watchlist_invalidated_update(message)
    return _format_signal_invalidated_update(message)


def format_expired_update(message: TelegramSignalMessage) -> str:
    return _format_watchlist_invalidated_update(message)


def format_no_longer_tracking_update(message: TelegramSignalMessage) -> str:
    return _format_watchlist_invalidated_update(message)


def format_public_no_trade_message(message: TelegramSignalMessage, reason: Any = NA) -> str:
    return _join(
        f"{HEADER_PREFIX} NO TRADE {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "The wolf is watching, but not entering.",
        "",
        "Status: NO VALID SETUP",
        f"Reason: {safe_public_rejection_summary(reason)}",
        "",
        "This one does not meet our quality rules yet.",
        "",
        "No confirmation = no trade.",
        "We protect the edge by saying no.",
        "",
        FOOTER,
    )


def _format_watchlist_invalidated_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} WATCHLIST INVALIDATED {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "The wolf walks away.",
        "",
        f"Bias was: {format_direction(message.direction)}",
        "Status: INVALIDATED",
        "",
        "Price failed the required structure and no longer fits our setup rules.",
        "",
        "No forced trades.",
        "No revenge entries.",
        "No weak confirmations.",
        "",
        "We wait for the next clean opportunity.",
        "",
        FOOTER,
    )


def _format_signal_invalidated_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"{HEADER_PREFIX} SIGNAL INVALIDATED {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "The setup is cancelled.",
        "",
        f"Bias was: {format_direction(message.direction)}",
        "Status: INVALIDATED",
        "",
        "Price failed the required structure before clean execution.",
        "",
        "No chase.",
        "No forced entry.",
        "The setup no longer meets Candle Craft rules.",
        "",
        FOOTER,
    )


def format_watchlist_alert(message: TelegramSignalMessage) -> str:
    return format_premium_watchlist_message(message)


def format_signal_confirmed_alert(message: TelegramSignalMessage) -> str:
    return format_premium_public_signal_message(message)


def format_symbol(value: Any) -> str:
    text = _display(value)
    return text.upper() if text != NA else NA


def format_direction(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    key = text.strip().lower()
    if key in {"bullish", "buy", "long"}:
        return "LONG"
    if key in {"bearish", "sell", "short"}:
        return "SHORT"
    return text.upper()


def format_rr(value: Any) -> str:
    return _rr_with_unit(value)


def format_price(value: Any) -> str:
    return _price_display(value)


def format_entry_zone(message: TelegramSignalMessage) -> str:
    return _watch_zone(message) if _watch_zone(message) != NA else _entry_range(message)


def format_tp_lines(message: TelegramSignalMessage) -> tuple[str, str, str]:
    return (
        f"TP1: {format_price(message.tp1)}",
        f"TP2: {format_price(message.tp2)}",
        f"TP3: {format_price(message.tp3)}",
    )


def _watchlist_tp_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    lines: list[str] = []
    for label, value in (("TP1", message.tp1), ("TP2", message.tp2), ("TP3", message.tp3)):
        price = format_price(value)
        if price != NA:
            lines.append(f"{label}: {price}")
    return tuple(lines)


def _watchlist_setup_display(value: Any) -> str:
    mode = _mode_display(value)
    if mode != NA:
        return mode
    text = _display(value)
    return _title_display(text) if text != NA else NA


def _watchlist_market_condition_display(message: TelegramSignalMessage) -> str:
    state = _title_display(message.regime_state)
    fit = _title_display(message.regime_compatibility_label)
    if state != NA and fit != NA:
        return f"{state} / {fit}"
    if state != NA:
        return state
    if fit != NA:
        return fit
    return NA


def _watchlist_pending_summary(message: TelegramSignalMessage) -> str:
    label = _watchlist_pending_label(message)
    if label == "Market/regime condition":
        return "Market condition pending."
    return "Trigger/confirmation pending."


def _watchlist_pending_label(message: TelegramSignalMessage) -> str:
    requirements = _confirmation_requirements(message).lower()
    market = _watchlist_market_condition_display(message)
    timing_tokens = ("confirmation", "trigger", "pullback", "entry", "limit zone", "fvg", "ob", "sweep", "bos", "choch")
    if market != NA and (
        any(token in requirements for token in ("market", "regime", "btc", "eth"))
        or not any(token in requirements for token in timing_tokens)
    ):
        return "Market/regime condition"
    return "Trigger/confirmation"


def safe_reason_text(*values: Any) -> str:
    for value in values:
        text = _safe_public_text(value)
        if text != NA:
            return text
    return NA


def safe_invalidation_text(message: TelegramSignalMessage) -> str:
    text = _safe_public_text(_first_display(message.invalidation_reason, message.watchlist_invalidation_reason))
    if text != NA:
        return text
    stop = format_price(message.stop_loss)
    direction = _direction_key(message.direction)
    if stop == NA:
        return NA
    if direction == "long":
        return f"Price accepts below {stop}."
    if direction == "short":
        return f"Price accepts above {stop}."
    return f"Price accepts beyond {stop}."


def safe_public_rejection_summary(value: Any) -> str:
    text = _safe_public_text(value)
    if text != NA:
        return text
    key = _status_key(value)
    if (
        key in {
            "missing_confirmation_structure_shift",
            "missing_structure_shift",
            "confirmation_missing",
            "no_bos_choch",
        }
        or "missing_confirmation" in key
        or "confirmation_structure" in key
    ):
        return "Confirmation is not clean yet."
    if "rr" in key or "risk_reward" in key:
        return "Reward does not justify the risk yet."
    if "score" in key or "quality" in key or "gate" in key:
        return "Quality is not strong enough yet."
    if "target" in key:
        return "Target path is not clean enough yet."
    if "regime" in key:
        return "Market conditions are not supportive enough yet."
    if "data" in key:
        return "Required data is not clean enough yet."
    return NA


def format_telegram_price(value: Any) -> str:
    return format_price(value)


def format_telegram_rr(value: Any) -> str:
    return format_rr(value)


def _signal_title(message: TelegramSignalMessage) -> str:
    mode = _mode_display(message.mode)
    return "SIGNAL" if mode == NA else f"{mode} SIGNAL"


def _mode_display(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    key = text.lower()
    for mode in ("scalp", "swing", "challenge"):
        if key == mode or key.endswith(f"_{mode}") or f"_{mode}_" in key:
            return mode.upper()
    return NA


def _quality_display(value: Any) -> str:
    text = _display(value)
    return text.upper() if text != NA else NA


def _title_display(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    return " ".join(part.capitalize() for part in text.replace("_", " ").split())


def _confidence_display(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    if text.endswith("/10"):
        return text
    return f"{text}/10"


def _research_next_trigger(message: TelegramSignalMessage) -> str:
    text = _display(message.confirmation_needed)
    if text != NA:
        return text
    values = message.needs_next
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        for value in values:
            text = _display(value)
            if text != NA:
                return text
    return NA


def _research_trade_map_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    if not _research_trade_map_valid(message):
        return (f"{NA} {EM_DASH} waiting for clean confirmation.",)
    lines = [
        f"Direction: {format_direction(message.direction)}",
        f"Entry Zone: {format_entry_zone(message)}",
        f"Stop: {format_price(message.stop_loss)}",
    ]
    for label, value in (("TP1", message.tp1), ("TP2", message.tp2), ("TP3", message.tp3)):
        price = format_price(value)
        if price != NA:
            lines.append(f"{label}: {price}")
    return tuple(lines)


def _research_trade_map_valid(message: TelegramSignalMessage) -> bool:
    direction = _direction_key(message.direction)
    if direction not in {"long", "short"}:
        return False
    entry_low = _decimal_value(message.entry_low)
    entry_high = _decimal_value(message.entry_high)
    stop = _decimal_value(message.stop_loss)
    tp1 = _decimal_value(message.tp1)
    if None in (entry_low, entry_high, stop, tp1):
        return False
    assert entry_low is not None
    assert entry_high is not None
    assert stop is not None
    assert tp1 is not None
    if entry_low > entry_high:
        return False
    if direction == "long":
        return stop < entry_low and tp1 > entry_high
    return stop > entry_high and tp1 < entry_low


def _confirmation_requirements(message: TelegramSignalMessage) -> str:
    lines = _needs_next_lines(message)
    if lines:
        return "\n".join(lines)
    text = safe_reason_text(message.confirmation_needed, message.current_context)
    return text if text != NA else NA


def _watchlist_invalidation_level(message: TelegramSignalMessage) -> str:
    stop = format_price(message.stop_loss)
    if stop != NA:
        return stop
    invalidation = safe_invalidation_text(message)
    return invalidation if invalidation != NA else NA


def _entry_range(message: TelegramSignalMessage) -> str:
    return f"{format_price(message.entry_low)} {RANGE_DASH} {format_price(message.entry_high)}"


def _watch_zone(message: TelegramSignalMessage) -> str:
    watch_zone = _price_range_text(message.watch_zone)
    return watch_zone if watch_zone != NA else _entry_range(message)


def _needs_next_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    lines: list[str] = []
    values = message.needs_next
    if isinstance(values, Sequence) and not isinstance(values, (str, bytes, bytearray)):
        for value in values:
            text = _safe_public_text(value)
            if text != NA and _chart_only_need(text):
                lines.append(f"{BULLET} {text}")
            if len(lines) == 3:
                break
    return tuple(lines)


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
        "hard rejection",
        "required threshold",
        "quality gate",
        "final quality",
        "core engine",
        "first_failed_gate",
        "strategy_diagnostics",
    )
    return "rr" not in tokens and not any(fragment in text for fragment in forbidden)


def _safe_public_text(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return NA
    lowered = text.lower()
    if (
        "decimal(" in lowered
        or "strategy_diagnostics" in lowered
        or "first_failed_gate" in lowered
        or "missing_structure_shift" in lowered
        or "missing_confirmation_structure_shift" in lowered
        or "hard rejection" in lowered
        or "risk/reward" in lowered
        or "opportunity score" in lowered
        or "quality score" in lowered
        or "quality gate" in lowered
        or "below minimum" in lowered
        or "below 80" in lowered
        or "failed gate" in lowered
        or "gate failed" in lowered
        or "{" in text
        or "}" in text
        or lowered in {"true", "false"}
    ):
        return NA
    return text if text.endswith((".", "!", "?")) else f"{text}."


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


def _rr_display(value: Any) -> str:
    number = _decimal_value(value)
    if number is None:
        return NA
    rounded = number.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return format(rounded, "f")


def _rr_with_unit(value: Any) -> str:
    text = _rr_display(value)
    return NA if text == NA else f"{text}R"


def _direction_key(value: Any) -> str:
    text = _display(value).lower()
    if text in {"long", "bullish", "buy"}:
        return "long"
    if text in {"short", "bearish", "sell"}:
        return "short"
    return ""


def _status_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    key = text.lower().strip().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


__all__ = [
    "FOOTER",
    "HEADER_PREFIX",
    "PUBLIC_STATUS_BY_ALERT_TYPE",
    "TelegramAlertType",
    "TelegramSignalMessage",
    "format_entry_zone",
    "format_expired_update",
    "format_invalidated_update",
    "format_limit_hit_update",
    "format_no_longer_tracking_update",
    "format_premium_lifecycle_update_message",
    "format_premium_public_signal_message",
    "format_premium_watchlist_message",
    "format_price",
    "format_public_no_trade_message",
    "format_research_watch_message",
    "format_signal_confirmed_alert",
    "format_sl_hit_update",
    "format_symbol",
    "format_direction",
    "format_rr",
    "format_telegram_signal_message",
    "format_telegram_price",
    "format_telegram_rr",
    "format_tp1_hit_update",
    "format_tp2_hit_update",
    "format_tp3_hit_update",
    "format_trade_complete_update",
    "format_tp_lines",
    "format_watchlist_alert",
    "format_watchlist_upgraded_message",
    "safe_invalidation_text",
    "safe_public_rejection_summary",
    "safe_reason_text",
]
