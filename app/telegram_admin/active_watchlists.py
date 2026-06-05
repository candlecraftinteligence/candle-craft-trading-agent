from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.analytics.public_signal_quality import public_quality_passes
from app.alerts.watchlist_expiry import watchlist_expiry_decision
from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import RANGE_DASH, TelegramAlertType, format_telegram_price

ACTIVE_WATCHLIST_DISPLAY_LIMIT = 10
WATCHLIST_STAGE_DISPLAY_LIMIT = 8
WATCHLIST_STATUS_WAITING = "Waiting for Limit Zone"
WATCHLIST_STATUS_LIMIT_HIT = "LIMIT ZONE HIT"
WATCHLIST_STATUS_TP1_HIT = "TP1 HIT"
WATCHLIST_STATUS_TP2_HIT = "TP2 HIT"
SIGNAL_STATUS_CONFIRMED = "Confirmed setup"
WATCHLIST_STAGE_STALKING = "STALKING"
WATCHLIST_STAGE_WATCH = "WATCH"
WATCHLIST_STAGE_COOLDOWN = "COOLDOWN"
WATCHLIST_DASHBOARD_FOOTER = "Candle Craft | Signal. Structure. Execution."
UNVERIFIED = "Unverified"

_WATCHLIST_TYPE = TelegramAlertType.WATCHLIST.value
_SIGNAL_CONFIRMED_TYPE = TelegramAlertType.SIGNAL_CONFIRMED.value
_LIMIT_TYPE = TelegramAlertType.LIMIT_HIT.value
_TP1_TYPE = TelegramAlertType.TP1_HIT.value
_TP2_TYPE = TelegramAlertType.TP2_HIT.value
_TP3_TYPE = TelegramAlertType.TP3_HIT.value
_SL_TYPE = TelegramAlertType.SL_HIT.value
_INVALIDATED_TYPE = TelegramAlertType.INVALIDATED.value
_EXPIRED_TYPE = TelegramAlertType.EXPIRED.value
_NO_LONGER_TRACKING_TYPE = TelegramAlertType.NO_LONGER_TRACKING.value
_TERMINAL_OUTCOME_TYPES = {
    _TP3_TYPE,
    _SL_TYPE,
    _INVALIDATED_TYPE,
    _EXPIRED_TYPE,
    _NO_LONGER_TRACKING_TYPE,
    "COOLDOWN",
}
_WATCHLIST_QUERY_TYPES = (
    _WATCHLIST_TYPE,
    _LIMIT_TYPE,
    _TP1_TYPE,
    _TP2_TYPE,
    _TP3_TYPE,
    _SL_TYPE,
    _INVALIDATED_TYPE,
    _EXPIRED_TYPE,
    _NO_LONGER_TRACKING_TYPE,
    "COOLDOWN",
)
_SIGNAL_QUERY_TYPES = (
    _SIGNAL_CONFIRMED_TYPE,
    _LIMIT_TYPE,
    _TP1_TYPE,
    _TP2_TYPE,
    _TP3_TYPE,
    _SL_TYPE,
    _INVALIDATED_TYPE,
    _EXPIRED_TYPE,
    _NO_LONGER_TRACKING_TYPE,
    "COOLDOWN",
)
_WATCHLIST_STAGE_QUERY_TYPES = tuple(
    dict.fromkeys((*_WATCHLIST_QUERY_TYPES, _SIGNAL_CONFIRMED_TYPE))
)
_LEVEL_COLUMNS = ("entry_low", "entry_high", "stop_loss", "tp1", "tp2", "tp3")
_ACTIVE_SIGNAL_STATE_KEYS = {"confirmed", "executing", "managing"}
_WATCH_STATE_KEYS = {"watch", "watchlist", "watchlisted"}
_STALKING_STATE_KEYS = {"stalking", "triggered"}
_COOLDOWN_STATE_KEYS = {
    "cooldown",
    "cooled_down",
    "invalidated",
    "expired",
    "no_longer_tracking",
    "removed",
    "cancelled",
    "canceled",
}
_TERMINAL_ALERT_TYPE_KEYS = {
    "invalidated",
    "expired",
    "no_longer_tracking",
    "cooldown",
}
_COMPLETED_OUTCOME_ALERT_TYPE_KEYS = {"sl_hit", "tp3_hit"}
_STALKING_ALERT_TYPE_KEYS = {
    "limit_hit",
    "tp1_hit",
    "tp2_hit",
}
_GATE_REASON_MAP = {
    "missing_confirmation_structure_shift": "sweep done, waiting BOS/CHoCH",
    "missing_confirmed_sweep": "waiting liquidity sweep",
    "no_ob_or_fvg_zone": "pullback forming",
    "challenge_limit_entry_missing": "pullback forming",
    "missing_target": "waiting target expansion",
    "target_integrity": "waiting target expansion",
    "rr_below_minimum": "RR too low, still stalking",
    "rr_too_low": "RR too low, still stalking",
    "challenge_rr_below_3": "RR too low, still stalking",
    "missing_rr": "waiting RR validation",
    "entry_window_expired": "structure not ready yet",
    "missing_displacement_impulse": "structure not ready yet",
    "no_displacement_candle": "structure not ready yet",
    "missing_stop": "structure not ready yet",
    "pullback_too_deep": "structure not ready yet",
    "pullback_beyond_786": "structure not ready yet",
    "body_acceptance_failure": "structure not ready yet",
    "structural_breakdown": "structure not ready yet",
    "trust_meter_below_minimum": "structure not ready yet",
    "challenge_trust_below_85": "structure not ready yet",
    "regime_compatibility": "structure not ready yet",
    "derivatives_conflict": "structure not ready yet",
    "funding_oi_guard": "structure not ready yet",
}


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


@dataclass(frozen=True)
class ActiveSignalItem:
    signal_id: str
    symbol: str
    direction: str
    updated_at: str
    status: str
    grade: str = NA
    entry_low: str = NA
    entry_high: str = NA
    stop_loss: str = NA
    tp1: str = NA
    tp2: str = NA
    tp3: str = NA
    hit_alert_types: frozenset[str] = frozenset()
    sort_id: int = 0

    @property
    def entry_text(self) -> str:
        return _zone_text(self.entry_low, self.entry_high)

    @property
    def targets_text(self) -> str:
        targets = [target for target in (self.tp1, self.tp2, self.tp3) if target != NA]
        return ", ".join(targets) if targets else NA


@dataclass(frozen=True)
class ActiveSignalQueryResult:
    source_available: bool
    items: tuple[ActiveSignalItem, ...] = ()
    total: int = 0


@dataclass(frozen=True)
class WatchlistStageItem:
    signal_id: str
    symbol: str
    stage: str
    reason: str
    updated_at: str = NA
    quality_sort: float = 0.0
    readiness_sort: int = 0
    updated_sort: float = 0.0


@dataclass(frozen=True)
class WatchlistStageDashboardResult:
    source_available: bool
    stalking_items: tuple[WatchlistStageItem, ...] = ()
    stalking_total: int = 0
    watch_items: tuple[WatchlistStageItem, ...] = ()
    watch_total: int = 0
    cooldown_items: tuple[WatchlistStageItem, ...] = ()
    cooldown_total: int = 0
    bucket_limit: int = WATCHLIST_STAGE_DISPLAY_LIMIT

    @property
    def total(self) -> int:
        return self.stalking_total + self.watch_total + self.cooldown_total


def load_active_public_watchlists(
    *,
    project_root: Path | str,
    database_path: Path | str,
    limit: int = ACTIVE_WATCHLIST_DISPLAY_LIMIT,
) -> ActiveWatchlistQueryResult:
    """Read active public WATCHLIST alerts without mutating local scan databases."""

    root = Path(project_root)
    preferred_path = _resolve_project_path(root, database_path)
    selected_path = _latest_runtime_database(root, preferred_path, alert_types=_WATCHLIST_QUERY_TYPES)
    if selected_path is None:
        return ActiveWatchlistQueryResult(source_available=False)

    try:
        with _connect_readonly(selected_path) as connection:
            rows = _sent_alert_attempt_rows(connection, alert_types=_WATCHLIST_QUERY_TYPES)
            items = _active_items_from_rows(connection, rows)
    except (OSError, sqlite3.Error):
        return ActiveWatchlistQueryResult(source_available=False)

    total = len(items)
    visible = tuple(items[: max(1, limit)])
    return ActiveWatchlistQueryResult(source_available=True, items=visible, total=total)


def load_active_public_signals(
    *,
    project_root: Path | str,
    database_path: Path | str,
    limit: int,
) -> ActiveSignalQueryResult:
    """Read active public SIGNAL_CONFIRMED alerts from the runtime store only."""

    root = Path(project_root)
    preferred_path = _resolve_project_path(root, database_path)
    selected_path = _latest_runtime_database(root, preferred_path, alert_types=_SIGNAL_QUERY_TYPES)
    if selected_path is None:
        return ActiveSignalQueryResult(source_available=False)

    try:
        with _connect_readonly(selected_path) as connection:
            rows = _sent_alert_attempt_rows(connection, alert_types=_SIGNAL_QUERY_TYPES)
            items = _active_signal_items_from_rows(connection, rows)
    except (OSError, sqlite3.Error):
        return ActiveSignalQueryResult(source_available=False)

    total = len(items)
    visible = tuple(items[: max(1, limit)])
    return ActiveSignalQueryResult(source_available=True, items=visible, total=total)


def load_watchlist_stage_dashboard(
    *,
    project_root: Path | str,
    database_path: Path | str,
    limit: int = WATCHLIST_STAGE_DISPLAY_LIMIT,
    include_lifecycle_fallback: bool = False,
) -> WatchlistStageDashboardResult:
    """Read grouped watchlist dashboard data without scanning or mutating runtime state."""

    root = Path(project_root)
    preferred_path = _resolve_project_path(root, database_path)
    selected_path = _latest_runtime_database(root, preferred_path, alert_types=_WATCHLIST_STAGE_QUERY_TYPES)
    if selected_path is None and include_lifecycle_fallback:
        selected_path = _latest_lifecycle_database(root, preferred_path)
    if selected_path is None:
        return WatchlistStageDashboardResult(source_available=False, bucket_limit=max(1, limit))

    try:
        with _connect_readonly(selected_path) as connection:
            rows = _sent_alert_attempt_rows(connection, alert_types=_WATCHLIST_STAGE_QUERY_TYPES)
            items = _stage_items_from_alert_rows(connection, rows)
            if not items and include_lifecycle_fallback:
                items = _stage_items_from_lifecycle_records(connection)
    except (OSError, sqlite3.Error):
        return WatchlistStageDashboardResult(source_available=False, bucket_limit=max(1, limit))

    return _stage_dashboard_result(items, limit=max(1, limit), source_available=True)


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


def format_active_signal_lines(result: ActiveSignalQueryResult) -> list[str]:
    if result.total == 0:
        return [
            "No active confirmed signals right now.",
            "",
            "The engine is waiting for clean structure.",
        ]

    lines: list[str] = []
    for item in result.items:
        if lines:
            lines.append("")
        lines.extend(
            (
                f"Symbol: {item.symbol}",
                f"Direction: {_title_text(item.direction)}",
                f"Grade: {item.grade}",
                f"Entry: {item.entry_text}",
                f"Stop: {item.stop_loss}",
                f"Targets: {item.targets_text}",
                f"Status: {item.status}",
                f"Updated: {item.updated_at}",
            )
        )
    if result.total > len(result.items):
        lines.append(f"{result.total - len(result.items)} more not shown.")
    return lines


def format_watchlist_stage_dashboard(result: WatchlistStageDashboardResult) -> str:
    lines: list[str] = [
        "🐺🟠 WATCHLISTS",
        "",
        "The wolf is stalking liquidity.",
        "",
    ]
    for title, items, total in (
        ("🔥 STALKING", result.stalking_items, result.stalking_total),
        ("👀 WATCH", result.watch_items, result.watch_total),
        ("❄️ COOLDOWN", result.cooldown_items, result.cooldown_total),
    ):
        lines.append(title)
        if total == 0:
            lines.append("None right now.")
        else:
            for item in items:
                lines.append(f"{item.symbol} — {item.reason}")
            if total > len(items):
                lines.append(f"+ {total - len(items)} more")
        lines.append("")

    if result.total == 0:
        lines.append("No forced trades.")
    lines.append(WATCHLIST_DASHBOARD_FOOTER)
    return "\n".join(lines)


def _latest_runtime_database(
    project_root: Path,
    preferred_path: Path,
    *,
    alert_types: Sequence[str],
) -> Path | None:
    if _has_sent_alert_attempt(preferred_path, alert_types=alert_types):
        return preferred_path

    scan_dir = project_root / "scan_runs"
    candidates = sorted(scan_dir.glob("*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True) if scan_dir.exists() else []
    for candidate in candidates:
        if candidate.resolve() == preferred_path.resolve():
            continue
        if _has_sent_alert_attempt(candidate, alert_types=alert_types):
            return candidate

    if _has_telegram_alert_attempts(preferred_path):
        return preferred_path
    for candidate in candidates:
        if candidate.resolve() == preferred_path.resolve():
            continue
        if _has_telegram_alert_attempts(candidate):
            return candidate
    return None


def _latest_lifecycle_database(project_root: Path, preferred_path: Path) -> Path | None:
    if _has_watchlist_lifecycle_records(preferred_path):
        return preferred_path

    scan_dir = project_root / "scan_runs"
    candidates = sorted(scan_dir.glob("*.sqlite"), key=lambda path: path.stat().st_mtime, reverse=True) if scan_dir.exists() else []
    for candidate in candidates:
        if candidate.resolve() == preferred_path.resolve():
            continue
        if _has_watchlist_lifecycle_records(candidate):
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


def _has_sent_alert_attempt(path: Path, *, alert_types: Sequence[str]) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        with _connect_readonly(path) as connection:
            if not _table_exists(connection, "telegram_alert_attempts"):
                return False
            columns = _table_columns(connection, "telegram_alert_attempts")
            if not {"alert_type", "telegram_status"} <= columns:
                return False
            placeholders = ",".join("?" for _ in alert_types)
            row = connection.execute(
                f"""
                SELECT 1
                FROM telegram_alert_attempts
                WHERE telegram_status = 'sent'
                  AND alert_type IN ({placeholders})
                LIMIT 1
                """,
                tuple(alert_types),
            ).fetchone()
            return row is not None
    except (OSError, sqlite3.Error):
        return False


def _has_watchlist_lifecycle_records(path: Path) -> bool:
    if not path.exists() or not path.is_file():
        return False
    try:
        with _connect_readonly(path) as connection:
            if not _table_exists(connection, "setup_lifecycle_records"):
                return False
            columns = _table_columns(connection, "setup_lifecycle_records")
            if not {"current_state", "symbol"} <= columns:
                return False
            placeholders = ",".join("?" for _ in (*_WATCH_STATE_KEYS, *_STALKING_STATE_KEYS, *_COOLDOWN_STATE_KEYS))
            row = connection.execute(
                f"""
                SELECT 1
                FROM setup_lifecycle_records
                WHERE LOWER(REPLACE(REPLACE(current_state, '-', '_'), ' ', '_')) IN ({placeholders})
                LIMIT 1
                """,
                tuple((*_WATCH_STATE_KEYS, *_STALKING_STATE_KEYS, *_COOLDOWN_STATE_KEYS)),
            ).fetchone()
            return row is not None
    except (OSError, sqlite3.Error):
        return False


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _sent_alert_attempt_rows(
    connection: sqlite3.Connection,
    *,
    alert_types: Sequence[str],
) -> tuple[Mapping[str, Any], ...]:
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
        _select_or_na("setup_quality_score", columns),
        _select_or_na("new_state", columns),
        _select_or_na("lifecycle_state", columns),
        _select_or_na("price_level", columns),
        _select_or_na("first_seen_at", columns),
        _select_or_na("last_seen_at", columns),
        _select_or_na("attempted_alert_type", columns),
        _select_or_na("rr_planned", columns),
        _select_or_na("min_rr", columns),
        _select_or_na("opportunity_score", columns),
        _select_or_na("technical_score", columns),
        _select_or_na("blocked_reason", columns),
        _select_or_na("invalid_target_fields", columns),
        _select_or_na("error_message", columns),
        _select_or_na("last_error_message", columns),
        *(_select_or_na(column, columns) for column in _LEVEL_COLUMNS),
    ]
    placeholders = ",".join("?" for _ in alert_types)
    rows = connection.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM telegram_alert_attempts
        WHERE telegram_status = 'sent'
          AND alert_type IN ({placeholders})
        ORDER BY id ASC
        """,
        tuple(alert_types),
    ).fetchall()
    return tuple(dict(row) for row in rows)


def _stage_items_from_alert_rows(
    connection: sqlite3.Connection,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[WatchlistStageItem, ...]:
    by_signal: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        signal_id = _clean(row.get("signal_id"))
        if signal_id == NA:
            continue
        by_signal.setdefault(signal_id, []).append(row)

    items: list[WatchlistStageItem] = []
    for signal_id, signal_rows in by_signal.items():
        if _group_has_confirmed_signal(signal_rows):
            continue
        watch_rows = [row for row in signal_rows if _status_key(row.get("alert_type")) == _status_key(_WATCHLIST_TYPE)]
        if not watch_rows:
            continue

        watch_row = max(watch_rows, key=_row_id)
        outcome_rows = [row for row in signal_rows if _row_id(row) != _row_id(watch_row)]
        latest_row = max(signal_rows, key=_row_id)
        lifecycle_row = _lifecycle_row_for_attempt(connection, latest_row)
        lifecycle_event = _latest_lifecycle_event(connection, lifecycle_row)
        symbol_row = _symbol_result_for_attempt(connection, latest_row)
        raw_result = _json_mapping(symbol_row.get("raw_result_json"))

        if _has_active_lifecycle_state((latest_row, watch_row), lifecycle_row):
            continue
        if _is_completed_outcome_not_retained(latest_row, lifecycle_row):
            continue
        stage = _stage_for_alert_group(latest_row, watch_row, outcome_rows, lifecycle_row, symbol_row, raw_result)
        if stage != WATCHLIST_STAGE_COOLDOWN and _watchlist_row_expired(watch_row, outcome_rows):
            continue
        if not _public_alert_quality_passes(watch_row):
            continue

        symbol = _symbol_text(watch_row.get("symbol"))
        if symbol == NA:
            continue
        reason = _stage_reason(
            stage=stage,
            latest_row=latest_row,
            watch_row=watch_row,
            outcome_rows=outcome_rows,
            lifecycle_row=lifecycle_row,
            lifecycle_event=lifecycle_event,
            symbol_row=symbol_row,
            raw_result=raw_result,
        )
        items.append(
            WatchlistStageItem(
                signal_id=signal_id,
                symbol=symbol,
                stage=stage,
                reason=reason,
                updated_at=_updated_at_for_stage_item(latest_row, lifecycle_row),
                quality_sort=_quality_sort(watch_row, lifecycle_row, symbol_row, raw_result),
                readiness_sort=_readiness_sort(lifecycle_row, symbol_row, raw_result),
                updated_sort=_timestamp_sort(_updated_at_for_stage_item(latest_row, lifecycle_row)),
            )
        )
    return tuple(items)


def _stage_items_from_lifecycle_records(connection: sqlite3.Connection) -> tuple[WatchlistStageItem, ...]:
    if not _table_exists(connection, "setup_lifecycle_records"):
        return ()
    columns = _table_columns(connection, "setup_lifecycle_records")
    required = {"lifecycle_id", "symbol", "current_state"}
    if not required <= columns:
        return ()
    select_columns = [
        "lifecycle_id",
        "symbol",
        _select_or_na("mode", columns),
        _select_or_na("direction", columns),
        "current_state",
        _select_or_na("last_seen_at", columns),
        _select_or_na("last_transition_at", columns),
        _select_or_na("failed_gate", columns),
        _select_or_zero("readiness_score", columns),
        _select_or_zero("quality_score", columns),
        _select_or_na("edge_score", columns),
        _select_or_na("action_label", columns),
        _select_or_na("invalidation_reason", columns),
    ]
    rows = connection.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM setup_lifecycle_records
        ORDER BY last_transition_at DESC, last_seen_at DESC, symbol ASC
        """
    ).fetchall()

    items: list[WatchlistStageItem] = []
    for raw_row in rows:
        row = dict(raw_row)
        if _status_key(row.get("current_state")) in _ACTIVE_SIGNAL_STATE_KEYS:
            continue
        stage = _stage_for_lifecycle_row(row)
        if stage == NA:
            continue
        symbol = _symbol_text(row.get("symbol"))
        if symbol == NA:
            continue
        event = _latest_lifecycle_event(connection, row)
        reason = _stage_reason(
            stage=stage,
            latest_row={},
            watch_row={},
            outcome_rows=(),
            lifecycle_row=row,
            lifecycle_event=event,
            symbol_row={},
            raw_result={},
        )
        updated_at = _updated_at_for_stage_item({}, row)
        items.append(
            WatchlistStageItem(
                signal_id=_clean(row.get("lifecycle_id")),
                symbol=symbol,
                stage=stage,
                reason=reason,
                updated_at=updated_at,
                quality_sort=_score_from_quality(row.get("quality_score")),
                readiness_sort=_int_value(row.get("readiness_score")),
                updated_sort=_timestamp_sort(updated_at),
            )
        )
    return tuple(items)


def _stage_dashboard_result(
    items: Sequence[WatchlistStageItem],
    *,
    limit: int,
    source_available: bool,
) -> WatchlistStageDashboardResult:
    buckets = {
        WATCHLIST_STAGE_STALKING: [],
        WATCHLIST_STAGE_WATCH: [],
        WATCHLIST_STAGE_COOLDOWN: [],
    }
    for item in items:
        buckets.setdefault(item.stage, []).append(item)

    sorted_buckets: dict[str, tuple[WatchlistStageItem, ...]] = {}
    for stage, stage_items in buckets.items():
        sorted_buckets[stage] = tuple(
            sorted(
                stage_items,
                key=lambda item: (-item.quality_sort, -item.updated_sort, -item.readiness_sort, item.symbol),
            )
        )
    return WatchlistStageDashboardResult(
        source_available=source_available,
        stalking_items=sorted_buckets[WATCHLIST_STAGE_STALKING][:limit],
        stalking_total=len(sorted_buckets[WATCHLIST_STAGE_STALKING]),
        watch_items=sorted_buckets[WATCHLIST_STAGE_WATCH][:limit],
        watch_total=len(sorted_buckets[WATCHLIST_STAGE_WATCH]),
        cooldown_items=sorted_buckets[WATCHLIST_STAGE_COOLDOWN][:limit],
        cooldown_total=len(sorted_buckets[WATCHLIST_STAGE_COOLDOWN]),
        bucket_limit=limit,
    )


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
        hit_alert_types = frozenset(_clean(row.get("alert_type")) for row in outcome_rows)
        if _watchlist_row_expired(watch_row, outcome_rows):
            continue
        if not _public_alert_quality_passes(watch_row):
            continue
        symbol = _symbol_text(watch_row.get("symbol"))
        direction = _direction_text(watch_row.get("direction"))
        if symbol == NA or direction == NA:
            continue
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


def _active_signal_items_from_rows(
    connection: sqlite3.Connection,
    rows: Sequence[Mapping[str, Any]],
) -> tuple[ActiveSignalItem, ...]:
    by_signal: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        signal_id = _clean(row.get("signal_id"))
        if signal_id == NA:
            continue
        by_signal.setdefault(signal_id, []).append(row)

    items: list[ActiveSignalItem] = []
    for signal_id, signal_rows in by_signal.items():
        confirmed_rows = [row for row in signal_rows if _clean(row.get("alert_type")) == _SIGNAL_CONFIRMED_TYPE]
        if not confirmed_rows:
            continue
        signal_row = max(confirmed_rows, key=_row_id)
        outcome_rows = [row for row in signal_rows if _clean(row.get("alert_type")) != _SIGNAL_CONFIRMED_TYPE]
        if any(_clean(row.get("alert_type")) in _TERMINAL_OUTCOME_TYPES for row in outcome_rows):
            continue
        if not _public_alert_quality_passes(signal_row):
            continue
        symbol = _symbol_text(signal_row.get("symbol"))
        direction = _direction_text(signal_row.get("direction"))
        if symbol == NA or direction == NA:
            continue
        hit_alert_types = frozenset(_clean(row.get("alert_type")) for row in outcome_rows)
        levels = _levels_for_watchlist(connection, signal_row, outcome_rows)
        latest_row = max((signal_row, *outcome_rows), key=_row_id)
        items.append(
            ActiveSignalItem(
                signal_id=signal_id,
                symbol=symbol,
                direction=direction,
                updated_at=_first_non_na(_clean(latest_row.get("last_seen_at")), _clean(latest_row.get("sent_at"))),
                status=_signal_status_from_outcomes(hit_alert_types),
                grade=_first_non_na(_clean(signal_row.get("setup_quality_score")), NA),
                hit_alert_types=hit_alert_types,
                sort_id=_row_id(latest_row),
                **levels,
            )
        )
    items.sort(key=lambda item: item.sort_id, reverse=True)
    return tuple(items)


def _group_has_confirmed_signal(rows: Sequence[Mapping[str, Any]]) -> bool:
    for row in rows:
        if _status_key(row.get("alert_type")) == _status_key(_SIGNAL_CONFIRMED_TYPE):
            return True
        if _status_key(row.get("lifecycle_state")) in _ACTIVE_SIGNAL_STATE_KEYS:
            return True
        if _status_key(row.get("new_state")) in _ACTIVE_SIGNAL_STATE_KEYS:
            return True
    return False


def _has_active_lifecycle_state(
    rows: Sequence[Mapping[str, Any]],
    lifecycle_row: Mapping[str, Any],
) -> bool:
    if _status_key(lifecycle_row.get("current_state")) in _ACTIVE_SIGNAL_STATE_KEYS:
        return True
    return any(_status_key(row.get("lifecycle_state")) in _ACTIVE_SIGNAL_STATE_KEYS for row in rows)


def _is_completed_outcome_not_retained(
    latest_row: Mapping[str, Any],
    lifecycle_row: Mapping[str, Any],
) -> bool:
    if _status_key(latest_row.get("alert_type")) not in _COMPLETED_OUTCOME_ALERT_TYPE_KEYS:
        return False
    return _status_key(lifecycle_row.get("current_state")) not in _COOLDOWN_STATE_KEYS


def _stage_for_alert_group(
    latest_row: Mapping[str, Any],
    watch_row: Mapping[str, Any],
    outcome_rows: Sequence[Mapping[str, Any]],
    lifecycle_row: Mapping[str, Any],
    symbol_row: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> str:
    state_keys = tuple(
        _status_key(value)
        for value in (
            lifecycle_row.get("current_state"),
            latest_row.get("lifecycle_state"),
            latest_row.get("new_state"),
            watch_row.get("lifecycle_state"),
            watch_row.get("new_state"),
        )
        if _status_key(value)
    )
    latest_alert_key = _status_key(latest_row.get("alert_type"))
    if latest_alert_key in _TERMINAL_ALERT_TYPE_KEYS or any(key in _COOLDOWN_STATE_KEYS for key in state_keys):
        return WATCHLIST_STAGE_COOLDOWN
    if any(key in _STALKING_STATE_KEYS for key in state_keys):
        return WATCHLIST_STAGE_STALKING
    if latest_alert_key in _STALKING_ALERT_TYPE_KEYS:
        return WATCHLIST_STAGE_STALKING
    if any(_status_key(row.get("alert_type")) in _STALKING_ALERT_TYPE_KEYS for row in outcome_rows):
        return WATCHLIST_STAGE_STALKING
    gate_key = _status_key(_first_text(symbol_row.get("failed_gate"), raw_result.get("first_failed_gate")))
    if gate_key == "missing_confirmation_structure_shift":
        return WATCHLIST_STAGE_STALKING
    return WATCHLIST_STAGE_WATCH


def _stage_for_lifecycle_row(row: Mapping[str, Any]) -> str:
    key = _status_key(row.get("current_state"))
    if key in _COOLDOWN_STATE_KEYS:
        return WATCHLIST_STAGE_COOLDOWN
    if key in _STALKING_STATE_KEYS:
        return WATCHLIST_STAGE_STALKING
    if key in _WATCH_STATE_KEYS:
        return WATCHLIST_STAGE_WATCH
    return NA


def _stage_reason(
    *,
    stage: str,
    latest_row: Mapping[str, Any],
    watch_row: Mapping[str, Any],
    outcome_rows: Sequence[Mapping[str, Any]],
    lifecycle_row: Mapping[str, Any],
    lifecycle_event: Mapping[str, Any],
    symbol_row: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> str:
    candidates = _reason_candidates(
        latest_row=latest_row,
        watch_row=watch_row,
        outcome_rows=outcome_rows,
        lifecycle_row=lifecycle_row,
        lifecycle_event=lifecycle_event,
        symbol_row=symbol_row,
        raw_result=raw_result,
    )
    if any(_is_unverified(value) for value in candidates):
        return UNVERIFIED
    if stage == WATCHLIST_STAGE_COOLDOWN:
        cooldown_reason = _cooldown_reason(candidates, latest_row, lifecycle_row)
        if cooldown_reason != NA:
            return cooldown_reason
    for value in candidates:
        reason = _reason_text(value)
        if reason != NA:
            return reason
    return NA


def _reason_candidates(
    *,
    latest_row: Mapping[str, Any],
    watch_row: Mapping[str, Any],
    outcome_rows: Sequence[Mapping[str, Any]],
    lifecycle_row: Mapping[str, Any],
    lifecycle_event: Mapping[str, Any],
    symbol_row: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> tuple[Any, ...]:
    diagnostics = _raw_diagnostics(raw_result)
    return (
        raw_result.get("short_reason"),
        raw_result.get("display_reason"),
        raw_result.get("watchlist_reason"),
        _nested_value(raw_result, "near_miss_intelligence", "next_trigger_needed"),
        symbol_row.get("next_trigger_needed"),
        _diagnostic_value(diagnostics, "next_trigger_needed"),
        _gate_reason(_first_text(symbol_row.get("failed_gate"), raw_result.get("first_failed_gate"), _diagnostic_value(diagnostics, "first_failed_gate"))),
        symbol_row.get("rejection_reason"),
        raw_result.get("rejection_reason"),
        lifecycle_row.get("invalidation_reason"),
        lifecycle_event.get("notes"),
        lifecycle_event.get("reason"),
        _gate_reason(lifecycle_row.get("failed_gate")),
        lifecycle_row.get("action_label"),
        latest_row.get("blocked_reason"),
        latest_row.get("invalid_target_fields"),
        latest_row.get("error_message"),
        latest_row.get("last_error_message"),
        watch_row.get("blocked_reason"),
        watch_row.get("error_message"),
        *(_alert_progress_reason(row) for row in outcome_rows),
    )


def _cooldown_reason(
    candidates: Sequence[Any],
    latest_row: Mapping[str, Any],
    lifecycle_row: Mapping[str, Any],
) -> str:
    alert_key = _status_key(latest_row.get("alert_type"))
    state_key = _status_key(_first_text(lifecycle_row.get("current_state"), latest_row.get("lifecycle_state"), latest_row.get("new_state")))
    haystack = " ".join(_clean(value).lower() for value in candidates if _clean(value) != NA)
    if alert_key == _status_key(_EXPIRED_TYPE) or state_key == "expired" or "expired" in haystack:
        return "expired, waiting reset"
    if (
        alert_key == _status_key(_INVALIDATED_TYPE)
        or state_key == "invalidated"
        or "invalidated" in haystack
        or "failed" in haystack
    ):
        return "invalidated, waiting reset"
    if alert_key in _TERMINAL_ALERT_TYPE_KEYS or state_key in _COOLDOWN_STATE_KEYS:
        return "waiting reset"
    return NA


def _reason_text(value: Any) -> str:
    mapped = _gate_reason(value)
    if mapped != NA:
        return mapped
    text = _short_reason_text(value)
    if text == NA:
        return NA
    if text in _GATE_REASON_MAP.values():
        return text
    key = _status_key(text)
    if "target" in key and any(token in key for token in ("wait", "missing", "need", "expand")):
        return "waiting target expansion"
    if "rr" in key or "reward_risk" in key or "risk_reward" in key:
        if "low" in key or "below" in key:
            return "RR too low, still stalking"
        return "waiting RR validation"
    if "sweep" in key and any(token in key for token in ("wait", "missing", "need")):
        return "waiting liquidity sweep"
    if ("bos" in key or "choch" in key or "structure_shift" in key) and any(
        token in key for token in ("wait", "missing", "need", "required")
    ):
        return "waiting BOS/CHoCH"
    if "pullback" in key and any(token in key for token in ("forming", "wait", "missing", "need")):
        return "pullback forming"
    return text


def _gate_reason(value: Any) -> str:
    key = _status_key(value)
    if not key:
        return NA
    return _GATE_REASON_MAP.get(key, NA)


def _alert_progress_reason(row: Mapping[str, Any]) -> str:
    key = _status_key(row.get("alert_type"))
    if key == _status_key(_LIMIT_TYPE):
        return "limit zone hit"
    if key == _status_key(_TP1_TYPE):
        return "TP1 hit, still tracking"
    if key == _status_key(_TP2_TYPE):
        return "TP2 hit, still tracking"
    if key == _status_key(_INVALIDATED_TYPE):
        return "invalidated"
    if key == _status_key(_EXPIRED_TYPE):
        return "expired"
    return NA


def _short_reason_text(value: Any, *, max_length: int = 72) -> str:
    text = _clean(value)
    if text == NA:
        return NA
    if _is_unverified(text):
        return UNVERIFIED
    if any(token in text for token in ("{", "}", "Decimal(")):
        return NA
    text = text.strip().rstrip(".")
    if len(text) <= max_length:
        return text
    return f"{text[: max_length - 3].rstrip()}..."


def _is_unverified(value: Any) -> bool:
    return "unverified" in _status_key(value)


def _symbol_result_for_attempt(connection: sqlite3.Connection, row: Mapping[str, Any]) -> Mapping[str, Any]:
    if not _table_exists(connection, "symbol_results"):
        return {}
    columns = _table_columns(connection, "symbol_results")
    if "symbol" not in columns:
        return {}
    select_columns = [
        _select_or_zero("id", columns),
        _select_or_na("run_id", columns),
        "symbol",
        _select_or_na("setup_quality_score", columns),
        _select_or_zero("readiness_score", columns),
        _select_or_na("failed_gate", columns),
        _select_or_na("rejection_reason", columns),
        _select_or_na("next_trigger_needed", columns),
        _select_or_na("action_label", columns),
        _select_or_na("raw_result_json", columns),
    ]
    symbol = _clean(row.get("symbol"))
    if symbol == NA:
        return {}
    scan_run_id = _clean(row.get("scan_run_id"))
    if "run_id" in columns and scan_run_id != NA:
        rows = connection.execute(
            f"""
            SELECT {", ".join(select_columns)}
            FROM symbol_results
            WHERE UPPER(symbol) = UPPER(?)
            ORDER BY CASE WHEN run_id = ? THEN 0 ELSE 1 END, id DESC
            LIMIT 1
            """,
            (symbol, scan_run_id),
        ).fetchone()
    else:
        rows = connection.execute(
            f"""
            SELECT {", ".join(select_columns)}
            FROM symbol_results
            WHERE UPPER(symbol) = UPPER(?)
            ORDER BY id DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
    return dict(rows) if rows is not None else {}


def _lifecycle_row_for_attempt(connection: sqlite3.Connection, row: Mapping[str, Any]) -> Mapping[str, Any]:
    if not _table_exists(connection, "setup_lifecycle_records"):
        return {}
    columns = _table_columns(connection, "setup_lifecycle_records")
    if not {"lifecycle_id", "symbol"} <= columns:
        return {}
    select_columns = [
        "lifecycle_id",
        "symbol",
        _select_or_na("mode", columns),
        _select_or_na("direction", columns),
        _select_or_na("current_state", columns),
        _select_or_na("last_seen_at", columns),
        _select_or_na("last_transition_at", columns),
        _select_or_na("failed_gate", columns),
        _select_or_zero("readiness_score", columns),
        _select_or_zero("quality_score", columns),
        _select_or_na("edge_score", columns),
        _select_or_na("action_label", columns),
        _select_or_na("invalidation_reason", columns),
    ]
    signal_id = _clean(row.get("signal_id"))
    if signal_id != NA:
        record = connection.execute(
            f"""
            SELECT {", ".join(select_columns)}
            FROM setup_lifecycle_records
            WHERE lifecycle_id = ?
            LIMIT 1
            """,
            (signal_id,),
        ).fetchone()
        if record is not None:
            return dict(record)

    symbol = _clean(row.get("symbol"))
    if symbol == NA:
        return {}
    direction = _clean(row.get("direction"))
    if "direction" in columns and direction != NA:
        record = connection.execute(
            f"""
            SELECT {", ".join(select_columns)}
            FROM setup_lifecycle_records
            WHERE UPPER(symbol) = UPPER(?)
              AND UPPER(direction) = UPPER(?)
            ORDER BY last_seen_at DESC
            LIMIT 1
            """,
            (symbol, direction),
        ).fetchone()
    else:
        record = connection.execute(
            f"""
            SELECT {", ".join(select_columns)}
            FROM setup_lifecycle_records
            WHERE UPPER(symbol) = UPPER(?)
            ORDER BY last_seen_at DESC
            LIMIT 1
            """,
            (symbol,),
        ).fetchone()
    return dict(record) if record is not None else {}


def _latest_lifecycle_event(
    connection: sqlite3.Connection,
    lifecycle_row: Mapping[str, Any],
) -> Mapping[str, Any]:
    lifecycle_id = _clean(lifecycle_row.get("lifecycle_id"))
    if lifecycle_id == NA or not _table_exists(connection, "setup_lifecycle_events"):
        return {}
    columns = _table_columns(connection, "setup_lifecycle_events")
    if "lifecycle_id" not in columns:
        return {}
    select_columns = [
        _select_or_zero("event_id", columns),
        "lifecycle_id",
        _select_or_na("timestamp", columns),
        _select_or_na("to_state", columns),
        _select_or_na("reason", columns),
        _select_or_na("failed_gate", columns),
        _select_or_na("notes", columns),
    ]
    row = connection.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM setup_lifecycle_events
        WHERE lifecycle_id = ?
        ORDER BY timestamp DESC, event_id DESC
        LIMIT 1
        """,
        (lifecycle_id,),
    ).fetchone()
    return dict(row) if row is not None else {}


def _raw_diagnostics(raw_result: Mapping[str, Any]) -> Mapping[str, Any]:
    diagnostics = raw_result.get("strategy_diagnostics")
    if isinstance(diagnostics, Mapping):
        return diagnostics
    return {}


def _diagnostic_value(diagnostics: Mapping[str, Any], key: str) -> Any:
    for value in diagnostics.values():
        if isinstance(value, Mapping) and _clean(value.get(key)) != NA:
            return value.get(key)
    return NA


def _updated_at_for_stage_item(row: Mapping[str, Any], lifecycle_row: Mapping[str, Any]) -> str:
    return _first_non_na(
        lifecycle_row.get("last_transition_at"),
        lifecycle_row.get("last_seen_at"),
        row.get("last_seen_at"),
        row.get("sent_at"),
    )


def _quality_sort(
    watch_row: Mapping[str, Any],
    lifecycle_row: Mapping[str, Any],
    symbol_row: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> float:
    diagnostics = _raw_diagnostics(raw_result)
    return max(
        _score_from_quality(watch_row.get("setup_quality_score")),
        _score_from_quality(symbol_row.get("setup_quality_score")),
        _score_from_quality(lifecycle_row.get("quality_score")),
        _score_from_quality(_nested_value(raw_result, "setup_quality", "quality_score")),
        _score_from_quality(_diagnostic_value(diagnostics, "trust_percentage")),
        _score_from_quality(_diagnostic_value(diagnostics, "trust_score")),
    )


def _readiness_sort(
    lifecycle_row: Mapping[str, Any],
    symbol_row: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> int:
    return max(
        _int_value(lifecycle_row.get("readiness_score")),
        _int_value(symbol_row.get("readiness_score")),
        _int_value(raw_result.get("readiness_score")),
    )


def _score_from_quality(value: Any) -> float:
    text = _clean(value)
    if text == NA:
        return 0.0
    try:
        return float(text)
    except ValueError:
        pass
    key = text.upper().replace(" ", "")
    grade_scores = {
        "A+": 98.0,
        "A": 95.0,
        "A-": 90.0,
        "B+": 85.0,
        "B": 80.0,
        "B-": 75.0,
        "C+": 70.0,
        "C": 65.0,
        "C-": 60.0,
    }
    return grade_scores.get(key, 0.0)


def _timestamp_sort(value: Any) -> float:
    text = _clean(value)
    if text == NA:
        return 0.0
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def _int_value(value: Any) -> int:
    try:
        return int(float(str(value)))
    except (TypeError, ValueError):
        return 0


def _watchlist_row_expired(
    watch_row: Mapping[str, Any],
    outcome_rows: Sequence[Mapping[str, Any]],
) -> bool:
    state_candidates = (
        watch_row.get("lifecycle_state"),
        watch_row.get("new_state"),
        watch_row.get("alert_type"),
        *(row.get("lifecycle_state") for row in outcome_rows),
        *(row.get("new_state") for row in outcome_rows),
        *(row.get("alert_type") for row in outcome_rows),
    )
    # Active public views are read-only, so use telegram_alert_attempts.first_seen_at
    # as the stable public watch anchor and leave persistence to lifecycle delivery.
    return watchlist_expiry_decision(
        timestamp_candidates=(watch_row.get("first_seen_at"), watch_row.get("sent_at")),
        state_candidates=state_candidates,
    ).expired


def _public_alert_quality_passes(row: Mapping[str, Any]) -> bool:
    quality = row.get("setup_quality_score")
    return public_quality_passes(grade_candidates=(quality,), score_candidates=(quality,))


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


def _signal_status_from_outcomes(alert_types: frozenset[str]) -> str:
    if _TP2_TYPE in alert_types:
        return WATCHLIST_STATUS_TP2_HIT
    if _TP1_TYPE in alert_types:
        return WATCHLIST_STATUS_TP1_HIT
    if _LIMIT_TYPE in alert_types:
        return WATCHLIST_STATUS_LIMIT_HIT
    return SIGNAL_STATUS_CONFIRMED


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


def _select_or_zero(column: str, columns: set[str]) -> str:
    return column if column in columns else f"0 AS {column}"


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


def _title_text(value: Any) -> str:
    text = _clean(value)
    if text == NA:
        return NA
    return " ".join(word[:1].upper() + word[1:].lower() for word in text.replace("_", " ").split())


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


def _nested_value(source: Mapping[str, Any], key: str, nested_key: str) -> Any:
    value = source.get(key)
    if isinstance(value, Mapping):
        return value.get(nested_key, NA)
    return NA


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key, NA) if isinstance(value, Mapping) else NA


def _first_non_na(*values: Any) -> Any:
    for value in values:
        if _clean(value) != NA:
            return value
    return NA


def _first_text(*values: Any) -> str:
    return _clean(_first_non_na(*values))


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


def _status_key(value: Any) -> str:
    return _status_key_value(_clean(value))


def _status_key_value(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.upper() == NA:
        return ""
    key = text.lower().replace("-", "_").replace(" ", "_")
    while "__" in key:
        key = key.replace("__", "_")
    return key.strip("_")
