from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WatchIterationMetadata:
    iteration_number: int
    started_at: str
    completed_at: str
    symbols_requested: int
    symbols_queued: int
    symbols_completed: int
    valid_activations: int
    still_watching: int
    rejected_no_edge: int
    data_issues: int
    runtime_sec: float
    portfolio_summary: dict[str, Any] | None = None
    symbol_health_summary: dict[str, Any] | None = None


@dataclass(frozen=True)
class ScanRunRecord:
    run_id: str
    timestamp: str
    exchange: str
    universe: str
    symbols_scanned: int
    symbols_json: str
    strategy: str
    timeframes_json: str
    market_regime: str
    regime_confidence: int
    regime_compatibility_json: str
    environment_notes_json: str
    runtime_stats_json: str
    command_preset: str
    command_used: str
    total_valid_setups: int
    near_misses: int
    rejected: int
    data_issues: int
    data_issues_json: str
    raw_payload_json: str
    is_watch_iteration: int = 0
    watch_iteration_number: int | None = None
    started_at: str | None = None
    completed_at: str | None = None
    symbols_requested: int = 0
    symbols_queued: int = 0
    symbols_completed: int = 0
    valid_activations: int = 0
    still_watching: int = 0
    rejected_no_edge: int = 0
    runtime_sec: float | None = None
    portfolio_summary_json: str = "{}"
    symbol_health_summary_json: str = "{}"
    actionable_setups: int = 0
    actionable_a_grade_setups: int = 0
    actionable_a_grade_target_caution: int = 0
    confirmed_setups: int = 0
    candidate_a_grade_setups: int = 0
    blocked_a_grade_by_scoring: int = 0
    blocked_a_grade_by_target: int = 0
    blocked_a_grade_by_entry_window: int = 0
    blocked_a_grade_by_trust: int = 0
    fatal_target_blocks: int = 0
    soft_target_warnings: int = 0


@dataclass(frozen=True)
class SymbolResultRecord:
    run_id: str
    symbol: str
    status: str
    display_bucket: str
    readiness_score: int
    setup_quality_score: str
    edge_score: str
    failed_gate: str
    rejection_reason: str
    next_trigger_needed: str
    action_label: str
    regime_state: str
    regime_confidence: str
    regime_compatibility_score: str
    regime_compatibility_label: str
    regime_penalty: int
    environment_notes_json: str
    derivatives_context_json: str
    volume_profile_context_json: str
    pullback_status: str
    portfolio_decision: str
    raw_result_json: str


@dataclass(frozen=True)
class SetupCandidateRecord:
    run_id: str
    symbol: str
    mode: str
    direction: str
    entry: str
    stop: str
    tp1: str
    tp2: str
    tp3: str
    rr: str
    invalidation: str
    quality_grade: str
    candidate_quality_grade: str
    final_quality_grade: str
    technical_score: str
    opportunity_score: str
    failed_gate: str
    final_failed_gate: str
    final_block_reason: str
    target_integrity_status: str
    target_failure: str
    target_failure_severity: str
    target_warning_reason: str
    actionability_state: str
    trust_meter: str
    risk_warning: str
    raw_candidate_json: str


@dataclass(frozen=True)
class ReplayResultRecord:
    run_id: str
    setup_fingerprint: str
    outcome: str
    filled: int
    tp_hit: str
    sl_hit: int
    final_r: str
    time_in_trade: str
    regime: str
    symbol: str
    mode: str
    raw_result_json: str


@dataclass(frozen=True)
class TelegramAlertAttemptRecord:
    signal_id: str
    symbol: str
    direction: str
    previous_state: str
    new_state: str
    alert_type: str
    lifecycle_state: str
    sent_at: str | None
    telegram_status: str
    message_hash: str
    scan_run_id: str | None = None
    attempted_at: str = "N/A"
    attempted_alert_type: str = "N/A"
    setup_quality_score: str = "N/A"
    canonical_setup_quality_score: str = "N/A"
    effective_min_setup_quality_score: str = "N/A"
    quality_grade: str = "N/A"
    min_quality_grade: str = "N/A"
    rr_planned: str = "N/A"
    min_rr: str = "N/A"
    opportunity_score: str = "N/A"
    min_score_for_idea: str = "N/A"
    min_opportunity_score: str = "N/A"
    technical_score: str = "N/A"
    price_level: str = "N/A"
    entry_low: str = "N/A"
    entry_high: str = "N/A"
    stop_loss: str = "N/A"
    tp1: str = "N/A"
    tp2: str = "N/A"
    tp3: str = "N/A"
    blocked_reason: str = "N/A"
    invalid_target_fields: str = "N/A"
    error_message: str = "N/A"
    first_seen_at: str = "N/A"
    last_seen_at: str = "N/A"
    seen_count: int = 1
    last_scan_run_id: str | None = None
    last_error_message: str = "N/A"
    public_watchlist_plan_id: str = "N/A"
    public_watchlist_event_key: str = "N/A"
    public_alert_event_type: str = "N/A"
    normalized_entry_zone_low: str = "N/A"
    normalized_entry_zone_high: str = "N/A"
    normalized_invalidation: str = "N/A"
    dedupe_status: str = "N/A"
    dedupe_reason: str = "N/A"
    delivery_state: str = "N/A"
    telegram_message_id: str | None = None
    telegram_chat_id: str | None = None
    delivery_part_count: int = 1
    id: int | None = None

@dataclass(frozen=True)
class PublicAlertEventRecord:
    canonical_plan_id: str
    event_type: str
    event_key: str
    symbol: str
    side: str
    setup_family: str = "N/A"
    structural_anchor: str = "N/A"
    normalized_zone_low: str = "N/A"
    normalized_zone_high: str = "N/A"
    normalized_invalidation: str = "N/A"
    raw_entry_low: str = "N/A"
    raw_entry_high: str = "N/A"
    raw_stop_loss: str = "N/A"
    status: str = "RESERVED"
    reserved_at: str | None = None
    sent_at: str | None = None
    source_modes: str = "N/A"
    matched_prior_alert_id: int | None = None
    matched_prior_event_id: int | None = None
    failure_reason: str = "N/A"
    delivery_state: str = "PENDING"
    payload_text: str = "N/A"
    message_hash: str = "N/A"
    destination_chat_id: str = "N/A"
    destination_kind: str = "N/A"
    attempt_id: str | None = None
    claim_owner: str | None = None
    claimed_at: str | None = None
    attempt_started_at: str | None = None
    lease_expires_at: str | None = None
    attempt_count: int = 0
    max_attempts: int = 3
    next_retry_at: str | None = None
    last_error_category: str = "N/A"
    last_error_detail: str = "N/A"
    telegram_message_id: str | None = None
    telegram_chat_id: str | None = None
    part_count: int = 1
    completed_at: str | None = None
    uncertain_at: str | None = None
    created_at: str = "N/A"
    updated_at: str = "N/A"
    id: int | None = None


@dataclass(frozen=True)
class ScanHistorySummary:
    run_id: str
    timestamp: str
    universe: str
    symbols_scanned: int
    total_valid_setups: int
    near_misses: int
    rejected: int
    data_issues: int
    market_regime: str
    runtime_seconds: str
    regime_confidence: int = 0
    actionable_setups: int = 0
    actionable_a_grade_setups: int = 0
    actionable_a_grade_target_caution: int = 0
    confirmed_setups: int = 0
    candidate_a_grade_setups: int = 0
    blocked_a_grade_by_scoring: int = 0
    blocked_a_grade_by_target: int = 0
    blocked_a_grade_by_entry_window: int = 0
    blocked_a_grade_by_trust: int = 0
    fatal_target_blocks: int = 0
    soft_target_warnings: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "timestamp": self.timestamp,
            "universe": self.universe,
            "symbols_scanned": self.symbols_scanned,
            "valid_setups": self.total_valid_setups,
            "actionable_setups": self.actionable_setups,
            "actionable_a_grade_setups": self.actionable_a_grade_setups,
            "actionable_a_grade_target_caution": self.actionable_a_grade_target_caution,
            "confirmed_setups": self.confirmed_setups,
            "candidate_a_grade_setups": self.candidate_a_grade_setups,
            "blocked_a_grade_by_scoring": self.blocked_a_grade_by_scoring,
            "blocked_a_grade_by_target": self.blocked_a_grade_by_target,
            "blocked_a_grade_by_entry_window": self.blocked_a_grade_by_entry_window,
            "blocked_a_grade_by_trust": self.blocked_a_grade_by_trust,
            "fatal_target_blocks": self.fatal_target_blocks,
            "soft_target_warnings": self.soft_target_warnings,
            "near_misses": self.near_misses,
            "rejected": self.rejected,
            "data_issues": self.data_issues,
            "regime": self.market_regime,
            "regime_confidence": self.regime_confidence,
            "runtime": self.runtime_seconds,
        }
