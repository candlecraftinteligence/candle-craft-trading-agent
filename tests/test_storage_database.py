from __future__ import annotations

import sqlite3
from decimal import Decimal

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
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from app.storage.database import StorageError, open_initialized_database
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


def _scan_result() -> ScannerRunResult:
    config = _scanner_config()
    return ScannerRunResult(
        config=config,
        results=(_valid_symbol(), _near_miss_symbol(), _rejected_symbol()),
        scanned_symbols=3,
        failed_symbols=0,
        trade_ideas_created=1,
        dry_run_alerts_created=0,
        journal_entries_created=0,
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

    assert {"scan_runs", "symbol_results", "setup_candidates", "replay_results"} <= tables


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


def test_scan_run_insert(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"

    run_id = store_scan_result(db_path, _scan_result(), command_preset="daily")

    with sqlite3.connect(db_path) as connection:
        row = connection.execute("SELECT run_id, command_preset, total_valid_setups FROM scan_runs").fetchone()

    assert row == (run_id, "daily", 1)


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


def test_missing_db_history_is_empty(tmp_path) -> None:
    history = list_scan_history(tmp_path / "missing.db", limit=10)

    assert history == ()


def test_corrupted_db_is_reported_cleanly(tmp_path) -> None:
    db_path = tmp_path / "candle_craft.db"
    db_path.write_text("not a sqlite database", encoding="utf-8")

    with pytest.raises(StorageError, match="Unable to initialize scan history database schema"):
        list_scan_history(db_path, limit=10)
