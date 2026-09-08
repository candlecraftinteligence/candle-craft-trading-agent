"""R0 observation-unit contract: producer reachability, not a new identity.

These fixtures are synthetic. They do not read live or existing scan databases.
Evidence labels below are test documentation, not application enums.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from app.agents.trade_idea import create_trade_idea
from app.alerts.telegram_lifecycle import (
    _public_economic_setup_plan,
    telegram_signal_message_from_symbol,
)
from app.analytics.evidence_contract import UNAVAILABLE, evidence_contract_payload
from app.analytics.outcome_ownership import (
    STATUS_AMBIGUOUS_CONTEXT,
    project_outcome_ownership,
)
from app.analytics.setup_quality import SetupQualityGrade, SetupQualityResult, SetupQualityState
from app.backtesting.strategy_replay import (
    LiquidityGrabMode,
    ReplayDirection,
    ReplaySetupCandidate,
    _candidate_key,
    _fill_window,
    ReplayConfig,
)
from app.data.dtos import NA
from app.lifecycle.economic_identity import (
    REASON_MISSING_STRUCTURAL_ANCHOR,
    REASON_PLAN_GEOMETRY_UNLOCKED,
    REASON_PLAN_VERSION_INVARIANT_VIOLATION,
    latch_economic_identities,
    mint_plan_version_id,
    mint_setup_id,
    proven_progress_plan_version_id,
)
from app.lifecycle.identity import generation_rotation_reason, new_setup_generation_id
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.outcome_policy import canonical_plan_identity
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import apply_lifecycle_to_run_result
from app.lifecycle.state_machine import PLAN_LOCK_STATES
from app.pipeline.scanner_runner import (
    ScannerPipelineStatus,
    ScannerRunConfig,
    ScannerRunResult,
    ScannerSymbolResult,
)
from app.storage.database import SCHEMA_VERSION, open_initialized_database

# Evidence classification required by R0. Not persisted.
EVIDENCE_NORMAL_PRODUCER = "normal production path reached with synthetic inputs"
EVIDENCE_DIRECT_WRITER = "direct lower-level writer/import-supported state"
EVIDENCE_MALFORMED = "malformed/adversarial supplied state"

BASE = datetime(2026, 1, 1, tzinfo=UTC)
TIMEFRAME = "5m"
INVALIDATION = "Invalid if price accepts beyond 95."


def _now(index: int) -> str:
    return (BASE + timedelta(minutes=5 * index)).replace(microsecond=0).isoformat()


def _candle(index: int, *, high: str, low: str) -> dict[str, object]:
    opened = BASE + timedelta(minutes=5 * index)
    return {
        "timestamp": int(opened.timestamp() * 1000),
        "open": Decimal(low),
        "high": Decimal(high),
        "low": Decimal(low),
        "close": Decimal(high),
        "volume": Decimal("10"),
    }


def _candles(*indexes: int) -> tuple[dict[str, object], ...]:
    return tuple(_candle(index, high="99", low="95") for index in indexes)


def _quality() -> SetupQualityResult:
    return SetupQualityResult(
        quality_state=SetupQualityState.HIGH_QUALITY_TRADE,
        quality_grade=SetupQualityGrade.B_PLUS,
        quality_score=82,
        tradeability_score=82,
        profitability_edge_score=82,
        execution_risk_score=18,
        strongest_factors=("structure", "RR meets threshold"),
        weakest_factors=(),
        decision_reason="Synthetic R0 fixture.",
        action_label="Trade candidate",
    )


def _trade_idea(*, tp1: str = "110", tp2: str = "117", tp3: str = "124"):
    return create_trade_idea(
        {
            "symbol": "BTCUSDT",
            "exchange": "binance",
            "market_type": "perpetual",
            "direction": "long",
            "timeframe": "15m",
            "setup_type": "liquidity_grab_pullback_swing",
            "entry_low": Decimal("100"),
            "entry_high": Decimal("102"),
            "stop_loss": Decimal("95"),
            "take_profit_targets": (Decimal(tp1), Decimal(tp2), Decimal(tp3)),
            "invalidation": INVALIDATION,
            "opportunity_score": Decimal("88"),
            "opportunity_grade": "A",
            "opportunity_decision": "alert_candidate",
            "risk_approved": True,
            "best_rr": Decimal("3.2"),
            "technical_summary": "Sweep and reclaim into valid pullback.",
            "derivatives_summary": "Funding normal while open interest is rising.",
            "confirmed_facts": ("LTF BOS/CHoCH confirmed.",),
            "cancel_condition": "Cancel if price accepts beyond invalidation.",
        }
    )


def _candidate(
    *,
    mode: str = "swing",
    anchor: str | None = "sweep-a",
    tp2: str = "117",
    candles: tuple[dict[str, object], ...] | None = None,
    symbol: str = "BTCUSDT",
) -> ScannerSymbolResult:
    diagnostics: dict[str, object] = {
        "mode": mode,
        "bias": "long",
        "execution_sweep_status": "passed",
        "confirmation_structure_shift_status": "passed",
        "pullback_zone_status": "valid",
        "gates_passed": ("sweep", "bos_choch", "pullback_zone"),
        "gates_failed": (),
        "entry_low": Decimal("100"),
        "entry_high": Decimal("102"),
        "stop": Decimal("95"),
        "tp1": Decimal("110"),
        "tp2": Decimal(tp2),
        "tp3": Decimal("124"),
        "rr_to_tp2": Decimal("3.2"),
        "opportunity_score": Decimal("88"),
        "invalidation": INVALIDATION,
        "quality_grade": "B+",
    }
    if anchor is not None:
        diagnostics["setup_generation_anchor"] = anchor
    payload = {
        "symbol": symbol,
        "status": ScannerPipelineStatus.IDEA_CREATED,
        "status_history": (ScannerPipelineStatus.IDEA_CREATED,),
        "latest_high": Decimal("110"),
        "latest_low": Decimal("108"),
        "technical_score": 70,
        "trade_idea": _trade_idea(tp2=tp2),
        "valid_strategy_modes": (mode,),
        "rejected_strategy_modes": (),
        "strategy_diagnostics": {mode: diagnostics},
        "setup_quality": _quality(),
        "lifecycle_execution_timeframe": TIMEFRAME,
    }
    if candles is not None:
        payload["lifecycle_execution_candles"] = candles
        payload["lifecycle_decision_timestamp"] = BASE + timedelta(minutes=5 * max(len(candles), 1))
    return ScannerSymbolResult(**payload)


def _scan(*symbol_results: ScannerSymbolResult) -> ScannerRunResult:
    symbols = [item.symbol for item in symbol_results] or ["BTCUSDT"]
    config = ScannerRunConfig.model_validate(
        {
            "symbols": symbols,
            "exchange": "binance",
            "account_equity": Decimal("10000"),
            "risk_per_trade_pct": Decimal("1"),
        }
    )
    return ScannerRunResult(
        config=config,
        results=symbol_results,
        scanned_symbols=len(symbol_results),
        failed_symbols=0,
        trade_ideas_created=0,
        dry_run_alerts_created=0,
        journal_entries_created=0,
    )


def _apply(db_path: Path, result: ScannerRunResult, *, now: str, scan_run_id: str, cycles: int = 1):
    return apply_lifecycle_to_run_result(
        result,
        database_path=db_path,
        scan_run_id=scan_run_id,
        now=now,
        confirmation_cycles=cycles,
    )


def _sql_progress(repository: SQLiteSetupLifecycleRepository, lifecycle_id: str | None = None) -> list[dict]:
    sql = "SELECT * FROM setup_lifecycle_outcome_progress"
    params: tuple[object, ...] = ()
    if lifecycle_id is not None:
        sql += " WHERE lifecycle_id = ?"
        params = (lifecycle_id,)
    sql += " ORDER BY id"
    cursor = repository._connection.execute(sql, params)
    columns = [item[0] for item in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def _latched_record(*, lifecycle_id: str, **updates) -> SetupLifecycleRecord:
    values = {
        "lifecycle_id": lifecycle_id,
        "symbol": "BTCUSDT",
        "mode": "swing",
        "direction": "long",
        "current_state": SetupLifecycleState.ACTIONABLE_A_GRADE,
        "first_seen_at": BASE.isoformat(),
        "last_seen_at": BASE.isoformat(),
        "last_transition_at": BASE.isoformat(),
        "confirmed_at": BASE.isoformat(),
        "invalidation_reason": INVALIDATION,
        "invalidation_logic": INVALIDATION,
        "setup_identity": f"{lifecycle_id}-setup",
        "structural_anchor": "setup_generation_anchor|sweep-a",
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "95",
        "tp1": "110",
        "tp2": "117",
        "tp3": "124",
    }
    values.update(updates)
    return latch_economic_identities(
        SetupLifecycleRecord(**values),
        instrument_venue="binance",
        plan_locked=True,
    )


def test_fresh_synthetic_schema_is_v24_without_admission_columns(tmp_path: Path) -> None:
    path = tmp_path / "r0-schema.sqlite"
    with open_initialized_database(path) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        sqlite_schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        progress_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'setup_lifecycle_outcome_progress'"
        ).fetchone()[0]
        analytics_sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'setup_outcome_analytics'"
        ).fetchone()[0]
        progress_cols = {
            row[1] for row in connection.execute("PRAGMA table_info(setup_lifecycle_outcome_progress)")
        }
        analytics_cols = {
            row[1] for row in connection.execute("PRAGMA table_info(setup_outcome_analytics)")
        }
    assert user_version == SCHEMA_VERSION == 24
    assert sqlite_schema_version != user_version
    assert "UNIQUE(lifecycle_id, plan_identity)" in progress_sql
    assert "UNIQUE(lifecycle_id, final_outcome)" in analytics_sql
    assert "plan_version_id" in progress_cols
    assert "last_eligibility_decision_at" in progress_cols
    assert "last_eligibility_prefix_evidence_json" in progress_cols
    assert "admission_id" not in progress_cols
    assert "evaluation_episode_id" not in progress_cols
    assert "plan_identity" not in analytics_cols
    assert "plan_version_id" not in analytics_cols


def test_same_known_anchor_repeated_scan_and_reopen_keep_one_progress_owner(tmp_path: Path) -> None:
    db_path = tmp_path / "r0-continuation.db"
    candles_first = _candles(0, 1)
    candles_second = _candles(0, 1, 2)
    first = _apply(
        db_path,
        _scan(_candidate(candles=candles_first)),
        now=_now(1),
        scan_run_id="scan-1",
    )
    first_record = first.results[0].lifecycle_state
    assert first_record is not None, EVIDENCE_NORMAL_PRODUCER
    assert first_record.current_state in PLAN_LOCK_STATES
    assert first_record.plan_version_id is not None
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        first_progress = _sql_progress(repository, first_record.lifecycle_id)
    assert len(first_progress) == 1, EVIDENCE_NORMAL_PRODUCER

    second = _apply(
        db_path,
        _scan(_candidate(candles=candles_second)),
        now=_now(3),
        scan_run_id="scan-2",
    )
    second_record = second.results[0].lifecycle_state
    assert second_record is not None
    assert second_record.lifecycle_id == first_record.lifecycle_id
    assert second_record.setup_id == first_record.setup_id
    assert second_record.plan_version_id == first_record.plan_version_id
    assert second_record.structural_anchor == first_record.structural_anchor

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        restored = repository.get_record(symbol="BTCUSDT", mode="swing", direction="long")
        rows = _sql_progress(repository, first_record.lifecycle_id)
        all_rows = _sql_progress(repository)
    assert restored is not None
    assert restored.lifecycle_id == first_record.lifecycle_id
    assert len(rows) == 1
    assert len(all_rows) == 1
    assert rows[0]["plan_identity"] == first_progress[0]["plan_identity"]
    assert rows[0]["plan_version_id"] == first_record.plan_version_id
    assert rows[0]["last_evaluated_at"] == _now(3)
    assert first_progress[0]["last_evaluated_at"] == _now(1)
    assert "admission_id" not in rows[0]


def test_changed_anchor_through_service_supersedes_without_resolving_prior_progress(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "r0-rotation.db"
    first = _apply(
        db_path,
        _scan(_candidate(anchor="sweep-a", candles=_candles(0, 1))),
        now=_now(1),
        scan_run_id="gen-a",
    )
    generation_a = first.results[0].lifecycle_state
    assert generation_a is not None, EVIDENCE_NORMAL_PRODUCER
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        prior_progress = _sql_progress(repository, generation_a.lifecycle_id)[0]
        prior_snapshot = dict(prior_progress)

    second = _apply(
        db_path,
        _scan(_candidate(anchor="sweep-b", candles=_candles(0, 1, 2))),
        now=_now(4),
        scan_run_id="gen-b",
    )
    generation_b = second.results[0].lifecycle_state
    assert generation_b is not None, EVIDENCE_NORMAL_PRODUCER
    assert generation_b.lifecycle_id != generation_a.lifecycle_id
    assert generation_b.setup_id != generation_a.setup_id
    assert generation_b.plan_version_id != generation_a.plan_version_id
    assert generation_b.structural_anchor == "setup_generation_anchor|sweep-b"

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        current = repository.get_record(symbol="BTCUSDT", mode="swing", direction="long")
        historical = repository.get_record_by_lifecycle_id(generation_a.lifecycle_id)
        prior_after = _sql_progress(repository, generation_a.lifecycle_id)
        new_rows = _sql_progress(repository, generation_b.lifecycle_id)
        all_rows = _sql_progress(repository)
    assert current is not None
    assert current.lifecycle_id == generation_b.lifecycle_id
    assert current.is_current is True
    assert historical is not None
    assert historical.is_current is False
    assert historical.plan_version_id == generation_a.plan_version_id
    assert historical.current_state == generation_a.current_state
    assert len(prior_after) == 1
    assert prior_after[0]["last_evaluated_at"] == prior_snapshot["last_evaluated_at"]
    assert prior_after[0]["terminal_outcome"] == prior_snapshot["terminal_outcome"]
    assert len(all_rows) == 1 + len(new_rows)
    assert proven_progress_plan_version_id(historical) == generation_a.plan_version_id


def test_same_known_anchor_after_planted_cooldown_reuses_generation(tmp_path: Path) -> None:
    db_path = tmp_path / "r0-cooldown-same-anchor.db"
    first = _apply(
        db_path,
        _scan(_candidate(anchor="sweep-a", candles=_candles(0, 1))),
        now=_now(1),
        scan_run_id="before-cooldown",
    )
    record = first.results[0].lifecycle_state
    assert record is not None, EVIDENCE_NORMAL_PRODUCER
    planted = record.model_copy(
        update={
            "current_state": SetupLifecycleState.COOLDOWN,
            "previous_state": SetupLifecycleState.SL_HIT,
            "cooldown_until": _now(2),
        }
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(planted)
        progress_before = _sql_progress(repository, record.lifecycle_id)

    reason = generation_rotation_reason(
        planted,
        observed_structural_anchor="setup_generation_anchor|sweep-a",
        setup_observable=True,
        terminal_observation=False,
        now=_now(10),
    )
    assert reason is None

    second = _apply(
        db_path,
        _scan(_candidate(anchor="sweep-a", candles=_candles(0, 1, 2))),
        now=_now(10),
        scan_run_id="after-cooldown",
    )
    resumed = second.results[0].lifecycle_state
    assert resumed is not None, EVIDENCE_DIRECT_WRITER
    assert resumed.lifecycle_id == record.lifecycle_id
    assert resumed.setup_id == record.setup_id
    assert resumed.plan_version_id == record.plan_version_id
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        rows = _sql_progress(repository, record.lifecycle_id)
        generations = repository.list_records_for_symbol(symbol="BTCUSDT")
    assert len(generations) == 1
    assert len(rows) == 1
    assert rows[0]["id"] == progress_before[0]["id"]


def test_missing_anchor_uuid_is_bookkeeping_not_economic_opportunity(tmp_path: Path) -> None:
    db_path = tmp_path / "r0-missing-anchor.db"
    first = _apply(
        db_path,
        _scan(_candidate(anchor=None, candles=_candles(0, 1))),
        now=_now(1),
        scan_run_id="missing-1",
    )
    record = first.results[0].lifecycle_state
    assert record is not None, EVIDENCE_NORMAL_PRODUCER
    assert record.structural_anchor == NA
    assert record.setup_id is None
    assert record.plan_version_id is None
    assert REASON_MISSING_STRUCTURAL_ANCHOR in (record.economic_identity_reason or "")
    first_id = record.lifecycle_id
    known_digest = new_setup_generation_id(
        symbol="BTCUSDT",
        mode="swing",
        direction="long",
        structural_anchor="setup_generation_anchor|sweep-a",
    )
    another_missing = new_setup_generation_id(
        symbol="BTCUSDT",
        mode="swing",
        direction="long",
        structural_anchor=NA,
    )
    assert first_id != known_digest
    assert another_missing != known_digest
    assert first_id != another_missing
    assert len(first_id) == 32

    second = _apply(
        db_path,
        _scan(_candidate(anchor=None, candles=_candles(0, 1, 2))),
        now=_now(3),
        scan_run_id="missing-2",
    )
    continued = second.results[0].lifecycle_state
    assert continued is not None
    assert continued.lifecycle_id == first_id
    assert continued.setup_id is None
    assert continued.plan_version_id is None

    third = _apply(
        db_path,
        _scan(_candidate(anchor="sweep-a", candles=_candles(0, 1, 2))),
        now=_now(4),
        scan_run_id="missing-then-known",
    )
    latched = third.results[0].lifecycle_state
    assert latched is not None
    assert latched.lifecycle_id == first_id
    assert latched.structural_anchor == "setup_generation_anchor|sweep-a"
    assert latched.setup_id == mint_setup_id(
        instrument_venue="binance",
        symbol="BTCUSDT",
        direction="long",
        mode="swing",
        structural_anchor="setup_generation_anchor|sweep-a",
    ).identity
    assert latched.plan_version_id is not None


def test_missing_anchor_cooldown_rotation_mints_new_uuid_without_setup_id(tmp_path: Path) -> None:
    db_path = tmp_path / "r0-missing-cooldown.db"
    first = _apply(
        db_path,
        _scan(_candidate(anchor=None)),
        now=_now(1),
        scan_run_id="missing-pre",
    )
    record = first.results[0].lifecycle_state
    assert record is not None, EVIDENCE_NORMAL_PRODUCER
    planted = record.model_copy(
        update={
            "current_state": SetupLifecycleState.COOLDOWN,
            "cooldown_until": _now(2),
            "structural_anchor": NA,
        }
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(planted)

    reason = generation_rotation_reason(
        planted,
        observed_structural_anchor=NA,
        setup_observable=True,
        terminal_observation=False,
        now=_now(10),
    )
    assert reason == "completed_cooldown_new_setup"

    second = _apply(
        db_path,
        _scan(_candidate(anchor=None)),
        now=_now(10),
        scan_run_id="missing-post",
    )
    rotated = second.results[0].lifecycle_state
    assert rotated is not None, EVIDENCE_DIRECT_WRITER
    assert rotated.lifecycle_id != record.lifecycle_id
    assert rotated.setup_id is None
    assert rotated.plan_version_id is None
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        historical = repository.get_record_by_lifecycle_id(record.lifecycle_id)
        current = repository.get_record(symbol="BTCUSDT", mode="swing", direction="long")
    assert historical is not None and historical.is_current is False
    assert current is not None and current.lifecycle_id == rotated.lifecycle_id


def test_ordinary_same_anchor_producer_does_not_fan_out_same_plan_version(tmp_path: Path) -> None:
    db_path = tmp_path / "r0-no-fanout.db"
    _apply(
        db_path,
        _scan(_candidate(anchor="sweep-a", candles=_candles(0, 1))),
        now=_now(1),
        scan_run_id="a-1",
    )
    _apply(
        db_path,
        _scan(_candidate(anchor="sweep-a", candles=_candles(0, 1, 2))),
        now=_now(3),
        scan_run_id="a-2",
    )
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        producer_rows = list(repository.list_outcome_progress())
        producer_records = repository.list_records_for_symbol(symbol="BTCUSDT")
    assert len(producer_records) == 1, EVIDENCE_NORMAL_PRODUCER
    assert len(producer_rows) == 1, EVIDENCE_NORMAL_PRODUCER
    assert len({row.plan_version_id for row in producer_rows}) == 1

    first = _latched_record(lifecycle_id="gen-a")
    second = _latched_record(lifecycle_id="gen-b")
    assert first.plan_version_id == second.plan_version_id
    with SQLiteSetupLifecycleRepository(tmp_path / "r0-direct-fanout.db") as repository:
        repository.upsert_record(first)
        evaluate_closed_candle_outcomes(
            first,
            execution_candles=_candles(0, 1),
            execution_timeframe=TIMEFRAME,
            decision_timestamp=_now(1),
            evaluated_at=_now(1),
            repository=repository,
        )
        repository.supersede_record(first.lifecycle_id)
        repository.upsert_record(second)
        evaluate_closed_candle_outcomes(
            second,
            execution_candles=_candles(0, 1),
            execution_timeframe=TIMEFRAME,
            decision_timestamp=_now(1),
            evaluated_at=_now(1),
            repository=repository,
        )
        imported = list(repository.list_outcome_progress())
    assert len(imported) == 2, EVIDENCE_DIRECT_WRITER
    payload = project_outcome_ownership(
        lifecycle_records=[first, second],
        progress_rows=imported,
        provenance={"synthetic": True, "source_namespace": "r0-direct", "coverage_complete": False},
    )
    same_plan = [
        item
        for item in payload["plan_interpretations"]
        if item["plan_version_id"] == first.plan_version_id
    ]
    assert same_plan[0]["interpretable"] is False
    assert same_plan[0]["integrity_status"] == STATUS_AMBIGUOUS_CONTEXT


def test_triggered_may_adopt_geometry_then_lock_preserves_latched_plan(tmp_path: Path) -> None:
    db_path = tmp_path / "r0-revision.db"
    first = _apply(
        db_path,
        _scan(_candidate(tp2="117")),
        now=_now(1),
        scan_run_id="unlocked",
        cycles=2,
    )
    unlocked = first.results[0].lifecycle_state
    assert unlocked is not None, EVIDENCE_NORMAL_PRODUCER
    assert unlocked.current_state == SetupLifecycleState.TRIGGERED
    assert SetupLifecycleState.TRIGGERED not in PLAN_LOCK_STATES
    assert unlocked.plan_version_id is None
    assert unlocked.economic_identity_reason == REASON_PLAN_GEOMETRY_UNLOCKED
    assert unlocked.tp2 in {"117", "117.0"}

    second = _apply(
        db_path,
        _scan(_candidate(tp2="118")),
        now=_now(2),
        scan_run_id="lock",
        cycles=2,
    )
    locked = second.results[0].lifecycle_state
    assert locked is not None
    assert locked.lifecycle_id == unlocked.lifecycle_id
    assert locked.current_state == SetupLifecycleState.CONFIRMED
    assert locked.tp2 in {"118", "118.0"}
    assert locked.plan_version_id is not None
    latched_plan = locked.plan_version_id
    latched_tp2 = locked.tp2

    third = _apply(
        db_path,
        _scan(_candidate(tp2="119")),
        now=_now(3),
        scan_run_id="after-lock",
        cycles=2,
    )
    frozen = third.results[0].lifecycle_state
    assert frozen is not None
    assert frozen.lifecycle_id == locked.lifecycle_id
    assert frozen.tp2 == latched_tp2
    assert frozen.plan_version_id == latched_plan

    mutated = frozen.model_copy(update={"tp2": "999"})
    preserved = latch_economic_identities(
        mutated,
        instrument_venue="binance",
        plan_locked=True,
        previous=frozen,
    )
    assert preserved.plan_version_id == latched_plan
    assert REASON_PLAN_VERSION_INVARIANT_VIOLATION in (preserved.economic_identity_reason or "")
    assert proven_progress_plan_version_id(preserved) is None


def test_omitted_symbol_and_missing_candles_do_not_advance_progress(tmp_path: Path) -> None:
    db_path = tmp_path / "r0-absence.db"
    first = _apply(
        db_path,
        _scan(_candidate(candles=_candles(0, 1))),
        now=_now(1),
        scan_run_id="present",
    )
    record = first.results[0].lifecycle_state
    assert record is not None, EVIDENCE_NORMAL_PRODUCER
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        before = _sql_progress(repository, record.lifecycle_id)[0]

    omitted = _apply(
        db_path,
        _scan(),
        now=_now(5),
        scan_run_id="omitted-symbol",
    )
    assert omitted.results == ()
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        after_omit = _sql_progress(repository, record.lifecycle_id)[0]
        still_current = repository.get_record(symbol="BTCUSDT", mode="swing", direction="long")
    assert still_current is not None
    assert still_current.lifecycle_id == record.lifecycle_id
    assert still_current.last_seen_at == record.last_seen_at
    assert after_omit["last_evaluated_at"] == before["last_evaluated_at"]
    assert after_omit["evaluation_cursor_close_at"] == before["evaluation_cursor_close_at"]

    without_candles = _apply(
        db_path,
        _scan(_candidate()),
        now=_now(6),
        scan_run_id="no-candles",
    )
    updated = without_candles.results[0].lifecycle_state
    assert updated is not None
    assert updated.lifecycle_id == record.lifecycle_id
    assert updated.last_seen_at == _now(6)
    assert without_candles.results[0].lifecycle_outcome_progress is None
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        after_skip = _sql_progress(repository, record.lifecycle_id)[0]
    assert after_skip["last_evaluated_at"] == before["last_evaluated_at"]

    service_text = Path("app/lifecycle/service.py").read_text(encoding="utf-8")
    assert "if final_record is not None and execution_candles is not None:" in service_text
    assert 'if status == "not_run"' in service_text
    production_hits = []
    for path in (*Path("app").rglob("*.py"), *Path("scripts").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if "evaluate_closed_candle_outcomes(" in text and path.name != "outcomes.py":
            production_hits.append(path.as_posix())
    assert production_hits == ["app/lifecycle/service.py"]


def test_existing_keys_cannot_recover_admission_or_source_context(tmp_path: Path) -> None:
    db_path = tmp_path / "r0-underdetermined.db"
    result = _apply(
        db_path,
        _scan(_candidate(candles=_candles(0, 1))),
        now=_now(1),
        scan_run_id="ghost-run",
    )
    record = result.results[0].lifecycle_state
    assert record is not None, EVIDENCE_NORMAL_PRODUCER
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        progress = repository.list_outcome_progress(lifecycle_id=record.lifecycle_id)[0]
        row = _sql_progress(repository, record.lifecycle_id)[0]
        events = repository.list_events(lifecycle_id=record.lifecycle_id)
        scan_rows = repository._connection.execute("SELECT run_id FROM scan_runs").fetchall()
    assert any(event.scan_run_id == "ghost-run" for event in events)
    assert scan_rows == []
    assert "scan_run_id" not in row
    assert "admission_id" not in row
    envelope = row["last_eligibility_prefix_evidence_json"]
    if envelope not in (None, ""):
        parsed = json.loads(envelope)
        assert "admission_id" not in parsed
        assert "source_namespace" not in parsed
    payload = project_outcome_ownership(
        lifecycle_records=[record],
        progress_rows=[progress],
        provenance={"synthetic": True, "source_namespace": "unspecified", "coverage_complete": True},
    )
    context = payload["evaluations"][0]["evaluation_context"]
    assert context["complete"] is False
    assert payload["canonical_outcome_per_plan_version"]["established"] is False
    assert payload["unavailable_metrics"]["unique_trade_count"]["status"] == UNAVAILABLE
    assert evidence_contract_payload()["unique_trade_count"]["status"] == UNAVAILABLE
    assert progress.plan_version_id == record.plan_version_id
    assert canonical_plan_identity(record) == progress.plan_identity
    assert canonical_plan_identity(record) != record.plan_version_id


def test_mode_and_replay_keys_are_not_research_episode_keys(tmp_path: Path) -> None:
    db_path = tmp_path / "r0-mode-replay.db"
    combined = _apply(
        db_path,
        _scan(
            _candidate(mode="swing", candles=_candles(0, 1)),
            _candidate(mode="scalp", candles=_candles(0, 1)),
        ),
        now=_now(1),
        scan_run_id="modes",
    )
    swing = next(item.lifecycle_state for item in combined.results if item.lifecycle_state.mode == "swing")
    scalp = next(item.lifecycle_state for item in combined.results if item.lifecycle_state.mode == "scalp")
    assert swing is not None and scalp is not None, EVIDENCE_NORMAL_PRODUCER
    assert swing.lifecycle_id != scalp.lifecycle_id
    assert swing.setup_id != scalp.setup_id
    assert swing.plan_version_id != scalp.plan_version_id
    swing_public = _public_economic_setup_plan(
        combined.results[0] if combined.results[0].lifecycle_state.mode == "swing" else combined.results[1],
        telegram_signal_message_from_symbol(
            combined.results[0] if combined.results[0].lifecycle_state.mode == "swing" else combined.results[1]
        ),
    )
    scalp_public = _public_economic_setup_plan(
        combined.results[0] if combined.results[0].lifecycle_state.mode == "scalp" else combined.results[1],
        telegram_signal_message_from_symbol(
            combined.results[0] if combined.results[0].lifecycle_state.mode == "scalp" else combined.results[1]
        ),
    )
    assert swing_public.plan_id != NA
    assert scalp_public.plan_id != NA
    assert swing_public.plan_id == scalp_public.plan_id

    with SQLiteSetupLifecycleRepository(db_path) as repository:
        rows = _sql_progress(repository)
    assert len(rows) == 2
    assert {row["mode"] for row in rows} == {"swing", "scalp"}

    setup = mint_setup_id(
        instrument_venue="binance",
        symbol="BTCUSDT",
        direction="long",
        mode="swing",
        structural_anchor="setup_generation_anchor|sweep-a",
    ).identity
    plan = mint_plan_version_id(
        setup_id=setup,
        entry_low="100",
        entry_high="102",
        stop_loss="95",
        tp1="110",
        tp2="117",
        tp3="124",
        invalidation=INVALIDATION,
    ).identity
    first = ReplaySetupCandidate(
        symbol="BTCUSDT",
        mode=LiquidityGrabMode.swing,
        direction=ReplayDirection.LONG,
        detected_at_index=0,
        entry=Decimal("101"),
        entry_low=Decimal("100"),
        entry_high=Decimal("102"),
        stop=Decimal("95"),
        tp1=Decimal("110"),
        tp2=Decimal("117"),
        sweep_candle_index=3,
        pullback_calculation_timeframe="15m",
        invalidation=INVALIDATION,
    )
    shifted_sweep = first.model_copy(update={"sweep_candle_index": 8})
    other_mode = first.model_copy(update={"mode": LiquidityGrabMode.scalp})
    assert _candidate_key(first) != plan
    assert _candidate_key(first) != _candidate_key(shifted_sweep)
    assert _candidate_key(first) != _candidate_key(other_mode)
    config = ReplayConfig()
    assert _fill_window(first, config) != _fill_window(other_mode, config)


def test_p3b1_insert_only_attribution_and_progress_conflict_sql() -> None:
    source = Path("app/lifecycle/repositories.py").read_text(encoding="utf-8")
    conflict = source.split("ON CONFLICT(lifecycle_id, plan_identity) DO UPDATE SET", 1)[1]
    update_block = conflict.split("def upsert_outcome_analytics", 1)[0]
    assert "plan_version_id = excluded.plan_version_id" not in update_block
    assert "plan_version_id" in source.split("INSERT INTO setup_lifecycle_outcome_progress", 1)[1].split(
        "ON CONFLICT", 1
    )[0]


def test_claim_preservation_markers_remain_unproven() -> None:
    payload = evidence_contract_payload()
    assert payload["unique_trade_count"]["status"] == UNAVAILABLE
    assert SetupLifecycleState.TRIGGERED not in PLAN_LOCK_STATES
    assert {
        SetupLifecycleState.WATCHLISTED,
        SetupLifecycleState.STALKING,
        SetupLifecycleState.CONFIRMED,
        SetupLifecycleState.ACTIONABLE_A_GRADE,
        SetupLifecycleState.A_GRADE_WATCH,
        SetupLifecycleState.EXECUTING,
        SetupLifecycleState.MANAGING,
    } == PLAN_LOCK_STATES
    _ = EVIDENCE_MALFORMED
