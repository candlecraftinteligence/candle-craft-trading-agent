from __future__ import annotations

import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Mapping

from app.data.dtos import NA

UNKNOWN_PUBLIC_BLOCK = "UNKNOWN_PUBLIC_BLOCK"
UNVERIFIED = "Unverified"

SCORE_GATE = "SCORE_GATE"
RR_GATE = "RR_GATE"
TARGET_INTEGRITY_GATE = "TARGET_INTEGRITY_GATE"
TARGET_CAUTION_GATE = "TARGET_CAUTION_GATE"
LIFECYCLE_PUBLIC_STATE_GATE = "LIFECYCLE_PUBLIC_STATE_GATE"
SYMBOL_UNIVERSE_GATE = "SYMBOL_UNIVERSE_GATE"
ENTRY_WINDOW_GATE = "ENTRY_WINDOW_GATE"
DEDUPLICATION_GATE = "DEDUPLICATION_GATE"
TERMINAL_UPDATE_GATE = "TERMINAL_UPDATE_GATE"
UNKNOWN_GATE = "UNKNOWN_GATE"

BLOCK_STAGES = (
    SCORE_GATE,
    RR_GATE,
    TARGET_INTEGRITY_GATE,
    TARGET_CAUTION_GATE,
    LIFECYCLE_PUBLIC_STATE_GATE,
    SYMBOL_UNIVERSE_GATE,
    ENTRY_WINDOW_GATE,
    DEDUPLICATION_GATE,
    TERMINAL_UPDATE_GATE,
    UNKNOWN_GATE,
)

DEFAULT_PUBLIC_QUALITY_THRESHOLD = Decimal("88")
DEFAULT_PUBLIC_RR_MIN = Decimal("3")
TARGET_CAUTION_RR_MIN = Decimal("2.8")
DEFAULT_PUBLIC_TECHNICAL_MIN = Decimal("95")
DEFAULT_PUBLIC_OPPORTUNITY_MIN = Decimal("95")
PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE = "initial_watchlist"
TERMINAL_LIFECYCLE_ATTEMPT_TYPES = {
    "COOLDOWN",
    "EXPIRED",
    "INVALIDATED",
    "LIMIT_HIT",
    "NO_LONGER_TRACKING",
    "SL_HIT",
    "STOPPED_OUT",
    "TAKE_PROFIT_HIT",
    "TARGET_HIT",
    "TP1_HIT",
    "TP2_HIT",
    "TP3_HIT",
    "WATCHLIST_EXPIRY",
    "WATCHLIST_OUTCOME_TRACKING",
    "WATCHLIST_TERMINAL_SUPPRESSION",
}

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
    "min_rr",
    "opportunity_score",
    "min_score_for_idea",
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
    "seen_count",
    "public_watchlist_plan_id",
    "public_watchlist_event_key",
    "public_alert_event_type",
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


def classify_block_stage(blocked_reason: str | Mapping[str, Any], dedupe_reason: str = NA) -> str:
    row = blocked_reason if isinstance(blocked_reason, Mapping) else None
    reason = _combined_reason(row) if row is not None else _combine_reason_text(blocked_reason, dedupe_reason)
    categories = set(normalize_public_block_reasons(reason))
    key = _reason_key(reason)

    if "terminal_update_no_prior_public_alert" in key or "TERMINAL_UPDATE_NO_PRIOR_PUBLIC_ALERT" in categories:
        return TERMINAL_UPDATE_GATE
    if "duplicate_successful_public_watchlist_event" in key or "DUPLICATE_PUBLIC_PLAN" in categories:
        return DEDUPLICATION_GATE
    if "TARGET_INTEGRITY_BLOCKED" in categories or "INVALID_TP_SEQUENCE" in categories:
        return TARGET_INTEGRITY_GATE
    if (
        "TARGET_CAUTION_SCORE_BELOW_88" in categories
        or "TARGET_CAUTION_RR_BELOW_2_8" in categories
        or "TARGET_CAUTION_NOT_STRONG_ENOUGH" in categories
        or "TARGET_INSIDE_CHOP" in categories
    ):
        return TARGET_CAUTION_GATE
    if "LOW_SCORE" in categories or "BELOW_MIN_PUBLIC_GRADE" in categories:
        return SCORE_GATE
    if "RR_BELOW_MIN" in categories:
        return RR_GATE
    if "NON_CRYPTO_SYMBOL" in categories:
        return SYMBOL_UNIVERSE_GATE
    if "ENTRY_WINDOW_EXPIRED" in categories:
        return ENTRY_WINDOW_GATE
    if "NON_PUBLIC_TERMINAL_STATE" in categories or "NON_ACTIONABLE_STATE" in categories:
        return LIFECYCLE_PUBLIC_STATE_GATE
    return UNKNOWN_GATE


def is_otherwise_publishable_near_miss(row: Mapping[str, Any]) -> bool:
    if _display(row.get("telegram_status")).lower() not in {"blocked", NA.lower()}:
        return False

    reason = _combined_reason(row)
    categories = set(normalize_public_block_reasons(reason))
    if {
        "NON_CRYPTO_SYMBOL",
        "INVALID_TP_SEQUENCE",
        "TARGET_INTEGRITY_BLOCKED",
        "LOW_SCORE",
        "LOW_TECHNICAL_SCORE",
        "LOW_OPPORTUNITY_SCORE",
        "TARGET_CAUTION_SCORE_BELOW_88",
        "TARGET_CAUTION_RR_BELOW_2_8",
        "TARGET_CAUTION_NOT_STRONG_ENOUGH",
    } & categories:
        return False
    if "RR_BELOW_MIN" in categories and not _target_caution_rr_qualifies(row):
        return False
    return _passes_score_rr_technical_opportunity_checks(row)


def build_near_miss_key(row: Mapping[str, Any]) -> str:
    plan_id = _display(row.get("public_watchlist_plan_id"))
    if plan_id != NA:
        return f"plan:{plan_id}"
    event_key = _display(row.get("public_watchlist_event_key"))
    if event_key != NA:
        return f"event:{event_key}"
    fields = (
        _display(row.get("symbol")),
        _status_key(row.get("direction")) or NA,
        _rounded_number(row.get("entry_low")),
        _rounded_number(row.get("entry_high")),
        _rounded_number(row.get("stop_loss")),
        _rounded_number(row.get("tp1")),
        _rounded_number(row.get("tp2")),
        _rounded_number(row.get("tp3")),
    )
    return "setup:" + "|".join(fields)


def summarize_block_reason_for_humans(blocked_reason: str | Mapping[str, Any]) -> str:
    reason = _combined_reason(blocked_reason) if isinstance(blocked_reason, Mapping) else _display(blocked_reason)
    stage = classify_block_stage(blocked_reason)
    categories = [category for category in normalize_public_block_reasons(reason) if category != UNKNOWN_PUBLIC_BLOCK]
    if not categories:
        return f"{stage}: {_shorten(reason, 80)}"
    return f"{stage}: {', '.join(categories)}"

def build_public_alert_funnel_report(
    database_path: Path | str,
    *,
    hours: int = 24,
    limit: int = 20,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a report whose lower cutoff is inclusive: timestamp >= as-of minus hours."""

    db_path = Path(database_path)
    report: dict[str, Any] = {
        "database_path": str(db_path),
        "hours": hours,
        "limit": limit,
        "source_available": False,
        "error": NA,
        "telegram_status_summary": _status_summary(Counter()),
        "status_by_lifecycle_state": [],
        "block_stage_counts": [],
        "normalized_block_category_counts": [],
        "top_blocked_symbols": [],
        "best_near_miss_blocked_setups": [],
        "otherwise_publishable_near_misses": [],
        "lifecycle_public_state_blocks": [],
        "sent_attempts": [],
        "non_crypto_symbol_blocks": [],
        "non_crypto_hygiene": {},
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
                    "block_stage_counts": _block_stage_counts(stop_rows),
                    "normalized_block_category_counts": _category_counts(stop_rows),
                    "top_blocked_symbols": _top_blocked_symbols(blocked_rows, max_rows),
                    "best_near_miss_blocked_setups": _best_near_miss_rows(blocked_rows, max_rows),
                    "otherwise_publishable_near_misses": _best_near_miss_rows(
                        [row for row in blocked_rows if is_otherwise_publishable_near_miss(row)],
                        max_rows,
                    ),
                    "lifecycle_public_state_blocks": _lifecycle_public_state_blocks(blocked_rows, max_rows),
                    "sent_attempts": _sent_attempt_rows(sent_rows, max_rows),
                    "non_crypto_symbol_blocks": _non_crypto_symbol_blocks(blocked_rows),
                    "non_crypto_hygiene": _non_crypto_hygiene(blocked_rows, max_rows),
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
        "Block stage counts",
        ("block_stage", "count"),
        report.get("block_stage_counts", ()),
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
            "count_seen",
            "first_seen_at",
            "last_seen_at",
            "categories",
            "block_stage",
            "raw_blocked_reason",
        ),
        report.get("best_near_miss_blocked_setups", ()),
    )
    _append_table(
        lines,
        "Otherwise publishable near-misses",
        (
            "symbol",
            "direction",
            "lifecycle_state",
            "score",
            "RR",
            "technical_score",
            "opportunity_score",
            "count_seen",
            "categories",
            "block_stage",
            "raw_blocked_reason",
        ),
        report.get("otherwise_publishable_near_misses", ()),
    )
    _append_table(
        lines,
        "Lifecycle/public-state block diagnostics",
        (
            "symbol",
            "direction",
            "lifecycle_state",
            "attempted_alert_type",
            "public_alert_event_type",
            "initial_watchlist_attempt",
            "terminal_lifecycle_update",
            "prior_public_alert_event",
            "otherwise_passed_quality_rr_technical_opportunity",
            "block_stage",
            "raw_blocked_reason",
        ),
        report.get("lifecycle_public_state_blocks", ()),
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
    non_crypto_hygiene = report.get("non_crypto_hygiene", {})
    _append_count_section(
        lines,
        "Non-crypto hygiene summary",
        {
            key: non_crypto_hygiene.get(key, NA)
            for key in ("blocked_attempts", "total_blocked_attempts", "blocked_attempt_percentage")
        },
    )
    _append_table(
        lines,
        "Top non-crypto symbols",
        ("symbol", "count", "in_near_miss_list"),
        non_crypto_hygiene.get("top_symbols") if isinstance(non_crypto_hygiene, Mapping) else (),
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

def _block_stage_counts(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    counter: Counter[str] = Counter(classify_block_stage(row) for row in rows)
    return [{"block_stage": stage, "count": count} for stage, count in counter.most_common()]


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
    return _deduped_near_miss_rows(rows)[:limit]


def _deduped_near_miss_rows(rows: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(build_near_miss_key(row), []).append(row)

    output: list[dict[str, Any]] = []
    for near_miss_key, group in grouped.items():
        best = max(group, key=_near_miss_rank_tuple)
        categories = _group_categories(group)
        output.append(
            {
                "near_miss_key": near_miss_key,
                "symbol": _display(best.get("symbol")),
                "direction": _display(best.get("direction")),
                "lifecycle_state": _display(best.get("lifecycle_state")),
                "score": _display(best.get("setup_quality_score")),
                "RR": _display(best.get("rr_planned")),
                "technical_score": _display(best.get("technical_score")),
                "opportunity_score": _display(best.get("opportunity_score")),
                "entry_low": _display(best.get("entry_low")),
                "entry_high": _display(best.get("entry_high")),
                "stop_loss": _display(best.get("stop_loss")),
                "TP1": _display(best.get("tp1")),
                "TP2": _display(best.get("tp2")),
                "TP3": _display(best.get("tp3")),
                "count_seen": sum(_seen_count(row) for row in group),
                "first_seen_at": _format_timestamp(_group_first_seen_at(group)),
                "last_seen_at": _format_timestamp(_group_last_seen_at(group)),
                "categories": ",".join(categories),
                "block_stage": classify_block_stage(best),
                "raw_blocked_reason": _shorten(_display(best.get("blocked_reason")), 100),
                "human_reason": summarize_block_reason_for_humans(best),
            }
        )
    return sorted(output, key=_near_miss_output_rank_tuple, reverse=True)


def _near_miss_rank_tuple(row: Mapping[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    return (
        _decimal_sort(row.get("setup_quality_score")),
        _decimal_sort(row.get("opportunity_score")),
        _decimal_sort(row.get("technical_score")),
        _decimal_sort(row.get("rr_planned")),
    )


def _near_miss_output_rank_tuple(row: Mapping[str, Any]) -> tuple[Decimal, Decimal, Decimal, Decimal, str]:
    return (
        _decimal_sort(row.get("score")),
        _decimal_sort(row.get("opportunity_score")),
        _decimal_sort(row.get("technical_score")),
        _decimal_sort(row.get("RR")),
        str(row.get("symbol")),
    )


def _lifecycle_public_state_blocks(rows: list[Mapping[str, Any]], limit: int) -> list[dict[str, Any]]:
    filtered = [
        row
        for row in rows
        if classify_block_stage(row) in {LIFECYCLE_PUBLIC_STATE_GATE, TERMINAL_UPDATE_GATE}
        or "NON_PUBLIC_TERMINAL_STATE" in normalize_public_block_reasons(_combined_reason(row))
    ]
    ranked = sorted(filtered, key=_near_miss_rank_tuple, reverse=True)
    output: list[dict[str, Any]] = []
    for row in ranked[:limit]:
        output.append(
            {
                "symbol": _display(row.get("symbol")),
                "direction": _display(row.get("direction")),
                "lifecycle_state": _display(row.get("lifecycle_state")),
                "attempted_alert_type": _display(row.get("attempted_alert_type")),
                "public_alert_event_type": _display(row.get("public_alert_event_type")),
                "raw_blocked_reason": _shorten(_display(row.get("blocked_reason")), 100),
                "initial_watchlist_attempt": _yes_no(_is_initial_watchlist_attempt(row)),
                "terminal_lifecycle_update": _yes_no(_is_terminal_lifecycle_update(row)),
                "prior_public_alert_event": _prior_public_alert_event_status(row),
                "otherwise_passed_quality_rr_technical_opportunity": _yes_no(
                    _passes_score_rr_technical_opportunity_checks(row)
                ),
                "block_stage": classify_block_stage(row),
                "categories": ",".join(normalize_public_block_reasons(_combined_reason(row))),
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

def _non_crypto_hygiene(rows: list[Mapping[str, Any]], limit: int) -> dict[str, Any]:
    counter: Counter[str] = Counter()
    near_miss_symbols = {row["symbol"] for row in _best_near_miss_rows(rows, limit)}
    for row in rows:
        if "NON_CRYPTO_SYMBOL" in normalize_public_block_reasons(_combined_reason(row)):
            counter[_display(row.get("symbol"))] += _seen_count(row)
    blocked_attempts = sum(counter.values())
    total_blocked_attempts = sum(_seen_count(row) for row in rows)
    top_symbols = [
        {
            "symbol": symbol,
            "count": count,
            "in_near_miss_list": _yes_no(symbol in near_miss_symbols),
        }
        for symbol, count in counter.most_common(limit)
    ]
    return {
        "blocked_attempts": blocked_attempts,
        "total_blocked_attempts": total_blocked_attempts,
        "blocked_attempt_percentage": _percentage(blocked_attempts, total_blocked_attempts),
        "top_symbols": top_symbols,
    }


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
    return _combine_reason_text(row.get("blocked_reason"), row.get("dedupe_reason"))


def _combine_reason_text(reason_value: Any, dedupe_reason_value: Any = NA) -> str:
    reason = _display(reason_value)
    dedupe_reason = _display(dedupe_reason_value)
    if dedupe_reason == NA or dedupe_reason == reason:
        return reason
    if reason == NA:
        return dedupe_reason
    return f"{reason}; {dedupe_reason}"


def _group_categories(rows: list[Mapping[str, Any]]) -> list[str]:
    categories: list[str] = []
    for row in rows:
        for category in normalize_public_block_reasons(_combined_reason(row)):
            if category not in categories:
                categories.append(category)
    return categories


def _group_first_seen_at(rows: list[Mapping[str, Any]]) -> datetime | None:
    timestamps = [_first_seen_timestamp(row) for row in rows]
    valid = [timestamp for timestamp in timestamps if timestamp is not None]
    return min(valid) if valid else None


def _group_last_seen_at(rows: list[Mapping[str, Any]]) -> datetime | None:
    timestamps = [_last_seen_timestamp(row) for row in rows]
    valid = [timestamp for timestamp in timestamps if timestamp is not None]
    return max(valid) if valid else None


def _first_seen_timestamp(row: Mapping[str, Any]) -> datetime | None:
    return _parse_timestamp(row.get("first_seen_at")) or _row_timestamp(row)


def _last_seen_timestamp(row: Mapping[str, Any]) -> datetime | None:
    return _parse_timestamp(row.get("last_seen_at")) or _row_timestamp(row)


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


def _decimal_or_none(value: Any) -> Decimal | None:
    text = _display(value)
    if text == NA:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _passes_score_rr_technical_opportunity_checks(row: Mapping[str, Any]) -> bool:
    score = _decimal_or_none(row.get("setup_quality_score"))
    technical_score = _decimal_or_none(row.get("technical_score"))
    opportunity_score = _decimal_or_none(row.get("opportunity_score"))
    rr = _decimal_or_none(row.get("rr_planned"))
    quality_threshold = _decimal_or_none(row.get("min_score_for_idea")) or DEFAULT_PUBLIC_QUALITY_THRESHOLD
    rr_min = _decimal_or_none(row.get("min_rr")) or DEFAULT_PUBLIC_RR_MIN

    if score is None or score < quality_threshold:
        return False
    if technical_score is None or technical_score < DEFAULT_PUBLIC_TECHNICAL_MIN:
        return False
    if opportunity_score is not None and opportunity_score < DEFAULT_PUBLIC_OPPORTUNITY_MIN:
        return False
    if rr is None:
        return False
    return rr >= rr_min or _target_caution_rr_qualifies(row)


def _target_caution_rr_qualifies(row: Mapping[str, Any]) -> bool:
    rr = _decimal_or_none(row.get("rr_planned"))
    if rr is None or rr < TARGET_CAUTION_RR_MIN:
        return False
    reason = _reason_key(_combined_reason(row))
    categories = normalize_public_block_reasons(_combined_reason(row))
    return "target_caution" in reason or any(category.startswith("TARGET_CAUTION_") for category in categories)


def _seen_count(row: Mapping[str, Any]) -> int:
    value = row.get("seen_count")
    try:
        count = int(value)
    except (TypeError, ValueError):
        return 1
    return count if count >= 1 else 1


def _rounded_number(value: Any) -> str:
    number = _decimal_or_none(value)
    if number is None:
        return _display(value)
    rounded = number.quantize(Decimal("0.00000001")).normalize()
    return format(rounded, "f")


def _status_key(value: Any) -> str:
    return _display(value).lower().replace("-", "_").replace(" ", "_")


def _is_initial_watchlist_attempt(row: Mapping[str, Any]) -> bool:
    attempted_type = _status_key(row.get("attempted_alert_type"))
    event_type = _status_key(row.get("public_alert_event_type"))
    return attempted_type == "watchlist" or event_type == PUBLIC_WATCHLIST_INITIAL_EVENT_TYPE


def _is_terminal_lifecycle_update(row: Mapping[str, Any]) -> bool:
    attempted_type = _display(row.get("attempted_alert_type")).upper()
    return attempted_type in TERMINAL_LIFECYCLE_ATTEMPT_TYPES or classify_block_stage(row) == TERMINAL_UPDATE_GATE


def _prior_public_alert_event_status(row: Mapping[str, Any]) -> str:
    reason = _reason_key(_combined_reason(row))
    if (
        "terminal_update_no_prior_public_alert" in reason
        or "outcome_tracking_no_prior_public_watchlist" in reason
        or "limit_hit_requires_prior_public_signal" in reason
    ):
        return "no"
    if "duplicate_successful_public_watchlist_event" in reason or "prior_successful_public_watchlist" in reason:
        return "yes"
    return UNVERIFIED


def _yes_no(value: bool) -> str:
    return "yes" if value else "no"


def _percentage(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0%"
    value = Decimal(numerator) / Decimal(denominator) * Decimal("100")
    return f"{value.quantize(Decimal('0.1'))}%"


def _format_timestamp(value: datetime | None) -> str:
    if value is None:
        return NA
    return _iso_z(value)

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
