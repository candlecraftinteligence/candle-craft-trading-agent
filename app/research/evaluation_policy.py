"""Immutable executable evaluator-policy manifests (EVALUATION_POLICY_MANIFEST).

Contract-only library. Describes the current runtime closed-candle outcome
evaluator and the current replay trade simulator at their declared call
boundaries. It does not persist IDs, bind progress/analytics/scan/replay rows,
admit episodes, or change either evaluator.

A content-derived policy ID identifies the declared invocation contract under
normalized effective parameters. It is not a research-episode binding, a
replay-experiment identity, or evidence that any stored row was produced under
this policy.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType, SimpleNamespace
from typing import Any, Final

from app.backtesting.strategy_replay import ReplayConfig, _fill_window, _max_hold_candles
from app.data.candle_integrity import CandleIntegrityError, timeframe_duration
from app.strategies.liquidity_grab_pullback import LiquidityGrabMode

MANIFEST_FORMAT_VERSION: Final[str] = "cci-evaluation-policy-manifest-v1"
RUNTIME_RULE_VOCABULARY_VERSION: Final[str] = "cci-runtime-closed-candle-rules-v1"
REPLAY_RULE_VOCABULARY_VERSION: Final[str] = "cci-replay-trade-rules-v1"
EVALUATOR_FAMILY_RUNTIME: Final[str] = "runtime_lifecycle_closed_candle"
EVALUATOR_FAMILY_REPLAY: Final[str] = "replay_trade_simulation"
POLICY_ID_PREFIX: Final[str] = "eval-policy-"
ID_DOMAIN: Final[bytes] = b"cci.evaluation_policy_manifest.v1"
FORBIDDEN_PLACEHOLDERS: Final[frozenset[str]] = frozenset(
    {"unknown", "n/a", "na", "unresolved", "tbd", "placeholder"}
)

_TOP_LEVEL_KEYS: Final[tuple[str, ...]] = (
    "manifest_format_version",
    "evaluator_family",
    "scope",
    "rule_vocabulary_version",
    "rules",
    "effective_parameters",
)
_SCOPE_KEYS: Final[tuple[str, ...]] = (
    "call_boundary",
    "identity_describes",
    "includes",
    "excludes",
)
_RUNTIME_PARAMETER_KEYS: Final[tuple[str, ...]] = ("execution_timeframe",)
_REPLAY_PARAMETER_KEYS: Final[tuple[str, ...]] = (
    "execution_timeframe",
    "same_candle_policy",
    "fill_window_candles",
    "max_hold_candles",
)
_RUNTIME_RULE_KEYS: Final[tuple[str, ...]] = (
    "entry_representation",
    "price_touch_predicates",
    "execution_timeframe",
    "candle_eligibility",
    "causal_start",
    "cutoff_and_processing",
    "entry_candle_policy",
    "pre_entry_invalidation",
    "same_candle_precedence",
    "milestone_versus_terminal",
    "target_ladder",
    "horizons",
    "expiry_terminal_shortcuts",
    "continuation_and_failure",
    "output_and_valuation",
    "price_consumption",
)
_REPLAY_RULE_KEYS: Final[tuple[str, ...]] = (
    "entry_representation",
    "price_touch_predicates",
    "execution_timeframe",
    "candle_eligibility",
    "causal_start",
    "cutoff_and_processing",
    "entry_candle_policy",
    "pre_entry_invalidation",
    "same_candle_precedence",
    "milestone_versus_terminal",
    "replay_exit_interpretation",
    "target_ladder",
    "horizons",
    "fill_search",
    "expiry_terminal_shortcuts",
    "continuation_and_failure",
    "output_and_valuation",
    "price_consumption",
)

# Provenance is intentionally outside the identity payload.
RUNTIME_CALL_BOUNDARY: Final[str] = "app.lifecycle.outcomes.evaluate_closed_candle_outcomes"
REPLAY_CALL_BOUNDARY: Final[str] = "app.backtesting.strategy_replay._simulate_trade"
UNUSED_REPLAY_HELPER: Final[str] = "app.backtesting.strategy_replay._evaluate_exit_candle"


class EvaluationPolicyError(ValueError):
    """Raised when a policy payload is incomplete, unknown, or malformed."""


@dataclass(frozen=True, slots=True)
class EvaluationPolicyManifest:
    """Validated immutable evaluator-policy contract plus its content-derived ID."""

    family: str
    payload: Mapping[str, Any]
    canonical_bytes: bytes
    policy_id: str

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return a defensive JSON-round-trip copy of the identity payload."""

        loaded = json.loads(self.canonical_bytes.decode("utf-8"))
        if not isinstance(loaded, dict):
            raise EvaluationPolicyError("canonical_bytes_must_decode_to_object")
        return loaded


def build_runtime_evaluation_policy(*, execution_timeframe: str) -> EvaluationPolicyManifest:
    """Return the current runtime closed-candle evaluator contract for one timeframe."""

    timeframe = _normalize_supported_timeframe(execution_timeframe, path="execution_timeframe")
    payload = {
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "evaluator_family": EVALUATOR_FAMILY_RUNTIME,
        "scope": _runtime_scope(),
        "rule_vocabulary_version": RUNTIME_RULE_VOCABULARY_VERSION,
        "rules": _runtime_rules(),
        "effective_parameters": {"execution_timeframe": timeframe},
    }
    return parse_evaluation_policy_payload(payload)


def build_replay_evaluation_policy(
    *,
    config: ReplayConfig,
    mode: LiquidityGrabMode | str,
) -> EvaluationPolicyManifest:
    """Return the current replay trade-simulator contract for one mode/config.

    Effective fill/hold limits are resolved from the validated ``ReplayConfig`` and
    the supplied candidate mode. Discovery-only config fields do not enter identity.
    Do not pass one multi-mode config as if it were a single policy.
    """

    if not isinstance(config, ReplayConfig):
        raise EvaluationPolicyError("replay_config_must_be_replayconfig_instance")
    resolved_mode = _normalize_mode(mode)
    timeframe = _normalize_supported_timeframe(
        config.execution_timeframe,
        path="effective_parameters.execution_timeframe",
    )
    fill_window = _fill_window(SimpleNamespace(mode=resolved_mode), config)
    hold = _max_hold_candles(resolved_mode, config.execution_timeframe, config)
    _require_positive_int(fill_window, "effective_parameters.fill_window_candles")
    _require_positive_int(hold, "effective_parameters.max_hold_candles")
    payload = {
        "manifest_format_version": MANIFEST_FORMAT_VERSION,
        "evaluator_family": EVALUATOR_FAMILY_REPLAY,
        "scope": _replay_scope(),
        "rule_vocabulary_version": REPLAY_RULE_VOCABULARY_VERSION,
        "rules": _replay_rules(),
        "effective_parameters": {
            "execution_timeframe": timeframe,
            "same_candle_policy": config.same_candle_policy,
            "fill_window_candles": fill_window,
            "max_hold_candles": hold,
        },
    }
    return parse_evaluation_policy_payload(payload)


def parse_evaluation_policy_payload(payload: Mapping[str, Any]) -> EvaluationPolicyManifest:
    """Validate a semantic payload and mint its content-derived policy ID."""

    if not isinstance(payload, Mapping):
        raise EvaluationPolicyError("payload_must_be_a_mapping")
    _reject_unknown_keys(payload, _TOP_LEVEL_KEYS, path="")
    for key in _TOP_LEVEL_KEYS:
        if key not in payload:
            raise EvaluationPolicyError(f"missing_field:{key}")
    format_version = _require_token(
        payload["manifest_format_version"],
        path="manifest_format_version",
        allowed={MANIFEST_FORMAT_VERSION},
    )
    family = _require_token(
        payload["evaluator_family"],
        path="evaluator_family",
        allowed={EVALUATOR_FAMILY_RUNTIME, EVALUATOR_FAMILY_REPLAY},
    )
    if family == EVALUATOR_FAMILY_RUNTIME:
        expected_vocab = RUNTIME_RULE_VOCABULARY_VERSION
        expected_rules = _RUNTIME_RULE_KEYS
        expected_params = _RUNTIME_PARAMETER_KEYS
        expected_scope = _runtime_scope()
    else:
        expected_vocab = REPLAY_RULE_VOCABULARY_VERSION
        expected_rules = _REPLAY_RULE_KEYS
        expected_params = _REPLAY_PARAMETER_KEYS
        expected_scope = _replay_scope()
    vocab = _require_token(
        payload["rule_vocabulary_version"],
        path="rule_vocabulary_version",
        allowed={expected_vocab},
    )
    if vocab != expected_vocab:
        raise EvaluationPolicyError("rule_vocabulary_does_not_match_family")
    scope = _validate_scope(payload["scope"], expected_scope)
    rules = _validate_rules(payload["rules"], expected_rules)
    declared_rules = _runtime_rules() if family == EVALUATOR_FAMILY_RUNTIME else _replay_rules()
    if _json_ready(rules) != _json_ready(declared_rules):
        raise EvaluationPolicyError("rules_must_match_declared_vocabulary")
    params = _validate_effective_parameters(payload["effective_parameters"], expected_params)
    validated = {
        "manifest_format_version": format_version,
        "evaluator_family": family,
        "scope": scope,
        "rule_vocabulary_version": vocab,
        "rules": rules,
        "effective_parameters": params,
    }
    frozen = _freeze(validated)
    canonical = canonical_json_bytes(validated)
    policy_id = _policy_id_for(canonical)
    return EvaluationPolicyManifest(
        family=family,
        payload=frozen,
        canonical_bytes=canonical,
        policy_id=policy_id,
    )


def canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Return canonical UTF-8 JSON bytes for a validated identity payload."""

    ready = _json_ready(payload)
    try:
        text = json.dumps(
            ready,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise EvaluationPolicyError("canonical_json_rejected") from exc
    return text.encode("utf-8")


def resolve_replay_effective_parameters(
    *,
    config: ReplayConfig,
    mode: LiquidityGrabMode | str,
) -> dict[str, Any]:
    """Resolve trade-simulation parameters without minting a policy ID."""

    manifest = build_replay_evaluation_policy(config=config, mode=mode)
    copied = manifest.to_canonical_dict()["effective_parameters"]
    if not isinstance(copied, dict):
        raise EvaluationPolicyError("effective_parameters_must_be_object")
    return copied


def _runtime_scope() -> dict[str, Any]:
    return {
        "call_boundary": RUNTIME_CALL_BOUNDARY,
        "identity_describes": "current_evaluator_invocation_contract",
        "includes": [
            "interpretation_of_supplied_lifecycle_progress_history_and_closed_ohlc",
            "terminal_shortcuts_and_boundary_fallbacks",
            "supplied_ohlc_range_evaluation",
        ],
        "excludes": [
            "admission_selection",
            "discovery",
            "upstream_retirement_decisions",
            "source_receipt_authority",
            "scheduling_and_interval_coverage",
            "episode_ownership",
            "historical_or_continuing_progress_attribution",
            "venue_contract_or_mark_price_provenance",
            "complete_episode_policy",
        ],
    }


def _replay_scope() -> dict[str, Any]:
    return {
        "call_boundary": REPLAY_CALL_BOUNDARY,
        "identity_describes": "current_evaluator_invocation_contract",
        "includes": [
            "point_entry_and_inclusive_range_price_touch_simulation",
            "resolved_fill_and_hold_horizons_for_one_mode",
            "same_candle_conservative_or_optimistic_exit_policy",
            "modeled_r_from_declared_entry_stop_and_targets",
        ],
        "excludes": [
            "admission_selection",
            "discovery_and_max_setups_replay_candles_or_aggressive_toggle",
            "source_receipt_authority",
            "same_opportunity_mapping_to_runtime",
            "actual_fill_fees_funding_or_slippage",
            "historical_or_continuing_progress_attribution",
            "complete_episode_policy",
            "unused_helper_evaluate_exit_candle",
        ],
    }


def _runtime_rules() -> dict[str, Any]:
    return {
        "entry_representation": {
            "model": "inclusive_zone_overlap",
            "predicate": "high >= entry_low and low <= entry_high",
            "uses_replay_entry_point": False,
            "long_short_symmetric_overlap": True,
        },
        "price_touch_predicates": {
            "stop": {
                "model": "directional_threshold_inequality",
                "long": "low <= stop_loss",
                "short": "high >= stop_loss",
                "requires_level_inside_inclusive_range": False,
            },
            "targets": {
                "model": "directional_threshold_passage",
                "long": "high >= target",
                "short": "low <= target",
                "requires_level_inside_inclusive_range": False,
                "records_all_newly_reached_stored_targets_on_the_candle": True,
            },
            "price_jump_without_containing_level": "still_counts_when_threshold_is_passed",
            "missing_candle_interval": "integrity_failure_not_a_price_jump",
        },
        "execution_timeframe": {
            "normalized": "strip_lower",
            "progress_mismatch": "integrity_failed_execution_timeframe_changed",
            "missing": "integrity_unverified_missing_execution_timeframe",
        },
        "candle_eligibility": {
            "timestamp_normalization": "utc_epoch_ms_or_aware_or_naive_as_utc",
            "accepted_close_boundary": "close_timestamp <= decision_timestamp",
            "continuity_order_duplicates": "validate_candle_sequence_require_continuity_true",
            "minimum_closed_history": 0,
            "source_availability_implied": False,
        },
        "causal_start": {
            "confirmation_primary": "record.confirmed_at_else_confirmed_event_timestamp",
            "material_plan_other_identities": (
                "max_of_confirmation_and_first_evaluated_at_or_evaluated_at"
            ),
            "first_eligible_open": "first_fully_post_boundary_aligned_open",
            "exact_open": "boundary_equals_candle_open_makes_that_bar_entry_eligible",
            "partial_overlap": "boundary_inside_bar_makes_that_bar_not_entry_eligible",
            "legacy_unfilled_cursor": "adopt_existing_cursor_prospectively_with_metadata",
        },
        "cutoff_and_processing": {
            "knowledge_cutoff": "supplied_decision_timestamp",
            "processing_time": "evaluated_at_does_not_admit_later_closes",
            "later_processing_clock_is_not_cutoff": True,
        },
        "entry_candle_policy": {
            "fill_on_entry_candle": True,
            "stop_on_entry_candle": True,
            "targets_on_entry_candle": False,
            "targets_begin": "next_closed_execution_candle",
        },
        "pre_entry_invalidation": {
            "stop_without_entry": "does_not_invalidate_advances_cursor",
            "targets_without_entry": "do_not_count",
        },
        "same_candle_precedence": {
            "policy": "conservative_stop_wins",
            "entry_and_stop": "entry_and_stop_same_candle_stop_wins",
            "post_entry_stop_and_target": "post_entry_stop_and_target_same_candle_stop_wins",
            "configurable": False,
        },
        "milestone_versus_terminal": {
            "tp1": "milestone_timestamp",
            "tp2": "milestone_timestamp",
            "tp3": "terminal_tp_hit",
            "later_stop_retains_earlier_milestones": True,
            "record_stop_clears_tp_timestamps": False,
        },
        "target_ladder": {
            "stored_tp1_tp2_tp3": "required",
            "missing_tp3": "not_tp3_and_not_terminal_tp_hit",
            "supplied_ladder_is_plan_input_not_policy_identity": True,
        },
        "horizons": {
            "fill_window": None,
            "hold_window": None,
            "end_of_input": "not_an_economic_terminal",
            "pending_suffix_exhaustion": "managing_terminal_outcome_na",
        },
        "expiry_terminal_shortcuts": {
            "incoming_lifecycle_terminal": "copy_state_without_completing_candle_filter",
            "causal_economic_support_implied": False,
            "existing_progress_terminal": "no_op_return",
        },
        "continuation_and_failure": {
            "missing_history": "integrity_unverified",
            "missing_timeframe": "integrity_unverified",
            "integrity_failures": "unverified_or_failed_no_invented_outcome",
            "no_new_pending_candles": "verified_no_new_pending_candles",
            "legal_early_terminal": "break_after_recorded_terminal",
            "not_run_skipped_by_service": True,
            "none_execution_candles_skips_evaluator": True,
            "empty_list_is_integrity_path": True,
        },
        "output_and_valuation": {
            "facts": "timestamps_labels_integrity_cursor_prefix_disposition",
            "modeled_r": False,
            "real_fill_authority": False,
            "complete_episode_outcome_authority": False,
        },
        "price_consumption": {
            "basis": "supplied_ohlc_range_evaluation",
            "venue_or_mark_price_asserted": False,
        },
    }


def _replay_rules() -> dict[str, Any]:
    return {
        "entry_representation": {
            "model": "inclusive_point_containment",
            "predicate": "low <= entry <= high",
            "uses_candidate_entry_low_high_in_simulate_trade": False,
            "long_short_symmetric_containment": True,
        },
        "price_touch_predicates": {
            "stop": {
                "model": "inclusive_range_containment",
                "predicate": "low <= stop <= high",
                "requires_level_inside_inclusive_range": True,
            },
            "targets": {
                "model": "inclusive_range_containment",
                "predicate": "low <= target <= high",
                "requires_level_inside_inclusive_range": True,
                "highest_numbered_target_in_range_without_requiring_lower_targets": True,
            },
            "price_jump_without_containing_level": "does_not_count",
            "missing_candle_interval": "normalization_integrity_failure_not_a_price_jump",
        },
        "execution_timeframe": {
            "source": "replay_config.execution_timeframe",
            "normalized": "strip_lower",
            "numeric_hold_default_currently_independent_of_1h_4h_branch": True,
        },
        "candle_eligibility": {
            "timestamp_normalization": "validate_candle_sequence_then_replay_candle",
            "accepted_close_boundary": "not_decision_timestamp_sliced",
            "continuity_order_duplicates": "validate_candle_sequence_require_continuity_true",
            "source_availability_implied": False,
        },
        "causal_start": {
            "fill_search_start": "detected_at_index_plus_one",
            "detection_candle_never_fills": True,
        },
        "cutoff_and_processing": {
            "knowledge_cutoff": "not_applied_inside_simulate_trade",
            "trade_simulation_may_consume_later_execution_bars_after_detection": True,
        },
        "entry_candle_policy": {
            "fill_on_fill_candle": True,
            "targets_and_exits_include_fill_candle": True,
        },
        "pre_entry_invalidation": {
            "stop_without_entry": "invalidated",
            "failure_reason": "Invalidation touched before the limit entry filled.",
        },
        "same_candle_precedence": {
            "configurable": True,
            "conservative": (
                "new_target_and_stop_on_same_candle_with_no_prior_tp_is_stopped; "
                "with_prior_tp_returns_prior_target_result"
            ),
            "optimistic": "credit_new_targets_before_stop_on_the_same_candle",
        },
        "milestone_versus_terminal": {
            "tp_hits_are_exit_labels": True,
            "runtime_style_retained_milestones_after_later_stop": False,
        },
        "replay_exit_interpretation": {
            "prior_target_then_stop": "return_highest_target_result_sl_hit_false_positive_r",
            "hold_or_input_end_with_prior_target": "return_highest_target_result",
            "inferred_partial_quantities": False,
            "actual_execution": False,
        },
        "target_ladder": {
            "tp3": "optional_na_means_final_target_is_tp2_else_tp3",
            "supplied_ladder_is_plan_input_not_policy_identity": True,
        },
        "horizons": {
            "fill_window_resolution": (
                "explicit_max_fill_candles_else_challenge_or_scalp_12_if_confirmation_5m_else_6_"
                "else_swing_uses_effective_hold"
            ),
            "hold_resolution": (
                "explicit_max_hold_candles_else_48_for_challenge_or_scalp_else_80_for_swing"
            ),
            "hold_loop": "inclusive_fill_index_through_fill_index_plus_max_hold_candles",
            "hold_loop_clipped_to_input_end": True,
            "unfilled": "missed_entry_not_filled",
        },
        "fill_search": {
            "max_fill_index": "min_last_index_and_detected_at_index_plus_fill_window",
            "loop": "range_detected_plus_one_through_max_fill_index_inclusive",
        },
        "expiry_terminal_shortcuts": {
            "hold_or_input_end_without_target": "expired_mark_to_market_r_at_close",
            "incoming_lifecycle_terminal_copy": False,
        },
        "continuation_and_failure": {
            "missing_execution_candles_at_engine": "replay_unavailable_note",
            "empty_or_invalid_series": "normalize_candles_raises",
        },
        "output_and_valuation": {
            "facts": "replay_outcome_filled_flags_indexes_modeled_r",
            "modeled_r": {
                "long": "(price - entry) / abs(entry - stop)",
                "short": "(entry - price) / abs(entry - stop)",
                "stopped_without_prior_target": "-1",
                "expired_without_target": "mark_to_market_at_expiry_close",
                "non_positive_risk": "0",
            },
            "real_fill_authority": False,
            "complete_episode_outcome_authority": False,
        },
        "price_consumption": {
            "basis": "supplied_ohlc_range_evaluation",
            "venue_or_mark_price_asserted": False,
        },
    }


def _validate_scope(value: Any, expected: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationPolicyError("scope_must_be_object")
    _reject_unknown_keys(value, _SCOPE_KEYS, path="scope")
    call_boundary = _require_token(
        value.get("call_boundary"),
        path="scope.call_boundary",
        allowed={expected["call_boundary"]},
    )
    identity_describes = _require_token(
        value.get("identity_describes"),
        path="scope.identity_describes",
        allowed={expected["identity_describes"]},
    )
    includes = _require_string_list(value.get("includes"), path="scope.includes")
    excludes = _require_string_list(value.get("excludes"), path="scope.excludes")
    if includes != list(expected["includes"]):
        raise EvaluationPolicyError("scope.includes_must_match_declared_boundary")
    if excludes != list(expected["excludes"]):
        raise EvaluationPolicyError("scope.excludes_must_match_declared_boundary")
    return {
        "call_boundary": call_boundary,
        "identity_describes": identity_describes,
        "includes": includes,
        "excludes": excludes,
    }


def _validate_rules(value: Any, expected_keys: Sequence[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationPolicyError("rules_must_be_object")
    _reject_unknown_keys(value, expected_keys, path="rules")
    for key in expected_keys:
        if key not in value:
            raise EvaluationPolicyError(f"missing_field:rules.{key}")
    validated: dict[str, Any] = {}
    for key in expected_keys:
        validated[key] = _validate_rule_node(value[key], path=f"rules.{key}")
    return validated


def _validate_rule_node(value: Any, *, path: str) -> Any:
    if isinstance(value, Mapping):
        _reject_placeholder_mapping(value, path=path)
        return {str(key): _validate_rule_node(item, path=f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_validate_rule_node(item, path=f"{path}[]") for item in value]
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if type(value) is int:
        return value
    if isinstance(value, str):
        _reject_placeholder(value, path=path)
        return value
    if isinstance(value, float):
        raise EvaluationPolicyError(f"non_finite_or_float_forbidden:{path}")
    raise EvaluationPolicyError(f"unsupported_type:{path}")


def _validate_effective_parameters(value: Any, expected_keys: Sequence[str]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise EvaluationPolicyError("effective_parameters_must_be_object")
    _reject_unknown_keys(value, expected_keys, path="effective_parameters")
    for key in expected_keys:
        if key not in value:
            raise EvaluationPolicyError(f"missing_field:effective_parameters.{key}")
    timeframe = _normalize_supported_timeframe(
        value["execution_timeframe"],
        path="effective_parameters.execution_timeframe",
    )
    if expected_keys == _RUNTIME_PARAMETER_KEYS:
        return {"execution_timeframe": timeframe}
    policy = _require_token(
        value["same_candle_policy"],
        path="effective_parameters.same_candle_policy",
        allowed={"conservative", "optimistic"},
    )
    fill_window = _require_positive_int(
        value["fill_window_candles"],
        "effective_parameters.fill_window_candles",
    )
    hold = _require_positive_int(
        value["max_hold_candles"],
        "effective_parameters.max_hold_candles",
    )
    return {
        "execution_timeframe": timeframe,
        "same_candle_policy": policy,
        "fill_window_candles": fill_window,
        "max_hold_candles": hold,
    }


def _normalize_supported_timeframe(value: Any, *, path: str) -> str:
    if not isinstance(value, str):
        raise EvaluationPolicyError(f"invalid_execution_timeframe:{path}")
    normalized = value.strip().lower()
    if not normalized:
        raise EvaluationPolicyError(f"invalid_execution_timeframe:{path}")
    _reject_placeholder(normalized, path=path)
    try:
        timeframe_duration(normalized)
    except CandleIntegrityError as exc:
        raise EvaluationPolicyError(f"unsupported_execution_timeframe:{path}") from exc
    return normalized


def _normalize_mode(mode: LiquidityGrabMode | str) -> LiquidityGrabMode:
    if isinstance(mode, LiquidityGrabMode):
        return mode
    if isinstance(mode, str):
        try:
            return LiquidityGrabMode(mode.strip().lower())
        except ValueError as exc:
            raise EvaluationPolicyError("unsupported_replay_mode") from exc
    raise EvaluationPolicyError("unsupported_replay_mode")


def _require_token(value: Any, *, path: str, allowed: set[str]) -> str:
    if not isinstance(value, str):
        raise EvaluationPolicyError(f"invalid_token:{path}")
    token = value.strip()
    _reject_placeholder(token, path=path)
    if token not in allowed:
        raise EvaluationPolicyError(f"unknown_value:{path}")
    return token


def _require_string_list(value: Any, *, path: str) -> list[str]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise EvaluationPolicyError(f"invalid_string_list:{path}")
    items: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str):
            raise EvaluationPolicyError(f"invalid_string_list:{path}[{index}]")
        _reject_placeholder(item, path=f"{path}[{index}]")
        items.append(item)
    return items


def _require_positive_int(value: Any, path: str) -> int:
    if isinstance(value, bool) or type(value) is not int:
        raise EvaluationPolicyError(f"invalid_int:{path}")
    if value < 1:
        raise EvaluationPolicyError(f"non_positive:{path}")
    return value


def _reject_unknown_keys(value: Mapping[str, Any], allowed: Sequence[str], *, path: str) -> None:
    allowed_set = set(allowed)
    for key in value:
        if key not in allowed_set:
            location = f"{path}.{key}" if path else str(key)
            raise EvaluationPolicyError(f"unknown_field:{location}")


def _reject_placeholder(value: str, *, path: str) -> None:
    if value.strip().lower() in FORBIDDEN_PLACEHOLDERS:
        raise EvaluationPolicyError(f"unresolved_placeholder:{path}")


def _reject_placeholder_mapping(value: Mapping[str, Any], *, path: str) -> None:
    for key in value:
        if not isinstance(key, str):
            raise EvaluationPolicyError(f"non_string_key:{path}")
        _reject_placeholder(key, path=f"{path}.{key}")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze(item) for item in value)
    return value


def _json_ready(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_json_ready(item) for item in value]
    if value is None or isinstance(value, (str, bool)):
        return value
    if type(value) is int:
        return value
    if isinstance(value, float):
        raise EvaluationPolicyError("non_finite_or_float_forbidden")
    raise EvaluationPolicyError("unsupported_canonical_type")


def _policy_id_for(canonical_bytes: bytes) -> str:
    digest = hashlib.sha256(ID_DOMAIN + b"\x1f" + canonical_bytes).hexdigest()
    return f"{POLICY_ID_PREFIX}{digest}"


__all__ = [
    "EVALUATOR_FAMILY_REPLAY",
    "EVALUATOR_FAMILY_RUNTIME",
    "EvaluationPolicyError",
    "EvaluationPolicyManifest",
    "MANIFEST_FORMAT_VERSION",
    "POLICY_ID_PREFIX",
    "REPLAY_CALL_BOUNDARY",
    "REPLAY_RULE_VOCABULARY_VERSION",
    "RUNTIME_CALL_BOUNDARY",
    "RUNTIME_RULE_VOCABULARY_VERSION",
    "UNUSED_REPLAY_HELPER",
    "build_replay_evaluation_policy",
    "build_runtime_evaluation_policy",
    "canonical_json_bytes",
    "parse_evaluation_policy_payload",
    "resolve_replay_effective_parameters",
]
