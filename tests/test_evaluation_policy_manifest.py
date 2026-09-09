"""EVALUATION_POLICY_MANIFEST: immutable evaluator-policy contract proofs.

Synthetic fixtures only. No live or existing scan database is read.
The contract is unused by production callers in this phase.
"""

from __future__ import annotations

import inspect
import json
import subprocess
import sys
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import MappingProxyType

import pytest

from app.analytics.evidence_contract import UNAVAILABLE, evidence_contract_payload
from app.analytics.outcome_ownership import project_outcome_ownership
from app.backtesting.strategy_replay import (
    LiquidityGrabMode,
    ReplayConfig,
    ReplayOutcome,
    ReplaySetupCandidate,
    _evaluate_exit_candle,
    _fill_window,
    _max_hold_candles,
    _normalize_candles,
    _price_touched,
    _simulate_trade,
)
from app.data.dtos import NA
from app.lifecycle.models import SetupLifecycleState
from app.lifecycle.outcome_policy import entry_touched, newly_touched_targets, stop_touched, stored_plan_geometry
from app.lifecycle.outcomes import evaluate_closed_candle_outcomes
from app.lifecycle.repositories import SQLiteSetupLifecycleRepository
from app.lifecycle.service import SetupLifecycleService
from app.pipeline.scanner_runner import ScannerPipelineStatus, ScannerSymbolResult
from app.research.evaluation_policy import (
    EVALUATOR_FAMILY_REPLAY,
    EVALUATOR_FAMILY_RUNTIME,
    EvaluationPolicyError,
    MANIFEST_FORMAT_VERSION,
    POLICY_ID_PREFIX,
    REPLAY_CALL_BOUNDARY,
    RUNTIME_CALL_BOUNDARY,
    UNUSED_REPLAY_HELPER,
    build_replay_evaluation_policy,
    build_runtime_evaluation_policy,
    parse_evaluation_policy_payload,
    resolve_replay_effective_parameters,
)
from app.storage.database import SCHEMA_VERSION, open_initialized_database
from tests.test_evaluation_semantics_contract import (
    EVIDENCE_MALFORMED,
    EVIDENCE_NORMAL_PRODUCER,
    EVIDENCE_SOURCE_ONLY,
    EVIDENCE_SUPPLIED_STATE,
    INVALIDATION,
    NON_EQUIVALENT,
    TIMEFRAME,
    _both_entry,
    _candidate,
    _candle,
    _decision,
    _no_touch,
    _record,
    _replay,
    _runtime,
    _stop,
    _target,
    _zone_not_point,
)
from tests.test_strategy_replay import _run_replay

EVIDENCE_MIXED = "mixed planted-record plus ordinary service evaluation"

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable


def _runtime_policy(*, timeframe: str = TIMEFRAME):
    return build_runtime_evaluation_policy(execution_timeframe=timeframe)


def _replay_policy(
    *,
    mode: LiquidityGrabMode | str = LiquidityGrabMode.swing,
    **config_fields,
):
    return build_replay_evaluation_policy(config=ReplayConfig(**config_fields), mode=mode)


def test_schema_remains_v25_without_policy_admission_or_source_binding(tmp_path: Path) -> None:
    path = tmp_path / "eval-policy-schema.sqlite"
    with open_initialized_database(path) as connection:
        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        sqlite_schema_version = connection.execute("PRAGMA schema_version").fetchone()[0]
        progress_cols = {
            row[1] for row in connection.execute("PRAGMA table_info(setup_lifecycle_outcome_progress)")
        }
        analytics_cols = {
            row[1] for row in connection.execute("PRAGMA table_info(setup_outcome_analytics)")
        }
        replay_cols = {row[1] for row in connection.execute("PRAGMA table_info(replay_results)")}
        table_names = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert user_version == SCHEMA_VERSION == 25
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
        assert forbidden not in replay_cols
    assert "admitted_evaluation_episodes" not in table_names


def test_canonical_bytes_and_ids_are_deterministic_and_deeply_immutable() -> None:
    first = _runtime_policy()
    second = _runtime_policy()
    reversed_keys = parse_evaluation_policy_payload(
        {
            "effective_parameters": {"execution_timeframe": "5m"},
            "rules": deepcopy(first.to_canonical_dict()["rules"]),
            "rule_vocabulary_version": first.to_canonical_dict()["rule_vocabulary_version"],
            "scope": deepcopy(first.to_canonical_dict()["scope"]),
            "evaluator_family": EVALUATOR_FAMILY_RUNTIME,
            "manifest_format_version": MANIFEST_FORMAT_VERSION,
        }
    )
    assert first.policy_id == second.policy_id
    assert first.canonical_bytes == second.canonical_bytes == reversed_keys.canonical_bytes
    assert first.policy_id.startswith(POLICY_ID_PREFIX)
    assert len(first.policy_id) == len(POLICY_ID_PREFIX) + 64
    assert first.canonical_bytes.decode("utf-8") == json.dumps(
        first.to_canonical_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    exported = first.to_canonical_dict()
    exported["effective_parameters"]["execution_timeframe"] = "1h"
    exported["rules"]["entry_representation"]["model"] = "mutated"
    assert first.policy_id == _runtime_policy().policy_id
    assert isinstance(first.payload, MappingProxyType)
    with pytest.raises(TypeError):
        first.payload["evaluator_family"] = "mutated"  # type: ignore[index]
    with pytest.raises(TypeError):
        first.payload["effective_parameters"]["execution_timeframe"] = "1h"  # type: ignore[index]


def test_independent_process_construction_matches_local_id() -> None:
    script = (
        "from app.research.evaluation_policy import build_runtime_evaluation_policy, "
        "build_replay_evaluation_policy; "
        "from app.backtesting.strategy_replay import ReplayConfig, LiquidityGrabMode; "
        "runtime = build_runtime_evaluation_policy(execution_timeframe='5m'); "
        "replay = build_replay_evaluation_policy("
        "config=ReplayConfig(execution_timeframe='5m'), mode=LiquidityGrabMode.swing); "
        "print(runtime.policy_id); print(replay.policy_id)"
    )
    completed = subprocess.run(
        [PYTHON, "-c", script],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    runtime_id, replay_id = completed.stdout.strip().splitlines()
    assert runtime_id == _runtime_policy().policy_id
    assert replay_id == _replay_policy(execution_timeframe="5m").policy_id


def test_runtime_and_replay_families_and_replay_policies_have_distinct_identities() -> None:
    runtime = _runtime_policy(timeframe="5m")
    replay_conservative = _replay_policy(
        execution_timeframe="5m",
        same_candle_policy="conservative",
        max_fill_candles=20,
        max_hold_candles=20,
    )
    replay_optimistic = _replay_policy(
        execution_timeframe="5m",
        same_candle_policy="optimistic",
        max_fill_candles=20,
        max_hold_candles=20,
    )
    replay_hour = _replay_policy(
        execution_timeframe="15m",
        same_candle_policy="conservative",
        max_fill_candles=20,
        max_hold_candles=20,
    )
    assert runtime.family == EVALUATOR_FAMILY_RUNTIME
    assert replay_conservative.family == EVALUATOR_FAMILY_REPLAY
    assert runtime.policy_id != replay_conservative.policy_id
    assert replay_conservative.policy_id != replay_optimistic.policy_id
    assert replay_conservative.policy_id != replay_hour.policy_id
    assert runtime.to_canonical_dict()["scope"]["call_boundary"] == RUNTIME_CALL_BOUNDARY
    assert replay_conservative.to_canonical_dict()["scope"]["call_boundary"] == REPLAY_CALL_BOUNDARY


def test_equivalent_explicit_and_default_limits_share_identity_unrelated_fields_do_not() -> None:
    default_five = _replay_policy(mode=LiquidityGrabMode.challenge, confirmation_timeframe="5m")
    explicit_matching = _replay_policy(
        mode=LiquidityGrabMode.challenge,
        confirmation_timeframe="15m",
        max_fill_candles=12,
        max_hold_candles=48,
        max_setups=3,
        replay_candles=99,
        aggressive_toggle=True,
        edge_min_sample=40,
        htf_timeframe="1d",
        bias_timeframe="4h",
        structure_timeframe="1h",
        modes=(LiquidityGrabMode.challenge, LiquidityGrabMode.swing),
    )
    default_fifteen = _replay_policy(mode=LiquidityGrabMode.challenge, confirmation_timeframe="15m")
    assert default_five.policy_id == explicit_matching.policy_id
    assert default_five.policy_id != default_fifteen.policy_id
    params = default_five.to_canonical_dict()["effective_parameters"]
    assert params["fill_window_candles"] == 12
    assert params["max_hold_candles"] == 48
    assert "confirmation_timeframe" not in params
    assert "max_setups" not in params
    swing_default = _replay_policy(mode=LiquidityGrabMode.swing)
    swing_unrelated = _replay_policy(
        mode=LiquidityGrabMode.swing,
        max_setups=1,
        replay_candles=50,
        aggressive_toggle=True,
    )
    assert swing_default.policy_id == swing_unrelated.policy_id
    assert swing_default.to_canonical_dict()["effective_parameters"]["fill_window_candles"] == 80
    assert swing_default.to_canonical_dict()["effective_parameters"]["max_hold_candles"] == 80


def test_effective_replay_defaults_match_active_helpers() -> None:
    config = ReplayConfig()
    for mode in (LiquidityGrabMode.challenge, LiquidityGrabMode.scalp, LiquidityGrabMode.swing):
        candidate = _candidate(direction="long").model_copy(update={"mode": mode})
        resolved = resolve_replay_effective_parameters(config=config, mode=mode)
        assert resolved["fill_window_candles"] == _fill_window(candidate, config)
        assert resolved["max_hold_candles"] == _max_hold_candles(
            mode, config.execution_timeframe, config
        )
    five = ReplayConfig(confirmation_timeframe="5m")
    fifteen = ReplayConfig(confirmation_timeframe="15m")
    assert _fill_window(_candidate(direction="long").model_copy(update={"mode": LiquidityGrabMode.scalp}), five) == 12
    assert _fill_window(_candidate(direction="long").model_copy(update={"mode": LiquidityGrabMode.scalp}), fifteen) == 6
    explicit = ReplayConfig(max_fill_candles=9, max_hold_candles=11)
    for mode in (LiquidityGrabMode.challenge, LiquidityGrabMode.scalp, LiquidityGrabMode.swing):
        params = resolve_replay_effective_parameters(config=explicit, mode=mode)
        assert params["fill_window_candles"] == 9
        assert params["max_hold_candles"] == 11
    for timeframe in ("5m", "15m", "1h", "4h"):
        hold_cfg = ReplayConfig(execution_timeframe=timeframe)
        assert _max_hold_candles(LiquidityGrabMode.swing, timeframe, hold_cfg) == 80
        assert _max_hold_candles(LiquidityGrabMode.challenge, timeframe, hold_cfg) == 48


def test_malformed_contract_input_fails_without_policy_id() -> None:
    valid = _runtime_policy().to_canonical_dict()
    with pytest.raises(EvaluationPolicyError, match="unknown_field"):
        parse_evaluation_policy_payload({**valid, "git_sha": "abc"})
    with pytest.raises(EvaluationPolicyError, match="unknown_value:manifest_format_version"):
        parse_evaluation_policy_payload({**valid, "manifest_format_version": "v0"})
    mutated_rules = deepcopy(valid)
    mutated_rules["rules"]["entry_representation"]["model"] = "invented_zone"
    with pytest.raises(EvaluationPolicyError, match="rules_must_match_declared_vocabulary"):
        parse_evaluation_policy_payload(mutated_rules)
    missing = deepcopy(valid)
    del missing["effective_parameters"]
    with pytest.raises(EvaluationPolicyError, match="missing_field:effective_parameters"):
        parse_evaluation_policy_payload(missing)
    with pytest.raises(EvaluationPolicyError, match="unsupported_execution_timeframe"):
        build_runtime_evaluation_policy(execution_timeframe="1s")
    with pytest.raises(EvaluationPolicyError, match="unsupported_replay_mode"):
        build_replay_evaluation_policy(config=ReplayConfig(), mode="unknown-mode")
    with pytest.raises(EvaluationPolicyError, match="replay_config_must_be_replayconfig_instance"):
        build_replay_evaluation_policy(config={"execution_timeframe": "5m"}, mode="swing")  # type: ignore[arg-type]
    placeholder = deepcopy(valid)
    placeholder["effective_parameters"]["execution_timeframe"] = "unknown"
    with pytest.raises(EvaluationPolicyError, match="unresolved_placeholder"):
        parse_evaluation_policy_payload(placeholder)
    replay_valid = _replay_policy(max_fill_candles=8, max_hold_candles=8).to_canonical_dict()
    replay_valid["effective_parameters"]["fill_window_candles"] = True
    with pytest.raises(EvaluationPolicyError, match="invalid_int"):
        parse_evaluation_policy_payload(replay_valid)
    replay_float = _replay_policy(max_fill_candles=8, max_hold_candles=8).to_canonical_dict()
    replay_float["effective_parameters"]["fill_window_candles"] = float("nan")
    with pytest.raises(EvaluationPolicyError):
        parse_evaluation_policy_payload(replay_float)
    with pytest.raises(ValueError):
        ReplayConfig(max_fill_candles=0)


def test_plan_ids_prices_sha_time_and_db_filename_are_not_identity_inputs() -> None:
    signature_runtime = inspect.signature(build_runtime_evaluation_policy)
    signature_replay = inspect.signature(build_replay_evaluation_policy)
    forbidden = {
        "git_sha",
        "plan_version_id",
        "lifecycle_id",
        "scan_run_id",
        "database_path",
        "inspected_at",
        "entry_low",
        "stop_loss",
        "decision_timestamp",
    }
    assert forbidden.isdisjoint(signature_runtime.parameters)
    assert forbidden.isdisjoint(signature_replay.parameters)
    first = _runtime_policy()
    second = _runtime_policy()
    assert first.policy_id == second.policy_id
    payload = first.to_canonical_dict()
    encoded = json.dumps(payload)
    for token in (
        "plan-version-",
        "afbc4ae",
        "main_live_runtime.sqlite",
        "BTCUSDT",
        "101",
    ):
        assert token not in encoded


@pytest.mark.parametrize("direction", ["long", "short"])
def test_existing_paired_long_short_cases_remain_family_separated(
    tmp_path: Path,
    direction: str,
) -> None:
    assert EVIDENCE_SUPPLIED_STATE
    ladder = [
        _no_touch(0, direction),
        _both_entry(1),
        _target(2, direction, 1),
        _target(3, direction, 2),
        _target(4, direction, 3),
    ]
    runtime_ladder = _runtime(tmp_path, _record(direction=direction), ladder, db_name=f"policy-ladder-{direction}.sqlite")
    replay_ladder = _replay(ladder, direction=direction)
    assert runtime_ladder.progress.terminal_outcome == SetupLifecycleState.TP_HIT.value
    assert replay_ladder.outcome == ReplayOutcome.TP3_HIT
    assert _runtime_policy().policy_id != _replay_policy(execution_timeframe=TIMEFRAME).policy_id

    zone = [_no_touch(0, "long"), _zone_not_point(1)]
    geometry = stored_plan_geometry(_record())
    assert entry_touched(Decimal("100.5"), Decimal("99.5"), geometry) is True
    assert not (Decimal("99.5") <= Decimal("101") <= Decimal("100.5"))
    runtime_zone = _runtime(tmp_path, _record(lifecycle_id="zone"), zone, db_name="policy-zone.sqlite")
    replay_zone = _replay(zone, direction="long", max_fill_candles=2)
    assert runtime_zone.progress.entry_at == _decision(1)
    assert replay_zone.filled is False
    assert NON_EQUIVALENT


def test_entry_candle_targets_same_candle_and_horizon_cases(tmp_path: Path) -> None:
    assert EVIDENCE_SUPPLIED_STATE
    entry_target = [
        _no_touch(0, "long"),
        _candle(1, high="111", low="99"),
        _stop(2, "long"),
    ]
    runtime_entry = _runtime(tmp_path, _record(), entry_target, db_name="policy-entry-target.sqlite")
    replay_entry = _replay(entry_target, direction="long")
    assert runtime_entry.progress.entry_at == _decision(1)
    assert runtime_entry.progress.tp1_at is None
    assert runtime_entry.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert replay_entry.outcome == ReplayOutcome.TP1_HIT

    entry_stop = [_no_touch(0, "long"), _candle(1, high="103", low="89")]
    runtime_es = _runtime(tmp_path, _record(lifecycle_id="es"), entry_stop, db_name="policy-es.sqlite")
    replay_es = _replay(entry_stop, direction="long")
    assert runtime_es.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert replay_es.outcome == ReplayOutcome.STOPPED

    post = [_no_touch(0, "long"), _both_entry(1), _candle(2, high="111", low="89")]
    runtime_post = _runtime(tmp_path, _record(lifecycle_id="post"), post, db_name="policy-post.sqlite")
    replay_cons = _replay(post, direction="long", same_candle_policy="conservative")
    replay_opt = _replay(post, direction="long", same_candle_policy="optimistic")
    assert runtime_post.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert runtime_post.progress.tp1_at is None
    assert replay_cons.outcome == ReplayOutcome.STOPPED
    assert replay_opt.outcome == ReplayOutcome.TP1_HIT

    tp_then_stop = [_no_touch(0, "long"), _both_entry(1), _target(2, "long", 1), _stop(3, "long")]
    runtime_ms = _runtime(tmp_path, _record(lifecycle_id="ms"), tp_then_stop, db_name="policy-ms.sqlite")
    replay_ms = _replay(tp_then_stop, direction="long")
    assert runtime_ms.progress.tp1_at == _decision(2)
    assert runtime_ms.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert replay_ms.outcome == ReplayOutcome.TP1_HIT
    assert replay_ms.sl_hit is False
    assert replay_ms.r_multiple > 0

    delayed = [_no_touch(0, "long"), _no_touch(1, "long"), _both_entry(2)]
    runtime_delay = _runtime(tmp_path, _record(lifecycle_id="delay"), delayed, db_name="policy-delay.sqlite")
    replay_miss = _replay(delayed, direction="long", max_fill_candles=1)
    assert runtime_delay.progress.entry_at == _decision(2)
    assert replay_miss.outcome == ReplayOutcome.NOT_FILLED

    hold = [_no_touch(0, "long"), _both_entry(1), _no_touch(2, "long"), _target(3, "long", 1)]
    runtime_hold = _runtime(tmp_path, _record(lifecycle_id="hold"), hold, db_name="policy-hold.sqlite")
    replay_hold = _replay(hold, direction="long", max_hold_candles=1)
    assert runtime_hold.progress.terminal_outcome == NA
    assert runtime_hold.progress.entry_at == _decision(1)
    assert replay_hold.outcome == ReplayOutcome.EXPIRED
    assert replay_hold.filled is True


def test_optional_tp3_and_input_exhaustion(tmp_path: Path) -> None:
    assert EVIDENCE_SUPPLIED_STATE
    candidate = ReplaySetupCandidate(
        symbol="BTCUSDT",
        mode=LiquidityGrabMode.swing,
        direction=_candidate(direction="long").direction,
        detected_at_index=0,
        detected_at_timestamp=_candidate(direction="long").detected_at_timestamp,
        entry=Decimal("101"),
        entry_low=Decimal("100"),
        entry_high=Decimal("102"),
        stop=Decimal("90"),
        tp1=Decimal("110"),
        tp2=Decimal("120"),
        tp3=NA,
        invalidation=INVALIDATION,
    )
    candles = [
        _no_touch(0, "long"),
        _both_entry(1),
        _target(2, "long", 1),
        _target(3, "long", 2),
    ]
    replay = _simulate_trade(
        candidate,
        _normalize_candles(candles, timeframe=TIMEFRAME),
        ReplayConfig(execution_timeframe=TIMEFRAME, max_fill_candles=20, max_hold_candles=20),
    )
    assert replay.outcome == ReplayOutcome.TP2_HIT
    runtime = _runtime(tmp_path, _record(lifecycle_id="tp3req"), candles, db_name="policy-tp3req.sqlite")
    assert runtime.progress.tp2_at == _decision(3)
    assert runtime.progress.tp3_at is None
    assert runtime.progress.terminal_outcome == NA

    exhausted = [_no_touch(0, "long"), _both_entry(1)]
    runtime_ex = _runtime(tmp_path, _record(lifecycle_id="exh"), exhausted, db_name="policy-exh.sqlite")
    replay_ex = _replay(exhausted, direction="long", max_hold_candles=1)
    assert runtime_ex.progress.terminal_outcome == NA
    assert replay_ex.outcome == ReplayOutcome.EXPIRED


def test_price_jumps_beyond_stop_and_target_through_full_evaluators(tmp_path: Path) -> None:
    # Direct helper evidence is labeled separately; these paths are full evaluators
    # with temporally continuous 5m bars. A price gap is not a coverage gap.
    assert EVIDENCE_SUPPLIED_STATE
    geometry = stored_plan_geometry(_record())
    jump_stop_candle = _candle(2, high="89", low="85")
    assert stop_touched(Decimal("89"), Decimal("85"), geometry) is True
    assert _price_touched(_normalize_candles([jump_stop_candle], timeframe=TIMEFRAME)[0], Decimal("90")) is False

    jump_stop = [
        _no_touch(0, "long"),
        _both_entry(1),
        jump_stop_candle,
        _target(3, "long", 1),
    ]
    runtime_stop = _runtime(tmp_path, _record(), jump_stop, db_name="policy-jump-stop.sqlite")
    replay_stop = _replay(jump_stop, direction="long")
    assert runtime_stop.progress.entry_at == _decision(1)
    assert runtime_stop.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert runtime_stop.progress.tp1_at is None
    assert replay_stop.filled is True
    assert replay_stop.outcome == ReplayOutcome.TP1_HIT
    assert replay_stop.sl_hit is False

    jump_target_candle = _candle(2, high="112", low="111")
    progress_stub = runtime_stop.progress.model_copy(
        update={"entry_at": _decision(1), "tp1_at": None, "tp2_at": None, "tp3_at": None, "stop_at": None}
    )
    assert newly_touched_targets(Decimal("112"), Decimal("111"), geometry, progress_stub)[0][0] == 1
    assert _price_touched(_normalize_candles([jump_target_candle], timeframe=TIMEFRAME)[0], Decimal("110")) is False

    jump_target = [
        _no_touch(0, "long"),
        _both_entry(1),
        jump_target_candle,
        _candle(3, high="89", low="85"),
    ]
    runtime_tp = _runtime(tmp_path, _record(lifecycle_id="jump-tp"), jump_target, db_name="policy-jump-tp.sqlite")
    replay_tp = _replay(jump_target, direction="long")
    assert runtime_tp.progress.tp1_at == _decision(2)
    assert runtime_tp.progress.terminal_outcome == SetupLifecycleState.SL_HIT.value
    assert replay_tp.outcome == ReplayOutcome.EXPIRED
    assert replay_tp.highest_tp_hit == 0
    runtime_rules = _runtime_policy().to_canonical_dict()["rules"]["price_touch_predicates"]
    replay_rules = _replay_policy().to_canonical_dict()["rules"]["price_touch_predicates"]
    assert runtime_rules["stop"]["requires_level_inside_inclusive_range"] is False
    assert replay_rules["stop"]["requires_level_inside_inclusive_range"] is True


def test_pre_entry_stop_invalidates_replay_not_runtime(tmp_path: Path) -> None:
    assert EVIDENCE_SUPPLIED_STATE
    candles = [
        _no_touch(0, "long"),
        _candle(1, high="95", low="89"),
        _both_entry(2),
    ]
    runtime = _runtime(tmp_path, _record(), candles, db_name="policy-pre-entry.sqlite")
    replay = _replay(candles, direction="long")
    assert runtime.progress.entry_at == _decision(2)
    assert runtime.progress.terminal_outcome == NA
    assert runtime.progress.stop_at is None
    assert runtime.record.current_state == SetupLifecycleState.MANAGING
    assert replay.outcome == ReplayOutcome.INVALIDATED
    assert replay.filled is False
    assert "before the limit entry filled" in replay.failure_reason
    assert _runtime_policy().to_canonical_dict()["rules"]["pre_entry_invalidation"]["stop_without_entry"] == (
        "does_not_invalidate_advances_cursor"
    )
    assert _replay_policy().to_canonical_dict()["rules"]["pre_entry_invalidation"]["stop_without_entry"] == (
        "invalidated"
    )


def test_exact_open_cutoff_mutation_and_malformed_future_sequence(tmp_path: Path) -> None:
    assert EVIDENCE_SUPPLIED_STATE
    exact = [_both_entry(0), _target(1, "long", 1)]
    runtime_exact = _runtime(
        tmp_path,
        _record(confirmed_at=datetime(2026, 1, 1, tzinfo=UTC).isoformat()),
        exact,
        db_name="policy-exact.sqlite",
    )
    replay_exact = _replay(exact, direction="long", max_fill_candles=1)
    assert runtime_exact.progress.entry_at == _decision(0)
    assert replay_exact.filled is False

    cutoff = [_no_touch(0, "long"), _both_entry(1), _target(2, "long", 1)]
    runtime_cut = _runtime(
        tmp_path,
        _record(lifecycle_id="cut"),
        cutoff,
        decision_timestamp=_decision(1),
        evaluated_at=_decision(4),
        db_name="policy-cut.sqlite",
    )
    assert runtime_cut.progress.entry_at == _decision(1)
    assert runtime_cut.progress.tp1_at is None
    mutated = deepcopy(cutoff)
    mutated[2] = _candle(2, high="200", low="110")
    runtime_mut = _runtime(
        tmp_path,
        _record(lifecycle_id="mut"),
        mutated,
        decision_timestamp=_decision(1),
        evaluated_at=_decision(4),
        db_name="policy-mut.sqlite",
    )
    assert runtime_mut.progress.entry_at == runtime_cut.progress.entry_at
    assert runtime_mut.progress.tp1_at is None

    gapped = [_no_touch(0, "long"), _both_entry(1), _candle(4, high="111", low="103")]
    runtime_gap = _runtime(
        tmp_path,
        _record(lifecycle_id="gap"),
        gapped,
        decision_timestamp=_decision(1),
        db_name="policy-gap.sqlite",
    )
    assert runtime_gap.progress.integrity_status == "Unverified"
    assert "continuity_gap" in runtime_gap.progress.diagnostic
    assert EVIDENCE_MALFORMED


def test_terminal_shortcut_missing_input_and_not_run_do_not_become_admission(
    tmp_path: Path,
) -> None:
    planted = _record(current_state=SetupLifecycleState.EXPIRED)
    runtime = _runtime(tmp_path, planted, [_both_entry(0)], db_name="policy-term.sqlite")
    assert runtime.progress.terminal_outcome == SetupLifecycleState.EXPIRED.value
    assert runtime.progress.entry_at is None
    assert runtime.progress.integrity_status == "Unverified"
    empty = _runtime(tmp_path, _record(lifecycle_id="empty"), [], db_name="policy-empty.sqlite")
    assert empty.progress.integrity_status == "Unverified"
    assert empty.progress.diagnostic == "missing_execution_candle_history"
    payload = project_outcome_ownership(
        lifecycle_records=[runtime.record],
        progress_rows=[runtime.progress],
        provenance={
            "synthetic": True,
            "label": "eval-policy-shortcut",
            "source_namespace": "synthetic-eval-policy",
            "coverage_complete": True,
        },
    )
    assert payload["evaluations"][0]["evaluation_context"]["complete"] is False
    contract = evidence_contract_payload()
    assert contract["unique_trade_count"]["status"] == UNAVAILABLE

    rejected = _runtime(
        tmp_path,
        _record(current_state=SetupLifecycleState.REJECTED, lifecycle_id="life-rejected"),
        [_both_entry(0)],
        db_name="policy-rejected.sqlite",
    )
    assert rejected.progress is None
    service_source = inspect.getsource(SetupLifecycleService)
    assert 'if status == "not_run"' in service_source
    scope = _runtime_policy().to_canonical_dict()["scope"]
    assert "admission_selection" in scope["excludes"]
    assert "admission_selection" not in scope["includes"]
    assert runtime.progress.plan_identity.startswith("plan-")
    assert "evaluation_policy_id" not in runtime.progress.model_dump()


def test_lifecycle_service_and_replay_engine_entrypoints_remain_reachable(tmp_path: Path) -> None:
    db_path = tmp_path / "policy-service.sqlite"
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
        lifecycle_decision_timestamp=datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=5),
    )
    dumped = symbol_result.model_dump()
    assert "lifecycle_execution_candles" not in dumped
    assert "lifecycle_decision_timestamp" not in dumped
    assert "lifecycle_execution_timeframe" not in dumped
    with SQLiteSetupLifecycleRepository(db_path) as repository:
        repository.upsert_record(record)
        service = SetupLifecycleService(db_path)
        initialized = service.apply_to_symbol_result(
            symbol_result,
            repository=repository,
            scan_run_id="scan-0",
            now=_decision(0),
        )
        entry_result = symbol_result.model_copy(
            update={
                "lifecycle_execution_candles": (baseline, _both_entry(1)),
                "lifecycle_decision_timestamp": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=10),
            }
        )
        activated = service.apply_to_symbol_result(
            entry_result,
            repository=repository,
            scan_run_id="scan-1",
            now=_decision(1),
        )
    assert initialized.lifecycle_outcome_progress is not None
    assert activated.lifecycle_outcome_progress.entry_at == _decision(1)
    assert EVIDENCE_MIXED

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
    assert trade.filled is True
    assert trade.fill_index == 36
    assert EVIDENCE_NORMAL_PRODUCER


def test_policy_identity_does_not_bind_source_or_progress_history(tmp_path: Path) -> None:
    runtime_a = _runtime_policy(timeframe="5m")
    runtime_b = _runtime_policy(timeframe="5m")
    assert runtime_a.policy_id == runtime_b.policy_id
    first = _runtime(tmp_path, _record(lifecycle_id="bind-a"), [_no_touch(0, "long"), _both_entry(1)])
    second = _runtime(
        tmp_path,
        _record(lifecycle_id="bind-b", symbol="ETHUSDT"),
        [_no_touch(0, "long"), _both_entry(1)],
        db_name="bind-b.sqlite",
    )
    assert first.progress.plan_identity != second.progress.plan_identity
    assert "evaluation_policy_id" not in first.progress.model_dump()
    assert runtime_a.to_canonical_dict()["scope"]["identity_describes"] == "current_evaluator_invocation_contract"
    reconstructed = evaluate_closed_candle_outcomes
    assert reconstructed is evaluate_closed_candle_outcomes
    assert EVIDENCE_SOURCE_ONLY
    assert UNUSED_REPLAY_HELPER.endswith("_evaluate_exit_candle")
    module_source = Path("app/backtesting/strategy_replay.py").read_text(encoding="utf-8")
    assert module_source.count("_evaluate_exit_candle(") == 1
    policy_source = Path("app/research/evaluation_policy.py").read_text(encoding="utf-8")
    assert "_evaluate_exit_candle(" not in policy_source
    assert inspect.isfunction(_evaluate_exit_candle)


def test_hold_loop_includes_fill_through_fill_plus_hold() -> None:
    candles = [
        _no_touch(0, "long"),
        _both_entry(1),
        _no_touch(2, "long"),
        _target(3, "long", 1),
    ]
    miss = _replay(candles, direction="long", max_hold_candles=1)
    hit = _replay(
        [_no_touch(0, "long"), _both_entry(1), _target(2, "long", 1), _no_touch(3, "long")],
        direction="long",
        max_hold_candles=1,
    )
    assert miss.outcome == ReplayOutcome.EXPIRED
    assert miss.candles_held == 1
    assert hit.outcome == ReplayOutcome.TP1_HIT
    rule = _replay_policy().to_canonical_dict()["rules"]["horizons"]["hold_loop"]
    assert rule == "inclusive_fill_index_through_fill_index_plus_max_hold_candles"


def test_production_modules_do_not_consume_the_manifest() -> None:
    hits: list[str] = []
    for folder in (REPO_ROOT / "app", REPO_ROOT / "scripts"):
        for path in folder.rglob("*.py"):
            if path.name == "evaluation_policy.py":
                continue
            text = path.read_text(encoding="utf-8")
            if "research.evaluation_policy" in text or "from app.research import evaluation_policy" in text:
                hits.append(str(path.relative_to(REPO_ROOT)))
    assert hits == []
    init_text = (REPO_ROOT / "app/research/__init__.py").read_text(encoding="utf-8")
    assert "evaluation_policy" not in init_text
    assert "build_runtime_evaluation_policy" not in inspect.getsource(evaluate_closed_candle_outcomes)
    assert "build_replay_evaluation_policy" not in inspect.getsource(_simulate_trade)


def test_claim_preservation_complete_context_and_unique_trade_remain_unproven() -> None:
    contract = evidence_contract_payload()
    assert contract["unique_trade_count"]["status"] == UNAVAILABLE
    payload = project_outcome_ownership(
        lifecycle_records=[],
        progress_rows=[],
        provenance={"synthetic": True, "coverage_complete": True},
    )
    assert payload["unavailable_metrics"]["unique_trade_count"]["status"] == UNAVAILABLE
    runtime_scope = _runtime_policy().to_canonical_dict()["scope"]["excludes"]
    assert "complete_episode_policy" in runtime_scope
    assert "source_receipt_authority" in runtime_scope
    assert "historical_or_continuing_progress_attribution" in runtime_scope
