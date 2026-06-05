from __future__ import annotations

from decimal import Decimal

from app.analytics.pullback_intelligence import (
    AcceptanceStatus,
    PullbackFailureType,
    PullbackQualityGrade,
    build_pullback_intelligence,
)
from app.analytics.pullback_zones import BASE_MIN_RR, CHALLENGE_MIN_RR
from app.formatters.scanner_display import format_symbol_card
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from scripts import run_scan


def _diagnostics(**overrides):
    data = {
        "mode": "swing",
        "bias": "long",
        "execution_sweep_status": "passed",
        "confirmation_structure_shift_status": "passed",
        "pullback_zone_status": "failed",
        "pullback_calculation_timeframe": "5m",
        "pullback_bos_choch_candle_index": 90,
        "candles_5m_count": 100,
        "gates_passed": ("sweep", "bos_choch"),
        "gates_failed": (),
        "required_rr": Decimal("2.5"),
    }
    data.update(overrides)
    return data


def _scanner_result(symbol_result: ScannerSymbolResult) -> ScannerRunResult:
    return ScannerRunResult(
        config=ScannerRunConfig(
            symbols=(symbol_result.symbol,),
            exchange="binance",
            account_equity=Decimal("1000"),
            risk_per_trade_pct=Decimal("1"),
        ),
        results=(symbol_result,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )


def _pullback_symbol(**diagnostics_overrides) -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol="PBUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={"swing": _diagnostics(**diagnostics_overrides)},
    )


def test_too_deep_classification_marks_fresh_structure_required() -> None:
    intelligence = build_pullback_intelligence(
        _diagnostics(
            first_failed_gate="pullback_too_deep",
            gates_failed=("pullback_too_deep",),
            pullback_depth_ratio=Decimal("0.82"),
            fib_alignment_status="pullback_too_deep",
            pullback_failure_reason="Pullback tagged beyond 0.786 before entry.",
        )
    )

    assert intelligence.pullback_failure_type == PullbackFailureType.TOO_DEEP
    assert intelligence.pullback_quality_grade == PullbackQualityGrade.REJECT
    assert intelligence.projection.can_reactivate_same_structure is False
    assert intelligence.projection.fresh_lifecycle_required is True
    assert "intent weakened" in intelligence.explanation


def test_no_ob_fvg_classification_stays_watch_only() -> None:
    intelligence = build_pullback_intelligence(
        _diagnostics(
            first_failed_gate="no_ob_or_fvg_zone",
            gates_failed=("no_ob_or_fvg_zone",),
            pullback_depth_ratio=Decimal("0.50"),
            fib_alignment_status="aligned",
            selected_zone_type="N/A",
            pullback_failure_reason="No valid OB or FVG was found inside the 5m displacement impulse.",
        )
    )

    assert intelligence.pullback_failure_type == PullbackFailureType.NO_OB_FVG
    assert intelligence.pullback_quality_grade == PullbackQualityGrade.C
    assert intelligence.ob_fvg_status == "missing"
    assert intelligence.projection.lifecycle_action == "WATCHLIST"
    assert "valid OB/FVG" in intelligence.next_pullback_condition


def test_rr_compression_classification_does_not_confirm_setup() -> None:
    intelligence = build_pullback_intelligence(
        _diagnostics(
            first_failed_gate="rr_below_minimum",
            gates_passed=("sweep", "bos_choch", "pullback_zone"),
            gates_failed=("rr_below_minimum",),
            pullback_zone_status="valid",
            pullback_depth_ratio=Decimal("0.48"),
            fib_alignment_status="aligned",
            selected_zone_type="OB",
            rr_to_tp2=Decimal("1.9"),
        )
    )

    assert intelligence.pullback_failure_type == PullbackFailureType.RR_COMPRESSION
    assert intelligence.pullback_quality_grade == PullbackQualityGrade.C
    assert intelligence.projection.lifecycle_action == "DO_NOT_CONFIRM"
    assert "better entry" in intelligence.next_pullback_condition


def test_data_incomplete_handling_returns_na_grade() -> None:
    intelligence = build_pullback_intelligence(
        _diagnostics(
            first_failed_gate="missing_confirmation_candles",
            gates_failed=("missing_confirmation_candles",),
            missing_data=("candles_5m: N/A",),
        )
    )

    assert intelligence.pullback_failure_type == PullbackFailureType.DATA_INCOMPLETE
    assert intelligence.pullback_quality_grade == PullbackQualityGrade.NA
    assert intelligence.pullback_depth_ratio == "N/A"
    assert intelligence.projection.lifecycle_action == "DATA_WAIT"


def test_weak_wick_reclaim_classification_stays_watch_only() -> None:
    intelligence = build_pullback_intelligence(
        _diagnostics(
            first_failed_gate="wick_sweep_reclaim",
            gates_failed=("wick_sweep_reclaim",),
            pullback_depth_ratio=Decimal("0.82"),
            wick_depth_ratio=Decimal("0.82"),
            close_depth_ratio=Decimal("0.76"),
            body_acceptance_ratio=Decimal("0.76"),
            max_wick_breach=Decimal("0.034"),
            max_body_breach=Decimal("0"),
            reclaim_detected=True,
            reclaim_strength="weak",
            candles_below_fib_zone=0,
            acceptance_status=AcceptanceStatus.WICK_SWEEP_RECLAIM.value,
            structural_reclaim_status="intact",
        )
    )

    assert intelligence.acceptance_status == AcceptanceStatus.WICK_SWEEP_RECLAIM.value
    assert intelligence.pullback_failure_type == "N/A"
    assert intelligence.pullback_quality_grade == PullbackQualityGrade.C
    assert intelligence.projection.lifecycle_action == "WATCHLIST"
    assert "strong reclaim" in intelligence.next_pullback_condition


def test_strong_deep_reclaim_classification_tracks_without_gate_bypass() -> None:
    intelligence = build_pullback_intelligence(
        _diagnostics(
            first_failed_gate="no_ob_or_fvg_zone",
            gates_failed=("no_ob_or_fvg_zone",),
            pullback_depth_ratio=Decimal("0.82"),
            wick_depth_ratio=Decimal("0.82"),
            close_depth_ratio=Decimal("0.64"),
            body_acceptance_ratio=Decimal("0.64"),
            max_wick_breach=Decimal("0.034"),
            max_body_breach=Decimal("0"),
            reclaim_detected=True,
            reclaim_strength="strong",
            candles_below_fib_zone=0,
            acceptance_status=AcceptanceStatus.DEEP_RECLAIM_VALID.value,
            structural_reclaim_status="intact",
        )
    )

    assert intelligence.acceptance_status == AcceptanceStatus.DEEP_RECLAIM_VALID.value
    assert intelligence.reclaim_strength == "strong"
    assert intelligence.pullback_failure_type == PullbackFailureType.NO_OB_FVG
    assert intelligence.projection.lifecycle_action == "WATCHLIST"
    assert "OB/FVG" in intelligence.next_pullback_condition


def test_display_output_includes_pullback_intelligence_block() -> None:
    text = format_symbol_card(
        _pullback_symbol(
            first_failed_gate="pullback_too_deep",
            gates_failed=("pullback_too_deep",),
            pullback_depth_ratio=Decimal("0.82"),
            wick_depth_ratio=Decimal("0.82"),
            close_depth_ratio=Decimal("0.82"),
            body_acceptance_ratio=Decimal("0.82"),
            reclaim_strength="N/A",
            candles_below_fib_zone=1,
            acceptance_status=AcceptanceStatus.BODY_ACCEPTANCE_FAILURE.value,
            structural_reclaim_status="intact",
            fib_alignment_status="pullback_too_deep",
            pullback_failure_reason="Pullback tagged beyond 0.786 before entry.",
        )
    )

    assert "Pullback Intelligence" in text
    assert "Failure type: TOO_DEEP" in text
    assert "Depth: 0.82" in text
    assert "Wick/Close Structure" in text
    assert "Wick depth: 0.82" in text
    assert "Acceptance: BODY_ACCEPTANCE_FAILURE" in text
    assert "Candles below zone: 1" in text
    assert "Next condition: fresh reclaim or new sweep + BOS required" in text


def test_json_output_contains_pullback_intelligence_object() -> None:
    symbol_result = _pullback_symbol(
        first_failed_gate="no_ob_or_fvg_zone",
        gates_failed=("no_ob_or_fvg_zone",),
        pullback_depth_ratio=Decimal("0.5"),
        wick_depth_ratio=Decimal("0.5"),
        close_depth_ratio=Decimal("0.5"),
        body_acceptance_ratio=Decimal("0.5"),
        reclaim_strength="N/A",
        candles_below_fib_zone=0,
        acceptance_status=AcceptanceStatus.CLEAN_PULLBACK.value,
        structural_reclaim_status="intact",
        fib_alignment_status="aligned",
        pullback_failure_reason="No valid OB or FVG was found inside the 5m displacement impulse.",
    )

    payload = run_scan._json_payload(_scanner_result(symbol_result))
    intelligence = payload["results"][0]["pullback_intelligence"]

    assert isinstance(intelligence, dict)
    assert intelligence["pullback_failure_type"] == "NO_OB_FVG"
    assert intelligence["ob_fvg_status"] == "missing"
    assert intelligence["wick_close_structure"]["acceptance_status"] == "CLEAN_PULLBACK"
    assert payload["results"][0]["acceptance_status"] == "CLEAN_PULLBACK"
    assert payload["results"][0]["body_acceptance_ratio"] == "0.5"


def test_pullback_intelligence_does_not_weaken_strategy_gates() -> None:
    intelligence = build_pullback_intelligence(
        _diagnostics(
            first_failed_gate="pullback_too_deep",
            gates_failed=("pullback_too_deep",),
            pullback_depth_ratio=Decimal("0.90"),
        )
    )

    assert BASE_MIN_RR == Decimal("2.5")
    assert CHALLENGE_MIN_RR == Decimal("2.7")
    assert intelligence.is_diagnostic_only is True
    assert intelligence.pullback_quality_grade == PullbackQualityGrade.REJECT


def test_rr_rules_unchanged_for_deep_reclaim() -> None:
    intelligence = build_pullback_intelligence(
        _diagnostics(
            first_failed_gate="rr_below_minimum",
            gates_passed=("sweep", "bos_choch", "pullback_zone"),
            gates_failed=("rr_below_minimum",),
            pullback_zone_status="valid",
            pullback_depth_ratio=Decimal("0.82"),
            wick_depth_ratio=Decimal("0.82"),
            close_depth_ratio=Decimal("0.64"),
            body_acceptance_ratio=Decimal("0.64"),
            reclaim_detected=True,
            reclaim_strength="strong",
            acceptance_status=AcceptanceStatus.DEEP_RECLAIM_VALID.value,
            structural_reclaim_status="intact",
            selected_zone_type="OB",
            rr_to_tp2=Decimal("1.9"),
        )
    )

    assert BASE_MIN_RR == Decimal("2.5")
    assert intelligence.pullback_failure_type == PullbackFailureType.RR_COMPRESSION
    assert intelligence.projection.lifecycle_action == "DO_NOT_CONFIRM"
