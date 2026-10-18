"""Regressions for the four stale-discovery lifecycle defects.

Synthetic operational databases only. These tests do not read Runtime, send
Telegram traffic, or place orders.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import timedelta
from decimal import Decimal

import pytest

from app.lifecycle.economic_identity import latch_economic_identities
from app.lifecycle.models import SetupLifecycleOutcomeProgress, SetupLifecycleState
from app.lifecycle.outcome_policy import compatible_plan_identities
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import SetupLifecycleService
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerSymbolResult,
)
from app.runtime_epoch.origin import register_operational_scan_run
from test_economic_identity import _confirmed_candidate
from test_telegram_lifecycle_delivery_phase42 import _setup_quality, _trade_idea
from test_lifecycle_outcomes import (
    BASE,
    TIMEFRAME,
    _baseline,
    _candle,
    _entry,
    _record,
    _stop,
    _target,
)
from test_prospective_runtime_epoch_isolation_boundaries import _live_adapter_delivery
from tests.runtime_epoch_support import (
    SYNTHETIC_EPOCH_ID,
    SYNTHETIC_IDENTITY,
    bootstrap_operational_test_database,
    grant_synthetic_origin,
    seed_legacy_lifecycle,
)

pytestmark = pytest.mark.no_auto_epoch

FRESHNESS_MISSING = (
    "essential_candle_freshness[15m]: N/A (role=execution,status=stale,reason=missing_completed_interval)",
)
_OWNED = {"scalp-owner", "swing-owner"}


def _when(candles, *, extra_minutes: int = 0):
    return BASE + timedelta(minutes=5 * (len(candles) + 1) + extra_minutes)


def _tp_candles():
    return [_baseline("short"), _entry(1, "short"), _candle(2, high="99", low="70")]


def _sl_candles():
    return [_baseline("short"), _entry(1, "short"), _stop(2, "short")]


def _breakeven_candles():
    return [
        _baseline("short"),
        _entry(1, "short"),
        _target(2, "short", 1),
        _candle(3, high="101", low="97"),
    ]


def _service(path):
    return SetupLifecycleService(
        path,
        expected_identity=SYNTHETIC_IDENTITY,
        confirmation_cycles=2,
    )


def _register(path, run_id: str) -> None:
    register_operational_scan_run(
        path,
        run_id=run_id,
        registered_at="2026-01-01T00:00:00Z",
        expected_identity=SYNTHETIC_IDENTITY,
    )


def _stale_result(candles, *, missing=FRESHNESS_MISSING, timeframe: str = TIMEFRAME, when=None):
    decision = when or _when(candles)
    return ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.SCANNED_NO_SETUP,
        status_history=(ScannerPipelineStatus.SCANNED_NO_SETUP,),
        rejection_stage="essential_candle_freshness",
        missing_data=missing,
        rejected_strategy_modes=("challenge",),
        evaluation_origin_kind="live_scan",
        evaluation_completed_at=decision,
        lifecycle_decision_timestamp=decision,
        lifecycle_execution_timeframe=timeframe,
        lifecycle_execution_candles=tuple(candles),
        lifecycle_execution_batch_delivery=_live_adapter_delivery(
            acquired_at=decision,
            symbol="BTCUSDT",
        ),
    )


def _run(symbol_result: ScannerSymbolResult) -> ScannerRunResult:
    config = ScannerRunConfig.model_validate(
        {
            "symbols": ["BTCUSDT"],
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
            "execution_timeframe": TIMEFRAME,
        }
    )
    return ScannerRunResult(
        config=config,
        results=(symbol_result,),
        scanned_symbols=1,
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )


def _apply(path, symbol_result, run_id: str, now: str):
    return _service(path).apply_to_run_result(_run(symbol_result), scan_run_id=run_id, now=now)


def _summary(result):
    return result.scanner_process_summary


def _ok(result):
    summary = _summary(result)
    assert summary["failed_symbols"] == 0, summary
    assert summary["status"] == "SUCCESS"
    return result


def _seed_current(path, specs: tuple[tuple[str, str, str], ...]) -> None:
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        connection = repository.connection
        assert connection is not None
        for lifecycle_id, mode, direction in specs:
            origin = grant_synthetic_origin(
                connection,
                symbol="BTCUSDT",
                run_id=f"seed-{lifecycle_id}",
            )
            assert origin.granted and origin.origin_id
            record = _record(
                lifecycle_id=lifecycle_id,
                mode=mode,
                direction=direction,
                state=SetupLifecycleState.CONFIRMED,
            ).model_copy(
                update={
                    "structural_anchor": f"{mode}-anchor",
                    "runtime_epoch_id": SYNTHETIC_EPOCH_ID,
                    "creation_origin_id": origin.origin_id,
                    "confirmation_count": 2,
                    "rr": "3",
                }
            )
            latched = latch_economic_identities(
                record,
                instrument_venue="binance",
                plan_locked=True,
            )
            assert latched.plan_version_id, latched.economic_identity_reason
            repository.upsert_record(latched)


def _rows(path, sql: str, params: tuple = ()):
    with sqlite3.connect(path) as connection:
        connection.row_factory = sqlite3.Row
        return [dict(row) for row in connection.execute(sql, params)]


def _owners(path):
    return _rows(
        path,
        """
        SELECT lifecycle_id, mode, direction, current_state, confirmation_count,
               last_seen_at, plan_version_id, runtime_epoch_id, creation_origin_id, is_current
        FROM setup_lifecycle_records
        ORDER BY lifecycle_id
        """,
    )


def _analytics(path):
    return _rows(
        path,
        """
        SELECT lifecycle_id, final_outcome
        FROM setup_outcome_analytics
        ORDER BY lifecycle_id, final_outcome
        """,
    )


def _progress(path):
    return _rows(
        path,
        """
        SELECT lifecycle_id, terminal_outcome, execution_timeframe, diagnostic
        FROM setup_lifecycle_outcome_progress
        ORDER BY lifecycle_id
        """,
    )


def _receipt(path, run_id: str):
    rows = _rows(
        path,
        """
        SELECT lifecycle_consumption_json
        FROM runtime_operational_origins
        WHERE run_id = ?
        """,
        (run_id,),
    )
    assert len(rows) == 1
    return json.loads(rows[0]["lifecycle_consumption_json"])


def _arm_stored_plan(connection, lifecycle_id: str) -> None:
    text = "Closed structure beyond the stored stop invalidates the plan."
    connection.execute(
        """
        UPDATE setup_lifecycle_records
        SET invalidation_logic = ?, invalidation_reason = ?,
            stop_loss = '90', tp1 = '110', tp2 = '120', tp3 = '130', rr = '3'
        WHERE lifecycle_id = ?
        """,
        (text, text, lifecycle_id),
    )


def _record_snapshot(path, lifecycle_id: str):
    rows = _rows(
        path,
        "SELECT * FROM setup_lifecycle_records WHERE lifecycle_id = ?",
        (lifecycle_id,),
    )
    assert len(rows) == 1
    return rows[0]


@pytest.mark.parametrize(
    ("kind", "candles", "state", "outcome"),
    [
        ("tp", _tp_candles(), SetupLifecycleState.TP_HIT.value, "TP3_HIT"),
        ("sl", _sl_candles(), SetupLifecycleState.SL_HIT.value, "SL_HIT"),
        ("breakeven", _breakeven_candles(), SetupLifecycleState.BREAKEVEN.value, "BREAKEVEN"),
    ],
)
def test_multiple_owners_persist_terminal_analytics(tmp_path, kind, candles, state, outcome) -> None:
    path = bootstrap_operational_test_database(tmp_path / "analytics.sqlite")
    _seed_current(
        path,
        (("scalp-owner", "scalp", "short"), ("swing-owner", "swing", "short")),
    )
    if kind == "breakeven":
        _register(path, "arm-be")
        _ok(
            _apply(
                path,
                _stale_result([_baseline("short")], when=BASE + timedelta(minutes=5)),
                "arm-be",
                "2026-01-01T13:00:00Z",
            )
        )
        candles = [
            _baseline("short"),
            _entry(1, "short"),
            _target(2, "short", 1),
            _candle(3, high="101", low="97"),
        ]
        symbol = _stale_result(candles, when=BASE + timedelta(minutes=25))
    else:
        symbol = _stale_result(candles)
    _register(path, "stale")
    _ok(_apply(path, symbol, "stale", "2026-01-01T14:01:00Z"))
    owned = [row for row in _owners(path) if row["lifecycle_id"] in _OWNED]
    assert {row["current_state"] for row in owned} == {state}
    assert {row["confirmation_count"] for row in owned} == {2}
    assert all(row["plan_version_id"] for row in owned)
    assert len({row["plan_version_id"] for row in owned}) == 2
    assert _analytics(path) == [
        {"lifecycle_id": "scalp-owner", "final_outcome": outcome},
        {"lifecycle_id": "swing-owner", "final_outcome": outcome},
    ]
    seen = {row["lifecycle_id"]: row["last_seen_at"] for row in owned}
    retry = _ok(_apply(path, symbol, "stale", "2026-01-01T14:02:00Z"))
    assert _summary(retry)["duplicate_confirmation_observations"] == 2
    assert _analytics(path) == [
        {"lifecycle_id": "scalp-owner", "final_outcome": outcome},
        {"lifecycle_id": "swing-owner", "final_outcome": outcome},
    ]
    retried = {row["lifecycle_id"]: row["last_seen_at"] for row in _owners(path) if row["lifecycle_id"] in _OWNED}
    assert retried == seen


def test_one_owner_stale_discovery_records_tp3_analytics(tmp_path) -> None:
    path = bootstrap_operational_test_database(tmp_path / "one.sqlite")
    _seed_current(path, (("swing-owner", "swing", "short"),))
    _register(path, "stale")
    before = _record_snapshot(path, "swing-owner")
    _ok(_apply(path, _stale_result(_tp_candles()), "stale", "2026-01-01T14:01:00Z"))
    after = _record_snapshot(path, "swing-owner")
    assert after["current_state"] == SetupLifecycleState.TP_HIT.value
    assert after["confirmation_count"] == before["confirmation_count"]
    assert after["plan_version_id"] == before["plan_version_id"]
    assert after["entry_low"] == before["entry_low"]
    assert after["stop_loss"] == before["stop_loss"]
    assert _analytics(path) == [{"lifecycle_id": "swing-owner", "final_outcome": "TP3_HIT"}]


def test_directionless_single_owner_retry_is_idempotent(tmp_path) -> None:
    path = bootstrap_operational_test_database(tmp_path / "retry.sqlite")
    _seed_current(path, (("swing-owner", "swing", "long"),))
    _register(path, "stale")
    symbol = _stale_result([_baseline("long")])
    _ok(_apply(path, symbol, "stale", "2026-01-01T14:01:00Z"))
    seen = _record_snapshot(path, "swing-owner")["last_seen_at"]
    plan = _record_snapshot(path, "swing-owner")["plan_version_id"]
    for now in ("2026-01-01T14:02:00Z", "2026-01-01T14:03:00Z"):
        result = _ok(_apply(path, symbol, "stale", now))
        assert _summary(result)["duplicate_confirmation_observations"] == 1
    restarted = _ok(_apply(path, symbol, "stale", "2026-01-01T14:04:00Z"))
    assert _summary(restarted)["duplicate_confirmation_observations"] == 1
    stored = _record_snapshot(path, "swing-owner")
    assert stored["last_seen_at"] == seen
    assert stored["current_state"] == SetupLifecycleState.CONFIRMED.value
    assert stored["confirmation_count"] == 2
    assert stored["plan_version_id"] == plan
    receipt = _receipt(path, "stale")
    assert set(receipt) == {"version", "modes"}
    assert set(receipt["modes"]) == {"swing"}
    assert receipt["modes"]["swing"]["lifecycle_id"] == "swing-owner"
    assert receipt["modes"]["swing"]["consumed_at"] == "2026-01-01T14:01:00Z"


def test_directionless_retry_rejects_changed_decision_content(tmp_path) -> None:
    path = bootstrap_operational_test_database(tmp_path / "conflict.sqlite")
    _seed_current(path, (("swing-owner", "swing", "long"),))
    _register(path, "stale")
    symbol = _stale_result([_baseline("long")])
    _ok(_apply(path, symbol, "stale", "2026-01-01T14:01:00Z"))
    before_owner = _record_snapshot(path, "swing-owner")
    before_receipt = _receipt(path, "stale")
    changed = symbol.model_copy(
        update={
            "lifecycle_decision_timestamp": symbol.lifecycle_decision_timestamp + timedelta(minutes=1),
            "evaluation_completed_at": symbol.evaluation_completed_at + timedelta(minutes=1),
        }
    )
    result = _apply(path, changed, "stale", "2026-01-01T14:02:00Z")
    summary = _summary(result)
    assert summary["status"] == "PARTIAL"
    assert summary["failed_symbols"] == 1
    assert summary["errors"][0]["type"] == "RuntimeEpochIdentityCollisionError"
    assert "conflicting decision content" in summary["errors"][0]["detail"]
    assert _record_snapshot(path, "swing-owner") == before_owner
    assert _receipt(path, "stale") == before_receipt


def test_multiple_owner_retry_persists_one_receipt(tmp_path) -> None:
    path = bootstrap_operational_test_database(tmp_path / "receipt.sqlite")
    _seed_current(
        path,
        (("scalp-owner", "scalp", "long"), ("swing-owner", "swing", "long")),
    )
    _register(path, "stale")
    symbol = _stale_result([_baseline("long")])
    _ok(_apply(path, symbol, "stale", "2026-01-01T14:01:00Z"))
    seen = {
        row["lifecycle_id"]: row["last_seen_at"]
        for row in _owners(path)
        if row["lifecycle_id"] in _OWNED
    }
    retry = _ok(_apply(path, symbol, "stale", "2026-01-01T14:02:00Z"))
    assert _summary(retry)["duplicate_confirmation_observations"] == 2
    assert {
        row["lifecycle_id"]: row["last_seen_at"]
        for row in _owners(path)
        if row["lifecycle_id"] in _OWNED
    } == seen
    receipt = _receipt(path, "stale")
    assert receipt["version"] == 1
    assert set(receipt["modes"]) == {"scalp", "swing"}
    assert receipt["modes"]["scalp"]["lifecycle_id"] == "scalp-owner"
    assert receipt["modes"]["swing"]["lifecycle_id"] == "swing-owner"
    assert {slot["consumed_at"] for slot in receipt["modes"].values()} == {"2026-01-01T14:01:00Z"}


def test_retry_does_not_bind_an_owner_that_appears_later(tmp_path) -> None:
    path = bootstrap_operational_test_database(tmp_path / "intervening.sqlite")
    _seed_current(
        path,
        (("scalp-owner", "scalp", "long"), ("swing-owner", "swing", "long")),
    )
    _register(path, "stale")
    symbol = _stale_result([_baseline("long")])
    _ok(_apply(path, symbol, "stale", "2026-01-01T14:01:00Z"))
    _seed_current(path, (("position-owner", "position", "long"),))
    before = _record_snapshot(path, "position-owner")
    held = {
        row["lifecycle_id"]: row["last_seen_at"]
        for row in _owners(path)
        if row["lifecycle_id"] in _OWNED
    }
    retry = _ok(_apply(path, symbol, "stale", "2026-01-01T14:02:00Z"))
    assert _summary(retry)["duplicate_confirmation_observations"] == 2
    assert _record_snapshot(path, "position-owner") == before
    assert {
        row["lifecycle_id"]: row["last_seen_at"]
        for row in _owners(path)
        if row["lifecycle_id"] in _OWNED
    } == held
    assert set(_receipt(path, "stale")["modes"]) == {"scalp", "swing"}


def test_owner_write_failure_rolls_back_receipt_and_sibling(tmp_path, monkeypatch) -> None:
    path = bootstrap_operational_test_database(tmp_path / "rollback.sqlite")
    _seed_current(
        path,
        (("scalp-owner", "scalp", "long"), ("swing-owner", "swing", "long")),
    )
    before = {row["lifecycle_id"]: _record_snapshot(path, row["lifecycle_id"]) for row in _owners(path)}
    _register(path, "stale")
    original = SQLiteSetupLifecycleRepository.upsert_record

    def _fail_swing(self, record):
        if record.lifecycle_id == "swing-owner" and record.last_seen_at != before["swing-owner"]["last_seen_at"]:
            raise RuntimeError("forced owner write failure")
        return original(self, record)

    monkeypatch.setattr(SQLiteSetupLifecycleRepository, "upsert_record", _fail_swing)
    with pytest.raises(RuntimeError, match="forced owner write failure"):
        _apply(path, _stale_result([_baseline("long")]), "stale", "2026-01-01T14:01:00Z")
    assert _analytics(path) == []
    assert _rows(
        path,
        "SELECT run_id, lifecycle_consumption_json FROM runtime_operational_origins WHERE run_id = 'stale'",
    ) == []
    for lifecycle_id, snapshot in before.items():
        assert _record_snapshot(path, lifecycle_id) == snapshot


def test_execution_evidence_must_match_owner_timeframe(tmp_path) -> None:
    path = bootstrap_operational_test_database(tmp_path / "timeframe.sqlite")
    _seed_current(
        path,
        (("scalp-owner", "scalp", "short"), ("swing-owner", "swing", "short")),
    )
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        owner = repository.get_record_by_lifecycle_id("swing-owner")
        assert owner is not None
        repository.upsert_outcome_progress(
            SetupLifecycleOutcomeProgress(
                lifecycle_id=owner.lifecycle_id,
                plan_identity=compatible_plan_identities(owner)[0],
                plan_version_id=owner.plan_version_id,
                symbol=owner.symbol,
                mode=owner.mode,
                direction=owner.direction,
                execution_timeframe="15m",
                first_evaluated_at=owner.confirmed_at or owner.first_seen_at,
                last_evaluated_at=owner.confirmed_at or owner.first_seen_at,
            )
        )
    _register(path, "stale")
    _ok(_apply(path, _stale_result(_tp_candles()), "stale", "2026-01-01T14:01:00Z"))
    states = {row["lifecycle_id"]: row["current_state"] for row in _owners(path)}
    assert states["scalp-owner"] == SetupLifecycleState.TP_HIT.value
    assert states["swing-owner"] == SetupLifecycleState.CONFIRMED.value
    progress = {row["lifecycle_id"]: row for row in _progress(path)}
    assert progress["swing-owner"]["execution_timeframe"] == "15m"
    assert "execution_timeframe_changed" not in str(progress["swing-owner"]["diagnostic"])
    assert _analytics(path) == [{"lifecycle_id": "scalp-owner", "final_outcome": "TP3_HIT"}]


@pytest.mark.parametrize("population", ["legacy", "current", "foreign", "mixed"])
def test_stale_owner_selection_is_active_epoch_only(tmp_path, population) -> None:
    path = bootstrap_operational_test_database(tmp_path / f"{population}.sqlite")
    legacy_id = f"{population}-legacy"
    foreign_id = f"{population}-foreign"
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repository:
        connection = repository.connection
        assert connection is not None
        if population in {"legacy", "mixed"}:
            seed_legacy_lifecycle(
                connection,
                lifecycle_id=legacy_id,
                symbol="BTCUSDT",
                mode="swing",
                direction="long",
                current_state=SetupLifecycleState.CONFIRMED.value,
                last_seen_at="2026-01-01T00:00:00Z",
            )
        if population == "foreign":
            seed_legacy_lifecycle(
                connection,
                lifecycle_id=foreign_id,
                symbol="BTCUSDT",
                mode="swing",
                direction="long",
                current_state=SetupLifecycleState.CONFIRMED.value,
                last_seen_at="2026-01-01T00:00:00Z",
            )
            connection.execute(
                """
                UPDATE setup_lifecycle_records
                SET runtime_epoch_id = ?
                WHERE lifecycle_id = ?
                """,
                ("foreign-epoch-not-active", foreign_id),
            )
        if population in {"legacy", "mixed"}:
            _arm_stored_plan(connection, legacy_id)
        if population == "foreign":
            _arm_stored_plan(connection, foreign_id)
        if population in {"current", "mixed"}:
            origin = grant_synthetic_origin(connection, symbol="BTCUSDT", run_id=f"seed-{population}")
            assert origin.granted and origin.origin_id
            record = _record(
                lifecycle_id=f"{population}-current",
                mode="swing",
                direction="long",
                state=SetupLifecycleState.CONFIRMED,
            ).model_copy(
                update={
                    "structural_anchor": f"{population}-anchor",
                    "runtime_epoch_id": SYNTHETIC_EPOCH_ID,
                    "creation_origin_id": origin.origin_id,
                    "confirmation_count": 2,
                    "rr": "3",
                }
            )
            latched = latch_economic_identities(record, instrument_venue="binance", plan_locked=True)
            assert latched.plan_version_id, latched.economic_identity_reason
            repository.upsert_record(latched)
    protected = []
    if population in {"legacy", "mixed"}:
        protected.append(legacy_id)
    if population == "foreign":
        protected.append(foreign_id)
    before = {lifecycle_id: _record_snapshot(path, lifecycle_id) for lifecycle_id in protected}
    _register(path, "stale")
    result = _ok(_apply(path, _stale_result([_baseline("long")]), "stale", "2026-01-01T14:01:00Z"))
    assert "Legacy lifecycle identity collision" not in str(_summary(result))
    for lifecycle_id, snapshot in before.items():
        assert _record_snapshot(path, lifecycle_id) == snapshot
    rows = _owners(path)
    if population == "current":
        assert [row["lifecycle_id"] for row in rows] == [f"{population}-current"]
        assert rows[0]["current_state"] == SetupLifecycleState.CONFIRMED.value
        assert rows[0]["confirmation_count"] == 2
        assert rows[0]["runtime_epoch_id"] == SYNTHETIC_EPOCH_ID
    if population == "mixed":
        current = next(row for row in rows if row["lifecycle_id"] == f"{population}-current")
        assert current["current_state"] == SetupLifecycleState.CONFIRMED.value
        assert current["confirmation_count"] == 2
        assert current["runtime_epoch_id"] == SYNTHETIC_EPOCH_ID
        assert current["last_seen_at"] == "2026-01-01T14:01:00Z"
        assert [row["lifecycle_id"] for row in rows] == sorted([legacy_id, f"{population}-current"])
    if population in {"legacy", "foreign"}:
        extras = [row for row in rows if row["lifecycle_id"] not in before]
        assert len(extras) == 1
        assert extras[0]["current_state"] == SetupLifecycleState.REJECTED.value
        assert extras[0]["confirmation_count"] == 0
        assert extras[0]["plan_version_id"] is None
        assert extras[0]["runtime_epoch_id"] == SYNTHETIC_EPOCH_ID


def test_stale_discovery_does_not_mint_a_plan_or_confirmation(tmp_path) -> None:
    path = bootstrap_operational_test_database(tmp_path / "new.sqlite")
    _register(path, "stale")
    result = _ok(_apply(path, _stale_result([_baseline("long")]), "stale", "2026-01-01T14:01:00Z"))
    assert result.results[0].status == ScannerPipelineStatus.SCANNED_NO_SETUP
    rows = _owners(path)
    assert len(rows) == 1
    assert rows[0]["current_state"] == SetupLifecycleState.REJECTED.value
    assert rows[0]["confirmation_count"] == 0
    assert rows[0]["plan_version_id"] is None
    assert _analytics(path) == []


def test_historical_decision_does_not_use_a_later_candle(tmp_path) -> None:
    path = bootstrap_operational_test_database(tmp_path / "causal.sqlite")
    _seed_current(path, (("swing-owner", "swing", "short"),))
    candles = _tp_candles()
    early = _stale_result(candles, when=BASE + timedelta(minutes=10))
    _register(path, "early")
    _ok(_apply(path, early, "early", "2026-01-01T14:01:00Z"))
    assert _record_snapshot(path, "swing-owner")["current_state"] != SetupLifecycleState.TP_HIT.value
    assert _analytics(path) == []
    _register(path, "later")
    _ok(_apply(path, _stale_result(candles), "later", "2026-01-01T14:02:00Z"))
    assert _record_snapshot(path, "swing-owner")["current_state"] == SetupLifecycleState.TP_HIT.value
    assert _analytics(path) == [{"lifecycle_id": "swing-owner", "final_outcome": "TP3_HIT"}]


def test_fresh_discovery_still_counts_a_confirmation(tmp_path) -> None:
    path = bootstrap_operational_test_database(tmp_path / "fresh.sqlite")
    _register(path, "fresh")
    when = BASE + timedelta(hours=2)
    candidate = _confirmed_candidate().model_copy(
        update={
            "trade_idea": _trade_idea(direction="long"),
            "setup_quality": _setup_quality(),
            "evaluation_origin_kind": "live_scan",
            "evaluation_completed_at": when,
            "lifecycle_decision_timestamp": when,
            "lifecycle_execution_batch_delivery": _live_adapter_delivery(acquired_at=when),
        }
    )
    result = _ok(_apply(path, candidate, "fresh", "2026-01-01T14:01:00Z"))
    state = result.results[0].lifecycle_state
    assert state is not None
    assert state.confirmation_count == 1
    assert state.current_state != SetupLifecycleState.REJECTED
    assert "essential_candle_freshness" not in str(state.failed_gate)
