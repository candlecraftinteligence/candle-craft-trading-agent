"""Strict discovery fails closed. Owned-plan monitoring stays independent."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from app.lifecycle.owner_monitoring import discovery_failure_continues_owner_monitoring
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.universe import symbol_universe
from app.universe.symbol_universe import (
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
    UniverseResolutionError,
)
from scripts import run_scan
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY
from tests.test_p5a_active_owner_monitoring import (
    TIMEFRAME,
    _event_count,
    _progress,
    _rejected_result,
    _seed_entered,
)
from tests.test_p5a_monitoring_binding_query_repair import _KlineClient, _quiet_runtime
from tests.test_watch_mode import SequenceWatchRunner, _bootstrap_watch_main


def _contract(base_symbol: str) -> dict[str, str]:
    return {
        "symbol": f"{base_symbol}USDT",
        "baseAsset": base_symbol,
        "quoteAsset": "USDT",
        "contractType": "PERPETUAL",
        "status": "TRADING",
        "underlyingType": "COIN",
    }


def _asset(base_symbol: str, rank: int) -> dict[str, object]:
    return {
        "id": f"{base_symbol.lower()}-{rank}",
        "symbol": base_symbol,
        "rank": rank,
        "quotes": {"USD": {"market_cap": str(1_000_000 - rank)}},
    }


def _ticker(base_symbol: str) -> dict[str, str]:
    return {"symbol": f"{base_symbol}USDT", "quoteVolume": "100"}


class _StrictSources:
    def __init__(self, *, fail_tickers_on_call: int | None = None) -> None:
        self.calls = 0
        self.fail_tickers_on_call = fail_tickers_on_call
        self.assets = [_asset("BTC", 1)]
        self.tickers = [_ticker("BTC")]
        self.exchange_info = {"symbols": [_contract("BTC")]}


def _install_strict_sources(monkeypatch: pytest.MonkeyPatch, sources: _StrictSources) -> None:
    async def rankings() -> list[dict[str, object]]:
        sources.calls += 1
        return sources.assets

    class _FakeBinance:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def get_24h_tickers(self) -> list[dict[str, str]] | None:
            if sources.fail_tickers_on_call is not None and sources.calls >= sources.fail_tickers_on_call:
                return None
            return sources.tickers

        async def get_exchange_info(self) -> dict[str, object]:
            return sources.exchange_info

        async def aclose(self) -> None:
            return None

    monkeypatch.setattr(symbol_universe, "fetch_coinpaprika_market_cap_rankings", rankings)
    monkeypatch.setattr(symbol_universe, "BinanceFuturesClient", _FakeBinance)


def _prepare(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    _quiet_runtime(monkeypatch, tmp_path)
    monkeypatch.setenv("LOCAL_MANUAL_MODE", "true")
    monkeypatch.setenv("ORDER_EXECUTION_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_DRY_RUN", "true")
    monkeypatch.setenv("TELEGRAM_SIGNALS_ENABLED", "false")
    monkeypatch.setenv("RUNTIME_EPOCH_ID", SYNTHETIC_IDENTITY.epoch_id)
    monkeypatch.setenv("RUNTIME_EPOCH_CUTOFF_AT", SYNTHETIC_IDENTITY.cutoff_at)
    monkeypatch.setenv("RUNTIME_REVIEWED_RELEASE_SHA", SYNTHETIC_IDENTITY.reviewed_release_sha)
    monkeypatch.setenv("RUNTIME_GENERATION_BINDING", SYNTHETIC_IDENTITY.generation_binding)
    monkeypatch.setenv("RUNTIME_EPOCH_CONTRACT_VERSION", SYNTHETIC_IDENTITY.contract_version)
    path, _epoch_path = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    return path


def _strict_argv(path: Path, *extra: str) -> list[str]:
    return [
        "--universe",
        BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
        "--universe-size",
        "1",
        "--database-path",
        str(path),
        "--lifecycle",
        "--execution-timeframe",
        TIMEFRAME,
        "--no-cache",
        "--display",
        "compact",
        *extra,
    ]


def _capture_runner(results: list[tuple[object, ...]] | None = None) -> None:
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = list(results or [])


def _queued_symbols() -> tuple[str, ...]:
    queued: list[str] = []
    for config in SequenceWatchRunner.configs:
        queued.extend(getattr(symbol, "symbol", str(symbol)) for symbol in config.symbols)
    return tuple(queued)


def _table_count(path: Path, table: str) -> int:
    connection = sqlite3.connect(path)
    try:
        row = connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
    finally:
        connection.close()
    assert row is not None
    return int(row[0])


def _lifecycle_symbols(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT symbol FROM setup_lifecycle_records").fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


@pytest.mark.parametrize("outcome", ["success", "gap", "unexpected"])
def test_one_shot_source_failure_skips_discovery_and_keeps_owner_monitoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
) -> None:
    path = _prepare(monkeypatch, tmp_path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository, symbol="CAKEUSDT")
        before_cursor = _progress(repository, owner.lifecycle_id).evaluation_cursor_close_at
        before_events = _event_count(repository, owner.lifecycle_id)
    _install_strict_sources(monkeypatch, _StrictSources(fail_tickers_on_call=1))
    _capture_runner()
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    if outcome == "gap":
        monkeypatch.setattr(
            run_scan,
            "_owner_monitoring_client",
            lambda args: _KlineClient(error=TimeoutError("klines timed out")),
        )
    elif outcome == "unexpected":
        def broken_factory(args: object) -> object:
            del args
            raise ValueError("owner bookkeeping failed")

        monkeypatch.setattr(run_scan, "_owner_monitoring_client", broken_factory)
    else:
        monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: _KlineClient())

    with pytest.raises(SystemExit) as raised:
        asyncio.run(run_scan.main(_strict_argv(path)))

    message = str(raised.value.code)
    assert isinstance(raised.value.__cause__, UniverseResolutionError)
    assert "ticker source returned malformed" in message
    assert discovery_failure_continues_owner_monitoring(raised.value) is True
    assert _queued_symbols() == ()
    assert _table_count(path, "symbol_results") == 0
    assert _table_count(path, "setup_candidates") == 0
    assert _table_count(path, "public_alert_events") == 0
    assert _lifecycle_symbols(path) == {"CAKEUSDT"}
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
        record = repository.get_record_by_lifecycle_id(owner.lifecycle_id)
    assert record is not None and record.current_state.value == "MANAGING"
    if outcome == "success":
        assert "owner_monitoring" not in message
        assert stored.evaluation_cursor_close_at != before_cursor
        assert stored.tp1_at is not None
        with SQLiteSetupLifecycleRepository(path) as repository:
            events_after_first = _event_count(repository, owner.lifecycle_id)
        assert events_after_first >= before_events
        with pytest.raises(SystemExit):
            asyncio.run(run_scan.main(_strict_argv(path)))
        with SQLiteSetupLifecycleRepository(path) as repository:
            repeated = _progress(repository, owner.lifecycle_id)
            assert repeated.evaluation_cursor_close_at == stored.evaluation_cursor_close_at
            assert repeated.tp1_at == stored.tp1_at
            assert _event_count(repository, owner.lifecycle_id) == events_after_first
    elif outcome == "gap":
        assert "owner_monitoring" not in message
        assert "ticker source returned malformed" in message
        assert "TimeoutError" in stored.diagnostic
        assert stored.terminal_outcome in (None, "N/A")
        assert stored.evaluation_cursor_close_at == before_cursor
    else:
        assert "ticker source returned malformed" in message
        assert "owner_monitoring" in message
        assert "owner bookkeeping failed" in message
        assert "exchange_market_data_unavailable" not in stored.diagnostic
        assert stored.evaluation_cursor_close_at == before_cursor


def test_watch_refresh_source_failure_does_not_scan_or_reinsert_the_owner(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _prepare(monkeypatch, tmp_path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository, symbol="CAKEUSDT")
        before_cursor = _progress(repository, owner.lifecycle_id).evaluation_cursor_close_at
    _install_strict_sources(monkeypatch, _StrictSources(fail_tickers_on_call=2))
    _capture_runner()
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: _KlineClient())
    output = tmp_path / "watch.jsonl"
    asyncio.run(
        run_scan.main(
            _strict_argv(
                path,
                "--watch",
                "--watch-max-iterations",
                "1",
                "--watch-interval-sec",
                "0.01",
                "--watch-output-file",
                str(output),
            )
        )
    )
    summary = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert summary["status"] == "FAILED"
    assert any("ticker source returned malformed" in error for error in summary["errors"])
    assert not any(str(error).startswith("owner_monitoring:") for error in summary["errors"])
    assert _queued_symbols() == ()
    assert "CAKEUSDT" not in _queued_symbols()
    assert _lifecycle_symbols(path) == {"CAKEUSDT"}
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert stored.evaluation_cursor_close_at != before_cursor
    assert stored.tp1_at is not None


def test_watch_refresh_keeps_monitoring_failure_beside_discovery_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _prepare(monkeypatch, tmp_path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository, symbol="CAKEUSDT")
        before_cursor = _progress(repository, owner.lifecycle_id).evaluation_cursor_close_at

    def broken_factory(args: object) -> object:
        del args
        raise ValueError("owner bookkeeping failed")

    _install_strict_sources(monkeypatch, _StrictSources(fail_tickers_on_call=2))
    _capture_runner()
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", broken_factory)
    output = tmp_path / "watch.jsonl"
    asyncio.run(
        run_scan.main(
            _strict_argv(
                path,
                "--watch",
                "--watch-max-iterations",
                "1",
                "--watch-interval-sec",
                "0.01",
                "--watch-output-file",
                str(output),
            )
        )
    )
    summary = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert any("ticker source returned malformed" in error for error in summary["errors"])
    assert any("owner bookkeeping failed" in error for error in summary["errors"])
    assert summary["phase_statuses"]["owner_monitoring"] == "PARTIAL"
    assert _queued_symbols() == ()
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert stored.evaluation_cursor_close_at == before_cursor
    assert "exchange_market_data_unavailable" not in stored.diagnostic


def test_operator_exclude_empty_queue_is_not_a_provider_outage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _prepare(monkeypatch, tmp_path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository, symbol="CAKEUSDT")
        before_cursor = _progress(repository, owner.lifecycle_id).evaluation_cursor_close_at
    _install_strict_sources(monkeypatch, _StrictSources())
    _capture_runner()
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: _KlineClient())

    with pytest.raises(SystemExit) as raised:
        asyncio.run(run_scan.main(_strict_argv(path, "--exclude-symbols", "BTCUSDT")))

    assert "empty after universe/include/exclude" in str(raised.value)
    assert not isinstance(raised.value.__cause__, UniverseResolutionError)
    assert discovery_failure_continues_owner_monitoring(raised.value) is False
    assert _queued_symbols() == ()
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert stored.evaluation_cursor_close_at == before_cursor


def test_valid_strict_discovery_scans_only_the_contract_intersection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = _prepare(monkeypatch, tmp_path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository, symbol="CAKEUSDT")
        before_cursor = _progress(repository, owner.lifecycle_id).evaluation_cursor_close_at
    _install_strict_sources(monkeypatch, _StrictSources())
    _capture_runner(
        [
            (
                _rejected_result(
                    "BTCUSDT",
                    (),
                    mode="scalp",
                    bias=None,
                ),
            )
        ]
    )
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: _KlineClient())

    asyncio.run(run_scan.main(_strict_argv(path)))

    assert _queued_symbols() == ("BTCUSDT",)
    assert "CAKEUSDT" not in _queued_symbols()
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
        owner_record = repository.get_record_by_lifecycle_id(owner.lifecycle_id)
    assert owner_record is not None and owner_record.current_state.value == "MANAGING"
    assert stored.evaluation_cursor_close_at != before_cursor


def test_include_and_persisted_symbols_cannot_widen_a_real_strict_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_strict_sources(monkeypatch, _StrictSources())
    args = run_scan.parse_args(
        [
            "--universe",
            BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
            "--universe-size",
            "1",
            "--include-symbols",
            "OUTSIDEUSDT",
        ]
    )
    resolution = asyncio.run(run_scan._resolve_universe_watchlist(args))
    assert resolution.symbols == ("BTCUSDT",)
    assert resolution.membership_boundary_ignored_symbols == ("OUTSIDEUSDT",)

    def persisted(*args: object, **kwargs: object) -> tuple[str, ...]:
        del args, kwargs
        return ("OUTSIDEUSDT", "BTCUSDT")

    monkeypatch.setattr(run_scan, "load_symbols_from_run", persisted)
    latest_args = run_scan.parse_args(
        [
            "--universe",
            BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
            "--universe-size",
            "1",
            "--watch",
            "--watch-symbols-from-latest-run",
        ]
    )
    latest = asyncio.run(run_scan._resolve_watchlist_for_args(latest_args))
    assert latest.symbols == ("BTCUSDT",)
    assert "OUTSIDEUSDT" in latest.membership_boundary_ignored_symbols


def test_failed_strict_resolution_does_not_consult_persisted_symbols(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_strict_sources(monkeypatch, _StrictSources(fail_tickers_on_call=1))

    def persisted(*args: object, **kwargs: object) -> tuple[str, ...]:
        del args, kwargs
        raise AssertionError("persisted symbols were consulted after source failure")

    monkeypatch.setattr(run_scan, "load_symbols_from_run", persisted)
    args = run_scan.parse_args(
        [
            "--universe",
            BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
            "--universe-size",
            "1",
            "--watch",
            "--watch-symbols-from-latest-run",
        ]
    )
    with pytest.raises(SystemExit) as raised:
        asyncio.run(run_scan._resolve_watchlist_for_args(args))
    assert isinstance(raised.value.__cause__, UniverseResolutionError)
    assert discovery_failure_continues_owner_monitoring(raised.value) is True
