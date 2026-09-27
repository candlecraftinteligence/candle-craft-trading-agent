"""A missing local TLS trust file is a setup failure, not an exchange gap."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.universe.symbol_universe import BINANCE_USDT_PERP_TOP_VOLUME_MODE, UniverseResolutionError
from scripts import run_scan
from tests.test_p5a_active_owner_monitoring import TIMEFRAME, _decision, _progress, _rejected_result, _seed_entered
from tests.test_p5a_monitoring_binding_query_repair import _args, _quiet_runtime
from tests.test_watch_mode import SequenceWatchRunner, _bootstrap_watch_main


def _missing_ca(tmp_path: Path, monkeypatch) -> Path:
    missing = tmp_path / "nonexistent-ca-bundle.pem"
    assert not missing.exists()
    monkeypatch.setenv("SSL_CERT_FILE", str(missing))
    return missing


def test_missing_local_ca_file_is_subsystem_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "local-config.db"
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)
        before_cursor = _progress(repository, owner.lifecycle_id).evaluation_cursor_close_at
    _missing_ca(tmp_path, monkeypatch)
    monkeypatch.setattr(run_scan, "_watch_iteration_timestamp", lambda: _decision(2))
    with pytest.raises(FileNotFoundError):
        run_scan._owner_monitoring_client(_args(path))
    result = asyncio.run(run_scan._continue_owned_plan_monitoring(_args(path)))
    phases: dict[str, str] = {}
    errors: list[str] = []
    run_scan._record_owner_monitoring_phase(phases, errors, result=result)
    with SQLiteSetupLifecycleRepository(path) as repository:
        progress = _progress(repository, owner.lifecycle_id)
    assert phases["owner_monitoring"] == "PARTIAL"
    assert any("FileNotFoundError" in error for error in errors)
    assert "exchange_market_data_unavailable" not in progress.diagnostic
    assert progress.evaluation_cursor_close_at == before_cursor
    assert progress.terminal_outcome in (None, "N/A")


def test_ranking_failure_and_missing_local_ca_report_both(tmp_path: Path, monkeypatch) -> None:
    _quiet_runtime(monkeypatch, tmp_path)
    path, _epoch = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)
        before_cursor = _progress(repository, owner.lifecycle_id).evaluation_cursor_close_at
    _missing_ca(tmp_path, monkeypatch)

    async def fail_ranking(args):
        del args
        try:
            raise UniverseResolutionError("ranking source unavailable")
        except UniverseResolutionError as cause:
            raise SystemExit("ranking source unavailable") from cause

    monkeypatch.setattr(run_scan, "_resolve_universe_watchlist", fail_ranking)
    with pytest.raises(SystemExit) as raised:
        asyncio.run(
            run_scan.main(
                [
                    "--universe",
                    BINANCE_USDT_PERP_TOP_VOLUME_MODE,
                    "--database-path",
                    str(path),
                    "--lifecycle",
                    "--execution-timeframe",
                    TIMEFRAME,
                ]
            )
        )
    message = str(raised.value.code)
    assert "ranking source unavailable" in message
    assert "owner_monitoring" in message
    assert "FileNotFoundError" in message
    with SQLiteSetupLifecycleRepository(path) as repository:
        progress = _progress(repository, owner.lifecycle_id)
    assert "exchange_market_data_unavailable" not in progress.diagnostic
    assert progress.evaluation_cursor_close_at == before_cursor


@pytest.mark.parametrize("watch", [False, True])
def test_real_main_missing_local_ca_must_be_visible(tmp_path: Path, monkeypatch, watch: bool) -> None:
    _quiet_runtime(monkeypatch, tmp_path)
    path, _epoch = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)
    _missing_ca(tmp_path, monkeypatch)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [(_rejected_result("CAKEUSDT", (), mode="scalp", bias=None),)]
    args = [
        "--symbols",
        "CAKEUSDT",
        "--database-path",
        str(path),
        "--lifecycle",
        "--execution-timeframe",
        TIMEFRAME,
    ]
    if watch:
        output = tmp_path / "watch.jsonl"
        args += [
            "--watch",
            "--watch-max-iterations",
            "1",
            "--watch-interval-sec",
            "0.01",
            "--watch-output-file",
            str(output),
        ]
        asyncio.run(run_scan.main(args))
        summary = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
        assert summary["phase_statuses"]["owner_monitoring"] == "PARTIAL"
        assert any("FileNotFoundError" in error for error in summary["errors"])
    else:
        with pytest.raises(SystemExit, match="owner_monitoring") as raised:
            asyncio.run(run_scan.main(args))
        assert "FileNotFoundError" in str(raised.value)
    with SQLiteSetupLifecycleRepository(path) as repository:
        progress = _progress(repository, owner.lifecycle_id)
    assert "exchange_market_data_unavailable" not in progress.diagnostic
