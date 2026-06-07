from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.data.dtos import NA
from app.agents.trade_idea import TradeIdeaAgent
from app.analytics.setup_quality import SetupQualityState, validate_setup_quality
from app.analytics.symbol_health import SymbolHealthRecord
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerRuntimeStats,
    ScannerSymbolResult,
)
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.storage.symbol_health import save_symbol_health_records
from app.universe.symbol_universe import BINANCE_USDT_PERP_TOP_VOLUME_MODE, SymbolUniverse
from app.watch_mode import (
    WatchSymbolState,
    WatchState,
    build_watch_iteration_summary,
    current_result_is_valid_activation,
    format_watch_activation_alert,
    load_symbols_from_run,
    save_watch_state,
    should_trigger_activation_alert,
    update_watch_state_for_result,
)
from scripts import run_scan


class SequenceWatchRunner:
    results_by_call: list[tuple[ScannerSymbolResult, ...]] = []
    configs: list[ScannerRunConfig] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def run(self, config, after_symbol=None, progress=None, resume_metadata=None):
        self.__class__.configs.append(config)
        index = min(len(self.__class__.configs) - 1, len(self.__class__.results_by_call) - 1)
        results = self.__class__.results_by_call[index] if self.__class__.results_by_call else ()
        if after_symbol is not None:
            for completed, symbol_result in enumerate(results, start=1):
                await after_symbol(symbol_result, completed, len(results))
        return ScannerRunResult(
            config=config,
            results=results,
            scanned_symbols=len(results),
            failed_symbols=0,
            trade_ideas_created=sum(1 for result in results if result.trade_idea is not None),
            dry_run_alerts_created=0,
            journal_entries_created=0,
            runtime_stats=ScannerRuntimeStats(
                total_runtime_seconds=round(0.25 * len(results), 3),
                average_seconds_per_symbol=0.25 if results else 0.0,
                completed_symbols=len(results),
            ),
        )


class EchoWatchRunner:
    configs: list[ScannerRunConfig] = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    async def run(self, config, after_symbol=None, progress=None, resume_metadata=None):
        self.__class__.configs.append(config)
        symbols = _config_symbols(config)
        results = tuple(_rejected_symbol(symbol) for symbol in symbols)
        if after_symbol is not None:
            for completed, symbol_result in enumerate(results, start=1):
                await after_symbol(symbol_result, completed, len(results))
        return ScannerRunResult(
            config=config,
            results=results,
            scanned_symbols=len(results),
            failed_symbols=0,
            trade_ideas_created=0,
            dry_run_alerts_created=0,
            journal_entries_created=0,
            runtime_stats=ScannerRuntimeStats(
                total_runtime_seconds=round(0.1 * len(results), 3),
                average_seconds_per_symbol=0.1 if results else 0.0,
                completed_symbols=len(results),
            ),
            resume_metadata=dict(resume_metadata or {}),
        )


def _config_symbols(config: ScannerRunConfig) -> tuple[str, ...]:
    return tuple(getattr(symbol, "symbol", str(symbol)) for symbol in config.symbols)


def _scanner_config(symbols: list[str]) -> ScannerRunConfig:
    return ScannerRunConfig.model_validate(
        {
            "symbols": symbols,
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )


def _trade_idea(symbol: str = "BTCUSDT"):
    return TradeIdeaAgent().create(
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
            "confirmed_facts": ("Sweep confirmed.",),
            "cancel_condition": "Cancel if price closes below 95.",
        }
    )


def _valid_symbol(symbol: str = "BTCUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        status_history=(
            ScannerPipelineStatus.IDEA_CREATED,
            ScannerPipelineStatus.ALERT_DRY_RUN_CREATED,
            ScannerPipelineStatus.JOURNAL_ENTRY_CREATED,
        ),
        trade_idea=_trade_idea(symbol),
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


def _watch_lifecycle_record(state: SetupLifecycleState = SetupLifecycleState.WATCHLISTED, **updates) -> SetupLifecycleRecord:
    data = {
        "lifecycle_id": f"life-{updates.get('symbol', 'WATCHUSDT')}",
        "symbol": updates.get("symbol", "WATCHUSDT"),
        "mode": "swing",
        "direction": "long",
        "current_state": state,
        "previous_state": None,
        "first_seen_at": "2026-06-07T00:00:00+00:00",
        "last_seen_at": "2026-06-07T00:00:00+00:00",
        "last_transition_at": "2026-06-07T00:00:00+00:00",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "115",
        "tp3": "120",
        "rr": "3",
        "quality_grade_current": "B+",
        "failed_gate": NA,
        "invalidation_reason": NA,
    }
    data.update(updates)
    return SetupLifecycleRecord.model_validate(data)


def _watch_symbol(symbol: str = "WATCHUSDT", **record_updates) -> ScannerSymbolResult:
    return _valid_symbol(symbol).model_copy(
        update={"lifecycle_state": _watch_lifecycle_record(symbol=symbol, **record_updates)}
    )


def _universe(symbols: tuple[str, ...], *, requested_size: int | None = None) -> SymbolUniverse:
    return SymbolUniverse(
        mode=BINANCE_USDT_PERP_TOP_VOLUME_MODE,
        requested_size=requested_size or len(symbols),
        resolved_symbols=symbols,
        excluded_symbols=(),
        source="test",
        generated_at="2026-06-07T00:00:00+00:00",
    )


async def _noop_admin_report(*args, **kwargs) -> None:
    return None


def _patch_watch_runtime(
    tmp_path,
    monkeypatch,
    *,
    symbols: tuple[str, ...],
    db_path=None,
):
    db = db_path or tmp_path / "queue.sqlite"
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", tmp_path / "watch_state.json")
    monkeypatch.setattr(run_scan, "LATEST_RUN_PATH", tmp_path / "latest_scan.json")
    monkeypatch.setattr(run_scan, "SCAN_RUN_MANIFEST_PATH", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(run_scan, "NIGHTLY_SCAN_HISTORY_PATH", tmp_path / "nightly_history.json")
    monkeypatch.setattr(run_scan, "_route_admin_report", _noop_admin_report)
    monkeypatch.setattr(run_scan, "ScannerRunner", EchoWatchRunner)
    EchoWatchRunner.configs = []

    async def resolve_universe(mode, *, universe_size, min_quote_volume):
        assert mode == BINANCE_USDT_PERP_TOP_VOLUME_MODE
        return _universe(symbols[:universe_size], requested_size=universe_size)

    monkeypatch.setattr(run_scan, "resolve_symbol_universe", resolve_universe)
    return db


def _watch_args(db_path, *, universe_size: int, max_symbols: int | None = None, extra_args=()) -> list[str]:
    args = [
        "--universe",
        BINANCE_USDT_PERP_TOP_VOLUME_MODE,
        "--universe-size",
        str(universe_size),
        "--watch",
        "--watch-max-iterations",
        "1",
        "--watch-interval-sec",
        "0.01",
        "--database-path",
        str(db_path),
        "--store-scan",
        "--diagnostics-level",
        "normal",
    ]
    if max_symbols is not None:
        args.extend(("--max-symbols", str(max_symbols)))
    args.extend(extra_args)
    return args


def _near_miss_symbol(symbol: str = "BTCUSDT") -> ScannerSymbolResult:
    return ScannerSymbolResult(
        symbol=symbol,
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejected_strategy_modes=("swing",),
        strategy_diagnostics={
            "swing": {
                "execution_sweep_status": "passed",
                "confirmation_structure_shift_status": "passed",
                "pullback_zone_status": "valid",
                "rr_to_tp2": Decimal("1.8"),
                "first_failed_gate": "rr_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": ("rr_below_minimum",),
                "derivatives_supports_trade": True,
            }
        },
        setup_quality=validate_setup_quality(
            {
                "symbol": symbol,
                "setup_valid": False,
                "mode": "swing",
                "bias": "long",
                "rr_to_tp2": Decimal("1.8"),
                "best_rr": Decimal("1.8"),
                "sweep_passed": True,
                "confirmation_passed": True,
                "pullback_valid": True,
                "first_failed_gate": "rr_below_minimum",
                "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
                "gates_failed": ("rr_below_minimum",),
            }
        ),
    )


def _rejected_symbol(symbol: str = "BTCUSDT") -> ScannerSymbolResult:
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


def _prior_state(symbol: str = "BTCUSDT", *, status: str = "near_miss", readiness: str = "WATCH") -> WatchState:
    return WatchState(
        symbols={
            symbol: WatchSymbolState(
                symbol=symbol,
                last_status=status,
                last_failed_gate="rr_below_minimum",
                readiness_score=75,
                readiness_label=readiness,
                last_seen_at="2026-05-17T00:00:00+00:00",
            )
        }
    )


def test_near_miss_symbols_loaded_from_prior_run(tmp_path) -> None:
    run_path = tmp_path / "latest_scan.json"
    payload = {
        "results": [
            _near_miss_symbol("NEARUSDT").model_dump(mode="json"),
            _rejected_symbol("REJECTUSDT").model_dump(mode="json"),
        ]
    }
    run_path.write_text(json.dumps(payload), encoding="utf-8")

    assert load_symbols_from_run(run_path, near_miss_only=True) == ("NEARUSDT",)


def test_alert_only_when_valid_setup_appears() -> None:
    symbol_result = _valid_symbol()
    previous = _prior_state().symbols["BTCUSDT"]

    assert current_result_is_valid_activation(symbol_result) is True
    assert should_trigger_activation_alert(symbol_result, previous) is True
    assert "WATCHLIST UPGRADED" in format_watch_activation_alert(symbol_result)


def test_no_alert_for_near_miss() -> None:
    previous = _prior_state().symbols["BTCUSDT"]

    assert should_trigger_activation_alert(_near_miss_symbol(), previous) is False


def test_no_alert_for_rejected() -> None:
    previous = _prior_state(status="no_setup", readiness="REJECTED").symbols["BTCUSDT"]

    assert should_trigger_activation_alert(_rejected_symbol(), previous) is False


def _watch_summary_for(*results: ScannerSymbolResult):
    return build_watch_iteration_summary(
        iteration=1,
        result=ScannerRunResult(
            config=_scanner_config([result.symbol for result in results] or ["BTCUSDT"]),
            results=results,
            scanned_symbols=len(results),
            failed_symbols=0,
            trade_ideas_created=sum(1 for result in results if result.trade_idea is not None),
            dry_run_alerts_created=0,
            journal_entries_created=0,
        ),
        activations=(),
        next_scan_seconds=None,
        scanned_at="2026-06-07T00:00:00+00:00",
    )


def test_watch_iteration_still_watching_counts_only_public_watchable_lifecycle_rows() -> None:
    summary = _watch_summary_for(_watch_symbol())

    assert summary.still_watching == 1
    assert summary.valid_activations == 0


def test_watch_iteration_still_watching_excludes_reject_grade_direction_na_and_na_trade_map() -> None:
    summary = _watch_summary_for(
        _watch_symbol("REJECTUSDT", quality_grade_current="Reject"),
        _watch_symbol("DIRNAUSDT", direction="n/a"),
        _watch_symbol("ENTRYNAUSDT", entry_low=NA, entry_high=NA),
        _watch_symbol("STOPNAUSDT", stop_loss=NA),
        _watch_symbol("TPNAUSDT", tp1=NA),
    )

    assert summary.still_watching == 0


def test_watch_iteration_still_watching_excludes_terminal_lifecycle_rows() -> None:
    summary = _watch_summary_for(
        _watch_symbol("COOLUSDT", current_state=SetupLifecycleState.COOLDOWN),
        _watch_symbol("INVALIDUSDT", current_state=SetupLifecycleState.INVALIDATED),
        _watch_symbol("TP2USDT", current_state=SetupLifecycleState.TP_HIT),
    )

    assert summary.still_watching == 0


def test_dry_run_default_does_not_call_telegram(tmp_path, monkeypatch, capsys) -> None:
    monkeypatch.setenv("TELEGRAM_ADMIN_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_DRY_RUN", "true")
    state_path = tmp_path / "watch_state.json"
    save_watch_state(state_path, _prior_state())
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", state_path)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_valid_symbol(),)]

    async def fail_send(*args, **kwargs):  # pragma: no cover - should never run
        raise AssertionError("dry-run watch mode must not call Telegram transport")

    monkeypatch.setattr("app.watch_mode.send_telegram_messages", fail_send)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "--watch",
                "--watch-max-iterations",
                "1",
                "--watch-interval-sec",
                "0.01",
            ]
        )
    )

    captured = capsys.readouterr()
    assert "Telegram manual lifecycle alerts: disabled" in captured.out
    assert "Telegram admin drafts: disabled/dry-run" in captured.out
    assert "Legacy scanner alerts: dry-run" in captured.out
    assert "WATCHLIST UPGRADED" in captured.out


def test_watch_mode_single_iteration_updates_state_and_jsonl(tmp_path, monkeypatch, capsys) -> None:
    state_path = tmp_path / "watch_state.json"
    output_path = tmp_path / "watch.jsonl"
    save_watch_state(state_path, _prior_state())
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", state_path)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_valid_symbol(),)]

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "--watch",
                "--watch-max-iterations",
                "1",
                "--watch-interval-sec",
                "0.01",
                "--watch-output-file",
                str(output_path),
            ]
        )
    )

    captured = capsys.readouterr()
    assert "Watch iteration 1" in captured.out
    assert "Valid activations: 1" in captured.out

    state_payload = json.loads(state_path.read_text(encoding="utf-8"))
    btc_state = state_payload["symbols"]["BTCUSDT"]
    assert btc_state["last_status"] == "valid_setup"
    assert btc_state["alert_sent"] is True
    assert btc_state["activation_count"] == 1
    assert btc_state["history"][-1]["alert_triggered"] is True

    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["valid_activations"] == 1
    assert summary["activated_symbols"] == ["BTCUSDT"]


def test_watch_iteration_stored_as_scan_run_when_store_scan_enabled(tmp_path, monkeypatch, capsys) -> None:
    state_path = tmp_path / "watch_state.json"
    db_path = tmp_path / "candle_craft.db"
    save_watch_state(state_path, _prior_state())
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", state_path)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_valid_symbol(),)]

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "--watch",
                "--watch-max-iterations",
                "1",
                "--watch-interval-sec",
                "0.01",
                "--store-scan",
                "--database-path",
                str(db_path),
            ]
        )
    )

    captured = capsys.readouterr()
    assert "Stored watch iteration: 1" in captured.out
    assert "Run ID:" in captured.out

    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT is_watch_iteration, watch_iteration_number, symbols_requested,
                   symbols_queued, symbols_completed, valid_activations,
                   still_watching, rejected_no_edge, data_issues, runtime_sec
            FROM scan_runs
            """
        ).fetchone()
        symbol_count = connection.execute("SELECT COUNT(*) FROM symbol_results").fetchone()[0]

    assert row[:9] == (1, 1, 1, 1, 1, 1, 0, 0, 0)
    assert row[9] >= 0
    assert symbol_count == 1


def _latest_queue_row(db_path):
    with sqlite3.connect(db_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute(
            """
            SELECT symbols_requested, symbols_queued, symbols_completed, runtime_stats_json
            FROM scan_runs
            ORDER BY timestamp DESC
            LIMIT 1
            """
        ).fetchone()


def _queue_payload(db_path) -> dict:
    row = _latest_queue_row(db_path)
    runtime = json.loads(row["runtime_stats_json"])
    return runtime["symbol_queue"]


def test_top_volume_watch_no_adaptive_queues_requested_top_100(tmp_path, monkeypatch, capsys) -> None:
    symbols = tuple(f"SYM{i:03d}USDT" for i in range(100))
    db_path = _patch_watch_runtime(tmp_path, monkeypatch, symbols=symbols)

    asyncio.run(
        run_scan.main(
            _watch_args(
                db_path,
                universe_size=100,
                max_symbols=100,
                extra_args=("--no-adaptive-symbol-priority", "--lifecycle"),
            )
        )
    )

    queued = _config_symbols(EchoWatchRunner.configs[0])
    row = _latest_queue_row(db_path)
    queue = json.loads(row["runtime_stats_json"])["symbol_queue"]
    captured = capsys.readouterr()

    assert queued == symbols
    assert row["symbols_requested"] == 100
    assert row["symbols_queued"] == 100
    assert row["symbols_completed"] == 100
    assert queue["universe_requested_count"] == 100
    assert queue["universe_resolved_count"] == 100
    assert queue["final_queued_count"] == 100
    assert queue["adaptive_priority_enabled"] is False
    assert queue["queue_cap_applied"] is False
    assert "Symbol queue diagnostics:" in captured.out
    assert "- Final queued count: 100" in captured.out


def test_watch_mode_does_not_imply_symbols_from_latest_run(tmp_path, monkeypatch) -> None:
    symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    db_path = _patch_watch_runtime(tmp_path, monkeypatch, symbols=symbols)
    run_scan.LATEST_RUN_PATH.write_text(
        json.dumps({"results": [_near_miss_symbol("LATESTUSDT").model_dump(mode="json")]}),
        encoding="utf-8",
    )

    asyncio.run(
        run_scan.main(
            _watch_args(
                db_path,
                universe_size=3,
                max_symbols=3,
                extra_args=("--no-adaptive-symbol-priority", "--disable-lifecycle"),
            )
        )
    )

    assert _config_symbols(EchoWatchRunner.configs[0]) == symbols


def test_watch_mode_does_not_imply_watch_only_near_misses(tmp_path, monkeypatch) -> None:
    symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    db_path = _patch_watch_runtime(tmp_path, monkeypatch, symbols=symbols)
    save_watch_state(run_scan.WATCH_STATE_PATH, _prior_state("AAAUSDT"))

    asyncio.run(
        run_scan.main(
            _watch_args(
                db_path,
                universe_size=3,
                max_symbols=3,
                extra_args=("--no-adaptive-symbol-priority", "--disable-lifecycle"),
            )
        )
    )

    assert _config_symbols(EchoWatchRunner.configs[0]) == symbols


def test_watch_universe_refreshes_each_iteration(tmp_path, monkeypatch) -> None:
    db_path = tmp_path / "queue.sqlite"
    sequences = [
        ("START1USDT", "START2USDT"),
        ("ITER1AUSDT", "ITER1BUSDT"),
        ("ITER2AUSDT", "ITER2BUSDT"),
    ]
    _patch_watch_runtime(tmp_path, monkeypatch, symbols=sequences[0], db_path=db_path)

    async def resolve_universe(mode, *, universe_size, min_quote_volume):
        index = min(resolve_universe.calls, len(sequences) - 1)
        resolve_universe.calls += 1
        return _universe(sequences[index], requested_size=universe_size)

    resolve_universe.calls = 0
    monkeypatch.setattr(run_scan, "resolve_symbol_universe", resolve_universe)

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(run_scan.asyncio, "sleep", no_sleep)

    asyncio.run(
        run_scan.main(
            _watch_args(
                db_path,
                universe_size=2,
                max_symbols=2,
                extra_args=(
                    "--watch-max-iterations",
                    "2",
                    "--no-adaptive-symbol-priority",
                    "--disable-lifecycle",
                ),
            )
        )
    )

    assert [_config_symbols(config) for config in EchoWatchRunner.configs] == [
        sequences[1],
        sequences[2],
    ]


def test_lifecycle_priority_promotes_without_shrinking_universe(tmp_path, monkeypatch) -> None:
    symbols = ("BASEUSDT", "WATCHUSDT", "OLDUSDT")
    db_path = tmp_path / "queue.sqlite"
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(_watch_lifecycle_record(symbol="WATCHUSDT"))
        repository.upsert_record(
            _watch_lifecycle_record(symbol="OLDUSDT", current_state=SetupLifecycleState.ARCHIVED)
        )
    _patch_watch_runtime(tmp_path, monkeypatch, symbols=symbols, db_path=db_path)

    asyncio.run(
        run_scan.main(
            _watch_args(
                db_path,
                universe_size=3,
                max_symbols=3,
                extra_args=("--no-adaptive-symbol-priority", "--lifecycle"),
            )
        )
    )

    queue = _queue_payload(db_path)
    assert _config_symbols(EchoWatchRunner.configs[0]) == ("WATCHUSDT", "BASEUSDT", "OLDUSDT")
    assert queue["final_queued_count"] == 3
    assert queue["lifecycle_priority_promoted_count"] == 1
    assert queue["lifecycle_priority_dropped_count"] == 0


def test_continue_watch_candidates_do_not_override_explicit_max_symbols(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "watch_state.json"
    latest_path = tmp_path / "latest_scan.json"
    save_watch_state(state_path, _prior_state("EXTRAUSDT"))
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", state_path)
    monkeypatch.setattr(run_scan, "LATEST_RUN_PATH", latest_path)
    args = SimpleNamespace(max_symbols=2)
    watchlist = run_scan.WatchlistResolution(
        symbols=("AAAUSDT", "BBBUSDT"),
        source_label="universe binance_usdt_perp_top_volume",
        universe=_universe(("AAAUSDT", "BBBUSDT"), requested_size=2),
    )

    extended = run_scan._extend_watchlist_for_continue_watch(args, watchlist)

    assert extended.symbols == ("AAAUSDT", "BBBUSDT")
    assert extended.queue_cap_applied is True
    assert extended.pre_cap_symbols_count == 3


def test_no_adaptive_priority_prevents_cooldown_from_shrinking_queue(tmp_path, monkeypatch) -> None:
    symbols = ("SLOWUSDT", "FASTUSDT", "OKUSDT")
    db_path = tmp_path / "queue.sqlite"
    save_symbol_health_records(
        db_path,
        {
            "SLOWUSDT": SymbolHealthRecord(
                symbol="SLOWUSDT",
                current_health_score=20,
                cooldown_until="2099-01-01T00:00:00+00:00",
            )
        },
    )
    _patch_watch_runtime(tmp_path, monkeypatch, symbols=symbols, db_path=db_path)

    asyncio.run(
        run_scan.main(
            _watch_args(
                db_path,
                universe_size=3,
                max_symbols=3,
                extra_args=("--no-adaptive-symbol-priority", "--disable-lifecycle"),
            )
        )
    )

    queue = _queue_payload(db_path)
    assert _config_symbols(EchoWatchRunner.configs[0]) == symbols
    assert queue["symbol_health_excluded_count"] == 0
    assert queue["adaptive_priority_enabled"] is False


def test_explicit_exclude_removes_only_requested_symbol_and_reports_reason(tmp_path, monkeypatch) -> None:
    symbols = ("AAAUSDT", "BBBUSDT", "CCCUSDT")
    db_path = _patch_watch_runtime(tmp_path, monkeypatch, symbols=symbols)

    asyncio.run(
        run_scan.main(
            _watch_args(
                db_path,
                universe_size=3,
                max_symbols=3,
                extra_args=("--exclude-symbols", "BBBUSDT", "--no-adaptive-symbol-priority", "--disable-lifecycle"),
            )
        )
    )

    queue = _queue_payload(db_path)
    assert _config_symbols(EchoWatchRunner.configs[0]) == ("AAAUSDT", "CCCUSDT")
    assert queue["explicit_user_excluded_count"] == 1
    assert queue["exclusion_examples"]["explicit_user_excluded"] == ["BBBUSDT"]
    assert queue["final_queued_count"] == 2


def test_active_hard_symbol_health_cooldown_excludes_only_affected_symbol(tmp_path, monkeypatch) -> None:
    symbols = ("SLOWUSDT", "FASTUSDT", "OKUSDT")
    db_path = tmp_path / "queue.sqlite"
    save_symbol_health_records(
        db_path,
        {
            "SLOWUSDT": SymbolHealthRecord(
                symbol="SLOWUSDT",
                current_health_score=20,
                cooldown_until="2099-01-01T00:00:00+00:00",
            )
        },
    )
    _patch_watch_runtime(tmp_path, monkeypatch, symbols=symbols, db_path=db_path)

    asyncio.run(
        run_scan.main(_watch_args(db_path, universe_size=3, max_symbols=3, extra_args=("--disable-lifecycle",)))
    )

    queue = _queue_payload(db_path)
    assert _config_symbols(EchoWatchRunner.configs[0]) == ("FASTUSDT", "OKUSDT")
    assert queue["symbol_health_excluded_count"] == 1
    assert queue["exclusion_examples"]["symbol_health_cooldown"] == ["SLOWUSDT"]
    assert queue["final_queued_count"] == 2


def test_expired_symbol_health_cooldown_does_not_exclude(tmp_path, monkeypatch) -> None:
    symbols = ("SLOWUSDT", "FASTUSDT", "OKUSDT")
    db_path = tmp_path / "queue.sqlite"
    save_symbol_health_records(
        db_path,
        {
            "SLOWUSDT": SymbolHealthRecord(
                symbol="SLOWUSDT",
                current_health_score=20,
                cooldown_until="2000-01-01T00:00:00+00:00",
            )
        },
    )
    _patch_watch_runtime(tmp_path, monkeypatch, symbols=symbols, db_path=db_path)

    asyncio.run(
        run_scan.main(_watch_args(db_path, universe_size=3, max_symbols=3, extra_args=("--disable-lifecycle",)))
    )

    queue = _queue_payload(db_path)
    assert set(_config_symbols(EchoWatchRunner.configs[0])) == set(symbols)
    assert queue["symbol_health_excluded_count"] == 0
    assert queue["final_queued_count"] == 3


def test_soft_symbol_health_penalty_does_not_exclude_when_no_adaptive(tmp_path, monkeypatch) -> None:
    symbols = ("LOWUSDT", "FASTUSDT")
    db_path = tmp_path / "queue.sqlite"
    save_symbol_health_records(
        db_path,
        {"LOWUSDT": SymbolHealthRecord(symbol="LOWUSDT", current_health_score=10, timeout_strikes=2)},
    )
    _patch_watch_runtime(tmp_path, monkeypatch, symbols=symbols, db_path=db_path)

    asyncio.run(
        run_scan.main(
            _watch_args(
                db_path,
                universe_size=2,
                max_symbols=2,
                extra_args=("--no-adaptive-symbol-priority", "--disable-lifecycle"),
            )
        )
    )

    queue = _queue_payload(db_path)
    assert _config_symbols(EchoWatchRunner.configs[0]) == symbols
    assert queue["symbol_health_excluded_count"] == 0
    assert queue["final_queued_count"] == 2


def test_watch_mode_cancelled_sleep_shuts_down_cleanly(tmp_path, monkeypatch, capsys) -> None:
    state_path = tmp_path / "watch_state.json"
    db_path = tmp_path / "candle_craft.db"
    save_watch_state(state_path, _prior_state())
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", state_path)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_near_miss_symbol(),)]

    async def cancel_sleep(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(run_scan.asyncio, "sleep", cancel_sleep)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "--watch",
                "--watch-interval-sec",
                "0.01",
                "--store-scan",
                "--database-path",
                str(db_path),
            ]
        )
    )

    captured = capsys.readouterr()
    assert "Watch mode stopped by user." in captured.out
    assert "Completed iterations: 1" in captured.out
    assert "Stored scan runs: 1" in captured.out
    assert f"Data saved to: {db_path.as_posix()}" in captured.out
    assert "CancelledError" not in captured.err
    assert "KeyboardInterrupt" not in captured.err


def test_watch_mode_does_not_store_scan_without_store_scan(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "watch_state.json"
    db_path = tmp_path / "candle_craft.db"
    save_watch_state(state_path, _prior_state())
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", state_path)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_near_miss_symbol(),)]

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "--watch",
                "--watch-max-iterations",
                "1",
                "--watch-interval-sec",
                "0.01",
                "--database-path",
                str(db_path),
                "--disable-lifecycle",
                "--no-adaptive-symbol-priority",
            ]
        )
    )

    assert not db_path.exists()


def test_watch_mode_max_iterations_stop(tmp_path, monkeypatch) -> None:
    state_path = tmp_path / "watch_state.json"
    save_watch_state(state_path, _prior_state())
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", state_path)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_near_miss_symbol(),), (_near_miss_symbol(),)]

    async def no_sleep(_seconds):
        return None

    monkeypatch.setattr(run_scan.asyncio, "sleep", no_sleep)

    asyncio.run(
        run_scan.main(
            [
                "--symbols",
                "BTCUSDT",
                "--watch",
                "--watch-max-iterations",
                "2",
                "--watch-interval-sec",
                "0.01",
            ]
        )
    )

    assert len(SequenceWatchRunner.configs) == 2


def test_live_telegram_requires_env_vars(monkeypatch, tmp_path) -> None:
    state_path = tmp_path / "watch_state.json"
    save_watch_state(state_path, _prior_state())
    monkeypatch.setattr(run_scan, "WATCH_STATE_PATH", state_path)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    monkeypatch.setattr(run_scan, "Settings", lambda: SimpleNamespace(telegram_bot_token=None, telegram_chat_id=None))
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    with pytest.raises(SystemExit, match="TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"):
        asyncio.run(
            run_scan.main(
                [
                    "--symbols",
                    "BTCUSDT",
                    "--watch",
                    "--watch-max-iterations",
                    "1",
                    "--telegram-live-alerts",
                    "true",
                ]
            )
        )


def test_watch_state_update_behavior() -> None:
    state = update_watch_state_for_result(
        _prior_state(),
        _valid_symbol(),
        alert_triggered=True,
        seen_at="2026-05-17T01:00:00+00:00",
    )

    symbol_state = state.symbols["BTCUSDT"]
    assert symbol_state.last_status == "valid_setup"
    assert symbol_state.readiness_label == "VALID SETUP"
    assert symbol_state.alert_sent is True
    assert symbol_state.activation_count == 1
    assert symbol_state.history[-1].quality_state in {
        SetupQualityState.HIGH_QUALITY_TRADE.value,
        SetupQualityState.VALID_BUT_LOWER_QUALITY.value,
    }


def test_watch_state_persists_deprecation_marker(tmp_path) -> None:
    state_path = tmp_path / "watch_state.json"

    save_watch_state(state_path, WatchState())

    payload = json.loads(state_path.read_text(encoding="utf-8"))
    assert payload["deprecated"] is True
    assert payload["source_of_truth"] == "db_lifecycle_state"
    assert "retained for compatibility" in payload["deprecation_note"]
