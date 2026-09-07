from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from app.analytics.evidence_baseline_audit import (
    EvidenceAuditError,
    KNOWN_LIVE_PATHS,
    LIVE_RUNTIME_BASENAME,
    build_evidence_baseline,
    dumps_evidence_payload,
    open_evidence_audit_database,
    reject_protected_database_path,
    separator_agnostic_path_parts,
)
from app.analytics.evidence_contract import UNAVAILABLE, UNSAFE
from app.analytics.evidence_time import EvidenceTimestampError, parse_aware_utc_timestamp, utc_iso
from app.storage import database as database_module
from scripts import audit_evidence_baseline


START = "2026-09-01T00:00:00Z"
CUTOFF = "2026-09-03T00:00:00Z"


def _write_sql(path: Path, statements: str) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(statements)
        connection.commit()


def _full_fixture(path: Path) -> None:
    _write_sql(
        path,
        """
        CREATE TABLE scan_runs (
            run_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            symbols_requested INTEGER,
            symbols_queued INTEGER,
            symbols_completed INTEGER,
            symbols_scanned INTEGER,
            total_valid_setups INTEGER,
            near_misses INTEGER,
            rejected INTEGER,
            data_issues INTEGER,
            valid_activations INTEGER,
            still_watching INTEGER,
            actionable_setups INTEGER,
            confirmed_setups INTEGER,
            actionable_a_grade_setups INTEGER,
            candidate_a_grade_setups INTEGER,
            blocked_a_grade_by_scoring INTEGER,
            blocked_a_grade_by_target INTEGER,
            blocked_a_grade_by_entry_window INTEGER,
            blocked_a_grade_by_trust INTEGER,
            fatal_target_blocks INTEGER,
            soft_target_warnings INTEGER
        );
        CREATE TABLE symbol_results (
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL
        );
        CREATE TABLE setup_candidates (
            run_id TEXT NOT NULL,
            symbol TEXT NOT NULL
        );
        CREATE TABLE replay_results (
            run_id TEXT NOT NULL,
            setup_fingerprint TEXT NOT NULL
        );
        CREATE TABLE setup_lifecycle_records (
            lifecycle_id TEXT PRIMARY KEY,
            symbol TEXT,
            mode TEXT,
            direction TEXT,
            current_state TEXT,
            setup_identity TEXT,
            last_seen_at TEXT
        );
        CREATE TABLE setup_lifecycle_events (
            event_id INTEGER PRIMARY KEY,
            lifecycle_id TEXT,
            timestamp TEXT
        );
        CREATE TABLE setup_lifecycle_outcome_progress (
            id INTEGER PRIMARY KEY,
            lifecycle_id TEXT,
            plan_identity TEXT,
            terminal_outcome TEXT,
            last_evaluated_at TEXT
        );
        CREATE TABLE setup_outcome_analytics (
            id INTEGER PRIMARY KEY,
            lifecycle_id TEXT,
            final_outcome TEXT,
            created_at TEXT
        );
        CREATE TABLE public_alert_events (
            event_key TEXT,
            canonical_plan_id TEXT,
            status TEXT,
            reserved_at TEXT
        );
        CREATE TABLE telegram_alert_attempts (
            signal_id TEXT,
            alert_type TEXT,
            public_watchlist_plan_id TEXT,
            lifecycle_state TEXT,
            attempted_at TEXT
        );

        INSERT INTO scan_runs (
            run_id, timestamp, symbols_requested, symbols_queued, symbols_completed, symbols_scanned,
            total_valid_setups, near_misses, rejected, data_issues, valid_activations, still_watching,
            actionable_setups, confirmed_setups, actionable_a_grade_setups, candidate_a_grade_setups,
            blocked_a_grade_by_scoring, blocked_a_grade_by_target, blocked_a_grade_by_entry_window,
            blocked_a_grade_by_trust, fatal_target_blocks, soft_target_warnings
        ) VALUES
            ('run-start', '2026-09-01T00:00:00+00:00', 2, 2, 2, 2, 1, 0, 1, 0, 0, 0, 1, 0, 1, 1, 0, 0, 0, 0, 0, 0),
            ('run-mid', '2026-09-02T00:00:00+00:00', 2, 2, 2, 2, 1, 0, 1, 0, 0, 0, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0),
            ('run-cutoff', '2026-09-03T00:00:00+00:00', 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9, 9);

        INSERT INTO symbol_results(run_id, symbol) VALUES
            ('run-start', 'BTCUSDT'),
            ('run-mid', 'BTCUSDT'),
            ('run-cutoff', 'ETHUSDT');
        INSERT INTO setup_candidates(run_id, symbol) VALUES
            ('run-start', 'BTCUSDT'),
            ('run-cutoff', 'ETHUSDT');
        INSERT INTO replay_results(run_id, setup_fingerprint) VALUES
            ('run-start', 'fp-1');

        INSERT INTO setup_lifecycle_records(
            lifecycle_id, symbol, mode, direction, current_state, setup_identity, last_seen_at
        ) VALUES
            ('life-a', 'BTCUSDT', 'swing', 'short', 'CONFIRMED', 'BTCUSDT|swing|short|1|2|3|N/A', '2026-09-01T12:00:00+00:00'),
            ('life-b', 'BTCUSDT', 'scalp', 'short', 'TRIGGERED', 'BTCUSDT|scalp|short|1|2|3|N/A', '2026-09-02T12:00:00+00:00'),
            ('life-generic', 'QNTUSDT', 'swing', 'long', 'WATCHLISTED', 'N/A', '2026-09-02T13:00:00+00:00'),
            ('life-after', 'SOLUSDT', 'swing', 'long', 'CONFIRMED', 'SOLUSDT|swing|long|1|2|3|x', '2026-09-03T00:00:00+00:00');

        INSERT INTO setup_lifecycle_events(lifecycle_id, timestamp) VALUES
            ('life-a', '2026-09-01T12:00:00+00:00'),
            ('life-a', '2026-09-02T12:00:00+00:00'),
            ('life-after', '2026-09-03T00:00:00+00:00');

        INSERT INTO setup_lifecycle_outcome_progress(lifecycle_id, plan_identity, terminal_outcome, last_evaluated_at) VALUES
            ('life-a', 'plan-1', 'TP1_HIT', '2026-09-01T12:00:00+00:00'),
            ('life-a', 'plan-2', 'TP2_HIT', '2026-09-02T12:00:00+00:00'),
            ('life-a', 'plan-3', 'N/A', '2026-09-02T18:00:00+00:00'),
            ('life-after', 'plan-x', 'TP3_HIT', '2026-09-03T00:00:00+00:00');

        INSERT INTO setup_outcome_analytics(lifecycle_id, final_outcome, created_at) VALUES
            ('life-a', 'TP1_HIT', '2026-09-01 12:00:00'),
            ('life-a', 'SL_HIT', '2026-09-02 12:00:00'),
            ('life-after', 'TP3_HIT', '2026-09-03 00:00:00');

        INSERT INTO public_alert_events(event_key, canonical_plan_id, status, reserved_at) VALUES
            ('plan|initial_watchlist', 'BTCUSDT|SHORT|abc', 'SENT', '2026-09-01T01:00:00+00:00'),
            ('plan|signal_confirmed', 'N/A', 'RESERVED', '2026-09-02T01:00:00+00:00');
        INSERT INTO telegram_alert_attempts(signal_id, alert_type, public_watchlist_plan_id, lifecycle_state, attempted_at) VALUES
            ('life-a-SETUP-ffff', 'SIGNAL_CONFIRMED', 'BTCUSDT|SHORT|abc', 'CONFIRMED', '2026-09-02T01:00:00+00:00');
        """,
    )


def test_naive_and_invalid_bounds_are_rejected(tmp_path: Path) -> None:
    path = tmp_path / "bounds.sqlite"
    _write_sql(path, "CREATE TABLE scan_runs (run_id TEXT, timestamp TEXT);")
    with pytest.raises(EvidenceTimestampError, match="naive"):
        build_evidence_baseline(path, start="2026-09-01T00:00:00", cutoff=CUTOFF)
    with pytest.raises(EvidenceTimestampError, match="later"):
        build_evidence_baseline(path, start=CUTOFF, cutoff=START)
    parsed = parse_aware_utc_timestamp("2026-09-01T02:00:00+02:00")
    assert utc_iso(parsed) == "2026-09-01T00:00:00Z"


def test_missing_path_fails_and_does_not_create(tmp_path: Path) -> None:
    missing = tmp_path / "does-not-exist.sqlite"
    with pytest.raises(EvidenceAuditError, match="does not exist"):
        build_evidence_baseline(missing, start=START, cutoff=CUTOFF)
    assert not missing.exists()


@pytest.mark.parametrize(
    "path",
    (
        KNOWN_LIVE_PATHS[0],
        Path(r"s:/candlecraftruntime/scan_runs/main_live_runtime.sqlite"),
        Path(r"S:\CandleCraftRuntime\scan_runs\MAIN_LIVE_RUNTIME.SQLITE"),
        Path("scan_runs") / "main_live_runtime.sqlite",
        Path(r"C:\research\main_live_runtime.sqlite"),
        r"C:\research\main_live_runtime.sqlite",
        "C:/research/MAIN_LIVE_RUNTIME.SQLITE",
    ),
)
def test_protected_paths_are_rejected_before_connect(path: Path | str, monkeypatch: pytest.MonkeyPatch) -> None:
    def must_not_connect(*args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("protected path must not open sqlite")

    monkeypatch.setattr(database_module.sqlite3, "connect", must_not_connect)
    with pytest.raises(EvidenceAuditError, match="Protected"):
        reject_protected_database_path(path)
    with pytest.raises(EvidenceAuditError, match="Protected"):
        build_evidence_baseline(path, start=START, cutoff=CUTOFF)
    parts = {part.casefold() for part in separator_agnostic_path_parts(path)}
    assert LIVE_RUNTIME_BASENAME in parts or "candlecraftruntime" in parts


def test_windows_backslash_live_basename_is_rejected_when_posix_pathlib_hides_it() -> None:
    raw = r"C:\research\main_live_runtime.sqlite"
    as_path = Path(raw)
    parts = separator_agnostic_path_parts(as_path)
    assert parts[-1].casefold() == LIVE_RUNTIME_BASENAME
    if as_path.name.casefold() != LIVE_RUNTIME_BASENAME:
        assert "\\" in str(as_path)
    with pytest.raises(EvidenceAuditError, match="Protected"):
        reject_protected_database_path(raw)
    with pytest.raises(EvidenceAuditError, match="Protected"):
        reject_protected_database_path(as_path)


def test_audit_connection_is_query_only_and_omits_immutable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "readonly.sqlite"
    _full_fixture(path)
    connect_call: dict[str, object] = {}
    real_connect = database_module.sqlite3.connect

    def wrapped_connect(*args: object, **kwargs: object):
        connect_call["database"] = args[0]
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(database_module.sqlite3, "connect", wrapped_connect)
    payload = build_evidence_baseline(path, start=START, cutoff=CUTOFF)
    assert "mode=ro" in str(connect_call["database"])
    assert "immutable=1" not in str(connect_call["database"])
    with open_evidence_audit_database(path) as connection:
        with pytest.raises(sqlite3.Error):
            connection.execute("INSERT INTO scan_runs(run_id, timestamp) VALUES ('x', '2026-09-01T00:00:00+00:00')")
        query_only = connection.execute("PRAGMA query_only").fetchone()[0]
    assert query_only == 1
    assert payload["database"]["read_only"]["immutable"] is False


def test_fixture_bytes_and_schema_unchanged_after_audit(tmp_path: Path) -> None:
    path = tmp_path / "frozen.sqlite"
    _full_fixture(path)
    before = path.read_bytes()
    digest = hashlib.sha256(before).hexdigest()
    first = dumps_evidence_payload(build_evidence_baseline(path, start=START, cutoff=CUTOFF))
    second = dumps_evidence_payload(build_evidence_baseline(path, start=START, cutoff=CUTOFF))
    assert first == second
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    assert path.read_bytes() == before


def test_window_is_half_open_and_counts_stay_distinct(tmp_path: Path) -> None:
    path = tmp_path / "window.sqlite"
    _full_fixture(path)
    payload = build_evidence_baseline(path, start=START, cutoff=CUTOFF)
    assert payload["window"]["bound"] == "[start, cutoff)"
    assert payload["coverage"]["scan_runs"]["rows_in_window"] == 2
    assert payload["scan_run_counters"]["symbols_requested"]["value"] == 4
    assert payload["observation_counts"]["symbol_results_in_window_runs"]["value"] == 2
    assert payload["observation_counts"]["setup_candidates_in_window_runs"]["value"] == 1
    assert payload["count_distinctions"]["observation_count"]["value"] == 2
    assert payload["count_distinctions"]["candidate_count"]["value"] == 1
    assert payload["count_distinctions"]["outcome_progress_row_count"]["value"] == 3
    assert payload["count_distinctions"]["unique_trade_count"]["status"] == UNAVAILABLE
    assert payload["scan_run_counters"]["valid_activations"]["status"] == UNSAFE
    assert payload["scan_run_counters"]["valid_activations"]["economic_research_metric"] is False
    assert payload["activation_accounting"]["watch_alert_activations"]["status"] == UNAVAILABLE
    assert payload["activation_accounting"]["watch_alert_activations"]["value"] is None
    assert payload["activation_accounting"]["entry_activated_event_records"]["status"] == UNAVAILABLE
    assert payload["activation_accounting"]["fill_occurrence_count"]["status"] == UNAVAILABLE
    assert payload["activation_accounting"]["manual_fill_count"]["status"] == UNAVAILABLE
    assert payload["count_distinctions"]["unique_trade_count"]["value"] is None
    assert payload["count_distinctions"]["unique_economic_plan_count"]["status"] == UNSAFE
    assert payload["identity_diagnostics"]["setup_identity"]["generic_or_na_rows"] == 1
    assert payload["identity_diagnostics"]["symbol_mode_direction"]["symbol_direction_pairs_with_multiple_modes"] == 1
    assert payload["join_cardinality"]["outcome_progress_per_lifecycle"]["max_rows_per_lifecycle"] == 3
    assert payload["join_cardinality"]["outcome_analytics_per_lifecycle"]["lifecycles_with_multiple_rows"] == 1
    assert payload["join_cardinality"]["unique_trade_inflation_guard"]["distinct_lifecycle_treated_as_unique_trades"] is False
    assert payload["causality"]["as_of_reconstruction"] == "unsupported"
    assert payload["lifecycle_event_counts"]["current_record_filter_is_not_as_of"] is True
    assert "generated_at" not in payload
    assert "duration_ms" not in payload


def test_missing_tables_are_unavailable_not_zero(tmp_path: Path) -> None:
    path = tmp_path / "partial.sqlite"
    _write_sql(
        path,
        """
        CREATE TABLE scan_runs (
            run_id TEXT PRIMARY KEY,
            timestamp TEXT NOT NULL,
            symbols_requested INTEGER
        );
        INSERT INTO scan_runs VALUES ('run-1', '2026-09-01T00:00:00+00:00', 3);
        """,
    )
    payload = build_evidence_baseline(path, start=START, cutoff=CUTOFF)
    assert payload["observation_counts"]["symbol_results_in_window_runs"]["status"] == UNAVAILABLE
    assert payload["observation_counts"]["symbol_results_in_window_runs"]["value"] is None
    assert payload["scan_run_counters"]["valid_activations"]["status"] == UNAVAILABLE
    assert payload["scan_run_counters"]["valid_activations"]["value"] is None
    assert payload["identity_diagnostics"]["lifecycle_id"]["status"] == UNAVAILABLE
    assert payload["scan_run_counters"]["symbols_requested"]["value"] == 3


def test_cli_requires_explicit_path_and_emits_deterministic_json(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "cli.sqlite"
    _full_fixture(path)
    parser = audit_evidence_baseline.build_parser()
    with pytest.raises(SystemExit):
        parser.parse_args([])
    first = audit_evidence_baseline.main(
        ["--database-path", str(path), "--start", START, "--cutoff", CUTOFF]
    )
    out_first = capsys.readouterr().out
    second = audit_evidence_baseline.main(
        ["--database-path", str(path), "--start", START, "--cutoff", CUTOFF]
    )
    out_second = capsys.readouterr().out
    assert first == 0
    assert second == 0
    assert out_first == out_second
    assert '"audit_version":"cci-evidence-baseline-audit-v2"' in out_first.replace(" ", "")
    refused = audit_evidence_baseline.main(
        [
            "--database-path",
            str(KNOWN_LIVE_PATHS[0]),
            "--start",
            START,
            "--cutoff",
            CUTOFF,
        ]
    )
    assert refused == 2
