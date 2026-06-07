from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from app.analytics.public_signal_quality import grade_from_score, normalize_grade, public_quality_passes
from app.alerts.watchlist_expiry import parse_utc_timestamp, watchlist_expiry_decision
from app.data.dtos import NA
from app.formatters.telegram_signal_formatter import RANGE_DASH, TelegramAlertType, format_telegram_price, format_telegram_rr
from app.lifecycle.eligibility import active_signal_eligible, public_watchlist_eligible

ACTIVE_WATCHLIST_DISPLAY_LIMIT = 10
WATCHLIST_STAGE_DISPLAY_LIMIT = 8
ACTIVE_SIGNAL_TTL_HOURS = 24
ACTIVE_SIGNAL_TTL_AGE = timedelta(hours=ACTIVE_SIGNAL_TTL_HOURS)
ACTIVE_SIGNAL_MIN_RR = Decimal("3")
WATCHLIST_STATUS_WAITING = "Waiting for Limit Zone"
WATCHLIST_STATUS_LIMIT_HIT = "LIMIT ZONE HIT"
WATCHLIST_STATUS_TP1_HIT = "TP1 HIT"
WATCHLIST_STATUS_TP2_HIT = "TP2 HIT"
WATCHLIST_STATUS_TP3_HIT = "TP3 HIT"
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
_ACTIVE_SIGNAL_BASE_TYPES = (_SIGNAL_CONFIRMED_TYPE, _LIMIT_TYPE)
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
_WATCHLIST_REQUIRED_LEVEL_COLUMNS = ("entry_low", "entry_high", "stop_loss", "tp1")
_ACTIVE_SIGNAL_STATE_KEYS = {
    "active",
    "confirmed",
    "executing",
    "limit_hit",
    "limit_zone_hit",
    "managing",
    "tp1_hit",
    "tp2_hit",
    "tp3_hit",
}
_ACTIVE_SIGNAL_ALLOWED_STATE_KEYS = {
    "active",
    "confirmed",
    "confirmed_setup",
    "signal_confirmed",
    "executing",
    "limit_hit",
    "limit_zone_hit",
}
_ACTIVE_SIGNAL_BLOCKED_STATE_KEYS = {
    "reject",
    "rejected",
    "no_trade",
    "no_setup",
    "watch",
    "watching",
    "watchlist",
    "watchlisted",
    "watchlist_only",
    "watch_only",
    "a_grade_watch",
    "stalking",
    "discovered",
    "near_miss",
    "monitoring",
    "cooldown",
    "cooled_down",
    "expired",
    "invalidated",
    "no_longer_tracking",
    "removed",
    "cancelled",
    "canceled",
    "closed",
    "archived",
    "tp_hit",
    "sl_hit",
    "stop_hit",
}
_WATCH_STATE_KEYS = {"watch", "watching_limit_zone", "watchlist", "watchlisted"}
_STALKING_STATE_KEYS = {"stalking"}
_COOLDOWN_STATE_KEYS = {
    "cooldown",
    "cooled_down",
    "invalidated",
    "expired",
    "no_longer_tracking",
    "removed",
    "cancelled",
    "canceled",
    "closed",
    "stopped",
}
_PUBLIC_COOLDOWN_STATE_KEYS = {"cooldown", "cooled_down", "invalidated", "expired", "no_longer_tracking"}
_ACTIVE_OWNED_ALERT_TYPE_KEYS = {"limit_hit", "tp1_hit", "tp2_hit", "tp3_hit"}
_PUBLIC_COOLDOWN_ALERT_TYPE_KEYS = {"invalidated", "expired", "no_longer_tracking", "cooldown"}
_TERMINAL_ALERT_TYPE_KEYS = {
    "invalidated",
    "expired",
    "no_longer_tracking",
    "cooldown",
    "closed",
    "stopped",
    "stop_hit",
}
_COMPLETED_OUTCOME_ALERT_TYPE_KEYS = {"sl_hit", "tp3_hit"}
_ACTIVE_SIGNAL_CLOSED_OUTCOME_KEYS = {"sl_hit", "tp3_hit"}
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
_PUBLIC_WATCHLIST_BLOCKED_KEYS = {
    "body_acceptance_failure",
    "cancelled",
    "canceled",
    "closed",
    "error",
    "failed",
    "failed_quality_gates",
    "insufficient_candles",
    "invalid",
    "invalid_scanner_result",
    "missing_required_candles",
    "no_setup",
    "no_trade",
    "no_valid_liquidity_grab_pullback_setup",
    "no_valid_setup",
    "not_enough_candles",
    "quality_gate_failed",
    "quality_gates_failed",
    "reject",
    "rejected",
    "rejected_strategy",
    "rejected_by_derivatives",
    "rejected_by_regime",
    "rejected_by_risk",
    "rejected_by_scoring",
    "rejected_by_technical",
    "removed",
    "scan_error",
    "scanner_error",
    "scanned_no_setup",
    "sl_hit",
    "stop_hit",
    "strategy_rejected",
    "tp3_hit",
}
_PUBLIC_WATCHLIST_BLOCKED_TEXT = (
    "no valid liquidity-grab pullback setup",
    "no valid liquidity grab pullback setup",
    "no valid setup",
    "not enough candles",
    "insufficient candles",
    "missing required candles",
    "invalid scanner result",
    "strategy rejected",
    "rejected strategy",
    "scan error",
    "scanned_no_setup",
    "failed quality gates",
    "quality gates failed",
)


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
    rr: str = NA
    invalidation: str = NA
    lifecycle_state: str = NA
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
    direction: str = NA
    entry_low: str = NA
    entry_high: str = NA
    stop_loss: str = NA
    tp1: str = NA
    tp2: str = NA
    tp3: str = NA
    rr: str = NA
    grade: str = NA
    invalidation: str = NA
    lifecycle_state: str = NA
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

    deduped = _dedupe_active_watchlist_items(items)
    visible = tuple(deduped[: max(1, limit)])
    return ActiveWatchlistQueryResult(source_available=True, items=visible, total=len(deduped))


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

    deduped = _dedupe_active_signal_items(items)
    visible = tuple(deduped[: max(1, limit)])
    return ActiveSignalQueryResult(source_available=True, items=visible, total=len(deduped))


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

    return _stage_dashboard_result(_dedupe_stage_items(items), limit=max(1, limit), source_available=True)


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
                f"RR: {item.rr}",
                f"Entry: {item.entry_text}",
                f"Stop: {item.stop_loss}",
                f"Targets: {item.targets_text}",
                f"Invalidation: {item.invalidation}",
                f"Lifecycle: {item.lifecycle_state}",
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
        (
            "🔥 STALKING",
            result.stalking_items,
            result.stalking_total,
        ),
        (
            "👀 WATCH",
            result.watch_items,
            result.watch_total,
        ),
        (
            "❄️ COOLDOWN",
            result.cooldown_items,
            result.cooldown_total,
        ),
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
            if not {"alert_type", "telegram_status", "sent_at"} <= columns:
                return False
            placeholders = ",".join("?" for _ in alert_types)
            row = connection.execute(
                f"""
                SELECT 1
                FROM telegram_alert_attempts
                WHERE telegram_status = 'sent'
                  AND sent_at IS NOT NULL
                  AND sent_at NOT IN ('', 'N/A')
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
            lifecycle_stage_keys = (*_WATCH_STATE_KEYS, *_STALKING_STATE_KEYS, *_PUBLIC_COOLDOWN_STATE_KEYS)
            placeholders = ",".join("?" for _ in lifecycle_stage_keys)
            row = connection.execute(
                f"""
                SELECT 1
                FROM setup_lifecycle_records
                WHERE LOWER(REPLACE(REPLACE(current_state, '-', '_'), ' ', '_')) IN ({placeholders})
                LIMIT 1
                """,
                tuple(lifecycle_stage_keys),
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
        _select_or_na("attempted_at", columns),
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
          AND sent_at IS NOT NULL
          AND sent_at NOT IN ('', 'N/A')
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
        if any(_clean(row.get("alert_type")) == _LIMIT_TYPE for row in outcome_rows):
            continue
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
        if stage == NA:
            continue
        if _public_watchlist_stage_blocked((latest_row, watch_row, *outcome_rows), lifecycle_row, symbol_row, raw_result, lifecycle_event):
            continue
        if stage != WATCHLIST_STAGE_COOLDOWN and _expired_public_view_row((latest_row, watch_row, *outcome_rows), lifecycle_row):
            continue
        if stage != WATCHLIST_STAGE_COOLDOWN and _watchlist_row_expired(watch_row, outcome_rows):
            continue
        if not _public_alert_quality_passes(watch_row):
            continue

        symbol = _symbol_text(watch_row.get("symbol"))
        if symbol == NA:
            continue
        levels = _levels_for_watchlist(connection, watch_row, outcome_rows, lifecycle_row, raw_result)
        candidate_meta = _candidate_metadata(connection, watch_row)
        direction = _planned_direction(
            watch_row.get("direction"),
            lifecycle_row.get("direction"),
            raw_result.get("direction"),
            raw_result.get("bias"),
        )
        invalidation = _planned_invalidation_text(lifecycle_row, candidate_meta, raw_result)
        if not _watchlist_stage_has_trade_plan(
            symbol=symbol,
            direction=direction,
            levels=levels,
            invalidation=invalidation,
        ):
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
            candidate_meta=candidate_meta,
        )
        items.append(
            WatchlistStageItem(
                signal_id=signal_id,
                symbol=symbol,
                stage=stage,
                reason=reason,
                updated_at=_updated_at_for_stage_item(latest_row, lifecycle_row),
                direction=direction,
                rr=_first_non_na(watch_row.get("rr_planned"), candidate_meta.get("rr")),
                grade=_first_non_na(watch_row.get("setup_quality_score"), candidate_meta.get("quality_grade")),
                invalidation=invalidation,
                lifecycle_state=_first_non_na(lifecycle_row.get("current_state"), latest_row.get("lifecycle_state"), latest_row.get("new_state")),
                quality_sort=_quality_sort(watch_row, lifecycle_row, symbol_row, raw_result),
                readiness_sort=_readiness_sort(lifecycle_row, symbol_row, raw_result),
                updated_sort=_timestamp_sort(_updated_at_for_stage_item(latest_row, lifecycle_row)),
                **levels,
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
        _select_or_na("invalidation_logic", columns),
        _select_or_na("rr", columns),
        *(_select_or_na(column, columns) for column in _LEVEL_COLUMNS),
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
        if stage != WATCHLIST_STAGE_COOLDOWN and _expired_public_view_row((row,), row):
            continue
        event = _latest_lifecycle_event(connection, row)
        if _public_watchlist_stage_blocked((row,), row, {}, {}, event):
            continue
        symbol = _symbol_text(row.get("symbol"))
        if symbol == NA:
            continue
        candidate_meta = _candidate_metadata(connection, row)
        levels = _levels_for_lifecycle_record(connection, row)
        direction = _planned_direction(row.get("direction"))
        invalidation = _planned_invalidation_text(row, candidate_meta, {})
        if not _watchlist_stage_has_trade_plan(
            symbol=symbol,
            direction=direction,
            levels=levels,
            invalidation=invalidation,
        ):
            continue
        reason = _stage_reason(
            stage=stage,
            latest_row={},
            watch_row={},
            outcome_rows=(),
            lifecycle_row=row,
            lifecycle_event=event,
            symbol_row={},
            raw_result={},
            candidate_meta=candidate_meta,
        )
        updated_at = _updated_at_for_stage_item({}, row)
        items.append(
            WatchlistStageItem(
                signal_id=_clean(row.get("lifecycle_id")),
                symbol=symbol,
                stage=stage,
                reason=reason,
                updated_at=updated_at,
                direction=direction,
                rr=_first_non_na(row.get("rr"), candidate_meta.get("rr")),
                grade=_first_non_na(candidate_meta.get("quality_grade"), row.get("quality_score")),
                invalidation=invalidation,
                lifecycle_state=row.get("current_state"),
                quality_sort=_score_from_quality(row.get("quality_score")),
                readiness_sort=_int_value(row.get("readiness_score")),
                updated_sort=_timestamp_sort(updated_at),
                **levels,
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
        if item.stage in buckets:
            buckets[item.stage].append(item)

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


def _dedupe_active_watchlist_items(items: Sequence[ActiveWatchlistItem]) -> tuple[ActiveWatchlistItem, ...]:
    latest: dict[tuple[str, str], ActiveWatchlistItem] = {}
    for item in items:
        key = (item.symbol, item.direction)
        existing = latest.get(key)
        if existing is None or item.sort_id > existing.sort_id:
            latest[key] = item
    return tuple(sorted(latest.values(), key=lambda item: item.sort_id, reverse=True))


def _dedupe_active_signal_items(items: Sequence[ActiveSignalItem]) -> tuple[ActiveSignalItem, ...]:
    latest: dict[tuple[str, str], ActiveSignalItem] = {}
    for item in items:
        key = (item.symbol, item.direction)
        existing = latest.get(key)
        if existing is None or item.sort_id > existing.sort_id:
            latest[key] = item
    return tuple(sorted(latest.values(), key=lambda item: item.sort_id, reverse=True))


def _dedupe_stage_items(items: Sequence[WatchlistStageItem]) -> tuple[WatchlistStageItem, ...]:
    latest: dict[tuple[str, str], WatchlistStageItem] = {}
    for item in items:
        key = (item.symbol, item.direction)
        existing = latest.get(key)
        if existing is None or _stage_item_sort(item) > _stage_item_sort(existing):
            latest[key] = item
    return tuple(latest.values())


def _stage_item_sort(item: WatchlistStageItem) -> tuple[float, float, int, str]:
    return (item.updated_sort, item.quality_sort, item.readiness_sort, item.signal_id)


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
        if outcome_rows:
            continue
        if _watchlist_row_expired(watch_row, outcome_rows):
            continue
        if not _public_alert_quality_passes(watch_row):
            continue
        symbol = _symbol_text(watch_row.get("symbol"))
        direction = _direction_text(watch_row.get("direction"))
        if symbol == NA or direction == NA:
            continue
        lifecycle_row = _lifecycle_row_for_attempt(connection, watch_row)
        symbol_row = _symbol_result_for_attempt(connection, watch_row)
        raw_result = _json_mapping(symbol_row.get("raw_result_json"))
        levels = _levels_for_watchlist(connection, watch_row, outcome_rows, lifecycle_row, raw_result)
        metadata = _candidate_metadata(connection, watch_row)
        if not public_watchlist_eligible(
            _watchlist_eligibility_record(
                watch_row=watch_row,
                lifecycle_row=lifecycle_row,
                symbol_row=symbol_row,
                raw_result=raw_result,
                levels=levels,
                metadata=metadata,
            )
        ):
            continue
        items.append(
            ActiveWatchlistItem(
                signal_id=signal_id,
                symbol=symbol,
                direction=direction,
                sent_at=_clean(watch_row.get("sent_at")),
                status=WATCHLIST_STATUS_WAITING,
                hit_alert_types=frozenset(),
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
        signal_row = _active_signal_base_row(signal_rows)
        if signal_row is None:
            continue
        outcome_rows = _active_signal_outcome_rows(signal_rows, signal_row)
        latest_row = max((signal_row, *outcome_rows), key=_row_id)
        lifecycle_row = _lifecycle_row_for_attempt(connection, latest_row)
        if not _active_signal_group_is_eligible(
            connection,
            signal_row=signal_row,
            outcome_rows=outcome_rows,
            latest_row=latest_row,
            lifecycle_row=lifecycle_row,
        ):
            continue
        symbol = _symbol_text(signal_row.get("symbol"))
        direction = _direction_text(signal_row.get("direction"))
        if symbol == NA or direction == NA:
            continue
        hit_alert_types = frozenset(
            _clean(row.get("alert_type"))
            for row in (signal_row, *outcome_rows)
            if _clean(row.get("alert_type")) not in {_SIGNAL_CONFIRMED_TYPE, NA}
        )
        levels = _stored_trade_map_levels(signal_row)
        candidate_meta = _candidate_metadata(connection, latest_row)
        lifecycle_state = _first_non_na(
            lifecycle_row.get("current_state"),
            latest_row.get("lifecycle_state"),
            latest_row.get("new_state"),
        )
        items.append(
            ActiveSignalItem(
                signal_id=signal_id,
                symbol=symbol,
                direction=direction,
                updated_at=_active_signal_updated_at(latest_row, lifecycle_row),
                status=_signal_status_from_outcomes(hit_alert_types),
                grade=_active_quality_text(signal_row),
                rr=_active_rr_text(signal_row),
                invalidation=_first_non_na(lifecycle_row.get("invalidation_reason"), candidate_meta.get("invalidation")),
                lifecycle_state=lifecycle_state,
                hit_alert_types=hit_alert_types,
                sort_id=_row_id(latest_row),
                **levels,
            )
        )
    items.sort(key=lambda item: item.sort_id, reverse=True)
    return tuple(items)


def _active_signal_base_row(rows: Sequence[Mapping[str, Any]]) -> Mapping[str, Any] | None:
    base_rows = [row for row in rows if _clean(row.get("alert_type")) in _ACTIVE_SIGNAL_BASE_TYPES]
    if not base_rows:
        return None
    confirmed_rows = [row for row in base_rows if _clean(row.get("alert_type")) == _SIGNAL_CONFIRMED_TYPE]
    if confirmed_rows:
        return max(confirmed_rows, key=_row_id)
    return max(base_rows, key=_row_id)


def _active_signal_outcome_rows(
    rows: Sequence[Mapping[str, Any]],
    base_row: Mapping[str, Any],
) -> tuple[Mapping[str, Any], ...]:
    return tuple(row for row in rows if _row_id(row) != _row_id(base_row))


def _active_signal_group_is_closed(
    rows: Sequence[Mapping[str, Any]],
    lifecycle_row: Mapping[str, Any],
) -> bool:
    values: list[Any] = [
        *(row.get("alert_type") for row in rows),
        *(row.get("new_state") for row in rows),
        *(row.get("lifecycle_state") for row in rows),
    ]
    if _lifecycle_row_matches_rows(rows, lifecycle_row):
        values.append(lifecycle_row.get("current_state"))
    keys = {_status_key(value) for value in values if _status_key(value)}
    return bool(keys & (_TERMINAL_ALERT_TYPE_KEYS | _ACTIVE_SIGNAL_CLOSED_OUTCOME_KEYS | _COOLDOWN_STATE_KEYS))


def _expired_public_view_row(rows: Sequence[Mapping[str, Any]], lifecycle_row: Mapping[str, Any]) -> bool:
    values: list[Any] = [
        *(row.get("alert_type") for row in rows),
        *(row.get("new_state") for row in rows),
        *(row.get("lifecycle_state") for row in rows),
    ]
    if _lifecycle_row_matches_rows(rows, lifecycle_row):
        values.extend((lifecycle_row.get("current_state"), lifecycle_row.get("failed_gate")))
    return "expired" in {_status_key(value) for value in values if _status_key(value)}


def _public_watchlist_stage_blocked(
    rows: Sequence[Mapping[str, Any]],
    lifecycle_row: Mapping[str, Any],
    symbol_row: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    lifecycle_event: Mapping[str, Any],
) -> bool:
    status_values: list[Any] = [
        *(row.get("alert_type") for row in rows),
        *(row.get("new_state") for row in rows),
        *(row.get("lifecycle_state") for row in rows),
        *(row.get("blocked_reason") for row in rows),
        *(row.get("error_message") for row in rows),
        *(row.get("last_error_message") for row in rows),
        lifecycle_row.get("current_state"),
        lifecycle_row.get("failed_gate"),
        lifecycle_event.get("to_state"),
        lifecycle_event.get("failed_gate"),
        symbol_row.get("status"),
        symbol_row.get("display_bucket"),
        symbol_row.get("failed_gate"),
        symbol_row.get("rejection_reason"),
        raw_result.get("status"),
        raw_result.get("display_status"),
        raw_result.get("display_bucket"),
        raw_result.get("first_failed_gate"),
        raw_result.get("rejection_reason"),
        _sequence_first_text(raw_result.get("rejection_reasons")),
        _sequence_first_text(raw_result.get("hard_rejection_reasons")),
        raw_result.get("error"),
        raw_result.get("error_message"),
    ]
    keys = {_status_key(value) for value in status_values if _status_key(value)}
    if keys & _PUBLIC_WATCHLIST_BLOCKED_KEYS:
        return True

    text_values = [str(value).lower() for value in status_values if _clean(value) != NA]
    return any(fragment in value for value in text_values for fragment in _PUBLIC_WATCHLIST_BLOCKED_TEXT)


def _active_signal_group_is_eligible(
    connection: sqlite3.Connection,
    *,
    signal_row: Mapping[str, Any],
    outcome_rows: Sequence[Mapping[str, Any]],
    latest_row: Mapping[str, Any],
    lifecycle_row: Mapping[str, Any],
) -> bool:
    rows = (signal_row, *outcome_rows)
    if _expired_public_view_row(rows, lifecycle_row):
        return False
    if _active_signal_group_is_closed(rows, lifecycle_row):
        return False
    if not _active_signal_state_is_allowed(rows, lifecycle_row):
        return False
    if not _active_signal_is_fresh(latest_row, lifecycle_row):
        return False
    if not _active_signal_quality_passes(connection, signal_row, latest_row, lifecycle_row):
        return False
    if not _active_signal_rr_passes(signal_row):
        return False
    if not _active_signal_row_has_complete_trade_map(signal_row):
        return False
    matched_lifecycle_row = lifecycle_row if _lifecycle_row_matches_rows(rows, lifecycle_row) else {}
    if not active_signal_eligible(_active_signal_eligibility_record(signal_row, matched_lifecycle_row)):
        return False

    latest_symbol_row = _latest_symbol_result_for_attempt(connection, latest_row)
    if _latest_symbol_row_blocks_active(latest_symbol_row):
        return False
    raw_result = _json_mapping(latest_symbol_row.get("raw_result_json"))
    if _latest_price_invalidates_signal(
        direction=_direction_text(signal_row.get("direction")),
        levels=_stored_trade_map_levels(signal_row),
        raw_result=raw_result,
    ):
        return False
    return True


def _watchlist_eligibility_record(
    *,
    watch_row: Mapping[str, Any],
    lifecycle_row: Mapping[str, Any],
    symbol_row: Mapping[str, Any],
    raw_result: Mapping[str, Any],
    levels: Mapping[str, Any],
    metadata: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        **raw_result,
        **symbol_row,
        **lifecycle_row,
        **watch_row,
        **levels,
        "current_state": _first_non_na(
            lifecycle_row.get("current_state"),
            watch_row.get("lifecycle_state"),
            watch_row.get("new_state"),
        ),
        "quality_grade_current": _first_non_na(
            lifecycle_row.get("quality_grade_current"),
            watch_row.get("setup_quality_score"),
            metadata.get("quality_grade"),
        ),
        "rr": _first_non_na(lifecycle_row.get("rr"), watch_row.get("rr_planned"), metadata.get("rr")),
        "invalidation_reason": _first_non_na(lifecycle_row.get("invalidation_reason"), raw_result.get("invalidation_reason")),
        "failed_gate": _first_non_na(lifecycle_row.get("failed_gate"), symbol_row.get("failed_gate"), raw_result.get("failed_gate")),
        "rejection_reason": _first_non_na(symbol_row.get("rejection_reason"), raw_result.get("rejection_reason")),
        "blocked_reason": _first_non_na(watch_row.get("blocked_reason"), raw_result.get("blocked_reason")),
    }


def _active_signal_eligibility_record(
    signal_row: Mapping[str, Any],
    lifecycle_row: Mapping[str, Any],
) -> dict[str, Any]:
    return {
        **lifecycle_row,
        **signal_row,
        "current_state": _first_non_na(
            lifecycle_row.get("current_state"),
            signal_row.get("lifecycle_state"),
            signal_row.get("new_state"),
        ),
        "quality_grade_current": _first_non_na(lifecycle_row.get("quality_grade_current"), signal_row.get("setup_quality_score")),
        "rr": _first_non_na(lifecycle_row.get("rr"), signal_row.get("rr_planned")),
    }


def _active_signal_state_is_allowed(
    rows: Sequence[Mapping[str, Any]],
    lifecycle_row: Mapping[str, Any],
) -> bool:
    values: list[Any] = [
        *(row.get("alert_type") for row in rows),
        *(row.get("new_state") for row in rows),
        *(row.get("lifecycle_state") for row in rows),
    ]
    if _lifecycle_row_matches_rows(rows, lifecycle_row):
        values.append(lifecycle_row.get("current_state"))
    keys = {_status_key(value) for value in values if _status_key(value)}
    if keys & _ACTIVE_SIGNAL_BLOCKED_STATE_KEYS:
        return False
    return bool(keys & _ACTIVE_SIGNAL_ALLOWED_STATE_KEYS)


def _lifecycle_row_matches_rows(rows: Sequence[Mapping[str, Any]], lifecycle_row: Mapping[str, Any]) -> bool:
    lifecycle_id = _clean(lifecycle_row.get("lifecycle_id"))
    if lifecycle_id == NA:
        return False
    row_ids = {
        identifier
        for row in rows
        for identifier in (_clean(row.get("signal_id")), _clean(row.get("lifecycle_id")))
        if identifier != NA
    }
    if lifecycle_id in row_ids:
        return True

    lifecycle_timestamp = _latest_timestamp(
        (
            lifecycle_row.get("last_seen_at"),
            lifecycle_row.get("last_transition_at"),
        )
    )
    row_timestamp = _latest_timestamp(
        value
        for row in rows
        for value in (row.get("last_seen_at"), row.get("sent_at"))
    )
    return lifecycle_timestamp is not None and row_timestamp is not None and lifecycle_timestamp >= row_timestamp


def _active_signal_row_has_complete_trade_map(row: Mapping[str, Any]) -> bool:
    if _clean(row.get("signal_id")) == NA:
        return False
    if _symbol_text(row.get("symbol")) == NA:
        return False
    if _direction_text(row.get("direction")) not in {"LONG", "SHORT"}:
        return False
    levels = _stored_trade_map_levels(row)
    if any(levels.get(column, NA) == NA for column in _LEVEL_COLUMNS):
        return False
    return _trade_map_directionally_valid(direction=_direction_text(row.get("direction")), levels=levels)


def _watchlist_stage_has_trade_plan(
    *,
    symbol: str,
    direction: str,
    levels: Mapping[str, str],
    invalidation: str,
) -> bool:
    if _symbol_text(symbol) == NA:
        return False
    if direction not in {"LONG", "SHORT"}:
        return False
    if any(levels.get(column, NA) == NA for column in _WATCHLIST_REQUIRED_LEVEL_COLUMNS):
        return False
    if _safe_plan_text(invalidation) == NA:
        return False
    return _watchlist_trade_map_directionally_valid(direction=direction, levels=levels)


def _stored_trade_map_levels(row: Mapping[str, Any]) -> dict[str, str]:
    levels = {column: _price_text(row.get(column)) for column in _LEVEL_COLUMNS}
    _normalize_single_level_zone(levels)
    return levels


def _watchlist_trade_map_directionally_valid(*, direction: str, levels: Mapping[str, str]) -> bool:
    entry_low = _decimal_or_none(levels.get("entry_low"))
    entry_high = _decimal_or_none(levels.get("entry_high"))
    stop = _decimal_or_none(levels.get("stop_loss"))
    tp1 = _decimal_or_none(levels.get("tp1"))
    if None in {entry_low, entry_high, stop, tp1}:
        return False
    entry_min = min(entry_low, entry_high)
    entry_max = max(entry_low, entry_high)
    if direction == "LONG":
        return stop < entry_min and entry_max < tp1
    if direction == "SHORT":
        return stop > entry_max and entry_min > tp1
    return False


def _planned_direction(*values: Any) -> str:
    for value in values:
        text = _direction_text(value)
        if text in {"LONG", "SHORT"}:
            return text
        key = _status_key(value)
        if key in {"bullish", "long_bias", "bull", "buy", "upside"}:
            return "LONG"
        if key in {"bearish", "short_bias", "bear", "sell", "downside"}:
            return "SHORT"
    return NA


def _planned_invalidation_text(
    lifecycle_row: Mapping[str, Any],
    candidate_meta: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> str:
    trade_idea = _mapping_or_empty(raw_result.get("trade_idea"))
    diagnostics = _raw_diagnostics(raw_result)
    for value in (
        lifecycle_row.get("invalidation_reason"),
        lifecycle_row.get("invalidation_logic"),
        candidate_meta.get("invalidation"),
        candidate_meta.get("cancel_condition"),
        raw_result.get("invalidation"),
        raw_result.get("invalidation_reason"),
        raw_result.get("cancel_condition"),
        raw_result.get("watchlist_invalidation"),
        trade_idea.get("invalidation"),
        trade_idea.get("cancel_condition"),
        diagnostics.get("invalidation"),
        diagnostics.get("invalidation_reason"),
        diagnostics.get("watchlist_invalidation"),
        diagnostics.get("invalidation_hint"),
    ):
        text = _safe_plan_text(value)
        if text != NA:
            return text
    return NA


def _safe_plan_text(value: Any) -> str:
    text = _short_reason_text(value, max_length=160)
    if text == NA or _looks_like_public_rejection_text(text):
        return NA
    return text


def _active_quality_text(row: Mapping[str, Any]) -> str:
    quality = _clean(row.get("setup_quality_score"))
    grade = normalize_grade(quality)
    if grade != NA:
        return grade
    grade = grade_from_score(quality)
    return grade if grade != NA else quality


def _active_rr_text(row: Mapping[str, Any]) -> str:
    return format_telegram_rr(row.get("rr_planned"))


def _active_signal_updated_at(
    latest_row: Mapping[str, Any],
    lifecycle_row: Mapping[str, Any],
) -> str:
    return _latest_timestamp_text(
        (
            lifecycle_row.get("last_seen_at"),
            lifecycle_row.get("last_transition_at"),
            latest_row.get("last_seen_at"),
            latest_row.get("sent_at"),
        )
    )


def _active_signal_is_fresh(
    latest_row: Mapping[str, Any],
    lifecycle_row: Mapping[str, Any],
) -> bool:
    parsed = _latest_timestamp(
        (
            lifecycle_row.get("last_seen_at"),
            lifecycle_row.get("last_transition_at"),
            latest_row.get("last_seen_at"),
            latest_row.get("sent_at"),
        )
    )
    if parsed is None:
        return False
    return datetime.now(UTC) - parsed <= ACTIVE_SIGNAL_TTL_AGE


def _active_signal_quality_passes(
    connection: sqlite3.Connection,
    signal_row: Mapping[str, Any],
    latest_row: Mapping[str, Any],
    lifecycle_row: Mapping[str, Any],
) -> bool:
    candidate_meta = _candidate_metadata(connection, latest_row)
    latest_symbol_row = _latest_symbol_result_for_attempt(connection, latest_row)
    raw_result = _json_mapping(latest_symbol_row.get("raw_result_json"))
    diagnostics = _raw_diagnostics(raw_result)
    setup_quality = _mapping_or_empty(raw_result.get("setup_quality"))
    trade_idea = _mapping_or_empty(raw_result.get("trade_idea"))

    explicit_grade_candidates = (
        signal_row.get("setup_quality_score"),
        candidate_meta.get("quality_grade"),
        setup_quality.get("quality_grade"),
        raw_result.get("quality_grade"),
        trade_idea.get("grade"),
        _diagnostic_value(diagnostics, "quality_grade"),
        _diagnostic_value(diagnostics, "trust_grade"),
    )
    score_candidates = (
        signal_row.get("setup_quality_score"),
        lifecycle_row.get("quality_score"),
        latest_symbol_row.get("setup_quality_score"),
        setup_quality.get("quality_score"),
        _diagnostic_value(diagnostics, "quality_score"),
        _diagnostic_value(diagnostics, "trust_percentage"),
        _diagnostic_value(diagnostics, "trust_score"),
    )
    if _quality_candidates_reject((*explicit_grade_candidates, *score_candidates)):
        return False
    return public_quality_passes(
        grade_candidates=explicit_grade_candidates,
        score_candidates=score_candidates,
    )


def _active_signal_rr_passes(row: Mapping[str, Any]) -> bool:
    rr = _decimal_or_none(row.get("rr_planned"))
    if rr is None or format_telegram_rr(row.get("rr_planned")) == NA:
        return False
    required = max(
        value
        for value in (ACTIVE_SIGNAL_MIN_RR, _decimal_or_none(row.get("min_rr")))
        if value is not None
    )
    return rr >= required


def _trade_map_directionally_valid(*, direction: str, levels: Mapping[str, str]) -> bool:
    entry_low = _decimal_or_none(levels.get("entry_low"))
    entry_high = _decimal_or_none(levels.get("entry_high"))
    stop = _decimal_or_none(levels.get("stop_loss"))
    tp1 = _decimal_or_none(levels.get("tp1"))
    tp2 = _decimal_or_none(levels.get("tp2"))
    tp3 = _decimal_or_none(levels.get("tp3"))
    if None in {entry_low, entry_high, stop, tp1, tp2, tp3}:
        return False
    entry_min = min(entry_low, entry_high)
    entry_max = max(entry_low, entry_high)
    if direction == "LONG":
        return stop < entry_min and entry_max < tp1 < tp2 < tp3
    if direction == "SHORT":
        return stop > entry_max and entry_min > tp1 > tp2 > tp3
    return False


def _latest_symbol_row_blocks_active(row: Mapping[str, Any]) -> bool:
    if not row:
        return False
    raw_result = _json_mapping(row.get("raw_result_json"))
    setup_quality = _mapping_or_empty(raw_result.get("setup_quality"))
    state_values = (
        row.get("status"),
        row.get("display_bucket"),
        raw_result.get("status"),
        raw_result.get("display_status"),
        raw_result.get("display_bucket"),
        setup_quality.get("quality_grade"),
        raw_result.get("quality_grade"),
        _mapping_or_empty(raw_result.get("trade_idea")).get("grade"),
    )
    state_keys = {_status_key(value) for value in state_values if _status_key(value)}
    if state_keys & _ACTIVE_SIGNAL_BLOCKED_STATE_KEYS:
        return True

    failure_values = (
        row.get("failed_gate"),
        row.get("rejection_reason"),
        raw_result.get("failed_gate"),
        raw_result.get("failed_stage"),
        raw_result.get("rejection_stage"),
        raw_result.get("rejection_reason"),
        _diagnostic_value(_raw_diagnostics(raw_result), "first_failed_gate"),
    )
    failure_keys = {_status_key(value) for value in failure_values if _status_key(value)}
    return bool(
        failure_keys
        & {
            "body_acceptance_failure",
            "challenge_rr_below_3",
            "invalidation_triggered",
            "invalidated",
            "missing_invalidation",
            "missing_rr",
            "missing_stop",
            "missing_target",
            "missing_targets",
            "missing_tp",
            "missing_tp1",
            "missing_tp2",
            "pullback_beyond_786",
            "pullback_too_deep",
            "quality_filter",
            "rr_below_minimum",
            "rr_too_low",
            "stop_wrong_side",
            "structural_breakdown",
            "target_integrity",
            "target_order_invalid",
            "targets_not_monotonic",
            "trust_meter_below_minimum",
            "wrong_side_stop",
        }
    )


def _latest_price_invalidates_signal(
    *,
    direction: str,
    levels: Mapping[str, str],
    raw_result: Mapping[str, Any],
) -> bool:
    price = _latest_price_from_raw_result(raw_result)
    stop = _decimal_or_none(levels.get("stop_loss"))
    if price is None or stop is None:
        return False
    if direction == "LONG":
        return price <= stop
    if direction == "SHORT":
        return price >= stop
    return True


def _latest_price_from_raw_result(raw_result: Mapping[str, Any]) -> Decimal | None:
    diagnostics = _raw_diagnostics(raw_result)
    candidates: list[Any] = [
        raw_result.get("current_price"),
        raw_result.get("latest_close"),
        raw_result.get("latest_price"),
        raw_result.get("last_price"),
        raw_result.get("price"),
        raw_result.get("close"),
        _diagnostic_value(diagnostics, "current_price"),
        _diagnostic_value(diagnostics, "latest_close"),
        _diagnostic_value(diagnostics, "latest_price"),
        _diagnostic_value(diagnostics, "last_price"),
        _diagnostic_value(diagnostics, "price"),
    ]
    for key in ("latest_candle", "current_candle", "candle"):
        candidates.append(_mapping_value(raw_result.get(key), "close"))
    for key in ("candles_5m", "candles_15m", "candles_1h", "candles_4h", "candles"):
        candidates.append(_latest_sequence_close(raw_result.get(key)))

    for value in candidates:
        parsed = _decimal_or_none(value)
        if parsed is not None:
            return parsed
    return None


def _latest_sequence_close(value: Any) -> Any:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray, Mapping)) or not value:
        return NA
    return _mapping_value(value[-1], "close")


def _quality_candidates_reject(values: Sequence[Any]) -> bool:
    for value in values:
        key = _status_key(value)
        if key in {"reject", "rejected", "no_trade", "no_setup"}:
            return True
        grade = normalize_grade(value)
        if grade in {"Reject", "No trade"}:
            return True
        score_grade = grade_from_score(value)
        if score_grade == "Reject" and _decimal_or_none(value) is not None:
            return True
    return False


def _latest_timestamp(values: Sequence[Any]) -> datetime | None:
    latest: datetime | None = None
    for value in values:
        parsed = parse_utc_timestamp(value)
        if parsed is not None and (latest is None or parsed > latest):
            latest = parsed
    return latest


def _latest_timestamp_text(values: Sequence[Any]) -> str:
    latest: tuple[datetime, str] | None = None
    fallback = NA
    for value in values:
        text = _clean(value)
        if fallback == NA and text != NA:
            fallback = text
        parsed = parse_utc_timestamp(text)
        if parsed is not None and (latest is None or parsed > latest[0]):
            latest = (parsed, text)
    return latest[1] if latest is not None else fallback


def _mapping_or_empty(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _decimal_or_none(value: Any) -> Decimal | None:
    text = _clean(value)
    if text == NA:
        return None
    try:
        number = Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


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
    if latest_alert_key in _PUBLIC_COOLDOWN_ALERT_TYPE_KEYS or any(key in _PUBLIC_COOLDOWN_STATE_KEYS for key in state_keys):
        return WATCHLIST_STAGE_COOLDOWN
    if latest_alert_key in _ACTIVE_OWNED_ALERT_TYPE_KEYS:
        return NA
    if any(key in _STALKING_STATE_KEYS for key in state_keys):
        return WATCHLIST_STAGE_STALKING
    if any(key in _WATCH_STATE_KEYS for key in state_keys):
        return WATCHLIST_STAGE_WATCH
    return NA


def _stage_for_lifecycle_row(row: Mapping[str, Any]) -> str:
    key = _status_key(row.get("current_state"))
    if key in _PUBLIC_COOLDOWN_STATE_KEYS:
        return WATCHLIST_STAGE_COOLDOWN
    if key in _COOLDOWN_STATE_KEYS:
        return NA
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
    candidate_meta: Mapping[str, Any] | None = None,
) -> str:
    candidates = _reason_candidates(
        latest_row=latest_row,
        watch_row=watch_row,
        outcome_rows=outcome_rows,
        lifecycle_row=lifecycle_row,
        lifecycle_event=lifecycle_event,
        symbol_row=symbol_row,
        raw_result=raw_result,
        candidate_meta=candidate_meta or {},
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
    candidate_meta: Mapping[str, Any],
) -> tuple[Any, ...]:
    diagnostics = _raw_diagnostics(raw_result)
    trade_idea = _mapping_or_empty(raw_result.get("trade_idea"))
    return (
        lifecycle_event.get("notes"),
        lifecycle_event.get("reason"),
        raw_result.get("lifecycle_reason"),
        raw_result.get("signal_reason"),
        raw_result.get("reason_for_trade"),
        trade_idea.get("reason_for_trade"),
        candidate_meta.get("reason"),
        _sequence_first_text(raw_result.get("confirmed_facts")),
        _sequence_first_text(trade_idea.get("confirmed_facts")),
        _sequence_first_text(candidate_meta.get("confirmed_facts")),
        raw_result.get("watchlist_reason"),
        raw_result.get("display_reason"),
        raw_result.get("short_reason"),
        _nested_value(raw_result, "near_miss_intelligence", "next_trigger_needed"),
        symbol_row.get("next_trigger_needed"),
        _diagnostic_value(diagnostics, "next_trigger_needed"),
        _gate_reason(_first_text(symbol_row.get("failed_gate"), raw_result.get("first_failed_gate"), _diagnostic_value(diagnostics, "first_failed_gate"))),
        candidate_meta.get("cancel_condition"),
        raw_result.get("cancel_condition"),
        trade_idea.get("cancel_condition"),
        lifecycle_row.get("invalidation_reason"),
        lifecycle_row.get("invalidation_logic"),
        raw_result.get("invalidation_context"),
        trade_idea.get("invalidation_context"),
        _gate_reason(lifecycle_row.get("failed_gate")),
        latest_row.get("blocked_reason"),
        latest_row.get("invalid_target_fields"),
        watch_row.get("blocked_reason"),
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
    if _looks_like_public_rejection_text(text):
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


def _looks_like_public_rejection_text(value: Any) -> bool:
    text = _clean(value)
    if text == NA:
        return False
    lower = text.lower()
    key = _status_key(text)
    if key in _PUBLIC_WATCHLIST_BLOCKED_KEYS:
        return True
    if any(fragment in lower for fragment in _PUBLIC_WATCHLIST_BLOCKED_TEXT):
        return True
    return any(
        fragment in lower
        for fragment in (
            "scanner rejected",
            "rejected this setup",
            "rejection reason",
            "not a valid setup",
            "no deterministic setup",
        )
    )


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


def _symbol_result_for_attempt(
    connection: sqlite3.Connection,
    row: Mapping[str, Any],
    *,
    prefer_scan_run: bool = True,
) -> Mapping[str, Any]:
    if not _table_exists(connection, "symbol_results"):
        return {}
    columns = _table_columns(connection, "symbol_results")
    if "symbol" not in columns:
        return {}
    select_columns = [
        _select_or_zero("id", columns),
        _select_or_na("run_id", columns),
        "symbol",
        _select_or_na("status", columns),
        _select_or_na("display_bucket", columns),
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
    if prefer_scan_run and "run_id" in columns and scan_run_id != NA:
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


def _latest_symbol_result_for_attempt(connection: sqlite3.Connection, row: Mapping[str, Any]) -> Mapping[str, Any]:
    return _symbol_result_for_attempt(connection, row, prefer_scan_run=False)


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
        _select_or_na("invalidation_logic", columns),
        _select_or_na("cooldown_until", columns),
        _select_or_na("archived_at", columns),
        _select_or_na("rr", columns),
        _select_or_na("quality_grade_current", columns),
        _select_or_na("quality_grade_confirmed", columns),
        *(_select_or_na(column, columns) for column in _LEVEL_COLUMNS),
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
    lifecycle_row: Mapping[str, Any],
    raw_result: Mapping[str, Any],
) -> dict[str, str]:
    levels = {column: NA for column in _LEVEL_COLUMNS}
    _fill_levels_from_row(levels, watch_row)
    _fill_zone_from_text(levels, watch_row.get("price_level"))

    for row in reversed(tuple(outcome_rows)):
        _fill_levels_from_row(levels, row)
        if _clean(row.get("alert_type")) == _LIMIT_TYPE:
            _fill_zone_from_text(levels, row.get("price_level"))

    _fill_levels_from_row(levels, lifecycle_row)
    candidate_levels = _candidate_levels(connection, watch_row)
    for key, value in candidate_levels.items():
        if levels.get(key, NA) == NA:
            levels[key] = value
    for source in _plan_level_sources(raw_result):
        _fill_levels_from_plan_mapping(levels, source)
    return levels


def _levels_for_lifecycle_record(
    connection: sqlite3.Connection,
    lifecycle_row: Mapping[str, Any],
) -> dict[str, str]:
    levels = {column: NA for column in _LEVEL_COLUMNS}
    _fill_levels_from_row(levels, lifecycle_row)
    candidate_levels = _candidate_levels(connection, lifecycle_row)
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


def _candidate_metadata(connection: sqlite3.Connection, watch_row: Mapping[str, Any]) -> dict[str, str]:
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
        _select_or_na("rr", columns),
        _select_or_na("invalidation", columns),
        _select_or_na("quality_grade", columns),
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
    raw = _json_mapping(row["raw_candidate_json"] if "raw_candidate_json" in row.keys() else NA)
    return {
        "rr": _first_non_na(row["rr"], raw.get("rr_to_tp2"), raw.get("planned_rr")),
        "invalidation": _first_non_na(
            row["invalidation"],
            raw.get("invalidation"),
            raw.get("invalidation_reason"),
            raw.get("cancel_condition"),
        ),
        "cancel_condition": _first_non_na(raw.get("cancel_condition"), raw.get("watchlist_cancel_condition")),
        "quality_grade": _first_non_na(row["quality_grade"], raw.get("quality_grade"), raw.get("trust_grade")),
        "reason": _first_non_na(raw.get("reason_for_trade"), raw.get("signal_reason"), raw.get("watchlist_reason")),
        "confirmed_facts": _sequence_first_text(raw.get("confirmed_facts")),
    }


def _levels_from_candidate_row(row: Mapping[str, Any]) -> dict[str, str]:
    raw = _json_mapping(row.get("raw_candidate_json"))
    levels = {column: NA for column in _LEVEL_COLUMNS}

    _fill_levels_from_plan_mapping(levels, raw)
    _fill_zone_from_text(levels, row.get("entry"))

    if levels["stop_loss"] == NA:
        levels["stop_loss"] = _first_price(row.get("stop"))
    if levels["tp1"] == NA:
        levels["tp1"] = _first_price(row.get("tp1"))
    if levels["tp2"] == NA:
        levels["tp2"] = _first_price(row.get("tp2"))
    if levels["tp3"] == NA:
        levels["tp3"] = _first_price(row.get("tp3"))
    return levels


def _plan_level_sources(raw_result: Mapping[str, Any]) -> tuple[Mapping[str, Any], ...]:
    sources: list[Mapping[str, Any]] = [raw_result]
    for key in ("trade_idea", "setup", "candidate", "plan", "trade_map"):
        value = raw_result.get(key)
        if isinstance(value, Mapping):
            sources.append(value)
    diagnostics = _raw_diagnostics(raw_result)
    if diagnostics:
        sources.extend(value for value in diagnostics.values() if isinstance(value, Mapping))
    return tuple(sources)


def _fill_levels_from_plan_mapping(levels: dict[str, str], raw: Mapping[str, Any]) -> None:
    _fill_zone_from_mapping(levels, raw)
    for key in ("watch_zone", "entry_zone", "limit_zone", "entry", "entry_price", "entry_trigger"):
        _fill_zone_from_text(levels, raw.get(key))
    if levels["stop_loss"] == NA:
        levels["stop_loss"] = _first_price(raw.get("stop_loss"), raw.get("stop"), raw.get("stop_price"))
    if levels["tp1"] == NA:
        levels["tp1"] = _first_price(
            raw.get("tp1"),
            raw.get("take_profit_1"),
            raw.get("target_1"),
            raw.get("first_target"),
            _target_from_raw(raw, 1),
        )
    if levels["tp2"] == NA:
        levels["tp2"] = _first_price(raw.get("tp2"), raw.get("take_profit_2"), raw.get("target_2"), _target_from_raw(raw, 2))
    if levels["tp3"] == NA:
        levels["tp3"] = _first_price(raw.get("tp3"), raw.get("take_profit_3"), raw.get("target_3"), _target_from_raw(raw, 3))


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


def _sequence_first_text(value: Any) -> str:
    if isinstance(value, str):
        return _clean(value)
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray, Mapping)):
        return NA
    for item in value:
        text = _clean(item)
        if text != NA:
            return text
    return NA


def _status_from_outcomes(alert_types: frozenset[str]) -> str:
    if _TP3_TYPE in alert_types:
        return WATCHLIST_STATUS_TP3_HIT
    if _TP2_TYPE in alert_types:
        return WATCHLIST_STATUS_TP2_HIT
    if _TP1_TYPE in alert_types:
        return WATCHLIST_STATUS_TP1_HIT
    if _LIMIT_TYPE in alert_types:
        return WATCHLIST_STATUS_LIMIT_HIT
    return WATCHLIST_STATUS_WAITING


def _signal_status_from_outcomes(alert_types: frozenset[str]) -> str:
    if _TP3_TYPE in alert_types:
        return WATCHLIST_STATUS_TP3_HIT
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
