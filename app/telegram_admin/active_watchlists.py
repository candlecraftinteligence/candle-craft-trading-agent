from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import RANGE_DASH, TelegramAlertType, format_telegram_price

ACTIVE_WATCHLIST_DISPLAY_LIMIT = 10
WATCHLIST_STATUS_WAITING = "Waiting for Limit Zone"
WATCHLIST_STATUS_LIMIT_HIT = "LIMIT ZONE HIT"
WATCHLIST_STATUS_TP1_HIT = "TP1 HIT"
WATCHLIST_STATUS_TP2_HIT = "TP2 HIT"

_WATCHLIST_TYPE = TelegramAlertType.WATCHLIST.value
_LIMIT_TYPE = TelegramAlertType.LIMIT_HIT.value
_TP1_TYPE = TelegramAlertType.TP1_HIT.value
_TP2_TYPE = TelegramAlertType.TP2_HIT.value
_TP3_TYPE = TelegramAlertType.TP3_HIT.value
_SL_TYPE = TelegramAlertType.SL_HIT.value
_ACTIVE_QUERY_TYPES = (_WATCHLIST_TYPE, _LIMIT_TYPE, _TP1_TYPE, _TP2_TYPE, _TP3_TYPE, _SL_TYPE)
_TERMINAL_OUTCOME_TYPES = {_TP3_TYPE, _SL_TYPE}
_LEVEL_COLUMNS = ("entry_low", "entry_high", "stop_loss", "tp1", "tp2", "tp3")


@dataclass(frozen=True)
class ActiveWatchlistItem:
    signal_id: str
    symbol: str
    direction: str
    sent_at: str
    status: str
    entry_low: str = NA
    entry_high: str = NA
    stop_loss: str = NA
    tp1: str = NA
    tp2: str = NA
    tp3: str = NA
    hit_alert_types: frozenset[str] = frozenset()
    sort_id: int = 0

    @property
    def limit_zone_text(self) -> str:
        return _zone_text(self.entry_low, self.entry_high)


@dataclass(frozen=True)
class ActiveWatchlistQueryResult:
    source_available: bool
    items: tuple[ActiveWatchlistItem, ...] = ()
    total: int = 0


def load_active_public_watchlists(
    *,
    project_root: Path | str,
    database_path: Path | str,
    limit: int = ACTIVE_WATCHLIST_DISPLAY_LIMIT,
) -> ActiveWatchlistQueryResult:
    """Read active public WATCHLIST alerts without mutating local scan databases."""

    root = Path(project_root)
    preferred_path = _resolve_project_path(root, database_path)
    selected_path = _latest_watchlist_database(root, preferred_path)
    if selected_path is None:
        return ActiveWatchlistQueryResult(source_available=False)

    try:
        with _connect_readonly(selected_path) as connection:
            rows = _sent_alert_attempt_rows(connection)
            items = _active_items_from_rows(connection, rows)
    except (OSError, sqlite3.Error):
        return ActiveWatchlistQueryResult(source_available=False)

    total = len(items)
    visible = tuple(items[: max(1, limit)])
    return ActiveWatchlistQueryResult(source_available=True, items=visible, total=total)


def format_active_watchlist_lines(result: ActiveWatchlistQueryResult) -> list[str]:
    if not result.source_available:
        return ["No local watchlist data found yet. Start the scanner first."]
    if result.total == 0:
        return ["No active public watchlists right now."]

    lines: list[str] = []
    for item in result.items:
        if lines:
            lines.append("")
        lines.extend(
            (
                f"{item.symbol} | {item.direction}",
                f"Limit Zone: {item.limit_zone_text}",
                f"SL: {item.stop_loss}",
                f"Status: {item.status}",
            )
        )
        if item.status != WATCHLIST_STATUS_WAITING:
            lines.extend(_target_progress_lines(item))
    if result.total > len(result.items):
        lines.extend(("", f"Showing {len(result.items)} of {result.total} active watchlists."))
    return lines


def _latest_watchlist_database(project_root: Path, preferred_path: Path) -> Path | None:
    if _has_telegram_alert_attempts(preferred_path):
        return preferred_path

    scan_dir = project_root / "scan_runs"
    if not scan_dir.exists():
        return None

    candidates = sorted(scan_dir.glob("*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True)
    for candidate in candidates:
        if candidate.resolve() == preferred_path.resolve():
            continue
        if _has_telegram_alert_attempts(candidate):
            return candidate
    return None


def _has_telegram_alert_attempts(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        with _connect_readonly(path) as connection:
            return _table_exists(connection, "telegram_alert_attempts")
    except (OSError, sqlite3.Error):
        return False


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _sent_alert_attempt_rows(connection: sqlite3.Connection) -> tuple[Mapping[str, Any], ...]:
    columns = _table_columns(connection, "telegram_alert_attempts")
    required = {"id", "signal_id", "symbol", "direction", "alert_type", "sent_at", "telegram_status"}
    if not required <= columns:
        return ()
    select_columns = [
        "id",
        "signal_id",
        "symbol",
        "direction",
        "alert_type",
        "sent_at",
        "telegram_status",
        _select_or_na("scan_run_id", columns),
        _select_or_na("price_level", columns),
        *(_select_or_na(column, columns) for column in _LEVEL_COLUMNS),
    ]
    placeholders = ",".join("?" for _ in _ACTIVE_QUERY_TYPES)
    rows = connection.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM telegram_alert_attempts
        WHERE telegram_status = 'sent'
          AND alert_type IN ({placeholders})
        ORDER BY id ASC
        """,
        _ACTIVE_QUERY_TYPES,
    ).fetchall()
    return tuple(dict(row) for row in rows)


def _active_items_from_rows(
    connection: sqlite3.Connection,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[ActiveWatchlistItem, ...]:
    by_signal: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        signal_id = _clean(row.get("signal_id"))
        if signal_id == NA:
            continue
        by_signal.setdefault(signal_id, []).append(row)

    items: list[ActiveWatchlistItem] = []
    for signal_id, signal_rows in by_signal.items():
        watch_rows = [row for row in signal_rows if _clean(row.get("alert_type")) == _WATCHLIST_TYPE]
        if not watch_rows:
            continue
        watch_row = max(watch_rows, key=_row_id)
        outcome_rows = [row for row in signal_rows if _clean(row.get("alert_type")) != _WATCHLIST_TYPE]
        if any(_clean(row.get("alert_type")) in _TERMINAL_OUTCOME_TYPES for row in outcome_rows):
            continue
        symbol = _symbol_text(watch_row.get("symbol"))
        direction = _direction_text(watch_row.get("direction"))
        if symbol == NA or direction == NA:
            continue
        hit_alert_types = frozenset(_clean(row.get("alert_type")) for row in outcome_rows)
        levels = _levels_for_watchlist(connection, watch_row, outcome_rows)
        items.append(
            ActiveWatchlistItem(
                signal_id=signal_id,
                symbol=symbol,
                direction=direction,
                sent_at=_clean(watch_row.get("sent_at")),
                status=_status_from_outcomes(hit_alert_types),
                hit_alert_types=hit_alert_types,
                sort_id=_row_id(watch_row),
                **levels,
            )
        )
    items.sort(key=lambda item: item.sort_id, reverse=True)
    return tuple(items)


def _levels_for_watchlist(
    connection: sqlite3.Connection,
    watch_row: Mapping[str, Any],
    outcome_rows: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    levels = {column: NA for column in _LEVEL_COLUMNS}
    _fill_levels_from_row(levels, watch_row)
    _fill_zone_from_text(levels, watch_row.get("price_level"))

    for row in reversed(tuple(outcome_rows)):
        _fill_levels_from_row(levels, row)
        if _clean(row.get("alert_type")) == _LIMIT_TYPE:
            _fill_zone_from_text(levels, row.get("price_level"))

    candidate_levels = _candidate_levels(connection, watch_row)
    for key, value in candidate_levels.items():
        if levels.get(key, NA) == NA:
            levels[key] = value
    return levels


def _fill_levels_from_row(levels: dict[str, str], row: Mapping[str, Any]) -> None:
    for column in _LEVEL_COLUMNS:
        if levels[column] == NA:
            levels[column] = _price_text(row.get(column))
    _normalize_single_level_zone(levels)


def _fill_zone_from_text(levels: dict[str, str], value: Any) -> None:
    low, high = _parse_zone_text(value)
    if low == NA and high == NA:
        return
    if levels["entry_low"] == NA:
        levels["entry_low"] = low if low != NA else high
    if levels["entry_high"] == NA:
        levels["entry_high"] = high if high != NA else low
    _normalize_single_level_zone(levels)


def _candidate_levels(connection: sqlite3.Connection, watch_row: Mapping[str, Any]) -> dict[str, str]:
    if not _table_exists(connection, "setup_candidates"):
        return {}
    columns = _table_columns(connection, "setup_candidates")
    if not {"symbol", "direction"} <= columns:
        return {}

    select_columns = [
        _select_or_na("id", columns),
        _select_or_na("run_id", columns),
        "symbol",
        "direction",
        _select_or_na("entry", columns),
        _select_or_na("stop", columns),
        _select_or_na("tp1", columns),
        _select_or_na("tp2", columns),
        _select_or_na("tp3", columns),
        _select_or_na("raw_candidate_json", columns),
    ]
    scan_run_id = _clean(watch_row.get("scan_run_id"))
    if "run_id" in columns and scan_run_id != NA:
        order_clause = "ORDER BY CASE WHEN run_id = ? THEN 0 ELSE 1 END, id DESC"
        params: tuple[Any, ...] = (_clean(watch_row.get("symbol")), _clean(watch_row.get("direction")), scan_run_id)
    else:
        order_clause = "ORDER BY id DESC" if "id" in columns else ""
        params = (_clean(watch_row.get("symbol")), _clean(watch_row.get("direction")))
    row = connection.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM setup_candidates
        WHERE UPPER(symbol) = UPPER(?)
          AND UPPER(direction) = UPPER(?)
        {order_clause}
        LIMIT 1
        """,
        params,
    ).fetchone()
    if row is None:
        return {}
    return _levels_from_candidate_row(dict(row))


def _levels_from_candidate_row(row: Mapping[str, Any]) -> dict[str, str]:
    raw = _json_mapping(row.get("raw_candidate_json"))
    levels = {column: NA for column in _LEVEL_COLUMNS}

    _fill_zone_from_mapping(levels, raw)
    _fill_zone_from_text(levels, raw.get("watch_zone"))
    _fill_zone_from_text(levels, raw.get("entry_zone"))
    _fill_zone_from_text(levels, row.get("entry"))

    levels["stop_loss"] = _first_price(raw.get("stop_loss"), raw.get("stop"), row.get("stop"))
    levels["tp1"] = _first_price(raw.get("tp1"), _target_from_raw(raw, 1), row.get("tp1"))
    levels["tp2"] = _first_price(raw.get("tp2"), _target_from_raw(raw, 2), row.get("tp2"))
    levels["tp3"] = _first_price(raw.get("tp3"), _target_from_raw(raw, 3), row.get("tp3"))
    return levels


def _fill_zone_from_mapping(levels: dict[str, str], raw: Mapping[str, Any]) -> None:
    _fill_zone_from_text(levels, raw.get("entry_zone"))
    low = _first_price(raw.get("entry_low"), _mapping_value(raw.get("entry_zone"), "low"))
    high = _first_price(raw.get("entry_high"), _mapping_value(raw.get("entry_zone"), "high"))
    if low != NA or high != NA:
        _fill_zone_from_text(levels, f"{low}-{high}")


def _target_from_raw(raw: Mapping[str, Any], index: int) -> Any:
    for key in ("take_profit_targets", "take_profits", "targets"):
        value = raw.get(key)
        target = _sequence_item(value, index)
        if _clean(target) != NA:
            return target
    return NA


def _sequence_item(value: Any, index: int) -> Any:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray, Mapping)):
        return NA
    if len(value) < index:
        return NA
    item = value[index - 1]
    if isinstance(item, Mapping):
        return _first_non_na(item.get("price"), item.get("target"), item.get("level"))
    return item


def _status_from_outcomes(alert_types: frozenset[str]) -> str:
    if _TP2_TYPE in alert_types:
        return WATCHLIST_STATUS_TP2_HIT
    if _TP1_TYPE in alert_types:
        return WATCHLIST_STATUS_TP1_HIT
    if _LIMIT_TYPE in alert_types:
        return WATCHLIST_STATUS_LIMIT_HIT
    return WATCHLIST_STATUS_WAITING


def _target_progress_lines(item: ActiveWatchlistItem) -> list[str]:
    lines: list[str] = []
    for target_type, label, level in (
        (_TP1_TYPE, "TP1", item.tp1),
        (_TP2_TYPE, "TP2", item.tp2),
        (_TP3_TYPE, "TP3", item.tp3),
    ):
        if level == NA:
            continue
        status = "HIT" if target_type in item.hit_alert_types else "waiting"
        lines.append(f"{label}: {status} ({level})")
    return lines


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _select_or_na(column: str, columns: set[str]) -> str:
    return column if column in columns else f"'{NA}' AS {column}"


def _resolve_project_path(project_root: Path, value: Path | str) -> Path:
    path = Path(value)
    return path if path.is_absolute() else project_root / path


def _row_id(row: Mapping[str, Any]) -> int:
    try:
        return int(str(row.get("id")))
    except (TypeError, ValueError):
        return 0


def _symbol_text(value: Any) -> str:
    text = _clean(value)
    return text.upper() if text != NA else NA


def _direction_text(value: Any) -> str:
    text = _clean(value)
    if text == NA:
        return NA
    upper = text.upper()
    return upper if upper in {"LONG", "SHORT"} else upper


def _zone_text(low: Any, high: Any) -> str:
    low_text = _price_text(low)
    high_text = _price_text(high)
    if low_text == NA and high_text == NA:
        return NA
    if high_text == NA or low_text == high_text:
        return low_text
    if low_text == NA:
        return high_text
    return f"{low_text} {RANGE_DASH} {high_text}"


def _parse_zone_text(value: Any) -> tuple[str, str]:
    if isinstance(value, Mapping):
        return (_first_price(value.get("low"), value.get("entry_low")), _first_price(value.get("high"), value.get("entry_high")))
    text = _clean(value)
    if text == NA:
        return NA, NA
    normalized = text.replace(RANGE_DASH, "-").replace("\u2014", "-")
    parts = [part.strip() for part in normalized.split("-") if part.strip()]
    if len(parts) == 1:
        price = _price_text(parts[0])
        return price, price
    if len(parts) >= 2:
        low = _price_text(parts[0])
        high = _price_text(parts[1])
        return low, high
    return NA, NA


def _normalize_single_level_zone(levels: dict[str, str]) -> None:
    low = levels.get("entry_low", NA)
    high = levels.get("entry_high", NA)
    if low != NA and high == NA:
        levels["entry_high"] = low
    elif high != NA and low == NA:
        levels["entry_low"] = high


def _first_price(*values: Any) -> str:
    for value in values:
        price = _price_text(value)
        if price != NA:
            return price
    return NA


def _price_text(value: Any) -> str:
    price = format_telegram_price(value)
    return price if price != NA else NA


def _json_mapping(value: Any) -> Mapping[str, Any]:
    text = _clean(value)
    if text == NA:
        return {}
    try:
        parsed = json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, Mapping) else {}


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key, NA) if isinstance(value, Mapping) else NA


def _first_non_na(*values: Any) -> Any:
    for value in values:
        if _clean(value) != NA:
            return value
    return NA


def _clean(value: Any) -> str:
    if value is None or value == "":
        return NA
    if isinstance(value, bool):
        return NA
    if isinstance(value, Mapping):
        return NA
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return NA
    text = " ".join(str(value).split())
    if not text or text.upper() == NA:
        return NA
    return text
