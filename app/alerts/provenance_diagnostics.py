"""Bounded, read-only diagnostics; never claims, repairs, or authorizes sends."""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from collections import Counter, OrderedDict
from pathlib import Path
from typing import Any

from app.data.dtos import NA
from app.runtime_epoch.errors import RuntimeEpochError
from app.runtime_epoch.ownership import (
    PUBLIC_EVENT_FAMILY_TYPES,
    decide_public_effect,
    require_canonical_reservation_for_public_effect,
)

logger = logging.getLogger(__name__)
_WARNED: OrderedDict[str, None] = OrderedDict()
WARNING_CACHE_LIMIT = 1024
WARNING_BUDGET = 10
INSPECTION_LIMIT = 256
DETAIL_LIMIT = 50
PAGE_SIZE = 64


def unavailable_provenance(reason: str, *, requested: bool = True) -> dict[str, Any]:
    return {
        "status": "UNAVAILABLE" if requested else "NOT_REQUESTED",
        "coverage_complete": False if requested else None,
        "unresolved": NA,
        "reason": reason,
    }


def summary_provenance(summary: Any | None, *, requested: bool = True) -> dict[str, Any]:
    report = getattr(summary, "provenance_diagnostics", None)
    if report:
        return dict(report)
    return unavailable_provenance("delivery_summary_unavailable" if requested else NA, requested=requested)


def event_provenance_issue(connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any] | None:
    """Classify current source facts using the same read-only canonical guard."""
    ownership = decide_public_effect(connection, row)
    reservation_reason = NA
    try:
        require_canonical_reservation_for_public_effect(connection, event_id=int(row["id"]))
    except RuntimeEpochError as exc:
        reservation_reason = str(exc) or type(exc).__name__
    if ownership.allowed and reservation_reason == NA:
        return None
    return {
        "event_id": row["id"],
        "event_key": row["event_key"],
        "event_type": row["event_type"],
        "symbol": row["symbol"],
        "delivery_state": row["delivery_state"],
        "source_updated_at": row["updated_at"] or NA,
        "runtime_epoch_id": row["runtime_epoch_id"] or NA,
        "canonical_reservation_attempt_id": row["canonical_reservation_attempt_id"] or NA,
        "ownership_reason": ownership.reason,
        "reservation_reason": reservation_reason,
        "disposition": "UNRESOLVED_NO_SEND",
        "historical_http_delivery": "Unverified",
    }


def _warn_once(scope: str, issue: dict[str, Any], updated_at: Any, *, budget: int) -> bool:
    fingerprint = hashlib.sha256(
        json.dumps([scope, issue, updated_at], sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()
    if fingerprint in _WARNED:
        _WARNED.move_to_end(fingerprint)
        return False
    if budget <= 0:
        return False
    _WARNED[fingerprint] = None
    while len(_WARNED) > WARNING_CACHE_LIMIT:
        _WARNED.popitem(last=False)
    logger.warning(
        "Unresolved public provenance: event_key=%s event_id=%s ownership=%s reservation=%s",
        issue["event_key"], issue["event_id"], issue["ownership_reason"], issue["reservation_reason"],
    )
    return True


def collect_provenance_diagnostics(
    connection: sqlite3.Connection,
    *,
    database_path: str | Path,
    inspection_limit: int = INSPECTION_LIMIT,
    detail_limit: int = DETAIL_LIMIT,
) -> dict[str, Any]:
    """Inspect a bounded oldest-first prefix, independent of delivery recovery.

    Counts are lower bounds when coverage is incomplete. No message text, chat
    destination, transport receipt, or database mutation is collected.
    """
    if inspection_limit < 1 or detail_limit < 0:
        raise ValueError("Provenance inspection requires positive inspection and nonnegative detail limits")
    report: dict[str, Any] = {
        "contract": "cci-public-provenance-diagnostics-v1",
        "status": "OBSERVED",
        "scope": "known_public_event_families_pending_retryable_in_flight",
        "observation_point": "before_delivery_reconciliation",
        "coverage_complete": False,
        "inspected": 0,
        "inspection_limit": inspection_limit,
        "detail_limit": detail_limit,
        "unresolved": 0,
        "omitted_details": 0,
        "warnings_emitted": 0,
        "warnings_suppressed": 0,
        "by_reason": {},
        "details": [],
        "historical_completeness": "Unverified",
    }
    reasons: Counter[str] = Counter()
    families = tuple(sorted(PUBLIC_EVENT_FAMILY_TYPES))
    placeholders = ",".join("?" for _ in families)
    predicate = f"id > ? AND id <= ? AND event_type IN ({placeholders}) AND delivery_state IN ('PENDING','RETRYABLE','IN_FLIGHT')"
    scope = str(Path(database_path).resolve())
    last_id = 0
    try:
        upper_id = int(connection.execute("SELECT COALESCE(MAX(id), 0) FROM public_alert_events").fetchone()[0])
        report["upper_event_id"] = upper_id or NA
        while report["inspected"] < inspection_limit:
            batch_size = min(PAGE_SIZE, inspection_limit - report["inspected"])
            rows = connection.execute(
                f"SELECT * FROM public_alert_events WHERE {predicate} ORDER BY id LIMIT ?",
                (last_id, upper_id, *families, batch_size),
            ).fetchall()
            for row in rows:
                last_id = int(row["id"])
                report["inspected"] += 1
                issue = event_provenance_issue(connection, row)
                if issue is None:
                    continue
                report["unresolved"] += 1
                reason = issue["reservation_reason"]
                if reason == NA:
                    reason = issue["ownership_reason"]
                reasons[reason] += 1
                if len(report["details"]) < detail_limit:
                    report["details"].append(issue)
                emitted = _warn_once(scope, issue, row["updated_at"], budget=WARNING_BUDGET - report["warnings_emitted"])
                report["warnings_emitted" if emitted else "warnings_suppressed"] += 1
            if len(rows) < batch_size:
                report["coverage_complete"] = True
                break
        else:
            report["coverage_complete"] = connection.execute(
                f"SELECT 1 FROM public_alert_events WHERE {predicate} LIMIT 1",
                (last_id, upper_id, *families),
            ).fetchone() is None
    except sqlite3.Error as exc:
        report["status"] = "UNAVAILABLE"
        report["coverage_complete"] = False
        report["error"] = type(exc).__name__
    report["by_reason"] = dict(sorted(reasons.items()))
    report["omitted_details"] = report["unresolved"] - len(report["details"])
    if report["status"] == "UNAVAILABLE" and report["inspected"] == 0:
        report["unresolved"] = NA
    return report
