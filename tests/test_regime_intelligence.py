from __future__ import annotations

import json
import sqlite3
from decimal import Decimal

from app.analytics.market_regime import MarketRegimeInput, RegimeState, evaluate_market_regime
from app.analytics.setup_quality import SetupQualityState, validate_setup_quality
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerSymbolResult,
    _apply_market_regime_to_results,
)
from app.research.queries import build_research_report
from app.storage.database import open_initialized_database
from app.storage.repositories import store_scan_result


def _trend_candles(*, start: str = "100", step: str = "1", count: int = 90, wick: str = "1") -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    start_value = Decimal(start)
    step_value = Decimal(step)
    wick_value = Decimal(wick)
    for index in range(count):
        price = start_value + step_value * Decimal(index)
        candles.append(
            {
                "timestamp": index,
                "open": price - step_value / Decimal("2"),
                "high": price + wick_value,
                "low": price - wick_value,
                "close": price,
                "volume": Decimal("100"),
            }
        )
    return candles


def _compression_candles() -> list[dict[str, Decimal | int]]:
    candles = _trend_candles(step="0.05", count=70, wick="2")
    for index in range(70, 90):
        close = Decimal("104") + Decimal(index % 2) * Decimal("0.01")
        candles.append(
            {
                "timestamp": index,
                "open": close,
                "high": close + Decimal("0.08"),
                "low": close - Decimal("0.08"),
                "close": close,
                "volume": Decimal("100"),
            }
        )
    return candles


def _config() -> ScannerRunConfig:
    return ScannerRunConfig.model_validate(
        {"symbols": ["BTCUSDT"], "exchange": "binance", "account_equity": Decimal("1000"), "risk_per_trade_pct": Decimal("1")}
    )


def _valid_symbol() -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED, ScannerPipelineStatus.JOURNAL_ENTRY_CREATED),
        valid_strategy_modes=("challenge",),
        strategy_diagnostics={
            "challenge": {
                "mode": "challenge",
                "is_valid": True,
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "rr_to_tp2": Decimal("3.2"),
                "trust_percentage": 88,
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
            }
        },
        setup_quality=validate_setup_quality(
            {
                "setup_valid": True,
                "mode": "challenge",
                "bias": "long",
                "rr_to_tp2": Decimal("3.2"),
                "best_rr": Decimal("3.2"),
                "sweep_passed": True,
                "confirmation_passed": True,
                "pullback_valid": True,
                "trust_percentage": 88,
                "first_failed_gate": "N/A",
            }
        ),
    )


def test_regime_confidence_and_compatibility_scores() -> None:
    result = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_trend_candles(),
            eth_candles=_trend_candles(start="80", step="0.8"),
            bullish_bias_pct=Decimal("72"),
            bearish_bias_pct=Decimal("15"),
            confirmation_pct=Decimal("66"),
            htf_agreement_pct=Decimal("80"),
            average_rr=Decimal("3.4"),
            setup_density_pct=Decimal("35"),
            rejection_clustering_pct=Decimal("8"),
            btc_d_context="stable",
            usdt_d_context="stable",
        )
    )

    assert result.state == RegimeState.TREND_EXPANSION
    assert result.confidence_score >= 71
    assert result.compatibility_scores["swing"].score >= result.compatibility_scores["challenge"].score
    assert "HTF alignment boost" in result.boosts


def test_regime_strictness_changes_trade_permission() -> None:
    base = {
        "btc_candles": _compression_candles(),
        "eth_candles": _compression_candles(),
        "bullish_bias_pct": Decimal("42"),
        "bearish_bias_pct": Decimal("35"),
    }

    low = evaluate_market_regime(MarketRegimeInput(**base, strictness="low"))
    high = evaluate_market_regime(MarketRegimeInput(**base, strictness="high"))

    assert low.state == RegimeState.RANGE_COMPRESSION
    assert low.compatibility_scores["challenge"].allowed is True
    assert high.compatibility_scores["challenge"].allowed is False
    assert high.adjustment.regime_penalty >= low.adjustment.regime_penalty


def test_weak_regime_blocks_high_confidence_setup_with_diagnostics() -> None:
    regime = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_trend_candles(step="0.4"),
            eth_candles=_trend_candles(start="140", step="-0.4"),
            valid_sweep_pct=Decimal("60"),
            confirmation_pct=Decimal("20"),
            failed_confirmation_pct=Decimal("60"),
            rejection_clustering_pct=Decimal("65"),
            strictness="high",
        )
    )

    adjusted = _apply_market_regime_to_results((_valid_symbol(),), regime)[0]

    assert adjusted.status == ScannerPipelineStatus.REJECTED_BY_REGIME
    assert adjusted.valid_strategy_modes == ()
    assert adjusted.rejected_strategy_modes == ("challenge",)
    assert adjusted.regime_blocked is True
    assert adjusted.setup_quality.quality_state == SetupQualityState.WATCHLIST_NEAR_MISS
    assert adjusted.regime_diagnostics["confidence_score"] == regime.confidence_score
    assert "penalty" in adjusted.rejection_reason


def test_regime_metadata_is_persisted(tmp_path) -> None:
    db_path = tmp_path / "regime.db"
    regime = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_trend_candles(),
            eth_candles=_trend_candles(start="80", step="0.8"),
            bullish_bias_pct=Decimal("72"),
            confirmation_pct=Decimal("65"),
        )
    )
    symbol = _valid_symbol().model_copy(
        update={
            "regime_state": regime.state.value,
            "regime_confidence_score": regime.confidence_score,
            "regime_compatibility_score": regime.compatibility_scores["challenge"].score,
            "regime_compatibility_label": regime.compatibility_scores["challenge"].label,
            "regime_penalty": regime.adjustment.regime_penalty,
            "regime_notes": regime.environment_notes,
        }
    )
    scan = ScannerRunResult(
        config=_config(),
        results=(symbol,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
        market_regime=regime,
        regime_adjustments=regime.adjustment,
    )

    run_id = store_scan_result(db_path, scan)

    with sqlite3.connect(db_path) as connection:
        run = connection.execute(
            "SELECT regime_confidence, regime_compatibility_json, environment_notes_json FROM scan_runs WHERE run_id = ?",
            (run_id,),
        ).fetchone()
        symbol_row = connection.execute(
            "SELECT regime_confidence, regime_compatibility_score, regime_compatibility_label, regime_penalty FROM symbol_results WHERE run_id = ?",
            (run_id,),
        ).fetchone()

    assert run[0] == regime.confidence_score
    assert json.loads(run[1])["challenge"]["score"] == regime.compatibility_scores["challenge"].score
    assert json.loads(run[2])
    assert symbol_row[0] == str(regime.confidence_score)
    assert symbol_row[2] == regime.compatibility_scores["challenge"].label


def test_phase_35_research_queries(tmp_path) -> None:
    db_path = tmp_path / "research.db"
    with open_initialized_database(db_path) as connection:
        connection.execute(
            """
            INSERT INTO scan_runs (
                run_id, timestamp, exchange, universe, symbols_scanned, symbols_json,
                strategy, timeframes_json, market_regime, regime_confidence,
                regime_compatibility_json, environment_notes_json, runtime_stats_json,
                command_preset, command_used, total_valid_setups, near_misses, rejected,
                data_issues, data_issues_json, raw_payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "run_1",
                "2026-05-18T09:00:00+00:00",
                "binance",
                "manual",
                2,
                "[]",
                "liquidity_grab_pullback",
                "{}",
                "CHOP",
                28,
                "{}",
                "[]",
                "{}",
                "N/A",
                "test",
                0,
                1,
                1,
                0,
                "[]",
                "{}",
            ),
        )
        for symbol, bucket, gate, quality in (
            ("BTCUSDT", "near_miss", "regime_compatibility", "68"),
            ("ETHUSDT", "no_setup", "missing_confirmed_sweep", "24"),
        ):
            raw = {
                "setup_quality": {"quality_state": "WATCHLIST_NEAR_MISS", "quality_grade": "C", "quality_score": quality},
                "readiness_label": "HOT WATCH",
                "valid_strategy_modes": [],
                "rejected_strategy_modes": ["challenge"],
            }
            connection.execute(
                """
                INSERT INTO symbol_results (
                    run_id, symbol, status, display_bucket, readiness_score, setup_quality_score,
                    edge_score, failed_gate, rejection_reason, next_trigger_needed, action_label,
                    regime_state, regime_confidence, regime_compatibility_score, regime_compatibility_label,
                    regime_penalty, environment_notes_json, derivatives_context_json, volume_profile_context_json,
                    pullback_status, portfolio_decision, raw_result_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "run_1",
                    symbol,
                    "scanned",
                    bucket,
                    55,
                    quality,
                    "N/A",
                    gate,
                    gate,
                    "Wait",
                    "Watchlist only",
                    "CHOP",
                    "28",
                    "24",
                    "Hostile",
                    20,
                    "[]",
                    "{}",
                    "{}",
                    "N/A",
                    "N/A",
                    json.dumps(raw),
                ),
            )
        connection.execute(
            """
            INSERT INTO replay_results (
                run_id, setup_fingerprint, outcome, filled, tp_hit, sl_hit, final_r,
                time_in_trade, regime, symbol, mode, raw_result_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("run_1", "fp", "stopped", 1, "N/A", 1, "-1", "10", "CHOP", "BTCUSDT", "challenge", "{}"),
        )
        connection.commit()

    expectancy = build_research_report(db_path, query="regime_expectancy")
    density = build_research_report(db_path, query="regime_setup_density")
    rejections = build_research_report(db_path, query="regime_rejection_patterns")
    quality = build_research_report(db_path, query="regime_quality_distribution")

    assert expectancy["regimes"][0]["regime"] == "CHOP"
    assert density["regimes"][0]["setup_density_pct"] == 50
    assert rejections["regimes"][0]["patterns"][0]["failed_gate"] in {"regime_compatibility", "missing_confirmed_sweep"}
    assert quality["regimes"][0]["compatibility_labels"][0]["regime_compatibility_label"] == "Hostile"
