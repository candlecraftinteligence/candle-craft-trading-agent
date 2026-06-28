from __future__ import annotations

import sqlite3
from pathlib import Path

DEFAULT_DATABASE_PATH = Path("scan_runs") / "candle_craft.db"
SCHEMA_VERSION = 14


class StorageError(RuntimeError):
    """Raised when local scan history cannot be read or written cleanly."""


def connect_database(path: Path | str = DEFAULT_DATABASE_PATH) -> sqlite3.Connection:
    database_path = Path(path)
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(database_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
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
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(event_key)
            );

            CREATE INDEX IF NOT EXISTS ix_public_alert_events_symbol_side_setup
                ON public_alert_events(symbol, side, setup_family, event_type, status);
            CREATE INDEX IF NOT EXISTS ix_public_alert_events_status
                ON public_alert_events(status);

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
        _ensure_nullable_telegram_sent_at(connection)
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
    connection.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_telegram_alert_attempts_public_event_active
            ON telegram_alert_attempts(public_watchlist_event_key)
            WHERE telegram_status IN ('reserved', 'sent')
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
