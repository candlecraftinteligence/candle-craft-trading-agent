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
    EXECUTING = "EXECUTING"
    MANAGING = "MANAGING"
    TP_HIT = "TP_HIT"
    SL_HIT = "SL_HIT"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    COOLDOWN = "COOLDOWN"
    ARCHIVED = "ARCHIVED"


class SetupTransitionReason(str, Enum):
    INITIALIZED = "Lifecycle initialized from current scan snapshot."
    DISCOVERED = "Setup discovered and awaiting stronger structure."
    REJECTED = "Setup rejected by current deterministic gates."
    READINESS_IMPROVED = "Readiness improved enough for watchlist."
    SWEEP_APPEARED = "Execution sweep appeared."
    STRUCTURE_SHIFT_CONFIRMED = "5m BOS/CHoCH confirmed after sweep."
    PULLBACK_RR_VALID = "Pullback zone and RR became valid."
    VALID_TRADE_IDEA = "Valid trade idea exists."
    ENTRY_FILL_SIMULATED = "Entry fill simulated or confirmed."
    TAKE_PROFIT_HIT = "Take-profit outcome recorded."
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
    readiness_score: int = Field(default=0, ge=0, le=100)
    quality_score: int = Field(default=0, ge=0, le=100)
    edge_score: str = NA
    regime_state: str = NA
    action_label: str = NA
    invalidation_reason: str = NA
    cooldown_until: str | None = None
    archived_at: str | None = None

    model_config = ConfigDict(frozen=True)

    @field_validator("symbol")
    @classmethod
    def _normalize_symbol(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("symbol must not be blank")
        return normalized

    @field_validator("mode", "direction", "failed_gate", "edge_score", "regime_state", "action_label", "invalidation_reason")
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


__all__ = [
    "SetupLifecycleEvent",
    "SetupLifecycleRecord",
    "SetupLifecycleState",
    "SetupTransitionReason",
    "SetupTransitionResult",
]
