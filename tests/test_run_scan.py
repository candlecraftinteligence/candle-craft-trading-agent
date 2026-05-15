from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from app.analytics.derivatives_enrichment import DerivativesEnrichmentResult
from app.analytics.volume_profile import VOLUME_PROFILE_SOURCE, VolumeProfileResult
from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunResult, ScannerSymbolResult
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


def test_near_miss_classification_requires_sweep_and_confirmation_then_later_failure() -> None:
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
    assert display.display_status_label == "🔴 NO SETUP"
    assert display.failed_checks == ("15m sweep",)


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

    assert display.display_status == "data_incomplete"
    assert display.display_status_label == "⚪ DATA INCOMPLETE"
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
    assert args.telegram_format is True


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
    assert payload["results"][0]["symbol"] == "BTCUSDT"
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
    assert payload["results"][0]["display_status"] == "no_setup"
    assert payload["results"][0]["display_status_label"] == "\U0001F534 NO SETUP"
    assert payload["results"][0]["setup_progress_total"] == 4
    assert payload["results"][0]["setup_progress_passed"] == 1
    assert payload["results"][0]["passed_checks"] == ["15m sweep"]
    assert payload["results"][0]["failed_checks"] == ["5m BOS/CHoCH"]
    assert payload["results"][0]["short_reason"] == "No 5m BOS/CHoCH close beyond the required LTF swing."
    assert payload["results"][0]["action_label"] == "No trade idea, no alert, no journal entry."


def test_show_strategy_output_prints_formatted_output(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--show-strategy-output"]))

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
                "--show-strategy-output",
                "--telegram-format",
            ]
        )
    )

    captured = capsys.readouterr()
    assert "BTCUSDT Candle Craft strategy output:" in captured.out
    assert "BTCUSDT — No Valid Setup" in captured.out
    assert "❌ Failed" in captured.out
    assert "• Gate: missing_confirmation_structure_shift" in captured.out
    assert "🧠 Why" in captured.out
    assert "No 5m BOS/CHoCH close beyond the required LTF swing." in captured.out
    assert "No valid setup. No trade. Watching only." in captured.out
    assert "⚔️ Candle Craft | Signal. Structure. Execution." in captured.out
    assert "Challenge: No valid challenge setup." not in captured.out


def test_display_compact_prints_dashboard_and_one_line_result(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--display", "compact"]))

    captured = capsys.readouterr()
    assert "Candle Craft Scanner" in captured.out
    assert "🔴 BTCUSDT — NO SETUP | Progress 1/4" in captured.out
    assert "Gate: missing_confirmation_structure_shift" in captured.out
    assert "2D HTF:" not in captured.out


def test_display_normal_prints_premium_card(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--display", "normal"]))

    captured = capsys.readouterr()
    assert "Candle Craft Scanner" in captured.out
    assert "Strategy: Liquidity Grab Pullback" in captured.out
    assert "Timeframes: 2D → 12H → 15m → 5m" in captured.out
    assert "🟢 Valid setups: 0" in captured.out
    assert "🔴 No setups: 1" in captured.out
    assert "Phase 12 Scanner Runner" not in captured.out
    assert "🔴 BTCUSDT — NO SETUP" in captured.out
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
    assert "No trade idea, no alert, no journal entry." in captured.out


def test_display_full_preserves_detailed_diagnostics(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--display", "full"]))

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

    assert text.count("Gate: no_ob_or_fvg_zone") == 1
    assert text.count("Reason") == 1
    assert "Reject: no_ob_or_fvg_zone" not in text
    assert "No valid OB or FVG was found inside the 5m displacement impulse." in text
    assert "🟡 BTCUSDT — NEAR MISS" in text


def test_verbose_maps_to_full_diagnostics(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--verbose"]))

    captured = capsys.readouterr()
    assert "Strategy diagnostics:" in captured.out
    assert "challenge 5m confirmation BOS/CHoCH: failed" in captured.out


def test_diagnostics_level_overrides_verbose(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(
        run_scan.main(["--symbols", "BTCUSDT", "--verbose", "--diagnostics-level", "summary", "--display", "compact"])
    )

    captured = capsys.readouterr()
    assert "🔴 BTCUSDT — NO SETUP" in captured.out
    assert "Strategy diagnostics:" not in captured.out
