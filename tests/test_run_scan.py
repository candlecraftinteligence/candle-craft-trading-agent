from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest

from app.backtesting import ReplayStats, ReplaySummary, ReplaySymbolResult
from app.analytics.derivatives_enrichment import DerivativesEnrichmentResult
from app.analytics.setup_quality import SetupQualityState, validate_setup_quality
from app.analytics.volume_profile import VOLUME_PROFILE_SOURCE, VolumeProfileResult
from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display, filter_ranked_results, rank_scan_results
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from scripts import run_scan


class FakeScannerRunner:
    async def run(self, config):
        volume_profile = VolumeProfileResult(
            symbol="BTCUSDT",
            timeframe="15m",
            poc=Decimal("80750"),
            value_area_high=Decimal("81200"),
            value_area_low=Decimal("80100"),
            nearest_high_volume_node=Decimal("80750"),
            nearest_low_volume_node=Decimal("80400"),
            price_range_high=Decimal("82000"),
            price_range_low=Decimal("79000"),
            total_volume=Decimal("100000"),
            candles_used=250,
        )
        symbol_result = ScannerSymbolResult(
            symbol="BTCUSDT",
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
            rejection_reason="No sweep, BOS, or CHoCH context was detected.",
            candles_fetched=250,
            latest_close=Decimal("104250.5"),
            funding_rate=Decimal("-0.0001"),
            open_interest=Decimal("105"),
            technical_score=62,
            derivatives_score=90,
            trend_context="bullish",
            recent_range_high=Decimal("105000"),
            recent_range_low=Decimal("103200"),
            latest_swing_high=Decimal("104900"),
            latest_swing_low=Decimal("103700"),
            rejection_stage="technical",
            rejection_reasons=("No sweep, BOS, or CHoCH context was detected.",),
            funding_status="normal",
            funding_extreme=False,
            open_interest_change_pct=Decimal("5"),
            oi_direction="rising",
            price_direction="up",
            price_oi_relationship="long_building_or_breakout_participation",
            long_short_ratio=Decimal("1.10"),
            crowding_risk="low",
            squeeze_risk="balanced",
            derivatives_enrichment=DerivativesEnrichmentResult(
                symbol="BTCUSDT",
                exchange="binance",
                funding_rate=Decimal("-0.0001"),
                funding_status="normal",
                funding_extreme=False,
                open_interest=Decimal("105"),
                open_interest_change_pct=Decimal("5"),
                oi_direction="rising",
                price_direction="up",
                price_oi_relationship="long_building_or_breakout_participation",
                long_short_ratio=Decimal("1.10"),
                crowding_risk="low",
                squeeze_risk="balanced",
                derivatives_score=90,
                supports_long=True,
                supports_short=True,
            ),
            strategy_name="liquidity_grab_pullback",
            volume_profile=volume_profile,
            volume_profile_source=VOLUME_PROFILE_SOURCE,
            poc=Decimal("80750"),
            value_area_high=Decimal("81200"),
            value_area_low=Decimal("80100"),
            nearest_high_volume_node=Decimal("80750"),
            nearest_low_volume_node=Decimal("80400"),
            formatted_strategy_output="Challenge Setup\nNo valid challenge setup.\n\nSwing Setup\nNo valid swing setup.\n\nScalp Setup\nNo valid scalp setup.",
            strategy_diagnostics={
                "challenge": {
                    "is_valid": False,
                    "htf_timeframe": "2d",
                    "bias_timeframe": "12h",
                    "execution_timeframe": "15m",
                    "confirmation_timeframe": "5m",
                    "htf_2d_context_source": "synthetic_from_1d",
                    "candles_2d_count": 125,
                    "candles_12h_count": 250,
                    "candles_15m_count": 250,
                    "candles_5m_count": 250,
                    "htf_2d_trend": "bearish",
                    "mtf_12h_trend": "neutral",
                    "ltf_confirmation_timeframe": "5m",
                    "ltf_confirmation_status": "missing",
                    "execution_sweep_status": "passed",
                    "confirmation_structure_shift_status": "failed",
                    "pullback_zone_status": "N/A",
                    "selected_zone_type": "N/A",
                    "ob_zone": {"is_present": False, "zone_type": "OB"},
                    "fvg_zone": {"is_present": False, "zone_type": "FVG"},
                    "fib_alignment_status": "N/A",
                    "fib_382": "N/A",
                    "fib_618": "N/A",
                    "fib_65": "N/A",
                    "fib_786": "N/A",
                    "entry_low": "N/A",
                    "entry_high": "N/A",
                    "stop": "N/A",
                    "tp1": "N/A",
                    "tp2": "N/A",
                    "tp3": "N/A",
                    "rr_to_tp2": "N/A",
                    "pullback_failure_reason": "N/A",
                    "confirmation_bos_choch_reason": "No 5m BOS/CHoCH close beyond the required LTF swing.",
                    "first_failed_gate": "missing_confirmation_structure_shift",
                    "volume_profile_source": VOLUME_PROFILE_SOURCE,
                    "poc": Decimal("80750"),
                    "poc_diagnostics": "POC available from estimated candle volume profile.",
                    "gates_failed": ("missing_confirmation_structure_shift",),
                    "hard_rejection_reasons": ("No 5m BOS/CHoCH close beyond the required LTF swing.",),
                    "sweep_diagnostics": "passed: bullish sweep at candle 30; magnitude 5 (0.5 ATR).",
                    "bos_choch_diagnostics": "failed: No 5m BOS/CHoCH close beyond the required LTF swing.",
                    "derivatives_supports_trade": True,
                    "derivatives_conflict_reason": "N/A",
                    "funding_context": {"funding_status": "normal"},
                    "oi_context": {"oi_direction": "rising"},
                    "crowding_risk": "low",
                    "squeeze_risk": "balanced",
                }
            },
            rejected_strategy_modes=("challenge", "swing", "scalp"),
            strategy_missing_data=("candles_2d: N/A", "cvd: N/A", "liquidation_data: N/A"),
            setup_quality=validate_setup_quality(
                {
                    "symbol": "BTCUSDT",
                    "setup_valid": False,
                    "mode": "challenge",
                    "bias": "long",
                    "sweep_passed": True,
                    "confirmation_passed": False,
                    "pullback_valid": False,
                    "rr_to_tp2": NA,
                    "best_rr": NA,
                    "htf_2d_trend": "bearish",
                    "mtf_12h_trend": "neutral",
                    "derivatives_supports_trade": True,
                    "derivatives_score": 90,
                    "funding_status": "normal",
                    "crowding_risk": "low",
                    "first_failed_gate": "missing_confirmation_structure_shift",
                    "gates_passed": ("sweep",),
                    "gates_failed": ("missing_confirmation_structure_shift",),
                    "rejection_reason": "No 5m BOS/CHoCH close beyond the required LTF swing.",
                }
            ),
        )
        return ScannerRunResult(
            config=config,
            results=(symbol_result,),
            scanned_symbols=1,
            failed_symbols=0,
            trade_ideas_created=0,
            dry_run_alerts_created=0,
            journal_entries_created=0,
        )


class CapturingScannerRunner:
    configs = []

    async def run(self, config):
        self.__class__.configs.append(config)
        return ScannerRunResult(
            config=config,
            results=(),
            scanned_symbols=0,
            failed_symbols=0,
            trade_ideas_created=0,
            dry_run_alerts_created=0,
            journal_entries_created=0,
        )


def _valid_rank_result(symbol: str = "VALIDUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        technical_score=85,
        derivatives_score=88,
        strategy_diagnostics={
            "swing": {
                "is_valid": True,
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
                "trust_percentage": 88,
                "rr_to_tp2": Decimal("3.2"),
                "derivatives_supports_trade": True,
            }
        },
        valid_strategy_modes=("swing",),
    )


def _near_miss_rank_result(symbol: str = "NEARUSDT", failed_gate: str = "trust_meter_below_minimum") -> ScannerSymbolResult:
    rr_value = Decimal("1.8") if failed_gate in {"rr_below_minimum", "rr_too_low"} else Decimal("2.6")
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        technical_score=70,
        derivatives_score=75,
        strategy_diagnostics={
            "swing": {
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "rr_to_tp2": rr_value,
                "first_failed_gate": failed_gate,
                "gates_failed": (failed_gate,),
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "trust_percentage": 62,
                "pullback_failure_reason": "Later setup gate failed after sweep and confirmation.",
                "derivatives_supports_trade": True,
            }
        },
        rejected_strategy_modes=("swing",),
    )


def _no_setup_rank_result(symbol: str = "NOSETUPUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        technical_score=50,
        derivatives_score=60,
        strategy_diagnostics={
            "challenge": {
                "execution_sweep_status": "failed",
                "confirmation_structure_shift_status": "not_evaluated",
                "first_failed_gate": "missing_confirmed_sweep",
                "gates_failed": ("missing_confirmed_sweep",),
            }
        },
        rejected_strategy_modes=("challenge",),
    )


class Phase20ScannerRunner:
    configs = []
    save_path = None

    async def run(self, config, after_symbol=None, resume_metadata=None):
        self.__class__.configs.append(config)
        results = tuple(_no_setup_rank_result(symbol.symbol) for symbol in config.symbols)
        if after_symbol is not None:
            total = len(results)
            for index, symbol_result in enumerate(results, start=1):
                await after_symbol(symbol_result, index, total)
                if self.__class__.save_path is not None:
                    payload = json.loads(self.__class__.save_path.read_text(encoding="utf-8"))
                    assert len(payload["results"]) == index
        return ScannerRunResult(
            config=config,
            results=results,
            scanned_symbols=len(results),
            failed_symbols=0,
            trade_ideas_created=0,
            dry_run_alerts_created=0,
            journal_entries_created=0,
            cache_stats={
                "enabled": True,
                "file_cache_enabled": False,
                "file_path": None,
                "hits": 2,
                "misses": 4,
                "expired": 0,
                "writes": 4,
                "errors": 0,
                "entries": 4,
            },
            retry_diagnostics=(),
            resume_metadata=dict(resume_metadata or {}),
        )


class RetryDiagnosticsScannerRunner:
    async def run(self, config, after_symbol=None, resume_metadata=None):
        symbol_result = ScannerSymbolResult(
            symbol="WARNUSDT",
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
            rejection_reason="No valid setup.",
            rejection_stage="strategy",
            rejection_reasons=("No valid setup.",),
            derivatives_warnings=("funding_rate unavailable from public endpoint: mocked retry failure",),
            strategy_diagnostics={
                "swing": {
                    "execution_sweep_status": "failed",
                    "confirmation_structure_shift_status": "not_evaluated",
                    "first_failed_gate": "missing_confirmed_sweep",
                    "gates_failed": ("missing_confirmed_sweep",),
                }
            },
            rejected_strategy_modes=("swing",),
        )
        return ScannerRunResult(
            config=config,
            results=(symbol_result,),
            scanned_symbols=1,
            failed_symbols=0,
            trade_ideas_created=0,
            dry_run_alerts_created=0,
            journal_entries_created=0,
            retry_diagnostics=(
                {
                    "operation": "binance_futures GET /fapi/v1/fundingRate",
                    "attempt": 1,
                    "attempts": 3,
                    "will_retry": True,
                    "delay_seconds": 0,
                    "error": "mocked retry",
                    "error_type": "ExchangeRateLimitError",
                },
            ),
            resume_metadata=dict(resume_metadata or {}),
        )


def _fake_replay_summary() -> ReplaySummary:
    stats = ReplayStats(
        total_setups=1,
        filled_trades=1,
        win_rate=Decimal("100.00"),
        tp1_rate=Decimal("100.00"),
        tp2_rate=Decimal("0.00"),
        average_r=Decimal("1.25"),
        median_r=Decimal("1.25"),
        expectancy_r=Decimal("1.25"),
        max_win_streak=1,
    )
    return ReplaySummary(
        symbols_tested=1,
        historical_candles=1000,
        stats=stats,
        replay_edge="mixed",
        sample_size="low_sample_size",
        sample_size_warning="low_sample_size",
        symbols=(
            ReplaySymbolResult(
                symbol="BTCUSDT",
                historical_candles=1000,
                stats=stats,
                replay_edge="mixed",
                sample_size="low_sample_size",
                sample_size_warning="low_sample_size",
                main_failure_reason="N/A",
                quality_note="Replay sample is too small for confidence.",
            ),
        ),
    )


def test_verbose_cli_flag_accepted() -> None:
    args = run_scan.parse_args(["--symbols", "BTCUSDT", "--verbose"])

    assert args.verbose is True
    assert args.diagnostics_level == "normal"
    assert args.diagnostics_level_explicit is False


def test_diagnostics_level_cli_flag_accepted() -> None:
    args = run_scan.parse_args(["--symbols", "BTCUSDT", "--diagnostics-level", "summary"])

    assert args.diagnostics_level == "summary"
    assert args.diagnostics_level_explicit is True


def test_display_cli_flag_defaults_to_normal_and_accepts_modes() -> None:
    default_args = run_scan.parse_args(["--symbols", "BTCUSDT"])
    compact_args = run_scan.parse_args(["--symbols", "BTCUSDT", "--display", "compact"])

    assert default_args.display == "normal"
    assert default_args.display_explicit is False
    assert compact_args.display == "compact"
    assert compact_args.display_explicit is True


def test_phase_18_cli_flags_default_to_ranked_hidden_no_setups() -> None:
    args = run_scan.parse_args(["--symbols", "BTCUSDT"])

    assert args.rank_results is True
    assert args.show_no_setups is False
    assert args.max_display_results == 10
    assert args.bucket_filter is None


def test_phase_20_cli_flags_accepted() -> None:
    args = run_scan.parse_args(
        [
            "--symbols",
            "BTCUSDT",
            "--cache",
            "--cache-ttl-seconds",
            "5",
            "--cache-file",
            "scan_runs/cache.json",
            "--resume-from",
            "scan_runs/latest_scan.json",
            "--save-run",
            "scan_runs/latest_scan.json",
            "--progress",
        ]
    )

    assert args.cache_enabled is True
    assert args.cache_ttl_seconds == 5
    assert args.cache_file.parts[-2:] == ("scan_runs", "cache.json")
    assert args.resume_from.parts[-2:] == ("scan_runs", "latest_scan.json")
    assert args.save_run.parts[-2:] == ("scan_runs", "latest_scan.json")
    assert args.progress is True


def test_phase_23_timeout_and_fast_cli_flags_accepted() -> None:
    args = run_scan.parse_args(
        [
            "--symbols",
            "BTCUSDT",
            "--request-timeout-sec",
            "3.5",
            "--symbol-timeout-sec",
            "12",
            "--scan-timeout-sec",
            "30",
            "--fast",
        ]
    )

    assert args.request_timeout_sec == 3.5
    assert args.symbol_timeout_sec == 12
    assert args.scan_timeout_sec == 30
    assert args.fast is True


def test_max_scan_seconds_alias_sets_scan_timeout() -> None:
    args = run_scan.parse_args(["--symbols", "BTCUSDT", "--max-scan-seconds", "15"])

    assert args.scan_timeout_sec == 15


def test_replay_candles_default_is_300() -> None:
    args = run_scan.parse_args(["--replay"])

    assert args.replay_candles == 300


def test_save_run_without_path_defaults_to_latest_scan() -> None:
    args = run_scan.parse_args(["--symbols", "BTCUSDT", "--save-run"])

    assert args.save_run.parts[-2:] == ("scan_runs", "latest_scan.json")


def test_no_cache_flag_is_passed_to_scanner_config(monkeypatch, capsys) -> None:
    CapturingScannerRunner.configs = []
    monkeypatch.setattr(run_scan, "ScannerRunner", CapturingScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--no-cache"]))

    config = CapturingScannerRunner.configs[0]
    assert config.cache_enabled is False


def test_list_presets_prints_available_presets(capsys) -> None:
    asyncio.run(run_scan.main(["--list-presets"]))

    captured = capsys.readouterr()
    assert "Available watchlist presets:" in captured.out
    assert "- majors (5 symbols)" in captured.out
    assert "- large_caps (13 symbols)" in captured.out
    assert "- meme_high_liquidity (6 symbols)" in captured.out


def test_manual_symbols_behavior_is_preserved() -> None:
    args = run_scan.parse_args(["--symbols", "btcusdt", "ETHUSDT"])
    resolution = run_scan._resolve_watchlist(args)

    assert resolution.source_label == "symbols"
    assert resolution.symbols == ("BTCUSDT", "ETHUSDT")


def test_preset_resolution_uses_static_symbols() -> None:
    args = run_scan.parse_args(["--preset", "majors"])
    resolution = run_scan._resolve_watchlist(args)

    assert resolution.source_label == "preset majors"
    assert resolution.symbols == ("BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT")


def test_include_exclude_behavior_runs_after_preset_resolution() -> None:
    args = run_scan.parse_args(
        [
            "--preset",
            "majors",
            "--include-symbols",
            "ethusdt",
            "jupusdt",
            "--exclude-symbols",
            "btcusdt",
        ]
    )

    resolution = run_scan._resolve_watchlist(args)

    assert resolution.symbols == ("ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "JUPUSDT")


def test_max_symbols_trims_after_include_exclude_processing() -> None:
    args = run_scan.parse_args(
        [
            "--preset",
            "large_caps",
            "--include-symbols",
            "JUPUSDT",
            "--exclude-symbols",
            "BTCUSDT",
            "--max-symbols",
            "3",
        ]
    )

    resolution = run_scan._resolve_watchlist(args)

    assert resolution.symbols == ("ETHUSDT", "SOLUSDT", "BNBUSDT")


def test_duplicate_removal_preserves_first_occurrence() -> None:
    args = run_scan.parse_args(
        [
            "--symbols",
            "btcusdt",
            "ETHUSDT",
            "BTCUSDT",
            "--include-symbols",
            "ethusdt",
            "solusdt",
        ]
    )

    resolution = run_scan._resolve_watchlist(args)

    assert resolution.symbols == ("BTCUSDT", "ETHUSDT", "SOLUSDT")


def test_custom_preset_file_source(tmp_path) -> None:
    preset_path = tmp_path / "watchlist.json"
    preset_path.write_text(json.dumps({"name": "custom_name", "symbols": ["btcusdt", "ethusdt"]}), encoding="utf-8")
    args = run_scan.parse_args(["--preset-file", str(preset_path)])

    resolution = run_scan._resolve_watchlist(args)

    assert resolution.source_label == "custom file custom_name"
    assert resolution.symbols == ("BTCUSDT", "ETHUSDT")


def test_unknown_preset_raises_clear_error() -> None:
    args = run_scan.parse_args(["--preset", "unknown"])

    with pytest.raises(SystemExit, match="Unknown watchlist preset 'unknown'"):
        run_scan._resolve_watchlist(args)


def test_empty_final_symbol_list_raises_clear_error() -> None:
    args = run_scan.parse_args(["--symbols", "BTCUSDT", "--exclude-symbols", "BTCUSDT"])

    with pytest.raises(SystemExit, match="Resolved watchlist is empty"):
        run_scan._resolve_watchlist(args)


def test_cli_integration_passes_resolved_watchlist_to_scanner(monkeypatch, capsys) -> None:
    CapturingScannerRunner.configs = []
    monkeypatch.setattr(run_scan, "ScannerRunner", CapturingScannerRunner)

    asyncio.run(run_scan.main(["--preset", "large_caps", "--max-symbols", "2", "--display", "compact"]))

    captured = capsys.readouterr()
    config = CapturingScannerRunner.configs[0]
    assert [symbol.symbol for symbol in config.symbols] == ["BTCUSDT", "ETHUSDT"]
    assert "Watchlist: preset large_caps" in captured.out
    assert "Symbols queued: 2" in captured.out


def test_resume_from_skips_completed_symbols(tmp_path, monkeypatch, capsys) -> None:
    resume_path = tmp_path / "resume.json"
    resume_path.write_text(
        json.dumps({"results": [_no_setup_rank_result("BTCUSDT").model_dump(mode="json")]}),
        encoding="utf-8",
    )
    CapturingScannerRunner.configs = []
    monkeypatch.setattr(run_scan, "ScannerRunner", CapturingScannerRunner)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "ETHUSDT",
                "--resume-from",
                str(resume_path),
            ]
        )
    )

    config = CapturingScannerRunner.configs[0]
    assert [symbol.symbol for symbol in config.symbols] == ["ETHUSDT"]


def test_no_resume_skip_scans_loaded_completed_symbols(tmp_path, monkeypatch, capsys) -> None:
    resume_path = tmp_path / "resume.json"
    resume_path.write_text(
        json.dumps({"results": [_no_setup_rank_result("BTCUSDT").model_dump(mode="json")]}),
        encoding="utf-8",
    )
    CapturingScannerRunner.configs = []
    monkeypatch.setattr(run_scan, "ScannerRunner", CapturingScannerRunner)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "ETHUSDT",
                "--resume-from",
                str(resume_path),
                "--no-resume-skip",
            ]
        )
    )

    config = CapturingScannerRunner.configs[0]
    assert [symbol.symbol for symbol in config.symbols] == ["BTCUSDT", "ETHUSDT"]


def test_save_run_writes_after_each_symbol(tmp_path, monkeypatch, capsys) -> None:
    save_path = tmp_path / "scan_runs" / "latest_scan.json"
    Phase20ScannerRunner.configs = []
    Phase20ScannerRunner.save_path = save_path
    monkeypatch.setattr(run_scan, "ScannerRunner", Phase20ScannerRunner)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "ETHUSDT",
                "--save-run",
                str(save_path),
                "--progress",
                "--show-no-setups",
            ]
        )
    )

    payload = json.loads(save_path.read_text(encoding="utf-8"))
    assert [result["symbol"] for result in payload["results"]] == ["BTCUSDT", "ETHUSDT"]
    assert "cache_stats" in payload
    assert payload["resume_metadata"]["save_run"] == str(save_path)
    captured = capsys.readouterr()
    assert "[1/2] BTCUSDT:" in captured.out
    assert "[2/2] ETHUSDT:" in captured.out


def test_valid_setup_ranks_above_near_miss() -> None:
    ranked = rank_scan_results((_near_miss_rank_result(), _valid_rank_result()))

    assert [item.symbol_result.symbol for item in ranked] == ["VALIDUSDT", "NEARUSDT"]
    assert ranked[0].display.display_bucket == "valid"
    assert ranked[1].display.display_bucket == "near_miss"


def test_near_miss_ranks_above_no_setup() -> None:
    ranked = rank_scan_results((_no_setup_rank_result(), _near_miss_rank_result()))

    assert [item.symbol_result.symbol for item in ranked] == ["NEARUSDT", "NOSETUPUSDT"]
    assert ranked[0].display.display_bucket == "near_miss"
    assert ranked[1].display.display_bucket == "no_setup"


def test_ranking_uses_setup_quality_priority_and_score() -> None:
    def quality_symbol(symbol: str, setup_quality) -> ScannerSymbolResult:
        return ScannerSymbolResult(
            symbol=symbol,
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
            setup_quality=setup_quality,
        )

    high = validate_setup_quality(
        {
            "setup_valid": True,
            "bias": "long",
            "rr_to_tp2": Decimal("3.5"),
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
            "derivatives_score": 90,
            "funding_status": "normal",
            "crowding_risk": "low",
            "risk_approved": True,
            "data_quality_score": Decimal("90"),
        }
    )
    lower = validate_setup_quality(
        {
            "setup_valid": True,
            "bias": "long",
            "rr_to_tp2": Decimal("2.1"),
            "sweep_passed": True,
            "confirmation_passed": True,
            "pullback_valid": True,
            "ob_or_fvg_valid": True,
            "fib_valid": True,
            "htf_2d_trend": "bullish",
            "mtf_12h_trend": "neutral",
            "trust_percentage": 76,
            "derivatives_supports_trade": "N/A",
            "risk_approved": True,
        }
    )
    watch = validate_setup_quality(
        {
            "setup_valid": False,
            "sweep_passed": True,
            "confirmation_passed": True,
            "pullback_valid": False,
            "rr_to_tp2": Decimal("3.0"),
            "first_failed_gate": "no_ob_or_fvg_zone",
            "gates_failed": ("no_ob_or_fvg_zone",),
        }
    )
    rejected = validate_setup_quality({"setup_valid": False, "first_failed_gate": "missing_confirmed_sweep"})
    data_issue = validate_setup_quality({"setup_valid": False, "first_failed_gate": "no_execution_candles"})

    ranked = rank_scan_results(
        (
            quality_symbol("DATAUSDT", data_issue),
            quality_symbol("WATCHUSDT", watch),
            quality_symbol("LOWERUSDT", lower),
            quality_symbol("REJECTUSDT", rejected),
            quality_symbol("HIGHUSDT", high),
        )
    )

    assert [item.symbol_result.symbol for item in ranked] == [
        "HIGHUSDT",
        "LOWERUSDT",
        "WATCHUSDT",
        "REJECTUSDT",
        "DATAUSDT",
    ]
    assert ranked[0].symbol_result.setup_quality.quality_state == SetupQualityState.HIGH_QUALITY_TRADE


def test_default_max_display_results_limits_to_ten() -> None:
    ranked = rank_scan_results(tuple(_valid_rank_result(f"VALID{index}USDT") for index in range(12)))
    visible = filter_ranked_results(ranked)

    assert len(visible) == 10


def test_valid_setup_display_label() -> None:
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        strategy_diagnostics={
            "challenge": {
                "is_valid": True,
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr"),
                "rr_to_tp2": Decimal("3.1"),
            }
        },
        valid_strategy_modes=("challenge",),
    )

    display = build_symbol_display(symbol_result)

    assert display.display_status == "valid_setup"
    assert display.display_status_label == "🟢 VALID SETUP"
    assert display.setup_progress_passed == 4


def test_no_ob_or_fvg_after_sweep_and_confirmation_is_near_miss() -> None:
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        strategy_diagnostics={
            "swing": {
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "failed",
                "rr_to_tp2": NA,
                "first_failed_gate": "no_ob_or_fvg_zone",
                "pullback_failure_reason": "No valid OB or FVG was found inside the 5m displacement impulse.",
            }
        },
        rejected_strategy_modes=("swing",),
    )

    display = build_symbol_display(symbol_result)

    assert display.display_status == "near_miss"
    assert display.display_status_label == "🟡 NEAR MISS"
    assert display.setup_progress_passed == 2
    assert display.failed_checks == ("Pullback zone", "RR")


def test_final_gate_failure_after_valid_pullback_is_near_miss() -> None:
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        strategy_diagnostics={
            "swing": {
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "rr_to_tp2": Decimal("2.8"),
                "first_failed_gate": "trust_meter_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr"),
                "gates_failed": ("trust_meter_below_minimum",),
            }
        },
        rejected_strategy_modes=("swing",),
    )

    display = build_symbol_display(symbol_result)

    assert display.display_status == "near_miss"
    assert display.display_status_label == "🟡 NEAR MISS"


def test_rr_failure_after_sweep_and_confirmation_is_near_miss() -> None:
    display = build_symbol_display(_near_miss_rank_result(failed_gate="rr_below_minimum"))

    assert display.display_status == "near_miss"
    assert display.display_bucket == "near_miss"
    assert display.failed_stage == "rr"


def test_no_setup_classification_for_early_core_gate_failure() -> None:
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        strategy_diagnostics={
            "challenge": {
                "execution_sweep_status": "failed",
                "confirmation_structure_shift_status": "not_evaluated",
                "first_failed_gate": "missing_confirmed_sweep",
            }
        },
        rejected_strategy_modes=("challenge",),
    )

    display = build_symbol_display(symbol_result)

    assert display.display_status == "no_setup"
    assert display.display_status_label == "\u26aa REJECTED"
    assert display.display_bucket == "no_setup"
    assert display.failed_checks == ("15m sweep",)


def test_failed_5m_confirmation_is_not_near_miss() -> None:
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        strategy_diagnostics={
            "challenge": {
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "failed",
                "first_failed_gate": "missing_confirmation_structure_shift",
                "gates_failed": ("missing_confirmation_structure_shift",),
                "confirmation_bos_choch_reason": "No 5m BOS/CHoCH close beyond the required LTF swing.",
            }
        },
        rejected_strategy_modes=("challenge",),
    )

    display = build_symbol_display(symbol_result)

    assert display.display_status == "no_setup"
    assert display.display_bucket == "no_setup"


def test_data_incomplete_classification_for_missing_required_market_data() -> None:
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        strategy_diagnostics={
            "challenge": {
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": NA,
                "first_failed_gate": "missing_confirmation_candles",
                "confirmation_bos_choch_reason": "5m confirmation candles missing.",
            }
        },
        rejected_strategy_modes=("challenge",),
    )

    display = build_symbol_display(symbol_result)

    assert display.display_status == "data_issue"
    assert display.display_status_label == "\U0001f534 DATA ISSUE"
    assert display.display_bucket == "data_issue"
    assert display.short_reason == "5m confirmation candles missing."


def test_strategy_cli_flags_accepted() -> None:
    args = run_scan.parse_args(
        [
            "--strategy",
            "liquidity_grab_pullback",
            "--modes",
            "challenge",
            "swing",
            "scalp",
            "--aggressive-toggle",
            "--show-strategy-output",
            "--htf-timeframe",
            "2d",
            "--bias-timeframe",
            "12h",
            "--execution-timeframe",
            "15m",
            "--confirmation-timeframe",
            "5m",
            "--replay",
            "--replay-candles",
            "1000",
            "--same-candle-policy",
            "conservative",
            "--replay-max-hold-candles",
            "48",
            "--replay-max-fill-candles",
            "12",
            "--telegram-format",
        ]
    )

    assert args.strategy == "liquidity_grab_pullback"
    assert args.modes == ["challenge", "swing", "scalp"]
    assert args.aggressive_toggle is True
    assert args.show_strategy_output is True
    assert args.htf_timeframe == "2d"
    assert args.bias_timeframe == "12h"
    assert args.execution_timeframe == "15m"
    assert args.confirmation_timeframe == "5m"
    assert args.replay is True
    assert args.replay_candles == 1000
    assert args.same_candle_policy == "conservative"
    assert args.replay_max_hold_candles == 48
    assert args.replay_max_fill_candles == 12
    assert args.telegram_format is True


def test_replay_candles_above_binance_limit_clamps_with_diagnostic_warning() -> None:
    args = run_scan.parse_args(["--replay", "--replay-candles", "2000"])
    warnings: list[str] = []

    limit = run_scan._replay_primary_fetch_limit(args, warnings)

    assert limit == 500
    assert warnings == ["replay_candles limit clamped from 2000 to 500 for safe replay maximum 500."]


def test_output_json_writes_mocked_scanner_result(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "scan_output.json"
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "--output-json",
                str(output_path),
            ]
        )
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    serialized = output_path.read_text(encoding="utf-8")
    assert payload["cache_stats"]["enabled"] is True
    assert payload["runtime_stats"]["total_runtime_seconds"] == 0.0
    assert payload["runtime_stats"]["completed_symbols"] == 1
    assert payload["resume_metadata"]["resume_from"] is None
    assert payload["results"][0]["symbol"] == "BTCUSDT"
    assert payload["results"][0]["runtime_seconds"] is None
    assert payload["results"][0]["timeout_status"] == "none"
    assert payload["results"][0]["candles_fetched"] == 250
    assert payload["results"][0]["rejection_reasons"] == ["No sweep, BOS, or CHoCH context was detected."]
    assert payload["results"][0]["strategy_name"] == "liquidity_grab_pullback"
    assert payload["results"][0]["volume_profile"]["source"] == VOLUME_PROFILE_SOURCE
    assert payload["results"][0]["volume_profile"]["poc"] == "80750"
    assert payload["results"][0]["volume_profile_source"] == VOLUME_PROFILE_SOURCE
    assert payload["results"][0]["derivatives_enrichment"]["funding_status"] == "normal"
    assert payload["results"][0]["derivatives_enrichment"]["price_oi_relationship"] == "long_building_or_breakout_participation"
    assert payload["results"][0]["derivatives_enrichment"]["derivatives_score"] == 90
    assert "derivatives_context_score" not in payload["results"][0]
    assert "derivatives_context_score" not in payload["results"][0]["derivatives_enrichment"]
    assert "api_key" not in serialized.lower()
    assert payload["results"][0]["formatted_strategy_output"].startswith("Challenge Setup")
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["gates_failed"] == [
        "missing_confirmation_structure_shift"
    ]
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["htf_timeframe"] == "2d"
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["bias_timeframe"] == "12h"
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["execution_timeframe"] == "15m"
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["confirmation_timeframe"] == "5m"
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["htf_2d_context_source"] == "synthetic_from_1d"
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["candles_12h_count"] == 250
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["candles_15m_count"] == 250
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["candles_5m_count"] == 250
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["execution_sweep_status"] == "passed"
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["confirmation_structure_shift_status"] == "failed"
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["pullback_zone_status"] == "N/A"
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["rr_to_tp2"] == "N/A"
    assert (
        payload["results"][0]["strategy_diagnostics"]["challenge"]["confirmation_bos_choch_reason"]
        == "No 5m BOS/CHoCH close beyond the required LTF swing."
    )
    assert (
        payload["results"][0]["strategy_diagnostics"]["challenge"]["first_failed_gate"]
        == "missing_confirmation_structure_shift"
    )
    assert "candles_2d: N/A" in payload["results"][0]["strategy_missing_data"]
    assert payload["results"][0]["display_rank"] == 1
    assert payload["results"][0]["display_bucket"] == "no_setup"
    assert isinstance(payload["results"][0]["display_priority_score"], int)
    assert (
        payload["results"][0]["display_reason"]
        == "No 5m BOS/CHoCH close beyond the required LTF swing."
    )
    assert payload["results"][0]["hidden_by_default"] is True
    assert payload["results"][0]["failed_stage"] == "structure"
    assert payload["results"][0]["display_status"] == "no_setup"
    assert payload["results"][0]["display_status_label"] == "\u26aa REJECTED"
    assert payload["results"][0]["setup_progress_total"] == 4
    assert payload["results"][0]["setup_progress_passed"] == 1
    assert payload["results"][0]["passed_checks"] == ["15m sweep"]
    assert payload["results"][0]["failed_checks"] == ["5m BOS/CHoCH"]
    assert payload["results"][0]["short_reason"] == "No 5m BOS/CHoCH close beyond the required LTF swing."
    assert payload["results"][0]["action_label"] == "Wait for confirmation"
    assert payload["results"][0]["near_miss_intelligence"]["primary_failed_gate"] == (
        "missing_confirmation_structure_shift"
    )
    assert payload["results"][0]["near_miss_intelligence"]["action_label"] == "Wait for confirmation"
    assert payload["results"][0]["setup_quality"]["quality_state"] == "REJECTED_NO_EDGE"
    assert payload["results"][0]["setup_quality"]["quality_grade"] == "Reject"
    assert payload["results"][0]["setup_quality"]["action_label"] == "Wait for confirmation"
    assert payload["results"][0]["setup_quality"]["decision_reason"] == (
        "Sweep passed but 5m BOS/CHoCH confirmation is missing."
    )


def test_replay_output_json_includes_replay_result(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "scan_output.json"
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    async def fake_run_replay(args, watchlist, scanner_config, cache):
        return _fake_replay_summary()

    monkeypatch.setattr(run_scan, "_run_replay", fake_run_replay)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--replay", "--output-json", str(output_path)]))

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["replay_result"]["strategy"] == "Liquidity Grab Pullback"
    assert payload["replay_result"]["stats"]["total_setups"] == 1
    assert payload["replay_result"]["symbols"][0]["sample_size_warning"] == "low_sample_size"


def test_replay_cli_output_includes_replay_summary(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    async def fake_run_replay(args, watchlist, scanner_config, cache):
        return _fake_replay_summary()

    monkeypatch.setattr(run_scan, "_run_replay", fake_run_replay)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--replay", "--show-no-setups"]))

    captured = capsys.readouterr()
    assert "Candle Craft Replay" in captured.out
    assert "Replay edge: mixed" in captured.out
    assert "Sample size warning: low_sample_size" in captured.out


def test_normal_scanner_does_not_run_replay_when_flag_is_absent(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    async def fail_run_replay(args, watchlist, scanner_config, cache):
        raise AssertionError("replay should not run")

    monkeypatch.setattr(run_scan, "_run_replay", fail_run_replay)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--show-no-setups"]))

    captured = capsys.readouterr()
    assert "Candle Craft Scanner" in captured.out
    assert "Candle Craft Replay" not in captured.out


def test_show_strategy_output_prints_formatted_output(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--show-no-setups", "--show-strategy-output"]))

    captured = capsys.readouterr()
    assert "BTCUSDT Candle Craft strategy output:" in captured.out
    assert "Challenge: No valid challenge setup." in captured.out
    assert "Swing: No valid swing setup." in captured.out
    assert "Scalp: No valid scalp setup." in captured.out


def test_show_strategy_output_with_telegram_format_prints_clean_message(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "--diagnostics-level",
                "summary",
                "--show-no-setups",
                "--show-strategy-output",
                "--telegram-format",
            ]
        )
    )

    captured = capsys.readouterr()
    assert "BTCUSDT Candle Craft strategy output:" in captured.out
    assert "BTCUSDT — No valid trade" in captured.out
    assert "Action: Wait for confirmation" in captured.out
    assert "Reason: Sweep passed but 5m BOS/CHoCH confirmation is missing." in captured.out
    assert "⚔️ Candle Craft | Signal. Structure. Execution." in captured.out
    assert "Challenge: No valid challenge setup." not in captured.out


def test_display_compact_prints_dashboard_and_one_line_result(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--display", "compact", "--show-no-setups"]))

    captured = capsys.readouterr()
    assert "Candle Craft Scanner" in captured.out
    assert "#1 BTCUSDT — REJECTED_NO_EDGE" in captured.out
    assert "Reject" in captured.out
    assert "Wait for confirmation" in captured.out
    assert "Progress 1/4" not in captured.out
    assert "Gate: missing_confirmation_structure_shift" not in captured.out
    assert "2D HTF:" not in captured.out


def test_default_display_hides_no_setups(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--display", "compact"]))

    captured = capsys.readouterr()
    assert "⚪ Hidden rejected/no-setup symbols: 1" in captured.out
    assert "BTCUSDT — REJECTED" not in captured.out


def test_dashboard_includes_phase_20_cache_and_error_counts() -> None:
    config = ScannerRunConfig.model_validate(
        {
            "symbols": ["BTCUSDT", "FAILUSDT"],
            "exchange": "binance",
            "account_equity": Decimal("1000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )
    result = ScannerRunResult(
        config=config,
        results=(
            _no_setup_rank_result("BTCUSDT"),
            ScannerSymbolResult(
                symbol="FAILUSDT",
                status=ScannerPipelineStatus.SCAN_ERROR,
                status_history=(ScannerPipelineStatus.SCAN_ERROR,),
                error_message="mocked failure",
                rejection_reason="mocked failure",
                rejection_stage="scanner",
                rejection_reasons=("mocked failure",),
            ),
        ),
        scanned_symbols=2,
        failed_symbols=1,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
        cache_stats={"hits": 3, "misses": 5},
    )

    text = run_scan.format_scan_dashboard(result)

    assert "Symbols scanned: 2" in text
    assert "Completed: 1" in text
    assert "Runtime:" in text
    assert "Average per symbol:" in text
    assert "Slowest symbol:" in text
    assert "Timeouts:" in text
    assert "Skipped/errored: 1" in text
    assert "No setup: 1" in text
    assert "Scan errors: 1" in text
    assert "Cache hits: 3" in text
    assert "Cache misses: 5" in text


def test_retry_warnings_hidden_in_normal_mode(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", RetryDiagnosticsScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "WARNUSDT", "--display", "compact"]))

    captured = capsys.readouterr()
    assert "Data warnings: 1 optional endpoint warnings." in captured.out
    assert "Retry diagnostics:" not in captured.out
    assert "binance_futures GET /fapi/v1/fundingRate" not in captured.out
    assert "mocked retry failure" not in captured.out


def test_retry_details_visible_in_full_diagnostics(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", RetryDiagnosticsScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "WARNUSDT", "--diagnostics-level", "full"]))

    captured = capsys.readouterr()
    assert "Retry diagnostics:" in captured.out
    assert "binance_futures GET /fapi/v1/fundingRate" in captured.out
    assert "attempt 1/3" in captured.out


def test_json_preserves_retry_and_optional_warning_diagnostics(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "scan_output.json"
    monkeypatch.setattr(run_scan, "ScannerRunner", RetryDiagnosticsScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "WARNUSDT", "--output-json", str(output_path)]))

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["retry_diagnostics"][0]["operation"] == "binance_futures GET /fapi/v1/fundingRate"
    assert payload["retry_diagnostics"][0]["error_type"] == "ExchangeRateLimitError"
    assert payload["results"][0]["derivatives_warnings"] == [
        "funding_rate unavailable from public endpoint: mocked retry failure"
    ]


def test_bucket_filter_reveals_selected_no_setup_bucket(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--display", "compact", "--bucket-filter", "no_setup"]))

    captured = capsys.readouterr()
    assert "#1 BTCUSDT — REJECTED_NO_EDGE" in captured.out


def test_display_normal_prints_premium_card(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--display", "normal", "--show-no-setups"]))

    captured = capsys.readouterr()
    assert "Candle Craft Scanner" in captured.out
    assert "Strategy: Liquidity Grab Pullback" in captured.out
    assert "Timeframes: 2D → 12H → 15m → 5m" in captured.out
    assert "🟢 Valid setups: 0" in captured.out
    assert "⚪ Hidden rejected/no-setup symbols: 0" in captured.out
    assert "Phase 12 Scanner Runner" not in captured.out
    assert "#1 ⚪ BTCUSDT — REJECTED" in captured.out
    assert "• Bucket: ⚪ REJECTED" in captured.out
    assert "• Mode(s): rejected challenge, swing, scalp" in captured.out
    assert "• HTF/Bias/Execution:" in captured.out
    assert "• Quality: REJECTED_NO_EDGE | Grade: Reject | Score:" in captured.out
    assert "• Edge:" in captured.out
    assert "• Risk:" in captured.out
    assert "• Action: Wait for confirmation" in captured.out
    assert "• Reason: Sweep passed but 5m BOS/CHoCH confirmation is missing." in captured.out
    assert "📍 Context" in captured.out
    assert "• 2D HTF: Bearish" in captured.out
    assert "• 12H Bias: Neutral" in captured.out
    assert "• Volume Profile: POC 80,750 | VAH 81,200 | VAL 80,100" in captured.out
    assert "• Derivatives: Funding normal | OI rising | Crowding low" in captured.out
    assert "✅ Passed" in captured.out
    assert "• 15m sweep detected" in captured.out
    assert "• Context score: 90" in captured.out
    assert "Derivatives score:" not in captured.out
    assert "❌ Failed" in captured.out
    assert "• 5m BOS/CHoCH: failed" in captured.out
    assert "📊 Setup Progress: 1/4" in captured.out
    assert "🧠 Reason" in captured.out
    assert "🎯 Action" in captured.out
    assert "Wait for confirmation" in captured.out


def test_display_full_preserves_detailed_diagnostics(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--display", "full", "--show-no-setups"]))

    captured = capsys.readouterr()
    assert "Strategy diagnostics:" in captured.out
    assert "challenge 2D context: synthetic from 1D" in captured.out
    assert "challenge 12H bias: direct" in captured.out
    assert "challenge POC: 80750" in captured.out
    assert "challenge 15m execution sweep: passed" in captured.out
    assert "challenge 5m confirmation BOS/CHoCH: failed" in captured.out
    assert "challenge Pullback Zone: N/A | OB/FVG: N/A | Fib: N/A | RR: N/A" in captured.out
    assert "Derivatives enrichment:" in captured.out
    assert "Derivatives context score: 90" in captured.out
    assert "Setup quality:" in captured.out
    assert "State: REJECTED_NO_EDGE" in captured.out


def test_pullback_rejection_normal_formatting_is_readable() -> None:
    text = run_scan._pullback_normal_text(
        {
            "pullback_zone_status": "failed",
            "selected_zone_type": NA,
            "fib_alignment_status": "pullback_too_deep",
            "rr_to_tp2": NA,
            "first_failed_gate": "pullback_too_deep",
            "pullback_failure_reason": "Pullback tagged beyond 0.786 before entry.",
        }
    )

    assert text == (
        "Pullback:\n"
        "Status: failed\n"
        "OB/FVG: N/A\n"
        "Fib: pullback_too_deep\n"
        "RR: N/A"
    )


def test_normal_block_prints_single_failed_gate_reason_for_pullback() -> None:
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        strategy_diagnostics={
            "swing": {
                "htf_2d_trend": "bullish",
                "htf_2d_context_source": "synthetic_from_1d",
                "mtf_12h_trend": "bullish",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "failed",
                "pullback_calculation_timeframe": "5m",
                "selected_zone_type": "N/A",
                "fib_alignment_status": "N/A",
                "rr_to_tp2": "N/A",
                "first_failed_gate": "no_ob_or_fvg_zone",
                "pullback_failure_reason": "No valid OB or FVG was found inside the 5m displacement impulse.",
                "sweep_diagnostics": "passed: bullish sweep at candle 30; magnitude 5 (0.5 ATR).",
            }
        },
        rejected_strategy_modes=("swing",),
    )

    text = run_scan._format_symbol_normal_block(symbol_result)

    assert text.count("Failed gate: no_ob_or_fvg_zone") == 1
    assert text.count("Reason") == 1
    assert "Reject: no_ob_or_fvg_zone" not in text
    assert "No valid OB or FVG was found inside the 5m displacement impulse." in text
    assert "🟡 BTCUSDT — NEAR MISS" in text
    assert "Needs next:" in text
    assert "Action: Watchlist only" in text


def test_verbose_maps_to_full_diagnostics(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--verbose", "--show-no-setups"]))

    captured = capsys.readouterr()
    assert "Strategy diagnostics:" in captured.out
    assert "challenge 5m confirmation BOS/CHoCH: failed" in captured.out


def test_diagnostics_level_overrides_verbose(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "--verbose",
                "--diagnostics-level",
                "summary",
                "--display",
                "compact",
                "--show-no-setups",
            ]
        )
    )

    captured = capsys.readouterr()
    assert "BTCUSDT — REJECTED_NO_EDGE" in captured.out
    assert "Strategy diagnostics:" not in captured.out
