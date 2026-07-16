from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.analytics.public_alert_funnel import (
    build_near_miss_key,
    build_public_alert_funnel_report,
    classify_block_stage,
    format_public_alert_funnel_report,
    is_otherwise_publishable_near_miss,
    normalize_public_block_reasons,
)
from app.storage.database import initialize_database
from scripts import audit_public_alert_funnel


@pytest.mark.parametrize(
    ("raw_reason", "expected"),
    (
        ("public_block_below_quality_score", ["LOW_SCORE"]),
        ("public_block_rr_below_3", ["RR_BELOW_MIN"]),
        ("public_block_target_caution_score_below_88", ["TARGET_CAUTION_SCORE_BELOW_88"]),
        ("public_block_target_caution_rr_below_2_8", ["TARGET_CAUTION_RR_BELOW_2_8"]),
        (
            "target_integrity_failed:invalid_target_fields=tp_order",
            ["TARGET_INTEGRITY_BLOCKED", "INVALID_TP_SEQUENCE"],
        ),
        ("public_block_non_crypto_symbol", ["NON_CRYPTO_SYMBOL"]),
        ("terminal_update_no_prior_public_alert", ["TERMINAL_UPDATE_NO_PRIOR_PUBLIC_ALERT"]),
        ("derivatives_conflict", ["DERIVATIVES_CONFLICT"]),
        ("", ["UNKNOWN_PUBLIC_BLOCK"]),
        ("not_a_known_public_reason", ["UNKNOWN_PUBLIC_BLOCK"]),
    ),
)
def test_normalize_public_block_reasons(raw_reason: str, expected: list[str]) -> None:
    assert normalize_public_block_reasons(raw_reason) == expected


@pytest.mark.parametrize(
    ("raw_reason", "expected_stage"),
    (
        ("public_block_below_quality_score", "SCORE_GATE"),
        ("public_block_rr_below_3", "RR_GATE"),
        ("public_block_target_caution_score_below_88", "TARGET_CAUTION_GATE"),
        ("target_integrity_failed:invalid_target_fields=tp_order", "TARGET_INTEGRITY_GATE"),
        ("public_block_non_crypto_symbol", "SYMBOL_UNIVERSE_GATE"),
        ("public_block_non_public_terminal_state", "LIFECYCLE_PUBLIC_STATE_GATE"),
        ("terminal_update_no_prior_public_alert", "TERMINAL_UPDATE_GATE"),
        ("duplicate_successful_public_watchlist_event", "DEDUPLICATION_GATE"),
    ),
)
def test_classify_block_stage(raw_reason: str, expected_stage: str) -> None:
    assert classify_block_stage(raw_reason) == expected_stage

def test_public_alert_funnel_report_uses_normalized_categories(tmp_path: Path) -> None:
    db_path = tmp_path / "public-funnel.sqlite"
    _seed_public_funnel_database(db_path)

    report = build_public_alert_funnel_report(
        db_path,
        hours=24,
        limit=10,
        now=datetime(2026, 7, 3, 12, tzinfo=UTC),
    )

    assert report["source_available"] is True
    assert report["telegram_status_summary"]["blocked"] == 3
    assert report["telegram_status_summary"]["skipped"] == 1
    assert report["telegram_status_summary"]["sent"] == 1
    category_counts = {row["category"]: row["count"] for row in report["normalized_block_category_counts"]}
    assert category_counts["RR_BELOW_MIN"] == 1
    assert category_counts["TARGET_INTEGRITY_BLOCKED"] == 1
    assert category_counts["INVALID_TP_SEQUENCE"] == 1
    assert category_counts["NON_CRYPTO_SYMBOL"] == 1
    assert category_counts["DUPLICATE_PUBLIC_PLAN"] == 1
    assert report["non_crypto_symbol_blocks"] == [{"symbol": "NVDAUSDT", "count": 1}]
    stage_counts = {row["block_stage"]: row["count"] for row in report["block_stage_counts"]}
    assert stage_counts["TARGET_INTEGRITY_GATE"] == 1
    assert stage_counts["TARGET_CAUTION_GATE"] == 1
    assert stage_counts["SYMBOL_UNIVERSE_GATE"] == 1
    assert stage_counts["DEDUPLICATION_GATE"] == 1
    assert report["non_crypto_hygiene"]["blocked_attempt_percentage"] == "33.3%"
    assert report["non_crypto_hygiene"]["top_symbols"] == [
        {"symbol": "NVDAUSDT", "count": 1, "in_near_miss_list": "yes"}
    ]
    assert report["best_near_miss_blocked_setups"][0]["symbol"] == "BTCUSDT"

    text = format_public_alert_funnel_report(report)
    assert "Telegram status summary:" in text
    assert "Best near-miss blocked setups:" in text
    assert "Otherwise publishable near-misses:" in text
    assert "Lifecycle/public-state block diagnostics:" in text
    assert "Non-crypto hygiene summary:" in text
    assert "Target caution/chop summary:" in text
    assert "Latest scan run counters:" in text


def test_public_alert_funnel_script_prints_sample_report(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path = tmp_path / "public-funnel-cli.sqlite"
    _seed_public_funnel_database(db_path)

    exit_code = audit_public_alert_funnel.main(
        [
            "--database-path",
            str(db_path),
            "--hours",
            "24",
            "--limit",
            "5",
            "--as-of",
            "2026-07-03T12:00:00Z",
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Public Alert Funnel Diagnostics" in output
    assert "blocked: 3" in output
    assert "NON_CRYPTO_SYMBOL" in output
    assert "BTCUSDT | long | ACTIONABLE_A_GRADE" in output
    assert "Otherwise publishable near-misses:" in output
    assert "Block stage counts:" in output


def test_public_alert_funnel_script_normalizes_offset_as_of_to_utc(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    db_path = tmp_path / "public-funnel-cli-offset.sqlite"
    _seed_public_funnel_database(db_path)
    common_args = ["--database-path", str(db_path), "--hours", "24", "--limit", "5", "--as-of"]

    assert audit_public_alert_funnel.main([*common_args, "2026-07-03T12:00:00Z"]) == 0
    utc_output = capsys.readouterr().out
    assert audit_public_alert_funnel.main([*common_args, "2026-07-03T14:00:00+02:00"]) == 0
    offset_output = capsys.readouterr().out

    assert offset_output == utc_output


@pytest.mark.parametrize(
    ("as_of", "expected_error"),
    (
        (
            "2026-07-03T12:00:00",
            "must include a UTC offset or Z; timezone-naive timestamps are not allowed",
        ),
        ("not-a-timestamp", "must be a valid ISO-8601 timestamp with a UTC offset or Z"),
    ),
)
def test_public_alert_funnel_script_rejects_invalid_as_of(
    as_of: str,
    expected_error: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exc_info:
        audit_public_alert_funnel.main(["--as-of", as_of])

    assert exc_info.value.code == 2
    assert expected_error in capsys.readouterr().err


def test_public_alert_funnel_script_without_as_of_uses_default_current_utc_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured: dict[str, object] = {}

    def build_report(database_path: Path, *, hours: int, limit: int, now: datetime | None) -> dict[str, object]:
        captured.update(database_path=database_path, hours=hours, limit=limit, now=now)
        return {"source_available": True}

    monkeypatch.setattr(audit_public_alert_funnel, "build_public_alert_funnel_report", build_report)
    monkeypatch.setattr(audit_public_alert_funnel, "format_public_alert_funnel_report", lambda report: "report")

    assert audit_public_alert_funnel.main(["--database-path", str(tmp_path / "unused.sqlite")]) == 0

    assert capsys.readouterr().out == "report\n"
    assert captured["now"] is None


def test_public_alert_funnel_reporting_cutoff_is_inclusive(tmp_path: Path) -> None:
    db_path = tmp_path / "public-funnel-boundary.sqlite"
    with sqlite3.connect(db_path) as connection:
        initialize_database(connection)
        for signal_id, symbol, attempted_at in (
            ("inside-window", "INSIDEUSDT", "2026-07-02T12:00:01Z"),
            ("exact-cutoff", "CUTOFFUSDT", "2026-07-02T12:00:00Z"),
            ("outside-window", "OUTSIDEUSDT", "2026-07-02T11:59:59Z"),
        ):
            _insert_attempt(
                connection,
                signal_id=signal_id,
                symbol=symbol,
                direction="long",
                lifecycle_state="ACTIONABLE_A_GRADE",
                status="blocked",
                attempted_at=attempted_at,
                score="90",
                rr="2.9",
                technical_score="96",
                opportunity_score="96",
                blocked_reason="public_block_rr_below_3",
            )
        connection.commit()

    report = build_public_alert_funnel_report(
        db_path,
        hours=24,
        limit=10,
        now=datetime(2026, 7, 3, 12, tzinfo=UTC),
    )

    assert report["telegram_status_summary"]["blocked"] == 2
    assert {row["symbol"] for row in report["top_blocked_symbols"]} == {
        "CUTOFFUSDT",
        "INSIDEUSDT",
    }


def test_best_near_miss_dedupes_repeated_plan_id_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "public-funnel-dedupe.sqlite"
    with sqlite3.connect(db_path) as connection:
        initialize_database(connection)
        _insert_scan_run(connection)
        _insert_attempt(
            connection,
            signal_id="btc-plan-first",
            symbol="BTCUSDT",
            direction="long",
            lifecycle_state="ACTIONABLE_A_GRADE",
            status="blocked",
            attempted_at="2026-07-03T10:05:00Z",
            score="90",
            rr="3.2",
            technical_score="100",
            opportunity_score="97",
            blocked_reason="public_block_non_public_terminal_state",
            public_watchlist_plan_id="plan-btc",
            first_seen_at="2026-07-03T10:00:00Z",
            last_seen_at="2026-07-03T10:05:00Z",
        )
        _insert_attempt(
            connection,
            signal_id="btc-plan-best",
            symbol="BTCUSDT",
            direction="long",
            lifecycle_state="ACTIONABLE_A_GRADE",
            status="blocked",
            attempted_at="2026-07-03T10:20:00Z",
            score="94",
            rr="3.6",
            technical_score="98",
            opportunity_score="99",
            blocked_reason="public_block_non_public_terminal_state",
            public_watchlist_plan_id="plan-btc",
            first_seen_at="2026-07-03T10:01:00Z",
            last_seen_at="2026-07-03T10:20:00Z",
        )
        connection.commit()

    report = build_public_alert_funnel_report(
        db_path,
        hours=24,
        limit=10,
        now=datetime(2026, 7, 3, 12, tzinfo=UTC),
    )

    rows = report["best_near_miss_blocked_setups"]
    assert len(rows) == 1
    assert build_near_miss_key({"public_watchlist_plan_id": "plan-btc"}) == "plan:plan-btc"
    assert rows[0]["symbol"] == "BTCUSDT"
    assert rows[0]["count_seen"] == 2
    assert rows[0]["score"] == "94"
    assert rows[0]["RR"] == "3.6"
    assert rows[0]["first_seen_at"] == "2026-07-03T10:00:00Z"
    assert rows[0]["last_seen_at"] == "2026-07-03T10:20:00Z"
    assert rows[0]["block_stage"] == "LIFECYCLE_PUBLIC_STATE_GATE"
    assert len(report["otherwise_publishable_near_misses"]) == 1
    assert report["otherwise_publishable_near_misses"][0]["symbol"] == "BTCUSDT"


def test_otherwise_publishable_near_miss_classification() -> None:
    strong = _diagnostic_row()
    assert is_otherwise_publishable_near_miss(strong) is True

    assert is_otherwise_publishable_near_miss(_diagnostic_row(setup_quality_score="87")) is False
    assert is_otherwise_publishable_near_miss(_diagnostic_row(rr_planned="2.7", blocked_reason="public_block_rr_below_3")) is False
    assert is_otherwise_publishable_near_miss(_diagnostic_row(symbol="NVDAUSDT", blocked_reason="public_block_non_crypto_symbol")) is False
    assert is_otherwise_publishable_near_miss(_diagnostic_row(blocked_reason="target_integrity_failed:invalid_target_fields=tp_order")) is False


def _diagnostic_row(**overrides: str) -> dict[str, str]:
    row = {
        "symbol": "BTCUSDT",
        "direction": "long",
        "telegram_status": "blocked",
        "lifecycle_state": "ACTIONABLE_A_GRADE",
        "attempted_alert_type": "WATCHLIST",
        "public_alert_event_type": "initial_watchlist",
        "setup_quality_score": "90",
        "min_score_for_idea": "88",
        "rr_planned": "3.2",
        "min_rr": "3",
        "technical_score": "100",
        "opportunity_score": "97",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "108",
        "tp2": "114",
        "tp3": "120",
        "blocked_reason": "public_block_non_public_terminal_state",
        "dedupe_reason": "N/A",
    }
    row.update(overrides)
    return row

def _seed_public_funnel_database(db_path: Path) -> None:
    with sqlite3.connect(db_path) as connection:
        initialize_database(connection)
        _insert_scan_run(connection)
        _insert_attempt(
            connection,
            signal_id="btc-blocked-rr",
            symbol="BTCUSDT",
            direction="long",
            lifecycle_state="ACTIONABLE_A_GRADE",
            status="blocked",
            attempted_at="2026-07-03T11:30:00Z",
            score="91",
            rr="2.7",
            technical_score="89",
            opportunity_score="93",
            blocked_reason="blocked:public_block_rr_below_3; target_integrity_failed:invalid_target_fields=tp_order",
        )
        _insert_attempt(
            connection,
            signal_id="nvda-blocked-non-crypto",
            symbol="NVDAUSDT",
            direction="long",
            lifecycle_state="TRIGGERED",
            status="blocked",
            attempted_at="2026-07-03T11:20:00Z",
            score="90",
            rr="3.2",
            technical_score="86",
            opportunity_score="88",
            blocked_reason="blocked:public_block_non_crypto_symbol; public_block_non_actionable_state",
        )
        _insert_attempt(
            connection,
            signal_id="xrp-target-caution",
            symbol="XRPUSDT",
            direction="short",
            lifecycle_state="ACTIONABLE_A_GRADE",
            status="blocked",
            attempted_at="2026-07-03T11:10:00Z",
            score="87",
            rr="2.6",
            technical_score="90",
            opportunity_score="91",
            blocked_reason=(
                "blocked:public_block_target_caution_score_below_88; "
                "public_block_target_caution_rr_below_2_8; target_failure=TARGET_INSIDE_CHOP"
            ),
        )
        _insert_attempt(
            connection,
            signal_id="eth-duplicate",
            symbol="ETHUSDT",
            direction="long",
            lifecycle_state="ACTIONABLE_A_GRADE",
            status="skipped",
            attempted_at="2026-07-03T11:05:00Z",
            score="92",
            rr="3.4",
            technical_score="91",
            opportunity_score="94",
            blocked_reason="public_watchlist_duplicate_equivalent_plan",
            dedupe_status="skipped",
            dedupe_reason="duplicate_successful_public_watchlist_event",
        )
        _insert_attempt(
            connection,
            signal_id="sol-sent",
            symbol="SOLUSDT",
            direction="long",
            lifecycle_state="ACTIONABLE_A_GRADE",
            status="sent",
            attempted_at="2026-07-03T11:00:00Z",
            sent_at="2026-07-03T11:00:02Z",
            score="95",
            rr="3.6",
            technical_score="94",
            opportunity_score="96",
            blocked_reason="N/A",
        )
        _insert_candidate(
            connection,
            symbol="BTCUSDT",
            actionability_state="A_GRADE_ACTIONABLE_TARGET_CAUTION",
            target_integrity_status="warning",
            target_failure="TARGET_INSIDE_CHOP",
            final_failed_gate="N/A",
        )
        _insert_candidate(
            connection,
            symbol="ETHUSDT",
            actionability_state="NOT_A_GRADE_CANDIDATE",
            target_integrity_status="warning",
            target_failure="TARGET_INSIDE_CHOP",
            final_failed_gate="scoring",
        )
        _insert_candidate(
            connection,
            symbol="XRPUSDT",
            actionability_state="A_GRADE_BLOCKED_BY_FINAL_GATES",
            target_integrity_status="blocked",
            target_failure="TARGET_INSIDE_CHOP",
            final_failed_gate="target_integrity",
        )
        connection.commit()


def _insert_scan_run(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO scan_runs (
            run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
            strategy, timeframes_json, market_regime, runtime_stats_json,
            command_preset, command_used, total_valid_setups, near_misses,
            rejected, data_issues, data_issues_json, raw_payload_json,
            actionable_setups, actionable_a_grade_setups, actionable_a_grade_target_caution,
            confirmed_setups, rejected_no_edge, fatal_target_blocks, soft_target_warnings
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "run-public-funnel",
            "2026-07-03T11:00:00Z",
            "binance",
            "manual",
            5,
            '["BTCUSDT","ETHUSDT","SOLUSDT","XRPUSDT","NVDAUSDT"]',
            "liquidity_grab_pullback",
            "{}",
            "mixed",
            "{}",
            "test",
            "pytest",
            4,
            3,
            1,
            0,
            "[]",
            "{}",
            3,
            2,
            1,
            1,
            1,
            1,
            2,
        ),
    )


def _insert_attempt(
    connection: sqlite3.Connection,
    *,
    signal_id: str,
    symbol: str,
    direction: str,
    lifecycle_state: str,
    status: str,
    attempted_at: str,
    score: str,
    rr: str,
    technical_score: str,
    opportunity_score: str,
    blocked_reason: str,
    sent_at: str | None = None,
    dedupe_status: str = "N/A",
    dedupe_reason: str = "N/A",
    public_watchlist_plan_id: str = "N/A",
    public_watchlist_event_key: str = "N/A",
    public_alert_event_type: str = "N/A",
    first_seen_at: str = "N/A",
    last_seen_at: str = "N/A",
    seen_count: int = 1,
    min_rr: str = "3",
    min_score_for_idea: str = "88",
) -> None:
    connection.execute(
        """
        INSERT INTO telegram_alert_attempts (
            signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
            sent_at, attempted_at, telegram_status, message_hash, scan_run_id,
            attempted_alert_type, setup_quality_score, rr_planned, min_rr, opportunity_score,
            min_score_for_idea, technical_score, entry_low, entry_high, stop_loss, tp1, tp2, tp3,
            blocked_reason, dedupe_status, dedupe_reason, first_seen_at, last_seen_at, seen_count,
            public_watchlist_plan_id, public_watchlist_event_key, public_alert_event_type
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            signal_id,
            symbol,
            direction,
            lifecycle_state,
            "WATCHLIST",
            lifecycle_state,
            sent_at,
            attempted_at,
            status,
            f"hash-{signal_id}",
            "run-public-funnel",
            "WATCHLIST",
            score,
            rr,
            min_rr,
            opportunity_score,
            min_score_for_idea,
            technical_score,
            "100",
            "102",
            "95",
            "108",
            "114",
            "120",
            blocked_reason,
            dedupe_status,
            dedupe_reason,
            first_seen_at,
            last_seen_at,
            seen_count,
            public_watchlist_plan_id,
            public_watchlist_event_key,
            public_alert_event_type,
        ),
    )

def _insert_candidate(
    connection: sqlite3.Connection,
    *,
    symbol: str,
    actionability_state: str,
    target_integrity_status: str,
    target_failure: str,
    final_failed_gate: str,
) -> None:
    connection.execute(
        """
        INSERT INTO setup_candidates (
            run_id, symbol, mode, direction, entry, stop, tp1, tp2, tp3, rr,
            invalidation, quality_grade, candidate_quality_grade, final_quality_grade,
            technical_score, opportunity_score, failed_gate, final_failed_gate,
            final_block_reason, target_integrity_status, target_failure,
            target_failure_severity, target_warning_reason, actionability_state,
            trust_meter, risk_warning, raw_candidate_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "run-public-funnel",
            symbol,
            "swing",
            "long",
            "100",
            "95",
            "108",
            "114",
            "120",
            "3.2",
            "Invalid below stop.",
            "A",
            "A",
            "A",
            "90",
            "92",
            "target_inside_chop",
            final_failed_gate,
            "N/A",
            target_integrity_status,
            target_failure,
            "target_caution_actionable",
            target_failure,
            actionability_state,
            "90%",
            "Manual execution only. Manage risk.",
            "{}",
        ),
    )
