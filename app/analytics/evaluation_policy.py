"""EVAL_POLICY: inspectable evaluation-policy manifest. Not a persisted binding.

This module names the evaluation-policy dimensions that EVAL_SEMANTICS proved
must be bound before runtime and replay outputs are treated as the same
research fact. It records what current evaluators actually implement.

It does **not**:
- mint ``evaluation_policy_id`` or ``policy_hash``
- persist a policy onto progress, analytics, replay, or admission rows
- hash source, schema version, git SHA, filenames, or scan ids into a policy
- change scanner, lifecycle, delivery, replay, or outcome behavior
- authorize replay as runtime outcome authority
- complete prospective admission
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any, Final

from app.analytics.evidence_contract import UNAVAILABLE
from app.data.dtos import NA

MANIFEST_VERSION: Final[str] = "cci-evaluation-policy-manifest-v1"

BINDING_IMPLEMENTED_UNBOUND: Final[str] = "implemented_unbound"
BINDING_CALLER_DECLARED_UNBOUND: Final[str] = "caller_declared_unbound"
BINDING_HARDCODED_UNBOUND: Final[str] = "hardcoded_unbound"
BINDING_ABSENT: Final[str] = "absent"
BINDING_EXTERNALLY_CONTROLLED: Final[str] = "externally_controlled"
BINDING_NOT_A_POLICY_BINDING: Final[str] = "not_a_policy_binding"

REQUIRED_DIMENSIONS: Final[tuple[str, ...]] = (
    "entry_model",
    "boundary_alignment",
    "execution_timeframe",
    "price_basis",
    "same_candle_precedence",
    "fill_rules",
    "termination",
    "expiry_horizon",
    "target_semantics",
)

ABSENT_EXTERNAL_CONTROLS: Final[tuple[str, ...]] = (
    "fees",
    "slippage",
    "queue_position",
    "partial_fills",
    "stop_movement",
    "quantity_fractions",
    "live_order_book_fills",
    "original_market_data_possession_at_decision_time",
    "admission_cause_and_time",
    "continuation_versus_reentry",
    "dependence_groups",
)

FORBIDDEN_SURROGATES: Final[tuple[str, ...]] = (
    "source_sha256",
    "pinned_git_sha",
    "schema_version",
    "pragma_user_version",
    "scan_run_id",
    "database_filename",
    "script_name",
    "caller_name",
    "safe_configuration_hash",
    "algorithm_marker",
    "document_revision",
    "fixture_label",
    "raw_payload_format",
    "ambiguity_policy_metadata",
    "entry_causality_contract",
    "replay_candidate_key",
    "plan_version_id",
    "plan_identity",
    "lifecycle_id",
)

_SURROGATE_REASON: Final[str] = "surrogate_is_not_evaluation_policy_binding"
_UNKNOWN_SURROGATE_REASON: Final[str] = "unknown_surrogate_is_not_evaluation_policy_binding"


def _engine(
    *,
    implemented: str,
    predicate: str,
    symbol: str,
    binding_status: str,
    durable_policy_object: bool,
    notes: str,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "implemented": implemented,
        "predicate": predicate,
        "symbol": symbol,
        "binding_status": binding_status,
        "durable_policy_object": durable_policy_object,
        "evaluation_policy_id": UNAVAILABLE,
        "policy_hash": UNAVAILABLE,
        "notes": notes,
    }
    if extra:
        payload.update(dict(extra))
    return payload


def _dimension(
    *,
    name: str,
    runtime: Mapping[str, Any],
    replay: Mapping[str, Any],
    comparable_without_named_assumption: bool,
    eval_semantics_proof: str,
    comparison_class: str,
) -> dict[str, Any]:
    return {
        "dimension": name,
        "runtime": dict(runtime),
        "replay": dict(replay),
        "comparable_without_named_assumption": comparable_without_named_assumption,
        "eval_semantics_proof": eval_semantics_proof,
        "comparison_class": comparison_class,
        "complete_immutable_binding": False,
    }


def _dimensions() -> dict[str, Any]:
    return {
        "entry_model": _dimension(
            name="entry_model",
            runtime=_engine(
                implemented="zone_overlap",
                predicate="high >= entry_low and low <= entry_high",
                symbol="app.lifecycle.outcome_policy.entry_touched",
                binding_status=BINDING_IMPLEMENTED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "Runtime fill uses the stored entry zone. Matching a replay point "
                    "price to a fixture is not enough to declare the same entry model."
                ),
            ),
            replay=_engine(
                implemented="point_touch",
                predicate="low <= entry <= high",
                symbol="app.backtesting.strategy_replay._price_touched",
                binding_status=BINDING_HARDCODED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "ReplaySetupCandidate.entry_low and entry_high exist on the "
                    "candidate and are not used by _simulate_trade."
                ),
                extra={"entry_price_field": "ReplaySetupCandidate.entry"},
            ),
            comparable_without_named_assumption=False,
            eval_semantics_proof="test_zone_overlap_without_point_touch_is_non_equivalent",
            comparison_class="non_equivalent_implemented_policy_differs",
        ),
        "boundary_alignment": _dimension(
            name="boundary_alignment",
            runtime=_engine(
                implemented="confirmation_or_material_plan_then_fully_post_boundary",
                predicate=(
                    "confirmation / material_plan_first_evaluated_at_after_confirmation; "
                    "exact-open eligible; partial overlap not eligible; exact close of N "
                    "is open of N+1"
                ),
                symbol="app.lifecycle.outcomes._entry_tracking_boundary",
                binding_status=BINDING_IMPLEMENTED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "Local marker entry_causality_contract="
                    "fully_post_boundary_closed_candle_v1 is an algorithm label, not a "
                    "complete policy binding. Material-plan first_evaluated_at is "
                    "processing time of the new progress row, not a universal decision clock."
                ),
            ),
            replay=_engine(
                implemented="detection_index_plus_one",
                predicate="fill search is range(detected_at_index + 1, max_fill_index + 1)",
                symbol="app.backtesting.strategy_replay._simulate_trade",
                binding_status=BINDING_HARDCODED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "The detection candle is never the fill candle. Partial vs exact "
                    "confirmation is not a replay fill rule."
                ),
            ),
            comparable_without_named_assumption=False,
            eval_semantics_proof="test_causal_boundary_exact_partial_later_processing_and_as_of_mutation",
            comparison_class="non_equivalent_implemented_policy_differs",
        ),
        "execution_timeframe": _dimension(
            name="execution_timeframe",
            runtime=_engine(
                implemented="progress_row_timeframe_must_match",
                predicate="mismatch is integrity failure; empty/missing is unverified",
                symbol="app.lifecycle.outcomes.evaluate_closed_candle_outcomes",
                binding_status=BINDING_IMPLEMENTED_UNBOUND,
                durable_policy_object=False,
                notes="Persisted on setup_lifecycle_outcome_progress.execution_timeframe.",
            ),
            replay=_engine(
                implemented="ReplayConfig.execution_timeframe",
                predicate="caller-chosen; default 15m",
                symbol="app.backtesting.strategy_replay.ReplayConfig.execution_timeframe",
                binding_status=BINDING_CALLER_DECLARED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "A ReplayConfig timeframe is a declared experiment input. It is not "
                    "bound to a runtime progress row or an evaluation_policy_id."
                ),
            ),
            comparable_without_named_assumption=False,
            eval_semantics_proof="tests/test_lifecycle_outcomes.py timeframe mismatch path",
            comparison_class="not_comparable_missing_or_conflicting_evidence",
        ),
        "price_basis": _dimension(
            name="price_basis",
            runtime=_engine(
                implemented="supplied_ohlc_on_candle_object",
                predicate="evaluators consume high/low/open/close already on the candle",
                symbol="app.lifecycle.outcomes.evaluate_closed_candle_outcomes",
                binding_status=BINDING_IMPLEMENTED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "Ordinary scanner for exchange=binance uses BinanceFuturesClient "
                    "get_klines /fapi/v1/klines. An endpoint in source does not prove "
                    "the provenance of every imported or synthetic candle. Receipt and "
                    "availability time are unavailable and are not minted."
                ),
            ),
            replay=_engine(
                implemented="supplied_ohlc_after_normalize_candles",
                predicate="same OHLC fields; no exchange fetch inside _simulate_trade",
                symbol="app.backtesting.strategy_replay._normalize_candles",
                binding_status=BINDING_HARDCODED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "A later reconstructed series can support a disclosed simulation. "
                    "It cannot establish exactly what runtime possessed at decision time."
                ),
            ),
            comparable_without_named_assumption=False,
            eval_semantics_proof="EVAL_SEMANTICS source-authority map; paired fixtures are synthetic",
            comparison_class="not_comparable_missing_or_conflicting_evidence",
        ),
        "same_candle_precedence": _dimension(
            name="same_candle_precedence",
            runtime=_engine(
                implemented="conservative_stop_wins",
                predicate=(
                    "entry+stop: entry_and_stop_same_candle_stop_wins; "
                    "post-entry stop+target: post_entry_stop_and_target_same_candle_stop_wins"
                ),
                symbol="app.lifecycle.outcomes.evaluate_closed_candle_outcomes",
                binding_status=BINDING_IMPLEMENTED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "metadata_json ambiguity_policy=conservative_stop_wins is a local "
                    "marker, not evaluation_policy_id. Do not treat conservative as a "
                    "historical default for every past row."
                ),
            ),
            replay=_engine(
                implemented="ReplayConfig.same_candle_policy",
                predicate="conservative vs optimistic; default conservative",
                symbol="app.backtesting.strategy_replay.ReplayConfig.same_candle_policy",
                binding_status=BINDING_CALLER_DECLARED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "_evaluate_exit_candle exists and is unused by _simulate_trade. "
                    "Caller-declared conservative/optimistic is still not a persisted policy."
                ),
                extra={"unused_helper": "app.backtesting.strategy_replay._evaluate_exit_candle"},
            ),
            comparable_without_named_assumption=False,
            eval_semantics_proof="test_same_candle_entry_stop_and_post_entry_stop_target",
            comparison_class="non_equivalent_implemented_policy_differs",
        ),
        "fill_rules": _dimension(
            name="fill_rules",
            runtime=_engine(
                implemented="first_fully_post_boundary_closed_bar",
                predicate="no fill window; invalidation-before-fill is not a replay-style INVALIDATED miss",
                symbol="app.lifecycle.outcomes.evaluate_closed_candle_outcomes",
                binding_status=BINDING_IMPLEMENTED_UNBOUND,
                durable_policy_object=False,
                notes="Runtime has no ReplayConfig.max_fill_candles equivalent.",
            ),
            replay=_engine(
                implemented="detection_plus_one_within_fill_window",
                predicate="max_fill_candles or mode/timeframe default; stop before fill -> INVALIDATED",
                symbol="app.backtesting.strategy_replay._fill_window",
                binding_status=BINDING_CALLER_DECLARED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "Unset max_fill_candles uses mode/timeframe defaults (scalp/challenge "
                    "12 or 6; otherwise hold default). Those defaults are current code, "
                    "not a historical bound policy."
                ),
            ),
            comparable_without_named_assumption=False,
            eval_semantics_proof="test_horizon_unfilled_filled_unresolved_and_input_exhaustion",
            comparison_class="non_equivalent_implemented_policy_differs",
        ),
        "termination": _dimension(
            name="termination",
            runtime=_engine(
                implemented="lifecycle_terminals_and_evaluator_sl_or_tp3",
                predicate=(
                    "evaluator SL_HIT / TP_HIT after TP3; lifecycle EXPIRED/INVALIDATED "
                    "may copy via _terminal_progress_for_record without a candle path"
                ),
                symbol="app.lifecycle.outcomes._terminal_progress_for_record",
                binding_status=BINDING_IMPLEMENTED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "A planted lifecycle EXPIRED copied onto progress is not a comparable "
                    "completed episode. P3A evaluation_context.complete stays false."
                ),
            ),
            replay=_engine(
                implemented="ReplayOutcome_enum",
                predicate="STOPPED / TPn_HIT / NOT_FILLED / INVALIDATED / EXPIRED plus modeled R",
                symbol="app.backtesting.strategy_replay.ReplayOutcome",
                binding_status=BINDING_HARDCODED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "Labels are not interchangeable with runtime terminals. "
                    "NOT_FILLED is MISSED_ENTRY. Replay EXPIRED is hold/end-of-input."
                ),
            ),
            comparable_without_named_assumption=False,
            eval_semantics_proof="test_unsafe_authority_terminal_shortcut_is_not_a_comparable_episode",
            comparison_class="not_comparable_missing_or_conflicting_evidence",
        ),
        "expiry_horizon": _dimension(
            name="expiry_horizon",
            runtime=_engine(
                implemented="no_evaluator_hold_window",
                predicate="pending-suffix exhaustion leaves MANAGING / terminal_outcome=N/A",
                symbol="app.lifecycle.outcomes.evaluate_closed_candle_outcomes",
                binding_status=BINDING_IMPLEMENTED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "End of supplied input is not a supported runtime economic terminal. "
                    "Lifecycle EXPIRED is a state-machine terminal, not an evaluator horizon."
                ),
            ),
            replay=_engine(
                implemented="fill_and_hold_windows",
                predicate="max_hold_candles or mode/timeframe default 48/80; unfilled NOT_FILLED; hold end EXPIRED with mark-to-market R",
                symbol="app.backtesting.strategy_replay._max_hold_candles",
                binding_status=BINDING_CALLER_DECLARED_UNBOUND,
                durable_policy_object=False,
                notes="Replay EXPIRED is not lifecycle EXPIRED.",
            ),
            comparable_without_named_assumption=False,
            eval_semantics_proof="test_horizon_unfilled_filled_unresolved_and_input_exhaustion",
            comparison_class="non_equivalent_implemented_policy_differs",
        ),
        "target_semantics": _dimension(
            name="target_semantics",
            runtime=_engine(
                implemented="milestones_then_tp3_terminal",
                predicate=(
                    "entry candle cannot credit targets; TP1/TP2 timestamps retained; "
                    "later stop is SL_HIT; TP_HIT only after TP3; missing TP3 is not TP3"
                ),
                symbol="app.lifecycle.outcome_events.record_stop",
                binding_status=BINDING_IMPLEMENTED_UNBOUND,
                durable_policy_object=False,
                notes="record_stop does not clear tp*_at. No quantity, fees, or P&L.",
            ),
            replay=_engine(
                implemented="economic_exit_with_optional_tp3",
                predicate=(
                    "fill candle can credit targets; _final_target_number is 3 if tp3 else 2; "
                    "prior TP then stop returns that TP with sl_hit=False and positive R"
                ),
                symbol="app.backtesting.strategy_replay._simulate_trade",
                binding_status=BINDING_HARDCODED_UNBOUND,
                durable_policy_object=False,
                notes=(
                    "Matching a replay TP1 to a runtime TP1 milestone conceals later stop "
                    "information. Replay R is not approved expectancy."
                ),
            ),
            comparable_without_named_assumption=False,
            eval_semantics_proof="test_earlier_tp_then_stop_is_milestone_not_a_winning_exit",
            comparison_class="non_equivalent_implemented_policy_differs",
        ),
    }


def _local_markers() -> dict[str, Any]:
    return {
        "ambiguity_policy_metadata": {
            "where": "setup_lifecycle_outcome_progress.metadata_json",
            "value": "conservative_stop_wins",
            "binding_status": BINDING_NOT_A_POLICY_BINDING,
            "evaluation_policy_id": UNAVAILABLE,
        },
        "entry_causality_contract": {
            "where": "app.lifecycle.outcomes.ENTRY_CAUSALITY_CONTRACT",
            "value": "fully_post_boundary_closed_candle_v1",
            "binding_status": BINDING_NOT_A_POLICY_BINDING,
            "evaluation_policy_id": UNAVAILABLE,
        },
        "algorithm_source_marker": {
            "where": "progress metadata_json source",
            "value": "canonical_lifecycle_closed_execution_candles",
            "binding_status": BINDING_NOT_A_POLICY_BINDING,
            "evaluation_policy_id": UNAVAILABLE,
        },
        "storage_raw_payload_format": {
            "where": "scan_runs.raw_payload_format",
            "value": "inline_v1 or symbol_refs_v1",
            "binding_status": BINDING_NOT_A_POLICY_BINDING,
            "notes": "STORAGE_SINGLE_COPY representation discriminator. Not a policy.",
            "evaluation_policy_id": UNAVAILABLE,
        },
    }


def _completeness() -> dict[str, Any]:
    return {
        "complete_immutable_evaluation_policy_binding": False,
        "persisted_evaluation_policy_id": UNAVAILABLE,
        "persisted_policy_hash": UNAVAILABLE,
        "runtime_complete": False,
        "replay_complete": False,
        "source_context_bound": False,
        "admission_bound": False,
        "missing_required_bindings": list(REQUIRED_DIMENSIONS),
        "reason": (
            "Every required dimension has implemented or caller-declared behavior, "
            "and none is an immutable evaluation-policy binding scoped to an admitted "
            "episode. Completing the checklist by hashing current source or assuming "
            "historical defaults is forbidden."
        ),
        "hashing_source_completes_checklist": False,
        "current_code_is_historical_default": False,
    }


def _authority() -> dict[str, Any]:
    return {
        "runtime_observation_vs_replay_experiment": (
            "Runtime closed-candle evaluation is a live observation path. Strategy "
            "replay is a separate experiment. Characterized disagreement is evidence."
        ),
        "replay_is_runtime_outcome_authority": False,
        "same_opportunity_producer_parity": False,
        "prospective_admission_persistence": "STOP",
        "canonical_episode_outcome": UNAVAILABLE,
        "unique_trade_count": UNAVAILABLE,
        "expectancy": UNAVAILABLE,
        "do_not_impose": "UNIQUE(plan_version_id) as an outcome rule",
    }


def build_evaluation_policy_manifest() -> dict[str, Any]:
    dimensions = _dimensions()
    missing = [name for name in REQUIRED_DIMENSIONS if name not in dimensions]
    if missing:
        raise RuntimeError(f"evaluation policy manifest missing dimensions: {missing}")
    extra = [name for name in dimensions if name not in REQUIRED_DIMENSIONS]
    if extra:
        raise RuntimeError(f"evaluation policy manifest has undeclared dimensions: {extra}")
    return {
        "manifest_version": MANIFEST_VERSION,
        "status": "research_manifest_unbound",
        "audited_base_commit": "afbc4ae75cbc886daea09cb536ae577661b96ada",
        "prerequisite_contracts": (
            "docs/research/observation_unit_contract_r0.md",
            "docs/research/evaluation_semantics_contract.md",
            "docs/research/storage_single_copy.md",
        ),
        "schema_version_expected": 25,
        "does_not": (
            "persist admissions",
            "mint evaluation_policy_id",
            "mint policy_hash",
            "bind source/policy to progress rows",
            "add a tracker",
            "authorize replay as runtime outcome authority",
            "compute expectancy",
            "change evaluator behavior",
        ),
        "required_dimensions": list(REQUIRED_DIMENSIONS),
        "dimensions": dimensions,
        "absent_external_controls": {
            name: {
                "binding_status": BINDING_EXTERNALLY_CONTROLLED,
                "value": NA,
                "evaluation_policy_id": UNAVAILABLE,
            }
            for name in ABSENT_EXTERNAL_CONTROLS
        },
        "forbidden_surrogates": {
            name: {
                "binding_status": BINDING_NOT_A_POLICY_BINDING,
                "evaluation_policy_id": UNAVAILABLE,
                "policy_hash": UNAVAILABLE,
                "reason": _SURROGATE_REASON,
            }
            for name in FORBIDDEN_SURROGATES
        },
        "local_markers_are_not_complete_bindings": _local_markers(),
        "completeness": _completeness(),
        "authority": _authority(),
        "future_binding_if_authorized": (
            "A later immutable evaluation-policy binding must name all nine required "
            "dimensions, refuse the forbidden surrogates, leave absent external "
            "controls as unavailable rather than zero, and still not treat replay as "
            "runtime outcome authority. Naming this future object does not create it."
        ),
    }


def evaluation_policy_manifest() -> dict[str, Any]:
    return build_evaluation_policy_manifest()


def dumps_evaluation_policy_manifest() -> str:
    return json.dumps(
        evaluation_policy_manifest(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def required_dimension_names() -> tuple[str, ...]:
    return REQUIRED_DIMENSIONS


def classify_policy_surrogate(kind: str, value: Any = None) -> dict[str, Any]:
    """Refuse to mint a policy id from a surrogate token.

    A supplied value is acknowledged as present and is never copied into
    ``evaluation_policy_id`` or ``policy_hash``.
    """

    normalized = str(kind or "").strip()
    known = normalized in FORBIDDEN_SURROGATES
    return {
        "kind": normalized if normalized else NA,
        "value_supplied": value is not None,
        "value_used_as_policy_id": False,
        "is_evaluation_policy_binding": False,
        "complete_immutable_binding": False,
        "evaluation_policy_id": UNAVAILABLE,
        "policy_hash": UNAVAILABLE,
        "binding_status": BINDING_NOT_A_POLICY_BINDING,
        "reason": _SURROGATE_REASON if known else _UNKNOWN_SURROGATE_REASON,
    }


def _config_mapping(config: Any) -> Mapping[str, Any]:
    if isinstance(config, Mapping):
        return config
    dump = getattr(config, "model_dump", None)
    if callable(dump):
        payload = dump()
        if isinstance(payload, Mapping):
            return payload
    raise TypeError("declared_replay_experiment_envelope requires a mapping or ReplayConfig")


def declared_replay_experiment_envelope(config: Any) -> dict[str, Any]:
    """Describe caller-declared replay fields without claiming a complete binding.

    Unset fill/hold windows stay unset. Mode/timeframe defaults are not invented
    here as a historical policy for past rows.
    """

    payload = _config_mapping(config)
    declared = {
        "execution_timeframe": payload.get("execution_timeframe", NA),
        "same_candle_policy": payload.get("same_candle_policy", NA),
        "max_fill_candles": payload.get("max_fill_candles"),
        "max_hold_candles": payload.get("max_hold_candles"),
    }
    if declared["max_fill_candles"] is None:
        declared["max_fill_candles"] = "unset_uses_mode_timeframe_default"
    if declared["max_hold_candles"] is None:
        declared["max_hold_candles"] = "unset_uses_mode_timeframe_default"
    return {
        "kind": "replay_experiment_envelope",
        "complete_immutable_binding": False,
        "is_evaluation_policy_binding": False,
        "evaluation_policy_id": UNAVAILABLE,
        "policy_hash": UNAVAILABLE,
        "declared": declared,
        "still_hardcoded": [
            "entry_model",
            "boundary_alignment",
            "price_basis",
            "termination",
            "target_semantics",
            "entry_candle_target_eligibility",
        ],
        "notes": (
            "A ReplayConfig difference is a different declared experiment, not a "
            "persisted evaluation_policy_id. Hardcoded dimensions remain unbound."
        ),
    }


def implemented_runtime_policy_snapshot() -> dict[str, Any]:
    dimensions = _dimensions()
    return {
        "kind": "runtime_implemented_policy_snapshot",
        "complete_immutable_binding": False,
        "is_evaluation_policy_binding": False,
        "evaluation_policy_id": UNAVAILABLE,
        "policy_hash": UNAVAILABLE,
        "implemented": {
            name: dimensions[name]["runtime"]["implemented"] for name in REQUIRED_DIMENSIONS
        },
        "durable_policy_object": False,
        "notes": (
            "This snapshot describes current runtime code. It is not a versioned "
            "binding and must not be hashed into evaluation_policy_id."
        ),
    }


__all__ = [
    "ABSENT_EXTERNAL_CONTROLS",
    "BINDING_ABSENT",
    "BINDING_CALLER_DECLARED_UNBOUND",
    "BINDING_EXTERNALLY_CONTROLLED",
    "BINDING_HARDCODED_UNBOUND",
    "BINDING_IMPLEMENTED_UNBOUND",
    "BINDING_NOT_A_POLICY_BINDING",
    "FORBIDDEN_SURROGATES",
    "MANIFEST_VERSION",
    "REQUIRED_DIMENSIONS",
    "build_evaluation_policy_manifest",
    "classify_policy_surrogate",
    "declared_replay_experiment_envelope",
    "dumps_evaluation_policy_manifest",
    "evaluation_policy_manifest",
    "implemented_runtime_policy_snapshot",
    "required_dimension_names",
]
