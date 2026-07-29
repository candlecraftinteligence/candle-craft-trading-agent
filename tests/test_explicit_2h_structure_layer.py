from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal

from app.analytics.post_restart_funnel_audit import _timeframe_verification
from app.pipeline.scanner_runner import (
    ScannerRunConfig,
    ScannerRunResult,
    _direct_strategy_timeframes,
)
from app.storage.repositories import _scan_run_record
from app.strategies import liquidity_grab_pullback as strategy_module
from app.strategies.liquidity_grab_pullback import (
    LiquidityGrabEngine,
    LiquidityGrabInput,
    StructureShiftSignal,
)
from scripts import run_scan


def _candles(start: str, *, count: int = 24) -> list[dict[str, Decimal]]:
    base = Decimal(start)
    return [
        {
            "open": base + Decimal(index),
            "high": base + Decimal(index) + Decimal("2"),
            "low": base + Decimal(index) - Decimal("1"),
            "close": base + Decimal(index) + Decimal("1"),
            "volume": Decimal("100"),
        }
        for index in range(count)
    ]


def _scanner_config(**overrides: object) -> ScannerRunConfig:
    data: dict[str, object] = {
        "symbols": ["BTCUSDT"],
        "exchange": "binance",
        "account_equity": Decimal("10000"),
        "risk_per_trade_pct": Decimal("1"),
    }
    data.update(overrides)
    return ScannerRunConfig.model_validate(data)


def test_default_cli_and_config_resolve_the_explicit_hierarchy() -> None:
    args = run_scan.parse_args([])
    config = _scanner_config(
        htf_timeframe=args.htf_timeframe,
        bias_timeframe=args.bias_timeframe,
        structure_timeframe=args.structure_timeframe,
        execution_timeframe=args.execution_timeframe,
        confirmation_timeframe=args.confirmation_timeframe,
    )

    assert (
        config.htf_timeframe,
        config.bias_timeframe,
        config.structure_timeframe,
        config.execution_timeframe,
        config.confirmation_timeframe,
    ) == ("2d", "12h", "2h", "15m", "15m")
    assert "2d>12h>2h>15m>15m" in run_scan._format_available_command_presets()


def test_structure_timeframe_changes_only_the_structure_fetch_role() -> None:
    default = _scanner_config()
    changed = _scanner_config(structure_timeframe="3h")
    default_requests = set(_direct_strategy_timeframes(default))
    changed_requests = set(_direct_strategy_timeframes(changed))

    assert default.execution_timeframe == changed.execution_timeframe == "15m"
    assert default.confirmation_timeframe == changed.confirmation_timeframe == "15m"
    assert default_requests ^ changed_requests == {"2h", "3h"}
    assert _direct_strategy_timeframes(default).count("15m") == 1


def test_confirmation_timeframe_does_not_change_structure_role() -> None:
    default = _scanner_config()
    confirmation_override = _scanner_config(confirmation_timeframe="5m")

    assert default.structure_timeframe == confirmation_override.structure_timeframe == "2h"
    assert "2h" in _direct_strategy_timeframes(confirmation_override)
    assert "5m" in _direct_strategy_timeframes(confirmation_override)


def test_structure_analyzer_receives_2h_while_execution_and_confirmation_remain_m15(
    monkeypatch,
) -> None:
    structure_candles = _candles("200")
    m15_candles = _candles("100")
    structure_inputs: list[tuple[Decimal, ...]] = []
    execution_roles: list[str] = []
    confirmation_roles: list[str] = []
    original_execution = strategy_module._select_execution_candles
    original_confirmation = strategy_module._select_confirmation_candles

    def spy_structure(candles, **_kwargs):
        structure_inputs.append(tuple(candle.close for candle in candles))
        return StructureShiftSignal()

    def spy_execution(normalized, data):
        selected = original_execution(normalized, data)
        execution_roles.append(selected.timeframe if selected is not None else "N/A")
        return selected

    def spy_confirmation(normalized, data):
        selected = original_confirmation(normalized, data)
        confirmation_roles.append(selected.timeframe if selected is not None else "N/A")
        return selected

    monkeypatch.setattr(strategy_module, "detect_structure_shift", spy_structure)
    monkeypatch.setattr(strategy_module, "_select_execution_candles", spy_execution)
    monkeypatch.setattr(strategy_module, "_select_confirmation_candles", spy_confirmation)

    result = LiquidityGrabEngine().analyze(
        LiquidityGrabInput(
            symbol="BTCUSDT",
            structure_analysis_required=True,
            structure_candles=structure_candles,
            candles_15m=m15_candles,
        )
    )

    assert structure_inputs
    assert all(values[0] == Decimal("201") for values in structure_inputs)
    assert set(execution_roles) == {"15m"}
    assert set(confirmation_roles) == {"15m"}
    assert all("candles_2h: N/A" not in setup.missing_data for setup in (result.challenge, result.swing, result.scalp))


def test_missing_structure_data_is_explicit_without_a_new_structure_gate() -> None:
    result = LiquidityGrabEngine().analyze(
        LiquidityGrabInput(
            symbol="BTCUSDT",
            structure_analysis_required=True,
            candles_15m=_candles("100"),
        )
    )

    for setup in (result.challenge, result.swing, result.scalp):
        assert "candles_2h: N/A" in setup.missing_data
        assert "structure" not in setup.gates_failed


def test_structure_metadata_is_persisted_without_lifecycle_or_candidate_changes() -> None:
    result = ScannerRunResult(
        config=_scanner_config(),
        results=(),
        scanned_symbols=0,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )

    record = _scan_run_record(
        run_id="phase2-structure",
        timestamp="2026-07-29T00:00:00Z",
        result=result,
        command_preset=None,
        command_used=None,
        raw_payload={"results": []},
        watch_iteration=None,
    )

    assert json.loads(record.timeframes_json) == {
        "htf": "2d",
        "bias": "12h",
        "structure": "2h",
        "execution": "15m",
        "confirmation": "15m",
    }


def test_structure_audit_requires_explicit_structure_evidence_and_keeps_history_readable() -> None:
    explicit = _timeframe_verification(
        [{"timeframes_json": json.dumps({"context": "2d", "bias": "12h", "structure": "2h", "execution": "15m", "confirmation": "15m"})}],
        Counter(),
        None,
    )
    historical = _timeframe_verification(
        [{"timeframes_json": json.dumps({"htf": "2d", "bias": "12h", "execution": "15m", "confirmation": "15m"})}],
        Counter(),
        None,
    )
    unrelated = _timeframe_verification(
        [{"timeframes_json": json.dumps({"unrelated_metric_timeframe": "2h"})}],
        Counter(),
        None,
    )

    assert explicit["timeframes"]["2H_structure"]["status"] == "ACTIVE_AND_VERIFIED"
    assert historical["timeframes"]["2H_structure"]["status"] == "NOT_VERIFIABLE"
    structure = unrelated["timeframes"]["2H_structure"]
    assert structure["status"] == "NOT_VERIFIABLE"
    assert any("unrelated_metric_timeframe" in item for item in structure["exact_evidence"])


def test_insufficient_structure_history_is_reported_by_the_established_analyzer() -> None:
    selected = strategy_module._select_structure_candles(
        LiquidityGrabInput(
            symbol="BTCUSDT",
            structure_candles=_candles("200", count=3),
        )
    )

    assert selected is not None
    signal = strategy_module._analyze_structure_layer(selected, lookback=2)
    assert signal.is_present is False
