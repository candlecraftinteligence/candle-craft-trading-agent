"""Prospective policy adoption must not depend on whether progress exists."""
from __future__ import annotations

import json

import pytest

from app.lifecycle.breakeven import POLICY
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.research.evaluation_policy import build_runtime_evaluation_policy
from tests.runtime_epoch_support import SYNTHETIC_IDENTITY
from test_f04_durable_source_replay import _operational, _own, _latched, _monitor, _candle, _decision

pytestmark = pytest.mark.no_auto_epoch


def _owner(path, direction):
    levels = {} if direction == "long" else {
        "stop_loss": "112", "tp1": "92", "tp2": "84", "tp3": "76",
    }
    return _own(path, _latched(direction=direction, **levels))


def _target(index, direction, number):
    high, low = (
        {1: ("111", "103"), 2: ("121", "111")}[number] if direction == "long"
        else {1: ("99", "91"), 2: ("91", "83")}[number]
    )
    return _candle(index, high=high, low=low)


def _load(path, lifecycle_id):
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repo:
        return repo.list_outcome_progress(lifecycle_id=lifecycle_id)[0]


@pytest.mark.parametrize("direction", ["long", "short"])
@pytest.mark.parametrize("targets", [1, 2])
def test_owned_plan_without_progress_keeps_pre_adoption_stop_loss(tmp_path, direction, targets):
    path = _operational(tmp_path)
    record = _owner(path, direction)
    # Ownership is committed before the first evaluator invocation, as at restart.
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repo:
        assert repo.list_outcome_progress(lifecycle_id=record.lifecycle_id) == ()
    candles = [_candle(0, high="103", low="99")]
    candles.extend(_target(number, direction, number) for number in range(1, targets + 1))
    stop_index = len(candles)
    candles.append(_candle(stop_index, high="105" if direction == "long" else "113",
                           low="89" if direction == "long" else "99"))
    adopted_at = _decision(stop_index + 1)
    _monitor(path, record, candles, when=adopted_at)
    progress = _load(path, record.lifecycle_id)
    binding = json.loads(progress.metadata_json)["stop_management"]
    assert binding["policy"] == POLICY
    assert binding["adopted_at"] == binding["effective_from_candle_open_at"] == adopted_at
    assert binding["prior_policy"] == "fixed_original_stop"
    assert progress.terminal_outcome == "SL_HIT"
    assert progress.tp1_at == _decision(1)
    assert (progress.tp2_at is not None) == (targets == 2)
    assert progress.outcome_at == _decision(stop_index)
    assert progress.plan_version_id == record.plan_version_id
    _monitor(path, record, candles, when=_decision(stop_index + 2))
    assert _load(path, record.lifecycle_id).model_dump() == progress.model_dump()


@pytest.mark.parametrize("direction", ["long", "short"])
def test_unbound_owner_ignores_old_midpoint_touch_then_protects_after_restart(tmp_path, direction):
    path = _operational(tmp_path)
    record = _owner(path, direction)
    candles = [
        _candle(0, high="103", low="99"),
        _target(1, direction, 1),
        _candle(2, high="105", low="99"),  # midpoint touch before adoption, not original SL
    ]
    _monitor(path, record, candles, when=_decision(2))
    adopted = _load(path, record.lifecycle_id)
    binding = json.loads(adopted.metadata_json)["stop_management"]
    assert binding["effective_from_candle_open_at"] == _decision(2)
    assert adopted.terminal_outcome == "N/A"
    assert adopted.tp1_at == _decision(1)
    assert adopted.stop_at is None
    # Reopening with the same evidence must retain the original adoption boundary.
    _monitor(path, record, candles, when=_decision(2))
    assert json.loads(_load(path, record.lifecycle_id).metadata_json)["stop_management"] == binding
    candles.append(_candle(3, high="105", low="99"))
    _monitor(path, record, candles, when=_decision(3))
    stopped = _load(path, record.lifecycle_id)
    assert stopped.terminal_outcome == "BREAKEVEN"
    assert stopped.stop_at == _decision(3)
    assert stopped.tp1_at == adopted.tp1_at
    assert json.loads(stopped.metadata_json)["stop_management"] == binding
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repo:
        terminal_events = [e for e in repo.list_events(lifecycle_id=record.lifecycle_id)
                           if e.to_state == SetupLifecycleState.BREAKEVEN]
        assert len(terminal_events) == 1


@pytest.mark.parametrize("direction", ["long", "short"])
def test_newly_tracked_owner_still_protects_after_tp1(tmp_path, direction):
    path = _operational(tmp_path)
    record = _owner(path, direction)
    candles = [_candle(0, high="103", low="99")]
    _monitor(path, record, candles, when=_decision(0))
    entered = _load(path, record.lifecycle_id)
    assert entered.entry_at == _decision(0)
    assert json.loads(entered.metadata_json)["stop_management"]["effective_from_candle_open_at"] == _decision(0)
    candles.extend([_target(1, direction, 1), _candle(2, high="105", low="99")])
    _monitor(path, record, candles, when=_decision(2))
    stopped = _load(path, record.lifecycle_id)
    assert stopped.terminal_outcome == "BREAKEVEN"
    assert stopped.tp1_at == _decision(1)
    assert stopped.stop_at == _decision(2)


@pytest.mark.parametrize("damage", ["null_cutoff", "rejected_v1_policy"])
def test_missing_or_rejected_policy_boundary_fails_closed(tmp_path, damage):
    path = _operational(tmp_path)
    record = _owner(path, "long")
    candles = [_candle(0, high="103", low="99")]
    _monitor(path, record, candles, when=_decision(0))
    original = _load(path, record.lifecycle_id)
    metadata = json.loads(original.metadata_json)
    if damage == "null_cutoff":
        metadata["stop_management"]["effective_from_candle_open_at"] = None
    else:
        metadata["stop_management"]["policy"] = "tp1_entry_zone_midpoint_breakeven_v1"
    with SQLiteSetupLifecycleRepository(path, expected_identity=SYNTHETIC_IDENTITY) as repo:
        repo.upsert_outcome_progress(original.model_copy(update={"metadata_json": json.dumps(metadata)}))
    candles.append(_target(1, "long", 1))
    _monitor(path, record, candles, when=_decision(1))
    rejected = _load(path, record.lifecycle_id)
    assert rejected.integrity_status == "Failed"
    assert rejected.evaluation_cursor_open_at == original.evaluation_cursor_open_at
    assert rejected.tp1_at is None
    assert rejected.terminal_outcome == "N/A"


def test_manifest_discloses_first_evaluation_cutoff_for_missing_progress():
    rule = build_runtime_evaluation_policy(execution_timeframe="5m").to_canonical_dict()["rules"]["stop_management"]
    assert rule["policy"] == POLICY
    assert "missing_progress_is_not_new_policy_provenance" in rule["new_progress"]
    assert rule["pre_adoption_candles"] == "fixed_original_stop"
