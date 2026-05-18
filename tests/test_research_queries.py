from __future__ import annotations

import asyncio
import json

import pytest

from app.research.queries import (
    MISSING_SCAN_DATABASE_MESSAGE,
    SAMPLE_SIZE_WARNING,
    ResearchDatabaseMissing,
    ResearchFilters,
    build_research_report,
)
from app.research.reports import format_research_report
from app.storage.database import open_initialized_database
from scripts import run_scan


def _seed_research_database(db_path) -> None:
    with open_initialized_database(db_path) as connection:
        _insert_run(
            connection,
            run_id="run_1",
            timestamp="2026-05-18T09:00:00+00:00",
            symbols_scanned=3,
            regime="trend_expansion",
            valid=1,
            near=1,
            rejected=1,
            data_issues=0,
        )
        _insert_run(
            connection,
            run_id="run_2",
            timestamp="2026-05-18T10:00:00+00:00",
            symbols_scanned=2,
            regime="chop",
            valid=1,
            near=0,
            rejected=0,
            data_issues=1,
        )
        _insert_symbol(
            connection,
            run_id="run_1",
            symbol="BTCUSDT",
            bucket="valid",
            readiness=92,
            quality=88,
            gate="N/A",
            reason="N/A",
            next_trigger="N/A",
            regime="trend_expansion",
            valid_modes=("swing",),
            rejected_modes=(),
            quality_state="HIGH_QUALITY_TRADE",
            quality_grade="A",
            readiness_label="VALID SETUP",
        )
        _insert_symbol(
            connection,
            run_id="run_1",
            symbol="ETHUSDT",
            bucket="near_miss",
            readiness=76,
            quality=68,
            gate="trust_meter_below_minimum",
            reason="Trust meter below minimum.",
            next_trigger="Final quality must improve.",
            regime="trend_expansion",
            valid_modes=(),
            rejected_modes=("swing",),
            quality_state="WATCHLIST_NEAR_MISS",
            quality_grade="C",
            readiness_label="HOT WATCH",
            pullback={
                "pullback_failure_type": "NO_OB_FVG",
                "pullback_quality_grade": "C",
                "pullback_depth_ratio": "0.5",
                "fib_zone_status": "aligned",
                "ob_fvg_status": "missing",
                "next_pullback_condition": "valid OB/FVG inside displacement required",
            },
            lifecycle_state="TRIGGERED",
        )
        _insert_symbol(
            connection,
            run_id="run_1",
            symbol="XRPUSDT",
            bucket="no_setup",
            readiness=22,
            quality=25,
            gate="missing_confirmed_sweep",
            reason="No confirmed sweep.",
            next_trigger="Wait for sweep.",
            regime="trend_expansion",
            valid_modes=(),
            rejected_modes=("challenge",),
            quality_state="REJECTED_NO_EDGE",
            quality_grade="Reject",
            readiness_label="REJECTED",
            missing_data=("cvd: N/A",),
            unverified_data=("derivatives: Unverified",),
            pullback={
                "pullback_failure_type": "TOO_DEEP",
                "pullback_quality_grade": "REJECT",
                "pullback_depth_ratio": "0.82",
                "fib_zone_status": "failed",
                "ob_fvg_status": "present",
                "next_pullback_condition": "fresh sweep + BOS required",
            },
            lifecycle_state="INVALIDATED",
        )
        _insert_symbol(
            connection,
            run_id="run_2",
            symbol="ETHUSDT",
            bucket="valid",
            readiness=88,
            quality=82,
            gate="N/A",
            reason="N/A",
            next_trigger="N/A",
            regime="chop",
            valid_modes=("swing",),
            rejected_modes=(),
            quality_state="VALID_BUT_LOWER_QUALITY",
            quality_grade="A",
            readiness_label="VALID SETUP",
        )
        _insert_symbol(
            connection,
            run_id="run_2",
            symbol="XRPUSDT",
            bucket="data_issue",
            readiness=5,
            quality=0,
            gate="not_enough_candles",
            reason="Required candles missing.",
            next_trigger="Required public market data must become available.",
            regime="chop",
            valid_modes=(),
            rejected_modes=("challenge",),
            quality_state="DATA_ISSUE",
            quality_grade="N/A",
            readiness_label="DATA ISSUE",
            missing_data=("candles_15m: N/A",),
        )
        _insert_setup(connection, "run_1", "BTCUSDT", "swing", "A")
        _insert_setup(connection, "run_2", "ETHUSDT", "swing", "A")
        _insert_replay(connection, "run_1", "BTCUSDT", "swing", "trend_expansion", "tp2_hit", 1, "TP2", 0, "2")
        _insert_replay(connection, "run_2", "ETHUSDT", "swing", "chop", "stopped", 1, "N/A", 1, "-1")
        connection.commit()


def _insert_run(connection, *, run_id, timestamp, symbols_scanned, regime, valid, near, rejected, data_issues) -> None:
    connection.execute(
        """
        INSERT INTO scan_runs (
            run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
            strategy, timeframes_json, market_regime, runtime_stats_json,
            command_preset, command_used, total_valid_setups, near_misses, rejected,
            data_issues, data_issues_json, raw_payload_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            timestamp,
            "binance",
            "manual",
            symbols_scanned,
            "[]",
            "liquidity_grab_pullback",
            "{}",
            regime,
            "{}",
            "N/A",
            "test",
            valid,
            near,
            rejected,
            data_issues,
            "[]",
            "{}",
        ),
    )


def _insert_symbol(
    connection,
    *,
    run_id,
    symbol,
    bucket,
    readiness,
    quality,
    gate,
    reason,
    next_trigger,
    regime,
    valid_modes,
    rejected_modes,
    quality_state,
    quality_grade,
    readiness_label,
    missing_data=(),
    unverified_data=(),
    pullback=None,
    lifecycle_state="N/A",
) -> None:
    modes = tuple(valid_modes or rejected_modes or ("swing",))
    raw = {
        "valid_strategy_modes": list(valid_modes),
        "rejected_strategy_modes": list(rejected_modes),
        "strategy_diagnostics": {
            mode: {
                "first_failed_gate": gate,
                "gates_failed": [] if gate == "N/A" else [gate],
            }
            for mode in modes
        },
        "setup_quality": {
            "quality_state": quality_state,
            "quality_grade": quality_grade,
            "quality_score": quality,
        },
        "readiness_label": readiness_label,
        "pullback_intelligence": pullback,
        "lifecycle_current_state": lifecycle_state,
        "missing_data": list(missing_data),
        "unverified_data": list(unverified_data),
    }
    connection.execute(
        """
        INSERT INTO symbol_results (
            run_id, symbol, status, display_bucket, readiness_score, setup_quality_score,
            edge_score, failed_gate, rejection_reason, next_trigger_needed, action_label,
            regime_state, derivatives_context_json, volume_profile_context_json,
            pullback_status, portfolio_decision, raw_result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            symbol,
            "scanned",
            bucket,
            readiness,
            str(quality),
            "N/A",
            gate,
            reason,
            next_trigger,
            "Watchlist only",
            regime,
            "{}",
            "{}",
            "N/A",
            "N/A",
            json.dumps(raw),
        ),
    )


def _insert_setup(connection, run_id, symbol, mode, grade) -> None:
    connection.execute(
        """
        INSERT INTO setup_candidates (
            run_id, symbol, mode, direction, entry, stop, tp1, tp2, tp3, rr,
            invalidation, quality_grade, trust_meter, risk_warning, raw_candidate_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            symbol,
            mode,
            "long",
            "100",
            "95",
            "110",
            "115",
            "120",
            "3",
            "Invalid if structure fails.",
            grade,
            "A 88",
            "Research fixture only.",
            "{}",
        ),
    )


def _insert_replay(connection, run_id, symbol, mode, regime, outcome, filled, tp_hit, sl_hit, final_r) -> None:
    connection.execute(
        """
        INSERT INTO replay_results (
            run_id, setup_fingerprint, outcome, filled, tp_hit, sl_hit, final_r,
            time_in_trade, regime, symbol, mode, raw_result_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            f"{run_id}_{symbol}_{mode}",
            outcome,
            filled,
            tp_hit,
            sl_hit,
            final_r,
            "12",
            regime,
            symbol,
            mode,
            "{}",
        ),
    )


def test_missing_database_handling(tmp_path) -> None:
    with pytest.raises(ResearchDatabaseMissing, match=MISSING_SCAN_DATABASE_MESSAGE):
        build_research_report(tmp_path / "missing.db", query="summary")


def test_summary_query(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    _seed_research_database(db_path)

    report = build_research_report(db_path, query="summary")

    assert report["summary"]["total_scan_runs"] == 2
    assert report["summary"]["total_symbols_scanned"] == 5
    assert report["summary"]["total_valid_setups"] == 2
    assert report["summary"]["total_near_misses"] == 1
    assert report["summary"]["total_rejected"] == 1
    assert report["summary"]["total_replay_outcomes"] == 2
    assert report["summary"]["most_common_regime"] == "trend_expansion"


def test_best_symbols_query(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    _seed_research_database(db_path)

    report = build_research_report(db_path, query="best_symbols")

    assert report["symbols"][0]["symbol"] == "BTCUSDT"
    assert report["symbols"][0]["valid_setups"] == 1
    assert report["symbols"][0]["replay_expectancy_r"] == 2


def test_rejection_reasons_query(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    _seed_research_database(db_path)

    report = build_research_report(db_path, query="rejection_reasons")

    gates = {row["failed_gate"]: row for row in report["reasons"]}
    assert gates["missing_confirmed_sweep"]["count"] == 1
    assert gates["not_enough_candles"]["affected_symbols"] == ["XRPUSDT"]
    assert "Required public data" in gates["not_enough_candles"]["possible_interpretation"]


def test_pullback_failure_research_query(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    _seed_research_database(db_path)

    report = build_research_report(db_path, query="pullback_failures")

    failure_counts = {row["pullback_failure_type"]: row["count"] for row in report["failure_type_counts"]}
    lifecycle_counts = {
        row["lifecycle_current_state"]: row["most_common_failure_type"]
        for row in report["failure_by_lifecycle_state"]
    }
    assert report["total_pullback_failures"] == 2
    assert failure_counts["TOO_DEEP"] == 1
    assert failure_counts["NO_OB_FVG"] == 1
    assert lifecycle_counts["INVALIDATED"] == "TOO_DEEP"
    assert report["conversion_rate_by_pullback_grade"]


def test_symbol_detail_query(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    _seed_research_database(db_path)

    report = build_research_report(
        db_path,
        query="symbol_detail",
        filters=ResearchFilters(symbol="ETHUSDT"),
    )

    assert report["scan_count"] == 2
    assert report["valid_setup_count"] == 1
    assert report["near_miss_count"] == 1
    assert report["most_common_failed_gate"] == "trust_meter_below_minimum"
    assert report["recent_history"][0]["display_bucket"] == "valid"


def test_json_output_from_cli_does_not_scan(tmp_path, monkeypatch, capsys) -> None:
    db_path = tmp_path / "research.db"
    output_path = tmp_path / "research.json"
    _seed_research_database(db_path)

    def fail_scanner(*args, **kwargs):
        raise AssertionError("research command should not run scanner")

    monkeypatch.setattr(run_scan, "ScannerRunner", fail_scanner)

    asyncio.run(
        run_scan.main(
            [
                "--research",
                "--research-query",
                "summary",
                "--database-path",
                str(db_path),
                "--research-output-json",
                str(output_path),
            ]
        )
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    captured = capsys.readouterr()
    assert payload["query"] == "summary"
    assert payload["summary"]["total_symbols_scanned"] == 5
    assert f"Exported research report: {output_path}" in captured.out


def test_sample_size_warning(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    _seed_research_database(db_path)

    report = build_research_report(db_path, query="replay_expectancy")
    text = format_research_report(report)

    assert SAMPLE_SIZE_WARNING in report["warnings"]
    assert SAMPLE_SIZE_WARNING in text


def test_filters_by_symbol_mode_and_regime(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    _seed_research_database(db_path)

    symbol_report = build_research_report(
        db_path,
        query="summary",
        filters=ResearchFilters(symbol="ETHUSDT"),
    )
    mode_report = build_research_report(
        db_path,
        query="summary",
        filters=ResearchFilters(mode="challenge"),
    )
    regime_report = build_research_report(
        db_path,
        query="summary",
        filters=ResearchFilters(regime="chop"),
    )

    assert symbol_report["summary"]["total_symbols_scanned"] == 2
    assert symbol_report["summary"]["total_valid_setups"] == 1
    assert symbol_report["summary"]["total_near_misses"] == 1
    assert mode_report["summary"]["total_symbols_scanned"] == 2
    assert mode_report["summary"]["total_valid_setups"] == 0
    assert regime_report["summary"]["total_symbols_scanned"] == 2
