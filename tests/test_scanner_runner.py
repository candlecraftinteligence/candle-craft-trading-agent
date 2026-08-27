from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import pytest

from app.agents.alert_agent import AlertAgent
from app.analytics.setup_quality import SetupQualityState
from app.analytics.target_intelligence import TargetFailureType, TargetIntelligenceResult, TargetQualityGrade
from app.analytics.volume_profile import VOLUME_PROFILE_SOURCE
from app.context import (
    BtcDominanceContextService,
    BtcDominanceObservation,
    ContextStatus,
)
from app.core.confirmed_data_health import confirmed_data_health_for_symbol
from app.core.process_memory import ProcessMemoryReading
from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display, display_fields
from app.pipeline import scanner_runner as scanner_runner_module
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunner

_INTERVAL_MS = {
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "1h": 60 * 60_000,
    "2h": 2 * 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "12h": 12 * 60 * 60_000,
    "1d": 24 * 60 * 60_000,
    "2d": 2 * 24 * 60 * 60_000,
}


def _time_aligned_candles(
    candles: list[dict[str, Decimal | int]],
    interval: str,
) -> list[dict[str, Decimal | int]]:
    duration = _INTERVAL_MS[interval.lower()]
    pattern_anchor = max(0, len(candles) - 6)
    start = pattern_anchor * (_INTERVAL_MS["15m"] - duration) if interval.lower() == "5m" else 0
    return [
        {**candle, "timestamp": start + (int(candle["timestamp"]) * duration)}
        for candle in candles
    ]


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
        return _time_aligned_candles(self.candles_by_symbol[symbol][-limit:], interval)

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


class FakeBtcDominanceProvider:
    source = "fake:btc_d"

    def __init__(self, observed_at: datetime, *, error: Exception | None = None) -> None:
        self.observed_at = observed_at
        self.error = error
        self.calls = 0

    async def get_snapshot(self) -> BtcDominanceObservation:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return BtcDominanceObservation(
            btc_dominance_pct=Decimal("57.25"),
            observed_at=self.observed_at,
            source=self.source,
        )


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


def test_scanner_run_config_defaults_to_closed_m15_confirmation() -> None:
    assert _config(["BTCUSDT"]).confirmation_timeframe == "15m"


def test_open_m15_candle_cannot_enter_scanner_confirmation_input() -> None:
    candles = [
        {
            "timestamp": index * _INTERVAL_MS["15m"],
            "open": Decimal("100"),
            "high": Decimal("101"),
            "low": Decimal("99"),
            "close": Decimal("100") if index < 2 else Decimal("250"),
            "volume": Decimal("100"),
        }
        for index in range(3)
    ]
    config = _config(
        ["BTCUSDT"],
        decision_timestamp=datetime.fromtimestamp(
            (2 * _INTERVAL_MS["15m"] + 7 * 60_000) / 1000,
            tz=timezone.utc,
        ),
    )

    confirmation_candles = ScannerRunner()._closed_candles_for_analysis(
        candles,
        symbol="BTCUSDT",
        timeframe=config.confirmation_timeframe,
        config=config,
        minimum_closed_history=1,
    )

    assert config.confirmation_timeframe == "15m"
    assert len(confirmation_candles) == 2
    assert confirmation_candles[-1]["close"] == Decimal("100")
    assert all(candle["close"] != Decimal("250") for candle in confirmation_candles)

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


def test_target_inside_chop_soft_warning_does_not_block_trade_map(monkeypatch) -> None:
    target_intelligence = TargetIntelligenceResult(
        target_quality_grade=TargetQualityGrade.B,
        target_failure_type=TargetFailureType.TARGET_INSIDE_CHOP,
        rr_compression_reason="TP1 sits inside chop.",
        next_target_condition="Wait for target expansion above the chop range.",
    )

    result, alert_agent = _scan_with_target_intelligence(monkeypatch, target_intelligence)

    symbol_result = result.results[0]
    diagnostics = symbol_result.strategy_diagnostics["swing"]
    assert symbol_result.status == ScannerPipelineStatus.JOURNAL_ENTRY_CREATED
    assert symbol_result.trade_idea is not None
    assert symbol_result.alert_result is not None
    assert symbol_result.journal_entry is not None
    assert alert_agent.calls != []
    assert diagnostics["target_integrity_status"] == "warning"
    assert diagnostics["target_failure"] == TargetFailureType.TARGET_INSIDE_CHOP.value
    assert diagnostics["target_failure_severity"] == "soft_target_warning"
    assert diagnostics["target_warning_reason"] == "TP1 sits inside chop."
    assert "target_integrity" in diagnostics["gates_passed"]
    assert "target_integrity" not in diagnostics.get("gates_failed", ())


def test_opposing_structure_target_failure_remains_blocking(monkeypatch) -> None:
    target_intelligence = TargetIntelligenceResult(
        target_quality_grade=TargetQualityGrade.B,
        target_failure_type=TargetFailureType.OPPOSING_STRUCTURE_BLOCK,
        rr_compression_reason="Opposing structure blocks the clean path before minimum RR.",
        next_target_condition="Wait for target expansion above opposing structure.",
    )

    result, alert_agent = _scan_with_target_intelligence(monkeypatch, target_intelligence)

    symbol_result = result.results[0]
    diagnostics = symbol_result.strategy_diagnostics["swing"]
    assert symbol_result.status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert symbol_result.rejection_stage == "target_integrity"
    assert symbol_result.trade_idea is None
    assert symbol_result.alert_result is None
    assert symbol_result.journal_entry is None
    assert alert_agent.calls == []
    assert diagnostics["target_failure"] == TargetFailureType.OPPOSING_STRUCTURE_BLOCK.value
    assert diagnostics["target_failure_severity"] == "fatal_target_failure"
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


def test_weak_technical_score_has_distinct_invalid_rejection_stage() -> None:
    violation = type("Violation", (), {"code": "weak_technical_score"})()
    hard_filter = type("HardFilter", (), {"violations": (violation,)})()
    score_result = type("ScoreResult", (), {"hard_filter_result": hard_filter})()

    assert scanner_runner_module._scoring_rejection_stage(score_result) == "technical_invalid"


def test_tp_sequence_enforces_directional_reward_order() -> None:
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


def test_tp_sequence_rejects_target_inside_full_entry_zone() -> None:
    assert not scanner_runner_module._tp_sequence_valid(
        direction="long",
        entry=Decimal("100"),
        entry_low=Decimal("100"),
        entry_high=Decimal("102"),
        stop_loss=Decimal("95"),
        take_profit_targets=(Decimal("101"), Decimal("115"), Decimal("120")),
    )
    assert not scanner_runner_module._tp_sequence_valid(
        direction="short",
        entry=Decimal("100"),
        entry_low=Decimal("98"),
        entry_high=Decimal("100"),
        stop_loss=Decimal("105"),
        take_profit_targets=(Decimal("99"), Decimal("85"), Decimal("80")),
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
    assert symbol_result.technical_status == "VALID"
    assert symbol_result.technical_score != NA
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
    assert result.results[0].rejection_stage == "scanner"
    assert result.results[0].technical_status == NA
    assert result.results[0].error_message == "mocked kline failure for FAILUSDT"
    assert result.results[1].status == ScannerPipelineStatus.JOURNAL_ENTRY_CREATED


def test_scanner_runtime_records_verified_process_memory() -> None:
    readings = iter(
        (
            ProcessMemoryReading(rss_bytes=100_000_000, source="test:rss"),
            ProcessMemoryReading(rss_bytes=125_000_000, source="test:rss"),
            ProcessMemoryReading(rss_bytes=110_000_000, source="test:rss"),
        )
    )
    client = FakeExchangeClient(
        {"BTCUSDT": _flat_candles()},
        failing_timeframes={"2d"},
    )

    result = run(
        ScannerRunner(
            exchange_client=client,
            process_memory_sampler=lambda: next(readings),
        ).run(_config(["BTCUSDT"]))
    )

    assert result.runtime_stats.process_memory.model_dump() == {
        "measurement_status": "Verified",
        "source": "test:rss",
        "rss_start_bytes": 100_000_000,
        "rss_end_bytes": 110_000_000,
        "rss_observed_peak_bytes": 125_000_000,
        "rss_delta_bytes": 10_000_000,
        "samples_attempted": 3,
        "samples_succeeded": 3,
        "samples_failed": 0,
        "failure_codes": (),
    }


def test_scanner_memory_sampling_failure_is_unverified_and_non_fatal() -> None:
    readings: list[ProcessMemoryReading | Exception] = [
        ProcessMemoryReading(rss_bytes=100_000_000, source="test:rss"),
        OSError("sampler unavailable"),
        ProcessMemoryReading(rss_bytes=105_000_000, source="test:rss"),
    ]

    def sample_memory() -> ProcessMemoryReading:
        reading = readings.pop(0)
        if isinstance(reading, Exception):
            raise reading
        return reading

    client = FakeExchangeClient(
        {"BTCUSDT": _flat_candles()},
        failing_timeframes={"2d"},
    )
    result = run(
        ScannerRunner(
            exchange_client=client,
            process_memory_sampler=sample_memory,
        ).run(_config(["BTCUSDT"]))
    )

    memory = result.runtime_stats.process_memory
    assert result.results[0].status == ScannerPipelineStatus.SCANNED_NO_SETUP
    assert memory.measurement_status == "Unverified"
    assert memory.rss_start_bytes == 100_000_000
    assert memory.rss_end_bytes == 105_000_000
    assert memory.rss_observed_peak_bytes == 105_000_000
    assert memory.rss_delta_bytes == 5_000_000
    assert memory.samples_attempted == 3
    assert memory.samples_succeeded == 2
    assert memory.samples_failed == 1
    assert memory.failure_codes == ("OSError",)



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
    assert len(result.results) == 2
    assert result.results[0].status == ScannerPipelineStatus.SCAN_ERROR
    assert "full scan timeout exceeded after 0.01 seconds" in str(result.results[0].error_message)
    assert result.results[0].timed_out is True
    assert result.results[0].timeout_status == "global_timeout"
    assert result.results[1].symbol == "ETHUSDT"
    assert result.results[1].status == ScannerPipelineStatus.NOT_RUN
    assert result.results[1].iteration_outcome == "not_run"
    assert result.results[1].not_run_reason == "global_timeout_not_run"
    assert result.runtime_stats.global_timeout_hit is True
    assert result.runtime_stats.timeout_count == 1
    assert result.runtime_stats.skipped_symbols == 1
    assert result.runtime_stats.outcome_counts == {
        "evaluated": 0,
        "rejected": 0,
        "errored": 0,
        "timed_out": 1,
        "not_run": 1,
    }
    assert any(symbol == "BTCUSDT" for symbol, _interval in client.started_klines)
    assert all(symbol != "ETHUSDT" for symbol, _interval in client.started_klines)


def test_owned_http_client_closes_when_market_regime_fetch_fails(monkeypatch) -> None:
    class OwnedFailingClient:
        def __init__(self) -> None:
            self.closed = False

        async def get_klines(self, symbol: str, interval: str, limit: int):
            raise OSError("temporary market-regime transport failure")

        async def aclose(self) -> None:
            self.closed = True

    client = OwnedFailingClient()
    monkeypatch.setattr(
        scanner_runner_module,
        "BinanceFuturesClient",
        lambda *args, **kwargs: client,
    )

    result = run(ScannerRunner().run(_config(["BTCUSDT"], market_regime_enabled=True)))

    assert result.results[0].status == ScannerPipelineStatus.SCAN_ERROR
    assert "market-regime transport" in str(result.results[0].error_message)
    assert client.closed is True


def test_owned_http_client_closes_and_cancellation_propagates(monkeypatch) -> None:
    class OwnedCancelledClient:
        def __init__(self) -> None:
            self.closed = False

        async def get_klines(self, symbol: str, interval: str, limit: int):
            raise asyncio.CancelledError

        async def aclose(self) -> None:
            self.closed = True

    client = OwnedCancelledClient()
    monkeypatch.setattr(
        scanner_runner_module,
        "BinanceFuturesClient",
        lambda *args, **kwargs: client,
    )

    with pytest.raises(asyncio.CancelledError):
        run(ScannerRunner().run(_config(["BTCUSDT"], market_regime_enabled=True)))

    assert client.closed is True


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
    assert "Fetching 15m confirmation..." not in messages
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
    assert symbol_result.strategy_diagnostics["challenge"]["confirmation_timeframe"] == "15m"
    assert symbol_result.strategy_diagnostics["challenge"]["htf_2d_context_source"] == "synthetic_from_1d"
    assert symbol_result.strategy_diagnostics["challenge"]["candles_2d_count"] > 0
    assert symbol_result.strategy_diagnostics["challenge"]["candles_12h_count"] == 220
    assert symbol_result.strategy_diagnostics["challenge"]["candles_15m_count"] == 220
    assert symbol_result.strategy_diagnostics["challenge"]["candles_5m_count"] == 0
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
    assert ("5m", 500) not in requested_limits
    assert ("12h", 220) in requested_limits
    assert ("1d", 440) in requested_limits
    assert ("1d", 2000) not in requested_limits
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
    assert ("BTCUSDT", "15m", 500) in client.requested_kline_limits
    assert ("BTCUSDT", "5m", 500) not in client.requested_kline_limits
    assert any("1d source for synthetic 2D candles limit clamped from 2000 to 1500" in warning for warning in diagnostics["timeframe_limit_warnings"])


def test_normal_scan_still_uses_configured_candle_limit_without_replay() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    diagnostics = result.results[0].strategy_diagnostics["challenge"]

    assert ("BTCUSDT", "15m", 220) in client.requested_kline_limits
    assert ("BTCUSDT", "12h", 220) in client.requested_kline_limits
    assert ("BTCUSDT", "5m", 220) not in client.requested_kline_limits
    assert diagnostics["candles_15m_count"] == 220
    assert diagnostics["candles_12h_count"] == 220
    assert diagnostics["candles_5m_count"] == 0


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


def test_default_scanner_fetches_m15_confirmation_without_requesting_m5() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    requested_intervals = [interval for _symbol, interval in client.requested_klines]
    diagnostics = result.results[0].strategy_diagnostics["challenge"]

    assert "12h" in requested_intervals
    assert "15m" in requested_intervals
    assert "5m" not in requested_intervals
    assert diagnostics["candles_12h_count"] == 220
    assert diagnostics["candles_15m_count"] == 220
    assert diagnostics["candles_5m_count"] == 0
    assert diagnostics["ltf_confirmation_timeframe"] == "15m"


def test_explicit_m5_research_override_remains_available_when_m5_fetch_fails() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"5m"})
    result = run(
        ScannerRunner(exchange_client=client).run(
            _config(["BTCUSDT"], confirmation_timeframe="5m")
        )
    )

    symbol_result = result.results[0]
    assert symbol_result.status != ScannerPipelineStatus.FAILED
    assert "candles_5m: N/A" in symbol_result.strategy_missing_data
    assert symbol_result.strategy_diagnostics["challenge"]["candles_15m_count"] == 220
    assert symbol_result.strategy_diagnostics["challenge"]["candles_5m_count"] == 0
    assert symbol_result.strategy_diagnostics["challenge"]["confirmation_timeframe"] == "5m"
    assert symbol_result.strategy_diagnostics["challenge"]["first_failed_gate"] == "missing_confirmation_candles"


def test_one_noncritical_timeframe_failure_does_not_crash_symbol_scan() -> None:
    client = FakeExchangeClient(
        {"BTCUSDT": _strategy_pullback_candles()},
        failing_timeframes={"2d", "4h"},
    )
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"])))

    symbol_result = result.results[0]
    assert symbol_result.status != ScannerPipelineStatus.FAILED
    assert "candles_4h: N/A" in symbol_result.strategy_missing_data


def test_strategy_valid_plus_199_technical_bars_is_explicitly_insufficient() -> None:
    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"})
    result = run(ScannerRunner(exchange_client=client).run(_config(["BTCUSDT"], candle_limit=199)))
    symbol_result = result.results[0]

    assert symbol_result.status == ScannerPipelineStatus.REJECTED_BY_TECHNICAL
    assert symbol_result.technical_status == "INSUFFICIENT_DATA"
    assert symbol_result.technical_score == NA
    assert symbol_result.technical_required_bars == 200
    assert symbol_result.technical_available_bars == 199
    assert symbol_result.rejection_stage == "technical_insufficient_data"
    assert symbol_result.valid_strategy_modes == ("swing", "scalp")
    assert symbol_result.setup_quality.quality_state == SetupQualityState.DATA_ISSUE
    assert any("status=INSUFFICIENT_DATA" in item for item in symbol_result.missing_data)
    assert symbol_result.trade_idea is None
    persisted_payload = symbol_result.model_dump(mode="json")
    assert persisted_payload["technical_score"] == NA
    assert persisted_payload["technical_result"]["data_quality"] == "insufficient_data"


@pytest.mark.parametrize("strategy_mode", ("scalp", "swing"))
def test_new_listing_short_history_is_explicit_in_each_mode(strategy_mode: str) -> None:
    client = FakeExchangeClient({"NEWUSDT": _flat_candles()[:83]}, failing_timeframes={"2d"})
    config = _config(["NEWUSDT"], candle_limit=83, strategy_modes=(strategy_mode,))
    symbol_result = run(ScannerRunner(exchange_client=client).run(config)).results[0]

    assert symbol_result.status == ScannerPipelineStatus.REJECTED_BY_TECHNICAL
    assert symbol_result.technical_status == "INSUFFICIENT_DATA"
    assert symbol_result.technical_required_bars == 200
    assert symbol_result.technical_available_bars == 83
    assert symbol_result.rejection_stage == "technical_insufficient_data"


def test_malformed_primary_candle_is_technical_data_error_not_bad_structure() -> None:
    candles = _flat_candles()
    del candles[10]["high"]
    client = FakeExchangeClient({"BADUSDT": candles}, failing_timeframes={"2d"})
    symbol_result = run(ScannerRunner(exchange_client=client).run(_config(["BADUSDT"]))).results[0]

    assert symbol_result.status == ScannerPipelineStatus.REJECTED_BY_TECHNICAL
    assert symbol_result.technical_status == "DATA_ERROR"
    assert symbol_result.technical_score == NA
    assert symbol_result.rejection_stage == "technical_data_error"
    assert symbol_result.technical_available_bars == 220
    assert symbol_result.setup_quality.quality_state == SetupQualityState.DATA_ISSUE
    assert any("status=DATA_ERROR" in item for item in symbol_result.unverified_data)
    assert symbol_result.trade_idea is None


def test_high_score_cannot_override_missing_required_technical_evidence() -> None:
    technical_result = (
        ScannerRunner()
        .technical_agent.analyze(_flat_candles()[:199], timeframe="15m")
        .model_copy(update={"structure_score": 100})
    )

    class HighScoreInsufficientAgent:
        def analyze(self, candles, *, timeframe=NA):
            return technical_result.model_copy(update={"timeframe": timeframe})

    client = FakeExchangeClient({"BTCUSDT": _strategy_pullback_candles()}, failing_timeframes={"2d"})
    runner = ScannerRunner(exchange_client=client, technical_agent=HighScoreInsufficientAgent())
    symbol_result = run(runner.run(_config(["BTCUSDT"]))).results[0]

    assert symbol_result.status == ScannerPipelineStatus.REJECTED_BY_TECHNICAL
    assert symbol_result.technical_score == 100
    assert symbol_result.rejection_stage == "technical_insufficient_data"
    assert symbol_result.trade_idea is None


def test_global_context_precedes_symbol_queue_and_removes_only_available_labels() -> None:
    decision_at = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    provider = FakeBtcDominanceProvider(decision_at)
    service = BtcDominanceContextService(provider, clock=lambda: decision_at)
    client = FakeExchangeClient(
        {
            "ENAUSDT": _strategy_pullback_candles(),
            "BTCUSDT": _strategy_pullback_candles(),
        },
        failing_timeframes={"2d"},
    )
    config = _config(
        ["ENAUSDT", "BTCUSDT"],
        global_context_enabled=True,
        decision_timestamp=decision_at,
    )

    result = run(
        ScannerRunner(
            exchange_client=client,
            btc_d_context_service=service,
        ).run(config)
    )

    assert tuple(item.symbol for item in result.results) == ("ENAUSDT", "BTCUSDT")
    assert client.requested_klines[:3] == [
        ("BTCUSDT", "12h"),
        ("BTCUSDT", "2h"),
        ("BTCUSDT", "15m"),
    ]
    assert provider.calls == 1
    assert result.global_context is not None
    assert result.global_context.diagnostics.btc_d_cache_hit is False
    assert result.global_context.diagnostics.weekend_context_status == ContextStatus.VERIFIED
    for symbol_result in result.results:
        assert "btc_context: N/A" not in symbol_result.strategy_missing_data
        assert "btc_d_context: N/A" not in symbol_result.strategy_missing_data
        assert "weekend_filter: N/A" not in symbol_result.strategy_missing_data
        data_health = confirmed_data_health_for_symbol(symbol_result)
        assert "btc_context" not in data_health.required_missing
        assert "btc_d_context" not in data_health.required_missing
        assert "weekend_filter" not in data_health.required_missing


def test_btc_d_failure_is_isolated_and_missing_label_remains_visible() -> None:
    decision_at = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    provider = FakeBtcDominanceProvider(decision_at, error=TimeoutError("mock BTC.D timeout"))
    service = BtcDominanceContextService(provider, clock=lambda: decision_at)
    client = FakeExchangeClient(
        {
            "ENAUSDT": _strategy_pullback_candles(),
            "BTCUSDT": _strategy_pullback_candles(),
        },
        failing_timeframes={"2d"},
    )

    result = run(
        ScannerRunner(
            exchange_client=client,
            btc_d_context_service=service,
        ).run(
            _config(
                ["ENAUSDT"],
                global_context_enabled=True,
                decision_timestamp=decision_at,
            )
        )
    )
    symbol_result = result.results[0]

    assert symbol_result.status != ScannerPipelineStatus.SCAN_ERROR
    assert result.global_context.btc_d_context.status == ContextStatus.UNAVAILABLE
    assert "mock BTC.D timeout" in result.global_context.btc_d_context.reason
    assert "btc_d_context: N/A" in symbol_result.strategy_missing_data
    assert "btc_context: N/A" not in symbol_result.strategy_missing_data
    assert "weekend_filter: N/A" not in symbol_result.strategy_missing_data


def test_global_context_uses_closed_btc_candle_not_open_spike() -> None:
    candles = _flat_candles() + [
        {
            "timestamp": 220,
            "open": Decimal("100"),
            "high": Decimal("10000"),
            "low": Decimal("99"),
            "close": Decimal("10000"),
            "volume": Decimal("10000"),
        }
    ]
    decision_at = datetime.fromtimestamp(
        (220 * _INTERVAL_MS["15m"] + 7 * 60_000) / 1000,
        tz=timezone.utc,
    )
    provider = FakeBtcDominanceProvider(decision_at)
    service = BtcDominanceContextService(provider, clock=lambda: decision_at)
    client = FakeExchangeClient(
        {"ENAUSDT": candles, "BTCUSDT": candles},
        failing_timeframes={"2d"},
    )

    result = run(
        ScannerRunner(
            exchange_client=client,
            btc_d_context_service=service,
        ).run(
            _config(
                ["ENAUSDT"],
                global_context_enabled=True,
                decision_timestamp=decision_at,
            )
        )
    )
    execution = result.global_context.btc_context.value.execution_15m

    assert execution.status == ContextStatus.VERIFIED
    assert execution.value == "neutral"
    assert execution.observed_at <= decision_at
    assert execution.observed_at == datetime.fromtimestamp(
        220 * _INTERVAL_MS["15m"] / 1000,
        tz=timezone.utc,
    )


def test_repeated_scans_inside_btc_d_ttl_use_one_provider_observation() -> None:
    decision_at = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    provider = FakeBtcDominanceProvider(decision_at)
    service = BtcDominanceContextService(provider, clock=lambda: decision_at)
    client = FakeExchangeClient(
        {"ENAUSDT": _strategy_pullback_candles(), "BTCUSDT": _strategy_pullback_candles()},
        failing_timeframes={"2d"},
    )
    runner = ScannerRunner(exchange_client=client, btc_d_context_service=service)
    config = _config(
        ["ENAUSDT"],
        global_context_enabled=True,
        decision_timestamp=decision_at,
    )

    async def scan_twice():
        return await runner.run(config), await runner.run(config)

    first, second = run(scan_twice())

    assert provider.calls == 1
    assert first.global_context.btc_d_context.cache_hit is False
    assert second.global_context.btc_d_context.cache_hit is True


def test_research_only_global_context_does_not_change_strategy_or_delivery_outputs() -> None:
    decision_at = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    candles = {
        "ENAUSDT": _strategy_pullback_candles(),
        "BTCUSDT": _strategy_pullback_candles(),
    }
    baseline = run(
        ScannerRunner(exchange_client=FakeExchangeClient(candles, failing_timeframes={"2d"})).run(
            _config(["ENAUSDT"], decision_timestamp=decision_at)
        )
    )
    provider = FakeBtcDominanceProvider(decision_at)
    enriched = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient(candles, failing_timeframes={"2d"}),
            btc_d_context_service=BtcDominanceContextService(
                provider,
                clock=lambda: decision_at,
            ),
        ).run(
            _config(
                ["ENAUSDT"],
                global_context_enabled=True,
                decision_timestamp=decision_at,
            )
        )
    )
    before = baseline.results[0]
    after = enriched.results[0]
    before_setup = before.strategy_results["swing"].swing
    after_setup = after.strategy_results["swing"].swing

    assert after.status == before.status
    assert after.valid_strategy_modes == before.valid_strategy_modes
    assert after_setup.is_valid == before_setup.is_valid
    assert after_setup.rr_to_tp2 == before_setup.rr_to_tp2
    assert after_setup.trust_meter.percentage == before_setup.trust_meter.percentage
    assert after.score_result.total_score == before.score_result.total_score
    assert after.score_result.grade == before.score_result.grade
    assert after.setup_quality.quality_score == before.setup_quality.quality_score
    assert after.setup_quality.quality_grade == before.setup_quality.quality_grade
    assert after.lifecycle_state == before.lifecycle_state
    assert after.lifecycle_transition == before.lifecycle_transition
    assert enriched.dry_run_alerts_created == baseline.dry_run_alerts_created
    removed = set(before.strategy_missing_data) - set(after.strategy_missing_data)
    assert removed == {
        "btc_context: N/A",
        "btc_d_context: N/A",
        "weekend_filter: N/A",
    }


def test_global_context_alone_cannot_create_trade_idea_or_telegram_alert() -> None:
    decision_at = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    provider = FakeBtcDominanceProvider(decision_at)
    client = FakeExchangeClient(
        {"ENAUSDT": _flat_candles(), "BTCUSDT": _flat_candles()},
        failing_timeframes={"2d"},
    )

    result = run(
        ScannerRunner(
            exchange_client=client,
            btc_d_context_service=BtcDominanceContextService(
                provider,
                clock=lambda: decision_at,
            ),
        ).run(
            _config(
                ["ENAUSDT"],
                global_context_enabled=True,
                decision_timestamp=decision_at,
            )
        )
    )

    assert result.results[0].trade_idea is None
    assert result.results[0].alert_result is None
    assert result.trade_ideas_created == 0
    assert result.dry_run_alerts_created == 0


def _verified_microstructure_snapshot(decision_at: datetime):
    from app.microstructure.models import FlowWindowSnapshot, MicrostructureFlowSnapshot

    windows = {}
    for minutes in (1, 5, 15):
        windows[f"{minutes}m"] = FlowWindowSnapshot(
            window_minutes=minutes,
            window_start=decision_at.replace(tzinfo=timezone.utc)
            - timedelta(minutes=minutes),
            window_end=decision_at,
            coverage_seconds=minutes * 60,
            coverage_complete=True,
            aggressive_buy_base=Decimal("10"),
            aggressive_sell_base=Decimal("4"),
            aggressive_buy_quote=Decimal("1000"),
            aggressive_sell_quote=Decimal("400"),
            delta_base=Decimal("6"),
            delta_quote=Decimal("600"),
            total_quote=Decimal("1400"),
            flow_imbalance_ratio=Decimal("0.42857143"),
            buyer_aggression_pct=Decimal("71.4286"),
            rolling_cvd_quote=Decimal("600"),
            cvd_slope_quote_per_min=Decimal("40"),
            price_return_pct=Decimal("1"),
            price_cvd_alignment="ALIGNED_UP",
            normal_quote_notional=Decimal("1300"),
            rpi_quote_notional=Decimal("100"),
            aggregate_event_count=42,
            underlying_trade_count=84,
        )
    return MicrostructureFlowSnapshot(
        symbol="BTCUSDT",
        source="binance_usdm:btcusdt@aggTrade",
        observed_at=decision_at,
        age_seconds=1,
        status=ContextStatus.VERIFIED,
        windows=windows,
        orderflow_summary=(
            "15m delta +600.00 USDT; buyer aggression 71.4%; "
            "CVD slope positive; price/CVD ALIGNED_UP; absorption."
        ),
        retained_bucket_count=16,
        max_retained_bucket_count=16,
        last_aggregate_trade_id=99,
        accepted_event_count=42,
    )


class _StaticMicrostructureService:
    def __init__(self, snapshot=None, *, error: Exception | None = None) -> None:
        self.value = snapshot
        self.error = error

    def snapshot(self, symbol: str):
        if self.error is not None:
            raise self.error
        return self.value.model_copy(update={"symbol": symbol})


def test_research_only_microstructure_does_not_change_strategy_lifecycle_or_delivery() -> None:
    decision_at = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    candles = {"BTCUSDT": _strategy_pullback_candles()}
    baseline = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient(candles, failing_timeframes={"2d"})
        ).run(_config(["BTCUSDT"], decision_timestamp=decision_at))
    )
    enriched = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient(candles, failing_timeframes={"2d"}),
            microstructure_flow_service=_StaticMicrostructureService(
                _verified_microstructure_snapshot(decision_at)
            ),
        ).run(
            _config(
                ["BTCUSDT"],
                decision_timestamp=decision_at,
                microstructure_flow_enabled=True,
            )
        )
    )

    before = baseline.results[0]
    after = enriched.results[0]
    before_setup = before.strategy_results["swing"].swing
    after_setup = after.strategy_results["swing"].swing

    assert after.microstructure_flow.status == ContextStatus.VERIFIED
    assert after.status == before.status
    assert after.valid_strategy_modes == before.valid_strategy_modes
    assert after_setup.is_valid == before_setup.is_valid
    assert after_setup.first_failed_gate == before_setup.first_failed_gate
    assert after_setup.trust_meter == before_setup.trust_meter
    assert after_setup.gate_result == before_setup.gate_result
    assert after_setup.entry_low == before_setup.entry_low
    assert after_setup.entry_high == before_setup.entry_high
    assert after_setup.stop == before_setup.stop
    assert after_setup.tp1 == before_setup.tp1
    assert after_setup.tp2 == before_setup.tp2
    assert after_setup.tp3 == before_setup.tp3
    assert after_setup.rr_to_tp2 == before_setup.rr_to_tp2
    assert after_setup.invalidation == before_setup.invalidation
    assert after.score_result.total_score == before.score_result.total_score
    assert after.score_result.grade == before.score_result.grade
    assert after.setup_quality.quality_score == before.setup_quality.quality_score
    assert after.setup_quality.quality_grade == before.setup_quality.quality_grade
    assert after.setup_quality.quality_state == before.setup_quality.quality_state
    assert after.setup_quality.action_label == before.setup_quality.action_label
    assert after.trade_idea.direction == before.trade_idea.direction
    assert after.trade_idea.status == before.trade_idea.status
    assert after.trade_idea.entry_zone == before.trade_idea.entry_zone
    assert after.trade_idea.stop_loss == before.trade_idea.stop_loss
    assert after.trade_idea.invalidation == before.trade_idea.invalidation
    assert after.trade_idea.take_profits == before.trade_idea.take_profits
    assert after.trade_idea.best_rr == before.trade_idea.best_rr
    assert after.trade_idea.confidence_score == before.trade_idea.confidence_score
    assert after.trade_idea.grade == before.trade_idea.grade
    assert after.trade_idea.quality_gate_result == before.trade_idea.quality_gate_result
    assert after.lifecycle_state == before.lifecycle_state
    assert after.lifecycle_transition == before.lifecycle_transition
    assert enriched.trade_ideas_created == baseline.trade_ideas_created
    assert enriched.dry_run_alerts_created == baseline.dry_run_alerts_created
    assert after.alert_result.status == before.alert_result.status
    assert after.alert_result.channel == before.alert_result.channel
    assert after.alert_result.dry_run == before.alert_result.dry_run
    removed = set(before.strategy_missing_data) - set(after.strategy_missing_data)
    assert removed == {"cvd: N/A", "orderflow_summary: N/A"}


def test_disabled_microstructure_does_not_call_or_change_scanner_behavior() -> None:
    decision_at = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    candles = {"BTCUSDT": _strategy_pullback_candles()}
    baseline = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient(candles, failing_timeframes={"2d"})
        ).run(_config(["BTCUSDT"], decision_timestamp=decision_at))
    ).results[0]
    disabled = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient(candles, failing_timeframes={"2d"}),
            microstructure_flow_service=_StaticMicrostructureService(
                error=AssertionError("disabled service must not be called")
            ),
        ).run(_config(["BTCUSDT"], decision_timestamp=decision_at))
    ).results[0]

    assert disabled.microstructure_flow is None
    assert disabled.status == baseline.status
    assert disabled.valid_strategy_modes == baseline.valid_strategy_modes
    assert disabled.score_result.total_score == baseline.score_result.total_score
    assert disabled.score_result.grade == baseline.score_result.grade
    assert disabled.lifecycle_state == baseline.lifecycle_state
    assert disabled.lifecycle_transition == baseline.lifecycle_transition
    assert not any(item.startswith("microstructure_flow:") for item in disabled.missing_data)
    assert not any(item.startswith("microstructure_flow:") for item in disabled.unverified_data)

def test_stale_research_only_microstructure_does_not_change_quality_or_plan() -> None:
    decision_at = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    candles = {"BTCUSDT": _strategy_pullback_candles()}
    baseline = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient(candles, failing_timeframes={"2d"})
        ).run(_config(["BTCUSDT"], decision_timestamp=decision_at))
    ).results[0]
    stale = _verified_microstructure_snapshot(decision_at).model_copy(
        update={
            "status": ContextStatus.STALE,
            "reason": "last_valid_event_stale",
            "age_seconds": 10,
        }
    )
    enriched = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient(candles, failing_timeframes={"2d"}),
            microstructure_flow_service=_StaticMicrostructureService(stale),
        ).run(
            _config(
                ["BTCUSDT"],
                decision_timestamp=decision_at,
                microstructure_flow_enabled=True,
            )
        )
    ).results[0]

    before_setup = baseline.strategy_results["swing"].swing
    after_setup = enriched.strategy_results["swing"].swing
    assert enriched.status == baseline.status
    assert enriched.valid_strategy_modes == baseline.valid_strategy_modes
    assert after_setup.trust_meter == before_setup.trust_meter
    assert after_setup.gate_result == before_setup.gate_result
    assert after_setup.rr_to_tp2 == before_setup.rr_to_tp2
    assert (after_setup.entry_low, after_setup.entry_high, after_setup.stop) == (
        before_setup.entry_low,
        before_setup.entry_high,
        before_setup.stop,
    )
    assert (after_setup.tp1, after_setup.tp2, after_setup.tp3) == (
        before_setup.tp1,
        before_setup.tp2,
        before_setup.tp3,
    )
    assert enriched.score_result.total_score == baseline.score_result.total_score
    assert enriched.score_result.grade == baseline.score_result.grade
    assert enriched.setup_quality.quality_score == baseline.setup_quality.quality_score
    assert enriched.setup_quality.quality_grade == baseline.setup_quality.quality_grade
    assert enriched.lifecycle_state == baseline.lifecycle_state
    assert enriched.lifecycle_transition == baseline.lifecycle_transition
    assert enriched.trade_idea.best_rr == baseline.trade_idea.best_rr
    assert enriched.trade_idea.take_profits == baseline.trade_idea.take_profits
    assert enriched.alert_result.status == baseline.alert_result.status
    assert any(item.startswith("microstructure_flow: Unverified") for item in enriched.unverified_data)

def test_microstructure_service_exception_does_not_crash_scanner() -> None:
    decision_at = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    result = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient(
                {"BTCUSDT": _strategy_pullback_candles()},
                failing_timeframes={"2d"},
            ),
            microstructure_flow_service=_StaticMicrostructureService(
                error=RuntimeError("synthetic snapshot failure")
            ),
        ).run(
            _config(
                ["BTCUSDT"],
                decision_timestamp=decision_at,
                microstructure_flow_enabled=True,
            )
        )
    )

    symbol_result = result.results[0]
    assert symbol_result.status != ScannerPipelineStatus.SCAN_ERROR
    assert symbol_result.microstructure_flow.status == ContextStatus.ERROR
    assert symbol_result.microstructure_flow.reason == "service_error:RuntimeError"
    assert (
        "microstructure_flow: N/A (status=ERROR, reason=service_error:RuntimeError)"
        in symbol_result.missing_data
    )
    assert "cvd: N/A" in symbol_result.strategy_missing_data
    assert "orderflow_summary: N/A" in symbol_result.strategy_missing_data
    data_health = confirmed_data_health_for_symbol(symbol_result)
    assert data_health.blocked is False
    assert data_health.required_missing == ()
    assert "microstructure_flow" in data_health.optional_missing


@pytest.mark.parametrize(
    ("status", "reason", "expected_channel"),
    [
        (ContextStatus.UNAVAILABLE, "insufficient_window_coverage", "missing"),
        (ContextStatus.UNAVAILABLE, "stream_disconnected", "missing"),
        (
            ContextStatus.UNAVAILABLE,
            "subscription_limit_exceeded:max_symbols=100",
            "missing",
        ),
        (ContextStatus.STALE, "last_valid_event_stale", "unverified"),
        (
            ContextStatus.UNAVAILABLE,
            "aggregate_trade_id_gap_in_window",
            "unverified",
        ),
    ],
)
def test_nonverified_microstructure_preserves_truthful_optional_diagnostics(
    status: ContextStatus,
    reason: str,
    expected_channel: str,
) -> None:
    from app.microstructure.models import MicrostructureFlowSnapshot

    decision_at = datetime(2026, 8, 27, 12, tzinfo=timezone.utc)
    unavailable = MicrostructureFlowSnapshot.unavailable(
        symbol="BTCUSDT",
        reason=reason,
        status=status,
        observed_at=decision_at,
        age_seconds=10,
    )
    result = run(
        ScannerRunner(
            exchange_client=FakeExchangeClient(
                {"BTCUSDT": _strategy_pullback_candles()},
                failing_timeframes={"2d"},
            ),
            microstructure_flow_service=_StaticMicrostructureService(unavailable),
        ).run(
            _config(
                ["BTCUSDT"],
                decision_timestamp=decision_at,
                microstructure_flow_enabled=True,
            )
        )
    )

    symbol_result = result.results[0]
    label = "N/A" if expected_channel == "missing" else "Unverified"
    diagnostic = f"microstructure_flow: {label} (status={status.value}, reason={reason})"
    data_health = confirmed_data_health_for_symbol(symbol_result)

    if expected_channel == "missing":
        assert diagnostic in symbol_result.missing_data
        assert diagnostic not in symbol_result.unverified_data
        assert "microstructure_flow" in data_health.optional_missing
        assert "microstructure_flow" not in data_health.optional_unverified
    else:
        assert diagnostic not in symbol_result.missing_data
        assert diagnostic in symbol_result.unverified_data
        assert "microstructure_flow" not in data_health.optional_missing
        assert "microstructure_flow" in data_health.optional_unverified
    assert data_health.blocked is False
    assert data_health.required_missing == ()
    assert data_health.required_unverified == ()
    assert "cvd: N/A" in symbol_result.strategy_missing_data
    assert "orderflow_summary: N/A" in symbol_result.strategy_missing_data
