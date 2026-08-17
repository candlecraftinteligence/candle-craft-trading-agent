from __future__ import annotations

from decimal import Decimal

from app.analytics.market_regime import (
    MarketRegimeInput,
    RegimeRiskLevel,
    RegimeState,
    disabled_market_regime_result,
    evaluate_market_regime,
)
from app.formatters.scanner_display import format_scan_dashboard
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from scripts import run_scan

REGIME_INTERVAL_MS = 12 * 60 * 60_000


def _trend_candles(*, start: str = "100", step: str = "1", count: int = 90, wick: str = "1") -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    price = Decimal(start)
    step_value = Decimal(step)
    wick_value = Decimal(wick)
    for index in range(count):
        price = Decimal(start) + step_value * Decimal(index)
        candles.append(
            {
                "timestamp": index * REGIME_INTERVAL_MS,
                "open": price - step_value / Decimal("2"),
                "high": price + wick_value,
                "low": price - wick_value,
                "close": price,
                "volume": Decimal("100"),
            }
        )
    return candles


def _compression_candles() -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    for index in range(70):
        close = Decimal("100") + (Decimal(index % 3) - Decimal("1")) * Decimal("0.1")
        candles.append({"timestamp": index * REGIME_INTERVAL_MS, "open": close, "high": close + Decimal("2"), "low": close - Decimal("2"), "close": close, "volume": Decimal("100")})
    for index in range(70, 90):
        close = Decimal("100") + (Decimal(index % 2) * Decimal("0.02"))
        candles.append({"timestamp": index * REGIME_INTERVAL_MS, "open": close, "high": close + Decimal("0.15"), "low": close - Decimal("0.15"), "close": close, "volume": Decimal("100")})
    return candles


def _panic_candles() -> list[dict[str, Decimal | int]]:
    candles = _trend_candles(start="100", step="0.1", count=70, wick="0.75")
    for index in range(70, 90):
        close = Decimal("110") + Decimal(index - 70) * Decimal("0.8")
        candles.append({"timestamp": index * REGIME_INTERVAL_MS, "open": close - Decimal("2"), "high": close + Decimal("9"), "low": close - Decimal("9"), "close": close, "volume": Decimal("500")})
    return candles


def test_trend_expansion_classification() -> None:
    result = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_trend_candles(start="100", step="1"),
            eth_candles=_trend_candles(start="80", step="0.8"),
            bullish_bias_pct=Decimal("70"),
            bearish_bias_pct=Decimal("20"),
            valid_sweep_pct=Decimal("35"),
            confirmation_pct=Decimal("60"),
        )
    )

    assert result.state == RegimeState.TREND_EXPANSION
    assert result.risk_level == RegimeRiskLevel.LOW
    # Regime compatibility remains informational-only; live risk and RR are neutral.
    assert result.adjustment.risk_multiplier == Decimal("1.00000000")


def test_chop_classification() -> None:
    result = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_trend_candles(start="100", step="0.4"),
            eth_candles=_trend_candles(start="140", step="-0.4"),
            bullish_bias_pct=Decimal("48"),
            bearish_bias_pct=Decimal("45"),
            valid_sweep_pct=Decimal("55"),
            confirmation_pct=Decimal("25"),
            failed_confirmation_pct=Decimal("55"),
        )
    )

    assert result.state == RegimeState.CHOP
    assert result.risk_level == RegimeRiskLevel.HIGH


def test_compression_classification() -> None:
    result = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_compression_candles(),
            eth_candles=_compression_candles(),
            bullish_bias_pct=Decimal("40"),
            bearish_bias_pct=Decimal("35"),
        )
    )

    assert result.state == RegimeState.COMPRESSION
    assert result.risk_level == RegimeRiskLevel.MEDIUM


def test_panic_volatility_classification() -> None:
    result = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_panic_candles(),
            eth_candles=_panic_candles(),
            bullish_bias_pct=Decimal("50"),
            bearish_bias_pct=Decimal("35"),
        )
    )

    assert result.state == RegimeState.PANIC_VOLATILITY
    assert result.risk_level == RegimeRiskLevel.EXTREME


def test_data_incomplete_classification() -> None:
    result = evaluate_market_regime(MarketRegimeInput(btc_candles=_trend_candles(count=20), eth_candles=()))

    assert result.state == RegimeState.DATA_INCOMPLETE
    assert result.risk_level == RegimeRiskLevel.NA
    assert "BTCUSDT_candles: N/A" in result.missing_data
    assert "ETHUSDT_candles: N/A" in result.missing_data


def test_regime_adjustment_for_high_volatility() -> None:
    result = evaluate_market_regime(MarketRegimeInput(btc_candles=_panic_candles(), eth_candles=_panic_candles()))

    assert result.adjustment.allow_challenge is False
    assert result.adjustment.risk_multiplier == Decimal("1.00000000")
    assert result.adjustment.min_rr_adjustment == Decimal("0")
    assert "Panic volatility detected" in result.adjustment.explanation


def test_regime_adjustment_for_chop() -> None:
    result = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_trend_candles(start="100", step="0.4"),
            eth_candles=_trend_candles(start="140", step="-0.4"),
            valid_sweep_pct=Decimal("55"),
            confirmation_pct=Decimal("25"),
            failed_confirmation_pct=Decimal("55"),
        )
    )

    assert result.adjustment.allow_scalps is False
    assert result.adjustment.risk_multiplier == Decimal("1.00000000")
    assert result.adjustment.min_rr_adjustment == Decimal("0")
    assert "Choppy regime" in result.adjustment.explanation


def test_scanner_dashboard_includes_regime() -> None:
    config = ScannerRunConfig.model_validate(
        {"symbols": ["BTCUSDT"], "exchange": "binance", "account_equity": Decimal("1000"), "risk_per_trade_pct": Decimal("1")}
    )
    regime = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_trend_candles(),
            eth_candles=_trend_candles(start="80", step="0.8"),
            bullish_bias_pct=Decimal("70"),
        )
    )
    result = ScannerRunResult(
        config=config,
        results=(),
        scanned_symbols=0,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
        market_regime=regime,
        regime_adjustments=regime.adjustment,
        regime_warnings=regime.warnings,
    )

    text = format_scan_dashboard(result)

    assert "Market Climate" in text
    assert "State: TREND_EXPANSION" in text
    assert "Risk: LOW" in text
    assert "Trade permission: Scalp yes | Swing yes | Challenge yes" in text


def test_json_includes_regime() -> None:
    config = ScannerRunConfig.model_validate(
        {"symbols": ["BTCUSDT"], "exchange": "binance", "account_equity": Decimal("1000"), "risk_per_trade_pct": Decimal("1")}
    )
    regime = disabled_market_regime_result()
    result = ScannerRunResult(
        config=config,
        results=(ScannerSymbolResult(symbol="BTCUSDT", status=ScannerPipelineStatus.SCANNED_NO_SETUP, status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,)),),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
        market_regime=regime,
        regime_adjustments=regime.adjustment,
        regime_warnings=regime.warnings,
    )

    payload = run_scan._json_payload(result)

    assert payload["market_regime"]["enabled"] is False
    assert payload["regime_adjustments"]["explanation"] == "Market climate filter disabled."
    assert payload["regime_warnings"] == ["Market climate filter disabled."]


def test_disable_regime_filter_cli_flag() -> None:
    args = run_scan.parse_args(["--symbols", "BTCUSDT", "--disable-regime-filter"])

    assert args.market_regime is False


def test_regime_filter_does_not_create_trades_from_invalid_setups() -> None:
    config = ScannerRunConfig.model_validate(
        {"symbols": ["BTCUSDT"], "exchange": "binance", "account_equity": Decimal("1000"), "risk_per_trade_pct": Decimal("1")}
    )
    regime = evaluate_market_regime(
        MarketRegimeInput(
            btc_candles=_trend_candles(),
            eth_candles=_trend_candles(start="80", step="0.8"),
            bullish_bias_pct=Decimal("75"),
        )
    )
    result = ScannerRunResult(
        config=config,
        results=(ScannerSymbolResult(symbol="BTCUSDT", status=ScannerPipelineStatus.SCANNED_NO_SETUP, status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,)),),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
        market_regime=regime,
        regime_adjustments=regime.adjustment,
        regime_warnings=regime.warnings,
    )

    payload = run_scan._json_payload(result)

    assert payload["trade_ideas_created"] == 0
    assert payload["results"][0]["trade_idea"] is None
    assert payload["results"][0]["valid_strategy_modes"] == []
