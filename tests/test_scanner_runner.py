from __future__ import annotations

import asyncio
from decimal import Decimal
from typing import Any

import pytest

from app.agents.alert_agent import AlertAgent
from app.analytics.setup_quality import SetupQualityState
from app.analytics.target_intelligence import TargetFailureType, TargetIntelligenceResult, TargetQualityGrade
from app.analytics.volume_profile import VOLUME_PROFILE_SOURCE
from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display, display_fields
from app.pipeline import scanner_runner as scanner_runner_module
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
        long_short_ratio: Decimal | str = Decimal("1.10"),
        failing_symbols: set[str] | None = None,
        failing_timeframes: set[str] | None = None,
        delayed_methods: dict[str, float] | None = None,
    ) -> None:
        self.candles_by_symbol = candles_by_symbol
        self.funding = funding
        self.open_interest = open_interest
        self.previous_open_interest = previous_open_interest
        self.long_short_ratio = long_short_ratio
        self.failing_symbols = failing_symbols or set()
        self.failing_timeframes = failing_timeframes or set()
        self.delayed_methods = delayed_methods or {}
        self.requested_symbols: list[str] = []
        self.requested_klines: list[tuple[str, str]] = []
        self.requested_kline_limits: list[tuple[str, str, int]] = []

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int]]:
        await self._maybe_delay("get_klines")
        self.requested_symbols.append(symbol)
        self.requested_klines.append((symbol, interval))
        self.requested_kline_limits.append((symbol, interval, limit))
        if symbol in self.failing_symbols:
            raise RuntimeError(f"mocked kline failure for {symbol}")
        if interval in self.failing_timeframes:
            raise RuntimeError(f"mocked kline failure for {symbol} {interval}")
        return self.candles_by_symbol[symbol][-limit:]

    async def get_ticker(self, symbol: str) -> dict[str, Decimal | str | int]:
        await self._maybe_delay("get_ticker")
        close = self.candles_by_symbol.get(symbol, _flat_candles())[-1]["close"]
        return {
            "symbol": symbol,
            "last_price": close,
            "price_change_ratio_24h": Decimal("0.01"),
            "quote_volume_24h": Decimal("100000000"),
        }

    async def get_funding_rate(self, symbol: str) -> dict[str, Decimal | str | int]:
        await self._maybe_delay("get_funding_rate")
        if self.funding == NA:
            raise RuntimeError("funding unavailable")
        return {"symbol": symbol, "funding_rate": self.funding, "timestamp": 1}

    async def get_funding_rate_history(self, symbol: str) -> list[dict[str, Decimal | str | int]]:
        await self._maybe_delay("get_funding_rate_history")
        if self.funding == NA:
            raise RuntimeError("funding history unavailable")
        return [
            {"symbol": symbol, "funding_rate": Decimal("0.00005"), "timestamp": 0},
            {"symbol": symbol, "funding_rate": self.funding, "timestamp": 1},
        ]

    async def get_open_interest(self, symbol: str) -> dict[str, Decimal | str | int]:
        await self._maybe_delay("get_open_interest")
        if self.open_interest == NA:
            raise RuntimeError("open interest unavailable")
        return {
            "symbol": symbol,
            "open_interest": self.open_interest,
            "previous_open_interest": self.previous_open_interest,
        }

    async def get_open_interest_history(self, symbol: str) -> list[dict[str, Decimal | str | int]]:
        await self._maybe_delay("get_open_interest_history")
        if self.open_interest == NA:
            raise RuntimeError("open interest history unavailable")
        return [
            {"symbol": symbol, "open_interest": self.previous_open_interest, "timestamp": 0},
            {"symbol": symbol, "open_interest": self.open_interest, "timestamp": 1},
        ]

    async def get_long_short_ratio(self, symbol: str) -> Decimal | str:
        await self._maybe_delay("get_long_short_ratio")
        if self.long_short_ratio == NA:
            raise RuntimeError("long/short ratio unavailable")
        return self.long_short_ratio

    async def _maybe_delay(self, method_name: str) -> None:
        delay = self.delayed_methods.get(method_name)
        if delay is not None:
            await asyncio.sleep(delay)


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


def _clean_target_intelligence() -> TargetIntelligenceResult:
    return TargetIntelligenceResult(
        tp1_candidate=Decimal("112"),
        tp2_candidate=Decimal("125"),
        tp3_candidate=Decimal("140"),
        nearest_opposing_liquidity=Decimal("125"),
        target_distance=Decimal("28"),
        clean_path_distance=Decimal("28"),
        rr_to_tp1=Decimal("2.8"),
        rr_to_tp2=Decimal("3.5"),
        rr_to_tp3=Decimal("4.2"),
        target_quality_grade=TargetQualityGrade.A,
        target_failure_type=NA,
        target_confidence=88,
        next_target_condition=NA,
    )


@pytest.fixture(autouse=True)
def clean_target_intelligence_by_default(monkeypatch) -> None:
    monkeypatch.setattr(
        scanner_runner_module,
        "build_target_intelligence",
        lambda *args, **kwargs: _clean_target_intelligence(),
    )


def _scan_with_target_intelligence(monkeypatch, target_intelligence: TargetIntelligenceResult):
    monkeypatch.setattr(
        scanner_runner_module,
        "build_target_intelligence",
        lambda *args, **kwargs: target_intelligence,
    )
    alert_agent = SpyAlertAgent()
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"})
    result = run(ScannerRunner(exchange_client=client, alert_agent=alert_agent).run(_config(["BTCUSDT"])))
    return result, alert_agent


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
    assert symbol_result.trade_idea.setup_type == "liquidity_grab_pullback_swing"
    assert symbol_result.trade_idea.quality_gate_result.passed is True
    assert symbol_result.strategy_name == "liquidity_grab_pullback"
    assert symbol_result.valid_strategy_modes == ("swing", "scalp")
    assert symbol_result.target_intelligence is not None
    assert "target_intelligence" in symbol_result.strategy_diagnostics["swing"]
    assert symbol_result.setup_quality.quality_state == SetupQualityState.HIGH_QUALITY_TRADE
    assert symbol_result.setup_quality.quality_score >= 85
    assert symbol_result.setup_quality.action_label == "Trade candidate"
    assert result.trade_ideas_created == 1


def test_target_quality_reject_blocks_trade_idea_alert_and_journal(monkeypatch) -> None:
    target_intelligence = TargetIntelligenceResult(
        target_quality_grade=TargetQualityGrade.REJECT,
        target_failure_type=TargetFailureType.RR_BELOW_MINIMUM,
        rr_compression_reason="Clean target path is too compressed for the required RR.",
        next_target_condition="Wait until TP2 expands beyond opposing structure.",
    )

    result, alert_agent = _scan_with_target_intelligence(monkeypatch, target_intelligence)

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert symbol_result.status_history == (ScannerPipelineStatus.SCANNED_NO_SETUP,)
    assert symbol_result.rejection_stage == "target_integrity"
    assert symbol_result.trade_idea is None
    assert symbol_result.alert_result is None
    assert symbol_result.journal_entry is None
    assert result.trade_ideas_created == 0
    assert result.dry_run_alerts_created == 0
    assert result.journal_entries_created == 0
    assert alert_agent.calls == []

    display = build_symbol_display(symbol_result)
    fields = display_fields(symbol_result)
    assert display.display_bucket == "near_miss"
    assert display.failed_stage == "target_integrity"
    assert display.action_label == "Wait for target expansion"
    assert fields["target_quality_grade"] == "Reject"
    assert fields["next_trigger_needed"] == "Wait until TP2 expands beyond opposing structure."


def test_blocking_target_failure_type_blocks_even_without_reject_grade(monkeypatch) -> None:
    target_intelligence = TargetIntelligenceResult(
        target_quality_grade=TargetQualityGrade.B,
        target_failure_type=TargetFailureType.TARGET_INSIDE_CHOP,
        rr_compression_reason="TP1 sits inside chop.",
        next_target_condition="Wait for target expansion above the chop range.",
    )

    result, alert_agent = _scan_with_target_intelligence(monkeypatch, target_intelligence)

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert symbol_result.rejection_stage == "target_integrity"
    assert symbol_result.trade_idea is None
    assert symbol_result.alert_result is None
    assert symbol_result.journal_entry is None
    assert alert_agent.calls == []
    assert build_symbol_display(symbol_result).action_label == "Wait for target expansion"


def test_invalid_tp_sequence_blocks_before_alert(monkeypatch) -> None:
    monkeypatch.setattr(scanner_runner_module, "_tp_sequence_valid", lambda **kwargs: False)

    result, alert_agent = _scan_with_target_intelligence(
        monkeypatch,
        TargetIntelligenceResult(
            target_quality_grade=TargetQualityGrade.A,
            target_failure_type=NA,
            rr_compression_reason=NA,
            next_target_condition=NA,
        ),
    )

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert symbol_result.rejection_stage == "target_integrity"
    assert symbol_result.rejection_reason == scanner_runner_module.INVALID_TP_SEQUENCE_WARNING
    assert symbol_result.trade_idea is None
    assert symbol_result.alert_result is None
    assert symbol_result.journal_entry is None
    assert alert_agent.calls == []
    assert build_symbol_display(symbol_result).short_reason == scanner_runner_module.INVALID_TP_SEQUENCE_WARNING


def test_target_integrity_block_does_not_synthesize_passed_gates() -> None:
    strategy_execution = scanner_runner_module._StrategyExecution(
        strategy_diagnostics={"swing": {}},
        rejected_strategy_modes=("swing",),
    )

    blocked = scanner_runner_module._strategy_execution_with_target_integrity_block(
        strategy_execution,
        reason="Clean target path is too compressed.",
        warning="Clean target path is too compressed.",
    )

    diagnostics = blocked.strategy_diagnostics["swing"]
    assert diagnostics["target_integrity_status"] == "blocked"
    assert diagnostics["gates_passed"] == ()
    assert diagnostics["gates_failed"] == ("target_integrity",)


def test_selected_setup_features_repair_stale_generic_technical_score_for_scoring() -> None:
    execution = scanner_runner_module._StrategyExecution(
        valid_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "selected_zone_type": "OB",
                "rr_to_tp2": Decimal("2.6"),
                "target_integrity_status": "passed",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "ob_fvg", "rr", "target_integrity"),
                "gates_failed": (),
            }
        },
    )
    stale_technical = type("Technical", (), {"structure_score": 25})()

    score = scanner_runner_module._technical_score_for_scoring(stale_technical, execution)

    assert score >= 50
    assert score > stale_technical.structure_score


def test_tp_sequence_uses_absolute_reward_distance() -> None:
    assert scanner_runner_module._tp_sequence_valid(
        direction="long",
        entry=Decimal("100"),
        take_profit_targets=(Decimal("105"), Decimal("110"), Decimal("120")),
    )
    assert scanner_runner_module._tp_sequence_valid(
        direction="short",
        entry=Decimal("100"),
        take_profit_targets=(Decimal("95"), Decimal("90"), Decimal("80")),
    )
    assert not scanner_runner_module._tp_sequence_valid(
        direction="short",
        entry=Decimal("100"),
        take_profit_targets=(Decimal("90"), Decimal("95"), Decimal("80")),
    )
    assert not scanner_runner_module._tp_sequence_valid(
        direction="long",
        entry=Decimal("100"),
        take_profit_targets=(Decimal("100"), Decimal("110"), Decimal("120")),
    )


def test_future_target_failure_type_literals_are_parseable() -> None:
    compressed = TargetIntelligenceResult.model_validate(
        {"target_quality_grade": "B", "target_failure_type": "RR_COMPRESSED"}
    )
    no_clean_path = TargetIntelligenceResult.model_validate(
        {"target_quality_grade": "C", "target_failure_type": "NO_CLEAN_TARGET_PATH"}
    )

    assert compressed.target_failure_type == "RR_COMPRESSED"
    assert no_clean_path.target_failure_type == "NO_CLEAN_TARGET_PATH"


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
    assert symbol_result.derivatives_enrichment is not None
    assert symbol_result.funding_status == "normal"
    assert symbol_result.open_interest_change_pct == Decimal("5.00000000")
    assert symbol_result.long_short_ratio == Decimal("1.10000000")
    assert symbol_result.crowding_risk in ("low", "medium")
    assert "funding_rate" in symbol_result.derivatives_enrichment.model_dump()


def test_scanner_derivatives_missing_data_does_not_reject_by_itself() -> None:
    client = FakeExchangeClient(
        {"BTCUSDT": _strategy_pullback_candles()},
        funding=NA,
        open_interest=NA,
        long_short_ratio=NA,
        failing_timeframes={"2d"},
    )
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.status != ScannerPipelineStatus.REJECTED_BY_DERIVATIVES
    assert symbol_result.derivatives_enrichment is not None
    assert symbol_result.derivatives_enrichment.funding_status == NA
    assert "funding_rate: N/A" in symbol_result.derivatives_missing_data
    assert symbol_result.setup_quality.quality_state != SetupQualityState.HIGH_QUALITY_TRADE
    assert "mixed derivatives" in symbol_result.setup_quality.weakest_factors


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
    assert result.results[0].status == ScannerPipelineStatus.SCAN_ERROR
    assert result.results[0].error_message == "mocked kline failure for FAILUSDT"
    assert result.results[1].status == ScannerPipelineStatus.JOURNAL_ENTRY_CREATED


def test_request_timeout_marks_symbol_scan_error() -> None:
    client = FakeExchangeClient(
        {"BTCUSDT": _flat_candles()},
        delayed_methods={"get_klines": 0.05},
    )

    result = run(
        ScannerRunner(exchange_client=client).run(
            _config(["BTCUSDT"], request_timeout_sec=0.01, symbol_timeout_sec=1)
        )
    )

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.SCAN_ERROR
    assert "request timed out after 0.01 seconds" in str(symbol_result.error_message)
    assert symbol_result.runtime_seconds is not None
    assert symbol_result.timed_out is True
    assert symbol_result.timeout_status == "request_timeout"
    assert result.runtime_stats.timeout_count == 1


def test_symbol_timeout_marks_symbol_scan_error_and_continues() -> None:
    class OneSlowSymbolClient(FakeExchangeClient):
        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int]]:
            if symbol == "SLOWUSDT":
                await asyncio.sleep(0.3)
            return await super().get_klines(symbol, interval, limit)

    client = OneSlowSymbolClient(
        {
            "SLOWUSDT": _flat_candles(),
            "BTCUSDT": _strategy_pullback_candles(),
        },
        failing_timeframes={"2d"},
    )

    result = run(
        ScannerRunner(exchange_client=client).run(
            _config(["SLOWUSDT", "BTCUSDT"], request_timeout_sec=1, symbol_timeout_sec=0.2)
        )
    )

    assert result.scanned_symbols == 2
    assert result.results[0].status == ScannerPipelineStatus.SCAN_ERROR
    assert "symbol timeout exceeded after 0.2 seconds" in str(result.results[0].error_message)
    assert result.results[0].runtime_seconds is not None
    assert result.results[0].timed_out is True
    assert result.results[0].timeout_status == "symbol_timeout"
    assert result.results[1].status == ScannerPipelineStatus.JOURNAL_ENTRY_CREATED
    assert result.results[1].runtime_seconds is not None
    assert result.results[1].timeout_status == "none"
    assert result.runtime_stats.completed_symbols == 1
    assert result.runtime_stats.errored_symbols == 1
    assert result.runtime_stats.timeout_count == 1
    assert result.runtime_stats.slowest_symbol in {"SLOWUSDT", "BTCUSDT"}


def test_scan_timeout_stops_gracefully_with_partial_results() -> None:
    class AttemptRecordingClient(FakeExchangeClient):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.started_klines: list[tuple[str, str]] = []

        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int]]:
            self.started_klines.append((symbol, interval))
            return await super().get_klines(symbol, interval, limit)

    client = AttemptRecordingClient(
        {
            "BTCUSDT": _flat_candles(),
            "ETHUSDT": _flat_candles(),
        },
        delayed_methods={"get_klines": 0.05},
    )

    result = run(
        ScannerRunner(exchange_client=client).run(
            _config(["BTCUSDT", "ETHUSDT"], request_timeout_sec=1, symbol_timeout_sec=1, scan_timeout_sec=0.01)
        )
    )

    assert result.scanned_symbols == 1
    assert result.results[0].status == ScannerPipelineStatus.SCAN_ERROR
    assert "full scan timeout exceeded after 0.01 seconds" in str(result.results[0].error_message)
    assert result.results[0].timed_out is True
    assert result.results[0].timeout_status == "global_timeout"
    assert result.runtime_stats.global_timeout_hit is True
    assert result.runtime_stats.timeout_count == 1
    assert result.runtime_stats.skipped_symbols == 1
    assert any(symbol == "BTCUSDT" for symbol, _interval in client.started_klines)
    assert all(symbol != "ETHUSDT" for symbol, _interval in client.started_klines)


def test_one_slow_symbol_does_not_stop_full_scan_runtime_stats() -> None:
    class OneSlowSymbolClient(FakeExchangeClient):
        async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, Decimal | int]]:
            if symbol == "SLOWUSDT":
                await asyncio.sleep(0.3)
            return await super().get_klines(symbol, interval, limit)

    client = OneSlowSymbolClient(
        {
            "SLOWUSDT": _flat_candles(),
            "ETHUSDT": _flat_candles(),
            "BTCUSDT": _strategy_pullback_candles(),
        },
        failing_timeframes={"2d"},
    )

    result = run(
        ScannerRunner(exchange_client=client).run(
            _config(["SLOWUSDT", "ETHUSDT", "BTCUSDT"], request_timeout_sec=1, symbol_timeout_sec=0.2)
        )
    )

    assert [item.symbol for item in result.results] == ["SLOWUSDT", "ETHUSDT", "BTCUSDT"]
    assert result.results[0].timeout_status == "symbol_timeout"
    assert result.results[1].status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert result.results[2].status == ScannerPipelineStatus.JOURNAL_ENTRY_CREATED
    assert result.runtime_stats.completed_symbols == 2
    assert result.runtime_stats.errored_symbols == 1
    assert result.runtime_stats.skipped_symbols == 0


def test_optional_endpoint_timeout_is_marked_na_without_blocking_scan() -> None:
    client = FakeExchangeClient(
        {"BTCUSDT": _flat_candles()},
        delayed_methods={"get_open_interest_history": 0.05},
    )

    result = run(
        ScannerRunner(exchange_client=client).run(
            _config(["BTCUSDT"], request_timeout_sec=0.01, symbol_timeout_sec=1)
        )
    )

    symbol_result = result.results[0]
    assert symbol_result.status != ScannerPipelineStatus.SCAN_ERROR
    assert "open_interest_history: N/A" in symbol_result.missing_data
    assert any("open_interest_history unavailable" in warning for warning in symbol_result.derivatives_warnings)


def test_progress_output_callback_receives_symbol_stages() -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()})
    messages: list[str] = []

    run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"]), progress=messages.append))

    assert "Starting BTCUSDT..." in messages
    assert "Fetching HTF 2d..." in messages
    assert "Fetching 12h bias..." in messages
    assert "Fetching 15m execution..." in messages
    assert "Fetching 5m confirmation..." in messages
    assert "Fetching derivatives..." in messages
    assert "Scoring..." in messages
    assert any(message.startswith("Done BTCUSDT in ") for message in messages)


def test_scanner_summary_includes_cache_counts() -> None:
    client = FakeExchangeClient({"BTCUSDT": _flat_candles()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    assert result.cache_stats["enabled"] is True
    assert result.cache_stats["misses"] > 0
    assert result.cache_stats["hits"] >= 0


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


def test_replay_candles_1000_clamps_execution_timeframes_only() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"], replay_candles=1000)))

    requested_limits = {(interval, limit) for _symbol, interval, limit in client.requested_kline_limits}
    diagnostics = result.results[0].strategy_diagnostics["challenge"]

    assert ("15m", 500) in requested_limits
    assert ("5m", 500) in requested_limits
    assert ("12h", 220) in requested_limits
    assert ("1d", 440) in requested_limits
    assert ("1d", 2000) not in requested_limits
    assert any("replay_candles limit clamped from 1000 to 500" in warning for warning in diagnostics["timeframe_limit_warnings"])
    assert diagnostics["htf_2d_context_source"] == "synthetic_from_1d"
    assert diagnostics["candles_2d_count"] == 110


def test_scanner_clamps_binance_kline_limits_and_reports_diagnostic_warning() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"})
    result = run(
        ScannerRunner(exchange_client=client).run(
            _config(["BTCUSDT"], candle_limit=1000, replay_candles=2000)
        )
    )

    diagnostics = result.results[0].strategy_diagnostics["challenge"]

    assert client.requested_kline_limits
    assert all(limit <= 1500 for _symbol, _interval, limit in client.requested_kline_limits)
    assert ("BTCUSDT", "1d", 1500) in client.requested_kline_limits
    assert ("BTCUSDT", "5m", 500) in client.requested_kline_limits
    assert any("1d source for synthetic 2D candles limit clamped from 2000 to 1500" in warning for warning in diagnostics["timeframe_limit_warnings"])
    assert any("replay_candles limit clamped from 2000 to 500" in warning for warning in diagnostics["timeframe_limit_warnings"])


def test_normal_scan_still_uses_configured_candle_limit_without_replay() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    diagnostics = result.results[0].strategy_diagnostics["challenge"]

    assert ("BTCUSDT", "15m", 220) in client.requested_kline_limits
    assert ("BTCUSDT", "12h", 220) in client.requested_kline_limits
    assert ("BTCUSDT", "5m", 220) in client.requested_kline_limits
    assert diagnostics["candles_15m_count"] == 220
    assert diagnostics["candles_12h_count"] == 220
    assert diagnostics["candles_5m_count"] == 220


def test_fast_mode_does_not_weaken_strategy_gates_for_no_setup() -> None:
    normal_client = FakeExchangeClient({"BTCUSDT": _flat_candles()})
    fast_client = FakeExchangeClient({"BTCUSDT": _flat_candles()})

    normal = run(ScannerRunner(exchange_client=normal_client).run(_config(["BTCUSDT"]))).results[0]
    fast = run(ScannerRunner(exchange_client=fast_client).run(_config(["BTCUSDT"], fast_mode=True))).results[0]

    assert normal.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert fast.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert normal.valid_strategy_modes == ()
    assert fast.valid_strategy_modes == ()
    assert normal.strategy_diagnostics["challenge"]["first_failed_gate"] == "missing_confirmed_sweep"
    assert fast.strategy_diagnostics["challenge"]["first_failed_gate"] == "missing_confirmed_sweep"


def test_fast_mode_skips_optional_derivatives_history_that_could_block() -> None:
    class BlockingOptionalHistoryClient(FakeExchangeClient):
        async def get_funding_rate_history(self, symbol: str) -> list[dict[str, Decimal | str | int]]:
            raise AssertionError("fast mode should skip funding history")

        async def get_open_interest_history(self, symbol: str) -> list[dict[str, Decimal | str | int]]:
            raise AssertionError("fast mode should skip open interest history")

        async def get_long_short_ratio(self, symbol: str) -> Decimal | str:
            raise AssertionError("fast mode should skip long/short ratio")

    client = BlockingOptionalHistoryClient({"BTCUSDT": _flat_candles()})

    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"], fast_mode=True)))

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert "funding_history: N/A" in symbol_result.missing_data
    assert "open_interest_history: N/A" in symbol_result.missing_data
    assert "long_short_ratio: N/A" in symbol_result.missing_data
    assert any("funding_history skipped in fast mode." in warning for warning in symbol_result.derivatives_warnings)


def test_fast_mode_caps_remaining_optional_derivatives_timeout() -> None:
    client = FakeExchangeClient(
        {"BTCUSDT": _flat_candles()},
        delayed_methods={"get_funding_rate": 1.0},
    )

    result = run(
        ScannerRunner(exchange_client=client).run(
            _config(["BTCUSDT"], fast_mode=True, request_timeout_sec=5, symbol_timeout_sec=2)
        )
    )

    symbol_result = result.results[0]
    assert symbol_result.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert symbol_result.timeout_status == "none"
    assert "funding_rate: N/A" in symbol_result.missing_data
    assert any("funding_rate unavailable from public endpoint" in warning for warning in symbol_result.derivatives_warnings)


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
    assert symbol_result.strategy_diagnostics["challenge"]["first_failed_gate"] == "missing_confirmation_candles"
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
