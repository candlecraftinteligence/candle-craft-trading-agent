from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from app.data.dtos import NA

UNKNOWN_PUBLIC_BLOCK = "UNKNOWN_PUBLIC_BLOCK"

PUBLIC_BLOCK_CATEGORIES = (
    "LOW_SCORE",
    "RR_BELOW_MIN",
    "TARGET_CAUTION_SCORE_BELOW_88",
    "TARGET_CAUTION_RR_BELOW_2_8",
    "TARGET_CAUTION_NOT_STRONG_ENOUGH",
    "TARGET_INSIDE_CHOP",
    "TARGET_INTEGRITY_BLOCKED",
    "INVALID_TP_SEQUENCE",
    "NON_CRYPTO_SYMBOL",
    "NON_ACTIONABLE_STATE",
    "NON_PUBLIC_TERMINAL_STATE",
    "ENTRY_WINDOW_EXPIRED",
    "LOW_TECHNICAL_SCORE",
    "LOW_OPPORTUNITY_SCORE",
    "BELOW_MIN_PUBLIC_GRADE",
    "DUPLICATE_PUBLIC_PLAN",
    "TERMINAL_UPDATE_NO_PRIOR_PUBLIC_ALERT",
    "DERIVATIVES_CONFLICT",
    "TRUST_METER_BELOW_MINIMUM",
    UNKNOWN_PUBLIC_BLOCK,
)

_ATTEMPT_COLUMNS = (
    "id",
    "signal_id",
    "symbol",
    "direction",
    "new_state",
    "alert_type",
    "lifecycle_state",
    "sent_at",
    "attempted_at",
    "telegram_status",
    "scan_run_id",
    "attempted_alert_type",
    "setup_quality_score",
    "rr_planned",
    "opportunity_score",
    "technical_score",
    "entry_low",
    "entry_high",
    "stop_loss",
    "tp1",
    "tp2",
    "tp3",
    "blocked_reason",
    "invalid_target_fields",
    "dedupe_status",
    "dedupe_reason",
    "first_seen_at",
    "last_seen_at",
)

_LATEST_SCAN_COUNTER_COLUMNS = (
    "timestamp",
    "symbols_scanned",
    "total_valid_setups",
    "actionable_setups",
    "actionable_a_grade_setups",
    "actionable_a_grade_target_caution",
    "confirmed_setups",
    "rejected_no_edge",
    "fatal_target_blocks",
    "soft_target_warnings",
)


def normalize_public_block_reasons(blocked_reason: str) -> list[str]:
    text = _display(blocked_reason)
    if text == NA:
        return [UNKNOWN_PUBLIC_BLOCK]

    key = _reason_key(text)
    categories: list[str] = []

    def add(category: str) -> None:
        if category not in categories:
            categories.append(category)

    if "public_block_below_quality_score" in key:
        add("LOW_SCORE")
    if (
        "public_block_rr_below_3" in key
        or "public_watchlist_rr_below_min" in key
        or "planned_rr_below_min" in key
        or "confirmed_rr_below_min" in key
        or "rr_below_minimum" in key
        or "rr_too_low" in key
        or "challenge_rr_below_3" in key
    ):
        add("RR_BELOW_MIN")
    if "public_block_target_caution_score_below_88" in key:
        add("TARGET_CAUTION_SCORE_BELOW_88")
    if "public_block_target_caution_rr_below_2_8" in key:
        add("TARGET_CAUTION_RR_BELOW_2_8")
    if (
        "public_block_target_caution_not_inside_chop" in key
        or "public_watchlist_target_caution_grade_below_a" in key
        or "target_caution_not_strong" in key
    ):
        add("TARGET_CAUTION_NOT_STRONG_ENOUGH")
    if "target_inside_chop" in key and "not_inside_chop" not in key:
        add("TARGET_INSIDE_CHOP")
    if (
        "target_integrity_failed" in key
        or "target_integrity_blocked" in key
        or "public_block_target_caution_status_blocked" in key
        or "a_grade_blocked_by_target" in key
        or key.endswith(":target_integrity")
    ):
        add("TARGET_INTEGRITY_BLOCKED")
    if "invalid_tp_order" in key or "tp_order" in key or "invalid_tp_sequence" in key:
        add("INVALID_TP_SEQUENCE")
    if "public_block_non_crypto_symbol" in key:
        add("NON_CRYPTO_SYMBOL")
    if (
        "public_block_non_actionable_state" in key
        or "public_watchlist_blocked_actionability" in key
        or "public_watchlist_state_not_eligible" in key
        or "public_watchlist_rejected_state_not_watchlist_eligible" in key
        or "lifecycle_state_not_eligible" in key
    ):
        add("NON_ACTIONABLE_STATE")
    if (
        "public_block_non_public_terminal_state" in key
        or "public_watchlist_terminal_state" in key
        or "terminal_or_rejected_state" in key
    ):
        add("NON_PUBLIC_TERMINAL_STATE")
    if "entry_window_expired" in key or "late_pullback" in key:
        add("ENTRY_WINDOW_EXPIRED")
    if "public_block_low_technical_score" in key or "technical_score_below_min" in key:
        add("LOW_TECHNICAL_SCORE")
    if "public_block_low_opportunity_score" in key or "opportunity_score_below_min" in key:
        add("LOW_OPPORTUNITY_SCORE")
    if (
        "below_min_public_grade" in key
        or "public_watchlist_below_min_grade" in key
        or "confirmed_grade_below_min" in key
    ):
        add("BELOW_MIN_PUBLIC_GRADE")
    if (
        "duplicate_public" in key
        or "duplicate_successful_public_watchlist_event" in key
        or "public_watchlist_duplicate_equivalent_plan" in key
        or "duplicate_equivalent_plan" in key
        or "prior_successful_public_watchlist" in key
    ):
        add("DUPLICATE_PUBLIC_PLAN")
    if "terminal_update_no_prior_public_alert" in key:
        add("TERMINAL_UPDATE_NO_PRIOR_PUBLIC_ALERT")
    if "derivatives_conflict" in key or "funding_oi_guard" in key:
        add("DERIVATIVES_CONFLICT")
    if "trust_meter_below_minimum" in key or "trust_meter_below_confirmed_min" in key:
        add("TRUST_METER_BELOW_MINIMUM")

    return categories or [UNKNOWN_PUBLIC_BLOCK]


def build_public_alert_funnel_report(
    database_path: Path | str,
    *,
    hours: int = 24,
    limit: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    db_path = Path(database_path)
    report: dict[str, Any] = {
        "database_path": str(db_path),
        "hours": hours,
        "limit": limit,
        "source_available": False,
        "error": NA,
        "telegram_status_summary": _status_summary(Counter()),
        "status_by_lifecycle_state": [],
        "normalized_block_category_counts": [],
        "top_blocked_symbols": [],
        "best_near_miss_blocked_setups": [],
        "sent_attempts": [],
        "non_crypto_symbol_blocks": [],
        "target_caution_chop_summary": [],
        "latest_scan_run_counters": {},
    }
    if not db_path.exists():
        report["error"] = f"Database does not exist: {db_path}"
        return report

    cutoff = _utc_now(now) - timedelta(hours=max(0, hours))
    max_rows = max(1, limit)

    try:
        with _connect_readonly(db_path) as connection:
            if not _table_exists(connection, "telegram_alert_attempts"):
                report["error"] = "telegram_alert_attempts table not found"
                return report

            attempts = _filtered_attempt_rows(connection, cutoff=cutoff)
            stop_rows = [
                row
                for row in attempts
                if _display(row.get("telegram_status")).lower() not in {"sent", NA.lower()}
            ]
            blocked_rows = [
                row
                for row in attempts
                if _display(row.get("telegram_status")).lower() == "blocked"
            ]
            sent_rows = [
                row
                for row in attempts
                if _display(row.get("telegram_status")).lower() == "sent"
            ]

            report.update(
                {
                    "source_available": True,
                    "telegram_status_summary": _status_summary(
                        Counter(_display(row.get("telegram_status")).lower() for row in attempts)
                    ),
                    "status_by_lifecycle_state": _status_by_lifecycle_state(attempts),
                    "normalized_block_category_counts": _category_counts(stop_rows),
                    "top_blocked_symbols": _top_blocked_symbols(blocked_rows, max_rows),
                    "best_near_miss_blocked_setups": _best_near_miss_rows(blocked_rows, max_rows),
                    "sent_attempts": _sent_attempt_rows(sent_rows, max_rows),
                    "non_crypto_symbol_blocks": _non_crypto_symbol_blocks(blocked_rows),
                    "target_caution_chop_summary": _target_caution_chop_summary(connection, cutoff, max_rows),
                    "latest_scan_run_counters": _latest_scan_run_counters(connection),
                }
            )
    except sqlite3.Error as exc:
        report["error"] = f"SQLite read failed: {exc}"
    except OSError as exc:
        report["error"] = f"Database read failed: {exc}"
    return report


def format_public_alert_funnel_report(report: Mapping[str, Any]) -> str:
    lines: list[str] = [
        "Public Alert Funnel Diagnostics",
        f"Database: {report.get('database_path', NA)}",
        f"Window: last {report.get('hours', NA)}h",
    ]
    if not report.get("source_available"):
        lines.append(f"Error: {report.get('error', 'source unavailable')}")
        return "\n".join(lines)

    _append_count_section(lines, "Telegram status summary", report.get("telegram_status_summary", {}))
    _append_table(
        lines,
        "Status by lifecycle state",
        ("lifecycle_state", "blocked", "skipped", "sent", "total"),
        report.get("status_by_lifecycle_state", ()),
    )
    _append_table(
        lines,
        "Normalized block category counts",
        ("category", "count"),
        report.get("normalized_block_category_counts", ()),
    )
    _append_table(
        lines,
        "Top blocked symbols",
        ("symbol", "lifecycle_state", "categories", "count"),
        report.get("top_blocked_symbols", ()),
    )
    _append_table(
        lines,
        "Best near-miss blocked setups",
        (
            "symbol",
            "direction",
            "lifecycle_state",
            "score",
            "RR",
            "technical_score",
            "opportunity_score",
            "entry_low",
            "entry_high",
            "stop_loss",
            "TP1",
            "TP2",
            "TP3",
            "categories",
            "raw_blocked_reason",
        ),
        report.get("best_near_miss_blocked_setups", ()),
    )
    _append_table(
        lines,
        "Sent attempts summary",
        ("symbol", "direction", "lifecycle_state", "score", "RR", "sent_at"),
        report.get("sent_attempts", ()),
    )
    _append_table(
        lines,
        "Non-crypto symbol blocked count",
        ("symbol", "count"),
        report.get("non_crypto_symbol_blocks", ()),
    )
    _append_table(
        lines,
        "Target caution/chop summary",
        ("actionability_state", "target_integrity_status", "target_failure", "final_failed_gate", "count"),
        report.get("target_caution_chop_summary", ()),
    )
    _append_count_section(lines, "Latest scan run counters", report.get("latest_scan_run_counters", {}))
    return "\n".join(lines)


def _filtered_attempt_rows(connection: sqlite3.Connection, *, cutoff: datetime) -> list[dict[str, Any]]:
    columns = _table_columns(connection, "telegram_alert_attempts")
    select_columns = [_select_or_na(column, columns) for column in _ATTEMPT_COLUMNS]
    rows = connection.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM telegram_alert_attempts
        ORDER BY id DESC
        """
    ).fetchall()
    filtered: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        timestamp = _row_timestamp(item)
        if timestamp is None or timestamp >= cutoff:
            filtered.append(item)
    return filtered


def _status_summary(counter: Counter[str]) -> dict[str, int]:
    summary = {
        "blocked": counter.get("blocked", 0),
        "skipped": counter.get("skipped", 0),
        "sent": counter.get("sent", 0),
    }
    for status, count in sorted(counter.items()):
        if status not in summary and status != NA.lower():
            summary[status] = count
    return summary


def _status_by_lifecycle_state(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, Counter[str]] = {}
    for row in rows:
        state = _display(row.get("lifecycle_state"))
        status = _display(row.get("telegram_status")).lower()
        grouped.setdefault(state, Counter())[status] += 1
    output = []
    for state, counter in grouped.items():
        total = sum(counter.values())
        output.append(
            {
                "lifecycle_state": state,
                "blocked": counter.get("blocked", 0),
                "skipped": counter.get("skipped", 0),
                "sent": counter.get("sent", 0),
                "total": total,
            }
        )
    output.sort(key=lambda row: (-int(row["total"]), str(row["lifecycle_state"])))
    return output


def _category_counts(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        for category in normalize_public_block_reasons(_combined_reason(row)):
            counter[category] += 1
    return [{"category": category, "count": count} for category, count in counter.most_common()]


def _top_blocked_symbols(rows: list[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    counter: Counter[tuple[str, str, str]] = Counter()
    for row in rows:
        categories = ",".join(normalize_public_block_reasons(_combined_reason(row)))
        counter[(_display(row.get("symbol")), _display(row.get("lifecycle_state")), categories)] += 1
    output = [
        {"symbol": symbol, "lifecycle_state": state, "categories": categories, "count": count}
        for (symbol, state, categories), count in counter.most_common(limit)
    ]
    output.sort(key=lambda row: (-int(row["count"]), str(row["symbol"]), str(row["lifecycle_state"])))
    return output[:limit]


def _best_near_miss_rows(rows: list[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    ranked = sorted(
        rows,
        key=lambda row: (
            _decimal_sort(row.get("setup_quality_score")),
            _decimal_sort(row.get("opportunity_score")),
            _decimal_sort(row.get("technical_score")),
            _decimal_sort(row.get("rr_planned")),
        ),
        reverse=True,
    )
    output: list[dict[str, Any]] = []
    for row in ranked[:limit]:
        output.append(
            {
                "symbol": _display(row.get("symbol")),
                "direction": _display(row.get("direction")),
                "lifecycle_state": _display(row.get("lifecycle_state")),
                "score": _display(row.get("setup_quality_score")),
                "RR": _display(row.get("rr_planned")),
                "technical_score": _display(row.get("technical_score")),
                "opportunity_score": _display(row.get("opportunity_score")),
                "entry_low": _display(row.get("entry_low")),
                "entry_high": _display(row.get("entry_high")),
                "stop_loss": _display(row.get("stop_loss")),
                "TP1": _display(row.get("tp1")),
                "TP2": _display(row.get("tp2")),
                "TP3": _display(row.get("tp3")),
                "categories": ",".join(normalize_public_block_reasons(_combined_reason(row))),
                "raw_blocked_reason": _shorten(_display(row.get("blocked_reason")), 100),
            }
        )
    return output


def _sent_attempt_rows(rows: list[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    ranked = sorted(rows, key=lambda row: _row_timestamp(row) or datetime.min.replace(tzinfo=UTC), reverse=True)
    return [
        {
            "symbol": _display(row.get("symbol")),
            "direction": _display(row.get("direction")),
            "lifecycle_state": _display(row.get("lifecycle_state")),
            "score": _display(row.get("setup_quality_score")),
            "RR": _display(row.get("rr_planned")),
            "sent_at": _display(row.get("sent_at")),
        }
        for row in ranked[:limit]
    ]


def _non_crypto_symbol_blocks(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter()
    for row in rows:
        if "NON_CRYPTO_SYMBOL" in normalize_public_block_reasons(_combined_reason(row)):
            counter[_display(row.get("symbol"))] += 1
    return [{"symbol": symbol, "count": count} for symbol, count in counter.most_common()]


def _target_caution_chop_summary(
    connection: sqlite3.Connection,
    cutoff: datetime,
    limit: int,
) -> list[dict[str, Any]]:
    if not _table_exists(connection, "setup_candidates") or not _table_exists(connection, "scan_runs"):
        return []
    candidate_columns = _table_columns(connection, "setup_candidates")
    run_columns = _table_columns(connection, "scan_runs")
    required_candidate = {"run_id", "actionability_state", "target_integrity_status", "target_failure", "final_failed_gate"}
    if not required_candidate <= candidate_columns or not {"run_id", "timestamp"} <= run_columns:
        return []
    rows = connection.execute(
        """
        SELECT
            sc.actionability_state,
            sc.target_integrity_status,
            sc.target_failure,
            sc.final_failed_gate,
            COUNT(*) AS count
        FROM setup_candidates AS sc
        JOIN scan_runs AS sr ON sr.run_id = sc.run_id
        WHERE sr.timestamp >= ?
        GROUP BY sc.actionability_state, sc.target_integrity_status, sc.target_failure, sc.final_failed_gate
        ORDER BY count DESC, sc.actionability_state ASC, sc.target_integrity_status ASC, sc.target_failure ASC
        LIMIT ?
        """,
        (_iso_z(cutoff), limit),
    ).fetchall()
    return [
        {
            "actionability_state": _display(row["actionability_state"]),
            "target_integrity_status": _display(row["target_integrity_status"]),
            "target_failure": _display(row["target_failure"]),
            "final_failed_gate": _display(row["final_failed_gate"]),
            "count": int(row["count"]),
        }
        for row in rows
    ]


def _latest_scan_run_counters(connection: sqlite3.Connection) -> dict[str, Any]:
    if not _table_exists(connection, "scan_runs"):
        return {}
    columns = _table_columns(connection, "scan_runs")
    select_columns = [_select_or_na(column, columns) for column in _LATEST_SCAN_COUNTER_COLUMNS]
    order_by = "timestamp DESC" if "timestamp" in columns else "rowid DESC"
    row = connection.execute(
        f"""
        SELECT {", ".join(select_columns)}
        FROM scan_runs
        ORDER BY {order_by}
        LIMIT 1
        """
    ).fetchone()
    if row is None:
        return {}
    return {column: _display(row[column]) for column in _LATEST_SCAN_COUNTER_COLUMNS}


def _combined_reason(row: Mapping[str, Any]) -> str:
    reason = _display(row.get("blocked_reason"))
    dedupe_reason = _display(row.get("dedupe_reason"))
    if dedupe_reason == NA or dedupe_reason == reason:
        return reason
    if reason == NA:
        return dedupe_reason
    return f"{reason}; {dedupe_reason}"


def _row_timestamp(row: Mapping[str, Any]) -> datetime | None:
    for key in ("attempted_at", "sent_at", "last_seen_at", "first_seen_at"):
        parsed = _parse_timestamp(row.get(key))
        if parsed is not None:
            return parsed
    return None


def _parse_timestamp(value: Any) -> datetime | None:
    text = _display(value)
    if text == NA:
        return None
    try:
        timestamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    return timestamp.astimezone(UTC)


def _decimal_sort(value: Any) -> Decimal:
    text = _display(value)
    if text == NA:
        return Decimal("-1")
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return Decimal("-1")


def _connect_readonly(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _table_exists(connection: sqlite3.Connection, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table_name})").fetchall()}


def _select_or_na(column: str, columns: set[str]) -> str:
    if column in columns:
        return column
    return f"'{NA}' AS {column}"


def _display(value: Any) -> str:
    if value is None:
        return NA
    text = " ".join(str(value).strip().split())
    if not text or text.upper() in {"NA", "N/A", "NONE", "NULL"}:
        return NA
    return text


def _reason_key(value: str) -> str:
    return (
        value.lower()
        .replace("-", "_")
        .replace(" ", "_")
        .replace("/", "_")
        .replace("__", "_")
    )


def _utc_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _iso_z(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _shorten(value: str, max_length: int) -> str:
    if len(value) <= max_length:
        return value
    return f"{value[: max(0, max_length - 3)]}..."


def _append_count_section(lines: list[str], title: str, counts: Mapping[str, Any]) -> None:
    lines.extend(("", f"{title}:"))
    if not counts:
        lines.append("  N/A")
        return
    for key, value in counts.items():
        lines.append(f"  {key}: {value}")


def _append_table(
    lines: list[str],
    title: str,
    headers: tuple[str, ...],
    rows: Any,
) -> None:
    lines.extend(("", f"{title}:"))
    row_list = list(rows or [])
    if not row_list:
        lines.append("  N/A")
        return
    lines.append("  " + " | ".join(headers))
    for row in row_list:
        lines.append("  " + " | ".join(_display(row.get(header)) for header in headers))
