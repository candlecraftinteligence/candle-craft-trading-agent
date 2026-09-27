"""Unexpected owner-monitoring failures stay visible. Market-data gaps do not."""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.universe.symbol_universe import UniverseResolutionError
from scripts import run_scan


def _discovery_exit() -> SystemExit:
    failure = SystemExit("universe_error: market-cap source failed: HTTP 503")
    failure.__cause__ = UniverseResolutionError("market-cap source failed: HTTP 503")
    return failure


def _summary(error: BaseException):
    return run_scan._failed_watch_iteration_summary(
        iteration=1,
        status="FAILED",
        watchlist=SimpleNamespace(symbols=(), active_lifecycle_symbols=()),
        iteration_error=error,
        scheduled_start_at="2026-09-27T00:00:00+00:00",
        actual_start_at="2026-09-27T00:00:00+00:00",
        schedule=SimpleNamespace(
            duration_seconds=1.0,
            sleep_seconds=0.0,
            cadence_lag_seconds=0.0,
            overrun_seconds=0.0,
            missed_interval_count=0,
        ),
        failure_streak=1,
        selected_backoff=0.0,
    )


def test_ranking_failure_with_successful_monitoring_keeps_discovery_error(monkeypatch) -> None:
    discovery = _discovery_exit()

    async def resolve(*args, **kwargs):
        del args, kwargs
        raise discovery

    async def monitor(*args, **kwargs):
        del args, kwargs
        return SimpleNamespace(errors=())

    monkeypatch.setattr(run_scan, "_watchlist_for_watch_iteration", resolve)
    monkeypatch.setattr(run_scan, "_continue_owned_plan_monitoring", monitor)
    watchlist, execution, error = asyncio.run(
        run_scan._attempt_watch_scan_iteration(
            SimpleNamespace(),
            startup_watchlist=SimpleNamespace(symbols=(), active_lifecycle_symbols=()),
            config=SimpleNamespace(),
            iteration=1,
        )
    )
    assert watchlist is None and execution is None
    assert error is discovery
    assert discovery.code == "universe_error: market-cap source failed: HTTP 503"
    assert getattr(discovery, "owner_monitoring_failure_lines", ()) == ()
    summary = _summary(discovery)
    assert summary.status == "FAILED"
    assert summary.phase_statuses == {"iteration": "FAILED"}
    assert len(summary.errors) == 1
    assert "universe_error" in summary.errors[0]
    assert "owner_monitoring" not in summary.errors[0]


def test_ranking_failure_and_monitoring_raise_are_both_visible(monkeypatch, capsys) -> None:
    discovery = _discovery_exit()

    async def resolve(*args, **kwargs):
        del args, kwargs
        raise discovery

    async def monitor(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("owner cursor store failed")

    monkeypatch.setattr(run_scan, "_watchlist_for_watch_iteration", resolve)
    monkeypatch.setattr(run_scan, "_continue_owned_plan_monitoring", monitor)
    _watchlist, _execution, error = asyncio.run(
        run_scan._attempt_watch_scan_iteration(
            SimpleNamespace(),
            startup_watchlist=SimpleNamespace(symbols=(), active_lifecycle_symbols=()),
            config=SimpleNamespace(),
            iteration=2,
        )
    )
    assert error is discovery
    assert discovery.code == "universe_error: market-cap source failed: HTTP 503"
    assert isinstance(discovery.owner_monitoring_error, RuntimeError)
    summary = _summary(discovery)
    assert summary.phase_statuses["iteration"] == "FAILED"
    assert summary.phase_statuses["owner_monitoring"] == "PARTIAL"
    assert "universe_error" in summary.errors[0]
    assert summary.errors[1] == "owner_monitoring:RuntimeError:owner cursor store failed"
    assert "owner_monitoring:RuntimeError:owner cursor store failed" in capsys.readouterr().out


def test_startup_discovery_exit_keeps_both_failures(monkeypatch, capsys) -> None:
    discovery = _discovery_exit()

    async def monitor(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("owner cursor store failed")

    monkeypatch.setattr(run_scan, "_continue_owned_plan_monitoring", monitor)
    asyncio.run(run_scan._surface_monitoring_during_discovery_failure(SimpleNamespace(), discovery))
    assert str(discovery.code).startswith("universe_error: market-cap source failed: HTTP 503")
    assert "owner_monitoring:RuntimeError:owner cursor store failed" in str(discovery.code)
    assert "owner_monitoring:RuntimeError:owner cursor store failed" in capsys.readouterr().out


def test_watch_iteration_records_partial_owner_monitoring() -> None:
    phases: dict[str, str] = {}
    errors: list[str] = []
    run_scan._record_owner_monitoring_phase(phases, errors, exc=RuntimeError("broken invariant"))
    assert phases["owner_monitoring"] == "PARTIAL"
    assert errors == ["owner_monitoring:RuntimeError:broken invariant"]


def test_successful_owner_monitoring_does_not_create_an_error() -> None:
    phases: dict[str, str] = {}
    errors: list[str] = []
    run_scan._record_owner_monitoring_phase(phases, errors, result=SimpleNamespace(errors=()))
    assert phases == {"owner_monitoring": "SUCCESS"}
    assert errors == []
    run_scan._raise_one_shot_owner_monitoring_failure(result=SimpleNamespace(errors=()))


def test_one_shot_monitoring_failure_is_not_discarded() -> None:
    with pytest.raises(SystemExit, match="owner_monitoring:RuntimeError:boom") as raised:
        run_scan._raise_one_shot_owner_monitoring_failure(RuntimeError("boom"))
    assert "pass" not in str(raised.value)
    with pytest.raises(SystemExit, match="owner_monitoring:CAKEUSDT:RuntimeError:broken"):
        run_scan._raise_one_shot_owner_monitoring_failure(
            result=SimpleNamespace(errors=({"symbol": "CAKEUSDT", "detail": "RuntimeError:broken"},))
        )


def test_owner_monitoring_call_sites_do_not_swallow_exceptions() -> None:
    text = Path("scripts/run_scan.py").read_text(encoding="utf-8")
    assert re.search(r"except Exception:\s+pass", text) is None
    assert "_surface_monitoring_during_discovery_failure" in text
    assert "_raise_one_shot_owner_monitoring_failure" in text
    assert "_record_owner_monitoring_phase" in text
