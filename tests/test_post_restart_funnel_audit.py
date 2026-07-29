from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
import sqlite3

import pytest

import app.analytics.post_restart_funnel_audit as audit_module
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


def _quiesce_fixture(database: Path) -> None:
    """Checkpoint fixture-only changes so the audit sees a quiescent source."""

    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    connection.close()
    for sidecar in (Path(f"{database}-wal"), Path(f"{database}-shm")):
        sidecar.unlink(missing_ok=True)
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


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

    safety = report["audit_identity"]["read_only_status"]
    assert safety["status"] == "VERIFIED_READ_ONLY"
    assert safety["sqlite_uri_mode"] == "ro"
    assert safety["source_mode"] == "QUIESCENT_IMMUTABLE"
    assert safety["immutable_requested"] is True
    assert safety["query_only_verified"] is True
    assert safety["sidecars_absent_before"] is True
    assert safety["sidecars_absent_after"] is True
    assert safety["source_metadata_unchanged"] is True
    assert (database.stat().st_size, database.stat().st_mtime_ns, _sha256(database)) == before
    assert not database.with_name(f"{database.name}-journal").exists()
    assert not Path(f"{database}-wal").exists()
    assert not Path(f"{database}-shm").exists()


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
            "--source-mode",
            "quiescent-immutable",
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


def test_quiescent_immutable_audit_reports_verified_source_safety(tmp_path: Path) -> None:
    report = _report(_fixture_database(tmp_path))
    safety = report["audit_identity"]["read_only_status"]

    assert safety["sqlite_uri_mode"] == "ro"
    assert safety["query_only_verified"] is True
    assert safety["immutable_requested"] is True
    assert safety["live_mutable_source"] is False
    assert safety["source_mode"] == "QUIESCENT_IMMUTABLE"


def test_existing_sqlite_sidecar_refuses_before_audit_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = _fixture_database(tmp_path)
    Path(f"{database}-wal").write_bytes(b"sidecar-present")

    def must_not_connect(*args, **kwargs):
        del args, kwargs
        pytest.fail("the audit must refuse a non-quiescent source before sqlite3.connect")

    monkeypatch.setattr(audit_module, "open_read_only_database", must_not_connect)
    with pytest.raises(FunnelAuditError, match="NO-GO"):
        _report(database)


def test_source_change_during_audit_fails_and_cli_writes_no_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    database = _fixture_database(tmp_path)
    original_query_safety = audit_module._verify_query_safety

    def mutate_source(connection, tables):
        result = original_query_safety(connection, tables)
        with sqlite3.connect(database) as writer:
            writer.execute("PRAGMA user_version = 15")
        return result

    monkeypatch.setattr(audit_module, "_verify_query_safety", mutate_source)
    output = tmp_path / "must_not_exist"
    code = audit_post_restart_funnel.main(
        [
            "--database-path", str(database),
            "--source-mode", "quiescent-immutable",
            "--window-start-utc", WINDOW_START,
            "--window-end-utc", WINDOW_END,
            "--expected-watch-interval-sec", "300",
            "--output-dir", str(output),
            "--report-label", "changed-source",
        ]
    )

    assert code == 2
    assert "NO-GO" in capsys.readouterr().err
    assert not output.exists()


def test_72_hour_end_exclusive_window_reports_exactly_864_cycles(tmp_path: Path) -> None:
    report = build_post_restart_funnel_report(
        _fixture_database(tmp_path),
        window_start_utc="2026-07-29T08:40:54Z",
        window_end_utc="2026-08-01T08:40:54Z",
        expected_watch_interval_sec=300,
        report_label="72h",
        generated_at_utc=GENERATED_AT,
    )

    assert report["scan_health"]["expected_scan_cycles"] == 864


def test_missing_critical_funnel_sources_prevent_market_scarcity(tmp_path: Path) -> None:
    database = tmp_path / "missing-critical.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE scan_runs (run_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL, symbols_requested INTEGER, symbols_completed INTEGER)"
        )
        connection.execute("CREATE INDEX ix_scan_runs_timestamp ON scan_runs(timestamp)")
        connection.execute(
            "INSERT INTO scan_runs VALUES ('run-1', '2026-07-29T00:00:00+00:00', 100, 100)"
        )
    report = _report(database)

    labels = {item["label"] for item in report["verdict"]["labels"]}
    assert "DATA_INSUFFICIENT" in labels
    assert "MARKET_SCARCITY" not in labels


def test_single_telegram_failure_does_not_invent_delivery_blocker(tmp_path: Path) -> None:
    report = _report(_fixture_database(tmp_path))

    assert report["telegram_delivery_funnel"]["failed"]["event_count"] == 1
    assert "DELIVERY_BLOCKER" not in {item["label"] for item in report["verdict"]["labels"]}
    assert report["verdict"]["delivery_blocker_policy"].startswith("NOT_RECORDED")


def test_unrelated_2h_timeframe_value_does_not_verify_structure(tmp_path: Path) -> None:
    database = _fixture_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE scan_runs SET timeframes_json = ? WHERE run_id = 'run-1'",
            (json.dumps({"htf_timeframe": "2d", "unrelated_metric_timeframe": "2h"}),),
        )
    connection.close()
    _quiesce_fixture(database)
    report = _report(database)

    structure = report["timeframe_verification"]["timeframes"]["2H_structure"]
    assert structure["status"] == "NOT_VERIFIABLE"
    assert any("unrelated_metric_timeframe" in item for item in structure["exact_evidence"])


def test_missing_index_support_refuses_source_without_large_table_scan(tmp_path: Path) -> None:
    database = tmp_path / "unindexed.sqlite"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE scan_runs (run_id TEXT PRIMARY KEY, timestamp TEXT NOT NULL)")
        connection.execute("INSERT INTO scan_runs VALUES ('run-1', '2026-07-29T00:00:00+00:00')")
    report = _report(database)

    evidence = report["data_coverage_and_reliability"]["query_performance_safeguards"]["query_plan_index_verification"]
    assert evidence["scan_runs_timestamp"]["status"] == "NOT_VERIFIABLE"
    assert report["scan_health"]["observed_scan_cycles"] == 0


def test_unused_large_payload_columns_are_not_selected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _fixture_database(tmp_path)
    captured_sql: list[str] = []
    original_query_rows = audit_module._query_rows

    def spy_query_rows(*args, **kwargs):
        captured_sql.append(str(args[1]))
        return original_query_rows(*args, **kwargs)

    monkeypatch.setattr(audit_module, "_query_rows", spy_query_rows)
    _report(database)

    selected = " ".join(captured_sql).lower()
    assert "derivatives_context_json" not in selected
    assert "volume_profile_context_json" not in selected
    assert "raw_payload_json" not in selected
    assert "metadata_json" not in selected
    assert "raw_result_json" in selected


def test_complete_scan_runs_with_empty_child_sources_cannot_produce_market_scarcity(tmp_path: Path) -> None:
    database = _fixture_database(tmp_path)
    start = datetime(2026, 7, 29, 8, 40, 54, tzinfo=UTC)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM symbol_results")
        connection.execute("DELETE FROM setup_candidates")
        for index in range(864):
            _insert_scan_run(
                connection,
                f"complete-{index:04d}",
                (start + timedelta(seconds=index * 300)).isoformat(),
            )
    connection.close()
    _quiesce_fixture(database)
    report = build_post_restart_funnel_report(
        database,
        window_start_utc="2026-07-29T08:40:54Z",
        window_end_utc="2026-08-01T08:40:54Z",
        expected_watch_interval_sec=300,
        report_label="empty-children",
        generated_at_utc=GENERATED_AT,
    )

    labels = {item["label"] for item in report["verdict"]["labels"]}
    direct = report["scan_health"]["market_scarcity_direct_evidence"]
    assert "MARKET_SCARCITY" not in labels
    assert "DATA_INSUFFICIENT" in labels
    assert direct["completion_cross_check"]["persisted_symbol_result_rows"] == 0
    assert "symbol_results contains no persisted evaluations for observed runs" in direct["market_scarcity_eligibility_issues"]


def test_na_direct_dispositions_cannot_produce_market_scarcity(tmp_path: Path) -> None:
    database = _fixture_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE symbol_results SET status = 'N/A', display_bucket = 'N/A'")
    connection.close()
    _quiesce_fixture(database)
    report = _report(database)

    direct = report["scan_health"]["market_scarcity_direct_evidence"]
    labels = {item["label"] for item in report["verdict"]["labels"]}
    assert direct["direct_disposition_coverage"]["unreliable_disposition_count"] == 3
    assert "MARKET_SCARCITY" not in labels


def test_symbols_completed_mismatch_prevents_market_scarcity(tmp_path: Path) -> None:
    database = _fixture_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE scan_runs SET symbols_completed = 3 WHERE run_id = 'run-1'")
    connection.close()
    _quiesce_fixture(database)
    report = _report(database)

    direct = report["scan_health"]["market_scarcity_direct_evidence"]
    assert direct["completion_cross_check"]["coverage_agrees"] is False
    assert direct["completion_cross_check"]["completion_mismatch_count"] >= 1
    assert "MARKET_SCARCITY" not in {item["label"] for item in report["verdict"]["labels"]}


def test_zero_candidates_and_zero_direct_gates_remain_data_insufficient(tmp_path: Path) -> None:
    database = _fixture_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("DELETE FROM setup_candidates")
        connection.execute("UPDATE symbol_results SET failed_gate = 'N/A', raw_result_json = '{}'")
    connection.close()
    _quiesce_fixture(database)
    report = _report(database)

    assert report["scan_health"]["candidate_row_events"] == 0
    assert report["gate_failures"]["denominator_failure_observations"] == 0
    assert report["scan_health"]["market_scarcity_direct_evidence"]["automatic_market_scarcity_verdict"] == "DATA_INSUFFICIENT"
    assert "MARKET_SCARCITY" not in {item["label"] for item in report["verdict"]["labels"]}
    assert "DATA_INSUFFICIENT" in {item["label"] for item in report["verdict"]["labels"]}


def test_oversized_optional_json_is_not_parsed_and_direct_scalar_evidence_survives(tmp_path: Path) -> None:
    database = _fixture_database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE symbol_results SET raw_result_json = ? WHERE run_id = 'run-1' AND symbol = 'BTCUSDT'",
            (json.dumps({"unused": "x" * (audit_module.MAX_JSON_FIELD_BYTES + 1)}),),
        )
    connection.close()
    _quiesce_fixture(database)
    report = _report(database)

    malformed = report["data_coverage_and_reliability"]["malformed_or_unparseable_records"]
    safeguards = report["data_coverage_and_reliability"]["query_performance_safeguards"]
    assert malformed["symbol_results.raw_result_json_optional_json_oversized"] == 1
    assert "symbol_results.raw_result_json_malformed_json" not in malformed
    assert "symbol_results" in safeguards["optional_json_evidence_unavailable_sources"]
    assert report["gate_failures"]["failure_occurrences_by_normalized_gate"]["target_inside_chop"] >= 1
    assert report["gate_failures"]["optional_raw_json_evidence"].startswith("NOT_VERIFIABLE")
