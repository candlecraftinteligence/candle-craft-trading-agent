from __future__ import annotations

import asyncio
import json
from decimal import Decimal

from app.analytics.symbol_health import (
    SymbolHealthRecord,
    build_symbol_priority_plan,
    calculate_next_health_score,
    cooldown_active,
    update_symbol_health_record,
)
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from app.research.queries import build_research_report
from app.research.reports import format_research_report
from app.storage.symbol_health import load_symbol_health_records, save_symbol_health_records
from scripts import run_scan


def _config(symbols: list[str]) -> ScannerRunConfig:
    return ScannerRunConfig.model_validate(
        {
            "symbols": symbols,
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )


def _symbol_result(
    symbol: str,
    *,
    timed_out: bool = False,
    data_issue: bool = False,
    near_miss: bool = False,
    runtime_seconds: float = 1.0,
) -> ScannerSymbolResult:
    if timed_out:
        return ScannerSymbolResult(
            symbol=symbol,
            status=ScannerPipelineStatus.SCAN_ERROR,
            status_history=(ScannerPipelineStatus.SCAN_ERROR,),
            error_message="symbol timeout exceeded after 0.2 seconds",
            rejection_reason="symbol timeout exceeded after 0.2 seconds",
            rejection_stage="scanner",
            rejection_reasons=("symbol timeout exceeded after 0.2 seconds",),
            runtime_seconds=runtime_seconds,
            timed_out=True,
            timeout_status="symbol_timeout",
        )
    if data_issue:
        return ScannerSymbolResult(
            symbol=symbol,
            status=ScannerPipelineStatus.SCAN_ERROR,
            status_history=(ScannerPipelineStatus.SCAN_ERROR,),
            error_message="not enough candles",
            rejection_reason="not enough candles",
            rejection_stage="scanner",
            rejection_reasons=("not enough candles",),
            missing_data=("candles_15m: N/A",),
            runtime_seconds=runtime_seconds,
        )
    if near_miss:
        return ScannerSymbolResult(
            symbol=symbol,
            status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
            rejected_strategy_modes=("swing",),
            runtime_seconds=runtime_seconds,
            strategy_diagnostics={
                "swing": {
                    "execution_sweep_status": "passed",
                    "confirmation_structure_shift_status": "passed",
                    "pullback_zone_status": "valid",
                    "first_failed_gate": "rr_below_minimum",
                    "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                    "gates_failed": ("rr_below_minimum",),
                    "rr_to_tp2": Decimal("1.8"),
                }
            },
        )
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        runtime_seconds=runtime_seconds,
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "execution_sweep_status": "failed",
                "confirmation_structure_shift_status": "not_evaluated",
                "first_failed_gate": "missing_confirmed_sweep",
                "gates_failed": ("missing_confirmed_sweep",),
            }
        },
    )


def test_health_score_calculation_rewards_stability_and_usefulness() -> None:
    stable = calculate_next_health_score(
        previous_score=70,
        timed_out=False,
        data_issue=False,
        useful=True,
        no_setup=False,
        readiness_label="HOT WATCH",
    )
    timeout = calculate_next_health_score(
        previous_score=70,
        timed_out=True,
        data_issue=False,
        useful=False,
        no_setup=False,
        timeout_strikes=2,
    )

    assert stable > 70
    assert timeout < 50


def test_timeout_strikes_trigger_temporary_cooldown() -> None:
    record = SymbolHealthRecord(symbol="SLOWUSDT", timeout_strikes=1, current_health_score=60)

    updated = update_symbol_health_record(
        record,
        _symbol_result("SLOWUSDT", timed_out=True, runtime_seconds=0.2),
        now="2026-05-19T00:00:00+00:00",
        cooldown_minutes=15,
        max_timeout_strikes=2,
    )

    assert updated.timeout_strikes == 2
    assert updated.timeout_count == 1
    assert updated.cooldown_until == "2026-05-19T00:15:00+00:00"
    assert updated.current_health_score < 60


def test_cooldown_expiry_allows_symbol_back_into_queue() -> None:
    future = SymbolHealthRecord(
        symbol="SLOWUSDT",
        current_health_score=30,
        cooldown_until="2026-05-19T00:30:00+00:00",
    )
    expired = future.model_copy(update={"cooldown_until": "2026-05-18T23:30:00+00:00"})

    future_plan = build_symbol_priority_plan(
        ("SLOWUSDT", "BTCUSDT"),
        {"SLOWUSDT": future},
        now="2026-05-19T00:00:00+00:00",
    )
    expired_plan = build_symbol_priority_plan(
        ("SLOWUSDT", "BTCUSDT"),
        {"SLOWUSDT": expired},
        now="2026-05-19T00:00:00+00:00",
    )

    assert future_plan.skipped_symbols == ("SLOWUSDT",)
    assert "SLOWUSDT" not in future_plan.symbols_to_scan
    assert "SLOWUSDT" in expired_plan.symbols_to_scan
    assert cooldown_active(expired.cooldown_until, "2026-05-19T00:00:00+00:00") is False


def test_adaptive_priority_orders_lifecycle_hot_health_then_liquidity() -> None:
    records = {
        "LOWUSDT": SymbolHealthRecord(symbol="LOWUSDT", current_health_score=95),
        "NEARUSDT": SymbolHealthRecord(symbol="NEARUSDT", current_health_score=55, last_readiness_label="HOT WATCH"),
        "OKUSDT": SymbolHealthRecord(symbol="OKUSDT", current_health_score=80),
    }

    plan = build_symbol_priority_plan(
        ("LOWUSDT", "NEARUSDT", "OKUSDT"),
        records,
        lifecycle_states={"LOWUSDT": "CONFIRMED"},
        now="2026-05-19T00:00:00+00:00",
    )

    assert plan.symbols_to_scan == ("LOWUSDT", "NEARUSDT", "OKUSDT")
    assert plan.priority_by_symbol()["LOWUSDT"].priority_rank == 1


def test_low_health_symbols_are_deprioritized_behind_healthier_liquidity_peer() -> None:
    records = {
        "LOWUSDT": SymbolHealthRecord(symbol="LOWUSDT", current_health_score=20, timeout_strikes=2),
        "OKUSDT": SymbolHealthRecord(symbol="OKUSDT", current_health_score=75),
    }

    plan = build_symbol_priority_plan(
        ("LOWUSDT", "OKUSDT"),
        records,
        now="2026-05-19T00:00:00+00:00",
    )

    assert plan.symbols_to_scan == ("OKUSDT", "LOWUSDT")


class HealthJsonScannerRunner:
    def __init__(self, *args, **kwargs) -> None:
        pass

    async def run(self, config, after_symbol=None, progress=None, resume_metadata=None):
        result = _symbol_result(config.symbols[0].symbol, near_miss=True, runtime_seconds=1.25)
        if after_symbol is not None:
            await after_symbol(result, 1, 1)
        return ScannerRunResult(
            config=config,
            results=(result,),
            scanned_symbols=1,
            failed_symbols=0,
            trade_ideas_created=0,
            dry_run_alerts_created=0,
            journal_entries_created=0,
            resume_metadata=dict(resume_metadata or {}),
        )


def test_json_output_includes_symbol_health(tmp_path, monkeypatch) -> None:
    output_path = tmp_path / "scan.json"
    db_path = tmp_path / "health.db"
    monkeypatch.setattr(run_scan, "ScannerRunner", HealthJsonScannerRunner)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "--adaptive-symbol-priority",
                "--database-path",
                str(db_path),
                "--output-json",
                str(output_path),
            ]
        )
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["symbol_health"]["enabled"] is True
    assert payload["symbol_health"]["prioritized_symbols"] == 1
    assert payload["symbol_health"]["priority_symbols"][0]["symbol"] == "BTCUSDT"
    assert payload["symbol_health"]["records"]["BTCUSDT"]["successful_scans"] == 1


def test_research_queries_include_symbol_health_output(tmp_path) -> None:
    db_path = tmp_path / "health.db"
    save_symbol_health_records(
        db_path,
        {
            "SLOWUSDT": SymbolHealthRecord(
                symbol="SLOWUSDT",
                timeout_count=3,
                timeout_strikes=2,
                average_runtime_sec=12.5,
                current_health_score=25,
                last_priority_rank=9,
            ),
            "FASTUSDT": SymbolHealthRecord(
                symbol="FASTUSDT",
                successful_scans=4,
                average_runtime_sec=0.5,
                current_health_score=90,
                last_priority_rank=1,
            ),
        },
    )

    health = build_research_report(db_path, query="symbol_health")
    slow = build_research_report(db_path, query="slow_symbols")
    timeouts = build_research_report(db_path, query="timeout_symbols")
    priority = build_research_report(db_path, query="priority_symbols")

    assert health["symbols"][0]["symbol"] == "FASTUSDT"
    assert slow["symbols"][0]["symbol"] == "SLOWUSDT"
    assert timeouts["symbols"][0]["symbol"] == "SLOWUSDT"
    assert priority["symbols"][0]["symbol"] == "FASTUSDT"
    assert "Symbol Health" in format_research_report(health)


def test_scanner_priority_works_when_no_health_data_exists(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "empty.db"
    output_path = tmp_path / "scan.json"
    monkeypatch.setattr(run_scan, "ScannerRunner", HealthJsonScannerRunner)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "--adaptive-symbol-priority",
                "--database-path",
                str(db_path),
                "--output-json",
                str(output_path),
            ]
        )
    )

    records = load_symbol_health_records(db_path)
    assert "BTCUSDT" in records
    assert records["BTCUSDT"].successful_scans == 1
