from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from app.analytics.public_alert_funnel import (
    build_public_alert_funnel_report,
    format_public_alert_funnel_report,
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
    assert report["best_near_miss_blocked_setups"][0]["symbol"] == "BTCUSDT"

    text = format_public_alert_funnel_report(report)
    assert "Telegram status summary:" in text
    assert "Best near-miss blocked setups:" in text
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
        ]
    )

    output = capsys.readouterr().out
    assert exit_code == 0
    assert "Public Alert Funnel Diagnostics" in output
    assert "blocked: 3" in output
    assert "NON_CRYPTO_SYMBOL" in output
    assert "BTCUSDT | long | ACTIONABLE_A_GRADE" in output


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
) -> None:
    connection.execute(
        """
        INSERT INTO telegram_alert_attempts (
            signal_id, symbol, direction, new_state, alert_type, lifecycle_state,
            sent_at, attempted_at, telegram_status, message_hash, scan_run_id,
            attempted_alert_type, setup_quality_score, rr_planned, opportunity_score,
            technical_score, entry_low, entry_high, stop_loss, tp1, tp2, tp3,
            blocked_reason, dedupe_status, dedupe_reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            opportunity_score,
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
