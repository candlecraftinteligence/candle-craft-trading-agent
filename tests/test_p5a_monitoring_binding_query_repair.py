"""Counterexamples for the P5A monitoring, binding, and query-scope repair.

These tests exercise the real monitoring layer. External candle acquisition and
ranking providers are faked. ``_continue_owned_plan_monitoring`` is not replaced.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import threading
from pathlib import Path

import pytest

from app.data.exceptions import ExchangeHTTPError
from app.lifecycle import owner_monitoring
from app.lifecycle.economic_identity import proven_progress_plan_version_id
from app.lifecycle.models import SetupLifecycleOutcomeProgress, SetupLifecycleState
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.owner_monitoring import (
    GAP_REQUIRED_CLOSED_CANDLE,
    SymbolMonitoringEvidence,
    evidence_key,
    monitor_obligations_with_market_data,
)
from app.lifecycle.plan_version_binding import (
    PLAN_VERSION_BINDING_AWAITING,
    PLAN_VERSION_BINDING_KEY,
    resolve_persisted_plan_version,
)
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import SetupLifecycleService
from app.universe.symbol_universe import (
    BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
    BINANCE_USDT_PERP_TOP_VOLUME_MODE,
    SymbolUniverse,
    UniverseResolutionError,
)
from scripts import run_scan
from tests.test_p5a_active_owner_monitoring import (
    TIMEFRAME,
    _apply,
    _candle,
    _continuation_candles,
    _decision,
    _evaluate,
    _event_count,
    _latched,
    _progress,
    _record,
    _rejected_result,
    _seed_entered,
)
from tests.test_watch_mode import SequenceWatchRunner, _bootstrap_watch_main


class _KlineClient:
    def __init__(self, *, candles=None, error: BaseException | None = None) -> None:
        self.calls: list[str] = []
        self._candles = list(candles if candles is not None else _continuation_candles())
        self._error = error

    async def get_klines(self, symbol: str, interval: str, limit: int) -> list[dict[str, object]]:
        del interval, limit
        self.calls.append(symbol)
        if self._error is not None:
            raise self._error
        return list(self._candles)

    async def aclose(self) -> None:
        return None


def _args(path: Path):
    return run_scan.parse_args(
        [
            "--symbols",
            "CAKEUSDT",
            "--database-path",
            str(path),
            "--lifecycle",
            "--execution-timeframe",
            TIMEFRAME,
        ]
    )


def _quiet_runtime(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("TELEGRAM_ADMIN_ENABLED", "false")
    monkeypatch.setenv("TELEGRAM_DRY_RUN", "true")
    monkeypatch.setenv("TELEGRAM_SIGNALS_ENABLED", "false")
    monkeypatch.setattr(run_scan, "SCAN_RUN_MANIFEST_PATH", tmp_path / "manifest.jsonl")
    monkeypatch.setattr(run_scan, "NIGHTLY_SCAN_HISTORY_PATH", tmp_path / "nightly.json")
    monkeypatch.setattr(run_scan, "LATEST_RUN_PATH", tmp_path / "latest.json")
    monkeypatch.setattr(run_scan, "PERFORMANCE_MEMORY_PATH", tmp_path / "performance_memory.json")


def _evidence(symbol: str, candles: list[dict[str, object]]) -> dict[str, SymbolMonitoringEvidence]:
    return {
        evidence_key(symbol, TIMEFRAME): SymbolMonitoringEvidence(
            candles=tuple(candles),
            execution_timeframe=TIMEFRAME,
            decision_timestamp=_decision(len(candles) - 1),
        )
    }


def _run_threads(workers: list) -> None:
    errors: list[BaseException] = []
    barrier = threading.Barrier(len(workers))

    def run(worker) -> None:
        try:
            barrier.wait(timeout=10)
            worker()
        except BaseException as exc:  # noqa: BLE001 - the test asserts the collection
            errors.append(exc)

    threads = [threading.Thread(target=run, args=(worker,)) for worker in workers]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()
    assert errors == []


def test_client_construction_bug_is_a_subsystem_failure(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "factory.db"
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)

    def broken_factory(args):
        del args
        raise ValueError("unexpected client construction bug")

    monkeypatch.setattr(run_scan, "_owner_monitoring_client", broken_factory)
    monkeypatch.setattr(run_scan, "_watch_iteration_timestamp", lambda: _decision(2))
    result = asyncio.run(run_scan._continue_owned_plan_monitoring(_args(path)))
    phases: dict[str, str] = {}
    errors: list[str] = []
    run_scan._record_owner_monitoring_phase(phases, errors, result=result)
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert phases["owner_monitoring"] == "PARTIAL"
    assert any("unexpected client construction bug" in error for error in errors)
    assert stored.diagnostic != GAP_REQUIRED_CLOSED_CANDLE
    assert "exchange_market_data_unavailable" not in stored.diagnostic


def test_fetch_adapter_bug_is_not_an_exchange_gap(tmp_path: Path) -> None:
    path = tmp_path / "adapter.db"
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)

    async def broken_fetch(symbol: str, timeframe: str, limit: int):
        del symbol, timeframe, limit
        raise RuntimeError("adapter blew up")

    result = asyncio.run(
        monitor_obligations_with_market_data(
            path,
            evidence_by_key={},
            fetch_candles=broken_fetch,
            execution_timeframe=TIMEFRAME,
            candle_limit=50,
            evaluated_at=_decision(2),
            scan_run_id="scan-adapter",
        )
    )
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert any("adapter blew up" in item["detail"] for item in result.errors)
    assert owner.lifecycle_id not in result.gap_lifecycle_ids
    assert "exchange_market_data_unavailable" not in stored.diagnostic


def test_timeout_and_unsupported_market_stay_gaps(tmp_path: Path) -> None:
    path = tmp_path / "gaps.db"
    with SQLiteSetupLifecycleRepository(path) as repository:
        timeout_owner = _seed_entered(repository, lifecycle_id="owner-timeout", symbol="CAKEUSDT")
        unsupported_owner = _seed_entered(repository, lifecycle_id="owner-delist", symbol="INJUSDT")

    async def fetch(symbol: str, timeframe: str, limit: int):
        del timeframe, limit
        if symbol == "INJUSDT":
            raise ExchangeHTTPError("Invalid symbol", status_code=400)
        raise TimeoutError("exchange timed out")

    result = asyncio.run(
        monitor_obligations_with_market_data(
            path,
            evidence_by_key={},
            fetch_candles=fetch,
            execution_timeframe=TIMEFRAME,
            candle_limit=50,
            evaluated_at=_decision(2),
            scan_run_id="scan-gaps",
        )
    )
    with SQLiteSetupLifecycleRepository(path) as repository:
        timed_out = _progress(repository, timeout_owner.lifecycle_id)
        unsupported = _progress(repository, unsupported_owner.lifecycle_id)
    assert result.errors == ()
    assert "TimeoutError" in timed_out.diagnostic
    assert unsupported.diagnostic == "market_unsupported_or_delisted"
    assert timed_out.terminal_outcome in (None, "N/A")
    assert unsupported.terminal_outcome in (None, "N/A")


def test_existing_legacy_null_rejects_supplied_id_and_rewritten_marker(tmp_path: Path) -> None:
    path = tmp_path / "legacy.db"
    record = _latched()
    progress = SetupLifecycleOutcomeProgress(
        lifecycle_id=record.lifecycle_id,
        plan_identity=canonical_plan_identity(record),
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        execution_timeframe=TIMEFRAME,
        first_evaluated_at=_decision(0),
        last_evaluated_at=_decision(0),
        metadata_json=json.dumps({"source": "legacy-reconstruction"}),
    )
    attacked = progress.model_copy(
        update={
            "plan_version_id": record.plan_version_id,
            "metadata_json": json.dumps(
                {"source": "legacy-reconstruction", PLAN_VERSION_BINDING_KEY: PLAN_VERSION_BINDING_AWAITING}
            ),
        }
    )
    with SQLiteSetupLifecycleRepository(path) as repository:
        repository.upsert_record(record)
        repository.upsert_outcome_progress(progress)
        repository.upsert_outcome_progress(attacked)
        stored = _progress(repository, record.lifecycle_id)
    metadata = json.loads(stored.metadata_json)
    assert stored.plan_version_id is None
    assert PLAN_VERSION_BINDING_KEY not in metadata


def test_geometry_mismatch_cannot_bind_at_repository_boundary(tmp_path: Path) -> None:
    path = tmp_path / "geometry.db"
    record = _latched()
    other_geometry = record.model_copy(update={"tp1": "91"})
    progress = SetupLifecycleOutcomeProgress(
        lifecycle_id=record.lifecycle_id,
        plan_identity=canonical_plan_identity(other_geometry),
        plan_version_id=record.plan_version_id,
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        execution_timeframe=TIMEFRAME,
        first_evaluated_at=_decision(0),
        last_evaluated_at=_decision(0),
    )
    with SQLiteSetupLifecycleRepository(path) as repository:
        repository.upsert_record(record)
        repository.upsert_outcome_progress(progress)
        stored = _progress(repository, record.lifecycle_id)
    assert stored.plan_identity != canonical_plan_identity(record)
    assert stored.plan_version_id is None


def test_new_row_attribution_bind_forward_immutable_retry_and_foreign_lifecycle(tmp_path: Path) -> None:
    path = tmp_path / "binding.db"
    record = _latched(lifecycle_id="life-owned")
    created = SetupLifecycleOutcomeProgress(
        lifecycle_id=record.lifecycle_id,
        plan_identity=canonical_plan_identity(record),
        plan_version_id=record.plan_version_id,
        symbol=record.symbol,
        mode=record.mode,
        direction=record.direction,
        execution_timeframe=TIMEFRAME,
        first_evaluated_at=_decision(0),
        last_evaluated_at=_decision(0),
    )
    foreign = _latched(lifecycle_id="life-foreign", symbol="INJUSDT")
    foreign_progress = created.model_copy(
        update={"lifecycle_id": record.lifecycle_id, "plan_version_id": foreign.plan_version_id}
    )
    with SQLiteSetupLifecycleRepository(path) as repository:
        repository.upsert_record(record)
        repository.upsert_outcome_progress(created)
        assert _progress(repository, record.lifecycle_id).plan_version_id == record.plan_version_id
        repository.upsert_outcome_progress(
            created.model_copy(update={"plan_version_id": "plan-version-retry"})
        )
        assert _progress(repository, record.lifecycle_id).plan_version_id == record.plan_version_id
        rejected = resolve_persisted_plan_version(foreign_progress, foreign, None)
    assert rejected.plan_version_id is None

    unlatched = _record(lifecycle_id="life-forward", symbol="KASUSDT")
    with SQLiteSetupLifecycleRepository(path) as repository:
        repository.upsert_record(unlatched)
        _evaluate(repository, unlatched, [_candle(0, high="99", low="95")])
        _evaluate(
            repository,
            unlatched,
            [_candle(0, high="99", low="95"), _candle(1, high="99", low="95")],
        )
        waiting = _progress(repository, unlatched.lifecycle_id)
        assert waiting.plan_version_id is None
        assert json.loads(waiting.metadata_json)[PLAN_VERSION_BINDING_KEY] == PLAN_VERSION_BINDING_AWAITING
        latched = repository.upsert_record(
            unlatched.model_copy(update={})
        )
        del latched
        locked = _latched(lifecycle_id="life-forward", symbol="KASUSDT")
        repository.upsert_record(locked)
        _evaluate(
            repository,
            locked,
            [
                _candle(0, high="99", low="95"),
                _candle(1, high="99", low="95"),
                _candle(2, high="103", low="99"),
            ],
        )
        stored = _progress(repository, locked.lifecycle_id)
    assert stored.plan_version_id == proven_progress_plan_version_id(locked)


def test_shared_symbol_first_pass_failure_is_not_rewritten_as_a_gap(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "shared.db"
    with SQLiteSetupLifecycleRepository(path) as repository:
        failed = _seed_entered(repository, lifecycle_id="owner-failed", mode="scalp")
        _seed_entered(repository, lifecycle_id="owner-ok", mode="swing")
    original = owner_monitoring.evaluate_closed_candle_outcomes

    def fail_one(record, **kwargs):
        if record.lifecycle_id == failed.lifecycle_id:
            raise RuntimeError("economic evaluation bug")
        return original(record, **kwargs)

    monkeypatch.setattr(owner_monitoring, "evaluate_closed_candle_outcomes", fail_one)
    first = _apply(path, _rejected_result("CAKEUSDT", tuple(_continuation_candles()), mode="scalp", bias=None))
    covered, evaluated = run_scan._owner_monitoring_skip(first)
    assert first.scanner_process_summary["owner_monitoring"]["errors"]

    def forbid_client(args):
        del args
        raise AssertionError("covered key should not request a client")

    monkeypatch.setattr(run_scan, "_owner_monitoring_client", forbid_client)
    monkeypatch.setattr(run_scan, "_watch_iteration_timestamp", lambda: _decision(2))
    continuation = asyncio.run(
        run_scan._continue_owned_plan_monitoring(
            _args(path),
            evidence_by_key=run_scan.evidence_from_symbol_results(
                first.results,
                decision_fallback=_decision(2),
            ),
            skip_keys=covered,
            skip_lifecycle_ids=evaluated,
        )
    )
    phases: dict[str, str] = {}
    errors: list[str] = []
    run_scan._record_owner_monitoring_phase(
        phases,
        errors,
        result=continuation,
        prior_errors=run_scan._prior_owner_monitoring_errors(first),
    )
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, failed.lifecycle_id)
    assert phases["owner_monitoring"] == "PARTIAL"
    assert any("economic evaluation bug" in error for error in errors)
    assert stored.diagnostic != GAP_REQUIRED_CLOSED_CANDLE


def test_first_pass_failure_survives_a_successful_continuation(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "survive.db"
    with SQLiteSetupLifecycleRepository(path) as repository:
        failed = _seed_entered(repository, lifecycle_id="owner-failed", mode="scalp")
        _seed_entered(repository, lifecycle_id="owner-ok", mode="swing")
    original = owner_monitoring.evaluate_closed_candle_outcomes
    seen: set[str] = set()

    def fail_first(record, **kwargs):
        if record.lifecycle_id == failed.lifecycle_id and record.lifecycle_id not in seen:
            seen.add(record.lifecycle_id)
            raise RuntimeError("economic evaluation bug")
        return original(record, **kwargs)

    monkeypatch.setattr(owner_monitoring, "evaluate_closed_candle_outcomes", fail_first)
    first = _apply(path, _rejected_result("CAKEUSDT", tuple(_continuation_candles()), mode="scalp", bias=None))
    covered, evaluated = run_scan._owner_monitoring_skip(first)
    monkeypatch.setattr(run_scan, "_watch_iteration_timestamp", lambda: _decision(2))
    continuation = asyncio.run(
        run_scan._continue_owned_plan_monitoring(
            _args(path),
            evidence_by_key=run_scan.evidence_from_symbol_results(
                first.results,
                decision_fallback=_decision(2),
            ),
            skip_keys=covered,
            skip_lifecycle_ids=evaluated,
        )
    )
    assert continuation.errors == ()
    phases: dict[str, str] = {}
    errors: list[str] = []
    run_scan._record_owner_monitoring_phase(
        phases,
        errors,
        result=continuation,
        prior_errors=run_scan._prior_owner_monitoring_errors(first),
    )
    assert phases["owner_monitoring"] == "PARTIAL"
    assert any("economic evaluation bug" in error for error in errors)
    with pytest.raises(SystemExit, match="economic evaluation bug"):
        run_scan._raise_one_shot_owner_monitoring_failure(
            result=continuation,
            prior_errors=run_scan._prior_owner_monitoring_errors(first),
        )


@pytest.mark.parametrize("watch", [False, True])
def test_real_main_shared_owner_failure_is_visible(tmp_path: Path, monkeypatch, watch: bool) -> None:
    _quiet_runtime(monkeypatch, tmp_path)
    path, _epoch = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    with SQLiteSetupLifecycleRepository(path) as repository:
        failed = _seed_entered(repository, lifecycle_id="owner-failed", mode="scalp")
        _seed_entered(repository, lifecycle_id="owner-ok", mode="swing")
    original = owner_monitoring.evaluate_closed_candle_outcomes

    def fail_one(record, **kwargs):
        if record.lifecycle_id == failed.lifecycle_id:
            raise RuntimeError("economic evaluation bug")
        return original(record, **kwargs)

    monkeypatch.setattr(owner_monitoring, "evaluate_closed_candle_outcomes", fail_one)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [
        (_rejected_result("CAKEUSDT", tuple(_continuation_candles()), mode="scalp", bias=None),)
    ]

    def forbid_client(args):
        del args
        raise AssertionError("unexpected external fetch")

    monkeypatch.setattr(run_scan, "_owner_monitoring_client", forbid_client)
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
        assert any("economic evaluation bug" in error for error in summary["errors"])
    else:
        with pytest.raises(SystemExit, match="owner_monitoring"):
            asyncio.run(run_scan.main(args))
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, failed.lifecycle_id)
    assert stored.diagnostic != GAP_REQUIRED_CLOSED_CANDLE


def _ranking_universe() -> SymbolUniverse:
    return SymbolUniverse(
        mode=BINANCE_USDT_PERP_TOP_VOLUME_MODE,
        requested_size=1,
        resolved_symbols=("BTCUSDT",),
        excluded_symbols=(),
        source="test",
        generated_at="2026-09-27T00:00:00+00:00",
    )


async def _ranking_resolution(args, *, fail_after: int):
    if getattr(_ranking_resolution, "calls", 0) < fail_after:
        _ranking_resolution.calls = getattr(_ranking_resolution, "calls", 0) + 1
        return run_scan.WatchlistResolution(
            symbols=("BTCUSDT",),
            source_label="test-universe",
            universe=_ranking_universe(),
        )
    failure = SystemExit("ranking provider failed")
    failure.__cause__ = UniverseResolutionError("ranking provider failed")
    raise failure


@pytest.mark.parametrize("outcome", ["success", "gap", "unexpected"])
def test_real_main_ranking_failure_keeps_monitoring_outcome(tmp_path: Path, monkeypatch, outcome: str) -> None:
    _quiet_runtime(monkeypatch, tmp_path)
    path, _epoch = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)
        before = _progress(repository, owner.lifecycle_id).evaluation_cursor_close_at
    client = _KlineClient(error=TimeoutError("exchange timed out") if outcome == "gap" else None)
    if outcome == "unexpected":
        def broken_factory(args):
            del args
            raise ValueError("unexpected client construction bug")

        monkeypatch.setattr(run_scan, "_owner_monitoring_client", broken_factory)
    else:
        monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: client)
    _ranking_resolution.calls = 0

    async def resolve(args):
        return await _ranking_resolution(args, fail_after=0)

    monkeypatch.setattr(run_scan, "_resolve_universe_watchlist", resolve)
    args = [
        "--universe",
        BINANCE_USDT_PERP_TOP_VOLUME_MODE,
        "--database-path",
        str(path),
        "--lifecycle",
        "--execution-timeframe",
        TIMEFRAME,
    ]
    with pytest.raises(SystemExit) as caught:
        asyncio.run(run_scan.main(args))
    message = str(caught.value.code)
    assert "ranking provider failed" in message
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    if outcome == "success":
        assert "owner_monitoring" not in message
        assert stored.evaluation_cursor_close_at != before
    elif outcome == "gap":
        assert "owner_monitoring" not in message
        assert "TimeoutError" in stored.diagnostic
        assert stored.terminal_outcome in (None, "N/A")
    else:
        assert "owner_monitoring" in message
        assert "unexpected client construction bug" in message
        assert "exchange_market_data_unavailable" not in stored.diagnostic


@pytest.mark.parametrize("outcome", ["success", "gap", "unexpected"])
def test_real_watch_ranking_failure_keeps_both_facts(tmp_path: Path, monkeypatch, outcome: str) -> None:
    _quiet_runtime(monkeypatch, tmp_path)
    path, _epoch = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)
    client = _KlineClient(error=TimeoutError("exchange timed out") if outcome == "gap" else None)
    if outcome == "unexpected":
        def broken_factory(args):
            del args
            raise ValueError("unexpected client construction bug")

        monkeypatch.setattr(run_scan, "_owner_monitoring_client", broken_factory)
    else:
        monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: client)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = []
    _ranking_resolution.calls = 0

    async def resolve(args):
        return await _ranking_resolution(args, fail_after=1)

    monkeypatch.setattr(run_scan, "_resolve_universe_watchlist", resolve)
    output = tmp_path / "watch.jsonl"
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
                "--watch",
                "--watch-max-iterations",
                "1",
                "--watch-interval-sec",
                "0.01",
                "--watch-output-file",
                str(output),
            ]
        )
    )
    summary = json.loads(output.read_text(encoding="utf-8").splitlines()[0])
    assert any("ranking provider failed" in error for error in summary["errors"])
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    if outcome == "unexpected":
        assert summary["phase_statuses"]["owner_monitoring"] == "PARTIAL"
        assert any("unexpected client construction bug" in error for error in summary["errors"])
        assert "exchange_market_data_unavailable" not in stored.diagnostic
    else:
        assert summary["phase_statuses"].get("owner_monitoring") != "PARTIAL"
        assert not any(error.startswith("owner_monitoring:") for error in summary["errors"])
        if outcome == "gap":
            assert "TimeoutError" in stored.diagnostic


def test_owned_symbol_outside_strict_membership_is_still_monitored(tmp_path: Path, monkeypatch) -> None:
    _quiet_runtime(monkeypatch, tmp_path)
    path, _epoch = _bootstrap_watch_main(tmp_path, monkeypatch, prior_state=False)
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository, symbol="CAKEUSDT")
        before = _progress(repository, owner.lifecycle_id).evaluation_cursor_close_at
    universe = SymbolUniverse(
        mode=BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
        requested_size=1,
        resolved_symbols=("BTCUSDT",),
        excluded_symbols=(),
        source="test",
        generated_at="2026-09-27T00:00:00+00:00",
    )

    async def resolve(args):
        del args
        return run_scan.WatchlistResolution(
            symbols=("BTCUSDT",),
            source_label="strict-test",
            universe=universe,
        )

    client = _KlineClient()
    monkeypatch.setattr(run_scan, "_resolve_universe_watchlist", resolve)
    monkeypatch.setattr(run_scan, "_owner_monitoring_client", lambda args: client)
    monkeypatch.setattr(run_scan, "ScannerRunner", SequenceWatchRunner)
    SequenceWatchRunner.configs = []
    SequenceWatchRunner.results_by_call = [
        (_rejected_result("BTCUSDT", tuple(_continuation_candles()), mode="scalp", bias=None),)
    ]
    asyncio.run(
        run_scan.main(
            [
                "--universe",
                BINANCE_USDT_PERP_TOP_MARKET_CAP_MODE,
                "--database-path",
                str(path),
                "--lifecycle",
                "--execution-timeframe",
                TIMEFRAME,
            ]
        )
    )
    assert SequenceWatchRunner.configs
    assert "CAKEUSDT" not in SequenceWatchRunner.configs[0].symbols
    assert "CAKEUSDT" in client.calls
    with SQLiteSetupLifecycleRepository(path) as repository:
        stored = _progress(repository, owner.lifecycle_id)
    assert stored.evaluation_cursor_close_at != before


def test_candidate_query_plan_and_rejection_scale(tmp_path: Path) -> None:
    path = tmp_path / "query-scale.db"
    with SQLiteSetupLifecycleRepository(path) as repository:
        _seed_entered(repository)
        repository.upsert_record(
            _record(lifecycle_id="rejection-template", symbol="REJECTUSDT", state=SetupLifecycleState.REJECTED)
        )

        def measure() -> int:
            ticks = [0]

            def tick() -> int:
                ticks[0] += 1
                return 0

            repository.connection.set_progress_handler(tick, 100)
            owners = owner_monitoring.select_tracking_obligations(repository)
            repository.connection.set_progress_handler(None, 0)
            assert len(owners) == 1
            return ticks[0]

        before = measure()
        columns = [
            row[1]
            for row in repository.connection.execute("PRAGMA table_info(setup_lifecycle_records)")
            if row[1] != "id"
        ]
        expressions = [
            "r.lifecycle_id || '-' || n"
            if column == "lifecycle_id"
            else "0"
            if column == "is_current"
            else 'r."' + column + '"'
            for column in columns
        ]
        repository.connection.execute(
            "WITH RECURSIVE numbers(n) AS (SELECT 1 UNION ALL SELECT n+1 FROM numbers WHERE n<5000) "
            + "INSERT INTO setup_lifecycle_records ("
            + ",".join('"' + column + '"' for column in columns)
            + ") SELECT "
            + ",".join(expressions)
            + " FROM setup_lifecycle_records r CROSS JOIN numbers WHERE r.lifecycle_id='rejection-template'"
        )
        captured: list[str] = []
        repository.connection.set_trace_callback(captured.append)
        owners = owner_monitoring.select_tracking_obligations(repository)
        repository.connection.set_trace_callback(None)
        query = next(sql for sql in captured if "SELECT * FROM (" in sql)
        plan = [tuple(row) for row in repository.connection.execute("EXPLAIN QUERY PLAN " + query)]
        after = measure()
    assert len(owners) == 1
    plan_text = "\n".join(str(row) for row in plan)
    assert "CROSS JOIN" in query
    assert "setup_lifecycle_outcome_progress" in query
    assert "INDEXED BY ix_lifecycle_records_epoch_locked_plan_state" in query
    assert "ix_lifecycle_records_epoch_locked_plan_state" in plan_text
    assert "lifecycle_id=?" in plan_text
    assert "ix_lifecycle_records_runtime_epoch" not in plan_text
    assert after <= max(before * 10, 100)


def test_concurrent_monitor_discovery_bind_and_terminal_retry(tmp_path: Path) -> None:
    path = tmp_path / "concurrent.db"
    with SQLiteSetupLifecycleRepository(path) as repository:
        owner = _seed_entered(repository)
        events_before = _event_count(repository, owner.lifecycle_id)
    candles = _continuation_candles()
    evidence = _evidence(owner.symbol, candles)
    control = tmp_path / "control.db"
    shutil.copy(path, control)

    async def monitor(database: Path):
        return await monitor_obligations_with_market_data(
            database,
            evidence_by_key=evidence,
            fetch_candles=None,
            execution_timeframe=TIMEFRAME,
            candle_limit=50,
            evaluated_at=_decision(2),
            scan_run_id="scan-concurrent",
            record_missing_evidence=False,
        )

    asyncio.run(monitor(control))
    with SQLiteSetupLifecycleRepository(control) as repository:
        control_events = _event_count(repository, owner.lifecycle_id)
        control_progress = _progress(repository, owner.lifecycle_id)

    symbol_result = _rejected_result("CAKEUSDT", tuple(candles), mode="scalp", bias=None)

    def apply_discovery() -> None:
        SetupLifecycleService(path).apply_to_run_result(
            run_scan.ScannerRunResult(
                config=symbol_result and _apply_config(),
                results=(symbol_result,),
                scanned_symbols=1,
                failed_symbols=0,
                trade_ideas_created=0,
                dry_run_alerts_created=0,
                journal_entries_created=0,
            ),
            scan_run_id="scan-discovery",
            now=_decision(3),
        )

    _run_threads(
        [
            apply_discovery,
            lambda: asyncio.run(monitor(path)),
        ]
    )
    with SQLiteSetupLifecycleRepository(path) as repository:
        assert _event_count(repository, owner.lifecycle_id) == control_events
        stored = _progress(repository, owner.lifecycle_id)
    assert stored.plan_version_id == control_progress.plan_version_id
    assert stored.terminal_outcome == control_progress.terminal_outcome
    assert events_before <= control_events

    forward = tmp_path / "forward.db"
    unlatched = _record(lifecycle_id="life-forward", symbol="KASUSDT")
    with SQLiteSetupLifecycleRepository(forward) as repository:
        repository.upsert_record(unlatched)
        _evaluate(repository, unlatched, [_candle(0, high="99", low="95")])
        locked = _latched(lifecycle_id="life-forward", symbol="KASUSDT")
        repository.upsert_record(locked)
    entry_evidence = _evidence("KASUSDT", [_candle(0, high="99", low="95"), _candle(1, high="103", low="99")])

    async def bind(database: Path):
        return await monitor_obligations_with_market_data(
            database,
            evidence_by_key=entry_evidence,
            fetch_candles=None,
            execution_timeframe=TIMEFRAME,
            candle_limit=50,
            evaluated_at=_decision(1),
            scan_run_id="scan-bind",
            record_missing_evidence=False,
        )

    _run_threads([lambda: asyncio.run(bind(forward)), lambda: asyncio.run(bind(forward))])
    with SQLiteSetupLifecycleRepository(forward) as repository:
        bound = _progress(repository, locked.lifecycle_id)
        assert len(repository.list_outcome_progress(lifecycle_id=locked.lifecycle_id)) == 1
    assert bound.plan_version_id == proven_progress_plan_version_id(locked)

    terminal = tmp_path / "terminal.db"
    with SQLiteSetupLifecycleRepository(terminal) as repository:
        entered = _seed_entered(repository, symbol="POLUSDT")
    stop_evidence = _evidence(
        "POLUSDT",
        [_candle(0, high="107", low="103"), _candle(1, high="103", low="99"), _candle(2, high="113", low="100")],
    )

    async def hit_stop(database: Path):
        return await monitor_obligations_with_market_data(
            database,
            evidence_by_key=stop_evidence,
            fetch_candles=None,
            execution_timeframe=TIMEFRAME,
            candle_limit=50,
            evaluated_at=_decision(2),
            scan_run_id="scan-stop",
            record_missing_evidence=False,
        )

    _run_threads([lambda: asyncio.run(hit_stop(terminal)), lambda: asyncio.run(hit_stop(terminal))])
    asyncio.run(hit_stop(terminal))
    with SQLiteSetupLifecycleRepository(terminal) as repository:
        stopped = _progress(repository, entered.lifecycle_id)
        stop_events = [
            event
            for event in repository.list_events(lifecycle_id=entered.lifecycle_id)
            if event.to_state == SetupLifecycleState.SL_HIT
        ]
    assert stopped.terminal_outcome == "SL_HIT"
    assert len(stop_events) == 1


def _apply_config():
    from decimal import Decimal

    from app.pipeline.scanner_runner import ScannerRunConfig

    return ScannerRunConfig.model_validate(
        {
            "symbols": ["CAKEUSDT"],
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
            "execution_timeframe": TIMEFRAME,
        }
    )
