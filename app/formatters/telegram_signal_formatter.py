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
class SignalMessageContext:
    symbol: Any = NA
    direction: Any = NA
    primary_mode: Any = NA
    secondary_modes: tuple[Any, ...] = ()
    source_modes: tuple[Any, ...] = ()
    confluence_valid: bool = False
    grade: Any = NA
    quality_score: Any = NA
    lifecycle_state: Any = NA
    actionability_state: Any = NA
    entry_low: Any = NA
    entry_high: Any = NA
    stop_loss: Any = NA
    tp1: Any = NA
    tp2: Any = NA
    tp3: Any = NA
    rr: Any = NA
    technical_score: Any = NA
    opportunity_score: Any = NA
    target_integrity_status: Any = NA
    target_failure: Any = NA
    target_failure_severity: Any = NA
    target_warning_reason: Any = NA
    final_failed_gate: Any = NA
    final_block_reason: Any = NA
    invalidation_logic: Any = NA
    why_it_matters_points: tuple[Any, ...] = ()
    what_we_want_next_points: tuple[Any, ...] = ()
    caution_points: tuple[Any, ...] = ()


@dataclass(frozen=True)
class TelegramSignalMessage:
    symbol: Any = NA
    direction: Any = NA
    signal_id: Any = NA
    mode: Any = NA
    primary_mode: Any = NA
    secondary_modes: tuple[Any, ...] = ()
    source_modes: tuple[Any, ...] = ()
    quality: Any = NA
    quality_score: Any = NA
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
    lifecycle_state: Any = NA
    technical_score: Any = NA
    opportunity_score: Any = NA
    regime_state: Any = NA
    regime_compatibility_label: Any = NA
    regime_confidence: Any = NA
    watchlist_status: Any = NA
    actionability_state: Any = NA
    target_integrity_status: Any = NA
    target_failure: Any = NA
    target_failure_severity: Any = NA
    target_warning_reason: Any = NA
    final_failed_gate: Any = NA
    final_block_reason: Any = NA
    invalidation_logic: Any = NA
    why_it_matters_points: tuple[Any, ...] = ()
    what_we_want_next_points: tuple[Any, ...] = ()
    caution_points: tuple[Any, ...] = ()
    signal_context: SignalMessageContext | None = None
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
        return format_simple_public_signal_message(message)
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

    return _format_public_signal_message(message, confirmed=True)


def format_simple_public_signal_message(message: TelegramSignalMessage) -> str:
    return _format_public_signal_message(message, confirmed=False)


def _format_public_signal_message(message: TelegramSignalMessage, *, confirmed: bool) -> str:
    return _join(
        f"{HEADER_PREFIX} {format_symbol(message.symbol)} {EM_DASH} {_public_signal_title(message)}",
        "",
        f"Bias: {format_direction(message.direction)}",
        _public_grade_score_line(message),
        f"Actionability: {_public_actionability_display(message, confirmed=confirmed)}",
        f"RR: {format_rr(message.planned_rr)}",
        "",
        *_public_trade_map_section(message),
        "",
        "\U0001F9E0 Why this setup matters",
        *_public_why_lines(message),
        "",
        "\u26A0\ufe0f Execution notes",
        *_public_execution_note_lines(message, confirmed=confirmed),
        "",
        "\U0001F6AB Invalid if",
        *_public_invalidation_lines(message),
        "",
        "\U0001F440 What we want next",
        *_public_next_lines(message, confirmed=confirmed),
        "",
        FOOTER,
    )

def _effective_signal_context(message: TelegramSignalMessage) -> SignalMessageContext:
    if message.signal_context is not None:
        return message.signal_context
    return SignalMessageContext(
        symbol=message.symbol,
        direction=message.direction,
        primary_mode=_first_display(message.primary_mode, message.mode),
        secondary_modes=message.secondary_modes,
        source_modes=message.source_modes,
        grade=message.quality,
        quality_score=message.quality_score,
        lifecycle_state=message.lifecycle_state,
        actionability_state=message.actionability_state,
        entry_low=message.entry_low,
        entry_high=message.entry_high,
        stop_loss=message.stop_loss,
        tp1=message.tp1,
        tp2=message.tp2,
        tp3=message.tp3,
        rr=message.planned_rr,
        technical_score=message.technical_score,
        opportunity_score=message.opportunity_score,
        target_integrity_status=message.target_integrity_status,
        target_failure=message.target_failure,
        target_failure_severity=message.target_failure_severity,
        target_warning_reason=message.target_warning_reason,
        final_failed_gate=message.final_failed_gate,
        final_block_reason=message.final_block_reason,
        invalidation_logic=_first_display(message.invalidation_logic, message.invalidation_reason, message.watchlist_invalidation_reason),
        why_it_matters_points=message.why_it_matters_points,
        what_we_want_next_points=message.what_we_want_next_points,
        caution_points=message.caution_points,
    )


def _public_signal_title(message: TelegramSignalMessage) -> str:
    context = _effective_signal_context(message)
    source_modes = _canonical_modes(
        context.source_modes,
        message.source_modes,
        context.primary_mode,
        message.primary_mode,
        message.mode,
        context.secondary_modes,
        message.secondary_modes,
    )
    primary = _canonical_mode(_first_display(context.primary_mode, message.primary_mode, message.mode))
    if primary == NA and source_modes:
        primary = source_modes[0]
    if context.confluence_valid and {"scalp", "swing"}.issubset(set(source_modes)):
        return "SCALP + SWING CONFLUENCE SIGNAL"
    if primary == "swing":
        return "SWING SETUP SIGNAL"
    if primary == "scalp":
        return "SCALP SETUP SIGNAL"
    if primary == "challenge":
        return "CHALLENGE SETUP SIGNAL"
    return _signal_title(message)


def _public_grade_score_line(message: TelegramSignalMessage) -> str:
    context = _effective_signal_context(message)
    grade = _quality_display(_first_display(context.grade, message.quality))
    score = _display(_first_display(context.quality_score, message.quality_score))
    return f"Grade: {grade} | Score: {score}"


def _public_actionability_display(message: TelegramSignalMessage, *, confirmed: bool) -> str:
    context = _effective_signal_context(message)
    actionability = _status_key(_first_display(context.actionability_state, message.actionability_state))
    lifecycle = _status_key(_first_display(context.lifecycle_state, message.lifecycle_state))
    watch_status = _status_key(message.watchlist_status)
    failed_gate = _status_key(_first_display(context.final_failed_gate, message.final_failed_gate, message.confirmation_needed))
    if actionability == "a_grade_actionable_target_caution" or _target_caution_points(message):
        return "A-grade target caution"
    if actionability in {"a_grade_actionable", "actionable_a_grade"} or lifecycle == "actionable_a_grade":
        return "Clean A-grade"
    if lifecycle == "confirmed" or confirmed:
        return "Confirmed plan"
    if "waiting_confirmation" in watch_status or "confirmation" in failed_gate or lifecycle in {"watch", "stalking", "triggered"}:
        return "Waiting confirmation"
    if "limit_zone" in failed_gate or "entry_zone" in failed_gate or "pullback" in failed_gate:
        return "Waiting limit zone"
    text = _title_display(_first_display(context.actionability_state, context.lifecycle_state, message.actionability_state))
    return text if text != NA else "Waiting confirmation"


def _public_trade_map_section(message: TelegramSignalMessage) -> tuple[str, ...]:
    missing = _public_trade_map_missing(message)
    header = "\U0001F3AF Trade Map" if not missing else "\U0001F3AF Trade Map (incomplete stored context)"
    lines: list[str] = [header]
    lines.extend(_public_available_trade_map_lines(message))
    if missing:
        lines.append(f"Missing: {', '.join(missing)}")
    return tuple(lines)


def _public_available_trade_map_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    context = _effective_signal_context(message)
    entry_low = _first_display(context.entry_low, message.entry_low)
    entry_high = _first_display(context.entry_high, message.entry_high)
    stop_loss = _first_display(context.stop_loss, message.stop_loss)
    tp1 = _first_display(context.tp1, message.tp1)
    tp2 = _first_display(context.tp2, message.tp2)
    tp3 = _first_display(context.tp3, message.tp3)
    lines: list[str] = []
    entry = _entry_range_values(entry_low, entry_high)
    if entry != NA:
        lines.append(f"Entry: {entry}")
    stop = format_price(stop_loss)
    if stop != NA:
        lines.append(f"Stop: {stop}")
    for label, value in (("TP1", tp1), ("TP2", tp2), ("TP3", tp3)):
        price = format_price(value)
        if price != NA:
            lines.append(f"{label}: {price}")
    return tuple(lines)


def _public_trade_map_missing(message: TelegramSignalMessage) -> tuple[str, ...]:
    context = _effective_signal_context(message)
    missing: list[str] = []
    if _entry_range_values(_first_display(context.entry_low, message.entry_low), _first_display(context.entry_high, message.entry_high)) == NA:
        missing.append("entry zone")
    if format_price(_first_display(context.stop_loss, message.stop_loss)) == NA:
        missing.append("stop")
    for label, value in (
        ("TP1", _first_display(context.tp1, message.tp1)),
        ("TP2", _first_display(context.tp2, message.tp2)),
        ("TP3", _first_display(context.tp3, message.tp3)),
    ):
        if format_price(value) == NA:
            missing.append(label)
    if _rr_display(_first_display(context.rr, message.planned_rr)) == NA:
        missing.append("RR")
    return tuple(missing)


def _entry_range_values(entry_low: Any, entry_high: Any) -> str:
    low = format_price(entry_low)
    high = format_price(entry_high)
    if low == NA or high == NA:
        return NA
    if low == high:
        return low
    return f"{low} {RANGE_DASH} {high}"


def _public_why_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    context = _effective_signal_context(message)
    lines = _public_point_lines(context.why_it_matters_points)
    if lines:
        return lines
    reason = safe_reason_text(message.structure_reason, message.confluence, message.current_context)
    if reason != NA:
        return (f"- {reason}",)
    return ("- Stored public context does not include structured setup rationale.",)


def _public_execution_note_lines(message: TelegramSignalMessage, *, confirmed: bool) -> tuple[str, ...]:
    context = _effective_signal_context(message)
    points: list[Any] = [*context.caution_points, *_target_caution_points(message)]
    actionability = _status_key(_first_display(context.actionability_state, message.actionability_state))
    if actionability not in {"a_grade_actionable", "a_grade_actionable_target_caution", "actionable_a_grade"} and not confirmed:
        points.append("Do not treat this as a clean actionable setup until the pending condition resolves.")
    if not points:
        points.append("Use the planned zone only. No market chase.")
    points.append("\u26A0\ufe0f Manual execution only. Manage risk.")
    return _public_point_lines(tuple(points))


def _public_invalidation_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    return (f"- {_public_invalidation_text(message)}",)


def _public_invalidation_text(message: TelegramSignalMessage) -> str:
    context = _effective_signal_context(message)
    explicit_context = _safe_public_text(context.invalidation_logic) if message.signal_context is not None else NA
    explicit_message = _safe_public_text(message.invalidation_logic)
    for explicit in (explicit_context, explicit_message):
        if explicit != NA:
            return explicit
    stop = format_price(_first_display(context.stop_loss, message.stop_loss))
    direction = _direction_key(_first_display(context.direction, message.direction))
    if stop != NA:
        if direction == "long":
            return f"Invalid if price accepts below {stop}."
        if direction == "short":
            return f"Invalid if price accepts above {stop}."
        return f"Invalid if price accepts beyond {stop}."
    raw = _safe_public_text(_first_display(message.invalidation_reason, message.watchlist_invalidation_reason))
    if raw != NA:
        return raw
    return "Hard invalidation: stop level unavailable in stored context."


def _public_next_lines(message: TelegramSignalMessage, *, confirmed: bool) -> tuple[str, ...]:
    context = _effective_signal_context(message)
    lines = _public_point_lines(context.what_we_want_next_points)
    if lines:
        return lines
    actionability = _status_key(_first_display(context.actionability_state, message.actionability_state))
    lifecycle = _status_key(_first_display(context.lifecycle_state, message.lifecycle_state))
    if actionability == "a_grade_actionable_target_caution":
        return _public_point_lines(("No chase. We want clean entry reaction and fast movement away from chop.",))
    if actionability in {"a_grade_actionable", "actionable_a_grade"} or lifecycle == "actionable_a_grade" or confirmed:
        return _public_point_lines(("Hold entry zone and continue displacement toward TP1.",))
    if lifecycle in {"watch", "watchlisted", "stalking"}:
        return _public_point_lines(("Wait for 5m BOS/CHoCH confirmation after sweep.",))
    if lifecycle == "triggered":
        return _public_point_lines(("Need confirmation candle close and target path expansion.",))
    needs = tuple(_safe_public_text(value) for value in message.needs_next)
    needs = tuple(value for value in needs if value != NA)
    if needs:
        return _public_point_lines(needs)
    return _public_point_lines(("Wait for the next stored confirmation condition before acting.",))


def _target_caution_points(message: TelegramSignalMessage) -> tuple[str, ...]:
    context = _effective_signal_context(message)
    state = _status_key(_first_display(context.actionability_state, message.actionability_state))
    severity = _status_key(_first_display(context.target_failure_severity, message.target_failure_severity))
    warning = _display(_first_display(context.target_warning_reason, message.target_warning_reason))
    warning_key = _status_key(warning)
    caution = (
        state == "a_grade_actionable_target_caution"
        or severity in {"target_caution_actionable", "soft_target_warning"}
        or ("chop" in warning_key or "range" in warning_key)
    )
    if not caution:
        return ()
    warning_detail = () if warning == NA else (warning.rstrip("."),)
    return (
        *warning_detail,
        f"Target path is tighter/choppy {EM_DASH} no chase.",
        "TP1 reaction matters.",
        "Reduce aggression until price clears chop.",
    )


def _public_point_lines(values: Sequence[Any]) -> tuple[str, ...]:
    lines: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = _safe_public_text(value)
        if text == NA:
            continue
        normalized = text.lower()
        if normalized in seen:
            continue
        seen.add(normalized)
        lines.append(f"- {text}")
        if len(lines) == 6:
            break
    return tuple(lines)


def _canonical_modes(*values: Any) -> tuple[str, ...]:
    modes: list[str] = []
    for value in values:
        for mode in _mode_tokens(value):
            if mode not in modes:
                modes.append(mode)
    return tuple(modes)


def _canonical_mode(value: Any) -> str:
    modes = _canonical_modes(value)
    return modes[0] if modes else NA


def _mode_tokens(value: Any) -> tuple[str, ...]:
    if value is None or value == NA or isinstance(value, Mapping):
        return ()
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        tokens: list[str] = []
        for item in value:
            for mode in _mode_tokens(item):
                if mode not in tokens:
                    tokens.append(mode)
        return tuple(tokens)
    text = _display(value)
    if text == NA:
        return ()
    key = text.lower().replace("+", " ").replace(",", " ").replace("/", " ").replace("|", " ")
    key = key.replace("-", "_")
    tokens: list[str] = []
    for mode in ("scalp", "swing", "challenge"):
        parts = key.split()
        if key == mode or mode in parts or key.endswith(f"_{mode}") or f"_{mode}_" in key:
            tokens.append(mode)
    return tuple(tokens)

def _target_caution_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    state = _status_key(message.actionability_state)
    severity = _status_key(message.target_failure_severity)
    warning = _display(message.target_warning_reason)
    warning_key = _status_key(warning)
    caution = (
        state == "a_grade_actionable_target_caution"
        or severity == "target_caution_actionable"
        or (severity == "soft_target_warning" and ("chop" in warning_key or "range" in warning_key))
    )
    if not caution:
        return ()
    detail = "TP2 sits inside recent chop/range" if warning == NA else warning.rstrip(".")
    return (f"Target caution: {detail}, so the path is tighter/choppy. No chase; use the planned zone only.",)


def format_premium_watchlist_message(message: TelegramSignalMessage) -> str:
    side = _direction_key(message.direction)
    hold_level = "support" if side == "long" else "resistance" if side == "short" else "support/resistance"
    structure = "Bullish" if side == "long" else "Bearish" if side == "short" else "Directional"
    valid_side = "above" if side == "long" else "below" if side == "short" else "around"
    limit_zone_hit = _status_key(message.watchlist_status) in {
        "limit_zone_hit_waiting_confirmation",
        "limit_zone_hit_waiting_confirm",
        "limit_zone_hit",
    }
    status = "LIMIT ZONE HIT — WAITING CONFIRMATION" if limit_zone_hit else "WATCHLIST"
    watch_lines = (
        (
            f"{BULLET} Price is in or near the Limit Zone.",
            f"{BULLET} Limit Zone must hold as {hold_level}.",
            f"{BULLET} Wait for clean confirmation before any trade.",
        )
        if limit_zone_hit
        else (
            f"{BULLET} Price must trade into the Limit Zone.",
            f"{BULLET} Limit Zone must hold as {hold_level} after the pullback.",
            f"{BULLET} {structure} structure must remain valid {valid_side} the invalidation level.",
        )
    )
    closing = "We let the market prove it." if limit_zone_hit else "We let the market come to us."
    return _join(
        f"{HEADER_PREFIX} WATCHLIST {EM_DASH} {format_symbol(message.symbol)}",
        "",
        "The wolf is stalking this one.",
        "",
        f"Bias: {format_direction(message.direction)}",
        f"Status: {status}",
        f"Quality: {_quality_display(message.quality)}",
        f"Potential RR: {_watchlist_rr_with_unit(message.planned_rr)}",
        "",
        "\U0001F440 What we want to see",
        *watch_lines,
        "",
        "\U0001F4CD Area of Interest",
        f"Zone: {format_entry_zone(message)}",
        f"Invalid below/above: {_watchlist_invalidation_level(message)}",
        "",
        "No confirmation = no trade.",
        closing,
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
    return format_simple_public_signal_message(message)


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


def _simple_signal_tp_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    lines = [f"TP1: {format_price(message.tp1)}"]
    for label, value in (("TP2", message.tp2), ("TP3", message.tp3)):
        price = format_price(value)
        if price != NA:
            lines.append(f"{label}: {price}")
    return tuple(lines)


def _simple_signal_invalidation_text(message: TelegramSignalMessage) -> str:
    text = safe_invalidation_text(message)
    return text if text != NA else "Hard invalidation: stop level unavailable in stored context."

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
        return f"Invalid if price accepts below {stop}."
    if direction == "short":
        return f"Invalid if price accepts above {stop}."
    return f"Invalid if price accepts beyond {stop}."


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


def _watchlist_rr_with_unit(value: Any) -> str:
    number = _decimal_value(value)
    if number is None:
        return NA
    rounded = number.quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"{format(rounded, 'f')}R"


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
    "SignalMessageContext",
    "TelegramSignalMessage",
    "format_entry_zone",
    "format_expired_update",
    "format_invalidated_update",
    "format_limit_hit_update",
    "format_no_longer_tracking_update",
    "format_premium_lifecycle_update_message",
    "format_premium_public_signal_message",
    "format_premium_watchlist_message",
    "format_simple_public_signal_message",
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
