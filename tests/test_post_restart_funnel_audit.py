from __future__ import annotations

import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from app.analytics.post_restart_funnel_audit import (
    FunnelAuditError,
    IncompatibleFunnelSchemaError,
    build_post_restart_funnel_report,
    render_post_restart_funnel_text,
    write_post_restart_funnel_reports,
)
from app.storage.database import open_initialized_database
from scripts import audit_post_restart_funnel


WINDOW_START = "2026-07-29T00:00:00Z"
WINDOW_END = "2026-07-29T00:10:00Z"
GENERATED_AT = "2026-07-29T01:00:00Z"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _insert_scan_run(connection, run_id: str, timestamp: str, *, malformed_timeframes: bool = False) -> None:
    connection.execute(
        """
        INSERT INTO scan_runs (
            run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
            strategy, timeframes_json, market_regime, runtime_stats_json,
            command_preset, command_used, total_valid_setups, near_misses, rejected,
            data_issues, data_issues_json, raw_payload_json, symbols_requested,
            symbols_completed
        ) VALUES (?, ?, 'binance', 'top_tradable', 2, '["BTCUSDT", "ETHUSDT"]',
            'liquidity_grab_pullback', ?, 'mixed', '{"errors":{"timeout":1}}',
            'runtime', 'run_scan', 0, 0, 2, 0, '[]', '{}', 2, 2)
        """,
        (
            run_id,
            timestamp,
            "{bad" if malformed_timeframes else json.dumps(
                {
                    "htf_timeframe": "2d",
                    "bias_timeframe": "12h",
                    "execution_timeframe": "15m",
                    "confirmation_timeframe": "15m",
                }
            ),
        ),
    )


def _insert_symbol_result(
    connection,
    run_id: str,
    symbol: str,
    *,
    failed_gate: str,
    raw_result: str,
) -> None:
    connection.execute(
        """
        INSERT INTO symbol_results (
            run_id, symbol, status, display_bucket, readiness_score,
            setup_quality_score, edge_score, failed_gate, rejection_reason,
            next_trigger_needed, action_label, regime_state,
            derivatives_context_json, volume_profile_context_json, pullback_status,
            portfolio_decision, raw_result_json
        ) VALUES (?, ?, 'rejected', 'REJECTED', 90, 'B+', 'N/A', ?,
            'recorded deterministic gate', 'N/A', 'WAIT', 'mixed', '{}', '{}',
            'N/A', 'not_selected', ?)
        """,
        (run_id, symbol, failed_gate, raw_result),
    )


def _fixture_database(tmp_path: Path, *, malformed: bool = False) -> Path:
    database = tmp_path / "post_restart_fixture.sqlite"
    connection = open_initialized_database(database)
    try:
        _insert_scan_run(connection, "run-1", "2026-07-29T00:00:00+00:00", malformed_timeframes=malformed)
        _insert_scan_run(connection, "run-2", "2026-07-29T00:05:00+00:00")
        _insert_scan_run(connection, "outside", "2026-07-29T00:10:00+00:00")
        target_only = json.dumps(
            {
                "gates_failed": ["target_inside_chop"],
                "mode": "swing",
                "direction": "long",
                "opportunity_score": "92",
                "min_score_for_idea": "80",
                "quality_grade": "B+",
            }
        )
        target_overlap = json.dumps(
            {
                "gates_failed": ["target_inside_chop", "missing_confirmation"],
                "mode": "swing",
                "direction": "long",
                "opportunity_score": "88",
                "min_score_for_idea": "80",
                "quality_grade": "A-",
            }
        )
        _insert_symbol_result(connection, "run-1", "BTCUSDT", failed_gate="target_inside_chop", raw_result=target_only)
        _insert_symbol_result(connection, "run-1", "ETHUSDT", failed_gate="target_inside_chop", raw_result=target_overlap)
        _insert_symbol_result(
            connection,
            "run-2",
            "BTCUSDT",
            failed_gate="missing_confirmation",
            raw_result=json.dumps({"gates_failed": ["missing_confirmation"]}),
        )
        _insert_symbol_result(connection, "outside", "DOGEUSDT", failed_gate="target_inside_chop", raw_result=target_only)
        connection.execute(
            """
            INSERT INTO setup_candidates (
                run_id, symbol, mode, direction, entry, stop, tp1, tp2, tp3, rr,
                invalidation, quality_grade, trust_meter, risk_warning, raw_candidate_json
            ) VALUES (
                'run-1', 'BTCUSDT', 'swing', 'long', '100', '95', '105', '110', '115',
                '3', 'Invalid below 95.', 'B+', 'verified', 'Risk capital only.', '{}'
            )
            """
        )
        for lifecycle_id, symbol, state in (
            ("life-btc", "BTCUSDT", "STALKING"),
            ("life-eth", "ETHUSDT", "CONFIRMED"),
        ):
            connection.execute(
                """
                INSERT INTO setup_lifecycle_records (
                    lifecycle_id, symbol, mode, direction, current_state, first_seen_at,
                    last_seen_at, last_transition_at
                ) VALUES (?, ?, 'swing', 'long', ?, '2026-07-29T00:00:00+00:00',
                    '2026-07-29T00:05:00+00:00', '2026-07-29T00:05:00+00:00')
                """,
                (lifecycle_id, symbol, state),
            )
        events = (
            ("life-btc", "2026-07-29T00:00:00+00:00", "BTCUSDT", "DISCOVERED", "WATCHLISTED"),
            ("life-btc", "2026-07-29T00:01:00+00:00", "BTCUSDT", "WATCHLISTED", "WATCHLISTED"),
            ("life-btc", "2026-07-29T00:05:00+00:00", "BTCUSDT", "WATCHLISTED", "STALKING"),
            ("life-eth", "2026-07-29T00:00:00+00:00", "ETHUSDT", "DISCOVERED", "WATCHLISTED"),
            ("life-eth", "2026-07-29T00:05:00+00:00", "ETHUSDT", "WATCHLISTED", "TRIGGERED"),
            ("life-eth", "2026-07-29T00:06:00+00:00", "ETHUSDT", "TRIGGERED", "CONFIRMED"),
        )
        connection.executemany(
            """
            INSERT INTO setup_lifecycle_events (
                lifecycle_id, timestamp, symbol, from_state, to_state, reason, scan_run_id
            ) VALUES (?, ?, ?, ?, ?, 'recorded_test_event', 'run-1')
            """,
            events,
        )
        connection.execute(
            """
            INSERT INTO setup_lifecycle_outcome_progress (
                lifecycle_id, plan_identity, symbol, tp1_at, first_evaluated_at,
                last_evaluated_at, terminal_outcome
            ) VALUES (
                'life-btc', 'plan-btc', 'BTCUSDT', '2026-07-29T00:06:00+00:00',
                '2026-07-29T00:00:00+00:00', '2026-07-29T00:06:00+00:00', 'N/A'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
                sent_at, attempted_at, telegram_status, message_hash, scan_run_id,
                dedupe_status, dedupe_reason
            ) VALUES (
                'signal-sent', 'BTCUSDT', 'long', 'WATCHLISTED', 'WATCHLIST',
                'WATCHLISTED', '2026-07-29T00:01:00+00:00', '2026-07-29T00:01:00+00:00',
                'sent', 'hash-sent', 'run-1', 'N/A', 'N/A'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO telegram_alert_attempts (
                signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
                attempted_at, telegram_status, message_hash, scan_run_id,
                dedupe_status, dedupe_reason, error_message
            ) VALUES (
                'signal-failed', 'ETHUSDT', 'long', 'CONFIRMED', 'IDEA', 'CONFIRMED',
                '2026-07-29T00:06:00+00:00', 'failed', 'hash-failed', 'run-2',
                'suppressed', 'cooldown_duplicate', 'timeout token=do-not-report'
            )
            """
        )
        connection.commit()
    finally:
        connection.close()
    return database


def _report(database: Path) -> dict:
    return build_post_restart_funnel_report(
        database,
        window_start_utc=WINDOW_START,
        window_end_utc=WINDOW_END,
        expected_watch_interval_sec=300,
        report_label="fixture",
        generated_at_utc=GENERATED_AT,
        stall_threshold_sec=300,
    )


def test_read_only_audit_preserves_fixture_database_hash_and_metadata(tmp_path: Path) -> None:
    database = _fixture_database(tmp_path)
    before = (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database))

    report = _report(database)

    assert report["audit_identity"]["read_only_status"]["status"] == "VERIFIED_READ_ONLY"
    assert report["audit_identity"]["read_only_status"]["sqlite_uri_mode"] == "ro"
    assert (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database)) == before
    assert not database.with_name(f"{database.name}-journal").exists()


def test_window_is_start_inclusive_end_exclusive_and_uses_no_outside_rows(tmp_path: Path) -> None:
    report = _report(_fixture_database(tmp_path))

    assert report["scan_health"]["observed_scan_cycles"] == 2
    assert report["scan_health"]["distinct_symbols_evaluated"] == 2
    assert report["scan_health"]["total_symbol_evaluations"] == 3
    assert report["gate_failures"]["failure_occurrences_by_normalized_gate"]["target_inside_chop"] == 2
    assert report["target_inside_chop_review"]["occurrences"] == 2


def test_empty_window_and_missing_optional_tables_are_reported(tmp_path: Path) -> None:
    empty = tmp_path / "minimal.sqlite"
    with sqlite3.connect(empty) as connection:
        connection.execute("CREATE TABLE scan_runs (run_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL)")
    report = _report(empty)

    assert report["scan_health"]["observed_scan_cycles"] == 0
    assert "symbol_results" in report["data_coverage_and_reliability"]["missing_tables"]
    assert report["verdict"]["labels"][0]["label"] == "DATA_INSUFFICIENT"


def test_malformed_optional_json_is_explicitly_recorded(tmp_path: Path) -> None:
    report = _report(_fixture_database(tmp_path, malformed=True))

    malformed = report["data_coverage_and_reliability"]["malformed_or_unparseable_records"]
    assert malformed["scan_runs.timeframes_json_malformed_json"] >= 1


def test_event_counts_are_separate_from_unique_setups_and_gate_overlap_is_explicit(tmp_path: Path) -> None:
    report = _report(_fixture_database(tmp_path))
    watch = next(stage for stage in report["funnel"]["stages"] if stage["stage"] == "WATCH")

    assert watch["event_count"] == 3
    assert watch["unique_setup_count"] == 2
    assert report["lifecycle_quality"]["repeated_states"] == 1
    assert report["gate_failures"]["exclusive_failures_by_gate"]["target_inside_chop"] == 1
    assert report["gate_failures"]["overlapping_failure_observations"] == 1
    assert report["target_inside_chop_review"]["exclusive_occurrences"] == 1
    assert report["target_inside_chop_review"]["overlapping_occurrences"] == 1


def test_open_setups_are_not_losses_and_telegram_delivery_is_not_eligibility(tmp_path: Path) -> None:
    report = _report(_fixture_database(tmp_path))

    assert report["outcomes"]["outcome_counts"]["TP1"]["unique_setup_count"] == 1
    assert report["outcomes"]["unresolved_open_setups"]["unique_setup_count"] == 2
    assert report["telegram_delivery_funnel"]["eligible"].startswith("NOT_RECORDED")
    assert report["telegram_delivery_funnel"]["delivered"]["event_count"] == 1
    assert report["telegram_delivery_funnel"]["failed"]["event_count"] == 1
    assert report["duplicate_and_cooldown_behavior"]["duplicate_detections"]["count"] == 1
    assert report["duplicate_and_cooldown_behavior"]["cooldown_suppressions"]["count"] == 1
    assert "do-not-report" not in json.dumps(report)


def test_reports_are_deterministic_and_cli_writes_both_formats(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    database = _fixture_database(tmp_path)
    first = _report(database)
    second = _report(database)

    assert first == second
    assert render_post_restart_funnel_text(first) == render_post_restart_funnel_text(second)
    output = tmp_path / "reports"
    text_path, json_path = write_post_restart_funnel_reports(first, output)
    assert text_path.read_text(encoding="utf-8") == render_post_restart_funnel_text(first)
    assert json.loads(json_path.read_text(encoding="utf-8"))["scan_health"]["observed_scan_cycles"] == 2
    cli_output = tmp_path / "cli_reports"
    code = audit_post_restart_funnel.main(
        [
            "--database-path",
            str(database),
            "--window-start-utc",
            WINDOW_START,
            "--window-end-utc",
            WINDOW_END,
            "--expected-watch-interval-sec",
            "300",
            "--output-dir",
            str(cli_output),
            "--report-label",
            "cli-fixture",
        ]
    )
    assert code == 0
    assert "Text report:" in capsys.readouterr().out


def test_incompatible_schema_and_invalid_window_fail_usefully(tmp_path: Path) -> None:
    database = tmp_path / "incompatible.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")

    with pytest.raises(IncompatibleFunnelSchemaError, match="scan_runs"):
        _report(database)
    with pytest.raises(FunnelAuditError, match="later"):
        build_post_restart_funnel_report(
            _fixture_database(tmp_path / "valid"),
            window_start_utc=WINDOW_END,
            window_end_utc=WINDOW_START,
            expected_watch_interval_sec=300,
            report_label="invalid",
        )
