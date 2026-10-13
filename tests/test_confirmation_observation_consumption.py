"""Synthetic production-orchestration proofs; no automatic origin stamping."""

import asyncio
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest

from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.service import SetupLifecycleService
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.runtime_epoch.errors import RuntimeEpochOriginError
from app.runtime_epoch.origin import register_operational_scan_run
from tests.test_economic_identity import _confirmed_candidate, _scan_result
from tests.test_prospective_runtime_epoch_isolation_boundaries import _live_adapter_delivery
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY, bootstrap_operational_test_database
from tests.test_prospective_runtime_epoch_isolation_repair4 import (
    _identity_cli,
    _patch_production_orchestration,
    _public_min_rr_pullback_candles,
    run_scan,
)
from tests.test_telegram_lifecycle_delivery_phase42 import FakeSender, _setup_quality, _trade_idea

pytestmark = pytest.mark.no_auto_epoch

BASE = datetime(2026, 3, 1, 13, tzinfo=UTC)


def _candidate(*, index=0, **kwargs):
    """Supplied decision fixture with explicit, normally validated producer lineage."""
    when = BASE + timedelta(minutes=index)
    return _confirmed_candidate(**kwargs).model_copy(update={
        "trade_idea": _trade_idea(direction=kwargs.get("direction", "long")),
        "setup_quality": _setup_quality(),
        "evaluation_origin_kind": "live_scan",
        "evaluation_completed_at": when,
        "lifecycle_decision_timestamp": when,
        "lifecycle_execution_batch_delivery": _live_adapter_delivery(acquired_at=when),
    })


def _register(path, run_id):
    register_operational_scan_run(
        path, run_id=run_id, registered_at="2026-03-01T12:00:00Z",
        expected_identity=SYNTHETIC_IDENTITY,
    )


def _apply(path, symbol, run_id="one", *, now="2026-03-01T14:00:00Z", cycles=2):
    service = SetupLifecycleService(path, expected_identity=SYNTHETIC_IDENTITY,
                                    confirmation_cycles=cycles)
    return service.apply_to_run_result(_scan_result(symbol), scan_run_id=run_id, now=now)


def _state(result):
    assert result.scanner_process_summary["failed_symbols"] == 0
    return result.results[0].lifecycle_state


def _snapshot(path):
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f"SELECT * FROM {table} ORDER BY rowid").fetchall()
            for table in ("runtime_operational_origins", "setup_lifecycle_records",
                          "setup_lifecycle_events", "setup_outcome_analytics",
                          "public_alert_events", "telegram_alert_attempts")
        }


@pytest.mark.parametrize("later", [False, True])
def test_supplied_decision_identical_retry_and_restart(tmp_path, later):
    path = bootstrap_operational_test_database(tmp_path / "retry.db")
    _register(path, "one")
    candidate = _candidate()
    first = _state(_apply(path, candidate))
    assert first.confirmation_count == 1
    before = _snapshot(path)
    now = "2026-03-01T19:00:00Z" if later else "2026-03-01T14:00:00Z"
    retried = _apply(path, candidate, now=now)
    assert _state(retried) == first
    assert _snapshot(path) == before
    assert retried.scanner_process_summary["duplicate_confirmation_observations"] == 1
    assert first.confirmed_at is None and first.plan_version_id is None


def test_retry_of_service_result_has_the_same_decision_identity(tmp_path):
    path = bootstrap_operational_test_database(tmp_path / "output-retry.db")
    _register(path, "one")
    first = _apply(path, _candidate())
    retry = _apply(path, first.results[0])
    assert _state(retry) == _state(first)


def test_first_contribution_without_transition_is_persisted(tmp_path):
    """Supplied pre-confirmation owner; public service still validates every origin."""
    path = bootstrap_operational_test_database(tmp_path / "first-no-transition.db")
    _register(path, "seed")
    _register(path, "one")
    seeded = _state(_apply(path, _candidate(), "seed", cycles=3))
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repo:
        repo.upsert_record(seeded.model_copy(update={"confirmation_count": 0}))
    first = _apply(path, _candidate(index=1), cycles=3)
    assert _state(first).confirmation_count == 1
    assert first.results[0].lifecycle_transition.transitioned is False
    before = _snapshot(path)
    assert _state(_apply(path, _candidate(index=1), cycles=3)) == _state(first)
    assert _snapshot(path) == before


def test_accounting_receipts_do_not_create_research_transitions_or_public_events(tmp_path):
    from app.research.queries import ResearchFilters, build_research_report
    from app.research.population import PROSPECTIVE_EPOCH_SCOPED_RESEARCH
    path = bootstrap_operational_test_database(tmp_path / "counts.db")
    for index in range(3):
        run_id = f"run-{index}"
        _register(path, run_id)
        assert _state(_apply(path, _candidate(index=index), run_id, cycles=4)).confirmation_count == index + 1
    filters = ResearchFilters(population_scope=PROSPECTIVE_EPOCH_SCOPED_RESEARCH,
                              runtime_epoch_id=SYNTHETIC_IDENTITY.epoch_id)
    report = build_research_report(path, query="lifecycle_transitions", filters=filters)
    assert report["total_transitions"] == 1
    assert report["transitions"][0]["to_state"] == "TRIGGERED"
    snapshot = _snapshot(path)
    assert len(snapshot["runtime_operational_origins"]) == 3
    assert len(snapshot["setup_lifecycle_records"]) == 1
    assert snapshot["public_alert_events"] == snapshot["setup_outcome_analytics"] == []
    _apply(path, _candidate(), "run-0", cycles=4)
    assert build_research_report(path, query="lifecycle_transitions", filters=filters) == report


def test_supplied_decision_old_retry_after_intervening_scan_and_no_transition(tmp_path):
    path = bootstrap_operational_test_database(tmp_path / "intervening.db")
    for run_id in ("one", "two", "three"):
        _register(path, run_id)
    first = _state(_apply(path, _candidate(), cycles=3))
    second_result = _apply(path, _candidate(index=1), "two", cycles=3)
    second = _state(second_result)
    assert second.confirmation_count == 2
    assert second_result.results[0].lifecycle_transition.transitioned is False
    before = _snapshot(path)
    assert _state(_apply(path, _candidate(), cycles=3)) == second
    assert _snapshot(path) == before
    third = _state(_apply(path, _candidate(index=2), "three", cycles=3))
    assert third.confirmation_count == 3
    assert third.confirmed_at is not None
    assert first.lifecycle_id == third.lifecycle_id


def test_supplied_distinct_scans_same_candle_and_out_of_order_still_count(tmp_path):
    path = bootstrap_operational_test_database(tmp_path / "distinct.db")
    _register(path, "one")
    _register(path, "two")
    _state(_apply(path, _candidate(index=2), "two"))
    # First arrival of older producer evidence retains established processing-order policy.
    result = _state(_apply(path, _candidate(), "one", now="2026-03-01T14:01:00Z"))
    assert result.confirmation_count == 2 and result.confirmed_at is not None


def test_supplied_concurrent_consumers_count_once(tmp_path):
    path = bootstrap_operational_test_database(tmp_path / "concurrent.db")
    _register(path, "one")
    barrier = Barrier(2)
    def consume():
        barrier.wait(timeout=10)
        return _state(_apply(path, _candidate()))
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(consume) for _ in range(2)]
        states = [future.result(timeout=30) for future in futures]
    assert states[0] == states[1]
    assert states[0].confirmation_count == 1
    assert states[0].confirmed_at is None


@pytest.mark.parametrize("change", ["prices", "direction", "anchor", "cutoff"])
def test_supplied_identity_conflict_is_visible_and_unchanged(tmp_path, change):
    path = bootstrap_operational_test_database(tmp_path / "conflict.db")
    _register(path, "one")
    _apply(path, _candidate())
    before = _snapshot(path)
    changes = {
        "prices": {"tp2": 120},
        "direction": {"direction": "short"},
        "anchor": {"anchor": "different"},
        "cutoff": {"index": 1},
    }
    result = _apply(path, _candidate(**changes[change]))
    assert result.scanner_process_summary["failed_symbols"] == 1
    assert "integrity exception" in result.scanner_process_summary["errors"][0]["detail"]
    assert _snapshot(path) == before


def test_supplied_modes_have_separate_receipts(tmp_path):
    path = bootstrap_operational_test_database(tmp_path / "modes.db")
    _register(path, "one")
    swing = _state(_apply(path, _candidate(mode="swing")))
    scalp = _state(_apply(path, _candidate(mode="scalp")))
    assert swing.lifecycle_id != scalp.lifecycle_id
    assert swing.confirmation_count == scalp.confirmation_count == 1
    assert _state(_apply(path, _candidate(mode="swing"))) == swing
    with sqlite3.connect(path) as connection:
        receipt = json.loads(connection.execute(
            "SELECT lifecycle_consumption_json FROM runtime_operational_origins"
        ).fetchone()[0])
    assert set(receipt["modes"]) == {"swing", "scalp"}


@pytest.mark.parametrize("anchor", ["sweep-a", "N/A"])
def test_supplied_generation_retry_cannot_borrow_current_owner(tmp_path, anchor):
    path = bootstrap_operational_test_database(tmp_path / "generation.db")
    _register(path, "one")
    _register(path, "two")
    first = _state(_apply(path, _candidate(anchor=anchor)))
    if anchor == "N/A":
        # Supplied-state generation boundary: no-anchor archived owner, no fabricated origin.
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repo:
            repo.upsert_record(first.model_copy(update={"current_state": SetupLifecycleState.ARCHIVED}))
        next_anchor = anchor
    else:
        next_anchor = "sweep-b"
    second = _state(_apply(path, _candidate(index=1, anchor=next_anchor), "two"))
    assert second.lifecycle_id != first.lifecycle_id
    before = _snapshot(path)
    retry = _state(_apply(path, _candidate(anchor=anchor)))
    assert retry.lifecycle_id == first.lifecycle_id and not retry.is_current
    assert _snapshot(path) == before
    assert second.confirmation_count == 1


@pytest.mark.parametrize("damage", ["missing_run", "foreign_run", "missing_cutoff",
                                    "missing_acquisition", "not_live"])
def test_supplied_invalid_lineage_cannot_consume(tmp_path, damage):
    path = bootstrap_operational_test_database(tmp_path / "invalid.db")
    symbol = _candidate()
    if damage != "missing_run":
        _register(path, "one")
    if damage == "foreign_run":
        with sqlite3.connect(path) as connection:
            connection.execute("UPDATE runtime_operational_runs SET runtime_epoch_id = 'foreign'")
    if damage == "missing_cutoff":
        symbol = symbol.model_copy(update={"lifecycle_decision_timestamp": None})
    if damage == "missing_acquisition":
        symbol = symbol.model_copy(update={"lifecycle_execution_batch_delivery": None})
    if damage == "not_live":
        symbol = symbol.model_copy(update={"evaluation_origin_kind": "replay"})
    if damage in {"missing_run", "foreign_run"}:
        with pytest.raises(RuntimeEpochOriginError):
            _apply(path, symbol)
    else:
        assert _state(_apply(path, symbol)) is None
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM setup_lifecycle_records").fetchone()[0] == 0


@pytest.mark.parametrize("failure", [ValueError, sqlite3.OperationalError])
def test_supplied_receipt_and_effect_rollback_together(tmp_path, monkeypatch, failure):
    path = bootstrap_operational_test_database(tmp_path / "failure.db")
    _register(path, "one")
    before = _snapshot(path)
    def fail(self, record):
        assert self.connection.execute(
            "SELECT lifecycle_consumption_json FROM runtime_operational_origins"
        ).fetchone()[0] != '{"version":1,"modes":{}}'
        raise failure("injected after receipt before state")
    with monkeypatch.context() as patch:
        patch.setattr(SQLiteSetupLifecycleRepository, "upsert_record", fail)
        if failure is ValueError:
            result = _apply(path, _candidate())
            assert result.scanner_process_summary["failed_symbols"] == 1
        else:
            with pytest.raises(failure):
                _apply(path, _candidate())
    assert _snapshot(path) == before
    assert _state(_apply(path, _candidate())).confirmation_count == 1


@pytest.mark.parametrize("rollback", ["explicit", "exception", "commit_failure"])
def test_supplied_enclosing_transaction_failure_is_retryable(tmp_path, monkeypatch, rollback):
    path = bootstrap_operational_test_database(tmp_path / "outer.db")
    _register(path, "one")
    before = _snapshot(path)
    import app.lifecycle.repositories as repository_module
    original_open = repository_module.open_repository_database
    if rollback == "commit_failure":
        class FailCommit(sqlite3.Connection):
            def commit(self):
                raise sqlite3.OperationalError("injected commit failure")
        def failing_open(*args, **kwargs):
            checked = original_open(*args, **kwargs)
            checked.close()
            connection = sqlite3.connect(path, factory=FailCommit)
            connection.row_factory = sqlite3.Row
            return connection
        monkeypatch.setattr(repository_module, "open_repository_database", failing_open)
    service = SetupLifecycleService(path, expected_identity=SYNTHETIC_IDENTITY)
    def attempt():
        with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repo:
            updated = service.apply_to_symbol_result(
                _candidate(), repository=repo, scan_run_id="one", now="2026-03-01T14:00:00Z")
            assert updated.lifecycle_state.confirmation_count == 1
            if rollback == "explicit":
                repo.connection.rollback()
            elif rollback == "exception":
                raise RuntimeError("injected enclosing failure")
    if rollback == "explicit":
        attempt()
    else:
        with pytest.raises((RuntimeError, sqlite3.OperationalError)):
            attempt()
    assert _snapshot(path) == before
    monkeypatch.setattr(repository_module, "open_repository_database", original_open)
    assert _state(_apply(path, _candidate())).confirmation_count == 1


@pytest.mark.parametrize("legacy", [False, True])
def test_supplied_duplicate_and_unmarked_origin_do_not_starve_owned_plan(tmp_path, legacy):
    from decimal import Decimal
    from tests.test_f04_durable_source_replay import _latched, _own
    path = bootstrap_operational_test_database(tmp_path / "monitor.db")
    boundary = "2026-03-01T12:00:00+00:00"
    owner = _own(path, _latched(
        lifecycle_id="owned", structural_anchor="setup_generation_anchor|sweep-a",
        first_seen_at=boundary, last_seen_at=boundary, last_transition_at=boundary,
        confirmed_at=boundary, confirmation_count=2, stop_loss="95", tp2="117", tp3="124",
        invalidation_logic="Invalid if price accepts below 95.",
        invalidation_reason="Invalid if price accepts below 95.",
    ))
    _register(path, "one")
    symbol = _candidate().model_copy(update={
        "lifecycle_execution_candles": (), "lifecycle_execution_timeframe": "5m",
    })
    initial = _state(_apply(path, symbol))
    assert initial.confirmed_at == boundary
    if legacy:
        with sqlite3.connect(path) as connection:
            connection.execute(
                "UPDATE runtime_operational_origins SET lifecycle_consumption_json=NULL WHERE run_id='one'"
            )
    def candle(minute, high, low):
        when = BASE - timedelta(hours=1) + timedelta(minutes=minute)
        return {"timestamp": int(when.timestamp() * 1000), "open": Decimal(low),
                "high": Decimal(high), "low": Decimal(low), "close": Decimal(high),
                "volume": Decimal("10")}
    # The discovery decision is identical; a recovered monitoring batch is separate evidence.
    symbol = symbol.model_copy(update={"lifecycle_execution_candles": (
        candle(0, "103", "99"), candle(5, "111", "103"),
    )})
    result = _apply(path, symbol)
    final = _state(result)
    assert final.current_state == SetupLifecycleState.MANAGING
    assert final.confirmation_count == 2
    assert final.confirmed_at == boundary
    assert final.plan_version_id == owner.plan_version_id
    assert final.stop_loss == "95"
    key = "unverified_confirmation_observations" if legacy else "duplicate_confirmation_observations"
    assert result.scanner_process_summary[key] == 1
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repo:
        progress = repo.list_outcome_progress(lifecycle_id=owner.lifecycle_id)[0]
        events = repo.list_events(lifecycle_id=owner.lifecycle_id)
    assert progress.entry_at is not None and progress.tp1_at is not None
    assert json.loads(progress.metadata_json)["stop_management"]["policy"] == "tp1_entry_zone_midpoint_breakeven_v2"
    _apply(path, symbol)
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repo:
        assert repo.list_events(lifecycle_id=owner.lifecycle_id) == events


@pytest.mark.parametrize("damage", ["malformed", "null_mode", "missing_owner", "foreign_origin"])
def test_supplied_corrupt_receipt_or_origin_cannot_be_treated_as_fresh(tmp_path, damage):
    path = bootstrap_operational_test_database(tmp_path / "corrupt.db")
    _register(path, "one")
    _apply(path, _candidate())
    with sqlite3.connect(path) as connection:
        raw = connection.execute(
            "SELECT lifecycle_consumption_json FROM runtime_operational_origins WHERE run_id='one'"
        ).fetchone()[0]
        doc = json.loads(raw)
        if damage == "malformed":
            raw = "not json"
        elif damage == "null_mode":
            doc["modes"]["swing"] = None
            raw = json.dumps(doc)
        elif damage == "missing_owner":
            doc["modes"]["swing"]["lifecycle_id"] = "absent"
            raw = json.dumps(doc)
        else:
            connection.execute("UPDATE runtime_operational_origins SET runtime_epoch_id='foreign'")
        connection.execute("UPDATE runtime_operational_origins SET lifecycle_consumption_json=?", (raw,))
    before = _snapshot(path)
    result = _apply(path, _candidate())
    assert result.scanner_process_summary["failed_symbols"] == 1
    assert _snapshot(path) == before


@pytest.mark.parametrize("failure", [ValueError, sqlite3.OperationalError, RuntimeError])
def test_direct_symbol_savepoint_failure_cannot_commit_a_receipt(tmp_path, monkeypatch, failure):
    path = bootstrap_operational_test_database(tmp_path / "direct.db")
    _register(path, "one")
    before = _snapshot(path)
    original = SQLiteSetupLifecycleRepository.upsert_record
    def fail(self, record):
        raise failure("state failure caught by caller")
    service = SetupLifecycleService(path, expected_identity=SYNTHETIC_IDENTITY)
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repo:
        monkeypatch.setattr(SQLiteSetupLifecycleRepository, "upsert_record", fail)
        with pytest.raises(failure, match="state failure"):
            service.apply_to_symbol_result(
                _candidate(), repository=repo, scan_run_id="one", now="2026-03-01T14:00:00Z")
        repo.connection.commit()  # Caller catches failure; no partial receipt may escape.
        monkeypatch.setattr(SQLiteSetupLifecycleRepository, "upsert_record", original)
    assert _snapshot(path) == before


def test_producer_missing_registration_still_fails_closed(tmp_path, monkeypatch):
    path = bootstrap_operational_test_database(tmp_path / "negative-producer.db")
    sender = FakeSender()
    _patch_production_orchestration(
        monkeypatch, tmp_path,
        candles_by_symbol={"BTCUSDT": _public_min_rr_pullback_candles()}, sender=sender,
    )
    monkeypatch.setattr(run_scan, "_register_operational_scan_run_before_acquisition",
                        lambda *args, **kwargs: None)
    with pytest.raises(SystemExit):
        asyncio.run(run_scan.main(["--symbols", "BTCUSDT", *_identity_cli(path)]))
    assert _snapshot(path)["setup_lifecycle_records"] == []
    assert sender.calls == []


@pytest.mark.parametrize("same_time", [False, True])
def test_producer_retry_does_not_confirm_one_scan(tmp_path, monkeypatch, same_time):
    database = bootstrap_operational_test_database(tmp_path / "producer.db")
    sender = FakeSender()
    _patch_production_orchestration(
        monkeypatch, tmp_path,
        candles_by_symbol={"BTCUSDT": _public_min_rr_pullback_candles()},
        sender=sender,
    )
    original = SetupLifecycleService.apply_to_run_result
    observed = []

    def retry(self, result, *, scan_run_id=None, now=None):
        now = "2026-03-01T14:30:00Z" if same_time else now
        first = original(self, result, scan_run_id=scan_run_id, now=now)
        again = original(self, result, scan_run_id=scan_run_id, now=now)
        observed.extend([first.results[0].lifecycle_state, again.results[0].lifecycle_state])
        # Deliver the original successful processing result. The redundant
        # consumer must not replace that result's legitimate transition notice.
        return first

    monkeypatch.setattr(SetupLifecycleService, "apply_to_run_result", retry)
    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", *_identity_cli(database)]))
    first, again = observed
    assert first.current_state == SetupLifecycleState.TRIGGERED
    assert first.confirmation_count == 1
    assert again.confirmation_count == 1
    assert again.current_state == SetupLifecycleState.TRIGGERED
    assert again.confirmed_at is None
    assert again.plan_version_id is None
    assert sender.calls == []
    # A genuinely new registered scan of the same synthetic candles still confirms.
    asyncio.run(run_scan.main(["--symbols", "BTCUSDT", *_identity_cli(database)]))
    second, second_retry = observed[2:]
    assert second.lifecycle_id == first.lifecycle_id
    assert second.confirmation_count == second_retry.confirmation_count == 2
    assert second.current_state == second_retry.current_state == SetupLifecycleState.CONFIRMED
    assert len(sender.calls) == 1
