from __future__ import annotations

import json
from collections import Counter
from decimal import Decimal
from typing import Any

from app.analytics.post_restart_funnel_audit import (
    QueryLimits,
    _compact_query_row,
    _timeframe_verification,
)
from app.pipeline.scanner_runner import ScannerRunConfig, _direct_strategy_timeframes, _strategy_diagnostics_for_setup
from app.strategies import liquidity_grab_pullback as strategy_module
from app.strategies.liquidity_grab_pullback import (
    LiquidityGrabEngine,
    LiquidityGrabInput,
    LiquiditySweepSignal,
    StructureLayerAnalysis,
    StructureShiftSignal,
)


def _flat_candles(count: int = 45) -> list[dict[str, Decimal | int]]:
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


def _structure_shift_candles() -> list[dict[str, Decimal | int]]:
    candles = _flat_candles()
    candles[20]["low"] = Decimal("90")
    candles[24]["high"] = Decimal("110")
    candles[30]["low"] = Decimal("85")
    candles[30]["close"] = Decimal("91")
    candles[35]["high"] = Decimal("114")
    candles[35]["close"] = Decimal("112")
    return candles


def _engine_input(**overrides: Any) -> LiquidityGrabInput:
    values: dict[str, Any] = {
        "symbol": "BTCUSDT",
        "candles_15m": _flat_candles(),
    }
    values.update(overrides)
    return LiquidityGrabInput.model_validate(values)


def _qualification_projection(setup: Any) -> tuple[Any, ...]:
    return (
        setup.is_valid,
        setup.status,
        setup.bias,
        setup.trust_meter,
        setup.gate_result,
        setup.rr_to_tp2,
        setup.entry_low,
        setup.entry_high,
        setup.entry,
        setup.stop,
        setup.tp1,
        setup.tp2,
        setup.tp3,
    )


def test_retains_known_2h_sweep_and_bos_choch_distinct_from_m15_confirmation() -> None:
    result = LiquidityGrabEngine().analyze(
        _engine_input(
            structure_analysis_required=True,
            structure_candles=_structure_shift_candles(),
        )
    )

    analysis = result.structure_layer_analysis
    assert analysis.timeframe == "2h"
    assert analysis.candle_count == 45
    assert analysis.status == "ANALYZED_SHIFT_PRESENT"
    assert analysis.sweep.is_present is True
    assert analysis.sweep.direction == "bullish"
    assert analysis.structure_shift.is_present is True
    assert analysis.structure_shift.kind in {"BOS", "CHoCH"}
    assert analysis.direction == "bullish"

    for setup in (result.challenge, result.swing, result.scalp):
        assert setup.structure_layer_analysis == analysis
        assert setup.structure_shift.is_present is False
        scanner_payload = _strategy_diagnostics_for_setup(setup)
        assert scanner_payload["structure_layer_analysis"] == analysis.model_dump(mode="json")


def test_structure_diagnostic_reports_missing_insufficient_and_neutral_without_direction() -> None:
    engine = LiquidityGrabEngine()

    missing = engine.analyze(_engine_input(structure_analysis_required=True))
    assert missing.structure_layer_analysis.status == "MISSING_DATA"
    assert "missing" in missing.structure_layer_analysis.reason.lower()

    insufficient = engine.analyze(
        _engine_input(
            structure_analysis_required=True,
            structure_candles=_flat_candles(3),
        )
    )
    assert insufficient.structure_layer_analysis.status == "INSUFFICIENT_DATA"
    assert "not enough candles" in insufficient.structure_layer_analysis.sweep.reason.lower()

    neutral = engine.analyze(
        _engine_input(
            structure_analysis_required=True,
            structure_candles=_flat_candles(),
        )
    )
    assert neutral.structure_layer_analysis.status == "ANALYZED_NO_SHIFT"
    assert neutral.structure_layer_analysis.direction == "N/A"
    assert neutral.structure_layer_analysis.structure_shift.is_present is False


def test_structure_analyzer_runs_once_per_engine_call_and_uses_custom_settings(monkeypatch) -> None:
    calls: list[tuple[int, int, int]] = []
    original = strategy_module._analyze_structure_layer

    def spy(data, structure, *, atr_period, lookback):
        calls.append((atr_period, lookback, len(structure.candles) if structure is not None else 0))
        return original(data, structure, atr_period=atr_period, lookback=lookback)

    monkeypatch.setattr(strategy_module, "_analyze_structure_layer", spy)

    result = LiquidityGrabEngine(atr_period=5, swing_lookback=3).analyze(
        _engine_input(
            structure_analysis_required=True,
            structure_candles=_structure_shift_candles(),
        )
    )

    assert calls == [(5, 3, 45)]
    assert result.challenge.structure_layer_analysis == result.swing.structure_layer_analysis
    assert result.swing.structure_layer_analysis == result.scalp.structure_layer_analysis


def test_opposing_neutral_and_missing_structure_results_do_not_change_qualification(monkeypatch) -> None:
    baseline = LiquidityGrabEngine().analyze(_engine_input())
    baseline_projection = {
        mode: _qualification_projection(getattr(baseline, mode))
        for mode in ("challenge", "swing", "scalp")
    }

    def diagnostic(status: str, direction: str) -> StructureLayerAnalysis:
        return StructureLayerAnalysis(
            timeframe="2h",
            candle_count=45,
            status=status,
            sweep=LiquiditySweepSignal(is_present=direction != "N/A", direction=direction),
            structure_shift=StructureShiftSignal(
                is_present=status == "ANALYZED_SHIFT_PRESENT",
                kind="BOS" if status == "ANALYZED_SHIFT_PRESENT" else "N/A",
                direction=direction,
            ),
            direction=direction,
            reason="Informational test diagnostic.",
        )

    for status, direction in (
        ("ANALYZED_SHIFT_PRESENT", "bearish"),
        ("ANALYZED_NO_SHIFT", "N/A"),
    ):
        monkeypatch.setattr(
            strategy_module,
            "_analyze_structure_layer",
            lambda *_args, _status=status, _direction=direction, **_kwargs: diagnostic(_status, _direction),
        )
        observed = LiquidityGrabEngine().analyze(
            _engine_input(
                structure_analysis_required=True,
                structure_candles=_structure_shift_candles(),
            )
        )
        for mode in ("challenge", "swing", "scalp"):
            assert _qualification_projection(getattr(observed, mode)) == baseline_projection[mode]

    monkeypatch.undo()
    missing = LiquidityGrabEngine().analyze(_engine_input(structure_analysis_required=True))
    for mode in ("challenge", "swing", "scalp"):
        assert _qualification_projection(getattr(missing, mode)) == baseline_projection[mode]
        assert "structure" not in getattr(missing, mode).gates_failed


def test_fetch_planner_deduplicates_2h_and_shared_m15_roles() -> None:
    base = ScannerRunConfig(
        symbols=["BTCUSDT"],
        exchange="binance",
        account_equity=Decimal("10000"),
        risk_per_trade_pct=Decimal("1"),
    )
    changed = base.model_copy(update={"structure_timeframe": "3h"})

    planned = _direct_strategy_timeframes(base)
    assert planned.count("2h") == 1
    assert planned.count("15m") == 1
    assert set(planned) ^ set(_direct_strategy_timeframes(changed)) == {"2h", "3h"}


def test_audit_requires_execution_evidence_not_configuration_only() -> None:
    timeframe_row = {
        "timeframes_json": json.dumps(
            {
                "context": "2d",
                "bias": "12h",
                "structure": "2h",
                "execution": "15m",
                "confirmation": "15m",
            }
        )
    }
    execution_row = {
        "raw_result_json": json.dumps(
            {
                "strategy_diagnostics": {
                    "swing": {
                        "structure_layer_analysis": {
                            "timeframe": "2h",
                            "status": "ANALYZED_NO_SHIFT",
                        }
                    }
                }
            }
        )
    }
    missing_row = {
        "raw_result_json": json.dumps(
            {
                "strategy_diagnostics": {
                    "swing": {
                        "structure_layer_analysis": {
                            "timeframe": "2h",
                            "status": "MISSING_DATA",
                        }
                    }
                }
            }
        )
    }

    configuration_only = _timeframe_verification([timeframe_row], Counter(), None)
    missing = _timeframe_verification([timeframe_row], Counter(), None, symbol_rows=[missing_row])
    executed = _timeframe_verification([timeframe_row], Counter(), None, symbol_rows=[execution_row])
    historical = _timeframe_verification([{"timeframes_json": json.dumps({})}], Counter(), None)
    unrelated = _timeframe_verification(
        [{"timeframes_json": json.dumps({"unrelated_timeframe": "2h"})}],
        Counter(),
        None,
    )

    assert configuration_only["timeframes"]["2H_structure"]["status"] == "NOT_VERIFIABLE"
    assert missing["timeframes"]["2H_structure"]["status"] == "NOT_VERIFIABLE"
    assert executed["timeframes"]["2H_structure"]["status"] == "ACTIVE_AND_VERIFIED"
    assert executed["timeframes"]["2H_structure"]["execution_evidence"]["verified_analysis_records"] == 1
    assert historical["timeframes"]["2H_structure"]["status"] == "NOT_VERIFIABLE"
    assert unrelated["timeframes"]["2H_structure"]["status"] == "NOT_VERIFIABLE"

def test_audit_compaction_retains_only_bounded_structure_execution_evidence() -> None:
    limits = QueryLimits(
        max_rows_per_source=10,
        truncated_sources=set(),
        optional_json_unavailable_sources=set(),
        malformed=Counter(),
        observed_rows_by_source={},
    )
    row = {
        "raw_result_json": json.dumps(
            {
                "strategy_diagnostics": {
                    "swing": {
                        "structure_layer_analysis": {
                            "timeframe": "2h",
                            "status": "ANALYZED_NO_SHIFT",
                        },
                        "large_unrelated_payload": ["ignored"] * 10,
                    },
                    "unexpected_mode": {
                        "structure_layer_analysis": {
                            "timeframe": "2h",
                            "status": "ANALYZED_SHIFT_PRESENT",
                        }
                    },
                },
                "unrelated_large_payload": ["ignored"] * 10,
            }
        )
    }

    compacted = _compact_query_row(row, "symbol_results", limits)
    assert json.loads(compacted["raw_result_json"]) == {
        "strategy_diagnostics": {
            "swing": {
                "structure_layer_analysis": {
                    "timeframe": "2h",
                    "status": "ANALYZED_NO_SHIFT",
                }
            }
        }
    }
