from __future__ import annotations

import argparse
import asyncio
import json
from decimal import Decimal

from app.agents.trade_idea import TradeIdeaAgent
from app.analytics.portfolio_selection import (
    BetaGroup,
    PortfolioCandidate,
    PortfolioDecision,
    PortfolioRiskLimits,
    PortfolioSelectionInput,
    select_portfolio,
)
from app.analytics.setup_quality import validate_setup_quality
from app.command_center import (
    build_command_center_payload,
    format_command_center_summary,
    format_portfolio_command_summary,
    format_top_setup_spotlight,
)
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerProcessMemoryStats,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerRuntimeStats,
    ScannerSymbolResult,
)
from app.watch_mode import WatchState, WatchSymbolState, load_watch_state, save_watch_state, should_trigger_activation_alert
from scripts import run_scan


def _config(symbols: list[str]) -> ScannerRunConfig:
    return ScannerRunConfig.model_validate(
        {
            "symbols": symbols,
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )


def _trade_idea(symbol: str = "BTCUSDT"):
    return TradeIdeaAgent().create(
        {
            "symbol": symbol,
            "exchange": "binance",
            "market_type": "perpetual_futures",
            "direction": "long",
            "timeframe": "15m",
            "setup_type": "liquidity_grab_pullback_swing",
            "entry_low": Decimal("100"),
            "entry_high": Decimal("102"),
            "stop_loss": Decimal("95"),
            "take_profit_targets": (Decimal("112"), Decimal("120")),
            "invalidation": "Invalid below 95.",
            "opportunity_score": Decimal("88"),
            "opportunity_grade": "A",
            "opportunity_decision": "high_quality_candidate",
            "risk_approved": True,
            "best_rr": Decimal("3.2"),
            "technical_summary": "Bullish sweep, confirmation, and pullback.",
            "derivatives_summary": "Derivatives support the long.",
            "confirmed_facts": ("Sweep confirmed.",),
            "cancel_condition": "Cancel if price closes below 95.",
        }
    )


def _valid_symbol(symbol: str = "BTCUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED, ScannerPipelineStatus.JOURNAL_ENTRY_CREATED),
        trade_idea=_trade_idea(symbol),
        valid_strategy_modes=("swing",),
        technical_score=88,
        derivatives_score=85,
        strategy_diagnostics={
            "swing": {
                "is_valid": True,
                "mode": "swing",
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "rr_to_tp2": Decimal("3.2"),
                "trust_percentage": 90,
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
                "derivatives_supports_trade": True,
                "derivatives_conflict_reason": "N/A",
                "crowding_risk": "low",
            }
        },
        setup_quality=validate_setup_quality(
            {
                "symbol": symbol,
                "setup_valid": True,
                "mode": "swing",
                "bias": "long",
                "rr_to_tp2": Decimal("3.2"),
                "best_rr": Decimal("3.2"),
                "sweep_passed": True,
                "confirmation_passed": True,
                "pullback_valid": True,
                "ob_or_fvg_valid": True,
                "fib_valid": True,
                "htf_2d_trend": "bullish",
                "mtf_12h_trend": "bullish",
                "trust_percentage": 90,
                "poc_available": True,
                "value_area_available": True,
                "derivatives_supports_trade": True,
                "derivatives_score": 88,
                "funding_status": "normal",
                "crowding_risk": "low",
                "risk_approved": True,
                "data_quality_score": Decimal("95"),
            }
        ),
        runtime_seconds=1.2,
    )


def _near_miss_symbol(symbol: str = "ETHUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "rr_to_tp2": Decimal("1.8"),
                "first_failed_gate": "rr_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": ("rr_below_minimum",),
                "pullback_failure_reason": "RR does not justify capital yet.",
                "derivatives_supports_trade": True,
            }
        },
        setup_quality=validate_setup_quality(
            {
                "symbol": symbol,
                "setup_valid": False,
                "mode": "swing",
                "bias": "long",
                "rr_to_tp2": Decimal("1.8"),
                "best_rr": Decimal("1.8"),
                "sweep_passed": True,
                "confirmation_passed": True,
                "pullback_valid": True,
                "first_failed_gate": "rr_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": ("rr_below_minimum",),
            }
        ),
        runtime_seconds=0.8,
    )


def _rejected_symbol(symbol: str = "ETHUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "execution_sweep_status": "failed",
                "confirmation_structure_shift_status": "not_evaluated",
                "first_failed_gate": "missing_confirmed_sweep",
                "gates_failed": ("missing_confirmed_sweep",),
            }
        },
        setup_quality=validate_setup_quality(
            {
                "symbol": symbol,
                "setup_valid": False,
                "first_failed_gate": "missing_confirmed_sweep",
                "gates_failed": ("missing_confirmed_sweep",),
            }
        ),
    )


def _scan_result() -> ScannerRunResult:
    return ScannerRunResult(
        config=_config(["BTCUSDT", "ETHUSDT"]),
        results=(_valid_symbol("BTCUSDT"), _near_miss_symbol("ETHUSDT")),
        scanned_symbols=2,
        failed_symbols=0,
        trade_ideas_created=1,
        dry_run_alerts_created=0,
        journal_entries_created=1,
        cache_stats={"hits": 3, "misses": 1},
        retry_diagnostics=({"operation": "mock", "attempt": 1},),
        runtime_stats=ScannerRuntimeStats(
            total_runtime_seconds=2,
            average_seconds_per_symbol=1,
            slowest_symbol="BTCUSDT",
            slowest_symbol_seconds=1.2,
            completed_symbols=2,
            process_memory=ScannerProcessMemoryStats(
                measurement_status="Verified",
                source="test:rss",
                rss_start_bytes=100_000_000,
                rss_end_bytes=105_000_000,
                rss_observed_peak_bytes=110_000_000,
                rss_delta_bytes=5_000_000,
                samples_attempted=4,
                samples_succeeded=4,
                samples_failed=0,
            ),
        ),
    )


def test_command_preset_configuration_defaults() -> None:
    daily = run_scan.parse_args(["--command-preset", "daily"])
    swing = run_scan.parse_args(["--command-preset", "swing"])
    challenge = run_scan.parse_args(["--command-preset", "challenge"])
    scalp = run_scan.parse_args(["--command-preset", "scalp"])
    override = run_scan.parse_args(["--command-preset", "daily", "--modes", "swing", "--no-continue-watch"])

    assert daily.htf_timeframe == "2d"
    assert daily.bias_timeframe == "12h"
    assert daily.execution_timeframe == "15m"
    assert daily.confirmation_timeframe == "15m"
    assert swing.confirmation_timeframe == "15m"
    assert challenge.confirmation_timeframe == "15m"
    assert scalp.confirmation_timeframe == "15m"
    assert daily.min_score_for_idea == "80"
    assert daily.min_rr == Decimal("2.5")
    assert daily.rank_results is True
    assert daily.portfolio_select is True
    assert daily.continue_watch is True
    assert scalp.fast is True
    assert scalp.candle_limit == 180
    assert scalp.symbol_timeout_sec == 20
    assert override.modes == ["swing"]
    assert override.continue_watch is False


def test_command_center_summary_and_runtime_metrics() -> None:
    result = _scan_result()
    ranked = run_scan.rank_scan_results(result.results)
    payload = build_command_center_payload(result, ranked_results=ranked)
    process_memory = payload["runtime_metrics"]["process_memory"]
    assert process_memory["measurement_status"] == "Verified"
    assert process_memory["rss_observed_peak_bytes"] == 110_000_000

    text = format_command_center_summary(
        result,
        ranked_results=ranked,
        promoted_watch_symbols=("ETHUSDT",),
        command_preset="daily",
        min_rr=Decimal("2.5"),
    )

    assert "DAILY COMMAND CENTER" in text
    assert "- Total symbols scanned: 2" in text
    assert "- Valid setups: 1" in text
    assert "- Near misses: 1" in text
    assert "- Best setup: BTCUSDT" in text
    assert "- Symbols promoted into watch mode: ETHUSDT" in text
    assert "avg symbol 1s; slowest BTCUSDT (1.2s); retries 1; cache efficiency 75% (3/4)" in text


def test_top_setup_spotlight_generation() -> None:
    text = format_top_setup_spotlight(_valid_symbol("BTCUSDT"))

    assert "TOP SETUP" in text
    assert "- Symbol: BTCUSDT" in text
    assert "- Direction: LONG" in text
    assert "- RR: 3.2" in text
    assert "- Invalidation: Invalid below 95." in text
    assert "- Risk warning:" in text


def test_portfolio_command_summary_shows_correlation_and_strongest_candidate() -> None:
    selection = select_portfolio(
        PortfolioSelectionInput(
            candidates=(
                PortfolioCandidate(
                    symbol="SOLUSDT",
                    mode="swing",
                    direction="long",
                    quality_state="HIGH_QUALITY_TRADE",
                    quality_score=92,
                    tradeability_score=92,
                    edge_score=Decimal("85"),
                    rr=Decimal("3.5"),
                    risk_pct=Decimal("1"),
                    beta_group=BetaGroup.SOL_BETA,
                    derivatives_score=85,
                    execution_risk_score=20,
                    derivatives_clean=True,
                ),
                PortfolioCandidate(
                    symbol="JUPUSDT",
                    mode="swing",
                    direction="long",
                    quality_state="HIGH_QUALITY_TRADE",
                    quality_score=80,
                    tradeability_score=80,
                    edge_score=Decimal("75"),
                    rr=Decimal("3.1"),
                    risk_pct=Decimal("1"),
                    beta_group=BetaGroup.SOL_BETA,
                    derivatives_score=80,
                    execution_risk_score=25,
                    derivatives_clean=True,
                ),
            ),
            risk_limits=PortfolioRiskLimits(max_selected_setups=2),
        )
    )

    assert selection.rejected_candidates[0].decision == PortfolioDecision.REJECTED_LOWER_QUALITY_DUPLICATE
    text = format_portfolio_command_summary(selection)

    assert "PORTFOLIO SUMMARY" in text
    assert "- Total selected setups: 1" in text
    assert "- Rejected correlated symbols: JUPUSDT" in text
    assert "- Strongest portfolio candidate: SOLUSDT" in text


def test_continue_watch_persists_near_miss_and_invalidates_rejected_candidate(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "watch_state.json"
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", state_path)

    args = argparse.Namespace(continue_watch=True, continued_watch_symbols=())
    promoted = run_scan._update_continue_watch_state(args, _scan_result())

    state = load_watch_state(state_path)
    assert promoted == ("ETHUSDT",)
    assert state.symbols["ETHUSDT"].invalidated is False

    save_watch_state(
        state_path,
        WatchState(
            symbols={
                "ETHUSDT": WatchSymbolState(
                    symbol="ETHUSDT",
                    last_status="near_miss",
                    readiness_label="WATCH",
                )
            }
        ),
    )
    rejected_result = ScannerRunResult(
        config=_config(["ETHUSDT"]),
        results=(_rejected_symbol("ETHUSDT"),),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )

    run_scan._update_continue_watch_state(args, rejected_result)
    invalidated = load_watch_state(state_path).symbols["ETHUSDT"]

    assert invalidated.invalidated is True
    assert should_trigger_activation_alert(_valid_symbol("ETHUSDT"), invalidated) is False


class ExportScannerRunner:
    async def run(self, config, after_symbol=None, progress=None, resume_metadata=None):
        result = _scan_result().model_copy(update={"config": config})
        if after_symbol is not None:
            for index, symbol_result in enumerate(result.results, start=1):
                await after_symbol(symbol_result, index, len(result.results))
        return result


def test_export_bundle_generation(tmp_path, monkeypatch) -> None:
    report_path = tmp_path / "report.txt"
    json_path = tmp_path / "command.json"
    watchlist_path = tmp_path / "watchlist.txt"
    state_path = tmp_path / "watch_state.json"
    latest_path = tmp_path / "latest_scan.json"
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", state_path)
    monkeypatch.setattr(run_scan, "LATEST_RUN_PATH", latest_path)
    monkeypatch.setattr(run_scan, "ScannerRunner", ExportScannerRunner)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "ETHUSDT",
                "--command-preset",
                "daily",
                "--export-report",
                str(report_path),
                "--export-json",
                str(json_path),
                "--export-watchlist",
                str(watchlist_path),
            ]
        )
    )

    report_text = report_path.read_text(encoding="utf-8")
    json_payload = json.loads(json_path.read_text(encoding="utf-8"))
    watchlist_text = watchlist_path.read_text(encoding="utf-8")

    assert "DAILY COMMAND CENTER" in report_text
    assert "TOP SETUP" in report_text
    assert json_payload["command_center"]["best_setup"] == "BTCUSDT"
    assert json_payload["command_center"]["promoted_watch_symbols"] == ["ETHUSDT"]
    assert "WATCHLIST SUMMARY" in watchlist_text
    assert "ETHUSDT" in watchlist_text
