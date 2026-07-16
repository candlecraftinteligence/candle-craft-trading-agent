from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.data.dtos import NA


class SetupLifecycleState(str, Enum):
    DISCOVERED = "DISCOVERED"
    REJECTED = "REJECTED"
    WATCHLISTED = "WATCHLISTED"
    STALKING = "STALKING"
    TRIGGERED = "TRIGGERED"
    CONFIRMED = "CONFIRMED"
    ACTIONABLE_A_GRADE = "ACTIONABLE_A_GRADE"
    A_GRADE_WATCH = "A_GRADE_WATCH"
    EXECUTING = "EXECUTING"
    MANAGING = "MANAGING"
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    COOLDOWN = "COOLDOWN"
    COOLED_DOWN = "COOLED_DOWN"
    NO_LONGER_TRACKING = "NO_LONGER_TRACKING"
    REMOVED = "REMOVED"
    CANCELLED = "CANCELLED"
    CANCELED = "CANCELED"
    ARCHIVED = "ARCHIVED"


# Ordered to preserve the lifecycle queue's established urgency semantics while
# ensuring execution-stage plans are observed before discovery work.
ACTIVE_LIFECYCLE_MONITORING_STATES = (
    SetupLifecycleState.EXECUTING,
    SetupLifecycleState.MANAGING,
    SetupLifecycleState.ACTIONABLE_A_GRADE,
    SetupLifecycleState.A_GRADE_WATCH,
    SetupLifecycleState.STALKING,
    SetupLifecycleState.TRIGGERED,
    SetupLifecycleState.CONFIRMED,
    SetupLifecycleState.WATCHLISTED,
)


def lifecycle_requires_market_observation(state: SetupLifecycleState | str) -> bool:
    try:
        normalized = (
            state
            if isinstance(state, SetupLifecycleState)
            else SetupLifecycleState(str(state).strip().upper())
        )
    except ValueError:
        return False
    return normalized in ACTIVE_LIFECYCLE_MONITORING_STATES


def lifecycle_monitoring_priority(state: SetupLifecycleState | str) -> int:
    try:
        normalized = (
            state
            if isinstance(state, SetupLifecycleState)
            else SetupLifecycleState(str(state).strip().upper())
        )
    except ValueError:
        return len(ACTIVE_LIFECYCLE_MONITORING_STATES)
    try:
        return ACTIVE_LIFECYCLE_MONITORING_STATES.index(normalized)
    except ValueError:
        return len(ACTIVE_LIFECYCLE_MONITORING_STATES)


class SetupTransitionReason(str, Enum):
    INITIALIZED = "Lifecycle initialized from current scan snapshot."
    DISCOVERED = "Setup discovered and awaiting stronger structure."
    REJECTED = "Setup rejected by current deterministic gates."
    CONFIRMATION_PENDING = "Setup is waiting for another consistent scan confirmation."
    MULTI_SCAN_CONFIRMED = "Setup passed required consecutive scan confirmations."
    READINESS_IMPROVED = "Readiness improved enough for watchlist."
    SWEEP_APPEARED = "Execution sweep appeared."
    STRUCTURE_SHIFT_CONFIRMED = "5m BOS/CHoCH confirmed after sweep."
    PULLBACK_RR_VALID = "Pullback zone and RR became valid."
    ACTIONABLE_A_GRADE = "Clean A-grade setup map is actionable but not fully confirmed."
    A_GRADE_WATCH = "A-grade setup mapped and waiting for limit zone."
    VALID_TRADE_IDEA = "Valid trade idea exists."
    ENTRY_ZONE_TOUCHED = "Entry zone touched by latest price range."
    ENTRY_ACTIVATED = "Entry activation recorded from a closed execution candle."
    ENTRY_FILL_SIMULATED = "Entry fill simulated or confirmed."
    SETUP_DECAYED = "Setup confidence decayed after no lifecycle progress."
    TAKE_PROFIT_HIT = "Take-profit outcome recorded."
    TP1_MILESTONE = "TP1 milestone recorded internally."
    TP2_MILESTONE = "TP2 milestone recorded internally."
    TP3_MILESTONE = "TP3 milestone recorded internally."
    STOP_LOSS_HIT = "Stop-loss outcome recorded."
    SETUP_INVALIDATED = "Setup invalidated by current structure or failed gate."
    SETUP_EXPIRED = "Setup expired before completion."
    COOLDOWN_STARTED = "Lifecycle moved into cooldown."
    COOLDOWN_EXPIRED = "Cooldown expired."
    REACTIVATED = "New structure reactivated archived setup."
    NO_CHANGE = "No lifecycle transition."
    INVALID_TRANSITION = "Invalid lifecycle transition rejected."


class SetupLifecycleRecord(BaseModel):
    lifecycle_id: str
    symbol: str
    mode: str = NA
    direction: str = NA
    current_state: SetupLifecycleState
    previous_state: SetupLifecycleState | None = None
    first_seen_at: str
    last_seen_at: str
    last_transition_at: str
    failed_gate: str = NA
    candidate_quality_grade: str = NA
    final_quality_grade: str = NA
    technical_score: str = NA
    opportunity_score: str = NA
    final_failed_gate: str = NA
    final_block_reason: str = NA
    target_integrity_status: str = NA
    target_failure: str = NA
    target_failure_severity: str = NA
    target_warning_reason: str = NA
    actionability_state: str = NA
    readiness_score: int = Field(default=0, ge=0, le=100)
    quality_score: int = Field(default=0, ge=0, le=100)
    edge_score: str = NA
    regime_state: str = NA
    action_label: str = NA
    invalidation_reason: str = NA
    cooldown_until: str | None = None
    archived_at: str | None = None
    entry_low: str = NA
    entry_high: str = NA
    stop_loss: str = NA
    tp1: str = NA
    tp2: str = NA
    tp3: str = NA
    rr: str = NA
    invalidation_logic: str = NA
    confirmation_count: int = Field(default=0, ge=0)
    required_confirmation_cycles: int = Field(default=2, ge=1)
    quality_grade_first_seen: str = NA
    quality_grade_current: str = NA
    quality_grade_confirmed: str = NA
    confirmed_at: str | None = None
    decay_count: int = Field(default=0, ge=0)
    decay_reason: str = NA
    symbol_health_score_at_detection: str = NA
    symbol_health_penalty_cycles: int = Field(default=0, ge=0)
    setup_identity: str = NA

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator(
        "mode",
        "direction",
        "failed_gate",
        "candidate_quality_grade",
        "final_quality_grade",
        "technical_score",
        "opportunity_score",
        "final_failed_gate",
        "final_block_reason",
        "target_integrity_status",
        "target_failure",
        "target_failure_severity",
        "target_warning_reason",
        "actionability_state",
        "edge_score",
        "regime_state",
        "action_label",
        "invalidation_reason",
        "entry_low",
        "entry_high",
        "stop_loss",
        "tp1",
        "tp2",
        "tp3",
        "rr",
        "invalidation_logic",
        "quality_grade_first_seen",
        "quality_grade_current",
        "quality_grade_confirmed",
        "decay_reason",
        "symbol_health_score_at_detection",
        "setup_identity",
    )
    @classmethod
    def _normalize_text(cls, value: str | None) -> str:
        if value is None:
            return NA
        text = str(value).strip()
        return text if text else NA


class SetupLifecycleEvent(BaseModel):
    lifecycle_id: str
    timestamp: str
    symbol: str
    from_state: SetupLifecycleState | None = None
    to_state: SetupLifecycleState
    reason: SetupTransitionReason
    scan_run_id: str | None = None
    readiness_score: int = Field(default=0, ge=0, le=100)
    quality_score: int = Field(default=0, ge=0, le=100)
    failed_gate: str = NA
    notes: str = NA

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator("failed_gate", "notes")
    @classmethod
    def _normalize_text(cls, value: str | None) -> str:
        if value is None:
            return NA
        text = str(value).strip()
        return text if text else NA


class SetupLifecycleOutcomeProgress(BaseModel):
    lifecycle_id: str
    plan_identity: str
    symbol: str
    mode: str = NA
    direction: str = NA
    execution_timeframe: str = NA
    evaluation_cursor_open_at: str | None = None
    evaluation_cursor_close_at: str | None = None
    entry_at: str | None = None
    tp1_at: str | None = None
    tp2_at: str | None = None
    tp3_at: str | None = None
    stop_at: str | None = None
    invalidated_at: str | None = None
    outcome_at: str | None = None
    terminal_outcome: str = NA
    integrity_status: str = NA
    diagnostic: str = NA
    metadata_json: str = "{}"
    first_evaluated_at: str
    last_evaluated_at: str

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_progress_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator(
        "lifecycle_id",
        "plan_identity",
        "mode",
        "direction",
        "execution_timeframe",
        "terminal_outcome",
        "integrity_status",
        "diagnostic",
        "metadata_json",
    )
    @classmethod
    def _normalize_progress_text(cls, value: str | None) -> str:
        if value is None:
            return NA
        text = str(value).strip()
        return text if text else NA


class SetupTransitionResult(BaseModel):
    lifecycle_id: str
    symbol: str
    from_state: SetupLifecycleState | None = None
    to_state: SetupLifecycleState
    reason: SetupTransitionReason
    transitioned: bool
    allowed: bool = True
    notes: str = NA
    event: SetupLifecycleEvent | None = None
    record: SetupLifecycleRecord | None = None

    model_config = ConfigDict(frozen=True)


class SetupOutcomeAnalyticsRecord(BaseModel):
    lifecycle_id: str
    symbol: str
    bias: str = NA
    first_seen_at: str
    confirmed_at: str = NA
    entry_zone: str = NA
    stop_loss: str = NA
    tp1: str = NA
    tp2: str = NA
    tp3: str = NA
    quality_at_first_detection: str = NA
    quality_at_confirmation: str = NA
    rr: str = NA
    lifecycle_path: str = NA
    final_outcome: str
    failure_reason: str = NA
    outcome_reason: str = NA
    regime_context: str = NA
    symbol_health_at_detection: str = NA
    raw_payload_json: str = "{}"

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator(
        "bias",
        "confirmed_at",
        "entry_zone",
        "stop_loss",
        "tp1",
        "tp2",
        "tp3",
        "quality_at_first_detection",
        "quality_at_confirmation",
        "rr",
        "lifecycle_path",
        "failure_reason",
        "outcome_reason",
        "regime_context",
        "symbol_health_at_detection",
        "raw_payload_json",
    )
    @classmethod
    def _normalize_text(cls, value: str | None) -> str:
        if value is None:
            return NA
        text = str(value).strip()
        return text if text else NA


__all__ = [
    "SetupLifecycleOutcomeProgress",
    "SetupLifecycleEvent",
    "SetupLifecycleRecord",
    "SetupOutcomeAnalyticsRecord",
    "SetupLifecycleState",
    "SetupTransitionReason",
    "SetupTransitionResult",
]
