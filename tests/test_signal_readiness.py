from __future__ import annotations

from decimal import Decimal

from app.data.dtos import NA
from app.formatters.scanner_display import build_symbol_display, display_fields, rank_scan_results
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerSymbolResult


def _result(
    symbol: str,
    diagnostics: dict[str, object],
    *,
    status: ScannerPipelineStatus = ScannerPipelineStatus.SCANNED_NO_SETUP,
    status_history: tuple[ScannerPipelineStatus, ...] | None = None,
    rejected_modes: tuple[str, ...] = ("swing",),
    valid_modes: tuple[str, ...] = (),
    error_message: str | None = None,
) -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=status,
        status_history=status_history or (status,),
        error_message=error_message,
        strategy_diagnostics={"swing": diagnostics},
        rejected_strategy_modes=rejected_modes,
        valid_strategy_modes=valid_modes,
        technical_score=80,
        derivatives_score=80,
        poc=Decimal("100"),
        value_area_high=Decimal("105"),
        value_area_low=Decimal("95"),
    )


def _base_diagnostics(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = {
        "bias": "long",
        "htf_2d_trend": "bullish",
        "mtf_12h_trend": "bullish",
        "execution_timeframe": "15m",
        "confirmation_timeframe": "5m",
        "execution_sweep_status": "passed",
        "confirmation_structure_shift_status": "passed",
        "pullback_zone_status": "valid",
        "rr_to_tp2": Decimal("3.0"),
        "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
        "derivatives_supports_trade": True,
        "derivatives_conflict_reason": NA,
    }
    data.update(overrides)
    return data


def _valid_setup() -> ScannerSymbolResult:
    return _result(
        "VALIDUSDT",
        _base_diagnostics(is_valid=True, gates_passed=("sweep", "bos_choch", "pullback_zone", "rr")),
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        rejected_modes=(),
        valid_modes=("swing",),
    )


def _hot_watch() -> ScannerSymbolResult:
    return _result(
        "HOTUSDT",
        _base_diagnostics(
            rr_to_tp2=Decimal("2.3"),
            first_failed_gate="rr_below_minimum",
            gates_failed=("rr_below_minimum",),
            rr_diagnostics="failed: RR to TP2 2.3 is below 2.5.",
        ),
    )


def _normal_watch() -> ScannerSymbolResult:
    return _result(
        "WATCHUSDT",
        _base_diagnostics(
            pullback_zone_status="failed",
            rr_to_tp2=NA,
            first_failed_gate="no_ob_or_fvg_zone",
            gates_passed=("sweep", "bos_choch"),
            gates_failed=("no_ob_or_fvg_zone",),
            pullback_failure_reason="No valid OB or FVG was found inside the displacement impulse.",
        ),
    )


def _missing_sweep() -> ScannerSymbolResult:
    return _result(
        "REJECTUSDT",
        _base_diagnostics(
            execution_sweep_status="failed",
            confirmation_structure_shift_status="not_evaluated",
            pullback_zone_status=NA,
            rr_to_tp2=NA,
            first_failed_gate="missing_confirmed_sweep",
            gates_passed=(),
            gates_failed=("missing_confirmed_sweep",),
        ),
    )


def _data_issue() -> ScannerSymbolResult:
    return _result(
        "DATAUSDT",
        _base_diagnostics(
            confirmation_structure_shift_status="failed",
            pullback_zone_status=NA,
            rr_to_tp2=NA,
            first_failed_gate="missing_confirmation_candles",
            gates_passed=("sweep",),
            gates_failed=("missing_confirmation_candles",),
        ),
    )


def test_valid_setup_ranks_above_hot_watch() -> None:
    ranked = rank_scan_results((_hot_watch(), _valid_setup()))

    assert [item.symbol_result.symbol for item in ranked] == ["VALIDUSDT", "HOTUSDT"]
    assert ranked[0].display.readiness_label == "VALID SETUP"
    assert ranked[1].display.readiness_label == "HOT WATCH"


def test_hot_watch_ranks_above_normal_watch() -> None:
    ranked = rank_scan_results((_normal_watch(), _hot_watch()))

    assert [item.symbol_result.symbol for item in ranked] == ["HOTUSDT", "WATCHUSDT"]
    assert ranked[0].display.readiness_label == "HOT WATCH"
    assert ranked[1].display.readiness_label == "WATCH"


def test_rejected_two_of_four_setup_does_not_become_hot_watch() -> None:
    display = build_symbol_display(_normal_watch())

    assert display.setup_progress_passed == 2
    assert display.readiness_label == "WATCH"
    assert display.readiness_label != "HOT WATCH"


def test_data_issue_is_ranked_separately() -> None:
    ranked = rank_scan_results((_missing_sweep(), _data_issue(), _normal_watch()))

    assert [item.display.readiness_label for item in ranked] == ["WATCH", "REJECTED", "DATA ISSUE"]
    assert ranked[-1].symbol_result.symbol == "DATAUSDT"


def test_next_trigger_text_is_deterministic() -> None:
    missing_confirmation = _result(
        "CONFIRMUSDT",
        _base_diagnostics(
            confirmation_structure_shift_status="failed",
            pullback_zone_status=NA,
            rr_to_tp2=NA,
            first_failed_gate="missing_confirmation_structure_shift",
            gates_passed=("sweep",),
            gates_failed=("missing_confirmation_structure_shift",),
        ),
    )
    derivatives_conflict = _result(
        "CONFLICTUSDT",
        _base_diagnostics(
            first_failed_gate="derivatives_conflict",
            gates_failed=("derivatives_conflict",),
            derivatives_supports_trade=False,
            derivatives_conflict_reason="Severe derivatives conflict against long.",
        ),
    )

    cases = (
        (_normal_watch(), "Wait for clean OB/FVG pullback"),
        (_hot_watch(), "Wait for RR expansion above minimum"),
        (missing_confirmation, "Wait for 5m BOS/CHoCH"),
        (_missing_sweep(), "Wait for new sweep"),
        (derivatives_conflict, "Avoid: derivatives conflict"),
        (_data_issue(), "Avoid: data unreliable"),
    )

    for symbol_result, expected in cases:
        assert build_symbol_display(symbol_result).next_trigger_needed == expected


def test_json_fields_include_readiness_metadata() -> None:
    fields = display_fields(_hot_watch(), display_rank=1)

    assert fields["readiness_score"] >= 0
    assert fields["readiness_score"] <= 100
    assert fields["readiness_label"] == "HOT WATCH"
    assert fields["next_trigger_needed"] == "Wait for RR expansion above minimum"
    assert fields["priority_rank_reason"].startswith("HOT WATCH: score ")


def test_readiness_does_not_change_strategy_gate_state() -> None:
    symbol_result = _missing_sweep()
    before_gate = symbol_result.strategy_diagnostics["swing"]["first_failed_gate"]

    display = build_symbol_display(symbol_result)

    assert symbol_result.strategy_diagnostics["swing"]["first_failed_gate"] == before_gate
    assert before_gate == "missing_confirmed_sweep"
    assert display.failed_gate == "missing_confirmed_sweep"
    assert symbol_result.trade_idea is None
