"""Offline proof of telemetry across the production scan/report boundaries."""
from __future__ import annotations

import asyncio
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from app.analytics.selective_fibonacci import SELECTIVE_VERSION
from app.command_center import build_command_center_payload
from app.core.process_memory import ProcessMemoryReading
from app.formatters.scanner_display import rank_scan_results
from app.microstructure.liquidation_service import LiquidationFlowService
from app.microstructure.order_book_service import OrderBookLiquidityService
from app.microstructure.service import MicrostructureFlowService
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerProcessMemoryStats,
    ScannerRunResult,
    ScannerRunner,
    ScannerRuntimeStats,
    ScannerSymbolResult,
)
from app.storage.database import SCHEMA_VERSION, identify_schema_version, open_read_only_database
from app.storage.repositories import store_scan_result
from app.storage.scan_payloads import load_logical_scan_payload
from app.telegram_admin.wolf_briefing import load_latest_db_scan_artifacts
from scripts import run_scan
from tests.runtime_epoch_support import (
    SYNTHETIC_IDENTITY,
    bootstrap_operational_test_database,
    register_synthetic_scan_run,
)
from tests.test_integrated_signal_product import AT
from tests.test_production_repair_derivatives import NativeMarket
from tests.test_scanner_runner import _config


pytestmark = pytest.mark.no_auto_epoch
TELEMETRY_FIELDS = (
    "derivatives_prefetch", "stream_health", "analysis_worker", "cmc_acquisition",
)


@pytest.mark.parametrize("inline_raw_payload", [False, True], ids=["symbol_refs_v1", "inline_v1"])
def test_runner_telemetry_survives_combination_storage_hydration_and_reporting(
    tmp_path, monkeypatch, inline_raw_payload,
):
    async def forbidden_cmc_collection(*args, **kwargs):
        raise AssertionError("CMC must remain off")

    monkeypatch.setattr("app.cmc.runtime.begin_collection", forbidden_cmc_collection)

    async def scan():
        market = NativeMarket(Decimal(".786"), "long")
        flow = MicrostructureFlowService(clock=lambda: AT)
        book = OrderBookLiquidityService(clock=lambda: AT)
        liquidations = LiquidationFlowService(clock=lambda: AT)
        try:
            # Allocate collectors without starting services, sockets or snapshot requests.
            for service in (flow, book, liquidations):
                await service.reconcile_symbols(["BTCUSDT"])
            await market.get_instrument_metadata("BTCUSDT", capture_clock=lambda: AT)
            return await ScannerRunner(
                exchange_client=market,
                microstructure_flow_service=flow,
                order_book_liquidity_service=book,
                liquidation_flow_service=liquidations,
                clock=lambda: AT + timedelta(seconds=1),
                capture_clock=lambda: AT,
                process_memory_sampler=lambda: ProcessMemoryReading(123_000_000, "synthetic:rss"),
            ).run(_config(
                ["BTCUSDT"], strategy_version=SELECTIVE_VERSION,
                htf_timeframe="1d", structure_timeframe="4h",
                global_context_enabled=False, decision_timestamp=None, cmc_mode="off",
            ))
        finally:
            await book.snapshot_client.aclose()
            await market.http.aclose()

    scanned = asyncio.run(scan())
    expected = scanned.runtime_stats.model_dump(mode="json")
    assert all(expected[field] for field in TELEMETRY_FIELDS)
    assert expected["derivatives_prefetch"]["acquired"] == 1
    assert set(expected["stream_health"]) == {"aggregate_flow", "order_book", "liquidations"}
    assert expected["stream_health"]["aggregate_flow"]["per_symbol"]["BTCUSDT"]["source_event_age_seconds"] is None
    assert expected["analysis_worker"]["concurrency_bound"] == 1
    assert expected["cmc_acquisition"]["mode"] == "off"
    assert expected["cmc_acquisition"]["collector_available"] is False
    assert expected["cmc_acquisition"]["last_acquisition"] is None

    combined = run_scan._combined_run_result(
        config=scanned.config,
        watchlist_symbols=("BTCUSDT",),
        results_by_symbol={item.symbol: item for item in scanned.results},
        cache=None,
        retry_diagnostics=scanned.retry_diagnostics,
        resume_metadata={"run_id": "synthetic-telemetry"},
        runtime_stats=scanned.runtime_stats,
        market_regime=scanned.market_regime,
        global_context=scanned.global_context,
    )
    for field in TELEMETRY_FIELDS:
        assert getattr(combined.runtime_stats, field) == expected[field]
    assert combined.results == scanned.results
    assert combined.runtime_stats.process_memory == scanned.runtime_stats.process_memory

    database = bootstrap_operational_test_database(tmp_path / "synthetic-telemetry.db")
    register_synthetic_scan_run(database, "synthetic-telemetry")
    ranked = rank_scan_results(combined.results)
    store_scan_result(
        database, combined, run_id="synthetic-telemetry",
        ranked_results=ranked, raw_payload=run_scan._json_payload(combined, ranked_results=ranked),
        inline_raw_payload=inline_raw_payload, expected_identity=SYNTHETIC_IDENTITY,
    )
    with open_read_only_database(database) as connection:
        row = connection.execute(
            "SELECT runtime_stats_json, raw_payload_format FROM scan_runs WHERE run_id = ?",
            ("synthetic-telemetry",),
        ).fetchone()
        assert row["raw_payload_format"] == ("inline_v1" if inline_raw_payload else "symbol_refs_v1")
        stored_stats = json.loads(row["runtime_stats_json"])
        logical = load_logical_scan_payload(connection, "synthetic-telemetry")
        assert identify_schema_version(connection) == SCHEMA_VERSION == 27

    hydrated = ScannerRunResult.model_validate(logical)
    artifacts = load_latest_db_scan_artifacts(project_root=tmp_path, database_path=database)
    assert artifacts.scan_payload is not None
    report = build_command_center_payload(hydrated)
    for field in TELEMETRY_FIELDS:
        assert stored_stats[field] == expected[field]
        assert logical["runtime_stats"][field] == expected[field]
        assert getattr(hydrated.runtime_stats, field) == expected[field]
        assert artifacts.scan_payload["runtime_stats"][field] == expected[field]
        assert report["runtime_metrics"][field] == expected[field]
    assert hydrated.model_dump(mode="json")["results"] == scanned.model_dump(mode="json")["results"]
    assert hydrated.runtime_stats.process_memory == scanned.runtime_stats.process_memory


@pytest.mark.parametrize("has_runtime_stats", [False, True])
def test_combined_stats_preserve_existing_accounting_and_missing_telemetry(has_runtime_stats):
    outcomes = ("evaluated", "rejected", "errored", "timed_out", "not_run")
    results = tuple(
        ScannerSymbolResult(
            symbol=f"S{index}USDT", status=ScannerPipelineStatus.SCANNED_NO_SETUP,
            status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
            iteration_outcome=outcome, runtime_seconds=seconds,
            not_run_reason="synthetic:budget_exhausted" if outcome == "not_run" else "N/A",
        )
        for index, (outcome, seconds) in enumerate(zip(outcomes, (1.1114, 2.2224, 3.3334, 4.4444, None)))
    )
    source = ScannerRuntimeStats(
        total_runtime_seconds=50.1236, global_timeout_hit=True,
        completed_symbols=99, outcome_counts={"evaluated": 99},
        process_memory=ScannerProcessMemoryStats(
            measurement_status="Unverified", source="synthetic:rss", samples_attempted=1,
            samples_failed=1, failure_codes=("synthetic:unavailable",),
        ),
    ) if has_runtime_stats else None
    combined = run_scan._combined_runtime_stats(results, total_symbols=7, runtime_stats=source)
    assert combined.model_dump(exclude=set(TELEMETRY_FIELDS) | {"process_memory"}) == {
        "total_runtime_seconds": 50.124 if has_runtime_stats else 11.112,
        "average_seconds_per_symbol": 2.778,
        "slowest_symbol": "S3USDT", "slowest_symbol_seconds": 4.444,
        "timeout_count": 1, "completed_symbols": 2, "skipped_symbols": 1,
        "errored_symbols": 2, "skipped_errored_symbols": 3,
        "global_timeout_hit": has_runtime_stats, "queued_symbols": 7,
        "evaluated_symbols": 1, "rejected_symbols": 1, "timed_out_symbols": 1,
        "not_run_symbols": 1, "outcome_counts": {outcome: 1 for outcome in outcomes},
    }
    assert combined.process_memory == (source.process_memory if source else ScannerProcessMemoryStats())
    assert all(getattr(combined, field) == {} for field in TELEMETRY_FIELDS)
