from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunResult, ScannerSymbolResult
from scripts import run_scan


class FakeScannerRunner:
    async def run(self, config):
        symbol_result = ScannerSymbolResult(
            symbol="BTCUSDT",
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
            rejection_reason="No sweep, BOS, or CHoCH context was detected.",
            candles_fetched=250,
            latest_close=Decimal("104250.5"),
            technical_score=62,
            derivatives_score=55,
            trend_context="bullish",
            recent_range_high=Decimal("105000"),
            recent_range_low=Decimal("103200"),
            latest_swing_high=Decimal("104900"),
            latest_swing_low=Decimal("103700"),
            rejection_stage="technical",
            rejection_reasons=("No sweep, BOS, or CHoCH context was detected.",),
            strategy_name="liquidity_grab_pullback",
            formatted_strategy_output="Challenge Setup\nNo valid challenge setup.\n\nSwing Setup\nNo valid swing setup.\n\nScalp Setup\nNo valid scalp setup.",
            strategy_diagnostics={
                "challenge": {
                    "is_valid": False,
                    "htf_2d_context_source": "synthetic_from_1d",
                    "candles_2d_count": 125,
                    "candles_12h_count": 250,
                    "candles_15m_count": 250,
                    "candles_5m_count": 250,
                    "htf_2d_trend": "bullish",
                    "mtf_12h_trend": "bullish",
                    "ltf_confirmation_timeframe": "15m",
                    "ltf_confirmation_status": "missing",
                    "first_failed_gate": "missing_confirmed_sweep",
                    "gates_failed": ("missing_confirmed_sweep",),
                    "hard_rejection_reasons": ("Confirmed liquidity sweep is required.",),
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
    assert payload["results"][0]["symbol"] == "BTCUSDT"
    assert payload["results"][0]["candles_fetched"] == 250
    assert payload["results"][0]["rejection_reasons"] == ["No sweep, BOS, or CHoCH context was detected."]
    assert payload["results"][0]["strategy_name"] == "liquidity_grab_pullback"
    assert payload["results"][0]["formatted_strategy_output"].startswith("Challenge Setup")
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["gates_failed"] == ["missing_confirmed_sweep"]
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["htf_2d_context_source"] == "synthetic_from_1d"
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["candles_12h_count"] == 250
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["candles_15m_count"] == 250
    assert payload["results"][0]["strategy_diagnostics"]["challenge"]["candles_5m_count"] == 250
    assert "candles_2d: N/A" in payload["results"][0]["strategy_missing_data"]
    assert "api_key" not in output_path.read_text(encoding="utf-8").lower()


def test_show_strategy_output_prints_formatted_output(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--show-strategy-output"]))

    captured = capsys.readouterr()
    assert "BTCUSDT Candle Craft strategy output:" in captured.out
    assert "No valid challenge setup." in captured.out


def test_verbose_output_shows_phase_121_timeframe_context(monkeypatch, capsys) -> None:
    monkeypatch.setattr(run_scan, "ScannerRunner", FakeScannerRunner)

    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", "--verbose"]))

    captured = capsys.readouterr()
    assert "challenge 2D context: synthetic from 1D" in captured.out
    assert "challenge 12H context: direct" in captured.out
    assert "challenge LTF confirmation: 15m / 5m" in captured.out
