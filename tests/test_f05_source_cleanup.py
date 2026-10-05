"""Owned source-client cleanup stays on the strict discovery failure route."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from app.lifecycle.owner_monitoring import discovery_failure_continues_owner_monitoring
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.universe import symbol_universe
from app.universe.symbol_universe import (
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
    UniverseResolutionError,
    resolve_symbol_universe,
)
from scripts import run_scan
from tests.test_f05_discovery_fail_closed import (
    _asset,
    _capture_runner,
    _contract,
    _prepare,
    _queued_symbols,
    _strict_argv,
    _table_count,
    _ticker,
)
from tests.test_p5a_active_owner_monitoring import _event_count, _progress, _seed_entered
from tests.test_p5a_monitoring_binding_query_repair import _KlineClient
from tests.test_watch_mode import SequenceWatchRunner

CLEANUP_TEXT = "synthetic HTTP client cleanup failure"
RANKING_CLEANUP_TEXT = "synthetic ranking client cleanup failure"
PRIMARY = {
    "metadata_missing": "exchange-info source returned malformed",
    "tickers_missing": "ticker source returned malformed",
    "metadata_empty": "exchange-info source returned empty",
    "tickers_empty": "ticker source returned empty",
    "non_trading": "no admissible USDT perpetual contracts",
    "valid": "Binance USDT perpetual source cleanup failed",
}


def _valid_payloads() -> tuple[list[dict[str, object]], list[dict[str, str]], dict[str, object]]:
    return ([_asset("BTC", 1)], [_ticker("BTC")], {"symbols": [_contract("BTC")]})


def _payloads_for(case: str, calls: int, fail_from: int) -> tuple[object, object, object]:
    assets, tickers, exchange_info = _valid_payloads()
    if calls < fail_from:
        return assets, tickers, exchange_info
    if case == "metadata_missing":
        return assets, tickers, None
    if case == "tickers_missing":
        return assets, None, exchange_info
    if case == "metadata_empty":
        return assets, tickers, {"symbols": []}
    if case == "tickers_empty":
        return assets, [], exchange_info
    if case == "non_trading":
        contract = dict(_contract("BTC"))
        contract["status"] = "BREAK"
        return assets, tickers, {"symbols": [contract]}
    return assets, tickers, exchange_info


def _install_owned_binance(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    *,
    fail_from: int = 1,
    close_error: BaseException | None = RuntimeError(CLEANUP_TEXT),
    acquire_error: BaseException | None = None,
) -> dict[str, int]:
    state = {"calls": 0, "closed": 0}

    async def rankings() -> object:
        state["calls"] += 1
        return _payloads_for(case, state["calls"], fail_from)[0]

    class FakeBinance:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def get_24h_tickers(self) -> object:
            if acquire_error is not None and state["calls"] >= fail_from:
                raise acquire_error
            return _payloads_for(case, state["calls"], fail_from)[1]

        async def get_exchange_info(self) -> object:
            return _payloads_for(case, state["calls"], fail_from)[2]

        async def aclose(self) -> None:
            state["closed"] += 1
            if close_error is not None and state["calls"] >= fail_from:
                raise close_error

    monkeypatch.setattr(symbol_universe, "fetch_coinpaprika_market_cap_rankings", rankings)
    monkeypatch.setattr(symbol_universe, "BinanceFuturesClient", FakeBinance)
    return state


def _arm_owner(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, object, str]:
    monkeypatch.setenv("SOURCE_REPLAY_CAPTURE_ENABLED", "false")
    path = _prepare(monkeypatch, tmp_path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository, symbol="CAKEUSDT")
        before = _progress(repository, owner.lifecycle_id).evaluation_cursor_close_at
    return path, owner, before


def _watch_argv(path: Path, output: Path) -> list[str]:
    return _strict_argv(
        path,
        "--watch",
        "--watch-max-iterations",
        "1",
        "--watch-interval-sec",
        "0.01",
        "--watch-output-file",
        str(output),
    )


def _assert_no_discovery(path: Path) -> None:
    assert _queued_symbols() == ()
    for table in ("symbol_results", "setup_candidates", "public_alert_events"):
        assert _table_count(path, table) == 0
    assert _lifecycle_symbols(path) == {"CAKEUSDT"}


def _lifecycle_symbols(path: Path) -> set[str]:
    import sqlite3

    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("SELECT symbol FROM setup_lifecycle_records").fetchall()
    finally:
        connection.close()
    return {str(row[0]) for row in rows}


@pytest.mark.parametrize(
    "case",
    ["metadata_missing", "tickers_missing", "metadata_empty", "tickers_empty", "non_trading", "valid"],
)
def test_one_shot_cleanup_failure_continues_owner_monitoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    path, owner, before = _arm_owner(tmp_path, monkeypatch)
    _install_owned_binance(monkeypatch, case)
    _capture_runner()
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    calls: list[str] = []
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: calls.append("client") or _KlineClient())

    with pytest.raises(SystemExit) as raised:
        asyncio.run(run_scan.main(_strict_argv(path)))

    cause = raised.value.__cause__
    message = str(raised.value.code)
    assert isinstance(cause, UniverseResolutionError)
    assert PRIMARY[case] in message
    assert CLEANUP_TEXT in message
    if case != "valid":
        assert message.index(PRIMARY[case]) < message.index("source_cleanup_error")
        assert cause.__cause__ is None or "cleanup" not in str(cause.__cause__)
    else:
        assert isinstance(cause.__cause__, RuntimeError)
        assert str(cause.__cause__) == CLEANUP_TEXT
    assert isinstance(cause.source_cleanup_error, RuntimeError)
    assert discovery_failure_continues_owner_monitoring(raised.value) is True
    assert calls == ["client"]
    _assert_no_discovery(path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
        events = _event_count(repository, owner.lifecycle_id)
    assert stored.evaluation_cursor_close_at != before
    assert stored.tp1_at is not None
    if case == "valid":
        with pytest.raises(SystemExit):
            asyncio.run(run_scan.main(_strict_argv(path)))
        with SQLiteSetupLifecycleRepository(path) as repository:
            repeated = _progress(repository, owner.lifecycle_id)
            assert repeated.evaluation_cursor_close_at == stored.evaluation_cursor_close_at
            assert repeated.tp1_at == stored.tp1_at
            assert _event_count(repository, owner.lifecycle_id) == events


@pytest.mark.parametrize("case", ["metadata_empty", "valid"])
def test_watch_refresh_cleanup_failure_continues_owner_monitoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case: str,
) -> None:
    path, owner, before = _arm_owner(tmp_path, monkeypatch)
    _install_owned_binance(monkeypatch, case, fail_from=2)
    _capture_runner()
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    calls: list[str] = []
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: calls.append("client") or _KlineClient())
    output = tmp_path / "watch.jsonl"

    asyncio.run(run_scan.main(_watch_argv(path, output)))

    summary = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    text = " ".join(summary["errors"])
    assert summary["status"] == "FAILED"
    assert PRIMARY[case] in text
    assert CLEANUP_TEXT in text
    assert "iteration:RuntimeError:" not in text
    assert calls == ["client"]
    _assert_no_discovery(path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert stored.evaluation_cursor_close_at != before
    assert stored.tp1_at is not None


def test_watch_startup_cleanup_failure_continues_owner_monitoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, owner, before = _arm_owner(tmp_path, monkeypatch)
    _install_owned_binance(monkeypatch, "metadata_empty")
    _capture_runner()
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    calls: list[str] = []
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: calls.append("client") or _KlineClient())
    output = tmp_path / "watch.jsonl"

    with pytest.raises(SystemExit) as raised:
        asyncio.run(run_scan.main(_watch_argv(path, output)))

    assert "exchange-info source returned empty" in str(raised.value.code)
    assert CLEANUP_TEXT in str(raised.value.code)
    assert discovery_failure_continues_owner_monitoring(raised.value) is True
    assert calls == ["client"]
    assert not output.exists()
    _assert_no_discovery(path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert stored.evaluation_cursor_close_at != before


@pytest.mark.parametrize("path_kind", ["one_shot", "watch_refresh"])
@pytest.mark.parametrize("monitoring", ["gap", "subsystem_failure"])
def test_cleanup_and_monitoring_failures_stay_distinct(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    path_kind: str,
    monitoring: str,
) -> None:
    path, owner, before = _arm_owner(tmp_path, monkeypatch)
    _install_owned_binance(monkeypatch, "metadata_empty", fail_from=2 if path_kind == "watch_refresh" else 1)
    _capture_runner()
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    if monitoring == "gap":
        monkeypatch.setattr(
            run_scan,
            "_owner_monitoring_client",
            lambda args: _KlineClient(error=TimeoutError("synthetic candle gap")),
        )
    else:
        def broken(_args: object) -> object:
            raise ValueError("synthetic owner subsystem failure")

        monkeypatch.setattr(run_scan, "_owner_monitoring_client", broken)
    output = tmp_path / "watch.jsonl"
    if path_kind == "watch_refresh":
        asyncio.run(run_scan.main(_watch_argv(path, output)))
        text = " ".join(json.loads(output.read_text(encoding="utf-8").splitlines()[0])["errors"])
    else:
        with pytest.raises(SystemExit) as raised:
            asyncio.run(run_scan.main(_strict_argv(path)))
        text = str(raised.value.code)
    assert "exchange-info source returned empty" in text
    assert CLEANUP_TEXT in text
    _assert_no_discovery(path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert stored.evaluation_cursor_close_at == before
    assert stored.terminal_outcome in (None, "N/A")
    if monitoring == "gap":
        assert "TimeoutError" in stored.diagnostic
        assert "owner_monitoring" not in text
    else:
        assert "synthetic owner subsystem failure" in text
        assert "exchange_market_data_unavailable" not in stored.diagnostic


def test_acquisition_and_cleanup_failures_keep_both_causes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, owner, before = _arm_owner(tmp_path, monkeypatch)
    acquisition = RuntimeError("synthetic ticker acquisition failure")
    _install_owned_binance(monkeypatch, "valid", acquire_error=acquisition)
    _capture_runner()
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: _KlineClient())

    with pytest.raises(SystemExit) as raised:
        asyncio.run(run_scan.main(_strict_argv(path)))

    cause = raised.value.__cause__
    assert isinstance(cause, UniverseResolutionError)
    assert cause.__cause__ is acquisition
    assert "Binance USDT perpetual source failed" in str(cause)
    assert "synthetic ticker acquisition failure" in str(cause)
    assert isinstance(cause.source_cleanup_error, RuntimeError)
    assert CLEANUP_TEXT in str(raised.value.code)
    assert discovery_failure_continues_owner_monitoring(raised.value) is True
    _assert_no_discovery(path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert stored.evaluation_cursor_close_at != before


@pytest.mark.parametrize("control_type", [asyncio.CancelledError, KeyboardInterrupt])
def test_binance_cleanup_does_not_replace_control_exception(
    monkeypatch: pytest.MonkeyPatch,
    control_type: type[BaseException],
) -> None:
    original = control_type("synthetic control exception")

    class FakeBinance:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def get_24h_tickers(self) -> list[dict[str, str]]:
            raise original

        async def get_exchange_info(self) -> dict[str, object]:
            return {"symbols": [_contract("BTC")]}

        async def aclose(self) -> None:
            raise RuntimeError(CLEANUP_TEXT)

    monkeypatch.setattr(symbol_universe, "BinanceFuturesClient", FakeBinance)
    with pytest.raises(control_type) as raised:
        asyncio.run(
            resolve_symbol_universe(
                BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
                universe_size=1,
                market_cap_fetcher=lambda: [_asset("BTC", 1)],
            )
        )
    assert raised.value is original
    assert isinstance(original.source_cleanup_error, RuntimeError)
    assert str(original.source_cleanup_error) == CLEANUP_TEXT
    assert discovery_failure_continues_owner_monitoring(raised.value) is False


@pytest.mark.parametrize("control_type", [asyncio.CancelledError, KeyboardInterrupt])
def test_ranking_client_cleanup_does_not_replace_control_exception(
    monkeypatch: pytest.MonkeyPatch,
    control_type: type[BaseException],
) -> None:
    original = control_type("synthetic ranking control exception")

    class FakeRankingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def get(self, *args: object, **kwargs: object) -> object:
            raise original

        async def aclose(self) -> None:
            raise RuntimeError(RANKING_CLEANUP_TEXT)

    monkeypatch.setattr(symbol_universe.httpx, "AsyncClient", FakeRankingClient)
    with pytest.raises(control_type) as raised:
        asyncio.run(
            resolve_symbol_universe(
                BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
                universe_size=1,
                ticker_fetcher=lambda: [_ticker("BTC")],
                exchange_info_fetcher=lambda: {"symbols": [_contract("BTC")]},
            )
        )
    assert raised.value is original
    assert isinstance(original.source_cleanup_error, RuntimeError)
    assert str(original.source_cleanup_error) == RANKING_CLEANUP_TEXT
    assert not isinstance(raised.value, UniverseResolutionError)
    assert discovery_failure_continues_owner_monitoring(raised.value) is False


def test_keyboard_interrupt_with_cleanup_does_not_start_monitoring(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, owner, before = _arm_owner(tmp_path, monkeypatch)
    original = KeyboardInterrupt("synthetic control exception")

    class FakeBinance:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def get_24h_tickers(self) -> list[dict[str, str]]:
            raise original

        async def get_exchange_info(self) -> dict[str, object]:
            return {"symbols": [_contract("BTC")]}

        async def aclose(self) -> None:
            raise RuntimeError(CLEANUP_TEXT)

    async def rankings() -> list[dict[str, object]]:
        return [_asset("BTC", 1)]

    calls: list[str] = []
    monkeypatch.setattr(symbol_universe, "fetch_coinpaprika_market_cap_rankings", rankings)
    monkeypatch.setattr(symbol_universe, "BinanceFuturesClient", FakeBinance)
    _capture_runner()
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: calls.append("client") or _KlineClient())

    with pytest.raises(KeyboardInterrupt) as raised:
        asyncio.run(run_scan.main(_strict_argv(path)))

    assert raised.value is original
    assert calls == []
    _assert_no_discovery(path)
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert stored.evaluation_cursor_close_at == before


def test_ranking_http_failure_keeps_status_when_cleanup_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    request = httpx.Request("GET", "https://coinpaprika.test/tickers")

    class FakeRankingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            del args, kwargs

        async def get(self, *args: object, **kwargs: object) -> httpx.Response:
            return httpx.Response(503, request=request)

        async def aclose(self) -> None:
            raise RuntimeError(RANKING_CLEANUP_TEXT)

    monkeypatch.setattr(symbol_universe.httpx, "AsyncClient", FakeRankingClient)
    with pytest.raises(UniverseResolutionError) as raised:
        asyncio.run(
            resolve_symbol_universe(
                BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
                universe_size=1,
                ticker_fetcher=lambda: [_ticker("BTC")],
                exchange_info_fetcher=lambda: {"symbols": [_contract("BTC")]},
            )
        )
    assert "HTTP 503" in str(raised.value)
    assert str(raised.value).index("HTTP 503") < str(raised.value).index("source_cleanup_error")
    assert isinstance(raised.value.source_cleanup_error, RuntimeError)
    assert RANKING_CLEANUP_TEXT in str(raised.value.source_cleanup_error)
    assert discovery_failure_continues_owner_monitoring(raised.value) is True


def test_owned_client_success_still_resolves_strict_intersection(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _install_owned_binance(monkeypatch, "valid", close_error=None)
    universe = asyncio.run(
        resolve_symbol_universe(BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE, universe_size=1)
    )
    assert universe.resolved_symbols == ("BTCUSDT",)
    assert universe.diagnostics["contract_metadata_used"] is True
    assert state["closed"] == 1


def test_builder_defect_with_cleanup_is_not_a_provider_outage(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, owner, before = _arm_owner(tmp_path, monkeypatch)

    def broken(_row: object) -> object:
        raise ValueError("synthetic internal arithmetic defect")

    monkeypatch.setattr(symbol_universe, "_market_cap_usd", broken)
    _install_owned_binance(monkeypatch, "valid")
    _capture_runner()
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    calls: list[str] = []
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: calls.append("client") or _KlineClient())

    with pytest.raises(SystemExit) as raised:
        asyncio.run(run_scan.main(_strict_argv(path)))

    assert isinstance(raised.value.__cause__, ValueError)
    assert "synthetic internal arithmetic defect" in str(raised.value)
    assert discovery_failure_continues_owner_monitoring(raised.value) is False
    assert calls == []
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert stored.evaluation_cursor_close_at == before
    assert isinstance(raised.value.__cause__.source_cleanup_error, RuntimeError)
