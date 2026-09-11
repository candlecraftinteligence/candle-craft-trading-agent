"""Bounded read-only Runtime checkpoint preflight collector and evidence assessment.

This module never deploys, migrates, checkpoints, vacuums, or opens a live path by
default. DEV tests and this process must pass an explicit existing source file.
Collector identity is not deployed-application identity.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

from app.storage.database import (
    SCHEMA_VERSION,
    DatabaseMissingError,
    StorageError,
    identify_schema_version,
    open_read_only_database,
    read_only_connection_safety_proof,
)
from app.storage.scan_payloads import INLINE_V1, SUPPORTED_FORMATS, SYMBOL_REFS_V1

TOOL_VERSION: Final[str] = "cci-runtime-checkpoint-readiness-v1"
TOOL_NAME: Final[str] = "cci-runtime-checkpoint-readiness"
CHECKPOINT_NAME: Final[str] = "FORENSIC_FOUNDATION_RUNTIME_CHECKPOINT"
FUNCTIONAL_ANCHOR_SHA: Final[str] = "2bac6b4eda271bc69f2c34f42d6b7592e37fa8cc"
OPERATIONAL_STATUS: Final[str] = "RUNTIME_CHECKPOINT_NOT_YET_AUTHORIZED"
UNAVAILABLE: Final[str] = "unavailable"
PENDING: Final[str] = "pending"

BUSY_TIMEOUT_MS: Final[int] = 250
QUERY_DEADLINE_MS: Final[int] = 2_000
RECENT_RUN_LIMIT: Final[int] = 5
PROGRESS_OPCODE_INTERVAL: Final[int] = 1_000
MAX_DETAIL_CHARS: Final[int] = 240
OPERATING_RESERVE_FLOOR_BYTES: Final[int] = 10 * 1024**3
OPERATING_RESERVE_FLOOR_PERCENT: Final[int] = 10
TIMESTAMP_INDEX: Final[str] = "ix_scan_runs_timestamp"
COMMIT_SHA_RE = re.compile(r"^[0-9a-f]{40}$", re.IGNORECASE)

_SECRET_KEY_FRAGMENTS: Final[tuple[str, ...]] = (
    "token",
    "password",
    "secret",
    "api_key",
    "apikey",
    "credential",
    "database_url",
    "chat_id",
    "invite",
    "donate",
    "authorization",
    "command_line",
    "cmdline",
    "environ",
    "environment_variable",
)

_WRITE_SQL_RE = re.compile(
    r"^\s*(INSERT|UPDATE|DELETE|REPLACE|CREATE|DROP|ALTER|VACUUM|REINDEX|"
    r"ANALYZE|ATTACH|DETACH)\b",
    re.IGNORECASE,
)

METADATA_TABLES: Final[tuple[str, ...]] = (
    "scan_runs",
    "symbol_results",
    "setup_candidates",
    "replay_results",
    "setup_lifecycle_records",
    "setup_lifecycle_events",
    "setup_lifecycle_outcome_progress",
    "setup_outcome_analytics",
    "telegram_alert_attempts",
    "public_alert_events",
    "public_alert_delivery_parts",
    "symbol_health",
    "symbol_health_events",
)

DECODER_COMPATIBILITY_INVENTORY: Final[dict[str, Any]] = {
    "status": "declared",
    "evidence_class": "SOURCE_INSPECTION_ONLY",
    "supported_formats": sorted(SUPPORTED_FORMATS),
    "legacy_without_raw_payload_format": (
        "load_logical_scan_payload treats stored JSON as inline_v1 and does not migrate"
    ),
    "inline_v1": INLINE_V1,
    "symbol_refs_v1": (
        f"{SYMBOL_REFS_V1} reconstructs using the exact enclosing run_id and symbol, "
        "then checks membership and SHA-256; failures raise ScanPayloadIntegrityError"
    ),
    "unknown_or_empty_format": "explicit integrity failure; never an empty or partial success",
    "inline_only_switch": (
        "SCAN_RAW_PAYLOAD_INLINE_ONLY=true stops future reference encoding on a "
        "decoder-capable release; it does not convert existing reference rows or make "
        "an old reader compatible"
    ),
}

CONSUMER_INVENTORY: Final[tuple[dict[str, Any], ...]] = (
    {
        "id": "scanner_watch_store",
        "module": "scripts/run_scan.py + app.storage.repositories.store_scan_result",
        "role": "writer",
        "db_path": "explicit --database-path; default scan_runs/candle_craft.db relative to CWD",
        "payload": "encodes via encode_scan_raw_payload; decoder-capable v25 writer",
        "startup": "scripts/run_scan.py including --watch / watch_supervisor",
    },
    {
        "id": "lifecycle_repository",
        "module": "app.lifecycle.repositories.SQLiteSetupLifecycleRepository",
        "role": "writer",
        "db_path": "same scanner database_path (DEFAULT_DATABASE_PATH if unset)",
        "payload": "lifecycle tables; not scan raw_payload_format",
        "startup": "enabled with --watch / --store-scan / --lifecycle",
    },
    {
        "id": "telegram_lifecycle_outbox",
        "module": "app.alerts.telegram_lifecycle",
        "role": "writer",
        "db_path": "same scanner database_path",
        "payload": "telegram_alert_attempts / public alert tables; SENT/UNCERTAIN states",
        "startup": "scanner delivery path; not the UI listener",
    },
    {
        "id": "symbol_health",
        "module": "app.storage.symbol_health",
        "role": "writer",
        "db_path": "same scanner database_path",
        "payload": "symbol_health tables",
        "startup": "scanner watch / store path",
    },
    {
        "id": "telegram_ui_listener",
        "module": "scripts/run_telegram_bot.py",
        "role": "reader",
        "db_path": "its own --database-path; default DEFAULT_DATABASE_PATH, not inferred from scanner",
        "payload": "open_read_only_database default (historic immutable-when-no-sidecars)",
        "startup": "separate process; --manifest-path --state-path --audit-path are independent",
        "note": "Do not assume listener defaults point at the scanner database.",
    },
    {
        "id": "telegram_admin_commands",
        "module": "app.telegram_admin.commands.TelegramAdminCommandService",
        "role": "reader",
        "db_path": "listener --database-path",
        "payload": "open_read_only_database default; scan_runs summaries",
        "startup": "scripts/run_telegram_bot.py / process_telegram_admin_commands.py",
    },
    {
        "id": "wolf_briefing",
        "module": "app.telegram_admin.wolf_briefing",
        "role": "reader",
        "db_path": "listener/command --database-path",
        "payload": "load_logical_scan_payload (decoder-capable)",
        "startup": "listener command path",
    },
    {
        "id": "active_watchlists",
        "module": "app.telegram_admin.active_watchlists",
        "role": "reader",
        "db_path": "listener --database-path",
        "payload": "open_read_only_database default; lifecycle/alert rows",
        "startup": "listener command path",
    },
    {
        "id": "scan_history_export",
        "module": "app.storage.repositories.list_scan_history / export_history_payload",
        "role": "reader",
        "db_path": "--database-path / DEFAULT_DATABASE_PATH",
        "payload": "scan_runs summary columns; not full raw payload reconstruction",
        "startup": "scripts/run_scan.py --history",
    },
    {
        "id": "research_queries",
        "module": "app.research.queries",
        "role": "reader",
        "db_path": "scripts/run_scan.py --research --database-path",
        "payload": "open_read_only_database default",
        "startup": "manual research CLI",
    },
    {
        "id": "public_alert_funnel_audit",
        "module": "app.analytics.public_alert_funnel",
        "role": "reader",
        "db_path": "explicit audit --database-path",
        "payload": "open_read_only_database default",
        "startup": "scripts/audit_public_alert_funnel.py",
    },
    {
        "id": "sqlite_maintenance_inspect",
        "module": "app.storage.maintenance.inspect_database",
        "role": "reader",
        "db_path": "explicit --database-path",
        "payload": (
            "open_read_only_database historic default assume_immutable_when_sidecars_absent=True; "
            "full table counts, dbstat, timestamp MIN/MAX, outbox aggregations"
        ),
        "startup": "scripts/sqlite_maintenance.py inspect",
        "note": "Not a bounded live preflight. Do not use it as the Runtime checkpoint collector.",
    },
    {
        "id": "sqlite_maintenance_backup",
        "module": "app.storage.maintenance.create_verified_backup",
        "role": "reader_of_source_writer_of_archive",
        "db_path": "explicit source plus archive directory",
        "payload": "SQLite online backup API; source open_read_only_database historic immutable default",
        "startup": "scripts/sqlite_maintenance.py backup",
        "note": (
            "Not certified for a mutable live source. Restrict to a proven-quiescent window "
            "(writers stopped; WAL/SHM absence verified) or STOP for a backup prerequisite."
        ),
    },
    {
        "id": "lifecycle_hygiene_repair",
        "module": "scripts/repair_lifecycle_hygiene.py",
        "role": "reader then optional writer",
        "db_path": "explicit --database-path",
        "payload": "must not run during checkpoint cutover except as a separately authorized copy repair",
        "startup": "manual",
    },
    {
        "id": "evidence_baseline_audit",
        "module": "app.analytics.evidence_baseline_audit",
        "role": "reader",
        "db_path": "explicit path; refuses live Runtime basename/path",
        "payload": "assume_immutable_when_sidecars_absent=False + consistent snapshot",
        "startup": "DEV/research only; not a Runtime preflight",
    },
    {
        "id": "post_restart_funnel_audit",
        "module": "app.analytics.post_restart_funnel_audit",
        "role": "reader",
        "db_path": "explicit --database-path",
        "payload": "open_read_only_database assume_immutable_when_sidecars_absent=True",
        "startup": "scripts/audit_post_restart_funnel.py; quiescent-immutable source mode",
    },
    {
        "id": "external_listener_state",
        "module": "file-backed, not SQLite",
        "role": "external_state",
        "db_path": "n/a",
        "payload": (
            "scan_runs/scan_run_manifest.jsonl; scan_runs/admin_commands/state.json; "
            "scan_runs/admin_commands/commands.jsonl; scan_runs/latest_scan.json; "
            "scan_runs/performance_memory.json; optional watch JSONL"
        ),
        "startup": "listener --manifest-path/--state-path/--audit-path; scanner watch artifacts",
        "note": "SQLite does not contain every restart-safety side effect.",
    },
    {
        "id": "unknown_external_readers",
        "module": "unresolved",
        "role": "unresolved_gate",
        "db_path": "UNKNOWN until Runtime process/configuration inspection",
        "payload": "UNKNOWN",
        "startup": "UNKNOWN",
        "note": "Unknown external readers remain unresolved rollout gates.",
    },
)

CAPACITY_TERM_KEYS: Final[tuple[str, ...]] = (
    "new_backup_bytes",
    "concurrent_restore_or_candidate_bytes",
    "migration_temp_bytes",
    "additional_peak_WAL_and_log_bytes",
    "growth_budget_bytes",
    "operating_reserve_bytes",
)


class ReadinessError(ValueError):
    """Raised when the readiness collector or writer refuses to proceed."""


def collect_runtime_checkpoint_preflight(
    source_path: Path | str,
    *,
    filesystem_only: bool = False,
    include_recent_runs: bool = False,
    busy_timeout_ms: int = BUSY_TIMEOUT_MS,
    query_deadline_ms: int = QUERY_DEADLINE_MS,
    recent_run_limit: int = RECENT_RUN_LIMIT,
    progress_opcode_interval: int = PROGRESS_OPCODE_INTERVAL,
    now: datetime | None = None,
    clock: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Collect a one-shot read-only preflight report for an explicit existing path."""

    started = _utc(now)
    requested = Path(source_path)
    if not str(requested).strip():
        raise ReadinessError("Source path must be an explicit non-empty path.")
    if not requested.exists():
        raise ReadinessError(f"Source path does not exist: {requested}")
    if not requested.is_file():
        raise ReadinessError(f"Source path is not a file: {requested}")

    resolved = requested.resolve(strict=True)
    filesystem = collect_filesystem_observation(resolved, now=started)
    sqlite_section: dict[str, Any]
    if filesystem_only:
        sqlite_section = {
            "status": "not_requested",
            "reason": "filesystem_only",
        }
    else:
        sqlite_section = collect_sqlite_metadata(
            resolved,
            include_recent_runs=include_recent_runs,
            busy_timeout_ms=busy_timeout_ms,
            query_deadline_ms=query_deadline_ms,
            recent_run_limit=recent_run_limit,
            progress_opcode_interval=progress_opcode_interval,
            clock=clock,
        )

    finished = _utc(None) if now is None else _utc(now)
    return _redact_secrets(
        {
            "tool_version": TOOL_VERSION,
            "report_kind": "runtime_checkpoint_preflight",
            "operational_status": OPERATIONAL_STATUS,
            "checkpoint_name": CHECKPOINT_NAME,
            "target": {
                "functional_anchor_sha": FUNCTIONAL_ANCHOR_SHA,
                "deployment_sha": {
                    "status": PENDING,
                    "reason": "exact_release_sha_is_the_reviewed_main_merge_of_this_readiness_pr",
                },
                "application_schema_version": SCHEMA_VERSION,
            },
            "collector_identity": collector_identity(),
            "observed_deployed_application": {
                "status": UNAVAILABLE,
                "reason": "not_inferred_from_collector_checkout",
                "note": (
                    "Running this diagnostic from a new checkout must never label an old "
                    "running process as the new release."
                ),
            },
            "budgets": {
                "busy_timeout_ms": int(busy_timeout_ms),
                "query_deadline_ms": int(query_deadline_ms),
                "recent_run_limit": int(recent_run_limit),
                "progress_opcode_interval": int(progress_opcode_interval),
            },
            "source": {
                "requested_path": str(requested),
                "resolved_path": str(resolved),
                "exists": True,
                "is_file": True,
            },
            "filesystem": filesystem,
            "sqlite": sqlite_section,
            "decoder_compatibility": dict(DECODER_COMPATIBILITY_INVENTORY),
            "consumer_inventory": {
                "status": "declared",
                "evidence_class": "SOURCE_INSPECTION_ONLY",
                "consumers": [dict(item) for item in CONSUMER_INVENTORY],
                "note": (
                    "This inventory is source-scoped. Actual Runtime process-to-path ownership "
                    "is UNKNOWN until inspected there."
                ),
            },
            "measurement": {
                "started_utc": _iso_utc(started),
                "finished_utc": _iso_utc(finished),
                "sampling": "non_atomic_sequential",
            },
            "go_for_runtime_deployment": False,
        }
    )


def collect_filesystem_observation(source_path: Path, *, now: datetime | None = None) -> dict[str, Any]:
    observed_at = _utc(now)
    main = _file_observation(source_path, observed_at)
    wal = _file_observation(Path(f"{source_path}-wal"), observed_at)
    shm = _file_observation(Path(f"{source_path}-shm"), observed_at)
    combined = int(main["size_bytes"]) + int(wal["size_bytes"]) + int(shm["size_bytes"])
    return {
        "status": "measured",
        "measured_utc": _iso_utc(observed_at),
        "main": main,
        "wal": wal,
        "shm": shm,
        "combined_allocated_bytes": combined,
        "journal_mode_file_header": _journal_mode_from_header(source_path),
        "volume": collect_volume_facts(source_path),
        "limits": [
            "Main, WAL, SHM, and volume free-space samples are sequential and not a single atomic snapshot.",
            "A drive letter does not prove local storage, durability, or free capacity on another destination.",
        ],
    }


def collect_volume_facts(source_path: Path) -> dict[str, Any]:
    usage_root = source_path.parent if source_path.is_file() else source_path
    try:
        usage = shutil.disk_usage(usage_root)
        volume: dict[str, Any] = {
            "status": "measured",
            "total_bytes": int(usage.total),
            "free_bytes": int(usage.free),
            "used_bytes": int(usage.used),
            "resolved_parent": str(usage_root.resolve(strict=False)),
        }
    except OSError as exc:
        return {
            "status": UNAVAILABLE,
            "reason": "disk_usage_failed",
            "detail": _detail(exc),
        }

    if os.name == "nt":
        volume.update(_windows_volume_facts(source_path))
    else:
        volume.update(
            {
                "root": str(source_path.resolve(strict=False).anchor or "/"),
                "drive_type": {"status": UNAVAILABLE, "reason": "not_windows"},
                "filesystem": {"status": UNAVAILABLE, "reason": "not_windows"},
                "volume_name": {"status": UNAVAILABLE, "reason": "not_windows"},
                "volume_serial_hex": {"status": UNAVAILABLE, "reason": "not_windows"},
                "local_device": {"status": UNAVAILABLE, "reason": "not_windows"},
                "note": "Windows volume identity cannot be established on this platform.",
            }
        )
    return volume


def collect_sqlite_metadata(
    source_path: Path,
    *,
    include_recent_runs: bool,
    busy_timeout_ms: int,
    query_deadline_ms: int,
    recent_run_limit: int,
    progress_opcode_interval: int,
    clock: Callable[[], float] | None,
) -> dict[str, Any]:
    connection: sqlite3.Connection | None = None
    monotonic = clock or time.monotonic
    deadline = monotonic() + max(1, int(query_deadline_ms)) / 1_000
    timed_out = False

    def progress_callback() -> int:
        nonlocal timed_out
        if monotonic() >= deadline:
            timed_out = True
            return 1
        return 0

    try:
        wal_path = Path(f"{source_path}-wal")
        shm_path = Path(f"{source_path}-shm")
        wal_exists = wal_path.exists()
        shm_exists = shm_path.exists()
        if wal_exists and not shm_exists:
            return {
                "status": UNAVAILABLE,
                "reason": "wal_without_shm",
                "detail": (
                    "Read-only WAL inspection requires the existing -shm sidecar; "
                    "refusing to create it or fall back to immutable=1."
                ),
            }
        if shm_exists and not wal_exists:
            return {
                "status": UNAVAILABLE,
                "reason": "shm_without_wal",
                "detail": "SHM present without WAL; refusing to open or repair sidecars.",
            }
        header_mode = _journal_mode_from_header(source_path)
        if header_mode == "wal" and not wal_exists and not shm_exists:
            return {
                "status": UNAVAILABLE,
                "reason": "wal_header_without_sidecars",
                "detail": (
                    "A WAL-format main file with no -wal/-shm would require creating sidecars "
                    "or immutable=1. This preflight refuses both; retain filesystem evidence "
                    "or inspect a verified offline copy / proven-quiescent window."
                ),
            }
        connection = open_read_only_database(
            source_path,
            require_supported_schema=False,
            busy_timeout_ms=max(1, int(busy_timeout_ms)),
            assume_immutable_when_sidecars_absent=False,
            require_consistent_snapshot=True,
            require_immutable_source=False,
        )
        proof = dict(read_only_connection_safety_proof(connection))
        if proof.get("immutable_requested") is True or "immutable=1" in str(proof):
            raise StorageError("Refusing immutable=1 on a Runtime checkpoint preflight connection.")
        connection.set_progress_handler(progress_callback, max(1, int(progress_opcode_interval)))
        schema_version = identify_schema_version(connection)
        tables = _table_names(connection)
        columns = {
            table: _table_columns(connection, table)
            for table in METADATA_TABLES
            if table in tables
        }
        indexes = _index_names(connection)
        page_count = _pragma_int(connection, "page_count")
        page_size = _pragma_int(connection, "page_size")
        freelist_count = _pragma_int(connection, "freelist_count")
        recent: dict[str, Any]
        if include_recent_runs:
            recent = _recent_run_sample(
                connection,
                tables=tables,
                columns=columns,
                indexes=indexes,
                limit=max(1, int(recent_run_limit)),
            )
        else:
            recent = {"status": "not_requested"}
        return {
            "status": "measured",
            "connection": {
                "sqlite_uri_mode": proof.get("sqlite_uri_mode"),
                "query_only_readback": proof.get("query_only_readback"),
                "query_only_verified": proof.get("query_only_verified"),
                "immutable_requested": proof.get("immutable_requested", False),
                "live_mutable_source": proof.get("live_mutable_source", True),
                "consistent_read_snapshot": proof.get("consistent_read_snapshot"),
                "busy_timeout_ms": proof.get("busy_timeout_ms", int(busy_timeout_ms)),
                "note": (
                    "Connection PRAGMA readback is not proof of another process's writer settings. "
                    "A read-only snapshot does not prove shared-memory bytes remained unchanged."
                ),
            },
            "schema_version": schema_version,
            "supported_schema_version": SCHEMA_VERSION,
            "schema_status": _schema_status(schema_version),
            "tables": sorted(tables),
            "columns": columns,
            "indexes": indexes,
            "page_count": page_count,
            "page_size": page_size,
            "freelist_count": freelist_count,
            "logical_database_bytes": page_count * page_size,
            "recent_runs": recent,
        }
    except DatabaseMissingError as exc:
        return {"status": UNAVAILABLE, "reason": "database_missing", "detail": _detail(exc)}
    except StorageError as exc:
        reason = "storage_error"
        text = str(exc)
        if "shm sidecar" in text:
            reason = "wal_without_shm"
        elif "locked" in text.lower() or "busy" in text.lower():
            reason = "busy_or_locked"
        return {"status": UNAVAILABLE, "reason": reason, "detail": _detail(exc)}
    except sqlite3.OperationalError as exc:
        reason = "query_deadline_exceeded" if timed_out or "interrupt" in str(exc).lower() else "sqlite_operational_error"
        return {"status": UNAVAILABLE, "reason": reason, "detail": _detail(exc)}
    except sqlite3.Error as exc:
        return {"status": UNAVAILABLE, "reason": "sqlite_error", "detail": _detail(exc)}
    finally:
        if connection is not None:
            try:
                connection.set_progress_handler(None, 0)
            except sqlite3.Error:
                pass
            try:
                if connection.in_transaction:
                    connection.rollback()
            except sqlite3.Error:
                pass
            connection.close()


def collector_identity() -> dict[str, Any]:
    module_path = Path(__file__).resolve()
    try:
        module_sha = hashlib.sha256(module_path.read_bytes()).hexdigest()
        module_sha_status = "measured"
    except OSError:
        module_sha = None
        module_sha_status = UNAVAILABLE
    return {
        "tool_name": TOOL_NAME,
        "tool_version": TOOL_VERSION,
        "module_path": str(module_path),
        "module_sha256": module_sha,
        "module_sha256_status": module_sha_status,
        "git": _collector_git_info(),
        "process": {
            "python_executable": sys.executable,
            "python_version": sys.version.split()[0],
            "sqlite_linked_version": sqlite3.sqlite_version,
            "note": "Collector process facts are not the deployed application identity.",
        },
        "note": (
            "Collector identity is distinct from any observed deployed application SHA. "
            "Do not copy these values onto a running old process."
        ),
    }


def write_report_exclusive(payload: Mapping[str, Any], output_path: Path | str) -> Path:
    path = Path(output_path)
    if not str(path).strip():
        raise ReadinessError("Output path must be an explicit non-empty path.")
    if path.exists():
        raise ReadinessError(f"Refusing to overwrite existing output: {path}")
    parent = path.parent
    if not parent.exists() or not parent.is_dir():
        raise ReadinessError(f"Output parent directory does not exist: {parent}")
    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as handle:
            created = True
            json.dump(_redact_secrets(payload), handle, indent=2, sort_keys=True, ensure_ascii=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except (OSError, TypeError, ValueError) as exc:
        if created:
            try:
                path.unlink()
            except OSError:
                pass
        raise ReadinessError(f"Unable to write report: {path}") from exc
    return path.resolve(strict=True)


def load_json_object(path: Path | str) -> dict[str, Any]:
    candidate = Path(path)
    if not candidate.exists() or not candidate.is_file():
        raise ReadinessError(f"Evidence file does not exist: {candidate}")
    try:
        payload = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReadinessError(f"Evidence file is not valid JSON: {candidate}") from exc
    if not isinstance(payload, dict):
        raise ReadinessError(f"Evidence file is not a JSON object: {candidate}")
    return payload


def assess_runtime_checkpoint_evidence(packet: Mapping[str, Any]) -> dict[str, Any]:
    """Deterministically assess a supplied evidence packet. Never executes remediation."""

    cleaned = _redact_secrets(packet)
    missing: list[str] = []
    adverse: list[str] = []
    notes: list[str] = [
        "This assessment never authorizes Runtime deployment.",
        "Completing a readiness PR does not mean GO_FOR_RUNTIME_DEPLOYMENT.",
        "Unobserved is not zero; a failed sample is not an empty successful scan.",
    ]

    collector = _mapping(cleaned.get("collector_report"))
    operator = _mapping(cleaned.get("operator"))
    sqlite_section = _mapping(collector.get("sqlite"))
    filesystem = _mapping(collector.get("filesystem"))

    deployed_sha = operator.get("deployed_application_sha")
    if not _is_commit_sha(deployed_sha):
        missing.append("operator.deployed_application_sha")
    collector_sha = (
        _mapping(collector.get("collector_identity")).get("git", {})
        if isinstance(collector.get("collector_identity"), Mapping)
        else {}
    )
    if isinstance(collector_sha, Mapping) and _is_commit_sha(deployed_sha):
        if collector_sha.get("commit_sha") == deployed_sha and collector:
            notes.append(
                "Deployed application SHA equals this collector checkout SHA only if that equality "
                "was independently verified from the running process; this tool does not prove it."
            )

    target_sha = operator.get("target_sha", _nested(collector, "target", "deployment_sha"))
    if _mapping(target_sha).get("status") == PENDING or target_sha == PENDING or not _is_commit_sha(target_sha):
        missing.append("exact_reviewed_main_release_sha")

    observed_schema = sqlite_section.get("schema_version")
    if not isinstance(observed_schema, int):
        missing.append("collector_report.sqlite.schema_version")
    elif observed_schema > SCHEMA_VERSION:
        adverse.append(
            f"observed schema {observed_schema} is newer than supported {SCHEMA_VERSION}"
        )
    elif observed_schema < SCHEMA_VERSION:
        notes.append(
            f"observed schema {observed_schema} is older than target {SCHEMA_VERSION}; "
            "actual-schema copy rehearsal on Runtime remains required"
        )

    accounted = operator.get("accounted_consumers")
    known_ids = {str(item["id"]) for item in CONSUMER_INVENTORY}
    if not isinstance(accounted, list) or not accounted:
        missing.append("operator.accounted_consumers")
        unresolved_consumers = sorted(known_ids)
    else:
        accounted_ids = {str(item) for item in accounted}
        unresolved_consumers = sorted(known_ids - accounted_ids)
        if unresolved_consumers:
            missing.append("unresolved_consumers:" + ",".join(unresolved_consumers))
        if "unknown_external_readers" in accounted_ids and operator.get("unknown_external_readers_resolved") is not True:
            missing.append("unknown_external_readers")

    restore = _mapping(operator.get("restore_evidence"))
    if restore.get("restore_tested") is not True:
        missing.append("restore_evidence.restore_tested")
    if restore.get("integrity_ok") is False:
        adverse.append("restore integrity_ok is false")
    if restore.get("restore_path_distinct_from_source") is not True:
        missing.append("restore_evidence.restore_path_distinct_from_source")
    if not isinstance(restore.get("snapshot_identity"), str) or not str(restore.get("snapshot_identity")).strip():
        missing.append("restore_evidence.snapshot_identity")

    capacity = assess_capacity_budget(_mapping(operator.get("capacity")))
    if capacity["status"] == "incomplete":
        missing.extend(capacity["missing"])
    elif capacity["status"] == "insufficient":
        adverse.append("insufficient_free_capacity")

    if sqlite_section.get("status") == UNAVAILABLE:
        notes.append(
            f"SQLite inspection unavailable ({sqlite_section.get('reason')}); "
            "filesystem-only evidence cannot substitute schema/decoder facts."
        )
        if "collector_report.sqlite.schema_version" not in missing:
            missing.append("sqlite_inspection_measured")

    if filesystem.get("status") != "measured":
        missing.append("filesystem_observation")

    if cleaned.get("go_for_runtime_deployment") is True or operator.get("go_for_runtime_deployment") is True:
        notes.append("Supplied GO_FOR_RUNTIME_DEPLOYMENT was ignored; this tool cannot grant it.")

    if adverse:
        disposition = "ADVERSE_MEASURED_RESULT"
    elif missing:
        disposition = "INCOMPLETE_PREREQUISITES"
    else:
        disposition = "PACKET_REVIEWABLE_NOT_AUTHORIZED"
        notes.append(
            "Packet fields are internally present and not adverse; operational authorization "
            "and Runtime-observed evidence remain required."
        )

    return _redact_secrets(
        {
            "tool_version": TOOL_VERSION,
            "report_kind": "runtime_checkpoint_assessment",
            "operational_status": OPERATIONAL_STATUS,
            "checkpoint_name": CHECKPOINT_NAME,
            "overall_disposition": disposition,
            "go_for_runtime_deployment": False,
            "missing_prerequisites": missing,
            "adverse_results": adverse,
            "capacity": capacity,
            "schema": {
                "observed": observed_schema if isinstance(observed_schema, int) else UNAVAILABLE,
                "target": SCHEMA_VERSION,
            },
            "release": {
                "functional_anchor_sha": FUNCTIONAL_ANCHOR_SHA,
                "deployed_application_sha": deployed_sha if _is_commit_sha(deployed_sha) else UNAVAILABLE,
                "target_sha": target_sha if _is_commit_sha(target_sha) else PENDING,
            },
            "consumers": {
                "known_ids": sorted(known_ids),
                "unresolved": unresolved_consumers,
            },
            "restore": {
                "restore_tested": restore.get("restore_tested"),
                "integrity_ok": restore.get("integrity_ok"),
                "snapshot_identity_present": bool(str(restore.get("snapshot_identity") or "").strip()),
            },
            "notes": notes,
        }
    )


def assess_capacity_budget(capacity: Mapping[str, Any]) -> dict[str, Any]:
    missing: list[str] = []
    total = capacity.get("volume_total_bytes")
    free = capacity.get("volume_free_bytes")
    if not _is_positive_int(total):
        missing.append("capacity.volume_total_bytes")
    if not _is_non_negative_int(free):
        missing.append("capacity.volume_free_bytes")

    terms: dict[str, int] = {}
    for key in CAPACITY_TERM_KEYS:
        value = capacity.get(key)
        if key == "growth_budget_bytes":
            if not _is_positive_int(value):
                missing.append(
                    "capacity.growth_budget_bytes (zero/negative/unknown growth does not establish runway)"
                )
            else:
                terms[key] = int(value)
            continue
        if not _is_non_negative_int(value):
            missing.append(f"capacity.{key}")
        else:
            terms[key] = int(value)

    if missing:
        return {
            "status": "incomplete",
            "reason": "STOP_FOR_CAPACITY_EVIDENCE",
            "missing": missing,
            "required_free_bytes": UNAVAILABLE,
            "volume_free_bytes": free if _is_non_negative_int(free) else UNAVAILABLE,
            "planning_reserve_floor_bytes": UNAVAILABLE,
        }

    assert isinstance(total, int) and isinstance(free, int)
    required = sum(terms[key] for key in CAPACITY_TERM_KEYS)
    reserve_floor = max(OPERATING_RESERVE_FLOOR_BYTES, total * OPERATING_RESERVE_FLOOR_PERCENT // 100)
    reserve = terms["operating_reserve_bytes"]
    reserve_note = None
    if reserve < reserve_floor and capacity.get("stronger_reserve_requirement_recorded") is not True:
        return {
            "status": "incomplete",
            "reason": "STOP_FOR_CAPACITY_EVIDENCE",
            "missing": [
                "capacity.operating_reserve_bytes below planning floor "
                f"(max(10GiB, 10% of volume)={reserve_floor}) without a recorded stronger requirement"
            ],
            "required_free_bytes": required,
            "volume_free_bytes": free,
            "planning_reserve_floor_bytes": reserve_floor,
        }
    if reserve < reserve_floor:
        reserve_note = "operator recorded a stronger requirement below the default planning floor"

    shared = bool(capacity.get("backup_shares_db_volume", True)) and bool(
        capacity.get("restore_shares_db_volume", True)
    )
    if not shared:
        for extra in ("backup_volume_free_bytes", "restore_volume_free_bytes"):
            if not _is_non_negative_int(capacity.get(extra)):
                return {
                    "status": "incomplete",
                    "reason": "STOP_FOR_CAPACITY_EVIDENCE",
                    "missing": [f"capacity.{extra}"],
                    "required_free_bytes": required,
                    "volume_free_bytes": free,
                    "planning_reserve_floor_bytes": reserve_floor,
                }

    sufficient = free >= required
    return {
        "status": "measured" if sufficient else "insufficient",
        "reason": None if sufficient else "insufficient_free_capacity",
        "missing": [],
        "required_free_bytes": required,
        "volume_total_bytes": total,
        "volume_free_bytes": free,
        "planning_reserve_floor_bytes": reserve_floor,
        "terms": terms,
        "shared_volume_accounting": shared,
        "sufficient_on_packet": sufficient,
        "reserve_note": reserve_note,
        "note": (
            "Existing allocations are already reflected in measured free space and are not charged twice. "
            "This is a planning assessment of a supplied packet, not Runtime-observed capacity."
        ),
    }


def _recent_run_sample(
    connection: sqlite3.Connection,
    *,
    tables: set[str],
    columns: Mapping[str, list[str]],
    indexes: Mapping[str, list[str]],
    limit: int,
) -> dict[str, Any]:
    if "scan_runs" not in tables:
        return {"status": UNAVAILABLE, "reason": "scan_runs_table_absent"}
    scan_columns = set(columns.get("scan_runs", ()))
    if "timestamp" not in scan_columns or "run_id" not in scan_columns:
        return {"status": UNAVAILABLE, "reason": "required_columns_absent"}
    scan_indexes = indexes.get("scan_runs", [])
    if TIMESTAMP_INDEX not in scan_indexes:
        return {
            "status": UNAVAILABLE,
            "reason": "timestamp_index_absent",
            "detail": "LIMIT alone does not bound an unindexed scan or sort.",
        }
    plan_rows = connection.execute(
        f"EXPLAIN QUERY PLAN SELECT run_id, timestamp FROM scan_runs ORDER BY timestamp DESC LIMIT {int(limit)}"
    ).fetchall()
    plan_text = " | ".join(str(row[-1]) for row in plan_rows)
    if TIMESTAMP_INDEX not in plan_text:
        return {
            "status": UNAVAILABLE,
            "reason": "indexed_access_unproven",
            "query_plan": plan_text,
        }
    select_format = "raw_payload_format" in scan_columns
    if select_format:
        sql = (
            "SELECT run_id, timestamp, raw_payload_format FROM scan_runs "
            "ORDER BY timestamp DESC LIMIT ?"
        )
    else:
        sql = "SELECT run_id, timestamp FROM scan_runs ORDER BY timestamp DESC LIMIT ?"
    rows = connection.execute(sql, (int(limit),)).fetchall()
    samples: list[dict[str, Any]] = []
    for row in rows:
        mapping = _row_mapping(row)
        sample = {
            "run_id": mapping.get("run_id"),
            "timestamp": mapping.get("timestamp"),
        }
        if select_format:
            sample["raw_payload_format"] = mapping.get("raw_payload_format")
        else:
            sample["raw_payload_format"] = {
                "status": UNAVAILABLE,
                "reason": "column_absent",
            }
        samples.append(sample)
    return {
        "status": "measured",
        "limit": int(limit),
        "truncated": len(samples) >= int(limit),
        "row_count": len(samples),
        "query_plan": plan_text,
        "rows": samples,
        "note": "Sample is not a table count, payload dump, or historical capture series.",
    }


def _windows_volume_facts(source_path: Path) -> dict[str, Any]:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    GetVolumePathNameW = kernel32.GetVolumePathNameW
    GetVolumePathNameW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
    GetVolumePathNameW.restype = wintypes.BOOL
    GetDriveTypeW = kernel32.GetDriveTypeW
    GetDriveTypeW.argtypes = [wintypes.LPCWSTR]
    GetDriveTypeW.restype = wintypes.UINT
    GetVolumeInformationW = kernel32.GetVolumeInformationW
    GetVolumeInformationW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPWSTR,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR,
        wintypes.DWORD,
    ]
    GetVolumeInformationW.restype = wintypes.BOOL

    drive_types = {
        0: "DRIVE_UNKNOWN",
        1: "DRIVE_NO_ROOT_DIR",
        2: "DRIVE_REMOVABLE",
        3: "DRIVE_FIXED",
        4: "DRIVE_REMOTE",
        5: "DRIVE_CDROM",
        6: "DRIVE_RAMDISK",
    }
    root_buf = ctypes.create_unicode_buffer(261)
    target = str(source_path.resolve(strict=False))
    if not GetVolumePathNameW(target, root_buf, len(root_buf)):
        return {
            "root": {"status": UNAVAILABLE, "reason": "GetVolumePathNameW_failed"},
            "drive_type": {"status": UNAVAILABLE, "reason": "volume_root_unavailable"},
            "filesystem": {"status": UNAVAILABLE, "reason": "volume_root_unavailable"},
            "volume_name": {"status": UNAVAILABLE, "reason": "volume_root_unavailable"},
            "volume_serial_hex": {"status": UNAVAILABLE, "reason": "volume_root_unavailable"},
            "local_device": {"status": UNAVAILABLE, "reason": "volume_root_unavailable"},
        }
    root = root_buf.value
    drive_code = int(GetDriveTypeW(root))
    drive_type = drive_types.get(drive_code, f"DRIVE_CODE_{drive_code}")
    vol_name = ctypes.create_unicode_buffer(261)
    fs_name = ctypes.create_unicode_buffer(261)
    serial = wintypes.DWORD()
    max_comp = wintypes.DWORD()
    flags = wintypes.DWORD()
    info_ok = bool(
        GetVolumeInformationW(
            root,
            vol_name,
            len(vol_name),
            ctypes.byref(serial),
            ctypes.byref(max_comp),
            ctypes.byref(flags),
            fs_name,
            len(fs_name),
        )
    )
    return {
        "root": root,
        "drive_type": drive_type,
        "filesystem": fs_name.value if info_ok else {"status": UNAVAILABLE, "reason": "GetVolumeInformationW_failed"},
        "volume_name": vol_name.value if info_ok else {"status": UNAVAILABLE, "reason": "GetVolumeInformationW_failed"},
        "volume_serial_hex": format(int(serial.value), "08X") if info_ok else {
            "status": UNAVAILABLE,
            "reason": "GetVolumeInformationW_failed",
        },
        "local_device": drive_type != "DRIVE_REMOTE",
        "note": (
            "local_device reports Windows GetDriveTypeW != DRIVE_REMOTE; it is not proof of "
            "durability, exclusive ownership, or backup capacity."
        ),
    }


def _collector_git_info() -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    try:
        inside = _git_output(["rev-parse", "--is-inside-work-tree"], cwd=root)
        if inside != "true":
            return {"status": UNAVAILABLE, "reason": "not_a_git_work_tree"}
        sha = _git_output(["rev-parse", "HEAD"], cwd=root)
        if not COMMIT_SHA_RE.fullmatch(sha):
            return {"status": UNAVAILABLE, "reason": "unrecognized_head_sha"}
        porcelain = _git_output(["status", "--porcelain"], cwd=root, allow_empty=True)
        return {
            "status": "captured",
            "commit_sha": sha,
            "dirty_working_tree": bool(porcelain),
            "role": "collector_checkout_only",
        }
    except (OSError, subprocess.SubprocessError, ReadinessError):
        return {"status": UNAVAILABLE, "reason": "git_probe_failed"}


def _git_output(args: list[str], *, cwd: Path, allow_empty: bool = False) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    if completed.returncode != 0:
        raise ReadinessError("git command failed")
    text = completed.stdout.strip()
    if not text and not allow_empty:
        raise ReadinessError("git command returned empty output")
    return text


def _file_observation(path: Path, observed_at: datetime) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "path": str(path),
        "exists": False,
        "size_bytes": 0,
        "measured_utc": _iso_utc(observed_at),
        "mtime_utc": UNAVAILABLE,
    }
    try:
        if not path.exists() or not path.is_file():
            return payload
        stat = path.stat()
        payload["exists"] = True
        payload["size_bytes"] = int(stat.st_size)
        payload["mtime_utc"] = _iso_utc(datetime.fromtimestamp(stat.st_mtime, tz=UTC))
        return payload
    except OSError as exc:
        payload["exists"] = UNAVAILABLE
        payload["reason"] = "stat_failed"
        payload["detail"] = _detail(exc)
        return payload


def _journal_mode_from_header(path: Path) -> dict[str, Any] | str:
    try:
        with path.open("rb") as handle:
            header = handle.read(20)
    except OSError as exc:
        return {"status": UNAVAILABLE, "reason": "header_unreadable", "detail": _detail(exc)}
    if header.startswith(b"SQLite format 3\x00") and len(header) >= 20 and header[18] == 2 and header[19] == 2:
        return "wal"
    if header.startswith(b"SQLite format 3\x00"):
        return "non-wal-header"
    return {"status": UNAVAILABLE, "reason": "not_sqlite_header"}


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    }


def _table_columns(connection: sqlite3.Connection, table: str) -> list[str]:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", table):
        return []
    return [str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})").fetchall()]


def _index_names(connection: sqlite3.Connection) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = {}
    rows = connection.execute(
        "SELECT tbl_name, name FROM sqlite_master WHERE type = 'index' AND name IS NOT NULL"
    ).fetchall()
    for table, name in rows:
        grouped.setdefault(str(table), []).append(str(name))
    for table in grouped:
        grouped[table] = sorted(grouped[table])
    return grouped


def _pragma_int(connection: sqlite3.Connection, pragma: str) -> int:
    row = connection.execute(f"PRAGMA {pragma}").fetchone()
    if row is None:
        raise StorageError(f"SQLite did not return PRAGMA {pragma}.")
    return int(row[0])


def _row_mapping(row: Any) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    keys = getattr(row, "keys", None)
    if callable(keys):
        return {str(key): row[key] for key in keys()}
    raise StorageError("SQLite row could not be mapped.")


def _schema_status(schema_version: int) -> str:
    if schema_version == SCHEMA_VERSION:
        return "current"
    if schema_version > SCHEMA_VERSION:
        return "unsupported-newer"
    if schema_version <= 0:
        return "unversioned"
    return "older-supported-for-read-only-inspection"


def _redact_secrets(value: Any) -> Any:
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, inner in value.items():
            name = str(key).lower()
            if any(fragment in name for fragment in _SECRET_KEY_FRAGMENTS):
                continue
            redacted[str(key)] = _redact_secrets(inner)
        return redacted
    if isinstance(value, list):
        return [_redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [_redact_secrets(item) for item in value]
    return value


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _nested(payload: Mapping[str, Any], *keys: str) -> Any:
    current: Any = payload
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def _is_commit_sha(value: Any) -> bool:
    return isinstance(value, str) and bool(COMMIT_SHA_RE.fullmatch(value.strip()))


def _is_non_negative_int(value: Any) -> bool:
    return type(value) is int and value >= 0


def _is_positive_int(value: Any) -> bool:
    return type(value) is int and value > 0


def _detail(exc: BaseException) -> str:
    text = str(exc).strip().replace("\n", " ")
    if len(text) > MAX_DETAIL_CHARS:
        return text[: MAX_DETAIL_CHARS - 3] + "..."
    return text


def _utc(value: datetime | None) -> datetime:
    current = value or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return current.astimezone(UTC)


def _iso_utc(value: datetime) -> str:
    return _utc(value).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def assert_sql_is_read_only(sql: str) -> None:
    """Test helper: refuse obvious write/maintenance SQL. Not a complete SQLite parser."""

    if _WRITE_SQL_RE.match(sql):
        raise AssertionError(f"write SQL is not permitted during preflight: {sql}")
    compact = re.sub(r"\s+", " ", sql).lower()
    if "count(*)" in compact.replace(" ", ""):
        raise AssertionError(f"full table COUNT(*) is not permitted during preflight: {sql}")
    if "dbstat" in compact:
        raise AssertionError(f"dbstat is not permitted during preflight: {sql}")
    if "integrity_check" in compact or "quick_check" in compact or "foreign_key_check" in compact:
        raise AssertionError(f"integrity scans are not permitted during preflight: {sql}")
    if "pragma journal_mode" in compact and "=" in compact:
        raise AssertionError(f"journal_mode change is not permitted during preflight: {sql}")
    if "wal_checkpoint" in compact:
        raise AssertionError(f"wal_checkpoint is not permitted during preflight: {sql}")


__all__ = [
    "BUSY_TIMEOUT_MS",
    "CAPACITY_TERM_KEYS",
    "CHECKPOINT_NAME",
    "CONSUMER_INVENTORY",
    "DECODER_COMPATIBILITY_INVENTORY",
    "FUNCTIONAL_ANCHOR_SHA",
    "OPERATIONAL_STATUS",
    "QUERY_DEADLINE_MS",
    "ReadinessError",
    "RECENT_RUN_LIMIT",
    "TOOL_VERSION",
    "UNAVAILABLE",
    "assess_capacity_budget",
    "assess_runtime_checkpoint_evidence",
    "assert_sql_is_read_only",
    "collect_filesystem_observation",
    "collect_runtime_checkpoint_preflight",
    "collect_sqlite_metadata",
    "collect_volume_facts",
    "collector_identity",
    "load_json_object",
    "write_report_exclusive",
]
