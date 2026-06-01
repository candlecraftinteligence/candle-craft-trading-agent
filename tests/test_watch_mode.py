from __future__ import annotations

import asyncio
import json
import sqlite3
from decimal import Decimal

import pytest

from app.agents.trade_idea import TradeIdeaAgent
from app.analytics.setup_quality import SetupQualityState, validate_setup_quality
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerRunConfig, ScannerRunResult, ScannerSymbolResult
from app.watch_mode import (
    WatchSymbolState,
    WatchState,
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
        )


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
    assert "Candle Craft Setup Activated" in format_watch_activation_alert(symbol_result)


def test_no_alert_for_near_miss() -> None:
    previous = _prior_state().symbols["BTCUSDT"]

    assert should_trigger_activation_alert(_near_miss_symbol(), previous) is False


def test_no_alert_for_rejected() -> None:
    previous = _prior_state(status="no_setup", readiness="REJECTED").symbols["BTCUSDT"]

    assert should_trigger_activation_alert(_rejected_symbol(), previous) is False


def test_dry_run_default_does_not_call_telegram(tmp_path, monkeypatch, capsys) -> None:
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
    assert "Telegram alerts: dry-run" in captured.out
    assert "Candle Craft Setup Activated" in captured.out


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
