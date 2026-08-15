"""Read-only, bounded post-restart scanner-funnel reporting.

This module deliberately reads only evidence that is already persisted by the
runtime.  It neither imports nor invokes scanner, lifecycle, or Telegram
services, so using it cannot change qualification or delivery behaviour.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
import json
from pathlib import Path
import re
import sqlite3
import subprocess
from typing import Any, Iterable, Mapping, Sequence

from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.state_machine import ALLOWED_TRANSITIONS
from app.storage.database import (
    StorageError,
    identify_schema_version,
    open_read_only_database,
    read_only_connection_safety_proof,
)


TOOL_VERSION = "1.0.0"
DEFAULT_MINIMUM_MEANINGFUL_WINDOW_SECONDS = 72 * 60 * 60
DEFAULT_MAX_ROWS_PER_SOURCE = 250_000
DEFAULT_MAX_DETAIL_RECORDS = 1_000
DEFAULT_DOMINANT_BLOCKER_SHARE = Decimal("0.5")
QUERY_BATCH_SIZE = 1_000
MAX_JSON_FIELD_BYTES = 16 * 1024
SOURCE_MODE_QUIESCENT_IMMUTABLE = "quiescent-immutable"
MARKET_SCARCITY_SCAN_COUNTER_FIELDS = (
    "symbols_scanned", "symbols_requested", "symbols_completed", "total_valid_setups",
    "near_misses", "rejected", "data_issues", "valid_activations", "still_watching",
    "rejected_no_edge", "actionable_setups", "confirmed_setups",
    "blocked_a_grade_by_scoring", "blocked_a_grade_by_target",
    "blocked_a_grade_by_entry_window", "blocked_a_grade_by_trust",
    "fatal_target_blocks", "soft_target_warnings",
)
MARKET_SCARCITY_DISPOSITION_FIELDS = (
    "status", "display_bucket", "failed_gate", "rejection_reason",
    "next_trigger_needed", "action_label", "pullback_status", "portfolio_decision",
)
DIRECT_EVIDENCE_SAMPLE_LIMIT = 100

# The audit never SELECTs every column. These compact evidence allowlists omit
# raw market, derivatives, volume-profile, message, and metadata payloads.
SOURCE_COLUMN_ALLOWLIST: dict[str, frozenset[str]] = {
    "scan_runs": frozenset({
        "run_id", "timestamp", "symbols_scanned", "symbols_requested", "symbols_completed",
        "total_valid_setups", "near_misses", "rejected", "data_issues", "valid_activations",
        "still_watching", "rejected_no_edge", "actionable_setups", "confirmed_setups",
        "blocked_a_grade_by_scoring", "blocked_a_grade_by_target", "blocked_a_grade_by_entry_window",
        "blocked_a_grade_by_trust", "fatal_target_blocks", "soft_target_warnings", "symbols_json",
        "timeframes_json", "runtime_stats_json",
    }),
    "symbol_results": frozenset({
        "id", "run_id", "symbol", "status", "display_bucket", "failed_gate", "final_failed_gate",
        "rejection_reason", "next_trigger_needed", "action_label", "pullback_status", "portfolio_decision",
        "readiness_score", "opportunity_score", "technical_score", "score", "setup_quality_score",
        "quality_grade", "final_quality_grade", "candidate_quality_grade", "grade", "direction", "side",
        "bias", "mode", "strategy_mode", "raw_result_json",
    }),
    "setup_candidates": frozenset({
        "id", "run_id", "symbol", "mode", "direction", "failed_gate", "final_failed_gate",
        "target_failure", "readiness_score", "opportunity_score", "technical_score", "score",
        "setup_quality_score", "quality_grade", "final_quality_grade", "candidate_quality_grade", "grade",
    }),
    "setup_lifecycle_events": frozenset({
        "event_id", "lifecycle_id", "timestamp", "symbol", "from_state", "to_state", "scan_run_id",
    }),
    "setup_lifecycle_records": frozenset({
        "lifecycle_id", "symbol", "mode", "direction", "current_state", "first_seen_at", "last_seen_at",
        "last_transition_at",
    }),
    "setup_lifecycle_outcome_progress": frozenset({
        "id", "lifecycle_id", "tp1_at", "tp2_at", "tp3_at", "stop_at", "invalidated_at", "outcome_at",
        "terminal_outcome", "integrity_status", "plan_identity",
    }),
    "telegram_alert_attempts": frozenset({
        "id", "signal_id", "attempted_at", "sent_at", "telegram_status", "scan_run_id", "dedupe_status",
        "dedupe_reason", "message_hash", "error_message", "last_error_message",
    }),
}
OPTIONAL_JSON_COLUMNS = frozenset({"timeframes_json", "runtime_stats_json", "symbols_json", "raw_result_json"})
TIMEFRAME_COMPACT_FIELDS = frozenset({
    "htf_timeframe", "bias_timeframe", "execution_timeframe", "confirmation_timeframe",
    "context", "htf", "bias", "structure", "execution", "confirmation",
    "structure_timeframe", "structure_context_timeframe", "structure_analysis_timeframe",
})

PROCESS_MEMORY_COMPACT_FIELDS = frozenset({
    "measurement_status",
    "source",
    "rss_start_bytes",
    "rss_end_bytes",
    "rss_observed_peak_bytes",
    "rss_delta_bytes",
    "samples_attempted",
    "samples_succeeded",
    "samples_failed",
})



RAW_RESULT_COMPACT_FIELDS = frozenset({
    "failed_gate", "final_failed_gate", "gates_failed", "failed_gates", "min_score_for_idea",
    "opportunity_score", "technical_score", "readiness_score", "score", "quality_grade",
    "final_quality_grade", "candidate_quality_grade", "grade", "direction", "side", "bias",
    "mode", "strategy_mode", "lifecycle_state",
    "strategy_diagnostics",
})
NA_VALUES = frozenset({"", "n/a", "na", "none", "null", "not_recorded"})
TERMINAL_STATES = frozenset(
    {
        SetupLifecycleState.TP_HIT.value,
        SetupLifecycleState.SL_HIT.value,
        SetupLifecycleState.INVALIDATED.value,
        SetupLifecycleState.EXPIRED.value,
    }
)
ACTIVE_STATE_ORDER = {
    SetupLifecycleState.DISCOVERED.value: 0,
    SetupLifecycleState.WATCHLISTED.value: 1,
    SetupLifecycleState.STALKING.value: 2,
    SetupLifecycleState.TRIGGERED.value: 3,
    SetupLifecycleState.CONFIRMED.value: 4,
    SetupLifecycleState.ACTIONABLE_A_GRADE.value: 4,
    SetupLifecycleState.A_GRADE_WATCH.value: 4,
    SetupLifecycleState.EXECUTING.value: 5,
    SetupLifecycleState.MANAGING.value: 6,
}
FUNNEL_STAGES: tuple[tuple[str, frozenset[str]], ...] = (
    ("WATCH", frozenset({SetupLifecycleState.WATCHLISTED.value, SetupLifecycleState.A_GRADE_WATCH.value})),
    ("STALKING", frozenset({SetupLifecycleState.STALKING.value})),
    ("TRIGGERED", frozenset({SetupLifecycleState.TRIGGERED.value})),
    ("CONFIRMED", frozenset({SetupLifecycleState.CONFIRMED.value})),
    ("EXECUTING_OR_ACTIVE", frozenset({SetupLifecycleState.EXECUTING.value, SetupLifecycleState.MANAGING.value})),
    ("TERMINAL", TERMINAL_STATES),
)
SAFE_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
SENSITIVE_VALUE = re.compile(
    r"(?i)\b(token|secret|password|api[_-]?key)\b\s*[:=]\s*[^\s,;]+"
)


class FunnelAuditError(RuntimeError):
    """Raised when a requested audit cannot be completed safely."""


class IncompatibleFunnelSchemaError(FunnelAuditError):
    """Raised when a SQLite file lacks the minimum scan-run evidence."""


@dataclass(frozen=True)
class AuditWindow:
    start: datetime
    end: datetime

    @property
    def seconds(self) -> int:
        return int((self.end - self.start).total_seconds())


@dataclass(frozen=True)
class QuiescentSourceMetadata:
    database_path: Path
    size_bytes: int
    mtime_ns: int
    ctime_ns: int

    def as_report_value(self) -> dict[str, int]:
        return {
            "size_bytes": self.size_bytes,
            "mtime_ns": self.mtime_ns,
            "ctime_ns": self.ctime_ns,
        }


@dataclass
class QueryLimits:
    max_rows_per_source: int
    truncated_sources: set[str]
    optional_json_unavailable_sources: set[str]
    malformed: Counter[str]
    observed_rows_by_source: dict[str, int]


def parse_utc_timestamp(value: str | datetime, *, argument_name: str) -> datetime:
    """Parse an ISO-8601 timestamp, requiring an explicit UTC offset."""

    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        normalized = value.strip()
        if normalized.endswith(("Z", "z")):
            normalized = f"{normalized[:-1]}+00:00"
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError as exc:
            raise FunnelAuditError(
                f"{argument_name} must be an ISO-8601 UTC timestamp, for example 2026-07-29T08:40:54Z."
            ) from exc
    else:
        raise FunnelAuditError(f"{argument_name} must be an ISO-8601 UTC timestamp.")
    if parsed.tzinfo is None:
        raise FunnelAuditError(f"{argument_name} must include an explicit UTC offset or Z suffix.")
    return parsed.astimezone(UTC)


def _prepare_quiescent_immutable_source(
    database: Path,
    source_mode: str,
) -> QuiescentSourceMetadata:
    if source_mode != SOURCE_MODE_QUIESCENT_IMMUTABLE:
        raise FunnelAuditError(
            "NO-GO: this audit supports only source_mode=quiescent-immutable; "
            "it never uses immutable mode against an active writer."
        )
    try:
        resolved = database.resolve(strict=True)
    except OSError as exc:
        raise FunnelAuditError(f"NO-GO: database source is unavailable: {database}") from exc
    if not resolved.is_file():
        raise FunnelAuditError(f"NO-GO: database source is not a file: {resolved}")
    sidecars = _sqlite_sidecars(resolved)
    if any(path.exists() for path in sidecars):
        raise FunnelAuditError(
            "NO-GO: quiescent immutable source requires both SQLite -wal and -shm sidecars to be absent "
            f"before opening: {resolved}. Stop the scanner and verify it is absent before retrying."
        )
    try:
        stat = resolved.stat()
    except OSError as exc:
        raise FunnelAuditError(f"NO-GO: unable to capture source metadata: {resolved}") from exc
    return QuiescentSourceMetadata(
        database_path=resolved,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        ctime_ns=stat.st_ctime_ns,
    )


def _verify_quiescent_immutable_source(
    before: QuiescentSourceMetadata,
) -> QuiescentSourceMetadata:
    sidecars = _sqlite_sidecars(before.database_path)
    if any(path.exists() for path in sidecars):
        raise FunnelAuditError(
            "NO-GO: a SQLite -wal or -shm sidecar appeared during the audit; "
            "the source was not quiescent and no report was written."
        )
    try:
        stat = before.database_path.stat()
    except OSError as exc:
        raise FunnelAuditError("NO-GO: source metadata could not be rechecked after the audit.") from exc
    after = QuiescentSourceMetadata(
        database_path=before.database_path,
        size_bytes=stat.st_size,
        mtime_ns=stat.st_mtime_ns,
        ctime_ns=stat.st_ctime_ns,
    )
    if after != before:
        raise FunnelAuditError(
            "NO-GO: source metadata changed during the audit; "
            "the source was not quiescent and no report was written."
        )
    return after


def _sqlite_sidecars(database_path: Path) -> tuple[Path, Path]:
    return (Path(f"{database_path}-wal"), Path(f"{database_path}-shm"))


def build_post_restart_funnel_report(
    database_path: Path | str,
    *,
    window_start_utc: str | datetime,
    window_end_utc: str | datetime,
    expected_watch_interval_sec: int,
    report_label: str,
    minimum_meaningful_window_sec: int = DEFAULT_MINIMUM_MEANINGFUL_WINDOW_SECONDS,
    dominant_blocker_minimum_share: Decimal | str | float = DEFAULT_DOMINANT_BLOCKER_SHARE,
    stall_threshold_sec: int | None = None,
    max_rows_per_source: int = DEFAULT_MAX_ROWS_PER_SOURCE,
    max_detail_records: int = DEFAULT_MAX_DETAIL_RECORDS,
    generated_at_utc: str | datetime | None = None,
    repository_root: Path | str | None = None,
    source_mode: str = SOURCE_MODE_QUIESCENT_IMMUTABLE,
) -> dict[str, Any]:
    """Build an observational report without changing the supplied SQLite file.

    The only database connection is ``mode=ro`` plus verified SQLite
    ``query_only`` protection supplied by :func:`open_read_only_database`.
    Queries are timestamp-filtered and capped.  Sources without a bounded path
    are not queried and are reported as ``NOT_RECORDED`` or ``NOT_VERIFIABLE``.
    """

    database = Path(database_path)
    window = AuditWindow(
        start=parse_utc_timestamp(window_start_utc, argument_name="window_start_utc"),
        end=parse_utc_timestamp(window_end_utc, argument_name="window_end_utc"),
    )
    if window.end <= window.start:
        raise FunnelAuditError("window_end_utc must be later than window_start_utc.")
    if expected_watch_interval_sec <= 0:
        raise FunnelAuditError("expected_watch_interval_sec must be greater than zero.")
    if minimum_meaningful_window_sec <= 0:
        raise FunnelAuditError("minimum_meaningful_window_sec must be greater than zero.")
    if stall_threshold_sec is not None and stall_threshold_sec <= 0:
        raise FunnelAuditError("stall_threshold_sec must be greater than zero when supplied.")
    if max_rows_per_source <= 0 or max_detail_records <= 0:
        raise FunnelAuditError("max_rows_per_source and max_detail_records must be greater than zero.")
    normalized_label = _report_label(report_label)
    dominant_share = _decimal_or_none(dominant_blocker_minimum_share)
    if dominant_share is None or dominant_share <= 0 or dominant_share > 1:
        raise FunnelAuditError("dominant_blocker_minimum_share must be greater than zero and no more than one.")
    generated_at = (
        parse_utc_timestamp(generated_at_utc, argument_name="generated_at_utc")
        if generated_at_utc is not None
        else datetime.now(UTC)
    )
    source_before = _prepare_quiescent_immutable_source(database, source_mode)

    limits = QueryLimits(
        max_rows_per_source=max_rows_per_source,
        truncated_sources=set(),
        optional_json_unavailable_sources=set(),
        malformed=Counter(),
        observed_rows_by_source={},
    )
    try:
        with open_read_only_database(
            source_before.database_path,
            require_supported_schema=False,
            assume_immutable_when_sidecars_absent=True,
            require_immutable_source=True,
            include_immutable_safety_proof=True,
        ) as connection:
            safety = read_only_connection_safety_proof(connection)
            schema_version = identify_schema_version(connection)
            tables = _table_columns(connection)
            if "scan_runs" not in tables or not {"run_id", "timestamp"} <= tables["scan_runs"]:
                raise IncompatibleFunnelSchemaError(
                    "Incompatible schema: scan_runs with run_id and timestamp is required for a bounded scanner-funnel audit."
                )
            query_safety = _verify_query_safety(connection, tables)
            scan_rows = (
                _load_scan_runs(connection, tables, window, limits)
                if _query_is_verified(query_safety, "scan_runs_timestamp")
                else []
            )
            scan_rows = _strict_window_rows(scan_rows, "timestamp", window, source="scan_runs")
            run_ids = [str(row["run_id"]) for row in scan_rows if _text(row.get("run_id"))]
            symbol_rows = (
                _load_rows_for_run_ids(connection, tables, "symbol_results", run_ids, limits)
                if _query_is_verified(query_safety, "symbol_results_run_id")
                else []
            )
            candidate_rows = (
                _load_rows_for_run_ids(connection, tables, "setup_candidates", run_ids, limits)
                if _query_is_verified(query_safety, "setup_candidates_run_id")
                else []
            )
            symbols = _observed_symbols(scan_rows, symbol_rows)
            lifecycle_events = (
                _load_lifecycle_events(connection, tables, symbols, window, limits)
                if _query_is_verified(query_safety, "lifecycle_events_symbol_timestamp")
                else []
            )
            lifecycle_events = _strict_window_rows(lifecycle_events, "timestamp", window, source="setup_lifecycle_events")
            lifecycle_records = (
                _load_lifecycle_records(
                    connection,
                    tables,
                    symbols,
                    [str(row["lifecycle_id"]) for row in lifecycle_events if _text(row.get("lifecycle_id"))],
                    window,
                    limits,
                )
                if _query_is_verified(query_safety, "lifecycle_records_lifecycle_id")
                and _query_is_verified(query_safety, "lifecycle_records_symbol")
                else []
            )
            lifecycle_ids = sorted(
                {
                    str(row["lifecycle_id"])
                    for row in (*lifecycle_events, *lifecycle_records)
                    if _text(row.get("lifecycle_id"))
                }
            )
            outcome_rows = (
                _load_rows_for_lifecycle_ids(
                    connection,
                    tables,
                    "setup_lifecycle_outcome_progress",
                    lifecycle_ids,
                    limits,
                )
                if _query_is_verified(query_safety, "lifecycle_outcome_lifecycle_id")
                else []
            )
            if _query_is_verified(query_safety, "telegram_scan_run_id"):
                telegram_rows, telegram_scope = _load_telegram_rows(
                    connection,
                    tables,
                    run_ids,
                    window,
                    limits,
                )
            else:
                telegram_rows = []
                telegram_scope = _query_skip_reason(query_safety, "telegram_scan_run_id")
    except StorageError as exc:
        raise FunnelAuditError(f"Read-only database audit failed safely: {exc}") from exc
    except sqlite3.Error as exc:
        raise FunnelAuditError(f"Read-only database audit failed safely: {type(exc).__name__}.") from exc
    finally:
        source_after = _verify_quiescent_immutable_source(source_before)

    malformed = limits.malformed
    scan_health = _scan_health(scan_rows, symbol_rows, candidate_rows, window, expected_watch_interval_sec, malformed)
    coverage = _data_coverage(
        tables=tables,
        scan_rows=scan_rows,
        symbol_rows=symbol_rows,
        candidate_rows=candidate_rows,
        lifecycle_events=lifecycle_events,
        lifecycle_records=lifecycle_records,
        outcome_rows=outcome_rows,
        telegram_rows=telegram_rows,
        limits=limits,
        malformed=malformed,
        window=window,
        query_safety=query_safety,
    )
    market_scarcity_evidence = _market_scarcity_direct_evidence(
        scan_rows=scan_rows,
        symbol_rows=symbol_rows,
        candidate_rows=candidate_rows,
        tables=tables,
        limits=limits,
        query_safety=query_safety,
    )
    scan_health["market_scarcity_direct_evidence"] = market_scarcity_evidence
    scan_time_by_id = {
        str(row["run_id"]): timestamp
        for row in scan_rows
        if _text(row.get("run_id")) and (timestamp := _row_timestamp(row, "timestamp")) is not None
    }
    gate_observations = _gate_observations(symbol_rows, scan_time_by_id, malformed)
    lifecycle = _lifecycle_quality(lifecycle_events, lifecycle_records, window, stall_threshold_sec)
    funnel = _funnel(symbol_rows, candidate_rows, lifecycle_events)
    gates = _gate_failures(gate_observations)
    if "symbol_results" in limits.optional_json_unavailable_sources:
        gates["optional_raw_json_evidence"] = (
            "NOT_VERIFIABLE: oversized optional raw_result_json was not selected or parsed; "
            "direct scalar failed_gate evidence remains separately observed."
        )
    outcomes = _outcomes(outcome_rows, lifecycle_events, lifecycle_records, window)
    target_inside_chop = _target_inside_chop_review(
        gate_observations,
        candidate_rows,
        lifecycle_events,
        lifecycle_records,
        outcome_rows,
        max_detail_records,
    )
    duplicate_cooldown = _duplicate_and_cooldown(telegram_rows, tables)
    telegram = _telegram_funnel(telegram_rows, tables, telegram_scope)
    timeframe = _timeframe_verification(scan_rows, malformed, repository_root, symbol_rows=symbol_rows)
    verdict = _verdict(
        window=window,
        scan_health=scan_health,
        funnel=funnel,
        gates=gates,
        lifecycle=lifecycle,
        telegram=telegram,
        coverage=coverage,
        market_scarcity_evidence=market_scarcity_evidence,
        minimum_meaningful_window_sec=minimum_meaningful_window_sec,
        dominant_share=dominant_share,
        stall_threshold_sec=stall_threshold_sec,
    )
    source_commit = _source_commit(scan_rows, malformed)
    report = {
        "audit_identity": {
            "report_label": normalized_label,
            "tool_version": TOOL_VERSION,
            "generated_utc": _utc_text(generated_at),
            "requested_window_start_utc": _utc_text(window.start),
            "requested_window_end_utc": _utc_text(window.end),
            "effective_window_start_utc": _utc_text(window.start),
            "effective_window_end_utc": _utc_text(window.end),
            "window_seconds": window.seconds,
            "database_path": str(source_before.database_path),
            "database_file_size_bytes": source_before.size_bytes,
            "schema_version": schema_version,
            "source_commit": source_commit,
            "repository_commit": _repository_commit(repository_root),
            "read_only_status": {
                "status": "VERIFIED_READ_ONLY",
                **safety,
                "source_mode": "QUIESCENT_IMMUTABLE",
                "immutable_requested": True,
                "sidecars_absent_before": True,
                "sidecars_absent_after": True,
                "source_metadata_before": source_before.as_report_value(),
                "source_metadata_after": source_after.as_report_value(),
                "source_metadata_unchanged": True,
                "no_database_copy_backup_checkpoint_vacuum_or_migration_performed": True,
            },
        },
        "data_coverage_and_reliability": coverage,
        "scan_health": scan_health,
        "funnel": funnel,
        "gate_failures": gates,
        "target_inside_chop_review": target_inside_chop,
        "lifecycle_quality": lifecycle,
        "outcomes": outcomes,
        "duplicate_and_cooldown_behavior": duplicate_cooldown,
        "telegram_delivery_funnel": telegram,
        "timeframe_verification": timeframe,
        "verdict": verdict,
    }
    return report


def render_post_restart_funnel_text(report: Mapping[str, Any]) -> str:
    """Render the report in a deterministic, line-oriented text form."""

    ordered_sections = (
        "audit_identity",
        "data_coverage_and_reliability",
        "scan_health",
        "funnel",
        "gate_failures",
        "target_inside_chop_review",
        "lifecycle_quality",
        "outcomes",
        "duplicate_and_cooldown_behavior",
        "telegram_delivery_funnel",
        "timeframe_verification",
        "verdict",
    )
    lines = ["POST-RESTART SCANNER FUNNEL AUDIT", ""]
    for section in ordered_sections:
        lines.append(section.upper().replace("_", " "))
        lines.append("-" * len(lines[-1]))
        lines.append(json.dumps(report.get(section, "NOT_RECORDED"), indent=2, sort_keys=True, ensure_ascii=False))
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def write_post_restart_funnel_reports(
    report: Mapping[str, Any],
    output_dir: Path | str,
) -> tuple[Path, Path]:
    """Write deterministic JSON and text files without touching the database."""

    destination = Path(output_dir)
    identity = report.get("audit_identity")
    if not isinstance(identity, Mapping):
        raise FunnelAuditError("Report has no audit_identity; refusing to choose output names.")
    label = _report_label(str(identity.get("report_label", "")))
    start = _filename_timestamp(str(identity.get("effective_window_start_utc", "")))
    end = _filename_timestamp(str(identity.get("effective_window_end_utc", "")))
    if not start or not end:
        raise FunnelAuditError("Report has no usable effective audit window; refusing to write output.")
    try:
        destination.mkdir(parents=True, exist_ok=True)
        text_path = destination / f"{label}_{start}_to_{end}.txt"
        json_path = destination / f"{label}_{start}_to_{end}.json"
        if text_path.exists() or json_path.exists():
            raise FunnelAuditError(
                f"Refusing to overwrite an existing audit report: {text_path if text_path.exists() else json_path}"
            )
        text_path.write_text(render_post_restart_funnel_text(report), encoding="utf-8", newline="\n")
        json_path.write_text(json.dumps(report, indent=2, sort_keys=True, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        raise FunnelAuditError(f"Unable to write audit reports to {destination}") from exc
    return text_path, json_path


def _table_columns(connection: sqlite3.Connection) -> dict[str, set[str]]:
    rows = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name ASC"
    ).fetchall()
    tables: dict[str, set[str]] = {}
    for row in rows:
        table = str(row[0])
        if not SAFE_IDENTIFIER.fullmatch(table):
            continue
        columns = connection.execute(f"PRAGMA table_info({_identifier(table)})").fetchall()
        tables[table] = {str(column[1]) for column in columns}
    return tables


def _verify_query_safety(
    connection: sqlite3.Connection,
    tables: Mapping[str, set[str]],
) -> dict[str, dict[str, Any]]:
    """Verify bounded access paths before any high-volume runtime query."""

    requirements = (
        ("scan_runs_timestamp", "scan_runs", {"run_id", "timestamp"},
         "SELECT run_id FROM scan_runs WHERE timestamp >= ? AND timestamp < ? ORDER BY timestamp ASC LIMIT 1", ("", "~")),
        ("symbol_results_run_id", "symbol_results", {"run_id"},
         "SELECT run_id FROM symbol_results WHERE run_id = ? LIMIT 1", ("",)),
        ("setup_candidates_run_id", "setup_candidates", {"run_id"},
         "SELECT run_id FROM setup_candidates WHERE run_id = ? LIMIT 1", ("",)),
        ("lifecycle_events_symbol_timestamp", "setup_lifecycle_events", {"symbol", "timestamp"},
         "SELECT lifecycle_id FROM setup_lifecycle_events WHERE symbol = ? AND timestamp >= ? AND timestamp < ? LIMIT 1", ("", "", "~")),
        ("lifecycle_records_lifecycle_id", "setup_lifecycle_records", {"lifecycle_id"},
         "SELECT lifecycle_id FROM setup_lifecycle_records WHERE lifecycle_id = ? LIMIT 1", ("",)),
        ("lifecycle_records_symbol", "setup_lifecycle_records", {"symbol", "first_seen_at", "last_seen_at"},
         "SELECT lifecycle_id FROM setup_lifecycle_records WHERE symbol = ? AND last_seen_at >= ? AND first_seen_at < ? LIMIT 1", ("", "", "~")),
        ("lifecycle_outcome_lifecycle_id", "setup_lifecycle_outcome_progress", {"lifecycle_id"},
         "SELECT lifecycle_id FROM setup_lifecycle_outcome_progress WHERE lifecycle_id = ? LIMIT 1", ("",)),
        ("telegram_scan_run_id", "telegram_alert_attempts", {"scan_run_id"},
         "SELECT scan_run_id FROM telegram_alert_attempts WHERE scan_run_id = ? LIMIT 1", ("",)),
    )
    result: dict[str, dict[str, Any]] = {}
    for key, table, required, sql, parameters in requirements:
        if table not in tables:
            result[key] = {"status": "NOT_RECORDED", "table": table, "reason": f"{table} table is absent."}
            continue
        missing = sorted(required - tables[table])
        if missing:
            result[key] = {
                "status": "NOT_VERIFIABLE",
                "table": table,
                "reason": f"Required bounded-query columns are absent: {', '.join(missing)}.",
            }
            continue
        try:
            plan_rows = connection.execute(f"EXPLAIN QUERY PLAN {sql}", parameters).fetchall()
        except sqlite3.Error as exc:
            result[key] = {
                "status": "NOT_VERIFIABLE",
                "table": table,
                "reason": f"Unable to verify a bounded query plan safely: {type(exc).__name__}.",
            }
            continue
        details = [str(row[3]) for row in plan_rows]
        safe_search = any("SEARCH" in detail.upper() and "USING" in detail.upper() for detail in details)
        unsafe_scan = any(f"SCAN {table.upper()}" in detail.upper() for detail in details)
        if safe_search and not unsafe_scan:
            result[key] = {"status": "VERIFIED", "table": table, "plan": details}
        else:
            result[key] = {
                "status": "NOT_VERIFIABLE",
                "table": table,
                "plan": details,
                "reason": "Refused high-volume metric: SQLite did not verify an indexed bounded access path.",
            }
    return {key: result[key] for key in sorted(result)}


def _query_is_verified(query_safety: Mapping[str, Mapping[str, Any]], key: str) -> bool:
    return query_safety.get(key, {}).get("status") == "VERIFIED"


def _query_skip_reason(query_safety: Mapping[str, Mapping[str, Any]], key: str) -> str:
    evidence = query_safety.get(key, {})
    status = str(evidence.get("status", "NOT_VERIFIABLE"))
    reason = str(evidence.get("reason", "An indexed bounded access path was not verified."))
    return f"{status}: {reason}"


def _load_scan_runs(
    connection: sqlite3.Connection,
    tables: Mapping[str, set[str]],
    window: AuditWindow,
    limits: QueryLimits,
) -> list[dict[str, Any]]:
    columns = tables["scan_runs"]
    selected = _selected_columns(columns, "scan_runs")
    sql = (
        f"SELECT {selected} FROM scan_runs WHERE timestamp >= ? AND timestamp < ? "
        "ORDER BY timestamp ASC, run_id ASC LIMIT ?"
    )
    return _query_rows(connection, sql, (_sqlite_utc_lower_bound(window.start), _sqlite_utc_upper_bound(window.end)), "scan_runs", limits)


def _load_rows_for_run_ids(
    connection: sqlite3.Connection,
    tables: Mapping[str, set[str]],
    table: str,
    run_ids: Sequence[str],
    limits: QueryLimits,
) -> list[dict[str, Any]]:
    if table not in tables or "run_id" not in tables[table] or not run_ids:
        return []
    rows: list[dict[str, Any]] = []
    selected = _selected_columns(tables[table], table)
    order_column = "id" if "id" in tables[table] else "run_id"
    for values in _chunks(sorted(set(run_ids)), 500):
        remaining = limits.max_rows_per_source - len(rows)
        if remaining <= 0:
            limits.truncated_sources.add(table)
            break
        placeholders = ",".join("?" for _ in values)
        sql = (
            f"SELECT {selected} FROM {_identifier(table)} WHERE run_id IN ({placeholders}) "
            f"ORDER BY run_id ASC, {_identifier(order_column)} ASC LIMIT ?"
        )
        rows.extend(_query_rows(connection, sql, values, table, limits, limit=remaining))
    return rows


def _load_lifecycle_events(
    connection: sqlite3.Connection,
    tables: Mapping[str, set[str]],
    symbols: Sequence[str],
    window: AuditWindow,
    limits: QueryLimits,
) -> list[dict[str, Any]]:
    table = "setup_lifecycle_events"
    required = {"symbol", "timestamp", "lifecycle_id"}
    if table not in tables or not required <= tables[table] or not symbols:
        return []
    rows: list[dict[str, Any]] = []
    selected = _selected_columns(tables[table], table)
    order_column = "event_id" if "event_id" in tables[table] else "lifecycle_id"
    for symbol in sorted(set(symbols)):
        remaining = limits.max_rows_per_source - len(rows)
        if remaining <= 0:
            limits.truncated_sources.add(table)
            break
        sql = (
            f"SELECT {selected} FROM {table} WHERE symbol = ? AND timestamp >= ? AND timestamp < ? "
            f"ORDER BY timestamp ASC, {_identifier(order_column)} ASC LIMIT ?"
        )
        rows.extend(
            _query_rows(
                connection,
                sql,
                (symbol, _sqlite_utc_lower_bound(window.start), _sqlite_utc_upper_bound(window.end)),
                table,
                limits,
                limit=remaining,
            )
        )
    return rows


def _load_lifecycle_records(
    connection: sqlite3.Connection,
    tables: Mapping[str, set[str]],
    symbols: Sequence[str],
    lifecycle_ids: Sequence[str],
    window: AuditWindow,
    limits: QueryLimits,
) -> list[dict[str, Any]]:
    table = "setup_lifecycle_records"
    if table not in tables or "lifecycle_id" not in tables[table]:
        return []
    selected = _selected_columns(tables[table], table)
    rows_by_id: dict[str, dict[str, Any]] = {}
    for values in _chunks(sorted(set(lifecycle_ids)), 500):
        if len(rows_by_id) >= limits.max_rows_per_source:
            limits.truncated_sources.add(table)
            break
        placeholders = ",".join("?" for _ in values)
        sql = f"SELECT {selected} FROM {table} WHERE lifecycle_id IN ({placeholders}) ORDER BY lifecycle_id ASC LIMIT ?"
        for row in _query_rows(connection, sql, values, table, limits, limit=limits.max_rows_per_source - len(rows_by_id)):
            rows_by_id[str(row["lifecycle_id"])] = row
    if {"symbol", "first_seen_at", "last_seen_at"} <= tables[table]:
        for symbol in sorted(set(symbols)):
            if len(rows_by_id) >= limits.max_rows_per_source:
                limits.truncated_sources.add(table)
                break
            sql = (
                f"SELECT {selected} FROM {table} WHERE symbol = ? "
                "AND last_seen_at >= ? AND first_seen_at < ? ORDER BY lifecycle_id ASC LIMIT ?"
            )
            for row in _query_rows(
                connection,
                sql,
                (symbol, _sqlite_utc_lower_bound(window.start), _sqlite_utc_upper_bound(window.end)),
                table,
                limits,
                limit=limits.max_rows_per_source - len(rows_by_id),
            ):
                rows_by_id[str(row["lifecycle_id"])] = row
    return [rows_by_id[key] for key in sorted(rows_by_id)]


def _load_rows_for_lifecycle_ids(
    connection: sqlite3.Connection,
    tables: Mapping[str, set[str]],
    table: str,
    lifecycle_ids: Sequence[str],
    limits: QueryLimits,
) -> list[dict[str, Any]]:
    if table not in tables or "lifecycle_id" not in tables[table] or not lifecycle_ids:
        return []
    selected = _selected_columns(tables[table], table)
    rows: list[dict[str, Any]] = []
    order_column = "id" if "id" in tables[table] else "lifecycle_id"
    for values in _chunks(sorted(set(lifecycle_ids)), 500):
        remaining = limits.max_rows_per_source - len(rows)
        if remaining <= 0:
            limits.truncated_sources.add(table)
            break
        placeholders = ",".join("?" for _ in values)
        sql = (
            f"SELECT {selected} FROM {_identifier(table)} WHERE lifecycle_id IN ({placeholders}) "
            f"ORDER BY lifecycle_id ASC, {_identifier(order_column)} ASC LIMIT ?"
        )
        rows.extend(_query_rows(connection, sql, values, table, limits, limit=remaining))
    return rows


def _load_telegram_rows(
    connection: sqlite3.Connection,
    tables: Mapping[str, set[str]],
    run_ids: Sequence[str],
    window: AuditWindow,
    limits: QueryLimits,
) -> tuple[list[dict[str, Any]], str]:
    table = "telegram_alert_attempts"
    if table not in tables:
        return [], "NOT_RECORDED: telegram_alert_attempts table is absent."
    if "scan_run_id" not in tables[table]:
        return [], "NOT_VERIFIABLE: no indexed scan_run_id link is available for a bounded audit query."
    rows: list[dict[str, Any]] = []
    selected = _selected_columns(tables[table], table)
    order_column = "id" if "id" in tables[table] else "scan_run_id"
    for values in _chunks(sorted(set(run_ids)), 500):
        remaining = limits.max_rows_per_source - len(rows)
        if remaining <= 0:
            limits.truncated_sources.add(table)
            break
        placeholders = ",".join("?" for _ in values)
        sql = (
            f"SELECT {selected} FROM {table} WHERE scan_run_id IN ({placeholders}) "
            f"ORDER BY scan_run_id ASC, {_identifier(order_column)} ASC LIMIT ?"
        )
        rows.extend(_query_rows(connection, sql, values, table, limits, limit=remaining))
    timestamp_columns = [field for field in ("attempted_at", "sent_at") if field in tables[table]]
    if not timestamp_columns:
        return [], "NOT_RECORDED: Telegram attempts lack attempted_at and sent_at timestamps."
    filtered = [row for row in rows if _row_has_timestamp_in_window(row, timestamp_columns, window)]
    return filtered, "OBSERVED_BY_SCAN_RUN_ID_AND_EXPLICIT_ATTEMPT_OR_SEND_TIMESTAMP"


def _query_rows(
    connection: sqlite3.Connection,
    sql: str,
    parameters: Sequence[Any],
    source: str,
    limits: QueryLimits,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Fetch compact evidence in bounded batches without retaining raw payloads."""

    requested = limits.max_rows_per_source if limit is None else limit
    already_observed = limits.observed_rows_by_source.get(source, 0)
    remaining = min(requested, limits.max_rows_per_source - already_observed)
    if remaining <= 0:
        limits.truncated_sources.add(source)
        return []
    cursor = connection.execute(sql, (*parameters, remaining + 1))
    rows: list[dict[str, Any]] = []
    while len(rows) <= remaining:
        batch = cursor.fetchmany(min(QUERY_BATCH_SIZE, remaining + 1 - len(rows)))
        if not batch:
            break
        for row in batch:
            if len(rows) >= remaining:
                limits.truncated_sources.add(source)
                limits.observed_rows_by_source[source] = already_observed + len(rows)
                return rows
            rows.append(_compact_query_row(dict(row), source, limits))
    limits.observed_rows_by_source[source] = already_observed + len(rows)
    return rows


def _compact_query_row(row: dict[str, Any], source: str, limits: QueryLimits) -> dict[str, Any]:
    for field in OPTIONAL_JSON_COLUMNS:
        if row.pop(f"{field}_truncated_for_audit", 0):
            limits.optional_json_unavailable_sources.add(source)
            limits.malformed[f"{source}.{field}_optional_json_oversized"] += 1
    if source == "scan_runs":
        _compact_scan_run_optional_json(row, limits)
    if source == "symbol_results" and "raw_result_json" in row:
        raw = _json_mapping(row.get("raw_result_json"), "symbol_results.raw_result_json", limits.malformed)
        compact: dict[str, Any] = {}
        for field in RAW_RESULT_COMPACT_FIELDS:
            value = raw.get(field)
            if field == "lifecycle_state" and isinstance(value, Mapping):
                compact[field] = {
                    key: value[key]
                    for key in (
                        "failed_gate", "final_failed_gate", "opportunity_score", "technical_score",
                        "readiness_score", "quality_grade", "final_quality_grade", "candidate_quality_grade",
                    )
                    if key in value
                }
            elif field == "strategy_diagnostics" and isinstance(value, Mapping):
                compact[field] = {
                    mode: {"structure_layer_analysis": diagnostics["structure_layer_analysis"]}
                    for mode in ("challenge", "swing", "scalp")
                    if isinstance((diagnostics := value.get(mode)), Mapping)
                    and "structure_layer_analysis" in diagnostics
                }
            elif field in raw:
                compact[field] = value
        row["raw_result_json"] = json.dumps(compact, sort_keys=True, separators=(",", ":"))
    return row


def _compact_scan_run_optional_json(row: dict[str, Any], limits: QueryLimits) -> None:
    if "runtime_stats_json" in row:
        payload = _json_mapping(row.get("runtime_stats_json"), "scan_runs.runtime_stats_json", limits.malformed)
        errors = payload.get("errors")
        compact_errors = dict(errors) if isinstance(errors, Mapping) else {}
        compact_payload: dict[str, Any] = {"errors": compact_errors}
        raw_memory = payload.get("process_memory")
        if isinstance(raw_memory, Mapping):
            compact_payload["process_memory"] = {
                key: raw_memory[key]
                for key in PROCESS_MEMORY_COMPACT_FIELDS
                if key in raw_memory
            }
        elif "process_memory" in payload:
            compact_payload["process_memory"] = "INVALID_TYPE"
        row["runtime_stats_json"] = json.dumps(compact_payload, sort_keys=True, separators=(",", ":"))
    if "timeframes_json" in row:
        payload = _json_mapping(row.get("timeframes_json"), "scan_runs.timeframes_json", limits.malformed)
        compact_timeframes = {
            str(key): value
            for key, value in payload.items()
            if _status_key(key) in TIMEFRAME_COMPACT_FIELDS or (_text(value) or "").lower() == "2h"
        }
        row["timeframes_json"] = json.dumps(compact_timeframes, sort_keys=True, separators=(",", ":"))
    if "symbols_json" in row:
        row["symbols_json"] = _compact_symbols_json(row.get("symbols_json"), limits.malformed)


def _compact_symbols_json(value: Any, malformed: Counter[str]) -> str:
    if value is None or value == "":
        return "[]"
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except (TypeError, json.JSONDecodeError):
        malformed["scan_runs.symbols_json_malformed_json"] += 1
        return "[]"
    if not isinstance(parsed, list):
        malformed["scan_runs.symbols_json_not_array"] += 1
        return "[]"
    symbols = sorted(
        {
            text
            for item in parsed
            for text in (
                _text(item) if isinstance(item, str) else _text(item.get("symbol")) if isinstance(item, Mapping) else None,
            )
            if text
        }
    )
    return json.dumps(symbols, separators=(",", ":"))


def _data_coverage(
    *,
    tables: Mapping[str, set[str]],
    scan_rows: Sequence[Mapping[str, Any]],
    symbol_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    lifecycle_events: Sequence[Mapping[str, Any]],
    lifecycle_records: Sequence[Mapping[str, Any]],
    outcome_rows: Sequence[Mapping[str, Any]],
    telegram_rows: Sequence[Mapping[str, Any]],
    limits: QueryLimits,
    malformed: Counter[str],
    window: AuditWindow,
    query_safety: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    sources = {
        "scan_runs": scan_rows,
        "symbol_results": symbol_rows,
        "setup_candidates": candidate_rows,
        "setup_lifecycle_events": lifecycle_events,
        "setup_lifecycle_records": lifecycle_records,
        "setup_lifecycle_outcome_progress": outcome_rows,
        "telegram_alert_attempts": telegram_rows,
    }
    relevant_tables = tuple(sources)
    missing_tables = sorted(table for table in relevant_tables if table not in tables)
    missing_fields = {
        "scan_runs": sorted({"run_id", "timestamp", "symbols_requested", "symbols_completed", "timeframes_json"} - tables.get("scan_runs", set())),
        "symbol_results": sorted({"run_id", "symbol", "status", "display_bucket", "failed_gate"} - tables.get("symbol_results", set())),
        "setup_candidates": sorted({"run_id", "symbol", "mode", "direction", "failed_gate"} - tables.get("setup_candidates", set())),
        "setup_lifecycle_events": sorted({"lifecycle_id", "timestamp", "symbol", "from_state", "to_state"} - tables.get("setup_lifecycle_events", set())),
        "setup_lifecycle_records": sorted({"lifecycle_id", "current_state", "first_seen_at", "last_seen_at"} - tables.get("setup_lifecycle_records", set())),
        "setup_lifecycle_outcome_progress": sorted({"lifecycle_id", "terminal_outcome", "outcome_at"} - tables.get("setup_lifecycle_outcome_progress", set())),
        "telegram_alert_attempts": sorted({"signal_id", "attempted_at", "telegram_status"} - tables.get("telegram_alert_attempts", set())),
    }
    timestamp_values: list[datetime] = []
    for rows, field in ((scan_rows, "timestamp"), (lifecycle_events, "timestamp")):
        for row in rows:
            parsed = _row_timestamp(row, field)
            if parsed is None:
                malformed[f"{field}_unparseable"] += 1
            else:
                timestamp_values.append(parsed)
    for row in scan_rows:
        _json_mapping(row.get("runtime_stats_json"), "scan_runs.runtime_stats_json", malformed)
        _json_mapping(row.get("timeframes_json"), "scan_runs.timeframes_json", malformed)
    null_rates = {
        source: _null_rates(rows, _critical_fields_for_source(source))
        for source, rows in sorted(sources.items())
    }
    return {
        "tables_or_sources_used": [source for source in sorted(sources) if source in tables],
        "missing_tables": missing_tables,
        "missing_fields": missing_fields,
        "earliest_relevant_timestamp_utc": _utc_text(min(timestamp_values)) if timestamp_values else "NOT_RECORDED",
        "latest_relevant_timestamp_utc": _utc_text(max(timestamp_values)) if timestamp_values else "NOT_RECORDED",
        "malformed_or_unparseable_records": dict(sorted(malformed.items())),
        "critical_field_null_rates": null_rates,
        "timestamp_timezone_assumptions": (
            "Timestamp filtering uses indexed ISO-8601 text comparisons and then strict Python UTC parsing. "
            "Rows with timestamps that cannot be parsed with an explicit offset are excluded from timestamp-scoped metrics."
        ),
        "setup_identity_definition": {
            "stable_lifecycle_setup": "setup_lifecycle_records.lifecycle_id when present",
            "evaluation_identity": "scan_runs.run_id + symbol_results.symbol; this is an evaluation, not a stable setup",
            "candidate_identity": "setup_candidates.id is a row event; no cross-run candidate setup identity is persisted",
        },
        "lifecycle_event_identity_definition": "setup_lifecycle_events.event_id when present, otherwise lifecycle_id + timestamp + to_state",
        "observation_method": "All reported database metrics are direct observations except explicitly labelled NOT_VERIFIABLE or inferred_from_current_record.",
        "query_performance_safeguards": {
            "query_plan_index_verification": {key: query_safety[key] for key in sorted(query_safety)},
            "explicit_selected_column_allowlists": {
                source: sorted(SOURCE_COLUMN_ALLOWLIST[source]) for source in sorted(SOURCE_COLUMN_ALLOWLIST)
            },
            "high_volume_fetch_batch_size": QUERY_BATCH_SIZE,
            "max_rows_per_source": limits.max_rows_per_source,
            "truncated_sources": sorted(limits.truncated_sources),
            "optional_json_evidence_unavailable_sources": sorted(limits.optional_json_unavailable_sources),
            "warning": (
                "Row-limit truncation is incomplete for dependent conclusions. Oversized optional JSON is not parsed; "
                "only metrics requiring that JSON are NOT_VERIFIABLE. A NOT_VERIFIABLE plan is skipped rather than an unbounded scan."
            ),
        },
        "window_boundary_policy": {
            "start_inclusive": True,
            "end_exclusive": True,
            "effective_window_start_utc": _utc_text(window.start),
            "effective_window_end_utc": _utc_text(window.end),
        },
    }


def _scan_health(
    scan_rows: Sequence[Mapping[str, Any]],
    symbol_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    window: AuditWindow,
    interval_sec: int,
    malformed: Counter[str],
) -> dict[str, Any]:
    timestamps = [timestamp for row in scan_rows if (timestamp := _row_timestamp(row, "timestamp")) is not None]
    expected_cycles = (window.seconds + interval_sec - 1) // interval_sec
    complete = 0
    partial = 0
    zero_completed = 0
    completion_not_recorded = 0
    error_counts: Counter[str] = Counter()
    runtime_payloads: list[Mapping[str, Any]] = []
    for row in scan_rows:
        requested = _integer_or_none(row.get("symbols_requested"))
        completed = _integer_or_none(row.get("symbols_completed"))
        if requested is None or completed is None:
            completion_not_recorded += 1
        elif requested > 0 and completed >= requested:
            complete += 1
        elif requested > 0 and completed == 0:
            zero_completed += 1
        elif requested > 0:
            partial += 1
        runtime_payloads.append(
            _count_runtime_errors(row.get("runtime_stats_json"), error_counts, malformed)
        )
    gaps = _scan_gaps(timestamps, window, interval_sec)
    evaluated_symbols = {str(row["symbol"]) for row in symbol_rows if _text(row.get("symbol"))}
    total_evaluations = len(symbol_rows)
    candidate_events = len(candidate_rows)
    return {
        "expected_scan_cycles": expected_cycles,
        "observed_scan_cycles": len(scan_rows),
        "cycle_coverage_percentage": _percentage(len(scan_rows), expected_cycles),
        "complete_cycles_from_recorded_completion_counts": complete,
        "partial_cycles_from_recorded_completion_counts": partial,
        "zero_completed_cycles_from_recorded_completion_counts": zero_completed,
        "successful_cycles": "NOT_RECORDED: no explicit success flag is persisted.",
        "failed_cycles": "NOT_RECORDED: no explicit failure flag is persisted.",
        "cycles_without_completion_counts": completion_not_recorded,
        "distinct_symbols_evaluated": len(evaluated_symbols),
        "total_symbol_evaluations": total_evaluations,
        "candidate_row_events": candidate_events,
        "unique_candidate_setup_count": "NOT_VERIFIABLE: setup_candidates has no stable cross-run setup identity.",
        "scan_gaps": gaps,
        "longest_scan_gap_seconds": max((gap["gap_seconds"] for gap in gaps), default=0),
        "recorded_errors_by_type": dict(sorted(error_counts.items())),
        "process_memory": _process_memory_health(runtime_payloads, malformed),
        "timestamp_count": len(timestamps),
        "timestamp_parse_failures": malformed.get("timestamp_unparseable", 0),
        "interval_assumption": f"Expected cycles assume a cycle at window start and every {interval_sec} seconds thereafter.",
    }


def _funnel(
    symbol_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    lifecycle_events: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    evaluated_ids = {_evaluation_id(row) for row in symbol_rows if _evaluation_id(row) is not None}
    lifecycle_by_stage: dict[str, list[Mapping[str, Any]]] = {}
    for stage, states in FUNNEL_STAGES:
        lifecycle_by_stage[stage] = [row for row in lifecycle_events if _state_text(row.get("to_state")) in states]
    stages: list[dict[str, Any]] = [
        {
            "stage": "EVALUATED",
            "persisted_states": ["symbol_results row"],
            "event_count": len(symbol_rows),
            "unique_setup_count": "NOT_VERIFIABLE",
            "unique_evaluation_count": len(evaluated_ids),
            "percentage_of_relevant_denominator": 100.0 if symbol_rows else "NOT_RECORDED",
            "conversion_from_preceding_supported_stage": "NOT_APPLICABLE",
            "identity_definition": "run_id + symbol (evaluation identity, not stable setup identity)",
        },
        {
            "stage": "CANDIDATE",
            "persisted_states": ["setup_candidates row"],
            "event_count": len(candidate_rows),
            "unique_setup_count": "NOT_VERIFIABLE",
            "unique_evaluation_count": len({_candidate_event_id(row) for row in candidate_rows}),
            "percentage_of_relevant_denominator": _percentage(len(candidate_rows), len(symbol_rows)),
            "conversion_from_preceding_supported_stage": "NOT_VERIFIABLE: candidates have no stable key shared with evaluations.",
            "identity_definition": "setup_candidates.id is a candidate row event; it is not a stable cross-run setup identity.",
        },
    ]
    prior_ids: set[str] | None = None
    for stage, states in FUNNEL_STAGES:
        rows = lifecycle_by_stage[stage]
        lifecycle_ids = {str(row["lifecycle_id"]) for row in rows if _text(row.get("lifecycle_id"))}
        conversion: float | str
        if prior_ids is None:
            conversion = "NOT_VERIFIABLE: no stable lifecycle identity is shared with the persisted candidate rows."
        else:
            conversion = _percentage(len(lifecycle_ids & prior_ids), len(prior_ids))
        stages.append(
            {
                "stage": stage,
                "persisted_states": sorted(states),
                "event_count": len(rows),
                "unique_setup_count": len(lifecycle_ids),
                "percentage_of_relevant_denominator": _percentage(len(lifecycle_ids), len({str(row["lifecycle_id"]) for row in lifecycle_events if _text(row.get("lifecycle_id"))})),
                "conversion_from_preceding_supported_stage": conversion,
                "identity_definition": "setup_lifecycle_events.lifecycle_id",
            }
        )
        prior_ids = lifecycle_ids
    return {
        "stages": stages,
        "repeated_watch_loop_handling": "Event counts retain repeated observations; unique lifecycle counts deduplicate only by lifecycle_id.",
        "stable_setup_identity_limit": "Candidate and symbol-result rows do not persist a stable cross-run setup identity, so they are never presented as unique setups.",
    }


def _gate_observations(rows: Sequence[Mapping[str, Any]], scan_time_by_id: Mapping[str, datetime], malformed: Counter[str]) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []
    for row in rows:
        raw = _json_mapping(row.get("raw_result_json"), "symbol_results.raw_result_json", malformed)
        gates = _gate_values(row, raw)
        if not gates:
            continue
        observations.append(
            {
                "identity": _evaluation_id(row),
                "run_id": _text(row.get("run_id")),
                "symbol": _text(row.get("symbol")),
                "timestamp": scan_time_by_id.get(str(row.get("run_id"))),
                "gates": tuple(sorted(gates)),
                "score": _score_value(row, raw),
                "grade": _grade_value(row, raw),
                "direction": _field_text(row, raw, ("direction", "side", "bias")),
                "mode": _field_text(row, raw, ("mode", "strategy_mode")),
                "raw": raw,
            }
        )
    return observations


def _gate_failures(observations: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    unique: dict[str, set[str]] = defaultdict(set)
    exclusive: Counter[str] = Counter()
    combinations: Counter[str] = Counter()
    grades: Counter[str] = Counter()
    scores: Counter[str] = Counter()
    high_score_near_misses = 0
    high_score_denominator = 0
    b_plus_near_misses = 0
    for observation in observations:
        gates = tuple(observation["gates"])
        combinations[" + ".join(gates)] += 1
        identity = str(observation.get("identity") or "NOT_VERIFIABLE")
        for gate in gates:
            counts[gate] += 1
            if identity != "NOT_VERIFIABLE":
                unique[gate].add(identity)
        if len(gates) == 1:
            exclusive[gates[0]] += 1
        grade = _text(observation.get("grade"))
        if grade:
            grades[grade] += 1
            if _grade_at_least_b_plus(grade):
                b_plus_near_misses += 1
        score = _decimal_or_none(observation.get("score"))
        if score is not None:
            scores[_decimal_text(score)] += 1
        threshold = _decimal_or_none(_mapping_value(observation.get("raw"), "min_score_for_idea"))
        if score is not None and threshold is not None:
            high_score_denominator += 1
            if score >= threshold:
                high_score_near_misses += 1
    return {
        "evidence_source": "symbol_results.failed_gate/final_failed_gate and explicitly persisted gates_failed values in raw_result_json",
        "denominator_failure_observations": len(observations),
        "failure_occurrences_by_normalized_gate": dict(sorted(counts.items())),
        "unique_evaluation_identities_affected_by_gate": {gate: len(unique[gate]) for gate in sorted(counts)},
        "exclusive_failures_by_gate": dict(sorted(exclusive.items())),
        "overlapping_failure_observations": sum(1 for observation in observations if len(observation["gates"]) > 1),
        "most_common_gate_combinations": _top_counts(combinations),
        "score_distribution_of_rejected_or_stalled_observations": dict(sorted(scores.items(), key=lambda item: _score_sort_key(item[0]))),
        "grade_distribution_of_rejected_or_stalled_observations": dict(sorted(grades.items())),
        "high_score_near_misses": (
            {"count": high_score_near_misses, "denominator": high_score_denominator}
            if high_score_denominator
            else "NOT_RECORDED: no observation persisted both score and its applicable min_score_for_idea."
        ),
        "b_plus_or_better_near_misses": {"count": b_plus_near_misses, "denominator": len(observations)},
        "exclusive_blocker_caution": "A failure is exclusive only when the same persisted observation records exactly one normalized gate. Notes and rejection text are not promoted to blockers.",
    }


def _target_inside_chop_review(
    observations: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    lifecycle_events: Sequence[Mapping[str, Any]],
    lifecycle_records: Sequence[Mapping[str, Any]],
    outcome_rows: Sequence[Mapping[str, Any]],
    max_detail_records: int,
) -> dict[str, Any]:
    target = [dict(observation, source="symbol_results") for observation in observations if "target_inside_chop" in observation["gates"]]
    candidate_target_rows = [
        row
        for row in candidate_rows
        if "target_inside_chop" in {
            _gate_key(row.get("failed_gate")),
            _gate_key(row.get("final_failed_gate")),
            _gate_key(row.get("target_failure")),
        }
    ]
    for row in candidate_target_rows:
        target.append(
            {
                "identity": None,
                "run_id": _text(row.get("run_id")),
                "symbol": _text(row.get("symbol")),
                "timestamp": None,
                "gates": ("target_inside_chop",),
                "score": _score_value(row, {}),
                "grade": _grade_value(row, {}),
                "direction": _text(row.get("direction")),
                "mode": _text(row.get("mode")),
                "raw": {},
                "source": "setup_candidates",
                "exclusive_eligible": False,
            }
        )
    candidate_context = _candidate_context(candidate_rows)
    records_by_key = _lifecycle_record_context(lifecycle_records)
    events_by_lifecycle: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in lifecycle_events:
        if _text(event.get("lifecycle_id")):
            events_by_lifecycle[str(event["lifecycle_id"])].append(event)
    outcomes_by_lifecycle: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for outcome in outcome_rows:
        if _text(outcome.get("lifecycle_id")):
            outcomes_by_lifecycle[str(outcome["lifecycle_id"])].append(outcome)
    details: list[dict[str, Any]] = []
    unique = {str(observation.get("identity")) for observation in target if observation.get("identity")}
    exclusive = sum(1 for observation in target if observation.get("source") == "symbol_results" and len(observation["gates"]) == 1)
    for observation in sorted(target, key=lambda item: (str(item.get("run_id") or ""), str(item.get("symbol") or ""))):
        if len(details) >= max_detail_records:
            break
        context = candidate_context.get((observation.get("run_id"), observation.get("symbol")), {})
        mode = context.get("mode") or observation.get("mode") or "NOT_RECORDED"
        direction = context.get("direction") or observation.get("direction") or "NOT_RECORDED"
        record = records_by_key.get((observation.get("symbol"), mode, direction))
        lifecycle_id = _text(record.get("lifecycle_id")) if record else None
        matching_events = events_by_lifecycle.get(lifecycle_id or "", [])
        matching_outcomes = outcomes_by_lifecycle.get(lifecycle_id or "", [])
        observation_time = observation.get("timestamp")
        if record is None or not isinstance(observation_time, datetime):
            lifecycle_link_method = "NOT_VERIFIABLE: no stable lifecycle_id is persisted on the gate observation."
            state_at_observation = "NOT_VERIFIABLE"
            subsequent_states = ["NOT_VERIFIABLE"]
            later_outcomes = ["NOT_VERIFIABLE"]
        else:
            lifecycle_link_method = "INFERRED_BY_SYMBOL_MODE_DIRECTION: lifecycle_id is not persisted on the gate observation."
            ordered_events = sorted(
                matching_events,
                key=lambda event: (_row_timestamp(event, "timestamp") or datetime.min.replace(tzinfo=UTC), str(event.get("event_id") or "")),
            )
            prior_states = [
                _state_text(event.get("to_state"))
                for event in ordered_events
                if (event_time := _row_timestamp(event, "timestamp")) is not None
                and event_time <= observation_time
                and _state_text(event.get("to_state"))
            ]
            state_at_observation = prior_states[-1] if prior_states else "NOT_RECORDED"
            subsequent_states = [
                _state_text(event.get("to_state"))
                for event in ordered_events
                if (event_time := _row_timestamp(event, "timestamp")) is not None
                and event_time > observation_time
                and _state_text(event.get("to_state"))
            ] or ["NOT_RECORDED"]
            later_outcomes = sorted(
                {
                    _text(outcome.get("terminal_outcome"))
                    for outcome in matching_outcomes
                    if _text(outcome.get("terminal_outcome"))
                    and (outcome_time := _row_timestamp(outcome, "outcome_at")) is not None
                    and outcome_time > observation_time
                }
            ) or ["NOT_RECORDED"]
        details.append(
            {
                "evaluation_identity": observation.get("identity") or "NOT_VERIFIABLE",
                "evidence_source": observation.get("source") or "NOT_RECORDED",
                "symbol": observation.get("symbol") or "NOT_RECORDED",
                "mode": mode,
                "direction": direction,
                "score": _display_or_not_recorded(observation.get("score")),
                "grade": _display_or_not_recorded(observation.get("grade")),
                "exclusive_recorded_gate": len(observation["gates"]) == 1,
                "overlapping_recorded_gates": list(observation["gates"]),
                "lifecycle_link_method": lifecycle_link_method,
                "lifecycle_state_at_first_observation": state_at_observation,
                "subsequent_observed_lifecycle_progression": subsequent_states,
                "later_recorded_outcomes": later_outcomes,
            }
        )
    return {
        "occurrences": len(target),
        "occurrences_by_source": {
            "symbol_results": sum(observation.get("source") == "symbol_results" for observation in target),
            "setup_candidates": sum(observation.get("source") == "setup_candidates" for observation in target),
        },
        "unique_affected_evaluation_identities": len(unique),
        "unique_stable_setups": "NOT_VERIFIABLE: target gate observations lack a persisted lifecycle_id.",
        "exclusive_occurrences": exclusive,
        "overlapping_occurrences": sum(1 for observation in target if observation.get("source") == "symbol_results" and len(observation["gates"]) > 1),
        "candidate_exclusive_or_overlap": "NOT_VERIFIABLE unless a candidate row persists its complete blocking-gate set.",
        "cross_table_deduplication": "NOT_VERIFIABLE: symbol-result and candidate rows lack a shared stable setup identity and are reported as separate persisted occurrences.",
        "detail_limit": max_detail_records,
        "details_truncated": len(target) > len(details),
        "affected_observations": details,
        "causality_limit": "This observational report does not claim a target-inside-chop setup would have won without persisted, attributable outcome evidence.",
    }


def _lifecycle_quality(
    events: Sequence[Mapping[str, Any]],
    records: Sequence[Mapping[str, Any]],
    window: AuditWindow,
    stall_threshold_sec: int | None,
) -> dict[str, Any]:
    state_entries: Counter[str] = Counter()
    state_setups: dict[str, set[str]] = defaultdict(set)
    transitions: Counter[str] = Counter()
    valid = 0
    impossible = 0
    repeated = 0
    skipped = 0
    regression = 0
    events_by_lifecycle: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for event in events:
        lifecycle_id = _text(event.get("lifecycle_id"))
        to_state = _state_text(event.get("to_state"))
        from_state = _state_text(event.get("from_state"))
        if not lifecycle_id or not to_state:
            continue
        state_entries[to_state] += 1
        state_setups[to_state].add(lifecycle_id)
        events_by_lifecycle[lifecycle_id].append(event)
        key = f"{from_state or 'N/A'} -> {to_state}"
        transitions[key] += 1
        if from_state == to_state:
            repeated += 1
            continue
        source_state = _enum_state(from_state)
        destination_state = _enum_state(to_state)
        if source_state is None or destination_state is None:
            continue
        if destination_state in ALLOWED_TRANSITIONS.get(source_state, set()):
            valid += 1
        else:
            impossible += 1
        if from_state in ACTIVE_STATE_ORDER and to_state in ACTIVE_STATE_ORDER:
            difference = ACTIVE_STATE_ORDER[to_state] - ACTIVE_STATE_ORDER[from_state]
            if difference > 1:
                skipped += 1
            elif difference < 0:
                regression += 1
    terminal_event_ids = {
        str(event["lifecycle_id"])
        for event in events
        if _text(event.get("lifecycle_id")) and _state_text(event.get("to_state")) in TERMINAL_STATES
    }
    open_records = [
        row
        for row in records
        if _state_text(row.get("current_state")) not in TERMINAL_STATES
    ]
    stalled: list[str] = []
    if stall_threshold_sec is not None:
        cutoff = window.end.timestamp() - stall_threshold_sec
        for row in open_records:
            last_transition = _row_timestamp(row, "last_transition_at")
            if last_transition is not None and last_transition.timestamp() <= cutoff:
                lifecycle_id = _text(row.get("lifecycle_id"))
                if lifecycle_id:
                    stalled.append(lifecycle_id)
    return {
        "state_entry_counts": dict(sorted(state_entries.items())),
        "distinct_setups_reaching_each_state": {state: len(state_setups[state]) for state in sorted(state_setups)},
        "valid_observed_transitions": valid,
        "impossible_transitions": impossible,
        "transition_counts": dict(sorted(transitions.items())),
        "repeated_states": repeated,
        "skipped_progression_states_observed": skipped,
        "regressions_to_earlier_active_stage": regression,
        "open_unresolved_setups": len(open_records),
        "invalidations": state_entries.get(SetupLifecycleState.INVALIDATED.value, 0),
        "expirations": state_entries.get(SetupLifecycleState.EXPIRED.value, 0),
        "cooldown_entries": state_entries.get(SetupLifecycleState.COOLDOWN.value, 0),
        "terminal_outcomes": len(terminal_event_ids),
        "stalled_setups": (
            {"count": len(stalled), "lifecycle_ids": sorted(stalled), "threshold_seconds": stall_threshold_sec}
            if stall_threshold_sec is not None
            else "NOT_RECORDED: provide --stall-threshold-sec; this audit does not invent a stall threshold."
        ),
        "transition_validation_source": "app.lifecycle.state_machine.ALLOWED_TRANSITIONS from the current repository; this audit does not alter it.",
    }


def _outcomes(
    outcome_rows: Sequence[Mapping[str, Any]],
    lifecycle_events: Sequence[Mapping[str, Any]],
    lifecycle_records: Sequence[Mapping[str, Any]],
    window: AuditWindow,
) -> dict[str, Any]:
    milestones = {
        "TP1": "tp1_at",
        "TP2": "tp2_at",
        "TP3": "tp3_at",
        "SL": "stop_at",
        "INVALIDATED": "invalidated_at",
    }
    counts: dict[str, dict[str, int | str]] = {}
    for name, field in milestones.items():
        rows = [row for row in outcome_rows if _timestamp_in_window(_row_timestamp(row, field), window)]
        counts[name] = {
            "event_count": len(rows),
            "unique_setup_count": len({str(row["lifecycle_id"]) for row in rows if _text(row.get("lifecycle_id"))}),
        }
    terminal_by_lifecycle: dict[str, set[str]] = defaultdict(set)
    for event in lifecycle_events:
        lifecycle_id = _text(event.get("lifecycle_id"))
        state = _state_text(event.get("to_state"))
        if lifecycle_id and state in TERMINAL_STATES:
            terminal_by_lifecycle[lifecycle_id].add(state)
    for row in outcome_rows:
        lifecycle_id = _text(row.get("lifecycle_id"))
        outcome = _state_text(row.get("terminal_outcome"))
        if lifecycle_id and outcome in TERMINAL_STATES and _timestamp_in_window(_row_timestamp(row, "outcome_at"), window):
            terminal_by_lifecycle[lifecycle_id].add(outcome)
    for terminal in ("TP_HIT", "SL_HIT", "INVALIDATED", "EXPIRED"):
        ids = {key for key, states in terminal_by_lifecycle.items() if terminal in states}
        counts[terminal] = {"event_count": sum(terminal in states for states in terminal_by_lifecycle.values()), "unique_setup_count": len(ids)}
    contradictory = {key: sorted(states) for key, states in sorted(terminal_by_lifecycle.items()) if len(states) > 1}
    unresolved = [row for row in lifecycle_records if _state_text(row.get("current_state")) not in TERMINAL_STATES]
    integrity_statuses = Counter(
        _text(row.get("integrity_status")) or "NOT_RECORDED" for row in outcome_rows
    )
    return {
        "outcome_counts": counts,
        "unresolved_open_setups": {
            "event_count": "NOT_APPLICABLE",
            "unique_setup_count": len({_text(row.get("lifecycle_id")) for row in unresolved if _text(row.get("lifecycle_id"))}),
            "loss_treatment": "Open or immature setups are not counted as losses.",
        },
        "contradictory_terminal_outcomes": contradictory,
        "recorded_outcome_integrity_statuses": dict(sorted(integrity_statuses.items())),
        "geometry_limit": "The audit reports persisted integrity status only; it does not repair or recalculate trade geometry.",
    }


def _duplicate_and_cooldown(rows: Sequence[Mapping[str, Any]], tables: Mapping[str, set[str]]) -> dict[str, Any]:
    table_columns = tables.get("telegram_alert_attempts", set())
    required = {"dedupe_status", "dedupe_reason"}
    if not required & table_columns:
        return {
            "duplicate_detections": "NOT_RECORDED",
            "duplicate_suppressions": "NOT_RECORDED",
            "cooldown_suppressions": "NOT_RECORDED",
            "repeated_signal_attempts": "NOT_RECORDED",
            "suspicious_duplicate_deliveries": "NOT_RECORDED: the upserted attempt table does not preserve a delivery-attempt history per signal.",
        }
    duplicate_rows = [row for row in rows if _explicit_reason_contains(row, "duplicate")]
    cooldown_rows = [row for row in rows if _explicit_reason_contains(row, "cooldown")]
    suppressed = [row for row in duplicate_rows if _explicit_suppression(row)]
    repeated = [row for row in rows if (_integer_or_none(row.get("seen_count")) or 0) > 1]
    return {
        "duplicate_detections": {"count": len(duplicate_rows), "unique_signal_ids": _unique_text_count(duplicate_rows, "signal_id")},
        "duplicate_suppressions": {"count": len(suppressed), "unique_signal_ids": _unique_text_count(suppressed, "signal_id")},
        "cooldown_suppressions": {"count": len(cooldown_rows), "unique_signal_ids": _unique_text_count(cooldown_rows, "signal_id")},
        "repeated_signal_attempts": {"count": len(repeated), "unique_signal_ids": _unique_text_count(repeated, "signal_id")},
        "suspicious_duplicate_deliveries": "NOT_RECORDED: the persisted upsert row cannot prove multiple delivery attempts for the same signal.",
        "evidence_rule": "Counts use only explicit dedupe_status, dedupe_reason, blocked_reason, or telegram_status values; no suppression is inferred from missing alerts.",
    }


def _telegram_funnel(
    rows: Sequence[Mapping[str, Any]],
    tables: Mapping[str, set[str]],
    scope: str,
) -> dict[str, Any]:
    columns = tables.get("telegram_alert_attempts", set())
    if not columns:
        return {"availability": "NOT_RECORDED: telegram_alert_attempts table is absent."}
    attempted = [row for row in rows if _present_timestamp(row.get("attempted_at"))]
    delivered = [
        row
        for row in rows
        if _status_key(row.get("telegram_status")) == "sent" and _present_timestamp(row.get("sent_at"))
    ]
    failed = [row for row in rows if _status_key(row.get("telegram_status")) in {"failed", "error", "delivery_failed"}]
    suppressed = [row for row in rows if _explicit_suppression(row)]
    dry_run = [row for row in rows if "dry" in _status_key(row.get("telegram_status"))]
    reasons: Counter[str] = Counter()
    for row in failed:
        reason = _safe_text(row.get("last_error_message") or row.get("error_message") or "NOT_RECORDED")
        reasons[reason] += 1
    return {
        "scope": scope,
        "eligible": "NOT_RECORDED: eligibility is not a separate persisted Telegram event and is not inferred from signal creation.",
        "attempted": {"event_count": len(attempted), "unique_signal_ids": _unique_text_count(attempted, "signal_id")},
        "delivered": {"event_count": len(delivered), "unique_signal_ids": _unique_text_count(delivered, "signal_id")},
        "failed": {"event_count": len(failed), "unique_signal_ids": _unique_text_count(failed, "signal_id")},
        "suppressed": {"event_count": len(suppressed), "unique_signal_ids": _unique_text_count(suppressed, "signal_id")},
        "dry_run": {"event_count": len(dry_run), "unique_signal_ids": _unique_text_count(dry_run, "signal_id")},
        "delivery_conversion_from_attempted_percentage": _percentage(len(delivered), len(attempted)),
        "failure_reasons": dict(sorted(reasons.items())),
        "evidence_rule": "Delivered requires explicit telegram_status='sent' and a persisted sent_at timestamp; signal creation and eligibility are not treated as delivery.",
    }


def _timeframe_verification(
    scan_rows: Sequence[Mapping[str, Any]],
    malformed: Counter[str],
    repository_root: Path | str | None,
    *, symbol_rows: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    observed: dict[str, list[str]] = defaultdict(list)
    structure_observations = 0
    structure_fields = ("structure", "structure_timeframe", "structure_context_timeframe", "structure_analysis_timeframe")
    unrelated_2h_fields: set[str] = set()
    for row in scan_rows:
        payload = _json_mapping(row.get("timeframes_json"), "scan_runs.timeframes_json", malformed)
        for key, value in payload.items():
            normalized_key = _status_key(key)
            value_text = _text(value)
            if value_text:
                observed[normalized_key].append(value_text.lower())
                if value_text.lower() == "2h" and normalized_key not in structure_fields:
                    unrelated_2h_fields.add(normalized_key)
        if any(_text(payload.get(field)) for field in structure_fields):
            structure_observations += 1
    execution_statuses: Counter[str] = Counter()
    execution_records = 0
    verified_execution_records = 0
    verified_statuses = frozenset({"ANALYZED_NO_SHIFT", "ANALYZED_SHIFT_PRESENT"})
    for row in symbol_rows:
        raw = _json_mapping(row.get("raw_result_json"), "symbol_results.raw_result_json", malformed)
        diagnostics_by_mode = raw.get("strategy_diagnostics")
        if not isinstance(diagnostics_by_mode, Mapping):
            continue
        for mode in ("challenge", "swing", "scalp"):
            diagnostics = diagnostics_by_mode.get(mode)
            if not isinstance(diagnostics, Mapping):
                continue
            analysis = diagnostics.get("structure_layer_analysis")
            if not isinstance(analysis, Mapping):
                continue
            timeframe = (_text(analysis.get("timeframe")) or "").lower()
            if timeframe != "2h":
                continue
            execution_records += 1
            status = (_text(analysis.get("status")) or "NOT_RECORDED").upper()
            execution_statuses[status] += 1
            if status in verified_statuses:
                verified_execution_records += 1

    repository_evidence = _repository_timeframe_evidence(repository_root)
    definitions = {
        "2D_context": (("context", "htf", "htf_timeframe"), "2d"),
        "12H_bias": (("bias", "bias_timeframe"), "12h"),
        "M15_execution": (("execution", "execution_timeframe"), "15m"),
        "M15_confirmation": (("confirmation", "confirmation_timeframe"), "15m"),
    }
    result: dict[str, Any] = {}
    for label, (fields, expected) in definitions.items():
        values = sorted({value for field in fields for value in observed.get(field, [])})
        matching_fields = [field for field in fields if expected in observed.get(field, [])]
        field = matching_fields[0] if matching_fields else fields[0]
        result[label] = {
            "status": "ACTIVE_AND_VERIFIED" if expected in values else "NOT_VERIFIABLE",
            "persisted_field": f"scan_runs.timeframes_json.{field}",
            "persisted_values": values or ["NOT_RECORDED"],
            "expected_value": expected,
            "exact_evidence": f"scan_runs.timeframes_json.{field}" if values else "NOT_RECORDED",
        }
    structure_values = sorted({value for field in structure_fields for value in observed.get(field, [])})
    configured_2h = any(value == "2h" for value in structure_values)
    configuration_status = (
        "CONFIGURED"
        if configured_2h
        else "CONFIGURED_OTHER_TIMEFRAME"
        if structure_values
        else "NOT_RECORDED"
    )
    configuration_evidence = [
        f"scan_runs.timeframes_json.{field}"
        for field in structure_fields
        if "2h" in observed.get(field, [])
    ]
    if verified_execution_records:
        structure_status = "ACTIVE_AND_VERIFIED"
        structure_evidence = [
            "symbol_results.raw_result_json.strategy_diagnostics.<mode>.structure_layer_analysis records an analyzed 2h structure timeframe."
        ]
    elif configured_2h:
        structure_status = "NOT_VERIFIABLE"
        structure_evidence = [
            "2h structure configuration is persisted, but no retained structure-analysis execution evidence was observed.",
            *configuration_evidence,
        ]
    elif scan_rows and structure_observations == len(scan_rows):
        structure_status = "ABSENT"
        structure_evidence = [
            f"scan_runs.timeframes_json.{field} persisted for every observed scan without value 2h"
            for field in structure_fields
            if observed.get(field)
        ]
    else:
        structure_status = "NOT_VERIFIABLE"
        structure_evidence = [
            "No structure-specific persisted timeframe field covers every observed scan.",
            *(f"Unrelated scan_runs.timeframes_json.{field}=2h is not structure evidence." for field in sorted(unrelated_2h_fields)),
            f"Repository review: {repository_evidence['source']} is related configuration, not verified structure wiring.",
        ]
    result["2H_structure"] = {
        "status": structure_status,
        "structure_specific_fields_checked": [f"scan_runs.timeframes_json.{field}" for field in structure_fields],
        "structure_specific_persisted_values": structure_values or ["NOT_RECORDED"],
        "configuration_evidence": {
            "status": configuration_status,
            "persisted_values": structure_values or ["NOT_RECORDED"],
            "exact_evidence": configuration_evidence or ["NOT_RECORDED"],
        },
        "execution_evidence": {
            "observed_structure_diagnostic_records": execution_records,
            "verified_analysis_records": verified_execution_records,
            "observed_statuses": dict(sorted(execution_statuses.items())),
            "persisted_field": "symbol_results.raw_result_json.strategy_diagnostics.<mode>.structure_layer_analysis",
        },
        "exact_evidence": structure_evidence,
    }
    return {
        "timeframes": result,
        "repository_configuration_evidence": repository_evidence,
        "interpretation_limit": "2H is verified only by retained structure-specific analysis execution evidence; configuration, arbitrary 2h values, and repository intent are not execution evidence.",
    }


def _verdict(
    *,
    window: AuditWindow,
    scan_health: Mapping[str, Any],
    funnel: Mapping[str, Any],
    gates: Mapping[str, Any],
    lifecycle: Mapping[str, Any],
    telegram: Mapping[str, Any],
    coverage: Mapping[str, Any],
    market_scarcity_evidence: Mapping[str, Any],
    minimum_meaningful_window_sec: int,
    dominant_share: Decimal,
    stall_threshold_sec: int | None,
) -> dict[str, Any]:
    del telegram
    labels: list[dict[str, Any]] = []
    duration_ready = window.seconds >= minimum_meaningful_window_sec
    safeguards = coverage["query_performance_safeguards"]
    truncated_sources = sorted(safeguards["truncated_sources"])
    query_safety = safeguards["query_plan_index_verification"]
    critical_issues: list[str] = []
    for key in ("scan_runs_timestamp", "symbol_results_run_id", "setup_candidates_run_id"):
        if query_safety.get(key, {}).get("status") != "VERIFIED":
            critical_issues.append(f"{key} is not index-verified")
    required_fields = {
        "scan_runs": {"run_id", "timestamp", "symbols_requested", "symbols_completed"},
        "symbol_results": {"run_id", "symbol", "status", "display_bucket", "failed_gate"},
        "setup_candidates": {"run_id", "symbol"},
    }
    for source, required in required_fields.items():
        missing = set(coverage["missing_fields"].get(source, []))
        if absent := required & missing:
            critical_issues.append(f"{source} missing {', '.join(sorted(absent))}")
    if any(source in truncated_sources for source in required_fields):
        critical_issues.append("critical funnel evidence was truncated")
    malformed = coverage["malformed_or_unparseable_records"]
    if any(value for key, value in malformed.items() if key.startswith("timestamp")):
        critical_issues.append("critical timestamp evidence is malformed")
    if scan_health["complete_cycles_from_recorded_completion_counts"] != scan_health["observed_scan_cycles"]:
        critical_issues.append("not every observed scan has a recorded complete completion count")
    if scan_health["observed_scan_cycles"] != scan_health["expected_scan_cycles"]:
        critical_issues.append("observed scan-cycle coverage is incomplete")

    scarcity_issues = list(market_scarcity_evidence["market_scarcity_eligibility_issues"])
    scarcity_issues.append(
        "No persisted scalar supplies a deterministic attribution from zero candidates and zero gates to genuine market scarcity; no threshold is invented."
    )
    data_insufficient = (
        not duration_ready
        or bool(truncated_sources)
        or scan_health["observed_scan_cycles"] == 0
        or bool(critical_issues)
        or bool(scarcity_issues)
    )
    if data_insufficient:
        labels.append(
            {
                "label": "DATA_INSUFFICIENT",
                "confidence": "PROVISIONAL",
                "evidence": {
                    "numerator": scan_health["observed_scan_cycles"],
                    "denominator": scan_health["expected_scan_cycles"],
                    "window_seconds": window.seconds,
                    "minimum_meaningful_window_seconds": minimum_meaningful_window_sec,
                    "critical_data_availability_issues": critical_issues,
                    "market_scarcity_eligibility_issues": scarcity_issues,
                    "truncated_sources": truncated_sources,
                },
                "configured_threshold": "NOT_APPLICABLE: no automatic market-scarcity threshold is configured.",
                "limitations": "MARKET_SCARCITY is deliberately not emitted automatically. Strong diagnoses require complete, bounded, directly persisted evidence.",
            }
        )

    exclusive = gates["exclusive_failures_by_gate"]
    total = gates["denominator_failure_observations"]
    gate_evidence_safe = (
        query_safety.get("symbol_results_run_id", {}).get("status") == "VERIFIED"
        and "symbol_results" not in truncated_sources
        and "symbol_results" not in safeguards["optional_json_evidence_unavailable_sources"]
    )
    if duration_ready and gate_evidence_safe and exclusive and total:
        gate, count = sorted(exclusive.items(), key=lambda item: (-item[1], item[0]))[0]
        share = Decimal(count) / Decimal(total)
        if share >= dominant_share:
            labels.append(
                {
                    "label": "DOMINANT_GATE_BLOCKER",
                    "confidence": "MODERATE",
                    "evidence": {
                        "gate": gate,
                        "numerator": count,
                        "denominator": total,
                        "share": _decimal_text(share),
                        "relevant_data_availability": "index-verified, untruncated symbol-result gate evidence",
                        "window_seconds": window.seconds,
                    },
                    "configured_threshold": {"exclusive_share_minimum": _decimal_text(dominant_share)},
                    "limitations": "Exclusive means exactly one persisted gate in an evaluation; notes are not blockers.",
                }
            )
    stalled = lifecycle["stalled_setups"]
    if duration_ready and isinstance(stalled, Mapping) and lifecycle["open_unresolved_setups"] and int(stalled["count"]):
        labels.append(
            {
                "label": "LIFECYCLE_BLOCKER",
                "confidence": "PROVISIONAL",
                "evidence": {
                    "numerator": int(stalled["count"]),
                    "denominator": lifecycle["open_unresolved_setups"],
                    "relevant_data_availability": "persisted lifecycle records under the supplied stall threshold",
                    "window_seconds": window.seconds,
                },
                "configured_threshold": {"stall_threshold_seconds": stall_threshold_sec},
                "limitations": "This is reported only under the explicit supplied stall threshold.",
            }
        )
    strong_labels = {item["label"] for item in labels} - {"DATA_INSUFFICIENT"}
    confidence = "MODERATE" if duration_ready and strong_labels else "PROVISIONAL"
    return {
        "labels": labels,
        "overall_confidence": confidence,
        "audit_window_seconds": window.seconds,
        "minimum_meaningful_window_seconds": minimum_meaningful_window_sec,
        "market_scarcity_policy": (
            "DATA_INSUFFICIENT: zero persisted candidates and gates cannot by themselves prove genuine market scarcity. "
            "No automatic MARKET_SCARCITY criterion is configured."
        ),
        "delivery_blocker_policy": "NOT_RECORDED: failed Telegram attempts are measured but no threshold is invented.",
        "checkpoint_guidance": "Snapshot A remains DATA_INSUFFICIENT before 72 hours; compare Snapshot B at 72 hours and Snapshot C at seven days using identical arguments except end time and label.",
    }


def _market_scarcity_direct_evidence(
    *,
    scan_rows: Sequence[Mapping[str, Any]],
    symbol_rows: Sequence[Mapping[str, Any]],
    candidate_rows: Sequence[Mapping[str, Any]],
    tables: Mapping[str, set[str]],
    limits: QueryLimits,
    query_safety: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    available_counters = tuple(
        field for field in MARKET_SCARCITY_SCAN_COUNTER_FIELDS if field in tables.get("scan_runs", set())
    )
    counter_totals: dict[str, int] = {}
    counter_invalid_rows: dict[str, int] = {}
    for field in available_counters:
        values = [_integer_or_none(row.get(field)) for row in scan_rows]
        counter_totals[field] = sum(value for value in values if value is not None)
        invalid_count = sum(value is None for value in values)
        if invalid_count:
            counter_invalid_rows[field] = invalid_count

    completed_by_run: dict[str, int] = {}
    completion_not_recorded: list[str] = []
    for row in scan_rows:
        run_id = _text(row.get("run_id"))
        if not run_id:
            continue
        completed = _integer_or_none(row.get("symbols_completed"))
        if completed is None:
            completion_not_recorded.append(run_id)
        else:
            completed_by_run[run_id] = completed
    persisted_by_run: Counter[str] = Counter(
        run_id for row in symbol_rows if (run_id := _text(row.get("run_id")))
    )
    completion_mismatches = [
        {
            "run_id": run_id,
            "symbols_completed": completed,
            "persisted_symbol_results": persisted_by_run.get(run_id, 0),
        }
        for run_id, completed in sorted(completed_by_run.items())
        if persisted_by_run.get(run_id, 0) != completed
    ]
    unmatched_symbol_result_runs = sorted(set(persisted_by_run) - set(completed_by_run))
    disposition_fields_present = tuple(
        field for field in MARKET_SCARCITY_DISPOSITION_FIELDS if field in tables.get("symbol_results", set())
    )
    core_disposition_fields = ("status", "display_bucket")
    missing_core_disposition_fields = [
        field for field in core_disposition_fields if field not in disposition_fields_present
    ]
    unreliable_disposition_rows = [
        _evaluation_id(row) or "NOT_VERIFIABLE"
        for row in symbol_rows
        if any(_text(row.get(field)) is None for field in core_disposition_fields)
    ]
    eligibility_issues: list[str] = []
    if query_safety.get("scan_runs_timestamp", {}).get("status") != "VERIFIED":
        eligibility_issues.append("scan_runs timestamp path is not index-verified")
    if query_safety.get("symbol_results_run_id", {}).get("status") != "VERIFIED":
        eligibility_issues.append("symbol_results run_id path is not index-verified")
    if query_safety.get("setup_candidates_run_id", {}).get("status") != "VERIFIED":
        eligibility_issues.append("setup_candidates run_id path is not index-verified")
    if any(source in limits.truncated_sources for source in ("scan_runs", "symbol_results", "setup_candidates")):
        eligibility_issues.append("critical direct funnel source was row-limit truncated")
    if not scan_rows:
        eligibility_issues.append("no scan_runs rows are available in the requested window")
    if not completed_by_run or completion_not_recorded:
        eligibility_issues.append("symbols_completed is absent or non-integral for one or more observed scan runs")
    if sum(completed_by_run.values()) <= 0:
        eligibility_issues.append("completed symbol evaluations are zero")
    if not symbol_rows:
        eligibility_issues.append("symbol_results contains no persisted evaluations for observed runs")
    if completion_mismatches or unmatched_symbol_result_runs:
        eligibility_issues.append("symbols_completed does not agree with persisted symbol_results coverage")
    if missing_core_disposition_fields:
        eligibility_issues.append("required direct disposition fields are absent: " + ", ".join(missing_core_disposition_fields))
    if unreliable_disposition_rows:
        eligibility_issues.append("one or more persisted symbol evaluations lack a reliable direct status/display_bucket disposition")
    if "setup_candidates" not in tables:
        eligibility_issues.append("setup_candidates table is absent")
    if "failed_gate" not in tables.get("symbol_results", set()):
        eligibility_issues.append("symbol_results.failed_gate is absent")

    return {
        "direct_scalar_evidence_paths": {
            "scan_run_counters": "app/storage/repositories.py:_scan_run_record and _scan_summary_metadata",
            "symbol_result_dispositions": "app/storage/repositories.py:_symbol_result_record",
            "display_bucket_interpretation": "app/research/queries.py uses persisted display_bucket for valid, near_miss, no_setup, and data_issue reporting",
        },
        "scan_run_counter_fields_available": list(available_counters),
        "scan_run_counter_totals": dict(sorted(counter_totals.items())),
        "scan_run_counter_non_integral_or_null_rows": dict(sorted(counter_invalid_rows.items())),
        "completion_cross_check": {
            "recorded_symbols_completed": sum(completed_by_run.values()),
            "persisted_symbol_result_rows": len(symbol_rows),
            "runs_with_unrecorded_symbols_completed": completion_not_recorded[:DIRECT_EVIDENCE_SAMPLE_LIMIT],
            "completion_mismatch_count": len(completion_mismatches),
            "completion_mismatch_samples": completion_mismatches[:DIRECT_EVIDENCE_SAMPLE_LIMIT],
            "unmatched_symbol_result_run_ids": unmatched_symbol_result_runs[:DIRECT_EVIDENCE_SAMPLE_LIMIT],
            "coverage_agrees": bool(completed_by_run) and not completion_not_recorded and not completion_mismatches and not unmatched_symbol_result_runs,
        },
        "direct_disposition_coverage": {
            "scalar_fields_present": list(disposition_fields_present),
            "required_core_fields": list(core_disposition_fields),
            "missing_core_fields": missing_core_disposition_fields,
            "persisted_symbol_evaluations": len(symbol_rows),
            "unreliable_disposition_count": len(unreliable_disposition_rows),
            "unreliable_disposition_samples": unreliable_disposition_rows[:DIRECT_EVIDENCE_SAMPLE_LIMIT],
            "all_evaluations_have_reliable_direct_disposition": bool(symbol_rows) and not missing_core_disposition_fields and not unreliable_disposition_rows,
        },
        "candidate_and_gate_evidence": {
            "candidate_row_events": len(candidate_rows),
            "candidate_source_complete": "setup_candidates" in tables and "setup_candidates" not in limits.truncated_sources,
            "direct_failed_gate_column_present": "failed_gate" in tables.get("symbol_results", set()),
            "raw_json_is_not_required_for_this_cross_check": True,
        },
        "market_scarcity_eligibility_issues": eligibility_issues,
        "automatic_market_scarcity_criterion": (
            "NOT_VERIFIABLE: no persisted scalar establishes that zero candidates and zero gates are attributable to genuine market scarcity; no threshold is invented."
        ),
        "automatic_market_scarcity_verdict": "DATA_INSUFFICIENT",
    }


def _observed_symbols(scan_rows: Sequence[Mapping[str, Any]], symbol_rows: Sequence[Mapping[str, Any]]) -> list[str]:
    symbols = {_text(row.get("symbol")) for row in symbol_rows if _text(row.get("symbol"))}
    for row in scan_rows:
        raw = row.get("symbols_json")
        try:
            parsed = json.loads(raw) if isinstance(raw, str) else raw
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, list):
            for item in parsed:
                if isinstance(item, str) and _text(item):
                    symbols.add(item.strip())
                elif isinstance(item, Mapping) and _text(item.get("symbol")):
                    symbols.add(str(item["symbol"]).strip())
    return sorted(symbols)


def _strict_window_rows(
    rows: Sequence[Mapping[str, Any]],
    timestamp_field: str,
    window: AuditWindow,
    *,
    source: str,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for row in rows:
        timestamp = _row_timestamp(row, timestamp_field)
        if timestamp is not None and _timestamp_in_window(timestamp, window):
            result.append(dict(row))
    return result


def _scan_gaps(timestamps: Sequence[datetime], window: AuditWindow, interval: int) -> list[dict[str, Any]]:
    ordered = sorted(set(timestamps))
    boundaries = [window.start, *ordered, window.end]
    gaps: list[dict[str, Any]] = []
    for left, right in zip(boundaries, boundaries[1:]):
        seconds = int((right - left).total_seconds())
        if seconds > interval:
            gaps.append({"from_utc": _utc_text(left), "to_utc": _utc_text(right), "gap_seconds": seconds})
    return gaps


def _count_runtime_errors(value: Any, counts: Counter[str], malformed: Counter[str]) -> Mapping[str, Any]:
    payload = _json_mapping(value, "scan_runs.runtime_stats_json", malformed)
    errors = payload.get("errors")
    if isinstance(errors, Mapping):
        for key, error_count in errors.items():
            number = _integer_or_none(error_count)
            counts[_safe_text(key)] += number if number is not None else 1
    elif isinstance(errors, list):
        for item in errors:
            counts[_safe_text(item)] += 1
    return payload


def _process_memory_health(
    runtime_payloads: Sequence[Mapping[str, Any]],
    malformed: Counter[str],
) -> dict[str, Any]:
    memory_blocks: list[Mapping[str, Any]] = []
    status_counts: Counter[str] = Counter()
    starts: list[int] = []
    ends: list[int] = []
    peaks: list[int] = []
    deltas: list[int] = []
    samples_attempted = 0
    samples_failed = 0

    for runtime_payload in runtime_payloads:
        if "process_memory" not in runtime_payload:
            continue
        raw_memory = runtime_payload.get("process_memory")
        if not isinstance(raw_memory, Mapping):
            malformed["scan_runs.runtime_stats_json.process_memory_wrong_type"] += 1
            continue

        memory_blocks.append(raw_memory)
        status = _text(raw_memory.get("measurement_status")) or "N/A"
        status_counts[status] += 1
        for key, values, allow_negative in (
            ("rss_start_bytes", starts, False),
            ("rss_end_bytes", ends, False),
            ("rss_observed_peak_bytes", peaks, False),
            ("rss_delta_bytes", deltas, True),
        ):
            value = _process_memory_integer(
                raw_memory,
                key,
                malformed,
                allow_negative=allow_negative,
            )
            if value is not None:
                values.append(value)

        attempted = _process_memory_integer(
            raw_memory,
            "samples_attempted",
            malformed,
            allow_negative=False,
        )
        failed = _process_memory_integer(
            raw_memory,
            "samples_failed",
            malformed,
            allow_negative=False,
        )
        samples_attempted += attempted or 0
        samples_failed += failed or 0

    observed_cycles = len(runtime_payloads)
    verified_cycles = sum(
        count
        for status, count in status_counts.items()
        if status.strip().lower() == "verified"
    )
    unverified_cycles = sum(
        count
        for status, count in status_counts.items()
        if status.strip().lower() == "unverified"
    )
    not_available_cycles = sum(
        count
        for status, count in status_counts.items()
        if status.strip().lower() in {"n/a", "na", "not_recorded"}
    )
    if not memory_blocks:
        measurement_status = "NOT_RECORDED"
    elif verified_cycles == observed_cycles and observed_cycles > 0:
        measurement_status = "Verified"
    elif not_available_cycles == observed_cycles and observed_cycles > 0:
        measurement_status = "N/A"
    else:
        measurement_status = "Unverified"

    stability_assessment = (
        "WAITING_FOR_RUNTIME_EVIDENCE: fewer than 2 verified per-scan RSS records."
        if verified_cycles < 2
        else (
            "OBSERVATIONAL_ONLY: per-scan RSS deltas are recorded; "
            "no automatic memory-leak verdict is inferred."
        )
    )
    return {
        "measurement_status": measurement_status,
        "observed_scan_cycles": observed_cycles,
        "cycles_with_memory_block": len(memory_blocks),
        "cycles_without_memory_block": observed_cycles - len(memory_blocks),
        "memory_block_coverage_percentage": _percentage(len(memory_blocks), observed_cycles),
        "status_counts": dict(sorted(status_counts.items())),
        "verified_cycles": verified_cycles,
        "unverified_cycles": unverified_cycles,
        "not_available_cycles": not_available_cycles,
        "rss_start_min_bytes": min(starts) if starts else "NOT_RECORDED",
        "rss_start_max_bytes": max(starts) if starts else "NOT_RECORDED",
        "rss_end_min_bytes": min(ends) if ends else "NOT_RECORDED",
        "rss_end_max_bytes": max(ends) if ends else "NOT_RECORDED",
        "rss_observed_peak_max_bytes": max(peaks) if peaks else "NOT_RECORDED",
        "rss_delta_min_bytes": min(deltas) if deltas else "NOT_RECORDED",
        "rss_delta_max_bytes": max(deltas) if deltas else "NOT_RECORDED",
        "rss_delta_average_bytes": (
            round(sum(deltas) / len(deltas), 3)
            if deltas
            else "NOT_RECORDED"
        ),
        "cycles_with_positive_rss_delta": sum(1 for value in deltas if value > 0),
        "samples_attempted_total": samples_attempted,
        "samples_failed_total": samples_failed,
        "stability_assessment": stability_assessment,
    }


def _process_memory_integer(
    memory: Mapping[str, Any],
    key: str,
    malformed: Counter[str],
    *,
    allow_negative: bool,
) -> int | None:
    raw_value = memory.get(key)
    value = _integer_or_none(raw_value)
    if value is None:
        if raw_value not in (None, "", "N/A", "NOT_RECORDED"):
            malformed[f"scan_runs.runtime_stats_json.process_memory.{key}_invalid"] += 1
        return None
    if not allow_negative and value < 0:
        malformed[f"scan_runs.runtime_stats_json.process_memory.{key}_invalid"] += 1
        return None
    return value



def _candidate_context(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str | None, str | None], dict[str, str]]:
    context: dict[tuple[str | None, str | None], dict[str, str]] = {}
    for row in sorted(rows, key=lambda item: (str(item.get("run_id") or ""), str(item.get("id") or ""))):
        key = (_text(row.get("run_id")), _text(row.get("symbol")))
        context.setdefault(
            key,
            {"mode": _text(row.get("mode")) or "", "direction": _text(row.get("direction")) or ""},
        )
    return context


def _lifecycle_record_context(rows: Sequence[Mapping[str, Any]]) -> dict[tuple[str | None, str, str], Mapping[str, Any]]:
    result: dict[tuple[str | None, str, str], Mapping[str, Any]] = {}
    for row in rows:
        key = (_text(row.get("symbol")), _text(row.get("mode")) or "", _text(row.get("direction")) or "")
        result[key] = row
    return result


def _gate_values(row: Mapping[str, Any], raw: Mapping[str, Any]) -> set[str]:
    values: list[Any] = [row.get("failed_gate"), row.get("final_failed_gate"), raw.get("failed_gate"), raw.get("final_failed_gate")]
    nested = raw.get("lifecycle_state")
    if isinstance(nested, Mapping):
        values.extend((nested.get("failed_gate"), nested.get("final_failed_gate")))
    for field in ("gates_failed", "failed_gates"):
        value = raw.get(field)
        if isinstance(value, (list, tuple)):
            values.extend(value)
    return {_gate_key(value) for value in values if _gate_key(value)}


def _gate_key(value: Any) -> str | None:
    text = _text(value)
    if not text:
        return None
    normalized = _status_key(text)
    return None if normalized in NA_VALUES else normalized


def _score_value(row: Mapping[str, Any], raw: Mapping[str, Any]) -> str | None:
    for field in ("opportunity_score", "technical_score", "readiness_score", "score"):
        value = row.get(field) if field in row else raw.get(field)
        if _decimal_or_none(value) is not None:
            return _decimal_text(_decimal_or_none(value))
    lifecycle = raw.get("lifecycle_state")
    if isinstance(lifecycle, Mapping):
        for field in ("opportunity_score", "technical_score", "readiness_score"):
            if _decimal_or_none(lifecycle.get(field)) is not None:
                return _decimal_text(_decimal_or_none(lifecycle.get(field)))
    return None


def _grade_value(row: Mapping[str, Any], raw: Mapping[str, Any]) -> str | None:
    for field in ("setup_quality_score", "quality_grade", "final_quality_grade", "candidate_quality_grade", "grade"):
        value = row.get(field) if field in row else raw.get(field)
        text = _text(value)
        if text:
            return text.upper()
    lifecycle = raw.get("lifecycle_state")
    if isinstance(lifecycle, Mapping):
        for field in ("quality_grade", "final_quality_grade", "candidate_quality_grade"):
            text = _text(lifecycle.get(field))
            if text:
                return text.upper()
    return None


def _field_text(row: Mapping[str, Any], raw: Mapping[str, Any], fields: Sequence[str]) -> str | None:
    for field in fields:
        text = _text(row.get(field)) or _text(raw.get(field))
        if text:
            return text
    return None


def _repository_timeframe_evidence(repository_root: Path | str | None) -> dict[str, Any]:
    root = Path(repository_root) if repository_root is not None else Path(__file__).resolve().parents[2]
    source = root / "scripts" / "run_scan.py"
    try:
        text = source.read_text(encoding="utf-8")
    except OSError:
        return {"source": str(source), "status": "NOT_VERIFIABLE", "explicit_2h_literal": None}
    return {
        "source": str(source),
        "status": "OBSERVED",
        "explicit_2h_literal": bool(re.search(r"['\"]2h['\"]", text, flags=re.IGNORECASE)),
        "contains_2d_default": 'htf_timeframe="2d"' in text,
        "contains_12h_default": 'bias_timeframe="12h"' in text,
        "contains_15m_execution_default": 'execution_timeframe="15m"' in text,
    }


def _source_commit(rows: Sequence[Mapping[str, Any]], malformed: Counter[str]) -> str:
    del rows, malformed
    return "NOT_RECORDED: raw scan payload is intentionally not selected by the live-safe audit."


def _repository_commit(repository_root: Path | str | None) -> str:
    root = Path(repository_root) if repository_root is not None else Path(__file__).resolve().parents[2]
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=root,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return "NOT_VERIFIABLE"
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and value else "NOT_VERIFIABLE"


def _selected_columns(columns: Iterable[str], table: str) -> str:
    allowed = SOURCE_COLUMN_ALLOWLIST.get(table, frozenset())
    selected = sorted(column for column in columns if column in allowed and SAFE_IDENTIFIER.fullmatch(column))
    if not selected:
        raise IncompatibleFunnelSchemaError(f"Incompatible schema: {table} has no readable allowlisted columns.")
    expressions: list[str] = []
    for column in selected:
        identifier = _identifier(column)
        if column in OPTIONAL_JSON_COLUMNS:
            expressions.extend(
                (
                    f"CASE WHEN length({identifier}) <= {MAX_JSON_FIELD_BYTES} THEN {identifier} ELSE NULL END AS {identifier}",
                    f"CASE WHEN length({identifier}) > {MAX_JSON_FIELD_BYTES} THEN 1 ELSE 0 END AS {_identifier(f'{column}_truncated_for_audit')}",
                )
            )
        else:
            expressions.append(identifier)
    return ", ".join(expressions)


def _identifier(value: str) -> str:
    if not SAFE_IDENTIFIER.fullmatch(value):
        raise FunnelAuditError(f"Unsafe SQLite identifier refused: {value!r}")
    return value


def _chunks(values: Sequence[str], size: int) -> Iterable[Sequence[str]]:
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _json_mapping(value: Any, source: str, malformed: Counter[str]) -> Mapping[str, Any]:
    if value is None or value == "":
        return {}
    if isinstance(value, Mapping):
        return value
    if not isinstance(value, str):
        malformed[f"{source}_wrong_type"] += 1
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        malformed[f"{source}_malformed_json"] += 1
        return {}
    if not isinstance(parsed, Mapping):
        malformed[f"{source}_not_object"] += 1
        return {}
    return parsed


def _row_timestamp(row: Mapping[str, Any], field: str) -> datetime | None:
    value = _text(row.get(field))
    if not value:
        return None
    try:
        return parse_utc_timestamp(value, argument_name=field)
    except FunnelAuditError:
        return None


def _row_has_timestamp_in_window(row: Mapping[str, Any], fields: Sequence[str], window: AuditWindow) -> bool:
    return any(_timestamp_in_window(_row_timestamp(row, field), window) for field in fields)


def _timestamp_in_window(timestamp: datetime | None, window: AuditWindow) -> bool:
    return timestamp is not None and window.start <= timestamp < window.end


def _sqlite_utc_lower_bound(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).replace(microsecond=0).isoformat(timespec="seconds")


def _sqlite_utc_upper_bound(timestamp: datetime) -> str:
    normalized = timestamp.astimezone(UTC)
    if normalized.microsecond:
        normalized = normalized.replace(microsecond=0) + timedelta(seconds=1)
    return normalized.isoformat(timespec="seconds")


def _utc_text(timestamp: datetime) -> str:
    return timestamp.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _filename_timestamp(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]+", "", value.replace("Z", "Z"))


def _report_label(value: str) -> str:
    label = value.strip()
    if not label or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", label):
        raise FunnelAuditError("report_label must be 1-80 characters using only letters, numbers, dot, underscore, or dash.")
    return label


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text.lower() in NA_VALUES else text


def _status_key(value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    return re.sub(r"_+", "_", re.sub(r"[^a-z0-9]+", "_", text.lower())).strip("_")


def _state_text(value: Any) -> str | None:
    text = _text(value)
    return text.upper() if text else None


def _enum_state(value: str | None) -> SetupLifecycleState | None:
    if value is None:
        return None
    try:
        return SetupLifecycleState(value)
    except ValueError:
        return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    text = _text(value)
    if text is None:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


def _decimal_text(value: Decimal | None) -> str:
    return format(value.normalize(), "f") if value is not None else "NOT_RECORDED"


def _integer_or_none(value: Any) -> int | None:
    decimal = _decimal_or_none(value)
    if decimal is None or decimal != decimal.to_integral_value():
        return None
    return int(decimal)


def _percentage(numerator: int, denominator: int) -> float | str:
    if denominator <= 0:
        return "NOT_RECORDED"
    return round((numerator / denominator) * 100, 4)


def _evaluation_id(row: Mapping[str, Any]) -> str | None:
    run_id = _text(row.get("run_id"))
    symbol = _text(row.get("symbol"))
    return f"{run_id}:{symbol}" if run_id and symbol else None


def _candidate_event_id(row: Mapping[str, Any]) -> str:
    value = _text(row.get("id"))
    return value or f"{_text(row.get('run_id')) or 'N/A'}:{_text(row.get('symbol')) or 'N/A'}:{_text(row.get('mode')) or 'N/A'}:{_text(row.get('direction')) or 'N/A'}"


def _grade_at_least_b_plus(value: str) -> bool:
    return _status_key(value) in {"a_plus", "a", "a_minus", "b_plus"}


def _top_counts(counts: Mapping[str, int], *, limit: int = 20) -> list[dict[str, Any]]:
    return [
        {"value": value, "count": count}
        for value, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))[:limit]
    ]


def _score_sort_key(value: str) -> tuple[int, Decimal | str]:
    decimal = _decimal_or_none(value)
    return (0, decimal) if decimal is not None else (1, value)


def _null_rates(rows: Sequence[Mapping[str, Any]], fields: Sequence[str]) -> dict[str, float | str]:
    if not rows:
        return {field: "NOT_RECORDED" for field in fields}
    return {
        field: round(sum(_text(row.get(field)) is None for row in rows) / len(rows) * 100, 4)
        for field in fields
    }


def _critical_fields_for_source(source: str) -> tuple[str, ...]:
    return {
        "scan_runs": ("run_id", "timestamp", "symbols_requested", "symbols_completed", "timeframes_json"),
        "symbol_results": ("run_id", "symbol", "status", "display_bucket", "failed_gate"),
        "setup_candidates": ("run_id", "symbol", "mode", "direction", "failed_gate"),
        "setup_lifecycle_events": ("lifecycle_id", "timestamp", "from_state", "to_state"),
        "setup_lifecycle_records": ("lifecycle_id", "current_state", "first_seen_at", "last_seen_at"),
        "setup_lifecycle_outcome_progress": ("lifecycle_id", "terminal_outcome", "outcome_at"),
        "telegram_alert_attempts": ("signal_id", "attempted_at", "telegram_status"),
    }[source]


def _as_sequence_rows(rows: Sequence[Mapping[str, Any]], _default: Sequence[Any]) -> Sequence[Mapping[str, Any]]:
    return rows


def _mapping_value(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else None


def _display_or_not_recorded(value: Any) -> str:
    return _text(value) or "NOT_RECORDED"


def _explicit_reason_contains(row: Mapping[str, Any], needle: str) -> bool:
    for field in ("dedupe_status", "dedupe_reason", "blocked_reason", "telegram_status"):
        key = _status_key(row.get(field))
        if needle in key and not key.startswith(("not_", "no_")):
            return True
    return False


def _explicit_suppression(row: Mapping[str, Any]) -> bool:
    return _status_key(row.get("telegram_status")) in {"suppressed", "blocked", "deduped"} or _status_key(row.get("dedupe_status")) in {"suppressed", "deduped", "duplicate"}


def _unique_text_count(rows: Sequence[Mapping[str, Any]], field: str) -> int:
    return len({_text(row.get(field)) for row in rows if _text(row.get(field))})


def _present_timestamp(value: Any) -> bool:
    return _row_timestamp({"value": value}, "value") is not None


def _safe_text(value: Any) -> str:
    text = _text(value) or "NOT_RECORDED"
    return SENSITIVE_VALUE.sub(r"\1=[REDACTED]", text)[:240]


__all__ = [
    "DEFAULT_MAX_DETAIL_RECORDS",
    "DEFAULT_MAX_ROWS_PER_SOURCE",
    "DEFAULT_MINIMUM_MEANINGFUL_WINDOW_SECONDS",
    "FunnelAuditError",
    "IncompatibleFunnelSchemaError",
    "TOOL_VERSION",
    "build_post_restart_funnel_report",
    "parse_utc_timestamp",
    "render_post_restart_funnel_text",
    "write_post_restart_funnel_reports",
]
