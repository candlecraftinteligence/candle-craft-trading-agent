"""Read-only development evidence baseline audit.

The audit never creates, migrates, or mutates a database. It requires an
explicit path and half-open time window. It does not default to an application
or live runtime database.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from app.analytics.evidence_contract import CONTRACT_VERSION, UNAVAILABLE, UNSAFE
from app.analytics.evidence_time import (
    EvidenceTimestampError,
    parse_aware_utc_timestamp,
    try_parse_stored_timestamp,
    utc_iso,
    validate_window,
    window_contains,
)
from app.storage.database import (
    DatabaseMissingError,
    StorageError,
    identify_schema_version,
    open_read_only_database,
    read_only_connection_safety_proof,
)

AUDIT_VERSION: Final[str] = "cci-evidence-baseline-audit-v1"
LIVE_RUNTIME_BASENAME: Final[str] = "main_live_runtime.sqlite"
KNOWN_LIVE_PATHS: Final[tuple[Path, ...]] = (
    Path(r"S:\CandleCraftRuntime\scan_runs\main_live_runtime.sqlite"),
)
PROTECTED_PATH_PART: Final[str] = "candlecraftruntime"

SCAN_RUN_COUNTERS: Final[tuple[str, ...]] = (
    "symbols_requested",
    "symbols_queued",
    "symbols_completed",
    "symbols_scanned",
    "total_valid_setups",
    "near_misses",
    "rejected",
    "data_issues",
    "valid_activations",
    "still_watching",
    "actionable_setups",
    "confirmed_setups",
    "actionable_a_grade_setups",
    "candidate_a_grade_setups",
    "blocked_a_grade_by_scoring",
    "blocked_a_grade_by_target",
    "blocked_a_grade_by_entry_window",
    "blocked_a_grade_by_trust",
    "fatal_target_blocks",
    "soft_target_warnings",
)

SQLITE_NAIVE_UTC_COLUMNS: Final[frozenset[str]] = frozenset({"created_at", "updated_at"})


class EvidenceAuditError(ValueError):
    """Raised when the evidence audit refuses to proceed."""


def dumps_evidence_payload(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build_evidence_baseline(
    database_path: Path | str,
    *,
    start: str,
    cutoff: str,
) -> dict[str, Any]:
    start_dt = parse_aware_utc_timestamp(start)
    cutoff_dt = parse_aware_utc_timestamp(cutoff)
    validate_window(start_dt, cutoff_dt)
    path = Path(database_path)
    reject_protected_database_path(path)
    if not path.exists():
        raise EvidenceAuditError(f"Database does not exist: {path}")
    if path.exists() and path.is_dir():
        raise EvidenceAuditError("Database path is not a file")

    with open_evidence_audit_database(path) as connection:
        proof = read_only_connection_safety_proof(connection)
        if proof.get("sqlite_uri_mode") != "ro" or proof.get("query_only_readback") != 1:
            raise EvidenceAuditError("Read-only safety proof failed")
        payload = _audit_connection(connection, start=start_dt, cutoff=cutoff_dt)
    payload["window"] = {
        "start": utc_iso(start_dt),
        "cutoff": utc_iso(cutoff_dt),
        "bound": "[start, cutoff)",
    }
    return payload


def open_evidence_audit_database(path: Path) -> sqlite3.Connection:
    reject_protected_database_path(path)
    try:
        connection = open_read_only_database(
            path,
            require_supported_schema=False,
            assume_immutable_when_sidecars_absent=False,
            require_consistent_snapshot=True,
            require_immutable_source=False,
        )
    except DatabaseMissingError as exc:
        raise EvidenceAuditError(str(exc)) from exc
    except StorageError as exc:
        raise EvidenceAuditError(str(exc)) from exc
    if getattr(connection, "enable_load_extension", None) is not None:
        try:
            connection.enable_load_extension(False)
        except sqlite3.Error:
            pass
    return connection


def reject_protected_database_path(path: Path | str) -> None:
    candidate = Path(path)
    if candidate.name.casefold() == LIVE_RUNTIME_BASENAME:
        raise EvidenceAuditError("Protected runtime database basename is not allowed.")
    raw_key = _path_key(candidate)
    for known in KNOWN_LIVE_PATHS:
        if raw_key == _path_key(known):
            raise EvidenceAuditError("Protected live runtime database path is not allowed.")
    parts = {part.casefold() for part in candidate.parts}
    if PROTECTED_PATH_PART in parts:
        raise EvidenceAuditError("Protected live runtime database path is not allowed.")
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise EvidenceAuditError("Unable to resolve database path.") from exc
    resolved_key = _path_key(resolved)
    for known in KNOWN_LIVE_PATHS:
        try:
            known_resolved = _path_key(known.resolve())
        except OSError:
            known_resolved = _path_key(known)
        if resolved_key == known_resolved:
            raise EvidenceAuditError("Protected live runtime database path is not allowed.")
    if resolved.name.casefold() == LIVE_RUNTIME_BASENAME:
        raise EvidenceAuditError("Protected runtime database basename is not allowed.")
    if PROTECTED_PATH_PART in {part.casefold() for part in resolved.parts}:
        raise EvidenceAuditError("Protected live runtime database path is not allowed.")


def _path_key(path: Path) -> str:
    return str(path).replace("/", "\\").casefold()


def _audit_connection(
    connection: sqlite3.Connection,
    *,
    start: datetime,
    cutoff: datetime,
) -> dict[str, Any]:
    tables = _table_columns(connection)
    try:
        schema_user_version: int | str = identify_schema_version(connection)
    except StorageError:
        schema_user_version = UNAVAILABLE

    scan_coverage, scan_rows = _scan_run_window(connection, tables, start=start, cutoff=cutoff)
    observations = _child_counts(
        connection,
        tables,
        table="symbol_results",
        run_ids=[row["run_id"] for row in scan_rows],
        unit="observation",
    )
    candidates = _child_counts(
        connection,
        tables,
        table="setup_candidates",
        run_ids=[row["run_id"] for row in scan_rows],
        unit="candidate_row",
    )
    replay = _child_counts(
        connection,
        tables,
        table="replay_results",
        run_ids=[row["run_id"] for row in scan_rows],
        unit="replay_row",
    )
    lifecycle_events = _timestamped_rows(
        connection,
        tables,
        table="setup_lifecycle_events",
        timestamp_column="timestamp",
        sqlite_naive=False,
        start=start,
        cutoff=cutoff,
        extra_columns=("lifecycle_id",),
    )
    lifecycle_records = _timestamped_rows(
        connection,
        tables,
        table="setup_lifecycle_records",
        timestamp_column="last_seen_at",
        sqlite_naive=False,
        start=start,
        cutoff=cutoff,
        extra_columns=("lifecycle_id", "setup_identity", "symbol", "mode", "direction", "current_state"),
        mutable_snapshot=True,
    )
    outcome_progress = _timestamped_rows(
        connection,
        tables,
        table="setup_lifecycle_outcome_progress",
        timestamp_column="last_evaluated_at",
        sqlite_naive=False,
        start=start,
        cutoff=cutoff,
        extra_columns=("lifecycle_id", "plan_identity", "terminal_outcome"),
    )
    outcome_analytics = _timestamped_rows(
        connection,
        tables,
        table="setup_outcome_analytics",
        timestamp_column="created_at",
        sqlite_naive=True,
        start=start,
        cutoff=cutoff,
        extra_columns=("lifecycle_id", "final_outcome"),
    )
    public_events = _timestamped_rows(
        connection,
        tables,
        table="public_alert_events",
        timestamp_column="reserved_at",
        sqlite_naive=False,
        start=start,
        cutoff=cutoff,
        extra_columns=("event_key", "canonical_plan_id", "status"),
    )
    telegram_attempts = _timestamped_rows(
        connection,
        tables,
        table="telegram_alert_attempts",
        timestamp_column="attempted_at",
        sqlite_naive=False,
        start=start,
        cutoff=cutoff,
        extra_columns=("signal_id", "alert_type", "public_watchlist_plan_id", "lifecycle_state"),
    )

    identity = _identity_diagnostics(
        lifecycle_records=lifecycle_records,
        outcome_progress=outcome_progress,
        outcome_analytics=outcome_analytics,
        public_events=public_events,
        tables=tables,
    )
    return {
        "audit_version": AUDIT_VERSION,
        "contract_version": CONTRACT_VERSION,
        "database": {
            "schema_user_version": schema_user_version,
            "tables_present": sorted(tables),
            "tables_missing": sorted(_expected_tables() - set(tables)),
            "path_kind": "explicit_development_or_test",
            "path_checks": {
                "live_basename_rejected": True,
                "known_live_path_rejected": True,
                "unknown_drive_mapping_limitation": (
                    "Path-string checks cannot identify every unknown drive mapping."
                ),
            },
            "read_only": {
                "sqlite_uri_mode": "ro",
                "query_only": True,
                "immutable": False,
            },
        },
        "coverage": {
            "scan_runs": scan_coverage,
            "symbol_results": observations["coverage"],
            "setup_candidates": candidates["coverage"],
            "replay_results": replay["coverage"],
            "setup_lifecycle_events": lifecycle_events["coverage"],
            "setup_lifecycle_records": lifecycle_records["coverage"],
            "setup_lifecycle_outcome_progress": outcome_progress["coverage"],
            "setup_outcome_analytics": outcome_analytics["coverage"],
            "public_alert_events": public_events["coverage"],
            "telegram_alert_attempts": telegram_attempts["coverage"],
        },
        "scan_run_counters": _counter_sums(scan_rows, tables.get("scan_runs", set())),
        "observation_counts": {
            "symbol_results_in_window_runs": observations["count"],
            "setup_candidates_in_window_runs": candidates["count"],
            "replay_results_in_window_runs": replay["count"],
        },
        "lifecycle_event_counts": {
            "events_in_window": lifecycle_events["count"],
            "current_records_last_seen_in_window": lifecycle_records["count"],
            "current_record_filter_is_not_as_of": True,
        },
        "public_event_linkage": _public_linkage(public_events, telegram_attempts),
        "outcome_row_counts": {
            "outcome_progress_rows_in_window": outcome_progress["count"],
            "outcome_analytics_rows_in_window": outcome_analytics["count"],
        },
        "identity_diagnostics": identity,
        "join_cardinality": _join_cardinality(
            outcome_progress=outcome_progress,
            outcome_analytics=outcome_analytics,
            lifecycle_events=lifecycle_events,
        ),
        "count_distinctions": {
            "observation_count": observations["count"],
            "candidate_count": candidates["count"],
            "setup_identity_distinct": identity["setup_identity"]["distinct_values"],
            "lifecycle_id_distinct": identity["lifecycle_id"]["distinct_values"],
            "plan_identity_distinct": identity["plan_identity"]["distinct_values"],
            "outcome_progress_row_count": outcome_progress["count"],
            "outcome_analytics_row_count": outcome_analytics["count"],
            "unique_economic_plan_count": {
                "status": UNSAFE,
                "value": None,
                "reason": "lifecycle_id and plan_identity are not proven immutable economic plans",
            },
            "unique_trade_count": {
                "status": UNAVAILABLE,
                "value": None,
                "reason": (
                    "Requires a frozen plan version, explicit fill/exit policies, and one "
                    "authoritative outcome owner. DISTINCT identifiers are not used."
                ),
            },
        },
        "causality": {
            "summary_kind": "retrospective_persist_or_event_time",
            "as_of_reconstruction": "unsupported",
            "information_availability_timestamps": "absent",
            "mutable_snapshot_limitation": (
                "Filtering setup_lifecycle_records by last_seen_at does not reconstruct "
                "state at the cutoff."
            ),
            "mixed_clocks": (
                "scan_runs.timestamp is persist time; lifecycle events use processing time; "
                "outcome milestones may use candle event time; public reserved_at is delivery time."
            ),
        },
    }


def _expected_tables() -> set[str]:
    return {
        "scan_runs",
        "symbol_results",
        "setup_candidates",
        "replay_results",
        "setup_lifecycle_records",
        "setup_lifecycle_events",
        "setup_lifecycle_outcome_progress",
        "setup_outcome_analytics",
        "public_alert_events",
        "telegram_alert_attempts",
    }


def _table_columns(connection: sqlite3.Connection) -> dict[str, set[str]]:
    tables: dict[str, set[str]] = {}
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    for row in rows:
        name = str(row[0])
        columns = connection.execute(f"PRAGMA table_info({_quote_ident(name)})").fetchall()
        tables[name] = {str(column[1]) for column in columns}
    return tables


def _quote_ident(name: str) -> str:
    if not name.replace("_", "").isalnum():
        raise EvidenceAuditError("Unsupported table name")
    return f'"{name}"'


def _scan_run_window(
    connection: sqlite3.Connection,
    tables: Mapping[str, set[str]],
    *,
    start: datetime,
    cutoff: datetime,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    coverage = _missing_coverage("scan_runs", "timestamp", "persist_time")
    if "scan_runs" not in tables or "run_id" not in tables["scan_runs"] or "timestamp" not in tables["scan_runs"]:
        coverage["status"] = UNAVAILABLE
        return coverage, []
    columns = ["run_id", "timestamp"]
    for name in SCAN_RUN_COUNTERS:
        if name in tables["scan_runs"]:
            columns.append(name)
    selected = ", ".join(_quote_ident(name) for name in columns)
    fetched = connection.execute(f"SELECT {selected} FROM scan_runs").fetchall()
    in_window: list[dict[str, Any]] = []
    ambiguous = 0
    missing = 0
    parsed_values: list[datetime] = []
    for raw in fetched:
        row = dict(raw)
        parsed, status = try_parse_stored_timestamp(row.get("timestamp"), sqlite_utc_naive_allowed=False)
        if status in {"missing"}:
            missing += 1
            continue
        if parsed is None:
            ambiguous += 1
            continue
        parsed_values.append(parsed)
        if window_contains(parsed, start=start, cutoff=cutoff):
            in_window.append(row)
    coverage.update(
        {
            "status": "available",
            "timestamp_field": "timestamp",
            "timestamp_meaning": "persist_time",
            "rows_total": len(fetched),
            "rows_in_window": len(in_window),
            "rows_ambiguous_timestamp": ambiguous,
            "rows_missing_timestamp": missing,
            "min_timestamp": utc_iso(min(parsed_values)) if parsed_values else None,
            "max_timestamp": utc_iso(max(parsed_values)) if parsed_values else None,
        }
    )
    return coverage, in_window


def _child_counts(
    connection: sqlite3.Connection,
    tables: Mapping[str, set[str]],
    *,
    table: str,
    run_ids: Sequence[str],
    unit: str,
) -> dict[str, Any]:
    coverage = _missing_coverage(table, "run_id via parent scan_runs.timestamp", "parent_persist_time")
    if table not in tables or "run_id" not in tables[table]:
        coverage["status"] = UNAVAILABLE
        return {"coverage": coverage, "count": {"status": UNAVAILABLE, "value": None, "unit": unit}}
    coverage.update({"status": "available", "rows_in_window": 0})
    if not run_ids:
        return {
            "coverage": coverage,
            "count": {"status": "available", "value": 0, "unit": unit, "scope": "rows whose parent scan run persist time is in window"},
        }
    placeholders = ",".join("?" for _ in run_ids)
    value = connection.execute(
        f"SELECT COUNT(*) FROM {_quote_ident(table)} WHERE run_id IN ({placeholders})",
        tuple(run_ids),
    ).fetchone()[0]
    coverage["rows_in_window"] = int(value)
    return {
        "coverage": coverage,
        "count": {
            "status": "available",
            "value": int(value),
            "unit": unit,
            "scope": "rows whose parent scan run persist time is in window",
        },
    }


def _timestamped_rows(
    connection: sqlite3.Connection,
    tables: Mapping[str, set[str]],
    *,
    table: str,
    timestamp_column: str,
    sqlite_naive: bool,
    start: datetime,
    cutoff: datetime,
    extra_columns: Sequence[str] = (),
    mutable_snapshot: bool = False,
) -> dict[str, Any]:
    meaning = "mutable_snapshot_last_seen" if mutable_snapshot else timestamp_column
    coverage = _missing_coverage(table, timestamp_column, meaning)
    if table not in tables:
        coverage["status"] = UNAVAILABLE
        return {"coverage": coverage, "count": {"status": UNAVAILABLE, "value": None}, "rows": []}
    if timestamp_column not in tables[table]:
        coverage["status"] = UNAVAILABLE
        coverage["missing_column"] = timestamp_column
        return {"coverage": coverage, "count": {"status": UNAVAILABLE, "value": None}, "rows": []}
    columns = [timestamp_column, *extra_columns]
    present = [name for name in columns if name in tables[table]]
    selected = ", ".join(_quote_ident(name) for name in present)
    fetched = connection.execute(f"SELECT {selected} FROM {_quote_ident(table)}").fetchall()
    in_window: list[dict[str, Any]] = []
    ambiguous = 0
    missing = 0
    parsed_values: list[datetime] = []
    allow_naive = sqlite_naive and timestamp_column in SQLITE_NAIVE_UTC_COLUMNS
    for raw in fetched:
        row = dict(raw)
        parsed, status = try_parse_stored_timestamp(
            row.get(timestamp_column),
            sqlite_utc_naive_allowed=allow_naive,
        )
        if status == "missing":
            missing += 1
            continue
        if parsed is None:
            ambiguous += 1
            continue
        parsed_values.append(parsed)
        if window_contains(parsed, start=start, cutoff=cutoff):
            in_window.append(row)
    coverage.update(
        {
            "status": "available",
            "timestamp_field": timestamp_column,
            "timestamp_meaning": meaning,
            "mutable_snapshot": mutable_snapshot,
            "sqlite_utc_naive_accepted": allow_naive,
            "rows_total": len(fetched),
            "rows_in_window": len(in_window),
            "rows_ambiguous_timestamp": ambiguous,
            "rows_missing_timestamp": missing,
            "min_timestamp": utc_iso(min(parsed_values)) if parsed_values else None,
            "max_timestamp": utc_iso(max(parsed_values)) if parsed_values else None,
        }
    )
    return {
        "coverage": coverage,
        "count": {"status": "available", "value": len(in_window)},
        "rows": in_window,
    }


def _missing_coverage(table: str, timestamp_field: str, meaning: str) -> dict[str, Any]:
    return {
        "table": table,
        "timestamp_field": timestamp_field,
        "timestamp_meaning": meaning,
        "status": UNAVAILABLE,
    }


def _counter_sums(rows: Sequence[Mapping[str, Any]], columns: set[str]) -> dict[str, Any]:
    payload: dict[str, Any] = {}
    for name in SCAN_RUN_COUNTERS:
        if name not in columns:
            payload[name] = {
                "status": UNAVAILABLE,
                "value": None,
                "unit": "scan_run_counter_sum",
                "research_funnel_suitable": False,
            }
            continue
        total = 0
        for row in rows:
            total += int(row.get(name) or 0)
        payload[name] = {
            "status": "available",
            "value": total,
            "unit": "scan_run_counter_sum",
            "scope": "sum of stored per-run counters whose persist timestamp is in window",
            "unique_economic_plans_counted": False,
            "research_funnel_suitable": False,
            "zero_semantics": "zero is a stored counter, not proof of missing evidence in another table",
        }
    return payload


def _identity_diagnostics(
    *,
    lifecycle_records: Mapping[str, Any],
    outcome_progress: Mapping[str, Any],
    outcome_analytics: Mapping[str, Any],
    public_events: Mapping[str, Any],
    tables: Mapping[str, set[str]],
) -> dict[str, Any]:
    setup_values = [
        str(row.get("setup_identity") or "")
        for row in lifecycle_records.get("rows", ())
        if "setup_identity" in row
    ]
    lifecycle_ids = [
        str(row.get("lifecycle_id") or "")
        for row in lifecycle_records.get("rows", ())
        if "lifecycle_id" in row
    ]
    plan_values = [
        str(row.get("plan_identity") or "")
        for row in outcome_progress.get("rows", ())
        if "plan_identity" in row
    ]
    return {
        "setup_identity": _cardinality_report(
            setup_values,
            available="setup_lifecycle_records" in tables and "setup_identity" in tables.get("setup_lifecycle_records", ()),
            generic_values={"", "N/A", "n/a"},
        ),
        "lifecycle_id": _cardinality_report(
            lifecycle_ids,
            available="setup_lifecycle_records" in tables and "lifecycle_id" in tables.get("setup_lifecycle_records", ()),
        ),
        "plan_identity": _cardinality_report(
            plan_values,
            available="setup_lifecycle_outcome_progress" in tables
            and "plan_identity" in tables.get("setup_lifecycle_outcome_progress", ()),
        ),
        "symbol_mode_direction": _mode_pairs(lifecycle_records.get("rows", ())),
        "unique_economic_plans": {
            "status": UNSAFE,
            "reason": "No immutable plan_version_id is persisted; COUNT(DISTINCT lifecycle_id) is not unique plans.",
        },
        "public_event_keys": _cardinality_report(
            [str(row.get("event_key") or "") for row in public_events.get("rows", ()) if "event_key" in row],
            available="public_alert_events" in tables,
        ),
        "outcome_analytics_lifecycle_ids": _cardinality_report(
            [str(row.get("lifecycle_id") or "") for row in outcome_analytics.get("rows", ()) if "lifecycle_id" in row],
            available="setup_outcome_analytics" in tables,
        ),
    }


def _cardinality_report(
    values: Sequence[str],
    *,
    available: bool,
    generic_values: set[str] | None = None,
) -> dict[str, Any]:
    if not available:
        return {"status": UNAVAILABLE, "rows": None, "distinct_values": None, "repeated_values": None}
    counter = Counter(values)
    generic = generic_values or set()
    repeated = sorted(
        [
            {"value_kind": "generic_or_na" if item in generic else "repeated", "count": count}
            for item, count in counter.items()
            if count > 1 or item in generic
        ],
        key=lambda item: (-item["count"], item["value_kind"]),
    )
    # Do not emit raw identity strings that might be large; report counts only.
    generic_rows = sum(counter[item] for item in generic if item in counter)
    return {
        "status": "available",
        "rows": len(values),
        "distinct_values": len(counter),
        "generic_or_na_rows": generic_rows,
        "values_with_repetition": sum(1 for count in counter.values() if count > 1),
        "repeated_value_examples_omitted": True,
        "repetition_summary": repeated[:20],
    }


def _mode_pairs(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if not rows or any(name not in rows[0] for name in ("symbol", "mode", "direction")):
        return {"status": UNAVAILABLE}
    groups: Counter[tuple[str, str]] = Counter()
    for row in rows:
        groups[(str(row.get("symbol")), str(row.get("direction")))] += 1
    multi_mode = 0
    by_symbol_direction: dict[tuple[str, str], set[str]] = {}
    for row in rows:
        key = (str(row.get("symbol")), str(row.get("direction")))
        by_symbol_direction.setdefault(key, set()).add(str(row.get("mode")))
    multi_mode = sum(1 for modes in by_symbol_direction.values() if len(modes) > 1)
    return {
        "status": "available",
        "symbol_direction_pairs_with_multiple_modes": multi_mode,
        "note": "Lifecycle storage is mode-partitioned; public economic ids may still collapse modes.",
    }


def _join_cardinality(
    *,
    outcome_progress: Mapping[str, Any],
    outcome_analytics: Mapping[str, Any],
    lifecycle_events: Mapping[str, Any],
) -> dict[str, Any]:
    progress_by_lifecycle = Counter(
        str(row.get("lifecycle_id"))
        for row in outcome_progress.get("rows", ())
        if row.get("lifecycle_id") is not None
    )
    analytics_by_lifecycle = Counter(
        str(row.get("lifecycle_id"))
        for row in outcome_analytics.get("rows", ())
        if row.get("lifecycle_id") is not None
    )
    events_by_lifecycle = Counter(
        str(row.get("lifecycle_id"))
        for row in lifecycle_events.get("rows", ())
        if row.get("lifecycle_id") is not None
    )
    return {
        "outcome_progress_per_lifecycle": _fanout(progress_by_lifecycle, "progress_row"),
        "outcome_analytics_per_lifecycle": _fanout(analytics_by_lifecycle, "analytics_row"),
        "lifecycle_events_per_lifecycle": _fanout(events_by_lifecycle, "event_row"),
        "unique_trade_inflation_guard": {
            "tp_progress_counted_as_trades": False,
            "distinct_lifecycle_treated_as_unique_plans": False,
            "distinct_lifecycle_treated_as_unique_trades": False,
        },
    }


def _fanout(counter: Counter[str], unit: str) -> dict[str, Any]:
    if not counter:
        return {"status": "available", "max_rows_per_lifecycle": 0, "lifecycles_with_multiple_rows": 0, "unit": unit}
    return {
        "status": "available",
        "max_rows_per_lifecycle": max(counter.values()),
        "lifecycles_with_multiple_rows": sum(1 for count in counter.values() if count > 1),
        "unit": unit,
    }


def _public_linkage(
    public_events: Mapping[str, Any],
    telegram_attempts: Mapping[str, Any],
) -> dict[str, Any]:
    if public_events["coverage"].get("status") != "available":
        public_status = UNAVAILABLE
        public_count = None
        with_plan = None
    else:
        public_status = "available"
        public_count = public_events["count"]["value"]
        with_plan = sum(
            1
            for row in public_events.get("rows", ())
            if str(row.get("canonical_plan_id") or "").strip() not in {"", "N/A"}
        )
    if telegram_attempts["coverage"].get("status") != "available":
        attempt_status = UNAVAILABLE
        attempt_count = None
    else:
        attempt_status = "available"
        attempt_count = telegram_attempts["count"]["value"]
    return {
        "public_alert_events": {"status": public_status, "rows_in_window": public_count, "rows_with_canonical_plan_id": with_plan},
        "telegram_alert_attempts": {"status": attempt_status, "rows_in_window": attempt_count},
        "note": "Public event counts are delivery events, not trades. Historical SENT rows are not proven immutable economics.",
    }


__all__ = [
    "AUDIT_VERSION",
    "EvidenceAuditError",
    "KNOWN_LIVE_PATHS",
    "LIVE_RUNTIME_BASENAME",
    "build_evidence_baseline",
    "dumps_evidence_payload",
    "open_evidence_audit_database",
    "reject_protected_database_path",
]
