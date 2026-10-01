"""F03: prospective research must be a proven Runtime-epoch population."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from app.alerts.telegram_lifecycle import PUBLIC_SIGNAL_MIN_RR
from app.analytics.public_signal_quality import MIN_PUBLIC_SETUP_QUALITY_SCORE, MIN_PUBLIC_SIGNAL_GRADE
from app.lifecycle.eligibility import PUBLIC_WATCHLIST_MIN_RR
from app.research.population import (
    HISTORICAL_MIXED_NON_PROSPECTIVE,
    PROSPECTIVE_EPOCH_SCOPED_RESEARCH,
    ResearchPopulationError,
    lifecycle_event_membership_sql,
    lifecycle_membership_sql,
    setup_candidates_membership_sql,
    symbol_results_membership_sql,
)
from app.research.queries import RESEARCH_QUERIES, ResearchFilters, build_research_report
from app.research.reports import format_research_report
from app.runtime_epoch.models import RUNTIME_EPOCH_CONTRACT_VERSION
from app.storage.database import SCHEMA_VERSION, open_initialized_database, open_read_only_database
from scripts import run_scan
from tests.runtime_epoch_support import SYNTHETIC_EPOCH_ID

EPOCH_A = "f03-epoch-a"
EPOCH_B = "f03-epoch-b"
EPOCH_EMPTY = "f03-epoch-empty"
SHARED_TS = "2026-09-15T12:00:00+00:00"
CUTOFF_A = "2026-01-01T00:00:00Z"
CUTOFF_B = "2026-06-01T00:00:00Z"
LEGACY_BULK = 2500


def _prospective(
    epoch: str | None = None,
    *,
    active: bool = False,
    symbol: str | None = None,
    mode: str | None = None,
    regime: str | None = None,
) -> ResearchFilters:
    return ResearchFilters(
        population_scope=PROSPECTIVE_EPOCH_SCOPED_RESEARCH,
        runtime_epoch_id=epoch,
        resolve_active_runtime_epoch=active,
        symbol=symbol,
        mode=mode,
        regime=regime,
    )


def _seed(db_path: Path) -> None:
    with open_initialized_database(db_path) as connection:
        _insert_epoch(connection, EPOCH_A, CUTOFF_A)
        _insert_epoch(connection, EPOCH_B, CUTOFF_B)
        _insert_epoch(connection, EPOCH_EMPTY, "2026-08-01T00:00:00Z")
        _insert_population(
            connection,
            run_id="legacy-btc",
            symbol="BTCUSDT",
            grade="A",
            score="99",
            final_r="10",
            epoch_id=None,
            origin_id=None,
        )
        _insert_population(
            connection,
            run_id="legacy-eth",
            symbol="ETHUSDT",
            grade="A",
            score="99",
            final_r=None,
            epoch_id=None,
            origin_id=None,
        )
        _insert_population(
            connection,
            run_id="run-a-btc",
            symbol="BTCUSDT",
            grade="B",
            score="61",
            final_r="1",
            epoch_id=EPOCH_A,
            origin_id="origin-a-btc",
        )
        _insert_population(
            connection,
            run_id="run-a-eth",
            symbol="ETHUSDT",
            grade="C",
            score="55",
            final_r=None,
            epoch_id=EPOCH_A,
            origin_id="origin-a-eth",
        )
        _insert_population(
            connection,
            run_id="run-b",
            symbol="BTCUSDT",
            grade="A",
            score="77",
            final_r="-4",
            epoch_id=EPOCH_B,
            origin_id="origin-b",
        )
        _insert_lifecycle(
            connection,
            lifecycle_id="life-legacy",
            symbol="BTCUSDT",
            state="TP_HIT",
            epoch_id=None,
            origin_id=None,
            is_current=1,
        )
        _insert_event(connection, "life-legacy", "BTCUSDT", "WATCHLISTED", "CONFIRMED", "legacy-confirmed", "legacy-btc")
        _insert_event(connection, "life-legacy", "BTCUSDT", "CONFIRMED", "TP_HIT", "legacy-tp", "legacy-btc")
        _insert_lifecycle(
            connection,
            lifecycle_id="life-legacy-eth",
            symbol="ETHUSDT",
            state="WATCHLISTED",
            epoch_id=None,
            origin_id=None,
            is_current=1,
        )
        _insert_event(connection, "life-legacy-eth", "ETHUSDT", "DISCOVERED", "WATCHLISTED", "legacy-eth", "legacy-eth")
        _insert_lifecycle(
            connection,
            lifecycle_id="epoch-a-looking",
            symbol="BTCUSDT",
            state="DISCOVERED",
            epoch_id=None,
            origin_id=None,
            is_current=0,
        )
        _insert_lifecycle(
            connection,
            lifecycle_id="life-stolen-origin",
            symbol="BTCUSDT",
            state="DISCOVERED",
            epoch_id=None,
            origin_id="origin-a-btc",
            is_current=0,
        )
        _insert_lifecycle(
            connection,
            lifecycle_id="life-a-confirmed",
            symbol="BTCUSDT",
            state="CONFIRMED",
            epoch_id=EPOCH_A,
            origin_id="origin-a-btc",
            is_current=0,
        )
        _insert_event(
            connection, "life-a-confirmed", "BTCUSDT", "WATCHLISTED", "CONFIRMED", "epoch-a-confirmed", "run-a-btc"
        )
        _insert_event(
            connection,
            "life-a-confirmed",
            "BTCUSDT",
            "CONFIRMED",
            "CONFIRMED",
            "cross-wired-from-b-run",
            "run-b",
        )
        _insert_lifecycle(
            connection,
            lifecycle_id="life-a-watch",
            symbol="BTCUSDT",
            state="WATCHLISTED",
            epoch_id=EPOCH_A,
            origin_id="origin-a-btc",
            is_current=0,
        )
        _insert_event(connection, "life-a-watch", "BTCUSDT", "DISCOVERED", "WATCHLISTED", "epoch-a-watch", "run-a-btc")
        _insert_lifecycle(
            connection,
            lifecycle_id="life-a-blank-origin",
            symbol="BTCUSDT",
            state="DISCOVERED",
            epoch_id=EPOCH_A,
            origin_id=None,
            is_current=0,
        )
        _insert_event(
            connection, "life-a-blank-origin", "BTCUSDT", "N/A", "DISCOVERED", "epoch-a-direct", "run-a-btc"
        )
        _insert_lifecycle(
            connection,
            lifecycle_id="life-a-conflict",
            symbol="BTCUSDT",
            state="TP_HIT",
            epoch_id=EPOCH_A,
            origin_id="origin-b",
            is_current=1,
        )
        _insert_event(
            connection, "life-a-conflict", "BTCUSDT", "CONFIRMED", "TP_HIT", "conflict-tp-must-stay-out", "run-a-btc"
        )
        _insert_lifecycle(
            connection,
            lifecycle_id="life-a-missing-origin",
            symbol="BTCUSDT",
            state="SL_HIT",
            epoch_id=EPOCH_A,
            origin_id="origin-missing",
            is_current=0,
        )
        _insert_event(
            connection,
            "life-a-missing-origin",
            "BTCUSDT",
            "CONFIRMED",
            "SL_HIT",
            "missing-origin-sl-must-stay-out",
            "run-a-btc",
        )
        _insert_lifecycle(
            connection,
            lifecycle_id="life-b",
            symbol="BTCUSDT",
            state="TP_HIT",
            epoch_id=EPOCH_B,
            origin_id="origin-b",
            is_current=1,
        )
        _insert_event(connection, "life-b", "BTCUSDT", "WATCHLISTED", "CONFIRMED", "epoch-b-confirmed", "run-b")
        _insert_event(connection, "life-b", "BTCUSDT", "CONFIRMED", "TP_HIT", "epoch-b-tp", "run-b")
        _insert_event(
            connection,
            "life-b",
            "BTCUSDT",
            "CONFIRMED",
            "CONFIRMED",
            "cross-wired-from-a-run",
            "run-a-btc",
        )
        connection.execute(
            """
            INSERT INTO symbol_health (symbol, timeout_count, current_health_score, average_runtime_sec)
            VALUES ('BTCUSDT', 7, 40, 12.5)
            """
        )


def _insert_epoch(connection: sqlite3.Connection, epoch_id: str, cutoff: str) -> None:
    connection.execute(
        """
        INSERT INTO runtime_epochs (
            epoch_id, contract_version, activated_at, cutoff_at,
            reviewed_release_sha, generation_binding, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            epoch_id,
            RUNTIME_EPOCH_CONTRACT_VERSION,
            cutoff,
            cutoff,
            "f03-synthetic",
            "f03-proof",
            cutoff,
        ),
    )


def _insert_population(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    symbol: str,
    grade: str,
    score: str,
    final_r: str | None,
    epoch_id: str | None,
    origin_id: str | None,
) -> None:
    connection.execute(
        """
        INSERT INTO scan_runs (
            run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
            strategy, timeframes_json, market_regime, runtime_stats_json,
            command_preset, command_used, total_valid_setups, near_misses, rejected,
            data_issues, data_issues_json, raw_payload_json
        ) VALUES (?, ?, 'binance', 'manual', 1, '[]', 'liquidity_grab_pullback', '{}',
                  'trend_expansion', '{}', 'N/A', 'f03', 1, 0, 0, 0, '[]', '{}')
        """,
        (run_id, SHARED_TS),
    )
    raw = {
        "valid_strategy_modes": ["scalp"],
        "setup_quality": {
            "quality_grade": grade,
            "quality_state": "HIGH_QUALITY_TRADE",
            "quality_score": score,
        },
        "readiness_label": "VALID SETUP",
    }
    connection.execute(
        """
        INSERT INTO symbol_results (
            run_id, symbol, status, display_bucket, readiness_score, setup_quality_score,
            edge_score, failed_gate, rejection_reason, next_trigger_needed, action_label,
            regime_state, derivatives_context_json, volume_profile_context_json,
            pullback_status, portfolio_decision, raw_result_json
        ) VALUES (?, ?, 'scanned', 'valid', 90, ?, 'N/A', 'N/A', 'N/A', 'N/A', 'Watchlist only',
                  'trend_expansion', '{}', '{}', 'N/A', 'N/A', ?)
        """,
        (run_id, symbol, score, json.dumps(raw)),
    )
    connection.execute(
        """
        INSERT INTO setup_candidates (
            run_id, symbol, mode, direction, entry, stop, tp1, tp2, tp3, rr,
            invalidation, quality_grade, trust_meter, risk_warning, raw_candidate_json
        ) VALUES (?, ?, 'scalp', 'long', '100', '95', '110', '115', '120', '3',
                  'Invalid if structure fails.', 'N/A', 'A 88', 'Research fixture only.', '{}')
        """,
        (run_id, symbol),
    )
    if final_r is not None:
        connection.execute(
            """
            INSERT INTO replay_results (
                run_id, setup_fingerprint, outcome, filled, tp_hit, sl_hit, final_r,
                time_in_trade, regime, symbol, mode, raw_result_json
            ) VALUES (?, ?, 'tp', 1, 'TP1', 0, ?, '12', 'trend_expansion', ?, 'scalp', '{}')
            """,
            (run_id, f"{run_id}-{symbol}", final_r, symbol),
        )
    if epoch_id is not None and origin_id is not None:
        connection.execute(
            """
            INSERT INTO runtime_operational_runs (
                run_id, runtime_epoch_id, registered_at, status, producer_started_at
            ) VALUES (?, ?, ?, 'registered', ?)
            """,
            (run_id, epoch_id, SHARED_TS, SHARED_TS),
        )
        connection.execute(
            """
            INSERT INTO runtime_operational_origins (
                origin_id, runtime_epoch_id, run_id, symbol,
                evaluation_completed_at, decision_cutoff_at, producer_observed_at,
                origin_kind, status, block_reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'live_fresh', 'granted', NULL, ?)
            """,
            (origin_id, epoch_id, run_id, symbol, SHARED_TS, SHARED_TS, SHARED_TS, SHARED_TS),
        )


def _insert_lifecycle(
    connection: sqlite3.Connection,
    *,
    lifecycle_id: str,
    symbol: str,
    state: str,
    epoch_id: str | None,
    origin_id: str | None,
    is_current: int,
) -> None:
    connection.execute(
        """
        INSERT INTO setup_lifecycle_records (
            lifecycle_id, symbol, mode, direction, current_state,
            first_seen_at, last_seen_at, last_transition_at,
            regime_state, is_current, runtime_epoch_id, creation_origin_id
        ) VALUES (?, ?, 'scalp', 'long', ?, ?, ?, ?, 'trend_expansion', ?, ?, ?)
        """,
        (lifecycle_id, symbol, state, SHARED_TS, SHARED_TS, SHARED_TS, is_current, epoch_id, origin_id),
    )


def _insert_event(
    connection: sqlite3.Connection,
    lifecycle_id: str,
    symbol: str,
    from_state: str,
    to_state: str,
    reason: str,
    scan_run_id: str,
) -> None:
    connection.execute(
        """
        INSERT INTO setup_lifecycle_events (
            lifecycle_id, timestamp, symbol, from_state, to_state, reason, scan_run_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (lifecycle_id, SHARED_TS, symbol, from_state, to_state, reason, scan_run_id),
    )


def _null_epoch_count(db_path: Path) -> int:
    with open_read_only_database(db_path) as connection:
        row = connection.execute(
            """
            SELECT COUNT(*)
            FROM setup_lifecycle_records
            WHERE runtime_epoch_id IS NULL
            """
        ).fetchone()
    return int(row[0])


def _plan(connection: sqlite3.Connection, sql: str, params: tuple[object, ...]) -> str:
    rows = connection.execute("EXPLAIN QUERY PLAN " + sql, params).fetchall()
    return "\n".join(" | ".join(str(column) for column in row) for row in rows)


def test_unscoped_history_mixes_and_prospective_entry_cannot(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)
    before = _null_epoch_count(db_path)

    historical = build_research_report(db_path, query="summary")
    epoch_a = build_research_report(db_path, query="summary", filters=_prospective(EPOCH_A))
    epoch_b = build_research_report(db_path, query="summary", filters=_prospective(EPOCH_B))

    assert historical["summary"]["total_symbols_scanned"] == 5
    assert historical["population"]["scope_type"] == HISTORICAL_MIXED_NON_PROSPECTIVE
    assert historical["population"]["prospective"] is False
    assert historical["population"]["expectancy_claim"] == "not_a_prospective_claim"
    assert historical["population"]["fallback_to_all_history"] is False
    assert historical["fallback_to_all_history"] is False
    assert "HISTORICAL_MIXED_NON_PROSPECTIVE" in format_research_report(historical)
    assert "prospective_claim=false" in format_research_report(historical)

    assert epoch_a["summary"]["total_symbols_scanned"] == 2
    assert epoch_a["summary"]["total_replay_outcomes"] == 1
    assert epoch_a["population"]["scope_type"] == PROSPECTIVE_EPOCH_SCOPED_RESEARCH
    assert epoch_a["population"]["resolved_runtime_epoch_id"] == EPOCH_A
    assert epoch_a["population"]["requested_runtime_epoch_id"] == EPOCH_A
    assert epoch_a["population"]["membership_rule"] == "durable_lineage_not_timestamp"
    assert epoch_a["population"]["prospective"] is True
    assert epoch_a["population"]["expectancy_claim"] == "prospective_population_only"
    assert epoch_a["fallback_to_all_history"] is False
    assert epoch_b["summary"]["total_symbols_scanned"] == 1
    assert epoch_b["summary"]["average_quality_score"] == 77
    assert epoch_a["summary"]["average_quality_score"] == 58
    assert _null_epoch_count(db_path) == before


def test_symbol_mode_and_time_do_not_admit_legacy_or_other_epochs(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)
    filters = _prospective(EPOCH_A, symbol="BTCUSDT", mode="scalp", regime="trend_expansion")

    summary = build_research_report(db_path, query="summary", filters=filters)
    quality = build_research_report(db_path, query="setup_quality", filters=filters)
    detail = build_research_report(db_path, query="lifecycle_symbol_detail", filters=filters)
    transitions = build_research_report(db_path, query="lifecycle_transitions", filters=filters)

    assert summary["summary"]["total_symbols_scanned"] == 1
    assert summary["summary"]["average_quality_score"] == 61
    assert {row["quality_grade"] for row in quality["quality_grades"]} == {"B"}
    assert quality["total_symbols"] == 1
    lifecycle_ids = {row["lifecycle_id"] for row in detail["lifecycles"]}
    assert lifecycle_ids == {"life-a-confirmed", "life-a-watch", "life-a-blank-origin"}
    assert "life-legacy" not in lifecycle_ids
    assert "life-b" not in lifecycle_ids
    assert "epoch-a-looking" not in lifecycle_ids
    assert "life-stolen-origin" not in lifecycle_ids
    assert "life-a-conflict" not in lifecycle_ids
    assert "life-a-missing-origin" not in lifecycle_ids
    reasons = {row["reason"] for row in transitions["transitions"]}
    assert "cross-wired-from-b-run" in reasons
    assert "cross-wired-from-a-run" not in reasons
    assert "conflict-tp-must-stay-out" not in reasons
    assert "missing-origin-sl-must-stay-out" not in reasons
    assert "legacy-tp" not in reasons
    assert all(row["to_state"] != "TP_HIT" for row in transitions["transitions"])
    cross = next(row for row in transitions["transitions"] if row["reason"] == "cross-wired-from-b-run")
    assert cross["scan_run_id"] == "run-b"


def test_epoch_b_keeps_its_own_lifecycle_and_replay(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)

    conversion = build_research_report(db_path, query="lifecycle_conversion", filters=_prospective(EPOCH_B))
    expectancy = build_research_report(db_path, query="replay_expectancy", filters=_prospective(EPOCH_B))
    transitions = build_research_report(db_path, query="lifecycle_transitions", filters=_prospective(EPOCH_B))

    assert conversion["total_lifecycles"] == 1
    assert conversion["funnel_counts"]["CONFIRMED"] == 1
    assert conversion["funnel_counts"]["TP_HIT"] == 1
    assert conversion["confirmed_outcomes"]["confirmed_count"] == 1
    assert conversion["confirmed_outcomes"]["tp_hit_count"] == 1
    assert conversion["confirmed_outcomes"]["tp_hit_rate_pct"] == 100
    assert expectancy["replay"]["expectancy_r"] == -4
    assert expectancy["replay"]["total_replay_samples"] == 1
    reasons = {row["reason"] for row in transitions["transitions"]}
    assert "cross-wired-from-a-run" in reasons
    assert "cross-wired-from-b-run" not in reasons
    assert "epoch-a-confirmed" not in reasons


def test_denominator_uses_only_the_requested_epoch(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)
    filters = _prospective(EPOCH_A)

    conversion = build_research_report(db_path, query="lifecycle_conversion", filters=filters)
    quality = build_research_report(db_path, query="setup_quality", filters=filters)
    regime = build_research_report(db_path, query="regime_expectancy", filters=filters)
    expectancy = build_research_report(db_path, query="replay_expectancy", filters=filters)

    assert conversion["total_lifecycles"] == 3
    assert conversion["funnel_counts"]["WATCHLISTED"] == 2
    assert conversion["funnel_counts"]["CONFIRMED"] == 1
    assert conversion["funnel_counts"]["TP_HIT"] == 0
    assert conversion["funnel_counts"]["SL_HIT"] == 0
    assert conversion["confirmed_outcomes"]["confirmed_count"] == 1
    assert conversion["confirmed_outcomes"]["tp_hit_count"] == 0
    assert conversion["confirmed_outcomes"]["tp_hit_rate_pct"] == 0
    assert conversion["watchlisted_to_valid"]["watchlisted_count"] == 2
    assert conversion["watchlisted_to_valid"]["valid_count"] == 1
    assert conversion["watchlisted_to_valid"]["conversion_rate_pct"] == 50
    assert {row["quality_grade"] for row in quality["quality_grades"]} == {"B", "C"}
    assert quality["total_symbols"] == 2
    assert regime["regimes"][0]["symbols_scanned"] == 2
    assert regime["regimes"][0]["replay_samples"] == 1
    assert regime["regimes"][0]["expectancy_r"] == 1
    assert expectancy["replay"]["expectancy_r"] == 1
    assert expectancy["replay"]["total_replay_samples"] == 1
    assert conversion["population"]["included"]["lifecycle_records"] == 3
    assert conversion["population"]["included"]["lifecycle_events"] == 4
    assert conversion["population"]["exclusions"]["unresolved_within_requested_epoch"] == 2
    assert conversion["population"]["fallback_to_all_history"] is False


def test_symbol_bounded_census_excludes_legacy_and_other_epoch(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)

    btc = build_research_report(
        db_path,
        query="lifecycle_summary",
        filters=_prospective(EPOCH_A, symbol="BTCUSDT"),
    )
    eth = build_research_report(
        db_path,
        query="summary",
        filters=_prospective(EPOCH_A, symbol="ETHUSDT"),
    )
    unscoped = build_research_report(db_path, query="summary", filters=_prospective(EPOCH_A))

    assert btc["population"]["exclusions"]["census"] == "symbol_bounded"
    assert btc["population"]["exclusions"]["legacy_or_null_epoch"] == 3
    assert btc["population"]["exclusions"]["different_epoch"] == 1
    assert btc["population"]["exclusions"]["unresolved_within_requested_epoch"] == 2
    assert btc["total_lifecycles"] == 3
    assert eth["summary"]["total_symbols_scanned"] == 1
    assert eth["population"]["included"]["lifecycle_records"] == 0
    assert eth["population"]["exclusions"]["legacy_or_null_epoch"] == 1
    assert eth["population"]["exclusions"]["different_epoch"] == 0
    assert unscoped["population"]["exclusions"]["census"] == "not_enumerated"
    assert "legacy_or_null_epoch" not in unscoped["population"]["exclusions"]


def test_historical_symbol_filter_still_mixes_epochs(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)

    historical = build_research_report(
        db_path,
        query="summary",
        filters=ResearchFilters(symbol="BTCUSDT", mode="scalp", regime="trend_expansion"),
    )
    prospective = build_research_report(
        db_path,
        query="summary",
        filters=_prospective(EPOCH_A, symbol="BTCUSDT", mode="scalp", regime="trend_expansion"),
    )

    assert historical["summary"]["total_symbols_scanned"] == 3
    assert historical["population"]["prospective"] is False
    assert prospective["summary"]["total_symbols_scanned"] == 1


def test_empty_and_unknown_epochs_do_not_fall_back(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)
    before = _null_epoch_count(db_path)

    empty = build_research_report(db_path, query="summary", filters=_prospective(EPOCH_EMPTY))
    assert empty["summary"]["total_symbols_scanned"] == 0
    assert empty["summary"]["total_replay_outcomes"] == 0
    assert empty["population"]["included"]["lifecycle_records"] == 0
    assert empty["population"]["resolved_runtime_epoch_id"] == EPOCH_EMPTY
    assert empty["fallback_to_all_history"] is False
    assert empty["summary"]["total_symbols_scanned"] != 5

    with pytest.raises(ResearchPopulationError, match="does not exist"):
        build_research_report(db_path, query="summary", filters=_prospective("f03-missing-epoch"))
    with pytest.raises(ResearchPopulationError, match="exact Runtime epoch or explicit active-epoch"):
        build_research_report(
            db_path,
            query="summary",
            filters=ResearchFilters(population_scope=PROSPECTIVE_EPOCH_SCOPED_RESEARCH),
        )
    with pytest.raises(ResearchPopulationError, match="not both"):
        build_research_report(
            db_path,
            query="summary",
            filters=_prospective(EPOCH_A, active=True),
        )
    with pytest.raises(ResearchPopulationError, match="does not accept a Runtime epoch"):
        build_research_report(
            db_path,
            query="summary",
            filters=ResearchFilters(
                population_scope=HISTORICAL_MIXED_NON_PROSPECTIVE,
                runtime_epoch_id=EPOCH_A,
            ),
        )
    assert _null_epoch_count(db_path) == before


def test_scope_alias_and_whitespace_epoch_id_do_not_widen_the_population(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)
    report = build_research_report(
        db_path,
        query="summary",
        filters=ResearchFilters(population_scope="prospective", runtime_epoch_id=f"  {EPOCH_A}  "),
    )
    assert report["population"]["prospective"] is True
    assert report["population"]["resolved_runtime_epoch_id"] == EPOCH_A
    assert report["summary"]["total_symbols_scanned"] == 2


def test_active_epoch_resolution_is_visible_and_missing_active_fails(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)

    untouched = build_research_report(
        db_path,
        query="summary",
        filters=_prospective(active=True),
    )
    assert untouched["summary"]["total_symbols_scanned"] == 0
    assert untouched["population"]["requested_runtime_epoch_id"] == "ACTIVE"
    assert untouched["population"]["resolved_runtime_epoch_id"] == SYNTHETIC_EPOCH_ID
    assert untouched["population"]["selection"] == "active_runtime_epoch"
    assert untouched["fallback_to_all_history"] is False

    with open_initialized_database(db_path) as connection:
        connection.execute(
            "UPDATE runtime_epoch_control SET epoch_id = ? WHERE control_key = 'active'",
            (EPOCH_A,),
        )

    resolved = build_research_report(db_path, query="summary", filters=_prospective(active=True))
    explicit = build_research_report(db_path, query="summary", filters=_prospective(EPOCH_A))
    assert resolved["summary"]["total_symbols_scanned"] == explicit["summary"]["total_symbols_scanned"] == 2
    assert resolved["population"]["requested_runtime_epoch_id"] == "ACTIVE"
    assert resolved["population"]["resolved_runtime_epoch_id"] == EPOCH_A

    with open_initialized_database(db_path) as connection:
        connection.execute("DELETE FROM runtime_epoch_control")

    with pytest.raises(ResearchPopulationError, match="none is configured"):
        build_research_report(db_path, query="summary", filters=_prospective(active=True))
    still_explicit = build_research_report(db_path, query="summary", filters=_prospective(EPOCH_A))
    assert still_explicit["summary"]["total_symbols_scanned"] == 2
    assert still_explicit["population"]["resolved_runtime_epoch_id"] == EPOCH_A


def test_symbol_health_stays_historical_and_is_not_a_prospective_cohort(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)

    historical = build_research_report(db_path, query="symbol_health")
    prospective = build_research_report(db_path, query="symbol_health", filters=_prospective(EPOCH_A))
    prospective_text = format_research_report(prospective)

    assert historical["symbols"][0]["symbol"] == "BTCUSDT"
    assert historical["population"]["prospective"] is False
    assert prospective["symbols"] == []
    assert prospective["population"]["included"]["symbol_health"] == 0
    assert prospective["population"]["exclusions"]["symbol_health"] == "excluded_no_durable_epoch_lineage"
    assert "no Runtime epoch lineage" in prospective["warnings"][0]
    assert "PROSPECTIVE_EPOCH_SCOPED_RESEARCH" in prospective_text
    assert "prospective_claim=true" in prospective_text


def test_setup_join_stays_on_the_registered_run(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)
    with open_read_only_database(db_path) as connection:
        rows = connection.execute(setup_candidates_membership_sql(1), ("run-a-btc",)).fetchall()
    assert len(rows) == 1
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["setup_quality_score"] == "61"
    assert rows[0]["run_id"] == "run-a-btc"


def test_run_chunking_does_not_drop_or_duplicate_epoch_rows(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)
    monkeypatch.setattr("app.research.queries._RUN_ID_CHUNK", 1)

    report = build_research_report(db_path, query="summary", filters=_prospective(EPOCH_A))

    assert report["summary"]["total_symbols_scanned"] == 2
    assert report["summary"]["total_scan_runs"] == 2
    assert report["population"]["included"]["setup_candidates"] == 2


def test_every_research_query_propagates_scope(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)
    for query in RESEARCH_QUERIES:
        historical = build_research_report(db_path, query=query)
        prospective = build_research_report(db_path, query=query, filters=_prospective(EPOCH_A))
        assert historical["population"]["prospective"] is False, query
        assert historical["population"]["scope_type"] == HISTORICAL_MIXED_NON_PROSPECTIVE, query
        assert historical["fallback_to_all_history"] is False, query
        assert prospective["population"]["prospective"] is True, query
        assert prospective["population"]["resolved_runtime_epoch_id"] == EPOCH_A, query
        assert prospective["population"]["fallback_to_all_history"] is False, query
        assert prospective["fallback_to_all_history"] is False, query
        assert "PROSPECTIVE_EPOCH_SCOPED_RESEARCH" in format_research_report(prospective)


def test_cli_defaults_remain_historical_and_prospective_is_explicit(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "mixed.db"
    output_path = tmp_path / "research.json"
    _seed(db_path)

    def fail_scanner(*args: object, **kwargs: object) -> None:
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
    historical = json.loads(output_path.read_text(encoding="utf-8"))
    assert historical["summary"]["total_symbols_scanned"] == 5
    assert historical["population"]["scope_type"] == HISTORICAL_MIXED_NON_PROSPECTIVE

    prospective_path = tmp_path / "prospective.json"
    asyncio.run(
        run_scan.main(
            [
                "--research",
                "--research-population",
                "prospective",
                "--research-epoch",
                EPOCH_A,
                "--research-query",
                "summary",
                "--database-path",
                str(db_path),
                "--research-output-json",
                str(prospective_path),
            ]
        )
    )
    prospective = json.loads(prospective_path.read_text(encoding="utf-8"))
    assert prospective["summary"]["total_symbols_scanned"] == 2
    assert prospective["population"]["resolved_runtime_epoch_id"] == EPOCH_A
    assert prospective["fallback_to_all_history"] is False

    with pytest.raises(SystemExit):
        run_scan.parse_args(["--research", "--research-epoch", EPOCH_A])


def test_query_plan_is_bounded_and_legacy_growth_does_not_change_results(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)
    before = build_research_report(db_path, query="summary", filters=_prospective(EPOCH_A))
    before_lifecycle = build_research_report(db_path, query="lifecycle_summary", filters=_prospective(EPOCH_A))

    with open_initialized_database(db_path) as connection:
        connection.execute(
            """
            INSERT INTO scan_runs (
                run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
                strategy, timeframes_json, market_regime, runtime_stats_json,
                command_preset, command_used, total_valid_setups, near_misses, rejected,
                data_issues, data_issues_json, raw_payload_json
            ) VALUES ('legacy-bulk', ?, 'binance', 'manual', ?, '[]', 'liquidity_grab_pullback', '{}',
                      'trend_expansion', '{}', 'N/A', 'f03', 0, 0, ?, 0, '[]', '{}')
            """,
            (SHARED_TS, LEGACY_BULK, LEGACY_BULK),
        )
        connection.executemany(
            """
            INSERT INTO symbol_results (
                run_id, symbol, status, display_bucket, readiness_score, setup_quality_score,
                edge_score, failed_gate, rejection_reason, next_trigger_needed, action_label,
                regime_state, derivatives_context_json, volume_profile_context_json,
                pullback_status, portfolio_decision, raw_result_json
            ) VALUES ('legacy-bulk', ?, 'scanned', 'valid', 1, '99', 'N/A', 'N/A', 'N/A', 'N/A',
                      'N/A', 'trend_expansion', '{}', '{}', 'N/A', 'N/A', '{}')
            """,
            [(f"BULK{index:05d}USDT",) for index in range(LEGACY_BULK)],
        )
        connection.executemany(
            """
            INSERT INTO setup_lifecycle_records (
                lifecycle_id, symbol, mode, direction, current_state,
                first_seen_at, last_seen_at, last_transition_at, regime_state, is_current
            ) VALUES (?, 'BTCUSDT', 'scalp', 'long', 'TP_HIT', ?, ?, ?, 'trend_expansion', 0)
            """,
            [(f"legacy-bulk-{index}", SHARED_TS, SHARED_TS, SHARED_TS) for index in range(LEGACY_BULK)],
        )
        symbol_plan = _plan(connection, symbol_results_membership_sql(1), ("run-a-btc",))
        lifecycle_plan = _plan(connection, lifecycle_membership_sql(), (EPOCH_A,))
        event_plan = _plan(connection, lifecycle_event_membership_sql(), (EPOCH_A,))

    after = build_research_report(db_path, query="summary", filters=_prospective(EPOCH_A))
    after_lifecycle = build_research_report(db_path, query="lifecycle_summary", filters=_prospective(EPOCH_A))
    historical = build_research_report(db_path, query="summary")

    assert after["summary"] == before["summary"]
    assert after_lifecycle["total_lifecycles"] == before_lifecycle["total_lifecycles"] == 3
    assert historical["summary"]["total_symbols_scanned"] == 5 + LEGACY_BULK
    assert "ix_symbol_results_run_id" in symbol_plan
    assert "SCAN symbol_results" not in symbol_plan
    assert "SCAN sr" not in symbol_plan
    assert "ix_lifecycle_records_runtime_epoch" in lifecycle_plan
    assert "SCAN setup_lifecycle_records" not in lifecycle_plan
    assert "ix_lifecycle_records_runtime_epoch" in event_plan
    assert SCHEMA_VERSION == 26


def test_strategy_gates_and_schema_are_unchanged(tmp_path: Path) -> None:
    db_path = tmp_path / "mixed.db"
    _seed(db_path)
    build_research_report(db_path, query="summary", filters=_prospective(EPOCH_A))
    with open_read_only_database(db_path) as connection:
        version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
        }
    assert version == 26
    assert SCHEMA_VERSION == 26
    assert "research_population_scope" not in tables
    assert MIN_PUBLIC_SETUP_QUALITY_SCORE == Decimal("88")
    assert MIN_PUBLIC_SIGNAL_GRADE == "A"
    assert PUBLIC_SIGNAL_MIN_RR == Decimal("3")
    assert PUBLIC_WATCHLIST_MIN_RR == Decimal("3")
