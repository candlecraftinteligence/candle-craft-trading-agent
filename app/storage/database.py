from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DATABASE_PATH = Path("scan_runs") / "candle_craft.db"
SCHEMA_VERSION = 3


class StorageError(RuntimeError):
    """Raised when local scan history cannot be read or written cleanly."""


def connect_database(path: Path | str = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    database_path = Path(path)
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection
    except sqlite3.Error as exc:
        raise StorageError(f"Unable to open scan history database: {database_path}") from exc
    except OSError as exc:
        raise StorageError(f"Unable to prepare scan history database directory: {database_path}") from exc


def initialize_database(connection: sqlite3.Connection) -> None:
    try:
        connection.executescript(
            """
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
                raw_payload_json TEXT NOT NULL
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
                readiness_score INTEGER NOT NULL DEFAULT 0,
                quality_score INTEGER NOT NULL DEFAULT 0,
                edge_score TEXT NOT NULL DEFAULT 'N/A',
                regime_state TEXT NOT NULL DEFAULT 'N/A',
                action_label TEXT NOT NULL DEFAULT 'N/A',
                invalidation_reason TEXT NOT NULL DEFAULT 'N/A',
                cooldown_until TEXT,
                archived_at TEXT,
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
            """
        )
        _ensure_column(connection, "scan_runs", "regime_confidence", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "scan_runs", "regime_compatibility_json", "TEXT NOT NULL DEFAULT '{}'")
        _ensure_column(connection, "scan_runs", "environment_notes_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(connection, "symbol_results", "regime_confidence", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "symbol_results", "regime_compatibility_score", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "symbol_results", "regime_compatibility_label", "TEXT NOT NULL DEFAULT 'N/A'")
        _ensure_column(connection, "symbol_results", "regime_penalty", "INTEGER NOT NULL DEFAULT 0")
        _ensure_column(connection, "symbol_results", "environment_notes_json", "TEXT NOT NULL DEFAULT '[]'")
        _ensure_column(connection, "setup_lifecycle_records", "cooldown_until", "TEXT")
        _ensure_column(connection, "setup_lifecycle_records", "archived_at", "TEXT")
        _ensure_column(connection, "setup_lifecycle_events", "scan_run_id", "TEXT")
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        connection.commit()
    except sqlite3.Error as exc:
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
