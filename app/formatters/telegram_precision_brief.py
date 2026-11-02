"""Evidence-led public presentation. Never generates or promotes a setup."""

from html import escape
import re
from urllib.parse import urlencode

from app.alerts.telegram_rich_message import pack_rich_message
from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import (
    PUBLIC_TRADE_MAP_RISK_WARNING,
    TelegramAlertType,
    TelegramSignalMessage,
    _coalesced_target_count,
    _decimal_value,
    _edge_structure_kind,
    _effective_signal_context,
    _first_display,
    _has_target_caution,
    _public_compact_invalidation_line,
    _public_edge_entry_line,
    _public_edge_evidence,
    _public_is_invalidated,
    _status_key,
    format_direction,
    format_price,
    format_symbol,
    safe_reason_text,
)


def tradingview_chart_url(symbol: str, exchange: str) -> str | None:
    """Exact perpetual market only; no guessed exchange or custom mobile scheme."""
    venue = str(exchange).strip().upper()
    ticker = str(symbol).strip().upper()
    if venue not in {"BINANCE", "BYBIT"} or not re.fullmatch(r"[A-Z0-9]{2,30}USDT", ticker):
        return None
    return "https://www.tradingview.com/chart/?" + urlencode({"symbol": f"{venue}:{ticker}.P"})


def _text(value: object, limit: int = 180) -> str:
    text = str(value)
    # Bound each escaped field as well as the full persisted payload. Never cut
    # inside an HTML entity or let source text become markup or a button.
    if len(text) > limit:
        text = text[:limit - 1] + "…"
    while len(escape(text, quote=True)) > limit:
        text = text[:-2] + "…"
    return escape(text, quote=True)


def _evidence_lines(message: TelegramSignalMessage) -> tuple[str, ...]:
    evidence = _public_edge_evidence(message)
    if evidence is None:
        return ("Setup evidence: N/A in stored context.",)
    lines = []
    level, wick = _decimal_value(evidence.swept_level), _decimal_value(evidence.sweep_wick)
    direction = str(evidence.sweep_direction).lower()
    if evidence.sweep_present:
        if level is not None and wick is not None and direction in {"bullish", "long", "downside"} and wick < level:
            lines.append(f"Wick {format_price(wick)} swept below the {format_price(level)} swing low.")
        elif level is not None and wick is not None and direction in {"bearish", "short", "upside"} and wick > level:
            lines.append(f"Wick {format_price(wick)} swept above the {format_price(level)} swing high.")
        else:
            lines.append("Sweep detail: Unverified (level, wick or direction unavailable/inconsistent).")
    kind = _edge_structure_kind(evidence.structure_kind)
    level, close = _decimal_value(evidence.structure_level), _decimal_value(evidence.structure_close)
    direction = str(evidence.structure_direction).lower()
    if evidence.structure_present:
        relation = "above" if direction in {"bullish", "long"} else "below" if direction in {"bearish", "short"} else None
        valid = level is not None and close is not None and relation is not None and (
            close > level if relation == "above" else close < level
        )
        if valid and kind != NA:
            lines.append(f"The {evidence.structure_timeframe} {kind} candle closed at {format_price(close)} {relation} {format_price(level)}.")
        else:
            lines.append("Structure detail: Unverified (break level, close or direction unavailable/inconsistent).")
    context = _effective_signal_context(message)
    matching_zone = all(
        _decimal_value(getattr(evidence, name)) == _decimal_value(_first_display(getattr(context, name), getattr(message, name)))
        for name in ("entry_low", "entry_high")
    )
    zone = _public_edge_entry_line(evidence) if matching_zone else "Entry-zone evidence: Unverified (does not match the stored plan)."
    if zone != NA:
        lines.append(zone)
    return tuple(lines) or ("Setup evidence: N/A in stored context.",)


def _levels(message: TelegramSignalMessage) -> tuple[tuple[str, str], ...]:
    context = _effective_signal_context(message)
    def value(name: str) -> str:
        return format_price(_first_display(getattr(context, name), getattr(message, name)))
    low, high = value("entry_low"), value("entry_high")
    entry = low if low == high else f"{low} – {high}"
    return (("ENTRY", entry), ("SL", value("stop_loss")),
            ("TP1", value("tp1")), ("TP2", value("tp2")), ("TP3", value("tp3")))


def _entry_plan(message: TelegramSignalMessage, confirmed: bool) -> str:
    if confirmed:
        context = _effective_signal_context(message)
        low = _decimal_value(_first_display(context.entry_low, message.entry_low))
        high = _decimal_value(_first_display(context.entry_high, message.entry_high))
        if low is None or high is None or low > high:
            return "Entry plan: N/A (stored entry zone unavailable/inconsistent)."
        entry = _levels(message)[0][1]
        direction = format_direction(_first_display(context.direction, message.direction))
        reaction = {"LONG": ("bullish", "below", "higher"), "SHORT": ("bearish", "above", "lower")}.get(direction)
        if reaction is None:
            start = f"Entry zone {entry} engaged." if message.zone_active else f"Wait for price to return to {entry}."
            plan = f"{start} Reaction direction: N/A."
        else:
            bias, adverse_side, movement = reaction
            start = (
                f"Entry zone {entry} engaged. Look for a clear {bias} reaction."
                if message.zone_active else
                f"Wait for price to return to {entry}, then look for a clear {bias} reaction."
            )
            plan = (f"{start} If price stalls in the zone or trades {adverse_side} it without a convincing "
                    f"push {movement}, treat the entry with caution.")
        return f"{plan} Actual exchange fill: Unverified." if message.zone_active else plan
    reason = safe_reason_text(message.confirmation_needed)
    return "Confirmation required: N/A in stored context." if reason == NA else reason


def format_precision_brief(alert_type: TelegramAlertType, message: TelegramSignalMessage) -> str:
    """Return one atomic rich payload, preserving canonical state and risk facts."""
    if alert_type == TelegramAlertType.RESEARCH_WATCH:
        # Research messages remain outside the public signal redesign.
        from app.formatters.telegram_signal_formatter import format_research_watch_message
        return format_research_watch_message(message)
    symbol = format_symbol(message.symbol)
    title = f"🐺 {symbol} · {format_direction(message.direction)}"
    initial = alert_type in {TelegramAlertType.WATCHLIST, TelegramAlertType.SETUP_TRIGGERED, TelegramAlertType.SIGNAL_CONFIRMED}
    if initial and _public_is_invalidated(message):
        return pack_rich_message(f"<h3>{_text(symbol)} · SETUP INVALIDATED</h3><p>{_text(_public_compact_invalidation_line(message))}</p>")
    if not initial:
        return _format_update(alert_type, message)
    context = _effective_signal_context(message)
    lifecycle = _status_key(_first_display(context.lifecycle_state, message.lifecycle_state))
    actionability = _status_key(_first_display(context.actionability_state, message.actionability_state))
    explicitly_pending = lifecycle in {"watch", "watchlisted", "watchlist", "stalking", "triggered", "a_grade_watch"} or actionability in {
        "confirmation_required", "waiting_confirmation", "a_grade_watch", "watch", "stalking", "triggered",
    }
    confirmed = alert_type == TelegramAlertType.SIGNAL_CONFIRMED and not explicitly_pending
    state = "CONFIRMED" if confirmed else "TRIGGERED · CONFIRMATION PENDING" if alert_type == TelegramAlertType.SETUP_TRIGGERED else "WATCH · CONFIRMATION REQUIRED"
    lines = _evidence_lines(message)
    html = f"<h2>{_text(title)}</h2><p><b>{state}</b></p><h4>TRADE MAP</h4><table compact>"
    levels = _levels(message)
    html += "".join(f'<tr><td>{label}</td><td align="right"><code>{_text(price, 64)}</code></td></tr>' for label, price in levels)
    html += "</table>"
    html += "<blockquote><b>WHAT HAPPENED</b><br>" + "<br>".join(_text(line) for line in lines) + "</blockquote>"
    html += f"<h4>ENTRY PLAN</h4><p>{_text(_entry_plan(message, confirmed), 420)}</p>"
    # Keep the engine's price-contact rule; never turn it into a candle-close rule.
    invalidation = _public_compact_invalidation_line(message).removeprefix("Invalidation: ")
    html += f"<h4>INVALIDATION</h4><p>{_text(invalidation)}</p>"
    if _has_target_caution(message):
        caution = safe_reason_text(_first_display(context.target_warning_reason, message.target_warning_reason))
        html += f"<p><b>TP1 priority</b> · {_text(caution if caution != NA else 'Later target path requires caution.')}</p>"
    html += f"<footer>{_text(PUBLIC_TRADE_MAP_RISK_WARNING)}</footer>"
    copy_text = symbol + "\n" + "\n".join(f"{label} {price}" for label, price in levels)
    url = tradingview_chart_url(str(message.symbol), str(message.market_exchange))
    html += '<tg-button-row>'
    if url:
        html += f'<tg-button type="url" style="primary" url="{escape(url, quote=True)}">TradingView ↗</tg-button>'
    # Telegram CopyTextButton limit is 256 characters; omit rather than truncate a level.
    if len(copy_text) <= 256:
        html += f'<tg-button type="copy_text" text="{escape(copy_text, quote=True)}">Copy levels</tg-button>'
    html += '</tg-button-row>'
    return pack_rich_message(html)


def _format_update(alert_type: TelegramAlertType, message: TelegramSignalMessage) -> str:
    symbol = format_symbol(message.symbol)
    labels = {
        TelegramAlertType.LIMIT_HIT: ("🎯", "ENTRY ZONE ENGAGED"),
        TelegramAlertType.TP1_HIT: ("✅", "TP1 HIT"), TelegramAlertType.TP2_HIT: ("✅", "TP2 HIT"),
        TelegramAlertType.TP3_HIT: ("✅", "TP3 HIT"), TelegramAlertType.SL_HIT: ("🛑", "SL HIT"),
        TelegramAlertType.BREAKEVEN: ("⚖️", "REFERENCE BREAKEVEN"), TelegramAlertType.INVALIDATED: ("🛑", "SETUP INVALIDATED"),
        TelegramAlertType.EXPIRED: ("⌛", "WATCH EXPIRED"), TelegramAlertType.NO_LONGER_TRACKING: ("🐺", "TRACKING CLOSED"),
    }
    icon, label = labels[alert_type]
    header = f"{icon} {symbol} · {label}"
    if alert_type == TelegramAlertType.LIMIT_HIT:
        body = f"{_levels(message)[0][1]} touched."
        detail = "Zone contact only; this does not establish an exchange fill."
    elif alert_type in {TelegramAlertType.TP1_HIT, TelegramAlertType.TP2_HIT, TelegramAlertType.TP3_HIT}:
        number = {TelegramAlertType.TP1_HIT: 1, TelegramAlertType.TP2_HIT: 2, TelegramAlertType.TP3_HIT: 3}[alert_type]
        body = f"{format_price(getattr(message, f'tp{number}'))} reached."
        if _coalesced_target_count(message) >= number and number > 1:
            body += " Recorded: " + " · ".join(f"TP{i}" for i in range(1, number + 1)) + "."
        detail = f"Next: TP{number+1} {format_price(getattr(message, f'tp{number+1}'))}." if number < 3 else "Target tracking complete."
    elif alert_type == TelegramAlertType.SL_HIT:
        body, detail = f"Stop {format_price(message.stop_loss)} reached.", "Setup closed at the tracked stop."
    elif alert_type == TelegramAlertType.BREAKEVEN:
        from app.formatters.telegram_signal_formatter import _breakeven_reference_line
        body, detail = _breakeven_reference_line(message), "Reference stop reached after TP1; prior milestones remain recorded. Net outcome depends on partial exits and costs."
    else:
        reason = safe_reason_text(message.invalidation_reason, message.watchlist_invalidation_reason)
        body, detail = reason, "Tracking closed." if alert_type != TelegramAlertType.INVALIDATED else "Setup thesis invalidated."
    return pack_rich_message(f"<h3>{_text(header)}</h3><p>{_text(body, 220)}<br>{_text(detail, 220)}</p><footer>Simulated tracking · actual fills and P&amp;L: Unverified.</footer>")
