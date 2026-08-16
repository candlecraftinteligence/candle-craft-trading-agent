from __future__ import annotations

from decimal import Decimal
from io import StringIO
from types import SimpleNamespace

from app.agents.trade_idea import TradeIdeaAgent
from app.analytics.setup_quality import validate_setup_quality
from app.formatters.scanner_console import ScannerConsolePresenter, format_watch_iteration_console
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerSymbolResult
from app.watch_mode import WatchActivation, WatchIterationSummary


def _summary(**updates) -> WatchIterationSummary:
    values = {
        "iteration": 3,
        "scanned_at": "2026-07-26T14:31:20+00:00",
        "symbols_watched": 1,
        "valid_activations": 0,
        "still_watching": 0,
        "rejected_no_edge": 1,
        "data_issues": 0,
        "status": "SUCCESS",
        "scheduled_start": "2026-07-26T14:31:20+00:00",
        "actual_start": "2026-07-26T14:31:20+00:00",
        "finished_at": "2026-07-26T14:31:26+00:00",
        "duration_seconds": 6,
        "sleep_seconds": 294,
        "next_scheduled_attempt": "2026-07-26T14:36:20+00:00",
        "next_scan_seconds": 294,
        "queue_total": 1,
        "outcome_counts": {"evaluated": 0, "rejected": 1, "errored": 0, "timed_out": 0, "not_run": 0},
        "phase_statuses": {
            "universe": "SUCCESS",
            "queue": "SUCCESS",
            "scanner": "SUCCESS",
            "lifecycle": "SUCCESS",
            "telegram_outbox": "SUCCESS",
            "symbol_health": "SUCCESS",
            "watch_state": "SUCCESS",
            "scan_storage": "SKIPPED",
        },
        "database_storage_status": "NOT_REQUESTED",
    }
    values.update(updates)
    return WatchIterationSummary(**values)


def _rejected(symbol: str) -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "execution_sweep_status": "failed",
                "confirmation_structure_shift_status": "not_evaluated",
                "first_failed_gate": "missing_confirmed_sweep",
                "gates_failed": ("missing_confirmed_sweep",),
            }
        },
        setup_quality=validate_setup_quality(
            {
                "symbol": symbol,
                "setup_valid": False,
                "first_failed_gate": "missing_confirmed_sweep",
                "gates_failed": ("missing_confirmed_sweep",),
            }
        ),
    )


def _history_warning(symbol: str, history: str) -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        missing_data=(f"Insufficient closed 2D history: {history}",),
    )


def _valid_result(symbol: str = "BTCUSDT") -> ScannerSymbolResult:
    idea = TradeIdeaAgent().create(
        {
            "symbol": symbol,
            "exchange": "binance",
            "market_type": "perpetual_futures",
            "direction": "long",
            "timeframe": "15m",
            "setup_type": "liquidity_grab_pullback_swing",
            "entry_low": Decimal("100"),
            "entry_high": Decimal("102"),
            "stop_loss": Decimal("95"),
            "take_profit_targets": (Decimal("112"), Decimal("120")),
            "invalidation": "Invalid below 95.",
            "opportunity_score": Decimal("88"),
            "opportunity_grade": "A",
            "opportunity_decision": "high_quality_candidate",
            "risk_approved": True,
            "best_rr": Decimal("3.2"),
            "technical_summary": "Bullish sweep, confirmation, and pullback.",
            "derivatives_summary": "Derivatives support the long.",
            "cancel_condition": "Cancel if price closes below 95.",
        }
    )
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        status_history=(
            ScannerPipelineStatus.IDEA_CREATED,
            ScannerPipelineStatus.ALERT_DRY_RUN_CREATED,
            ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        ),
        trade_idea=idea,
        valid_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "is_valid": True,
                "mode": "swing",
                "bias": "long",
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "rr_to_tp2": Decimal("3.2"),
                "gates_passed": ("sweep", "bos_choch", "pullback_zone", "rr", "trust_meter"),
                "derivatives_supports_trade": True,
            }
        },
        setup_quality=validate_setup_quality(
            {
                "symbol": symbol,
                "setup_valid": True,
                "mode": "swing",
                "bias": "long",
                "rr_to_tp2": Decimal("3.2"),
                "best_rr": Decimal("3.2"),
                "sweep_passed": True,
                "confirmation_passed": True,
                "pullback_valid": True,
                "ob_or_fvg_valid": True,
                "fib_valid": True,
                "htf_2d_trend": "bullish",
                "mtf_12h_trend": "bullish",
                "trust_percentage": 90,
                "poc_available": True,
                "value_area_available": True,
                "derivatives_supports_trade": True,
                "derivatives_score": 88,
                "funding_status": "normal",
                "crowding_risk": "low",
                "risk_approved": True,
                "data_quality_score": Decimal("95"),
            }
        ),
    )


def test_compact_hides_raw_mappings_and_summarizes_ordinary_rejections() -> None:
    results = tuple(_rejected(f"SYM{index:03d}USDT") for index in range(100))
    output = format_watch_iteration_console(
        _summary(symbols_watched=100, queue_total=100, rejected_no_edge=100, outcome_counts={"evaluated": 0, "rejected": 100, "errored": 0, "timed_out": 0, "not_run": 0}),
        results=results,
    )

    assert "Phases: {" not in output
    assert "Symbol outcomes: {" not in output
    assert "No setup: 100" in output
    assert "SYM000USDT" not in output
    assert "No valid setup this iteration." in output


def test_compact_groups_repeated_candle_history_warnings_with_limited_examples() -> None:
    symbols = ("REUSDT", "CAPUSDT", "HSKUSDT", "KROUSDT", "MOREUSDT", "LASTUSDT", "ONEUSDT", "TWOUSDT", "THREEUSDT")
    results = tuple(_history_warning(symbol, f"{index + 8}/20") for index, symbol in enumerate(symbols))
    output = format_watch_iteration_console(
        _summary(
            symbols_watched=len(symbols),
            queue_total=len(symbols),
            rejected_no_edge=0,
            data_issues=len(symbols),
            outcome_counts={"evaluated": 0, "rejected": len(symbols), "errored": 0, "timed_out": 0, "not_run": 0},
        ),
        results=results,
    )

    assert "Insufficient closed 2D history: 9 symbols" in output
    assert "REUSDT 8/20" in output
    assert "TWOUSDT" in output
    assert "THREEUSDT" not in output
    assert "...and 1 more" in output


def test_compact_distinguishes_partial_and_failed_iterations() -> None:
    partial = format_watch_iteration_console(
        _summary(status="PARTIAL", phase_statuses={"scanner": "PARTIAL"}),
        results=(_rejected("BTCUSDT"),),
    )
    failed = format_watch_iteration_console(
        _summary(status="FAILED", phase_statuses={"iteration": "FAILED"}, next_scan_seconds=5),
    )

    assert "Status: PARTIAL" in partial
    assert "[WARN] Scanner" in partial
    assert "Status: FAILED" in failed
    assert "[FAIL] Iteration" in failed
    assert "will retry" in failed


def test_compact_keeps_valid_candidate_and_delivery_visible() -> None:
    result = _valid_result()
    summary = _summary(
        valid_activations=1,
        rejected_no_edge=0,
        outcome_counts={"evaluated": 1, "rejected": 0, "errored": 0, "timed_out": 0, "not_run": 0},
        alerts=(WatchActivation(symbol="BTCUSDT", mode="swing", message="safe test", delivery_status="dry_run", delivery_detail="Dry run."),),
    )
    output = format_watch_iteration_console(summary, results=(result,))

    assert "Symbol  | Mode" in output
    assert "Telegram" in output
    assert "BTCUSDT" in output
    assert "DRY_RUN" in output
    assert "3.2" in output


def test_redirected_no_color_narrow_and_verbose_output_are_readable(monkeypatch) -> None:
    result = _rejected("BTCUSDT")
    summary = _summary(symbol_outcomes={"BTCUSDT": {"outcome": "rejected", "reason_code": "technical", "status": "scanned_no_setup"}})
    monkeypatch.setenv("NO_COLOR", "1")
    stream = StringIO()
    presenter = ScannerConsolePresenter(mode="compact", stream=stream, width=48, color=True)
    compact = presenter.format_watch_iteration(summary, results=(result,))
    verbose = format_watch_iteration_console(summary, mode="verbose", results=(result,), width=80, stream=StringIO())

    assert "\x1b[" not in compact
    assert all(len(line) <= 48 for line in compact.splitlines())
    assert "SYMBOL OUTCOMES" in verbose
    assert "BTCUSDT: outcome=rejected" in verbose


def test_compact_uses_canonical_lifecycle_fields_and_shows_exact_telegram_blocker() -> None:
    result = _valid_result("NEARUSDT").model_copy(
        update={
            "trade_idea": None,
            "technical_score": 97,
            "lifecycle_state": SetupLifecycleRecord(
                lifecycle_id="near-lifecycle",
                symbol="NEARUSDT",
                mode="scalp",
                direction="long",
                current_state=SetupLifecycleState.STALKING,
                first_seen_at="2026-08-16T08:00:00+00:00",
                last_seen_at="2026-08-16T08:21:00+00:00",
                last_transition_at="2026-08-16T08:00:00+00:00",
                quality_score=94,
                quality_grade_current="A+",
                rr="3.4",
            ),
        }
    )
    delivery = SimpleNamespace(
        symbol="NEARUSDT",
        status="blocked_repeat",
        alert_type="WATCHLIST",
        error_message="public_block_same_symbol_same_side_cooldown",
        detail="Repeated blocked Telegram alert attempt compacted.",
    )
    summary = _summary(
        iteration=1,
        active_lifecycle_count=1,
        telegram_outbox_status={"sent": 0, "blocked_repeat": 1},
    )

    output = format_watch_iteration_console(
        summary,
        results=(result,),
        telegram_deliveries=(delivery,),
        width=160,
    )

    assert "Q/T/O" in output
    assert "LONG" in output
    assert "3.4" in output
    assert "public_block_same_symbol_same_side_cooldown" in output
    assert "Blocked retries compacted: 1" in output
    assert "Iteration 1 is process-local" in output
    assert "One high score alone is not Telegram eligibility" in output
