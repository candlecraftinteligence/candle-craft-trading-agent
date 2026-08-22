from __future__ import annotations

import asyncio
from decimal import Decimal
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.alerts import telegram_lifecycle
from app.command_center import build_command_center_payload
from app.core.minimum_rr import (
    candidate_rr_meets_minimum,
    minimum_rr_policy,
    rr_rejection_reason,
)
from app.data.dtos import NA
from app.pipeline import scanner_runner
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerSymbolResult,
)
from app.strategies import liquidity_grab_pullback as strategy
from app.strategies.liquidity_grab_pullback import LiquidityGrabSetup
from scripts import run_scan


def _config(*, min_rr: Decimal = Decimal("2.5")) -> ScannerRunConfig:
    return ScannerRunConfig(
        symbols=("BTCUSDT",),
        exchange="binance",
        account_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
        min_rr=min_rr,
    )


def _base_candles(count: int = 45) -> list[dict[str, Decimal | int]]:
    return [
        {
            "timestamp": index,
            "open": Decimal("100"),
            "high": Decimal("105"),
            "low": Decimal("95"),
            "close": Decimal("100"),
            "volume": Decimal("100"),
        }
        for index in range(count)
    ]


def _full_bullish_setup_candles() -> list[dict[str, Decimal | int]]:
    candles = _base_candles(36)
    candles[20]["low"] = Decimal("90")
    candles[24]["high"] = Decimal("110")
    candles[30]["low"] = Decimal("85")
    candles[30]["close"] = Decimal("91")
    candles[30]["volume"] = Decimal("200")
    candles[33].update(
        open=Decimal("99"),
        close=Decimal("97"),
        low=Decimal("95"),
        high=Decimal("100"),
    )
    candles[35].update(
        open=Decimal("104"),
        high=Decimal("114"),
        low=Decimal("101"),
        close=Decimal("112"),
    )
    return candles


def _trend_candles(count: int = 30) -> list[dict[str, Decimal | int]]:
    return [
        {
            "timestamp": index,
            "open": Decimal(100 + index),
            "high": Decimal(102 + index),
            "low": Decimal(98 + index),
            "close": Decimal(101 + index),
            "volume": Decimal("100"),
        }
        for index in range(count)
    ]


def _valid_strategy_input(*, min_rr: Decimal = Decimal("2.5")) -> dict[str, object]:
    return {
        "symbol": "BTCUSDT",
        "mode": "swing",
        "candles_15m": _full_bullish_setup_candles(),
        "candles_5m": _full_bullish_setup_candles(),
        "candles_2d": _trend_candles(),
        "min_rr": min_rr,
    }


def _force_pullback_rr(monkeypatch: pytest.MonkeyPatch, candidate_rr: Decimal) -> None:
    original = strategy.analyze_pullback_zone

    def analyze_with_forced_rr(input_data):
        result = original(input_data)
        if result.entry == NA or result.stop == NA:
            return result
        entry = Decimal(result.entry)
        stop = Decimal(result.stop)
        risk = abs(entry - stop)
        tp2 = entry + (candidate_rr * risk) if input_data.direction == "long" else entry - (candidate_rr * risk)
        return result.model_copy(
            update={
                "valid": True,
                "pullback_zone_status": "valid",
                "first_failed_gate": NA,
                "pullback_failure_reason": NA,
                "tp2": tp2,
                "rr_to_tp2": candidate_rr,
            }
        )

    monkeypatch.setattr(strategy, "analyze_pullback_zone", analyze_with_forced_rr)


def _symbol_with_rr_diagnostics(
    *,
    configured: Decimal,
    hard: Decimal,
    effective: Decimal,
    candidate: Decimal,
    mode: str = "swing",
    rejection_reason: str = NA,
) -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        strategy_diagnostics={
            mode: {
                "mode": mode,
                "configured_global_minimum_rr": configured,
                "hard_mode_floor": hard,
                "effective_minimum_rr": effective,
                "candidate_rr": candidate,
                "rr_to_tp2": candidate,
                "rr_rejection_reason": rejection_reason,
            }
        },
        rejected_strategy_modes=(mode,),
    )


def _run_result(config: ScannerRunConfig, symbol_result: ScannerSymbolResult) -> ScannerRunResult:
    return ScannerRunResult(
        config=config,
        results=(symbol_result,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )


def test_cli_default_produces_configured_minimum_rr_25() -> None:
    assert run_scan.parse_args([]).min_rr == Decimal("2.5")
    assert _config().min_rr == Decimal("2.5")


def test_cli_override_is_passed_into_scanner_run_config(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class ConfigCaptured(Exception):
        pass

    async def resolve_watchlist(_args):
        return SimpleNamespace(symbols=("BTCUSDT",))

    def capture_config(**kwargs):
        captured.update(kwargs)
        raise ConfigCaptured

    monkeypatch.setattr(run_scan, "_resolve_watchlist_for_args", resolve_watchlist)
    monkeypatch.setattr(run_scan, "ScannerRunConfig", capture_config)

    with pytest.raises(ConfigCaptured):
        asyncio.run(run_scan.main(["--min-rr", "4.0"]))

    assert captured["min_rr"] == Decimal("4.0")


@pytest.mark.parametrize(
    ("mode", "configured", "hard_floor", "effective"),
    (
        ("scalp", "2.5", "2.5", "2.5"),
        ("swing", "2.5", "2.5", "2.5"),
        ("challenge", "2.5", "3.0", "3.0"),
        ("scalp", "4.0", "2.5", "4.0"),
        ("swing", "4.0", "2.5", "4.0"),
        ("challenge", "4.0", "3.0", "4.0"),
    ),
)
def test_effective_minimum_rr_policy(mode: str, configured: str, hard_floor: str, effective: str) -> None:
    policy = minimum_rr_policy(Decimal(configured), mode)

    assert policy.hard_mode_floor == Decimal(hard_floor)
    assert policy.effective_minimum_rr == Decimal(effective)


@pytest.mark.parametrize("value", ("2.49", "0", "-1", "NaN", "Infinity", "-Infinity", "invalid"))
def test_cli_rejects_unsafe_or_invalid_minimum_rr(value: str, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        run_scan.parse_args(["--min-rr", value])

    assert "finite decimal greater than or equal to 2.5" in capsys.readouterr().err


@pytest.mark.parametrize("value", (Decimal("2.49"), Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("Infinity")))
def test_scanner_config_rejects_values_that_could_weaken_or_break_rr(value: Decimal) -> None:
    with pytest.raises(ValidationError, match="finite decimal greater than or equal to 2.5"):
        _config(min_rr=value)


@pytest.mark.parametrize(
    ("mode", "configured", "candidate", "accepted"),
    (
        ("scalp", "2.5", "2.5", True),
        ("scalp", "2.5", "2.49999999", False),
        ("swing", "2.5", "2.5", True),
        ("swing", "2.5", "2.49999999", False),
        ("challenge", "2.5", "3.0", True),
        ("challenge", "2.5", "2.99999999", False),
        ("scalp", "4.0", "4.0", True),
        ("swing", "4.0", "3.99999999", False),
        ("challenge", "4.0", "4.0", True),
    ),
)
def test_candidate_rr_boundary_is_inclusive(
    mode: str,
    configured: str,
    candidate: str,
    accepted: bool,
) -> None:
    policy = minimum_rr_policy(Decimal(configured), mode)

    assert candidate_rr_meets_minimum(Decimal(candidate), policy) is accepted
    assert (rr_rejection_reason(Decimal(candidate), policy) == NA) is accepted


def test_higher_override_is_enforced_by_strategy_evaluation() -> None:
    setup = strategy.analyze_liquidity_grab_pullback(_valid_strategy_input(min_rr=Decimal("4.0"))).swing

    assert setup.is_valid is False
    assert setup.first_failed_gate == "rr_below_minimum"
    assert setup.configured_global_minimum_rr == Decimal("4.0")
    assert setup.hard_mode_floor == Decimal("2.5")
    assert setup.effective_minimum_rr == Decimal("4.0")
    assert setup.candidate_rr == Decimal("2.65955826")
    assert setup.candidate_rr == (
        (setup.tp2 - setup.entry) / (setup.entry - setup.stop)
    ).quantize(Decimal("0.00000001"))
    assert setup.rr_rejection_reason.startswith("rr_below_minimum:candidate_rr=2.65955826;")


@pytest.mark.parametrize(
    ("mode", "candidate", "expected_valid"),
    (
        ("scalp", Decimal("4.0"), True),
        ("swing", Decimal("4.0"), True),
        ("challenge", Decimal("4.0"), True),
        ("swing", Decimal("3.99999999"), False),
    ),
)
def test_strategy_rr_gate_uses_inclusive_effective_boundary(
    monkeypatch: pytest.MonkeyPatch,
    mode: str,
    candidate: Decimal,
    expected_valid: bool,
) -> None:
    _force_pullback_rr(monkeypatch, candidate)

    result = strategy.analyze_liquidity_grab_pullback(_valid_strategy_input(min_rr=Decimal("4.0")))
    setup = getattr(result, mode)

    assert setup.is_valid is expected_valid
    assert ("rr_below_minimum" in setup.gates_failed) is (not expected_valid)


def test_default_strategy_result_is_unchanged_by_explicit_default() -> None:
    default_input = _valid_strategy_input()
    default_input.pop("min_rr")

    implicit = strategy.analyze_liquidity_grab_pullback(default_input)
    explicit = strategy.analyze_liquidity_grab_pullback({**default_input, "min_rr": Decimal("2.5")})

    assert implicit.model_dump() == explicit.model_dump()


def test_long_and_short_rr_calculation_remains_symmetric() -> None:
    long_rr = strategy._risk_reward("bullish", Decimal("100"), Decimal("90"), Decimal("125"))
    short_rr = strategy._risk_reward("bearish", Decimal("100"), Decimal("110"), Decimal("75"))
    policy = minimum_rr_policy(Decimal("2.5"), "swing")

    assert long_rr == short_rr == Decimal("2.50000000")
    assert candidate_rr_meets_minimum(long_rr, policy) is True
    assert candidate_rr_meets_minimum(short_rr, policy) is True


def test_target_integrity_receives_strategy_effective_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    def capture_target_intelligence(**kwargs):
        captured.update(kwargs)
        return scanner_runner.TargetIntelligenceResult()

    monkeypatch.setattr(scanner_runner, "build_target_intelligence", capture_target_intelligence)
    setup = LiquidityGrabSetup(mode="challenge", effective_minimum_rr=Decimal("4.0"))
    technical = SimpleNamespace(
        recent_range_high=NA,
        recent_range_low=NA,
        nearest_support=NA,
        nearest_resistance=NA,
    )
    volume_profile = SimpleNamespace(
        poc=NA,
        value_area_high=NA,
        value_area_low=NA,
        nearest_high_volume_node=NA,
        nearest_low_volume_node=NA,
    )

    scanner_runner._target_intelligence_for_setup(
        setup=setup,
        diagnostics={},
        candles_by_timeframe={},
        technical=technical,
        volume_profile=volume_profile,
        higher_timeframe_volume_profile=None,
    )

    assert captured["minimum_rr"] == Decimal("4.0")


@pytest.mark.parametrize(
    ("diagnostic_floor", "unrelated_context_floor"),
    ((Decimal("4.0"), Decimal("2.5")), (Decimal("2.5"), Decimal("4.0"))),
)
def test_public_delivery_uses_strategy_effective_threshold(
    diagnostic_floor: Decimal,
    unrelated_context_floor: Decimal,
) -> None:
    symbol_result = _symbol_with_rr_diagnostics(
        configured=diagnostic_floor,
        hard=Decimal("2.5"),
        effective=diagnostic_floor,
        candidate=diagnostic_floor,
    )
    context = telegram_lifecycle.TelegramEligibilityContext(min_rr=unrelated_context_floor)

    aligned = telegram_lifecycle._minimum_rr_context_for_symbol(symbol_result, context)

    assert aligned.min_rr == diagnostic_floor


def test_audit_and_manifest_report_complete_rr_policy() -> None:
    reason = (
        "rr_below_minimum:candidate_rr=3.9;configured_global_minimum_rr=4.0;"
        "hard_mode_floor=2.5;effective_minimum_rr=4.0;mode=swing"
    )
    symbol_result = _symbol_with_rr_diagnostics(
        configured=Decimal("4.0"),
        hard=Decimal("2.5"),
        effective=Decimal("4.0"),
        candidate=Decimal("3.9"),
        rejection_reason=reason,
    )
    result = _run_result(_config(min_rr=Decimal("4.0")), symbol_result)

    payload = build_command_center_payload(result, ranked_results=())
    audit = payload["minimum_rr_audit"][0]
    manifest = run_scan._scan_run_manifest_row(
        result,
        watchlist=SimpleNamespace(
            universe=SimpleNamespace(mode="manual", label="manual"),
            source_label="manual",
        ),
        ranked_results=(),
    )

    assert payload["configured_min_rr"] == "4"
    assert payload["minimum_rr_policy"]["modes"]["challenge"]["hard_mode_floor"] == "3"
    assert payload["minimum_rr_policy"]["modes"]["challenge"]["effective_minimum_rr"] == "4"
    assert audit == {
        "symbol": "BTCUSDT",
        "mode": "swing",
        "configured_global_minimum_rr": "4",
        "hard_mode_floor": "2.5",
        "effective_minimum_rr": "4",
        "candidate_rr": "3.9",
        "rr_rejection_reason": reason,
    }
    assert manifest["minimum_rr_policy"] == payload["minimum_rr_policy"]
    assert manifest["minimum_rr_audit"] == payload["minimum_rr_audit"]


def test_strategy_has_no_order_execution_surface() -> None:
    engine = strategy.LiquidityGrabEngine()

    assert not hasattr(engine, "place_order")
    assert not hasattr(engine, "execute_order")
