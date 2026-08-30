from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import Enum
from typing import Any

from app.core.price_precision import quantize_public_price
from app.data.dtos import NA

HEADER_PREFIX = "\U0001F43A\U0001F7E0"
FOOTER = "Candle Craft | Signal. Structure. Execution."
BULLET = "\u2022"
RANGE_DASH = "\u2013"
EM_DASH = "\u2014"
MIDDLE_DOT = "\u00B7"
COMPACT_SEPARATOR = "\u2501" * 14
DEFAULT_MIN_RR_DISPLAY = Decimal("3")


class TelegramAlertType(str, Enum):
    RESEARCH_WATCH = "RESEARCH_WATCH"
    WATCHLIST = "WATCHLIST"
    SETUP_TRIGGERED = "SETUP_TRIGGERED"
    SIGNAL_CONFIRMED = "SIGNAL_CONFIRMED"
    LIMIT_HIT = "LIMIT_HIT"
    TP1_HIT = "TP1_HIT"
    TP2_HIT = "TP2_HIT"
    TP3_HIT = "TP3_HIT"
    SL_HIT = "SL_HIT"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    NO_LONGER_TRACKING = "NO_LONGER_TRACKING"


@dataclass(frozen=True)
class SignalEdgeEvidence:
    """Validated strategy facts that may be projected into the public Edge block."""

    sweep_present: bool = False
    sweep_direction: Any = NA
    swept_level: Any = NA
    sweep_wick: Any = NA
    structure_present: bool = False
    structure_kind: Any = NA
    structure_direction: Any = NA
    structure_timeframe: Any = NA
    structure_level: Any = NA
    structure_close: Any = NA
    selected_zone_type: Any = NA
    fib_aligned: bool = False
    pullback_depth_ratio: Any = NA
    entry_low: Any = NA
    entry_high: Any = NA
    rr_to_tp2: Any = NA


PUBLIC_STATUS_BY_ALERT_TYPE = {
    TelegramAlertType.RESEARCH_WATCH: "RESEARCH WATCH",
    TelegramAlertType.WATCHLIST: "ON THE RADAR",
    TelegramAlertType.SETUP_TRIGGERED: "HUNT ACTIVE — CONFIRMATION PENDING",
    TelegramAlertType.SIGNAL_CONFIRMED: "SIGNAL CONFIRMED",
    TelegramAlertType.LIMIT_HIT: "ZONE ENGAGED",
    TelegramAlertType.TP1_HIT: "TP1 SECURED",
    TelegramAlertType.TP2_HIT: "TP2 SECURED",
    TelegramAlertType.TP3_HIT: "FULL TARGET SEQUENCE COMPLETE",
    TelegramAlertType.SL_HIT: "SETUP INVALIDATED",
    TelegramAlertType.INVALIDATED: "SETUP INVALIDATED",
    TelegramAlertType.EXPIRED: "WATCH EXPIRED",
    TelegramAlertType.NO_LONGER_TRACKING: "NO LONGER TRACKING",
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
    confirmation_timeframe: Any = NA
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
    edge_evidence: SignalEdgeEvidence | None = None
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
    confirmation_timeframe: Any = NA
    target_integrity_status: Any = NA
    target_failure: Any = NA
    target_failure_severity: Any = NA
    target_warning_reason: Any = NA
    final_failed_gate: Any = NA
    final_block_reason: Any = NA
    invalidation_logic: Any = NA
    edge_evidence: SignalEdgeEvidence | None = None
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
    if normalized == TelegramAlertType.SETUP_TRIGGERED:
        return format_triggered_setup_message(message)
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


def format_triggered_setup_message(message: TelegramSignalMessage) -> str:
    return _format_public_signal_message(message, confirmed=False, triggered=True)


def _format_public_signal_message(
    message: TelegramSignalMessage,
    *,
    confirmed: bool,
    triggered: bool = False,
) -> str:
    edge_lines = _public_edge_lines(message)
    state_lines = _public_state_summary_lines(confirmed=confirmed, triggered=triggered)
    intelligence_section = (
        ("\U0001F9E0 INTELLIGENCE", *edge_lines, "", COMPACT_SEPARATOR, "")
        if edge_lines
        else ()
    )
    return _join(
        f"\U0001F43A {format_symbol(message.symbol)} {MIDDLE_DOT} {format_direction(message.direction)} {MIDDLE_DOT} {_public_setup_style(message)}",
        "",
        _public_status_display(message, confirmed=confirmed, triggered=triggered),
        "",
        _public_grade_score_rr_line(message),
        *(("", *state_lines) if state_lines else ()),
        "",
        COMPACT_SEPARATOR,
        "",
        "\U0001F3AF TRADE MAP",
        *_public_compact_trade_map_lines(message),
        "",
        COMPACT_SEPARATOR,
        "",
        *intelligence_section,
        "\u2694\ufe0f EXECUTION",
        *_public_execution_lines(message, confirmed=confirmed, triggered=triggered),
        "",
        _public_compact_invalidation_line(message),
        "",
        _public_closing_line(message, confirmed=confirmed, triggered=triggered),
        "",
        "Not financial advice.",
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
        confirmation_timeframe=message.confirmation_timeframe,
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
        edge_evidence=message.edge_evidence,
        why_it_matters_points=message.why_it_matters_points,
        what_we_want_next_points=message.what_we_want_next_points,
        caution_points=message.caution_points,
    )



def _public_setup_style(message: TelegramSignalMessage) -> str:
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
        return "SCALP/SWING"
    labels = {"scalp": "SCALP", "swing": "SWING", "challenge": "CHALLENGE"}
    if primary in labels:
        return labels[primary]
    mode = _mode_display(message.mode)
    return mode if mode != NA else "SETUP"


def _public_grade_score_rr_line(message: TelegramSignalMessage) -> str:
    context = _effective_signal_context(message)
    grade = _quality_display(_first_display(context.grade, message.quality))
    score = _display(_first_display(context.quality_score, message.quality_score))
    rr = format_rr(_first_display(context.rr, message.planned_rr))
    return f"{grade} {MIDDLE_DOT} Score {score} {MIDDLE_DOT} RR {rr}"


def _public_status_display(
    message: TelegramSignalMessage,
    *,
    confirmed: bool,
    triggered: bool = False,
) -> str:
    if _public_is_invalidated(message):
        return "\U0001F534 SETUP INVALIDATED"
    if confirmed:
        upgrade = f" {MIDDLE_DOT} WATCHLIST UPGRADED" if message.upgraded_from_watchlist else ""
        caution = f" {MIDDLE_DOT} TP1 PRIORITY" if _has_target_caution(message) else ""
        return f"\U0001F7E2 SIGNAL CONFIRMED{upgrade}{caution}"
    if triggered:
        return f"\U0001F7E0 HUNT ACTIVE {EM_DASH} CONFIRMATION PENDING"
    lifecycle = _status_key(_first_display(_effective_signal_context(message).lifecycle_state, message.lifecycle_state))
    if lifecycle == "stalking":
        return f"\U0001F43A WOLF TRACKING {EM_DASH} CONFIRMATION REQUIRED"
    if _has_target_caution(message):
        return f"\U0001F441 ON THE RADAR {MIDDLE_DOT} TP1 PRIORITY"
    if _public_requires_confirmation(message, confirmed=confirmed):
        return f"\U0001F441 ON THE RADAR {EM_DASH} CONFIRMATION REQUIRED"
    return "\U0001F441 ON THE RADAR"


def _public_state_summary_lines(*, confirmed: bool, triggered: bool) -> tuple[str, ...]:
    if triggered:
        return ("The setup has activated, but the final confirmation gate has not been earned.",)
    return ()

def _public_is_invalidated(message: TelegramSignalMessage) -> bool:
    context = _effective_signal_context(message)
    terminal_keys = {
        "invalidated",
        "expired",
        "no_longer_tracking",
        "cancelled",
        "canceled",
        "closed",
        "sl_hit",
        "stop_hit",
    }
    values = (
        context.lifecycle_state,
        message.lifecycle_state,
        message.watchlist_status,
        context.final_failed_gate,
        message.final_failed_gate,
    )
    return any(_status_key(value) in terminal_keys for value in values)


def _public_requires_confirmation(message: TelegramSignalMessage, *, confirmed: bool) -> bool:
    if confirmed:
        return False
    context = _effective_signal_context(message)
    actionability = _status_key(_first_display(context.actionability_state, message.actionability_state))
    lifecycle = _status_key(_first_display(context.lifecycle_state, message.lifecycle_state))
    watch_status = _status_key(message.watchlist_status)
    failed_gate = _status_key(_first_display(context.final_failed_gate, message.final_failed_gate, message.confirmation_needed))
    if actionability in {"a_grade_actionable", "a_grade_actionable_target_caution", "actionable_a_grade"}:
        return False
    if lifecycle in {"confirmed", "actionable_a_grade", "active", "executing", "managing"}:
        return False
    if lifecycle in {"watch", "watchlist", "watchlisted", "stalking", "triggered", "a_grade_watch"}:
        return True
    if "waiting_confirmation" in watch_status or "confirmation" in failed_gate:
        return True
    return True


def _has_target_caution(message: TelegramSignalMessage) -> bool:
    context = _effective_signal_context(message)
    state = _status_key(_first_display(context.actionability_state, message.actionability_state))
    severity = _status_key(_first_display(context.target_failure_severity, message.target_failure_severity))
    warning = _status_key(_first_display(context.target_warning_reason, message.target_warning_reason))
    target_status = _status_key(_first_display(context.target_integrity_status, message.target_integrity_status))
    failure = _status_key(_first_display(context.target_failure, message.target_failure, context.final_failed_gate, message.final_failed_gate))
    return (
        state == "a_grade_actionable_target_caution"
        or severity in {"target_caution_actionable", "soft_target_warning"}
        or target_status in {"warning", "soft_warning"}
        or "target_inside_chop" in failure
        or "chop" in warning
        or "range" in warning
    )


def _public_compact_trade_map_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    context = _effective_signal_context(message)
    entry = _entry_range_values(_first_display(context.entry_low, message.entry_low), _first_display(context.entry_high, message.entry_high))
    stop = format_price(_first_display(context.stop_loss, message.stop_loss))
    tp1 = format_price(_first_display(context.tp1, message.tp1))
    tp2 = format_price(_first_display(context.tp2, message.tp2))
    tp3 = format_price(_first_display(context.tp3, message.tp3))
    return (
        f"Entry: {entry if entry != NA else NA}",
        f"SL: {stop if stop != NA else NA}",
        "",
        f"TP1: {tp1 if tp1 != NA else NA}",
        f"TP2: {tp2 if tp2 != NA else NA}",
        f"TP3: {tp3 if tp3 != NA else NA}",
    )


def _public_edge_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    evidence = _public_edge_evidence(message)
    lines: list[str] = []
    liquidity = _public_edge_liquidity_line(message, evidence=evidence)
    if liquidity != NA:
        lines.append(liquidity)
    structure = _public_edge_structure_line(evidence)
    if structure != NA and structure not in lines:
        lines.append(structure)
    entry = _public_edge_entry_line(evidence)
    if entry != NA and entry not in lines:
        lines.append(entry)
    rr = _public_edge_rr_line(evidence)
    if rr != NA and rr not in lines:
        lines.append(rr)
    return tuple(lines[:4])


def _public_edge_evidence(message: TelegramSignalMessage) -> SignalEdgeEvidence | None:
    context = _effective_signal_context(message)
    return context.edge_evidence or message.edge_evidence


def _public_edge_liquidity_line(
    message: TelegramSignalMessage,
    *,
    evidence: SignalEdgeEvidence | None = None,
) -> str:
    if evidence is not None:
        if not evidence.sweep_present:
            return NA
        side = _edge_sweep_side(evidence.sweep_direction, message.direction)
        if side == NA:
            return NA
        level = format_price(evidence.swept_level)
        wick = format_price(evidence.sweep_wick)
        close_side = "above" if side == "downside" else "below"
        if level != NA and wick != NA and wick != level:
            return (
                f"Price swept {side} liquidity at {level} with a wick to {wick}, "
                f"then closed back {close_side} the level."
            )
        if level != NA:
            return f"Price swept {side} liquidity at {level} and closed back {close_side} the level."
        return f"Price swept {side} liquidity and closed back inside the prior structure."

    return NA


def _edge_sweep_side(sweep_direction: Any, message_direction: Any) -> str:
    key = _status_key(sweep_direction)
    if key in {"bullish", "long", "downside"}:
        return "downside"
    if key in {"bearish", "short", "upside"}:
        return "upside"
    direction = _direction_key(message_direction)
    return "downside" if direction == "long" else "upside" if direction == "short" else NA


def _public_edge_structure_line(evidence: SignalEdgeEvidence | None) -> str:
    if evidence is None or not evidence.structure_present:
        return NA
    kind = _edge_structure_kind(evidence.structure_kind)
    if kind == NA:
        return NA
    direction = _status_key(evidence.structure_direction)
    direction_word = (
        "bullish"
        if direction in {"bullish", "long"}
        else "bearish"
        if direction in {"bearish", "short"}
        else NA
    )
    timeframe = _display(evidence.structure_timeframe)
    prefix = " ".join(part for part in (timeframe, direction_word, kind) if part != NA)
    if not prefix:
        return NA
    level = format_price(evidence.structure_level)
    close = format_price(evidence.structure_close)
    if level != NA and close != NA and direction_word != NA:
        relation = "above" if direction_word == "bullish" else "below"
        return f"{prefix} closed {relation} {level} at {close}."
    if level != NA and direction_word != NA:
        relation = "above" if direction_word == "bullish" else "below"
        return f"{prefix} confirmed through {level} with a body close {relation} structure."
    return f"{prefix} confirmed the structure shift."


def _edge_structure_kind(value: Any) -> str:
    key = _status_key(value)
    if key == "bos":
        return "BOS"
    if key in {"choch", "ch_och"}:
        return "CHoCH"
    if key == "mss":
        return "MSS"
    return NA


def _public_edge_entry_line(evidence: SignalEdgeEvidence | None) -> str:
    if evidence is None:
        return NA
    zone = _edge_zone_label(evidence.selected_zone_type)
    fib = bool(evidence.fib_aligned)
    if zone == NA and not fib:
        return NA
    entry = _entry_range_values(evidence.entry_low, evidence.entry_high)
    subject = f"Entry {entry}" if entry != NA else "The mapped entry"
    if zone != NA and fib:
        line = f"{subject} overlaps the selected {zone} and the validated fib pullback zone"
    elif zone != NA:
        line = f"{subject} overlaps the selected {zone}"
    else:
        line = f"{subject} is aligned with the validated fib pullback zone"
    depth = _edge_pullback_depth(evidence.pullback_depth_ratio)
    if depth != NA:
        line += f" at a {depth} retracement"
    return f"{line}."


def _edge_zone_label(value: Any) -> str:
    key = _status_key(value)
    if key in {"ob_fvg_overlap", "ob_fvg", "ob_fvg_valid", "order_block_fvg_overlap"}:
        return "OB/FVG overlap"
    if key in {"ob", "ob_valid", "order_block", "order_block_valid"}:
        return "order block"
    if key in {"fvg", "fvg_valid", "fair_value_gap", "fair_value_gap_valid", "imbalance"}:
        return "FVG"
    return NA


def _edge_pullback_depth(value: Any) -> str:
    decimal = _decimal_value(value)
    if decimal is None or decimal < 0 or decimal > 1:
        return NA
    return f"{decimal.quantize(Decimal('0.01'), rounding=ROUND_HALF_UP):f}"


def _public_edge_rr_line(evidence: SignalEdgeEvidence | None) -> str:
    if evidence is None:
        return NA
    rr = format_rr(evidence.rr_to_tp2)
    if rr == NA:
        return NA
    return f"The stored plan provides {rr} to TP2."


def _public_execution_lines(
    message: TelegramSignalMessage,
    *,
    confirmed: bool,
    triggered: bool = False,
) -> tuple[str, ...]:
    if triggered:
        return (
            "Wait for final confirmation.",
            "Do not enter blindly or chase price.",
        )
    if _public_requires_confirmation(message, confirmed=confirmed):
        return (
            "Wait for confirmation.",
            "No entry until structure accepts back through the trigger zone.",
        )
    lines = ["No chase outside the mapped zone."]
    if _has_target_caution(message):
        lines.append("TP1 reaction matters because TP2/TP3 path is choppy.")
    return tuple(lines[:2])


def _public_compact_invalidation_line(message: TelegramSignalMessage) -> str:
    context = _effective_signal_context(message)
    stop = format_price(_first_display(context.stop_loss, message.stop_loss))
    direction = _direction_key(_first_display(context.direction, message.direction))
    if stop != NA:
        side = "below" if direction == "long" else "above" if direction == "short" else "beyond"
        return f"Invalid if price body-closes and accepts {side} {stop}."
    explicit = _public_invalidation_text(message)
    if explicit != NA and "stop level unavailable" not in explicit.lower():
        return explicit
    return "Invalidation: N/A."


def _public_closing_line(
    message: TelegramSignalMessage,
    *,
    confirmed: bool,
    triggered: bool,
) -> str:
    if triggered:
        return "\U0001F43A Territory reached. Structure decides what happens next."
    if confirmed:
        evidence = _public_edge_evidence(message)
        if evidence is not None and evidence.sweep_present and evidence.structure_present:
            return "\U0001F43A Liquidity taken. Structure confirmed. Hunt active."
        return "\U0001F43A Signal confirmed. Execution stays disciplined."
    return "\U0001F43A On the radar. Patience protects the edge."

def _entry_range_values(entry_low: Any, entry_high: Any) -> str:
    low = format_price(entry_low)
    high = format_price(entry_high)
    if low == NA or high == NA:
        return NA
    if low == high:
        return low
    return f"{low} {RANGE_DASH} {high}"


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
            return f"Invalid if price body-closes and accepts below {stop}."
        if direction == "short":
            return f"Invalid if price body-closes and accepts above {stop}."
        return f"Invalid if price body-closes and accepts beyond {stop}."
    raw = _safe_public_text(_first_display(message.invalidation_reason, message.watchlist_invalidation_reason))
    if raw != NA:
        return raw
    return "Hard invalidation: stop level unavailable in stored context."


def _target_caution_points(message: TelegramSignalMessage) -> tuple[str, ...]:
    if not _has_target_caution(message):
        return ()
    context = _effective_signal_context(message)
    warning = _display(_first_display(context.target_warning_reason, message.target_warning_reason))
    warning_detail = () if warning == NA else (warning.rstrip("."),)
    return (*warning_detail, "TP1 reaction matters because TP2/TP3 path is choppy.")

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
    if not _has_target_caution(message):
        return ()
    return ("TP1 reaction matters because TP2/TP3 path is choppy.",)

def format_premium_watchlist_message(message: TelegramSignalMessage) -> str:
    return _format_public_signal_message(message, confirmed=False)


def format_watchlist_upgraded_message(message: TelegramSignalMessage) -> str:
    return _format_public_signal_message(message, confirmed=True)


def format_premium_lifecycle_update_message(
    alert_type: TelegramAlertType | str,
    message: TelegramSignalMessage,
) -> str:
    return format_telegram_signal_message(alert_type, message)


def format_limit_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"\U0001F43A {format_symbol(message.symbol)} {MIDDLE_DOT} {format_direction(message.direction)}",
        "",
        "\U0001F3AF ZONE ENGAGED",
        "",
        "Price has entered the mapped territory.",
        "",
        f"Entry: {format_entry_zone(message)}",
        f"Quality: {_quality_display(message.quality)}",
        "",
        "\U0001F7E0 STATUS: REACTION REQUIRED",
        "",
        "The setup is alive, but confirmation has not been earned yet.",
        "",
        "Use the existing published plan only.",
        _public_compact_invalidation_line(message),
        "",
        "\U0001F43A The wolf is in position. No confirmation = no chase.",
        "",
        FOOTER,
    )


def format_tp1_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"\u2705 {format_symbol(message.symbol)} {MIDDLE_DOT} TP1 SECURED",
        "",
        "First objective reached.",
        "",
        f"TP1: {format_price(message.tp1)}",
        "",
        "The setup is progressing according to the stored plan.",
        "",
        "Next:",
        f"TP2: {format_price(message.tp2)}",
        f"TP3: {format_price(message.tp3)}",
        "",
        "\U0001F43A First target secured. The hunt continues.",
        "",
        FOOTER,
    )


def format_tp2_hit_update(message: TelegramSignalMessage) -> str:
    context = _effective_signal_context(message)
    rr = format_rr(_first_display(context.rr, message.planned_rr))
    rr_line = () if rr == NA else (f"RR to TP2: {rr}",)
    return _join(
        f"\U0001F525 {format_symbol(message.symbol)} {MIDDLE_DOT} TP2 SECURED",
        "",
        "Second objective reached.",
        "",
        f"TP2: {format_price(message.tp2)}",
        *rr_line,
        "",
        "Strong follow-through from the mapped setup.",
        "",
        "Remaining:",
        f"TP3: {format_price(message.tp3)}",
        "",
        "\U0001F43A Second target secured. Momentum remains with the plan.",
        "",
        FOOTER,
    )


def format_tp3_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"\u2705 {format_symbol(message.symbol)} {MIDDLE_DOT} FULL TARGET SEQUENCE COMPLETE",
        "",
        f"TP3: {format_price(message.tp3)}",
        "",
        "The stored target sequence has completed.",
        "",
        "\U0001F43A Full target sequence secured. Hunt complete.",
        "",
        FOOTER,
    )


def format_trade_complete_update(message: TelegramSignalMessage) -> str:
    return format_tp3_hit_update(message)


def format_sl_hit_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"\U0001F534 {format_symbol(message.symbol)} {MIDDLE_DOT} SETUP INVALIDATED",
        "",
        f"Stop: {format_price(message.stop_loss)}",
        "",
        "Price failed the structural thesis and the setup is closed.",
        "",
        "Result: SL",
        "",
        "\U0001F9E0 Outcome remains part of lifecycle and expectancy tracking.",
        "",
        "No revenge. No reinterpretation. Next setup.",
        "",
        FOOTER,
    )


def format_invalidated_update(message: TelegramSignalMessage) -> str:
    if message.was_watchlist:
        return _format_watchlist_invalidated_update(message)
    return _format_signal_invalidated_update(message)


def format_expired_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"\U0001F441 {format_symbol(message.symbol)} {MIDDLE_DOT} WATCH EXPIRED",
        "",
        "The mapped opportunity expired before confirmation.",
        "",
        "No entry was confirmed.",
        "No setup was forced.",
        "",
        "\U0001F43A Timing passed. The edge was not chased.",
        "",
        FOOTER,
    )


def format_no_longer_tracking_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"\U0001F441 {format_symbol(message.symbol)} {MIDDLE_DOT} NO LONGER TRACKING",
        "",
        "The setup no longer qualifies for active monitoring.",
        "",
        "No entry was confirmed.",
        "No weak setup was carried forward.",
        "",
        "\U0001F43A The radar clears when the edge fades.",
        "",
        FOOTER,
    )


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
        f"\U0001F534 {format_symbol(message.symbol)} {MIDDLE_DOT} WATCHLIST INVALIDATED",
        "",
        "The watchlist thesis no longer meets the required structure.",
        "",
        "No entry was confirmed.",
        "No chase.",
        "No weak confirmations.",
        "",
        "\U0001F43A The wolf walks away when the edge disappears.",
        "",
        FOOTER,
    )


def _format_signal_invalidated_update(message: TelegramSignalMessage) -> str:
    return _join(
        f"\U0001F534 {format_symbol(message.symbol)} {MIDDLE_DOT} SETUP INVALIDATED",
        "",
        "The required structure no longer supports the original thesis.",
        "",
        "No chase.",
        "No forced entry.",
        "No weak confirmation.",
        "",
        "\U0001F43A The wolf walks away when the edge disappears.",
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
        return f"Invalid if price body-closes and accepts below {stop}."
    if direction == "short":
        return f"Invalid if price body-closes and accepts above {stop}."
    return f"Invalid if price body-closes and accepts beyond {stop}."


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
    rounded = quantize_public_price(number)
    output = format(rounded, "f")
    return output.rstrip("0").rstrip(".") if "." in output else output


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
    "SignalEdgeEvidence",
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
