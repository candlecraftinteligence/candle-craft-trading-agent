from __future__ import annotations

import json
import shutil
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.analytics.setup_quality import validate_setup_quality
from app.backtesting import (
    ReplayDirection,
    ReplayOutcome,
    ReplaySetupCandidate,
    ReplayStats,
    ReplaySummary,
    ReplaySymbolResult,
    ReplayTradeResult,
)
from app.data.dtos import NA
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerRuntimeStats,
    ScannerSymbolResult,
)
from app.storage.database import (
    SCHEMA_VERSION,
    DatabaseMissingError,
    StorageError,
    open_initialized_database,
)
from app.storage.maintenance import create_verified_backup, verify_backup
from app.storage.models import WatchIterationMetadata
from app.storage.repositories import export_history_payload, list_scan_history, store_scan_result


def _scanner_config() -> ScannerRunConfig:
    return ScannerRunConfig(
        symbols=("BTCUSDT", "ETHUSDT", "XRPUSDT"),
        exchange="binance",
        account_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        market_regime_enabled=True,
    )


def _valid_symbol() -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        technical_score=88,
        derivatives_score=82,
        candidate_quality_grade="A",
        final_quality_grade="A",
        final_failed_gate=NA,
        final_block_reason=NA,
        target_integrity_status="passed",
        target_failure=NA,
        actionability_state="A_GRADE_ACTIONABLE",
        valid_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "is_valid": True,
                "mode": "swing",
                "bias": "long",
                "entry": Decimal("100"),
                "stop": Decimal("95"),
                "tp1": Decimal("110"),
                "tp2": Decimal("115"),
                "tp3": Decimal("120"),
                "rr_to_tp2": Decimal("3"),
                "opportunity_score": Decimal("88"),
                "candidate_quality_grade": "A",
                "final_quality_grade": "A",
                "final_failed_gate": NA,
                "actionability_state": "A_GRADE_ACTIONABLE",
                "target_integrity_status": "passed",
                "invalidation": "Invalid if price accepts below 95.",
                "trust_grade": "A",
                "trust_percentage": 88,
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
                "derivatives_supports_trade": True,
            }
        },
        setup_quality=validate_setup_quality(
            {
                "setup_valid": True,
                "mode": "swing",
                "bias": "long",
                "rr_to_tp2": Decimal("3"),
                "best_rr": Decimal("3"),
                "derivatives_supports_trade": True,
                "derivatives_score": 82,
                "first_failed_gate": NA,
            }
        ),
    )


def _near_miss_symbol() -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="ETHUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        technical_score=72,
        derivatives_score=76,
        rejected_strategy_modes=("swing",),
        rejection_reason="Trust meter below minimum.",
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "first_failed_gate": "trust_meter_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": ("trust_meter_below_minimum",),
                "rr_to_tp2": Decimal("2.8"),
                "trust_percentage": 61,
                "pullback_failure_reason": "Late setup quality gate failed.",
                "derivatives_supports_trade": True,
            }
        },
        setup_quality=validate_setup_quality(
            {
                "setup_valid": False,
                "mode": "swing",
                "bias": "long",
                "rr_to_tp2": Decimal("2.8"),
                "best_rr": Decimal("2.8"),
                "confirmation_passed": True,
                "pullback_valid": True,
                "first_failed_gate": "trust_meter_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": ("trust_meter_below_minimum",),
                "rejection_reason": "Trust meter below minimum.",
            }
        ),
    )


def _rejected_symbol() -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="XRPUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("challenge",),
        missing_data=("cvd: N/A",),
        unverified_data=("derivatives: Unverified",),
        strategy_diagnostics={
            "challenge": {
                "execution_sweep_status": "failed",
                "confirmation_structure_shift_status": "not_evaluated",
                "pullback_zone_status": NA,
                "first_failed_gate": "missing_confirmed_sweep",
                "gates_failed": ("missing_confirmed_sweep",),
            }
        },
    )


def _scan_result(
    *,
    runtime_stats: ScannerRuntimeStats | None = None,
    resume_metadata: dict[str, object] | None = None,
) -> ScannerRunResult:
    config = _scanner_config()
    return ScannerRunResult(
        config=config,
        results=(_valid_symbol(), _near_miss_symbol(), _rejected_symbol()),
        scanned_symbols=3,
        failed_symbols=0,
        trade_ideas_created=1,
        dry_run_alerts_created=0,
        journal_entries_created=0,
        resume_metadata=resume_metadata or {},
        runtime_stats=runtime_stats or ScannerRuntimeStats(),
    )


def _replay_summary() -> ReplaySummary:
    candidate = ReplaySetupCandidate(
        symbol="BTCUSDT",
        mode="swing",
        direction=ReplayDirection.LONG,
        detected_at_index=10,
        entry=Decimal("100"),
        entry_low=Decimal("99"),
        entry_high=Decimal("101"),
        stop=Decimal("95"),
        tp1=Decimal("110"),
        tp2=Decimal("115"),
        tp3=Decimal("120"),
        rr_to_tp2=Decimal("3"),
        invalidation="Invalid if price accepts below 95.",
        risk_warning="This is not financial advice. Pullback ideas are conditional.",
    )
    trade = ReplayTradeResult(
        symbol="BTCUSDT",
        mode="swing",
        direction=ReplayDirection.LONG,
        candidate=candidate,
        outcome=ReplayOutcome.TP1_HIT,
        entry=Decimal("100"),
        stop=Decimal("95"),
        tp1=Decimal("110"),
        tp2=Decimal("115"),
        tp3=Decimal("120"),
        filled=True,
        entry_filled=True,
        tp1_hit=True,
        highest_tp_hit=1,
        final_r_multiple=Decimal("2"),
        candles_held=12,
    )
    stats = ReplayStats(total_setups=1, filled_trades=1, tp1_rate=Decimal("100"), average_r=Decimal("2"))
    return ReplaySummary(
        symbols_tested=1,
        historical_candles=300,
        stats=stats,
        symbols=(ReplaySymbolResult(symbol="BTCUSDT", historical_candles=300, trades=(trade,), stats=stats),),
    )


def test_database_creation(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"

    with open_initialized_database(db_path):
        pass

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }

    assert {
        "scan_runs",
        "symbol_results",
        "setup_candidates",
        "replay_results",
        "telegram_alert_attempts",
        "public_alert_events",
        "setup_outcome_analytics",
        "setup_lifecycle_outcome_progress",
        "symbol_health_events",
    } <= tables


def test_scanner_process_improvement_schema_columns_exist(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"

    with open_initialized_database(db_path):
        pass

    with sqlite3.connect(db_path) as connection:
        lifecycle_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(setup_lifecycle_records)").fetchall()
        }
        health_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(symbol_health)").fetchall()
        }
        outcome_columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(setup_outcome_analytics)").fetchall()
        }
        progress_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(setup_lifecycle_outcome_progress)"
            ).fetchall()
        }

    assert {
        "entry_low",
        "entry_high",
        "stop_loss",
        "rr",
        "confirmation_count",
        "required_confirmation_cycles",
        "quality_grade_first_seen",
        "quality_grade_current",
        "quality_grade_confirmed",
        "confirmed_at",
        "decay_count",
        "decay_reason",
        "symbol_health_score_at_detection",
        "symbol_health_penalty_cycles",
        "setup_identity",
    } <= lifecycle_columns
    assert {
        "invalidation_count",
        "expired_setup_count",
        "rejected_setup_count",
        "false_confirmation_count",
        "malformed_setup_event_count",
        "stop_breach_after_confirmation_count",
        "duplicate_noisy_setup_count",
    } <= health_columns
    assert {"lifecycle_id", "symbol", "final_outcome", "lifecycle_path", "raw_payload_json"} <= outcome_columns
    assert {
        "lifecycle_id",
        "plan_identity",
        "execution_timeframe",
        "evaluation_cursor_open_at",
        "entry_at",
        "tp1_at",
        "tp2_at",
        "tp3_at",
        "stop_at",
        "invalidated_at",
        "outcome_at",
        "terminal_outcome",
        "integrity_status",
        "diagnostic",
        "metadata_json",
    } <= progress_columns


def test_scan_run_migration_adds_watch_columns_without_destroying_rows(tmp_path) -> None:
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE scan_runs (
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
            )
            """
        )
        connection.execute(
            """
            INSERT INTO scan_runs (
                run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
                strategy, timeframes_json, market_regime, runtime_stats_json,
                command_preset, command_used, total_valid_setups, near_misses,
                rejected, data_issues, data_issues_json, raw_payload_json
            ) VALUES (
                'legacy_run', '2026-05-18T00:00:00+00:00', 'binance', 'manual',
                1, '["BTCUSDT"]', 'liquidity_grab_pullback', '{}', 'trend_expansion',
                '{}', 'N/A', 'legacy', 0, 0, 1, 0, '[]', '{}'
            )
            """
        )
        connection.commit()

    with open_initialized_database(db_path):
        pass

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(scan_runs)").fetchall()
        }
        row = connection.execute(
            """
            SELECT run_id, is_watch_iteration, symbols_requested, valid_activations
            FROM scan_runs
            WHERE run_id = 'legacy_run'
            """
        ).fetchone()

    assert {
        "is_watch_iteration",
        "watch_iteration_number",
        "started_at",
        "completed_at",
        "symbols_requested",
        "symbols_queued",
        "symbols_completed",
        "valid_activations",
        "still_watching",
        "rejected_no_edge",
        "runtime_sec",
        "portfolio_summary_json",
        "symbol_health_summary_json",
    } <= columns
    assert row == ("legacy_run", 0, 0, 0)


def test_telegram_alert_attempt_migration_adds_audit_hygiene_columns(tmp_path) -> None:
    db_path = tmp_path / "legacy_telegram.db"
    with sqlite3.connect(db_path) as connection:
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
                sent_at TEXT NOT NULL,
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
                blocked_reason TEXT NOT NULL DEFAULT 'N/A',
                error_message TEXT NOT NULL DEFAULT 'N/A',
                UNIQUE(signal_id, alert_type)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, new_state, alert_type,
                lifecycle_state, sent_at, telegram_status, message_hash
            ) VALUES (
                'sig-legacy', 'BTCUSDT', 'long', 'WATCHLISTED', 'WATCHLIST_BLOCKED_abc',
                'WATCHLISTED', '2026-06-02T00:00:00+00:00', 'blocked', 'hash'
            )
            """
        )
        connection.commit()

    with open_initialized_database(db_path):
        pass

    with sqlite3.connect(db_path) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(telegram_alert_attempts)").fetchall()
        }
        sent_at_info = next(
            row
            for row in connection.execute("PRAGMA table_info(telegram_alert_attempts)").fetchall()
            if row[1] == "sent_at"
        )
        row = connection.execute(
            """
            SELECT seen_count, first_seen_at, last_seen_at, last_scan_run_id, last_error_message, invalid_target_fields,
                   attempted_at,
                   entry_low, entry_high, stop_loss, tp1, tp2, tp3,
                   public_watchlist_plan_id, public_watchlist_event_key
            FROM telegram_alert_attempts
            WHERE signal_id = 'sig-legacy'
            """
        ).fetchone()
        indexes = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'index'").fetchall()
        }

    assert {
        "first_seen_at",
        "last_seen_at",
        "seen_count",
        "last_scan_run_id",
        "last_error_message",
        "invalid_target_fields",
        "attempted_at",
        "entry_low",
        "entry_high",
        "stop_loss",
        "tp1",
        "tp2",
        "tp3",
        "public_watchlist_plan_id",
        "public_watchlist_event_key",
    } <= columns
    assert sent_at_info[3] == 0
    assert {
        "ix_telegram_alert_attempts_public_plan",
        "ux_telegram_alert_attempts_public_event_sent",
    } <= indexes
    assert row == (
        1,
        "N/A",
        "N/A",
        None,
        "N/A",
        "N/A",
        "2026-06-02T00:00:00+00:00",
        "N/A",
        "N/A",
        "N/A",
        "N/A",
        "N/A",
        "N/A",
        "N/A",
        "N/A",
    )


def test_database_migration_preserves_existing_alert_history(tmp_path) -> None:
    test_telegram_alert_attempt_migration_adds_audit_hygiene_columns(tmp_path)


def test_public_alert_events_migration_is_idempotent_with_existing_watchlist_attempt_columns(tmp_path) -> None:
    db_path = tmp_path / "legacy_public_watchlist_columns.db"
    with sqlite3.connect(db_path) as connection:
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
        connection.execute(
            """
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
                sent_at, attempted_at, telegram_status, message_hash, attempted_alert_type,
                entry_low, entry_high, stop_loss, public_watchlist_plan_id, public_watchlist_event_key
            ) VALUES (
                'legacy-pepe-watchlist', '1000PEPEUSDT', 'long', 'WATCHLISTED', 'WATCHLIST', 'WATCHLISTED',
                '2026-06-25T18:48:00+00:00', '2026-06-25T18:48:00+00:00', 'sent', 'legacy-hash', 'WATCHLIST',
                '0.00270433', '0.0027082', '0.00268872', 'N/A', 'N/A'
            )
            """
        )
        connection.commit()

    with open_initialized_database(db_path):
        pass
    with open_initialized_database(db_path):
        pass

    with sqlite3.connect(db_path) as connection:
        tables = {
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
        attempts = connection.execute(
            """
            SELECT signal_id, symbol, direction, telegram_status, public_watchlist_plan_id, public_watchlist_event_key
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()
        event_count = connection.execute("SELECT COUNT(*) FROM public_alert_events").fetchone()[0]
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        unique_event_key_indexes = []
        for index in connection.execute("PRAGMA index_list(public_alert_events)").fetchall():
            if index[2] != 1:
                continue
            columns = tuple(row[2] for row in connection.execute(f"PRAGMA index_info({index[1]})").fetchall())
            unique_event_key_indexes.append(columns)

    assert "public_alert_events" in tables
    assert attempts == [("legacy-pepe-watchlist", "1000PEPEUSDT", "long", "sent", "N/A", "N/A")]
    assert event_count == 0
    assert ("event_key",) in unique_event_key_indexes
    assert version == SCHEMA_VERSION


def test_scan_run_insert(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"

    run_id = store_scan_result(db_path, _scan_result(), command_preset="daily")

    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT run_id, command_preset, total_valid_setups FROM scan_runs").fetchone()

    assert row == (run_id, "daily", 1)


def test_normal_scan_run_persists_summary_metadata_from_runtime_stats(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"
    runtime_stats = ScannerRuntimeStats(
        total_runtime_seconds=1.234,
        average_seconds_per_symbol=0.411,
        completed_symbols=3,
    )
    result = _scan_result(
        runtime_stats=runtime_stats,
        resume_metadata={
            "watchlist_symbols": ["BTCUSDT", "ETHUSDT", "XRPUSDT"],
            "symbols_to_scan": ["BTCUSDT", "ETHUSDT", "XRPUSDT"],
        },
    )

    run_id = store_scan_result(db_path, result)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT runtime_stats_json, symbols_requested, symbols_queued,
                   symbols_completed, symbols_scanned, runtime_sec
            FROM scan_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

    stored_runtime = json.loads(row["runtime_stats_json"])
    assert stored_runtime["total_runtime_seconds"] == 1.234
    assert row["symbols_requested"] == 3
    assert row["symbols_queued"] == 3
    assert row["symbols_completed"] == 3
    assert row["symbols_scanned"] == 3
    assert row["runtime_sec"] == 1.234
    assert row["symbols_completed"] == stored_runtime["completed_symbols"]
    assert row["runtime_sec"] == stored_runtime["total_runtime_seconds"]


def test_watch_scan_run_persists_summary_metadata_from_runtime_stats(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"
    runtime_stats = ScannerRuntimeStats(
        total_runtime_seconds=2.5,
        average_seconds_per_symbol=0.833,
        completed_symbols=3,
    )
    result = _scan_result(
        runtime_stats=runtime_stats,
        resume_metadata={
            "watchlist_symbols": ["BTCUSDT", "ETHUSDT", "XRPUSDT"],
            "symbols_to_scan": ["BTCUSDT", "ETHUSDT", "XRPUSDT"],
            "watch_mode": True,
            "watch_iteration": 4,
        },
    )
    watch_iteration = WatchIterationMetadata(
        iteration_number=4,
        started_at="2026-06-06T10:00:00+00:00",
        completed_at="2026-06-06T10:00:03+00:00",
        symbols_requested=3,
        symbols_queued=3,
        symbols_completed=1,
        valid_activations=1,
        still_watching=1,
        rejected_no_edge=0,
        data_issues=0,
        runtime_sec=99.0,
    )

    run_id = store_scan_result(db_path, result, watch_iteration=watch_iteration)

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT runtime_stats_json, is_watch_iteration, watch_iteration_number,
                   symbols_requested, symbols_queued, symbols_completed,
                   valid_activations, still_watching, rejected_no_edge, runtime_sec
            FROM scan_runs
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

    stored_runtime = json.loads(row["runtime_stats_json"])
    assert row["is_watch_iteration"] == 1
    assert row["watch_iteration_number"] == 4
    assert row["symbols_requested"] == 3
    assert row["symbols_queued"] == 3
    assert row["symbols_completed"] == stored_runtime["completed_symbols"] == 3
    assert row["runtime_sec"] == stored_runtime["total_runtime_seconds"] == 2.5
    assert row["valid_activations"] == 1
    assert row["still_watching"] == 1
    assert row["rejected_no_edge"] == 0


def test_symbol_result_insert(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"

    run_id = store_scan_result(db_path, _scan_result())

    with sqlite3.connect(db_path) as connection:
        rows = connection.execute(
            "SELECT symbol, display_bucket, readiness_score FROM symbol_results WHERE run_id = ? ORDER BY symbol",
            (run_id,),
        ).fetchall()

    assert len(rows) == 3
    assert ("BTCUSDT", "valid", 90) in rows


def test_setup_candidate_insert(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"

    run_id = store_scan_result(db_path, _scan_result())

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT symbol, mode, direction, entry, stop, tp1, tp2, rr FROM setup_candidates WHERE run_id = ?",
            (run_id,),
        ).fetchone()

    assert row == ("BTCUSDT", "swing", "long", "100", "95", "110", "115", "3")


def test_setup_candidate_persists_final_actionability_audit_fields(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"

    run_id = store_scan_result(db_path, _scan_result())

    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT candidate_quality_grade, final_quality_grade, technical_score,
                   opportunity_score, failed_gate, final_block_reason,
                   target_integrity_status, target_failure, rr, actionability_state
            FROM setup_candidates
            WHERE run_id = ?
            """,
            (run_id,),
        ).fetchone()

    assert dict(row) == {
        "candidate_quality_grade": "A",
        "final_quality_grade": "A",
        "technical_score": "88",
        "opportunity_score": "88",
        "failed_gate": NA,
        "final_block_reason": NA,
        "target_integrity_status": "passed",
        "target_failure": NA,
        "rr": "3",
        "actionability_state": "A_GRADE_ACTIONABLE",
    }


def test_replay_result_insert(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"

    run_id = store_scan_result(db_path, _scan_result(), replay_summary=_replay_summary())

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT symbol, mode, outcome, filled, tp_hit, sl_hit, final_r, time_in_trade FROM replay_results WHERE run_id = ?",
            (run_id,),
        ).fetchone()

    assert row == ("BTCUSDT", "swing", "tp1_hit", 1, "TP1", 0, "2", "12")


def test_history_retrieval(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"
    store_scan_result(db_path, _scan_result(), command_preset="daily")

    history = list_scan_history(db_path, limit=5)

    assert len(history) == 1
    assert history[0].symbols_scanned == 3
    assert history[0].total_valid_setups == 1
    assert history[0].near_misses == 1
    assert history[0].rejected == 1


def test_json_export(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"
    store_scan_result(db_path, _scan_result())

    payload = export_history_payload(db_path, limit=10)

    assert payload[0]["symbols_scanned"] == 3
    assert payload[0]["valid_setups"] == 1
    assert payload[0]["near_misses"] == 1


def test_missing_db_history_fails_without_initializing(tmp_path) -> None:
    db_path = tmp_path / "missing.db"

    with pytest.raises(DatabaseMissingError, match="Database does not exist"):
        list_scan_history(db_path, limit=10)

    assert not db_path.exists()


def test_corrupted_db_is_reported_cleanly(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"
    db_path.write_text("not a sqlite database", encoding="utf-8")

    with pytest.raises(StorageError, match="Unable to identify database schema version"):
        list_scan_history(db_path, limit=10)


def _create_schema_v14_lifecycle_delivery_fixture(db_path: Path) -> None:
    """Create the representative schema contract immediately before outcome v15."""

    with open_initialized_database(db_path):
        pass

    with sqlite3.connect(db_path) as connection:
        connection.executescript(
            """
            DROP TABLE IF EXISTS public_alert_delivery_parts;
            DROP TABLE IF EXISTS setup_lifecycle_outcome_progress;
            DROP TABLE telegram_alert_attempts;
            DROP TABLE public_alert_events;

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
            );

            CREATE INDEX ix_telegram_alert_attempts_signal
                ON telegram_alert_attempts(signal_id, alert_type);
            CREATE INDEX ix_telegram_alert_attempts_scan_run
                ON telegram_alert_attempts(scan_run_id);
            CREATE INDEX ix_telegram_alert_attempts_public_plan
                ON telegram_alert_attempts(public_watchlist_plan_id);
            CREATE UNIQUE INDEX ux_telegram_alert_attempts_public_event_sent
                ON telegram_alert_attempts(public_watchlist_event_key)
                WHERE telegram_status = 'sent'
                  AND public_watchlist_event_key IS NOT NULL
                  AND public_watchlist_event_key NOT IN ('', 'N/A');
            CREATE UNIQUE INDEX ux_telegram_alert_attempts_public_event_active
                ON telegram_alert_attempts(public_watchlist_event_key)
                WHERE telegram_status IN ('reserved', 'sent')
                  AND public_watchlist_event_key IS NOT NULL
                  AND public_watchlist_event_key NOT IN ('', 'N/A');

            CREATE TABLE public_alert_events (
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

            CREATE INDEX ix_public_alert_events_symbol_side_setup
                ON public_alert_events(symbol, side, setup_family, event_type, status);
            CREATE INDEX ix_public_alert_events_status
                ON public_alert_events(status);

            INSERT INTO setup_lifecycle_records (
                lifecycle_id, symbol, mode, direction, current_state, previous_state,
                first_seen_at, last_seen_at, last_transition_at, readiness_score,
                quality_score, edge_score, regime_state, action_label, entry_low,
                entry_high, stop_loss, tp1, tp2, tp3, rr, invalidation_logic,
                confirmation_count, quality_grade_first_seen, quality_grade_current,
                quality_grade_confirmed, confirmed_at, setup_identity
            ) VALUES (
                'v14-lifecycle', 'BTCUSDT', 'swing', 'long', 'CONFIRMED',
                'WATCHLISTED', '2026-06-30T09:00:00Z', '2026-07-01T10:00:00Z',
                '2026-07-01T10:00:00Z', 91, 88, '84', 'TRENDING',
                'MANAGE', '100', '101', '95', '105', '110', '115', '3.0',
                'Invalid below 95', 2, 'A', 'A', 'A',
                '2026-07-01T10:00:00Z', 'v14-plan'
            );

            INSERT INTO setup_lifecycle_events (
                event_id, lifecycle_id, timestamp, symbol, from_state, to_state,
                reason, scan_run_id, readiness_score, quality_score, failed_gate, notes
            ) VALUES (
                41, 'v14-lifecycle', '2026-07-01T10:00:00Z', 'BTCUSDT',
                'WATCHLISTED', 'CONFIRMED', 'confirmation_complete', 'v14-scan',
                91, 88, 'N/A', 'closed candle confirmation'
            );

            INSERT INTO setup_outcome_analytics (
                id, lifecycle_id, symbol, bias, first_seen_at, confirmed_at,
                entry_zone, stop_loss, tp1, tp2, tp3, quality_at_first_detection,
                quality_at_confirmation, rr, lifecycle_path, final_outcome,
                failure_reason, outcome_reason, regime_context,
                symbol_health_at_detection, raw_payload_json, created_at, updated_at
            ) VALUES (
                51, 'v14-lifecycle', 'BTCUSDT', 'long',
                '2026-06-30T09:00:00Z', '2026-07-01T10:00:00Z', '100-101',
                '95', '105', '110', '115', 'A', 'A', '3.0',
                'WATCHLISTED>CONFIRMED>TP1', 'TP1', 'N/A', 'tp1_reached',
                'TRENDING', '82', '{"fixture":"v14"}',
                '2026-07-01T10:05:00Z', '2026-07-01T10:05:00Z'
            );

            INSERT INTO telegram_alert_attempts (
                id, signal_id, symbol, direction, previous_state, new_state,
                alert_type, lifecycle_state, sent_at, attempted_at,
                telegram_status, message_hash, scan_run_id, attempted_alert_type,
                first_seen_at, last_seen_at, public_watchlist_plan_id,
                public_watchlist_event_key, public_alert_event_type,
                normalized_entry_zone_low, normalized_entry_zone_high,
                normalized_invalidation, dedupe_status, dedupe_reason
            ) VALUES
                (
                    61, 'v14-sent-signal', 'BTCUSDT', 'long', 'N/A',
                    'WATCHLISTED', 'WATCHLIST', 'WATCHLISTED',
                    '2026-07-01T10:00:01Z', '2026-07-01T10:00:00Z', 'sent',
                    'v14-sent-hash', 'v14-scan', 'WATCHLIST',
                    '2026-07-01T10:00:00Z', '2026-07-01T10:00:01Z',
                    'v14-sent-plan', 'v14-sent-plan|initial_watchlist',
                    'initial_watchlist', '100', '101', '95', 'sent', 'N/A'
                ),
                (
                    62, 'v14-reserved-signal', 'ETHUSDT', 'short', 'N/A',
                    'WATCHLISTED', 'WATCHLIST', 'WATCHLISTED', NULL,
                    '2026-07-01T10:01:00Z', 'reserved',
                    'v14-reserved-hash', 'v14-scan', 'WATCHLIST',
                    '2026-07-01T10:01:00Z', '2026-07-01T10:01:00Z',
                    'v14-reserved-plan', 'v14-reserved-plan|initial_watchlist',
                    'initial_watchlist', '200', '201', '205', 'reserved', 'N/A'
                );

            INSERT INTO public_alert_events (
                id, canonical_plan_id, event_type, event_key, symbol, side,
                setup_family, normalized_zone_low, normalized_zone_high,
                normalized_invalidation, raw_entry_low, raw_entry_high,
                raw_stop_loss, status, reserved_at, sent_at, source_modes,
                matched_prior_alert_id, failure_reason, created_at, updated_at
            ) VALUES
                (
                    71, 'v14-sent-plan', 'initial_watchlist',
                    'v14-sent-plan|initial_watchlist', 'BTCUSDT', 'long',
                    'swing', '100', '101', '95', '100', '101', '95', 'SENT',
                    '2026-07-01T10:00:00Z', '2026-07-01T10:00:01Z', 'swing',
                    61, 'N/A', '2026-07-01T10:00:00Z',
                    '2026-07-01T10:00:01Z'
                ),
                (
                    72, 'v14-reserved-plan', 'initial_watchlist',
                    'v14-reserved-plan|initial_watchlist', 'ETHUSDT', 'short',
                    'swing', '200', '201', '205', '200', '201', '205',
                    'RESERVED', '2026-07-01T10:01:00Z', NULL, 'swing', 62,
                    'N/A', '2026-07-01T10:01:00Z',
                    '2026-07-01T10:01:02Z'
                );

            PRAGMA user_version = 14;
            """
        )
        connection.commit()


def _representative_v14_rows(db_path: Path) -> dict[str, list[tuple[object, ...]]]:
    with sqlite3.connect(db_path) as connection:
        return {
            "lifecycle": connection.execute(
                """
                SELECT lifecycle_id, symbol, mode, direction, current_state,
                       previous_state, first_seen_at, last_seen_at,
                       last_transition_at, confirmed_at, setup_identity
                FROM setup_lifecycle_records
                WHERE lifecycle_id = 'v14-lifecycle'
                """
            ).fetchall(),
            "events": connection.execute(
                """
                SELECT event_id, lifecycle_id, timestamp, from_state, to_state,
                       reason, scan_run_id, notes
                FROM setup_lifecycle_events
                WHERE lifecycle_id = 'v14-lifecycle'
                """
            ).fetchall(),
            "analytics": connection.execute(
                """
                SELECT id, lifecycle_id, symbol, final_outcome, outcome_reason,
                       raw_payload_json, created_at, updated_at
                FROM setup_outcome_analytics
                WHERE lifecycle_id = 'v14-lifecycle'
                """
            ).fetchall(),
            "attempts": connection.execute(
                """
                SELECT id, signal_id, telegram_status, sent_at, attempted_at,
                       message_hash, public_watchlist_plan_id,
                       public_watchlist_event_key
                FROM telegram_alert_attempts
                ORDER BY id
                """
            ).fetchall(),
            "public_events": connection.execute(
                """
                SELECT id, canonical_plan_id, event_key, status, reserved_at,
                       sent_at, matched_prior_alert_id, created_at, updated_at
                FROM public_alert_events
                ORDER BY id
                """
            ).fetchall(),
        }


def _schema_contract(db_path: Path) -> tuple[int, set[str], set[str], set[str]]:
    with sqlite3.connect(db_path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        attempt_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(telegram_alert_attempts)"
            ).fetchall()
        }
        public_columns = {
            str(row[1])
            for row in connection.execute(
                "PRAGMA table_info(public_alert_events)"
            ).fetchall()
        }
    return version, tables, attempt_columns, public_columns


def _create_schema_v15_delivery_fixture(db_path) -> None:
    with sqlite3.connect(db_path) as connection:
        connection.executescript(
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
                public_watchlist_plan_id TEXT NOT NULL DEFAULT 'N/A',
                public_watchlist_event_key TEXT NOT NULL DEFAULT 'N/A',
                UNIQUE(signal_id, alert_type)
            );
            CREATE TABLE public_alert_events (
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
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, new_state, alert_type,
                lifecycle_state, sent_at, attempted_at, telegram_status,
                message_hash, public_watchlist_plan_id, public_watchlist_event_key
            ) VALUES (
                'v15-signal', 'BTCUSDT', 'long', 'WATCHLISTED', 'WATCHLIST',
                'WATCHLISTED', '2026-07-01T10:00:00Z', '2026-07-01T10:00:00Z',
                'sent', 'v15-hash', 'v15-plan', 'v15-plan|initial_watchlist'
            );
            INSERT INTO public_alert_events (
                canonical_plan_id, event_type, event_key, symbol, side,
                setup_family, status, reserved_at, sent_at, source_modes
            ) VALUES (
                'v15-plan', 'initial_watchlist', 'v15-plan|initial_watchlist',
                'BTCUSDT', 'long', 'swing', 'SENT',
                '2026-07-01T10:00:00Z', '2026-07-01T10:00:01Z', 'swing'
            );
            PRAGMA user_version = 15;
            """
        )
        connection.commit()


def test_schema_v14_fixture_matches_pre_outcome_pre_outbox_contract(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v14-contract.db"
    _create_schema_v14_lifecycle_delivery_fixture(db_path)

    version, tables, attempt_columns, public_columns = _schema_contract(db_path)
    rows = _representative_v14_rows(db_path)

    assert version == 14
    assert "setup_lifecycle_outcome_progress" not in tables
    assert "public_alert_delivery_parts" not in tables
    assert "delivery_state" not in attempt_columns
    assert "telegram_message_id" not in attempt_columns
    assert "delivery_state" not in public_columns
    assert "payload_text" not in public_columns
    assert rows["lifecycle"] == [
        (
            "v14-lifecycle",
            "BTCUSDT",
            "swing",
            "long",
            "CONFIRMED",
            "WATCHLISTED",
            "2026-06-30T09:00:00Z",
            "2026-07-01T10:00:00Z",
            "2026-07-01T10:00:00Z",
            "2026-07-01T10:00:00Z",
            "v14-plan",
        )
    ]
    assert rows["events"] == [
        (
            41,
            "v14-lifecycle",
            "2026-07-01T10:00:00Z",
            "WATCHLISTED",
            "CONFIRMED",
            "confirmation_complete",
            "v14-scan",
            "closed candle confirmation",
        )
    ]
    assert rows["analytics"] == [
        (
            51,
            "v14-lifecycle",
            "BTCUSDT",
            "TP1",
            "tp1_reached",
            '{"fixture":"v14"}',
            "2026-07-01T10:05:00Z",
            "2026-07-01T10:05:00Z",
        )
    ]
    assert rows["attempts"] == [
        (
            61,
            "v14-sent-signal",
            "sent",
            "2026-07-01T10:00:01Z",
            "2026-07-01T10:00:00Z",
            "v14-sent-hash",
            "v14-sent-plan",
            "v14-sent-plan|initial_watchlist",
        ),
        (
            62,
            "v14-reserved-signal",
            "reserved",
            None,
            "2026-07-01T10:01:00Z",
            "v14-reserved-hash",
            "v14-reserved-plan",
            "v14-reserved-plan|initial_watchlist",
        ),
    ]
    assert rows["public_events"] == [
        (
            71,
            "v14-sent-plan",
            "v14-sent-plan|initial_watchlist",
            "SENT",
            "2026-07-01T10:00:00Z",
            "2026-07-01T10:00:01Z",
            61,
            "2026-07-01T10:00:00Z",
            "2026-07-01T10:00:01Z",
        ),
        (
            72,
            "v14-reserved-plan",
            "v14-reserved-plan|initial_watchlist",
            "RESERVED",
            "2026-07-01T10:01:00Z",
            None,
            62,
            "2026-07-01T10:01:00Z",
            "2026-07-01T10:01:02Z",
        ),
    ]


def test_schema_v14_to_v16_preserves_lifecycle_and_telegram_data(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "v14-to-v16.db"
    _create_schema_v14_lifecycle_delivery_fixture(db_path)
    before = _representative_v14_rows(db_path)

    with open_initialized_database(db_path):
        pass

    assert _representative_v14_rows(db_path) == before
    version, tables, attempt_columns, public_columns = _schema_contract(db_path)
    assert version == 16 == SCHEMA_VERSION
    assert "setup_lifecycle_outcome_progress" in tables
    assert "public_alert_delivery_parts" in tables
    assert "delivery_state" in attempt_columns
    assert "telegram_message_id" in attempt_columns
    assert "delivery_state" in public_columns
    assert "payload_text" in public_columns

    with sqlite3.connect(db_path) as connection:
        progress_count = connection.execute(
            "SELECT COUNT(*) FROM setup_lifecycle_outcome_progress"
        ).fetchone()[0]
        part_count = connection.execute(
            "SELECT COUNT(*) FROM public_alert_delivery_parts"
        ).fetchone()[0]
        attempts = connection.execute(
            """
            SELECT signal_id, delivery_state, message_hash,
                   telegram_chat_id, telegram_message_id, delivery_part_count
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()
        events = connection.execute(
            """
            SELECT canonical_plan_id, delivery_state, payload_text, message_hash,
                   telegram_chat_id, telegram_message_id, uncertain_at,
                   last_error_category
            FROM public_alert_events
            ORDER BY id
            """
        ).fetchall()

    assert progress_count == 0
    assert part_count == 0
    assert attempts == [
        ("v14-sent-signal", "SENT", "v14-sent-hash", None, None, 1),
        ("v14-reserved-signal", "UNCERTAIN", "v14-reserved-hash", None, None, 1),
    ]
    assert events == [
        (
            "v14-sent-plan",
            "SENT",
            "N/A",
            "N/A",
            None,
            None,
            None,
            "N/A",
        ),
        (
            "v14-reserved-plan",
            "UNCERTAIN",
            "N/A",
            "N/A",
            None,
            None,
            "2026-07-01T10:01:02Z",
            "legacy_reserved_acceptance_unknown",
        ),
    ]


def test_schema_v14_to_v16_migration_is_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "v14-v16-idempotent.db"
    _create_schema_v14_lifecycle_delivery_fixture(db_path)

    with open_initialized_database(db_path):
        pass
    first_rows = _representative_v14_rows(db_path)
    with sqlite3.connect(db_path) as connection:
        first_delivery = connection.execute(
            """
            SELECT id, delivery_state, telegram_message_id
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall()

    with open_initialized_database(db_path):
        pass

    assert _representative_v14_rows(db_path) == first_rows
    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            """
            SELECT id, delivery_state, telegram_message_id
            FROM telegram_alert_attempts
            ORDER BY id
            """
        ).fetchall() == first_delivery
        assert connection.execute(
            "SELECT COUNT(*) FROM setup_lifecycle_records"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM setup_lifecycle_events"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts"
        ).fetchone()[0] == 2
        assert connection.execute(
            "SELECT COUNT(*) FROM public_alert_events"
        ).fetchone()[0] == 2
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_schema_v14_migration_failure_rolls_back_completely(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.storage.database as database_module

    db_path = tmp_path / "v14-rollback.db"
    _create_schema_v14_lifecycle_delivery_fixture(db_path)
    before = _representative_v14_rows(db_path)

    def fail_migration(connection):
        raise sqlite3.OperationalError("fault-injected v14 migration failure")

    monkeypatch.setattr(
        database_module,
        "_migrate_public_alert_delivery_state_v16",
        fail_migration,
    )
    with pytest.raises(StorageError, match="initialize scan history database schema"):
        with open_initialized_database(db_path):
            pass

    version, tables, attempt_columns, public_columns = _schema_contract(db_path)
    assert version == 14
    assert "setup_lifecycle_outcome_progress" not in tables
    assert "public_alert_delivery_parts" not in tables
    assert "delivery_state" not in attempt_columns
    assert "delivery_state" not in public_columns
    assert _representative_v14_rows(db_path) == before


def test_verified_v14_backup_restore_migrates_copy_without_changing_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "runtime-v14.db"
    archive = tmp_path / "archive"
    restored = tmp_path / "restored-v14.db"
    _create_schema_v14_lifecycle_delivery_fixture(source)
    source_rows = _representative_v14_rows(source)

    backup = create_verified_backup(
        source,
        archive,
        allow_unsafe_temp=True,
        unique_suffix="v14-chain",
    )
    snapshot = Path(str(backup["snapshot_path"]))

    assert backup["status"] == "verified"
    assert backup["manifest"]["source_schema_version"] == 14
    assert backup["manifest"]["snapshot_schema_version"] == 14
    assert verify_backup(snapshot)["ok"] is True

    shutil.copy2(snapshot, restored)
    with open_initialized_database(restored):
        pass

    assert _representative_v14_rows(restored) == source_rows
    assert _schema_contract(restored)[0] == SCHEMA_VERSION
    assert _schema_contract(source)[0] == 14
    assert _representative_v14_rows(source) == source_rows


def test_schema_v15_delivery_data_survives_v16_migration(tmp_path) -> None:
    db_path = tmp_path / "v15-to-v16.db"
    _create_schema_v15_delivery_fixture(db_path)

    with open_initialized_database(db_path):
        pass

    with sqlite3.connect(db_path) as connection:
        attempt = connection.execute(
            """
            SELECT signal_id, telegram_status, sent_at, delivery_state
            FROM telegram_alert_attempts
            """
        ).fetchone()
        event = connection.execute(
            """
            SELECT canonical_plan_id, status, sent_at, delivery_state
            FROM public_alert_events
            """
        ).fetchone()
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]

    assert attempt == (
        "v15-signal", "sent", "2026-07-01T10:00:00Z", "SENT"
    )
    assert event == (
        "v15-plan", "SENT", "2026-07-01T10:00:01Z", "SENT"
    )
    assert "public_alert_delivery_parts" in tables
    assert version == 16 == SCHEMA_VERSION


def test_schema_v16_migration_is_idempotent_for_v15_delivery_data(tmp_path) -> None:
    db_path = tmp_path / "v16-idempotent.db"
    _create_schema_v15_delivery_fixture(db_path)

    with open_initialized_database(db_path):
        pass
    with open_initialized_database(db_path):
        pass

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM telegram_alert_attempts WHERE signal_id = 'v15-signal'"
        ).fetchone()[0] == 1
        assert connection.execute(
            "SELECT COUNT(*) FROM public_alert_events WHERE canonical_plan_id = 'v15-plan'"
        ).fetchone()[0] == 1
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION


def test_schema_v16_migration_failure_rolls_back_completely(tmp_path, monkeypatch) -> None:
    import app.storage.database as database_module

    db_path = tmp_path / "v16-rollback.db"
    _create_schema_v15_delivery_fixture(db_path)

    def fail_migration(connection):
        raise sqlite3.OperationalError("fault-injected migration failure")

    monkeypatch.setattr(
        database_module, "_migrate_public_alert_delivery_state_v16", fail_migration
    )
    with sqlite3.connect(db_path) as connection:
        with pytest.raises(StorageError, match="initialize scan history database schema"):
            database_module.initialize_database(connection)

    with sqlite3.connect(db_path) as connection:
        public_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(public_alert_events)"
            ).fetchall()
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        event_count = connection.execute(
            "SELECT COUNT(*) FROM public_alert_events"
        ).fetchone()[0]

    assert "delivery_state" not in public_columns
    assert "public_alert_delivery_parts" not in tables
    assert version == 15
    assert event_count == 1
