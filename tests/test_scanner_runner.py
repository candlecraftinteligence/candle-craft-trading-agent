from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

from app.agents.alert_agent import AlertAgent
from app.analytics.volume_profile import VOLUME_PROFILE_SOURCE
from app.data.dtos import NA
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunner


def run(coro: Any) -> Any:
    return asyncio.run(coro)


def _trend_candles_with_valid_setup() -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    for index in range(220):
        price = Decimal(100 + index)
        candles.append(
            {
                "timestamp": index,
                "open": price,
                "high": price + Decimal("1"),
                "low": price - Decimal("1"),
                "close": price,
                "volume": Decimal("100"),
            }
        )

    candles[170]["low"] = Decimal("150")
    candles[180]["high"] = Decimal("350")
    candles[-1]["open"] = Decimal("359")
    candles[-1]["high"] = Decimal("365")
    candles[-1]["low"] = Decimal("149")
    candles[-1]["close"] = Decimal("360")
    candles[-1]["volume"] = Decimal("500")
    return candles


def _flat_candles() -> list[dict[str, Decimal | int]]:
    return [
        {
            "timestamp": index,
            "open": Decimal("100"),
            "high": Decimal("101"),
            "low": Decimal("99"),
            "close": Decimal("100"),
            "volume": Decimal("100"),
        }
        for index in range(220)
    ]


def _strategy_pullback_candles() -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    for index in range(184):
        candles.append(
            {
                "timestamp": index,
                "open": Decimal("100"),
                "high": Decimal("105"),
                "low": Decimal("95"),
                "close": Decimal("100"),
                "volume": Decimal("100"),
            }
        )

    pattern: list[dict[str, Decimal | int]] = []
    for index in range(36):
        pattern.append(
            {
                "timestamp": 184 + index,
                "open": Decimal("100"),
                "high": Decimal("105"),
                "low": Decimal("95"),
                "close": Decimal("100"),
                "volume": Decimal("100"),
            }
        )

    pattern[20]["low"] = Decimal("90")
    pattern[24]["high"] = Decimal("110")
    pattern[30]["low"] = Decimal("85")
    pattern[30]["close"] = Decimal("91")
    pattern[30]["volume"] = Decimal("200")
    pattern[33]["open"] = Decimal("99")
    pattern[33]["close"] = Decimal("97")
    pattern[33]["low"] = Decimal("95")
    pattern[33]["high"] = Decimal("100")
    pattern[35]["open"] = Decimal("104")
    pattern[35]["high"] = Decimal("114")
    pattern[35]["low"] = Decimal("101")
    pattern[35]["close"] = Decimal("112")
    pattern[35]["volume"] = Decimal("300")
    return candles + pattern


def _bos_without_stop_candles() -> list[dict[str, Decimal | int]]:
    candles: list[dict[str, Decimal | int]] = []
    for index in range(220):
        price = Decimal(100 + index)
        candles.append(
            {
                "timestamp": index,
                "open": price,
                "high": price + Decimal("1"),
                "low": price - Decimal("1"),
                "close": price,
                "volume": Decimal("100"),
            }
        )
    candles[180]["high"] = Decimal("350")
    candles[-1]["open"] = Decimal("359")
    candles[-1]["high"] = Decimal("365")
    candles[-1]["low"] = Decimal("355")
    candles[-1]["close"] = Decimal("360")
    candles[-1]["volume"] = Decimal("500")
    return candles


class FakeExchangeClient:
    def __init__(
        self,
        candles_by_symbol: dict[str, list[dict[str, Decimal | int]]],
        *,
        funding: Decimal | str = Decimal("0.0001"),
        open_interest: Decimal | str = Decimal("105"),
        previous_open_interest: Decimal | str = Decimal("100"),
        failing_symbols: set[str] | None = None,
        failing_timeframes: set[str] | None = None,
    ) -> None:
        self.candles_by_symbol = candles_by_symbol
        self.funding = funding
        self.open_interest = open_interest
        self.previous_open_interest = previous_open_interest
        self.failing_symbols = failing_symbols or set()
        self.failing_timeframes = failing_timeframes or set()
        self.requested_symbols: list[str] = []
        self.requested_klines: list[tuple[str, str]] = []

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int]]:
        self.requested_symbols.append(symbol)
        self.requested_klines.append((symbol, interval))
        if symbol in self.failing_symbols:
            raise RuntimeError(f"mocked kline failure for {symbol}")
        if interval in self.failing_timeframes:
            raise RuntimeError(f"mocked kline failure for {symbol} {interval}")
        return self.candles_by_symbol[symbol][-limit:]

    async def get_ticker(self, symbol: str) -> dict[str, Decimal | str | int]:
        close = self.candles_by_symbol.get(symbol, _flat_candles())[-1]["close"]
        return {
            "symbol": symbol,
            "last_price": close,
            "price_change_ratio_24h": Decimal("0.01"),
            "quote_volume_24h": Decimal("100000000"),
        }

    async def get_funding_rate(self, symbol: str) -> dict[str, Decimal | str | int]:
        if self.funding == NA:
            raise RuntimeError("funding unavailable")
        return {"symbol": symbol, "funding_rate": self.funding, "timestamp": 1}

    async def get_open_interest(self, symbol: str) -> dict[str, Decimal | str | int]:
        if self.open_interest == NA:
            raise RuntimeError("open interest unavailable")
        return {
            "symbol": symbol,
            "open_interest": self.open_interest,
            "previous_open_interest": self.previous_open_interest,
        }


class SpyAlertAgent(AlertAgent):
    def __init__(self) -> None:
        super().__init__()
        self.calls: list[dict[str, Any]] = []

    async def send(self, alert: dict[str, Any] | None = None, **overrides: Any) -> Any:
        payload = dict(alert or {})
        payload.update(overrides)
        self.calls.append(payload)
        if payload.get("dry_run") is not True:
            raise AssertionError("scanner alerts must be dry-run by default")
        return await super().send(payload)


def _config(symbols: list[str], **overrides: object) -> ScannerRunConfig:
    data: dict[str, object] = {
        "symbols": symbols,
        "exchange": "binance",
        "interval": "15m",
        "candle_limit": 220,
        "dry_run_alerts": True,
        "account_equity": Decimal("10000"),
        "risk_per_trade_pct": Decimal("1"),
        "min_score_for_idea": Decimal("80"),
    }
    data.update(overrides)
    return ScannerRunConfig.model_validate(data)


def test_scanner_handles_one_valid_mocked_symbol() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.JOURNAL_ENTRY_CREATED
    assert ScannerPipelineStatus.IDEA_CREATED in symbol_result.status_history
    assert ScannerPipelineStatus.ALERT_DRY_RUN_CREATED in symbol_result.status_history
    assert symbol_result.trade_idea is not None
    assert symbol_result.trade_idea.setup_type == "liquidity_grab_pullback_challenge"
    assert symbol_result.trade_idea.quality_gate_result.passed is True
    assert symbol_result.strategy_name == "liquidity_grab_pullback"
    assert symbol_result.valid_strategy_modes == ("challenge", "swing", "scalp")
    assert result.trade_ideas_created == 1


def test_scanner_handles_no_setup() -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert symbol_result.trade_idea is None
    assert symbol_result.rejection_reason == "No valid Liquidity-Grab Pullback setup."
    assert symbol_result.rejection_stage == "strategy"


def test_verbose_config_defaults_to_false() -> None:
    assert _config(["BTCUSDT"]).verbose is False


def test_scanner_diagnostics_exist_for_no_setup_result() -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.candles_fetched == 220
    assert symbol_result.latest_close == Decimal("100")
    assert symbol_result.technical_score != NA
    assert symbol_result.derivatives_score != NA
    assert symbol_result.trend_context in ("bullish", "bearish", "neutral", NA)
    assert symbol_result.sweep_detected is False
    assert symbol_result.bos_detected is False
    assert symbol_result.choch_detected is False
    assert symbol_result.rejection_stage == "strategy"
    assert symbol_result.rejection_reasons == ("No valid Liquidity-Grab Pullback setup.",)
    assert "swing" in symbol_result.strategy_diagnostics
    assert symbol_result.volume_profile is not None
    assert symbol_result.volume_profile_source == VOLUME_PROFILE_SOURCE
    assert symbol_result.poc != NA
    assert symbol_result.value_area_high != NA
    assert symbol_result.value_area_low != NA
    assert symbol_result.strategy_diagnostics["challenge"]["volume_profile_source"] == VOLUME_PROFILE_SOURCE
    assert symbol_result.strategy_diagnostics["challenge"]["poc"] == symbol_result.poc
    assert (
        symbol_result.strategy_diagnostics["challenge"]["poc_diagnostics"]
        == "POC available from estimated candle volume profile."
    )


def test_scanner_continues_if_one_symbol_fails() -> None:
    client = FakeExchangeClient(
        {
            "FAILUSDT": _flat_candles(),
            "BTCUSDT": _strategy_pullback_candles(),
        },
        failing_symbols={"FAILUSDT"},
        failing_timeframes={"2d"},
    )

    result = run(ScannerRunner(exchange_client=client).run(_config(["FAILUSDT", "BTCUSDT"])))

    assert result.scanned_symbols == 2
    assert result.results[0].status == ScannerPipelineStatus.FAILED
    assert result.results[0].error_message == "mocked kline failure for FAILUSDT"
    assert result.results[1].status == ScannerPipelineStatus.JOURNAL_ENTRY_CREATED


def test_scanner_rejects_candidate_without_invalidation() -> None:
    client = FakeExchangeClient({"BTCUSDT": _bos_without_stop_candles()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"], enable_strategy_output=False)))

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.REJECTED_BY_TECHNICAL
    assert symbol_result.trade_idea is None
    assert "invalidation: N/A" in symbol_result.missing_data


def test_scanner_creates_dry_run_alert_only_when_idea_passes_gates() -> None:
    alert_agent = SpyAlertAgent()
    client = FakeExchangeClient(
        {
            "BTCUSDT": _strategy_pullback_candles(),
            "ETHUSDT": _flat_candles(),
        },
        failing_timeframes={"2d"},
    )

    result = run(ScannerRunner(exchange_client=client, alert_agent=alert_agent).run(_config(["BTCUSDT", "ETHUSDT"])))

    assert result.results[0].alert_result is not None
    assert result.results[0].alert_result.dry_run is True
    assert result.results[1].alert_result is None
    assert len(alert_agent.calls) == 1
    assert alert_agent.calls[0]["dry_run"] is True


def test_scanner_creates_journal_entry_only_when_idea_exists() -> None:
    client = FakeExchangeClient(
        {
            "BTCUSDT": _strategy_pullback_candles(),
            "ETHUSDT": _flat_candles(),
        },
        failing_timeframes={"2d"},
    )

    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT", "ETHUSDT"])))

    assert result.results[0].journal_entry is not None
    assert result.results[1].journal_entry is None
    assert result.journal_entries_created == 1


def test_dry_run_alert_does_not_call_telegram_live() -> None:
    alert_agent = SpyAlertAgent()
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"})

    result = run(ScannerRunner(exchange_client=client, alert_agent=alert_agent).run(_config(["BTCUSDT"])))

    assert result.results[0].alert_result is not None
    assert result.results[0].alert_result.status == "dry_run"
    assert alert_agent.calls[0]["dry_run"] is True


def test_missing_funding_marked_na() -> None:
    client = FakeExchangeClient({"BTCUSDT": _trend_candles_with_valid_setup()}, funding=NA)
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.funding_rate == NA
    assert symbol_result.funding_direction == NA
    assert symbol_result.funding_severity == NA
    assert "funding_rate: N/A" in symbol_result.missing_data
    assert symbol_result.derivatives_result is not None
    assert symbol_result.derivatives_result.funding.raw_funding_rate == NA


def test_missing_oi_marked_na() -> None:
    client = FakeExchangeClient({"BTCUSDT": _trend_candles_with_valid_setup()}, open_interest=NA)
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.open_interest == NA
    assert symbol_result.oi_direction == NA
    assert symbol_result.price_oi_relationship == NA
    assert "open_interest: N/A" in symbol_result.missing_data
    assert symbol_result.derivatives_result is not None
    assert symbol_result.derivatives_result.open_interest.current_open_interest == NA


def test_tests_use_mocked_exchange_client_without_live_api_calls() -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()})
    run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    assert client.requested_klines
    assert all(symbol == "BTCUSDT" for symbol, _interval in client.requested_klines)


def test_scanner_marks_volume_profile_na_when_execution_volume_missing() -> None:
    candles = _flat_candles()
    for candle in candles:
        candle.pop("volume")
    client = FakeExchangeClient({"BTCUSDT": candles})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.volume_profile is not None
    assert symbol_result.volume_profile.source == VOLUME_PROFILE_SOURCE
    assert symbol_result.poc == NA
    assert "volume: N/A" in symbol_result.missing_data
    assert symbol_result.volume_profile_warnings


def test_scanner_returns_strategy_results_output_and_diagnostics() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert "challenge" in symbol_result.strategy_results
    assert "Challenge Setup" in symbol_result.formatted_strategy_output
    assert "sweep_diagnostics" in symbol_result.strategy_diagnostics["challenge"]
    assert symbol_result.strategy_diagnostics["challenge"]["htf_timeframe"] == "2d"
    assert symbol_result.strategy_diagnostics["challenge"]["bias_timeframe"] == "12h"
    assert symbol_result.strategy_diagnostics["challenge"]["execution_timeframe"] == "15m"
    assert symbol_result.strategy_diagnostics["challenge"]["confirmation_timeframe"] == "5m"
    assert symbol_result.strategy_diagnostics["challenge"]["htf_2d_context_source"] == "synthetic_from_1d"
    assert symbol_result.strategy_diagnostics["challenge"]["candles_2d_count"] > 0
    assert symbol_result.strategy_diagnostics["challenge"]["candles_12h_count"] == 220
    assert symbol_result.strategy_diagnostics["challenge"]["candles_15m_count"] == 220
    assert symbol_result.strategy_diagnostics["challenge"]["candles_5m_count"] == 220
    assert symbol_result.strategy_diagnostics["challenge"]["execution_sweep_status"] == "passed"
    assert symbol_result.strategy_diagnostics["challenge"]["confirmation_structure_shift_status"] == "passed"


def test_challenge_invalid_output_remains_exact_message() -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    challenge_result = result.results[0].strategy_results["challenge"]
    assert challenge_result.formatted_output.challenge_setup == "No valid challenge setup."


def test_rejected_strategy_does_not_create_trade_idea_alert_or_journal() -> None:
    alert_agent = SpyAlertAgent()
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()})
    result = run(ScannerRunner(exchange_client=client, alert_agent=alert_agent).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert symbol_result.trade_idea is None
    assert symbol_result.alert_result is None
    assert symbol_result.journal_entry is None
    assert alert_agent.calls == []


def test_missing_strategy_context_is_marked_na() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"1d", "12h"})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert "candles_2d: N/A" in symbol_result.strategy_missing_data
    assert "candles_12h: N/A" in symbol_result.strategy_missing_data
    assert "cvd: N/A" in symbol_result.strategy_missing_data
    assert "liquidation_data: N/A" in symbol_result.strategy_missing_data
    assert "candles_2d: N/A" in symbol_result.missing_data


def test_scanner_does_not_request_binance_2d_and_uses_synthetic_2d() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    requested_intervals = [interval for _symbol, interval in client.requested_klines]
    diagnostics = result.results[0].strategy_diagnostics["challenge"]

    assert "2d" not in requested_intervals
    assert "1d" in requested_intervals
    assert diagnostics["htf_2d_context_source"] == "synthetic_from_1d"
    assert diagnostics["candles_2d_count"] == 110


def test_scanner_fetches_12h_15m_and_5m_for_strategy_context() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    requested_intervals = [interval for _symbol, interval in client.requested_klines]
    diagnostics = result.results[0].strategy_diagnostics["challenge"]

    assert "12h" in requested_intervals
    assert "15m" in requested_intervals
    assert "5m" in requested_intervals
    assert diagnostics["candles_12h_count"] == 220
    assert diagnostics["candles_15m_count"] == 220
    assert diagnostics["candles_5m_count"] == 220
    assert diagnostics["ltf_confirmation_timeframe"] == "5m"


def test_missing_5m_context_does_not_crash_if_15m_exists() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"5m"})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.status != ScannerPipelineStatus.FAILED
    assert "candles_5m: N/A" in symbol_result.strategy_missing_data
    assert symbol_result.strategy_diagnostics["challenge"]["candles_15m_count"] == 220
    assert symbol_result.strategy_diagnostics["challenge"]["candles_5m_count"] == 0
    assert symbol_result.strategy_diagnostics["challenge"]["confirmation_timeframe"] == NA
    assert symbol_result.strategy_diagnostics["challenge"]["first_failed_gate"] == "missing_confirmation_structure_shift"
    assert (
        symbol_result.strategy_diagnostics["challenge"]["confirmation_bos_choch_reason"]
        == "5m confirmation candles missing."
    )


def test_one_noncritical_timeframe_failure_does_not_crash_symbol_scan() -> None:
    client = FakeExchangeClient(
        {"BTCUSDT": _strategy_pullback_candles()},
        failing_timeframes={"2d", "4h"},
    )
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.status != ScannerPipelineStatus.FAILED
    assert "candles_4h: N/A" in symbol_result.strategy_missing_data
