"""Canonical CCI evidence-contract definitions.

This module is the machine-readable source of truth for research counting
units, metric dictionary entries, and timestamp semantics. It documents the
current implementation; it does not change scanner, lifecycle, delivery, or
outcome behavior.
"""

from __future__ import annotations

import json
from typing import Any, Final

CONTRACT_VERSION: Final[str] = "cci-evidence-contract-v2"
UNSAFE: Final[str] = "CURRENTLY UNSAFE / AMBIGUOUS"
UNAVAILABLE: Final[str] = "unavailable"

_REQUIRED_ENTITIES: Final[tuple[str, ...]] = (
    "OBSERVATION",
    "CANDIDATE",
    "SETUP",
    "ECONOMIC PLAN",
    "PLAN VERSION",
    "LIFECYCLE",
    "READINESS",
    "QUALITY",
    "CONFIRMATION",
    "ACTIVATION",
    "SIMULATED FILL",
    "MANUAL FILL",
    "PUBLIC SIGNAL",
    "OUTCOME",
    "REPLAY RECORD",
)

_REQUIRED_METRICS: Final[tuple[str, ...]] = (
    "symbols_requested",
    "symbols_queued",
    "symbols_completed",
    "symbols_scanned",
    "total_valid_setups",
    "near_misses",
    "rejected",
    "data_issues",
    "valid_activations",
    "still_watching",
    "actionable_setups",
    "confirmed_setups",
    "actionable_a_grade_setups",
    "candidate_a_grade_setups",
    "blocked_a_grade_by_scoring",
    "blocked_a_grade_by_target",
    "blocked_a_grade_by_entry_window",
    "blocked_a_grade_by_trust",
    "fatal_target_blocks",
    "soft_target_warnings",
)

_REQUIRED_TIMESTAMPS: Final[tuple[str, ...]] = (
    "source_event_time",
    "source_receipt_time",
    "feature_ready_time",
    "scan_time",
    "decision_time",
    "lifecycle_event_time",
    "public_available_time",
    "outcome_evaluation_time",
)


def _entity(
    *,
    current_meaning: str,
    intended_research_meaning: str,
    current_authoritative_id: str,
    proposed_future_id: str,
    unit: str,
    owner: str,
    creation_point: str,
    mutability: str,
    timestamp_semantics: str,
    relationships: str,
    cardinality: str,
    research_statistics_safe: str,
    producers: tuple[str, ...],
    consumers: tuple[str, ...],
    enforcement: str,
) -> dict[str, Any]:
    return {
        "current_meaning": current_meaning,
        "intended_research_meaning": intended_research_meaning,
        "current_authoritative_id": current_authoritative_id,
        "proposed_future_id": proposed_future_id,
        "unit": unit,
        "owner": owner,
        "creation_point": creation_point,
        "mutability": mutability,
        "timestamp_semantics": timestamp_semantics,
        "relationships": relationships,
        "cardinality": cardinality,
        "research_statistics_safe": research_statistics_safe,
        "producers": list(producers),
        "consumers": list(consumers),
        "enforcement": enforcement,
    }


def _metric(
    *,
    producer: str,
    update_path: str,
    persistence: str,
    inclusion_rules: str,
    counting_unit: str,
    scope: str,
    repeated_observations_counted: str,
    unique_economic_plans_counted: str,
    snapshot_time: str,
    relative_to_lifecycle: str,
    consumers: tuple[str, ...],
    default_null_zero: str,
    categories_overlap: str,
    research_funnel_suitable: str,
    trust_status: str,
    limitations: str,
) -> dict[str, Any]:
    return {
        "producer": producer,
        "update_path": update_path,
        "persistence": persistence,
        "inclusion_rules": inclusion_rules,
        "counting_unit": counting_unit,
        "scope": scope,
        "repeated_observations_counted": repeated_observations_counted,
        "unique_economic_plans_counted": unique_economic_plans_counted,
        "snapshot_time": snapshot_time,
        "relative_to_lifecycle": relative_to_lifecycle,
        "consumers": list(consumers),
        "default_null_zero": default_null_zero,
        "categories_overlap": categories_overlap,
        "research_funnel_suitable": research_funnel_suitable,
        "trust_status": trust_status,
        "limitations": limitations,
    }


def _timestamp(
    *,
    existing_field: str,
    producer: str,
    meaning: str,
    format_timezone: str,
    null_semantics: str,
    event_or_wall: str,
    as_of_suitable: str,
    missing_and_future: str,
) -> dict[str, Any]:
    return {
        "existing_field": existing_field,
        "producer": producer,
        "meaning": meaning,
        "format_timezone": format_timezone,
        "null_semantics": null_semantics,
        "event_or_wall": event_or_wall,
        "as_of_suitable": as_of_suitable,
        "missing_and_future": missing_and_future,
    }


def build_evidence_contract() -> dict[str, Any]:
    """Return the canonical structured evidence contract."""

    return {
        "contract_version": CONTRACT_VERSION,
        "unsafe_label": UNSAFE,
        "unavailable_label": UNAVAILABLE,
        "unique_trade_count": {
            "status": UNAVAILABLE,
            "reason": (
                "A unique-trade count requires an immutable economic plan version, "
                "an explicit fill policy, an explicit exit policy, and one "
                "authoritative outcome owner per fill occurrence. Those are not "
                "jointly enforced today. Do not manufacture the count with DISTINCT "
                "on lifecycle_id, setup_identity, or outcome-row ids."
            ),
        },
        "entities": _entities(),
        "metrics": _metrics(),
        "timestamps": _timestamps(),
        "counting_unit_examples": _counting_unit_examples(),
        "activation_accounting": _activation_accounting_contract(),
        "outcome_ownership": _outcome_ownership_contract(),
    }


def _entities() -> dict[str, Any]:
    return {
        "OBSERVATION": _entity(
            current_meaning=(
                "One ScannerSymbolResult persisted as one symbol_results row for one "
                "scan_runs.run_id. UNIQUE(run_id, symbol). Display bucket and "
                "rejection_reason are computed at serialize/store time."
            ),
            intended_research_meaning=(
                "A point-in-time scan of one instrument in one run. Not a unique setup "
                "and not a trade."
            ),
            current_authoritative_id="symbol_results.(run_id, symbol)",
            proposed_future_id="observation_id (not persisted; derive from run_id+symbol)",
            unit="symbol observation per scan run",
            owner="app.storage.repositories._symbol_result_record / store_scan_result",
            creation_point="scripts/run_scan.py persist path when --store-scan",
            mutability="insert-once with the scan run; overwritten only if the run row is replaced",
            timestamp_semantics="Inherits scan_runs.timestamp (persist wall-clock UTC), not candle event time",
            relationships="N observations per run (one per symbol). 0..1 setup_candidates child. May attach a lifecycle_id in raw JSON.",
            cardinality="one row per symbol per stored run",
            research_statistics_safe=(
                "Safe as an observation count. Not safe as unique setup, unique plan, or unique trade."
            ),
            producers=(
                "app.storage.repositories._symbol_result_record",
                "app.formatters.scanner_display.build_symbol_display",
            ),
            consumers=(
                "app.research.queries",
                "app.analytics.post_restart_funnel_audit",
                "evidence baseline audit",
            ),
            enforcement="DB UNIQUE(run_id, symbol); tests in tests/test_storage_database.py",
        ),
        "CANDIDATE": _entity(
            current_meaning=(
                "Optional setup_candidates row created when valid_strategy_modes is "
                "non-empty or lifecycle is ACTIONABLE_A_GRADE / A_GRADE_WATCH and a "
                "mode can be resolved. At most one candidate per symbol per run in code."
            ),
            intended_research_meaning=(
                "A structured trade-plan snapshot observed in one run. Not proof of "
                "activation or of an immutable plan."
            ),
            current_authoritative_id="setup_candidates.id (surrogate); no natural unique key on (run_id, symbol)",
            proposed_future_id="Do not promote candidate.id to setup_id",
            unit="candidate row per symbol per run (0..1 by convention)",
            owner="app.storage.repositories._setup_candidate_record",
            creation_point="store_scan_result",
            mutability="insert-once",
            timestamp_semantics="No own timestamp; inherits scan run persist time",
            relationships="Child of scan_runs. Geometry fields default to N/A sentinels.",
            cardinality="0..1 per symbol per run (convention, not UNIQUE constraint)",
            research_statistics_safe=(
                "Safe as candidate-row count. Not unique economic plans. Missing candidate is not zero setups."
            ),
            producers=("app.storage.repositories._setup_candidate_record",),
            consumers=("app.research.queries", "post_restart_funnel_audit"),
            enforcement="code convention; no UNIQUE(run_id, symbol) on setup_candidates",
        ),
        "SETUP": _entity(
            current_meaning=(
                "Informal. Closest persisted field is setup_lifecycle_records.setup_identity, "
                "a pipe-joined geometry string: symbol|mode|direction|entry_low|entry_high|"
                "stop_loss|invalidation_reason. TPs and RR are omitted. Missing values become N/A."
            ),
            intended_research_meaning=(
                "Stable lineage of a market structure idea across scans, independent of "
                "target revisions and public message ids."
            ),
            current_authoritative_id="setup_identity (derived, not unique; still authoritative for current consumers)",
            proposed_future_id="setup_id (P1 parallel foundation; consumers still use setup_identity)",
            unit="geometry identity string; many lifecycle rows may share one",
            owner="app.lifecycle.identity.setup_geometry_identity",
            creation_point="lifecycle create/update in SetupLifecycleService / state_machine._setup_identity",
            mutability="recomputed from observation unless current_state is in PLAN_LOCK_STATES",
            timestamp_semantics="No dedicated setup timestamp; uses lifecycle first_seen_at",
            relationships="Many observations and many lifecycle_ids can share one setup_identity",
            cardinality="not unique; no UNIQUE constraint",
            research_statistics_safe=UNSAFE,
            producers=(
                "app.lifecycle.identity.setup_geometry_identity",
                "app.lifecycle.state_machine._setup_identity",
            ),
            consumers=(
                "setup_lifecycle_records.setup_identity",
                "telegram _lifecycle_setup_delivery_signal_id",
            ),
            enforcement="code + tests; no DB uniqueness",
        ),
        "ECONOMIC PLAN": _entity(
            current_meaning=(
                "Closest object is stored lifecycle geometry (entry/stop/TPs/invalidation) "
                "plus canonical_plan_identity which hashes lifecycle_id and those fields. "
                "Public layer uses a separate mode-neutral economic plan hash."
            ),
            intended_research_meaning=(
                "Frozen prices, direction, instrument, horizon, and exit policy that define "
                "one version of a trade plan."
            ),
            current_authoritative_id=(
                "plan_identity on setup_lifecycle_outcome_progress; public canonical_plan_id is a different namespace"
            ),
            proposed_future_id="migrate outcome consumers from plan_identity to plan_version_id",
            unit="hashed plan including lifecycle_id (so generation is mixed into economics)",
            owner="app.lifecycle.outcome_policy.canonical_plan_identity",
            creation_point="outcome progress upsert during lifecycle apply",
            mutability="hash changes if stored geometry text changes; lookup tries canonical + legacy",
            timestamp_semantics="progress tracking_start_at / first_evaluated_at",
            relationships="UNIQUE(lifecycle_id, plan_identity); one lifecycle may have many plan_identity rows",
            cardinality="1..N plan identities per lifecycle_id",
            research_statistics_safe=UNSAFE,
            producers=("app.lifecycle.outcome_policy.canonical_plan_identity",),
            consumers=("outcomes.evaluate_closed_candle_outcomes", "telegram compatible_plan_identities"),
            enforcement="UNIQUE(lifecycle_id, plan_identity); Decimal normalize on canonical path only, not tick",
        ),
        "PLAN VERSION": _entity(
            current_meaning=(
                "P1 parallel foundation: setup_lifecycle_records.plan_version_id is a SHA-256 "
                "of immutable plan economics (schema, setup_id, entry/stop/TPs, invalidation) "
                "without lifecycle_id. It is not yet used by outcome, public, or Telegram "
                "consumers. Current consumer joins still use plan_identity, which includes "
                "lifecycle_id. TRIGGERED remains outside PLAN_LOCK_STATES, so plan_version_id "
                "is not latched in that state. No exit/fill-policy version exists to include."
            ),
            intended_research_meaning="Immutable snapshot of economics; supersession creates a new version",
            current_authoritative_id=(
                "setup_lifecycle_records.plan_version_id (P1 parallel; not used by outcome/public consumers)"
            ),
            proposed_future_id="plan_version_id as outcome/trade authority after consumer migration",
            unit="hashed plan economics independent of lifecycle_id; NULL when unavailable/legacy",
            owner="app.lifecycle.economic_identity.mint_plan_version_id",
            creation_point="evaluate_lifecycle_transition latch after record construction",
            mutability="latched once in PLAN_LOCK_STATES; later geometry change is invariant_violation, not overwrite",
            timestamp_semantics="No dedicated plan-version timestamp; uses lifecycle first_seen_at / last_transition_at",
            relationships="Many lifecycle_ids may share one setup_id; plan_version_id is per latched economics; no UNIQUE constraint",
            cardinality="0..1 latched plan_version_id per lifecycle row; historical rows remain NULL",
            research_statistics_safe=UNSAFE,
            producers=("app.lifecycle.economic_identity.latch_economic_identities",),
            consumers=(
                "app.analytics.outcome_ownership.project_outcome_ownership "
                "(P3A diagnostic of explicitly supplied records; not a runtime consumer)",
            ),
            enforcement="additive nullable columns; no uniqueness; tests/test_economic_identity.py",
        ),
        "LIFECYCLE": _entity(
            current_meaning=(
                "setup_lifecycle_records row keyed by lifecycle_id (alias setup_generation_id). "
                "Created by new_setup_generation_id: SHA256 of version+symbol+mode+direction+"
                "structural_anchor, or UUID when anchor is N/A. Partial UNIQUE current "
                "(symbol, mode, direction) WHERE is_current=1."
            ),
            intended_research_meaning="Mutable state machine instance attached to one plan version",
            current_authoritative_id="setup_lifecycle_records.lifecycle_id",
            proposed_future_id="keep lifecycle_id; stop treating it as immutable economics",
            unit="lifecycle generation row",
            owner="app.lifecycle.service.SetupLifecycleService",
            creation_point="app.lifecycle.identity.new_setup_generation_id",
            mutability="row is current/archived; current_state overwritten; geometry frozen only in PLAN_LOCK_STATES (TRIGGERED excluded)",
            timestamp_semantics="first_seen_at, last_seen_at, last_transition_at, confirmed_at via now_utc_iso (UTC wall, microseconds stripped)",
            relationships="events append by lifecycle_id; outcomes join by lifecycle_id; public signal_id often equals lifecycle_id",
            cardinality="one current row per (symbol, mode, direction); historical generations retained with is_current=0",
            research_statistics_safe=(
                "Safe as lifecycle-row or event counts. Not unique economic plans or unique trades. "
                "Current-row filter is not as-of reconstruction."
            ),
            producers=("app.lifecycle.identity.new_setup_generation_id", "app.lifecycle.repositories"),
            consumers=(
                "setup_lifecycle_events",
                "setup_outcome_analytics",
                "telegram signal_id",
            ),
            enforcement="PK lifecycle_id; partial unique current triple; tests/test_lifecycle.py",
        ),
        "READINESS": _entity(
            current_meaning=(
                "readiness_score / readiness_label from display/setup-readiness helpers, copied onto "
                "symbol_results and lifecycle records. Overwritten each scan."
            ),
            intended_research_meaning="Non-economic display/priority of how close a setup is to tradable",
            current_authoritative_id="NOT FOUND (score fields only)",
            proposed_future_id="do not use in economic identity",
            unit="integer score / label per observation or lifecycle snapshot",
            owner="app.formatters.scanner_display",
            creation_point="display_fields / lifecycle record update",
            mutability="overwritten",
            timestamp_semantics="scan persist / last_seen_at",
            relationships="Independent of lifecycle_id identity",
            cardinality="per observation / current lifecycle row",
            research_statistics_safe="Safe only as a contemporaneous score snapshot; not a trade unit",
            producers=("app.formatters.scanner_display._setup_readiness",),
            consumers=("symbol_results.readiness_score", "scanner console"),
            enforcement="code; not an identity key",
        ),
        "QUALITY": _entity(
            current_meaning=(
                "candidate_quality_grade, final_quality_grade, setup_quality_score, A-grade "
                "actionability_state. Display/scoring layer, overwritten."
            ),
            intended_research_meaning="Quality and actionability labels, not economics",
            current_authoritative_id="NOT FOUND",
            proposed_future_id="keep separate from setup_id / plan_version_id",
            unit="grade or actionability state per observation/lifecycle snapshot",
            owner="setup quality + actionability producers on ScannerSymbolResult / lifecycle",
            creation_point="scan evaluation then persist/lifecycle update",
            mutability="overwritten",
            timestamp_semantics="scan persist / last_seen_at",
            relationships="actionability_state drives A-grade scan_runs counters",
            cardinality="per observation",
            research_statistics_safe="Safe as snapshot labels. Not unique setups.",
            producers=("scanner quality/actionability path", "lifecycle record update"),
            consumers=("_a_grade_actionability_counts", "public watchlist gates"),
            enforcement="code + tests; not identity",
        ),
        "CONFIRMATION": _entity(
            current_meaning=(
                "confirmation_count and confirmed_at on setup_lifecycle_records. Increments on "
                "consistent countable scans. State CONFIRMED is a lifecycle state, distinct from "
                "ACTIONABLE_A_GRADE. confirmed_setups counts symbols whose lifecycle_current_state "
                "is CONFIRMED at persist time."
            ),
            intended_research_meaning="Structural confirmation of the setup, not an entry fill",
            current_authoritative_id="lifecycle confirmed_at / confirmation_count (mutable snapshot)",
            proposed_future_id="confirmation events should remain non-economic",
            unit="count of confirmation cycles on a lifecycle row; or observation in CONFIRMED",
            owner="app.lifecycle.state_machine",
            creation_point="lifecycle evaluate/update",
            mutability="count and timestamp update; state can leave CONFIRMED",
            timestamp_semantics="confirmed_at is processing/scan clock via now_utc_iso",
            relationships="Must not be equated with ACTIVATION or SIMULATED FILL",
            cardinality="0..N confirmations per lifecycle; 0..1 confirmed_at",
            research_statistics_safe=UNSAFE,
            producers=("app.lifecycle.state_machine",),
            consumers=("scan_runs.confirmed_setups via _lifecycle_state_counts",),
            enforcement="code + lifecycle tests",
        ),
        "ACTIVATION": _entity(
            current_meaning=(
                "P2A splits the previously mixed label. (1) Legacy scan_runs.valid_activations "
                "is len(watch_mode WatchActivation) for watch iterations else DEFAULT 0; a "
                "WatchActivation is a watch-loop alert trigger (trade_idea + valid quality + "
                "display_status valid_setup), not a lifecycle EXECUTING transition. This field "
                "is preserved operationally and is deprecated as an economic research metric. "
                "(2) Authoritative closed-candle entry-activation evidence is "
                "setup_lifecycle_events.reason = ENTRY_ACTIVATED, counted only as event records. "
                "(3) ENTRY_FILL_SIMULATED is a different event-record unit whose reason text "
                "cannot distinguish simulated vs verified fills. Fill-occurrence identity is "
                "unavailable. Audit-reported activation narratives with valid_activations=0 "
                "are consistent with this split when those narratives are lifecycle events "
                "rather than watch alerts."
            ),
            intended_research_meaning="First permitted entry occurrence of a frozen plan version",
            current_authoritative_id=(
                "Watch alerts: scan_runs.valid_activations on is_watch_iteration=1. "
                "Entry activation evidence: setup_lifecycle_events.event_id where reason is "
                "ENTRY_ACTIVATED. No unified activation/fill occurrence id."
            ),
            proposed_future_id="simulated_trade_id / fill occurrence id (later; not P2A)",
            unit="watch alert (legacy counter) vs lifecycle event records (different units)",
            owner="app.watch_mode.build_watch_iteration_summary vs app.lifecycle.outcomes",
            creation_point="watch loop activations list; or evaluate_closed_candle_outcomes entry_activated",
            mutability="scan_runs counter is insert-once per run; lifecycle events append; state continues to mutate",
            timestamp_semantics=(
                "Watch: scan persist / watch completed_at. ENTRY_ACTIVATED event.timestamp is "
                "evaluation/processing time, not candle-close occurrence time."
            ),
            relationships=(
                "Do not infer activation from current_state EXECUTING, confirmed_setups, "
                "Telegram delivery, or reconciled symbols_completed. See activation_accounting."
            ),
            cardinality=(
                "valid_activations defaults 0 on non-watch runs even if lifecycle fills exist; "
                "that default is not a complete economic zero"
            ),
            research_statistics_safe=UNSAFE,
            producers=(
                "app.watch_mode.build_watch_iteration_summary",
                "app.storage.repositories._scan_run_record",
                "app.lifecycle.outcomes.evaluate_closed_candle_outcomes",
                "app.analytics.activation_accounting.project_activation_accounting",
            ),
            consumers=(
                "scan_runs.valid_activations (operational, unchanged)",
                "research.queries valid_activations_from_watch (watch-scoped research)",
                "evidence baseline audit activation_accounting (P2A projection)",
            ),
            enforcement=(
                "Watch counter tests remain; P2A adds isolated research projections and "
                "unavailable occurrence metrics. Lifecycle/fill producers are unchanged."
            ),
        ),
        "SIMULATED FILL": _entity(
            current_meaning=(
                "SetupTransitionReason.ENTRY_FILL_SIMULATED and observation/replay filled flags. "
                "Candle-touch simulation in strategy_replay and lifecycle outcome evaluation. "
                "Not a broker fill."
            ),
            intended_research_meaning="Policy-defined simulated entry under an explicit fill rule",
            current_authoritative_id="NOT FOUND (reason string / boolean flags)",
            proposed_future_id="simulated_trade_id",
            unit="lifecycle event or replay trade row",
            owner="app.lifecycle.outcome_events / app.backtesting.strategy_replay",
            creation_point="closed-candle outcome eval or replay engine",
            mutability="event append; replay_results insert-once",
            timestamp_semantics="candle close / evaluation time mixed; see outcome timestamps",
            relationships="Must not be counted as MANUAL FILL",
            cardinality="may repeat if re-evaluated; replay_results may be empty in runtime DBs",
            research_statistics_safe=UNSAFE,
            producers=(
                "app.lifecycle.state_machine",
                "app.lifecycle.outcome_events",
                "app.backtesting.strategy_replay",
            ),
            consumers=("setup_lifecycle_events.reason", "replay_results.filled"),
            enforcement="code + outcome/replay tests",
        ),
        "MANUAL FILL": _entity(
            current_meaning="NOT FOUND as a distinct identity, table, or type.",
            intended_research_meaning="Operator-reported fill distinct from simulation",
            current_authoritative_id="NOT FOUND",
            proposed_future_id="optional manual_fill_id; do not invent",
            unit="none",
            owner="none",
            creation_point="NOT FOUND",
            mutability="NOT FOUND",
            timestamp_semantics="NOT FOUND",
            relationships="Cannot be inferred from Telegram SENT or TP progress",
            cardinality="NOT FOUND",
            research_statistics_safe=UNAVAILABLE,
            producers=(),
            consumers=(),
            enforcement="absence",
        ),
        "PUBLIC SIGNAL": _entity(
            current_meaning=(
                "Delivery identity: signal_id, public_watchlist_plan_id, event_key, message_hash. "
                "Hard uniqueness: telegram_alert_attempts UNIQUE(signal_id, alert_type) and "
                "public_alert_events UNIQUE(event_key). message_hash is SHA256 of formatted text, "
                "not JSON. Public economic plan ids are mode-neutral and tick-quantized."
            ),
            intended_research_meaning="A published message/event, not a trade",
            current_authoritative_id="public_alert_events.event_key / telegram_alert_attempts.(signal_id, alert_type)",
            proposed_future_id="public_event_id (existing event_key is sufficient if not reused as plan id)",
            unit="delivery attempt or reserved public event",
            owner="app.alerts.telegram_lifecycle / telegram_outbox",
            creation_point="reserve_public_watchlist_event and attempt insert",
            mutability="dedupe_status/delivery_state mutate; event_key unique",
            timestamp_semantics="attempted_at / reserved_at / sent_at wall-clock delivery times",
            relationships="May reference lifecycle_id; historical SENT rows store geometry snapshot fields",
            cardinality="many events per plan; one event_key",
            research_statistics_safe="Safe as public-event counts. Not trades. Identity mismatch is attribution, not missing messages.",
            producers=(
                "app.alerts.telegram_lifecycle",
                "app.alerts.public_identity.canonical_public_event_key",
            ),
            consumers=("telegram_outbox", "public_alert_funnel"),
            enforcement="UNIQUE constraints + reservation path; tests for public delivery",
        ),
        "OUTCOME": _entity(
            current_meaning=(
                "Two tables: setup_lifecycle_outcome_progress UNIQUE(lifecycle_id, plan_identity) "
                "with terminal_outcome and TP/SL timestamps; setup_outcome_analytics UNIQUE("
                "lifecycle_id, final_outcome). Multiple analytics rows per lifecycle_id are allowed "
                "for different final_outcome values. TP progress is not a completed trade."
            ),
            intended_research_meaning="One authoritative result per immutable plan version / fill",
            current_authoritative_id="NOT FOUND as a single owner; dual tables keyed by lifecycle_id",
            proposed_future_id="authoritative outcome per plan_version_id (later phase; P3A is diagnostic only)",
            unit="progress row or analytics row",
            owner="app.lifecycle.outcomes / service._outcome_analytics_record",
            creation_point="evaluate_closed_candle_outcomes; terminal analytics upsert",
            mutability="progress upserted; analytics unique per (lifecycle_id, final_outcome)",
            timestamp_semantics="entry_at/tp*_at/stop_at are candle event times when set; evaluated_at is evaluation pass / scan now",
            relationships="Naive join of analytics to progress on lifecycle_id fans out",
            cardinality="1..N progress rows per lifecycle; 1..N analytics rows per lifecycle",
            research_statistics_safe=UNSAFE,
            producers=(
                "app.lifecycle.outcomes.evaluate_closed_candle_outcomes",
                "app.lifecycle.service._outcome_analytics_record",
            ),
            consumers=(
                "lifecycle hygiene",
                "telegram outcome matching; research.queries does not read these tables",
                "app.analytics.outcome_ownership.project_outcome_ownership (supplied records only)",
            ),
            enforcement="UNIQUE keys as above; join fan-out is a known limitation",
        ),
        "REPLAY RECORD": _entity(
            current_meaning=(
                "replay_results row with setup_fingerprint = SHA256 of JSON {symbol, mode, "
                "direction, detected_at_index, entry, stop, condition_key}. Separate from "
                "lifecycle setup_identity. Runtime samples may have zero rows."
            ),
            intended_research_meaning="Causal replay of a frozen plan against historical candles",
            current_authoritative_id="replay_results.(run_id, setup_fingerprint) — not unique declared beyond surrogate id",
            proposed_future_id="replay attached to plan_version_id",
            unit="replay trade result row",
            owner="app.storage.repositories._replay_result_records",
            creation_point="store_scan_result when ReplaySummary provided",
            mutability="insert-once",
            timestamp_semantics="inherits scan run persist time; replay internals use candle indexes",
            relationships="Child of scan_runs; not joined to lifecycle_id",
            cardinality="0..N per run; empty table is not a zero expectancy",
            research_statistics_safe=(
                "Safe as replay-row presence. Empty table means unavailable replay evidence, not 0% win rate."
            ),
            producers=("app.storage.repositories._setup_fingerprint", "app.backtesting.strategy_replay"),
            consumers=("app.research.queries", "replay dataset export"),
            enforcement="FK run_id; tests for storage; emptiness is an evidence status",
        ),
    }


def _metrics() -> dict[str, Any]:
    persist = "scan_runs column via _scan_run_record / store_scan_result (insert-once)"
    consumers = (
        "list_scan_history",
        "app.research.queries",
        "app.analytics.post_restart_funnel_audit",
        "app.formatters.scanner_console",
    )
    return {
        "symbols_requested": _metric(
            producer="app.storage.repositories._scan_summary_metadata",
            update_path="len(resume_metadata.watchlist_symbols) else len(config.symbols); watch uses WatchIterationMetadata.symbols_requested",
            persistence=persist,
            inclusion_rules="Requested universe size for the run, not completed scans",
            counting_unit="integer counter stored per scan run (typically symbols)",
            scope="run",
            repeated_observations_counted="each run counts its request list once; summing across runs recounts symbols",
            unique_economic_plans_counted="no",
            snapshot_time="scan persist",
            relative_to_lifecycle="independent of lifecycle processing except that persist happens after the scan result exists",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="not a partition with queued/completed/scanned",
            research_funnel_suitable="workload size only; not a quality funnel",
            trust_status="research-safe as requested-count per run",
            limitations="does not prove freshness or uniqueness of instruments across days",
        ),
        "symbols_queued": _metric(
            producer="app.storage.repositories._scan_summary_metadata",
            update_path="len(symbols_to_scan) metadata else config length; watch uses len(queued_symbols)",
            persistence=persist,
            inclusion_rules="Queue size, which may differ from requested",
            counting_unit="integer counter per run",
            scope="run",
            repeated_observations_counted="per run",
            unique_economic_plans_counted="no",
            snapshot_time="scan persist",
            relative_to_lifecycle="before/independent of lifecycle counts",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="may equal requested; not a subset proof",
            research_funnel_suitable="workload only",
            trust_status="research-safe as queued-count per run",
            limitations="watch vs normal paths use different sources",
        ),
        "symbols_completed": _metric(
            producer="app.storage.repositories._scan_summary_metadata",
            update_path="Always runtime_stats.completed_symbols (evaluated+rejected). Watch metadata.symbols_completed is ignored for this column.",
            persistence=persist,
            inclusion_rules="Runtime completed_symbols only",
            counting_unit="integer counter per run",
            scope="run",
            repeated_observations_counted="per run",
            unique_economic_plans_counted="no",
            snapshot_time="scan persist",
            relative_to_lifecycle="runtime completion, not lifecycle success",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="does not include timed_out/errored the way symbols_scanned may",
            research_funnel_suitable="workload completion only; not activation",
            trust_status="research-safe as runtime completed-count per run",
            limitations="reconciled requested/queued/completed/scanned does not imply valid activations",
        ),
        "symbols_scanned": _metric(
            producer="ScannerRunResult.scanned_symbols (scanner_runner)",
            update_path="_scan_run_record copies result.scanned_symbols; counts results with iteration_outcome != not_run",
            persistence=persist,
            inclusion_rules="May include timed_out/errored unlike symbols_completed",
            counting_unit="integer counter per run",
            scope="run",
            repeated_observations_counted="per run",
            unique_economic_plans_counted="no",
            snapshot_time="scan persist",
            relative_to_lifecycle="scanner runtime, independent of lifecycle",
            consumers=consumers,
            default_null_zero="NOT NULL",
            categories_overlap="not equal to symbols_completed by definition",
            research_funnel_suitable="workload only",
            trust_status="research-safe as scanned-count per run",
            limitations="unit is symbol results, not unique setups",
        ),
        "total_valid_setups": _metric(
            producer="app.storage.repositories._bucket_counts",
            update_path="count raw_payload results with display_bucket == 'valid' (from display_status valid_setup)",
            persistence=persist,
            inclusion_rules="display_bucket valid only; not lifecycle CONFIRMED; not unique plans",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes — same symbol valid on every run is counted every run",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time display fields",
            relative_to_lifecycle="display layer; lifecycle may also be present on the same payload",
            consumers=consumers,
            default_null_zero="NOT NULL",
            categories_overlap="disjoint from near_misses/rejected/data_issues buckets in _bucket_counts, but not from A-grade counters",
            research_funnel_suitable="observation funnel only; not unique setup funnel",
            trust_status="research-safe as valid-observation count per run",
            limitations="display-derived; summing across runs overcounts unique setups",
        ),
        "near_misses": _metric(
            producer="_bucket_counts",
            update_path="display_bucket == 'near_miss'",
            persistence=persist,
            inclusion_rules="display near_miss observations",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time display",
            relative_to_lifecycle="display layer",
            consumers=consumers,
            default_null_zero="NOT NULL",
            categories_overlap="disjoint from valid/no_setup/data_issue in _bucket_counts",
            research_funnel_suitable="observation funnel only",
            trust_status="research-safe as near-miss observation count per run",
            limitations="not unique setups",
        ),
        "rejected": _metric(
            producer="_bucket_counts",
            update_path="display_bucket == 'no_setup' — not all pipeline rejections; not watch rejected_no_edge",
            persistence=persist,
            inclusion_rules="no_setup display bucket only",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time display",
            relative_to_lifecycle="display layer",
            consumers=consumers,
            default_null_zero="NOT NULL",
            categories_overlap="name collides with watch rejected_no_edge; different column",
            research_funnel_suitable="observation funnel only, with naming ambiguity",
            trust_status=UNSAFE,
            limitations="label 'rejected' means display no_setup, not rejected setups in general",
        ),
        "data_issues": _metric(
            producer="_scan_run_record",
            update_path="watch: WatchIterationMetadata.data_issues; else _bucket_counts data_issue. May differ from len(data_issues_json).",
            persistence=persist,
            inclusion_rules="watch vs display-bucket paths differ",
            counting_unit="symbol observation (watch also counts scan_error / DATA ISSUE label)",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time",
            relative_to_lifecycle="independent",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="can disagree with data_issues_json length",
            research_funnel_suitable="limited; path-dependent definition",
            trust_status=UNSAFE,
            limitations="two producers; do not treat missing as zero issues in an audit of another table",
        ),
        "valid_activations": _metric(
            producer="app.watch_mode.build_watch_iteration_summary",
            update_path="len(activations); stored only when watch_iteration metadata is passed; else 0",
            persistence=persist,
            inclusion_rules="WatchActivation list: prior state allows activation, current_result_is_valid_activation (trade_idea, quality, display_status==valid_setup, optional portfolio). NOT lifecycle ENTRY_ACTIVATED or ENTRY_FILL_SIMULATED.",
            counting_unit="watch-loop alert trigger per iteration",
            scope="watch iteration / run",
            repeated_observations_counted="per watch iteration that stored the summary",
            unique_economic_plans_counted="no",
            snapshot_time="watch iteration persist",
            relative_to_lifecycle="not the lifecycle fill or entry-activation counter",
            consumers=consumers + ("app.research.reports valid_activations_from_watch",),
            default_null_zero="NOT NULL DEFAULT 0 — zero on non-watch runs is default, not proof of no fills",
            categories_overlap="orthogonal to confirmed_setups and actionable_a_grade_setups",
            research_funnel_suitable="no — cannot be used as entry funnel",
            trust_status=UNSAFE,
            limitations=(
                "P2A: deprecated as an economic research metric. Physical field and operational "
                "consumers are unchanged. Mixed-window sums that include non-watch default zeros "
                "are unsafe. Use activation_accounting.watch_alert_activations for the watch-scoped "
                "counter and entry_activated_event_records for closed-candle activation evidence."
            ),
        ),
        "still_watching": _metric(
            producer="app.watch_mode.build_watch_iteration_summary",
            update_path="public_watchlist_eligible + has_valid_trade_map, not in activated set, not data_issue",
            persistence=persist,
            inclusion_rules="watch only; else 0",
            counting_unit="symbol in one watch iteration",
            scope="watch iteration / run",
            repeated_observations_counted="yes across iterations",
            unique_economic_plans_counted="no",
            snapshot_time="watch iteration persist",
            relative_to_lifecycle="uses public eligibility helpers, not lifecycle state exclusively",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0 on non-watch",
            categories_overlap="excludes activated symbols in that iteration",
            research_funnel_suitable="watch snapshot only",
            trust_status="research-safe as watch still-watching observation count",
            limitations="zero on normal scans is default",
        ),
        "actionable_setups": _metric(
            producer="_scan_run_record",
            update_path="Alias of actionable_a_grade_setups (same assignment)",
            persistence=persist,
            inclusion_rules="identical to actionable_a_grade_setups, not 'any actionable'",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time actionability/lifecycle fields",
            relative_to_lifecycle="uses actionability_state after lifecycle fields are on the payload",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="equal to actionable_a_grade_setups by assignment",
            research_funnel_suitable="no — name overstates the unit",
            trust_status=UNSAFE,
            limitations="not a broader actionable count",
        ),
        "confirmed_setups": _metric(
            producer="_lifecycle_state_counts",
            update_path="count payload/results whose lifecycle current_state is CONFIRMED",
            persistence=persist,
            inclusion_rules="CONFIRMED state only; ACTIONABLE_A_GRADE is not included",
            counting_unit="symbol observation in this run with CONFIRMED lifecycle",
            scope="run / observation / lifecycle snapshot",
            repeated_observations_counted=(
                "yes; current-state snapshot per run. Prospective CONFIRMED↔ACTIONABLE_A_GRADE "
                "quality/actionability oscillation is guarded and no longer moves this count; "
                "historical event rows may still oscillate"
            ),
            unique_economic_plans_counted="no",
            snapshot_time="persist-time lifecycle snapshot",
            relative_to_lifecycle="after lifecycle apply on the scan result",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="can overlap A-grade counters if states and actionability disagree; typically different labels",
            research_funnel_suitable="no — snapshot of a mutable state",
            trust_status=UNSAFE,
            limitations=(
                "does not reconstruct history; still a mutable current-state snapshot, not unique "
                "confirmations. Historical CONFIRMED↔ACTIONABLE_A_GRADE rows are not rewritten"
            ),
        ),
        "actionable_a_grade_setups": _metric(
            producer="_a_grade_actionability_counts",
            update_path="actionability_state in {A_GRADE_ACTIONABLE, A_GRADE_ACTIONABLE_TARGET_CAUTION} or fallback ACTIONABLE_A_GRADE + A-grade candidate",
            persistence=persist,
            inclusion_rules="A-grade actionable including target-caution variant",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time",
            relative_to_lifecycle="uses actionability and lifecycle fields on the payload",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="includes target-caution; overlaps soft_target_warnings",
            research_funnel_suitable="observation snapshot only",
            trust_status=UNSAFE,
            limitations="not unique plans; caution included",
        ),
        "candidate_a_grade_setups": _metric(
            producer="_a_grade_actionability_counts",
            update_path="A-/A/A+ candidate grade or actionability_state startswith A_GRADE_",
            persistence=persist,
            inclusion_rules="broader than actionable; includes blocked A-grade states",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time",
            relative_to_lifecycle="scan-time grades/actionability",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="superset of blocked and actionable A-grade observations",
            research_funnel_suitable="not a disjoint funnel step",
            trust_status="research-safe as A-grade-candidate observation count per run",
            limitations="can exceed actionable; not unique setups",
        ),
        "blocked_a_grade_by_scoring": _metric(
            producer="_a_grade_actionability_counts",
            update_path="actionability_state == A_GRADE_BLOCKED_BY_SCORING",
            persistence=persist,
            inclusion_rules="that state only",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time",
            relative_to_lifecycle="actionability snapshot",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="also counted in candidate_a_grade_setups; disjoint among blocked_by_* states",
            research_funnel_suitable="blocker snapshot only",
            trust_status="research-safe as observation blocker count per run",
            limitations="not unique plans",
        ),
        "blocked_a_grade_by_target": _metric(
            producer="_a_grade_actionability_counts",
            update_path="actionability_state == A_GRADE_BLOCKED_BY_TARGET",
            persistence=persist,
            inclusion_rules="that state only",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time",
            relative_to_lifecycle="actionability snapshot",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="also increments fatal_target_blocks when state maps to a_grade_blocked_by_target",
            research_funnel_suitable="blocker snapshot only",
            trust_status=UNSAFE,
            limitations="overlaps fatal_target_blocks by construction",
        ),
        "blocked_a_grade_by_entry_window": _metric(
            producer="_a_grade_actionability_counts",
            update_path="actionability_state == A_GRADE_BLOCKED_BY_ENTRY_WINDOW",
            persistence=persist,
            inclusion_rules="that state only",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time",
            relative_to_lifecycle="actionability snapshot",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="also in candidate_a_grade_setups",
            research_funnel_suitable="blocker snapshot only",
            trust_status="research-safe as observation blocker count per run",
            limitations="not unique plans",
        ),
        "blocked_a_grade_by_trust": _metric(
            producer="_a_grade_actionability_counts",
            update_path="actionability_state == A_GRADE_BLOCKED_BY_TRUST",
            persistence=persist,
            inclusion_rules="that state only",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time",
            relative_to_lifecycle="actionability snapshot",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="also in candidate_a_grade_setups",
            research_funnel_suitable="blocker snapshot only",
            trust_status="research-safe as observation blocker count per run",
            limitations="not unique plans",
        ),
        "fatal_target_blocks": _metric(
            producer="_a_grade_actionability_counts",
            update_path="target_failure_severity == fatal_target_failure OR actionability maps to a_grade_blocked_by_target",
            persistence=persist,
            inclusion_rules="severity or blocked-by-target state",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time",
            relative_to_lifecycle="target diagnostics + actionability",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="overlaps blocked_a_grade_by_target",
            research_funnel_suitable="no — not disjoint",
            trust_status=UNSAFE,
            limitations="double-count risk with blocked_a_grade_by_target; TARGET_INSIDE_CHOP saturation is a separate audit finding",
        ),
        "soft_target_warnings": _metric(
            producer="_a_grade_actionability_counts",
            update_path="severity in {soft_target_warning, target_caution_actionable} OR state a_grade_actionable_target_caution",
            persistence=persist,
            inclusion_rules="soft/caution severity or caution actionable state",
            counting_unit="symbol observation in this run",
            scope="run / observation",
            repeated_observations_counted="yes",
            unique_economic_plans_counted="no",
            snapshot_time="persist-time",
            relative_to_lifecycle="target diagnostics + actionability",
            consumers=consumers,
            default_null_zero="NOT NULL DEFAULT 0",
            categories_overlap="overlaps actionable_a_grade_setups when caution is still actionable",
            research_funnel_suitable="no — not disjoint",
            trust_status=UNSAFE,
            limitations="caution can be counted as both warning and actionable",
        ),
    }


def _timestamps() -> dict[str, Any]:
    return {
        "source_event_time": _timestamp(
            existing_field="NOT FOUND as a scan-level field; verified equivalents: candle timestamp, microstructure event_time",
            producer="market-data / candle objects; app.microstructure.order_book event_time",
            meaning="exchange or candle occurrence time",
            format_timezone="varies; microstructure UTC-normalized",
            null_semantics="missing candles are not invented",
            event_or_wall="event time",
            as_of_suitable="candle timestamps can bound closed bars; not sufficient alone to prove information availability",
            missing_and_future="need an explicit source_event_time on observations without replacing now()",
        ),
        "source_receipt_time": _timestamp(
            existing_field="NOT FOUND as a named scan field; verified equivalent: microstructure received_at",
            producer="order-book ingest received_at",
            meaning="local receipt/processing of a market event",
            format_timezone="wall-clock at receive, UTC when normalized",
            null_semantics="absent on most scanner symbol results",
            event_or_wall="receipt / wall-clock",
            as_of_suitable="required for causal availability; currently missing on the scan evidence path",
            missing_and_future="do not backfill with today's clock",
        ),
        "feature_ready_time": _timestamp(
            existing_field="NOT FOUND",
            producer="NOT FOUND",
            meaning="when derived features were available to the decision",
            format_timezone="NOT FOUND",
            null_semantics="NOT FOUND",
            event_or_wall="NOT FOUND",
            as_of_suitable="no",
            missing_and_future="required for causal replay; not added in this phase",
        ),
        "scan_time": _timestamp(
            existing_field="scan_runs.timestamp; watch started_at/completed_at; manifest timestamp",
            producer="store_scan_result uses datetime.now(timezone.utc).isoformat(timespec='seconds'); watch uses now_utc_iso()",
            meaning="persist or iteration wall-clock, not necessarily scan start or candle event",
            format_timezone="ISO-8601 UTC offset (+00:00)",
            null_semantics="timestamp NOT NULL on scan_runs; started_at null for non-watch",
            event_or_wall="wall-clock processing/persist time",
            as_of_suitable="weak; labels a persist window, not information availability",
            missing_and_future="keep distinct from decision_timestamp and source_event_time",
        ),
        "decision_time": _timestamp(
            existing_field="ScannerRunConfig.decision_timestamp",
            producer="scanner_runner resolves clock() UTC when missing; drives closed_candles_as_of",
            meaning="as-of clock for market context and candle finality",
            format_timezone="aware UTC datetime / ISO",
            null_semantics="resolved before scan work in runner",
            event_or_wall="decision / as-of processing clock",
            as_of_suitable="best current causal replay anchor, still not a receipt time",
            missing_and_future="persist on scan_runs would require a migration or JSON sidecar; not added as a column in this phase",
        ),
        "lifecycle_event_time": _timestamp(
            existing_field="setup_lifecycle_events.timestamp; record first_seen_at/last_seen_at/last_transition_at/confirmed_at",
            producer="app.lifecycle.state_machine.now_utc_iso unless outcome path passes evaluation time",
            meaning="mix of scan wall-clock and some candle-close times in notes",
            format_timezone="ISO UTC, microseconds stripped",
            null_semantics="N/A sentinels on some fields; event timestamp NOT NULL",
            event_or_wall="usually wall-clock processing; some outcome notes use candle time",
            as_of_suitable="event log is append-only but not a proven as-of snapshot of the record",
            missing_and_future="do not treat last_seen_at filter as reconstruction at cutoff",
        ),
        "public_available_time": _timestamp(
            existing_field="NOT FOUND; closest: reserved_at, attempted_at, sent_at, completed_at",
            producer="telegram_lifecycle / outbox",
            meaning="delivery reservation or send wall-clock",
            format_timezone="ISO-ish text; attempted_at may be N/A",
            null_semantics="sent_at null until sent; N/A sentinels",
            event_or_wall="delivery wall-clock",
            as_of_suitable="proves delivery attempt time, not that the plan was economically frozen",
            missing_and_future="keep labeled as delivery time",
        ),
        "outcome_evaluation_time": _timestamp(
            existing_field="first_evaluated_at, last_evaluated_at; milestone entry_at/tp*_at/stop_at/outcome_at",
            producer="app.lifecycle.outcomes",
            meaning="milestones are candle-close event times when set; evaluated_at is the evaluation pass clock",
            format_timezone="ISO UTC when written by app; SQLite CURRENT_TIMESTAMP naive UTC on created_at/updated_at",
            null_semantics="NULL until observed; N/A terminal_outcome until set",
            event_or_wall="mixed event and evaluation wall-clock",
            as_of_suitable="milestones can be event-time; evaluated_at is not information-availability",
            missing_and_future="do not count TP timestamp rows as unique trades",
        ),
    }


def _counting_unit_examples() -> dict[str, Any]:
    return {
        "description": (
            "Fixture-oriented illustrations of units. They do not change identity generation."
        ),
        "repeated_observations": {
            "unit": "observation",
            "example": "BTCUSDT valid in two scan_runs => observation_count=2, not two trades",
        },
        "candidate_versus_observation": {
            "unit": "candidate row vs symbol_results row",
            "example": "A run may have 3 symbol_results and 1 setup_candidates row",
        },
        "reused_setup_identity": {
            "unit": "setup_identity string vs lifecycle_id",
            "example": "Two lifecycle_ids can share symbol|mode|direction|entry|stop|invalidation",
        },
        "mode_split": {
            "unit": "lifecycle current generation is mode-partitioned; public plan id is mode-neutral",
            "example": "BTCUSDT long scalp and swing are two lifecycle currents, possibly one public economic id",
        },
        "outcome_fanout": {
            "unit": "outcome row vs lifecycle vs trade",
            "example": "Three progress rows for one lifecycle_id are not three completed trades",
        },
        "plan_outcome_versus_trade_occurrence": {
            "unit": "verified plan_version_id inventory vs unavailable fill/trade occurrence",
            "example": (
                "P3A may count a verified plan identity once across lifecycle generations "
                "and may interpret one coherent evaluation as a plan-level simulation. "
                "That interpretation is not a unique trade. Fill-occurrence identity remains unavailable."
            ),
        },
        "boundary_window": {
            "unit": "half-open [start, cutoff)",
            "example": "A scan_runs.timestamp equal to cutoff is excluded",
        },
        "trigger_touch_activation_fill": {
            "unit": "distinct evidence kinds; do not sum",
            "example": (
                "TRIGGERED lifecycle event is a trigger observation. "
                "ENTRY_ZONE_TOUCHED is a zone-touch event record. "
                "ENTRY_ACTIVATED is closed-candle activation evidence (event record). "
                "ENTRY_FILL_SIMULATED is a later state-progression event whose text cannot "
                "prove a verified manual fill. Watch valid_activations=1 is a watch alert, "
                "not any of the above. None of these is a unique trade."
            ),
        },
    }


def _activation_accounting_contract() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "accounting_version": "cci-activation-accounting-v1",
        "schema_version_unchanged": True,
        "feeds_operational_decisions": False,
        "prospective_applicability": (
            "Projections apply to committed rows in an explicit [start, cutoff) window. "
            "Historical scan_runs.valid_activations values are not rewritten. "
            "Missing P1 identities stay missing. No backfill."
        ),
        "legacy_valid_activations": {
            "name": "valid_activations",
            "meaning": "Watch-loop WatchActivation alert count stored on scan_runs",
            "producer": "app.watch_mode.build_watch_iteration_summary then _scan_run_record",
            "authoritative_predicate": (
                "watch_iteration is not None: len(WatchActivation); else INTEGER DEFAULT 0"
            ),
            "source_identity": "scan_runs.run_id",
            "event_time": "scan persist / watch completed_at, not candle close",
            "counting_unit": "watch-loop alert per stored watch iteration",
            "uniqueness_rule": "per scan_runs row; repeated watch iterations recount",
            "aggregation_grain": "watch-scoped rows only for research; mixed sums are unsafe",
            "run_iteration_attribution": "the stored scan_runs row",
            "evidence_window_watermark": "scan_runs.timestamp in [start, cutoff)",
            "completion_criteria": "insert-once scan persist succeeded",
            "null_zero_semantics": (
                "Non-watch 0 is default, not observed inactivity. Missing column is unavailable."
            ),
            "retry_replay_behavior": "insert-once; re-reading the row does not increment",
            "persistence": "scan_runs.valid_activations INTEGER NOT NULL DEFAULT 0",
            "consumers": (
                "watch console, research watch summaries, post-restart funnel raw extract"
            ),
            "contract_version": CONTRACT_VERSION,
            "prospective_applicability_boundary": "unchanged operational writes from P2A onward",
            "legacy_interpretation": (
                "Deprecated as economic activation/fill count. Keep the physical field."
            ),
        },
        "watch_alert_activations": {
            "name": "watch_alert_activations",
            "meaning": "Watch-scoped sum of stored valid_activations",
            "producer": "app.analytics.activation_accounting.project_activation_accounting",
            "authoritative_predicate": "is_watch_iteration=1",
            "source_identity": "scan_runs.run_id",
            "event_time": "scan persist time",
            "counting_unit": "watch-loop WatchActivation alert",
            "uniqueness_rule": "per watch-iteration row",
            "aggregation_grain": "persist-window sum of watch rows",
            "run_iteration_attribution": "the watch-iteration scan_runs row",
            "evidence_window_watermark": "scan_runs.timestamp in [start, cutoff) and is_watch_iteration=1",
            "completion_criteria": (
                "is_watch_iteration column present and at least one watch row in window"
            ),
            "null_zero_semantics": (
                "No watch rows or missing columns => unavailable, not zero. "
                "Watch rows all storing 0 => complete supported zero of watch alerts."
            ),
            "retry_replay_behavior": "deterministic projection; does not rewrite source rows",
            "persistence": "derived; no new column",
            "consumers": "evidence baseline audit activation_accounting only",
            "contract_version": CONTRACT_VERSION,
            "prospective_applicability_boundary": "P2A+ research projection",
            "legacy_interpretation": "replaces mixed valid_activations sums for research",
        },
        "entry_activated_event_records": {
            "name": "entry_activated_event_records",
            "meaning": "Closed-candle entry activation evidence records",
            "producer": "app.lifecycle.outcomes.evaluate_closed_candle_outcomes",
            "authoritative_predicate": "reason == ENTRY_ACTIVATED enum value",
            "source_identity": "setup_lifecycle_events.event_id",
            "event_time": "event.timestamp = evaluated_at (processing time)",
            "counting_unit": "event record",
            "uniqueness_rule": "event_id; not fill-occurrence identity",
            "aggregation_grain": "processing-time window",
            "run_iteration_attribution": (
                "event.scan_run_id when present; otherwise lifecycle-event measurement only"
            ),
            "evidence_window_watermark": "setup_lifecycle_events.timestamp in [start, cutoff)",
            "completion_criteria": "events table and reason column present",
            "null_zero_semantics": "missing table/column => unavailable; complete window with no matches => 0",
            "retry_replay_behavior": "append-only events; projection re-reads the same rows",
            "persistence": "existing setup_lifecycle_events",
            "consumers": "evidence baseline audit activation_accounting only",
            "contract_version": CONTRACT_VERSION,
            "prospective_applicability_boundary": "P2A+ research projection",
            "legacy_interpretation": "not represented by scan_runs.valid_activations",
        },
        "entry_zone_touched_event_records": {
            "name": "entry_zone_touched_event_records",
            "meaning": "Lifecycle zone-touch event records",
            "producer": "app.lifecycle.state_machine / outcome_events.advance_to_managing",
            "authoritative_predicate": "reason == ENTRY_ZONE_TOUCHED enum value",
            "source_identity": "setup_lifecycle_events.event_id",
            "event_time": "event.timestamp processing time",
            "counting_unit": "event record",
            "uniqueness_rule": "event_id; a touch is not an activation or fill",
            "aggregation_grain": "processing-time window",
            "run_iteration_attribution": "event.scan_run_id when present",
            "evidence_window_watermark": "setup_lifecycle_events.timestamp in [start, cutoff)",
            "completion_criteria": "events table and reason column present",
            "null_zero_semantics": "missing table/column => unavailable; no matches => complete 0",
            "retry_replay_behavior": "append-only events; projection re-reads",
            "persistence": "existing setup_lifecycle_events",
            "consumers": "evidence baseline audit activation_accounting only",
            "contract_version": CONTRACT_VERSION,
            "prospective_applicability_boundary": "P2A+ research projection",
            "legacy_interpretation": "not a valid_activations increment",
        },
        "entry_fill_simulated_event_records": {
            "name": "entry_fill_simulated_event_records",
            "meaning": "ENTRY_FILL_SIMULATED event records; simulated vs confirmed indistinguishable",
            "producer": "app.lifecycle.outcome_events.advance_to_managing / state_machine MANAGING reason",
            "authoritative_predicate": "reason == ENTRY_FILL_SIMULATED enum value",
            "source_identity": "setup_lifecycle_events.event_id",
            "event_time": "event.timestamp processing time",
            "counting_unit": "event record",
            "uniqueness_rule": "event_id; not a verified manual fill and not a unique trade",
            "aggregation_grain": "processing-time window",
            "run_iteration_attribution": "event.scan_run_id when present",
            "evidence_window_watermark": "setup_lifecycle_events.timestamp in [start, cutoff)",
            "completion_criteria": "events table and reason column present",
            "null_zero_semantics": "missing table/column => unavailable; no matches => complete 0",
            "retry_replay_behavior": "append-only events; projection re-reads",
            "persistence": "existing setup_lifecycle_events",
            "consumers": "evidence baseline audit activation_accounting only",
            "contract_version": CONTRACT_VERSION,
            "prospective_applicability_boundary": "P2A+ research projection",
            "legacy_interpretation": (
                "Preserve the structured ambiguity. Do not mint two fill classes from the reason text."
            ),
        },
        "unsupported_occurrence_metrics": {
            "fill_occurrence_count": UNAVAILABLE,
            "manual_fill_count": UNAVAILABLE,
            "unique_activation_occurrence_count": UNAVAILABLE,
        },
    }


def _outcome_ownership_contract() -> dict[str, Any]:
    return {
        "contract_version": CONTRACT_VERSION,
        "ownership_version": "cci-outcome-ownership-v1",
        "schema_version_unchanged": True,
        "feeds_operational_decisions": False,
        "canonical_outcome_per_plan_version": {
            "established": False,
            "status": "unproven",
            "current_owner": "setup_lifecycle_outcome_progress UNIQUE(lifecycle_id, plan_identity)",
            "plan_version_id_at_outcome_write_boundary": "absent",
            "reason": (
                "evaluate_closed_candle_outcomes persists progress by plan_identity, which "
                "hashes lifecycle_id plus geometry. plan_version_id is latched only on "
                "setup_lifecycle_records and is not written onto progress, events, or analytics."
            ),
        },
        "source_evidence_row": (
            "One supplied record from setup_lifecycle_records, setup_lifecycle_outcome_progress, "
            "setup_lifecycle_events, or setup_outcome_analytics, identified by its table and "
            "physical key. Exact duplicate payloads collapse; contradictory payloads for the "
            "same physical key remain a conflict."
        ),
        "verified_immutable_plan_identity": (
            "A plan_version_id that remints from the supplied lifecycle snapshot economics "
            "and, for progress rows, whose plan_identity matches compatible_plan_identities "
            "of that same snapshot. Missing/null/legacy ids stay unavailable."
        ),
        "plan_level_outcome": (
            "A diagnostic interpretation of one coherent evaluation context for a verified "
            "plan_version_id. Not a persisted canonical owner and not a trade."
        ),
        "fill_trade_occurrence_outcome": (
            "Unavailable. ENTRY_ACTIVATED, ENTRY_FILL_SIMULATED, watch alerts, public delivery, "
            "and generic activation labels are not fill-occurrence identity."
        ),
        "append_only_versus_canonical_projection": (
            "Events are append-only. Progress is a mutable current projection per "
            "(lifecycle_id, plan_identity). Analytics is a lifecycle-terminal snapshot that "
            "can store COOLDOWN as final_outcome without replacing an economic terminal. "
            "P3A does not persist or register a canonical plan-outcome projection. Terminal "
            "stability on progress is producer early-return once terminal_outcome is set; "
            "later COOLDOWN/ARCHIVED is successor lifecycle, not economic supersession. "
            "No correction protocol is invented."
        ),
        "authority_rules": {
            "not_used": (
                "updated_at, MAX, latest-row-only, first-terminal-wins, DISTINCT, "
                "table-name precedence"
            ),
            "used": (
                "producer unique keys, reminted P1 identity, compatible plan_identity match, "
                "monotonic non-null milestone timestamps on the same physical progress key"
            ),
        },
        "unresolved_ownership_cases": (
            "legacy null plan_version_id; TRIGGERED unlocked geometry; "
            "plan_version_invariant_violation; historical progress whose plan_identity does "
            "not match the supplied snapshot; multiple generations/windows for one plan; "
            "replay versus live namespaces; missing tracking_start_at"
        ),
        "prospective_applicability": (
            "Diagnostic only. Applies to explicitly supplied records. Historical rows are "
            "not rewritten. Missing P1 identities stay missing. No backfill."
        ),
        "consumers": "tests and fixture-derived synthetic report only",
    }


def evidence_contract_payload() -> dict[str, Any]:
    contract = build_evidence_contract()
    missing_entities = [name for name in _REQUIRED_ENTITIES if name not in contract["entities"]]
    missing_metrics = [name for name in _REQUIRED_METRICS if name not in contract["metrics"]]
    missing_timestamps = [name for name in _REQUIRED_TIMESTAMPS if name not in contract["timestamps"]]
    if missing_entities or missing_metrics or missing_timestamps:
        raise RuntimeError(
            "evidence contract missing required keys: "
            f"entities={missing_entities} metrics={missing_metrics} timestamps={missing_timestamps}"
        )
    return contract


def dumps_evidence_contract() -> str:
    """Deterministic JSON serialization of the contract."""

    return json.dumps(
        evidence_contract_payload(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )


def required_entity_names() -> tuple[str, ...]:
    return _REQUIRED_ENTITIES


def required_metric_names() -> tuple[str, ...]:
    return _REQUIRED_METRICS


def required_timestamp_names() -> tuple[str, ...]:
    return _REQUIRED_TIMESTAMPS


__all__ = [
    "CONTRACT_VERSION",
    "UNAVAILABLE",
    "UNSAFE",
    "build_evidence_contract",
    "dumps_evidence_contract",
    "evidence_contract_payload",
    "required_entity_names",
    "required_metric_names",
    "required_timestamp_names",
]
