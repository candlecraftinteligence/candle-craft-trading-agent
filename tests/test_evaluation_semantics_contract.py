"""EVAL_SEMANTICS: paired runtime/replay evaluation-policy proofs.

These fixtures are synthetic. They do not read live or existing scan databases.
They do not mint admission, episode, trade, or policy IDs.
Evidence labels below are test documentation, not application enums.

Reachability labels:
- normal production path reached with synthetic inputs
- direct evaluator / import-supported supplied state
- malformed / adversarial supplied state
- source inspection only, not executed here
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from app.analytics.evidence_contract import UNAVAILABLE, evidence_contract_payload
from app.data.dtos import NA
from app.analytics.outcome_ownership import project_outcome_ownership
from app.backtesting.strategy_replay import (
    LiquidityGrabMode,
    ReplayConfig,
    ReplayDirection,
    ReplayOutcome,
    ReplaySetupCandidate,
    StrategyReplayEngine,
    _normalize_candles,
    _simulate_trade,
)
from app.lifecycle.economic_identity import latch_economic_identities
from app.lifecycle.models import SetupLifecycleRecord, SetupLifecycleState
from app.lifecycle.outcome_policy import entry_touched, stored_plan_geometry
from app.lifecycle.outcomes import (
    OUTCOME_ELIGIBLE_STATES,
    evaluate_closed_candle_outcomes,
)
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import SetupLifecycleService
from app.lifecycle.state_machine import PLAN_LOCK_STATES
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerSymbolResult
from app.storage.database import SCHEMA_VERSION, open_initialized_database
from tests.test_strategy_replay import _run_replay

EVIDENCE_NORMAL_PRODUCER = "normal production path reached with synthetic inputs"
EVIDENCE_SUPPLIED_STATE = "direct evaluator / import-supported supplied state"
EVIDENCE_MALFORMED = "malformed / adversarial supplied state"
EVIDENCE_SOURCE_ONLY = "source inspection only, not executed here"

COMPARABLE = "comparable_for_named_dimensions"
NON_EQUIVALENT = "non_equivalent_implemented_policy_differs"
NOT_COMPARABLE = "not_comparable_missing_or_conflicting_evidence"

BASE = datetime(2026, 1, 1, tzinfo=UTC)
TIMEFRAME = "5m"
INVALIDATION = "Closed structure beyond the stored stop invalidates the plan."
REPLAY_ENTRY = Decimal("101")


def _close(index: int) -> datetime:
    return BASE + timedelta(minutes=5 * (index + 1))


def _decision(index: int) -> str:
    return _close(index).isoformat()


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


def _no_touch(index: int, direction: str) -> dict[str, object]:
    if direction == "long":
        return _candle(index, high="99", low="95")
    return _candle(index, high="107", low="103")


def _both_entry(index: int) -> dict[str, object]:
    return _candle(index, high="103", low="99")


def _zone_not_point(index: int) -> dict[str, object]:
    return _candle(index, high="100.5", low="99.5")


def _target(index: int, direction: str, target: int) -> dict[str, object]:
    if direction == "long":
        values = {1: ("111", "103"), 2: ("121", "111"), 3: ("131", "121")}
    else:
        values = {1: ("99", "91"), 2: ("91", "83"), 3: ("83", "75")}
    high, low = values[target]
    return _candle(index, high=high, low=low)


def _stop(index: int, direction: str) -> dict[str, object]:
    if direction == "long":
        return _candle(index, high="105", low="89")
    return _candle(index, high="113", low="99")


def _levels(direction: str) -> dict[str, str]:
    if direction == "long":
        return {
            "entry_low": "100",
            "entry_high": "102",
            "stop_loss": "90",
            "tp1": "110",
            "tp2": "120",
            "tp3": "130",
        }
    return {
        "entry_low": "100",
        "entry_high": "102",
        "stop_loss": "112",
        "tp1": "92",
        "tp2": "84",
        "tp3": "76",
    }


def _record(*, direction: str = "long", lifecycle_id: str = "life-eval", **updates) -> SetupLifecycleRecord:
    values = {
        "lifecycle_id": lifecycle_id,
        "symbol": "BTCUSDT",
        "mode": "swing",
        "direction": direction,
        "current_state": SetupLifecycleState.ACTIONABLE_A_GRADE,
        "first_seen_at": BASE.isoformat(),
        "last_seen_at": BASE.isoformat(),
        "last_transition_at": BASE.isoformat(),
        "confirmed_at": BASE.isoformat(),
        "invalidation_reason": INVALIDATION,
        "invalidation_logic": INVALIDATION,
        "setup_identity": f"{lifecycle_id}-setup",
        **_levels(direction),
    }
    values.update(updates)
    return SetupLifecycleRecord(**values)


def _latched(*, direction: str = "long", **updates) -> SetupLifecycleRecord:
    return latch_economic_identities(
        _record(direction=direction, **updates),
        instrument_venue="binance",
        plan_locked=True,
    )


def _runtime(
    tmp_path: Path,
    record: SetupLifecycleRecord,
    candles: list[dict[str, object]],
    *,
    decision_timestamp: datetime | None = None,
    evaluated_at: str | None = None,
    db_name: str = "runtime.sqlite",
):
    index = len(candles) - 1 if candles else 0
    decision = decision_timestamp if decision_timestamp is not None else _close(index)
    processed = evaluated_at if evaluated_at is not None else _decision(index)
    with SQLiteSetupLifecycleRepository(tmp_path / db_name) as repository:
        repository.upsert_record(record)
        return evaluate_closed_candle_outcomes(
            record,
            execution_candles=candles,
            execution_timeframe=TIMEFRAME,
            decision_timestamp=decision,
            evaluated_at=processed,
            repository=repository,
            scan_run_id="synthetic-eval-semantics",
        )


def _candidate(*, direction: str, detected_at_index: int = 0) -> ReplaySetupCandidate:
    levels = _levels(direction)
    return ReplaySetupCandidate(
        symbol="BTCUSDT",
        mode=LiquidityGrabMode.swing,
        direction=ReplayDirection.LONG if direction == "long" else ReplayDirection.SHORT,
        detected_at_index=detected_at_index,
        detected_at_timestamp=int(BASE.timestamp() * 1000),
        entry=REPLAY_ENTRY,
        entry_low=Decimal(levels["entry_low"]),
        entry_high=Decimal(levels["entry_high"]),
        stop=Decimal(levels["stop_loss"]),
        tp1=Decimal(levels["tp1"]),
        tp2=Decimal(levels["tp2"]),
        tp3=Decimal(levels["tp3"]),
        invalidation=INVALIDATION,
    )


def _replay(
    candles: list[dict[str, object]],
    *,
    direction: str,
    detected_at_index: int = 0,
    max_fill_candles: int = 20,
    max_hold_candles: int = 20,
    same_candle_policy: str = "conservative",
):
    config = ReplayConfig(
        execution_timeframe=TIMEFRAME,
        max_fill_candles=max_fill_candles,
        max_hold_candles=max_hold_candles,
        same_candle_policy=same_candle_policy,
    )
    return _simulate_trade(
        _candidate(direction=direction, detected_at_index=detected_at_index),
        _normalize_candles(candles, timeframe=TIMEFRAME),
        config,
    )


def test_schema_remains_v24_without_admission_policy_or_source_binding(tmp_path: Path) -> None:
    path = tmp_path / "eval-semantics-schema.sqlite"
    with open_initialized_database(path) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        sqlite_schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        progress_cols = {
            row[1] for row in connection.execute("PRAGMA table_info(setup_lifecycle_outcome_progress)")
        }
        analytics_cols = {
            row[1] for row in connection.execute("PRAGMA table_info(setup_outcome_analytics)")
        }
        table_names = {
            row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
    assert user_version == SCHEMA_VERSION == 24
    assert sqlite_schema_version != user_version
    for forbidden in (
        "admission_id",
        "evaluation_episode_id",
        "evaluation_policy_id",
        "policy_hash",
        "source_namespace",
    ):
        assert forbidden not in progress_cols
        assert forbidden not in analytics_cols
    assert "admitted_evaluation_episodes" not in table_names


@pytest.mark.parametrize("direction", ["long", "short"])
def test_positive_comparable_subset_fill_ladder_and_separate_stop(
    tmp_path: Path,
    direction: str,
) -> None:
    # Evidence: direct evaluator / import-supported supplied state.
    # Declared common assumptions: 5m closed candles; runtime decision_timestamp is
    # the close of the last supplied bar; replay receives the same series with
    # detected_at_index=0 so fill search starts at index 1; replay entry is 101,
    # inside the runtime zone [100, 102]; confirmation is exactly at candle-0 open.
    assert EVIDENCE_SUPPLIED_STATE
    ladder = [
        _no_touch(0, direction),
        _both_entry(1),
        _target(2, direction, 1),
        _target(3, direction, 2),
        _target(4, direction, 3),
    ]
    runtime_ladder = _runtime(tmp_path, _record(direction=direction), ladder, db_name=f"ladder-{direction}.sqlite")
    replay_ladder = _replay(ladder, direction=direction)

    assert runtime_ladder.progress is not None
    assert runtime_ladder.progress.entry_at == _decision(1)
    assert runtime_ladder.progress.tp1_at == _decision(2)
    assert runtime_ladder.progress.tp2_at == _decision(3)
    assert runtime_ladder.progress.tp3_at == _decision(4)
    assert runtime_ladder.progress.terminal_outcome == SetupLifecycleState.TP_HIT.value
    assert runtime_ladder.progress.stop_at is None
    assert replay_ladder.filled is True
    assert replay_ladder.fill_index == 1
    assert replay_ladder.outcome == ReplayOutcome.TP3_HIT
    assert replay_ladder.highest_tp_hit == 3
    assert replay_ladder.sl_hit is False
    # Comparable: fill on index 1; TP1/TP2/TP3 touches on 2/3/4; both terminate on bar 4.
    # Non-equivalent: runtime label TP_HIT vs replay tp3_hit; entry_at is close ISO vs fill_timestamp open-ms.
    assert runtime_ladder.progress.entry_at != str(replay_ladder.fill_timestamp)
    assert COMPARABLE
    assert NON_EQUIVALENT

    stop_path = [_no_touch(0, direction), _both_entry(1), _stop(2, direction)]
    runtime_stop = _runtime(
        tmp_path,
        _record(direction=direction, lifecycle_id=f"life-stop-{direction}"),
        stop_path,
        db_name=f"stop-{direction}.sqlite",
    )
    replay_stop = _replay(stop_path, direction=direction)
    assert runtime_stop.progress.entry_at == _decision(1)
    assert runtime_stop.progress.tp1_at is None
    assert runtime_stop.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert runtime_stop.progress.stop_at == _decision(2)
    assert replay_stop.filled is True
    assert replay_stop.fill_index == 1
    assert replay_stop.outcome == ReplayOutcome.STOPPED
    assert replay_stop.highest_tp_hit == 0
    assert replay_stop.r_multiple == Decimal("-1.00000000")


def test_zone_overlap_without_point_touch_is_non_equivalent(tmp_path: Path) -> None:
    # Evidence: direct evaluator / import-supported supplied state.
    # Do not move replay entry=101 to hide the difference.
    candles = [_no_touch(0, "long"), _zone_not_point(1)]
    geometry = stored_plan_geometry(_record())
    high = Decimal("100.5")
    low = Decimal("99.5")
    assert entry_touched(high, low, geometry) is True
    assert not (low <= REPLAY_ENTRY <= high)

    runtime = _runtime(tmp_path, _record(), candles, db_name="zone.sqlite")
    replay = _replay(candles, direction="long", max_fill_candles=2)
    assert runtime.progress.entry_at == _decision(1)
    assert runtime.progress.terminal_outcome == NA
    assert runtime.progress.stop_at is None
    assert replay.filled is False
    assert replay.outcome == ReplayOutcome.NOT_FILLED
    assert replay.fill_index == "N/A"


def test_entry_candle_target_is_credited_by_replay_not_runtime(tmp_path: Path) -> None:
    # Evidence: direct evaluator / import-supported supplied state.
    # Entry candle also reaches TP1 without stop. Discriminating later path is a stop.
    candles = [
        _no_touch(0, "long"),
        _candle(1, high="111", low="99"),
        _stop(2, "long"),
    ]
    runtime = _runtime(tmp_path, _record(), candles, db_name="entry-target.sqlite")
    replay = _replay(candles, direction="long")
    assert runtime.progress.entry_at == _decision(1)
    assert runtime.progress.tp1_at is None
    assert runtime.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert runtime.progress.stop_at == _decision(2)
    assert replay.filled is True
    assert replay.fill_index == 1
    assert replay.outcome == ReplayOutcome.TP1_HIT
    assert replay.highest_tp_hit == 1
    assert replay.sl_hit is False
    # Replay already returned on the fill candle, so the later stop is not an exit.


def test_same_candle_entry_stop_and_post_entry_stop_target(tmp_path: Path) -> None:
    # Evidence: direct evaluator / import-supported supplied state.
    entry_stop = [_no_touch(0, "long"), _candle(1, high="103", low="89")]
    runtime_entry_stop = _runtime(tmp_path, _record(), entry_stop, db_name="entry-stop.sqlite")
    replay_conservative = _replay(entry_stop, direction="long", same_candle_policy="conservative")
    replay_optimistic = _replay(entry_stop, direction="long", same_candle_policy="optimistic")
    assert runtime_entry_stop.progress.entry_at == _decision(1)
    assert runtime_entry_stop.progress.stop_at == _decision(1)
    assert runtime_entry_stop.progress.tp1_at is None
    assert runtime_entry_stop.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert replay_conservative.outcome == ReplayOutcome.STOPPED
    assert replay_conservative.filled is True
    # Optimistic still fills then sees stop+no-final-target. TP1 is not touched (high 103 < 110),
    # so optimistic also stops. The policy difference appears when a target is also present.
    assert replay_optimistic.outcome == ReplayOutcome.STOPPED

    post = [_no_touch(0, "long"), _both_entry(1), _candle(2, high="111", low="89")]
    runtime_post = _runtime(
        tmp_path,
        _record(lifecycle_id="life-post"),
        post,
        db_name="post-stop-target.sqlite",
    )
    replay_post_c = _replay(post, direction="long", same_candle_policy="conservative")
    replay_post_o = _replay(post, direction="long", same_candle_policy="optimistic")
    assert runtime_post.progress.entry_at == _decision(1)
    assert runtime_post.progress.tp1_at is None
    assert runtime_post.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert replay_post_c.outcome == ReplayOutcome.STOPPED
    assert replay_post_c.highest_tp_hit == 0
    # Optimistic: skip conservative stop-first; TP1 is touched then stop with highest_tp>0
    # returns the prior/new target result, not STOPPED.
    assert replay_post_o.outcome == ReplayOutcome.TP1_HIT
    assert replay_post_o.highest_tp_hit == 1
    assert replay_post_o.sl_hit is False


def test_earlier_tp_then_stop_is_milestone_not_a_winning_exit(tmp_path: Path) -> None:
    # Evidence: direct evaluator / import-supported supplied state.
    candles = [
        _no_touch(0, "long"),
        _both_entry(1),
        _target(2, "long", 1),
        _stop(3, "long"),
    ]
    runtime = _runtime(tmp_path, _record(), candles, db_name="tp-then-sl.sqlite")
    replay = _replay(candles, direction="long")
    assert runtime.progress.tp1_at == _decision(2)
    assert runtime.progress.tp2_at is None
    assert runtime.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert runtime.progress.stop_at == _decision(3)
    assert replay.outcome == ReplayOutcome.TP1_HIT
    assert replay.highest_tp_hit == 1
    assert replay.tp1_hit is True
    assert replay.sl_hit is False
    assert replay.r_multiple > 0
    # Matching replay TP1 to runtime TP1 would conceal the later stop.


def test_horizon_unfilled_filled_unresolved_and_input_exhaustion(tmp_path: Path) -> None:
    # Evidence: direct evaluator / import-supported supplied state.
    delayed_fill = [_no_touch(0, "long"), _no_touch(1, "long"), _both_entry(2)]
    runtime_delayed = _runtime(tmp_path, _record(), delayed_fill, db_name="horizon-fill.sqlite")
    replay_missed = _replay(delayed_fill, direction="long", max_fill_candles=1)
    assert runtime_delayed.progress.entry_at == _decision(2)
    assert replay_missed.filled is False
    assert replay_missed.outcome == ReplayOutcome.NOT_FILLED

    unresolved = [_no_touch(0, "long"), _both_entry(1), _no_touch(2, "long"), _target(3, "long", 1)]
    runtime_unresolved = _runtime(
        tmp_path,
        _record(lifecycle_id="life-unresolved"),
        unresolved[:3],
        db_name="horizon-open.sqlite",
    )
    replay_expired = _replay(unresolved, direction="long", max_fill_candles=5, max_hold_candles=1)
    assert runtime_unresolved.progress.entry_at == _decision(1)
    assert runtime_unresolved.progress.tp1_at is None
    assert runtime_unresolved.progress.terminal_outcome == NA
    assert runtime_unresolved.record.current_state == SetupLifecycleState.MANAGING
    # Replay hold window is one candle after fill: last_index = 2. TP1 on index 3 is outside hold.
    assert replay_expired.filled is True
    assert replay_expired.outcome == ReplayOutcome.EXPIRED
    assert replay_expired.highest_tp_hit == 0

    exhausted = [_no_touch(0, "long"), _both_entry(1)]
    runtime_exhausted = _runtime(
        tmp_path,
        _record(lifecycle_id="life-exhausted"),
        exhausted,
        db_name="horizon-exhausted.sqlite",
    )
    replay_exhausted = _replay(exhausted, direction="long", max_fill_candles=20, max_hold_candles=20)
    assert runtime_exhausted.progress.entry_at == _decision(1)
    assert runtime_exhausted.progress.terminal_outcome == NA
    assert replay_exhausted.filled is True
    assert replay_exhausted.outcome == ReplayOutcome.EXPIRED
    # Input exhaustion is not a runtime economic terminal. Replay EXPIRED is a hold/end-of-input result.


def test_causal_boundary_exact_partial_later_processing_and_as_of_mutation(tmp_path: Path) -> None:
    # Evidence: direct evaluator / import-supported supplied state.
    candles = [_no_touch(0, "long"), _both_entry(1), _target(2, "long", 1)]
    exact_open_entry_on_detection = [_both_entry(0), _no_touch(1, "long")]
    runtime_detection = _runtime(
        tmp_path,
        _record(),
        exact_open_entry_on_detection,
        db_name="boundary-detect.sqlite",
    )
    replay_detection = _replay(exact_open_entry_on_detection, direction="long", max_fill_candles=1)
    assert runtime_detection.progress.entry_at == _decision(0)
    assert replay_detection.filled is False
    assert replay_detection.outcome == ReplayOutcome.NOT_FILLED

    partial_record = _record(lifecycle_id="life-partial", confirmed_at=(BASE + timedelta(minutes=1)).isoformat())
    partial_candles = [_both_entry(0), _both_entry(1)]
    runtime_partial = _runtime(tmp_path, partial_record, partial_candles, db_name="boundary-partial.sqlite")
    replay_partial = _replay(partial_candles, direction="long")
    # Runtime skips the partially overlapping confirmation bar; first fully post-boundary open is candle 1.
    assert runtime_partial.progress.entry_at == _decision(1)
    assert replay_partial.fill_index == 1

    cutoff = _close(1)
    later_clock = (BASE + timedelta(hours=6)).isoformat()
    runtime_cutoff = _runtime(
        tmp_path,
        _record(lifecycle_id="life-cutoff"),
        candles,
        decision_timestamp=cutoff,
        evaluated_at=later_clock,
        db_name="boundary-cutoff.sqlite",
    )
    assert runtime_cutoff.progress.entry_at == _decision(1)
    assert runtime_cutoff.progress.tp1_at is None
    assert runtime_cutoff.progress.last_evaluated_at == later_clock

    mutated = copy.deepcopy(candles)
    mutated[2] = _candle(2, high="9000", low="1")
    runtime_mutated = _runtime(
        tmp_path,
        _record(lifecycle_id="life-mutated"),
        mutated,
        decision_timestamp=cutoff,
        evaluated_at=later_clock,
        db_name="boundary-mutated.sqlite",
    )
    assert runtime_mutated.progress.entry_at == runtime_cutoff.progress.entry_at
    assert runtime_mutated.progress.tp1_at is None

    # Replay _simulate_trade has no decision_timestamp. The same full list credits TP1.
    replay_full = _replay(candles, direction="long")
    assert replay_full.outcome == ReplayOutcome.TP1_HIT
    replay_sliced = _replay(candles[:2], direction="long")
    assert replay_sliced.filled is True
    assert replay_sliced.outcome == ReplayOutcome.EXPIRED
    # Comparable as-of fill requires explicitly slicing replay input. Identical lists are not a shared cutoff.


def test_unsafe_authority_terminal_shortcut_is_not_a_comparable_episode(tmp_path: Path) -> None:
    # Mixed: planted terminal record is direct writer; evaluator path is supplied-state.
    record = _latched(current_state=SetupLifecycleState.EXPIRED, lifecycle_id="life-shortcut")
    runtime = _runtime(tmp_path, record, [_no_touch(0, "long"), _both_entry(1)], db_name="shortcut.sqlite")
    assert runtime.progress.terminal_outcome == SetupLifecycleState.EXPIRED.value
    assert runtime.progress.tracking_start_at is None
    assert runtime.progress.entry_at is None
    assert runtime.progress.integrity_status == "Unverified"
    assert runtime.progress.diagnostic == "terminal_state_preceded_canonical_outcome_cursor"

    replay_expired = _replay(
        [_no_touch(0, "long"), _both_entry(1), _no_touch(2, "long")],
        direction="long",
        max_hold_candles=1,
    )
    assert replay_expired.outcome == ReplayOutcome.EXPIRED
    assert replay_expired.filled is True
    # Both can say "expired" while meaning different events. That is not a completed comparable episode.

    payload = project_outcome_ownership(
        lifecycle_records=[runtime.record],
        progress_rows=[runtime.progress],
        provenance={
            "synthetic": True,
            "label": "eval-semantics-shortcut",
            "source_namespace": "synthetic-eval-semantics",
            "coverage_complete": True,
        },
    )
    evaluation = payload["evaluations"][0]
    assert evaluation["evaluation_context"]["complete"] is False
    assert evaluation["evaluation_context"]["start_boundary_provenance"] == "absent"
    contract = evidence_contract_payload()
    assert contract["unique_trade_count"]["status"] == UNAVAILABLE
    assert NOT_COMPARABLE


def test_lifecycle_service_forwards_closed_candle_evaluation(tmp_path: Path) -> None:
    # Mixed chain: planted ACTIONABLE record is a direct writer; apply_to_symbol_result is the
    # ordinary service evaluation producer with synthetic ScannerSymbolResult inputs.
    db_path = tmp_path / "service-forward.sqlite"
    record = _record()
    baseline = _no_touch(0, "long")
    symbol_result = ScannerSymbolResult(
        symbol="BTCUSDT",
        status=ScannerPipelineStatus.IDEA_CREATED,
        status_history=(ScannerPipelineStatus.IDEA_CREATED,),
        strategy_diagnostics={
            "swing": {
                "mode": "swing",
                "bias": "long",
                "invalidation": INVALIDATION,
            }
        },
        valid_strategy_modes=("swing",),
        lifecycle_execution_candles=(baseline,),
        lifecycle_execution_timeframe=TIMEFRAME,
        lifecycle_decision_timestamp=_close(0),
    )
    assert "lifecycle_execution_candles" not in symbol_result.model_dump()
    assert "lifecycle_decision_timestamp" not in symbol_result.model_dump()
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        service = SetupLifecycleService(db_path)
        initialized = service.apply_to_symbol_result(
            symbol_result,
            repository=repository,
            scan_run_id="scan-0",
            now=_decision(0),
        )
        assert initialized.lifecycle_outcome_progress is not None
        assert initialized.lifecycle_outcome_progress.entry_at is None
        entry_result = symbol_result.model_copy(
            update={
                "lifecycle_execution_candles": (baseline, _both_entry(1)),
                "lifecycle_decision_timestamp": _close(1),
            }
        )
        activated = service.apply_to_symbol_result(
            entry_result,
            repository=repository,
            scan_run_id="scan-1",
            now=_decision(1),
        )
    assert activated.lifecycle_outcome_progress.entry_at == _decision(1)
    assert activated.lifecycle_state.current_state == SetupLifecycleState.MANAGING
    assert EVIDENCE_NORMAL_PRODUCER


def test_strategy_replay_engine_entrypoint_simulates_fill_from_detection() -> None:
    # Evidence: normal replay producer with synthetic candles. Not the same opportunity
    # as the runtime paired fixtures above; discovery geometry is strategy-owned.
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("112"),
                "high": Decimal("113"),
                "low": Decimal("97"),
                "close": Decimal("105"),
                "volume": Decimal("100"),
            }
        ]
    )
    trade = summary.symbols[0].trades[0]
    assert summary.stats.total_setups == 1
    assert trade.filled is True
    assert trade.fill_index == 36
    assert trade.time_to_entry == 1
    assert EVIDENCE_NORMAL_PRODUCER


def test_replay_engine_looks_ahead_on_execution_series_after_causal_detection() -> None:
    # Source-grounded engine wiring executed here: detection uses a closed prefix;
    # _simulate_trade then receives the remaining execution series, not a runtime
    # decision_timestamp. This is replay simulation, not exact-as-run possession.
    summary = _run_replay(
        [
            {
                "timestamp": 36,
                "open": Decimal("99"),
                "high": Decimal("100"),
                "low": Decimal("97"),
                "close": Decimal("98"),
                "volume": Decimal("100"),
            },
            {
                "timestamp": 37,
                "open": Decimal("98"),
                "high": Decimal("125"),
                "low": Decimal("98"),
                "close": Decimal("120"),
                "volume": Decimal("100"),
            },
        ]
    )
    trade = summary.symbols[0].trades[0]
    assert trade.candidate.detected_at_index == 35
    assert trade.filled is True
    assert trade.outcome == ReplayOutcome.TP1_HIT
    engine = StrategyReplayEngine()
    assert engine.strategy_engine is not None


def test_ineligible_and_missing_data_do_not_invent_comparable_outcomes(tmp_path: Path) -> None:
    rejected = _record(current_state=SetupLifecycleState.REJECTED, lifecycle_id="life-rejected")
    assert rejected.current_state not in OUTCOME_ELIGIBLE_STATES
    runtime = _runtime(tmp_path, rejected, [_both_entry(0)], db_name="rejected.sqlite")
    assert runtime.progress is None
    empty = _runtime(tmp_path, _record(lifecycle_id="life-empty"), [], db_name="empty.sqlite")
    # Empty list is a different path from execution_candles is None (service skip).
    assert empty.progress is not None
    assert empty.progress.entry_at is None
    assert empty.progress.integrity_status == "Unverified"
    assert empty.progress.diagnostic == "missing_execution_candle_history"
    assert EVIDENCE_MALFORMED


def test_claim_preservation_admission_and_replay_authority_remain_unproven() -> None:
    payload = evidence_contract_payload()
    assert payload["unique_trade_count"]["status"] == UNAVAILABLE
    assert SetupLifecycleState.TRIGGERED not in PLAN_LOCK_STATES
    assert SetupLifecycleState.TRIGGERED in OUTCOME_ELIGIBLE_STATES
    assert SetupLifecycleState.WATCHLISTED in PLAN_LOCK_STATES
    assert SetupLifecycleState.WATCHLISTED in OUTCOME_ELIGIBLE_STATES
    assert ReplayConfig().same_candle_policy == "conservative"
    assert ReplayOutcome.NOT_FILLED is ReplayOutcome.MISSED_ENTRY
    _ = EVIDENCE_SOURCE_ONLY
    _ = EVIDENCE_MALFORMED
