from __future__ import annotations

import sqlite3
import time
from pathlib import Path

DEFAULT_DATABASE_PATH = Path("scan_runs") / "candle_craft.db"
SCHEMA_VERSION = 16
WRITABLE_BUSY_TIMEOUT_MS = 5_000
WRITABLE_JOURNAL_MODE = "wal"
WRITABLE_SYNCHRONOUS = "FULL"
WRITABLE_SYNCHRONOUS_LEVEL = 2
WRITABLE_WAL_AUTOCHECKPOINT_PAGES = 1_000
WAL_INITIALIZATION_LOCK_SLICE_MS = 250
WAL_INITIALIZATION_RETRY_INTERVAL_SECONDS = 0.025


class StorageError(RuntimeError):
    """Raised when local scan history cannot be read or written cleanly."""


class UnsupportedSchemaVersionError(StorageError):
    """Raised when a database was created by a newer unsupported runtime."""


class DatabaseMissingError(StorageError):
    """Raised when read-only access is requested for a missing database."""


class ManagedSQLiteConnection(sqlite3.Connection):
    """SQLite connection whose context manager also releases the file handle."""

    read_only_safety_proof: dict[str, str | int | bool] | None = None

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object,
    ) -> bool:
        try:
            return bool(super().__exit__(exc_type, exc_value, traceback))
        finally:
            self.close()


def connect_database(
    path: Path | str = DEFAULT_DATABASE_PATH,
    *,
    busy_timeout_ms: int = WRITABLE_BUSY_TIMEOUT_MS,
) -> sqlite3.Connection:
    """Open a writable runtime connection using the verified SQLite safety profile."""

    database_path = Path(path)
    connection: sqlite3.Connection | None = None
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        timeout_ms = max(1, int(busy_timeout_ms))
        connection = sqlite3.connect(
            database_path,
            timeout=timeout_ms / 1_000,
            factory=ManagedSQLiteConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")

        _ensure_writable_journal_mode(
            connection,
            database_path,
            timeout_ms=timeout_ms,
        )

        connection.execute(f"PRAGMA synchronous = {WRITABLE_SYNCHRONOUS}")
        connection.execute(
            f"PRAGMA wal_autocheckpoint = {WRITABLE_WAL_AUTOCHECKPOINT_PAGES}"
        )
        _verify_writable_profile(connection, database_path, timeout_ms=timeout_ms)
        return connection
    except StorageError:
        if connection is not None:
            connection.close()
        raise
    except sqlite3.Error as exc:
        if connection is not None:
            connection.close()
        raise StorageError(f"Unable to open scan history database: {database_path}") from exc
    except OSError as exc:
        if connection is not None:
            connection.close()
        raise StorageError(f"Unable to prepare scan history database directory: {database_path}") from exc


def open_read_only_database(
    path: Path | str,
    *,
    require_supported_schema: bool = True,
    busy_timeout_ms: int = WRITABLE_BUSY_TIMEOUT_MS,
    assume_immutable_when_sidecars_absent: bool = True,
    require_consistent_snapshot: bool = False,
    require_immutable_source: bool = False,
    include_immutable_safety_proof: bool = False,
) -> sqlite3.Connection:
    """Open an existing SQLite database without creating, migrating, or changing it.

    ``assume_immutable_when_sidecars_absent`` deliberately retains the historic
    inspection default for existing callers. Audits of a live mutable source
    must pass ``False``: absent WAL sidecars do not prove that a writer will not
    resume later. ``require_consistent_snapshot`` starts and proves a bounded
    read transaction after ``query_only`` has been verified.
    ``require_immutable_source`` is a fail-closed opt-in for a separately
    verified quiescent source: it refuses existing WAL/SHM sidecars before
    connecting and requires ``immutable=1``. Opt in to
    ``include_immutable_safety_proof`` when the caller must report the exact
    immutable setting without changing established default proof payloads.
    """

    database_path = Path(path)
    connection: sqlite3.Connection | None = None
    try:
        if not database_path.exists():
            raise DatabaseMissingError(f"Database does not exist: {database_path}")
        if not database_path.is_file():
            raise StorageError(f"Database path is not a file: {database_path}")
        resolved_path = database_path.resolve(strict=True)
        wal_path = Path(f"{resolved_path}-wal")
        shm_path = Path(f"{resolved_path}-shm")
        if wal_path.exists() and not shm_path.exists():
            raise StorageError(
                "Read-only WAL inspection requires the existing -shm sidecar; "
                f"refusing to create it for {resolved_path}."
            )
        if require_immutable_source:
            if not assume_immutable_when_sidecars_absent:
                raise StorageError(
                    "A required immutable source cannot disable the immutable assumption."
                )
            if wal_path.exists() or shm_path.exists():
                raise StorageError(
                    "A required immutable source must have no existing SQLite -wal or -shm sidecars; "
                    f"refusing to connect to {resolved_path}."
                )
        uri_options = "mode=ro"
        immutable_requested = (
            require_immutable_source
            or (assume_immutable_when_sidecars_absent and not wal_path.exists() and not shm_path.exists())
        )
        if immutable_requested:
            uri_options += "&immutable=1"
        timeout_ms = max(1, int(busy_timeout_ms))
        read_only_uri = f"{resolved_path.as_uri()}?{uri_options}"
        connection = sqlite3.connect(
            # URI mode=ro is required for the dry-run inspection connection.
            read_only_uri,
            uri=True,
            timeout=timeout_ms / 1_000,
            factory=ManagedSQLiteConnection,
        )
        query_only_readback = _enable_and_verify_query_only(connection)
        connection.read_only_safety_proof = {
            "sqlite_uri_mode": "ro",
            "query_only_readback": query_only_readback,
            "query_only_verified": True,
        }
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")
        if require_consistent_snapshot:
            _begin_and_verify_read_snapshot(connection)
            connection.read_only_safety_proof.update(
                {
                    "immutable_requested": immutable_requested,
                    "live_mutable_source": not immutable_requested,
                    "consistent_read_snapshot": "transaction_read_snapshot",
                    "busy_timeout_ms": timeout_ms,
                }
            )
        elif not assume_immutable_when_sidecars_absent:
            connection.read_only_safety_proof.update(
                {
                    "immutable_requested": False,
                    "live_mutable_source": True,
                    "busy_timeout_ms": timeout_ms,
                }
            )
        elif include_immutable_safety_proof or require_immutable_source:
            connection.read_only_safety_proof.update(
                {
                    "immutable_requested": immutable_requested,
                    "live_mutable_source": not immutable_requested,
                    "busy_timeout_ms": timeout_ms,
                }
            )
        # query_only was set and verified before any other connection configuration.
        schema_version = identify_schema_version(connection)
        if require_supported_schema and schema_version > SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"Unsupported database schema version {schema_version}; "
                f"this runtime supports up to version {SCHEMA_VERSION}."
            )
        return connection
    except StorageError:
        if connection is not None:
            connection.close()
        raise
    except (OSError, sqlite3.Error) as exc:
        if connection is not None:
            connection.close()
        raise StorageError(f"Unable to open database read-only: {database_path}") from exc


def read_only_connection_safety_proof(connection: sqlite3.Connection) -> dict[str, str | int | bool]:
    """Return the verified non-persistent safety settings for a read-only connection."""

    proof = getattr(connection, "read_only_safety_proof", None)
    if not isinstance(proof, dict):
        raise StorageError("Read-only safety proof is unavailable; refusing to continue.")
    if proof.get("sqlite_uri_mode") != "ro":
        raise StorageError("Read-only safety proof lacks SQLite URI mode=ro; refusing to continue.")
    if proof.get("query_only_verified") is not True or proof.get("query_only_readback") != 1:
        raise StorageError("Read-only safety proof lacks verified PRAGMA query_only=1; refusing to continue.")
    return dict(proof)


def _enable_and_verify_query_only(connection: sqlite3.Connection) -> int:
    """Enable SQLite query-only mode and fail closed unless its exact value is one."""

    try:
        connection.execute("PRAGMA query_only = ON")
        row = connection.execute("PRAGMA query_only").fetchone()
    except (AttributeError, IndexError, TypeError, sqlite3.Error) as exc:
        raise StorageError(
            "Read-only safety proof failed: PRAGMA query_only readback was unavailable."
        ) from exc

    if row is None:
        raise StorageError("Read-only safety proof failed: PRAGMA query_only returned no value.")
    try:
        if len(row) != 1:
            raise ValueError("unexpected number of query_only columns")
        query_only = row[0]
    except (AttributeError, IndexError, KeyError, TypeError, ValueError) as exc:
        raise StorageError("Read-only safety proof failed: PRAGMA query_only returned malformed data.") from exc
    if type(query_only) is not int or query_only != 1:
        raise StorageError(
            "Read-only safety proof failed: PRAGMA query_only must read back exactly 1, "
            f"got {query_only!r}."
        )
    return query_only


def _begin_and_verify_read_snapshot(connection: sqlite3.Connection) -> None:
    """Pin a read snapshot or fail closed without attempting a writable fallback."""

    try:
        connection.execute("BEGIN")
        # BEGIN is deferred. This harmless catalog read establishes the actual
        # snapshot while query_only remains enabled.
        connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        raise StorageError(
            "Unable to establish a bounded, consistent read snapshot safely; "
            "the database may be locked or use an unsupported live journal state."
        ) from exc

def identify_schema_version(connection: sqlite3.Connection) -> int:
    """Read the SQLite application schema version without changing it."""

    try:
        row = connection.execute("PRAGMA user_version").fetchone()
        if row is None:
            raise StorageError("SQLite did not return a schema version.")
        return int(row[0])
    except StorageError:
        raise
    except (TypeError, ValueError, sqlite3.Error) as exc:
        raise StorageError("Unable to identify database schema version.") from exc


def _pragma_text(connection: sqlite3.Connection, expression: str) -> str:
    row = connection.execute(f"PRAGMA {expression}").fetchone()
    if row is None or row[0] is None:
        return ""
    return str(row[0]).strip().lower()


def _ensure_writable_journal_mode(
    connection: sqlite3.Connection,
    database_path: Path,
    *,
    timeout_ms: int,
) -> None:
    """Enable WAL with bounded retries when concurrent first-openers hold SQLite locks."""

    deadline = time.monotonic() + (timeout_ms / 1_000)
    lock_slice_ms = min(timeout_ms, WAL_INITIALIZATION_LOCK_SLICE_MS)
    connection.execute(f"PRAGMA busy_timeout = {lock_slice_ms}")
    try:
        while True:
            try:
                current_journal_mode = _pragma_text(connection, "journal_mode")
                if current_journal_mode != WRITABLE_JOURNAL_MODE:
                    requested_journal_mode = _pragma_text(
                        connection,
                        f"journal_mode = {WRITABLE_JOURNAL_MODE.upper()}",
                    )
                    if requested_journal_mode != WRITABLE_JOURNAL_MODE:
                        raise StorageError(
                            "SQLite refused required WAL journal mode for writable database "
                            f"{database_path}: returned "
                            f"{requested_journal_mode or 'no value'}."
                        )

                verified_journal_mode = _pragma_text(connection, "journal_mode")
                if verified_journal_mode != WRITABLE_JOURNAL_MODE:
                    raise StorageError(
                        "Writable database did not remain in required WAL journal mode "
                        f"for {database_path}: returned "
                        f"{verified_journal_mode or 'no value'}."
                    )
                return
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_lock_error(exc):
                    raise
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    raise StorageError(
                        "Timed out while enabling required WAL journal mode for writable "
                        f"database {database_path} after {timeout_ms} ms because another "
                        "SQLite connection retained the initialization lock."
                    ) from exc
                time.sleep(
                    min(
                        WAL_INITIALIZATION_RETRY_INTERVAL_SECONDS,
                        remaining_seconds,
                    )
                )
    finally:
        connection.execute(f"PRAGMA busy_timeout = {timeout_ms}")


def _is_sqlite_lock_error(exc: sqlite3.OperationalError) -> bool:
    error_code = getattr(exc, "sqlite_errorcode", None)
    if error_code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        return True
    message = str(exc).strip().lower()
    return "locked" in message or "busy" in message


def _verify_writable_profile(
    connection: sqlite3.Connection,
    database_path: Path,
    *,
    timeout_ms: int,
) -> None:
    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    actual_timeout_ms = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
    synchronous = int(connection.execute("PRAGMA synchronous").fetchone()[0])
    wal_autocheckpoint = int(
        connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0]
    )
    if foreign_keys != 1:
        raise StorageError(
            f"SQLite foreign key enforcement is unavailable for {database_path}."
        )
    if actual_timeout_ms != timeout_ms:
        raise StorageError(
            f"SQLite busy timeout verification failed for {database_path}: "
            f"expected {timeout_ms} ms, got {actual_timeout_ms} ms."
        )
    if synchronous != WRITABLE_SYNCHRONOUS_LEVEL:
        raise StorageError(
            f"SQLite synchronous policy verification failed for {database_path}: "
            f"expected {WRITABLE_SYNCHRONOUS}, got level {synchronous}."
        )
    if wal_autocheckpoint != WRITABLE_WAL_AUTOCHECKPOINT_PAGES:
        raise StorageError(
            f"SQLite WAL auto-checkpoint verification failed for {database_path}: "
            f"expected {WRITABLE_WAL_AUTOCHECKPOINT_PAGES} pages, got {wal_autocheckpoint}."
        )


def initialize_database(connection: sqlite3.Connection) -> None:
    try:
        if connection.in_transaction:
            connection.commit()
        existing_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        if existing_version > SCHEMA_VERSION:
            raise UnsupportedSchemaVersionError(
                f"Unsupported database schema version {existing_version}; "
                f"this runtime supports up to version {SCHEMA_VERSION}."
            )
        connection.executescript(
            """
            BEGIN IMMEDIATE;

            CREATE TABLE IF NOT EXISTS scan_runs (
                run_id TEXT PRIMARY KEY,
                timestamp TEXT NOT NULL,
                exchange TEXT NOT NULL,
                universe TEXT NOT NULL,
                symbols_scanned INTEGER NOT NULL,
                symbols_json TEXT NOT NULL,
                strategy TEXT NOT NULL,
                timeframes_json TEXT NOT NULL,
                market_regime TEXT NOT NULL,
                regime_confidence INTEGER NOT NULL DEFAULT 0,
                regime_compatibility_json TEXT NOT NULL DEFAULT '{}',
                environment_notes_json TEXT NOT NULL DEFAULT '[]',
                runtime_stats_json TEXT NOT NULL,
                command_preset TEXT NOT NULL,
                command_used TEXT NOT NULL,
                total_valid_setups INTEGER NOT NULL,
                near_misses INTEGER NOT NULL,
                rejected INTEGER NOT NULL,
                data_issues INTEGER NOT NULL,
                data_issues_json TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL,
                is_watch_iteration INTEGER NOT NULL DEFAULT 0,
                watch_iteration_number INTEGER,
                started_at TEXT,
                completed_at TEXT,
                symbols_requested INTEGER NOT NULL DEFAULT 0,
                symbols_queued INTEGER NOT NULL DEFAULT 0,
                symbols_completed INTEGER NOT NULL DEFAULT 0,
                valid_activations INTEGER NOT NULL DEFAULT 0,
                still_watching INTEGER NOT NULL DEFAULT 0,
                rejected_no_edge INTEGER NOT NULL DEFAULT 0,
                runtime_sec REAL,
                portfolio_summary_json TEXT NOT NULL DEFAULT '{}',
                symbol_health_summary_json TEXT NOT NULL DEFAULT '{}',
                actionable_setups INTEGER NOT NULL DEFAULT 0,
                actionable_a_grade_setups INTEGER NOT NULL DEFAULT 0,
                actionable_a_grade_target_caution INTEGER NOT NULL DEFAULT 0,
                confirmed_setups INTEGER NOT NULL DEFAULT 0,
                candidate_a_grade_setups INTEGER NOT NULL DEFAULT 0,
                blocked_a_grade_by_scoring INTEGER NOT NULL DEFAULT 0,
                blocked_a_grade_by_target INTEGER NOT NULL DEFAULT 0,
                blocked_a_grade_by_entry_window INTEGER NOT NULL DEFAULT 0,
                blocked_a_grade_by_trust INTEGER NOT NULL DEFAULT 0,
                fatal_target_blocks INTEGER NOT NULL DEFAULT 0,
                soft_target_warnings INTEGER NOT NULL DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS symbol_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES scan_runs(run_id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                status TEXT NOT NULL,
                display_bucket TEXT NOT NULL,
                readiness_score INTEGER NOT NULL,
                setup_quality_score TEXT NOT NULL,
                edge_score TEXT NOT NULL,
                failed_gate TEXT NOT NULL,
                rejection_reason TEXT NOT NULL,
                next_trigger_needed TEXT NOT NULL,
                action_label TEXT NOT NULL,
                regime_state TEXT NOT NULL,
                regime_confidence TEXT NOT NULL DEFAULT 'N/A',
                regime_compatibility_score TEXT NOT NULL DEFAULT 'N/A',
                regime_compatibility_label TEXT NOT NULL DEFAULT 'N/A',
                regime_penalty INTEGER NOT NULL DEFAULT 0,
                environment_notes_json TEXT NOT NULL DEFAULT '[]',
                derivatives_context_json TEXT NOT NULL,
                volume_profile_context_json TEXT NOT NULL,
                pullback_status TEXT NOT NULL,
                portfolio_decision TEXT NOT NULL,
                raw_result_json TEXT NOT NULL,
                UNIQUE(run_id, symbol)
            );

            CREATE TABLE IF NOT EXISTS setup_candidates (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES scan_runs(run_id) ON DELETE CASCADE,
                symbol TEXT NOT NULL,
                mode TEXT NOT NULL,
                direction TEXT NOT NULL,
                entry TEXT NOT NULL,
                stop TEXT NOT NULL,
                tp1 TEXT NOT NULL,
                tp2 TEXT NOT NULL,
                tp3 TEXT NOT NULL,
                rr TEXT NOT NULL,
                invalidation TEXT NOT NULL,
                quality_grade TEXT NOT NULL,
                candidate_quality_grade TEXT NOT NULL DEFAULT 'N/A',
                final_quality_grade TEXT NOT NULL DEFAULT 'N/A',
                technical_score TEXT NOT NULL DEFAULT 'N/A',
                opportunity_score TEXT NOT NULL DEFAULT 'N/A',
                failed_gate TEXT NOT NULL DEFAULT 'N/A',
                final_failed_gate TEXT NOT NULL DEFAULT 'N/A',
                final_block_reason TEXT NOT NULL DEFAULT 'N/A',
                target_integrity_status TEXT NOT NULL DEFAULT 'N/A',
                target_failure TEXT NOT NULL DEFAULT 'N/A',
                target_failure_severity TEXT NOT NULL DEFAULT 'N/A',
                target_warning_reason TEXT NOT NULL DEFAULT 'N/A',
                actionability_state TEXT NOT NULL DEFAULT 'N/A',
                trust_meter TEXT NOT NULL,
                risk_warning TEXT NOT NULL,
                raw_candidate_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS replay_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                run_id TEXT NOT NULL REFERENCES scan_runs(run_id) ON DELETE CASCADE,
                setup_fingerprint TEXT NOT NULL,
                outcome TEXT NOT NULL,
                filled INTEGER NOT NULL,
                tp_hit TEXT NOT NULL,
                sl_hit INTEGER NOT NULL,
                final_r TEXT NOT NULL,
                time_in_trade TEXT NOT NULL,
                regime TEXT NOT NULL,
                symbol TEXT NOT NULL,
                mode TEXT NOT NULL,
                raw_result_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS ix_symbol_results_run_id ON symbol_results(run_id);
            CREATE INDEX IF NOT EXISTS ix_setup_candidates_run_id ON setup_candidates(run_id);
            CREATE INDEX IF NOT EXISTS ix_replay_results_run_id ON replay_results(run_id);
            CREATE INDEX IF NOT EXISTS ix_scan_runs_timestamp ON scan_runs(timestamp);

            CREATE TABLE IF NOT EXISTS setup_lifecycle_records (
                lifecycle_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                mode TEXT NOT NULL,
                direction TEXT NOT NULL,
                current_state TEXT NOT NULL,
                previous_state TEXT NOT NULL DEFAULT 'N/A',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                last_transition_at TEXT NOT NULL,
                failed_gate TEXT NOT NULL DEFAULT 'N/A',
                candidate_quality_grade TEXT NOT NULL DEFAULT 'N/A',
                final_quality_grade TEXT NOT NULL DEFAULT 'N/A',
                technical_score TEXT NOT NULL DEFAULT 'N/A',
                opportunity_score TEXT NOT NULL DEFAULT 'N/A',
                final_failed_gate TEXT NOT NULL DEFAULT 'N/A',
                final_block_reason TEXT NOT NULL DEFAULT 'N/A',
                target_integrity_status TEXT NOT NULL DEFAULT 'N/A',
                target_failure TEXT NOT NULL DEFAULT 'N/A',
                target_failure_severity TEXT NOT NULL DEFAULT 'N/A',
                target_warning_reason TEXT NOT NULL DEFAULT 'N/A',
                actionability_state TEXT NOT NULL DEFAULT 'N/A',
                readiness_score INTEGER NOT NULL DEFAULT 0,
                quality_score INTEGER NOT NULL DEFAULT 0,
                edge_score TEXT NOT NULL DEFAULT 'N/A',
                regime_state TEXT NOT NULL DEFAULT 'N/A',
                action_label TEXT NOT NULL DEFAULT 'N/A',
                invalidation_reason TEXT NOT NULL DEFAULT 'N/A',
                cooldown_until TEXT,
                archived_at TEXT,
                entry_low TEXT NOT NULL DEFAULT 'N/A',
                entry_high TEXT NOT NULL DEFAULT 'N/A',
                stop_loss TEXT NOT NULL DEFAULT 'N/A',
                tp1 TEXT NOT NULL DEFAULT 'N/A',
                tp2 TEXT NOT NULL DEFAULT 'N/A',
                tp3 TEXT NOT NULL DEFAULT 'N/A',
                rr TEXT NOT NULL DEFAULT 'N/A',
                invalidation_logic TEXT NOT NULL DEFAULT 'N/A',
                confirmation_count INTEGER NOT NULL DEFAULT 0,
                required_confirmation_cycles INTEGER NOT NULL DEFAULT 2,
                quality_grade_first_seen TEXT NOT NULL DEFAULT 'N/A',
                quality_grade_current TEXT NOT NULL DEFAULT 'N/A',
                quality_grade_confirmed TEXT NOT NULL DEFAULT 'N/A',
                confirmed_at TEXT,
                decay_count INTEGER NOT NULL DEFAULT 0,
                decay_reason TEXT NOT NULL DEFAULT 'N/A',
                symbol_health_score_at_detection TEXT NOT NULL DEFAULT 'N/A',
                symbol_health_penalty_cycles INTEGER NOT NULL DEFAULT 0,
                setup_identity TEXT NOT NULL DEFAULT 'N/A',
                UNIQUE(symbol, mode, direction)
            );

            CREATE TABLE IF NOT EXISTS setup_lifecycle_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                lifecycle_id TEXT NOT NULL REFERENCES setup_lifecycle_records(lifecycle_id) ON DELETE CASCADE,
                timestamp TEXT NOT NULL,
                symbol TEXT NOT NULL,
                from_state TEXT NOT NULL DEFAULT 'N/A',
                to_state TEXT NOT NULL,
                reason TEXT NOT NULL,
                scan_run_id TEXT,
                readiness_score INTEGER NOT NULL DEFAULT 0,
                quality_score INTEGER NOT NULL DEFAULT 0,
                failed_gate TEXT NOT NULL DEFAULT 'N/A',
                notes TEXT NOT NULL DEFAULT 'N/A'
            );

            CREATE INDEX IF NOT EXISTS ix_lifecycle_records_symbol_mode_direction
                ON setup_lifecycle_records(symbol, mode, direction);
            CREATE INDEX IF NOT EXISTS ix_lifecycle_events_lifecycle_id
                ON setup_lifecycle_events(lifecycle_id);
            CREATE INDEX IF NOT EXISTS ix_lifecycle_events_symbol_timestamp
                ON setup_lifecycle_events(symbol, timestamp);

            CREATE TABLE IF NOT EXISTS setup_lifecycle_outcome_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lifecycle_id TEXT NOT NULL REFERENCES setup_lifecycle_records(lifecycle_id) ON DELETE CASCADE,
                plan_identity TEXT NOT NULL,
                symbol TEXT NOT NULL,
                mode TEXT NOT NULL DEFAULT 'N/A',
                direction TEXT NOT NULL DEFAULT 'N/A',
                execution_timeframe TEXT NOT NULL DEFAULT 'N/A',
                evaluation_cursor_open_at TEXT,
                evaluation_cursor_close_at TEXT,
                entry_at TEXT,
                tp1_at TEXT,
                tp2_at TEXT,
                tp3_at TEXT,
                stop_at TEXT,
                invalidated_at TEXT,
                outcome_at TEXT,
                terminal_outcome TEXT NOT NULL DEFAULT 'N/A',
                integrity_status TEXT NOT NULL DEFAULT 'N/A',
                diagnostic TEXT NOT NULL DEFAULT 'N/A',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                first_evaluated_at TEXT NOT NULL,
                last_evaluated_at TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(lifecycle_id, plan_identity)
            );

            CREATE INDEX IF NOT EXISTS ix_lifecycle_outcome_progress_active_plan
                ON setup_lifecycle_outcome_progress(lifecycle_id, plan_identity);
            CREATE INDEX IF NOT EXISTS ix_lifecycle_outcome_progress_symbol_outcome
                ON setup_lifecycle_outcome_progress(symbol, terminal_outcome);

            CREATE TABLE IF NOT EXISTS setup_outcome_analytics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                lifecycle_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                bias TEXT NOT NULL DEFAULT 'N/A',
                first_seen_at TEXT NOT NULL,
                confirmed_at TEXT NOT NULL DEFAULT 'N/A',
                entry_zone TEXT NOT NULL DEFAULT 'N/A',
                stop_loss TEXT NOT NULL DEFAULT 'N/A',
                tp1 TEXT NOT NULL DEFAULT 'N/A',
                tp2 TEXT NOT NULL DEFAULT 'N/A',
                tp3 TEXT NOT NULL DEFAULT 'N/A',
                quality_at_first_detection TEXT NOT NULL DEFAULT 'N/A',
                quality_at_confirmation TEXT NOT NULL DEFAULT 'N/A',
                rr TEXT NOT NULL DEFAULT 'N/A',
                lifecycle_path TEXT NOT NULL DEFAULT 'N/A',
                final_outcome TEXT NOT NULL,
                failure_reason TEXT NOT NULL DEFAULT 'N/A',
                outcome_reason TEXT NOT NULL DEFAULT 'N/A',
                regime_context TEXT NOT NULL DEFAULT 'N/A',
                symbol_health_at_detection TEXT NOT NULL DEFAULT 'N/A',
                raw_payload_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(lifecycle_id, final_outcome)
            );

            CREATE INDEX IF NOT EXISTS ix_setup_outcome_analytics_symbol
                ON setup_outcome_analytics(symbol, final_outcome);

            CREATE TABLE IF NOT EXISTS telegram_alert_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                signal_id TEXT NOT NULL,
                symbol TEXT NOT NULL,
                direction TEXT NOT NULL,
                previous_state TEXT NOT NULL DEFAULT 'N/A',
                new_state TEXT NOT NULL,
                alert_type TEXT NOT NULL,
                lifecycle_state TEXT NOT NULL,
                sent_at TEXT,
                attempted_at TEXT NOT NULL DEFAULT 'N/A',
                telegram_status TEXT NOT NULL,
                message_hash TEXT NOT NULL,
                scan_run_id TEXT,
                attempted_alert_type TEXT NOT NULL DEFAULT 'N/A',
                setup_quality_score TEXT NOT NULL DEFAULT 'N/A',
                rr_planned TEXT NOT NULL DEFAULT 'N/A',
                min_rr TEXT NOT NULL DEFAULT 'N/A',
                opportunity_score TEXT NOT NULL DEFAULT 'N/A',
                min_score_for_idea TEXT NOT NULL DEFAULT 'N/A',
                technical_score TEXT NOT NULL DEFAULT 'N/A',
                price_level TEXT NOT NULL DEFAULT 'N/A',
                entry_low TEXT NOT NULL DEFAULT 'N/A',
                entry_high TEXT NOT NULL DEFAULT 'N/A',
                stop_loss TEXT NOT NULL DEFAULT 'N/A',
                tp1 TEXT NOT NULL DEFAULT 'N/A',
                tp2 TEXT NOT NULL DEFAULT 'N/A',
                tp3 TEXT NOT NULL DEFAULT 'N/A',
                blocked_reason TEXT NOT NULL DEFAULT 'N/A',
                invalid_target_fields TEXT NOT NULL DEFAULT 'N/A',
                error_message TEXT NOT NULL DEFAULT 'N/A',
                first_seen_at TEXT NOT NULL DEFAULT 'N/A',
                last_seen_at TEXT NOT NULL DEFAULT 'N/A',
                seen_count INTEGER NOT NULL DEFAULT 1,
                last_scan_run_id TEXT,
                last_error_message TEXT NOT NULL DEFAULT 'N/A',
                public_watchlist_plan_id TEXT NOT NULL DEFAULT 'N/A',
                public_watchlist_event_key TEXT NOT NULL DEFAULT 'N/A',
                public_alert_event_type TEXT NOT NULL DEFAULT 'N/A',
                normalized_entry_zone_low TEXT NOT NULL DEFAULT 'N/A',
                normalized_entry_zone_high TEXT NOT NULL DEFAULT 'N/A',
                normalized_invalidation TEXT NOT NULL DEFAULT 'N/A',
                dedupe_status TEXT NOT NULL DEFAULT 'N/A',
                dedupe_reason TEXT NOT NULL DEFAULT 'N/A',
                UNIQUE(signal_id, alert_type)
            );

            CREATE INDEX IF NOT EXISTS ix_telegram_alert_attempts_signal
                ON telegram_alert_attempts(signal_id, alert_type);
            CREATE INDEX IF NOT EXISTS ix_telegram_alert_attempts_scan_run
                ON telegram_alert_attempts(scan_run_id);

            CREATE TABLE IF NOT EXISTS public_alert_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                canonical_plan_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                event_key TEXT NOT NULL,
                symbol TEXT NOT NULL,
                side TEXT NOT NULL,
                setup_family TEXT NOT NULL DEFAULT 'N/A',
                normalized_zone_low TEXT NOT NULL DEFAULT 'N/A',
                normalized_zone_high TEXT NOT NULL DEFAULT 'N/A',
                normalized_invalidation TEXT NOT NULL DEFAULT 'N/A',
                raw_entry_low TEXT NOT NULL DEFAULT 'N/A',
                raw_entry_high TEXT NOT NULL DEFAULT 'N/A',
                raw_stop_loss TEXT NOT NULL DEFAULT 'N/A',
                status TEXT NOT NULL,
                reserved_at TEXT,
                sent_at TEXT,
                source_modes TEXT NOT NULL DEFAULT 'N/A',
                matched_prior_alert_id INTEGER,
                matched_prior_event_id INTEGER,
                failure_reason TEXT NOT NULL DEFAULT 'N/A',
                delivery_state TEXT NOT NULL DEFAULT 'PENDING',
                payload_text TEXT NOT NULL DEFAULT 'N/A',
                message_hash TEXT NOT NULL DEFAULT 'N/A',
                destination_chat_id TEXT NOT NULL DEFAULT 'N/A',
                destination_kind TEXT NOT NULL DEFAULT 'N/A',
                attempt_id TEXT,
                claim_owner TEXT,
                claimed_at TEXT,
                attempt_started_at TEXT,
                lease_expires_at TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 3,
                next_retry_at TEXT,
                last_error_category TEXT NOT NULL DEFAULT 'N/A',
                last_error_detail TEXT NOT NULL DEFAULT 'N/A',
                telegram_message_id TEXT,
                telegram_chat_id TEXT,
                part_count INTEGER NOT NULL DEFAULT 1,
                completed_at TEXT,
                uncertain_at TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(event_key)
            );

            CREATE INDEX IF NOT EXISTS ix_public_alert_events_symbol_side_setup
                ON public_alert_events(symbol, side, setup_family, event_type, status);
            CREATE INDEX IF NOT EXISTS ix_public_alert_events_status
                ON public_alert_events(status);

            CREATE TABLE IF NOT EXISTS public_alert_delivery_parts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                public_alert_event_id INTEGER NOT NULL REFERENCES public_alert_events(id) ON DELETE CASCADE,
                event_key TEXT NOT NULL,
                part_index INTEGER NOT NULL,
                part_count INTEGER NOT NULL,
                payload_text TEXT NOT NULL,
                payload_hash TEXT NOT NULL,
                delivery_state TEXT NOT NULL DEFAULT 'PENDING',
                attempt_id TEXT,
                attempt_count INTEGER NOT NULL DEFAULT 0,
                attempt_started_at TEXT,
                sent_at TEXT,
                telegram_message_id TEXT,
                telegram_chat_id TEXT,
                http_status INTEGER,
                retry_after_seconds REAL,
                next_retry_at TEXT,
                last_error_category TEXT NOT NULL DEFAULT 'N/A',
                last_error_detail TEXT NOT NULL DEFAULT 'N/A',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(public_alert_event_id, part_index)
            );

            CREATE INDEX IF NOT EXISTS ix_public_alert_delivery_parts_state
                ON public_alert_delivery_parts(delivery_state, next_retry_at);
            CREATE INDEX IF NOT EXISTS ix_public_alert_delivery_parts_event
                ON public_alert_delivery_parts(public_alert_event_id, part_index);

            CREATE TABLE IF NOT EXISTS symbol_health (
                symbol TEXT PRIMARY KEY,
                successful_scans INTEGER NOT NULL DEFAULT 0,
                timeout_count INTEGER NOT NULL DEFAULT 0,
                data_issue_count INTEGER NOT NULL DEFAULT 0,
                average_runtime_sec REAL NOT NULL DEFAULT 0,
                last_success_at TEXT,
                last_timeout_at TEXT,
                current_health_score INTEGER NOT NULL DEFAULT 70,
                cooldown_until TEXT,
                timeout_strikes INTEGER NOT NULL DEFAULT 0,
                last_priority_rank INTEGER,
                last_prioritized_at TEXT,
                last_scanned_at TEXT,
                last_data_issue_at TEXT,
                last_display_bucket TEXT NOT NULL DEFAULT 'N/A',
                last_readiness_label TEXT NOT NULL DEFAULT 'N/A',
                useful_scan_count INTEGER NOT NULL DEFAULT 0,
                rejected_count INTEGER NOT NULL DEFAULT 0,
                last_rejected_at TEXT,
                invalidation_count INTEGER NOT NULL DEFAULT 0,
                expired_setup_count INTEGER NOT NULL DEFAULT 0,
                rejected_setup_count INTEGER NOT NULL DEFAULT 0,
                false_confirmation_count INTEGER NOT NULL DEFAULT 0,
                malformed_setup_event_count INTEGER NOT NULL DEFAULT 0,
                stop_breach_after_confirmation_count INTEGER NOT NULL DEFAULT 0,
                duplicate_noisy_setup_count INTEGER NOT NULL DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS ix_symbol_health_score
                ON symbol_health(current_health_score DESC);
            CREATE INDEX IF NOT EXISTS ix_symbol_health_cooldown
                ON symbol_health(cooldown_until);

            CREATE TABLE IF NOT EXISTS symbol_health_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol TEXT NOT NULL,
                event_type TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'scanner_lifecycle',
                occurred_at TEXT NOT NULL,
                scan_run_id TEXT,
                lifecycle_id TEXT,
                details_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE INDEX IF NOT EXISTS ix_symbol_health_events_symbol_type
                ON symbol_health_events(symbol, event_type, occurred_at);
            """
        )
        _ensure_column(connection, "scan_runs", "regime_confidence", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "regime_compatibility_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(connection, "scan_runs", "environment_notes_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(connection, "scan_runs", "is_watch_iteration", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "watch_iteration_number", "INTEGER")
        _ensure_column(connection, "scan_runs", "started_at", "TEXT")
        _ensure_column(connection, "scan_runs", "completed_at", "TEXT")
        _ensure_column(connection, "scan_runs", "symbols_requested", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "symbols_queued", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "symbols_completed", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "valid_activations", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "still_watching", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "rejected_no_edge", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "runtime_sec", "REAL")
        _ensure_column(connection, "scan_runs", "portfolio_summary_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(connection, "scan_runs", "symbol_health_summary_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(connection, "scan_runs", "actionable_setups", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "actionable_a_grade_setups", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "actionable_a_grade_target_caution", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "confirmed_setups", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "candidate_a_grade_setups", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "blocked_a_grade_by_scoring", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "blocked_a_grade_by_target", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "blocked_a_grade_by_entry_window", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "blocked_a_grade_by_trust", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "fatal_target_blocks", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "soft_target_warnings", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "symbol_results", "regime_confidence", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "symbol_results", "regime_compatibility_score", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "symbol_results", "regime_compatibility_label", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "symbol_results", "regime_penalty", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "symbol_results", "environment_notes_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(connection, "setup_candidates", "candidate_quality_grade", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_candidates", "final_quality_grade", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_candidates", "technical_score", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_candidates", "opportunity_score", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_candidates", "failed_gate", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_candidates", "final_failed_gate", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_candidates", "final_block_reason", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_candidates", "target_integrity_status", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_candidates", "target_failure", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_candidates", "target_failure_severity", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_candidates", "target_warning_reason", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_candidates", "actionability_state", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "cooldown_until", "TEXT")
        _ensure_column(connection, "setup_lifecycle_records", "archived_at", "TEXT")
        _ensure_column(connection, "setup_lifecycle_records", "candidate_quality_grade", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "final_quality_grade", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "technical_score", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "opportunity_score", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "final_failed_gate", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "final_block_reason", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "target_integrity_status", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "target_failure", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "target_failure_severity", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "target_warning_reason", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "actionability_state", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "entry_low", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "entry_high", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "stop_loss", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "tp1", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "tp2", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "tp3", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "rr", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "invalidation_logic", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "confirmation_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(
            connection,
            "setup_lifecycle_records",
            "required_confirmation_cycles",
            "INTEGER NOT NULL DEFAULT 2",
        )
        _ensure_column(connection, "setup_lifecycle_records", "quality_grade_first_seen", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "quality_grade_current", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "quality_grade_confirmed", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_records", "confirmed_at", "TEXT")
        _ensure_column(connection, "setup_lifecycle_records", "decay_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "setup_lifecycle_records", "decay_reason", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(
            connection,
            "setup_lifecycle_records",
            "symbol_health_score_at_detection",
            "TEXT NOT NULL DEFAULT 'N/A'",
        )
        _ensure_column(
            connection,
            "setup_lifecycle_records",
            "symbol_health_penalty_cycles",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(connection, "setup_lifecycle_records", "setup_identity", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "setup_lifecycle_events", "scan_run_id", "TEXT")
        _ensure_column(connection, "telegram_alert_attempts", "scan_run_id", "TEXT")
        _ensure_column(connection, "telegram_alert_attempts", "attempted_at", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "attempted_alert_type", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "setup_quality_score", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "rr_planned", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "min_rr", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "opportunity_score", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "min_score_for_idea", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "technical_score", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "price_level", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "entry_low", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "entry_high", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "stop_loss", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "tp1", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "tp2", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "tp3", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "blocked_reason", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "invalid_target_fields", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "error_message", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "first_seen_at", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "last_seen_at", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "seen_count", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(connection, "telegram_alert_attempts", "last_scan_run_id", "TEXT")
        _ensure_column(connection, "telegram_alert_attempts", "last_error_message", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "public_watchlist_plan_id", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "public_watchlist_event_key", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "public_alert_event_type", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "normalized_entry_zone_low", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "normalized_entry_zone_high", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "normalized_invalidation", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "dedupe_status", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "dedupe_reason", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "delivery_state", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "delivery_attempt_id", "TEXT")
        _ensure_column(connection, "telegram_alert_attempts", "delivery_attempt_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "telegram_alert_attempts", "delivery_next_retry_at", "TEXT")
        _ensure_column(connection, "telegram_alert_attempts", "delivery_last_error_category", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "telegram_chat_id", "TEXT")
        _ensure_column(connection, "telegram_alert_attempts", "telegram_message_id", "TEXT")
        _ensure_column(connection, "telegram_alert_attempts", "delivery_part_count", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(connection, "public_alert_events", "delivery_state", "TEXT NOT NULL DEFAULT 'PENDING'")
        _ensure_column(connection, "public_alert_events", "payload_text", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "public_alert_events", "message_hash", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "public_alert_events", "destination_chat_id", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "public_alert_events", "destination_kind", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "public_alert_events", "attempt_id", "TEXT")
        _ensure_column(connection, "public_alert_events", "claim_owner", "TEXT")
        _ensure_column(connection, "public_alert_events", "claimed_at", "TEXT")
        _ensure_column(connection, "public_alert_events", "attempt_started_at", "TEXT")
        _ensure_column(connection, "public_alert_events", "lease_expires_at", "TEXT")
        _ensure_column(connection, "public_alert_events", "attempt_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "public_alert_events", "max_attempts", "INTEGER NOT NULL DEFAULT 3")
        _ensure_column(connection, "public_alert_events", "next_retry_at", "TEXT")
        _ensure_column(connection, "public_alert_events", "last_error_category", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "public_alert_events", "last_error_detail", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "public_alert_events", "telegram_message_id", "TEXT")
        _ensure_column(connection, "public_alert_events", "telegram_chat_id", "TEXT")
        _ensure_column(connection, "public_alert_events", "part_count", "INTEGER NOT NULL DEFAULT 1")
        _ensure_column(connection, "public_alert_events", "completed_at", "TEXT")
        _ensure_column(connection, "public_alert_events", "uncertain_at", "TEXT")
        _migrate_public_alert_delivery_state_v16(connection)
        _ensure_nullable_telegram_sent_at(connection)
        _ensure_column(connection, "telegram_alert_attempts", "delivery_state", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "delivery_attempt_id", "TEXT")
        _ensure_column(connection, "telegram_alert_attempts", "delivery_attempt_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "telegram_alert_attempts", "delivery_next_retry_at", "TEXT")
        _ensure_column(connection, "telegram_alert_attempts", "delivery_last_error_category", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "telegram_alert_attempts", "telegram_chat_id", "TEXT")
        _ensure_column(connection, "telegram_alert_attempts", "telegram_message_id", "TEXT")
        _ensure_column(connection, "telegram_alert_attempts", "delivery_part_count", "INTEGER NOT NULL DEFAULT 1")
        _migrate_public_alert_delivery_state_v16(connection)
        _ensure_telegram_alert_attempt_indexes(connection)
        _ensure_public_alert_event_indexes(connection)
        _ensure_column(connection, "symbol_health", "timeout_strikes", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "symbol_health", "last_priority_rank", "INTEGER")
        _ensure_column(connection, "symbol_health", "last_prioritized_at", "TEXT")
        _ensure_column(connection, "symbol_health", "last_scanned_at", "TEXT")
        _ensure_column(connection, "symbol_health", "last_data_issue_at", "TEXT")
        _ensure_column(connection, "symbol_health", "last_display_bucket", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "symbol_health", "last_readiness_label", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "symbol_health", "useful_scan_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "symbol_health", "rejected_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "symbol_health", "last_rejected_at", "TEXT")
        _ensure_column(connection, "symbol_health", "invalidation_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "symbol_health", "expired_setup_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "symbol_health", "rejected_setup_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "symbol_health", "false_confirmation_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "symbol_health", "malformed_setup_event_count", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(
            connection,
            "symbol_health",
            "stop_breach_after_confirmation_count",
            "INTEGER NOT NULL DEFAULT 0",
        )
        _ensure_column(connection, "symbol_health", "duplicate_noisy_setup_count", "INTEGER NOT NULL DEFAULT 0")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise StorageError("Unable to initialize scan history database schema.") from exc


def open_initialized_database(path: Path | str = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    connection = connect_database(path)
    try:
        initialize_database(connection)
        return connection
    except Exception:
        connection.close()
        raise


def _ensure_column(connection: sqlite3.Connection, table: str, column: str, definition: str) -> None:
    columns = {
        row[1]
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def _ensure_telegram_alert_attempt_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_telegram_alert_attempts_signal
            ON telegram_alert_attempts(signal_id, alert_type)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_telegram_alert_attempts_scan_run
            ON telegram_alert_attempts(scan_run_id)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_telegram_alert_attempts_public_plan
            ON telegram_alert_attempts(public_watchlist_plan_id)
        """
    )
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_telegram_alert_attempts_public_event_sent
            ON telegram_alert_attempts(public_watchlist_event_key)
            WHERE telegram_status = 'sent'
              AND public_watchlist_event_key IS NOT NULL
              AND public_watchlist_event_key NOT IN ('', 'N/A')
        """
    )
    connection.execute("DROP INDEX IF EXISTS ux_telegram_alert_attempts_public_event_active")
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_telegram_alert_attempts_public_event_active
            ON telegram_alert_attempts(public_watchlist_event_key)
            WHERE telegram_status IN ('reserved', 'pending', 'in_flight', 'retryable', 'uncertain', 'sent')
              AND public_watchlist_event_key IS NOT NULL
              AND public_watchlist_event_key NOT IN ('', 'N/A')
        """
    )

def _ensure_public_alert_event_indexes(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_public_alert_events_symbol_side_setup
            ON public_alert_events(symbol, side, setup_family, event_type, status)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_public_alert_events_status
            ON public_alert_events(status)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_public_alert_events_delivery_state
            ON public_alert_events(delivery_state, next_retry_at)
        """
    )


def _migrate_public_alert_delivery_state_v16(connection: sqlite3.Connection) -> None:
    """Map pre-v16 rows without treating old reservations as safe to resend."""

    connection.execute(
        """
        UPDATE public_alert_events
        SET delivery_state = CASE
                WHEN status = 'SENT' THEN 'SENT'
                WHEN status = 'RESERVED' THEN 'UNCERTAIN'
                WHEN status = 'FAILED' THEN 'FAILED_FINAL'
                WHEN status = 'BLOCKED' THEN 'FAILED_FINAL'
                ELSE 'FAILED_FINAL'
            END,
            uncertain_at = CASE
                WHEN status = 'RESERVED' THEN COALESCE(uncertain_at, updated_at, reserved_at, created_at)
                ELSE uncertain_at
            END,
            last_error_category = CASE
                WHEN status = 'RESERVED' THEN 'legacy_reserved_acceptance_unknown'
                ELSE last_error_category
            END
        WHERE delivery_state IS NULL
           OR delivery_state = ''
           OR (
                delivery_state = 'PENDING'
                AND payload_text = 'N/A'
                AND message_hash = 'N/A'
                AND attempt_count = 0
                AND status IN ('SENT', 'RESERVED', 'FAILED', 'BLOCKED')
           )
        """
    )
    connection.execute(
        """
        UPDATE telegram_alert_attempts
        SET delivery_state = CASE
                WHEN telegram_status = 'sent' THEN 'SENT'
                WHEN telegram_status = 'reserved' THEN 'UNCERTAIN'
                WHEN telegram_status = 'failed' THEN 'FAILED_FINAL'
                ELSE delivery_state
            END
        WHERE delivery_state IS NULL OR delivery_state IN ('', 'N/A')
        """
    )


def _ensure_nullable_telegram_sent_at(connection: sqlite3.Connection) -> None:
    columns = connection.execute("PRAGMA table_info(telegram_alert_attempts)").fetchall()
    sent_at = next((row for row in columns if row[1] == "sent_at"), None)
    if sent_at is None or int(sent_at[3]) == 0:
        return

    legacy_columns = [str(row[1]) for row in columns]
    connection.execute("DROP INDEX IF EXISTS ix_telegram_alert_attempts_signal")
    connection.execute("DROP INDEX IF EXISTS ix_telegram_alert_attempts_scan_run")
    connection.execute("DROP INDEX IF EXISTS ix_telegram_alert_attempts_public_plan")
    connection.execute("DROP INDEX IF EXISTS ux_telegram_alert_attempts_public_event_sent")
    connection.execute("DROP INDEX IF EXISTS ux_telegram_alert_attempts_public_event_active")
    connection.execute("ALTER TABLE telegram_alert_attempts RENAME TO telegram_alert_attempts_legacy_sent_at")
    connection.execute(
        """
        CREATE TABLE telegram_alert_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            signal_id TEXT NOT NULL,
            symbol TEXT NOT NULL,
            direction TEXT NOT NULL,
            previous_state TEXT NOT NULL DEFAULT 'N/A',
            new_state TEXT NOT NULL,
            alert_type TEXT NOT NULL,
            lifecycle_state TEXT NOT NULL,
            sent_at TEXT,
            attempted_at TEXT NOT NULL DEFAULT 'N/A',
            telegram_status TEXT NOT NULL,
            message_hash TEXT NOT NULL,
            scan_run_id TEXT,
            attempted_alert_type TEXT NOT NULL DEFAULT 'N/A',
            setup_quality_score TEXT NOT NULL DEFAULT 'N/A',
            rr_planned TEXT NOT NULL DEFAULT 'N/A',
            min_rr TEXT NOT NULL DEFAULT 'N/A',
            opportunity_score TEXT NOT NULL DEFAULT 'N/A',
            min_score_for_idea TEXT NOT NULL DEFAULT 'N/A',
            technical_score TEXT NOT NULL DEFAULT 'N/A',
            price_level TEXT NOT NULL DEFAULT 'N/A',
            entry_low TEXT NOT NULL DEFAULT 'N/A',
            entry_high TEXT NOT NULL DEFAULT 'N/A',
            stop_loss TEXT NOT NULL DEFAULT 'N/A',
            tp1 TEXT NOT NULL DEFAULT 'N/A',
            tp2 TEXT NOT NULL DEFAULT 'N/A',
            tp3 TEXT NOT NULL DEFAULT 'N/A',
            blocked_reason TEXT NOT NULL DEFAULT 'N/A',
            invalid_target_fields TEXT NOT NULL DEFAULT 'N/A',
            error_message TEXT NOT NULL DEFAULT 'N/A',
            first_seen_at TEXT NOT NULL DEFAULT 'N/A',
            last_seen_at TEXT NOT NULL DEFAULT 'N/A',
            seen_count INTEGER NOT NULL DEFAULT 1,
            last_scan_run_id TEXT,
            last_error_message TEXT NOT NULL DEFAULT 'N/A',
            public_watchlist_plan_id TEXT NOT NULL DEFAULT 'N/A',
            public_watchlist_event_key TEXT NOT NULL DEFAULT 'N/A',
            public_alert_event_type TEXT NOT NULL DEFAULT 'N/A',
            normalized_entry_zone_low TEXT NOT NULL DEFAULT 'N/A',
            normalized_entry_zone_high TEXT NOT NULL DEFAULT 'N/A',
            normalized_invalidation TEXT NOT NULL DEFAULT 'N/A',
            dedupe_status TEXT NOT NULL DEFAULT 'N/A',
            dedupe_reason TEXT NOT NULL DEFAULT 'N/A',
            UNIQUE(signal_id, alert_type)
        )
        """
    )
    target_columns = [
        "id",
        "signal_id",
        "symbol",
        "direction",
        "previous_state",
        "new_state",
        "alert_type",
        "lifecycle_state",
        "sent_at",
        "attempted_at",
        "telegram_status",
        "message_hash",
        "scan_run_id",
        "attempted_alert_type",
        "setup_quality_score",
        "rr_planned",
        "min_rr",
        "opportunity_score",
        "min_score_for_idea",
        "technical_score",
        "price_level",
        "entry_low",
        "entry_high",
        "stop_loss",
        "tp1",
        "tp2",
        "tp3",
        "blocked_reason",
        "invalid_target_fields",
        "error_message",
        "first_seen_at",
        "last_seen_at",
        "seen_count",
        "last_scan_run_id",
        "last_error_message",
        "public_watchlist_plan_id",
        "public_watchlist_event_key",
        "public_alert_event_type",
        "normalized_entry_zone_low",
        "normalized_entry_zone_high",
        "normalized_invalidation",
        "dedupe_status",
        "dedupe_reason",
    ]
    common_columns = [column for column in target_columns if column in legacy_columns]
    column_list = ", ".join(common_columns)
    connection.execute(
        f"""
        INSERT INTO telegram_alert_attempts ({column_list})
        SELECT {column_list}
        FROM telegram_alert_attempts_legacy_sent_at
        """
    )
    if "sent_at" in legacy_columns:
        connection.execute(
            """
            UPDATE telegram_alert_attempts
            SET attempted_at = sent_at
            WHERE telegram_status IN ('blocked', 'skipped', 'failed')
              AND (attempted_at IS NULL OR attempted_at = 'N/A' OR attempted_at = '')
              AND sent_at IS NOT NULL
              AND sent_at NOT IN ('', 'N/A')
            """
        )
    connection.execute("DROP TABLE telegram_alert_attempts_legacy_sent_at")
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_telegram_alert_attempts_signal
            ON telegram_alert_attempts(signal_id, alert_type)
        """
    )
    connection.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_telegram_alert_attempts_scan_run
            ON telegram_alert_attempts(scan_run_id)
        """
    )
