from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from app.analytics.target_intelligence import (
    TargetFailureType,
    TargetQualityGrade,
    build_target_intelligence,
)
from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display, display_fields, format_target_intelligence_block
from app.lifecycle.service import observation_from_symbol_result
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerSymbolResult
from app.research.queries import build_research_report
from app.research.reports import format_research_report
from app.storage.database import open_initialized_database


def _candle(index: int, high: str, low: str, close: str | None = None) -> dict[str, Decimal | int]:
    close_value = Decimal(close if close is not None else low)
    return {
        "timestamp": index,
        "open": close_value,
        "high": Decimal(high),
        "low": Decimal(low),
        "close": close_value,
        "volume": Decimal("100"),
    }


def test_nearest_opposing_liquidity_detection() -> None:
    result = build_target_intelligence(
        symbol="BTCUSDT",
        direction="long",
        entry=Decimal("100"),
        stop=Decimal("95"),
        minimum_rr=Decimal("2.5"),
        swing_lookback=1,
        candles=(
            _candle(0, "100", "90"),
            _candle(1, "112", "95"),
            _candle(2, "105", "94"),
            _candle(3, "120", "96"),
            _candle(4, "110", "97"),
        ),
    )

    assert result.nearest_opposing_liquidity == Decimal("112.00000000")
    assert result.tp1_candidate == Decimal("112.00000000")
    assert result.liquidity_targets[0].source == "opposing_swing_high"


def test_rr_projection_to_tp1_and_tp2() -> None:
    result = build_target_intelligence(
        direction="long",
        entry=Decimal("100"),
        stop=Decimal("95"),
        minimum_rr=Decimal("2.5"),
        swing_lookback=1,
        candles=(
            _candle(0, "100", "90"),
            _candle(1, "112", "95"),
            _candle(2, "105", "94"),
            _candle(3, "120", "96"),
            _candle(4, "110", "97"),
        ),
    )

    assert result.rr_to_tp1 == Decimal("2.40000000")
    assert result.rr_to_tp2 == Decimal("4.00000000")
    assert result.rr_projections[0].target_label == "TP1"
    assert result.rr_projections[1].meets_minimum is True


def test_rr_below_minimum_classification() -> None:
    result = build_target_intelligence(
        direction="long",
        entry=Decimal("100"),
        stop=Decimal("95"),
        minimum_rr=Decimal("2.5"),
        impulse_start=Decimal("100"),
        impulse_end=Decimal("105"),
    )

    assert result.rr_to_tp2 == Decimal("1.61800000")
    assert result.target_failure_type == TargetFailureType.RR_BELOW_MINIMUM
    assert result.target_quality_grade == TargetQualityGrade.REJECT


def test_no_target_available_classification() -> None:
    result = build_target_intelligence(
        direction="long",
        entry=Decimal("100"),
        stop=Decimal("95"),
        candles=(_candle(0, "99", "90"), _candle(1, "98", "91"), _candle(2, "97", "92")),
    )

    assert result.target_failure_type == TargetFailureType.NO_CLEAR_TARGET
    assert result.tp1_candidate == NA
    assert result.tp2_candidate == NA


def test_opposing_structure_block_classification() -> None:
    result = build_target_intelligence(
        direction="long",
        entry=Decimal("100"),
        stop=Decimal("95"),
        minimum_rr=Decimal("2.5"),
        swing_lookback=1,
        candles=(
            _candle(0, "100", "90"),
            _candle(1, "106", "95"),
            _candle(2, "104", "94"),
            _candle(3, "109", "96"),
            _candle(4, "103", "97"),
        ),
        impulse_start=Decimal("90"),
        impulse_end=Decimal("105"),
    )

    assert result.nearest_opposing_liquidity == Decimal("106.00000000")
    assert result.target_failure_type == TargetFailureType.OPPOSING_STRUCTURE_BLOCK
    assert "Opposing structure is too close" in result.rr_compression_reason


def test_no_fake_target_creation_without_structure_or_impulse() -> None:
    result = build_target_intelligence(direction="short", entry=Decimal("100"), stop=Decimal("105"))

    assert result.tp1_candidate == NA
    assert result.tp2_candidate == NA
    assert result.tp3_candidate == NA
    assert result.liquidity_targets == ()
    assert result.target_failure_type == TargetFailureType.NO_CLEAR_TARGET


def test_target_intelligence_display_and_json_output() -> None:
    intelligence = build_target_intelligence(
        direction="long",
        entry=Decimal("100"),
        stop=Decimal("95"),
        impulse_start=Decimal("100"),
        impulse_end=Decimal("105"),
    )
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        target_intelligence=intelligence,
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "first_failed_gate": "rr_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": ("rr_below_minimum",),
                "rr_to_tp2": Decimal("1.6"),
            }
        },
    )

    text = format_target_intelligence_block(symbol_result)
    payload = display_fields(symbol_result)

    assert "Target Intelligence" in text
    assert "- TP1 candidate:" in text
    assert "- RR to TP2: 1.618" in text
    assert payload["target_intelligence"]["target_failure_type"] == "RR_BELOW_MINIMUM"
    assert payload["target_failure_type"] == "RR_BELOW_MINIMUM"


def test_target_intelligence_does_not_weaken_rr_gate() -> None:
    symbol_result = ScannerSymbolResult(
        symbol="ETHUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        target_intelligence=build_target_intelligence(
            direction="long",
            entry=Decimal("100"),
            stop=Decimal("95"),
            impulse_start=Decimal("100"),
            impulse_end=Decimal("105"),
        ),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "first_failed_gate": "rr_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": ("rr_below_minimum",),
                "rr_to_tp2": Decimal("1.6"),
            }
        },
    )

    display = build_symbol_display(symbol_result)

    assert display.display_bucket == "near_miss"
    assert display.failed_gate == "rr_below_minimum"
    assert symbol_result.trade_idea is None


def test_rr_compression_remains_watchable_not_invalidated() -> None:
    symbol_result = ScannerSymbolResult(
        symbol="SOLUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        target_intelligence=build_target_intelligence(
            direction="long",
            entry=Decimal("100"),
            stop=Decimal("95"),
            impulse_start=Decimal("100"),
            impulse_end=Decimal("105"),
        ),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "first_failed_gate": "rr_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": ("rr_below_minimum",),
                "rr_to_tp2": Decimal("1.6"),
                "pullback_intelligence": {
                    "pullback_failure_type": "RR_COMPRESSION",
                    "lifecycle_projection": {"lifecycle_action": "WATCHLIST"},
                },
            }
        },
    )

    observation = observation_from_symbol_result(symbol_result)

    assert observation.invalidated is False
    assert observation.expired is False
    assert observation.pullback_valid is True
    assert observation.rr_valid is False


def test_target_research_queries(tmp_path) -> None:
    db_path = tmp_path / "target_research.db"
    with open_initialized_database(db_path) as connection:
        _insert_run(connection)
        _insert_symbol(
            connection,
            symbol="BTCUSDT",
            bucket="near_miss",
            target={
                "tp1_candidate": "106",
                "tp2_candidate": "108.09",
                "nearest_opposing_liquidity": "106",
                "target_distance": "8.09",
                "clean_path_distance": "6",
                "rr_to_tp1": "1.2",
                "rr_to_tp2": "1.618",
                "target_quality_grade": "Reject",
                "target_failure_type": "RR_BELOW_MINIMUM",
                "rr_compression_reason": "Entry is too late for the visible TP2 room.",
                "target_confidence": 58,
                "next_target_condition": "Wider clean TP2 or better entry required",
                "liquidity_targets": [{"source": "fib_extension_1.272"}],
            },
            gate="rr_below_minimum",
        )
        _insert_symbol(
            connection,
            symbol="ETHUSDT",
            bucket="valid",
            target={
                "tp1_candidate": "112",
                "tp2_candidate": "120",
                "nearest_opposing_liquidity": "112",
                "target_distance": "20",
                "clean_path_distance": "12",
                "rr_to_tp1": "2.4",
                "rr_to_tp2": "4",
                "target_quality_grade": "B",
                "target_failure_type": "N/A",
                "rr_compression_reason": "N/A",
                "target_confidence": 72,
                "next_target_condition": "Maintain clean path to TP2 with RR above minimum.",
                "liquidity_targets": [{"source": "opposing_swing_high"}],
            },
            gate="N/A",
        )
        connection.commit()

    failures = build_research_report(db_path, query="target_failures")
    compression = build_research_report(db_path, query="rr_compression_analysis")
    distribution = build_research_report(db_path, query="target_quality_distribution")
    best = build_research_report(db_path, query="best_target_conditions")
    text = format_research_report(compression)

    assert failures["total_target_failures"] == 1
    assert failures["failure_type_counts"][0]["target_failure_type"] == "RR_BELOW_MINIMUM"
    assert compression["total_rr_compression_cases"] == 1
    assert distribution["target_quality_grades"][0]["count"] >= 1
    assert best["conditions"][0]["symbol"] == "ETHUSDT"
    assert "RR Compression Analysis" in text


def _insert_run(connection: sqlite3.Connection) -> None:
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
            "run_1",
            "2026-05-19T09:00:00+00:00",
            "binance",
            "manual",
            2,
            "[]",
            "liquidity_grab_pullback",
            "{}",
            "trend_expansion",
            "{}",
            "N/A",
            "test",
            1,
            1,
            0,
            0,
            "[]",
            "{}",
        ),
    )


def _insert_symbol(connection: sqlite3.Connection, *, symbol: str, bucket: str, target: dict, gate: str) -> None:
    raw = {
        "valid_strategy_modes": ["swing"] if bucket == "valid" else [],
        "rejected_strategy_modes": [] if bucket == "valid" else ["swing"],
        "strategy_diagnostics": {"swing": {"first_failed_gate": gate, "target_intelligence": target}},
        "setup_quality": {"quality_state": "WATCHLIST_NEAR_MISS", "quality_grade": "C", "quality_score": 70},
        "readiness_label": "WATCH",
        "target_intelligence": target,
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
            "run_1",
            symbol,
            "scanned",
            bucket,
            75,
            "70",
            "N/A",
            gate,
            "RR below minimum." if gate != "N/A" else "N/A",
            "Wider clean TP2 or better entry required",
            "Watchlist only",
            "trend_expansion",
            "{}",
            "{}",
            "valid",
            "N/A",
            json.dumps(raw),
        ),
    )
