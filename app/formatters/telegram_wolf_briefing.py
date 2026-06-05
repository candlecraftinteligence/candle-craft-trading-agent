from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from app.data.dtos import NA

UNVERIFIED = "Unverified"
WOLF_BRIEFING_SIGNATURE = "Candle Craft | Signal. Structure. Execution."
WOLF_FOCUS_DASH = " — "
WOLF_FOCUS_LIMIT = 5

_WATCH_STATE_KEYS = {"stalking", "triggered", "watchlisted", "watchlist", "monitoring"}


@dataclass(frozen=True)
class WolfBriefingFocusItem:
    symbol: str
    status: str
    reason: str = NA


@dataclass(frozen=True)
class WolfBriefingSnapshot:
    market_mood: str = NA
    signal_quality: str = NA
    best_action: str = NA
    active_signal_count: int = 0
    watchlist_count: int = 0
    near_miss_count: int = 0
    rejected_setup_count: int = 0
    focus_items: tuple[WolfBriefingFocusItem, ...] = ()
    run_id: str = NA


def format_wolf_briefing(snapshot: WolfBriefingSnapshot, *, max_focus: int = WOLF_FOCUS_LIMIT) -> str:
    """Format a concise, deterministic Telegram Wolf Briefing."""

    focus_items = snapshot.focus_items[: max(0, min(max_focus, WOLF_FOCUS_LIMIT))]
    focus_lines = [_focus_line(item) for item in focus_items]
    if not focus_lines:
        focus_lines = [NA]

    lines = [
        "🐺🟠 WOLF BRIEFING",
        "",
        f"Market Mood: {_public_text(snapshot.market_mood)}",
        f"Signal quality: {_public_text(snapshot.signal_quality)}",
        f"Best action: {_public_text(snapshot.best_action)}",
        "",
        f"Active signals: {_count_text(snapshot.active_signal_count)}",
        f"Watchlist: {_count_text(snapshot.watchlist_count)}",
        f"Near misses: {_count_text(snapshot.near_miss_count)}",
        f"Rejected setups: {_count_text(snapshot.rejected_setup_count)}",
        "",
        "Focus:",
        *focus_lines,
        "",
        "No forced trades.",
        WOLF_BRIEFING_SIGNATURE,
    ]
    return "\n".join(lines)


def build_wolf_briefing_snapshot(
    *,
    manifest_row: Mapping[str, Any] | None = None,
    scan_payload: Mapping[str, Any] | None = None,
    active_signal_items: Sequence[Any] = (),
    active_signal_count: int | None = None,
    watchlist_items: Sequence[Any] = (),
    watchlist_count: int | None = None,
    max_focus: int = WOLF_FOCUS_LIMIT,
) -> WolfBriefingSnapshot:
    """Build a briefing snapshot from persisted scan/watch artifacts only."""

    manifest = manifest_row or {}
    payload = scan_payload or {}
    rows = _result_rows(payload)
    valid_rows = _valid_rows(rows)
    near_rows = _near_rows(rows)
    rejected_rows = _rejected_rows(rows)
    scan_watch_rows = _scan_watchlist_rows(rows)

    active_count = (
        _safe_count(active_signal_count)
        if active_signal_count is not None
        else _safe_count(_first_value(manifest.get("active_signal_count"), manifest.get("valid_setup_count"), len(valid_rows)))
    )
    watch_count = (
        _safe_count(watchlist_count)
        if watchlist_count is not None
        else _safe_count(_first_value(manifest.get("watchlist_count"), len(scan_watch_rows)))
    )
    near_count = _safe_count(_first_value(manifest.get("near_miss_count"), len(near_rows)))
    rejected_count = _safe_count(_first_value(manifest.get("rejected_count"), len(rejected_rows)))

    focus = _focus_items(
        active_signal_items=active_signal_items,
        watchlist_items=watchlist_items,
        near_rows=near_rows,
        max_focus=max_focus,
    )

    return WolfBriefingSnapshot(
        market_mood=_market_mood(manifest, payload),
        signal_quality=_signal_quality(manifest, payload, active_count, watch_count, near_count, rejected_count),
        best_action=_best_action(manifest, payload, active_count, watch_count, near_count, rejected_count),
        active_signal_count=active_count,
        watchlist_count=watch_count,
        near_miss_count=near_count,
        rejected_setup_count=rejected_count,
        focus_items=focus,
        run_id=_display(_first_value(manifest.get("run_id"), payload.get("run_id"))),
    )


def _focus_items(
    *,
    active_signal_items: Sequence[Any],
    watchlist_items: Sequence[Any],
    near_rows: Sequence[Mapping[str, Any]],
    max_focus: int,
) -> tuple[WolfBriefingFocusItem, ...]:
    limit = max(0, min(max_focus, WOLF_FOCUS_LIMIT))
    output: list[WolfBriefingFocusItem] = []
    seen: set[str] = set()

    for item in active_signal_items:
        symbol = _symbol_text(_attr(item, "symbol"))
        if symbol == NA or symbol in seen:
            continue
        output.append(WolfBriefingFocusItem(symbol=symbol, status="Active signal", reason=_display(_attr(item, "status"))))
        seen.add(symbol)
        if len(output) >= limit:
            return tuple(output)

    for item in watchlist_items:
        symbol = _symbol_text(_attr(item, "symbol"))
        if symbol == NA or symbol in seen:
            continue
        output.append(WolfBriefingFocusItem(symbol=symbol, status="Watchlist", reason=_display(_attr(item, "status"))))
        seen.add(symbol)
        if len(output) >= limit:
            return tuple(output)

    for row in near_rows:
        symbol = _symbol_text(row.get("symbol"))
        if symbol == NA or symbol in seen:
            continue
        output.append(
            WolfBriefingFocusItem(
                symbol=symbol,
                status="Near miss",
                reason=_short_reason(row),
            )
        )
        seen.add(symbol)
        if len(output) >= limit:
            return tuple(output)

    return tuple(output)


def _market_mood(manifest: Mapping[str, Any], payload: Mapping[str, Any]) -> str:
    market_regime = payload.get("market_regime")
    market_mood = _first_value(
        manifest.get("market_mood"),
        manifest.get("market_regime"),
        _mapping_value(market_regime, "state"),
        _mapping_value(market_regime, "market_mood"),
        payload.get("market_mood"),
    )
    return _title_text(market_mood)


def _signal_quality(
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    active_count: int,
    watch_count: int,
    near_count: int,
    rejected_count: int,
) -> str:
    configured = _first_value(
        manifest.get("signal_quality"),
        manifest.get("signal_quality_summary"),
        payload.get("signal_quality"),
        payload.get("signal_quality_summary"),
    )
    if _display(configured) != NA:
        return _public_text(configured)
    if active_count > 0:
        return "Confirmed setups active"
    if watch_count > 0 or near_count > 0:
        return "Selective watch only"
    if rejected_count > 0:
        return "Weak setups rejected"
    return NA


def _best_action(
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    active_count: int,
    watch_count: int,
    near_count: int,
    rejected_count: int,
) -> str:
    configured = _first_value(manifest.get("best_action"), payload.get("best_action"))
    if _display(configured) != NA:
        return _public_text(configured)
    if active_count > 0:
        return "Manual review only"
    if watch_count > 0 or near_count > 0:
        return "Wait for confirmation"
    if rejected_count > 0:
        return "Stand down"
    return NA


def _result_rows(payload: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    rows = payload.get("results")
    if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes, bytearray, Mapping)):
        return ()
    return tuple(row for row in rows if isinstance(row, Mapping))


def _valid_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return _ranked_rows(row for row in rows if _status_key(row.get("display_status")) == "valid_setup" or _status_key(row.get("display_bucket")) == "valid")


def _near_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return _ranked_rows(
        row
        for row in rows
        if _status_key(row.get("display_status")) == "near_miss" or _status_key(row.get("display_bucket")) == "near_miss"
    )


def _rejected_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return _ranked_rows(
        row
        for row in rows
        if _status_key(row.get("display_status")) in {"no_setup", "rejected"}
        or _status_key(row.get("display_bucket")) in {"no_setup", "rejected"}
    )


def _scan_watchlist_rows(rows: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
    return _ranked_rows(
        row
        for row in rows
        if _status_key(row.get("lifecycle_current_state")) in _WATCH_STATE_KEYS
        or _status_key(row.get("display_status")) == "watchlist"
        or _status_key(row.get("display_bucket")) == "watchlist"
    )


def _ranked_rows(rows: Any) -> tuple[Mapping[str, Any], ...]:
    indexed = list(enumerate(rows))
    indexed.sort(key=lambda item: (_rank_value(item[1]), item[0]))
    return tuple(row for _index, row in indexed)


def _rank_value(row: Mapping[str, Any]) -> int:
    try:
        return int(str(row.get("display_rank")))
    except (TypeError, ValueError):
        return 1_000_000


def _short_reason(row: Mapping[str, Any]) -> str:
    return _short_text(
        _first_value(
            row.get("short_reason"),
            row.get("display_reason"),
            row.get("next_trigger_needed"),
            _mapping_value(row.get("near_miss_intelligence"), "next_trigger_needed"),
            row.get("failed_stage"),
        )
    )


def _focus_line(item: WolfBriefingFocusItem) -> str:
    symbol = _symbol_text(item.symbol)
    status = _public_text(item.status)
    reason = _public_text(item.reason)
    if reason == NA or reason.lower() == status.lower():
        detail = status
    else:
        detail = f"{status}: {reason}"
    return _short_text(f"{symbol}{WOLF_FOCUS_DASH}{detail}", max_length=110)


def _count_text(value: Any) -> str:
    return str(_safe_count(value))


def _safe_count(value: Any) -> int:
    try:
        return max(0, int(str(value)))
    except (TypeError, ValueError):
        return 0


def _symbol_text(value: Any) -> str:
    text = _display(value)
    return text.upper() if text != NA else NA


def _title_text(value: Any) -> str:
    text = _public_text(value)
    if text == NA or text == UNVERIFIED:
        return text
    return " ".join(word[:1].upper() + word[1:].lower() for word in text.replace("_", " ").replace("-", " ").split())


def _public_text(value: Any) -> str:
    text = _display(value)
    if text == NA or text == UNVERIFIED:
        return text
    text = re.sub(r"\bregime\b", "market mood", text, flags=re.IGNORECASE)
    return _short_text(text)


def _short_text(value: Any, *, max_length: int = 90) -> str:
    text = " ".join(_display(value).split())
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3].rstrip()}..."


def _display(value: Any) -> str:
    if value is None or value == "":
        return NA
    if value == NA:
        return NA
    if isinstance(value, bool):
        return NA
    if isinstance(value, Mapping):
        return NA
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return NA
    enum_value = getattr(value, "value", None)
    text = str(enum_value if isinstance(enum_value, str) else value).strip()
    if not text or text.upper() in {"NA", "N/A"}:
        return NA
    if text.lower() in {"unverified", "unreliable"}:
        return UNVERIFIED
    return text


def _status_key(value: Any) -> str:
    text = _display(value)
    if text == NA:
        return ""
    key = text.replace("-", "_").replace(" ", "_").lower()
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")


def _first_value(*values: Any) -> Any:
    for value in values:
        if _display(value) != NA:
            return value
    return NA


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key, NA) if isinstance(value, Mapping) else NA


def _attr(value: Any, name: str) -> Any:
    return getattr(value, name, NA)


__all__ = [
    "UNVERIFIED",
    "WOLF_BRIEFING_SIGNATURE",
    "WOLF_FOCUS_DASH",
    "WolfBriefingFocusItem",
    "WolfBriefingSnapshot",
    "build_wolf_briefing_snapshot",
    "format_wolf_briefing",
]
