from __future__ import annotations

import json
from decimal import Decimal

from app.analytics.performance_memory import (
    ConfidenceBucket,
    SetupPerformanceStats,
    apply_performance_memory_to_result,
    apply_performance_memory_to_symbol,
    confidence_bucket,
    ingest_replay_summary,
    load_performance_memory,
    performance_memory_result,
    reset_performance_memory,
    save_performance_memory,
    setup_fingerprint_from_scan,
)
from app.analytics.edge_analytics import condition_key_from_diagnostics
from app.analytics.setup_quality import SetupQualityState, validate_setup_quality
from app.analytics.portfolio_selection import PortfolioRiskLimits, build_portfolio_selection_from_scan
from app.backtesting import (
    ReplayDirection,
    ReplayOutcome,
    ReplaySetupCandidate,
    ReplaySummary,
    ReplaySymbolResult,
    ReplayTradeResult,
)
from app.data.dtos import NA
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from app.strategies.liquidity_grab_pullback import LiquidityGrabMode
from scripts import run_scan


def _candidate(symbol: str = "BTCUSDT", *, index: int = 0) -> ReplaySetupCandidate:
    return ReplaySetupCandidate(
        symbol=symbol,
        mode=LiquidityGrabMode.swing,
        direction=ReplayDirection.LONG,
        detected_at_index=index,
        detected_at_timestamp=index,
        entry=Decimal("100"),
        entry_low=Decimal("99"),
        entry_high=Decimal("101"),
        stop=Decimal("95"),
        tp1=Decimal("110"),
        tp2=Decimal("115"),
        rr_to_tp2=Decimal("3"),
        pullback_calculation_timeframe="15m",
        selected_zone_type="FVG",
        trust_grade="A",
        trust_percentage=90,
        condition_key=condition_key_from_diagnostics(
            symbol=symbol,
            mode="swing",
            readiness_score=90,
            diagnostics={
                "bias": "long",
                "mode": "swing",
                "htf_2d_trend": "bullish",
                "mtf_12h_trend": "bullish",
                "derivatives_supports_trade": True,
                "derivatives_conflict_reason": "N/A",
                "crowding_risk": "low",
                "poc": Decimal("100"),
                "entry": Decimal("100"),
                "entry_low": Decimal("99"),
                "entry_high": Decimal("101"),
                "rr_to_tp2": Decimal("3"),
                "trust_percentage": 90,
                "execution_sweep_status": "passed",
                "pullback_zone_status": "valid",
                "selected_zone_type": "FVG",
                "fib_alignment_status": "aligned",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
            },
        ),
    )


def _trade(
    symbol: str = "BTCUSDT",
    *,
    index: int = 0,
    r_multiple: Decimal = Decimal("1"),
    tp1_hit: bool = True,
    tp2_hit: bool = False,
    filled: bool = True,
    outcome: ReplayOutcome = ReplayOutcome.TP1_HIT,
) -> ReplayTradeResult:
    candidate = _candidate(symbol, index=index)
    return ReplayTradeResult(
        symbol=symbol,
        mode=LiquidityGrabMode.swing,
        direction=ReplayDirection.LONG,
        candidate=candidate,
        outcome=outcome,
        filled=filled,
        entry_filled=filled,
        tp1_hit=tp1_hit,
        tp2_hit=tp2_hit,
        r_multiple=r_multiple,
        final_r_multiple=r_multiple,
        candles_held=4,
    )


def _summary(trades: tuple[ReplayTradeResult, ...], *, symbol: str = "BTCUSDT") -> ReplaySummary:
    return ReplaySummary(
        symbols_tested=1,
        historical_candles=100,
        symbols=(ReplaySymbolResult(symbol=symbol, historical_candles=100, trades=trades),),
    )


def _scan_config(symbols: list[str]) -> ScannerRunConfig:
    return ScannerRunConfig.model_validate(
        {
            "symbols": symbols,
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )


def _symbol_result(
    symbol: str = "BTCUSDT",
    *,
    valid: bool = True,
    quality_score: int = 90,
) -> ScannerSymbolResult:
    state_input = {
        "symbol": symbol,
        "setup_valid": valid,
        "mode": "swing",
        "bias": "long",
        "rr_to_tp2": Decimal("3"),
        "best_rr": Decimal("3"),
        "sweep_passed": True,
        "confirmation_passed": True,
        "pullback_valid": True,
        "ob_or_fvg_valid": True,
        "fib_valid": True,
        "htf_2d_trend": "bullish",
        "mtf_12h_trend": "bullish",
        "trust_percentage": quality_score,
        "poc_available": True,
        "value_area_available": True,
        "derivatives_supports_trade": True,
        "derivatives_score": 85,
        "funding_status": "normal",
        "crowding_risk": "low",
        "risk_approved": True,
        "data_quality_score": Decimal("95"),
        "first_failed_gate": NA if valid else "quality_filter",
        "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
        "gates_failed": () if valid else ("quality_filter",),
    }
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.IDEA_CREATED if valid else ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.IDEA_CREATED if valid else ScannerPipelineStatus.SCANNED_NO_SETUP,),
        technical_score=88,
        derivatives_score=85,
        crowding_risk="low",
        squeeze_risk="balanced",
        strategy_diagnostics={
            "swing": {
                "is_valid": valid,
                "mode": "swing",
                "bias": "long",
                "htf_2d_trend": "bullish",
                "mtf_12h_trend": "bullish",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "selected_zone_type": "FVG",
                "fib_alignment_status": "aligned",
                "rr_to_tp2": Decimal("3"),
                "trust_percentage": quality_score,
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
                "gates_failed": () if valid else ("quality_filter",),
                "derivatives_supports_trade": True,
                "derivatives_conflict_reason": "N/A",
                "crowding_risk": "low",
                "squeeze_risk": "balanced",
                "poc": Decimal("100"),
                "entry": Decimal("100"),
                "entry_low": Decimal("99"),
                "entry_high": Decimal("101"),
            }
        },
        valid_strategy_modes=("swing",) if valid else (),
        rejected_strategy_modes=() if valid else ("swing",),
        setup_quality=validate_setup_quality(state_input),
    )


def _store_with_trades(trades: tuple[ReplayTradeResult, ...]):
    return ingest_replay_summary(load_performance_memory("__missing_performance_memory__.json"), _summary(trades)).store


def test_fingerprint_generation_is_deterministic() -> None:
    symbol_result = _symbol_result()

    first = setup_fingerprint_from_scan(symbol_result)
    second = setup_fingerprint_from_scan(symbol_result)

    assert first.signature == second.signature
    assert first.direction == "LONG"
    assert first.pullback_quality == "CLEAN"
    assert "LONG" in first.label


def test_confidence_bucket_boundaries() -> None:
    assert confidence_bucket(0) == ConfidenceBucket.VERY_LOW
    assert confidence_bucket(10) == ConfidenceBucket.LOW
    assert confidence_bucket(25) == ConfidenceBucket.MEDIUM
    assert confidence_bucket(75) == ConfidenceBucket.HIGH
    assert confidence_bucket(200) == ConfidenceBucket.VERY_HIGH


def test_replay_ingestion_updates_expectancy_and_prevents_duplicates() -> None:
    trades = tuple(
        _trade(index=index, r_multiple=Decimal("1"), tp1_hit=True)
        if index < 6
        else _trade(index=index, r_multiple=Decimal("-1"), tp1_hit=False, outcome=ReplayOutcome.STOPPED)
        for index in range(10)
    )
    first = ingest_replay_summary(load_performance_memory("__missing_performance_memory__.json"), _summary(trades))
    second = ingest_replay_summary(first.store, _summary(trades))
    stats = next(iter(first.store.setup_stats.values()))

    assert first.events_added == 10
    assert second.events_added == 0
    assert second.duplicates_ignored == 10
    assert stats.total_occurrences == 10
    assert stats.wins == 6
    assert stats.losses == 4
    assert stats.average_r == Decimal("0.20000000")
    assert stats.tp1_rate == Decimal("60.00")
    assert stats.confidence_bucket == ConfidenceBucket.LOW


def test_corrupted_memory_entries_are_rejected(tmp_path) -> None:
    path = tmp_path / "performance_memory.json"
    valid_store = _store_with_trades(tuple(_trade(index=index) for index in range(10)))
    payload = valid_store.model_dump(mode="json")
    payload["setup_stats"]["bad"] = {
        "fingerprint": {},
        "total_occurrences": 1,
        "filled_occurrences": 1,
        "wins": 2,
        "losses": 0,
        "tp1_hits": 0,
        "tp2_hits": 0,
        "r_multiples": ["1"],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = load_performance_memory(path)

    assert loaded.rejected_memory_entries == 1
    assert len(loaded.setup_stats) == 1


def test_adjustment_caps_and_insufficient_sample_handling() -> None:
    high_store = _store_with_trades(tuple(_trade(index=index, r_multiple=Decimal("10")) for index in range(10)))
    high_stats = next(iter(high_store.setup_stats.values()))
    high_result = performance_memory_result(fingerprint=high_stats.fingerprint, stats=high_stats)

    low_store = _store_with_trades(tuple(_trade(index=index, r_multiple=Decimal("10")) for index in range(5)))
    low_stats = next(iter(low_store.setup_stats.values()))
    low_result = performance_memory_result(fingerprint=low_stats.fingerprint, stats=low_stats)

    bad_store = _store_with_trades(
        tuple(_trade(index=index, r_multiple=Decimal("-10"), tp1_hit=False, outcome=ReplayOutcome.STOPPED) for index in range(10))
    )
    bad_stats = next(iter(bad_store.setup_stats.values()))
    bad_result = performance_memory_result(fingerprint=bad_stats.fingerprint, stats=bad_stats)

    assert high_result.memory_adjustments["edge_score_adjustment"] == 10
    assert low_result.confidence_bucket == ConfidenceBucket.VERY_LOW
    assert low_result.memory_adjustments["edge_score_adjustment"] == 0
    assert low_result.historical_warning == "Performance memory confidence too low."
    assert bad_result.memory_adjustments["edge_score_adjustment"] == -15


def test_scanner_json_fields_and_no_strategy_bypass() -> None:
    invalid = _symbol_result(valid=False)
    original_state = invalid.setup_quality.quality_state
    store = _store_with_trades(tuple(_trade(index=index, r_multiple=Decimal("2")) for index in range(10)))

    updated = apply_performance_memory_to_symbol(invalid, store)
    payload = updated.model_dump(mode="json")

    assert updated.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert updated.setup_quality.quality_state == original_state
    assert updated.setup_quality.quality_state not in (
        SetupQualityState.HIGH_QUALITY_TRADE,
        SetupQualityState.VALID_BUT_LOWER_QUALITY,
    )
    assert "performance_memory" in payload
    assert payload["historical_expectancy"] != NA
    assert payload["confidence_bucket"] == "LOW"
    assert payload["memory_adjustments"]["edge_score_adjustment"] > 0
    assert payload["historical_warning"] != NA


def test_portfolio_prefers_memory_but_does_not_select_invalid_setup() -> None:
    strong = apply_performance_memory_to_symbol(
        _symbol_result("BTCUSDT", quality_score=90),
        _store_with_trades(tuple(_trade("BTCUSDT", index=index, r_multiple=Decimal("2")) for index in range(10))),
    )
    weak = apply_performance_memory_to_symbol(
        _symbol_result("ETHUSDT", quality_score=90),
        _store_with_trades(
            tuple(_trade("ETHUSDT", index=index, r_multiple=Decimal("-1"), tp1_hit=False, outcome=ReplayOutcome.STOPPED) for index in range(10))
        ),
    )
    invalid = apply_performance_memory_to_symbol(
        _symbol_result("SOLUSDT", valid=False, quality_score=90),
        _store_with_trades(tuple(_trade("SOLUSDT", index=index, r_multiple=Decimal("3")) for index in range(10))),
    )
    scan_result = ScannerRunResult(
        config=_scan_config(["BTCUSDT", "ETHUSDT", "SOLUSDT"]),
        results=(weak, strong, invalid),
        scanned_symbols=3,
        failed_symbols=0,
        trade_ideas_created=2,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )

    selection = build_portfolio_selection_from_scan(scan_result, risk_limits=PortfolioRiskLimits(max_selected_setups=1))

    assert [candidate.symbol for candidate in selection.selected_candidates] == ["BTCUSDT"]
    assert "SOLUSDT" not in [candidate.symbol for candidate in selection.selected_candidates]


def test_memory_reset_and_disable(tmp_path) -> None:
    path = tmp_path / "performance_memory.json"
    reset_store = reset_performance_memory(path)
    loaded = load_performance_memory(path)
    disabled_result = apply_performance_memory_to_result(
        ScannerRunResult(
            config=_scan_config(["BTCUSDT"]),
            results=(_symbol_result(),),
            scanned_symbols=1,
            failed_symbols=0,
            trade_ideas_created=1,
            dry_run_alerts_created=0,
            journal_entries_created=0,
        ),
        loaded,
        enabled=False,
    )

    assert path.exists()
    assert reset_store.setup_stats == {}
    assert loaded.setup_stats == {}
    assert disabled_result.results[0].performance_memory["enabled"] is False
    assert disabled_result.results[0].memory_adjustments["applied"] is False


def test_performance_memory_cli_flags_are_accepted() -> None:
    enabled = run_scan.parse_args(["--symbols", "BTCUSDT", "--performance-memory", "--min-memory-confidence", "MEDIUM"])
    disabled = run_scan.parse_args(["--symbols", "BTCUSDT", "--disable-performance-memory"])
    reset = run_scan.parse_args(["--symbols", "BTCUSDT", "--reset-performance-memory"])
    daily_default = run_scan.parse_args(["--command-preset", "daily"])

    assert enabled.performance_memory is True
    assert enabled.min_memory_confidence == "MEDIUM"
    assert disabled.performance_memory is False
    assert reset.reset_performance_memory is True
    assert run_scan._performance_memory_enabled(daily_default) is True
